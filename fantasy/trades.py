"""fantasy/trades.py -- the trade engine + trade/pending-offer renderers (F5 split, part 4).

Cross-team trade generation (`find_trades`/`find_trades_combined`), the scarcity-weighted
trade-value currency (`_trade_value`/`compute_position_scarcity`/`_enrich_trade_player`),
the acceptance layer (`_need_mult`/`_trade_tilt`/`_star_premium`/`_deal_star_reach`/
`_deal_star_surrender`/`_pending_verdict`/`_counter_suggestion`), the real-offer grader
(`_grade_pending_trades`), and the digest renderers (`build_trade_radar`/
`build_pending_trades_section` + their line/badge helpers + `_tradelab_button`).

Reads the lower layers (ui/scoring/analytics) via star-import; those names are NOT
re-exported from here. The rebound global `_POS_SCARCITY` is co-located with its writer
`compute_position_scarcity` and its sole reader `_trade_value` (nothing outside this
module reads it), so the facade's star-import of send_digest is unaffected.
send_digest re-exports this module's own names via `from fantasy.trades import *`.
"""
import itertools
import math
from datetime import datetime, timedelta
from urllib.parse import quote

from name_utils import _name_key
from fantasy.ui import *          # noqa: F401,F403
from fantasy.scoring import *     # noqa: F401,F403
from fantasy.analytics import *   # noqa: F401,F403

_EXCLUDE = set(dir())            # everything above is imported, not exported from trades

TRADE_LAB_URL = "https://desktoptableau136-commits.github.io/baseball/"  # hosted pocket Trade Lab


_TRADE_SVHD_W       = 0.35  # punt-saves: discount SV+H contribution in trade value


_POS_SCARCITY_CLAMP = (0.75, 1.50)  # bound the positional-scarcity multiplier so a thin pool can't over-swing _tval


_TRADE_MAX_VAL      = 1.35  # give-side value ceiling — protects my elite bats (sell-high exempt)


_TRADE_GIVE_SLACK   = 0.15  # most extra value I'll include to sweeten (favor-me: don't overpay)


_TRADE_MAX_EDGE     = 0.45  # steal ceiling — how much MORE value I may extract (keeps it plausible)


_TRADE_FAIR_BAND    = 0.20  # fair lane: most value edge I keep and still call it realistic (near-even)


_TRADE_FAIR_PAYUP   = 0.35  # fair lane: most value I'll PAY UP to land a scarce need-fill (a rival accepts)


_TRADE_FAIR_MAX_VAL = 1.90  # fair lane give-ceiling — higher than favor's (deal a real chip) but still protects a franchise anchor


_TRADE_MAX_CARDS    = 6     # trades surfaced


_TRADE_PER_TEAM_CAP = 2     # max cards per partner team (variety)


# --- Personal strategy overlays, keyed by CANONICAL team name -----------------------------
# Applied ONLY when find_trades runs from that team's perspective; every other view (--team)
# sees the default rank-derived behavior, so these never distort another manager's read.
#   PUNT: roto cats to DROP from my need set, so the radar stops recommending help I don't want.
#         (I punt saves — SVHD sits at rank ~10 but chasing closers is counterproductive.)
#   TARGET: positions to treat as acquisition-worthy even when NOT a bottom-third deficit — a
#         clear upgrade there still surfaces. C/SS = my weak hitter spots (SS is a real need,
#         C sits just outside the cutoff); SP = rotation stability so I stream less, knowingly
#         paying a little value for cats I already win (a solid SP helps K/QS/W where I'm deep).
_TEAM_PUNT_CATS  = {"Guerrero Warfare": {"SVHD"}}
_TEAM_TARGET_POS = {"Guerrero Warfare": {"C", "SS", "SP"}}


def _set_trade_caps(max_cards=None, per_team_cap=None):
    """Facade-safe override hook for the Partner-Fit board's temporarily-raised caps.
    Post-F5 these caps live in THIS module; an external `sd._TRADE_MAX_CARDS = N` would
    only rebind the send_digest facade, not the copy find_trades reads here. trade_lab
    calls this (re-exported through the facade) so the raise lands in the right namespace."""
    global _TRADE_MAX_CARDS, _TRADE_PER_TEAM_CAP
    if max_cards is not None:
        _TRADE_MAX_CARDS = max_cards
    if per_team_cap is not None:
        _TRADE_PER_TEAM_CAP = per_team_cap


_TRADE_POOL_WIDTH   = 4     # top-N of each side fed into the 2-for-2 combinations


_POS_DEPTH_SLACK    = 1     # bodies beyond POS_STARTERS I can still use (bench/flex) before a position reads "stacked"


_STAR_TVAL_FLOOR    = 1.10  # _tval below which a player moves freely (no endowment premium) — mid relievers/role bats sit here
_STAR_TVAL_SLOPE    = 1.4   # premium added per _tval point above the floor


_STAR_PREM_CAP      = 1.10  # ceiling so a franchise anchor can't demand an impossible overpay


_TRADE_REALISTIC_MAX = 0.05 # rival "realistic" read: most value I may win and still call it realistic (aggressive)


_NEED_MULT_CAT      = 0.14  # per need-cat boost to a player's value for a team short there


_NEED_MULT_POS      = 0.20  # boost for a hitter at a team's thin position


_NEED_MULT_SURPLUS  = 0.10  # discount when a player only helps a cat/position the team is already deep in


_NEED_MULT_CLAMP    = (0.70, 1.60)  # bound the need multiplier so thin data can't over-swing


_TRADE_THIN_POS_PENALTY = 0.15


def _trade_pos_groups(r):
    """POS_GROUPS labels this player fills (OF covers LF/CF/RF)."""
    tags = {p.strip() for p in str(r.get("Position") or "").upper().replace("/", ",").split(",") if p.strip()}
    return {label for label, slots, _ in POS_GROUPS if tags & slots}


def _team_position_counts(hitters, team_key):
    """Count of a team's rostered YEAR hitters eligible at each POS_GROUPS label (multi-eligible
    bats count at every position they qualify for). Feeds both the my-side redundancy guard and
    the both-parties depth floor (_leaves_position_short), so it takes any team key. INCLUDES
    IL-slot players, who still occupy the position on the roster and return to it."""
    counts = {}
    for r in hitters:
        if int(_n(r.get("Dataset")) or 0) != YEAR:
            continue
        if " ".join((r.get("FantasyTeam") or "").split()) != team_key:
            continue
        for g in _trade_pos_groups(r):
            counts[g] = counts.get(g, 0) + 1
    # sorted keys: _trade_pos_groups returns a set (salted string order), and this dict is
    # JSON-serialized into the Trade Lab DATA blob — sort so the render is process-stable.
    # Readers key by position name, so order is logic-irrelevant.
    return {k: counts[k] for k in sorted(counts)}


def _leaves_position_short(team_counts, give_players, get_players):
    """Hitter positions where GIVING `give_players` and RECEIVING `get_players` drops a team
    below POS_STARTERS bodies (no same-position replacement). A body-count depth floor that is
    INDEPENDENT of the _tval currency — so it catches 'only catcher for 2 OF' that the value math
    misses (catcher category value collapses to ~0, so two productive OF out-value the lone C).
    The rival-side mirror of the my-side surplus_pos give gate, applied to whichever team gives.
    Hitter slots only — pitching need is category-shaped, so SP/RP floors would falsely veto
    reasonable arm swaps. `team_counts` from _team_position_counts (multi-elig + IL aware)."""
    short = set()
    give_groups = {g for p in give_players for g in p.get("_tgroups", set())}
    for P in give_groups:
        if P not in POS_STARTERS or P in ("SP", "RP"):   # hitter slots only
            continue
        leaving  = sum(1 for p in give_players if P in p.get("_tgroups", set()))
        arriving = sum(1 for p in get_players  if P in p.get("_tgroups", set()))
        post = team_counts.get(P, 0) - leaving + arriving
        if post < POS_STARTERS[P]:
            short.add(P)
    return short


def _non_redundant_get_pos(get_pos, outs, ins, my_pos_count):
    """Filter get-positions to those that AREN'T roster-redundant. A position P is redundant
    when the deal would leave me with more eligible bodies there than my startable slots plus
    one bench (POS_STARTERS[P] + _POS_DEPTH_SLACK) AND I shed nobody eligible at P — e.g.
    acquiring a 4th catcher while rostering three and dealing none back. Acquiring an upgrade
    while shedding a body at P (a swap) still counts as filling the need. Guards against
    trades that read as 'fills your C slot' when catching is already stacked."""
    keep = set()
    for P in get_pos:
        added = sum(1 for i in ins if P in i.get("_tgroups", set()))
        shed  = sum(1 for o in outs if P in o.get("_tgroups", set()))
        post  = my_pos_count.get(P, 0) - shed + added
        cap   = POS_STARTERS.get(P, 1) + _POS_DEPTH_SLACK
        if not (post > cap and shed == 0):
            keep.add(P)
    return keep


