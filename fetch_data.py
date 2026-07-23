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

from name_utils import _name_key  # canonical player-name join key (shared leaf module)

warnings.filterwarnings("ignore")

# â”€â”€ CONFIG â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Credentials come ONLY from environment variables: GitHub Actions secrets on CI,
# .env locally (loaded below; the repo is public, so no hardcoded fallbacks — ever).
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

ESPN_CONFIG = {
    "league_id": 277836,
    "year":      2026,
    "swid":      os.getenv("ESPN_SWID", ""),
    "espn_s2":   os.getenv("ESPN_S2", ""),
}
if not ESPN_CONFIG["swid"] or not ESPN_CONFIG["espn_s2"]:
    sys.exit(
        "ERROR: ESPN_SWID and ESPN_S2 are not set.\n"
        "Locally: add both to .env (see .env.example for how to find them).\n"
        "CI: set them as GitHub Actions repository secrets."
    )

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


# league.free_agents() defaults to size=50 (ESPN's top-50-by-%-owned). The realistic FA
# candidate pool is bounded by FantasyPros' own top-300-per-role scrape, not ESPN's full
# player universe -- a live cross-check found the default size=50 covers only ~13% of those
# real candidates by name (an injured/low-owned player like a 10-day-IL bench arm easily
# ranks outside the top 50), size=300 only ~63%, while size=1000 covers ~100% (verified: a
# 279/280-name match). Cost is ~1.6s per call at size=1000 (negligible against the ~60s
# fetch pipeline). Below this size, an unmatched FA's FreeAgentInjuryStatus/ESPN season
# stats silently default to blank -- indistinguishable from a genuinely healthy, checked
# player -- so don't lower this without re-running that coverage check.
_FA_PULL_SIZE = 1000


def get_pitcher_espn_svhd(league) -> pd.DataFrame:
    """Pull season stats from ESPN player stats (scoring period 0 = season total).
    Covers both rostered and FA pitchers. Returns K, W, IP, GS, GP, SV, HLD, SVHD, plus
    ERA/WHIP and the MLB club abbrev -- the last three exist so build_pitcher_data can
    SEED synthetic season rows for pitchers absent from the FantasyPros scrape (call-ups /
    low-owned FAs), which the left-from-FP merge would otherwise silently drop."""
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
            "ESPN_OUTS":  int(outs) if outs > 0 else -1,
            "ESPN_GS":    bd.get('GS',  -1),
            "ESPN_GP":    bd.get('GP',  -1),
            "ESPN_ERA":   bd.get('ERA',  -1),
            "ESPN_WHIP":  bd.get('WHIP', -1),
            "ESPN_Team":  getattr(pl, "proTeam", "") or "",
        })
        seen.add(pl.name)

    for tm in league.teams:
        for pl in tm.roster:
            if is_pitcher(pl):
                _extract(pl)
    for fa in league.free_agents(size=_FA_PULL_SIZE):
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
    for fa in league.free_agents(size=_FA_PULL_SIZE):
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
    for fa in league.free_agents(size=_FA_PULL_SIZE):
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


def get_hitter_espn_stats(league) -> pd.DataFrame:
    """Pull season hitting totals from ESPN's player stats breakdown (scoring period 0),
    for both rostered and FA hitters. The hitter analog of get_pitcher_espn_svhd, but used
    ONLY to SEED synthetic season rows for hitters absent from the FantasyPros scrape (hot
    call-ups / low-owned bats) that the left-from-FP merge in build_hitter_data would drop --
    it is never merged back onto existing rows, so no ESPN_* hitter columns pollute the
    snapshot. Batter strikeouts live under ESPN's 'B_SO' key (its 'K' is pitcher Ks, null for
    hitters); everything else is the standard AB/R/HR/RBI/SB + AVG/OBP/SLG/OPS breakdown."""
    rows = []
    seen = set()

    def _extract(pl):
        if pl.name in seen:
            return
        bd = (pl.stats or {}).get(0, {}).get('breakdown', {})
        rows.append({
            "PlayerName": pl.name,
            "ESPN_AB":   bd.get('AB',  -1),
            "ESPN_R":    bd.get('R',   -1),
            "ESPN_HR":   bd.get('HR',  -1),
            "ESPN_RBI":  bd.get('RBI', -1),
            "ESPN_SB":   bd.get('SB',  -1),
            "ESPN_B_SO": bd.get('B_SO', -1),
            "ESPN_AVG":  bd.get('AVG', -1),
            "ESPN_OBP":  bd.get('OBP', -1),
            "ESPN_SLG":  bd.get('SLG', -1),
            "ESPN_OPS":  bd.get('OPS', -1),
            "ESPN_Team": getattr(pl, "proTeam", "") or "",
        })
        seen.add(pl.name)

    for tm in league.teams:
        for pl in tm.roster:
            if is_hitter(pl):
                _extract(pl)
    for fa in league.free_agents(size=_FA_PULL_SIZE):
        if is_hitter(fa):
            _extract(fa)

    df = pd.DataFrame(rows).drop_duplicates(subset="PlayerName")
    log(f"  ESPN season stats: {len(df)} hitters")
    return apply_name_patches(df, HITTER_NAME_PATCHES)


# â”€â”€ PROBABLE STARTERS â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _strip_accents(name: str) -> str:
    """'MartÃ­n PÃ©rez' â†’ 'Martin Perez' so MLB API names match FantasyPros names."""
    return "".join(
        c for c in unicodedata.normalize("NFD", name)
        if unicodedata.category(c) != "Mn"
    )


