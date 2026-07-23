Run the **full save sequence** — the complete wrap-up after a change is done and verified.
This is the canonical closing ritual: keep the docs honest, land the code, then rebuild and
open all three outputs so the result can be eyeballed. Follow every step in order; don't skip
silently. The conditional doc steps (2, 3) always get *considered*, even when skipped.

`$ARGUMENTS`: pass `norefresh` to skip the fresh data pull in the finale (fast code-render
check — digest/dashboard render off the existing snapshot). Default is a **fresh refresh** so all
three outputs show the same current data. (The pocket Pages build always refreshes on CI either way.)

---

### Phase A — Docs & code (the "save" half)

1. **Document** — if behavior/architecture changed, update the machine-facing doc for the surface
   that changed. CLAUDE.md is a thin router + universal rules; per-surface detail lives in
   `docs/*.md` (`docs/trades.md`, `docs/scoring.md`, `docs/dashboard.md`, `docs/recap.md`,
   `docs/fetch_pipeline.md`). Put the update in the file whose surface changed (see the router
   table at the top of CLAUDE.md), not blindly in CLAUDE.md. A brand-new surface/file → add a
   router row in CLAUDE.md + a new `docs/*.md`. Never use `@import`.
2. **Glossary** *(conditional — user-facing metric/score/feature)* — if the change adds/alters a
   score, metric, badge, column, or feature the digest surfaces, update `build_glossary_section()`
   in `send_digest.py` so the in-digest "Glossary & Methodology" reference stays accurate.
3. **README** *(conditional — user-facing)* — update `README.md` for any user-facing change: section
   order, new/removed sections, scoring changes, table columns, snapshot fields, data sources.
4. **Save** — ensure all edits are written to disk.
5. **Commit** — on the current **feature branch** (per the feature-branch workflow: real work goes on
   `feature/…`, `fix/…`, `chore/…`, not straight to `main`). Descriptive message; end with the
   `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>` trailer.
6. **Push + PR** — `git push` the branch to origin. `gh` **is** authenticated in this repo, so open
   the PR directly: `gh pr create --fill` (or `--web`) against `main`, repo
   `desktoptableau136-commits/baseball`. Hand the user the PR URL. **Do not self-merge** — the user
   reviews the diff on GitHub and **squash-merges himself**, then deletes the branch. If the work is
   already merged to `main` (user said "after merging and cleaning everything"), skip 5–6 and note that.
7. **Memory & doc hygiene (trim as you go — keep the always-loaded files lean)** — the point is that
   `CLAUDE.md` and `MEMORY.md` load into context every session, so bloat there is a recurring tax.
   Trim per-session, never in a big backlog sweep (per `feedback_memory_hygiene`). **Trimming =
   route content to its proper `.md`, not just cut it** — migrate what still has value to the right
   cold file (forensics → `NOTES.md`, surface detail → `docs/*.md`, closed sessions →
   `todo_history.md`) and only outright-delete what the change made *wrong*:
   - **CLAUDE.md** — keep it **actionable rules only**. Move completed-work forensics, rationale, and
     "here's the bug we hit" background into `NOTES.md`. If this change made a rule obsolete, delete
     the stale rule rather than stacking a new one beside it. A new surface adds a router row, not a
     wall of detail (detail goes in the surface's `docs/*.md`).
   - **MEMORY.md** — keep every index line a **one-liner** (`- [Title](file.md) — hook`). Archive the
     just-closed session: compress its narrative to a pointer entry in `todo_history.md`, strip
     `todo_next_session.md` back to open items only, delete any memory this change made wrong, and
     fold duplicate memories together. Memory bodies never live in `MEMORY.md`.

### Phase B — Rebuild & open all three (the finale)

Do these so the outputs open with minimal waiting — kick off CI first, build locally while it bakes.

8. **Trigger the pocket Trade Lab Pages rebuild first** (runs on CI ~2 min; the digest's "Build in
   Trade Lab" buttons deep-link to this hosted site, which only reflects fresh data after this runs):
   ```
   gh workflow run pocket-tradelab.yml
   ```
   Grab the run id from `gh run list --workflow=pocket-tradelab.yml --limit 1`.
   Fallback if `gh` is unavailable: GitHub Actions UI → "Pocket Trade Lab (GitHub Pages)" → Run
   workflow, or tap the in-page ↻ Refresh once the site is open. A pocket-build failure must NOT
   fail the whole sequence — note it and continue.

9. **Local build — one fetch, both previews:**
   ```
   python send_digest.py --with-dashboard --dry-run          # default: fresh refresh
   python send_digest.py --with-dashboard --dry-run --no-refresh   # if $ARGUMENTS = norefresh
   ```
   Writes `previews/digest_preview_Guerrero_Warfare.html` + `previews/dashboard_Guerrero_Warfare.html`
   (dry-run = no email).

10. **Open the digest + dashboard** in the default browser:
    ```
    start "" "C:\Users\katzs\Desktop\baseball\previews\digest_preview_Guerrero_Warfare.html"
    start "" "C:\Users\katzs\Desktop\baseball\previews\dashboard_Guerrero_Warfare.html"
    ```

11. **Wait for the pocket deploy, then open it:**
    ```
    gh run watch <run-id> --exit-status --interval 10
    start "" "https://desktoptableau136-commits.github.io/baseball/"
    ```
    The Pages tab polls `build.json` and reloads when the new `built_at` lands — give it a moment.

---

**Relay to the user at the end:** which doc/glossary/README files changed (or were considered and
skipped), the PR URL (or "already merged"), the two local preview paths, and the pocket run status
(id + success/fail) with the Pages URL. Keep it to a few lines.
