"""Settings that may be changed from the dashboard, with validation and .env writing.

Only strategy and risk knobs are editable. Mode, exchange, API keys and the
live-trading confirmation are deliberately left out: those stay SSH-only, so a
leaked dashboard password can never switch the bot to real money.
"""
from __future__ import annotations

import os
import re
from dataclasses import replace
from pathlib import Path

from .config import Config

SYMBOL_RE = re.compile(r"[A-Z0-9]{2,10}/[A-Z0-9]{2,10}")

# key -> spec. Bounds are deliberately tighter than what the bot would accept.
EDITABLE: dict[str, dict] = {
    "bot_name": {"type": "text", "label": "Bot name", "help": "Shown on the dashboard and in the bots list."},
    "symbols": {"type": "symbols", "label": "Pairs", "help": "Comma separated, same quote currency, max 5. Example: BTC/USDT,SOL/USDT"},
    "timeframe": {"type": "select", "label": "Candle interval", "options": ["15m", "1h", "4h", "1d"]},
    "buy_threshold": {"type": "float", "label": "Buy threshold", "min": 0.5, "max": 0.95, "step": 0.01,
                      "help": "Model probability needed to buy."},
    "sell_threshold": {"type": "float", "label": "Sell threshold", "min": 0.05, "max": 0.6, "step": 0.01,
                       "help": "Sell when the probability drops below this. Must be below the buy threshold."},
    "retrain_hours": {"type": "float", "label": "Retrain every (hours)", "min": 1, "max": 168, "step": 1},
    "risk_per_trade": {"type": "float", "label": "Risk per trade", "min": 0.001, "max": 0.05, "step": 0.001,
                       "help": "Share of equity lost if a stop is hit (0.01 = 1%)."},
    "max_position_pct": {"type": "float", "label": "Max position size", "min": 0.05, "max": 1.0, "step": 0.05,
                         "help": "Max share of equity in one position."},
    "max_open_positions": {"type": "int", "label": "Max open positions", "min": 1, "max": 10, "step": 1},
    "stop_loss_atr": {"type": "float", "label": "Stop-loss (× ATR)", "min": 0.5, "max": 10, "step": 0.1},
    "take_profit_atr": {"type": "float", "label": "Take-profit (× ATR)", "min": 0.5, "max": 20, "step": 0.1},
    "trailing_stop": {"type": "bool", "label": "Trailing stop"},
    "max_drawdown_pct": {"type": "float", "label": "Kill switch drawdown", "min": 0.02, "max": 0.5, "step": 0.01,
                         "help": "Close everything and stop below this drop from peak."},
    "daily_loss_limit_pct": {"type": "float", "label": "Daily loss limit", "min": 0.01, "max": 0.2, "step": 0.01},
    "small_account_mode": {"type": "bool", "label": "Small-account mode",
                           "help": "Buy the exchange minimum when risk sizing is below it."},
}


def _as_env(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, tuple):
        return ",".join(value)
    return str(value)


def describe(cfg: Config) -> list[dict]:
    """Schema plus current values, for the settings form."""
    out = []
    for key, spec in EDITABLE.items():
        value = getattr(cfg, key)
        if isinstance(value, tuple):
            value = ",".join(value)
        out.append({"key": key, "value": value, **spec})
    return out


def parse_changes(cfg: Config, raw: dict, open_symbols: set[str] = frozenset()) -> dict:
    """Validate submitted values against EDITABLE; raise ValueError with a readable message.

    open_symbols: pairs with an open position; they cannot be removed, otherwise the
    bot would stop managing that position's stop-loss."""
    changes = {}
    for key, value in raw.items():
        spec = EDITABLE.get(key)
        if spec is None:
            raise ValueError(f"'{key}' cannot be changed from the dashboard")
        label, kind = spec["label"], spec["type"]
        try:
            if kind == "bool":
                if not isinstance(value, bool):
                    raise ValueError
                parsed = value
            elif kind == "int":
                parsed = int(value)
                if float(value) != parsed:
                    raise ValueError
            elif kind == "float":
                parsed = float(value)
            elif kind == "select":
                parsed = str(value)
                if parsed not in spec["options"]:
                    raise ValueError
            elif kind == "symbols":
                items = [s.strip().upper() for s in str(value).split(",") if s.strip()]
                if not 1 <= len(items) <= 5 or not all(SYMBOL_RE.fullmatch(s) for s in items):
                    raise ValueError
                parsed = tuple(dict.fromkeys(items))
            else:
                parsed = str(value).strip()
        except (TypeError, ValueError):
            raise ValueError(f"{label}: invalid value") from None
        if kind in ("int", "float") and not spec["min"] <= parsed <= spec["max"]:
            raise ValueError(f"{label} must be between {spec['min']} and {spec['max']}")
        changes[key] = parsed

    if "symbols" in changes:
        quotes = {s.split("/")[1] for s in changes["symbols"]}
        if quotes != {cfg.quote}:
            raise ValueError(f"Pairs must keep the quote currency {cfg.quote} (change it over SSH)")
        dropped = sorted(open_symbols - set(changes["symbols"]))
        if dropped:
            raise ValueError(f"Close the open position first: {', '.join(dropped)}")

    candidate = replace(cfg, **changes)
    candidate.validate()  # cross-field rules, e.g. sell < buy, same quote currency
    return changes


def write_env(path: Path, changes: dict) -> None:
    """Update KEY=value lines in .env in place, keeping comments and other settings."""
    lines = path.read_text().splitlines() if path.exists() else []
    pending = {k.upper(): _as_env(v) for k, v in changes.items()}
    for value in pending.values():
        if "\n" in value or "\r" in value:
            raise ValueError("Values cannot contain line breaks")
    out = []
    for line in lines:
        m = re.match(r"\s*([A-Z_][A-Z0-9_]*)\s*=", line)
        if m and m.group(1) in pending:
            out.append(f"{m.group(1)}={pending.pop(m.group(1))}")
        else:
            out.append(line)
    if pending:
        out.append("")
        out.append("# ---- Changed from the dashboard ----")
        out.extend(f"{k}={v}" for k, v in pending.items())
    if path.exists():
        path.with_name(path.name + ".bak").write_text(path.read_text())
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(out) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
