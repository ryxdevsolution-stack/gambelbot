"""One-off diagnostic: log every raw websocket message received after
authentication, then place a single $1 PUT on DEMO, to see exactly what
(if anything) the server sends back for an order request.

DEMO account only. Not part of the bot.
"""

import asyncio
import os

from dotenv import load_dotenv
from pyquotex.api import QuotexAPI
from pyquotex.stable_api import Quotex
from pyquotex.utils.account_type import AccountType

load_dotenv()

PERIOD = 300

_original_on_message = QuotexAPI._on_message


async def patched_on_message(self, msg):
    msg_str = msg.decode("utf-8", errors="ignore") if isinstance(msg, bytes) else str(msg)
    if self.state.auth_status.name == "AUTHENTICATED":
        print(f"[RAW MSG] {msg_str[:500]}")
    return await _original_on_message(self, msg)


QuotexAPI._on_message = patched_on_message


async def main():
    email = os.environ["QUOTEX_EMAIL"]
    password = os.environ["QUOTEX_PASSWORD"]

    client = Quotex(email=email, password=password, lang="en", root_path="data")
    check, reason = await client.connect()
    if not check:
        raise SystemExit(f"Connection failed: {reason}")

    try:
        await client.api.change_account(AccountType.DEMO)
        print("=== Switched to DEMO ===")

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

        print(f"=== Placing Rs.100 PUT on {asset}, duration={PERIOD}s ===")
        success, event_data = await client.buy(100.0, asset, "put", PERIOD)
        print(f"=== buy() returned: success={success} event_data={event_data!r} ===")
    finally:
        await client.close()


asyncio.run(main())
