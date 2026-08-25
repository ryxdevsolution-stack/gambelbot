"""Phase 1: replay strategy.py rules over historical candles.
No live/demo connection — reads a CSV from data/, reports win rate,
sample size, expected value, and max drawdown.

Usage:
    python3 backtest.py data/EURUSD_otc_60s.csv --payout 0.80
"""

import argparse
import csv
import math
from datetime import datetime, timezone

from strategy import MoneyManager, candle_color, confirmation_passed, pattern_at

DEFAULT_PAYOUT_RATE = 0.80  # set to match the payout Quotex actually shows for the asset
CONFIRMATION_WINDOW_SECONDS = 600  # 10 minutes


def load_candles(path):
    candles = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            candles.append({
                "time": int(float(row["time"])),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
            })
    candles.sort(key=lambda c: c["time"])
    return candles


def day_key(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).date()


def run_backtest(candles, payout_rate=DEFAULT_PAYOUT_RATE):
    mm = MoneyManager()
    pattern_log = []   # resolved pattern instances: {'time','outcome'}
    pending = {}        # trigger index -> green streak, awaiting resolution
    trades = []
    current_day = None
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0

    for i, c in enumerate(candles):
        d = day_key(c["time"])
        if d != current_day:
            current_day = d
            mm.new_day()

        if (i - 1) in pending:
            outcome_color = candle_color(c)
            outcome = "win" if outcome_color == "red" else (
                "loss" if outcome_color == "green" else "void"
            )
            pattern_log.append({"time": candles[i - 1]["time"], "outcome": outcome})
            del pending[i - 1]

        streak = pattern_at(candles, i)
        if streak is None:
            continue
        pending[i] = streak

        if not mm.can_trade():
            continue
        if not confirmation_passed(pattern_log, c["time"], CONFIRMATION_WINDOW_SECONDS):
            continue
        if i + 1 >= len(candles):
            continue

        won = candle_color(candles[i + 1]) == "red"
        stake = mm.stake
        mm.record_result(won, payout_rate)
        equity += stake * payout_rate if won else -stake
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
        trades.append({
            "time": c["time"], "stake": stake, "won": won,
            "streak": streak, "daily_pnl": mm.daily_pnl,
        })

    return trades, equity, max_drawdown


def summarize(trades, equity, max_drawdown):
    n = len(trades)
    if n == 0:
        print("No trades matched pattern + confirmation in this data.")
        return

    wins = sum(1 for t in trades if t["won"])
    win_rate = wins / n
    print(f"Trades taken:     {n}")
    print(f"Wins / Losses:    {wins} / {n - wins}")
    print(f"Win rate:         {win_rate:.1%}")
    print(f"Net P&L:          ${equity:+.2f}")
    print(f"Max drawdown:     ${max_drawdown:.2f}")

    if n > 1:
        se = math.sqrt(win_rate * (1 - win_rate) / n)
        lo, hi = win_rate - 1.96 * se, win_rate + 1.96 * se
        print(f"95% CI win rate:  {max(0, lo):.1%} - {min(1, hi):.1%}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path")
    parser.add_argument("--payout", type=float, default=DEFAULT_PAYOUT_RATE)
    args = parser.parse_args()

    all_candles = load_candles(args.csv_path)
    all_trades, final_equity, dd = run_backtest(all_candles, args.payout)
    summarize(all_trades, final_equity, dd)
