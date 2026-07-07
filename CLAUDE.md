# CLAUDE.md

Guidance for Claude Code when working in this repo. Actionable rules only — background/rationale and completed-work forensics live in `NOTES.md`.

## Commands

```bash
python send_digest.py                          # full run: refresh data (~60s) then send email
python send_digest.py --no-refresh             # use existing snapshot (fast — email-only changes)
python send_digest.py --dry-run                # save HTML to previews/, no email
python send_digest.py --dry-run --no-refresh   # instant preview, no network
python send_digest.py --dry-run --no-refresh --team "Houck Tuah"  # another team's digest (needs all_matchups)
python fetch_data.py                           # refresh data only → data/snapshot.json
python weekly_recap.py                         # Monday recap: refresh + email full-league recap
python weekly_recap.py --dry-run --no-refresh  # instant recap preview → previews/recap_week_N.html
pip install -r requirements.txt
```

No linter, no test suite. Verify by opening `previews/digest_preview_{team_slug}.html` or `previews/recap_week_N.html` in a browser.

## Setup

Copy `.env.example` → `.env` and add a Gmail App Password (myaccount.google.com/security → App Passwords).

## Architecture

Three files, one intermediate artifact. The daily digest and Monday recap are independent scripts that both read the same snapshot:

**`fetch_data.py`** pulls from 5+ sources → `data/snapshot.json`:
1. FantasyPros HTML (`pd.read_html`) — pitcher/hitter stats across 4 ranges (7/15/30/season)
2. ESPN Fantasy API (`espn_api`) — rosters, FA list, roto box scores, standings, transactions
3. MLB Stats API — probable starters (batch hydrate) + opponent OPS
4. pybaseball — Statcast contact quality, expected stats, sprint speed, recent game logs

**`send_digest.py`** reads the snapshot, computes all derived metrics, builds one self-contained HTML email via Gmail SMTP. Two parts: inline HTML body (Gmail may clip at 102 KB) + an attached `digest_YYYY-MM-DD.html` for full render. All new features go here.

**`weekly_recap.py`** reads the same snapshot on Mondays, builds a full-league recap HTML email: Week N Highlights · My Matchup (full 12-cat table) · League Scoreboard (all 6 matchups) · Weekly Roto Rankings · Top Performers (rostered + hot FAs) · Standings & Luck · Season Trajectory. Does NOT import from `send_digest.py` — copies the ~100 lines of constants/helpers it needs. Output: `previews/recap_week_N.html`. GitHub Actions: `.github/workflows/weekly-recap.yml` (Monday 15:30 UTC).

**`build_commissioner_story`** (`weekly_recap.py`) — Week N Highlights section at the top of the recap. Two-column layout: commissioner-style prose (left, 60%) + compact stat sidebar (right, 40%). Finds (a) weekly roto winner (top roto score that week), (b) hitter of week (rostered, best OPS, min 10 AB from `prev_week_hitting`), (c) pitcher of week (rostered, best ERA, min 8 IP from `prev_week_pitching`), (d) best available FA hitter. `prev_week_hitting`/`prev_week_pitching` are snapshot fields fetched over the exact previous matchup Mon–Sun window — distinct from the rolling `recent_hitting` (7-day) and `recent_pitching` (15-day) fields used by `build_top_performers`. Prose uses named historical benchmarks (Bonds 1.422 OPS 2004, Gibson 1.12 ERA 1968, deGrom 1.70 ERA 2018, Ryan 383 K 1973, etc.). Sidebar cards show fantasy logo + MLB team logo (`_mlb_logo`, ESPN CDN) + slash line / K / QS / categories led. Roto weekly win count uses weekly top-roto-score count, NOT H2H W-L. MLB team abbreviation sourced from season hitter/pitcher rows (`Team` field, e.g. "WSH"); `_MLB_ABBREV_ESPN` maps Baseball Reference outliers (TBR→tb, KCR→kc, SFG→sf, WSN→wsh, etc.).

**`data/snapshot.json`** is the schema contract between the two files. ~1.2 MB, not committed. Numeric missing values are stored as `-1` (not `NaN`) after the merge pipelines run.

## Critical gotchas

