"""fantasy/analytics.py -- shared analytics + score-breakdown narrative (F5 split, part 3).

The layer above scoring: per-cat percentile/value helpers, positional breakdown,
team category ranks, the tap-to-expand score-breakdown prose (`_*_clauses` /
`_score_narrative` / `_hitter_score_breakdown` / `_pitcher_score_breakdown`) and the
badge-context blocks. No trade logic and no `build_*` section renderers live here --
this is a strict lower layer that the trade engine and the section renderers both read.

Imports name_utils + the ui and scoring leaves via star-import so the moved bodies stay
byte-identical; those lower names are NOT re-exported from here. send_digest re-exports
this module's own names via `from fantasy.analytics import *`.
"""
import math
from datetime import datetime

from name_utils import _name_key
from fantasy.ui import *        # noqa: F401,F403 -- palette + primitives used by moved bodies
from fantasy.scoring import *   # noqa: F401,F403 -- scorers + calibration used by moved bodies

_EXCLUDE = set(dir())          # everything above is imported, not exported from analytics

_badge_name_key = _name_key


_DL_STATUSES = {"TEN_DAY_DL", "FIFTEEN_DAY_DL", "SIXTY_DAY_DL", "IL", "OUT"}

# Recent-form emoji for the tap-to-expand score-breakdown "N-day form" line.
_FORM_EMOJI = {"hot": "\U0001F525", "cold": "\U0001F976", "steady": "➖"}


def _on_il(r):
    """True if the player occupies one of the league's dedicated IL roster slots.
    League has 2 IL slots that don't consume active/bench room, so dropping such a
    player frees nothing usable — never suggest it. ESPN_OnIL is a native bool from
    fetch_data (lineupSlot == 'IL'); guard against a stringified value just in case."""
    v = r.get("ESPN_OnIL")
    if isinstance(v, str):
        return v.strip().lower() == "true"
    return bool(v)


# Plain-English injury read for the tap-to-expand score panel: which IL tier the player is on
# and how long/bad the absence is. ESPN exposes only its status vocabulary (no body-part free
# text), so "how bad" maps to the DL tier. The severity ordering mirrors trades._IL_TVAL_MULT
# so the panel prose and the trade-value discount tell one story. {status: prose}.
_INJURY_CTX = {
    "SIXTY_DAY_DL":   "on the <b>60-day IL</b> &mdash; a long-term absence (likely out for months); trade value is deeply discounted.",
    "OUT":            "<b>ruled OUT</b> &mdash; not available to play; trade value is deeply discounted.",
    "FIFTEEN_DAY_DL": "on the <b>15-day IL</b> &mdash; expected to miss a couple of weeks; trade value lightly discounted.",
    "TEN_DAY_DL":     "on the <b>10-day IL</b> &mdash; a short-term absence, likely back within a week or two; trade value lightly discounted.",
    "IL":             "on the <b>IL</b> &mdash; currently unavailable; trade value discounted.",
    "DAY_TO_DAY":     "<b>day-to-day</b> &mdash; a minor, nagging injury; likely still plays, barely discounted.",
}


def _injury_detail_str(r):
    """The body-part / detail / expected-return specifics (from ESPN's public injuries API, stored
    on the row by fetch_data.attach_injury_notes) as a joined muted string, or "" when absent. Same
    'BodyPart — Detail · exp. return Mon D' shape the digest's Roster Alerts uses, so the two agree."""
    parts = []
    bp  = str(r.get("InjuryBodyPart") or "").strip()
    det = str(r.get("InjuryDetail") or "").strip()
    if bp:
        parts.append(f"{bp}{' &mdash; ' + det if det else ''}")
    rd = str(r.get("InjuryReturnDate") or "").strip()
    if rd:
        try:
            dt = datetime.strptime(rd[:10], "%Y-%m-%d")
            parts.append(f"exp. return {dt.strftime('%b')} {dt.day}")
        except Exception:
            pass
    return "; ".join(parts)


def _injury_context(r):
    """Injury line for the tap-to-expand score panel — names WHICH side of the IL a player is
    on and HOW bad/long the absence is (plus the body part + expected return when the snapshot
    carries them), so a manager weighing any move (especially a trade) sees why he's unavailable
    and why his trade value took a hit. Appears on EVERY surface the score breakdown feeds. Reads
    ESPN_Status first (rostered rows), then FreeAgentInjuryStatus (FA); an IL-slot player with a
    stale/blank status still gets a generic IL note via the _on_il fallback. "" for a healthy,
    active player."""
    status = (str(r.get("ESPN_Status") or "").strip().upper()
              or str(r.get("FreeAgentInjuryStatus") or "").strip().upper())
    txt = _INJURY_CTX.get(status)
    if txt is None and _on_il(r):
        txt = "on the <b>IL</b> &mdash; currently unavailable; trade value discounted."
    if not txt:
        return ""
    detail = _injury_detail_str(r)
    detail_html = (f' <span style="color:{MUTED};">({detail})</span>' if detail else '')
    return (f'<div style="margin-top:6px;color:{YELLOW};">'
            f'&#129657; Injury: <span style="color:{TEXT};">{txt}</span>{detail_html}</div>')


