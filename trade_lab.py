#!/usr/bin/env python3
"""Interactive Trade Lab — a standalone, browser-only tool for building and grading
hypothetical trades. Unlike the digest's Trade Radar (speculative ideas) or the Pending
Trades evaluator (real ESPN offers), this lets you pick ANY two teams, see each roster
grouped by role (hitters / starting pitchers / relief pitchers) with the same badges and
expandable score pills used everywhere else, then select players on each side and watch a
live verdict — value tilt, categories/positions helped, timing, and whether the partner
would realistically accept.

It IMPORTS send_digest and reuses its scoring / trade-value functions verbatim, so every
number matches the digest (honors the "same score in every section" rule in CLAUDE.md).
The heavy lifting stays in Python: each player's score, badges, breakdown prose, and trade
value (`_tval`) are pre-computed and embedded as JSON. The JavaScript only handles
selection and sums those pre-computed numbers, so it can never disagree with the digest.

Because it needs JavaScript for live selection, it is browser-only (mail clients strip JS)
and is NOT emailable like the dashboard. It writes previews/tradelab_{team_slug}.html.

    python trade_lab.py                      # use existing snapshot (fast), write preview
    python trade_lab.py --refresh            # refresh data first (~60s), then write
    python trade_lab.py --team "Houck Tuah"  # default the LEFT (my) side to another team
"""

import argparse
import json
from datetime import datetime
from pathlib import Path

import send_digest as sd
from send_digest import (
    BG, SURFACE, SURFACE2, BORDER, TEXT, MUTED, ACCENT, GREEN, RED, YELLOW,
    YEAR, MY_TEAM, _n, _is_sp,
)

SNAPSHOT = Path(__file__).parent / "data" / "snapshot.json"
PREVIEWS = Path(__file__).parent / "previews"

# The 12 roto categories, with short display labels (B_SO = batter strikeouts).
CAT_LABELS = {
    "R": "R", "HR": "HR", "RBI": "RBI", "SB": "SB", "OPS": "OPS", "B_SO": "bSO",
    "K": "K", "QS": "QS", "W": "W", "ERA": "ERA", "WHIP": "WHIP", "SVHD": "SV+H",
}


def _key(name):
    return " ".join((name or "").split())


def _disp(name):
    """Display form of a team name (single-spaced)."""
    return " ".join((name or "").split())


def _freshness(refreshed_at):
    """(label, color) for the snapshot's refresh stamp, computed at build time.
    Green = refreshed today, yellow = 1 day old, red = 2+ days old (or unparseable)."""
    from datetime import datetime, timezone
    if not refreshed_at:
        return ("no refresh timestamp", RED)
    try:
        dt = datetime.fromisoformat(str(refreshed_at).replace("Z", "+00:00"))
    except Exception:
        return ("refresh time unknown", RED)
    try:
        from zoneinfo import ZoneInfo
        et = dt.astimezone(ZoneInfo("America/New_York"))
        now_et = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        et = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        now_et = datetime.now(timezone.utc)
    age = (now_et.date() - et.date()).days
    h = et.hour % 12 or 12
    ampm = "AM" if et.hour < 12 else "PM"
    stamp = f"{et.strftime('%b')} {et.day}, {h}:{et.minute:02d} {ampm} ET"
    if age <= 0:
        return (f"{stamp} &middot; fresh today", GREEN)
    if age == 1:
        return (f"{stamp} &middot; 1 day old", YELLOW)
    return (f"{stamp} &middot; {age} days old &mdash; run --refresh", RED)


def _fmt(v, dec=2):
    v = _n(v)
    s = f"{v:.{dec}f}"
    return s[1:] if 0 <= v < 1 else s


_POS_ORDER = ["C", "1B", "2B", "3B", "SS", "OF", "DH"]


def _pos_tokens(r, role):
    """Clean, compact position chips for a hitter (OF variants collapsed to OF).
    Pitchers are already grouped as SP/RP, so they get none."""
    if role != "hit":
        return []
    parts = {p.strip() for p in str(r.get("Position") or "").upper().replace("/", ",").split(",") if p.strip()}
    norm = set()
    for p in parts:
        if p in ("LF", "CF", "RF", "OF"):
            norm.add("OF")
        elif p in _POS_ORDER:
            norm.add(p)
    return [p for p in _POS_ORDER if p in norm]


def _stat_line(r, role):
    """A compact role-specific stat line for the player row."""
    if role == "hit":
        return (f'{int(_n(r.get("HR")))} HR &middot; {int(_n(r.get("RBI")))} RBI &middot; '
                f'{int(_n(r.get("SB")))} SB &middot; {_fmt(r.get("OPS"), 3)} OPS')
    k = int(sd._cat_value(r, "K"))
    if role == "sp":
        return (f'{_fmt(r.get("ERA"))} ERA &middot; {_fmt(r.get("WHIP"))} WHIP &middot; '
                f'{k} K &middot; {int(_n(r.get("GS")))} GS')
    svhd = int(sd._cat_value(r, "SVHD"))
    return (f'{svhd} SV+H &middot; {k} K &middot; {_fmt(r.get("ERA"))} ERA &middot; '
            f'{_fmt(r.get("WHIP"))} WHIP')


