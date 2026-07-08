#!/usr/bin/env python3
"""
weekly_recap.py — League Weekly Recap
Reads data/snapshot.json (refreshing if needed), builds a full-league
previous-week recap HTML email, and sends it.

Run manually:   python weekly_recap.py
Dry run:        python weekly_recap.py --dry-run   (saves previews/recap_week_N.html)
Skip refresh:   python weekly_recap.py --no-refresh
"""

import json
import os
import re
import subprocess
import sys
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:
    _ET = None


def _fmt_refresh_time(iso_str):
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
SNAPSHOT   = Path(__file__).parent / "data" / "snapshot.json"

# ── COLORS & STYLES (copied from send_digest.py) ──────────────────────────────
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

TH_S = (f"padding:8px 10px;background:{SURFACE};color:{MUTED};font-size:10px;"
        f"font-weight:700;text-transform:uppercase;letter-spacing:.7px;"
        f"border-bottom:2px solid {BORDER};white-space:nowrap;")
TD_S = (f"padding:7px 10px;border-bottom:1px solid {BORDER};color:{TEXT};"
        f"font-size:13px;vertical-align:middle;")
TDC  = (f"padding:7px 10px;border-bottom:1px solid {BORDER};color:{TEXT};"
        f"font-size:13px;text-align:center;vertical-align:middle;")

_TOP_LINK_DIV = (
    f'<div style="text-align:right;margin:6px 0 0;">'
    f'<a href="#top" style="color:{MUTED};font-size:10px;text-decoration:none;'
    f'font-weight:600;opacity:.7;letter-spacing:.3px;">↑ top</a></div>'
)

_CAT_DISPLAY = {
    "R": "R", "HR": "HR", "RBI": "RBI", "SB": "SB", "OPS": "OPS",
    "B_SO": "B/SO", "K": "K", "QS": "QS", "W": "W",
    "ERA": "ERA", "WHIP": "WHIP", "SVHD": "SV+H",
}
_CAT_ORDER = ["R", "HR", "RBI", "SB", "OPS", "B_SO", "K", "QS", "W", "ERA", "WHIP", "SVHD"]

# ── SHARED HELPERS ─────────────────────────────────────────────────────────────

def _n(val):
    try:
        v = float(val or 0)
        return v if v > 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _name_key(name):
    """Accent-strip + lowercase + drop Jr/Sr/II-V — fuzzy name merge key."""
    if not name:
        return ""
    s = unicodedata.normalize("NFD", str(name))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = s.lower()
    s = re.sub(r"\s+(jr\.?|sr\.?|ii|iii|iv|v)$", "", s)
    return re.sub(r"[^a-z0-9 ]", "", s).strip()


def _fix_mojibake(s):
    """Fix literal \\xNN escape sequences in player names from pybaseball.
    e.g. 'Hern\\xc3\\xa1ndez' (literal backslashes) → 'Hernández'
    """
    if not isinstance(s, str) or "\\x" not in s:
        return s or ""
    try:
        # Replace each literal \xNN with the corresponding Latin-1 character
        expanded = re.sub(r"\\x([0-9a-fA-F]{2})",
                          lambda m: chr(int(m.group(1), 16)), s)
        # The resulting chars (0x80–0xFF) are UTF-8 bytes masquerading as Latin-1;
        # encode back to bytes then decode as UTF-8 to recover the real Unicode.
        return expanded.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


_FANTASY_EMOJI = {
    "Giga Vlad":        ("\U0001f9db", "#6d28d9"),
    "Dumpsta Fire":     ("\U0001f525", "#ea580c"),
    "Kai-Wei Jelly":    ("\U0001f347", "#7e22ce"),
    "The BIG Dumpers":  ("\U0001f4a9", "#78350f"),
    "Walking Wounded":  ("\U0001fa79", "#0369a1"),
}
_BAD_LOGO_DOMAINS = ("mystique-api.fantasy.espn.com", "cdn.citybeat.com")


def _emoji_avatar(team_name, size):
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
    if not url or any(d in url for d in _BAD_LOGO_DOMAINS):
        return _emoji_avatar(team_name, size) if team_name else ""
    return (
        f'<img src="{url}" width="{size}" height="{size}" '
        f'style="vertical-align:middle;border-radius:50%;margin-right:6px;object-fit:contain;" '
        f'alt="">'
    )


def band_divider(label, color=None, anchor=None):
    c = color or MUTED
    anchor_tag = (
        f'<a name="{anchor}" id="{anchor}" style="display:block;position:relative;'
        f'top:-60px;visibility:hidden;"></a>'
    ) if anchor else ""
    top_link = (
        f'<a href="#top" style="color:{MUTED};font-size:9px;font-weight:600;'
        f'text-decoration:none;padding-left:10px;opacity:.7;">↑ top</a>'
    ) if anchor else ""
    right_div = f'<div style="flex:0;">{top_link}</div>' if top_link else ""
    return (
        anchor_tag +
        f'<div style="display:flex;align-items:center;margin:32px 0 22px;">'
        f'<div style="flex:1;height:1px;background:{BORDER};"></div>'
        f'<span style="padding:0 14px;color:{c};font-size:10px;font-weight:700;'
        f'letter-spacing:2px;text-transform:uppercase;">{label}</span>'
        f'<div style="flex:1;height:1px;background:{BORDER};"></div>'
        + right_div +
        f'</div>'
    )


def _nav_bar():
    links = [
        ("Highlights",  "#band-highlights"),
        ("My Matchup", "#sec-matchup"),
        ("Scoreboard",  "#band-scoreboard"),
        ("Roto",        "#band-roto"),
        ("Performers",  "#band-performers"),
        ("Standings",   "#band-standings"),
        ("Trajectory",  "#band-trajectory"),
    ]
    pills = "".join(
        f'<a href="{href}" style="color:{ACCENT};background:{ACCENT}1a;'
        f'border:1px solid {ACCENT}44;border-radius:12px;padding:3px 10px;'
        f'font-size:10px;font-weight:600;text-decoration:none;white-space:nowrap;'
        f'display:inline-block;margin:2px 3px;">{label}</a>'
        for label, href in links
    )
    return (
        f'<a name="top" id="top" style="display:block;height:0;visibility:hidden;"></a>'
        f'<div style="text-align:center;margin:0 0 20px;padding:6px 0;">'
        f'{pills}'
        f'</div>'
    )


def section_head(title, sub=""):
    subtitle = (f'<div style="color:{MUTED};font-size:11px;margin-top:2px;">{sub}</div>'
                if sub else "")
    return (
        f'<div style="border-left:3px solid {ACCENT};padding-left:11px;margin:0 0 10px 0;">'
        f'<div style="color:{TEXT};font-size:12px;font-weight:700;text-transform:uppercase;'
        f'letter-spacing:.6px;">{title}</div>'
        f'{subtitle}</div>'
    )


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
            "luck":      rr - s["standing"],
            "logo_url":  s.get("logo_url", ""),
        })
    return sorted(result, key=lambda r: r["standing"])


# ── FORMAT HELPERS ─────────────────────────────────────────────────────────────

def _fmt_cat(val, cat):
    """Format a raw category value for display."""
    dec = 3 if cat == "OPS" else (2 if cat in {"ERA", "WHIP"} else 0)
    try:
        f = f"{float(val):.{dec}f}"
        # Drop leading zero for sub-1 stats (OPS, ERA sub-1)
        if dec > 0 and float(val) < 1.0:
            f = f.lstrip("0") or "0"
        return f
    except (TypeError, ValueError):
        return "—"


def _fmt_ops(v):
    """Format sub-1 rates (AVG, OPS): strip leading zero."""
    try:
        s = f"{float(v):.3f}"
        return s.lstrip("0") or ".000"
    except (TypeError, ValueError):
        return "—"


def _ordinal(n):
    n = int(n)
    sfx = {1: "st", 2: "nd", 3: "rd"}.get(n % 10 if n % 100 not in (11, 12, 13) else 0, "th")
    return f"{n}{sfx}"


_MLB_ABBREV_ESPN = {
    "TBR": "tb",  "KCR": "kc",  "SDP": "sd",  "SFG": "sf",
    "WSN": "wsh", "CHW": "chw", "CHC": "chc", "LAA": "laa",
    "WSH": "wsh", "ARI": "ari", "ATH": "oak",
}

