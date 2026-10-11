from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from grgtrading.backtest import run_backtest
from grgtrading.bot import TradingBot
from grgtrading.broker import PaperBroker
from grgtrading.config import Config
from grgtrading.features import FEATURE_COLUMNS, add_features, make_labels
from grgtrading.model import SignalModel
from grgtrading.risk import AccountGuard, Position, exit_reason, order_size, size_quote


def synthetic_ohlcv(n=3000, seed=0, momentum=0.0):
    rng = np.random.default_rng(seed)
    rets = np.zeros(n)
    noise = rng.normal(0, 0.01, n)
    for i in range(1, n):  # optional autocorrelation gives the model something to learn
        rets[i] = momentum * rets[i - 1] + noise[i]
    close = 100 * np.exp(np.cumsum(rets))
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.004, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.004, n))
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                         "volume": rng.uniform(100, 200, n)}, index=idx)


class FakeExchange:
    def __init__(self, df):
        self.df = df

    def parse_timeframe(self, tf):
        return 3600

    def milliseconds(self):
        return int(self.df.index[-1].timestamp() * 1000) + 1

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=1000):
        df = self.df
        if since is not None:
            df = df[df.index >= pd.Timestamp(since, unit="ms", tz="UTC")]
        df = df.head(limit) if since is not None else df.tail(limit)
        return [[int(ts.timestamp() * 1000), *row] for ts, row in zip(df.index, df.to_numpy().tolist())]

    def fetch_ticker(self, symbol):
        return {"last": float(self.df["close"].iloc[-1])}

    def market(self, symbol):
        return {"limits": {"cost": {"min": 5}, "amount": {"min": 0.001}}}


def cfg(tmp_path, **kw):
    return Config(data_dir=tmp_path, symbols=("BTC/USDT",), **kw)


def test_features_have_no_lookahead():
    df = synthetic_ohlcv(600)
    full = add_features(df)
    truncated = add_features(df.iloc[:400])
    pd.testing.assert_frame_equal(full.iloc[:400], truncated)
    assert list(full.columns) == FEATURE_COLUMNS


def test_labels_are_nan_at_the_end():
    labels = make_labels(synthetic_ohlcv(300), horizon=6, fee_rate=0.001)
    assert labels.iloc[-6:].isna().all() and labels.iloc[:-6].notna().all()


def test_position_size_respects_limits(tmp_path):
    c = cfg(tmp_path, risk_per_trade=0.01, max_position_pct=0.3, stop_loss_atr=2.0)
    # stop distance 2*1=2 on price 100 -> risk-based size 1000*0.01/2*100 = 500, capped at 300
    assert size_quote(c, 1000, 1000, 100, 1.0) == pytest.approx(300)
    assert size_quote(c, 1000, 100, 100, 1.0) == pytest.approx(99)
    assert size_quote(c, 1000, 1000, 100, 0.0) == 0


def test_order_size_respects_exchange_minimum(tmp_path):
    # 20 USDT account: risk sizing gives ~6 USDT, below the 5 * 1.25 = 6.25 floor
    normal = cfg(tmp_path)
    small = cfg(tmp_path, small_account_mode=True)
    assert order_size(normal, 20, 20, 100, 1.0, exchange_min=5) == 0
    assert order_size(small, 20, 20, 100, 1.0, exchange_min=5) == pytest.approx(6.25)
    # balance below the exchange minimum: impossible even in small-account mode
    assert order_size(small, 2.2, 2.2, 100, 1.0, exchange_min=5) == 0
    # big account: risk sizing wins
    assert order_size(normal, 1000, 1000, 100, 1.0, exchange_min=5) == pytest.approx(300)


def test_min_order_cost_uses_amount_and_cost_limits(tmp_path):
    broker = PaperBroker(cfg(tmp_path), FakeExchange(synthetic_ohlcv(300)))
    assert broker.min_order_cost("BTC/USDT", 100) == 5        # cost limit dominates
    assert broker.min_order_cost("BTC/USDT", 10_000) == 10    # 0.001 * 10000


def test_exit_rules(tmp_path):
    c = cfg(tmp_path)
    pos = Position("BTC/USDT", 1, 100, stop=95, take_profit=110, opened_at="")
    assert exit_reason(c, pos, 94, 101, 0.7) == "stop_loss"
    assert exit_reason(c, pos, 99, 111, 0.7) == "take_profit"
    assert exit_reason(c, pos, 99, 101, 0.3) == "model_exit"
    assert exit_reason(c, pos, 99, 101, 0.55) is None


