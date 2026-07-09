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
from datetime import datetime, timedelta
from pathlib import Path

import send_digest as sd
from send_digest import (
    BG, SURFACE, SURFACE2, BORDER, TEXT, MUTED, ACCENT, GREEN, RED, YELLOW, PURPLE,
    YEAR, MY_TEAM, _n, _is_sp, _fmt_ip, _starts_this_week,
    _project, _cat_win_prob, _CAT_DEC, _CAT_LABELS_MAP, _LOWER_BETTER, _RATE_CATS,
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

    rec_h = {r["PlayerName"]: r for r in recent_hitting  if r.get("PlayerName")}
    rec_p = {r["PlayerName"]: r for r in recent_pitching if r.get("PlayerName")}
    def _idx(rows, ds):
        return {r["PlayerName"]: r for r in rows if int(r.get("Dataset", 0) or 0) == ds and r.get("PlayerName")}
    p7, p15, p30 = _idx(pitchers, 7), _idx(pitchers, 15), _idx(pitchers, 30)
    h7, h15, h30 = _idx(hitters, 7),  _idx(hitters, 15),  _idx(hitters, 30)

    # Derive volume benchmarks / calibration / league averages (ORDER MATTERS — the
    # scoring functions read the module globals these populate; see build_email).
    sd.compute_ab_benchmarks(hitters)
    sd.compute_pitcher_benchmarks(pitchers)
    sd.compute_score_calibration(pitchers)
    sd.compute_league_averages(hitters, pitchers)

    rec_p_fp = {}
    for name, r in rec_p.items():
        ip = _n(r.get("IP")); k = _n(r.get("K")); g = _n(r.get("G"))
        rec_p_fp[name] = {**r, "K/IP": round(k / ip, 3) if ip > 0 else 0,
                          "IP_per_G": round(ip / g, 2) if g > 0 else 0}
    best_recent_p = {**rec_p_fp, **p7, **p15, **p30}
    best_recent_h = {**rec_h,    **h7, **h15, **h30}

    today_str = datetime.now().strftime("%Y-%m-%d")
    todays_txns = [t for t in snap.get("transactions", []) if t.get("TransactionDate", "").startswith(today_str)]
    latest_txn = {}
    for t in sorted(todays_txns, key=lambda t: t.get("TransactionDate", "")):
        latest_txn[t["PlayerName"]] = t["TransactionType"]
    claimed = {name for name, tt in latest_txn.items() if tt == "FA ADDED"}

    fa_sp  = sd.fa_starters(pitchers, claimed, idx_recent=best_recent_p)
    fa_rp  = sd.fa_relievers(pitchers, claimed)
    fa_hit = sd.fa_hitters(hitters, claimed, idx_recent=best_recent_h)
    luck   = sd.luck_standings(roto, standings)
    team_logos = {" ".join(s["team_name"].split()): s.get("logo_url", "") for s in standings}
    cats, n = sd.category_ranks(roto, my_team)
    current_week_num = matchup.get("week") or max((int(r.get("Week", 0)) for r in roto), default=0)
    weekly_avgs = sd.compute_weekly_avgs(roto, current_week_num)
    weekly_std  = sd.compute_weekly_std(roto, current_week_num)
    _today = datetime.now().date()

    _mstart_raw = snap.get("matchup_start_date") or ""
    _mend_raw   = snap.get("matchup_end_date")   or ""
    _mdays      = snap.get("matchup_period_days") or 0
    if _mend_raw:
        matchup_end_date   = datetime.strptime(_mend_raw, "%Y-%m-%d").date()
        matchup_start_date = datetime.strptime(_mstart_raw, "%Y-%m-%d").date() if _mstart_raw else (_today - timedelta(days=_today.weekday()))
        matchup_period_days = int(_mdays) if _mdays else max(7, (matchup_end_date - matchup_start_date).days + 1)
        week_end_str = _mend_raw
    else:
        matchup_start_date  = _today - timedelta(days=_today.weekday())
        matchup_end_date    = _today + timedelta(days=6 - _today.weekday())
        matchup_period_days = 7
        week_end_str = matchup_end_date.strftime("%Y-%m-%d")
    days_elapsed = max(0, (_today - matchup_start_date).days)
    _mgdays    = snap.get("matchup_game_days")
    _mgdays_el = snap.get("matchup_game_days_elapsed")
    matchup_game_days = int(_mgdays) if _mgdays is not None else matchup_period_days
    game_days_elapsed = int(_mgdays_el) if _mgdays_el is not None else days_elapsed
    is_sunday = _today >= matchup_end_date

    # Pitcher counting-stat projections from actual remaining starts (K, QS, W) —
    # replicated from build_email's local closures (public helpers only).
    _opp_key = " ".join(matchup.get("opp_team", "").split()) if matchup else ""
    def _remaining_starters(team_key):
        return [r for r in pitchers
                if int(r.get("Dataset", 0) or 0) == YEAR
                and " ".join((r.get("FantasyTeam") or "").split()) == team_key
                and r.get("PSP_Date", "") not in ("1999-01-01", "", None)
                and today_str <= r.get("PSP_Date", "") <= week_end_str
                and _is_sp(r)]
    def _proj_qs(ss): return sum((sd.qs_probability(r) or 0) / 100 for r in ss)
    def _proj_k(ss):
        total = 0
        for r in ss:
            gs = _n(r.get("GS")); k = _n(r.get("K")); ip_g = _n(r.get("IP_per_G")); kip = _n(r.get("K/IP") or r.get("KIP"))
            total += (k / gs) if gs > 0 else (ip_g * kip if ip_g > 0 and kip > 0 else 5)
        return total
    def _proj_w(ss):
        total = 0
        for r in ss:
            gs = _n(r.get("GS")); w = _n(r.get("ESPN_W") or r.get("W"))
            total += (w / gs) if gs > 0 else 0.12
        return total
    _my_ss  = _remaining_starters(" ".join(my_team.split()))
    _opp_ss = _remaining_starters(_opp_key)
    pit_proj = {
        "QS": {"my": _proj_qs(_my_ss),  "opp": _proj_qs(_opp_ss)},
        "K":  {"my": _proj_k(_my_ss),   "opp": _proj_k(_opp_ss)},
        "W":  {"my": _proj_w(_my_ss),   "opp": _proj_w(_opp_ss)},
    }

    classification = sd.classify_categories(
        matchup, weekly_avgs=weekly_avgs, days_elapsed=days_elapsed, remaining_proj=pit_proj,
        matchup_days=matchup_period_days,
        game_days_elapsed=game_days_elapsed, matchup_game_days=matchup_game_days,
    )

    pos_data = sd.positional_breakdown(pitchers, hitters, my_team, best_recent_p, best_recent_h)
    starts   = sd.my_upcoming_starts(pitchers, my_team)
    alerts   = sd.roster_alerts(pitchers, hitters, my_team)
    opp_intel = sd.opponent_week_intel(pitchers, hitters, matchup.get("opp_team", "") if matchup else "",
                                       best_recent_h, today_str, week_end_str)
    lineup_eff_current = snap.get("lineup_efficiency_current", {}) if not override else {}
    roster_sugg = sd._roster_suggestion(
        matchup, pitchers, hitters, fa_sp, fa_rp, fa_hit, my_team, best_recent_p, best_recent_h,
        all_matchups, week_end_str, classification=classification,
        league_total_roster_max=snap.get("league_total_roster_max", 28),
        pos_data=pos_data, lineup_eff=lineup_eff_current,
    )
    emerging, fading = sd.save_role_watch(pitchers, my_team, claimed)

    # Per-week roto scores → sparkline + weekly finishes + KPI averages
    my_key = " ".join(my_team.split())
    week_scores = {}
    for row in roto:
        t = " ".join((row.get("Team") or "").split()); wk = int(row.get("Week", 0))
        week_scores.setdefault(wk, {})[t] = float(row.get("Roto_Score") or 0)
    wk_ranks, wk_pts, roto_week_results = [], [], {}
    for wk in sorted(week_scores):
        if wk >= current_week_num:
            continue
        scores = week_scores[wk]
        if my_key not in scores:
            continue
        ranked = sorted(scores.items(), key=lambda x: -x[1])
        roto_week_results[wk] = {t: ('W' if i == 0 else 'L') for i, (t, _) in enumerate(ranked)}
        my_rank = next((i + 1 for i, (t, _) in enumerate(ranked) if t == my_key), None)
        if my_rank:
            wk_ranks.append(my_rank); wk_pts.append(scores[my_key])
    sparkline, peak_label = sd.make_sparkline(roto, my_team, current_week_num, weekly_results=roto_week_results)

    # Roster-wide hot/cold counts (hitters 7d OPS ±.015, pitchers 15d ERA ±.40) — matches KPI.
    n_hot = n_cold = 0
    for r in hitters:
        if (" ".join((r.get("FantasyTeam") or "").split()) == my_key and int(r.get("Dataset", 0)) == YEAR
                and float(r.get("OPS") or 0) > 0):
            s_ops = float(r.get("OPS") or 0); rh = rec_h.get(r.get("PlayerName", ""), {})
            r_ops = float(rh.get("OPS") or 0) if rh else 0
            if s_ops > 0 and r_ops > 0:
                d = r_ops - s_ops
                if d >= 0.015: n_hot += 1
                elif d <= -0.015: n_cold += 1
    for r in pitchers:
        if (" ".join((r.get("FantasyTeam") or "").split()) == my_key and int(r.get("Dataset", 0) or 0) == YEAR
                and _n(r.get("ERA")) > 0):
            s_era = _n(r.get("ERA")); rp = p15.get(r.get("PlayerName", "")) or rec_p.get(r.get("PlayerName", ""), {})
            r_era = _n(rp.get("ERA")) if rp else 0; r_ip = _n(rp.get("IP")) if rp else 0
            if s_era > 0 and r_era > 0 and r_ip >= 3:
                d = s_era - r_era
                if d >= 0.40: n_hot += 1
                elif d <= -0.40: n_cold += 1

    my_row = next((r for r in luck if " ".join((r.get("team") or "").split()) == my_key), {})

    return dict(
        my_team=my_team, override=override, pitchers=pitchers, hitters=hitters, roto=roto,
        matchup=matchup, current_week_num=current_week_num, team_logos=team_logos,
        best_recent_p=best_recent_p, best_recent_h=best_recent_h, p15=p15, rec_p=rec_p, rec_h=rec_h,
        fa_sp=fa_sp, fa_rp=fa_rp, fa_hit=fa_hit, luck=luck, my_row=my_row, cats=cats, n_teams=n,
        weekly_avgs=weekly_avgs, weekly_std=weekly_std, classification=classification, pit_proj=pit_proj,
        pos_data=pos_data, starts=starts, alerts=alerts, opp_intel=opp_intel,
        lineup_eff_current=lineup_eff_current, roster_sugg=roster_sugg, emerging=emerging, fading=fading,
        sparkline=sparkline, peak_label=peak_label, roto_week_results=roto_week_results,
        weekly_results=weekly_results, wk_ranks=wk_ranks, wk_pts=wk_pts,
        days_elapsed=days_elapsed, game_days_elapsed=game_days_elapsed, matchup_game_days=matchup_game_days,
        matchup_period_days=matchup_period_days, week_end_str=week_end_str, is_sunday=is_sunday,
        n_hot=n_hot, n_cold=n_cold, refreshed_at=snap.get("refreshed_at", ""),
    )


# ══════════════════════════════════════════════════════════════════════════════
# COMPACT RENDER HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _fv(v, dec=0):
    """Format a category value; strip the leading zero for OPS/rate sub-1 values."""
    s = f"{v:.{dec}f}"
    return s[1:] if (dec >= 2 and 0 <= v < 1) else s


def _tile(title, body, flex=1.0, accent=ACCENT, sub=""):
    sub_html = f'<span style="color:{MUTED};font-weight:400;text-transform:none;letter-spacing:0;font-size:10px;margin-left:6px;">{sub}</span>' if sub else ""
    return (
        f'<div style="flex:{flex} 1 0;min-height:0;background:{SURFACE};border:1px solid {BORDER};'
        f'border-top:2px solid {accent};border-radius:6px;padding:6px 10px;display:flex;flex-direction:column;overflow:hidden;">'
        f'<div style="color:{TEXT};font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:.6px;'
        f'margin-bottom:4px;flex:0 0 auto;">{title}{sub_html}</div>'
        f'<div style="flex:1 1 0;min-height:0;overflow:hidden;font-size:12.5px;color:{TEXT};line-height:1.4;">{body}</div>'
        f'</div>'
    )


def _mini_badge(score):
    s = int(score or 0)
    if s >= 72:   bg = "#16a34a"
    elif s >= 52: bg = "#2563eb"
    elif s >= 32: bg = "#d97706"
    else:         bg = "#dc2626"
    return f'<span style="background:{bg};color:#fff;padding:1px 6px;border-radius:9px;font-size:10px;font-weight:800;">{s}</span>'


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
            f'<div style="text-align:center;padding:0 12px;border-left:1px solid {BORDER};">'
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
        f'<div style="flex:0 0 auto;background:linear-gradient(135deg,#0b1a38,#0f172a);border:1px solid {BORDER};'
        f'border-radius:6px;padding:8px 12px;display:flex;align-items:center;justify-content:space-between;gap:10px;">'
        f'<div style="display:flex;align-items:center;gap:8px;min-width:0;">{logo}'
        f'<div style="min-width:0;"><div style="color:{TEXT};font-size:17px;font-weight:900;letter-spacing:-.5px;'
        f'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">{my_team}</div>'
        f'<div style="color:#4b7bc4;font-size:9px;text-transform:uppercase;letter-spacing:.6px;">'
        f'Command Dashboard{" &middot; vs " + opp if opp else ""} &middot; {fresh}</div></div></div>'
        f'<div style="display:flex;align-items:center;">{chips}</div>'
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

    return (
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

    cells = "".join(
        _pulse_cell(c, ctx, my_avgs, opp_avgs, my_std, opp_std, elapsed_frac, remaining_frac, has_proj)
        for c in matchup["categories"]
    )
    grid = (
        f'<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:4px;height:100%;'
        f'grid-auto-rows:1fr;">{cells}</div>'
    )
    cw = sum(1 for c in matchup["categories"] if c["result"] == "W")
    cl = sum(1 for c in matchup["categories"] if c["result"] == "L")
    ct = sum(1 for c in matchup["categories"] if c["result"] == "T")
    cls = ctx["classification"]
    pw = sum(1 for (r, _) in cls.values() if r == "W"); pl = sum(1 for (r, _) in cls.values() if r == "L"); pt = sum(1 for (r, _) in cls.values() if r == "T")
    close = sum(1 for (r, tier) in cls.values() if tier == "tossup")
    sub = (f'{cw}W&middot;{cl}L&middot;{ct}T &rarr; proj {pw}-{pl}-{pt}'
           + (f' &middot; &#9889;{close}' if close else ''))
    return _tile(f"Category Pulse", grid, flex=1.45, sub=sub)


def render_opponent(ctx):
    oi = ctx["opp_intel"]; matchup = ctx["matchup"]
    opp = matchup.get("opp_team", "") if matchup else ""
    if not opp:
        return _tile("Opponent", f'<div style="color:{MUTED};">No opponent set.</div>')
    logo = sd.fantasy_logo(ctx["team_logos"].get(" ".join(opp.split()), ""), size=18, team_name=opp)
    parts = [f'<div style="display:flex;align-items:center;gap:6px;margin-bottom:4px;">{logo}'
             f'<span style="color:{TEXT};font-weight:700;font-size:12px;">{opp}</span></div>']

    if oi:
        two = oi.get("two_start", [])
        two_s = ("".join(f'<span style="color:{PURPLE};font-weight:700;">{r.get("PlayerName")}</span>&#215;2 ' for r in two)) if two else ""
        parts.append(
            f'<div style="color:{MUTED};font-size:10px;margin-bottom:3px;">'
            f'<span style="color:{TEXT};font-weight:700;">{oi.get("n_starts",0)}</span> starts / '
            f'<span style="color:{TEXT};">{oi.get("n_starters",0)}</span> arms {two_s}</div>'
        )
        hot = oi.get("hot_hitters", [])
        if hot:
            rows = "".join(
                f'<div style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">'
                f'<span style="color:{TEXT};">{r.get("PlayerName")}</span> '
                f'<span style="color:{MUTED};">{_pos(r)}</span> '
                f'<span style="color:{GREEN};font-weight:700;">{_fv(ops,3)}</span></div>'
                for r, ops in hot
            )
            parts.append(f'<div style="color:{MUTED};font-size:10px;text-transform:uppercase;letter-spacing:.4px;margin:5px 0 2px;">Hot bats (recent OPS)</div>{rows}')

    # Opponent roto strengths / weaknesses (season category ranks)
    ocats, on = sd.category_ranks(ctx["roto"], opp)
    if ocats:
        best = sorted(ocats.items(), key=lambda kv: kv[1])[:3]
        worst = sorted(ocats.items(), key=lambda kv: -kv[1])[:3]
        b = " ".join(f'<span style="color:{GREEN};">{_CAT_LABELS_MAP.get(c,c)}</span>' for c, _ in best)
        w = " ".join(f'<span style="color:{RED};">{_CAT_LABELS_MAP.get(c,c)}</span>' for c, _ in worst)
        parts.append(
            f'<div style="margin-top:4px;font-size:10px;line-height:1.4;">'
            f'<span style="color:{MUTED};">strong</span> {b}<br>'
            f'<span style="color:{MUTED};">weak</span> {w}</div>'
        )
    return _tile("Opponent This Matchup", "".join(parts))


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
        two = ' <span style="color:%s;font-weight:700;">&#215;2</span>' % PURPLE if _starts_this_week(r, datetime.now().strftime("%Y-%m-%d"), ctx["week_end_str"]) >= 2 else ""
        rows.append(
            f'<div style="display:flex;justify-content:space-between;gap:6px;padding:2px 0;white-space:nowrap;border-bottom:1px solid {BORDER};">'
            f'<span style="overflow:hidden;text-overflow:ellipsis;"><span style="color:{TEXT};font-weight:600;">{r.get("PlayerName")}</span>{two} '
            f'<span style="color:{MUTED};font-size:10px;">{dlabel} {hva}</span></span>'
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
    movers.sort(reverse=True)
    hot = movers[:3]; cold = movers[-3:][::-1] if len(movers) > 3 else []

    def line(d, r, r_ops, icon, col):
        hrp = _n(r.get("HR_Probability"))
        hr_s = f' <span style="color:{MUTED};font-size:9px;">HR{hrp*100:.0f}%</span>' if hrp > 0 else ""
        return (
            f'<div style="display:flex;justify-content:space-between;gap:5px;white-space:nowrap;padding:3.5px 0;border-bottom:1px solid {BORDER};">'
            f'<span style="overflow:hidden;text-overflow:ellipsis;color:{TEXT};">{icon} {r.get("PlayerName")} '
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
    return _tile("Hitting Hot / Cold", "".join(rows), sub="7-day OPS vs season")


def render_holes(ctx):
    ranked = sorted([p for p in ctx["pos_data"] if p.get("rank")], key=lambda p: -(p["rank"] or 0))
    rows = []
    for p in ranked[:3]:
        worst = p.get("worst_player"); fa = (p.get("top_fa") or [None])[0]
        wname = worst.get("PlayerName", "—") if worst else "—"
        wsc = int(worst.get("_pscore", 0)) if worst else 0
        fa_s = ""
        if fa:
            fsc = int(fa.get("_pscore", 0))
            gain = fsc - wsc
            fa_s = (f' &rarr; <span style="color:{GREEN if gain>0 else MUTED};">{fa.get("PlayerName")}</span> {_mini_badge(fsc)}')
        rows.append(
            f'<div style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;padding:2px 0;border-bottom:1px solid {BORDER};">'
            f'<span style="color:{YELLOW};font-weight:700;">{p["pos"]}</span> '
            f'<span style="color:{MUTED};font-size:10px;">#{p["rank"]}/{p["n_teams"]}</span> '
            f'<span style="color:{TEXT};">{wname}</span> {_mini_badge(wsc)}{fa_s}</div>'
        )
    holes = "".join(rows) if rows else f'<div style="color:{MUTED};">Balanced roster.</div>'
    watch = sd.build_bench_watch(ctx["lineup_eff_current"]) if ctx["lineup_eff_current"] else ""
    if watch:
        # tighten the reused callout for the compact tile
        watch = f'<div style="margin-top:5px;font-size:10.5px;">{watch}</div>'
    return _tile("Weakest Spots &middot; Lineup Watch", holes + watch, flex=1.25, sub="rank / worst &rarr; best FA")


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
    def spline(r, sc, extra):
        return (f'<div style="display:flex;justify-content:space-between;gap:6px;white-space:nowrap;padding:2px 0;border-bottom:1px solid {BORDER};">'
                f'<span style="overflow:hidden;text-overflow:ellipsis;color:{TEXT};">{r.get("PlayerName")} '
                f'<span style="color:{MUTED};font-size:10px;">{_pos(r)}</span></span>'
                f'<span style="flex:0 0 auto;"><span style="color:{MUTED};font-size:10px;">{extra}</span> {_mini_badge(sc)}</span></div>')
    def hdr(t):
        return f'<div style="color:{ACCENT};font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;margin-top:4px;">{t}</div>'
    parts = [hdr("Starters")]
    for r in ctx["fa_sp"][:2]:
        qs = sd.qs_probability(r)
        parts.append(spline(r, r.get("_score", 0), f'{_n(r.get("ERA")):.2f} ERA &middot; QS{qs}%'))
    parts.append(hdr("Relievers"))
    for r in ctx["fa_rp"][:2]:
        parts.append(spline(r, r.get("_rp_score", 0), f'{int(_n(r.get("ESPN_SVHD")) or _n(r.get("SVHD")))} SV+H &middot; {_n(r.get("ERA")):.2f}'))
    parts.append(hdr("Hitters"))
    for r in ctx["fa_hit"][:2]:
        parts.append(spline(r, r.get("_score", 0), f'{_fv(_n(r.get("OPS")),3)} OPS'))
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

STYLE = """
  * { box-sizing:border-box; }
  html,body { margin:0; padding:0; height:100%; }
  body { background:#060b18; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
         color:#e2e8f0; overflow:hidden; }
  #wrap { position:fixed; inset:0; display:flex; flex-direction:column; gap:6px; padding:6px; }
  #grid { flex:1 1 auto; min-height:0; display:grid; grid-template-columns:1fr 1fr 1fr;
          grid-template-rows:minmax(0,1fr); gap:6px; }
  .col { display:flex; flex-direction:column; gap:6px; min-height:0; height:100%; }
"""


def build_dashboard(snap, my_team):
    ctx = build_context(snap, my_team)
    topbar = render_topbar(ctx)
    # Weakest Spots/Lineup Watch is content-heavy → give it the roomy col-1 bottom slot
    # (only 2 tiles share col 1). Opponent is lighter → it fits col 2's 3-tile stack.
    col1 = f'<div class="col">{render_category_pulse(ctx)}{render_holes(ctx)}</div>'
    col2 = f'<div class="col">{render_pitching(ctx)}{render_hitting(ctx)}{render_opponent(ctx)}</div>'
    col3 = f'<div class="col">{render_moves(ctx)}{render_fa_radar(ctx)}{render_season(ctx)}</div>'
    return (
        f'<!DOCTYPE html><html><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{my_team} — Dashboard</title><style>{STYLE}</style></head>'
        f'<body><div id="wrap">{topbar}<div id="grid">{col1}{col2}{col3}</div></div></body></html>'
    )


def main():
    ap = argparse.ArgumentParser(description="Single-viewport fantasy dashboard")
    ap.add_argument("--refresh", action="store_true", help="Refresh snapshot data first (~60s)")
    ap.add_argument("--team", default=None, help="Render another team's dashboard (needs all_matchups)")
    args = ap.parse_args()

    if args.refresh:
        import fetch_data
        fetch_data.main()

    with open(SNAPSHOT, encoding="utf-8") as f:
        snap = json.load(f)

    my_team = args.team or snap.get("my_team", MY_TEAM)
    html = build_dashboard(snap, my_team)

    PREVIEWS.mkdir(exist_ok=True)
    slug = my_team.strip().replace(" ", "_")
    out = PREVIEWS / f"dashboard_{slug}.html"
    out.write_text(html, encoding="utf-8")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