# ESPN publishes a PROJECTED probable starter for every game a full week out (its own
# rotation model), while the MLB Stats API only CONFIRMS ~2 days out -- so sourcing purely
# from ESPN populates the mid-week days (Thu/Fri) the old MLB-confirmed + homemade
# rotation-walk approach left empty. ESPN exposes NO confirmed/projected flag, so
# PSP_Projected is inferred from how many days out the start is: today+tomorrow are treated
# as confirmed (MLB has firmed those up and ESPN mirrors them), >= _PROJECTED_MIN_DAYS_OUT
# out as projected. Since the daily fetch re-runs, a projection is superseded by the real
# confirmed line as the date approaches; confirmed also wins the _attach_start_lists dedup.
_PROJECTED_MIN_DAYS_OUT = 2
_ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard?dates={ymd}"


def get_probable_starters(days: int = SP_DAYS_OUT) -> pd.DataFrame:
    """Probable starters for the next `days` days from ESPN's public MLB scoreboard.

    One scoreboard call per day; each game's home/away `probables` entry gives the starter.
    ESPN projects a probable for every game a full week out (where the MLB Stats API only
    confirms ~48h ahead), so this fills the mid-week gap the prior MLB-only + rotation-walk
    method left empty. ESPN carries no confirmed/projected flag, so PSP_Projected is inferred
    from days-out (< _PROJECTED_MIN_DAYS_OUT -> confirmed). Team display names match MLB's
    exactly, so PSP_HomeVAway keeps the 'vs/@  <full team name>' form the opponent-OPS merge
    and opp_logo already expect. Names are accent-stripped so 'Martin Perez' merges cleanly.
    Returns the _attach_start_lists output (adds PSP_Dates/PSP_HomeVAways for two-start
    detection). Empty DataFrame (never raises) on total failure -> downstream degrades to
    'no upcoming starts', same as an MLB outage under the old method."""
    today  = datetime.now().date()
    frames = []
    for off in range(days):
        d = today + timedelta(days=off)
        try:
            j = requests.get(_ESPN_SCOREBOARD.format(ymd=d.strftime("%Y%m%d")), timeout=15).json()
        except Exception as e:
            log(f"  ESPN scoreboard {d} fetch failed: {e}")
            continue
        date_str  = d.strftime("%Y-%m-%d")
        projected = off >= _PROJECTED_MIN_DAYS_OUT
        for event in j.get("events", []):
            for comp in event.get("competitions", []):
                comps = comp.get("competitors", [])
                # home/away full team names for the "vs/@ OPP" string (match MLB names exactly)
                names = {c.get("homeAway"): (c.get("team") or {}).get("displayName", "") for c in comps}
                for c in comps:
                    probs = c.get("probables") or []
                    if not probs:
                        continue
                    ath = probs[0].get("athlete") or {}
                    nm  = ath.get("displayName") or probs[0].get("displayName") or ""
                    if not nm or nm.strip().upper() == "TBD":
                        continue
                    side = c.get("homeAway")
                    opp  = names.get("away") if side == "home" else names.get("home")
                    ha   = f"vs {opp}" if side == "home" else f"@ {opp}"
                    frames.append({
                        "PlayerName":    _strip_accents(nm),
                        "PSP_HomeVAway": ha,
                        "PSP_Date":      date_str,
                        "PSP_Projected": bool(projected),
                    })
    if not frames:
        log("  Probable starters (ESPN): 0 entries (scoreboard empty or unreachable)")
        return pd.DataFrame(columns=["PlayerName", "PSP_HomeVAway", "PSP_Date",
                                     "PSP_Projected", "PSP_Dates", "PSP_HomeVAways"])
    # confirmed (False) sorts before projected (True), so the _attach_start_lists dedup keeps
    # a confirmed entry over a projected one for the same pitcher/date.
    df = pd.DataFrame(frames).sort_values(["PSP_Date", "PSP_Projected"])
    n_conf = int((~df["PSP_Projected"]).sum())
    log(f"  Probable starters (ESPN): {len(df)} entries ({n_conf} confirmed + {len(df) - n_conf} projected) over {days} days")
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


