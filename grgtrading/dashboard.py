"""Private, read-only web dashboard: python -m grgtrading dashboard

Serves a single page plus /api/data on the local network. It only reads the
bot's state file and trade journal; it never places orders or exposes keys.
"""
from __future__ import annotations

import base64
import hmac
import json
import logging
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import Config
from .storage import State, TradeJournal

log = logging.getLogger(__name__)
PAGE = Path(__file__).with_name("dashboard.html")


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


def make_handler(cfg: Config):
    password = cfg.dashboard_password

    class Handler(BaseHTTPRequestHandler):
        def _authorized(self) -> bool:
            if not password:
                return True
            header = self.headers.get("Authorization", "")
            if not header.startswith("Basic "):
                return False
            try:
                _, _, given = base64.b64decode(header[6:]).decode().partition(":")
            except Exception:
                return False
            return hmac.compare_digest(given, password)

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 (http.server naming)
            if not self._authorized():
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="GrgTrading"')
                self.end_headers()
                return
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/data":
                try:
                    body = json.dumps(collect(cfg)).encode()
                    self._send(200, body, "application/json")
                except Exception as exc:  # e.g. bot never ran yet
                    log.exception("Dashboard data error")
                    self._send(500, json.dumps({"error": str(exc)}).encode(), "application/json")
            else:
                self._send(404, b"Not found", "text/plain")

        def log_message(self, *_):  # keep the journal quiet
            pass

    return Handler


def serve(cfg: Config, host: str, port: int) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(cfg))
    log.info("Dashboard on http://%s:%d (password %s)", host, port,
             "enabled" if cfg.dashboard_password else "off")
    server.serve_forever()
