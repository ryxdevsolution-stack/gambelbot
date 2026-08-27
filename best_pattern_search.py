"""Find the best end-of-day-profit pattern on real 5-min markets, testing
BOTH rule families with BOTH staking plans, optimized directly for $/day
(not win rate), with train/validation discipline.

Rule families:
  A) CONTINUATION (the original pattern): streak of N same-color candles,
     then a break candle of the opposite color -- bet the break continues.
  B) REVERSAL (new, motivated by a confirmed statistical effect: real
     markets show a small but significant tendency to reverse after a
     same-color run, not continue it): streak of N same-color candles --
     bet the NEXT candle reverses color.

Staking plans:
  - FLAT: $1 every trade, no martingale.
  - MARTINGALE: $1 base, $2 recovery bet after a loss, resets after a win
    or after 2 straight losses (same as strategy.py's MoneyManager).

For each (family, streak length, staking) combo: simulate day-by-day P&L
on the SEARCH set (first 5/8 of the window), rank by avg $/day. Re-run
the best candidates on the VALIDATION set (last 3/8, never used to pick
anything) and only report ones that hold up there.
"""

import csv
import glob
import os
from datetime import datetime, timezone

DATA_DIR = "data"
PERIOD_SUFFIX = "300s"
TRAIN_FRACTION = 5 / 8
STAKE = 1.0
MARTINGALE_STAKE = 2.0
PAYOUT = 0.80
MIN_TRADES = 30


def load_real_asset_csvs():
    assets = {}
    for path in sorted(glob.glob(f"{DATA_DIR}/search_*_{PERIOD_SUFFIX}.csv")):
        base = os.path.basename(path)
        asset = base[len("search_"):-len(f"_{PERIOD_SUFFIX}.csv")]
        if "_otc" in asset.lower():
            continue
        rows = []
        with open(path) as f:
            for row in csv.DictReader(f):
                rows.append({"time": int(row["time"]), "open": float(row["open"]), "close": float(row["close"])})
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


def compute_streaks(colors):
    n = len(colors)
    streak = [0] * n
    for i in range(n):
        if i == 0 or colors[i] != colors[i - 1] or colors[i] == "D":
            streak[i] = 1
        else:
            streak[i] = streak[i - 1] + 1
    return streak


def find_events(rows, family, min_streak):
    """Yield (time, bet_color, i) for each qualifying signal. Causal only --
    never looks at colors[i+1] or later to decide whether to fire."""
    colors = [color(r) for r in rows]
    streak = compute_streaks(colors)
    n = len(colors)
    for i in range(30, n - 1):
        if colors[i] == "D":
            continue
        if family == "continuation":
            # i is a break candle: streak of min_streak ended at i-1 with the
            # OPPOSITE color, and i itself is the single break candle.
            if streak[i] != 1:
                continue
            if i - 1 < 0 or streak[i - 1] < min_streak:
                continue
            if colors[i] == colors[i - 1]:
                continue
            bet_color = colors[i]  # bet the break continues
            yield rows[i]["time"], bet_color, i
        elif family == "reversal":
            # fire exactly once per streak: the candle where the streak
            # FIRST reaches min_streak (not every candle of a longer run).
            if streak[i] != min_streak:
                continue
            bet_color = "R" if colors[i] == "G" else "G"  # bet it reverses
            yield rows[i]["time"], bet_color, i


def day_key(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).date()


def simulate(events_with_result, staking):
    """events_with_result: list of (time, won:bool), already sorted."""
    by_day = {}
    for t, won in events_with_result:
        by_day.setdefault(day_key(t), []).append(won)

    daily_pnl = {}
    for d, outcomes in by_day.items():
        pnl = 0.0
        if staking == "flat":
            for won in outcomes:
                pnl += STAKE * PAYOUT if won else -STAKE
        else:  # martingale
            stake = STAKE
            consec_losses = 0
            for won in outcomes:
                if won:
                    pnl += stake * PAYOUT
                    stake = STAKE
                    consec_losses = 0
                else:
                    pnl -= stake
                    consec_losses += 1
                    if consec_losses >= 2:
                        stake = STAKE
                        consec_losses = 0
                    else:
                        stake = MARTINGALE_STAKE
        daily_pnl[d] = pnl
    return daily_pnl


