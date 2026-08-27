"""One-off test: place a single $1 PUT trade on the DEMO account and
inspect exactly what buy() and check_win() return, so live_signal_bot.py's
auto-trade path can be trusted before real use.

DEMO account only. Places one real (demo) trade.
"""

import asyncio
import os

from dotenv import load_dotenv
from pyquotex.stable_api import Quotex
from pyquotex.utils.account_type import AccountType

load_dotenv()

PERIOD = 300


async def main():
    email = os.environ["QUOTEX_EMAIL"]
    password = os.environ["QUOTEX_PASSWORD"]

    client = Quotex(email=email, password=password, lang="en", root_path="data")
    check, reason = await client.connect()
    if not check:
        raise SystemExit(f"Connection failed: {reason}")

    try:
        await client.api.change_account(AccountType.DEMO)
        balance_before = await client.get_balance()
        print(f"DEMO balance before: {balance_before}")

        all_assets = client.get_all_asset_name() or []
        real_assets = [c for c, _n in all_assets if "_otc" not in c.lower()]
        asset = None
        for a in real_assets:
            try:
                _info, status = await client.check_asset_open(a)
                if status and status[2]:
                    asset = a
                    break
            except Exception:
                continue
        if not asset:
            print("No open real asset found.")
            return

        print(f"Placing Rs.100 PUT on {asset}, duration={PERIOD}s (TIMER mode) ...")
        success, event_data = await client.buy(100.0, asset, "put", PERIOD, time_mode="TIMER")
        print(f"buy() returned: success={success} event_data={event_data!r} type={type(event_data)}")

        if not success:
            print("Buy failed, stopping here.")
            return

        order_id = event_data.get("id") if isinstance(event_data, dict) else event_data
        print(f"order_id={order_id!r}. Waiting for result (up to ~{PERIOD + 30}s)...")

        win, profit = await client.check_win(order_id, duration=PERIOD)
        print(f"check_win() returned: win={win!r} profit={profit!r}")

        balance_after = await client.get_balance()
        print(f"DEMO balance after: {balance_after}  (delta={balance_after - balance_before:+.4f})")
    finally:
        await client.close()


asyncio.run(main())