_POS_SCARCITY = {}


def _trade_value(r, ptype, hit_pctile, pit_pctile):
    """Cross-role trade currency (`_tval`): summed above-median category contribution,
    then (hitters) scaled by positional scarcity. Hitters span 5 everyday cats; a
    reliever's counting stats (K/W) rank low vs the whole pitcher pool (volume-aware) and
    SV+H is punt-discounted — so an everyday bat outweighs a one-category closer even when
    both post a top role-score badge. The scarcity scale (_POS_SCARCITY) then corrects the
    remaining position blindness: without it an everyday OF and an everyday SS graded
    equal, so the radar handed you 'give abundant OF, get scarce SS' deals no rival would
    accept. Promoted from a find_trades closure so the pending-trade evaluator grades on
    the SAME currency."""
    cats, pctile = (_FA_HIT_CATS, hit_pctile) if ptype == "hit" else (_FA_RP_CATS, pit_pctile)
    v = 0.0
    for c in cats:
        p = _cat_pctile(pctile, c, _cat_value(r, c))
        v += max(0.0, p - 0.5) * (_TRADE_SVHD_W if c == "SVHD" else 1.0)
    if ptype == "hit" and _POS_SCARCITY:
        # Best-slot value: a multi-eligible bat is worth his scarcest position (a
        # catcher-eligible hitter carries the C premium). Empty global → mult 1.0 (raw),
        # which is also what compute_position_scarcity sees while deriving the scale.
        mult = max((_POS_SCARCITY.get(g, 1.0) for g in _trade_pos_groups(r)), default=1.0)
        v *= mult
    return v


def compute_position_scarcity(hitters, hit_pctile):
    """Derive the hitter positional-scarcity scale from this snapshot → module global
    _POS_SCARCITY (read by _trade_value). A position whose league-wide STARTER talent is
    weak (low mean raw _tval among the players who'd actually start there) is SCARCE — a
    competent bat there clears replacement level by more, so his trade value is boosted;
    a deep position (OF) is discounted. mult[P] = baseline / starter_avg_tval[P], baseline
    = mean starter-tval across positions, clamped to _POS_SCARCITY_CLAMP so a thin sample
    can't over-swing. Reset to {} first so the _trade_value calls below return RAW value
    (the scale must be built from unscaled tvals). Hitters only — pitcher value stays
    punt-saves-shaped, a separate axis. Call AFTER hit_pctile is built."""
    global _POS_SCARCITY
    _POS_SCARCITY = {}
    season = [r for r in hitters if int(_n(r.get("Dataset")) or 0) == YEAR]
    if not season:
        return _POS_SCARCITY
    n_teams = len({" ".join((r.get("FantasyTeam") or "").split())
                   for r in season if (r.get("FantasyTeam") or "").strip()}) or 12
    starter_avg = {}
    for pos_label, slots, ptype in POS_GROUPS:
        if ptype != "hit":
            continue
        elig = [r for r in season
                if any(s in str(r.get("Position", "")).split(", ") for s in slots)]
        vals = sorted((_trade_value(r, "hit", hit_pctile, None) for r in elig), reverse=True)
        top = vals[:POS_STARTERS.get(pos_label, 1) * n_teams]
        if top:
            starter_avg[pos_label] = sum(top) / len(top)
    if not starter_avg:
        return _POS_SCARCITY
    baseline = sum(starter_avg.values()) / len(starter_avg)
    lo, hi = _POS_SCARCITY_CLAMP
    _POS_SCARCITY = {pos: max(lo, min(hi, baseline / sa)) if sa > 0 else 1.0
                     for pos, sa in starter_avg.items()}
    return _POS_SCARCITY


def _enrich_trade_player(r, ptype, best_recent_p, best_recent_h, hit_pctile, pit_pctile):
    """Attach the trade-scoring fields to a player row (role score, cat strengths, value
    currency, buy/sell timing, position groups). Promoted from a find_trades closure so
    both find_trades and build_pending_trades_section share one source of truth."""
    if ptype == "hit":
        r["_tscore"] = _blend(r, hitter_score, best_recent_h)
        r["_tcats"]  = set(player_cat_strengths(r, hit_pctile, _FA_HIT_CATS, set()))
    else:
        r["_tscore"] = _score_p(r, best_recent_p)
        r["_tcats"]  = set(player_cat_strengths(r, pit_pctile, _FA_RP_CATS, set()))
    r["_tval"]    = _trade_value(r, ptype, hit_pctile, pit_pctile)
    _flag = _regression_flag(r) if ptype == "hit" else pitcher_regression_flag(r)
    r["_tsell"]   = (_flag == "sell")
    r["_tbuy"]    = (_flag == "buy")
    r["_tgroups"] = _trade_pos_groups(r)
    r["_tptype"]  = ptype
    return r


def _pos_need_surplus(pos_data, n_teams_fallback):
    """(need_pos, surplus_pos) from a team's positional_breakdown rows — the shared convention
    find_trades uses for 'my' side and (via pos_data_by_team) any rival side. need_pos:
    {pos: (rank, my_avg)} for bottom-third HITTER positions only (positional need is
    hitter-only — pitching need stays category-shaped). surplus_pos: set of top-third
    positions across ALL roles (safe to trade FROM without opening a hole)."""
    _nt    = lambda p: (p.get("n_teams") or n_teams_fallback)
    _third = lambda p: max(1, round(_nt(p) / 3.0))
    surplus_pos = {p["pos"] for p in pos_data if (p.get("rank") or _nt(p)) <= _third(p)}
    need_pos = {p["pos"]: ((p.get("rank") or _nt(p)), (p.get("my_avg") or 0))
                for p in pos_data if p.get("ptype") == "hit"
                and (p.get("rank") or _nt(p)) >= _nt(p) - _third(p) + 1}
    return need_pos, surplus_pos


