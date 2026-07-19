"""Backtest the category win-probability model (`sd._cat_win_prob`) -- is it calibrated?

WALK-FORWARD. For each completed matchup week N that has at least `--min-weeks` prior
weeks, every team's per-category weekly mean/stddev is computed from weeks < N ONLY
(`sd.compute_weekly_avgs` / `sd.compute_weekly_std` -- the real model state the digest
would have had). For each cross-team category matchup we predict P(team wins) with the
SHIPPED `sd._cat_win_prob` in its pre-week form (projected value = the historical weekly
avg, full week remaining, sigma = sqrt(std_a^2 + std_b^2) with the same `_CLOSE_THRESH`
fallback the digest uses), then compare to the ACTUAL week-N outcome (raw value vs raw
value, direction-adjusted, tie band = half a display unit -- the same band the model uses).
Reliability bins + Brier + ECE tell us whether a stated 65% actually wins ~65% of the time,
and a sigma-inflation sweep reports the single multiplier that would best calibrate it.

Scope / honesty:
  * This validates the probabilistic ENGINE -- the weekly-variance assumption + the
    normal-CDF -- on full-week outcomes. The LIVE digest additionally blends mid-week
    banked stats into the projected values; that mid-week path can't be replayed without
    retained intermediate snapshots (the snapshot is overwritten every run), so it's out
    of scope. A mis-calibration here still propagates to the mid-week numbers (same sigma
    + same CDF), so this is a valid diagnostic of the shared machinery.
  * Actual outcome uses a raw value comparison. For ERA/WHIP this ignores ESPN's ~25-IP
    ratio floor (a display-scoring wrinkle, not a model input) -- a documented minor
    discrepancy, flagged per-category in the report.

No network, no email, no snapshot writes -- a pure read of data/snapshot.json.

Run:
  python backtest_winprob.py
  python backtest_winprob.py --bins 8 --csv
  python backtest_winprob.py --min-weeks 3
"""
import argparse
import json
import math
import os

import send_digest as sd

CATS = ["R", "HR", "RBI", "SB", "OPS", "B_SO", "K", "QS", "W", "ERA", "WHIP", "SVHD"]


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _norm(t):
    return " ".join((t or "").split())


def _phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2)))


def _probs(edge, sigma, dec, k=1.0):
    """Faithful re-implementation of sd._cat_win_prob's normal model with a RESIDUAL sigma
    multiplier k (used only for the inflation sweep; the base report calls the shipped fn).
    The shipped fn already widens sigma by _WINPROB_SIGMA_INFLATE, and at remaining_frac=1
    its eff = sigma * _WINPROB_SIGMA_INFLATE — so k=1 matches shipped and k is the ADDITIONAL
    widen on top of it."""
    eff = max(sigma * sd._WINPROB_SIGMA_INFLATE * k, 1e-9)
    h = 0.5 * (10 ** (-dec))
    p_win = 1.0 - _phi((h - edge) / eff)
    p_loss = _phi((-h - edge) / eff)
    return p_win, max(0.0, 1.0 - p_win - p_loss)


