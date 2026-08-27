"""Systematic pattern search over cached candle data
(data/search_{ASSET}_{PERIOD}s.csv, produced by fetch_search_data.py /
fetch_multi_tf.py).

Usage: python3 pattern_search.py [period_suffix]
  period_suffix defaults to "300s" (5-min). Use "900s"/"1800s"/"3600s" for
  15-min/30-min/1hr data once fetched.

Pure offline analysis -- no login, no network, no trading.

Methodology (to avoid data-dredging a lucky-looking pattern):
  1. Split each asset's history chronologically: first TRAIN_FRACTION of
     the window is the SEARCH set, the rest is a held-out VALIDATION set
     that is never used to pick or tune anything.
  2. Stage 1: sweep base candlestick triggers (direction x streak length).
  3. Stage 2: layer single filters (RSI at multiple thresholds, EMA trend,
     momentum/volatility, prior-trigger result) onto the best base
     triggers.
  4. Stage 3: combine the best surviving single filters, pairwise AND
     triple-wise.
  5. Stage 4: for the best rule so far, check per-hour and per-asset win
     rate on the SEARCH set, and try restricting to the strongest subset
     (data-driven -- flagged as highest overfit risk, must survive
     validation to count).
  6. FINAL: re-run every surviving candidate (train win rate >= 70%,
     n >= MIN_TRADES) against the VALIDATION set it never touched. Only
     candidates that hold up there are reported as confirmed.
"""

import csv
import glob
import os
import sys
from datetime import datetime, timezone

DATA_DIR = "data"
TRAIN_FRACTION = 5 / 8  # first 5 of 8 weeks = search set, last 3 = validation
WARMUP = 25  # candles needed before indicators are valid
SEARCH_MIN_WINRATE = 0.70  # candidates below this on search set aren't worth validating

PERIOD_SUFFIX = sys.argv[1] if len(sys.argv) > 1 else "300s"
MIN_TRADES_BY_PERIOD = {"300s": 30, "900s": 20, "1800s": 15, "3600s": 10}
MIN_TRADES = MIN_TRADES_BY_PERIOD.get(PERIOD_SUFFIX, 15)


def load_asset_csvs():
    assets = {}
    pattern = f"{DATA_DIR}/search_*_{PERIOD_SUFFIX}.csv"
    for path in sorted(glob.glob(pattern)):
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


def ema(series, span):
    out = [None] * len(series)
    k = 2 / (span + 1)
    e = None
    for i, v in enumerate(series):
        e = v if e is None else v * k + e * (1 - k)
        out[i] = e
    return out


def compute_features(rows):
    n = len(rows)
    closes = [r["close"] for r in rows]
    colors = [color(r) for r in rows]

    streak = [0] * n
    for i in range(n):
        if i == 0 or colors[i] != colors[i - 1] or colors[i] == "D":
            streak[i] = 1
        else:
            streak[i] = streak[i - 1] + 1

    period = 14
    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        delta = closes[i] - closes[i - 1]
        gains[i] = max(delta, 0.0)
        losses[i] = max(-delta, 0.0)
    rsi = [None] * n
    avg_gain = avg_loss = None
    for i in range(1, n):
        if i < period:
            continue
        if i == period:
            avg_gain = sum(gains[1:period + 1]) / period
            avg_loss = sum(losses[1:period + 1]) / period
        else:
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rsi[i] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)

    ema5 = ema(closes, 5)
    ema20 = ema(closes, 20)

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

    body = [abs(rows[i]["close"] - rows[i]["open"]) for i in range(n)]
    hour = [datetime.fromtimestamp(rows[i]["time"], tz=timezone.utc).hour for i in range(n)]

    # distance of close from EMA20, in ATR units (overextension measure)
    ext = [None] * n
    for i in range(n):
        if ema20[i] is not None and atr[i]:
            ext[i] = (rows[i]["close"] - ema20[i]) / atr[i]

    return {"colors": colors, "streak": streak, "rsi": rsi, "ema5": ema5,
            "ema20": ema20, "atr": atr, "body": body, "hour": hour, "ext": ext}