def find_trades(pitchers, hitters, roto, my_team, best_recent_p, best_recent_h,
                pos_data, hit_pctile, pit_pctile, mode="favor",
                pos_data_by_team=None, realistic_only=False):
    """Ranked list of mutually-beneficial trades between my_team and each rival.
    Perspective-driven via my_team (works under --team for free). See CLAUDE.md.

    `mode` selects the value lane (both grade on the SAME scarcity-weighted `_tval`):
    - "favor" (default): tilt value to me — I never overpay past _TRADE_GIVE_SLACK and may
      extract up to a _TRADE_MAX_EDGE steal; my elite bats are protected from the give side.
    - "fair": realistic, near-even deals a rival would actually accept — the ones that land
      a scarce C/SS upgrade. The give-side value ceiling is dropped (I'll deal a real chip
      for fair return), the gate is centered on even (I may even PAY UP to _TRADE_FAIR_PAYUP
      for a need-fill), and ranking rewards need-fit + their benefit + timing over any edge.
    Each trade is tagged `"lane"` = the mode that produced it.

    `pos_data_by_team` (optional): {team_key: positional_breakdown(...)} cache so a caller
    that already built it (e.g. trade_lab.py's Partner Fit Board, run from every team's POV)
    doesn't trigger a positional_breakdown call storm; falls back to computing a rival's
    positional_breakdown on demand when not supplied.
    `realistic_only`: drop any candidate `_trade_tilt` wouldn't call "realistic" (rival demand
    -side net or the graduated star-reach check) before it can win a slot — used by the
    prescriptive Trade Radar surfaces (digest, dashboard) so they never suggest a deal the
    rival would balk at. Left False for Partner Fit Board, which intentionally shows a
    REACH/"worth a shot" tier too."""
    ranks, n = team_category_ranks(roto)
    my_key = " ".join(my_team.split())
    if n < 2 or my_key not in ranks:
        return []
    third = max(1, round(n / 3.0))
    needs_of   = lambda team: {c for c, rk in ranks[team].items() if rk >= n - third + 1}
    surplus_of = lambda team: {c for c, rk in ranks[team].items() if rk <= third}

    # Personal strategy overlay (my perspective only): drop punted cats from my needs so the
    # radar stops chasing them, and note target positions to surface even absent a rank deficit.
    punt_cats  = _TEAM_PUNT_CATS.get(my_key, set())
    target_pos = _TEAM_TARGET_POS.get(my_key, set())

    my_needs, my_surplus = (needs_of(my_key) - punt_cats), surplus_of(my_key)
    if not my_needs and not target_pos:
        return []

    # Position groups I'm deep in (top-third rank) — safe to trade FROM without a hole.
    # HITTER positions I'm thin at (bottom-third rank) → {pos: (rank, my_avg_score)}. The
    # hitting game is filling roster holes across C/1B/2B/3B/SS/OF, so a hitter who
    # UPGRADES a thin position is worth acquiring even when my category totals there
    # aren't bottom-third — positional scarcity is its own need. (Pitching need is
    # category-shaped — QS/SP vs SV+H/K balance — so it stays category-driven.)
    need_pos, surplus_pos = _pos_need_surplus(pos_data, n)
    # Fold in explicit TARGET positions (C/SS hitter spots, SP for rotation stability) even when
    # NOT a bottom-third deficit — an incomer still has to clear my starter avg there (upgrade-
    # gated in _fills_need_pos below), so this surfaces a real upgrade at a spot I care about
    # without flagging phantom needs. SP joins need_pos as an honorary entry so its downstream
    # (rank ranking, upgrade gate) works; _need_mult skips the positional term for pitchers, so
    # the SP key never distorts a pitcher's demand-side value.
    for d in pos_data:
        if d.get("pos") in target_pos and d.get("pos") not in need_pos:
            need_pos[d["pos"]] = ((d.get("rank") or n), (d.get("my_avg") or 0))

    def roster(source, team):
        return [r for r in source
                if " ".join((r.get("FantasyTeam") or "").split()) == team
                and int(r.get("Dataset", 0) or 0) == YEAR]

    def enrich(r, ptype):
        return _enrich_trade_player(r, ptype, best_recent_p, best_recent_h, hit_pctile, pit_pctile)

    my_players = ([enrich(r, "hit") for r in roster(hitters, my_key)] +
                  [enrich(r, "pit") for r in roster(pitchers, my_key)])
    my_pos_count = _team_position_counts(hitters, my_key)   # redundancy + depth guard: my bodies per position

    all_teams = {" ".join((r.get("FantasyTeam") or "").split())
                 for r in itertools.chain(hitters, pitchers)
                 if (r.get("FantasyTeam") or "").strip()}

    def _fills_need_pos(r):
        """Need/target positions this player upgrades — a hitter at a thin (or explicitly
        targeted) position of mine whose value clears my current average there (a genuine
        upgrade, not a lateral move). SP is credited only as an explicit rotation-stability
        TARGET (not a category need — my pitching cats are a surplus), same upgrade gate."""
        if r["_tptype"] == "hit":
            return {pos for pos in (r["_tgroups"] & set(need_pos))
                    if r["_tscore"] > need_pos[pos][1]}
        if "SP" in need_pos and _is_sp(r) and r["_tscore"] > need_pos["SP"][1]:
            return {"SP"}
        return set()

    trades = []
    for team in all_teams:
        if team == my_key or team not in ranks:
            continue
        t_needs, t_surplus = needs_of(team), surplus_of(team)   # rival's demand-side context
        send_cats = my_surplus & t_needs           # cats I can help THEM in (they must benefit)
        get_cats  = t_surplus & my_needs           # cats they can help ME in
        if not send_cats:                          # no mutual benefit possible → skip
            continue
        t_players = ([enrich(r, "hit") for r in roster(hitters, team)] +
                     [enrich(r, "pit") for r in roster(pitchers, team)])
        t_pos_count = _team_position_counts(hitters, team)   # depth guard: rival bodies per position
        t_pos_data = (pos_data_by_team.get(team) if pos_data_by_team else None) \
            or positional_breakdown(pitchers, hitters, team, best_recent_p, best_recent_h)
        t_need_pos, t_surplus_pos = _pos_need_surplus(t_pos_data, n)   # rival's positional need/surplus

        # Give side: strong in a send cat, at a surplus position, and NOT above the value
        # ceiling (unless a sell-high regression candidate I want to move anyway). FAIR mode
        # raises the ceiling (_TRADE_FAIR_MAX_VAL > favor's _TRADE_MAX_VAL) so I'll deal a
        # genuinely good chip for fair return — but still protects a franchise anchor from
        # being auto-offered (the value gate keeps the return fair either way).
        _give_ceil = _TRADE_FAIR_MAX_VAL if mode == "fair" else _TRADE_MAX_VAL
        out_pool = sorted([r for r in my_players
                           if (r["_tcats"] & send_cats) and (r["_tgroups"] & surplus_pos)
                           and (r["_tval"] <= _give_ceil or r["_tsell"])],
                          key=lambda r: -r["_tscore"])[:_TRADE_POOL_WIDTH]
        # Incoming: helps a category I need OR upgrades a thin position of mine.
        in_pool  = sorted([r for r in t_players if (r["_tcats"] & get_cats) or _fills_need_pos(r)],
                          key=lambda r: -r["_tscore"])[:_TRADE_POOL_WIDTH]
        if not out_pool or not in_pool:
            continue
        for r in in_pool:
            r["_tfillpos"] = sorted(_fills_need_pos(r))

        def _favor_me(vo, vi, mult=1.0):
            """I don't overpay (give minus get within a small slack) and I may extract up
            to a capped edge (get minus give) — so trades tilt to my advantage but stay
            plausible enough that a rival fixing a category need would accept."""
            return (vo - vi) <= _TRADE_GIVE_SLACK * mult and (vi - vo) <= _TRADE_MAX_EDGE * mult

        def _fair(vo, vi, mult=1.0):
            """Near-even: I keep at most a modest edge (_TRADE_FAIR_BAND) and may PAY UP at
            most _TRADE_FAIR_PAYUP — the acceptable-to-a-rival zone that lands scarce pieces."""
            net = vi - vo
            return -_TRADE_FAIR_PAYUP * mult <= net <= _TRADE_FAIR_BAND * mult

        gate = _fair if mode == "fair" else _favor_me
        packages = []
        for o in out_pool:                                   # 1-for-1
            for i in in_pool:
                if gate(o["_tval"], i["_tval"]):
                    packages.append(([o], [i]))
        for oa, ob in itertools.combinations(out_pool, 2):   # 2-for-2
            for ia, ib in itertools.combinations(in_pool, 2):
                pos_ben  = _non_redundant_get_pos(set(ia["_tfillpos"]) | set(ib["_tfillpos"]),
                                                  [oa, ob], [ia, ib], my_pos_count)
                benefits = (((ia["_tcats"] | ib["_tcats"]) & get_cats) | pos_ben)
                if len(benefits) < 2:
                    continue   # a package must address >= 2 distinct needs (cat or position)
                if gate(oa["_tval"] + ob["_tval"], ia["_tval"] + ib["_tval"], mult=1.5):
                    packages.append(([oa, ob], [ia, ib]))

        for outs, ins in packages:
            gcov = set().union(*[i["_tcats"] for i in ins]) & get_cats   # category needs met
            gpos = _non_redundant_get_pos(set().union(*[set(i["_tfillpos"]) for i in ins]),
                                          outs, ins, my_pos_count)         # positional holes filled (redundancy-guarded)
            scov = set().union(*[o["_tcats"] for o in outs]) & send_cats
            if (not gcov and not gpos) or not scov:
                continue
            # DEPTH FLOOR (both parties, hard veto): a deal may not drop either team below
            # startable bodies at a hitter position without a same-position body coming back.
            # Catches the 'their only catcher for 2 of my OF' asymmetry the _tval math misses
            # (catcher category value ~0, so 2 OF out-value the lone C and it reads "realistic").
            if _leaves_position_short(t_pos_count, ins, outs):    # rival gives ins, receives outs
                continue
            if _leaves_position_short(my_pos_count, outs, ins):   # I give outs, receive ins
                continue
            net_val  = sum(i["_tval"] for i in ins) - sum(o["_tval"] for o in outs)  # my base value edge (+ = I win)
            # A radar idea is something *I* would send — never suggest one that requires me to
            # surrender my best-role star at par (same graduated reluctance _deal_star_reach
            # checks on the rival's side). There's no "counter" on an outgoing offer, so instead
            # of flagging it, just don't propose it.
            if _deal_star_surrender(ins, outs, net_val):
                continue
            # Demand-side net from the RIVAL's POV (+ = they win by THEIR needs): what they
            # receive (my outs) valued by their needs (category AND positional — same
            # convention as the my-side calc below), minus what they surrender (the ins).
            # Drives the "realistic vs aggressive ask" read so it's roster-aware, not just
            # base-value. Matches the Trade Lab JS mirror (`netThem`/`partnerMeta`) exactly.
            their_get_val = sum(o["_tval"] * _need_mult(o, t_needs, t_surplus, t_need_pos, t_surplus_pos) for o in outs)
            their_give_val = sum(i["_tval"] * _need_mult(i, t_needs, t_surplus, t_need_pos, t_surplus_pos) for i in ins)
            net_them = their_get_val - their_give_val
            # Demand-side net from MY POV (+ = I win by MY needs) — feeds the quiet value-matrix
            # (Base / You / Them give-get-net) on the card, not a verdict; there's nothing to
            # "accept" on a deal I'm the one proposing.
            my_get_val = sum(i["_tval"] * _need_mult(i, my_needs, my_surplus, need_pos, surplus_pos) for i in ins)
            my_give_val = sum(o["_tval"] * _need_mult(o, my_needs, my_surplus, need_pos, surplus_pos) for o in outs)
            net_me = my_get_val - my_give_val
            # HONEST READ: a surviving deal that leaves the rival at exactly the floor at a
            # single-slot position (they give their starting C/1B/2B/3B/SS with no backup) is
            # thin, not clean. Each such slot penalizes net_them so _trade_tilt flips the read
            # to "aggressive ask", and names the hole for the card footer. Read layer only.
            thin_them = set()
            for P in {g for i in ins for g in i.get("_tgroups", set())}:
                if POS_STARTERS.get(P) != 1:                      # single-slot hitter positions only
                    continue
                leaving  = sum(1 for i in ins  if P in i.get("_tgroups", set()))
                arriving = sum(1 for o in outs if P in o.get("_tgroups", set()))
                if t_pos_count.get(P, 0) - leaving + arriving == POS_STARTERS[P]:
                    thin_them.add(P)
            thin_note = ""
            if thin_them:
                net_them -= _TRADE_THIN_POS_PENALTY * len(thin_them)
                _lbl = ", ".join(sorted(thin_them))
                thin_note = f"leaves them without a backup at {_lbl}"
            # Prescriptive surfaces (Trade Radar) only want deals the rival would actually
            # accept — reuse _trade_tilt's own realistic/aggressive-ask read (net_them band +
            # graduated star-reach check) as a hard generation-time gate, so an "aggressive
            # ask" idea can never win a card slot. Left off for Partner Fit Board, which wants
            # to see reach deals too (tiered separately as REACH/"worth a shot").
            if realistic_only and _trade_tilt(net_val, ins, outs, net_them=net_them)[1] != "realistic":
                continue
            # DIRECTIONAL timing: selling-high on the GIVE side and buying-low on the GET
            # side are the arbitrage (do more of it); giving away a riser or acquiring a
            # regression candidate is the reverse (penalize — these are traps).
            sell_out = sum(1 for o in outs if o.get("_tsell"))   # good: move my regressors
            buy_in   = sum(1 for i in ins  if i.get("_tbuy"))    # good: acquire their risers
            buy_out  = sum(1 for o in outs if o.get("_tbuy"))    # bad: don't sell my risers low
            sell_in  = sum(1 for i in ins  if i.get("_tsell"))   # bad: don't buy their regressors
            timing = sell_out + buy_in - buy_out - sell_in
            # Season fit: deeper need cats/positions addressed (higher rank number) score more.
            # FAVOR mode tilts to me — their benefit down-weighted (0.3), reward my value edge
            # (+5·net_val). FAIR mode optimizes for a realistic, accepted deal — weight THEIR
            # benefit higher (0.5, acceptance likelihood) and, instead of rewarding an edge,
            # PENALIZE paying up (−4·overpay) so the closest-to-even need-fills rank first.
            my_gain    = sum(ranks[my_key][c] for c in gcov) + sum(need_pos[p][0] for p in gpos)
            their_gain = sum(ranks[team][c]   for c in scov)
            if mode == "fair":
                score = (my_gain + 0.5 * their_gain + 4.0 * timing
                         - 4.0 * max(0.0, -net_val) - 0.5 * (len(ins) - 1))
            else:
                score = (my_gain + 0.3 * their_gain + 5.0 * net_val
                         + 4.0 * timing - 0.5 * (len(ins) - 1))
            trades.append({
                "team": team, "outs": outs, "ins": ins, "net_val": net_val, "score": score,
                "net_them": net_them, "net_me": net_me, "thin_note": thin_note,
                "my_give_val": my_give_val, "my_get_val": my_get_val,
                "their_give_val": their_give_val, "their_get_val": their_get_val,
                "lane": mode, "sell_out": sell_out, "buy_in": buy_in,
                "get_cats":  sorted(gcov, key=lambda c: -ranks[my_key][c]),
                "get_pos":   sorted(gpos, key=lambda p: -need_pos[p][0]),
                "send_cats": sorted(scov, key=lambda c: -ranks[team][c]),
            })

    trades.sort(key=lambda t: -t["score"])
    picked, per_team, seen = [], {}, set()
    for t in trades:
        sig = (t["team"],
               tuple(sorted(o.get("PlayerName", "") for o in t["outs"])),
               tuple(sorted(i.get("PlayerName", "") for i in t["ins"])))
        if sig in seen or per_team.get(t["team"], 0) >= _TRADE_PER_TEAM_CAP:
            continue
        seen.add(sig)
        per_team[t["team"]] = per_team.get(t["team"], 0) + 1
        picked.append(t)
        if len(picked) >= _TRADE_MAX_CARDS:
            break
    return picked


