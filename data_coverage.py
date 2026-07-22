"""Data-coverage / degradation report for data/snapshot.json (stdlib-only, no deps).

The digest leans on 5+ flaky sources (FantasyPros HTML scrape, ESPN API, MLB StatsAPI,
Baseball Savant via pybaseball). `snapshot_schema.py` guards the STRUCTURAL contract
(missing keys, empty core lists, NaN-sentinel leaks). This tool is the complementary
SEMANTIC check: how well did the enrichment merges actually populate the fields that feed
scoring? A silent drop in Statcast coverage (say 92% -> 60%) passes structural validation
but quietly worsens every score -- this surfaces it so you know when to trust the numbers
less.

`coverage_report(snap)` -> a structured dict; `format_report(...)` -> an ASCII console
report; `worst_status(...)` -> "OK"/"WARN"/"LOW" for CI. Presence test is sentinel-aware:
a field counts as present when it's a real positive number (the pipeline stores missing
enrichment as the -1 sentinel or 0, and every Statcast value here is positive when known).

CLI:
  python data_coverage.py                 # report on data/snapshot.json
  python data_coverage.py path/to.json    # another snapshot
  python data_coverage.py --strict        # exit 1 if any group is LOW (for CI gating)
"""
import json
import os
import sys
from datetime import datetime, timezone

YEAR = 2026

# Statcast/model enrichment fields whose coverage degrades silently when a source flakes.
# (SVHD-type fields are population-limited -- only relievers with saves/holds have them --
#  so they are NOT coverage metrics and are deliberately excluded.)
_PITCHER_SAVANT = ["xERA", "xwOBA_against", "WhiffPctile", "BarrelPctAllowed", "HardHitPctAllowed"]
_HITTER_SAVANT  = ["xwOBA", "xBA", "xSLG", "SprintSpeed", "Barrel_Pct", "HardHit_Pct"]
_HITTER_MODEL   = ["HR_Probability", "wRCplus"]

# FA_Matched: True when a free-agent row was actually returned by ESPN's free_agents() pull
# (fetch_data.py's _FA_PULL_SIZE), so its FreeAgentInjuryStatus is a real, checked status
# rather than a default blank that reads as "healthy". A drop here means either the FA pull
# size regressed or the ESPN endpoint is degraded -- either way, pickup suggestions may be
# missing an injury flag (the Blaze Alexander bug this group exists to catch).
_FA_STATUS = ["FA_Matched"]

_HEALTHY = 0.85   # >= this fraction present -> OK
_WARN    = 0.70   # >= this -> WARN; below -> LOW

_RECENT_WINDOWS = [7, 15, 30]   # FantasyPros short-range datasets
_RECENT_WARN    = 120           # < this many rows in a window -> WARN
_RECENT_LOW     = 40            # < this -> LOW (scrape likely failed)


def _present(v):
    """Sentinel-aware presence: a real positive number (missing enrichment is stored as the
    -1 sentinel or 0; NaN is treated as absent)."""
    if v is None or v == "":
        return False
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return f == f and f > 0   # f == f rejects NaN


def _status_frac(frac):
    return "OK" if frac >= _HEALTHY else ("WARN" if frac >= _WARN else "LOW")


def _yrows(snap, key):
    return [r for r in snap.get(key, []) if int(r.get("Dataset", 0) or 0) == YEAR]


def _yrows_fa(snap, key):
    """YEAR rows for the free-agent pool only (unrostered -- FantasyTeam blank)."""
    return [r for r in _yrows(snap, key) if not str(r.get("FantasyTeam") or "").strip()]


def _field_cov(rows, field):
    n = len(rows)
    present = sum(1 for r in rows if _present(r.get(field)))
    frac = (present / n) if n else 0.0
    return {"present": present, "total": n, "frac": frac, "status": _status_frac(frac) if n else "n/a"}


def _group(rows, fields):
    out = {f: _field_cov(rows, f) for f in fields}
    worst = "OK"
    order = {"OK": 0, "WARN": 1, "LOW": 2, "n/a": 0}
    for f in fields:
        if order.get(out[f]["status"], 0) > order[worst]:
            worst = out[f]["status"]
    return {"fields": out, "status": worst}


