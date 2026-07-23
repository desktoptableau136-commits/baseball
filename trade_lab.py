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
    BG, SURFACE, SURFACE2, BORDER, TEXT, MUTED, ACCENT, GREEN, RED, YELLOW, PURPLE,
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

    # Scoring calibration + percentile pools + recent-form indexes — the SAME shared
    # send_digest helpers build_email uses, so every number (incl. _tval) matches.
    hit_pctile, pit_pctile = sd.prepare_scoring(pitchers, hitters)
    idx = sd.build_recent_indexes(pitchers, hitters,
                                  snap.get("recent_pitching", []), snap.get("recent_hitting", []))
    best_recent_p, best_recent_h = idx["best_recent_p"], idx["best_recent_h"]

    # Per-team category ranks → needs/surplus derived via the shared overlay-aware helper
    # (sd._team_trade_context, per team) inside the loop below.
    ranks, n = sd.team_category_ranks(roto)

    # Team roster ordered by standings; keep the double-space snapshot keys for matching.
    team_keys = [_key(s.get("team_name")) for s in standings] or sorted(ranks.keys())
    team_logos = {_key(s.get("team_name")): s.get("logo_url", "") for s in standings}

    teams_meta, players, pos_data_by_team, hit_agg = {}, {}, {}, {}
    for tk in team_keys:
        # Thin HITTER positions for this team → {pos: my_avg_score} (positional need).
        pos_data = sd.positional_breakdown(pitchers, hitters, tk, best_recent_p, best_recent_h)
        pos_data_by_team[tk] = pos_data   # reused by the Partner Fit board (per-POV engine run)
        pos_rank = {}
        for p in pos_data:
            # League rank at this position/role (1 = best crew) → collapsed-section gauge.
            if p.get("rank") and p.get("n_teams"):
                pos_rank[p["pos"]] = {"rank": p["rank"], "n": p["n_teams"]}
        # Need model — the SAME overlay-aware source find_trades + the pending verdict use, so the
        # LIVE Trade Lab verdict grades against the exact needs the Partner-Fit board generated on
        # (punt SVHD, target C/SS). Without this the board could headline a deal the builder then
        # DECLINEs. need_pos → {pos: my_avg_score} for the JS upgrade gate; surplus_pos → list.
        _needs, _surplus, _need_pos_t, _surplus_pos = sd._team_trade_context(tk, ranks, n, pos_data)
        need_pos    = {p: round(v[1], 1) for p, v in _need_pos_t.items()}
        surplus_pos = sorted(_surplus_pos)
        # Aggregate lineup-hitting score = Σ starter-avg × slots — ranked across teams
        # after the loop → the Hitters role-header "Xth-best hitters" gauge. Same my_avg
        # values shown per position, so a folded Hitters section summarizes its parts.
        hit_agg[tk] = sum((p.get("my_avg") or 0) * sd.POS_STARTERS.get(p["pos"], 1)
                          for p in pos_data if p.get("ptype") == "hit")
        teams_meta[tk] = {
            "name":     _disp(tk),
            "logo":     sd.fantasy_logo(team_logos.get(tk, ""), 24, tk),
            "needs":    sorted(_needs),
            "surplus":  sorted(_surplus),
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

    # Rank teams by aggregate lineup-hitting score → each team's overall Hitters rank.
    _hn = len(hit_agg)
    for i, (tk, _) in enumerate(sorted(hit_agg.items(), key=lambda kv: -kv[1])):
        teams_meta[tk]["hit_rank"] = {"rank": i + 1, "n": _hn}

    # Partner Fit board: one engine-graded deal per rival, from EVERY team's POV so the
    # board stays correct when the LEFT dropdown switches. See build_partner_fit.
    partner_fit = build_partner_fit(pitchers, hitters, roto, team_keys, ranks, n,
                                    best_recent_p, best_recent_h, hit_pctile, pit_pctile,
                                    pos_data_by_team)
    # Consolidation megadeals (give depth, get fewer-but-better) — a separate strip ABOVE the
    # per-rival board. Win-win only; keyed by POV like partner_fit so the LEFT dropdown stays live.
    mega_deals = build_megadeal_board(pitchers, hitters, roto, team_keys, ranks, n,
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
        "megaDeals": mega_deals,
        "myTeam":    my_key,
        "catLabels": CAT_LABELS,
        "lowerBetter": sorted(sd._LOWER_BETTER),
        "posStarters": {p: sd.POS_STARTERS.get(p, 1) for p in ("C","1B","2B","3B","SS","OF")},
        "posSlack":  sd._POS_DEPTH_SLACK,   # redundancy guard: bench/flex bodies allowed beyond starters
        # Acceptance-model tuning baked from send_digest so the Lab JS can't drift from the digest:
        # graduated star reluctance + aggressive realistic band + demand-side need multiplier.
        "tune": {
            "starTvalFloor": sd._STAR_TVAL_FLOOR, "starTvalSlope": sd._STAR_TVAL_SLOPE,
            "starPremCap": sd._STAR_PREM_CAP, "realisticMax": sd._TRADE_REALISTIC_MAX,
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
        # Season skill (QS / K+) first, then the risk flags (blowup ⚠ / regression $ ▼).
        badges    = sd.sp_skill_badges(r) + sd.blowup_badge(r) + sd.pitcher_regression_badge(r)
        breakdown = sd._pitcher_score_breakdown(r, best_recent_p) + sd._sp_skill_context(r)
    else:
        badges    = sd.pitcher_regression_badge(r)
        breakdown = sd._pitcher_score_breakdown(r, best_recent_p)
    badges += sd._il_badge(r)   # injury chip — explains the _tval discount (mirrors the digest cards)
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
        "tvalStar":  round(_n(r.get("_tval_star", r.get("_tval"))), 3),   # star-reach premium basis (healthy unless severe IL)
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


def _fit_deal_words(value_phrase, accept, rival_gains=True):
    """Plain-English one-liner for a graded deal → (sentence, tier_key). The deal is already
    filtered to ACCEPT-for-me (good for my side), so BEST is reserved for a true win-win: the
    rival ALSO clearly gains (rival_gains) at a realistic price. Otherwise it's a REACH — worth
    floating, but the rival may resist."""
    if accept == "realistic" and rival_gains:
        if value_phrase == "you pay up":
            return ("You pay a hair, but a fair ask for a need — they gain too.", "BEST")
        return ("Fair swap — a win for both sides.", "BEST")
    # good for me, but the rival wins little or nothing → they'll likely push back
    if value_phrase == "you win the value":
        return ("You come out ahead, but it's an aggressive ask — expect a counter.", "REACH")
    return ("Worth floating, but the value's thin for them — expect some back-and-forth.", "REACH")


def _fit_get_tags(ins, my_needs):
    """Short need tags an incoming player fills: thin positions (C/SS/...) + my need cats."""
    tags = []
    for p in ins:
        # sorted iteration: _tfillpos/_tcats are set-derived (salted order), and these tags
        # get JSON-serialized into the Partner-Fit board — sort each group so the render is
        # process-stable while keeping positions-before-cats grouping.
        for pos in sorted(p.get("_tfillpos") or []):
            if pos not in tags:
                tags.append(pos)
        for c in sorted(p.get("_tcats") or []):
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
        sd._set_trade_caps(400, 6)   # facade-safe: lands in fantasy.trades where find_trades reads it
        for pov in team_keys:
            # Overlay-aware pov needs (punt SVHD, target C/SS) — the SAME model the engine + the
            # live verdict use, so the board's diagnosis + tags can't drift from the builder.
            my_needs, my_surplus, _pov_np, _ = sd._team_trade_context(
                pov, ranks, n, pos_data_by_team.get(pov, []))
            deals = sd.find_trades_combined(pitchers, hitters, roto, pov, best_recent_p,
                                            best_recent_h, pos_data_by_team.get(pov, []),
                                            hit_pctile, pit_pctile, cards=400,
                                            pos_data_by_team=pos_data_by_team)
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
                # Only deals that are ACCEPT for MY side can be a "target" — a deal that's good for
                # them but not for me (I overpay / fill no need) is exactly what made the board
                # headline a deal the builder then DECLINEd. Fall through to a diagnosis instead.
                cand = [d for d in by_team.get(rival, []) if d.get("my_verdict") == "ACCEPT"]

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
                    # BEST is a true win-win: realistic price AND the rival CLEARLY gains (not just
                    # breaks even). A good-for-me-only deal drops to REACH ("worth a shot").
                    rival_gains = _n(best.get("net_them")) >= sd._TRADE_RIVAL_GAIN_MIN
                    words, tier = _fit_deal_words(vp, ac, rival_gains)
                    get = [{"name": p.get("PlayerName", ""),
                            "tags": _fit_get_tags([p], my_needs)} for p in best["ins"]]
                    records.append({
                        "team": rival, "tier": tier,
                        "get": get, "give": [p.get("PlayerName", "") for p in best["outs"]],
                        "verdict": words,
                        # Name only the cats the players I'd ACTUALLY send cover for them (the deal's
                        # own send_cats), not the roster-wide surplus∩needs intersection — so the
                        # "you're deep in X (their needs)" prose can't overclaim.
                        "whyOffer": [CAT_LABELS.get(c, c) for c in best.get("send_cats", [])],
                        "whyGet": _fit_get_tags(best["ins"], my_needs),
                    })
                elif not i_offer and not they_offer:
                    records.append({"team": rival, "tier": "NOFIT",
                        "why": "Category twins — you share the same strengths and the same needs. "
                               "Nothing to arbitrage."})
                elif not i_offer:
                    records.append({"team": rival, "tier": "ONEWAY",
                        "why": "They have pieces you'd want, but they're strong everywhere you are — "
                               "you can't fill a need of theirs, so you'd overpay."})
                else:
                    records.append({"team": rival, "tier": "SLIM",
                        "why": "Some overlap on paper, but no clean, near-even deal came together. "
                               "Worth a manual look."})

            records.sort(key=lambda r: (_FIT_TIER_ORDER[r["tier"]], r["team"]))
            out[pov] = records
    finally:
        sd._set_trade_caps(*_save)
    return out


def build_megadeal_board(pitchers, hitters, roto, team_keys, ranks, n,
                         best_recent_p, best_recent_h, hit_pctile, pit_pctile, pos_data_by_team):
    """{pov_key: [up to 2 consolidation-megadeal records]} — the Trade Lab's megadeal strip.

    A megadeal (N-for-M, |give| >= |get|, one side >= 3) is the multi-player win-win a manager
    builds by hand: give roster depth, get fewer-but-better need-fillers (a scarce C/SS upgrade).
    The base engine can't form these (2-per-side cap, raw-value gate), so this runs the dedicated
    sd.find_megadeals path — WIN-WIN only — from EVERY team's POV (so the strip stays correct when
    the LEFT dropdown switches). Each record mirrors the Partner-Fit ACTIONABLE shape (tier MEGA)
    so the client renders it with the SAME fitCard markup + "Build this" (loadDeal already handles
    N players)."""
    out = {}
    for pov in team_keys:
        # Overlay-aware pov needs (punt SVHD, target C/SS) — the SAME model the engine uses — so the
        # get-tags/why prose can't drift from what find_megadeals actually built the deal around.
        my_needs, _my_surplus, _np, _ = sd._team_trade_context(
            pov, ranks, n, pos_data_by_team.get(pov, []))
        deals = sd.find_megadeals(pitchers, hitters, roto, pov, best_recent_p, best_recent_h,
                                  pos_data_by_team.get(pov, []), hit_pctile, pit_pctile,
                                  limit=2, pos_data_by_team=pos_data_by_team)
        recs = []
        for d in deals:
            get = [{"name": p.get("PlayerName", ""), "tags": _fit_get_tags([p], my_needs)}
                   for p in d["ins"]]
            # "Why it's a blockbuster" spark — sells the excitement: how many depth pieces roll up
            # into how many difference-makers, how many needs it fixes at once, and that they win too.
            n_out, n_in = len(d["outs"]), len(d["ins"])
            n_needs = len(d.get("get_cats", [])) + len(d.get("get_pos", []))
            spark = (f"Rolls {n_out} depth pieces into {n_in} difference-maker"
                     f"{'s' if n_in != 1 else ''} — fixes "
                     f"{n_needs} need{'s' if n_needs != 1 else ''} in one move, and they still win too.")
            recs.append({
                "team": d["team"], "tier": "MEGA",
                "get": get, "give": [p.get("PlayerName", "") for p in d["outs"]],
                "verdict": "Blockbuster win-win — you give depth, get fewer-but-better.",
                "spark": spark,
                # Cats the players I'd ACTUALLY send cover for them (the deal's own send_cats).
                "whyOffer": [CAT_LABELS.get(c, c) for c in d.get("send_cats", [])],
                "whyGet": _fit_get_tags(d["ins"], my_needs),
            })
        out[pov] = recs
    return out


# ══════════════════════════════════════════════════════════════════════════════
# RENDER — static shell + embedded JSON + the selection/verdict JavaScript.
# ══════════════════════════════════════════════════════════════════════════════

def build_html(data):
    blob = json.dumps(data).replace("</", "<\\/")   # </script> safety
    css = _CSS.format(BG=BG, SURFACE=SURFACE, SURFACE2=SURFACE2, BORDER=BORDER,
                      TEXT=TEXT, MUTED=MUTED, ACCENT=ACCENT, GREEN=GREEN, RED=RED, YELLOW=YELLOW,
                      PURPLE=PURPLE)
    js = _JS.format(GREEN=GREEN, RED=RED, YELLOW=YELLOW, ACCENT=ACCENT,
                    MUTED=MUTED, TEXT=TEXT, BORDER=BORDER, SURFACE2=SURFACE2, PURPLE=PURPLE)
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
      <div class="hsub">Pick two teams, click players to build a deal, watch it get graded live. &#127919; = fills your need &middot; <span style="color:{YELLOW};font-weight:800">&#9656;</span> send (your surplus they value more) &middot; <span style="color:{GREEN};font-weight:800">&#9666;</span> grab (their surplus you value more).</div>
    </div>
    <div class="headright">
      <div class="fresh" title="Snapshot refresh time — rerun with --refresh to update"><span class="dot" style="background:{fresh_color}"></span><span>Data: {fresh_label}</span></div>
      {refresh_btn}
    </div>
  </div>
  <details id="fitboard" open>
    <summary class="fbsum">
      <div class="fbhead"><span class="fbtitle">Who should you be trading with?</span><span class="fbtoggle">Targets</span></div>
      <div class="fbsub" id="fbneeds"></div>
    </summary>
    <div class="fblegend">
      <span><b style="color:{GREEN}">BEST TARGET</b> &mdash; realistic deal, lands a need</span>
      <span><b style="color:{YELLOW}">WORTH A SHOT</b> &mdash; good deal, aggressive ask</span>
      <span><b style="color:{MUTED}">SLIM / ONE-WAY / NO DEAL</b> &mdash; why not</span>
    </div>
    <div id="megawrap" style="display:none;">
      <div class="megahdr">&#128171; Blockbuster deals
        <span class="megasub">give roster depth, get fewer-but-better &mdash; the multi-player win-win</span></div>
      <div class="megalist" id="megalist"></div>
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
        <div class="rail give"><div class="lhead give-h">&#9660; YOU GIVE</div><div id="giveList" class="llist"></div><div id="giveSub"></div></div>
        <div class="rail get"><div class="lhead get-h">&#9650; YOU GET</div><div id="getList" class="llist"></div><div id="getSub"></div></div>
      </div>
      <div id="fairbar"></div>
      <div id="dealsum"></div>
      <div id="reads"></div>
      <details id="vdetail"><summary>Value detail</summary><div id="vdetailBody"></div></details>
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
#cols {{ display:grid; grid-template-columns:3fr 4fr 3fr; gap:14px; align-items:start; }}
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
.possubhdr {{ display:flex; align-items:center; gap:6px; margin:8px 4px 3px 10px; cursor:pointer; user-select:none; }}
.possubhdr:hover {{ background:{SURFACE2}; border-radius:6px; }}
.posname {{ font-size:10px; font-weight:800; letter-spacing:.5px; color:{TEXT}; }}
.poscount {{ font-size:9px; font-weight:700; color:{MUTED}; }}
.ranktxt {{ margin-left:auto; font-size:10.5px; font-weight:800; letter-spacing:.3px; }}
.prow {{ padding:6px 8px; border-radius:7px; cursor:pointer; border:1px solid transparent; margin-bottom:2px; }}
.prow:hover {{ background:{SURFACE2}; }}
.prow.sel {{ background:rgba(59,130,246,.14); border-color:{ACCENT}; }}
/* Target = need-fit (🎯 icon). Edge is a SOFT WHITE hint — green is reserved for the arb
   marker below, so a green edge always means "value edge", never merely "fills a need". */
.prow.target {{ box-shadow:inset 3px 0 0 rgba(226,232,240,.28); }}
/* Value-asymmetry ("arb") marker — a glyph-first signal DISTINCT from 🎯 target (need-fit)
   and from the $/▼ luck badges. Amber ▸ = your surplus they value more (send); green ◂ =
   their surplus you value more (grab). The GREEN edge belongs to grab; it overrides the
   soft-white target edge when a partner player is BOTH a target and a grab. */
.arb {{ font-weight:900; cursor:help; flex:0 0 auto; letter-spacing:-1px; }}
.arb.bait {{ color:{YELLOW}; }}
.arb.grab {{ color:{GREEN}; }}
.prow.abait {{ box-shadow:inset -3px 0 0 {YELLOW}; }}   /* left panel — send edge */
.prow.agrab {{ box-shadow:inset 3px 0 0 {GREEN}; }}     /* right panel — grab edge (wins over target) */
.prow.strong.abait {{ background:linear-gradient(90deg,transparent,rgba(245,158,11,.10)); }}
.prow.strong.agrab {{ background:linear-gradient(90deg,rgba(34,197,94,.10),transparent); }}
.prow-top {{ display:flex; align-items:center; gap:6px; }}
.pname {{ font-weight:700; font-size:13px; }}
.poschip {{ display:inline-block; font-size:9px; font-weight:800; letter-spacing:.4px; color:{TEXT}; background:{SURFACE2}; border:1px solid {BORDER}; border-radius:4px; padding:1px 4px; vertical-align:middle; }}
.tgt {{ font-size:12px; cursor:help; }}
.pill {{ margin-left:auto; font-size:11px; font-weight:800; padding:1px 7px; border-radius:9px; color:#0b1220; cursor:pointer; }}
.pstat {{ color:{MUTED}; font-size:11px; margin-top:1px; }}
.bd {{ display:none; margin-top:5px; padding:7px 8px; background:{SURFACE2}; border:1px solid {BORDER}; border-radius:6px; font-size:11.5px; line-height:1.5; color:{TEXT}; }}
.bd.open {{ display:block; }}
#mid {{ position:sticky; top:12px; min-width:0; background:{SURFACE}; border:1px solid {BORDER}; border-radius:10px; padding:14px; }}
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
.litem {{ font-size:12px; padding:3px 0; display:flex; align-items:center; gap:5px; min-width:0; }}
.litem .x {{ color:{MUTED}; cursor:pointer; font-weight:800; }}
.totrow {{ display:flex; justify-content:space-between; font-size:11px; color:{MUTED}; margin-top:8px; padding-top:8px; border-top:1px solid {BORDER}; }}
/* calm two-rail ledger */
.rail {{ min-width:0; }}
.litem .lname {{ cursor:pointer; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
.litem .lv {{ margin-left:auto; color:{MUTED}; font-weight:700; font-variant-numeric:tabular-nums; cursor:pointer; }}
.litem .arb {{ margin-left:4px; }}
.litem .lv + .x {{ margin-left:6px; }}
.lsub {{ display:flex; justify-content:space-between; font-size:11px; color:{MUTED}; margin-top:5px; padding-top:5px; border-top:1px solid {BORDER}; }}
.lsub b {{ color:{TEXT}; font-weight:700; font-variant-numeric:tabular-nums; }}
/* per-player value story (tap a name or value to reveal) */
.vstory {{ background:{BG}; border:1px solid {BORDER}; border-left:3px solid {ACCENT}; border-radius:6px; padding:8px 10px; margin:2px 0 5px; font-size:11px; line-height:1.5; }}
.vstory .vs-base {{ color:#c4cdda; margin-bottom:5px; }}
.vstory .vs-base b {{ color:{TEXT}; font-variant-numeric:tabular-nums; }}
.vstory .vs-ln {{ display:flex; gap:8px; align-items:baseline; padding:3px 0 0; }}
.vstory .who {{ flex:0 0 auto; min-width:70px; color:{MUTED}; font-weight:700; }}
.vstory .val {{ font-weight:800; font-variant-numeric:tabular-nums; }}
.vstory .val.up {{ color:{GREEN}; }} .vstory .val.dn {{ color:{RED}; }}
.vstory .vs-rz {{ color:{MUTED}; padding:1px 0 3px 8px; line-height:1.4; }}
/* fairness bar */
#fairbar {{ margin-top:14px; }}
.btrack {{ position:relative; height:9px; border-radius:6px; overflow:hidden; display:flex; border:1px solid {BORDER}; }}
.bgive {{ background:linear-gradient(90deg,rgba(239,68,68,.55),rgba(239,68,68,.30)); }}
.bget {{ background:linear-gradient(90deg,rgba(34,197,94,.30),rgba(34,197,94,.55)); }}
.bmid {{ position:absolute; left:50%; top:-2px; bottom:-2px; width:2px; background:{MUTED}; opacity:.55; }}
.blabels {{ display:flex; justify-content:space-between; font-size:10.5px; margin-top:5px; color:{MUTED}; font-variant-numeric:tabular-nums; }}
.blabels b {{ color:{TEXT}; font-weight:700; }}
.btag {{ text-align:center; font-size:11px; color:{MUTED}; margin-top:5px; }}
#dealsum {{ }}
/* value-detail disclosure */
details#vdetail {{ margin-top:12px; padding-top:10px; border-top:1px solid {BORDER}; }}
details#vdetail > summary {{ list-style:none; cursor:pointer; user-select:none; font-size:10px; font-weight:800; letter-spacing:.8px; text-transform:uppercase; color:{ACCENT}; }}
details#vdetail > summary:hover {{ color:{TEXT}; }}
details#vdetail > summary::-webkit-details-marker {{ display:none; }}
details#vdetail > summary::before {{ content:'\\25B6'; margin-right:6px; color:{ACCENT}; }}
details#vdetail[open] > summary::before {{ content:'\\25BC'; }}
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
.megahdr {{ font-size:12.5px; font-weight:800; color:{PURPLE}; padding:12px 16px 0; letter-spacing:.2px; }}
.megasub {{ font-weight:600; color:{MUTED}; font-size:11px; margin-left:6px; letter-spacing:0; }}
.megalist {{ display:grid; grid-template-columns:1fr 1fr; gap:10px; padding:8px 16px 4px; }}
#megawrap {{ border-bottom:1px solid {BORDER}; }}
.fbcard {{ background:{SURFACE}; border:1px solid {BORDER}; border-radius:10px; padding:11px 13px; }}
.fbcard.dim {{ background:{SURFACE2}; opacity:.85; }}
.fbcard.fbmega {{ border-color:{PURPLE}; background-image:linear-gradient(180deg,rgba(168,85,247,0.07),rgba(168,85,247,0)); }}
.fbspark {{ font-size:10.5px; color:{PURPLE}; font-weight:600; margin-top:6px; line-height:1.4;
            background:rgba(168,85,247,0.10); border:1px solid {PURPLE}; border-radius:6px; padding:5px 8px; }}
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
  .possubhdr {{ margin:9px 6px 4px 8px; padding:3px 4px; }}   /* bigger fold tap target */
  .posname {{ font-size:11.5px; }}
  .ranktxt {{ font-size:12px; }}
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
var collapsedPos = {{ L:{{}}, R:{{}} }};  // side -> position -> bool; per-hitter-position fold state
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

// League-rank tag for a section header: at a glance "these are the #2 SP crew" /
// "these bats are 2nd-worst" is more contextual than the top player's score. Rendered
// as plain COLORED TEXT (not a pill) so it doesn't read like a player's score badge.
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
function rankText(rd, label) {{
  if (!rd || !rd.n) return '';
  var rank = rd.rank, n = rd.n, third = Math.max(1, Math.round(n / 3));
  var col = rank <= third ? '{GREEN}' : (rank >= n - third + 1 ? '{RED}' : '{YELLOW}');
  return '<span class="ranktxt" style="color:' + col + '" '
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

// One player row. `gkey` makes the DOM ids unique when a multi-eligible hitter is
// duplicated across position groups; selection stays in sync because toggle() keys off
// data-pid across EVERY copy on the side, not a single element id.
function playerRowHtml(side, p, gkey, myMeta, holderMeta, otherMeta) {{
  var on = picked[side][p.id] ? ' sel' : '';
  var pos = (p.posTokens || []).map(function(t) {{ return '<span class="poschip">' + t + '</span>'; }}).join(' ');
  if (pos) pos = ' ' + pos;
  var tgt = '', tgtCls = '';
  if (side === 'R') {{
    var tr = targetReasons(p, myMeta);
    if (tr.length) {{ tgt = ' <span class="tgt" title="Target &mdash; ' + tr.join('; ') + '">&#127919;</span>'; tgtCls = ' target'; }}
  }}
  // Arb marker — lives ALONGSIDE the target (different question: value edge, not need-fit).
  var m = arbMarker(p, holderMeta || {{}}, otherMeta || {{}}), arb = '', arbCls = '';
  if (m) {{
    var oName = side === 'L' ? ((otherMeta||{{}}).name || 'They') : 'You';
    arb = arbGlyph(side, m, arbReasons(p, holderMeta || {{}}, otherMeta || {{}}, oName));
    arbCls = (side === 'L' ? ' abait' : ' agrab') + (m.tier === 'strong' ? ' strong' : '');
  }}
  var bid = 'bd-' + side + '-' + gkey + '-' + p.id;
  return '<div class="prow' + on + tgtCls + arbCls + '" id="row-' + side + '-' + gkey + '-' + p.id + '" '
    + 'data-pid="' + p.id + '" data-side="' + side + '">'
    + '<div class="prow-top" onclick="toggle(\'' + side + '\',\'' + p.id + '\')">'
    + p.logo + '<span class="pname">' + p.name + '</span>' + pos + p.badges + tgt + arb
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
  // League-rank tags use the RENDERED side's own team meta (correct for either column).
  var meta = DATA.teamsMeta[tk] || {{}};
  var pr_rank = meta.pos_rank || {{}};
  // Arb markers judge THIS side's players (holder=meta) against the OTHER dropdown's team.
  var otherTk = document.getElementById(side === 'L' ? 'selR' : 'selL').value;
  var otherMeta = DATA.teamsMeta[otherTk] || {{}};
  var cs = collapsed[side] || (collapsed[side] = {{}});
  var cps = collapsedPos[side] || (collapsedPos[side] = {{}});
  var html = '';
  ['hit','sp','rp'].forEach(function(role) {{
    var rows = pl[role] || [];
    if (!rows.length) return;
    var fold = !!cs[role];
    // Role header is a tap target (fold/unfold) with a league-rank tag: SP/RP show the
    // positional-crew rank; Hitters show the AGGREGATE lineup-hitting rank (so a folded
    // Hitters section says "Xth-best hitters in the league" at a glance).
    var roleRank = role === 'hit' ? meta.hit_rank
                 : pr_rank[role === 'sp' ? 'SP' : 'RP'];
    html += '<div class="rolehdr" onclick="toggleSection(\'' + side + '\',\'' + role + '\')">'
      + '<span class="caret">' + (fold ? '&#9654;' : '&#9660;') + '</span>'
      + '<span class="rolelbl">' + ROLE_LABEL[role] + '</span>'
      + '<span class="rolecount">' + rows.length + '</span>'
      + rankText(roleRank, ROLE_LABEL[role])
      + '</div>';
    html += '<div class="secbody"' + (fold ? ' style="display:none"' : '') + '>';
    if (role === 'hit') {{
      var g = groupHitters(rows);
      // Each position sub-group is INDEPENDENTLY collapsible (its own fold state), so a
      // folded group still shows its league-rank tag — a quick per-slot strength scan.
      var emit = function(pos, pr, showRank) {{
        var pfold = !!cps[pos];
        html += '<div class="possubhdr" onclick="togglePos(\'' + side + '\',\'' + pos + '\')">'
          + '<span class="caret">' + (pfold ? '&#9654;' : '&#9660;') + '</span>'
          + '<span class="posname">' + pos + '</span>'
          + '<span class="poscount">' + pr.length + '</span>'
          + (showRank ? rankText(pr_rank[pos], pos) : '')
          + '</div>'
          + '<div class="possecbody"' + (pfold ? ' style="display:none"' : '') + '>';
        pr.forEach(function(p) {{ html += playerRowHtml(side, p, pos, myMeta, meta, otherMeta); }});
        html += '</div>';
      }};
      POS_GROUPS.forEach(function(pos) {{
        var pr = g.groups[pos];
        if (pr.length) emit(pos, pr, true);
      }});
      if (g.util.length) emit('UTIL', g.util, false);   // UTIL has no positional rank
    }} else {{
      rows.forEach(function(p) {{ html += playerRowHtml(side, p, role, myMeta, meta, otherMeta); }});
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

function togglePos(side, pos) {{
  var cps = collapsedPos[side] || (collapsedPos[side] = {{}});
  cps[pos] = !cps[pos];
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

// ── Value-asymmetry ("arb") marker + per-player value story ──────────────────
// A tune delta -> signed 2-dec label, e.g. 0.14 -> "+.14", -0.10 -> "-.10".
function _d(x) {{ return (x>=0?'+':'-') + Math.abs(x).toFixed(2).replace(/^0/,''); }}

// The ± components of needMult(p, meta) as plain-English strings — a faithful mirror of
// needMult's branches, so the story/tooltip can never drift from the value math.
function multParts(p, meta) {{
  var T=DATA.tune, cats=p.tcats||[], needs=meta.needs||[], surplus=meta.surplus||[], out=[];
  var nc = cats.filter(function(c){{ return needs.indexOf(c)>=0; }});
  nc.forEach(function(c){{ out.push({{s:1, t:_d(T.needCat)+' '+(DATA.catLabels[c]||c)+' need'}}); }});
  if (cats.length && !nc.length && cats.some(function(c){{ return surplus.indexOf(c)>=0; }}))
    out.push({{s:-1, t:_d(-T.needSurplus)+' only helps where deep'}});
  if (p.role==='hit') {{
    var groups=p.tgroups||[], needPos=meta.need_pos||{{}}, surplusPos=meta.surplus_pos||[];
    var fills=groups.filter(function(g){{ return g in needPos; }});
    if (fills.length) out.push({{s:1, t:_d(T.needPos)+' fills thin '+fills.join('/')}});
    else if (surplusPos.length && groups.length && groups.every(function(g){{ return surplusPos.indexOf(g)>=0; }}))
      out.push({{s:-1, t:_d(-T.needSurplus)+' stacks a deep slot'}});
  }}
  return out;
}}

// Does the OTHER team value him more than his HOLDER? (favorable to move inward.)
function arbMarker(p, holderMeta, otherMeta) {{
  var gap = needMult(p, otherMeta) - needMult(p, holderMeta);
  if (gap < DATA.tune.needCat) return null;
  return {{ tier: gap >= (DATA.tune.needCat + DATA.tune.needSurplus) ? 'strong' : 'mild' }};
}}

// Tooltip: why the other side values him more (+ a note when the holder is deep).
function arbReasons(p, holderMeta, otherMeta, otherName) {{
  var pos = multParts(p, otherMeta).filter(function(x){{ return x.s>0; }}).map(function(x){{ return x.t; }});
  var deep = multParts(p, holderMeta).some(function(x){{ return x.s<0; }});
  var s = (otherName||'They') + ' value him more: ' + (pos.join(' &#183; ') || 'fits their build');
  if (deep) s += ' &#183; you are deep here';
  return s;
}}

// The inward arrow glyph (L=amber send, R=green grab; doubled on a strong gap).
function arbGlyph(side, m, title) {{
  if (!m) return '';
  var strong = m.tier==='strong', cls = side==='L' ? 'bait' : 'grab';
  // HTML entities (not \\u escapes): _JS is a raw string, so \\u would survive literally.
  var g = side==='L' ? (strong?'&#9656;&#9656;':'&#9656;') : (strong?'&#9666;&#9666;':'&#9666;');
  return ' <span class="arb '+cls+'" title="'+title+'">'+g+'</span>';
}}

function openVs(id) {{ var e=document.getElementById(id); if(e) e.style.display = (e.style.display==='none'?'block':'none'); }}

// Per-player value story: base drivers, then the same player re-priced by each side's needs.
function valueStory(p) {{
  var myMeta=DATA.teamsMeta[document.getElementById('selL').value]||{{}};
  var partnerMeta=DATA.teamsMeta[document.getElementById('selR').value]||{{}};
  var pn = partnerMeta.name || 'them';
  var driv = (p.tcats||[]).map(function(c){{ return DATA.catLabels[c]||c; }}).join(', ') || 'role production';
  var posd = (p.role==='hit' && (p.tgroups||[]).length) ? ' &middot; ' + p.tgroups.join('/') : '';
  function line(lbl, meta) {{
    var m=needMult(p,meta), val=p.tval*m, parts=multParts(p,meta);
    var cls = m>1.001?'up':(m<0.999?'dn':'');
    var rz = parts.length ? parts.map(function(x){{ return x.t; }}).join(' &middot; ') : 'no change';
    return '<div class="vs-ln"><span class="who">'+lbl+'</span><span class="val '+cls+'">'+val.toFixed(2)+'</span></div>'
         + '<div class="vs-rz">'+rz+'</div>';
  }}
  return '<div class="vs-base"><b>Base '+p.tval.toFixed(2)+'</b> &mdash; strong in '+driv+posd+'</div>'
    + line('To you', myMeta) + line('To '+pn, partnerMeta);
}}

function ledgerItem(side, p) {{
  var myMeta=DATA.teamsMeta[document.getElementById('selL').value]||{{}};
  var partnerMeta=DATA.teamsMeta[document.getElementById('selR').value]||{{}};
  var holderMeta = side==='L' ? myMeta : partnerMeta;
  var otherMeta  = side==='L' ? partnerMeta : myMeta;
  var otherName  = side==='L' ? (partnerMeta.name||'They') : 'You';
  var m = arbMarker(p, holderMeta, otherMeta);
  var arb = arbGlyph(side, m, m ? arbReasons(p, holderMeta, otherMeta, otherName) : '');
  var vsId = 'vs-'+side+'-'+p.id;
  return '<div class="litem">' + p.logo
    + '<span class="lname" onclick="openVs(\'' + vsId + '\')">' + p.name + '</span>' + p.badges + arb
    + '<span class="lv" onclick="openVs(\'' + vsId + '\')">' + p.tval.toFixed(2) + '</span>'
    + '<span class="x" onclick="toggle(\'' + side + '\',\'' + p.id + '\')">&times;</span></div>'
    + '<div class="vstory" id="' + vsId + '" style="display:none">' + valueStory(p) + '</div>';
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

// JS mirror of send_digest._star_premium: graduated endowment premium keyed on trade VALUE
// (tval), NOT role score — 0 below the floor, rising per tval point, capped. Value-keyed so a
// vulture-win role player's inflated role score can't pose as a star and an elite closer earns
// real premium (relievers now comparable on one axis, so there's no role exclusion).
function starPremium(tval) {{
  var T = DATA.tune;
  return Math.max(0, Math.min(T.starPremCap, (tval - T.starTvalFloor) * T.starTvalSlope));
}}

// JS mirror of send_digest._deal_star_reach: would a rival balk at parting with prized players
// without a real overpay? Required overpay = SUM of premium across what they surrender (getArr,
// = what I acquire) minus the SUM across what they receive back (giveArr). Summing (not max)
// catches "two franchise players for one star + a role player". Reach (they balk) when that's
// positive AND I'm not paying up by at least it (net > -req). Drives the "Would they do it?" read.
function dealStarReach(getArr, giveArr, netVal) {{
  var surrender = 0, receive = 0;
  getArr.forEach(function(p) {{ surrender += starPremium(p.tvalStar != null ? p.tvalStar : p.tval); }});
  giveArr.forEach(function(p) {{ receive += starPremium(p.tvalStar != null ? p.tvalStar : p.tval); }});
  var req = Math.max(0, surrender - receive);
  return req > 0 && netVal > -req;
}}

// MY-side mirror (send_digest._deal_star_surrender): would *I* balk at parting with prized
// players without a real value win? Required premium = SUM across my give (giveArr) minus the
// SUM across my acquire (getArr). I hold out when that's positive AND I'm not winning by at
// least it (net < req). Same value-keyed, summed premium — drives "Would you do it?".
function dealStarSurrender(getArr, giveArr, netVal) {{
  var givePrem = 0, getPrem = 0;
  giveArr.forEach(function(p) {{ givePrem += starPremium(p.tvalStar != null ? p.tvalStar : p.tval); }});
  getArr.forEach(function(p) {{ getPrem += starPremium(p.tvalStar != null ? p.tvalStar : p.tval); }});
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
    ['giveSub','getSub','fairbar','dealsum','vdetailBody'].forEach(function(idv){{ var e=document.getElementById(idv); if(e) e.innerHTML=''; }});
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

  // OVERALL-DEAL check: the pill above answers "is this good FOR ME" — but a trade is a
  // two-sided negotiation, so an ACCEPT that the rival would flatly balk at (pfTier no/maybe)
  // isn't really landable as-is. Downgrade to COUNTER and say why, so this pill can't
  // contradict "Would they do it?" below it.
  if (label === 'ACCEPT' && pfTier !== 'yes' && pfTier !== 'na') {{
    label='COUNTER'; color='{YELLOW}';
    why = "good for you, but " + pfReason;
  }}

  vBox.innerHTML = '<span class="vpill" style="background:'+color+'">'+label+'</span>'
    + '<div class="vwhy">'+why+'</div>';
  setDealBar(lKeys.length, rKeys.length, netVal, label, color);

  // MY-side acceptance — the mirror of "Would they do it?". A star surrender at par is the read
  // that makes an even deal one *I* should hold out on, no matter how well the categories fit.
  // Trap (timing) is checked here too so this line can't disagree with the pill above, which
  // also gates on trap.
  var yfTier, yfReason;
  if (!lKeys.length && !rKeys.length) {{
    yfTier = 'na'; yfReason = '&mdash;';
  }} else if (starSurrender) {{
    yfTier = 'no'; yfReason = 'you\'d ship your star at even value &mdash; hold out for more';
  }} else if (leavesMeShort.length) {{
    yfTier = 'no'; yfReason = 'it leaves you without a backup at ' + leavesMeShort.join(', ') + ' &mdash; get a replacement first';
  }} else if (trap && !(needFilled.length || posList.length)) {{
    yfTier = 'no'; yfReason = 'the timing is a trap &mdash; you\'d be selling a riser or buying a regressor';
  }} else if ((needFilled.length || posList.length) && trap) {{
    yfTier = 'maybe';
    yfReason = 'it fills a need (' + needFilled.map(function(c){{return DATA.catLabels[c]||c;}}).join(', ')
             + (posList.length ? (needFilled.length ? ', ' : '') + posList.join(', ') + ' slot' : '')
             + '), but the timing is a trap';
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
  // Per-rail subtotals.
  document.getElementById('giveSub').innerHTML = lKeys.length ? '<div class="lsub"><span>give</span><b>'+giveVal.toFixed(2)+'</b></div>' : '';
  document.getElementById('getSub').innerHTML  = rKeys.length ? '<div class="lsub"><span>get</span><b>'+getVal.toFixed(2)+'</b></div>' : '';

  // Fairness bar — one glance at who pays up on paper (BASE value, the neutral yardstick).
  var tot = giveVal + getVal, gpct = tot>0 ? Math.round(giveVal/tot*100) : 50;
  var btag = netVal > 0.1 ? 'A shade your way &mdash; you come out ahead on paper'
           : (netVal < -0.1 ? 'You pay up a touch on paper' : 'Roughly even on paper');
  document.getElementById('fairbar').innerHTML =
      '<div class="btrack"><div class="bgive" style="flex:0 0 '+gpct+'%"></div>'
    + '<div class="bget" style="flex:0 0 '+(100-gpct)+'%"></div><div class="bmid"></div></div>'
    + '<div class="blabels"><span>give <b>'+giveVal.toFixed(2)+'</b></span><span>get <b>'+getVal.toFixed(2)+'</b></span></div>'
    + '<div class="btag">'+btag+'</div>';

  // Plain-English takeaway.
  document.getElementById('dealsum').innerHTML = '<div class="dealsum">' + dealSummary(netVal, netMe, netThem, addressesNeed) + '</div>';

  // Resting reads — just the two acceptance lines; everything numeric moves to Value detail.
  reads.innerHTML = accLine('Would they do it?', pfTier, pfReason) + accLine('Would you do it?', yfTier, yfReason);

  // Value detail (collapsed): the 3-POV matrix + category gain/lose chips.
  document.getElementById('vdetailBody').innerHTML =
      '<div class="valgrid" style="margin-top:8px">'
        + '<div class="vgh"></div><div class="vgh">Give</div><div class="vgh">Get</div><div class="vgh">Net</div>'
        + _vrow('Base', giveVal, getVal, netVal, 'Universal value (tval) &mdash; ' + tilt)
        + _vrow('My value', myGive, myGet, netMe, 'Re-valued by your roster needs')
        + _vrow('Their value', thGet, thGive, netThem, (partnerMeta.name||'Their') + ' give/get, re-valued by their needs')
      + '</div>'
    + '<div class="readline" style="margin-top:8px"><span class="readlbl">You gain:</span> ' + gainChips + (posChips?' &nbsp; '+posChips:'') + '</div>'
    + '<div class="readline"><span class="readlbl">You lose:</span> ' + loseChips + '</div>';
}}

function clearAll() {{
  picked = {{ L:{{}}, R:{{}} }};
  renderRoster('L'); renderRoster('R'); recompute();
}}

// ── Partner Fit board ─────────────────────────────────────────────────────────
// Renders DATA.partnerFit[leftTeam] — one engine-graded deal per rival, tiered by
// how landable it is. "Build this" drops the deal into the builder below.
var FIT_TIER = {{ BEST:['{GREEN}','BEST TARGET'], REACH:['{YELLOW}','WORTH A SHOT'],
                  MEGA:['{PURPLE}','&#128171; BLOCKBUSTER'],
                  SLIM:['{MUTED}','SLIM'], ONEWAY:['#ea580c','ONE-WAY'], NOFIT:['{RED}','NO DEAL'] }};

function escAttr(s) {{ return String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }}
function fitTag(t) {{ return '<span class="fbntag">' + t + '</span>'; }}

function fitCard(r) {{
  var t = FIT_TIER[r.tier] || FIT_TIER.SLIM, col = t[0], lbl = t[1];
  var meta = DATA.teamsMeta[r.team] || {{ name:r.team }};
  var head = '<div class="fbchead"><span class="fbvchip" style="color:' + col + ';border-color:' + col + '">'
           + lbl + '</span><span class="fbteam">' + meta.name + '</span>';
  if (r.tier === 'BEST' || r.tier === 'REACH' || r.tier === 'MEGA') {{
    var giveNames = (r.give || []).join(',');
    var getNames  = (r.get || []).map(function(g) {{ return g.name; }}).join(',');
    head += '<span class="fbbuild" data-partner="' + escAttr(r.team) + '" data-give="' + escAttr(giveNames)
          + '" data-get="' + escAttr(getNames) + '" onclick="loadDeal(this)">Build this &#9654;</span></div>';
    var getParts = (r.get || []).map(function(g) {{
      return '<b>' + g.name + '</b> ' + (g.tags || []).slice(0,2).map(fitTag).join(' ');
    }}).join('  ');
    var deal = '<div class="fbdeal"><span class="fbget">Get</span> ' + getParts
             + '<div class="fbgive"><span class="fbgv">for</span> ' + (r.give || []).join(' + ') + '</div></div>';
    var vcol = (r.tier === 'BEST' || r.tier === 'MEGA') ? '{GREEN}' : '{YELLOW}';
    var verdict = '<div class="fbverdict" style="color:' + vcol + '">' + r.verdict + '</div>';
    var why = '';
    if ((r.whyOffer && r.whyOffer.length) || (r.whyGet && r.whyGet.length)) {{
      var off = (r.whyOffer && r.whyOffer.length) ? r.whyOffer.join('/') : 'what they lack';
      var got = (r.whyGet && r.whyGet.length) ? r.whyGet.join('/') : 'a need of yours';
      why = '<div class="fbwhy"><span class="wl">Why it works:</span> you\'re deep in ' + off
          + ' (their needs); they can spare the ' + got + ' you need.</div>';
    }}
    // Blockbuster spark — the extra "why it's exciting" line, only on MEGA cards.
    var spark = (r.tier === 'MEGA' && r.spark)
              ? '<div class="fbspark">&#128171; ' + r.spark + '</div>' : '';
    return '<div class="fbcard' + (r.tier === 'MEGA' ? ' fbmega' : '') + '">'
         + head + deal + verdict + spark + why + '</div>';
  }}
  head += '</div>';
  return '<div class="fbcard dim">' + head
       + '<div class="fbwhy" style="margin-top:0;color:{MUTED}">' + (r.why || '') + '</div></div>';
}}

function renderFitBoard() {{
  var myTk = document.getElementById('selL').value;
  var meta = DATA.teamsMeta[myTk] || {{ needs:[], need_pos:{{}}, name:myTk }};
  var hEl = document.getElementById('fbneeds');
  if (hEl) {{
    var needs = (meta.needs || []).map(function(c) {{ return DATA.catLabels[c] || c; }});
    var pos = Object.keys(meta.need_pos || {{}});
    var bits = [];
    if (needs.length) bits.push('needs <b class="w">' + needs.join(', ') + '</b>');
    if (pos.length)   bits.push('thin at <b class="w">' + pos.join(', ') + '</b>');
    hEl.innerHTML = (meta.name || myTk) + (bits.length ? ' &middot; ' + bits.join(' &middot; ') : '');
  }}
  var recs = (DATA.partnerFit || {{}})[myTk] || [];
  var list = document.getElementById('fblist');
  if (list) list.innerHTML = recs.map(fitCard).join('') || '<div class="empty">No rivals.</div>';
  // Consolidation megadeal strip (above the per-rival board) — shown only when a win-win exists.
  var megas = (DATA.megaDeals || {{}})[myTk] || [];
  var ml = document.getElementById('megalist'), mw = document.getElementById('megawrap');
  if (ml) ml.innerHTML = megas.map(fitCard).join('');
  if (mw) mw.style.display = megas.length ? '' : 'none';
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
  var fb = document.getElementById('fitboard');
  if (fb) fb.open = false;   // fold the "who to trade with" board once a deal is loaded
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

// The HOSTED build (GitHub Pages / pocket) opens fully folded — every roster role
// section AND hitter position sub-group starts collapsed so the page is a short list
// of headers you expand as you want, on ANY device. Keyed off DATA.refreshUrl (baked
// only into the --refresh-url pocket build), so local desktop dev runs stay expanded.
// Seeded BEFORE the first render; taps still toggle live.
function pocketCollapseDefaults() {{
  if (!DATA.refreshUrl) return;
  ['L','R'].forEach(function(side) {{
    var cs = collapsed[side] || (collapsed[side] = {{}});
    ['hit','sp','rp'].forEach(function(role) {{ cs[role] = true; }});
    var cps = collapsedPos[side] || (collapsedPos[side] = {{}});
    POS_GROUPS.concat(['UTIL']).forEach(function(pos) {{ cps[pos] = true; }});
  }});
  coachFold = true;          // Deal Coach starts folded too
  var fb = document.getElementById('fitboard');
  if (fb) fb.open = false;   // the "who to trade with" board also folds
}}

(function() {{
  var keys = DATA.teamKeys;
  var rDefault = keys.find(function(k){{ return k !== DATA.myTeam; }}) || keys[0];
  teamOptions(document.getElementById('selL'), DATA.myTeam);
  teamOptions(document.getElementById('selR'), rDefault);
  pocketCollapseDefaults();
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
