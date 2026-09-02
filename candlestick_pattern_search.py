"""Search for real, named candlestick reversal/continuation patterns --
NOT the existing "N same-color candles then a break" rule already in
strategy.py / pattern_search.py / best_pattern_search.py. This tests
different pattern shapes entirely: engulfing, hammer/shooting star, doji
at a swing extreme, morning/evening star, three soldiers/crows, inside
bar breakout, tweezer top/bottom.

Every pattern is detected from raw OHLC only (the pattern candle(s)
themselves, plus a short recent-price-structure lookback for "is this at
a swing high/low" context) -- no RSI/EMA/MACD/any external indicator.

Same anti-overfitting discipline as pattern_search.py:
  1. Chronological train/validation split per asset (first 5/8 weeks
     search, last 3/8 held out).
  2. Real (live) and OTC (synthetic) assets reported separately -- OTC is
     already proven to be statistically random (see artifact_check.py),
     included here only for completeness/honesty, not because a
     candlestick edge is expected there.
  3. A pattern only counts as "found" if its win rate clears breakeven
     (55.6% @ 80% payout) on the search set AND holds up on validation.

Usage: python3 candlestick_pattern_search.py [period_suffix]
  period_suffix defaults to "300s".
"""

import csv
import glob
import os
import sys
from datetime import datetime, timezone

DATA_DIR = "data"
TRAIN_FRACTION = 5 / 8
PAYOUT = 0.80
BREAKEVEN = 1 / (1 + PAYOUT)  # 55.6%
SWING_LOOKBACK = 10  # candles used to judge "at a recent high/low"
MIN_TRADES_BY_PERIOD = {"300s": 30, "900s": 20, "1800s": 15, "3600s": 10}

PERIOD_SUFFIX = sys.argv[1] if len(sys.argv) > 1 else "300s"
MIN_TRADES = MIN_TRADES_BY_PERIOD.get(PERIOD_SUFFIX, 15)


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


# ---- candle geometry helpers (pure OHLC, no indicators) ----

def body(c):
    return abs(c["close"] - c["open"])


def rng(c):
    return max(c["high"] - c["low"], 1e-12)


def is_green(c):
    return c["close"] > c["open"]


def is_red(c):
    return c["close"] < c["open"]


def upper_wick(c):
    return c["high"] - max(c["open"], c["close"])


def lower_wick(c):
    return min(c["open"], c["close"]) - c["low"]


def at_swing_low(rows, i, lookback=SWING_LOOKBACK):
    lo = min(r["low"] for r in rows[max(0, i - lookback):i])
    return rows[i]["low"] <= lo


def at_swing_high(rows, i, lookback=SWING_LOOKBACK):
    hi = max(r["high"] for r in rows[max(0, i - lookback):i])
    return rows[i]["high"] >= hi


# ---- pattern detectors: each yields (i, "up"|"down") meaning
#      "candle i completes this pattern; bet candle i+1 closes up/down" ----

def detect_engulfing(rows):
    for i in range(1, len(rows) - 1):
        prev, cur = rows[i - 1], rows[i]
        if body(prev) == 0:
            continue
        if is_green(cur) and is_red(prev) and cur["open"] <= prev["close"] and cur["close"] >= prev["open"] \
                and body(cur) > body(prev):
            yield i, "up"
        elif is_red(cur) and is_green(prev) and cur["open"] >= prev["close"] and cur["close"] <= prev["open"] \
                and body(cur) > body(prev):
            yield i, "down"


def detect_hammer_shootingstar(rows):
    for i in range(2, len(rows) - 1):
        c = rows[i]
        b = body(c)
        if b == 0 or rng(c) < 1e-12:
            continue
        prior_down = rows[i - 1]["close"] < rows[i - 2]["close"]
        prior_up = rows[i - 1]["close"] > rows[i - 2]["close"]
        # hammer: small body in upper half, long lower wick, tiny upper wick
        if lower_wick(c) >= 2 * b and upper_wick(c) <= 0.3 * b and prior_down:
            yield i, "up"
        # shooting star: small body in lower half, long upper wick, tiny lower wick
        elif upper_wick(c) >= 2 * b and lower_wick(c) <= 0.3 * b and prior_up:
            yield i, "down"


