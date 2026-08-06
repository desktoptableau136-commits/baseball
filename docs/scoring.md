# docs/scoring.md — Scoring, badges & Category Pulse

> Loaded on demand from CLAUDE.md. Read when editing `fantasy/scoring.py`, `fantasy/analytics.py`, the badges, or Category Pulse / win-prob. Rules here are as authoritative as CLAUDE.md.

### Recent Form is ONE cascade-driven, Score-delta concept everywhere
Every hot/cold surface — `build_pitcher_hot_cold_section` ("Pitcher Recent Form"), `build_hot_cold_section` ("Hitter Recent Form"), My Upcoming Starts, FA Starting Pitchers, FA Hitters, My/FA Relief Pitchers, `roster_hot_cold_counts` (the Roster KPI), and both dashboard tiles — is driven by `_hitter_recent_form`/`_pitcher_recent_form` (`fantasy/analytics.py`): a **composite Score delta** (season vs whichever window is freshest per player — the `best_recent_h`/`best_recent_p` cascade, 30d FP > 15d FP > 7d FP > Baseball-Ref), NOT a raw-stat delta over one fixed window. `_pitcher_recent_form` auto-branches SP (`pitcher_score`) vs RP (`rp_score_recent`, rate-paced). Thresholds are `_FORM_HOT_DELTA=12`/`_FORM_WARM_DELTA=5` score points (mirrored for cool/cold), replacing what used to be four separately-tuned raw-stat threshold pairs. Every cell shows a muted window tag (`_window_badge`, e.g. "15d"/"30d") naming which window backed it, since the cascade picks a different window per player. `fetch_recent_pitcher_stats`/`fetch_recent_hitter_stats` still fetch the 15/7-day pybaseball fallback rows that feed the cascade, and both accept optional `start_dt`/`end_dt` string params for an exact date window (used to populate `prev_week_hitting`/`prev_week_pitching`).

### Save-Role Watch (`save_role_watch`)
**Recent holds are unavailable anywhere in the pipeline** (per-window `SVHD` captures recent SAVES only; ESPN exposes only season totals), so recency is save-only. Flags (a) **emerging FA closers** (FA RP with ≥ 3 saves in last 15 days) and (b) **fading rostered closers** (my RP with **season saves `ESPN_SV` ≥ 5**, 0 recent saves despite ≥ 3 recent appearances). Gated on season *saves*, not SV+H, so a holds-based reliever (JoJo Romero 0 SV / 20 HLD) is never falsely flagged. Callout on the FA Relief Pitchers section.

### Category classification (`classify_categories`)
Returns `{cat: (proj_res, tier)}` reusing Category Pulse's projection math (`_project` + `pit_proj` for K/QS/W). Tier is only `tossup` (margin ≤ `_CLOSE_THRESH`) or `leaning` — **no `locked` tier / no 🔒 badge**. Used to detect a THIN ERA/WHIP lead for the ratio-stat pickup warning. Computed once in `build_email` as `category_classification`, passed to `_roster_suggestion`.

### Category Pulse
- Tied cats use `TEXT` (white) for border/value/status — not `YELLOW`. Win=green, loss=red, tie=white. Card value (`my score`/`vs opp`) stacked on two lines so decimal-heavy stats don't cause width/height inconsistency. Card div is `position:relative`.
- **Corner badge** (`position:absolute` top-right, not inline) carries two things: the **⚡ toss-up flag or the WIN %** (mutually exclusive — ⚡ replaces the number on a toss-up), then the **projected-outcome marker ▲▼◆** after it.
  - **⚡ = win-% toss-up, NOT a current-margin close:** fires when `win_pct` ∈ 45–55 **or `proj_res == "T"`**. Collected into `close_flags` → summary `⚡N close`. (`_CLOSE_THRESH` is still used for the `_cat_win_prob` sigma fallback + `classify_categories` tossup tier — just not the card ⚡.)
  - **▲▼◆ = the PROJECTED OUTCOME, not a flip:** renders on **every** card with `proj_res is not None` (▲ green win / ▼ red loss / ◆ white tie); a flip is visible by *contrast* with the card's current status. `proj_res` uses `round(pm/po, dec)` so it can't disagree with the point-estimate tie test.
- **Opponent This Week — always wrap text in an explicit color.** The panel sits on `SURFACE2` (dark); without an explicit `color:` span text inherits the client default (often black) and disappears. Every name/value in `opp_preview_section` must carry `color:{TEXT}` (or `MUTED`) — same rule for any new dark-panel content.
- Summary line: current then projected record, each as full **W · L · T** (T always shown, even `0T`): `10W · 2L · 0T · ⚡N close → proj 11W · 1L · 0T`. The `⚡N close` segment appears only when ≥ 1 cat is close.
- **`days_elapsed`** = days since matchup start (0 on Monday). Derived from `matchup_start_date` — NOT `datetime.now().weekday()` — so it counts across a 2-week matchup. Guard: `day_clause = f' through Day {days_elapsed}' if days_elapsed > 0 else ' (week starting)'`.
- **Pitcher projections (K, QS, W)** use actual remaining starts × per-start rate (`pit_proj`, passed as `build_category_pulse(remaining_proj=pit_proj)`), not weekly averages. **Hitter counting cats (R/HR/RBI/SB/B_SO)** are ALSO projected schedule-aware via `compute_hit_proj` (below) — `weekly_avg × team_hit_sched_frac`, the team's roster-weighted fraction of window bat-games still to come — and merged into the same `pit_proj` dict (`pit_proj.update(compute_hit_proj(...))`) so `classify_categories`/`build_category_pulse` both pick it up. **OPS (rate cat) still uses the `_project` rate blend over `compute_weekly_avgs`** — only the 5 hitter *counting* cats moved off the league-wide time fraction.
- **Win-probability (`_cat_win_prob` + `compute_weekly_std`):** each card's `WIN %` chip is colored to `proj_res`; ⚡ replaces it on toss-ups. `compute_weekly_std(roto, week)` → per-team/per-cat stddev (needs ≥ 2 completed weeks), threaded into `build_category_pulse(weekly_std=…)`. `_cat_win_prob(pm, po, cat, sigma, remaining_frac)` → `(p_win, p_tie)` via normal-CDF (`math.erf`): `edge` direction-adjusted for `_LOWER_BETTER`; `sigma = sqrt(my_std² + opp_std²)` (falls back to `_CLOSE_THRESH[cat]`) **× `_WINPROB_SIGMA_INFLATE` (1.5)**; counting-cat uncertainty × `remaining_frac`; tie band `0.5·10^-dec`. Display-only — `classify_categories` untouched, summary record stays point-estimate `→ proj`. **`_WINPROB_SIGMA_INFLATE` calibration:** `backtest_winprob.py` (walk-forward over ~10k historical category matchups) found the raw std-of-weekly-values understates the true margin spread → the model was materially **over-confident** (a stated 90%+ won ~73%, ECE 7.4 pts) because the raw std treats a team's historical mean as its true level, ignoring mean uncertainty (roster/role churn). 1.5× pulls ECE to ~2.8 (under the ~3-pt well-calibrated bar); applied INSIDE `_cat_win_prob` so digest + dashboard agree. The pre-week optimum is ~1.9× but that would over-widen mid/late-week (banked stats cut real uncertainty; counting cats already taper via `remaining_frac`) — re-run the backtest to see the residual-k sweep if retuning. Only the shown Win%/⚡ changes, never a projected W/L/T verdict.

### Win-the-Week odds + swing leverage (`_matchup_win_prob` / `_matchup_swing` / `_winprob_joint`)
Combines the 12 independent per-category `_cat_win_prob` outputs into ONE number: the probability of
winning more categories than the opponent this matchup. Co-located with `_cat_win_prob` in
send_digest.py (~line 1140).
- **The math is an exact DP, not a simulation.** `_matchup_win_prob(cat_probs)` takes a list of
  `(p_win, p_tie)` pairs and convolves the category-margin distribution `D = (#won) − (#lost)`:
  each category shifts `D` by +1 (prob `p_win`), 0 (`p_tie`), or −1 (`p_loss = 1 − p_win − p_tie`).
  13 integer states (−12…+12), so the whole thing is a handful of dict operations — no numpy, no
  Monte Carlo. Returns `(P_win_week, P_tie_week, P_loss_week)`.
- **`_matchup_swing(cat_probs_by_cat)`** — `{cat: (p_win, p_tie)}` → `{cat: leverage}`. Leverage =
  `P(win week | cat forced to a sure win)` − `P(win week | cat forced to a sure loss)`, everything
  else held fixed. Near-zero for an already-locked or already-conceded category (forcing it W vs L
  barely changes an outcome that was never in doubt); largest for genuine coin-flip categories —
  exactly the ones worth spending a roster move on. 12 cheap re-runs of the same DP.
