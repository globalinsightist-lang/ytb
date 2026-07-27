# TODO

## YouTube monetization compliance (2025 "inauthentic content" policy)

Make autopilot Shorts safely monetizable under the July 15, 2025 YPP update.

- [x] **Break the template** — voice rotates over an 8-voice pool, cut cadence
      varies per run, subtitles restyled. _Partial:_ Pexels clips are shuffled
      per run but there is still no cross-video dedup, so the same stock clip
      can recur across uploads.
- [ ] **Add genuine transformation** — original commentary/analysis on top of the
      stock footage, not just narrated facts over generic B-roll. **Still open,
      and the biggest remaining policy risk.**
- [x] ~~Add human review before publish~~ — deliberately declined; the pipeline
      stays fully autonomous.
- [x] **Disclose AI/altered content** — `status.containsSyntheticMedia` is now set
      on every upload.
- [ ] **Meet the baseline YPP bar** — 1,000 subs + 10M Shorts views in 90 days.
      At 43 subs / 12,040 views after a month, this is ~23x and ~314x away.
- [x] **Disallow usage of copyright music** — `resource/songs/licenses.json` is an
      allowlist and `get_bgm_file` enforces it. The 29 upstream tracks came from
      YouTube videos with no rights cleared, so they are listed uncleared and
      **all renders now ship with no BGM** until licensed tracks are added.
- [x] **Remove bad subtitles** — Anton (bold condensed, SIL OFL) at 84px with a
      5px stroke, moved to `center` so YouTube's Shorts UI stops covering them.
- [x] **Utilise the feedbacks in next set of scripts** — the prompt is now built
      from real YouTube retention (`performance.jsonl`), not LLM platitudes.
      **Blocked on adding the `yt-analytics.readonly` OAuth scope.**
- [x] **Remove AI words, script should sound human written** — banned diction list
      in the script prompt (`_HUMAN_VOICE_RULES`).
- [x] **No Emoji in titles** — stripped in `sanitize_title`, plus banned openers
      ("Unlock…", "Secrets") and trailing exclamation marks.
- [x] **6:30 - 7:30 is most views, need to post 1 vid there** — added a 01:05 UTC
      slot (06:35 IST). **Assumes the peak window was read in IST** — if it was
      UTC, change the hour list in `cloudflare-cron/wrangler.toml`.

## Open items

- [ ] **Re-mint `YT_REFRESH_TOKEN` with the `yt-analytics.readonly` scope.**
      Run `scripts/run_youtube_auth.py`, which prints the granted scopes before
      the token. A refresh token carries only the scopes it was issued with, so
      adding the scope to the consent screen does nothing to the existing token —
      it must be re-minted. Until then the learning loop falls back to
      `feedback_notes` and the evidence block stays empty.
- [ ] **Source licensed BGM** and fill in `resource/songs/licenses.json`.
      Renders currently have no background music at all.
- [ ] **Dead-man's switch** — alert if `history.jsonl` stops growing for 24h. The
      pipeline was silently dead for 3 days (Jul 23-26) with nobody notified.
- [ ] **Cross-video footage dedup** — track recently used Pexels ids so the same
      clip doesn't recur across uploads.
- [ ] **Re-check the 90-day view pace** two weeks after the analytics loop lands,
      when there is real retention data to judge the Shorts route on.
