# Pocket Trade Lab — refresh proxy (Cloudflare Worker)

This tiny Worker is the only piece that holds a GitHub token. The public GitHub Pages
page POSTs here when you tap **↻ Refresh data**; the Worker fires a `repository_dispatch`
event that triggers `.github/workflows/pocket-tradelab.yml` to refresh the data and
republish the page. The page then polls `build.json` and auto-reloads when the new build
lands (~2–3 min).

## One-time setup

### 1. Enable GitHub Pages
Repo → **Settings → Pages → Build and deployment → Source = GitHub Actions**.

### 2. Create a fine-grained Personal Access Token (PAT)
GitHub → **Settings (your account) → Developer settings → Personal access tokens →
Fine-grained tokens → Generate new token**:
- **Repository access:** *Only select repositories* → `baseball`.
- **Permissions → Repository permissions → Contents: Read and write.**
  ⚠️ This must be **Contents**, not Actions — the `repository_dispatch` REST endpoint the
  Worker calls is gated on the *Contents* write permission. Granting only *Actions* returns
  `403 "Resource not accessible by personal access token"` and the Refresh button fails.
- (Leave everything else at *No access*.) The token's only power is triggering this workflow.
- Copy the token (starts with `github_pat_...`).

> **Already set up with the wrong permission?** You don't need a new token or a Worker
> redeploy — just edit the existing one: GitHub → **Settings → Developer settings →
> Fine-grained tokens → (your token) → Edit → Repository permissions → set Contents to
> *Read and write* → Save**. The token string stays the same, so the Worker secret is
> unchanged.

### 3. Deploy the Worker
Install Cloudflare's CLI once (`npm i -g wrangler`) or use `npx`:

```bash
cd worker
npx wrangler login                       # opens a browser to your Cloudflare account
npx wrangler secret put GH_DISPATCH_TOKEN # paste the PAT from step 2 when prompted
npx wrangler deploy                       # prints the Worker URL, e.g.
                                          #   https://pocket-tradelab-refresh.<you>.workers.dev
```

Copy the printed Worker URL.

### 4. Tell the page where the Worker lives
Repo → **Settings → Secrets and variables → Actions → Variables tab → New repository
variable**:
- **Name:** `POCKET_REFRESH_URL`
- **Value:** the Worker URL from step 3.

(A *variable*, not a secret — the URL isn't sensitive. The token stays inside the Worker.)

### 5. Publish + install on your phone
Repo → **Actions → Pocket Trade Lab → Run workflow** (once, manually). When it finishes,
open `https://desktoptableau136-commits.github.io/baseball/` on your phone and
**Add to Home Screen** so it launches like an app.

## Daily use
Open the home-screen icon → tap **↻ Refresh data** → wait ~2–3 min for the auto-reload.
The `Data:` badge (green/yellow/red dot) shows how fresh the snapshot is.

## Notes
- The Worker only accepts POSTs from the Pages origin (CORS-locked in `worker.js`).
- To harden further, add a Cloudflare **Rate Limiting** rule on the Worker route.
- If you rename the repo/owner, update `OWNER`/`REPO`/`ALLOWED_ORIGIN` in `worker.js`.
