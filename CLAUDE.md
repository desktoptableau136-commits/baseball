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
- ESPN season stats (`ESPN_SV`, `ESPN_K`, `ESPN_W`, `ESPN_IP`, `ESPN_GS`, `ESPN_GP`, `ESPN_SVHD`) are stored on **all dataset rows** in the snapshot so send_digest.py can use season counts for players who only appear in short-range FantasyPros datasets. `ESPN_SVHD`/`ESPN_SV`/`ESPN_HLD` override `SVHD`/`SV`/`HLD` on `Dataset==YEAR` rows; `ESPN_HLD` is then dropped but `ESPN_SV` is kept on all rows (it's the only way `save_role_watch` can tell a real closer from a holds-only reliever for players outside the FP top-300, who have no YEAR row). Use `_n(r.get("ESPN_SVHD")) or _n(r.get("SVHD"))` (not `if >= 0`) for the fallback — `_n` floors negatives to 0 so `>= 0` is always true.
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

**Weekly matchup is Monday–Sunday:** `week_end_str` is computed as the Sunday of the current week (`today + timedelta(days=6 - today.weekday())`). FA SP sections and My Upcoming Starts show all starts including next week, but dates past Sunday get a `NEXT WK` badge. The KPI "Starts This Week" and Week at a Glance bullet 3 count/recommend only within the current matchup week — except on Sundays (see below).

**Two-start pitchers (`PSP_Dates`):** `fetch_data.py` now preserves a list of ALL upcoming start dates per pitcher (`PSP_Dates`, plus parallel `PSP_HomeVAways`) via `_attach_start_lists` before the one-row-per-pitcher dedup. The scalar `PSP_Date`/`PSP_HomeVAway`/`PSP_Projected` remain the earliest start (unchanged for existing consumers). `_starts_this_week(r, today, week_end)` in send_digest counts entries within the matchup week (falls back to the scalar `PSP_Date` for old snapshots). A pitcher with ≥ 2 starts Mon–Sun gets a bold green `2-START` chip (`two_start_badge()`) in FA SP + My Upcoming Starts, and is preferred (secondary sort key, NOT a score change) in the Week-at-a-Glance best-FA-SP bullet, which appends "×2 starts this week". Note: two-start weeks are only visible when both starts fall in the window — mid-week runs usually show 0 because the +6 rotation pushes the 2nd start into next week; the signal lights up on Mon/Tue runs. Never fold two-start into the 0–100 score (keeps scores normalized).

**Save-Role Watch (`save_role_watch`):** SVHD is the most volatile category. **Recent holds are NOT available anywhere in the pipeline** — per-window (`Dataset` 7/15/30) `SVHD` captures recent SAVES only (FantasyPros windows have no HLD), and ESPN exposes only season totals (no rolling 15-day split — verified: `pl.stats` has keys 0/98/99, none a usable window). So recency is save-only. The function flags (a) **emerging FA closers** — FA RP with ≥ 3 saves in the last 15 days — and (b) **fading rostered closers** — my RP gated on **season saves `ESPN_SV` ≥ 5** (a real closer) with 0 recent saves despite pitching (≥ 3 recent appearances). The fading side is gated on season *saves*, not SV+H, specifically so a holds-based reliever (e.g. JoJo Romero: 0 SV / 20 HLD — whose recent hold production we can't see) is never falsely flagged as fading. Rendered as a callout appended to the FA Relief Pitchers section.

**Category classification (`classify_categories`):** Returns `{cat: (proj_res, tier)}` reusing Category Pulse's projection math (`_project` + `pit_proj` for K/QS/W). Tiers from margin vs `_CLOSE_THRESH`: `locked` (clinched win or dead loss), `tossup` (≤ thresh), else `leaning`. **The lock bar is time-aware:** `lock_mult = 2.5 + 5×remaining_frac` (Sun ≈ 2.5×thresh, Wed ≈ 5.4×, Mon ≈ 6.8×) and nothing locks before Day 2 (`can_lock = days_elapsed >= 2`) — so a mid-week gap doesn't get called a lock just because it's currently large. Same margin logic in both the projection and no-projection (fallback) branches. Computed once in `build_email` as `category_classification`, passed to (1) `build_category_pulse` for the 🔒 corner badge on locked cards and (2) `_roster_suggestion`, which drops `locked`-loss categories from the pickup target set so streaming chases only still-winnable cats.

**HR% column (`_hrp_cell`):** `HR_Probability` (computed in `fetch_data.compute_hr_probability` from barrel%, hard-hit%, launch angle, HR/AB, xwOBA, ISO, recent HR streak; range ≈ 0.05–0.31, a modeled per-game HR probability) is surfaced as a color-coded `HR%` column in Roster Hot/Cold and FA Hitters via `_hrp_cell`. Green ≥ 20%, yellow ≥ 14%. Already fed into `hitter_score`; this just displays it. HR is heavily weighted in this league, so it's a headline hitter metric.

**Hot/Cold Score columns:** Both `build_pitcher_hot_cold_section` and `build_hot_cold_section` take a `best_recent_*` index and render a role-aware Score badge (pitcher → `_score_p`, hitter → `_blend(hitter_score)`) — same normalized number shown everywhere else for that player.

**Ratio-stat risk guardrail:** In `_roster_suggestion`, when the chosen add is an SP (`_is_sp`) and ERA or WHIP is a currently-won, non-locked category, and the candidate's ERA > 4.20 / WHIP > 1.30, the pickup bullet appends a yellow `⚠ boosts K/W/QS but his {ERA} {cat} over ~{IP} IP risks your thin {cat} lead.` IP is `IP_per_G × _starts_this_week`, formatted via `_fmt_ip`. A good-ERA streamer correctly produces no warning.

**Sunday mode (`is_sunday`):** When `datetime.now().weekday() == 6`, the digest shifts to a next-week preview. Changes: header subtitle → "Weekly Lookahead"; email subject → "Lookahead"; KPI tile → "Starts Next Week" (counts starts after `week_end_str`); Category Pulse subtitle → "Final stretch — week ends today"; Week at a Glance box → "Next Week Preview" label; bullet 1 appends "— final" instead of "through Day N"; bullet 2 shows next-week confirmed starts; bullet 3 shows best FA SP for next week. `next_week_end_str` is `today + timedelta(days=13 - today.weekday())` and is available in `build_email` scope.

**SP/RP role detection uses `_is_sp(r)`:** Never use `"SP" in pos` or `gs > 3` alone. The helper uses a priority chain: ESPN season GS/GP ratio (≥ 5 appearances) → dataset GS/G ratio (≥ 4 appearances) → IP/G → Position field. Thresholds: GS/G ≥ 0.80 → SP, ≤ 0.20 → RP; IP/G ≥ 4.5 → SP, < 2.5 → RP. All SP/RP-sensitive functions use it: `pitcher_score`, `_score_p`, `fa_starters`, `fa_relievers`, My RP filter, `positional_breakdown`.

**Unified role scores — a player shows the SAME score in every section.** Three canonical role scores, all calibrated to the shared 0–100 scale (p50→50, p90→80): SP → `_score_p` (blended `pitcher_score`), RP → `rp_score` (never blended — built on ESPN season counting stats, and skipping the blend keeps the number identical across My RP, FA RP, and Positional Breakdown), Hitter → `_blend(r, hitter_score, best_recent_h)`. Never score a section with a different function than the others use for the same role — that's how Ashby showed 72 in one table and 58 in another. `sp_fa_score` (pitcher_score + hidden start bonus) was removed for this reason; the FA SP Score column now equals the My Upcoming Starts badge for the same pitcher.

**Week at a Glance pickup bullet (bullet 4):** Shows positions for both the add and the drop (via `_pos_disp`, which hides the generic `P` tag). Drop selection is position-aware: weakest droppable player sharing a `POS_GROUPS` group with the add first (adding an OF drops the worst OF, not an infielder), then same player type (pit/hit), then any droppable. `_can_drop` still guards that every position keeps ≥ 1 healthy player.

**FA RP requires SVHD ≥ 1:** `fa_relievers` gates on `(_n(r.get("ESPN_SVHD")) or _n(r.get("SVHD"))) >= 1`. A pitcher with zero saves and zero holds all season has no role and should not be recommended.

**Score cascade (`best_recent_p` / `best_recent_h`):** Built in `build_email` by merging `{**rec_p_fp, **p7, **p15, **p30}` (pitchers) and `{**rec_h, **h7, **h15, **h30}` (hitters) — later dicts win, so 30d FP > 15d FP > 7d FP > Baseball Ref. `rec_p_fp` is `recent_pitching` with computed `K/IP = K/IP` and `IP_per_G = IP/G` added so `pitcher_score` can use it. These dicts are passed to `_blend` and `positional_breakdown`. Coverage: ~500 pitchers / ~460 hitters vs 300 from 30d alone.

**`positional_breakdown` viable filter:** FA pool for each position excludes benchies. SP: `GS >= 3`. RP: `ESPN_GP >= 12 or IP >= 20`. Hitters: `OPS > 0.200 or R+RBI > 5`. FA quality (`fa_quality`) = avg blended score of top-3 viable FAs. Scarcity thresholds: `< 50` → scarce (RED), `< 60` → moderate (YELLOW), `>= 60` → deep (MUTED).

**Category Pulse `days_elapsed`:** `days_elapsed = datetime.now().weekday()` (Mon=0 … Sun=6). ESPN stats are through *yesterday*, so today is always remaining — do not add 1. Guard: `day_clause = f' through Day {days_elapsed}' if days_elapsed > 0 else ' (week starting)'`.

**Category Pulse pitcher projections (K, QS, W):** These three use actual remaining starts × per-start rate instead of historical weekly averages. Computed in `build_email` as `pit_proj = {"QS": {"my": ..., "opp": ...}, "K": ..., "W": ...}` from pitchers with `PSP_Date >= today and <= week_end_str`. Passed to `build_category_pulse(remaining_proj=pit_proj)`. Rate/hitter cats (ERA, WHIP, OPS, R, HR, RBI, SB, B_SO, SVHD) still use historical averages via `compute_weekly_avgs`.

**Dry-run preview filenames:** Always `previews/digest_preview_{team_slug}.html` (e.g. `digest_preview_Guerrero_Warfare.html`, `digest_preview_Giga_Vlad.html`). The old `digest_preview.html` fallback is gone — always slug-based regardless of whether `--team` is passed.

## Scoring functions (send_digest.py)

- `_is_sp(r)` → bool. Usage-based SP/RP detection. Priority: ESPN season GS/GP → dataset GS/G → IP/G → Position field. See gotcha above.
- `_blend(r, score_fn, idx_recent, w=0.4)` → blended score. 40% recent (best available window) + 60% season. `idx_recent` is `best_recent_p` or `best_recent_h` (see below). Falls back to `score_fn(r)` if player has no recent row.
- `_score_p(r, idx_recent=None)` → canonical role-aware pitcher score. SP → `_blend(r, pitcher_score, idx_recent)`; RP → `rp_score(r)` unblended. Used by every pitcher Score display/sort: FA SP (`fa_starters`), My Upcoming Starts badge, `positional_breakdown`, and Week at a Glance add/drop pools. See "Unified role scores" gotcha.
- `_starts_this_week(r, today, week_end)` → int. Count of the pitcher's upcoming starts within the matchup week (from `PSP_Dates`; falls back to scalar `PSP_Date`). Drives the `2-START` badge and best-FA-SP preference. See "Two-start pitchers" gotcha.
- `save_role_watch(pitchers, my_team, claimed)` → `(emerging, fading)` lists for the Save-Role Watch callout. See gotcha.
- `classify_categories(matchup, weekly_avgs, days_elapsed, remaining_proj)` → `{cat: (proj_res, tier)}`. Powers 🔒 lock badges + pickup steering. See gotcha.
- `opponent_week_intel(pitchers, hitters, opp_team, best_recent_h, today, week_end)` → dict (starts, two-start pitchers, hot hitters) for the Opponent This Week block. Returns None when `opp_team` is empty.
- `pitcher_score(r)` → 0–100. Role-aware via `_is_sp(r)`. **SP path**: role bonus 9–12 based on GS volume; SVHD ignored. **RP path**: role bonus 5–12 scaled by SVHD + W (up to 6pts) + IP/G opportunity (up to 5pts). K component uses WhiffPct if available, else Kpct_P, else K/IP. ERA component prefers xFIP over ERA. **Small-sample penalty**: `s *= min(1.0, ip / 20)` applied before calibration — suppresses sub-20-IP samples. Calibrated to p50=50, p90=80: `s * 1.875 - 67.6`.
- `rp_score(r)` → 0–100 composite for RP ranking. Weights (raw, max 100): SVHD (40pts) · K (22pts) · W (13pts) · IP/G (10pts) · ERA (9pts) · WHIP (6pts). Uses `ESPN_SVHD`/`ESPN_K`/`ESPN_W` with FantasyPros fallback. Calibrated to the shared scale: `s * 0.9464 + 16.5` (p50→50, p90→80, from the league RP distribution — elite closers land 84–97). Used by FA RP, My Relief Pitchers, and (via `_score_p`) every other section that scores an RP. My Relief Pitchers picks the best available dataset per player (YEAR → 30 → 15 → 7) so recently called-up RPs outside FantasyPros' season top-300 still appear.
- `hitter_score(r)` → 0–100. Prefers wRC+ over OPS. Uses xwOBA, sprint speed, Barrel%, ISO, HR_Probability. Calibrated: `s * 1.587 - 5.2`. Displayed everywhere as `_blend(r, hitter_score, best_recent_h)` — `fa_hitters` takes `idx_recent` for this.
- `qs_probability(r)` → 1–99. Calibrated to league-average ~38%, ace ~75%. Uses IP/G (not IP/GS).
- `_fmt_ip(ip_decimal)` → baseball IP string. Converts true decimal (5.333) to notation (5.1). Formula: `whole = int(d); outs = round((d-whole)*3); if outs>=3: whole+=1, outs=0`. Used in Proj. Line display for both FA SP and My Upcoming Starts.
- `_proj_line_html(r)` → `IP · ER · K` span. ER is adjusted for opponent strength and park: `raw_er * opp_factor * park_factor`. `opp_factor = clamp(opp_ops / 0.717, 0.80, 1.20)` where `_LEAGUE_AVG_OPS = 0.717`. `park_factor = 0.97` if `PSP_HomeVAway` starts with `"vs "` (home), `1.03` if `"@ "` (away), else `1.0`. K is not adjusted (no team K% in snapshot). Both fields are already on the pitcher row.
- `hot_cold_cell(season_val, recent_val, ..., no_data_title=None)` → `<td>` with colored recent stat + 🔥/↑/❄/↓ icon vs season baseline. When recent_val is missing/zero and `no_data_title` is set, renders `—` with a dotted underline and hover tooltip explaining the absence.
- `band_divider(label, color)` → full-width `<div>` with centered label between `BORDER` lines. Used at band boundaries in final assembly.

## Key data fields

**Pitchers:** `PlayerName`, `FantasyTeam`, `Position`, `Dataset` (7/15/30/2026), `IP`, `K`, `ERA`, `WHIP`, `GS`, `SVHD`, `K/IP`, `Kpct_P`, `IP_per_G`, `PSP_Date`, `PSP_HomeVAway`, `PSP_Projected`, `PSP_Dates` (list of all upcoming starts), `PSP_HomeVAways` (parallel list), `Team_OPS_Value`, `BarrelPctAllowed`, `ESPN_SV`, `ESPN_K`, `ESPN_W`, `ESPN_IP`, `ESPN_GS`, `ESPN_GP`, `ESPN_SVHD`

**Hitters:** `PlayerName`, `FantasyTeam`, `Position`, `Dataset`, `HR`, `RBI`, `R`, `SB`, `AVG`, `OPS`, `wRCplus`, `xwOBA`, `xBA`, `xSLG`, `SprintSpeed`, `ISO`, `Barrel_Pct`, `HardHit_Pct`, `HR_Probability`

**Roto:** `Team`, `Week`, `Roto_Score`, `{CAT}_Points` for each of 12 categories

Numeric missing values are stored as `-1` (not `NaN`) after the merge pipelines run.

## Digest section order (send_digest.py body_parts)

Five bands separated by full-width `band_divider()` rules (centered label between `BORDER` lines). The Triage divider only renders when `alert_section` is non-empty.

**Jump-to nav (`nav_bar`)** — a pill bar at the very top of the body (`My Roster · Free Agents · Season`) with anchor links to the `band_divider(..., anchor=...)` targets (`band-myroster`, `band-fa`, `band-season`). Anchors are `<a name= id=>` for max email-client support; they jump in the browser-rendered attachment and degrade to harmless styled links inline where Gmail ignores fragment jumps. This is the deliberate substitute for real tabs, which need JS/CSS that Gmail strips.

**⚑ ALERTS** (conditional)
1. Roster Alerts

**MY ROSTER**
2. Week at a Glance
3. Category Pulse (projection cards)
4. Current Matchup — category rankings (renamed from "This Week's Category Rankings"; sits above the score banner)
5. Matchup (score banner + category table)
5b. Opponent This Week — scouting block (`opponent_week_intel` / `opp_preview_section`): opponent's start count, two-start pitchers, top-3 hot bats by recent OPS, season roto strengths/weaknesses (top-3 / bottom-3 categories via `category_ranks`), and wire activity (count of their `FA ADDED` transactions in the recent window → very active / moderate / quiet). Logo uses `fantasy_logo()` so a dead ESPN URL falls back to an emoji avatar (raw `<img>` rendered blank). Renders only when the opponent has starters or hot hitters.

**MY ROSTER** (Positional Breakdown sits first so the biggest roster holes lead)
10. Positional Breakdown
6. My Upcoming Starts
7. My Relief Pitchers
8. Pitcher Hot/Cold (15-day vs season ERA; has a role-aware Score badge column via `_score_p`)
9. Roster Hot/Cold (hitters, 7-day vs season OPS; has HR% and a `hitter_score` Score badge column)

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

**My Upcoming Starts badges:** `2-START` (green, when `_starts_this_week ≥ 2`), QS (green) and 5K+ (yellow) badges shown next to pitcher name. QS fires at qs_probability ≥ 51%; 5K+ fires at K/IP ≥ 0.90 or K% ≥ 24% with IP/G ≥ 4.5.

## Color palette

```python
BG="#080e1c"  SURFACE="#101827"  SURFACE2="#0d1424"  BORDER="#1e2d45"
TEXT="#e2e8f0"  MUTED="#64748b"  ACCENT="#3b82f6"
GREEN="#22c55e"  RED="#ef4444"  YELLOW="#f59e0b"
```

My team name is always styled `font-weight:800;color:{ACCENT}` with a ← arrow.

## Automation

- **GitHub Actions:** `.github/workflows/daily-digest.yml` triggers at 06:00 and 15:00 UTC (2 AM / 11 AM EDT). GitHub's scheduler is unreliable — actual delays vary 1–4 hours, so expected delivery is roughly 4–6 AM / 1–3 PM EDT. **Cron is always UTC** — no GitHub account or org timezone setting affects it. ESPN credentials are stored as repo secrets (`ESPN_SWID`, `ESPN_S2`).
- **Local runner:** `scripts/run_digest.bat` can be used for manual local runs (logs to `logs/digest.log`).
