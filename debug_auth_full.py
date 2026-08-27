"""One-off diagnostic: instrument the full login -> websocket-authorization
chain to find exactly where and why it's failing.

Confirms/refutes:
  1. Does the HTTP login step succeed, and does the response carry
     Cloudflare bot-management markers (cf-ray, server: cloudflare,
     __cf_bm cookie)?
  2. Does a real (non-empty) SSID/token get produced from that login?
  3. What exact payload gets sent to the websocket 'authorization' event?
  4. What (if anything) comes back with the 'authorization/reject' event,
     and how long after the authorization frame was sent?

Not part of the bot. Read-only -- does not place trades.
"""

import asyncio
import os
import time

from dotenv import load_dotenv
from pyquotex.network.login import Login
from pyquotex.stable_api import Quotex
from pyquotex.utils.async_utils import EventDispatcher
from pyquotex.ws.channels.ssid import Ssid

load_dotenv()

_original_success_login = Login.success_login
_original_ssid_call = Ssid.__call__
_original_on_reject = EventDispatcher._on_reject
_original_on_auth = EventDispatcher._on_auth

_t_auth_sent = None


async def patched_success_login(self):
    if self.response is not None:
        r = self.response
        print("=== HTTP LOGIN RESPONSE ===")
        print("url:", r.url)
        print("status_code:", r.status_code)
        headers = dict(r.headers)
        cf_headers = {k: v for k, v in headers.items() if "cf-" in k.lower() or k.lower() == "server"}
        print("cloudflare-related headers:", cf_headers or "(none found)")
        set_cookie = headers.get("set-cookie", "")
        print("has __cf_bm in set-cookie:", "__cf_bm" in set_cookie)
        print("has cf_clearance in set-cookie:", "cf_clearance" in set_cookie)
        print("=== END HTTP LOGIN RESPONSE ===")
    return await _original_success_login(self)


async def patched_ssid_call(self, ssid):
    global _t_auth_sent
    masked = f"{ssid[:8]}...{ssid[-6:]}" if ssid and len(ssid) > 14 else repr(ssid)
    print(f"=== SENDING websocket authorization === ssid={masked} len={len(ssid) if ssid else 0}")
    _t_auth_sent = time.time()
    return await _original_ssid_call(self, ssid)


def patched_on_reject(self, data):
    elapsed = (time.time() - _t_auth_sent) if _t_auth_sent else None
    print(f"=== authorization/reject RECEIVED === raw_data={data!r} elapsed_since_auth_sent={elapsed}")
    return _original_on_reject(self, data)


def patched_on_auth(self, data):
    elapsed = (time.time() - _t_auth_sent) if _t_auth_sent else None
    print(f"=== s_authorization (SUCCESS) RECEIVED === raw_data={data!r} elapsed_since_auth_sent={elapsed}")
    return _original_on_auth(self, data)


Login.success_login = patched_success_login
Ssid.__call__ = patched_ssid_call
EventDispatcher._on_reject = patched_on_reject
EventDispatcher._on_auth = patched_on_auth


async def main():
    email = os.environ["QUOTEX_EMAIL"]
    password = os.environ["QUOTEX_PASSWORD"]

    client = Quotex(email=email, password=password, lang="en", root_path="data")
    check, reason = await client.connect()
    print()
    print("=== FINAL RESULT ===")
    print("status:", check, "reason:", reason)
    ssid_final = client.api.state.SSID if client.api else None
    print("final SSID present:", bool(ssid_final), "len:", len(ssid_final) if ssid_final else 0)
    await client.close()


asyncio.run(main())