def _mlb_logo(abbrev, size=16):
    if not abbrev:
        return ""
    norm = _MLB_ABBREV_ESPN.get(str(abbrev).upper(), str(abbrev).lower())
    return (
        f'<img src="https://a.espncdn.com/i/teamlogos/mlb/500/{norm}.png" '
        f'width="{size}" height="{size}" '
        f'style="vertical-align:middle;margin-left:4px;object-fit:contain;" alt="{abbrev}">'
    )


def _fmt_stat(key, val):
    """Format a performer-table stat value, returning (display_str, color)."""
    try:
        v = float(val or 0)
    except (TypeError, ValueError):
        return "—", MUTED

    if v == 0 and key not in ("ERA", "WHIP", "OPS"):
        return "—", MUTED

    if key == "OPS":
        s = f"{v:.3f}".lstrip("0") or ".000"
        color = GREEN if v >= 0.900 else (YELLOW if v >= 0.800 else TEXT)
        return s, color
    if key == "ERA":
        if v <= 0:
            return "—", MUTED
        color = GREEN if v <= 2.50 else (RED if v > 5.00 else TEXT)
        return f"{v:.2f}", color
    if key == "WHIP":
        if v <= 0:
            return "—", MUTED
        color = GREEN if v <= 1.00 else (RED if v > 1.40 else TEXT)
        return f"{v:.2f}", color
    if key == "IP":
        return f"{v:.1f}" if v > 0 else "—", TEXT
    # Integer stats
    return str(int(v)), TEXT


# ── SECTION BUILDERS ──────────────────────────────────────────────────────────

def build_my_matchup(prev_matchup, logos):
    """Guerrero Warfare's previous week result with full 12-category breakdown."""
    if not prev_matchup or not prev_matchup.get("categories"):
        return ""

    week    = prev_matchup.get("week", "")
    opp     = prev_matchup.get("opp_team", "Opponent")
    my_name = prev_matchup.get("my_team", MY_TEAM)
    wins    = prev_matchup.get("wins", 0)
    losses  = prev_matchup.get("losses", 0)
    ties    = prev_matchup.get("ties", 0)
    cats    = {c["cat"]: c for c in prev_matchup.get("categories", [])}

    if wins > losses:
        outcome_color, outcome_word = GREEN, "WIN"
    elif losses > wins:
        outcome_color, outcome_word = RED, "LOSS"
    else:
        outcome_color, outcome_word = TEXT, "TIE"

    score_str = f"{wins}–{losses}" + (f"–{ties}" if ties else "")

    th = (f"padding:3px 5px;text-align:center;font-size:10px;font-weight:700;"
          f"color:{MUTED};text-transform:uppercase;letter-spacing:0;"
          f"border-bottom:1px solid {BORDER};white-space:nowrap;")
    td = "padding:4px 5px;text-align:center;font-size:10px;font-weight:500;white-space:nowrap;"
    VAL_COLOR = "#94a3b8"

    header_cells = f'<th style="{th}text-align:left;min-width:36px;"></th>'
    for i, cat in enumerate(_CAT_ORDER):
        lbl = _CAT_DISPLAY.get(cat, cat)
        c   = cats.get(cat, {})
        res = c.get("result", "T")
        col = GREEN if res == "W" else (RED if res == "L" else MUTED)
        sep = f"border-left:1px solid {BORDER};" if i == 6 else ""
        header_cells += (
            f'<th style="{th}{sep}color:{col};border-bottom:2px solid {col};">{lbl}</th>'
        )

    def _data_row(label, label_color, val_key, win_result):
        row = (f'<td style="{td}text-align:left;color:{label_color};font-weight:700;'
               f'font-size:11px;">{label}</td>')
        for i, cat in enumerate(_CAT_ORDER):
            c   = cats.get(cat, {})
            val = c.get(val_key, 0)
            res = c.get("result", "T")
            lb  = f"border-left:1px solid {BORDER};" if i == 6 else ""
            val_str = _fmt_cat(val, cat)
            if res == win_result:
                val_str = (f'<span style="outline:1px solid {TEXT}44;outline-offset:3px;'
                           f'border-radius:3px;display:inline-block;">{val_str}</span>')
            row += f'<td style="{td}color:{VAL_COLOR};{lb}">{val_str}</td>'
        return f"<tr>{row}</tr>"

    my_logo  = fantasy_logo(logos.get(" ".join(my_name.split()), ""), 18, my_name)
    opp_logo = fantasy_logo(logos.get(" ".join(opp.split()), ""), 18, opp)

    table = (
        f'<div style="overflow-x:auto;-webkit-overflow-scrolling:touch;margin-top:10px;">'
        f'<table style="width:100%;border-collapse:collapse;min-width:420px;">'
        f'<thead><tr>{header_cells}</tr></thead>'
        f'<tbody>'
        + _data_row(my_logo + "Me", ACCENT, "my_val",  "W")
        + _data_row(opp_logo + "Opp", TEXT, "opp_val", "L")
        + "</tbody></table></div>"
    )

    return (
        section_head(f"Guerrero Warfare — Week {week}", f"vs. {opp}") +
        f'<div style="background:{SURFACE};border:1px solid {BORDER};border-radius:6px;'
        f'padding:12px 16px;margin-bottom:16px;">'
        f'<div style="display:flex;align-items:baseline;gap:10px;">'
        f'<span style="color:{outcome_color};font-weight:800;font-size:18px;">{outcome_word}</span>'
        f'<span style="color:{TEXT};font-weight:700;font-size:15px;">{score_str}</span>'
        f'<span style="color:{MUTED};font-size:12px;">vs. {opp}</span>'
        f'</div>'
        + table +
        f'</div>'
    )


def build_lineup_efficiency(eff):
    """MY team's start/sit opportunity cost last week: batter production stranded on
    the bench (net of the bat I'd have sat to play him) + active-slot pitcher blowups
    that counted then got dropped. Data from fetch_data.get_lineup_efficiency."""
    if not eff:
        return ""
    bench   = eff.get("bench") or []
    blowups = eff.get("blowups") or []
    if not bench and not blowups:
        return ""

    net = eff.get("net") or {}
    parts = []

    # ── headline: net recoverable ──
    net_bits = [f"{net.get(c, 0):+.0f} {c}" for c in ("HR", "RBI", "R", "SB") if net.get(c, 0)]
    if net_bits:
        parts.append(
            f'<div style="background:{SURFACE};border:1px solid {RED}55;border-radius:6px;'
            f'padding:12px 16px;margin-bottom:14px;">'
            f'<div style="color:{RED};font-weight:800;font-size:13px;letter-spacing:.3px;">'
            f'LEFT ON THE BENCH &nbsp;{" &middot; ".join(net_bits)}</div>'
            f'<div style="color:{MUTED};font-size:11px;margin-top:3px;">'
            f'Net of the bat you\'d have benched to start him &mdash; production that never '
            f'counted toward your categories.</div></div>'
        )
    elif bench:
        parts.append(
            f'<div style="color:{GREEN};font-weight:700;font-size:12px;margin-bottom:12px;">'
            f'Bench production was covered &mdash; every startable bat was in your lineup.</div>'
        )

    # ── per-player bench leakage ──
    for b in bench:
        slash = f"{b['H']}-{b['AB']}"
        tot = " &middot; ".join(f"{b[c]} {c}" for c in ("R", "HR", "RBI", "SB") if b[c])
        parts.append(
            f'<div style="background:{SURFACE};border:1px solid {BORDER};border-radius:6px;'
            f'padding:10px 14px;margin-bottom:8px;">'
            f'<div><span style="color:{TEXT};font-weight:700;font-size:13px;">{b["name"]}</span>'
            f'<span style="color:{MUTED};font-size:11px;"> &nbsp;{slash} on the bench &nbsp;&mdash;&nbsp; {tot}</span></div>'
            + "".join(
                f'<div style="color:{MUTED};font-size:11px;margin-top:3px;">'
                f'<span style="color:{YELLOW};">&rsaquo;</span> {d["date"]} '
                f'<span style="color:{TEXT};">{d["line"]}</span>'
                + (f' ({d["extra"]})' if d.get("extra") else '')
                + f' <span style="color:{MUTED};">[{d["tag"]}]</span></div>'
                for d in b.get("days", [])
            )
            + '</div>'
        )

    # ── pitcher blowups that counted ──
    if blowups:
        parts.append(
            f'<div style="color:{MUTED};font-size:11px;font-weight:700;text-transform:uppercase;'
            f'letter-spacing:.5px;margin:14px 0 6px;">Active-slot blowups (ER/WHIP counted)</div>'
        )
        for p in blowups:
            drop = ''
            if p.get("drop_when"):
                drop = (f' <span style="color:{RED};font-weight:700;">dropped {p["drop_when"]}</span>'
                        f'<span style="color:{MUTED};"> &mdash; imploded then cut, damage already banked</span>')
            parts.append(
                f'<div style="background:{SURFACE};border:1px solid {BORDER};border-radius:6px;'
                f'padding:9px 14px;margin-bottom:7px;">'
                f'<span style="color:{TEXT};font-weight:700;font-size:12px;">{p["name"]}</span>'
                f'<span style="color:{MUTED};font-size:11px;"> &nbsp;{p["date"]} &nbsp;'
                f'{p["ip"]} IP, <span style="color:{RED};font-weight:700;">{p["er"]} ER</span>, '
                f'{p["k"]} K (+{p["h"]} H, {p["bb"]} BB)</span>{drop}</div>'
            )

    return "".join(parts)


