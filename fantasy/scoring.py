"""fantasy/scoring.py — player scoring, calibration, and luck/blowup flags (F5 split, part 2).

Everything that turns a stat row into a 0-100 role score, plus the per-snapshot
calibration state and the display-only regression/blowup flags that read it.

Mutable calibration globals live here WITH their writers and (for the rebound
`_XERA_OFFSET`) its reader `pitcher_regression_flag`. The in-place dicts
(`_LG`/`_PIT_BENCH`/`_AB_BENCH`/`_SCORE_CALIB`) are `.clear()`-ed and refilled, never
rebound, so send_digest's `from fantasy.scoring import *` binding tracks their updates.

Imports the ui leaf via `from fantasy.ui import *` so the moved bodies (which use the
palette, `_n`, `_hit_badge`, …) are byte-identical; those ui names are NOT re-exported
from here. send_digest re-exports this module's own names (and YEAR) via
`from fantasy.scoring import *`.
"""
import math

from fantasy.ui import *   # noqa: F401,F403 — palette + primitives used by moved bodies

_EXCLUDE = set(dir())      # everything above is imported, not exported from scoring

YEAR = 2026                # current fantasy season (single-sourced here; re-exported)

def _is_sp(r):
    """Usage-based SP/RP detection. Priority: ESPN season GS/GP → dataset GS/G → IP/G → Position."""
    pos      = str(r.get("Position") or "")
    gs       = _n(r.get("GS"))
    g        = _n(r.get("G"))
    ip_per_g = _n(r.get("IP_per_G"))
    espn_gs  = _n(r.get("ESPN_GS"))
    espn_gp  = _n(r.get("ESPN_GP"))

    # ESPN season GS/GP — full-season sample, most reliable
    if espn_gp >= 5:
        rate = espn_gs / espn_gp
        if rate >= 0.80:
            return True
        if rate <= 0.20:
            return False
        # 20–80%: ambiguous, fall through

    # Dataset GS/G — only trust with enough appearances
    if g >= 4:
        rate = gs / g
        if rate >= 0.80:
            return True
        if rate <= 0.20 and ip_per_g < 4.0:
            return False

    # IP/G — catches bulk/opener cases regardless of GS rate
    if ip_per_g >= 4.5:
        return True
    if 0 < ip_per_g < 2.5:
        return False

    # Position field last resort
    if "SP" in pos and "RP" not in pos:
        return True
    if "RP" in pos and "SP" not in pos:
        return False
    if "SP" in pos:  # dual-eligible: lean SP if decent IP/G
        return ip_per_g >= 3.0

    return False


_BLEND_W = 0.35   # recent-form weight in the displayed blended score (season weight = 1 - _BLEND_W)


def _blend(r, score_fn, idx_recent, w=None):
    """Blend of best-available recent stats and season score. Default weight _BLEND_W
    (35% recent / 65% season): the composite leans on the stable season signal, since
    hot/cold streaks are already surfaced explicitly in the Hot/Cold sections."""
    if w is None:
        w = _BLEND_W
    s_year = score_fn(r)
    r_rec = idx_recent.get(r.get("PlayerName", ""))
    if not r_rec:
        return s_year
    s_rec = score_fn(r_rec)
    return round(w * s_rec + (1 - w) * s_year) if s_rec > 0 else s_year


_PIT_BENCH      = {}      # (window:int, role:"SP"|"RP") -> {"IP":.., "GS":.., "GP":..}


_IP_RELY_FRAC   = 0.20    # rate stats trusted once IP reaches this fraction of the role/window leader


_GS_VIABLE_FRAC = 0.17    # a viable SP has made this fraction of the leader's starts


_GP_VIABLE_FRAC = 0.30    # a viable RP has this fraction of the leader's appearances…


_IP_VIABLE_FRAC = 0.38    # …or this fraction of the leader's innings


_PIT_FALLBACK   = {"IP_RELY": 20.0, "GS_VIABLE": 3.0, "GP_VIABLE": 12.0, "IP_VIABLE": 20.0}


_SCORE_CALIB = {
    "sp": (1.2503, -15.0391),   # recalibrated 2026-07-25 (post QS/W rebalance) — also the small-pool fallback
    "rp": (1.6272, -28.9514),   # recalibrated 2026-07-25 (post _ip_reliability_mult add)
    "hit": (1.4112, 0.2910),    # recalibrated 2026-07-25 (post live-recalibration add; was fixed 1.587/-5.2)
}


_MIN_CALIB_POOL = 30            # qualified pitchers a role needs before live re-anchoring kicks in


def compute_pitcher_benchmarks(pitchers):
    """Leader IP/GS/GP per (window, role) from the live snapshot, so pitcher volume
    thresholds (small-sample reliability + positional viability) track the season rather
    than tripping a fixed minimum. Uses p95 as an outlier-robust 'leader'. Writes the
    module global _PIT_BENCH; slices with too few pitchers are left unset (→ fallback)."""
    _PIT_BENCH.clear()
    for ds in (7, 15, 30, YEAR):
        rows = [r for r in pitchers if int(_n(r.get("Dataset")) or 0) == ds]
        for role in ("SP", "RP"):
            grp = [r for r in rows if _is_sp(r) == (role == "SP")]
            def _p95(field):
                v = sorted(_n(x.get(field)) for x in grp if _n(x.get(field)) > 0)
                return v[int(len(v) * 0.95)] if len(v) >= 12 else 0.0
            _PIT_BENCH[(ds, role)] = {"IP": _p95("IP"),
                                      "GS": _p95("GS") or _p95("ESPN_GS"),
                                      "GP": _p95("ESPN_GP") or _p95("GP")}
    return _PIT_BENCH


def _ip_reliability_mult(r):
    """Small-sample multiplier (≤ 1.0) for pitcher_score: rate stats are trusted once a
    pitcher reaches _IP_RELY_FRAC of the role/window leader's innings, scaling down below
    that. Role- and window-aware so an SP and an RP aren't held to the same absolute IP."""
    ip = _n(r.get("IP"))
    if ip <= 0:
        return 1.0
    ds = int(_n(r.get("Dataset")) or 0) or 7
    role = "SP" if _is_sp(r) else "RP"
    leader = _PIT_BENCH.get((ds, role), {}).get("IP") or 0
    thresh = leader * _IP_RELY_FRAC if leader > 0 else _PIT_FALLBACK["IP_RELY"]
    return min(1.0, ip / thresh) if thresh > 0 else 1.0


def _pit_viable_min(role, stat):
    """Season (YEAR) volume floor for the positional-breakdown 'viable FA' filter and the
    recalibration population — a fraction of the role's season leader, so 'getting real
    opportunities' scales with the season. Falls back to the old hard-coded minimum."""
    b = _PIT_BENCH.get((YEAR, role), {})
    if stat == "GS":
        return (b.get("GS") or 0) * _GS_VIABLE_FRAC or _PIT_FALLBACK["GS_VIABLE"]
    if stat == "GP":
        return (b.get("GP") or 0) * _GP_VIABLE_FRAC or _PIT_FALLBACK["GP_VIABLE"]
    return (b.get("IP") or 0) * _IP_VIABLE_FRAC or _PIT_FALLBACK["IP_VIABLE"]


