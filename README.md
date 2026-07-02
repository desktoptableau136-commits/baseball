# Fantasy Baseball — Daily Digest

Automated email digest for ESPN fantasy league 277836 (Guerrero Warfare). Runs twice daily via GitHub Actions (06:00 & 15:00 UTC; GitHub's scheduler is unreliable so delivery lands roughly 4–6 AM / 1–3 PM EDT) — no laptop required.

---

## Table of Contents

1. [How It Works (Big Picture)](#how-it-works)
2. [One-time Setup](#one-time-setup)
3. [Running the Digest](#running-the-digest)
4. [Automation via GitHub Actions](#automation)
5. [What's in the Digest](#whats-in-the-digest)
6. [Troubleshooting](#troubleshooting)
7. [Making Changes](#making-changes)
8. [Data Sources & Pipeline](#data-sources)
9. [Composite Scores Explained](#composite-scores)
10. [Key Snapshot Fields](#key-snapshot-fields)
11. [File Reference](#file-reference)

---

## How It Works

```
fetch_data.py  →  data/snapshot.json  →  send_digest.py  →  email
```

1. **`fetch_data.py`** pulls data from ESPN, FantasyPros, MLB Stats API, and Baseball Savant / Baseball Reference (via `pybaseball`). Takes ~60–90 seconds. Saves everything to `data/snapshot.json`. *(FanGraphs is never called directly — it returns 403; `pybaseball` handles the headers.)*

2. **`send_digest.py`** reads the snapshot, builds a single HTML email, and sends it via Gmail SMTP. The email includes both an inline HTML body and an attached `digest_YYYY-MM-DD.html` file — the attachment bypasses Gmail's 102 KB inline clip limit so the full digest is always accessible by opening the attachment in a browser. Alternatively saves `digest_preview.html` for local browser preview (no email).

3. **GitHub Actions** runs this twice daily using credentials stored as repository secrets — no laptop needed.

---

## One-time Setup

### 1. Clone the repo and install dependencies

```bash
git clone https://github.com/desktoptableau136-commits/baseball.git
cd baseball
pip install -r requirements.txt
```

### 2. Configure ESPN credentials

Your ESPN credentials are already hardcoded in `fetch_data.py` as fallbacks. They only need updating if ESPN logs you out (the `espn_s2` cookie expires periodically).

**How to get fresh ESPN credentials:**
1. Log into ESPN Fantasy on Chrome
2. Press `F12` → Application tab → Cookies → `espn.com`
3. Copy the values for `swid` and `espn_s2`
4. Update the fallback values near the top of `fetch_data.py`:

```python
ESPN_CONFIG = {
    "league_id": 277836,
    "year":      2026,
    "swid":      "{YOUR-SWID-HERE}",
    "espn_s2":   "YOUR-ESPN-S2-HERE",
}
```

Also update the matching GitHub Actions secrets (see [Automation](#automation)).

### 3. Configure Gmail (local sending only)

For local runs that send real email, you need a Gmail App Password in a `.env` file. GitHub Actions uses the secret instead.

1. Google Account → Security → enable **2-Step Verification**
2. Search **"App Passwords"** → create one named "Baseball Digest"
3. Create a `.env` file in the project folder:

```
GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
```

---

## Running the Digest

Open a terminal in the `baseball/` folder and run:

```bash
# Full refresh + send email
python send_digest.py

# Full refresh + browser preview (NO email sent)
python send_digest.py --dry-run

# Browser preview using cached data (instant, no network calls)
python send_digest.py --dry-run --no-refresh

# Refresh data only (no email, no preview)
python fetch_data.py
```

After `--dry-run`, open `previews/digest_preview_{TeamName}.html` (e.g. `previews/digest_preview_Guerrero_Warfare.html`) in any browser to see the output. Add `--team "Team Name"` to preview any team's digest (requires a fresh snapshot).

**On Windows PowerShell**, if `git` isn't found, run this first to restore it:
```powershell
$env:PATH = [System.Environment]::GetEnvironmentVariable("PATH","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("PATH","User")
```

---

## Automation

GitHub Actions runs `.github/workflows/daily-digest.yml` twice daily at **06:00 and 15:00 UTC** (2 AM / 11 AM EDT). Cron is always UTC. GitHub's scheduler is unreliable — actual delays run 1–4 hours, so expect delivery roughly 4–6 AM / 1–3 PM EDT. It uses Python on Ubuntu.

### Trigger a manual run

1. Go to your repo on GitHub
2. Click the **Actions** tab
3. Click **Daily Fantasy Baseball Digest** in the left sidebar
4. Click **Run workflow** → **Run workflow**
5. Watch the run — green checkmark = email sent, red X = check the logs

### Required repository secrets

Go to **Settings → Secrets and variables → Actions** to view or update:

| Secret | What it is |
|--------|-----------|
| `GMAIL_APP_PASSWORD` | 16-character Google App Password |
| `ESPN_SWID` | Your ESPN `swid` cookie |
| `ESPN_S2` | Your ESPN `espn_s2` cookie (long string) |

### Email recipients

| Role | Address |
|------|---------|
| To   | desktoptableau136@gmail.com |
| CC   | katzsam@duck.com |

---

## What's in the Digest

The digest is organized into labeled **bands**, with a **Jump to** nav in the header (My Roster · Free Agents · Season · Glossary) that anchors to each band.

**Header** — date · team name + logo · KPI row · Jump-to nav pills
KPI row: **Record** · **Current Matchup** (W-L-T + win%) · **Roster** (whole-team hot/cold count — hitters *and* pitchers) · **Starts This Week**

**Weekly matchup overview** (top of the email)
1. **Monday Recap** — *(Mondays only)* last week's final result
2. **Week at a Glance**
3. **Category Pulse**
4. **Opponent This Week** — scouting block for this week's opponent
5. **Current Matchup** — this week's category rankings grid
6. **Week N Matchup** — score banner + category-by-category table

**⚑ MY ROSTER**
7. **Roster Alerts** — *(only if you have injured players)*
8. **Positional Breakdown**
9. **My Upcoming Starts**
10. **My Relief Pitchers**
11. **Pitcher Hot/Cold**
12. **Roster Hot/Cold**

**FREE AGENTS**
13. **FA Pickup — Starting Pitchers**
14. **FA Pickup — Relief Pitchers**
15. **FA Pickup — Hitters**

**SEASON**
16. **My Season Category Rankings**
17. **League Luck Standings**

**REFERENCE**
18. **Glossary & Methodology** — collapsible in-digest reference for every score, metric, and data source

On **Sundays** the digest shifts to a next-week lookahead (subtitle, subject, KPI, and Week-at-a-Glance all preview the coming week).

### KPI Row
Two-row panel at the top of every digest. Your team logo appears next to the team name in the header.

**Top row:** Category record (W-L-T) with Win% sub-line · Category matchup record (W-L-T) with Win% sub-line · Roster hot/cold count · Upcoming starts

**Bottom row:**
- **Roto Trend** — SVG line chart of your weekly roto score across all completed weeks. Dots are color-coded: green filled = your personal peak week, ★ (yellow star) = you ranked #1 in roto points among all 12 teams that week, grey = all other weeks. A legend below the chart reads: 🟢 Peak Wk: N  |  ★ #1 roto wk. Note: uses ★ (U+2605) instead of an emoji so the marker size is controlled by the SVG font-size attribute.
- **Standing** — Your current league standing (#N) with your average roto category W-L-T per week underneath (season totals ÷ weeks played).
- **Roto Rank** — Season-to-date cumulative roto rank (#N) with average weekly rank and average weekly roto points underneath.
- **Luck** — Roto rank minus record rank. Positive = your W-L is better than your underlying stats deserve; negative = underperforming your true quality.

### Week at a Glance
Three-bullet summary placed directly above the category rankings grid:

1. **Week record** — current W-L-T vs. this week's opponent through the current day, with the categories you're winning (green) and trailing (red) called out.
2. **Rotation coverage** — confirmed start count and days covered; flags thin days (< 2 my starts) by day-of-week so you know where to add from FA.
3. **Top FA pickup** — best available FA starter by QS%, with their next start day and QS%. If the highest-score and highest-QS pitchers differ, both are mentioned.

### Current Matchup (category rankings)
Your roto rank (out of 12 teams) in each of the 12 scoring categories for the **current matchup week only**. Green = top 3, red = bottom half.

Scoring categories: **R · HR · RBI · SB · OPS · B/SO** (batter strikeouts, hitting) + **K · QS · W · ERA · WHIP · SV+H** (pitching)

### Opponent This Week
Scouting block for the current opponent, directly below Category Pulse. Shows their start count (and any two-start pitchers), top-3 hottest bats by recent OPS, season roto strengths/weaknesses (top-3 / bottom-3 categories), and wire activity (how active they've been on the FA wire). Only renders when the opponent has starters or hot hitters.

### Category Pulse
12 visual cards — 6 hitting, 6 pitching. Each card shows:
- **Current value** (big, colored green/red/white) vs opponent value
- **Fill bar** showing relative share of the combined total
- **⚡** = within striking distance (close enough to flip)
- **proj X.X vs Y.Y** = projected end-of-week (K/QS/W use your actual remaining starts × per-start rate; other cats use each team's weekly average)
- **▲ / ▼ / ◆** flip badge = the projection flips the current result (▲ to a win, ▼ to a loss, ◆ to a tie)

### Week N Matchup
Score banner (team logos + overall W-L-T, with a projected final record) followed by a category-by-category table. Each row shows your value and the opponent's, colored by who's currently winning. Below each value is the **projected** end-of-week value, **colored by its projected outcome** (green = you're projected to win that category, red = lose) with a **▲/▼/◆ flip arrow** on your side when the projection differs from the current standing — so a category you're currently losing but projected to win shows a red current value and a green projection with a ▲.

### My Relief Pitchers
Your rostered relievers, showing season SV+H / K / W (from ESPN) plus ERA/WHIP from the best available dataset, with a role-aware **Score** badge. RP scoring is **skill-weighted (punt-saves)** — see [Composite Scores](#composite-scores).

### Pitcher Hot/Cold
Your rostered pitchers sorted hottest → coldest. Compares **last-15-day ERA** to season ERA (15 days, not 7 — starters pitch too infrequently for a 7-day window to be meaningful). Includes a role-aware **Score** badge.

### Roster Hot/Cold
Your rostered **hitters** sorted hottest → coldest. Compares last-7-day OPS to season OPS. Includes an **HR%** column (modeled per-game home-run probability, hover for drivers) and a **Score** badge.
- 🔥 = OPS up +0.050 or more
- ↑ = OPS up +0.015 to +0.050
- ↓ = OPS down -0.015 to -0.050
- ❄ = OPS down -0.050 or more

### Positional Breakdown
For each position (C, 1B, 2B, 3B, SS, OF, SP, RP): your weakest rostered player vs. the best available FA at that position. **↑** = the FA is a meaningful upgrade.

### Roster Alerts
Any injured players on your roster. Only shown if there are active alerts. Color: yellow = DTD, red = IL/OUT.

### FA Pickup — Starting Pitchers
Free agent starters with a confirmed upcoming start, grouped by date with day headers. Sorted by composite SP score within each day. Starts past Sunday get a `NEXT WK` badge; a pitcher with ≥ 2 starts in the matchup week gets a green `2-START` chip.

**Columns:** Pitcher · **Proj. Line** · Matchup · Opp OPS · QS% · ERA · **L15 ERA** (hot/cold colored) · K% · Score

**Proj. Line** = projected `IP · ER · K` for one start. ER is adjusted for opponent lineup strength (their OPS) and a home/away park factor; K is adjusted for the opponent lineup's strikeout rate. IP is the pitcher's per-start average in baseball notation (e.g. 5.1 = 5⅓).

**Day headers** show a ⚑ badge with your start count for that day: red = 0 my starts, yellow = 1, blue = 2+.

**Pickup badges** appear on thin days (< 2 my starts) only. Both can fire simultaneously:
- 🟢 **QS** badge (green left border) — QS% ≥ 51%; likely quality start
- 🟡 **5K+** badge (yellow left border) — K/IP ≥ 0.90 or K% ≥ 24%, **and** IP/G ≥ 4.5 (deep enough to rack up strikeouts)
- When both fire: left border is green (top half) / yellow (bottom half)

**K% highlight** — top 3 K% values across the table are highlighted yellow.

**FA exclusion:** players who appear in today's ESPN transaction log as "FA ADDED" (net of any same-day drops) are excluded even if the ESPN roster API hasn't reflected the pickup yet. DL-status players are also excluded.

### FA Pickup — Relief Pitchers
Top available relievers (must have ≥ 1 SV+H on the season), ranked by RP score (SV+H · K · W · ERA · WHIP — skill-weighted, see [Composite Scores](#composite-scores)). A **Cats** column lists up to 3 roto categories the reliever is strong in, with your currently-contested categories highlighted. Includes a **Save-Role Watch** callout flagging emerging FA closers and fading rostered closers.

### FA Pickup — Hitters
Top available hitters sorted by composite score. Columns: R · HR · RBI · SB · OPS · **Cats** (up to 3 strong roto categories, your contested ones highlighted) · **HR%** (modeled per-game HR probability) · Score. Includes a hot/cold recent-form indicator.

### My Upcoming Starts
Your pitchers with confirmed or projected starts, grouped by date.

**Columns:** Pitcher · **Proj. Line** · Matchup · Opp OPS · QS% · ERA · **L15 ERA** (hot/cold colored) · K% · Score

Badges next to the name: `2-START` (green), `QS` (green, QS% ≥ 51%), `5K+` (yellow). `(proj.)` = rotation estimate, not yet confirmed by MLB. **K% highlight** — top 3 K% values across the table are highlighted yellow.

### My Season Category Rankings
Season-to-date roto rank across all 12 categories. Same color coding as the weekly version at the top, but for the full season.

### League Luck Standings
All 12 teams sorted by record. Shows W-L-T · Win% · Roto rank · Cumulative roto points · Luck delta. **Luck** = roto rank minus record rank. Positive luck means your W-L-T is better than your underlying stats deserve; negative means you're underperforming your true quality.

### Glossary & Methodology
A collapsible in-digest reference at the very bottom (also linked from the header nav). Five expandable groups — **Scores**, **Pitching metrics**, **Hitting metrics**, **Projections & matchup**, **Data sources** — explaining how every score and metric is computed and where the data comes from. Kept in sync with the code as part of the save sequence.

---

## Troubleshooting

### Email stopped arriving
1. Check the GitHub Actions tab — is the workflow running? Is it green or red?
2. If red, click the failed run and read the error. Common causes:
   - **ESPN credentials expired** — get fresh `swid`/`espn_s2` from the browser and update the GitHub secrets
   - **FantasyPros scraping failed** — their HTML structure changed; check the `fetch_fantasypros()` function
   - **Gmail App Password invalid** — regenerate it and update the `GMAIL_APP_PASSWORD` secret

### Sections show "—" or are missing
The underlying data source probably failed silently. Run locally with `python send_digest.py --dry-run` and look for `FAILED` lines in the output. Each data source is wrapped in a try/except so one failure won't crash the whole digest.

### Player name not matching (wrong team, missing stats)
ESPN and FantasyPros use slightly different player names. Add a patch near the top of `fetch_data.py`:

```python
HITTER_NAME_PATCHES = {
    "ESPN Name":        "FantasyPros Name",
    "Cedric Mullins":   "Cedric Mullins II",   # existing example
}
```

### Category Pulse shows no projections
Projections need at least one completed past week in the roto data. They won't appear in Week 1. Also requires `weekly_avgs` to find both your team and your opponent's team name — if the team name lookup fails silently, check for double-spaces in team names (the normalization in `compute_weekly_avgs` handles this).

### Roster Hot/Cold is empty
`recent_hitting` is populated by `pybaseball.batting_stats_range`. If this fetch fails (network issue, FanGraphs down), the section is silently skipped. Run `python fetch_data.py` locally and look for `Recent hitter stats FAILED`.

### Windows: `git` not found in PowerShell
Git PATH drops between PowerShell sessions. Fix:
```powershell
$env:PATH = [System.Environment]::GetEnvironmentVariable("PATH","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("PATH","User")
```

### ESPN session expired
Signs: standings/roster data is stale or empty. Get fresh cookies from Chrome (F12 → Application → Cookies → espn.com) and update:
- The hardcoded fallbacks in `fetch_data.py`
- The `ESPN_SWID` and `ESPN_S2` GitHub Actions secrets

---

## Making Changes

### Changing the year
Update `year` in `ESPN_CONFIG` at the top of `fetch_data.py`. Also update `YEAR` in `send_digest.py`.

### Adding/removing a digest section
Each section is a function in `send_digest.py` that returns an HTML string. The final assembly is at the bottom of `build_email()` in the `body_parts` list — add, remove, or reorder entries there.

### Changing which columns appear in a table
Find the relevant section in `build_email()` (search for the section header, e.g. `"FA Pickup — Hitters"`). Each row is built with f-strings; add or remove `<td>` cells and matching `<th>` headers.

### Updating fantasy team emoji avatars
If a team's ESPN logo URL is broken or auth-gated, it falls back to an emoji avatar. Update `_FANTASY_EMOJI` in `send_digest.py`:

```python
_FANTASY_EMOJI = {
    "Team Name":     ("🔥", "#ea580c"),   # (emoji, background color)
    ...
}
```

### Adjusting hot/cold thresholds
In `send_digest.py`, `hot_cold_cell()` uses these defaults:
- Hitters (OPS): 🔥 at +0.050, ↑ at +0.020
- Pitchers (ERA, lower=better): 🔥 at -0.75, ↑ at -0.25

Change `hot_thresh` and `warm_thresh` in the `hot_cold_cell()` call for the relevant table.

### Committing and deploying changes
```bash
git add -A
git commit -m "describe what you changed"
git push
```
GitHub Actions automatically uses whatever is on `main`. The next scheduled run (or manual trigger) will use the new code.

---

## Data Sources

| Data | Source | Auth needed? |
|------|--------|-------------|
| Pitcher / hitter stats (7d / 15d / 30d / season) | FantasyPros HTML scrape | No |
| Recent hitter stats (last 7d) | Baseball Reference via `pybaseball.batting_stats_range` | No |
| Recent pitcher stats (last 15d) | Baseball Reference via `pybaseball.pitching_stats_range` | No |
| Roster, FA, transactions, roto scores, team logos, **season counting stats** (SV/K/W/IP/GS/GP) | ESPN Fantasy API (`espn_api` library) | Yes — `swid` + `espn_s2` cookies |
| Probable starters (+6-day rotation projection) | MLB Stats API | No |
| Opponent team **OPS and K rate** | MLB Stats API | No |
| Barrel%/hard-hit% allowed, **xERA, xwOBA-against, whiff percentile** (pitchers) | Baseball Savant via `pybaseball` | No |
| xwOBA, xBA, xSLG, Barrel%, hard-hit%, sprint speed (hitters) | Baseball Savant via `pybaseball` | No |

> **Note:** FanGraphs blocks direct HTTP requests with 403 errors. Always use `pybaseball` — it handles the necessary headers automatically.

### How probable starters are fetched (3-phase logic)

1. **Range schedule call** — one request gets all game IDs for the next 7 days
2. **Batch hydrate** — one request gets confirmed probable pitchers for all those games (`PSP_Projected = False`)
3. **Rotation projection** — for unannounced slots, finds each pitcher's last start and adds 6 days ±1 (`PSP_Projected = True`)

Projected starts show `(proj.)` in the digest. If the batch call returns nothing, falls back to per-game live-feed calls.

---

## Composite Scores

Each player gets a **0–100 score**, calibrated so the median qualified player ≈ 50 and a top-10% player ≈ 80 (benchmarks are derived from the live data each run). A player shows the **same** score in every section. Shown as a colored badge:

| Badge color | Score range |
|-------------|------------|
| Green | ≥ 72 — elite |
| Blue | ≥ 52 — solid |
| Yellow | ≥ 32 — fringe |
| Red | < 32 — avoid |

Scores are **not** dampened for injuries (injury status is shown separately as a tag; DL players are excluded from FA lists). Three canonical role scores:

**Starting-pitcher score (`pitcher_score` / `_score_p`):** K% (blended 60/40 with Baseball Savant whiff percentile) + run prevention (ERA blended 55/45 with Savant xERA) + WHIP + contact-quality allowed (barrel%/xwOBA-against) + a start-volume role bonus. Small samples damped toward the mean. Displayed blended 60% season / 40% recent form.

**Relief-pitcher score (`rp_score`):** Skill-weighted **punt-saves** build — K, ERA (blended with xERA) and WHIP carry most of the weight; **SV+H is deliberately de-emphasized (~15%)** since it's the most volatile category and one we're willing to sacrifice. A dominant setup man can outrank a mediocre closer. Counting stats prefer ESPN season totals.

**Hitter score (`hitter_score`):** wRC+ (or OPS) + HR volume + ISO + RBI + speed (sprint speed preferred, falls back to SB) + xwOBA/AVG + HR-probability model. Scaled by an **opportunity multiplier** (at-bats vs a full-time benchmark) so a part-time bat can't score like a regular. Displayed blended 60% season / 40% recent form.

**QS Probability:** Formula-based estimate (no MLB API support). Inputs: IP/G, ERA, WHIP, Brl%, K%, opponent OPS. Baseline = 38% (league average). Key driver is IP/G — uses total games (not just starts) so relief appearances bleed down the innings-depth signal for mixed-role pitchers. Calibration: ace (~75%), league avg (~38%), short reliever making a spot start (~15%). Shown as a color-coded percentage in FA SP and My Upcoming Starts tables: green ≥ 60%, white ≥ 40%, muted < 40%.

> When the raw component mix of a score changes, rerun `python recalibrate_scores.py` and paste the new p50→50 / p90→80 constants back into the score function.

---

## Key Snapshot Fields

`data/snapshot.json` is rebuilt on every run. It's the only file shared between `fetch_data.py` and `send_digest.py`.

**pitchers** (list of dicts, one per player per time range):
`PlayerName, FantasyTeam, Position, Dataset` (7/15/30/2026), `IP, G, GS, K, ERA, WHIP, SV, HLD, SVHD, K/IP, Kpct_P, IP_per_G` (IP÷G — honest for mixed starters/relievers), `PSP_Date` (1999-01-01 = no start), `PSP_HomeVAway, PSP_Projected`, `PSP_Dates` + `PSP_HomeVAways` (lists of ALL upcoming starts, for two-start detection), `Team_OPS_Value, Team_K_Value` (opponent OPS & K-per-PA), advanced: `xERA, xwOBA_against, WhiffPctile, BarrelPctAllowed, HardHitPctAllowed, AvgEVAllowed`, ESPN season counts: `ESPN_SV, ESPN_K, ESPN_W, ESPN_IP, ESPN_GS, ESPN_GP, ESPN_SVHD`

**hitters** (list of dicts, one per player per time range):
`PlayerName, FantasyTeam, Position, Dataset, HR, RBI, R, SB, AVG, OPS, wRCplus, xwOBA, xBA, xSLG, SprintSpeed, ISO, Barrel_Pct, HardHit_Pct, HR_Probability`

**roto** (list of dicts, one per team per week):
`Team, Week, R, HR, RBI, SB, OPS, B_SO, K, QS, W, ERA, WHIP, SVHD, Roto_Score, {CAT}_Points`

**standings** (list of dicts):
`team_name, wins, losses, ties, standing, logo_url`

**current_matchup** (dict):
`week, my_team, opp_team, wins, losses, ties, categories[]`
Each category: `cat, my_val, opp_val, result` (W/L/T), `lower_better`

**recent_hitting** (list of dicts — last 7 days, all MLB hitters):
`PlayerName, G, PA, AB, R, HR, RBI, SB, OBP, SLG, OPS`

**recent_pitching** (list of dicts — last 15 days, all MLB pitchers):
`PlayerName, G, GS, IP, ERA, WHIP, BB`

**weekly_results** (dict — `{"1": {"Team Name": "W"/"L"/"T", ...}, ...}`):
Per-week head-to-head matchup results for every team. Keys are week numbers as strings. Note: the sparkline dot encoding uses roto-derived rank results computed in `send_digest.py` from the `roto` data — not this H2H field directly.

---

## Player Name Patches

ESPN and FantasyPros occasionally use different names for the same player. When a player shows up as a free agent but you know they're rostered (or vice versa), add a patch near the top of `fetch_data.py`:

```python
PITCHER_NAME_PATCHES = {
    "ESPN Name":   "FantasyPros Name",
    "Nestor Cortes": "Nestor Cortes Jr.",    # example
}
HITTER_NAME_PATCHES = {
    "Cedric Mullins":  "Cedric Mullins II",  # example
    "Victor Scott II": "Victor Scott",       # example
}
```

---

## File Reference

```
baseball/
├── fetch_data.py                        # Data pipeline — runs first (~60s)
├── send_digest.py                       # Email builder + sender
├── requirements.txt                     # pip install -r requirements.txt
├── .env                                 # GMAIL_APP_PASSWORD — do not commit
├── .env.example                         # Safe template to share
├── .github/
│   └── workflows/
│       └── daily-digest.yml            # GitHub Actions schedule (7 AM EDT daily)
├── data/
│   └── snapshot.json                    # ~1.7 MB — rebuilt every run, gitignored
├── logs/
│   └── digest.log                       # Local send history, gitignored
└── _archive/                            # Legacy files (gitignored)
    ├── dashboard.html                   # Old single-page dashboard app
    ├── digest_preview.html              # Last local dry-run preview
    └── tableau_screenshots/             # Early Tableau exploration screenshots
```
