"""Check for statistical artifacts in OTC (broker-synthetic) candle data
that wouldn't be present in a genuine, efficient market -- and compare
directly against the real-market data as a control.

Tests, all computed causally per-asset then aggregated:
  1. Lag-1..20 autocorrelation of candle-to-candle returns.
     A real efficient market should sit near 0 at every lag. A biased
     synthetic generator might show a consistent non-zero spike (momentum
     or mean-reversion) or periodicity at a specific lag.
  2. Color transition probabilities: P(next green | current green) vs
     P(next green | current red) vs overall P(green). Equal = no
     information in the previous candle's color. A gap here is a
     directly tradeable signal, distinct from anything already tested.
  3. Streak-length distribution vs the theoretical geometric distribution
     expected from i.i.d. 50/50 up/down candles.

Pure offline analysis on cached CSVs. No login, no trading.
"""

import csv
import glob
import math
import os
from datetime import datetime, timezone

DATA_DIR = "data"
PERIOD_SUFFIX = "300s"
MAX_LAG = 20


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


def returns(rows):
    closes = [r["close"] for r in rows]
    return [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes))]


def autocorr(series, lag):
    n = len(series)
    if n <= lag:
        return None
    mean = sum(series) / n
    var = sum((x - mean) ** 2 for x in series) / n
    if var == 0:
        return None
    cov = sum((series[i] - mean) * (series[i - lag] - mean) for i in range(lag, n)) / n
    return cov / var


def color_transitions(rows):
    colors = [color(r) for r in rows]
    colors = [c for c in colors if c != "D"]
    n = len(colors)
    p_green_overall = colors.count("G") / n

    after_g = [colors[i] for i in range(1, n) if colors[i - 1] == "G"]
    after_r = [colors[i] for i in range(1, n) if colors[i - 1] == "R"]
    p_green_after_g = after_g.count("G") / len(after_g) if after_g else None
    p_green_after_r = after_r.count("G") / len(after_r) if after_r else None
    return p_green_overall, p_green_after_g, p_green_after_r, len(after_g), len(after_r)


def streak_lengths(rows):
    colors = [color(r) for r in rows if color(r) != "D"]
    lengths = []
    cur_len = 1
    for i in range(1, len(colors)):
        if colors[i] == colors[i - 1]:
            cur_len += 1
        else:
            lengths.append(cur_len)
            cur_len = 1
    lengths.append(cur_len)
    return lengths


def geometric_expected(n_streaks, p_continue, max_len=8):
    """Expected count of streaks of each length under geometric(p_continue)."""
    out = {}
    remaining = n_streaks
    for length in range(1, max_len):
        prob = (p_continue ** (length - 1)) * (1 - p_continue)
        out[length] = n_streaks * prob
    return out


def analyze_group(all_data, group_name):
    print(f"\n{'='*20} {group_name} ({len(all_data)} assets) {'='*20}")

    # --- Autocorrelation, averaged across assets ---
    lag_sums = {lag: [] for lag in range(1, MAX_LAG + 1)}
    for asset, rows in all_data.items():
        r = returns(rows)
        n = len(r)
        for lag in range(1, MAX_LAG + 1):
            ac = autocorr(r, lag)
            if ac is not None:
                lag_sums[lag].append(ac)

    total_n = sum(len(returns(rows)) for rows in all_data.values()) / max(1, len(all_data))
    sig_threshold = 1.96 / math.sqrt(total_n) if total_n else None
    print(f"Autocorrelation of returns, averaged across assets (95% no-signal band: +/-{sig_threshold:.4f})")
    for lag in range(1, MAX_LAG + 1):
        vals = lag_sums[lag]
        avg = sum(vals) / len(vals) if vals else None
        flag = " <-- OUTSIDE band" if avg is not None and sig_threshold and abs(avg) > sig_threshold else ""
        print(f"  lag {lag:2d}: avg autocorr = {avg:+.4f}{flag}")

    # --- Color transitions, aggregated (pooled) across assets ---
    total_g = total_after_g_g = total_after_g_n = total_after_r_g = total_after_r_n = 0
    for asset, rows in all_data.items():
        p_overall, p_after_g, p_after_r, n_after_g, n_after_r = color_transitions(rows)
        colors = [color(r) for r in rows if color(r) != "D"]
        total_g += colors.count("G")
        if p_after_g is not None:
            total_after_g_g += round(p_after_g * n_after_g)
            total_after_g_n += n_after_g
        if p_after_r is not None:
            total_after_r_g += round(p_after_r * n_after_r)
            total_after_r_n += n_after_r
    total_colors = sum(len([c for c in [color(r) for r in rows] if c != "D"]) for rows in all_data.values())
    p_overall = total_g / total_colors if total_colors else None
    p_after_g = total_after_g_g / total_after_g_n if total_after_g_n else None
    p_after_r = total_after_r_g / total_after_r_n if total_after_r_n else None
    print(f"\nColor transitions (pooled across all assets):")
    print(f"  P(green) overall:        {p_overall:.1%}  (n={total_colors})")
    print(f"  P(green | prev green):   {p_after_g:.1%}  (n={total_after_g_n})")
    print(f"  P(green | prev red):     {p_after_r:.1%}  (n={total_after_r_n})")
    if p_after_g is not None and p_after_r is not None:
        gap = p_after_g - p_after_r
        se = math.sqrt(p_overall * (1 - p_overall) * (1 / total_after_g_n + 1 / total_after_r_n))
        z = gap / se if se else 0
        print(f"  Gap (momentum/reversion signal): {gap:+.1%}  (z={z:+.2f}, |z|>1.96 = significant)")

    # --- Streak-length distribution, pooled ---
    print(f"\nStreak-length distribution (pooled), vs expected under fair 50/50 i.i.d.:")
    all_lengths = []
    for asset, rows in all_data.items():
        all_lengths.extend(streak_lengths(rows))
    n_streaks = len(all_lengths)
    expected = geometric_expected(n_streaks, p_continue=0.5)
    for length in range(1, 8):
        observed = sum(1 for l in all_lengths if l == length)
        exp = expected[length]
        print(f"  streak len {length}: observed={observed:5d}  expected={exp:8.1f}  "
              f"ratio={observed/exp if exp else 0:.2f}")


def main():
    all_rows = load_asset_csvs()
    if not all_rows:
        print("No cached 5-min data found.")
        return

    real_data = {a: rows for a, rows in all_rows.items() if "_otc" not in a.lower()}
    otc_data = {a: rows for a, rows in all_rows.items() if "_otc" in a.lower()}

    print(f"Real markets found: {list(real_data.keys())}")
    print(f"OTC markets found:  {list(otc_data.keys())}")

    if real_data:
        analyze_group(real_data, "REAL MARKETS (control)")
    if otc_data:
        analyze_group(otc_data, "OTC MARKETS (synthetic)")
    else:
        print("\nNo OTC data cached yet -- run fetch_otc_data.py first.")


if __name__ == "__main__":
    main()
