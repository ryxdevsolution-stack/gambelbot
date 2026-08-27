"""Live signal bot for the ORIGINAL pattern (2+ green candles, then a red
trigger, only taken if the PREVIOUS occurrence of this same trigger
resolved as a direct win) -- flat Rs.100 staking (no martingale, since testing
showed martingale is strictly worse), with a strict daily loss cap.

Sends a Telegram alert ~5 seconds BEFORE the trigger candle closes (i.e.
~5 seconds before the entry candle begins), so you have time to place the
PUT/DOWN trade yourself. By default this script NEVER places trades --
signal only. Set AUTO_TRADE_DEMO=1 to have it place the trade itself on
the DEMO account (never REAL -- that path does not exist in this script).

Run manually for a session (Ctrl+C to stop). It does not run unattended
or all day.

IMPORTANT, read before trusting this live: this exact pattern did NOT show
a statistically confirmed edge in backtesting (validation-set results were
flat/negative). This script is running it anyway as a live, closely
risk-capped experiment, per an explicit decision to do so -- not because
the numbers proved it works.
"""

import asyncio
import os
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from collections import deque

IST = timezone(timedelta(hours=5, minutes=30))

from dotenv import load_dotenv
from pyquotex.stable_api import Quotex
from pyquotex.utils.account_type import AccountType

load_dotenv()

PERIOD = 300  # 5-min
REAL_MARKET_COUNT = 10
DAILY_STOP = -500.0  # INR -- stop for the day once P&L hits this (flat Rs.100 stakes)
STAKE = 100.0  # INR -- account minimum trade size
PAYOUT = 0.80  # approximate, used only for the simulated (signal-only) P&L display
SIGNAL_LEAD_SECONDS = 5
HISTORY_SEED_HOURS = 24

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
DEBUG_HEARTBEAT = os.environ.get("DEBUG_HEARTBEAT", "0") == "1"

# Opt-in only. When off (default), this script only sends Telegram alerts
# and never touches the trading API. When on, it places real orders on the
# DEMO account -- there is intentionally no code path to the REAL account
# here; that would need a separate, explicit change.
AUTO_TRADE_DEMO = os.environ.get("AUTO_TRADE_DEMO", "0") == "1"


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10)
    except Exception as e:
        print(f"[telegram send failed] {e}")


def color(open_, close_):
    if close_ > open_:
        return "G"
    if close_ < open_:
        return "R"
    return "D"


def resolve(colors, i):
    if i + 1 >= len(colors):
        return None
    if colors[i + 1] == "R":
        return "direct"
    if colors[i + 1] == "G":
        if i + 2 >= len(colors):
            return None
        if colors[i + 2] == "R":
            return "martingale"
        if colors[i + 2] == "G":
            return "double_loss"
    return None


class AssetState:
    """Tracks closed-candle history and previous-trigger-confirmation
    state for one asset."""

    def __init__(self, asset):
        self.asset = asset
        self.colors = deque(maxlen=2000)  # closed-candle colors, oldest->newest
        self.last_closed_time = None
        self.last_prev_trigger_result = None  # outcome of most recent RESOLVED trigger
        self.pending_entry_start = None  # start time of the ENTRY candle we bet on, awaiting its close
        self.signaled_for_candle_start = None  # guards against duplicate alerts
        self.auto_traded_entry = False  # True if the pending entry has a real order (see reconcile_real_trade)
        self.auto_trade_pending = False  # True if the buy() call still needs to be placed at the entry candle's open
        # running tick-derived tracking for the candle currently forming
        self.tracking_candle_start = None
        self.tracking_open = None
        self.tracking_last = None

    def seed(self, historical_candles):
        historical_candles = sorted(historical_candles, key=lambda c: c["time"])
        for c in historical_candles:
            self.colors.append(color(c["open"], c["close"]))
        self.last_closed_time = historical_candles[-1]["time"] if historical_candles else None
        self._replay_trigger_history()

    def _replay_trigger_history(self):
        colors = list(self.colors)
        n = len(colors)
        triggers = []
        for i in range(2, n):
            if colors[i] != "R":
                continue
            streak = 0
            j = i - 1
            while j >= 0 and colors[j] == "G":
                streak += 1
                j -= 1
            if streak >= 2:
                triggers.append(i)
        last_result = None
        for i in triggers:
            outcome = resolve(colors, i)
            if outcome is not None:
                last_result = outcome
        self.last_prev_trigger_result = last_result

    def is_current_trigger(self, provisional_color):
        """Would the currently-forming candle (with this provisional
        color) be a valid trigger, i.e. 2+ green immediately before it,
        then this candle red?"""
        if provisional_color != "R":
            return False
        colors = list(self.colors)
        streak = 0
        j = len(colors) - 1
        while j >= 0 and colors[j] == "G":
            streak += 1
            j -= 1
        return streak >= 2

    def confirmed_by_prev_trigger(self):
        return self.last_prev_trigger_result == "direct"

    def on_candle_closed(self, closed_open, closed_close, closed_time):
        c = color(closed_open, closed_close)
        was_trigger = self.is_current_trigger(c) if c == "R" else False
        self.colors.append(c)
        self.last_closed_time = closed_time
        self._replay_trigger_history()
        return was_trigger