def find_trades_combined(pitchers, hitters, roto, my_team, best_recent_p, best_recent_h,
                         pos_data, hit_pctile, pit_pctile, cards=None,
                         pos_data_by_team=None, realistic_only=False):
    """Blend the FAIR lane (realistic, near-even/pay-up deals a rival would accept — the
    ones that actually land a scarce C/SS upgrade) with the FAVOR-ME lane (value plays that
    fix a rival's need while tilting to me). Leads with a fair deal (the realistic ask),
    then alternates a value play, deduping identical packages and keeping the per-team +
    total caps joint across lanes. Both lanes grade on the same scarcity-weighted `_tval`.
    `pos_data_by_team`/`realistic_only` are forwarded to find_trades verbatim — see its
    docstring."""
    cards = cards or _TRADE_MAX_CARDS
    args = (pitchers, hitters, roto, my_team, best_recent_p, best_recent_h,
            pos_data, hit_pctile, pit_pctile)
    kwargs = dict(pos_data_by_team=pos_data_by_team, realistic_only=realistic_only)
    fair  = find_trades(*args, mode="fair", **kwargs)
    value = find_trades(*args, mode="favor", **kwargs)
    order = []
    for f, v in itertools.zip_longest(fair, value):   # fair leads each round (user priority)
        if f is not None:
            order.append(f)
        if v is not None:
            order.append(v)
    picked, per_team, seen = [], {}, set()
    for t in order:
        sig = (t["team"],
               tuple(sorted(o.get("PlayerName", "") for o in t["outs"])),
               tuple(sorted(i.get("PlayerName", "") for i in t["ins"])))
        if sig in seen or per_team.get(t["team"], 0) >= _TRADE_PER_TEAM_CAP:
            continue
        seen.add(sig)
        per_team[t["team"]] = per_team.get(t["team"], 0) + 1
        picked.append(t)
        if len(picked) >= cards:
            break
    return picked


def _star_premium(tval):
    """Graduated endowment/star premium a manager attaches to a player of this trade VALUE:
    0 below _STAR_TVAL_FLOOR, rising _STAR_TVAL_SLOPE per _tval point, capped at _STAR_PREM_CAP.
    Acceptance-layer only (NEVER folds into _tval). Keyed on `_tval`, NOT the role score — role
    score and value diverge structurally (rho~0.82): a vulture-win reliever (Aaron Ashby: score
    95, _tval 0.88) scores like a star but trades like a role player, and an elite closer scores
    like every other closer while trading much cheaper. Pricing the premium on the cross-role
    value currency fixes both, and makes relievers comparable to hitters/starters on ONE axis —
    so the old `_star_role` exclusion is gone (a mid reliever simply sits below the floor at ~0,
    an elite closer earns real premium). 0.88->0.00, 1.10->0.00, 1.42->0.45, 1.52->0.59,
    1.90->1.10."""
    return max(0.0, min(_STAR_PREM_CAP, (_n(tval) - _STAR_TVAL_FLOOR) * _STAR_TVAL_SLOPE))


