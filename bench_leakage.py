"""
bench_leakage.py  -  PROTOTYPE (feeds the Monday recap's Lineup Efficiency section)
===================================================================================
Reconstructs last week's DAILY lineup decisions and reports, for MY team and my
matchup OPPONENT:

  1. BATTER BENCH LEAKAGE - counting-stat production (R/HR/RBI/SB) a hitter put up
     while sitting in a BE slot, so it never counted. Each miss is classified by
     OPPORTUNITY COST:
       [OPEN]  - he could have been slotted into an open active spot (or a legal
                 reshuffle) WITHOUT benching anyone => a genuinely free miss.
       [SWAP]  - the active lineup was full at his eligible spots; starting him
                 meant sitting a specific starter (shown with that day's line) =>
                 a judgment call, netted against the guy you'd have benched.
     "Recoverable" totals count OPEN days only.

  2. PITCHER BLOWUPS IN ACTIVE SLOT - a start with 5+ ER (or 4+ ER in <3 IP) that
     counted toward ERA/WHIP, cross-referenced with the transaction log to flag
     "imploded then dropped" (damage already banked).

Mechanism: `mRoster` fetched with `scoringPeriodId=<day>` returns each roster
entry's `lineupSlotId` AS SET THAT DAY + that day's stat split (categories leagues
expose no per-player lineup through box_scores). Opportunity cost uses the league's
lineupSlotCounts + each player's eligibleSlots via a max-bipartite-matching feasibility
check.

Run:  python bench_leakage.py
"""
from datetime import datetime, timedelta

import fetch_data as fd
from espn_api.baseball.constant import POSITION_MAP

# stat-id string keys (STATS_MAP)
AB, H, HR, TB, BB_H, R, RBI, SB, B_SO = "0", "1", "5", "8", "10", "20", "21", "23", "27"
OUTS, GS, P_H, P_BB, ER, K = "34", "33", "37", "39", "45", "48"

PIT_SLOT_IDS = {13, 14, 15}   # P, SP, RP
BENCH_ID, IL_ID = 16, 17


def _f(d, k):
    try:
        return float(d.get(k))
    except (TypeError, ValueError):
        return 0.0


def _day_split(pl, sp):
    for s in pl.get("stats", []):
        if s.get("scoringPeriodId") == sp and s.get("statSourceId") == 0 and s.get("stats"):
            return s["stats"]
    return {}


def _fmt_ip(outs):
    whole, rem = divmod(int(round(outs)), 3)
    return f"{whole}.{rem}"


def _bipartite_full(player_eligs):
    """player_eligs: list of iterable-of-slot-instance-indices, one per player.
    Returns True iff every player can be simultaneously matched to a distinct slot."""
    match = {}  # slot_idx -> player_idx

    def aug(p, seen):
        for s in player_eligs[p]:
            if s in seen:
                continue
            seen.add(s)
            if s not in match or aug(match[s], seen):
                match[s] = p
                return True
        return False

    for p in range(len(player_eligs)):
        if not aug(p, set()):
            return False
    return True


def _slot_instances(slot_counts):
    """Return (hit_instances, hit_slot_ids). hit_instances is a list of slot-type
    ids expanded by count for starting HITTER slots (pitcher/BE/IL excluded)."""
    hit = []
    hit_ids = set()
    for sid_str, cnt in slot_counts.items():
        sid = int(sid_str)
        if sid in PIT_SLOT_IDS or sid in (BENCH_ID, IL_ID) or cnt <= 0:
            continue
        hit_ids.add(sid)
        hit.extend([sid] * cnt)
    return hit, hit_ids


def _is_pitcher(elig):
    return bool(set(elig) & PIT_SLOT_IDS) and not (set(elig) & {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 19})


def _day_val(st):
    """Cheap offensive-value scalar for picking the weakest displaceable starter."""
    return _f(st, TB) + _f(st, BB_H) + _f(st, SB)