def build_league_scoreboard(all_prev_matchups, logos):
    """All 6 matchups with full 12-category breakdown, one card each."""
    if not all_prev_matchups:
        return ""

    # De-duplicate: keep one entry per pair (sort team names to pick canonical side)
    seen, pairs = set(), []
    for matchup in all_prev_matchups.values():
        pair = tuple(sorted([matchup.get("my_team", ""), matchup.get("opp_team", "")]))
        if pair not in seen:
            seen.add(pair)
            pairs.append(matchup)

    # Guerrero Warfare's matchup first, rest sorted alphabetically
    my_key = " ".join(MY_TEAM.split())
    pairs.sort(key=lambda m: (0 if " ".join(m.get("my_team", "").split()) == my_key else 1,
                               m.get("my_team", "")))

    week = pairs[0].get("week", "") if pairs else ""

    th = (f"padding:3px 5px;text-align:center;font-size:9px;font-weight:700;"
          f"color:{MUTED};text-transform:uppercase;letter-spacing:0;"
          f"border-bottom:1px solid {BORDER};white-space:nowrap;background:{SURFACE};")
    td_val = "padding:4px 5px;text-align:center;font-size:10px;font-weight:500;white-space:nowrap;"
    VAL_COLOR = "#94a3b8"

    blocks = []
    for matchup in pairs:
        team_a = matchup.get("my_team", "")
        team_b = matchup.get("opp_team", "")
        cats   = {c["cat"]: c for c in matchup.get("categories", [])}
        w_a, l_a, t_a = matchup.get("wins", 0), matchup.get("losses", 0), matchup.get("ties", 0)
        is_my_a = " ".join(team_a.split()) == my_key

        logo_a = fantasy_logo(logos.get(" ".join(team_a.split()), ""), 18, team_a)
        logo_b = fantasy_logo(logos.get(" ".join(team_b.split()), ""), 18, team_b)
        col_a = ACCENT if is_my_a else TEXT

        score_a = f"{w_a}–{l_a}" + (f"–{t_a}" if t_a else "")
        score_b = f"{l_a}–{w_a}" + (f"–{t_a}" if t_a else "")

        # First column is the team label (logo + name + W–L–T); category headers follow.
        header_cells = f'<th style="{th}text-align:left;padding-left:8px;min-width:130px;"></th>'
        for i, cat in enumerate(_CAT_ORDER):
            lbl = _CAT_DISPLAY.get(cat, cat)
            sep = f"border-left:1px solid {BORDER};" if i == 6 else ""
            # Neutral header — win/loss coloring only makes sense from one team's
            # POV; the full-league scoreboard is even-handed (outlined value = winner).
            header_cells += (
                f'<th style="{th}{sep}color:{MUTED};border-bottom:2px solid {BORDER};">{lbl}</th>'
            )

        def _row(logo, label, label_color, score, val_key, win_result, cats=cats):
            label_cell = (
                f'<td style="{td_val}text-align:left;padding-left:8px;'
                f'overflow:hidden;text-overflow:ellipsis;">'
                f'{logo}'
                f'<span style="color:{label_color};font-weight:700;font-size:11px;'
                f'vertical-align:middle;">{label}</span>'
                f'<span style="color:{MUTED};font-weight:700;font-size:10px;'
                f'vertical-align:middle;margin-left:5px;">{score}</span>'
                f'</td>'
            )
            row = label_cell
            for i, cat in enumerate(_CAT_ORDER):
                c   = cats.get(cat, {})
                val = c.get(val_key, 0)
                res = c.get("result", "T")
                lb  = f"border-left:1px solid {BORDER};" if i == 6 else ""
                val_str = _fmt_cat(val, cat)
                if res == win_result:
                    val_str = (f'<span style="outline:1px solid {TEXT}44;outline-offset:3px;'
                               f'border-radius:3px;display:inline-block;">{val_str}</span>')
                row += f'<td style="{td_val}color:{VAL_COLOR};{lb}">{val_str}</td>'
            return f"<tr>{row}</tr>"

        # Fixed layout + colgroup so every matchup card shares the SAME column
        # positions (label col + 12 equal cat cols) — otherwise each table sizes
        # its first column to its own team-name length and the axis drifts card-to-card.
        colgroup = ('<colgroup><col style="width:150px;">'
                    + '<col>' * len(_CAT_ORDER) + '</colgroup>')
        cat_table = (
            f'<div style="overflow-x:auto;-webkit-overflow-scrolling:touch;">'
            f'<table style="width:100%;border-collapse:collapse;min-width:520px;'
            f'table-layout:fixed;">'
            f'{colgroup}'
            f'<thead><tr>{header_cells}</tr></thead>'
            f'<tbody>'
            + _row(logo_a, team_a, col_a, score_a, "my_val",  "W")
            + _row(logo_b, team_b, TEXT,  score_b, "opp_val", "L")
            + "</tbody></table></div>"
        )

        blocks.append(
            f'<div style="background:{SURFACE};border:1px solid {BORDER};border-radius:6px;'
            f'overflow:hidden;margin-bottom:14px;">'
            + f'<div style="padding:4px 0 8px;">{cat_table}</div>'
            f'</div>'
        )

    return (
        section_head(f"League Scoreboard — Week {week}",
                     "All 6 matchups \xb7 outlined value = category winner") +
        "\n".join(blocks)
    )


def build_weekly_roto_rankings(roto, prev_week, logos):
    """Teams ranked by roto score for the previous week only (not season total)."""
    week_rows = [r for r in roto if int(r.get("Week") or 0) == int(prev_week)]
    if not week_rows:
        return ""

    # Find category leaders (team with highest rank-points for that cat this week)
    cat_leaders: dict[str, list[str]] = {}
    for cat in _CAT_ORDER:
        pt_field = f"{cat}_Points"
        best_pts = max((float(r.get(pt_field) or 0) for r in week_rows), default=0)
        if best_pts > 0:
            for r in week_rows:
                if float(r.get(pt_field) or 0) == best_pts:
                    cat_leaders.setdefault(r["Team"], []).append(cat)

    ranked = sorted(week_rows, key=lambda r: -float(r.get("Roto_Score") or 0))
    n      = len(ranked)
    my_key = " ".join(MY_TEAM.split())

    # Compact local styles — tighter than global TH_S/TDC to avoid horizontal scroll
    _th  = TH_S.replace("padding:8px 10px", "padding:3px 5px").replace("font-size:10px", "font-size:9px")
    _tdc = TDC.replace("padding:7px 10px", "padding:3px 5px").replace("font-size:13px", "font-size:10px")
    _tds = TD_S.replace("padding:7px 10px", "padding:3px 5px").replace("font-size:13px", "font-size:10px")

    rows_html = ""
    for rank, r in enumerate(ranked, 1):
        team  = r.get("Team", "")
        score = float(r.get("Roto_Score") or 0)
        led   = cat_leaders.get(team, [])
        is_my = " ".join(team.split()) == my_key

        if rank <= 3:
            row_bg = f"background:rgba(34,197,94,0.07);"
        elif rank >= n - 2:
            row_bg = f"background:rgba(239,68,68,0.07);"
        else:
            row_bg = ""

        logo = fantasy_logo(logos.get(" ".join(team.split()), ""), 16, team)
        rank_color = GREEN if rank <= 3 else (RED if rank >= n - 2 else MUTED)

        led_pills = "".join(
            f'<span style="background:{ACCENT}22;color:{ACCENT};padding:1px 4px;'
            f'border-radius:10px;font-size:8px;font-weight:700;margin-left:2px;">'
            f'{_CAT_DISPLAY.get(c, c)}</span>'
            for c in led
        )

        # All 12 cat columns, 5-tier rank heat-map (1st→2nd→mid→11th→last)
        stat_cells = ""
        for cat in _CAT_ORDER:
            pts     = float(r.get(f"{cat}_Points") or 0)
            val_str = _fmt_cat(r.get(cat, 0), cat)
            if val_str == "—":
                color, badge = MUTED, False
            elif pts == n:          # rank 1 — best
                color, badge = GREEN, True
            elif pts == n - 1:      # rank 2 — 2nd best
                color, badge = "#86efac", False
            elif pts == 1:          # rank last — worst
                color, badge = RED, True
            elif pts == 2:          # rank 2nd worst
                color, badge = YELLOW, False
            else:                   # ranks 3–10 — middle pack
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
            f'color:{ACCENT if is_my else TEXT};white-space:nowrap;">'
            f'{logo}{team}'
            + (f'<span style="margin-left:4px;">{led_pills}</span>' if led_pills else "")
            + f'</td>'
            f'<td style="{_tdc}font-weight:700;">{score:.1f}</td>'
            + stat_cells +
            f'</tr>'
        )

    stat_headers = "".join(
        f'<th style="{_th}text-align:center;">{_CAT_DISPLAY.get(c, c)}</th>'
        for c in _CAT_ORDER
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
        section_head(f"Weekly Roto Rankings — Week {prev_week}",
                     "Roto score for this week only \xb7 bright green = #1 \xb7 light green = #2 \xb7 amber = #11 \xb7 red = #12") +
        table
    )