def pitcher_score(r, _raw=False, _parts=False):
    kip   = _n(r.get("K/IP") or r.get("KIP"))
    era   = _n(r.get("ERA"))
    whip  = _n(r.get("WHIP"))
    gs    = _n(r.get("GS"))
    svhd  = _n(r.get("SVHD")) or _n(r.get("SV"))
    kpct  = _n(r.get("Kpct_P"))
    w     = _n(r.get("ESPN_W")) or _n(r.get("W"))
    ip_g  = _n(r.get("IP_per_G"))
    xera     = _n(r.get("xERA"))            # Baseball Savant deserved-ERA (absolute)
    xwoba_ag = _n(r.get("xwOBA_against"))   # xwOBA allowed (absolute, ~.315 avg)
    brl_ag   = _n(r.get("BarrelPctAllowed"))
    whiff_pt = _n(r.get("WhiffPctile"))     # league whiff PERCENTILE 0-100 (not a rate)
    is_sp = _is_sp(r)

    if not kip and not era and not kpct:
        return ({}, 1.0) if _parts else 0

    c = {}
    # ── Strikeouts (SP 20 / RP 28): results-based K% (or K/IP) blended 60/40 with the
    #    predictive whiff% percentile, which leads K% start-to-start. Trimmed from 28 for
    #    SP to make room for QS/W credit below — RP keeps the original 28 cap.
    k_cap = 20 if is_sp else 28
    if kpct > 0:
        k_comp = min(k_cap, kpct / 0.28 * k_cap)
    else:
        k_comp = min(k_cap, kip / 1.5 * k_cap)
    if whiff_pt > 0:
        k_comp = 0.6 * k_comp + 0.4 * min(k_cap, whiff_pt / 100 * k_cap)
    c["K"] = k_comp

    # ── Run prevention (SP 26 / RP 28): actual ERA (a league category) blended 55/45 with
    #    xERA (deserved, strips defense/sequencing luck).
    runprev_cap = 26 if is_sp else 28
    era_base = 0.55 * era + 0.45 * xera if (era > 0 and xera > 0) else era
    c["RunPrev"] = max(0, min(runprev_cap, (6.0 - era_base) / 4.0 * runprev_cap))

    # ── WHIP (20): results only — no clean predictive twin in the feed.
    c["WHIP"] = max(0, min(20, (2.0 - whip) / 1.1 * 20))

    # ── Contact quality allowed (SP 0-8 / RP 0-12): barrel%-allowed + xwOBA-against, both
    #    lower-is-better. Rewards suppressing hard contact regardless of results. Trimmed
    #    from 12 for SP alongside K — the two components this whole rebalance identified as
    #    over-weighted for a contact-managed, not lucky, profile.
    brl_cap, xwoba_cap = (3, 5) if is_sp else (5, 7)
    contact = 0.0
    if brl_ag > 0:
        contact += max(0, min(brl_cap, (10.0 - brl_ag) / 6.0 * brl_cap))
    if xwoba_ag > 0:
        contact += max(0, min(xwoba_cap, (0.360 - xwoba_ag) / 0.110 * xwoba_cap))
    c["Contact"] = contact

    if is_sp:
        # QS (18): durability + run-prevention skill via the existing qs_probability model
        # (already computed elsewhere, never previously fed into any score). Full credit at
        # ~70% modeled QS rate (an ace-durability threshold; league avg is ~38%).
        qsp = qs_probability(r)
        c["QS"] = max(0, min(18, ((qsp or 0) / 70.0) * 18))
        # W (8): a real scored category SP previously got zero credit for (RP's Role already
        # credits it). Divisor 15 not RP's 10 — a full-season SP racks up more decisions.
        c["W"] = min(8, w / 15 * 8)
    else:
        # RP role: SVHD first, then W and IP/G as opportunity signals
        c["Role"] = (5 + min(7, svhd / 15 * 7)
                     + min(6, w / 10 * 6)       # wins
                     + min(5, ip_g / 1.2 * 5))  # opportunity: IP per appearance

    # Small-sample penalty: rate stats are unreliable below a role/window-relative innings
    # floor (derived from the leader, so it scales with the season — not a fixed 20 IP).
    mult = _ip_reliability_mult(r)
    if _parts:
        return c, mult

    s = sum(c.values()) * mult
    if _raw:
        return s
    # Calibrate to shared 0-100 scale (p50→50, p90→80) — re-anchored live per snapshot by
    # compute_score_calibration(); falls back to the hand-tuned constants for a thin pool.
    A, C = _SCORE_CALIB["sp"]
    s = s * A + C
    return max(0, min(100, round(s)))


_AB_BENCH    = {}                                  # window -> full-time AB, set per snapshot


_FULLTIME_AB = {7: 18, 15: 38, 30: 74, YEAR: 225}  # fallback only


_AB_FLOOR    = 0.40   # extreme part-timers keep at least this fraction of their rate score


_AB_LEADER_FRAC = 0.62  # a regular starter reaches ~62% of the window's leader → full credit


def compute_ab_benchmarks(hitters):
    """Full-time AB benchmark per window = _AB_LEADER_FRAC × the window's leader AB
    (p95, outlier-robust). Derived from the live snapshot so 'full-time' scales as the
    season progresses rather than tripping a fixed minimum. Writes the module global
    _AB_BENCH; windows with too few players fall back to _FULLTIME_AB."""
    _AB_BENCH.clear()
    for ds in (7, 15, 30, YEAR):
        abs_ = sorted(_n(r.get("AB")) for r in hitters
                      if int(_n(r.get("Dataset")) or 0) == ds and _n(r.get("AB")) > 0)
        if len(abs_) >= 20:
            leader = abs_[int(len(abs_) * 0.95)]   # p95 ≈ healthy everyday leader
            _AB_BENCH[ds] = max(1.0, leader * _AB_LEADER_FRAC)
    return _AB_BENCH


def _ab_opportunity_mult(r):
    """Playing-time multiplier (≥ _AB_FLOOR, ≤ 1.0) from a hitter's at-bats vs the
    full-time benchmark for its window. rec_h rows carry no Dataset → treated as 7-day.
    No AB on the row → 1.0 (never penalize missing data)."""
    ab = _n(r.get("AB"))
    if ab <= 0:
        return 1.0
    ds = int(_n(r.get("Dataset")) or 0) or 7
    full = _AB_BENCH.get(ds) or _FULLTIME_AB.get(ds) or _FULLTIME_AB[7]
    return max(_AB_FLOOR, min(1.0, ab / full))


_LG = {}   # key -> league-average value; callers fall back to the old literal when unset


