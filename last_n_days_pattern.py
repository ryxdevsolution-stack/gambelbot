"""Count direct-profit trades for the user's exact pattern (2+ green candles,
then 1 red trigger, only taken if the PREVIOUS occurrence of this same
trigger resolved as a direct win) over the last N days of cached 5-min data.

Pure offline analysis on already-cached CSVs. No login, no trading.
"""

import csv
import glob
import os
import sys
from datetime import datetime, timezone

DATA_DIR = "data"
PERIOD_SUFFIX = "300s"
DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 2


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


def resolve(colors, i):
    """direct win / martingale loss / double loss, for a down (PUT) trigger."""
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
    """2+ green then red trigger, only counted if the PREVIOUS such trigger
    on this asset resolved as a direct win."""
    colors = [color(r) for r in rows]
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

    resolutions = [(i, resolve(colors, i)) for i in triggers]
    resolutions = [(i, t) for i, t in resolutions if t is not None]

    entries = []
    for k in range(1, len(resolutions)):
        prev_type = resolutions[k - 1][1]
        if prev_type != "direct":
            continue
        i, cur_type = resolutions[k]
        entries.append({"time": rows[i]["time"], "outcome": cur_type})
    return entries


def main():
    all_rows = load_asset_csvs()
    if not all_rows:
        print("No cached 5-min data found.")
        return

    max_time = max(r["time"] for rows in all_rows.values() for r in rows)
    cutoff = max_time - DAYS * 86400
    print(f"Last {DAYS} day(s): {datetime.fromtimestamp(cutoff, tz=timezone.utc)} -> "
          f"{datetime.fromtimestamp(max_time, tz=timezone.utc)}")
    print(f"Assets: {list(all_rows.keys())}\n")

    total = {"direct": 0, "martingale": 0, "double_loss": 0}
    per_asset = {}
    log = []

    for asset, rows in all_rows.items():
        entries = find_confirmed_entries(rows)
        recent = [e for e in entries if e["time"] >= cutoff]
        per_asset[asset] = {"direct": 0, "martingale": 0, "double_loss": 0}
        for e in recent:
            total[e["outcome"]] += 1
            per_asset[asset][e["outcome"]] += 1
            log.append((e["time"], asset, e["outcome"]))

    log.sort()
    print("=== Confirmed-entry log (chronological) ===")
    for t, asset, outcome in log:
        ts = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%m-%d %H:%M UTC")
        print(f"{ts}  {asset:8s}  {outcome}")

    print("\n=== Per-asset breakdown ===")
    for asset, d in per_asset.items():
        n = d["direct"] + d["martingale"] + d["double_loss"]
        if n == 0:
            continue
        print(f"{asset:8s} entries={n:3d}  direct={d['direct']:3d}  "
              f"martingale={d['martingale']:3d}  double_loss={d['double_loss']:3d}")

    n = total["direct"] + total["martingale"] + total["double_loss"]
    print(f"\n=== TOTAL, last {DAYS} day(s), all 10 markets ===")
    print(f"Confirmed entries: {n}")
    print(f"Direct profit (won on first bet):     {total['direct']}")
    print(f"Martingale needed (won on 2nd bet):    {total['martingale']}")
    print(f"Double loss (lost both bets):          {total['double_loss']}")
    if n:
        direct_rate = total["direct"] / n
        eventual_wins = total["direct"] + total["martingale"]
        print(f"\nDirect-profit rate: {total['direct']}/{n} = {direct_rate:.1%}")
        print(f"Eventual win rate (direct + martingale recovery): {eventual_wins}/{n} = {eventual_wins/n:.1%}")


if __name__ == "__main__":
    main()