- **`_winprob_joint(winprob_ctx, winprob_rf)`** — the adapter every consumer actually calls. Loops
  the `winprob_ctx` dict (`{cat: (pm, po, sigma)}`, already built once in `build_email` via
  `_winprob_ctx` — the SAME projection/sigma setup `build_category_pulse`'s cards use), runs the
  calibrated `_cat_win_prob` per category, and returns `(joint_tuple, per_cat_map)`. Because it's fed
  the identical context as the cards, the joint number can **never disagree** with what the reader
  sees on the Category Pulse grid. `build_email` builds this ONCE (`winprob_joint`, `winprob_percat`)
  right after `winprob_weeks`; every consumer (the 🏆 chip, the Briefing line, `build_game_plan`, the
  dashboard) reads that single result — nobody recomputes it per-render.
- **Independence caveat (ship "modeled odds," not a guarantee):** categories aren't truly
  independent — a strong pitching week correlates K/QS/W/ERA/WHIP — so the joint is mildly
  overconfident at the tails, same assumption the shipped per-cat model already makes. Partially
  self-correcting: the per-cat sigmas are already inflated by `_WINPROB_SIGMA_INFLATE` (1.5,
  calibrated against `backtest_winprob.py`), which pulls per-cat probabilities off the extremes
  before they ever reach the joint. A future refinement could model block correlation (pitching cats
  as one correlated group, hitting cats as another) instead of treating all 12 as independent; not
  done for v1 — flagged here as the known next step if the tails prove materially off in practice.
- **How to read the number (drives ALL user-facing copy):** 50% = genuine coin flip. True 100%/0% is
  rare — 12 categories means you need to be near-certain in almost all of them to reach an extreme,
  so a great week realistically reads ~90%, not 100%; that's correct behavior, not a bug. There's
  always a small tie slice (`P(win)+P(tie)+P(loss)=100%`); the shown chip is the WIN share only.
- **Display sites, all reading the SAME `winprob_joint`/`winprob_percat`:** the Category Pulse
  summary line's 🏆 chip (`build_category_pulse` collects each card's `(p_win, p_tie)` into
  `cat_probs` during its own card loop — a SEPARATE, redundant call to `_matchup_win_prob` on the
  identical inputs, not a second data source, so it can't drift even though it isn't literally the
  same Python call as `build_email`'s `winprob_joint`); The Briefing's "This matchup" line
  (`render_briefing(win_week_pct=...)`); the Weekly Game Plan header (below); and the dashboard's
  Category Pulse tile subtitle (`dashboard._pulse_cell` returns each cell's `(p_win, p_tie)` as a
  third tuple element, `render_category_pulse` collects them and calls `sd._matchup_win_prob`
  directly — same pattern, dashboard has no `build_email` context to import).

### The Weekly Game Plan (`build_game_plan`)
A digest section (top of the TRANSACTIONS band) that turns the win-prob machinery into a coach's
read instead of a report. Two parts, both gated on `matchup` + a non-empty `winprob_percat` (mirrors
every other matchup-dependent section's empty-state guard).
- **Part A — contest/concede strip.** Every category lands in exactly one bucket, from
  `winprob_percat` + `_matchup_swing`:
  - **🔒 Locked** (`p_win% >= _GAMEPLAN_LOCK_PCT` = 85) — already yours, no action.
  - **⚔️ Contest** — a real toss-up (not locked, not conceded) whose leverage clears
    `_GAMEPLAN_MIN_LEVERAGE` (8 win-the-week percentage points) — genuinely worth a move, sorted
    most-decisive-first.
  - **✋ Concede** (`p_win% <= _GAMEPLAN_CONCEDE_PCT` = 15, OR a toss-up whose leverage is below the
    minimum) — don't spend a move chasing it; it either can't be won or winning it barely moves the
    week.
  - This is a **separate taxonomy from `classify_categories`'s `tossup`/`leaning` tiers** — that
    function classifies a category's own closeness (margin-based); this one classifies whether the
    category is worth ACTING on, using leverage against the whole week. Don't conflate the two or
    reuse one's thresholds for the other.
- **Part B — up to `_GAMEPLAN_MAX_HIT_MOVES` (2) hitter + `_GAMEPLAN_MAX_PIT_MOVES` (2) pitcher
  ranked move cards, in two side-by-side columns.** Candidates come ONLY from the already-built
  `fa_sp`/`fa_rp`/`fa_hit` pools passed in from `build_email` (never rebuilt), so the plan can't
  recommend a player the FA tables don't also list. The FA pool is split into a **hitter** candidate
  list (`fa_hit`) and a **pitcher** list (`fa_sp` + `fa_rp` combined) BEFORE scoring — `_score_candidates`
  runs once per list, and `_build_cards` independently caps+renders each (own `①②` numbering per
  column, sharing the same `slots_left`/`_used_drops` state so the two columns' drop picks still
  never collide). `_GAMEPLAN_MAX_MOVES` = the sum (4), kept only for the section subtitle. Ranking
  within each column is by **matchup-level lift** (`_move_win_delta`), not a single category's swing:
  - **`_pickup_contrib(cand_row, role, remaining_frac, today_str, week_end_str, weeks_played, team_game_dates=None, opp_starter_by_date=None, pitchers_by_name=None)`** →
    `{cat: delta}` — the role-aware remaining-production estimate, extracted out of
    `pickup_win_delta` (the FA-table "Cats" column's swing chip) so both the chip and the Game Plan
    ranking share the exact same contribution math and can't disagree about what an add actually
    produces. SP uses actual remaining starts × per-start rate; RP rate-paces the season total by
    `weeks_played` × `remaining_frac`. **hit rate-paces by `weeks_played` × a quality-weighted
    fraction** — the SAME per-day opposing-starter-quality weighting as `compute_hit_proj`'s Tier 2
    (via `_opp_starter_quality_mult`, `send_digest.py`), just scoped to this ONE candidate's own MLB
    team instead of a whole fantasy roster: walks his team's remaining scheduled dates (from
    `team_game_dates`), scores each day's opposing probable starter via `qs_probability`
    (`opp_starter_by_date` → `pitchers_by_name` lookup), and uses the resulting weighted fraction in
    place of the flat `remaining_frac`. Falls back to flat `remaining_frac` when any of the three
    optional args is missing or his team can't be resolved to scheduled games (old snapshots degrade
    cleanly). `pickup_win_delta`/`_move_win_delta` both thread these three args straight through to
    this helper (its own gating/return shape is unchanged — verify with `render_diff.py check`).
  - **`_move_win_delta(cand_row, role, winprob_ctx, per_cat, remaining_frac, today_str,
    week_end_str, weeks_played)`** → `(best_cat, cat_before%, cat_after%, week_before%, week_after%)`
    or `None`. Folds `_pickup_contrib`'s deltas into a COPY of `per_cat` (never mutates the shared
    map — the same one the 🏆 chip reads), recomputing only the affected category's `p_win` via
    `_cat_win_prob`, then reruns `_matchup_win_prob` on the copy. Unlike `pickup_win_delta`, this does
    **not** gate on `_PICKUP_WINDELTA_MIN`/`_PICKUP_CONTESTED_MAX` — a candidate whose only
    production lands in an already-locked/conceded category naturally yields ~0 matchup lift and
    sorts itself out; `build_game_plan` additionally skips any candidate whose `best_cat` is in the
    Locked set outright (avoids a spurious "+0%" card).
  - **`streamer` vs `hold` tag:** `hold` when the candidate's SEASON blended score beats my starter
    quality at his position (`pos_data[pos]["my_avg"]`, the same top-K-starter average
    `_roster_suggestion`'s BAT bullet uses) by `>= _UPGRADE_MARGIN` — a durable upgrade. Otherwise
    `streamer` (an SP picked up for this week's start(s), or a bat whose edge is this week/recent-form
    only). Concrete rule, no fuzziness — see `_is_hold` inside `build_game_plan`. **Rendered top-right
    of the card header**, beside the `① +N% {cat} odds` line (an `overflow:hidden` header div
    contains the `float:right` tag so it can't leak into the row below) — moved off the drop line
    (where it used to sit floated beside "Drop {player}") so the hold/streamer read is the first
    thing seen, before the drop cost.
  - **Drop selection** mirrors `_roster_suggestion`'s `_take_drop`/`_used_drops` PATTERN (transaction-
    aware via a `pending_add` coverage check, IL-slot-safe) but is its OWN local instance inside
    `build_game_plan` — not literally shared state with `_roster_suggestion` (that function's surplus
    definition also folds in bench-leakage `lineup_eff`, which the Game Plan doesn't thread through).
    The dedupe guarantee ("two cards never suggest dropping the same player") holds WITHIN the Game
    Plan's own cards via its own `_used_drops`; it does not cross-dedupe against the separate
    Week-at-a-Glance bullets. As of the recency/same-position revision below, **both** functions now
    share the same eligibility model (`_drop_eligibility_score`, `_DROP_SEASON_FLOOR`, `_outclasses`),
    defined once in `send_digest.py` near `_UPGRADE_MARGIN` — only the surrounding roster context
    (surplus/leak groups, `pending_add` coverage) stays per-function.
  - **Droppability — two independent eligibility paths, either qualifies a candidate (session
    addendum, caught in review; revised again to add recency + same-position targeting).** The FIRST
    version picked a drop by worst BLENDED score (65% season / 35% recent) across the whole roster,
    same as `_roster_suggestion`. That's wrong: a single brutal recent week (a 0-for-a-lot stretch, a
    couple of bad starts) can crater a real starter's blended score enough that the algorithm reads
    him as the "worst" player on the roster and offers him up as the cost of a throwaway streamer —
    confirmed in testing when a legitimate everyday 3B (season score 66, blended down to 55 by a
    0.465-OPS/7-day slump) got surfaced as the drop for a middling streaming SP. The fix was an
    absolute SEASON-only floor — safe, but blunt: on a roster where every bench body still scores
    like a real contributor, it could leave the drop pool completely empty, and it could never single
    out a same-position player who's merely been outclassed by a specific incoming FA (a decent player
    isn't "fringe" in the abstract, but can still be the obviously-correct cut for a *specific* add at
    his own position). Current model, both paths gated first on the shared hard excludes
    (`_active_role_pitcher` — any current-season SVHD > 0, protects a closer/setup arm that
    `rp_score`'s punt-saves weighting (~15% SVHD) can rank as "worst" despite real save-role value —
    and `_is_protected`, the team's `_TEAM_PROTECTED_PLAYERS` list, see "Personal strategy overlays" in
    `docs/trades.md`):
    - **Path A (absolute / fringe).** `_drop_eligibility_score(r, kind, best_recent_h) <
      _DROP_SEASON_FLOOR` (40 — a touch above `_FA_SP_MIN_SCORE`'s 35 "streamer-tier" cutoff).
      `_drop_eligibility_score` is the season score for pitchers, and for hitters the season score
      DISCOUNTED by the same continuous, confirmed-decline signal `_tval` already uses for trade
      valuation (`_recency_value_mult`/`hitter_recency_severity`, `fantasy/trades.py`) — a slump that
      doesn't survive AB-weighted regression toward xBA/xSLG is a no-op (`'noise'`, mult 1.0), so this
      still can't reopen the original bug; only a REAL, magnitude-scaled decline moves a hitter's
      eligibility score down. Pitchers stay plain season score — no continuous pitcher-side severity
      signal exists (a `pitcher_recency_severity` analog was investigated via walk-forward backtest
      and found backwards for pitchers at these sample sizes — see "Pitcher recency: disabled
      confirmation flag + validated bounce-back replacement" below; the disabled `pitcher_recency_flag`
      and its `pitcher_bounceback_flag` replacement are both THIS-START-scoped display signals, not
      durable-value ones, so neither is a candidate for this eligibility discount).
    - **Path B (relative / outclassed at the same spot, NEW).** `_outclasses(cand, cand_score,
      pending_add)`: `cand` shares a `POS_GROUPS` label with the specific `pending_add` being offered
      in THIS card/bullet, and `pending_add`'s blended score beats `cand`'s by `>= _UPGRADE_MARGIN` —
      the same margin already used to decide a position is a real NEED. This is what lets a
      not-globally-fringe player go when he's demonstrably the worse option at the exact position an
      add fills (e.g. a struggling everyday SS clearly outclassed by a hot FA at the same spot), without
      loosening anything for an unrelated pickup — Path B only ever fires against the add actually on
      the card. Recomputed per `pending_add` (like the coverage check), since it depends on the
      specific transaction. Ranked ahead of Path A in `_take_drop`'s sort (same-spot consolidation
      first, then Path A surplus, then Path A other) and rendered with an `[outclassed]` tag distinct
      from `[surplus]` so the digest names the real reason.
    - This applies to EVERY move, hold or streamer — a slump doesn't make a real asset expendable
      for a durable upgrade, and an outclass read is only ever scoped to the position actually being
      upgraded.
  - **No safe drop ≠ no card (loosened from the original all-or-nothing gate).** On a well-built
    roster `_droppable` can legitimately be empty, so `_take_drop` returns `None` — but rather than
    skip the candidate outright, the card still renders (ranked purely by matchup lift) with an
    advisory line ("No safe drop right now — worth a manual look at your bench") in place of a
    specific drop. The original version `continue`d past the candidate entirely whenever no safe
    drop existed, which could zero out Part B even when good adds existed; the advisory line
    preserves the "never force a bad cut" guarantee while still surfacing the pickup idea. Each
    column can independently render fewer than its own cap, or a per-column empty-state note
    ("No moves clear the bar" + a hitter/pitcher-specific line), when that side has genuinely zero
    positive-lift candidates; the combined full-width empty state only renders when BOTH columns
    are empty.
  - **`is_sunday`** (matchup ends today) suppresses Part B entirely (no move can swing today's
    closing matchup — same rationale as `_matchup_closing_note`) but Part A (final odds +
    contest/concede) still renders.