def compute_league_averages(hitters, pitchers):
    """Populate the _LG module global from qualified YEAR rows: hitter `ops` (full-time
    regulars), `team_ops` (mean opponent OPS faced), and starter `era`/`whip`/`k_pct`/
    `ip_per_start`/`barrel_allowed`. Each key is left unset when its population is empty,
    so consumers keep their hard-coded fallback."""
    _LG.clear()

    def _mean(vals):
        vals = [x for x in vals if x is not None and x > 0]
        return sum(vals) / len(vals) if vals else None

    # Hitter OPS over full-time regulars (AB ≥ 55% of the season full-time benchmark).
    ab_floor = (_AB_BENCH.get(YEAR) or _FULLTIME_AB[YEAR]) * 0.55
    ops = _mean([_n(r.get("OPS")) for r in hitters
                 if int(_n(r.get("Dataset")) or 0) == YEAR and _n(r.get("AB")) >= ab_floor])
    if ops:
        _LG["ops"] = round(ops, 4)

    # Opponent strength faced by pitchers (per-start opponent team OPS + team K rate).
    team_ops = _mean([_n(r.get("Team_OPS_Value")) for r in pitchers])
    if team_ops:
        _LG["team_ops"] = round(team_ops, 4)
    team_k = _mean([_n(r.get("Team_K_Value")) for r in pitchers])
    if team_k:
        _LG["team_k"] = round(team_k, 4)

    # Starter league averages for qs_probability anchors — qualified SPs only.
    ip_min = _pit_viable_min("SP", "IP")
    sps = [r for r in pitchers
           if int(_n(r.get("Dataset")) or 0) == YEAR and _is_sp(r) and _n(r.get("IP")) >= ip_min]
    for key, field in (("era", "ERA"), ("whip", "WHIP"), ("k_pct", "Kpct_P"),
                       ("ip_per_start", "IP_per_G"), ("barrel_allowed", "BarrelPctAllowed")):
        m = _mean([_n(r.get(field)) for r in sps])
        if m:
            _LG[key] = round(m, 4)
    return _LG


def hitter_score(r, _raw=False, _parts=False):
    """0-100 hitter score. `_parts=True` returns (components_dict, opportunity_mult)
    instead — the raw pre-multiplier component contributions and the playing-time
    multiplier — so the score-breakdown tooltip stays in sync with the real math."""
    ops    = _n(r.get("OPS"))
    hr     = _n(r.get("HR"))
    rbi    = _n(r.get("RBI"))
    sb     = _n(r.get("SB"))
    avg    = _n(r.get("AVG"))
    hrp    = _n(r.get("HR_Probability"))
    wrc    = _n(r.get("wRCplus"))
    xwoba  = _n(r.get("xwOBA"))
    sprint = _n(r.get("SprintSpeed"))
    iso    = _n(r.get("ISO"))

    if not ops and not hr and not wrc:
        return ({}, 1.0) if _parts else 0

    c = {}
    if wrc > 0:
        c["Prod"] = max(0, min(30, (wrc - 60) / 80 * 30))
    else:
        c["Prod"] = max(0, min(30, (ops - 0.55) / 0.50 * 30))
    c["HR"]  = min(16, hr / 35 * 16)
    c["ISO"] = min(6, iso / 0.25 * 6) if iso > 0 else 0.0
    c["RBI"] = min(10, rbi / 110 * 10)
    if sprint > 0:
        c["Speed"] = max(0, min(10, (sprint - 24) / 6 * 10))
    else:
        c["Speed"] = min(10, sb / 40 * 10)
    if xwoba > 0:
        c["xwOBA"] = max(0, min(10, (xwoba - 0.270) / 0.120 * 10))
    else:
        c["xwOBA"] = max(0, min(10, (avg - 0.180) / 0.160 * 10))
    c["HR%"] = min(8, hrp * 40)

    # Opportunity adjustment: the rate components above reward a part-time masher as
    # much as a regular, but over a week a bench bat who gets ~1 AB every few games
    # can't accumulate counting stats. Scale by at-bats vs a full-time benchmark that
    # is derived from the live data (compute_ab_benchmarks), so it tracks the season.
    mult = _ab_opportunity_mult(r)
    if _parts:
        return c, mult

    s = sum(c.values()) * mult
    if _raw:
        return s
    # Calibrate to shared 0-100 scale (p50→50, p90→80) — re-anchored live per snapshot by
    # compute_score_calibration(); falls back to the hand-tuned constants for a thin pool.
    A, C = _SCORE_CALIB["hit"]
    s = s * A + C
    return max(0, min(100, round(s)))


def qs_probability(r):
    """QS probability for a start. Formula calibrated to real QS rates: league avg ~38%, ace ~75%."""
    gs = int(_n(r.get("GS")) or 0)
    if gs < 1:
        return None
    ip_per_g = _n(r.get("IP_per_G"))   # IP / total G (honest for starters mixed with relief)
    if ip_per_g <= 0:                   # fallback for snapshots predating this field
        _g = max(_n(r.get("G")) or 1, 1)
        ip_per_g = min(_n(r.get("IP", 0)) / _g, 7.5)
    era      = _n(r.get("ERA"))
    whip     = _n(r.get("WHIP"))
    brl      = _n(r.get("BarrelPctAllowed"))
    kpct     = _n(r.get("Kpct_P"))     # 0.0–0.50 scale
    opp      = _n(r.get("Team_OPS_Value"))

    # League-average anchors are derived from the live snapshot (_LG) with the old fixed
    # values as fallback. The intercept (38 = league QS rate) and the multipliers stay
    # fixed, so a league-average starter still scores ~38 regardless of the anchors — only
    # the reference point tracks the season. Keeps the function calibrated (avg ~38, ace ~75).
    score = 38  # league-average QS-rate baseline
    if ip_per_g > 0:
        score += (ip_per_g - (_LG.get("ip_per_start") or 5.4)) * 16  # biggest driver: IP/appearance
    if era > 0:
        score += ((_LG.get("era") or 4.2) - era) * 8
    if whip > 0:
        score += ((_LG.get("whip") or 1.35) - whip) * 12
    if brl > 0:
        score += ((_LG.get("barrel_allowed") or 7.5) - brl) * 0.5
    if kpct > 0:
        score += (kpct - (_LG.get("k_pct") or 0.22)) * 20
    if opp > 0:
        score += ((_LG.get("team_ops") or 0.730) - opp) * 60     # matchup adjustment

    return max(1, min(99, round(score)))