# ══════════════════════════════════════════════════════════════════════════════
# DATA — reconstruct the digest's derived structures, then serialize every team.
# ══════════════════════════════════════════════════════════════════════════════

def build_data(snap, my_team):
    pitchers  = snap.get("pitchers", [])
    hitters   = snap.get("hitters", [])
    roto      = snap.get("roto", [])
    standings = snap.get("standings", [])

    # ORDER MATTERS — scoring functions read the module globals these populate.
    sd.compute_ab_benchmarks(hitters)
    sd.compute_pitcher_benchmarks(pitchers)
    sd.compute_score_calibration(pitchers)
    sd.compute_league_averages(hitters, pitchers)
    sd.compute_xera_offset(pitchers)   # de-bias the pitcher buy/sell (ERA vs xERA) flag

    # Recent-form indices for score blending (YEAR-preferred: 30 > 15 > 7 > pybaseball).
    def _idx(rows, ds):
        return {r["PlayerName"]: r for r in rows if int(r.get("Dataset", 0) or 0) == ds and r.get("PlayerName")}
    rec_h = {r["PlayerName"]: r for r in snap.get("recent_hitting", [])  if r.get("PlayerName")}
    rec_p = {r["PlayerName"]: r for r in snap.get("recent_pitching", []) if r.get("PlayerName")}
    rec_p_fp = {}
    for name, r in rec_p.items():
        ip = _n(r.get("IP")); k = _n(r.get("K")); g = _n(r.get("G"))
        rec_p_fp[name] = {**r, "K/IP": round(k / ip, 3) if ip > 0 else 0,
                          "IP_per_G": round(ip / g, 2) if g > 0 else 0}
    best_recent_p = {**rec_p_fp, **_idx(pitchers, 7), **_idx(pitchers, 15), **_idx(pitchers, 30)}
    best_recent_h = {**rec_h,    **_idx(hitters, 7),  **_idx(hitters, 15),  **_idx(hitters, 30)}

    # Percentile pools — SAME qualified YEAR pools as the digest (so _tval matches).
    _ab_pool_floor = (sd._AB_BENCH.get(YEAR) or sd._FULLTIME_AB[YEAR]) * 0.30
    _hit_pool = [r for r in hitters if int(_n(r.get("Dataset")) or 0) == YEAR and _n(r.get("AB")) >= _ab_pool_floor]
    hit_pctile = sd.build_cat_percentiles(_hit_pool, sd._FA_HIT_CATS)
    _pit_pool = [r for r in pitchers if int(_n(r.get("Dataset")) or 0) == YEAR]
    pit_pctile = sd.build_cat_percentiles(_pit_pool, sd._FA_RP_CATS)
    sd.compute_position_scarcity(hitters, hit_pctile)   # positional-scarcity scale → _POS_SCARCITY (hitter _tval)

    # Per-team category ranks → needs (bottom third) / surplus (top third).
    ranks, n = sd.team_category_ranks(roto)
    third = max(1, round(n / 3.0)) if n else 1
    needs_of   = lambda t: sorted(c for c, rk in ranks.get(t, {}).items() if rk >= n - third + 1)
    surplus_of = lambda t: sorted(c for c, rk in ranks.get(t, {}).items() if rk <= third)

    # Team roster ordered by standings; keep the double-space snapshot keys for matching.
    team_keys = [_key(s.get("team_name")) for s in standings] or sorted(ranks.keys())
    team_logos = {_key(s.get("team_name")): s.get("logo_url", "") for s in standings}

    teams_meta, players = {}, {}
    for tk in team_keys:
        # Thin HITTER positions for this team → {pos: my_avg_score} (positional need).
        pos_data = sd.positional_breakdown(pitchers, hitters, tk, best_recent_p, best_recent_h)
        need_pos = {}
        for p in pos_data:
            if p.get("ptype") != "hit":
                continue
            nt = p.get("n_teams") or n
            pt = max(1, round(nt / 3.0))
            if (p.get("rank") or nt) >= nt - pt + 1:
                need_pos[p["pos"]] = round(p.get("my_avg") or 0, 1)
        teams_meta[tk] = {
            "name":     _disp(tk),
            "logo":     sd.fantasy_logo(team_logos.get(tk, ""), 24, tk),
            "needs":    needs_of(tk),
            "surplus":  surplus_of(tk),
            "need_pos": need_pos,
        }

        buckets = {"hit": [], "sp": [], "rp": []}
        # Hitters
        for r in hitters:
            if _key(r.get("FantasyTeam")) != tk or int(r.get("Dataset", 0) or 0) != YEAR:
                continue
            sd._enrich_trade_player(r, "hit", best_recent_p, best_recent_h, hit_pctile, pit_pctile)
            buckets["hit"].append(_serialize(r, "hit", best_recent_h, best_recent_p, hit_pctile))
        # Pitchers (split SP / RP by usage role)
        for r in pitchers:
            if _key(r.get("FantasyTeam")) != tk or int(r.get("Dataset", 0) or 0) != YEAR:
                continue
            sd._enrich_trade_player(r, "pit", best_recent_p, best_recent_h, hit_pctile, pit_pctile)
            role = "sp" if _is_sp(r) else "rp"
            buckets[role].append(_serialize(r, role, best_recent_h, best_recent_p, hit_pctile))
        for role in buckets:
            buckets[role].sort(key=lambda p: -p["score"])
        players[tk] = buckets

    my_key = _key(my_team)
    if my_key not in players:
        my_key = _key(snap.get("my_team", MY_TEAM))
    return {
        "teamKeys":  team_keys,
        "teamsMeta": teams_meta,
        "players":   players,
        "myTeam":    my_key,
        "catLabels": CAT_LABELS,
        "lowerBetter": sorted(sd._LOWER_BETTER),
        "refreshed": snap.get("refreshed_at", ""),
    }


