#!/usr/bin/env python3
"""Single-viewport, zero-scroll dashboard — a companion "control panel" to the daily
digest (send_digest.py). Where the digest is a long scrolling email covering every
topic in depth, this is one glance-able pane that fits a 1440x900 laptop screen with
NO scrolling, giving even, high-level coverage of every topic the digest covers.

It IMPORTS send_digest and reuses its scoring / projection / aggregation functions
verbatim, so every number here is identical to the digest's (honors the "same score
in every section" rule in CLAUDE.md). It does not modify send_digest and never emails
— it writes previews/dashboard_{team_slug}.html for laptop viewing.

    python dashboard.py                     # use existing snapshot (fast), write preview
    python dashboard.py --refresh           # refresh data first (~60s), then write
    python dashboard.py --team "Houck Tuah" # another team's dashboard (needs all_matchups)
"""

import argparse
import json
import math
import re
from datetime import datetime
from pathlib import Path

import send_digest as sd
from send_digest import (
    SURFACE, SURFACE2, BORDER, TEXT, MUTED, ACCENT, GREEN, RED, YELLOW, ORANGE, CYAN,
    YEAR, MY_TEAM, _n, _is_sp, _fmt_ip, _starts_this_week,
    _project, _cat_win_prob, _CAT_DEC, _CAT_LABELS_MAP, _LOWER_BETTER,
    _CLOSE_THRESH, _TOSSUP_LO, _TOSSUP_HI,
)

SNAPSHOT = Path(__file__).parent / "data" / "snapshot.json"
PREVIEWS = Path(__file__).parent / "previews"


# ══════════════════════════════════════════════════════════════════════════════
# CONTEXT — mirror the derived-value prelude of build_email (send_digest.py ~3572-3777)
# ══════════════════════════════════════════════════════════════════════════════

def build_context(snap, my_team):
    """Reconstruct all the derived structures the digest computes, by calling the
    SAME send_digest functions in the same order. Returns a dict `ctx` of everything
    the tiles render."""
    pitchers   = snap.get("pitchers", [])
    hitters    = snap.get("hitters", [])
    roto       = snap.get("roto", [])
    standings  = snap.get("standings", [])
    override   = (" ".join(my_team.split()) != " ".join(snap.get("my_team", "").split()))
    all_matchups = snap.get("all_matchups", {})
    matchup    = all_matchups.get(" ".join(my_team.split())) or (snap.get("current_matchup", {}) if not override else {})

    recent_hitting  = snap.get("recent_hitting",  [])
    recent_pitching = snap.get("recent_pitching", [])
    weekly_results  = snap.get("weekly_results",  {})

    # Scoring calibration + percentile pools + recent-form indexes + claimed set —
    # the SAME shared send_digest helpers build_email uses, so every number matches.
    hit_pctile, pit_pctile = sd.prepare_scoring(pitchers, hitters)
    idx = sd.build_recent_indexes(pitchers, hitters, recent_pitching, recent_hitting)
    rec_p, rec_h, p15 = idx["rec_p"], idx["rec_h"], idx["p15"]
    best_recent_p, best_recent_h = idx["best_recent_p"], idx["best_recent_h"]

    today_str = datetime.now().strftime("%Y-%m-%d")
    claimed = sd.claimed_today(snap.get("transactions", []), today_str)

    fa_sp  = sd.fa_starters(pitchers, claimed, idx_recent=best_recent_p)
    fa_rp  = sd.fa_relievers(pitchers, claimed)
    fa_hit = sd.fa_hitters(hitters, claimed, idx_recent=best_recent_h)
    luck   = sd.luck_standings(roto, standings)
    team_logos = {" ".join(s["team_name"].split()): s.get("logo_url", "") for s in standings}
    cats, n = sd.category_ranks(roto, my_team)
    current_week_num = matchup.get("week") or max((int(r.get("Week", 0)) for r in roto), default=0)
    weekly_avgs = sd.compute_weekly_avgs(roto, current_week_num)
    weekly_std  = sd.compute_weekly_std(roto, current_week_num)

    # Matchup window + pitcher K/QS/W projections — shared send_digest helpers.
    win = sd.parse_matchup_window(snap)
    matchup_period_days = win["matchup_period_days"]
    week_end_str        = win["week_end_str"]
    days_elapsed        = win["days_elapsed"]
    matchup_game_days   = win["matchup_game_days"]
    game_days_elapsed   = win["game_days_elapsed"]
    is_sunday           = win["is_sunday"]

    pit_proj = sd.compute_pit_proj(pitchers, my_team, matchup.get("opp_team", "") if matchup else "",
                                   today_str, week_end_str)
    pit_proj.update(sd.compute_hit_proj(weekly_avgs, my_team,
                                        matchup.get("opp_team", "") if matchup else "",
                                        snap.get("team_hit_sched_frac")))

    classification = sd.classify_categories(
        matchup, weekly_avgs=weekly_avgs, days_elapsed=days_elapsed, remaining_proj=pit_proj,
        matchup_days=matchup_period_days,
        game_days_elapsed=game_days_elapsed, matchup_game_days=matchup_game_days,
    )

    pos_data = sd.positional_breakdown(pitchers, hitters, my_team, best_recent_p, best_recent_h)
    starts   = sd.my_upcoming_starts(pitchers, my_team)
    alerts   = sd.roster_alerts(pitchers, hitters, my_team)
    lineup_eff_current = snap.get("lineup_efficiency_current", {}) if not override else {}
    roster_sugg = sd._roster_suggestion(
        matchup, pitchers, hitters, fa_sp, fa_rp, fa_hit, my_team, best_recent_p, best_recent_h,
        all_matchups, week_end_str, classification=classification,
        league_total_roster_max=snap.get("league_total_roster_max", 28),
        pos_data=pos_data, lineup_eff=lineup_eff_current, pill_fn=_mini_badge,
    )
    emerging, fading = sd.save_role_watch(pitchers, my_team, claimed)

    # hit_pctile / pit_pctile / positional scarcity already set by sd.prepare_scoring above.
    # Trade Radar tile is prescriptive ("go send this") — only ever surface deals a rival
    # would realistically accept (see find_trades' realistic_only docstring).
    trades = sd.find_trades_combined(pitchers, hitters, roto, my_team, best_recent_p, best_recent_h,
                                     pos_data, hit_pctile, pit_pctile, realistic_only=True)
    # Real pending trade OFFERS (my team only) — graded once, same as the digest. A concrete
    # offer to decide on outranks the speculative radar ideas, so the tile leads with it.
    pending_incoming = [g for g in sd._grade_pending_trades(
        snap.get("pending_trades") or [], pitchers, hitters, roto, my_team,
        best_recent_p, best_recent_h, pos_data, hit_pctile, pit_pctile, today_str=today_str)
        if g["incoming"]] if not override else []

    # Weekly finishes + sparkline + roster hot/cold KPI — shared send_digest helpers.
    my_key = " ".join(my_team.split())
    wk_ranks, wk_pts, roto_week_results = sd.compute_week_finishes(roto, my_team, current_week_num)
    sparkline, peak_label = sd.make_sparkline(roto, my_team, current_week_num, weekly_results=roto_week_results)
    n_hot, n_cold = sd.roster_hot_cold_counts(pitchers, hitters, my_team, rec_h, rec_p, p15)

    my_row = next((r for r in luck if " ".join((r.get("team") or "").split()) == my_key), {})

    return dict(
        my_team=my_team, override=override, pitchers=pitchers, hitters=hitters, roto=roto,
        matchup=matchup, current_week_num=current_week_num, team_logos=team_logos,
        best_recent_p=best_recent_p, best_recent_h=best_recent_h, p15=p15, rec_p=rec_p, rec_h=rec_h,
        fa_sp=fa_sp, fa_rp=fa_rp, fa_hit=fa_hit, luck=luck, my_row=my_row, cats=cats, n_teams=n,
        weekly_avgs=weekly_avgs, weekly_std=weekly_std, classification=classification, pit_proj=pit_proj,
        pos_data=pos_data, starts=starts, alerts=alerts, hit_pctile=hit_pctile,
        trades=trades, pending_incoming=pending_incoming,
        lineup_eff_current=lineup_eff_current, roster_sugg=roster_sugg, emerging=emerging, fading=fading,
        sparkline=sparkline, peak_label=peak_label, roto_week_results=roto_week_results,
        weekly_results=weekly_results, wk_ranks=wk_ranks, wk_pts=wk_pts,
        days_elapsed=days_elapsed, game_days_elapsed=game_days_elapsed, matchup_game_days=matchup_game_days,
        matchup_period_days=matchup_period_days, week_end_str=week_end_str, is_sunday=is_sunday,
        n_hot=n_hot, n_cold=n_cold, refreshed_at=snap.get("refreshed_at", ""),
        todays_games=snap.get("todays_games", []),
    )