def audit_team(lg, team_id, prev_days, dates, hit_instances, hit_ids):
    """Return dict: {hit_leak, gross, recoverable, blowups}."""
    hit_leak = {}   # name -> aggregate + per-notable-day classification
    pit_lines = []

    for sp, dt in zip(prev_days, dates):
        data = lg.espn_request.league_get(params={"view": "mRoster", "scoringPeriodId": sp})
        mt = next((t for t in data.get("teams", []) if t.get("id") == team_id), None)
        if not mt:
            continue
        entries = mt["roster"]["entries"]

        # active hitters this day (for opportunity-cost feasibility)
        active_hit = []   # (name, eligible-set)
        for e in entries:
            pl = e["playerPoolEntry"]["player"]
            slot_id = e.get("lineupSlotId")
            elig = pl.get("eligibleSlots", [])
            if slot_id not in (BENCH_ID, IL_ID) and slot_id not in PIT_SLOT_IDS and not _is_pitcher(elig):
                active_hit.append((pl.get("fullName", "?"),
                                   {s for s in elig if s in hit_ids},
                                   _day_split(pl, sp)))

        def _fits_without_benching(cand_elig):
            """Could a hitter with cand_elig be added to today's active hitters
            (open slot or legal reshuffle) without benching anyone?"""
            base = [ [i for i, styp in enumerate(hit_instances) if styp in he] for _, he, _ in active_hit ]
            cand = [i for i, styp in enumerate(hit_instances) if styp in cand_elig]
            return _bipartite_full(base + [cand])

        for e in entries:
            pl = e["playerPoolEntry"]["player"]
            nm = pl.get("fullName", "?")
            slot_id = e.get("lineupSlotId")
            elig = pl.get("eligibleSlots", [])
            st = _day_split(pl, sp)
            if not st:
                continue
            is_pitch = _is_pitcher(elig)

            # ---- BATTER bench leakage ----
            if not is_pitch and slot_id == BENCH_ID and _f(st, AB) > 0:
                agg = hit_leak.setdefault(nm, {"R": 0, "HR": 0, "RBI": 0, "SB": 0,
                                               "H": 0, "AB": 0, "B_SO": 0,
                                               "net": {"R": 0, "HR": 0, "RBI": 0, "SB": 0},
                                               "days": []})
                for cat, key in (("R", R), ("HR", HR), ("RBI", RBI), ("SB", SB), ("H", H), ("AB", AB), ("B_SO", B_SO)):
                    agg[cat] += _f(st, key)

                notable = _f(st, HR) or _f(st, SB) or _f(st, R) >= 2 or _f(st, RBI) >= 2
                if not notable:
                    continue   # nothing actionable to recover on a quiet bench day

                cand_elig = {s for s in elig if s in hit_ids}
                free = _fits_without_benching(cand_elig)
                # On a full lineup, starting him benches the weakest startable regular;
                # net recoverable = his line minus that guy's line that day (open slot => minus nothing).
                displaced_st, displaced_nm = {}, None
                if not free:
                    overlap = [(anm, ast) for anm, ae, ast in active_hit if ae & cand_elig]
                    if overlap:
                        displaced_nm, displaced_st = min(overlap, key=lambda x: _day_val(x[1]))[0], \
                                                     min(overlap, key=lambda x: _day_val(x[1]))[1]
                for cat, key in (("R", R), ("HR", HR), ("RBI", RBI), ("SB", SB)):
                    agg["net"][cat] += _f(st, key) - _f(displaced_st, key)

                line = f"{int(_f(st,H))}-{int(_f(st,AB))}"
                extra = []
                if _f(st, HR):  extra.append(f"{int(_f(st,HR))} HR")
                if _f(st, RBI): extra.append(f"{int(_f(st,RBI))} RBI")
                if _f(st, R):   extra.append(f"{int(_f(st,R))} R")
                if _f(st, SB):  extra.append(f"{int(_f(st,SB))} SB")
                if free:
                    tag = "[OPEN]"
                elif displaced_nm:
                    dl = f"{int(_f(displaced_st,H))}-{int(_f(displaced_st,AB))}"
                    tag = f"[SWAP vs {displaced_nm} {dl}]"
                else:
                    tag = "[SWAP]"
                agg["days"].append(f"{dt:%a %m/%d} {tag}: {line}"
                                   + (f" ({', '.join(extra)})" if extra else ""))

            # ---- PITCHER active-slot appearance ----
            if is_pitch and slot_id not in (BENCH_ID, IL_ID) and _f(st, OUTS) > 0:
                pit_lines.append({"name": nm, "date": dt, "outs": _f(st, OUTS),
                                  "er": _f(st, ER), "k": _f(st, K),
                                  "h": _f(st, P_H), "bb": _f(st, P_BB)})

    gross = {c: sum(a[c] for a in hit_leak.values()) for c in ("R", "HR", "RBI", "SB")}
    net = {c: sum(a["net"][c] for a in hit_leak.values()) for c in ("R", "HR", "RBI", "SB")}
    blowups = [p for p in pit_lines if p["er"] >= 5 or (p["er"] >= 4 and p["outs"] < 9)]
    return {"hit_leak": hit_leak, "gross": gross, "net": net, "blowups": blowups}


def team_drops(lg, team_name):
    try:
        acts = lg.recent_activity(size=150)
    except Exception:
        return []
    out = []
    tgt = " ".join(team_name.split())
    for act in acts:
        for team_obj, tx_type, player_obj in act.actions:
            tn = " ".join((team_obj.team_name if team_obj else "").split())
            if tn == tgt and "DROP" in str(tx_type).upper():
                out.append((str(player_obj), datetime.fromtimestamp(act.date / 1000)))
    return out