- **`_GAMEPLAN_WEEKLY_MOVE_CAP` (7) is a reference constant only** — the same "7 moves/week" figure
  already cited as rationale for the FA-SP per-day display cap — NOT a live remaining-moves counter.
  No snapshot field tracks weekly transaction usage (ESPN's `recent_activity()` pull isn't a reliable
  per-team-per-week count), so the section subtitle names it for budget CONTEXT only and never claims
  an exact "N moves left."

### HR% (`_hrp_cell`)
`HR_Probability` (`fetch_data.compute_hr_probability` from barrel%, hard-hit%, launch angle, HR/AB, xwOBA, ISO, recent HR streak; ≈ 0.05–0.31 modeled per-game HR prob) → color-coded `HR%` column in Hitter Recent Form + FA Hitters via `_hrp_cell(row)`, hover `title` = drivers. Green ≥ 20%, yellow ≥ 14%. Takes the full row (Hitter Recent Form stashes the season row as `srow`). **Measures power SKILL, not availability** — must NOT gate on `ESPN_Status` (an earlier gate zeroed out Judge/Trout/Buxton). Returns 0.0 → "—" only when no usable signal. `ISO = SLG − AVG` (FP omits it).

### Tap-to-expand score breakdown v2 (v1→v2 rationale in NOTES)
Tapping a Score badge reveals a **full-width row below the player's row** narrating the 2–3 most decisive drivers in prose. The recent-form clause **names the actual window** (`30-day`/`15-day`/`7-day`) from the recent row's `Dataset` (30 > 15 > 7 > pybaseball; hitter → 7-day, pitcher → 15-day) — this is the exact same `_hitter_recent_form`/`_pitcher_recent_form` season-vs-recent Score comparison the visible Recent Form column uses, so the dropdown header and the column can never disagree (see "Recent Form" above).
- **Mechanism (`:target` CSS + a JS toggle enhancement):** `score_reveal(score, breakdown_html, uid, colspan, small=False)` returns `(cell_html, row_html)`. `cell_html` = badge in `<a href="#{uid}" class="bdlink">` (▾ caret); `row_html` = a hidden `<tr id="{uid}" class="scorebd-row" style="display:none;">` spanning `colspan`, appended immediately after the player's `</tr>`. Head-`<style>` rule `tr.scorebd-row:target { display:table-row !important; }` reveals it; `✕` (`href="#{uid}x"` → dead anchor) closes it. (Gmail strips `<style>` → rows stay hidden there.)
  - **Click-to-toggle (`_BD_TOGGLE_SCRIPT`, module const injected before `</body>`):** a progressive-enhancement script makes **clicking a `.bdlink` pill TOGGLE its breakdown** (and the ✕ close too). It `preventDefault`s so the URL fragment never changes → the `:target` rule stays a clean **no-JS fallback**. Handles both the `<tr>` and `<div class="scorebd-div">` variants (`el.tagName === 'TR' ? 'table-row' : 'block'`). **Attachment-only** — Gmail strips `<script>` like `<style>`, so the email body is unaffected. A deliberate, contained exception to the otherwise-JS-free digest (trade_lab remains the only tool that *depends* on JS).
  - **`small=True`** shrinks the pill (`badge(score, small=True)` → 9px font / `1px 6px` pad / smaller ▾) — used for the Positional Breakdown drop-candidate line.