# ══════════════════════════════════════════════════════════════════════════════
# COMPACT RENDER HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _fv(v, dec=0):
    """Format a category value; strip the leading zero for OPS/rate sub-1 values."""
    s = f"{v:.{dec}f}"
    return s[1:] if (dec >= 2 and 0 <= v < 1) else s


def _reg_chip8(r):
    """Tiny 8px pitcher buy-low ($) / sell-high (▼) chip, matching the QS/5K+/⚠ chips in
    My Pitching. Same $/▼ glyph + green/red as everywhere else (sd.pitcher_regression_*)."""
    flag = sd.pitcher_regression_flag(r)
    if not flag:
        return ""
    era, xera = _n(r.get("ERA")), _n(r.get("xERA"))
    if flag == "buy":
        col, glyph, tip = GREEN, "$", f"ERA {era:.2f} vs xERA {xera:.2f} &mdash; buy-low (unlucky, positive regression likely)"
    else:
        col, glyph, tip = RED, "&#9660;", f"ERA {era:.2f} vs xERA {xera:.2f} &mdash; sell-high (lucky, regression risk)"
    rr, gg, bb = int(col[1:3], 16), int(col[3:5], 16), int(col[5:7], 16)
    return (f' <span title="{tip}" style="font-size:8px;font-weight:700;color:{col};'
            f'background:rgba({rr},{gg},{bb},0.12);border:1px solid rgba({rr},{gg},{bb},0.35);'
            f'border-radius:3px;padding:0 3px;vertical-align:middle;">{glyph}</span>')


def _tile(title, body, flex=1.0, accent=ACCENT, sub=""):
    sub_html = f'<span style="color:{MUTED};font-weight:400;text-transform:none;letter-spacing:0;font-size:10px;margin-left:6px;">{sub}</span>' if sub else ""
    return (
        f'<div class="tile" style="flex:{flex} 1 0;min-height:0;background:{SURFACE};border:1px solid {BORDER};'
        f'border-top:2px solid {accent};border-radius:6px;padding:6px 10px;display:flex;flex-direction:column;overflow:hidden;">'
        f'<div style="color:{TEXT};font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:.6px;'
        f'margin-bottom:4px;flex:0 0 auto;">{title}{sub_html}</div>'
        f'<div class="tile-body" style="flex:1 1 0;min-height:0;overflow:hidden;font-size:12.5px;color:{TEXT};line-height:1.4;">{body}</div>'
        f'</div>'
    )


def _mini_badge(score):
    s = int(score or 0)
    if s >= 72:   bg = "#16a34a"
    elif s >= 52: bg = "#2563eb"
    elif s >= 32: bg = "#d97706"
    else:         bg = "#dc2626"
    return f'<span style="background:{bg};color:#fff;padding:1px 6px;border-radius:9px;font-size:10px;font-weight:800;">{s}</span>'


def _score_tip(p):
    """Short plain-text score explanation for a hover tooltip (title=), role-aware off
    the trade player's _tptype. Reuses send_digest's narrative helpers so the prose
    matches the digest's tap-to-expand breakdowns. Returns '' on any failure."""
    try:
        if p.get("_tptype") == "hit":
            comps, _ = sd.hitter_score(p, _parts=True)
            role, season, clauses = "Hitter", sd.hitter_score(p), sd._hit_clauses(p, comps)
        elif _is_sp(p):
            comps, _ = sd.pitcher_score(p, _parts=True)
            role, season, clauses = "SP", sd.pitcher_score(p), sd._sp_clauses(p, comps)
        else:
            comps, _ = sd.rp_score(p, _parts=True)
            role, season, clauses = "RP", sd.rp_score(p), sd._rp_clauses(p, comps)
        if not comps:
            return ""
        txt = f"{role} score {season}. {sd._score_narrative(clauses)}"
        return txt.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
    except Exception:
        return ""


def _score_pill_tip(p):
    """_mini_badge for a trade player's _tscore, wrapped in a hover tooltip."""
    pill = _mini_badge(int(round(p.get("_tscore", 0))))
    tip = _score_tip(p)
    if not tip:
        return pill
    return f'<span title="{tip}" style="cursor:help;">{pill}</span>'


def _pos(r):
    return str(r.get("Position", "")).split(",")[0].strip()


def _stretch_spark(svg, height=58):
    """Make make_sparkline's fixed-width SVG fill the tile width (it's authored at
    ~14px/week so it never reaches the tile edge). Add a viewBox + width:100% and
    stretch to the given pixel height with preserveAspectRatio=none so it lines up
    with the full-width Weekly Finishes pills below it."""
    m = re.search(r'<svg width="(\d+)" height="(\d+)"', svg)
    if not m:
        return svg
    w, h = m.group(1), m.group(2)
    return svg.replace(
        f'<svg width="{w}" height="{h}" style="display:inline-block;vertical-align:middle;"',
        f'<svg viewBox="0 0 {w} {h}" width="100%" height="{height}" preserveAspectRatio="none" '
        f'style="display:block;width:100%;height:{height}px;"',
        1,
    )


# ── KPI top bar ────────────────────────────────────────────────────────────────

def render_topbar(ctx):
    my_team = ctx["my_team"]; my_row = ctx["my_row"]; matchup = ctx["matchup"]
    logo = sd.fantasy_logo(ctx["team_logos"].get(" ".join(my_team.split()), ""), size=30, team_name=my_team)

    # Freshness
    try:
        _rdt = datetime.fromisoformat(ctx["refreshed_at"])
        if _rdt.tzinfo is not None and sd._ET is not None:
            _rdt = _rdt.astimezone(sd._ET)
        fresh_date = _rdt.strftime("%Y-%m-%d")
    except Exception:
        fresh_date = ctx["refreshed_at"][:10]
    clock = sd._fmt_refresh_time(ctx["refreshed_at"])
    fresh_today = fresh_date == datetime.now().strftime("%Y-%m-%d")
    fresh = (f'<span style="color:{GREEN if fresh_today else YELLOW};font-size:9px;">'
             f'{"&#10003;" if fresh_today else "&#9888;"} {clock or fresh_date}</span>')

    # Record / matchup / proj
    w, l, t = my_row.get('wins', 0), my_row.get('losses', 0), my_row.get('ties', 0)
    rec = f"{w}-{l}-{t}"
    cw, cl, ct = (matchup.get("wins", 0), matchup.get("losses", 0), matchup.get("ties", 0)) if matchup else (0, 0, 0)
    cwl = f"{cw}-{cl}-{ct}"
    cwl_c = GREEN if cw > cl else (RED if cl > cw else TEXT)
    cls = ctx["classification"]
    pw = sum(1 for (res, _) in cls.values() if res == "W")
    pl = sum(1 for (res, _) in cls.values() if res == "L")
    pt = sum(1 for (res, _) in cls.values() if res == "T")
    proj = f"{pw}-{pl}-{pt}" if cls else "—"
    proj_c = GREEN if pw > pl else (RED if pl > pw else TEXT)

    starts_wk = sum(1 for s in ctx["starts"] if s.get("PSP_Date", "") <= ctx["week_end_str"])
    hc = f'<span style="color:{GREEN};">&#128293;{ctx["n_hot"]}</span> <span style="color:{ACCENT};">&#10052;{ctx["n_cold"]}</span>'
    luck_val = my_row.get("luck", 0)
    luck_s = f"+{luck_val}" if luck_val > 0 else str(luck_val)
    luck_c = GREEN if luck_val > 2 else (RED if luck_val < -2 else MUTED)

    def chip(label, value, vcolor=TEXT):
        return (
            f'<div class="chip" style="text-align:center;padding:0 12px;border-left:1px solid {BORDER};">'
            f'<div style="color:{MUTED};font-size:8px;text-transform:uppercase;letter-spacing:.6px;">{label}</div>'
            f'<div style="color:{vcolor};font-size:16px;font-weight:800;margin-top:1px;white-space:nowrap;">{value}</div>'
            f'</div>'
        )

    chips = "".join([
        chip("Record", rec),
        chip("Matchup", cwl, cwl_c),
        chip("Proj", proj, proj_c),
        chip("Roster", hc),
        chip("Starts", starts_wk),
        chip("Standing", f'#{my_row.get("standing","—")}'),
        chip("Roto", f'#{my_row.get("roto_rank","—")}'),
        chip("Luck", luck_s, luck_c),
    ])

    opp = matchup.get("opp_team", "") if matchup else ""
    return (
        f'<div class="topbar" style="flex:0 0 auto;background:linear-gradient(135deg,#0b1a38,#0f172a);border:1px solid {BORDER};'
        f'border-radius:6px;padding:8px 12px;display:flex;align-items:center;justify-content:space-between;gap:10px;">'
        f'<div style="display:flex;align-items:center;gap:8px;min-width:0;">{logo}'
        f'<div style="min-width:0;"><div style="color:{TEXT};font-size:17px;font-weight:900;letter-spacing:-.5px;'
        f'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">{my_team}</div>'
        f'<div style="color:#4b7bc4;font-size:9px;text-transform:uppercase;letter-spacing:.6px;">'
        f'Command Dashboard{" &middot; vs " + opp if opp else ""} &middot; {fresh}</div></div></div>'
        f'<div class="topbar-chips" style="display:flex;align-items:center;">{chips}</div>'
        f'</div>'
    )