def team_category_ranks(roto_rows):
    """{team_key: {cat: rank}}, n_teams — rank 1 = best in that category. Generalizes
    category_ranks (which returns my team only) to EVERY team, for Trade Radar. Same
    {cat}_Points aggregation, so _LOWER_BETTER is already baked into the points."""
    CATS = ["R", "HR", "RBI", "SB", "OPS", "B_SO", "K", "QS", "W", "ERA", "WHIP", "SVHD"]
    totals = {}
    for row in roto_rows:
        t = " ".join((row.get("Team") or "").split())
        if not t:
            continue
        if t not in totals:
            totals[t] = {c: 0.0 for c in CATS}
        for c in CATS:
            totals[t][c] += float(row.get(f"{c}_Points") or 0)
    teams = list(totals.keys())
    ranks = {t: {} for t in teams}
    for c in CATS:
        ranked = sorted(teams, key=lambda t: -totals[t][c])
        for rank, t in enumerate(ranked, 1):
            ranks[t][c] = rank
    return ranks, len(teams)


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


POS_STARTERS = {"C": 1, "1B": 1, "2B": 1, "3B": 1, "SS": 1, "OF": 3, "SP": 4, "RP": 3}


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

        def score(r, ptype=ptype, score_fn=score_fn, idx30=idx30):
            if ptype == "pit":
                return _score_p(r, idx30)   # role-aware: SP blend / RP rp_score
            return _blend(r, score_fn, idx30)

        my_p = sorted(
            [r for r in season if " ".join((r.get("FantasyTeam") or "").split()) == my_key and pos_match(r)],
            key=lambda r: -score(r),
        )
        for r in my_p:
            r["_pscore"] = score(r)

        # Per-team STARTER score at this position → league rank. Rank on each team's
        # top-K players (K = typical starting slots, POS_STARTERS) rather than the mean
        # of ALL eligible players, so bench/utility depth (e.g. a backup catcher carrying
        # 1B eligibility, or a cold bat behind a starter) can't dilute a position into a
        # phantom "need".
        k = POS_STARTERS.get(pos_label, 1)
        def _starter_avg(scores, k=k):
            top = sorted(scores, reverse=True)[:k]
            return sum(top) / len(top) if top else 0
        team_scores = {}
        for r in season:
            t = r.get("FantasyTeam", "")
            if t and pos_match(r):
                team_scores.setdefault(t, []).append(score(r))
        team_avgs = sorted(_starter_avg(v) for v in team_scores.values())
        my_avg = _starter_avg([r["_pscore"] for r in my_p])
        n = len(team_avgs)
        rank = n - sum(1 for s in team_avgs if s <= my_avg) + 1 if n else None

        # Viable check: only count players actually getting opportunities
        if ptype == "pit":
            if pos_label == "SP":
                gs_min = _pit_viable_min("SP", "GS")
                viable = lambda r: _n(r.get("GS")) >= gs_min
            else:
                gp_min = _pit_viable_min("RP", "GP")
                ip_min = _pit_viable_min("RP", "IP")
                viable = lambda r: _n(r.get("ESPN_GP")) >= gp_min or _n(r.get("IP")) >= ip_min
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
        # Weakest player is an implicit drop/replace target, so skip anyone parked in an
        # IL slot — cutting them frees no active/bench room. Fall back to the full list
        # only if every player at the position is on IL.
        drop_pool = [r for r in my_p if not _on_il(r)] or my_p
        results.append({
            "pos":          pos_label,
            "ptype":        ptype,
            "starter":      my_p[0] if my_p else None,   # rank-defining anchor (top score)
            "worst_player": drop_pool[-1] if drop_pool else None,
            "my_avg":       round(my_avg, 1),
            "rank":         rank,
            "n_teams":      n,
            "top_fa":       fa[:1],
            "fa_depth":     len(fa),
            "fa_quality":   fa_quality,
        })
    return results