def test_kill_switch_and_daily_pause(tmp_path):
    c = cfg(tmp_path, max_drawdown_pct=0.2, daily_loss_limit_pct=0.05)
    g = AccountGuard()
    day = datetime(2024, 1, 1, tzinfo=timezone.utc)
    assert g.check(c, 1000, day) == "ok"
    assert g.check(c, 940, day) == "daily_pause"
    assert g.check(c, 940, datetime(2024, 1, 2, tzinfo=timezone.utc)) == "ok"
    assert g.check(c, 790, datetime(2024, 1, 2, tzinfo=timezone.utc)) == "kill"
    assert g.check(c, 2000, datetime(2024, 1, 3, tzinfo=timezone.utc)) == "kill"  # stays killed


def test_paper_broker_charges_fees(tmp_path):
    c = cfg(tmp_path, fee_rate=0.001, slippage=0.0)
    df = synthetic_ohlcv(300)
    broker = PaperBroker(c, FakeExchange(df))
    buy = broker.buy("BTC/USDT", 500)
    sell = broker.sell("BTC/USDT", buy.amount)
    assert broker.cash() == pytest.approx(1000 - 500 * 0.001 - (500 - 0.5) * 0.001)
    assert broker.base_amount("BTC/USDT") == pytest.approx(0)
    assert sell.fee > 0


def test_model_refuses_to_trade_random_walk(tmp_path):
    model = SignalModel(cfg(tmp_path))
    metrics = model.train(synthetic_ohlcv(3000, seed=1))
    assert metrics["tradeable"] is False


def test_model_finds_real_pattern(tmp_path):
    model = SignalModel(cfg(tmp_path, buy_threshold=0.55, sell_threshold=0.45))
    metrics = model.train(synthetic_ohlcv(4000, seed=1, momentum=0.4))
    assert metrics["tradeable"] is True


def test_backtest_runs(tmp_path):
    result = run_backtest(synthetic_ohlcv(2600, seed=3, momentum=0.4),
                          cfg(tmp_path, buy_threshold=0.55), train_candles=2000, retrain_every=300)
    assert result["end_equity"] > 0
    assert len(result["equity_curve"]) == 600


def test_bot_step_trades_and_persists(tmp_path):
    c = cfg(tmp_path, buy_threshold=0.55, history_candles=3000, min_model_edge=-1.0)
    df = synthetic_ohlcv(3001, seed=4, momentum=0.4)
    broker = PaperBroker(c, FakeExchange(df))
    bot = TradingBot(c, broker=broker)
    bot.step()
    assert (tmp_path / "state_paper.json").exists()
    assert bot.models["BTC/USDT"].clf is not None
    eq = bot.state.last_equity
    assert eq == pytest.approx(broker.cash() + sum(
        p.amount * df["close"].iloc[-1] for p in bot.state.positions.values()))
    # restart restores paper balances and positions
    bot2 = TradingBot(c, broker=PaperBroker(c, FakeExchange(df), bot.state.paper_balances))
    assert bot2.state.positions.keys() == bot.state.positions.keys()


def test_live_mode_requires_confirmation():
    with pytest.raises(ValueError):
        Config(mode="live", api_key="k", api_secret="s").validate()
    Config(mode="live", sandbox=True, api_key="k", api_secret="s").validate()  # testnet: no real money
    assert Config(mode="live", sandbox=True).profile == "sandbox"
    Config(mode="live", api_key="k", api_secret="s", live_confirm="I_UNDERSTAND_THE_RISKS").validate()


def test_dashboard_serves_data(tmp_path):
    import json
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer
    from grgtrading.dashboard import make_handler
    from grgtrading.storage import TradeJournal

    c = cfg(tmp_path, buy_threshold=0.55, min_model_edge=-1.0, dashboard_password="secret")
    df = synthetic_ohlcv(3001, seed=4, momentum=0.4)
    TradingBot(c, broker=PaperBroker(c, FakeExchange(df))).step()
    j = TradeJournal(tmp_path / "trades_paper.db")
    j.record(ts="2024-01-01T00:00:00+00:00", mode="paper", symbol="BTC/USDT", side="sell",
             amount=1, price=100, cost=100, fee=0.1, reason="take_profit", pnl=1.5)
    j.close()

    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(c))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(url + "/api/data")
        assert err.value.code == 401
        req = urllib.request.Request(url + "/api/data", headers={"Authorization": "Basic dTpzZWNyZXQ="})
        data = json.load(urllib.request.urlopen(req))
        assert data["start_equity"] == c.starting_balance
        assert data["summary"]["closed_trades"] == 1
        assert len(data["equity_history"]) == 1
        page = urllib.request.urlopen(urllib.request.Request(url + "/", headers={"Authorization": "Basic dTpzZWNyZXQ="}))
        assert b"GrgTrading" in page.read()
    finally:
        server.shutdown()
