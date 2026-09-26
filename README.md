# Options Agents — AI-assisted options day trading for Webull

A team of agents that scans the US market, finds option day-trade setups, sizes them for a
small account, and executes and manages them through your Webull account. It follows your
rules, and your approval if you want it.

> **Read this first.** Most retail options day traders lose money. This software does not
> change that by itself. What it gives you is discipline: consistent setups, fixed risk per
> trade, hard daily loss limits, PDT compliance, automatic exits, and a journal that tells
> you what is and isn't working. Run it in **sim** and **paper** mode for weeks before
> risking real money. Nothing here is financial advice.

---

## What it does

```
             ┌─────────────── Market Scanner agent ───────────────┐
             │ core watchlist + today's movers (gainers/losers,  │
             │ relative volume) → liquid "hot list" + features    │
             └────────────────────────────────────────────────────┘
                 │                  │                 │                 │
          Momentum agent      0DTE agent      Catalyst agent    Unusual Options agent
          ORB breakouts,      SPY/QQQ/IWM     gap-and-go /      volume ≫ open interest,
          VWAP reclaims,      same-day        gap-fade on       big premium, confirmed
          trend pullbacks     scalps          earnings/news     by price action
                 └──────────────────┴────────┬────────┴─────────────────┘
                                             ▼  signals (scored 0-100)
                              Contract Selector — expiry, delta, spread, OI, affordability
                                             ▼
                              Risk Manager — PDT, daily loss, streaks, sizing, time windows
                                             ▼
                              AI Analyst (optional) — Claude + web search: news, events, sanity check
                                             ▼
                 Execution agent — ALERTS │ APPROVE (you click) │ AUTO
                                             ▼
                 Position Manager — stop, scale-out, trailing stop, thesis stop, time stop, EOD flat
```

| Agent | Job |
|---|---|
| **Scanner** | Builds the watchlist every minute from your core list plus the day's movers. Computes VWAP, EMAs, opening range, relative volume, RSI and ATR. |
| **Momentum** | Opening-range breakouts, VWAP reclaim/rejection on volume, and trend-day pullbacks to EMA21. Uses 1–7 DTE options. |
| **0DTE** | Same-day SPY/QQQ/IWM trades on breaks of the 15-minute high/low and prior-day high/low, with trend and VWAP aligned. It has tighter stops and a 30-minute time stop. |
| **Catalyst** | Gappers of 4% or more with 2x+ relative volume, especially on earnings. Trades the gap-and-go continuation or a failed-gap fade. |
| **Unusual Options** | Contracts trading 3x+ their open interest with $250k+ premium. They're only traded when the underlying's price action agrees. |
| **Contract Selector** | Picks the nearest valid expiry, the strike closest to the target delta, and a spread of 10% or less. It also checks open interest, volume, and whether the contract fits your risk budget. |
| **Risk Manager** | Every trade passes through it in every mode. See *Risk controls* below. |
| **AI Analyst** | Optional. Claude reviews each proposal and can search the web for news or scheduled events. It returns *take / caution / skip* with its reasons. In auto mode you can require its approval. |
| **Execution** | Places limit orders starting at the mid price and steps toward the ask a few times, within a cap. It never chases. |
| **Position Manager** | Runs the exit plan on every open position until it's closed. |

### Trade modes (switch live from the dashboard)
- **Alerts:** agents only notify you (dashboard plus phone). You trade manually.
- **Approve** (default): each trade shows up as a card with the contract, size, max loss, stop and targets, reasons, and the AI verdict. You click **Approve & buy** and the app handles the entry and every exit.
- **Auto:** agents trade on their own inside your risk limits. Auto-trading real money needs a second opt-in, `ALLOW_LIVE_AUTO_TRADING=true`. Without it, live auto mode quietly runs as Approve.

### Risk controls (always on)
- **PDT:** margin accounts under $25k get at most 3 day trades per rolling 5 business days. Open positions and entries in progress count before they're closed, so a race can't slip in a 4th. The dashboard shows how many you have left.
- **Fixed-fractional sizing:** default 1% of equity lost if the stop hits. There are also caps on premium per trade ($750) and position size (10% of equity).
- **Daily loss limit:** new trades stop at -$300 or -3% of equity, whichever is smaller, counting open losses. Trading also stops after 3 losses in a row.
- **Limits:** max 2 open positions, max 4 trades a day, and one position per symbol.
- **Time windows:** no entries in the first 5 minutes, none after 15:30 (15:00 for 0DTE), and **everything is closed by 15:50 ET**.
- **Kill switch:** blocks new trades and can flatten everything with one click.
- **Cash accounts:** uses settled cash only (options settle T+1). Cash accounts are exempt from PDT.

### Exit plan (per position)
- Stop at -30% of premium (-35% for 0DTE).
- Sell half at +40%, then move the stop to breakeven.
- Close the rest at +90%.
- Trailing stop: 20% below the best price, once the position is up 25%.
- *Thesis stop:* exits if the underlying crosses the level that invalidates the setup.
- Time stop at 60 minutes (30 for 0DTE) if the trade isn't working.
- End-of-day flatten.

Webull doesn't accept market or trailing-stop orders on options. Exits are therefore marketable limit orders at the bid, stepped down a tick at a time until filled, so **keep the app running while you hold positions**.