def rp_score(r, _raw=False, _parts=False):
    svhd = _n(r.get("ESPN_SVHD")) or _n(r.get("SVHD"))   # prefer season total from ESPN
    k    = _n(r.get("ESPN_K"))    or _n(r.get("K"))       # prefer season count from ESPN
    w    = _n(r.get("ESPN_W"))    or _n(r.get("W"))
    ip_g = _n(r.get("IP_per_G"))
    era  = _n(r.get("ERA")) or 5.0
    whip = _n(r.get("WHIP")) or 1.5
    xera     = _n(r.get("xERA"))
    brl_ag   = _n(r.get("BarrelPctAllowed"))
    whiff_pt = _n(r.get("WhiffPctile"))     # league whiff PERCENTILE 0-100
    # SVHD is deliberately DE-EMPHASIZED (punt-saves weighting): saves are the most
    # volatile RP category and one we're willing to sacrifice, so it's ~15% of the raw
    # score, below an equal 5-cat share. Skill/ratio cats carry the weight instead:
    # SVHD 15 · K 26 · W 15 · IP/G 8, then ERA 16 · WHIP 12 · contact 8.
    c = {}
    c["SVHD"] = min(15, svhd / 20 * 15)
    c["K"]    = min(26, k    / 80 * 26)
    c["W"]    = min(15, w    / 10 * 15)
    c["IP/G"] = min(8,  ip_g / 1.2 * 8)    # opportunity: IP per appearance, max at 1.2 IP/G
    # Run prevention (16): ERA blended 50/50 with xERA (deserved).
    era_base = 0.5 * era + 0.5 * xera if xera > 0 else era
    c["RunPrev"] = max(0, min(16, (5.0 - era_base) / 3.0 * 16))
    c["WHIP"] = max(0, min(12, (2.0 - whip) / 1.0 * 12))
    # Contact quality allowed (0-8): barrel%-allowed (lower better) + whiff% percentile.
    contact = 0.0
    if brl_ag > 0:
        contact += max(0, min(4, (10.0 - brl_ag) / 6.0 * 4))
    if whiff_pt > 0:
        contact += min(4, whiff_pt / 100 * 4)
    c["Contact"] = contact
    mult = _ip_reliability_mult(r)
    if _parts:
        return c, mult

    s = sum(c.values()) * mult
    if _raw:
        return s
    # Calibrate to shared 0-100 scale (p50→50, p90→80) — re-anchored live per snapshot by
    # compute_score_calibration(); falls back to the hand-tuned constants for a thin pool.
    A, C = _SCORE_CALIB["rp"]
    s = s * A + C
    return max(0, min(100, round(s)))


def _ip_notation_to_dec(ip):
    """Baseball innings notation (25.1 = 25 1/3, 25.2 = 25 2/3) -> decimal. Local to keep
    scoring.py a low leaf (no fetch_data import)."""
    ip = _n(ip)
    if ip <= 0:
        return 0.0
    whole = int(ip)
    frac  = round((ip - whole) * 10)
    return whole + frac / 3.0


def rp_score_recent(rec, season_row):
    """Rate-based RECENT (short-window) RP score, comparable to the season rp_score.

    Display-only companion to rp_score for the score-pill dropdown's `season | 30-day`
    header. The window row's FantasyPros counting stats (SVHD/K/W) are PACED to the
    player's season-equivalent volume so both numbers ride the same static caps AND the
    same live _SCORE_CALIB["rp"] rescale -> a clean same-scale, same-player comparison.
    The delta then reflects pure rate change (K/IP, SV+H/appearance, W/IP, ERA, WHIP).

    Returns an int 0-100, or None when not computable (missing window row / thin sample).
    Uses the WINDOW FP fields (SVHD/K/W/IP) and deliberately omits ESPN_* keys so rp_score
    reads the paced window values instead of the season-broadcast ESPN totals."""
    if not rec:
        return None
    ip_w = _ip_notation_to_dec(rec.get("IP"))
    ip_s = _ip_notation_to_dec(season_row.get("IP"))
    if ip_w < 5 or ip_s <= 0:
        return None                              # too thin to pace reliably

    scale = ip_s / ip_w                          # pace window production to season IP
    paced_k = _n(rec.get("K")) * scale
    paced_w = _n(rec.get("W")) * scale
    # SV+H is opportunity-driven -> pace per-appearance when IP/G is available, else per-IP.
    svhd_w = _n(rec.get("SVHD"))
    ipg_w  = _n(rec.get("IP_per_G"))
    ipg_s  = _n(season_row.get("IP_per_G"))
    if ipg_w > 0 and ipg_s > 0:
        g_w = ip_w / ipg_w
        g_s = ip_s / ipg_s
        paced_svhd = (svhd_w / g_w) * g_s if g_w > 0 else svhd_w * scale
    else:
        paced_svhd = svhd_w * scale

    # A genuinely scoreless short window (ERA/WHIP == 0 over real innings) is ELITE, but
    # rp_score's `_n(...) or 5.0` / `or 1.5` fallbacks treat a falsy 0 as a blowup. Nudge a
    # real 0 to a tiny positive so it scores as near-perfect run prevention, not a disaster.
    era_r  = _n(rec.get("ERA"));  whip_r = _n(rec.get("WHIP"))
    if 0 <= era_r  < 0.01: era_r  = 0.01
    if 0 <= whip_r < 0.01: whip_r = 0.01
    synth = {
        "SVHD": paced_svhd, "K": paced_k, "W": paced_w,
        "IP_per_G": ipg_w if ipg_w > 0 else ipg_s,
        "ERA": era_r, "WHIP": whip_r, "xERA": rec.get("xERA"),
        "BarrelPctAllowed": rec.get("BarrelPctAllowed"), "WhiffPctile": rec.get("WhiffPctile"),
    }   # no ESPN_* keys -> rp_score falls to these paced window values
    return rp_score(synth)


