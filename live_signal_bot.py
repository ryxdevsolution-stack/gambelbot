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
import html
import json
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
EARLY_LEAD_SECONDS = 10  # can fire this early ONLY if the move already looks decisive -- see EARLY_CONFIRM_RATIO
EARLY_CONFIRM_RATIO = 0.7  # current down-move must be >= 70% of this asset's typical candle body to fire early
TYPICAL_RANGE_WINDOW = 20  # candles averaged to find "typical" body size
HISTORY_SEED_HOURS = 24
MAX_CONCURRENT_TRADES = 2  # across ALL assets combined -- signals still send, but auto-placement is skipped beyond this

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
DEBUG_HEARTBEAT = os.environ.get("DEBUG_HEARTBEAT", "0") == "1"

# Opt-in only. When off (default), this script only sends Telegram alerts
# and never touches the trading API. When on, it places real orders on the
# DEMO account -- there is intentionally no code path to the REAL account
# here; that would need a separate, explicit change.
AUTO_TRADE_DEMO = os.environ.get("AUTO_TRADE_DEMO", "0") == "1"

def emit(event_type, **fields):
    """Print a structured JSON event line (prefixed EVT::) alongside the
    normal human-readable print()s elsewhere in this file. A dashboard can
    parse these for a rich UI without touching this script's control flow
    -- purely additive, the trading logic itself is unchanged by this."""
    try:
        print("EVT::" + json.dumps({"type": event_type, "ts": time.time(), **fields}), flush=True)
    except (TypeError, ValueError):
        pass


