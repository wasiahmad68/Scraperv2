import io
import asyncio
import inspect
import math
import os
import requests
import re
import json
import subprocess
import sys
import time
import random
import unicodedata
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Optional
from urllib.parse import urlparse as _urlparse
from bs4 import BeautifulSoup
from markitdown import MarkItDown
from domain_registry import registry as _registry

_md = MarkItDown()


_STRATEGY_NAMES = {
    1: "browser UA",
    2: "Facebook UA",
    3: "Googlebot UA",
    4: "cloudscraper",
    5: "curl_cffi",
    6: "Playwright + stealth",
    7: "nodriver + real Chrome",
}

# ── Proxy pool (rotating) ──────────────────────────────────────────────────
# Fetched from Webshare API on first use; cached in-memory for one hour.
_PROXY_USERNAME  = os.environ.get("PROXY_USERNAME", "")
_PROXY_PASSWORD  = os.environ.get("PROXY_PASSWORD", "")
_PROXY_TOKEN     = os.environ.get("PROXY_TOKEN", "")

_proxy_pool: list[str] = []       # list of "http://ip:port"
_proxy_pool_ts: float = 0.0       # last-fetch timestamp
_PROXY_POOL_URL  = f"https://proxy.webshare.io/api/v2/proxy/list/download/{_PROXY_TOKEN}/-/any/sourceip/direct"
_PROXY_CACHE_TTL = 3600           # 1 hour
_PROXY_MAX_RETRY = 5              # give up after this many proxy failures per scrape


def _fetch_proxy_pool() -> list[str]:
    """Fetch proxy list from Webshare, cache it, and return."""
    global _proxy_pool, _proxy_pool_ts
    now = time.time()
    if _proxy_pool and now - _proxy_pool_ts < _PROXY_CACHE_TTL:
        return _proxy_pool
    try:
        r = requests.get(_PROXY_POOL_URL, timeout=15)
        if r.status_code == 200:
            _proxy_pool = [
                f"http://{p.strip()}"
                for p in r.text.strip().split("\n")
                if p.strip()
            ]
            _proxy_pool_ts = now
            print(f"[proxy] fetched {len(_proxy_pool)} proxies from Webshare")
        else:
            print(f"[proxy] fetch failed ({r.status_code}), using cached pool ({len(_proxy_pool)})")
    except Exception as e:
        print(f"[proxy] fetch error: {e}, using cached pool ({len(_proxy_pool)})")
    return _proxy_pool


def _rotate_proxy(failed: str | None = None) -> str | None:
    """Return a random proxy URL with auth, optionally removing *failed* from the pool."""
    pool = _fetch_proxy_pool()
    if not pool:
        return None
    if failed:
        base = failed.split("@", 1)[-1] if "@" in failed else failed
        try:
            pool.remove(base)
            print(f"[proxy] removed dead proxy {base} ({len(pool)} remaining)")
        except ValueError:
            pass
    if not pool:
        return None
    chosen = random.choice(pool)
    proxy_with_auth = chosen.replace("://", f"://{_PROXY_USERNAME}:{_PROXY_PASSWORD}@")
    return proxy_with_auth


# ── Chrome cleanup & timeouts ──────────────────────────────────────────────────

_STRATEGY_TIMEOUTS = {1: 30, 2: 30, 3: 30, 4: 30, 5: 30, 6: 90, 7: 90}


def _kill_orphan_chrome() -> None:
    """Force-kill leftover Chrome/Chromium processes to prevent memory exhaustion."""
    try:
        subprocess.run(
            ["pkill", "-f", "chrome"],
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass


def _run_strategy_with_timeout(
    strategy: int, url: str, session, saved_cookies, skip_warming: bool, proxy: bool,
) -> tuple | None:
    """Run a strategy with a per-strategy timeout, multiplied for proxy retries."""
    timeout = _STRATEGY_TIMEOUTS.get(strategy, 60)
    if proxy:
        timeout *= _PROXY_MAX_RETRY
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            _run_strategy, strategy, url, session, saved_cookies, skip_warming, proxy,
        )
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError:
            print(f"[scrape] strategy {strategy} timed out after {timeout}s")
            return None
        except Exception as e:
            print(f"[scrape] strategy {strategy} failed: {e}")
            return None


# ── Canvas / WebGL noise injection script ─────────────────────────────────────
# Injects subtle pixel-level noise into canvas read-back operations so that
# CF's canvas fingerprint hash doesn't match known bot signatures.
# Tiny ±1 per-channel XOR keeps the visual output indistinguishable but
# makes every toDataURL() / getImageData() call produce a unique result.
_CANVAS_NOISE_JS = """
(function() {
    const _noise = () => Math.random() < 0.5 ? -1 : 1;
    // ── 2-D canvas noise ──────────────────────────────────────────────────
    const _getCtx = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function(type, ...args) {
        const ctx = _getCtx.call(this, type, ...args);
        if (!ctx || type !== '2d') return ctx;
        const _gid = ctx.getImageData.bind(ctx);
        ctx.getImageData = function(x, y, w, h) {
            const d = _gid(x, y, w, h);
            for (let i = 0; i < d.data.length; i += 4) {
                d.data[i]   = Math.max(0, Math.min(255, d.data[i]   + _noise()));
                d.data[i+1] = Math.max(0, Math.min(255, d.data[i+1] + _noise()));
                d.data[i+2] = Math.max(0, Math.min(255, d.data[i+2] + _noise()));
            }
            return d;
        };
        return ctx;
    };
    const _toURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function(type) {
        const ctx = this.getContext('2d');
        if (ctx && this.width > 0 && this.height > 0) {
            const d = ctx.getImageData(0, 0, this.width, this.height);
            ctx.putImageData(d, 0, 0);
        }
        return _toURL.apply(this, arguments);
    };
    // ── WebGL noise (readPixels) ──────────────────────────────────────────
    const _patchGL = (ctx) => {
        if (!ctx) return ctx;
        const _rp = ctx.readPixels.bind(ctx);
        ctx.readPixels = function(x, y, w, h, fmt, type, buf) {
            _rp(x, y, w, h, fmt, type, buf);
            if (buf instanceof Uint8Array) {
                for (let i = 0; i < buf.length; i += 4) {
                    buf[i]   = Math.max(0, Math.min(255, buf[i]   + _noise()));
                    buf[i+1] = Math.max(0, Math.min(255, buf[i+1] + _noise()));
                    buf[i+2] = Math.max(0, Math.min(255, buf[i+2] + _noise()));
                }
            }
        };
        return ctx;
    };
    const _orig = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function(type, ...args) {
        const ctx = _orig.call(this, type, ...args);
        if (type === 'webgl' || type === 'webgl2' || type === 'experimental-webgl')
            return _patchGL(ctx);
        return ctx;
    };
})();
"""