- **Scroll positioning:** the `:target` rule also sets `scroll-margin-top:40vh` (send_digest.py ~line 4015). Browser-attachment only.
- **`_bd_uid(prefix, name)`** mints a globally-unique anchor id (`bd-{prefix}-{slug}-{counter}` via a running `_BD_SEQ`). Prefixes: `rhc`/`phc`/`mus`/`myrp`/`fasp`/`farp`/`fahit`/`posw`/`posfa`.
- **Panel layout — four stacked lines (`_hitter_score_breakdown`/`_pitcher_score_breakdown`):** (1) a bold **dual-score header** `Hitter score (season | 30-day): 77 | 60 🥶` — season and recent scores each wrapped in `_score_text_hex(...)` (brighter GREEN/ACCENT/YELLOW/RED palette variants keyed to the SAME 72/52/32 tiers as `badge()`, readable as text on the dark panel; `_score_bg_hex` single-sources the pill background tiers), with the Recent Form **emoji** (`_FORM_EMOJI` = 🔥 hot/↑ warm/➖ steady/↓ cool/❄ cold, keyed off the same `_FORM_HOT_DELTA`/`_FORM_WARM_DELTA` Score-delta thresholds the visible column uses) beside the recent score (no trailing period). **RP now gets the dual header too** via **`rp_score_recent(rec, season_row)`** (`fantasy/scoring.py`): the headline `rp_score` is season-volume based (SV+H/K/W are season counting totals, and it *prefers* the season-broadcast `ESPN_*` fields), so a naive `rp_score` on a 30-day row returns a near-season number. `rp_score_recent` instead PACES the window's FP counting stats to season-equivalent volume (K & W per-IP, SV+H per-appearance) and feeds a synthetic row **with no `ESPN_*` keys**, so it rides the SAME static caps AND the SAME live `_SCORE_CALIB["rp"]` as the season score → a clean same-scale, same-player comparison whose delta is pure rate change. Returns `None` (→ single-score fallback header) on a thin/absent window (`< 5` window IP). It also nudges a genuinely scoreless window's `0.00` ERA/WHIP to `0.01` so `rp_score`'s `_n(...) or 5.0`/`or 1.5` fallbacks don't misread elite run-prevention as a blowup. (1b) two compact muted **metrics lines** (`_recent_form_line(rec, role, win, season_row=r)`) — a `Season: …` line built from the season row `r`, stacked directly above a `Last 30 days: …` line built from the recent row `rec`, both via the shared `_fmt_stat_parts(row, role)` formatter (identical stats/formatting on both lines so the two windows compare directly) naming ≤ 3 rates — SP/RP `Season: 3.10 ERA · 24% K · 1.08 WHIP` / `Last 30 days: 0.00 ERA · 19% K · 0.32 WHIP`, hitters `Season: .765 OPS · .275 AVG · 14 HR` / `Last 30 days: 1.028 OPS · .275 AVG · 8 HR`. The Season line renders whenever `season_row` has stats (i.e. always, once `comps` is non-empty); the Last-N-days line renders only when the dual header shows (recent row + window label present). (2) an **italic scouting archetype one-liner** (`_archetype_line` wraps `_hitter_archetype`/`_pitcher_archetype`) — a punchy profile read (breadth `does a bit of everything (N/5 cats)` · `a true three-outcome slugger` · `a speed merchant` · `a power-speed threat` for hitters; `a front-line arm` · `a polished run-preventer` · `a bat-misser with traffic` · `a lockdown reliever` for pitchers) + a form/value tail from `_archetype_form_tail` (words only — the emoji lives in the header; **the RP branch now receives a hot/cold `tag`** and renders "throwing fire lately"/"scuffling lately but still an elite arm"). Hitter "speed" keys on **SB, not raw sprint**; pitcher K% falls back to **K/IP** when the rate is missing so an ace isn't demoted; front-line rewards elite WHIP+K behind an unlucky ERA. (3) the mechanical **`Carried by …`** clause (below).
- **Narrative (`_score_narrative` + `_hit_clauses`/`_sp_clauses`/`_rp_clauses`):** each `_*_clauses` returns `(fill, strength_phrase, weakness_phrase)` per component (`fill = comp_points / max`); `_score_narrative` names ≤ 2 strongest (fill ≥ .60) + ≤ 2 weakest (≤ .35): `Carried by … ; held back by …`. Punt-saves-consistent: low SVHD / low HR% NOT surfaced as weaknesses; RP `Role` omitted (pre-existing, unrelated to the QS/W addition below). **HR/ISO power dedupe (`_hit_clauses`):** when ISO is strong and HR weak (same "power" concept), the HR weakness clause is dropped (symmetric for the reverse); the strength always survives. **SP narrative includes QS/W clauses** (added alongside the SP component rebalance below) — `_sp_clauses`'s `maxes` dict tracks the live SP cap values exactly (K 20, RunPrev 26, WHIP 20, Contact 8, QS 18, W 8), and `add("QS", …)`/`add("W", …)` name the modeled QS rate and win total the same way `add("K", …)` etc. do.
- **Wired into all Score badges:** the 7 tables (Hitter Recent Form, Pitcher Recent Form, My Upcoming Starts, My Relief Pitchers, FA SP, FA RP, FA Hitters) plus Positional Breakdown (**starter** anchor `poss` + drop-candidate `posw` (small pill) + best-FA `posfa`, role-aware via `p["ptype"]`, `colspan=4`). **HR% drivers are in the expanded hitter panel** (`_hitter_score_breakdown`) as a trailing muted `<div>` via `_hrp_driver_str(row)`.
- **Injury context (`_injury_context`, in `fantasy/analytics.py`):** the panel appends an amber **⚠ Injury:** line (after the badge-context block) naming WHICH side of the IL the player is on and HOW long/bad the absence is — 60-day IL / OUT (deeply discounted) · 15-day / 10-day IL (short-term, lightly discounted) · DTD (barely). Prose severity mirrors `trades._IL_TVAL_MULT`, so the panel and the trade-value discount tell one story. The tier read comes from `ESPN_Status` → `FreeAgentInjuryStatus` (map `_INJURY_CTX`); an IL-slot player with a stale/blank status still gets a generic IL note via the `_on_il` fallback. **Enriched with the body part + specifics + expected return** (`_injury_detail_str` → "Neck — Inflammation; exp. return Aug 13", the SAME shape Roster Alerts shows) from the row's `InjuryBodyPart`/`InjuryDetail`/`InjuryReturnDate` — attached at fetch time by `fetch_data.attach_injury_notes` (ESPN's PUBLIC injuries API, a separate endpoint from the fantasy status enum; see `docs/fetch_pipeline.md`). `""` for a healthy player. Wired **inside both `_hitter_score_breakdown` and `_pitcher_score_breakdown`**, so it appears on EVERY score-pill dropdown — digest tables, Trade Radar / Pending Trades cards, and Trade Lab (baked into the serialized prose). The body-part detail is snapshot-sourced, so it degrades to tier-only prose under `--no-refresh` on an old snapshot. (The dashboard has no tap-to-expand panels, so it's simply N/A there.)
- **Badge context (why each chip fired):** the panel appends a muted block explaining each tactical badge the row earns (exact chip + triggering stat). Hitters — `_hit_badge_context(row, hit_pctile, cap=None)` (SAME predicates/order/cap as `hitter_badges`), appended inside `_hitter_score_breakdown` (takes a `hit_pctile` param, threaded from all 3 call sites). SP — `_sp_badge_context(row, qs_fires, k_fires, two_start_n, recent_era=None)` fed the fire flags + L15 ERA (`p15r.get("ERA")`) **at the My Upcoming Starts + FA SP sites only, NOT in the shared `_pitcher_score_breakdown`** (else QS/5K+/⚠ context would show in Pitcher Recent Form where no chip renders). Explains QS / 5K+ / 2 + the **⚠** chip. Reuses `_hit_badge`/`two_start_badge()`/`blowup_badge()` so chips are byte-identical to the table.

### Recent Form columns & KPI
- Both `build_pitcher_hot_cold_section` ("Pitcher Recent Form") and `build_hot_cold_section` ("Hitter Recent Form") take a `best_recent_*` index and render a role-aware Score badge (pitcher → `_score_p`, hitter → `_blend(hitter_score)`) PLUS a `recent_form_cell` (`fantasy/analytics.py`) column — the same shared cell used in My Upcoming Starts / FA SP / FA Hitters / My-FA Relief Pitchers, so a player's icon reads identically everywhere.
- **Roster KPI hot/cold counter:** the "Roster" KPI tile counts my ENTIRE roster — hitters AND pitchers (SP + RP, role auto-detected) — via `roster_hot_cold_counts`, which calls `_hitter_recent_form`/`_pitcher_recent_form` per rostered player and buckets `tag in ("hot","warm")` → hot, `tag in ("cold","cool")` → cold. This is the SAME comparison + thresholds (`_FORM_HOT_DELTA`/`_FORM_WARM_DELTA`) the two Recent Form section subtitles count, so the whole-roster KPI and the per-role subtitles can never drift apart (previously three independently-tuned raw-stat threshold sets). Tile label is "Roster" (whole team).

### Score cascade (`best_recent_p` / `best_recent_h`)
Built in `build_email` by merging `{**rec_p_fp, **p7, **p15, **p30}` (pitchers) and `{**rec_h, **h7, **h15, **h30}` (hitters) — later dicts win, so 30d FP > 15d FP > 7d FP > Baseball Ref. Passed to `_blend` and `positional_breakdown`.

### positional_breakdown viable filter
FA pool per position excludes benchies. SP: `GS >= _pit_viable_min("SP","GS")`. RP: `ESPN_GP >= _pit_viable_min("RP","GP") or IP >= _pit_viable_min("RP","IP")`. Hitters: `OPS > 0.200 or R+RBI > 5`. FA quality (`fa_quality`) = avg blended score of top-3 viable FAs. Scarcity: `< 50` scarce (RED), `< 60` moderate (YELLOW), `>= 60` deep (MUTED).

### positional_breakdown ranks on TOP-K STARTERS, not the mean of all eligible (no phantom needs)
`my_avg`/`rank` per position = the average of a team's **top-K players** at that spot, K = `POS_STARTERS` (`{C:1,1B:1,2B:1,3B:1,SS:1,OF:3,SP:4,RP:3}` ≈ the active-lineup count). It is **NOT the mean of every eligible player** — that let bench/utility depth create phantom needs (a backup catcher carrying 1B eligibility, or a cold bat parked behind a starter, dragged a position's average into the bottom third even when the actual starter was strong; the "1B need with Olson at 1B" bug → NOTES). `_starter_avg(scores, k)` slices the top-K of each team's eligible scores (my team AND every rival, so the league rank stays fair) before averaging. Feeds the digest Positional Breakdown, `_roster_suggestion`'s BAT bullet need detection, and Trade Radar / Trade Lab `need_pos` (all read `rank`/`my_avg`). `worst_player` (the implicit drop target) is still the genuine weakest non-IL body, unaffected.

**Positional/role-logic — ONE convention, everywhere.** The "upgrade / represents this position" reference is **starter quality (`my_avg`, top-K)**, NOT the weakest eligible *body* (`worst_player`). **Canonical rules:** need = bottom-third rank (`rank ≥ n − round(n/3) + 1`); rank/my_avg = top-K starters; upgrade = vs `my_avg`; a multi-eligible bat counts at his **scarcest** slot for *value* (`_POS_SCARCITY`) but **every** slot for *depth* (`_my_position_counts`) — that split is intentional and consistent.

**Digest Positional Breakdown render — leads with the STARTER, not the weakest body.** The first column is **"My Starter"** (the rank-defining anchor = `pos_data[p]["starter"]` = top-scored `my_p`), with the weakest eligible body shown beneath as an explicit muted **"drop candidate"** sub-line (`small=True` pill) — and *only* when it's a different player (a 1-deep position has nothing to drop). Header: **"Best FA Available · ↑ = beats my starter"**. `positional_breakdown` returns a `"starter"` key alongside `worst_player`/`my_avg`. (The dashboard "Weakest Spots" tile stays worst→FA framed — it's need-gated + action-oriented — with its green already honest vs `my_avg`.)

### Dynamic volume benchmarks (no hard-coded IP/AB/GS minimums)
"Full-time" thresholds are derived from the live snapshot each run so they scale with the season. Two builders, both called once at the top of `build_email`:
- `compute_ab_benchmarks(hitters)` → `_AB_BENCH[window]` = `_AB_LEADER_FRAC` (0.62) × the window's p95 (leader) AB. Consumed by `_ab_opportunity_mult` in `hitter_score`. `_FULLTIME_AB` is a cold-start fallback only.
- `compute_pitcher_benchmarks(pitchers)` → `_PIT_BENCH[(window, role)]` = leader IP/GS/GP (p95) per role, `_is_sp`-split. `_ip_reliability_mult` uses `_IP_RELY_FRAC` (0.20) × leader IP for the row's window+role. `_pit_viable_min(role, stat)` uses `_GS_VIABLE_FRAC`/`_GP_VIABLE_FRAC`/`_IP_VIABLE_FRAC` (0.17/0.30/0.38) × the season leader. `_PIT_FALLBACK` holds cold-start constants.

### Data-derived league averages (`_LG` / `compute_league_averages`)
Called once in `build_email` next to the benchmark builders; writes `_LG` with `ops` (full-time regulars), `team_ops` (mean opponent OPS faced), `team_k`, and starter `era`/`whip`/`k_pct`/`ip_per_start`/`barrel_allowed` from qualified YEAR rows. Consumers read `_LG.get(key) or <old literal>`. `qs_probability` stays calibrated because the intercept `38` and multipliers are fixed. fetch_data derives its own `LG_OPS` for wRC+. ONLY genuine league averages live in `_LG`; calibration/scaling constants (score spans/floors, park factor, `IP*4.3`, `compute_hr_probability` weights) do NOT.

## Scoring functions (send_digest.py)

