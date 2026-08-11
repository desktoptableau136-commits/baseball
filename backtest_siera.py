"""Backtest whether SIERA predicts FUTURE run prevention better than raw ERA (and, as an
illustrative but leak-contaminated reference, season xERA), stratified by first-half sample
size. See docs/scoring.md's "SIERA" section for the full write-up; this script's own report
output restates the key caveats so they travel with the numbers.

Design: for several past complete seasons, split into a first half (season start -> All-Star
break) and second half (break -> season end). Correlate each pitcher's FIRST-HALF SIERA and
FIRST-HALF raw ERA against his SECOND-HALF actual ERA (the outcome being predicted),
stratified by how many batters he faced in the first half -- the sample-size axis
"stabilizes faster" is actually about.

FRAMING CAVEAT (read before trusting a number here): this tests PREDICTIVE VALIDITY (does a
first-half metric predict second-half outcomes?), not the video's literal SPLIT-HALF
SELF-RELIABILITY claim ("SIERA correlates with itself after ~200 BF"). Related, but not the
same question -- this script answers "should I trust this metric's read on a player right
now," not "does SIERA literally stabilize at exactly 200 BF."

SURVIVORSHIP CAVEAT: only pitchers who have a line in BOTH halves enter the comparison -- a
pitcher hurt/demoted/released after a rough first half drops out of the second-half sample
entirely. This attenuates every metric's r and hits the smallest-BF bucket hardest (where
marginal arms concentrate). Shared equally across SIERA/ERA/xERA, so the RELATIVE comparison
across metrics should still be valid -- but don't read a modest r as "SIERA barely helps"
without this caveat attached.

XERA CAVEAT: season xERA (statcast_pitcher_expected_stats) has no date-range parameter -- it
covers the WHOLE season, including the second half being predicted. That's a real leak, not
a fair third competitor. Reported separately, labeled illustrative; don't let a close
SIERA-vs-xERA number leak into the headline SIERA-vs-ERA conclusion.

SCALE CAVEAT: SIERA is intentionally regressed toward league-average run environment (no
park/defense/sequencing terms); raw first-half ERA is not. MAE/RMSE/bias below will
penalize SIERA for being well-calibrated-but-different-scale, not for being wrong -- lead
with r (Pearson correlation), not the error terms, when comparing metrics.

Run:
  python backtest_siera.py                          # default seasons 2022-2025, BF buckets 100/150/200/250
  python backtest_siera.py --seasons 2023,2024       # fewer seasons, faster
  python backtest_siera.py --bf-buckets 100,200      # fewer buckets
  python backtest_siera.py --no-cache                # bypass the local per-(season,half) JSON cache
"""
import argparse
import json
import math
import os

import fetch_data as fd
from backtest_projections import Acc

CACHE_DIR = os.path.join("data", "backtest_cache")

# All-Star break dates -- a reasonable, well-defined midpoint for each season. Exact
# precision doesn't matter; this just needs to be roughly the season's halfway point.
_ALLSTAR = {
    2022: "2022-07-19",
    2023: "2023-07-11",
    2024: "2024-07-16",
    2025: "2025-07-15",
}