def compute_score_calibration(pitchers, hitters):
    """Re-anchor the SP/RP/hitter score calibration live from this snapshot's raw-score
    distribution (approach A) so displayed scores track the season without a hand-paste. For
    each role, solve A/C such that raw p50 -> 50 and p90 -> 80, using the SAME qualified pool as
    recalibrate_scores.py (SP: _is_sp + IP past the reliability floor; RP: _pit_viable_min on
    GP or IP; hitter: AB >= 30% of full-time benchmark, same floor prepare_scoring's hit_pool
    uses; all from YEAR rows). Writes the module global _SCORE_CALIB. SMALL-POOL GUARD:
    a role whose qualified pool is below _MIN_CALIB_POOL or degenerate (p90 <= p50) KEEPS its
    hand-tuned fallback constants, so a noisy early-season distribution can't warp the scale.
    Must run AFTER compute_pitcher_benchmarks (qualification + _raw scores read _PIT_BENCH) and
    compute_ab_benchmarks (hitter qualification reads _AB_BENCH). NOTE: this runs before
    compute_league_averages in prepare_scoring's sequence, so pitcher_score's SP-branch
    qs_probability() call sees an empty _LG here and falls back to its own hardcoded league
    averages for this one-time raw-score solve — a small precision loss on the calibration
    anchor only, not on live per-player displayed scores (computed later, after _LG is
    populated). Do not reorder to fix this."""
    ps = [r for r in pitchers if int(_n(r.get("Dataset")) or 0) == YEAR]
    sp_ip_min = (_PIT_BENCH.get((YEAR, "SP"), {}).get("IP") or 0) * _IP_RELY_FRAC \
                or _PIT_FALLBACK["IP_RELY"]
    sp_raw = sorted(pitcher_score(r, _raw=True) for r in ps
                    if _is_sp(r) and _n(r.get("IP")) >= sp_ip_min)
    rp_raw = sorted(rp_score(r, _raw=True) for r in ps
                    if not _is_sp(r) and (_n(r.get("ESPN_GP")) >= _pit_viable_min("RP", "GP")
                                          or _n(r.get("IP")) >= _pit_viable_min("RP", "IP")))
    ab_min = (_AB_BENCH.get(YEAR) or _FULLTIME_AB[YEAR]) * 0.30
    hit_raw = sorted(hitter_score(r, _raw=True) for r in hitters
                     if int(_n(r.get("Dataset")) or 0) == YEAR and _n(r.get("AB")) >= ab_min)

    def _pctl(vals, q):
        i = q * (len(vals) - 1)
        lo = int(i); hi = min(lo + 1, len(vals) - 1)
        return vals[lo] + (vals[hi] - vals[lo]) * (i - lo)

    for role, raws in (("sp", sp_raw), ("rp", rp_raw), ("hit", hit_raw)):
        if len(raws) < _MIN_CALIB_POOL:
            continue                        # too thin — keep the hand-tuned fallback
        p50, p90 = _pctl(raws, 0.50), _pctl(raws, 0.90)
        if p90 - p50 <= 0:
            continue                        # degenerate spread — keep the fallback
        A = 30.0 / (p90 - p50)
        C = 50.0 - A * p50
        _SCORE_CALIB[role] = (A, C)
    return _SCORE_CALIB


def _score_p(r, idx_recent=None):
    """Canonical pitcher score — role-aware, so a player shows the SAME number in
    every section. SP → pitcher_score blended with recent form (start-to-start
    volatility matters). RP → rp_score, unblended: it is built on ESPN season
    counting stats (role/opportunity driven), and skipping the blend guarantees
    the number matches the RP tables exactly."""
    if _is_sp(r):
        return _blend(r, pitcher_score, idx_recent) if idx_recent is not None else pitcher_score(r)
    return rp_score(r)


def _starts_this_week(r, today_str, week_end_str):
    """Count a pitcher's upcoming starts within the current matchup week [today, week_end].
    Uses the PSP_Dates list (all scheduled starts); falls back to the single PSP_Date
    scalar for snapshots predating the list field."""
    dates = r.get("PSP_Dates")
    if isinstance(dates, list) and dates:
        return sum(1 for d in dates if today_str <= d <= week_end_str)
    d = r.get("PSP_Date", "")
    return 1 if d and d != "1999-01-01" and today_str <= d <= week_end_str else 0


_PWR_HRP_MIN   = 0.23    # modeled per-game HR probability (a notch above _hrp_cell's 0.20 green tier)


_SB_PCTILE_MIN = 0.80    # top ~20% of SB producers within the qualified YEAR pool


_SB_SPEED_MIN  = 27.0    # ft/s sprint-speed corroboration (skipped when SprintSpeed missing)


_XREG_BA       = 0.020   # xBA − AVG gap for a regression flag


_XREG_SLG      = 0.030   # xSLG − SLG gap for a regression flag


_RISK_MIN        = 55.0   # 0-100 risk score at/above which the ⚠ RISK chip fires (~top ~12% of startable arms)


_RISK_W          = {"whip": 0.32, "k": 0.22, "era": 0.28, "contact": 0.18}


_RISK_WHIP_SPAN  = 0.35   # WHIP this far ABOVE league starter WHIP = max traffic risk


_RISK_K_SPAN     = 0.09   # K% this far BELOW league starter K% = max no-escape risk


_RISK_WPCT_SPAN  = 40.0   # WhiffPctile fallback: this far below the 50th pctile = max


_RISK_ERA_SPAN   = 1.60   # effective ERA this far ABOVE league starter ERA = max run-prevention risk


_RISK_HH_BASE    = 40.0   # HardHit% allowed baseline; +10 pts above = max contact risk


_RISK_HH_SPAN    = 10.0


_RISK_RECENT_W   = 0.25   # weight of the recent-form escalator when a recent ERA is supplied


_RISK_RECENT_SPAN = 2.50  # recent ERA this far ABOVE the pitcher's own baseline = max cold-form risk


def _effective_era(r):
    """Actual ERA regressed toward xERA (IP-weighted, same shrinkage as the proj line's
    _ERA_REG_PRIOR_IP). Better than pure xERA for a FLOOR read: a pitcher genuinely running
    a high ERA has been giving up runs regardless of 'deserved', which is what wrecks a
    start — but small samples are still pulled toward the luck-stripped skill."""
    era = _n(r.get("ERA"))
    if era <= 0:
        return 0.0
    target = _n(r.get("xERA")) or (_LG.get("era") or 4.00)
    ip = _n(r.get("IP"))
    return (era * ip + target * _ERA_REG_PRIOR_IP) / (ip + _ERA_REG_PRIOR_IP)


def _effective_whip(r):
    """WHIP regressed toward league-average WHIP, IP-weighted (same _ERA_REG_PRIOR_IP shrinkage
    as _effective_era) — no Statcast 'x' twin exists for WHIP, so the target is the league mean
    rather than an expected stat."""
    whip = _n(r.get("WHIP"))
    if whip <= 0:
        return 0.0
    target = _LG.get("whip") or 1.35
    ip = _n(r.get("IP"))
    return (whip * ip + target * _ERA_REG_PRIOR_IP) / (ip + _ERA_REG_PRIOR_IP)


_HIT_REG_PRIOR_AB = 100.0  # recent-AB shrinkage strength (AB analog of _ERA_REG_PRIOR_IP)


def _effective_avg(season_row, recent_row):
    """Recent-window actual AVG regressed toward season xBA (skill estimate), AB-weighted --
    same shrinkage pattern as _effective_era/_effective_whip. A small recent-AB sample is
    pulled hard toward the luck-stripped season skill; a large recent sample lets the
    recent AVG carry more weight."""
    avg, ab = _n(recent_row.get("AVG")), _n(recent_row.get("AB"))
    if avg <= 0 or ab <= 0:
        return 0.0
    target = _n(season_row.get("xBA")) or 0.250
    return (avg * ab + target * _HIT_REG_PRIOR_AB) / (ab + _HIT_REG_PRIOR_AB)


def _effective_slg(season_row, recent_row):
    """Recent-window actual SLG regressed toward season xSLG, same shrinkage as _effective_avg."""
    slg, ab = _n(recent_row.get("SLG")), _n(recent_row.get("AB"))
    if slg <= 0 or ab <= 0:
        return 0.0
    target = _n(season_row.get("xSLG")) or 0.400
    return (slg * ab + target * _HIT_REG_PRIOR_AB) / (ab + _HIT_REG_PRIOR_AB)