def _need_mult(row, need_cats, surplus_cats, need_pos=None, surplus_pos=None):
    """Demand-side team-need multiplier: how much MORE (or less) this player is worth to a
    given team, given that team's category needs/surpluses and (hitters) thin/deep positions.
    effective_value(player, team) = _tval * _need_mult. ACCEPTANCE-READ layer only -- never
    folds into _tval or the find_trades ranking (which already rewards need coverage), so the
    universal currency stays intact. Reuses the enriched `_tcats`/`_tgroups` fields. This is
    what lets the same catcher be worth more to a team that needs the position than to one
    already set there. Clamped to _NEED_MULT_CLAMP."""
    cats = row.get("_tcats") or set()
    need_cats, surplus_cats = (need_cats or set()), (surplus_cats or set())
    m = 1.0
    m += _NEED_MULT_CAT * len(cats & need_cats)           # each need cat he covers is worth more here
    if cats and not (cats & need_cats) and (cats & surplus_cats):
        m -= _NEED_MULT_SURPLUS                            # only helps where they're already deep
    if row.get("_tptype") == "hit":
        groups = row.get("_tgroups") or set()
        if need_pos and (groups & set(need_pos)):
            m += _NEED_MULT_POS                            # fills a thin position for this team
        elif surplus_pos and groups and groups <= set(surplus_pos):
            m -= _NEED_MULT_SURPLUS                        # only stacks positions they're deep in
    lo, hi = _NEED_MULT_CLAMP
    return max(lo, min(hi, m))


def _deal_star_reach(ins, outs, net_val):
    """Market-perception (NOT value) check: would a rival balk because the deal asks them to
    part with prized players without a real overpay? The premium the rival must be paid to
    SURRENDER their side (`ins` = what I acquire) is the SUM of `_star_premium(_tval)` across
    those players; the premium they RECEIVE back is the sum across `outs` (what I give). Reach
    (they balk) when required overpay `req = sum(surrender) - sum(receive)` is positive AND I'm
    NOT paying up by at least that much (`net_val > -req`). SUMMING (not max-per-side) is what
    catches "two franchise players for one star + a role player": a lone high-value return can't
    mask that the rival is shipping two premium assets. Value-keyed via `_star_premium`, so a
    role-player's inflated role score no longer counts as a star and a mid reliever contributes
    ~0. Keeps `_tval` a pure value currency; the premium lives in the acceptance layer only.
    Called by `_trade_tilt`."""
    if not ins:
        return False
    surrender = sum(_star_premium(p.get("_tval")) for p in ins)
    receive   = sum(_star_premium(o.get("_tval")) for o in (outs or []))
    req = max(0.0, surrender - receive)
    return req > 0.0 and net_val > -req


def _deal_star_surrender(ins, outs, net_val):
    """The MY-side mirror of `_deal_star_reach`: would *I* balk at parting with prized players
    without a real value win? Required premium = SUM of `_star_premium(_tval)` across my give
    (`outs`) minus the sum across my acquire (`ins`) — a star-for-star swap nets ~0. I hold out
    when that's positive AND I'm NOT winning by at least that much (`net_val < req`). Same
    value-keyed, summed premium; acceptance-layer only. Used by `_pending_verdict`."""
    if not outs:
        return False
    give_prem = sum(_star_premium(o.get("_tval")) for o in outs)
    get_prem  = sum(_star_premium(p.get("_tval")) for p in (ins or []))
    req = max(0.0, give_prem - get_prem)
    return req > 0.0 and net_val < req


def _trade_tilt(net_val, ins=None, outs=None, net_them=None):
    """(value_phrase, accept_phrase, accept_color) for a trade. `net_val` is MY base value edge
    (+ = I win) and sets the my-POV value phrase. The accept phrase is the RIVAL's-POV read on
    whether they'd say yes. It's now AGGRESSIVE: the rival must not clearly lose. When
    `net_them` (their demand-side net, + = they win by THEIR needs) is supplied it drives that
    read (`net_them >= -_TRADE_REALISTIC_MAX`); otherwise it falls back to the base symmetric
    proxy (`net_val <= _TRADE_REALISTIC_MAX`, i.e. their base loss is small). A STAR REACH
    (prying their best player without an overpay) also forces "aggressive ask". Pass `ins`/`outs`
    so the graduated star check runs. Shared by the digest Trade Radar + the dashboard tile."""
    value = ("you win the value" if net_val > 0.1 else
             "even value" if net_val >= -0.1 else "you pay up")
    rival_ok = (net_them >= -_TRADE_REALISTIC_MAX) if net_them is not None \
        else (net_val <= _TRADE_REALISTIC_MAX)
    realistic = rival_ok and not _deal_star_reach(ins, outs, net_val)
    if realistic:
        return value, "realistic", GREEN
    return value, "aggressive ask", YELLOW


def _trade_score_reveal(score, breakdown_html, uid):
    """Div-based analog of `score_reveal` for the Trade Radar cards, whose players are
    stacked <div>s inside table cells (not per-player <tr> rows) — so the <tr> reveal
    can't be appended. Returns (badge_link, hidden_div): the clickable ▾ badge + a hidden
    breakdown <div> to drop right after the player line; a `div.scorebd-div:target` rule
    in the head <style> reveals it in the browser attachment (Gmail strips <style> → the
    div stays hidden, the badge is a harmless no-op)."""
    if not breakdown_html or not uid:
        return badge(score), ""
    cell = (f'<a href="#{uid}" class="bdlink" title="Tap for score breakdown" '
            f'style="text-decoration:none;white-space:nowrap;">{badge(score)}'
            f'<span style="color:{MUTED};font-size:9px;font-weight:700;">&nbsp;&#9662;</span></a>')
    div = (f'<div id="{uid}" class="scorebd-div" style="display:none;background:{SURFACE2};'
           f'padding:8px 12px;margin:2px 0 6px;font-size:11px;line-height:1.55;color:{MUTED};'
           f'border-left:3px solid {ACCENT};border-radius:4px;white-space:normal;">'
           f'{breakdown_html}'
           f'<a href="#{uid}x" style="color:{MUTED};text-decoration:none;font-weight:700;'
           f'float:right;margin-left:10px;">&#10005;</a></div>')
    return cell, div


def _trade_player_line(r, hi_cats, hi_color, side, show_pos=False,
                       best_recent_p=None, best_recent_h=None, hit_pctile=None):
    """One player row inside a trade card: MLB logo + name + score badge + cat chips
    (+ a CYAN position chip for a thin slot the incoming player upgrades, + the CANONICAL
    buy-low/sell-high chip — same glyph-only `$`/`▼` (green/red) as everywhere else in the
    digest, so the visual language stays consistent). `side` ('give'/'get') only tunes the
    hover tooltip; the whole-trade framing lives in the footer's sell-high/buy-low tag.
    The score badge is tap-to-expand (role-aware prose breakdown for every player in the
    trade), same as the section tables."""
    logo  = team_logo(r.get("Team"), 14)
    nm    = str(r.get("PlayerName") or "")
    chips = "".join(_hit_badge(_CAT_DISPLAY.get(c, c), hi_color, f"strong in {c}")
                    for c in sorted(r["_tcats"] & hi_cats))
    if show_pos:
        chips += "".join(_hit_badge(p, CYAN, f"upgrades your thin {p}")
                         for p in r.get("_tfillpos", []))
    if r.get("_tsell"):
        tip = ("results ahead of his Statcast expected — sell him high"
               if side == "give" else
               "results ahead of his Statcast expected — regression risk (you'd be buying high)")
        chips += _hit_badge("&#9660;", RED, tip)
    elif r.get("_tbuy"):
        tip = ("results behind his Statcast expected — a rebound candidate (think twice before dealing him)"
               if side == "give" else
               "results behind his Statcast expected — positive regression likely, acquire cheap")
        chips += _hit_badge("$", GREEN, tip)
    if r.get("_tptype") == "hit":
        bd = _hitter_score_breakdown(r, best_recent_h, hit_pctile)
    else:
        bd = _pitcher_score_breakdown(r, best_recent_p)
    uid = _bd_uid("trade", nm) if bd else None
    score_html, reveal = _trade_score_reveal(int(round(r["_tscore"])), bd, uid)
    return (f'<div style="margin:3px 0;font-size:12px;color:{TEXT};white-space:nowrap;">'
            f'{logo}<span style="font-weight:600;">{nm}</span> '
            f'{score_html}{chips}</div>{reveal}')