def _performer_table(players, stat_keys, stat_labels):
    """Generic performer table — one row per player."""
    if not players:
        return ""

    header = (
        f'<th style="{TH_S}">Player</th>'
        + "".join(f'<th style="{TH_S}text-align:center;">{lbl}</th>' for lbl in stat_labels)
    )
    rows = ""
    for r in players:
        team = r.get("FantasyTeam", "")
        logo = r.get("_logo", "")
        name = _fix_mojibake(r.get("PlayerName", ""))
        pos  = r.get("Position", "")

        is_mine = " ".join(team.split()) == " ".join(MY_TEAM.split())
        name_color = ACCENT if is_mine else TEXT
        name_cell = (
            f'<td style="{TD_S}">'
            f'{logo}'
            f'<span style="font-weight:600;color:{name_color};">{name}</span>'
            + (f'<span style="color:{MUTED};font-size:10px;margin-left:4px;">{pos}</span>' if pos else "")
            + (f'<br><span style="color:{MUTED};font-size:10px;">{team}</span>' if team else "")
            + f'</td>'
        )
        stat_cells = ""
        for key in stat_keys:
            display_str, color = _fmt_stat(key, r.get(key))
            stat_cells += f'<td style="{TDC}"><span style="color:{color};">{display_str}</span></td>'

        rows += f"<tr>{name_cell}{stat_cells}</tr>"

    return (
        f'<div style="overflow-x:auto;-webkit-overflow-scrolling:touch;">'
        f'<table style="width:100%;border-collapse:collapse;">'
        f'<thead><tr>{header}</tr></thead>'
        f'<tbody>{rows}</tbody></table></div>'
    )


def build_top_performers(recent_hitting, recent_pitching, hitters, pitchers, logos, snap_year=2026,
                         week_dates=""):
    """Top rostered performers of the week + hot free agents.

    `recent_hitting`/`recent_pitching` are the exact prev-week matchup window
    (`prev_week_hitting`/`prev_week_pitching`) so the timeline matches the rest
    of the recap — NOT the rolling 7-day/15-day windows.
    """

    # Build name-keyed season-row lookups for FantasyTeam tagging
    h_exact, h_keyed = {}, {}
    for r in hitters:
        if int(r.get("Dataset", 0) or 0) == snap_year and r.get("PlayerName"):
            n = r["PlayerName"]
            h_exact.setdefault(n, r)
            h_keyed.setdefault(_name_key(n), r)

    p_exact, p_keyed = {}, {}
    for r in pitchers:
        if int(r.get("Dataset", 0) or 0) == snap_year and r.get("PlayerName"):
            n = r["PlayerName"]
            p_exact.setdefault(n, r)
            p_keyed.setdefault(_name_key(n), r)

    def _enrich_h(rh):
        raw_n = rh.get("PlayerName", "")
        n  = _fix_mojibake(raw_n)   # prev_week_* names arrive mojibaked (e.g. "Eury PÃ©rez")
        s  = h_exact.get(raw_n) or h_exact.get(n) or h_keyed.get(_name_key(n)) or {}
        ft = (s.get("FantasyTeam") or "").strip()
        return {**rh, "PlayerName": n, "FantasyTeam": ft, "Position": s.get("Position", ""),
                "_logo": fantasy_logo(logos.get(" ".join(ft.split()), ""), 18, ft) if ft else ""}

    def _enrich_p(rp):
        raw_n = rp.get("PlayerName", "")
        n  = _fix_mojibake(raw_n)
        s  = p_exact.get(raw_n) or p_exact.get(n) or p_keyed.get(_name_key(n)) or {}
        ft = (s.get("FantasyTeam") or "").strip()
        return {**rp, "PlayerName": n, "FantasyTeam": ft, "Position": s.get("Position", ""),
                "_logo": fantasy_logo(logos.get(" ".join(ft.split()), ""), 18, ft) if ft else ""}

    def _is_fa(ft):
        return not ft or ft in ("Free Agent", "FA")

    def _era_key(r):
        e = _n(r.get("ERA"))
        return e if e > 0 else 99.0

    # Hitters: min 10 AB
    enriched_h = [_enrich_h(r) for r in recent_hitting if _n(r.get("AB")) >= 10]
    rostered_h = sorted([r for r in enriched_h if not _is_fa(r["FantasyTeam"])],
                        key=lambda r: -_n(r.get("OPS")))[:10]
    fa_h       = sorted([r for r in enriched_h if _is_fa(r["FantasyTeam"])],
                        key=lambda r: -_n(r.get("OPS")))[:5]

    # Pitchers: min 8 IP
    enriched_p = [_enrich_p(r) for r in recent_pitching if _n(r.get("IP")) >= 8]
    rostered_p = sorted([r for r in enriched_p if not _is_fa(r["FantasyTeam"])],
                        key=_era_key)[:10]
    fa_p       = sorted([r for r in enriched_p if _is_fa(r["FantasyTeam"])],
                        key=_era_key)[:5]

    HIT_KEYS   = ["OPS", "HR", "RBI", "R", "SB"]
    HIT_LABELS = ["OPS", "HR", "RBI", "R", "SB"]
    PIT_KEYS   = ["ERA", "WHIP", "IP", "K"]
    PIT_LABELS = ["ERA", "WHIP", "IP", "K"]

    _window = f"{week_dates} \xb7 " if week_dates else ""
    out = section_head("Top Performers",
                       f"{_window}Hitting: min 10 AB \xb7 Pitching: min 8 IP")

    if rostered_h:
        out += (f'<div style="font-size:10px;font-weight:700;color:{MUTED};text-transform:uppercase;'
                f'letter-spacing:.7px;margin:12px 0 6px;">Rostered Hitters — by OPS</div>'
                + _performer_table(rostered_h, HIT_KEYS, HIT_LABELS))

    if rostered_p:
        out += (f'<div style="font-size:10px;font-weight:700;color:{MUTED};text-transform:uppercase;'
                f'letter-spacing:.7px;margin:18px 0 6px;">Rostered Pitchers — by ERA</div>'
                + _performer_table(rostered_p, PIT_KEYS, PIT_LABELS))

    if fa_h or fa_p:
        out += (
            f'<div style="margin:22px 0 10px;padding:10px 14px;background:{SURFACE};'
            f'border:1px solid {YELLOW}44;border-radius:6px;">'
            f'<div style="color:{YELLOW};font-size:10px;font-weight:700;text-transform:uppercase;'
            f'letter-spacing:.7px;margin-bottom:10px;">Hot Free Agents — worth picking up</div>'
        )
        if fa_h:
            out += (f'<div style="font-size:10px;font-weight:700;color:{MUTED};text-transform:uppercase;'
                    f'letter-spacing:.7px;margin-bottom:4px;">Hitters</div>'
                    + _performer_table(fa_h, HIT_KEYS, HIT_LABELS))
        if fa_p:
            out += (f'<div style="font-size:10px;font-weight:700;color:{MUTED};text-transform:uppercase;'
                    f'letter-spacing:.7px;margin:12px 0 4px;">Pitchers</div>'
                    + _performer_table(fa_p, PIT_KEYS, PIT_LABELS))
        out += '</div>'

    return out


