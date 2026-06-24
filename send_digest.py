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
from datetime import datetime
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

def _n(val):
    """Coerce to float, return 0 for falsy/negative sentinel values."""
    try:
        v = float(val or 0)
        return v if v > 0 else 0
    except (TypeError, ValueError):
        return 0


def pitcher_score(r):
    kip   = _n(r.get("K/IP") or r.get("KIP"))
    era   = _n(r.get("ERA"))
    whip  = _n(r.get("WHIP"))
    gs    = _n(r.get("GS"))
    svhd  = _n(r.get("SVHD")) or _n(r.get("SV"))
    xfip  = _n(r.get("xFIP"))
    whiff = _n(r.get("WhiffPct"))   # stored as decimal: 0.28 = 28%
    kpct  = _n(r.get("Kpct_P"))
    inj   = str(r.get("FreeAgentInjuryStatus") or "").upper()

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
    s += 12 if gs > 10 else (9 if gs > 3 else (8 if svhd > 3 else 5))

    if xfip > 0:
        s += 5 if xfip < 3.2 else (2 if xfip < 3.8 else 0)

    if inj in ("IL", "OUT"):
        s -= 22
    elif inj == "DTD":
        s -= 10

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
    inj    = str(r.get("FreeAgentInjuryStatus") or "").upper()

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

    if inj in ("IL", "OUT"):
        s -= 22
    elif inj == "DTD":
        s -= 10

    return max(0, min(100, round(s)))


def sp_fa_score(r):
    gs  = _n(r.get("GS"))
    pos = str(r.get("Position") or "")
    if gs < 1 and "SP" not in pos:
        return 0
    s = pitcher_score(r)
    if r.get("PSP_Date") and r.get("PSP_Date") != "1999-01-01":
        s += 15
    return min(100, round(s))


# ── DATA HELPERS ───────────────────────────────────────────────────────────────

def fa_starters(pitchers):
    fa = [
        r for r in pitchers
        if r.get("FantasyTeam", "") == ""
        and int(r.get("Dataset", 0)) == YEAR
        and r.get("PSP_Date", "") not in ("1999-01-01", "", None)
        and str(r.get("FreeAgentInjuryStatus", "")).upper() != "OUT"
    ]
    for r in fa:
        r["_score"] = sp_fa_score(r)
    return sorted(fa, key=lambda r: -r["_score"])[:12]


def fa_hitters(hitters):
    fa = [
        r for r in hitters
        if r.get("FantasyTeam", "") == ""
        and int(r.get("Dataset", 0)) == YEAR
        and _n(r.get("OPS")) > 0
        and str(r.get("FreeAgentInjuryStatus", "")).upper() != "OUT"
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
            "standing":  s["standing"],
            "roto_pts":  round(totals.get(t, 0), 1),
            "roto_rank": rr,
            "luck":      rr - s["standing"],   # positive = lucky
            "logo_url":  s.get("logo_url", ""),
        })
    return sorted(result, key=lambda r: r["standing"])


def category_ranks(roto_rows, my_team):
    CATS = ["R", "HR", "RBI", "SB", "OPS", "B_SO", "K", "QS", "W", "ERA", "WHIP", "SVHD"]
    totals = {}
    for row in roto_rows:
        t = row.get("Team", "")
        if t not in totals:
            totals[t] = {c: 0 for c in CATS}
        for c in CATS:
            totals[t][c] += float(row.get(f"{c}_Points") or 0)

    teams = list(totals.keys())
    my_ranks = {}
    for c in CATS:
        ranked = sorted(teams, key=lambda t: -totals[t][c])
        for rank, t in enumerate(ranked, 1):
            if t == my_team:
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