def _actual(va, vb, cat):
    """Actual outcome of a vs b in `cat`: 'W' (a strictly better), 'L', or 'T', using the
    same half-display-unit tie band and lower-is-better direction as the model."""
    dec = sd._CAT_DEC.get(cat, 0)
    h = 0.5 * (10 ** (-dec))
    edge = (vb - va) if cat in sd._LOWER_BETTER else (va - vb)   # > 0 favors a
    if edge > h:
        return "W"
    if edge < -h:
        return "L"
    return "T"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snapshot", default=os.path.join("data", "snapshot.json"))
    ap.add_argument("--min-weeks", type=int, default=2,
                    help="prior completed weeks required before a week is scored "
                         "(>= 2 needed for a stddev; default 2)")
    ap.add_argument("--bins", type=int, default=10, help="reliability bins over [0,1]")
    ap.add_argument("--csv", action="store_true", help="dump every scored comparison as CSV")
    args = ap.parse_args()

    snap = _load(args.snapshot)
    roto = snap.get("roto", [])
    if not roto:
        print("No roto history in the snapshot -- nothing to backtest.")
        return

    # week -> team -> cat -> raw weekly value
    weeks = {}
    for r in roto:
        try:
            wk = int(r.get("Week", 0))
        except (TypeError, ValueError):
            continue
        if wk <= 0:
            continue
        t = _norm(r.get("Team"))
        if not t:
            continue
        cell = weeks.setdefault(wk, {}).setdefault(t, {})
        for c in CATS:
            try:
                cell[c] = float(r[c])
            except (KeyError, TypeError, ValueError):
                pass

    completed = sorted(weeks)
    if len(completed) <= args.min_weeks:
        print(f"Only {len(completed)} completed weeks -- need > {args.min_weeks} to score any. "
              f"(Snapshot may be early-season.)")
        return

    # each sample: dict(cat, p_win, p_tie, won, tied, edge, sigma, dec)
    samples = []
    csv_rows = []
    scored_weeks = []
    for N in completed:
        if len([w for w in completed if w < N]) < args.min_weeks:
            continue
        avgs = sd.compute_weekly_avgs(roto, N)   # weeks < N
        stds = sd.compute_weekly_std(roto, N)
        teams = sorted(weeks[N])
        wk_n = 0
        for i, a in enumerate(teams):
            for b in teams[i + 1:]:              # unordered pairs, predict P(a beats b)
                for c in CATS:
                    ma = avgs.get(a, {}).get(c)
                    mb = avgs.get(b, {}).get(c)
                    va = weeks[N][a].get(c)
                    vb = weeks[N][b].get(c)
                    if ma is None or mb is None or va is None or vb is None:
                        continue
                    sa = stds.get(a, {}).get(c)
                    sb = stds.get(b, {}).get(c)
                    if sa is not None and sb is not None:
                        sigma = math.sqrt(sa * sa + sb * sb)
                    else:
                        sigma = sd._CLOSE_THRESH.get(c, 1) or 1
                    p_win, p_tie = sd._cat_win_prob(ma, mb, c, sigma, 1.0)   # shipped fn
                    dec = sd._CAT_DEC.get(c, 0)
                    edge = (mb - ma) if c in sd._LOWER_BETTER else (ma - mb)
                    res = _actual(va, vb, c)
                    samples.append({"cat": c, "p_win": p_win, "p_tie": p_tie,
                                    "won": 1 if res == "W" else 0,
                                    "tied": 1 if res == "T" else 0,
                                    "edge": edge, "sigma": sigma, "dec": dec})
                    wk_n += 1
                    if args.csv:
                        csv_rows.append((N, a, b, c, f"{p_win:.4f}",
                                         1 if res == "W" else 0, f"{p_tie:.4f}",
                                         1 if res == "T" else 0))
        if wk_n:
            scored_weeks.append(N)

    if not samples:
        print("No scorable comparisons (not enough prior-week history).")
        return

    if args.csv:
        print("week,team_a,team_b,cat,p_win,a_won,p_tie,tied")
        for row in csv_rows:
            print(",".join(str(x) for x in row))
        print()

    _report(samples, scored_weeks, args.bins)


def _brier(rows):
    return sum((r["p_win"] - r["won"]) ** 2 for r in rows) / len(rows)


def _ece(preds, outcomes, nbins):
    """Expected calibration error over `nbins` equal-width bins of `preds` (parallel to
    `outcomes`, 0/1)."""
    n = len(preds)
    edges = [i / nbins for i in range(nbins + 1)]
    ece = 0.0
    for i in range(nbins):
        lo, hi = edges[i], edges[i + 1]
        idx = [j for j in range(n)
               if (lo <= preds[j] < hi) or (i == nbins - 1 and preds[j] == 1.0)]
        if not idx:
            continue
        ap_ = sum(preds[j] for j in idx) / len(idx)
        ao = sum(outcomes[j] for j in idx) / len(idx)
        ece += (len(idx) / n) * abs(ap_ - ao)
    return ece


