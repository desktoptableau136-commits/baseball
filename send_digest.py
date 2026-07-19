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

import itertools
import json
import math
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from name_utils import _name_key  # canonical player-name join key (shared leaf module)
from fantasy.ui import *          # re-exported UI/presentation primitives (F5 split, part 1)
from fantasy.scoring import *     # re-exported scoring/calibration + YEAR (F5 split, part 2)
from fantasy.analytics import *   # re-exported shared analytics + score-breakdown prose (F5 split, part 3)
from fantasy.trades import *      # re-exported trade engine + pending-offer grader/renderers (F5 split, part 4)

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:                       # zoneinfo missing (very old Python / no tzdata)
    _ET = None

# Back-compat alias: this name matches a roster/ESPN player name to a FantasyPros stat
# row. It used to be its own near-duplicate of the name-key logic; it now points at the
# single canonical `_name_key` (shared with fetch_data/weekly_recap). Kept as an alias so
# existing internal call sites and `sd._badge_name_key` references (dashboard, docs) work.

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
SNAPSHOT   = Path(__file__).parent / "data" / "snapshot.json"
LOG_DIR    = Path(__file__).parent / "logs"

# ── SCORING ────────────────────────────────────────────────────────────────────

_FP_IL_TAGS  = {"IL10", "IL15", "IL60", "IL", "DTD", "O"}   # suffixes in FantasyPros "Player" field
_STATUS_LABELS = {
    "TEN_DAY_DL": "10-Day IL", "FIFTEEN_DAY_DL": "15-Day IL", "SIXTY_DAY_DL": "60-Day IL",
    "IL10": "10-Day IL", "IL15": "15-Day IL", "IL60": "60-Day IL",
}

def _fmt_status(s):
    return _STATUS_LABELS.get(s, s)

def _get_injury_status(r):
    """Return the best available injury status string for any player (rostered or FA)."""
    # ESPN_Status is merged for both hitters and pitchers (fetch_data.py's roster merges)
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

# Role-aware pitcher volume benchmarks, derived per snapshot (compute_pitcher_benchmarks)
# so "enough of a sample" and "a real workload" scale with the season instead of a fixed
# IP/GS/GP minimum that goes stale. Keyed by (window, role) → leader IP/GS/GP for that
# slice; the fractions below turn a leader into a threshold. Cold-start fallbacks are the
# old hard-coded minimums, used when a window/role slice has too few pitchers.

# Live score calibration (approach A). The SP/RP raw-score distributions are re-anchored each
# run so raw p50 -> 50 and p90 -> 80 on the shared 0-100 scale (same math as
# recalibrate_scores.py), instead of the constants being hand-pasted after a manual rerun.
# Recomputing live means the anchors track the season automatically; the median/p90 player is
# pinned by construction, so day-to-day drift is sub-point for most players. SMALL-POOL
# FALLBACK: when a role's qualified pool is below _MIN_CALIB_POOL (early season / thin sample)
# or degenerate, that role KEEPS these hand-tuned constants so a noisy distribution can't
# produce a wild transform. Populated by compute_score_calibration() at the top of build_email.

# Full-time AB benchmark per window, used to scale a hitter's score by playing time
# (a part-time bat accumulates fewer weekly PAs — see the opportunity adjustment in
# hitter_score). Populated at runtime by compute_ab_benchmarks() as a fraction of each
# window's leader, so it tracks the season instead of a stale hard-coded minimum. The
# dict below is only a cold-start fallback (early season / a window with too few players).

# League-average reference points, derived per snapshot (compute_league_averages) so the
# projection/probability math tracks the season instead of stale magic numbers — replaces
# the "league-average OPS" trio (_LEAGUE_AVG_OPS 0.717 / fetch_data LG_OPS 0.720 / the
# inline 0.730 in qs_probability) and qs_probability's fixed ERA/WHIP/K%/IP-per-start/barrel
# anchors. ONLY genuine "league average X" values live here; calibration/scaling constants
# (score-component spans, park factor, recalibration constants) deliberately do NOT.

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

_FA_SP_MIN_SCORE = 30   # hide streamer-tier FA starters below this SP score (not worth the risk)

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
    fa = [r for r in fa if r["_score"] >= _FA_SP_MIN_SCORE]
    return sorted(fa, key=lambda r: -r["_score"])[:12]

# ── Hitter tactical badges (PWR / SB / BUY-LOW / SELL-HIGH) ───────────────────────
# Glance flags next to a hitter's name, mirroring the pitcher QS/5K+/2-START badges:
# display-only (never folded into any score), and each carries a hover `title` naming
# the stat that justifies it so it stays "anchored" even in a table with no such column.

def _qs_stat_clause(row):
    """The run-prevention analytic behind a QS projection — xERA (the luck-stripped skill
    the ER projection regresses toward), falling back to raw ERA. Deliberately does NOT
    restate IP/start (that just repeats the projected-line IP). Empty when unavailable."""
    if row is None:
        return ""
    xera = _n(row.get("xERA"))
    if xera > 0:
        return f"{xera:.2f} xERA"
    era = _n(row.get("ERA"))
    if era > 0:
        return f"{era:.2f} ERA"
    return ""

def qs_badge(ip_g, er, row=None):
    """Cyan QS chip with a hover `title` naming the projected line that earned it, plus the
    length + run-prevention analytics (IP/start, xERA) backing the projection when available."""
    stat = _qs_stat_clause(row)
    tail = f" &mdash; {stat}" if stat else ""
    return _hit_badge("QS", CYAN, f"Projected {_fmt_ip(ip_g)} IP &middot; {er} ER{tail}")

def _k5_stat_clause(row):
    """The K-skill 'advanced stat' behind a 5K+ projection: raw whiff (swing-and-miss)
    rate preferred, then whiff percentile, then K%. Empty when none is available."""
    if row is None:
        return ""
    whiff = _n(row.get("WhiffPct"))
    if whiff > 0:
        return f"{whiff:.0f}% whiff rate"
    wpct = _n(row.get("WhiffPctile"))
    if wpct > 0:
        return f"{wpct:.0f}th-pctile whiff"
    kpct = _n(row.get("Kpct_P"))
    if kpct > 0:
        return f"{kpct * 100:.0f}% K rate"
    return ""

def k5_badge(k, row=None):
    """Yellow 5K+ chip with a hover `title` naming the projected strikeouts, plus the
    swing-and-miss skill (whiff rate) that backs the projection when available."""
    stat = _k5_stat_clause(row)
    tail = f" &mdash; {stat}" if stat else ""
    return _hit_badge("5K+", YELLOW, f"Projected {k} strikeouts (&ge; 5){tail}")

# ── Blowup-risk (low-floor) flag for starters ────────────────────────────────────
# A DISPLAY-ONLY, skill-based read of how prone a starter is to a disaster outing
# (the ER/WHIP-wrecking 5+ ER start you can't take back once it's slotted). It is
# deliberately NOT folded into pitcher_score/_score_p — a high-ceiling arm can still
# carry real blowup risk; the flag is an independent floor warning, not a quality knock.
#
# WHY SKILL-ONLY (no realized blowup rate): a pitcher's own past blowup FREQUENCY is
# almost pure binomial noise at a season's ~20 starts (p~0.17 -> SD~8.5%), so folding
# it in HURT decile lift in the walk-forward backtest (1.34x vs 1.38x skill-only).
# The four skill drivers below — traffic (WHIP), a strikeout escape hatch (K%),
# true-skill run prevention (xERA), and loud contact allowed (HardHit%) — carry the
# real, stable signal. Validated in backtest_projections.py (skill risk decile lift
# ~1.38x top decile / AUC ~0.54 on 1600+ walk-forward starts).

# ── Pitcher buy-low / sell-high (ERA vs xERA) — the pitcher analog of the hitter $ / ▼ ──
# MEAN-regression / luck read, DISTINCT from the ⚠ low-floor flag (single-start TAIL risk):
# a pitcher can be sell-high without being blowup-prone (a soft-contact arm riding a low
# BABIP) or blowup-prone without being sell-high (ugly ERA that already matches an ugly
# xERA). They share the ERA/xERA signal so they sometimes co-fire — a strong "move him" cue.
# xERA runs systematically ABOVE ERA in this data (~+0.33 median), so a raw threshold would
# over-fire "sell". De-bias against the league median gap → the flag means luckier/unluckier
# than TYPICAL. Set live from the snapshot (like _LG / _SCORE_CALIB); 0.0 cold-start default.

def hitter_badges(row, hit_pctile=None, cap=None):
    """Concatenated tactical badge HTML for a hitter row (priority PWR→SB→BUY/SELL; `cap=None`
    shows every applicable badge). `hit_pctile` is the league SB percentile pool
    (build_cat_percentiles) — when None, SB is skipped."""
    badges = []

    # PWR — power/HR threat (modeled per-game HR probability). Highest priority.
    hrp = _n(row.get("HR_Probability"))
    if hrp >= _PWR_HRP_MIN:
        badges.append(_hit_badge("PWR", PURPLE, _hrp_driver_str(row) or f"HR prob {hrp*100:.0f}%"))

    # SB — genuine base-stealer (scarce, streamable). Percentile of actual SB, speed-corroborated.
    if hit_pctile is not None:
        sb = _n(row.get("SB"))
        spd = _n(row.get("SprintSpeed"))
        if sb > 0 and _cat_pctile(hit_pctile, "SB", sb) >= _SB_PCTILE_MIN and (spd <= 0 or spd >= _SB_SPEED_MIN):
            _t = f"SB {sb:.0f}" + (f" · Sprint {spd:.1f} ft/s" if spd > 0 else "")
            badges.append(_hit_badge("SB", SILVER, _t))

    # BUY-LOW / SELL-HIGH — Statcast expected vs actual (skill-vs-luck read). Mutually exclusive.
    avg, iso, xba, xslg = _n(row.get("AVG")), _n(row.get("ISO")), _n(row.get("xBA")), _n(row.get("xSLG"))
    if avg > 0 and iso > 0 and xba > 0 and xslg > 0:
        slg = iso + avg
        d_ba, d_slg = xba - avg, xslg - slg
        _rt = f"xBA {xba:.3f} vs AVG {avg:.3f} · xSLG {xslg:.3f} vs SLG {slg:.3f}"
        if d_ba >= _XREG_BA and d_slg >= _XREG_SLG:
            badges.append(_hit_badge("$", GREEN, _rt))
        elif -d_ba >= _XREG_BA and -d_slg >= _XREG_SLG:
            badges.append(_hit_badge("&#9660;", RED, _rt))

    return "".join(badges[:cap])

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

# Typical starting slots per position. positional_breakdown ranks each team on the
# average of its top-K players here (not the mean of ALL eligible players), so a
# team's bench/utility depth — e.g. a backup catcher who carries 1B eligibility, or
# a cold bat sitting behind a starter — can't dilute a position into a phantom
# "need". K ~ the league's active-lineup count at each spot.

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

# Favorite MLB team(s) — their games are pinned to the top of "Today's MLB Games"
# regardless of matchup-overlap score (the manager is an Atlanta fan and wants to see
# them first). Set of team abbrevs (as _FULLNAME_TO_ABBREV maps them). Empty = no pin.

# ── HTML HELPERS ───────────────────────────────────────────────────────────────

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

def _sp_badge_context(row, qs_fires, k_fires, two_start_n, recent_era=None):
    """Explain the SP badges (2-START / QS / 5K+ / ⚠ RISK) actually shown on a row. Fed the
    already-computed fire flags + recent ERA from the render site so the panel can never
    disagree with the chips beside the name."""
    lines = []
    vals = _proj_line_vals(row)
    ip_g, er, k = vals if vals else (0, 0, 0)
    if two_start_n >= 2:
        lines.append(f'{two_start_badge()} {two_start_n} starts inside the matchup week.')
    if qs_fires:
        stat = _qs_stat_clause(row)
        tail = f", backed by {stat}" if stat else ""
        lines.append(f'{qs_badge(ip_g, er, row)} projected {_fmt_ip(ip_g)} IP &middot; {er} ER '
                     f'is a quality start (6+ IP, &le; 3 ER){tail}.')
    if k_fires:
        stat = _k5_stat_clause(row)
        tail = f", backed by a {stat}" if stat else ""
        lines.append(f'{k5_badge(k, row)} projected {k} strikeouts (&ge; 5){tail}.')
    if _is_blowup_risk(row, recent_era):
        drivers = _risk_drivers(row, recent_era)
        tail = ": " + " &middot; ".join(drivers) if drivers else ""
        lines.append(f'{blowup_badge(row, recent_era)} low floor &mdash; blowup-prone{tail}. '
                     f'A floor warning only; it doesn&rsquo;t lower the score.')
    _rflag = pitcher_regression_flag(row)
    if _rflag:
        era, xera = _n(row.get("ERA")), _n(row.get("xERA"))
        if _rflag == "sell":
            lines.append(f'{pitcher_regression_badge(row)} ERA {era:.2f} is running below his '
                         f'{xera:.2f} xERA &mdash; getting lucky, regression risk (sell-high). '
                         f'Separate from &#9888;: this is mean regression, not blowup floor.')
        else:
            lines.append(f'{pitcher_regression_badge(row)} ERA {era:.2f} is running above his '
                         f'{xera:.2f} xERA &mdash; unlucky, positive regression likely (buy-low).')
    return _badge_ctx_wrap(lines)

# Progressive enhancement for the tap-to-expand Score breakdown: with JS (the
# browser-opened attachment), clicking a score pill TOGGLES its breakdown open/closed —
# no more hunting for the ✕. It preventDefaults so the URL fragment never changes, which
# means the CSS `:target` rule stays a clean no-JS fallback (older/JS-off renderers still
# get open-on-click + ✕-to-close). Handles both the <tr> (table) and <div> (trade-card)
# breakdown variants; the ✕ still closes too. Attachment-only (Gmail strips <script> just
# like it strips <style>), so the email body is unaffected.

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

    # Regress the ERA the ER projection is built on toward the pitcher's expected ERA
    # (xERA — luck-stripped skill) or, when xERA is missing, the league starter ERA,
    # weighted by season IP. A 1607-start walk-forward backtest showed raw ERA
    # systematically UNDER-projects per-start ER (bias -0.33); this shrinkage removes
    # most of that bias (-> -0.25) and lowers RMSE without touching the K/IP terms.
    if era > 0:
        _reg_target = _n(r.get("xERA")) or (_LG.get("era") or 4.00)
        _ip_season = _n(r.get("IP"))
        era = (era * _ip_season + _reg_target * _ERA_REG_PRIOR_IP) / (_ip_season + _ERA_REG_PRIOR_IP)

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

def _whiff_sub(r):
    """Small muted second line under the K% cell showing the pitcher's raw overall
    swing-and-miss rate (Baseball Savant pitch-arsenal, pitches-weighted). DISPLAY
    ONLY — never scored (WhiffPctile already drives the K component). Distinct from
    the WhiffPctile 0-100 percentile: WhiffPct is a raw rate. Empty when missing."""
    val = _n(r.get("WhiffPct"))
    if val <= 0:
        return ""
    return (
        f'<div style="color:{MUTED};font-size:10px;margin-top:1px;white-space:nowrap;">'
        f'whiff {val:.0f}%</div>'
    )

def _qs_sub(r):
    """Small muted second line under the QS% cell naming the run-prevention SKILL
    behind the projection: xERA (Baseball Savant, luck-stripped ERA — what the ER
    projection regresses toward). DISPLAY ONLY. xERA only, NOT raw ERA (which has
    its own adjacent column here, unlike whiff% under K%). Empty when missing."""
    val = _n(r.get("xERA"))
    if val <= 0:
        return ""
    return (
        f'<div style="color:{MUTED};font-size:10px;margin-top:1px;white-space:nowrap;">'
        f'{val:.2f} xERA</div>'
    )

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

def inj_tag(r):
    inj = _get_injury_status(r)
    if not inj:
        return ""
    color = RED if (inj in _DL_STATUSES or inj.startswith("IL")) else YELLOW
    return f' <span style="color:{color};font-size:10px;font-weight:600;">{_fmt_status(inj)}</span>'

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
                          weekly_avgs=None, days_elapsed=None, remaining_proj=None,
                          matchup_days=7, game_days_elapsed=None, matchup_game_days=None):
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
    if game_days_elapsed is not None and matchup_game_days:
        elapsed_frac = min(1.0, max(0.0, game_days_elapsed / matchup_game_days))
    else:
        elapsed_frac = min(1.0, max(0.0, (days_elapsed or 0) / matchup_days))
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
        section_head(f"Matchup {week}", f"vs. {opp} · current standings") +
        score_banner +
        table
    )

# ── ROSTER HOT/COLD ──────────────────────────────────────────────────────────

