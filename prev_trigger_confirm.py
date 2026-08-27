"""Manual-rule backtest: '2+ green then red' pattern, but only take the
CURRENT trigger if the immediately PRECEDING trigger resolved as a direct
win (streak G's -> R -> another R). If the preceding trigger needed a
martingale recovery (G-streak -> R -> G -> R) or double-lost (-> G -> G),
skip the current trigger. This mirrors the user's manual chart-reading
method exactly (not the 10-min time-window confirmation in strategy.py).

Read-only. Scans >=N real (non-OTC) open markets on 5-min candles over
a configurable window in one login session.
"""

import asyncio
import os

from dotenv import load_dotenv
from pyquotex.stable_api import Quotex

load_dotenv()

HOURS = 168  # 1 week, so enough 5-min triggers accumulate
MIN_REAL_MARKETS = 10
PERIOD = 300  # 5-min


def candle_color(c):
    if c["close"] > c["open"]:
        return "G"
    if c["close"] < c["open"]:
        return "R"
    return "D"


def find_triggers(colors):
    """Return list of trigger indices: green streak >=2 immediately
    followed by red at that index."""
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
            triggers.append((i, streak))
    return triggers


def resolve(colors, i):
    """Classify how trigger at index i resolved."""
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
    return None  # doji


def analyze(candles):
    candles = sorted(candles, key=lambda c: c["time"])
    colors = [candle_color(c) for c in candles]
    triggers = find_triggers(colors)

    resolutions = [(i, streak, resolve(colors, i)) for i, streak in triggers]
    resolutions = [r for r in resolutions if r[2] is not None]

    confirmed = 0
    direct_wins = 0
    martingale_wins = 0
    double_losses = 0

    for k in range(1, len(resolutions)):
        prev_type = resolutions[k - 1][2]
        if prev_type != "direct":
            continue
        confirmed += 1
        cur_type = resolutions[k][2]
        if cur_type == "direct":
            direct_wins += 1
        elif cur_type == "martingale":
            martingale_wins += 1
        else:
            double_losses += 1

    return {
        "candles": len(candles),
        "total_triggers": len(resolutions),
        "confirmed_entries": confirmed,
        "direct_wins": direct_wins,
        "martingale_wins": martingale_wins,
        "double_losses": double_losses,
    }


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

        tot_confirmed = 0
        tot_direct = 0
        tot_mart = 0
        tot_dl = 0

        for asset in open_real_assets:
            try:
                candles = await client.get_historical_candles(
                    asset=asset,
                    amount_of_seconds=HOURS * 3600,
                    period=PERIOD,
                    max_workers=3,
                )
            except Exception as e:
                print(f"{asset}: FAILED ({e})")
                continue
            r = analyze(candles)
            tot_confirmed += r["confirmed_entries"]
            tot_direct += r["direct_wins"]
            tot_mart += r["martingale_wins"]
            tot_dl += r["double_losses"]
            print(
                f"{asset:10s} candles={r['candles']:4d} triggers={r['total_triggers']:3d} "
                f"confirmed_entries={r['confirmed_entries']:3d} "
                f"direct={r['direct_wins']:3d} martingale={r['martingale_wins']:3d} "
                f"double_loss={r['double_losses']:3d}"
            )

        print()
        print("=== SUMMARY (5-min candles, prior-trigger confirmation rule) ===")
        print(f"Confirmed entries taken: {tot_confirmed}")
        if tot_confirmed:
            direct_rate = tot_direct / tot_confirmed
            overall_rate = (tot_direct + tot_mart) / tot_confirmed
            print(f"Direct win rate (no martingale needed): {tot_direct}/{tot_confirmed} = {direct_rate:.1%}")
            print(f"Overall win rate (incl. martingale recovery): {tot_direct + tot_mart}/{tot_confirmed} = {overall_rate:.1%}")
            print(f"Double losses (both legs lost): {tot_dl}/{tot_confirmed} = {tot_dl/tot_confirmed:.1%}")
    finally:
        await client.close()


asyncio.run(main())