def _freshness(snap):
    iso = snap.get("refreshed_at")
    try:
        dt = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return {"iso": iso, "age_h": None, "status": "n/a"}
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    age_h = (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
    status = "OK" if age_h < 18 else ("WARN" if age_h < 30 else "LOW")
    return {"iso": iso, "age_h": age_h, "status": status}


def coverage_report(snap):
    """Compute the full coverage report as a structured dict."""
    pit_y = _yrows(snap, "pitchers")
    hit_y = _yrows(snap, "hitters")

    fa_y = _yrows_fa(snap, "pitchers") + _yrows_fa(snap, "hitters")

    # Savant coverage measures whether the enrichment merges populated Statcast for players
    # that COULD have it. Off-FP players seeded from ESPN (Source=="ESPN", fresh call-ups /
    # low-owned bats) are legitimately below Savant's qualifier minimums and lack the
    # HR_Probability/wRC+ Statcast inputs, so excluding them from BOTH the pitcher- and
    # hitter-Savant denominators (and the hitter model) keeps a healthy influx of call-ups
    # from false-tripping the degradation badge. fa_status/recent windows stay on the full pool.
    pit_y_fp = [r for r in pit_y if str(r.get("Source") or "") != "ESPN"]
    hit_y_fp = [r for r in hit_y if str(r.get("Source") or "") != "ESPN"]

    rep = {
        "freshness": _freshness(snap),
        "pitcher_savant": _group(pit_y_fp, _PITCHER_SAVANT),
        "hitter_savant": _group(hit_y_fp, _HITTER_SAVANT),
        "hitter_model": _group(hit_y_fp, _HITTER_MODEL),
        "fa_status": _group(fa_y, _FA_STATUS),
        "recent_windows": {},
        "probable_starters": {},
        "optional": {},
        "counts": {"pitchers_year": len(pit_y), "hitters_year": len(hit_y), "fa_pool": len(fa_y)},
    }

    # recent-form windows: row counts per short-range Dataset (pitchers + hitters)
    for w in _RECENT_WINDOWS:
        npit = sum(1 for r in snap.get("pitchers", []) if int(r.get("Dataset", 0) or 0) == w)
        nhit = sum(1 for r in snap.get("hitters", []) if int(r.get("Dataset", 0) or 0) == w)
        tot = npit + nhit
        st = "OK" if tot >= _RECENT_WARN else ("WARN" if tot >= _RECENT_LOW else "LOW")
        rep["recent_windows"][w] = {"pit": npit, "hit": nhit, "total": tot, "status": st}

    # probable starters: informational -- confirmed% legitimately rises through the week
    up = [r for r in snap.get("pitchers", [])
          if r.get("PSP_Date") and str(r.get("PSP_Date")) not in ("1999-01-01", "None")]
    conf = [r for r in up if r.get("PSP_Projected") in (False, "False", 0, "0")]
    rep["probable_starters"] = {
        "upcoming": len(up), "confirmed": len(conf),
        "confirmed_frac": (len(conf) / len(up)) if up else 0.0, "status": "info",
    }

    # batting handedness (platoon reads) -- one statsapi /sports/1/players pull, ~100%
    # coverage; INFO-level (a display nicety, not injury-safety), so it is reported but
    # NOT wired into worst_status/the footer badge -- a drop just means the platoon
    # markers go quiet, never a wrong scoring number.
    n_hit_all = sum(1 for r in snap.get("hitters", []) if int(r.get("Dataset", 0) or 0) == YEAR)
    n_bats = sum(1 for r in hit_y if str(r.get("Bats") or "").upper() in ("L", "R", "S"))
    _bfrac = (n_bats / n_hit_all) if n_hit_all else 0.0
    rep["handedness"] = {
        "present": n_bats, "total": n_hit_all, "frac": _bfrac,
        "status": (_status_frac(_bfrac) if n_hit_all else "n/a"),
    }

    # legitimately-empty optional fields (WARN-level in schema, never an error)
    rep["optional"] = {
        "todays_games": len(snap.get("todays_games", [])),
        "pending_trades": len(snap.get("pending_trades", [])),
    }
    return rep


def worst_status(rep):
    """The worst status across the gating groups (freshness + coverage + recent windows)."""
    order = {"OK": 0, "n/a": 0, "info": 0, "WARN": 1, "LOW": 2}
    worst = "OK"
    keys = [rep["freshness"]["status"], rep["pitcher_savant"]["status"],
            rep["hitter_savant"]["status"], rep["hitter_model"]["status"],
            rep["fa_status"]["status"]]
    keys += [w["status"] for w in rep["recent_windows"].values()]
    for s in keys:
        if order.get(s, 0) > order[worst]:
            worst = s
    return worst


def _bar(f):
    filled = int(round(f * 20))
    return "#" * filled + "." * (20 - filled)


def format_report(rep):
    L = []
    bar = "=" * 68
    L.append(bar)
    L.append("DATA COVERAGE / DEGRADATION REPORT")
    fr = rep["freshness"]
    age = f"{fr['age_h']:.1f}h ago" if fr["age_h"] is not None else "unknown age"
    L.append(f"snapshot refreshed {age}  [{fr['status']}]   ({fr['iso']})")
    L.append(f"YEAR rows: {rep['counts']['pitchers_year']} pitchers, "
             f"{rep['counts']['hitters_year']} hitters")
    L.append(bar)

    def _grp(title, g):
        L.append(f"\n{title}  [{g['status']}]")
        for f, c in g["fields"].items():
            if c["status"] == "n/a":
                L.append(f"  {f:20} (no rows)")
                continue
            L.append(f"  {f:20} {_bar(c['frac'])} {c['frac']*100:5.1f}%  "
                     f"({c['present']}/{c['total']})  {c['status']}")

    _grp("PITCHER Statcast (Baseball Savant)", rep["pitcher_savant"])
    _grp("HITTER Statcast (Baseball Savant)", rep["hitter_savant"])
    _grp("HITTER model fields", rep["hitter_model"])
    _grp(f"FA INJURY-STATUS MATCH ({rep['counts']['fa_pool']} free-agent rows -- "
         f"was ESPN's free_agents() pull actually checked?)", rep["fa_status"])

    L.append("\nRECENT-FORM WINDOWS (FantasyPros short-range row counts)")
    for w, c in rep["recent_windows"].items():
        L.append(f"  {w:>2}-day  pit {c['pit']:>3} + hit {c['hit']:>3} = {c['total']:>3} rows  {c['status']}")

    ps = rep["probable_starters"]
    L.append(f"\nPROBABLE STARTERS (info -- confirmed% rises through the week)")
    L.append(f"  {ps['confirmed']}/{ps['upcoming']} upcoming starts confirmed by MLB "
             f"({ps['confirmed_frac']*100:.0f}%); the rest are rotation-walk projections")

    hd = rep["handedness"]
    if hd["total"]:
        L.append(f"\nBATTING HANDEDNESS (platoon reads -- info, not gated)")
        L.append(f"  {hd['present']}/{hd['total']} hitters with a bat-hand "
                 f"({hd['frac']*100:.0f}%)  [{hd['status']}]")

    op = rep["optional"]
    L.append(f"\nOPTIONAL (legitimately 0 on an off-day / quiet week)")
    L.append(f"  todays_games={op['todays_games']}  pending_trades={op['pending_trades']}")

    ws = worst_status(rep)
    L.append("\n" + "-" * 68)
    if ws == "OK":
        L.append("VERDICT: OK -- enrichment coverage is healthy; trust the numbers.")
    elif ws == "WARN":
        L.append("VERDICT: WARN -- some coverage is below its healthy band. Numbers are")
        L.append("         usable but a source may be partially degraded; spot-check.")
    else:
        L.append("VERDICT: LOW -- a source likely failed this run. Scores are degraded;")
        L.append("         consider re-running the fetch before trusting the digest.")
    L.append("-" * 68)
    return "\n".join(L)


def main():
    args = [a for a in sys.argv[1:]]
    strict = "--strict" in args
    args = [a for a in args if a != "--strict"]
    path = args[0] if args else os.path.join("data", "snapshot.json")
    try:
        with open(path, encoding="utf-8") as f:
            snap = json.load(f)
    except (OSError, ValueError) as e:
        print(f"Could not read snapshot at {path}: {e}")
        sys.exit(2)
    rep = coverage_report(snap)
    print(format_report(rep))
    if strict and worst_status(rep) == "LOW":
        sys.exit(1)


if __name__ == "__main__":
    main()