# ── Column 1: Category Pulse + Opponent ─────────────────────────────────────────

def _pulse_cell(c, ctx, my_avgs, opp_avgs, my_std, opp_std, elapsed_frac, remaining_frac, has_proj):
    cat = c["cat"]; my_v = c["my_val"]; opp_v = c["opp_val"]; res = c["result"]
    dec = _CAT_DEC.get(cat, 0); label = _CAT_LABELS_MAP.get(cat, cat)
    if res == "W":   bar_c = GREEN
    elif res == "L": bar_c = RED
    else:            bar_c = TEXT

    proj_res = None; win_pct = None; pm = po = None
    rp = ctx["pit_proj"].get(cat)
    if rp is not None:
        pm, po = my_v + rp["my"], opp_v + rp["opp"]
    elif has_proj and cat in my_avgs and cat in opp_avgs:
        pm = _project(my_v, my_avgs[cat], elapsed_frac, cat)
        po = _project(opp_v, opp_avgs[cat], elapsed_frac, cat)
    if pm is not None:
        pm_r, po_r = round(pm, dec), round(po, dec)
        lower = cat in _LOWER_BETTER
        if lower:
            proj_res = "W" if pm_r < po_r else ("T" if pm_r == po_r else "L")
        else:
            proj_res = "W" if pm_r > po_r else ("T" if pm_r == po_r else "L")
        sm, so = my_std.get(cat), opp_std.get(cat)
        sigma = math.sqrt(sm * sm + so * so) if (sm is not None and so is not None) else (_CLOSE_THRESH.get(cat, 1) or 1)
        p_win, _ = _cat_win_prob(pm, po, cat, sigma, remaining_frac)
        win_pct = round(p_win * 100)

    is_close = win_pct is not None and (proj_res == "T" or _TOSSUP_LO <= win_pct <= _TOSSUP_HI)
    if is_close:
        corner = f'<span style="color:{YELLOW};font-size:10px;">&#9889;</span>'
    elif win_pct is not None:
        wp_c = GREEN if proj_res == "W" else (RED if proj_res == "L" else TEXT)
        corner = f'<span style="color:{wp_c};font-size:9px;">{win_pct}%</span>'
    else:
        corner = ""
    if proj_res == "W":   mark = f'<span style="color:{GREEN};font-size:9px;">&#9650;</span>'
    elif proj_res == "L": mark = f'<span style="color:{RED};font-size:9px;">&#9660;</span>'
    elif proj_res == "T": mark = f'<span style="color:{TEXT};font-size:9px;">&#9670;</span>'
    else:                 mark = ""

    total = my_v + opp_v
    if total > 0:
        pct = (opp_v / total * 100) if cat in _LOWER_BETTER else (my_v / total * 100)
    else:
        pct = 50
    pct = max(6, min(94, pct))

    proj_line = ""
    if pm is not None:
        proj_line = (f'<div style="color:{MUTED};font-size:10px;">proj '
                     f'<span style="color:{TEXT};">{_fv(pm,dec)}</span> / {_fv(po,dec)}</div>')

    html = (
        f'<div style="position:relative;background:{SURFACE2};border:1px solid {BORDER};border-left:3px solid {bar_c};'
        f'border-radius:4px;padding:6px 8px;display:flex;flex-direction:column;justify-content:space-between;min-height:0;">'
        f'<div style="position:absolute;top:5px;right:6px;display:flex;gap:3px;align-items:center;line-height:1;">{corner}{mark}</div>'
        f'<div style="color:{MUTED};font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;">{label}</div>'
        f'<div style="margin:3px 0;line-height:1.1;"><span style="color:{bar_c};font-size:22px;font-weight:800;">{_fv(my_v,dec)}</span>'
        f'<span style="color:{MUTED};font-size:13px;"> / {_fv(opp_v,dec)}</span></div>'
        f'{proj_line}'
        f'<div style="height:3px;background:{BORDER};border-radius:2px;margin-top:5px;">'
        f'<div style="width:{pct:.0f}%;height:100%;background:{bar_c};border-radius:2px;"></div></div>'
        f'</div>'
    )
    return html, is_close


def render_tv_games(ctx):
    """Compact 'Today's Games' tile — your favorite team (&#9733;, pinned first) plus the
    next highest-overlap game. Mirrors the digest's Today's MLB Games selection + badges
    (sd._rank_todays_games with pin_favorite, role-aware chips) but trimmed to 2 games so
    it fits the zero-scroll grid. Returns '' (no tile) when there are no games today."""
    try:
        games_raw = ctx.get("todays_games") or []
        if not games_raw:
            return ""
        matchup = ctx.get("matchup") or {}
        my_key  = " ".join((ctx["my_team"] or "").split())
        opp_key = " ".join((matchup.get("opp_team") or "").split())
        ranked = sd._rank_todays_games(games_raw, my_key, opp_key, pin_favorite=True)
        if not ranked:
            return ""
        # Badge row lookups (YEAR preferred, 30->15->7 fallback) — same as build_email.
        hit_rows, pit_rows, rec_era = {}, {}, {}
        for ds in (7, 15, 30, YEAR):
            for r in ctx["hitters"]:
                if int(_n(r.get("Dataset")) or 0) == ds and r.get("PlayerName"):
                    hit_rows[sd._badge_name_key(r["PlayerName"])] = r
            for r in ctx["pitchers"]:
                if int(_n(r.get("Dataset")) or 0) == ds and r.get("PlayerName"):
                    pit_rows[sd._badge_name_key(r["PlayerName"])] = r
        for nm, r in {**ctx["rec_p"], **ctx["p15"]}.items():
            if r.get("ERA") is not None:
                rec_era[sd._badge_name_key(nm)] = r.get("ERA")
        hit_pctile = ctx.get("hit_pctile")

        def _badges(p):
            k = sd._badge_name_key(p.get("name", ""))
            if p.get("is_p"):
                row = pit_rows.get(k)
                return (sd.blowup_badge(row, rec_era.get(k)) + sd.pitcher_regression_badge(row)) if row else ""
            row = hit_rows.get(k)
            return sd.hitter_badges(row, hit_pctile, cap=2) if row else ""

        def _names(players, cap=3):
            if not players:
                return f'<span style="color:{MUTED};">0</span>'
            parts = []
            for p in players[:cap]:
                mark = f'<span style="color:{CYAN};font-weight:700;">&#9918;</span>' if p.get("is_sp") else ""
                parts.append(f'{p.get("name","")}{mark}{_badges(p)}')
            extra = f' +{len(players)-cap}' if len(players) > cap else ""
            return ", ".join(parts) + extra

        blocks = []
        for item in ranked[:2]:
            g = item["g"]
            aw = sd._FULLNAME_TO_ABBREV.get(g.get("away_name", ""), g.get("away_name", ""))
            hm = sd._FULLNAME_TO_ABBREV.get(g.get("home_name", ""), g.get("home_name", ""))
            star = f'<span style="color:{YELLOW};font-weight:700;">&#9733; </span>' if item.get("fav") else ""
            gt  = sd._fmt_game_time_et(g.get("game_time_utc", ""))
            net = (g.get("national_tv") or [None])[0] or g.get("away_tv") or g.get("home_tv") or ""
            net = net.split(" Presented by")[0].strip()   # drop sponsor tail — compact tile
            meta = gt + (f' &middot; {net}' if net else "")
            my_sp  = {sd._ascii_lower(p["name"]) for p in item["mine"] if p.get("is_sp")}
            opp_sp = {sd._ascii_lower(p["name"]) for p in item["opp"] if p.get("is_sp")}
            def _pn(nm):
                if not nm:
                    return f'<span style="color:{MUTED};">TBD</span>'
                a = sd._ascii_lower(nm); c = ACCENT if a in my_sp else RED if a in opp_sp else MUTED
                return f'<span style="color:{c};">{nm.split()[-1]}</span>'
            blocks.append(
                f'<div style="margin-bottom:5px;font-size:11px;line-height:1.4;">'
                f'<div>{star}{sd.team_logo(aw,13)}<b style="color:{TEXT};">{aw}</b>'
                f'<span style="color:{MUTED};"> @ </span>{sd.team_logo(hm,13)}<b style="color:{TEXT};">{hm}</b>'
                f'<span style="color:{MUTED};font-size:10px;"> &middot; {meta}</span></div>'
                f'<div style="color:{MUTED};font-size:10px;">&#9918; {_pn(g.get("away_prob"))}'
                f'<span style="color:{MUTED};"> v </span>{_pn(g.get("home_prob"))}</div>'
                f'<div><span style="color:{ACCENT};font-weight:700;">You:</span> '
                f'<span style="color:{TEXT};">{_names(item["mine"])}</span></div>'
                f'<div><span style="color:{MUTED};font-weight:700;">Opp:</span> '
                f'<span style="color:{MUTED};">{_names(item["opp"])}</span></div>'
                f'</div>'
            )
        return _tile("Today's Games", "".join(blocks), flex=1.05,
                     sub="&#9733; your team + top overlap")
    except Exception:
        return ""