def _verdict_pill(label, color):
    """The Accept/Counter/Decline pill for a pending trade (shared by the section render
    and the glossary so the two can't drift)."""
    return (f'<span style="background:{color};color:#0b1220;font-weight:800;'
            f'font-size:11px;padding:2px 9px;border-radius:10px;">{label}</span>')


def _tradelab_button(partner, give_names, get_names):
    """Deep-link a Pending Trades / Trade Radar card into the hosted Trade Lab with this
    exact deal preloaded, via the same #partner=&give=&get= hash trade_lab.py's
    preloadFromHash() already parses. A plain cross-site link (not the Windows file://
    launch that DATA.preload works around), so no JS is needed on the digest side.
    Returns a bare <a> (no wrapper) meant for the card's flex header row, alongside the
    partner name, so a card doesn't need its own extra row. "" when nothing to preload."""
    give_names = [nm for nm in give_names if nm]
    get_names = [nm for nm in get_names if nm]
    if not (give_names or get_names):
        return ""
    frag = (f"partner={quote(partner)}&give={quote(','.join(give_names))}"
            f"&get={quote(','.join(get_names))}")
    return (f'<a href="{TRADE_LAB_URL}#{frag}" target="_blank" rel="noopener" '
            f'style="color:{ACCENT};font-size:10px;font-weight:700;text-decoration:none;'
            f'letter-spacing:.2px;white-space:nowrap;flex-shrink:0;margin-left:8px;">'
            f'Build in Trade Lab &#8250;</a>')


def _trade_net_summary(base_net, me_net, them_net):
    """One-line net-value hint in a Trade Radar card header, right before the Trade Lab
    link — base (universal tval net), you / them (re-valued by each side's own needs).
    Plain numbers only, no verdict language, nothing to accept/counter/decline."""
    def _seg(label, net):
        color = GREEN if net > 0.1 else (RED if net < -0.1 else MUTED)
        sign = "+" if net >= 0 else ""
        return (f'<span style="color:{MUTED};">{label} </span>'
                f'<span style="color:{color};font-weight:700;">{sign}{net:.2f}</span>')
    return (f'<span style="font-size:9.5px;white-space:nowrap;margin-right:8px;">'
            f'{_seg("base", base_net)} &middot; {_seg("you", me_net)} &middot; '
            f'{_seg("them", them_net)}</span>')


def _pending_verdict(net_val, addresses_need, timing, incoming, star_surrender=False,
                     leaves_me_short=None):
    """Accept / Counter / Decline lean for a pending trade, from the SAME signals
    find_trades ranks on: my value edge (net_val = get − give), whether it addresses a
    real category/positional need, and timing (positive = I sell-high / buy-low, negative
    = a trap I'd be selling a riser / buying a regressor). Returns (label, color, why).
    Only meaningful for INCOMING offers (mine to decide); outgoing gets a status read
    instead. Thresholds mirror the Trade Radar value tilt (±0.1).
    `star_surrender` (from `_deal_star_surrender`) is the MY-side mirror of the Trade Radar
    star-reach: when the offer pries my crown-jewel star at par, an otherwise-ACCEPT is
    downgraded to COUNTER (endowment/star bias — I'd hold out even at category-even value).
    `leaves_me_short` (from `_leaves_position_short`) is the roster-depth mirror: hitter
    positions the deal would strip me below startable depth at (my only C/SS with no body
    back) — also downgrades an ACCEPT to COUNTER (get a replacement before dealing the slot)."""
    trap = timing < 0
    leaves_me_short = leaves_me_short or []
    def _hold():   # the COUNTER override shared by every ACCEPT branch, star-first
        if star_surrender:
            return ("COUNTER", YELLOW, "they're prying your star at par — hold out for more")
        if leaves_me_short:
            _lbl = ", ".join(leaves_me_short)
            return ("COUNTER", YELLOW, f"leaves you thin at {_lbl} — get a replacement first")
        return None
    if net_val >= 0.1 and not trap:
        return _hold() or ("ACCEPT", GREEN, "you win the value" +
                (" and it fills a need" if addresses_need else ""))
    if addresses_need and net_val >= -0.1 and not trap:
        return _hold() or ("ACCEPT", GREEN, "roughly even value and it fills a real need")
    if addresses_need:
        why = ("you'd be paying up" if net_val < -0.1 else "the timing is a trap")
        return ("COUNTER", YELLOW, f"right direction but {why} — ask for more")
    if net_val >= 0.1:
        return _hold() or ("ACCEPT", GREEN, "you win the value")
    return ("DECLINE", RED, "no need addressed and you don't gain value")


def _hitter_fills_need_pos(r, need_pos):
    """Thin hitter positions of mine this player upgrades (value clears my avg there)."""
    if r.get("_tptype") != "hit":
        return set()
    return {pos for pos in (r["_tgroups"] & set(need_pos))
            if r["_tscore"] > need_pos[pos][1]}


def _fmt_trade_expiry(iso, today_str):
    """A short human 'expires in Nd' from the offer's ISO expiration (empty on failure)."""
    if not iso:
        return ""
    try:
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00")).date()
        t = datetime.strptime(today_str, "%Y-%m-%d").date()
        days = (d - t).days
    except Exception:
        return ""
    if days <= 0:
        return "expires today"
    if days == 1:
        return "expires tomorrow"
    return f"expires in {days}d"


def _counter_suggestion(gap, partner_key, pitchers, hitters, my_needs, need_pos,
                        best_recent_p, best_recent_h, hit_pctile, pit_pctile, exclude_keys):
    """When an incoming offer has me overpaying by `gap` (my give value − their get value),
    suggest the single best ADD-ON to request from the partner: a spareable piece that
    closes the value gap and, ideally, helps a category/positional need of mine. Reuses the
    Trade Radar enrich/value machinery so the ask stays realistic (won't request a stud far
    above what evens the deal). Returns a phrase like 'ask them to add X (adds SB, C)'."""
    if gap <= 0.1:
        return ""
    def _roster(source):
        return [r for r in source
                if " ".join((r.get("FantasyTeam") or "").split()) == partner_key
                and int(r.get("Dataset", 0) or 0) == YEAR]
    cands = ([_enrich_trade_player(dict(r), "hit", best_recent_p, best_recent_h, hit_pctile, pit_pctile)
              for r in _roster(hitters)]
             + [_enrich_trade_player(dict(r), "pit", best_recent_p, best_recent_h, hit_pctile, pit_pctile)
                for r in _roster(pitchers)])
    best, best_key = None, None
    for r in cands:
        if _badge_name_key(r.get("PlayerName", "")) in exclude_keys:
            continue
        v = r["_tval"]
        if v <= 0 or v > gap + _TRADE_MAX_EDGE:     # nothing to add / an unrealistic overreach
            continue
        need_hit = bool(r["_tcats"] & my_needs) or bool(_hitter_fills_need_pos(r, need_pos))
        closes   = v >= gap * 0.7
        key = (closes, need_hit, bool(r.get("_tbuy")), -abs(v - gap))
        if best_key is None or key > best_key:
            best_key, best = key, r
    if best is None:
        return ""
    nm   = best.get("PlayerName", "")
    bits = ([_CAT_DISPLAY.get(c, c) for c in sorted(best["_tcats"] & my_needs)]
            + sorted(_hitter_fills_need_pos(best, need_pos)))
    reason = f" (adds {', '.join(bits)})" if bits else " to even the value"
    return f"ask them to add {nm}{reason}"


