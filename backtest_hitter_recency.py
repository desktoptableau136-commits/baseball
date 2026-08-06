"""Backtest hitter_recency_flag / hitter_recency_severity (fantasy/scoring.py).

Honest WALK-FORWARD, mirroring backtest_projections.py's design for the pitcher-side
pitcher_recency_flag/pitcher_bounceback_flag: every trailing recent-window read is built
strictly from games BEFORE the point being evaluated, and the flag under test is called via
the REAL shipped sd.hitter_recency_flag / sd.hitter_recency_severity -- not a parallel
reimplementation. Unlike the pitcher script (which swept a candidate metric before separately
retesting the shipped function), there's only ONE production function under test here, so the
whole loop calls it directly throughout.

Why this exists: pitcher_recency_flag was disabled in session 107 after this exact kind of
backtest found its confirmation direction inverted (see docs/scoring.md, "Pitcher recency").
hitter_recency_flag is architecturally the same signal -- a recent-window stat vs. a season
skill anchor, confirm/contradict a buy-low/sell-high read -- and has never been backtested this
way. It's live today: it decides whether a hitter's sell-high badge renders hollow (season-level
guess) or confirmed (solid), and hitter_recency_severity feeds a continuous
_drop_eligibility_score discount (fantasy/trades.py).

Simplification (mirrors the pitcher script's own opponent-OPS note): the season SKILL ANCHOR
(xBA/xSLG) is NOT walk-forward -- it's pulled once from the current data/snapshot.json season
row, same simplification backtest_projections.py already uses and treats as acceptable for
xERA. Only the recent window AND the forward outcome window are walk-forward, built from MLB
StatsAPI per-game hitting logs (group=hitting -- same host/pattern as the pitcher script's
group=pitching pull, reusing its _get_json/_parse_date/_nk/Acc via import).

Evaluation cadence: a fresh read every `--recency-window` days per hitter (trailing window ->
flag -> forward window outcome, then advance), NOT one evaluation per game -- daily evaluation
against a multi-day window would make consecutive reads ~90%+ overlapping (pseudo-replication
inflating n without adding independent information). This mirrors the pitcher script's own
start-to-start cadence (~5 days apart vs. a 30-day window) at a comparable overlap ratio.

Data: MLB StatsAPI per-hitter game logs. No email, no snapshot writes.

Run:
  python backtest_hitter_recency.py                       # broad pool, cached
  python backtest_hitter_recency.py --limit 30 --no-cache  # quick smoke test
  python backtest_hitter_recency.py --recency-window 7     # shorter recent/forward window
"""
import argparse
import json
import os
import time
from datetime import timedelta

import send_digest as sd
import backtest_projections as bp

STATSAPI = bp.STATSAPI
CACHE_DIR = os.path.join("data", "backtest_cache_hit")  # separate from the pitcher cache dir --
# person IDs are shared across stat groups (a two-way player like Ohtani has both a pitching
# AND a hitting game log under the SAME MLB person id), so a shared cache dir keyed only on
# "{pid}_{season}.json" would silently serve one script's cached JSON to the other.
DEFAULT_SEASON = sd.YEAR


# ---------------------------------------------------------------- data pulls ----
def build_hitter_pool(season, limit, min_ab):
    """Top-`limit` MLB hitters by at-bats -> [(person_id, name, ab)]."""
    url = (f"{STATSAPI}/stats?stats=season&group=hitting&season={season}"
           f"&sportId=1&gameType=R&playerPool=all&limit={limit}&sortStat=atBats")
    data = bp._get_json(url)
    out = []
    for split in data.get("stats", [{}])[0].get("splits", []):
        person = split.get("player") or split.get("person") or {}
        pid = person.get("id")
        name = person.get("fullName", "")
        ab = int((split.get("stat") or {}).get("atBats") or 0)
        if pid and ab >= min_ab:
            out.append((pid, name, ab))
    return out