def render_category_pulse(ctx):
    matchup = ctx["matchup"]
    if not matchup or not matchup.get("categories"):
        return _tile("Category Pulse", f'<div style="color:{MUTED};">No live matchup yet.</div>', flex=1.7)
    mk = " ".join(matchup.get("my_team", "").split()); ok = " ".join(matchup.get("opp_team", "").split())
    my_avgs = ctx["weekly_avgs"].get(mk, {}); opp_avgs = ctx["weekly_avgs"].get(ok, {})
    my_std = ctx["weekly_std"].get(mk, {}); opp_std = ctx["weekly_std"].get(ok, {})
    has_proj = bool(my_avgs and opp_avgs)
    if ctx["matchup_game_days"]:
        elapsed_frac = min(1.0, max(0.0, ctx["game_days_elapsed"] / ctx["matchup_game_days"]))
    else:
        elapsed_frac = min(1.0, max(0.0, ctx["days_elapsed"] / ctx["matchup_period_days"]))
    remaining_frac = 1.0 - elapsed_frac

    rendered = [
        _pulse_cell(c, ctx, my_avgs, opp_avgs, my_std, opp_std, elapsed_frac, remaining_frac, has_proj)
        for c in matchup["categories"]
    ]
    cells = "".join(h for h, _ in rendered)
    grid = (
        f'<div class="pulse-grid" style="display:grid;grid-template-columns:repeat(4,1fr);gap:4px;height:100%;'
        f'grid-auto-rows:1fr;">{cells}</div>'
    )
    cw = sum(1 for c in matchup["categories"] if c["result"] == "W")
    cl = sum(1 for c in matchup["categories"] if c["result"] == "L")
    ct = sum(1 for c in matchup["categories"] if c["result"] == "T")
    cls = ctx["classification"]
    pw = sum(1 for (r, _) in cls.values() if r == "W"); pl = sum(1 for (r, _) in cls.values() if r == "L"); pt = sum(1 for (r, _) in cls.values() if r == "T")
    # ⚡N close counts the cards actually showing the ⚡ toss-up glyph (win-% band /
    # projected tie) — the SAME source as the per-card flag, so the count and the
    # visible bolts always agree (matches the digest's close_flags summary). NOT the
    # margin-based classify_categories "tossup" tier, which disagrees with the cards.
    close = sum(1 for _, ic in rendered if ic)
    sub = (f'{cw}W&middot;{cl}L&middot;{ct}T &rarr; proj {pw}-{pl}-{pt}'
           + (f' &middot; &#9889;{close}' if close else ''))
    return _tile(f"Category Pulse", grid, flex=1.6, sub=sub)


# ── Column 2: Pitching, Hitting, Holes ──────────────────────────────────────────

def render_pitching(ctx):
    rows = []
    for r in ctx["starts"][:6]:
        d = r.get("PSP_Date", "")
        try:
            dlabel = datetime.strptime(d, "%Y-%m-%d").strftime("%a %-m/%-d")
        except Exception:
            try:
                dlabel = datetime.strptime(d, "%Y-%m-%d").strftime("%a %m/%d")
            except Exception:
                dlabel = d
        vals = sd._proj_line_vals(r)
        line = f'{_fmt_ip(vals[0])} IP&middot;{vals[1]}ER&middot;{vals[2]}K' if vals else "—"
        qs = sd.qs_probability(r)
        hva = str(r.get("PSP_HomeVAway") or "")
        _n_starts = _starts_this_week(r, datetime.now().strftime("%Y-%m-%d"), ctx["week_end_str"])
        two = (' <span title="%d starts this matchup week" style="color:%s;font-weight:700;">&#215;2</span>' % (_n_starts, ACCENT)) if _n_starts >= 2 else ""
        # QS / 5K+ badges annotate the projected line (same rule as the digest's My
        # Upcoming Starts, so they never contradict the Proj. Line shown here). Hover
        # titles mirror the digest badges.
        _pip, _per, _pk = vals if vals else (0, 0, 0)
        badges = ""
        if vals and sd._proj_is_qs(_pip, _per):
            _qt = f'Projected {_fmt_ip(_pip)} IP&middot;{_per} ER &mdash; quality start (6+ IP, &le; 3 ER)'
            badges += (f' <span title="{_qt}" style="font-size:8px;font-weight:700;color:{CYAN};'
                       f'background:rgba(34,211,238,0.12);border:1px solid rgba(34,211,238,0.35);'
                       f'border-radius:3px;padding:0 3px;vertical-align:middle;">QS</span>')
        if vals and _pk >= 5:
            _kstat = sd._k5_stat_clause(r)
            _kt = f'Projected {_pk} strikeouts (&ge; 5)' + (f' &mdash; {_kstat}' if _kstat else '')
            badges += (f' <span title="{_kt}" style="font-size:8px;font-weight:700;color:{YELLOW};'
                       f'background:rgba(245,158,11,0.12);border:1px solid rgba(245,158,11,0.35);'
                       f'border-radius:3px;padding:0 3px;vertical-align:middle;">5K+</span>')
        # ⚠ RISK — low-floor (blowup-prone) skill profile, L15-escalated; same rule/model as
        # the digest's My Upcoming Starts, so a flag here never contradicts the digest.
        _l15 = (ctx["p15"].get(r.get("PlayerName", "")) or ctx["rec_p"].get(r.get("PlayerName", ""), {})).get("ERA")
        if sd._is_blowup_risk(r, _l15):
            _rd = sd._risk_drivers(r, _l15)
            _rt = "Low floor &mdash; blowup-prone: " + " &middot; ".join(_rd) if _rd else "Low floor &mdash; blowup-prone"
            badges += (f' <span title="{_rt}" style="font-size:8px;font-weight:700;color:{ORANGE};'
                       f'background:rgba(234,88,12,0.12);border:1px solid rgba(234,88,12,0.35);'
                       f'border-radius:3px;padding:0 3px;vertical-align:middle;">&#9888;</span>')
        badges += _reg_chip8(r)   # $ buy-low / ▼ sell-high (ERA vs xERA)
        rows.append(
            f'<div style="display:flex;justify-content:space-between;gap:6px;padding:2px 0;white-space:nowrap;border-bottom:1px solid {BORDER};">'
            f'<span style="overflow:hidden;text-overflow:ellipsis;">{sd.team_logo(r.get("Team"), 14)}<span style="color:{TEXT};font-weight:600;">{r.get("PlayerName")}</span>{two} '
            f'<span style="color:{MUTED};font-size:10px;">{dlabel} {hva}</span>{badges}</span>'
            f'<span style="flex:0 0 auto;"><span style="color:{MUTED};font-size:10px;">{line}</span> '
            f'<span style="color:{ACCENT};font-size:10px;">QS{qs}%</span> {_mini_badge(sd._score_p(r, ctx["best_recent_p"]))}</span></div>'
        )
    if not rows:
        rows.append(f'<div style="color:{MUTED};">No upcoming starts this matchup.</div>')

    # Coldest active arm (season ERA vs L15) — one-liner
    cold = _arm_movers(ctx)
    body = "".join(rows) + cold
    starts_wk = sum(1 for s in ctx["starts"] if s.get("PSP_Date", "") <= ctx["week_end_str"])
    return _tile("My Pitching", body, flex=1.15, sub=f'{starts_wk} starts this matchup')