def build_standings_section(roto_rows, standings, logos):
    """Current standings with season-long luck metric."""
    data = luck_standings(roto_rows, standings)
    my_key = " ".join(MY_TEAM.split())

    rows_html = ""
    for r in data:
        team  = r["team"]
        is_my = " ".join(team.split()) == my_key
        luck  = r["luck"]
        luck_color = GREEN if luck > 0 else (RED if luck < 0 else MUTED)
        luck_str = (f'<span style="color:{luck_color};font-weight:700;">'
                    f'{"+" if luck > 0 else ""}{luck}</span>')
        win_pct = ((r["wins"] + r["ties"] * 0.5) /
                   max(r["wins"] + r["losses"] + r["ties"], 1))
        logo = fantasy_logo(r.get("logo_url", ""), 22, team)
        bg   = f"background:{ACCENT}18;" if is_my else ""

        rows_html += (
            f'<tr style="{bg}">'
            f'<td style="{TDC}color:{MUTED};font-weight:700;">{r["standing"]}</td>'
            f'<td style="{TD_S}font-weight:{"800" if is_my else "600"};'
            f'color:{ACCENT if is_my else TEXT};">{logo}{team}</td>'
            f'<td style="{TDC}">{r["wins"]}–{r["losses"]}'
            + (f'–{r["ties"]}' if r["ties"] else "")
            + f'</td>'
            f'<td style="{TDC}">{win_pct:.3f}</td>'
            f'<td style="{TDC}color:{MUTED};">{r["roto_rank"]}</td>'
            f'<td style="{TDC}color:{MUTED};">{r["roto_pts"]}</td>'
            f'<td style="{TDC}">{luck_str}</td>'
            f'</tr>'
        )

    header_row = (
        f'<th style="{TH_S}text-align:center;">Rank</th>'
        f'<th style="{TH_S}">Team</th>'
        f'<th style="{TH_S}text-align:center;">W–L–T</th>'
        f'<th style="{TH_S}text-align:center;">Win%</th>'
        f'<th style="{TH_S}text-align:center;">Roto Rank</th>'
        f'<th style="{TH_S}text-align:center;">Roto Pts</th>'
        f'<th style="{TH_S}text-align:center;">Luck</th>'
    )

    table = (
        f'<table style="width:100%;border-collapse:collapse;">'
        f'<thead><tr>{header_row}</tr></thead>'
        f'<tbody>{rows_html}</tbody></table>'
    )

    return (
        section_head("Standings & Luck",
                     "Luck = roto rank − actual W-L rank \xb7 positive = W-L better than roto predicts") +
        table
    )


def build_trajectory(weekly_results, standings, logos):
    """Season W/L grid: teams as rows, weeks as columns."""
    if not weekly_results or not standings:
        return ""

    teams = [s["team_name"] for s in sorted(standings, key=lambda s: s["standing"])]
    weeks = sorted(weekly_results.keys(), key=lambda w: int(w))
    if not weeks:
        return ""

    my_key = " ".join(MY_TEAM.split())

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

        team_cell = (
            f'<td style="{TD_S}font-weight:{"800" if is_my else "600"};'
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
        f'<th style="{TH_S}">Team</th>'
        + week_headers
        + f'<th style="{TH_S}text-align:center;">Streak</th>'
    )

    table = (
        f'<div style="overflow-x:auto;-webkit-overflow-scrolling:touch;">'
        f'<table style="width:100%;border-collapse:collapse;">'
        f'<thead><tr>{header_row}</tr></thead>'
        f'<tbody>{rows_html}</tbody></table></div>'
    )

    return (
        section_head("Season Trajectory",
                     "W/L/T by week \xb7 current streak in final column") +
        table
    )


# ── COMMISSIONER'S STORY ──────────────────────────────────────────────────────

