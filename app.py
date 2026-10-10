"""Oura dashboard server: serves the phone web app and syncs Oura in the background.

Run:  .venv/bin/python app.py [--port 8765]

Holds the only connection to oura.duckdb (DuckDB allows one writing process),
so oura_sync.py runs inside this process on a timer instead of from cron.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import http.server
import json
import mimetypes
import os
import secrets
import statistics
import sys
import threading
import time
import traceback
import urllib.parse
from pathlib import Path

import duckdb

import cycle
import oura_sync
from oura_auth import ROOT

STATIC_DIR = ROOT / "static"
PIN_PATH = ROOT / "app_pin.txt"
SECRET_PATH = ROOT / "app_secret.txt"
SYNC_INTERVAL_SECONDS = 2 * 60 * 60
TREND_DAYS = 90
BASELINE_DAYS = 28
MIN_BASELINE_NIGHTS = 14
SESSION_MAX_AGE = 365 * 24 * 60 * 60
MAX_FAILED_LOGINS = 10
LOCKOUT_SECONDS = 600

mimetypes.add_type("application/manifest+json", ".webmanifest")


# --------------------------------------------------------------------------- secrets


def read_or_create(path: Path, make) -> str:
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(make())
    return path.read_text().strip()


PIN = read_or_create(PIN_PATH, lambda: f"{secrets.randbelow(10**6):06d}")
SECRET = read_or_create(SECRET_PATH, lambda: secrets.token_hex(32)).encode()
# Changing the PIN logs every device out.
SESSION_TOKEN = hmac.new(SECRET, PIN.encode(), hashlib.sha256).hexdigest()


class LoginGuard:
    def __init__(self) -> None:
        self.failures = 0
        self.locked_until = 0.0
        self.lock = threading.Lock()

    def check(self, pin: str) -> tuple[bool, str | None]:
        with self.lock:
            if time.time() < self.locked_until:
                return False, "Too many attempts. Try again in a few minutes."
            if hmac.compare_digest(pin.encode(), PIN.encode()):
                self.failures = 0
                return True, None
            self.failures += 1
            if self.failures >= MAX_FAILED_LOGINS:
                self.failures = 0
                self.locked_until = time.time() + LOCKOUT_SECONDS
            time.sleep(1)
            return False, "Wrong PIN."


# --------------------------------------------------------------------------- database


class Store:
    def __init__(self, path: Path) -> None:
        # The dataset is tiny; keep DuckDB from sizing itself to the whole machine.
        self.con = duckdb.connect(str(path), config={"memory_limit": "256MB", "threads": 2})
        oura_sync.init_db(self.con)
        self.con.execute("CREATE SCHEMA IF NOT EXISTS app")
        self.con.execute(
            """CREATE TABLE IF NOT EXISTS app.period_log (
                day DATE PRIMARY KEY,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )"""
        )
        self.write_lock = threading.Lock()
        self.sync_status = {"running": False, "last_finished": None, "error": None}

    def cursor(self) -> duckdb.DuckDBPyConnection:
        return self.con.cursor()

    # ---- sync

    def sync(self) -> None:
        if self.sync_status["running"]:
            return
        self.sync_status["running"] = True
        try:
            with self.write_lock:
                failures = oura_sync.run_sync(self.cursor())
            self.sync_status["error"] = f"Failed: {', '.join(failures)}" if failures else None
        except oura_sync.AuthError as exc:
            self.sync_status["error"] = str(exc)
        except oura_sync.SyncBusy:
            self.sync_status["error"] = None
        except Exception as exc:  # Keep the server alive; surface the problem in the UI.
            traceback.print_exc()
            self.sync_status["error"] = f"Sync error: {type(exc).__name__}"
        finally:
            self.sync_status["running"] = False
            self.sync_status["last_finished"] = dt.datetime.now(dt.timezone.utc).isoformat()

    def sync_in_background(self) -> None:
        threading.Thread(target=self.sync, daemon=True).start()

    def sync_forever(self) -> None:
        while True:
            self.sync()
            time.sleep(SYNC_INTERVAL_SECONDS)

    # ---- periods

    def add_period(self, day: dt.date) -> None:
        with self.write_lock:
            self.cursor().execute(
                "INSERT INTO app.period_log (day) VALUES (?) ON CONFLICT DO NOTHING", [day]
            )

    def delete_period(self, day: dt.date) -> None:
        with self.write_lock:
            self.cursor().execute("DELETE FROM app.period_log WHERE day = ?", [day])

    # ---- reads

    def daily_rows(self, since: dt.date) -> list[dict]:
        cur = self.cursor()
        cur.execute(
            """
            WITH days AS (
                SELECT unnest(generate_series(?::DATE, current_date, INTERVAL 1 DAY))::DATE AS day
            ),
            main_sleep AS (
                SELECT day,
                       (payload->>'average_hrv')::DOUBLE AS hrv,
                       (payload->>'lowest_heart_rate')::DOUBLE AS rhr,
                       (payload->>'total_sleep_duration')::DOUBLE / 3600 AS sleep_hours,
                       (payload->>'average_breath')::DOUBLE AS breath,
                       (payload->'readiness'->>'temperature_deviation')::DOUBLE AS sleep_temp
                FROM raw.sleep
                WHERE payload->>'type' = 'long_sleep'
                QUALIFY row_number() OVER (
                    PARTITION BY day ORDER BY (payload->>'total_sleep_duration')::INT DESC) = 1
            )
            SELECT d.day,
                   (r.payload->>'score')::INT AS readiness,
                   coalesce((r.payload->>'temperature_deviation')::DOUBLE, s.sleep_temp) AS temp,
                   (ds.payload->>'score')::INT AS sleep_score,
                   (a.payload->>'score')::INT AS activity,
                   (a.payload->>'steps')::INT AS steps,
                   st.payload->>'day_summary' AS stress,
                   (sp.payload->'spo2_percentage'->>'average')::DOUBLE AS spo2,
                   s.hrv, s.rhr, s.sleep_hours, s.breath
            FROM days d
            LEFT JOIN raw.daily_readiness r ON r.day = d.day
            LEFT JOIN raw.daily_sleep ds ON ds.day = d.day
            LEFT JOIN raw.daily_activity a ON a.day = d.day
            LEFT JOIN raw.daily_stress st ON st.day = d.day
            LEFT JOIN raw.daily_spo2 sp ON sp.day = d.day
            LEFT JOIN main_sleep s ON s.day = d.day
            ORDER BY d.day
            """,
            [since],
        )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def period_starts(self) -> list[dt.date]:
        return [r[0] for r in self.cursor().execute(
            "SELECT day FROM app.period_log ORDER BY day").fetchall()]

    def sync_state(self) -> list[dict]:
        cur = self.cursor()
        # epoch_ms avoids DuckDB's pytz dependency for TIMESTAMPTZ values.
        cur.execute("SELECT endpoint, epoch_ms(last_success_at), last_error FROM meta.sync_state")
        return [{"endpoint": e, "last_success_ms": ms, "error": err} for e, ms, err in cur.fetchall()]


# --------------------------------------------------------------------------- dashboard


def round_or_none(value, digits=1):
    return None if value is None else round(value, digits)


def baselines(rows: list[dict], today: dt.date) -> dict:
    """Her personal normal: median of the last BASELINE_DAYS, excluding today."""
    window = [r for r in rows if today - dt.timedelta(days=BASELINE_DAYS) <= r["day"] < today]
    out = {}
    for key in ("hrv", "rhr", "sleep_hours", "readiness", "sleep_score", "steps", "temp"):
        values = [r[key] for r in window if r[key] is not None]
        out[key] = {
            "median": round_or_none(statistics.median(values), 2) if values else None,
            "nights": len(values),
            "ready": len(values) >= MIN_BASELINE_NIGHTS,
        }
    return out


def strain_signal(latest: dict | None, base: dict) -> dict | None:
    """Flag the classic 'getting sick or run down' pattern: RHR up, HRV down."""
    if not latest or not (base["rhr"]["ready"] and base["hrv"]["ready"]):
        return None
    if latest["rhr"] is None or latest["hrv"] is None:
        return None
    rhr_up = latest["rhr"] - base["rhr"]["median"]
    hrv_ratio = latest["hrv"] / base["hrv"]["median"] if base["hrv"]["median"] else 1
    if rhr_up >= 3 and hrv_ratio <= 0.8:
        return {
            "level": "warning",
            "text": f"Resting heart rate is {rhr_up:.0f} bpm above her normal and HRV is "
                    f"{(1 - hrv_ratio) * 100:.0f}% below. Her body may be fighting something "
                    "or under strain. Take it easy today.",
        }
    return None


def build_dashboard(store: Store) -> dict:
    today = dt.date.today()
    rows = store.daily_rows(today - dt.timedelta(days=TREND_DAYS))
    with_data = [r for r in rows if any(r[k] is not None for k in ("readiness", "sleep_score", "activity", "hrv"))]
    latest = with_data[-1] if with_data else None
    base = baselines(rows, today)

    temps = {r["day"]: r["temp"] for r in rows if r["temp"] is not None}
    periods = store.period_starts()
    cyc = cycle.classify(periods, temps, today, history_days=TREND_DAYS)

    def iso(value):
        return value.isoformat() if isinstance(value, dt.date) else value

    def clean(row):
        return {k: iso(v) if k == "day" else (round(v, 2) if isinstance(v, float) else v)
                for k, v in row.items()}

    prediction = dict(cyc["prediction"])
    prediction["next_period"] = iso(prediction["next_period"])
    if prediction["fertile_window"]:
        prediction["fertile_window"] = [iso(d) for d in prediction["fertile_window"]]

    return {
        "today": today.isoformat(),
        "latest": clean(latest) if latest else None,
        "baselines": base,
        "strain": strain_signal(latest, base),
        "trends": [clean(r) for r in rows],
        "cycle": {
            "today": {**cyc["days"][today], "day": today.isoformat()},
            "days": [{"day": d.isoformat(), **v} for d, v in sorted(cyc["days"].items())],
            "cycles": [{k: iso(v) for k, v in c.items()} for c in cyc["cycles"]],
            "prediction": prediction,
            "notes": cyc["notes"],
            "periods": [d.isoformat() for d in periods],
        },
        "sync": {**store.sync_status, "endpoints": store.sync_state()},
    }


# --------------------------------------------------------------------------- HTTP


class Handler(http.server.BaseHTTPRequestHandler):
    store: Store
    guard: LoginGuard
    server_version = "OuraDashboard"
    sys_version = ""

    def log_message(self, fmt, *args) -> None:
        sys.stderr.write(f"{self.log_date_time_string()} {self.command} "
                         f"{urllib.parse.urlsplit(self.path).path} {args[1] if len(args) > 1 else ''}\n")

    # ---- helpers

    def send_json(self, status: int, body: dict, headers: dict | None = None) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def authed(self) -> bool:
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == "session" and hmac.compare_digest(value, SESSION_TOKEN):
                return True
        return False

    def read_json(self) -> dict:
        length = min(int(self.headers.get("Content-Length") or 0), 10_000)
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return {}

    def parse_day(self, value) -> dt.date | None:
        try:
            day = dt.date.fromisoformat(str(value))
        except ValueError:
            return None
        return day if day <= dt.date.today() else None

    # ---- routes

    def do_GET(self) -> None:  # noqa: N802
        self.guarded(self.route_get)

    def do_POST(self) -> None:  # noqa: N802
        self.guarded(self.route_post)

    def guarded(self, route) -> None:
        try:
            route()
        except Exception:
            traceback.print_exc()
            self.send_json(500, {"error": "Server error. Check the app.py terminal."})

    def route_get(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/dashboard":
            if not self.authed():
                return self.send_json(401, {"error": "login required"})
            return self.send_json(200, build_dashboard(self.store))
        if path == "/api/session":
            return self.send_json(200, {"authed": self.authed()})
        self.serve_static(path)

    def route_post(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/login":
            ok, error = self.guard.check(str(self.read_json().get("pin", "")))
            if not ok:
                return self.send_json(401, {"error": error})
            cookie = (f"session={SESSION_TOKEN}; Max-Age={SESSION_MAX_AGE}; Path=/; "
                      "HttpOnly; SameSite=Strict")
            return self.send_json(200, {"ok": True}, {"Set-Cookie": cookie})
        if not self.authed():
            return self.send_json(401, {"error": "login required"})
        if path == "/api/period":
            day = self.parse_day(self.read_json().get("day"))
            if not day:
                return self.send_json(400, {"error": "Pick a date that isn't in the future."})
            self.store.add_period(day)
            return self.send_json(200, {"ok": True})
        if path == "/api/period/delete":
            day = self.parse_day(self.read_json().get("day"))
            if not day:
                return self.send_json(400, {"error": "bad date"})
            self.store.delete_period(day)
            return self.send_json(200, {"ok": True})
        if path == "/api/sync":
            self.store.sync_in_background()
            return self.send_json(202, {"ok": True})
        self.send_json(404, {"error": "not found"})

    def serve_static(self, path: str) -> None:
        name = "index.html" if path in ("", "/") else path.lstrip("/")
        file = (STATIC_DIR / name).resolve()
        if STATIC_DIR.resolve() not in file.parents or not file.is_file():
            self.send_response(404)
            self.end_headers()
            return
        data = file.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(file.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    parser = argparse.ArgumentParser(description="Oura dashboard server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-sync", action="store_true", help="don't sync in the background")
    args = parser.parse_args()

    try:
        store = Store(oura_sync.DB_PATH)
    except duckdb.IOException:
        sys.exit("oura.duckdb is in use by another process (another app.py or oura_sync.py?).")
    Handler.store = store
    Handler.guard = LoginGuard()
    if not args.no_sync:
        threading.Thread(target=store.sync_forever, daemon=True).start()

    server = http.server.ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Oura dashboard on http://localhost:{args.port}  (PIN is in {PIN_PATH.name})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