def print_report(label, res, drops):
    print("\n" + "=" * 68)
    print(f"  {label}")
    print("=" * 68)

    print("\n1) BATTER BENCH LEAKAGE  (production while on BE - did NOT count)")
    print("-" * 68)
    hl = res["hit_leak"]
    if not hl:
        print("   None - every hitter who played was in the active lineup.")
    else:
        for nm, a in sorted(hl.items(), key=lambda kv: (kv[1]["HR"], kv[1]["RBI"], kv[1]["R"]), reverse=True):
            print(f"   {nm:22s}  {int(a['H'])}-{int(a['AB'])} | "
                  f"{int(a['R'])} R  {int(a['HR'])} HR  {int(a['RBI'])} RBI  {int(a['SB'])} SB")
            for d in a["days"]:
                print(f"       > {d}")
        g, n = res["gross"], res["net"]
        print("-" * 68)
        print(f"   GROSS on bench:      {int(g['R'])} R | {int(g['HR'])} HR | {int(g['RBI'])} RBI | {int(g['SB'])} SB   (raw)")
        print(f"   NET RECOVERABLE:   {n['R']:+.0f} R | {n['HR']:+.0f} HR | {n['RBI']:+.0f} RBI | {n['SB']:+.0f} SB"
              "   (his big days, minus the bat you'd have sat)")

    print("\n2) PITCHER BLOWUPS IN ACTIVE SLOT  (ER/WHIP damage counted)")
    print("-" * 68)
    if not res["blowups"]:
        print("   No active-slot starts with 5+ ER (or 4+ ER in <3 IP).")
    else:
        for p in sorted(res["blowups"], key=lambda x: x["er"], reverse=True):
            print(f"   {p['name']:22s}  {p['date']:%a %m/%d}  {_fmt_ip(p['outs'])} IP, "
                  f"{int(p['er'])} ER, {int(p['k'])} K (+{int(p['h'])} H, {int(p['bb'])} BB to WHIP)")
            after = [d for nm, d in drops if nm == p["name"] and d.date() >= p["date"] and (d.date() - p["date"]).days <= 4]
            if after:
                dd = min(after)
                lag = (dd.date() - p["date"]).days
                when = "same day" if lag == 0 else f"{lag}d later"
                print(f"       > DROPPED {dd:%a %m/%d} ({when}) - imploded then cut; damage already banked.")


def main():
    lg = fd.connect_espn()
    if not lg:
        raise SystemExit("Could not connect to ESPN")

    my_norm = " ".join(fd.MY_TEAM_NAME.split())
    id_to_name = {t.team_id: t.team_name for t in lg.teams}
    myid = next((tid for tid, nm in id_to_name.items() if " ".join(nm.split()) == my_norm), None)
    if myid is None:
        raise SystemExit(f"Could not find team {fd.MY_TEAM_NAME!r}")

    # last completed matchup + its 7 daily scoring periods
    today = datetime.now().date()
    today_sp = int(lg.scoringPeriodId)
    this_mon_sp = today_sp - today.weekday()
    prev_days = list(range(this_mon_sp - 7, this_mon_sp))
    prev_mon_date = today - timedelta(days=today.weekday() + 7)
    dates = [prev_mon_date + timedelta(days=i) for i in range(7)]

    # opponent from last week's box score
    prev_week = int(lg.currentMatchupPeriod) - 1
    oppid = None
    for b in lg.box_scores(prev_week):
        h = getattr(b.home_team, "team_id", None)
        a = getattr(b.away_team, "team_id", None)
        if h == myid:
            oppid = a
        elif a == myid:
            oppid = h
        if oppid:
            break

    slot_counts = lg.espn_request.league_get(params={"view": "mSettings"})["settings"]["rosterSettings"]["lineupSlotCounts"]
    hit_instances, hit_ids = _slot_instances(slot_counts)

    print("#" * 68)
    print(f"  LINEUP EFFICIENCY  -  week of {dates[0]:%b %d} - {dates[-1]:%b %d}, {dates[-1]:%Y}")
    print(f"  (scoring periods {prev_days[0]}-{prev_days[-1]})")
    print("#" * 68)

    my_res = audit_team(lg, myid, prev_days, dates, hit_instances, hit_ids)
    print_report(f"MY TEAM - {id_to_name[myid].strip()}", my_res, team_drops(lg, id_to_name[myid]))

    if oppid:
        opp_res = audit_team(lg, oppid, prev_days, dates, hit_instances, hit_ids)
        print_report(f"OPPONENT - {id_to_name[oppid].strip()}", opp_res, team_drops(lg, id_to_name[oppid]))

        print("\n" + "=" * 68)
        print("  HEAD-TO-HEAD LINEUP MANAGEMENT (net recoverable left on bench)")
        print("=" * 68)
        m, o = my_res["net"], opp_res["net"]
        print(f"   {id_to_name[myid].strip():24s} net {m['HR']:+.0f} HR / {m['RBI']:+.0f} RBI / {m['SB']:+.0f} SB / {m['R']:+.0f} R")
        print(f"   {id_to_name[oppid].strip():24s} net {o['HR']:+.0f} HR / {o['RBI']:+.0f} RBI / {o['SB']:+.0f} SB / {o['R']:+.0f} R")

    print()


if __name__ == "__main__":
    main()
