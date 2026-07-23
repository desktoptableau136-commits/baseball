"""fantasy/ui.py — presentation primitives for the digest (F5 split, part 1).

Pure HTML/format helpers with NO scoring or data dependencies: the color palette,
badges, band dividers, logos, table cells, and formatting utilities. This is the
LEAF layer — it imports only the stdlib (and nothing from send_digest), so every
other layer can depend on it. send_digest re-exports everything here via
`from fantasy.ui import *`, so `sd.<name>` keeps working unchanged.
"""
import hashlib
import re
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:                       # zoneinfo missing (very old Python / no tzdata)
    _ET = None

_EXCLUDE = set(dir())  # names above are imports, not exports

# -- color palette --
BG       = "#080e1c"
SURFACE  = "#101827"
SURFACE2 = "#0d1424"
BORDER   = "#1e2d45"
TEXT     = "#e2e8f0"
MUTED    = "#64748b"
ACCENT   = "#3b82f6"   # also the pitcher two-start "2" badge (solid, white text) + dashboard ×2 markers
GREEN    = "#22c55e"
RED      = "#ef4444"
YELLOW   = "#f59e0b"
ORANGE   = "#ea580c"   # starter low-floor ⚠ badge (burnt orange) — deliberately distinct from the amber YELLOW 5K+ chip
PURPLE   = "#a855f7"   # hitter PWR badge (translucent) — distinct from green/yellow/red
CYAN     = "#22d3ee"   # pitcher QS badge (translucent) — QS + two-start can co-occur, so 2 moved to blue
SILVER   = "#c8d0da"   # hitter SB "Quicksilver" speed badge (metallic, distinct from TEXT/MUTED)
MAGENTA  = "#e935c1"   # Trade Radar "upgrades your thin {pos}" chip — its own hue (moved off CYAN, which now means only the QS season-skill badge on a trade card; toned magenta fits the 500-level palette and collides with nothing else on the card — widest margin from PWR purple of the candidates tested)

TH_S = f"padding:8px 10px;background:{SURFACE};color:{MUTED};font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.7px;border-bottom:2px solid {BORDER};white-space:nowrap;"
TD_S = f"padding:7px 10px;border-bottom:1px solid {BORDER};color:{TEXT};font-size:13px;vertical-align:middle;"
TDC  = f"padding:7px 10px;border-bottom:1px solid {BORDER};color:{TEXT};font-size:13px;text-align:center;vertical-align:middle;"


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