def _serialize(r, role, best_recent_h, best_recent_p, hit_pctile):
    """One JSON-safe player record. Badges + breakdown are pre-rendered HTML strings."""
    if role == "hit":
        badges    = sd.hitter_badges(r, hit_pctile)
        breakdown = sd._hitter_score_breakdown(r, best_recent_h, hit_pctile)
    elif role == "sp":
        badges    = sd.blowup_badge(r) + sd.pitcher_regression_badge(r)
        breakdown = sd._pitcher_score_breakdown(r, best_recent_p)
    else:
        badges    = sd.pitcher_regression_badge(r)
        breakdown = sd._pitcher_score_breakdown(r, best_recent_p)
    return {
        "id":        sd._bd_uid("tl", r.get("PlayerName")),
        "name":      r.get("PlayerName", ""),
        "role":      role,
        "logo":      sd.team_logo(r.get("Team"), 16),
        "pos":       str(r.get("Position") or ""),
        "posTokens": _pos_tokens(r, role),
        "stat":      _stat_line(r, role),
        "score":     int(round(_n(r.get("_tscore")))),
        "badges":    badges,
        "breakdown": breakdown,
        "tval":      round(_n(r.get("_tval")), 3),
        "tcats":     sorted(r.get("_tcats") or []),
        "tgroups":   sorted(r.get("_tgroups") or []),
        "sell":      bool(r.get("_tsell")),
        "buy":       bool(r.get("_tbuy")),
    }


# ══════════════════════════════════════════════════════════════════════════════
# RENDER — static shell + embedded JSON + the selection/verdict JavaScript.
# ══════════════════════════════════════════════════════════════════════════════

def build_html(data):
    blob = json.dumps(data).replace("</", "<\\/")   # </script> safety
    css = _CSS.format(BG=BG, SURFACE=SURFACE, SURFACE2=SURFACE2, BORDER=BORDER,
                      TEXT=TEXT, MUTED=MUTED, ACCENT=ACCENT, GREEN=GREEN, RED=RED, YELLOW=YELLOW)
    js = _JS.format(GREEN=GREEN, RED=RED, YELLOW=YELLOW, ACCENT=ACCENT,
                    MUTED=MUTED, TEXT=TEXT, BORDER=BORDER, SURFACE2=SURFACE2)
    my_name = _disp(data["myTeam"])
    fresh_label, fresh_color = _freshness(data.get("refreshed", ""))
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trade Lab &mdash; {my_name}</title>
<style>{css}</style>
</head><body>
<div id="app">
  <div id="head">
    <div class="headmain">
      <div class="htitle">&#9878;&#65039; Trade Lab</div>
      <div class="hsub">Pick two teams, click players to build a deal, watch it get graded live. &#127919; marks partner players who fill your needs.</div>
    </div>
    <div class="fresh" title="Snapshot refresh time — rerun with --refresh to update"><span class="dot" style="background:{fresh_color}"></span><span>Data: {fresh_label}</span></div>
  </div>
  <div id="cols">
    <div class="side" id="sideL">
      <div class="sidehead">
        <span class="sidetag">MY TEAM</span>
        <select id="selL" class="teamsel"></select>
      </div>
      <div class="roster" id="rosterL"></div>
    </div>
    <div id="mid">
      <div id="verdict"></div>
      <div class="ledger">
        <div class="give"><div class="lhead give-h">YOU GIVE</div><div id="giveList" class="llist"></div></div>
        <div class="get"><div class="lhead get-h">YOU GET</div><div id="getList" class="llist"></div></div>
      </div>
      <div id="reads"></div>
      <div id="coach"></div>
      <button id="clearBtn" onclick="clearAll()">Clear deal</button>
    </div>
    <div class="side" id="sideR">
      <div class="sidehead">
        <span class="sidetag">TRADE PARTNER</span>
        <select id="selR" class="teamsel"></select>
      </div>
      <div class="roster" id="rosterR"></div>
    </div>
  </div>