def detect_doji_at_extreme(rows):
    for i in range(SWING_LOOKBACK, len(rows) - 1):
        c = rows[i]
        if body(c) > 0.1 * rng(c):
            continue
        if at_swing_high(rows, i):
            yield i, "down"
        elif at_swing_low(rows, i):
            yield i, "up"


def detect_star(rows):
    for i in range(2, len(rows) - 1):
        a, b, c = rows[i - 2], rows[i - 1], rows[i]
        if body(a) == 0:
            continue
        small_middle = body(b) < 0.4 * body(a)
        mid_a = (a["open"] + a["close"]) / 2
        if is_red(a) and small_middle and is_green(c) and c["close"] > mid_a:
            yield i, "up"  # morning star
        elif is_green(a) and small_middle and is_red(c) and c["close"] < mid_a:
            yield i, "down"  # evening star


def detect_three_soldiers_crows(rows):
    for i in range(2, len(rows) - 1):
        a, b, c = rows[i - 2], rows[i - 1], rows[i]
        if is_green(a) and is_green(b) and is_green(c) \
                and b["close"] > a["close"] and c["close"] > b["close"] \
                and b["open"] > a["open"] and c["open"] > b["open"] \
                and upper_wick(a) <= 0.3 * body(a) and upper_wick(b) <= 0.3 * body(b) and upper_wick(c) <= 0.3 * body(c):
            yield i, "up"
        elif is_red(a) and is_red(b) and is_red(c) \
                and b["close"] < a["close"] and c["close"] < b["close"] \
                and b["open"] < a["open"] and c["open"] < b["open"] \
                and lower_wick(a) <= 0.3 * body(a) and lower_wick(b) <= 0.3 * body(b) and lower_wick(c) <= 0.3 * body(c):
            yield i, "down"


def detect_inside_bar_breakout(rows):
    for i in range(2, len(rows) - 1):
        mother, inside, brk = rows[i - 2], rows[i - 1], rows[i]
        if inside["high"] <= mother["high"] and inside["low"] >= mother["low"]:
            if brk["close"] > mother["high"]:
                yield i, "up"
            elif brk["close"] < mother["low"]:
                yield i, "down"


def detect_tweezer(rows):
    tol = 0.0005  # 0.05% relative tolerance on matching high/low
    for i in range(1, len(rows) - 1):
        a, b = rows[i - 1], rows[i]
        if abs(a["high"] - b["high"]) <= tol * max(a["high"], 1e-9) and is_green(a) and is_red(b):
            yield i, "down"
        elif abs(a["low"] - b["low"]) <= tol * max(a["low"], 1e-9) and is_red(a) and is_green(b):
            yield i, "up"


PATTERNS = {
    "engulfing": detect_engulfing,
    "hammer_shootingstar": detect_hammer_shootingstar,
    "doji_at_extreme": detect_doji_at_extreme,
    "morning_evening_star": detect_star,
    "three_soldiers_crows": detect_three_soldiers_crows,
    "inside_bar_breakout": detect_inside_bar_breakout,
    "tweezer": detect_tweezer,
}


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


def run_group(label, assets):
    if not assets:
        print(f"  (no {label} assets in this dataset)")
        return

    all_times = [r["time"] for rows in assets.values() for r in rows]
    cutoff = min(all_times) + TRAIN_FRACTION * (max(all_times) - min(all_times))

    print(f"\n==================== {label} ({len(assets)} assets) ====================")
    for pname, detector in PATTERNS.items():
        tn = tw = vn = vw = 0
        for asset, rows in assets.items():
            for i, direction in detector(rows):
                actual_up = rows[i + 1]["close"] > rows[i]["close"]
                win = actual_up if direction == "up" else not actual_up
                if rows[i]["close"] == rows[i + 1]["close"]:
                    continue  # doji next candle -- no resolution
                t = rows[i]["time"]
                if t < cutoff:
                    tn += 1
                    tw += win
                else:
                    vn += 1
                    vw += win

        train_str = fmt_ci(tn, tw)
        val_str = fmt_ci(vn, vw)
        train_rate = tw / tn if tn else 0
        flag = ""
        if tn >= MIN_TRADES and train_rate >= BREAKEVEN:
            val_rate = vw / vn if vn else 0
            held = vn >= MIN_TRADES and val_rate >= BREAKEVEN
            flag = "  <-- CLEARS BREAKEVEN ON SEARCH, " + ("HOLDS on validation!" if held else "fails validation")
        print(f"  {pname:24s} TRAIN {train_str:45s} VAL {val_str}{flag}")
    print(f"  (breakeven win rate needed @ {PAYOUT:.0%} payout: {BREAKEVEN:.1%})")


