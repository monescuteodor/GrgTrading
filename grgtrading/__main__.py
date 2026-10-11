"""Command line: python -m grgtrading {run,backtest,status,check,dashboard,reset-kill}"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from logging.handlers import RotatingFileHandler

from .config import Config


def setup_logging(cfg: Config) -> None:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in (logging.StreamHandler(sys.stdout),
                    RotatingFileHandler(cfg.data_dir / "bot.log", maxBytes=5_000_000, backupCount=5)):
        handler.setFormatter(fmt)
        root.addHandler(handler)


def cmd_run(cfg: Config, _args) -> None:
    from .bot import TradingBot
    if cfg.profile == "live":
        logging.warning("LIVE MODE: real orders with real money on %s", cfg.exchange)
    TradingBot(cfg).run_forever()


def cmd_backtest(cfg: Config, args) -> None:
    from .backtest import run_backtest
    from .broker import PaperBroker, make_exchange
    broker = PaperBroker(cfg, make_exchange(cfg))
    for symbol in args.symbols or cfg.symbols:
        df = broker.fetch_ohlcv(symbol, args.candles)
        exchange_min = broker.min_order_cost(symbol, float(df["close"].iloc[-1]))
        result = run_backtest(df, cfg, train_candles=args.train, exchange_min=exchange_min)
        curve = result.pop("equity_curve")
        print(f"\n=== {symbol} [{cfg.timeframe}] ===")
        for k, v in result.items():
            print(f"  {k:24s} {v}")
        out = cfg.data_dir / f"backtest_{symbol.replace('/', '_')}_{cfg.timeframe}.csv"
        curve.to_csv(out, header=["equity"])
        print(f"  equity curve saved to {out}")


def cmd_status(cfg: Config, _args) -> None:
    from .storage import State, TradeJournal
    state = State.load(cfg.data_dir / f"state_{cfg.profile}.json")
    journal = TradeJournal(cfg.data_dir / f"trades_{cfg.profile}.db")
    print(json.dumps({
        "mode": cfg.profile,
        "last_heartbeat": state.last_heartbeat,
        "equity": round(state.last_equity, 2),
        "peak_equity": round(state.guard.peak_equity, 2),
        "kill_switch": state.guard.kill_reason if state.guard.killed else None,
        "open_positions": {s: p.to_dict() for s, p in state.positions.items()},
        **journal.summary(),
    }, indent=2))
    print("\nRecent trades:")
    for row in journal.recent(10):
        print("  ", row)


def cmd_check(cfg: Config, _args) -> None:
    """Verify connectivity, API keys and minimum order sizes without placing any order."""
    import ccxt
    from .broker import make_broker
    from .risk import MIN_ORDER_BUFFER
    print(f"Mode: {cfg.profile}   Exchange: {cfg.exchange}")
    try:
        broker = make_broker(cfg)
        cash = broker.cash()
    except ccxt.AuthenticationError as exc:
        sys.exit(f"API key rejected by {cfg.exchange}: {exc}")
    except ccxt.NetworkError as exc:
        sys.exit(f"Cannot reach {cfg.exchange}: {exc}")
    simulated = "  (simulated STARTING_BALANCE)" if cfg.mode == "paper" else ""
    print(f"Available {cfg.quote}: {cash:.4f}{simulated}")
    ok = True
    for symbol in cfg.symbols:
        price = broker.last_price(symbol)
        needed = broker.min_order_cost(symbol, price) * MIN_ORDER_BUFFER
        can_trade = cash * 0.99 >= needed
        ok &= can_trade
        print(f"  {symbol:12s} price {price:<14.6g} smallest tradable position ~{needed:.2f} {cfg.quote}"
              f"  {'OK' if can_trade else 'BALANCE TOO SMALL'}")
    if ok and not cfg.small_account_mode:
        print("Note: with a small balance, risk-based sizing may still fall below these minimums; "
              "set SMALL_ACCOUNT_MODE=true to trade the minimum instead.")
    print("No orders were placed.")


def cmd_dashboard(cfg: Config, args) -> None:
    from .dashboard import serve
    serve(cfg, args.host, args.port or cfg.dashboard_port)


def cmd_reset_kill(cfg: Config, _args) -> None:
    from .risk import AccountGuard
    from .storage import State
    path = cfg.data_dir / f"state_{cfg.profile}.json"
    state = State.load(path)
    state.guard = AccountGuard()
    state.save(path)
    print("Kill switch reset; peak equity will be re-measured from now.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="grgtrading", description="GrgTrading AI trading bot")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="run the bot 24/7")
    bt = sub.add_parser("backtest", help="walk-forward backtest on historical data")
    bt.add_argument("--symbols", nargs="*")
    bt.add_argument("--candles", type=int, default=8000)
    bt.add_argument("--train", type=int, default=2000)
    sub.add_parser("status", help="show equity, positions and recent trades")
    sub.add_parser("check", help="check API keys, balance and exchange minimums (no orders)")
    dash = sub.add_parser("dashboard", help="private web dashboard on the local network")
    dash.add_argument("--host", default="0.0.0.0")
    dash.add_argument("--port", type=int, default=0)
    sub.add_parser("reset-kill", help="clear the drawdown kill switch")
    args = parser.parse_args()

    overrides = {"mode": "paper", "sandbox": False} if args.command == "backtest" else {}
    cfg = Config.from_env(**overrides)
    if args.command in ("run", "backtest", "dashboard"):
        setup_logging(cfg)
    {"run": cmd_run, "backtest": cmd_backtest, "status": cmd_status, "check": cmd_check, "dashboard": cmd_dashboard,
     "reset-kill": cmd_reset_kill}[args.command](cfg, args)


if __name__ == "__main__":
    main()