def _grade_pending_trades(pending, pitchers, hitters, roto, my_team,
                          best_recent_p, best_recent_h, pos_data,
                          hit_pctile, pit_pctile, today_str=""):
    """Grade every real pending offer ONCE (resolution + value/verdict + counter) so the
    section render, the Briefing, and the Week-at-a-Glance headline all read the same
    numbers. Returns a list of graded dicts. Reuses the Trade Radar machinery
    (`_enrich_trade_player`, `_pending_verdict`, `_counter_suggestion`)."""
    if not pending:
        return []

    # YEAR-preferred row lookups (fall back 30→15→7), keyed like the Today's Games block.
    hit_rows, pit_rows = {}, {}
    for _ds in (7, 15, 30, YEAR):
        for _r in hitters:
            if int(_n(_r.get("Dataset")) or 0) == _ds and _r.get("PlayerName"):
                hit_rows[_badge_name_key(_r["PlayerName"])] = _r
        for _r in pitchers:
            if int(_n(_r.get("Dataset")) or 0) == _ds and _r.get("PlayerName"):
                pit_rows[_badge_name_key(_r["PlayerName"])] = _r

    ranks, n = team_category_ranks(roto)
    third = max(1, round(n / 3.0)) if n else 1
    needs_of   = lambda tk: {c for c, rk in ranks.get(tk, {}).items() if rk >= n - third + 1}
    surplus_of = lambda tk: {c for c, rk in ranks.get(tk, {}).items() if rk <= third}
    my_key   = " ".join(my_team.split())
    my_needs, my_surplus = needs_of(my_key), surplus_of(my_key)   # my demand-side context
    _nt    = lambda p: (p.get("n_teams") or n or 1)
    _third = lambda p: max(1, round(_nt(p) / 3.0))
    need_pos = {p["pos"]: ((p.get("rank") or _nt(p)), (p.get("my_avg") or 0))
                for p in pos_data if p.get("ptype") == "hit"
                and (p.get("rank") or _nt(p)) >= _nt(p) - _third(p) + 1}
    my_need_pos    = set(need_pos.keys())
    my_surplus_pos = {p["pos"] for p in pos_data if p.get("ptype") == "hit"
                      and (p.get("rank") or _nt(p)) <= _third(p)}
    my_pos_count = _team_position_counts(hitters, my_key)   # redundancy + depth guard: bodies per position

    def _resolve(entry):
        k = _badge_name_key(entry.get("name", ""))
        if k in hit_rows:
            return _enrich_trade_player(dict(hit_rows[k]), "hit", best_recent_p,
                                        best_recent_h, hit_pctile, pit_pctile)
        if k in pit_rows:
            return _enrich_trade_player(dict(pit_rows[k]), "pit", best_recent_p,
                                        best_recent_h, hit_pctile, pit_pctile)
        return None

    graded = []
    for tr in pending:
        partner  = " ".join((tr.get("partner") or "").split())
        incoming = bool(tr.get("incoming"))
        get_rows  = [(_resolve(e), e.get("name")) for e in (tr.get("get") or [])]
        give_rows = [(_resolve(e), e.get("name")) for e in (tr.get("give") or [])]
        ins  = [r for (r, nm) in get_rows if r is not None]
        outs = [r for (r, nm) in give_rows if r is not None]
        for r in ins:
            r["_tfillpos"] = sorted(_hitter_fills_need_pos(r, need_pos))

        net_val = sum(r["_tval"] for r in ins) - sum(r["_tval"] for r in outs)   # base value edge
        # Demand-side net from MY POV (+ = I win by MY needs): incoming valued by my needs
        # minus outgoing. Drives the ACCEPT/COUNTER/DECLINE verdict so it's roster-aware — a
        # need-filling piece is worth accepting even at slight base-value cost. The displayed
        # `value` phrase + the counter-gap stay on BASE value (the universal read).
        _mm = lambda r: _need_mult(r, my_needs, my_surplus, my_need_pos, my_surplus_pos)
        net_me = sum(r["_tval"] * _mm(r) for r in ins) - sum(r["_tval"] * _mm(r) for r in outs)
        gcov = set().union(*[r["_tcats"] for r in ins]) & my_needs if ins else set()
        gpos = (_non_redundant_get_pos(set().union(*[set(r.get("_tfillpos", [])) for r in ins]),
                                       outs, ins, my_pos_count) if ins else set())
        scov = set().union(*[r["_tcats"] for r in outs]) & needs_of(partner) if outs else set()
        addresses_need = bool(gcov or gpos)
        timing = (sum(1 for r in outs if r.get("_tsell")) + sum(1 for r in ins if r.get("_tbuy"))
                  - sum(1 for r in outs if r.get("_tbuy")) - sum(1 for r in ins if r.get("_tsell")))
        value = ("you win the value" if net_val > 0.1 else
                 "even value" if net_val >= -0.1 else "you pay up")

        star_surrender = _deal_star_surrender(ins, outs, net_val)   # star reluctance stays on base value
        # DEPTH FLOOR (soft — a real offer isn't vetoed, it's flagged): would accepting leave ME
        # short at a hitter position (I give my only C/SS with no same-position body back)? If so,
        # an otherwise-ACCEPT downgrades to COUNTER — get a replacement before dealing the slot.
        leaves_me_short = sorted(_leaves_position_short(my_pos_count, outs, ins)) if incoming else []
        verdict = _pending_verdict(net_me, addresses_need, timing, incoming, star_surrender,
                                   leaves_me_short) if incoming else None
        counter = ""
        if verdict and verdict[0] == "COUNTER":
            exclude = {_badge_name_key(nm) for (_r, nm) in get_rows + give_rows}
            counter = _counter_suggestion(-net_val, partner, pitchers, hitters, my_needs,
                                          need_pos, best_recent_p, best_recent_h,
                                          hit_pctile, pit_pctile, exclude)

        gains = ([_CAT_DISPLAY.get(c, c) for c in sorted(gcov, key=lambda c: -ranks[my_key][c])]
                 + [f"{p} slot" for p in sorted(gpos, key=lambda p: -need_pos[p][0])])
        graded.append({
            "partner": partner, "incoming": incoming,
            "expires": tr.get("expires", ""), "expiry_str": _fmt_trade_expiry(tr.get("expires", ""), today_str),
            "get_rows": get_rows, "give_rows": give_rows,
            "get_names": [nm for (_r, nm) in get_rows], "give_names": [nm for (_r, nm) in give_rows],
            "net_val": net_val, "value": value, "verdict": verdict, "counter": counter,
            "scov": scov, "gcov": gcov, "get_lbl": ", ".join(gains) or "depth",
        })
    return graded


def _pending_headline(g, brief=False):
    """One-line incoming-offer headline for the Briefing / Week-at-a-Glance (plain text +
    light inline spans). `brief=True` trims to the essentials for the email body."""
    label, color, why = g["verdict"] if g["verdict"] else ("REVIEW", ACCENT, "")
    get_s  = ", ".join(g["get_names"]) or "—"
    give_s = ", ".join(g["give_names"]) or "—"
    pill = f'<b style="color:{color};">{label}</b>'
    exp  = (f' <span style="color:{RED};">&middot; {g["expiry_str"]}</span>'
            if g.get("expiry_str") else "")
    head = (f'&#129309; Trade from <b>{g["partner"]}</b>: get <b>{get_s}</b> for '
            f'<b>{give_s}</b> — {pill}{exp}')
    if g.get("counter"):
        head += f' <span style="color:{MUTED};">&rarr; counter: {g["counter"]}</span>'
    elif not brief and why:
        head += f' <span style="color:{MUTED};">({why})</span>'
    return head


