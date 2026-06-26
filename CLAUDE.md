# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Full run: refresh data (~60s) then send email
python send_digest.py

# Skip data refresh, use existing snapshot (fast — for email-only changes)
python send_digest.py --no-refresh

# Save HTML to digest_preview.html without sending email
python send_digest.py --dry-run

# Instant preview with no network calls
python send_digest.py --dry-run --no-refresh

# Refresh data only (writes data/snapshot.json)
python fetch_data.py

# Install dependencies
pip install -r requirements.txt
```

No linter, no test suite. Verify changes by opening `digest_preview.html` in a browser.

## Setup

Copy `.env.example` → `.env` and add a Gmail App Password (not your regular password — create one at myaccount.google.com/security → App Passwords).

## Architecture

Two files; one intermediate artifact:

**`fetch_data.py`** pulls from 5+ sources and writes `data/snapshot.json`:
1. FantasyPros HTML (`pd.read_html`) — pitcher and hitter stats across 4 ranges (7/15/30/season)
2. ESPN Fantasy API (`espn_api`) — rosters, FA list, roto box scores, standings, transactions
3. MLB Stats API — probable starters (batch hydrate method) + opponent OPS
4. pybaseball — Statcast contact quality, expected stats, sprint speed, recent game logs

**`send_digest.py`** reads the snapshot, computes all derived metrics, and builds a single self-contained HTML email sent via Gmail SMTP (`smtplib`). The email has two parts: inline HTML body (may be clipped by Gmail at 102 KB) and an attached `digest_YYYY-MM-DD.html` for full render. All new features go here.

**`data/snapshot.json`** is the schema contract between the two files. It is ~1.2MB and not committed.

## Critical gotchas

**Data sources:**
- FanGraphs returns 403 — never use it directly. pybaseball functions work because they handle headers.
- `pitching_stats()` (FanGraphs leaderboard) returns 403. Use `pitching_stats_range()` instead, which scrapes Baseball Reference — but it has no `HLD` column.
- SVHD (saves+holds) is pulled from ESPN player stats via `get_pitcher_espn_svhd()` in fetch_data.py, which reads `pl.stats[0]['breakdown']` using ESPN stat IDs (SV=57, HLD=60, SVHD=83). This overwrites FantasyPros SVHD for the season dataset because FantasyPros does not reliably include holds.
- xFIP and WhiffPct are not available from FantasyPros pitcher tables. Use `BarrelPctAllowed` and `Kpct_P` (derived K%) instead.
- ESPN injury statuses are `TEN_DAY_DL`, `FIFTEEN_DAY_DL`, `SIXTY_DAY_DL` — not `IL` or `OUT`. The constant `_DL_STATUSES` in send_digest.py covers all of these. FA views and positional breakdown exclude all DL-status players.

**Team name double-space:** `MY_TEAM_NAME = "Guerrero  Warfare"` in fetch_data.py has a double space to match ESPN exactly. `MY_TEAM = "Guerrero Warfare"` in send_digest.py uses a single space for display. Never normalize these to match each other.

**Merge direction:** Pitcher and hitter merges start from FantasyPros as the left side. Players outside the FP top 300 are dropped from short-range (7-day) views but appear in longer ranges.

**PSP sentinel:** `PSP_Date = "1999-01-01"` means no upcoming start. `PSP_Projected = True` means the start was projected via the +6-day rotation rule, not confirmed by the MLB API.

**FA exclusion logic:** Players claimed today are identified by reading today's `transactions` list from the snapshot. The *most recent* transaction per player wins — so add-then-drop-same-day is handled correctly (dropped players re-appear as FA). FA views and positional breakdown replacement options exclude all `_DL_STATUSES` players.

**B_SO is lower-is-better:** `B_SO` (batter strikeouts) is in `_LOWER_BETTER` alongside ERA and WHIP. This affects the Category Pulse bar direction and projection flip logic — having fewer B_SO than the opponent is a win.

**Probable starters:** The primary method uses two MLB API calls (range schedule → batch hydrate). The +6-day rotation projection fills unannounced slots. A live-feed fallback exists if the batch returns nothing.

**Pitcher hot/cold uses 15-day ERA:** `build_pitcher_hot_cold_section`, My Upcoming Starts, and FA Starting Pitchers all compare season ERA vs 15-day ERA (from `p15` index — Dataset==15 rows). The 7-day window is too short for SPs who start infrequently. The `p15` index is built alongside `rec_p` in the main build function. Column header is "L15 ERA".

## Scoring functions (send_digest.py)

- `pitcher_score(r)` → 0–100. Role-aware: detects SP vs RP from `Position` field or `GS > 3`. **SP path**: role bonus 9–12 based on GS volume; SVHD ignored. **RP path**: role bonus 5–12 scaled by SVHD; QS/GS ignored. K component uses WhiffPct if available, else Kpct_P, else K/IP. ERA component prefers xFIP over ERA.
- `rp_score(r)` → composite for RP ranking. Weights: SVHD (40pts), K (25pts), W (15pts), ERA (12pts), WHIP (8pts). Used by both FA RP and My Relief Pitchers sections. My Relief Pitchers picks the best available dataset per player (YEAR → 30 → 15 → 7) so recently called-up RPs outside FantasyPros' season top-300 still appear.
- `hitter_score(r)` → 0–100. Prefers wRC+ over OPS. Uses xwOBA, sprint speed, Barrel%, ISO, HR_Probability.
- `sp_fa_score(r)` → pitcher_score + start bonus (scaled 8–22 by QS probability). Only applies to pitchers with GS ≥ 1 or "SP" in Position.
- `qs_probability(r)` → 1–99. Calibrated to league-average ~38%, ace ~75%. Uses IP/G (not IP/GS).

## Key data fields

**Pitchers:** `PlayerName`, `FantasyTeam`, `Position`, `Dataset` (7/15/30/2026), `IP`, `K`, `ERA`, `WHIP`, `GS`, `SVHD`, `K/IP`, `Kpct_P`, `IP_per_G`, `PSP_Date`, `PSP_HomeVAway`, `PSP_Projected`, `Team_OPS_Value`, `BarrelPctAllowed`

**Hitters:** `PlayerName`, `FantasyTeam`, `Position`, `Dataset`, `HR`, `RBI`, `R`, `SB`, `AVG`, `OPS`, `wRCplus`, `xwOBA`, `xBA`, `xSLG`, `SprintSpeed`, `ISO`, `Barrel_Pct`, `HardHit_Pct`, `HR_Probability`

**Roto:** `Team`, `Week`, `Roto_Score`, `{CAT}_Points` for each of 12 categories

Numeric missing values are stored as `-1` (not `NaN`) after the merge pipelines run.

## Digest section order (send_digest.py body_parts)

1. Week at a Glance
2. This Week's Category Rankings
3. Matchup (score banner + category table)
4. Category Pulse (projection cards)
5. FA Pickup — Starting Pitchers
6. FA Pickup — Relief Pitchers
7. My Upcoming Starts
8. My Relief Pitchers
9. Pitcher Hot/Cold (15-day vs season ERA)
10. Roster Hot/Cold (hitters, 7-day vs season OPS)
11. Positional Breakdown
11. Roster Alerts
12. FA Pickup — Hitters
13. League Luck Standings
14. Category Rankings (season)
15. Sparkline / trend

## Color palette

```python
BG="#080e1c"  SURFACE="#101827"  SURFACE2="#0d1424"  BORDER="#1e2d45"
TEXT="#e2e8f0"  MUTED="#64748b"  ACCENT="#3b82f6"
GREEN="#22c55e"  RED="#ef4444"  YELLOW="#f59e0b"
```

My team name is always styled `font-weight:800;color:{ACCENT}` with a ← arrow.

## Automation

- **Windows Task Scheduler:** Task "GuerreroDailyDigest" runs `run_digest.bat` daily at 7:00 AM. `WakeToRun=True`, `StartWhenAvailable=True`.
- **GitHub Actions:** `.github/workflows/daily-digest.yml` runs at 11:00 UTC (7 AM EDT). ESPN credentials are stored as repo secrets (`ESPN_SWID`, `ESPN_S2`).
