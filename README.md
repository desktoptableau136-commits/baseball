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
fetch_data.py  →  data/snapshot.json  →  send_digest.py   →  daily email
                                      →  weekly_recap.py  →  Monday recap email
```

1. **`fetch_data.py`** pulls data from ESPN, FantasyPros, MLB Stats API, and Baseball Savant / Baseball Reference (via `pybaseball`). Takes ~60–90 seconds. Saves everything to `data/snapshot.json`. *(FanGraphs is never called directly — it returns 403; `pybaseball` handles the headers.)*

2. **`send_digest.py`** reads the snapshot, builds a single HTML email, and sends it via Gmail SMTP. The email includes both an inline HTML body and an attached `digest_YYYY-MM-DD.html` file — the attachment bypasses Gmail's 102 KB inline clip limit so the full digest is always accessible by opening the attachment in a browser. Alternatively saves `digest_preview.html` for local browser preview (no email).

3. **`weekly_recap.py`** reads the same snapshot every Monday and emails a full-league recap: **Matchup N Highlights** (commissioner-style prose + stat sidebar — roto winner, hitter/pitcher/FA of the matchup with MLB team logos and named historical benchmarks), your matchup result, **Lineup Efficiency** (last matchup's start/sit opportunity cost — bench leakage + active-slot pitcher blowups), all 6 scoreboard matchups, Matchup Roto Rankings (all 12 categories, 5-tier heat-map coloring), Top Performers (hitters and pitchers side-by-side), Standings & Luck, Season Trajectory, and Season Roto Rankings (the same 12-category grid aggregated over every matchup — ranked by cumulative roto points, each category showing its true season-to-date value from ESPN). Saves `previews/recap_week_N.html` on dry runs. GitHub Actions: `.github/workflows/weekly-recap.yml` (Monday 15:30 UTC).

4. **GitHub Actions** runs both scripts automatically using credentials stored as repository secrets — no laptop needed.

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

### Running the Monday Recap

```bash
# Monday recap — refresh + send email
python weekly_recap.py

# Recap preview using cached data (instant, no network calls)
python weekly_recap.py --dry-run --no-refresh
```

After `--dry-run`, open `previews/recap_week_N.html` in any browser.

### Single-Viewport Dashboard

A glance-able "command dashboard" that condenses the entire digest onto **one 1440×900 laptop screen with zero scrolling** — even coverage of every topic (matchup, category pulse, opponent, pitching, hitting hot/cold, weakest spots, moves, free agents, season). It reuses the digest's exact scoring so every number matches.

```bash
# Write previews/dashboard_{team}.html from the existing snapshot (fast, no email)
python dashboard.py

# Refresh data first, then write
python dashboard.py --refresh

# Another team's dashboard (needs all_matchups in the snapshot)
python dashboard.py --team "Houck Tuah"

