# GrgTrading

An AI-driven crypto trading bot built to run 24/7. It uses a machine-learning
model (gradient boosting) trained on technical indicators, wrapped in strict
risk management, and can trade on any exchange supported by
[ccxt](https://github.com/ccxt/ccxt) (Binance, Bybit, OKX, Kraken, ...).

> **Read this before using real money.**
> No bot guarantees profit, and most automated strategies lose money after
> fees. The bot starts in **paper** mode (simulated money on real prices) by
> default. Run a `backtest` first, then paper-trade for a few weeks, and only
> switch to `live` with an amount you can afford to lose completely.

## How it works

1. **Data**: downloads OHLCV candles (1h by default) for every pair in `SYMBOLS`.
2. **Features**: multi-horizon returns, RSI, MACD, Bollinger bands, ATR,
   volume, EMA trend, volatility, time of day (`grgtrading/features.py`).
3. **AI model** (`grgtrading/model.py`): a `HistGradientBoostingClassifier`
   estimates the probability that price rises over the next 6 candles by
   enough to cover fees. It retrains automatically every 12 hours.
4. **Overfitting guard**: before each use the model is validated walk-forward
   on data it has never seen. If its signals don't clearly beat chance
   (overall *and* in most periods), the bot **opens no new positions** with it.
   Sitting still beats trading on noise.
5. **Decision**: buys (long only, spot, no leverage) when the probability is
   ≥ `BUY_THRESHOLD`; sells when it drops below `SELL_THRESHOLD`.
6. **Risk** (`grgtrading/risk.py`):
   - positions are sized so that hitting the stop loses ~1% of equity;
   - ATR-based stop-loss and take-profit, with a trailing stop;
   - at most `MAX_POSITION_PCT` of equity per position, at most `MAX_OPEN_POSITIONS`;
   - daily loss > 5% → no new entries until the next UTC day;
   - drawdown > 20% from peak → **kill switch**: close everything and stop.
7. **24/7**: the loop retries network and exchange errors with backoff, state
   is saved to disk after every trade, and Docker restarts the container if it
   stops.

## Quick start

```bash
cp .env.example .env          # edit your settings
pip install -r requirements.txt

python -m grgtrading backtest # test the strategy on ~1 year of history
python -m grgtrading check    # verify connection, keys, balance and exchange minimums
python -m grgtrading run      # start the bot (paper trading by default)
python -m grgtrading status   # equity, open positions, recent trades
```

### Reading the backtest

- `model_tradeable_share` = 0 means the model found no edge; it would not trade live either.
- Compare `total_return_pct` with `buy_and_hold_pct`. If the bot does worse than
  simply holding, it is not worth running.
- `max_drawdown_pct` is the worst drop along the way. Expect the same or worse live.
- With fewer than ~30 `trades`, the result may just be luck.

### Running 24/7 with Docker (recommended, on a VPS)

```bash
cp .env.example .env
docker compose up -d --build
docker compose logs -f
docker compose exec bot python -m grgtrading status
```

`restart: unless-stopped` brings it back after a crash or reboot.
Data (state, models, trade journal, logs) lives in `./data`.

### Running 24/7 on a Raspberry Pi or any Linux machine

Works on a Raspberry Pi 4/5 and, with 512 MB RAM, on a Pi Zero 2 W. Flash
**Raspberry Pi OS Lite (64-bit)** (the 32-bit OS would have to compile pandas and
scikit-learn for hours), enable SSH and Wi-Fi in Raspberry Pi Imager, then:

```bash
sudo apt-get install -y git
git clone https://github.com/monescuteodor/GrgTrading
cd GrgTrading
bash deploy/install-pi.sh        # venv, dependencies, systemd service
nano .env                        # settings (paper mode by default)
sudo systemctl start grgtrading  # runs now and after every reboot
journalctl -u grgtrading -f      # live log
```

The same script works on any Linux PC or laptop with systemd (Debian/Ubuntu,
Arch/BlackArch, Fedora). On rolling-release distros like Arch, a Python upgrade can
break the virtual environment: delete `venv` and run the script again.

On a Pi Zero 2 W, keep `SYMBOLS` to one or two pairs. If the bot is killed while
retraining (out of memory), give it 1 GB of swap.

## Going live

1. **Optional dry run on the testnet** (fake money, real order flow): create
   testnet API keys (Binance: https://testnet.binance.vision), then set
   `MODE=live`, `SANDBOX=true` and the testnet keys. Testnet prices and
   liquidity are not realistic, so use it to check that orders work, not to
   judge profit.
2. On the real exchange, create an API key with **spot trading enabled and
   withdrawals disabled**, restricted to your server's IP. A dedicated
   sub-account for the bot is best.
3. In `.env`:
   ```
   MODE=live
   SANDBOX=false
   API_KEY=...
   API_SECRET=...
   LIVE_CONFIRM=I_UNDERSTAND_THE_RISKS
   ```
   For a first live run, consider tighter limits: `RISK_PER_TRADE=0.005`,
   `MAX_OPEN_POSITIONS=1`, `MAX_DRAWDOWN_PCT=0.10`, `DAILY_LOSS_LIMIT_PCT=0.03`.
4. Run `python -m grgtrading check` to confirm the keys work and your balance
   clears the exchange minimums, then start the bot.
5. While it runs, don't trade manually in the same account and don't deposit
   or withdraw: the bot would read that as profit or loss and its risk limits
   would be off.

Stops are managed by the bot (checked every `POLL_SECONDS`), not placed as
orders on the exchange, so open positions are unprotected while the server is down.

## Small accounts

Exchanges reject orders below a minimum value (on Binance typically around
5 USDT per order; `check` shows the exact figure for your exchange and pairs).
The bot also keeps a 25% margin above that minimum so a stop-loss sell after a
price drop is still accepted. So:

- **The balance must be above the smallest tradable position** shown by
  `check`. Below that, no bot can place a trade on that exchange.
- With a balance just above the minimum, the normal 1%-risk sizing produces
  orders that are too small. Set `SMALL_ACCOUNT_MODE=true` to buy the minimum
  instead. Each trade then puts most of the account at stake.
- Trade a single pair (`SYMBOLS=BTC/USDT`) and keep `MAX_OPEN_POSITIONS=1`.
- Try it in paper mode first with the same amount, e.g. `STARTING_BALANCE=7`;
  paper mode applies the real exchange minimums.

## Private dashboard

`python -m grgtrading dashboard` serves a page on port 8080. The Raspberry Pi
installer runs it as the `grgtrading-dashboard` service; open
`http://grgtrading.local:8080` from a device on the same network.

- **First visit:** you create the dashboard password (stored only as a salted hash
  in `data/`). Forgot it? Delete `data/dashboard_auth.json` over SSH and restart
  the dashboard service.
- **Overview:** equity curve, P&L per trade, KPIs, open positions, recent trades.
- **Settings:** change pairs, interval, thresholds and risk limits. Values are
  validated, written to `.env`, and the bot restarts itself to apply them; open
  positions are kept. Mode, exchange and API keys are **not** editable here, so a
  leaked dashboard password can never switch the bot to real money.
- **Bots:** see several bots in one table. Each bot shows a *fleet key* under
  Settings; add another bot with its dashboard address and that key. The key only
  unlocks a read-only summary.

Your numbers stay on the machine and are not part of the repository. Never forward
this port on your router.

## Telegram notifications (optional)

Create a bot with [@BotFather](https://t.me/BotFather), put `TELEGRAM_TOKEN`
and `TELEGRAM_CHAT_ID` in `.env`, and you'll get a message for every buy and
sell, repeated errors, and the kill switch.

## Main settings

| Variable | Default | What it does |
|---|---|---|
| `MODE` | `paper` | `paper` or `live` |
| `SANDBOX` | `false` | in live mode, use the exchange testnet |
| `EXCHANGE` | `binance` | any ccxt exchange id |
| `SYMBOLS` | `BTC/USDT,ETH/USDT` | pairs to trade (same quote currency) |
| `TIMEFRAME` | `1h` | candle interval |
| `BUY_THRESHOLD` / `SELL_THRESHOLD` | `0.60` / `0.45` | probability thresholds |
| `RISK_PER_TRADE` | `0.01` | share of equity lost if a stop is hit |
| `SMALL_ACCOUNT_MODE` | `false` | trade the exchange minimum on tiny balances |
| `MAX_DRAWDOWN_PCT` | `0.20` | kill-switch threshold |
| `DAILY_LOSS_LIMIT_PCT` | `0.05` | pause for the rest of the day |

Full list: `grgtrading/config.py` and `.env.example`.

## Website

`website/` holds a static landing page (HTML, CSS, JS, no build step). To host it
on Cloudflare Pages: connect the repo, leave the build command empty and set the
output directory to `website`. Or upload the folder via "Direct Upload".

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

## Layout

```
grgtrading/
  config.py     settings from .env
  features.py   technical indicators and training labels
  model.py      the AI model and walk-forward validation
  risk.py       position sizing, stops, kill switch
  broker.py     paper trading and real orders via ccxt
  bot.py        the 24/7 loop
  backtest.py   walk-forward backtest with fees
  storage.py    bot state and trade journal (SQLite)
  notify.py     Telegram notifications
```