def _hit_clauses(r, comps):
    """(fill, strength_phrase, weakness_phrase) per hitter component, for narration."""
    ops = _n(r.get("OPS")); wrc = _n(r.get("wRCplus")); hr = _n(r.get("HR"))
    iso = _n(r.get("ISO")); rbi = _n(r.get("RBI")); sb = _n(r.get("SB"))
    sprint = _n(r.get("SprintSpeed")); xwoba = _n(r.get("xwOBA")); hrp = _n(r.get("HR_Probability"))
    maxes = {"Prod": 30, "HR": 16, "ISO": 6, "RBI": 10, "Speed": 10, "xwOBA": 10, "HR%": 8}
    out = []

    def add(key, strong, weak):
        if key in comps:
            out.append((comps[key] / maxes[key], strong, weak))

    prod_stat = f"wRC+ {int(wrc)}" if wrc > 0 else f"OPS {_st(ops)}"
    add("Prod", f"strong production ({prod_stat})", f"weak production ({prod_stat})")
    # HR (volume) and ISO (rate) are the same "power" concept — never let one surface as a
    # strength while the other reads as a weakness ("big raw power (ISO .190) … little power
    # (6 HR)"). Keep the strength; drop the opposite power stat's weakness clause.
    hr_fill  = comps.get("HR",  0.0) / maxes["HR"]  if "HR"  in comps else None
    iso_fill = comps.get("ISO", 0.0) / maxes["ISO"] if ("ISO" in comps and iso > 0) else None
    hr_weak, iso_weak = f"little power ({int(hr)} HR)", f"flat ISO ({_st(iso)})"
    if hr_fill is not None and iso_fill is not None:
        if iso_fill >= 0.60 and hr_fill <= 0.35:   # strong rate, weak volume → drop HR weakness
            hr_weak = None
        elif hr_fill >= 0.60 and iso_fill <= 0.35:  # strong volume, weak rate → drop ISO weakness
            iso_weak = None
    add("HR", f"real power ({int(hr)} HR)", hr_weak)
    if iso > 0:
        add("ISO", f"big raw power (ISO {_st(iso)})", iso_weak)
    add("RBI", f"drives in runs ({int(rbi)} RBI)", f"few RBI ({int(rbi)})")
    if sprint > 0:
        add("Speed", f"plus speed ({sprint:.1f} ft/s)", f"slow ({sprint:.1f} ft/s)")
    else:
        add("Speed", f"steals bags ({int(sb)} SB)", f"no steals ({int(sb)} SB)")
    if xwoba > 0:
        add("xwOBA", f"quality contact (xwOBA {_st(xwoba)})", f"empty contact (xwOBA {_st(xwoba)})")
    if hrp > 0:
        add("HR%", f"high HR odds ({hrp * 100:.0f}%/gm)", None)   # low HR odds isn't a real weakness
    return out


def _sp_clauses(r, comps):
    """(fill, strength, weakness) per SP component."""
    kpct = _n(r.get("Kpct_P")); kip = _n(r.get("K/IP")); era = _n(r.get("ERA"))
    whip = _n(r.get("WHIP")); brl = _n(r.get("BarrelPctAllowed")); xwoba_ag = _n(r.get("xwOBA_against"))
    maxes = {"K": 28, "RunPrev": 28, "WHIP": 20, "Contact": 12, "Role": 12}
    out = []

    def add(key, strong, weak):
        if key in comps:
            out.append((comps[key] / maxes[key], strong, weak))

    kstat = f"{kpct * 100:.0f}% K" if kpct > 0 else (f"{kip:.2f} K/IP" if kip > 0 else "K rate")
    add("K", f"swing-and-miss ({kstat})", f"low strikeouts ({kstat})")
    add("RunPrev", f"prevents runs ({era:.2f} ERA)", f"gets hit hard ({era:.2f} ERA)")
    add("WHIP", f"limits baserunners ({whip:.2f} WHIP)", f"high WHIP ({whip:.2f})")
    if brl > 0:
        add("Contact", f"soft contact ({brl:.1f}% barrels)", f"hard contact ({brl:.1f}% barrels)")
    elif xwoba_ag > 0:
        add("Contact", f"soft contact (xwOBA-ag {_st(xwoba_ag)})", f"hard contact (xwOBA-ag {_st(xwoba_ag)})")
    return out   # Role (start volume) is a marker, not a skill — left out of the narrative


