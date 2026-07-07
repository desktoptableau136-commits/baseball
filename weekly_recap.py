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

        logo_a = fantasy_logo(logos.get(" ".join(team_a.split()), ""), 22, team_a)
        logo_b = fantasy_logo(logos.get(" ".join(team_b.split()), ""), 22, team_b)
        col_a = ACCENT if is_my_a else TEXT

        score_a = f"{w_a}–{l_a}" + (f"–{t_a}" if t_a else "")
        score_b = f"{l_a}–{w_a}" + (f"–{t_a}" if t_a else "")

        header_div = (
            f'<div style="display:flex;align-items:center;justify-content:space-between;'
            f'padding:9px 12px;background:{SURFACE2};border-bottom:1px solid {BORDER};">'
            f'<div style="display:flex;align-items:center;gap:6px;">'
            f'{logo_a}'
            f'<span style="color:{col_a};font-weight:700;font-size:13px;">{team_a}</span>'
            f'<span style="color:{MUTED};font-size:11px;font-weight:700;margin-left:4px;">{score_a}</span>'
            f'</div>'
            f'<span style="color:{MUTED};font-size:10px;font-weight:600;padding:0 8px;">vs</span>'
            f'<div style="display:flex;align-items:center;gap:6px;flex-direction:row-reverse;">'
            f'{logo_b}'
            f'<span style="color:{TEXT};font-weight:700;font-size:13px;">{team_b}</span>'
            f'<span style="color:{MUTED};font-size:11px;font-weight:700;margin-right:4px;">{score_b}</span>'
            f'</div>'
            f'</div>'
        )

        header_cells = f'<th style="{th}text-align:left;padding-left:8px;min-width:36px;"></th>'
        for i, cat in enumerate(_CAT_ORDER):
            lbl = _CAT_DISPLAY.get(cat, cat)
            c   = cats.get(cat, {})
            res = c.get("result", "T")
            col = GREEN if res == "W" else (RED if res == "L" else MUTED)
            sep = f"border-left:1px solid {BORDER};" if i == 6 else ""
            header_cells += (
                f'<th style="{th}{sep}color:{col};border-bottom:2px solid {col};">{lbl}</th>'
            )

        def _row(label, label_color, val_key, win_result, team_a=team_a, cats=cats):
            row = (f'<td style="{td_val}text-align:left;color:{label_color};font-weight:700;'
                   f'font-size:10px;padding-left:8px;">{label}</td>')
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

        cat_table = (
            f'<div style="overflow-x:auto;-webkit-overflow-scrolling:touch;">'
            f'<table style="width:100%;border-collapse:collapse;min-width:440px;">'
            f'<thead><tr>{header_cells}</tr></thead>'
            f'<tbody>'
            + _row(team_a, col_a, "my_val",  "W")
            + _row(team_b, TEXT,  "opp_val", "L")
            + "</tbody></table></div>"
        )

        blocks.append(
            f'<div style="background:{SURFACE};border:1px solid {BORDER};border-radius:6px;'
            f'overflow:hidden;margin-bottom:14px;">'
            + header_div
            + f'<div style="padding:4px 0 8px;">{cat_table}</div>'
            f'</div>'
        )

    return (
        section_head(f"League Scoreboard — Week {week}",
                     "All 6 matchups \xb7 green col = team A won that cat \xb7 outlined value = winner") +
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


def build_top_performers(recent_hitting, recent_pitching, hitters, pitchers, logos, snap_year=2026):
    """Top rostered performers of the week + hot free agents."""

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
        n  = rh.get("PlayerName", "")
        s  = h_exact.get(n) or h_keyed.get(_name_key(n)) or {}
        ft = (s.get("FantasyTeam") or "").strip()
        return {**rh, "FantasyTeam": ft, "Position": s.get("Position", ""),
                "_logo": fantasy_logo(logos.get(" ".join(ft.split()), ""), 18, ft) if ft else ""}

    def _enrich_p(rp):
        n  = rp.get("PlayerName", "")
        s  = p_exact.get(n) or p_keyed.get(_name_key(n)) or {}
        ft = (s.get("FantasyTeam") or "").strip()
        return {**rp, "FantasyTeam": ft, "Position": s.get("Position", ""),
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
    PIT_KEYS   = ["ERA", "WHIP", "IP", "G"]
    PIT_LABELS = ["ERA", "WHIP", "IP", "G"]

    out = section_head("Top Performers",
                       "Hitting: FanGraphs last 7 days (min 10 AB) \xb7 "
                       "Pitching: last 15 days (min 8 IP)")

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


# ── RECAP ASSEMBLY ────────────────────────────────────────────────────────────

def build_recap(snap):
    all_prev = snap.get("all_prev_matchups") or {}
    roto     = snap.get("roto") or []
    standings = snap.get("standings") or []
    weekly_results  = snap.get("weekly_results") or {}
    recent_hitting  = snap.get("recent_hitting") or []
    recent_pitching = snap.get("recent_pitching") or []
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
        _sec_anchor,
        build_my_matchup(prev_matchup, logos),
        band_divider("LEAGUE SCOREBOARD", anchor="band-scoreboard"),
        build_league_scoreboard(all_prev, logos),
        band_divider("WEEKLY PERFORMANCE", anchor="band-roto"),
        build_weekly_roto_rankings(roto, prev_week, logos),
        band_divider("TOP PERFORMERS", anchor="band-performers"),
        build_top_performers(recent_hitting, recent_pitching, hitters, pitchers, logos, snap_year),
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