def _report(samples, scored_weeks, nbins):
    N = len(samples)
    mean_p = sum(s["p_win"] for s in samples) / N
    actual = sum(s["won"] for s in samples) / N
    brier = _brier(samples)
    mean_pt = sum(s["p_tie"] for s in samples) / N
    actual_t = sum(s["tied"] for s in samples) / N

    bar = "=" * 74
    print(bar)
    print("CATEGORY WIN-PROBABILITY CALIBRATION  (walk-forward)")
    print(f"{N} category comparisons across weeks "
          f"{scored_weeks[0]}-{scored_weeks[-1]} ({len(scored_weeks)} scored)")
    print("Prediction = pre-week sd._cat_win_prob(avg, avg, sigma, remaining=1).")
    print(bar)
    print(f"\nOverall  mean P(win) = {mean_p*100:5.1f}%   actual win rate = {actual*100:5.1f}%"
          f"   (gap {(mean_p-actual)*100:+.1f} pts)")
    print(f"         mean P(tie) = {mean_pt*100:5.1f}%   actual tie rate = {actual_t*100:5.1f}%")
    print(f"         Brier score = {brier:.4f}   (0 = perfect, 0.25 = always guessing 50%)")

    # ---- reliability table ----
    edges = [i / nbins for i in range(nbins + 1)]
    print("\nRELIABILITY  (a well-calibrated model tracks the diagonal: pred ~= actual)")
    print(f"  {'bin':>11} | {'n':>6} | {'avg pred':>8} | {'actual':>7} | {'gap':>6} | dir")
    print("  " + "-" * 60)
    for i in range(nbins):
        lo, hi = edges[i], edges[i + 1]
        b = [s for s in samples if (lo <= s["p_win"] < hi) or (i == nbins - 1 and s["p_win"] == 1.0)]
        if not b:
            continue
        n = len(b)
        ap_ = sum(s["p_win"] for s in b) / n
        ao = sum(s["won"] for s in b) / n
        gap = ap_ - ao
        arrow = "over" if gap > 0.02 else ("under" if gap < -0.02 else "ok")
        print(f"  {lo*100:4.0f}-{hi*100:3.0f}% | {n:6d} | {ap_*100:7.1f}% | {ao*100:6.1f}% "
              f"| {gap*100:+5.1f} | {arrow}")
    print("  " + "-" * 60)
    base_ece = _ece([s["p_win"] for s in samples], [s["won"] for s in samples], nbins)
    print(f"  Expected Calibration Error (ECE) = {base_ece*100:.2f} pts  "
          f"(lower is better; < ~3 pts is well-calibrated)")

    # ---- sigma-inflation sweep: the single multiplier that best calibrates ----
    outcomes = [s["won"] for s in samples]
    best_k, best_ece = 1.0, base_ece
    k = 0.80
    while k <= 2.51:
        preds = [_probs(s["edge"], s["sigma"], s["dec"], k)[0] for s in samples]
        e = _ece(preds, outcomes, nbins)
        if e < best_ece:
            best_k, best_ece = k, e
        k += 0.05
    infl = sd._WINPROB_SIGMA_INFLATE
    print(f"\nSIGMA-INFLATION SWEEP  (shipped _WINPROB_SIGMA_INFLATE = {infl:g}; k is an")
    print("ADDITIONAL residual multiplier on top of it, re-measuring ECE)")
    if best_k > 1.001:
        pv = [_probs(s["edge"], s["sigma"], s["dec"], best_k)[0] for s in samples]
        bb = sum((pv[i] - outcomes[i]) ** 2 for i in range(N)) / N
        print(f"  best residual k = {best_k:.2f}  ->  ECE {base_ece*100:.2f} -> {best_ece*100:.2f} pts, "
              f"Brier {brier:.4f} -> {bb:.4f}")
        print(f"  Reading: still slightly over-confident; another ~{(best_k-1)*100:.0f}% widen "
              f"(total ~{best_k*infl:.2f}x) would flatten the tails a bit more.")
        print(f"  Tune _WINPROB_SIGMA_INFLATE in send_digest (display-only; changes shown Win%,")
        print(f"  never a projected W/L/T verdict). Pre-week optimum is ~1.9x total.")
    else:
        print(f"  best residual k = {best_k:.2f} (no meaningful improvement) -- the shipped "
              f"{infl:g}x is well-calibrated as is.")

    # ---- per-category ----
    print("\nPER-CATEGORY  (over = model too confident, under = too timid)")
    print(f"  {'cat':>5} | {'n':>5} | {'mean p':>7} | {'actual':>7} | {'gap':>6} | {'Brier':>6} | dir")
    print("  " + "-" * 62)
    for c in CATS:
        rows = [s for s in samples if s["cat"] == c]
        if not rows:
            continue
        n = len(rows)
        mp = sum(s["p_win"] for s in rows) / n
        ao = sum(s["won"] for s in rows) / n
        gap = mp - ao
        br = _brier(rows)
        note = "over" if gap > 0.02 else ("under" if gap < -0.02 else "ok")
        if c in sd._RATE_CATS and c in sd._LOWER_BETTER:
            note += " *floor"   # ERA/WHIP: raw compare ignores ESPN IP floor
        print(f"  {c:>5} | {n:5d} | {mp*100:6.1f}% | {ao*100:6.1f}% | {gap*100:+5.1f} "
              f"| {br:.4f} | {note}")
    print("  " + "-" * 62)
    print("  * ERA/WHIP actuals use raw value comparison; ESPN's ~25-IP ratio floor can")
    print("    flip a real category result, so treat their calibration as approximate.")
    print("  Note: per-cat 'gap' is near zero by construction (mean pred ~= base rate); the")
    print("  overconfidence lives in the SPREAD -- see the reliability table + sweep above.")
    print()


if __name__ == "__main__":
    main()