</div>
<script>const DATA = {blob};</script>
<script>{js}</script>
</body></html>"""


_CSS = """
* {{ box-sizing:border-box; }}
body {{ margin:0; background:{BG}; color:{TEXT}; font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; }}
#app {{ max-width:1500px; margin:0 auto; padding:16px; }}
#head {{ margin-bottom:12px; display:flex; justify-content:space-between; align-items:flex-start; gap:12px; flex-wrap:wrap; }}
.fresh {{ font-size:11.5px; color:{MUTED}; white-space:nowrap; display:flex; align-items:center; gap:6px; padding-top:5px; }}
.dot {{ width:9px; height:9px; border-radius:50%; display:inline-block; flex:0 0 auto; }}
.htitle {{ font-size:22px; font-weight:800; }}
.hsub {{ color:{MUTED}; font-size:13px; margin-top:2px; }}
#cols {{ display:grid; grid-template-columns:1fr 360px 1fr; gap:14px; align-items:start; }}
.side {{ background:{SURFACE}; border:1px solid {BORDER}; border-radius:10px; overflow:hidden; }}
.sidehead {{ display:flex; align-items:center; gap:8px; padding:10px 12px; border-bottom:1px solid {BORDER}; background:{SURFACE2}; }}
.sidetag {{ font-size:10px; font-weight:800; letter-spacing:.8px; color:{MUTED}; white-space:nowrap; }}
.teamsel {{ flex:1; background:{BG}; color:{TEXT}; border:1px solid {BORDER}; border-radius:6px; padding:6px 8px; font-size:13px; font-weight:700; }}
.roster {{ max-height:74vh; overflow-y:auto; padding:8px; }}
.rolehdr {{ font-size:10px; font-weight:800; letter-spacing:.8px; color:{MUTED}; text-transform:uppercase; margin:10px 4px 4px; border-bottom:1px solid {BORDER}; padding-bottom:3px; }}
.prow {{ padding:6px 8px; border-radius:7px; cursor:pointer; border:1px solid transparent; margin-bottom:2px; }}
.prow:hover {{ background:{SURFACE2}; }}
.prow.sel {{ background:rgba(59,130,246,.14); border-color:{ACCENT}; }}
.prow.target {{ box-shadow:inset 3px 0 0 {GREEN}; }}
.prow-top {{ display:flex; align-items:center; gap:6px; }}
.pname {{ font-weight:700; font-size:13px; }}
.poschip {{ display:inline-block; font-size:9px; font-weight:800; letter-spacing:.4px; color:{TEXT}; background:{SURFACE2}; border:1px solid {BORDER}; border-radius:4px; padding:1px 4px; vertical-align:middle; }}
.tgt {{ font-size:12px; cursor:help; }}
.pill {{ margin-left:auto; font-size:11px; font-weight:800; padding:1px 7px; border-radius:9px; color:#0b1220; cursor:pointer; }}
.pstat {{ color:{MUTED}; font-size:11px; margin-top:1px; }}
.bd {{ display:none; margin-top:5px; padding:7px 8px; background:{SURFACE2}; border:1px solid {BORDER}; border-radius:6px; font-size:11.5px; line-height:1.5; color:{TEXT}; }}
.bd.open {{ display:block; }}
#mid {{ position:sticky; top:12px; background:{SURFACE}; border:1px solid {BORDER}; border-radius:10px; padding:14px; }}
#verdict {{ text-align:center; margin-bottom:10px; }}
.vpill {{ display:inline-block; font-weight:800; font-size:14px; padding:4px 14px; border-radius:12px; color:#0b1220; }}
.vwhy {{ color:{MUTED}; font-size:12px; margin-top:6px; }}
.ledger {{ display:grid; grid-template-columns:1fr 1fr; gap:8px; }}
.lhead {{ font-size:10px; font-weight:800; letter-spacing:.6px; padding-bottom:4px; border-bottom:1px solid {BORDER}; margin-bottom:5px; }}
.give-h {{ color:{RED}; }}
.get-h {{ color:{GREEN}; }}
.llist {{ min-height:40px; }}
.litem {{ font-size:12px; padding:3px 0; display:flex; align-items:center; gap:5px; }}
.litem .x {{ color:{MUTED}; cursor:pointer; font-weight:800; }}
.totrow {{ display:flex; justify-content:space-between; font-size:11px; color:{MUTED}; margin-top:8px; padding-top:8px; border-top:1px solid {BORDER}; }}
#reads {{ margin-top:10px; font-size:12px; line-height:1.6; }}
.chip {{ display:inline-block; font-size:10px; font-weight:700; padding:1px 6px; border-radius:8px; margin:1px 2px; border:1px solid {BORDER}; color:{MUTED}; }}
.chip.need {{ background:rgba(34,197,94,.16); border-color:{GREEN}; color:{GREEN}; }}
.chip.lose {{ background:rgba(239,68,68,.12); border-color:{RED}; color:{RED}; }}
.chip.pos {{ background:rgba(59,130,246,.14); border-color:{ACCENT}; color:{ACCENT}; }}
.readline {{ margin-top:6px; }}
.readlbl {{ color:{MUTED}; font-weight:700; }}
#clearBtn {{ margin-top:12px; width:100%; background:{SURFACE2}; color:{MUTED}; border:1px solid {BORDER}; border-radius:6px; padding:7px; font-size:12px; font-weight:700; cursor:pointer; }}
#clearBtn:hover {{ color:{TEXT}; }}
.empty {{ color:{MUTED}; font-size:12px; font-style:italic; }}
#coach {{ margin-top:12px; padding-top:10px; border-top:1px solid {BORDER}; }}
.coachhdr {{ font-size:10px; font-weight:800; letter-spacing:.8px; color:{ACCENT}; margin-bottom:6px; }}
.stratrow {{ display:flex; align-items:center; gap:5px; margin-bottom:8px; flex-wrap:wrap; }}
.stratlbl {{ font-size:10px; font-weight:700; color:{MUTED}; text-transform:uppercase; letter-spacing:.5px; margin-right:2px; }}
.stratbtn {{ font-size:11px; font-weight:700; color:{MUTED}; background:{SURFACE2}; border:1px solid {BORDER}; border-radius:6px; padding:3px 9px; cursor:pointer; }}
.stratbtn:hover {{ color:{TEXT}; }}
.stratbtn.active {{ color:#0b1220; background:{ACCENT}; border-color:{ACCENT}; }}
.ctxline {{ font-size:11.5px; line-height:1.7; }}
.ctxline .lbl {{ color:{MUTED}; font-weight:700; }}
.sugblock {{ margin-top:9px; }}
.sughdr {{ font-size:10px; font-weight:700; color:{MUTED}; text-transform:uppercase; letter-spacing:.5px; margin-bottom:4px; }}
.sugchip {{ display:inline-block; font-size:11px; color:{TEXT}; background:{SURFACE2}; border:1px solid {BORDER}; border-radius:6px; padding:2px 7px; margin:2px 3px 2px 0; cursor:pointer; }}
.sugchip:hover {{ border-color:{ACCENT}; background:rgba(59,130,246,.12); }}
.sugchip .plus {{ color:{GREEN}; font-weight:800; margin-right:3px; }}
.sugchip .sugwhy {{ color:{MUTED}; font-size:10px; margin-left:4px; }}
.sugchip .v {{ color:{ACCENT}; font-size:10px; font-weight:700; margin-left:5px; }}
.nudge {{ margin-top:9px; font-size:11.5px; line-height:1.5; color:{TEXT}; background:{SURFACE2}; border:1px solid {BORDER}; border-left:3px solid {ACCENT}; border-radius:5px; padding:6px 9px; }}
@media (max-width:1000px) {{
  #cols {{ grid-template-columns:1fr; }}
  #mid {{ position:static; order:-1; }}
  .roster {{ max-height:none; }}
}}
"""


_JS = r"""
var picked = {{ L:{{}}, R:{{}} }};   // id -> player, per side
var strategy = 'favor';              // fair | favor | fleece — how hard the coach tilts value to me
var TARGET_NET = {{ fair:0.0, favor:0.30, fleece:0.70 }};   // value edge the coach steers toward
var STUD_CEIL  = {{ fair:99, favor:1.6, fleece:1.2 }};      // don't suggest offering my pieces above this value

function setStrategy(s) {{ strategy = s; renderCoach(); }}

function pillColor(s) {{
  if (s >= 72) return '{GREEN}';
  if (s >= 52) return '{ACCENT}';
  if (s >= 32) return '{YELLOW}';
  return '{RED}';
}}

function teamOptions(sel, chosen) {{
  sel.innerHTML = '';
  DATA.teamKeys.forEach(function(tk) {{
    var o = document.createElement('option');
    o.value = tk; o.textContent = DATA.teamsMeta[tk].name;
    if (tk === chosen) o.selected = true;
    sel.appendChild(o);
  }});
}}

var ROLE_LABEL = {{ hit:'Hitters', sp:'Starting Pitchers', rp:'Relief Pitchers' }};

// Why a partner player is worth targeting for MY (left) team: fills a category need
// or (hitter) upgrades one of my thin positions. Reused from the digest's need logic.
function targetReasons(p, myMeta, poss) {{
  poss = poss || 'your';
  var out = [];
  var nc = (p.tcats || []).filter(function(c) {{ return (myMeta.needs || []).indexOf(c) >= 0; }});
  if (nc.length) out.push('fills ' + poss + ' ' + nc.map(function(c) {{ return DATA.catLabels[c] || c; }}).join('/') + ' need');
  if (p.role === 'hit' && myMeta.need_pos) {{
    (p.tgroups || []).forEach(function(pos) {{
      if ((pos in myMeta.need_pos) && p.score > myMeta.need_pos[pos]) out.push('upgrades ' + poss + ' ' + pos);
    }});
  }}
  return out;
}}

function renderRoster(side) {{
  var box = document.getElementById(side === 'L' ? 'rosterL' : 'rosterR');
  var tk = document.getElementById(side === 'L' ? 'selL' : 'selR').value;
  var pl = DATA.players[tk] || {{ hit:[], sp:[], rp:[] }};
  // Targets are shown on the PARTNER (right) side, judged against MY (left) team's needs.
  var myMeta = DATA.teamsMeta[document.getElementById('selL').value] || {{ needs:[], need_pos:{{}} }};
  var html = '';
  ['hit','sp','rp'].forEach(function(role) {{
    var rows = pl[role] || [];
    if (!rows.length) return;
    html += '<div class="rolehdr">' + ROLE_LABEL[role] + '</div>';
    rows.forEach(function(p) {{
      var on = picked[side][p.id] ? ' sel' : '';
      var pos = (p.posTokens || []).map(function(t) {{ return '<span class="poschip">' + t + '</span>'; }}).join(' ');
      if (pos) pos = ' ' + pos;
      var tgt = '', tgtCls = '';
      if (side === 'R') {{
        var tr = targetReasons(p, myMeta);
        if (tr.length) {{ tgt = ' <span class="tgt" title="Target &mdash; ' + tr.join('; ') + '">&#127919;</span>'; tgtCls = ' target'; }}
      }}
      html += '<div class="prow' + on + tgtCls + '" id="row-' + side + '-' + p.id + '">'
        + '<div class="prow-top" onclick="toggle(\'' + side + '\',\'' + p.id + '\')">'
        + p.logo + '<span class="pname">' + p.name + '</span>' + pos + p.badges + tgt
        + '<span class="pill" style="background:' + pillColor(p.score) + '" '
        + 'onclick="event.stopPropagation();openBd(\'' + side + '\',\'' + p.id + '\')">' + p.score + '</span>'
        + '</div>'
        + '<div class="pstat">' + p.stat + '</div>'
        + '<div class="bd" id="bd-' + side + '-' + p.id + '">' + (p.breakdown || 'No breakdown.') + '</div>'
        + '</div>';
    }});
  }});
  box.innerHTML = html || '<div class="empty">No rostered players.</div>';
}}

function findPlayer(tk, id) {{
  var pl = DATA.players[tk] || {{}};
  var all = (pl.hit||[]).concat(pl.sp||[], pl.rp||[]);
  for (var i=0;i<all.length;i++) if (all[i].id === id) return all[i];
  return null;
}}

function toggle(side, id) {{
  var tk = document.getElementById(side === 'L' ? 'selL' : 'selR').value;
  if (picked[side][id]) delete picked[side][id];
  else picked[side][id] = findPlayer(tk, id);
  var row = document.getElementById('row-' + side + '-' + id);
  if (row) row.classList.toggle('sel', !!picked[side][id]);
  recompute();
}}

function openBd(side, id) {{
  var bd = document.getElementById('bd-' + side + '-' + id);
  if (bd) bd.classList.toggle('open');
}}

function sumVal(obj) {{ var s=0; for (var k in obj) s += obj[k].tval; return s; }}

function unionCats(obj) {{
  var out = {{}};
  for (var k in obj) (obj[k].tcats||[]).forEach(function(c){{ out[c]=1; }});
  return Object.keys(out);
}}

function catChip(c, cls) {{
  var lbl = DATA.catLabels[c] || c;
  return '<span class="chip ' + cls + '">' + lbl + '</span>';
}}

function ledgerItem(side, p) {{
  return '<div class="litem">' + p.logo + '<span>' + p.name + '</span>'
    + '<span class="x" onclick="toggle(\'' + side + '\',\'' + p.id + '\')">&times;</span></div>';
}}

function flatPool(tk) {{
  var pl = DATA.players[tk] || {{}};
  return (pl.hit || []).concat(pl.sp || [], pl.rp || []);
}}

function needLabels(meta) {{
  var cats = (meta.needs || []).map(function(c) {{ return DATA.catLabels[c] || c; }});
  return cats.concat(Object.keys(meta.need_pos || {{}}));
}}

function sugChip(side, x) {{
  var why = x.r.length ? x.r[0] : 'top value chip';
  return '<span class="sugchip" onclick="toggle(\'' + side + '\',\'' + x.p.id + '\')">'
    + '<span class="plus">+</span>' + x.p.name
    + '<span class="sugwhy">' + why + '</span>'
    + '<span class="v">' + x.p.tval.toFixed(1) + '</span></span>';
}}

// The Deal Coach: match-up context + value-ranked, clickable add suggestions + a
// running balance nudge. Reuses targetReasons() from BOTH perspectives — partner
// players that fill MY needs (get) and my players that fill THEIRS (offer).
function renderCoach() {{
  var myTk = document.getElementById('selL').value;
  var partnerTk = document.getElementById('selR').value;
  var myMeta = DATA.teamsMeta[myTk] || {{ needs:[], surplus:[], need_pos:{{}} }};
  var partnerMeta = DATA.teamsMeta[partnerTk] || {{ needs:[], surplus:[], need_pos:{{}} }};

  var youNeed = needLabels(myMeta);
  var theyNeed = needLabels(partnerMeta);
  var leverage = (myMeta.surplus || []).filter(function(c) {{ return (partnerMeta.needs || []).indexOf(c) >= 0; }})
                   .map(function(c) {{ return DATA.catLabels[c] || c; }});

  function notPicked(p) {{ return !picked.L[p.id] && !picked.R[p.id]; }}
  function rank(pool, meta, poss) {{
    var out = pool.filter(notPicked).map(function(p) {{ return {{ p:p, r:targetReasons(p, meta, poss) }}; }})
                  .filter(function(x) {{ return x.r.length; }});
    out.sort(function(a, b) {{ return b.p.tval - a.p.tval; }});
    return out;
  }}
  // GET = partner players that fill MY needs; OFFER = my players that fill THEIRS.
  var getSug = rank(flatPool(partnerTk), myMeta, 'your');
  var giveSug = rank(flatPool(myTk), partnerMeta, 'their');
  // Strategy gates the OFFER list: the harder I favor myself, the more I protect my
  // studs (value ceiling) and the cheaper the need-filler I lead with (ascending value).
  var ceil = STUD_CEIL[strategy];
  giveSug = giveSug.filter(function(x) {{ return x.p.tval <= ceil; }});
  if (strategy !== 'fair') giveSug.sort(function(a, b) {{ return a.p.tval - b.p.tval; }});
  // Fallback so the biggest chips still surface when nothing squarely fills a need.
  if (!getSug.length) {{
    getSug = flatPool(partnerTk).filter(notPicked).map(function(p) {{ return {{ p:p, r:[] }}; }})
               .sort(function(a, b) {{ return b.p.tval - a.p.tval; }}).slice(0, 4);
  }}
  var getHtml = getSug.slice(0, 4).map(function(x) {{ return sugChip('R', x); }}).join('') || '<span class="empty">none</span>';
  var giveHtml = giveSug.slice(0, 4).map(function(x) {{ return sugChip('L', x); }}).join('')
                 || '<span class="empty">nothing spare that they need &mdash; lead with value</span>';

  var net = sumVal(picked.R) - sumVal(picked.L);
  var nSel = Object.keys(picked.L).length + Object.keys(picked.R).length;
  var target = TARGET_NET[strategy];
  var label = {{ fair:'fair', favor:'favor-me', fleece:'fleece' }}[strategy];
  var diff = net - target;
  var nudge;
  if (!nSel) nudge = 'Pick a player to give and a target to get &mdash; the suggestions above rank by trade value and update as you go.';
  else if (diff > 0.1) nudge = 'Ahead of your ' + label + ' target (net ' + (net >= 0 ? '+' : '') + net.toFixed(2) + '). You could add a give to sweeten it, or expect them to counter.';
  else if (diff < -0.1) nudge = 'Below your ' + label + ' target (net ' + (net >= 0 ? '+' : '') + net.toFixed(2) + '). Add a get piece, or drop one of yours.';
  else nudge = 'On target for a ' + label + ' deal (net ' + (net >= 0 ? '+' : '') + net.toFixed(2) + '). Make sure it fills a real need for both sides.';

  function stratBtn(s, lab) {{
    return '<button class="stratbtn' + (strategy === s ? ' active' : '') + '" onclick="setStrategy(\'' + s + '\')">' + lab + '</button>';
  }}
  document.getElementById('coach').innerHTML =
      '<div class="coachhdr">DEAL COACH</div>'
    + '<div class="stratrow"><span class="stratlbl">Strategy</span>'
      + stratBtn('fair', 'Fair') + stratBtn('favor', 'Favor me') + stratBtn('fleece', 'Fleece') + '</div>'
    + '<div class="ctxline"><span class="lbl">You need:</span> ' + (youNeed.join(', ') || 'balanced everywhere') + '</div>'
    + '<div class="ctxline"><span class="lbl">They need:</span> ' + (theyNeed.join(', ') || 'balanced everywhere') + '</div>'
    + (leverage.length ? '<div class="ctxline"><span class="lbl">Your leverage:</span> ' + leverage.join(', ') + ' &mdash; deep for you, thin for them</div>' : '')
    + '<div class="sugblock"><div class="sughdr">Add to get &mdash; fills your needs</div>' + getHtml + '</div>'
    + '<div class="sugblock"><div class="sughdr">Offer them &mdash; fills their needs</div>' + giveHtml + '</div>'
    + '<div class="nudge">' + nudge + '</div>';
}}

function recompute() {{
  var L = picked.L, R = picked.R;                       // L = give, R = get
  var lKeys = Object.keys(L), rKeys = Object.keys(R);
  var giveBox = document.getElementById('giveList');
  var getBox  = document.getElementById('getList');
  giveBox.innerHTML = lKeys.length ? lKeys.map(function(k){{return ledgerItem('L',L[k]);}}).join('')
                                   : '<div class="empty">Click your players &rarr;</div>';
  getBox.innerHTML  = rKeys.length ? rKeys.map(function(k){{return ledgerItem('R',R[k]);}}).join('')
                                   : '<div class="empty">&larr; Click theirs</div>';

  renderCoach();

  var vBox = document.getElementById('verdict');
  var reads = document.getElementById('reads');
  if (!lKeys.length && !rKeys.length) {{
    vBox.innerHTML = '<span class="vpill" style="background:{BORDER};color:{MUTED}">SELECT PLAYERS</span>';
    reads.innerHTML = '';
    return;
  }}

  var giveVal = sumVal(L), getVal = sumVal(R);
  var netVal = getVal - giveVal;                         // + = I win value

  var myTk = document.getElementById('selL').value;
  var partnerTk = document.getElementById('selR').value;
  var myMeta = DATA.teamsMeta[myTk] || {{needs:[],surplus:[],need_pos:{{}}}};
  var partnerMeta = DATA.teamsMeta[partnerTk] || {{needs:[]}};
  var myNeeds = myMeta.needs || [];

  // Categories gained (from what I get) vs lost (from what I give).
  var gained = unionCats(R), lost = unionCats(L);
  var needFilled = gained.filter(function(c){{ return myNeeds.indexOf(c) >= 0; }});

  // Positional upgrades: an incoming hitter at one of my thin slots whose score clears my avg there.
  var posFilled = {{}};
  rKeys.forEach(function(k){{
    var p = R[k];
    if (p.role !== 'hit') return;
    (p.tgroups||[]).forEach(function(pos){{
      if (myMeta.need_pos && (pos in myMeta.need_pos) && p.score > myMeta.need_pos[pos]) posFilled[pos]=1;
    }});
  }});
  var posList = Object.keys(posFilled);
  var addressesNeed = needFilled.length > 0 || posList.length > 0;

  // Timing: + = I sell-high / buy-low ; - = a trap (dealing a riser / buying a regressor).
  var timing = 0;
  lKeys.forEach(function(k){{ timing += (L[k].sell?1:0) - (L[k].buy?1:0); }});
  rKeys.forEach(function(k){{ timing += (R[k].buy?1:0) - (R[k].sell?1:0); }});
  var trap = timing < 0;

  // Verdict — the exact _pending_verdict thresholds (from my/left perspective).
  var label, color, why;
  if (netVal >= 0.1 && !trap) {{
    label='ACCEPT'; color='{GREEN}'; why='you win the value' + (addressesNeed?' and it fills a need':'');
  }} else if (addressesNeed && netVal >= -0.1 && !trap) {{
    label='ACCEPT'; color='{GREEN}'; why='roughly even value and it fills a real need';
  }} else if (addressesNeed) {{
    label='COUNTER'; color='{YELLOW}'; why='right direction but ' + (netVal < -0.1 ? "you'd be paying up" : 'the timing is a trap') + ' &mdash; ask for more';
  }} else if (netVal >= 0.1) {{
    label='ACCEPT'; color='{GREEN}'; why='you win the value';
  }} else {{
    label='DECLINE'; color='{RED}'; why='no need addressed and you don\'t gain value';
  }}
  vBox.innerHTML = '<span class="vpill" style="background:'+color+'">'+label+'</span>'
    + '<div class="vwhy">'+why+'</div>';

  // Value tilt phrase (Trade Radar wording, +/-0.1).
  var tilt = netVal > 0.1 ? 'you win the value' : (netVal < -0.1 ? 'you pay up' : 'even value');

  // Partner-fit: does what I give address THEIR needs?
  var partnerNeeds = partnerMeta.needs || [];
  var partnerGets = lost.filter(function(c){{ return partnerNeeds.indexOf(c) >= 0; }});
  var partnerFit = partnerGets.length
    ? 'fits their needs (' + partnerGets.map(function(c){{return DATA.catLabels[c]||c;}}).join(', ') + ') &mdash; realistic'
    : (lKeys.length ? 'doesn\'t hit their category needs &mdash; may be a tough sell' : '&mdash;');

  var timingTxt = timing > 0 ? 'in your favor (selling high / buying low)'
                : (timing < 0 ? 'a trap (dealing a riser or buying a regressor)' : 'neutral');

  var gainChips = gained.length ? gained.map(function(c){{
        return catChip(c, needFilled.indexOf(c)>=0 ? 'need':''); }}).join('') : '<span class="empty">none</span>';
  var loseChips = lost.length ? lost.map(function(c){{
        var strong = (myMeta.surplus||[]).indexOf(c) >= 0;   // fine to lose from a strength
        return catChip(c, strong ? '' : 'lose'); }}).join('') : '<span class="empty">none</span>';
  var posChips = posList.length ? posList.map(function(p){{ return '<span class="chip pos">'+p+'</span>'; }}).join('') : '';

  reads.innerHTML =
      '<div class="totrow"><span>Give value ' + giveVal.toFixed(2) + '</span>'
        + '<span>Net ' + (netVal>=0?'+':'') + netVal.toFixed(2) + ' &middot; ' + tilt + '</span>'
        + '<span>Get value ' + getVal.toFixed(2) + '</span></div>'
    + '<div class="readline"><span class="readlbl">You gain:</span> ' + gainChips + (posChips?' &nbsp; '+posChips:'') + '</div>'
    + '<div class="readline"><span class="readlbl">You lose:</span> ' + loseChips + '</div>'
    + '<div class="readline"><span class="readlbl">Timing:</span> ' + timingTxt + '</div>'
    + '<div class="readline"><span class="readlbl">Would they do it?</span> ' + partnerFit + '</div>';
}}

function clearAll() {{
  picked = {{ L:{{}}, R:{{}} }};
  renderRoster('L'); renderRoster('R'); recompute();
}}

function initSide(side) {{
  var sel = document.getElementById(side === 'L' ? 'selL' : 'selR');
  sel.addEventListener('change', function() {{
    picked[side] = {{}};                 // reset that side's picks on team change
    renderRoster('L'); renderRoster('R'); recompute();   // R targets depend on L's needs
  }});
}}

(function() {{
  var keys = DATA.teamKeys;
  var rDefault = keys.find(function(k){{ return k !== DATA.myTeam; }}) || keys[0];
  teamOptions(document.getElementById('selL'), DATA.myTeam);
  teamOptions(document.getElementById('selR'), rDefault);
  initSide('L'); initSide('R');
  renderRoster('L'); renderRoster('R'); recompute();
}})();
"""


def main():
    ap = argparse.ArgumentParser(description="Interactive Trade Lab (browser-only)")
    ap.add_argument("--refresh", action="store_true", help="Refresh snapshot data first (~60s)")
    ap.add_argument("--team", default=None, help="Default the LEFT (my) side to another team")
    args = ap.parse_args()

    if args.refresh:
        import fetch_data
        fetch_data.main()

    with open(SNAPSHOT, encoding="utf-8") as f:
        snap = json.load(f)

    my_team = args.team or snap.get("my_team", MY_TEAM)
    data = build_data(snap, my_team)
    html = build_html(data)

    PREVIEWS.mkdir(exist_ok=True)
    slug = _disp(my_team).replace(" ", "_")
    out = PREVIEWS / f"tradelab_{slug}.html"
    out.write_text(html, encoding="utf-8")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
