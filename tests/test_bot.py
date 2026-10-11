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



# ---------------------------------------------------------------- dashboard
import json as _json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from grgtrading.dashboard import AuthStore, make_handler
from grgtrading.settings import parse_changes, write_env
from grgtrading.storage import TradeJournal


class Client:
    def __init__(self, url):
        self.url, self.cookie = url, ""

    def call(self, path, body=None, headers=None):
        h = {"Cookie": self.cookie, **(headers or {})}
        data = None
        if body is not None:
            data = _json.dumps(body).encode()
            h.setdefault("X-GRG", "1")
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(self.url + path, data=data, headers=h)
        try:
            res = urllib.request.urlopen(req)
        except urllib.error.HTTPError as exc:
            return exc.code, _json.loads(exc.read() or b"{}")
        cookie = res.headers.get("Set-Cookie")
        if cookie:
            self.cookie = cookie.split(";")[0]
        return res.status, _json.loads(res.read())


def start_dashboard(c, env_path):
    auth = AuthStore(c.data_dir / "dashboard_auth.json", c.dashboard_password)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(c, auth, env_path=env_path))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, auth, f"http://127.0.0.1:{server.server_address[1]}"


def make_bot_data(c):
    df = synthetic_ohlcv(3001, seed=4, momentum=0.4)
    TradingBot(c, broker=PaperBroker(c, FakeExchange(df))).step()
    j = TradeJournal(c.data_dir / "trades_paper.db")
    j.record(ts="2024-01-01T00:00:00+00:00", mode="paper", symbol="BTC/USDT", side="sell",
             amount=1, price=100, cost=100, fee=0.1, reason="take_profit", pnl=1.5)
    j.close()


def test_settings_validation(tmp_path):
    c = cfg(tmp_path)
    assert parse_changes(c, {"buy_threshold": 0.7, "symbols": "btc/usdt, sol/usdt"}) == {
        "buy_threshold": 0.7, "symbols": ("BTC/USDT", "SOL/USDT")}
    for bad in ({"mode": "live"}, {"api_key": "x"}, {"risk_per_trade": 0.5}, {"buy_threshold": "abc"},
                {"symbols": "BTC/EUR"}, {"sell_threshold": 0.6, "buy_threshold": 0.55}, {"timeframe": "2m"},
                {"bot_name": "bad<name>"}):
        with pytest.raises(ValueError):
            parse_changes(c, bad)
    with pytest.raises(ValueError, match="open position"):
        parse_changes(c, {"symbols": "ETH/USDT"}, open_symbols={"BTC/USDT"})


def test_write_env_keeps_other_lines(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# comment\nMODE=paper\nBUY_THRESHOLD=0.60\nAPI_KEY=secret\n")
    write_env(env, {"buy_threshold": 0.65, "trailing_stop": False, "symbols": ("BTC/USDT",)})
    text = env.read_text()
    assert "# comment" in text and "MODE=paper" in text and "API_KEY=secret" in text
    assert "BUY_THRESHOLD=0.65" in text and "TRAILING_STOP=false" in text and "SYMBOLS=BTC/USDT" in text
    assert (tmp_path / ".env.bak").exists()


def test_dashboard_auth_settings_and_fleet(tmp_path, monkeypatch):
    import os
    monkeypatch.setattr(os, "environ", os.environ.copy())  # .env reloads must not leak into other tests
    env = tmp_path / ".env"
    env.write_text("MODE=paper\n")
    c = cfg(tmp_path, buy_threshold=0.55, min_model_edge=-1.0)
    make_bot_data(c)
    server, auth, url = start_dashboard(c, env)
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    env2 = other_dir / ".env"
    env2.write_text("MODE=paper\n")
    c2 = cfg(other_dir, bot_name="pi-two", dashboard_password="other-password")
    make_bot_data(c2)
    server2, auth2, url2 = start_dashboard(c2, env2)
    cl = Client(url)
    try:
        assert cl.call("/api/auth")[1] == {"configured": False, "logged_in": False}
        assert cl.call("/api/data")[0] == 401
        assert cl.call("/api/setup", {"password": "short"})[0] == 400
        assert cl.call("/api/setup", {"password": "long-enough"}, headers={"X-GRG": "0"})[0] == 403
        assert cl.call("/api/setup", {"password": "long-enough"})[0] == 200
        assert cl.call("/api/setup", {"password": "another-one"})[0] == 409  # only once

        status, data = cl.call("/api/data")
        assert status == 200 and data["summary"]["closed_trades"] == 1 and data["name"]

        other = Client(url)
        assert other.call("/api/login", {"password": "wrong-pass"})[0] == 401
        assert other.call("/api/login", {"password": "long-enough"})[0] == 200
        assert other.call("/api/data")[0] == 200

        # settings: allowed change is written, forbidden ones are refused
        assert cl.call("/api/settings", {"values": {"buy_threshold": 0.7, "bot_name": "main"}})[0] == 200
        assert "BUY_THRESHOLD=0.7" in env.read_text() and "BOT_NAME=main" in env.read_text()
        assert cl.call("/api/settings", {"values": {"mode": "live"}})[0] == 400
        assert "MODE=paper" in env.read_text()
        assert cl.call("/api/settings")[1]["locked"]["mode"] == "paper"

        # fleet: the other bot only answers with its own key
        assert Client(url2).call("/api/fleet", headers={"X-Fleet-Key": "nope"})[0] == 403
        assert cl.call("/api/bots/add", {"url": url2, "key": "nope"})[0] == 400
        key2 = auth2.data["fleet_key"]
        status, added = cl.call("/api/bots/add", {"url": url2, "key": key2})
        assert status == 200 and added["name"] == "pi-two"
        bots = cl.call("/api/bots")[1]
        assert [b["name"] for b in bots] == ["main", "pi-two"] and all(b["online"] for b in bots)
        assert "api_key" not in _json.dumps(bots).lower()
        assert cl.call("/api/bots/remove", {"url": url2})[0] == 200
        assert len(cl.call("/api/bots")[1]) == 1

        # password change logs out old sessions
        assert cl.call("/api/password", {"current": "long-enough", "new": "brand-new-pass"})[0] == 200
        assert other.call("/api/data")[0] == 401
        assert cl.call("/api/data")[0] == 200
    finally:
        server.shutdown()
        server2.shutdown()


def test_bot_restarts_when_settings_change(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("MODE=paper\n")
    monkeypatch.setenv("GRG_ENV_FILE", str(env))
    c = cfg(tmp_path, poll_seconds=60)
    bot = TradingBot(c, broker=PaperBroker(c, FakeExchange(synthetic_ohlcv(3001, seed=4))))
    monkeypatch.setattr(bot, "step", lambda: None)
    monkeypatch.setattr("grgtrading.bot.signal.signal", lambda *a: None)

    def edit():
        import time as _t
        _t.sleep(1.5)
        env.write_text("MODE=paper\nBUY_THRESHOLD=0.7\n")
    threading.Thread(target=edit, daemon=True).start()
    t = threading.Thread(target=bot.run_forever, daemon=True)
    t.start()
    t.join(timeout=10)
    assert not t.is_alive()  # exited so systemd restarts it with the new settings