def _load_cache(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return None


def _save_cache(path, data):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def _pull_range_era_bf(start, end, year, half, use_cache=True):
    """Raw BBRef range pull -> {PlayerName: {"ERA":.., "BF":..}}, deduped keeping the
    higher-BF row per name (two distinct real pitchers can share an exact name -- same
    ambiguity fetch_data.get_bbref_pitcher_battedball already guards against)."""
    path = os.path.join(CACHE_DIR, f"siera_bt_range_{year}_{half}.json")
    if use_cache:
        cached = _load_cache(path)
        if cached is not None:
            return cached
    from pybaseball import pitching_stats_range
    df = pitching_stats_range(start, end)
    name_col = next((c for c in df.columns if c.lower() in ("name", "playername")), None)
    out = {}
    for _, row in df.iterrows():
        name = row.get(name_col)
        if not name:
            continue
        try:
            bf = float(row.get("BF"))
            era = float(row.get("ERA"))
        except (TypeError, ValueError):
            continue
        # BBRef can render an "inf" ERA string for a 0.0-IP-but-runs-allowed edge case, which
        # float() parses successfully (unlike NaN, `inf == inf` is True, so a plain NaN check
        # doesn't catch it) -- guard explicitly, or one row poisons every downstream Acc it
        # touches (MAE/RMSE/bias/r all become inf/nan for the whole bucket).
        if not (math.isfinite(bf) and math.isfinite(era)) or bf <= 0:
            continue
        prev = out.get(name)
        if prev is None or bf > prev["BF"]:
            out[name] = {"ERA": era, "BF": bf}
    _save_cache(path, out)
    return out


def _first_half_siera(year, start, end, use_cache=True):
    """{PlayerName: SIERA} for a season window, via the real production getter
    (fetch_data.get_bbref_pitcher_battedball) -- no parallel formula copy."""
    path = os.path.join(CACHE_DIR, f"siera_bt_siera_{year}.json")
    if use_cache:
        cached = _load_cache(path)
        if cached is not None:
            return cached
    df = fd.get_bbref_pitcher_battedball(year, start=start, end=end)
    out = {row["PlayerName"]: row["SIERA"] for _, row in df.iterrows()}
    _save_cache(path, out)
    return out


def _season_xera(year, use_cache=True):
    """{PlayerName: xERA} for a WHOLE season -- LEAK-CONTAMINATED for this backtest's
    second half (see module docstring); illustrative reference only, not a fair competitor."""
    path = os.path.join(CACHE_DIR, f"siera_bt_xera_{year}.json")
    if use_cache:
        cached = _load_cache(path)
        if cached is not None:
            return cached
    df = fd.get_savant_pitcher_expected(year)
    out = {row["PlayerName"]: row["xERA"] for _, row in df.iterrows()
           if row.get("PlayerName") and "xERA" in df.columns}
    _save_cache(path, out)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", default="2022,2023,2024,2025")
    ap.add_argument("--bf-buckets", default="100,150,200,250")
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()
    use_cache = not args.no_cache
    seasons = [int(s) for s in args.seasons.split(",")]
    bf_floors = sorted(int(b) for b in args.bf_buckets.split(","))

    print("=" * 72)
    print("SIERA BACKTEST -- predictive validity, NOT the literal 200-BF stabilization claim")
    print("(first-half SIERA/ERA vs second-half actual ERA; xERA arm is leak-contaminated)")
    print("=" * 72)

    pooled = {bf: {"SIERA": Acc(), "ERA": Acc(), "xERA (leaky)": Acc()} for bf in bf_floors}

    for year in seasons:
        allstar = _ALLSTAR.get(year)
        if not allstar:
            print(f"\n{year}: no All-Star date on file, skipping")
            continue
        season_start, season_end = f"{year}-03-01", f"{year}-11-01"
        print(f"\n--- {year} (H1 {season_start}..{allstar}, H2 {allstar}..{season_end}) ---")

        h1 = _pull_range_era_bf(season_start, allstar, year, "h1", use_cache)
        h2 = _pull_range_era_bf(allstar, season_end, year, "h2", use_cache)
        siera1 = _first_half_siera(year, season_start, allstar, use_cache)
        xera_season = _season_xera(year, use_cache)

        season_acc = {bf: {"SIERA": Acc(), "ERA": Acc(), "xERA (leaky)": Acc()} for bf in bf_floors}
        n_considered = 0
        for name, row1 in h1.items():
            row2 = h2.get(name)
            if row2 is None:
                continue
            bf1, era1, era2 = row1["BF"], row1["ERA"], row2["ERA"]
            n_considered += 1
            for bf_floor in bf_floors:
                if bf1 < bf_floor:
                    continue
                season_acc[bf_floor]["ERA"].add(era1, era2)
                pooled[bf_floor]["ERA"].add(era1, era2)
                s = siera1.get(name)
                if s is not None:
                    season_acc[bf_floor]["SIERA"].add(s, era2)
                    pooled[bf_floor]["SIERA"].add(s, era2)
                x = xera_season.get(name)
                if x is not None:
                    season_acc[bf_floor]["xERA (leaky)"].add(x, era2)
                    pooled[bf_floor]["xERA (leaky)"].add(x, era2)

        print(f"  {n_considered} pitchers with a first- AND second-half line")
        for bf_floor in bf_floors:
            print(f"  BF>={bf_floor}:")
            for label, acc in season_acc[bf_floor].items():
                print("   " + acc.row(label))

    print("\n" + "=" * 72)
    print("POOLED ACROSS SEASONS (the number to trust -- single-season buckets are thin)")
    print("=" * 72)
    for bf_floor in bf_floors:
        print(f"\nBF>={bf_floor}:")
        for label, acc in pooled[bf_floor].items():
            print("  " + acc.row(label))

    print()
    print("Reminders:")
    print("  1. Tests PREDICTIVE VALIDITY, not the literal 200-BF split-half self-reliability")
    print("     claim -- see module docstring.")
    print("  2. Survivorship bias attenuates absolute r at every bucket, worst at the")
    print("     smallest -- read the RELATIVE ranking across metrics, not the absolute r.")
    print("  3. The xERA arm sees the second half it's predicting (no date-range param on")
    print("     Savant's expected-stats endpoint) -- NOT a fair competitor, illustrative only.")
    print("  4. MAE/RMSE/bias above are scale-confounded (SIERA regresses toward")
    print("     league-average; raw ERA doesn't) -- lead with r when comparing metrics.")


if __name__ == "__main__":
    main()
