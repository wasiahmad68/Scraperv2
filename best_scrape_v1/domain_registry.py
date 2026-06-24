"""Domain-level strategy registry backed by PostgreSQL.

Tracks which scraping strategy works for each domain, caches browser cookies,
and forgets stale knowledge so the scraper adapts when sites change.

Schema
------
scraper_domains (indexed on domain):
    domain                TEXT PRIMARY KEY
    working_strategy      INTEGER          -- last strategy that succeeded (NULL = unknown)
    failed_strategies     JSONB            -- list of strategy ints that failed
    consecutive_failures  INTEGER          -- how many times working_strategy has failed in a row
    last_success          TIMESTAMPTZ      -- timestamp of last success
    last_failure          TIMESTAMPTZ      -- timestamp of last failure
    cookies               JSONB            -- list of cookie dicts (for browser strategies)
    cookie_expiry         TIMESTAMPTZ      -- earliest cookie expiry
    avg_latency_ms        JSONB            -- dict {strategy: avg_ms} for timing hints

Connection
----------
Reads standard PostgreSQL environment variables:
    PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD
(same variables used by psql, libpq, and most Postgres clients)

Forgetting rules (applied at read time):
    * Cookie expiry         -- cookies cleared when cookie_expiry is in the past.
    * Strategy staleness    -- working_strategy cleared when last_success is older than
                               STRATEGY_TTL_DAYS days (default 7).
    * Consecutive failures  -- if working_strategy fails FAILURE_RESET_COUNT times in a row
                               (default 3) the entire domain entry is wiped and a fresh
                               full-sweep is performed.
"""

import json
import os
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

import psycopg2
import psycopg2.extras

# ── Tuneable constants ────────────────────────────────────────────────────────
STRATEGY_TTL_DAYS: int = 7       # forget working strategy after this many days of no use
COOKIE_TTL_HOURS: int = 1        # max age for stored browser cookies
FAILURE_RESET_COUNT: int = 3     # consecutive failures before wiping the domain entry

# ── PostgreSQL connection parameters from standard env vars ──────────────────
_PG_PARAMS = {
    k: v for k, v in {
        "host":     os.environ.get("PGHOST"),
        "port":     os.environ.get("PGPORT"),
        "dbname":   os.environ.get("PGDATABASE"),
        "user":     os.environ.get("PGUSER"),
        "password": os.environ.get("PGPASSWORD"),
    }.items() if v is not None
}

_local = threading.local()   # thread-local connection cache


# ── Internal helpers ──────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _days_ago(dt: Optional[datetime]) -> Optional[float]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (_utcnow() - dt).total_seconds() / 86400


# ── Registry ──────────────────────────────────────────────────────────────────

