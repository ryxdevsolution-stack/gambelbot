# Quotex Signal Bot

A live signal bot for Quotex (binary options broker). Watches 5-minute
candles on real (non-OTC) markets for one specific candlestick pattern,
sends Telegram alerts when it fires, and — optionally — places the trade
itself on your **DEMO** account. Includes a local live dashboard.

This is a live, risk-capped **experiment**, not a proven strategy — see
[The pattern](#the-pattern) below.

## Contents

- [Setup](#setup)
- [Running it](#running-it)
- [The dashboard](#the-dashboard)
- [The pattern](#the-pattern)
- [Modes / env vars](#modes--env-vars)
- [Troubleshooting](#troubleshooting)
- [Known issues / caveats](#known-issues--caveats)
- [Project layout](#project-layout)

## Setup

**Requirements:** Docker Desktop, a Quotex account, a Telegram bot token +
chat id (optional but recommended — see [BotFather](https://t.me/BotFather)).

`pyquotex` (the unofficial, reverse-engineered Quotex client this project
depends on) needs Python **≥3.12**; that's why everything runs through
Docker rather than directly with Python 3.10/3.11 on your machine.

1. Copy `.env.example` to `.env` and fill in real values:
   ```
   QUOTEX_EMAIL=you@example.com
   QUOTEX_PASSWORD=your-password
   TELEGRAM_BOT_TOKEN=123456:ABC...
   TELEGRAM_CHAT_ID=123456789
   ```
   Never commit `.env` — it's already in `.gitignore`.

2. Build the image (from the project root):
   ```powershell
   docker build -t quotex-bot .
   ```

## Running it

**Windows — PowerShell** (recommended terminal on this OS):
```powershell
cd "c:\Users\loges\Documents\GitHub\gambelbot"
docker run --rm -it --name qxbot --env-file .env -p 8787:8787 -v "${PWD}:/app" quotex-bot python3 live_signal_bot.py
```

**Git Bash / MSYS**, if you use that instead — the volume mount needs
`MSYS_NO_PATHCONV=1` or Docker mangles the `/app` path:
```bash
MSYS_NO_PATHCONV=1 docker run --rm -it --name qxbot --env-file .env -p 8787:8787 -v "$(pwd):/app" quotex-bot python3 live_signal_bot.py
```

- First login (or after a session reset) it emails a **PIN code** to your
  Quotex address — check email and type it in at the prompt, quickly (the
  code expires in a couple of minutes; if it fails once, `rm session.json`
  and just retry — a fresh code each time is normal).
- Once connected, it watches 10 open real markets on 5-minute candles.
- Signal alerts arrive on Telegram. **By default it never places trades** —
  you place them yourself.
- Leave the window open. `Ctrl+C` to stop. It's meant to run attended for a
  session, not unattended all day.

## The dashboard

Open **http://localhost:8787** while the bot is running (needs the
`-p 8787:8787` flag above). It's a small built-in web server
(`live_signal_bot.py` + `dashboard.html`), polling every second, showing:

- **One card per asset**: last 14 closed candles as colored bars
  (green/red/gray) plus a dashed bar for the candle currently forming —
  so you can watch the trigger pattern build in real time instead of
  reading logs.
- **Green streak count** and a **CONFIRMED ✓** badge that lights up the
  instant both trigger conditions are true.
- **Countdown** to each candle's close (turns amber in the last few
  seconds — when the bot actually checks for a signal).
- **Daily P&L** and stop status.
- **Recent Signals** and **Recent Trades** tables for the session.

No extra setup — it reads directly off the bot's own live state, no
separate service to run.

## The pattern

1. **Trigger shape:** 2 or more green (up) candles in a row, immediately
   followed by 1 red (down) candle — 5-minute candles, real (non-OTC)
   markets only.
2. **Confirmation filter:** only act on that trigger if the *previous*
   time this exact shape happened on that asset, the very next candle was
   *also* red — a clean win, no martingale needed. If the last occurrence
   needed a martingale, lost twice straight, or hasn't resolved yet, the
   current trigger is skipped.
3. **Entry:** if both hold, trade **DOWN (PUT)** on the candle right after
   the trigger, 5-minute expiry, flat ₹100 stake — no martingale, no
   scaling.
4. **Daily stop:** if running P&L hits **-₹500** (5 straight losses at
   flat stake), it stops sending/taking new signals for the rest of the
   day.

Implemented in [`live_signal_bot.py`](live_signal_bot.py):
`AssetState.is_current_trigger()` (step 1), `confirmed_by_prev_trigger()`
(step 2), `DailyPnL` (step 4). [`strategy.py`](strategy.py) has the same
rules in a form shared with the offline backtest tooling.

**This pattern did not show a statistically confirmed edge in
backtesting** (validation-set results were flat/negative) — it runs live
anyway as a closely risk-capped experiment, not because the numbers
proved it works. Treat results as a live test, not a guarantee.

## Modes / env vars

| Env var | Default | Effect |
|---|---|---|
| `AUTO_TRADE_DEMO` | `0` | Set to `1` to have the bot place the ₹100 trade itself, on your **DEMO** account only. There is no code path to real-money auto-trade. |
| `DEBUG_HEARTBEAT` | `0` | Set to `1` for a ~15s heartbeat per asset plus a `DEBUG candle closed ...` line every time a candle closes with its official open/close/color — useful for verifying a signal against the real data in real time rather than after the fact. |
| `DASHBOARD_PORT` | `8787` | Port the dashboard listens on inside the container; remap with `-p <host>:<container>` if you change it or it collides with something else. |
| `PAYOUT_MIN` | `85` | Signals are held back (not sent, not auto-traded) if the asset's current live payout % is below this. Quotex's payout per asset changes through the day; the bot rechecks every 60s. Real (non-OTC) markets have topped out around 87% in practice, so setting this above ~87 will silence most/all signals. |
## The browser start/stop dashboard

`dashboard_server.py` (separate from the read-only dashboard above) gives you a **Start/Stop button in the browser**, plus PIN/OTP entry on the page itself instead of the terminal — handy for running the bot from a phone.

```bash
docker run --rm -it --env-file .env -p 8090:8090 -v "$(pwd):/app" quotex-bot python3 dashboard_server.py
```

Open `http://<host>:8090`. Clicking Start runs the exact same `live_signal_bot.py` as running it directly from the command line — same logic, same fixes, just controlled from a page instead of a terminal.

**This binds to all interfaces (`0.0.0.0`) with no login of any kind.** Anyone who can reach the host and port can start/stop the bot and place trades — there is no password protection. If you expose this port on a public IP (e.g. a VPS), that control panel is open to the whole internet. Prefer an SSH tunnel (`ssh -L 8090:localhost:8090 user@host`, then open `http://localhost:8090` locally) or a firewall rule restricting the port to your own IP.

Example, everything on:
```powershell
docker run --rm -it --name qxbot --env-file .env -e AUTO_TRADE_DEMO=1 -e DEBUG_HEARTBEAT=1 -p 8787:8787 -v "${PWD}:/app" quotex-bot python3 live_signal_bot.py
```

## Troubleshooting

- **"Connection failed" / authorization rejected** — stale login. Run
  `rm -f session.json`, then start the bot again; it'll email a fresh PIN.
- **PIN submission fails right after you get the email** — the code
  expires quickly (a couple of minutes). Retry and enter it as fast as
  possible; the PIN must be submitted in the *same* `docker run` process
  that requested it (it's tied to that login session's cookies).
- **Docker can't find `/app` / mount looks empty (Git Bash only)** —
  prefix the command with `MSYS_NO_PATHCONV=1`; Git Bash otherwise rewrites
  `/app` into a Windows path before Docker sees it.
- **Dashboard tab shows nothing** — make sure you launched with
  `-p 8787:8787` and that the bot has printed `Dashboard: http://localhost:8787`
  (it starts before the Quotex login completes, so the page loads even
  during a PIN wait — it'll just show "connecting…" for the asset cards).

## Known issues / caveats

- **`session.json` is committed to git.** It holds live Quotex login
  cookies — functionally a credential. The current `.gitignore` only
  matches `data/*.json`, not the top-level `session.json`, so it isn't
  even excluded going forward, and it's already in history from the
  initial commit. Worth stripping from tracking (`git rm --cached
  session.json`) and adding an explicit `.gitignore` entry.
- **Live vs. official candle data can disagree.** During testing, a signal
  fired for a candle sequence that the broker's own historical-candle API
  later reported differently (only 1 green candle before the trigger, not
  the required 2+). The code already has a defensive fallback for this
  (`fetch_official_candle` retries, and warns if it has to fall back to
  less-reliable tick-derived coloring) — but it's a real, observed
  discrepancy, not just theoretical. Run with `DEBUG_HEARTBEAT=1` if you
  want to catch it happening live.
- Auto-trade only ever touches the **DEMO** account — this is intentional,
  not a limitation to work around.

## Project layout

**Core (used by the running bot):**

| File | Purpose |
|---|---|
| [`live_signal_bot.py`](live_signal_bot.py) | The live bot: connects, watches candles, sends alerts, optional auto-trade, serves the dashboard. |
| [`dashboard.html`](dashboard.html) | The dashboard's front end, served by the bot at `/`. |
| [`strategy.py`](strategy.py) | Pattern/confirmation/money-management rules, shared by the backtest tooling. |
| [`bot.py`](bot.py) | Unfinished "phase 2" scaffold — superseded by `live_signal_bot.py`. |
| `Dockerfile`, `requirements.txt` | Container build (Python 3.12 + `pyquotex`). |
| `.env` / `.env.example` | Credentials and Telegram config (never commit `.env`). |
| `session.json` | Saved Quotex login session — see caveat above. |

**Research / one-off scripts** (not part of the running bot; each has its
own docstring with more detail):

| File | Purpose |
|---|---|
| `backtest.py` | Replay `strategy.py` rules over a historical CSV — win rate, EV, drawdown. |
| `pattern_search.py`, `best_pattern_search.py` | Systematic search over cached candle data for profitable patterns. |
| `prev_trigger_confirm.py`, `chart_switch_backtest.py`, `daily_cap_backtest.py`, `sma_ema_filter.py`, `sr_bounce.py`, `last_n_days_pattern.py` | Variants/backtests of the confirmation rule and related filters. |
| `fetch_data.py`, `fetch_multi_tf.py`, `fetch_otc_data.py`, `fetch_search_data.py` | Pull historical candles from Quotex into `data/*.csv` for offline analysis. |
| `today_check.py`, `today_check_all.py`, `multi_check.py`, `verify_trigger.py` | Live/manual checks against current-day data without running the full bot. |
| `artifact_check.py` | Checks OTC candle data for broker-synthetic statistical artifacts. |
| `check_balance.py` | Read-only: print demo/real account balances. |
| `test_auto_trade.py` | One-off: place a single demo trade and inspect `buy()`/`check_win()` output, to sanity-check the auto-trade path. |
| `debug_login.py`, `debug_auth_full.py`, `debug_buy.py`, `debug_assets.py` | Low-level diagnostics for login/auth/order-placement issues. |

`data/` holds fetched CSVs and diagnostic dumps (gitignored, except
`session.json` — see caveat above).
