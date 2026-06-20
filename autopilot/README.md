# Autopilot — self-improving Shorts on a schedule

Every run (driven by `.github/workflows/autopilot.yml`):

1. **Pick a topic** — walks `topics.txt`, skipping recently used ones; when the
   list is exhausted it asks Gemini to invent fresh topics in your `niche`.
2. **Generate 4 candidate scripts** using the *current evolving* system prompt.
3. **LLM-as-judge** ranks the 4 on hook / pacing / clarity / payoff, picks the
   winner, and writes 1–3 reusable lessons.
4. **Render only the winner** through the normal pipeline (TTS → subtitles →
   Pexels footage → MoviePy composite).
5. **Upload** the final cut to YouTube via the Data API.
6. **Learn** — the judge's lessons are appended to `state.json["feedback_notes"]`
   (bounded), folded into the system prompt next run, and the workflow commits
   the updated state back to the repo.

Compute runs on **GitHub Actions** (free + unlimited on public repos, 4 vCPU /
16 GB) — not Render, whose free tier can't render video. Persistence is the
git commit-back, since `storage/` is gitignored and wiped on each checkout.

---

## One-time setup

### 0. Enable Actions write access
`Settings → Actions → General → Workflow permissions` → select **Read and write
permissions**. Without this the commit-back step gets HTTP 403. Scheduled
workflows also only run from the repo's **default branch**, so this file must be
on `main`.

### 1. GitHub repo secrets
`Settings → Secrets and variables → Actions → New repository secret`:

| Secret | What it is |
| --- | --- |
| `CONFIG_TOML` | Your **entire** `config.toml` (see below). |
| `YT_CLIENT_ID` | OAuth client ID (step 3). |
| `YT_CLIENT_SECRET` | OAuth client secret (step 3). |
| `YT_REFRESH_TOKEN` | OAuth refresh token (step 3). |

### 2. `CONFIG_TOML` contents
Copy `config.example.toml`, set these keys, and paste the whole file as the
secret value:

```toml
[app]
llm_provider = "gemini"
gemini_api_key = "YOUR_FREE_GEMINI_KEY"     # https://aistudio.google.com/apikey
gemini_model_name = "gemini-2.5-flash"
pexels_api_keys = ["YOUR_PEXELS_KEY"]       # free: https://www.pexels.com/api/
subtitle_provider = "edge"                  # keep "edge" — whisper would download a ~3GB model
# leave upload_post_* disabled; autopilot uploads to YouTube directly
```

### 3. YouTube refresh token (free, direct Data API)
1. In [Google Cloud Console](https://console.cloud.google.com): create a
   project → **APIs & Services → Library → enable "YouTube Data API v3"**.
2. **OAuth consent screen**: User type *External*; add scope
   `https://www.googleapis.com/auth/youtube.upload`. Then **Publish the app**
   ("In production"). ⚠️ If you leave it in *Testing*, refresh tokens expire
   after 7 days — publishing avoids that (an "unverified app" warning is fine
   for your own channel).
3. **Credentials → Create OAuth client ID → Desktop app.** Copy the client ID
   and secret into the GitHub secrets above.
4. Get the refresh token via the
   [OAuth Playground](https://developers.google.com/oauthplayground): click the
   gear ⚙️ → "Use your own OAuth credentials" → paste client ID/secret →
   authorize scope `.../auth/youtube.upload` → **Exchange authorization code
   for tokens** → copy the `refresh_token` into `YT_REFRESH_TOKEN`.

### 4. Tune `config.json`
`niche`, `language`, `voice_name`, `num_candidates`, `privacy_status`
(`public` / `unlisted` / `private`), etc. Set `upload_enabled: false` to
render-only while testing.

---

## ⚠️ YouTube upload quota
`videos.insert` costs **1600 units** against the default **10,000/day** API
quota → **~6 uploads/day**, independent of the channel's manual upload limit.
Past that you'll get HTTP 403 `quotaExceeded`. To publish more, request a quota
increase for your project in the Cloud Console (audit-gated). Until then, either
lower the cron cadence or set `privacy_status: "private"` and review/publish
manually.

---

## Run it locally
```bash
# config.toml must exist with the keys from step 2
export YT_CLIENT_ID=... YT_CLIENT_SECRET=... YT_REFRESH_TOKEN=...
uv run python run_autopilot.py
```
Set `"upload_enabled": false` in `config.json` first if you just want to verify
rendering without touching YouTube.

## Files
- `config.json` — knobs (tracked, edit freely).
- `state.json` — evolving system-prompt feedback + run counter (auto-updated, committed back).
- `topics.txt` — seed topic queue.
- `history.jsonl` — append-only log of every run (topic, scores, lessons, YouTube URL).