def main():
    all_assets = load_asset_csvs()
    if not all_assets:
        print(f"No cached data found for suffix '{PERIOD_SUFFIX}' -- fetch it first.")
        return
    print(f"Period: {PERIOD_SUFFIX}  MIN_TRADES: {MIN_TRADES}  Assets: {list(all_assets.keys())}")

    real = {a: r for a, r in all_assets.items() if "_otc" not in a.lower()}
    otc = {a: r for a, r in all_assets.items() if "_otc" in a.lower()}

    run_group("REAL (live) markets", real)
    run_group("OTC (synthetic) markets", otc)


main()


def find_90pct_slices_and_validate():
    """Concretely test the user's proposed filter: for every pattern,
    slice by asset and find any train-set win rate >= 90%, then check
    what that exact slice does on validation data it never touched."""
    all_assets = load_asset_csvs()
    all_times = [r["time"] for rows in all_assets.values() for r in rows]
    cutoff = min(all_times) + TRAIN_FRACTION * (max(all_times) - min(all_times))

    print(f"\n{'='*70}\nSearching for ANY per-asset slice hitting >=90% train win rate\n{'='*70}")
    found_any = False
    for pname, detector in PATTERNS.items():
        for asset, rows in all_assets.items():
            tn = tw = vn = vw = 0
            for i, direction in detector(rows):
                if rows[i]["close"] == rows[i + 1]["close"]:
                    continue
                actual_up = rows[i + 1]["close"] > rows[i]["close"]
                win = actual_up if direction == "up" else not actual_up
                if rows[i]["time"] < cutoff:
                    tn += 1
                    tw += win
                else:
                    vn += 1
                    vw += win
            if tn >= 10 and (tw / tn) >= 0.90:
                found_any = True
                val_rate = f"{vw/vn:.1%}" if vn else "n/a"
                print(f"  {pname:24s} {asset:14s} TRAIN {fmt_ci(tn, tw)}  ->  VALIDATION n={vn} win={vw} rate={val_rate}")
    if not found_any:
        print("  None. No pattern+asset slice reaches 90% train win rate even at n>=10 "
              "(and n>=10 is already too small to trust -- true edges don't need this much slicing to find).")


if __name__ == "__main__" and "--filter90" in sys.argv:
    find_90pct_slices_and_validate()


ASSET_PAYOUT = {"CADJPY": 0.87, "EURGBP": 0.86, "AUDJPY": 0.85}


def per_asset_breakdown():
    all_assets = load_asset_csvs()
    all_times = [r["time"] for rows in all_assets.values() for r in rows]
    cutoff = min(all_times) + TRAIN_FRACTION * (max(all_times) - min(all_times))

    print(f"\n{'='*70}\nPer-asset breakdown for high-payout real pairs: {list(ASSET_PAYOUT)}\n{'='*70}")
    for asset, payout in ASSET_PAYOUT.items():
        if asset not in all_assets:
            print(f"  {asset}: no cached data for this period")
            continue
        breakeven = 1 / (1 + payout)
        rows = all_assets[asset]
        print(f"\n{asset} (current payout {payout:.0%}, breakeven win rate {breakeven:.1%}):")
        for pname, detector in PATTERNS.items():
            tn = tw = vn = vw = 0
            for i, direction in detector(rows):
                if rows[i]["close"] == rows[i + 1]["close"]:
                    continue
                actual_up = rows[i + 1]["close"] > rows[i]["close"]
                win = actual_up if direction == "up" else not actual_up
                if rows[i]["time"] < cutoff:
                    tn += 1
                    tw += win
                else:
                    vn += 1
                    vw += win
            train_rate = tw / tn if tn else 0
            val_rate = vw / vn if vn else 0
            flag = ""
            if tn >= 10 and train_rate >= breakeven:
                held = vn >= 10 and val_rate >= breakeven
                flag = "  <-- clears breakeven on search, " + ("HOLDS on validation!" if held else "fails validation")
            print(f"  {pname:24s} TRAIN {fmt_ci(tn, tw)}   VAL {fmt_ci(vn, vw)}{flag}")


if __name__ == "__main__" and "--perasset" in sys.argv:
    per_asset_breakdown()
