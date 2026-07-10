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

Simplification (noted in the report): the pitcher's own line is strictly
walk-forward, but the OPPONENT OPS/K adjustment uses each team's full-season
offense (season-stable, and the formula clamps that factor to +/-15-20% anyway).
"""
import argparse
import json
import math
import os
import time
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
        recent = []                       # trailing (ip, er) of prior STARTS (walk-forward L15 proxy)
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
                        r3ip = sum(x[0] for x in recent[-3:])
                        r3er = sum(x[1] for x in recent[-3:])
                        rec_era = (9.0 * r3er / r3ip) if r3ip > 0 else None
                        risk = sd.blowup_risk(skill_row, recent_era=rec_era)
                        if risk > 0:
                            risk_pairs.append((risk, 1 if _is_blowup(a_ip, a_er) else 0))
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
                recent.append((g["ip"], g["er"]))
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
