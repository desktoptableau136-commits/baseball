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
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

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


def _blend(r, score_fn, idx_recent, w=0.4):
    """40/60 blend of best-available recent stats and season score."""
    s_year = score_fn(r)
    r_rec = idx_recent.get(r.get("PlayerName", ""))
    if not r_rec:
        return s_year
    s_rec = score_fn(r_rec)
    return round(w * s_rec + (1 - w) * s_year) if s_rec > 0 else s_year


def pitcher_score(r):
    kip   = _n(r.get("K/IP") or r.get("KIP"))
    era   = _n(r.get("ERA"))
    whip  = _n(r.get("WHIP"))
    gs    = _n(r.get("GS"))
    svhd  = _n(r.get("SVHD")) or _n(r.get("SV"))
    xfip  = _n(r.get("xFIP"))
    whiff = _n(r.get("WhiffPct"))   # stored as decimal: 0.28 = 28%
    kpct  = _n(r.get("Kpct_P"))
    w     = _n(r.get("ESPN_W")) or _n(r.get("W"))
    ip_g  = _n(r.get("IP_per_G"))
    ip    = _n(r.get("IP"))
    is_sp = _is_sp(r)

    if not kip and not era and not kpct:
        return 0

    s = 0
    if whiff > 0:
        s += min(28, whiff / 0.135 * 28)
    elif kpct > 0:
        s += min(28, kpct / 0.28 * 28)
    else:
        s += min(28, kip / 1.5 * 28)

    era_base = xfip if xfip > 0 else era
    s += max(0, min(28, (6.0 - era_base) / 4.0 * 28))
    s += max(0, min(20, (2.0 - whip) / 1.1 * 20))

    if is_sp:
        # SP role: reward starts volume; SVHD is irrelevant
        s += 12 if gs > 10 else 9
    else:
        # RP role: SVHD first, then W and IP/G as opportunity signals
        s += 5 + min(7, svhd / 15 * 7)
        s += min(6, w / 10 * 6)       # wins
        s += min(5, ip_g / 1.2 * 5)   # opportunity: IP per appearance

    if xfip > 0:
        s += 5 if xfip < 3.2 else (2 if xfip < 3.8 else 0)

    # Small-sample penalty: rate stats are unreliable below 20 IP
    if ip > 0:
        s *= min(1.0, ip / 20)

    # Calibrate to shared 0-100 scale (p50→50, p90→80) derived from observed distribution
    s = s * 1.875 - 67.6
    return max(0, min(100, round(s)))


def hitter_score(r):
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
        return 0

    s = 0
    if wrc > 0:
        s += max(0, min(30, (wrc - 60) / 80 * 30))
    else:
        s += max(0, min(30, (ops - 0.55) / 0.50 * 30))

    s += min(16, hr / 35 * 16)
    if iso > 0:
        s += min(6, iso / 0.25 * 6)

    s += min(10, rbi / 110 * 10)

    if sprint > 0:
        s += max(0, min(10, (sprint - 24) / 6 * 10))
    else:
        s += min(10, sb / 40 * 10)

    if xwoba > 0:
        s += max(0, min(10, (xwoba - 0.270) / 0.120 * 10))
    else:
        s += max(0, min(10, (avg - 0.180) / 0.160 * 10))

    s += min(8, hrp * 40)

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

    score = 38  # league-average baseline
    if ip_per_g > 0:
        score += (ip_per_g - 5.4) * 16  # biggest driver: avg innings per appearance
    if era > 0:
        score += (4.2 - era) * 8
    if whip > 0:
        score += (1.35 - whip) * 12
    if brl > 0:
        score += (7.5 - brl) * 0.5
    if kpct > 0:
        score += (kpct - 0.22) * 20
    if opp > 0:
        score += (0.730 - opp) * 60     # matchup adjustment

    return max(1, min(99, round(score)))


def sp_fa_score(r):
    if not _is_sp(r):
        return 0
    s = pitcher_score(r)
    if r.get("PSP_Date") and r.get("PSP_Date") != "1999-01-01":
        qsp = qs_probability(r) or 50
        s += max(8, min(22, round(8 + qsp * 0.14)))
    return min(100, round(s))


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


def fa_starters(pitchers, claimed=None, week_end=None):
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
    ]
    for r in fa:
        r["_score"] = sp_fa_score(r)
    return sorted(fa, key=lambda r: -r["_score"])[:12]


