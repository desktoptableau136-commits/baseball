"""Backtest the starting-pitcher projected line (IP / ER / K per start).

Honest WALK-FORWARD: every past start is projected using only the pitcher's
cumulative stats through the day BEFORE that start, then compared to what he
actually did. It reuses the REAL projection code (`send_digest._proj_line_vals`
+ `_proj_is_qs`) so we measure the exact formula the digest ships, not a copy.

Data: MLB StatsAPI per-pitcher game logs (same host/requests pattern as
fetch_data.get_opponent_ops). No email, no snapshot writes.

Run:
  python backtest_projections.py                 # broad starter set, cached
  python backtest_projections.py --limit 30 --no-cache   # quick smoke test
  python backtest_projections.py --csv           # dump per-start rows
  python backtest_projections.py --recency-metric kwera --recency-window 60
      # sweep the recency-vs-xERA diagnostic (see fantasy.scoring's pitcher_bounceback_flag /
      # disabled pitcher_recency_flag docstrings for what this validated and why) with a
      # different recent-window metric [era|fip|kwera] / calendar-day window / gap threshold

Simplification (noted in the report): the pitcher's own line is strictly
walk-forward, but the OPPONENT OPS/K adjustment uses each team's full-season
offense (season-stable, and the formula clamps that factor to +/-15-20% anyway).
"""
import argparse
import json
import math
import os
import time
from datetime import date as _date
import requests

import send_digest as sd

STATSAPI = "https://statsapi.mlb.com/api/v1"
CACHE_DIR = os.path.join("data", "backtest_cache")
DEFAULT_SEASON = sd.YEAR


# ---------------------------------------------------------------- helpers ----
def _ip_to_dec(ip):
    """MLB game-log innings notation -> decimal. '6.1'->6.333, '6.2'->6.667, '6.0'->6.0."""
    try:
        s = str(ip)
        if "." in s:
            whole, frac = s.split(".", 1)
            return int(whole) + int(frac[0]) / 3.0
        return float(s)
    except (ValueError, TypeError):
        return 0.0


def _get_json(url, timeout=20):
    return requests.get(url, timeout=timeout).json()


# ---- LEGACY projection (pre-port raw-ERA formula) --------------------------
# Frozen copy of the OLD _proj_line_vals (raw ERA, no regression) so every run
# shows a live legacy-vs-ported comparison even after send_digest changes.
def _proj_legacy(era, kip, ip_per_g, opp_ops, opp_k, hva, lg_ops, lg_k):
    ip = min(ip_per_g, 7.5)
    if ip <= 0:
        return None
    opp_factor = min(1.20, max(0.80, opp_ops / lg_ops)) if opp_ops > 0 else 1.0
    park_factor = 0.97 if hva.startswith("vs ") else (1.03 if hva.startswith("@ ") else 1.0)
    k_factor = min(1.15, max(0.85, opp_k / lg_k)) if opp_k > 0 else 1.0
    raw_er = era * ip / 9 if era > 0 else 0
    er = round(raw_er * opp_factor * park_factor)
    k = round(kip * ip * k_factor) if kip > 0 else 0
    return ip, er, k


def _parse_date(s):
    try:
        y, m, d = str(s).split("-")
        return _date(int(y), int(m), int(d))
    except (ValueError, TypeError):
        return None


_FIP_CONSTANT = 3.10  # standard-ish MLB FIP constant; only used for a relative recent-vs-season
                       # comparison, so a fixed reasonable value (not season-recalibrated) is fine


def _fip(hr, bb, hbp, k, ip):
    """Fielding-Independent Pitching: only counts K/BB/HBP/HR (outcomes a pitcher controls
    independent of defense/BABIP/strand-rate luck), unlike raw ERA which is dominated by
    sequencing variance over a small sample. Candidate FIX #1 for the recency flag's
    inverted-direction bug (see pitcher_recency_flag's DISABLED docstring)."""
    if ip <= 0:
        return 0.0
    return (13.0 * hr + 3.0 * (bb + hbp) - 2.0 * k) / ip + _FIP_CONSTANT


