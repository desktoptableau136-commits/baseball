# NOTES.md — background & rationale

Forensic detail and "why we did it this way" narrative moved out of `CLAUDE.md` to keep that file to actionable rules. Nothing here is required to follow the rules; consult it only when you need the history behind a decision. Rules live in `CLAUDE.md`; this is the "why".

## "Week" → "Matchup" terminology (2026-07-08)

User flagged that labeling everything "Week N" is misleading: ESPN's unit is a **matchup period** (`currentMatchupPeriod`), which is usually 7 days but **14 days for the All-Star break and playoffs**, and the number is a matchup *index* (a 2-week matchup still advances `currentMatchupPeriod` by only 1, so it can diverge from a calendar-week count). His words: *"I'll be looking for a Matchup 15 Roto readout, not just a 'Week' of it."* And the "Weekly Recap" is really the *previous matchup period* recap.

The code already handled the *timing* correctly (everything keys off `matchup_end_date` / `matchup_period_days`, so a 2-week All-Star matchup already spans 14 days) — this was purely a **display-label** fix, so it's low-risk: no snapshot fields, variable names, filenames, or workflow names changed, only user-visible strings. User chose **full consistency** and the term **"Matchup 15"** (matches ESPN's own UI).

Policy (also captured as a CLAUDE.md rule so future edits follow it): every label for the matchup unit → "Matchup {n}" (Roto Rankings, the matchup score panel, Category Pulse, "Matchup at a Glance", KPI "Starts This Matchup", "Opponent This Matchup", the recap title/subject/highlights, Season Trajectory "by matchup", sidebar "…of the Matchup"). **Kept as "week"**: genuine 7-day rolling windows (last-7-days, 7-day OPS, L15 ERA) and the **Lineup Watch**, which is deliberately the current *calendar* week (Mon→yesterday) — a real distinction from the full matchup, so its "week to date" / "so far this week" wording is correct and stays. Verified both rendered emails: 0 stray "Week N" labels, and the kept "week" instances all belong to Lineup Watch / 7-day windows.

## Season Trajectory ported recap → digest (2026-07-08)

User asked to carry the recap's Season Trajectory panel (W/L/T grid, teams×weeks, current streak in the final column) into the daily digest's SEASON band. `send_digest.py` and `weekly_recap.py` deliberately **don't import each other** (each copies the constants/helpers it needs), so `build_season_trajectory` is a near-verbatim copy of `weekly_recap.build_trajectory` — the only change is a `my_team` param (default `MY_TEAM`) so the `--team` view highlights the right row instead of always Guerrero Warfare. Both read the same snapshot `weekly_results` (`{week: {team: W/L/T}}`) + `standings`, so they stay in sync by construction. If the panel logic ever changes, update both copies. Placed as digest section 16, after Luck Standings.

## Week at a Glance pickups: from "best available hitter" to roster-context aware (2026-07-08)

User caught the flaw: the pickup bullet kept telling him to grab an **OF** while his OF was so deep he was *benching* a masher (Riley Greene), and it never mentioned **catcher** where he ranked dead last, nor his **pitching** after two active-slot implosions. Root cause was one line — `add_candidate = sorted(fa_hit, by score)[0]` — the single best available hitter in the league, with zero roster context. OF is structurally the largest pool (4 OF slots + LF/CF/RF fold in), so the best FA hitter is *chronically* an OF. The bullet was also hard-locked to hitters (`focus_pit = False`), so it literally could not respond to a pitching crisis.

The fix reuses two signals the digest already computes but the bullet ignored: `positional_breakdown` (per-position league rank + my worst starter + best FA, all carrying `_pscore`) and the new `lineup_efficiency_current` (bench surplus + active-slot blowups). Redesigned per the user's two explicit asks — **two bullets** (positional-need bat + pitching-recovery), pitching-recovery triggered by **either** an implosion **or** a non-toss-up ratio loss:

- **NEED vs SURPLUS.** A hitter position is a NEED if I rank bottom-third AND a real FA upgrade exists over my worst starter there; SURPLUS if top-third rank OR I'm leaking that position's production on my bench. Never recommend adding at a surplus position (kills the "add OF" nonsense); among needs, surface the **weakest** first.
- **`_UPGRADE_MARGIN` calibration (6.0 → 3.0).** The first cut used a 6-pt minimum upgrade and picked the biggest raw gap. On live data that *hid catcher*: C ranked 12/12 but the best FA (Alvarez) was only +4 over my worst — real, but below 6. Wrong bar for the exact case the user cared about. Lowered to 3.0 and re-sorted to weakest-position-first: at your worst spot even a modest bump is worth flagging; the point is to fill the hole, not chase the largest gap (which lives at strong positions with a weak backup). Verified: bat bullet now says "Add Francisco Alvarez (C) — weakest bat spot — C #12/12".
- **Pitching stabilizer, not a streamer.** Recovery picks a high-FLOOR arm (ERA ≤ 4.00, WHIP ≤ 1.25, real sample, ranked by xERA/ERA then WHIP) so it lowers ratios rather than trading them for K/W/QS. This made the old SP `ratio_warn` guardrail redundant — removed it (bat bullet is hitters-only; pitch bullet is a stabilizer by construction).
- **Distinct surplus drops.** Both bullets drop from strength (surplus-first, then lowest score), take distinct players, and show a `[surplus]` tag — verified by forcing `league_total_roster_max=0`. With open roster spots (the common case) both render as free pickups.