def build_hot_cold_section(hitters, recent_hitting, my_team, best_recent_h=None, hit_pctile=None):
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
            r["score"], _hitter_score_breakdown(r["srow"], best_recent_h, hit_pctile),
            _bd_uid("rhc", r["name"]), 7)
        rows_html += (
            f'<tr style="{bg}">'
            f'<td style="{TD_S}font-weight:600;">{team_logo(r["team"])}{r["name"]}{r["inj"]}{hitter_badges(r["srow"], hit_pctile)}</td>'
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

        _whiff = _n(r["srow"].get("WhiffPct"))
        whiff_cell = (
            f'<span style="color:{GREEN};font-weight:700;">{_whiff:.0f}%</span>' if _whiff >= 30
            else (f'{_whiff:.0f}%' if _whiff > 0 else f'<span style="color:{MUTED};">—</span>')
        )
        _cell, _bdrow = score_reveal(
            r["score"], _pitcher_score_breakdown(r["srow"], best_recent_p),
            _bd_uid("phc", r["name"]), 7)
        rows_html += (
            f'<tr style="{bg}">'
            f'<td style="{TD_S}font-weight:600;">{team_logo(r["team"])}{r["name"]}{r["inj"]}{pitcher_regression_badge(r["srow"])}</td>'
            f'<td style="{TDC}color:{MUTED};">{r["pos"]}</td>'
            f'<td style="{TDC}">{r["season_era"]:.2f}</td>'
            f'<td style="{TDC}">{recent_str}</td>'
            f'<td style="{TDC}">{delta_html} {arrow}</td>'
            f'<td style="{TDC}">{whiff_cell}</td>'
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
        f'<th style="{TH_S}text-align:center;">Whiff%</th>'
        f'<th style="{TH_S}text-align:center;">Score</th>'
        f'</tr></thead><tbody>{rows_html}</tbody></table>'
    )

# ── CATEGORY PULSE ───────────────────────────────────────────────────────────

_RATE_CATS    = {"OPS", "ERA", "WHIP"}   # true rate stats — weighted-avg projection

_CLOSE_THRESH = {
    "R": 8, "HR": 3, "RBI": 8, "SB": 3, "OPS": 0.025, "B_SO": 8,
    "K": 8, "QS": 2, "W": 2, "ERA": 0.30, "WHIP": 0.08, "SVHD": 3,
}

# Win-% band that reads as a toss-up — drives the ⚡ on Category Pulse cards, kept in
# sync with the % chip so a bolt appears exactly when the odds are near even.
_TOSSUP_LO, _TOSSUP_HI = 45, 55

_HIT_CATS = {"R", "HR", "RBI", "SB", "OPS", "B_SO"}
_PIT_CATS = {"K", "QS", "W",   "ERA", "WHIP", "SVHD"}

# ── FA "Cats" column — which roto categories a free agent most helps ──────────────
# Only cats surfaced on the FA rows (B_SO omitted — not shown on FA hitter rows; QS is
# SP-only and FA RP has none). ERA/WHIP are lower-is-better (handled via _LOWER_BETTER).

def build_cat_percentiles(rows, cats):
    """{cat: sorted pool values} for percentile lookup, from a qualified YEAR player pool
    of one type. Cats with too small a pool are omitted (→ no chip)."""
    out = {}
    for c in cats:
        vals = sorted(v for v in (_cat_value(r, c) for r in rows) if v > 0)
        if len(vals) >= 8:
            out[c] = vals
    return out

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

# ── TRADE RADAR ───────────────────────────────────────────────────────────────
# Cross-team trade finder tilted to MY advantage. The rival accepts because the deal
# fixes a category NEED of theirs (I send a player strong in a cat that is my surplus and
# their need); I win on VALUE and on buy-low/sell-high timing. Season-fit ranking.
#
# TRADE VALUE is a CROSS-ROLE currency, NOT the role-calibrated 0-100 badge score. A 95
# closer and a 98 hitter both sit "top of their role," but they are NOT equal trade
# chips: an everyday bat moves 5 categories every day while a closer touches one punt
# category (SV+H) plus a sliver of K/ERA/WHIP in ~60 IP. `_trade_value` sums a player's
# above-median contribution across EVERY category they move (percentiles vs the whole
# qualified pool, so a reliever's low K/W volume ranks low), with saves discounted since
# we punt them — so a smasher correctly outweighs a one-category reliever.
#
# FAVOR-ME, GOOD-not-GREAT: I never trade an elite bat (give-side value ceiling), EXCEPT
# a sell-high regression candidate (whom I *want* to move). I never overpay (directional
# gate) and may extract up to a capped edge. Sell-high out + buy-low in is a double
# arbitrage: my sell-high guy's actual stats OVERstate his value, their buy-low guy's
# UNDERstate his — even on paper, I win; they still accept on category need.
# Graduated endowment/star reluctance (supply-side, ACCEPTANCE-layer only -- never folds
# into _tval): the better a player's role SCORE, the bigger the overpay a manager needs to
# part with him. Replaces the old binary score>=80 / 0.25-payup cliff. Premium in _tval:
# 74->0.00, 78->0.20, 82->0.40, 88->0.70, 94->1.00 (capped).
# Demand-side team-need value: a piece is worth more to a team that needs his position/cats.
# effective_value = _tval * need_mult(player, team); acceptance-read layer only.
# Roster-depth floor (both parties): a trade may not drop a team below POS_STARTERS bodies at a
# hitter position without a same-position body coming back (hard veto in find_trades). A deal that
# survives but leaves the rival at exactly the floor at a single-slot position (C/1B/2B/3B/SS) is
# read honestly — each such thin slot subtracts this from net_them so the read flips to "aggressive
# ask". Acceptance-read layer only; never folds into _tval. (See _leaves_position_short.)
# Player category coverage reuses the FA cat lists (QS/B_SO aren't per-player stats, so a
# team need in those simply finds no matching player — intentional, same as the FA "Cats").

# Positional-scarcity multipliers for the hitter trade currency, keyed by POS_GROUPS
# label. Set by compute_position_scarcity() in the scoring prelude; empty (→ no
# adjustment, raw behavior) until then. Pitchers are intentionally NOT scaled — pitcher
# value is already punt-saves-shaped (SV+H discounted), a separate axis from scarcity.

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

def compute_weekly_std(roto, current_week):
    """Return {team: {cat: population stddev}} of the same weekly buckets used by
    compute_weekly_avgs. Feeds the win-probability model (spread of a team's weekly
    category totals ≈ the uncertainty in this week's final value). Cats with < 2
    completed weeks are omitted (no meaningful spread → win-prob falls back to
    _CLOSE_THRESH)."""
    from collections import defaultdict
    CATS = ["R", "HR", "RBI", "SB", "OPS", "B_SO", "K", "QS", "W", "ERA", "WHIP", "SVHD"]
    past = [r for r in roto if int(r.get("Week", 0)) < current_week]
    if not past:
        return {}
    buckets = defaultdict(lambda: {c: [] for c in CATS})
    for row in past:
        t = " ".join((row.get("Team", "") or "").split())
        if not t:
            continue
        for c in CATS:
            try:
                buckets[t][c].append(float(row[c]))
            except (KeyError, TypeError, ValueError):
                pass

    def _std(vals):
        n = len(vals)
        m = sum(vals) / n
        return math.sqrt(sum((x - m) ** 2 for x in vals) / n)

    return {t: {c: _std(v) for c, v in cats.items() if len(v) >= 2}
            for t, cats in buckets.items()}

_WINPROB_SIGMA_INFLATE = 1.5   # calibration widen — see _cat_win_prob

def _cat_win_prob(pm, po, cat, sigma, remaining_frac):
    """Probability (p_win, p_tie) that I win / tie a category, from a normal model of
    the final margin. `pm`/`po` are my/opp projected end-of-week values; `sigma` is the
    combined per-week spread of the margin. Counting-cat uncertainty shrinks toward the
    week's end (× remaining_frac); rate cats keep their weekly spread. The tie band
    matches the display-rounding precision so p_win/p_tie agree with the point-estimate
    W/L/T (round(pm,dec) vs round(po,dec)).

    Sigma is widened by `_WINPROB_SIGMA_INFLATE`: `backtest_winprob.py` (walk-forward over
    ~10k historical category matchups) showed the raw std-of-weekly-values understates the
    true margin spread — the model was materially OVER-confident (a stated 90%+ won ~73%,
    ECE 7.4 pts). The raw std treats a team's historical mean as its true level, ignoring
    that the mean itself is uncertain (roster/role churn). 1.5x pulls ECE to ~2.8 (under the
    ~3-pt well-calibrated bar); the pre-week optimum is ~1.9 but that would over-widen mid/
    late-week, where banked stats legitimately cut uncertainty (and counting cats already
    taper via remaining_frac). DISPLAY-ONLY: this changes the shown Win%/⚡ toss-up flag, never
    a projected W/L/T verdict (proj_res is a separate point-estimate)."""
    sigma = sigma * _WINPROB_SIGMA_INFLATE
    dec  = _CAT_DEC.get(cat, 0)
    edge = (po - pm) if cat in _LOWER_BETTER else (pm - po)   # > 0 favors me
    eff  = sigma if cat in _RATE_CATS else sigma * max(remaining_frac, 0.0)
    eff  = max(eff, 1e-9)
    h    = 0.5 * (10 ** (-dec))                               # half a display unit
    def _phi(x):
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2)))
    p_win  = 1.0 - _phi((h - edge) / eff)
    p_loss = _phi((-h - edge) / eff)
    p_tie  = max(0.0, 1.0 - p_win - p_loss)
    return p_win, p_tie

def _project(current, avg, elapsed_frac, cat):
    """Project end-of-week value from current accumulated stat and historical weekly avg."""
    remaining = 1.0 - elapsed_frac
    if cat in _RATE_CATS:
        if elapsed_frac == 0:
            return avg  # no innings yet; NaN * 0 = NaN, so skip current entirely
        return current * elapsed_frac + avg * remaining   # weighted blend
    else:
        return current + remaining * avg                  # counting: add expected remainder

def classify_categories(matchup, weekly_avgs=None, days_elapsed=None, remaining_proj=None, matchup_days=7,
                        game_days_elapsed=None, matchup_game_days=None):
    """Classify each category's closeness, using the same projection math as Category
    Pulse. Returns {cat: (proj_res, tier)} where proj_res is W/L/T and tier is
    'tossup' (within a close-threshold — a thin margin) or 'leaning' (clear).
    Used to detect thin ERA/WHIP leads for the ratio-stat pickup warning."""
    out = {}
    if not matchup or not matchup.get("categories"):
        return out
    de = days_elapsed or 0
    if game_days_elapsed is not None and matchup_game_days:
        elapsed_frac = min(1.0, max(0.0, game_days_elapsed / matchup_game_days))
    else:
        elapsed_frac = min(1.0, max(0.0, de / matchup_days))
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

def build_category_pulse(matchup, weekly_avgs=None, days_elapsed=None, remaining_proj=None, is_sunday=False, weekly_std=None, matchup_days=7,
                         game_days_elapsed=None, matchup_game_days=None):
    if not matchup or not matchup.get("categories"):
        return ""

    week         = matchup.get("week", "")
    opp          = matchup.get("opp_team", "Opponent")
    my_team_key  = " ".join(matchup.get("my_team",  "").split())
    opp_team_key = " ".join(matchup.get("opp_team", "").split())

    # Projection setup — use game days when available so dark days (All-Star break etc.)
    # don't inflate projected totals or deflate win probability.
    if game_days_elapsed is not None and matchup_game_days:
        elapsed_frac = min(1.0, max(0.0, game_days_elapsed / matchup_game_days))
    else:
        elapsed_frac  = min(1.0, max(0.0, (days_elapsed or 0) / matchup_days))
    remaining_frac = 1.0 - elapsed_frac
    my_avgs  = (weekly_avgs or {}).get(my_team_key,  {})
    opp_avgs = (weekly_avgs or {}).get(opp_team_key, {})
    my_std   = (weekly_std or {}).get(my_team_key,  {})
    opp_std  = (weekly_std or {}).get(opp_team_key, {})
    has_proj = bool(my_avgs and opp_avgs)
    proj_results = []
    close_flags  = []   # per-card toss-up flag (win% in the _TOSSUP band) → summary count

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
        proj_res = None
        proj_html = ""
        win_pct = None
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

            # Win probability — combined per-week spread of the margin (falls back to the
            # close-threshold when a team has no weekly history yet).
            sm, so = my_std.get(cat), opp_std.get(cat)
            sigma = math.sqrt(sm * sm + so * so) if (sm is not None and so is not None) \
                    else (_CLOSE_THRESH.get(cat, 1) or 1)
            p_win, _ = _cat_win_prob(pm, po, cat, sigma, remaining_frac)
            win_pct = round(p_win * 100)

            proj_html = (
                f'<div style="margin-top:4px;color:{MUTED};font-size:9px;">'
                f'proj&nbsp;<span style="color:{TEXT};">{pm:.{dec}f}</span>'
                f'&nbsp;vs&nbsp;{po:.{dec}f}'
                f'</div>'
            )

        # ⚡ = toss-up: win odds near even, OR a projected tie (a tie is the closest
        # possible outcome, so it's always striking distance even when the win% sits
        # just outside the band — e.g. a low-volume cat projected to tie at 57%). Appears
        # in sync with the % chip; replaces the old current-margin closeness (blank until
        # games were played). Summary "N close" counts these.
        is_close = win_pct is not None and (
            proj_res == "T" or _TOSSUP_LO <= win_pct <= _TOSSUP_HI
        )
        close_flags.append(is_close)

        # Top-right corner badge: ⚡ (toss-up) OR the WIN % · then the projected-outcome
        # marker. On a toss-up the ⚡ replaces the number — the exact odds don't matter at
        # a coin-flip, but a decisive % (79% / 9%) is worth showing.
        corner_parts = []
        if is_close:
            corner_parts.append(f'<span style="color:{YELLOW};font-size:10px;">⚡</span>')
        elif win_pct is not None:
            # Color the confidence % to match the projected outcome (green = proj win,
            # red = proj loss, white = proj tie) — it always agrees in direction.
            wp_c = GREEN if proj_res == "W" else (RED if proj_res == "L" else TEXT)
            corner_parts.append(
                f'<span style="color:{wp_c};font-weight:400;font-size:8px;">{win_pct}%</span>'
            )
        # Projected-outcome marker: ▲ green = projected win, ▼ red = projected loss,
        # ◆ white = projected tie. Shown on EVERY card that has a projection (not only on
        # a flip) — it still reveals a flip via contrast with the current WINNING/LOSING/
        # TIED status, while always surfacing the week's projected result at a glance.
        if proj_res is not None:
            if proj_res == "W":
                mark_c, mark = GREEN, "▲"
            elif proj_res == "L":
                mark_c, mark = RED, "▼"
            else:
                mark_c, mark = TEXT, "◆"
            corner_parts.append(f'<span style="color:{mark_c};font-size:10px;">{mark}</span>')
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
    close_count  = sum(close_flags)
    summary = (
        f'<span style="color:{GREEN};font-weight:700;">{wins_count}W</span>'
        f'<span style="color:{MUTED};margin:0 4px;">·</span>'
        f'<span style="color:{RED};font-weight:700;">{losses_count}L</span>'
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
            f'<span style="color:{MUTED};margin:0 4px;">·</span>'
            f'<span style="color:{TEXT}88;font-weight:600;">{proj_t}T</span>'
        )

    return (
        section_head(f"Category Pulse — Matchup {week}", f"vs. {opp} · {'Final stretch — matchup ends today' if is_sunday else '% = win odds · ⚡ = toss-up'}") +
        f'<div style="margin-bottom:8px;font-size:12px;">{summary}</div>' +
        table
    )

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
        f'letter-spacing:.7px;margin-bottom:9px;">Last Matchup — Final Result</div>'
        f'<div style="display:flex;align-items:baseline;gap:10px;">'
        f'<span style="color:{outcome_color};font-weight:800;font-size:15px;">{outcome_word}</span>'
        f'<span style="color:{TEXT};font-weight:700;">{score_str}</span>'
        f'<span style="color:{MUTED};font-size:12px;">vs. {opp} &middot; Matchup {week}</span>'
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

_UPGRADE_MARGIN = 3.0   # min score-pt upgrade over my worst starter to bother flagging a bat pickup
                        # (deliberately modest: at my WEAKEST position even a small bump is worth it —
                        #  the point is to fill the hole, not chase the single biggest raw gap)

