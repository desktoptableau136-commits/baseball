# Fantasy Baseball — Daily Digest

Automated morning email digest for ESPN fantasy league 277836. No cloud auth. No manual logins. Runs daily at 7 AM via Windows Task Scheduler.

---

## One-time Setup

### 1. Install dependencies

```bash
pip install pandas requests espn_api pybaseball python-dotenv
```

### 2. Configure ESPN credentials

Open `fetch_data.py` and update the top section:

```python
ESPN_CONFIG = {
    "league_id": 277836,
    "year":      2026,
    "swid":      "{YOUR-SWID}",
    "espn_s2":   "AEB1C0...",
}
MY_TEAM_NAME = "Guerrero Warfare"
```

**Finding ESPN credentials:**
Log into ESPN Fantasy on Chrome → F12 → Application → Cookies → espn.com.
Copy `swid` and `espn_s2`. These rarely change; paste once and forget.

### 3. Configure Gmail

1. Google Account → Security → enable **2-Step Verification**
2. Search **"App Passwords"** → create one, name it "Baseball Digest"
3. Copy the 16-character password

```bash
copy .env.example .env
```

Paste your app password into `.env`:

```
GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
```

---

## Running the Digest

```bash
python send_digest.py                        # refresh data + build + send email
python send_digest.py --dry-run              # save digest_preview.html, no email
python send_digest.py --no-refresh           # skip data fetch, use existing snapshot
python send_digest.py --dry-run --no-refresh # instant preview from cached snapshot
```

Open `digest_preview.html` in a browser to inspect the output locally.

### Automation

Windows Task Scheduler task **GuerreroDailyDigest** runs `run_digest.bat` every day at 7:00 AM.
`WakeToRun` and `StartWhenAvailable` are enabled so it fires even if the laptop was asleep.

```bash
schtasks /query /tn "GuerreroDailyDigest"    # verify it's scheduled
```

Logs are written to `logs\digest.log` after each run.

### Recipients

| Role | Address |
|------|---------|
| To   | desktoptableau136@gmail.com |
| CC   | katzsam@duck.com |

---

## What's in the Digest

Sections appear in this order:

| Section | What you see |
|---------|-------------|
| **KPI Row** | Record, standing, roto rank, upcoming starts count |
| **This Week's Category Rankings** | Roto rank across all 12 scoring categories for the current matchup week |
| **Current Week Matchup** | Category-by-category breakdown vs. this week's opponent; score banner with team logos |
| **Positional Breakdown** | Your weakest player at each position vs. best FA upgrade available (↑ = upgrade) |
| **Roster Alerts** | Injured players on your roster (omitted when none) |
| **FA Pickup: Starting Pitchers** | Free agents with upcoming starts, sorted by spFAScore |
| **FA Pickup: Hitters** | Top available hitters by composite score |
| **My Upcoming Starts** | Your pitchers with starts in the next 7 days |
| **My Category Rankings** | Season-to-date roto rank across all 12 categories |
| **League Luck Standings** | Every team's W-L vs. roto rank; positive luck = overperforming |

### Team Logos

All player tables show the pitcher/hitter's MLB team logo (ESPN CDN).
The matchup banner and luck standings show each fantasy team's ESPN logo.
Teams with auth-gated or dead logo URLs automatically get a styled emoji avatar instead
(defined in `_FANTASY_EMOJI` near the top of the HTML helpers section in `send_digest.py`).

---

## Composite Scores

**pitcherScore** (0–100): K% (Whiff% → K% → K/IP fallback) + ERA quality (xFIP preferred, falls back to ERA) + WHIP + role bonus + xFIP elite bonus. IL/OUT = −22, DTD = −10.

**hitterScore** (0–100): wRC+ or OPS + HR + ISO + RBI + sprint speed/SB + xwOBA/AVG + HR probability. IL/OUT = −22, DTD = −10.

**spFAScore**: pitcherScore + 15 if the pitcher has a confirmed upcoming start. Requires GS ≥ 1 or SP eligibility.

Score badges: green ≥ 72 · blue ≥ 52 · yellow ≥ 32 · red < 32

---

## Data Sources

| Data | Source |
|------|--------|
| Pitcher / hitter stats (7d / 15d / 30d / season) | FantasyPros — public, no auth |
| Roster / FA / transactions / roto / team logos | ESPN Fantasy API (`espn_api`) |
| Probable starters | MLB Stats API — public, no auth |
| Opponent team OPS | MLB Stats API — public, no auth |
| Statcast barrel %, hard-hit % (pitchers) | Baseball Savant via `pybaseball` |
| Expected stats (xBA, xSLG, xwOBA), sprint speed | Baseball Savant via `pybaseball` |

> FanGraphs returns 403 errors — do not use directly. `pybaseball` handles the necessary headers.

### Probable Starters Logic

`fetch_data.py` uses a three-phase strategy:
1. Range schedule call → all gamePks for the next 7 days
2. Batch `hydrate=probablePitcher` call → confirmed starters (`PSP_Projected=False`)
3. Rotation projection: finds each pitcher's last start + 6-day interval ±1 to fill unannounced slots (`PSP_Projected=True`)

Projected starts show `(proj.)` in the digest. Falls back to per-game live feed if the batch returns nothing.

---

## Key Snapshot Fields

`data/snapshot.json` (~1.2 MB) is the intermediate data layer refreshed each run.

**Pitchers:** PlayerName, FantasyTeam, Position, Dataset, IP, K, ERA, WHIP, GS, SV, HLD, SVHD, K/IP, Kpct_P, PSP_Date (`1999-01-01` = no start), PSP_HomeVAway, PSP_Projected, Team_OPS_Value, BarrelPctAllowed, HardHitPctAllowed

**Hitters:** PlayerName, FantasyTeam, Position, Dataset, HR, RBI, R, SB, AVG, OPS, wRCplus, xwOBA, xBA, xSLG, SprintSpeed, ISO, Barrel_Pct, HardHit_Pct, HR_Probability

**Roto:** Team, Week, Roto_Score, per-category points (R_Points, HR_Points, …)

**Standings:** team_name, wins, losses, standing, logo_url

**current_matchup:** week, my_team, opp_team, wins, losses, ties, categories[]

---

## Player Name Patches

ESPN and FantasyPros sometimes use different names. Add mismatches near the top of `fetch_data.py`:

```python
PITCHER_NAME_PATCHES = {
    "Nestor Cortes": "Nestor Cortes Jr.",
}
HITTER_NAME_PATCHES = {
    "Cedric Mullins":  "Cedric Mullins II",
    "Victor Scott II": "Victor Scott",
}
```

---

## File Reference

```
baseball/
├── fetch_data.py       # Data pipeline → data/snapshot.json  (~60s to run)
├── send_digest.py      # Email builder + sender
├── dashboard.html      # Legacy local dashboard (secondary, not actively maintained)
├── run_digest.bat      # Task Scheduler launcher
├── .env                # GMAIL_APP_PASSWORD (do not share)
├── .env.example        # Template for .env
├── data/
│   └── snapshot.json   # ~1.2 MB data cache, refreshed daily
└── logs/
    └── digest.log      # Send history
```