def mk_rsi_filter(threshold, direction):
    if direction == "above":
        return lambda f, i: f["rsi"][i] is not None and f["rsi"][i] >= threshold
    return lambda f, i: f["rsi"][i] is not None and f["rsi"][i] <= threshold


FILTERS = {
    "none": lambda f, i: True,
    "rsi_high60": mk_rsi_filter(60, "above"),
    "rsi_high70": mk_rsi_filter(70, "above"),
    "rsi_high75": mk_rsi_filter(75, "above"),
    "rsi_low40": mk_rsi_filter(40, "below"),
    "rsi_low30": mk_rsi_filter(30, "below"),
    "rsi_low25": mk_rsi_filter(25, "below"),
    "ema_downtrend": lambda f, i: f["ema5"][i] is not None and f["ema5"][i] < f["ema20"][i],
    "ema_uptrend": lambda f, i: f["ema5"][i] is not None and f["ema5"][i] > f["ema20"][i],
    "big_body": lambda f, i: f["atr"][i] and f["body"][i] > 1.2 * f["atr"][i],
    "small_body": lambda f, i: f["atr"][i] and f["body"][i] < 0.8 * f["atr"][i],
    "overextended_up": lambda f, i: f["ext"][i] is not None and f["ext"][i] > 1.5,
    "overextended_down": lambda f, i: f["ext"][i] is not None and f["ext"][i] < -1.5,
}


def base_events(asset, feat, min_streak, direction):
    colors = feat["colors"]
    streak = feat["streak"]
    n = len(colors)
    trig_color = "R" if direction == "down" else "G"
    pre_color = "G" if direction == "down" else "R"
    for i in range(WARMUP, n - 1):
        if colors[i] != trig_color:
            continue
        if colors[i - 1] != pre_color or streak[i - 1] < min_streak:
            continue
        yield i


def resolves_win(feat, i, direction):
    colors = feat["colors"]
    if i + 1 >= len(colors):
        return None
    if direction == "down":
        return colors[i + 1] == "R"
    return colors[i + 1] == "G"


def build_prev_direct_map(all_data, direction):
    result = {}
    for asset, (rows, feat) in all_data.items():
        events = list(base_events(asset, feat, 2, direction))
        last_win = None
        for i in events:
            result[(asset, i)] = last_win
            win = resolves_win(feat, i, direction)
            if win is not None:
                last_win = win
    return result


def ci(wins, n):
    if n == 0:
        return None
    p = wins / n
    se = (p * (1 - p) / n) ** 0.5
    return p, max(0, p - 1.96 * se), min(1, p + 1.96 * se)


def cutoff_time(all_data):
    all_times = [r["time"] for rows, _ in all_data.values() for r in rows]
    lo, hi = min(all_times), max(all_times)
    return lo + TRAIN_FRACTION * (hi - lo)


def passes_filters(feat, i, filter_names, asset, prev_direct):
    for fname in filter_names:
        if fname == "prev_direct":
            pd = prev_direct.get(asset, {}).get(i)
            if pd is not True:
                return False
        elif not FILTERS[fname](feat, i):
            return False
    return True


def collect_events(all_data, direction, min_streak, filter_names, prev_direct,
                    hour_allow=None, asset_allow=None):
    """Yield (asset, i, time, win) for every event passing all filters."""
    for asset, (rows, feat) in all_data.items():
        if asset_allow is not None and asset not in asset_allow:
            continue
        pd_map = prev_direct.get(direction, {}) if "prev_direct" in filter_names else {}
        for i in base_events(asset, feat, min_streak, direction):
            if hour_allow is not None and feat["hour"][i] not in hour_allow:
                continue
            if not passes_filters(feat, i, filter_names, asset, {direction: pd_map}):
                continue
            win = resolves_win(feat, i, direction)
            if win is None:
                continue
            yield asset, i, rows[i]["time"], win


