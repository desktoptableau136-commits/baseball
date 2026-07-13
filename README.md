# Fantasy Baseball — Daily Digest

Automated email digest for ESPN fantasy league 277836 (Guerrero Warfare). Runs once each morning via GitHub Actions (fires 06:00 UTC / 2 AM EDT; GitHub's scheduler typically adds ~6h, so it lands ~8 AM ET) — no laptop required. A single email carries a short skimmable **Briefing** in the body plus the full digest and the single-viewport dashboard as HTML attachments.

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

2. **`send_digest.py`** reads the snapshot, builds the email, and sends it via Gmail SMTP. The **inline body** is a short plain-English **Briefing** (time-sensitive actions, then a one-line matchup and season read) meant to be skimmed before you dig in; the **full digest** rides along as an attached `digest_YYYY-MM-DD.html` you open in a browser. Run with `--with-dashboard` (the daily job does) and the single-viewport dashboard is attached too, so one email carries everything. Alternatively saves previews under `previews/` for local viewing (no email).

3. **`weekly_recap.py`** reads the same snapshot every Monday and emails a full-league recap: **Matchup N Highlights** (commissioner-style prose + stat sidebar — roto winner, hitter/pitcher/FA of the matchup with MLB team logos and named historical benchmarks), your matchup result, **Lineup Efficiency** (last matchup's start/sit opportunity cost — bench leakage + active-slot pitcher blowups), all 6 scoreboard matchups, Matchup Roto Rankings (all 12 categories, 5-tier heat-map coloring), Top Performers (hitters and pitchers side-by-side), Standings & Luck, Season Trajectory, and Season Roto Rankings (the same 12-category grid aggregated over every matchup — ranked by cumulative roto points, each category showing its true season-to-date value from ESPN). Saves `previews/recap_week_N.html` on dry runs. GitHub Actions: `.github/workflows/weekly-recap.yml` (Monday 15:30 UTC).

4. **GitHub Actions** runs the daily job (`send_digest.py --with-dashboard`, one email with both attachments) and the Monday recap automatically, using credentials stored as repository secrets — no laptop needed.

`snapshot_schema.py` validates `data/snapshot.json` against the contract the readers depend on. `fetch_data.py` runs it automatically before saving — if the fetch produced a broken snapshot (a missing key, an empty roster, an upstream column drop) the run fails loudly and the previous good snapshot is left in place, rather than silently emailing a garbled digest. Run it by hand anytime with `python snapshot_schema.py`.

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

A glance-able "command dashboard" that condenses the entire digest onto **one 1440×900 laptop screen with zero scrolling** — even coverage of every topic (today's games, category pulse, pitching, hitting hot/cold, weakest spots, moves, free agents, trade radar, season). It reuses the digest's exact scoring so every number matches.

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

A compact **Today's Games** tile rides at the top of the left column — your favorite team (Atlanta, marked ★ and pinned first) plus the next highest-overlap game, each with first-pitch time, where to watch, the pitching matchup, and the involved players (with the same tactical badges as everywhere else) — the same "what to tune into" read as the digest's Today's MLB Games, trimmed to two games.

The **My Pitching** tile lists each upcoming start with its projected `IP·ER·K` line, a blue `×2` marker for two-start pitchers, cyan **QS** / yellow **5K+** badges, an orange **⚠** low-floor (blowup-risk) chip, and the **$ / ▼** buy-low / sell-high chip after the matchup date (same projected-line, risk, and regression rules as the digest). The **Free-Agent Radar** (starters and relievers) and **Weakest Spots** (pitcher rows) carry the ⚠ and $ / ▼ chips too. A compact **Trade Radar** tile (in place of a standalone opponent tile — opponent scouting lives in the digest) shows the top couple of mutually-beneficial trade suggestions (two distinct partners), each laid out as a 3-column mini-card — partner + value tilt on the left, then **Give** and **Get** columns of players — with a **score pill** (hover for a short breakdown), plus the same $ / ▼ and position chips as the digest — the full list lives in the daily digest. **When you have a real incoming trade offer, it leads this tile** (retitled **"Trades"**) as an "📥 Offer to review" card with the **Accept / Counter / Decline** verdict, days-until-expiry, and the counter suggestion — a concrete decision on the clock outranks the speculative ideas, which drop to one to keep the layout scroll-free. The **Recommended Moves** tile shows a **score pill** next to every add and drop player, so you can gauge the quality of a suggested pickup and the player it costs at a glance. **MLB team logos** appear next to player names throughout the dashboard (Today's Games, My Pitching, Hitting Hot/Cold, Weakest Spots, Free-Agent Radar, Trade Radar). The browser tab title reads **"Dashboard — {team}"** (and the daily digest reads **"Daily Digest — {team}"**) so the type is identifiable at a glance. A **legend** defines every badge/marker at a glance (score pill, ▲▼◆ projected outcome, ⚡ toss-up, ×2, QS, 5K+, ⚠, $ / ▼, PWR, SB, 🔥/❄) — a slim strip pinned to the bottom of the pane on desktop, and a "Key" panel at the bottom of the right column (below Trade Radar) on tablet/phone.

It's also **responsive**: on a tablet (≤1100px) the tiles reflow into two height-balanced columns — Today's Games → Category Pulse → Recommended Moves → Free-Agent Radar down the left, then My Pitching → Hitting Hot/Cold → Weakest Spots → Trade Radar → Season down the right — and on a phone (≤700px) into a single column, un-pinning the fixed pane so the page scrolls normally with larger, readable text. The desktop no-scroll layout is unchanged above 1100px.

**Viewing on a phone/tablet:** use `--email` (or attach `previews/dashboard_{team}.html` to an email yourself) and open the **attachment** in your device browser — email apps strip the `<style>` block that holds the layout, so the attached file works but a pasted-in body won't.

### Interactive Trade Lab

A hands-on, **browser-only** trade builder. Where the digest's Trade Radar hands you finished ideas, the Trade Lab lets you construct your own: pick any two teams, browse each roster grouped by role, click players onto each side of the deal, and watch it get graded in real time.

```bash
# Write previews/tradelab_{team}.html from the existing snapshot (fast, no email)
python trade_lab.py

# Refresh data first, then write
python trade_lab.py --refresh

# Default the left (my) side to another team
python trade_lab.py --team "Houck Tuah"
```

Open the file in a browser (it's the only page here that uses JavaScript, so it **can't be emailed** — mail clients strip the script). The layout is three columns — **My Team · Trade · Trade Partner**. Each side has a team dropdown (any of the 12 teams) and lists that roster as **Hitters / Starting Pitchers / Relief Pitchers**, every player carrying the same tactical badges (PWR, SB, ⚠, $ / ▼) and a color-coded **score pill** you can click to expand a plain-English breakdown — identical to the digest.

Click players to drop them into the center **You give / You get** ledger and the panel grades the deal live: an **Accept / Counter / Decline** verdict, the value tilt (*you win the value / even / you pay up*), the categories you gain (green when they fill one of your needs) and lose (red when it's a real strength), any positional upgrades, the timing read (are you selling high / buying low, or walking into a trap), and a **"Would they do it?"** partner-fit line that checks whether what you're offering addresses the other team's actual category needs. Every number is pre-computed by the same scoring code as the digest, so the lab can never disagree with it.

Two things help you build the deal quickly. Each player carries a **position chip** by their name, and partner players who'd actually help you are flagged with a **🎯 target** marker (they're strong in a category you need, or upgrade one of your thin positions). Below the ledger, a **Deal Coach** panel shows what each side needs and your leverage, then lists **value-ranked, clickable suggestions** — players to *get* that fill your needs and pieces to *offer* that fill theirs (click one to add it) — plus a running balance nudge. A **Fair · Favor me · Fleece** strategy toggle tunes how the coach curates: sliding toward *Fleece* protects your best players from the offer list, leads with cheaper fillers, and aims for a bigger value edge in your favor. The toggle only shapes the coaching — the Accept/Counter/Decline verdict stays objective.

**On Windows PowerShell**, if `git` isn't found, run this first to restore it:
```powershell
$env:PATH = [System.Environment]::GetEnvironmentVariable("PATH","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("PATH","User")
```

---

## Automation

Two scheduled workflows run on GitHub Actions (plus one manual-only), Python on Ubuntu; cron is always UTC:

| Workflow | Cron (UTC) | Fires (EDT) | Lands (ET) | What it runs |
|---|---|---|---|---|
| `daily-digest.yml` | `0 6 * * *` | 2 AM | ~8 AM | `python send_digest.py --with-dashboard` |
| `weekly-recap.yml` | `30 15 * * 1` | Mon 11:30 AM | — | `python weekly_recap.py` |
| `daily-dashboard.yml` | *manual only* | — | — | `python dashboard.py --refresh --email` (`workflow_dispatch`) |

**One email, both deliverables.** The daily job runs `send_digest.py --with-dashboard`: a single ESPN fetch, then one email carrying the digest (inline body + `digest_*.html` attachment) *and* the dashboard (`dashboard_*.html` attachment). The old separate `daily-dashboard.yml` schedule is retired (kept for manual standalone sends). If the dashboard ever fails to build, the digest still sends — the attachment is just dropped.

**The cron is when the job *fires*, not when the email *arrives*.** GitHub's scheduler is unreliable — delays run 1–4 hours and in practice **~6 hours** (the 06:00 UTC job lands ~8 AM ET), and only ever push runs *later*. That same lag keeps the data fresh: the 2 AM cron actually *executes* ~8 AM, well past ESPN's overnight refresh. (Firing later — e.g. 4 AM EDT — would guard against the rare fast-scheduler day but push everyday arrival ~2h later, so we don't.)

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

The digest is organized into labeled **bands**, with a **Jump to** nav in the header (My Roster · Transactions · Season · Glossary) that anchors to each band.

**Header** — date · team name + logo · KPI row · Jump-to nav pills
KPI row: **Record** · **Current Matchup** (W-L-T + win%) · **Roster** (whole-team hot/cold count — hitters *and* pitchers) · **Starts This Matchup**

**Matchup overview** (top of the email)
1. **Monday Recap** — *(Mondays only)* last matchup's final result (per-team via `all_prev_matchups`, so `--team` shows that team's prior matchup)
2. **Matchup at a Glance**
3. **Category Pulse**
4. **Opponent This Matchup** — scouting block for this matchup's opponent
4b. **Today's MLB Games** — real games today ranked by how much they overlap your matchup (weighted toward your roster; counts hitters, confirmed starters, and relievers), with first-pitch time, where to watch (national + local TV), the pitching matchup, per-side player counts, and each player's tactical badges. Your favorite team's games (Atlanta) are pinned first *(what to tune into; renders only when a game overlaps your matchup)*
5. **Current Matchup** — this matchup's category rankings grid *(hidden Monday before stats accumulate)*
5b. **Matchup N Roto Rankings** — live all-12-team roto table for the current matchup *(hidden Monday before stats accumulate)*
6. **Matchup N** — score banner + category-by-category table

**⚑ MY ROSTER**
7. **Roster Alerts** — *(only if you have injured players)*
7b. **Lineup Watch** — *(matchup-to-date bench leakage / active-slot pitcher blowups / idle "wasting space" hitters; silent on a clean matchup)*
8. **Positional Breakdown**
9. **My Upcoming Starts**
10. **My Relief Pitchers**
11. **Pitcher Hot/Cold**
12. **Roster Hot/Cold**

**TRANSACTIONS**
12b. **Pending Trades** — the real trade offers involving your team, each graded **Accept / Counter / Decline** (for offers made to you) or shown as *awaiting the partner* (for offers you sent), with the same player scores and value read as Trade Radar. On a **Counter**, it names the best add-on to ask the other manager for. Because offers expire, an incoming-offer headline (with the verdict + days left) also rides at the top of **Matchup at a Glance** and in the email body's **⚡ Act today** list *(shown only when a trade is pending)*
13. **FA Pickup — Starting Pitchers**
14. **FA Pickup — Relief Pitchers**
15. **FA Pickup — Hitters**
15b. **Trade Radar** — mutually-beneficial trade ideas with rival teams *(shown only when candidates exist)*

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

### Today's MLB Games
Answers a simple question a league-mate posed: *which real games today actually move my matchup?* Each of today's MLB games is ranked by how many of **your** and **your opponent's** rostered players are in it, **weighted toward your roster** (your players count double the opponent's, and a confirmed starting pitcher counts double a hitter — he's guaranteed to pitch and touches ~5 pitching categories). Players likely to actually appear are counted: **hitters, confirmed starting pitchers, and relievers** (a save/hold chance moves the week; relievers are identified by usage *role*, not just position). The one exclusion is a **starting pitcher who isn't starting tonight** — a starter on his off-day — so he can't inflate the game (e.g. your starter Drohan counts on his start day; your *other* starter sitting that night does not, but your closer does).

Each of the top few games shows both teams' logos, first-pitch time (ET), **where to watch** (the national network when there is one, plus each side's local TV feed so you always have a channel), the **pitching matchup** (`⚾ SP: away vs home`, with a probable highlighted in blue if he's yours / red if your opponent's), and a **"You: N / Opp: N"** line naming the involved players (a ⚾ marks a confirmed starter) — each with the **same tactical badges** the rest of the digest uses (power/speed/buy/sell for hitters, blowup-risk/buy/sell for pitchers). Your **favorite team's games (Atlanta) are pinned to the top** (marked ★) regardless of overlap score. The single highest-overlap game also gets a one-line **📺 Tune in** teaser (with its network) at the top of the email body. Hitters are counted optimistically — we don't have posted MLB batting lineups, so only starting pitchers are confirmed. Renders only when at least one game overlaps your matchup (skipped on an off-day).

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
Your rostered pitchers sorted hottest → coldest. Compares **last-15-day ERA** to season ERA (15 days, not 7 — starters pitch too infrequently for a 7-day window to be meaningful). Includes a **Whiff%** column (raw swing-and-miss rate, green ≥ 30%) and a role-aware **Score** badge. Pitcher names can carry a **$ / ▼** buy-low / sell-high chip (the pitcher version of the hitter regression badge): **$** = ERA running *above* his Statcast expected ERA (xERA) → unlucky, buy-low; **▼** = ERA *below* xERA → lucky, sell-high. It's measured relative to the league's typical xERA-vs-ERA offset, and it's **distinct from the ⚠ blowup-risk flag** — ▼ is mean regression (a lucky ERA), ⚠ is single-start disaster risk. Also appears in My Upcoming Starts, My Relief Pitchers, the FA pitcher lists, and Positional Breakdown, and it powers the buy-low / sell-high timing in Trade Radar.

### Roster Hot/Cold
Your rostered **hitters** sorted hottest → coldest. Compares last-7-day OPS to season OPS. Includes an **HR%** column (modeled per-game home-run probability, hover for drivers — also listed inside the expanded Score panel for touch devices) and a **Score** badge. Tapping the Score badge shows a breakdown whose recent-form line names its window (e.g. "30-day form") — a broader window than this L7 column, so a bat can be 🔥 here yet read "cold" on the composite.

**Hitter badges** (next to the name, hover for the justifying stat) flag tactical edges — display-only, never part of the score; every applicable badge shows (no cap):
- 🟣 **PWR** — power/HR threat (top-tier modeled HR probability)
- ⚪ **SB** ("Quicksilver", silver) — a genuine base-stealer (top-20% SB producer, speed-corroborated)
- 🟢 **$** — buy-low: under-performing his Statcast expected stats (positive regression coming)
- 🔴 **▼** — sell-high: over-performing his expected stats (regression risk — don't chase)

These also appear on FA Hitters, the Positional Breakdown, and across the dashboard (Hitting, FA Radar, Weakest Spots). **Tapping the Score badge** explains each chip the player earned, with the exact stat that triggered it (e.g. "SB — 26 SB, top 1% of the league · 28.5 ft/s sprint"). The pitcher Score dropdown does the same for the **QS / 5K+ / 2 / ⚠** badges (e.g. "QS — projected 6.1 IP · 3 ER is a quality start"; "5K+ — projected 7 strikeouts, backed by a 30% whiff rate"; "⚠ — low floor, blowup-prone: 1.48 WHIP · 5.18 eff. ERA · 6.94 L15 ERA (cold)"). The 5K+ and ⚠ badges' hover tooltips carry those same stats.
- 🔥 = OPS up +0.050 or more
- ↑ = OPS up +0.015 to +0.050
- ↓ = OPS down -0.015 to -0.050
- ❄ = OPS down -0.050 or more

### Positional Breakdown
For each position (C, 1B, 2B, 3B, SS, OF, SP, RP): your weakest rostered player vs. the best available FA at that position. **↑** = the FA is a meaningful upgrade. A player parked in one of your 2 IL roster slots is never surfaced as the weakest/drop candidate (cutting them frees no active or bench room).

### Roster Alerts
Any injured players on your roster. Only shown if there are active alerts. Color: yellow = DTD, red = IL/OUT.

### Lineup Watch
A compact callout that audits your **daily** lineup for the matchup so far (its first day → yesterday — the **full matchup period**, so a 14-day All-Star/playoff matchup is covered end-to-end, not just the current calendar week), reconstructed from ESPN's historical per-day slots. It surfaces three kinds of start/sit mistakes:

- **Bench leakage** — counting-stat production (R/HR/RBI/SB) a hitter racked up while sitting in a bench slot, so it never counted. Shown **net of the bat you'd have benched to start him** — if your active lineup was full at his eligible positions, playing him meant sitting someone, so the tool subtracts that player's line (a feasibility check on your lineup slots + each player's position eligibility decides whether an open slot even existed). This is the honest "money left on the table," not raw bench stats.
- **Active-slot blowups** — a starter who imploded (5+ ER, or 4+ ER in <3 IP) *in your active lineup*, so the ERA/WHIP damage counted. Flagged with a note if you then dropped him ("imploded then cut").
- **Wasting active space** — a hitter sitting in an active slot but not accumulating stats (0 AB), **only counting games his MLB team actually played** (a scheduled off day is never held against him). Surfaced only when it's a pattern — idle **3 games in a row**, or an AB in **under half** the games he was slotted active — so an occasional rest day stays silent while a genuinely stranded roster spot gets flagged.

Only still-actionable misses appear — it's silent on a clean matchup. The Monday recap carries the fuller completed-matchup version (**Lineup Efficiency**). Deep-dive / opponent comparison: run `python bench_leakage.py`.

### FA Pickup — Starting Pitchers
Free agent starters with a confirmed or projected upcoming start, grouped by date with day headers. Sorted by composite SP score within each day. Starts past Sunday get a `NEXT WK` badge; a pitcher with ≥ 2 starts in the matchup week gets a blue `2` chip. **Only starters with an SP score of 30 or higher are shown** — streamer-tier arms below that are filtered out (tunable via `_FA_SP_MIN_SCORE`).

**Columns:** Pitcher · **Proj. Line** · Matchup (with opponent OPS on a second line) · QS% (with xERA on a second line) · ERA · **L15 ERA** (hot/cold colored) · K% (with raw whiff% on a second line) · Score

**Proj. Line** = projected `IP · ER · K` for one start. ER builds off the pitcher's ERA regressed toward his expected ERA (xERA — luck-stripped, weighted by season IP), then adjusted for opponent lineup strength (their OPS) and a home/away park factor; K is adjusted for the opponent lineup's strikeout rate. IP is the pitcher's per-start average in baseball notation (e.g. 5.1 = 5⅓). *(The ERA regression is a small backtest-verified accuracy gain — see `backtest_projections.py`.)*

**Day headers** show a ⚑ badge with your start count for that day: red = 0 my starts, yellow = 1, blue = 2+.

**Pickup badges** annotate the projected line for **every** FA start (not only on thin rotation days), so a badge always matches the **Proj. Line** you see. Any combination can fire together:
- **QS** chip (cyan) — the projected line is a quality start (6+ IP & ≤3 ER)
- **5K+** chip (yellow) — the projected line is 5+ K
- **⚠** badge (orange, glyph only) — a **low-floor** (blowup-prone) skill profile: high WHIP + weak strikeout escape hatch + poor effective run prevention (ERA regressed toward xERA) + loud contact allowed, escalated when the arm is cold lately (high L15 ERA). Hover for the worst 2–3 drivers. It's a floor *warning* only — it never lowers the Score, and the digest steers pickup recommendations away from flagged arms. Blowups are largely random (validated in `backtest_projections.py`: ~1.25× top-decile lift, AUC ≈ 0.52), so treat it as "stream with caution," not a guarantee.

The **QS% column** shows the season quality-start *probability* separately, with the pitcher's **xERA** (luck-stripped run-prevention skill — what the ER projection regresses toward) on a muted second line beneath it. A lower xERA than his ERA hints the quality-start rate is earned rather than lucky.

**K% highlight** — top 3 K% values across the table are highlighted yellow.

**FA exclusion:** players who appear in today's ESPN transaction log as "FA ADDED" (net of any same-day drops) are excluded even if the ESPN roster API hasn't reflected the pickup yet. DL-status players are also excluded.

### FA Pickup — Relief Pitchers
Top available relievers (must have ≥ 1 SV+H on the season), ranked by RP score (SV+H · K · W · ERA · WHIP — skill-weighted, see [Composite Scores](#composite-scores)). A **Cats** column lists up to 3 roto categories the reliever is strong in, with your currently-contested categories highlighted. Includes a **Save-Role Watch** callout flagging emerging FA closers and fading rostered closers.

### FA Pickup — Hitters
Top available hitters sorted by composite score. Columns: R · HR · RBI · SB · OPS · **Cats** (up to 3 strong roto categories, your contested ones highlighted) · **HR%** (modeled per-game HR probability) · Score. Includes a hot/cold recent-form indicator and the **PWR / SB / $ / ▼** tactical badges next to the name (see [Roster Hot/Cold](#roster-hotcold)).

### Trade Radar
Trade ideas with rival teams — the one lever the digest can act on beyond your own roster and the free-agent pool. Each card **fixes a rival's category need** (their reason to accept the deal) while **tilting value to you**. You send a player strong in a category you're deep in and the rival is weak in, and get back one who fills a category **or a thin roster position** you need. Only players at a position where you have **surplus** are offered (so no trade ever opens a hole), and your **elite bats are protected** — the radar won't put a genuine masher on the block unless he's a sell-high regression candidate you'd want to move anyway.

Value is judged on **true category contribution**, not the role-score badge: a closer and an everyday hitter can both post a 90+ score, but the hitter contributes to five categories every day while the closer touches one *punt* category (SV+H) plus a sliver of ERA/WHIP/K in ~60 innings — so an everyday smasher won't be offered straight up for a reliever (saves are discounted since we punt them). Because the hitting game is filling roster holes across C/1B/2B/SS/OF, the radar also surfaces hitters who **upgrade a thin position** even when that position isn't a bottom-third *category*.

Deals are tuned to **favor you** rather than land even (the rival accepts on category need, so the value can tilt your way), and where possible they exploit **buy-low / sell-high** timing from Statcast expected-vs-actual stats: move a bat whose surface numbers are about to regress, acquire one due to rebound. Chips flag what's in play — blue = category gained, cyan = a thin position filled, and the same **$** (buy-low) / **▼** (sell-high) glyphs used everywhere else in the digest; the footer tag tells you which way the timing helps you. Every player's **Score pill is tappable** (on both sides of the deal) for the same prose breakdown you get in the section tables. Both 1-for-1 and 2-for-2 shapes appear; the section only shows when real candidates exist.

### My Upcoming Starts
Your pitchers with confirmed or projected starts, grouped by date.

**Columns:** Pitcher · **Proj. Line** · Matchup (with opponent OPS on a second line) · QS% (with xERA on a second line) · ERA · **L15 ERA** (hot/cold colored) · K% (with raw whiff% on a second line) · Score

Badges next to the name: `2` (blue — two starts this matchup week), `QS` (cyan — projected quality start, 6+ IP & ≤3 ER), `5K+` (yellow — projected 5+ K). Both annotate the **Proj. Line** shown for that start (they never contradict it), identical to FA Starting Pitchers. `(proj.)` = rotation estimate, not yet confirmed by MLB. **K% highlight** — top 3 K% values across the table are highlighted yellow.

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
A collapsible in-digest reference at the very bottom (also linked from the header nav). Six expandable groups — **Scores**, **Badges & icons**, **Pitching metrics**, **Hitting metrics**, **Projections & matchup**, **Data sources** — explaining how every score and metric is computed and where the data comes from. The **Badges & icons** group renders each actual badge chip inline beside its definition and is sub-grouped by who it applies to (Any player · Pitchers · Hitters · Buy-low/sell-high · Category Pulse cards), so a shared badge like `$` / `▼` is defined once. Kept in sync with the code as part of the save sequence.

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
| Probable starters (rotation-order projection) | MLB Stats API | No |
| Opponent team **OPS and K rate** | MLB Stats API | No |
| Barrel%/hard-hit% allowed, **xERA, xwOBA-against, whiff percentile, raw whiff%** (pitchers) | Baseball Savant via `pybaseball` | No |
| xwOBA, xBA, xSLG, Barrel%, hard-hit%, sprint speed (hitters) | Baseball Savant via `pybaseball` | No |

> **Note:** FanGraphs blocks direct HTTP requests with 403 errors. Always use `pybaseball` — it handles the necessary headers automatically.

### How probable starters are fetched (3-phase logic)

1. **Range schedule call** — one request gets all game IDs for the next 7 days
2. **Batch hydrate** — one request gets confirmed probable pitchers for all those games (`PSP_Projected = False`); these are published only ~1–2 days out
3. **Rotation-order projection** — for unannounced slots, each team's rotation is advanced through its *actual upcoming games*, one pitcher per game (`PSP_Projected = True`). Each team's recent confirmed starts reconstruct its rotation order; the longest-rested arm is "due" next, and confirmed starts re-sync the cycle. Because slots are filled game-by-game, two projected pitchers can never land on the same team/day, and turns counted by games (not calendar days) handle off-days and two-start weeks correctly. (This replaced an older `last start + 6 calendar days` guess that could double-list two projected starters on one team/day.)

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
├── dashboard.py                         # Single-viewport command dashboard (--refresh/--team/--email)
├── trade_lab.py                         # Interactive browser-only Trade Lab (--refresh/--team; JS, not emailable)
├── weekly_recap.py                      # Monday full-league recap email builder
├── bench_leakage.py                     # Standalone daily-lineup audit (my team + opponent → console)
├── backtest_projections.py              # Standalone walk-forward accuracy check of the SP proj line → console
├── CLAUDE.md                            # Actionable rules / gotchas for contributors
├── NOTES.md                             # Background & rationale ("why we did it this way")
├── requirements.txt                     # pip install -r requirements.txt
├── .env                                 # GMAIL_APP_PASSWORD — do not commit
├── .env.example                         # Safe template to share
├── .github/
│   └── workflows/
│       ├── daily-digest.yml            # Digest + dashboard attached — fires 06:00 UTC (2 AM EDT), lands ~8 AM ET
│       ├── daily-dashboard.yml         # Standalone dashboard — manual (workflow_dispatch) only; schedule retired
│       ├── weekly-recap.yml            # Recap — fires Mon 15:30 UTC
│       └── pr-check.yml                # CI: compile + dry-run render on PRs into main
├── data/
│   └── snapshot.json                    # ~1.7 MB — rebuilt every run, gitignored
├── logs/
│   └── digest.log                       # Local send history, gitignored
└── _archive/                            # Legacy files (gitignored)
    ├── dashboard.html                   # Old single-page dashboard app
    ├── digest_preview.html              # Last local dry-run preview
    └── tableau_screenshots/             # Early Tableau exploration screenshots
```
