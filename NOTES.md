# NOTES.md — background & rationale

Forensic detail and "why we did it this way" narrative moved out of `CLAUDE.md` to keep that file to actionable rules. Nothing here is required to follow the rules; consult it only when you need the history behind a decision. Rules live in `CLAUDE.md`; this is the "why".

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