def _rp_clauses(r, comps):
    """(fill, strength, weakness) per RP component."""
    svhd = _n(r.get("ESPN_SVHD")) or _n(r.get("SVHD"))
    k = _n(r.get("ESPN_K")) or _n(r.get("K"))
    w = _n(r.get("ESPN_W")) or _n(r.get("W"))
    ipg = _n(r.get("IP_per_G")); era = _n(r.get("ERA")); whip = _n(r.get("WHIP"))
    brl = _n(r.get("BarrelPctAllowed"))
    maxes = {"SVHD": 15, "K": 26, "W": 15, "IP/G": 8, "RunPrev": 16, "WHIP": 12, "Contact": 8}
    out = []

    def add(key, strong, weak):
        if key in comps:
            out.append((comps[key] / maxes[key], strong, weak))

    add("SVHD", f"save/hold volume ({int(svhd)} SV+H)", None)   # punt-saves: low SVHD isn't a knock
    add("K", f"racks up Ks ({int(k)} K)", f"low K total ({int(k)})")
    add("W", f"vulturing wins ({int(w)} W)", None)
    add("IP/G", f"multi-inning role ({ipg:.1f} IP/app)", None)
    add("RunPrev", f"prevents runs ({era:.2f} ERA)", f"gets hit ({era:.2f} ERA)")
    add("WHIP", f"limits baserunners ({whip:.2f} WHIP)", f"high WHIP ({whip:.2f})")
    if brl > 0:
        add("Contact", f"soft contact ({brl:.1f}% barrels)", f"hard contact ({brl:.1f}% barrels)")
    return out


def _score_narrative(clauses):
    """Turn per-component (fill, strength, weakness) tuples into a prose sentence naming
    the 2 strongest drivers (fill ≥ .60) and the 2 weakest (fill ≤ .35)."""
    strengths = sorted([(f, s) for (f, s, w) in clauses if s and f >= 0.60], key=lambda x: -x[0])
    weaks     = sorted([(f, w) for (f, s, w) in clauses if w and f <= 0.35], key=lambda x: x[0])
    s_txt = [t for _, t in strengths[:2]]
    w_txt = [t for _, t in weaks[:2]]
    if s_txt and w_txt:
        return f"Carried by {' and '.join(s_txt)}; held back by {', '.join(w_txt)}."
    if s_txt:
        return f"Carried by {' and '.join(s_txt)}; no glaring holes."
    if w_txt:
        return f"Held back by {', '.join(w_txt)}."
    return "Balanced across the board — no standout strength or weakness."


def _badge_ctx_wrap(lines):
    """Wrap per-badge explanation lines in a muted block for the tap-to-expand score panel."""
    if not lines:
        return ""
    inner = "".join(f'<div style="margin-top:3px;">{ln}</div>' for ln in lines)
    return f'<div style="margin-top:6px;color:{MUTED};">{inner}</div>'


def _hit_badge_context(row, hit_pctile=None, cap=None):
    """Explain whichever hitter badges `row` earns — SAME predicates, order and cap as
    hitter_badges (cap=None: every applicable badge) — so the tap-to-expand panel explains
    exactly the chips shown, no more."""
    lines = []
    hrp = _n(row.get("HR_Probability"))
    if hrp >= _PWR_HRP_MIN:
        lines.append(f'{_hit_badge("PWR", PURPLE)} power threat &mdash; modeled HR probability {hrp*100:.0f}% '
                     f'(&ge; {_PWR_HRP_MIN*100:.0f}%, top power tier).')
    if hit_pctile is not None:
        sb = _n(row.get("SB")); spd = _n(row.get("SprintSpeed"))
        if sb > 0 and _cat_pctile(hit_pctile, "SB", sb) >= _SB_PCTILE_MIN and (spd <= 0 or spd >= _SB_SPEED_MIN):
            top = (1.0 - _cat_pctile(hit_pctile, "SB", sb)) * 100
            spd_s = f' &middot; {spd:.1f} ft/s sprint' if spd > 0 else ''
            lines.append(f'{_hit_badge("SB", SILVER)} base-stealer &mdash; {sb:.0f} SB, top {top:.0f}% of the league{spd_s}.')
    avg, iso, xba, xslg = _n(row.get("AVG")), _n(row.get("ISO")), _n(row.get("xBA")), _n(row.get("xSLG"))
    if avg > 0 and iso > 0 and xba > 0 and xslg > 0:
        slg = iso + avg; d_ba = xba - avg; d_slg = xslg - slg
        gap = f'xBA {xba:.3f} vs AVG {avg:.3f}, xSLG {xslg:.3f} vs SLG {slg:.3f}'
        if d_ba >= _XREG_BA and d_slg >= _XREG_SLG:
            lines.append(f'{_hit_badge("$", GREEN)} under his Statcast expected stats ({gap}) '
                         f'&mdash; positive regression likely (buy-low).')
        elif -d_ba >= _XREG_BA and -d_slg >= _XREG_SLG:
            lines.append(f'{_hit_badge("&#9660;", RED)} over his Statcast expected stats ({gap}) '
                         f'&mdash; regression risk (sell-high).')
    return _badge_ctx_wrap(lines[:cap])