async def fetch_official_candle(client, asset, candle_time, attempts=4, delay=2.0):
    """Fetch the OFFICIAL server-recorded OHLC for a specific closed
    candle. Tick-derived open/close can misjudge a near-doji candle's
    color (confirmed live: a candle within 0.00001 of flat got read as
    green from ticks when the official record says red) -- this is the
    authoritative source used for pattern history instead.

    Retries a few times since the official record can lag a few seconds
    right at candle close."""
    for i in range(attempts):
        try:
            recent = await client.get_historical_candles(
                asset=asset, amount_of_seconds=PERIOD * 6, period=PERIOD, max_workers=1,
            )
            for c in recent:
                if c["time"] == candle_time:
                    return c["open"], c["close"]
        except Exception as e:
            print(f"[{asset}] fetch_official_candle error (attempt {i+1}): {e}")
        await asyncio.sleep(delay)
    return None, None


class DailyPnL:
    def __init__(self):
        self.day = None
        self.pnl = 0.0
        self.stopped = False

    def check_new_day(self):
        today = datetime.now(timezone.utc).date()
        if today != self.day:
            self.day = today
            self.pnl = 0.0
            self.stopped = False
            print(f"[{today}] New trading day, simulated P&L reset.")

    def record(self, won):
        self.record_amount(STAKE * PAYOUT if won else -STAKE)

    def record_amount(self, amount):
        self.check_new_day()
        self.pnl += amount
        if self.pnl <= DAILY_STOP and not self.stopped:
            self.stopped = True
            send_telegram(f"STOP for today. P&L hit ₹{self.pnl:+.2f} "
                           f"(limit ₹{DAILY_STOP:.2f}). No more signals until tomorrow.")
            print(f"[STOP] Daily loss cap hit: ₹{self.pnl:+.2f}")


