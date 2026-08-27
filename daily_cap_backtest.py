"""Same rule as prev_trigger_confirm.py (only enter a trigger if the
immediately preceding trigger on that asset resolved as a direct win),
but capped at the first N confirmed entries per calendar day (UTC),
across all scanned markets combined -- modeling "I only take 10 trades
a day." Read-only.
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
DAILY_TRADE_CAP = 10

BASE_STAKE = 1.0
MARTINGALE_STAKE = 2.0
PAYOUT = 0.80


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
    """Return list of {'time': int, 'type': str} for confirmed entries."""
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
        entries.append({"time": candles[i]["time"], "asset": asset, "type": cur_type})
    return entries


def day_key(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).date()


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

        all_entries.sort(key=lambda e: e["time"])

        # group by UTC day, cap first N per day
        by_day = {}
        for e in all_entries:
            by_day.setdefault(day_key(e["time"]), []).append(e)

        capped = []
        for d in sorted(by_day):
            capped.extend(by_day[d][:DAILY_TRADE_CAP])

        print(f"Total confirmed entries available (uncapped): {len(all_entries)}")
        print(f"Days covered: {len(by_day)}")
        print(f"Trades taken with {DAILY_TRADE_CAP}/day cap: {len(capped)}")
        print()

        direct = sum(1 for e in capped if e["type"] == "direct")
        mart = sum(1 for e in capped if e["type"] == "martingale")
        dl = sum(1 for e in capped if e["type"] == "double_loss")
        n = len(capped)

        print(f"Direct wins:      {direct}")
        print(f"Martingale wins:  {mart}")
        print(f"Double losses:    {dl}")
        if n:
            direct_rate = direct / n
            overall_rate = (direct + mart) / n
            se = (direct_rate * (1 - direct_rate) / n) ** 0.5
            lo, hi = max(0, direct_rate - 1.96 * se), min(1, direct_rate + 1.96 * se)
            print(f"Direct win rate:       {direct_rate:.1%}  (95% CI {lo:.1%}-{hi:.1%})")
            print(f"Overall win rate (incl martingale recovery): {overall_rate:.1%}")

            pnl = direct * (BASE_STAKE * PAYOUT) + mart * (-BASE_STAKE + MARTINGALE_STAKE * PAYOUT) + dl * -(BASE_STAKE + MARTINGALE_STAKE)
            print(f"Net P&L over {len(by_day)} days (${BASE_STAKE:.0f} base / ${MARTINGALE_STAKE:.0f} martingale stake): ${pnl:+.2f}")
            print(f"Avg P&L per trading day: ${pnl/len(by_day):+.2f}")

        print()
        print("--- per-day breakdown (first {} entries/day) ---".format(DAILY_TRADE_CAP))
        for d in sorted(by_day):
            day_entries = by_day[d][:DAILY_TRADE_CAP]
            dd = sum(1 for e in day_entries if e["type"] == "direct")
            dm = sum(1 for e in day_entries if e["type"] == "martingale")
            ddl = sum(1 for e in day_entries if e["type"] == "double_loss")
            print(f"{d}  available={len(by_day[d]):3d} taken={len(day_entries):2d} direct={dd} martingale={dm} double_loss={ddl}")
    finally:
        await client.close()


asyncio.run(main())
