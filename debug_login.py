"""One-off diagnostic: monkeypatch success_login to dump the real HTML
Quotex returns after PIN submission, instead of the library's generic
'Unknown error' fallback. Not part of the bot."""

import asyncio
import os

from dotenv import load_dotenv
from pyquotex.network.login import Login
from pyquotex.stable_api import Quotex

load_dotenv()

_original_success_login = Login.success_login


async def patched_success_login(self):
    if self.response is not None:
        print("=== response url:", self.response.url)
        print("=== status_code:", self.response.status_code)
        with open("data/login_debug.html", "w") as f:
            f.write(self.response.text)
        print("=== saved body to data/login_debug.html, length:", len(self.response.text))
    return await _original_success_login(self)


Login.success_login = patched_success_login


async def main():
    email = os.environ["QUOTEX_EMAIL"]
    password = os.environ["QUOTEX_PASSWORD"]

    client = Quotex(email=email, password=password, lang="en", root_path="data")
    check, reason = await client.connect()
    print("status:", check, "reason:", reason)
    await client.close()


asyncio.run(main())