def build_commissioner_story(roto, prev_week, recent_hitting, recent_pitching,
                              hitters, pitchers, standings, logos,
                              weekly_results=None, snap_year=2026):
    """Weekly highlights: roto winner · hitter/pitcher/FA of the week."""

    # Enrichment lookups — mirror build_top_performers pattern
    h_exact, h_keyed = {}, {}
    for r in hitters:
        if int(r.get("Dataset", 0) or 0) == snap_year and r.get("PlayerName"):
            n = r["PlayerName"]
            h_exact.setdefault(n, r)
            h_keyed.setdefault(_name_key(n), r)
    p_exact, p_keyed, p_15d = {}, {}, {}
    for r in pitchers:
        n = r.get("PlayerName")
        if not n:
            continue
        ds = int(r.get("Dataset", 0) or 0)
        if ds == snap_year:
            p_exact.setdefault(n, r)
            p_keyed.setdefault(_name_key(n), r)
        if ds == 15:
            p_15d.setdefault(n, r)
            p_15d.setdefault(_name_key(n), r)

    def _enrich_h(rh):
        raw_n = rh.get("PlayerName", "")
        n  = _fix_mojibake(raw_n)
        s  = h_exact.get(raw_n) or h_keyed.get(_name_key(n)) or {}
        ft = (s.get("FantasyTeam") or "").strip()
        mlb = rh.get("Team") or s.get("Team", "")
        return {**rh, "PlayerName": n, "FantasyTeam": ft,
                "Position": s.get("Position", ""), "MLBTeam": mlb,
                "_logo": fantasy_logo(logos.get(" ".join(ft.split()), ""), 18, ft) if ft else ""}

    def _enrich_p(rp):
        raw_n = rp.get("PlayerName", "")
        n  = _fix_mojibake(raw_n)
        s  = p_exact.get(raw_n) or p_keyed.get(_name_key(n)) or {}
        fp15 = p_15d.get(raw_n) or p_15d.get(_name_key(n)) or {}
        ft = (s.get("FantasyTeam") or "").strip()
        mlb = rp.get("Team") or s.get("Team", "")
        # K and QS come from FP 15-day data when absent from recent_pitching
        k_val  = rp.get("K") if rp.get("K") is not None else fp15.get("K")
        qs_val = rp.get("QS") if rp.get("QS") is not None else fp15.get("QS")
        return {**rp, "PlayerName": n, "FantasyTeam": ft,
                "Position": s.get("Position", ""), "MLBTeam": mlb,
                "K": k_val, "QS": qs_val,
                "_logo": fantasy_logo(logos.get(" ".join(ft.split()), ""), 18, ft) if ft else ""}

    def _is_fa(ft):
        return not ft or ft in ("Free Agent", "FA")

    def _is_mine(ft):
        return " ".join((ft or "").split()) == " ".join(MY_TEAM.split())

    # ── Roto winner ───────────────────────────────────────────────────────────
    week_rows = [r for r in roto if int(r.get("Week") or 0) == int(prev_week)]
    winner_team, winner_score = "", 0.0
    if week_rows:
        best = max(week_rows, key=lambda r: float(r.get("Roto_Score") or 0))
        winner_team  = best.get("Team", "")
        winner_score = float(best.get("Roto_Score") or 0)

    # ── Hitter / Pitcher / FA of the week ────────────────────────────────────
    def _era_key(r):
        e = _n(r.get("ERA"))
        return e if e > 0 else 99.0

    enriched_h = [_enrich_h(r) for r in recent_hitting  if _n(r.get("AB")) >= 10]
    enriched_p = [_enrich_p(r) for r in recent_pitching if _n(r.get("IP")) >=  8]

    rostered_h = sorted([r for r in enriched_h if not _is_fa(r["FantasyTeam"])],
                        key=lambda r: -_n(r.get("OPS")))
    rostered_p = sorted([r for r in enriched_p if not _is_fa(r["FantasyTeam"])],
                        key=_era_key)
    fa_h       = sorted([r for r in enriched_h if  _is_fa(r["FantasyTeam"])],
                        key=lambda r: -_n(r.get("OPS")))

    potw_hit = rostered_h[0] if rostered_h else None
    potw_pit = rostered_p[0] if rostered_p else None
    fa_potw  = fa_h[0]       if fa_h       else None

    if not any([winner_team, potw_hit, potw_pit, fa_potw]):
        return ""

    # ── Prose helpers ─────────────────────────────────────────────────────────
    def _b(text, color=None):
        c = color or TEXT
        return f'<strong style="color:{c};">{text}</strong>'

    def _slash(r):
        avg = _n(r.get("AVG") or r.get("BA"))
        obp = _n(r.get("OBP"))
        slg = _n(r.get("SLG"))
        if avg and obp and slg:
            return f"{_fmt_ops(avg)}/{_fmt_ops(obp)}/{_fmt_ops(slg)}"
        if obp and slg:
            return f"{_fmt_ops(obp)}/{_fmt_ops(slg)} OBP/SLG"
        ops = _n(r.get("OPS"))
        return f"{_fmt_ops(ops)} OPS" if ops else ""

    def _hit_counts(r):
        parts, ab = [], int(_n(r.get("AB")))
        for val, lbl in [(int(_n(r.get("R"))),  "R"),
                         (int(_n(r.get("HR"))), "HR"),
                         (int(_n(r.get("RBI"))),"RBI"),
                         (int(_n(r.get("SB"))), "SB")]:
            if val: parts.append(f"{val} {lbl}")
        s = ", ".join(parts)
        if ab: s += f" in {ab} AB"
        return s

    def _para(text):
        return (
            f'<p style="color:{TEXT};font-size:9px;line-height:1.75;margin:0 0 12px 0;">'
            f'{text}</p>'
        )

    # ── Build paragraphs ──────────────────────────────────────────────────────
    paras = []

    # Opener — roto winner
    if winner_team:
        mine   = _is_mine(winner_team)
        wcolor = ACCENT if mine else GREEN
        wstand = next((s["standing"] for s in standings
                       if " ".join(s["team_name"].split()) == " ".join(winner_team.split())), None)

        # Roto cats led this week
        n_teams  = len(week_rows)
        wrow     = next((r for r in week_rows
                         if " ".join(r.get("Team","").split()) == " ".join(winner_team.split())), {})
        led_cats = [_CAT_DISPLAY.get(c, c) for c in _CAT_ORDER
                    if float(wrow.get(f"{c}_Points", 0)) == n_teams]

        # Count weeks this team had the top roto score (roto weekly wins)
        wteam_norm = " ".join(winner_team.split())
        roto_weekly_wins = 0
        from collections import defaultdict
        roto_by_week = defaultdict(list)
        for r in roto:
            wk = r.get("Week")
            if wk is not None and int(wk) <= int(prev_week):
                roto_by_week[int(wk)].append(r)
        for wk_rows in roto_by_week.values():
            if not wk_rows:
                continue
            top = max(wk_rows, key=lambda r: float(r.get("Roto_Score") or 0))
            if " ".join(top.get("Team","").split()) == wteam_norm:
                roto_weekly_wins += 1

        stand_note = f", who sit at #{wstand} overall" if wstand else ""
        suffix = "  That's us!" if mine else ""

        sent = (f"Congrats to {_b(winner_team, wcolor)}{stand_note}, "
                f"for winning Week {prev_week} with {_b(f'{winner_score:.1f}')} roto points.{suffix}")
        if led_cats:
            sent += (f"  They led the league in {', '.join(led_cats[:3])}"
                     + (" and more" if len(led_cats) > 3 else "") + " this week.")
        if roto_weekly_wins > 1:
            sent += (f"  This marks their {_ordinal(roto_weekly_wins)} weekly roto win of the season.")
        paras.append(sent)

    # Hitter of the week
    if potw_hit:
        h     = potw_hit
        name  = h.get("PlayerName", "")
        ft    = h.get("FantasyTeam", "")
        pos   = h.get("Position", "")
        mine  = _is_mine(ft)
        nc    = ACCENT if mine else TEXT
        slash = _slash(h)
        cnts  = _hit_counts(h)
        ops   = _n(h.get("OPS"))
        hr    = int(_n(h.get("HR")))
        first = name.split()[0]
        pos_str  = f"{pos} " if pos else ""
        team_str = f" ({_b(ft, ACCENT if mine else MUTED)})" if ft else ""

        sent = f"The fantasy position player of the week was {pos_str}{_b(name, nc)}{team_str}."
        if slash and cnts:
            sent += f"  {first} slashed {_b(slash)} with {cnts}."

        # Named benchmarks
        if ops >= 1.100:
            sent += (f"  Barry Bonds holds the all-time single-season OPS record at 1.422 (2004) — "
                     f"his four-year run from 2001-2004 averaged 1.368.  A {_fmt_ops(ops)} week "
                     f"puts {first} in that same area code, at least for seven days.")
        elif ops >= 1.000:
            sent += (f"  Babe Ruth's career OPS of 1.164 is the highest in baseball history.  "
                     f"A 1.000+ OPS over a full season has happened fewer than 50 times ever — "
                     f"{first} did it in a week.")
        elif ops >= 0.950:
            sent += (f"  Shohei Ohtani's 2021 AL MVP season ended at .965 OPS — "
                     f"one of the best marks in the game over a full year.  "
                     f"{first}'s {_fmt_ops(ops)} week lands right in that territory.")
        elif ops >= 0.900:
            sent += (f"  A .900 OPS over a full season is All-Star caliber at almost any position — "
                     f"Mike Trout's career mark is .994.  {first} cleared the bar for the week.")
        else:
            sent += f"  {first} led all rostered hitters in OPS and delivered at the right time."

        # Position scarcity
        pos_tokens = set(re.split(r"[/,\s]+", pos.upper())) if pos else set()
        premium = pos_tokens & {"C", "SS", "2B"}
        if premium:
            pname = sorted(premium)[0]
            facts = {"C": "Mike Piazza holds the career OPS record for catchers at .922",
                     "SS": "Derek Jeter's career OPS was .838, considered elite for the position",
                     "2B": "Jeff Kent's career .855 OPS is the gold standard for second basemen"}
            sent += f"  {facts.get(pname, f'Elite {pname} production is exceptionally rare')} — {first}'s week clears that bar."

        # HR note
        if hr >= 4:
            sent += (f"  The {hr} home runs alone would be a productive week for most hitters — "
                     f"{first} treated them as a side dish.")
        elif hr >= 2:
            sent += f"  The {hr} HR added a power bonus on top of an already elite slash line."
        paras.append(sent)

    # Pitcher of the week
    if potw_pit:
        p     = potw_pit
        name  = p.get("PlayerName", "")
        ft    = p.get("FantasyTeam", "")
        mine  = _is_mine(ft)
        nc    = ACCENT if mine else TEXT
        era   = _n(p.get("ERA"))
        whip  = _n(p.get("WHIP"))
        ip    = _n(p.get("IP"))
        g     = int(_n(p.get("G")))
        k     = int(_n(p.get("SO") or p.get("K") or 0))
        w     = int(_n(p.get("W") or 0))
        qs    = int(_n(p.get("QS") or 0))
        first = name.split()[0]
        team_str = f" ({_b(ft, ACCENT if mine else MUTED)})" if ft else ""
        g_str    = f"In {g} appearance{'s' if g != 1 else ''}, " if g else ""

        stats = []
        if ip:  stats.append(f"{ip:.1f} IP")
        if k:   stats.append(f"{k} K")
        if qs:  stats.append(f"{qs} QS")
        if w:   stats.append(f"{w} W")
        ratio = ""
        if era > 0 and whip > 0:
            ratio = f" a {_b(f'{era:.2f}')} ERA and {_b(f'{whip:.2f}')} WHIP"
        elif era > 0:
            ratio = f" a {_b(f'{era:.2f}')} ERA"

        sent = f"The fantasy pitcher of the week was {_b(name, nc)}{team_str}.  {g_str}{first} posted{ratio}"
        sent += f" across {', '.join(stats)}." if stats else "."

        # Named ERA benchmarks
        if era <= 0 or era > 20:
            sent += f"  Dominant from start to finish — best pitching performance among rostered arms this week."
        elif era <= 0.50:
            sent += (f"  For context: Dutch Leonard set the all-time single-season ERA record in 1914 at 0.96, "
                     f"and Bob Gibson's legendary 1968 season — the one that prompted MLB to lower the mound — "
                     f"came in at 1.12.  {first}'s week came in below both.")
        elif era <= 1.00:
            sent += (f"  Bob Gibson's 1968 season is considered the greatest pitching season of the modern era — "
                     f"he finished at 1.12 ERA, won the Cy Young and MVP, and literally changed the rules of baseball.  "
                     f"A sub-1.00 week for {first} puts him below even that bar.")
        elif era <= 1.80:
            sent += (f"  Jacob deGrom's back-to-back Cy Young seasons came in at 1.70 ERA (2018) and 2.43 (2019) — "
                     f"considered among the most dominant pitching stretches of the modern era.  "
                     f"{first}'s {era:.2f} week puts him squarely in that company.")
        elif era <= 3.00:
            sent += (f"  In the modern run-scoring era, a sub-3.00 ERA over a full season earns Cy Young votes — "
                     f"the league ERA has hovered around 4.00-4.50 for most of the past decade.  "
                     f"{first} cleared that bar this week.")
        else:
            sent += f"  Best ERA among rostered pitchers this week and a steady presence out of the rotation."

        # Multi-start bonus
        if g >= 2:
            sent += (f"  The two-start week is the gift fantasy managers spend all spring drafting around — "
                     f"{first} delivered on both ends.")

        # Named K benchmarks
        if k >= 20:
            sent += (f"  {k} strikeouts in a single week.  Roger Clemens struck out 20 batters in one game "
                     f"(April 29, 1986 against the Mariners) — the single-game record.  "
                     f"{first} nearly matched it across the full week.")
        elif k >= 15:
            sent += (f"  Nolan Ryan struck out 383 batters in 1973 — the all-time single-season record.  "
                     f"Spread across a 26-week season, that works out to roughly 15 per week.  "
                     f"{first}'s {k} K are right on that historic pace.")
        elif k >= 10:
            sent += (f"  The {k} strikeouts are a meaningful fantasy bonus — "
                     f"for reference, Sandy Koufax averaged about 10 K per 9 innings across his peak years (1962-1966), "
                     f"a rate that defined dominant pitching for a generation.")
        paras.append(sent)

    # Best available FA
    if fa_potw:
        h     = fa_potw
        name  = h.get("PlayerName", "")
        pos   = h.get("Position", "")
        slash = _slash(h)
        cnts  = _hit_counts(h)
        ops   = _n(h.get("OPS"))
        first = name.split()[0]
        pos_str = f"{pos} " if pos else ""

        sent = f"The top available player of the week was {pos_str}{_b(name)}."
        if slash and cnts:
            sent += f"  {first} slashed {_b(slash)} with {cnts}."

        if ops >= 0.950:
            sent += (f"  A {_fmt_ops(ops)} OPS from a waiver wire pickup is an indictment of every manager "
                     f"who passed.  If {first} is still available in your league, this is a mandatory add.")
        elif ops >= 0.900:
            sent += (f"  Posting .900+ OPS while sitting unclaimed is the kind of thing that haunts managers "
                     f"at the end of the season.  Check availability now.")
        elif ops >= 0.800:
            sent += (f"  Solid production for a player still available — the kind of depth piece that "
                     f"separates playoff teams from the bubble.")
        else:
            sent += f"  Top available bat this week — if the roster spot is there, {first} is the call."

        pos_tokens = set(re.split(r"[/,\s]+", pos.upper())) if pos else set()
        if pos_tokens & {"C", "SS", "2B"}:
            pname = sorted(pos_tokens & {"C", "SS", "2B"})[0]
            sent += f"  {pname} production at this level on the wire almost never happens — don't sleep on it."
        paras.append(sent)

    # ── Sidebar stat cards ────────────────────────────────────────────────────
    def _card(label, label_color, name, logo, stat_lines):
        lines_html = "".join(
            f'<div style="color:{MUTED};font-size:10px;line-height:1.6;">{ln}</div>'
            for ln in stat_lines
        )
        return (
            f'<div style="padding:8px 0;border-top:1px solid {BORDER};">'
            f'<div style="color:{label_color};font-size:8px;font-weight:700;'
            f'text-transform:uppercase;letter-spacing:.8px;margin-bottom:4px;">{label}</div>'
            f'<div style="display:flex;align-items:center;gap:4px;margin-bottom:3px;">'
            f'{logo}'
            f'<span style="color:{TEXT};font-weight:700;font-size:11px;">{name}</span>'
            f'</div>'
            f'{lines_html}'
            f'</div>'
        )

    sidebar_cards = []

    if winner_team:
        mine   = _is_mine(winner_team)
        wcolor = ACCENT if mine else GREEN
        wlogo  = fantasy_logo(logos.get(" ".join(winner_team.split()), ""), 18, winner_team)
        wrow2  = next((r for r in week_rows
                       if " ".join(r.get("Team","").split()) == " ".join(winner_team.split())), {})
        n_t2   = len(week_rows)
        wled   = [_CAT_DISPLAY.get(c, c) for c in _CAT_ORDER
                  if float(wrow2.get(f"{c}_Points", 0)) == n_t2]
        score_line = f"{winner_score:.1f} roto pts"
        if wled:
            score_line += " | #1 in: " + ", ".join(wled)
        card_lines = [score_line]
        sidebar_cards.append(_card(
            f"Week {prev_week} Roto Winner", wcolor,
            winner_team, wlogo, card_lines,
        ))

    if potw_hit:
        h     = potw_hit
        name  = h.get("PlayerName", "")
        ft    = h.get("FantasyTeam", "")
        logo  = h.get("_logo", "")
        mlb   = h.get("MLBTeam", "")
        slash = _slash(h)
        cnts_parts = []
        for val, lbl in [(int(_n(h.get("HR"))), "HR"), (int(_n(h.get("RBI"))), "RBI"),
                         (int(_n(h.get("R"))), "R"),   (int(_n(h.get("SB"))),  "SB")]:
            if val: cnts_parts.append(f"{val} {lbl}")
        stat_lines = []
        if slash: stat_lines.append(slash)
        if cnts_parts: stat_lines.append(" \xb7 ".join(cnts_parts))
        sidebar_cards.append(_card("Hitter of the Week", GREEN,
                                   name + _mlb_logo(mlb), logo, stat_lines))

    if potw_pit:
        p     = potw_pit
        name  = p.get("PlayerName", "")
        logo  = p.get("_logo", "")
        mlb   = p.get("MLBTeam", "")
        era   = _n(p.get("ERA"))
        whip  = _n(p.get("WHIP"))
        ip    = _n(p.get("IP"))
        k     = int(_n(p.get("K") or p.get("SO") or 0))
        qs    = int(_n(p.get("QS") or 0))
        stat_lines = []
        if era > 0 and whip > 0:
            stat_lines.append(f"{era:.2f} ERA \xb7 {whip:.2f} WHIP")
        ip_line = f"{ip:.1f} IP" if ip else ""
        if k:  ip_line += f" \xb7 {k} K"
        if qs: ip_line += f" \xb7 {qs} QS"
        if ip_line: stat_lines.append(ip_line)
        sidebar_cards.append(_card("Pitcher of the Week", ACCENT,
                                   name + _mlb_logo(mlb), logo, stat_lines))

    if fa_potw:
        h     = fa_potw
        name  = h.get("PlayerName", "")
        mlb   = h.get("MLBTeam", "")
        slash = _slash(h)
        cnts_parts = []
        for val, lbl in [(int(_n(h.get("HR"))), "HR"), (int(_n(h.get("RBI"))), "RBI"),
                         (int(_n(h.get("R"))), "R")]:
            if val: cnts_parts.append(f"{val} {lbl}")
        stat_lines = []
        if slash: stat_lines.append(slash)
        if cnts_parts: stat_lines.append(" \xb7 ".join(cnts_parts))
        sidebar_cards.append(_card("Best Available", YELLOW,
                                   name + _mlb_logo(mlb), "", stat_lines))

    # ── Assemble two-column layout ────────────────────────────────────────────
    anchor = (
        '<a name="band-highlights" id="band-highlights" '
        'style="display:block;position:relative;top:-60px;visibility:hidden;"></a>'
    )
    prose_col = (
        f'<td style="vertical-align:top;padding-right:18px;width:60%;">'
        + "".join(_para(p) for p in paras)
        + f'</td>'
    )
    sidebar_col = (
        f'<td style="vertical-align:top;border-left:1px solid {BORDER};'
        f'padding-left:18px;width:40%;">'
        + "".join(sidebar_cards)
        + f'</td>'
    )
    return (
        anchor +
        section_head(f"Week {prev_week} Highlights",
                     "Roto winner \xb7 player of the week \xb7 best available") +
        f'<div style="background:{SURFACE};border-left:3px solid {YELLOW}88;'
        f'border-radius:0 6px 6px 0;padding:14px 20px;margin-bottom:6px;">'
        f'<table style="width:100%;border-collapse:collapse;"><tr>'
        + prose_col + sidebar_col +
        f'</tr></table>'
        f'</div>'
    )


