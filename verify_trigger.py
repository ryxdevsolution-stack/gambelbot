"""One-off: fetch recent EURAUD 5-min candles and print times/colors around
a specific trigger timestamp, to verify the pattern that fired a real
signal. Read-only."""

import asyncio
import os
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
from pyquotex.stable_api import Quotex

load_dotenv()

IST = timezone(timedelta(hours=5, minutes=30))
TRIGGER_TIME = 1787830200  # from the log


def color(o, c):
    if c > o:
        return "G"
    if c < o:
        return "R"
    return "D"


async def main():
    email = os.environ["QUOTEX_EMAIL"]
    password = os.environ["QUOTEX_PASSWORD"]
    client = Quotex(email=email, password=password, lang="en", root_path="data")
    check, reason = await client.connect()
    if not check:
        raise SystemExit(f"Connection failed: {reason}")
    try:
        candles = await client.get_historical_candles(
            asset="EURAUD", amount_of_seconds=3600 * 2, period=300, max_workers=1,
        )
        candles = sorted(candles, key=lambda c: c["time"])
        for c in candles:
            if TRIGGER_TIME - 1800 <= c["time"] <= TRIGGER_TIME + 600:
                ist = datetime.fromtimestamp(c["time"], tz=IST).strftime("%H:%M")
                col = color(c["open"], c["close"])
                marker = " <-- TRIGGER" if c["time"] == TRIGGER_TIME else (
                    " <-- ENTRY (traded)" if c["time"] == TRIGGER_TIME + 300 else "")
                print(f"{ist} IST  t={c['time']}  open={c['open']}  close={c['close']}  color={col}{marker}")
    finally:
        await client.close()


asyncio.run(main())