- `_is_sp(r)` → bool. Usage-based SP/RP detection (see gotcha).
- `_blend(r, score_fn, idx_recent, w=None)` → blended score. `_BLEND_W = 0.35` (35% recent + 65% season) — single source for math + tooltip. `idx_recent` is `best_recent_p`/`best_recent_h`. Applies to hitters + SPs; RP `rp_score` never blended.
- `hitter_score(r, _parts=…)` / `pitcher_score(r, _raw=…, _parts=…)` / `rp_score(r, _raw=…, _parts=…)` → `_parts=True` returns `(components_dict, multiplier)`. Component insertion order == display order (single source for tap-to-expand).
- `_score_p(r, idx_recent=None)` → canonical role-aware pitcher score. SP → `_blend(r, pitcher_score, idx_recent)`; RP → `rp_score(r)` unblended. Used by every pitcher Score display/sort.
- `_starts_this_week(r, today, week_end)` → int. Upcoming starts within the matchup week (from `PSP_Dates`; falls back to scalar `PSP_Date`). Drives the two-start `2` badge and best-FA-SP preference.
- `save_role_watch(pitchers, my_team, claimed)` → `(emerging, fading)` (see gotcha).
- `classify_categories(matchup, weekly_avgs, days_elapsed, remaining_proj, matchup_days=7)` → `{cat: (proj_res, tier)}` (see gotcha). Pass `matchup_days=matchup_period_days` for 2-week periods.
- `compute_weekly_std(roto, current_week)` → per-team/per-cat stddev; `_cat_win_prob(pm, po, cat, sigma, remaining_frac)` → `(p_win, p_tie)` (see Category Pulse gotcha).
- `opponent_week_intel(pitchers, hitters, opp_team, best_recent_h, today, week_end)` → dict (starts, two-start pitchers, hot hitters) for the Opponent This Week block. None when `opp_team` empty.
- `pitcher_score(r, _raw=False)` → 0–100. **RP branch** components (unchanged): K/WhiffPctile (28), ERA/xERA (28), WHIP (20), contact-quality/BarrelPct+xwOBA (0–12), Role bonus (SVHD+W+IP/G, ~23). **SP branch** components (rebalanced — see "Cross-role score/value normalization" below): K/WhiffPctile (20), ERA/xERA (26), WHIP (20, unchanged), contact-quality (0–8), **QS via `qs_probability` (0–18)**, **W (0–8)** — SP no longer gets a flat durability "Role" bonus; QS/W replace it as real scored-category credit. Small-sample penalty via `_ip_reliability_mult` (IP-relative to the role/window leader, not a fixed 20). Calibrated `s * A + C` with `(A,C) = _SCORE_CALIB["sp"]` (re-anchored live — see below). `_raw=True` returns pre-calibration.
- `rp_score(r, _raw=False)` → 0–100. SVHD de-emphasized to ~15% (punt-saves). Components from ESPN season counts: SVHD (15), K (26), W (15), ERA/xERA (16), WHIP (12), IP/G (8), contact-quality (0–8). **Now applies `_ip_reliability_mult`** (previously SP-only — a 9-20 IP reliever no longer maxes out on a tiny sample). Calibrated `s * A + C` with `(A,C) = _SCORE_CALIB["rp"]`. My Relief Pitchers picks best dataset per player (YEAR → 30 → 15 → 7).
- `rp_score_recent(rec, season_row)` → 0–100 or `None`. **Display-only** rate-based short-window companion to `rp_score` for the score-pill dropdown's `season | 30-day` header (headline scores/sorting untouched). Paces the window's FP counting stats (K & W per-IP, SV+H per-appearance) to the player's season-equivalent IP, then reuses `rp_score` on a synthetic ESPN-free row → same static caps + same live `_SCORE_CALIB["rp"]` as the season number. `None` on thin/absent windows (`< 5` window IP / zero season IP). Nudges a scoreless window's `0.00` ERA/WHIP → `0.01` to dodge `rp_score`'s falsy-zero `or 5.0`/`or 1.5` blowup fallback. Season-vs-rate rationale: RP and SP are BOTH calibrated to 50/80 so the season score is NOT inflated cross-role — the volume basis is intentional roto value, and the rate view belongs in this recent number, not the headline. (`_ip_notation_to_dec` = local baseball-notation→decimal so scoring.py stays a low leaf.)
- **Live SP/RP/hitter recalibration — `compute_score_calibration(pitchers, hitters)`** (rationale → NOTES): SP/RP/hitter calibration constants are **re-derived every run** from the snapshot's raw-score distribution (same p50→50/p90→80 solve + qualified pool as `recalibrate_scores.py`: SP = `_is_sp` + IP past `_IP_RELY_FRAC` of the leader; RP = `_pit_viable_min` on GP or IP; hitter = AB ≥ 30% of the full-time AB benchmark (the same floor `prepare_scoring`'s `hit_pool` uses); all YEAR rows), written to the module global `_SCORE_CALIB = {"sp":(A,C), "rp":(A,C), "hit":(A,C)}` that `pitcher_score`/`rp_score`/`hitter_score` read. **Called by `prepare_scoring` right AFTER `compute_pitcher_benchmarks`/`compute_ab_benchmarks`** (`_raw` scores read `_PIT_BENCH`/`_AB_BENCH` — order matters; `prepare_scoring` is the ONLY place the sequence should live). Runs BEFORE `compute_league_averages`, so the SP branch's `qs_probability` call inside this one-time raw-score solve sees an empty `_LG` and falls back to its own hardcoded league averages — a small precision loss on the calibration anchor only, not on live per-player displayed scores (computed later, after `_LG` is populated). **SMALL-POOL FALLBACK:** a role whose pool is below `_MIN_CALIB_POOL` (30) or degenerate (`p90 <= p50`) keeps the hand-tuned defaults (SP 1.5070/-44.3346, RP 1.6543/-28.0645, hit 1.587/-5.2). `recalibrate_scores.py` is now a manual inspection tool + the source of those fallback literals — update the defaults from it if the component mix changes materially. **Hitter score is now live-recalibrated the same way as SP/RP** (previously a fixed `s * 1.587 - 5.2`, which let the hitter pool's actual p90 drift to ~84 instead of 80 — see "Cross-role score/value normalization" below).
- `hitter_score(r, _raw=False)` → 0–100. Prefers wRC+ over OPS. Uses xwOBA, sprint speed, Barrel%, ISO, HR_Probability. Opportunity multiplier (`_ab_opportunity_mult`): raw score scaled by AB vs a full-time benchmark (floored `_AB_FLOOR = 0.40`, capped 1.0). Calibrated `s * A + C` with `(A,C) = _SCORE_CALIB["hit"]` (re-anchored live — see above). `_raw=True` returns pre-calibration. Displayed everywhere as `_blend(r, hitter_score, best_recent_h)`.
- `qs_probability(r)` → 1–99. Calibrated league-avg ~38%, ace ~75%. Uses IP/G (not IP/GS). Also now feeds `pitcher_score`'s SP-branch QS component and `_trade_value`'s QS credit (see below) — previously computed but only ever displayed.

### Cross-role score/value normalization (session ~83)
A full audit found the three "universal 0-100" role scores and `_trade_value` (`_tval`) were not
actually comparable across roles at the top tail, plus SP had no mechanism to credit QS — a real
scored roto category with no per-player raw stat anywhere in the data model. Four fixes, applied
together:
1. **Hitter score lost its fixed `s*1.587-5.2` constant** in favor of the same live
   `compute_score_calibration` re-anchoring SP/RP already had (p90 was drifting to ~84, not 80).
2. **`rp_score` now applies `_ip_reliability_mult`** — previously only `pitcher_score`'s SP path
   did, letting 9-20 IP relievers max out WHIP/RunPrev on a tiny sample.
3. **`pitcher_score`'s SP branch credits QS (0–18) and W (0–8)** via `qs_probability(r)`, trimming
   K (28→20) and Contact (12→8) to make room (WHIP/RunPrev nearly untouched — they already
   reflected real results correctly). A durable, QS-reliable innings-eater with modest strikeout
   stuff (the "contact-managed, not lucky" profile) now scores meaningfully higher; a pure-stuff,
   thin-QS arm scores somewhat lower. `_sp_clauses` (`fantasy/analytics.py`) was updated in the
   same change — its `maxes` dict and `add()` calls are the ONLY place the new QS/W credit gets a
   narrative sentence in the tap-to-expand breakdown.
4. **`_trade_value` gained a role-aware pitcher cat list** `_TRADE_PIT_CATS = ["SVHD","QS","K","W",
   "ERA","WHIP"]` (`fantasy/analytics.py`, replacing the RP-only `_FA_RP_CATS` it used to share
   across both roles) with QS proxied via `qs_probability` at weight `_TRADE_QS_W=0.65`
   (`fantasy/trades.py`, between `_TRADE_SVHD_W`'s 0.35 and a raw cat's 1.0 — a durability/skill
   signal, not role-noise, but model-derived rather than a box-score count). `_cat_value`
   (`fantasy/analytics.py`) also runs ERA/WHIP through the existing IP-weighted shrinkage
   (`_effective_era`/new `_effective_whip`) before scoring them into `_tval`, so a tiny-sample
   rate-stat outlier (same small-IP relievers as fix 2) can't dominate a percentile pool. This
   also refines the FA-table "Cats" strength chips (`player_cat_strengths` calls `_cat_value` too)
   for both SP and RP — intended, not scope creep.