def send_telegram(text):
    """HTML-formatted Telegram message. Any interpolated value that isn't
    a known-safe fixed string (asset codes, our own formatted numbers)
    must be passed through html.escape() first -- Telegram rejects the
    whole message if the HTML is malformed."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}).encode()
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
        self.candle_records = deque(maxlen=100)  # {time, open, close, color} for the UI
        self.last_closed_time = None
        self.last_prev_trigger_result = None  # outcome of most recent RESOLVED trigger
        self.last_prev_trigger_info = None  # {"trigger_time":, "outcome":} -- for UI "why confirmed" text
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
            cc = color(c["open"], c["close"])
            self.colors.append(cc)
            self.candle_records.append({"time": c["time"], "open": c["open"], "close": c["close"], "color": cc})
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
        last_index = None
        for i in triggers:
            outcome = resolve(colors, i)
            if outcome is not None:
                last_result = outcome
                last_index = i
        self.last_prev_trigger_result = last_result
        if last_index is not None and self.last_closed_time is not None:
            offset_from_end = (n - 1) - last_index
            trigger_time = self.last_closed_time - offset_from_end * PERIOD
            self.last_prev_trigger_info = {"trigger_time": trigger_time, "outcome": last_result}
        else:
            self.last_prev_trigger_info = None

    def typical_range(self, n=TYPICAL_RANGE_WINDOW):
        """Average |close-open| body size over the last n closed candles --
        the yardstick for judging whether an in-progress move is already
        decisive enough to call early (see EARLY_CONFIRM_RATIO)."""
        recs = list(self.candle_records)[-n:]
        if not recs:
            return None
        diffs = [abs(r["close"] - r["open"]) for r in recs]
        return sum(diffs) / len(diffs)

    def confirmation_reason(self):
        """Plain-language explanation of why the pattern is (or isn't)
        currently confirmed, for the dashboard."""
        info = self.last_prev_trigger_info
        if info is None:
            return "No prior 2-green-then-red trigger seen yet in history -- nothing to confirm against."
        t = datetime.fromtimestamp(info["trigger_time"], tz=IST).strftime("%H:%M")
        outcome = info["outcome"]
        if outcome == "direct":
            return f"Confirmed: last such trigger at {t} IST won directly (the very next candle was red)."
        if outcome == "martingale":
            return f"Not confirmed: last such trigger at {t} IST only won via martingale (2nd candle after was red, not the 1st)."
        if outcome == "double_loss":
            return f"Not confirmed: last such trigger at {t} IST lost twice in a row."
        return "Not confirmed."

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
        self.candle_records.append({
            "time": closed_time, "open": closed_open, "close": closed_close, "color": c,
        })
        self.last_closed_time = closed_time
        self._replay_trigger_history()
        return was_trigger

    def revise_candle(self, candle_time, new_open, new_close):
        """Correct a previously-recorded candle if Quotex's OFFICIAL value
        for it changed after settling -- confirmed live: get_historical_
        candles can return a still-provisional close right after a candle
        closes, then a different (final) value some time later.

        self.colors holds far more history (up to 2000 candles) than
        candle_records (capped at 100, for the UI) and isn't timestamped,
        so the matching position is located by its offset from the newest
        closed candle rather than rebuilt from candle_records -- rebuilding
        from the capped list would silently truncate the trigger-history
        window. Returns the new color if the value actually changed, else
        None."""
        new_c = color(new_open, new_close)
        changed = False
        for rec in self.candle_records:
            if rec["time"] == candle_time:
                if rec["color"] != new_c or rec["open"] != new_open or rec["close"] != new_close:
                    rec["open"], rec["close"], rec["color"] = new_open, new_close, new_c
                    changed = True
                break
        if not changed or self.last_closed_time is None:
            return None
        offset_from_end = (self.last_closed_time - candle_time) // PERIOD
        idx = len(self.colors) - 1 - offset_from_end
        if 0 <= idx < len(self.colors):
            colors_list = list(self.colors)
            colors_list[idx] = new_c
            self.colors = deque(colors_list, maxlen=self.colors.maxlen)
            self._replay_trigger_history()
        return new_c


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


REVALIDATE_DELAY = 90  # seconds -- give Quotex's feed time to settle before trusting a candle for good


async def revalidate_and_emit(client, asset, state, closed_start):
    """One-shot delayed re-check of a candle already recorded via
    fetch_official_candle(). If Quotex's value for it changed in the
    meantime (confirmed live -- see revise_candle()), correct our stored
    history and tell the dashboard. Runs as a background task so it never
    delays the main monitoring loop or trade placement."""
    await asyncio.sleep(REVALIDATE_DELAY)
    try:
        open_, close_ = await fetch_official_candle(client, asset, closed_start, attempts=2, delay=2.0)
    except Exception as e:
        print(f"[{asset}] revalidate error for {closed_start}: {e}")
        return
    if open_ is None:
        return
    new_color = state.revise_candle(closed_start, open_, close_)
    if new_color is not None:
        print(f"[{asset}] CORRECTED candle {closed_start}: Quotex revised it to "
              f"open={open_} close={close_} color={new_color} (differs from the value first read)")
        emit("candle_revised", asset=asset, time=closed_start, open=open_, close=close_,
             color=new_color, last_prev_trigger=state.last_prev_trigger_result,
             reason=state.confirmation_reason())


class DailyPnL:
    def __init__(self):
        self.day = None
        self.pnl = 0.0
        self.stopped = False
        self.open_trades = 0  # currently-open real orders, across all assets
        self.signals_sent = 0  # session totals -- do not reset daily, purely informational
        self.wins = 0
        self.losses = 0

    def can_open_trade(self):
        return self.open_trades < MAX_CONCURRENT_TRADES

    def trade_opened(self):
        self.open_trades += 1

    def trade_closed(self):
        self.open_trades = max(0, self.open_trades - 1)

    def record_signal(self):
        self.signals_sent += 1

    def check_new_day(self):
        today = datetime.now(timezone.utc).date()
        if today != self.day:
            self.day = today
            self.pnl = 0.0
            self.stopped = False
            print(f"[{today}] New trading day, simulated P&L reset.")
            emit("new_day", day=str(today))

    def record(self, won):
        self.record_amount(STAKE * PAYOUT if won else -STAKE, won)

    def record_amount(self, amount, won):
        self.check_new_day()
        self.pnl += amount
        if won:
            self.wins += 1
        else:
            self.losses += 1
        if self.pnl <= DAILY_STOP and not self.stopped:
            self.stopped = True
            send_telegram(
                f"🛑 <b>Daily stop hit</b>\n\n"
                f"📊 P&amp;L: <b>₹{self.pnl:+.2f}</b>\n"
                f"🚧 Limit: ₹{DAILY_STOP:.2f}\n\n"
                f"No more signals until tomorrow."
            )
            print(f"[STOP] Daily loss cap hit: ₹{self.pnl:+.2f}")
            emit("daily_stop", pnl=self.pnl)


async def monitor_asset(client, asset, state, pnl_tracker, trade_lock):
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

        # 1. TIME-CRITICAL, runs FIRST: if an auto-trade is pending for the
        #    candle that JUST started (candle_start == pending_entry_start),
        #    place it right now -- before anything slower below -- so a
        #    TIMER-mode trade's window matches the entry candle's true open
        #    as closely as possible. (This used to run AFTER the official-
        #    candle reconciliation below, which could delay buy() by up to
        #    ~8s on retries -- misaligning the trade window. Confirmed live:
        #    this delay is the likely cause of a case where the bot's own
        #    candle-color read said LOSS but the real platform result was
        #    WIN, i.e. the actual order caught a different price window
        #    than the candle it was compared against.)
        if state.auto_trade_pending and state.pending_entry_start == candle_start:
            state.auto_trade_pending = False
            # Serialize the whole check-then-buy sequence with trade_lock:
            # pyquotex's buy() uses UNKEYED shared state on the one client
            # connection (self.api.buy_id, self.api.slots.buy_confirm) --
            # confirmed by reading its source. Two buy() calls in flight at
            # once can each observe the OTHER's confirmation event, so both
            # "succeed" with the same order_id while only one real order
            # lands on the exchange (seen live: 3 assets signaled the same
            # candle, dashboard showed 3 trades placed with an IDENTICAL
            # order_id, Quotex showed only 1 real trade). The lock also
            # closes a race in can_open_trade()/trade_opened(): without it,
            # multiple assets could all pass the "under the cap" check
            # before any of them registered as open, letting more than
            # MAX_CONCURRENT_TRADES through.
            async with trade_lock:
                if not pnl_tracker.can_open_trade():
                    print(f"[{asset}] AUTO-TRADE SKIPPED: already {pnl_tracker.open_trades} "
                          f"trade(s) open (max {MAX_CONCURRENT_TRADES}).")
                    send_telegram(
                        f"⏭️ <b>{html.escape(asset)}</b> -- auto-trade skipped\n"
                        f"Already {MAX_CONCURRENT_TRADES} trades open (max reached)."
                    )
                    emit("trade_skipped", asset=asset, reason="max_concurrent", open_trades=pnl_tracker.open_trades)
                    state.auto_traded_entry = False
                else:
                    try:
                        success, event_data = await client.buy(
                            STAKE, asset, "put", PERIOD, time_mode="TIMER"
                        )
                    except Exception as e:
                        success, event_data = False, str(e)
                    if success and isinstance(event_data, dict) and event_data.get("id"):
                        order_id = event_data["id"]
                        pnl_tracker.trade_opened()
                        print(f"[{asset}] AUTO-TRADE placed at candle open, order_id={order_id} "
                              f"(open_trades={pnl_tracker.open_trades})")
                        emit("trade_placed", asset=asset, order_id=str(order_id), open_trades=pnl_tracker.open_trades)
                        asyncio.create_task(
                            reconcile_real_trade(client, asset, order_id, pnl_tracker)
                        )
                    else:
                        print(f"[{asset}] AUTO-TRADE FAILED: {event_data}")
                        send_telegram(
                            f"⚠️ <b>{html.escape(asset)} auto-trade FAILED</b>\n"
                            f"{html.escape(str(event_data))}"
                        )
                        emit("trade_failed", asset=asset, error=str(event_data))
                        # No real order exists -- clear the flag so the entry-candle
                        # close below falls through to the simulated P&L record
                        # instead of silently waiting forever on a result that will
                        # never arrive.
                        state.auto_traded_entry = False

        # 2. Detect a boundary crossing: the candle we were tracking has
        #    now closed. Finalize it using the OFFICIAL server-recorded
        #    OHLC (not tick-derived -- confirmed live that tick-derived
        #    open/close can misjudge a near-doji candle's color, which
        #    corrupts the pattern history it feeds into).
        if state.tracking_candle_start is not None and candle_start != state.tracking_candle_start:
            closed_start = state.tracking_candle_start
            was_entry = (state.pending_entry_start == closed_start)

            official_open, official_close = await fetch_official_candle(client, asset, closed_start)
            used_fallback = official_open is None
            if used_fallback:
                print(f"[{asset}] WARNING: could not fetch official candle for {closed_start} "
                      f"after retries -- falling back to tick-derived data (less reliable).")
                official_open, official_close = state.tracking_open, state.tracking_last

            if official_open is not None and official_close is not None:
                was_trigger = state.on_candle_closed(official_open, official_close, closed_start)
                closed_color = color(official_open, official_close)
                if DEBUG_HEARTBEAT:
                    print(f"[{asset}] DEBUG candle closed t={closed_start} "
                          f"open={official_open} close={official_close} "
                          f"color={closed_color} was_entry={was_entry}")
                emit("candle", asset=asset, time=closed_start, open=official_open, close=official_close,
                     color=closed_color, was_trigger=was_trigger, was_entry=was_entry,
                     used_fallback=used_fallback, last_prev_trigger=state.last_prev_trigger_result,
                     reason=state.confirmation_reason())
                # Quotex's own historical data isn't always final the
                # moment a candle closes (confirmed live -- see
                # revise_candle()); re-check once, later, and correct if
                # it changed. Background task, never blocks this loop.
                asyncio.create_task(revalidate_and_emit(client, asset, state, closed_start))
                if was_entry:
                    won = closed_color == "R"
                    if state.auto_traded_entry:
                        # real P&L comes from reconcile_real_trade() via
                        # check_win() instead -- skip the simulated record
                        # to avoid double-counting.
                        print(f"[{asset}] entry candle closed (official): "
                              f"{'WIN' if won else 'LOSS'} -- waiting on real order result")
                        emit("entry_closed", asset=asset, entry_time=closed_start, won=won, pending_real=True)
                    else:
                        pnl_tracker.record(won)
                        print(f"[{asset}] entry candle closed: "
                              f"{'WIN' if won else 'LOSS'}  daily_pnl=₹{pnl_tracker.pnl:+.2f}")
                        emit("result", asset=asset, entry_time=closed_start, won=won, mode="simulated",
                             amount=(STAKE * PAYOUT if won else -STAKE), daily_pnl=pnl_tracker.pnl,
                             wins=pnl_tracker.wins, losses=pnl_tracker.losses)
                    state.pending_entry_start = None
                    state.auto_traded_entry = False
            else:
                print(f"[{asset}] WARNING: no data at all for candle {closed_start}, "
                      f"could not reconcile (pending_entry={state.pending_entry_start})")
            state.tracking_candle_start = None
            state.tracking_open = None
            state.tracking_last = None

        # start (or continue) tracking the current candle
        if open_price is not None and latest_price is not None:
            if state.tracking_candle_start != candle_start:
                state.tracking_candle_start = candle_start
                state.tracking_open = open_price
            state.tracking_last = latest_price

        # Live snapshot for the dashboard -- the currently-forming candle's
        # provisional color/streak, independent of the signal-lead window
        # below (this is purely informational, doesn't affect trading).
        provisional_now = color(open_price, latest_price) if (open_price is not None and latest_price is not None) else None
        streak_now = 0
        for c in reversed(state.colors):
            if c == "G":
                streak_now += 1
            else:
                break
        is_trigger_now = state.is_current_trigger(provisional_now) if provisional_now else False
        confirmed_now = is_trigger_now and state.confirmed_by_prev_trigger()
        emit("tick", asset=asset, candle_start=candle_start, remaining=round(remaining, 1),
             open=open_price, last=latest_price, provisional=provisional_now, streak=streak_now,
             is_trigger=is_trigger_now, confirmed=confirmed_now,
             pending_entry_start=state.pending_entry_start, auto_traded_entry=state.auto_traded_entry)

        # 3. Fire a signal once the trigger is confirmed. Normally waits
        #    until SIGNAL_LEAD_SECONDS before close (fires regardless of
        #    how marginal the move is -- the safety net). If the move
        #    already looks decisive well before that -- at least
        #    EARLY_CONFIRM_RATIO of this asset's typical candle body size
        #    -- fire as early as EARLY_LEAD_SECONDS instead, for more
        #    reaction time. A reversal in the remaining seconds is still
        #    possible either way; this only changes WHEN we call it, not
        #    whether the candle can flip.
        if (SIGNAL_LEAD_SECONDS - 1) <= remaining <= (EARLY_LEAD_SECONDS + 1):
            if state.signaled_for_candle_start != candle_start and not pnl_tracker.stopped:
                if open_price is not None and latest_price is not None:
                    provisional = color(open_price, latest_price)
                    if state.is_current_trigger(provisional) and state.confirmed_by_prev_trigger():
                        in_normal_window = remaining <= (SIGNAL_LEAD_SECONDS + 1)
                        is_early = False
                        if not in_normal_window:
                            typical = state.typical_range()
                            move = open_price - latest_price  # positive = moved down (red-ward)
                            is_early = typical is not None and typical > 0 and move >= EARLY_CONFIRM_RATIO * typical
                        if in_normal_window or is_early:
                            trigger_start_ist = datetime.fromtimestamp(candle_start, tz=IST).strftime("%H:%M")
                            entry_start_ist = datetime.fromtimestamp(candle_start + PERIOD, tz=IST).strftime("%H:%M")
                            action_line = ("🤖 Auto-trading this one for you\n" if AUTO_TRADE_DEMO
                                            else "👉 Place this trade yourself\n")
                            early_line = "⚡ <b>Early call</b> -- move already decisive\n\n" if is_early else ""
                            closing_note = "closing soon" if is_early else "closing now"
                            send_telegram(
                                f"🔴 <b>SIGNAL: {html.escape(asset)}</b>\n"
                                f"📉 Direction: <b>DOWN (PUT)</b>\n\n"
                                f"{early_line}"
                                f"⏱️ Trigger candle: <b>{trigger_start_ist} IST</b> ({closing_note})\n"
                                f"🎯 Entry candle: <b>{entry_start_ist} IST</b> ← trade this one\n\n"
                                f"⏳ Expiry: 5 minutes\n"
                                f"💰 Stake: ₹{STAKE:.0f}\n"
                                f"{action_line}\n"
                                f"📊 Daily P&amp;L: ₹{pnl_tracker.pnl:+.2f}"
                            )
                            print(f"[{asset}] SIGNAL SENT (remaining={remaining:.1f}s"
                                  f"{' EARLY' if is_early else ''})")
                            pnl_tracker.record_signal()
                            emit("signal", asset=asset, trigger_time=candle_start, entry_time=candle_start + PERIOD,
                                 reason=state.confirmation_reason(), auto_trade=AUTO_TRADE_DEMO, early=is_early)
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
        pnl_tracker.trade_closed()
        print(f"[{asset}] Could not determine result for order {order_id} -- NOT recorded to daily P&L.")
        send_telegram(
            f"❓ <b>{html.escape(asset)} trade result unknown</b>\n"
            f"Order: {html.escape(str(order_id))}\n"
            f"Please check your app manually."
        )
        emit("result", asset=asset, order_id=str(order_id), won=None, mode="unknown",
             open_trades=pnl_tracker.open_trades)
        return

    won = win == "win"
    if profit is not None and profit != 0:
        # trust the real figure from the platform (works whether a loss is
        # recorded as 0, or as a negative profitAmount).
        amount = profit if won else -abs(profit)
    else:
        amount = STAKE * PAYOUT if won else -STAKE

    pnl_tracker.trade_closed()
    pnl_tracker.record_amount(amount, won)
    print(f"[{asset}] REAL trade result: {win}  profit={profit}  amount=₹{amount:+.2f}  "
          f"daily_pnl=₹{pnl_tracker.pnl:+.2f}  open_trades={pnl_tracker.open_trades}")
    emit("result", asset=asset, order_id=str(order_id), won=won, amount=amount, mode="real",
         daily_pnl=pnl_tracker.pnl, wins=pnl_tracker.wins, losses=pnl_tracker.losses,
         open_trades=pnl_tracker.open_trades)


async def otp_callback(message):
    """Called by pyquotex when Quotex needs an email PIN/2FA code. Emits a
    structured event so a UI can show an input box, then blocks (off the
    event loop, via a worker thread) on a plain input() -- in the terminal
    that's the real keyboard; behind the dashboard, dashboard_server.py
    pipes the code the user submits in the browser into this process's
    stdin, which input() then reads exactly the same way."""
    emit("pin_requested", message=message)
    loop = asyncio.get_running_loop()
    code = await loop.run_in_executor(None, input)
    emit("pin_submitted")
    return code


async def main():
    email = os.environ["QUOTEX_EMAIL"]
    password = os.environ["QUOTEX_PASSWORD"]

    client = Quotex(email=email, password=password, lang="en", root_path="data",
                     on_otp_callback=otp_callback)
    check, reason = await client.connect()
    if not check:
        emit("error", message=f"Connection failed: {reason}")
        raise SystemExit(f"Connection failed: {reason}")
    emit("connected")

    if AUTO_TRADE_DEMO:
        await client.api.change_account(AccountType.DEMO)
        demo_balance = await client.get_balance()
        print(f"AUTO-TRADE ENABLED on DEMO account. Balance: {demo_balance}")

    print("Connected. Finding open real markets...")
    all_assets = client.get_all_asset_name() or []
    real_assets = [code for code, _name in all_assets if "_otc" not in code.lower()]
    seen = set()
    real_assets = [a for a in real_assets if not (a in seen or seen.add(a))]
    print(f"[DEBUG] all_assets={len(all_assets)} real_assets={len(real_assets)}")

    open_assets = []
    for asset in real_assets:
        try:
            _info, status = await client.check_asset_open(asset)
            is_open = bool(status[2]) if status else False
            print(f"[DEBUG] {asset}: status={status} is_open={is_open}")
        except Exception as e:
            is_open = False
            print(f"[DEBUG] {asset}: check_asset_open raised {type(e).__name__}: {e}")
        if is_open:
            open_assets.append(asset)
        if len(open_assets) >= REAL_MARKET_COUNT:
            break

    mode_line = ("🤖 Auto-trade ON (DEMO account) -- trades placed automatically."
                 if AUTO_TRADE_DEMO else
                 "👀 Signal-only -- you place trades manually.")
    print(f"Monitoring: {open_assets}")
    send_telegram(
        f"✅ <b>Signal bot started</b>\n\n"
        f"📡 Monitoring {len(open_assets)} markets:\n"
        f"<code>{html.escape(', '.join(open_assets))}</code>\n\n"
        f"🛑 Daily stop: ₹{DAILY_STOP:.2f}\n"
        f"{mode_line}"
    )
    emit("monitoring", assets=open_assets, auto_trade=AUTO_TRADE_DEMO, daily_stop=DAILY_STOP,
         stake=STAKE, max_concurrent=MAX_CONCURRENT_TRADES)

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
        emit("seeded", asset=asset, candles=list(state.candle_records)[-30:],
             last_prev_trigger=state.last_prev_trigger_result)

    trade_lock = asyncio.Lock()  # serializes client.buy() calls -- see monitor_asset for why
    try:
        await asyncio.gather(*[
            monitor_asset(client, asset, states[asset], pnl_tracker, trade_lock) for asset in open_assets
        ])
    finally:
        await client.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped by user.")
