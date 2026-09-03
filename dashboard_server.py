"""Web dashboard to start/stop live_signal_bot.py and watch it live --
candle-by-candle pattern state, signals, trades, P&L, and PIN entry, all
from the browser.

Runs INSIDE the same container/environment as the bot (same installed
deps, same .env), and launches/stops it as a child subprocess. It never
reimplements bot logic -- live_signal_bot.py prints structured `EVT::
{json}` lines (alongside its normal human-readable prints) that this file
parses into a shared, thread-safe state object the UI polls.

    docker run --rm -it --env-file .env -p 127.0.0.1:8090:8090 -v "$(pwd):/app" quotex-bot python3 dashboard_server.py

Then open http://localhost:8090 -- PIN/2FA entry, if Quotex asks for one,
now happens ON THE PAGE (the bot's stdin is piped from this server, not
inherited from the terminal, so typing into the terminal no longer works
once this dashboard is in use).

No login is required to reach this page -- anyone who can reach the host
and port can start/stop the bot and place trades. Keep this bound to
localhost/a private network, or put it behind your own auth, if that's
not acceptable.
"""

import json
import os
import signal
import subprocess
import threading
import time
from collections import deque

from flask import Flask, jsonify, request, send_file

HERE = os.path.dirname(os.path.abspath(__file__))
BOT_SCRIPT = os.path.join(HERE, "live_signal_bot.py")
STOP_GRACE_SECONDS = 8
LOG_MAXLEN = 2000
EVENT_MAXLEN = 300
ASSET_LOG_MAXLEN = 60
ALLOWED_CANDLE_PERIODS = {60, 120, 180, 300, 600, 900, 1800, 3600}
DEFAULT_CANDLE_PERIOD = 300

app = Flask(__name__)