Net effect: SP/RP/hitter p50/p90 land much closer together (all ≈50/80), a QS-durable innings
type like Nick Martinez scores/values meaningfully higher without any ERA/WHIP discount, and
tiny-sample RP fluke seasons score/value lower. Mid-tier (70/80 threshold) players were
confirmed pre-change to already be fair across roles — this was specifically a top-tail fix and
shouldn't meaningfully move ordinary/mid-pack players.
- `_fmt_ip(ip_decimal)` → baseball IP notation. `whole = int(d); outs = round((d-whole)*3); if outs>=3: whole+=1, outs=0`.
- `_proj_line_html(r)` / `_proj_line_vals(r)` → `IP · ER · K`. ER = `raw_er * opp_factor * park_factor`, `raw_er = era_reg * IP_per_G / 9`; **`era_reg` = row ERA regressed toward `xERA` (falls back to `_LG["era"]` when missing), IP-weighted: `(ERA*IP + target*_ERA_REG_PRIOR_IP)/(IP + _ERA_REG_PRIOR_IP)`, `_ERA_REG_PRIOR_IP = 40`** (backtest-confirmed ER win; residual −0.33 bias is structural blowup-skew — see NOTES). `opp_factor = clamp(opp_ops / LG_team_ops, 0.80, 1.20)`; `park_factor` = 0.97 home / 1.03 away. K opponent-adjusted via `Team_K_Value`. IP = `IP_per_G` (backtest-confirmed best predictor at r=0.27; IP_per_GS and pitch-count predictors rejected).
- `hot_cold_cell(display_str, delta, hot_thresh=…, warm_thresh=…, no_data_title=None, td_style=TDC)` (`fantasy/ui.py`) → `<td>` with a colored, already-formatted string + 🔥/↑/❄/↓ icon from a PRECOMPUTED delta (score-space, computed upstream by `recent_form_cell`). A pure rendering primitive — no scoring knowledge, never formats a number or reads a stat itself. When `display_str`/`delta` is `None` and `no_data_title` is set, renders `—` with a dotted underline + hover tooltip. Optional `td_style` so the compacted pitcher tables' Recent Form cell matches. The window tag is a SEPARATE `<td>` — `_window_cell(label, td_style=None)` (`fantasy/ui.py`) — a dedicated 7px-pill column naming which window (30d/15d/7d) backed the row, since it's a different question than the value/icon. `recent_form_cell(r, role, idx_recent, td_style=None)` (`fantasy/analytics.py`) is the actual call site every table uses — it resolves `_hitter_recent_form`/`_pitcher_recent_form`, builds the `"{season} | {recent}"` Score-pair string (the SAME numbers the tap-to-expand header shows), and returns BOTH cells concatenated (`hot_cold_cell(...) + _window_cell(...)`) as one string, so a caller's `+ recent_form_cell(...) +` splice silently inserts two `<td>`s, not one — every table's header/colspan must budget for both. **Column-order rule: Recent Form and its window-tag column are always the LAST TWO columns before Score, in every table** — any table-specific extra column (K%, Cats, HR%, Whiff%) goes BEFORE Recent Form, never between it and Score. The window `<th>` carries a 📅 glyph + hover title, never blank.
- `band_divider(label, color, anchor=…)` → full-width band boundary `<div>`.

### QS / 5K+ / 2 (two-start) badges (My Upcoming Starts + FA SP)
- `2` (blue/`ACCENT`, `_starts_this_week ≥ 2`), QS (cyan), 5K+ (yellow) render next to the pitcher name via `two_start_badge(title)` / `qs_badge(ip_g, er, row=None)` / `k5_badge(k, row=None)` — the QS/5K+ helpers wrap `_hit_badge` so the chip is byte-identical to the hitter badges + carries a hover `title` naming the projected line. The dashboard My Pitching tile mirrors the tooltips on its own 8px chips. **QS and 5K+ purely annotate the projected line** — driven ONLY by `_proj_line_vals(r)`, NOT season rates: QS = `_proj_is_qs` (6+ displayed IP & ≤ 3 ER via `_fmt_ip` rounding), 5K+ = projected `K ≥ 5`. The **QS% column** shows season QS probability separately. **The QS tooltip + its `_sp_badge_context` dropdown line both name the run-prevention analytic** via the shared `_qs_stat_clause(row)` (`xERA`, falling back to raw `ERA`; empty when neither present) — pass `qs_badge` the row at every site. **The 5K+ tooltip + its dropdown line both name the K-skill** via the shared `_k5_stat_clause(row)` (raw `WhiffPct` → `WhiffPctile` → `Kpct_P`, first available) — pass `k5_badge` the row at every site. Display-only — never fed into `pitcher_score` (would double-count `WhiffPctile`).
- **FA SP badges are unconditional:** they fire on the projected line wherever the pitcher appears. `thin_days`/`my_starts_by_day` still drive the ⚑ per-day "N my starts" banner and Week-at-a-Glance bullet 2.

### Blowup-risk (⚠ / low-floor) flag — starters only
A burnt-orange (`ORANGE`) glyph-only **⚠** chip next to a startable arm's name flags a **low floor** (disaster-start prone) **for his current upcoming start**. **`blowup_risk(r, recent_era=None)` → 0–100** (higher = lower floor); **`_is_blowup_risk(r, recent_era)`** = `_is_sp(r) and blowup_risk ≥ _RISK_MIN` (55 → ~10–12% of startable arms); **`blowup_badge(r, recent_era)`** renders the chip (empty when not flagged, hover title = worst 2–3 drivers via `_risk_drivers`). **DISPLAY-ONLY — never folded into `pitcher_score`/`_score_p`** (an independent floor warning, not a quality knock; a high-ceiling arm can still be risky). Four skill drivers league-anchored via `_LG`, tunable via `_RISK_*`: WHIP, K%/`WhiffPctile` (escape hatch), **`_effective_era(r)`** (ERA regressed toward xERA, SAME `_ERA_REG_PRIOR_IP=40` shrinkage as `_proj_line_vals`), `HardHitPctAllowed`. Two escalators layer on top of that skill base — **NOT** additional weighted drivers, so `_RISK_W`'s calibration is untouched:
  - **Recent form** (`_RISK_RECENT_W=0.25`) — **ADDITIVE-ONLY**: a cold `recent_era` (L15) RAISES risk, a hot stretch does NOT lower it (a good week doesn't cure a structurally shaky arm's floor).
  - **Opponent matchup** (`_RISK_OPP_W=0.20`, `_RISK_OPP_SPAN=0.060`) — **SYMMETRIC**, unlike recent form: reads `Team_OPS_Value` straight off the row (already merged onto every pitcher row scoped to his current earliest upcoming start, no new plumbing needed — same field `qs_probability` already reads) vs `_LG["team_ops"]`. A tough lineup for THIS start raises risk; a genuinely weak one lowers it — this is what makes the flag answer "will he implode in this matchup" rather than just "is he generally shaky." Symmetric (not additive-only like recent form) because it's a stable, large-sample signal, not small-sample noise the asymmetric guard exists to protect against — same design precedent as `qs_probability`'s own symmetric opponent term (`fantasy/scoring.py:404-405`). Surfaces as a 6th potential driver string in `_risk_drivers` (`"{opp:.3f} opp OPS (tough matchup)"`) when it's risk-raising. **Two-start pitchers:** only the scalar earliest start's opponent is reflected (no per-start rendering infrastructure exists to split a 2-start week into two separate risk reads — same limitation `Team_OPS_Value`/`qs_probability` already have).
  - **Trade surfaces are matchup-neutralized:** `fantasy/trades.py`'s `_trade_skill_badges` and `trade_lab.py`'s `_serialize` both call `blowup_badge({**r, "Team_OPS_Value": -1})` so a trade card's ⚠ doesn't flicker based on who's next on the schedule — mirrors the existing `_sp_qs_season` precedent (`fantasy/scoring.py:981-986`, `qs_probability({**row, "Team_OPS_Value": -1})`), which strips the same term for the same reason on the season-skill badge.

**Wired:** inline chip in My Upcoming Starts + FA SP (`blowup_badge(r, p15r.get("ERA"))`), dashboard My Pitching (8px) + FA Radar Starters (9px); the tap-to-expand dropdown explains it via `_sp_badge_context(..., recent_era=p15r.get("ERA"))`. **Steer:** best-FA-SP pick deprioritizes flagged arms; `_roster_suggestion` pitch-stabilizer pool excludes them (`and not _is_blowup_risk(r)`).

### Pitcher recency: disabled confirmation flag + validated bounce-back replacement
`pitcher_recency_flag` (the pitcher analog of `hitter_recency_flag` — did a recent-window stat
already confirm the season $/▼ regression call?) is **DISABLED as of 2026-08-06 — always returns
`None`.** A walk-forward backtest (`backtest_projections.py`, per-pitcher MLB game logs, cumulative
stats strictly through the day before each start) found its confirmation direction **inverted**:
starts flagged `'declining'` (recent form worse than season xERA) beat their next-start projection
MORE often than a random start, and starts flagged `'improving'` beat it LESS often — both
backwards. A 9-way sweep (3 recent metrics [raw ERA / FIP / kwERA] × 3 windows [15/30/60 calendar
days]) ruled out two easy explanations: **not small-sample noise** (wider windows made raw-ERA/FIP
results WORSE, not better — Pearson r on signed-gap-vs-residual went from −0.067 at 15 days to
−0.282 at 60 days) and **not "wrong stat"** (FIP, which strips BABIP/strand-rate luck, was no
better than raw ERA at the same window; only kwERA — K%/BB% only, no HR term — got close to zero,
never positive). Conclusion: at these sample sizes, a recent-window-vs-season-xERA gap for a
**starting pitcher** is dominated by regression to the mean, not trend persistence — gating on
"diverges sharply from the skill anchor" selects for noisy extremes almost by construction.
Contrast with `hitter_recency_flag`/`hitter_recency_severity` (unaffected, still live) — never
itself backtested this way, so it's "not yet disproven," not "proven safe."

Every caller of `pitcher_recency_flag` already treats a non-`'declining'`/`'improving'` return as
the safe no-confirmation state (hollow chip, no standalone arrow), so disabling it at the source
was enough to stop the bad advice with **zero call-site changes** — `fantasy/scoring.py`
(`pitcher_regression_badge`, the standalone early-read arrow), `fantasy/analytics.py`
(`_pitcher_badge_context`), `fantasy/trades.py` (`_trade_skill_badges`), `dashboard.py` (`_reg_chip8`
+ the Trade Radar tile) all degrade automatically.

