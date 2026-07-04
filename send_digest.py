#!/usr/bin/env python3
"""
send_digest.py — Guerrero Warfare Daily Fantasy Baseball Digest
Reads data/snapshot.json (or runs fetch_data.py to refresh it), builds an
HTML email, and sends it via Gmail SMTP.

Setup:
    1. In your Google Account -> Security -> enable 2-Step Verification
    2. Google Account -> Security -> App Passwords -> create one (name it "Baseball Digest")
    3. Copy .env.example -> .env and fill in GMAIL_APP_PASSWORD
    pip install python-dotenv    (only needed for .env loading; optional)

Run manually:   python send_digest.py
Dry run:        python send_digest.py --dry-run   (saves digest_preview.html, no email)
Skip refresh:   python send_digest.py --no-refresh
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:                       # zoneinfo missing (very old Python / no tzdata)
    _ET = None


def _fmt_refresh_time(iso_str):
    """Format a snapshot's refreshed_at ISO timestamp as a display clock in ET, e.g.
    '6:32 AM ET'. tz-aware timestamps (UTC from CI) are converted to Eastern; naive ones
    (older manual local runs on the user's ET box) are shown as-is. Returns '' on any
    parse failure so the caller degrades to the plain date-only badge."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str)
    except Exception:
        return ""
    if dt.tzinfo is not None and _ET is not None:
        dt = dt.astimezone(_ET)
    h = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{h}:{dt.minute:02d} {ampm} ET"

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

# ── CONFIG ─────────────────────────────────────────────────────────────────────
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
TO_EMAIL   = "desktoptableau136@gmail.com"
CC_EMAIL   = "katzsam@duck.com"
FROM_EMAIL = "desktoptableau136@gmail.com"
MY_TEAM    = "Guerrero Warfare"
YEAR       = 2026
SNAPSHOT   = Path(__file__).parent / "data" / "snapshot.json"
LOG_DIR    = Path(__file__).parent / "logs"

# ── SCORING ────────────────────────────────────────────────────────────────────

_DL_STATUSES = {"TEN_DAY_DL", "FIFTEEN_DAY_DL", "SIXTY_DAY_DL", "IL", "OUT"}
_FP_IL_TAGS  = {"IL10", "IL15", "IL60", "IL", "DTD", "O"}   # suffixes in FantasyPros "Player" field
_STATUS_LABELS = {
    "TEN_DAY_DL": "10-Day IL", "FIFTEEN_DAY_DL": "15-Day IL", "SIXTY_DAY_DL": "60-Day IL",
    "IL10": "10-Day IL", "IL15": "15-Day IL", "IL60": "60-Day IL",
}


def _fmt_status(s):
    return _STATUS_LABELS.get(s, s)


def _get_injury_status(r):
    """Return the best available injury status string for any player (rostered or FA)."""
    # ESPN_Status is merged for hitters (and pitchers once fetch_data.py is updated)
    espn = str(r.get("ESPN_Status") or "").upper()
    if espn and espn not in ("", "ACTIVE", "FA", "UNKNOWN"):
        return espn
    # FreeAgentInjuryStatus is set for FA players only
    fa_inj = str(r.get("FreeAgentInjuryStatus") or "").upper()
    if fa_inj and fa_inj not in ("", "ACTIVE"):
        return fa_inj
    # FantasyPros embeds status as a trailing word: "Will Smith (LAD - C) IL10"
    player_str = str(r.get("Player") or "").upper()
    if player_str:
        last_word = player_str.rsplit(None, 1)[-1]
        if last_word in _FP_IL_TAGS or last_word.startswith("IL"):
            return last_word
    return ""


def _is_healthy(r):
    return not bool(_get_injury_status(r))


def _on_il(r):
    """True if the player occupies one of the league's dedicated IL roster slots.
    League has 2 IL slots that don't consume active/bench room, so dropping such a
    player frees nothing usable — never suggest it. ESPN_OnIL is a native bool from
    fetch_data (lineupSlot == 'IL'); guard against a stringified value just in case."""
    v = r.get("ESPN_OnIL")
    if isinstance(v, str):
        return v.strip().lower() == "true"
    return bool(v)


def _n(val):
    """Coerce to float, return 0 for falsy/negative sentinel values."""
    try:
        v = float(val or 0)
        return v if v > 0 else 0
    except (TypeError, ValueError):
        return 0


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


# Role-aware pitcher volume benchmarks, derived per snapshot (compute_pitcher_benchmarks)
# so "enough of a sample" and "a real workload" scale with the season instead of a fixed
# IP/GS/GP minimum that goes stale. Keyed by (window, role) → leader IP/GS/GP for that
# slice; the fractions below turn a leader into a threshold. Cold-start fallbacks are the
# old hard-coded minimums, used when a window/role slice has too few pitchers.
_PIT_BENCH      = {}      # (window:int, role:"SP"|"RP") -> {"IP":.., "GS":.., "GP":..}
_IP_RELY_FRAC   = 0.20    # rate stats trusted once IP reaches this fraction of the role/window leader
_GS_VIABLE_FRAC = 0.17    # a viable SP has made this fraction of the leader's starts
_GP_VIABLE_FRAC = 0.30    # a viable RP has this fraction of the leader's appearances…
_IP_VIABLE_FRAC = 0.38    # …or this fraction of the leader's innings
_PIT_FALLBACK   = {"IP_RELY": 20.0, "GS_VIABLE": 3.0, "GP_VIABLE": 12.0, "IP_VIABLE": 20.0}


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
    ip    = _n(r.get("IP"))
    xera     = _n(r.get("xERA"))            # Baseball Savant deserved-ERA (absolute)
    xwoba_ag = _n(r.get("xwOBA_against"))   # xwOBA allowed (absolute, ~.315 avg)
    brl_ag   = _n(r.get("BarrelPctAllowed"))
    whiff_pt = _n(r.get("WhiffPctile"))     # league whiff PERCENTILE 0-100 (not a rate)
    is_sp = _is_sp(r)

    if not kip and not era and not kpct:
        return ({}, 1.0) if _parts else 0

    c = {}
    # ── Strikeouts (28): results-based K% (or K/IP) blended 60/40 with the
    #    predictive whiff% percentile, which leads K% start-to-start.
    if kpct > 0:
        k_comp = min(28, kpct / 0.28 * 28)
    else:
        k_comp = min(28, kip / 1.5 * 28)
    if whiff_pt > 0:
        k_comp = 0.6 * k_comp + 0.4 * min(28, whiff_pt / 100 * 28)
    c["K"] = k_comp

    # ── Run prevention (28): actual ERA (a league category) blended 55/45 with
    #    xERA (deserved, strips defense/sequencing luck).
    era_base = 0.55 * era + 0.45 * xera if (era > 0 and xera > 0) else era
    c["RunPrev"] = max(0, min(28, (6.0 - era_base) / 4.0 * 28))

    # ── WHIP (20): results only — no clean predictive twin in the feed.
    c["WHIP"] = max(0, min(20, (2.0 - whip) / 1.1 * 20))

    # ── Contact quality allowed (0-12): barrel%-allowed + xwOBA-against, both
    #    lower-is-better. Rewards suppressing hard contact regardless of results.
    contact = 0.0
    if brl_ag > 0:
        contact += max(0, min(5, (10.0 - brl_ag) / 6.0 * 5))
    if xwoba_ag > 0:
        contact += max(0, min(7, (0.360 - xwoba_ag) / 0.110 * 7))
    c["Contact"] = contact

    if is_sp:
        # SP role: reward starts volume; SVHD is irrelevant
        c["Role"] = 12 if gs > 10 else 9
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
    # Calibrate to shared 0-100 scale (p50→50, p90→80) — see recalibrate_scores.py
    s = s * 1.4341 - 39.957
    return max(0, min(100, round(s)))


# Full-time AB benchmark per window, used to scale a hitter's score by playing time
# (a part-time bat accumulates fewer weekly PAs — see the opportunity adjustment in
# hitter_score). Populated at runtime by compute_ab_benchmarks() as a fraction of each
# window's leader, so it tracks the season instead of a stale hard-coded minimum. The
# dict below is only a cold-start fallback (early season / a window with too few players).
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


# League-average reference points, derived per snapshot (compute_league_averages) so the
# projection/probability math tracks the season instead of stale magic numbers — replaces
# the "league-average OPS" trio (_LEAGUE_AVG_OPS 0.717 / fetch_data LG_OPS 0.720 / the
# inline 0.730 in qs_probability) and qs_probability's fixed ERA/WHIP/K%/IP-per-start/barrel
# anchors. ONLY genuine "league average X" values live here; calibration/scaling constants
# (score-component spans, park factor, recalibration constants) deliberately do NOT.
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


def hitter_score(r, _parts=False):
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
    # Calibrate to shared 0-100 scale (p50→50, p90→80) derived from observed distribution
    s = s * 1.587 - 5.2
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


# ── DATA HELPERS ───────────────────────────────────────────────────────────────

def fetch_injury_notes():
    """Fetch MLB injury return dates + body parts from ESPN sports API (public, no auth)."""
    try:
        import urllib.request
        url = "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/injuries"
        with urllib.request.urlopen(url, timeout=8) as resp:
            data = json.loads(resp.read())
        notes = {}
        for team_block in data.get("injuries", []):
            for inj in team_block.get("injuries", []):
                name = (inj.get("athlete") or {}).get("displayName", "")
                if not name:
                    continue
                details = inj.get("details") or {}
                key = name.lower()
                if key not in notes:
                    notes[key] = {
                        "return_date": details.get("returnDate", ""),
                        "body_part":   details.get("type", ""),
                        "detail":      details.get("detail", ""),
                    }
        return notes
    except Exception:
        return {}


def fa_starters(pitchers, claimed=None, week_end=None, idx_recent=None):
    claimed = claimed or set()
    today_str = datetime.now().strftime("%Y-%m-%d")
    fa = [
        r for r in pitchers
        if r.get("FantasyTeam", "") == ""
        and r.get("PlayerName", "") not in claimed
        and int(r.get("Dataset", 0)) == YEAR
        and r.get("PSP_Date", "") not in ("1999-01-01", "", None)
        and r.get("PSP_Date", "") >= today_str
        and str(r.get("FreeAgentInjuryStatus", "")) not in _DL_STATUSES
        and (week_end is None or r.get("PSP_Date", "") <= week_end)
        and _is_sp(r)
    ]
    for r in fa:
        r["_score"] = _score_p(r, idx_recent)
    return sorted(fa, key=lambda r: -r["_score"])[:12]


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
    if _parts:
        return c, 1.0

    s = sum(c.values())
    if _raw:
        return s
    # Calibrate to shared 0-100 scale (p50→50, p90→80) — see recalibrate_scores.py
    s = s * 1.9619 - 43.0286
    return max(0, min(100, round(s)))


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


def two_start_badge():
    """Bold chip flagging a pitcher with two starts inside the matchup week."""
    return (
        f'<span style="font-size:9px;font-weight:800;color:#fff;'
        f'background:{PURPLE};border-radius:3px;padding:1px 5px;margin-left:5px;'
        f'vertical-align:middle;letter-spacing:.3px;">2-START</span>'
    )


def fa_relievers(pitchers, claimed=None):
    claimed = claimed or set()
    fa = [
        r for r in pitchers
        if r.get("FantasyTeam", "") == ""
        and r.get("PlayerName", "") not in claimed
        and int(r.get("Dataset", 0)) == YEAR
        and "RP" in str(r.get("Position", ""))
        and not _is_sp(r)
        and (_n(r.get("ESPN_SVHD")) or _n(r.get("SVHD"))) >= 1
        and str(r.get("FreeAgentInjuryStatus", "")) not in _DL_STATUSES
    ]
    for r in fa:
        r["_rp_score"] = rp_score(r)
    return sorted(fa, key=lambda r: -r["_rp_score"])[:3]


def save_role_watch(pitchers, my_team, claimed=None):
    """Detect save-role momentum for the volatile SVHD category. Returns
    (emerging, fading): FAs suddenly closing (recent saves spiking) worth adding,
    and rostered CLOSERS whose save role looks lost (real season save total but zero
    recent saves despite pitching).

    Data limitation: per-window feeds capture recent SAVES but not holds, and ESPN
    only exposes season totals. So we can't see recent holds at all. To avoid
    falsely flagging a holds-based reliever (who's still producing SV+H we can't
    see) as fading, the fading side is gated on SEASON SAVES (`ESPN_SV`) — it only
    fires for genuine closers, for whom "no recent saves" is a real role signal."""
    claimed = claimed or set()
    my_key  = " ".join(my_team.split())
    year_idx = {r["PlayerName"]: r for r in pitchers if int(r.get("Dataset", 0) or 0) == YEAR and r.get("PlayerName")}
    d15_idx  = {r["PlayerName"]: r for r in pitchers if int(r.get("Dataset", 0) or 0) == 15 and r.get("PlayerName")}

    emerging, fading = [], []
    for name in set(year_idx) | set(d15_idx):
        base = year_idx.get(name) or d15_idx.get(name)
        if "RP" not in str(base.get("Position", "")) or _is_sp(base):
            continue
        if str(base.get("FreeAgentInjuryStatus", "")) in _DL_STATUSES:
            continue
        ft        = " ".join((base.get("FantasyTeam") or "").split())
        season    = _n(base.get("ESPN_SVHD")) or _n(base.get("SVHD"))
        season_sv = _n(base.get("ESPN_SV"))     # season SAVES only — identifies true closers
        d15       = d15_idx.get(name, {})
        recent    = _n(d15.get("SVHD"))         # recent saves (window holds not captured)
        recent_g  = _n(d15.get("G"))
        rec = {"name": name, "team": base.get("Team"), "recent": recent, "season": season}

        if ft == "" and name not in claimed and recent >= 3:
            emerging.append(rec)                # a free agent suddenly racking up saves
        elif ft == my_key and season_sv >= 5 and recent == 0 and recent_g >= 3:
            fading.append(rec)                  # my closer (real save role), pitching but no saves lately

    emerging.sort(key=lambda x: (-x["recent"], -x["season"]))
    fading.sort(key=lambda x: -x["season"])
    return emerging[:3], fading[:3]


def fa_hitters(hitters, claimed=None, idx_recent=None):
    claimed = claimed or set()
    fa = [
        r for r in hitters
        if r.get("FantasyTeam", "") == ""
        and r.get("PlayerName", "") not in claimed
        and int(r.get("Dataset", 0)) == YEAR
        and _n(r.get("OPS")) > 0
        and str(r.get("FreeAgentInjuryStatus", "")) not in _DL_STATUSES
    ]
    for r in fa:
        r["_score"] = _blend(r, hitter_score, idx_recent) if idx_recent is not None else hitter_score(r)
    return sorted(fa, key=lambda r: -r["_score"])[:12]


def luck_standings(roto_rows, standings):
    totals = {}
    for row in roto_rows:
        t = row.get("Team", "")
        totals[t] = totals.get(t, 0) + float(row.get("Roto_Score") or 0)

    sorted_teams = sorted(totals.items(), key=lambda x: -x[1])
    roto_rank = {t: i + 1 for i, (t, _) in enumerate(sorted_teams)}

    result = []
    for s in standings:
        t = s["team_name"]
        rr = roto_rank.get(t, len(standings))
        result.append({
            "team":      t,
            "wins":      s["wins"],
            "losses":    s["losses"],
            "ties":      s.get("ties", 0),
            "standing":  s["standing"],
            "roto_pts":  round(totals.get(t, 0), 1),
            "roto_rank": rr,
            "luck":      rr - s["standing"],   # positive = lucky
            "logo_url":  s.get("logo_url", ""),
        })
    return sorted(result, key=lambda r: r["standing"])


def category_ranks(roto_rows, my_team):
    CATS = ["R", "HR", "RBI", "SB", "OPS", "B_SO", "K", "QS", "W", "ERA", "WHIP", "SVHD"]
    my_key = " ".join(my_team.split())
    totals = {}
    for row in roto_rows:
        t = " ".join((row.get("Team") or "").split())
        if t not in totals:
            totals[t] = {c: 0 for c in CATS}
        for c in CATS:
            totals[t][c] += float(row.get(f"{c}_Points") or 0)

    teams = list(totals.keys())
    my_ranks = {}
    for c in CATS:
        ranked = sorted(teams, key=lambda t: -totals[t][c])
        for rank, t in enumerate(ranked, 1):
            if t == my_key:
                my_ranks[c] = rank
    return my_ranks, len(teams)


POS_GROUPS = [
    ("C",  {"C"},                   "hit"),
    ("1B", {"1B"},                  "hit"),
    ("2B", {"2B"},                  "hit"),
    ("3B", {"3B"},                  "hit"),
    ("SS", {"SS"},                  "hit"),
    ("OF", {"OF", "LF", "CF", "RF"},"hit"),
    ("SP", {"SP"},                  "pit"),
    ("RP", {"RP"},                  "pit"),
]


def positional_breakdown(pitchers, hitters, my_team, best_recent_p=None, best_recent_h=None):
    my_key = " ".join(my_team.split())
    if best_recent_p is None:
        best_recent_p = {r["PlayerName"]: r for r in pitchers if int(r.get("Dataset", 0) or 0) == 30 and r.get("PlayerName")}
    if best_recent_h is None:
        best_recent_h = {r["PlayerName"]: r for r in hitters  if int(r.get("Dataset", 0) or 0) == 30 and r.get("PlayerName")}
    results = []
    for pos_label, slots, ptype in POS_GROUPS:
        source   = pitchers if ptype == "pit" else hitters
        score_fn = pitcher_score if ptype == "pit" else hitter_score
        idx30    = best_recent_p if ptype == "pit" else best_recent_h
        season   = [r for r in source if int(r.get("Dataset", 0) or 0) == YEAR]

        def pos_match(r, slots=slots, pos_label=pos_label):
            if pos_label == "SP":
                return _is_sp(r)
            if pos_label == "RP":
                parts = str(r.get("Position", "")).split(", ")
                return any(s in parts for s in slots) and not _is_sp(r)
            parts = str(r.get("Position", "")).split(", ")
            return any(s in parts for s in slots)

        def score(r, ptype=ptype, score_fn=score_fn, idx30=idx30):
            if ptype == "pit":
                return _score_p(r, idx30)   # role-aware: SP blend / RP rp_score
            return _blend(r, score_fn, idx30)

        my_p = sorted(
            [r for r in season if " ".join((r.get("FantasyTeam") or "").split()) == my_key and pos_match(r)],
            key=lambda r: -score(r),
        )
        for r in my_p:
            r["_pscore"] = score(r)

        # Per-team average score at this position → league rank
        team_scores = {}
        for r in season:
            t = r.get("FantasyTeam", "")
            if t and pos_match(r):
                team_scores.setdefault(t, []).append(score(r))
        team_avgs = sorted(sum(v) / len(v) for v in team_scores.values())
        my_avg = sum(r["_pscore"] for r in my_p) / len(my_p) if my_p else 0
        n = len(team_avgs)
        rank = n - sum(1 for s in team_avgs if s <= my_avg) + 1 if n else None

        # Viable check: only count players actually getting opportunities
        if ptype == "pit":
            if pos_label == "SP":
                gs_min = _pit_viable_min("SP", "GS")
                viable = lambda r: _n(r.get("GS")) >= gs_min
            else:
                gp_min = _pit_viable_min("RP", "GP")
                ip_min = _pit_viable_min("RP", "IP")
                viable = lambda r: _n(r.get("ESPN_GP")) >= gp_min or _n(r.get("IP")) >= ip_min
        else:
            viable = lambda r: _n(r.get("OPS")) > 0.200 or _n(r.get("R")) + _n(r.get("RBI")) > 5

        # Best FA at this position (exclude DL players and benchies)
        fa = sorted(
            [r for r in season if r.get("FantasyTeam", "") == "" and pos_match(r)
             and str(r.get("FreeAgentInjuryStatus", "")) not in _DL_STATUSES
             and viable(r)],
            key=lambda r: -score(r),
        )
        for r in fa:
            r["_pscore"] = score(r)

        top3 = [r["_pscore"] for r in fa[:3]]
        fa_quality = sum(top3) / len(top3) if top3 else 0
        # Weakest player is an implicit drop/replace target, so skip anyone parked in an
        # IL slot — cutting them frees no active/bench room. Fall back to the full list
        # only if every player at the position is on IL.
        drop_pool = [r for r in my_p if not _on_il(r)] or my_p
        results.append({
            "pos":          pos_label,
            "ptype":        ptype,
            "worst_player": drop_pool[-1] if drop_pool else None,
            "my_avg":       round(my_avg, 1),
            "rank":         rank,
            "n_teams":      n,
            "top_fa":       fa[:1],
            "fa_depth":     len(fa),
            "fa_quality":   fa_quality,
        })
    return results


def roster_alerts(pitchers, hitters, my_team):
    my_key = " ".join(my_team.split())
    seen = set()
    alerts = []
    for r in pitchers + hitters:
        if " ".join((r.get("FantasyTeam") or "").split()) != my_key or int(r.get("Dataset", 0)) != YEAR:
            continue
        name = r["PlayerName"]
        inj = _get_injury_status(r)
        if inj and name not in seen:
            alerts.append({"name": name, "status": inj})
            seen.add(name)
    return alerts


def my_upcoming_starts(pitchers, my_team, week_end=None):
    my_key = " ".join(my_team.split())
    today_str = datetime.now().strftime("%Y-%m-%d")
    sp = [
        r for r in pitchers
        if " ".join((r.get("FantasyTeam") or "").split()) == my_key
        and int(r.get("Dataset", 0)) == YEAR
        and r.get("PSP_Date", "") not in ("1999-01-01", "", None)
        and r.get("PSP_Date", "") >= today_str
        and (week_end is None or r.get("PSP_Date", "") <= week_end)
    ]
    return sorted(sp, key=lambda r: r.get("PSP_Date", ""))


def opponent_week_intel(pitchers, hitters, opp_team, best_recent_h, today_str, week_end_str):
    """Scouting data on what the opponent brings this week: their upcoming starts,
    two-start pitchers, and hottest bats (by recent OPS). Returns a dict or None."""
    if not opp_team:
        return None
    opp_key = " ".join(opp_team.split())
    opp_sp = [
        r for r in pitchers
        if int(r.get("Dataset", 0) or 0) == YEAR
        and " ".join((r.get("FantasyTeam") or "").split()) == opp_key
        and r.get("PSP_Date", "") not in ("1999-01-01", "", None)
        and today_str <= r.get("PSP_Date", "") <= week_end_str
        and _is_sp(r)
    ]
    n_starts  = sum(_starts_this_week(r, today_str, week_end_str) or 1 for r in opp_sp)
    two_start = [r for r in opp_sp if _starts_this_week(r, today_str, week_end_str) >= 2]

    def _recent_ops(r):
        rr = best_recent_h.get(r.get("PlayerName", "")) or {}
        return _n(rr.get("OPS")) or _n(r.get("OPS"))

    opp_hit = [r for r in hitters
               if int(r.get("Dataset", 0) or 0) == YEAR
               and " ".join((r.get("FantasyTeam") or "").split()) == opp_key
               and _recent_ops(r) > 0]
    hot = sorted(opp_hit, key=_recent_ops, reverse=True)[:3]
    return {"n_starters": len(opp_sp), "n_starts": n_starts,
            "two_start": two_start, "hot_hitters": [(r, _recent_ops(r)) for r in hot]}


# ── TEAM LOGOS ────────────────────────────────────────────────────────────────

_TEAM_ESPN = {
    "ARI": "ari", "ATL": "atl", "BAL": "bal", "BOS": "bos",
    "CHC": "chc", "CWS": "chw", "CIN": "cin", "CLE": "cle",
    "COL": "col", "DET": "det", "HOU": "hou", "KC":  "kc",
    "LAA": "laa", "LAD": "lad", "MIA": "mia", "MIL": "mil",
    "MIN": "min", "NYM": "nym", "NYY": "nyy", "ATH": "oak",
    "PHI": "phi", "PIT": "pit", "SD":  "sd",  "SEA": "sea",
    "SF":  "sf",  "STL": "stl", "TB":  "tb",  "TEX": "tex",
    "TOR": "tor", "WSH": "wsh",
}

_FULLNAME_TO_ABBREV = {
    "Arizona Diamondbacks": "ARI", "Atlanta Braves": "ATL",
    "Baltimore Orioles": "BAL",    "Boston Red Sox": "BOS",
    "Chicago Cubs": "CHC",         "Chicago White Sox": "CWS",
    "Cincinnati Reds": "CIN",      "Cleveland Guardians": "CLE",
    "Colorado Rockies": "COL",     "Detroit Tigers": "DET",
    "Houston Astros": "HOU",       "Kansas City Royals": "KC",
    "Los Angeles Angels": "LAA",   "Los Angeles Dodgers": "LAD",
    "Miami Marlins": "MIA",        "Milwaukee Brewers": "MIL",
    "Minnesota Twins": "MIN",      "New York Mets": "NYM",
    "New York Yankees": "NYY",     "Athletics": "ATH",
    "Oakland Athletics": "ATH",    "Sacramento Athletics": "ATH",
    "Philadelphia Phillies": "PHI","Pittsburgh Pirates": "PIT",
    "San Diego Padres": "SD",      "Seattle Mariners": "SEA",
    "San Francisco Giants": "SF",  "St. Louis Cardinals": "STL",
    "Tampa Bay Rays": "TB",        "Texas Rangers": "TEX",
    "Toronto Blue Jays": "TOR",    "Washington Nationals": "WSH",
}


def team_logo(abbrev, size=20):
    espn = _TEAM_ESPN.get(str(abbrev or "").upper(), "")
    if not espn:
        return ""
    return (
        f'<img src="https://a.espncdn.com/i/teamlogos/mlb/500/{espn}.png" '
        f'width="{size}" height="{size}" '
        f'style="vertical-align:middle;border-radius:2px;margin-right:5px;" '
        f'alt="{abbrev}">'
    )


def opp_logo(psp_home_away, size=18):
    """Return logo for the opponent team in a PSP_HomeVAway string."""
    if not psp_home_away or " " not in psp_home_away:
        return ""
    full_name = psp_home_away.split(" ", 1)[1]
    abbrev = _FULLNAME_TO_ABBREV.get(full_name, "")
    return team_logo(abbrev, size) if abbrev else ""


_FANTASY_EMOJI = {
    "Giga Vlad":        ("🧛", "#6d28d9"),  # Vlad the vampire
    "Dumpsta Fire":     ("🔥", "#ea580c"),  # dumpster fire
    "Kai-Wei Jelly":    ("🍇", "#7e22ce"),  # grape jelly
    "The BIG Dumpers":  ("💩", "#78350f"),  # self-explanatory
    "Walking Wounded":  ("🩹", "#0369a1"),  # always on the IL
}

_BAD_LOGO_DOMAINS = ("mystique-api.fantasy.espn.com", "cdn.citybeat.com")


def _emoji_avatar(team_name, size):
    """Colored circle with emoji or initials — used when logo URL won't render in email."""
    norm = " ".join(team_name.split())
    emoji, color = _FANTASY_EMOJI.get(norm, ("", ""))
    if not emoji:
        words = norm.split()
        emoji = "".join(w[0].upper() for w in words[:2])
        color = f"#{abs(hash(norm)) % 0xBBBBBB + 0x222222:06x}"
    font_size = max(10, int(size * 0.58))
    return (
        f'<span style="display:inline-block;width:{size}px;height:{size}px;'
        f'border-radius:50%;background:{color};text-align:center;'
        f'line-height:{size}px;font-size:{font_size}px;'
        f'vertical-align:middle;margin-right:6px;">{emoji}</span>'
    )


def fantasy_logo(url, size=26, team_name=""):
    """Render a fantasy team logo. Falls back to an emoji avatar for auth-gated or dead URLs."""
    if not url or any(d in url for d in _BAD_LOGO_DOMAINS):
        return _emoji_avatar(team_name, size) if team_name else ""
    return (
        f'<img src="{url}" width="{size}" height="{size}" '
        f'style="vertical-align:middle;border-radius:50%;margin-right:6px;object-fit:contain;" '
        f'alt="">'
    )


# ── HTML HELPERS ───────────────────────────────────────────────────────────────

BG       = "#080e1c"
SURFACE  = "#101827"
SURFACE2 = "#0d1424"
BORDER   = "#1e2d45"
TEXT     = "#e2e8f0"
MUTED    = "#64748b"
ACCENT   = "#3b82f6"
GREEN    = "#22c55e"
RED      = "#ef4444"
YELLOW   = "#f59e0b"
PURPLE   = "#a855f7"   # 2-START badge — distinct from green (quality/QS) & yellow (K/5K+)

TH_S = f"padding:8px 10px;background:{SURFACE};color:{MUTED};font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.7px;border-bottom:2px solid {BORDER};white-space:nowrap;"
TD_S = f"padding:7px 10px;border-bottom:1px solid {BORDER};color:{TEXT};font-size:13px;vertical-align:middle;"
TDC  = f"padding:7px 10px;border-bottom:1px solid {BORDER};color:{TEXT};font-size:13px;text-align:center;vertical-align:middle;"


def badge(score):
    s = int(score or 0)
    if s >= 72:   bg, fg = "#16a34a", "#fff"
    elif s >= 52: bg, fg = "#2563eb", "#fff"
    elif s >= 32: bg, fg = "#d97706", "#fff"
    else:          bg, fg = "#dc2626", "#fff"
    return f'<span style="background:{bg};color:{fg};padding:2px 9px;border-radius:12px;font-size:11px;font-weight:800;">{s}</span>'


# ── Tap-to-expand Score breakdown (v2) ─────────────────────────────────────────
# The Score badge links to a hidden, FULL-WIDTH <tr> rendered directly below the
# player's row; tapping the badge reveals it via CSS :target (browser-opened
# attachment). This replaces the v1 in-cell <details> panel, which looked cramped
# expanding inside the narrow Score column. The panel narrates the 2-3 most decisive
# score DRIVERS in prose (see _score_narrative) instead of a "points/max" list.
#
# Degradation: the row carries inline display:none; a `:target { … !important }` rule
# in the head <style> overrides it when tapped. Gmail's inline body strips <style>, so
# the rows stay hidden there and the badge link is a harmless no-op — the score badge
# itself always shows. The user reads the attachment, where the reveal works.

_BD_SEQ = [0]


def _bd_uid(prefix, name):
    """Globally-unique anchor id for one breakdown row (a player can appear in several
    tables, so the running counter guarantees uniqueness across the document)."""
    _BD_SEQ[0] += 1
    slug = re.sub(r"[^a-z0-9]", "", str(name or "").lower())[:16]
    return f"bd-{prefix}-{slug}-{_BD_SEQ[0]}"


def _st(x, dec=3):
    """Format a stat, dropping the leading zero for sub-1 values (0.272 → '.272')."""
    s = f"{x:.{dec}f}"
    return s[1:] if 0 <= x < 1 else s


def _hit_clauses(r, comps):
    """(fill, strength_phrase, weakness_phrase) per hitter component, for narration."""
    ops = _n(r.get("OPS")); wrc = _n(r.get("wRCplus")); hr = _n(r.get("HR"))
    iso = _n(r.get("ISO")); rbi = _n(r.get("RBI")); sb = _n(r.get("SB"))
    sprint = _n(r.get("SprintSpeed")); xwoba = _n(r.get("xwOBA")); hrp = _n(r.get("HR_Probability"))
    maxes = {"Prod": 30, "HR": 16, "ISO": 6, "RBI": 10, "Speed": 10, "xwOBA": 10, "HR%": 8}
    out = []

    def add(key, strong, weak):
        if key in comps:
            out.append((comps[key] / maxes[key], strong, weak))

    prod_stat = f"wRC+ {int(wrc)}" if wrc > 0 else f"OPS {_st(ops)}"
    add("Prod", f"strong production ({prod_stat})", f"weak production ({prod_stat})")
    add("HR", f"real power ({int(hr)} HR)", f"little power ({int(hr)} HR)")
    if iso > 0:
        add("ISO", f"big raw power (ISO {_st(iso)})", f"flat ISO ({_st(iso)})")
    add("RBI", f"drives in runs ({int(rbi)} RBI)", f"few RBI ({int(rbi)})")
    if sprint > 0:
        add("Speed", f"plus speed ({sprint:.1f} ft/s)", f"slow ({sprint:.1f} ft/s)")
    else:
        add("Speed", f"steals bags ({int(sb)} SB)", f"no steals ({int(sb)} SB)")
    if xwoba > 0:
        add("xwOBA", f"quality contact (xwOBA {_st(xwoba)})", f"empty contact (xwOBA {_st(xwoba)})")
    if hrp > 0:
        add("HR%", f"high HR odds ({hrp * 100:.0f}%/gm)", None)   # low HR odds isn't a real weakness
    return out


def _sp_clauses(r, comps):
    """(fill, strength, weakness) per SP component."""
    kpct = _n(r.get("Kpct_P")); kip = _n(r.get("K/IP")); era = _n(r.get("ERA"))
    whip = _n(r.get("WHIP")); brl = _n(r.get("BarrelPctAllowed")); xwoba_ag = _n(r.get("xwOBA_against"))
    maxes = {"K": 28, "RunPrev": 28, "WHIP": 20, "Contact": 12, "Role": 12}
    out = []

    def add(key, strong, weak):
        if key in comps:
            out.append((comps[key] / maxes[key], strong, weak))

    kstat = f"{kpct * 100:.0f}% K" if kpct > 0 else (f"{kip:.2f} K/IP" if kip > 0 else "K rate")
    add("K", f"swing-and-miss ({kstat})", f"low strikeouts ({kstat})")
    add("RunPrev", f"prevents runs ({era:.2f} ERA)", f"gets hit hard ({era:.2f} ERA)")
    add("WHIP", f"limits baserunners ({whip:.2f} WHIP)", f"high WHIP ({whip:.2f})")
    if brl > 0:
        add("Contact", f"soft contact ({brl:.1f}% barrels)", f"hard contact ({brl:.1f}% barrels)")
    elif xwoba_ag > 0:
        add("Contact", f"soft contact (xwOBA-ag {_st(xwoba_ag)})", f"hard contact (xwOBA-ag {_st(xwoba_ag)})")
    return out   # Role (start volume) is a marker, not a skill — left out of the narrative


def _rp_clauses(r, comps):
    """(fill, strength, weakness) per RP component."""
    svhd = _n(r.get("ESPN_SVHD")) or _n(r.get("SVHD"))
    k = _n(r.get("ESPN_K")) or _n(r.get("K"))
    w = _n(r.get("ESPN_W")) or _n(r.get("W"))
    ipg = _n(r.get("IP_per_G")); era = _n(r.get("ERA")); whip = _n(r.get("WHIP"))
    brl = _n(r.get("BarrelPctAllowed"))
    maxes = {"SVHD": 15, "K": 26, "W": 15, "IP/G": 8, "RunPrev": 16, "WHIP": 12, "Contact": 8}
    out = []

    def add(key, strong, weak):
        if key in comps:
            out.append((comps[key] / maxes[key], strong, weak))

    add("SVHD", f"save/hold volume ({int(svhd)} SV+H)", None)   # punt-saves: low SVHD isn't a knock
    add("K", f"racks up Ks ({int(k)} K)", f"low K total ({int(k)})")
    add("W", f"vulturing wins ({int(w)} W)", None)
    add("IP/G", f"multi-inning role ({ipg:.1f} IP/app)", None)
    add("RunPrev", f"prevents runs ({era:.2f} ERA)", f"gets hit ({era:.2f} ERA)")
    add("WHIP", f"limits baserunners ({whip:.2f} WHIP)", f"high WHIP ({whip:.2f})")
    if brl > 0:
        add("Contact", f"soft contact ({brl:.1f}% barrels)", f"hard contact ({brl:.1f}% barrels)")
    return out


def _score_narrative(clauses):
    """Turn per-component (fill, strength, weakness) tuples into a prose sentence naming
    the 2 strongest drivers (fill ≥ .60) and the 2 weakest (fill ≤ .35)."""
    strengths = sorted([(f, s) for (f, s, w) in clauses if s and f >= 0.60], key=lambda x: -x[0])
    weaks     = sorted([(f, w) for (f, s, w) in clauses if w and f <= 0.35], key=lambda x: x[0])
    s_txt = [t for _, t in strengths[:2]]
    w_txt = [t for _, t in weaks[:2]]
    if s_txt and w_txt:
        return f"Carried by {' and '.join(s_txt)}; held back by {', '.join(w_txt)}."
    if s_txt:
        return f"Carried by {' and '.join(s_txt)}; no glaring holes."
    if w_txt:
        return f"Held back by {', '.join(w_txt)}."
    return "Balanced across the board — no standout strength or weakness."


def _hitter_score_breakdown(r, idx_recent=None):
    """Prose breakdown of a hitter's Score for the tap-to-expand panel."""
    comps, mult = hitter_score(r, _parts=True)
    if not comps:
        return ""
    season = hitter_score(r)
    html = f'<b style="color:{TEXT};">Hitter score {season}.</b> {_score_narrative(_hit_clauses(r, comps))}'
    if mult < 0.995:
        html += f' Trimmed to {round(mult * 100)}% for thin playing time (few at-bats vs a regular).'
    if idx_recent:
        rec = idx_recent.get(r.get("PlayerName", ""))
        if rec:
            rs = hitter_score(rec)
            if rs > 0:
                ds  = int(_n(rec.get("Dataset")) or 0)
                win = f"{ds}-day" if ds in (7, 15, 30) else "7-day"
                tag = "hot" if rs > season else ("cold" if rs < season else "steady")
                html += (f' {win} form {rs} ({tag}) → shown blends '
                         f'{round((1 - _BLEND_W) * 100)}% season / {round(_BLEND_W * 100)}% recent.')
    hrp = _n(r.get("HR_Probability"))
    if hrp > 0:
        drivers = _hrp_driver_str(r)
        line = f'HR% {hrp * 100:.0f}% modeled per-game HR probability'
        line += f' ({drivers})' if drivers else ''
        html += f'<div style="margin-top:6px;color:{MUTED};">{line}</div>'
    return html


def _pitcher_score_breakdown(r, idx_recent=None):
    """Prose breakdown of a pitcher's Score. Role-aware: SP → pitcher_score components
    (blended with recent form); RP → rp_score (unblended)."""
    if _is_sp(r):
        comps, mult = pitcher_score(r, _parts=True)
        season, role, clauses = pitcher_score(r), "SP", _sp_clauses(r, comps)
    else:
        comps, mult = rp_score(r, _parts=True)
        season, role, clauses = rp_score(r), "RP", _rp_clauses(r, comps)
    if not comps:
        return ""
    html = f'<b style="color:{TEXT};">{role} score {season}.</b> {_score_narrative(clauses)}'
    if mult < 0.995:
        html += f' Trimmed to {round(mult * 100)}% for small innings sample.'
    if role == "SP" and idx_recent:
        rec = idx_recent.get(r.get("PlayerName", ""))
        if rec:
            rs = pitcher_score(rec)
            if rs > 0:
                ds  = int(_n(rec.get("Dataset")) or 0)
                win = f"{ds}-day" if ds in (7, 15, 30) else "15-day"
                tag = "hot" if rs > season else ("cold" if rs < season else "steady")
                html += (f' {win} form {rs} ({tag}) → shown blends '
                         f'{round((1 - _BLEND_W) * 100)}% season / {round(_BLEND_W * 100)}% recent.')
    return html


def score_reveal(score, breakdown_html, uid=None, colspan=1):
    """Return (cell_html, row_html): the Score-cell badge and the full-width breakdown
    <tr> to append immediately after the player's row. The badge is an anchor to the
    hidden row, revealed via CSS :target in the browser attachment. Falls back to a
    plain badge with an empty row when there is no breakdown or no uid."""
    if not breakdown_html or not uid:
        return badge(score), ""
    cell = (
        f'<a href="#{uid}" class="bdlink" title="Tap for score breakdown" '
        f'style="text-decoration:none;white-space:nowrap;">{badge(score)}'
        f'<span style="color:{MUTED};font-size:9px;font-weight:700;">&nbsp;&#9662;</span></a>'
    )
    row = (
        f'<tr id="{uid}" class="scorebd-row" style="display:none;">'
        f'<td colspan="{colspan}" style="padding:0;border-bottom:1px solid {BORDER};">'
        f'<div style="background:{SURFACE2};padding:8px 14px;font-size:11px;line-height:1.55;'
        f'color:{MUTED};font-weight:400;border-left:3px solid {ACCENT};">'
        f'{breakdown_html}'
        f'<a href="#{uid}x" style="color:{MUTED};text-decoration:none;font-weight:700;'
        f'float:right;margin-left:10px;">&#10005;</a>'
        f'</div></td></tr>'
    )
    return cell, row


def v(val, dec=2):
    """Format a numeric value; show em-dash for missing/negative."""
    try:
        f = float(val or 0)
        if f < 0:
            return f'<span style="color:{MUTED}">—</span>'
        return f"{f:.{dec}f}"
    except (TypeError, ValueError):
        return f'<span style="color:{MUTED}">—</span>'


def _fmt_ip(ip_decimal):
    """Convert decimal IP to baseball notation: 5.333 → '5.1', 5.667 → '5.2', 6.0 → '6.0'."""
    whole = int(ip_decimal)
    outs = round((ip_decimal - whole) * 3)
    if outs >= 3:
        whole += 1
        outs = 0
    return f"{whole}.{outs}"


_LEAGUE_AVG_OPS = 0.717  # fallback only — league team-OPS is derived per snapshot into _LG

def _proj_line_vals(r):
    """Numeric projected single-game line → (ip_per_start, ER, K), or None when
    there's no usable IP/G. Shared by _proj_line_html (display) and the FA-SP /
    My-Upcoming-Starts badge logic, so a QS/5K+ badge tracks the SAME projected
    numbers the reader sees on the proj line."""
    ip_g = _n(r.get("IP_per_G"))
    if ip_g <= 0:
        return None
    era = _n(r.get("ERA"))
    kip = _n(r.get("K/IP"))

    # Adjust ER for opponent OPS vs league average, and home/away park effect
    opp_ops  = _n(r.get("Team_OPS_Value"))
    hva      = str(r.get("PSP_HomeVAway") or "")
    opp_factor  = min(1.20, max(0.80, opp_ops / (_LG.get("team_ops") or _LEAGUE_AVG_OPS))) if opp_ops > 0 else 1.0
    park_factor = 0.97 if hva.startswith("vs ") else (1.03 if hva.startswith("@ ") else 1.0)

    # Adjust K for the opponent lineup's strikeout rate: a whiff-prone offense inflates a
    # starter's Ks, a contact-heavy one suppresses them. Clamped tighter than ER (±15%)
    # since team K% varies less than team OPS. Falls back to 1.0 when opp K% is missing.
    opp_k     = _n(r.get("Team_K_Value"))
    k_factor  = min(1.15, max(0.85, opp_k / (_LG.get("team_k") or 0.22))) if opp_k > 0 else 1.0

    raw_er = era * ip_g / 9 if era > 0 else 0
    er = round(raw_er * opp_factor * park_factor)
    k  = round(kip * ip_g * k_factor) if kip > 0 else 0
    return ip_g, er, k


def _proj_is_qs(ip_g, er):
    """True when the projected line reads as a quality start (6+ displayed IP, ≤3 ER).
    Uses the same third-of-an-inning rounding as _fmt_ip so it matches what the reader
    sees: e.g. 5.84 IP/G displays as '6.0' and counts, 5.5 displays as '5.2' and does not."""
    whole = int(ip_g); outs = round((ip_g - whole) * 3)
    if outs >= 3:
        whole += 1
    return whole >= 6 and er <= 3


def _proj_line_html(r):
    vals = _proj_line_vals(r)
    if vals is None:
        return f'<span style="color:{MUTED}">—</span>'
    ip_g, er, k = vals
    return f'<span style="color:{MUTED};font-size:10px;white-space:nowrap;">{_fmt_ip(ip_g)}&nbsp;IP&thinsp;·&thinsp;{er}&nbsp;ER&thinsp;·&thinsp;{k}K</span>'


def _opp_ops_sub(r):
    """Small muted second line for the Matchup cell showing the opponent team's
    OPS — folded in from the former standalone 'Opp OPS' column (dropped to fit
    the 9→8-column pitcher tables on an iPad). Empty when OPS is missing."""
    val = _n(r.get("Team_OPS_Value"))
    if val <= 0:
        return ""
    return (
        f'<div style="color:{MUTED};font-size:10px;margin-top:1px;white-space:nowrap;">'
        f'Opp OPS {val:.3f}</div>'
    )


def band_divider(label, color=None, anchor=None):
    c = color or MUTED
    # Anchor target for the "jump to" nav. name+id maximizes email-client support;
    # where in-message anchors don't work the link is harmless and it jumps in the
    # browser-rendered attachment.
    anchor_html = f'<a name="{anchor}" id="{anchor}" style="text-decoration:none;"></a>' if anchor else ''
    # "↑ Top" back-link on the right of each anchored band, so a reader who jumped
    # down via the nav pills can return without scrolling. A matching-width left
    # spacer keeps the label visually centered. Jumps in the attachment; harmless
    # inline where fragment links are ignored.
    top_link = (
        f'<a href="#top" style="color:{MUTED};font-size:10px;font-weight:700;'
        f'letter-spacing:1px;text-decoration:none;white-space:nowrap;">↑&nbsp;TOP</a>'
    ) if anchor else ''
    right_cell = f'<span style="padding-left:14px;">{top_link}</span>' if anchor else ''
    left_spacer = '<span style="padding-right:14px;">&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;</span>' if anchor else ''
    return (
        f'{anchor_html}'
        f'<div style="display:flex;align-items:center;margin:32px 0 22px;">'
        f'<div style="flex:1;height:1px;background:{BORDER};"></div>'
        f'{left_spacer}'
        f'<span style="padding:0 14px;color:{c};font-size:10px;font-weight:700;'
        f'letter-spacing:2px;text-transform:uppercase;">{label}</span>'
        f'{right_cell}'
        f'<div style="flex:1;height:1px;background:{BORDER};"></div>'
        f'</div>'
    )


def nav_bar():
    """'Jump to' pill nav, rendered in the top-right of the header (not the body, so it
    doesn't push Week at a Glance down). Anchor links behave like tabs without JS/CSS
    tricks that Gmail strips; they jump in the attachment and degrade to harmless styled
    links inline. Also drops the `top` anchor so the band `↑ TOP` links have a target."""
    items = [
        ("#band-myroster", "My Roster"),
        ("#band-fa",       "Free Agents"),
        ("#band-season",   "Season"),
        ("#band-glossary", "Glossary"),
    ]
    pills = "".join(
        f'<a href="{href}" style="display:inline-block;padding:5px 11px;margin:0 0 5px 5px;'
        f'background:rgba(255,255,255,0.04);border:1px solid {BORDER};border-radius:13px;color:#8fb4e8;'
        f'font-size:11px;font-weight:700;text-decoration:none;letter-spacing:.3px;white-space:nowrap;">{label}</a>'
        for href, label in items
    )
    return (
        f'<a name="top" id="top" style="text-decoration:none;"></a>'
        f'<div style="text-align:right;line-height:1.6;">'
        f'<span style="color:{MUTED};font-size:9px;font-weight:700;text-transform:uppercase;'
        f'letter-spacing:1px;display:block;margin-bottom:3px;">Jump to</span>{pills}</div>'
    )


def vp(val):
    """Format a decimal-stored percentage (0.28 → 28.0%)."""
    try:
        f = float(val or 0)
        if f <= 0:
            return f'<span style="color:{MUTED}">—</span>'
        return f"{f * 100:.1f}%"
    except (TypeError, ValueError):
        return f'<span style="color:{MUTED}">—</span>'


def _hrp_driver_str(row):
    """The HR-probability drivers (Barrel% · HardHit% · EV · xwOBA · ISO) as a joined
    string, or "" when none are present. Single source for the HR% hover tooltip and
    the expanded score-breakdown panel (so touch users see the same drivers)."""
    b, hh, xw, ev, iso = (_n(row.get("Barrel_Pct")), _n(row.get("HardHit_Pct")),
                          _n(row.get("xwOBA")), _n(row.get("MaxEV")), _n(row.get("ISO")))
    parts = []
    if b > 0:   parts.append(f"Barrel {b:.1f}%")
    if hh > 0:  parts.append(f"HardHit {hh:.0f}%")
    if ev > 0:  parts.append(f"EV {ev:.0f}")
    if xw > 0:  parts.append(f"xwOBA {xw:.3f}")
    if iso > 0: parts.append(f"ISO {iso:.3f}")
    return " · ".join(parts)


def _hrp_cell(row):
    """Colored HR% cell from the modeled per-game HR probability (HR_Probability,
    a Statcast contact-quality model). Hover shows the underlying drivers.
    `row` is a player dict carrying HR_Probability + the Statcast inputs."""
    hrp = _n(row.get("HR_Probability"))
    if hrp <= 0:
        return f'<span style="color:{MUTED};" title="No Statcast contact data — batter has too few batted balls for a model">—</span>'
    c = GREEN if hrp >= 0.20 else (YELLOW if hrp >= 0.14 else MUTED)
    # Underlying drivers as a hover tooltip (renders in the attachment; harmless inline)
    title = _hrp_driver_str(row) or "modeled HR probability"
    return (f'<span title="{title}" style="color:{c};font-weight:700;'
            f'border-bottom:1px dotted {MUTED};cursor:help;">{hrp*100:.0f}%</span>')


def pos_stat_line(r, pos):
    """Build a muted stat line for a player in the positional breakdown."""
    if pos == "RP":
        svhd = _n(r.get("ESPN_SVHD")) or _n(r.get("SVHD"))
        k    = _n(r.get("ESPN_K"))    or _n(r.get("K"))
        parts = []
        if svhd > 0: parts.append(f"SV+H {svhd:.0f}")
        if k    > 0: parts.append(f"K {k:.0f}")
        if not parts:
            return ""
        line = " · ".join(parts)
        return f'<div style="color:{MUTED};font-size:11px;margin-top:2px;">{line}</div>'
    elif pos == "SP":
        specs = [("ERA", 2), ("WHIP", 2), ("K", 0)]
    else:
        specs = [("HR", 0), ("RBI", 0), ("OPS", 3)]

    parts = []
    for key, dec in specs:
        raw = r.get(key)
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        if val < 0:
            continue
        parts.append(f"{key} {val:.{dec}f}")

    if not parts:
        return ""
    line = " · ".join(parts)
    return f'<div style="color:{MUTED};font-size:10px;margin-top:2px;">{line}</div>'


def hot_cold_cell(season_val, recent_val, lower_better=False, dec=2, hot_thresh=None, warm_thresh=None, no_data_title=None, td_style=None):
    """Table cell showing recent stat + hot/cold icon vs season baseline.
    td_style overrides the cell style (defaults to TDC) so a caller can render a
    tighter cell that matches a compacted table."""
    tdc = td_style or TDC
    _dash_cell = (
        f'<td style="{tdc}"><span style="color:{MUTED};cursor:help;border-bottom:1px dotted {MUTED};" title="{no_data_title}">—</span></td>'
        if no_data_title else f'<td style="{tdc}color:{MUTED};">—</td>'
    )
    try:
        sv = float(season_val or 0)
        rv = float(recent_val or 0)
        if sv <= 0 or rv <= 0:
            return _dash_cell
    except (TypeError, ValueError):
        return _dash_cell

    ht = hot_thresh  if hot_thresh  is not None else (0.75 if lower_better else 0.050)
    wt = warm_thresh if warm_thresh is not None else (0.25 if lower_better else 0.020)

    delta = (sv - rv) if lower_better else (rv - sv)   # positive = improvement

    if delta >= ht:
        icon, color = "🔥", GREEN
    elif delta >= wt:
        icon, color = "↑", GREEN
    elif delta <= -ht:
        icon, color = "❄", RED
    elif delta <= -wt:
        icon, color = "↓", RED
    else:
        icon, color = "", MUTED

    val_str = f"{rv:.{dec}f}"
    return (
        f'<td style="{tdc}">'
        f'<span style="color:{color};">{val_str}</span>'
        f'{"&nbsp;" + icon if icon else ""}'
        f'</td>'
    )


def inj_tag(r):
    inj = _get_injury_status(r)
    if not inj:
        return ""
    color = RED if (inj in _DL_STATUSES or inj.startswith("IL")) else YELLOW
    return f' <span style="color:{color};font-size:10px;font-weight:600;">{_fmt_status(inj)}</span>'


def section_head(title, sub=""):
    subtitle = f'<div style="color:{MUTED};font-size:11px;margin-top:2px;">{sub}</div>' if sub else ""
    return (
        f'<div style="border-left:3px solid {ACCENT};padding-left:11px;margin:0 0 10px 0;">'
        f'<div style="color:{TEXT};font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;">{title}</div>'
        f'{subtitle}</div>'
    )


def make_sparkline(roto, my_team, current_week, n=99, weekly_results=None):
    """
    SVG line chart scaled against the league-wide 5th/95th percentile.
    Dots: medal (🏅) = ranked #1 roto that week among all 12 teams (appears above dot);
          green filled circle = personal peak week; grey = everything else.
    Returns (svg_html, peak_label) tuple.
    """
    my_key = " ".join(my_team.split())
    wr = weekly_results or {}

    my_scores = {}
    league_vals = []
    for row in roto:
        wk = int(row.get("Week", 0))
        if wk >= current_week:
            continue
        val = float(row.get("Roto_Score") or 0)
        league_vals.append(val)
        t = " ".join((row.get("Team") or "").split())
        if t == my_key:
            my_scores[wk] = val

    past = sorted(my_scores.keys())[-n:]
    if len(past) < 2:
        return ("", "")

    league_vals.sort()
    trim = max(1, len(league_vals) // 20)
    lo = league_vals[trim]
    hi = league_vals[-trim]
    rng = hi - lo or 1

    vals  = [my_scores[w] for w in past]
    weeks = list(past)
    peak_wk = weeks[vals.index(max(vals))]

    # SVG geometry — scale width to number of points (min 130)
    # PAD_T (top padding) reserves room for the ★ marker above peak dots without overflow:visible
    n_pts = len(vals)
    SW, SH, PAD, PAD_T = max(130, n_pts * 14), 50, 5, 14

    def sx(i):
        return PAD + (i / max(n_pts - 1, 1)) * (SW - 2 * PAD)

    def sy(v):
        norm = max(0.0, min(1.0, (v - lo) / rng))
        return PAD_T + (1 - norm) * (SH - PAD_T - PAD)

    pts  = [(sx(i), sy(v)) for i, v in enumerate(vals)]
    line = " ".join(f"{px:.1f},{py:.1f}" for px, py in pts)
    fill = f"{pts[0][0]:.1f},{SH} " + line + f" {pts[-1][0]:.1f},{SH}"

    dots = []
    for i, (wk, v) in enumerate(zip(weeks, vals)):
        cx, cy = pts[i]
        wk_res = wr.get(wk) or wr.get(str(wk), {})
        is_first = (wk_res.get(my_key) or wk_res.get(my_team, "")) == "W"
        if wk == peak_wk:
            # ★ (U+2605) instead of medal emoji — font-size is honored in SVG unlike emoji
            star = f'<text x="{cx:.1f}" y="{cy - 6:.1f}" text-anchor="middle" font-size="8" fill="{YELLOW}">&#9733;</text>' if is_first else ""
            dots.append(
                f'{star}<circle cx="{cx:.1f}" cy="{cy:.1f}" r="3.5" fill="{GREEN}" stroke="#0d1424" stroke-width="1"/>'
            )
        elif is_first:
            dots.append(
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="2" fill="{YELLOW}"/>'
                f'<text x="{cx:.1f}" y="{cy - 6:.1f}" text-anchor="middle" font-size="8" fill="{YELLOW}">&#9733;</text>'
            )
        else:
            dots.append(
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="1.8" fill="#4b5563"/>'
            )

    svg = (
        f'<svg width="{SW}" height="{SH}" style="display:inline-block;vertical-align:middle;" xmlns="http://www.w3.org/2000/svg">'
        f'<polygon points="{fill}" fill="{ACCENT}" opacity="0.12"/>'
        f'<polyline points="{line}" fill="none" stroke="{ACCENT}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'
        f'{"".join(dots)}'
        f'</svg>'
    )

    peak_label = f'<div style="color:{GREEN};font-size:9px;margin-top:2px;">Peak Wk: {peak_wk}</div>'
    return svg, peak_label


def kpi_cell(label, value):
    return (
        f'<td class="kpi-cell" style="text-align:center;padding:14px 8px;border-right:1px solid {BORDER};">'
        f'<div style="color:{MUTED};font-size:10px;text-transform:uppercase;letter-spacing:.7px;">{label}</div>'
        f'<div style="color:{TEXT};font-size:20px;font-weight:800;margin-top:3px;">{value}</div>'
        f'</td>'
    )


def kpi_cell_sm(label, value, color=None, font_size="20px", font_weight="800"):
    val_color = color or TEXT
    return (
        f'<td class="kpi-cell" style="text-align:center;padding:8px 8px 10px;border-right:1px solid {BORDER};">'
        f'<div style="color:{MUTED};font-size:9px;text-transform:uppercase;letter-spacing:.7px;">{label}</div>'
        f'<div style="color:{val_color};font-size:{font_size};font-weight:{font_weight};margin-top:3px;">{value}</div>'
        f'</td>'
    )


# ── MATCHUP SECTION ───────────────────────────────────────────────────────────

_CAT_LABELS_MAP = {
    "R": "R", "HR": "HR", "RBI": "RBI", "SB": "SB", "OPS": "OPS",
    "B_SO": "B/SO", "K": "K", "QS": "QS", "W": "W",
    "ERA": "ERA", "WHIP": "WHIP", "SVHD": "SV+H",
}
_CAT_DEC = {
    "OPS": 3, "ERA": 2, "WHIP": 2,
}


def build_matchup_section(matchup, logos=None, my_team=MY_TEAM,
                          weekly_avgs=None, days_elapsed=None, remaining_proj=None):
    if not matchup or not matchup.get("categories"):
        return ""

    logos   = logos or {}
    wins    = matchup["wins"]
    losses  = matchup["losses"]
    ties    = matchup["ties"]
    opp     = matchup.get("opp_team", "Opponent")
    week    = matchup.get("week", "")

    # Projection setup (mirrors build_category_pulse)
    my_team_key  = " ".join(matchup.get("my_team",  "").split())
    opp_team_key = " ".join(matchup.get("opp_team", "").split())
    elapsed_frac = min(1.0, max(0.0, (days_elapsed or 0) / 7))
    my_avgs  = (weekly_avgs or {}).get(my_team_key,  {})
    opp_avgs = (weekly_avgs or {}).get(opp_team_key, {})
    has_proj = bool(my_avgs and opp_avgs)

    score_str = f"{wins}-{losses}-{ties}"
    if wins > losses:
        score_color, status = GREEN, "Winning"
    elif losses > wins:
        score_color, status = RED, "Losing"
    else:
        score_color, status = TEXT, "Tied"

    opp_short = opp[:16] + ("…" if len(opp) > 16 else "")

    def _norm(n): return " ".join(n.split())
    my_logo_html  = fantasy_logo(logos.get(_norm(my_team), ""), 36, my_team)
    opp_logo_html = fantasy_logo(logos.get(_norm(opp), ""), 36, opp)

    # Pre-compute projections for all categories
    proj_map = {}
    for c in matchup["categories"]:
        cat  = c["cat"]
        my_v = c["my_val"]
        ov   = c["opp_val"]
        rp   = (remaining_proj or {}).get(cat)
        if rp is not None:
            proj_map[cat] = {"pm": my_v + rp["my"], "po": ov + rp["opp"]}
        elif has_proj and cat in my_avgs and cat in opp_avgs:
            proj_map[cat] = {
                "pm": _project(my_v, my_avgs[cat], elapsed_frac, cat),
                "po": _project(ov,   opp_avgs[cat], elapsed_frac, cat),
            }

    # Projected record
    proj_w = proj_l = proj_t = 0
    for c in matchup["categories"]:
        cat = c["cat"]
        p   = proj_map.get(cat)
        if p is None:
            continue
        dec = _CAT_DEC.get(cat, 0)
        pm_r = round(p["pm"], dec)
        po_r = round(p["po"], dec)
        lower = cat in _LOWER_BETTER
        if pm_r == po_r:
            proj_t += 1
        elif (pm_r < po_r) == lower:
            proj_w += 1
        else:
            proj_l += 1

    if proj_map:
        pw_col = f"{score_color}99"
        proj_record_html = (
            f'<div style="font-size:10px;font-weight:400;color:{MUTED};margin-top:3px;">'
            f'proj <span style="color:{pw_col};font-weight:600;">'
            f'{proj_w}-{proj_l}'
            + (f'-{proj_t}' if proj_t else '')
            + f'</span></div>'
        )
    else:
        proj_record_html = ""

    score_banner = (
        f'<table style="width:100%;border-collapse:collapse;background:{SURFACE};'
        f'border-radius:6px;margin-bottom:12px;">'
        f'<tr>'
        f'<td style="width:42%;padding:12px 16px;font-size:13px;font-weight:800;color:{ACCENT};text-align:center;">'
        f'{my_logo_html}{my_team} &#8592;</td>'
        f'<td style="width:16%;text-align:center;padding:12px 8px;">'
        f'<div style="font-size:10px;color:{MUTED};text-transform:uppercase;letter-spacing:.5px;">{status}</div>'
        f'<div style="font-size:18px;font-weight:900;color:{score_color};">{score_str}</div>'
        f'{proj_record_html}'
        f'</td>'
        f'<td style="width:42%;padding:12px 16px;font-size:13px;font-weight:700;color:{TEXT};text-align:center;">'
        f'{opp_logo_html}{opp_short}</td>'
        f'</tr></table>'
    )

    rows = ""
    for i, c in enumerate(matchup["categories"]):
        cat   = c["cat"]
        my_v  = c["my_val"]
        opp_v = c["opp_val"]
        res   = c["result"]
        dec   = _CAT_DEC.get(cat, 0)
        label = _CAT_LABELS_MAP.get(cat, cat)

        my_color  = GREEN if res == "W" else (RED   if res == "L" else MUTED)
        opp_color = RED   if res == "W" else (GREEN if res == "L" else MUTED)

        p = proj_map.get(cat)

        # Projected outcome for this category (my perspective). This colors the proj
        # values by the PROJECTED status — not the current one — so a category I'm
        # currently losing but projected to win shows a red current value with a green
        # projection. Also drives the flip arrow.
        proj_res = None
        if p is not None:
            pm_r, po_r = round(p["pm"], dec), round(p["po"], dec)
            lower = cat in _LOWER_BETTER
            proj_res = "T" if pm_r == po_r else ("W" if (pm_r < po_r) == lower else "L")
        my_proj_c  = GREEN if proj_res == "W" else (RED   if proj_res == "L" else MUTED)
        opp_proj_c = RED   if proj_res == "W" else (GREEN if proj_res == "L" else MUTED)

        # Flip arrow (▲ to a win, ▼ to a loss, ◆ to a tie) when the projected result
        # differs from the current one. Shown on my side's projection.
        flip_arrow = ""
        if proj_res is not None and proj_res != res:
            if proj_res == "W":
                flip_arrow = f'&nbsp;<span style="color:{GREEN};">&#9650;</span>'
            elif proj_res == "L":
                flip_arrow = f'&nbsp;<span style="color:{RED};">&#9660;</span>'
            else:
                flip_arrow = f'&nbsp;<span style="color:{TEXT};">&#9670;</span>'

        def _proj_span(val, color, arrow=""):
            if val is None:
                return ""
            return (f'<div style="font-size:9px;font-weight:400;color:{MUTED};margin-top:2px;">'
                    f'proj <span style="color:{color};font-weight:600;">{val:.{dec}f}</span>{arrow}</div>')

        cat_label = f'<span style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:{MUTED};">{label}</span>'
        arrow_l = f'<span style="color:{ACCENT};">&#9664;</span>' if res == "W" else ''
        arrow_r = f'<span style="color:{YELLOW};">&#9654;</span>' if res == "L" else ''
        mid = (
            f'<table style="width:100%;border-collapse:collapse;"><tr>'
            f'<td style="width:22%;text-align:right;padding:0 4px 0 0;">{arrow_l}</td>'
            f'<td style="width:56%;text-align:center;padding:0;">{cat_label}</td>'
            f'<td style="width:22%;text-align:left;padding:0 0 0 4px;">{arrow_r}</td>'
            f'</tr></table>'
        )
        mid_color = MUTED

        bg = f"background:{SURFACE2};" if i % 2 else ""
        rows += (
            f'<tr style="{bg}">'
            f'<td style="{TDC}font-weight:700;color:{my_color};font-size:14px;">'
            f'{my_v:.{dec}f}{_proj_span(p["pm"] if p else None, my_proj_c, flip_arrow)}</td>'
            f'<td style="{TDC}color:{mid_color};">{mid}</td>'
            f'<td style="{TDC}font-weight:700;color:{opp_color};font-size:14px;">'
            f'{opp_v:.{dec}f}{_proj_span(p["po"] if p else None, opp_proj_c)}</td>'
            f'</tr>'
        )

    table = (
        f'<table style="width:100%;border-collapse:collapse;margin-bottom:24px;font-size:13px;">'
        f'<thead><tr>'
        f'<th style="{TH_S}width:42%;text-align:center;">{my_team}</th>'
        f'<th style="{TH_S}width:16%;text-align:center;"></th>'
        f'<th style="{TH_S}width:42%;text-align:center;">{opp_short}</th>'
        f'</tr></thead><tbody>{rows}</tbody></table>'
    )

    return (
        section_head(f"Week {week} Matchup", f"vs. {opp} · current standings") +
        score_banner +
        table
    )


# ── ROSTER HOT/COLD ──────────────────────────────────────────────────────────

def build_hot_cold_section(hitters, recent_hitting, my_team, best_recent_h=None):
    if not recent_hitting:
        return ""

    # Index recent stats by player name
    recent = {r["PlayerName"]: r for r in recent_hitting if r.get("PlayerName")}

    # Get my rostered hitters with season OPS
    season_year = YEAR
    my_hitters = [
        r for r in hitters
        if " ".join((r.get("FantasyTeam") or "").split()) == " ".join(my_team.split())
        and int(r.get("Dataset", 0)) == season_year
        and float(r.get("OPS") or 0) > 0
    ]
    if not my_hitters:
        return ""

    rows_data = []
    for r in my_hitters:
        name = r["PlayerName"]
        season_ops = float(r.get("OPS") or 0)
        rec = recent.get(name, {})
        recent_ops = float(rec.get("OPS") or 0) if rec else None
        recent_g   = int(rec.get("G") or 0) if rec else 0

        delta = (recent_ops - season_ops) if recent_ops else None
        rows_data.append({
            "name":       name,
            "pos":        r.get("Position", ""),
            "team":       r.get("Team", ""),
            "season_ops": season_ops,
            "recent_ops": recent_ops,
            "recent_g":   recent_g,
            "delta":      delta,
            "inj":        inj_tag(r),
            "srow":       r,   # full season row for the HR% tooltip drivers
            "score":      _blend(r, hitter_score, best_recent_h) if best_recent_h is not None else hitter_score(r),
        })

    # Sort: players with recent data first (by delta desc), then no-data players
    with_data    = sorted([r for r in rows_data if r["delta"] is not None], key=lambda x: -x["delta"])
    without_data = [r for r in rows_data if r["delta"] is None]
    sorted_rows  = with_data + without_data

    rows_html = ""
    for i, r in enumerate(sorted_rows):
        bg = f"background:{SURFACE2};" if i % 2 else ""
        delta = r["delta"]

        if delta is None:
            delta_html = f'<span style="color:{MUTED};">—</span>'
            arrow = ""
        elif delta >= 0.050:
            delta_html = f'<span style="color:{GREEN};font-weight:700;">+{delta:.3f}</span>'
            arrow = f'<span style="color:{GREEN};">🔥</span>'
        elif delta >= 0.015:
            delta_html = f'<span style="color:{GREEN};">+{delta:.3f}</span>'
            arrow = f'<span style="color:{GREEN};">↑</span>'
        elif delta <= -0.050:
            delta_html = f'<span style="color:{RED};font-weight:700;">{delta:.3f}</span>'
            arrow = f'<span style="color:{RED};">❄</span>'
        elif delta <= -0.015:
            delta_html = f'<span style="color:{RED};">{delta:.3f}</span>'
            arrow = f'<span style="color:{RED};">↓</span>'
        else:
            delta_html = f'<span style="color:{MUTED};">{delta:+.3f}</span>'
            arrow = ""

        recent_str = (
            f'{r["recent_ops"]:.3f} <span style="color:{MUTED};font-size:10px;">({r["recent_g"]}G)</span>'
            if r["recent_ops"] else f'<span style="color:{MUTED};">—</span>'
        )

        _cell, _bdrow = score_reveal(
            r["score"], _hitter_score_breakdown(r["srow"], best_recent_h),
            _bd_uid("rhc", r["name"]), 7)
        rows_html += (
            f'<tr style="{bg}">'
            f'<td style="{TD_S}font-weight:600;">{team_logo(r["team"])}{r["name"]}{r["inj"]}</td>'
            f'<td style="{TDC}color:{MUTED};">{r["pos"]}</td>'
            f'<td style="{TDC}">{r["season_ops"]:.3f}</td>'
            f'<td style="{TDC}">{recent_str}</td>'
            f'<td style="{TDC}">{delta_html} {arrow}</td>'
            f'<td style="{TDC}">{_hrp_cell(r["srow"])}</td>'
            f'<td style="{TDC}">{_cell}</td>'
            f'</tr>'
            f'{_bdrow}'
        )

    n_hot  = sum(1 for r in with_data if r["delta"] >= 0.015)
    n_cold = sum(1 for r in with_data if r["delta"] <= -0.015)
    sub = f"{n_hot} hot · {n_cold} cold · last 7 days vs season OPS · HR% = modeled per-game HR probability"

    return (
        section_head("Roster Hot/Cold", sub) +
        f'<table style="width:100%;border-collapse:collapse;margin-bottom:24px;font-size:13px;">'
        f'<thead><tr>'
        f'<th style="{TH_S}">Hitter</th>'
        f'<th style="{TH_S}text-align:center;">Pos</th>'
        f'<th style="{TH_S}text-align:center;">Season OPS</th>'
        f'<th style="{TH_S}text-align:center;">Last 7 OPS</th>'
        f'<th style="{TH_S}text-align:center;">Δ</th>'
        f'<th style="{TH_S}text-align:center;">HR%</th>'
        f'<th style="{TH_S}text-align:center;">Score</th>'
        f'</tr></thead><tbody>{rows_html}</tbody></table>'
    )


def build_pitcher_hot_cold_section(pitchers, my_team, rec_p=None, best_recent_p=None):
    my_key = " ".join(my_team.split())

    # Season rows for my pitchers
    season = {
        r["PlayerName"]: r for r in pitchers
        if " ".join((r.get("FantasyTeam") or "").split()) == my_key
        and int(r.get("Dataset", 0) or 0) == YEAR
        and _n(r.get("ERA")) > 0
    }
    if not season:
        return ""

    # 15-day rows as "recent"; fall back to pybaseball 15-day scrape for fringe players
    recent_15 = {
        r["PlayerName"]: r for r in pitchers
        if int(r.get("Dataset", 0) or 0) == 15
    }

    rows_data = []
    for name, r in season.items():
        season_era = _n(r.get("ERA"))
        rec        = recent_15.get(name) or (rec_p or {}).get(name, {})
        recent_era = _n(rec.get("ERA")) if rec else None
        recent_ip  = _n(rec.get("IP"))  if rec else 0

        # Require at least 3 IP in the recent window to avoid noise
        if recent_era and recent_ip < 3:
            recent_era = None

        # delta > 0 means recent ERA is LOWER (better) → hot
        delta = (season_era - recent_era) if recent_era and season_era else None
        rows_data.append({
            "name":       name,
            "pos":        r.get("Position", ""),
            "team":       r.get("Team", ""),
            "season_era": season_era,
            "recent_era": recent_era,
            "recent_ip":  recent_ip,
            "delta":      delta,
            "inj":        inj_tag(r),
            "srow":       r,   # season row for the score-breakdown panel
            "score":      _score_p(r, best_recent_p),
        })

    with_data    = sorted([r for r in rows_data if r["delta"] is not None], key=lambda x: -x["delta"])
    without_data = [r for r in rows_data if r["delta"] is None]
    sorted_rows  = with_data + without_data

    rows_html = ""
    for i, r in enumerate(sorted_rows):
        bg    = f"background:{SURFACE2};" if i % 2 else ""
        delta = r["delta"]

        if delta is None:
            delta_html = f'<span style="color:{MUTED};">—</span>'
            arrow = ""
        elif delta >= 1.00:
            delta_html = f'<span style="color:{GREEN};font-weight:700;">-{delta:.2f}</span>'
            arrow = f'<span style="color:{GREEN};">🔥</span>'
        elif delta >= 0.40:
            delta_html = f'<span style="color:{GREEN};">-{delta:.2f}</span>'
            arrow = f'<span style="color:{GREEN};">↑</span>'
        elif delta <= -1.00:
            delta_html = f'<span style="color:{RED};font-weight:700;">+{abs(delta):.2f}</span>'
            arrow = f'<span style="color:{RED};">❄</span>'
        elif delta <= -0.40:
            delta_html = f'<span style="color:{RED};">+{abs(delta):.2f}</span>'
            arrow = f'<span style="color:{RED};">↓</span>'
        else:
            sign = "-" if delta >= 0 else "+"
            delta_html = f'<span style="color:{MUTED};">{sign}{abs(delta):.2f}</span>'
            arrow = ""

        recent_str = (
            f'{r["recent_era"]:.2f} <span style="color:{MUTED};font-size:10px;">({r["recent_ip"]:.0f} IP)</span>'
            if r["recent_era"] else f'<span style="color:{MUTED};">—</span>'
        )

        _cell, _bdrow = score_reveal(
            r["score"], _pitcher_score_breakdown(r["srow"], best_recent_p),
            _bd_uid("phc", r["name"]), 6)
        rows_html += (
            f'<tr style="{bg}">'
            f'<td style="{TD_S}font-weight:600;">{team_logo(r["team"])}{r["name"]}{r["inj"]}</td>'
            f'<td style="{TDC}color:{MUTED};">{r["pos"]}</td>'
            f'<td style="{TDC}">{r["season_era"]:.2f}</td>'
            f'<td style="{TDC}">{recent_str}</td>'
            f'<td style="{TDC}">{delta_html} {arrow}</td>'
            f'<td style="{TDC}">{_cell}</td>'
            f'</tr>'
            f'{_bdrow}'
        )

    n_hot  = sum(1 for r in with_data if r["delta"] >= 0.40)
    n_cold = sum(1 for r in with_data if r["delta"] <= -0.40)
    sub    = f"{n_hot} hot · {n_cold} cold · last 15 days vs season ERA"

    return (
        section_head("Pitcher Hot/Cold", sub) +
        f'<table style="width:100%;border-collapse:collapse;margin-bottom:24px;font-size:13px;">'
        f'<thead><tr>'
        f'<th style="{TH_S}">Pitcher</th>'
        f'<th style="{TH_S}text-align:center;">Pos</th>'
        f'<th style="{TH_S}text-align:center;">Season ERA</th>'
        f'<th style="{TH_S}text-align:center;">Last 15 ERA</th>'
        f'<th style="{TH_S}text-align:center;">Δ</th>'
        f'<th style="{TH_S}text-align:center;">Score</th>'
        f'</tr></thead><tbody>{rows_html}</tbody></table>'
    )


# ── CATEGORY PULSE ───────────────────────────────────────────────────────────

_RATE_CATS    = {"OPS", "ERA", "WHIP", "B_SO"}   # use weighted-avg projection
_LOWER_BETTER = {"ERA", "WHIP", "B_SO"}

_CLOSE_THRESH = {
    "R": 8, "HR": 3, "RBI": 8, "SB": 3, "OPS": 0.025, "B_SO": 0.08,
    "K": 8, "QS": 2, "W": 2, "ERA": 0.30, "WHIP": 0.08, "SVHD": 3,
}

_HIT_CATS = {"R", "HR", "RBI", "SB", "OPS", "B_SO"}
_PIT_CATS = {"K", "QS", "W",   "ERA", "WHIP", "SVHD"}

# ── FA "Cats" column — which roto categories a free agent most helps ──────────────
# Only cats surfaced on the FA rows (B_SO omitted — not shown on FA hitter rows; QS is
# SP-only and FA RP has none). ERA/WHIP are lower-is-better (handled via _LOWER_BETTER).
_FA_HIT_CATS = ["R", "HR", "RBI", "SB", "OPS"]
_FA_RP_CATS  = ["SVHD", "K", "W", "ERA", "WHIP"]


def _cat_value(row, cat):
    """Raw per-player value for a roto category (RP counting stats prefer ESPN season)."""
    if cat == "SVHD":
        return _n(row.get("ESPN_SVHD")) or _n(row.get("SVHD"))
    if cat == "K":
        return _n(row.get("ESPN_K")) or _n(row.get("K"))
    if cat == "W":
        return _n(row.get("ESPN_W")) or _n(row.get("W"))
    return _n(row.get(cat))


def build_cat_percentiles(rows, cats):
    """{cat: sorted pool values} for percentile lookup, from a qualified YEAR player pool
    of one type. Cats with too small a pool are omitted (→ no chip)."""
    out = {}
    for c in cats:
        vals = sorted(v for v in (_cat_value(r, c) for r in rows) if v > 0)
        if len(vals) >= 8:
            out[c] = vals
    return out


def _cat_pctile(pctile, cat, val):
    """Percentile (0–1) of val within the pool for cat; lower-is-better cats inverted."""
    import bisect
    pool = pctile.get(cat)
    if not pool or val <= 0:
        return 0.0
    p = bisect.bisect_left(pool, val) / len(pool)
    return (1.0 - p) if cat in _LOWER_BETTER else p


def player_cat_strengths(row, pctile, cats, need_cats, thresh=0.70):
    """Up to 3 cats the player is strong in (percentile ≥ thresh), need-cats first."""
    scored = []
    for c in cats:
        pv = _cat_pctile(pctile, c, _cat_value(row, c))
        if pv >= thresh:
            scored.append((c in need_cats, pv, c))
    scored.sort(key=lambda t: (not t[0], -t[1]))
    return [c for _, _, c in scored][:3]


def _cats_cell(row, pctile, cats, need_cats):
    """<td> of category chips a FA helps; need-cats highlighted (ACCENT), others MUTED."""
    strong = player_cat_strengths(row, pctile, cats, need_cats)
    if not strong:
        return f'<td style="{TDC}color:{MUTED};">—</td>'
    chips = []
    for c in strong:
        hot = c in need_cats
        col = ACCENT if hot else MUTED
        wt  = "700" if hot else "600"
        chips.append(f'<span style="color:{col};font-weight:{wt};font-size:11px;">{_CAT_DISPLAY.get(c, c)}</span>')
    inner = '<span style="color:#334155;"> · </span>'.join(chips)
    return f'<td style="{TDC}white-space:nowrap;">{inner}</td>'


def compute_weekly_avgs(roto, current_week):
    """Return {team: {cat: weekly_avg}} from all completed weeks before current_week."""
    from collections import defaultdict
    CATS = ["R", "HR", "RBI", "SB", "OPS", "B_SO", "K", "QS", "W", "ERA", "WHIP", "SVHD"]
    past = [r for r in roto if int(r.get("Week", 0)) < current_week]
    if not past:
        return {}
    buckets = defaultdict(lambda: {c: [] for c in CATS})
    for row in past:
        t = " ".join((row.get("Team", "") or "").split())  # normalize whitespace
        if not t:
            continue
        for c in CATS:
            try:
                buckets[t][c].append(float(row[c]))
            except (KeyError, TypeError, ValueError):
                pass
    return {t: {c: sum(v) / len(v) for c, v in cats.items() if v}
            for t, cats in buckets.items()}


def _project(current, avg, elapsed_frac, cat):
    """Project end-of-week value from current accumulated stat and historical weekly avg."""
    remaining = 1.0 - elapsed_frac
    if cat in _RATE_CATS:
        if elapsed_frac == 0:
            return avg  # no innings yet; NaN * 0 = NaN, so skip current entirely
        return current * elapsed_frac + avg * remaining   # weighted blend
    else:
        return current + remaining * avg                  # counting: add expected remainder


def classify_categories(matchup, weekly_avgs=None, days_elapsed=None, remaining_proj=None):
    """Classify each category's closeness, using the same projection math as Category
    Pulse. Returns {cat: (proj_res, tier)} where proj_res is W/L/T and tier is
    'tossup' (within a close-threshold — a thin margin) or 'leaning' (clear).
    Used to detect thin ERA/WHIP leads for the ratio-stat pickup warning."""
    out = {}
    if not matchup or not matchup.get("categories"):
        return out
    de = days_elapsed or 0
    elapsed_frac = min(1.0, max(0.0, de / 7))
    my_key  = " ".join(matchup.get("my_team",  "").split())
    opp_key = " ".join(matchup.get("opp_team", "").split())
    my_avgs  = (weekly_avgs or {}).get(my_key,  {})
    opp_avgs = (weekly_avgs or {}).get(opp_key, {})
    has_proj = bool(my_avgs and opp_avgs)

    def _tier(margin, thresh):
        return "tossup" if margin <= thresh else "leaning"

    for c in matchup["categories"]:
        cat   = c["cat"]
        my_v  = c["my_val"]
        opp_v = c["opp_val"]
        res   = c["result"]
        lower = cat in _LOWER_BETTER
        dec   = _CAT_DEC.get(cat, 0)
        thresh = _CLOSE_THRESH.get(cat, 999)

        rp = (remaining_proj or {}).get(cat)
        if rp is not None:
            pm, po = my_v + rp["my"], opp_v + rp["opp"]
        elif has_proj and cat in my_avgs and cat in opp_avgs:
            pm = _project(my_v,  my_avgs[cat],  elapsed_frac, cat)
            po = _project(opp_v, opp_avgs[cat], elapsed_frac, cat)
        else:
            pm = po = None

        if pm is None:
            # No projection available — judge by the current margin alone.
            margin = abs(round(my_v, dec) - round(opp_v, dec))
            out[cat] = (res, _tier(margin, thresh))
            continue

        pm_r, po_r = round(pm, dec), round(po, dec)
        if lower:
            proj_res = "W" if pm_r < po_r else ("T" if pm_r == po_r else "L")
        else:
            proj_res = "W" if pm_r > po_r else ("T" if pm_r == po_r else "L")
        out[cat] = (proj_res, _tier(abs(pm_r - po_r), thresh))
    return out


def build_category_pulse(matchup, weekly_avgs=None, days_elapsed=None, remaining_proj=None, is_sunday=False):
    if not matchup or not matchup.get("categories"):
        return ""

    week         = matchup.get("week", "")
    opp          = matchup.get("opp_team", "Opponent")
    my_team_key  = " ".join(matchup.get("my_team",  "").split())
    opp_team_key = " ".join(matchup.get("opp_team", "").split())

    # Projection setup
    elapsed_frac = min(1.0, max(0.0, (days_elapsed or 0) / 7))
    my_avgs  = (weekly_avgs or {}).get(my_team_key,  {})
    opp_avgs = (weekly_avgs or {}).get(opp_team_key, {})
    has_proj = bool(my_avgs and opp_avgs)
    proj_results = []

    def _card(c):
        cat   = c["cat"]
        my_v  = c["my_val"]
        opp_v = c["opp_val"]
        res   = c["result"]
        label = _CAT_LABELS_MAP.get(cat, cat)
        dec   = _CAT_DEC.get(cat, 0)

        if res == "W":
            border_c, val_c, status, status_c = GREEN,  GREEN,  "WINNING", GREEN
        elif res == "L":
            border_c, val_c, status, status_c = RED,    RED,    "LOSING",  RED
        else:
            border_c, val_c, status, status_c = TEXT,   TEXT,   "TIED",    TEXT

        margin = abs(my_v - opp_v)
        is_close = res in ("W", "L") and margin <= _CLOSE_THRESH.get(cat, 999)

        # Bar: % filled = my share of the total; invert for lower-is-better
        total = my_v + opp_v
        if total > 0:
            pct = (opp_v / total * 100) if cat in _LOWER_BETTER else (my_v / total * 100)
        else:
            pct = 50
        pct = max(5, min(95, pct))

        bar = (
            f'<div style="height:3px;background:{BORDER};border-radius:2px;margin:7px 0 5px;">'
            f'<div style="width:{pct:.0f}%;height:100%;background:{val_c};border-radius:2px;"></div>'
            f'</div>'
        )

        # Projection footer
        flip = False
        proj_res = None
        proj_html = ""
        pm = po = None
        rp = (remaining_proj or {}).get(cat)
        if rp is not None:
            # Use actual remaining starts × per-start rate (K, QS, W)
            pm = my_v  + rp["my"]
            po = opp_v + rp["opp"]
        elif has_proj and cat in my_avgs and cat in opp_avgs:
            pm = _project(my_v,  my_avgs[cat],  elapsed_frac, cat)
            po = _project(opp_v, opp_avgs[cat], elapsed_frac, cat)
        if pm is not None:
            lower = cat in _LOWER_BETTER
            pm_r = round(pm, dec)
            po_r = round(po, dec)
            if lower:
                proj_res = "W" if pm_r < po_r else ("T" if pm_r == po_r else "L")
            else:
                proj_res = "W" if pm_r > po_r else ("T" if pm_r == po_r else "L")

            flip = proj_res != res
            proj_html = (
                f'<div style="margin-top:4px;color:{MUTED};font-size:9px;">'
                f'proj&nbsp;<span style="color:{TEXT};">{pm:.{dec}f}</span>'
                f'&nbsp;vs&nbsp;{po:.{dec}f}'
                f'</div>'
            )

        # Top-right corner badge: ⚡ (close) and/or ▲▼ (flip)
        corner_parts = []
        if is_close:
            close_c = GREEN if res == "W" else RED
            corner_parts.append(f'<span style="color:{close_c};">⚡</span>')
        if flip:
            if proj_res == "W":
                flip_c, flip_arrow = GREEN, "▲"
            elif proj_res == "L":
                flip_c, flip_arrow = RED, "▼"
            else:
                flip_c, flip_arrow = TEXT, "◆"
            corner_parts.append(f'<span style="color:{flip_c};font-size:10px;">{flip_arrow}</span>')
        corner_html = (
            f'<div style="position:absolute;top:5px;right:6px;line-height:1;'
            f'display:flex;gap:2px;align-items:center;">{"".join(corner_parts)}</div>'
        ) if corner_parts else ""

        proj_results.append(proj_res)

        return (
            f'<td style="padding:4px;width:16.66%;">'
            f'<div style="position:relative;background:{SURFACE};border:1px solid {border_c}33;'
            f'border-top:2px solid {border_c};border-radius:6px;padding:9px 11px;height:100%;box-sizing:border-box;">'
            f'{corner_html}'
            f'<div style="color:{MUTED};font-size:9px;font-weight:700;'
            f'text-transform:uppercase;letter-spacing:.7px;">{label}</div>'
            f'<div style="margin-top:5px;">'
            f'<div style="color:{val_c};font-size:19px;font-weight:900;line-height:1.1;">{my_v:.{dec}f}</div>'
            f'<div style="color:{MUTED};font-size:11px;">vs {opp_v:.{dec}f}</div>'
            f'</div>'
            f'{bar}'
            f'<div style="color:{status_c};font-size:9px;font-weight:700;">{status}</div>'
            f'{proj_html}'
            f'</div></td>'
        )

    hit_cats = [c for c in matchup["categories"] if c["cat"] in _HIT_CATS]
    pit_cats = [c for c in matchup["categories"] if c["cat"] in _PIT_CATS]

    def _row(cat_list, label):
        cells = "".join(_card(c) for c in cat_list)
        return (
            f'<tr><td colspan="6" style="padding:4px 4px 2px;">'
            f'<div style="color:{MUTED};font-size:9px;font-weight:700;'
            f'text-transform:uppercase;letter-spacing:.6px;">{label}</div></td></tr>'
            f'<tr>{cells}</tr>'
        )

    wins   = matchup["wins"]
    losses = matchup["losses"]
    score_color = GREEN if wins > losses else (RED if losses > wins else TEXT)

    table = (
        f'<div style="overflow-x:auto;-webkit-overflow-scrolling:touch;margin-bottom:24px;">'
        f'<table style="width:100%;border-collapse:collapse;min-width:480px;">'
        f'{_row(hit_cats, "Hitting")}'
        f'<tr><td colspan="6" style="height:6px;"></td></tr>'
        f'{_row(pit_cats, "Pitching")}'
        f'</table></div>'
    )

    wins_count   = sum(1 for c in matchup["categories"] if c["result"] == "W")
    losses_count = sum(1 for c in matchup["categories"] if c["result"] == "L")
    ties_count   = sum(1 for c in matchup["categories"] if c["result"] == "T")
    close_count  = sum(
        1 for c in matchup["categories"]
        if c["result"] in ("W", "L") and abs(c["my_val"] - c["opp_val"]) <= _CLOSE_THRESH.get(c["cat"], 999)
    )
    summary = (
        f'<span style="color:{GREEN};font-weight:700;">{wins_count}W</span>'
        f'<span style="color:{MUTED};margin:0 4px;">·</span>'
        f'<span style="color:{RED};font-weight:700;">{losses_count}L</span>'
    )
    if ties_count:
        summary += (
            f'<span style="color:{MUTED};margin:0 4px;">·</span>'
            f'<span style="color:{TEXT};font-weight:700;">{ties_count}T</span>'
        )
    if close_count:
        summary += (
            f'<span style="color:{MUTED};margin:0 4px;">·</span>'
            f'<span style="color:{YELLOW};">⚡{close_count} close</span>'
        )

    proj_w = sum(1 for r in proj_results if r == "W")
    proj_l = sum(1 for r in proj_results if r == "L")
    proj_t = sum(1 for r in proj_results if r == "T")
    if any(r is not None for r in proj_results):
        pw_col = f"{GREEN}99"
        pl_col = f"{RED}99"
        summary += (
            f'<span style="color:{MUTED};margin:0 6px;font-size:11px;">→ proj</span>'
            f'<span style="color:{pw_col};font-weight:600;">{proj_w}W</span>'
            f'<span style="color:{MUTED};margin:0 4px;">·</span>'
            f'<span style="color:{pl_col};font-weight:600;">{proj_l}L</span>'
        )
        if proj_t:
            summary += (
                f'<span style="color:{MUTED};margin:0 4px;">·</span>'
                f'<span style="color:{TEXT}88;font-weight:600;">{proj_t}T</span>'
            )

    return (
        section_head(f"Category Pulse — Week {week}", f"vs. {opp} · {'Final stretch — week ends today' if is_sunday else '⚡ = within striking distance'}") +
        f'<div style="margin-bottom:8px;font-size:12px;">{summary}</div>' +
        table
    )


_CAT_DISPLAY = {
    "R": "R", "HR": "HR", "RBI": "RBI", "SB": "SB", "OPS": "OPS",
    "B_SO": "B/SO", "K": "K", "QS": "QS", "W": "W",
    "ERA": "ERA", "WHIP": "WHIP", "SVHD": "SV+H",
}


def build_prev_matchup_recap(prev_matchup, team_logos=None):
    if not prev_matchup or not prev_matchup.get("categories"):
        return ""

    week    = prev_matchup.get("week", "")
    opp     = prev_matchup.get("opp_team", "Opponent")
    my_team = prev_matchup.get("my_team", MY_TEAM)
    wins    = prev_matchup.get("wins", 0)
    losses  = prev_matchup.get("losses", 0)
    ties    = prev_matchup.get("ties", 0)
    cats    = prev_matchup.get("categories", [])
    _logos  = team_logos or {}

    if wins > losses:
        outcome_color, outcome_word = GREEN, "WIN"
    elif losses > wins:
        outcome_color, outcome_word = RED, "LOSS"
    else:
        outcome_color, outcome_word = TEXT, "TIE"

    score_str = f"{wins}-{losses}" + (f"-{ties}" if ties else "")

    cat_order = ["R", "HR", "RBI", "SB", "OPS", "B_SO", "K", "QS", "W", "ERA", "WHIP", "SVHD"]
    cat_map   = {c["cat"]: c for c in cats}

    def _fmt(val, cat):
        dec = 3 if cat == "OPS" else (2 if cat in {"ERA", "WHIP"} else 0)
        try:
            return f"{float(val):.{dec}f}"
        except (TypeError, ValueError):
            return "—"

    # Shared cell styles — tight padding to minimize horizontal scroll
    th = (f'padding:3px 5px;text-align:center;font-size:10px;font-weight:700;'
          f'color:{MUTED};text-transform:uppercase;letter-spacing:0;'
          f'border-bottom:1px solid {BORDER};white-space:nowrap;')
    td = f'padding:4px 5px;text-align:center;font-size:10px;font-weight:500;white-space:nowrap;'
    VAL_COLOR = "#94a3b8"

    # Header row: cat label colored + solid bottom border by result
    header_cells = f'<th style="{th}text-align:left;min-width:36px;"></th>'
    for i, cat in enumerate(cat_order):
        lbl = _CAT_DISPLAY.get(cat, cat)
        c   = cat_map.get(cat, {})
        res = c.get("result", "T")
        col = GREEN if res == "W" else (RED if res == "L" else MUTED)
        sep = f'border-left:1px solid {BORDER};' if i == 6 else ''
        header_cells += (
            f'<th style="{th}{sep}color:{col};border-bottom:2px solid {col};">'
            f'{lbl}</th>'
        )

    def _data_row(label, label_color, val_key, win_result):
        row = (f'<td style="{td}text-align:left;color:{label_color};font-weight:700;'
               f'font-size:11px;">{label}</td>')
        for i, cat in enumerate(cat_order):
            c   = cat_map.get(cat, {})
            val = c.get(val_key, 0)
            res = c.get("result", "T")
            left_border = f'border-left:1px solid {BORDER};' if i == 6 else ''
            val_str = _fmt(val, cat)
            if res == win_result:
                val_str = (f'<span style="outline:1px solid {TEXT}44;outline-offset:3px;'
                           f'border-radius:3px;display:inline-block;">{val_str}</span>')
            row += f'<td style="{td}color:{VAL_COLOR};{left_border}">{val_str}</td>'
        return f'<tr>{row}</tr>'

    my_logo_url  = _logos.get(" ".join(my_team.split()), "")
    opp_logo_url = _logos.get(" ".join(opp.split()), "")
    my_label  = fantasy_logo(my_logo_url,  18, my_team) + "Me"
    opp_label = fantasy_logo(opp_logo_url, 18, opp)     + "Opp"

    table = (
        f'<div style="overflow-x:auto;-webkit-overflow-scrolling:touch;margin-top:10px;">'
        f'<table style="width:100%;border-collapse:collapse;min-width:420px;">'
        f'<thead><tr>{header_cells}</tr></thead>'
        f'<tbody>'
        + _data_row(my_label,  ACCENT, "my_val",  "W")
        + _data_row(opp_label, TEXT,   "opp_val", "L")
        + f'</tbody></table></div>'
    )

    return (
        f'<div style="background:{SURFACE};border:1px solid {BORDER};border-radius:6px;'
        f'padding:12px 16px;margin-bottom:12px;">'
        f'<div style="color:{MUTED};font-size:10px;font-weight:700;text-transform:uppercase;'
        f'letter-spacing:.7px;margin-bottom:9px;">Last Week — Final Result</div>'
        f'<div style="display:flex;align-items:baseline;gap:10px;">'
        f'<span style="color:{outcome_color};font-weight:800;font-size:15px;">{outcome_word}</span>'
        f'<span style="color:{TEXT};font-weight:700;">{score_str}</span>'
        f'<span style="color:{MUTED};font-size:12px;">vs. {opp} &middot; Week {week}</span>'
        f'</div>'
        f'{table}'
        f'</div>'
    )


def _cat_score(r, cat):
    """Score a player on a single category for trade/add targeting."""
    if cat == "K":    return _n(r.get("ESPN_K"))   or _n(r.get("K"))
    if cat == "W":    return _n(r.get("ESPN_W"))   or _n(r.get("W"))
    if cat == "QS":   return qs_probability(r)
    if cat == "SVHD": return (_n(r.get("ESPN_SVHD")) or _n(r.get("SVHD")))
    if cat == "ERA":  era  = _n(r.get("ERA"));  return max(0, 6   - era)  if era  > 0 else 0
    if cat == "WHIP": whip = _n(r.get("WHIP")); return max(0, 2   - whip) if whip > 0 else 0
    if cat == "HR":   return _n(r.get("HR"))
    if cat == "RBI":  return _n(r.get("RBI"))
    if cat == "R":    return _n(r.get("R"))
    if cat == "SB":   return _n(r.get("SB"))
    if cat == "OPS":  return _n(r.get("OPS"))
    if cat == "B_SO": bso = _n(r.get("B_SO")); return max(0, 200 - bso) if bso > 0 else 0
    return 0


def _roster_suggestion(matchup, pitchers, hitters, fa_sp, fa_rp, fa_hit,
                        my_team, best_recent_p, best_recent_h,
                        all_matchups, week_end_str, classification=None):
    """Return one add/drop or trade suggestion bullet HTML for Week at a Glance."""
    if not matchup:
        return ""

    classification = classification or {}
    cats        = matchup.get("categories", [])
    my_norm     = " ".join(my_team.split())
    opp         = matchup.get("opp_team", "")
    losing      = [c for c in cats if c["result"] == "L"]
    losing_cats = {c["cat"] for c in losing}
    if not losing_cats:
        return ""

    losing_pit = losing_cats & _PIT_CATS
    losing_hit = losing_cats & _HIT_CATS
    # Week-at-a-Glance bullet 4 is dedicated to a NON-PITCHER (hitter) pickup — pitcher
    # streaming is already covered by My Upcoming Starts / FA Starting Pitchers, so this
    # bullet always steers to the bats. (Was: focus_pit = len(losing_pit) >= len(losing_hit).)
    focus_pit  = False

    # ── ADD / DROP ────────────────────────────────────────────────────────────
    add_candidate = drop_candidate = None
    add_reason    = ""

    # Always recommend the best available hitter — target our losing bat categories when
    # any, else frame it as general bat depth (we may be losing only pitching cats).
    fa_pool    = sorted(fa_hit, key=lambda r: _blend(r, hitter_score, best_recent_h), reverse=True)
    add_reason = ("/".join(_CAT_DISPLAY.get(c, c) for c in sorted(losing_hit)) + " gap") if losing_hit else "bat depth"
    add_candidate = fa_pool[0] if fa_pool else None

    # Determine what to DROP: weakest rostered player who won't strand a position.
    # Full roster (all positions, for coverage checking):
    full_pit = [r for r in pitchers
                if " ".join((r.get("FantasyTeam") or "").split()) == my_norm
                and int(r.get("Dataset", 0) or 0) == YEAR]
    full_hit = [r for r in hitters
                if " ".join((r.get("FantasyTeam") or "").split()) == my_norm
                and int(r.get("Dataset", 0) or 0) == YEAR]

    # Droppable candidates: pitchers without an upcoming start this week + all hitters
    drop_pit = [r for r in full_pit
                if r.get("PSP_Date", "1999-01-01") in ("1999-01-01", "")
                or r.get("PSP_Date", "9999-99-99") > week_end_str]
    scored_drop = sorted(
        [(r, _score_p(r, best_recent_p),              "pit") for r in drop_pit] +
        [(r, _blend(r, hitter_score, best_recent_h),  "hit") for r in full_hit],
        key=lambda x: x[1]
    )

    def _pos_tags(r):
        pos_str = (r.get("Position") or "").upper()
        return {p.strip() for p in pos_str.replace("/", ",").split(",") if p.strip()}

    def _pos_groups_of(r):
        """POS_GROUPS labels this player fills (OF covers LF/CF/RF etc.)."""
        tags = _pos_tags(r)
        return {label for label, slots, _ in POS_GROUPS if tags & slots}

    def _pos_disp(r):
        """Display positions, dropping the generic P tag when a real role exists."""
        parts = [p.strip() for p in str(r.get("Position") or "").split(",")
                 if p.strip() and p.strip().upper() != "P"]
        return ", ".join(parts) or str(r.get("Position") or "")

    def _can_drop(cand):
        """True if dropping cand leaves at least one healthy player at every position it fills."""
        # Never drop a player parked in a dedicated IL slot — those 2 slots don't take up
        # active/bench room, so cutting them frees nothing usable.
        if _on_il(cand):
            return False
        cand_name = cand.get("PlayerName", "")
        for _, slots, ptype in POS_GROUPS:
            if not (_pos_tags(cand) & slots):
                continue
            pool = full_pit if ptype == "pit" else full_hit
            healthy_others = [
                r for r in pool
                if r.get("PlayerName") != cand_name
                and _is_healthy(r)
                and (_pos_tags(r) & slots)
            ]
            if not healthy_others:
                return False
        return True

    # Prefer a like-for-like drop: same position group as the add first (so adding
    # an OF drops the worst OF, not an infielder), then same player type, then any.
    droppable = [(r, t) for r, _, t in scored_drop if _can_drop(r)]
    add_groups = _pos_groups_of(add_candidate) if add_candidate else set()
    add_type   = "pit" if focus_pit else "hit"
    drop_candidate = (
        next((r for r, _ in droppable if _pos_groups_of(r) & add_groups), None)
        or next((r for r, t in droppable if t == add_type), None)
        or (droppable[0][0] if droppable else None)
    )

    # Ratio-stat risk: streaming a mediocre SP for K/W/QS can flip a thin ERA/WHIP lead.
    ratio_warn = ""
    if add_candidate and _is_sp(add_candidate):
        res_by_cat = {c["cat"]: c["result"] for c in cats}
        anchors = {"ERA": (_n(add_candidate.get("ERA")),  4.20, "ERA"),
                   "WHIP": (_n(add_candidate.get("WHIP")), 1.30, "WHIP")}
        at_risk = [
            (lbl, val) for rc, (val, anchor, lbl) in anchors.items()
            if res_by_cat.get(rc) == "W"
            and classification.get(rc, (None, "leaning"))[1] == "tossup"   # only warn on a THIN lead
            and val > anchor
        ]
        if at_risk:
            lbl, val = at_risk[0]
            _today = datetime.now().strftime("%Y-%m-%d")
            ipg  = _n(add_candidate.get("IP_per_G"))
            n_st = _starts_this_week(add_candidate, _today, week_end_str) or 1
            ip_est = ipg * n_st
            ip_txt = f'~{_fmt_ip(ip_est)} IP' if ip_est > 0 else 'his innings'
            ratio_warn = (
                f'<span style="color:{YELLOW};"> &#9888; boosts K/W/QS but his '
                f'{val:.2f} {lbl} over {ip_txt} risks your thin {lbl} lead.</span>'
            )

    if add_candidate and drop_candidate:
        an = add_candidate.get("PlayerName", "")
        dn = drop_candidate.get("PlayerName", "")
        if an and dn and an != dn:
            return (
                f'Pickup: Add <span style="color:{TEXT};font-weight:700;">{an}</span>'
                f'<span style="color:{MUTED};"> ({_pos_disp(add_candidate)})</span>'
                f'<span style="color:{MUTED};"> ({add_reason})</span>'
                f' &middot; Drop <span style="color:{MUTED};">{dn} ({_pos_disp(drop_candidate)})</span>'
                f'{ratio_warn}'
            )

    # ── TRADE ─────────────────────────────────────────────────────────────────
    opp_matchup = all_matchups.get(" ".join(opp.split()), {}) if opp else {}
    if not opp_matchup:
        return ""

    opp_cats_map = {c["cat"]: c for c in opp_matchup.get("categories", [])}
    opp_winning  = {cat for cat, c in opp_cats_map.items() if c["result"] == "W"}
    my_winning   = {c["cat"] for c in cats if c["result"] == "W"}
    they_offer   = opp_winning  & losing_cats   # their surplus = my need
    i_offer      = my_winning   & {cat for cat, c in opp_cats_map.items() if c["result"] == "L"}

    if not they_offer or not i_offer:
        return ""

    # Pick primary categories: prefer pitching (more trade value stability)
    need_cat  = max(they_offer,  key=lambda c: (c in _PIT_CATS, _cat_score({}, c)))
    offer_cat = max(i_offer,     key=lambda c: (c in _PIT_CATS, _cat_score({}, c)))

    opp_norm = " ".join(opp.split())
    if need_cat in _PIT_CATS:
        pool = [r for r in pitchers if " ".join((r.get("FantasyTeam") or "").split()) == opp_norm
                and int(r.get("Dataset", 0) or 0) == YEAR]
        their_player = max(pool, key=lambda r: _cat_score(r, need_cat), default=None)
    else:
        pool = [r for r in hitters if " ".join((r.get("FantasyTeam") or "").split()) == opp_norm
                and int(r.get("Dataset", 0) or 0) == YEAR]
        their_player = max(pool, key=lambda r: _cat_score(r, need_cat), default=None)

    # Offer my 2nd-best in the offer category (skip ace — unrealistic to trade away)
    if offer_cat in _PIT_CATS:
        my_pool = sorted(
            [r for r in pitchers if " ".join((r.get("FantasyTeam") or "").split()) == my_norm
             and int(r.get("Dataset", 0) or 0) == YEAR],
            key=lambda r: _cat_score(r, offer_cat), reverse=True)
    else:
        my_pool = sorted(
            [r for r in hitters if " ".join((r.get("FantasyTeam") or "").split()) == my_norm
             and int(r.get("Dataset", 0) or 0) == YEAR],
            key=lambda r: _cat_score(r, offer_cat), reverse=True)
    my_offer = my_pool[1] if len(my_pool) > 1 else (my_pool[0] if my_pool else None)

    if their_player and my_offer:
        tn = their_player.get("PlayerName", "")
        mn = my_offer.get("PlayerName", "")
        nc = _CAT_DISPLAY.get(need_cat, need_cat)
        oc = _CAT_DISPLAY.get(offer_cat, offer_cat)
        if tn and mn:
            return (
                f'Trade: Offer <span style="color:{TEXT};font-weight:700;">{mn}</span>'
                f' to {opp} for <span style="color:{TEXT};font-weight:700;">{tn}</span>'
                f'<span style="color:{MUTED};"> — fills {nc} gap, gives them {oc}</span>'
            )

    return ""


def build_week_overview(matchup, week_cats, week_n, fa_sp, starts, days_elapsed, my_starts_by_day, week_end=None, is_sunday=False, roster_suggestion=""):
    bullets = []

    def _cat_label(key):
        return _CAT_DISPLAY.get(key, key)

    # Bullet 1: week record with hitting/pitching split summary
    if matchup:
        cw = matchup.get("wins", 0)
        cl = matchup.get("losses", 0)
        ct = matchup.get("ties", 0)
        opp = matchup.get("opp_team", "opponent")
        status_color = GREEN if cw > cl else (RED if cl > cw else TEXT)
        status_word  = "Leading" if cw > cl else ("Trailing" if cl > cw else "Tied")
        cats_list    = matchup.get("categories", [])
        hit_wins = sum(1 for c in cats_list if c["cat"] in _HIT_CATS and c.get("result") == "W")
        hit_loss = sum(1 for c in cats_list if c["cat"] in _HIT_CATS and c.get("result") == "L")
        hit_ties = sum(1 for c in cats_list if c["cat"] in _HIT_CATS and c.get("result") == "T")
        pit_wins = sum(1 for c in cats_list if c["cat"] in _PIT_CATS and c.get("result") == "W")
        pit_loss = sum(1 for c in cats_list if c["cat"] in _PIT_CATS and c.get("result") == "L")
        pit_ties = sum(1 for c in cats_list if c["cat"] in _PIT_CATS and c.get("result") == "T")
        hit_color = GREEN if hit_wins > hit_loss else (RED if hit_loss > hit_wins else TEXT)
        pit_color = GREEN if pit_wins > pit_loss else (RED if pit_loss > pit_wins else TEXT)
        if is_sunday:
            day_clause = ' — final'
        else:
            day_clause = f' through Day {days_elapsed}' if days_elapsed > 0 else ' (week starting)'
        bullets.append(
            f'<span style="color:{status_color};font-weight:700;">{status_word} {cw}-{cl}-{ct}</span>'
            f' vs. {opp}{day_clause} — '
            f'<span style="color:{hit_color};">batting {hit_wins}-{hit_loss}-{hit_ties}</span>, '
            f'<span style="color:{pit_color};">pitching {pit_wins}-{pit_loss}-{pit_ties}</span>.'
        )

    # Bullet 2: rotation coverage — on Sunday, show next-week starts instead
    if is_sunday:
        next_confirmed = [s for s in starts if s.get("PSP_Date", "1999-01-01") > (week_end or "")]
        nw_days = len(set(s["PSP_Date"] for s in next_confirmed))
        if next_confirmed:
            rot_str = (
                f'Next week: <span style="color:{ACCENT};font-weight:700;">{len(next_confirmed)} starts</span>'
                f' already lined up across {nw_days} day{"s" if nw_days != 1 else ""} — check FA SP below to fill gaps.'
            )
        else:
            rot_str = (
                f'<span style="color:{YELLOW};font-weight:700;">No confirmed starts next week yet</span>'
                f' — check FA SP section below and plan your pickups.'
            )
        bullets.append(rot_str)
    else:
        # Scope to the current matchup week so this matches the "Starts This Week" KPI
        # (PSP_Dates can run into next week; those get a NEXT WK badge elsewhere).
        wk_end = week_end or "9999-99-99"
        confirmed = [s for s in starts
                     if s.get("PSP_Date", "1999-01-01") != "1999-01-01"
                     and s.get("PSP_Date", "") <= wk_end]
        n_days = len(set(s["PSP_Date"] for s in confirmed))
        thin_days = sorted(d for d, cnt in my_starts_by_day.items() if cnt < 2 and d <= wk_end)
        if confirmed:
            rot_str = (
                f'<span style="color:{ACCENT};font-weight:700;">{len(confirmed)} starts</span>'
                f' queued across {n_days} day{"s" if n_days != 1 else ""}'
            )
            if thin_days:
                thin_labels = []
                for d in thin_days[:3]:
                    try:
                        thin_labels.append(datetime.strptime(d, "%Y-%m-%d").strftime("%a"))
                    except Exception:
                        thin_labels.append(d[5:])
                rot_str += (
                    f' — <span style="color:{YELLOW};">thin on {", ".join(thin_labels)}</span>,'
                    f' consider adding from FA below.'
                )
            else:
                rot_str += ' — rotation well-covered through the week.'
            bullets.append(rot_str)
        else:
            bullets.append(
                f'<span style="color:{RED};font-weight:700;">No confirmed starts</span>'
                f' yet — check FA SP section below.'
            )

    # Bullet 3: best FA SP pickup — on Sundays always target next week
    if fa_sp:
        def _pos_label(r):
            return "SP" if _is_sp(r) else (r.get("Position", "P") or "P")

        def _best_fa_str(pool, label_prefix="Best FA SP pickup"):
            if not pool:
                return ""
            _today = datetime.now().strftime("%Y-%m-%d")
            def _two(r):
                return _starts_this_week(r, _today, week_end or "9999-99-99") >= 2
            # Prefer a two-start pitcher (double the K/W/QS) among comparable candidates,
            # then fall back to highest QS probability.
            best = max(pool, key=lambda r: (_two(r), qs_probability(r) or 0))
            top  = pool[0]
            qsp  = qs_probability(best)
            try:
                day = datetime.strptime(best.get("PSP_Date", ""), "%Y-%m-%d").strftime("%a %b %d")
            except Exception:
                day = "?"
            qc = GREEN if qsp >= 60 else (YELLOW if qsp >= 40 else MUTED)
            s = (
                f'{label_prefix}: <span style="color:{TEXT};font-weight:700;">{best["PlayerName"]}</span>'
                f' <span style="color:{MUTED};font-size:10px;">({_pos_label(best)})</span>'
                f' ({day}'
            )
            if qsp:
                s += f', QS <span style="color:{qc};font-weight:700;">{qsp}%</span>'
            era = _n(best.get("ERA"))
            if era > 0:
                ec = GREEN if era < 3.50 else (YELLOW if era < 4.50 else MUTED)
                s += f', ERA <span style="color:{ec};">{era:.2f}</span>'
            kpct = _n(best.get("Kpct_P"))
            if kpct > 0:
                kc = GREEN if kpct >= 0.26 else (YELLOW if kpct >= 0.22 else TEXT)
                s += f', K% <span style="color:{kc};">{kpct*100:.1f}%</span>'
            s += ')'
            if _two(best):
                s += f' · <span style="color:{GREEN};font-weight:700;">×2 starts this week</span>'
            if top.get("PlayerName") != best.get("PlayerName"):
                s += (
                    f' · highest score: <span style="color:{TEXT};font-weight:600;">'
                    f'{top["PlayerName"]}</span>'
                    f' <span style="color:{MUTED};font-size:10px;">({_pos_label(top)})</span>'
                )
            return s

        if is_sunday:
            fa_next = [r for r in fa_sp if r.get("PSP_Date", "") > (week_end or "")]
            if fa_next:
                fa_str = _best_fa_str(fa_next, label_prefix="Top FA pickup next week")
            else:
                fa_str = f'<span style="color:{MUTED};">No confirmed FA starts next week yet — check back Monday.</span>'
            bullets.append(fa_str)
        else:
            fa_sp_this_week = [r for r in fa_sp if week_end is None or r.get("PSP_Date", "") <= week_end]
            if fa_sp_this_week:
                best_qs  = max(fa_sp_this_week, key=lambda r: qs_probability(r) or 0)
                best_qsp = qs_probability(best_qs)
                if best_qsp and best_qsp >= 50:
                    fa_str = _best_fa_str(fa_sp_this_week)
                else:
                    fa_next_any = [r for r in fa_sp if week_end is None or r.get("PSP_Date", "") > (week_end or "")]
                    if fa_next_any:
                        best_nw = max(fa_next_any, key=lambda r: qs_probability(r) or 0)
                        qsp_nw  = qs_probability(best_nw)
                        try:
                            day_nw = datetime.strptime(best_nw.get("PSP_Date", ""), "%Y-%m-%d").strftime("%a %b %d")
                        except Exception:
                            day_nw = "?"
                        qc_nw = GREEN if qsp_nw >= 60 else (YELLOW if qsp_nw >= 40 else MUTED)
                        fa_str = (
                            f'<span style="color:{MUTED};">No FA starters this week</span>'
                            f' — next week: <span style="color:{TEXT};font-weight:700;">{best_nw["PlayerName"]}</span>'
                            f' <span style="color:{MUTED};font-size:10px;">({_pos_label(best_nw)})</span>'
                            f' ({day_nw}'
                        )
                        if qsp_nw:
                            fa_str += f', QS <span style="color:{qc_nw};font-weight:700;">{qsp_nw}%</span>'
                        fa_str += ')'
                    else:
                        fa_str = f'<span style="color:{MUTED};">No upcoming FA starts found.</span>'
            else:
                fa_next_any = [r for r in fa_sp if r.get("PSP_Date", "") > (week_end or "")]
                if fa_next_any:
                    best_nw = max(fa_next_any, key=lambda r: qs_probability(r) or 0)
                    qsp_nw  = qs_probability(best_nw)
                    try:
                        day_nw = datetime.strptime(best_nw.get("PSP_Date", ""), "%Y-%m-%d").strftime("%a %b %d")
                    except Exception:
                        day_nw = "?"
                    qc_nw = GREEN if qsp_nw >= 60 else (YELLOW if qsp_nw >= 40 else MUTED)
                    fa_str = (
                        f'<span style="color:{MUTED};">No FA starters this week</span>'
                        f' — next week: <span style="color:{TEXT};font-weight:700;">{best_nw["PlayerName"]}</span>'
                        f' <span style="color:{MUTED};font-size:10px;">({_pos_label(best_nw)})</span>'
                        f' ({day_nw}'
                    )
                    if qsp_nw:
                        fa_str += f', QS <span style="color:{qc_nw};font-weight:700;">{qsp_nw}%</span>'
                    fa_str += ')'
                else:
                    fa_str = f'<span style="color:{MUTED};">No upcoming FA starts found.</span>'
            bullets.append(fa_str)

    if roster_suggestion:
        bullets.append(roster_suggestion)

    if not bullets:
        return ""

    items = "".join(
        f'<div style="padding:4px 0;font-size:13px;color:{TEXT};line-height:1.5;">'
        f'<span style="color:{ACCENT};margin-right:7px;">&#9656;</span>{b}'
        f'</div>'
        for b in bullets
    )
    return (
        f'<div style="background:#080e1c;border:1px solid {BORDER};border-radius:6px;'
        f'padding:13px 16px;margin-bottom:20px;">'
        f'<div style="color:{MUTED};font-size:10px;font-weight:700;text-transform:uppercase;'
        f'letter-spacing:.7px;margin-bottom:8px;">{"Next Week Preview" if is_sunday else "Week at a Glance"}</div>'
        f'{items}'
        f'</div>'
    )


def build_glossary_section():
    """In-digest reference explaining every score, metric, and data source.

    Email-safe: uses <details>/<summary> so it's collapsible in the browser-rendered
    attachment (and in clients that support it), and degrades to always-visible content
    in clients that don't toggle (e.g. Outlook) — never JS. Lives at the very bottom so
    an always-expanded fallback doesn't push the actionable sections down."""
    def _entry(term, body):
        return (
            f'<div style="margin:9px 0;">'
            f'<div style="color:{TEXT};font-weight:700;font-size:12px;">{term}</div>'
            f'<div style="color:{MUTED};font-size:11px;line-height:1.5;margin-top:2px;">{body}</div>'
            f'</div>'
        )

    def _group(title, entries):
        return (
            f'<details style="margin-bottom:8px;background:{SURFACE};border:1px solid {BORDER};'
            f'border-radius:8px;padding:4px 14px;">'
            f'<summary style="cursor:pointer;color:{ACCENT};font-weight:700;font-size:12px;'
            f'padding:8px 0;list-style:none;text-transform:uppercase;letter-spacing:.5px;">{title}</summary>'
            f'<div style="padding:2px 0 10px;">{"".join(entries)}</div>'
            f'</details>'
        )

    scores = _group("Scores (0–100)", [
        _entry("Role scores are unified",
               "Every player shows the <b>same</b> 0–100 score in every section, calibrated so the "
               "median qualified player ≈ 50 and a top-10% player ≈ 80. Benchmarks are derived from "
               "the live data each run, so “full-time” scales as the season grows."),
        _entry("Tap a Score badge for the breakdown",
               "Every Score badge expands on tap into a full-width row below the player that "
               "explains, in plain English, the 2-3 drivers behind the number (e.g. &ldquo;carried "
               "by swing-and-miss and a low WHIP; held back by hard contact&rdquo;) plus the "
               "season-vs-recent blend — so you can see <i>why</i> two similar-looking players score "
               "differently. The recent-form line names its actual window (e.g. “30-day form (cold)”), "
               "which is a broader window than the L7/L15 Hot/Cold column beside it — so a bat can be "
               "🔥 this week yet read “cold” on the longer composite. Hitter panels also list the HR% "
               "drivers on their own line (so touch users get them without hovering). Works in the "
               "browser-opened attachment; the ▾ caret marks a tappable "
               "badge, and the ✕ (or tapping another badge) closes it."),
        _entry("Starting-pitcher score",
               "K% (blended with Baseball Savant whiff percentile) + run prevention (ERA blended with "
               "Savant xERA) + WHIP + contact-quality allowed (barrel%/xwOBA-against) + a start-volume "
               "role bonus. Small samples are damped toward the mean. Blended 65% season / 35% recent form."),
        _entry("Relief-pitcher score",
               "Skill-weighted, punt-saves build: K, ERA (blended with xERA) and WHIP carry most of the "
               "weight; <b>SVHD (saves+holds) is deliberately de-emphasized (~15%)</b> since it's the most "
               "volatile category and one we're willing to sacrifice. A dominant setup man can outrank a "
               "mediocre closer. Counting stats prefer ESPN season totals."),
        _entry("Hitter score",
               "Prefers wRC+ over OPS, plus xwOBA, sprint speed, Barrel%, ISO and modeled HR probability. "
               "Scaled by an <b>opportunity multiplier</b> (at-bats vs a full-time benchmark) so a part-time "
               "bat can't score like a regular over a week. Blended 65% season / 35% recent form."),
        _entry("QS% (quality-start probability)",
               "Modeled chance a starter throws a quality start (6+ IP, ≤3 ER). League-average ≈ 38%, "
               "an ace ≈ 75%. Driven by innings-per-start, K%, ERA/WHIP and contact allowed."),
        _entry("QS / 5K+ badges",
               "Next to a starter's name in My Upcoming Starts and FA Starting Pitchers. They annotate "
               "his projected line for that day: green QS shows when the Proj. Line is a quality start "
               "(6+ IP & ≤3 ER); yellow 5K+ shows when it projects 5+ strikeouts. They match the Proj. Line "
               "exactly (no 5K+ badge next to a 4 K line) and appear regardless of your rotation that day. "
               "The QS% column shows the season quality-start probability separately."),
    ])
    pitching = _group("Pitching metrics", [
        _entry("xERA / xwOBA-against", "Baseball Savant “deserved” run prevention from contact quality — "
               "strips out luck and defense. Lower is better; blended with actual results in the scores."),
        _entry("Whiff percentile", "Where a pitcher's swing-and-miss rate ranks league-wide (0–100). "
               "A skill signal that leads strikeout results."),
        _entry("Barrel% / HardHit% allowed", "Share of batted balls against that are barrels (ideal "
               "exit-velo + angle) or hit ≥95 mph. Lower is better."),
        _entry("L15 ERA", "ERA over the last 15 days — the hot/cold window for starters, who pitch "
               "infrequently (7 days is too noisy). Compared against season ERA."),
        _entry("Proj. Line (IP · ER · K)", "Projected stat line for one upcoming start. ER adjusts the "
               "pitcher's ERA for opponent lineup strength (their OPS vs the league mean) and a home/away "
               "park factor; K uses his K/IP rate. IP is his per-start average."),
    ])
    hitting = _group("Hitting metrics", [
        _entry("wRC+", "Total offensive value indexed so 100 = league average; 150 = 50% better than "
               "average. Park- and league-adjusted."),
        _entry("xwOBA / xBA / xSLG", "Statcast “expected” outcomes from exit velo and launch angle — "
               "what a hitter's contact <i>should</i> yield, independent of defense and luck."),
        _entry("Barrel% / HardHit% / EV", "Quality-of-contact: barrels (ideal EV+angle), balls hit "
               "≥95 mph, and average exit velocity. Higher is better for a hitter."),
        _entry("HR%", "Modeled per-game home-run probability from barrel%, hard-hit%, launch angle, "
               "HR/AB, xwOBA, ISO and recent HR streak. Green ≥20%, yellow ≥14%. Hover shows the drivers "
               "(also listed in the hitter's expanded Score panel for touch devices)."),
        _entry("Sprint speed / ISO", "Statcast sprint speed (ft/sec, a steals/​range signal) and Isolated "
               "Power (SLG − AVG, extra-base power)."),
    ])
    proj = _group("Projections & matchup", [
        _entry("Category Pulse cards", "Per-category snapshot of the current week: your value vs the "
               "opponent, who's winning, and whether it's within striking distance (⚡)."),
        _entry("Projected values & flip arrows", "“proj” is the end-of-week estimate — for K/QS/W it uses "
               "your actual remaining starts × per-start rate; other cats use each team's weekly average. "
               "The projection is colored by its <b>projected</b> outcome (green = projected win, red = loss). "
               "An arrow marks a flip vs the current standing: ▲ flipping to a win, ▼ to a loss, ◆ to a tie."),
        _entry("Luck (standings)", "Roto rank minus record rank. Positive = your W-L is better than your "
               "category performance suggests (running lucky); negative = unlucky."),
    ])
    sources = _group("Data sources", [
        _entry("FantasyPros", "Pitcher & hitter stat lines across 4 ranges (last 7/15/30 days + season)."),
        _entry("ESPN Fantasy", "Rosters, free agents, weekly roto box scores, standings, transactions, "
               "and season counting totals (SV/K/W/IP) for pitchers."),
        _entry("MLB Stats API", "Probable starters (confirmed + a +6-day rotation projection) and the "
               "opponent lineup OPS each starter faces."),
        _entry("Baseball Savant (via pybaseball)", "Statcast: contact quality, expected stats (xERA, "
               "xwOBA, xBA/xSLG), sprint speed and whiff percentiles."),
    ])

    return (
        section_head("Glossary &amp; Methodology",
                     "How every score and metric is computed, and where the data comes from · tap a section to expand")
        + f'<div style="margin-bottom:24px;">{scores}{pitching}{hitting}{proj}{sources}</div>'
    )


# ── EMAIL BUILDER ─────────────────────────────────────────────────────────────

def build_email(snap, override_team=None):
    my_team       = override_team if override_team else snap.get("my_team", MY_TEAM)
    pitchers      = snap.get("pitchers", [])
    hitters       = snap.get("hitters", [])
    roto          = snap.get("roto", [])
    standings     = snap.get("standings", [])
    refreshed_iso = snap.get("refreshed_at", "")
    refreshed     = refreshed_iso[:10]
    refreshed_clock = _fmt_refresh_time(refreshed_iso)  # "6:32 AM ET" or "" — surfaced next to the freshness badge
    all_matchups  = snap.get("all_matchups", {})
    matchup       = all_matchups.get(" ".join(my_team.split())) or (snap.get("current_matchup", {}) if not override_team else {})
    recent_hitting  = snap.get("recent_hitting",  [])
    recent_pitching = snap.get("recent_pitching", [])
    weekly_results  = snap.get("weekly_results",  {})
    prev_matchup    = snap.get("prev_matchup",    {})
    rec_h = {r["PlayerName"]: r for r in recent_hitting  if r.get("PlayerName")}
    rec_p = {r["PlayerName"]: r for r in recent_pitching if r.get("PlayerName")}
    p7    = {r["PlayerName"]: r for r in pitchers if int(r.get("Dataset", 0) or 0) == 7  and r.get("PlayerName")}
    p15   = {r["PlayerName"]: r for r in pitchers if int(r.get("Dataset", 0) or 0) == 15 and r.get("PlayerName")}
    p30   = {r["PlayerName"]: r for r in pitchers if int(r.get("Dataset", 0) or 0) == 30 and r.get("PlayerName")}
    h7    = {r["PlayerName"]: r for r in hitters  if int(r.get("Dataset", 0) or 0) == 7  and r.get("PlayerName")}
    h15   = {r["PlayerName"]: r for r in hitters  if int(r.get("Dataset", 0) or 0) == 15 and r.get("PlayerName")}
    h30   = {r["PlayerName"]: r for r in hitters  if int(r.get("Dataset", 0) or 0) == 30 and r.get("PlayerName")}

    # Derive volume benchmarks from this snapshot so scoring/viability thresholds scale
    # with the season instead of stale hard-coded minimums (hitter AB, pitcher IP/GS/GP).
    compute_ab_benchmarks(hitters)
    compute_pitcher_benchmarks(pitchers)
    compute_league_averages(hitters, pitchers)   # league-avg reference points → _LG

    # Map Baseball Ref recent rows to add fields pitcher_score expects
    rec_p_fp = {}
    for name, r in rec_p.items():
        ip = _n(r.get("IP")); k = _n(r.get("K")); g = _n(r.get("G"))
        rec_p_fp[name] = {**r, "K/IP": round(k / ip, 3) if ip > 0 else 0,
                          "IP_per_G": round(ip / g, 2) if g > 0 else 0}

    # Best-available recent row per player: 30d > 15d > 7d > Baseball Ref (last dict wins in merge)
    best_recent_p = {**rec_p_fp, **p7, **p15, **p30}
    best_recent_h = {**rec_h,    **h7, **h15, **h30}

    # Players claimed today may not yet have FantasyTeam set in the ESPN roster API.
    # Use today's transactions as a second source of truth, but be precise:
    # only exclude a player if their MOST RECENT transaction today is FA ADDED
    # (handles add-then-drop-same-day correctly).
    today_str = datetime.now().strftime("%Y-%m-%d")
    todays_txns = [
        t for t in snap.get("transactions", [])
        if t.get("TransactionDate", "").startswith(today_str)
    ]
    latest_txn = {}
    for t in sorted(todays_txns, key=lambda t: t.get("TransactionDate", "")):
        latest_txn[t["PlayerName"]] = t["TransactionType"]
    claimed = {name for name, txn_type in latest_txn.items() if txn_type == "FA ADDED"}

    fa_sp     = fa_starters(pitchers, claimed, idx_recent=best_recent_p)
    fa_rp     = fa_relievers(pitchers, claimed)
    fa_hit    = fa_hitters(hitters, claimed, idx_recent=best_recent_h)
    luck      = luck_standings(roto, standings)
    team_logos = {" ".join(s["team_name"].split()): s.get("logo_url", "") for s in standings}
    cats, n   = category_ranks(roto, my_team)
    current_week_num = matchup.get("week") or max((int(r.get("Week", 0)) for r in roto), default=0)
    weekly_avgs  = compute_weekly_avgs(roto, current_week_num)
    days_elapsed = datetime.now().weekday()   # Mon=0 (no stats yet) … Sun=6
    _today = datetime.now().date()
    week_end_str = (_today + timedelta(days=6 - _today.weekday())).strftime("%Y-%m-%d")
    is_sunday  = _today.weekday() == 6
    is_monday  = _today.weekday() == 0
    next_week_end_str = (_today + timedelta(days=13 - _today.weekday())).strftime("%Y-%m-%d")
    week_roto = [r for r in roto if int(r.get("Week", 0)) == current_week_num]
    week_cats, week_n = category_ranks(week_roto, my_team)

    # Compute pitcher counting stat projections from actual remaining starts (K, QS, W)
    _opp_key = " ".join(matchup.get("opp_team", "").split()) if matchup else ""
    def _remaining_starters(team_key):
        return [r for r in pitchers
                if int(r.get("Dataset", 0) or 0) == YEAR
                and " ".join((r.get("FantasyTeam") or "").split()) == team_key
                and r.get("PSP_Date", "") not in ("1999-01-01", "", None)
                and r.get("PSP_Date", "") >= today_str
                and r.get("PSP_Date", "") <= week_end_str
                and _is_sp(r)]
    def _proj_qs(starters):
        return sum((qs_probability(r) or 0) / 100 for r in starters)
    def _proj_k(starters):
        total = 0
        for r in starters:
            gs = _n(r.get("GS")); k = _n(r.get("K")); ip_g = _n(r.get("IP_per_G")); kip = _n(r.get("K/IP") or r.get("KIP"))
            total += (k / gs) if gs > 0 else (ip_g * kip if ip_g > 0 and kip > 0 else 5)
        return total
    def _proj_w(starters):
        total = 0
        for r in starters:
            gs = _n(r.get("GS")); w = _n(r.get("ESPN_W") or r.get("W"))
            total += (w / gs) if gs > 0 else 0.12
        return total
    _my_starters  = _remaining_starters(" ".join(my_team.split()))
    _opp_starters = _remaining_starters(_opp_key)
    pit_proj = {
        "QS": {"my": _proj_qs(_my_starters),  "opp": _proj_qs(_opp_starters)},
        "K":  {"my": _proj_k(_my_starters),   "opp": _proj_k(_opp_starters)},
        "W":  {"my": _proj_w(_my_starters),   "opp": _proj_w(_opp_starters)},
    }

    # Category classification (used by the pickup steering AND the FA "Cats" column).
    # Computed here (before the FA tables) so need_cats is available to them.
    category_classification = classify_categories(
        matchup, weekly_avgs=weekly_avgs, days_elapsed=days_elapsed, remaining_proj=pit_proj
    )
    # need_cats = the categories I'm losing OR that are a tossup — highlighted in FA "Cats".
    _losing_now = {c["cat"] for c in (matchup.get("categories", []) if matchup else []) if c.get("result") == "L"}
    need_cats = _losing_now | {c for c, (res, tier) in category_classification.items() if tier == "tossup"}
    # League percentile pools for the FA "Cats" column (qualified YEAR pools per type).
    _ab_pool_floor = (_AB_BENCH.get(YEAR) or _FULLTIME_AB[YEAR]) * 0.30
    _hit_pool = [r for r in hitters  if int(_n(r.get("Dataset")) or 0) == YEAR and _n(r.get("AB")) >= _ab_pool_floor]
    _rp_pool  = [r for r in pitchers if int(_n(r.get("Dataset")) or 0) == YEAR and not _is_sp(r)]
    hit_pctile = build_cat_percentiles(_hit_pool, _FA_HIT_CATS)
    rp_pctile  = build_cat_percentiles(_rp_pool,  _FA_RP_CATS)
    # Pseudo roto score from the ranks shown in the week table (n - rank + 1 per
    # category). Matches the Season section's method so the two subtitles are on
    # the same scale, and matches the ranks rendered directly below it. (Raw
    # Roto_Score diverges here because ESPN splits points on ties.)
    my_week_roto_pts = sum(week_n - rank + 1 for rank in week_cats.values() if rank is not None)
    my_season_pseudo_roto = sum(n - rank + 1 for rank in cats.values() if rank is not None)
    alerts    = roster_alerts(pitchers, hitters, my_team)
    starts    = my_upcoming_starts(pitchers, my_team)
    pos_data  = positional_breakdown(pitchers, hitters, my_team, best_recent_p, best_recent_h)

    my_row = next((r for r in luck if " ".join((r.get("team") or "").split()) == " ".join(my_team.split())), {})
    today  = datetime.now().strftime("%A, %B %d, %Y")
    _digest_label = "Weekly Lookahead" if is_sunday else "Daily Fantasy Digest"

    # ── Derived KPI values ─────────────────────────────────────────────────────
    my_logo_url = team_logos.get(" ".join(my_team.split()), "")
    my_logo_html = fantasy_logo(my_logo_url, size=36, team_name=my_team)

    # Build per-week roto scores and rank-based results (used by sparkline + KPI stats)
    my_key = " ".join(my_team.split())
    week_scores = {}
    for row in roto:
        t = " ".join((row.get("Team") or "").split())
        wk = int(row.get("Week", 0))
        if wk not in week_scores:
            week_scores[wk] = {}
        week_scores[wk][t] = float(row.get("Roto_Score") or 0)
    wk_ranks = []; wk_pts = []
    roto_week_results = {}
    for wk in sorted(week_scores):
        if wk >= current_week_num:   # skip current (partial) week
            continue
        scores = week_scores[wk]
        if my_key not in scores:
            continue
        ranked = sorted(scores.items(), key=lambda x: -x[1])
        wk_res = {}
        for i, (t, _) in enumerate(ranked):
            wk_res[t] = 'W' if i == 0 else 'L'
        roto_week_results[wk] = wk_res
        my_rank = next((i + 1 for i, (t, _) in enumerate(ranked) if t == my_key), None)
        if my_rank:
            wk_ranks.append(my_rank)
            wk_pts.append(scores[my_key])

    sparkline, peak_label = make_sparkline(roto, my_team, current_week_num, weekly_results=roto_week_results)
    spark_trend = ""
    trend_scores = []
    for row in roto:
        if " ".join((row.get("Team") or "").split()) == my_key and int(row.get("Week", 0)) < current_week_num:
            trend_scores.append((int(row.get("Week", 0)), float(row.get("Roto_Score") or 0)))
    trend_scores.sort()
    if len(trend_scores) >= 4:
        recent_avg = sum(s for _, s in trend_scores[-3:]) / 3
        early_avg  = sum(s for _, s in trend_scores[:3])  / 3
        spark_trend = (
            f'&nbsp;<span style="color:{GREEN};font-size:10px;">&#9650;</span>'
            if recent_avg > early_avg else
            f'&nbsp;<span style="color:{RED};font-size:10px;">&#9660;</span>'
        )

    # Hot/cold counts across my ENTIRE roster — hitters (7-day OPS) + pitchers (15-day ERA),
    # matching the thresholds in build_hot_cold_section / build_pitcher_hot_cold_section.
    n_hot = n_cold = 0
    _my_key = " ".join(my_team.split())
    for r in hitters:
        if (" ".join((r.get("FantasyTeam") or "").split()) == _my_key
                and int(r.get("Dataset", 0)) == YEAR
                and float(r.get("OPS") or 0) > 0):
            s_ops = float(r.get("OPS") or 0)
            rh = rec_h.get(r.get("PlayerName", ""), {})
            r_ops = float(rh.get("OPS") or 0) if rh else 0
            if s_ops > 0 and r_ops > 0:
                d = r_ops - s_ops
                if d >= 0.015:   n_hot  += 1
                elif d <= -0.015: n_cold += 1
    # Pitchers: 15-day ERA vs season ERA (lower recent = hot), require >=3 recent IP
    _p15 = {r["PlayerName"]: r for r in pitchers if int(r.get("Dataset", 0) or 0) == 15}
    for r in pitchers:
        if (" ".join((r.get("FantasyTeam") or "").split()) == _my_key
                and int(r.get("Dataset", 0) or 0) == YEAR
                and _n(r.get("ERA")) > 0):
            s_era = _n(r.get("ERA"))
            rp    = _p15.get(r.get("PlayerName", "")) or rec_p.get(r.get("PlayerName", ""), {})
            r_era = _n(rp.get("ERA")) if rp else 0
            r_ip  = _n(rp.get("IP"))  if rp else 0
            if s_era > 0 and r_era > 0 and r_ip >= 3:
                d = s_era - r_era
                if d >= 0.40:    n_hot  += 1
                elif d <= -0.40: n_cold += 1
    hc_str = (
        f'<span style="color:{GREEN};">&#128293;&nbsp;{n_hot}</span>'
        f'<span style="color:{MUTED};margin:0 4px;">·</span>'
        f'<span style="color:{ACCENT};">&#10052;&nbsp;{n_cold}</span>'
    )

    # Category W-L this week
    cat_wl = f'{matchup.get("wins","—")}-{matchup.get("losses","—")}-{matchup.get("ties",0)}' if matchup else "—"
    cat_wl_color = GREEN if matchup and matchup.get("wins", 0) > matchup.get("losses", 0) else (RED if matchup and matchup.get("losses", 0) > matchup.get("wins", 0) else TEXT)
    _cw, _cl, _ct = (matchup.get("wins", 0), matchup.get("losses", 0), matchup.get("ties", 0)) if matchup else (0, 0, 0)
    _ctotal = _cw + _cl + _ct
    cat_win_pct = f"{(_cw + 0.5 * _ct) / _ctotal:.3f}" if _ctotal else "—"

    # Luck
    luck_val = my_row.get("luck", 0)
    luck_str = f"+{luck_val}" if luck_val > 0 else str(luck_val)
    luck_color = GREEN if luck_val > 2 else (RED if luck_val < -2 else MUTED)

    # ── Header ─────────────────────────────────────────────────────────────────
    _data_fresh = (refreshed == today_str)
    # The clock shows the actual fetch TIME (ET), not just the date — ESPN's live category
    # standings keep settling for hours after a fetch, so "data current" (date matches today)
    # can still be several hours behind ESPN. The time makes that lag legible.
    _clock_suffix = f" at {refreshed_clock}" if refreshed_clock else ""
    if _data_fresh:
        _data_badge = (
            f'<span style="color:{MUTED};font-size:7px;margin-left:10px;vertical-align:middle;">'
            f'&#10003;&thinsp;data as of today{_clock_suffix}</span>'
        )
    else:
        try:
            _ref_dt = datetime.strptime(refreshed, "%Y-%m-%d")
            _ref_label = f"{_ref_dt.strftime('%b')} {_ref_dt.day}"
        except Exception:
            _ref_label = refreshed
        _data_badge = (
            f'<span style="color:{YELLOW};font-size:7px;font-weight:600;margin-left:10px;vertical-align:middle;">'
            f'&#9888;&thinsp;data from {_ref_label}{_clock_suffix} &mdash; run a refresh for today\'s matchup</span>'
        )

    # Jump-to nav lives in the header's top-right (a two-column table keeps it email-safe;
    # on mobile the cells stack via the .hdr-main / .hdr-nav responsive rules).
    nav_html = nav_bar()
    header = f"""
<div style="background:linear-gradient(135deg,#0b1a38 0%,#0f172a 100%);padding:22px 28px;border-bottom:2px solid {BORDER};">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="width:100%;"><tr>
    <td class="hdr-main" valign="top" style="vertical-align:top;">
      <div style="color:{MUTED};font-size:10px;text-transform:uppercase;letter-spacing:1px;">{today}{_data_badge}</div>
      <div style="margin-top:6px;vertical-align:middle;">{my_logo_html}<span style="color:{TEXT};font-size:24px;font-weight:900;letter-spacing:-1px;vertical-align:middle;">{my_team}</span></div>
      <div style="color:#4b7bc4;font-size:11px;letter-spacing:.8px;margin-top:4px;text-transform:uppercase;">{_digest_label}</div>
    </td>
    <td class="hdr-nav" valign="top" align="right" style="vertical-align:top;text-align:right;padding-left:12px;">{nav_html}</td>
  </tr></table>
</div>"""

    # ── KPI row (two lines) ────────────────────────────────────────────────────
    # Record: category W-L-T from standings
    wl = f"{my_row.get('wins','—')}-{my_row.get('losses','—')}-{my_row.get('ties',0)}"
    _w, _l, _t = my_row.get('wins', 0), my_row.get('losses', 0), my_row.get('ties', 0)
    _total = _w + _l + _t
    win_pct = f"{(_w + 0.5 * _t) / _total:.3f}" if _total else "—"
    wl_val = wl + f'<div style="color:{MUTED};font-size:9px;margin-top:3px;">{win_pct}</div>'

    avg_rank = f"{sum(wk_ranks)/len(wk_ranks):.1f}" if wk_ranks else "—"
    avg_pts  = f"{sum(wk_pts)/len(wk_pts):.0f}"   if wk_pts  else "—"
    roto_rank_sub = (
        f'<div style="color:{MUTED};font-size:9px;margin-top:3px;">'
        f'avg rank #{avg_rank} &nbsp;·&nbsp; {avg_pts} pts</div>'
    )

    # Roto W-L-T per week average (category record from standings ÷ completed weeks)
    roto_w = my_row.get('wins', 0); roto_l = my_row.get('losses', 0); roto_t = my_row.get('ties', 0)
    completed_weeks = len(wk_ranks)
    if completed_weeks:
        matchup_sub = (
            f'<div style="color:{MUTED};font-size:9px;margin-top:3px;">'
            f'{roto_w/completed_weeks:.1f}W · {roto_l/completed_weeks:.1f}L · {roto_t/completed_weeks:.1f}T /wk</div>'
        )
    else:
        matchup_sub = ''

    def _dot(r, fill, stroke=None, sw=1.5):
        sf = f' stroke="{stroke}" stroke-width="{sw}"' if stroke else ''
        return (f'<svg width="7" height="7" style="vertical-align:middle;" xmlns="http://www.w3.org/2000/svg">'
                f'<circle cx="3.5" cy="3.5" r="{r}" fill="{fill}"{sf}/></svg>')

    _no1_weeks = sorted(wk for wk, res in roto_week_results.items() if res.get(my_key) == 'W')
    _no1_wk_str = (
        f'<span style="color:{YELLOW};">: {", ".join(str(w) for w in _no1_weeks)}</span>'
        if _no1_weeks else ''
    )
    spark_footer = (
        f'<div style="font-size:9px;color:{MUTED};margin-top:2px;white-space:nowrap;">'
        f'{_dot(3.5, GREEN)}&thinsp;{peak_label.replace("<div","<span").replace("</div>","</span>")}'
        f'&ensp;|&ensp;'
        f'<span style="color:{YELLOW};">&#9733;</span>&thinsp;#1 roto wk{_no1_wk_str}'
        f'</div>'
    )

    spark_cell_val = f'{sparkline}{spark_trend}{spark_footer}'
    kpi = f"""
<table style="width:100%;border-collapse:collapse;background:{SURFACE};border-bottom:2px solid {BORDER};">
<tr>
  {kpi_cell("Record", wl_val)}
  {kpi_cell("Current Matchup", f'<span style="color:{cat_wl_color};">{cat_wl}</span><div style="color:{MUTED};font-size:9px;margin-top:3px;">{cat_win_pct}</div>')}
  {kpi_cell("Roster", hc_str)}
  {kpi_cell("Starts Next Week" if is_sunday else "Starts This Week", sum(1 for s in starts if s.get("PSP_Date","") > week_end_str) if is_sunday else sum(1 for s in starts if s.get("PSP_Date","") <= week_end_str))}
</tr>
<tr style="border-top:1px solid {BORDER};">
  {kpi_cell_sm("Roto Trend", spark_cell_val, font_size="inherit", font_weight="normal")}
  {kpi_cell_sm("Standing", f'#{my_row.get("standing","—")}{matchup_sub}')}
  {kpi_cell_sm("Roto Rank", f'#{my_row.get("roto_rank","—")}{roto_rank_sub}')}
  {kpi_cell_sm("Luck", luck_str, color=luck_color)}
</tr>
</table>"""

    # ── Alerts ─────────────────────────────────────────────────────────────────
    if alerts:
        inj_notes = fetch_injury_notes()
        items_html = []
        for a in alerts:
            status_color = RED if (a["status"] in _DL_STATUSES or a["status"].startswith("IL")) else YELLOW
            note = inj_notes.get(a["name"].lower(), {})
            meta_parts = []
            bp  = note.get("body_part", "")
            det = note.get("detail", "")
            if bp:
                meta_parts.append(f"{bp}{' — ' + det if det else ''}")
            rd = note.get("return_date", "")
            if rd:
                try:
                    dt = datetime.strptime(rd, "%Y-%m-%d")
                    meta_parts.append(f'exp. return <span style="color:{TEXT};">{dt.strftime("%b")} {dt.day}</span>')
                except Exception:
                    pass
            meta_html = (
                f'<span style="color:{MUTED};font-size:10px;margin-left:8px;">{"&thinsp;·&thinsp;".join(meta_parts)}</span>'
                if meta_parts else ""
            )
            items_html.append(
                f'<div style="padding:5px 0;border-bottom:1px solid {BORDER};font-size:12px;">'
                f'<span style="color:{YELLOW};">&#9888;</span> '
                f'<strong style="color:{TEXT};">{a["name"]}</strong>'
                f' <span style="color:{status_color};font-weight:600;">{_fmt_status(a["status"])}</span>'
                f'{meta_html}</div>'
            )
        alert_section = (
            f'<div style="background:{SURFACE};border:1px solid {BORDER};border-left:3px solid {YELLOW};'
            f'border-radius:6px;padding:12px 14px;margin-bottom:20px;">'
            f'<div style="color:{YELLOW};font-size:10px;font-weight:700;text-transform:uppercase;'
            f'letter-spacing:.7px;margin-bottom:6px;">&#9888; Roster Alerts</div>'
            f'{"".join(items_html)}</div>'
        )
    else:
        alert_section = ""

    # ── My upcoming starts ─────────────────────────────────────────────────────
    if starts:
        by_date = {}
        for r in starts:
            by_date.setdefault(r.get("PSP_Date", ""), []).append(r)

        # Compacted cell styles for this 9-column table so it fits an iPad width
        # without horizontal scroll (tighter horizontal padding + 12px body font).
        # Scoped locally — the shared TH_S/TDC/TD_S constants are unchanged.
        _th  = TH_S.replace("padding:8px 10px", "padding:8px 6px")
        _tdc = TDC.replace("padding:7px 10px", "padding:7px 6px").replace("font-size:13px", "font-size:12px")
        _tds = TD_S.replace("padding:7px 10px", "padding:7px 6px").replace("font-size:13px", "font-size:12px")

        _top3_kpct_starts = set(sorted((_n(r.get("Kpct_P")) for r in starts), reverse=True)[:3])
        rows = ""
        row_idx = 0
        for date_str in sorted(by_date.keys()):
            day_pitchers = by_date[date_str]
            try:
                day_label = datetime.strptime(date_str, "%Y-%m-%d").strftime("%a %b %d")
            except Exception:
                day_label = date_str[5:]
            count = len(day_pitchers)
            next_wk_badge = (
                f'<span style="color:{MUTED};font-size:9px;font-weight:700;'
                f'background:rgba(100,116,139,0.15);border:1px solid rgba(100,116,139,0.3);'
                f'border-radius:3px;padding:1px 5px;margin-left:8px;vertical-align:middle;">NEXT WK</span>'
                if date_str > week_end_str else ""
            )
            rows += (
                f'<tr style="background:{SURFACE};">'
                f'<td colspan="8" style="padding:5px 10px;'
                f'border-top:1px solid {BORDER};border-bottom:1px solid {BORDER};">'
                f'<span style="color:{ACCENT};font-size:11px;font-weight:700;'
                f'text-transform:uppercase;letter-spacing:.5px;">{day_label}</span>'
                f'<span style="color:{MUTED};font-size:10px;margin-left:8px;">'
                f'{count} start{"s" if count != 1 else ""}</span>'
                f'{next_wk_badge}'
                f'</td></tr>'
            )
            for r in day_pitchers:
                bg = f"background:{SURFACE2};" if row_idx % 2 else ""
                row_idx += 1
                ha   = r.get("PSP_HomeVAway", "")
                name = r.get("PlayerName", "")
                p15r = p15.get(name) or rec_p.get(name, {})
                qsp = qs_probability(r)
                qsp_color = GREEN if qsp and qsp >= 60 else (TEXT if qsp and qsp >= 40 else MUTED)
                qsp_str = f'<span style="color:{qsp_color};font-weight:700;">{qsp}%</span>' if qsp else "—"
                _kpct_s = _n(r.get("Kpct_P"))
                _kpct_s_top = _kpct_s > 0 and _kpct_s in _top3_kpct_starts
                kpct_s_cell = (
                    f'<span style="color:{YELLOW};font-weight:700;">{_kpct_s*100:.1f}%</span>'
                    if _kpct_s_top and _kpct_s > 0
                    else (f"{_kpct_s*100:.1f}%" if _kpct_s > 0 else f'<span style="color:{MUTED}">—</span>')
                )
                # Annotate the projected line (see FA-SP note) so the same pitcher shows
                # identical QS/5K+ badges here and in FA Starting Pitchers, never contradicting
                # the Proj. Line. QS = projected quality start (6+ IP & ≤3 ER); 5K+ = 5+ proj K.
                _pv_s = _proj_line_vals(r)
                _pjs_ip, _pjs_er, _pjs_k = _pv_s if _pv_s else (0, 0, 0)
                qs_fires_s = _proj_is_qs(_pjs_ip, _pjs_er)
                k_fires_s  = _pjs_k >= 5
                start_badges = []
                if _starts_this_week(r, today_str, week_end_str) >= 2:
                    start_badges.append(two_start_badge())
                if qs_fires_s:
                    start_badges.append(
                        f'<span style="font-size:9px;font-weight:700;color:{GREEN};'
                        f'background:rgba(34,197,94,0.12);border:1px solid rgba(34,197,94,0.35);'
                        f'border-radius:3px;padding:1px 5px;margin-left:5px;vertical-align:middle;">QS</span>'
                    )
                if k_fires_s:
                    start_badges.append(
                        f'<span style="font-size:9px;font-weight:700;color:{YELLOW};'
                        f'background:rgba(245,158,11,0.12);border:1px solid rgba(245,158,11,0.35);'
                        f'border-radius:3px;padding:1px 5px;margin-left:5px;vertical-align:middle;">5K+</span>'
                    )
                start_badge = "".join(start_badges)
                proj_line_s = _proj_line_html(r)
                _cell, _bdrow = score_reveal(
                    _score_p(r, best_recent_p), _pitcher_score_breakdown(r, best_recent_p),
                    _bd_uid("mus", name), 8)
                rows += (
                    f'<tr style="{bg}">'
                    f'<td style="{_tds}font-weight:600;">{team_logo(r.get("Team"))}{name}{inj_tag(r)}{start_badge}</td>'
                    f'<td style="{_tdc}">{proj_line_s}</td>'
                    f'<td style="{_tdc}">{opp_logo(ha)}{ha}'
                    f'{"&nbsp;<span style=\"color:#888;font-size:11px\">(proj.)</span>" if r.get("PSP_Projected") else ""}'
                    f'{_opp_ops_sub(r)}</td>'
                    f'<td style="{_tdc}">{qsp_str}</td>'
                    f'<td style="{_tdc}">{v(r.get("ERA"), 2)}</td>'
                    + hot_cold_cell(r.get("ERA"), p15r.get("ERA"), lower_better=True, dec=2, no_data_title="No 15-day stats — player may not have pitched recently", td_style=_tdc) +
                    f'<td style="{_tdc}">{kpct_s_cell}</td>'
                    f'<td style="{_tdc}">{_cell}</td>'
                    f'</tr>'
                    f'{_bdrow}'
                )

        _this_wk_n = sum(1 for s in starts if s.get("PSP_Date", "") <= week_end_str)
        _next_wk_n = len(starts) - _this_wk_n
        _this_wk_html = (
            f'<span style="color:{RED};">{_this_wk_n} this wk</span>'
            if _this_wk_n == 0 else
            f'{_this_wk_n} this wk'
        )
        _next_wk_html = f', {_next_wk_n} next wk' if _next_wk_n > 0 else ''
        _starts_sub = f'{len(starts)} starts across {len(by_date)} days | {_this_wk_html}{_next_wk_html}'
        starts_section = (
            section_head("My Upcoming Starts", _starts_sub) +
            f'<div style="overflow-x:auto;-webkit-overflow-scrolling:touch;margin-bottom:24px;">'
            f'<table style="width:100%;border-collapse:collapse;font-size:12px;">'
            f'<thead><tr>'
            f'<th style="{_th}">Pitcher</th>'
            f'<th style="{_th}text-align:center;">Proj. Line</th>'
            f'<th style="{_th}text-align:center;">Matchup</th>'
            f'<th style="{_th}text-align:center;">QS%</th>'
            f'<th style="{_th}text-align:center;">ERA</th>'
            f'<th style="{_th}text-align:center;">L15 ERA</th>'
            f'<th style="{_th}text-align:center;">K%</th>'
            f'<th style="{_th}text-align:center;">Score</th>'
            f'</tr></thead><tbody>{rows}</tbody></table>'
            f'</div>'
        )
    else:
        starts_section = ""

    # ── My RP ─────────────────────────────────────────────────────────────────
    # Use best available dataset per player (YEAR preferred; fall back for
    # recently called-up RPs who aren't in FantasyPros' season top-300).
    _rp_candidates = [
        r for r in pitchers
        if " ".join((r.get("FantasyTeam") or "").split()) == " ".join(my_team.split())
        and "RP" in str(r.get("Position", ""))
        and not _is_sp(r)
    ]
    _rp_best = {}
    _dataset_rank = {YEAR: 4, 30: 3, 15: 2, 7: 1}
    for r in _rp_candidates:
        name = r.get("PlayerName", "")
        ds   = int(r.get("Dataset", 0) or 0)
        if _dataset_rank.get(ds, 0) > _dataset_rank.get(int((_rp_best.get(name) or {}).get("Dataset", 0) or 0), 0):
            _rp_best[name] = r
    my_rp = sorted(_rp_best.values(), key=lambda r: -rp_score(r))
    for r in my_rp:
        r["_rp_score"] = rp_score(r)

    if my_rp:
        def _rp_row(r, i, score_key="_rp_score"):
            bg   = f"background:{SURFACE2};" if i % 2 else ""
            era  = _n(r.get("ERA"))
            whip = _n(r.get("WHIP"))
            svhd = _n(r.get("ESPN_SVHD")) or _n(r.get("SVHD"))
            k    = _n(r.get("ESPN_K"))    or _n(r.get("K"))
            w    = _n(r.get("ESPN_W"))    or _n(r.get("W"))
            ds   = int(r.get("Dataset", 0) or 0)
            ds_label = {30: "30d", 15: "15d", 7: "7d"}.get(ds, "")
            no_espn = _n(r.get("ESPN_GP")) <= 0
            ds_badge = (
                f'<span style="color:{MUTED};font-size:9px;font-weight:600;'
                f'background:rgba(100,116,139,0.12);border:1px solid rgba(100,116,139,0.25);'
                f'border-radius:3px;padding:1px 4px;margin-left:5px;vertical-align:middle;">'
                f'{ds_label}</span>'
            ) if ds_label and no_espn else ""
            _cell, _bdrow = score_reveal(
                r[score_key], _pitcher_score_breakdown(r),
                _bd_uid("myrp", r.get("PlayerName", "")), 8)
            return (
                f'<tr style="{bg}">'
                f'<td style="{TD_S}font-weight:600;">{team_logo(r.get("Team"))}{r.get("PlayerName","")}{inj_tag(r)}{ds_badge}</td>'
                f'<td style="{TDC}color:{MUTED};">{r.get("Position","")}</td>'
                f'<td style="{TDC}">{v(svhd, 0)}</td>'
                f'<td style="{TDC}">{v(k, 0)}</td>'
                f'<td style="{TDC}">{v(w, 0)}</td>'
                f'<td style="{TDC}">{f"{era:.2f}" if era > 0 else "—"}</td>'
                f'<td style="{TDC}">{f"{whip:.2f}" if whip > 0 else "—"}</td>'
                f'<td style="{TDC}">{_cell}</td>'
                f'</tr>'
                f'{_bdrow}'
            )

        rp_rows = "".join(_rp_row(r, i) for i, r in enumerate(my_rp))
        my_rp_table = (
            f'<div style="overflow-x:auto;-webkit-overflow-scrolling:touch;margin-bottom:24px;">'
            f'<table style="width:100%;border-collapse:collapse;font-size:13px;">'
            f'<thead><tr>'
            f'<th style="{TH_S}">Reliever</th>'
            f'<th style="{TH_S}text-align:center;">Pos</th>'
            f'<th style="{TH_S}text-align:center;">SV+H</th>'
            f'<th style="{TH_S}text-align:center;">K</th>'
            f'<th style="{TH_S}text-align:center;">W</th>'
            f'<th style="{TH_S}text-align:center;">ERA</th>'
            f'<th style="{TH_S}text-align:center;">WHIP</th>'
            f'<th style="{TH_S}text-align:center;">Score</th>'
            f'</tr></thead><tbody>{rp_rows}</tbody></table>'
            f'</div>'
        )
        my_rp_section = section_head("My Relief Pitchers", "Rostered RP · SV+H/K/W season (ESPN) · ERA/WHIP from best dataset") + my_rp_table
    else:
        my_rp_section = ""

    body_parts = []

    # ── FA: Starting Pitchers ──────────────────────────────────────────────────
    # Count my starts per day so thin days (< 2) can be highlighted
    my_starts_by_day = {}
    for s in starts:
        d = s.get("PSP_Date", "")
        if d and d != "1999-01-01":
            my_starts_by_day[d] = my_starts_by_day.get(d, 0) + 1

    if fa_sp:
        by_date_fa = {}
        for r in fa_sp:
            by_date_fa.setdefault(r.get("PSP_Date", ""), []).append(r)

        # Compacted cell styles (match My Upcoming Starts) so this 9-column table
        # fits an iPad width without horizontal scroll. Scoped locally.
        _th  = TH_S.replace("padding:8px 10px", "padding:8px 6px")
        _tdc = TDC.replace("padding:7px 10px", "padding:7px 6px").replace("font-size:13px", "font-size:12px")
        _tds = TD_S.replace("padding:7px 10px", "padding:7px 6px").replace("font-size:13px", "font-size:12px")

        _top3_kpct_fa = set(sorted((_n(r.get("Kpct_P")) for r in fa_sp), reverse=True)[:3])
        rows = ""
        row_idx = 0
        for date_str in sorted(by_date_fa.keys()):
            day_pitchers = by_date_fa[date_str]
            try:
                day_label = datetime.strptime(date_str, "%Y-%m-%d").strftime("%a %b %d")
            except Exception:
                day_label = date_str[5:]
            count = len(day_pitchers)
            my_count = my_starts_by_day.get(date_str, 0)
            if my_count == 0:
                my_starts_label, badge_color = "0 my starts", RED
            elif my_count == 1:
                my_starts_label, badge_color = "1 my start", YELLOW
            else:
                my_starts_label, badge_color = f"{my_count} my starts", ACCENT
            thin_badge = (
                f'<span style="color:{badge_color};font-size:10px;font-weight:600;'
                f'margin-left:10px;">⚑ {my_starts_label}</span>'
            ) if date_str <= week_end_str else ""
            next_wk_badge = (
                f'<span style="color:{MUTED};font-size:9px;font-weight:700;'
                f'background:rgba(100,116,139,0.15);border:1px solid rgba(100,116,139,0.3);'
                f'border-radius:3px;padding:1px 5px;margin-left:8px;vertical-align:middle;">NEXT WK</span>'
                if date_str > week_end_str else ""
            )
            rows += (
                f'<tr style="background:{SURFACE};">'
                f'<td colspan="8" style="padding:5px 10px;'
                f'border-top:1px solid {BORDER};border-bottom:1px solid {BORDER};">'
                f'<span style="color:{ACCENT};font-size:11px;font-weight:700;'
                f'text-transform:uppercase;letter-spacing:.5px;">{day_label}</span>'
                f'<span style="color:{MUTED};font-size:10px;margin-left:8px;">'
                f'{count} FA start{"s" if count != 1 else ""}</span>'
                f'{thin_badge}'
                f'{next_wk_badge}'
                f'</td></tr>'
            )
            for r in day_pitchers:
                bg = f"background:{SURFACE2};" if row_idx % 2 else ""
                row_idx += 1
                ha = r.get("PSP_HomeVAway", "")
                _pname = r.get("PlayerName", "")
                p15r = p15.get(_pname) or rec_p.get(_pname, {})
                qsp = qs_probability(r)
                qsp_color = GREEN if qsp and qsp >= 60 else (TEXT if qsp and qsp >= 40 else MUTED)
                qsp_str = f'<span style="color:{qsp_color};font-weight:700;">{qsp}%</span>' if qsp else "—"

                # QS / 5K+ badges annotate the projected game line the reader sees, and
                # fire unconditionally (not only on thin rotation days). QS = a projected
                # quality start (6+ IP & ≤3 ER); 5K+ = 5+ projected K. Driving both purely
                # off the Proj. Line means a badge can never contradict it (no "5K+" next
                # to a 4 K line). The QS% column still shows the probability separately.
                _pv = _proj_line_vals(r)
                _pj_ip, _pj_er, _pj_k = _pv if _pv else (0, 0, 0)
                qs_fires = _proj_is_qs(_pj_ip, _pj_er)
                k_fires  = _pj_k >= 5
                pickup_badges = []
                name_border = ""
                if qs_fires:
                    pickup_badges.append(
                        f'<span style="font-size:9px;font-weight:700;color:{GREEN};'
                        f'background:rgba(34,197,94,0.12);border:1px solid rgba(34,197,94,0.35);'
                        f'border-radius:3px;padding:1px 5px;margin-left:5px;vertical-align:middle;">QS</span>'
                    )
                if k_fires:
                    pickup_badges.append(
                        f'<span style="font-size:9px;font-weight:700;color:{YELLOW};'
                        f'background:rgba(245,158,11,0.12);border:1px solid rgba(245,158,11,0.35);'
                        f'border-radius:3px;padding:1px 5px;margin-left:5px;vertical-align:middle;">5K+</span>'
                    )
                if qs_fires and k_fires:
                    # Half green (top) / half yellow (bottom)
                    name_border = (
                        f"background-image:linear-gradient(to bottom,{GREEN} 50%,{YELLOW} 50%);"
                        f"background-size:3px 100%;background-repeat:no-repeat;background-position:0 0;"
                    )
                elif qs_fires:
                    name_border = f"border-left:3px solid {GREEN};"
                elif k_fires:
                    name_border = f"border-left:3px solid {YELLOW};"
                pickup_badge = "".join(pickup_badges)
                # Two-start flag always shows — a 2-start FA is a top streaming target
                two_start_html = two_start_badge() if _starts_this_week(r, today_str, week_end_str) >= 2 else ""

                _kpct_val = _n(r.get("Kpct_P"))
                _kpct_top = _kpct_val > 0 and _kpct_val in _top3_kpct_fa
                kpct_cell = (
                    f'<span style="color:{YELLOW};font-weight:700;">{_kpct_val*100:.1f}%</span>'
                    if _kpct_top and _kpct_val > 0
                    else (f"{_kpct_val*100:.1f}%" if _kpct_val > 0 else f'<span style="color:{MUTED}">—</span>')
                )
                proj_line_str = _proj_line_html(r)
                _cell, _bdrow = score_reveal(
                    r["_score"], _pitcher_score_breakdown(r, best_recent_p),
                    _bd_uid("fasp", r.get("PlayerName", "")), 8)
                rows += (
                    f'<tr style="{bg}">'
                    f'<td style="{name_border}{_tds}font-weight:600;">{team_logo(r.get("Team"))}{r.get("PlayerName","")}{inj_tag(r)}{two_start_html}{pickup_badge}</td>'
                    f'<td style="{_tdc}">{proj_line_str}</td>'
                    f'<td style="{_tdc}">{opp_logo(ha)}{ha}'
                    f'{"&nbsp;<span style=\"color:#888;font-size:11px\">(proj.)</span>" if r.get("PSP_Projected") else ""}'
                    f'{_opp_ops_sub(r)}</td>'
                    f'<td style="{_tdc}">{qsp_str}</td>'
                    f'<td style="{_tdc}">{v(r.get("ERA"), 2)}</td>'
                    + hot_cold_cell(r.get("ERA"), p15r.get("ERA"), lower_better=True, dec=2, no_data_title="No 15-day stats — player may not have pitched recently", td_style=_tdc) +
                    f'<td style="{_tdc}">{kpct_cell}</td>'
                    f'<td style="{_tdc}">{_cell}</td>'
                    f'</tr>'
                    f'{_bdrow}'
                )
        table = (
            f'<div style="overflow-x:auto;-webkit-overflow-scrolling:touch;margin-bottom:24px;">'
            f'<table style="width:100%;border-collapse:collapse;font-size:12px;">'
            f'<thead><tr>'
            f'<th style="{_th}">Pitcher</th>'
            f'<th style="{_th}text-align:center;">Proj. Line</th>'
            f'<th style="{_th}text-align:center;">Matchup</th>'
            f'<th style="{_th}text-align:center;">QS%</th>'
            f'<th style="{_th}text-align:center;">ERA</th>'
            f'<th style="{_th}text-align:center;">L15 ERA</th>'
            f'<th style="{_th}text-align:center;">K%</th>'
            f'<th style="{_th}text-align:center;">Score</th>'
            f'</tr></thead><tbody>{rows}</tbody></table>'
            f'</div>'
        )
    else:
        table = f'<p style="color:{MUTED};font-style:italic;margin-bottom:24px;">No FA starters with confirmed upcoming starts.</p>'

    fa_sp_section = section_head("FA Pickup — Starting Pitchers", "Free agents with confirmed upcoming starts · sorted by SP score") + table

    # ── FA: Relief Pitchers ────────────────────────────────────────────────────
    if fa_rp:
        def _fa_rp_row(r, i):
            bg   = f"background:{SURFACE2};" if i % 2 else ""
            era  = _n(r.get("ERA"))
            whip = _n(r.get("WHIP"))
            svhd = _n(r.get("ESPN_SVHD")) or _n(r.get("SVHD"))
            k    = _n(r.get("ESPN_K"))    or _n(r.get("K"))
            w    = _n(r.get("ESPN_W"))    or _n(r.get("W"))
            ds   = int(r.get("Dataset", 0) or 0)
            ds_label = {30: "30d", 15: "15d", 7: "7d"}.get(ds, "")
            no_espn = _n(r.get("ESPN_GP")) <= 0
            ds_badge = (
                f'<span style="color:{MUTED};font-size:9px;font-weight:600;'
                f'background:rgba(100,116,139,0.12);border:1px solid rgba(100,116,139,0.25);'
                f'border-radius:3px;padding:1px 4px;margin-left:5px;vertical-align:middle;">'
                f'{ds_label}</span>'
            ) if ds_label and no_espn else ""
            _cell, _bdrow = score_reveal(
                r["_rp_score"], _pitcher_score_breakdown(r),
                _bd_uid("farp", r.get("PlayerName", "")), 9)
            return (
                f'<tr style="{bg}">'
                f'<td style="{TD_S}font-weight:600;">{team_logo(r.get("Team"))}{r.get("PlayerName","")}{inj_tag(r)}{ds_badge}</td>'
                f'<td style="{TDC}color:{MUTED};">{r.get("Position","")}</td>'
                f'<td style="{TDC}">{v(svhd, 0)}</td>'
                f'<td style="{TDC}">{v(k, 0)}</td>'
                f'<td style="{TDC}">{v(w, 0)}</td>'
                f'<td style="{TDC}">{f"{era:.2f}" if era > 0 else "—"}</td>'
                f'<td style="{TDC}">{f"{whip:.2f}" if whip > 0 else "—"}</td>'
                f'{_cats_cell(r, rp_pctile, _FA_RP_CATS, need_cats)}'
                f'<td style="{TDC}">{_cell}</td>'
                f'</tr>'
                f'{_bdrow}'
            )
        rp_table = (
            f'<div style="overflow-x:auto;-webkit-overflow-scrolling:touch;margin-bottom:24px;">'
            f'<table style="width:100%;border-collapse:collapse;font-size:13px;">'
            f'<thead><tr>'
            f'<th style="{TH_S}">Reliever</th>'
            f'<th style="{TH_S}text-align:center;">Pos</th>'
            f'<th style="{TH_S}text-align:center;">SV+H</th>'
            f'<th style="{TH_S}text-align:center;">K</th>'
            f'<th style="{TH_S}text-align:center;">W</th>'
            f'<th style="{TH_S}text-align:center;">ERA</th>'
            f'<th style="{TH_S}text-align:center;">WHIP</th>'
            f'<th style="{TH_S}text-align:center;">Cats</th>'
            f'<th style="{TH_S}text-align:center;">Score</th>'
            f'</tr></thead><tbody>{"".join(_fa_rp_row(r,i) for i,r in enumerate(fa_rp))}</tbody></table>'
            f'</div>'
        )
    else:
        rp_table = f'<p style="color:{MUTED};font-style:italic;margin-bottom:24px;">No FA relievers found.</p>'

    fa_rp_section = section_head("FA Pickup — Relief Pitchers", "Top 3 available RP · ranked by SV+H, K, W, ERA, WHIP · Cats = categories he'd boost (your contested ones highlighted)") + rp_table

    # Save-Role Watch: emerging FA closers to add + your RP whose save role is slipping
    _sr_emerging, _sr_fading = save_role_watch(pitchers, my_team, claimed)
    if _sr_emerging or _sr_fading:
        _sr_lines = []
        for e in _sr_emerging:
            _sr_lines.append(
                f'<div style="margin:3px 0;">'
                f'<span style="color:{GREEN};font-weight:700;">▲ Emerging closer:</span> '
                f'{team_logo(e["team"])}<span style="color:{TEXT};font-weight:600;">{e["name"]}</span> '
                f'<span style="color:{MUTED};">— {int(e["recent"])} SV in last 15d '
                f'(season {int(e["season"])} SV+H) · available to add</span>'
                f'</div>'
            )
        for f in _sr_fading:
            _sr_lines.append(
                f'<div style="margin:3px 0;">'
                f'<span style="color:{YELLOW};font-weight:700;">▼ Save role slipping:</span> '
                f'{team_logo(f["team"])}<span style="color:{TEXT};font-weight:600;">{f["name"]}</span> '
                f'<span style="color:{MUTED};">— 0 SV in last 15d despite pitching '
                f'(season {int(f["season"])} SV+H) · role may be lost</span>'
                f'</div>'
            )
        fa_rp_section += (
            f'<div style="background:{SURFACE2};border:1px solid {BORDER};border-radius:8px;'
            f'padding:10px 14px;margin:-8px 0 24px;font-size:12px;">'
            f'<div style="color:{ACCENT};font-weight:700;font-size:11px;text-transform:uppercase;'
            f'letter-spacing:.5px;margin-bottom:5px;">Save-Role Watch</div>'
            f'{"".join(_sr_lines)}'
            f'</div>'
        )

    # ── FA: Hitters ────────────────────────────────────────────────────────────
    if fa_hit:
        rows = ""
        for i, r in enumerate(fa_hit):
            bg = f"background:{SURFACE2};" if i % 2 else ""
            rh = rec_h.get(r.get("PlayerName", ""), {})
            _cell, _bdrow = score_reveal(
                r["_score"], _hitter_score_breakdown(r, best_recent_h),
                _bd_uid("fahit", r.get("PlayerName", "")), 11)
            rows += (
                f'<tr style="{bg}">'
                f'<td style="{TD_S}font-weight:600;">{team_logo(r.get("Team"))}{r.get("PlayerName","")}{inj_tag(r)}</td>'
                f'<td style="{TDC}color:{MUTED};">{r.get("Position","")}</td>'
                f'<td style="{TDC}">{v(r.get("R"), 0)}</td>'
                f'<td style="{TDC}">{v(r.get("HR"), 0)}</td>'
                f'<td style="{TDC}">{v(r.get("RBI"), 0)}</td>'
                f'<td style="{TDC}">{v(r.get("SB"), 0)}</td>'
                f'<td style="{TDC}">{v(r.get("OPS"), 3)}</td>'
                + hot_cold_cell(r.get("OPS"), rh.get("OPS"), dec=3, no_data_title="No 7-day stats — player may not have played recently") +
                f'<td style="{TDC}">{_hrp_cell(r)}</td>'
                f'{_cats_cell(r, hit_pctile, _FA_HIT_CATS, need_cats)}'
                f'<td style="{TDC}">{_cell}</td>'
                f'</tr>'
                f'{_bdrow}'
            )
        table = (
            f'<table style="width:100%;border-collapse:collapse;margin-bottom:0;font-size:13px;">'
            f'<thead><tr>'
            f'<th style="{TH_S}">Hitter</th>'
            f'<th style="{TH_S}text-align:center;">Pos</th>'
            f'<th style="{TH_S}text-align:center;">R</th>'
            f'<th style="{TH_S}text-align:center;">HR</th>'
            f'<th style="{TH_S}text-align:center;">RBI</th>'
            f'<th style="{TH_S}text-align:center;">SB</th>'
            f'<th style="{TH_S}text-align:center;">OPS</th>'
            f'<th style="{TH_S}text-align:center;">L7 OPS</th>'
            f'<th style="{TH_S}text-align:center;">HR%</th>'
            f'<th style="{TH_S}text-align:center;">Cats</th>'
            f'<th style="{TH_S}text-align:center;">Score</th>'
            f'</tr></thead><tbody>{rows}</tbody></table>'
        )
        table = f'<div style="overflow-x:auto;-webkit-overflow-scrolling:touch;margin-bottom:24px;">{table}</div>'
    else:
        table = f'<p style="color:{MUTED};font-style:italic;margin-bottom:24px;">No FA hitters found.</p>'

    fa_hit_section = section_head("FA Pickup — Hitters", "Top available hitters · HR% = modeled per-game HR probability · Cats = categories he'd boost (your contested ones highlighted) · sorted by composite score") + table

    # ── Category Rankings ──────────────────────────────────────────────────────
    CAT_LABELS = [
        ("R","R"), ("HR","HR"), ("RBI","RBI"), ("SB","SB"), ("OPS","OPS"), ("B_SO","B/SO"),
        ("K","K"), ("QS","QS"), ("W","W"), ("ERA","ERA"), ("WHIP","WHIP"), ("SVHD","SV+H"),
    ]
    cat_cells = ""
    for key, label in CAT_LABELS:
        rank = cats.get(key)
        if rank is None:
            display, color = "—", MUTED
        elif rank == 1:
            display, color = "#1", GREEN
        elif rank <= 3:
            display, color = f"#{rank}", ACCENT
        elif n and rank > n // 2:
            display, color = f"#{rank}", RED
        else:
            display, color = f"#{rank}", TEXT
        cat_cells += (
            f'<td class="cat-cell" style="text-align:center;padding:10px 4px;border-right:1px solid {BORDER};">'
            f'<div class="cat-label" style="color:{MUTED};font-size:9px;text-transform:uppercase;letter-spacing:.5px;white-space:nowrap;">{label}</div>'
            f'<div class="cat-val" style="color:{color};font-size:17px;font-weight:800;margin-top:3px;">{display}</div>'
            f'</td>'
        )
    cat_section = (
        section_head("My Season Category Rankings", f"Season-to-date · {my_season_pseudo_roto} roto pts · roto points rank per category") +
        f'<table style="width:100%;border-collapse:collapse;background:{SURFACE};border-radius:6px;margin-bottom:24px;overflow:hidden;">'
        f'<tr>{cat_cells}</tr></table>'
    )

    # ── This week's category rankings ──────────────────────────────────────────
    week_cat_cells = ""
    for key, label in CAT_LABELS:
        rank = week_cats.get(key)
        if rank is None:
            display, color = "—", MUTED
        elif rank == 1:
            display, color = "#1", GREEN
        elif rank <= 3:
            display, color = f"#{rank}", ACCENT
        elif week_n and rank > week_n // 2:
            display, color = f"#{rank}", RED
        else:
            display, color = f"#{rank}", TEXT
        week_cat_cells += (
            f'<td class="cat-cell" style="text-align:center;padding:10px 4px;border-right:1px solid {BORDER};">'
            f'<div class="cat-label" style="color:{MUTED};font-size:9px;text-transform:uppercase;letter-spacing:.5px;white-space:nowrap;">{label}</div>'
            f'<div class="cat-val" style="color:{color};font-size:17px;font-weight:800;margin-top:3px;">{display}</div>'
            f'</td>'
        )
    week_cat_section = (
        section_head("Current Matchup", f"Week {current_week_num} · {my_week_roto_pts} roto pts · vs. this week's matchup") +
        f'<table style="width:100%;border-collapse:collapse;background:{SURFACE};border-radius:6px;margin-bottom:24px;overflow:hidden;">'
        f'<tr>{week_cat_cells}</tr></table>'
    )

    # ── Positional Breakdown ───────────────────────────────────────────────────
    pos_rows = ""
    for i, p in enumerate(pos_data):
        bg      = f"background:{SURFACE2};" if i % 2 else ""
        rank    = p["rank"]
        n_teams = p["n_teams"]

        if rank is None or n_teams == 0:
            rank_color, strength = MUTED, "—"
        elif rank <= max(1, n_teams // 3):
            rank_color, strength = GREEN,  "Strong"
        elif rank <= max(1, n_teams * 2 // 3):
            rank_color, strength = YELLOW, "Average"
        else:
            rank_color, strength = RED,    "Need Help"

        rank_str = f"#{rank} of {n_teams}" if rank else "—"

        # Role-aware breakdown for this position's players (SP/RP vs hitter)
        _is_pit_pos = p["ptype"] == "pit"

        def _pb_reveal(pl, tag):
            bd = (_pitcher_score_breakdown(pl, best_recent_p) if _is_pit_pos
                  else _hitter_score_breakdown(pl, best_recent_h))
            return score_reveal(pl["_pscore"], bd, _bd_uid(tag, pl.get("PlayerName", "")), 4)

        _worst_bdrow = _fa_bdrow = ""
        worst = p["worst_player"]
        if worst:
            _worst_badge, _worst_bdrow = _pb_reveal(worst, "posw")
            player_cell = (
                f'{team_logo(worst.get("Team"), 16)}'
                f'<span style="font-weight:600;">{worst["PlayerName"]}</span>'
                f'{inj_tag(worst)}'
                f' {_worst_badge}'
                f'{pos_stat_line(worst, p["pos"])}'
            )
        else:
            player_cell = f'<span style="color:{RED};font-weight:600;">EMPTY</span>'

        top_fa = p["top_fa"][0] if p["top_fa"] else None
        fa_score = top_fa["_pscore"] if top_fa else 0
        worst_score = worst["_pscore"] if worst else 0
        fa_depth   = p.get("fa_depth",   0)
        fa_quality = p.get("fa_quality", 0)
        # Both score types now on shared 0-100 scale; single set of thresholds
        if fa_quality < 50:
            depth_color, depth_label, upgrade_thresh = RED,    "scarce",    5
        elif fa_quality < 60:
            depth_color, depth_label, upgrade_thresh = YELLOW, "moderate",  8
        else:
            depth_color, depth_label, upgrade_thresh = MUTED,  "deep",     12
        depth_html = (
            f'<div style="color:{depth_color};font-size:10px;margin-top:1px;">'
            f'{fa_depth} avail · {depth_label}</div>'
        )
        upgrade = top_fa and fa_score > worst_score + upgrade_thresh
        if top_fa:
            _fa_badge, _fa_bdrow = _pb_reveal(top_fa, "posfa")
            fa_cell = (
                f'{team_logo(top_fa.get("Team"), 16)}'
                f'<span style="{"font-weight:600;" if upgrade else ""}'
                f'color:{GREEN if upgrade else MUTED};">'
                f'{top_fa["PlayerName"]}</span> {_fa_badge}'
                f'{"&nbsp;&#8593;" if upgrade else ""}'
                f'{pos_stat_line(top_fa, p["pos"])}'
                f'{depth_html}'
            )
        else:
            fa_cell = (
                f'<span style="color:{MUTED}">—</span>'
                f'{depth_html}'
            )

        pos_rows += (
            f'<tr style="{bg}">'
            f'<td style="{TDC}font-weight:800;color:{TEXT};font-size:14px;">{p["pos"]}</td>'
            f'<td style="{TD_S}">{player_cell}</td>'
            f'<td style="{TDC}color:{rank_color};font-weight:700;font-size:12px;">'
            f'{strength}<br><span style="color:{MUTED};font-size:10px;">{rank_str}</span></td>'
            f'<td style="{TD_S}font-size:12px;color:{MUTED};">{fa_cell}</td>'
            f'</tr>'
            f'{_worst_bdrow}{_fa_bdrow}'
        )

    pos_section = (
        section_head("Positional Breakdown", "Your depth at each position vs. the rest of the league") +
        f'<div style="overflow-x:auto;-webkit-overflow-scrolling:touch;margin-bottom:24px;">'
        f'<table style="width:100%;border-collapse:collapse;font-size:13px;">'
        f'<thead><tr>'
        f'<th style="{TH_S}text-align:center;">Pos</th>'
        f'<th style="{TH_S}">My Weakest Player</th>'
        f'<th style="{TH_S}text-align:center;">Strength</th>'
        f'<th style="{TH_S}">Best FA Available &nbsp;<span style="color:{GREEN};font-size:9px;">&#8593; = upgrade</span></th>'
        f'</tr></thead><tbody>{pos_rows}</tbody></table>'
        f'</div>'
    )

    # ── League Luck Standings ──────────────────────────────────────────────────
    luck_rows = ""
    for i, row in enumerate(luck):
        bg   = f"background:{SURFACE2};" if i % 2 else ""
        is_me = " ".join(row["team"].split()) == " ".join(my_team.split())
        name_s = f"font-weight:800;color:{ACCENT};" if is_me else "font-weight:500;"
        me_arrow = " &#8592;" if is_me else ""
        logo_html = fantasy_logo(row.get("logo_url", ""), 24, row["team"])
        lv = row["luck"]
        if lv > 2:
            lcolor, lstr = GREEN, f"+{lv}"
        elif lv < -2:
            lcolor, lstr = RED, str(lv)
        else:
            lcolor, lstr = MUTED, str(lv)
        _rw, _rl, _rt = row["wins"], row["losses"], row.get("ties", 0)
        _rtotal = _rw + _rl + _rt
        _rpct = f"{(_rw + 0.5 * _rt) / _rtotal:.3f}" if _rtotal else "—"
        luck_rows += (
            f'<tr style="{bg}">'
            f'<td style="{TDC}color:{MUTED};">{row["standing"]}</td>'
            f'<td style="{TD_S}{name_s}">{logo_html}{row["team"]}{me_arrow}</td>'
            f'<td style="{TDC}">{_rw}-{_rl}-{_rt}</td>'
            f'<td style="{TDC}color:{MUTED};">{_rpct}</td>'
            f'<td style="{TDC}color:{MUTED};">{row["roto_rank"]}</td>'
            f'<td class="hide-mob" style="{TDC}color:{MUTED};">{row["roto_pts"]:.0f}</td>'
            f'<td style="{TDC}color:{lcolor};font-weight:700;">{lstr}</td>'
            f'</tr>'
        )
    luck_section = (
        section_head("League Luck Standings", "Luck = roto rank minus record rank · positive = W-L better than roto suggests") +
        f'<div style="overflow-x:auto;-webkit-overflow-scrolling:touch;margin-bottom:8px;">'
        f'<table style="width:100%;border-collapse:collapse;margin-bottom:0;font-size:13px;">'
        f'<thead><tr>'
        f'<th style="{TH_S}text-align:center;">#</th>'
        f'<th style="{TH_S}">Team</th>'
        f'<th style="{TH_S}text-align:center;">W-L-T</th>'
        f'<th style="{TH_S}text-align:center;">Win%</th>'
        f'<th style="{TH_S}text-align:center;">Roto #</th>'
        f'<th class="hide-mob" style="{TH_S}text-align:center;">Roto Pts</th>'
        f'<th style="{TH_S}text-align:center;">Luck</th>'
        f'</tr></thead><tbody>{luck_rows}</tbody></table>'
        f'</div>'
    )

    # ── Final assembly ─────────────────────────────────────────────────────────
    # (category_classification computed earlier, before the FA tables, so the FA "Cats"
    # column can reuse its need_cats.)

    # Opponent scouting block (placed right after the matchup panel)
    _opp_name  = matchup.get("opp_team", "") if matchup else ""
    _opp_intel = opponent_week_intel(pitchers, hitters, _opp_name, best_recent_h, today_str, week_end_str)
    opp_preview_section = ""
    if _opp_intel and (_opp_intel["n_starters"] or _opp_intel["hot_hitters"]):
        _opp_key   = " ".join(_opp_name.split())
        _logo_html = fantasy_logo(team_logos.get(_opp_key, ""), 20, _opp_name)
        _lines = []
        if _opp_intel["n_starters"]:
            _two = _opp_intel["two_start"]
            _two_html = ""
            if _two:
                _two_html = (
                    ' · <span style="color:' + GREEN + ';font-weight:700;">2-start:</span> '
                    + ", ".join(t.get("PlayerName", "") for t in _two)
                )
            _lines.append(
                f'<div style="margin:3px 0;"><span style="color:{MUTED};">Pitching:</span> '
                f'<span style="color:{TEXT};font-weight:600;">{_opp_intel["n_starts"]} starts</span> '
                f'<span style="color:{MUTED};">from {_opp_intel["n_starters"]} SP this week</span>{_two_html}</div>'
            )
        if _opp_intel["hot_hitters"]:
            _hh = " · ".join(
                f'{r.get("PlayerName","")} <span style="color:{MUTED};">({ops:.3f})</span>'
                for r, ops in _opp_intel["hot_hitters"]
            )
            _lines.append(
                f'<div style="margin:3px 0;"><span style="color:{MUTED};">Hot bats:</span> '
                f'<span style="color:{TEXT};">{_hh}</span></div>'
            )
        # Season category strengths/weaknesses (roto rank per category)
        _opp_ranks, _n_teams = category_ranks(roto, _opp_name)
        if _opp_ranks:
            _sorted = sorted(_opp_ranks.items(), key=lambda kv: kv[1])
            _strong = [f'{_CAT_DISPLAY.get(c, c)} <span style="color:{MUTED};">#{r}</span>' for c, r in _sorted[:3]]
            _weak   = [f'{_CAT_DISPLAY.get(c, c)} <span style="color:{MUTED};">#{r}</span>' for c, r in _sorted[-3:][::-1]]
            _lines.append(
                f'<div style="margin:3px 0;"><span style="color:{GREEN};">Strong:</span> '
                f'<span style="color:{TEXT};">{" · ".join(_strong)}</span></div>'
                f'<div style="margin:3px 0;"><span style="color:{RED};">Weak:</span> '
                f'<span style="color:{TEXT};">{" · ".join(_weak)}</span></div>'
            )
        # Wire activity: how many FA adds this team made in the recent transaction window
        _opp_adds = sum(
            1 for t in snap.get("transactions", [])
            if " ".join((t.get("FantasyTeam") or "").split()) == _opp_key
            and t.get("TransactionType") == "FA ADDED"
        )
        if _opp_adds >= 4:
            _wire = f'<span style="color:{YELLOW};font-weight:700;">very active</span> — {_opp_adds} pickups in recent days; expect streaming'
        elif _opp_adds >= 1:
            _wire = f'{_opp_adds} recent pickup{"s" if _opp_adds != 1 else ""} — moderately active'
        else:
            _wire = 'quiet — mostly letting it ride'
        _lines.append(
            f'<div style="margin:3px 0;"><span style="color:{MUTED};">Wire:</span> '
            f'<span style="color:{TEXT};">{_wire}</span></div>'
        )
        opp_preview_section = (
            section_head("Opponent This Week", f"{_logo_html}Scouting {_opp_name} — starts, hot bats, roto strengths &amp; wire activity")
            + f'<div style="background:{SURFACE2};border:1px solid {BORDER};border-radius:8px;'
              f'padding:10px 14px;margin-bottom:24px;font-size:12px;">{"".join(_lines)}</div>'
        )
    roster_suggestion = _roster_suggestion(
        matchup, pitchers, hitters, fa_sp, fa_rp, fa_hit,
        my_team, best_recent_p, best_recent_h,
        all_matchups, week_end_str, classification=category_classification
    )
    week_overview = build_week_overview(
        matchup, week_cats, week_n, fa_sp, starts, days_elapsed, my_starts_by_day,
        week_end=week_end_str, is_sunday=is_sunday, roster_suggestion=roster_suggestion
    )
    body_parts += [
        build_prev_matchup_recap(prev_matchup, team_logos=team_logos) if is_monday and prev_matchup.get("week") != (matchup or {}).get("week") else "",  # 2a MONDAY RECAP
        week_overview,                                                                    # 2  WEEK INTELLIGENCE
        build_category_pulse(matchup, weekly_avgs=weekly_avgs, days_elapsed=days_elapsed, remaining_proj=pit_proj, is_sunday=is_sunday), # 3
        opp_preview_section,                                                              # 3b OPPONENT SCOUTING (below Category Pulse)
        week_cat_section,                                                                 # 4  (before matchup panel)
        build_matchup_section(matchup, logos=team_logos, my_team=my_team,
                              weekly_avgs=weekly_avgs, days_elapsed=days_elapsed,
                              remaining_proj=pit_proj),                                    # 5
        band_divider("MY ROSTER", anchor="band-myroster"),                                # MY TEAM band header
        alert_section,                                                                    # 1  ALERTS (top of My Roster)
        pos_section,                                                                      # 10 Positional Breakdown (moved to top of My Roster)
        starts_section,                                                                   # 6
        my_rp_section,                                                                    # 7
        build_pitcher_hot_cold_section(pitchers, my_team, rec_p, best_recent_p),         # 8
        build_hot_cold_section(hitters, recent_hitting, my_team, best_recent_h),         # 9
        band_divider("FREE AGENTS", anchor="band-fa"),                                    # ACTION band header
        fa_sp_section,                                                                    # 11
        fa_rp_section,                                                                    # 12
        fa_hit_section,                                                                   # 13
        band_divider("SEASON", anchor="band-season"),                                     # SEASON CONTEXT band header
        cat_section,                                                                      # 14
        luck_section,                                                                     # 15
        band_divider("REFERENCE", anchor="band-glossary"),                                # REFERENCE band header
        build_glossary_section(),                                                         # 16 Glossary & Methodology
    ]
    body = "\n".join(p for p in body_parts if p)

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{my_team} Daily Digest</title>
  <style>
    /* Tap-to-expand Score breakdown (v2): each breakdown <tr> carries inline
       display:none; tapping its badge sets the URL fragment and this :target rule
       reveals the full-width row below the player (!important beats the inline style).
       Renders in the browser-opened attachment; Gmail strips <style> so the rows stay
       hidden there (the score badge itself always shows). */
    tr.scorebd-row:target {{ display:table-row !important; scroll-margin-top:40vh; }}
    a.bdlink {{ outline:none; }}
    @media only screen and (max-width:600px) {{
      .ew {{ width:100% !important; padding:8px !important; }}
      table th, table td {{ padding:5px 4px !important; }}
      .kpi-cell {{ width:50% !important; display:inline-block; box-sizing:border-box; }}
      .kpi-cell:nth-child(1), .kpi-cell:nth-child(2) {{ border-bottom:1px solid {BORDER} !important; }}
      .cat-cell {{ font-size:14px !important; padding:6px 2px !important; }}
      .cat-cell .cat-label {{ font-size:8px !important; }}
      .cat-cell .cat-val {{ font-size:14px !important; }}
      .hide-mob {{ display:none !important; }}
      .mob-sm {{ font-size:11px !important; }}
      .hdr-main, .hdr-nav {{ display:block !important; width:100% !important; padding-left:0 !important; }}
      .hdr-nav {{ text-align:left !important; margin-top:12px; }}
      .hdr-nav div {{ text-align:left !important; }}
      .hdr-nav a {{ margin:0 5px 5px 0 !important; }}
    }}
  </style>
</head>
<body style="margin:0;padding:16px;background:#060b18;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<div class="ew" style="max-width:740px;margin:0 auto;background:{BG};border:1px solid {BORDER};border-radius:8px;overflow:hidden;">

  {header}
  {kpi}

  <div class="ew" style="padding:22px 26px;">
    {body}
  </div>

  <div style="text-align:center;padding:14px;color:{MUTED};font-size:11px;border-top:1px solid {BORDER};">
    Data refreshed {refreshed} &middot; ESPN League 277836 &middot; Guerrero Warfare
  </div>
</div>
</body>
</html>"""


# ── SEND ──────────────────────────────────────────────────────────────────────

def send_email(html, subject, filename=None):
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    if not GMAIL_APP_PASSWORD:
        print("ERROR: GMAIL_APP_PASSWORD not set — add it to .env")
        sys.exit(1)

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"]    = FROM_EMAIL
    msg["To"]      = TO_EMAIL
    msg["Cc"]      = CC_EMAIL

    # Inline body — Gmail clips this at 102KB; attachment below is the full render
    msg.attach(MIMEText(html, "html"))

    # HTML attachment so the full digest is always accessible (open in browser)
    attachment = MIMEText(html, "html", "utf-8")
    attachment.add_header(
        "Content-Disposition", "attachment",
        filename=filename or f"digest_{datetime.now().strftime('%Y-%m-%d')}.html",
    )
    msg.attach(attachment)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(FROM_EMAIL, GMAIL_APP_PASSWORD)
        smtp.sendmail(FROM_EMAIL, [TO_EMAIL, CC_EMAIL], msg.as_string())
    return 200


# ── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    dry_run    = "--dry-run"    in sys.argv
    no_refresh = "--no-refresh" in sys.argv
    override_team = None
    if "--team" in sys.argv:
        idx = sys.argv.index("--team")
        if idx + 1 < len(sys.argv) and not sys.argv[idx + 1].startswith("--"):
            override_team = sys.argv[idx + 1]
        else:
            print("WARNING: --team requires a team name argument, e.g. --team \"Houck Tuah\"")
            sys.exit(1)

    LOG_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    team_label = override_team or "Guerrero Warfare"
    print("=" * 60)
    print(f"  {team_label} Daily Digest")
    print(f"  {ts}")
    print("=" * 60)

    if not no_refresh:
        print("\n[1/3] Refreshing data (this takes ~60s)...")
        result = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "fetch_data.py")],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"  WARNING: fetch_data.py exited {result.returncode}")
            print(f"  {result.stderr[-300:] if result.stderr else '(no stderr)'}")
            if not SNAPSHOT.exists():
                sys.exit("No snapshot found and refresh failed — aborting.")
            print("  Falling back to existing snapshot.")
        else:
            print("  Refresh complete.")
    else:
        print("\n[1/3] Skipping data refresh (--no-refresh).")

    print("\n[2/3] Building email...")
    with open(SNAPSHOT) as f:
        snap = json.load(f)

    html      = build_email(snap, override_team=override_team)
    team_slug = team_label.replace(" ", "_")
    date_str   = datetime.now().strftime('%Y-%m-%d')
    _is_sun    = datetime.now().weekday() == 6
    subject    = f"⚾ {team_label} {'Lookahead' if _is_sun else 'Digest'} — {datetime.now().strftime('%b %d')}"

    if dry_run:
        fname = f"digest_preview_{team_slug}.html"
        previews_dir = Path(__file__).parent / "previews"
        previews_dir.mkdir(exist_ok=True)
        out = previews_dir / fname
        out.write_text(html, encoding="utf-8")
        print(f"\n  Dry run — saved to {out}")
        print("\nDone (no email sent).")
        return

    print(f"\n[3/3] Sending to {TO_EMAIL}...")
    attach_name = f"digest_{date_str}_{team_slug}.html" if override_team else f"digest_{date_str}.html"
    send_email(html, subject, filename=attach_name)
    print("  Sent.")

    # Structured send-history line. Wrapped so a locked/unwritable log never
    # crashes a run whose email already went out. run_digest.bat captures full
    # console output in a SEPARATE file (logs/run_console.log) to avoid the two
    # processes holding a handle on this same file at once.
    try:
        LOG_DIR.mkdir(exist_ok=True)
        log_line = f"{ts} | sent | subject={subject}\n"
        (LOG_DIR / "digest.log").open("a", encoding="utf-8").write(log_line)
    except OSError as e:
        print(f"  (log write skipped: {e})")

    print("\nDone.")


if __name__ == "__main__":
    main()
