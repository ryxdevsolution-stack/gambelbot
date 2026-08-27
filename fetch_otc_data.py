"""Fetch 8 weeks of 5-min candles for the top OTC (broker-synthetic) open
markets, caching each to data/search_{ASSET}_300s.csv (same naming as the
real-market fetch -- asset names already contain "_otc" so they coexist).

Read-only. Does not place trades.
"""

import asyncio
import csv
import os

from dotenv import load_dotenv
from pyquotex.stable_api import Quotex

load_dotenv()

HOURS = 1344  # 8 weeks
MIN_OTC_MARKETS = 10
PERIOD = 300  # 5-min


async def main():
    email = os.environ["QUOTEX_EMAIL"]
    password = os.environ["QUOTEX_PASSWORD"]

    client = Quotex(email=email, password=password, lang="en", root_path="data")
    check, reason = await client.connect()
    if not check:
        raise SystemExit(f"Connection failed: {reason}")

    try:
        all_assets = client.get_all_asset_name() or []
        otc_assets = [code for code, _name in all_assets if "_otc" in code.lower()]
        seen = set()
        otc_assets = [a for a in otc_assets if not (a in seen or seen.add(a))]

        open_otc_assets = []
        for asset in otc_assets:
            try:
                _info, status = await client.check_asset_open(asset)
                is_open = bool(status[2]) if status else False
            except Exception:
                is_open = False
            if is_open:
                open_otc_assets.append(asset)
            if len(open_otc_assets) >= MIN_OTC_MARKETS:
                break

        print(f"Open OTC markets: {len(open_otc_assets)} -> {open_otc_assets}", flush=True)

        for asset in open_otc_assets:
            try:
                candles = await client.get_historical_candles(
                    asset=asset, amount_of_seconds=HOURS * 3600, period=PERIOD, max_workers=3,
                )
            except Exception as e:
                print(f"{asset}: FAILED ({e})", flush=True)
                continue

            candles = sorted(candles, key=lambda c: c["time"])
            path = f"data/search_{asset}_300s.csv"
            with open(path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["time", "open", "high", "low", "close"])
                for c in candles:
                    w.writerow([c["time"], c["open"], c["high"], c["low"], c["close"]])
            print(f"{asset}: saved {len(candles)} candles -> {path}", flush=True)
    finally:
        await client.close()


asyncio.run(main())