# Also email it to yourself as an attachment (reuses the digest's Gmail setup)
python dashboard.py --email
```

Open the file maximized in a browser — it's tuned for a 1440×900 viewport and should show no scrollbars.

It's also **responsive**: on a tablet (≤1100px) the tiles reflow into two height-balanced columns — Category Pulse → Recommended Moves → Free-Agent Radar → Season down the left, then My Pitching → Hitting Hot/Cold → Weakest Spots → Opponent down the right — and on a phone (≤700px) into a single column, un-pinning the fixed pane so the page scrolls normally with larger, readable text. The desktop no-scroll layout is unchanged above 1100px.

**Viewing on a phone/tablet:** use `--email` (or attach `previews/dashboard_{team}.html` to an email yourself) and open the **attachment** in your device browser — email apps strip the `<style>` block that holds the layout, so the attached file works but a pasted-in body won't.

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
KPI row: **Record** · **Current Matchup** (W-L-T + win%) · **Roster** (whole-team hot/cold count — hitters *and* pitchers) · **Starts This Matchup**

**Matchup overview** (top of the email)
1. **Monday Recap** — *(Mondays only)* last matchup's final result (per-team via `all_prev_matchups`, so `--team` shows that team's prior matchup)
2. **Matchup at a Glance**
3. **Category Pulse**
4. **Opponent This Matchup** — scouting block for this matchup's opponent
5. **Current Matchup** — this matchup's category rankings grid *(hidden Monday before stats accumulate)*
5b. **Matchup N Roto Rankings** — live all-12-team roto table for the current matchup *(hidden Monday before stats accumulate)*
6. **Matchup N** — score banner + category-by-category table

**⚑ MY ROSTER**
7. **Roster Alerts** — *(only if you have injured players)*
7b. **Lineup Watch** — *(current-week bench leakage / active-slot pitcher blowups; silent on a clean week)*
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
18. **Season Trajectory** — W/L/T by matchup for every team, current streak in the final column
19. **Season Roto Rankings** — all 12 teams ranked by cumulative roto score; each category shows its true season-to-date value from ESPN (rate cats innings/AB-weighted, not a weekly average)

**REFERENCE**
19. **Glossary & Methodology** — collapsible in-digest reference for every score, metric, and data source

On **Sundays** the digest shifts to a next-week lookahead (subtitle, subject, KPI, and Week-at-a-Glance all preview the coming week).

### KPI Row
Two-row panel at the top of every digest. Your team logo appears next to the team name in the header.

**Top row:** Category record (W-L-T) with Win% sub-line · Category matchup record (W-L-T) with Win% sub-line · Roster hot/cold count · Upcoming starts

**Bottom row:**
- **Roto Trend** — SVG line chart of your weekly roto score across all completed weeks. Dots are color-coded: green filled = your personal peak week, ★ (yellow star) = you ranked #1 in roto points among all 12 teams that week, grey = all other weeks. A legend below the chart reads: 🟢 Peak Wk: N  |  ★ #1 roto wk. Note: uses ★ (U+2605) instead of an emoji so the marker size is controlled by the SVG font-size attribute.
- **Standing** — Your current league standing (#N) with your average roto category W-L-T per week underneath (season totals ÷ weeks played).
- **Roto Rank** — Season-to-date cumulative roto rank (#N) with average weekly rank and average weekly roto points underneath.
- **Luck** — Roto rank minus record rank. Positive = your W-L is better than your underlying stats deserve; negative = underperforming your true quality.

### Matchup at a Glance
Four-bullet summary placed directly above the category rankings grid:

1. **Matchup record** — current W-L-T vs. this matchup's opponent through the current day, with the categories you're winning (green) and trailing (red) called out.
2. **Rotation coverage** — confirmed start count and days covered; flags thin days (< 2 my starts) by day-of-week so you know where to add from FA.
3. **Top FA pickup** — best available FA starter by QS%, with their next start day and QS%. If the highest-score and highest-QS pitchers differ, both are mentioned.
4. **Pickups (roster-context aware)** — up to two targeted add/drop bullets:
   - **Bat** — upgrades your **weakest hitter position** where a real free-agent upgrade exists (from the Positional Breakdown league ranks). It deliberately **won't** send you to a position you're already deep at or leaving production on the bench (that's surplus / trade capital, not a hole) — so it recommends the catcher upgrade, not another outfielder. Falls back to a losing-category bat only when there's no clear positional hole.
   - **Pitching fix** — appears when you're in ratio trouble: a starter imploded in your active lineup this week (from Lineup Watch) **or** you're losing ERA/WHIP by a non-toss-up margin. It recommends a **high-floor stabilizer** (low ERA/WHIP), not a volatile streamer that would make your ratios worse.

   Drops prefer a **surplus** player (a deep position or a bench-leaker), tagged `[surplus]`, and the two bullets never suggest dropping the same player. If you have an open roster spot the add shows as a free pickup instead. Drops never target an injured player in one of your **2 IL roster slots** (cutting them frees nothing), and always leave ≥ 1 healthy player at every position.

### Current Matchup (category rankings)
Your roto rank (out of 12 teams) in each of the 12 scoring categories for the **current matchup only**. Green = top 3, red = bottom half. The subtitle's total roto points is your stored `Roto_Score` — the same figure shown in the Matchup N Roto Rankings table, so the two panels agree (tied categories split points, so this can be a half-point below the sum of the ordinal rank chips shown in the grid).

Scoring categories: **R · HR · RBI · SB · OPS · B/SO** (batter strikeouts, hitting) + **K · QS · W · ERA · WHIP · SV+H** (pitching)

### Opponent This Week
Scouting block for the current opponent, directly below Category Pulse. Shows their start count (and any two-start pitchers), top-3 hottest bats by recent OPS, season roto strengths/weaknesses (top-3 / bottom-3 categories), and wire activity (how active they've been on the FA wire). Only renders when the opponent has starters or hot hitters.

### Category Pulse
A summary line above the cards shows your current record and projected end-of-week record, each as a full **W · L · T** (the tie count is always shown, even at `0T`), with a `⚡N close` count between them: `10W · 2L · 0T · ⚡3 close → proj 11W · 1L · 0T`.

12 visual cards — 6 hitting, 6 pitching. Each card shows:
- **Current value** (big, colored green/red/white) vs opponent value
- **Fill bar** showing relative share of the combined total
- **NN%** (corner) = your odds of winning that category, from a normal model of the final margin, colored to match the projected outcome (green = projected win, red = loss, white = tie) — uncertainty is each team's week-to-week spread in the stat and shrinks for counting cats as the week ends
- **⚡** (corner) = toss-up — win odds near even (45–55%) **or** a projected tie; on a toss-up the ⚡ **replaces** the % (the exact number doesn't matter at a coin-flip)
- **proj X.X vs Y.Y** = projected end-of-week (K/QS/W use your actual remaining starts × per-start rate; other cats use each team's weekly average)
- **▲ / ▼ / ◆** (corner) = the **projected outcome** — ▲ green = projected win, ▼ red = loss, ◆ white = tie. Shown on every card; when it disagrees with the card's current color (WINNING/LOSING/TIED), that's a projected flip

### Matchup N
Score banner (team logos + overall W-L-T, with a projected final record) followed by a category-by-category table. Each row shows your value and the opponent's, colored by who's currently winning. Below each value is the **projected** end-of-matchup value, **colored by its projected outcome** (green = you're projected to win that category, red = lose) with a **▲/▼/◆ flip arrow** on your side when the projection differs from the current standing — so a category you're currently losing but projected to win shows a red current value and a green projection with a ▲.

### My Relief Pitchers
Your rostered relievers, showing season SV+H / K / W (from ESPN) plus ERA/WHIP from the best available dataset, with a role-aware **Score** badge. RP scoring is **skill-weighted (punt-saves)** — see [Composite Scores](#composite-scores).

### Pitcher Hot/Cold
Your rostered pitchers sorted hottest → coldest. Compares **last-15-day ERA** to season ERA (15 days, not 7 — starters pitch too infrequently for a 7-day window to be meaningful). Includes a **Whiff%** column (raw swing-and-miss rate, green ≥ 30%) and a role-aware **Score** badge.

### Roster Hot/Cold
Your rostered **hitters** sorted hottest → coldest. Compares last-7-day OPS to season OPS. Includes an **HR%** column (modeled per-game home-run probability, hover for drivers — also listed inside the expanded Score panel for touch devices) and a **Score** badge. Tapping the Score badge shows a breakdown whose recent-form line names its window (e.g. "30-day form") — a broader window than this L7 column, so a bat can be 🔥 here yet read "cold" on the composite.
- 🔥 = OPS up +0.050 or more
- ↑ = OPS up +0.015 to +0.050
- ↓ = OPS down -0.015 to -0.050
- ❄ = OPS down -0.050 or more

### Positional Breakdown
For each position (C, 1B, 2B, 3B, SS, OF, SP, RP): your weakest rostered player vs. the best available FA at that position. **↑** = the FA is a meaningful upgrade. A player parked in one of your 2 IL roster slots is never surfaced as the weakest/drop candidate (cutting them frees no active or bench room).

### Roster Alerts
Any injured players on your roster. Only shown if there are active alerts. Color: yellow = DTD, red = IL/OUT.

### Lineup Watch
A compact callout that audits your **daily** lineup for the week so far (Monday → yesterday), reconstructed from ESPN's historical per-day slots. It surfaces two kinds of start/sit mistakes:

- **Bench leakage** — counting-stat production (R/HR/RBI/SB) a hitter racked up while sitting in a bench slot, so it never counted. Shown **net of the bat you'd have benched to start him** — if your active lineup was full at his eligible positions, playing him meant sitting someone, so the tool subtracts that player's line (a feasibility check on your lineup slots + each player's position eligibility decides whether an open slot even existed). This is the honest "money left on the table," not raw bench stats.
- **Active-slot blowups** — a starter who imploded (5+ ER, or 4+ ER in <3 IP) *in your active lineup*, so the ERA/WHIP damage counted. Flagged with a note if you then dropped him ("imploded then cut").

Only still-actionable, net-positive misses appear — it's silent on a clean week. The Monday recap carries the fuller completed-week version (**Lineup Efficiency**). Deep-dive / opponent comparison: run `python bench_leakage.py`.

### FA Pickup — Starting Pitchers
Free agent starters with a confirmed upcoming start, grouped by date with day headers. Sorted by composite SP score within each day. Starts past Sunday get a `NEXT WK` badge; a pitcher with ≥ 2 starts in the matchup week gets a purple `2-START` chip.

**Columns:** Pitcher · **Proj. Line** · Matchup (with opponent OPS on a second line) · QS% · ERA · **L15 ERA** (hot/cold colored) · K% · Score

**Proj. Line** = projected `IP · ER · K` for one start. ER is adjusted for opponent lineup strength (their OPS) and a home/away park factor; K is adjusted for the opponent lineup's strikeout rate. IP is the pitcher's per-start average in baseball notation (e.g. 5.1 = 5⅓).

**Day headers** show a ⚑ badge with your start count for that day: red = 0 my starts, yellow = 1, blue = 2+.

**Pickup badges** annotate the projected line for **every** FA start (not only on thin rotation days), so a badge always matches the **Proj. Line** you see. Both can fire simultaneously:
- 🟢 **QS** badge (green left border) — the projected line is a quality start (6+ IP & ≤3 ER)
- 🟡 **5K+** badge (yellow left border) — the projected line is 5+ K
- When both fire: left border is green (top half) / yellow (bottom half)

The **QS% column** shows the season quality-start *probability* separately.

**K% highlight** — top 3 K% values across the table are highlighted yellow.

**FA exclusion:** players who appear in today's ESPN transaction log as "FA ADDED" (net of any same-day drops) are excluded even if the ESPN roster API hasn't reflected the pickup yet. DL-status players are also excluded.

### FA Pickup — Relief Pitchers
Top available relievers (must have ≥ 1 SV+H on the season), ranked by RP score (SV+H · K · W · ERA · WHIP — skill-weighted, see [Composite Scores](#composite-scores)). A **Cats** column lists up to 3 roto categories the reliever is strong in, with your currently-contested categories highlighted. Includes a **Save-Role Watch** callout flagging emerging FA closers and fading rostered closers.

### FA Pickup — Hitters
Top available hitters sorted by composite score. Columns: R · HR · RBI · SB · OPS · **Cats** (up to 3 strong roto categories, your contested ones highlighted) · **HR%** (modeled per-game HR probability) · Score. Includes a hot/cold recent-form indicator.

### My Upcoming Starts
Your pitchers with confirmed or projected starts, grouped by date.

**Columns:** Pitcher · **Proj. Line** · Matchup (with opponent OPS on a second line) · QS% · ERA · **L15 ERA** (hot/cold colored) · K% · Score

Badges next to the name: `2-START` (purple), `QS` (green — projected quality start, 6+ IP & ≤3 ER), `5K+` (yellow — projected 5+ K). Both annotate the **Proj. Line** shown for that start (they never contradict it), identical to FA Starting Pitchers. `(proj.)` = rotation estimate, not yet confirmed by MLB. **K% highlight** — top 3 K% values across the table are highlighted yellow.

### My Season Category Rankings
Season-to-date roto rank across all 12 categories. Same color coding as the weekly version at the top, but for the full season.

### Matchup N Roto Rankings
Sits just above the Matchup table (section 5b). All 12 teams ranked by current-matchup roto score, with all 12 category columns. Updates live throughout the matchup so you can watch standings shift — the roto table is ranked by each category's live **value** (not ESPN's per-category result, which stays unset until the period closes), so it populates as soon as the matchup's first games are played. Hidden only before any stats accumulate (when all teams share an equal roto score — same suppression logic as Current Matchup). Uses the same 5-tier heat-map coloring as the Monday recap: bright green = #1 in cat, light green = #2, amber = #11, red = #12, muted = middle pack. Your team is bold blue; category leaders get accent-colored pills. Row background tints top-3 green, bottom-3 red.

### League Luck Standings
All 12 teams sorted by record. Shows W-L-T · Win% · Roto rank · Cumulative roto points · Luck delta. **Luck** = roto rank minus record rank. Positive luck means your W-L-T is better than your underlying stats deserve; negative means you're underperforming your true quality.

### Season Trajectory
A W/L/T grid of the whole season — every team down the rows (in standings order), each completed week across the columns, and each team's **current streak** (e.g. W3, L2) in the final column. Wins are green, losses red, ties white. Your row is highlighted. Same panel as the Monday recap (ported so the two share the view); it scrolls horizontally on narrow screens as the season lengthens.

### Season Roto Rankings
The same 12-category roto grid as the live **Matchup N Roto Rankings** panel, but aggregated over the **entire season to date** rather than one matchup. All 12 teams are **ranked** by cumulative roto score (the sum of each matchup's roto points — i.e. who won each category week by week). Each category cell **displays the true season-to-date figure straight from ESPN** (innings/AB-weighted, so it reconciles with the site to the digit — a season ERA is *not* the average of your weekly ERAs). Ranking and displayed value are independent: a team can show a better season ERA yet sit lower in ERA points if it lost more of the weekly ERA matchups. Cells use the same 5-tier heat-map (bright green = best in cat, light green = 2nd, amber = 2nd-last, red = last, muted = middle). Your team is bold blue; top-3 rows tint green, bottom-3 red. Same panel as the Monday recap (ported so the two share the view).

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
| Barrel%/hard-hit% allowed, **xERA, xwOBA-against, whiff percentile, raw whiff%** (pitchers) | Baseball Savant via `pybaseball` | No |
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

Scores are **not** dampened for injuries (injury status is shown separately as a tag; DL players are excluded from FA lists).

> **Tap any Score badge for its breakdown.** Every Score badge (including the Positional Breakdown badges) expands on tap into a **full-width row below the player** that narrates, in plain English, the 2–3 drivers behind the number — e.g. *"Carried by swing-and-miss (24% K) and limits baserunners (1.23 WHIP); no glaring holes. Recent form 58 (cold) → shown blends 65% season / 35% recent."* — so you can see *why* two similar-looking players score differently. A ▾ caret marks a tappable badge; tap the ✕ (or another badge) to close. The tapped player row stays in view (the breakdown opens in the upper-middle of the screen rather than snapping to the top). Works when you open the HTML attachment on phone/tablet. (Pure CSS `:target`, no JavaScript — email-safe.)

Three canonical role scores:

**Starting-pitcher score (`pitcher_score` / `_score_p`):** K% (blended 60/40 with Baseball Savant whiff percentile) + run prevention (ERA blended 55/45 with Savant xERA) + WHIP + contact-quality allowed (barrel%/xwOBA-against) + a start-volume role bonus. Small samples damped toward the mean. Displayed blended 65% season / 35% recent form.

**Relief-pitcher score (`rp_score`):** Skill-weighted **punt-saves** build — K, ERA (blended with xERA) and WHIP carry most of the weight; **SV+H is deliberately de-emphasized (~15%)** since it's the most volatile category and one we're willing to sacrifice. A dominant setup man can outrank a mediocre closer. Counting stats prefer ESPN season totals.

**Hitter score (`hitter_score`):** wRC+ (or OPS) + HR volume + ISO + RBI + speed (sprint speed preferred, falls back to SB) + xwOBA/AVG + HR-probability model. Scaled by an **opportunity multiplier** (at-bats vs a full-time benchmark) so a part-time bat can't score like a regular. Displayed blended 65% season / 35% recent form.

**QS Probability:** Formula-based estimate (no MLB API support). Inputs: IP/G, ERA, WHIP, Brl%, K%, opponent OPS. Baseline = 38% (league average). Key driver is IP/G — uses total games (not just starts) so relief appearances bleed down the innings-depth signal for mixed-role pitchers. Calibration: ace (~75%), league avg (~38%), short reliever making a spot start (~15%). Shown as a color-coded percentage in FA SP and My Upcoming Starts tables: green ≥ 60%, white ≥ 40%, muted < 40%.

> **Pitcher scores self-recalibrate.** The SP/RP p50→50 / p90→80 constants are re-derived from the live data on every run (`compute_score_calibration`), so the 0–100 scale tracks the season without any hand-editing. If the qualified pitcher pool is too thin (early season), it falls back to the last hand-tuned constants. `recalibrate_scores.py` is now just a manual inspection tool (prints the current live constants) and the home of those fallback values — update them there if you materially change a score's component mix. Hitter scores still use fixed constants.

---

## Key Snapshot Fields

`data/snapshot.json` is rebuilt on every run. It's the only file shared between `fetch_data.py` and `send_digest.py`.

**pitchers** (list of dicts, one per player per time range):
`PlayerName, FantasyTeam, Position, Dataset` (7/15/30/2026), `IP, G, GS, K, ERA, WHIP, SV, HLD, SVHD, K/IP, Kpct_P, IP_per_G` (IP÷G — honest for mixed starters/relievers), `PSP_Date` (1999-01-01 = no start), `PSP_HomeVAway, PSP_Projected`, `PSP_Dates` + `PSP_HomeVAways` (lists of ALL upcoming starts, for two-start detection), `Team_OPS_Value, Team_K_Value` (opponent OPS & K-per-PA), advanced: `xERA, xwOBA_against, WhiffPctile, WhiffPct` (raw rate, display-only), `BarrelPctAllowed, HardHitPctAllowed, AvgEVAllowed`, ESPN season counts: `ESPN_SV, ESPN_K, ESPN_W, ESPN_IP, ESPN_GS, ESPN_GP, ESPN_SVHD`

**hitters** (list of dicts, one per player per time range):
`PlayerName, FantasyTeam, Position, Dataset, HR, RBI, R, SB, AVG, OPS, wRCplus, xwOBA, xBA, xSLG, SprintSpeed, ISO, Barrel_Pct, HardHit_Pct, HR_Probability`

**roto** (list of dicts, one per team per week):
`Team, Week, R, HR, RBI, SB, OPS, B_SO, K, QS, W, ERA, WHIP, SVHD, Roto_Score, {CAT}_Points`

**standings** (list of dicts):
`team_name, wins, losses, ties, standing, logo_url`

**current_matchup** (dict):
`week, my_team, opp_team, wins, losses, ties, categories[]`
Each category: `cat, my_val, opp_val, result` (W/L/T), `lower_better`

**recent_hitting** (list of dicts — last 7 rolling days, all MLB hitters):
`PlayerName, G, PA, AB, R, HR, RBI, SB, OBP, SLG, OPS`

**recent_pitching** (list of dicts — last 15 rolling days, all MLB pitchers):
`PlayerName, G, GS, IP, ERA, WHIP, BB`

**prev_week_hitting** (list of dicts — exact previous matchup Mon–Sun, all MLB hitters):
Same schema as `recent_hitting`. Used by `build_commissioner_story` (hitter-of-the-week) **and `build_top_performers`** so the recap's Top Performers timeline matches the rest of the recap (the exact matchup week), not a rolling window.

**prev_week_pitching** (list of dicts — exact previous matchup Mon–Sun, all MLB pitchers):
Same schema as `recent_pitching`. Used by `build_commissioner_story` (pitcher-of-the-week) **and `build_top_performers`** (matchup-week timeline). The Top Performers pitcher table shows **K** rather than G.

**weekly_results** (dict — `{"1": {"Team Name": "W"/"L"/"T", ...}, ...}`):
Per-week head-to-head matchup results for every team. Keys are week numbers as strings. Note: the sparkline dot encoding uses roto-derived rank results computed in `send_digest.py` from the `roto` data — not this H2H field directly.

**lineup_efficiency** / **lineup_efficiency_current** (dicts — MY team's daily start/sit audit):
`week, mode` ("prev"/"current"), `week_dates`, `bench[]` (per stranded hitter: name, slash, R/HR/RBI/SB, `net` correction, and per-day `days[]` with the swap target), `gross`/`net` totals, `blowups[]` (active-slot pitcher meltdowns + drop flag). `lineup_efficiency` is the last completed week (Monday recap); `lineup_efficiency_current` is the in-progress week Mon→yesterday (daily-digest Lineup Watch). Both come from `get_lineup_efficiency`, which reads ESPN's historical per-day lineup via `mRoster?scoringPeriodId=<day>`.

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

Contributor docs are split in two: **`CLAUDE.md`** holds the actionable rules and gotchas (kept lean so it loads fast as agent context), and **`NOTES.md`** holds the background — the "why we did it this way" narrative and the forensic history behind past decisions.

```
baseball/
├── fetch_data.py                        # Data pipeline — runs first (~60s)
├── send_digest.py                       # Email builder + sender
├── weekly_recap.py                      # Monday full-league recap email builder
├── bench_leakage.py                     # Standalone daily-lineup audit (my team + opponent → console)
├── CLAUDE.md                            # Actionable rules / gotchas for contributors
├── NOTES.md                             # Background & rationale ("why we did it this way")
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