def _roster_suggestion(matchup, pitchers, hitters, fa_sp, fa_rp, fa_hit,
                        my_team, best_recent_p, best_recent_h,
                        all_matchups, week_end_str, classification=None,
                        league_total_roster_max=28, pos_data=None, lineup_eff=None,
                        pill_fn=None):
    """Return a LIST of Week-at-a-Glance pickup bullets (HTML strings), roster-context
    aware. (Was: a single 'best available hitter' bullet, blind to positional need -- it
    chronically told you to add an OF, the deepest pool, even while you were benching a
    masher there and near-last at catcher.)

      - BAT bullet: upgrade my WEAKEST hitter position where a real FA upgrade exists
        (positional_breakdown rank + score gap). NEVER a position I'm deep at or leaking
        bench production from (that's surplus / trade capital, not a hole).
      - PITCH bullet: when I'm in ratio trouble -- an active-slot implosion this week
        (lineup_eff blowups) OR losing ERA/WHIP by a non-toss-up margin (classification) --
        recommend a high-FLOOR stabilizer (low ERA/WHIP/xERA, real sample), not a volatile
        streamer that would make ratios worse.

    Drops prefer a SURPLUS player (deep position / bench-leaker), and the two bullets take
    DISTINCT drops. Falls back to a trade idea only when neither pickup fires.

    `pill_fn`: optional callback (score:int -> HTML) that renders a score pill after each
    add/drop player name — used by the dashboard's Recommended Moves tile for at-a-glance
    quality; the digest passes nothing (bullets unchanged)."""
    if not matchup:
        return []

    def _pill(score):
        return f' {pill_fn(int(round(score)))}' if pill_fn else ''

    classification = classification or {}
    pos_data   = pos_data or []
    lineup_eff = lineup_eff or {}
    cats        = matchup.get("categories", [])
    my_norm     = " ".join(my_team.split())
    opp         = matchup.get("opp_team", "")
    res_by_cat  = {c["cat"]: c["result"] for c in cats}
    losing_cats = {c["cat"] for c in cats if c["result"] == "L"}
    losing_hit  = losing_cats & _HIT_CATS

    full_pit = [r for r in pitchers
                if " ".join((r.get("FantasyTeam") or "").split()) == my_norm
                and int(r.get("Dataset", 0) or 0) == YEAR]
    full_hit = [r for r in hitters
                if " ".join((r.get("FantasyTeam") or "").split()) == my_norm
                and int(r.get("Dataset", 0) or 0) == YEAR]

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

    # -- SURPLUS vs NEED from positional_breakdown + bench leakage --------------
    # A hitter position is SURPLUS if I rank top-third there OR I'm leaving that
    # position's production on my bench (lineup_eff). It's a NEED if I rank bottom-third
    # AND a real FA upgrade exists over my weakest starter there.
    hit_by_name = {r.get("PlayerName"): r for r in full_hit}
    leak_groups = set()
    for b in (lineup_eff.get("bench") or []):
        r = hit_by_name.get(b.get("name"))
        if r:
            leak_groups |= _pos_groups_of(r)

    surplus_groups = set()
    need_positions = []   # (leverage, pos_entry, top_fa, worst)
    for p in pos_data:
        if p.get("ptype") != "hit":
            continue
        label = p.get("pos")
        n     = p.get("n_teams") or 12
        rank  = p.get("rank") or n
        third = max(1, round(n / 3.0))
        strong = rank <= third
        weak   = rank >= n - third + 1
        if strong or label in leak_groups:
            surplus_groups.add(label)
        top_fa = (p.get("top_fa") or [None])[0]
        worst  = p.get("worst_player")
        if top_fa is not None and weak and not strong and label not in leak_groups:
            # Upgrade size is measured against my STARTER quality (my_avg = top-K starter
            # avg), NOT my weakest eligible body (worst._pscore). worst is often a
            # multi-eligible backup (Caratini, a C carrying 1B eligibility) whose real
            # weakness belongs elsewhere, so beating him over-fired the margin and
            # recommended an FA that doesn't actually beat what I run out there -- the same
            # starters-not-scraps fix as the digest ↑ arrow (#60). (Even the _UPGRADE_MARGIN
            # comment already reads "over my worst starter".)
            lever = _n(top_fa.get("_pscore")) - _n(p.get("my_avg"))
            if lever >= _UPGRADE_MARGIN:
                need_positions.append((lever, p, top_fa, worst))

    # -- droppable pool, surplus-first -----------------------------------------
    drop_pit = [r for r in full_pit
                if r.get("PSP_Date", "1999-01-01") in ("1999-01-01", "")
                or r.get("PSP_Date", "9999-99-99") > week_end_str]
    scored_drop = sorted(
        [(r, _score_p(r, best_recent_p),              "pit") for r in drop_pit] +
        [(r, _blend(r, hitter_score, best_recent_h),  "hit") for r in full_hit],
        key=lambda x: x[1]
    )
    _drop_score = {id(r): s for r, s, _ in scored_drop}

    def _can_drop(cand):
        """True if dropping cand leaves at least one healthy player at every position it fills."""
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

    def _in_surplus(r):
        return bool(_pos_groups_of(r) & surplus_groups)

    # worst first, but surplus positions ahead of everything (drop from strength)
    drop_order = [r for r, _, _ in sorted(
        [(r, s, t) for r, s, t in scored_drop if _can_drop(r)],
        key=lambda x: (0 if _in_surplus(x[0]) else 1, x[1]))]
    _used_drops = set()

    def _take_drop():
        for r in drop_order:
            if r.get("PlayerName") not in _used_drops:
                _used_drops.add(r.get("PlayerName"))
                return r
        return None

    my_total_count = len(full_pit) + len(full_hit)
    slots_left = max(0, league_total_roster_max - my_total_count)

    def _move_tail(add_row):
        """Free-pickup badge if an open roster spot remains, else ' . Drop <worst surplus>'."""
        nonlocal slots_left
        if slots_left > 0:
            slots_left -= 1
            return f'<span style="color:{GREEN};font-size:10px;margin-left:6px;">&#10003; roster spot open</span>'
        d = _take_drop()
        if d and d.get("PlayerName") != add_row.get("PlayerName"):
            surplus_tag = f' <span style="color:{MUTED};font-size:10px;">[surplus]</span>' if _in_surplus(d) else ''
            return (f' &middot; Drop <span style="color:{MUTED};">{d.get("PlayerName","")}'
                    f' ({_pos_disp(d)})</span>{_pill(_drop_score.get(id(d), 0))}{surplus_tag}')
        return ''

    bullets = []

    # -- BAT bullet: fill my weakest hitter spot (never a surplus one) ----------
    bat_add = bat_reason = None
    if need_positions:
        # weakest position first (that's the hole the user wants filled), then biggest upgrade
        need_positions.sort(key=lambda x: ((x[1].get("rank") or 0), x[0]), reverse=True)
        lever, p, top_fa, worst = need_positions[0]
        bat_add = top_fa
        bat_reason = f'weakest bat spot &mdash; {p.get("pos")} #{p.get("rank")}/{p.get("n_teams")}'
    elif losing_hit:
        # fallback: best FA hitter for a losing cat, but NOT at a position I'm already deep at
        pool = sorted([r for r in fa_hit if not _in_surplus(r)],
                      key=lambda r: _blend(r, hitter_score, best_recent_h), reverse=True)
        if pool:
            bat_add = pool[0]
            bat_reason = "/".join(_CAT_DISPLAY.get(c, c) for c in sorted(losing_hit)) + " gap"
    if bat_add and bat_add.get("PlayerName"):
        bullets.append(
            f'Pickup (bat): Add <span style="color:{TEXT};font-weight:700;">{bat_add["PlayerName"]}</span>'
            f'{_pill(_blend(bat_add, hitter_score, best_recent_h))}'
            f'<span style="color:{MUTED};"> ({_pos_disp(bat_add)})</span>'
            f'<span style="color:{MUTED};"> &mdash; {bat_reason}</span>'
            + _move_tail(bat_add)
        )

    # -- PITCH bullet: stabilize ratios after a blowup / a non-toss-up ratio loss --
    blowups    = lineup_eff.get("blowups") or []
    ratio_loss = [c for c in ("ERA", "WHIP")
                  if res_by_cat.get(c) == "L"
                  and classification.get(c, (None, "leaning"))[1] != "tossup"]
    if blowups or ratio_loss:
        def _stable(r):
            era, whip = _n(r.get("ERA")), _n(r.get("WHIP"))
            if era <= 0 or whip <= 0 or era > 4.00 or whip > 1.25:
                return False
            if _is_sp(r):
                return _n(r.get("GS")) >= _pit_viable_min("SP", "GS")
            return (_n(r.get("ESPN_GP")) >= _pit_viable_min("RP", "GP")
                    or _n(r.get("IP")) >= _pit_viable_min("RP", "IP"))
        # Steer away from low-floor arms: a stabilizer must not be blowup-prone
        # (a tidy ERA/WHIP can still hide a high-xERA / loud-contact disaster profile).
        pool = sorted([r for r in (fa_sp + fa_rp) if _stable(r) and not _is_blowup_risk(r)],
                      key=lambda r: ((_n(r.get("xERA")) or _n(r.get("ERA"))), _n(r.get("WHIP"))))
        if pool:
            pa   = pool[0]
            era, whip = _n(pa.get("ERA")), _n(pa.get("WHIP"))
            role = "SP" if _is_sp(pa) else "RP"
            ec = GREEN if era < 3.50 else (YELLOW if era < 4.00 else TEXT)
            wc = GREEN if whip < 1.10 else (YELLOW if whip < 1.25 else TEXT)
            rl_lbl = "/".join(ratio_loss)
            if blowups and ratio_loss:
                preason = f'shore up {rl_lbl} after {len(blowups)} blowup{"s" if len(blowups) != 1 else ""} this week'
            elif blowups:
                preason = f'stabilize ratios after {len(blowups)} blowup{"s" if len(blowups) != 1 else ""} this week'
            else:
                preason = f'you&rsquo;re losing {rl_lbl}'
            bullets.append(
                f'Pitching fix: Add <span style="color:{TEXT};font-weight:700;">{pa.get("PlayerName","")}</span>'
                f'{_pill(_score_p(pa, best_recent_p))}'
                f'<span style="color:{MUTED};"> ({role}, </span>'
                f'<span style="color:{ec};">{era:.2f} ERA</span>'
                f'<span style="color:{MUTED};"> / </span><span style="color:{wc};">{whip:.2f} WHIP</span>'
                f'<span style="color:{MUTED};">) &mdash; {preason}</span>'
                + _move_tail(pa)
            )

    if bullets:
        return bullets

    # -- TRADE fallback (only when neither pickup fired) ------------------------
    opp_matchup = all_matchups.get(" ".join(opp.split()), {}) if opp else {}
    if not opp_matchup:
        return []

    opp_cats_map = {c["cat"]: c for c in opp_matchup.get("categories", [])}
    opp_winning  = {cat for cat, c in opp_cats_map.items() if c["result"] == "W"}
    my_winning   = {c["cat"] for c in cats if c["result"] == "W"}
    they_offer   = opp_winning  & losing_cats   # their surplus = my need
    i_offer      = my_winning   & {cat for cat, c in opp_cats_map.items() if c["result"] == "L"}

    if not they_offer or not i_offer:
        return []

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

    # Offer my 2nd-best in the offer category (skip ace -- unrealistic to trade away)
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
            _mn_score = (_score_p(my_offer, best_recent_p) if offer_cat in _PIT_CATS
                         else _blend(my_offer, hitter_score, best_recent_h))
            _tn_score = (_score_p(their_player, best_recent_p) if need_cat in _PIT_CATS
                         else _blend(their_player, hitter_score, best_recent_h))
            return [
                f'Trade: Offer <span style="color:{TEXT};font-weight:700;">{mn}</span>{_pill(_mn_score)}'
                f' to {opp} for <span style="color:{TEXT};font-weight:700;">{tn}</span>{_pill(_tn_score)}'
                f'<span style="color:{MUTED};"> &mdash; fills {nc} gap, gives them {oc}</span>'
            ]

    return []

def build_week_overview(matchup, week_cats, week_n, fa_sp, starts, days_elapsed, my_starts_by_day, week_end=None, is_sunday=False, roster_suggestion="", trade_bullets=None):
    bullets = []

    # Time-sensitive incoming trade offers ride at the TOP (they expire) — the full grade is
    # in the Pending Trades section; this is the headline so it isn't missed.
    if trade_bullets:
        bullets.extend(trade_bullets)

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
                f'Next matchup: <span style="color:{ACCENT};font-weight:700;">{len(next_confirmed)} starts</span>'
                f' already lined up across {nw_days} day{"s" if nw_days != 1 else ""} — check FA SP below to fill gaps.'
            )
        else:
            rot_str = (
                f'<span style="color:{YELLOW};font-weight:700;">No confirmed starts next matchup yet</span>'
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
            # Steer away from low-floor (blowup-prone) arms first, then prefer a two-start
            # pitcher (double the K/W/QS) among comparable candidates, then highest QS prob.
            best = max(pool, key=lambda r: (not _is_blowup_risk(r), _two(r), qs_probability(r) or 0))
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
            if _is_blowup_risk(best):
                _rd = _risk_drivers(best)
                _rt = " · ".join(_rd) if _rd else "blowup-prone skill profile"
                s += (f' · <span style="color:{RED};font-weight:700;" title="{_rt}">&#9888; low floor</span>')
            if _two(best):
                s += f' · <span style="color:{GREEN};font-weight:700;">×2 starts this matchup</span>'
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
                fa_str = _best_fa_str(fa_next, label_prefix="Top FA pickup next matchup")
            else:
                fa_str = f'<span style="color:{MUTED};">No confirmed FA starts next matchup yet — check back Monday.</span>'
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
                            f'<span style="color:{MUTED};">No FA starters this matchup</span>'
                            f' — next matchup: <span style="color:{TEXT};font-weight:700;">{best_nw["PlayerName"]}</span>'
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

    # roster_suggestion is now a LIST of bullets (bat + pitch); tolerate a bare string too
    if roster_suggestion:
        if isinstance(roster_suggestion, str):
            bullets.append(roster_suggestion)
        else:
            bullets.extend([b for b in roster_suggestion if b])

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
        f'letter-spacing:.7px;margin-bottom:8px;">{"Next Matchup Preview" if is_sunday else "Matchup at a Glance"}</div>'
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

    # A colored glyph marker (▲▼◆ / ⚡), sized to sit inline beside a term next to the
    # tinted _hit_badge chips. Keeps the reference showing the ACTUAL badge, not just prose.
    def _mark(g, c):
        return f'<span style="color:{c};font-weight:700;font-size:13px;margin-left:5px;vertical-align:middle;">{g}</span>'

    # Sub-group label INSIDE a _group (Badges & icons groups its chips by who they apply to).
    def _subhead(t):
        return (f'<div style="color:{ACCENT};font-size:10px;font-weight:800;text-transform:uppercase;'
                f'letter-spacing:.6px;margin:13px 0 3px;padding-top:9px;border-top:1px solid {BORDER};">{t}</div>')

    scores = _group("Scores (0–100)", [
        _entry("Role scores are unified",
               "Every player shows the <b>same</b> 0–100 score in every section, calibrated so the "
               "median qualified player ≈ 50 and a top-10% player ≈ 80. Benchmarks are derived from "
               "the live data each run, so “full-time” scales as the season grows."),
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
               "an ace ≈ 75%. Driven by innings-per-start, K%, ERA/WHIP and contact allowed. The muted "
               "<b>xERA</b> line beneath it is the luck-stripped run-prevention skill behind the projection "
               "(what the ER projection regresses toward) — a lower xERA than ERA hints the QS rate is earned."),
    ])
    badges = _group("Badges & icons", [
        _subhead("Any player"),
        _entry(f'Score badge (tap to expand){badge(72)}',
               "The colored pill is the player's 0–100 role score (green ≥ 72, blue ≥ 52, amber ≥ 32, red below). "
               "In the browser-opened attachment it expands on tap into a full-width row below the player that "
               "explains, in plain English, the 2-3 drivers behind the number (e.g. &ldquo;carried by "
               "swing-and-miss and a low WHIP; held back by hard contact&rdquo;) plus the season-vs-recent "
               "blend. The ▾ caret marks a tappable badge; ✕ (or tapping another badge) closes it."),
        _entry(f'Hot / cold{_mark("&#128293;", GREEN)}{_mark("&#10052;", ACCENT)}',
               "In the Hot/Cold columns, 🔥 (or ↑) marks a player running <b>hot</b> vs his season baseline "
               "over the recent window (7-day OPS for hitters, 15-day ERA for pitchers); ❄ (or ↓) marks "
               "<b>cold</b>. The colored value beside it is the recent stat; the Δ is the change from season. "
               "This recent window is narrower than the one named in the Score breakdown, so a bat can read "
               "🔥 here yet “cold” there."),

        _subhead("Pitchers (starters)"),
        _entry(f'QS / 5K+ / 2{qs_badge(6.0, 2)}{k5_badge(6)}{two_start_badge()}',
               "In My Upcoming Starts and FA Starting Pitchers, these annotate a starter's <b>projected line</b> "
               "for that day: cyan <b>QS</b> = the Proj. Line is a quality start (6+ IP & ≤3 ER); yellow "
               "<b>5K+</b> = projects 5+ strikeouts; blue <b>2</b> = two starts inside the matchup week. They "
               "match the Proj. Line exactly (no 5K+ next to a 4 K line) and appear regardless of your rotation "
               "that day. <b>Hover</b> (or tap the Score pill) for the projected line that earned each one — the "
               "5K+ tooltip also names the K-skill behind it (whiff rate, whiff percentile, or K%). The QS% "
               "column shows season quality-start probability separately."),
        _entry(f'⚠ low floor{_hit_badge("&#9888;", ORANGE)}',
               "Warns a starter is <b>blowup-prone</b> — a skill profile at risk of the disaster outing (5+ ER) "
               "that wrecks your ERA/WHIP and can't be undone once it's in your lineup. Blends baserunner traffic "
               "(WHIP), a strikeout escape hatch (K% / whiff), effective run prevention (ERA regressed toward "
               "xERA), and hard contact allowed, then escalates when the arm is <b>cold lately</b> (high L15 ERA). "
               "<b>Hover</b> for the worst 2–3 drivers. A floor warning only — it never lowers the Score, and the "
               "digest steers pickups away from flagged arms. Distinct from ▼ sell-high — ⚠ is single-start "
               "<i>tail</i> risk, ▼ is <i>mean</i> regression."),

        _subhead("Hitters"),
        _entry(f'PWR{_hit_badge("PWR", PURPLE)}',
               "Next to a hitter's name — a top-tier power/HR threat (highest modeled HR-probability tier). "
               "Shown first when several badges apply. Hover (or tap the Score pill) for the drivers."),
        _entry(f'SB{_hit_badge("SB", SILVER)}',
               "Next to a hitter's name — a genuine base-stealer (top-20% SB producer, corroborated by sprint "
               "speed)."),

        _subhead("Buy-low / sell-high — pitchers &amp; hitters"),
        _entry(f'$ / ▼{_hit_badge("$", GREEN)}{_hit_badge("&#9660;", RED)}',
               "Statcast expected-vs-actual regression flags (mutually exclusive). <b>$</b> (green) = "
               "<b>buy-low</b>: results running <i>behind</i> the expected stats (unlucky) → positive regression "
               "likely, a good acquire-cheap target. <b>▼</b> (red) = <b>sell-high</b>: results <i>ahead</i> of "
               "expected (lucky) → regression risk, move him while the surface looks great. For <b>hitters</b> "
               "the read is xBA/xSLG vs actual AVG/SLG; for <b>pitchers</b> it's xERA vs ERA (measured relative to "
               "the league's typical xERA-vs-ERA offset, since xERA runs a bit high). Display-only — never changes "
               "a Score — and it powers the buy-low / sell-high timing in Trade Radar. <b>Hover</b> for the numbers."),

        _subhead("Category Pulse cards"),
        _entry(f'Outcome markers{_mark("&#9650;", GREEN)}{_mark("&#9660;", RED)}{_mark("&#9670;", TEXT)}',
               "On every card, shows the <b>projected</b> end-of-matchup result: <b>▲</b> green = projected win, "
               "<b>▼</b> red = loss, <b>◆</b> white = tie. When it disagrees with the card's current "
               "WINNING/LOSING/TIED status, that's a projected flip."),
        _entry(f'Win % &amp; ⚡ toss-up{_mark("&#9889;", YELLOW)}',
               "The <b>%</b> in each card corner is your odds of winning that category (normal model of the final "
               "margin), colored to the projected outcome. On a toss-up — odds near even, or a projected tie — a "
               "<b>⚡</b> replaces the number instead."),

        _subhead("Pending trades"),
        _entry(f'Verdict{_verdict_pill("ACCEPT", GREEN)}&nbsp;{_verdict_pill("COUNTER", YELLOW)}&nbsp;{_verdict_pill("DECLINE", RED)}',
               "On a real trade offer made <b>to you</b>, the lean: <b>ACCEPT</b> (green) = you win the value, or "
               "it's roughly even and fills a real category/positional need without a timing trap; <b>COUNTER</b> "
               "(yellow) = the right direction but you'd be paying up or selling a riser / buying a regressor — on a "
               "counter it also <b>names the best add-on to ask the other manager for</b> (a spare piece of theirs "
               "that evens the value and helps a need of yours); <b>DECLINE</b> (red) = it addresses no need and you "
               "don't gain value. An even-value offer that <b>pries one of your star players at par</b> also leans "
               "COUNTER (hold out — you'd give up a crown jewel for parity). Value is judged on the same cross-role currency Trade Radar uses. Because offers "
               "<b>expire</b>, an incoming-offer headline (verdict + days left) also shows atop Matchup at a Glance "
               "and in the email body's &ldquo;Act today&rdquo; list. An offer <b>you</b> proposed shows an "
               "&ldquo;awaiting {partner}&rdquo; status instead (it's their call)."),
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
        _entry("Whiff%", "Raw swing-and-miss rate — share of swings that miss, across all pitch types "
               "(pitches-weighted, from Baseball Savant). A pitch-skill read on strikeout upside; ~25% is "
               "league average, 30%+ is elite. Shown for reference only — not folded into the Score."),
        _entry("Proj. Line (IP · ER · K)", "Projected stat line for one upcoming start. ER builds off the "
               "pitcher's ERA — regressed toward his expected ERA (xERA, luck-stripped) by sample size — then "
               "adjusts for opponent lineup strength (their OPS vs the league mean) and a home/away park factor; "
               "K uses his K/IP rate. IP is his per-start average."),
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
        _entry("Category Pulse cards", "Per-category snapshot of the current matchup: your value vs the "
               "opponent, who's winning, and whether the odds are a toss-up (⚡ = win % near even)."),
        _entry("Projected (proj) values", "The “proj” line on each card is the end-of-matchup estimate — for "
               "K/QS/W it uses your actual remaining starts × per-start rate; other cats use each team's "
               "per-matchup average — colored by its <b>projected</b> outcome (green = win, red = loss, white = "
               "tie). Uncertainty behind the corner win % comes from each team's matchup-to-matchup spread in "
               "that stat and shrinks for counting cats as the matchup ends; a category with no history yet "
               "falls back to its close-threshold. The % and the ▲▼◆ marker always agree in direction with "
               "the “proj” value (see <b>Badges & icons</b> for the markers)."),
        _entry(f'Trade Radar{_hit_badge("$", GREEN)}{_hit_badge("&#9660;", RED)}',
               "Trade ideas that <b>fix a rival's category need</b> (their reason to accept) "
               "while <b>tilting value to you</b>. You send a player strong in a category you're deep in and "
               "they're weak in; you get back one who fills a category <i>or a thin roster position</i> you "
               "need. Only players at a position where you have <b>surplus</b> are offered (no hole opened), "
               "and your <b>elite bats are protected</b> — never offered unless they're a sell-high regression "
               "candidate you'd want to move anyway. Value is judged on <b>true category contribution</b>, not "
               "the role-score badge (a closer and an everyday bat can both post a 90+ score, but the bat "
               "moves five categories daily vs one punt category in ~60 innings), and deals are tuned to "
               "<b>favor you</b> rather than land even. Where possible it leverages <b>buy-low / sell-high</b> "
               "timing (Statcast expected vs actual): move a bat whose surface stats are about to regress, "
               "acquire one due to rebound. Chips: blue = category gained, cyan = thin position upgraded, and "
               "the same <b>$</b> (buy-low) / <b>▼</b> (sell-high) glyphs used everywhere else in the digest — "
               "the footer tag tells you which way the timing helps you."),
        _entry("Luck (standings)", "Roto rank minus record rank. Positive = your W-L is better than your "
               "category performance suggests (running lucky); negative = unlucky."),
        _entry("Season Trajectory", "Every team's matchup result (W/L/T) across the whole season, "
               "teams in standings order down the rows, matchups across the columns, with each team's "
               "<b>current streak</b> (e.g. W3, L2) in the final column. Your row is highlighted."),
        _entry("Lineup Watch", "Reconstructs your <b>daily</b> lineup for the matchup so far (first Monday→"
               "yesterday) from ESPN's historical slots. Flags (a) counting-stat production a hitter put up "
               "while sitting on your <b>bench</b> — shown <b>net</b> of the bat you'd have benched to start him "
               "(so it only counts what a legal lineup change would actually have gained), (b) a starter "
               "who imploded in an <b>active</b> slot (ER/WHIP already counted), and (c) a hitter <b>idling</b> "
               "in an active slot — 0 AB on days his team played — when it's a pattern (3 games in a row, or an "
               "AB in under half his active games), i.e. wasting the roster spot. Only still-fixable misses "
               "surface — it's silent on a clean matchup. The Monday recap shows the fuller completed-matchup "
               "version."),
    ])
    sources = _group("Data sources", [
        _entry("FantasyPros", "Pitcher & hitter stat lines across 4 ranges (last 7/15/30 days + season)."),
        _entry("ESPN Fantasy", "Rosters, free agents, weekly roto box scores, standings, transactions, "
               "and season counting totals (SV/K/W/IP) for pitchers."),
        _entry("MLB Stats API", "Probable starters (confirmed, plus a rotation-order projection that walks "
               "each team's rotation through its upcoming games), the opponent lineup OPS each starter faces, "
               "and today's game schedule + national and local TV broadcasts for the Today's MLB Games panel."),
        _entry("Today's MLB Games", "The real games today worth tuning into, ranked by how much they overlap "
               "your current matchup. Each involved rostered player scores (2 if a confirmed starting pitcher, "
               "else 1) x (2 if he's yours, 1 if your opponent's) — so YOUR players and starters weigh most. "
               "Counted as likely to actually appear: hitters, confirmed starters (marked ⚾), and relievers "
               "(a save/hold chance moves the week); a starting pitcher who ISN'T tonight's probable — a starter "
               "on his off-day — is skipped so he can't inflate a game. Relievers are identified by usage ROLE, "
               "not just position. Hitters count even if their real manager benches them (we have no posted MLB "
               "batting lineups — only pitching probables are confirmed). A ★ game is your favorite team "
               "(Atlanta) — those are pinned to the top regardless of score. Each player carries the same "
               "tactical badges as the rest of the digest (PWR/SB/buy/sell for hitters, blowup-risk/buy/sell "
               "for pitchers). Where to watch shows national TV plus each side's local RSN; the pitching "
               "matchup lists both probables (yours in blue, your opponent's in red)."),
        _entry("Baseball Savant (via pybaseball)", "Statcast: contact quality, expected stats (xERA, "
               "xwOBA, xBA/xSLG), sprint speed and whiff percentiles."),
    ])

    return (
        section_head("Glossary &amp; Methodology",
                     "How every score and metric is computed, and where the data comes from · tap a section to expand")
        + f'<div style="margin-bottom:24px;">{scores}{badges}{pitching}{hitting}{proj}{sources}</div>'
    )