**The replacement — `pitcher_bounceback_flag(season_row, recent_row)` / `pitcher_bounceback_badge`**
(`fantasy/scoring.py`) — is the OPPOSITE-polarity signal the same backtest actually validated
(n up to 409 flagged starts, FIP metric, consistent across window sizes): a recent FIP notably
**worse** than season xERA predicts a **bounce-back** next start (green **↑**, glyph
`&#8593;`), not a continued slide; a recent FIP notably **better** than xERA predicts a
**letdown** next start (red **↓**, glyph `&#8595;`), not continued excellence. `_recent_fip`
computes `(13·HR + 3·BB − 2·K)/IP + _FIP_CONSTANT` (`_FIP_CONSTANT = 3.10`, a fixed relative
constant — no HBP term, since FantasyPros' short-range pitcher scrape (the dominant tier in
`best_recent_p`) doesn't carry one, matching the common "simple FIP" convention). Gated on
`_BOUNCEBACK_MIN_IP = 20` (the 15-day tier's thinner sample showed a much weaker backtest effect
than the 30-day tier) and `_BOUNCEBACK_GAP_ERA = 1.50` (same threshold scale as the disabled flag).
**Deliberately a SEPARATE badge, not a repolarized `pitcher_regression_badge`** — this is a
THIS-START confidence read (like ⚠ blowup risk), not a durable season-value signal like $/▼/▽, so
it needs its own glyph/color and its own trade-neutrality story (pass `idx_recent=None`, or simply
never wire it into a trade surface — mirrors `blowup_badge`'s `Team_OPS_Value:-1` neutralization
in spirit, but by omission rather than input-stripping since there's nothing to neutralize when the
badge is never called there). SP-only (`_is_sp` gate, mirrors `blowup_badge`).

**Wired:** My Upcoming Starts + FA SP (`_sp_badge_context` also explains it in the tap-to-expand
dropdown), Today's MLB Games, Weekly Game Plan cards, dashboard My Pitching (8px) + FA Radar
Starters. **Deliberately NOT wired:** Trade Radar / Pending Trades / Trade Lab (`fantasy/trades.py`,
`trade_lab.py`) — stays out of the season-value trade engine entirely, same reasoning as why ⚠
gets matchup-neutralized rather than removed there (a trade shouldn't flicker based on a next-start
read), except here there's no season-durable core to preserve, so omission is simpler than
neutralization. Glossary: "Buy-low / sell-high" group, own entry ("↑ / ↓ bounce-back (pitchers)").

### Hitter tactical badges (PWR / SB / BUY / SELL)
Glance flags next to a **hitter's** name, mirroring the SP badges — **display-only, never folded into any score** (rationale → NOTES). Shared source `hitter_badges(row, hit_pctile=None, cap=None)` in send_digest.py (dashboard imports `sd.hitter_badges`), chips via `_hit_badge(text, color, title)`. Each carries a hover `title` naming the justifying stat. Priority **PWR → SB → BUY/SELL**; `cap=None` → all applicable badges show (`_hit_badge_context` matches with the same order + `cap=None`). Four flags (tunable constants, ~10–25% of the qualified YEAR pool):
- **PWR** (`PURPLE`) — `HR_Probability ≥ _PWR_HRP_MIN` (0.23, ~7% of pool). Title = `_hrp_driver_str(row)`.
- **SB** (`SILVER`, "Quicksilver") — SB percentile `≥ _SB_PCTILE_MIN` (0.80) via the `build_cat_percentiles` SB pool AND `SprintSpeed ≥ _SB_SPEED_MIN` (27.0, skipped when missing). Needs the `hit_pctile` pool — silently doesn't fire when `None`.
- **$** (buy-low, `GREEN`) / **▼ or ▽** (sell-high, `RED`) — Statcast expected-vs-actual regression, mutually exclusive; `SLG = ISO + AVG`; buy = `xBA−AVG ≥ _XREG_BA` (0.020) AND `xSLG−SLG ≥ _XREG_SLG` (0.030); sell = both inverted. Requires `xBA/xSLG/AVG/ISO > 0`. **This badge is season-actual vs season-expected only** — see the recency signal below for a recent-window read. **HOLLOW vs SOLID glyph (session addendum):** a filled `▼` reads as "this is already happening," but the season flag alone is a *prediction*, not an observed decline — confusing when the only thing that's actually confirmed-or-not is the separate confirmation-arrow check below. So the sell-high glyph itself now carries that distinction: **`▽` (hollow, U+25BD)** = the season-level prediction only, not yet corroborated by his recent games (the default/most common case); **`▼` (solid, U+25BC)** = the SAME sell-high call, but recency has already confirmed it (see below) — reserved for that one case. The buy-low `$` glyph is unaffected (a "buy" signal is a positive framing either way, not subject to the same "did it already happen" ambiguity). **CONFIRMATION ARROW (`_CONFIRM_UP`/`_CONFIRM_DOWN`, now shared with pitchers — moved to `fantasy/scoring.py` since `pitcher_regression_badge` needs them too; analytics.py inherits them via its star-import):** the season-level flag says a player *should* regress; it says nothing about whether that's actually started showing up. When the optional `idx_recent` param (`hitter_badges`/`_hit_badge_context`, both default `None` → hollow `▽`/no arrow) resolves a recent-window row AND `hitter_recency_flag(row, recent_row)` agrees with the direction — `'improving'` for `$`, `'declining'` for `▼` — the badge appends a plain-text diagonal arrow (`&#8599;`/`&#8600;`, chosen over a checkmark/📈-📉/star/bold-arrows from a rendered side-by-side mockup) AND (for the sell-high case) switches the glyph from hollow to solid: `$↗` / `▼↘`, same GREEN/RED as the base chip (plain glyphs, not emoji, so they inherit the chip's own color rather than carrying a fixed color of their own). Badge stays hollow (no arrow) when there's no recent row or the recency read is `'noise'`/unreliable. **CONTRADICTION TAIL:** when the recency read instead disagrees with the season direction — the recent window is trending the *opposite* way — the badge glyph stays hollow but `_hit_badge_context`'s tooltip gains a "heads up: his recent games are trending the OTHER way … worth watching before acting on this" clause. **Validated rare on the live pool at the point of shipping** (1 of ~134 season-flagged hitters actually confirmed — Luis Rengifo, `buy`+`improving`; 11 contradicted, e.g. CJ Abrams `sell`+`improving`; 49 read as noise; 73 lack enough recent AB to evaluate at all), so the solid `▼`/arrow reads as a strong, occasional signal on top of an already-fired season badge, not routine. `_tsell`/`_tbuy` (the underlying booleans used for trade-pool gating) are **untouched** — this is display-only. Wired at all 13 real `hitter_badges` call sites across `send_digest.py`/`dashboard.py`/`trade_lab.py` (the 14th, `fantasy/trades.py`'s `_trade_skill_badges`, runs with `regression=False` so `$`/`▼`/`▽` never fires there — nothing to wire). **Tooltip names the recent number (`_rec_avg_slg_str`, `fantasy/scoring.py`):** the confirmed/contradicted/standalone-arrow tooltip text used to say "already trending up/down in recent games" without ever naming the recent AVG/SLG that's actually doing the confirming — only the season xBA/xSLG anchor. `_rec_avg_slg_str(recent_row)` formats the RAW (unshrunk) recent-window AVG/SLG (`"recent AVG .316/SLG .592"`, `""` when unavailable) and is appended into every arrow-bearing tooltip in `hitter_badges`/`_hit_badge_context` — so hovering the arrow shows the actual number, not just the verdict.
- **STANDALONE TREND CHIP (bare `↗`/`↘`, no `$`/`▼`):** the confirmation arrow above only shows on top of an *already-fired* season `$`/`▼` — and that intersection is nearly empty by construction (a season aggregate is slow-moving; by the time it's flagged, the most-recent slice often isn't still moving the same way). Checked independently of the season flag, though, `hitter_recency_flag` alone clears its threshold for real players *right now* (25 of 262 eligible hitters on the live pool at ship time — 18 `improving`/7 `declining` — 13 of those also pass `hitter_badges`'s stricter `AVG>0 and ISO>0` gate). So when the season `$`/`▼` did **not** fire but `hitter_recency_flag` returns `'improving'`/`'declining'` on its own, `hitter_badges`/`_hit_badge_context` render a bare `_CONFIRM_UP`/`_CONFIRM_DOWN` chip (same GREEN/RED, no `$`/`▼` glyph) — an early recency-only skill-trend read, ahead of the season aggregate catching up. Mutually exclusive with `$`/`▼` (only one of the two chip families ever shows for a given player) and with the confirmation-arrow/contradiction-tail behavior above. Same `idx_recent`-gated, zero-plumbing wiring — no new call sites, no new threshold constants, `hitter_recency_flag`/`_recency_value_mult` untouched.

### Hitter recency-trust line (tap-to-expand only, no visual badge)
The buy/sell badge above can't say whether a *recent* hot/cold stretch is backed by a real change or is just small-sample luck, because **every Statcast field is a season-cumulative value merged onto all windows uniformly** (`fetch_data.py`'s `get_statcast_expected_stats`/`get_statcast_contact`/`get_sprint_speed` compute once on the season-YEAR rows and back-merge — confirmed identical xBA/xSLG/xwOBA/Barrel_Pct/HardHit_Pct/SprintSpeed across a player's 7/15/30/season rows). `AVG`/`SLG`/`AB` ARE genuinely window-specific (real FantasyPros per-range scrapes), so `fantasy/scoring.py`'s `_effective_avg(season_row, recent_row)` / `_effective_slg(season_row, recent_row)` regress the recent window's actual AVG/SLG toward the season xBA/xSLG (skill estimate), AB-weighted via `_HIT_REG_PRIOR_AB` (100.0) — the exact same shrinkage pattern as `_effective_era`/`_effective_whip` (IP→AB, ERA/WHIP→AVG/SLG). `hitter_recency_flag(season_row, recent_row)` classifies the regressed read: `'declining'`/`'improving'` when the recent direction survives regression (both AVG and SLG gaps clear `_HIT_RECENCY_GAP_BA`(0.030)/`_HIT_RECENCY_GAP_SLG`(0.050) in the same direction — a real signal), `'noise'` when shrinkage pulls the effective rate back near season skill (small-sample-driven — reassuring, not alarming), `None` below the `_HIT_RECENCY_MIN_AB`(15) reliability floor or when season xBA/xSLG is missing. **Display-only, tap-to-expand prose, no new badge glyph** — `_recency_trust_context(season_row, recent_row, tag)` (`fantasy/analytics.py`) renders one muted line (colored RED/GREEN/MUTED per verdict) inside `_hitter_score_breakdown`, gated on `tag` already being hot/warm/cold/cool (reuses `_hitter_recent_form`'s existing Score-delta tag — never fires on a `steady` player). Wired at the single shared breakdown call site, so it appears everywhere the score-pill panel does (digest tables, Trade Radar/Pending Trades cards, Trade Lab) with no extra call-site changes.