def evaluate(all_data, family, min_streak, staking, cutoff):
    train_events = []
    val_events = []
    for asset, rows in all_data.items():
        colors = [color(r) for r in rows]
        for t, bet_color, i in find_events(rows, family, min_streak):
            if i + 1 >= len(rows):
                continue
            won = colors[i + 1] == bet_color
            if t < cutoff:
                train_events.append((t, won))
            else:
                val_events.append((t, won))
    train_events.sort()
    val_events.sort()
    train_daily = simulate(train_events, staking)
    val_daily = simulate(val_events, staking)
    return train_events, val_events, train_daily, val_daily


def summarize_daily(daily_pnl, label):
    if not daily_pnl:
        print(f"{label:55s} no data")
        return None
    days = len(daily_pnl)
    total = sum(daily_pnl.values())
    avg = total / days
    profitable_days = sum(1 for v in daily_pnl.values() if v > 0)
    print(f"{label:55s} days={days:3d}  avg/day=${avg:+.2f}  total=${total:+.2f}  "
          f"profitable_days={profitable_days}/{days} ({profitable_days/days:.0%})")
    return avg


def main():
    all_data = load_real_asset_csvs()
    print(f"Real markets: {list(all_data.keys())}")

    all_times = [r["time"] for rows in all_data.values() for r in rows]
    cutoff = min(all_times) + TRAIN_FRACTION * (max(all_times) - min(all_times))
    print(f"Train/validation cutoff (UTC): {datetime.fromtimestamp(cutoff, tz=timezone.utc)}\n")

    results = []
    for family in ("continuation", "reversal"):
        for min_streak in (1, 2, 3, 4, 5, 6, 7, 8):
            if family == "continuation" and min_streak < 2:
                continue
            for staking in ("flat", "martingale"):
                train_events, val_events, train_daily, val_daily = evaluate(
                    all_data, family, min_streak, staking, cutoff)
                if len(train_events) < MIN_TRADES:
                    continue
                train_avg = sum(train_daily.values()) / len(train_daily) if train_daily else None
                if train_avg is None:
                    continue
                results.append((family, min_streak, staking, train_events, val_events,
                                 train_daily, val_daily, train_avg))

    results.sort(key=lambda r: -r[7])
    print("=== TOP 15 by SEARCH-set avg $/day ===")
    for family, min_streak, staking, train_events, val_events, train_daily, val_daily, train_avg in results[:15]:
        label = f"{family:12s} streak>={min_streak} {staking:10s} (n_train={len(train_events)})"
        summarize_daily(train_daily, label)

    print("\n=== VALIDATION-set results for the top 8 search-set candidates ===")
    print("(only these numbers matter -- search-set numbers overstate reality)")
    confirmed = []
    for family, min_streak, staking, train_events, val_events, train_daily, val_daily, train_avg in results[:8]:
        label = f"{family:12s} streak>={min_streak} {staking:10s} (n_val={len(val_events)})"
        val_avg = summarize_daily(val_daily, label)
        if val_avg is not None and val_avg > 0 and len(val_events) >= 15:
            confirmed.append((family, min_streak, staking, train_avg, val_avg, len(val_events)))

    print(f"\n=== SUMMARY: {len(confirmed)} candidate(s) profitable on BOTH search and validation ===")
    for family, min_streak, staking, train_avg, val_avg, n_val in confirmed:
        print(f" - {family} streak>={min_streak} {staking}: "
              f"train avg=${train_avg:+.2f}/day, validation avg=${val_avg:+.2f}/day (n_val={n_val})")


if __name__ == "__main__":
    main()