def _fmt_game_time_et(iso_str):
    """Format an MLB StatsAPI game UTC time (ISO, e.g. '2026-07-11T23:10:00Z') as a
    compact ET clock like '7:10p ET'. Returns '' on any parse failure."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(str(iso_str).replace("Z", "+00:00"))
    except Exception:
        return ""
    if dt.tzinfo is not None and _ET is not None:
        dt = dt.astimezone(_ET)
    h = dt.hour % 12 or 12
    ap = "a" if dt.hour < 12 else "p"
    return f"{h}:{dt.minute:02d}{ap} ET"


def _ascii_lower(s):
    """Accent-stripped, lowercased name for loose cross-source matching (ESPN roster
    names vs MLB StatsAPI probable-pitcher names differ by accents)."""
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", str(s or ""))
                   if not unicodedata.combining(c)).lower().strip()


def _n(val):
    """Coerce to float, return 0 for falsy/negative sentinel values."""
    try:
        v = float(val or 0)
        return v if v > 0 else 0
    except (TypeError, ValueError):
        return 0


def two_start_badge(title=""):
    """Bold chip flagging a pitcher with two starts inside the matchup week."""
    tt = f' title="{title}"' if title else ""
    return (
        f'<span{tt} style="font-size:9px;font-weight:800;color:#fff;'
        f'background:{ACCENT};border-radius:3px;padding:1px 5px;margin-left:5px;'
        f'vertical-align:middle;letter-spacing:.3px;">2</span>'
    )


def _hit_badge(text, color, title=""):
    """A translucent hitter badge chip in the QS/5K+ visual style (color-tinted bg + border)."""
    r, g, b = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
    tt = f' title="{title}"' if title else ""
    return (
        f'<span{tt} style="font-size:9px;font-weight:700;color:{color};'
        f'background:rgba({r},{g},{b},0.12);border:1px solid rgba({r},{g},{b},0.35);'
        f'border-radius:3px;padding:1px 5px;margin-left:5px;vertical-align:middle;">{text}</span>'
    )


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


_FAVORITE_MLB_TEAMS = {"ATL"}


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
        # deterministic hash (md5) so the fallback color is stable across runs —
        # builtin hash() is per-process salted, which forced a PYTHONHASHSEED pin on
        # the render-diff harness. Same mid-brightness band [0x222222, 0xDDDDDD].
        seed = int(hashlib.md5(norm.encode("utf-8")).hexdigest(), 16)
        color = f"#{seed % 0xBBBBBB + 0x222222:06x}"
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


def _score_bg_hex(score):
    """Badge BACKGROUND color for a 0-100 score (paired with white text on the pill)."""
    s = int(score or 0)
    if s >= 72:   return "#16a34a"
    elif s >= 52: return "#2563eb"
    elif s >= 32: return "#d97706"
    else:         return "#dc2626"


def _score_text_hex(score):
    """Score color for use as TEXT on the dark surface (brighter palette variants, same
    72/52/32 tiers as the badge) — e.g. the season|recent numbers in a breakdown header."""
    s = int(score or 0)
    if s >= 72:   return GREEN
    elif s >= 52: return ACCENT
    elif s >= 32: return YELLOW
    else:         return RED


def badge(score, small=False):
    s = int(score or 0)
    bg, fg = _score_bg_hex(s), "#fff"
    pad, radius, fs = ("1px 6px", "10px", "9px") if small else ("2px 9px", "12px", "11px")
    return (f'<span style="background:{bg};color:{fg};padding:{pad};border-radius:{radius};'
            f'font-size:{fs};font-weight:800;">{s}</span>')


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


def score_reveal(score, breakdown_html, uid=None, colspan=1, small=False):
    """Return (cell_html, row_html): the Score-cell badge and the full-width breakdown
    <tr> to append immediately after the player's row. The badge is an anchor to the
    hidden row, revealed via CSS :target in the browser attachment. Falls back to a
    plain badge with an empty row when there is no breakdown or no uid. `small` shrinks the
    pill to sit inline with 10px sub-text (e.g. the Positional Breakdown drop candidate)."""
    if not breakdown_html or not uid:
        return badge(score, small), ""
    caret_fs = "8px" if small else "9px"
    cell = (
        f'<a href="#{uid}" class="bdlink" title="Tap for score breakdown" '
        f'style="text-decoration:none;white-space:nowrap;">{badge(score, small)}'
        f'<span style="color:{MUTED};font-size:{caret_fs};font-weight:700;">&nbsp;&#9662;</span></a>'
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


_BD_TOGGLE_SCRIPT = """<script>
document.addEventListener('click', function(e){
  var a = e.target.closest ? e.target.closest('a') : null;
  if(!a) return;
  var h = a.getAttribute('href') || '';
  if(h.charAt(0) !== '#') return;
  var id = h.slice(1);
  if(a.className && a.className.indexOf('bdlink') !== -1){
    var el = document.getElementById(id);
    if(!el) return;
    e.preventDefault();
    var open = el.style.display !== 'none' && el.style.display !== '';
    el.style.display = open ? 'none' : (el.tagName === 'TR' ? 'table-row' : 'block');
  } else if(id.slice(-1) === 'x'){
    var el2 = document.getElementById(id.slice(0, -1));
    if(el2){ e.preventDefault(); el2.style.display = 'none'; }
  }
});
</script>"""


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
        ("#band-fa",       "Transactions"),
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


def kpi_cell_sm(label, value, color=None, font_size="20px", font_weight="800"):
    val_color = color or TEXT
    return (
        f'<td class="kpi-cell" style="text-align:center;padding:8px 8px 10px;border-right:1px solid {BORDER};">'
        f'<div style="color:{MUTED};font-size:9px;text-transform:uppercase;letter-spacing:.7px;">{label}</div>'
        f'<div style="color:{val_color};font-size:{font_size};font-weight:{font_weight};margin-top:3px;">{value}</div>'
        f'</td>'
    )


__all__ = [n for n in dir()
           if n not in _EXCLUDE and n != '_EXCLUDE' and not n.startswith('__')]
