"""Same pattern + confirmation filter as prev_trigger_confirm.py (only
take a trigger if the immediately preceding trigger on that SAME asset
resolved as a direct win), but now simulating actual execution rules:

  - Flat $1 stake per trade (no martingale) -- "direct profit" focus.
  - After a LOSING trade, the next trade must be on a DIFFERENT asset
    (rotate off a losing chart).
  - Stop trading for the day once daily P&L hits +$4 (target) or
    -$9 (stop), whichever comes first. Resume next day.

Read-only. Scans >=N real (non-OTC) open markets on 5-min candles over
a configurable window in one login session.
"""

import asyncio
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from pyquotex.stable_api import Quotex

load_dotenv()

HOURS = 672  # 4 weeks
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
        return "direct"  # flat-bet win
    if colors[i + 1] == "G":
        if i + 2 >= len(colors):
            return None
        if colors[i + 2] == "R":
            return "martingale"  # flat-bet loss (would've needed a 2nd bet to recover)
        if colors[i + 2] == "G":
            return "double_loss"  # flat-bet loss
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
        won = cur_type == "direct"  # flat single-shot bet outcome
        entries.append({"time": candles[i]["time"], "asset": asset, "won": won})
    return entries


def day_key(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).date()


def simulate(all_entries):
    all_entries.sort(key=lambda e: e["time"])
    by_day = {}
    for e in all_entries:
        by_day.setdefault(day_key(e["time"]), []).append(e)

    day_results = []
    total_trades = 0
    total_wins = 0
    total_pnl = 0.0

    for d in sorted(by_day):
        day_pnl = 0.0
        day_trades = 0
        day_wins = 0
        last_asset = None
        last_won = None
        stopped = False

        for e in by_day[d]:
            if stopped:
                break
            if last_asset == e["asset"] and last_won is False:
                continue  # rotate off a losing chart -- skip until a different asset comes up

            won = e["won"]
            day_pnl += STAKE * PAYOUT if won else -STAKE
            day_trades += 1
            if won:
                day_wins += 1
            last_asset = e["asset"]
            last_won = won

            if day_pnl >= DAILY_TARGET or day_pnl <= DAILY_STOP:
                stopped = True

        day_results.append((d, day_trades, day_wins, day_pnl, stopped))
        total_trades += day_trades
        total_wins += day_wins
        total_pnl += day_pnl

    return day_results, total_trades, total_wins, total_pnl


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

        day_results, total_trades, total_wins, total_pnl = simulate(all_entries)

        print("--- per-day (chart-switch-on-loss, stop at +$4 / -$9) ---")
        for d, trades, wins, pnl, stopped in day_results:
            reason_str = "target/stop hit" if stopped else "ran out of signals"
            print(f"{d}  trades={trades:2d} wins={wins:2d} pnl=${pnl:+.2f}  ({reason_str})")

        print()
        print("=== SUMMARY ===")
        print(f"Days: {len(day_results)}")
        print(f"Total trades: {total_trades}")
        if total_trades:
            win_rate = total_wins / total_trades
            se = (win_rate * (1 - win_rate) / total_trades) ** 0.5
            lo, hi = max(0, win_rate - 1.96 * se), min(1, win_rate + 1.96 * se)
            print(f"Win rate: {total_wins}/{total_trades} = {win_rate:.1%} (95% CI {lo:.1%}-{hi:.1%})")
        print(f"Total P&L: ${total_pnl:+.2f}")
        print(f"Avg P&L/day: ${total_pnl/len(day_results):+.2f}")
        days_hit_target = sum(1 for _, _, _, pnl, s in day_results if s and pnl > 0)
        days_hit_stop = sum(1 for _, _, _, pnl, s in day_results if s and pnl < 0)
        print(f"Days hitting +$4 target: {days_hit_target}")
        print(f"Days hitting -$9 stop:   {days_hit_stop}")
    finally:
        await client.close()


asyncio.run(main())
