"""Private web dashboard: python -m grgtrading dashboard

- Overview of this bot (equity, P&L, positions, trades).
- Settings: edit strategy/risk knobs (never mode or API keys) -> writes .env, the bot restarts itself.
- Bots: a list of several bots ("workers"), each running its own dashboard.

Security: the password is created on the first visit (or taken from DASHBOARD_PASSWORD)
and stored only as a PBKDF2 hash. Sessions are signed cookies. Other bots are queried
with a per-bot "fleet key" that only unlocks a read-only summary.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import settings
from .config import Config, env_file
from .storage import State, TradeJournal

log = logging.getLogger(__name__)
PAGE = Path(__file__).with_name("dashboard.html")
SESSION_DAYS = 30
MAX_BODY = 64 * 1024
PBKDF2_ROUNDS = 200_000


# ---------------------------------------------------------------- auth store
class AuthStore:
    def __init__(self, path: Path, initial_password: str = ""):
        self.path = path
        self.lock = threading.Lock()
        self.data = json.loads(path.read_text()) if path.exists() else None
        if self.data is None and initial_password:
            self.set_password(initial_password)

    @property
    def configured(self) -> bool:
        return self.data is not None

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    @staticmethod
    def _hash(password: str, salt: str) -> str:
        return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ROUNDS).hex()

    def set_password(self, password: str) -> None:
        with self.lock:
            salt = secrets.token_hex(16)
            old = self.data or {}
            self.data = {
                "salt": salt,
                "hash": self._hash(password, salt),
                # A new secret logs out every existing session.
                "secret": secrets.token_hex(32),
                "fleet_key": old.get("fleet_key") or secrets.token_urlsafe(24),
            }
            self._save()

    def check_password(self, password: str) -> bool:
        if not self.data:
            return False
        return hmac.compare_digest(self._hash(password, self.data["salt"]), self.data["hash"])

    def new_session(self) -> str:
        exp = str(int(time.time()) + SESSION_DAYS * 86400)
        return f"{exp}.{self._sign(exp)}"

    def valid_session(self, token: str | None) -> bool:
        if not self.data or not token or "." not in token:
            return False
        exp, sig = token.split(".", 1)
        return exp.isdigit() and int(exp) > time.time() and hmac.compare_digest(sig, self._sign(exp))

    def _sign(self, value: str) -> str:
        return hmac.new(bytes.fromhex(self.data["secret"]), value.encode(), hashlib.sha256).hexdigest()

    def valid_fleet_key(self, key: str | None) -> bool:
        return bool(self.data and key and hmac.compare_digest(key, self.data["fleet_key"]))


class LoginLimiter:
    """After 5 wrong passwords from one address, refuse logins from it for 5 minutes."""

    def __init__(self):
        self.fails: dict[str, tuple[int, float]] = {}

    def blocked(self, ip: str) -> bool:
        count, until = self.fails.get(ip, (0, 0.0))
        return count >= 5 and time.time() < until

    def failed(self, ip: str) -> None:
        count, _ = self.fails.get(ip, (0, 0.0))
        self.fails[ip] = (count + 1, time.time() + 300)

    def ok(self, ip: str) -> None:
        self.fails.pop(ip, None)


# ---------------------------------------------------------------- data
def collect(cfg: Config) -> dict:
    state = State.load(cfg.data_dir / f"state_{cfg.profile}.json")
    journal = TradeJournal(cfg.data_dir / f"trades_{cfg.profile}.db")
    try:
        history = journal.equity_history()
        trades = journal.trades()
        summary = journal.summary()
    finally:
        journal.close()

    if cfg.mode == "paper":
        start = cfg.starting_balance
    else:
        start = history[0][1] if history else state.last_equity

    heartbeat_age = None
    if state.last_heartbeat:
        last = datetime.fromisoformat(state.last_heartbeat)
        heartbeat_age = (datetime.now(timezone.utc) - last).total_seconds()

    positions = []
    for symbol, pos in state.positions.items():
        price = state.last_prices.get(symbol, pos.entry_price)
        positions.append({
            **pos.to_dict(),
            "price": price,
            "value": pos.amount * price,
            "unrealized_pnl": pos.amount * (price - pos.entry_price),
        })

    return {
        "name": cfg.name,
        "mode": cfg.profile,
        "exchange": cfg.exchange,
        "quote": cfg.quote,
        "symbols": list(cfg.symbols),
        "timeframe": cfg.timeframe,
        "start_equity": start,
        "equity": state.last_equity,
        "peak_equity": state.guard.peak_equity,
        "max_drawdown_pct": cfg.max_drawdown_pct,
        "kill_switch": state.guard.kill_reason if state.guard.killed else None,
        "last_heartbeat": state.last_heartbeat,
        "heartbeat_age_s": heartbeat_age,
        "positions": positions,
        "summary": summary,
        "equity_history": history,
        "trades": trades,
    }


def fleet_summary(cfg: Config) -> dict:
    """The small, read-only summary other dashboards may fetch with the fleet key."""
    d = collect(cfg)
    return {k: d[k] for k in ("name", "mode", "exchange", "quote", "symbols", "timeframe", "start_equity",
                              "equity", "kill_switch", "heartbeat_age_s", "summary")} | {
        "open_positions": len(d["positions"])}


def _workers_path(cfg: Config) -> Path:
    return cfg.data_dir / "dashboard_workers.json"


def load_workers(cfg: Config) -> list[dict]:
    p = _workers_path(cfg)
    return json.loads(p.read_text()) if p.exists() else []


def save_workers(cfg: Config, workers: list[dict]) -> None:
    p = _workers_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(workers, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(p)


def normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    return url


def fetch_worker(url: str, key: str, timeout: float = 4.0) -> dict:
    req = urllib.request.Request(url + "/api/fleet", headers={"X-Fleet-Key": key})
    with urllib.request.urlopen(req, timeout=timeout) as res:  # noqa: S310 (LAN URLs chosen by the owner)
        return json.load(res)


def fleet(cfg: Config) -> list[dict]:
    rows = [{**fleet_summary(cfg), "url": None, "local": True, "online": True}]
    workers = load_workers(cfg)

    def one(w: dict) -> dict:
        try:
            return {**fetch_worker(w["url"], w["key"]), "url": w["url"], "local": False, "online": True}
        except urllib.error.HTTPError as exc:
            err = "wrong fleet key" if exc.code == 403 else f"HTTP {exc.code}"
        except Exception as exc:
            err = "unreachable" if "timed out" in str(exc) or "refused" in str(exc) else str(exc)[:80]
        return {"name": w.get("name") or w["url"], "url": w["url"], "local": False, "online": False, "error": err}

    if workers:
        with ThreadPoolExecutor(max_workers=min(8, len(workers))) as pool:
            rows.extend(pool.map(one, workers))
    return rows


# ---------------------------------------------------------------- HTTP
def make_handler(cfg: Config, auth: AuthStore, limiter: LoginLimiter | None = None,
                 env_path: Path | None = None):
    limiter = limiter or LoginLimiter()
    env_path = env_path or env_file()
    live = {"cfg": cfg, "mtime": _mtime(env_path)}

    def current_cfg() -> Config:
        """Pick up .env edits made over SSH as well as from this dashboard."""
        mtime = _mtime(env_path)
        if mtime != live["mtime"]:
            live["mtime"] = mtime
            try:
                live["cfg"] = Config.from_env(reload=True, env_path=env_path)
            except Exception:
                log.exception("Could not reload .env; keeping the previous settings")
        return live["cfg"]

    class Handler(BaseHTTPRequestHandler):
        # ---- helpers
        def _json(self, status: int, payload, cookie: str | None = None) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if cookie is not None:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(body)

        def _session_cookie(self, token: str, max_age: int) -> str:
            return f"grg_session={token}; Max-Age={max_age}; Path=/; HttpOnly; SameSite=Strict"

        def _logged_in(self) -> bool:
            jar = SimpleCookie(self.headers.get("Cookie", ""))
            token = jar["grg_session"].value if "grg_session" in jar else None
            return auth.valid_session(token)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ValueError("Request too large")
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("Expected a JSON object")
            return data

        def _ip(self) -> str:
            return self.client_address[0]

        # ---- GET
        def do_GET(self):  # noqa: N802
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                body = PAGE.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/api/auth":
                return self._json(200, {"configured": auth.configured, "logged_in": self._logged_in()})
            if path == "/api/fleet":
                if not auth.valid_fleet_key(self.headers.get("X-Fleet-Key")):
                    return self._json(403, {"error": "forbidden"})
                return self._safe(lambda: fleet_summary(current_cfg()))
            if not self._logged_in():
                return self._json(401, {"error": "login required"})
            if path == "/api/data":
                return self._safe(lambda: collect(current_cfg()))
            if path == "/api/settings":
                c = current_cfg()
                return self._json(200, {
                    "fields": settings.describe(c),
                    "locked": {"mode": c.profile, "exchange": c.exchange, "quote": c.quote},
                    "fleet_key": auth.data["fleet_key"],
                })
            if path == "/api/bots":
                return self._safe(lambda: fleet(current_cfg()))
            return self._json(404, {"error": "not found"})

        def _safe(self, fn) -> None:
            try:
                self._json(200, fn())
            except Exception as exc:  # e.g. the bot has not created its files yet
                log.exception("Dashboard error")
                self._json(500, {"error": str(exc)})

        # ---- POST
        def do_POST(self):  # noqa: N802
            # Custom header => browsers block cross-site form posts (CSRF) via CORS preflight.
            if self.headers.get("X-GRG") != "1":
                return self._json(403, {"error": "missing header"})
            path = self.path.split("?")[0]
            try:
                body = self._body()
            except (ValueError, json.JSONDecodeError) as exc:
                return self._json(400, {"error": str(exc)})

            if path == "/api/setup":
                if auth.configured:
                    return self._json(409, {"error": "A password is already set"})
                pw = str(body.get("password", ""))
                if len(pw) < 8:
                    return self._json(400, {"error": "Use at least 8 characters"})
                auth.set_password(pw)
                return self._json(200, {"ok": True},
                                  cookie=self._session_cookie(auth.new_session(), SESSION_DAYS * 86400))

            if path == "/api/login":
                if limiter.blocked(self._ip()):
                    return self._json(429, {"error": "Too many attempts. Try again in 5 minutes."})
                if auth.check_password(str(body.get("password", ""))):
                    limiter.ok(self._ip())
                    return self._json(200, {"ok": True},
                                      cookie=self._session_cookie(auth.new_session(), SESSION_DAYS * 86400))
                limiter.failed(self._ip())
                time.sleep(0.5)
                return self._json(401, {"error": "Wrong password"})

            if path == "/api/logout":
                return self._json(200, {"ok": True}, cookie=self._session_cookie("", 0))

            if not self._logged_in():
                return self._json(401, {"error": "login required"})

            if path == "/api/password":
                if not auth.check_password(str(body.get("current", ""))):
                    return self._json(400, {"error": "Current password is wrong"})
                new = str(body.get("new", ""))
                if len(new) < 8:
                    return self._json(400, {"error": "Use at least 8 characters"})
                auth.set_password(new)
                return self._json(200, {"ok": True},
                                  cookie=self._session_cookie(auth.new_session(), SESSION_DAYS * 86400))

            if path == "/api/settings":
                c = current_cfg()
                try:
                    st = State.load(c.data_dir / f"state_{c.profile}.json")
                    changes = settings.parse_changes(c, body.get("values") or {}, set(st.positions))
                    if changes:
                        settings.write_env(env_path, changes)
                        live["cfg"], live["mtime"] = replace(c, **changes), _mtime(env_path)
                except ValueError as exc:
                    return self._json(400, {"error": str(exc)})
                return self._json(200, {"ok": True, "changed": sorted(changes)})

            if path == "/api/bots/add":
                c = current_cfg()
                url, key = normalize_url(str(body.get("url", ""))), str(body.get("key", "")).strip()
                if not key:
                    return self._json(400, {"error": "Fleet key is required"})
                try:
                    info = fetch_worker(url, key)
                except urllib.error.HTTPError as exc:
                    msg = "Wrong fleet key" if exc.code == 403 else f"HTTP {exc.code}"
                    return self._json(400, {"error": msg})
                except Exception as exc:
                    return self._json(400, {"error": f"Cannot reach {url}: {exc}"})
                workers = [w for w in load_workers(c) if w["url"] != url]
                workers.append({"url": url, "key": key, "name": info.get("name", url)})
                save_workers(c, workers)
                return self._json(200, {"ok": True, "name": info.get("name")})

            if path == "/api/bots/remove":
                c = current_cfg()
                url = normalize_url(str(body.get("url", "")))
                save_workers(c, [w for w in load_workers(c) if w["url"] != url])
                return self._json(200, {"ok": True})

            return self._json(404, {"error": "not found"})

        def log_message(self, *_):  # keep the journal quiet
            pass

    return Handler


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def serve(cfg: Config, host: str, port: int) -> None:
    auth = AuthStore(cfg.data_dir / "dashboard_auth.json", cfg.dashboard_password)
    server = ThreadingHTTPServer((host, port), make_handler(cfg, auth))
    log.info("Dashboard on http://%s:%d (%s)", host, port,
             "password set" if auth.configured else "open it in a browser to create the password")
    server.serve_forever()