def positional_breakdown(pitchers, hitters, my_team):
    results = []
    for pos_label, slots, ptype in POS_GROUPS:
        source   = pitchers if ptype == "pit" else hitters
        score_fn = pitcher_score if ptype == "pit" else hitter_score
        season   = [r for r in source if int(r.get("Dataset", 0) or 0) == YEAR]

        def pos_match(r, slots=slots):
            parts = str(r.get("Position", "")).split(", ")
            return any(s in parts for s in slots)

        my_p = sorted(
            [r for r in season if r.get("FantasyTeam", "") == my_team and pos_match(r)],
            key=lambda r: -score_fn(r),
        )
        for r in my_p:
            r["_pscore"] = score_fn(r)

        # Per-team average score at this position → league rank
        team_scores = {}
        for r in season:
            t = r.get("FantasyTeam", "")
            if t and pos_match(r):
                team_scores.setdefault(t, []).append(score_fn(r))
        team_avgs = sorted(sum(v) / len(v) for v in team_scores.values())
        my_avg = sum(r["_pscore"] for r in my_p) / len(my_p) if my_p else 0
        n = len(team_avgs)
        rank = n - sum(1 for s in team_avgs if s <= my_avg) + 1 if n else None

        # Best FA at this position
        fa = sorted(
            [r for r in season if r.get("FantasyTeam", "") == "" and pos_match(r)],
            key=lambda r: -score_fn(r),
        )
        for r in fa:
            r["_pscore"] = score_fn(r)

        results.append({
            "pos":          pos_label,
            "worst_player": my_p[-1] if my_p else None,
            "my_avg":       round(my_avg, 1),
            "rank":         rank,
            "n_teams":      n,
            "top_fa":       fa[:1],
        })
    return results


def roster_alerts(pitchers, hitters, my_team):
    seen = set()
    alerts = []
    for r in pitchers + hitters:
        if r.get("FantasyTeam", "") != my_team or int(r.get("Dataset", 0)) != YEAR:
            continue
        name = r["PlayerName"]
        inj = str(r.get("FreeAgentInjuryStatus") or "").upper()
        if inj and inj not in ("", "ACTIVE") and name not in seen:
            alerts.append({"name": name, "status": inj})
            seen.add(name)
    return alerts