def _arm_movers(ctx):
    """One hottest + one coldest rostered arm by 15-day ERA vs season."""
    my_key = " ".join(ctx["my_team"].split())
    movers = []
    for r in ctx["pitchers"]:
        if (" ".join((r.get("FantasyTeam") or "").split()) == my_key and int(r.get("Dataset", 0) or 0) == YEAR
                and _n(r.get("ERA")) > 0):
            rp = ctx["p15"].get(r.get("PlayerName", "")) or ctx["rec_p"].get(r.get("PlayerName", ""), {})
            r_era = _n(rp.get("ERA")); r_ip = _n(rp.get("IP"))
            if r_era > 0 and r_ip >= 3:
                movers.append((_n(r.get("ERA")) - r_era, r.get("PlayerName"), r_era))
    if not movers:
        return ""
    movers.sort(reverse=True)
    hot = movers[0]; cold = movers[-1]
    bits = []
    if hot[0] >= 0.40:
        bits.append(f'<span style="color:{GREEN};">&#128293; {hot[1]} {hot[2]:.2f}</span>')
    if cold[0] <= -0.40 and cold[1] != hot[1]:
        bits.append(f'<span style="color:{ACCENT};">&#10052; {cold[1]} {cold[2]:.2f}</span>')
    if not bits:
        return ""
    return f'<div style="margin-top:5px;padding-top:4px;font-size:11px;color:{MUTED};">L15 ERA &nbsp;{" &nbsp;&middot;&nbsp; ".join(bits)}</div>'


def render_hitting(ctx):
    my_key = " ".join(ctx["my_team"].split())
    movers = []
    for r in ctx["hitters"]:
        if (" ".join((r.get("FantasyTeam") or "").split()) == my_key and int(r.get("Dataset", 0)) == YEAR
                and float(r.get("OPS") or 0) > 0):
            rh = ctx["best_recent_h"].get(r.get("PlayerName", ""), {})
            r_ops = _n(rh.get("OPS"))
            if r_ops > 0:
                movers.append((r_ops - _n(r.get("OPS")), r, r_ops))
    # sort by the OPS delta only -- tuples carry a dict (r), so an unkeyed
    # sort compares dicts on a delta tie and raises TypeError (crashed CI 2026-07-11)
    movers.sort(key=lambda x: x[0], reverse=True)
    hot = movers[:3]; cold = movers[-3:][::-1] if len(movers) > 3 else []

    def line(d, r, r_ops, icon, col):
        hrp = _n(r.get("HR_Probability"))
        hr_s = f' <span style="color:{MUTED};font-size:9px;">HR{hrp*100:.0f}%</span>' if hrp > 0 else ""
        return (
            f'<div style="display:flex;justify-content:space-between;gap:5px;white-space:nowrap;padding:3.5px 0;border-bottom:1px solid {BORDER};">'
            f'<span style="overflow:hidden;text-overflow:ellipsis;color:{TEXT};">{icon} {sd.team_logo(r.get("Team"), 14)}{r.get("PlayerName")}{sd.hitter_badges(r, ctx["hit_pctile"])} '
            f'<span style="color:{MUTED};font-size:10px;">{_pos(r)}</span></span>'
            f'<span style="flex:0 0 auto;"><span style="color:{col};font-weight:700;">{_fv(r_ops,3)}</span>'
            f'<span style="color:{MUTED};font-size:10px;"> ({d:+.3f})</span>{hr_s} {_mini_badge(sd._blend(r, sd.hitter_score, ctx["best_recent_h"]))}</span></div>'
        )
    rows = [line(d, r, ro, "&#128293;", GREEN) for d, r, ro in hot]
    if cold:
        rows.append(f'<div style="border-top:1px solid {BORDER};margin:4px 0;"></div>')
        rows += [line(d, r, ro, "&#10052;", ACCENT) for d, r, ro in cold]
    if not rows:
        rows = [f'<div style="color:{MUTED};">No hitter data.</div>']
    return _tile("Hitting Hot / Cold", "".join(rows), flex=1.18, sub="7-day OPS vs season")


def render_holes(ctx):
    # Only show GENUINE needs -- bottom-third rank -- matching the app-wide convention
    # (_roster_suggestion / Trade Radar: rank >= n - third + 1, third = round(n/3)). Without
    # this gate the tile force-fed the 3 weakest-ranked positions even when the 3rd was fine
    # (e.g. 1B #7/12 anchored by an 83-score Olson), reading as a false "1B hole".
    def _is_need(p):
        n = p.get("n_teams") or 12
        third = max(1, round(n / 3.0))
        return (p.get("rank") or 0) >= n - third + 1
    ranked = sorted([p for p in ctx["pos_data"] if p.get("rank") and _is_need(p)],
                    key=lambda p: -(p["rank"] or 0))
    rows = []
    for p in ranked[:3]:
        worst = p.get("worst_player"); fa = (p.get("top_fa") or [None])[0]
        _hit_pos = p.get("ptype") == "hit"
        wname = worst.get("PlayerName", "—") if worst else "—"
        wlogo = sd.team_logo(worst.get("Team"), 13) if worst else ""
        wsc = int(worst.get("_pscore", 0)) if worst else 0
        wbadge = (sd.hitter_badges(worst, ctx["hit_pctile"]) if _hit_pos else sd.pitcher_regression_badge(worst)) if worst else ""
        fa_s = ""
        if fa:
            fsc = int(fa.get("_pscore", 0))
            # Green "upgrade" is judged vs my STARTER quality (my_avg = top-K starter avg),
            # NOT the worst eligible body (wsc) -- which is often a multi-eligible backup
            # whose weakness belongs elsewhere. Matches the digest ↑ arrow (#60); the
            # worst→FA pairing still displays the drop target, only the green is honest.
            gain = fsc - (p.get("my_avg") or 0)
            fbadge = sd.hitter_badges(fa, ctx["hit_pctile"]) if _hit_pos else sd.pitcher_regression_badge(fa)
            fa_s = (f' &rarr; {sd.team_logo(fa.get("Team"), 13)}<span style="color:{GREEN if gain>0 else MUTED};">{fa.get("PlayerName")}</span>{fbadge} {_mini_badge(fsc)}')
        rows.append(
            f'<div style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;padding:2px 0;border-bottom:1px solid {BORDER};">'
            f'<span style="color:{YELLOW};font-weight:700;">{p["pos"]}</span> '
            f'<span style="color:{MUTED};font-size:10px;">#{p["rank"]}/{p["n_teams"]}</span> '
            f'{wlogo}<span style="color:{TEXT};">{wname}</span>{wbadge} {_mini_badge(wsc)}{fa_s}</div>'
        )
    holes = "".join(rows) if rows else f'<div style="color:{MUTED};">Balanced roster.</div>'
    watch = sd.build_bench_watch(ctx["lineup_eff_current"]) if ctx["lineup_eff_current"] else ""
    if watch:
        # Lineup Watch is the ONE dashboard region allowed to scroll (per user
        # preference): bench-leakage detail can run long, so it gets its own
        # overflow-y:auto pane below the fixed Weakest-Spots rows. On tablet the
        # tile-body is content-sized (overflow:visible), so height:100% resolves to
        # the content and the pane just expands with the page — no inner scrollbar.
        body = (
            f'<div style="display:flex;flex-direction:column;height:100%;min-height:0;">'
            f'<div style="flex:0 0 auto;">{holes}</div>'
            f'<div style="flex:1 1 0;min-height:0;overflow-y:auto;margin-top:5px;font-size:10.5px;">{watch}</div>'
            f'</div>'
        )
    else:
        body = holes
    return _tile("Weakest Spots &middot; Lineup Watch", body, flex=1.0, sub="rank / worst &rarr; best FA")