---

## Quick start (5 minutes, no accounts needed)

```bash
git clone <this repo> && cd Optionstrading
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m optrader run --mode sim
```

Open http://127.0.0.1:8000. **Sim mode** runs a synthetic market at 30x speed (a full day in about 13 minutes), with realistic regimes, gaps, earnings movers and unusual options prints. Use it to learn the dashboard, try the Approve flow, and experiment with settings.

## Connect Webull (paper first)

1. **Get OpenAPI access.** Apply for Webull OpenAPI in the Webull app or at https://developer.webull.com. Then create an application and copy its **App Key** and **App Secret**. For real-time quotes you may need Webull's market-data subscription for API use. Check your developer portal.
2. `cp .env.example .env` and fill in `WEBULL_APP_KEY` and `WEBULL_APP_SECRET`.
3. **Test the connection:**
   ```bash
   python -m optrader diagnose
   ```
   The first time, Webull sends a login approval to your phone. Approve it and the token is cached in `data/webull_token/`. The command prints raw responses from every endpoint the app uses: account, balance, positions, quotes, bars, option chains, option snapshot, and screeners.
4. **Paper trade with real data** (simulated fills against live Webull quotes):
   ```bash
   cp config.example.yaml config.yaml   # set app.mode: paper
   python -m optrader run --mode paper
   ```
   With no Webull keys yet, set `app.data_source: yahoo` for free, delayed data. That's fine for learning the flow, not for real day trading.
5. **Go live, only after you trust the paper results:**
   ```bash
   python -m optrader run --mode live      # asks you to type LIVE
   ```
   Live mode starts in whatever trade mode you configured (Approve by default).

## Use it from your phone
- **Notifications:** set `NTFY_TOPIC`, `DISCORD_WEBHOOK_URL` or Telegram in `.env`. You get pushed proposals ("Approve?"), fills and exits.
- **Dashboard on your phone:** the UI is mobile-friendly. To reach it away from your computer, set `DASHBOARD_TOKEN` and `app.host: 0.0.0.0`, and connect through a private network such as [Tailscale](https://tailscale.com). **Don't expose it to the public internet.** The app refuses to bind to a non-local address without a token.

## Backtesting
```bash
python -m optrader backtest --source sim   --days 20 --no-pdt
python -m optrader backtest --source yahoo --days 5  --symbols SPY,QQQ,NVDA,TSLA   # last ~7 days of 1-min bars
python -m optrader backtest --source yahoo --days 50 --interval 5m                 # up to 60 days of 5-min bars
```
The backtester replays history through the **same** strategy, risk and exit code as live trading. It reports win rate, profit factor, expectancy, drawdown, and results by strategy and by exit reason, and saves them to `backtest_results/`. Option prices are *modeled* with Black-Scholes, realized volatility and a spread, because historical option quotes aren't free. Treat backtests as a filter for bad ideas, not a promise.

## Making it better over time
1. Run **sim**, then **paper**, and keep it running for 2–4 weeks.
2. Check the **Journal** tab: P&L by strategy and by exit reason. Turn off strategies that lose (Settings → strategies) and tighten or loosen exits based on the data.
3. Raise `min_signal_score` if too many marginal trades lose. Try `ai.required_for_auto` with the AI analyst.
4. Only go live with small size. Keep `risk_per_trade_pct` around 0.5–1% and let the numbers, not hope, decide when to scale.

## Configuration
- `config.yaml` has all the knobs; see `config.example.yaml` for every option with its default.
- `.env` holds secrets: Webull keys, dashboard token, Anthropic key and notification hooks.
- Dashboard **Settings** changes are saved to `data/runtime_overrides.json`.

## Project layout
```
optrader/
  agents/        scanner, strategies (momentum/0DTE/catalyst), unusual_options, contract_selector,
                 risk, ai_analyst, execution, position_manager, features
  data/          webull_data (OpenAPI), yahoo_data (fallback), simulated (sim market),
                 indicators, options_math (Black-Scholes/IV/greeks)
  brokers/       webull_broker (live orders via order_v3), paper (conservative fill simulator)
  api/server.py  FastAPI dashboard + REST + Server-Sent Events
  web/           dashboard (vanilla JS, mobile-friendly)
  orchestrator.py, backtest.py, analytics.py, config.py, db.py (SQLite journal)
tests/           unit + end-to-end tests (pytest)
```

## Tests
```bash
python -m pytest
```
Covers options math, indicators, strategies, contract selection, paper fills, PDT, sizing, daily limits, every exit rule, and the Webull payload format. It also includes the AI analyst with a fake client, the dashboard API with auth, full simulated trading days, and a regression test showing that concurrent approvals can't exceed the PDT limit.

## Known limitations / notes
- The Webull adapters use the official `webull-openapi-python-sdk` (3.x) with `order_v3` for options and the documented order format. Response field names are parsed defensively. If `diagnose` shows different names for your account, adjust the `pick(...)` keys in `optrader/data/webull_data.py` or `brokers/webull_broker.py`.
- Snapshot option data can't tell whether unusual volume was bought or sold, so UOA signals require price confirmation.
- The app manages stops itself, so if your computer or the app goes down, stops won't fire. Run it on a machine that stays on, and consider a manual stop in the Webull app as a backstop for larger positions.
