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
from datetime import datetime, timezone
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

    teams_meta, players, pos_data_by_team = {}, {}, {}
    for tk in team_keys:
        # Thin HITTER positions for this team → {pos: my_avg_score} (positional need).
        pos_data = sd.positional_breakdown(pitchers, hitters, tk, best_recent_p, best_recent_h)
        pos_data_by_team[tk] = pos_data   # reused by the Partner Fit board (per-POV engine run)
        need_pos, surplus_pos, pos_rank = {}, [], {}
        for p in pos_data:
            # League rank at this position/role (1 = best crew) → collapsed-section gauge.
            if p.get("rank") and p.get("n_teams"):
                pos_rank[p["pos"]] = {"rank": p["rank"], "n": p["n_teams"]}
            if p.get("ptype") != "hit":
                continue
            nt = p.get("n_teams") or n
            pt = max(1, round(nt / 3.0))
            if (p.get("rank") or nt) >= nt - pt + 1:
                need_pos[p["pos"]] = round(p.get("my_avg") or 0, 1)
            elif (p.get("rank") or nt) <= pt:
                surplus_pos.append(p["pos"])            # deep hitter positions (demand-side discount)
        teams_meta[tk] = {
            "name":     _disp(tk),
            "logo":     sd.fantasy_logo(team_logos.get(tk, ""), 24, tk),
            "needs":    needs_of(tk),
            "surplus":  surplus_of(tk),
            "need_pos": need_pos,
            "surplus_pos": surplus_pos,
            "pos_rank": pos_rank,   # {pos: {rank, n}} → collapsed-section league-rank gauge
            "pos_count": sd._team_position_counts(hitters, tk),   # redundancy + depth guard: bodies per position
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

    # Partner Fit board: one engine-graded deal per rival, from EVERY team's POV so the
    # board stays correct when the LEFT dropdown switches. See build_partner_fit.
    partner_fit = build_partner_fit(pitchers, hitters, roto, team_keys, ranks, n,
                                    best_recent_p, best_recent_h, hit_pctile, pit_pctile,
                                    pos_data_by_team)

    my_key = _key(my_team)
    if my_key not in players:
        my_key = _key(snap.get("my_team", MY_TEAM))
    return {
        "teamKeys":  team_keys,
        "teamsMeta": teams_meta,
        "players":   players,
        "partnerFit": partner_fit,
        "myTeam":    my_key,
        "catLabels": CAT_LABELS,
        "lowerBetter": sorted(sd._LOWER_BETTER),
        "posStarters": {p: sd.POS_STARTERS.get(p, 1) for p in ("C","1B","2B","3B","SS","OF")},
        "posSlack":  sd._POS_DEPTH_SLACK,   # redundancy guard: bench/flex bodies allowed beyond starters
        # Acceptance-model tuning baked from send_digest so the Lab JS can't drift from the digest:
        # graduated star reluctance + aggressive realistic band + demand-side need multiplier.
        "tune": {
            "starFloor": sd._STAR_RELUCT_FLOOR, "starSlope": sd._STAR_RELUCT_SLOPE,
            "starCap": sd._STAR_RELUCT_CAP, "realisticMax": sd._TRADE_REALISTIC_MAX,
            "needCat": sd._NEED_MULT_CAT, "needPos": sd._NEED_MULT_POS,
            "needSurplus": sd._NEED_MULT_SURPLUS, "needClamp": list(sd._NEED_MULT_CLAMP),
            "thinPosPenalty": sd._TRADE_THIN_POS_PENALTY,   # depth floor: read penalty per single-slot pos a team is left thin at
        },
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
# PARTNER FIT BOARD — "Who should you be trading with?"
# One concrete, engine-graded deal per rival, tiered by how landable it is. Reuses
# send_digest's real trade engine (find_trades_combined + _trade_tilt) so the reads
# match the digest Trade Radar verbatim. Precomputed from EVERY team's POV at build
# time (cheap arithmetic on already-enriched rows) so the board stays correct when the
# LEFT dropdown changes — a deliberate, contained use of the engine server-side (like
# dashboard.py), not the Lab's usual "JS only sums pre-computed numbers".
# ══════════════════════════════════════════════════════════════════════════════

# Tier sort order (best target first, dead ends last).
_FIT_TIER_ORDER = {"BEST": 0, "REACH": 1, "SLIM": 2, "ONEWAY": 3, "NOFIT": 4}
# Needs that are genuinely scarce to fill (catcher / shortstop bats, saves+holds) — a
# deal landing one of these is preferred when choosing the single best deal per rival.
_FIT_SCARCE_POS = {"C", "SS"}


def _fit_deal_words(value_phrase, accept):
    """Plain-English one-liner for a graded deal → (sentence, tier_key)."""
    if accept == "realistic":
        if value_phrase == "you pay up":
            return ("You pay a hair, but a fair ask for the need.", "BEST")
        return ("Fair swap — they'd likely accept.", "BEST")
    # aggressive ask
    if value_phrase == "you win the value":
        return ("You come out ahead, but it's an aggressive ask — expect a counter.", "REACH")
    return ("Even on paper, but you'd be prying their guy — expect resistance.", "REACH")


def _fit_get_tags(ins, my_needs):
    """Short need tags an incoming player fills: thin positions (C/SS/...) + my need cats."""
    tags = []
    for p in ins:
        for pos in (p.get("_tfillpos") or []):
            if pos not in tags:
                tags.append(pos)
        for c in (p.get("_tcats") or []):
            if c in my_needs:
                lbl = CAT_LABELS.get(c, c)
                if lbl not in tags:
                    tags.append(lbl)
    return tags


def build_partner_fit(pitchers, hitters, roto, team_keys, ranks, n,
                      best_recent_p, best_recent_h, hit_pctile, pit_pctile, pos_data_by_team):
    """{pov_key: [rival fit record, ...]} — one engine-graded deal per rival per POV.

    Each record is either ACTIONABLE ({team, tier BEST|REACH, get:[{name,tags}], give:[name],
    verdict, whyOffer:[cat lbls], whyGet:[tags]}) or a DIAGNOSIS ({team, tier SLIM|ONEWAY|NOFIT,
    why}). Sorted by tier then team name."""
    third = max(1, round(n / 3.0)) if n else 1
    needs_of   = lambda t: {c for c, rk in ranks.get(t, {}).items() if rk >= n - third + 1}
    surplus_of = lambda t: {c for c, rk in ranks.get(t, {}).items() if rk <= third}

    out = {}
    # Raise the engine's per-team / total caps so every rival surfaces its best deal
    # (the digest only wants the top ~6 league-wide; the board wants one per partner).
    _save = (sd._TRADE_MAX_CARDS, sd._TRADE_PER_TEAM_CAP)
    try:
        sd._TRADE_MAX_CARDS, sd._TRADE_PER_TEAM_CAP = 400, 6
        for pov in team_keys:
            my_needs, my_surplus = needs_of(pov), surplus_of(pov)
            deals = sd.find_trades_combined(pitchers, hitters, roto, pov, best_recent_p,
                                            best_recent_h, pos_data_by_team.get(pov, []),
                                            hit_pctile, pit_pctile, cards=400)
            by_team = {}
            for d in deals:
                by_team.setdefault(d.get("team"), []).append(d)

            records = []
            for rival in team_keys:
                if rival == pov:
                    continue
                r_needs, r_surplus = needs_of(rival), surplus_of(rival)
                i_offer   = sorted(my_surplus & r_needs)    # my categorical reason for them
                they_offer = sorted(r_surplus & my_needs)   # what they can spare me
                cand = by_team.get(rival, [])

                def _score(d):
                    vp, ac, _ = sd._trade_tilt(d.get("net_val", 0), d.get("ins"), d.get("outs"),
                                               net_them=d.get("net_them"))
                    fillpos  = [pos for p in d["ins"] for pos in (p.get("_tfillpos") or [])]
                    fillcats = [c for p in d["ins"] for c in (p.get("_tcats") or [])]
                    scarce = any(pos in _FIT_SCARCE_POS for pos in fillpos) or ("SVHD" in fillcats)
                    getval = sum(_n(p.get("_tval")) for p in d["ins"])
                    return (ac == "realistic", scarce, getval)

                best = max(cand, key=_score) if cand else None
                if best:
                    vp, ac, _ = sd._trade_tilt(best.get("net_val", 0), best.get("ins"), best.get("outs"),
                                               net_them=best.get("net_them"))
                    words, tier = _fit_deal_words(vp, ac)
                    get = [{"name": p.get("PlayerName", ""),
                            "tags": _fit_get_tags([p], my_needs)} for p in best["ins"]]
                    records.append({
                        "team": rival, "tier": tier,
                        "get": get, "give": [p.get("PlayerName", "") for p in best["outs"]],
                        "verdict": words,
                        "whyOffer": [CAT_LABELS.get(c, c) for c in i_offer],
                        "whyGet": _fit_get_tags(best["ins"], my_needs),
                    })
                elif not i_offer and not they_offer:
                    records.append({"team": rival, "tier": "NOFIT",
                        "why": "Category twins — you share the same strengths and the same holes. "
                               "Nothing to arbitrage."})
                elif not i_offer:
                    records.append({"team": rival, "tier": "ONEWAY",
                        "why": "They have pieces you'd want, but they're strong everywhere you are — "
                               "you can't fill a hole of theirs, so you'd overpay."})
                else:
                    records.append({"team": rival, "tier": "SLIM",
                        "why": "Some overlap on paper, but no clean, near-even deal came together. "
                               "Worth a manual look."})

            records.sort(key=lambda r: (_FIT_TIER_ORDER[r["tier"]], r["team"]))
            out[pov] = records
    finally:
        sd._TRADE_MAX_CARDS, sd._TRADE_PER_TEAM_CAP = _save
    return out


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
    refresh_btn = ('<button id="refreshBtn" class="refreshbtn" onclick="doRefresh()">'
                   '&#8635; Refresh data</button>') if data.get("refreshUrl") else ""
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
    <div class="headright">
      <div class="fresh" title="Snapshot refresh time — rerun with --refresh to update"><span class="dot" style="background:{fresh_color}"></span><span>Data: {fresh_label}</span></div>
      {refresh_btn}
    </div>
  </div>
  <details id="fitboard" open>
    <summary class="fbsum">
      <div class="fbhead"><span class="fbtitle">Who should you be trading with?</span><span class="fbtoggle">Targets</span></div>
      <div class="fbsub" id="fbholes"></div>
    </summary>
    <div class="fblegend">
      <span><b style="color:{GREEN}">BEST TARGET</b> &mdash; realistic deal, lands a need</span>
      <span><b style="color:{YELLOW}">WORTH A SHOT</b> &mdash; good deal, aggressive ask</span>
      <span><b style="color:{MUTED}">SLIM / ONE-WAY / NO DEAL</b> &mdash; why not</span>
    </div>
    <div class="fblist" id="fblist"></div>
  </details>
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
  <div id="dealbar" onclick="jumpToDeal()">
    <div id="dbsummary">Tap players to build a deal</div>
    <div id="dbverdict"></div>
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
.headright {{ display:flex; align-items:center; gap:10px; flex-wrap:wrap; }}
.fresh {{ font-size:11.5px; color:{MUTED}; white-space:nowrap; display:flex; align-items:center; gap:6px; padding-top:5px; }}
.refreshbtn {{ font-size:12px; font-weight:800; color:#0b1220; background:{ACCENT}; border:1px solid {ACCENT}; border-radius:7px; padding:7px 13px; cursor:pointer; white-space:nowrap; }}
.refreshbtn:hover {{ filter:brightness(1.08); }}
.refreshbtn:disabled {{ background:{SURFACE2}; color:{MUTED}; border-color:{BORDER}; cursor:default; }}
/* Bottom "deal bar" — hidden on desktop, shown only on phones (see the 640px block). */
#dealbar {{ display:none; position:fixed; left:0; right:0; bottom:0; z-index:20; align-items:center; justify-content:space-between; gap:10px; padding:9px 14px; background:{SURFACE}; border-top:1px solid {BORDER}; box-shadow:0 -4px 16px rgba(0,0,0,.35); cursor:pointer; }}
#dbsummary {{ font-size:13px; color:{TEXT}; }}
.dot {{ width:9px; height:9px; border-radius:50%; display:inline-block; flex:0 0 auto; }}
.htitle {{ font-size:22px; font-weight:800; }}
.hsub {{ color:{MUTED}; font-size:13px; margin-top:2px; }}
#cols {{ display:grid; grid-template-columns:1fr 1fr 1fr; gap:14px; align-items:start; }}
.side {{ background:{SURFACE}; border:1px solid {BORDER}; border-radius:10px; overflow:hidden; }}
.sidehead {{ display:flex; align-items:center; gap:8px; padding:10px 12px; border-bottom:1px solid {BORDER}; background:{SURFACE2}; }}
.sidetag {{ font-size:10px; font-weight:800; letter-spacing:.8px; color:{MUTED}; white-space:nowrap; }}
.teamsel {{ flex:1; background:{BG}; color:{TEXT}; border:1px solid {BORDER}; border-radius:6px; padding:6px 8px; font-size:13px; font-weight:700; }}
.roster {{ max-height:74vh; overflow-y:auto; padding:8px; }}
.rolehdr {{ display:flex; align-items:center; gap:6px; font-size:10px; font-weight:800; letter-spacing:.8px; color:{MUTED}; text-transform:uppercase; margin:10px 4px 4px; border-bottom:1px solid {BORDER}; padding-bottom:3px; cursor:pointer; user-select:none; }}
.rolehdr:hover {{ color:{TEXT}; }}
.caret {{ font-size:9px; color:{MUTED}; width:10px; flex:0 0 auto; }}
.rolelbl {{ flex:0 0 auto; }}
.rolecount {{ font-size:9px; font-weight:700; color:{MUTED}; background:{SURFACE2}; border-radius:8px; padding:0 6px; }}
.possubhdr {{ display:flex; align-items:center; gap:6px; margin:8px 4px 3px 10px; }}
.posname {{ font-size:10px; font-weight:800; letter-spacing:.5px; color:{TEXT}; }}
.poscount {{ font-size:9px; font-weight:700; color:{MUTED}; }}
.gauge {{ margin-left:auto; font-size:9.5px; font-weight:800; color:#0b1220; border-radius:8px; padding:1px 7px; }}
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
.counteradd {{ color:{ACCENT}; cursor:pointer; font-weight:800; }}
.counteradd:hover {{ text-decoration:underline; }}
.ledger {{ display:grid; grid-template-columns:1fr 1fr; gap:8px; }}
.lhead {{ font-size:10px; font-weight:800; letter-spacing:.6px; padding-bottom:4px; border-bottom:1px solid {BORDER}; margin-bottom:5px; }}
.give-h {{ color:{RED}; }}
.get-h {{ color:{GREEN}; }}
.llist {{ min-height:40px; }}
.litem {{ font-size:12px; padding:3px 0; display:flex; align-items:center; gap:5px; }}
.litem .x {{ color:{MUTED}; cursor:pointer; font-weight:800; }}
.totrow {{ display:flex; justify-content:space-between; font-size:11px; color:{MUTED}; margin-top:8px; padding-top:8px; border-top:1px solid {BORDER}; }}
.valgrid {{ display:grid; grid-template-columns:auto 1fr 1fr 1fr; gap:2px 10px; font-size:11px; margin-top:8px; padding-top:8px; border-top:1px solid {BORDER}; align-items:center; }}
.valgrid .vgh {{ color:{MUTED}; font-weight:700; text-align:right; font-size:10px; text-transform:uppercase; letter-spacing:.03em; }}
.valgrid .vgh:first-child {{ text-align:left; }}
.valgrid .vgl {{ color:{TEXT}; font-weight:700; }}
.valgrid .vgc {{ text-align:right; color:{MUTED}; font-variant-numeric:tabular-nums; }}
.valgrid .vgpos {{ color:{GREEN}; font-weight:700; }}
.valgrid .vgneg {{ color:{RED}; font-weight:700; }}
.valgrid .vgeven {{ color:{TEXT}; font-weight:700; }}
.dealsum {{ color:{TEXT}; font-size:12px; font-style:italic; margin-top:8px; padding-top:8px; border-top:1px solid {BORDER}; }}
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
.coachhdr {{ display:flex; align-items:center; gap:6px; font-size:10px; font-weight:800; letter-spacing:.8px; color:{ACCENT}; margin-bottom:6px; cursor:pointer; user-select:none; }}
.coachhdr:hover {{ color:{TEXT}; }}
.coachhdr .caret {{ color:{ACCENT}; }}
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
/* Partner Fit board */
#fitboard {{ background:{SURFACE2}; border:1px solid {BORDER}; border-radius:12px; margin-bottom:16px; overflow:hidden; }}
#fitboard > summary {{ list-style:none; cursor:pointer; padding:13px 16px; background:{SURFACE}; }}
#fitboard[open] > summary {{ border-bottom:1px solid {BORDER}; }}
#fitboard > summary::-webkit-details-marker {{ display:none; }}
.fbhead {{ display:flex; align-items:center; justify-content:space-between; gap:10px; }}
.fbtitle {{ font-size:17px; font-weight:800; }}
.fbtoggle {{ font-size:10.5px; font-weight:800; letter-spacing:.6px; color:{MUTED}; text-transform:uppercase; }}
#fitboard[open] .fbtoggle::after {{ content:' \\25B4'; }}
#fitboard:not([open]) .fbtoggle::after {{ content:' \\25BE'; }}
.fbsub {{ color:{MUTED}; font-size:12.5px; margin-top:3px; }}
.fbsub .w {{ color:{YELLOW}; font-weight:700; }}
.fblegend {{ display:flex; gap:16px; flex-wrap:wrap; font-size:11px; color:{MUTED}; padding:11px 16px; border-bottom:1px solid {BORDER}; }}
.fblist {{ display:grid; grid-template-columns:1fr 1fr; gap:10px; padding:14px 16px; }}
.fbcard {{ background:{SURFACE}; border:1px solid {BORDER}; border-radius:10px; padding:11px 13px; }}
.fbcard.dim {{ background:{SURFACE2}; opacity:.85; }}
.fbchead {{ display:flex; align-items:center; gap:9px; margin-bottom:7px; }}
.fbvchip {{ font-size:9px; font-weight:800; letter-spacing:.6px; border:1px solid; border-radius:5px; padding:2px 6px; white-space:nowrap; background:rgba(255,255,255,.02); }}
.fbteam {{ font-weight:800; font-size:14px; flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
.fbbuild {{ font-size:10px; font-weight:800; color:{ACCENT}; border:1px solid {ACCENT}; border-radius:6px; padding:2px 8px; white-space:nowrap; cursor:pointer; }}
.fbbuild:hover {{ background:rgba(59,130,246,.14); }}
.fbdeal {{ font-size:13.5px; line-height:1.5; }}
.fbget {{ font-size:8.5px; font-weight:800; letter-spacing:.4px; color:{GREEN}; background:rgba(34,197,94,.14); border-radius:4px; padding:1px 5px; margin-right:5px; vertical-align:middle; }}
.fbdeal b {{ font-weight:700; }}
.fbgive {{ color:{MUTED}; font-size:12.5px; margin-top:2px; }}
.fbgv {{ font-size:8.5px; font-weight:800; letter-spacing:.4px; color:{RED}; background:rgba(239,68,68,.11); border-radius:4px; padding:1px 5px; margin-right:5px; vertical-align:middle; text-transform:uppercase; }}
.fbntag {{ font-size:8.5px; font-weight:800; color:#22d3ee; background:rgba(34,211,238,.12); border:1px solid rgba(34,211,238,.4); border-radius:4px; padding:0 4px; vertical-align:middle; margin-left:2px; }}
.fbverdict {{ font-size:12px; font-weight:700; margin-top:8px; }}
.fbwhy {{ font-size:11.5px; color:{TEXT}; margin-top:6px; line-height:1.5; }}
.fbwhy .wl {{ color:{MUTED}; font-weight:700; }}
@media (max-width:1000px) {{
  #cols {{ grid-template-columns:1fr; }}
  #mid {{ position:static; order:-1; }}
  .roster {{ max-height:none; }}
  .fblist {{ grid-template-columns:1fr; }}
}}
/* Pocket (phone) layout — additive; desktop above is untouched. Bigger tap targets,
   a sticky team-section header, and an always-visible bottom deal bar. */
@media (max-width:640px) {{
  #app {{ padding:10px 10px 76px; }}   /* bottom padding clears the fixed deal bar */
  #head {{ margin-bottom:10px; }}
  .htitle {{ font-size:19px; }}
  .hsub {{ display:none; }}
  .headright {{ width:100%; justify-content:space-between; }}
  .refreshbtn {{ font-size:13.5px; padding:10px 16px; flex:1; }}
  .sidehead {{ position:sticky; top:0; z-index:5; padding:11px 12px; }}
  .teamsel {{ padding:10px; font-size:14.5px; }}
  .roster {{ padding:10px; }}
  .prow {{ padding:11px 10px; margin-bottom:4px; }}
  .pname {{ font-size:14.5px; }}
  .pill {{ font-size:13px; padding:4px 11px; }}
  .pstat {{ font-size:12px; }}
  .poschip {{ font-size:10px; padding:2px 5px; }}
  .rolehdr {{ padding:9px 4px 5px; font-size:11.5px; }}   /* bigger fold tap target */
  .possubhdr {{ margin:9px 6px 4px 8px; }}
  .posname {{ font-size:11.5px; }}
  .gauge {{ font-size:11px; padding:2px 9px; }}
  #mid {{ padding:12px; }}
  .fbtitle {{ font-size:15.5px; }}
  .fbcard {{ padding:12px; }}
  #dealbar {{ display:flex; }}
}}
"""


_JS = r"""
var picked = {{ L:{{}}, R:{{}} }};   // id -> player, per side
var strategy = 'favor';              // fair | favor | fleece — how hard the coach tilts value to me
var collapsed = {{ L:{{}}, R:{{}} }};  // side -> role -> bool; persists per-role section fold state across re-renders
var coachFold = false;               // Deal Coach collapsed? persists across re-renders
var TARGET_NET = {{ fair:0.0, favor:0.30, fleece:0.70 }};   // value edge the coach steers toward
var STUD_CEIL  = {{ fair:99, favor:1.6, fleece:1.2 }};      // don't suggest offering my pieces above this value

function setStrategy(s) {{ strategy = s; renderCoach(); }}

// ---- Pocket: in-page data refresh (fires the GitHub workflow via the Worker proxy) ----
var _pollTimer = null, _pollTries = 0;
function doRefresh() {{
  if (!DATA.refreshUrl) return;
  var b = document.getElementById('refreshBtn');
  if (b) {{ b.disabled = true; b.textContent = 'Refreshing... (~2-3 min)'; }}
  // Only start polling if the Worker actually FIRED the dispatch (202 {{ok:true}}). A 502
  // (bad/insufficient token) or a network/CORS failure must surface, not silently poll
  // forever for a build that will never come.
  fetch(DATA.refreshUrl, {{ method:'POST' }})
    .then(function(r) {{
      return r.json().catch(function() {{ return {{}}; }}).then(function(j) {{ return {{ ok:r.ok, body:j }}; }});
    }})
    .then(function(res) {{
      if (res.ok && res.body && res.body.ok) {{ startPoll(); }}
      else {{ refreshFailed(b, res.body); }}
    }}, function() {{ refreshFailed(b, null); }});
}}
function refreshFailed(b, body) {{
  if (b) {{ b.disabled = false; b.innerHTML = '&#8635; Refresh data'; }}
  var code = body && body.status ? ' (GitHub ' + body.status + ')' : '';
  alert('Refresh could not start' + code + ' - the page was NOT updated. The refresh '
      + 'token likely needs "Contents: Read and write" permission (see worker/README.md).');
}}
function startPoll() {{
  _pollTries = 0;
  if (_pollTimer) clearInterval(_pollTimer);
  _pollTimer = setInterval(checkBuild, 15000);
}}
function checkBuild() {{
  if (++_pollTries > 24) {{                     // ~6 min ceiling
    clearInterval(_pollTimer);
    var b = document.getElementById('refreshBtn');
    if (b) {{ b.disabled = false; b.innerHTML = '&#8635; Refresh data'; }}
    alert('Still building - give it another minute, then reload.');
    return;
  }}
  fetch('build.json?t=' + Date.now(), {{ cache:'no-store' }})
    .then(function(r) {{ return r.json(); }})
    .then(function(j) {{ if (j && j.built_at && j.built_at !== DATA.builtAt) location.reload(); }})
    .catch(function() {{}});
}}

// ---- Pocket: bottom deal bar (always-visible running grade on phones) ----
function setDealBar(giveN, getN, net, label, color) {{
  var s = document.getElementById('dbsummary'), v = document.getElementById('dbverdict');
  if (!s) return;
  if (!giveN && !getN) {{ s.textContent = 'Tap players to build a deal'; v.innerHTML = ''; return; }}
  var nc = net > 0.1 ? '{GREEN}' : (net < -0.1 ? '{RED}' : '{MUTED}');
  var nt = (net > 0 ? '+' : '') + net.toFixed(2);
  s.innerHTML = 'Give ' + giveN + ' &middot; Get ' + getN + ' &middot; <b style="color:' + nc + '">net ' + nt + '</b>';
  v.innerHTML = label ? '<span class="vpill" style="background:' + color + ';font-size:12px;padding:3px 11px">' + label + '</span>' : '';
}}
function jumpToDeal() {{
  var m = document.getElementById('mid');
  if (m) m.scrollIntoView({{ behavior:'smooth', block:'start' }});
}}

function pillColor(s) {{
  if (s >= 72) return '{GREEN}';
  if (s >= 52) return '{ACCENT}';
  if (s >= 32) return '{YELLOW}';
  return '{RED}';
}}

// League-rank gauge for a collapsed section header: at a glance "this is the #2 SP
// crew" / "these bats are 2nd-worst" is more contextual than the top player's score.
function ordinal(k) {{
  var s = ['th','st','nd','rd'], v = k % 100;
  return k + (s[(v - 20) % 10] || s[v] || s[0]);
}}
function rankPhrase(rank, n) {{
  if (rank <= 1) return 'best of ' + n;
  if (rank >= n) return 'worst of ' + n;
  if (rank <= n / 2) return ordinal(rank) + '-best of ' + n;
  return ordinal(n - rank + 1) + '-worst of ' + n;
}}
function rankBadge(rd, label) {{
  if (!rd || !rd.n) return '';
  var rank = rd.rank, n = rd.n, third = Math.max(1, Math.round(n / 3));
  var col = rank <= third ? '{GREEN}' : (rank >= n - third + 1 ? '{RED}' : '{YELLOW}');
  return '<span class="gauge" style="background:' + col + '" '
    + 'title="' + label + ': ' + rankPhrase(rank, n) + ' in the league">#' + rank + '</span>';
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
var POS_GROUPS = ['C','1B','2B','3B','SS','OF','DH'];   // hitters group under EACH eligible slot (mirrors _POS_ORDER)

// Redundancy guard (mirrors send_digest._non_redundant_get_pos): a position is "stacked"
// when acquiring these players leaves me more eligible bodies than startable slots + one
// bench (posStarters[P] + posSlack) AND I shed nobody eligible there. So a 4th catcher
// stops reading as "fills your C" unless the deal also deals a catcher back (a swap).
function posStacked(pos, myMeta, giveList, getList) {{
  var added = (getList  || []).filter(function(p) {{ return (p.tgroups || []).indexOf(pos) >= 0; }}).length;
  var shed  = (giveList || []).filter(function(p) {{ return (p.tgroups || []).indexOf(pos) >= 0; }}).length;
  var post  = (((myMeta.pos_count || {{}})[pos]) || 0) - shed + added;
  var cap   = ((DATA.posStarters || {{}})[pos] || 1) + (DATA.posSlack || 0);
  return post > cap && shed === 0;
}}

// Depth floor (mirrors send_digest._leaves_position_short): hitter positions where GIVING
// `giveList` and RECEIVING `getList` drops a team below its startable bodies (DATA.posStarters)
// with no same-position body back. Body-count check, INDEPENDENT of tval — catches "their only
// catcher for 2 OF" that the value sums miss. `counts` = teamsMeta[tk].pos_count of the GIVING team.
function leavesShort(counts, giveList, getList) {{
  var short = [], starters = DATA.posStarters || {{}}, seen = {{}};
  (giveList || []).forEach(function(p) {{ (p.tgroups || []).forEach(function(g) {{ seen[g] = 1; }}); }});
  Object.keys(seen).forEach(function(P) {{
    if (!(P in starters)) return;   // hitter slots only (SP/RP absent from posStarters)
    var leaving  = (giveList || []).filter(function(p) {{ return (p.tgroups || []).indexOf(P) >= 0; }}).length;
    var arriving = (getList  || []).filter(function(p) {{ return (p.tgroups || []).indexOf(P) >= 0; }}).length;
    if (((counts || {{}})[P] || 0) - leaving + arriving < starters[P]) short.push(P);
  }});
  return short;
}}

// Single-slot positions where the GIVING team is left at EXACTLY the floor (starter, no backup) —
// the "thin, not clean" borderline that the honest read flags (mirrors find_trades' thin_them).
function thinPos(counts, giveList, getList) {{
  var thin = [], starters = DATA.posStarters || {{}}, seen = {{}};
  (giveList || []).forEach(function(p) {{ (p.tgroups || []).forEach(function(g) {{ seen[g] = 1; }}); }});
  Object.keys(seen).forEach(function(P) {{
    if (starters[P] !== 1) return;   // single-slot hitter positions only
    var leaving  = (giveList || []).filter(function(p) {{ return (p.tgroups || []).indexOf(P) >= 0; }}).length;
    var arriving = (getList  || []).filter(function(p) {{ return (p.tgroups || []).indexOf(P) >= 0; }}).length;
    if (((counts || {{}})[P] || 0) - leaving + arriving === starters[P]) thin.push(P);
  }});
  return thin;
}}

// Why a partner player is worth targeting for MY (left) team: fills a category need
// or (hitter) upgrades one of my thin positions. Reused from the digest's need logic.
function targetReasons(p, myMeta, poss) {{
  poss = poss || 'your';
  var out = [];
  var nc = (p.tcats || []).filter(function(c) {{ return (myMeta.needs || []).indexOf(c) >= 0; }});
  if (nc.length) out.push('fills ' + poss + ' ' + nc.map(function(c) {{ return DATA.catLabels[c] || c; }}).join('/') + ' need');
  if (p.role === 'hit' && myMeta.need_pos) {{
    (p.tgroups || []).forEach(function(pos) {{
      // Judge this single add: get=[p], give=[] — suppressed once the slot is already stacked.
      if ((pos in myMeta.need_pos) && p.score > myMeta.need_pos[pos] && !posStacked(pos, myMeta, [], [p]))
        out.push('upgrades ' + poss + ' ' + pos);
    }});
  }}
  return out;
}}

// Bucket hitters under EVERY eligible position (a 2B/SS/OF bat appears in all three)
// so each group reads as "the team's strength at that slot". Rows keep their incoming
// score-desc order; a bat with no recognized slot lands in UTIL so nobody is dropped.
function groupHitters(rows) {{
  var groups = {{}}; POS_GROUPS.forEach(function(p) {{ groups[p] = []; }});
  var util = [];
  rows.forEach(function(p) {{
    var toks = (p.posTokens || []).filter(function(t) {{ return POS_GROUPS.indexOf(t) >= 0; }});
    if (!toks.length) {{ util.push(p); return; }}
    toks.forEach(function(t) {{ groups[t].push(p); }});
  }});
  return {{ groups: groups, util: util }};
}}

// A small colored strength gauge for a section header = the group's BEST role score,
// so a FOLDED section still says "worth digging?" at a glance. Empty for empty groups.
function gaugeHtml(rows) {{
  if (!rows.length) return '';
  var best = rows.reduce(function(m, p) {{ return p.score > m ? p.score : m; }}, 0);
  return '<span class="gauge" style="background:' + pillColor(best) + '" '
    + 'title="Best role score here">' + best + '</span>';
}}

// One player row. `gkey` makes the DOM ids unique when a multi-eligible hitter is
// duplicated across position groups; selection stays in sync because toggle() keys off
// data-pid across EVERY copy on the side, not a single element id.
function playerRowHtml(side, p, gkey, myMeta) {{
  var on = picked[side][p.id] ? ' sel' : '';
  var pos = (p.posTokens || []).map(function(t) {{ return '<span class="poschip">' + t + '</span>'; }}).join(' ');
  if (pos) pos = ' ' + pos;
  var tgt = '', tgtCls = '';
  if (side === 'R') {{
    var tr = targetReasons(p, myMeta);
    if (tr.length) {{ tgt = ' <span class="tgt" title="Target &mdash; ' + tr.join('; ') + '">&#127919;</span>'; tgtCls = ' target'; }}
  }}
  var bid = 'bd-' + side + '-' + gkey + '-' + p.id;
  return '<div class="prow' + on + tgtCls + '" id="row-' + side + '-' + gkey + '-' + p.id + '" '
    + 'data-pid="' + p.id + '" data-side="' + side + '">'
    + '<div class="prow-top" onclick="toggle(\'' + side + '\',\'' + p.id + '\')">'
    + p.logo + '<span class="pname">' + p.name + '</span>' + pos + p.badges + tgt
    + '<span class="pill" style="background:' + pillColor(p.score) + '" '
    + 'onclick="event.stopPropagation();openBd(\'' + bid + '\')">' + p.score + '</span>'
    + '</div>'
    + '<div class="pstat">' + p.stat + '</div>'
    + '<div class="bd" id="' + bid + '">' + (p.breakdown || 'No breakdown.') + '</div>'
    + '</div>';
}}

function renderRoster(side) {{
  var box = document.getElementById(side === 'L' ? 'rosterL' : 'rosterR');
  var tk = document.getElementById(side === 'L' ? 'selL' : 'selR').value;
  var pl = DATA.players[tk] || {{ hit:[], sp:[], rp:[] }};
  // Targets are shown on the PARTNER (right) side, judged against MY (left) team's needs.
  var myMeta = DATA.teamsMeta[document.getElementById('selL').value] || {{ needs:[], need_pos:{{}} }};
  // League-rank gauges use the RENDERED side's own team meta (correct for either column).
  var pr_rank = (DATA.teamsMeta[tk] || {{}}).pos_rank || {{}};
  var cs = collapsed[side] || (collapsed[side] = {{}});
  var html = '';
  ['hit','sp','rp'].forEach(function(role) {{
    var rows = pl[role] || [];
    if (!rows.length) return;
    var fold = !!cs[role];
    // Role header is a tap target (fold/unfold). Hitters carry per-position gauges
    // inside, so only SP/RP get a whole-section gauge on the header itself.
    html += '<div class="rolehdr" onclick="toggleSection(\'' + side + '\',\'' + role + '\')">'
      + '<span class="caret">' + (fold ? '&#9654;' : '&#9660;') + '</span>'
      + '<span class="rolelbl">' + ROLE_LABEL[role] + '</span>'
      + '<span class="rolecount">' + rows.length + '</span>'
      + (role === 'hit' ? '' : (rankBadge(pr_rank[role === 'sp' ? 'SP' : 'RP'], ROLE_LABEL[role]) || gaugeHtml(rows)))
      + '</div>';
    html += '<div class="secbody"' + (fold ? ' style="display:none"' : '') + '>';
    if (role === 'hit') {{
      var g = groupHitters(rows);
      POS_GROUPS.forEach(function(pos) {{
        var pr = g.groups[pos];
        if (!pr.length) return;
        html += '<div class="possubhdr"><span class="posname">' + pos + '</span>'
          + '<span class="poscount">' + pr.length + '</span>'
          + (rankBadge(pr_rank[pos], pos) || gaugeHtml(pr)) + '</div>';
        pr.forEach(function(p) {{ html += playerRowHtml(side, p, pos, myMeta); }});
      }});
      if (g.util.length) {{
        html += '<div class="possubhdr"><span class="posname">UTIL</span>'
          + '<span class="poscount">' + g.util.length + '</span>' + gaugeHtml(g.util) + '</div>';
        g.util.forEach(function(p) {{ html += playerRowHtml(side, p, 'util', myMeta); }});
      }}
    }} else {{
      rows.forEach(function(p) {{ html += playerRowHtml(side, p, role, myMeta); }});
    }}
    html += '</div>';
  }});
  box.innerHTML = html || '<div class="empty">No rostered players.</div>';
}}

function toggleSection(side, role) {{
  var cs = collapsed[side] || (collapsed[side] = {{}});
  cs[role] = !cs[role];
  renderRoster(side);
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
  var on = !!picked[side][id];
  // A multi-eligible hitter has one row per position group — keep every copy in sync.
  var rows = document.querySelectorAll('.prow[data-side="' + side + '"][data-pid="' + id + '"]');
  for (var i = 0; i < rows.length; i++) rows[i].classList.toggle('sel', on);
  recompute();
}}

function openBd(bid) {{
  var bd = document.getElementById(bid);
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
  var body = coachFold ? '' :
      '<div class="stratrow"><span class="stratlbl">Strategy</span>'
      + stratBtn('fair', 'Fair') + stratBtn('favor', 'Favor me') + stratBtn('fleece', 'Fleece') + '</div>'
    + '<div class="ctxline"><span class="lbl">You need:</span> ' + (youNeed.join(', ') || 'balanced everywhere') + '</div>'
    + '<div class="ctxline"><span class="lbl">They need:</span> ' + (theyNeed.join(', ') || 'balanced everywhere') + '</div>'
    + (leverage.length ? '<div class="ctxline"><span class="lbl">Your leverage:</span> ' + leverage.join(', ') + ' &mdash; deep for you, thin for them</div>' : '')
    + '<div class="sugblock"><div class="sughdr">Add to get &mdash; fills your needs</div>' + getHtml + '</div>'
    + '<div class="sugblock"><div class="sughdr">Offer them &mdash; fills their needs</div>' + giveHtml + '</div>'
    + '<div class="nudge">' + nudge + '</div>';
  document.getElementById('coach').innerHTML =
      '<div class="coachhdr" onclick="toggleCoach()"><span class="caret">' + (coachFold ? '&#9654;' : '&#9660;') + '</span>DEAL COACH</div>'
    + body;
}}

function toggleCoach() {{ coachFold = !coachFold; renderCoach(); }}

// When a COUNTER verdict is driven by overpaying, name the single best partner
// add-on to REQUEST — a spare piece that closes the value gap without a fresh
// overpay, preferring one that fills a need of mine (then buy-low, then the value
// closest to the gap). Mirrors send_digest _counter_suggestion. Returns a player
// (with _cr = the need-reasons array) or null. gap = how much I'm overpaying (>0).
function counterAddon(partnerTk, myMeta, gap) {{
  if (gap <= 0.1) return null;
  var lo = 0.7 * gap, hi = gap + 0.45;                  // enough to close it, not a new overpay
  var cands = flatPool(partnerTk).filter(function(p) {{
    return !picked.L[p.id] && !picked.R[p.id] && p.tval >= lo && p.tval <= hi;
  }});
  if (!cands.length) return null;
  cands.forEach(function(p) {{ p._cr = targetReasons(p, myMeta, 'your'); }});
  cands.sort(function(a, b) {{
    var an = a._cr.length ? 1 : 0, bn = b._cr.length ? 1 : 0;
    if (bn !== an) return bn - an;                       // need-fillers first
    var ab = a.buy ? 1 : 0, bb = b.buy ? 1 : 0;
    if (bb !== ab) return bb - ab;                       // then buy-low pieces
    return Math.abs(a.tval - gap) - Math.abs(b.tval - gap);  // then closest to the gap
  }});
  return cands[0];
}}

function starRole(p) {{ return p.role === 'hit' || p.role === 'sp'; }}  // relievers not cross-role comparable

// JS mirror of send_digest._star_reluctance: graduated endowment premium (in tval) by role
// SCORE — 0 below the floor, rising per point, capped. Better player => bigger overpay to pry.
function starReluctance(score) {{
  var T = DATA.tune;
  return Math.max(0, Math.min(T.starCap, (score - T.starFloor) * T.starSlope));
}}

// JS mirror of send_digest._deal_star_reach: would a rival balk at parting with a prized
// player without a real overpay? Required overpay = premium(their best star-role acquire)
// minus premium(my best star-role give) (a star-for-star swap needs little). Reach (they
// balk) when that's positive AND I'm not paying up by at least it (net > -req). Relievers
// excluded both sides. Acceptance-layer only — drives the "Would they do it?" read.
function dealStarReach(getArr, giveArr, netVal) {{
  var getPrem = 0, givePrem = 0;
  getArr.forEach(function(p) {{ if (starRole(p)) getPrem = Math.max(getPrem, starReluctance(p.score)); }});
  giveArr.forEach(function(p) {{ if (starRole(p)) givePrem = Math.max(givePrem, starReluctance(p.score)); }});
  var req = Math.max(0, getPrem - givePrem);
  return req > 0 && netVal > -req;
}}

// MY-side mirror (send_digest._deal_star_surrender): would *I* balk at parting with a prized
// player without a real value win? Required premium = premium(my best star-role give) minus
// premium(my best star-role acquire). I hold out when that's positive AND I'm not winning by
// at least it (net < req). Same graduated curve, my side — drives "Would you do it?".
function dealStarSurrender(getArr, giveArr, netVal) {{
  var givePrem = 0, getPrem = 0;
  giveArr.forEach(function(p) {{ if (starRole(p)) givePrem = Math.max(givePrem, starReluctance(p.score)); }});
  getArr.forEach(function(p) {{ if (starRole(p)) getPrem = Math.max(getPrem, starReluctance(p.score)); }});
  var req = Math.max(0, givePrem - getPrem);
  return req > 0 && netVal < req;
}}

// JS mirror of send_digest._need_mult: demand-side team-need multiplier. A player is worth
// MORE to a team that needs his category/position, LESS if he only helps where they're deep.
// effective_value = tval * needMult. Acceptance-read layer only (never touches tval). This is
// what makes the same catcher worth more to a team that needs the slot than one already set.
function needMult(p, meta) {{
  var T = DATA.tune, cats = p.tcats || [], needs = meta.needs || [], surplus = meta.surplus || [];
  var m = 1.0;
  var inNeed = cats.filter(function(c) {{ return needs.indexOf(c) >= 0; }}).length;
  m += T.needCat * inNeed;
  var inSurplus = cats.some(function(c) {{ return surplus.indexOf(c) >= 0; }});
  if (cats.length && inNeed === 0 && inSurplus) m -= T.needSurplus;
  if (p.role === 'hit') {{
    var groups = p.tgroups || [], needPos = meta.need_pos || {{}}, surplusPos = meta.surplus_pos || [];
    if (groups.some(function(g) {{ return g in needPos; }})) {{
      m += T.needPos;
    }} else if (surplusPos.length && groups.length &&
               groups.every(function(g) {{ return surplusPos.indexOf(g) >= 0; }})) {{
      m -= T.needSurplus;
    }}
  }}
  return Math.max(T.needClamp[0], Math.min(T.needClamp[1], m));
}}
function sumEff(arr, meta) {{ var s = 0; arr.forEach(function(p) {{ s += (p.tval || 0) * needMult(p, meta); }}); return s; }}

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
    setDealBar(0, 0, 0, '', '');
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

  // Positional upgrades: an incoming hitter at one of my thin slots whose score clears my avg
  // there — but redundancy-guarded, so stacking a 4th body at a full slot doesn't count as
  // filling a need (unless the deal also sheds a body there). Package-aware: give=L, get=R.
  var giveArr = lKeys.map(function(k){{ return L[k]; }});
  var getArr  = rKeys.map(function(k){{ return R[k]; }});

  // Demand-side team-modified value (both POVs): each side re-values the same players by ITS
  // own needs. netMe drives MY verdict; netThem drives the "Would they do it?" read. Base
  // (netVal) stays the universal yardstick, shown alongside. Mirrors send_digest net_me/net_them.
  var netMe   = sumEff(getArr, myMeta) - sumEff(giveArr, myMeta);        // + = I win by MY needs
  var netThem = sumEff(giveArr, partnerMeta) - sumEff(getArr, partnerMeta);  // + = they win by THEIRS

  var posFilled = {{}};
  rKeys.forEach(function(k){{
    var p = R[k];
    if (p.role !== 'hit') return;
    (p.tgroups||[]).forEach(function(pos){{
      if (myMeta.need_pos && (pos in myMeta.need_pos) && p.score > myMeta.need_pos[pos]
          && !posStacked(pos, myMeta, giveArr, getArr)) posFilled[pos]=1;
    }});
  }});
  var posList = Object.keys(posFilled);
  var addressesNeed = needFilled.length > 0 || posList.length > 0;

  // Timing: + = I sell-high / buy-low ; - = a trap (dealing a riser / buying a regressor).
  var timing = 0;
  lKeys.forEach(function(k){{ timing += (L[k].sell?1:0) - (L[k].buy?1:0); }});
  rKeys.forEach(function(k){{ timing += (R[k].buy?1:0) - (R[k].sell?1:0); }});
  var trap = timing < 0;

  // Verdict — the exact _pending_verdict thresholds, now on netMe (my demand-side value) so it's
  // roster-aware: a need-filler is worth accepting even at slight base-value cost. The counter
  // gap stays on BASE value (what actually evens the deal).
  var label, color, why;
  if (netMe >= 0.1 && !trap) {{
    label='ACCEPT'; color='{GREEN}'; why='you win the value' + (addressesNeed?' and it fills a need':'');
  }} else if (addressesNeed && netMe >= -0.1 && !trap) {{
    label='ACCEPT'; color='{GREEN}'; why='roughly even value and it fills a real need';
  }} else if (addressesNeed) {{
    label='COUNTER'; color='{YELLOW}';
    if (netMe < -0.1) {{
      var add = counterAddon(partnerTk, myMeta, -netVal);
      if (add) {{
        var rz = add._cr && add._cr.length ? ' (' + add._cr.join(', ') + ')' : '';
        why = "right direction but you'd be paying up &mdash; counter: ask them to add "
            + '<b class="counteradd" onclick="toggle(\'R\',\'' + add.id + '\')">' + add.name + '</b>' + rz;
      }} else {{
        why = "right direction but you'd be paying up &mdash; ask for more (or offer a cheaper give)";
      }}
    }} else {{
      why = 'right direction but the timing is a trap &mdash; ask for more';
    }}
  }} else if (netMe >= 0.1) {{
    label='ACCEPT'; color='{GREEN}'; why='you win the value';
  }} else {{
    label='DECLINE'; color='{RED}'; why='no need addressed and you don\'t gain value';
  }}
  // MY-side star guard (mirror of the partner star-reach): an otherwise-ACCEPT that pries my
  // crown-jewel star at par is downgraded to COUNTER — I'd hold out (endowment/star bias).
  var starSurrender = dealStarSurrender(getArr, giveArr, netVal);
  // Depth floor (both parties, mirrors _leaves_position_short): who is left below startable
  // bodies at a hitter position. I give giveArr / receive getArr; the partner gives getArr
  // (what I take) / receives giveArr. thinThem = single-slot positions they're left at the floor.
  var leavesMeShort   = leavesShort(myMeta.pos_count, giveArr, getArr);
  var leavesThemShort = leavesShort(partnerMeta.pos_count, getArr, giveArr);
  var thinThem        = thinPos(partnerMeta.pos_count, getArr, giveArr);
  if (label === 'ACCEPT' && starSurrender) {{
    label='COUNTER'; color='{YELLOW}';
    why='they\'re prying your star at par &mdash; hold out for more';
  }} else if (label === 'ACCEPT' && leavesMeShort.length) {{
    label='COUNTER'; color='{YELLOW}';
    why='leaves you thin at '+leavesMeShort.join(', ')+' &mdash; get a replacement first';
  }}
  vBox.innerHTML = '<span class="vpill" style="background:'+color+'">'+label+'</span>'
    + '<div class="vwhy">'+why+'</div>';
  setDealBar(lKeys.length, rKeys.length, netVal, label, color);

  // Value tilt phrase (Trade Radar wording, +/-0.1).
  var tilt = netVal > 0.1 ? 'you win the value' : (netVal < -0.1 ? 'you pay up' : 'even value');

  // Partner-fit ("Would they do it?"): would the RIVAL accept? Aggressive & demand-side aware,
  // mirroring send_digest._trade_tilt(net_them). A STAR REACH (prying their best player without
  // an overpay) is always a tough sell; then, if they come out clearly behind by THEIR own
  // valuation (netThem < -realisticMax) it's an aggressive ask; else if my give hits their
  // category needs it's realistic; otherwise a likely tough sell.
  // Each acceptance read carries a SENTIMENT tier (yes/maybe/no) so it renders with a quick
  // color + thumb icon — the reader should grok "how each side feels" at a glance, then read why.
  var partnerNeeds = partnerMeta.needs || [];
  var partnerGets = lost.filter(function(c){{ return partnerNeeds.indexOf(c) >= 0; }});
  // Honest read: a thin single-slot loss penalizes their demand-side net (read only — the
  // displayed Their-value row stays pure), so the tilt naturally flips toward "aggressive ask".
  var thinNote = thinThem.length ? ' (no backup at ' + thinThem.join(', ') + ')' : '';
  var netThemRead = netThem - (DATA.tune.thinPosPenalty || 0) * thinThem.length;
  var pfTier, pfReason;
  if (!lKeys.length && !rKeys.length) {{
    pfTier = 'na'; pfReason = '&mdash;';
  }} else if (leavesThemShort.length) {{
    pfTier = 'no'; pfReason = 'they won\'t gut their ' + leavesThemShort.join(', ') + ' &mdash; no backup left';
  }} else if (dealStarReach(getArr, giveArr, netVal)) {{
    pfTier = 'no'; pfReason = 'they won\'t ship their star at even value';
  }} else if (netThemRead < -DATA.tune.realisticMax) {{
    pfTier = 'maybe'; pfReason = 'they come out behind on their own needs' + thinNote + ' &mdash; you\'d have to sweeten it';
  }} else if (partnerGets.length) {{
    pfTier = 'yes'; pfReason = 'it fills their needs (' + partnerGets.map(function(c){{return DATA.catLabels[c]||c;}}).join(', ') + ')' + thinNote;
  }} else {{
    pfTier = 'no'; pfReason = 'it doesn\'t address anything they need';
  }}

  // MY-side acceptance — the mirror of "Would they do it?". A star surrender at par is the read
  // that makes an even deal one *I* should hold out on, no matter how well the categories fit.
  var yfTier, yfReason;
  if (!lKeys.length && !rKeys.length) {{
    yfTier = 'na'; yfReason = '&mdash;';
  }} else if (starSurrender) {{
    yfTier = 'no'; yfReason = 'you\'d ship your star at even value &mdash; hold out for more';
  }} else if (leavesMeShort.length) {{
    yfTier = 'no'; yfReason = 'it leaves you without a backup at ' + leavesMeShort.join(', ') + ' &mdash; get a replacement first';
  }} else if (needFilled.length || posList.length) {{
    yfTier = 'yes';
    yfReason = 'it fills your needs (' + needFilled.map(function(c){{return DATA.catLabels[c]||c;}}).join(', ')
             + (posList.length ? (needFilled.length ? ', ' : '') + posList.join(', ') + ' slot' : '') + ')';
  }} else if (netMe >= -0.1) {{
    yfTier = 'maybe'; yfReason = 'fair value, but nothing you\'re short on';
  }} else {{
    yfTier = 'no'; yfReason = 'you\'d overpay without filling a need';
  }}

  // Sentiment marker: thumb icon + color, keyed to the tier. Same mapping both sides so the
  // color/icon language is learned once (green = this side likes it, red = this side balks).
  var ACC = {{ yes:['&#128077;','{GREEN}'], maybe:['&#129300;','{YELLOW}'], no:['&#128078;','{RED}'], na:['','{MUTED}'] }};
  function accLine(lbl, tier, reason) {{
    var m = ACC[tier] || ACC.na;
    var ic = m[0] ? m[0] + ' ' : '';
    return '<div class="readline"><span class="readlbl">' + lbl + '</span> '
         + '<span style="color:' + m[1] + '">' + ic + reason + '</span></div>';
  }}

  var gainChips = gained.length ? gained.map(function(c){{
        return catChip(c, needFilled.indexOf(c)>=0 ? 'need':''); }}).join('') : '<span class="empty">none</span>';
  var loseChips = lost.length ? lost.map(function(c){{
        var strong = (myMeta.surplus||[]).indexOf(c) >= 0;   // fine to lose from a strength
        return catChip(c, strong ? '' : 'lose'); }}).join('') : '<span class="empty">none</span>';
  var posChips = posList.length ? posList.map(function(p){{ return '<span class="chip pos">'+p+'</span>'; }}).join('') : '';

  // Three-row value block: Base (universal tval) + each side's team-modified value. A positive
  // Net in a row = that side comes out ahead by its own valuation — so a base-negative deal can
  // still be win-win (both team rows positive).
  function _sg(v) {{ return (v>=0?'+':'') + v.toFixed(2); }}
  function _ncls(v) {{ return v > 0.1 ? 'vgpos' : (v < -0.1 ? 'vgneg' : 'vgeven'); }}
  // Plain-English takeaway under the block: base clause = raw-value tilt (netVal, roster-blind),
  // tail = how it lands once each side re-prices by needs (netMe / netThem). Same +/-0.1 band as _ncls.
  function dealSummary(netVal, netMe, netThem, addressesNeed) {{
    var B = 0.1;
    var base = netVal >  0.5 ? 'You win the raw value'
             : netVal >  B   ? 'A small raw-value edge to you'
             : netVal < -0.5 ? 'You pay up on paper'
             : netVal < -B   ? 'A slight overpay on paper'
             :                 'Even on paper';
    var mine = netMe > B, theirs = netThem > B;
    var tail;
    if (mine && theirs)       tail = "but it fills both sides' needs &mdash; a win-win";
    else if (mine && !theirs) tail = 'and it fills your needs (a tougher sell for them)';
    else if (!mine && theirs) tail = 'but it mostly helps them &mdash; ask for more';
    else                      tail = addressesNeed ? 'even on needs for both sides'
                                                   : 'and neither side gains much';
    return base + ', ' + tail + '.';
  }}
  var myGive = sumEff(giveArr, myMeta), myGet = sumEff(getArr, myMeta);
  var thGive = sumEff(giveArr, partnerMeta), thGet = sumEff(getArr, partnerMeta);
  function _vrow(lbl, g, gt, net, hint) {{
    return '<div class="vgl" title="'+hint+'">'+lbl+'</div>'
         + '<div class="vgc">'+g.toFixed(2)+'</div><div class="vgc">'+gt.toFixed(2)+'</div>'
         + '<div class="vgc '+_ncls(net)+'">'+_sg(net)+'</div>';
  }}
  reads.innerHTML =
      '<div class="valgrid">'
        + '<div class="vgh"></div><div class="vgh">Give</div><div class="vgh">Get</div><div class="vgh">Net</div>'
        + _vrow('Base', giveVal, getVal, netVal, 'Universal value (tval) &mdash; ' + tilt)
        + _vrow('My value', myGive, myGet, netMe, 'Re-valued by your roster needs')
        + _vrow('Their value', thGive, thGet, netThem, 'Re-valued by ' + (partnerMeta.name||'their') + ' needs (Net = give &minus; get, their surplus)')
      + '</div>'
    + ((lKeys.length || rKeys.length)
         ? '<div class="dealsum">' + dealSummary(netVal, netMe, netThem, addressesNeed) + '</div>'
         : '')
    + '<div class="readline"><span class="readlbl">You gain:</span> ' + gainChips + (posChips?' &nbsp; '+posChips:'') + '</div>'
    + '<div class="readline"><span class="readlbl">You lose:</span> ' + loseChips + '</div>'
    + accLine('Would they do it?', pfTier, pfReason)
    + accLine('Would you do it?', yfTier, yfReason);
}}

function clearAll() {{
  picked = {{ L:{{}}, R:{{}} }};
  renderRoster('L'); renderRoster('R'); recompute();
}}

// ── Partner Fit board ─────────────────────────────────────────────────────────
// Renders DATA.partnerFit[leftTeam] — one engine-graded deal per rival, tiered by
// how landable it is. "Build this" drops the deal into the builder below.
var FIT_TIER = {{ BEST:['{GREEN}','BEST TARGET'], REACH:['{YELLOW}','WORTH A SHOT'],
                  SLIM:['{MUTED}','SLIM'], ONEWAY:['#ea580c','ONE-WAY'], NOFIT:['{RED}','NO DEAL'] }};

function escAttr(s) {{ return String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }}
function fitTag(t) {{ return '<span class="fbntag">' + t + '</span>'; }}

function fitCard(r) {{
  var t = FIT_TIER[r.tier] || FIT_TIER.SLIM, col = t[0], lbl = t[1];
  var meta = DATA.teamsMeta[r.team] || {{ name:r.team }};
  var head = '<div class="fbchead"><span class="fbvchip" style="color:' + col + ';border-color:' + col + '">'
           + lbl + '</span><span class="fbteam">' + meta.name + '</span>';
  if (r.tier === 'BEST' || r.tier === 'REACH') {{
    var giveNames = (r.give || []).join(',');
    var getNames  = (r.get || []).map(function(g) {{ return g.name; }}).join(',');
    head += '<span class="fbbuild" data-partner="' + escAttr(r.team) + '" data-give="' + escAttr(giveNames)
          + '" data-get="' + escAttr(getNames) + '" onclick="loadDeal(this)">Build this &#9654;</span></div>';
    var getParts = (r.get || []).map(function(g) {{
      return '<b>' + g.name + '</b> ' + (g.tags || []).slice(0,2).map(fitTag).join(' ');
    }}).join('  ');
    var deal = '<div class="fbdeal"><span class="fbget">Get</span> ' + getParts
             + '<div class="fbgive"><span class="fbgv">for</span> ' + (r.give || []).join(' + ') + '</div></div>';
    var vcol = r.tier === 'BEST' ? '{GREEN}' : '{YELLOW}';
    var verdict = '<div class="fbverdict" style="color:' + vcol + '">' + r.verdict + '</div>';
    var why = '';
    if ((r.whyOffer && r.whyOffer.length) || (r.whyGet && r.whyGet.length)) {{
      var off = (r.whyOffer && r.whyOffer.length) ? r.whyOffer.join('/') : 'what they lack';
      var got = (r.whyGet && r.whyGet.length) ? r.whyGet.join('/') : 'a hole of yours';
      why = '<div class="fbwhy"><span class="wl">Why it works:</span> you\'re deep in ' + off
          + ' (their holes); they can spare the ' + got + ' you need.</div>';
    }}
    return '<div class="fbcard">' + head + deal + verdict + why + '</div>';
  }}
  head += '</div>';
  return '<div class="fbcard dim">' + head
       + '<div class="fbwhy" style="margin-top:0;color:{MUTED}">' + (r.why || '') + '</div></div>';
}}

function renderFitBoard() {{
  var myTk = document.getElementById('selL').value;
  var meta = DATA.teamsMeta[myTk] || {{ needs:[], need_pos:{{}}, name:myTk }};
  var hEl = document.getElementById('fbholes');
  if (hEl) {{
    var holes = (meta.needs || []).map(function(c) {{ return DATA.catLabels[c] || c; }});
    var pos = Object.keys(meta.need_pos || {{}});
    var bits = [];
    if (holes.length) bits.push('holes <b class="w">' + holes.join(', ') + '</b>');
    if (pos.length)   bits.push('thin at <b class="w">' + pos.join(', ') + '</b>');
    hEl.innerHTML = (meta.name || myTk) + (bits.length ? ' &middot; ' + bits.join(' &middot; ') : '');
  }}
  var recs = (DATA.partnerFit || {{}})[myTk] || [];
  var list = document.getElementById('fblist');
  if (list) list.innerHTML = recs.map(fitCard).join('') || '<div class="empty">No rivals.</div>';
}}

function loadDeal(el) {{
  var partnerTk = el.getAttribute('data-partner');
  var giveNames = el.getAttribute('data-give');
  var getNames  = el.getAttribute('data-get');
  clearAll();
  var selR = document.getElementById('selR');
  if (partnerTk && DATA.teamKeys.indexOf(partnerTk) >= 0) selR.value = partnerTk;
  renderRoster('L'); renderRoster('R');
  function pickByName(side, names) {{
    if (!names) return;
    var tk = document.getElementById(side === 'L' ? 'selL' : 'selR').value;
    var pool = flatPool(tk);
    names.split(',').forEach(function(nm) {{
      nm = nm.trim().toLowerCase(); if (!nm) return;
      for (var i=0;i<pool.length;i++)
        if ((pool[i].name || '').toLowerCase() === nm) {{ toggle(side, pool[i].id); break; }}
    }});
  }}
  pickByName('L', giveNames);
  pickByName('R', getNames);
  var cols = document.getElementById('cols');
  if (cols && cols.scrollIntoView) cols.scrollIntoView({{ behavior:'smooth', block:'start' }});
}}

function initSide(side) {{
  var sel = document.getElementById(side === 'L' ? 'selL' : 'selR');
  sel.addEventListener('change', function() {{
    picked[side] = {{}};                 // reset that side's picks on team change
    renderRoster('L'); renderRoster('R'); recompute();   // R targets depend on L's needs
    if (side === 'L') renderFitBoard();  // the board is judged from the LEFT (my) team
  }});
}}

// Optional preload of a specific deal, from either the URL hash
// (#partner=<team key>&give=<names>&get=<names>) OR a baked-in DATA.preload object
// ({{partner, give, get}}). The baked-in form is used when the OS strips the URL
// fragment on file:// launch (Windows shell). Silent no-op when neither is present.
function preloadFromHash() {{
  var params = {{}};
  var h = (location.hash || '').replace(/^#/, '');
  if (h) {{
    h.split('&').forEach(function(kv){{ var p = kv.split('='); params[p[0]] = decodeURIComponent((p[1]||'').replace(/\+/g,' ')); }});
  }} else if (DATA.preload && (DATA.preload.partner || DATA.preload.give || DATA.preload.get)) {{
    params = DATA.preload;
  }} else {{
    return false;
  }}
  if (params.partner && DATA.teamKeys.indexOf(params.partner) >= 0)
    document.getElementById('selR').value = params.partner;
  renderRoster('L'); renderRoster('R');
  function pickByName(side, names) {{
    if (!names) return;
    var tk = document.getElementById(side === 'L' ? 'selL' : 'selR').value;
    var pool = flatPool(tk);
    names.split(',').forEach(function(nm){{
      nm = nm.trim().toLowerCase(); if (!nm) return;
      for (var i=0;i<pool.length;i++)
        if ((pool[i].name||'').toLowerCase() === nm) {{ toggle(side, pool[i].id); break; }}
    }});
  }}
  pickByName('L', params.give);
  pickByName('R', params.get);
  return true;
}}

(function() {{
  var keys = DATA.teamKeys;
  var rDefault = keys.find(function(k){{ return k !== DATA.myTeam; }}) || keys[0];
  teamOptions(document.getElementById('selL'), DATA.myTeam);
  teamOptions(document.getElementById('selR'), rDefault);
  initSide('L'); initSide('R');
  if (!preloadFromHash()) {{ renderRoster('L'); renderRoster('R'); recompute(); }}
  renderFitBoard();
}})();
"""


def main():
    ap = argparse.ArgumentParser(description="Interactive Trade Lab (browser-only)")
    ap.add_argument("--refresh", action="store_true", help="Refresh snapshot data first (~60s)")
    ap.add_argument("--team", default=None, help="Default the LEFT (my) side to another team")
    ap.add_argument("--partner", default=None, help="Preload: RIGHT-side team key (e.g. 'The BIG Dumpers')")
    ap.add_argument("--give", default=None, help="Preload: comma-separated player names to put on YOUR side")
    ap.add_argument("--get", default=None, help="Preload: comma-separated player names to acquire from the partner")
    ap.add_argument("--out", default=None,
                    help="Write the HTML to this exact path (for publishing, e.g. public/index.html) "
                         "and a sibling build.json freshness marker, instead of previews/tradelab_{slug}.html")
    ap.add_argument("--refresh-url", default=None,
                    help="Cloudflare Worker endpoint for the in-page Refresh button (or env POCKET_REFRESH_URL). "
                         "When unset, the Refresh button is not rendered.")
    args = ap.parse_args()

    if args.refresh:
        import fetch_data
        fetch_data.main()

    with open(SNAPSHOT, encoding="utf-8") as f:
        snap = json.load(f)

    my_team = args.team or snap.get("my_team", MY_TEAM)
    data = build_data(snap, my_team)
    if args.partner or args.give or args.get:
        data["preload"] = {"partner": " ".join((args.partner or "").split()),
                           "give": args.give or "", "get": args.get or ""}

    import os
    refresh_url = args.refresh_url or os.environ.get("POCKET_REFRESH_URL") or ""
    if refresh_url:
        data["refreshUrl"] = refresh_url
    # build_at stamps the render; build.json below carries the same value so the pocket page
    # can poll for a completed rebuild and auto-reload (see the Refresh JS).
    built_at = datetime.now(timezone.utc).isoformat()
    data["builtAt"] = built_at

    html = build_html(data)

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        (out.parent / "build.json").write_text(
            json.dumps({"built_at": built_at, "refreshed_at": data.get("refreshed", "")}),
            encoding="utf-8")
        print(f"Wrote {out} and {out.parent / 'build.json'}")
    else:
        PREVIEWS.mkdir(exist_ok=True)
        slug = _disp(my_team).replace(" ", "_")
        out = PREVIEWS / f"tradelab_{slug}.html"
        out.write_text(html, encoding="utf-8")
        print(f"Wrote {out}")


if __name__ == "__main__":
    main()
