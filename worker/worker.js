// Cloudflare Worker — token proxy for the Pocket Trade Lab "Refresh data" button.
//
// The public GitHub Pages page cannot hold a GitHub token, so it POSTs here instead.
// This Worker holds a narrowly-scoped fine-grained PAT (secret GH_DISPATCH_TOKEN, with
// only "Actions: Read and write" on the one repo) and fires a repository_dispatch event,
// which triggers .github/workflows/pocket-tradelab.yml to refresh data and republish.
//
// Setup (see worker/README.md): set GH_DISPATCH_TOKEN as a Worker secret, deploy, then
// put the deployed Worker URL into the repo Actions variable POCKET_REFRESH_URL.

const OWNER = "desktoptableau136-commits";
const REPO = "baseball";
const EVENT_TYPE = "refresh-tradelab";

// Only the GitHub Pages origin may call this Worker (blocks drive-by cross-site use).
const ALLOWED_ORIGIN = "https://desktoptableau136-commits.github.io";

function corsHeaders(origin) {
  const allow = origin === ALLOWED_ORIGIN ? origin : ALLOWED_ORIGIN;
  return {
    "Access-Control-Allow-Origin": allow,
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
  };
}

export default {
  async fetch(request, env) {
    const origin = request.headers.get("Origin") || "";
    const cors = corsHeaders(origin);

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: cors });
    }
    if (request.method !== "POST") {
      return new Response("Method not allowed", { status: 405, headers: cors });
    }

    const resp = await fetch(`https://api.github.com/repos/${OWNER}/${REPO}/dispatches`, {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${env.GH_DISPATCH_TOKEN}`,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "pocket-tradelab-worker",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ event_type: EVENT_TYPE }),
    });

    // GitHub returns 204 No Content on success.
    if (resp.status === 204) {
      return new Response(JSON.stringify({ ok: true }), {
        status: 202,
        headers: { ...cors, "Content-Type": "application/json" },
      });
    }
    const text = await resp.text();
    return new Response(JSON.stringify({ ok: false, status: resp.status, detail: text }), {
      status: 502,
      headers: { ...cors, "Content-Type": "application/json" },
    });
  },
};