def build_season_trajectory(weekly_results, standings, my_team=MY_TEAM):
    """Season W/L/T grid — teams as rows (standings order), weeks as columns, current
    streak in the final column. Ported from weekly_recap.build_trajectory (the two
    scripts don't import each other) so the SEASON band of the daily digest carries the
    same at-a-glance history. `weekly_results` = snapshot `{week: {team: W/L/T}}`."""
    if not weekly_results or not standings:
        return ""

    teams = [s["team_name"] for s in sorted(standings, key=lambda s: s["standing"])]
    weeks = sorted(weekly_results.keys(), key=lambda w: int(w))
    if not weeks:
        return ""

    my_key = " ".join(my_team.split())

    def _get_result(week_data, team):
        r = week_data.get(team)
        if r:
            return r
        nteam = " ".join(team.split())
        for k, v in week_data.items():
            if " ".join(k.split()) == nteam:
                return v
        return ""

    def _streak(team):
        results = [_get_result(weekly_results[w], team) for w in weeks]
        results = [r for r in results if r]
        if not results:
            return ""
        last, count = results[-1], 0
        for r in reversed(results):
            if r == last:
                count += 1
            else:
                break
        return f"{last}{count}"

    week_headers = "".join(
        f'<th style="{TH_S}text-align:center;padding:4px 6px;min-width:22px;">{w}</th>'
        for w in weeks
    )

    rows_html = ""
    for team in teams:
        is_my  = " ".join(team.split()) == my_key
        streak = _streak(team)
        streak_color = GREEN if streak.startswith("W") else (RED if streak.startswith("L") else MUTED)
        bg = f"background:{ACCENT}12;" if is_my else ""

        # Frozen first column: sticky-left with an OPAQUE background (so scrolling week
        # cells don't show through it) + a right border to separate it from the grid.
        team_bg = "#13233f" if is_my else SURFACE
        team_cell = (
            f'<td style="{TD_S}position:sticky;left:0;z-index:2;background:{team_bg};'
            f'border-right:1px solid {BORDER};font-weight:{"800" if is_my else "600"};'
            f'color:{ACCENT if is_my else TEXT};white-space:nowrap;padding:4px 8px;">{team}</td>'
        )
        week_cells = ""
        for w in weeks:
            result = _get_result(weekly_results[w], team)
            if result == "W":
                cell_c = f"color:{GREEN};background:rgba(34,197,94,0.15);"
            elif result == "L":
                cell_c = f"color:{RED};background:rgba(239,68,68,0.12);"
            elif result == "T":
                cell_c = f"color:{TEXT};"
            else:
                cell_c = f"color:{MUTED};"
            week_cells += (
                f'<td style="{TDC}{cell_c}font-weight:700;font-size:11px;padding:4px 6px;">'
                f'{result or "\xb7"}</td>'
            )

        streak_cell = (
            f'<td style="{TDC}font-weight:700;color:{streak_color};font-size:11px;">'
            f'{streak}</td>'
        )
        rows_html += f'<tr style="{bg}">{team_cell}{week_cells}{streak_cell}</tr>'

    header_row = (
        f'<th style="{TH_S}position:sticky;left:0;z-index:3;border-right:1px solid {BORDER};">Team</th>'
        + week_headers
        + f'<th style="{TH_S}text-align:center;">Streak</th>'
    )

    # `direction:rtl` on the scroll container makes the horizontal scrollbar START at the
    # far right (latest weeks + streak) on load; `direction:ltr` on the table keeps the
    # columns reading week 1 → N normally. The first column is frozen via position:sticky
    # (above), so team names stay visible while you scroll left into earlier weeks.
    # border-collapse:separate (not collapse) because sticky cells + collapsed borders
    # render buggily in some engines.
    table = (
        f'<div style="overflow-x:auto;-webkit-overflow-scrolling:touch;direction:rtl;">'
        f'<table style="width:100%;border-collapse:separate;border-spacing:0;direction:ltr;">'
        f'<thead><tr>{header_row}</tr></thead>'
        f'<tbody>{rows_html}</tbody></table></div>'
    )

    return (
        section_head("Season Trajectory",
                     "W/L/T by matchup \xb7 current streak in final column") +
        table
    )

# Cats whose season value is an average, not a sum (rate stats).
_SEASON_RATE_CATS = {"OPS", "ERA", "WHIP"}

def build_season_roto_rankings(roto, my_team=MY_TEAM, team_logos=None, season_totals=None):
    """Season-long roto rankings grid: rank teams by cumulative roto score (summed weekly
    roto points) but DISPLAY each category's true season-to-date value from ESPN's
    cumulative `mTeam` view (`season_totals`, snapshot `season_cat_totals`). Ranking/coloring
    and the displayed value are independent by design: points reflect who won each category
    week by week; the value is ESPN's innings/AB-weighted season stat (rate cats like ERA
    can't be recovered by averaging weekly values). Falls back to the old summed/averaged
    weekly value only when a season total is missing. Ported from
    weekly_recap.build_season_roto_rankings (the two scripts don't import each other)."""
    if not roto:
        return ""

    team_logos = team_logos or {}
    season_totals = season_totals or {}
    _st_lookup = {" ".join(k.split()): v for k, v in season_totals.items()}
    _order = ["R", "HR", "RBI", "SB", "OPS", "B_SO", "K", "QS", "W", "ERA", "WHIP", "SVHD"]

    agg: dict = {}
    for r in roto:
        team = r.get("Team", "")
        if not team:
            continue
        t = agg.setdefault(team, {
            "pts":  {c: 0.0 for c in _order},   # summed roto points → ranking + coloring
            "vsum": {c: 0.0 for c in _order},   # summed raw value
            "vcnt": {c: 0   for c in _order},   # weeks with a value (for rate averages)
            "roto": 0.0,
        })
        t["roto"] += float(r.get("Roto_Score") or 0)
        for c in _order:
            t["pts"][c] += float(r.get(f"{c}_Points") or 0)
            v = r.get(c)
            if v not in (None, ""):
                try:
                    t["vsum"][c] += float(v)
                    t["vcnt"][c] += 1
                except (TypeError, ValueError):
                    pass

    if not agg:
        return ""

    # Per-cat coloring tiers from the DISTINCT summed-point values (tie-safe, mirrors
    # the live grid's value-based tiers rather than ordinal ranks).
    tiers = {}
    for c in _order:
        vals = sorted({t["pts"][c] for t in agg.values()}, reverse=True)
        tiers[c] = {
            "best":  vals[0] if vals else None,
            "2nd":   vals[1] if len(vals) > 1 else None,
            "worst": vals[-1] if vals else None,
            "2last": vals[-2] if len(vals) > 1 else None,
        }

    def _fmt(val, cat):
        dec = 3 if cat == "OPS" else (2 if cat in {"ERA", "WHIP"} else 0)
        try:
            f = f"{float(val):.{dec}f}"
            if dec > 0 and float(val) < 1.0:
                f = f.lstrip("0") or "0"
            return f
        except (TypeError, ValueError):
            return "—"

    ranked = sorted(agg.items(), key=lambda kv: -kv[1]["roto"])
    n      = len(ranked)
    my_key = " ".join(my_team.split())

    _th  = TH_S.replace("padding:8px 10px", "padding:3px 5px").replace("font-size:10px", "font-size:9px")
    _tdc = TDC.replace("padding:7px 10px", "padding:3px 5px").replace("font-size:13px", "font-size:10px")
    _tds = TD_S.replace("padding:7px 10px", "padding:3px 5px").replace("font-size:13px", "font-size:10px")

    rows_html = ""
    for rank, (team, t) in enumerate(ranked, 1):
        is_my = " ".join(team.split()) == my_key
        if rank <= 3:
            row_bg = "background:rgba(34,197,94,0.07);"
        elif rank >= n - 2:
            row_bg = "background:rgba(239,68,68,0.07);"
        else:
            row_bg = ""

        logo = fantasy_logo(team_logos.get(" ".join(team.split()), ""), 16, team)
        rank_color = GREEN if rank <= 3 else (RED if rank >= n - 2 else MUTED)

        st_row = _st_lookup.get(" ".join(team.split()), {})
        stat_cells = ""
        for c in _order:
            pts = t["pts"][c]
            # Prefer ESPN's true cumulative season value; fall back to the old
            # summed/averaged weekly value only when a season total is missing.
            if c in st_row and st_row[c] is not None:
                val = st_row[c]
            elif c in _SEASON_RATE_CATS:
                val = t["vsum"][c] / t["vcnt"][c] if t["vcnt"][c] else 0.0
            else:
                val = t["vsum"][c]
            val_str = _fmt(val, c)
            ti = tiers[c]
            if val_str == "—":
                color, badge = MUTED, False
            elif ti["best"] is not None and pts == ti["best"]:
                color, badge = GREEN, True
            elif ti["2nd"] is not None and pts == ti["2nd"]:
                color, badge = "#86efac", False
            elif ti["worst"] is not None and pts == ti["worst"]:
                color, badge = RED, True
            elif ti["2last"] is not None and pts == ti["2last"]:
                color, badge = YELLOW, False
            else:
                color, badge = MUTED, False
            inner = (
                f'<span style="border:1px solid {color};border-radius:3px;padding:2px 6px;">{val_str}</span>'
                if badge else val_str
            )
            stat_cells += f'<td style="{_tdc}color:{color};">{inner}</td>'

        rows_html += (
            f'<tr style="{row_bg}">'
            f'<td style="{_tdc}color:{rank_color};font-weight:700;width:24px;">{rank}</td>'
            f'<td style="{_tds}font-weight:{"800" if is_my else "600"};'
            f'color:{ACCENT if is_my else TEXT};white-space:nowrap;">{logo}{team}</td>'
            f'<td style="{_tdc}font-weight:700;">{t["roto"]:.1f}</td>'
            + stat_cells +
            f'</tr>'
        )

    stat_headers = "".join(
        f'<th style="{_th}text-align:center;">{_CAT_DISPLAY.get(c, c)}</th>'
        for c in _order
    )
    header_row = (
        f'<th style="{_th}text-align:center;width:24px;">#</th>'
        f'<th style="{_th}">Team</th>'
        f'<th style="{_th}text-align:center;">Pts</th>'
        + stat_headers
    )
    table = (
        f'<div style="overflow-x:auto;-webkit-overflow-scrolling:touch;">'
        f'<table style="width:100%;border-collapse:collapse;font-size:10px;">'
        f'<thead><tr>{header_row}</tr></thead>'
        f'<tbody>{rows_html}</tbody></table></div>'
    )

    return (
        section_head("Season Roto Rankings",
                     "Ranked by cumulative roto points \xb7 category values are true season-to-date (ESPN) \xb7 "
                     "bright green = #1 \xb7 light green = #2 \xb7 amber = 2nd-last \xb7 red = last") +
        table
    )

def build_bench_watch(eff):
    """Compact matchup-to-date 'Lineup Watch' callout for the daily digest: batter
    production you've stranded on the bench so far this matchup (net of the bat you'd have
    sat — still fixable for the remaining days) + any starter who imploded in an active
    slot (ER/WHIP already counted) + hitters idling in an active slot (wasting the spot).
    eff = snapshot['lineup_efficiency_current'] (matchup start->yesterday). Silent on a
    clean matchup. Fuller post-mortem lives in the recap's build_lineup_efficiency."""
    if not eff:
        return ""
    bench   = eff.get("bench") or []
    blowups = eff.get("blowups") or []
    idle    = eff.get("idle") or []
    net     = eff.get("net") or {}
    net_bits = [f"{net.get(c, 0):+.0f} {c}" for c in ("HR", "RBI", "R", "SB") if net.get(c, 0)]
    if not net_bits and not blowups and not idle:
        return ""

    rows = []
    if net_bits:
        rows.append(
            f'<div style="font-size:12px;color:{TEXT};padding:3px 0;">'
            f'<strong>{" &middot; ".join(net_bits)}</strong> '
            f'<span style="color:{MUTED};">left on your bench so far this week</span></div>'
        )
        for b in bench[:2]:
            days = b.get("days") or []
            swap = next((d["tag"] for d in days if str(d.get("tag", "")).startswith("vs ")), "")
            hits = " &middot; ".join(f"{b[c]} {c}" for c in ("HR", "RBI", "SB", "R") if b[c])
            note = f' <span style="color:{MUTED};">(startable {swap})</span>' if swap else ""
            rows.append(
                f'<div style="font-size:11px;color:{MUTED};padding:1px 0 1px 16px;">'
                f'<span style="color:{TEXT};font-weight:600;">{b["name"]}</span> &mdash; {hits} on the bench{note}</div>'
            )
    for p in blowups:
        drop = f', <span style="color:{RED};">dropped {p["drop_when"]}</span>' if p.get("drop_when") else ""
        rows.append(
            f'<div style="font-size:11px;color:{MUTED};padding:1px 0;">'
            f'<span style="color:{TEXT};font-weight:600;">{p["name"]}</span> imploded in your active slot '
            f'({p["ip"]} IP, {p["er"]} ER){drop} &mdash; ER/WHIP already counted</div>'
        )
    if idle:
        rows.append(
            f'<div style="font-size:12px;color:{TEXT};padding:6px 0 2px;">'
            f'<strong>Wasting active space</strong> '
            f'<span style="color:{MUTED};">&mdash; idle in your lineup on game days</span></div>'
        )
        for p in idle[:3]:
            rows.append(
                f'<div style="font-size:11px;color:{MUTED};padding:1px 0 1px 16px;">'
                f'<span style="color:{TEXT};font-weight:600;">{p["name"]}</span> &mdash; {p["reason"]} '
                f'({p["played"]}/{p["active"]} games with an AB)</div>'
            )

    dates = eff.get("week_dates", "")
    return (
        f'<div style="background:{SURFACE};border:1px solid {BORDER};border-left:3px solid {RED};'
        f'border-radius:6px;padding:12px 14px;margin-bottom:20px;">'
        f'<div style="color:{RED};font-size:10px;font-weight:700;text-transform:uppercase;'
        f'letter-spacing:.7px;margin-bottom:6px;">&#9888; Lineup Watch &middot; matchup to date ({dates})</div>'
        f'{"".join(rows)}</div>'
    )