### Data sources
- FanGraphs returns 403 — never use directly. pybaseball works (handles headers).
- `pitching_stats()` (FanGraphs leaderboard) → 403. Use `pitching_stats_range()` (Baseball Reference) — but it has no `HLD` column.
- SVHD comes from ESPN via `get_pitcher_espn_svhd()`, reading `pl.stats[0]['breakdown']`, which uses **string keys** (`'SV'`, `'HLD'`, `'SVHD'`, `'K'`, `'W'`, `'OUTS'`, `'ERA'`, `'WHIP'`, `'GP'`, `'GS'`) — not numeric IDs. Called at fetch time for all rostered + FA pitchers.
- ESPN season stats (`ESPN_SV`/`ESPN_K`/`ESPN_W`/`ESPN_IP`/`ESPN_GS`/`ESPN_GP`/`ESPN_SVHD`) are stored on **all** dataset rows so send_digest can use season counts for players who only appear in short-range FantasyPros datasets. `ESPN_SVHD`/`ESPN_SV`/`ESPN_HLD` override `SVHD`/`SV`/`HLD` on `Dataset==YEAR` rows; `ESPN_HLD` is then dropped but `ESPN_SV` stays on all rows (the only way `save_role_watch` distinguishes a real closer from a holds-only reliever for players outside the FP top-300). Use `_n(r.get("ESPN_SVHD")) or _n(r.get("SVHD"))` (not `if >= 0`) — `_n` floors negatives to 0 so `>= 0` is always true.
- xFIP / CSW are unavailable from FantasyPros (and FanGraphs — 403). But Baseball Savant (via pybaseball) supplies predictive pitcher stats, merged on `PlayerName`: `xERA` + `xwOBA_against` (`get_savant_pitcher_expected`, absolute values), `WhiffPctile` (a league whiff **percentile** 0–100, not a rate, from `get_savant_pitcher_skill`), and `BarrelPctAllowed`/`HardHitPctAllowed`/`AvgEVAllowed` (`get_savant_pitcher_contact`). Coverage ≈ 90–99% of the FP top-300; missing rows fall back to raw ERA/K% cleanly.
- **`WhiffPct` (raw overall whiff %, DISPLAY-ONLY)** — via `get_savant_pitcher_whiff` (pybaseball `statcast_pitcher_arsenal_stats`), pitch-type rows aggregated by pitches-weighting per `player_id`. A raw 0–100 **rate**, DISTINCT from `WhiffPctile` (a 0–100 **percentile**). Merged on `PlayerName` via `merge_on_name`. **Never feed it into `pitcher_score`/`rp_score`** — `WhiffPctile` already drives the K component, so raw whiff% would double-count. Displayed only: a muted `whiff NN%` subline under the K% cell in the compacted My Upcoming Starts + FA SP tables (`_whiff_sub`, keeps them at 8 cols), and a real **Whiff%** column (green ≥ 30) in Pitcher Hot/Cold (7 cols; `score_reveal` colspan 7).
- ESPN injury statuses are `TEN_DAY_DL`/`FIFTEEN_DAY_DL`/`SIXTY_DAY_DL` — not `IL`/`OUT`. Constant `_DL_STATUSES` covers all. FA views and positional breakdown exclude all DL-status players.

### Names & merges
- **Team name double-space:** `MY_TEAM_NAME = "Guerrero  Warfare"` (double space) in fetch_data.py matches ESPN exactly. `MY_TEAM = "Guerrero Warfare"` (single space) in send_digest is for display. Never normalize these to match each other.
- **Merge direction:** pitcher/hitter merges start from FantasyPros (left side). Players outside the FP top 300 drop from short-range (7-day) views but appear in longer ranges. FantasyPros↔FantasyPros merges (`fp7`, season→all-rows enrich) stay exact.
- **`merge_on_name` / `_name_key`:** ESPN and FantasyPros names differ by accents and generational suffixes (`Luis García Jr.` vs `Luis Garcia`); an exact-string merge drops the roster link so a rostered player wrongly shows as a free agent. `merge_on_name` does the exact merge first, then fills still-unmatched rows via `_name_key` (accent-stripped, lowercased, trailing Jr./Sr./II–V + punctuation removed). Invariants: (a) exact matches always win — the fallback only fills NaNs; (b) a key is trusted only when it maps to a single player on **both** sides (the several MLB "Luis Garcia" pitchers stay ambiguous, never guessed). Wired into both roster+FA merges AND the Statcast/pybaseball merges. Older per-player `HITTER_NAME_PATCHES`/`PITCHER_NAME_PATCHES` still applied.
- **Index-alignment invariant:** build the fill-loop keys `fkeys` from `merged["PlayerName"]`, NOT `fp["PlayerName"]` — `fp.merge()` resets to a clean RangeIndex, so keys from `fp` can be unalignable with the `missing` mask from `merged` → `IndexingError: Unalignable boolean Series`.
- **Statcast name matching:** `lf_to_name()` converts Baseball Savant "Last, First" → "First Last" AND strips accents (`Ramírez, José` → `Jose Ramirez`) so the merge against ASCII FantasyPros names succeeds. Without it, accented-name players silently lose all Statcast data.

