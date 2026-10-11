"""Persistent state (survives restarts) and a trade journal."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .risk import AccountGuard, Position


@dataclass
class State:
    positions: dict[str, Position] = field(default_factory=dict)
    guard: AccountGuard = field(default_factory=AccountGuard)
    paper_balances: dict[str, float] | None = None
    last_equity: float = 0.0
    last_heartbeat: str = ""
    last_prices: dict[str, float] = field(default_factory=dict)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        blob = {
            "positions": {s: p.to_dict() for s, p in self.positions.items()},
            "guard": asdict(self.guard),
            "paper_balances": self.paper_balances,
            "last_equity": self.last_equity,
            "last_heartbeat": self.last_heartbeat,
            "last_prices": self.last_prices,
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(blob, indent=2))
        tmp.replace(path)  # atomic: a crash never leaves a half-written file

    @classmethod
    def load(cls, path: Path) -> "State":
        if not path.exists():
            return cls()
        blob = json.loads(path.read_text())
        return cls(
            positions={s: Position(**p) for s, p in blob.get("positions", {}).items()},
            guard=AccountGuard(**blob.get("guard", {})),
            paper_balances=blob.get("paper_balances"),
            last_equity=blob.get("last_equity", 0.0),
            last_heartbeat=blob.get("last_heartbeat", ""),
            last_prices=blob.get("last_prices", {}),
        )


class TradeJournal:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=10)
        self.conn.execute("""CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, mode TEXT, symbol TEXT, side TEXT, amount REAL, price REAL,
            cost REAL, fee REAL, reason TEXT, pnl REAL)""")
        self.conn.execute("CREATE TABLE IF NOT EXISTS equity (ts TEXT PRIMARY KEY, equity REAL)")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def record(self, **row) -> None:
        cols = ",".join(row)
        self.conn.execute(f"INSERT INTO trades ({cols}) VALUES ({','.join('?' * len(row))})",
                          tuple(row.values()))
        self.conn.commit()

    def record_equity(self, ts: str, equity: float) -> None:
        self.conn.execute("INSERT OR REPLACE INTO equity (ts, equity) VALUES (?, ?)", (ts, equity))
        self.conn.commit()

    def equity_history(self) -> list[tuple[str, float]]:
        return self.conn.execute("SELECT ts, equity FROM equity ORDER BY ts").fetchall()

    def trades(self, n: int = 500) -> list[dict]:
        cur = self.conn.execute(
            "SELECT ts, symbol, side, amount, price, cost, fee, reason, pnl "
            "FROM trades ORDER BY id DESC LIMIT ?", (n,))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def recent(self, n: int = 20) -> list[tuple]:
        return self.conn.execute(
            "SELECT ts, symbol, side, amount, price, reason, pnl FROM trades ORDER BY id DESC LIMIT ?",
            (n,)).fetchall()

    def summary(self) -> dict:
        row = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(pnl),0), COALESCE(SUM(pnl>0),0) "
            "FROM trades WHERE side='sell'").fetchone()
        closed, pnl, wins = row
        return {"closed_trades": closed, "realized_pnl": round(pnl, 2),
                "win_rate": round(wins / closed, 3) if closed else None}