_HIT_RECENCY_MIN_AB  = 15     # min recent AB so the read isn't 3-AB noise
_HIT_RECENCY_GAP_BA  = 0.030  # effective-AVG vs xBA gap that still counts as "survives regression"
_HIT_RECENCY_GAP_SLG = 0.050  # same for SLG


def hitter_recency_flag(season_row, recent_row):
    """Does a hitter's recent hot/cold stretch survive AB-weighted regression toward his
    season xBA/xSLG skill level, or does shrinkage mostly erase it?
    'declining'/'improving' = the recent direction survives regression (a real signal);
    'noise' = shrinkage pulls the effective rate back near season skill (small-sample-driven);
    None = recent AB below the reliability floor, or season xStats missing."""
    ab = _n(recent_row.get("AB")) if recent_row else 0
    if ab < _HIT_RECENCY_MIN_AB:
        return None
    xba, xslg = _n(season_row.get("xBA")), _n(season_row.get("xSLG"))
    if xba <= 0 or xslg <= 0:
        return None
    gap_avg = _effective_avg(season_row, recent_row) - xba
    gap_slg = _effective_slg(season_row, recent_row) - xslg
    if gap_avg <= -_HIT_RECENCY_GAP_BA and gap_slg <= -_HIT_RECENCY_GAP_SLG:
        return "declining"
    if gap_avg >= _HIT_RECENCY_GAP_BA and gap_slg >= _HIT_RECENCY_GAP_SLG:
        return "improving"
    return "noise"


def hitter_recency_severity(season_row, recent_row):
    """(flag, severity) — the same flag as hitter_recency_flag, plus HOW FAR past the
    'survives regression' gap threshold the hitter sits (1.0 = right at the boundary,
    growing from there; 0.0 when flag isn't 'declining'/'improving'). The flag alone can't
    tell a hitter who just crossed the line apart from one a month deep in a real slump —
    this powers the CONTINUOUS _tval recency discount in fantasy/trades.py (_recency_value_mult),
    which needs magnitude, not just direction. Uses the same effective-AVG/SLG gap math as
    hitter_recency_flag so the two can never disagree on direction."""
    flag = hitter_recency_flag(season_row, recent_row)
    if flag not in ("declining", "improving"):
        return flag, 0.0
    xba, xslg = _n(season_row.get("xBA")), _n(season_row.get("xSLG"))
    gap_avg = _effective_avg(season_row, recent_row) - xba
    gap_slg = _effective_slg(season_row, recent_row) - xslg
    severity = max(abs(gap_avg) / _HIT_RECENCY_GAP_BA, abs(gap_slg) / _HIT_RECENCY_GAP_SLG)
    return flag, severity


def _rec_avg_slg_str(recent_row):
    """Format a hitter's RAW recent-window AVG/SLG for a confirmation-arrow (↗/↘) tooltip, or
    '' when unavailable. The confirmation prose says a hot/cold stretch is 'already showing up
    in recent games' but names only the season xBA/xSLG anchor -- this surfaces the actual
    recent number that's beating (or missing) it, since that's the whole point of the arrow."""
    if not recent_row:
        return ""
    avg, slg = _n(recent_row.get("AVG")), _n(recent_row.get("SLG"))
    if avg <= 0 or slg <= 0:
        return ""
    return f"recent AVG {avg:.3f}/SLG {slg:.3f}"


def blowup_risk(r, recent_era=None):
    """0-100 skill-based blowup (disaster-start) risk for a starter — higher = lower floor.
    Combines baserunner traffic (WHIP), strikeout escape hatch (K%/whiff), effective run
    prevention (`_effective_era` — ERA regressed toward xERA), and loud contact allowed
    (HardHit%). League-anchored via `_LG`. When `recent_era` (e.g. L15 ERA) is supplied, a
    cold recent stretch escalates the score (a currently-scuffling arm has a lower floor).
    Display-only; never fed into any quality score. See _is_blowup_risk."""
    whip = _n(r.get("WHIP"))
    if whip <= 0:
        return 0.0
    _clamp = lambda x: 0.0 if x < 0 else (1.0 if x > 1 else x)

    lg_whip = _LG.get("whip") or 1.28
    whip_bad = _clamp((whip - lg_whip) / _RISK_WHIP_SPAN)

    lg_k = _LG.get("k_pct") or 0.22
    kpct = _n(r.get("Kpct_P"))
    if kpct > 0:
        k_bad = _clamp((lg_k - kpct) / _RISK_K_SPAN)
    else:
        wpct = _n(r.get("WhiffPctile"))
        k_bad = _clamp((50.0 - wpct) / _RISK_WPCT_SPAN) if wpct > 0 else 0.5

    lg_era = _LG.get("era") or 4.10
    eff = _effective_era(r)
    era_bad = _clamp((eff - lg_era) / _RISK_ERA_SPAN) if eff > 0 else 0.5

    hh = _n(r.get("HardHitPctAllowed"))
    contact_bad = _clamp((hh - _RISK_HH_BASE) / _RISK_HH_SPAN) if hh > 0 else 0.4

    w = _RISK_W
    base = w["whip"] * whip_bad + w["k"] * k_bad + w["era"] * era_bad + w["contact"] * contact_bad

    # Recent-form escalator: ADDITIVE-ONLY. A cold recent stretch (recent ERA above the
    # pitcher's own effective baseline) RAISES the floor risk; a hot stretch does NOT lower it
    # (a good week doesn't cure a structurally shaky arm's blowup floor). Off when ERA missing.
    rec = _n(recent_era) if recent_era is not None else 0.0
    risk01 = base
    if rec > 0 and eff > 0:
        risk01 = _clamp(base + _RISK_RECENT_W * _clamp((rec - eff) / _RISK_RECENT_SPAN))
    return round(100.0 * risk01, 1)


def _is_blowup_risk(r, recent_era=None):
    """True when a startable arm's skill (+ recent form) profile flags a low floor."""
    return _is_sp(r) and blowup_risk(r, recent_era) >= _RISK_MIN


