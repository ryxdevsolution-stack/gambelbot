"""One-off: print current live payout percentages for every open asset,
sorted highest first, so we know which assets currently clear a chosen
payout threshold (e.g. >=90%). Payout changes live and isn't stored in
historical candle data, so this can't be backtested -- it's a real-time
filter, checked at signal time, not something to search history for.

Read-only. Does not place trades.
"""

import asyncio
import os

from dotenv import load_dotenv
from pyquotex.stable_api import Quotex

load_dotenv()


async def main():
    email = os.environ["QUOTEX_EMAIL"]
    password = os.environ["QUOTEX_PASSWORD"]

    client = Quotex(email=email, password=password, lang="en", root_path="data")
    check, reason = await client.connect()
    if not check:
        raise SystemExit(f"Connection failed: {reason}")

    try:
        await client.get_instruments()
        payments = client.get_payment()

        rows = []
        for asset, info in payments.items():
            if not info.get("open"):
                continue
            rows.append((asset, info.get("payment"), info.get("turbo_payment"), info.get("profit")))

        rows.sort(key=lambda r: (r[1] or 0), reverse=True)
        print(f"{'asset':16s} {'payment%':>9s} {'turbo%':>8s}  profit(1M/5M)")
        for asset, payment, turbo, profit in rows:
            p1 = profit.get("1M") if profit else None
            p5 = profit.get("5M") if profit else None
            print(f"{asset:16s} {str(payment):>9s} {str(turbo):>8s}  {p1}/{p5}")
    finally:
        await client.close()


asyncio.run(main())
