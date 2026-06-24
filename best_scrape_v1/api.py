import asyncio
import os
import time
import uuid
import shutil
from contextlib import asynccontextmanager
from typing import Annotated
from urllib.parse import urlparse

import psycopg2
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from scraper import scrape_as_html, _html_to_markdown, validate_strategy_runtimes


_REQUIRED_PG_VARS = ["PGHOST", "PGDATABASE", "PGUSER", "PGPASSWORD"]
_PG_RETRY_INTERVAL = 2   # seconds between connection attempts
_PG_MAX_RETRIES    = 15  # give up after this many attempts (~30 s)


def _check_env_vars() -> None:
    """Raise RuntimeError if any required PostgreSQL env vars are missing."""
    missing = [v for v in _REQUIRED_PG_VARS if not os.environ.get(v)]
    if missing:
        raise RuntimeError(
            f"Missing required environment variable(s): {', '.join(missing)}\n"
            f"Set PGHOST, PGDATABASE, PGUSER, and PGPASSWORD before starting."
        )


def _wait_for_postgres() -> None:
    """Block until a PostgreSQL connection succeeds, then close it.

    Retries every {_PG_RETRY_INTERVAL}s up to {_PG_MAX_RETRIES} times so the
    container can start before the database is ready without crashing.
    """
    params = {
        k: v for k, v in {
            "host":     os.environ.get("PGHOST"),
            "port":     os.environ.get("PGPORT"),
            "dbname":   os.environ.get("PGDATABASE"),
            "user":     os.environ.get("PGUSER"),
            "password": os.environ.get("PGPASSWORD"),
        }.items() if v is not None
    }
    for attempt in range(1, _PG_MAX_RETRIES + 1):
        try:
            conn = psycopg2.connect(**params, connect_timeout=5)
            conn.close()
            print(f"[startup] PostgreSQL connection OK "
                  f"({params.get('host')}:{params.get('port', 5432)}/{params.get('dbname')})")
            return
        except psycopg2.OperationalError as e:
            print(f"[startup] Waiting for PostgreSQL (attempt {attempt}/{_PG_MAX_RETRIES}): {e}")
            if attempt == _PG_MAX_RETRIES:
                raise RuntimeError(
                    f"Could not connect to PostgreSQL after {_PG_MAX_RETRIES} attempts: {e}"
                ) from e
            time.sleep(_PG_RETRY_INTERVAL)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    _check_env_vars()
    _wait_for_postgres()
    await asyncio.to_thread(validate_strategy_runtimes)
    yield


app = FastAPI(title="Best Scraper API", lifespan=_lifespan)

_INLINE_MAX_BYTES  = 20  * 1024 * 1024   # 20 MB  — stream inline
_DISK_MAX_BYTES    = 300 * 1024 * 1024   # 300 MB — hard limit
_DOWNLOAD_DIR      = os.environ.get("SCRAPER_DOWNLOAD_DIR", "/tmp/scraper_downloads")
_STALE_SECONDS     = 4 * 60 * 60         # 4 hours


def _purge_stale_files() -> None:
    """Delete any per-UUID subdirectory older than _STALE_SECONDS."""
    try:
        cutoff = time.time() - _STALE_SECONDS
        for entry in os.scandir(_DOWNLOAD_DIR):
            if entry.is_dir() and entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry.path, ignore_errors=True)
                print(f"[download] purged stale dir: {entry.path}")
    except FileNotFoundError:
        pass


def _save_to_disk(content: bytes, filename: str) -> str:
    """Save content under a unique UUID subdirectory; purge stale files first.

    Returns the UUID key used for the download URL.
    """
    file_uuid = str(uuid.uuid4())
    dest_dir  = os.path.join(_DOWNLOAD_DIR, file_uuid)
    os.makedirs(dest_dir, exist_ok=True)
    with open(os.path.join(dest_dir, filename), "wb") as fh:
        fh.write(content)
    _purge_stale_files()
    return file_uuid


@app.get("/ping")
def ping():
    return {"status": "ok"}


@app.get("/scrape")
def scrape(
    url: Annotated[str, Query(description="URL to scrape")],
    browser: Annotated[bool, Query(description="Use Playwright/nodriver (with JS expand) instead of lightweight HTTP strategies")] = False,
):
    t0 = time.perf_counter()
    try:
        content, content_type, strategy = scrape_as_html(url, browser=browser)
    except RuntimeError as e:
        return JSONResponse(status_code=502, content={"error": str(e)})
    elapsed_s = round(time.perf_counter() - t0, 6)

    if isinstance(content, bytes):
        size_mb  = len(content) / (1024 * 1024)
        filename = urlparse(url).path.rstrip("/").split("/")[-1] or "download"

        if len(content) > _DISK_MAX_BYTES:
            return JSONResponse(
                status_code=413,
                content={"error": f"File too large: {size_mb:.1f} MB (limit {_DISK_MAX_BYTES // (1024 * 1024)} MB)"},
            )

        if len(content) > _INLINE_MAX_BYTES:
            file_uuid    = _save_to_disk(content, filename)
            download_url = f"/download/{file_uuid}"
            return JSONResponse(
                status_code=200,
                content={
                    "download_url": download_url,
                    "filename":     filename,
                    "size_mb":      round(size_mb, 2),
                    "content_type": content_type,
                    "time_s":       elapsed_s,
                },
            )

        return StreamingResponse(
            iter([content]),
            media_type=content_type,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Scrape-Time-S":    str(elapsed_s),
            },
        )

    assert isinstance(content, str)
    return {
        "url":              url,
        "strategy":         strategy,
        "html":             content,
        "markdown":         _html_to_markdown(content, clean=False),
        "cleaned_markdown": _html_to_markdown(content, clean=True),
        "time_s":           elapsed_s,
    }


@app.get("/download/{file_uuid}")
def download(file_uuid: str):
    """Serve a previously saved large file, then delete it from disk."""
    dest_dir = os.path.join(_DOWNLOAD_DIR, file_uuid)

    if not os.path.isdir(dest_dir):
        return JSONResponse(status_code=404, content={"error": "File not found or already downloaded"})

    entries = [e for e in os.scandir(dest_dir) if e.is_file()]
    if not entries:
        shutil.rmtree(dest_dir, ignore_errors=True)
        return JSONResponse(status_code=404, content={"error": "File not found or already downloaded"})

    file_path = entries[0].path
    filename  = entries[0].name

    def _iter_and_delete():
        try:
            with open(file_path, "rb") as fh:
                while chunk := fh.read(1024 * 1024):  # 1 MB chunks
                    yield chunk
        finally:
            shutil.rmtree(dest_dir, ignore_errors=True)
            print(f"[download] deleted after serving: {dest_dir}")

    return StreamingResponse(
        _iter_and_delete(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