def get_hitter_game_log(pid, season, use_cache=True):
    """Per-game hitting log for one player, date-ascending. Cached raw JSON."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"{pid}_{season}.json")
    if use_cache and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    else:
        url = (f"{STATSAPI}/people/{pid}/stats?stats=gameLog&group=hitting"
               f"&season={season}&gameType=R")
        data = bp._get_json(url)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        time.sleep(0.15)  # be gentle
    splits = data.get("stats", [{}])[0].get("splits", [])
    games = []
    for sp in splits:
        st = sp.get("stat") or {}
        games.append({
            "date": sp.get("date", ""),
            "ab": float(st.get("atBats") or 0),
            "h":  float(st.get("hits") or 0),
            "2b": float(st.get("doubles") or 0),
            "3b": float(st.get("triples") or 0),
            "hr": float(st.get("homeRuns") or 0),
        })
    games.sort(key=lambda g: g["date"])
    return games


def _window_avg_slg(games, lo, hi, d, before):
    """Sum AB/H/2B/3B/HR over games whose date falls in the window, then derive AVG/SLG.
    `before=True` -> strictly BEFORE d (the trailing/recent window); `before=False` ->
    ON OR AFTER d (the forward/outcome window). `lo`/`hi` are day-offsets from d."""
    ab = h = d2 = d3 = hr = 0.0
    for g in games:
        gd = bp._parse_date(g["date"])
        if not gd:
            continue
        delta = (d - gd).days if before else (gd - d).days
        if lo <= delta < hi:
            ab += g["ab"]; h += g["h"]; d2 += g["2b"]; d3 += g["3b"]; hr += g["hr"]
    if ab <= 0:
        return 0.0, 0.0, 0.0
    singles = h - d2 - d3 - hr
    tb = singles + 2 * d2 + 3 * d3 + 4 * hr
    return ab, h / ab, tb / ab


# ------------------------------------------------------------------- main ----
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=DEFAULT_SEASON)
    ap.add_argument("--min-ab", type=int, default=200, help="min season AB for the pool")
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--recency-window", type=int, default=15,
                     help="trailing/forward calendar-day window (also the eval cadence)")
    args = ap.parse_args()
    use_cache = not args.no_cache

    with open(os.path.join("data", "snapshot.json"), encoding="utf-8") as f:
        snap = json.load(f)

    # Season-final skill rows (xBA/xSLG), keyed by loose name -- same simplification the
    # pitcher script already uses for xERA (see module docstring).
    skill_by_name = {}
    for r in snap["hitters"]:
        if int(sd._n(r.get("Dataset")) or 0) == args.season and r.get("PlayerName"):
            skill_by_name.setdefault(bp._nk(r["PlayerName"]), r)

    print(f"Fetching hitter pool (season {args.season}, top {args.limit} by AB)...")
    pool = build_hitter_pool(args.season, args.limit, args.min_ab)
    print(f"  {len(pool)} hitters with >= {args.min_ab} AB")

    avg_bucket = {"declining": bp.Acc(), "improving": bp.Acc(), "noise": bp.Acc()}
    slg_bucket = {"declining": bp.Acc(), "improving": bp.Acc(), "noise": bp.Acc()}
    avg_pairs, slg_pairs = [], []   # (signed severity, residual) for declining/improving only
    matched = scored = 0
    fwd_ab_floor = sd._HIT_RECENCY_MIN_AB  # reuse production's own reliability floor

    for i, (pid, name, ab_szn) in enumerate(pool, 1):
        season_row = skill_by_name.get(bp._nk(name))
        if season_row is None:
            continue
        xba, xslg = sd._n(season_row.get("xBA")), sd._n(season_row.get("xSLG"))
        if xba <= 0 or xslg <= 0:
            continue
        matched += 1
        try:
            games = get_hitter_game_log(pid, args.season, use_cache)
        except Exception as e:
            print(f"  [{i}/{len(pool)}] {name}: log FAILED ({e})")
            continue
        if not games:
            continue

        first_d = bp._parse_date(games[0]["date"])
        if not first_d:
            continue
        next_eval = first_d  # advances by recency_window each time a read fires
        W = args.recency_window
        for g in games:
            d = bp._parse_date(g["date"])
            if not d or d < next_eval:
                continue
            tr_ab, tr_avg, tr_slg = _window_avg_slg(games, 0, W, d, before=True)
            if tr_ab < sd._HIT_RECENCY_MIN_AB:
                continue
            recent_row = {"AVG": tr_avg, "SLG": tr_slg, "AB": tr_ab}
            flag = sd.hitter_recency_flag(season_row, recent_row)
            if flag not in ("declining", "improving", "noise"):
                continue
            _, severity = sd.hitter_recency_severity(season_row, recent_row)
            fw_ab, fw_avg, fw_slg = _window_avg_slg(games, 0, W, d, before=False)
            if fw_ab < fwd_ab_floor:
                next_eval = d + timedelta(days=W)
                continue
            avg_bucket[flag].add(xba, fw_avg)
            slg_bucket[flag].add(xslg, fw_slg)
            if flag in ("declining", "improving"):
                signed = severity if flag == "improving" else -severity
                avg_pairs.append((signed, fw_avg - xba))
                slg_pairs.append((signed, fw_slg - xslg))
            scored += 1
            next_eval = d + timedelta(days=W)
        if i % 25 == 0:
            print(f"  [{i}/{len(pool)}] processed, {scored} reads scored so far")

    # ------------------------------------------------------------- report ----
    print("\n" + "=" * 70)
    print(f"HITTER RECENCY-FLAG BACKTEST  (walk-forward, season {args.season})")
    print(f"{scored} reads scored across {matched} hitters with a season xBA/xSLG anchor "
          f"(window={args.recency_window}d, min recent AB={sd._HIT_RECENCY_MIN_AB})")
    print("Calls sd.hitter_recency_flag / sd.hitter_recency_severity directly -- the exact")
    print("shipped functions, not a parallel copy.")
    print("=" * 70)

    def _report(label, bucket, pairs, target_name):
        n_decl, n_impr, n_noise = bucket["declining"].n, bucket["improving"].n, bucket["noise"].n
        print(f"\n{label}: n declining={n_decl}  improving={n_impr}  noise={n_noise}")
        if n_decl:
            print("  " + bucket["declining"].row("decl."))
        if n_impr:
            print("  " + bucket["improving"].row("impr."))
        if n_noise:
            print("  " + bucket["noise"].row("noise"))
        base_pairs = (bucket["declining"].pairs + bucket["improving"].pairs
                      + bucket["noise"].pairs)
        base_worse = (sum(1 for p, a in base_pairs if a < p) / len(base_pairs)) if base_pairs else 0.0
        if bucket["declining"].pairs:
            dp = bucket["declining"].pairs
            dr = sum(1 for p, a in dp if a < p) / len(dp)
            print(f"  DECLINING: rate(actual {target_name} < season {target_name} anchor)={dr:.1%}  "
                  f"(base={base_worse:.1%}, lift={dr / base_worse if base_worse else 0:.2f}x "
                  f"-- expect CLEARLY >1.0x if the flag confirms real continued weakness)")
        if bucket["improving"].pairs:
            ip_ = bucket["improving"].pairs
            ir = sum(1 for p, a in ip_ if a > p) / len(ip_)
            base_better = 1.0 - base_worse
            print(f"  IMPROVING: rate(actual {target_name} > season {target_name} anchor)={ir:.1%}  "
                  f"(base={base_better:.1%}, lift={ir / base_better if base_better else 0:.2f}x "
                  f"-- expect CLEARLY >1.0x if the flag confirms real continued strength)")
        if len(pairs) >= 40:
            r = bp._pearson(pairs)
            print(f"  signed severity vs residual: n={len(pairs)}  Pearson r={r:+.3f}  "
                  f"(want CLEARLY positive -- declining should predict a negative residual, "
                  f"improving a positive one)")
        else:
            print(f"  (only {len(pairs)} flagged reads -- need >=40; run without --limit or "
                  f"lower --min-ab.)")

    _report("AVG", avg_bucket, avg_pairs, "AVG")
    _report("SLG", slg_bucket, slg_pairs, "SLG")

    print("\n" + "=" * 70)
    print("Read this the same way the pitcher backtest was read: if DECLINING's lift is")
    print("clearly <1.0x (or IMPROVING's is <1.0x) and/or the Pearson r is negative, the flag")
    print("is INVERTED like pitcher_recency_flag was -- disable hitter_recency_flag/severity")
    print("the same way. If both lifts are clearly >1.0x and r is positive, the flag validates.")


if __name__ == "__main__":
    main()