# ── EMAIL BODY: "THE BRIEFING" ────────────────────────────────────────────────
# The inline email body is NOT the full digest anymore (that ships as the attachment).
# It's a short, plain-English skim — time-sensitive actions up top, then a one-line
# matchup and season read with a touch of prose — meant to be glanced at before opening
# the dashboard and diving into the digest. All values come from the same computed
# artifacts the digest uses. Inline-styled only (Gmail strips <style>), so it renders
# in any mail client. Kept defensive: any missing piece is simply skipped.

_BRIEF_HIT_CATS = {"R", "HR", "RBI", "SB", "OPS", "B_SO"}
_BRIEF_CAT_LABEL = {"B_SO": "few strikeouts", "SVHD": "SV+H", "QS": "QS", "OPS": "OPS",
                    "ERA": "ERA", "WHIP": "WHIP", "HR": "HR", "RBI": "RBI",
                    "SB": "SB", "R": "runs", "K": "K", "W": "wins"}

def _brief_lab(c):
    return _BRIEF_CAT_LABEL.get(c, c)

def _brief_cat_list(cats, limit=3):
    """Join up to `limit` category labels as friendly prose ('runs, HR and RBI')."""
    labs = [_brief_lab(c) for c in cats[:limit]]
    if not labs:
        return ""
    if len(labs) == 1:
        return labs[0]
    return ", ".join(labs[:-1]) + " and " + labs[-1]

def render_briefing(my_team, today, matchup, classification, starts, today_str,
                    week_end_str, sr_emerging, alerts, my_row, n_teams, tune_in="",
                    pending_incoming=None):
    """Short, skimmable inline email body ("The Briefing"). Returns an HTML string."""
    my_team = " ".join((my_team or "").split())    # collapse ESPN's double-space for display
    # %-d is not portable (Windows), so build the day number by hand.
    try:
        date_str = today.strftime("%a %b ") + str(today.day)
    except Exception:
        date_str = ""

    # ── Matchup: current record (ESPN) + projected record (classification) ──────
    cw = (matchup or {}).get("wins", 0); cl = (matchup or {}).get("losses", 0); ct = (matchup or {}).get("ties", 0)
    pw = sum(1 for (res, _t) in classification.values() if res == "W")
    pl = sum(1 for (res, _t) in classification.values() if res == "L")
    ptt = sum(1 for (res, _t) in classification.values() if res == "T")
    if pw > pl:
        mk, mc, verb = "▲", GREEN, "on track to win"
    elif pl > pw:
        mk, mc, verb = "▼", RED, "trailing"
    else:
        mk, mc, verb = "◆", TEXT, "dead even"

    lead_cats = [c for c, (res, tier) in classification.items() if res == "W" and tier != "tossup"]
    toss_cats = [c for c, (res, tier) in classification.items() if tier == "tossup"]
    lose_cats = [c for c, (res, tier) in classification.items() if res == "L" and tier != "tossup"]
    lead_hit = [c for c in lead_cats if c in _BRIEF_HIT_CATS]
    lead_pit = [c for c in lead_cats if c not in _BRIEF_HIT_CATS]

    # ── Analyst-voice narrative of the matchup (connected sentences) ────────────
    margin = pw - pl
    n_toss = len(toss_cats)
    # Framed as the PROJECTION, not the current standing: the visible record is "now"
    # (e.g. a 6-6-0 tie), while pw/pl is where the categories are trending -- so "ahead"
    # must read as projected, not as a claim about the tied current record beside it.
    if pw > pl:
        lead_in = ("The projection has you firmly in control of this matchup" if (margin >= 4 and n_toss <= 3)
                   else "The projection leans your way, though it isn't locked away")
    elif pl > pw:
        lead_in = ("The projection has you in a real hole this matchup" if (pl - pw) >= 4
                   else "The projection has you trailing, but within striking distance")
    else:
        lead_in = "The projection is a dead heat"

    where = []
    if lead_hit:
        where.append(f"the bats are doing the heavy lifting ({_brief_cat_list(lead_hit)})")
    if lead_pit:
        where.append(f"the arms have {_brief_cat_list(lead_pit)} well in hand")
    where_clause = (" — " + " and ".join(where)) if where else ""

    if n_toss:
        need = n_toss // 2 + 1
        cats_txt = _brief_cat_list(toss_cats, limit=4)
        swing_clause = (f" It hinges on a single coin-flip: {cats_txt}."
                        if n_toss == 1 else
                        f" It turns on {n_toss} coin-flips ({cats_txt}) — take {need} and the week is yours.")
    elif lose_cats:
        swing_clause = f" The ground to make up is in {_brief_cat_list(lose_cats)}."
    else:
        swing_clause = ""

    matchup_prose = f"{lead_in}{where_clause}.{swing_clause}".strip()

    opp = (matchup or {}).get("opp_team", "")
    opp_str = f" vs {opp}" if opp else ""

    # ── ACT TODAY: time-sensitive items ─────────────────────────────────────────
    items = []
    # Incoming trade offers come FIRST — they expire, so they're the most time-sensitive.
    for g in (pending_incoming or []):
        label = g["verdict"][0] if g.get("verdict") else "REVIEW"
        col = {"ACCEPT": GREEN, "COUNTER": YELLOW, "DECLINE": RED}.get(label, ACCENT)
        items.append((col, _pending_headline(g, brief=True)))
    upcoming = [s for s in (starts or [])
                if today_str <= s.get("PSP_Date", "") <= week_end_str]
    two_start = [s for s in upcoming if _starts_this_week(s, today_str, week_end_str) >= 2]
    if two_start:
        nm = two_start[0].get("PlayerName", "an arm")
        extra = f" (+{len(two_start) - 1} more)" if len(two_start) > 1 else ""
        items.append((ACCENT, f"<b>{nm}</b> starts twice this matchup{extra} — lock him in"))
    elif upcoming:
        items.append((MUTED, f"{len(upcoming)} start{'s' if len(upcoming) != 1 else ''} coming this matchup — set your rotation"))

    risky = [s for s in upcoming if _is_blowup_risk(s)]
    if risky:
        nm = risky[0].get("PlayerName", "an arm")
        items.append((ORANGE, f"⚠ <b>{nm}</b> is a low-floor start — weigh sitting him"))

    if sr_emerging:
        e = sr_emerging[0]
        items.append((GREEN, f"FA closer heating up: <b>{e.get('name','')}</b> "
                             f"({int(e.get('recent',0))} saves last 15d) — worth a claim"))

    if alerts:
        nms = [a.get("name", "") for a in alerts[:2] if a.get("name")]
        if nms:
            more = f" +{len(alerts) - len(nms)}" if len(alerts) > len(nms) else ""
            items.append((RED, f"Injury watch: <b>{', '.join(nms)}</b>{more} — check your lineup"))

    if tune_in:
        items.append((ACCENT, tune_in))

    if items:
        rows = "".join(
            f'<div style="padding:3px 0;font-size:13px;line-height:1.5;color:{TEXT};">'
            f'<span style="color:{col};">•</span> {txt}</div>'
            for col, txt in items
        )
        act_html = (
            f'<div style="background:{SURFACE};border:1px solid {BORDER};border-left:3px solid {ORANGE};'
            f'border-radius:6px;padding:11px 13px;margin-bottom:16px;">'
            f'<div style="color:{ORANGE};font-size:10px;font-weight:700;letter-spacing:.7px;'
            f'text-transform:uppercase;margin-bottom:5px;">⚡ Act today</div>{rows}</div>'
        )
    else:
        act_html = (
            f'<div style="font-size:12px;color:{MUTED};margin-bottom:16px;">'
            f'✓ Nothing urgent — lineup looks set.</div>'
        )

    # ── Season read ─────────────────────────────────────────────────────────────
    standing = my_row.get("standing", "—")
    roto_rank = my_row.get("roto_rank", "—")
    luck = my_row.get("luck", 0)
    lk = luck if isinstance(luck, (int, float)) else 0
    strong = abs(lk) >= 3
    if lk < 0:
        season_prose = (f"You're outplaying your record — #{roto_rank} in roto quality against a #{standing} "
                        f"standing. That gap is {'largely ' if strong else ''}bad luck, and it tends to correct "
                        f"in your favor over a full season.")
    elif lk > 0:
        season_prose = (f"You're a step ahead of your process — #{standing} in the standings on a #{roto_rank} "
                        f"roto profile. {'A fair bit of' if strong else 'A little'} variance is padding the "
                        f"record, so keep banking wins while it lasts.")
    else:
        season_prose = (f"Record and roto quality line up cleanly (both around #{roto_rank}) — you're right "
                        f"where you've earned to be, with no luck correction looming either way.")

    # ── Assemble ────────────────────────────────────────────────────────────────
    return (
        f'<div style="max-width:640px;margin:0 auto 20px;font-family:-apple-system,BlinkMacSystemFont,'
        f'\'Segoe UI\',Roboto,sans-serif;background:{SURFACE2};border:1px solid {BORDER};'
        f'border-radius:10px;padding:20px 22px;color:{TEXT};">'
        f'<div style="font-size:17px;font-weight:800;color:{TEXT};">⚾ {my_team}</div>'
        f'<div style="font-size:11px;color:{MUTED};margin-bottom:15px;letter-spacing:.3px;">'
        f'The Briefing · {date_str}</div>'
        f'{act_html}'
        # Matchup
        f'<div style="margin-bottom:13px;">'
        f'<div style="font-size:10px;font-weight:700;letter-spacing:.7px;color:{MUTED};'
        f'text-transform:uppercase;margin-bottom:3px;">This matchup{opp_str}</div>'
        f'<div style="font-size:15px;color:{mc};font-weight:700;">{mk} {cw}–{cl}–{ct} '
        f'<span style="color:{MUTED};font-weight:400;font-size:13px;">now · '
        f'{verb} {pw}–{pl}–{ptt}</span></div>'
        + (f'<div style="font-size:12.5px;color:#cbd5e1;margin-top:4px;line-height:1.5;">{matchup_prose}</div>' if matchup_prose else "")
        + '</div>'
        # Season
        f'<div style="margin-bottom:4px;">'
        f'<div style="font-size:10px;font-weight:700;letter-spacing:.7px;color:{MUTED};'
        f'text-transform:uppercase;margin-bottom:3px;">Season</div>'
        f'<div style="font-size:15px;color:{TEXT};font-weight:700;">#{standing} of {n_teams}</div>'
        f'<div style="font-size:12.5px;color:#cbd5e1;margin-top:4px;line-height:1.5;">{season_prose}</div>'
        '</div>'
        # Footer
        f'<div style="font-size:11.5px;color:{MUTED};margin-top:16px;border-top:1px solid {BORDER};'
        f'padding-top:11px;line-height:1.5;">Skim the <b style="color:{TEXT};">dashboard</b> attachment for the '
        f'glance, then open the <b style="color:{TEXT};">digest</b> attachment for the full breakdown.</div>'
        '</div>'
    )

# ── TODAY'S MLB GAMES ─────────────────────────────────────────────────────────

def _is_favorite_game(g):
    """True when either club in this game is a favorite team (_FAVORITE_MLB_TEAMS)."""
    for side in ("home_name", "away_name"):
        if _FULLNAME_TO_ABBREV.get(g.get(side, "")) in _FAVORITE_MLB_TEAMS:
            return True
    return False

def _rank_todays_games(todays_games, my_key, opp_key, pin_favorite=True):
    """Rank today's games by how much they overlap the current matchup, biased toward MY
    roster. Each returned item = {g, mine, opp, score, fav}; `mine`/`opp` are the involved
    rostered players. Only players likely to actually appear count: HITTERS (probable to
    play), CONFIRMED STARTING PITCHERS, and RELIEVERS (a save/hold chance moves the
    matchup). The one exclusion is a STARTING pitcher who isn't tonight's probable (a
    starter on his off-day) — he won't appear, so he can't inflate the count. Value per
    player = (2 if confirmed starter else 1) × (2 for my player, 1 for the opponent's) —
    my players and starters both weigh more. Games with fewer than 2 counted players and
    no starter are dropped — EXCEPT a favorite-team game (_FAVORITE_MLB_TEAMS), which is
    always kept. When pin_favorite, favorite games sort ahead of everything regardless of
    score (the manager wants to see them first); within each tier, score desc then
    earliest first pitch. pin_favorite=False gives pure impact order (used by the Briefing
    'tune in' teaser, which should name the true highest-overlap game)."""
    ranked = []
    for g in (todays_games or []):
        fav = _is_favorite_game(g)
        mine, opp = [], []
        for p in g.get("involved", []):
            if p.get("is_p") and not p.get("is_sp") and not p.get("is_rp"):
                continue   # a starter on his off-day -> won't appear (relievers DO count)
            fk = " ".join((p.get("FantasyTeam") or "").split())
            if fk == my_key:
                mine.append(p)
            elif fk == opp_key:
                opp.append(p)
        if not fav:
            if not mine and not opp:
                continue
            n_sp = sum(1 for p in mine + opp if p.get("is_sp"))
            if (len(mine) + len(opp)) < 2 and n_sp == 0:
                continue
        def _val(players, side_mult):
            return sum((2 if p.get("is_sp") else 1) * side_mult for p in players)
        score = _val(mine, 2) + _val(opp, 1)   # my players weighted 2x
        ranked.append({"g": g, "mine": mine, "opp": opp, "score": score, "fav": fav})
    ranked.sort(key=lambda x: ((not x["fav"]) if pin_favorite else False,
                               -x["score"], x["g"].get("game_time_utc", "")))
    return ranked

def build_todays_games_section(todays_games, my_team, opp_team, max_games=4,
                               hit_rows=None, pit_rows=None, recent_era=None, hit_pctile=None):
    """The 'Today's MLB Games' panel — the real games worth tuning into because they
    carry the most of my and my opponent's rostered players. Returns '' when nothing
    qualifies (off-day / no overlap). Perspective is applied here from each involved
    player's FantasyTeam, so it works under --team for free. When the row lookups are
    passed (`hit_rows`/`pit_rows` = season YEAR row keyed by _badge_name_key, `recent_era`
    = L15 ERA keyed the same, `hit_pctile` = the qualified-pool percentile index), each
    involved player gets the SAME role-aware tactical badges as the rest of the digest —
    hitters PWR/SB/$/▼ (`hitter_badges`), pitchers ⚠ (`blowup_badge`, self-gated to
    startable arms) + $/▼ (`pitcher_regression_badge`)."""
    my_key  = " ".join((my_team or "").split())
    opp_key = " ".join((opp_team or "").split())
    ranked = _rank_todays_games(todays_games, my_key, opp_key)
    if not ranked:
        return ""
    hit_rows = hit_rows or {}; pit_rows = pit_rows or {}; recent_era = recent_era or {}

    def _badges(p):
        key = _badge_name_key(p.get("name", ""))
        if p.get("is_p"):
            row = pit_rows.get(key)
            if not row:
                return ""
            return blowup_badge(row, recent_era.get(key)) + pitcher_regression_badge(row)
        row = hit_rows.get(key)
        return hitter_badges(row, hit_pctile) if row else ""

    def _side(players, label, color):
        if not players:
            return f'<span style="color:{MUTED};">{label}: 0</span>'
        names = ", ".join(
            f'<span style="color:{MUTED};">{p.get("name","")}'
            + (f'<span style="color:{CYAN};font-weight:700;"> ⚾</span>' if p.get("is_sp") else "")
            + _badges(p)
            + '</span>'
            for p in players
        )
        return (f'<span style="color:{color};font-weight:700;">{label}: {len(players)}</span> '
                f'<span style="color:{MUTED};">({names})</span>')

    cards = []
    for item in ranked[:max_games]:
        g = item["g"]
        away_ab = _FULLNAME_TO_ABBREV.get(g.get("away_name", ""), "")
        home_ab = _FULLNAME_TO_ABBREV.get(g.get("home_name", ""), "")
        fav_star = (f'<span style="color:{YELLOW};font-weight:700;" title="Your team — pinned to the top">★ </span>'
                    if item.get("fav") else "")
        matchup_html = (
            f'{fav_star}'
            f'{team_logo(away_ab, 16)}<span style="color:{TEXT};font-weight:700;">{away_ab or g.get("away_name","")}</span>'
            f'<span style="color:{MUTED};"> @ </span>'
            f'{team_logo(home_ab, 16)}<span style="color:{TEXT};font-weight:700;">{home_ab or g.get("home_name","")}</span>'
        )

        # Header meta: first pitch (ET) + where to watch (national TV bright, local RSNs muted).
        meta = []
        gt = _fmt_game_time_et(g.get("game_time_utc", ""))
        if gt:
            meta.append(f'<span style="color:{MUTED};">{gt}</span>')
        tv = []
        nat = g.get("national_tv") or []
        if nat:
            tv.append(f'<span style="color:{CYAN};font-weight:600;">\U0001f4fa {", ".join(nat)}</span>')
        locals_ = []
        if g.get("away_tv"):
            locals_.append(f'{away_ab or "AWAY"} {g["away_tv"]}')
        if g.get("home_tv"):
            locals_.append(f'{home_ab or "HOME"} {g["home_tv"]}')
        if locals_:
            pre = "" if nat else "\U0001f4fa "
            tv.append(f'<span style="color:{MUTED};">{pre}{" · ".join(locals_)}</span>')
        meta += tv
        meta_html = ('<span style="color:' + MUTED + ';"> — </span>' + " · ".join(meta)) if meta else ""

        # Pitching matchup — the actual probables (colored if rostered: mine ACCENT, opp RED).
        my_sp  = {_ascii_lower(p["name"]) for p in item["mine"] if p.get("is_sp")}
        opp_sp = {_ascii_lower(p["name"]) for p in item["opp"] if p.get("is_sp")}
        def _prob(nm):
            if not nm:
                return f'<span style="color:{MUTED};">TBD</span>'
            a = _ascii_lower(nm)
            c, w = (ACCENT, "700") if a in my_sp else (RED, "700") if a in opp_sp else (MUTED, "500")
            return f'<span style="color:{c};font-weight:{w};">{nm}</span>'
        pitch_html = (
            f'<div style="margin:1px 0 3px;font-size:11px;">'
            f'<span style="color:{MUTED};">⚾ SP: </span>{_prob(g.get("away_prob"))}'
            f'<span style="color:{MUTED};"> vs </span>{_prob(g.get("home_prob"))}</div>'
        )

        cards.append(
            f'<div style="background:{SURFACE2};border:1px solid {BORDER};border-radius:8px;'
            f'padding:9px 13px;margin-bottom:8px;font-size:12px;">'
            f'<div style="margin-bottom:2px;">{matchup_html}{meta_html}</div>'
            f'{pitch_html}'
            f'<div style="margin:2px 0;">{_side(item["mine"], "You", ACCENT)}</div>'
            f'<div style="margin:2px 0;">{_side(item["opp"], "Opp", TEXT)}</div>'
            f'</div>'
        )
    fav_note = (" ★ Your team's games are pinned first." if any(it.get("fav") for it in ranked) else "")
    return (
        section_head("\U0001f4fa Today's MLB Games",
                     "Games worth tuning into — ranked by how much they overlap your matchup (your players "
                     "weighted heaviest). Counts hitters, confirmed starters (⚾), and relievers (save/hold "
                     "chances); only a starter who isn't pitching tonight is skipped. Hitters count even if "
                     "their real manager sits them." + fav_note)
        + "".join(cards)
        + '<div style="margin-bottom:24px;"></div>'
    )