def hitter_badges(row, hit_pctile=None, cap=None, regression=True):
    """Concatenated tactical badge HTML for a hitter row (priority PWR->SB->BUY/SELL; `cap=None`
    shows every applicable badge). `hit_pctile` is the league SB percentile pool
    (build_cat_percentiles) — when None, SB is skipped. `regression=False` drops the $/▼
    buy-low/sell-high chip — used by the trade cards, which render their own side-aware
    directional version from `_tsell`/`_tbuy` and would otherwise show it twice."""
    badges = []

    # PWR — power/HR threat (modeled per-game HR probability). Highest priority.
    hrp = _n(row.get("HR_Probability"))
    if hrp >= _PWR_HRP_MIN:
        badges.append(_hit_badge("PWR", PURPLE, _hrp_driver_str(row) or f"HR prob {hrp*100:.0f}%"))

    # SB — genuine base-stealer (scarce, streamable). Percentile of actual SB, speed-corroborated.
    if hit_pctile is not None:
        sb = _n(row.get("SB"))
        spd = _n(row.get("SprintSpeed"))
        if sb > 0 and _cat_pctile(hit_pctile, "SB", sb) >= _SB_PCTILE_MIN and (spd <= 0 or spd >= _SB_SPEED_MIN):
            _t = f"SB {sb:.0f}" + (f" · Sprint {spd:.1f} ft/s" if spd > 0 else "")
            badges.append(_hit_badge("SB", SILVER, _t))

    # BUY-LOW / SELL-HIGH — Statcast expected vs actual (skill-vs-luck read). Mutually exclusive.
    if regression:
        avg, iso, xba, xslg = _n(row.get("AVG")), _n(row.get("ISO")), _n(row.get("xBA")), _n(row.get("xSLG"))
        if avg > 0 and iso > 0 and xba > 0 and xslg > 0:
            slg = iso + avg
            d_ba, d_slg = xba - avg, xslg - slg
            _rt = f"xBA {xba:.3f} vs AVG {avg:.3f} · xSLG {xslg:.3f} vs SLG {slg:.3f}"
            if d_ba >= _XREG_BA and d_slg >= _XREG_SLG:
                badges.append(_hit_badge("$", GREEN, _rt))
            elif -d_ba >= _XREG_BA and -d_slg >= _XREG_SLG:
                badges.append(_hit_badge("&#9660;", RED, _rt))

    return "".join(badges[:cap])


def _sp_skill_context(row):
    """Tap-to-expand 'why' for the season QS / K+ badges (`sp_skill_badges`, in scoring) —
    mirrors `_sp_badge_context` so a trade surface's score panel explains the chips shown.
    Empty when neither fires."""
    lines = []
    qsp = _sp_qs_season(row)
    if qsp is not None and qsp >= _QS_SEASON_MIN:
        lines.append(f'{_hit_badge("QS", CYAN)} reliable quality starts &mdash; {qsp}% season QS rate '
                     f'(elite; league avg ~38%). A durable skill read, not this week&rsquo;s matchup.')
    if _is_sp(row) and _n(row.get("IP")) >= _SP_SKILL_MIN_IP:
        kpct = _n(row.get("Kpct_P"))
        if kpct >= _K_SEASON_MIN:
            whiff, wpct = _n(row.get("WhiffPct")), _n(row.get("WhiffPctile"))
            extra = (f", {whiff:.0f}% whiff" if whiff > 0
                     else (f", {wpct:.0f}th-pctile whiff" if wpct > 0 else ""))
            lines.append(f'{_hit_badge("K+", YELLOW)} strikeout arm &mdash; {kpct*100:.0f}% season K rate '
                         f'(top tier{extra}).')
    return _badge_ctx_wrap(lines)