def my_upcoming_starts(pitchers, my_team):
    sp = [
        r for r in pitchers
        if r.get("FantasyTeam", "") == my_team
        and int(r.get("Dataset", 0)) == YEAR
        and r.get("PSP_Date", "") not in ("1999-01-01", "", None)
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
    if pos in ("SP", "RP"):
        specs = [("ERA", 2), ("WHIP", 2), ("SVHD", 0)] if pos == "RP" else [("ERA", 2), ("WHIP", 2), ("K", 0)]
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
    inj = str(r.get("FreeAgentInjuryStatus") or "").upper()
    if not inj or inj in ("", "ACTIVE"):
        return ""
    color = RED if inj in ("IL", "OUT") else YELLOW
    return f' <span style="color:{color};font-size:10px;font-weight:600;">{inj}</span>'


def section_head(title, sub=""):
    subtitle = f'<div style="color:{MUTED};font-size:11px;margin-top:2px;">{sub}</div>' if sub else ""
    return (
        f'<div style="border-left:3px solid {ACCENT};padding-left:11px;margin:0 0 10px 0;">'
        f'<div style="color:{TEXT};font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;">{title}</div>'
        f'{subtitle}</div>'
    )


def kpi_cell(label, value):
    return (
        f'<td class="kpi-cell" style="text-align:center;padding:14px 8px;border-right:1px solid {BORDER};">'
        f'<div style="color:{MUTED};font-size:10px;text-transform:uppercase;letter-spacing:.7px;">{label}</div>'
        f'<div style="color:{TEXT};font-size:20px;font-weight:800;margin-top:3px;">{value}</div>'
        f'</td>'
    )


# ── MATCHUP SECTION ───────────────────────────────────────────────────────────

_CAT_LABELS_MAP = {
    "R": "R", "HR": "HR", "RBI": "RBI", "SB": "SB", "OPS": "OPS",
    "B_SO": "BB/K", "K": "K", "QS": "QS", "W": "W",
    "ERA": "ERA", "WHIP": "WHIP", "SVHD": "SV+H",
}
_CAT_DEC = {
    "OPS": 3, "ERA": 2, "WHIP": 2,
}


def build_matchup_section(matchup, logos=None):
    if not matchup or not matchup.get("categories"):
        return ""

    logos   = logos or {}
    wins    = matchup["wins"]
    losses  = matchup["losses"]
    ties    = matchup["ties"]
    opp     = matchup.get("opp_team", "Opponent")
    week    = matchup.get("week", "")

    score_str = f"{wins}-{losses}" + (f"-{ties}" if ties else "")
    if wins > losses:
        score_color, status = GREEN, "Winning"
    elif losses > wins:
        score_color, status = RED, "Losing"
    else:
        score_color, status = YELLOW, "Tied"

    opp_short = opp[:16] + ("…" if len(opp) > 16 else "")

    def _norm(n): return " ".join(n.split())
    my_logo_html  = fantasy_logo(logos.get(_norm(MY_TEAM), ""), 36, MY_TEAM)
    opp_logo_html = fantasy_logo(logos.get(_norm(opp), ""), 36, opp)

    score_banner = (
        f'<table style="width:100%;border-collapse:collapse;background:{SURFACE};'
        f'border-radius:6px;margin-bottom:12px;">'
        f'<tr>'
        f'<td style="padding:12px 16px;font-size:13px;font-weight:800;color:{ACCENT};">'
        f'{my_logo_html}Guerrero Warfare &#8592;</td>'
        f'<td style="text-align:center;padding:12px 8px;">'
        f'<div style="font-size:24px;font-weight:900;color:{score_color};">{score_str}</div>'
        f'<div style="font-size:10px;color:{MUTED};text-transform:uppercase;letter-spacing:.5px;">{status}</div>'
        f'</td>'
        f'<td style="padding:12px 16px;font-size:13px;font-weight:700;color:{TEXT};text-align:right;">'
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

        cat_label = f'<span style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:{MUTED};">{label}</span>'
        if res == "W":   # arrow points left → toward GW
            mid = f'&#9664;&nbsp;{cat_label}'
            mid_color = ACCENT   # blue → my team wins
        elif res == "L": # arrow points right → toward Opp
            mid = f'{cat_label}&nbsp;&#9654;'
            mid_color = YELLOW   # orange → opponent wins
        else:
            mid = cat_label
            mid_color = MUTED

        bg = f"background:{SURFACE2};" if i % 2 else ""
        rows += (
            f'<tr style="{bg}">'
            f'<td style="{TDC}font-weight:700;color:{my_color};font-size:14px;">{my_v:.{dec}f}</td>'
            f'<td style="{TDC}color:{mid_color};">{mid}</td>'
            f'<td style="{TDC}font-weight:700;color:{opp_color};font-size:14px;">{opp_v:.{dec}f}</td>'
            f'</tr>'
        )

    table = (
        f'<table style="width:100%;border-collapse:collapse;margin-bottom:24px;font-size:13px;">'
        f'<thead><tr>'
        f'<th style="{TH_S}text-align:center;">Guerrero Warfare</th>'
        f'<th style="{TH_S}text-align:center;"></th>'
        f'<th style="{TH_S}text-align:center;">{opp_short}</th>'
        f'</tr></thead><tbody>{rows}</tbody></table>'
    )

    return (
        section_head(f"Week {week} Matchup", f"vs. {opp} · current standings") +
        score_banner +
        table
    )


# ── EMAIL BUILDER ─────────────────────────────────────────────────────────────

def build_email(snap):
    my_team    = snap.get("my_team", MY_TEAM)
    pitchers   = snap.get("pitchers", [])
    hitters    = snap.get("hitters", [])
    roto       = snap.get("roto", [])
    standings  = snap.get("standings", [])
    refreshed  = snap.get("refreshed_at", "")[:10]
    matchup    = snap.get("current_matchup", {})

    fa_sp     = fa_starters(pitchers)
    fa_hit    = fa_hitters(hitters)
    luck      = luck_standings(roto, standings)
    team_logos = {" ".join(s["team_name"].split()): s.get("logo_url", "") for s in standings}
    cats, n   = category_ranks(roto, my_team)
    current_week_num = matchup.get("week") or max((int(r.get("Week", 0)) for r in roto), default=0)
    week_roto = [r for r in roto if int(r.get("Week", 0)) == current_week_num]
    week_cats, week_n = category_ranks(week_roto, my_team)
    alerts    = roster_alerts(pitchers, hitters, my_team)
    starts    = my_upcoming_starts(pitchers, my_team)
    pos_data  = positional_breakdown(pitchers, hitters, my_team)

    my_row = next((r for r in luck if r["team"] == my_team), {})
    today  = datetime.now().strftime("%A, %B %d, %Y")

    # ── Header ─────────────────────────────────────────────────────────────────
    header = f"""
<div style="background:linear-gradient(135deg,#0b1a38 0%,#0f172a 100%);padding:22px 28px;border-bottom:2px solid {BORDER};">
  <div style="color:{MUTED};font-size:10px;text-transform:uppercase;letter-spacing:1px;">{today}</div>
  <div style="color:{TEXT};font-size:24px;font-weight:900;letter-spacing:-1px;margin-top:4px;">Guerrero Warfare</div>
  <div style="color:#4b7bc4;font-size:11px;letter-spacing:.8px;margin-top:2px;text-transform:uppercase;">Daily Fantasy Digest</div>
</div>"""

    # ── KPI row ────────────────────────────────────────────────────────────────
    wl = f"{my_row.get('wins','—')}-{my_row.get('losses','—')}"
    kpi = f"""
<table style="width:100%;border-collapse:collapse;background:{SURFACE};border-bottom:2px solid {BORDER};">
<tr>
  {kpi_cell("Record", wl)}
  {kpi_cell("Standing", f"#{my_row.get('standing','—')}")}
  {kpi_cell("Roto Rank", f"#{my_row.get('roto_rank','—')}")}
  {kpi_cell("Upcoming Starts", len(starts))}
</tr>
</table>"""

    # ── Alerts ─────────────────────────────────────────────────────────────────
    if alerts:
        items = "".join(
            f'<div style="padding:5px 0;border-bottom:1px solid {BORDER};font-size:12px;">'
            f'<span style="color:{YELLOW};">&#9888;</span> '
            f'<strong style="color:{TEXT};">{a["name"]}</strong>'
            f' <span style="color:{RED if a["status"] in ("IL","OUT") else YELLOW};">{a["status"]}</span></div>'
            for a in alerts
        )
        alert_section = (
            f'<div style="background:#1a0f08;border:1px solid #78350f;border-radius:6px;padding:12px 14px;margin-bottom:20px;">'
            f'<div style="color:{YELLOW};font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.7px;margin-bottom:6px;">&#9888; Roster Alerts</div>'
            f'{items}</div>'
        )
    else:
        alert_section = ""

    # ── My upcoming starts ─────────────────────────────────────────────────────
    if starts:
        by_date = {}
        for r in starts:
            by_date.setdefault(r.get("PSP_Date", ""), []).append(r)

        rows = ""
        row_idx = 0
        for date_str in sorted(by_date.keys()):
            day_pitchers = by_date[date_str]
            try:
                day_label = datetime.strptime(date_str, "%Y-%m-%d").strftime("%a %b %d")
            except Exception:
                day_label = date_str[5:]
            count = len(day_pitchers)
            rows += (
                f'<tr><td colspan="6" style="background:{SURFACE};padding:5px 10px;'
                f'border-top:1px solid {BORDER};border-bottom:1px solid {BORDER};">'
                f'<span style="color:{ACCENT};font-size:11px;font-weight:700;'
                f'text-transform:uppercase;letter-spacing:.5px;">{day_label}</span>'
                f'<span style="color:{MUTED};font-size:10px;margin-left:8px;">'
                f'{count} start{"s" if count != 1 else ""}</span>'
                f'</td></tr>'
            )
            for r in day_pitchers:
                bg = f"background:{SURFACE2};" if row_idx % 2 else ""
                row_idx += 1
                ha = r.get("PSP_HomeVAway", "")
                rows += (
                    f'<tr style="{bg}">'
                    f'<td style="{TD_S}font-weight:600;">{team_logo(r.get("Team"))}{r.get("PlayerName","")}{inj_tag(r)}</td>'
                    f'<td style="{TDC}">{opp_logo(ha)}{ha}'
                    f'{"&nbsp;<span style=\"color:#888;font-size:11px\">(proj.)</span>" if r.get("PSP_Projected") else ""}</td>'
                    f'<td style="{TDC}">{v(r.get("Team_OPS_Value"), 3)}</td>'
                    f'<td style="{TDC}">{v(r.get("ERA"), 2)}</td>'
                    f'<td style="{TDC}">{v(r.get("BarrelPctAllowed"), 1)}</td>'
                    f'<td style="{TDC}">{badge(pitcher_score(r))}</td>'
                    f'</tr>'
                )

        starts_section = (
            section_head("My Upcoming Starts", f"{len(starts)} starts across {len(by_date)} days") +
            f'<table style="width:100%;border-collapse:collapse;margin-bottom:24px;font-size:13px;">'
            f'<thead><tr>'
            f'<th style="{TH_S}">Pitcher</th>'
            f'<th style="{TH_S}text-align:center;">Matchup</th>'
            f'<th style="{TH_S}text-align:center;">Opp OPS</th>'
            f'<th style="{TH_S}text-align:center;">ERA</th>'
            f'<th style="{TH_S}text-align:center;">Brl%</th>'
            f'<th style="{TH_S}text-align:center;">Score</th>'
            f'</tr></thead><tbody>{rows}</tbody></table>'
        )
    else:
        starts_section = ""

    body_parts = []

    # ── FA: Starting Pitchers ──────────────────────────────────────────────────
    if fa_sp:
        rows = ""
        for i, r in enumerate(fa_sp):
            bg = f"background:{SURFACE2};" if i % 2 else ""
            ha = r.get("PSP_HomeVAway", "")
            rows += (
                f'<tr style="{bg}">'
                f'<td style="{TD_S}font-weight:600;">{team_logo(r.get("Team"))}{r.get("PlayerName","")}{inj_tag(r)}</td>'
                f'<td style="{TDC}color:{MUTED};">{r.get("Position","")}</td>'
                f'<td style="{TDC}">{r.get("PSP_Date","")[5:]}</td>'
                f'<td style="{TDC}">{opp_logo(ha)}{ha}'
                f'{"&nbsp;<span style=\"color:#888;font-size:11px\">(proj.)</span>" if r.get("PSP_Projected") else ""}</td>'
                f'<td style="{TDC}">{v(r.get("Team_OPS_Value"), 3)}</td>'
                f'<td style="{TDC}">{v(r.get("ERA"), 2)}</td>'
                f'<td style="{TDC}">{v(r.get("BarrelPctAllowed"), 1)}</td>'
                f'<td class="hide-mob" style="{TDC}">{v(r.get("K/IP"), 2)}</td>'
                f'<td class="hide-mob" style="{TDC}">{vp(r.get("Kpct_P"))}</td>'
                f'<td style="{TDC}">{badge(r["_score"])}</td>'
                f'</tr>'
            )
        table = (
            f'<table style="width:100%;border-collapse:collapse;margin-bottom:0;font-size:13px;">'
            f'<thead><tr>'
            f'<th style="{TH_S}">Pitcher</th>'
            f'<th style="{TH_S}text-align:center;">Pos</th>'
            f'<th style="{TH_S}text-align:center;">Start</th>'
            f'<th style="{TH_S}text-align:center;">Matchup</th>'
            f'<th style="{TH_S}text-align:center;">Opp OPS</th>'
            f'<th style="{TH_S}text-align:center;">ERA</th>'
            f'<th style="{TH_S}text-align:center;">Brl%</th>'
            f'<th class="hide-mob" style="{TH_S}text-align:center;">K/IP</th>'
            f'<th class="hide-mob" style="{TH_S}text-align:center;">K%</th>'
            f'<th style="{TH_S}text-align:center;">Score</th>'
            f'</tr></thead><tbody>{rows}</tbody></table>'
        )
        table = f'<div style="overflow-x:auto;-webkit-overflow-scrolling:touch;margin-bottom:24px;">{table}</div>'
    else:
        table = f'<p style="color:{MUTED};font-style:italic;margin-bottom:24px;">No FA starters with confirmed upcoming starts.</p>'

    fa_sp_section = section_head("FA Pickup — Starting Pitchers", "Free agents with confirmed upcoming starts · sorted by SP score") + table

    # ── FA: Hitters ────────────────────────────────────────────────────────────
    if fa_hit:
        rows = ""
        for i, r in enumerate(fa_hit):
            bg = f"background:{SURFACE2};" if i % 2 else ""
            rows += (
                f'<tr style="{bg}">'
                f'<td style="{TD_S}font-weight:600;">{team_logo(r.get("Team"))}{r.get("PlayerName","")}{inj_tag(r)}</td>'
                f'<td style="{TDC}color:{MUTED};">{r.get("Position","")}</td>'
                f'<td style="{TDC}">{v(r.get("OPS"), 3)}</td>'
                f'<td style="{TDC}">{v(r.get("wRCplus"), 0)}</td>'
                f'<td style="{TDC}">{v(r.get("xwOBA"), 3)}</td>'
                f'<td style="{TDC}">{v(r.get("HR"), 0)}</td>'
                f'<td style="{TDC}">{v(r.get("SB"), 0)}</td>'
                f'<td class="hide-mob" style="{TDC}">{v(r.get("Barrel_Pct"), 1)}</td>'
                f'<td style="{TDC}">{badge(r["_score"])}</td>'
                f'</tr>'
            )
        table = (
            f'<table style="width:100%;border-collapse:collapse;margin-bottom:0;font-size:13px;">'
            f'<thead><tr>'
            f'<th style="{TH_S}">Hitter</th>'
            f'<th style="{TH_S}text-align:center;">Pos</th>'
            f'<th style="{TH_S}text-align:center;">OPS</th>'
            f'<th style="{TH_S}text-align:center;">wRC+</th>'
            f'<th style="{TH_S}text-align:center;">xwOBA</th>'
            f'<th style="{TH_S}text-align:center;">HR</th>'
            f'<th style="{TH_S}text-align:center;">SB</th>'
            f'<th class="hide-mob" style="{TH_S}text-align:center;">Brl%</th>'
            f'<th style="{TH_S}text-align:center;">Score</th>'
            f'</tr></thead><tbody>{rows}</tbody></table>'
        )
        table = f'<div style="overflow-x:auto;-webkit-overflow-scrolling:touch;margin-bottom:24px;">{table}</div>'
    else:
        table = f'<p style="color:{MUTED};font-style:italic;margin-bottom:24px;">No FA hitters found.</p>'

    fa_hit_section = section_head("FA Pickup — Hitters", "Top available hitters by composite score") + table

    # ── Category Rankings ──────────────────────────────────────────────────────
    CAT_LABELS = [
        ("R","R"), ("HR","HR"), ("RBI","RBI"), ("SB","SB"), ("OPS","OPS"), ("B_SO","BB/K"),
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
        section_head("My Category Rankings", "Season-to-date roto points rank per category") +
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
        section_head("This Week's Category Rankings", f"Week {current_week_num} roto rank · vs. this week's matchup") +
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
                f' {badge(worst["_pscore"])}'
                f'{pos_stat_line(worst, p["pos"])}'
            )
        else:
            player_cell = f'<span style="color:{RED};font-weight:600;">EMPTY</span>'

        top_fa = p["top_fa"][0] if p["top_fa"] else None
        fa_score = top_fa["_pscore"] if top_fa else 0
        worst_score = worst["_pscore"] if worst else 0
        upgrade = top_fa and fa_score > worst_score + 5
        if top_fa:
            fa_cell = (
                f'{team_logo(top_fa.get("Team"), 16)}'
                f'<span style="{"font-weight:600;" if upgrade else ""}'
                f'color:{GREEN if upgrade else MUTED};">'
                f'{top_fa["PlayerName"]}</span> {badge(fa_score)}'
                f'{"&nbsp;&#8593;" if upgrade else ""}'
                f'{pos_stat_line(top_fa, p["pos"])}'
            )
        else:
            fa_cell = f'<span style="color:{MUTED}">—</span>'

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
        f'<table style="width:100%;border-collapse:collapse;margin-bottom:24px;font-size:13px;">'
        f'<thead><tr>'
        f'<th style="{TH_S}text-align:center;">Pos</th>'
        f'<th style="{TH_S}">My Weakest Player</th>'
        f'<th style="{TH_S}text-align:center;">Strength</th>'
        f'<th style="{TH_S}">Best FA Available &nbsp;<span style="color:{GREEN};font-size:9px;">&#8593; = upgrade</span></th>'
        f'</tr></thead><tbody>{pos_rows}</tbody></table>'
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
        luck_rows += (
            f'<tr style="{bg}">'
            f'<td style="{TDC}color:{MUTED};">{row["standing"]}</td>'
            f'<td style="{TD_S}{name_s}">{logo_html}{row["team"]}{me_arrow}</td>'
            f'<td style="{TDC}">{row["wins"]}-{row["losses"]}</td>'
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
        f'<th style="{TH_S}text-align:center;">W-L</th>'
        f'<th style="{TH_S}text-align:center;">Roto #</th>'
        f'<th class="hide-mob" style="{TH_S}text-align:center;">Roto Pts</th>'
        f'<th style="{TH_S}text-align:center;">Luck</th>'
        f'</tr></thead><tbody>{luck_rows}</tbody></table>'
        f'</div>'
    )

    # ── Final assembly ─────────────────────────────────────────────────────────
    body_parts += [
        week_cat_section,
        build_matchup_section(matchup, logos=team_logos),
        pos_section,
        alert_section,
        fa_sp_section,
        fa_hit_section,
        starts_section,
        cat_section,
        luck_section,
    ]
    body = "\n".join(p for p in body_parts if p)

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Guerrero Warfare Daily Digest</title>
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

def send_email(html, subject):
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    if not GMAIL_APP_PASSWORD:
        print("ERROR: GMAIL_APP_PASSWORD not set — add it to .env")
        sys.exit(1)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = FROM_EMAIL
    msg["To"]      = TO_EMAIL
    msg["Cc"]      = CC_EMAIL
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(FROM_EMAIL, GMAIL_APP_PASSWORD)
        smtp.sendmail(FROM_EMAIL, [TO_EMAIL, CC_EMAIL], msg.as_string())
    return 200


# ── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    dry_run    = "--dry-run"    in sys.argv
    no_refresh = "--no-refresh" in sys.argv

    LOG_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("=" * 60)
    print("  Guerrero Warfare Daily Digest")
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

    html    = build_email(snap)
    subject = f"⚾ Fantasy Baseball Digest — {datetime.now().strftime('%b %d')}"

    if dry_run:
        out = Path(__file__).parent / "digest_preview.html"
        out.write_text(html, encoding="utf-8")
        print(f"\n  Dry run — saved to {out}")
        print("\nDone (no email sent).")
        return

    print(f"\n[3/3] Sending to {TO_EMAIL}...")
    send_email(html, subject)
    print("  Sent.")

    log_line = f"{ts} | sent | subject={subject}\n"
    (LOG_DIR / "digest.log").open("a", encoding="utf-8").write(log_line)

    print("\nDone.")


if __name__ == "__main__":
    main()
