"""Fetch historical 1-min candles from Quotex and save them as a CSV
in data/. Needs Python >=3.12 (pyquotex requirement) — run inside the
Docker image, not directly on the host:

    docker run --rm --env-file .env -v "$(pwd)/data:/app/data" quotex-bot \
        python3 fetch_data.py --asset EURUSD_otc --hours 72
"""

import argparse
import asyncio
import csv
import os
from pathlib import Path

from dotenv import load_dotenv
from pyquotex.stable_api import Quotex

load_dotenv()


async def fetch(asset, hours, period, max_workers):
    email = os.environ["QUOTEX_EMAIL"]
    password = os.environ["QUOTEX_PASSWORD"]

    client = Quotex(email=email, password=password, lang="en", root_path="data")
    check, reason = await client.connect()
    if not check:
        raise SystemExit(f"Connection failed: {reason}")

    try:
        client.set_account_mode("PRACTICE")
        return await client.get_historical_candles(
            asset=asset,
            amount_of_seconds=hours * 3600,
            period=period,
            max_workers=max_workers,
        )
    finally:
        await client.close()


def save_csv(candles, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["time", "open", "high", "low", "close"])
        writer.writeheader()
        for c in candles:
            writer.writerow({k: c[k] for k in ("time", "open", "high", "low", "close")})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--asset", default="EURUSD_otc")
    parser.add_argument("--hours", type=int, default=72)
    parser.add_argument("--period", type=int, default=60)
    parser.add_argument("--max-workers", type=int, default=3,
                         help="keep at 2-5: too many risks a Quotex ban (see pyquotex docs)")
    args = parser.parse_args()

    fetched = asyncio.run(fetch(args.asset, args.hours, args.period, args.max_workers))
    out_path = Path("data") / f"{args.asset}_{args.period}s.csv"
    save_csv(fetched, out_path)
    print(f"Saved {len(fetched)} candles to {out_path}")
