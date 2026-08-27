"""Live "did we hit target/stop today" check -- same pattern + confirmation
filter + chart-switch-on-loss + $4 target / $9 stop rule as
chart_switch_backtest.py, but only fetches a short recent window (48h)
so it runs fast, and only prints TODAY's result.

Read-only. Does not place trades.
"""

import asyncio
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from pyquotex.stable_api import Quotex

load_dotenv()

HOURS = 48  # just enough history to seed the pattern/confirmation logic
MIN_REAL_MARKETS = 10
PERIOD = 300  # 5-min

STAKE = 1.0
PAYOUT = 0.80
DAILY_TARGET = 4.0
DAILY_STOP = -9.0


def candle_color(c):
    if c["close"] > c["open"]:
        return "G"
    if c["close"] < c["open"]:
        return "R"
    return "D"


def find_triggers(colors):
    triggers = []
    for i in range(2, len(colors)):
        if colors[i] != "R":
            continue
        streak = 0
        j = i - 1
        while j >= 0 and colors[j] == "G":
            streak += 1
            j -= 1
        if streak >= 2:
            triggers.append(i)
    return triggers


def resolve(colors, i):
    if i + 1 >= len(colors):
        return None
    if colors[i + 1] == "R":
        return "direct"
    if colors[i + 1] == "G":
        if i + 2 >= len(colors):
            return None
        if colors[i + 2] == "R":
            return "martingale"
        if colors[i + 2] == "G":
            return "double_loss"
        return None
    return None


def confirmed_entries_for_asset(asset, candles):
    candles = sorted(candles, key=lambda c: c["time"])
    colors = [candle_color(c) for c in candles]
    triggers = find_triggers(colors)
    resolutions = [(i, resolve(colors, i)) for i in triggers]
    resolutions = [(i, t) for i, t in resolutions if t is not None]

    entries = []
    for k in range(1, len(resolutions)):
        prev_type = resolutions[k - 1][1]
        if prev_type != "direct":
            continue
        i, cur_type = resolutions[k]
        won = cur_type == "direct"
        entries.append({"time": candles[i]["time"], "asset": asset, "won": won})
    return entries


def day_key(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).date()


def simulate_today(all_entries):
    today = datetime.now(timezone.utc).date()
    todays = sorted([e for e in all_entries if day_key(e["time"]) == today], key=lambda e: e["time"])

    day_pnl = 0.0
    day_trades = 0
    day_wins = 0
    last_asset = None
    last_won = None
    stopped = False
    log = []

    for e in todays:
        if stopped:
            break
        if last_asset == e["asset"] and last_won is False:
            continue

        won = e["won"]
        day_pnl += STAKE * PAYOUT if won else -STAKE
        day_trades += 1
        if won:
            day_wins += 1
        last_asset = e["asset"]
        last_won = won
        log.append((e["time"], e["asset"], won, day_pnl))

        if day_pnl >= DAILY_TARGET or day_pnl <= DAILY_STOP:
            stopped = True

    return today, log, day_trades, day_wins, day_pnl, stopped


async def main():
    email = os.environ["QUOTEX_EMAIL"]
    password = os.environ["QUOTEX_PASSWORD"]

    client = Quotex(email=email, password=password, lang="en", root_path="data")
    check, reason = await client.connect()
    if not check:
        raise SystemExit(f"Connection failed: {reason}")

    try:
        all_assets = client.get_all_asset_name() or []
        real_assets = [code for code, _name in all_assets if "_otc" not in code.lower()]
        seen = set()
        real_assets = [a for a in real_assets if not (a in seen or seen.add(a))]

        open_real_assets = []
        for asset in real_assets:
            try:
                _info, status = await client.check_asset_open(asset)
                is_open = bool(status[2]) if status else False
            except Exception:
                is_open = False
            if is_open:
                open_real_assets.append(asset)
            if len(open_real_assets) >= MIN_REAL_MARKETS:
                break

        print(f"Open real markets found: {len(open_real_assets)} -> {open_real_assets}")
        print()

        all_entries = []
        for asset in open_real_assets:
            try:
                candles = await client.get_historical_candles(
                    asset=asset, amount_of_seconds=HOURS * 3600, period=PERIOD, max_workers=3,
                )
            except Exception as e:
                print(f"{asset}: FAILED ({e})")
                continue
            all_entries.extend(confirmed_entries_for_asset(asset, candles))

        today, log, trades, wins, pnl, stopped = simulate_today(all_entries)

        print(f"=== TODAY ({today}) -- live check ===")
        if not log:
            print("No confirmed pattern entries yet today.")
        for t, asset, won, running_pnl in log:
            ts = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%H:%M UTC")
            print(f"{ts}  {asset:8s}  {'WIN ' if won else 'LOSS'}  running_pnl=${running_pnl:+.2f}")

        print()
        print(f"Trades so far today: {trades}  Wins: {wins}")
        print(f"P&L so far: ${pnl:+.2f}")
        if stopped:
            verdict = "TARGET HIT (+$4) -- stop trading" if pnl > 0 else "STOP HIT (-$9) -- stop trading"
            print(f"Status: {verdict}")
        else:
            print("Status: still in play -- neither target nor stop reached yet")
    finally:
        await client.close()


asyncio.run(main())