def build_pending_trades_section(graded, best_recent_p, best_recent_h, hit_pctile, team_logos=None):
    """Render the Pending Trades cards from pre-graded offers (`_grade_pending_trades`).
    Distinguishes an INCOMING offer (Accept/Counter/Decline verdict + a counter suggestion
    when it's a COUNTER) from an OUTGOING one (awaiting the partner). Returns "" when
    nothing is pending."""
    if not graded:
        return ""
    team_logos = team_logos or {}

    def _plain_line(name):
        return (f'<div style="margin:3px 0;font-size:12px;color:{MUTED};white-space:nowrap;">'
                f'<span style="font-weight:600;">{name}</span> '
                f'<span style="font-size:10px;">(no data)</span></div>')

    cards = []
    for g in graded:
        partner, incoming = g["partner"], g["incoming"]
        give_html = "".join(
            (_trade_player_line(r, g["scov"], MUTED, "give", best_recent_p=best_recent_p,
                                best_recent_h=best_recent_h, hit_pctile=hit_pctile)
             if r is not None else _plain_line(nm))
            for (r, nm) in g["give_rows"])
        get_html = "".join(
            (_trade_player_line(r, g["gcov"], ACCENT, "get", show_pos=True, best_recent_p=best_recent_p,
                                best_recent_h=best_recent_h, hit_pctile=hit_pctile)
             if r is not None else _plain_line(nm))
            for (r, nm) in g["get_rows"])

        exp = (f' <span style="color:{RED};font-weight:700;">&middot; {g["expiry_str"]}</span>'
               if g.get("expiry_str") else "")
        if incoming:
            label, vcolor, why = g["verdict"]
            tag = f'<span style="color:{ACCENT};font-weight:700;">OFFER TO YOU</span>'
            verdict_html = f'{_verdict_pill(label, vcolor)} <span style="color:{MUTED};">{why}</span>'
            counter_html = (f'<div style="color:{YELLOW};margin-top:3px;">&#128161; Counter: '
                            f'{g["counter"]}</div>' if g.get("counter") else "")
        else:
            tag = f'<span style="color:{MUTED};font-weight:700;">YOUR OFFER</span>'
            verdict_html = (f'<span style="color:{MUTED};">Awaiting '
                            f'<span style="color:{TEXT};font-weight:700;">{partner or "partner"}</span>'
                            f' &middot; {g["value"]} from your side</span>')
            counter_html = ""
        logo = fantasy_logo(team_logos.get(partner, ""), size=20, team_name=partner)
        tradelab_btn = _tradelab_button(partner, g["give_names"], g["get_names"])
        cards.append(
            f'<div style="background:{SURFACE};border:1px solid {BORDER};border-radius:8px;'
            f'padding:12px 14px;margin-bottom:12px;">'
            f'<div style="display:flex;justify-content:space-between;align-items:center;'
            f'font-size:11px;color:{MUTED};margin-bottom:8px;">'
            f'<span>{tag} &middot; with {logo}'
            f'<span style="color:{TEXT};font-weight:700;">{partner}</span>{exp}</span>'
            f'{tradelab_btn}</div>'
            f'<table style="width:100%;border-collapse:collapse;"><tr>'
            f'<td style="width:47%;vertical-align:top;">'
            f'<div style="font-size:9px;font-weight:700;color:{RED};text-transform:uppercase;'
            f'letter-spacing:.5px;margin-bottom:3px;">You give</div>{give_html}</td>'
            f'<td style="width:6%;text-align:center;color:{MUTED};font-size:17px;vertical-align:middle;">&#8644;</td>'
            f'<td style="width:47%;vertical-align:top;">'
            f'<div style="font-size:9px;font-weight:700;color:{GREEN};text-transform:uppercase;'
            f'letter-spacing:.5px;margin-bottom:3px;">You get</div>{get_html}</td>'
            f'</tr></table>'
            f'<div style="font-size:11px;margin-top:8px;border-top:1px solid {BORDER};'
            f'padding-top:7px;">{verdict_html}{counter_html}'
            f'<div style="color:{MUTED};margin-top:3px;">Upgrades your '
            f'<span style="color:{ACCENT};font-weight:700;">{g["get_lbl"]}</span></div></div>'
            f'</div>'
        )

    n_in = sum(1 for g in graded if g["incoming"])
    sub = (f'{len(graded)} live offer{"s" if len(graded) != 1 else ""}'
           + (f' &middot; {n_in} awaiting your decision' if n_in else ' &middot; awaiting partners'))
    return section_head("Pending Trades", sub) + "".join(cards)


def build_trade_radar(pitchers, hitters, roto, my_team, best_recent_p, best_recent_h,
                      pos_data, hit_pctile, pit_pctile, team_logos=None):
    # Trade Radar is prescriptive ("go send this") — only ever surface deals a rival would
    # realistically accept (see find_trades' realistic_only docstring).
    trades = find_trades_combined(pitchers, hitters, roto, my_team, best_recent_p, best_recent_h,
                                  pos_data, hit_pctile, pit_pctile, realistic_only=True)
    if not trades:
        return ""
    team_logos = team_logos or {}
    cards = []
    for t in trades:
        give = "".join(_trade_player_line(o, set(t["send_cats"]), MUTED, "give",
                                          best_recent_p=best_recent_p, best_recent_h=best_recent_h,
                                          hit_pctile=hit_pctile) for o in t["outs"])
        get_ = "".join(_trade_player_line(i, set(t["get_cats"]), ACCENT, "get", show_pos=True,
                                          best_recent_p=best_recent_p, best_recent_h=best_recent_h,
                                          hit_pctile=hit_pctile) for i in t["ins"])
        gains = ([_CAT_DISPLAY.get(c, c) for c in t["get_cats"]]
                 + [f"{p} slot" for p in t.get("get_pos", [])])
        get_lbl  = ", ".join(gains) or "depth"
        send_lbl = ", ".join(_CAT_DISPLAY.get(c, c) for c in t["send_cats"])
        net = t.get("net_val", 0.0)
        value, accept, acc_color = _trade_tilt(net, t["ins"], t["outs"], net_them=t.get("net_them"))
        thesis = ("sell-high" if t.get("sell_out") else "") + \
                 (" · " if t.get("sell_out") and t.get("buy_in") else "") + \
                 ("buy-low" if t.get("buy_in") else "")
        if thesis:
            value += f" &middot; {thesis}"
        thin_html = (f'<span style="color:{ORANGE};"> &middot; {t["thin_note"]}</span>'
                     if t.get("thin_note") else "")
        # Realism chip in the card header — the RIVAL's-POV read on whether they'd accept
        # (a fair/pay-up deal is realistic; a value-tilted one is a tougher sell). This is
        # what turns "which of these would actually land?" into a glance.
        acc_chip = (f'<span style="font-size:8.5px;font-weight:700;letter-spacing:.4px;'
                    f'text-transform:uppercase;color:{acc_color};border:1px solid {acc_color};'
                    f'border-radius:3px;padding:1px 5px;margin-left:6px;">{accept}</span>')
        # Quiet one-line net hint (Base / You / Them) — see _trade_net_summary.
        base_give = sum(o["_tval"] for o in t["outs"])
        base_get  = sum(i["_tval"] for i in t["ins"])
        net_summary = _trade_net_summary(
            base_get - base_give,
            t.get("my_get_val", base_get) - t.get("my_give_val", base_give),
            t.get("their_get_val", base_get) - t.get("their_give_val", base_give))
        logo = fantasy_logo(team_logos.get(t["team"], ""), size=20, team_name=t["team"])
        tradelab_btn = _tradelab_button(t["team"],
                                        [o.get("PlayerName") for o in t["outs"]],
                                        [i.get("PlayerName") for i in t["ins"]])
        cards.append(
            f'<div style="background:{SURFACE};border:1px solid {BORDER};border-radius:8px;'
            f'padding:12px 14px;margin-bottom:12px;">'
            f'<div style="display:flex;justify-content:space-between;align-items:center;'
            f'font-size:11px;color:{MUTED};margin-bottom:8px;">'
            f'<span>with {logo}'
            f'<span style="color:{TEXT};font-weight:700;">{t["team"]}</span>{acc_chip}</span>'
            f'<span>{net_summary}{tradelab_btn}</span></div>'
            f'<table style="width:100%;border-collapse:collapse;"><tr>'
            f'<td style="width:47%;vertical-align:top;">'
            f'<div style="font-size:9px;font-weight:700;color:{RED};text-transform:uppercase;'
            f'letter-spacing:.5px;margin-bottom:3px;">You give</div>{give}</td>'
            f'<td style="width:6%;text-align:center;color:{MUTED};font-size:17px;vertical-align:middle;">&#8644;</td>'
            f'<td style="width:47%;vertical-align:top;">'
            f'<div style="font-size:9px;font-weight:700;color:{GREEN};text-transform:uppercase;'
            f'letter-spacing:.5px;margin-bottom:3px;">You get</div>{get_}</td>'
            f'</tr></table>'
            f'<div style="font-size:11px;color:{MUTED};margin-top:8px;border-top:1px solid {BORDER};'
            f'padding-top:7px;">Upgrades your '
            f'<span style="color:{ACCENT};font-weight:700;">{get_lbl}</span>; they shore up '
            f'<span style="color:{TEXT};">{send_lbl}</span>'
            f'<span style="color:{MUTED};"> &middot; {value}</span>{thin_html}</div>'
            f'</div>'
        )
    n_fair = sum(1 for t in trades if t.get("lane") == "fair")
    sub = (f'{len(trades)} swap{"s" if len(trades) != 1 else ""} that fix a rival&rsquo;s '
           f'category need &mdash; <span style="color:{GREEN};">realistic</span>, near-even '
           f'asks that actually land the upgrade, plus value plays where a rival is motivated'
           if n_fair else
           f'{len(trades)} swap{"s" if len(trades) != 1 else ""} that fix a rival&rsquo;s '
           f'category need while tilting value your way (buy-low / sell-high where possible)')
    return section_head("Trade Radar", sub) + "".join(cards)


__all__ = [n for n in dir()
           if n not in _EXCLUDE and n != '_EXCLUDE' and not n.startswith('__')]
