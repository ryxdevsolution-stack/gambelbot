"""Same as today_check.py, but scans BOTH real and OTC markets (not just
real), tags each entry by category, and reports the winning percentage
overall and split real vs OTC for today.

Read-only. Does not place trades.
"""

import asyncio
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from pyquotex.stable_api import Quotex

load_dotenv()

HOURS = 48  # just enough history to seed the pattern/confirmation logic
MIN_PER_CATEGORY = 10
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


def confirmed_entries_for_asset(asset, category, candles):
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
        entries.append({"time": candles[i]["time"], "asset": asset, "category": category, "won": won})
    return entries


def day_key(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).date()


def ci(wins, trades):
    if trades == 0:
        return None
    p = wins / trades
    se = (p * (1 - p) / trades) ** 0.5
    lo, hi = max(0, p - 1.96 * se), min(1, p + 1.96 * se)
    return p, lo, hi


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
        log.append((e["time"], e["asset"], e["category"], won, day_pnl))

        if day_pnl >= DAILY_TARGET or day_pnl <= DAILY_STOP:
            stopped = True

    return today, log, day_trades, day_wins, day_pnl, stopped, todays


async def main():
    email = os.environ["QUOTEX_EMAIL"]
    password = os.environ["QUOTEX_PASSWORD"]

    client = Quotex(email=email, password=password, lang="en", root_path="data")
    check, reason = await client.connect()
    if not check:
        raise SystemExit(f"Connection failed: {reason}")

    try:
        all_assets = client.get_all_asset_name() or []
        seen = set()
        unique_assets = [(c, n) for c, n in all_assets if not (c in seen or seen.add(c))]

        real_assets = [c for c, _ in unique_assets if "_otc" not in c.lower()]
        otc_assets = [c for c, _ in unique_assets if "_otc" in c.lower()]

        async def find_open(candidates, limit):
            found = []
            for asset in candidates:
                try:
                    _info, status = await client.check_asset_open(asset)
                    is_open = bool(status[2]) if status else False
                except Exception:
                    is_open = False
                if is_open:
                    found.append(asset)
                if len(found) >= limit:
                    break
            return found

        open_real = await find_open(real_assets, MIN_PER_CATEGORY)
        open_otc = await find_open(otc_assets, MIN_PER_CATEGORY)

        print(f"Open REAL markets: {len(open_real)} -> {open_real}")
        print(f"Open OTC markets:  {len(open_otc)} -> {open_otc}")
        print()

        all_entries = []
        for asset in open_real:
            try:
                candles = await client.get_historical_candles(
                    asset=asset, amount_of_seconds=HOURS * 3600, period=PERIOD, max_workers=3,
                )
            except Exception as e:
                print(f"{asset}: FAILED ({e})")
                continue
            all_entries.extend(confirmed_entries_for_asset(asset, "real", candles))

        for asset in open_otc:
            try:
                candles = await client.get_historical_candles(
                    asset=asset, amount_of_seconds=HOURS * 3600, period=PERIOD, max_workers=3,
                )
            except Exception as e:
                print(f"{asset}: FAILED ({e})")
                continue
            all_entries.extend(confirmed_entries_for_asset(asset, "otc", candles))

        today, log, trades, wins, pnl, stopped, todays_all = simulate_today(all_entries)

        print(f"=== TODAY ({today}) -- real + OTC combined, live check ===")
        if not log:
            print("No confirmed pattern entries yet today (after chart-switch-on-loss filtering).")
        for t, asset, category, won, running_pnl in log:
            ts = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%H:%M UTC")
            print(f"{ts}  {asset:10s} [{category:4s}]  {'WIN ' if won else 'LOSS'}  running_pnl=${running_pnl:+.2f}")

        print()
        print(f"--- Executed trades (after chart-switch + target/stop rule) ---")
        print(f"Trades: {trades}  Wins: {wins}")
        result = ci(wins, trades)
        if result:
            p, lo, hi = result
            print(f"Win rate: {p:.1%} (95% CI {lo:.1%}-{hi:.1%})")
        print(f"P&L: ${pnl:+.2f}")
        if stopped:
            verdict = "TARGET HIT (+$4) -- stop trading" if pnl > 0 else "STOP HIT (-$9) -- stop trading"
            print(f"Status: {verdict}")
        else:
            print("Status: still in play -- neither target nor stop reached yet")

        print()
        print(f"--- Raw signal quality today, ALL confirmed entries (no chart-switch/stop filtering) ---")
        total_wins = sum(1 for e in todays_all if e["won"])
        total_n = len(todays_all)
        result = ci(total_wins, total_n)
        if result:
            p, lo, hi = result
            print(f"Overall: {total_wins}/{total_n} = {p:.1%} (95% CI {lo:.1%}-{hi:.1%})")
        for cat in ("real", "otc"):
            cat_entries = [e for e in todays_all if e["category"] == cat]
            cat_wins = sum(1 for e in cat_entries if e["won"])
            cat_n = len(cat_entries)
            result = ci(cat_wins, cat_n)
            if result:
                p, lo, hi = result
                print(f"{cat.upper():4s}: {cat_wins}/{cat_n} = {p:.1%} (95% CI {lo:.1%}-{hi:.1%})")
            else:
                print(f"{cat.upper():4s}: no confirmed entries today")
    finally:
        await client.close()


asyncio.run(main())
