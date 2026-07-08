# Contributing / Dev Workflow

This is a solo project, but changes go through a lightweight branch → PR → merge flow.
The reason is specific: **GitHub Actions auto-deploys from `main`** (the daily digest and
Monday recap run on a schedule from whatever is on `main`), so a broken commit on `main`
ships to the league's inbox on the next run. The workflow below exists to catch breakage
*before* it reaches `main` — nothing more.

## The flow

1. **Branch** off `main` for any non-trivial change:
   ```bash
   git checkout main && git pull
   git checkout -b feature/<short-name>     # or fix/… , chore/…
   ```
2. **Build + verify locally.** Always render before pushing (no email is sent):
   ```bash
   python send_digest.py  --dry-run --no-refresh   # open previews/digest_preview_*.html
   python weekly_recap.py --dry-run --no-refresh   # open previews/recap_week_N.html
   ```
3. **Commit + push the branch** (not `main`):
   ```bash
   git push -u origin feature/<short-name>
   ```
4. **Open a PR** into `main`. The **PR Smoke Test** CI
   (`.github/workflows/pr-check.yml`) runs automatically: compile every module, refresh a
   real snapshot, dry-run render the digest + recap. It must be green to merge.
5. **Squash and merge**, then delete the branch. One tidy commit per feature on `main`.

Trivial doc/typo-only changes may go straight to `main`.

## Notes

- **CI on a PR runs the workflow file from `main`.** A PR opened before the CI workflow
  itself is merged won't be gated until it lands on `main` — so the CI PR merges first.
- **Feature branches don't trigger the scheduled digest/recap** — those `schedule`
  triggers only fire on the default branch, so work-in-progress never emails the league.
- The full "save sequence" for a feature: update `CLAUDE.md` (rules), the in-digest
  glossary, `README.md`, then commit → push the branch → open the PR. Background/rationale
  goes in `NOTES.md`.

## Managing PRs from the shell (optional, recommended)

Install the GitHub CLI once so PRs can be opened/merged without the web UI:
```bash
winget install --id GitHub.cli        # Windows
gh auth login                         # one-time browser login
```
Then, per change:
```bash
gh pr create --fill --base main
gh pr checks --watch                  # wait for the smoke test to go green
gh pr merge --squash --delete-branch
```
