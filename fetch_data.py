"""
fantasy_baseball/fetch_data.py
================================
Replaces Baseball_All.ipynb â€” no Google Sheets, no Tableau, no manual auth.

Run:  python fetch_data.py
Output: data/snapshot.json  (consumed by send_digest.py)

Dependencies:
    python -m pip install pandas requests espn_api pybaseball
"""

import json
import os
import re
import sys
import unicodedata
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

warnings.filterwarnings("ignore")

# â”€â”€ CONFIG â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Credentials are read from environment variables (GitHub Actions secrets) and
# fall back to the hardcoded values below for local development.
_ESPN_S2_DEFAULT = (
    "AEB1C0RkPOTa50dJzQQG4BZuBNx1tKiEP7xREJDbsYuebz81BVSgMKZRNSRBBE2tKD"
    "%2BLiUkt456IRYYqP3pa%2F8WNuzkZWVEoezqYBu9NUMzwY0nDmTWzUwXX8%2BsLi78"
    "zCaKUe41LX4ILlUG7%2BFnBgprAKjjCpQNsHRxhh6KlH11jAWNZANuteehxSckaybxi"
    "%2B%2Fk3uFoABmTkzuw%2FHR4lHvXQb89k31ni6O7kSfKdbQgjWgpr3FFUWKnwUu%2F"
    "ZsuGnzKl7Cin8yPMZ1adpgH6dNF0D"
)
ESPN_CONFIG = {
    "league_id": 277836,
    "year":      2026,
    "swid":      os.getenv("ESPN_SWID", "{389786AB-5AC8-47E0-AF7A-771B7B626E04}"),
    "espn_s2":   os.getenv("ESPN_S2",   _ESPN_S2_DEFAULT),
}

# Your team name on ESPN (used to identify your players in the digest)
MY_TEAM_NAME = "Guerrero Warfare"   # e.g. "Sam's Sluggers" â€” leave blank to auto-detect first team

STAT_RANGES  = [7, 15, 30, ESPN_CONFIG["year"]]
SP_DAYS_OUT  = 7          # how many days of probable starters to pull

# Player name patches (ESPN name â†’ FantasyPros name)
PITCHER_NAME_PATCHES = {"Nestor Cortes": "Nestor Cortes Jr."}
HITTER_NAME_PATCHES  = {
    "Cedric Mullins":  "Cedric Mullins II",
    "Victor Scott II": "Victor Scott",
}

OUTPUT_DIR  = Path(__file__).parent / "data"
OUTPUT_FILE = OUTPUT_DIR / "snapshot.json"
CURRENT_YEAR = ESPN_CONFIG["year"]

# â”€â”€ HELPERS â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def innings_to_decimal(ip_str):
    try:
        parts = str(ip_str).split(".")
        return int(parts[0]) + (int(parts[1]) / 3 if len(parts) > 1 else 0)
    except Exception:
        return 0.0


def extract_player_name(raw):
    m = re.search(r"^(.*?)\s*\(", raw)
    return m.group(1).strip() if m else raw


def extract_team(raw):
    m = re.search(r"\((.*?)\)", raw)
    return m.group(1).split()[0] if m else ""


def extract_fp_position(raw):
    """Extract position(s) from FantasyPros player string: 'Name (TEAM - POS,POS)' â†’ 'POS, POS'."""
    m = re.search(r"\(.*?-\s*(.*?)\)", raw)
    if not m:
        return ""
    return ", ".join(p.strip() for p in m.group(1).split(","))


def log(msg):
    print(f"  {msg}", flush=True)


def lf_to_name(x):
    """Convert 'Last, First' format to 'First Last', stripping accents to match FantasyPros ASCII names."""
    if isinstance(x, str) and ", " in x:
        parts = x.split(", ", 1)
        name = f"{parts[1].strip()} {parts[0].strip()}"
        return "".join(c for c in unicodedata.normalize("NFD", name) if unicodedata.category(c) != "Mn")
    return x


_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def _name_key(name):
    """Normalized join key for roster matching: accent-stripped, lowercased, and with
    trailing generational suffixes (Jr./Sr./II/III/…) and punctuation removed. Lets
    FantasyPros 'Luis Garcia' match ESPN 'Luis García Jr.' without a per-player patch.
    Keeps at least the first+last token so it never collapses a real name."""
    if not isinstance(name, str):
        return ""
    s = "".join(c for c in unicodedata.normalize("NFD", name) if unicodedata.category(c) != "Mn")
    toks = s.lower().replace(".", " ").replace(",", " ").split()
    while len(toks) > 2 and toks[-1] in _NAME_SUFFIXES:
        toks.pop()
    return " ".join(toks)


def merge_on_name(fp, right, cols, how="left"):
    """Merge right[cols] onto fp by exact PlayerName, then fill any rows the exact match
    missed using an accent/suffix-insensitive key (_name_key). Exact matches always win;
    the fallback only fills NaNs, so it can add roster/FA matches but never change or
    remove an existing one. To avoid guessing between name-twins (e.g. the several MLB
    'Luis Garcia' pitchers), a key is only trusted when it maps to a single player on
    BOTH sides — ambiguous keys are left as-is."""
    merged = fp.merge(right[cols], on="PlayerName", how=how)
    val_cols = [c for c in cols if c != "PlayerName"]

    # Right side: key -> row, but only keys that map to exactly one player.
    r2 = right[["PlayerName", *val_cols]].copy()
    r2["_k"] = r2["PlayerName"].map(_name_key)
    r2 = r2[r2["_k"] != ""]
    r2 = r2[r2.groupby("_k")["PlayerName"].transform("nunique") == 1]
    r2 = r2.drop_duplicates("_k").set_index("_k")

    # fp side: keys that are ambiguous among distinct fp players (never rescue those).
    # Build fkeys from `merged` (not `fp`) so its index matches the `missing` mask below —
    # fp.merge resets to a clean RangeIndex, so a non-default fp index would misalign.
    fkeys = merged["PlayerName"].map(_name_key)
    ambig = set(pd.Series(fp["PlayerName"].values, index=fp["PlayerName"].map(_name_key).values)
                .groupby(level=0).nunique().loc[lambda s: s > 1].index)

    for vc in val_cols:
        if vc not in r2.columns:
            continue
        missing = merged[vc].isna()
        if not missing.any():
            continue
        cand = fkeys[missing]
        cand = cand[(cand != "") & (~cand.isin(ambig)) & (cand.isin(r2.index))]
        # Scalar .at assignment (not a vectorized .loc=Series) so list-valued columns
        # like PSP_Dates fill correctly; cand is only the handful of rescued rows.
        for idx, k in cand.items():
            merged.at[idx, vc] = r2.at[k, vc]
    return merged


# â”€â”€ ESPN CONNECTION â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def connect_espn():
    try:
        from espn_api.baseball import League
        league = League(
            league_id=ESPN_CONFIG["league_id"],
            year=ESPN_CONFIG["year"],
            espn_s2=ESPN_CONFIG["espn_s2"],
            swid=ESPN_CONFIG["swid"],
        )
        log(f"ESPN connected â€” {len(league.teams)} teams")
        return league
    except Exception as e:
        log(f"ESPN connection failed: {e}")
        return None


# â”€â”€ FANTASYPROS SCRAPER â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def fetch_fantasypros(player_type: str) -> pd.DataFrame:
    """player_type: 'pitchers' or 'hitters'"""
    frames = []
    for rng in STAT_RANGES:
        url = f"https://www.fantasypros.com/mlb/stats/{player_type}.php?range={rng}"
        try:
            df = pd.read_html(url)[0]
            df["PlayerName"]   = df["Player"].apply(extract_player_name)
            df["Team"]         = df["Player"].apply(extract_team)
            df["FP_Position"]  = df["Player"].apply(extract_fp_position)
            df["Dataset"]      = rng
            df.dropna(subset=["Team"], inplace=True)
            frames.append(df)
            log(f"  FantasyPros {player_type} range={rng}: {len(df)} rows")
        except Exception as e:
            log(f"  FantasyPros {player_type} range={rng} FAILED: {e}")
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


# â”€â”€ ESPN ROSTER / FA HELPERS â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