def rp_score(r):
    svhd = _n(r.get("ESPN_SVHD")) or _n(r.get("SVHD"))   # prefer season total from ESPN
    k    = _n(r.get("ESPN_K"))    or _n(r.get("K"))       # prefer season count from ESPN
    w    = _n(r.get("ESPN_W"))    or _n(r.get("W"))
    ip_g = _n(r.get("IP_per_G"))
    era  = _n(r.get("ERA")) or 5.0
    whip = _n(r.get("WHIP")) or 1.5
    # Weights (max 100): SVHD 40 · K 22 · W 13 · IP/G 10 · ERA 9 · WHIP 6
    s  = min(40, svhd / 20 * 40)
    s += min(22, k    / 80 * 22)
    s += min(13, w    / 10 * 13)
    s += min(10, ip_g / 1.2 * 10)   # opportunity: IP per appearance, max at 1.2 IP/G
    s += max(0, min(9, (5.0 - era)  / 3.0 * 9))
    s += max(0, min(6, (2.0 - whip) / 1.0 * 6))
    return round(s, 1)


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


def fa_hitters(hitters, claimed=None):
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
        r["_score"] = hitter_score(r)
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

        def score(r, score_fn=score_fn, idx30=idx30):
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
                viable = lambda r: _n(r.get("GS")) >= 3
            else:
                viable = lambda r: _n(r.get("ESPN_GP")) >= 12 or _n(r.get("IP")) >= 20
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
        results.append({
            "pos":          pos_label,
            "ptype":        ptype,
            "worst_player": my_p[-1] if my_p else None,
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


_LEAGUE_AVG_OPS = 0.717  # 2026 MLB average across eligible starters in snapshot

def _proj_line_html(r):
    ip_g = _n(r.get("IP_per_G"))
    if ip_g <= 0:
        return f'<span style="color:{MUTED}">—</span>'
    era = _n(r.get("ERA"))
    kip = _n(r.get("K/IP"))

    # Adjust ER for opponent OPS vs league average, and home/away park effect
    opp_ops  = _n(r.get("Team_OPS_Value"))
    hva      = str(r.get("PSP_HomeVAway") or "")
    opp_factor  = min(1.20, max(0.80, opp_ops / _LEAGUE_AVG_OPS)) if opp_ops > 0 else 1.0
    park_factor = 0.97 if hva.startswith("vs ") else (1.03 if hva.startswith("@ ") else 1.0)

    raw_er = era * ip_g / 9 if era > 0 else 0
    er = round(raw_er * opp_factor * park_factor)
    k  = round(kip * ip_g) if kip > 0 else 0
    return f'<span style="color:{MUTED};font-size:10px;white-space:nowrap;">{_fmt_ip(ip_g)}&nbsp;IP&thinsp;·&thinsp;{er}&nbsp;ER&thinsp;·&thinsp;{k}K</span>'


def band_divider(label, color=None):
    c = color or MUTED
    return (
        f'<div style="display:flex;align-items:center;margin:32px 0 22px;">'
        f'<div style="flex:1;height:1px;background:{BORDER};"></div>'
        f'<span style="padding:0 14px;color:{c};font-size:10px;font-weight:700;'
        f'letter-spacing:2px;text-transform:uppercase;">{label}</span>'
        f'<div style="flex:1;height:1px;background:{BORDER};"></div>'
        f'</div>'
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


def hot_cold_cell(season_val, recent_val, lower_better=False, dec=2, hot_thresh=None, warm_thresh=None, no_data_title=None):
    """Table cell showing recent stat + hot/cold icon vs season baseline."""
    _dash_cell = (
        f'<td style="{TDC}"><span style="color:{MUTED};cursor:help;border-bottom:1px dotted {MUTED};" title="{no_data_title}">—</span></td>'
        if no_data_title else f'<td style="{TDC}color:{MUTED};">—</td>'
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
        f'<td style="{TDC}">'
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

        def _proj_span(val, ref_color):
            if val is None:
                return ""
            return (f'<div style="font-size:9px;font-weight:400;color:{MUTED};margin-top:2px;">'
                    f'proj <span style="color:{ref_color}99;">{val:.{dec}f}</span></div>')

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
            f'{my_v:.{dec}f}{_proj_span(p["pm"] if p else None, my_color)}</td>'
            f'<td style="{TDC}color:{mid_color};">{mid}</td>'
            f'<td style="{TDC}font-weight:700;color:{opp_color};font-size:14px;">'
            f'{opp_v:.{dec}f}{_proj_span(p["po"] if p else None, opp_color)}</td>'
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

def build_hot_cold_section(hitters, recent_hitting, my_team):
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

        rows_html += (
            f'<tr style="{bg}">'
            f'<td style="{TD_S}font-weight:600;">{team_logo(r["team"])}{r["name"]}{r["inj"]}</td>'
            f'<td style="{TDC}color:{MUTED};">{r["pos"]}</td>'
            f'<td style="{TDC}">{r["season_ops"]:.3f}</td>'
            f'<td style="{TDC}">{recent_str}</td>'
            f'<td style="{TDC}">{delta_html} {arrow}</td>'
            f'</tr>'
        )

    n_hot  = sum(1 for r in with_data if r["delta"] >= 0.015)
    n_cold = sum(1 for r in with_data if r["delta"] <= -0.015)
    sub = f"{n_hot} hot · {n_cold} cold · last 7 days vs season OPS"

    return (
        section_head("Roster Hot/Cold", sub) +
        f'<table style="width:100%;border-collapse:collapse;margin-bottom:24px;font-size:13px;">'
        f'<thead><tr>'
        f'<th style="{TH_S}">Hitter</th>'
        f'<th style="{TH_S}text-align:center;">Pos</th>'
        f'<th style="{TH_S}text-align:center;">Season OPS</th>'
        f'<th style="{TH_S}text-align:center;">Last 7 OPS</th>'
        f'<th style="{TH_S}text-align:center;">Δ</th>'
        f'</tr></thead><tbody>{rows_html}</tbody></table>'
    )


def build_pitcher_hot_cold_section(pitchers, my_team, rec_p=None):
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

        rows_html += (
            f'<tr style="{bg}">'
            f'<td style="{TD_S}font-weight:600;">{team_logo(r["team"])}{r["name"]}{r["inj"]}</td>'
            f'<td style="{TDC}color:{MUTED};">{r["pos"]}</td>'
            f'<td style="{TDC}">{r["season_era"]:.2f}</td>'
            f'<td style="{TDC}">{recent_str}</td>'
            f'<td style="{TDC}">{delta_html} {arrow}</td>'
            f'</tr>'
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


def build_prev_matchup_recap(prev_matchup):
    if not prev_matchup or not prev_matchup.get("categories"):
        return ""

    week    = prev_matchup.get("week", "")
    opp     = prev_matchup.get("opp_team", "Opponent")
    my_team = prev_matchup.get("my_team", MY_TEAM)
    wins    = prev_matchup.get("wins", 0)
    losses  = prev_matchup.get("losses", 0)
    ties    = prev_matchup.get("ties", 0)
    cats    = prev_matchup.get("categories", [])

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
    header_cells = f'<th style="{th}text-align:left;min-width:72px;"></th>'
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

    def _data_row(label, label_color, val_key, row_style=""):
        row = (f'<td style="{td}text-align:left;color:{label_color};font-weight:700;'
               f'font-size:11px;">{label}</td>')
        for i, cat in enumerate(cat_order):
            c   = cat_map.get(cat, {})
            val = c.get(val_key, 0)
            left_border = f'border-left:1px solid {BORDER};' if i == 6 else ''
            row += f'<td style="{td}color:{VAL_COLOR};{left_border}">{_fmt(val, cat)}</td>'
        return f'<tr{" " + row_style if row_style else ""}>{row}</tr>'

    my_short  = " ".join(my_team.split())
    opp_short = opp[:14] + ("…" if len(opp) > 14 else "")

    table = (
        f'<div style="overflow-x:auto;-webkit-overflow-scrolling:touch;margin-top:10px;">'
        f'<table style="width:100%;border-collapse:collapse;min-width:420px;">'
        f'<thead><tr>{header_cells}</tr></thead>'
        f'<tbody>'
        + _data_row(my_short,  ACCENT, "my_val")
        + _data_row(opp_short, TEXT,   "opp_val")
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
                        all_matchups, week_end_str):
    """Return one add/drop or trade suggestion bullet HTML for Week at a Glance."""
    if not matchup:
        return ""

    cats        = matchup.get("categories", [])
    my_norm     = " ".join(my_team.split())
    opp         = matchup.get("opp_team", "")
    losing      = [c for c in cats if c["result"] == "L"]
    losing_cats = {c["cat"] for c in losing}
    if not losing_cats:
        return ""

    losing_pit = losing_cats & _PIT_CATS
    losing_hit = losing_cats & _HIT_CATS
    focus_pit  = len(losing_pit) >= len(losing_hit)

    # ── ADD / DROP ────────────────────────────────────────────────────────────
    add_candidate = drop_candidate = None
    add_reason    = ""

    # Determine what to ADD based on losing categories
    if focus_pit:
        only_svhd = losing_pit == {"SVHD"}
        if only_svhd:
            fa_pool    = sorted(fa_rp, key=lambda r: _blend(r, pitcher_score, best_recent_p), reverse=True)
            add_reason = "SV+H gap"
        else:
            fa_pool    = sorted(fa_sp, key=lambda r: sp_fa_score(r), reverse=True)
            sp_losing  = losing_pit - {"SVHD"}
            add_reason = "/".join(_CAT_DISPLAY.get(c, c) for c in sorted(sp_losing)) + " gap"
    else:
        fa_pool    = sorted(fa_hit, key=lambda r: _blend(r, hitter_score, best_recent_h), reverse=True)
        add_reason = "/".join(_CAT_DISPLAY.get(c, c) for c in sorted(losing_hit)) + " gap"
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
        [(r, _blend(r, pitcher_score, best_recent_p)) for r in drop_pit] +
        [(r, _blend(r, hitter_score,  best_recent_h)) for r in full_hit],
        key=lambda x: x[1]
    )

    def _pos_tags(r):
        pos_str = (r.get("Position") or "").upper()
        return {p.strip() for p in pos_str.replace("/", ",").split(",") if p.strip()}

    def _can_drop(cand):
        """True if dropping cand leaves at least one healthy player at every position it fills."""
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

    drop_candidate = next((r for r, _ in scored_drop if _can_drop(r)), None)

    if add_candidate and drop_candidate:
        an = add_candidate.get("PlayerName", "")
        dn = drop_candidate.get("PlayerName", "")
        if an and dn and an != dn:
            return (
                f'Pickup: Add <span style="color:{TEXT};font-weight:700;">{an}</span>'
                f'<span style="color:{MUTED};"> ({add_reason})</span>'
                f' &middot; Drop <span style="color:{MUTED};">{dn}</span>'
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
        confirmed = [s for s in starts if s.get("PSP_Date", "1999-01-01") != "1999-01-01"]
        n_days = len(set(s["PSP_Date"] for s in confirmed))
        thin_days = sorted(d for d, cnt in my_starts_by_day.items() if cnt < 2)
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
            best = max(pool, key=lambda r: qs_probability(r) or 0)
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


# ── EMAIL BUILDER ─────────────────────────────────────────────────────────────

def build_email(snap, override_team=None):
    my_team       = override_team if override_team else snap.get("my_team", MY_TEAM)
    pitchers      = snap.get("pitchers", [])
    hitters       = snap.get("hitters", [])
    roto          = snap.get("roto", [])
    standings     = snap.get("standings", [])
    refreshed     = snap.get("refreshed_at", "")[:10]
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

    fa_sp     = fa_starters(pitchers, claimed)
    fa_rp     = fa_relievers(pitchers, claimed)
    fa_hit    = fa_hitters(hitters, claimed)
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
    my_week_roto_pts = sum(
        float(r.get("Roto_Score") or 0)
        for r in week_roto
        if " ".join((r.get("Team") or "").split()) == " ".join(my_team.split())
    )
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

    # Hot/cold counts from recent_hitting
    n_hot = n_cold = 0
    for r in hitters:
        if (" ".join((r.get("FantasyTeam") or "").split()) == " ".join(my_team.split())
                and int(r.get("Dataset", 0)) == YEAR
                and float(r.get("OPS") or 0) > 0):
            s_ops = float(r.get("OPS") or 0)
            rh = rec_h.get(r.get("PlayerName", ""), {})
            r_ops = float(rh.get("OPS") or 0) if rh else 0
            if s_ops > 0 and r_ops > 0:
                d = r_ops - s_ops
                if d >= 0.015:   n_hot  += 1
                elif d <= -0.015: n_cold += 1
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
    if _data_fresh:
        _data_badge = (
            f'<span style="color:{MUTED};font-size:10px;margin-left:10px;vertical-align:middle;">'
            f'&#10003;&thinsp;data current</span>'
        )
    else:
        try:
            _ref_dt = datetime.strptime(refreshed, "%Y-%m-%d")
            _ref_label = f"{_ref_dt.strftime('%b')} {_ref_dt.day}"
        except Exception:
            _ref_label = refreshed
        _data_badge = (
            f'<span style="color:{YELLOW};font-size:10px;font-weight:600;margin-left:10px;vertical-align:middle;">'
            f'&#9888;&thinsp;data from {_ref_label} &mdash; run a refresh for today\'s matchup</span>'
        )

    header = f"""
<div style="background:linear-gradient(135deg,#0b1a38 0%,#0f172a 100%);padding:22px 28px;border-bottom:2px solid {BORDER};">
  <div style="color:{MUTED};font-size:10px;text-transform:uppercase;letter-spacing:1px;">{today}{_data_badge}</div>
  <div style="margin-top:6px;vertical-align:middle;">{my_logo_html}<span style="color:{TEXT};font-size:24px;font-weight:900;letter-spacing:-1px;vertical-align:middle;">{my_team}</span></div>
  <div style="color:#4b7bc4;font-size:11px;letter-spacing:.8px;margin-top:4px;text-transform:uppercase;">{_digest_label}</div>
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
                f'<td colspan="9" style="padding:5px 10px;'
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
                qs_fires_s = bool(qsp and qsp >= 51)
                k_fires_s  = (_n(r.get("K/IP")) >= 0.90 or _n(r.get("Kpct_P")) >= 0.24) and _n(r.get("IP_per_G")) >= 4.5
                start_badges = []
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
                rows += (
                    f'<tr style="{bg}">'
                    f'<td style="{TD_S}font-weight:600;">{team_logo(r.get("Team"))}{name}{inj_tag(r)}{start_badge}</td>'
                    f'<td style="{TDC}">{proj_line_s}</td>'
                    f'<td style="{TDC}">{opp_logo(ha)}{ha}'
                    f'{"&nbsp;<span style=\"color:#888;font-size:11px\">(proj.)</span>" if r.get("PSP_Projected") else ""}</td>'
                    f'<td style="{TDC}">{v(r.get("Team_OPS_Value"), 3)}</td>'
                    f'<td style="{TDC}">{qsp_str}</td>'
                    f'<td style="{TDC}">{v(r.get("ERA"), 2)}</td>'
                    + hot_cold_cell(r.get("ERA"), p15r.get("ERA"), lower_better=True, dec=2, no_data_title="No 15-day stats — player may not have pitched recently") +
                    f'<td style="{TDC}">{kpct_s_cell}</td>'
                    f'<td style="{TDC}">{badge(_blend(r, pitcher_score, best_recent_p))}</td>'
                    f'</tr>'
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
            f'<table style="width:100%;border-collapse:collapse;font-size:13px;">'
            f'<thead><tr>'
            f'<th style="{TH_S}">Pitcher</th>'
            f'<th style="{TH_S}text-align:center;">Proj. Line</th>'
            f'<th style="{TH_S}text-align:center;">Matchup</th>'
            f'<th style="{TH_S}text-align:center;">Opp OPS</th>'
            f'<th style="{TH_S}text-align:center;">QS%</th>'
            f'<th style="{TH_S}text-align:center;">ERA</th>'
            f'<th style="{TH_S}text-align:center;">L15 ERA</th>'
            f'<th style="{TH_S}text-align:center;">K%</th>'
            f'<th style="{TH_S}text-align:center;">Score</th>'
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
            return (
                f'<tr style="{bg}">'
                f'<td style="{TD_S}font-weight:600;">{team_logo(r.get("Team"))}{r.get("PlayerName","")}{inj_tag(r)}{ds_badge}</td>'
                f'<td style="{TDC}color:{MUTED};">{r.get("Position","")}</td>'
                f'<td style="{TDC}">{v(svhd, 0)}</td>'
                f'<td style="{TDC}">{v(k, 0)}</td>'
                f'<td style="{TDC}">{v(w, 0)}</td>'
                f'<td style="{TDC}">{f"{era:.2f}" if era > 0 else "—"}</td>'
                f'<td style="{TDC}">{f"{whip:.2f}" if whip > 0 else "—"}</td>'
                f'<td style="{TDC}">{badge(r[score_key])}</td>'
                f'</tr>'
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
            thin_day = my_count < 2 and date_str <= week_end_str
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
                f'<td colspan="9" style="padding:5px 10px;'
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

                # Pickup highlight on thin days — both badges can fire simultaneously
                qs_fires = bool(qsp and qsp >= 51)
                k_fires  = (_n(r.get("K/IP")) >= 0.90 or _n(r.get("Kpct_P")) >= 0.24) and _n(r.get("IP_per_G")) >= 4.5
                pickup_badges = []
                name_border = ""
                if thin_day or date_str > week_end_str:
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

                _kpct_val = _n(r.get("Kpct_P"))
                _kpct_top = _kpct_val > 0 and _kpct_val in _top3_kpct_fa
                kpct_cell = (
                    f'<span style="color:{YELLOW};font-weight:700;">{_kpct_val*100:.1f}%</span>'
                    if _kpct_top and _kpct_val > 0
                    else (f"{_kpct_val*100:.1f}%" if _kpct_val > 0 else f'<span style="color:{MUTED}">—</span>')
                )
                proj_line_str = _proj_line_html(r)
                rows += (
                    f'<tr style="{bg}">'
                    f'<td style="{name_border}{TD_S}font-weight:600;">{team_logo(r.get("Team"))}{r.get("PlayerName","")}{inj_tag(r)}{pickup_badge}</td>'
                    f'<td style="{TDC}">{proj_line_str}</td>'
                    f'<td style="{TDC}">{opp_logo(ha)}{ha}'
                    f'{"&nbsp;<span style=\"color:#888;font-size:11px\">(proj.)</span>" if r.get("PSP_Projected") else ""}</td>'
                    f'<td style="{TDC}">{v(r.get("Team_OPS_Value"), 3)}</td>'
                    f'<td style="{TDC}">{qsp_str}</td>'
                    f'<td style="{TDC}">{v(r.get("ERA"), 2)}</td>'
                    + hot_cold_cell(r.get("ERA"), p15r.get("ERA"), lower_better=True, dec=2, no_data_title="No 15-day stats — player may not have pitched recently") +
                    f'<td style="{TDC}">{kpct_cell}</td>'
                    f'<td style="{TDC}">{badge(r["_score"])}</td>'
                    f'</tr>'
                )
        table = (
            f'<div style="overflow-x:auto;-webkit-overflow-scrolling:touch;margin-bottom:24px;">'
            f'<table style="width:100%;border-collapse:collapse;font-size:13px;">'
            f'<thead><tr>'
            f'<th style="{TH_S}">Pitcher</th>'
            f'<th style="{TH_S}text-align:center;">Proj. Line</th>'
            f'<th style="{TH_S}text-align:center;">Matchup</th>'
            f'<th style="{TH_S}text-align:center;">Opp OPS</th>'
            f'<th style="{TH_S}text-align:center;">QS%</th>'
            f'<th style="{TH_S}text-align:center;">ERA</th>'
            f'<th style="{TH_S}text-align:center;">L15 ERA</th>'
            f'<th style="{TH_S}text-align:center;">K%</th>'
            f'<th style="{TH_S}text-align:center;">Score</th>'
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
            return (
                f'<tr style="{bg}">'
                f'<td style="{TD_S}font-weight:600;">{team_logo(r.get("Team"))}{r.get("PlayerName","")}{inj_tag(r)}{ds_badge}</td>'
                f'<td style="{TDC}color:{MUTED};">{r.get("Position","")}</td>'
                f'<td style="{TDC}">{v(svhd, 0)}</td>'
                f'<td style="{TDC}">{v(k, 0)}</td>'
                f'<td style="{TDC}">{v(w, 0)}</td>'
                f'<td style="{TDC}">{f"{era:.2f}" if era > 0 else "—"}</td>'
                f'<td style="{TDC}">{f"{whip:.2f}" if whip > 0 else "—"}</td>'
                f'<td style="{TDC}">{badge(r["_rp_score"])}</td>'
                f'</tr>'
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
            f'<th style="{TH_S}text-align:center;">Score</th>'
            f'</tr></thead><tbody>{"".join(_fa_rp_row(r,i) for i,r in enumerate(fa_rp))}</tbody></table>'
            f'</div>'
        )
    else:
        rp_table = f'<p style="color:{MUTED};font-style:italic;margin-bottom:24px;">No FA relievers found.</p>'

    fa_rp_section = section_head("FA Pickup — Relief Pitchers", "Top 3 available RP · ranked by SV+H, K, W, ERA, WHIP") + rp_table

    # ── FA: Hitters ────────────────────────────────────────────────────────────
    if fa_hit:
        rows = ""
        for i, r in enumerate(fa_hit):
            bg = f"background:{SURFACE2};" if i % 2 else ""
            rh = rec_h.get(r.get("PlayerName", ""), {})
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
                f'<td style="{TDC}">{badge(r["_score"])}</td>'
                f'</tr>'
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
            f'<th style="{TH_S}text-align:center;">Score</th>'
            f'</tr></thead><tbody>{rows}</tbody></table>'
        )
        table = f'<div style="overflow-x:auto;-webkit-overflow-scrolling:touch;margin-bottom:24px;">{table}</div>'
    else:
        table = f'<p style="color:{MUTED};font-style:italic;margin-bottom:24px;">No FA hitters found.</p>'

    fa_hit_section = section_head("FA Pickup — Hitters", "Top available hitters · R / HR / RBI / SB / OPS · sorted by composite score") + table

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
        section_head("Current Matchup", f"Week {current_week_num} · {my_week_roto_pts:.1f} roto pts · vs. this week's matchup") +
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

        worst = p["worst_player"]
        if worst:
            player_cell = (
                f'{team_logo(worst.get("Team"), 16)}'
                f'<span style="font-weight:600;">{worst["PlayerName"]}</span>'
                f'{inj_tag(worst)}'
                f' {badge(worst["_pscore"])}'
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
            fa_cell = (
                f'{team_logo(top_fa.get("Team"), 16)}'
                f'<span style="{"font-weight:600;" if upgrade else ""}'
                f'color:{GREEN if upgrade else MUTED};">'
                f'{top_fa["PlayerName"]}</span> {badge(fa_score)}'
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
    roster_suggestion = _roster_suggestion(
        matchup, pitchers, hitters, fa_sp, fa_rp, fa_hit,
        my_team, best_recent_p, best_recent_h,
        all_matchups, week_end_str
    )
    week_overview = build_week_overview(
        matchup, week_cats, week_n, fa_sp, starts, days_elapsed, my_starts_by_day,
        week_end=week_end_str, is_sunday=is_sunday, roster_suggestion=roster_suggestion
    )
    body_parts += [
        build_prev_matchup_recap(prev_matchup) if is_monday and prev_matchup.get("week") != (matchup or {}).get("week") else "",  # 2a MONDAY RECAP
        week_overview,                                                                    # 2  WEEK INTELLIGENCE
        build_category_pulse(matchup, weekly_avgs=weekly_avgs, days_elapsed=days_elapsed, remaining_proj=pit_proj, is_sunday=is_sunday), # 3
        week_cat_section,                                                                 # 4  (before matchup panel)
        build_matchup_section(matchup, logos=team_logos, my_team=my_team,
                              weekly_avgs=weekly_avgs, days_elapsed=days_elapsed,
                              remaining_proj=pit_proj),                                    # 5
        band_divider("MY ROSTER"),                                                        # MY TEAM band header
        alert_section,                                                                    # 1  ALERTS (top of My Roster)
        starts_section,                                                                   # 6
        my_rp_section,                                                                    # 7
        build_pitcher_hot_cold_section(pitchers, my_team, rec_p),                        # 8
        build_hot_cold_section(hitters, recent_hitting, my_team),                        # 9
        pos_section,                                                                      # 10
        band_divider("FREE AGENTS"),                                                      # ACTION band header
        fa_sp_section,                                                                    # 11
        fa_rp_section,                                                                    # 12
        fa_hit_section,                                                                   # 13
        band_divider("SEASON"),                                                           # SEASON CONTEXT band header
        cat_section,                                                                      # 14
        luck_section,                                                                     # 15
    ]
    body = "\n".join(p for p in body_parts if p)

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{my_team} Daily Digest</title>
  <style>
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

    log_line = f"{ts} | sent | subject={subject}\n"
    (LOG_DIR / "digest.log").open("a", encoding="utf-8").write(log_line)

    print("\nDone.")


if __name__ == "__main__":
    main()