def render_trade_radar(ctx):
    """Abbreviated Trade Radar — just the top couple of swaps (the full list lives in the
    daily digest). Two lines per trade (give / get), canonical $/▼ + position chips."""
    trades = ctx.get("trades") or []

    def pl(p, is_get):
        chips = ""
        if p.get("_tsell"):
            tip = ("results ahead of his Statcast expected — regression risk, you'd be buying high"
                   if is_get else "results ahead of his Statcast expected — sell him high")
            chips += f' <span title="{tip}" style="color:{RED};font-weight:700;font-size:11px;cursor:help;">&#9660;</span>'
        elif p.get("_tbuy"):
            tip = ("results behind his Statcast expected — positive regression likely, acquire cheap"
                   if is_get else "results behind his Statcast expected — a rebound candidate, think twice before dealing him")
            chips += f' <span title="{tip}" style="color:{GREEN};font-weight:700;font-size:11px;cursor:help;">$</span>'
        if is_get and p.get("_tfillpos"):
            _pp = ",".join(p["_tfillpos"])
            chips += f' <span title="upgrades your thin {_pp} — a position you rank near the bottom of the league" style="color:{CYAN};font-size:11px;cursor:help;">({_pp})</span>'
        return (f'<div style="font-size:12px;line-height:1.5;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">'
                f'{sd.team_logo(p.get("Team"), 12)}<span style="color:{TEXT};">{p.get("PlayerName")}</span> '
                f'{_score_pill_tip(p)}{chips}</div>')

    _clip = "overflow:hidden;text-overflow:ellipsis;white-space:nowrap;"
    _colhdr = "font-size:9.5px;font-weight:700;letter-spacing:.5px;text-transform:uppercase;margin-bottom:2px;"

    def pl_entry(entry, is_get):
        r, nm = entry
        if r is None:
            return (f'<div style="font-size:12px;line-height:1.5;{_clip}color:{MUTED};">'
                    f'{nm} <span style="font-size:9px;">(no data)</span></div>')
        return pl(r, is_get)

    # A REAL incoming offer is a decision on a clock — it leads the tile, above the
    # speculative radar ideas (which drop to one row so the tile stays zero-scroll).
    pending = ctx.get("pending_incoming") or []
    offer_cards = []
    for g in pending:
        label, vcolor, why = g["verdict"] if g.get("verdict") else ("REVIEW", ACCENT, "")
        logo = sd.fantasy_logo(ctx["team_logos"].get(g["partner"], ""), 14, g["partner"])
        give = "".join(pl_entry(e, False) for e in g["give_rows"])
        get_ = "".join(pl_entry(e, True)  for e in g["get_rows"])
        exp = (f'<div style="font-size:9px;color:{RED};font-weight:600;">{g["expiry_str"]}</div>'
               if g.get("expiry_str") else "")
        counter = (f'<div style="font-size:10px;color:{YELLOW};margin-top:5px;">'
                   f'&#128161; Counter: {g["counter"]}</div>' if g.get("counter") else "")
        offer_cards.append(
            f'<div style="background:{SURFACE2};border-left:3px solid {vcolor};border-radius:6px;'
            f'padding:7px 9px;margin-bottom:8px;">'
            f'<div style="display:flex;align-items:flex-start;gap:8px;">'
            f'<div style="flex:0 0 27%;min-width:0;">'
            f'<div style="font-size:8.5px;font-weight:700;letter-spacing:.4px;color:{ACCENT};'
            f'text-transform:uppercase;">&#128229; Offer to review</div>'
            f'<div style="font-size:11.5px;{_clip}">{logo}<span style="color:{TEXT};font-weight:600;">{g["partner"]}</span></div>'
            f'<div style="margin-top:2px;">{sd._verdict_pill(label, vcolor)}</div>{exp}</div>'
            f'<div style="flex:1 1 0;min-width:0;">'
            f'<div style="{_colhdr}color:{RED};">Give</div>{give}</div>'
            f'<div style="flex:1 1 0;min-width:0;">'
            f'<div style="{_colhdr}color:{GREEN};">Get</div>{get_}</div>'
            f'</div>{counter}</div>')

    # Abbreviated view: prefer DISTINCT partners (the dashboard is dense); backfill from the
    # full ranked list if only one distinct team fits. When REAL incoming offers exist they
    # LEAD the tile (a live decision on a clock beats speculative ideas), and we keep just ONE
    # radar idea below them — from a team I DON'T already have an incoming offer from (a fresh
    # partner to shop, not a duplicate of a pending negotiation). The pane scrolls so nothing
    # clips.
    offer_partners = {g["partner"] for g in pending}
    max_radar = 1 if offer_cards else 2
    top, _seen = [], set()
    for t in trades:
        if len(top) >= max_radar:  # check BEFORE appending, so the cap is exact
            break
        if t["team"] in _seen or t["team"] in offer_partners:
            continue               # skip teams I already have a pending offer with
        _seen.add(t["team"]); top.append(t)
    for t in trades:               # backfill if fewer than max_radar distinct fresh partners exist
        if len(top) >= max_radar:
            break
        if t not in top and t["team"] not in offer_partners:
            top.append(t)

    rows = list(offer_cards)
    for t in top:
        give = "".join(pl(o, False) for o in t["outs"])
        get_ = "".join(pl(i, True) for i in t["ins"])
        net = t.get("net_val", 0)
        val = "you win" if net > 0.1 else "even" if net >= -0.1 else "you pay up"
        _, accept, acc_color = sd._trade_tilt(net, t["ins"], t["outs"], net_them=t.get("net_them"))   # rival's-POV acceptance read (demand-side aware; realistic / aggressive ask; star-reach aware)
        thesis = "sell-high" if t.get("sell_out") else ""
        thesis += ("/buy-low" if thesis and t.get("buy_in") else ("buy-low" if t.get("buy_in") else ""))
        logo = sd.fantasy_logo(ctx["team_logos"].get(t["team"], ""), 14, t["team"])
        accent = GREEN if net > 0.1 else MUTED
        # Each trade is a padded mini-card laid out as THREE side-by-side columns —
        # (1) partner + value/thesis "why", (2) what I GIVE, (3) what I GET — so the card
        # reads horizontally and stays short (both swaps fit without one getting clipped).
        # Left accent green when value tilts to me.
        rows.append(
            f'<div style="background:{SURFACE2};border-left:3px solid {accent};border-radius:6px;'
            f'padding:7px 9px;margin-bottom:8px;display:flex;align-items:flex-start;gap:8px;">'
            f'<div style="flex:0 0 27%;min-width:0;">'
            f'<div style="font-size:11.5px;{_clip}">{logo}<span style="color:{TEXT};font-weight:600;">{t["team"]}</span></div>'
            f'<div style="font-size:9.5px;color:{accent};font-weight:600;margin-top:1px;">{val}</div>'
            f'<div style="font-size:9px;color:{acc_color};font-weight:600;">{accept}</div>'
            + (f'<div style="font-size:9px;color:{MUTED};">{thesis}</div>' if thesis else "")
            + (f'<div style="font-size:8.5px;color:{ORANGE};">{t["thin_note"]}</div>' if t.get("thin_note") else "")
            + f'</div>'
            f'<div style="flex:1 1 0;min-width:0;">'
            f'<div style="{_colhdr}color:{RED};">Give</div>{give}</div>'
            f'<div style="flex:1 1 0;min-width:0;">'
            f'<div style="{_colhdr}color:{GREEN};">Get</div>{get_}</div>'
            f'</div>')
    if not rows:
        rows = [f'<div style="color:{MUTED};">No trade fits right now.</div>']
    body = "".join(rows)
    if offer_cards:
        title = "Trades"
        n_off = len(offer_cards)
        sub = (f'{n_off} incoming offer{"s" if n_off != 1 else ""} to review '
               f'&middot; then 1 idea')
        # Whenever a live offer leads the tile the body scrolls (the ONLY dashboard scroll
        # region besides Lineup Watch, per user preference) — so every incoming offer PLUS
        # the one fresh-partner idea is always reachable, none clipped.
        body = f'<div style="height:100%;overflow-y:auto;">{body}</div>'
    else:
        title, sub = "Trade Radar", "top mutual-benefit swaps &middot; hover a badge for why"
    return _tile(title, body, flex=0.9, sub=sub)


# ── Column 3: Moves, FA Radar, Season ───────────────────────────────────────────

def render_moves(ctx):
    rows = []
    for b in (ctx["roster_sugg"] or [])[:3]:
        rows.append(f'<div style="padding:4px 0;border-bottom:1px solid {BORDER};font-size:11.5px;line-height:1.45;">{b}</div>')
    if not rows:
        rows.append(f'<div style="color:{MUTED};">No pressing moves — roster is set.</div>')
    # Save-role watch
    srw = []
    for e in ctx["emerging"][:2]:
        srw.append(f'<span style="color:{GREEN};">&#9650; {e["name"]}</span> ({e["recent"]} recent SV, FA)')
    for f in ctx["fading"][:2]:
        srw.append(f'<span style="color:{RED};">&#9660; {f["name"]}</span> (cold, {int(f["season"])} SV+H)')
    if srw:
        rows.append(f'<div style="margin-top:6px;font-size:10.5px;color:{MUTED};">'
                    f'<span style="text-transform:uppercase;letter-spacing:.4px;">Save-role watch:</span><br>' + " &middot; ".join(srw) + '</div>')
    return _tile("Recommended Moves", "".join(rows), flex=0.85)