PITCHER_SLOTS = {"P", "SP", "RP"}
HITTER_SLOTS  = {"C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "DH", "OF", "UTIL"}


def is_pitcher(player):
    return any(s in PITCHER_SLOTS for s in (player.eligibleSlots or []))


def is_hitter(player):
    return any(s in HITTER_SLOTS for s in (player.eligibleSlots or []))


def apply_name_patches(df, patches, col="PlayerName"):
    for old, new in patches.items():
        df.loc[df[col] == old, col] = new
    return df


def get_pitcher_roster(league) -> pd.DataFrame:
    rows = []
    for tm in league.teams:
        for pl in tm.roster:
            if is_pitcher(pl):
                slots = [s for s in (pl.eligibleSlots or []) if s in PITCHER_SLOTS]
                rows.append({
                    "PlayerName":  pl.name,
                    "FantasyTeam": tm.team_name,
                    "Position":    ", ".join(slots),
                    "ESPN_Status": pl.injuryStatus or "ACTIVE",
                    "ESPN_OnIL":   getattr(pl, "lineupSlot", "") == "IL" or bool(getattr(pl, "injured", False)),
                })
    df = pd.DataFrame(rows).drop_duplicates(subset="PlayerName")
    return apply_name_patches(df, PITCHER_NAME_PATCHES)


def get_pitcher_espn_svhd(league) -> pd.DataFrame:
    """Pull season stats from ESPN player stats (scoring period 0 = season total).
    Covers both rostered and FA pitchers. Returns K, W, IP, GS, GP, SV, HLD, SVHD."""
    rows = []
    seen = set()

    def _extract(pl):
        if pl.name in seen:
            return
        bd = (pl.stats or {}).get(0, {}).get('breakdown', {})
        outs = bd.get('OUTS', 0) or 0
        rows.append({
            "PlayerName": pl.name,
            "ESPN_SV":    bd.get('SV',   0) or 0,
            "ESPN_HLD":   bd.get('HLD',  0) or 0,
            "ESPN_SVHD":  bd.get('SVHD', 0) or 0,
            "ESPN_K":     bd.get('K',   -1),
            "ESPN_W":     bd.get('W',   -1),
            "ESPN_IP":    round(outs / 3, 1) if outs > 0 else -1,
            "ESPN_GS":    bd.get('GS',  -1),
            "ESPN_GP":    bd.get('GP',  -1),
        })
        seen.add(pl.name)

    for tm in league.teams:
        for pl in tm.roster:
            if is_pitcher(pl):
                _extract(pl)
    for fa in league.free_agents():
        if is_pitcher(fa):
            _extract(fa)

    df = pd.DataFrame(rows).drop_duplicates(subset="PlayerName")
    log(f"  ESPN season stats: {len(df)} pitchers")
    return apply_name_patches(df, PITCHER_NAME_PATCHES)


def get_hitter_roster(league) -> pd.DataFrame:
    rows = []
    for tm in league.teams:
        for pl in tm.roster:
            if is_hitter(pl):
                # Prefer specific positions over UTIL/BE/IL
                priority = [s for s in (pl.eligibleSlots or []) if s in {"C","1B","2B","3B","SS","LF","CF","RF","DH","OF"}]
                slots = priority or [s for s in (pl.eligibleSlots or []) if s in HITTER_SLOTS]
                rows.append({
                    "PlayerName":  pl.name,
                    "FantasyTeam": tm.team_name,
                    "Position":    ", ".join(slots),
                    "ESPN_Status": pl.injuryStatus or "ACTIVE",
                    "ESPN_OnIL":   getattr(pl, "lineupSlot", "") == "IL" or bool(getattr(pl, "injured", False)),
                })
    df = pd.DataFrame(rows).drop_duplicates(subset="PlayerName")
    return apply_name_patches(df, HITTER_NAME_PATCHES)


def get_pitcher_fa(league) -> pd.DataFrame:
    rows = []
    seen = set()
    for fa in league.free_agents():
        if fa.name in seen:
            continue
        if is_pitcher(fa):
            slots = [s for s in (fa.eligibleSlots or []) if s in PITCHER_SLOTS]
            rows.append({
                "PlayerName":            fa.name,
                "FreeAgentInjuryStatus": fa.injuryStatus or "",
                "FA_Position":           ", ".join(slots),
            })
            seen.add(fa.name)
    return pd.DataFrame(rows).drop_duplicates(subset="PlayerName")


def get_hitter_fa(league) -> pd.DataFrame:
    rows = []
    seen = set()
    for fa in league.free_agents():
        if fa.name in seen:
            continue
        if is_hitter(fa):
            priority = [s for s in (fa.eligibleSlots or []) if s in {"C","1B","2B","3B","SS","LF","CF","RF","DH","OF"}]
            slots = priority or [s for s in (fa.eligibleSlots or []) if s in HITTER_SLOTS]
            rows.append({
                "PlayerName":            fa.name,
                "FreeAgentInjuryStatus": fa.injuryStatus or "",
                "FA_Position":           ", ".join(slots),
            })
            seen.add(fa.name)
    return pd.DataFrame(rows).drop_duplicates(subset="PlayerName")


# â”€â”€ PROBABLE STARTERS â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _strip_accents(name: str) -> str:
    """'MartÃ­n PÃ©rez' â†’ 'Martin Perez' so MLB API names match FantasyPros names."""
    return "".join(
        c for c in unicodedata.normalize("NFD", name)
        if unicodedata.category(c) != "Mn"
    )


def _get_team_recent_starts(days_back: int = 14) -> dict:
    """
    Returns {team_name: [(date_str, pitcher_name), ...]} â€” every confirmed start
    over the past `days_back` days, sorted chronologically per team.  Used to
    reconstruct each team's rotation ORDER so unannounced slots can be projected
    by advancing the rotation through the team's actual upcoming games (rather
    than a crude last_start + 6 calendar days guess).
    """
    end_str   = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    start_str = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    try:
        sched = requests.get(
            f"https://statsapi.mlb.com/api/v1/schedule"
            f"?sportId=1&startDate={start_str}&endDate={end_str}&gameType=R",
            timeout=15,
        ).json()
    except Exception:
        return {}

    game_meta = {}
    for d in sched.get("dates", []):
        for g in d.get("games", []):
            pk = g["gamePk"]
            game_meta[pk] = (d["date"], g["teams"]["home"]["team"]["name"],
                             g["teams"]["away"]["team"]["name"])

    team_starts = {}  # team_name -> list of (date_str, pitcher_name)
    all_pks = list(game_meta)
    for i in range(0, len(all_pks), 30):
        chunk = all_pks[i : i + 30]
        try:
            resp = requests.get(
                f"https://statsapi.mlb.com/api/v1/schedule"
                f"?gamePks={','.join(str(p) for p in chunk)}&hydrate=probablePitcher",
                timeout=15,
            ).json()
        except Exception:
            continue
        for d in resp.get("dates", []):
            for g in d.get("games", []):
                pk = g["gamePk"]
                if pk not in game_meta:
                    continue
                date_str = game_meta[pk][0]
                for side in ("home", "away"):
                    sp   = g["teams"][side].get("probablePitcher") or {}
                    name = sp.get("fullName", "")
                    if not name or name == "TBD":
                        continue
                    cleaned = _strip_accents(name)
                    team    = g["teams"][side]["team"]["name"]
                    team_starts.setdefault(team, []).append((date_str, cleaned))

    for team in team_starts:
        team_starts[team].sort()  # chronological
    return team_starts


def get_probable_starters(days: int = SP_DAYS_OUT) -> pd.DataFrame:
    """
    Two-call strategy:
      1. One range schedule call â†’ all gamePks for the window
      2. One batched call with ?gamePks=...&hydrate=probablePitcher
    Falls back to the per-game live-feed if the batch returns nothing.
    Unconfirmed (TBD) slots are filled by rotation projection: last_start + 5 days Â±1.
    Names are accent-stripped so 'MartÃ­n PÃ©rez' merges as 'Martin Perez'.
    PSP_Projected=True marks rotation projections vs confirmed MLB entries.
    """
    start_str = datetime.now().strftime("%Y-%m-%d")
    end_str   = (datetime.now() + timedelta(days=days - 1)).strftime("%Y-%m-%d")

    # Step 1 â€” one call for all gamePks in the window
    try:
        sched = requests.get(
            f"https://statsapi.mlb.com/api/v1/schedule"
            f"?sportId=1&startDate={start_str}&endDate={end_str}&gameType=R",
            timeout=15,
        ).json()
    except Exception as e:
        log(f"  Probable starters schedule fetch failed: {e}")
        return _probable_starters_live_feed(days)

    game_meta = {}  # gamePk -> (date_str, home_name, away_name)
    for d in sched.get("dates", []):
        date_str = d["date"]
        for g in d.get("games", []):
            pk   = g["gamePk"]
            home = g["teams"]["home"]["team"]["name"]
            away = g["teams"]["away"]["team"]["name"]
            game_meta[pk] = (date_str, home, away)

    if not game_meta:
        return pd.DataFrame(columns=["PlayerName", "PSP_HomeVAway", "PSP_Date", "PSP_Projected"])

    # Step 2 â€” batch hydrate in chunks of 30
    all_pks           = list(game_meta.keys())
    frames            = []
    confirmed_slots   = set()   # (team_name, date_str) that already have a named pitcher
    confirmed_upcoming = {}     # pitcher_name -> (date_str, team_name) for upcoming confirmed starts

    for i in range(0, len(all_pks), 30):
        chunk = all_pks[i : i + 30]
        try:
            resp = requests.get(
                f"https://statsapi.mlb.com/api/v1/schedule"
                f"?gamePks={','.join(str(p) for p in chunk)}&hydrate=probablePitcher",
                timeout=15,
            ).json()
        except Exception:
            continue
        for d in resp.get("dates", []):
            for g in d.get("games", []):
                pk   = g["gamePk"]
                meta = game_meta.get(pk)
                if not meta:
                    continue
                date_str, home, away = meta
                for side in ("home", "away"):
                    sp   = g["teams"][side].get("probablePitcher") or {}
                    name = sp.get("fullName", "")
                    team = g["teams"][side]["team"]["name"]
                    opp  = away if side == "home" else home
                    ha   = f"vs {opp}" if side == "home" else f"@ {home}"
                    if name and name != "TBD":
                        cleaned = _strip_accents(name)
                        confirmed_slots.add((team, date_str))
                        confirmed_upcoming[cleaned] = (date_str, team)
                        frames.append({
                            "PlayerName":    cleaned,
                            "PSP_HomeVAway": ha,
                            "PSP_Date":      date_str,
                            "PSP_Projected": False,
                        })

    if not frames:
        log("  Batch probable starters returned nothing â€” falling back to live-feed method")
        return _probable_starters_live_feed(days)

    # Step 3 â€” rotation projection: advance each team's rotation ORDER through its
    # actual upcoming games (one pitcher per game), rather than a crude
    # last_start + 6 calendar days guess. Walking real games (a) makes each game
    # slot exclusive, so two projected SPs can never land on the same team/day,
    # and (b) counts turns by games (honoring off-days/doubleheaders), which also
    # surfaces legitimate two-start weeks. Confirmed MLB entries (already in
    # frames) act as anchors that re-sync the cycle.
    _ROT_RECENCY   = 11  # a rotation member must have started within this many days
    _PROJ_MIN_DAYS = 4   # only surface projections >= this many days out (near-term
                         # slots are usually confirmed by MLB soon, so an early
                         # projection there is noise). The walk still advances the
                         # rotation through near-term games to keep phase; it just
                         # doesn't EMIT a projected row until the date is far enough.
    team_recent  = _get_team_recent_starts()
    today        = datetime.now().date()

    # confirmed upcoming start date per (team, pitcher) so we never project a
    # pitcher onto an open game earlier than a start the MLB API already confirmed
    confirmed_date = {}  # (team, pitcher) -> date_str
    for pitcher, (date_str, team) in confirmed_upcoming.items():
        confirmed_date[(team, pitcher)] = date_str

    # confirmed pitcher per (team, date) slot, to anchor/re-sync the walk
    confirmed_pitcher = {}  # (team, date_str) -> pitcher_name
    for pitcher, (date_str, team) in confirmed_upcoming.items():
        confirmed_pitcher[(team, date_str)] = pitcher

    # upcoming games grouped per team, chronological
    team_games = {}  # team_name -> list of (date_str, ha)
    for pk, (date_str, home, away) in game_meta.items():
        for team, opp, side in [(home, away, "home"), (away, home, "away")]:
            ha = f"vs {opp}" if side == "home" else f"@ {home}"
            team_games.setdefault(team, []).append((date_str, ha))

    proj_count = 0
    for team, games in team_games.items():
        games.sort(key=lambda g: g[0])

        # rotation queue: members who started within the recency guard, ordered
        # by last start date ascending (longest-rested / most "due" at the front)
        last_by_pitcher = {}
        for d, p in team_recent.get(team, []):
            last_by_pitcher[p] = d  # chronological input -> keeps latest
        members = sorted(
            ((d, p) for p, d in last_by_pitcher.items()
             if (today - datetime.strptime(d, "%Y-%m-%d").date()).days <= _ROT_RECENCY),
        )
        queue = [p for _, p in members]

        for date_str, ha in games:
            conf = confirmed_pitcher.get((team, date_str))
            if conf is not None:
                # confirmed slot (already in frames) â€” rotate the anchor to the back
                if conf in queue:
                    queue.remove(conf)
                    queue.append(conf)
                continue
            # next due pitcher who isn't already spoken for by a later confirmed start
            chosen_idx = None
            for i, p in enumerate(queue):
                cd = confirmed_date.get((team, p))
                if cd and cd > date_str:
                    continue
                chosen_idx = i
                break
            if chosen_idx is None:
                continue
            pitcher = queue.pop(chosen_idx)
            queue.append(pitcher)  # just started -> back of the rotation
            days_out = (datetime.strptime(date_str, "%Y-%m-%d").date() - today).days
            if days_out < _PROJ_MIN_DAYS:
                continue  # phase kept, but too near to surface as a projection
            frames.append({
                "PlayerName":    pitcher,
                "PSP_HomeVAway": ha,
                "PSP_Date":      date_str,
                "PSP_Projected": True,
            })
            proj_count += 1

    n_confirmed = len(frames) - proj_count
    # Sort confirmed before projected so dedup keeps confirmed when pitcher appears in both
    df = pd.DataFrame(frames).sort_values(["PSP_Projected", "PSP_Date"])
    log(f"  Probable starters: {n_confirmed} confirmed + {proj_count} projected over {days} days (batch method)")
    return _attach_start_lists(df)


def _attach_start_lists(df: pd.DataFrame) -> pd.DataFrame:
    """Attach PSP_Dates / PSP_HomeVAways (ALL upcoming starts per pitcher, so a
    two-start week is detectable downstream), then dedup to one row per pitcher.
    The surviving scalar PSP_Date/PSP_HomeVAway/PSP_Projected remain the earliest
    start (unchanged behavior for every existing consumer)."""
    by_player = {}
    for row in df.sort_values("PSP_Date").itertuples(index=False):
        # setdefault keeps the confirmed entry (appended first) if a date repeats
        by_player.setdefault(row.PlayerName, {}).setdefault(row.PSP_Date, row.PSP_HomeVAway)
    deduped = df.drop_duplicates(subset="PlayerName", keep="first").copy()
    deduped["PSP_Dates"] = deduped["PlayerName"].map(
        lambda p: sorted(by_player.get(p, {})))
    deduped["PSP_HomeVAways"] = deduped["PlayerName"].map(
        lambda p: [by_player[p][d] for d in sorted(by_player.get(p, {}))])
    return deduped


def _probable_starters_live_feed(days: int) -> pd.DataFrame:
    """Fallback: one live-feed call per game (original method)."""
    frames = []
    for i in range(days):
        date_str = (datetime.now() + timedelta(days=i)).strftime("%Y-%m-%d")
        try:
            sched = requests.get(
                f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={date_str}",
                timeout=10,
            ).json()
        except Exception:
            continue
        for date_info in sched.get("dates", []):
            for game in date_info.get("games", []):
                try:
                    gd = requests.get(
                        f"https://statsapi.mlb.com/api/v1.1/game/{game['gamePk']}/feed/live",
                        timeout=10,
                    ).json().get("gameData", {})
                    pitchers = gd.get("probablePitchers", {})
                    home = game["teams"]["home"]["team"]["name"]
                    away = game["teams"]["away"]["team"]["name"]
                    for side, ha in [("home", f"vs {away}"), ("away", f"@ {home}")]:
                        name = pitchers.get(side, {}).get("fullName", "TBD")
                        if name and name != "TBD":
                            frames.append({
                                "PlayerName":    _strip_accents(name),
                                "PSP_HomeVAway": ha,
                                "PSP_Date":      date_str,
                                "PSP_Projected": False,
                            })
                except Exception:
                    pass
    if not frames:
        return pd.DataFrame(columns=["PlayerName", "PSP_HomeVAway", "PSP_Date",
                                     "PSP_Projected", "PSP_Dates", "PSP_HomeVAways"])
    df = pd.DataFrame(frames).sort_values("PSP_Date")
    log(f"  Probable starters: {len(df)} entries over {days} days (live-feed fallback)")
    return _attach_start_lists(df)


# â”€â”€ OPPONENT OPS â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def get_opponent_ops() -> pd.DataFrame:
    frames = []
    today = datetime.now().strftime("%Y-%m-%d")
    periods = [
        (None, CURRENT_YEAR),
        (7,    7),
        (15,   15),
        (30,   30),
    ]
    for days_back, dataset_val in periods:
        try:
            if days_back is None:
                url = (
                    f"https://statsapi.mlb.com/api/v1/teams/stats"
                    f"?season={CURRENT_YEAR}&sportId=1&group=hitting&stats=season"
                )
            else:
                start = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
                url = (
                    f"https://statsapi.mlb.com/api/v1/teams/stats"
                    f"?season={CURRENT_YEAR}&sportId=1&group=hitting"
                    f"&stats=byDateRange&startDate={start}&endDate={today}"
                )
            data = requests.get(url, timeout=15).json()
            rows = []
            for split in data.get("stats", [{}])[0].get("splits", []):
                st  = split["stat"]
                ops = st.get("ops")
                if ops is not None:
                    # Team strikeout rate (K per plate appearance) from the SAME call, so
                    # the proj-line K can be opponent-adjusted (a whiff-prone lineup inflates
                    # a starter's Ks; a contact lineup suppresses them).
                    try:
                        so = float(st.get("strikeOuts") or 0)
                        pa = float(st.get("plateAppearances") or 0)
                        k_val = round(so / pa, 4) if pa > 0 else -1.0
                    except (TypeError, ValueError):
                        k_val = -1.0
                    rows.append({
                        "OpponentTeam":  split["team"]["name"],
                        "Team_OPS_Value": float(ops),
                        "Team_K_Value":   k_val,
                        "Dataset_OPS":   dataset_val,
                    })
            if rows:
                frames.append(pd.DataFrame(rows))
                log(f"  Opp OPS dataset={dataset_val}: {len(rows)} teams")
        except Exception as e:
            log(f"  Opp OPS dataset={dataset_val} FAILED: {e}")
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def get_savant_pitcher_contact(year: int) -> pd.DataFrame:
    """Barrel% allowed and hard-hit% allowed from Baseball Savant (via pybaseball)."""
    try:
        from pybaseball import statcast_pitcher_exitvelo_barrels, cache
        cache.enable()
        raw = statcast_pitcher_exitvelo_barrels(year, minBBE=50)
        name_col = next((c for c in raw.columns if "last_name" in c.lower()), None)
        if name_col:
            raw["PlayerName"] = raw[name_col].apply(lf_to_name)
        col_map = {
            "brl_percent":  "BarrelPctAllowed",
            "ev95percent":  "HardHitPctAllowed",
            "avg_hit_speed":"AvgEVAllowed",
        }
        raw = raw.rename(columns={k: v for k, v in col_map.items() if k in raw.columns})
        keep = ["PlayerName"] + [v for v in col_map.values() if v in raw.columns]
        log(f"  Savant pitcher contact quality: {len(raw)} pitchers")
        return raw[keep].drop_duplicates("PlayerName")
    except Exception as e:
        log(f"  Savant pitcher contact FAILED: {e}")
        return pd.DataFrame(columns=["PlayerName"])


def get_savant_pitcher_expected(year: int) -> pd.DataFrame:
    """xERA and xwOBA-against from Baseball Savant expected stats (via pybaseball).
    Both are ABSOLUTE values (xERA ~ ERA scale; xwOBA_against ~ .315 league avg),
    so send_digest.py can blend them directly with real ERA/contact numbers."""
    try:
        from pybaseball import statcast_pitcher_expected_stats, cache
        cache.enable()
        df = statcast_pitcher_expected_stats(year, minPA=50)
        name_col = next((c for c in df.columns if "last_name" in c.lower()), None)
        if name_col:
            df["PlayerName"] = df[name_col].apply(lf_to_name)
        col_map = {"xera": "xERA", "est_woba": "xwOBA_against"}
        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
        keep = ["PlayerName"] + [v for v in col_map.values() if v in df.columns]
        log(f"  Savant pitcher expected stats: {len(df)} pitchers")
        return df[keep].drop_duplicates("PlayerName")
    except Exception as e:
        log(f"  Savant pitcher expected stats FAILED: {e}")
        return pd.DataFrame(columns=["PlayerName"])


def get_savant_pitcher_skill(year: int) -> pd.DataFrame:
    """Whiff% as a Baseball Savant league PERCENTILE RANK (0-100), not a raw rate.
    A pitch-skill signal for the strikeout component that leads results-based K%."""
    try:
        from pybaseball import statcast_pitcher_percentile_ranks, cache
        cache.enable()
        df = statcast_pitcher_percentile_ranks(year)
        name_col = next((c for c in df.columns if "player_name" in c.lower() or "last_name" in c.lower()), None)
        if name_col:
            df["PlayerName"] = df[name_col].apply(lf_to_name)
        col_map = {"whiff_percent": "WhiffPctile"}
        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
        keep = ["PlayerName"] + [v for v in col_map.values() if v in df.columns]
        if "WhiffPctile" in df.columns:
            df = df.dropna(subset=["WhiffPctile"])
        log(f"  Savant pitcher skill percentiles: {len(df)} pitchers")
        return df[keep].drop_duplicates("PlayerName")
    except Exception as e:
        log(f"  Savant pitcher skill FAILED: {e}")
        return pd.DataFrame(columns=["PlayerName"])


def get_savant_pitcher_whiff(year: int) -> pd.DataFrame:
    """Raw overall Whiff% from Baseball Savant pitch-arsenal stats (via pybaseball).
    The arsenal feed is per-pitcher-per-pitch-type; aggregate to one overall rate by
    pitches-weighting each pitch type's whiff%. DISPLAY-ONLY (a raw swing-and-miss rate,
    0-100) — must NOT feed pitcher_score, which already uses WhiffPctile for the K
    component (raw whiff% would double-count). Distinct column WhiffPct vs WhiffPctile."""
    try:
        from pybaseball import statcast_pitcher_arsenal_stats, cache
        cache.enable()
        raw = statcast_pitcher_arsenal_stats(year)
        raw = raw[["player_id", "last_name, first_name", "pitches", "whiff_percent"]].copy()
        raw["pitches"] = pd.to_numeric(raw["pitches"], errors="coerce").fillna(0)
        raw["whiff_percent"] = pd.to_numeric(raw["whiff_percent"], errors="coerce")
        raw = raw.dropna(subset=["whiff_percent"])
        raw["_wp"] = raw["whiff_percent"] * raw["pitches"]
        agg = raw.groupby(["player_id", "last_name, first_name"], as_index=False).agg(
            _wp=("_wp", "sum"), pitches=("pitches", "sum"))
        agg = agg[agg["pitches"] > 0]
        agg["WhiffPct"] = (agg["_wp"] / agg["pitches"]).round(1)
        agg["PlayerName"] = agg["last_name, first_name"].apply(lf_to_name)
        log(f"  Savant pitcher raw whiff%: {len(agg)} pitchers")
        return agg[["PlayerName", "WhiffPct"]].drop_duplicates("PlayerName")
    except Exception as e:
        log(f"  Savant pitcher raw whiff% FAILED: {e}")
        return pd.DataFrame(columns=["PlayerName"])


def get_statcast_expected_stats(year: int) -> pd.DataFrame:
    """xBA, xSLG, xwOBA from Baseball Savant expected stats."""
    try:
        from pybaseball import statcast_batter_expected_stats
        df = statcast_batter_expected_stats(year, minPA=50)
        name_col = next((c for c in df.columns if "last_name" in c.lower()), None)
        if name_col:
            df["PlayerName"] = df[name_col].apply(lf_to_name)
        col_map = {"est_ba": "xBA", "est_slg": "xSLG", "est_woba": "xwOBA"}
        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
        keep = ["PlayerName"] + [v for v in col_map.values() if v in df.columns]
        log(f"  Statcast expected stats: {len(df)} batters")
        return df[keep].drop_duplicates("PlayerName")
    except Exception as e:
        log(f"  Statcast expected stats FAILED: {e}")
        return pd.DataFrame(columns=["PlayerName"])


def get_sprint_speed(year: int) -> pd.DataFrame:
    """Statcast sprint speed â€” best SB predictor."""
    try:
        from pybaseball import statcast_sprint_speed
        df = statcast_sprint_speed(year, min_opp=10)
        # Handle 'last_name, first_name' or separate columns
        combo_col = next((c for c in df.columns if "last_name" in c and "first" in c), None)
        last_col  = next((c for c in df.columns if c == "last_name"), None)
        first_col = next((c for c in df.columns if c == "first_name"), None)
        if combo_col:
            df["PlayerName"] = df[combo_col].apply(lf_to_name)
        elif last_col and first_col:
            df["PlayerName"] = (df[first_col].str.strip() + ", " + df[last_col].str.strip()).apply(lf_to_name)
        if "sprint_speed" in df.columns:
            df = df.rename(columns={"sprint_speed": "SprintSpeed"})
        if "PlayerName" in df.columns and "SprintSpeed" in df.columns:
            log(f"  Sprint speed: {len(df)} players")
            return df[["PlayerName", "SprintSpeed"]].drop_duplicates("PlayerName")
        return pd.DataFrame(columns=["PlayerName", "SprintSpeed"])
    except Exception as e:
        log(f"  Sprint speed FAILED: {e}")
        return pd.DataFrame(columns=["PlayerName", "SprintSpeed"])


# â”€â”€ ROTO SCORES â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

HITTER_CATS  = ["R", "HR", "RBI", "B_SO", "SB", "OPS"]
PITCHER_CATS = ["K", "QS", "W", "ERA", "WHIP", "SVHD"]
ROTO_CATS    = HITTER_CATS + PITCHER_CATS
LOWER_BETTER = {"ERA", "WHIP", "B_SO"}


def roto_score_week(league, week: int) -> pd.DataFrame:
    boxes = league.box_scores(week)
    rows = []
    for b in boxes:
        for side, opp in [("home", "away"), ("away", "home")]:
            team  = getattr(b, f"{side}_team").team_name
            stats = getattr(b, f"{side}_stats")
            for cat, info in stats.items():
                # Rank roto cats by VALUE, not ESPN's per-category `result`: for the
                # live (in-progress) matchup period ESPN leaves `result` = None until the
                # period closes, so a result-gated filter drops the whole current week.
                # Value is present live, and the ranking math below never used `result`
                # (completed weeks are unaffected — value + result are both present there).
                if cat in ROTO_CATS and info.get("value") is not None:
                    rows.append({
                        "Team":     team,
                        "Category": cat,
                        "Value":    info.get("value"),
                        "Week":     week,
                    })
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["Value"] = pd.to_numeric(df["Value"], errors="coerce")
    pivot = df.groupby(["Team", "Category"])["Value"].sum().unstack(fill_value=0)
    pivot = pivot.reindex(columns=ROTO_CATS, fill_value=0)
    n = len(pivot)
    pivot["Roto_Score"] = 0
    for cat in ROTO_CATS:
        if cat not in pivot.columns:
            continue
        ranked = pivot[cat].rank(method="average", ascending=True, na_option="bottom")
        if cat in LOWER_BETTER:
            pivot[f"{cat}_Points"] = n - ranked + 1
        else:
            pivot[f"{cat}_Points"] = ranked
        pivot["Roto_Score"] += pivot[f"{cat}_Points"]
    pivot["Week"] = week
    return pivot.reset_index()


def get_weekly_matchup_results(league) -> dict:
    """Returns {week: {team_name: 'W'/'L'/'T'}} for all completed weeks."""
    current_week = getattr(league, 'currentMatchupPeriod', 25)
    weekly = {}
    for wk in range(1, current_week):
        try:
            boxes = league.box_scores(wk)
            wk_results = {}
            for b in boxes:
                winner = getattr(b, 'winner', None)
                ht = getattr(b.home_team, 'team_name', '') if b.home_team else ''
                at = getattr(b.away_team, 'team_name', '') if b.away_team else ''
                if not ht or not at or not winner:
                    continue
                ht = " ".join(ht.split())
                at = " ".join(at.split())
                if winner == 'HOME':
                    wk_results[ht] = 'W'; wk_results[at] = 'L'
                elif winner == 'AWAY':
                    wk_results[at] = 'W'; wk_results[ht] = 'L'
                else:
                    wk_results[ht] = 'T'; wk_results[at] = 'T'
            if wk_results:
                weekly[wk] = wk_results
        except Exception:
            pass
    return weekly


def get_season_cat_totals(league) -> dict:
    """True season-to-date category totals per team, straight from ESPN's cumulative
    `mTeam` view (`valuesByStat`). Returns {team_name: {CAT: value}} for the 12 ROTO_CATS.

    These are the ONLY correct season rate stats (ERA/WHIP/OPS): summing or averaging
    each matchup's weekly rate value is mathematically wrong (a 5-IP week can't weigh the
    same as a 40-IP week). ESPN's cumulative value is innings/AB-weighted and reconciles
    with the site to the digit. The season roto grids DISPLAY these values but still RANK
    by summed weekly roto points, so the two columns stay independent by design."""
    try:
        from espn_api.baseball.constant import STATS_MAP
        cat_to_id = {}
        for sid, name in STATS_MAP.items():
            if name in ROTO_CATS and name not in cat_to_id:
                cat_to_id[name] = str(sid)
        data = league.espn_request.league_get(params={"view": "mTeam"})
        id2name = {t.team_id: t.team_name for t in league.teams}
        totals = {}
        for t in data.get("teams", []):
            name = id2name.get(t.get("id")) or t.get("name") or ""
            if not name:
                continue
            vbs = t.get("valuesByStat") or {}
            row = {}
            for cat, sid in cat_to_id.items():
                v = vbs.get(sid)
                if v is not None:
                    row[cat] = v
            totals[name] = row
        log(f"  Season category totals: {len(totals)} teams")
        return totals
    except Exception as e:
        log(f"  Season category totals FAILED: {e}")
        return {}


def get_all_roto(league) -> list:
    results = []
    current_week = getattr(league, 'currentMatchupPeriod', 25)
    for wk in range(1, current_week + 1):
        try:
            df = roto_score_week(league, wk)
            if df is not None and not df.empty:
                results.append(df)
        except Exception as e:
            log(f"  roto_score_week({wk}) failed: {e}")
            continue
    if not results:
        return []
    combined = pd.concat(results, ignore_index=True)
    return combined.to_dict(orient="records")


# â”€â”€ HR PROBABILITY â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def compute_hr_probability(row) -> float:
    """Modeled per-game HR probability from power skill (barrel/hard-hit/launch/
    xwOBA/ISO + HR rate). This measures SKILL, not availability — do not gate on
    injury status (that zeroed out injured stars like Judge/Trout/Buxton whose
    power is intact). Availability is surfaced separately via injury tags."""
    hr_r    = min(row.get("HR_per_AB",  0) / 0.10, 1.0) if row.get("HR_per_AB", -1) > 0 else 0
    barrel  = min(row.get("Barrel_Pct", 0) / 20.0, 1.0) if row.get("Barrel_Pct", -1) > 0 else 0
    hh      = min(row.get("HardHit_Pct",0) / 58.0, 1.0) if row.get("HardHit_Pct",-1) > 0 else 0
    la      = min(max((row.get("Avg_LA", 0) - 8) / 14.0, 0), 1.0) if row.get("Avg_LA", -1) > 0 else 0
    streak  = min(row.get("HR_Last7",   0) / 3.0,  1.0)
    # Boost from xwOBA and ISO
    xwoba   = min(max((row.get("xwOBA", 0) - 0.28) / 0.15, 0), 1.0) if row.get("xwOBA", 0) > 0 else 0
    iso_v   = min(row.get("ISO", 0) / 0.25, 1.0) if row.get("ISO", 0) > 0 else 0
    # No usable signal at all → unknown (blank cell downstream), not a fake 5% floor
    if hr_r <= 0 and barrel <= 0 and hh <= 0 and xwoba <= 0:
        return 0.0
    raw     = hr_r * 0.30 + barrel * 0.28 + hh * 0.15 + la * 0.08 + streak * 0.05 + xwoba * 0.08 + iso_v * 0.06
    return round(0.05 + raw * 0.26, 4)


def get_statcast_contact() -> pd.DataFrame:
    try:
        from pybaseball import statcast_batter_exitvelo_barrels, cache
        cache.enable()
        raw = statcast_batter_exitvelo_barrels(CURRENT_YEAR, minBBE=50)
        col_map = {
            "last_name, first_name": "PlayerName_LF",
            "brl_percent":           "Barrel_Pct",
            "ev95percent":           "HardHit_Pct",
            "avg_hit_speed":         "MaxEV",
            "avg_hit_angle":         "Avg_LA",
        }
        sc = raw.rename(columns={k: v for k, v in col_map.items() if k in raw.columns})
        sc["PlayerName"] = sc["PlayerName_LF"].apply(lf_to_name)
        keep = [c for c in ["PlayerName", "Barrel_Pct", "HardHit_Pct", "MaxEV", "Avg_LA"] if c in sc.columns]
        log(f"  Statcast exit velo/barrels: {len(sc)} batters")
        return sc[keep].copy()
    except Exception as e:
        log(f"  Statcast FAILED: {e}")
        return pd.DataFrame(columns=["PlayerName", "Barrel_Pct", "HardHit_Pct", "MaxEV", "Avg_LA"])


# â”€â”€ TRANSACTIONS â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def get_transactions(league) -> list:
    try:
        activities = league.recent_activity()
    except Exception:
        return []
    rows = []
    for act in activities:
        for item in act.actions:
            team_obj, tx_type, player_obj = item
            ts = datetime.fromtimestamp(act.date / 1000).strftime("%Y-%m-%d %H:%M:%S")
            rows.append({
                "FantasyTeam":        team_obj.team_name if team_obj else "N/A",
                "TransactionType":    tx_type,
                "TransactionDate":    ts,
                "PlayerName":         str(player_obj),
                "MLBTeam":            player_obj.proTeam if hasattr(player_obj, "proTeam") else "N/A",
                "PositionEligibility": ", ".join(player_obj.eligibleSlots) if hasattr(player_obj, "eligibleSlots") else "N/A",
            })
    return rows


# â”€â”€ PITCHER PIPELINE â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def build_pitcher_data(league) -> list:
    log("Fetching pitcher stats from FantasyProsâ€¦")
    fp = fetch_fantasypros("pitchers")
    if fp.empty:
        return []

    fp["IP"] = pd.to_numeric(fp["IP"], errors="coerce")
    fp["K"]  = pd.to_numeric(fp["K"],  errors="coerce")
    fp["ERA"]= pd.to_numeric(fp["ERA"],errors="coerce")
    fp.dropna(subset=["IP", "K", "ERA"], inplace=True)
    fp["K/IP"] = (fp["K"] / fp["IP"].apply(innings_to_decimal)).round(5)

    if "SV"  not in fp.columns: fp["SV"]  = 0
    if "HLD" not in fp.columns: fp["HLD"] = 0
    fp["SV"]   = pd.to_numeric(fp["SV"],  errors="coerce").fillna(0)
    fp["HLD"]  = pd.to_numeric(fp["HLD"], errors="coerce").fillna(0)
    fp["SVHD"] = fp["SV"] + fp["HLD"]

    log("Getting pitcher roster from ESPN…")
    roster_df   = get_pitcher_roster(league)
    fa_df       = get_pitcher_fa(league)
    espn_svhd   = get_pitcher_espn_svhd(league)

    # Merge roster (brings FantasyTeam + Position + ESPN_Status), then FA status separately
    # FA_Position avoids Position_x / Position_y collision. merge_on_name adds an
    # accent/suffix-insensitive fallback so ESPN 'Luis García Jr.' matches FP 'Luis Garcia'.
    merged = merge_on_name(fp, roster_df, ["PlayerName", "FantasyTeam", "Position", "ESPN_Status", "ESPN_OnIL"])
    merged["ESPN_Status"] = merged["ESPN_Status"].fillna("")
    # ESPN_OnIL: True only for a rostered player sitting in an IL lineup slot (dropping
    # them frees no active/bench room). Unmatched (FP-only / FA) rows default to False.
    # Keep native python bools (not .astype(bool) → numpy bool_, which json's default=str
    # would stringify to the *truthy* "False").
    merged["ESPN_OnIL"] = merged.get("ESPN_OnIL", False).fillna(False)
    merged = merge_on_name(merged, fa_df, ["PlayerName", "FreeAgentInjuryStatus", "FA_Position"])

    # Coalesce position: ESPN roster â†’ ESPN FA â†’ FantasyPros player string
    merged["Position"] = merged["Position"].fillna("").str.strip()
    fa_pos = merged.get("FA_Position", pd.Series("", index=merged.index)).fillna("").str.strip()
    merged["Position"] = merged.apply(lambda r: r["Position"] if r["Position"] else fa_pos.loc[r.name], axis=1)
    merged.drop(columns=["FA_Position"], inplace=True, errors="ignore")
    fp_pos = merged.get("FP_Position", pd.Series("", index=merged.index)).fillna("").str.strip()
    merged["Position"] = merged.apply(lambda r: r["Position"] if r["Position"] else fp_pos.loc[r.name], axis=1)
    merged.drop(columns=["FP_Position"], inplace=True, errors="ignore")

    merged["FreeAgentInjuryStatus"] = merged["FreeAgentInjuryStatus"].fillna("")
    merged["FantasyTeam"] = merged["FantasyTeam"].fillna("")
    merged["RosterStatus"] = merged["FreeAgentInjuryStatus"].astype(str) + merged["FantasyTeam"].astype(str)

    log("Fetching probable startersâ€¦")
    sp = get_probable_starters()
    merged = merge_on_name(merged, sp, list(sp.columns))   # suffix/accent-safe (Jr./II)
    merged["PSP_Date"]      = merged["PSP_Date"].fillna("1999-01-01")
    merged["PSP_HomeVAway"] = merged["PSP_HomeVAway"].fillna("")
    merged["PSP_Projected"] = merged["PSP_Projected"].fillna(False)
    # List columns: fillna can't take a list, so coerce non-list (NaN) cells to []
    for _col in ("PSP_Dates", "PSP_HomeVAways"):
        if _col in merged.columns:
            merged[_col] = merged[_col].apply(lambda x: x if isinstance(x, list) else [])
        else:
            merged[_col] = [[] for _ in range(len(merged))]

    log("Fetching opponent OPSâ€¦")
    opp_ops = get_opponent_ops()
    if not opp_ops.empty:
        merged["OpponentTeam_temp"] = merged["PSP_HomeVAway"].str.split(" ").str[1:].str.join(" ")
        merged = merged.merge(
            opp_ops,
            left_on=["OpponentTeam_temp", "Dataset"],
            right_on=["OpponentTeam",     "Dataset_OPS"],
            how="left",
        )
        for col in ["Dataset_OPS", "OpponentTeam_temp", "OpponentTeam"]:
            if col in merged.columns:
                merged.drop(columns=[col], inplace=True)

    log("Fetching Baseball Savant pitcher contact quality (Barrel% allowed, HardHit% allowed)â€¦")
    sc_p = get_savant_pitcher_contact(CURRENT_YEAR)
    if not sc_p.empty:
        merged = merge_on_name(merged, sc_p, list(sc_p.columns))   # suffix/accent-safe

    log("Fetching Baseball Savant pitcher expected stats (xERA, xwOBA-against) and whiff%â€¦")
    xp_p = get_savant_pitcher_expected(CURRENT_YEAR)
    if not xp_p.empty:
        merged = merge_on_name(merged, xp_p, list(xp_p.columns))   # suffix/accent-safe
    sk_p = get_savant_pitcher_skill(CURRENT_YEAR)
    if not sk_p.empty:
        merged = merge_on_name(merged, sk_p, list(sk_p.columns))   # suffix/accent-safe
    wh_p = get_savant_pitcher_whiff(CURRENT_YEAR)   # raw Whiff% — DISPLAY ONLY, not scored
    if not wh_p.empty:
        merged = merge_on_name(merged, wh_p, list(wh_p.columns))   # suffix/accent-safe

    # Derive approximate K% from FantasyPros K and estimated TBF (K/IP * 9 / K9-to-TBF ratio)
    if "K" in merged.columns and "IP" in merged.columns:
        k_num = pd.to_numeric(merged["K"], errors="coerce").fillna(0)
        ip_num = merged["IP"].apply(innings_to_decimal)
        # Approx TBF ~ IP * 4.3; K% = K / TBF
        merged["Kpct_P"] = (k_num / (ip_num * 4.3 + 0.001)).clip(0, 0.50).round(4)
        gs_num = pd.to_numeric(merged.get("GS", 0), errors="coerce").fillna(0)
        g_num  = pd.to_numeric(merged.get("G",  0), errors="coerce").fillna(0)
        merged["IP_per_GS"] = (ip_num / gs_num.clip(lower=1)).clip(upper=7.5).round(2)
        merged["IP_per_G"]  = (ip_num / g_num.clip(lower=1)).clip(upper=7.5).round(2)

    # Override season SVHD with ESPN's own totals — more reliable than FantasyPros HLD.
    # Also keep ESPN_K, ESPN_W, ESPN_IP, ESPN_GS, ESPN_GP on all rows so send_digest.py
    # can use season counts for players who only appear in short-range FP datasets.
    if not espn_svhd.empty:
        merged = merged.merge(espn_svhd, on="PlayerName", how="left")
        yr_mask = pd.to_numeric(merged["Dataset"], errors="coerce") == CURRENT_YEAR
        for col, espn_col in [("SV", "ESPN_SV"), ("HLD", "ESPN_HLD"), ("SVHD", "ESPN_SVHD")]:
            if espn_col in merged.columns:
                override = pd.to_numeric(merged.loc[yr_mask, espn_col], errors="coerce")
                merged.loc[yr_mask, col] = override.where(override >= 0, merged.loc[yr_mask, col])
        merged.drop(columns=["ESPN_HLD"], inplace=True, errors="ignore")
        # ESPN_SV, ESPN_SVHD, ESPN_K, ESPN_W, ESPN_IP, ESPN_GS, ESPN_GP stay on all rows
        # so send_digest.py can use season counts for players only in short-range FP datasets.
        # ESPN_SV in particular lets save_role_watch tell a real closer (season saves) from a
        # holds-only reliever, whose recent-hold activity the data pipeline can't see.
        for c in ["ESPN_SV", "ESPN_SVHD", "ESPN_K", "ESPN_W", "ESPN_IP", "ESPN_GS", "ESPN_GP"]:
            if c in merged.columns:
                merged[c] = pd.to_numeric(merged[c], errors="coerce").fillna(-1)

    num_cols = merged.select_dtypes(include="number").columns
    merged[num_cols] = merged[num_cols].fillna(-1)
    merged = merged.fillna("")
    merged["IP"]   = merged["IP"].round(1)
    merged["ERA"]  = merged["ERA"].round(5)
    merged["WHIP"] = merged["WHIP"].round(5) if "WHIP" in merged.columns else -1
    merged["K/IP"] = merged["K/IP"].round(5)

    for col in ["Team_y", "Team_x"]:
        if col in merged.columns:
            merged.drop(columns=[col], inplace=True)

    return merged.to_dict(orient="records")


# â”€â”€ HITTER PIPELINE â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def build_hitter_data(league) -> list:
    log("Fetching hitter stats from FantasyProsâ€¦")
    fp = fetch_fantasypros("hitters")
    if fp.empty:
        return []

    log("Getting hitter roster from ESPNâ€¦")
    roster_df = get_hitter_roster(league)
    fa_df     = get_hitter_fa(league)

    # Same pattern: avoid Position_x / Position_y collision. merge_on_name adds the
    # accent/suffix-insensitive fallback (FP 'Luis Garcia' ↔ ESPN 'Luis García Jr.').
    merged = merge_on_name(fp, roster_df, ["PlayerName", "FantasyTeam", "Position", "ESPN_OnIL"])
    # ESPN_OnIL: True only for a rostered player sitting in an IL lineup slot (dropping
    # them frees no active/bench room). Unmatched (FP-only / FA) rows default to False.
    # Keep native python bools (see build_pitcher_data note on json default=str).
    merged["ESPN_OnIL"] = merged.get("ESPN_OnIL", False).fillna(False)
    merged = merge_on_name(merged, fa_df, ["PlayerName", "FreeAgentInjuryStatus", "FA_Position"])

    merged["Position"] = merged["Position"].fillna("").str.strip()
    fa_pos = merged.get("FA_Position", pd.Series("", index=merged.index)).fillna("").str.strip()
    merged["Position"] = merged.apply(lambda r: r["Position"] if r["Position"] else fa_pos.loc[r.name], axis=1)
    merged.drop(columns=["FA_Position"], inplace=True, errors="ignore")
    fp_pos = merged.get("FP_Position", pd.Series("", index=merged.index)).fillna("").str.strip()
    merged["Position"] = merged.apply(lambda r: r["Position"] if r["Position"] else fp_pos.loc[r.name], axis=1)
    merged.drop(columns=["FP_Position"], inplace=True, errors="ignore")

    merged["FreeAgentInjuryStatus"] = merged["FreeAgentInjuryStatus"].fillna("")
    merged["FantasyTeam"] = merged["FantasyTeam"].fillna("")
    merged["RosterStatus"] = merged["FreeAgentInjuryStatus"].astype(str) + merged["FantasyTeam"].astype(str)

    # â”€â”€ wRC+ approximation from OPS â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # lgOPS is derived from this snapshot's full-time regulars (season rows, AB >= 55% of the
    # p95 leader) so it tracks the season, not a fixed 0.720. Falls back to 0.720 early season.
    ops_num = pd.to_numeric(merged["OPS"], errors="coerce").fillna(0)
    LG_OPS = 0.720
    try:
        ds_num = pd.to_numeric(merged.get("Dataset"), errors="coerce")
        ab_num = pd.to_numeric(merged.get("AB"), errors="coerce").fillna(0)
        season = (ds_num == CURRENT_YEAR) & (ab_num > 0) & (ops_num > 0)
        if season.sum() >= 20:
            leader_ab = ab_num[season].quantile(0.95)
            reg = season & (ab_num >= leader_ab * 0.55)
            if reg.sum() >= 10:
                LG_OPS = round(float(ops_num[reg].mean()), 4)
    except Exception:
        LG_OPS = 0.720
    log(f"lgOPS (derived) = {LG_OPS:.4f}")
    merged["wRCplus"] = ((ops_num / LG_OPS) * 100).where(ops_num > 0, -1).round(0).astype(int)

    # â”€â”€ Statcast data (season rows only) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    log("Fetching Statcast contact data for HR probabilityâ€¦")
    sc = get_statcast_contact()

    log("Fetching Statcast expected stats (xBA, xSLG, xwOBA)â€¦")
    sc_exp = get_statcast_expected_stats(CURRENT_YEAR)

    log("Fetching sprint speed (SB predictor)â€¦")
    sprint = get_sprint_speed(CURRENT_YEAR)

    try:
        fp7 = pd.read_html("https://www.fantasypros.com/mlb/stats/hitters.php?range=7")[0]
        fp7["PlayerName"] = fp7["Player"].apply(extract_player_name)
        fp7["HR_Last7"]   = pd.to_numeric(fp7.get("HR", 0), errors="coerce").fillna(0)
        fp7 = fp7[["PlayerName", "HR_Last7"]]
    except Exception:
        fp7 = pd.DataFrame(columns=["PlayerName", "HR_Last7"])

    season_df = merged[merged["Dataset"] == CURRENT_YEAR].copy()
    season_df = merge_on_name(season_df, sc,     list(sc.columns))      # suffix/accent-safe
    season_df = merge_on_name(season_df, sc_exp, list(sc_exp.columns))  # suffix/accent-safe
    season_df = merge_on_name(season_df, sprint, list(sprint.columns))  # suffix/accent-safe
    season_df = season_df.merge(fp7,    on="PlayerName", how="left")    # FP↔FP names, exact

    roster_status = roster_df[["PlayerName", "ESPN_Status"]].copy() if "ESPN_Status" in roster_df.columns else roster_df.assign(ESPN_Status="ACTIVE")[["PlayerName", "ESPN_Status"]]
    espn_status = pd.concat([
        roster_status,
        fa_df.assign(ESPN_Status="FA")[["PlayerName", "ESPN_Status"]],
    ], ignore_index=True).drop_duplicates("PlayerName")[["PlayerName", "ESPN_Status"]]
    apply_name_patches(espn_status, HITTER_NAME_PATCHES)

    season_df = merge_on_name(season_df, espn_status, ["PlayerName", "ESPN_Status"])  # suffix/accent-safe
    season_df["ESPN_Status"] = season_df["ESPN_Status"].fillna("Unknown")
    season_df["HR"]        = pd.to_numeric(season_df.get("HR",  0), errors="coerce").fillna(0)
    season_df["AB"]        = pd.to_numeric(season_df.get("AB",  1), errors="coerce").replace(0, 1)
    season_df["HR_per_AB"] = (season_df["HR"] / season_df["AB"]).round(4)
    season_df["HR_Last7"]  = season_df.get("HR_Last7", pd.Series(0, index=season_df.index)).fillna(0)
    for c in ["Barrel_Pct", "HardHit_Pct", "MaxEV", "Avg_LA", "xBA", "xSLG", "xwOBA", "SprintSpeed", "ISO"]:
        if c not in season_df.columns:
            season_df[c] = -1
        season_df[c] = pd.to_numeric(season_df[c], errors="coerce").fillna(-1)

    # ISO (isolated power = SLG − AVG) drives the HR model but isn't in the FP feed;
    # derive it when SLG/AVG are present so the term isn't dead weight.
    _slg = pd.to_numeric(season_df.get("SLG", 0), errors="coerce").fillna(0)
    _avg = pd.to_numeric(season_df.get("AVG", 0), errors="coerce").fillna(0)
    _iso = (_slg - _avg).round(3)
    season_df["ISO"] = _iso.where(_iso > 0, season_df["ISO"])

    season_df["HR_Probability"] = season_df.apply(compute_hr_probability, axis=1)

    # Merge season-only enrichment back into all-range rows
    enrich_cols = ["PlayerName", "HR_Probability", "HR_per_AB", "ISO", "Barrel_Pct", "HardHit_Pct",
                   "MaxEV", "Avg_LA", "xBA", "xSLG", "xwOBA", "SprintSpeed"]
    enrich_cols = [c for c in enrich_cols if c in season_df.columns]
    enrich = season_df[enrich_cols].drop_duplicates("PlayerName")
    merged = merged.merge(enrich, on="PlayerName", how="left")
    merged["HR_Probability"] = merged["HR_Probability"].fillna(0)

    num_cols = merged.select_dtypes(include="number").columns
    merged[num_cols] = merged[num_cols].fillna(-1)
    merged = merged.fillna("")

    return merged.to_dict(orient="records")


# â”€â”€ RECENT HITTER STATS â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def fetch_recent_pitcher_stats(days: int = 7, start_dt: str = None, end_dt: str = None) -> list:
    """Pull pitcher stats from FanGraphs via pybaseball. Pass start_dt/end_dt for an exact window."""
    try:
        from pybaseball import pitching_stats_range
        end_dt   = end_dt   or datetime.now().strftime("%Y-%m-%d")
        start_dt = start_dt or (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        df = pitching_stats_range(start_dt, end_dt)
        if df is None or df.empty:
            return []
        name_col = next((c for c in df.columns if c.lower() in ("name", "playername")), None)
        if name_col and name_col != "PlayerName":
            df = df.rename(columns={name_col: "PlayerName"})
        if "SO" in df.columns and "K" not in df.columns:
            df = df.rename(columns={"SO": "K"})
        keep = [c for c in ["PlayerName", "Team", "G", "GS", "QS", "IP", "K", "ERA", "WHIP", "BB"] if c in df.columns]
        df = df[keep].copy()
        for c in keep[1:]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df.dropna(subset=["PlayerName"], inplace=True)
        df["PlayerName"] = df["PlayerName"].str.strip()
        log(f"  Recent pitcher stats ({start_dt} to {end_dt}): {len(df)} rows")
        return df.to_dict(orient="records")
    except Exception as e:
        log(f"  Recent pitcher stats FAILED: {e}")
        return []


def fetch_recent_hitter_stats(days: int = 7, start_dt: str = None, end_dt: str = None) -> list:
    """Pull hitter stats from FanGraphs via pybaseball. Pass start_dt/end_dt for an exact window."""
    try:
        from pybaseball import batting_stats_range
        end_dt   = end_dt   or datetime.now().strftime("%Y-%m-%d")
        start_dt = start_dt or (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        df = batting_stats_range(start_dt, end_dt)
        if df is None or df.empty:
            return []
        # Normalize name column (FanGraphs uses 'Name')
        name_col = next((c for c in df.columns if c.lower() in ("name", "playername")), None)
        if name_col and name_col != "PlayerName":
            df = df.rename(columns={name_col: "PlayerName"})
        if "BA" in df.columns and "AVG" not in df.columns:
            df = df.rename(columns={"BA": "AVG"})
        keep = [c for c in ["PlayerName", "Team", "G", "PA", "AB", "R", "HR", "RBI", "SB", "AVG", "OBP", "SLG", "OPS"] if c in df.columns]
        df = df[keep].copy()
        for c in keep[1:]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df.dropna(subset=["PlayerName"], inplace=True)
        df["PlayerName"] = df["PlayerName"].str.strip()
        log(f"  Recent hitter stats ({start_dt} to {end_dt}): {len(df)} rows")
        return df.to_dict(orient="records")
    except Exception as e:
        log(f"  Recent hitter stats FAILED: {e}")
        return []


# â”€â”€ STANDINGS â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def get_standings(league) -> list:
    rows = []
    for tm in league.teams:
        rows.append({
            "team_name":    tm.team_name,
            "wins":         tm.wins,
            "losses":       tm.losses,
            "ties":         getattr(tm, "ties", 0),
            "standing":     tm.standing,
            "logo_url":     getattr(tm, "logo_url", ""),
        })
    return sorted(rows, key=lambda r: r["standing"])


# â”€â”€ CURRENT WEEK MATCHUP â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def get_all_matchups(league) -> dict:
    """Return current-week matchups for all teams as {normalized_team_name: matchup_dict}."""
    current_week = getattr(league, "currentMatchupPeriod", None)
    if not current_week:
        return {}
    try:
        boxes = league.box_scores(current_week)
    except Exception as e:
        log(f"  Matchup fetch failed: {e}")
        return {}

    all_matchups = {}
    _flip = {"W": "L", "L": "W", "T": "T"}

    for b in boxes:
        home_name  = b.home_team.team_name
        away_name  = b.away_team.team_name
        home_stats = getattr(b, "home_stats", {}) or {}
        away_stats = getattr(b, "away_stats", {}) or {}

        wins = losses = ties = 0
        cats = []
        for cat in ROTO_CATS:
            h_info = home_stats.get(cat, {}) or {}
            a_info = away_stats.get(cat, {}) or {}
            h_val  = float(h_info.get("value") or 0)
            a_val  = float(a_info.get("value") or 0)

            # Prefer ESPN's own per-category result — it already applies the league's
            # ratio-stat innings-pitched minimum (ERA/WHIP don't count for a team until
            # it clears the IP floor, e.g. 25 IP), which a raw value comparison misses:
            # a team with the better WHIP but under the IP min still LOSES that category.
            # Fall back to comparing values only when ESPN supplies no result.
            espn_res = str(h_info.get("result") or "").upper()
            if espn_res in ("WIN", "LOSS", "TIE"):
                result = {"WIN": "W", "LOSS": "L", "TIE": "T"}[espn_res]
            elif h_val == a_val:
                result = "T"
            elif cat in LOWER_BETTER:
                result = "W" if h_val < a_val else "L"
            else:
                result = "W" if h_val > a_val else "L"

            if result == "W":   wins += 1
            elif result == "L": losses += 1
            else:               ties += 1

            cats.append({
                "cat": cat, "my_val": h_val, "opp_val": a_val,
                "result": result, "lower_better": cat in LOWER_BETTER,
            })

        all_matchups[" ".join(home_name.split())] = {
            "week": current_week, "my_team": home_name, "opp_team": away_name,
            "wins": wins, "losses": losses, "ties": ties, "categories": cats,
        }
        away_cats = [
            {**c, "my_val": c["opp_val"], "opp_val": c["my_val"], "result": _flip[c["result"]]}
            for c in cats
        ]
        all_matchups[" ".join(away_name.split())] = {
            "week": current_week, "my_team": away_name, "opp_team": home_name,
            "wins": losses, "losses": wins, "ties": ties, "categories": away_cats,
        }

    return all_matchups



def get_matchup_dates(league) -> dict:
    """Return actual start/end dates for the current and next matchup periods.

    Uses finalScoringPeriod to infer whether the current matchup is longer than
    7 days (e.g. All-Star break = 14 days). ESPN's matchupPeriods dict maps each
    matchup period ID → a list of WEEKLY scoring period IDs (not daily), so
    len([15]) == 1 even when the All-Star break matchup spans 14 days. Instead:

      remaining_daily_sps  = finalScoringPeriod - this_monday_sp + 1
      expected_days        = remaining_regular_mps * 7 + playoff_days
      extra_days           = remaining_daily_sps - expected_days   (≥ 0)
      period_days          = 7 + min(extra_days, 7)

    Returns keys: matchup_start_date, matchup_end_date, matchup_period_days,
    next_matchup_end_date  (all YYYY-MM-DD strings except matchup_period_days=int).
    Returns {} if ESPN doesn't expose the needed fields.
    """
    current_week = getattr(league, "currentMatchupPeriod", None)
    today_sp     = getattr(league, "scoringPeriodId",      None)
    final_sp     = getattr(league, "finalScoringPeriod",   None)
    if not current_week or not today_sp or not final_sp:
        return {}
    matchup_periods = getattr(league.settings, "matchup_periods", {}) or {}
    if not matchup_periods:
        return {}

    today = datetime.now().date()
    # This Monday's daily scoring period (anchor for start_date)
    matchup_start_sp = int(today_sp) - today.weekday()
    start_date = today - timedelta(days=today.weekday())

    # Counts: regular (1 weekly SP) vs playoff/extended (>1 weekly SPs)
    regular_mp_count  = sum(1 for v in matchup_periods.values() if len(v) == 1)
    playoff_sps       = sum(len(v) for v in matchup_periods.values() if len(v) > 1)
    playoff_days      = playoff_sps * 7

    # How many regular matchup periods remain from this one onward?
    remaining_regular = regular_mp_count - int(current_week) + 1
    remaining_daily   = int(final_sp) - matchup_start_sp + 1
    expected_days     = max(0, remaining_regular) * 7 + playoff_days
    extra_days        = max(0, remaining_daily - expected_days)
    period_days       = 7 + min(extra_days, 7)
    end_date          = start_date + timedelta(days=period_days - 1)

    # Next matchup: advance one period forward
    next_start_sp     = matchup_start_sp + period_days
    next_remaining    = int(final_sp) - next_start_sp + 1
    next_regular      = remaining_regular - 1
    next_expected     = max(0, next_regular) * 7 + playoff_days
    next_extra        = max(0, next_remaining - next_expected)
    next_period_days  = 7 + min(next_extra, 7)
    next_end          = end_date + timedelta(days=next_period_days)

    # Count actual MLB game days in the matchup window (excludes All-Star break etc.)
    start_str = start_date.strftime("%Y-%m-%d")
    end_str   = end_date.strftime("%Y-%m-%d")
    today_str = today.strftime("%Y-%m-%d")
    try:
        sched = requests.get(
            f"https://statsapi.mlb.com/api/v1/schedule"
            f"?sportId=1&startDate={start_str}&endDate={end_str}&gameType=R",
            timeout=10,
        ).json()
        game_date_set = {d["date"] for d in sched.get("dates", []) if d.get("games")}
        matchup_game_days    = len(game_date_set)
        game_days_elapsed    = sum(1 for d in game_date_set if d < today_str)
    except Exception:
        matchup_game_days    = period_days
        game_days_elapsed    = (today - start_date).days

    return {
        "matchup_start_date":         start_date.strftime("%Y-%m-%d"),
        "matchup_end_date":           end_date.strftime("%Y-%m-%d"),
        "matchup_period_days":        period_days,
        "next_matchup_end_date":      next_end.strftime("%Y-%m-%d"),
        "matchup_game_days":          matchup_game_days,
        "matchup_game_days_elapsed":  game_days_elapsed,
    }


# ESPN proTeamId (1..30, see espn_api PRO_TEAM_MAP) -> MLB StatsAPI team id. Both id sets are
# stable; used to gate the idle "wasting space" check on whether a hitter's team actually played.
_ESPN_PROID_TO_MLBID = {
    1: 110, 2: 111, 3: 108, 4: 145, 5: 114, 6: 116, 7: 118, 8: 158, 9: 142, 10: 147,
    11: 133, 12: 136, 13: 140, 14: 141, 15: 144, 16: 112, 17: 113, 18: 117, 19: 119, 20: 120,
    21: 121, 22: 143, 23: 134, 24: 138, 25: 135, 26: 137, 27: 115, 28: 146, 29: 109, 30: 139,
}


def get_lineup_efficiency(league, my_team_name: str, mode: str = "prev") -> dict:
    """Reconstruct MY team's DAILY lineup and quantify the opportunity cost of my
    start/sit calls. BOTH modes span the FULL matchup period (the exact daily scoring
    periods ESPN lists in the matchup's pointsByScoringPeriod - so a 14-day All-Star/
    playoff matchup is covered end-to-end, not just one calendar week). `mode="prev"`
    audits the last COMPLETED matchup (its full span, for the Monday recap post-mortem);
    `mode="current"` audits the IN-PROGRESS matchup from its first day -> yesterday (for
    the daily digest, so misses are still fixable). Returns {} on a Monday with no
    completed days yet.

      (a) BATTER BENCH LEAKAGE - a hitter's R/HR/RBI/SB put up while sitting in a BE
          slot (never counted), NET of the weakest startable bat I'd have benched to
          play him (open slot => net of nothing). Quiet bench days (nothing to
          recover) are ignored.
      (b) PITCHER BLOWUPS - an active-slot start of 5+ ER (or 4+ ER in <3 IP) that
          counted toward ERA/WHIP, flagged if I then dropped him (damage banked).
      (c) IDLE ACTIVE HITTERS ("wasting space") - a hitter in an active slot not getting
          ABs. Only games his MLB team actually PLAYED count (schedule-gated). Flagged
          only on a pattern: 3+ idle games in a row, or an AB in <50% of active games
          (min 4) - an occasional day off stays silent.

    Uses `mRoster` fetched per `scoringPeriodId` (the only way to see the slot AS SET
    that day for a categories league - box_scores exposes team totals only) plus the
    league's lineupSlotCounts + each player's eligibleSlots for a max-bipartite-matching
    'could he have been slotted without benching anyone' test. Returns a
    JSON-serializable dict consumed by weekly_recap. Standalone/opponent version lives
    in bench_leakage.py. Returns {} on any failure (never breaks the fetch)."""
    try:
        AB, H, HR, R, RBI, SB, B_SO = "0", "1", "5", "20", "21", "23", "27"
        TB, BB_H = "8", "10"
        OUTS, P_H, P_BB, ER, K = "34", "37", "39", "45", "48"
        PIT_IDS = {13, 14, 15}
        BE_ID, IL_ID = 16, 17
        HIT_POS = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 19}

        def _f(d, k):
            try:
                return float(d.get(k))
            except (TypeError, ValueError):
                return 0.0

        def _split(pl, sp):
            for s in pl.get("stats", []):
                if s.get("scoringPeriodId") == sp and s.get("statSourceId") == 0 and s.get("stats"):
                    return s["stats"]
            return {}

        def _is_pit(elig):
            return bool(set(elig) & PIT_IDS) and not (set(elig) & HIT_POS)

        def _full_match(player_eligs):
            match = {}

            def aug(p, seen):
                for s in player_eligs[p]:
                    if s in seen:
                        continue
                    seen.add(s)
                    if s not in match or aug(match[s], seen):
                        match[s] = p
                        return True
                return False
            return all(aug(p, set()) for p in range(len(player_eligs)))

        my_norm = " ".join(my_team_name.split())
        myid = next((t.team_id for t in league.teams if " ".join(t.team_name.split()) == my_norm), None)
        if myid is None:
            return {}

        today = datetime.now().date()
        today_sp = int(league.scoringPeriodId)
        this_mon_sp = today_sp - today.weekday()
        cur_week = int(getattr(league, "currentMatchupPeriod", 0))
        # True daily scoring-period span of each matchup, straight from ESPN's schedule:
        # a matchup entry's `home.pointsByScoringPeriod` keys ARE the exact days that count
        # toward it. This is robust for a 14-day All-Star/playoff matchup (which
        # matchup_periods encodes as a single "week", so a length-based guess misses it) and
        # equals this-Monday for a normal 7-day week. {} on any failure -> weekly fallback.
        sp_span = {}
        try:
            _sched = league.espn_request.league_get(
                params={"view": ["mMatchupScore"], "scoringPeriodId": today_sp}).get("schedule", [])
            for _m in _sched:
                mp = _m.get("matchupPeriodId")
                ks = [int(k) for k in ((_m.get("home") or {}).get("pointsByScoringPeriod") or {}).keys()]
                if mp is not None and ks:
                    lo, hi = min(ks), max(ks)
                    prev = sp_span.get(mp)
                    sp_span[mp] = (min(lo, prev[0]), max(hi, prev[1])) if prev else (lo, hi)
        except Exception:
            sp_span = {}

        if mode == "current":
            # Full in-progress matchup: its first day through YESTERDAY (today is incomplete,
            # excluded). Spans the whole period, so a 14-day matchup covers both weeks.
            span = sp_span.get(cur_week)
            start_sp = span[0] if span else this_mon_sp
            prev_days = list(range(start_sp, today_sp))
            start_date = today - timedelta(days=today_sp - start_sp)
            week = cur_week
        else:
            # Full last COMPLETED matchup: its first through last day (all days are complete).
            span = sp_span.get(cur_week - 1)
            if span:
                p_start, p_end = span
                prev_days = list(range(p_start, p_end + 1))
                start_date = today - timedelta(days=today_sp - p_start)
            else:
                prev_days = list(range(this_mon_sp - 7, this_mon_sp))
                start_date = today - timedelta(days=today.weekday() + 7)
            week = cur_week - 1
        if not prev_days:
            return {}
        dates = [start_date + timedelta(days=i) for i in range(len(prev_days))]

        slot_counts = league.espn_request.league_get(
            params={"view": "mSettings"})["settings"]["rosterSettings"]["lineupSlotCounts"]
        hit_inst, hit_ids = [], set()
        for sid_str, cnt in slot_counts.items():
            sid = int(sid_str)
            if sid in PIT_IDS or sid in (BE_ID, IL_ID) or cnt <= 0:
                continue
            hit_ids.add(sid)
            hit_inst.extend([sid] * cnt)

        # Schedule gate for "wasted active space": a 0-AB day only counts against a hitter
        # if his MLB team actually PLAYED that day. Build {(mlb_team_id, "YYYY-MM-DD")} of
        # completed games over the window in one ranged call. None => fetch failed, so idle
        # tracking falls back to counting every active day (feature stays alive, ungated).
        played_days = None
        try:
            sched = requests.get(
                f"https://statsapi.mlb.com/api/v1/schedule"
                f"?sportId=1&startDate={dates[0].strftime('%Y-%m-%d')}"
                f"&endDate={dates[-1].strftime('%Y-%m-%d')}&gameType=R",
                timeout=10,
            ).json()
            played_days = set()
            for d in sched.get("dates", []):
                ds = d.get("date")
                for g in d.get("games", []):
                    if (g.get("status", {}) or {}).get("abstractGameState") != "Final":
                        continue
                    for side in ("home", "away"):
                        tid = (((g.get("teams", {}) or {}).get(side, {}) or {}).get("team", {}) or {}).get("id")
                        if tid is not None:
                            played_days.add((tid, ds))
        except Exception:
            played_days = None

        hit_leak, pit_lines, idle_track = {}, [], {}
        for sp, dt in zip(prev_days, dates):
            ds = dt.strftime("%Y-%m-%d")
            data = league.espn_request.league_get(params={"view": "mRoster", "scoringPeriodId": sp})
            mt = next((t for t in data.get("teams", []) if t.get("id") == myid), None)
            if not mt:
                continue
            entries = mt["roster"]["entries"]

            active_hit = []  # (name, eligible-hit-slot-set, day-stats)
            for e in entries:
                pl = e["playerPoolEntry"]["player"]
                sid = e.get("lineupSlotId")
                elig = pl.get("eligibleSlots", [])
                if sid not in (BE_ID, IL_ID) and sid not in PIT_IDS and not _is_pit(elig):
                    st = _split(pl, sp)
                    active_hit.append((pl.get("fullName", "?"),
                                       {s for s in elig if s in hit_ids}, st))
                    # Idle "wasting space" tracking: only count days his MLB team had a game.
                    mlbid = _ESPN_PROID_TO_MLBID.get(pl.get("proTeamId"))
                    if played_days is None or (mlbid is not None and (mlbid, ds) in played_days):
                        t = idle_track.setdefault(pl.get("fullName", "?"), {"active": 0, "played": 0, "seq": []})
                        p = 1 if _f(st, AB) > 0 else 0
                        t["active"] += 1
                        t["played"] += p
                        t["seq"].append(p)

            def _fits(cand_elig):
                base = [[i for i, styp in enumerate(hit_inst) if styp in he] for _, he, _ in active_hit]
                cand = [i for i, styp in enumerate(hit_inst) if styp in cand_elig]
                return _full_match(base + [cand])

            for e in entries:
                pl = e["playerPoolEntry"]["player"]
                nm = pl.get("fullName", "?")
                sid = e.get("lineupSlotId")
                elig = pl.get("eligibleSlots", [])
                st = _split(pl, sp)
                if not st:
                    continue

                if not _is_pit(elig) and sid == BE_ID and _f(st, AB) > 0:
                    agg = hit_leak.setdefault(nm, {"R": 0, "HR": 0, "RBI": 0, "SB": 0, "H": 0, "AB": 0,
                                                   "net": {"R": 0, "HR": 0, "RBI": 0, "SB": 0}, "days": []})
                    for c, key in (("R", R), ("HR", HR), ("RBI", RBI), ("SB", SB), ("H", H), ("AB", AB)):
                        agg[c] += _f(st, key)
                    notable = _f(st, HR) or _f(st, SB) or _f(st, R) >= 2 or _f(st, RBI) >= 2
                    if not notable:
                        continue
                    cand_elig = {s for s in elig if s in hit_ids}
                    free = _fits(cand_elig)
                    disp_st, disp_nm = {}, None
                    if not free:
                        overlap = [(anm, ast) for anm, ae, ast in active_hit if ae & cand_elig]
                        if overlap:
                            best = min(overlap, key=lambda x: _f(x[1], TB) + _f(x[1], BB_H) + _f(x[1], SB))
                            disp_nm, disp_st = best[0], best[1]
                    for c, key in (("R", R), ("HR", HR), ("RBI", RBI), ("SB", SB)):
                        agg["net"][c] += _f(st, key) - _f(disp_st, key)
                    ln = f"{int(_f(st, H))}-{int(_f(st, AB))}"
                    ex = []
                    if _f(st, HR):  ex.append(f"{int(_f(st, HR))} HR")
                    if _f(st, RBI): ex.append(f"{int(_f(st, RBI))} RBI")
                    if _f(st, R):   ex.append(f"{int(_f(st, R))} R")
                    if _f(st, SB):  ex.append(f"{int(_f(st, SB))} SB")
                    if free:
                        tag = "open slot"
                    elif disp_nm:
                        tag = f"vs {disp_nm} {int(_f(disp_st, H))}-{int(_f(disp_st, AB))}"
                    else:
                        tag = "swap"
                    agg["days"].append({"date": dt.strftime("%a %m/%d"), "line": ln,
                                        "extra": ", ".join(ex), "tag": tag})

                if _is_pit(elig) and sid not in (BE_ID, IL_ID) and _f(st, OUTS) > 0:
                    pit_lines.append({"name": nm, "date": dt, "outs": _f(st, OUTS), "er": _f(st, ER),
                                      "k": _f(st, K), "h": _f(st, P_H), "bb": _f(st, P_BB)})

        # drops of my players (for implode-then-drop flag)
        my_drops = []
        try:
            for act in league.recent_activity(size=150):
                for team_obj, tx_type, player_obj in act.actions:
                    tn = " ".join((team_obj.team_name if team_obj else "").split())
                    if tn == my_norm and "DROP" in str(tx_type).upper():
                        my_drops.append((str(player_obj), datetime.fromtimestamp(act.date / 1000).date()))
        except Exception:
            pass

        blowups = []
        for p in sorted((x for x in pit_lines if x["er"] >= 5 or (x["er"] >= 4 and x["outs"] < 9)),
                        key=lambda x: x["er"], reverse=True):
            whole, rem = divmod(int(round(p["outs"])), 3)
            after = [d for nm, d in my_drops if nm == p["name"] and d >= p["date"] and (d - p["date"]).days <= 4]
            drop_when = None
            if after:
                lag = (min(after) - p["date"]).days
                drop_when = "same day" if lag == 0 else f"{lag}d later"
            blowups.append({"name": p["name"], "date": p["date"].strftime("%a %m/%d"),
                            "ip": f"{whole}.{rem}", "er": int(p["er"]), "k": int(p["k"]),
                            "h": int(p["h"]), "bb": int(p["bb"]), "drop_when": drop_when})

        bench = []
        for nm, a in sorted(hit_leak.items(), key=lambda kv: (kv[1]["HR"], kv[1]["RBI"], kv[1]["R"]), reverse=True):
            if not a["days"]:
                continue  # produced nothing notable while benched
            bench.append({"name": nm, "H": int(a["H"]), "AB": int(a["AB"]), "R": int(a["R"]),
                          "HR": int(a["HR"]), "RBI": int(a["RBI"]), "SB": int(a["SB"]),
                          "net": {k: round(v) for k, v in a["net"].items()}, "days": a["days"]})

        net = {c: round(sum(a["net"][c] for a in hit_leak.values())) for c in ("R", "HR", "RBI", "SB")}
        gross = {c: int(sum(a[c] for a in hit_leak.values())) for c in ("R", "HR", "RBI", "SB")}

        # (c) idle "wasting space": an active-slot hitter not accumulating stats. Only games
        # his team actually played count (schedule-gated above). Flag when it's a pattern -
        # 3+ idle in a row, or an AB in < 50% of his active games (min 4 so tiny samples stay
        # quiet) - so an occasional day off never trips it.
        idle = []
        for nm, t in idle_track.items():
            active_n, played_n = t["active"], t["played"]
            seq = t["seq"]
            idle_n = active_n - played_n
            max_streak = _cur = 0
            for p in seq:
                _cur = 0 if p else _cur + 1
                max_streak = max(max_streak, _cur)
            trail = 0
            for p in reversed(seq):
                if p:
                    break
                trail += 1
            streak_flag = max_streak >= 3
            rate_flag = active_n >= 4 and played_n / active_n < 0.50
            if not (streak_flag or rate_flag):
                continue
            if trail >= 3:
                reason = f"{trail} straight idle games"
            elif streak_flag:
                reason = f"{max_streak} straight idle games"
            else:
                reason = f"played only {played_n} of {active_n} games"
            idle.append({"name": nm, "active": active_n, "played": played_n, "idle": idle_n,
                         "max_streak": max_streak, "trail_streak": trail, "reason": reason})
        idle.sort(key=lambda x: (x["trail_streak"], x["idle"]), reverse=True)

        return {
            "week": week, "mode": mode,
            "week_dates": f"{dates[0].strftime('%b %d')} - {dates[-1].strftime('%b %d')}",
            "bench": bench, "gross": gross, "net": net, "blowups": blowups, "idle": idle,
        }
    except Exception as e:
        log(f"  get_lineup_efficiency failed: {e}")
        return {}


def get_all_prev_matchups(league) -> dict:
    """Return the most recently completed matchup for ALL teams as
    {normalized_team_name: matchup_dict} — same structure/keys as get_all_matchups,
    so send_digest can resolve the prev-week recap per team (--team flag) instead of
    only for MY_TEAM. Mirrors get_all_matchups but on the previous week."""
    current_week = getattr(league, "currentMatchupPeriod", None)
    prev_week = current_week - 1 if current_week and current_week > 1 else None
    if not prev_week:
        return {}
    try:
        boxes = league.box_scores(prev_week)
    except Exception:
        return {}

    all_prev = {}
    _flip = {"W": "L", "L": "W", "T": "T"}

    for b in boxes:
        home_name  = b.home_team.team_name
        away_name  = b.away_team.team_name
        home_stats = getattr(b, "home_stats", {}) or {}
        away_stats = getattr(b, "away_stats", {}) or {}

        wins = losses = ties = 0
        cats = []
        for cat in ROTO_CATS:
            h_info = home_stats.get(cat, {}) or {}
            a_info = away_stats.get(cat, {}) or {}
            h_val  = float(h_info.get("value") or 0)
            a_val  = float(a_info.get("value") or 0)

            # Trust ESPN's own result (applies the ratio-stat IP minimum); see get_all_matchups.
            espn_res = str(h_info.get("result") or "").upper()
            if espn_res in ("WIN", "LOSS", "TIE"):
                result = {"WIN": "W", "LOSS": "L", "TIE": "T"}[espn_res]
            elif h_val == a_val:
                result = "T"
            elif cat in LOWER_BETTER:
                result = "W" if h_val < a_val else "L"
            else:
                result = "W" if h_val > a_val else "L"

            if result == "W":   wins += 1
            elif result == "L": losses += 1
            else:               ties += 1

            cats.append({
                "cat": cat, "my_val": h_val, "opp_val": a_val,
                "result": result, "lower_better": cat in LOWER_BETTER,
            })

        all_prev[" ".join(home_name.split())] = {
            "week": prev_week, "my_team": home_name, "opp_team": away_name,
            "wins": wins, "losses": losses, "ties": ties, "categories": cats,
        }
        away_cats = [
            {**c, "my_val": c["opp_val"], "opp_val": c["my_val"], "result": _flip[c["result"]]}
            for c in cats
        ]
        all_prev[" ".join(away_name.split())] = {
            "week": prev_week, "my_team": away_name, "opp_team": home_name,
            "wins": losses, "losses": wins, "ties": ties, "categories": away_cats,
        }

    return all_prev


# â”€â”€ MAIN â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("  Fantasy Baseball Data Refresh")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    print("\n[1/10] Connecting to ESPN...")
    league = connect_espn()
    if not league:
        sys.exit("Could not connect to ESPN â€” check credentials in ESPN_CONFIG.")

    print("\n[2/10] Building pitcher data (FantasyPros + ESPN + FanGraphs advanced)...")
    pitchers = build_pitcher_data(league)
    print(f"       {len(pitchers)} pitcher rows")

    print("\n[3/10] Building hitter data (FantasyPros + ESPN + FanGraphs + Statcast)...")
    hitters = build_hitter_data(league)
    print(f"       {len(hitters)} hitter rows")

    print("\n[4/10] Pulling roto scores...")
    roto = get_all_roto(league)
    weekly_results = get_weekly_matchup_results(league)
    season_cat_totals = get_season_cat_totals(league)
    print(f"       {len(roto)} roto rows, {len(weekly_results)} weeks of matchup results, {len(season_cat_totals)} season-total teams")

    print("\n[5/10] Pulling transactions...")
    transactions = get_transactions(league)
    print(f"       {len(transactions)} transaction rows")

    print("\n[6/10] Pulling standings...")
    standings = get_standings(league)
    print(f"       {len(standings)} teams")

    espn_names = [s["team_name"] for s in standings]
    normalized = {" ".join(n.split()): n for n in espn_names}
    my_team = normalized.get(" ".join(MY_TEAM_NAME.split())) or MY_TEAM_NAME or (espn_names[0] if espn_names else "")

    print("\n[7/10] Pulling current week matchups...")
    all_matchups      = get_all_matchups(league)
    current_matchup   = all_matchups.get(" ".join(my_team.split()), {})
    all_prev_matchups = get_all_prev_matchups(league)
    prev_matchup      = all_prev_matchups.get(" ".join(my_team.split()), {})
    matchup_dates     = get_matchup_dates(league)
    if current_matchup:
        days = matchup_dates.get("matchup_period_days", 7)
        print(f"       Week {current_matchup['week']}: {my_team} vs {current_matchup['opp_team']} ({current_matchup['wins']}-{current_matchup['losses']}) | {days}-day period | {len(all_matchups)} teams indexed")
    else:
        print("       No active matchup found.")
    if prev_matchup:
        prev_wk = prev_matchup.get("week", "?")
        print(f"       Prev week {prev_wk}: {prev_matchup['wins']}-{prev_matchup['losses']}-{prev_matchup['ties']} vs {prev_matchup['opp_team']}")

    print("       Reconstructing daily lineup efficiency (prev + current week)...")
    lineup_efficiency = get_lineup_efficiency(league, my_team, mode="prev")            # Monday recap
    lineup_efficiency_current = get_lineup_efficiency(league, my_team, mode="current")  # daily digest
    for _lbl, _eff in (("prev", lineup_efficiency), ("current", lineup_efficiency_current)):
        if _eff.get("bench") or _eff.get("blowups") or _eff.get("idle"):
            _n = _eff.get("net", {})
            print(f"       [{_lbl}] bench net: {_n.get('HR',0):+} HR / {_n.get('RBI',0):+} RBI | "
                  f"{len(_eff.get('blowups',[]))} active-slot blowup(s) | "
                  f"{len(_eff.get('idle',[]))} idle active hitter(s)")

    print("\n[8/10] Fetching last-7-day hitter stats...")
    recent_hitting = fetch_recent_hitter_stats(days=7)
    print(f"       {len(recent_hitting)} hitters with recent stats")

    print("\n[9/10] Fetching last-15-day pitcher stats...")
    recent_pitching = fetch_recent_pitcher_stats(days=15)
    print(f"       {len(recent_pitching)} pitchers with recent stats")

    # Prev-week exact window (Mon–Sun) for weekly recap commissioner story
    _today = datetime.now()
    if _today.weekday() == 0:
        _prev_mon = _today - timedelta(days=7)
        _prev_sun = _today - timedelta(days=1)
    else:
        _dsm = _today.weekday()
        _prev_mon = _today - timedelta(days=_dsm + 7)
        _prev_sun = _prev_mon + timedelta(days=6)
    _pw_start = _prev_mon.strftime("%Y-%m-%d")
    _pw_end   = _prev_sun.strftime("%Y-%m-%d")
    print(f"\n       Fetching prev-week hitter stats ({_pw_start} to {_pw_end})...")
    prev_week_hitting = fetch_recent_hitter_stats(start_dt=_pw_start, end_dt=_pw_end)
    print(f"       {len(prev_week_hitting)} hitters in prev-week window")
    print(f"       Fetching prev-week pitcher stats ({_pw_start} to {_pw_end})...")
    prev_week_pitching = fetch_recent_pitcher_stats(start_dt=_pw_start, end_dt=_pw_end)
    print(f"       {len(prev_week_pitching)} pitchers in prev-week window")

    # Total roster cap = max total players (active + IL) on any team. The fullest team is at the cap.
    # send_digest uses: open_spots = league_total_roster_max - my_total → free pickup if > 0.
    from collections import Counter as _Counter
    _team_total = _Counter()
    for r in pitchers + hitters:
        tm = r.get("FantasyTeam", "")
        if tm and int(r.get("Dataset", 0) or 0) == CURRENT_YEAR:
            _team_total[tm] += 1
    league_total_roster_max = max(_team_total.values()) if _team_total else 28

    print("\n[10/10] Writing snapshot...")
    snapshot = {
        "refreshed_at":    datetime.now(timezone.utc).isoformat(),  # tz-aware (UTC) so the digest can show the fetch time in ET regardless of where it ran (CI is UTC, manual runs local)
        "my_team":         my_team,
        "league_year":     CURRENT_YEAR,
        "standings":       standings,
        "pitchers":        pitchers,
        "hitters":         hitters,
        "roto":            roto,
        "season_cat_totals": season_cat_totals,
        "weekly_results":  {str(k): v for k, v in weekly_results.items()},
        "transactions":    transactions,
        "current_matchup": current_matchup,
        "prev_matchup":    prev_matchup,
        "all_matchups":    all_matchups,
        "all_prev_matchups": all_prev_matchups,
        **matchup_dates,
        "league_total_roster_max": league_total_roster_max,
        "recent_hitting":    recent_hitting,
        "recent_pitching":   recent_pitching,
        "prev_week_hitting":  prev_week_hitting,
        "prev_week_pitching": prev_week_pitching,
        "lineup_efficiency":         lineup_efficiency,
        "lineup_efficiency_current": lineup_efficiency_current,
    }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(snapshot, f, default=str)

    print(f"\nSnapshot saved -> {OUTPUT_FILE}")
    print("=" * 60)


if __name__ == "__main__":
    main()
