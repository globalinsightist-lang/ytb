// Cloudflare Worker — reliable cron trigger for the GitHub Actions autopilot.
//
// GitHub's native `schedule` event is best-effort and silently drops runs
// (worst for new repos / high load). This Worker runs on Cloudflare's far more
// reliable cron triggers and fires the workflows via the `workflow_dispatch`
// REST API instead. The workflows keep their `workflow_dispatch:` trigger and
// no longer rely on GitHub's `schedule:`.
//
// Setup (see README.md): `wrangler secret put GH_PAT` with a fine-grained PAT
// scoped to this repo only, Permissions → Actions: Read and write.

const OWNER = "globalinsightist-lang";
const REPO = "ytb";
const REF = "main";

// Map each cron trigger (must match wrangler.toml exactly) → workflow file to dispatch.
const CRON_TO_WORKFLOW = {
  "0 */4 * * *": "autopilot.yml",     // every 4h — heavy render/judge/upload, 6×/day (YouTube quota cap)
  "21 1 * * *": "refresh-topics.yml", // daily 01:21 UTC — refresh topic list from Google Trends
};

async function dispatch(workflow, token) {
  const url = `https://api.github.com/repos/${OWNER}/${REPO}/actions/workflows/${workflow}/dispatches`;
  const res = await fetch(url, {
    method: "POST",
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${token}`,
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "ytb-cf-cron", // GitHub API requires a User-Agent
    },
    body: JSON.stringify({ ref: REF }),
  });
  // Successful workflow_dispatch returns 204 No Content.
  if (res.status !== 204) {
    throw new Error(`dispatch ${workflow} failed: ${res.status} ${await res.text()}`);
  }
  console.log(`dispatched ${workflow} (ref=${REF})`);
}

export default {
  // Cron-triggered entrypoint.
  async scheduled(event, env, ctx) {
    const workflow = CRON_TO_WORKFLOW[event.cron];
    if (!workflow) {
      console.log(`no workflow mapped for cron "${event.cron}"`);
      return;
    }
    ctx.waitUntil(dispatch(workflow, env.GH_PAT));
  },

  // Optional manual trigger for testing: GET /?workflow=autopilot.yml&key=<TRIGGER_KEY>
  // Disabled unless the TRIGGER_KEY secret is set, so the endpoint is never open.
  async fetch(request, env) {
    if (!env.TRIGGER_KEY) return new Response("not found\n", { status: 404 });
    const url = new URL(request.url);
    if (url.searchParams.get("key") !== env.TRIGGER_KEY) {
      return new Response("unauthorized\n", { status: 401 });
    }
    const workflow = url.searchParams.get("workflow") || "autopilot.yml";
    if (!Object.values(CRON_TO_WORKFLOW).includes(workflow)) {
      return new Response(`unknown workflow: ${workflow}\n`, { status: 400 });
    }
    try {
      await dispatch(workflow, env.GH_PAT);
      return new Response(`ok: dispatched ${workflow}\n`);
    } catch (e) {
      return new Response(`error: ${e.message}\n`, { status: 500 });
    }
  },
};