def render_fa_radar(ctx):
    def spline(r, sc, extra, badges=""):
        return (f'<div style="display:flex;justify-content:space-between;gap:6px;white-space:nowrap;padding:2px 0;border-bottom:1px solid {BORDER};">'
                f'<span style="overflow:hidden;text-overflow:ellipsis;color:{TEXT};">{sd.team_logo(r.get("Team"), 13)}{r.get("PlayerName")}{badges} '
                f'<span style="color:{MUTED};font-size:10px;">{_pos(r)}</span></span>'
                f'<span style="flex:0 0 auto;"><span style="color:{MUTED};font-size:10px;">{extra}</span> {_mini_badge(sc)}</span></div>')
    def hdr(t):
        return f'<div style="color:{ACCENT};font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;margin-top:4px;">{t}</div>'
    parts = [hdr("Starters")]
    for r in ctx["fa_sp"][:2]:
        qs = sd.qs_probability(r)
        _l15 = (ctx["p15"].get(r.get("PlayerName", "")) or ctx["rec_p"].get(r.get("PlayerName", ""), {})).get("ERA")
        parts.append(spline(r, r.get("_score", 0), f'{_n(r.get("ERA")):.2f} ERA &middot; QS{qs}%',
                            badges=sd.blowup_badge(r, _l15) + sd.pitcher_regression_badge(r)))
    parts.append(hdr("Relievers"))
    for r in ctx["fa_rp"][:2]:
        parts.append(spline(r, r.get("_rp_score", 0), f'{int(_n(r.get("ESPN_SVHD")) or _n(r.get("SVHD")))} SV+H &middot; {_n(r.get("ERA")):.2f}',
                            badges=sd.pitcher_regression_badge(r)))
    parts.append(hdr("Hitters"))
    for r in ctx["fa_hit"][:2]:
        parts.append(spline(r, r.get("_score", 0), f'{_fv(_n(r.get("OPS")),3)} OPS', badges=sd.hitter_badges(r, ctx["hit_pctile"])))
    return _tile("Free-Agent Radar", "".join(parts), flex=1.2, sub="top available by score")


def render_season(ctx):
    my_row = ctx["my_row"]
    # Standings / roto / luck mini line
    def stat(lbl, val):
        return (f'<div style="text-align:center;"><div style="color:{MUTED};font-size:10px;text-transform:uppercase;'
                f'letter-spacing:.5px;">{lbl}</div><div style="color:{TEXT};font-size:18px;font-weight:800;">{val}</div></div>')
    _std = my_row.get("standing", "—")
    _rr  = my_row.get("roto_rank", "—")
    _pts = my_row.get("roto_pts", "—")
    _rec = f'{my_row.get("wins",0)}-{my_row.get("losses",0)}-{my_row.get("ties",0)}'
    top = (
        f'<div style="display:flex;justify-content:space-around;margin-bottom:4px;">'
        f'{stat("Standing", f"#{_std}")}{stat("Roto", f"#{_rr}")}'
        f'{stat("Pts", _pts)}{stat("Record", _rec)}'
        f'</div>'
    )
    avg_rank = f"{sum(ctx['wk_ranks'])/len(ctx['wk_ranks']):.1f}" if ctx["wk_ranks"] else "—"
    spark = f'<div style="margin:4px 0 2px;">{_stretch_spark(ctx["sparkline"])}</div>' if ctx["sparkline"] else ""
    spark_sub = (f'<div style="color:{MUTED};font-size:10px;">roto by matchup &middot; avg finish #{avg_rank} &middot; '
                 f'{ctx["peak_label"].replace("<div", "<span").replace("</div>", "</span>")}</div>')

    # Trajectory strip — my weekly H2H finishes. Show the SAME completed-week set the
    # sparkline plots (weeks < current_week) so the pill count lines up with the line's
    # data points instead of confusingly showing fewer.
    my_key = " ".join(ctx["my_team"].split())
    wr = ctx["weekly_results"] or {}
    cur = ctx["current_week_num"] or 0
    weeks = sorted(w for w in (int(k) for k in wr.keys()) if not cur or w < cur)
    cells = []
    for w in weeks:
        res = (wr.get(str(w)) or wr.get(w) or {}).get(my_key) or (wr.get(str(w)) or wr.get(w) or {}).get(ctx["my_team"])
        c = GREEN if res == "W" else (RED if res == "L" else (MUTED if res else BORDER))
        lab = res or "&middot;"
        cells.append(f'<div style="flex:1;text-align:center;background:{c}22;border:1px solid {c};border-radius:3px;'
                     f'padding:4px 0;color:{c};font-size:11px;font-weight:700;">{lab}</div>')
    strip = (f'<div style="margin-top:6px;"><div style="color:{MUTED};font-size:10px;text-transform:uppercase;'
             f'letter-spacing:.5px;margin-bottom:3px;">Weekly finishes</div>'
             f'<div style="display:flex;gap:3px;">{"".join(cells)}</div></div>') if cells else ""
    return _tile("Season", top + spark + spark_sub + strip, flex=1.15)


# ══════════════════════════════════════════════════════════════════════════════
# ASSEMBLE
# ══════════════════════════════════════════════════════════════════════════════

# ── Legend / key ────────────────────────────────────────────────────────────────

def _legend_chip(glyph, color, size=8):
    """A tinted badge chip matching the QS/5K+/PWR visual style, for the legend."""
    r, g, b = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
    return (f'<span style="font-size:{size}px;font-weight:700;color:{color};'
            f'background:rgba({r},{g},{b},0.12);border:1px solid rgba({r},{g},{b},0.35);'
            f'border-radius:3px;padding:0 3px;vertical-align:middle;">{glyph}</span>')


def _legend_items():
    """The badge/marker key items, shared by the desktop bottom bar (render_legend)
    and the tablet panel (render_legend_panel) so the two never drift apart."""
    def plain(g, c, sz=10):
        return f'<span style="color:{c};font-weight:700;font-size:{sz}px;">{g}</span>'

    def item(badge, desc):
        return (f'<span style="display:inline-flex;align-items:center;gap:3px;white-space:nowrap;">'
                f'{badge}<span style="color:{MUTED};font-size:9px;">{desc}</span></span>')

    items = [
        item(_mini_badge(80), "role score 0&ndash;100"),
        item(f'{plain("&#9650;",GREEN,9)}{plain("&#9660;",RED,9)}{plain("&#9670;",TEXT,9)}', "proj W / L / T"),
        item(plain("&#9889;", YELLOW), "toss-up cat"),
        item(f'<span style="color:{GREEN};font-size:9px;font-weight:700;">62%</span>', "cat win %"),
        item(plain("&#215;2", ACCENT), "two starts / wk"),
        item(_legend_chip("QS", CYAN), "proj quality start"),
        item(_legend_chip("5K+", YELLOW), "proj 5+ K"),
        item(_legend_chip("&#9888;", ORANGE), "low floor / blowup risk"),
        item(_legend_chip("$", GREEN), "buy-low (unlucky)"),
        item(_legend_chip("&#9660;", RED), "sell-high (lucky)"),
        item(_legend_chip("PWR", sd.PURPLE), "power / HR threat"),
        item(_legend_chip("SB", sd.SILVER), "speed / steals"),
        item(f'{plain("&#128293;",GREEN)}{plain("&#10052;",ACCENT)}', "hot / cold vs season"),
    ]
    return "".join(items)


def render_legend():
    """A slim key strip pinned to the bottom of the DESKTOP pane, defining every
    badge/marker on the dashboard. Sits outside both grids (flex:0 0 auto). Hidden on
    tablet/phone (media query below 1100px), where the same key renders as a panel at
    the foot of the right tablet column instead (render_legend_panel), per user pref."""
    return (
        f'<div id="legend" style="flex:0 0 auto;background:{SURFACE};border:1px solid {BORDER};'
        f'border-radius:6px;padding:4px 10px;display:flex;flex-wrap:wrap;align-items:center;gap:5px 12px;">'
        f'<span style="color:{TEXT};font-weight:800;text-transform:uppercase;letter-spacing:.6px;font-size:9px;">Key</span>'
        + _legend_items() +
        f'</div>'
    )


def render_legend_panel():
    """The same key rendered as a normal tile — placed last in the right tablet column
    (inside #gridt, so it only shows on tablet/phone; desktop uses the bottom bar)."""
    inner = (
        f'<div style="display:flex;flex-wrap:wrap;align-items:center;gap:5px 12px;">'
        + _legend_items() +
        f'</div>'
    )
    return _tile("Key", inner)


