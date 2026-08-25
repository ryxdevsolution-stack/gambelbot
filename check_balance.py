"""Read-only: print PRACTICE (demo) and REAL account balances.
Never places a trade. Not part of the bot."""

import asyncio
import os

from dotenv import load_dotenv
from pyquotex.stable_api import Quotex
from pyquotex.utils.account_type import AccountType

load_dotenv()


async def main():
    email = os.environ["QUOTEX_EMAIL"]
    password = os.environ["QUOTEX_PASSWORD"]

    client = Quotex(email=email, password=password, lang="en", root_path="data")
    check, reason = await client.connect()
    if not check:
        raise SystemExit(f"Connection failed: {reason}")

    try:
        # set_account_mode() alone doesn't sync to the balance lookup —
        # change_account() is what actually flips it server-side.
        await client.api.change_account(AccountType.DEMO)
        demo_balance = await client.get_balance()
        print(f"PRACTICE (demo) balance: {demo_balance}")

        await client.api.change_account(AccountType.REAL)
        real_balance = await client.get_balance()
        print(f"REAL (live) balance: {real_balance}")
    finally:
        await client.close()


asyncio.run(main())