def _risk_drivers(r, recent_era=None, cap=3):
    """The 2-3 worst blowup drivers, worst-first, for the ⚠ RISK chip tooltip."""
    whip = _n(r.get("WHIP"))
    kpct = _n(r.get("Kpct_P"))
    wpct = _n(r.get("WhiffPctile"))
    eff  = _effective_era(r)
    hh   = _n(r.get("HardHitPctAllowed"))
    lg_whip = _LG.get("whip") or 1.28
    lg_k    = _LG.get("k_pct") or 0.22
    lg_era  = _LG.get("era") or 4.10
    drivers = []
    if whip > 0:
        drivers.append(((whip - lg_whip) / _RISK_WHIP_SPAN, f"{whip:.2f} WHIP"))
    if kpct > 0:
        drivers.append(((lg_k - kpct) / _RISK_K_SPAN, f"{kpct*100:.0f}% K rate"))
    elif wpct > 0:
        drivers.append(((50.0 - wpct) / _RISK_WPCT_SPAN, f"{wpct:.0f}th-pctile whiff"))
    if eff > 0:
        drivers.append(((eff - lg_era) / _RISK_ERA_SPAN, f"{eff:.2f} eff. ERA"))
    if hh > 0:
        drivers.append(((hh - _RISK_HH_BASE) / _RISK_HH_SPAN, f"{hh:.0f}% hard-hit"))
    rec = _n(recent_era) if recent_era is not None else 0.0
    if rec > 0 and eff > 0:
        drivers.append(((rec - eff) / _RISK_RECENT_SPAN, f"{rec:.2f} L15 ERA (cold)"))
    drivers.sort(key=lambda d: -d[0])
    return [txt for score, txt in drivers[:cap] if score > 0]


def blowup_badge(r, recent_era=None):
    """Red ⚠ RISK chip for a low-floor (blowup-prone) starter, or '' when not flagged.
    Hover title names the worst 2-3 drivers. `recent_era` (L15) escalates on cold form.
    Display-only, steer-aware."""
    if not _is_blowup_risk(r, recent_era):
        return ""
    drivers = _risk_drivers(r, recent_era)
    tip = "Low floor &mdash; blowup-prone: " + " &middot; ".join(drivers) if drivers else "Low floor &mdash; blowup-prone"
    return _hit_badge("&#9888;", ORANGE, tip)


_XREG_ERA    = 1.00   # de-biased |xERA − ERA| gap for a pitcher regression flag (~12% each side)


_XREG_ERA_IP = 20     # min IP so the gap is a real signal, not small-sample noise


_XERA_OFFSET = 0.0


# Confirmation arrows on the $/▼ badge: the season-level regression flag says a player *should*
# revert; the recency flag (hitter_recency_flag / pitcher_recency_flag, below) says whether that
# reversion has ALREADY shown up in his recent games. Plain text glyphs (not emoji) so they
# inherit the chip's own GREEN/RED color rather than carrying a fixed color of their own -- no
# new hue, palette stays single-meaning-per-hue. Defined here (not in fantasy/analytics.py, the
# original home) because `pitcher_regression_badge` -- a scoring-layer function -- now needs them
# too; analytics.py inherits them via its `from fantasy.scoring import *`.
_CONFIRM_UP   = "&#8599;"  # ↗ suffix when the recency flag == 'improving'; STANDALONE (no $/▼
    # prefix) when the season flag hasn't fired at all -- an early recency-only trend read.
_CONFIRM_DOWN = "&#8600;"  # ↘ suffix on a solid ▼ when the recency flag == 'declining'; STANDALONE same idea.


def _rec_era_str(recent_row):
    """Format a pitcher's RAW recent-window ERA for a confirmation-arrow (↗/↘) tooltip, or ''
    when unavailable. Pitcher analog of _rec_avg_slg_str -- the actual recent number the
    confirmation prose refers to but doesn't name."""
    if not recent_row:
        return ""
    era = _n(recent_row.get("ERA"))
    if era <= 0:
        return ""
    return f"recent ERA {era:.2f}"


_PIT_RECENCY_MIN_IP   = 8     # min recent IP so a pitcher ERA read isn't 1-2 shaky starts of
                              # noise (IP analog of _HIT_RECENCY_MIN_AB)
_PIT_RECENCY_PRIOR_IP = 25.0  # recent-IP shrinkage strength toward season xERA when checking
                              # confirmation (pitcher analog of _HIT_REG_PRIOR_AB)
_PIT_RECENCY_GAP_ERA  = 1.50  # effective recent ERA vs xERA gap that counts as "confirmed" --
                              # ~1.5x the season _XREG_ERA bar, same margin-over-the-season-
                              # threshold pattern as _HIT_RECENCY_GAP_BA/SLG vs _XREG_BA/SLG


def _effective_era_recent(season_row, recent_row):
    """Recent-window ERA regressed toward season xERA (skill estimate), IP-weighted -- same
    shrinkage pattern as _effective_avg/_effective_slg, adapted for a pitcher's ERA."""
    era, ip = _n(recent_row.get("ERA")), _n(recent_row.get("IP"))
    if era <= 0 or ip <= 0:
        return 0.0
    target = _n(season_row.get("xERA")) or (_LG.get("era") or 4.10)
    return (era * ip + target * _PIT_RECENCY_PRIOR_IP) / (ip + _PIT_RECENCY_PRIOR_IP)


def pitcher_recency_flag(season_row, recent_row):
    """Pitcher analog of hitter_recency_flag: does a pitcher's recent ERA already show the
    season regression flag's predicted move (relative to his season xERA skill anchor), or does
    IP-shrinkage mostly erase it? 'declining' = recent ERA already worse than xERA (confirms a
    sell-high call -- he's already getting hit harder, not just statistically 'due'); 'improving'
    = recent ERA already better than xERA (confirms a buy-low call); 'noise' = the recent sample
    doesn't clear the anchor either way; None = below the IP reliability floor or no xERA. ERA is
    lower-is-better, so the sign convention is flipped vs hitter_recency_flag's AVG/SLG
    (higher-is-better)."""
    ip = _n(recent_row.get("IP")) if recent_row else 0
    if ip < _PIT_RECENCY_MIN_IP:
        return None
    xera = _n(season_row.get("xERA"))
    if xera <= 0:
        return None
    gap = _effective_era_recent(season_row, recent_row) - xera
    if gap >= _PIT_RECENCY_GAP_ERA:
        return "declining"
    if gap <= -_PIT_RECENCY_GAP_ERA:
        return "improving"
    return "noise"