class DomainRegistry:
    """Thread-safe, PostgreSQL-backed registry of per-domain scraping knowledge.

    One instance can be shared across threads; each thread gets its own
    connection via thread-local storage.

    Connection is configured via standard PostgreSQL environment variables:
    PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD.

    Usage::

        registry = DomainRegistry()

        info = registry.get("reuters.com")
        if info["working_strategy"]:
            ...

        registry.record_success("reuters.com", strategy=6, cookies=[...], latency_ms=4200)
        registry.record_failure("reuters.com", strategy=1)
    """

    def __init__(self) -> None:
        self._init_db()

    # ── Connection management ────────────────────────────────────────────────

    @contextmanager
    def _conn(self):
        """Yield a thread-local psycopg2 connection; commit on success, rollback on error."""
        conn = getattr(_local, "pg_conn", None)
        # Reconnect if connection is closed or broken
        if conn is None or conn.closed:
            conn = psycopg2.connect(
                **_PG_PARAMS,
                cursor_factory=psycopg2.extras.RealDictCursor,
            )
            conn.autocommit = False
            _local.pg_conn = conn
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def _init_db(self) -> None:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS scraper_domains (
                        domain               TEXT        PRIMARY KEY,
                        working_strategy     INTEGER,
                        failed_strategies    JSONB       NOT NULL DEFAULT '[]',
                        consecutive_failures INTEGER     NOT NULL DEFAULT 0,
                        last_success         TIMESTAMPTZ,
                        last_failure         TIMESTAMPTZ,
                        cookies              JSONB,
                        cookie_expiry        TIMESTAMPTZ,
                        avg_latency_ms       JSONB       NOT NULL DEFAULT '{}'
                    )
                """)
                # domain is already the PK (btree index); add an explicit one
                # so EXPLAIN output is unambiguous and query planner has stats.
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_scraper_domains_domain
                        ON scraper_domains (domain)
                """)

    # ── Public API ───────────────────────────────────────────────────────────

    def get(self, domain: str) -> dict:
        """Return the registry entry for *domain* after applying forgetting rules.

        Always returns a dict with all keys; missing/stale fields are None / [].
        Mutates the DB in-place when stale data is cleared.
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM scraper_domains WHERE domain = %s", (domain,)
                )
                row = cur.fetchone()

        if row is None:
            return self._empty()

        entry = dict(row)
        # psycopg2 returns JSONB as native Python objects already
        entry["failed_strategies"] = entry["failed_strategies"] or []
        entry["avg_latency_ms"]    = entry["avg_latency_ms"]    or {}
        # cookies may be None

        mutated = False

        # ── Rule 1: expire cookies ────────────────────────────────────────────
        cookie_expiry = entry.get("cookie_expiry")
        if cookie_expiry and entry.get("cookies") is not None:
            if cookie_expiry.tzinfo is None:
                cookie_expiry = cookie_expiry.replace(tzinfo=timezone.utc)
            if cookie_expiry <= _utcnow():
                print(f"[registry] {domain}: cookies expired, clearing")
                entry["cookies"]       = None
                entry["cookie_expiry"] = None
                mutated = True

        # ── Rule 2: forget stale working strategy ─────────────────────────────
        age_days = _days_ago(entry.get("last_success"))
        if entry["working_strategy"] is not None and (
            age_days is None or age_days > STRATEGY_TTL_DAYS
        ):
            age_str = f"{age_days:.1f}d" if age_days is not None else "never"
            print(f"[registry] {domain}: strategy {entry['working_strategy']} stale "
                  f"({age_str} > {STRATEGY_TTL_DAYS}d), resetting")
            entry["working_strategy"]     = None
            entry["failed_strategies"]    = []
            entry["consecutive_failures"] = 0
            mutated = True

        if mutated:
            self._upsert(domain, entry)

        return entry

    def record_success(
        self,
        domain: str,
        strategy: int,
        cookies: Optional[list] = None,
        latency_ms: Optional[float] = None,
    ) -> None:
        """Record that *strategy* succeeded for *domain*.

        Args:
            domain:     Registered domain (e.g. ``"reuters.com"``).
            strategy:   Strategy number (1-7) that produced valid content.
            cookies:    List of cookie dicts from the browser session (optional).
                        Each dict should have at least ``name``, ``value``,
                        ``domain``, and optionally ``expires`` (Unix timestamp).
            latency_ms: Time in milliseconds the strategy took (optional).
        """
        entry = self.get(domain)

        # Keep only failures *below* the winning strategy — they are still
        # known-bad.  Failures above it are irrelevant now.
        entry["failed_strategies"]    = [s for s in entry["failed_strategies"] if s < strategy]
        entry["working_strategy"]     = strategy
        entry["consecutive_failures"] = 0
        entry["last_success"]         = _utcnow()

        # Store cookies and compute earliest expiry
        if cookies:
            entry["cookies"] = cookies
            expires_ts = [
                c["expires"] for c in cookies
                if isinstance(c.get("expires"), (int, float)) and c["expires"] > 0
            ]
            if expires_ts:
                earliest = datetime.fromtimestamp(min(expires_ts), tz=timezone.utc)
                capped   = _utcnow() + timedelta(hours=COOKIE_TTL_HOURS)
                entry["cookie_expiry"] = min(earliest, capped)
            else:
                entry["cookie_expiry"] = _utcnow() + timedelta(hours=COOKIE_TTL_HOURS)

        # Update rolling average latency for this strategy
        if latency_ms is not None:
            lats = entry["avg_latency_ms"]
            key  = str(strategy)
            prev = lats.get(key)
            lats[key] = latency_ms if prev is None else round(prev * 0.7 + latency_ms * 0.3, 1)
            entry["avg_latency_ms"] = lats

        print(f"[registry] {domain}: recorded success strategy={strategy} "
              f"cookies={'yes' if cookies else 'no'} latency={latency_ms}ms")
        self._upsert(domain, entry)

    def record_failure(self, domain: str, strategy: int) -> None:
        """Record that *strategy* failed for *domain*.

        If the known working strategy fails FAILURE_RESET_COUNT times in a row
        the entire entry is wiped so the scraper performs a fresh full sweep.
        """
        entry = self.get(domain)

        if strategy not in entry["failed_strategies"]:
            entry["failed_strategies"].append(strategy)

        entry["last_failure"] = _utcnow()

        if entry["working_strategy"] == strategy:
            entry["consecutive_failures"] = (entry.get("consecutive_failures") or 0) + 1
            if entry["consecutive_failures"] >= FAILURE_RESET_COUNT:
                print(f"[registry] {domain}: strategy {strategy} failed "
                      f"{entry['consecutive_failures']} times in a row — wiping entry")
                self._delete(domain)
                return

        print(f"[registry] {domain}: recorded failure strategy={strategy} "
              f"consecutive={entry['consecutive_failures']}")
        self._upsert(domain, entry)

    def planned_order(self, domain: str) -> list[int]:
        """Return the strategy execution order for *domain*.

        Known working strategy is tried first, followed by strategies above it
        (ascending), then any below it — all excluding known-failed strategies.

        Returns [1, 2, 3, 4, 5, 6, 7] when nothing is known about the domain.
        """
        all_strategies = [1, 2, 3, 4, 5, 6, 7]
        entry = self.get(domain)
        ws    = entry.get("working_strategy")
        fails = set(entry.get("failed_strategies") or [])

        if ws is None:
            return [s for s in all_strategies if s not in fails]

        above = [s for s in all_strategies if s > ws and s not in fails]
        below = [s for s in all_strategies if s < ws and s not in fails]
        return [ws] + above + below

    def valid_cookies(self, domain: str) -> Optional[list]:
        """Return stored cookies if still valid, else None."""
        return self.get(domain).get("cookies")  # get() already clears expired ones

    def all_domains(self) -> list[dict]:
        """Return all registry entries (for inspection / debugging)."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM scraper_domains ORDER BY domain")
                return [dict(r) for r in cur.fetchall()]

    def delete(self, domain: str) -> None:
        """Manually remove the registry entry for *domain*."""
        self._delete(domain)
        print(f"[registry] {domain}: entry deleted")

    # ── Internal helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _empty() -> dict:
        return {
            "domain":               None,
            "working_strategy":     None,
            "failed_strategies":    [],
            "consecutive_failures": 0,
            "last_success":         None,
            "last_failure":         None,
            "cookies":              None,
            "cookie_expiry":        None,
            "avg_latency_ms":       {},
        }

    def _upsert(self, domain: str, entry: dict) -> None:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO scraper_domains
                        (domain, working_strategy, failed_strategies, consecutive_failures,
                         last_success, last_failure, cookies, cookie_expiry, avg_latency_ms)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (domain) DO UPDATE SET
                        working_strategy     = EXCLUDED.working_strategy,
                        failed_strategies    = EXCLUDED.failed_strategies,
                        consecutive_failures = EXCLUDED.consecutive_failures,
                        last_success         = EXCLUDED.last_success,
                        last_failure         = EXCLUDED.last_failure,
                        cookies              = EXCLUDED.cookies,
                        cookie_expiry        = EXCLUDED.cookie_expiry,
                        avg_latency_ms       = EXCLUDED.avg_latency_ms
                """, (
                    domain,
                    entry.get("working_strategy"),
                    json.dumps(entry.get("failed_strategies") or []),
                    entry.get("consecutive_failures") or 0,
                    entry.get("last_success"),
                    entry.get("last_failure"),
                    json.dumps(entry["cookies"]) if entry.get("cookies") is not None else None,
                    entry.get("cookie_expiry"),
                    json.dumps(entry.get("avg_latency_ms") or {}),
                ))

    def _delete(self, domain: str) -> None:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM scraper_domains WHERE domain = %s", (domain,))


# ── Module-level singleton ────────────────────────────────────────────────────
try:
    registry = DomainRegistry()
except Exception as _e:
    print(f"[domain_registry] PG unavailable, using in-memory fallback: {_e}", file=sys.stderr)
    import threading

    class _InMemoryRegistry:
        def __init__(self):
            self._data: dict = {}
            self._lock = threading.Lock()

        def get(self, domain: str) -> dict:
            with self._lock:
                return self._data.get(domain, {})

        def planned_order(self, domain: str) -> list:
            return list(range(1, 8))

        def record_success(self, domain: str, strategy: int, cookies=None, latency_ms=None) -> None:
            with self._lock:
                self._data[domain] = {"working_strategy": strategy}

        def record_failure(self, domain: str, strategy: int) -> None:
            pass

    registry = _InMemoryRegistry()  # type: ignore[assignment]