def _pitcher_badge_context(row):
    """Explain the pitcher regression badge ($ buy-low / ▼ sell-high) `row` earns — SAME
    predicate as `pitcher_regression_badge` — so the tap-to-expand panel explains the chip
    shown beside the name. Row-only (ERA vs xERA, no recent-form input), so it renders from
    the shared breakdown for BOTH SP and RP. The SP-only ⚠ blowup badge — which needs the
    render-site L15 ERA to stay in lockstep with its chip — stays in `_sp_badge_context`."""
    flag = pitcher_regression_flag(row)
    if not flag:
        return ""
    era, xera = _n(row.get("ERA")), _n(row.get("xERA"))
    if flag == "sell":
        line = (f'{pitcher_regression_badge(row)} ERA {era:.2f} is running below his '
                f'{xera:.2f} xERA &mdash; getting lucky, regression risk (sell-high).')
        if _is_sp(row):   # ⚠ only shows for startable arms, so the distinction is SP-only
            line += ' Separate from &#9888;: this is mean regression, not blowup floor.'
    else:
        line = (f'{pitcher_regression_badge(row)} ERA {era:.2f} is running above his '
                f'{xera:.2f} xERA &mdash; unlucky, positive regression likely (buy-low).')
    return _badge_ctx_wrap([line])


def _archetype_form_tail(season, tag, noun, hot, cold_lead):
    """Shared form/value tail for the archetype one-liner. Words only — the hot/cold
    emoji lives on the dedicated 'N-day form' line, so it isn't doubled here. A cold
    read still credits a genuinely good player ('...but still a solid bat')."""
    if tag == "hot":
        return hot
    if tag == "cold":
        tier = "elite" if season >= 85 else ("solid" if season >= 65 else None)
        art  = "an" if tier == "elite" else "a"
        return f"{cold_lead} but still {art} {tier} {noun}" if tier else cold_lead
    if tag == "steady":
        return "steady of late"
    return ""   # no recent-form read (e.g. RP)


def _hitter_archetype(r, hit_pctile, season, tag):
    """Punchy one-line scouting read of a hitter's profile for the score-pill lead-in:
    a category-breadth / power / speed archetype label + a form-aware value tail.
    Reuses the season cat-percentiles (R/HR/RBI/SB/OPS) + raw power/speed shape."""
    pc = ({c: _cat_pctile(hit_pctile, c, _cat_value(r, c)) for c in _FA_HIT_CATS}
          if hit_pctile else {})
    n_strong = sum(1 for c in _FA_HIT_CATS if pc.get(c, 0.0) >= 0.70)
    iso, avg  = _n(r.get("ISO")), _n(r.get("AVG"))
    hrp, k_ct = _n(r.get("HR_Probability")), _n(r.get("K"))
    # Roto "speed" is SB, not raw sprint — a fast runner who doesn't steal helps no cat.
    power_hi = pc.get("HR", 0.0) >= 0.70 or iso >= 0.189 or hrp >= 0.15
    speed_hi = pc.get("SB", 0.0) >= 0.80 or _n(r.get("SB")) >= 15

    if power_hi and k_ct >= 115 and not speed_hi:   # TTO leads breadth — a distinctive read
        desc = "A true three-outcome slugger — big power, big whiff"
    elif n_strong >= 4:
        desc = f"Does a bit of everything ({n_strong}/5 cats)"
    elif power_hi and speed_hi:
        desc = "A power-speed threat"
    elif power_hi:
        desc = "A power-first bat"
    elif speed_hi:
        desc = "A speed merchant — steals carry the value"
    elif pc.get("OPS", 0.0) >= 0.70:
        desc = "An on-base producer"
    elif avg >= 0.285:
        desc = "A high-average, contact-first bat"
    elif n_strong <= 1:
        desc = "A one-dimensional, streaky bat"
    else:
        desc = "A solid everyday contributor"

    tail = _archetype_form_tail(season, tag, "bat",
                                hot="red-hot right now", cold_lead="ice-cold lately")
    return f"{desc}, {tail}." if tail else f"{desc}."