def compute_xera_offset(pitchers):
    """Set the module `_XERA_OFFSET` = league median (xERA − ERA) over the qualified YEAR
    pool, so `pitcher_regression_flag` measures luck RELATIVE to the systematic offset."""
    global _XERA_OFFSET
    gaps = sorted(_n(r.get("xERA")) - _n(r.get("ERA")) for r in pitchers
                  if int(_n(r.get("Dataset")) or 0) == YEAR
                  and _n(r.get("ERA")) > 0 and _n(r.get("xERA")) > 0 and _n(r.get("IP")) >= _XREG_ERA_IP)
    if len(gaps) >= 20:
        _XERA_OFFSET = gaps[len(gaps) // 2]


def pitcher_regression_flag(row):
    """'buy' (ERA unluckier than typical — positive regression likely), 'sell' (ERA luckier
    than typical — regression risk), or None. Pitcher analog of `_regression_flag`, de-biased
    by `_XERA_OFFSET` so it flags relative luck, not the league-wide xERA/ERA offset."""
    era, xera, ip = _n(row.get("ERA")), _n(row.get("xERA")), _n(row.get("IP"))
    if era <= 0 or xera <= 0 or ip < _XREG_ERA_IP:
        return None
    adj = (xera - era) - _XERA_OFFSET   # + = luckier than typical, − = unluckier
    if adj >= _XREG_ERA:
        return "sell"
    if adj <= -_XREG_ERA:
        return "buy"
    return None


def pitcher_regression_badge(row, idx_recent=None):
    """Green $ (buy-low) / red ▼ (sell-high, CONFIRMED) or hollow ▽ (sell-high, still just a
    season-level prediction) chip for a pitcher whose ERA has diverged from xERA, or '' when
    neither. Display-only (never folded into any score); hover names the ERA vs xERA gap.
    SEPARATE from ⚠ (tail risk) — see the note above. `idx_recent` (optional, the
    best_recent_p index) checks the sell-high call against `pitcher_recency_flag` — same
    three-outcome pattern as the hitter $/▼ badge (`hitter_badges`): recency CONFIRMS -> solid
    ▼ + a downward confirmation arrow; recency DISAGREES, has no data, or wasn't passed ->
    hollow ▽ (still a live warning — just not yet shown up in his actual recent games).
    Omitting `idx_recent` (default) always renders the hollow ▽ for a sell-high flag, since
    there's no way to check confirmation without it. STANDALONE EARLY-READ ARROW (pitcher
    analog of hitter_badges' 4th case): when the season flag hasn't fired at all but
    `pitcher_recency_flag` clears its threshold on its own, renders a bare confirmation arrow
    (no $/▼/▽) — an early skill-trend read ahead of the season ERA catching up."""
    flag = pitcher_regression_flag(row)
    rec = idx_recent.get(row.get("PlayerName", "")) if idx_recent else None
    rflag = pitcher_recency_flag(row, rec) if rec else None
    if not flag:
        xera = _n(row.get("xERA"))
        if xera <= 0:
            return ""
        rec_s = _rec_era_str(rec)
        if rflag == "improving":
            return _hit_badge(_CONFIRM_UP, GREEN,
                               f"Recent games already trending better than his season {xera:.2f} xERA"
                               + (f" ({rec_s})" if rec_s else "") + " &mdash; before it's shown up enough "
                               "in his season ERA to trip a season-level buy signal. Early skill-trend "
                               "read, not yet a season call.")
        if rflag == "declining":
            return _hit_badge(_CONFIRM_DOWN, RED,
                               f"Recent games already trending worse than his season {xera:.2f} xERA"
                               + (f" ({rec_s})" if rec_s else "") + " &mdash; before it's shown up enough "
                               "in his season ERA to trip a season-level sell signal. Early skill-trend "
                               "read, not yet a season call.")
        return ""
    era, xera = _n(row.get("ERA")), _n(row.get("xERA"))
    gap = f"ERA {era:.2f} vs xERA {xera:.2f}"
    if flag == "buy":
        return _hit_badge("$", GREEN, gap + " &mdash; ERA above expected, positive regression likely (buy-low)")
    if rflag == "declining":
        rec_s = _rec_era_str(rec)
        return _hit_badge("&#9660;" + _CONFIRM_DOWN, RED,
                           gap + " &mdash; confirmed: already worsening in his recent games"
                           + (f" ({rec_s})" if rec_s else "") + ", not just a season-level prediction. "
                           "Regression risk (sell-high).")
    tail = (" Heads up: his recent games are trending the OTHER way (improving) &mdash; worth watching."
            if rflag == "improving" else "")
    return _hit_badge("&#9661;", RED, gap + " &mdash; ERA below expected, regression risk (sell-high)." + tail)


# ── Season starter-skill badges (the SEASON analog of the per-start QS / 5K+ chips) ──
# qs_badge/k5_badge (send_digest) describe ONE projected outing (streaming surfaces); these
# read a starter's DURABLE season skill — quality-start reliability + strikeout rate — so
# they belong on trade surfaces (Trade Lab + Trade Radar / Pending Trades / dashboard tile).
# Kept OFF the digest's My Upcoming Starts / FA SP, where the per-start chips already live,
# so the two QS meanings never collide. Thresholds grounded in the qualified-SP distribution
# (~top fifth each); QS is matchup-neutralized so it reads season skill, not this week's opp.
_QS_SEASON_MIN   = 55     # season QS% for the 'reliable QS arm' badge (~top fifth of qualified SP)
_K_SEASON_MIN    = 0.26   # season K rate for the 'strikeout arm' badge (~top fifth of qualified SP)
_SP_SKILL_MIN_IP = 30     # IP floor so a small-sample hot start can't false-fire either badge


def _sp_qs_season(row):
    """Season QS% with the next-opponent term stripped -> pure, matchup-neutral season skill,
    or None when the row isn't a qualified starter."""
    if not _is_sp(row) or _n(row.get("IP")) < _SP_SKILL_MIN_IP:
        return None
    return qs_probability({**row, "Team_OPS_Value": -1})   # -1 skips qs_probability's opp-OPS term


def sp_skill_badges(row, cap=None):
    """Durable season-skill badges for a STARTER: 'QS' (cyan) when he posts quality starts at
    an elite season clip, 'K+' (yellow) when he's an elite-strikeout arm. Season analog of the
    per-start qs_badge/k5_badge; shown on the trade surfaces (see the note above)."""
    badges = []
    qsp = _sp_qs_season(row)
    if qsp is not None and qsp >= _QS_SEASON_MIN:
        badges.append(_hit_badge("QS", CYAN, f"Reliable quality starts &mdash; {qsp}% season QS rate (elite)"))
    if _is_sp(row) and _n(row.get("IP")) >= _SP_SKILL_MIN_IP:
        kpct = _n(row.get("Kpct_P"))
        if kpct >= _K_SEASON_MIN:
            badges.append(_hit_badge("K+", YELLOW, f"Strikeout arm &mdash; {kpct*100:.0f}% K rate (top tier)"))
    return "".join(badges[:cap])


_ERA_REG_PRIOR_IP = 40.0  # ER-projection ERA-regression strength (see _proj_line_vals)


def _regression_flag(row):
    """'buy' (positive regression, buy-low), 'sell' (regression risk, sell-high), or None
    — Statcast expected-vs-actual, SAME thresholds as the $ / ▼ hitter badges."""
    avg, iso, xba, xslg = _n(row.get("AVG")), _n(row.get("ISO")), _n(row.get("xBA")), _n(row.get("xSLG"))
    if avg <= 0 or iso <= 0 or xba <= 0 or xslg <= 0:
        return None
    slg = iso + avg
    d_ba, d_slg = xba - avg, xslg - slg
    if d_ba >= _XREG_BA and d_slg >= _XREG_SLG:
        return "buy"
    if -d_ba >= _XREG_BA and -d_slg >= _XREG_SLG:
        return "sell"
    return None


__all__ = [n for n in dir()
           if n not in _EXCLUDE and n != '_EXCLUDE' and not n.startswith('__')]
