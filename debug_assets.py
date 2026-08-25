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
        assets = client.get_all_asset_name()
        print("assets is None?", assets is None)
        if assets:
            print("total assets:", len(assets))
            print("sample:", assets[:10])
            real = [a for a in assets if "_otc" not in a[0].lower() and "_otc" not in a[1].lower()]
            print("real-looking count:", len(real))
            print("real sample:", real[:15])

        print("---check_asset_open EURUSD---")
        info = await client.check_asset_open("EURUSD")
        print(repr(info))
    finally:
        await client.close()


asyncio.run(main())