def _pitcher_archetype(r, role, season, tag):
    """Punchy one-line scouting read of a pitcher's profile for the score-pill lead-in.
    Raw-field based (ERA/WHIP/K%/SVHD) so it needs no percentile pool. Adds a
    'watch the baserunners' caveat when a bat-missing arm carries a high WHIP."""
    era, whip = _n(r.get("ERA")), _n(r.get("WHIP"))
    kpct, kip = _n(r.get("Kpct_P")), _n(r.get("K/IP"))
    svhd = _n(r.get("ESPN_SVHD")) or _n(r.get("SVHD"))
    caveat = ""
    # K% is sometimes absent (NaN→0) on otherwise-elite arms — fall back to K/IP so a
    # missing rate can't demote an ace to "mid-rotation".
    k_strong = kpct >= 0.23  or kip >= 1.00
    k_elite  = kpct >= 0.244 or kip >= 1.05
    low_k    = (0 < kpct < 0.20) or (0 < kip < 0.82)

    if role == "SP":
        # Front-line = strong ratios + bat-missing; elite WHIP+K still qualifies an arm whose
        # ERA is inflated by bad luck (e.g. 3.67 ERA behind a 1.06 WHIP and 28% K).
        if whip <= 1.20 and k_strong and (era <= 3.60 or whip <= 1.10):
            desc = "A front-line arm — misses bats and limits damage"
        elif k_elite and (whip >= 1.30 or era >= 4.0):
            desc = "A bat-misser with traffic — Ks, but hittable"
        elif whip <= 1.15 and low_k:
            desc = "A control artist — pounds the zone, low on Ks"
        elif era <= 3.20 and whip <= 1.10:
            desc = "A polished run-preventer — elite ratios, quieter Ks"
        elif era >= 4.80 or whip >= 1.36:
            desc = "Getting hit hard lately"
        elif era <= 4.04:
            desc = "A steady mid-rotation arm"
        else:
            desc = "A back-end streamer"
        hot, cold_lead = "dealing lately", "scuffling lately"
    else:  # RP
        if svhd >= 10 and era <= 3.0 and whip <= 1.15:
            desc = "A lockdown reliever — SV+H volume with clean ratios"
        elif (kpct >= 0.26 or kip >= 1.12) and whip >= 1.30:
            desc = "A strikeout arm with control wobbles"
        elif era <= 3.0 and whip <= 1.15:
            desc = "A ratio helper, light on saves/holds"
        elif kpct >= 0.26 or kip >= 1.12:
            desc = "A strikeout reliever"
        elif era >= 4.46 or whip >= 1.36:
            desc = "A live arm with shaky control"
        else:
            desc = "A middle-relief arm"
        hot, cold_lead = "throwing fire lately", "scuffling lately"

    if k_strong and whip >= 1.40 and not any(w in desc for w in ("traffic", "hit hard", "shaky", "wobbles")):
        caveat = f"watch the baserunners (WHIP {whip:.2f})"

    tail = _archetype_form_tail(season, tag, "arm", hot=hot, cold_lead=cold_lead)
    out = desc
    if tail:
        out += f", {tail}"
    if caveat:
        out += f"; {caveat}"
    return out + "."


def _archetype_line(desc):
    """The archetype sentence as its own italic line between the score header and the
    mechanical 'Carried by...' clause."""
    return (f'<div style="margin-top:4px;font-style:italic;color:{MUTED};">{desc}</div>'
            if desc else "")


def _hitter_score_breakdown(r, idx_recent=None, hit_pctile=None):
    """Prose breakdown of a hitter's Score for the tap-to-expand panel."""
    comps, mult = hitter_score(r, _parts=True)
    if not comps:
        return ""
    season = hitter_score(r)
    # Recent-form lookup drives both the dual-score header and the "N-day form" line.
    rec = idx_recent.get(r.get("PlayerName", "")) if idx_recent else None
    rs  = hitter_score(rec) if rec else 0
    win = tag = None
    if rs > 0:
        ds  = int(_n(rec.get("Dataset")) or 0)
        win = f"{ds}-day" if ds in (7, 15, 30) else "7-day"
        tag = "hot" if rs > season else ("cold" if rs < season else "steady")
    sc = f'<span style="color:{_score_text_hex(season)};font-weight:800;">{season}</span>'
    if rs > 0:
        rc = f'<span style="color:{_score_text_hex(rs)};font-weight:800;">{rs}</span>'
        head = f"Hitter score (season | {win}): {sc} | {rc} {_FORM_EMOJI[tag]}"
    else:
        head = f"Hitter score {sc}"
    narr = _score_narrative(_hit_clauses(r, comps))
    if mult < 0.995:
        narr += f' Trimmed to {round(mult * 100)}% for thin playing time (few at-bats vs a regular).'
    html = (f'<b style="color:{TEXT};">{head}</b>'
            + _archetype_line(_hitter_archetype(r, hit_pctile, season, tag))
            + f'<div style="margin-top:4px;">{narr}</div>')
    hrp = _n(r.get("HR_Probability"))
    if hrp > 0:
        drivers = _hrp_driver_str(r)
        line = f'HR% {hrp * 100:.0f}% modeled per-game HR probability'
        line += f' ({drivers})' if drivers else ''
        html += f'<div style="margin-top:6px;color:{MUTED};">{line}</div>'
    html += _hit_badge_context(r, hit_pctile)
    html += _injury_context(r)
    return html


