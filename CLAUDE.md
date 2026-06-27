# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Full run: refresh data (~60s) then send email
python send_digest.py

# Skip data refresh, use existing snapshot (fast — for email-only changes)
python send_digest.py --no-refresh

# Save HTML to previews/digest_preview.html without sending email
python send_digest.py --dry-run

# Instant preview with no network calls
python send_digest.py --dry-run --no-refresh

# View any team's full digest (requires a fresh snapshot with all_matchups)
python send_digest.py --dry-run --no-refresh --team "Houck Tuah"

# Refresh data only (writes data/snapshot.json)
python fetch_data.py

# Install dependencies
pip install -r requirements.txt
```

No linter, no test suite. Verify changes by opening `previews/digest_preview.html` in a browser.

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
- SVHD (saves+holds) is pulled from ESPN player stats via `get_pitcher_espn_svhd()` in fetch_data.py, which reads `pl.stats[0]['breakdown']`. The breakdown uses **string keys** (`'SV'`, `'HLD'`, `'SVHD'`, `'K'`, `'W'`, `'OUTS'`, `'ERA'`, `'WHIP'`, `'GP'`, `'GS'`) — not numeric stat IDs. This is called at fetch time for all rostered and FA pitchers.
- ESPN season stats (`ESPN_K`, `ESPN_W`, `ESPN_IP`, `ESPN_GS`, `ESPN_GP`, `ESPN_SVHD`) are stored on **all dataset rows** in the snapshot so send_digest.py can use season counts for players who only appear in short-range FantasyPros datasets. `ESPN_SVHD` overrides `SVHD` on `Dataset==YEAR` rows. Use `_n(r.get("ESPN_SVHD")) or _n(r.get("SVHD"))` (not `if >= 0`) for the fallback — `_n` floors negatives to 0 so `>= 0` is always true.
- xFIP and WhiffPct are not available from FantasyPros pitcher tables. Use `BarrelPctAllowed` and `Kpct_P` (derived K%) instead.
- ESPN injury statuses are `TEN_DAY_DL`, `FIFTEEN_DAY_DL`, `SIXTY_DAY_DL` — not `IL` or `OUT`. The constant `_DL_STATUSES` in send_digest.py covers all of these. FA views and positional breakdown exclude all DL-status players.

**Team name double-space:** `MY_TEAM_NAME = "Guerrero  Warfare"` in fetch_data.py has a double space to match ESPN exactly. `MY_TEAM = "Guerrero Warfare"` in send_digest.py uses a single space for display. Never normalize these to match each other.

**Merge direction:** Pitcher and hitter merges start from FantasyPros as the left side. Players outside the FP top 300 are dropped from short-range (7-day) views but appear in longer ranges.

**PSP sentinel:** `PSP_Date = "1999-01-01"` means no upcoming start. `PSP_Projected = True` means the start was projected via the +6-day rotation rule, not confirmed by the MLB API.

**FA exclusion logic:** Players claimed today are identified by reading today's `transactions` list from the snapshot. The *most recent* transaction per player wins — so add-then-drop-same-day is handled correctly (dropped players re-appear as FA). FA views and positional breakdown replacement options exclude all `_DL_STATUSES` players.

**B_SO is lower-is-better:** `B_SO` (batter strikeouts) is in `_LOWER_BETTER` alongside ERA and WHIP. This affects the Category Pulse bar direction and projection flip logic — having fewer B_SO than the opponent is a win.

**Category Pulse card design:**
- Tied categories use `TEXT` (#e2e8f0, white) for border/value/status — not `YELLOW`. Win = green, loss = red, tie = white.
- ⚡ (close) and flip indicators (▲▼◆) live in an `position:absolute` top-right corner badge, not inline with the status or projection text. The card div is `position:relative`.
- Flip uses `round(pm, dec)` / `round(po, dec)` (display precision) for outcome comparison — raw floats cause false flips when both round to the same displayed value.
- Flip arrow: ▲ green = projecting to flip to a win; ▼ red = projecting to flip to a loss; ◆ white = projecting to flip to a tie.
- Summary line shows W · L · T (T only appended when at least one category is tied).
- Card value (`my score` / `vs opp`) is stacked on two lines (score block + "vs X" below) so decimal-heavy stats (OPS/ERA/WHIP) don't cause width or height inconsistency across the row.

**My Season Category Rankings** (section 14) subtitle shows a pseudo-single-week roto score: `sum(n - rank + 1 for rank in cats.values())` — same scale as a weekly roto score, max = n × 12. Directly comparable to the "This Week's Category Rankings" subtitle score.

**My Upcoming Starts subheader** format: `X starts across Y days | N this wk[, N next wk]`. The "this wk" count is red when 0. The ", N next wk" segment is omitted entirely when next-week count is 0.

**Probable starters:** The primary method uses two MLB API calls (range schedule → batch hydrate). The +6-day rotation projection fills unannounced slots. A live-feed fallback exists if the batch returns nothing.

**Pitcher hot/cold uses 15-day ERA:** `build_pitcher_hot_cold_section`, My Upcoming Starts, and FA Starting Pitchers all compare season ERA vs 15-day ERA (from `p15` index — Dataset==15 rows). The 7-day window is too short for SPs who start infrequently. When a player is absent from the FP 15-day top 300 (fringe starters like Davis Martin), the code falls back to `rec_p` — the pybaseball Baseball Reference 15-day scrape stored in `recent_pitching`. `p15` and `rec_p` are both built in the main build function. Column header is "L15 ERA". `fetch_recent_pitcher_stats` fetches 15 days (not 7) to match this window.

**Statcast name matching:** `lf_to_name()` in fetch_data.py converts Baseball Savant "Last, First" names to "First Last" AND strips accents (e.g. `Ramírez, José` → `Jose Ramirez`) so the merge against FantasyPros ASCII names succeeds. Without accent stripping, accented-name players (Jose Ramirez, etc.) silently lose all Statcast data (xwOBA, Barrel%, SprintSpeed, HR_Probability).

**Weekly matchup is Monday–Sunday:** `week_end_str` is computed as the Sunday of the current week (`today + timedelta(days=6 - today.weekday())`). FA SP sections and My Upcoming Starts show all starts including next week, but dates past Sunday get a `NEXT WK` badge. The KPI "Starts This Week" and Week at a Glance bullet 3 count/recommend only within the current matchup week.

**SP/RP role detection uses `_is_sp(r)`:** Never use `"SP" in pos` or `gs > 3` alone. The helper uses a priority chain: ESPN season GS/GP ratio (≥ 5 appearances) → dataset GS/G ratio (≥ 4 appearances) → IP/G → Position field. Thresholds: GS/G ≥ 0.80 → SP, ≤ 0.20 → RP; IP/G ≥ 4.5 → SP, < 2.5 → RP. All four SP/RP-sensitive functions use it: `pitcher_score`, `sp_fa_score`, `fa_relievers`, My RP filter, `positional_breakdown`.

**FA RP requires SVHD ≥ 1:** `fa_relievers` gates on `(_n(r.get("ESPN_SVHD")) or _n(r.get("SVHD"))) >= 1`. A pitcher with zero saves and zero holds all season has no role and should not be recommended.

## Scoring functions (send_digest.py)

- `_is_sp(r)` → bool. Usage-based SP/RP detection. Priority: ESPN season GS/GP → dataset GS/G → IP/G → Position field. See gotcha above.
- `pitcher_score(r)` → 0–100. Role-aware via `_is_sp(r)`. **SP path**: role bonus 9–12 based on GS volume; SVHD ignored. **RP path**: role bonus 5–12 scaled by SVHD; QS/GS ignored. K component uses WhiffPct if available, else Kpct_P, else K/IP. ERA component prefers xFIP over ERA.
- `rp_score(r)` → composite for RP ranking. Weights: SVHD (40pts), K (25pts), W (15pts), ERA (12pts), WHIP (8pts). Uses `ESPN_SVHD`/`ESPN_K`/`ESPN_W` with FantasyPros fallback. Used by both FA RP and My Relief Pitchers sections. My Relief Pitchers picks the best available dataset per player (YEAR → 30 → 15 → 7) so recently called-up RPs outside FantasyPros' season top-300 still appear.
- `hitter_score(r)` → 0–100. Prefers wRC+ over OPS. Uses xwOBA, sprint speed, Barrel%, ISO, HR_Probability.
- `sp_fa_score(r)` → pitcher_score + start bonus (scaled 8–22 by QS probability). Returns 0 if `not _is_sp(r)`.
- `qs_probability(r)` → 1–99. Calibrated to league-average ~38%, ace ~75%. Uses IP/G (not IP/GS).
- `_fmt_ip(ip_decimal)` → baseball IP string. Converts true decimal (5.333) to notation (5.1). Formula: `whole = int(d); outs = round((d-whole)*3); if outs>=3: whole+=1, outs=0`. Used in Proj. Line display for both FA SP and My Upcoming Starts.
- `hot_cold_cell(season_val, recent_val, ..., no_data_title=None)` → `<td>` with colored recent stat + 🔥/↑/❄/↓ icon vs season baseline. When recent_val is missing/zero and `no_data_title` is set, renders `—` with a dotted underline and hover tooltip explaining the absence.
- `band_divider(label, color)` → full-width `<div>` with centered label between `BORDER` lines. Used at band boundaries in final assembly.

## Key data fields

**Pitchers:** `PlayerName`, `FantasyTeam`, `Position`, `Dataset` (7/15/30/2026), `IP`, `K`, `ERA`, `WHIP`, `GS`, `SVHD`, `K/IP`, `Kpct_P`, `IP_per_G`, `PSP_Date`, `PSP_HomeVAway`, `PSP_Projected`, `Team_OPS_Value`, `BarrelPctAllowed`, `ESPN_K`, `ESPN_W`, `ESPN_IP`, `ESPN_GS`, `ESPN_GP`, `ESPN_SVHD`

**Hitters:** `PlayerName`, `FantasyTeam`, `Position`, `Dataset`, `HR`, `RBI`, `R`, `SB`, `AVG`, `OPS`, `wRCplus`, `xwOBA`, `xBA`, `xSLG`, `SprintSpeed`, `ISO`, `Barrel_Pct`, `HardHit_Pct`, `HR_Probability`

**Roto:** `Team`, `Week`, `Roto_Score`, `{CAT}_Points` for each of 12 categories

Numeric missing values are stored as `-1` (not `NaN`) after the merge pipelines run.

## Digest section order (send_digest.py body_parts)

Five bands separated by full-width `band_divider()` rules (centered label between `BORDER` lines). The Triage divider only renders when `alert_section` is non-empty.

**⚑ ALERTS** (conditional)
1. Roster Alerts

**MY ROSTER**
2. Week at a Glance
3. Category Pulse (projection cards)
4. Current Matchup — category rankings (renamed from "This Week's Category Rankings"; sits above the score banner)
5. Matchup (score banner + category table)

**MY ROSTER**
6. My Upcoming Starts
7. My Relief Pitchers
8. Pitcher Hot/Cold (15-day vs season ERA)
9. Roster Hot/Cold (hitters, 7-day vs season OPS)
10. Positional Breakdown

**FREE AGENTS**
11. FA Pickup — Starting Pitchers
12. FA Pickup — Relief Pitchers
13. FA Pickup — Hitters

**SEASON**
14. My Season Category Rankings
15. League Luck Standings

**FA Starting Pitchers table columns:** Pitcher · Proj. Line · Matchup · Opp OPS · QS% · ERA · L15 ERA · K% · Score. "Pos" was removed (redundant for SPs). "Proj. Line" shows projected `IP · ER · K` per start, with IP in baseball notation via `_fmt_ip()` (decimal 5.333 → "5.1", 5.667 → "5.2"). Date header rows span `colspan="9"` with background on `<tr>` (not `<td>`) for full-width highlight.

**My Upcoming Starts table columns:** Pitcher · Proj. Line · Matchup · Opp OPS · QS% · ERA · L15 ERA · K% · Score. Same Proj. Line formula as FA SP. Date header rows span `colspan="9"`.

**`--team` flag:** `python send_digest.py --team "Team Name"` shows a full digest from another team's perspective. All sections render correctly including Category Pulse and Matchup score banner. Requires a fresh snapshot (run `fetch_data.py` first) since `all_matchups` must be present. Falls back to `current_matchup` (Guerrero Warfare only) for old snapshots. `build_matchup_section` accepts `my_team` param (default `MY_TEAM` constant) so it renders the correct team name and logo.

**My Upcoming Starts badges:** QS (green) and 5K+ (yellow) badges always shown next to pitcher name. QS fires at qs_probability ≥ 51%; 5K+ fires at K/IP ≥ 0.90 or K% ≥ 24% with IP/G ≥ 4.5.

## Color palette

```python
BG="#080e1c"  SURFACE="#101827"  SURFACE2="#0d1424"  BORDER="#1e2d45"
TEXT="#e2e8f0"  MUTED="#64748b"  ACCENT="#3b82f6"
GREEN="#22c55e"  RED="#ef4444"  YELLOW="#f59e0b"
```

My team name is always styled `font-weight:800;color:{ACCENT}` with a ← arrow.

## Automation

- **GitHub Actions:** `.github/workflows/daily-digest.yml` runs at 11:00 UTC (7 AM EDT). ESPN credentials are stored as repo secrets (`ESPN_SWID`, `ESPN_S2`).
- **Local runner:** `scripts/run_digest.bat` can be used for manual local runs (logs to `logs/digest.log`).