**Callers pass the `hit_pctile` pool** (`build_cat_percentiles(_hit_pool, _FA_HIT_CATS)` on the qualified YEAR pool — `build_email`, and dashboard `build_context` as `ctx["hit_pctile"]`). **Digest sites:** Hitter Recent Form (`build_hot_cold_section` takes a `hit_pctile` param), FA Hitters, Positional Breakdown (hitter branch, gate on `p["ptype"]`), **Today's MLB Games** (each involved player, role-picked in `_badges`). **Dashboard sites:** Hitter Recent Form, FA Radar hitters, Weakest Spots (hitter positions). **When adding a hitter surface**, drop `hitter_badges(row, hit_pctile)` after the name with a `hit_pctile` pool in scope.

### Pitcher buy/sell badge (`$` / `▼` / `▽`) — the pitcher analog of the hitter regression badge
`pitcher_regression_badge(row, idx_recent=None)` → glyph-only `$` (buy-low, `GREEN`) / `▼` or `▽` (sell-high, `RED`) chip, `""` when neither. `pitcher_regression_flag(row)` → `'buy'`/`'sell'`/`None`. Signal = **ERA vs Baseball Savant xERA**, gated on `IP ≥ _XREG_ERA_IP` (20). **DISPLAY-ONLY** (never folded into any score), same as the hitter version.
- **DE-BIASED (critical):** xERA runs systematically ABOVE ERA in this data (~+0.33 median), so a raw symmetric threshold over-fires "sell" (37% vs 17% observed). `compute_xera_offset(pitchers)` sets module global `_XERA_OFFSET` = league median `(xERA − ERA)` over the qualified YEAR pool; the flag tests `(xERA − ERA) − _XERA_OFFSET` against `_XREG_ERA` (1.00) → luckier/unluckier **than typical** (~12% sell / 15% buy). **Called by `prepare_scoring` right after `compute_league_averages`** (same calibration block as `_LG`/`_SCORE_CALIB`); 0.0 cold-start default if uncalled.
- **DISTINCT from `⚠` (blowup-risk):** `▼`/`▽` sell-high = MEAN regression / luck (ERA better than deserved, due to rise); `⚠` = single-start TAIL risk / low floor. They share the ERA/xERA input so they can co-fire (a strong "move him" signal) but are not redundant.
- **PITCHER CONFIRMATION (`pitcher_recency_flag`, session addendum — brings pitchers to parity with the hitter confirmation arrow above):** pitchers previously had NO recency check at all, so a pitcher's sell-high badge was always a bare season-level prediction — exactly the "reads as already-declining when it's really just a forecast" problem that motivated the hollow/solid glyph split. `_effective_era_recent(season_row, recent_row)` regresses the recent-window row's ERA toward the season `xERA` anchor, IP-weighted (`_PIT_RECENCY_PRIOR_IP`=25.0 — the IP analog of `_HIT_REG_PRIOR_AB`), gated on `recent_row IP ≥ _PIT_RECENCY_MIN_IP` (8). `pitcher_recency_flag(season_row, recent_row)` compares the regressed value to `xERA`: `gap ≥ _PIT_RECENCY_GAP_ERA` (1.50, ~1.5x the season `_XREG_ERA` bar, same margin-over-season-threshold pattern as the hitter `_HIT_RECENCY_GAP_BA`/`SLG`) → `'declining'` (recent ERA already worse than expected — confirms sell-high); `gap ≤ -1.50` → `'improving'` (confirms buy-low, though buy-low doesn't change glyph); else `'noise'`; `None` below the IP floor or no `xERA`. Sign convention is flipped vs the hitter version because ERA is lower-is-better. `pitcher_regression_badge`'s `idx_recent` param (the `best_recent_p` index) feeds this check: `'declining'` → solid `▼` + `_CONFIRM_DOWN` (`&#9660;&#8600;`); anything else (contradicted, noise, no data, or `idx_recent` omitted) → hollow `▽`. **Tooltip names the recent ERA (`_rec_era_str`, pitcher analog of `_rec_avg_slg_str` above, same file):** every arrow-bearing tooltip (confirmed `▼↘`, standalone early-read arrow) appends the raw recent-window ERA (`"recent ERA 2.10"`) alongside the season xERA anchor, so the hover explains itself with a real number instead of just "already worsening."
- **STANDALONE EARLY-READ ARROW (pitcher analog of the hitter's bare-arrow 4th case, added same session):** `pitcher_regression_badge` computes `rflag` (via `pitcher_recency_flag`) BEFORE checking whether the season flag fired at all, not just inside the sell branch. When `pitcher_regression_flag(row)` is `None` (no season-level buy/sell) but `rflag` is `'improving'`/`'declining'` on its own (and `xERA > 0`), it renders a bare `_CONFIRM_UP`/`_CONFIRM_DOWN` chip — no `$`/`▼`/`▽` — an early skill-trend signal (recent ERA already diverging from his own `xERA` anchor) ahead of the season aggregate catching up. Same mutual-exclusivity as the hitter version (only one chip family ever fires) and same zero-new-call-site wiring (every call site already threads `idx_recent`, so this activates for free). `_pitcher_badge_context` mirrors the same branch so the tap-to-expand prose matches. Unlike the hitter version, `pitcher_regression_badge`/`_pitcher_badge_context` render ONE glyph for one row (not a badge list), so the standalone case lives as an early-return branch rather than a parallel append.
- **Digest sites (all pass `idx_recent=best_recent_p`):** Pitcher Recent Form (`r["srow"]`), Today's MLB Games (`build_todays_games_section`'s new `idx_recent_p` param), Weekly Game Plan, My Upcoming Starts + FA SP (appended to the `start_badges`/`pickup_badges` list beside QS/5K+/⚠), My Relief Pitchers + FA RP (name cell), Positional Breakdown (pitcher branch, both the starter and top-FA cells). Tap-to-expand explained in `_sp_badge_context` (SP sites) and `_pitcher_badge_context` (the shared score-breakdown panel, now also threaded `idx_recent` so its prose never disagrees with the glyph). **Dashboard sites (pass `ctx["best_recent_p"]`):** My Pitching (`_reg_chip8`, 8px, updated with its own inline hollow/solid split), FA Radar SP+RP, Weakest Spots (pitcher rows) — `build_context` calls `compute_xera_offset` so `_XERA_OFFSET` is set there too. **Feeds Trade Radar/Trade Lab/Pending Trades** (`_tsell`/`_tbuy` for pitchers AND hitters — `_trade_player_line` and dashboard's `render_trade_radar` both now check `hitter_recency_flag`/`pitcher_recency_flag` against `best_recent_h`/`best_recent_p` before choosing solid vs hollow, same rule as everywhere else). **When adding a pitcher surface**, drop `pitcher_regression_badge(row, idx_recent=best_recent_p)` after the name (best on a YEAR/season row — the flag needs season ERA vs xERA) — omitting `idx_recent` still works but always renders the hollow, unconfirmed `▽`.

### Recommended-move clipboard badge (📋, `_move_badge`) — cross-references Week-at-a-Glance / Weekly Game Plan onto every other player table
`_move_badge(name, move_registry)` (send_digest.py) → a `TAN`-colored (`#c19a6b`, its own reserved hue) clipboard chip via `_hit_badge`, or `""` when the player isn't involved in anything. Purely informational — never touches a score, ranking, or the drop/eligibility logic in the section above; it only flags that a player is ALSO named somewhere else.
- **`move_registry` is `{PlayerName: [reason, ...]}`**, built once in `build_email` from two structured return values: `_roster_suggestion` and `build_game_plan` each now return `(html, moves)` instead of just `html` — `moves` is a list of `{"name": PlayerName, "reason": str}` records for every add AND every drop they name (populated inside `_move_tail`/`_build_cards`, right where each function already resolves its own drop — not re-parsed from the rendered HTML). `build_email` merges both `moves` lists into `move_registry` via `setdefault(...).append(...)`, so a player named by more than one suggestion (e.g. both a Week-at-a-Glance bullet AND a Game Plan card point at the same drop) gets every reason listed in one tooltip.
- **Computed EARLY, before any player-listing table renders.** `_roster_suggestion`/`build_game_plan` are called once, immediately after `category_classification`/`need_cats` — well before My Upcoming Starts, My RP, Recent Form, or the FA tables build their rows (all of which come later in `build_email`'s code, even though some render earlier in the final HTML via the `top_sections`/`myroster_band`/`transactions_band` assembly lists further down). Their returned HTML (`roster_suggestion`, `game_plan`) is reused unchanged at its original later assembly point — **each function is called EXACTLY ONCE**, since both carry internal `_used_drops`/`slots_left` state; a second call could pick a *different* drop and desync the badge registry from what's actually rendered.
- **Wired at 7 call sites, ALWAYS LAST** among that row's badges (after `inj_tag`, QS/5K+/⚠, regression, hitter tactical, two-start — whatever else fires on that row) so it never competes with a higher-priority signal: My Upcoming Starts, My Relief Pitchers, Pitcher Recent Form, Hitter Recent Form, FA SP, FA RP, FA Hitters. `build_hot_cold_section`/`build_pitcher_hot_cold_section` (separate top-level functions, unlike the inline FA/My-roster tables) take an added `move_registry=None` param threaded from their `build_email` call sites.
- **`dashboard.py`'s `_roster_suggestion` call was updated to unpack the new tuple** (`roster_sugg, _ = sd._roster_suggestion(...)`) — the dashboard's compact Recommended Moves tile doesn't build its own `move_registry` (no FA/roster tables there to badge).
- **Tap-to-expand explained via `_move_badge_context(name, move_registry)`** (send_digest.py, next to `_move_badge`) — same predicate, same `move_registry` lookup, so the score panel explains the 📋 chip like every other badge instead of leaving it as tooltip-only. Appended (via `_badge_ctx_wrap`, one line per reason) onto the breakdown string at each of the SAME 7 call sites, always after that site's other badge-context calls (`_sp_badge_context`/`_winprob_context`/etc.) so it reads last in the dropdown too, mirroring its always-last position in the name cell.