def validate_strategy_runtimes() -> None:
    """Fail fast if any scraper strategy cannot initialize in this runtime.

    The check intentionally avoids external URLs. Strategies 1-5 validate their
    Python/runtime dependencies, while strategies 6-7 launch real Chrome against
    a blank page so container display and sandbox problems fail at API startup.
    """
    failures: list[str] = []

    for strategy in (1, 2, 3):
        try:
            requests.Session().headers.update({})
            print(f"[startup] strategy {strategy} OK: {_STRATEGY_NAMES[strategy]}")
        except Exception as e:
            failures.append(f"strategy {strategy} ({_STRATEGY_NAMES[strategy]}): {e}")

    try:
        import cloudscraper
        cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
        print(f"[startup] strategy 4 OK: {_STRATEGY_NAMES[4]}")
    except Exception as e:
        failures.append(f"strategy 4 ({_STRATEGY_NAMES[4]}): {e}")

    try:
        from curl_cffi import requests as _cffi_requests
        _cffi_requests.Session()
        print(f"[startup] strategy 5 OK: {_STRATEGY_NAMES[5]}")
    except Exception as e:
        failures.append(f"strategy 5 ({_STRATEGY_NAMES[5]}): {e}")

    chrome_path = os.environ.get("CHROME_BIN", "/usr/bin/google-chrome-stable")
    if not os.path.isfile(chrome_path):
        failures.append(
            f"strategy 6/7 browser runtime: Chrome not found at {chrome_path!r}; "
            "set CHROME_BIN"
        )
    else:
        try:
            from playwright.sync_api import sync_playwright
            from playwright_stealth import Stealth

            with sync_playwright() as p:
                browser = p.chromium.launch(
                    executable_path=chrome_path,
                    headless=False,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--disable-dev-shm-usage",
                        "--no-sandbox",
                    ],
                )
                page = browser.new_page()
                Stealth().apply_stealth_sync(page)
                page.goto("about:blank")
                browser.close()
            print(f"[startup] strategy 6 OK: {_STRATEGY_NAMES[6]}")
        except Exception as e:
            failures.append(f"strategy 6 ({_STRATEGY_NAMES[6]}): {e}")

        async def _check_nodriver() -> None:
            import nodriver as uc

            browser = await uc.start(
                browser_executable_path=chrome_path,
                headless=False,
                no_sandbox=True,
                browser_args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            try:
                await browser.get("about:blank")
            finally:
                stop = getattr(browser, "stop", None) or getattr(browser, "close", None)
                if stop is not None:
                    result = stop()
                    if inspect.isawaitable(result):
                        await result

        try:
            asyncio.run(_check_nodriver())
            print(f"[startup] strategy 7 OK: {_STRATEGY_NAMES[7]}")
        except Exception as e:
            failures.append(f"strategy 7 ({_STRATEGY_NAMES[7]}): {e}")

    if failures:
        details = "\n".join(f"- {failure}" for failure in failures)
        print(f"[startup] WARNING: Some strategies failed validation:\n{details}")
        print("[startup] Continuing with available strategies only.")

# Regex matching class/id names that indicate UI boilerplate (not article content).
# Patterns are specific enough to avoid false positives on content text.
_BOILERPLATE_RE = re.compile(
    r"(?:"
    r"cookie[-_]?(?:consent|banner|notice|bar|popup|wall)|"
    r"gdpr[-_]?(?:banner|notice|popup)?|"
    r"newsletter[-_]?(?:signup|subscribe|form|popup|modal|bar)?|"
    r"(?:^|[-_\s])subscribe[-_]?(?:form|modal|popup|banner|wall)?(?:\s|$)|"
    r"(?:share|social)[-_]?(?:bar|button|link|icon)s?|"
    r"social[-_]?(?:media|network)[-_]?(?:link|icon|button)s?|"
    r"(?:popup|modal|lightbox|overlay)[-_]?(?:wrapper|container|overlay|backdrop)?|"
    r"breadcrumb|"
    r"pagination[-_]?(?:wrapper|container|nav)?|"
    r"(?:related|recommended|more[-_]?from|you[-_]?may[-_]?also)[-_](?:article|post|stor|content)s?|"
    r"comment[-_]?(?:section|form|list|area|count)|"
    r"disqus|"
    r"back[-_]to[-_]top|"
    r"skip[-_](?:nav|link|to[-_]content)|"
    r"ad[-_](?:banner|container|unit|slot|wrapper)|"
    r"advertisement[-_]?(?:container|wrapper)?|"
    r"promo[-_]?(?:banner|bar|box|strip)|"
    r"paywall[-_](?:overlay|popup|modal|wall|banner|bar|notice|mask)|"
    r"(?:sticky|fixed)[-_](?:header|footer|bar|nav)"
    r")",
    re.I,
)

# Class patterns for the main article body — used for content extraction.
_CONTENT_CLASS_RE = re.compile(
    r"(?:^|\s)(?:"
    r"article[-_]?(?:body|content|text|main)?|"
    r"post[-_]?(?:content|body|text|article)|"
    r"entry[-_]?(?:content|body|text)|"
    r"story[-_]?(?:body|content|text)|"
    r"content[-_]?(?:body|area|main)|"
    r"article__(?:body|content|text)|"
    r"post__(?:body|content)|"
    r"body[-_]?(?:content|copy|text)"
    r")(?:\s|$)",
    re.I,
)

# Patterns in HTML that indicate JS-expandable content (read more, show full story).
# When a lightweight strategy succeeds but the HTML contains these, the scraper
# automatically retries with a browser strategy that can click the button.
_EXPANDABLE_PATTERNS = re.compile(
    r"(?:"
    r"read[\s-]?more|"
    r"show[\s-]?more|"
    r"full[\s-]?story|"
    r"load[\s-]?more|"
    r"read[\s-]?full[\s-]?story|"
    r"expand[\s-]?story|"
    r"readmore(?:action|wrap|content)|"
    r"showmore(?:btn|wrap|container)"
    r")",
    re.I,
)


def _has_expandable_content(html: str, plain_text: str) -> bool:
    """Return True if the page appears to have hidden content behind JS expand buttons.

    Checks HTML attributes (class/id/data attrs) for expand-related patterns.
    Only skips if the page is too short to contain meaningful content (< 500 chars).
    No upper length bound — even long pages can have expandable sections.
    """
    if len(plain_text) < 500:   # too short to judge, probably a different issue
        return False
    if _EXPANDABLE_PATTERNS.search(html):
        return True
    return False


def _extract_fusion_content(html: str) -> str | None:
    """Extract article body from Arc XP / Fusion.globalContent JSON.

    Some publishers (CTV News, etc.) embed the full article text in a JavaScript
    variable rather than visible HTML.  Returns cleaned HTML on success, None if
    the Fusion data structure is not found.

    Uses brace-depth tracking to safely extract the JSON object — much more
    robust than regex for nested structures.
    """
    m = re.search(r"Fusion\.globalContent\s*=\s*", html)
    if not m:
        return None
    start = m.end()
    while start < len(html) and html[start] in " \n\r\t":
        start += 1
    if start >= len(html) or html[start] != "{":
        return None
    depth = 0
    end = start
    for i in range(start, len(html)):
        if html[i] == "{":
            depth += 1
        elif html[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end == start:
        return None
    try:
        data = json.loads(html[start:end])
        elements = data.get("content_elements") or []
        parts = []
        for el in elements:
            content = el.get("content", "")
            if content and el.get("type") in ("text", "paragraph", "raw_html"):
                parts.append(content)
        if not parts:
            return None
        body = " ".join(parts)
        return f"<article><p>{body}</p></article>"
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def _clean_html(html: str) -> str:
    """Remove boilerplate elements from HTML; return cleaned HTML string."""
    # ── Step 0: extract from Arc XP / Fusion JS data when visible HTML ──────
    # is too polluted with sidebar content (CTV News etc.).
    fusion_html = _extract_fusion_content(html)
    if fusion_html:
        return fusion_html

    soup = BeautifulSoup(html, "html.parser")

    # ── Step 1: isolate main article content if a clear container exists ──────
    main = (
        soup.find("article")
        or soup.find("main")
        or soup.find(attrs={"role": "main"})
        or soup.find(class_=_CONTENT_CLASS_RE)
    )
    if main and len(main.get_text(strip=True)) > 400:
        title = soup.find("title")
        title_html = f"<h1>{title.get_text(strip=True)}</h1>\n" if title else ""
        soup = BeautifulSoup(title_html + str(main), "html.parser")

    # ── Step 2: remove structural non-content tags ────────────────────────────
    for tag in soup(["nav", "footer", "aside", "iframe", "noscript"]):
        tag.decompose()

    # ── Step 3: remove by ARIA landmark roles ─────────────────────────────────
    for role in ("navigation", "banner", "contentinfo", "complementary", "search"):
        for tag in soup.find_all(attrs={"role": role}):
            tag.decompose()

    # ── Step 4: remove boilerplate by class/id keyword patterns ──────────────
    # Snapshot the list first: decomposing tags during iteration can leave
    # "zombie" entries whose attrs have been cleared to None.
    for tag in list(soup.find_all(True)):
        attrs = getattr(tag, "attrs", None)
        if attrs is None:
            continue
        cls = " ".join(attrs.get("class", []) or [])
        tid = attrs.get("id") or ""
        if _BOILERPLATE_RE.search(cls) or _BOILERPLATE_RE.search(tid):
            tag.decompose()

    return str(soup)


def _clean_markdown(md: str) -> str:
    """Collapse 3+ consecutive blank lines down to two."""
    return re.sub(r"\n{3,}", "\n\n", md).strip()


# Standard browser headers — no Accept-Encoding so requests handles decompression automatically
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://www.google.com/",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# Facebook crawler UA — causes social sites to return OG meta tags server-side
_FACEBOOK_HEADERS = {
    "User-Agent": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# Googlebot UA — some publishers whitelist Googlebot
_GOOGLEBOT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# Phrases that indicate a bot-challenge or login wall — not real article content
_BLOCK_PHRASES = [
    "just a moment",
    "enable javascript and cookies",
    "verifying you are human",
    "please wait while we check",
    "access denied",
    "please enable cookies to continue",
    "log in to see",
    "sign in to see",
    "performing security verification",
    "waiting for www.",
    "verify you are human",
    "security verification",
]


def _normalize(text: str) -> str:
    """NFC-normalise and replace fancy Unicode punctuation with ASCII equivalents."""
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\u2018", "'").replace("\u2019", "'")  # curly single quotes
    text = text.replace("\u201c", '"').replace("\u201d", '"')  # curly double quotes
    text = text.replace("\u2013", "-").replace("\u2014", "-")  # en/em dashes
    text = text.replace("\u00a0", " ")                         # non-breaking space
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _html_to_text(html: str) -> str:
    """Parse HTML and return clean visible text (used for block-detection only)."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "iframe", "head"]):
        tag.decompose()
    return _normalize(soup.get_text(separator=" ", strip=True))


def _html_to_markdown(html: str, clean: bool = False) -> str:
    """Parse HTML and return clean markdown."""
    if clean:
        html = _clean_html(html)
    buf = io.BytesIO(html.encode("utf-8"))
    result = _md.convert(buf, file_extension=".html")
    md = result.text_content if not result.markdown.strip() else result.markdown
    if clean:
        md = _clean_markdown(md)
    return md


def _is_blocked(text: str) -> bool:
    """Return True if the text looks like a challenge/login page instead of real content."""
    sample = text.lower()[:600]
    return any(phrase in sample for phrase in _BLOCK_PHRASES)


def _human_move(page, sx: float, sy: float, ex: float, ey: float, steps: int = 0) -> None:
    """Move the mouse along a Bezier curve with subtle jitter — mimics human motion.

    Speed follows a parabolic profile: slow at start/end, fast in the middle.
    A random control point curves the path so it is never perfectly straight.
    """
    if not steps:
        steps = max(20, int(math.hypot(ex - sx, ey - sy) / 8))
    # Random curve offset — larger distance → bigger possible detour
    mx = (sx + ex) / 2 + random.randint(-60, 60)
    my = (sy + ey) / 2 + random.randint(-40, 40)
    for i in range(steps + 1):
        t = i / steps
        x = (1 - t) ** 2 * sx + 2 * (1 - t) * t * mx + t ** 2 * ex + random.gauss(0, 0.8)
        y = (1 - t) ** 2 * sy + 2 * (1 - t) * t * my + t ** 2 * ey + random.gauss(0, 0.8)
        page.mouse.move(x, y)
        # Parabolic speed: peaks at t=0.5 → natural acceleration / deceleration
        speed = 0.5 + 2.0 * t * (1 - t)
        time.sleep(random.uniform(0.003, 0.012) / speed)


_EXPAND_CONTENT_JS = '''(() => {
    const selectors = [
        "button.read-more",    "a.read-more",
        "button.show-more",    "a.show-more",
        "button.load-more",    "a.load-more",
        "button.expand-story", "a.expand-story",
        "button.full-story",   "a.full-story",
        "button.read-full",    "a.read-full",
        "button.see-more",     "a.see-more",
        "button.story-expand",
        "a[class*=\\"read-more\\"]",  "a[class*=\\"show-more\\"]",
        "a[class*=\\"load-more\\"]",  "a[class*=\\"expand-story\\"]",
        "a[class*=\\"story-expand\\"]",
        "button[class*=\\"read-more\\"]", "button[class*=\\"show-more\\"]",
        "button[class*=\\"load-more\\"]", "button[class*=\\"expand-story\\"]",
        "button[class*=\\"story-expand\\"]",
        "#showMore", "#readMore", "#loadMore", "#expandStory",
        "[data-expand]", "[data-read-more]", "[data-show-more]",
        "[data-action=\\"expand\\"]", "[data-action=\\"read-more\\"]",
    ];
    let clicked = 0;
    for (const sel of selectors) {
        for (const el of document.querySelectorAll(sel)) {
            if (el.offsetParent === null) continue;
            if (el.getAttribute("aria-expanded") === "true") continue;
            if (el.classList.contains("active") || el.classList.contains("expanded")) continue;
            el.click();
            clicked++;
        }
    }
    const keywords = ["read more", "show more", "load more", "full story",
                      "view full story", "see full story", "read full story",
                      "expand story", "show full story"];
    for (const el of document.querySelectorAll("button, a, span[role=\\"button\\"], div[role=\\"button\\"]")) {
        const text = (el.textContent || "").trim().toLowerCase();
        if (keywords.some(k => text.includes(k))) {
            if (el.offsetParent === null) continue;
            if (el.getAttribute("aria-expanded") === "true") continue;
            if (el.classList.contains("active") || el.classList.contains("expanded")) continue;
            el.click();
            clicked++;
        }
    }
    return clicked;
})()'''


def _expand_page_content(page) -> int:
    """Click 'read more' / 'show full story' / 'load more' buttons.

    Uses Playwright's *page.evaluate* to run ``_EXPAND_CONTENT_JS``.
    Returns the number of buttons clicked.
    """
    try:
        clicked = page.evaluate(_EXPAND_CONTENT_JS)
        if clicked:
            print(f"[scrape] clicked {clicked} expand button(s), waiting for content to load")
            time.sleep(3)
        return clicked
    except Exception as e:
        print(f"[scrape] expand-content JS failed: {e}")
        return 0


def _click_cf_checkbox(page) -> bool:
    """Detect a Cloudflare Turnstile checkbox and click it with human-like movement.

    Cloudflare embeds the Turnstile widget via a shadow-DOM iframe that cannot be
    directly accessed from Playwright.  Instead we locate the widget container in
    the main DOM (``div[id^="cf-chl-widget"]``), compute the checkbox's on-screen
    position, and drive the mouse there naturally before clicking.

    Returns True if we found and clicked the widget, False if no challenge was detected.
    """
    # Quick sanity check: are we actually on a CF challenge page?
    try:
        title = page.title().lower()
    except Exception:
        return False
    if "just a moment" not in title and "performing security" not in title:
        return False

    print("[scrape] Cloudflare challenge detected — attempting checkbox click")

    # Find the widget wrapper to get its screen position.
    # The hidden <input id="cf-chl-widget-*_response"> is always present; its
    # closest block ancestor gives us the widget bounding box.
    widget_rect = page.evaluate("""() => {
        const inp = document.querySelector('input[id^="cf-chl-widget"]');
        if (!inp) return null;
        // Walk up to the first block-level container that has non-zero size
        let el = inp.parentElement;
        while (el) {
            const r = el.getBoundingClientRect();
            if (r.width > 50 && r.height > 30) return {x: r.x, y: r.y, w: r.width, h: r.height};
            el = el.parentElement;
        }
        return null;
    }""")

    if not widget_rect:
        print("[scrape] CF widget container not found in DOM")
        return False

    # The checkbox sits in the left ~30 px of the widget; vertically centred
    cb_x = widget_rect["x"] + 20
    cb_y = widget_rect["y"] + widget_rect["h"] / 2

    # Start from a neutral position and approach the checkbox naturally
    cur_x, cur_y = 640.0, 200.0
    page.mouse.move(cur_x, cur_y)
    time.sleep(random.uniform(0.3, 0.6))

    # Wander toward the widget in two intermediate hops
    mid1_x = (cur_x + cb_x) / 2 + random.randint(-40, 40)
    mid1_y = (cur_y + cb_y) / 2 + random.randint(-30, 30)
    _human_move(page, cur_x, cur_y, mid1_x, mid1_y)
    time.sleep(random.uniform(0.04, 0.12))
    _human_move(page, mid1_x, mid1_y, cb_x, cb_y, steps=15)
    time.sleep(random.uniform(0.08, 0.18))

    page.mouse.click(cb_x, cb_y)
    print(f"[scrape] Clicked checkbox at ({cb_x:.0f}, {cb_y:.0f})")
    return True


def _resolve_response(r, strategy: int) -> tuple[str | bytes, str, int] | None:
    """Given a successful requests.Response, return (content, content_type, strategy).

    If the response is HTML, content is a decoded string and content_type is 'text/html'.
    If it is any other type (PDF, image, etc.), content is raw bytes and content_type is
    the actual MIME type from the response headers.
    Returns None if the response looks like a bot-block page.
    """
    ct = r.headers.get("Content-Type", "text/html").split(";")[0].strip().lower()
    is_html = "html" in ct or ct.startswith("text/")
    if not is_html:
        print(f"[scrape] strategy {strategy} detected non-HTML content-type: {ct}")
        return r.content, ct, strategy
    plain = _html_to_text(r.text)
    if len(plain) > 200 and not _is_blocked(plain):
        return r.text, ct, strategy
    print(f"[scrape] strategy {strategy} rejected: len={len(plain)} blocked={_is_blocked(plain)}")
    return None


def _inline_iframes(outer_html: str, frame_map: dict, base_url: str) -> tuple[str, int]:
    """Replace <iframe> tags in outer_html with <div> containing the frame's body HTML.

    Args:
        outer_html: The outer page HTML (from page.content() or tab.get_content()).
        frame_map:  Dict mapping keys to frame HTML strings. Keys tried in order:
                    resolved absolute src URL, raw src, frame name, frame id.
        base_url:   The page URL, used to resolve relative src values.

    Returns:
        Tuple of (modified HTML string, number of iframes inlined).
    """
    from urllib.parse import urljoin as _urljoin

    soup = BeautifulSoup(outer_html, "html.parser")
    inlined = 0

    for iframe_tag in soup.find_all("iframe"):
        src = iframe_tag.get("src", "").strip()
        name = iframe_tag.get("name", "")
        id_ = iframe_tag.get("id", "")

        resolved_src = _urljoin(base_url, src) if src else ""
        frame_html = (
            frame_map.get(resolved_src)
            or frame_map.get(src)
            or frame_map.get(name)
            or frame_map.get(id_)
        )
        if not frame_html:
            continue

        try:
            fsoup = BeautifulSoup(frame_html, "html.parser")
            fbody = fsoup.find("body")
            inner = fbody.decode_contents() if fbody else frame_html

            div = soup.new_tag("div", attrs={"class": "iframe-content"})
            div.append(BeautifulSoup(inner, "html.parser"))
            iframe_tag.replace_with(div)
            inlined += 1
        except Exception:
            pass

    return str(soup), inlined


def _run_strategy(
    strategy: int,
    url: str,
    session: requests.Session,
    saved_cookies: Optional[list],
    skip_warming: bool = False,
    proxy: bool = False,
) -> tuple[str | bytes, str, int] | None:
    """Run a single numbered strategy. Returns a result tuple on success, None on failure.

    Args:
        strategy:     Strategy number 1-7.
        url:          Target URL.
        session:      Shared requests.Session for strategies 1-5.
        saved_cookies: Cookie list from the registry (used by strategies 6-7).
        skip_warming: If True, skip homepage cookie-warming in strategies 6/7.
                      Set when this domain's working strategy is already known to
                      be 6 or 7 (i.e. warming was done in a prior successful run).
    """
    # ── Strategy 1: Standard browser UA ──────────────────────────────────────
    _proxy_url = None

    if strategy == 1:
        _proxy_to_remove = None
        _content_block = False
        _direct_tried = False
        for _attempt in range((_PROXY_MAX_RETRY + 1) if proxy else 1):
            if proxy and _direct_tried:
                break
            if proxy:
                if _attempt < _PROXY_MAX_RETRY and not _content_block:
                    _proxy_url = _rotate_proxy(failed=_proxy_to_remove)
                    _proxy_to_remove = None
                else:
                    if _content_block:
                        print("[scrape] content blocked via proxy, trying direct")
                        _content_block = False
                    else:
                        print("[scrape] all proxies failed, trying direct")
                    _proxy_url = None
                    _direct_tried = True
                if _proxy_url:
                    print(f"[scrape] proxy attempt {_attempt+1}: {_proxy_url.split('@')[-1]}")
                elif _attempt == 0:
                    print("[scrape] proxy pool empty, falling back to direct")
                elif _direct_tried or _attempt >= _PROXY_MAX_RETRY:
                    pass
                else:
                    break
            try:
                r = session.get(url, headers=_BROWSER_HEADERS, timeout=30, allow_redirects=True,
                                proxies={"http": _proxy_url, "https": _proxy_url} if _proxy_url else None)
                print(f"[scrape] strategy 1 status: {r.status_code}")
                if r.status_code == 200:
                    return _resolve_response(r, 1)
                if _proxy_url and (r.status_code in (403, 503) or "Just a moment" in r.text[:300]):
                    print(f"[scrape] strategy 1: content blocked ({r.status_code}), skipping to direct")
                    _content_block = True
                    continue
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as _te:
                print(f"[scrape] strategy 1 attempt {_attempt+1} transport error: {_te}")
                _proxy_to_remove = _proxy_url
            except Exception as e:
                print(f"[scrape] strategy 1 attempt {_attempt+1} failed: {e}")
        return None

    # ── Strategy 2: Facebook crawler UA ──────────────────────────────────────
    if strategy == 2:
        _proxy_to_remove = None
        _content_block = False
        _direct_tried = False
        for _attempt in range((_PROXY_MAX_RETRY + 1) if proxy else 1):
            if proxy and _direct_tried:
                break
            if proxy:
                if _attempt < _PROXY_MAX_RETRY and not _content_block:
                    _proxy_url = _rotate_proxy(failed=_proxy_to_remove)
                    _proxy_to_remove = None
                else:
                    if _content_block:
                        print("[scrape] content blocked via proxy, trying direct")
                        _content_block = False
                    else:
                        print("[scrape] all proxies failed, trying direct")
                    _proxy_url = None
                    _direct_tried = True
                if _proxy_url:
                    print(f"[scrape] proxy attempt {_attempt+1}: {_proxy_url.split('@')[-1]}")
                elif _attempt == 0:
                    print("[scrape] proxy pool empty, falling back to direct")
                elif _direct_tried or _attempt >= _PROXY_MAX_RETRY:
                    pass
                else:
                    break
            try:
                r = session.get(url, headers=_FACEBOOK_HEADERS, timeout=25, allow_redirects=True,
                                proxies={"http": _proxy_url, "https": _proxy_url} if _proxy_url else None)
                print(f"[scrape] strategy 2 status: {r.status_code}")
                if r.status_code == 200:
                    return _resolve_response(r, 2)
                if _proxy_url and (r.status_code in (403, 503) or "Just a moment" in r.text[:300]):
                    print(f"[scrape] strategy 2: content blocked ({r.status_code}), skipping to direct")
                    _content_block = True
                    continue
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as _te:
                print(f"[scrape] strategy 2 attempt {_attempt+1} transport error: {_te}")
                _proxy_to_remove = _proxy_url
            except Exception as e:
                print(f"[scrape] strategy 2 attempt {_attempt+1} failed: {e}")
        return None

    # ── Strategy 3: Googlebot UA ──────────────────────────────────────────────
    if strategy == 3:
        _proxy_to_remove = None
        _content_block = False
        _direct_tried = False
        for _attempt in range((_PROXY_MAX_RETRY + 1) if proxy else 1):
            if proxy and _direct_tried:
                break
            if proxy:
                if _attempt < _PROXY_MAX_RETRY and not _content_block:
                    _proxy_url = _rotate_proxy(failed=_proxy_to_remove)
                    _proxy_to_remove = None
                else:
                    if _content_block:
                        print("[scrape] content blocked via proxy, trying direct")
                        _content_block = False
                    else:
                        print("[scrape] all proxies failed, trying direct")
                    _proxy_url = None
                    _direct_tried = True
                if _proxy_url:
                    print(f"[scrape] proxy attempt {_attempt+1}: {_proxy_url.split('@')[-1]}")
                elif _attempt == 0:
                    print("[scrape] proxy pool empty, falling back to direct")
                elif _direct_tried or _attempt >= _PROXY_MAX_RETRY:
                    pass
                else:
                    break
            try:
                r = session.get(url, headers=_GOOGLEBOT_HEADERS, timeout=30, allow_redirects=True,
                                proxies={"http": _proxy_url, "https": _proxy_url} if _proxy_url else None)
                print(f"[scrape] strategy 3 status: {r.status_code}")
                if r.status_code == 200:
                    return _resolve_response(r, 3)
                if _proxy_url and (r.status_code in (403, 503) or "Just a moment" in r.text[:300]):
                    print(f"[scrape] strategy 3: content blocked ({r.status_code}), skipping to direct")
                    _content_block = True
                    continue
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as _te:
                print(f"[scrape] strategy 3 attempt {_attempt+1} transport error: {_te}")
                _proxy_to_remove = _proxy_url
            except Exception as e:
                print(f"[scrape] strategy 3 attempt {_attempt+1} failed: {e}")
        return None

    # ── Strategy 4: cloudscraper (Cloudflare JS challenges) ──────────────────
    if strategy == 4:
        _proxy_to_remove = None
        _content_block = False
        _direct_tried = False
        for _attempt in range((_PROXY_MAX_RETRY + 1) if proxy else 1):
            if proxy and _direct_tried:
                break
            if proxy:
                if _attempt < _PROXY_MAX_RETRY and not _content_block:
                    _proxy_url = _rotate_proxy(failed=_proxy_to_remove)
                    _proxy_to_remove = None
                else:
                    if _content_block:
                        print("[scrape] content blocked via proxy, trying direct")
                        _content_block = False
                    else:
                        print("[scrape] all proxies failed, trying direct")
                    _proxy_url = None
                    _direct_tried = True
                if _proxy_url:
                    print(f"[scrape] proxy attempt {_attempt+1}: {_proxy_url.split('@')[-1]}")
                elif _attempt == 0:
                    print("[scrape] proxy pool empty, falling back to direct")
                elif _direct_tried or _attempt >= _PROXY_MAX_RETRY:
                    pass
                else:
                    break
            try:
                import cloudscraper
                cs = cloudscraper.create_scraper(
                    browser={"browser": "chrome", "platform": "windows", "mobile": False}
                )
                r = cs.get(url, timeout=30,
                           proxies={"http": _proxy_url, "https": _proxy_url} if _proxy_url else None)
                print(f"[scrape] strategy 4 status: {r.status_code}")
                if r.status_code == 200:
                    return _resolve_response(r, 4)
                if _proxy_url and (r.status_code in (403, 503) or "Just a moment" in r.text[:300]):
                    print(f"[scrape] strategy 4: content blocked ({r.status_code}), skipping to direct")
                    _content_block = True
                    continue
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as _te:
                print(f"[scrape] strategy 4 attempt {_attempt+1} transport error: {_te}")
                _proxy_to_remove = _proxy_url
            except Exception as e:
                print(f"[scrape] strategy 4 attempt {_attempt+1} failed: {e}")
        return None

    # ── Strategy 5: curl_cffi with real TLS fingerprint ──────────────────────
    if strategy == 5:
        _proxy_to_remove = None
        _content_block = False
        _direct_tried = False
        for _attempt in range((_PROXY_MAX_RETRY + 1) if proxy else 1):
            if proxy and _direct_tried:
                break
            if proxy:
                if _attempt < _PROXY_MAX_RETRY and not _content_block:
                    _proxy_url = _rotate_proxy(failed=_proxy_to_remove)
                    _proxy_to_remove = None
                else:
                    if _content_block:
                        print("[scrape] content blocked via proxy, trying direct")
                        _content_block = False
                    else:
                        print("[scrape] all proxies failed, trying direct")
                    _proxy_url = None
                    _direct_tried = True
                if _proxy_url:
                    print(f"[scrape] proxy attempt {_attempt+1}: {_proxy_url.split('@')[-1]}")
                elif _attempt == 0:
                    print("[scrape] proxy pool empty, falling back to direct")
                elif _direct_tried or _attempt >= _PROXY_MAX_RETRY:
                    pass
                else:
                    break
            try:
                from curl_cffi import requests as cffi_requests
                r = cffi_requests.get(url, impersonate="chrome124", timeout=30,
                                      proxies={"http": _proxy_url, "https": _proxy_url} if _proxy_url else None)
                print(f"[scrape] strategy 5 status: {r.status_code}")
                if r.status_code == 200:
                    return _resolve_response(r, 5)
                if _proxy_url and (r.status_code in (403, 503) or "Just a moment" in r.text[:300]):
                    print(f"[scrape] strategy 5: content blocked ({r.status_code}), skipping to direct")
                    _content_block = True
                    continue
            except Exception as e:
                print(f"[scrape] strategy 5 attempt {_attempt+1} failed: {e}")
        return None

    # ── Strategy 6: Playwright + stealth + canvas noise + cookie warming ──────
    # Launches headed Chromium with stealth patches, canvas/WebGL noise injection,
    # and a homepage pre-visit to collect cookies before the target.
    # Saved cookies from a previous successful session are injected up-front,
    # which can bypass CF Turnstile entirely when cf_clearance is still valid.
    if strategy == 6:
        _pw_result = None
        _proxy_to_remove = None
        _content_block = False
        _direct_tried = False
        for _attempt in range((_PROXY_MAX_RETRY + 1) if proxy else 1):
            if proxy and _direct_tried:
                break
            if proxy:
                if _attempt < _PROXY_MAX_RETRY and not _content_block:
                    _proxy_url = _rotate_proxy(failed=_proxy_to_remove)
                    _proxy_to_remove = None
                else:
                    if _content_block:
                        print("[scrape] content blocked via proxy, trying direct")
                        _content_block = False
                    else:
                        print("[scrape] all proxies failed, trying direct")
                    _proxy_url = None
                    _direct_tried = True
                if _proxy_url:
                    print(f"[scrape] proxy attempt {_attempt+1}: {_proxy_url.split('@')[-1]}")
                elif _attempt == 0:
                    print("[scrape] proxy pool empty, falling back to direct")
                elif _direct_tried or _attempt >= _PROXY_MAX_RETRY:
                    pass
                else:
                    break
            try:
                from playwright.sync_api import sync_playwright
                from playwright_stealth import Stealth
                _pw_proxy = None
                if _proxy_url:
                    _parsed_pw = _urlparse(_proxy_url)
                    _pw_proxy = {
                        "server": f"{_parsed_pw.scheme}://{_parsed_pw.hostname}:{_parsed_pw.port}",
                        "username": _parsed_pw.username,
                        "password": _parsed_pw.password,
                    }

                with sync_playwright() as p:
                    browser = p.chromium.launch(
                        executable_path="/usr/bin/google-chrome-stable",
                        headless=False,
                        proxy=_pw_proxy,
                        args=[
                            "--disable-blink-features=AutomationControlled",
                            "--disable-dev-shm-usage",
                            "--no-sandbox",
                        ],
                    )
                    context = browser.new_context(
                        user_agent=(
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36"
                        ),
                        viewport={"width": 1280, "height": 800},
                        locale="en-US",
                        timezone_id="America/New_York",
                        extra_http_headers={
                            "sec-ch-ua": (
                                '"Google Chrome";v="124", "Chromium";v="124", '
                                '"Not-A.Brand";v="99"'
                            ),
                        },
                    )
                    context.add_init_script(_CANVAS_NOISE_JS)

                    # Inject saved cookies before any navigation
                    if saved_cookies:
                        try:
                            context.add_cookies(saved_cookies)
                            print(f"[scrape] strategy 6: injected {len(saved_cookies)} saved cookie(s)")
                        except Exception as _ce:
                            print(f"[scrape] strategy 6: cookie injection failed: {_ce}")

                    page = context.new_page()
                    Stealth().apply_stealth_sync(page)

                    # Capture the initial HTTP response body before JavaScript modifies
                    # the DOM.  Some sites (e.g. The Guardian's DCR framework) serve the
                    # correct article server-side but JS then overwrites it with a
                    # different version.  We keep both and pick the one with more text.
                    _initial_response_body: list[bytes] = []

                    def _on_response(resp):
                        if not _initial_response_body and resp.request and resp.request.url == url:
                            try:
                                _body = resp.body()
                                _initial_response_body.append(_body)
                                print(f"[scrape] strategy 6: captured SSR response "
                                      f"({len(_body)} bytes, "
                                      f"starts with: {_body[:80]})")
                            except Exception as e:
                                print(f"[scrape] strategy 6: SSR capture failed: {e}")

                    page.on("response", _on_response)

                    _parsed  = _urlparse(url)
                    _homepage = f"{_parsed.scheme}://{_parsed.netloc}/"

                    # Browser strategies handle CF natively (stealth, canvas noise,
                    # checkbox click).  Homepage warming is not needed and can break
                    # session-based content routing (e.g. The Guardian serves wrong
                    # article version after a homepage visit).
                    print(f"[scrape] strategy 6: warming skipped (native CF bypass)")

                    page.goto(url, timeout=60000, wait_until="domcontentloaded")
                    time.sleep(4)

                    if _click_cf_checkbox(page):
                        deadline = time.time() + 30
                        while time.time() < deadline:
                            time.sleep(2)
                            if "just a moment" not in page.title().lower():
                                print("[scrape] CF challenge cleared")
                                break
                        else:
                            print("[scrape] CF challenge did not clear within 30 s")

                    try:
                        page.wait_for_load_state("networkidle", timeout=20000)
                    except Exception as e:
                        print(f"[scrape] networkidle timeout: {e}")

                    _expand_page_content(page)

                    outer_html = page.content()
                    frame_map: dict = {}
                    for frame in page.frames:
                        try:
                            if frame.url and frame.url not in ("about:blank", ""):
                                frame_map[frame.url] = frame.content()
                            if frame.name:
                                frame_map.setdefault(frame.name, frame.content())
                        except Exception:
                            pass

                    html, inlined = _inline_iframes(outer_html, frame_map, url)
                    print(f"[scrape] strategy 6: inlined {inlined} iframe(s), "
                          f"plain_len={len(_html_to_text(html))}")

                    # Prefer the initial SSR response over the JS-modified DOM.
                    # Some JS-heavy sites (e.g. The Guardian's DCR framework) serve the
                    # correct article server-side but client-side JS then replaces it
                    # with a different version.  Use the SSR unless it has minimal text
                    # (which indicates a SPA loading shell before JS renders content).
                    if _initial_response_body:
                        _ssr_html = _initial_response_body[0].decode("utf-8", errors="replace")
                        _ssr_plain = len(_html_to_text(_ssr_html))
                        print(f"[scrape] strategy 6: SSR={_ssr_plain}c JS={len(_html_to_text(html))}c")
                        if _ssr_plain >= 500:
                            print(f"[scrape] strategy 6: using SSR response")
                            html = _ssr_html
                        else:
                            print(f"[scrape] strategy 6: SSR too short (< 500c), using JS DOM")

                    # Harvest cookies for storage in registry
                    harvested_cookies = context.cookies()
                    browser.close()

                plain = _html_to_text(html)
                if len(plain) > 200 and not _is_blocked(plain):
                    _pw_result = (html, "text/html", 6, harvested_cookies)
                elif _proxy_url and _is_blocked(plain):
                    print(f"[scrape] strategy 6: content blocked via proxy, skipping to direct")
                    _content_block = True
                    continue
            except (ConnectionAbortedError, ConnectionResetError, OSError) as _te:
                print(f"[scrape] strategy 6 attempt {_attempt+1} transport error: {_te}")
                _proxy_to_remove = _proxy_url
            except Exception as e:
                print(f"[scrape] strategy 6 attempt {_attempt+1} failed: {e}")
            finally:
                _kill_orphan_chrome()
            if _pw_result:
                return _pw_result  # type: ignore[return-value]
        return None

    # ── Strategy 7: nodriver + real Chrome (Private Access Token support) ─────
    # Only real Google Chrome can satisfy CF's PAT attestation.  nodriver drives
    # it via CDP without exposing WebDriver.
    if strategy == 7:
        _nd_result = None
        _proxy_to_remove = None
        _content_block = False
        _direct_tried = False
        for _attempt in range((_PROXY_MAX_RETRY + 1) if proxy else 1):
            if proxy and _direct_tried:
                break
            if proxy:
                if _attempt < _PROXY_MAX_RETRY and not _content_block:
                    _proxy_url = _rotate_proxy(failed=_proxy_to_remove)
                    _proxy_to_remove = None
                else:
                    if _content_block:
                        print("[scrape] content blocked via proxy, trying direct")
                        _content_block = False
                    else:
                        print("[scrape] all proxies failed, trying direct")
                    _proxy_url = None
                    _direct_tried = True
                if _proxy_url:
                    print(f"[scrape] proxy attempt {_attempt+1}: {_proxy_url.split('@')[-1]}")
                elif _attempt == 0:
                    print("[scrape] proxy pool empty, falling back to direct")
                elif _direct_tried or _attempt >= _PROXY_MAX_RETRY:
                    pass
                else:
                    break
            try:
                import asyncio
                import nodriver as uc

                chrome_path = os.environ.get("CHROME_BIN", "/usr/bin/google-chrome-stable")
                if not os.path.isfile(chrome_path):
                    raise FileNotFoundError(
                        f"Chrome not found at {chrome_path!r}; set CHROME_BIN"
                    )

                _nd_browser_args = ["--no-sandbox", "--disable-dev-shm-usage"]
                if _proxy_url:
                    _nd_browser_args.append(f"--proxy-server={_proxy_url}")

                async def _fetch_with_nodriver() -> tuple[str, list]:
                    browser = await uc.start(
                        browser_executable_path=chrome_path,
                        headless=False,
                        no_sandbox=True,
                        browser_args=_nd_browser_args,
                    )
                    try:
                        tab = await browser.get("about:blank")

                        # Inject canvas noise via CDP
                        try:
                            import nodriver.cdp.page as _cdp_page
                            await tab.send(
                                _cdp_page.add_script_to_evaluate_on_new_document(
                                    source=_CANVAS_NOISE_JS
                                )
                            )
                            print("[scrape] nodriver: canvas noise script registered")
                        except Exception as _ce:
                            print(f"[scrape] nodriver: canvas injection skipped ({_ce})")

                        # Inject saved cookies via CDP before any navigation
                        if saved_cookies:
                            try:
                                import nodriver.cdp.network as _cdp_network
                                for _c in saved_cookies:
                                    await tab.send(_cdp_network.set_cookie(
                                        name=_c["name"],
                                        value=_c["value"],
                                        domain=_c.get("domain", ""),
                                        path=_c.get("path", "/"),
                                        secure=_c.get("secure", False),
                                        http_only=_c.get("httpOnly", False),
                                    ))
                                print(f"[scrape] nodriver: injected {len(saved_cookies)} saved cookie(s)")
                            except Exception as _ci:
                                print(f"[scrape] nodriver: cookie injection failed: {_ci}")

                        _parsed   = _urlparse(url)
                        _homepage = f"{_parsed.scheme}://{_parsed.netloc}/"

                        # Browser strategies handle CF natively (stealth, canvas noise,
                        # checkbox click).  Homepage warming is not needed and can break
                        # session-based content routing (e.g. The Guardian serves wrong
                        # article version after a homepage visit).
                        print("[scrape] nodriver: warming skipped (native CF bypass)")

                        await tab.get(url)
                        await asyncio.sleep(5)

                        # Poll until CF clears and real content arrives (up to 90 s)
                        deadline = time.monotonic() + 90
                        while time.monotonic() < deadline:
                            title         = await tab.evaluate("document.title")
                            html_snapshot = await tab.get_content()
                            plain         = _html_to_text(html_snapshot)
                            if len(plain) > 200 and not _is_blocked(plain):
                                print(f"[scrape] nodriver: page loaded, title={title!r}")
                                break

                            print(f"[scrape] nodriver: waiting for page, title={title!r}")

                            if "just a moment" in title.lower() or "security" in plain.lower():
                                rect_json = await tab.evaluate(
                                    "JSON.stringify((() => {"
                                    "  const inp = document.querySelector('input[id^=\"cf-chl-widget\"]');"
                                    "  if (!inp) return null;"
                                    "  let el = inp.parentElement;"
                                    "  while (el) {"
                                    "    const r = el.getBoundingClientRect();"
                                    "    if (r.width > 50 && r.height > 30)"
                                    "      return {x: r.x, y: r.y, w: r.width, h: r.height};"
                                    "    el = el.parentElement;"
                                    "  }"
                                    "  return null;"
                                    "})()"
                                    ")"
                                )
                                if isinstance(rect_json, str) and rect_json != "null":
                                    rect = json.loads(rect_json)
                                    cb_x = rect["x"] + 20
                                    cb_y = rect["y"] + rect["h"] / 2
                                    await tab.mouse_click(cb_x, cb_y)
                                    print(f"[scrape] nodriver: clicked checkbox at ({cb_x:.0f}, {cb_y:.0f})")
                                else:
                                    print("[scrape] nodriver: CF widget not found in DOM yet")

                            await asyncio.sleep(5)

                        # Expand "read more" / "show full story" buttons
                        _expand_count = await tab.evaluate(_EXPAND_CONTENT_JS)
                        if _expand_count:
                            print(f"[scrape] nodriver: clicked {_expand_count} expand button(s), waiting for content")
                            await asyncio.sleep(3)

                        # Inline iframes into the final HTML
                        _outer     = await tab.get_content()
                        _frame_map: dict = {}
                        try:
                            _iframes_js = await tab.evaluate(
                                "JSON.stringify(Array.from(document.querySelectorAll('iframe')).map(f => ({"
                                "  src: f.src || '',"
                                "  name: f.name || '',"
                                "  id: f.id || '',"
                                "  html: (() => { try { return f.contentDocument && f.contentDocument.documentElement"
                                "    ? f.contentDocument.documentElement.outerHTML : ''; } catch(e) { return ''; } })()"
                                "})))"
                            )
                            if isinstance(_iframes_js, str):
                                for _fi in json.loads(_iframes_js):
                                    if _fi.get("html"):
                                        if _fi.get("src"):
                                            _frame_map[_fi["src"]] = _fi["html"]
                                        if _fi.get("name"):
                                            _frame_map.setdefault(_fi["name"], _fi["html"])
                                        if _fi.get("id"):
                                            _frame_map.setdefault(_fi["id"], _fi["html"])
                        except Exception as _fe:
                            print(f"[scrape] nodriver: iframe JS collection failed: {_fe}")

                        _inlined_html, _inlined_count = _inline_iframes(_outer, _frame_map, url)
                        print(f"[scrape] nodriver: inlined {_inlined_count} iframe(s)")

                        # Harvest cookies for registry storage
                        try:
                            _cookies_js = await tab.evaluate(
                                "JSON.stringify(document.cookie.split('; ').map(c => {"
                                "  const [name, ...rest] = c.split('=');"
                                "  return {name, value: rest.join('=')};"
                                "}))"
                            )
                            _harvested = json.loads(_cookies_js) if isinstance(_cookies_js, str) else []
                        except Exception as _che:
                            print(f"[scrape] nodriver: cookie harvest failed: {_che}")
                            _harvested = []
                        return _inlined_html, _harvested
                    finally:
                        browser.stop()

                _nd_html, _nd_cookies = asyncio.run(_fetch_with_nodriver())
                _nd_plain = _html_to_text(_nd_html)
                if len(_nd_plain) > 200 and not _is_blocked(_nd_plain):
                    _nd_result = (_nd_html, "text/html", 7, _nd_cookies)
                elif _proxy_url and _is_blocked(_nd_plain):
                    print(f"[scrape] strategy 7: content blocked via proxy, skipping to direct")
                    _content_block = True
                    continue
            except (ConnectionAbortedError, ConnectionResetError, OSError) as _te:
                print(f"[scrape] strategy 7 attempt {_attempt+1} transport error: {_te}")
                _proxy_to_remove = _proxy_url
            except Exception as e:
                print(f"[scrape] strategy 7 attempt {_attempt+1} failed: {e}")
            finally:
                _kill_orphan_chrome()
            if _nd_result:
                return _nd_result  # type: ignore[return-value]
        return None

    raise ValueError(f"Unknown strategy: {strategy}")


def _handle_browser_result(
    result: tuple[str | bytes, str, int] | tuple[str | bytes, str, int, list],
    domain: str,
    latency_ms: float,
) -> tuple[str | bytes, str, int]:
    """Extract content, record success, and return result for a browser strategy."""
    content, content_type, strat = result[:3]
    harvested = result[3] if len(result) == 4 else None
    _registry.record_success(
        domain, strat,
        cookies=harvested or None,
        latency_ms=round(latency_ms, 1),
    )
    print(f"[scrape] strategy {strat} succeeded ({latency_ms:.0f} ms)")
    return content, content_type, strat


def scrape_as_html(url: str, browser: bool = False, proxy: bool = False) -> tuple[str | bytes, str, int]:
    """Fetch a URL and return (content, content_type, strategy_number).

    Consults the domain registry to determine the best strategy order and
    injects saved browser cookies when available.  Records success and failure
    outcomes so future calls to the same domain are faster.

    Args:
        url:     Target URL to scrape.
        browser: If True, skip lightweight HTTP strategies (1-5) and use
                 Playwright (6) or nodriver (7) directly.  Useful for JS-heavy
                 news sites where content is behind "read more" expand buttons.

    content is an HTML string when the URL points to a web page, or raw bytes
    when it points to a file (PDF, image, etc.).  content_type is the MIME type
    taken directly from the winning response's Content-Type header.
    """
    _kill_orphan_chrome()
    session = requests.Session()
    print(f"[scrape] {url}")

    domain       = _urlparse(url).netloc
    _entry       = _registry.get(domain)
    order        = _registry.planned_order(domain)
    saved_cookies = _entry.get("cookies")  # already expiry-checked by get()

    if browser:
        order = [s for s in order if s >= 6] or [6, 7]
        print(f"[registry] {domain}: browser mode forced, order = {order}")
    skip_warming = _entry.get("working_strategy") in (6, 7)

    if saved_cookies:
        print(f"[registry] {domain}: using {len(saved_cookies)} saved cookie(s)")
    print(f"[registry] {domain}: strategy order = {order}")

    expandable_seen = False

    for strategy in order:
        if expandable_seen and strategy < 6:
            print(f"[scrape] strategy {strategy}: skipping (expandable content already detected)")
            _registry.record_failure(domain, strategy)
            continue
        print(f"[scrape] trying strategy {strategy}: {_STRATEGY_NAMES.get(strategy, '?')}")
        t0 = time.monotonic()
        try:
            result = _run_strategy_with_timeout(strategy, url, session, saved_cookies, skip_warming=skip_warming, proxy=proxy)
            latency_ms = (time.monotonic() - t0) * 1000

            if strategy >= 6:
                _kill_orphan_chrome()

            if result is None:
                print(f"[scrape] strategy {strategy} rejected")
                if not proxy and strategy >= 6:
                    print(f"[scrape] retrying strategy {strategy} with proxy")
                    _registry.record_failure(domain, strategy)
                    t0 = time.monotonic()
                    result = _run_strategy_with_timeout(strategy, url, session, saved_cookies, skip_warming=skip_warming, proxy=True)
                    latency_ms = (time.monotonic() - t0) * 1000
                    _kill_orphan_chrome()
                    if result is not None:
                        return _handle_browser_result(result, domain, latency_ms)
                    print(f"[scrape] strategy {strategy} also rejected with proxy")
                _registry.record_failure(domain, strategy)
                continue

            if len(result) == 4:
                content, content_type, strat, harvested = result
                _registry.record_success(
                    domain, strat,
                    cookies=harvested or None,
                    latency_ms=round(latency_ms, 1),
                )
                print(f"[scrape] strategy {strat} succeeded ({latency_ms:.0f} ms)")
                return content, content_type, strat

            content, content_type, strat = result

            if strat < 6 and isinstance(content, str) and content_type == "text/html":
                plain = _html_to_text(content)
                if _has_expandable_content(content, plain):
                    print(f"[scrape] strategy {strat}: expandable content detected "
                          f"(plain={len(plain)}c), will retry with browser")
                    _registry.record_failure(domain, strat)
                    expandable_seen = True
                    continue

            _registry.record_success(domain, strat, latency_ms=round(latency_ms, 1))
            print(f"[scrape] strategy {strat} succeeded ({latency_ms:.0f} ms)")
            return content, content_type, strat

        except Exception as e:
            latency_ms = (time.monotonic() - t0) * 1000
            print(f"[scrape] strategy {strategy} failed: {e}")
            if strategy >= 6:
                _kill_orphan_chrome()
            if not proxy and strategy >= 6:
                print(f"[scrape] retrying strategy {strategy} with proxy")
                t0 = time.monotonic()
                try:
                    result = _run_strategy_with_timeout(strategy, url, session, saved_cookies, skip_warming=skip_warming, proxy=True)
                    latency_ms = (time.monotonic() - t0) * 1000
                    _kill_orphan_chrome()
                    if result is not None:
                        return _handle_browser_result(result, domain, latency_ms)
                except Exception as e2:
                    print(f"[scrape] strategy {strategy} also failed with proxy: {e2}")
                print(f"[scrape] strategy {strategy} also failed with proxy")
            _registry.record_failure(domain, strategy)

    raise RuntimeError(f"All scraping strategies failed for: {url}")


def scrape_as_markdown(url: str, clean: bool = False, browser: bool = False, proxy: bool = False) -> str:
    """Fetch a URL and return its content as markdown.

    Args:
        url:     The URL to scrape.
        clean:   If True, strip boilerplate (nav, footer, cookie banners, etc.)
                 and collapse excess blank lines. Default False.
        browser: If True, use Playwright/nodriver (with JS expand support) instead
                 of lightweight HTTP strategies.
        proxy:   If True, route requests through the configured HTTP proxy.
    """
    html, _, _ = scrape_as_html(url, browser=browser, proxy=proxy)
    return _html_to_markdown(html, clean=clean)


def run_tests(json_path: str = "scraper_text.json", clean: bool = False) -> bool:
    with open(json_path, "r") as f:
        test_cases = json.load(f)

    passed = 0
    failed = 0

    for case in test_cases:
        url = case["link"]
        expected_texts = case["contains_text"]

        try:
            text = scrape_as_markdown(url, clean=clean)
            norm_text = _normalize(text)
            missing = [t for t in expected_texts if _normalize(t) not in norm_text]

            if not missing:
                print(f"PASS: {url}")
                passed += 1
            else:
                print(f"FAIL: {url}")
                for m in missing:
                    print(f"      missing: {m[:80]!r}")
                failed += 1

        except Exception as e:
            print(f"ERROR: {url}")
            print(f"       {e}")
            failed += 1

    print(f"\n{'='*60}")
    print(f"Results: {passed} passed, {failed} failed out of {len(test_cases)} total")
    return failed == 0


if __name__ == "__main__":
    ok = run_tests()
    sys.exit(0 if ok else 1)
    # print(scrape_url("https://www.indiatvnews.com/bihar/news-bihar-samrat-choudhary-likely-to-become-chief-minister-pm-modi-amit-shah-to-attend-swearing-in-ceremony-2026-04-13-1037348"))
    # print(scrape_as_markdown("https://www.dfat.gov.au/trade/agreements/not-yet-in-force/aeufta", clean=True))
    # print(scrape_url("https://www.dfat.gov.au/news"))
    # print(scrape_url("https://www.transportation.gov/mission/meet-key-officials"))
    # print(scrape_url("https://www.dni.gov/index.php/who-we-are/leadership"))
    # print(scrape_url("https://ballotpedia.org/Wilbur_Ross"))
    # text = scrape_as_markdown("https://www.bizjournals.com/triad/news/2019/01/23/burlington-cone-denim-owner-itg-changing-name.html", clean=False)
    # print(text)
    # needle = "Cone Denim owner ITG changing name"
    # if needle.lower() in text.lower():
    #     print(f"\n✓ SUCCESS: Found {needle!r}")
    # else:
    #     print(f"\n✗ NOT FOUND: {needle!r}")
