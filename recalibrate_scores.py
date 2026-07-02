"""Re-derive the pitcher_score / rp_score calibration constants.

Both role scores are calibrated so the qualified-league distribution maps
p50 -> 50 and p90 -> 80 on the shared 0-100 scale. When the raw component mix
changes (e.g. adding xERA / whiff% / contact-allowed), the raw distribution
shifts and these constants must be re-derived, or every displayed number moves.

Run:  python recalibrate_scores.py
Then paste the printed constants into pitcher_score / rp_score in send_digest.py.
"""
import json
from send_digest import (pitcher_score, rp_score, _is_sp, _n, YEAR,
                         compute_pitcher_benchmarks, _pit_viable_min,
                         _PIT_BENCH, _IP_RELY_FRAC, _PIT_FALLBACK)


def pctl(sorted_vals, q):
    if not sorted_vals:
        return 0.0
    i = q * (len(sorted_vals) - 1)
    lo = int(i)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (i - lo)


def solve(raws, name, form):
    raws = sorted(raws)
    p50, p90 = pctl(raws, 0.50), pctl(raws, 0.90)
    A = 30.0 / (p90 - p50)          # calibrated = A*raw + C ; A*p50+C=50, A*p90+C=80
    C = 50.0 - A * p50
    print(f"\n{name}: n={len(raws)}  raw p50={p50:.2f}  p90={p90:.2f}")
    print(f"  A = {A:.4f}   C = {C:.4f}")
    if form == "minus":
        print(f"  -> s = s * {A:.4f} - {(-C):.4f}")
    else:
        print(f"  -> s = s * {A:.4f} + {C:.4f}")
    # sanity spot-check
    for label, q in [("p10", .10), ("p50", .50), ("p90", .90), ("p99", .99)]:
        rv = pctl(raws, q)
        print(f"     {label}: raw {rv:6.2f} -> {max(0, min(100, round(A*rv+C)))}")


def main():
    d = json.load(open("data/snapshot.json", encoding="utf-8"))
    # Match send_digest's dynamic, role-relative volume thresholds so the qualified
    # population tracks the season (mirrors _ip_reliability_mult / _pit_viable_min).
    compute_pitcher_benchmarks(d["pitchers"])
    ps = [r for r in d["pitchers"] if int(r.get("Dataset", 0) or 0) == YEAR]
    # SP qualification = the same small-sample reliability floor pitcher_score applies
    # (_IP_RELY_FRAC of the season SP leader); RP uses the positional viability floors.
    sp_ip_min = (_PIT_BENCH.get((YEAR, "SP"), {}).get("IP") or 0) * _IP_RELY_FRAC \
                or _PIT_FALLBACK["IP_RELY"]

    sp_raw = [pitcher_score(r, _raw=True) for r in ps
              if _is_sp(r) and _n(r.get("IP")) >= sp_ip_min]
    rp_raw = [rp_score(r, _raw=True) for r in ps
              if not _is_sp(r) and (_n(r.get("ESPN_GP")) >= _pit_viable_min("RP", "GP")
                                    or _n(r.get("IP")) >= _pit_viable_min("RP", "IP"))]

    solve(sp_raw, "pitcher_score (SP path)", "minus")
    solve(rp_raw, "rp_score", "plus")


if __name__ == "__main__":
    main()
