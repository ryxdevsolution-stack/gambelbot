"""One-off: single login session, scan >=10 REAL (non-OTC) markets on both
1-min and 5-min candles for the last N hours, and report direct-win counts
for the '2+ green then red' pattern (win = the very next candle is also red).
Read-only — never places a trade. Not part of the bot."""

import asyncio
import os

from dotenv import load_dotenv
from pyquotex.stable_api import Quotex

load_dotenv()

HOURS = 2
MIN_REAL_MARKETS = 10
PERIODS = [60, 300]  # 1-min, 5-min


def candle_color(c):
    if c["close"] > c["open"]:
        return "G"
    if c["close"] < c["open"]:
        return "R"
    return "D"


def analyze(candles):
    candles = sorted(candles, key=lambda c: c["time"])
    colors = [candle_color(c) for c in candles]

    triggers = 0
    direct_wins = 0
    for i in range(2, len(colors)):
        if colors[i] != "R":
            continue
        streak = 0
        j = i - 1
        while j >= 0 and colors[j] == "G":
            streak += 1
            j -= 1
        if streak < 2:
            continue
        triggers += 1
        if i + 1 < len(colors) and colors[i + 1] == "R":
            direct_wins += 1
    return len(candles), triggers, direct_wins


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
        # dedupe, keep order
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

        results = []
        for asset in open_real_assets:
            for period in PERIODS:
                try:
                    candles = await client.get_historical_candles(
                        asset=asset,
                        amount_of_seconds=HOURS * 3600,
                        period=period,
                        max_workers=3,
                    )
                except Exception as e:
                    print(f"{asset} @ {period}s: FAILED ({e})")
                    continue
                n, triggers, wins = analyze(candles)
                label = "1min" if period == 60 else "5min"
                win_rate = f"{wins/triggers:.1%}" if triggers else "n/a"
                results.append((asset, label, n, triggers, wins, win_rate))
                print(f"{asset:12s} {label:5s} candles={n:4d} triggers={triggers:3d} direct_wins={wins:3d} win_rate={win_rate}")

        print()
        print("=== SUMMARY ===")
        tot_triggers_1m = sum(r[3] for r in results if r[1] == "1min")
        tot_wins_1m = sum(r[4] for r in results if r[1] == "1min")
        tot_triggers_5m = sum(r[3] for r in results if r[1] == "5min")
        tot_wins_5m = sum(r[4] for r in results if r[1] == "5min")
        if tot_triggers_1m:
            print(f"1min:  {tot_wins_1m}/{tot_triggers_1m} direct wins = {tot_wins_1m/tot_triggers_1m:.1%}")
        if tot_triggers_5m:
            print(f"5min:  {tot_wins_5m}/{tot_triggers_5m} direct wins = {tot_wins_5m/tot_triggers_5m:.1%}")
    finally:
        await client.close()


asyncio.run(main())