STYLE = """
  * { box-sizing:border-box; }
  html,body { margin:0; padding:0; height:100%; }
  body { background:#060b18; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
         color:#e2e8f0; overflow:hidden; }
  #wrap { position:fixed; inset:0; display:flex; flex-direction:column; gap:6px; padding:6px; }
  #grid { flex:1 1 auto; min-height:0; display:grid; grid-template-columns:1fr 1fr 1fr;
          grid-template-rows:minmax(0,1fr); gap:6px; }
  .col { display:flex; flex-direction:column; gap:6px; min-height:0; height:100%; }
  /* Tablet/phone grid — hidden on desktop, swapped in below 1100px. */
  #gridt { display:none; }
  .colt { display:flex; flex-direction:column; gap:6px; min-width:0; }

  /* ---- Tablet (<=1100px): 2 columns, un-pin the page and allow normal scrolling.
     Tiles size to their content (no clipping) since the single no-scroll pane only
     makes sense on a wide screen. Desktop (>1100px) is untouched. ---- */
  @media (max-width:1100px) {
    html, body { overflow-y:auto !important; overflow-x:hidden !important; height:auto !important; }
    #wrap { position:static !important; inset:auto !important; height:auto !important; min-height:100vh; }
    /* Swap the desktop 3-col grid for the tablet grid: two INDEPENDENT-PACKING flex
       columns holding the tiles in the user's order (left = 1,6,7,2; right = 3,4,8,5).
       Because each column is its own flex-column, tiles pack tight top-to-bottom with
       NO cross-column row alignment — so a short tile never leaves whitespace beneath
       it (the failure mode of the earlier order-aware single grid). `align-items:
       flex-start` keeps the two columns their natural heights (a ragged bottom, but no
       internal gaps). */
    #grid  { display:none !important; }
    #legend { display:none !important; }  /* desktop bottom bar; tablet uses the panel in colt_r */
    #gridt { display:flex !important; gap:6px !important; align-items:flex-start !important; }
    .colt  { flex:1 1 0 !important; min-width:0 !important; }
    .tile { flex:0 0 auto !important; min-height:0 !important; overflow:visible !important; margin:0 !important; }
    .tile-body { flex:0 0 auto !important; overflow:visible !important; font-size:13.5px !important; }
    .pulse-grid { height:auto !important; grid-auto-rows:auto !important; grid-template-columns:repeat(3,1fr) !important; }
    .topbar { flex-wrap:wrap !important; gap:8px !important; }
    .topbar-chips { flex-wrap:wrap !important; justify-content:flex-start !important; row-gap:6px; }
  }

  /* ---- Phone (<=700px): stack the two tablet columns into one (straight down in the
     order 1,6,7,2,3,4,8,5), bigger text, 2-wide category grid (roomier for OPS/ERA/
     WHIP than 3-wide). ---- */
  @media (max-width:700px) {
    #gridt { flex-direction:column !important; }
    .colt  { flex:0 0 auto !important; width:100% !important; }
    .tile-body { font-size:14.5px !important; }
    .pulse-grid { grid-template-columns:repeat(2,1fr) !important; }
    .chip { padding:0 6px !important; }
    .topbar-chips { width:100% !important; }
  }
"""


def build_dashboard(snap, my_team):
    ctx = build_context(snap, my_team)
    topbar = render_topbar(ctx)

    # Render each tile ONCE, then place the IDENTICAL markup in two independent
    # layout containers (tiles carry no ids/anchors, so duplication is safe — only
    # one container is ever displayed at a breakpoint):
    #   #grid  — desktop 3-col fixed no-scroll pane (>1100px)
    #   #gridt — tablet/phone: two INDEPENDENT-PACKING flex columns in the user's
    #            order. Each column packs tight (like the desktop columns), so there
    #            is NO row-alignment whitespace (which an order-aware single grid,
    #            the previous approach, left below short tiles).
    t_tv     = render_tv_games(ctx)
    t_pulse  = render_category_pulse(ctx)
    t_holes  = render_holes(ctx)
    t_pitch  = render_pitching(ctx)
    t_hit    = render_hitting(ctx)
    t_moves  = render_moves(ctx)
    t_fa     = render_fa_radar(ctx)
    t_season = render_season(ctx)
    t_trade  = render_trade_radar(ctx)

    # Desktop 3-col: col1 = Pulse + Weakest Spots/Lineup, col2 = Pitching·Hitting·Trade
    # Radar (Trade Radar took the Opponent This Matchup slot per user preference — opponent
    # scouting still lives in the digest), col3 = Moves·FA·Season.
    # Today's Games rides at the TOP of col1 (above Category Pulse) — the user wants the
    # TV panel first on the dashboard. It's a compact 2-game tile (favorite + top overlap)
    # so col1 stays a 3-tile column like the others; '' (no games) collapses cleanly.
    col1 = f'<div class="col">{t_tv}{t_pulse}{t_holes}</div>'
    col2 = f'<div class="col">{t_pitch}{t_hit}{t_trade}</div>'
    col3 = f'<div class="col">{t_moves}{t_fa}{t_season}</div>'
    grid_desktop = f'<div id="grid">{col1}{col2}{col3}</div>'

    # Tablet 2-col, HEIGHT-BALANCED: left = Pulse · Moves · FA; right =
    # Pitching · Hitting · Weakest Spots · Trade Radar · Season · Key. The two tall
    # tiles (Pulse + Weakest Spots) sit one-per-column so the columns end at roughly the
    # same height. Season rides 2nd-to-last in the right column, just above the Key panel
    # (per user preference). On a phone the two columns stack top-to-bottom.
    colt_l = f'<div class="colt">{t_tv}{t_pulse}{t_moves}{t_fa}</div>'
    colt_r = f'<div class="colt">{t_pitch}{t_hit}{t_holes}{t_trade}{t_season}{render_legend_panel()}</div>'
    grid_tablet = f'<div id="gridt">{colt_l}{colt_r}</div>'

    legend = render_legend()

    return (
        f'<!DOCTYPE html><html><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>Dashboard — {my_team}</title><style>{STYLE}</style></head>'
        f'<body><div id="wrap">{topbar}{grid_desktop}{grid_tablet}{legend}</div></body></html>'
    )


def send_dashboard_email(html, my_team):
    """Email the dashboard to yourself as an ATTACHMENT (reuses send_digest's Gmail
    SMTP creds). The whole layout lives in a <style> block that Gmail strips from an
    inline body, so the message body is just a pointer — the working dashboard is the
    attached .html, which the reader opens in their phone/tablet browser."""
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    if not sd.GMAIL_APP_PASSWORD:
        print("ERROR: GMAIL_APP_PASSWORD not set — add it to .env (same one the digest uses).")
        return False

    date_str = datetime.now().strftime("%Y-%m-%d")
    slug  = my_team.strip().replace(" ", "_")
    fname = f"dashboard_{slug}_{date_str}.html"
    subject = f"⚾ {my_team} Dashboard — {datetime.now().strftime('%b %d')}"
    body = (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,sans-serif;'
        'padding:16px;color:#111;">'
        '<h2 style="margin:0 0 8px;">Your command dashboard is attached.</h2>'
        f'<p style="color:#444;line-height:1.5;">Tap <b>{fname}</b> below &rarr; '
        '<b>Open in browser</b> to view it. It’s responsive — one column on a phone, '
        'two on a tablet, and the full three-column no-scroll pane on a laptop.</p>'
        '<p style="color:#888;font-size:12px;">(The dashboard’s styling lives in the file, '
        'which email apps can’t render inline — so open the attachment, not this message.)</p>'
        '</div>'
    )

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = sd.FROM_EMAIL
    msg["To"]   = sd.TO_EMAIL
    msg["Cc"]   = sd.CC_EMAIL
    msg.attach(MIMEText(body, "html"))
    att = MIMEText(html, "html", "utf-8")
    att.add_header("Content-Disposition", "attachment", filename=fname)
    msg.attach(att)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(sd.FROM_EMAIL, sd.GMAIL_APP_PASSWORD)
        smtp.sendmail(sd.FROM_EMAIL, [sd.TO_EMAIL, sd.CC_EMAIL], msg.as_string())
    print(f"Emailed dashboard to {sd.TO_EMAIL} (attachment: {fname})")
    return True


def main():
    ap = argparse.ArgumentParser(description="Single-viewport fantasy dashboard")
    ap.add_argument("--refresh", action="store_true", help="Refresh snapshot data first (~60s)")
    ap.add_argument("--team", default=None, help="Render another team's dashboard (needs all_matchups)")
    ap.add_argument("--email", action="store_true", help="Also email the dashboard to yourself as an attachment")
    args = ap.parse_args()

    if args.refresh:
        import fetch_data
        fetch_data.main()

    with open(SNAPSHOT, encoding="utf-8") as f:
        snap = json.load(f)

    # Non-blocking schema check: surface a drifted snapshot, never crash a working reader.
    try:
        from snapshot_schema import validate_snapshot, report as _snap_report
        _errs, _warns = validate_snapshot(snap)
        if _errs or _warns:
            _snap_report(_errs, _warns)
    except ImportError:
        pass

    my_team = args.team or snap.get("my_team", MY_TEAM)
    html = build_dashboard(snap, my_team)

    PREVIEWS.mkdir(exist_ok=True)
    slug = my_team.strip().replace(" ", "_")
    out = PREVIEWS / f"dashboard_{slug}.html"
    out.write_text(html, encoding="utf-8")
    print(f"Wrote {out}")

    if args.email:
        send_dashboard_email(html, my_team)


if __name__ == "__main__":
    main()
