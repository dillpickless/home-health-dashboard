"""Daily incremental pull from the Oura API v2 into a local DuckDB file.

Raw JSON lands in raw.<endpoint>, keyed by document id (heartrate: timestamp +
source), so reruns are idempotent. meta.sync_state holds a per-endpoint
high-water date; each run re-pulls the last LOOKBACK_DAYS for late data.

Run:  .venv/bin/python oura_sync.py [--start YYYY-MM-DD] [--endpoints a,b] [--full]
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import duckdb

from oura_auth import ENV_PATH, ROOT, TOKEN_PATH, USER_AGENT, load_env, save_tokens

API_BASE = "https://api.ouraring.com/v2/usercollection/"
DB_PATH = ROOT / "oura.duckdb"
TOKEN_LOCK_PATH = ROOT / "tokens.lock"
SYNC_LOCK_PATH = ROOT / "sync.lock"

LOOKBACK_DAYS = 3
DEFAULT_START_DAYS_AGO = 30
REFRESH_MARGIN_SECONDS = 600
MAX_ATTEMPTS = 6
MAX_BACKOFF_SECONDS = 300

# Document endpoints: name -> field holding the document's day.
DOC_ENDPOINTS = {
    "daily_sleep": "day",
    "sleep": "day",
    "daily_readiness": "day",
    "daily_activity": "day",
    "daily_spo2": "day",
    "daily_stress": "day",
    "enhanced_tag": "start_day",
}
# Time-series endpoints: no document id, filtered by datetime.
SERIES_ENDPOINTS = ("heartrate",)
ALL_ENDPOINTS = (*DOC_ENDPOINTS, *SERIES_ENDPOINTS)
CHUNK_DAYS = {"heartrate": 7}
DEFAULT_CHUNK_DAYS = 30


class AuthError(Exception):
    """Refresh token rejected; the user must re-run oura_auth.py."""


class SyncBusy(Exception):
    """Another process holds the sync lock."""


class ApiError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status


# --------------------------------------------------------------------------- locks


@contextmanager
def file_lock(path: Path, blocking: bool = True):
    with open(path, "a") as handle:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(handle, flags)
        except BlockingIOError:
            raise SyncBusy(f"{path.name} is held by another process") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


# --------------------------------------------------------------------------- tokens


def read_tokens() -> dict:
    if not TOKEN_PATH.exists():
        raise AuthError(f"Missing {TOKEN_PATH.name}. Run oura_auth.py first.")
    return json.loads(TOKEN_PATH.read_text(encoding="utf-8"))


def expires_soon(tokens: dict) -> bool:
    expires_in = tokens.get("expires_in")
    if not expires_in:
        return False  # Unknown lifetime: rely on the 401 path instead.
    return time.time() >= tokens["obtained_at"] + expires_in - REFRESH_MARGIN_SECONDS


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def refresh_tokens(stale_access_token: str | None) -> dict:
    """Refresh under an exclusive lock and persist before returning.

    Oura refresh tokens are single-use, so the new pair is written to disk
    atomically before anything uses it. If another process already refreshed
    (the on-disk token differs from the one we saw fail), reuse theirs.
    """
    with file_lock(TOKEN_LOCK_PATH):
        tokens = read_tokens()
        already_refreshed = (
            stale_access_token is not None and tokens["access_token"] != stale_access_token
        )
        if already_refreshed and not expires_soon(tokens):
            return tokens
        if not tokens.get("refresh_token"):
            raise AuthError("No refresh token stored. Run oura_auth.py again.")

        env = load_env(ENV_PATH)
        token_url = tokens.get("token_url") or env["OURA_TOKEN_URL"]
        payload = urllib.parse.urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": env["OURA_CLIENT_ID"],
                "client_secret": env["OURA_CLIENT_SECRET"],
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            token_url,
            data=payload,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                fresh = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # Report only the OAuth error code; never echo the response body.
            try:
                code = json.loads(exc.read().decode("utf-8")).get("error", "unknown")
            except (ValueError, AttributeError):
                code = "unparseable response"
            raise AuthError(
                f"Token refresh failed: HTTP {exc.code} ({code}). "
                "Run oura_auth.py again to re-authorize."
            ) from None
        except urllib.error.URLError as exc:
            raise AuthError(f"Could not reach the token endpoint: {exc.reason}") from None

        if "access_token" not in fresh:
            raise AuthError(f"Refresh response had no access_token. Keys: {sorted(fresh)}")
        fresh.setdefault("refresh_token", tokens["refresh_token"])
        save_tokens(fresh, token_url)
        fsync_dir(ROOT)
        print("Access token refreshed.")
        return read_tokens()


class TokenProvider:
    def __init__(self) -> None:
        self.tokens = read_tokens()

    def access_token(self) -> str:
        if expires_soon(self.tokens):
            self.tokens = refresh_tokens(self.tokens["access_token"])
        return self.tokens["access_token"]

    def force_refresh(self) -> str:
        self.tokens = refresh_tokens(self.tokens["access_token"])
        return self.tokens["access_token"]


# --------------------------------------------------------------------------- HTTP


def backoff_seconds(attempt: int, retry_after: str | None) -> float:
    if retry_after:
        try:
            return min(float(retry_after), MAX_BACKOFF_SECONDS)
        except ValueError:
            pass
    return min(2**attempt + random.uniform(0, 1), MAX_BACKOFF_SECONDS)


def api_get(tokens: TokenProvider, endpoint: str, params: dict) -> dict:
    url = API_BASE + endpoint + "?" + urllib.parse.urlencode(params)
    refreshed_on_401 = False
    for attempt in range(MAX_ATTEMPTS):
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {tokens.access_token()}",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and not refreshed_on_401:
                refreshed_on_401 = True
                tokens.force_refresh()
                continue
            if exc.code == 429 or exc.code >= 500:
                wait = backoff_seconds(attempt, exc.headers.get("Retry-After"))
                print(f"  {endpoint}: HTTP {exc.code}, retrying in {wait:.0f}s")
                time.sleep(wait)
                continue
            detail = exc.read().decode("utf-8", errors="replace")[:200]
            raise ApiError(exc.code, detail) from None
        except (urllib.error.URLError, TimeoutError) as exc:
            wait = backoff_seconds(attempt, None)
            print(f"  {endpoint}: network error ({exc}), retrying in {wait:.0f}s")
            time.sleep(wait)
    raise ApiError(0, f"gave up after {MAX_ATTEMPTS} attempts")


def fetch_all(tokens: TokenProvider, endpoint: str, params: dict) -> tuple[list[dict], int]:
    items: list[dict] = []
    pages = 0
    next_token = None
    while True:
        page_params = dict(params, next_token=next_token) if next_token else params
        body = api_get(tokens, endpoint, page_params)
        items.extend(body.get("data") or [])
        pages += 1
        next_token = body.get("next_token")
        if not next_token:
            return items, pages


# --------------------------------------------------------------------------- DuckDB


def init_db(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("CREATE SCHEMA IF NOT EXISTS raw")
    con.execute("CREATE SCHEMA IF NOT EXISTS meta")
    for endpoint in DOC_ENDPOINTS:
        con.execute(
            f"""CREATE TABLE IF NOT EXISTS raw.{endpoint} (
                id VARCHAR PRIMARY KEY,
                day DATE,
                payload JSON NOT NULL,
                fetched_at TIMESTAMPTZ NOT NULL
            )"""
        )
    con.execute(
        """CREATE TABLE IF NOT EXISTS raw.heartrate (
            ts TIMESTAMPTZ NOT NULL,
            source VARCHAR NOT NULL,
            bpm INTEGER,
            payload JSON NOT NULL,
            fetched_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (ts, source)
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS meta.sync_state (
            endpoint VARCHAR PRIMARY KEY,
            high_water_date DATE,
            last_success_at TIMESTAMPTZ,
            last_rows INTEGER,
            last_error VARCHAR,
            last_error_at TIMESTAMPTZ
        )"""
    )


def get_high_water(con: duckdb.DuckDBPyConnection, endpoint: str) -> dt.date | None:
    row = con.execute(
        "SELECT high_water_date FROM meta.sync_state WHERE endpoint = ?", [endpoint]
    ).fetchone()
    return row[0] if row else None


def set_state(
    con: duckdb.DuckDBPyConnection,
    endpoint: str,
    *,
    high_water: dt.date | None = None,
    rows: int | None = None,
    error: str | None = None,
) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    if error is None:
        con.execute(
            """INSERT INTO meta.sync_state (endpoint, high_water_date, last_success_at, last_rows)
               VALUES (?, ?, ?, ?)
               ON CONFLICT (endpoint) DO UPDATE SET
                 high_water_date = excluded.high_water_date,
                 last_success_at = excluded.last_success_at,
                 last_rows = excluded.last_rows,
                 last_error = NULL,
                 last_error_at = NULL""",
            [endpoint, high_water, now, rows],
        )
    else:
        con.execute(
            """INSERT INTO meta.sync_state (endpoint, last_error, last_error_at)
               VALUES (?, ?, ?)
               ON CONFLICT (endpoint) DO UPDATE SET
                 last_error = excluded.last_error,
                 last_error_at = excluded.last_error_at""",
            [endpoint, error[:500], now],
        )


def upsert(con: duckdb.DuckDBPyConnection, endpoint: str, items: list[dict]) -> set:
    now = dt.datetime.now(dt.timezone.utc)
    if endpoint in DOC_ENDPOINTS:
        day_field = DOC_ENDPOINTS[endpoint]
        # Dedupe by key: overlapping chunks and pages can repeat a document.
        rows = {
            item["id"]: (item["id"], item.get(day_field), json.dumps(item), now)
            for item in items
        }
        sql = f"""INSERT INTO raw.{endpoint} (id, day, payload, fetched_at)
                  VALUES (?, ?, ?, ?)
                  ON CONFLICT (id) DO UPDATE SET
                    day = excluded.day,
                    payload = excluded.payload,
                    fetched_at = excluded.fetched_at"""
    else:
        rows = {
            (item["timestamp"], item["source"]): (
                item["timestamp"],
                item["source"],
                item.get("bpm"),
                json.dumps(item),
                now,
            )
            for item in items
        }
        sql = """INSERT INTO raw.heartrate (ts, source, bpm, payload, fetched_at)
                 VALUES (?, ?, ?, ?, ?)
                 ON CONFLICT (ts, source) DO UPDATE SET
                   bpm = excluded.bpm,
                   payload = excluded.payload,
                   fetched_at = excluded.fetched_at"""
    if rows:
        con.executemany(sql, list(rows.values()))
    return set(rows)


# --------------------------------------------------------------------------- sync


def date_chunks(start: dt.date, end: dt.date, size: int):
    """Yield [chunk_start, chunk_end] pairs; chunk_end is the next chunk's start.

    Neighbouring chunks share a boundary day, which covers either reading of
    end_date (inclusive or exclusive). Upserts absorb the overlap.
    """
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + dt.timedelta(days=size), end)
        yield cursor, chunk_end
        cursor = chunk_end


def chunk_params(endpoint: str, start: dt.date, end: dt.date) -> dict:
    if endpoint in SERIES_ENDPOINTS:
        return {
            "start_datetime": f"{start.isoformat()}T00:00:00+00:00",
            "end_datetime": f"{end.isoformat()}T00:00:00+00:00",
        }
    return {"start_date": start.isoformat(), "end_date": end.isoformat()}


def sync_endpoint(
    con: duckdb.DuckDBPyConnection,
    tokens: TokenProvider,
    endpoint: str,
    default_start: dt.date,
    full: bool,
) -> int:
    today = dt.date.today()
    high_water = None if full else get_high_water(con, endpoint)
    start = high_water - dt.timedelta(days=LOOKBACK_DAYS) if high_water else default_start
    # Pull through tomorrow so today is included whether end_date is exclusive or not.
    end = today + dt.timedelta(days=1)

    seen_keys: set = set()
    total_pages = 0
    for chunk_start, chunk_end in date_chunks(
        start, end, CHUNK_DAYS.get(endpoint, DEFAULT_CHUNK_DAYS)
    ):
        items, pages = fetch_all(tokens, endpoint, chunk_params(endpoint, chunk_start, chunk_end))
        total_pages += pages
        # Commit each chunk with its high-water mark so a crash resumes here.
        con.begin()
        try:
            keys = upsert(con, endpoint, items)
            set_state(con, endpoint, high_water=min(chunk_end, today), rows=len(keys))
            con.commit()
        except BaseException:
            con.rollback()
            raise
        seen_keys |= keys

    print(f"{endpoint:16} {start} -> {today}  pages={total_pages:<3} rows={len(seen_keys)}")
    return len(seen_keys)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--start",
        type=dt.date.fromisoformat,
        default=dt.date.today() - dt.timedelta(days=DEFAULT_START_DAYS_AGO),
        help=f"first-run start date (default: {DEFAULT_START_DAYS_AGO} days ago)",
    )
    parser.add_argument(
        "--endpoints",
        default=",".join(ALL_ENDPOINTS),
        help="comma-separated subset of: " + ", ".join(ALL_ENDPOINTS),
    )
    parser.add_argument("--db", type=Path, default=DB_PATH, help="DuckDB file path")
    parser.add_argument(
        "--full", action="store_true", help="ignore high-water marks and pull from --start"
    )
    args = parser.parse_args()
    args.endpoints = [e.strip() for e in args.endpoints.split(",") if e.strip()]
    unknown = sorted(set(args.endpoints) - set(ALL_ENDPOINTS))
    if unknown:
        parser.error(f"unknown endpoints: {', '.join(unknown)}")
    return args


def run_sync(
    con: duckdb.DuckDBPyConnection,
    endpoints=ALL_ENDPOINTS,
    start: dt.date | None = None,
    full: bool = False,
) -> list[str]:
    """Sync the given endpoints into an open connection; return failed endpoints.

    Raises AuthError if re-authorization is needed and SyncBusy if another
    sync is running. Shared by the CLI and the app server.
    """
    start = start or dt.date.today() - dt.timedelta(days=DEFAULT_START_DAYS_AGO)
    with file_lock(SYNC_LOCK_PATH, blocking=False):
        tokens = TokenProvider()
        init_db(con)
        failures = []
        for endpoint in endpoints:
            try:
                sync_endpoint(con, tokens, endpoint, start, full)
            except ApiError as exc:
                # 403 usually means a missing scope; keep going with the rest.
                print(f"{endpoint:16} FAILED {exc}", file=sys.stderr)
                set_state(con, endpoint, error=str(exc))
                failures.append(endpoint)
        return failures


def main() -> int:
    args = parse_args()
    try:
        con = duckdb.connect(str(args.db))
    except duckdb.IOException:
        print("Database is in use (is app.py running? It syncs on its own).", file=sys.stderr)
        return 3
    try:
        failures = run_sync(con, args.endpoints, args.start, args.full)
    except AuthError as exc:
        print(exc, file=sys.stderr)
        return 2
    except SyncBusy:
        print("Another sync is in progress. Exiting.", file=sys.stderr)
        return 3
    finally:
        con.close()
    if failures:
        print(f"Finished with failures: {', '.join(failures)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