Returns a **list** now (was a string); `build_week_overview` `.extend`s it (still tolerates a bare string for safety). Team-specific: for The BIG Dumpers the bat bullet correctly recommends an OF because OF *is* their #11/12 weakest spot — it's need-driven, not a blanket rule.

## Lineup efficiency / bench leakage (2026-07-08)

Answers a user question: *can we see which player stats actually counted toward the matchup, so we can catch (a) a bat that homered on my bench and (b) a starter I let implode in my active lineup then dropped?* Yes — but not through the obvious path.

**Why `box_scores` isn't enough:** this is a **categories** league, so `league.box_scores()` returns `H2HCategoryBoxScore`, which carries only team category totals — no per-player lineup. The `home_lineup`/`away_lineup` `BoxPlayer` list only exists on the **points**-league box class. The per-player, per-day slot instead comes from `league.espn_request.league_get(params={"view":"mRoster","scoringPeriodId":<day>})`. Verified experimentally that ESPN **preserves the historical daily lineup**: querying a past scoring period returns each entry's `lineupSlotId` *as it was set that day* (a player's slot genuinely varies day to day — e.g. James Wood `OF,OF,OF,BE,OF...`), plus that day's actual stat split (`statSourceId==0`, matching `scoringPeriodId`). ESPN baseball scoring periods are daily and increment by 1, so a matchup week = 7 consecutive scoring-period ids ending the day before this Monday's id.

**Opportunity-cost correction (the key nuance, from the user):** raw "he produced X on the bench" overstates the miss — with a full active lineup, starting him means benching someone else. So bench leakage is reported **net** of the weakest startable bat you'd have sat. Whether he could have been slotted *without* benching anyone (open slot **or** a legal reshuffle) is a **max-bipartite-matching feasibility** check (`_full_match`, augmenting-path) over the day's active hitters + the candidate, against `lineupSlotCounts`-expanded hitter-slot instances, using each player's `eligibleSlots`. If feasible → displaced = nobody. Else → displaced = the min-value (`TB+BB+SB`) active hitter sharing an eligible slot. Net uses only his *notable* days (HR/SB/2+R/2+RBI) — a quiet 0-4 bench day has nothing to recover, and netting it would cherry-pick negatively. **OPS is never netted** — it's a rate, and benching an 0-4 line actually *helps* OPS, so "recovering" it is ambiguous.

**Pitcher side (user's example 2):** an active-slot start of 5+ ER (or 4+ ER in <3 IP) already counted toward ERA/WHIP; cross-referenced against `recent_activity(size=150)` drops to flag "imploded then dropped" — the damage is banked, the tactical lesson is sit-don't-start next time. (Once a player is off the roster his future stats don't attribute to you, so there's nothing to recover there — only the pre-drop damage is visible.)

**Split across two surfaces:** `mode="prev"` (completed Mon–Sun) feeds the Monday recap's fuller **Lineup Efficiency** band (post-mortem); `mode="current"` (Mon→yesterday, today excluded as incomplete) feeds the daily digest's compact **Lineup Watch** callout — deliberately mid-week, because that's when a benched masher is *still fixable* for the remaining days, unlike the Monday view. Both stored in the snapshot (`lineup_efficiency`, `lineup_efficiency_current`) so the render scripts stay snapshot-only; the live daily fetch lives in `fetch_data.get_lineup_efficiency`. Standalone console version with the opponent + head-to-head comparison: `bench_leakage.py` (the user cared less about the opponent view, so only my-team surfaces in the digest/recap). First real run caught Riley Greene's 4 HR / 9 RBI net stranded on the bench (started cold Moniak over him) and Dustin May imploding (0.2 IP, 5 ER) then getting cut same day.

## Matchup W-L: the ratio-stat IP floor (the 10-2 vs 11-1 bug)

`get_all_matchups` / `get_prev_matchup` read ESPN's own `box.home_stats[cat]["result"]` (`"WIN"`/`"LOSS"`/`"TIE"`) rather than comparing raw values. Why: ESPN applies a ratio-stat innings-pitched minimum (~25 IP) before ERA/WHIP count. A team with the *better* WHIP that is still under the IP floor **loses** that category. A naive `h_val < a_val` comparison reported 10-2 when ESPN showed 11-1 — my WHIP 1.55 "lost" to the opponent's 1.37 on raw value, but the opponent had only ~22.7 IP (68 OUTS) < 25, so ESPN scored it a WIN for me. The box-score stat dict exposes `OUTS` (IP×3) and a per-cat `result`; discovered via live diagnostic. The 25-IP-min insight came from the user. Raw-value comparison remains only as a fallback when ESPN supplies no `result` (old snapshots).

Intended consequence: the current result (honors the live IP floor) can legitimately differ from the projected result (Category Pulse / `classify_categories` compare raw projected values, since both teams usually clear the IP min by Sunday). So a category you're "winning" only because the opponent is under the IP min will correctly show a ▼ flip arrow projecting the loss once they qualify. Don't make the projection honor the floor — the divergence is informative.

## QS / 5K+ badges: why proj-line-only

An earlier version fired the badges on season-rate OR proj-line, which produced contradictions like a `5K+` badge next to a `4 K` proj line (Peter Lambert: 0.90 K/IP rate fired the badge, but low IP/G + a contact-heavy opponent → proj K=4). The user found this confusing. Badges are now driven ONLY by `_proj_line_vals(r)` (the same numeric `(ip, er, k)` the Proj. Line cell renders), so a badge can never disagree with the line. The QS% column still shows the season quality-start probability separately for nuance.

FA-SP badges also used to render only on thin rotation days (`thin_day = my_count < 2 and date_str <= week_end_str`), so a strong streamer showed a great Proj. Line but no badge on a full-rotation day. That gate was removed 2026-07-03; badges now fire wherever the pitcher appears.

## Score unification: the Ashby 72-vs-58 bug

`sp_fa_score` (pitcher_score + a hidden start bonus) was removed because it made Ashby show 72 in one table and 58 in another. Three canonical role scores now: SP → `_score_p` (blended), RP → `rp_score` (never blended — built on ESPN season counting stats, so identical across sections), Hitter → `_blend(r, hitter_score, best_recent_h)`. Never score a section with a different function than the others use for the same role.

## Blend weight history

`_BLEND_W` moved 0.40 (60/40) → 0.35 (65/35 season/recent) on 2026-07-02, to lean on the stable season signal since hot/cold streaks are surfaced separately in the Hot/Cold sections. No recalibration needed — the blend is a post-calibration weighted average of two already-calibrated 0–100 scores.

## Advanced pitcher analytics (2026-07-02, "full + blended")

Added Baseball Savant predictive stats via pybaseball: `get_savant_pitcher_expected` → xERA/xwOBA_against; `get_savant_pitcher_skill` → WhiffPctile. `pitcher_score` SP path blends K% 60/40 with whiff%, ERA 55/45 with xERA, plus a contact-allowed component. `rp_score` blends ERA 50/50 with xERA. Recalibrated both via `recalibrate_scores.py` (SP `s*1.4341-39.957`, RP `s*1.9619-43.0286`). All blends fall back to raw when the Savant field is missing. Hitters were left as-is (already Statcast-driven at 98–99% coverage).

RP SVHD was deliberately de-emphasized ("punt saves harder"): raw weight 40→15 (~37%→~15%), shifted to K/ERA/WHIP/W, because saves are the most volatile category and one we're willing to sacrifice. Skill/holds relievers (Dylan Lee, Aaron Ashby → ~100) now top mediocre closers (Sewald 19SV/4.50ERA → 64).

## merge_on_name refresh crash (pre-existing, fixed 2026-07-02)

`fkeys` (fp-side name keys) was built from `fp["PlayerName"]` (a non-default index) while the boolean `missing` mask came from `merged` (clean RangeIndex after `fp.merge()`). When the incoming frame carried a non-default index the two were unalignable → `IndexingError: Unalignable boolean Series`, which silently forced every data refresh to fall back to the stale snapshot. Fixed by building `fkeys` from `merged["PlayerName"]`.

## Tap-to-expand v1 → v2

v1 made each Score badge a `<details class="scorebd">`, but a `<details>` lives inside one `<td>` and can only expand within that narrow cell — the user found it cramped. v2 uses a `:target`-toggled full-width `<tr colspan>` below the player row, the only no-JS, email-safe way to get the full-width look. Trade-off: Gmail's inline body strips the head `<style>`, so there the rows stay hidden (badge still shows; link is a harmless no-op) rather than always-visible as v1 degraded — better degrade since the user reads the browser-rendered attachment.

## Investigations (kept for reference)

- **Hitter-score spread** (Ruiz 43 / Gonzales 53 / Caratini 58 / Karros 68 / Moniak 54 / Mitchell 76 despite similar OPS): OPS is ~30% of the score. Spread comes from SprintSpeed (Caratini 0.8 vs Mitchell 9.7) and xwOBA quality-of-contact (Ruiz's .272 → 0.2/10, his .791 OPS is "empty"), plus the recent-form blend.
- **Seth Lugo genuinely bad (17):** 5.32 xERA vs 4.20 ERA, ~5th-pctile whiff, 10.3% barrel + .353 xwOBA-against. Model reads him as a regression candidate, not unlucky.
- **WhiffPct/xFIP absent:** present in 0 snapshot rows (FanGraphs 403). Dead `whiff`/`xfip` branches removed from `pitcher_score` (scores byte-identical).
- **Dynamic benchmark fractions** were chosen so derived floors ≈ the old hard-codes *today* (SP rely ≈ 20.4 IP, SP viable ≈ 3.06 GS, RP viable ≈ 12 GP / 19.8 IP), so the change is calibration-neutral now and only diverges as the season grows.