# ── EMAIL BUILDER ─────────────────────────────────────────────────────────────

# ══════════════════════════════════════════════════════════════════════════════
# SHARED DERIVED-VALUE HELPERS — the single source for every reader of the
# snapshot: the digest (build_email), the dashboard (dashboard.build_context),
# and the Trade Lab (trade_lab.build_data). These used to be copy-pasted (or
# hand-mirrored from build_email's local closures) in each caller; any change
# here is automatically shared, so the three tools cannot drift apart.
# ══════════════════════════════════════════════════════════════════════════════

def prepare_scoring(pitchers, hitters):
    """The canonical scoring prelude. Runs the order-sensitive calibration sequence
    (the module globals each call populates are read by the scoring functions and
    by the next calls in the sequence — do NOT reorder), then builds the two
    qualified-YEAR percentile pools and the positional-scarcity scale on top of
    them. Returns (hit_pctile, pit_pctile)."""
    compute_ab_benchmarks(hitters)
    compute_pitcher_benchmarks(pitchers)
    compute_score_calibration(pitchers)          # re-anchor SP/RP score scale (after benchmarks)
    compute_league_averages(hitters, pitchers)   # league-avg reference points → _LG
    compute_xera_offset(pitchers)                # de-bias the pitcher buy/sell (ERA vs xERA) flag
    # League percentile pools (qualified YEAR pools per type). The pitcher pool spans
    # ALL starters+relievers: a traded arm's K/W/ERA/WHIP/SV+H is measured vs the
    # whole pitcher population, not just relievers.
    ab_pool_floor = (_AB_BENCH.get(YEAR) or _FULLTIME_AB[YEAR]) * 0.30
    hit_pool = [r for r in hitters  if int(_n(r.get("Dataset")) or 0) == YEAR and _n(r.get("AB")) >= ab_pool_floor]
    pit_pool = [r for r in pitchers if int(_n(r.get("Dataset")) or 0) == YEAR]
    hit_pctile = build_cat_percentiles(hit_pool, _FA_HIT_CATS)
    pit_pctile = build_cat_percentiles(pit_pool, _FA_RP_CATS)
    compute_position_scarcity(hitters, hit_pctile)   # positional-scarcity scale → _POS_SCARCITY (hitter _tval)
    return hit_pctile, pit_pctile

def build_recent_indexes(pitchers, hitters, recent_pitching, recent_hitting):
    """Per-window name→row indexes plus the best-available-recent cascade.
    Returns a dict with p7/p15/p30, h7/h15/h30 (FantasyPros short windows),
    rec_p/rec_h (pybaseball Baseball Ref recents), and best_recent_p/best_recent_h
    (30d > 15d > 7d > Baseball Ref — later dicts win in the merge). Baseball Ref
    pitcher rows get the K/IP + IP_per_G fields pitcher_score expects."""
    def _idx(rows, ds):
        return {r["PlayerName"]: r for r in rows
                if int(r.get("Dataset", 0) or 0) == ds and r.get("PlayerName")}
    rec_h = {r["PlayerName"]: r for r in recent_hitting  if r.get("PlayerName")}
    rec_p = {r["PlayerName"]: r for r in recent_pitching if r.get("PlayerName")}
    rec_p_fp = {}
    for name, r in rec_p.items():
        ip = _n(r.get("IP")); k = _n(r.get("K")); g = _n(r.get("G"))
        rec_p_fp[name] = {**r, "K/IP": round(k / ip, 3) if ip > 0 else 0,
                          "IP_per_G": round(ip / g, 2) if g > 0 else 0}
    p7, p15, p30 = _idx(pitchers, 7), _idx(pitchers, 15), _idx(pitchers, 30)
    h7, h15, h30 = _idx(hitters, 7),  _idx(hitters, 15),  _idx(hitters, 30)
    return dict(
        rec_p=rec_p, rec_h=rec_h,
        p7=p7, p15=p15, p30=p30, h7=h7, h15=h15, h30=h30,
        best_recent_p={**rec_p_fp, **p7, **p15, **p30},
        best_recent_h={**rec_h,    **h7, **h15, **h30},
    )

def claimed_today(transactions, today_str):
    """Names claimed off waivers today. Players claimed today may not yet have
    FantasyTeam set in the ESPN roster API, so today's transactions are a second
    source of truth — but only exclude a player if their MOST RECENT transaction
    today is FA ADDED (handles add-then-drop-same-day correctly)."""
    todays = [t for t in (transactions or [])
              if t.get("TransactionDate", "").startswith(today_str)]
    latest = {}
    for t in sorted(todays, key=lambda t: t.get("TransactionDate", "")):
        latest[t["PlayerName"]] = t["TransactionType"]
    return {name for name, txn_type in latest.items() if txn_type == "FA ADDED"}

def parse_matchup_window(snap, today=None):
    """The matchup period window, from the snapshot's ESPN-derived dates (handles
    2-week All-Star matchups) with a calendar-week fallback when the snapshot
    predates those fields. Returns a dict:
      matchup_start_date / matchup_end_date (date), matchup_period_days (int),
      week_end_str (YYYY-MM-DD), days_elapsed (0 on the start day),
      matchup_game_days / game_days_elapsed (real MLB game days — excludes dark
      days like the All-Star break so projections don't accrue on them),
      is_sunday (today is on/past the LAST day — not always a calendar Sunday),
      is_monday (today is the first day)."""
    today = today or datetime.now().date()
    mstart_raw = snap.get("matchup_start_date") or ""
    mend_raw   = snap.get("matchup_end_date")   or ""
    mdays      = snap.get("matchup_period_days") or 0
    if mend_raw:
        matchup_end_date   = datetime.strptime(mend_raw,   "%Y-%m-%d").date()
        matchup_start_date = datetime.strptime(mstart_raw, "%Y-%m-%d").date() if mstart_raw else (today - timedelta(days=today.weekday()))
        matchup_period_days = int(mdays) if mdays else max(7, (matchup_end_date - matchup_start_date).days + 1)
        week_end_str = mend_raw
    else:
        matchup_start_date  = today - timedelta(days=today.weekday())
        matchup_end_date    = today + timedelta(days=6 - today.weekday())
        matchup_period_days = 7
        week_end_str        = matchup_end_date.strftime("%Y-%m-%d")
    days_elapsed = max(0, (today - matchup_start_date).days)
    mgdays    = snap.get("matchup_game_days")
    mgdays_el = snap.get("matchup_game_days_elapsed")
    return dict(
        matchup_start_date=matchup_start_date, matchup_end_date=matchup_end_date,
        matchup_period_days=matchup_period_days, week_end_str=week_end_str,
        days_elapsed=days_elapsed,
        matchup_game_days=int(mgdays) if mgdays is not None else matchup_period_days,
        game_days_elapsed=int(mgdays_el) if mgdays_el is not None else days_elapsed,
        is_sunday=today >= matchup_end_date,
        is_monday=today == matchup_start_date,
    )

def compute_pit_proj(pitchers, my_team, opp_team, today_str, week_end_str):
    """Pitcher counting-stat projections (K, QS, W) for the REST of the matchup,
    from each side's actual remaining confirmed/projected starts — not weekly
    averages. Returns {"QS"|"K"|"W": {"my": float, "opp": float}} for
    build_category_pulse / classify_categories (remaining_proj=...)."""
    my_key  = " ".join((my_team or "").split())
    opp_key = " ".join((opp_team or "").split())
    def remaining_starters(team_key):
        return [r for r in pitchers
                if int(r.get("Dataset", 0) or 0) == YEAR
                and " ".join((r.get("FantasyTeam") or "").split()) == team_key
                and r.get("PSP_Date", "") not in ("1999-01-01", "", None)
                and today_str <= r.get("PSP_Date", "") <= week_end_str
                and _is_sp(r)]
    def proj_qs(starters):
        return sum((qs_probability(r) or 0) / 100 for r in starters)
    def proj_k(starters):
        total = 0
        for r in starters:
            gs = _n(r.get("GS")); k = _n(r.get("K")); ip_g = _n(r.get("IP_per_G")); kip = _n(r.get("K/IP") or r.get("KIP"))
            total += (k / gs) if gs > 0 else (ip_g * kip if ip_g > 0 and kip > 0 else 5)
        return total
    def proj_w(starters):
        total = 0
        for r in starters:
            gs = _n(r.get("GS")); w = _n(r.get("ESPN_W") or r.get("W"))
            total += (w / gs) if gs > 0 else 0.12
        return total
    my_ss, opp_ss = remaining_starters(my_key), remaining_starters(opp_key)
    return {
        "QS": {"my": proj_qs(my_ss), "opp": proj_qs(opp_ss)},
        "K":  {"my": proj_k(my_ss),  "opp": proj_k(opp_ss)},
        "W":  {"my": proj_w(my_ss),  "opp": proj_w(opp_ss)},
    }

def compute_week_finishes(roto, my_team, current_week_num):
    """Per-completed-week roto finishes. Returns (wk_ranks, wk_pts, roto_week_results):
    my weekly roto rank + points per completed week, and {week: {team: 'W'|'L'}}
    where W = that week's roto winner (feeds make_sparkline's weekly_results)."""
    my_key = " ".join((my_team or "").split())
    week_scores = {}
    for row in roto:
        t = " ".join((row.get("Team") or "").split())
        wk = int(row.get("Week", 0))
        week_scores.setdefault(wk, {})[t] = float(row.get("Roto_Score") or 0)
    wk_ranks, wk_pts, roto_week_results = [], [], {}
    for wk in sorted(week_scores):
        if wk >= current_week_num:   # skip current (partial) week
            continue
        scores = week_scores[wk]
        if my_key not in scores:
            continue
        ranked = sorted(scores.items(), key=lambda x: -x[1])
        roto_week_results[wk] = {t: ('W' if i == 0 else 'L') for i, (t, _) in enumerate(ranked)}
        my_rank = next((i + 1 for i, (t, _) in enumerate(ranked) if t == my_key), None)
        if my_rank:
            wk_ranks.append(my_rank)
            wk_pts.append(scores[my_key])
    return wk_ranks, wk_pts, roto_week_results

def roster_hot_cold_counts(pitchers, hitters, my_team, rec_h, rec_p, p15):
    """Hot/cold counts across my ENTIRE roster for the Roster KPI — hitters by
    7-day OPS vs season (±.015), pitchers by 15-day ERA vs season (±.40, ≥3
    recent IP, rec_p fallback). The two thresholds differ by design (OPS vs ERA
    scale) and match build_hot_cold_section / build_pitcher_hot_cold_section.
    Returns (n_hot, n_cold)."""
    my_key = " ".join((my_team or "").split())
    n_hot = n_cold = 0
    for r in hitters:
        if (" ".join((r.get("FantasyTeam") or "").split()) == my_key
                and int(r.get("Dataset", 0)) == YEAR
                and float(r.get("OPS") or 0) > 0):
            s_ops = float(r.get("OPS") or 0)
            rh = rec_h.get(r.get("PlayerName", ""), {})
            r_ops = float(rh.get("OPS") or 0) if rh else 0
            if s_ops > 0 and r_ops > 0:
                d = r_ops - s_ops
                if d >= 0.015:    n_hot  += 1
                elif d <= -0.015: n_cold += 1
    for r in pitchers:
        if (" ".join((r.get("FantasyTeam") or "").split()) == my_key
                and int(r.get("Dataset", 0) or 0) == YEAR
                and _n(r.get("ERA")) > 0):
            s_era = _n(r.get("ERA"))
            rp    = p15.get(r.get("PlayerName", "")) or rec_p.get(r.get("PlayerName", ""), {})
            r_era = _n(rp.get("ERA")) if rp else 0
            r_ip  = _n(rp.get("IP"))  if rp else 0
            if s_era > 0 and r_era > 0 and r_ip >= 3:
                d = s_era - r_era
                if d >= 0.40:    n_hot  += 1
                elif d <= -0.40: n_cold += 1
    return n_hot, n_cold

def build_coverage_footer(snap):
    """One compact digest-footer line summarizing enrichment coverage — quiet/muted when
    healthy, colored + prefixed when a source degraded, so you know when to trust the numbers
    less (the freshness badge answers 'how fresh?'; this answers 'how complete?'). Reuses
    data_coverage.py so the thresholds match the standalone report + CI. Returns '' on any
    failure or if data_coverage is unavailable (never breaks the digest)."""
    try:
        import data_coverage as _dc
        rep = _dc.coverage_report(snap)
        ws = _dc.worst_status(rep)
    except Exception:
        return ""
    try:
        fracs = [c["frac"] for grp in ("pitcher_savant", "hitter_savant")
                 for c in rep.get(grp, {}).get("fields", {}).values() if c.get("status") != "n/a"]
        floor = int(min(fracs) * 100) if fracs else 0
        if ws == "OK":
            return (f'<div style="margin-top:6px;color:{MUTED};font-size:11px;">'
                    f'Data coverage OK &middot; Statcast &ge;{floor}% &middot; recent windows full</div>')
        bad = []
        if rep["pitcher_savant"]["status"] != "OK" or rep["hitter_savant"]["status"] != "OK":
            bad.append(f"Statcast {floor}%")
        for w, c in rep["recent_windows"].items():
            if c["status"] != "OK":
                bad.append(f"{w}d window thin")
        if rep["freshness"]["status"] != "OK":
            bad.append("snapshot stale")
        detail = "; ".join(bad) or "some fields"
        if ws == "WARN":
            return (f'<div style="margin-top:6px;color:{YELLOW};font-size:11px;font-weight:600;">'
                    f'&#9888; Data partially degraded &mdash; {detail}</div>')
        return (f'<div style="margin-top:6px;color:{RED};font-size:11px;font-weight:700;">'
                f'&#9888; Data DEGRADED this run &mdash; {detail}; scores may be off</div>')
    except Exception:
        return ""

