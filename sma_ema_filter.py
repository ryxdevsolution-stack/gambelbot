"""Add SMA(10)/EMA(10) confirmation to the user's pattern (2+ green, then
red trigger, only counted if the previous same trigger resolved as a
direct win). Additionally require the trigger candle's close to be BELOW
the SMA10 and/or EMA10 line -- checks if trading only "below the line"
signals improves the win rate.

Reports both the last 2 days (recent snapshot) and the full 8-week cached
window (statistically reliable read) -- short windows have repeatedly
looked better than they really are in this project, so both numbers are
shown together on purpose.

Pure offline analysis on cached CSVs. No login, no trading.
"""

import csv
import glob
import os
import sys
from datetime import datetime, timezone

DATA_DIR = "data"
PERIOD_SUFFIX = "300s"
RECENT_DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 2


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


def sma(closes, n):
    out = [None] * len(closes)
    s = 0.0
    for i, v in enumerate(closes):
        s += v
        if i >= n:
            s -= closes[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(closes, span):
    out = [None] * len(closes)
    k = 2 / (span + 1)
    e = None
    for i, v in enumerate(closes):
        e = v if e is None else v * k + e * (1 - k)
        out[i] = e
    return out


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


def find_confirmed_entries(rows):
    closes = [r["close"] for r in rows]
    colors = [color(r) for r in rows]
    n = len(colors)
    sma10 = sma(closes, 10)
    ema10 = ema(closes, 10)

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

    resolutions = [(i, resolve(colors, i)) for i in triggers]
    resolutions = [(i, t) for i, t in resolutions if t is not None]

    entries = []
    for k in range(1, len(resolutions)):
        prev_type = resolutions[k - 1][1]
        if prev_type != "direct":
            continue
        i, cur_type = resolutions[k]
        below_sma = sma10[i] is not None and closes[i] < sma10[i]
        below_ema = ema10[i] is not None and closes[i] < ema10[i]
        entries.append({
            "time": rows[i]["time"],
            "outcome": cur_type,
            "below_sma": below_sma,
            "below_ema": below_ema,
            "below_both": below_sma and below_ema,
        })
    return entries


def summarize(entries, label):
    n = len(entries)
    direct = sum(1 for e in entries if e["outcome"] == "direct")
    mart = sum(1 for e in entries if e["outcome"] == "martingale")
    dl = sum(1 for e in entries if e["outcome"] == "double_loss")
    if n == 0:
        print(f"{label:40s} n=0")
        return
    direct_rate = direct / n
    eventual = (direct + mart) / n
    se = (direct_rate * (1 - direct_rate) / n) ** 0.5
    lo, hi = max(0, direct_rate - 1.96 * se), min(1, direct_rate + 1.96 * se)
    print(f"{label:40s} n={n:4d}  direct={direct:4d} ({direct_rate:.1%}, CI[{lo:.1%}-{hi:.1%}])  "
          f"martingale={mart:3d}  double_loss={dl:3d}  eventual_win={eventual:.1%}")


def main():
    all_rows = load_asset_csvs()
    if not all_rows:
        print("No cached 5-min data found.")
        return

    all_entries = []
    for asset, rows in all_rows.items():
        all_entries.extend(find_confirmed_entries(rows))

    max_time = max(r["time"] for rows in all_rows.values() for r in rows)
    recent_cutoff = max_time - RECENT_DAYS * 86400

    for window_name, entries in [
        (f"LAST {RECENT_DAYS} DAYS", [e for e in all_entries if e["time"] >= recent_cutoff]),
        ("FULL 8-WEEK WINDOW", all_entries),
    ]:
        print(f"\n=== {window_name} ===")
        summarize(entries, "Baseline (no SMA/EMA filter)")
        summarize([e for e in entries if e["below_sma"]], "Trigger close BELOW SMA(10)")
        summarize([e for e in entries if e["below_ema"]], "Trigger close BELOW EMA(10)")
        summarize([e for e in entries if e["below_both"]], "Trigger close BELOW SMA(10) AND EMA(10)")
        summarize([e for e in entries if not e["below_sma"]], "Trigger close ABOVE SMA(10) (for contrast)")
        summarize([e for e in entries if not e["below_ema"]], "Trigger close ABOVE EMA(10) (for contrast)")


if __name__ == "__main__":
    main()