# ── RECAP ASSEMBLY ────────────────────────────────────────────────────────────

def build_recap(snap):
    all_prev = snap.get("all_prev_matchups") or {}
    roto     = snap.get("roto") or []
    standings = snap.get("standings") or []
    weekly_results  = snap.get("weekly_results") or {}
    recent_hitting  = snap.get("recent_hitting")  or []
    recent_pitching = snap.get("recent_pitching") or []
    # Exact prev-week window (Mon–Sun) for commissioner story; fall back to rolling window
    prev_week_hitting  = snap.get("prev_week_hitting")  or recent_hitting
    prev_week_pitching = snap.get("prev_week_pitching") or recent_pitching
    hitters  = snap.get("hitters") or []
    pitchers = snap.get("pitchers") or []
    snap_year = int(snap.get("league_year") or 2026)

    # Logo lookup: normalized team name -> url
    logos = {" ".join(s["team_name"].split()): s.get("logo_url", "") for s in standings}

    # Resolve MY_TEAM entry (snapshot keys are whitespace-normalized)
    my_key = " ".join(MY_TEAM.split())
    prev_matchup = {}
    for k, v in all_prev.items():
        if " ".join(k.split()) == my_key:
            prev_matchup = v
            break

    prev_week = prev_matchup.get("week") or 0
    my_w = prev_matchup.get("wins", 0)
    my_l = prev_matchup.get("losses", 0)
    my_t = prev_matchup.get("ties", 0)
    my_opp = prev_matchup.get("opp_team", "?")

    if my_w > my_l:
        outcome_c, outcome_word = GREEN, "WIN"
    elif my_l > my_w:
        outcome_c, outcome_word = RED, "LOSS"
    else:
        outcome_c, outcome_word = TEXT, "TIE"

    # Date range (works correctly when run on Monday)
    today = datetime.now()
    if today.weekday() == 0:
        prev_sun = today - timedelta(days=1)
        prev_mon = today - timedelta(days=7)
    else:
        days_since_mon = today.weekday()
        prev_mon = today - timedelta(days=days_since_mon + 7)
        prev_sun = prev_mon + timedelta(days=6)
    week_dates = f"{prev_mon.strftime('%b %d')} – {prev_sun.strftime('%b %d')}"

    refreshed_time = _fmt_refresh_time(snap.get("refreshed_at", ""))
    refreshed = f"today at {refreshed_time}" if refreshed_time else "recently"

    # ── HEADER ────────────────────────────────────────────────────────────────
    score_str = f"{my_w}–{my_l}" + (f"–{my_t}" if my_t else "")
    header = (
        f'<div style="background:{SURFACE};padding:20px 26px;border-bottom:1px solid {BORDER};">'
        f'<table style="width:100%;border-collapse:collapse;">'
        f'<tr>'
        f'<td style="vertical-align:top;">'
        f'<div style="color:{ACCENT};font-size:20px;font-weight:800;letter-spacing:-.3px;">'
        f'Week {prev_week} Recap</div>'
        f'<div style="color:{MUTED};font-size:12px;margin-top:2px;">{week_dates}</div>'
        f'<div style="margin-top:8px;">'
        f'<span style="color:{ACCENT};font-weight:800;">Guerrero Warfare</span>'
        f'<span style="color:{MUTED};font-size:12px;"> → </span>'
        f'<span style="color:{outcome_c};font-weight:700;">{outcome_word}</span> '
        f'<span style="color:{TEXT};font-weight:700;">{score_str}</span>'
        f'<span style="color:{MUTED};font-size:11px;"> vs {my_opp}</span>'
        f'</div>'
        f'</td>'
        f'<td style="text-align:right;vertical-align:top;">'
        f'<div style="color:{MUTED};font-size:10px;font-weight:700;text-transform:uppercase;'
        f'letter-spacing:.7px;">ESPN League 277836</div>'
        f'<div style="color:{MUTED};font-size:10px;margin-top:3px;">Data as of {refreshed}</div>'
        f'</td>'
        f'</tr></table></div>'
    )

    # ── BODY ──────────────────────────────────────────────────────────────────
    _sec_anchor = (
        '<a name="sec-matchup" id="sec-matchup" style="display:block;position:relative;'
        'top:-60px;visibility:hidden;"></a>'
    )
    _traj_anchor = (
        '<a name="band-trajectory" id="band-trajectory" style="display:block;position:relative;'
        'top:-60px;visibility:hidden;"></a>'
    )
    body_parts = [
        _nav_bar(),
        build_commissioner_story(
            roto, prev_week, prev_week_hitting, prev_week_pitching,
            hitters, pitchers, standings, logos,
            weekly_results=weekly_results, snap_year=snap_year,
        ),
        _sec_anchor,
        build_my_matchup(prev_matchup, logos),
        (band_divider("LINEUP EFFICIENCY", anchor="band-lineup") + _lineup_eff
         if (_lineup_eff := build_lineup_efficiency(snap.get("lineup_efficiency") or {})) else ""),
        band_divider("LEAGUE SCOREBOARD", anchor="band-scoreboard"),
        build_league_scoreboard(all_prev, logos),
        band_divider("WEEKLY PERFORMANCE", anchor="band-roto"),
        build_weekly_roto_rankings(roto, prev_week, logos),
        band_divider("TOP PERFORMERS", anchor="band-performers"),
        build_top_performers(prev_week_hitting, prev_week_pitching, hitters, pitchers, logos,
                             snap_year, week_dates=week_dates),
        band_divider("STANDINGS & SEASON", anchor="band-standings"),
        build_standings_section(roto, standings, logos),
        f'<div style="margin-top:28px;"></div>',
        _traj_anchor,
        build_trajectory(weekly_results, standings, logos),
        _TOP_LINK_DIV,
    ]
    body = "\n".join(p for p in body_parts if p)

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Week {prev_week} Recap — Guerrero Warfare</title>
  <style>
    @media only screen and (max-width:600px) {{
      .ew {{ width:100% !important; padding:8px !important; }}
      table th, table td {{ padding:4px 3px !important; font-size:10px !important; }}
      .hide-mob {{ display:none !important; }}
    }}
  </style>
