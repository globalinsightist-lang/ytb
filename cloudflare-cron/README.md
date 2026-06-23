# Cloudflare Worker cron trigger

Reliable replacement for GitHub Actions' flaky native `schedule` event. This
Worker runs on Cloudflare's cron triggers and fires the repo's workflows via the
GitHub `workflow_dispatch` REST API.

It drives two workflows (`autopilot.yml`, `refresh-topics.yml`), which now keep
only their `workflow_dispatch:` trigger — the GitHub `schedule:` blocks were
removed so Cloudflare is the single source of truth (no double-fires, which would
waste the YouTube upload quota).

## One-time setup

1. **Create a fine-grained GitHub PAT** — https://github.com/settings/tokens?type=beta
   - Resource owner: `globalinsightist-lang`
   - Repository access: **Only select repositories → `ytb`**
   - Permissions → Repository → **Actions: Read and write**
   - Copy the token (`github_pat_…`). This is the entire blast radius if it leaks:
     it can only dispatch Actions on this one repo.

2. **Install + log in to wrangler** (Cloudflare's CLI; free account, no card):
   ```bash
   npm i -g wrangler
   wrangler login
   ```

3. **Store the token as a Worker secret** (never commit it):
   ```bash
   cd cloudflare-cron
   wrangler secret put GH_PAT          # paste the PAT when prompted
   # optional: enables the manual test endpoint
   wrangler secret put TRIGGER_KEY     # any random string
   ```

4. **Deploy:**
   ```bash
   wrangler deploy
   ```

That's it — Cloudflare now fires `autopilot.yml` every 4h and `refresh-topics.yml`
daily at 01:21 UTC.

## Verify it works

- Manual API test (no Worker needed), confirms the PAT + dispatch path:
  ```bash
  curl -i -X POST \
    -H "Accept: application/vnd.github+json" \
    -H "Authorization: Bearer $GH_PAT" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    https://api.github.com/repos/globalinsightist-lang/ytb/actions/workflows/refresh-topics.yml/dispatches \
    -d '{"ref":"main"}'
  # expect: HTTP/2 204
  ```
- Or via the Worker (if you set `TRIGGER_KEY`):
  `https://ytb-cron-trigger.<your-subdomain>.workers.dev/?workflow=refresh-topics.yml&key=<TRIGGER_KEY>`
- Watch it land: `gh run list --workflow=refresh-topics.yml`

## Changing the schedule

Edit the `crons` in `wrangler.toml` **and** the matching keys in
`CRON_TO_WORKFLOW` in `worker.js` (they must be byte-identical), then
`wrangler deploy`. Reflect any cadence change in `docs/index.html` per CLAUDE.md.