def split_train_val(events, cutoff):
    tn = tw = vn = vw = 0
    for asset, i, t, win in events:
        if t < cutoff:
            tn += 1
            tw += 1 if win else 0
        else:
            vn += 1
            vw += 1 if win else 0
    return tn, tw, vn, vw


def fmt_ci(n, w):
    result = ci(w, n)
    if not result:
        return f"n={n}"
    p, lo, hi = result
    return f"n={n} win={w} rate={p:.1%} CI[{lo:.1%}-{hi:.1%}]"


def main():
    all_rows = load_asset_csvs()
    if not all_rows:
        print(f"No cached data found for suffix '{PERIOD_SUFFIX}' -- fetch it first.")
        return
    print(f"Period: {PERIOD_SUFFIX}  MIN_TRADES: {MIN_TRADES}")
    print(f"Loaded assets: {list(all_rows.keys())}")
    for a, rows in all_rows.items():
        print(f"  {a}: {len(rows)} candles "
              f"({datetime.fromtimestamp(rows[0]['time'], tz=timezone.utc).date()} -> "
              f"{datetime.fromtimestamp(rows[-1]['time'], tz=timezone.utc).date()})")

    all_data = {a: (rows, compute_features(rows)) for a, rows in all_rows.items()}
    cutoff = cutoff_time(all_data)
    print(f"\nTrain/validation cutoff (UTC): {datetime.fromtimestamp(cutoff, tz=timezone.utc)}")

    prev_direct = {
        "down": build_prev_direct_map(all_data, "down"),
        "up": build_prev_direct_map(all_data, "up"),
    }

    def ev(direction, min_streak, filter_names, hour_allow=None, asset_allow=None):
        events = list(collect_events(all_data, direction, min_streak, filter_names,
                                      prev_direct, hour_allow, asset_allow))
        return split_train_val(events, cutoff), events

    # ---------- Stage 1 ----------
    print("\n=== STAGE 1: base candlestick trigger sweep (SEARCH set) ===")
    stage1 = []
    for direction in ("down", "up"):
        for min_streak in (2, 3, 4, 5, 6, 7, 8):
            (tn, tw, vn, vw), _ = ev(direction, min_streak, [])
            stage1.append((direction, min_streak, tn, tw, vn, vw))
            if tn:
                print(f"{direction:5s} streak>={min_streak}  TRAIN {fmt_ci(tn, tw)}")

    base_candidates = [(d, ms) for d, ms, tn, tw, vn, vw in stage1 if tn >= MIN_TRADES]

    # ---------- Stage 2 ----------
    print("\n=== STAGE 2: single-filter overlays (SEARCH set) ===")
    stage2_results = []
    filter_names_all = list(FILTERS.keys()) + ["prev_direct"]
    for direction, min_streak in base_candidates:
        for fname in filter_names_all:
            (tn, tw, vn, vw), _ = ev(direction, min_streak, [fname])
            if tn < MIN_TRADES:
                continue
            stage2_results.append((direction, min_streak, (fname,), tn, tw, vn, vw, tw / tn))

    stage2_results.sort(key=lambda r: -r[7])
    for direction, min_streak, fnames, tn, tw, vn, vw, rate in stage2_results[:15]:
        print(f"{direction:5s} streak>={min_streak}  +{'+'.join(fnames):14s}  TRAIN {fmt_ci(tn, tw)}")

    # ---------- Stage 3: pairwise + triple combos ----------
    print("\n=== STAGE 3: multi-filter combos (SEARCH set) ===")
    top_singles = stage2_results[:8]
    base_keys = sorted(set((d, ms) for d, ms, *_ in top_singles))
    stage3_results = []
    for d, ms in base_keys:
        fnames_for_base = [fn[0] for dd, mm, fn, *_ in top_singles if dd == d and mm == ms]
        for i in range(len(fnames_for_base)):
            for j in range(i + 1, len(fnames_for_base)):
                combo = tuple(sorted({fnames_for_base[i], fnames_for_base[j]}))
                if len(combo) < 2:
                    continue
                (tn, tw, vn, vw), _ = ev(d, ms, list(combo))
                if tn < MIN_TRADES:
                    continue
                stage3_results.append((d, ms, combo, tn, tw, vn, vw, tw / tn))
        for i in range(len(fnames_for_base)):
            for j in range(i + 1, len(fnames_for_base)):
                for k in range(j + 1, len(fnames_for_base)):
                    combo = tuple(sorted({fnames_for_base[i], fnames_for_base[j], fnames_for_base[k]}))
                    if len(combo) < 3:
                        continue
                    (tn, tw, vn, vw), _ = ev(d, ms, list(combo))
                    if tn < MIN_TRADES:
                        continue
                    stage3_results.append((d, ms, combo, tn, tw, vn, vw, tw / tn))

    stage3_results.sort(key=lambda r: -r[7])
    for d, ms, fnames, tn, tw, vn, vw, rate in stage3_results[:10]:
        print(f"{d:5s} streak>={ms}  +{'+'.join(fnames):28s}  TRAIN {fmt_ci(tn, tw)}")

    # ---------- Collect candidates clearing the bar so far ----------
    pre_stage4 = []
    for direction, min_streak, tn, tw, vn, vw in stage1:
        if tn >= MIN_TRADES and tw / tn >= SEARCH_MIN_WINRATE:
            pre_stage4.append((direction, min_streak, (), tn, tw))
    for direction, min_streak, fnames, tn, tw, vn, vw, rate in stage2_results:
        if rate >= SEARCH_MIN_WINRATE:
            pre_stage4.append((direction, min_streak, fnames, tn, tw))
    for d, ms, fnames, tn, tw, vn, vw, rate in stage3_results:
        if rate >= SEARCH_MIN_WINRATE:
            pre_stage4.append((d, ms, fnames, tn, tw))

    # ---------- Stage 4: hour / asset refinement on the single best rule ----------
    print("\n=== STAGE 4: session-hour / per-asset refinement (best rule so far, SEARCH set) ===")
    all_ranked = sorted(stage2_results + [(d, ms, (), tn, tw, vn, vw, tw / tn if tn else 0)
                                           for d, ms, tn, tw, vn, vw in stage1],
                         key=lambda r: -r[3] if r[3] >= MIN_TRADES else 0)
    candidates_ranked = sorted(
        [r for r in (stage2_results + stage3_results) if r[3] >= MIN_TRADES],
        key=lambda r: -r[7]
    )
    hour_asset_candidates = []
    if candidates_ranked:
        best = candidates_ranked[0]
        d, ms, fnames = best[0], best[1], best[2]
        print(f"Base rule: {d} streak>={ms} +{'+'.join(fnames)}")
        (tn, tw, vn, vw), events = ev(d, ms, list(fnames))
        train_events = [(a, i, t, w) for a, i, t, w in events if t < cutoff]

        by_hour = {}
        for a, i, t, w in train_events:
            h = datetime.fromtimestamp(t, tz=timezone.utc).hour
            by_hour.setdefault(h, [0, 0])
            by_hour[h][0] += 1
            by_hour[h][1] += 1 if w else 0
        print("Per-hour (UTC) breakdown on SEARCH set:")
        good_hours = set()
        for h in sorted(by_hour):
            n, w = by_hour[h]
            rate = w / n if n else 0
            flag = " <-- above overall" if n >= 5 and rate >= (tw / tn if tn else 0) else ""
            print(f"  {h:02d}:00  n={n:4d} win={w:4d} rate={rate:.1%}{flag}")
            if n >= 5 and rate >= 0.60:
                good_hours.add(h)
        if good_hours and len(good_hours) < len(by_hour):
            (htn, htw, hvn, hvw), _ = ev(d, ms, list(fnames), hour_allow=good_hours)
            print(f"Hour-restricted candidate (hours={sorted(good_hours)}): TRAIN {fmt_ci(htn, htw)}")
            if htn >= MIN_TRADES:
                hour_asset_candidates.append((d, ms, fnames + ("hour_filtered",), htn, htw, good_hours, None))

        by_asset = {}
        for a, i, t, w in train_events:
            by_asset.setdefault(a, [0, 0])
            by_asset[a][0] += 1
            by_asset[a][1] += 1 if w else 0
        print("Per-asset breakdown on SEARCH set:")
        good_assets = set()
        for a in sorted(by_asset):
            n, w = by_asset[a]
            rate = w / n if n else 0
            flag = " <-- above overall" if n >= 5 and rate >= (tw / tn if tn else 0) else ""
            print(f"  {a:8s} n={n:4d} win={w:4d} rate={rate:.1%}{flag}")
            if n >= 5 and rate >= 0.60:
                good_assets.add(a)
        if good_assets and len(good_assets) < len(by_asset):
            (atn, atw, avn, avw), _ = ev(d, ms, list(fnames), asset_allow=good_assets)
            print(f"Asset-restricted candidate (assets={sorted(good_assets)}): TRAIN {fmt_ci(atn, atw)}")
            if atn >= MIN_TRADES:
                hour_asset_candidates.append((d, ms, fnames + ("asset_filtered",), atn, atw, None, good_assets))
    else:
        print("No base rule with enough trades to refine.")

    # ---------- FINAL: validate everything that cleared the search-set bar ----------
    print(f"\n=== CANDIDATES CLEARING {SEARCH_MIN_WINRATE:.0%} on SEARCH set (n>={MIN_TRADES}) ===")
    final_candidates = list(pre_stage4)
    for d, ms, fnames, tn, tw, hour_allow, asset_allow in hour_asset_candidates:
        rate = tw / tn
        if rate >= SEARCH_MIN_WINRATE:
            final_candidates.append((d, ms, fnames, tn, tw, hour_allow, asset_allow))

    if not final_candidates:
        print("None found.")
    else:
        for c in final_candidates:
            d, ms, fnames, tn, tw = c[0], c[1], c[2], c[3], c[4]
            label = f"{d} streak>={ms}" + (f" +{'+'.join(fnames)}" if fnames else "")
            print(f"{label:55s} TRAIN {fmt_ci(tn, tw)}")

    print(f"\n=== FINAL: VALIDATION-SET results for search-set survivors ===")
    print("(only these numbers matter -- search-set numbers are not honest estimates)")
    confirmed = []
    for c in final_candidates:
        d, ms, fnames = c[0], c[1], c[2]
        tn, tw = c[3], c[4]
        hour_allow = c[5] if len(c) > 5 else None
        asset_allow = c[6] if len(c) > 6 else None
        base_fnames = tuple(f for f in fnames if f not in ("hour_filtered", "asset_filtered"))
        (_, _, vn, vw), _ = ev(d, ms, list(base_fnames), hour_allow=hour_allow, asset_allow=asset_allow)
        label = f"{d} streak>={ms}" + (f" +{'+'.join(fnames)}" if fnames else "")
        train_rate = tw / tn
        val_rate = vw / vn if vn else None
        status = "CONFIRMED" if (vn >= 15 and val_rate is not None and val_rate >= 0.70) else "did not hold up"
        print(f"{label:55s} TRAIN rate={train_rate:.1%} (n={tn})  ->  VALIDATION {fmt_ci(vn, vw)}  [{status}]")
        if status == "CONFIRMED":
            confirmed.append(label)

    print(f"\n=== SUMMARY: {len(confirmed)} pattern(s) confirmed on held-out data ===")
    for label in confirmed:
        print(f" - {label}")


if __name__ == "__main__":
    main()