### Fields & sentinels
- **PSP sentinel:** `PSP_Date = "1999-01-01"` = no upcoming start. `PSP_Projected = True` = start projected via the +6-day rotation rule, not confirmed by the MLB API.
- **Two-start pitchers (`PSP_Dates`):** fetch_data preserves a list of ALL upcoming start dates per pitcher (`PSP_Dates` + parallel `PSP_HomeVAways`) via `_attach_start_lists` before the one-row-per-pitcher dedup. Scalar `PSP_Date`/`PSP_HomeVAway`/`PSP_Projected` remain the earliest start. `_starts_this_week(r, today, week_end)` counts entries within the matchup week. ≥ 2 starts Mon–Sun → bold **purple** `2-START` chip (`two_start_badge()`) in FA SP + My Upcoming Starts, and preferred (secondary sort key, NOT a score change) in the Week-at-a-Glance best-FA-SP bullet ("×2 starts this week"). Never fold two-start into the 0–100 score.
- **B_SO is lower-is-better:** batter strikeouts sit in `_LOWER_BETTER` alongside ERA/WHIP. Affects Category Pulse bar direction and projection flip logic — fewer B_SO than the opponent is a win.
- **`ESPN_OnIL`** (native python bool = `pl.lineupSlot == "IL" or pl.injured`) is captured in `get_pitcher_roster`/`get_hitter_roster` and broadcast to all rows. `lineupSlot` is the primary check (slot 17 in ESPN's POSITION_MAP); `pl.injured` is a fallback because ESPN's API sometimes omits `lineupSlotId` from the roster entry (e.g. Will Smith on the 60-day IL), leaving `lineupSlot = ''`. Keep it a **native bool**, NOT `.astype(bool)` → numpy `bool_`, which `json.dump(default=str)` stringifies to the truthy `"False"`.

### FA logic
- **FA exclusion:** players claimed today are found via today's `transactions` list; the *most recent* transaction per player wins. FA views + positional breakdown exclude all `_DL_STATUSES`.
- **FA RP requires SVHD ≥ 1:** `fa_relievers` gates on `(_n(r.get("ESPN_SVHD")) or _n(r.get("SVHD"))) >= 1`. Zero saves + zero holds all season = no role.
- **FA "Cats" column (`_cats_cell` / `player_cat_strengths` / `build_cat_percentiles`):** FA Hitters + FA Relievers show a **Cats** column (before Score) listing up to 3 roto cats the player is strong in (percentile ≥ 0.70 within a qualified YEAR pool; `_LOWER_BETTER` inverted). Cats in `need_cats` (my currently-losing ∪ tossup) render `ACCENT`; others `MUTED`. `_FA_HIT_CATS=[R,HR,RBI,SB,OPS]`, `_FA_RP_CATS=[SVHD,K,W,ERA,WHIP]`.

### Roster/drop rules
- **Never drop an IL-slot player (`_on_il`):** the league has **2 dedicated IL slots** that don't consume active/bench room, so dropping a player parked there frees nothing. `_on_il(r)` (tolerant of a stringified `"true"`) gates two drop paths: (a) `_can_drop` returns False (Week-at-a-Glance bullet 4); (b) `positional_breakdown`'s `worst_player` uses `drop_pool = [r for r in my_p if not _on_il(r)] or my_p`. It is lineup-SLOT-specific, NOT injury status — a DL player on the BENCH is `False` and stays droppable (cutting him frees a real bench spot). Populated for all teams; only my rows affect my suggestions.
- **`_can_drop`** still guards that every position keeps ≥ 1 healthy player.

### SP/RP role detection (`_is_sp(r)`)
Never use `"SP" in pos` or `gs > 3` alone. Priority chain: ESPN season GS/GP ratio (≥ 5 appearances) → dataset GS/G ratio (≥ 4 appearances) → IP/G → Position field. Thresholds: GS/G ≥ 0.80 → SP, ≤ 0.20 → RP; IP/G ≥ 4.5 → SP, < 2.5 → RP. Used by `pitcher_score`, `_score_p`, `fa_starters`, `fa_relievers`, My RP filter, `positional_breakdown`.

### Pitcher hot/cold uses 15-day ERA
`build_pitcher_hot_cold_section`, My Upcoming Starts, and FA Starting Pitchers compare season ERA vs 15-day ERA (`p15` = Dataset==15 rows). The 7-day window is too short for infrequent SPs. When a player is absent from the FP 15-day top 300 (fringe starters), fall back to `rec_p` (pybaseball Baseball Reference 15-day scrape in `recent_pitching`). Column header "L15 ERA". `fetch_recent_pitcher_stats` fetches 15 days to match. Both `fetch_recent_pitcher_stats` and `fetch_recent_hitter_stats` accept optional `start_dt`/`end_dt` string params for an exact date window (used to populate `prev_week_hitting`/`prev_week_pitching`).

### Save-Role Watch (`save_role_watch`)
SVHD is the most volatile category, and **recent holds are unavailable anywhere in the pipeline** — per-window `SVHD` captures recent SAVES only (FP windows have no HLD), ESPN exposes only season totals. So recency is save-only. Flags (a) **emerging FA closers** — FA RP with ≥ 3 saves in last 15 days — and (b) **fading rostered closers** — my RP gated on **season saves `ESPN_SV` ≥ 5** (a real closer) with 0 recent saves despite ≥ 3 recent appearances. The fading side is gated on season *saves*, not SV+H, so a holds-based reliever (e.g. JoJo Romero 0 SV / 20 HLD) is never falsely flagged. Rendered as a callout on the FA Relief Pitchers section.

### Category classification (`classify_categories`)
Returns `{cat: (proj_res, tier)}` reusing Category Pulse's projection math (`_project` + `pit_proj` for K/QS/W). Tier is only `tossup` (margin ≤ `_CLOSE_THRESH`) or `leaning` — **no `locked` tier / no 🔒 badge**. Used to detect a THIN ERA/WHIP lead for the ratio-stat pickup warning. Computed once in `build_email` as `category_classification`, passed to `_roster_suggestion`; the pickup steering targets all losing cats (no dead-cat pruning).

### Matchup W-L uses ESPN's `result` field, NOT raw comparison
`get_all_matchups`/`get_prev_matchup` read `box.home_stats[cat]["result"]` (`WIN`/`LOSS`/`TIE`) as source of truth, mapped to `W`/`L`/`T`. Critical because ESPN applies a ratio-stat **innings-pitched minimum** (~25 IP) before ERA/WHIP count — a team with the better WHIP but under the IP floor **loses** that category. **Intended consequence:** the current result can differ from the projected result (Category Pulse / `classify_categories` compare raw projected values without the IP floor). Don't unify them — the divergence is informative.

### Category Pulse
- Tied cats use `TEXT` (#e2e8f0, white) for border/value/status — not `YELLOW`. Win=green, loss=red, tie=white.
- ⚡ (toss-up) and projected-outcome markers (▲▼◆) live in a `position:absolute` top-right corner badge, not inline. Card div is `position:relative`.
- **⚡ = win-% toss-up, NOT a current-margin close.** A card gets ⚡ when `win_pct` is in the 45–55 band **or `proj_res == "T"`** (a projected tie always counts). Collected into `close_flags` → summary `⚡N close`. `_CLOSE_THRESH` is still used elsewhere (sigma fallback in `_cat_win_prob`, `classify_categories` tossup tier) — just not for the card ⚡.
- **Opponent This Week — always wrap text in an explicit color.** The panel sits on `SURFACE2` (dark); without an explicit `color:` span, text inherits the client default (often black) and disappears. Every name/value in `opp_preview_section` must carry `color:{TEXT}` (or `MUTED`). Same rule for any new dark-panel content.
- **⚡ and the WIN % are mutually exclusive in the corner** — on a toss-up the ⚡ **replaces** the number (the exact odds don't matter at a coin-flip), otherwise the corner shows the % (a decisive 79% / 9% is worth seeing). The projected-outcome marker (▲▼◆) renders after either.
- **The corner marker is the PROJECTED OUTCOME, not a flip.** Renders on **every** card with a projection (`proj_res is not None`): ▲ green = projected win, ▼ red = projected loss, ◆ white = projected tie. A flip is visible by *contrast* (marker disagreeing with the card's current status). `proj_res` uses `round(pm, dec)` / `round(po, dec)` so it can't disagree with the point-estimate tie test.
- **INTENTIONAL DIVERGENCE — do NOT unify:** `build_matchup_section` (~line 1720) still shows ▲▼◆ **only on a flip** (`flip = proj_res != res`). Only Category Pulse always-shows projected outcome; leave the Matchup table flip-only.
- Summary line: current record then projected, each as full **W · L · T** (the T is always shown, even at `0T`, on both sides): `10W · 2L · 0T · ⚡N close → proj 11W · 1L · 0T`. The `⚡N close` segment appears only when ≥ 1 cat is close.
- Card value (`my score` / `vs opp`) stacked on two lines so decimal-heavy stats (OPS/ERA/WHIP) don't cause width/height inconsistency.
- **`days_elapsed`** = days since matchup start (0 on Monday of matchup start). Derived from `matchup_start_date` (snapshot field, see below) — NOT `datetime.now().weekday()` — so it counts correctly across a 2-week matchup. Guard: `day_clause = f' through Day {days_elapsed}' if days_elapsed > 0 else ' (week starting)'`.
- **Pitcher projections (K, QS, W)** use actual remaining starts × per-start rate, not weekly averages. `pit_proj` is built from pitchers with upcoming starts and passed to `build_category_pulse(remaining_proj=pit_proj)`. Rate/hitter cats still use historical averages via `compute_weekly_avgs`.
- **Win-probability (`_cat_win_prob` + `compute_weekly_std`):** each card shows a `WIN %` chip colored to match `proj_res`; ⚡ replaces it on toss-ups. `compute_weekly_std(roto, week)` → per-team/per-cat stddev (needs ≥ 2 completed weeks); threaded into `build_category_pulse(weekly_std=…)`. `_cat_win_prob(pm, po, cat, sigma, remaining_frac)` → `(p_win, p_tie)` via normal-CDF (`math.erf`): `edge` direction-adjusted for `_LOWER_BETTER`; `sigma = sqrt(my_std² + opp_std²)` (falls back to `_CLOSE_THRESH[cat]`); counting-cat uncertainty × `remaining_frac`. Tie band half-width `0.5·10^-dec` matches the display-precision tie test. Display-only — `classify_categories` untouched. `WIN %` per-card only; summary record stays point-estimate `→ proj`.

### Weekly matchup is Monday–Sunday (or Monday–Sunday×2 for All-Star break)
`week_end_str` comes from `matchup_end_date` in the snapshot (not a hardcoded `today + 6 - weekday`). `fetch_data.get_matchup_dates(league)` reads `league.settings.matchup_periods` — a dict mapping period → list of weekly scoring-period IDs (e.g. `{'15': [15]}` for 7 days, `{'16': [16, 17]}` for 14-day All-Star). Period length = `len(ids) * 7 days`. Stored in snapshot as `matchup_start_date`, `matchup_end_date`, `matchup_period_days`, `next_matchup_end_date`. Dates past `week_end_str` get a `NEXT WK` badge. **Bullet 2 scopes both `confirmed` and `thin_days` to `PSP_Date <= week_end`** so its count matches the KPI.

### End-of-matchup mode (`is_sunday`)
When `today >= matchup_end_date` (last day of the matchup period — not always a calendar Sunday for multi-week periods): header subtitle → "Weekly Lookahead"; subject → "Lookahead"; KPI → "Starts Next Week" (counts starts after `week_end_str`); Category Pulse subtitle → "Final stretch — week ends today"; Week at a Glance → "Next Week Preview"; bullet 1 appends "— final"; bullet 2 shows next-period confirmed starts; bullet 3 shows best FA SP for next period. `next_week_end_str` comes from `next_matchup_end_date` in the snapshot (available in `build_email`). `classify_categories` and `build_category_pulse` accept `matchup_days=matchup_period_days` so `elapsed_frac` divides by 14 (not 7) for a 2-week period.

### Probable starters
Primary: two MLB API calls (range schedule → batch hydrate). The +6-day rotation projection fills unannounced slots. A live-feed fallback exists if the batch returns nothing.

### HR% (`_hrp_cell`)
`HR_Probability` (computed in `fetch_data.compute_hr_probability` from barrel%, hard-hit%, launch angle, HR/AB, xwOBA, ISO, recent HR streak; ≈ 0.05–0.31, a modeled per-game HR probability) is a color-coded `HR%` column in Roster Hot/Cold + FA Hitters via `_hrp_cell(row)`, with a hover `title` tooltip of drivers (Barrel% · HardHit% · EV · xwOBA · ISO). Green ≥ 20%, yellow ≥ 14%. Takes the full player row (Roster Hot/Cold stashes the season row as `srow`). **`compute_hr_probability` measures power SKILL, not availability** — it must NOT gate on `ESPN_Status` (an earlier gate zeroed out Judge/Trout/Buxton). Returns 0.0 only when there's no usable signal at all (shows "—"). `ISO = SLG − AVG` (FP omits it).

### Week at a Glance pickup bullet (bullet 4) is hitter-only
The add is **always** the best available FA **hitter** — `focus_pit` is hard-set to `False` (pitcher streaming is covered by My Upcoming Starts / FA SP). `add_reason` targets losing hitter cats, else `"bat depth"`. Consequence: the SP `ratio_warn` never fires here; `add_type` is always `"hit"` so the drop prefers a hitter. Shows positions for add + drop (via `_pos_disp`, hides the generic `P` tag). Drop selection is position-aware: weakest droppable player sharing a `POS_GROUPS` group with the add first (add an OF → drop worst OF), then same player type, then any droppable. `_can_drop` guards ≥ 1 healthy player per position.

### Ratio-stat risk guardrail
In `_roster_suggestion`, when the chosen add is an SP (`_is_sp`) and ERA or WHIP is a currently-won **tossup** (per `classify_categories`), and the candidate's ERA > 4.20 / WHIP > 1.30, the pickup bullet appends a yellow `⚠ boosts K/W/QS but his {ERA} {cat} over ~{IP} IP risks your thin {cat} lead.` IP = `IP_per_G × _starts_this_week`, via `_fmt_ip`. (Note: bullet 4 is hitter-only, so this fires only elsewhere.)

### Tap-to-expand score breakdown v2
Tapping a Score badge reveals a **full-width row below the player's row** narrating the score's 2–3 most decisive drivers in prose. The recent-form clause **names the actual window** (`30-day`/`15-day`/`7-day`) from the recent row's `Dataset` (30 > 15 > 7 > pybaseball; hitter → 7-day, pitcher → 15-day) — this window intentionally differs from the Hot/Cold Δ column (different metric/period).
- **Mechanism (no JS, email-safe):** `score_reveal(score, breakdown_html, uid, colspan)` returns a **tuple `(cell_html, row_html)`**. `cell_html` is the badge in an `<a href="#{uid}">` (with a ▾ caret); `row_html` is a `<tr id="{uid}" class="scorebd-row" style="display:none;">` spanning `colspan` columns. The caller inserts `cell_html` into the Score `<td>` and appends `row_html` immediately after the player's `</tr>`. Head-`<style>` rule `tr.scorebd-row:target { display:table-row !important; }` reveals it. A `✕` link (`href="#{uid}x"` → dead anchor) closes it.
- **Scroll positioning:** the `:target` rule also sets `scroll-margin-top:40vh` (send_digest.py ~line 4015). Browser-attachment only (Gmail strips `<style>`).
- **`_bd_uid(prefix, name)`** mints a globally-unique anchor id (`bd-{prefix}-{slug}-{counter}` via a running `_BD_SEQ`). Prefixes: `rhc`/`phc`/`mus`/`myrp`/`fasp`/`farp`/`fahit`/`posw`/`posfa`.
- **Narrative (`_score_narrative` + `_hit_clauses`/`_sp_clauses`/`_rp_clauses`):** each `_*_clauses` returns `(fill, strength_phrase, weakness_phrase)` per component (`fill = comp_points / max`). `_score_narrative` names ≤ 2 strongest (fill ≥ .60) and ≤ 2 weakest (fill ≤ .35): `Carried by … ; held back by …`. Punt-saves-consistent: low SVHD / low HR% are NOT surfaced as weaknesses; SP `Role` (start volume) is omitted entirely. **HR/ISO power dedupe (`_hit_clauses`):** HR (volume) and ISO (rate) are the same "power" concept — when ISO is strong (fill ≥ .60) and HR weak (≤ .35) the HR weakness clause is dropped (and symmetrically for the reverse); the strength always survives.
- **Wired into all Score badges:** the 7 tables (Roster Hot/Cold, Pitcher Hot/Cold, My Upcoming Starts, My Relief Pitchers, FA SP, FA RP, FA Hitters) plus Positional Breakdown (weakest-my-player `posw` + best-FA `posfa`, role-aware via `p["ptype"]`, `colspan=4`). **HR% drivers are in the expanded hitter panel** (`_hitter_score_breakdown`) as a trailing muted `<div>` via `_hrp_driver_str(row)` — so touch users (no hover) see them.

### Unified role scores — a player shows the SAME score in every section
Three canonical role scores, all calibrated to p50→50, p90→80: SP → `_score_p` (blended `pitcher_score`), RP → `rp_score` (never blended — ESPN season counting stats, identical across My RP / FA RP / Positional Breakdown), Hitter → `_blend(r, hitter_score, best_recent_h)`. Never score a section with a different function than others use for the same role.

### Hot/Cold columns & KPI
- Both `build_pitcher_hot_cold_section` and `build_hot_cold_section` take a `best_recent_*` index and render a role-aware Score badge (pitcher → `_score_p`, hitter → `_blend(hitter_score)`).
- **Roster KPI hot/cold counter (`hc_str`):** the "Roster" KPI tile counts my ENTIRE roster — hitters AND pitchers. Hitters use 7-day OPS vs season (±0.015); pitchers use 15-day ERA vs season (±0.40, ≥ 3 recent IP, `rec_p` fallback). The two thresholds differ by design (OPS vs ERA scale) — keep the KPI in sync with each section's threshold. Tile label is "Roster" (whole team).

### Score cascade (`best_recent_p` / `best_recent_h`)
Built in `build_email` by merging `{**rec_p_fp, **p7, **p15, **p30}` (pitchers) and `{**rec_h, **h7, **h15, **h30}` (hitters) — later dicts win, so 30d FP > 15d FP > 7d FP > Baseball Ref. Passed to `_blend` and `positional_breakdown`.

### positional_breakdown viable filter
FA pool per position excludes benchies. SP: `GS >= _pit_viable_min("SP","GS")`. RP: `ESPN_GP >= _pit_viable_min("RP","GP") or IP >= _pit_viable_min("RP","IP")`. Hitters: `OPS > 0.200 or R+RBI > 5`. FA quality (`fa_quality`) = avg blended score of top-3 viable FAs. Scarcity: `< 50` scarce (RED), `< 60` moderate (YELLOW), `>= 60` deep (MUTED).

### Dynamic volume benchmarks (no hard-coded IP/AB/GS minimums)
"Full-time" thresholds are derived from the live snapshot each run so they scale with the season. Two builders, both called once at the top of `build_email`:
- `compute_ab_benchmarks(hitters)` → `_AB_BENCH[window]` = `_AB_LEADER_FRAC` (0.62) × the window's p95 (leader) AB. Consumed by `_ab_opportunity_mult` in `hitter_score`. `_FULLTIME_AB` is a cold-start fallback only.
- `compute_pitcher_benchmarks(pitchers)` → `_PIT_BENCH[(window, role)]` = leader IP/GS/GP (p95) per role, `_is_sp`-split. `_ip_reliability_mult` uses `_IP_RELY_FRAC` (0.20) × leader IP for the row's window+role. `_pit_viable_min(role, stat)` uses `_GS_VIABLE_FRAC`/`_GP_VIABLE_FRAC`/`_IP_VIABLE_FRAC` (0.17/0.30/0.38) × the season leader. `_PIT_FALLBACK` holds cold-start constants.

### Data-derived league averages (`_LG` / `compute_league_averages`)
Called once in `build_email` next to the benchmark builders; writes `_LG` with `ops` (full-time regulars), `team_ops` (mean opponent OPS faced), `team_k`, and starter `era`/`whip`/`k_pct`/`ip_per_start`/`barrel_allowed` from qualified YEAR rows. Consumers read `_LG.get(key) or <old literal>`. `qs_probability` stays calibrated because the intercept `38` and multipliers are fixed. fetch_data derives its own `LG_OPS` for wRC+. ONLY genuine league averages live in `_LG`; calibration/scaling constants (score spans/floors, park factor, `IP*4.3`, `compute_hr_probability` weights) do NOT.

### Dry-run preview filenames
Always `previews/digest_preview_{team_slug}.html` (e.g. `digest_preview_Guerrero_Warfare.html`). No `digest_preview.html` fallback — always slug-based.

### `--team` flag
`python send_digest.py --team "Team Name"` shows a full digest from another team's perspective (all sections render, incl. Category Pulse + Matchup banner). Requires `all_matchups` in the snapshot. `build_matchup_section` accepts `my_team` (default `MY_TEAM`). **Monday recap is per-team:** `get_all_prev_matchups(league)` builds prior-week recap for ALL teams into `all_prev_matchups`; `prev_matchup` resolves to `all_prev_matchups[my_team]`.

## Scoring functions (send_digest.py)

- `_is_sp(r)` → bool. Usage-based SP/RP detection (see gotcha).
- `_blend(r, score_fn, idx_recent, w=None)` → blended score. `_BLEND_W = 0.35` (35% recent + 65% season) — single source for math + tooltip. `idx_recent` is `best_recent_p`/`best_recent_h`. Applies to hitters + SPs; RP `rp_score` never blended.
- `hitter_score(r, _parts=…)` / `pitcher_score(r, _raw=…, _parts=…)` / `rp_score(r, _raw=…, _parts=…)` → `_parts=True` returns `(components_dict, multiplier)`. Component insertion order == display order (single source for tap-to-expand).
- `_score_p(r, idx_recent=None)` → canonical role-aware pitcher score. SP → `_blend(r, pitcher_score, idx_recent)`; RP → `rp_score(r)` unblended. Used by every pitcher Score display/sort.
- `_starts_this_week(r, today, week_end)` → int. Upcoming starts within the matchup week (from `PSP_Dates`; falls back to scalar `PSP_Date`). Drives the `2-START` badge and best-FA-SP preference.
- `save_role_watch(pitchers, my_team, claimed)` → `(emerging, fading)` (see gotcha).
- `classify_categories(matchup, weekly_avgs, days_elapsed, remaining_proj, matchup_days=7)` → `{cat: (proj_res, tier)}` (see gotcha). Pass `matchup_days=matchup_period_days` for 2-week periods.
- `compute_weekly_std(roto, current_week)` → per-team/per-cat stddev; `_cat_win_prob(pm, po, cat, sigma, remaining_frac)` → `(p_win, p_tie)` (see Category Pulse gotcha).
- `opponent_week_intel(pitchers, hitters, opp_team, best_recent_h, today, week_end)` → dict (starts, two-start pitchers, hot hitters) for the Opponent This Week block. None when `opp_team` empty.
- `pitcher_score(r, _raw=False)` → 0–100. Components: K/WhiffPctile (28), ERA/xERA (28), WHIP (20), contact-quality/BarrelPct+xwOBA (0–12), SP role bonus 9–12 by GS. Small-sample penalty `s *= min(1.0, ip/20)`. Calibrated `s * 1.5070 - 44.3346`. `_raw=True` returns pre-calibration.
- `rp_score(r, _raw=False)` → 0–100. SVHD de-emphasized to ~15% (punt-saves). Components from ESPN season counts: SVHD (15), K (26), W (15), ERA/xERA (16), WHIP (12), IP/G (8), contact-quality (0–8). Calibrated `s * 1.6543 - 28.0645`. My Relief Pitchers picks best dataset per player (YEAR → 30 → 15 → 7). Rerun `recalibrate_scores.py` when the SVHD-vs-skill balance changes.
- **Recalibration:** after changing component weights, rerun `python recalibrate_scores.py` and paste the new constants back. Populations drift with the season — rerun periodically.
- `hitter_score(r)` → 0–100. Prefers wRC+ over OPS. Uses xwOBA, sprint speed, Barrel%, ISO, HR_Probability. Opportunity multiplier (`_ab_opportunity_mult`): raw score scaled by AB vs a full-time benchmark (floored `_AB_FLOOR = 0.40`, capped 1.0) — a full-time hitter lands at 1.0 (no penalty). Calibrated `s * 1.587 - 5.2`. Displayed everywhere as `_blend(r, hitter_score, best_recent_h)`.
- `qs_probability(r)` → 1–99. Calibrated league-avg ~38%, ace ~75%. Uses IP/G (not IP/GS).
- `_fmt_ip(ip_decimal)` → baseball IP notation. `whole = int(d); outs = round((d-whole)*3); if outs>=3: whole+=1, outs=0`.
- `_proj_line_html(r)` → `IP · ER · K` span. ER = `raw_er * opp_factor * park_factor`; `opp_factor = clamp(opp_ops / LG_team_ops, 0.80, 1.20)`; `park_factor` = 0.97 home / 1.03 away. K also opponent-adjusted via `Team_K_Value`. IP = `IP_per_G`.
- `hot_cold_cell(season_val, recent_val, …, no_data_title=None, td_style=TDC)` → `<td>` with colored recent stat + 🔥/↑/❄/↓ icon vs season baseline. When recent is missing/zero and `no_data_title` is set, renders `—` with a dotted underline + hover tooltip. Optional `td_style` so the compacted pitcher tables' L15 ERA cell matches.
- `band_divider(label, color, anchor=…)` → full-width band boundary `<div>`.

### QS / 5K+ / 2-START badges (My Upcoming Starts + FA SP)
- `2-START` (purple, `_starts_this_week ≥ 2`), QS (green), 5K+ (yellow) render next to the pitcher name. **QS and 5K+ purely annotate the projected line** — driven ONLY by `_proj_line_vals(r)`, NOT season rates. QS = `_proj_is_qs` (6+ displayed IP & ≤ 3 ER, using `_fmt_ip` rounding); 5K+ = projected `K ≥ 5`. The **QS% column** shows season QS probability separately. Both tables use the identical rule.
- **FA SP badges are unconditional:** they fire on the projected line wherever the pitcher appears (the old thin-rotation-day gate was removed). `thin_days`/`my_starts_by_day` still drive the ⚑ per-day "N my starts" banner and Week-at-a-Glance bullet 2.

## Key data fields

**Pitchers:** `PlayerName`, `FantasyTeam`, `Position`, `Dataset` (7/15/30/2026), `IP`, `K`, `ERA`, `WHIP`, `GS`, `SVHD`, `K/IP`, `Kpct_P`, `IP_per_G`, `PSP_Date`, `PSP_HomeVAway`, `PSP_Projected`, `PSP_Dates` (list), `PSP_HomeVAways`, `Team_OPS_Value`, `Team_K_Value`, `BarrelPctAllowed`, `HardHitPctAllowed`, `AvgEVAllowed`, `xERA`, `xwOBA_against`, `WhiffPctile`, `WhiffPct` (raw rate, display-only), `ESPN_SV`, `ESPN_K`, `ESPN_W`, `ESPN_IP`, `ESPN_GS`, `ESPN_GP`, `ESPN_SVHD`, `ESPN_OnIL`

**Hitters:** `PlayerName`, `FantasyTeam`, `Position`, `Dataset`, `HR`, `RBI`, `R`, `SB`, `AVG`, `OPS`, `wRCplus`, `xwOBA`, `xBA`, `xSLG`, `SprintSpeed`, `ISO`, `Barrel_Pct`, `HardHit_Pct`, `HR_Probability`, `ESPN_OnIL`

**Roto:** `Team`, `Week`, `Roto_Score`, `{CAT}_Points` for each of 12 categories

## Digest section order (send_digest.py body_parts)

Five bands separated by `band_divider()` rules. The Triage divider renders only when `alert_section` is non-empty.

**Jump-to nav (`nav_bar`)** — pill nav (`My Roster · Free Agents · Season`) with anchor links to `band-myroster`/`band-fa`/`band-season`. Lives in the **header's top-right** (two-column table: `.hdr-main` left, `.hdr-nav` right) so it doesn't push Week at a Glance down. Mobile media query stacks the two cells. `nav_bar` drops a `<a name="top" id="top">`; every anchored `band_divider` renders a right-aligned `↑ TOP` back-link.

- **⚑ ALERTS** (conditional): 1. Roster Alerts
- **MY ROSTER:** 2. Week at a Glance · 3. Category Pulse · 3b. Opponent This Week (`opponent_week_intel`/`opp_preview_section`, below Category Pulse; opponent start count, two-start pitchers, top-3 hot bats by recent OPS, season roto strengths/weaknesses via `category_ranks`, wire activity; logo via `fantasy_logo()`; renders only when opponent has starters or hot hitters) · 4. Current Matchup (category rankings; hidden on Monday when `_all_roto_tied`) · 4b. Week N Roto Rankings (all 12 teams, live; `_all_roto_tied` flag controls both 4 and 4b — both hidden Monday before stats accumulate, when all `Roto_Score` values are equal; #1/#12 ranks rendered as inline `<span>` badges, not `<td>` borders) · 5. Matchup (score banner + category table)
- **MY ROSTER (holes first):** 10. Positional Breakdown · 6. My Upcoming Starts · 7. My Relief Pitchers · 8. Pitcher Hot/Cold · 9. Roster Hot/Cold
- **FREE AGENTS:** 11. FA — Starting Pitchers · 12. FA — Relief Pitchers · 13. FA — Hitters
- **SEASON:** 14. My Season Category Rankings · 15. League Luck Standings

**My Season Category Rankings subtitle** shows a pseudo-single-week roto score: `sum(n - rank + 1 for rank in cats.values())` (max = n × 12).

**My Upcoming Starts subheader:** `X starts across Y days | N this wk[, N next wk]`. "this wk" count is red when 0; ", N next wk" omitted when next-week count is 0.

**FA SP / My Upcoming Starts columns (8):** Pitcher · Proj. Line · Matchup · QS% · ERA · L15 ERA · K% · Score. "Proj. Line" = projected `IP · ER · K` per start, IP in baseball notation via `_fmt_ip()`. **Opp OPS is folded into the Matchup cell** as a muted second line (`_opp_ops_sub(r)` → "Opp OPS .742"). **Raw whiff% is folded under the K% cell** as a muted subline (`_whiff_sub(r)` → "whiff 28%") — keeps the table at 8 cols. Date-header rows span `colspan="8"` with background on `<tr>`.

**Compacted 8-column pitcher tables (My Upcoming Starts + FA SP):** these two overflowed iPad width. Two things keep them narrow: (1) each builds local tight style vars `_th`/`_tdc`/`_tds` = `TH_S`/`TDC`/`TD_S` with padding `10px→6px` and font `13px→12px` (via `.replace()`) + table inline `font-size:12px`; (2) Opp OPS folded into Matchup (9→8 cols). The style swap is scoped to these two blocks only — shared `TH_S`/`TDC`/`TD_S` are untouched. Keep the two tables identical: same padding/font, 8-column layout, `colspan="8"` banner rows, and **`score_reveal(...)` colspan arg = 8** (a stale 9 leaves the breakdown row a column short).

## Color palette

```python
BG="#080e1c"  SURFACE="#101827"  SURFACE2="#0d1424"  BORDER="#1e2d45"
TEXT="#e2e8f0"  MUTED="#64748b"  ACCENT="#3b82f6"
GREEN="#22c55e"  RED="#ef4444"  YELLOW="#f59e0b"  PURPLE="#a855f7"
```

`PURPLE` is used only for the `2-START` badge. My team name is always `font-weight:800;color:{ACCENT}` with a ← arrow.

## Automation

- **GitHub Actions:** `.github/workflows/daily-digest.yml` triggers 06:00 and 15:00 UTC. GitHub's scheduler is unreliable — delays run 1–4 h (often 3–7 h), so expected delivery ≈ 4–6 AM / 1–3 PM EDT. Cron is always UTC. ESPN credentials are repo secrets (`ESPN_SWID`, `ESPN_S2`). Decision: sticking with GitHub Actions — do not migrate to cron-job.org unless revisited.
- **Fetch-time freshness badge:** the record and category standings are a point-in-time snapshot of ESPN's live box scores at fetch time. The header badge shows the fetch **time in ET** (`_fmt_refresh_time`): green ✓ when data is from today, yellow ⚠ when stale. `fetch_data.py` writes `refreshed_at` as tz-aware UTC (`datetime.now(timezone.utc)`) so the ET conversion is correct on CI (UTC) or local; `_fmt_refresh_time` converts to `America/New_York`, shows naive stamps as-is. `_data_fresh` keys off the date only.
- **Local runner:** `scripts/run_digest.bat` for manual runs. Captures console output (incl. tracebacks) to `logs/run_console.log`; the structured one-line-per-send record goes to `logs/digest.log` (written by send_digest.py). Kept in **separate files** — the old shared handle collided (`PermissionError` on Windows). The Python write is wrapped so a locked log can't crash a run that already sent.

## Environment notes

- fetch_data.py on Windows: ASCII-only in print/log strings (charmap encoding crashes on Unicode).
- ESPN credentials (swid, espn_s2) are hardcoded in fetch_data.py as fallbacks; also GitHub Actions secrets.