def get_pending_trades(league, my_team_name) -> list:
    """Pending trade PROPOSALS involving my team, from MY perspective.

    espn_api does not expose pending trades, so we hit the raw ESPN endpoint with the
    same cookies the League object holds. Trades live under the top-level
    `pendingTransactions` key (NOT `transactions`). For each PENDING TRADE_PROPOSAL we
    record its direction (get/give) from my team's id and whether it is INCOMING (a
    rival proposed it -> awaiting my accept/decline/counter) or OUTGOING (I proposed it
    -> awaiting the partner). Only trades that touch my team are kept. Broad try/except
    -> [] so an ESPN hiccup never blocks the snapshot write (same discipline as
    fetch_todays_games)."""
    try:
        my_key = " ".join((my_team_name or "").split())
        team_by_id = {t.team_id: t.team_name for t in league.teams}
        my_id = next((tid for tid, nm in team_by_id.items()
                      if " ".join((nm or "").split()) == my_key), None)
        if my_id is None:
            return []

        # player id -> (name, proTeam abbrev) from every roster (traded players are rostered)
        pid_info = {}
        for t in league.teams:
            for p in t.roster:
                pid_info[p.playerId] = (p.name, getattr(p, "proTeam", "") or "")

        def _pname(pid):
            if pid in pid_info:
                return pid_info[pid]
            try:
                info = league.player_info(playerId=pid)
                if info:
                    return (info.name, getattr(info, "proTeam", "") or "")
            except Exception:
                pass
            return (f"player#{pid}", "")

        my_swid = str(ESPN_CONFIG.get("swid") or "").upper()
        url = (f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/flb/seasons/"
               f"{ESPN_CONFIG['year']}/segments/0/leagues/{ESPN_CONFIG['league_id']}"
               f"?view=mPendingTransactions")
        cookies = {"espn_s2": ESPN_CONFIG["espn_s2"], "SWID": ESPN_CONFIG["swid"]}
        resp = requests.get(url, cookies=cookies,
                            headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        top = data if isinstance(data, dict) else data[0]
        pending = top.get("pendingTransactions") or []

        out = []
        for tr in pending:
            if tr.get("type") != "TRADE_PROPOSAL" or tr.get("status") != "PENDING":
                continue
            items = tr.get("items") or []
            team_ids = {it.get("fromTeamId") for it in items} | {it.get("toTeamId") for it in items}
            if my_id not in team_ids:
                continue
            partner_id = next((tid for tid in team_ids if tid not in (my_id, None)), None)
            # I proposed it iff the proposing member's SWID is mine.
            incoming = str(tr.get("memberId") or "").upper() != my_swid

            get_side, give_side = [], []
            for it in items:
                if it.get("type") != "TRADE":
                    continue
                nm, pro = _pname(it.get("playerId"))
                entry = {"name": nm, "playerId": it.get("playerId"), "mlb_team": pro}
                if it.get("toTeamId") == my_id:
                    get_side.append(entry)
                elif it.get("fromTeamId") == my_id:
                    give_side.append(entry)

            expires = tr.get("expirationDate")
            try:
                expires_iso = (datetime.fromtimestamp(expires / 1000, tz=timezone.utc).isoformat()
                               if expires else "")
            except Exception:
                expires_iso = ""

            out.append({
                "id":       tr.get("id"),
                "proposer": team_by_id.get(tr.get("teamId"), ""),
                "partner":  team_by_id.get(partner_id, ""),
                "incoming": incoming,
                "status":   tr.get("status"),
                "expires":  expires_iso,
                "get":      get_side,
                "give":     give_side,
            })
        return out
    except Exception as e:
        log(f"get_pending_trades failed: {e}")
        return []


# â”€â”€ PITCHER PIPELINE â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

# ESPN proTeam abbrev (from PRO_TEAM_MAP, e.g. 'ChW'/'Oak') -> the UPPERCASE abbrev the logo
# map (fantasy/ui.py _TEAM_ESPN) + FantasyPros both key on. Only two diverge once uppercased
# (White Sox ChW->CWS, Athletics Oak->ATH); all 28 others match after .upper(), so a plain
# uppercase + this override yields both a resolvable logo and FP-consistent Team casing.
_ESPN_ABBREV_FIX = {"CHW": "CWS", "OAK": "ATH"}


def _norm_espn_team(espn_abbrev) -> str:
    t = str(espn_abbrev or "").upper()
    return _ESPN_ABBREV_FIX.get(t, t)


def _seed_offfp_pitchers(fp, espn_svhd) -> pd.DataFrame:
    """Build FP-schema season rows for pitchers present in ESPN (rostered/FA) but ABSENT from
    the FantasyPros scrape, so the left-from-FP merge in build_pitcher_data can't silently
    drop them (fresh call-ups / low-owned FAs). Seeds only the fields the scoring/projection
    math needs from ESPN's own season breakdown (IP/ERA/WHIP/K/GS/G); Statcast (xERA/whiff)
    is backfilled downstream by the existing Savant merges wherever the arm qualifies, and the
    projection's _ERA_REG_PRIOR_IP=40 shrinkage regresses a thin sample toward league ERA.
    Requires a real MLB line (IP>0, K>=0, ERA>=0) so a 0-inning name never seeds a junk row."""
    if espn_svhd is None or espn_svhd.empty:
        return pd.DataFrame()
    fp_keys = set(fp["PlayerName"].map(_name_key))
    rows = []
    for r in espn_svhd.to_dict(orient="records"):
        nm = r.get("PlayerName", "")
        if not nm or _name_key(nm) in fp_keys:
            continue
        outs = int(r.get("ESPN_OUTS", -1) or -1)
        gp   = int(r.get("ESPN_GP",  -1) or -1)
        k    = float(r.get("ESPN_K",   -1) or -1)
        era  = float(r.get("ESPN_ERA", -1) or -1)
        # Need a real MLB line AND a game count (IP_per_G is GP-derived; a missing GP would
        # clip IP_per_G to 7.5 and misclassify a reliever as a workhorse starter).
        if outs <= 0 or gp <= 0 or k < 0 or era < 0:
            continue
        # FantasyPros IP is baseball notation (the decimal digit is OUTS, decoded by
        # innings_to_decimal downstream), so seed IP the same way -- a plain outs/3 decimal
        # would be mis-decoded (12.33 -> 23 innings). 37 outs -> 12 + 1/10 = "12.1".
        ip = outs // 3 + (outs % 3) / 10.0
        rows.append({
            "PlayerName": nm,
            "Team":       _norm_espn_team(r.get("ESPN_Team")),
            "Dataset":    CURRENT_YEAR,
            "IP":   ip,
            "K":    k,
            "ERA":  era,
            "WHIP": float(r.get("ESPN_WHIP", -1) or -1),
            "GS":   r.get("ESPN_GS", -1),
            "G":    r.get("ESPN_GP", -1),
            "SV":   r.get("ESPN_SV",  0) or 0,
            "HLD":  r.get("ESPN_HLD", 0) or 0,
            "Source": "ESPN",
        })
    return pd.DataFrame(rows)


def _seed_offfp_hitters(fp, espn_hit) -> pd.DataFrame:
    """Hitter analog of _seed_offfp_pitchers: build FP-schema season rows for hitters present
    in ESPN (rostered/FA) but ABSENT from the FantasyPros scrape, so the left-from-FP merge in
    build_hitter_data can't silently drop them (hot call-ups / low-owned bats). Seeds only the
    counting/rate fields the scoring math needs from ESPN's breakdown (AB/R/HR/RBI/SB + AVG/
    OBP/SLG/OPS, plus batter Ks under FP's 'K' column). wRC+ derives from the seeded OPS, ISO
    from SLG-AVG, and Statcast (xBA/xSLG/xwOBA/SprintSpeed/Barrel) is backfilled downstream by
    the existing Savant merges wherever the bat qualifies. Requires a real MLB line (AB>0) so a
    0-AB name never seeds a junk row. Unlike pitcher IP, hitter counts need no notation fix."""
    if espn_hit is None or espn_hit.empty:
        return pd.DataFrame()
    fp_keys = set(fp["PlayerName"].map(_name_key))
    rows = []
    for r in espn_hit.to_dict(orient="records"):
        nm = r.get("PlayerName", "")
        if not nm or _name_key(nm) in fp_keys:
            continue
        ab = float(r.get("ESPN_AB", -1) or -1)
        if ab <= 0:
            continue
        rows.append({
            "PlayerName": nm,
            "Team":       _norm_espn_team(r.get("ESPN_Team")),
            "Dataset":    CURRENT_YEAR,
            "AB":  ab,
            "R":   r.get("ESPN_R",   -1),
            "HR":  r.get("ESPN_HR",  -1),
            "RBI": r.get("ESPN_RBI", -1),
            "SB":  r.get("ESPN_SB",  -1),
            "K":   r.get("ESPN_B_SO", -1),   # FP's hitter 'K' column = batter strikeouts (ESPN 'B_SO')
            "AVG": r.get("ESPN_AVG", -1),
            "OBP": r.get("ESPN_OBP", -1),
            "SLG": r.get("ESPN_SLG", -1),
            "OPS": r.get("ESPN_OPS", -1),
            "Source": "ESPN",
        })
    return pd.DataFrame(rows)


def build_pitcher_data(league) -> list:
    log("Fetching pitcher stats from FantasyProsâ€¦")
    fp = fetch_fantasypros("pitchers")
    if fp.empty:
        return []
    fp["Source"] = "FP"

    # Widen the universe: FantasyPros is the base frame and every downstream enrichment is a
    # left-join onto it, so a pitcher absent from the FP scrape would be dropped. Seed synthetic
    # season rows from ESPN's own breakdown (already pulled below) BEFORE the coercion/merges so
    # they flow through the identical probable-starter + Statcast + season-override pipeline.
    espn_svhd = get_pitcher_espn_svhd(league)
    seeded = _seed_offfp_pitchers(fp, espn_svhd)
    if not seeded.empty:
        fp = pd.concat([fp, seeded], ignore_index=True)
        log(f"  Seeded {len(seeded)} off-FantasyPros pitchers from ESPN")
    # Seed-only breakdown columns must not ride the season-override merge onto every row.
    espn_svhd = espn_svhd.drop(columns=["ESPN_ERA", "ESPN_WHIP", "ESPN_Team", "ESPN_OUTS"], errors="ignore")

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
    # espn_svhd already fetched above (for the off-FP seeding) — reused here for the season override.

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
    # FA_Matched: True only when merge_on_name actually found this player in the ESPN FA pull
    # (exact or accent/suffix fallback) -- i.e. FreeAgentInjuryStatus below is a real, checked
    # status rather than a default blank. Computed before FA_Position is filled/dropped.
    merged["FA_Matched"] = merged["FA_Position"].notna()

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
    fp["Source"] = "FP"

    # Widen the universe (mirror of build_pitcher_data): FantasyPros is the base frame and
    # every downstream enrichment is a left-join onto it, so a hitter absent from the FP scrape
    # would be dropped. Seed synthetic season rows from ESPN's own hitting breakdown BEFORE the
    # merges so they flow through the identical roster/FA + Statcast + wRC+/ISO pipeline. Lower
    # risk than pitchers: the hit_pctile pool gates on AB, so low-AB seeds can't skew percentiles.
    espn_hit = get_hitter_espn_stats(league)
    seeded   = _seed_offfp_hitters(fp, espn_hit)
    if not seeded.empty:
        fp = pd.concat([fp, seeded], ignore_index=True)
        log(f"  Seeded {len(seeded)} off-FantasyPros hitters from ESPN")

    log("Getting hitter roster from ESPNâ€¦")
    roster_df = get_hitter_roster(league)
    fa_df     = get_hitter_fa(league)

    # Same pattern as build_pitcher_data: avoid Position_x / Position_y collision, and bring
    # ESPN_Status along with the roster merge. merge_on_name adds the accent/suffix-insensitive
    # fallback (FP 'Luis Garcia' ↔ ESPN 'Luis García Jr.').
    merged = merge_on_name(fp, roster_df, ["PlayerName", "FantasyTeam", "Position", "ESPN_Status", "ESPN_OnIL"])
    merged["ESPN_Status"] = merged["ESPN_Status"].fillna("")
    # ESPN_OnIL: True only for a rostered player sitting in an IL lineup slot (dropping
    # them frees no active/bench room). Unmatched (FP-only / FA) rows default to False.
    # Keep native python bools (see build_pitcher_data note on json default=str).
    merged["ESPN_OnIL"] = merged.get("ESPN_OnIL", False).fillna(False)
    merged = merge_on_name(merged, fa_df, ["PlayerName", "FreeAgentInjuryStatus", "FA_Position"])
    # FA_Matched: True only when merge_on_name actually found this player in the ESPN FA pull
    # (exact or accent/suffix fallback) -- i.e. FreeAgentInjuryStatus below is a real, checked
    # status rather than a default blank. Computed before FA_Position is filled/dropped.
    merged["FA_Matched"] = merged["FA_Position"].notna()

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

    # NOTE: season_df already carries a real ESPN_Status (inherited from `merged`'s roster
    # merge above) -- an older, narrower re-derivation used to live here before that upstream
    # merge existed. compute_hr_probability doesn't read ESPN_Status either, so nothing here
    # needs it recomputed.
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



def _current_matchup_start_sp(current_week):
    """Earliest DAILY scoring-period id that belongs to the current matchup period.

    Read from each matchup's `home/away.pointsByScoringPeriod` keys (the authoritative
    day span ESPN actually scored) via the raw mMatchupScore view — the same source
    get_lineup_efficiency trusts. This is the ONLY reliable start signal for a multi-week
    (All-Star) matchup: ESPN's matchup_periods dict is degenerate here (period 15 -> [15]),
    so a "this Monday" anchor lands a full week late when the matchup is in its 2nd week.
    Returns the min day id, or None on any failure so the caller falls back to the Monday
    anchor. (Daily SP ids are consecutive calendar days, so a SP delta == a day delta.)
    """
    try:
        url = (f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/flb/seasons/"
               f"{ESPN_CONFIG['year']}/segments/0/leagues/{ESPN_CONFIG['league_id']}"
               f"?view=mMatchupScore")
        cookies = {"espn_s2": ESPN_CONFIG["espn_s2"], "SWID": ESPN_CONFIG["swid"]}
        resp = requests.get(url, cookies=cookies,
                            headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        resp.raise_for_status()
        sched = resp.json().get("schedule", []) or []
        sps = set()
        for m in sched:
            if m.get("matchupPeriodId") != current_week:
                continue
            for side in ("home", "away"):
                pbsp = (m.get(side) or {}).get("pointsByScoringPeriod") or {}
                for k in pbsp:
                    try:
                        sps.add(int(k))
                    except (TypeError, ValueError):
                        pass
        return min(sps) if sps else None
    except Exception:
        return None


def get_matchup_dates(league) -> dict:
    """Return actual start/end dates for the current and next matchup periods.

    START is anchored at the earliest daily scoring period actually scored in the current
    matchup (`_current_matchup_start_sp`, from pointsByScoringPeriod), NOT "this Monday" —
    the Monday anchor lands a full week late for the 2nd week of a multi-week (All-Star)
    matchup. LENGTH still uses finalScoringPeriod to detect a >7-day matchup: ESPN's
    matchupPeriods dict maps each matchup period ID → a list of WEEKLY scoring period IDs
    (not daily), so len([15]) == 1 even when the All-Star matchup spans 14 days. Instead:

      remaining_daily_sps  = finalScoringPeriod - true_start_sp + 1
      expected_days        = remaining_regular_mps * 7 + playoff_days
      extra_days           = remaining_daily_sps - expected_days   (≥ 0)
      period_days          = 7 + min(extra_days, 7)

    Anchoring at the TRUE start (not this Monday) is what makes this surplus arithmetic
    resolve to 14 mid-All-Star-break: the extra first week is no longer excluded from
    remaining_daily_sps.

    Returns keys: matchup_start_date, matchup_end_date, matchup_period_days,
    next_matchup_end_date, matchup_game_days, matchup_game_days_elapsed  (all
    YYYY-MM-DD strings except the int fields). Returns {} if ESPN lacks the needed fields.
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
    # Anchor the matchup START at the earliest daily scoring period that actually belongs
    # to the current matchup (from pointsByScoringPeriod) — NOT "this Monday". The Monday
    # anchor is a full week late for the 2nd week of a multi-week (All-Star) matchup, which
    # zeroes out days_elapsed/game_days_elapsed and makes every counting-cat projection
    # stack a full period on top of already-banked stats. Daily SP ids are consecutive
    # days, so (today_sp - start_sp) == days between today and the start.
    true_start_sp = _current_matchup_start_sp(int(current_week))
    if true_start_sp is not None:
        matchup_start_sp = true_start_sp
        start_date = today - timedelta(days=int(today_sp) - true_start_sp)
    else:
        # Fallback: this Monday's daily scoring period (correct for a normal 7-day matchup)
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
    # and, from the SAME schedule response, per-MLB-team game counts (whole window vs
    # still-to-come) so hitter counting-cat projections can respect each team's real
    # remaining schedule (off-days/doubleheaders) instead of a league-wide time fraction.
    start_str = start_date.strftime("%Y-%m-%d")
    end_str   = end_date.strftime("%Y-%m-%d")
    today_str = today.strftime("%Y-%m-%d")
    team_win_games = {}   # mlb_team_id -> games in the whole window
    team_rem_games = {}   # mlb_team_id -> games with date > today
    try:
        sched = requests.get(
            f"https://statsapi.mlb.com/api/v1/schedule"
            f"?sportId=1&startDate={start_str}&endDate={end_str}&gameType=R",
            timeout=10,
        ).json()
        game_date_set = {d["date"] for d in sched.get("dates", []) if d.get("games")}
        matchup_game_days    = len(game_date_set)
        game_days_elapsed    = sum(1 for d in game_date_set if d < today_str)
        for d in sched.get("dates", []):
            gd = d.get("date", "")
            for g in d.get("games", []):
                for side in ("home", "away"):
                    tid = (((g.get("teams") or {}).get(side) or {}).get("team") or {}).get("id")
                    if not tid:
                        continue
                    team_win_games[tid] = team_win_games.get(tid, 0) + 1
                    if gd > today_str:
                        team_rem_games[tid] = team_rem_games.get(tid, 0) + 1
    except Exception:
        matchup_game_days    = period_days
        game_days_elapsed    = (today - start_date).days

    # Per-fantasy-team roster-weighted HITTER schedule fraction = fraction of the team's
    # window bat-games still to come. Mirrors the fetch_todays_games proTeam->ESPN->MLB id
    # join. Consumed by send_digest.compute_hit_proj to project R/HR/RBI/SB/B_SO off actual
    # remaining games (the pitching side already does this via remaining starts). Empty ->
    # send_digest falls back to the league-wide elapsed-fraction projection unchanged.
    team_hit_sched_frac = {}
    try:
        from espn_api.baseball.constant import PRO_TEAM_MAP
        _abbrev_to_espn = {v: k for k, v in PRO_TEAM_MAP.items()}
    except Exception:
        _abbrev_to_espn = {}
    if team_win_games:
        for tm in getattr(league, "teams", []) or []:
            sum_win = sum_rem = 0
            for pl in getattr(tm, "roster", []) or []:
                if not is_hitter(pl):
                    continue
                mlbid = _ESPN_PROID_TO_MLBID.get(_abbrev_to_espn.get(getattr(pl, "proTeam", None)))
                if not mlbid or mlbid not in team_win_games:
                    continue
                sum_win += team_win_games.get(mlbid, 0)
                sum_rem += team_rem_games.get(mlbid, 0)
            if sum_win > 0:
                team_hit_sched_frac[tm.team_name] = round(sum_rem / sum_win, 4)

    return {
        "matchup_start_date":         start_date.strftime("%Y-%m-%d"),
        "matchup_end_date":           end_date.strftime("%Y-%m-%d"),
        "matchup_period_days":        period_days,
        "next_matchup_end_date":      next_end.strftime("%Y-%m-%d"),
        "matchup_game_days":          matchup_game_days,
        "matchup_game_days_elapsed":  game_days_elapsed,
        "team_hit_sched_frac":        team_hit_sched_frac,
    }


# ESPN proTeamId (1..30, see espn_api PRO_TEAM_MAP) -> MLB StatsAPI team id. Both id sets are
# stable; used to gate the idle "wasting space" check on whether a hitter's team actually played.
_ESPN_PROID_TO_MLBID = {
    1: 110, 2: 111, 3: 108, 4: 145, 5: 114, 6: 116, 7: 118, 8: 158, 9: 142, 10: 147,
    11: 133, 12: 136, 13: 140, 14: 141, 15: 144, 16: 112, 17: 113, 18: 117, 19: 119, 20: 120,
    21: 121, 22: 143, 23: 134, 24: 138, 25: 135, 26: 137, 27: 115, 28: 146, 29: 109, 30: 139,
}


def _espn_is_reliever(pl) -> bool:
    """Usage-based RP detection from a rostered player's ESPN season GS/GP breakdown
    (mirrors send_digest._is_sp, inverted) so a reliever is identified by ROLE, not just a
    position string. GS/GP <= 0.20 with >= 5 appearances -> reliever; >= 0.80 -> starter;
    the ambiguous middle and thin samples fall back to eligibleSlots (RP-only -> reliever).
    Wrapped in try/except so a missing stats breakdown never blocks the snapshot."""
    try:
        bd = (pl.stats or {}).get(0, {}).get('breakdown', {})
        gs = bd.get('GS'); gp = bd.get('GP')
        if gp and gp >= 5 and gs is not None:
            rate = gs / gp
            if rate <= 0.20:
                return True
            if rate >= 0.80:
                return False
    except Exception:
        pass
    slots = pl.eligibleSlots or []
    return ('RP' in slots) and ('SP' not in slots)


def fetch_todays_games(league) -> list:
    """Today's real MLB games, enriched with which ROSTERED players (any fantasy team)
    are involved -- so send_digest can surface the games that most overlap the current
    matchup ("which broadcasts actually move my week"). Roster -> game join is on MLB
    team id (robust vs FantasyPros/StatsAPI abbrev quirks): each rostered player's ESPN
    proTeamId maps to an MLB StatsAPI team id via _ESPN_PROID_TO_MLBID, then bucketed to
    the game whose home/away team id matches. Each involved player carries its
    FantasyTeam so the renderer can apply my/opponent perspective (keeps --team working).
    A rostered pitcher is flagged is_sp when he's the game's confirmed probable starter
    (guaranteed to pitch; a bat may sit -- we have no posted MLB batting lineups) and is_rp
    when he's a reliever by ROLE (_espn_is_reliever) -- so the renderer can count relievers
    (whose save/hold chance moves the matchup) while still skipping a starter on his off-day. Games
    with zero rostered involvement are dropped to keep the snapshot lean. Broad
    try/except -> [] so a StatsAPI hiccup never blocks the snapshot write."""
    try:
        date_str = datetime.now().strftime("%Y-%m-%d")
        try:
            sched = requests.get(
                f"https://statsapi.mlb.com/api/v1/schedule"
                f"?sportId=1&date={date_str}&gameType=R"
                f"&hydrate=probablePitcher,broadcasts(all)",
                timeout=15,
            ).json()
        except Exception as e:
            log(f"  Today's games schedule fetch failed: {e}")
            return []

        # Bucket every rostered player by the MLB team id he plays for. espn_api exposes
        # the player's MLB club as a proTeam abbrev ('Sea'), not the numeric proTeamId, so
        # recover the ESPN id via PRO_TEAM_MAP then map ESPN id -> MLB StatsAPI id.
        try:
            from espn_api.baseball.constant import PRO_TEAM_MAP
            _abbrev_to_espn = {v: k for k, v in PRO_TEAM_MAP.items()}
        except Exception:
            _abbrev_to_espn = {}
        roster_by_mlbid = {}  # mlb_team_id -> [ {name, FantasyTeam, is_pitcher} ]
        for tm in league.teams:
            for pl in tm.roster:
                espn_id = _abbrev_to_espn.get(getattr(pl, "proTeam", None))
                mlbid = _ESPN_PROID_TO_MLBID.get(espn_id)
                if not mlbid:
                    continue
                _is_pit = is_pitcher(pl)
                roster_by_mlbid.setdefault(mlbid, []).append({
                    "name":        pl.name,
                    "FantasyTeam": tm.team_name,
                    "is_pitcher":  _is_pit,
                    "is_rp":       _is_pit and _espn_is_reliever(pl),
                })

        games = []
        for d in sched.get("dates", []):
            for g in d.get("games", []):
                home = g["teams"]["home"]
                away = g["teams"]["away"]
                home_id = home["team"].get("id")
                away_id = away["team"].get("id")

                # Probable starter per side (full name for display; "" when TBD).
                def _prob(side):
                    nm = (side.get("probablePitcher") or {}).get("fullName", "")
                    return "" if nm == "TBD" else nm
                home_prob = _prob(home)
                away_prob = _prob(away)
                probables = {_strip_accents(n) for n in (home_prob, away_prob) if n}

                involved = []
                for side_id in (home_id, away_id):
                    for pl in roster_by_mlbid.get(side_id, []):
                        involved.append({
                            "name":        pl["name"],
                            "FantasyTeam": pl["FantasyTeam"],
                            "is_p":        bool(pl["is_pitcher"]),
                            "is_rp":       bool(pl.get("is_rp")),
                            "is_sp":       bool(pl["is_pitcher"]
                                                and _strip_accents(pl["name"]) in probables),
                        })
                if not involved:
                    continue

                # Broadcasts: national TV (everyone) + each side's local TV feed (RSN),
                # so the reader always has a channel to tune to, not just the rare
                # nationally-televised game.
                national, home_tv, away_tv = [], "", ""
                for b in (g.get("broadcasts") or []):
                    if b.get("type") != "TV" or not b.get("name"):
                        continue
                    if b.get("isNational"):
                        if b["name"] not in national:
                            national.append(b["name"])
                    elif b.get("homeAway") == "home" and not home_tv:
                        home_tv = b["name"]
                    elif b.get("homeAway") == "away" and not away_tv:
                        away_tv = b["name"]

                games.append({
                    "gamePk":        g.get("gamePk"),
                    "game_time_utc": g.get("gameDate", ""),
                    "home_name":     home["team"].get("name", ""),
                    "away_name":     away["team"].get("name", ""),
                    "home_prob":     home_prob,
                    "away_prob":     away_prob,
                    "national_tv":   national,
                    "home_tv":       home_tv,
                    "away_tv":       away_tv,
                    "involved":      involved,
                })
        return games
    except Exception as e:
        log(f"  fetch_todays_games failed: {e}")
        return []


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
      (d) BENCHED-SP LEAKAGE - a good start (QS / 6+ K / a W) put up while sitting in a BE
          slot, so it never counted. Counting cats (K/QS/W) are netted vs the weakest
          same-day active pitcher I'd have benched to start him (open slot => full gain);
          ratios (ERA/WHIP) are shown as prose only, never netted (see build_bench_watch).

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
        W_ID, GS_ID = "53", "33"  # pitcher win / games-started (daily stat ids)
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

        # Pitcher active-slot instances (the SP/RP/P slots), for the benched-SP feasibility
        # test - the pitcher analog of hit_inst/hit_ids above.
        pit_inst, pit_slot_ids = [], set()
        for sid_str, cnt in slot_counts.items():
            sid = int(sid_str)
            if sid not in PIT_IDS or cnt <= 0:
                continue
            pit_slot_ids.add(sid)
            pit_inst.extend([sid] * cnt)

        hit_leak, pit_lines, idle_track, sp_leak = {}, [], {}, {}
        for sp, dt in zip(prev_days, dates):
            ds = dt.strftime("%Y-%m-%d")
            data = league.espn_request.league_get(params={"view": "mRoster", "scoringPeriodId": sp})
            mt = next((t for t in data.get("teams", []) if t.get("id") == myid), None)
            if not mt:
                continue
            entries = mt["roster"]["entries"]

            active_hit = []  # (name, eligible-hit-slot-set, day-stats)
            active_pit = []  # (name, eligible-pit-slot-set, day-stats)
            for e in entries:
                pl = e["playerPoolEntry"]["player"]
                sid = e.get("lineupSlotId")
                elig = pl.get("eligibleSlots", [])
                if sid in PIT_IDS:  # a pitcher in an active pitching slot
                    active_pit.append((pl.get("fullName", "?"),
                                       {s for s in elig if s in pit_slot_ids}, _split(pl, sp)))
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

            def _fits_pit(cand_elig):
                base = [[i for i, styp in enumerate(pit_inst) if styp in pe] for _, pe, _ in active_pit]
                cand = [i for i, styp in enumerate(pit_inst) if styp in cand_elig]
                return _full_match(base + [cand])

            def _pit_qs(s):  # a quality start on the day: 6+ IP (18 outs) and <=3 ER
                return 1 if (_f(s, OUTS) >= 18 and _f(s, ER) <= 3) else 0

            def _pit_val(s):  # weakest-displaced proxy: same-day pitching counting value
                return _f(s, K) + 5 * _f(s, W_ID) + 3 * _pit_qs(s)

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

                # (d) BENCHED-SP LEAKAGE - a good start put up while sitting in a BE slot
                # (never counted). GS>=1 (or 5+ IP as a start proxy if daily GS is unset)
                # marks the appearance as a start; the "good" gate (QS / 6+ K / a W) keeps a
                # mop-up quiet. Counting cats (K/QS/W) are netted vs the weakest same-day
                # active pitcher I'd have benched (a rest-day arm contributes 0 => full gain);
                # ratios (ERA/WHIP) are shown as directional prose only, never netted.
                if (_is_pit(elig) and sid == BE_ID
                        and (_f(st, GS_ID) >= 1 or _f(st, OUTS) >= 15)):
                    outs_, er_ = _f(st, OUTS), _f(st, ER)
                    k_, w_, qs_ = _f(st, K), _f(st, W_ID), _pit_qs(st)
                    if not (qs_ or k_ >= 6 or w_ >= 1):
                        continue
                    cand_elig = {s for s in elig if s in pit_slot_ids}
                    free = _fits_pit(cand_elig)
                    disp_st, disp_nm = {}, None
                    if not free:
                        overlap = [(anm, ast) for anm, ae, ast in active_pit if ae & cand_elig]
                        if overlap:
                            best = min(overlap, key=lambda x: _pit_val(x[1]))
                            disp_nm, disp_st = best[0], best[1]
                    agg = sp_leak.setdefault(nm, {"K": 0, "QS": 0, "W": 0,
                                                  "net": {"K": 0, "QS": 0, "W": 0}, "days": []})
                    agg["K"] += k_; agg["QS"] += qs_; agg["W"] += w_
                    agg["net"]["K"]  += k_  - _f(disp_st, K)
                    agg["net"]["QS"] += qs_ - _pit_qs(disp_st)
                    agg["net"]["W"]  += w_  - _f(disp_st, W_ID)
                    whole, rem = divmod(int(round(outs_)), 3)
                    ln = (f"{whole}.{rem} IP, {int(er_)} ER, {int(k_)} K"
                          + (", W" if w_ >= 1 else "") + (", QS" if qs_ else ""))
                    # A displaced arm who didn't pitch that day (0 outs) cost nothing to
                    # bench, so that's effectively an open slot - the common "your active
                    # SP was on his rest day" case.
                    if free or (disp_nm and _f(disp_st, OUTS) == 0):
                        tag = "open slot"
                    elif disp_nm:
                        d_whole, d_rem = divmod(int(round(_f(disp_st, OUTS))), 3)
                        tag = f"vs {disp_nm} {d_whole}.{d_rem} IP"
                    else:
                        tag = "swap"
                    agg["days"].append({"date": dt.strftime("%a %m/%d"), "line": ln,
                                        "k": int(k_), "w": int(w_), "qs": int(qs_), "tag": tag})

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

        # (d) benched-SP leakage: starts left on the bench, surfaced only when the net
        # counting gain (over the arm I'd have benched) is actually positive.
        bench_sp = []
        for nm, a in sorted(sp_leak.items(),
                            key=lambda kv: (kv[1]["net"]["W"], kv[1]["net"]["QS"], kv[1]["net"]["K"]),
                            reverse=True):
            if not (a["net"]["K"] > 0 or a["net"]["QS"] > 0 or a["net"]["W"] > 0):
                continue
            bench_sp.append({"name": nm, "K": int(a["K"]), "QS": int(a["QS"]), "W": int(a["W"]),
                             "net": {k: round(v) for k, v in a["net"].items()}, "days": a["days"]})
        # Headline sums only the surfaced (net-positive) starts, and floors each cat at 0 so a
        # single arm whose displaced replacement happened to grab that cat can't drag the
        # "left on the bench" total negative (which would read as if benching him was a loss).
        net_pit = {c: max(0, round(sum(b["net"][c] for b in bench_sp))) for c in ("K", "QS", "W")}

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
            "bench_sp": bench_sp, "net_pit": net_pit,
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

def fetch_injury_notes():
    """MLB injury body-part / detail / expected-return from ESPN's PUBLIC sports API (no auth) --
    a SEPARATE endpoint from the fantasy injuryStatus enum (ESPN_Status), which carries only the
    DL tier. Keyed by _name_key so it joins the accent/suffix-insensitive way the rest of the
    pipeline does. Each value = {body_part, detail, return_date}. Broad try/except -> {} so a
    fetch hiccup never breaks the run (rows just carry no injury detail). Mirrors the live copy in
    send_digest.fetch_injury_notes, but stored on rows here so EVERY snapshot reader (digest score
    dropdowns AND the browser-only Trade Lab) gets the detail without a network call of its own."""
    try:
        url = "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/injuries"
        resp = requests.get(url, timeout=8)
        data = resp.json()
        notes = {}
        for team_block in data.get("injuries", []):
            for inj in team_block.get("injuries", []):
                name = (inj.get("athlete") or {}).get("displayName", "")
                if not name:
                    continue
                key = _name_key(name)
                if not key or key in notes:
                    continue
                details = inj.get("details") or {}
                notes[key] = {
                    "body_part":   details.get("type", "") or "",
                    "detail":      details.get("detail", "") or "",
                    "return_date": details.get("returnDate", "") or "",
                }
        return notes
    except Exception:
        return {}


def attach_injury_notes(rows, notes):
    """Broadcast injury body-part/detail/return-date onto every player row whose name matches an
    ESPN injuries-API entry (by _name_key). Sparse by nature -- only injured players match, so
    healthy rows simply carry no keys. Read by analytics._injury_context to enrich the score-pill
    dropdown with which side of the IL a player is on and how bad the injury is."""
    if not notes:
        return
    for r in rows:
        note = notes.get(_name_key(r.get("PlayerName", "")))
        if not note:
            continue
        r["InjuryBodyPart"]   = note["body_part"]
        r["InjuryDetail"]     = note["detail"]
        r["InjuryReturnDate"] = note["return_date"]


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

    print("       Fetching today's MLB games (matchup overlap)...")
    todays_games = fetch_todays_games(league)
    print(f"       {len(todays_games)} games with rostered-player involvement")

    print("       Fetching pending trade proposals...")
    pending_trades = get_pending_trades(league, my_team)
    _n_in = sum(1 for t in pending_trades if t.get("incoming"))
    print(f"       {len(pending_trades)} pending trade(s) ({_n_in} incoming)")

    print("       Fetching MLB injury notes (body part / detail / return date)...")
    _inj_notes = fetch_injury_notes()
    attach_injury_notes(pitchers, _inj_notes)
    attach_injury_notes(hitters, _inj_notes)
    _n_inj = sum(1 for r in pitchers + hitters if r.get("InjuryBodyPart"))
    print(f"       {len(_inj_notes)} injured MLB players; detail attached to {_n_inj} rows")

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
        "todays_games":              todays_games,
        "pending_trades":            pending_trades,
    }

    # Validate the reader contract BEFORE persisting -- a broken snapshot fails LOUD here
    # (raising exits fetch_data nonzero, so send_digest/dashboard fall back to the previous
    # good snapshot) instead of silently garbling the digest. Optional import: never block a
    # fetch on the validator's absence.
    try:
        from snapshot_schema import assert_valid, SnapshotValidationError
    except ImportError:
        assert_valid = None
    if assert_valid is not None:
        try:
            assert_valid(snapshot, verbose=True)
        except SnapshotValidationError as e:
            print(f"  SNAPSHOT VALIDATION FAILED: {e}")
            print("  Refusing to overwrite the previous snapshot. Investigate upstream data.")
            raise

    with open(OUTPUT_FILE, "w") as f:
        json.dump(snapshot, f, default=str)

    print(f"\nSnapshot saved -> {OUTPUT_FILE}")
    print("=" * 60)


if __name__ == "__main__":
    main()
