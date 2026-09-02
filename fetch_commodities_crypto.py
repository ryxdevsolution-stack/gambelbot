"""Fetch 8 weeks of candles (5m/15m/30m/1hr) for commodity and crypto
assets -- both live (real) and OTC variants -- caching each to
data/search_{ASSET}_{PERIOD}s.csv in the same format fetch_search_data.py
/ fetch_multi_tf.py use, so pattern_search.py / best_pattern_search.py /
artifact_check.py pick them up automatically.

Read-only. Does not place trades.
"""

import asyncio
import csv
import os
import re

from dotenv import load_dotenv
from pyquotex.stable_api import Quotex

load_dotenv()

HOURS = 1344  # 8 weeks
PERIODS = [300, 900, 1800, 3600]

COMMODITY_RE = re.compile(r'^(XAU|XAG|XPT|XPD|UKBRENT|USCRUDE|WTI|BRENT|NGAS|UKOIL|USOIL)', re.I)
CRYPTO_RE = re.compile(r'(BTC|ETH|LTC|XRP|BCH|BNB|ADA|SOL|DOT|DOGE|AVA|ATO|AXS|LNK|LINK|UNI|TRX|MATIC)', re.I)


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
        codes = [code for code, _name in all_assets if not (code in seen or seen.add(code))]

        commodities = [c for c in codes if COMMODITY_RE.match(c)]
        crypto = [c for c in codes if CRYPTO_RE.search(c)]
        wanted = sorted(set(commodities) | set(crypto))
        print(f"Commodity candidates: {commodities}", flush=True)
        print(f"Crypto candidates: {crypto}", flush=True)

        open_assets = []
        for asset in wanted:
            try:
                _info, status = await client.check_asset_open(asset)
                is_open = bool(status[2]) if status else False
            except Exception:
                is_open = False
            print(f"  {asset}: {'OPEN' if is_open else 'closed'}", flush=True)
            if is_open:
                open_assets.append(asset)

        print(f"\nOpen assets to fetch: {len(open_assets)} -> {open_assets}", flush=True)

        for period in PERIODS:
            for asset in open_assets:
                try:
                    candles = await client.get_historical_candles(
                        asset=asset, amount_of_seconds=HOURS * 3600, period=period, max_workers=3,
                    )
                except Exception as e:
                    print(f"{asset} @ {period}s: FAILED ({e})", flush=True)
                    continue

                candles = sorted(candles, key=lambda c: c["time"])
                path = f"data/search_{asset}_{period}s.csv"
                with open(path, "w", newline="") as f:
                    w = csv.writer(f)
                    w.writerow(["time", "open", "high", "low", "close"])
                    for c in candles:
                        w.writerow([c["time"], c["open"], c["high"], c["low"], c["close"]])
                print(f"{asset} @ {period}s: saved {len(candles)} candles -> {path}", flush=True)
    finally:
        await client.close()


asyncio.run(main())
