"""Bot configuration, read from environment variables (or a .env file)."""
from __future__ import annotations

import os
import re
import socket
from dataclasses import dataclass, fields
from pathlib import Path


def env_file() -> Path:
    """The .env file the bot reads and the dashboard edits."""
    explicit = os.getenv("GRG_ENV_FILE")
    if explicit:
        return Path(explicit).resolve()
    local = Path(".env")
    if local.exists():
        return local.resolve()
    try:
        from dotenv import find_dotenv
        found = find_dotenv()
        if found:
            return Path(found).resolve()
    except ImportError:
        pass
    return local.resolve()


@dataclass(frozen=True)
class Config:
    # --- Mode & exchange ---
    mode: str = "paper"                 # "paper" (simulated money) or "live" (real orders)
    exchange: str = "binance"           # any ccxt exchange id
    api_key: str = ""
    api_secret: str = ""
    sandbox: bool = False               # live mode against the exchange's testnet (fake money)
    symbols: tuple[str, ...] = ("BTC/USDT", "ETH/USDT")
    timeframe: str = "1h"
    history_candles: int = 3000         # candles used to train the model

    # --- Paper trading ---
    starting_balance: float = 1000.0
    fee_rate: float = 0.001             # taker fee per side (0.1%)
    slippage: float = 0.0005

    # --- Strategy ---
    buy_threshold: float = 0.60         # model probability needed to open a long
    sell_threshold: float = 0.45        # close the long when probability drops below this
    label_horizon: int = 6              # candles ahead the model tries to predict
    min_model_edge: float = 0.03        # out-of-sample precision must beat the base rate by this
    retrain_hours: float = 12.0

    # --- Risk management ---
    risk_per_trade: float = 0.01        # fraction of equity lost if the stop is hit
    max_position_pct: float = 0.30      # max fraction of equity in one position
    max_open_positions: int = 3
    stop_loss_atr: float = 2.0
    take_profit_atr: float = 4.0
    trailing_stop: bool = True
    small_account_mode: bool = False    # buy the exchange minimum when risk sizing is below it
    max_drawdown_pct: float = 0.20      # kill switch: close everything and stop trading
    daily_loss_limit_pct: float = 0.05  # no new entries for the rest of the UTC day

    # --- Runtime ---
    poll_seconds: int = 60
    data_dir: Path = Path("data")
    telegram_token: str = ""
    telegram_chat_id: str = ""
    live_confirm: str = ""              # must be "I_UNDERSTAND_THE_RISKS" for live mode
    dashboard_port: int = 8080
    dashboard_password: str = ""        # optional initial password; otherwise set on first visit
    bot_name: str = ""                  # shown on the dashboard; defaults to the hostname

    @classmethod
    def from_env(cls, reload: bool = False, env_path: Path | None = None, **overrides) -> "Config":
        """reload=True re-reads .env over values already in the environment
        (used by the long-running dashboard after settings are edited)."""
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path or env_file(), override=reload)
        except ImportError:
            pass
        values = {}
        for f in fields(cls):
            raw = os.getenv(f.name.upper())
            if raw is None or raw == "":
                continue
            default = f.default
            if isinstance(default, bool):
                values[f.name] = raw.strip().lower() in ("1", "true", "yes", "on")
            elif isinstance(default, int):
                values[f.name] = int(raw)
            elif isinstance(default, float):
                values[f.name] = float(raw)
            elif isinstance(default, tuple):
                values[f.name] = tuple(s.strip() for s in raw.split(",") if s.strip())
            elif isinstance(default, Path):
                values[f.name] = Path(raw)
            else:
                values[f.name] = raw.strip()
        values.update(overrides)
        cfg = cls(**values)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.mode not in ("paper", "live"):
            raise ValueError("MODE must be 'paper' or 'live'")
        if self.mode == "live":
            if not (self.api_key and self.api_secret):
                raise ValueError("Live mode needs API_KEY and API_SECRET")
            if not self.sandbox and self.live_confirm != "I_UNDERSTAND_THE_RISKS":
                raise ValueError("Live mode needs LIVE_CONFIRM=I_UNDERSTAND_THE_RISKS")
        if not self.symbols:
            raise ValueError("SYMBOLS must list at least one market, e.g. BTC/USDT")
        quotes = {s.split("/")[1] for s in self.symbols}
        if len(quotes) != 1:
            raise ValueError("All SYMBOLS must share the same quote currency")
        if not 0 < self.risk_per_trade <= 0.05:
            raise ValueError("RISK_PER_TRADE must be between 0 and 0.05")
        if not 0 < self.max_position_pct <= 1:
            raise ValueError("MAX_POSITION_PCT must be between 0 and 1")
        if not re.fullmatch(r"[A-Za-z0-9 _.-]{0,32}", self.bot_name):
            raise ValueError("BOT_NAME may only contain letters, digits, spaces, '.', '_' and '-'")
        if self.sell_threshold >= self.buy_threshold:
            raise ValueError("SELL_THRESHOLD must be below BUY_THRESHOLD")

    @property
    def profile(self) -> str:
        """Name used for state/journal files so paper, sandbox and live never mix."""
        return "sandbox" if self.mode == "live" and self.sandbox else self.mode

    @property
    def name(self) -> str:
        return self.bot_name or socket.gethostname()

    @property
    def quote(self) -> str:
        return self.symbols[0].split("/")[1]