def build_email(snap, override_team=None):
    my_team       = override_team if override_team else snap.get("my_team", MY_TEAM)
    pitchers      = snap.get("pitchers", [])
    hitters       = snap.get("hitters", [])
    roto          = snap.get("roto", [])
    standings     = snap.get("standings", [])
    refreshed_iso = snap.get("refreshed_at", "")
    # Convert to ET before slicing — a raw UTC slice shows "tomorrow" for any fetch after 8 PM ET.
    try:
        _rdt = datetime.fromisoformat(refreshed_iso)
        if _rdt.tzinfo is not None and _ET is not None:
            _rdt = _rdt.astimezone(_ET)
        refreshed = _rdt.strftime("%Y-%m-%d")
    except Exception:
        refreshed = refreshed_iso[:10]
    refreshed_clock = _fmt_refresh_time(refreshed_iso)  # "6:32 AM ET" or "" — surfaced next to the freshness badge
    all_matchups  = snap.get("all_matchups", {})
    matchup       = all_matchups.get(" ".join(my_team.split())) or (snap.get("current_matchup", {}) if not override_team else {})
    recent_hitting  = snap.get("recent_hitting",  [])
    recent_pitching = snap.get("recent_pitching", [])
    weekly_results  = snap.get("weekly_results",  {})
    prev_matchup    = snap.get("all_prev_matchups", {}).get(" ".join(my_team.split())) or (snap.get("prev_matchup", {}) if not override_team else {})
    # Scoring calibration + percentile pools + recent-form indexes — the SHARED
    # helpers (see the block above build_email) so the dashboard and Trade Lab
    # derive the exact same values.
    hit_pctile, pit_pctile = prepare_scoring(pitchers, hitters)
    idx = build_recent_indexes(pitchers, hitters, recent_pitching, recent_hitting)
    rec_p, rec_h = idx["rec_p"], idx["rec_h"]
    p15 = idx["p15"]
    best_recent_p, best_recent_h = idx["best_recent_p"], idx["best_recent_h"]

    today_str = datetime.now().strftime("%Y-%m-%d")
    claimed = claimed_today(snap.get("transactions", []), today_str)

    fa_sp     = fa_starters(pitchers, claimed, idx_recent=best_recent_p)
    fa_rp     = fa_relievers(pitchers, claimed)
    fa_hit    = fa_hitters(hitters, claimed, idx_recent=best_recent_h)
    luck      = luck_standings(roto, standings)
    team_logos = {" ".join(s["team_name"].split()): s.get("logo_url", "") for s in standings}
    cats, n   = category_ranks(roto, my_team)
    current_week_num = matchup.get("week") or max((int(r.get("Week", 0)) for r in roto), default=0)
    weekly_avgs  = compute_weekly_avgs(roto, current_week_num)
    weekly_std   = compute_weekly_std(roto, current_week_num)
    # Matchup window + pitcher K/QS/W projections — shared helpers (see above build_email).
    _today = datetime.now().date()
    win = parse_matchup_window(snap, _today)
    matchup_start_date  = win["matchup_start_date"]
    matchup_end_date    = win["matchup_end_date"]
    matchup_period_days = win["matchup_period_days"]
    week_end_str        = win["week_end_str"]
    days_elapsed        = win["days_elapsed"]
    matchup_game_days   = win["matchup_game_days"]
    game_days_elapsed   = win["game_days_elapsed"]
    is_sunday           = win["is_sunday"]
    is_monday           = win["is_monday"]
    week_roto = [r for r in roto if int(r.get("Week", 0)) == current_week_num]
    week_cats, week_n = category_ranks(week_roto, my_team)

    pit_proj = compute_pit_proj(pitchers, my_team, matchup.get("opp_team", "") if matchup else "",
                                today_str, week_end_str)

    # Category classification (used by the pickup steering AND the FA "Cats" column).
    # Computed here (before the FA tables) so need_cats is available to them.
    category_classification = classify_categories(
        matchup, weekly_avgs=weekly_avgs, days_elapsed=days_elapsed, remaining_proj=pit_proj,
        matchup_days=matchup_period_days,
        game_days_elapsed=game_days_elapsed, matchup_game_days=matchup_game_days,
    )
    # need_cats = the categories I'm losing OR that are a tossup — highlighted in FA "Cats".
    _losing_now = {c["cat"] for c in (matchup.get("categories", []) if matchup else []) if c.get("result") == "L"}
    need_cats = _losing_now | {c for c, (res, tier) in category_classification.items() if tier == "tossup"}
    # hit_pctile / pit_pctile / positional scarcity already set by prepare_scoring above.
    # The RP-only pool is digest-specific (FA Relievers "Cats" column rates a reliever
    # vs other RELIEVERS, not the whole pitcher pool the trade currency uses).
    _rp_pool  = [r for r in pitchers if int(_n(r.get("Dataset")) or 0) == YEAR and not _is_sp(r)]
    rp_pctile = build_cat_percentiles(_rp_pool, _FA_RP_CATS)
    # Current Matchup subtitle uses the SAME stored Roto_Score the Week N Roto
    # Rankings table renders (ESPN's method, which splits points on ties) so the
    # two panels agree. A pseudo rank-sum (n - rank + 1) would over-count ties
    # (ordinal ranks give a tied leader the full points), diverging by a few pts.
    _my_wrow = next(
        (r for r in week_roto
         if " ".join((r.get("Team") or "").split()) == " ".join(my_team.split())),
        None,
    )
    _my_week_roto_raw = float(_my_wrow.get("Roto_Score") or 0) if _my_wrow else 0.0
    my_week_roto_pts = (int(_my_week_roto_raw) if _my_week_roto_raw == int(_my_week_roto_raw)
                        else round(_my_week_roto_raw, 1))
    my_season_pseudo_roto = sum(n - rank + 1 for rank in cats.values() if rank is not None)
    alerts    = roster_alerts(pitchers, hitters, my_team)
    starts    = my_upcoming_starts(pitchers, my_team)
    pos_data  = positional_breakdown(pitchers, hitters, my_team, best_recent_p, best_recent_h)

    # Grade real pending trade offers ONCE (my team only — snapshot stores only my trades);
    # the section render, the Briefing "Act today" list, and the Week-at-a-Glance headline
    # all read from this so they can't disagree. Time-sensitive (offers expire), so it feeds
    # the highest-up surfaces, not just the dedicated section.
    graded_pending = _grade_pending_trades(
        snap.get("pending_trades") or [], pitchers, hitters, roto, my_team,
        best_recent_p, best_recent_h, pos_data, hit_pctile, pit_pctile, today_str=today_str
    ) if not override_team else []
    incoming_pending = [g for g in graded_pending if g["incoming"]]

    my_row = next((r for r in luck if " ".join((r.get("team") or "").split()) == " ".join(my_team.split())), {})
    today  = datetime.now().strftime("%A, %B %d, %Y")
    _digest_label = "Matchup Lookahead" if is_sunday else "Daily Fantasy Digest"

    # ── Derived KPI values ─────────────────────────────────────────────────────
    my_logo_url = team_logos.get(" ".join(my_team.split()), "")
    my_logo_html = fantasy_logo(my_logo_url, size=36, team_name=my_team)

    # Per-week roto finishes (sparkline + KPI stats) — shared helper (see above build_email).
    my_key = " ".join(my_team.split())
    wk_ranks, wk_pts, roto_week_results = compute_week_finishes(roto, my_team, current_week_num)

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

    # Roster-wide hot/cold counts for the Roster KPI — shared helper (see above build_email).
    n_hot, n_cold = roster_hot_cold_counts(pitchers, hitters, my_team, rec_h, rec_p, p15)
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
  {kpi_cell("Starts Next Matchup" if is_sunday else "Starts This Matchup", sum(1 for s in starts if s.get("PSP_Date","") > week_end_str) if is_sunday else sum(1 for s in starts if s.get("PSP_Date","") <= week_end_str))}
</tr>
<tr style="border-top:1px solid {BORDER};">
  {kpi_cell_sm("Roto Trend", spark_cell_val, font_size="inherit", font_weight="normal")}
  {kpi_cell_sm("Standing", f'#{my_row.get("standing","—")}{matchup_sub}')}
  {kpi_cell_sm("Roto Rank", f'#{my_row.get("roto_rank","—")}{roto_rank_sub}')}
  {kpi_cell_sm("Luck", luck_str, color=luck_color)}