def _pitcher_score_breakdown(r, idx_recent=None):
    """Prose breakdown of a pitcher's Score. Role-aware: SP → pitcher_score components
    (blended with recent form); RP → rp_score (unblended)."""
    if _is_sp(r):
        comps, mult = pitcher_score(r, _parts=True)
        season, role, clauses = pitcher_score(r), "SP", _sp_clauses(r, comps)
    else:
        comps, mult = rp_score(r, _parts=True)
        season, role, clauses = rp_score(r), "RP", _rp_clauses(r, comps)
    if not comps:
        return ""
    # SP blends recent form; RP is unblended (no recent line, header stays single-score).
    rec = idx_recent.get(r.get("PlayerName", "")) if (role == "SP" and idx_recent) else None
    rs  = pitcher_score(rec) if rec else 0
    win = tag = None
    if rs > 0:
        ds  = int(_n(rec.get("Dataset")) or 0)
        win = f"{ds}-day" if ds in (7, 15, 30) else "15-day"
        tag = "hot" if rs > season else ("cold" if rs < season else "steady")
    sc = f'<span style="color:{_score_text_hex(season)};font-weight:800;">{season}</span>'
    if rs > 0:
        rc = f'<span style="color:{_score_text_hex(rs)};font-weight:800;">{rs}</span>'
        head = f"{role} score (season | {win}): {sc} | {rc} {_FORM_EMOJI[tag]}"
    else:
        head = f"{role} score {sc}"
    narr = _score_narrative(clauses)
    if mult < 0.995:
        narr += f' Trimmed to {round(mult * 100)}% for small innings sample.'
    html = (f'<b style="color:{TEXT};">{head}</b>'
            + _archetype_line(_pitcher_archetype(r, role, season, tag))
            + f'<div style="margin-top:4px;">{narr}</div>')
    html += _pitcher_badge_context(r)
    html += _injury_context(r)
    return html


def _hrp_driver_str(row):
    """The HR-probability drivers (Barrel% · HardHit% · EV · xwOBA · ISO) as a joined
    string, or "" when none are present. Single source for the HR% hover tooltip and
    the expanded score-breakdown panel (so touch users see the same drivers)."""
    b, hh, xw, ev, iso = (_n(row.get("Barrel_Pct")), _n(row.get("HardHit_Pct")),
                          _n(row.get("xwOBA")), _n(row.get("MaxEV")), _n(row.get("ISO")))
    parts = []
    if b > 0:   parts.append(f"Barrel {b:.1f}%")
    if hh > 0:  parts.append(f"HardHit {hh:.0f}%")
    if ev > 0:  parts.append(f"EV {ev:.0f}")
    if xw > 0:  parts.append(f"xwOBA {xw:.3f}")
    if iso > 0: parts.append(f"ISO {iso:.3f}")
    return " · ".join(parts)


_LOWER_BETTER = {"ERA", "WHIP", "B_SO"}  # B_SO is lower-better but a COUNTING stat (accumulates), so NOT a rate cat


_FA_HIT_CATS = ["R", "HR", "RBI", "SB", "OPS"]


_FA_RP_CATS  = ["SVHD", "K", "W", "ERA", "WHIP"]


def _cat_value(row, cat):
    """Raw per-player value for a roto category (RP counting stats prefer ESPN season)."""
    if cat == "SVHD":
        return _n(row.get("ESPN_SVHD")) or _n(row.get("SVHD"))
    if cat == "K":
        return _n(row.get("ESPN_K")) or _n(row.get("K"))
    if cat == "W":
        return _n(row.get("ESPN_W")) or _n(row.get("W"))
    return _n(row.get(cat))


def _cat_pctile(pctile, cat, val):
    """Percentile (0–1) of val within the pool for cat; lower-is-better cats inverted."""
    import bisect
    pool = pctile.get(cat)
    if not pool or val <= 0:
        return 0.0
    p = bisect.bisect_left(pool, val) / len(pool)
    return (1.0 - p) if cat in _LOWER_BETTER else p


def player_cat_strengths(row, pctile, cats, need_cats, thresh=0.70):
    """Up to 3 cats the player is strong in (percentile ≥ thresh), need-cats first."""
    scored = []
    for c in cats:
        pv = _cat_pctile(pctile, c, _cat_value(row, c))
        if pv >= thresh:
            scored.append((c in need_cats, pv, c))
    scored.sort(key=lambda t: (not t[0], -t[1]))
    return [c for _, _, c in scored][:3]


_CAT_DISPLAY = {
    "R": "R", "HR": "HR", "RBI": "RBI", "SB": "SB", "OPS": "OPS",
    "B_SO": "B/SO", "K": "K", "QS": "QS", "W": "W",
    "ERA": "ERA", "WHIP": "WHIP", "SVHD": "SV+H",
}


__all__ = [n for n in dir()
           if n not in _EXCLUDE and n != '_EXCLUDE' and not n.startswith('__')]
