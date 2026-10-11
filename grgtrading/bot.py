"""The 24/7 trading loop."""
from __future__ import annotations

import logging
import math
import signal
import time
from datetime import datetime, timezone

import pandas as pd

from .broker import Broker, candles_to_frame, make_broker
from .config import Config, env_file
from .features import atr
from .model import SignalModel
from .notify import Notifier
from .risk import (MIN_ORDER_BUFFER, Position, exit_reason, initial_levels, order_size,
                   size_quote, update_trailing)
from .storage import State, TradeJournal

log = logging.getLogger(__name__)


class TradingBot:
    def __init__(self, cfg: Config, broker: Broker | None = None):
        self.cfg = cfg
        self.state_path = cfg.data_dir / f"state_{cfg.profile}.json"
        self.state = State.load(self.state_path)
        self.broker = broker or make_broker(cfg, self.state.paper_balances)
        self.journal = TradeJournal(cfg.data_dir / f"trades_{cfg.profile}.db")
        self.notifier = Notifier(cfg)
        self.models = {s: SignalModel.load(cfg, self._model_path(s)) for s in cfg.symbols}
        self.candles: dict[str, pd.DataFrame] = {}
        self._last_snapshot = float("-inf")
        self._running = True

    def _model_path(self, symbol: str):
        return self.cfg.data_dir / "models" / f"{symbol.replace('/', '_')}_{self.cfg.timeframe}.joblib"

    # ---------- data ----------
    def _closed_candles(self, symbol: str) -> pd.DataFrame:
        """Full history on first call, then only the newest candles (cheap on the API)."""
        cached = self.candles.get(symbol)
        if cached is None:
            df = self.broker.fetch_ohlcv(symbol, self.cfg.history_candles + 1)
        else:
            recent = self.broker.exchange.fetch_ohlcv(symbol, self.cfg.timeframe, limit=10)
            df = pd.concat([cached, candles_to_frame(recent)])
            df = df[~df.index.duplicated(keep="last")].sort_index()
        df = df.tail(self.cfg.history_candles + 1)
        self.candles[symbol] = df
        return df.iloc[:-1]  # the last candle is still forming

    def _ensure_model(self, symbol: str, df: pd.DataFrame) -> SignalModel:
        model = self.models[symbol]
        if model.is_stale() or model.clf is None:
            metrics = model.train(df)
            model.save(self._model_path(symbol))
            log.info("Retrained %s: %s", symbol, metrics)
            if not model.tradeable:
                log.warning("%s model shows no reliable edge on unseen data; no new entries "
                            "until a retrain finds one", symbol)
        return model

    # ---------- trading ----------
    def equity(self, prices: dict[str, float]) -> float:
        total = self.broker.cash()
        for symbol, pos in self.state.positions.items():
            total += pos.amount * prices.get(symbol, pos.entry_price)
        return total

    def _open(self, symbol: str, price: float, atr_value: float, prob: float, equity: float) -> None:
        cash = self.broker.cash()
        exchange_min = self.broker.min_order_cost(symbol, price)
        quote = order_size(self.cfg, equity, cash, price, atr_value, exchange_min)
        if quote <= 0:
            log.info("%s: buy signal skipped, position would be below the exchange minimum "
                     "(%.2f %s incl. safety margin, cash %.2f). Add funds or set SMALL_ACCOUNT_MODE=true.",
                     symbol, exchange_min * MIN_ORDER_BUFFER, self.cfg.quote, cash)
            return
        if quote > size_quote(self.cfg, equity, cash, price, atr_value):
            log.warning("%s: small-account mode, buying the exchange minimum; this risks more than "
                        "RISK_PER_TRADE of equity", symbol)
        fill = self.broker.buy(symbol, quote)
        stop, tp = initial_levels(self.cfg, fill.price, atr_value)
        self.state.positions[symbol] = Position(symbol, fill.amount, fill.price, stop, tp,
                                                datetime.now(timezone.utc).isoformat())
        self.journal.record(ts=_now(), mode=self.cfg.profile, symbol=symbol, side="buy",
                            amount=fill.amount, price=fill.price, cost=fill.cost, fee=fill.fee,
                            reason=f"p={prob:.3f}", pnl=None)
        self.notifier.send(f"BUY {symbol} {fill.amount:.6f} @ {fill.price:.2f} "
                           f"(p={prob:.2f}, stop {stop:.2f}, tp {tp:.2f})")
        self._persist(self.state.last_equity)

    def _close(self, symbol: str, reason: str) -> None:
        pos = self.state.positions[symbol]
        fill = self.broker.sell(symbol, pos.amount)  # if this raises, the position stays tracked
        del self.state.positions[symbol]
        pnl = fill.cost - pos.amount * pos.entry_price * (1 + self.cfg.fee_rate)
        self.journal.record(ts=_now(), mode=self.cfg.profile, symbol=symbol, side="sell",
                            amount=fill.amount, price=fill.price, cost=fill.cost, fee=fill.fee,
                            reason=reason, pnl=pnl)
        self.notifier.send(f"SELL {symbol} @ {fill.price:.2f} ({reason}) PnL {pnl:+.2f} {self.cfg.quote}")
        self._persist(self.state.last_equity)

    def step(self) -> None:
        prices: dict[str, float] = {}
        signals: dict[str, tuple[float, float, float]] = {}

        # 1) Manage open positions first (stops matter more than new entries).
        for symbol in self.cfg.symbols:
            df = self._closed_candles(symbol)
            model = self._ensure_model(symbol, df)
            price = self.broker.last_price(symbol)
            atr_value = float(atr(df).iloc[-1])
            prob = model.predict_latest(df)
            prices[symbol] = price
            signals[symbol] = (price, atr_value, prob if model.tradeable else float("nan"))

            pos = self.state.positions.get(symbol)
            if pos:
                reason = exit_reason(self.cfg, pos, price, price, prob)
                if reason:
                    self._close(symbol, reason)
                else:
                    update_trailing(self.cfg, pos, price, atr_value)

        # 2) Account-level guard.
        self.state.last_prices = prices
        equity = self.equity(prices)
        status = self.state.guard.check(self.cfg, equity, datetime.now(timezone.utc))
        if status == "kill":
            if self.state.positions:
                self.notifier.send(f"KILL SWITCH: {self.state.guard.kill_reason}. Closing all positions.")
                for symbol in list(self.state.positions):
                    self._close(symbol, "kill_switch")
            self._persist(equity)
            return

        # 3) New entries.
        if status == "ok":
            candidates = sorted(
                ((prob, symbol, price, atr_value) for symbol, (price, atr_value, prob) in signals.items()
                 if not math.isnan(prob) and prob >= self.cfg.buy_threshold
                 and symbol not in self.state.positions),
                reverse=True)
            for prob, symbol, price, atr_value in candidates:
                if len(self.state.positions) >= self.cfg.max_open_positions:
                    break
                self._open(symbol, price, atr_value, prob, equity)
        self._persist(self.equity(prices))
        self._snapshot_equity()

    def _snapshot_equity(self) -> None:
        """Equity history for the dashboard chart, at most every 5 minutes."""
        if time.monotonic() - self._last_snapshot >= 300:
            self.journal.record_equity(self.state.last_heartbeat, self.state.last_equity)
            self._last_snapshot = time.monotonic()

    def _persist(self, equity: float) -> None:
        self.state.last_equity = equity
        self.state.last_heartbeat = _now()
        if hasattr(self.broker, "balances"):
            self.state.paper_balances = self.broker.balances
        self.state.save(self.state_path)

    # ---------- 24/7 loop ----------
    def stop(self, *_):
        log.info("Shutdown requested, finishing current step...")
        self._running = False

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        self.notifier.send(f"Started in {self.cfg.profile.upper()} mode on {self.cfg.exchange} "
                           f"{', '.join(self.cfg.symbols)} [{self.cfg.timeframe}]")
        failures = 0
        settings_file = env_file()
        settings_mtime = _mtime(settings_file)
        while self._running:
            started = time.monotonic()
            try:
                self.step()
                failures = 0
                log.info("Heartbeat: equity %.2f %s, open positions: %s", self.state.last_equity,
                         self.cfg.quote, list(self.state.positions) or "none")
                if self.state.guard.killed:
                    self.notifier.send("Bot halted by kill switch. Fix the cause, then reset "
                                       "with `python -m grgtrading reset-kill`.")
                    break
            except Exception as exc:  # network blips, exchange downtime, etc.
                failures += 1
                log.exception("Step failed (%d in a row): %s", failures, exc)
                if failures in (3, 10, 50):
                    self.notifier.send(f"Warning: {failures} failed steps in a row: {exc}")
                self.candles.clear()  # resync from scratch after errors
            delay = self.cfg.poll_seconds if failures == 0 else min(600, 15 * 2 ** min(failures, 5))
            while self._running and time.monotonic() - started < delay:
                if _mtime(settings_file) != settings_mtime:
                    # Edited from the dashboard: exit so the service manager restarts the bot
                    # with the new settings. State and open positions are on disk.
                    self.notifier.send("Settings changed; restarting to apply them.")
                    self._running = False
                    break
                time.sleep(1)
        self.notifier.send("Stopped.")


def _mtime(path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