</tr>
</table>"""

    # ── Alerts ─────────────────────────────────────────────────────────────────
    # (Incoming trade offers surface HIGHER — in Week at a Glance + the Briefing "Act today"
    # list — since they're time-sensitive; they don't clutter this roster-injury box.)
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
                _n_starts_s = _starts_this_week(r, today_str, week_end_str)
                start_badges = []
                if _n_starts_s >= 2:
                    start_badges.append(two_start_badge(f"{_n_starts_s} starts this matchup week"))
                if qs_fires_s:
                    start_badges.append(qs_badge(_pjs_ip, _pjs_er, r))
                if k_fires_s:
                    start_badges.append(k5_badge(_pjs_k, r))
                start_badges.append(blowup_badge(r, p15r.get("ERA")))
                start_badges.append(pitcher_regression_badge(r))
                start_badge = "".join(start_badges)
                proj_line_s = _proj_line_html(r)
                _mus_bd = (_pitcher_score_breakdown(r, best_recent_p)
                           + _sp_badge_context(r, qs_fires_s, k_fires_s, _n_starts_s, p15r.get("ERA")))
                _cell, _bdrow = score_reveal(
                    _score_p(r, best_recent_p), _mus_bd,
                    _bd_uid("mus", name), 8)
                rows += (
                    f'<tr style="{bg}">'
                    f'<td style="{_tds}font-weight:600;">{team_logo(r.get("Team"))}{name}{inj_tag(r)}{start_badge}</td>'
                    f'<td style="{_tdc}">{proj_line_s}</td>'
                    f'<td style="{_tdc}">{opp_logo(ha)}{ha}'
                    f'{"&nbsp;<span style=\"color:#888;font-size:11px\">(proj.)</span>" if r.get("PSP_Projected") else ""}'
                    f'{_opp_ops_sub(r)}</td>'
                    f'<td style="{_tdc}">{qsp_str}{_qs_sub(r)}</td>'
                    f'<td style="{_tdc}">{v(r.get("ERA"), 2)}</td>'
                    + hot_cold_cell(r.get("ERA"), p15r.get("ERA"), lower_better=True, dec=2, no_data_title="No 15-day stats — player may not have pitched recently", td_style=_tdc) +
                    f'<td style="{_tdc}">{kpct_s_cell}{_whiff_sub(r)}</td>'
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
                f'<td style="{TD_S}font-weight:600;">{team_logo(r.get("Team"))}{r.get("PlayerName","")}{inj_tag(r)}{ds_badge}{pitcher_regression_badge(r)}</td>'
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
                    pickup_badges.append(qs_badge(_pj_ip, _pj_er, r))
                if k_fires:
                    pickup_badges.append(k5_badge(_pj_k, r))
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
                pickup_badges.append(blowup_badge(r, p15r.get("ERA")))
                pickup_badges.append(pitcher_regression_badge(r))
                pickup_badge = "".join(pickup_badges)
                # Two-start flag always shows — a 2-start FA is a top streaming target
                _n_starts_fa = _starts_this_week(r, today_str, week_end_str)
                two_start_html = two_start_badge(f"{_n_starts_fa} starts this matchup week") if _n_starts_fa >= 2 else ""

                _kpct_val = _n(r.get("Kpct_P"))
                _kpct_top = _kpct_val > 0 and _kpct_val in _top3_kpct_fa
                kpct_cell = (
                    f'<span style="color:{YELLOW};font-weight:700;">{_kpct_val*100:.1f}%</span>'
                    if _kpct_top and _kpct_val > 0
                    else (f"{_kpct_val*100:.1f}%" if _kpct_val > 0 else f'<span style="color:{MUTED}">—</span>')
                )
                proj_line_str = _proj_line_html(r)
                _fasp_bd = (_pitcher_score_breakdown(r, best_recent_p)
                            + _sp_badge_context(r, qs_fires, k_fires, _n_starts_fa, p15r.get("ERA")))
                _cell, _bdrow = score_reveal(
                    r["_score"], _fasp_bd,
                    _bd_uid("fasp", r.get("PlayerName", "")), 8)
                rows += (
                    f'<tr style="{bg}">'
                    f'<td style="{name_border}{_tds}font-weight:600;">{team_logo(r.get("Team"))}{r.get("PlayerName","")}{inj_tag(r)}{two_start_html}{pickup_badge}</td>'
                    f'<td style="{_tdc}">{proj_line_str}</td>'
                    f'<td style="{_tdc}">{opp_logo(ha)}{ha}'
                    f'{"&nbsp;<span style=\"color:#888;font-size:11px\">(proj.)</span>" if r.get("PSP_Projected") else ""}'
                    f'{_opp_ops_sub(r)}</td>'
                    f'<td style="{_tdc}">{qsp_str}{_qs_sub(r)}</td>'
                    f'<td style="{_tdc}">{v(r.get("ERA"), 2)}</td>'
                    + hot_cold_cell(r.get("ERA"), p15r.get("ERA"), lower_better=True, dec=2, no_data_title="No 15-day stats — player may not have pitched recently", td_style=_tdc) +
                    f'<td style="{_tdc}">{kpct_cell}{_whiff_sub(r)}</td>'
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
        table = f'<p style="color:{MUTED};font-style:italic;margin-bottom:24px;">No FA starters (score {_FA_SP_MIN_SCORE}+) with upcoming starts.</p>'

    fa_sp_section = section_head("FA Pickup — Starting Pitchers", f"Free agents with upcoming starts · score {_FA_SP_MIN_SCORE}+ only · sorted by SP score") + table

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
                f'<td style="{TD_S}font-weight:600;">{team_logo(r.get("Team"))}{r.get("PlayerName","")}{inj_tag(r)}{ds_badge}{pitcher_regression_badge(r)}</td>'
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
                r["_score"], _hitter_score_breakdown(r, best_recent_h, hit_pctile),
                _bd_uid("fahit", r.get("PlayerName", "")), 11)
            rows += (
                f'<tr style="{bg}">'
                f'<td style="{TD_S}font-weight:600;">{team_logo(r.get("Team"))}{r.get("PlayerName","")}{inj_tag(r)}{hitter_badges(r, hit_pctile)}</td>'
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
    # Suppress both this section and the league roto table on Monday before stats
    # accumulate — when all teams share an equal Roto_Score the ranks are arbitrary.
    _all_roto_tied = len(set(float(r.get("Roto_Score") or 0) for r in week_roto)) <= 1
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
    week_cat_section = ("" if _all_roto_tied else
        section_head("Current Matchup", f"Matchup {current_week_num} · {my_week_roto_pts} roto pts") +
        f'<table style="width:100%;border-collapse:collapse;background:{SURFACE};border-radius:6px;margin-bottom:24px;overflow:hidden;">'
        f'<tr>{week_cat_cells}</tr></table>'
    )

    # ── This week's full league roto rankings (live, all 12 teams) ────────────
    week_roto_rankings_section = ""
    if week_roto and not _all_roto_tied:
        _wrt_th  = TH_S.replace("padding:8px 10px", "padding:3px 5px").replace("font-size:10px", "font-size:9px")
        _wrt_tdc = TDC.replace("padding:7px 10px", "padding:3px 5px").replace("font-size:13px", "font-size:10px")
        _wrt_tds = TD_S.replace("padding:7px 10px", "padding:3px 5px").replace("font-size:13px", "font-size:10px")
        _wrt_key = " ".join(my_team.split())

        _wrt_leaders: dict = {}
        for _wrt_cat, _ in CAT_LABELS:
            _pt_field = f"{_wrt_cat}_Points"
            _best_pts = max((float(r.get(_pt_field) or 0) for r in week_roto), default=0)
            if _best_pts > 0:
                for r in week_roto:
                    if float(r.get(_pt_field) or 0) == _best_pts:
                        _wrt_leaders.setdefault(r["Team"], []).append(_wrt_cat)

        _ranked_wrt = sorted(week_roto, key=lambda r: -float(r.get("Roto_Score") or 0))
        _wrt_n = len(_ranked_wrt)

        def _wrt_fmt(val, cat):
            dec = 3 if cat == "OPS" else (2 if cat in {"ERA", "WHIP"} else 0)
            try:
                f = f"{float(val):.{dec}f}"
                if dec > 0 and float(val) < 1.0:
                    f = f.lstrip("0") or "0"
                return f
            except (TypeError, ValueError):
                return "—"

        _wrt_rows = ""
        for _wrt_rank, r in enumerate(_ranked_wrt, 1):
            _wrt_team  = r.get("Team", "")
            _wrt_score = float(r.get("Roto_Score") or 0)
            _wrt_led   = _wrt_leaders.get(_wrt_team, [])
            _wrt_is_my = " ".join(_wrt_team.split()) == _wrt_key

            if _wrt_rank <= 3:
                _wrt_row_bg = "background:rgba(34,197,94,0.07);"
            elif _wrt_rank >= _wrt_n - 2:
                _wrt_row_bg = "background:rgba(239,68,68,0.07);"
            else:
                _wrt_row_bg = ""

            _wrt_logo       = fantasy_logo(team_logos.get(" ".join(_wrt_team.split()), ""), 16, _wrt_team)
            _wrt_rank_color = GREEN if _wrt_rank <= 3 else (RED if _wrt_rank >= _wrt_n - 2 else MUTED)
            _wrt_led_pills  = "".join(
                f'<span style="background:{ACCENT}22;color:{ACCENT};padding:1px 4px;'
                f'border-radius:10px;font-size:8px;font-weight:700;margin-left:2px;">'
                f'{_CAT_DISPLAY.get(c, c)}</span>'
                for c in _wrt_led
            )
            _wrt_stat_cells = ""
            for _wrt_c, _ in CAT_LABELS:
                _wrt_pts     = float(r.get(f"{_wrt_c}_Points") or 0)
                _wrt_val_str = _wrt_fmt(r.get(_wrt_c, 0), _wrt_c)
                if _wrt_val_str == "—":
                    _wrt_color, _wrt_badge = MUTED, False
                elif _wrt_pts == _wrt_n:
                    _wrt_color, _wrt_badge = GREEN, True
                elif _wrt_pts == _wrt_n - 1:
                    _wrt_color, _wrt_badge = "#86efac", False
                elif _wrt_pts == 1:
                    _wrt_color, _wrt_badge = RED, True
                elif _wrt_pts == 2:
                    _wrt_color, _wrt_badge = YELLOW, False
                else:
                    _wrt_color, _wrt_badge = MUTED, False
                _wrt_inner = (
                    f'<span style="border:1px solid {_wrt_color};border-radius:3px;padding:2px 6px;">{_wrt_val_str}</span>'
                    if _wrt_badge else _wrt_val_str
                )
                _wrt_stat_cells += f'<td style="{_wrt_tdc}color:{_wrt_color};">{_wrt_inner}</td>'

            _wrt_rows += (
                f'<tr style="{_wrt_row_bg}">'
                f'<td style="{_wrt_tdc}color:{_wrt_rank_color};font-weight:700;width:24px;">{_wrt_rank}</td>'
                f'<td style="{_wrt_tds}font-weight:{"800" if _wrt_is_my else "600"};'
                f'color:{ACCENT if _wrt_is_my else TEXT};white-space:nowrap;">'
                f'{_wrt_logo}{_wrt_team}'
                + (f'<span style="margin-left:4px;">{_wrt_led_pills}</span>' if _wrt_led_pills else "")
                + f'</td>'
                f'<td style="{_wrt_tdc}font-weight:700;">{_wrt_score:.1f}</td>'
                + _wrt_stat_cells +
                f'</tr>'
            )

        _wrt_stat_headers = "".join(
            f'<th style="{_wrt_th}text-align:center;">{_CAT_DISPLAY.get(c, c)}</th>'
            for c, _ in CAT_LABELS
        )
        _wrt_header_row = (
            f'<th style="{_wrt_th}text-align:center;width:24px;">#</th>'
            f'<th style="{_wrt_th}">Team</th>'
            f'<th style="{_wrt_th}text-align:center;">Pts</th>'
            + _wrt_stat_headers
        )
        _wrt_table = (
            f'<div style="overflow-x:auto;-webkit-overflow-scrolling:touch;">'
            f'<table style="width:100%;border-collapse:collapse;font-size:10px;">'
            f'<thead><tr>{_wrt_header_row}</tr></thead>'
            f'<tbody>{_wrt_rows}</tbody></table></div>'
        )
        week_roto_rankings_section = (
            '<div style="margin-bottom:24px;">' +
            section_head(f"Matchup {current_week_num} Roto Rankings",
                         f"Live standings \xb7 bright green = #1 \xb7 light green = #2 \xb7 amber = #11 \xb7 red = #12") +
            _wrt_table +
            '</div>'
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

        def _pb_reveal(pl, tag, small=False):
            bd = (_pitcher_score_breakdown(pl, best_recent_p) if _is_pit_pos
                  else _hitter_score_breakdown(pl, best_recent_h, hit_pctile))
            return score_reveal(pl["_pscore"], bd, _bd_uid(tag, pl.get("PlayerName", "")), 4, small=small)

        # Lead with my STARTER (the rank-defining anchor) — that's what determines how good
        # I am here and the bar the FA arrow is judged against, so showing him kills the old
        # "worse body listed, better FA shows no arrow" contradiction. The weakest eligible
        # body is the DROP candidate; show it as an explicit muted sub-line, but only when
        # it's a different player (a 1-deep position has nothing to drop).
        _start_bdrow = _worst_bdrow = _fa_bdrow = ""
        starter = p.get("starter")
        worst   = p["worst_player"]
        if starter:
            _start_badge, _start_bdrow = _pb_reveal(starter, "poss")
            player_cell = (
                f'{team_logo(starter.get("Team"), 16)}'
                f'<span style="font-weight:600;">{starter["PlayerName"]}</span>'
                f'{inj_tag(starter)}'
                f'{pitcher_regression_badge(starter) if _is_pit_pos else hitter_badges(starter, hit_pctile)}'
                f' {_start_badge}'
                f'{pos_stat_line(starter, p["pos"])}'
            )
            if worst and worst.get("PlayerName") != starter.get("PlayerName"):
                _worst_badge, _worst_bdrow = _pb_reveal(worst, "posw", small=True)
                player_cell += (
                    f'<div style="color:{MUTED};font-size:10px;margin-top:2px;">'
                    f'drop&nbsp;candidate: {team_logo(worst.get("Team"), 13)}'
                    f'{worst["PlayerName"]} {_worst_badge}</div>'
                )
        else:
            player_cell = f'<span style="color:{RED};font-weight:600;">EMPTY</span>'

        top_fa = p["top_fa"][0] if p["top_fa"] else None
        fa_score = top_fa["_pscore"] if top_fa else 0
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
        # The "↑ upgrade" flag compares the best FA against my STARTER quality at this
        # position (my_avg = top-K starter avg), NOT my weakest eligible body. That body is
        # often a multi-eligible backup (e.g. Caratini, a C carrying 1B
        # eligibility) whose primary weakness belongs to another position, so beating him
        # painted a false "upgrade" where my real starter (Olson 83) is strong. Judge
        # against what I actually run out there -- same starters-not-scraps theme as the
        # top-K rank and the dashboard need-gate.
        upgrade = top_fa and fa_score > p["my_avg"] + upgrade_thresh
        if top_fa:
            _fa_badge, _fa_bdrow = _pb_reveal(top_fa, "posfa")
            fa_cell = (
                f'{team_logo(top_fa.get("Team"), 16)}'
                f'<span style="{"font-weight:600;" if upgrade else ""}'
                f'color:{GREEN if upgrade else MUTED};">'
                f'{top_fa["PlayerName"]}</span>'
                f'{pitcher_regression_badge(top_fa) if _is_pit_pos else hitter_badges(top_fa, hit_pctile)}'
                f' {_fa_badge}'
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
            f'{_start_bdrow}{_worst_bdrow}{_fa_bdrow}'
        )

    pos_section = (
        section_head("Positional Breakdown", "Your depth at each position vs. the rest of the league") +
        f'<div style="overflow-x:auto;-webkit-overflow-scrolling:touch;margin-bottom:24px;">'
        f'<table style="width:100%;border-collapse:collapse;font-size:13px;">'
        f'<thead><tr>'
        f'<th style="{TH_S}text-align:center;">Pos</th>'
        f'<th style="{TH_S}">My Starter <span style="color:{MUTED};font-size:9px;">/ drop candidate</span></th>'
        f'<th style="{TH_S}text-align:center;">Strength</th>'
        f'<th style="{TH_S}">Best FA Available &nbsp;<span style="color:{GREEN};font-size:9px;">&#8593; = beats my starter</span></th>'
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
                    + '<span style="color:' + TEXT + ';font-weight:600;">'
                    + ", ".join(t.get("PlayerName", "") for t in _two)
                    + '</span>'
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
            section_head("Opponent This Matchup", f"{_logo_html}Scouting {_opp_name} — starts, hot bats, roto strengths &amp; wire activity")
            + f'<div style="background:{SURFACE2};border:1px solid {BORDER};border-radius:8px;'
              f'padding:10px 14px;margin-bottom:24px;font-size:12px;">{"".join(_lines)}</div>'
        )
    # Today's MLB games that most overlap the matchup (the games worth tuning into),
    # plus a one-line "tune in" teaser for the Briefing. Defensive: any failure just
    # drops both (section '' + teaser '').
    todays_games_section = ""
    _tune_in = ""
    try:
        _tg_list = snap.get("todays_games") or []
        # Best-available stat row per player, keyed loosely (ESPN roster names vs
        # FantasyPros) so each involved player carries the SAME tactical badges as the rest
        # of the digest. Prefer the season YEAR row but fall back 30 -> 15 -> 7 (some arms —
        # e.g. Hunter Brown — appear only in the short-range FP views this snapshot); a
        # thinner row just means a regression/IP-gated badge quietly doesn't fire.
        _tg_hit_rows, _tg_pit_rows, _tg_recent_era = {}, {}, {}
        for _ds in (7, 15, 30, YEAR):   # ascending preference — YEAR overwrites last
            for _r in hitters:
                if int(_n(_r.get("Dataset")) or 0) == _ds and _r.get("PlayerName"):
                    _tg_hit_rows[_badge_name_key(_r["PlayerName"])] = _r
            for _r in pitchers:
                if int(_n(_r.get("Dataset")) or 0) == _ds and _r.get("PlayerName"):
                    _tg_pit_rows[_badge_name_key(_r["PlayerName"])] = _r
        for _nm, _r in {**rec_p, **p15}.items():   # p15 wins; rec_p (Baseball Ref L15) fallback
            if _r.get("ERA") is not None:
                _tg_recent_era[_badge_name_key(_nm)] = _r.get("ERA")
        todays_games_section = build_todays_games_section(
            _tg_list, my_team, _opp_name,
            hit_rows=_tg_hit_rows, pit_rows=_tg_pit_rows,
            recent_era=_tg_recent_era, hit_pctile=hit_pctile)
        # Teaser names the true highest-OVERLAP game (pin_favorite=False), not the pinned favorite.
        _ranked_tg = _rank_todays_games(_tg_list, " ".join(my_team.split()), " ".join(_opp_name.split()), pin_favorite=False)
        if _ranked_tg:
            _top = _ranked_tg[0]
            _g0 = _top["g"]
            _aw = _FULLNAME_TO_ABBREV.get(_g0.get("away_name", ""), _g0.get("away_name", ""))
            _hm = _FULLNAME_TO_ABBREV.get(_g0.get("home_name", ""), _g0.get("home_name", ""))
            _net = (_g0.get("national_tv") or [None])[0] or _g0.get("away_tv") or _g0.get("home_tv") or ""
            _net_str = f' <span style="color:{MUTED};">({_net})</span>' if _net else ""
            _tune_in = (f'\U0001f4fa Tune in: <b>{_aw}–{_hm}</b>{_net_str} — '
                        f'{len(_top["mine"])} of yours + {len(_top["opp"])} of theirs')
    except Exception as _e:
        print(f"  WARNING: today's-games panel failed ({_e}); skipping it.")

    league_total_roster_max = int(snap.get("league_total_roster_max") or 28)
    roster_suggestion = _roster_suggestion(
        matchup, pitchers, hitters, fa_sp, fa_rp, fa_hit,
        my_team, best_recent_p, best_recent_h,
        all_matchups, week_end_str, classification=category_classification,
        league_total_roster_max=league_total_roster_max,
        pos_data=pos_data, lineup_eff=(snap.get("lineup_efficiency_current") or {} if not override_team else {}),
    )
    trade_bullets = [_pending_headline(g) for g in incoming_pending]
    week_overview = build_week_overview(
        matchup, week_cats, week_n, fa_sp, starts, days_elapsed, my_starts_by_day,
        week_end=week_end_str, is_sunday=is_sunday, roster_suggestion=roster_suggestion,
        trade_bullets=trade_bullets
    )
    # Matchup-to-date Lineup Watch (my team only — snapshot stores only my daily lineup).
    bench_watch = build_bench_watch(snap.get("lineup_efficiency_current") or {}) if not override_team else ""
    # Pending trade offers (graded once above; the section renders the full cards).
    pending_section = build_pending_trades_section(
        graded_pending, best_recent_p, best_recent_h, hit_pctile, team_logos=team_logos)
    body_parts += [
        build_prev_matchup_recap(prev_matchup, team_logos=team_logos) if is_monday and prev_matchup.get("week") != (matchup or {}).get("week") else "",  # 2a MONDAY RECAP
        week_overview,                                                                    # 2  WEEK INTELLIGENCE
        build_category_pulse(matchup, weekly_avgs=weekly_avgs, days_elapsed=days_elapsed, remaining_proj=pit_proj, is_sunday=is_sunday, weekly_std=weekly_std, matchup_days=matchup_period_days, game_days_elapsed=game_days_elapsed, matchup_game_days=matchup_game_days), # 3
        opp_preview_section,                                                              # 3b OPPONENT SCOUTING (below Category Pulse)
        todays_games_section,                                                             # 3c TODAY'S MLB GAMES (matchup overlap — what to tune into)
        week_cat_section,                                                                 # 4  (before matchup panel)
        week_roto_rankings_section,                                                       # 4b league-wide roto (hidden Monday before stats accumulate)
        build_matchup_section(matchup, logos=team_logos, my_team=my_team,
                              weekly_avgs=weekly_avgs, days_elapsed=days_elapsed,
                              remaining_proj=pit_proj, matchup_days=matchup_period_days,
                              game_days_elapsed=game_days_elapsed, matchup_game_days=matchup_game_days),  # 5
        band_divider("MY ROSTER", anchor="band-myroster"),                                # MY TEAM band header
        alert_section,                                                                    # 1  ALERTS (top of My Roster)
        bench_watch,                                                                      # 1b Lineup Watch (matchup-to-date bench leakage / blowups / idle hitters)
        pos_section,                                                                      # 10 Positional Breakdown (moved to top of My Roster)
        starts_section,                                                                   # 6
        my_rp_section,                                                                    # 7
        build_pitcher_hot_cold_section(pitchers, my_team, rec_p, best_recent_p),         # 8
        build_hot_cold_section(hitters, recent_hitting, my_team, best_recent_h, hit_pctile),  # 9
        band_divider("TRANSACTIONS", anchor="band-fa"),                                   # ACTION band header (FA pickups + Trade Radar)
        pending_section,                                                                  # 10b Pending Trades (real offers — Accept/Counter/Decline)
        fa_sp_section,                                                                    # 11
        fa_rp_section,                                                                    # 12
        fa_hit_section,                                                                   # 13
        build_trade_radar(pitchers, hitters, roto, my_team, best_recent_p, best_recent_h,
                          pos_data, hit_pctile, pit_pctile, team_logos=team_logos),       # 13b Trade Radar
        band_divider("SEASON", anchor="band-season"),                                     # SEASON CONTEXT band header
        cat_section,                                                                      # 14
        luck_section,                                                                     # 15
        build_season_trajectory(weekly_results, standings, my_team=my_team),              # 16 Season Trajectory (W/L/T by week + streak)
        '<div style="margin-top:28px;"></div>',                                            # breathing room before Season Roto Rankings
        build_season_roto_rankings(roto, my_team=my_team, team_logos=team_logos,
                                   season_totals=snap.get("season_cat_totals")),          # 17 Season Roto Rankings (all matchups aggregated)
        band_divider("REFERENCE", anchor="band-glossary"),                                # REFERENCE band header
        build_glossary_section(),                                                         # 16 Glossary & Methodology
    ]
    body = "\n".join(p for p in body_parts if p)

    # The skimmable inline email body ("The Briefing") — the full digest below ships
    # as the attachment. Defensive: any failure just falls back to no briefing (main
    # then uses the full digest as the body, exactly as before this feature).
    try:
        briefing_html = render_briefing(
            my_team=my_team, today=_today, matchup=matchup,
            classification=category_classification, starts=starts,
            today_str=today_str, week_end_str=week_end_str,
            sr_emerging=_sr_emerging, alerts=alerts, my_row=my_row,
            n_teams=len(standings), tune_in=_tune_in,
            pending_incoming=incoming_pending,
        )
    except Exception as _e:
        print(f"  WARNING: briefing build failed ({_e}); body falls back to full digest.")
        briefing_html = ""

    coverage_footer = build_coverage_footer(snap)   # quiet on healthy data, colored on degradation

    full_html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Daily Digest — {my_team}</title>
  <style>
    /* Tap-to-expand Score breakdown (v2): each breakdown <tr> carries inline
       display:none; tapping its badge sets the URL fragment and this :target rule
       reveals the full-width row below the player (!important beats the inline style).
       Renders in the browser-opened attachment; Gmail strips <style> so the rows stay
       hidden there (the score badge itself always shows). */
    tr.scorebd-row:target {{ display:table-row !important; scroll-margin-top:40vh; }}
    div.scorebd-div:target {{ display:block !important; scroll-margin-top:40vh; }}
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
    {coverage_footer}
  </div>
</div>
{_BD_TOGGLE_SCRIPT}
</body>
</html>"""

    return full_html, briefing_html

# ── SEND ──────────────────────────────────────────────────────────────────────

def send_email(body_html, attachment_html, subject, filename=None, extra_attachments=None):
    """Send the email. `body_html` is the skimmable inline body ("The Briefing");
    `attachment_html` is the full digest, attached so it always renders in a browser.
    `extra_attachments` is an optional list of (html, filename) tuples appended as
    further .html attachments (e.g. the dashboard rides along under --with-dashboard)
    so both deliverables arrive in ONE email."""
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

    # Inline body — the short Briefing (Gmail renders it; the full digest is attached below)
    msg.attach(MIMEText(body_html, "html"))

    # HTML attachment so the full digest is always accessible (open in browser)
    attachment = MIMEText(attachment_html, "html", "utf-8")
    attachment.add_header(
        "Content-Disposition", "attachment",
        filename=filename or f"digest_{datetime.now().strftime('%Y-%m-%d')}.html",
    )
    msg.attach(attachment)

    # Additional attachments (dashboard, etc.) — each its own .html, opened in a browser
    for extra_html, extra_name in (extra_attachments or []):
        extra = MIMEText(extra_html, "html", "utf-8")
        extra.add_header("Content-Disposition", "attachment", filename=extra_name)
        msg.attach(extra)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(FROM_EMAIL, GMAIL_APP_PASSWORD)
        smtp.sendmail(FROM_EMAIL, [TO_EMAIL, CC_EMAIL], msg.as_string())
    return 200

# ── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    dry_run        = "--dry-run"       in sys.argv
    no_refresh     = "--no-refresh"    in sys.argv
    with_dashboard = "--with-dashboard" in sys.argv
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

    # Non-blocking schema check: surface a drifted snapshot (esp. under --no-refresh, where
    # fetch_data's write-time guard never ran) but never crash a working reader.
    try:
        from snapshot_schema import validate_snapshot, report as _snap_report
        _errs, _warns = validate_snapshot(snap)
        if _errs or _warns:
            _snap_report(_errs, _warns)
    except ImportError:
        pass

    html, briefing = build_email(snap, override_team=override_team)
    body_html = briefing or html          # fall back to the full digest if the briefing failed
    team_slug = team_label.replace(" ", "_")
    date_str   = datetime.now().strftime('%Y-%m-%d')
    _is_sun    = datetime.now().weekday() == 6
    _kind      = "Lookahead" if _is_sun else "Digest"
    _kind_sub  = f"{_kind} + Dashboard" if with_dashboard else _kind
    subject    = f"⚾ {team_label} {_kind_sub} — {datetime.now().strftime('%b %d')}"

    # Optionally build the dashboard and attach it to THIS email (one delivery for both).
    # Isolated so a dashboard failure only drops the attachment — the digest still sends.
    extra_attachments = []
    dash_html = None
    if with_dashboard:
        print("  Building dashboard attachment...")
        try:
            import dashboard as _dash  # deferred: dashboard imports send_digest at module load
            dash_team = override_team or snap.get("my_team", MY_TEAM)
            dash_html = _dash.build_dashboard(snap, dash_team)
            dash_name = f"dashboard_{team_slug}_{date_str}.html"
            extra_attachments.append((dash_html, dash_name))
            print(f"  Dashboard built ({len(dash_html) // 1024} KB) — will attach as {dash_name}.")
        except Exception as e:  # never let the dashboard sink the digest
            print(f"  WARNING: dashboard build failed ({e}); sending digest without it.")

    if dry_run:
        fname = f"digest_preview_{team_slug}.html"
        previews_dir = Path(__file__).parent / "previews"
        previews_dir.mkdir(exist_ok=True)
        out = previews_dir / fname
        out.write_text(html, encoding="utf-8")
        print(f"\n  Dry run — saved to {out}")
        # Also drop the inline Briefing body so it can be eyeballed on its own.
        bout = previews_dir / f"briefing_preview_{team_slug}.html"
        bout.write_text(body_html, encoding="utf-8")
        print(f"  Dry run — briefing (email body) saved to {bout}")
        if dash_html is not None:
            dout = previews_dir / f"dashboard_{team_slug}.html"
            dout.write_text(dash_html, encoding="utf-8")
            print(f"  Dry run — dashboard saved to {dout}")
        print("\nDone (no email sent).")
        return

    print(f"\n[3/3] Sending to {TO_EMAIL}...")
    attach_name = f"digest_{date_str}_{team_slug}.html" if override_team else f"digest_{date_str}.html"
    send_email(body_html, html, subject, filename=attach_name, extra_attachments=extra_attachments)
    print("  Sent." + (" (briefing body + digest + dashboard)" if extra_attachments else " (briefing body + digest)"))

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