def _kwera(k, bb, bf):
    """kwERA: ERA-scale estimate from K% and BB% ONLY (no HR term) -- excludes the
    lowest-frequency, highest-variance FIP component (HR is a rare event; over 20-30 IP its
    rate is itself extremely noisy). Candidate FIX #2: tests whether FIP's reversal was really
    about HR-rate noise specifically, not BABIP/strand-rate in general."""
    if bf <= 0:
        return 0.0
    return 5.40 - 12.0 * (k / bf) + 10.0 * (bb / bf)


def _nk(name):
    """Loose name key for matching MLB fullName -> snapshot PlayerName (accent/punct/lower)."""
    import unicodedata
    s = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode()
    return "".join(ch for ch in s.lower() if ch.isalnum() or ch == " ").strip()


def _is_blowup(ip, er):
    """A disaster start: the ER/WHIP-wrecking outing the RISK flag is meant to predict."""
    return er >= 5 or (er >= 4 and ip < 3)


def _auc(pairs):
    """AUC (prob a random blowup outscores a random clean start) via rank-sum. pairs=(score,label)."""
    pos = [s for s, y in pairs if y]
    neg = [s for s, y in pairs if not y]
    if not pos or not neg:
        return 0.5
    ranked = sorted(pairs, key=lambda p: p[0])
    # average ranks (1-based), handling ties
    ranks = {}
    i = 0
    while i < len(ranked):
        j = i
        while j + 1 < len(ranked) and ranked[j + 1][0] == ranked[i][0]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[id(ranked[k])] = avg
        i = j + 1
    sum_pos = sum(ranks[id(p)] for p in ranked if p[1])
    n_pos, n_neg = len(pos), len(neg)
    return (sum_pos - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def _pearson(pairs):
    n = len(pairs)
    if n < 2:
        return 0.0
    sx = sum(a for a, _ in pairs)
    sy = sum(b for _, b in pairs)
    sxx = sum(a * a for a, _ in pairs)
    syy = sum(b * b for _, b in pairs)
    sxy = sum(a * b for a, b in pairs)
    dx = n * sxx - sx * sx
    dy = n * syy - sy * sy
    if dx <= 0 or dy <= 0:
        return 0.0
    return (n * sxy - sx * sy) / math.sqrt(dx * dy)


# ------------------------------------------------------------ data pulls ----
def build_pitcher_pool(season, limit, min_gs):
    """Top-`limit` MLB pitchers by games started -> [(person_id, name, gs)]."""
    url = (f"{STATSAPI}/stats?stats=season&group=pitching&season={season}"
           f"&sportId=1&gameType=R&playerPool=all&limit={limit}&sortStat=gamesStarted")
    data = _get_json(url)
    out = []
    for split in data.get("stats", [{}])[0].get("splits", []):
        person = split.get("player") or split.get("person") or {}
        pid = person.get("id")
        name = person.get("fullName", "")
        gs = int((split.get("stat") or {}).get("gamesStarted") or 0)
        if pid and gs >= min_gs:
            out.append((pid, name, gs))
    return out


def get_game_log(pid, season, use_cache=True):
    """Per-start game log for one pitcher, date-ascending. Cached raw JSON."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"{pid}_{season}.json")
    if use_cache and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    else:
        url = (f"{STATSAPI}/people/{pid}/stats?stats=gameLog&group=pitching"
               f"&season={season}&gameType=R")
        data = _get_json(url)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        time.sleep(0.15)  # be gentle
    splits = data.get("stats", [{}])[0].get("splits", [])
    games = []
    for sp in splits:
        st = sp.get("stat") or {}
        opp = (sp.get("opponent") or {}).get("name", "")
        games.append({
            "date": sp.get("date", ""),
            "opp": opp,
            "is_home": bool(sp.get("isHome")),
            "gs": int(st.get("gamesStarted") or 0),
            "ip": _ip_to_dec(st.get("inningsPitched")),
            "er": float(st.get("earnedRuns") or 0),
            "k": float(st.get("strikeOuts") or 0),
            "pitches": float(st.get("numberOfPitches") or 0),
            "bf": float(st.get("battersFaced") or 0),
            "hr": float(st.get("homeRuns") or 0),
            "bb": float(st.get("baseOnBalls") or 0),
            "hbp": float(st.get("hitBatsmen") or 0),
        })
    games.sort(key=lambda g: g["date"])
    return games


def get_team_offense(season):
    """{team_name: (season_OPS, season_K_rate)} from the season hitting split."""
    url = (f"{STATSAPI}/teams/stats?season={season}&sportId=1"
           f"&group=hitting&stats=season")
    data = _get_json(url)
    out = {}
    for split in data.get("stats", [{}])[0].get("splits", []):
        st = split.get("stat") or {}
        name = (split.get("team") or {}).get("name", "")
        ops = st.get("ops")
        if name and ops is not None:
            try:
                so = float(st.get("strikeOuts") or 0)
                pa = float(st.get("plateAppearances") or 0)
                krate = round(so / pa, 4) if pa > 0 else -1.0
            except (TypeError, ValueError):
                krate = -1.0
            out[name] = (float(ops), krate)
    return out


# ---------------------------------------------------------------- metrics ----
class Acc:
    """Accumulates errors for one projected metric."""
    def __init__(self):
        self.n = 0
        self.abs_sum = 0.0
        self.sq_sum = 0.0
        self.bias_sum = 0.0
        self.pairs = []

    def add(self, proj, actual):
        d = proj - actual
        self.n += 1
        self.abs_sum += abs(d)
        self.sq_sum += d * d
        self.bias_sum += d
        self.pairs.append((proj, actual))

    def row(self, label):
        if self.n == 0:
            return f"  {label:<5}  (no data)"
        mae = self.abs_sum / self.n
        rmse = math.sqrt(self.sq_sum / self.n)
        bias = self.bias_sum / self.n
        r = _pearson(self.pairs)
        return (f"  {label:<5} n={self.n:<5} MAE={mae:5.2f}  RMSE={rmse:5.2f}  "
                f"bias={bias:+5.2f}  r={r:+.2f}")


class Clf:
    """Binary-classifier accuracy for a badge (predicted vs actual)."""
    def __init__(self):
        self.tp = self.fp = self.tn = self.fn = 0

    def add(self, pred, actual):
        if pred and actual:
            self.tp += 1
        elif pred and not actual:
            self.fp += 1
        elif not pred and actual:
            self.fn += 1
        else:
            self.tn += 1

    def row(self, label):
        n = self.tp + self.fp + self.tn + self.fn
        if n == 0:
            return f"  {label:<6} (no data)"
        acc = (self.tp + self.tn) / n
        prec = self.tp / (self.tp + self.fp) if (self.tp + self.fp) else 0.0
        rec = self.tp / (self.tp + self.fn) if (self.tp + self.fn) else 0.0
        return (f"  {label:<6} acc={acc:.0%}  precision={prec:.0%}  recall={rec:.0%}  "
                f"(TP={self.tp} FP={self.fp} FN={self.fn} TN={self.tn})")


# ------------------------------------------------------------------- main ----
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=DEFAULT_SEASON)
    ap.add_argument("--min-gs", type=int, default=10)
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--warmup", type=float, default=20.0,
                    help="min prior IP before a start is scored")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--csv", action="store_true")
    ap.add_argument("--recency-window", type=int, default=30,
                    help="trailing calendar days for the recency experiment")
    ap.add_argument("--recency-gap", type=float, default=1.50,
                    help="recent-metric-vs-xERA gap threshold to flag declining/improving")
    ap.add_argument("--recency-metric", choices=["era", "fip", "kwera"], default="fip",
                    help="which recent-window metric to test against season xERA")
    args = ap.parse_args()
    use_cache = not args.no_cache

    # Populate _LG exactly like the digest, so opp/park denominators match production.
    with open(os.path.join("data", "snapshot.json"), encoding="utf-8") as f:
        snap = json.load(f)
    try:
        sd.compute_ab_benchmarks(snap["hitters"])
        sd.compute_pitcher_benchmarks(snap["pitchers"])
    except Exception:
        pass
    try:
        sd.compute_score_calibration(snap["pitchers"])
    except Exception:
        pass
    sd.compute_league_averages(snap["hitters"], snap["pitchers"])
    print(f"_LG team_ops={sd._LG.get('team_ops')}  team_k={sd._LG.get('team_k')}")

    # Season-final skill rows (xERA / WHIP / K% / HardHit) keyed by loose name, for the
    # blowup-RISK validation. Risk is a per-pitcher skill PROPENSITY (not walk-forward), but
    # the recent-form escalator IS walk-forward (trailing-3-start ERA computed from game logs).
    skill_by_name = {}
    for r in snap["pitchers"]:
        if int(sd._n(r.get("Dataset")) or 0) == args.season and r.get("PlayerName"):
            skill_by_name.setdefault(_nk(r["PlayerName"]), r)

    print(f"Fetching pitcher pool (season {args.season}, top {args.limit} by GS)...")
    pool = build_pitcher_pool(args.season, args.limit, args.min_gs)
    print(f"  {len(pool)} starters with >= {args.min_gs} GS")

    print("Fetching team offense (season OPS / K%)...")
    team_off = get_team_offense(args.season)
    print(f"  {len(team_off)} teams")

    # Error accumulators: projection under test vs a naive (no-adjustment) baseline.
    ip_a, er_a, k_a = Acc(), Acc(), Acc()          # LIVE ported sd._proj_line_vals
    er_naive, k_naive = Acc(), Acc()
    ip_l, er_l, k_l = Acc(), Acc(), Acc()          # LEGACY (pre-port raw ERA)
    qs_l, k5_l = Clf(), Clf()
    qs_clf, k5_clf = Clf(), Clf()
    # IP predictors: which prior-cumulative signal best tracks actual outing length?
    ip_pred = {"IP/G": Acc(), "pitches/start": Acc(), "batters/start": Acc(),
               "pitches/inn (eff.)": Acc()}
    # home/away and opponent-OPS-bucket ER breakdowns
    er_home, er_away = Acc(), Acc()
    er_bucket = {"weak": Acc(), "avg": Acc(), "strong": Acc()}

    risk_pairs = []      # (blowup_risk score, actual_blowup 0/1) per scored start
    recency_pairs = []   # (signed FIP-vs-xERA gap, ER residual = actual-projected) per flagged start
    recency_bucket = {"declining": Acc(), "improving": Acc(), "noise": Acc()}
    _RECENCY_GAP = args.recency_gap
    shipped_bucket = {"bounceback": Acc(), "regression": Acc(), "noise": Acc()}  # calls the REAL
    # sd.pitcher_bounceback_flag/_recent_fip directly (not a parallel copy) -- the shipped design
    csv_rows = []
    lg_ops = sd._LG.get("team_ops") or 0.717
    lg_k = sd._LG.get("team_k") or 0.22
    print(f"_LG era={sd._LG.get('era')} (fallback ERA-regression target when xERA absent)")
    scored = 0

    for i, (pid, name, gs) in enumerate(pool, 1):
        try:
            games = get_game_log(pid, args.season, use_cache)
        except Exception as e:
            print(f"  [{i}/{len(pool)}] {name}: log FAILED ({e})")
            continue
        # cumulative-through-prior totals
        c_ip = c_er = c_k = c_pitches = c_bf = 0.0
        c_games = c_starts = 0
        recent = []                       # trailing (date, ip, er, hr, bb, hbp, k, bf) of prior STARTS
        skill_row = skill_by_name.get(_nk(name))
        for g in games:
            prior_ip = c_ip
            # Only SCORE actual starts with enough prior sample.
            if g["gs"] >= 1 and prior_ip >= args.warmup and c_games > 0:
                era = 9.0 * c_er / c_ip if c_ip > 0 else 0.0
                kip = c_k / c_ip if c_ip > 0 else 0.0
                ip_per_g = c_ip / c_games
                opp = g["opp"]
                oo = team_off.get(opp, (0, -1))
                hva = ("vs " if g["is_home"] else "@ ") + opp
                row = {
                    "IP_per_G": min(ip_per_g, 7.5),
                    "ERA": era,
                    "K/IP": kip,
                    "IP": c_ip,          # prior sample -> ERA-regression weight (ported fix)
                    "Team_OPS_Value": oo[0],
                    "Team_K_Value": oo[1],
                    "PSP_HomeVAway": hva,
                }
                vals = sd._proj_line_vals(row)
                if vals is not None:
                    p_ip, p_er, p_k = vals
                    a_ip, a_er, a_k = g["ip"], g["er"], g["k"]
                    ip_a.add(p_ip, a_ip)
                    er_a.add(p_er, a_er)
                    k_a.add(p_k, a_k)
                    # naive baseline: flat season rates, no opp/park factor
                    er_naive.add(round(era * min(ip_per_g, 7.5) / 9), a_er)
                    k_naive.add(round(kip * min(ip_per_g, 7.5)), a_k)
                    # badge classifiers
                    qs_clf.add(sd._proj_is_qs(p_ip, p_er), a_ip >= 6 and a_er <= 3)
                    k5_clf.add(p_k >= 5, a_k >= 5)
                    # breakdowns (ER)
                    (er_home if g["is_home"] else er_away).add(p_er, a_er)
                    if oo[0] > 0:
                        bucket = ("weak" if oo[0] < lg_ops * 0.95
                                  else "strong" if oo[0] > lg_ops * 1.05 else "avg")
                        er_bucket[bucket].add(p_er, a_er)
                    # ---- LEGACY (pre-port raw ERA) for a live before/after ----
                    lv = _proj_legacy(era, kip, ip_per_g, oo[0], oo[1], hva, lg_ops, lg_k)
                    if lv is not None:
                        l_ip, l_er, l_k = lv
                        ip_l.add(l_ip, a_ip)
                        er_l.add(l_er, a_er)
                        k_l.add(l_k, a_k)
                        qs_l.add(sd._proj_is_qs(l_ip, l_er), a_ip >= 6 and a_er <= 3)
                        k5_l.add(l_k >= 5, a_k >= 5)
                    # ---- IP predictors vs actual outing length (r only) ----
                    ip_pred["IP/G"].add(ip_per_g, a_ip)
                    if c_starts > 0:
                        ip_pred["pitches/start"].add(c_pitches / c_starts, a_ip)
                        ip_pred["batters/start"].add(c_bf / c_starts, a_ip)
                    if c_ip > 0:
                        ip_pred["pitches/inn (eff.)"].add(c_pitches / c_ip, a_ip)
                    # ---- BLOWUP-RISK validation (skill propensity + walk-forward L15) ----
                    if skill_row is not None:
                        r3ip = sum(x[1] for x in recent[-3:])
                        r3er = sum(x[2] for x in recent[-3:])
                        rec_era = (9.0 * r3er / r3ip) if r3ip > 0 else None
                        risk = sd.blowup_risk(skill_row, recent_era=rec_era)
                        if risk > 0:
                            risk_pairs.append((risk, 1 if _is_blowup(a_ip, a_er) else 0))
                    # ---- RECENCY-FIP validation (candidate FIX for the disabled ERA-based
                    # pitcher_recency_flag). Trailing-30-calendar-day window (walk-forward: only
                    # starts strictly before this one). Tests recent FIP (K/BB/HBP/HR only --
                    # defense/BABIP/strand-rate independent) vs season xERA, instead of the old
                    # noisy recent-ERA-vs-xERA comparison, against THIS start's residual (actual
                    # ER minus the live opp/park-adjusted projection).
                    if skill_row is not None and vals is not None:
                        xera = sd._n(skill_row.get("xERA"))
                        cutoff = _parse_date(g["date"])
                        r30_ip = r30_er = r30_hr = r30_bb = r30_hbp = r30_k = r30_bf = 0.0
                        if cutoff:
                            for d_str, ip_, er_, hr_, bb_, hbp_, k_, bf_ in recent:
                                d_ = _parse_date(d_str)
                                if d_ and 0 <= (cutoff - d_).days <= args.recency_window:
                                    r30_ip += ip_
                                    r30_er += er_
                                    r30_hr += hr_
                                    r30_bb += bb_
                                    r30_hbp += hbp_
                                    r30_k += k_
                                    r30_bf += bf_
                        if r30_ip > 0 and xera > 0:
                            if args.recency_metric == "fip":
                                recent_val = _fip(r30_hr, r30_bb, r30_hbp, r30_k, r30_ip)
                            elif args.recency_metric == "kwera":
                                recent_val = _kwera(r30_k, r30_bb, r30_bf)
                            else:
                                recent_val = 9.0 * r30_er / r30_ip
                            gap = recent_val - xera
                            rflag = ("declining" if gap >= _RECENCY_GAP
                                      else "improving" if gap <= -_RECENCY_GAP else "noise")
                            if rflag in recency_bucket:
                                recency_bucket[rflag].add(p_er, a_er)
                            if rflag in ("declining", "improving"):
                                signed = gap / _RECENCY_GAP
                                recency_pairs.append((signed, a_er - p_er))
                        # ---- SHIPPED-CODE validation: calls the REAL sd.pitcher_bounceback_flag
                        # / sd._recent_fip (fantasy/scoring.py) directly, not a parallel copy --
                        # same "test the exact formula that ships" standard as the LIVE ER/K/IP
                        # comparison above. Uses the trailing-30-day recent_row already built.
                        if r30_ip > 0:
                            ship_recent_row = {"IP": r30_ip, "HR": r30_hr, "BB": r30_bb, "K": r30_k}
                            ship_flag = sd.pitcher_bounceback_flag(skill_row, ship_recent_row)
                            if ship_flag in shipped_bucket:
                                shipped_bucket[ship_flag].add(p_er, a_er)
                    scored += 1
                    if args.csv:
                        csv_rows.append([name, g["date"], hva, f"{p_ip:.2f}", p_er, p_k,
                                         f"{a_ip:.2f}", int(a_er), int(a_k)])
            # accrue this game into the running totals (starts + relief)
            c_ip += g["ip"]
            c_er += g["er"]
            c_k += g["k"]
            c_pitches += g["pitches"]
            c_bf += g["bf"]
            c_games += 1
            if g["gs"] >= 1:
                c_starts += 1
                recent.append((g["date"], g["ip"], g["er"], g["hr"], g["bb"], g["hbp"], g["k"], g["bf"]))
        if i % 25 == 0:
            print(f"  [{i}/{len(pool)}] processed, {scored} starts scored so far")

    # ------------------------------------------------------------- report ----
    print("\n" + "=" * 70)
    print(f"SP PROJECTED-LINE BACKTEST  (walk-forward, season {args.season})")
    print(f"{scored} starts scored across {len(pool)} pitchers "
          f"(warmup {args.warmup:.0f} prior IP)")
    print("Opponent OPS/K uses season offense (approximation); pitcher line is walk-forward.")
    print("=" * 70)

    print("\nLIVE (ported sd._proj_line_vals) -- projected minus actual:")
    print(ip_a.row("IP"))
    print(er_a.row("ER"))
    print(k_a.row("K"))

    print("\nLEGACY (pre-port raw ERA) -- before/after the ER regression:")
    print(ip_l.row("IP"))
    print(er_l.row("ER"))
    print(k_l.row("K"))

    print("\nvs NAIVE BASELINE (season rates, NO opp/park adjustment):")
    print(er_naive.row("ER"))
    print(k_naive.row("K"))

    print("\nBADGE ACCURACY -- LIVE vs LEGACY:")
    print("  " + qs_clf.row("QS   (live)"))
    print("  " + qs_l.row("QS   (legacy)"))
    print("  " + k5_clf.row("5K+  (live)"))
    print("  " + k5_l.row("5K+  (legacy)"))

    print("\nIP PREDICTORS vs ACTUAL OUTING LENGTH (Pearson r; which signal is most telling?):")
    for label, acc in ip_pred.items():
        r = _pearson(acc.pairs)
        print(f"  {label:<20} n={acc.n:<5} r={r:+.3f}")

    print("\nER BY HOME/AWAY (tests the blanket 0.97/1.03 park factor):")
    print(er_home.row("home"))
    print(er_away.row("away"))

    print("\nER BY OPPONENT OFFENSE (tests the opp-OPS factor):")
    print(er_bucket["weak"].row("weak"))
    print(er_bucket["avg"].row("avg"))
    print(er_bucket["strong"].row("strong"))

    print("\nBLOWUP-RISK FLAG (sd.blowup_risk) -- does it sort starts by disaster rate?")
    print("  blowup = ER>=5, or ER>=4 in <3 IP (the ER/WHIP-wrecking outing).")
    if len(risk_pairs) >= 100:
        base = sum(y for _, y in risk_pairs) / len(risk_pairs)
        auc = _auc(risk_pairs)
        ordered = sorted(risk_pairs, key=lambda p: p[0])
        d = len(ordered) // 10
        bot = ordered[:d]
        top = ordered[-d:]
        br_bot = sum(y for _, y in bot) / len(bot)
        br_top = sum(y for _, y in top) / len(top)
        flagged = [y for s, y in risk_pairs if s >= sd._RISK_MIN]
        clean = [y for s, y in risk_pairs if s < sd._RISK_MIN]
        print(f"  n={len(risk_pairs)}  base blowup rate={base:.1%}  AUC={auc:.3f}")
        print(f"  top decile (riskiest) blowup={br_top:.1%} ({br_top/base:.2f}x base)  "
              f"bottom decile={br_bot:.1%} ({br_bot/base:.2f}x)")
        if flagged:
            fr = sum(flagged) / len(flagged)
            cr = (sum(clean) / len(clean)) if clean else 0.0
            print(f"  FLAGGED (risk>={sd._RISK_MIN:.0f}): n={len(flagged)} blowup={fr:.1%} ({fr/base:.2f}x)  "
                  f"| not flagged: n={len(clean)} blowup={cr:.1%}")
        print("  (skill risk is a soft signal by nature -- blowups are largely variance, so AUC "
              "tops out ~0.52-0.53; a ~1.25x top-decile lift + a <1.0x safe bottom decile is a "
              "real, useful floor read -- swapping raw ERA for the xERA regression moves AUC <0.01.)")
    else:
        print(f"  (only {len(risk_pairs)} risk-scored starts -- need >=100; run without --limit.)")

    print("\nRECENCY-FIP CANDIDATE FIX (recent FIP vs season xERA, NOT wired into any score) --")
    print("does trailing-30-day FIP predict THIS start's residual (actual ER minus the live")
    print("opp/park-adjusted projection), where recent raw ERA (the disabled design) did not?")
    n_decl, n_impr, n_noise = (recency_bucket["declining"].n, recency_bucket["improving"].n,
                                recency_bucket["noise"].n)
    print(f"  n declining={n_decl}  improving={n_impr}  noise={n_noise}")
    if n_decl:
        print("  " + recency_bucket["declining"].row("decl."))
    if n_impr:
        print("  " + recency_bucket["improving"].row("impr."))
    base_pairs = (recency_bucket["declining"].pairs + recency_bucket["improving"].pairs
                  + recency_bucket["noise"].pairs)
    base_worse_rate = (sum(1 for p, a in base_pairs if a > p) / len(base_pairs)) if base_pairs else 0.0
    if recency_bucket["declining"].pairs:
        dp = recency_bucket["declining"].pairs
        dr = sum(1 for p, a in dp if a > p) / len(dp)
        print(f"  DECLINING: actual worse-than-projected rate={dr:.1%}  "
              f"(base rate={base_worse_rate:.1%}, lift={dr/base_worse_rate if base_worse_rate else 0:.2f}x "
              f"-- expect >1.0x if the flag confirms a real ongoing decline)")
    if recency_bucket["improving"].pairs:
        ip_ = recency_bucket["improving"].pairs
        ir = sum(1 for p, a in ip_ if a < p) / len(ip_)
        base_better_rate = 1.0 - base_worse_rate
        print(f"  IMPROVING: actual better-than-projected rate={ir:.1%}  "
              f"(base rate={base_better_rate:.1%}, lift={ir/base_better_rate if base_better_rate else 0:.2f}x "
              f"-- expect >1.0x if the flag confirms a real ongoing improvement)")
    if len(recency_pairs) >= 40:
        r = _pearson(recency_pairs)
        print(f"  signed FIP-gap vs residual: n={len(recency_pairs)}  Pearson r={r:+.3f}  "
              f"(want CLEARLY positive -- worse recent FIP should predict a worse residual)")
    else:
        print(f"  (only {len(recency_pairs)} flagged starts -- need >=40; run without --limit.)")

    print("\nSHIPPED sd.pitcher_bounceback_flag / sd._recent_fip (fantasy/scoring.py) -- calls the")
    print("REAL production code directly (fixed thresholds: _BOUNCEBACK_MIN_IP/_BOUNCEBACK_GAP_ERA/")
    print("_FIP_CONSTANT), trailing-30-day recent_row, NOT a parallel copy or a swept parameter.")
    n_bb, n_rg, n_ns = (shipped_bucket["bounceback"].n, shipped_bucket["regression"].n,
                        shipped_bucket["noise"].n)
    print(f"  n bounceback={n_bb}  regression={n_rg}  noise={n_ns}")
    ship_base_pairs = (shipped_bucket["bounceback"].pairs + shipped_bucket["regression"].pairs
                       + shipped_bucket["noise"].pairs)
    ship_base_worse = (sum(1 for p, a in ship_base_pairs if a > p) / len(ship_base_pairs)) if ship_base_pairs else 0.0
    if shipped_bucket["bounceback"].pairs:
        bp = shipped_bucket["bounceback"].pairs
        # bounceback = badge claims next start trends BETTER than the recent line -> want a
        # LOW worse-than-projected rate (below the population base rate).
        br = sum(1 for p, a in bp if a > p) / len(bp)
        print(f"  BOUNCEBACK badge: actual worse-than-projected rate={br:.1%}  "
              f"(base rate={ship_base_worse:.1%}, ratio={br/ship_base_worse if ship_base_worse else 0:.2f}x "
              f"-- want CLEARLY <1.0x)")
    if shipped_bucket["regression"].pairs:
        rp = shipped_bucket["regression"].pairs
        # regression = badge claims next start trends WORSE than the recent line -> want a HIGH
        # worse-than-projected rate (above the population base rate).
        rr = sum(1 for p, a in rp if a > p) / len(rp)
        print(f"  REGRESSION badge: actual worse-than-projected rate={rr:.1%}  "
              f"(base rate={ship_base_worse:.1%}, ratio={rr/ship_base_worse if ship_base_worse else 0:.2f}x "
              f"-- want CLEARLY >1.0x)")
    print("  (this is the pass/fail check for what actually shipped -- confirms the production")
    print("  formula/thresholds reproduce the validated direction, not just the design concept.)")

    if args.csv:
        os.makedirs("scratchpad", exist_ok=True)
        out = os.path.join("scratchpad", "backtest_starts.csv")
        with open(out, "w", encoding="utf-8") as f:
            f.write("pitcher,date,matchup,proj_ip,proj_er,proj_k,act_ip,act_er,act_k\n")
            for r in csv_rows:
                f.write(",".join(str(x) for x in r) + "\n")
        print(f"\nWrote {len(csv_rows)} per-start rows -> {out}")


if __name__ == "__main__":
    main()