async def monitor_asset(client, asset, state, pnl_tracker):
    await client.start_candles_stream(asset, PERIOD)
    print(f"[{asset}] streaming started")
    last_heartbeat = 0

    while True:
        if DEBUG_HEARTBEAT and time.time() - last_heartbeat >= 15:
            last_heartbeat = time.time()
            try:
                dbg_ticks = await client.get_realtime_price(asset)
            except Exception as e:
                dbg_ticks = []
                print(f"[{asset}] HEARTBEAT get_realtime_price error: {e}")
            cs = int(time.time() // PERIOD * PERIOD)
            rem = cs + PERIOD - time.time()
            n_this_candle = sum(1 for t in dbg_ticks if isinstance(t, dict) and t.get("time", 0) >= cs)
            print(f"[{asset}] HEARTBEAT remaining={rem:.0f}s total_ticks={len(dbg_ticks)} "
                  f"this_candle_ticks={n_this_candle} "
                  f"last_tick={dbg_ticks[-1] if dbg_ticks else None}")
        pnl_tracker.check_new_day()

        try:
            ticks = await client.get_realtime_price(asset)
        except Exception:
            ticks = []

        # Use the BROKER'S clock (from the latest price tick), not this
        # container's system clock -- avoids drift between the two, which
        # is what caused candle times to look wrong before.
        now = ticks[-1]["time"] if ticks and isinstance(ticks[-1], dict) and ticks[-1].get("time") else time.time()
        candle_start = int(now // PERIOD * PERIOD)
        candle_close = candle_start + PERIOD
        remaining = candle_close - now

        this_candle_ticks = [
            t for t in ticks
            if isinstance(t, dict) and t.get("time") is not None and t["time"] >= candle_start
        ]
        if this_candle_ticks:
            latest_price = this_candle_ticks[-1].get("price")
            open_price = this_candle_ticks[0].get("price")
        else:
            latest_price = open_price = None

        # 1. Detect a boundary crossing: the candle we were tracking has
        #    now closed. Finalize it using the OFFICIAL server-recorded
        #    OHLC (not tick-derived -- confirmed live that tick-derived
        #    open/close can misjudge a near-doji candle's color, which
        #    corrupts the pattern history it feeds into).
        if state.tracking_candle_start is not None and candle_start != state.tracking_candle_start:
            closed_start = state.tracking_candle_start
            was_entry = (state.pending_entry_start == closed_start)

            official_open, official_close = await fetch_official_candle(client, asset, closed_start)
            if official_open is None:
                print(f"[{asset}] WARNING: could not fetch official candle for {closed_start} "
                      f"after retries -- falling back to tick-derived data (less reliable).")
                official_open, official_close = state.tracking_open, state.tracking_last

            if official_open is not None and official_close is not None:
                state.on_candle_closed(official_open, official_close, closed_start)
                if DEBUG_HEARTBEAT:
                    print(f"[{asset}] DEBUG candle closed t={closed_start} "
                          f"open={official_open} close={official_close} "
                          f"color={color(official_open, official_close)} was_entry={was_entry}")
                if was_entry:
                    won = color(official_open, official_close) == "R"
                    if state.auto_traded_entry:
                        # real P&L comes from reconcile_real_trade() via
                        # check_win() instead -- skip the simulated record
                        # to avoid double-counting.
                        print(f"[{asset}] entry candle closed (official): "
                              f"{'WIN' if won else 'LOSS'} -- waiting on real order result")
                    else:
                        pnl_tracker.record(won)
                        print(f"[{asset}] entry candle closed: "
                              f"{'WIN' if won else 'LOSS'}  daily_pnl=₹{pnl_tracker.pnl:+.2f}")
                    state.pending_entry_start = None
                    state.auto_traded_entry = False
            else:
                print(f"[{asset}] WARNING: no data at all for candle {closed_start}, "
                      f"could not reconcile (pending_entry={state.pending_entry_start})")
            state.tracking_candle_start = None
            state.tracking_open = None
            state.tracking_last = None

        # 1b. If an auto-trade is pending for the candle that JUST started
        #     (candle_start == pending_entry_start), place it now -- right
        #     at the true open, so a TIMER-mode trade's window matches the
        #     entry candle exactly instead of starting ~5s early.
        if state.auto_trade_pending and state.pending_entry_start == candle_start:
            state.auto_trade_pending = False
            try:
                success, event_data = await client.buy(
                    STAKE, asset, "put", PERIOD, time_mode="TIMER"
                )
            except Exception as e:
                success, event_data = False, str(e)
            if success and isinstance(event_data, dict) and event_data.get("id"):
                order_id = event_data["id"]
                print(f"[{asset}] AUTO-TRADE placed at candle open, order_id={order_id}")
                asyncio.create_task(
                    reconcile_real_trade(client, asset, order_id, pnl_tracker)
                )
            else:
                print(f"[{asset}] AUTO-TRADE FAILED: {event_data}")
                send_telegram(f"WARNING: {asset} auto-trade failed to place: {event_data}")

        # start (or continue) tracking the current candle
        if open_price is not None and latest_price is not None:
            if state.tracking_candle_start != candle_start:
                state.tracking_candle_start = candle_start
                state.tracking_open = open_price
            state.tracking_last = latest_price

        # 2. Near the close of the CURRENT candle, evaluate the provisional
        #    color and fire a signal if it's a confirmed trigger.
        if (SIGNAL_LEAD_SECONDS - 1) <= remaining <= (SIGNAL_LEAD_SECONDS + 1):
            if state.signaled_for_candle_start != candle_start and not pnl_tracker.stopped:
                if open_price is not None and latest_price is not None:
                    provisional = color(open_price, latest_price)
                    if state.is_current_trigger(provisional) and state.confirmed_by_prev_trigger():
                        trigger_start_ist = datetime.fromtimestamp(candle_start, tz=IST).strftime("%H:%M")
                        entry_start_ist = datetime.fromtimestamp(candle_start + PERIOD, tz=IST).strftime("%H:%M")
                        send_telegram(
                            f"SIGNAL: {asset}  TRADE DOWN (PUT)\n"
                            f"Trigger candle: {trigger_start_ist} IST (closing now)\n"
                            f"Entry candle: {entry_start_ist} IST -- trade THIS one\n"
                            f"5-min expiry. Flat Rs.100 stake recommended.\n"
                            f"Daily P&L so far: ₹{pnl_tracker.pnl:+.2f}"
                        )
                        print(f"[{asset}] SIGNAL SENT (remaining={remaining:.1f}s)")
                        state.signaled_for_candle_start = candle_start
                        state.pending_entry_start = candle_start + PERIOD

                        if AUTO_TRADE_DEMO:
                            # Don't buy() yet -- that would start the TIMER
                            # ~5s before the entry candle actually begins,
                            # misaligning the trade window with the candle.
                            # Place it right when the entry candle starts
                            # instead (see boundary-crossing block below).
                            state.auto_traded_entry = True
                            state.auto_trade_pending = True

        await asyncio.sleep(1)


async def reconcile_real_trade(client, asset, order_id, pnl_tracker):
    win = profit = None
    try:
        win, profit = await client.check_win(order_id, duration=PERIOD)
    except Exception as e:
        print(f"[{asset}] check_win failed for order {order_id}: {e}")

    # Cross-check against trade history -- authoritative, and not subject
    # to the same event-timing race check_win()'s live wait can hit.
    try:
        status, item = await client.get_result(str(order_id))
        if status in ("win", "loss"):
            if win is not None and status != win:
                print(f"[{asset}] check_win said {win} but history says {status} "
                      f"for order {order_id} -- trusting history.")
            win = status
            if isinstance(item, dict) and "profitAmount" in item:
                profit = float(item["profitAmount"])
    except Exception as e:
        print(f"[{asset}] get_result cross-check failed for order {order_id}: {e}")

    if win not in ("win", "loss"):
        print(f"[{asset}] Could not determine result for order {order_id} -- NOT recorded to daily P&L.")
        send_telegram(f"WARNING: {asset} trade result unknown (order {order_id}). Check your app manually.")
        return

    if profit is not None and profit != 0:
        # trust the real figure from the platform (works whether a loss is
        # recorded as 0, or as a negative profitAmount).
        amount = profit if win == "win" else -abs(profit)
    else:
        amount = STAKE * PAYOUT if win == "win" else -STAKE

    pnl_tracker.record_amount(amount)
    print(f"[{asset}] REAL trade result: {win}  profit={profit}  amount=₹{amount:+.2f}  "
          f"daily_pnl=₹{pnl_tracker.pnl:+.2f}")


async def main():
    email = os.environ["QUOTEX_EMAIL"]
    password = os.environ["QUOTEX_PASSWORD"]

    client = Quotex(email=email, password=password, lang="en", root_path="data")
    check, reason = await client.connect()
    if not check:
        raise SystemExit(f"Connection failed: {reason}")

    if AUTO_TRADE_DEMO:
        await client.api.change_account(AccountType.DEMO)
        demo_balance = await client.get_balance()
        print(f"AUTO-TRADE ENABLED on DEMO account. Balance: {demo_balance}")

    print("Connected. Finding open real markets...")
    all_assets = client.get_all_asset_name() or []
    real_assets = [code for code, _name in all_assets if "_otc" not in code.lower()]
    seen = set()
    real_assets = [a for a in real_assets if not (a in seen or seen.add(a))]

    open_assets = []
    for asset in real_assets:
        try:
            _info, status = await client.check_asset_open(asset)
            is_open = bool(status[2]) if status else False
        except Exception:
            is_open = False
        if is_open:
            open_assets.append(asset)
        if len(open_assets) >= REAL_MARKET_COUNT:
            break

    mode_line = ("AUTO-TRADE ON (DEMO account) -- trades placed automatically."
                 if AUTO_TRADE_DEMO else
                 "Signal-only, you place trades manually.")
    print(f"Monitoring: {open_assets}")
    send_telegram(f"Signal bot started. Monitoring {len(open_assets)} markets: {', '.join(open_assets)}\n"
                  f"Daily stop: ₹{DAILY_STOP:.2f}. {mode_line}")

    pnl_tracker = DailyPnL()
    states = {}
    for asset in open_assets:
        state = AssetState(asset)
        try:
            hist = await client.get_historical_candles(
                asset=asset, amount_of_seconds=HISTORY_SEED_HOURS * 3600, period=PERIOD, max_workers=3,
            )
            state.seed(hist)
        except Exception as e:
            print(f"[{asset}] seed failed: {e}")
        states[asset] = state

    try:
        await asyncio.gather(*[
            monitor_asset(client, asset, states[asset], pnl_tracker) for asset in open_assets
        ])
    finally:
        await client.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped by user.")