class BotProcess:
    def __init__(self):
        self.lock = threading.RLock()
        self.proc = None
        self.auto_trade = False
        self.candle_period = DEFAULT_CANDLE_PERIOD
        self.started_at = None
        self.log_lines = deque(maxlen=LOG_MAXLEN)
        self.log_total = 0
        self._reset_structured()

    def _reset_structured(self):
        self.phase = "idle"  # idle | connecting | awaiting_pin | running | stopped | error
        self.pin_message = None
        self.error = None
        self.monitored_assets = []
        self.stats = {"signals_sent": 0, "wins": 0, "losses": 0, "daily_pnl": 0.0,
                      "open_trades": 0, "stopped_for_day": False}
        self.assets = {}  # asset -> latest snapshot dict
        self.events = deque(maxlen=EVENT_MAXLEN)  # signals + results, newest first

    def is_running(self):
        with self.lock:
            return self.proc is not None and self.proc.poll() is None

    def start(self, auto_trade, candle_period):
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                return False, "Already running."
            if candle_period not in ALLOWED_CANDLE_PERIODS:
                return False, f"Invalid candle period: {candle_period}"
            env = os.environ.copy()
            env["AUTO_TRADE_DEMO"] = "1" if auto_trade else "0"
            env["CANDLE_PERIOD"] = str(candle_period)
            self.proc = subprocess.Popen(
                ["python3", "-u", BOT_SCRIPT],
                cwd=HERE,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            self.auto_trade = auto_trade
            self.candle_period = candle_period
            self.started_at = time.time()
            self.log_lines.clear()
            self.log_total = 0
            self._reset_structured()
            self.phase = "connecting"
            proc = self.proc
            self._append_log(f"--- started (auto_trade={auto_trade}) ---")
        threading.Thread(target=self._read_output, args=(proc,), daemon=True).start()
        return True, "Started."

    def _read_output(self, proc):
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line.startswith("EVT::"):
                self._handle_event(line[len("EVT::"):])
            else:
                self._append_log(line)
        with self.lock:
            if self.phase not in ("stopped",):
                self.phase = "stopped"
        self._append_log(f"--- process exited (code={proc.poll()}) ---")

    def _handle_event(self, raw):
        try:
            ev = json.loads(raw)
        except (ValueError, TypeError):
            return
        t = ev.get("type")
        with self.lock:
            if t == "pin_requested":
                self.phase = "awaiting_pin"
                self.pin_message = ev.get("message")
            elif t == "pin_submitted":
                self.pin_message = None
                self.phase = "connecting"
            elif t == "connected":
                self.phase = "connecting"  # still finding markets
            elif t == "error":
                self.phase = "error"
                self.error = ev.get("message")
            elif t == "monitoring":
                self.phase = "running"
                self.monitored_assets = ev.get("assets", [])
                self.auto_trade = bool(ev.get("auto_trade", self.auto_trade))
            elif t == "seeded":
                asset = ev.get("asset")
                a = self.assets.setdefault(asset, {"asset": asset})
                a["candles"] = ev.get("candles", [])
                a["last_prev_trigger"] = ev.get("last_prev_trigger")
            elif t == "tick":
                asset = ev.get("asset")
                a = self.assets.setdefault(asset, {"asset": asset})
                a.update({
                    "candle_start": ev.get("candle_start"),
                    "remaining": ev.get("remaining"),
                    "open": ev.get("open"),
                    "last": ev.get("last"),
                    "provisional": ev.get("provisional"),
                    "streak": ev.get("streak"),
                    "is_trigger": ev.get("is_trigger"),
                    "confirmed": ev.get("confirmed"),
                    "pending_entry_start": ev.get("pending_entry_start"),
                    "auto_traded_entry": ev.get("auto_traded_entry"),
                })
            elif t in ("candle", "candle_revised"):
                asset = ev.get("asset")
                a = self.assets.setdefault(asset, {"asset": asset})
                candles = a.get("candles", [])
                new_rec = {
                    "time": ev.get("time"), "open": ev.get("open"), "close": ev.get("close"),
                    "color": ev.get("color"), "was_trigger": ev.get("was_trigger"),
                    "was_entry": ev.get("was_entry"), "used_fallback": ev.get("used_fallback"),
                    "revised": (t == "candle_revised"),
                }
                # Replace any existing record for this candle time instead
                # of appending -- covers both the seed-vs-first-live-close
                # overlap and a later candle_revised correction.
                candles = [c for c in candles if c.get("time") != new_rec["time"]]
                candles.append(new_rec)
                candles.sort(key=lambda c: c["time"])
                a["candles"] = candles[-ASSET_LOG_MAXLEN:]
                a["last_prev_trigger"] = ev.get("last_prev_trigger")
                a["reason"] = ev.get("reason")
            elif t == "signal":
                self.events.appendleft({
                    "kind": "signal", "asset": ev.get("asset"), "trigger_time": ev.get("trigger_time"),
                    "entry_time": ev.get("entry_time"), "reason": ev.get("reason"),
                    "auto_trade": ev.get("auto_trade"), "early": ev.get("early"), "ts": ev.get("ts"),
                })
                self.stats["signals_sent"] += 1
            elif t == "trade_placed":
                self.events.appendleft({"kind": "trade_placed", "asset": ev.get("asset"),
                                         "order_id": ev.get("order_id"), "ts": ev.get("ts")})
                self.stats["open_trades"] = ev.get("open_trades", self.stats["open_trades"])
            elif t == "trade_failed":
                self.events.appendleft({"kind": "trade_failed", "asset": ev.get("asset"),
                                         "error": ev.get("error"), "ts": ev.get("ts")})
            elif t == "trade_skipped":
                self.events.appendleft({"kind": "trade_skipped", "asset": ev.get("asset"),
                                         "reason": ev.get("reason"), "ts": ev.get("ts")})
            elif t == "entry_closed":
                self.events.appendleft({"kind": "entry_closed", "asset": ev.get("asset"),
                                         "won": ev.get("won"), "pending_real": True, "ts": ev.get("ts")})
            elif t == "result":
                self.events.appendleft({
                    "kind": "result", "asset": ev.get("asset"), "won": ev.get("won"),
                    "amount": ev.get("amount"), "mode": ev.get("mode"), "ts": ev.get("ts"),
                })
                if "daily_pnl" in ev:
                    self.stats["daily_pnl"] = ev["daily_pnl"]
                if "wins" in ev:
                    self.stats["wins"] = ev["wins"]
                if "losses" in ev:
                    self.stats["losses"] = ev["losses"]
                if "open_trades" in ev:
                    self.stats["open_trades"] = ev["open_trades"]
            elif t == "daily_stop":
                self.stats["stopped_for_day"] = True
                self.stats["daily_pnl"] = ev.get("pnl", self.stats["daily_pnl"])
            elif t == "new_day":
                self.stats["stopped_for_day"] = False
                self.stats["daily_pnl"] = 0.0

    def _append_log(self, line):
        with self.lock:
            self.log_total += 1
            self.log_lines.append((self.log_total, line))

    def stop(self):
        with self.lock:
            proc = self.proc
        if proc is None or proc.poll() is not None:
            return False, "Not running."
        try:
            proc.send_signal(signal.SIGINT)  # same as Ctrl+C -- lets it shut down cleanly
        except ProcessLookupError:
            pass
        deadline = time.time() + STOP_GRACE_SECONDS
        while time.time() < deadline and proc.poll() is None:
            time.sleep(0.3)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        with self.lock:
            self.phase = "stopped"
        self._append_log("--- stopped from dashboard ---")
        return True, "Stopped."

    def submit_pin(self, code):
        with self.lock:
            proc = self.proc
            awaiting = self.phase == "awaiting_pin"
        if proc is None or proc.poll() is not None:
            return False, "Not running."
        if not awaiting:
            return False, "Not currently waiting for a PIN."
        try:
            proc.stdin.write(str(code).strip() + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, ValueError) as e:
            return False, f"Could not send PIN: {e}"
        return True, "PIN sent."

    def logs_since(self, idx):
        with self.lock:
            return [(i, l) for i, l in self.log_lines if i > idx]

    def state_snapshot(self):
        running = self.is_running()
        with self.lock:
            return {
                "running": running,
                "pid": self.proc.pid if self.proc else None,
                "auto_trade": self.auto_trade,
                "candle_period": self.candle_period,
                "started_at": self.started_at,
                "uptime_seconds": (time.time() - self.started_at) if (running and self.started_at) else None,
                "phase": self.phase,
                "pin_message": self.pin_message,
                "error": self.error,
                "monitored_assets": self.monitored_assets,
                "stats": dict(self.stats),
                "assets": list(self.assets.values()),
                "events": list(self.events)[:100],
                "log_total": self.log_total,
            }


bot = BotProcess()


@app.route("/")
def index():
    return send_file(os.path.join(HERE, "dashboard.html"))


@app.route("/api/start", methods=["POST"])
def api_start():
    body = request.get_json(silent=True) or {}
    auto_trade = bool(body.get("auto_trade", True))
    try:
        candle_period = int(body.get("candle_period", DEFAULT_CANDLE_PERIOD))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "Invalid candle_period."})
    ok, msg = bot.start(auto_trade, candle_period)
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    ok, msg = bot.stop()
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/pin", methods=["POST"])
def api_pin():
    body = request.get_json(silent=True) or {}
    code = body.get("code", "")
    ok, msg = bot.submit_pin(code)
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/state")
def api_state():
    return jsonify(bot.state_snapshot())


@app.route("/api/logs")
def api_logs():
    since = int(request.args.get("since", 0) or 0)
    lines = bot.logs_since(since)
    return jsonify({"lines": [{"i": i, "text": l} for i, l in lines]})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8090, threaded=True)