</head>
<body style="margin:0;padding:16px;background:#060b18;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<div class="ew" style="max-width:760px;margin:0 auto;background:{BG};border:1px solid {BORDER};border-radius:8px;overflow:hidden;">
  {header}
  <div class="ew" style="padding:22px 26px;">{body}</div>
  <div style="text-align:center;padding:14px;color:{MUTED};font-size:11px;border-top:1px solid {BORDER};">
    Data refreshed {refreshed} &middot; ESPN League 277836
  </div>
</div>
</body>
</html>"""


# ── EMAIL ─────────────────────────────────────────────────────────────────────

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

    msg.attach(MIMEText(html, "html"))

    attachment = MIMEText(html, "html", "utf-8")
    attachment.add_header(
        "Content-Disposition", "attachment",
        filename=filename or f"recap_{datetime.now().strftime('%Y-%m-%d')}.html",
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

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("=" * 60)
    print("  League Weekly Recap")
    print(f"  {ts}")
    print("=" * 60)

    if not no_refresh:
        print("\n[1/3] Refreshing data (~60s)...")
        result = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "fetch_data.py")],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"  WARNING: fetch_data.py exited {result.returncode}")
            print(f"  {result.stderr[-300:] if result.stderr else '(no stderr)'}")
            if not SNAPSHOT.exists():
                sys.exit("No snapshot and refresh failed — aborting.")
            print("  Falling back to existing snapshot.")
        else:
            print("  Refresh complete.")
    else:
        print("\n[1/3] Skipping data refresh (--no-refresh).")

    print("\n[2/3] Building recap...")
    with open(SNAPSHOT, encoding="utf-8") as f:
        snap = json.load(f)

    html = build_recap(snap)

    # Derive week number and my result for subject line
    all_prev = snap.get("all_prev_matchups") or {}
    prev_week, my_w, my_l = 0, 0, 0
    my_key = " ".join(MY_TEAM.split())
    for k, v in all_prev.items():
        if " ".join(k.split()) == my_key:
            prev_week = v.get("week", 0)
            my_w      = v.get("wins", 0)
            my_l      = v.get("losses", 0)
            break

    subject    = f"Week {prev_week} Recap — Guerrero Warfare {my_w}-{my_l}"
    attach_name = f"recap_week_{prev_week}.html"

    if dry_run:
        previews_dir = Path(__file__).parent / "previews"
        previews_dir.mkdir(exist_ok=True)
        out = previews_dir / attach_name
        out.write_text(html, encoding="utf-8")
        print(f"\n  Dry run — saved to {out}")
        print("\nDone (no email sent).")
        return

    print(f"\n[3/3] Sending to {TO_EMAIL}...")
    send_email(html, subject, filename=attach_name)
    print("  Sent.")
    print("\nDone.")


if __name__ == "__main__":
    main()
