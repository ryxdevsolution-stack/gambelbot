"""Support/Resistance bounce pattern backtest, 5-min candles, last N days.

Definition (causal, no lookahead):
  - Support[i]    = lowest LOW over the K candles before i (not including i)
  - Resistance[i] = highest HIGH over the K candles before i (not including i)
  - Bullish bounce at i: candle i's LOW comes within TOL*ATR of Support[i]
    (touches/tests it), but candle i closes GREEN and back above Support[i]
    (rejection). Bet UP -- win if candle i+1 is also green.
  - Bearish bounce at i: candle i's HIGH comes within TOL*ATR of
    Resistance[i], but candle i closes RED and back below Resistance[i]
    (rejection). Bet DOWN -- win if candle i+1 is also red.

Runs over the LAST N days of cached data only (recent slice, not the full
8-week window) -- pure offline analysis on cached CSVs, no login needed.
"""

import csv
import glob
import os
import sys
from datetime import datetime, timezone, timedelta

DATA_DIR = "data"
PERIOD_SUFFIX = "300s"
DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 3
LOOKBACKS = [10, 20, 50]  # candles used to define S/R (~50min, ~1.7h, ~4.2h on 5-min bars)
TOLERANCES = [0.3, 0.5]   # x ATR "near enough to count as touching" the level


def load_asset_csvs():
    assets = {}
    for path in sorted(glob.glob(f"{DATA_DIR}/search_*_{PERIOD_SUFFIX}.csv")):
        base = os.path.basename(path)
        asset = base[len("search_"):-len(f"_{PERIOD_SUFFIX}.csv")]
        rows = []
        with open(path) as f:
            for row in csv.DictReader(f):
                rows.append({
                    "time": int(row["time"]),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                })
        rows.sort(key=lambda c: c["time"])
        if rows:
            assets[asset] = rows
    return assets


def color(c):
    if c["close"] > c["open"]:
        return "G"
    if c["close"] < c["open"]:
        return "R"
    return "D"


def compute_atr(rows, period=14):
    n = len(rows)
    closes = [r["close"] for r in rows]
    tr = [0.0] * n
    for i in range(n):
        hi, lo = rows[i]["high"], rows[i]["low"]
        prev_close = closes[i - 1] if i > 0 else rows[i]["open"]
        tr[i] = max(hi - lo, abs(hi - prev_close), abs(lo - prev_close))
    atr = [None] * n
    a = None
    for i in range(n):
        if i < period:
            continue
        a = sum(tr[1:period + 1]) / period if i == period else (a * (period - 1) + tr[i]) / period
        atr[i] = a
    return atr


def ci(wins, n):
    if n == 0:
        return None
    p = wins / n
    se = (p * (1 - p) / n) ** 0.5
    return p, max(0, p - 1.96 * se), min(1, p + 1.96 * se)


def fmt_ci(n, w):
    result = ci(w, n)
    if not result:
        return f"n={n}"
    p, lo, hi = result
    return f"n={n} win={w} rate={p:.1%} CI[{lo:.1%}-{hi:.1%}]"


def find_bounces(rows, lookback, tol):
    n = len(rows)
    colors = [color(r) for r in rows]
    atr = compute_atr(rows)
    events = []
    for i in range(max(lookback, 25), n - 1):
        window = rows[i - lookback:i]
        support = min(r["low"] for r in window)
        resistance = max(r["high"] for r in window)
        a = atr[i]
        if not a:
            continue

        # bullish bounce off support
        if (rows[i]["low"] <= support + tol * a and colors[i] == "G"
                and rows[i]["close"] > support):
            win = colors[i + 1] == "G"
            events.append(("support", i, rows[i]["time"], win))

        # bearish bounce off resistance
        if (rows[i]["high"] >= resistance - tol * a and colors[i] == "R"
                and rows[i]["close"] < resistance):
            win = colors[i + 1] == "R"
            events.append(("resistance", i, rows[i]["time"], win))
    return events


def main():
    all_rows = load_asset_csvs()
    if not all_rows:
        print("No cached 5-min data found.")
        return

    max_time = max(r["time"] for rows in all_rows.values() for r in rows)
    cutoff = max_time - DAYS * 86400
    print(f"Last {DAYS} days: from {datetime.fromtimestamp(cutoff, tz=timezone.utc)} "
          f"to {datetime.fromtimestamp(max_time, tz=timezone.utc)}")
    print(f"Assets: {list(all_rows.keys())}\n")

    print("=== Support/Resistance bounce, last N days, all lookback/tolerance combos ===")
    results = []
    for lookback in LOOKBACKS:
        for tol in TOLERANCES:
            total_n = total_w = 0
            sup_n = sup_w = 0
            res_n = res_w = 0
            for asset, rows in all_rows.items():
                events = find_bounces(rows, lookback, tol)
                for kind, i, t, win in events:
                    if t < cutoff:
                        continue
                    total_n += 1
                    total_w += 1 if win else 0
                    if kind == "support":
                        sup_n += 1
                        sup_w += 1 if win else 0
                    else:
                        res_n += 1
                        res_w += 1 if win else 0
            results.append((lookback, tol, total_n, total_w, sup_n, sup_w, res_n, res_w))
            print(f"lookback={lookback:2d} tol={tol}xATR  ALL {fmt_ci(total_n, total_w)}  "
                  f"| support {fmt_ci(sup_n, sup_w)}  | resistance {fmt_ci(res_n, res_w)}")

    print("\n=== Same pattern over the FULL 8-week cached window (for comparison) ===")
    for lookback in LOOKBACKS:
        for tol in TOLERANCES:
            total_n = total_w = 0
            for asset, rows in all_rows.items():
                events = find_bounces(rows, lookback, tol)
                for kind, i, t, win in events:
                    total_n += 1
                    total_w += 1 if win else 0
            print(f"lookback={lookback:2d} tol={tol}xATR  ALL {fmt_ci(total_n, total_w)}")


if __name__ == "__main__":
    main()
