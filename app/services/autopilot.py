"""
Self-improving autopilot loop for one cron iteration.

`run_once()` performs a full cycle:
  1. Pull real per-video retention from YouTube Analytics into performance.jsonl.
  2. Pick a topic (round-robin over autopilot/topics.txt, else LLM-invented).
  3. Compose the script prompt from what actually held viewers on this channel.
  4. Generate N candidate scripts, each trimmed to a 30-40s word budget.
  5. LLM-as-judge ranks the candidates (shuffled, to defeat position bias).
  6. Render only the winning script, with a rotated voice and cut cadence.
  7. Upload the final cut to YouTube, declaring it as synthetic media.
  8. Record the hook and render choices so step 1 can attribute retention later.

The loop is closed on real data by design. An earlier version learned only from
an LLM judging its own scripts against each other; with no external anchor it
converged on restating "start with a strong hook" and stopped teaching anything.
`feedback_notes` survives only as the cold-start fallback for the window before
enough analytics accumulate.

All persistent learning lives under autopilot/ (a *tracked* directory) so a
CI job can commit the updated state back to the repo between runs — storage/
is gitignored and would be wiped on every fresh checkout.
"""
import json
import os
import random
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from os import path
from typing import List

import requests
from loguru import logger

from app.models.schema import VideoParams
from app.services import llm, task, youtube_analytics, youtube_upload
from app.services.llm import (
    DEFAULT_SCRIPT_SYSTEM_PROMPT,
    _generate_response,
    _strip_code_fence,
)
from app.utils import utils

AUTOPILOT_DIR = path.join(utils.root_dir(), "autopilot")
CONFIG_FILE = path.join(AUTOPILOT_DIR, "config.json")
STATE_FILE = path.join(AUTOPILOT_DIR, "state.json")
TOPICS_FILE = path.join(AUTOPILOT_DIR, "topics.txt")
HISTORY_FILE = path.join(AUTOPILOT_DIR, "history.jsonl")
PERFORMANCE_FILE = path.join(AUTOPILOT_DIR, "performance.jsonl")

DEFAULT_CONFIG = {
    "niche": "fascinating science and history facts",
    "language": "en",
    "paragraph_number": 1,
    "num_candidates": 4,
    "aspect": "9:16",
    "video_source": "pexels",
    "voice_name": "en-US-JennyNeural-Female",  # fallback when voice_pool is empty
    "bgm_type": "random",
    "subtitle_enabled": True,
    "upload_enabled": True,
    "privacy_status": "public",
    "youtube_category_id": "22",
    "max_feedback_notes": 12,
    "recent_topics_window": 5,
    # Daily Google-Trends topic refresh (scripts/run_refresh_topics.py).
    "trends_geos": ["IN", "GB", "US"],
    "daily_topic_count": 6,
    # ---------------- Template-breaking (varies per run) ----------------
    # One voice across every upload is the loudest "same factory" signal a
    # faceless channel can emit. Rotate deterministically by run number so the
    # output varies but a given run is still reproducible.
    "voice_pool": [
        "en-US-AriaNeural-Female",
        "en-US-AndrewNeural-Male",
        "en-US-EmmaNeural-Female",
        "en-US-GuyNeural-Male",
        "en-GB-SoniaNeural-Female",
        "en-GB-RyanNeural-Male",
        "en-AU-NatashaNeural-Female",
        "en-US-JennyNeural-Female",
    ],
    # Cut cadence rotates so consecutive uploads don't share a rhythm.
    "clip_duration_pool": [3, 4, 5],
    # Hard cuts only, deliberately. Every transition in video_effects.py
    # composites against a black background for its full 1s duration, so on a
    # 3-5s clip it blacks out a fifth to a third of the frame time — and on the
    # FIRST clip it means frame 0 is 100% black, measured. A Short is judged in
    # under a second, so opening on black is the most expensive thing the
    # render can do. Variation comes from voice, cut cadence and footage.
    "transition_pool": [""],
    # ---------------- Subtitle styling ----------------
    # Captions sit low in the frame, but NOT flush to the bottom: YouTube's
    # Shorts UI (title, handle, description, CTA button) paints over roughly the
    # bottom 15% of the frame, so the built-in "bottom" anchor (y = 95% of
    # height) is physically occluded on the exact surface viewers are reading.
    # "custom" + 78% is the lowest band that still clears that chrome. Raise
    # `custom_position` toward 90 for a true bottom edge if the UI overlap is
    # acceptable; lower it toward 50 to move back to mid-frame.
    "subtitle_position": "custom",
    "custom_position": 78,
    # Anton is a heavy condensed Latin face (SIL OFL). The upstream default is
    # a CJK font whose Latin glyphs are thin and generic at Shorts scale.
    "font_name": "Anton-Regular.ttf",
    # 54px over a 1080-wide frame keeps a caption to 1-2 lines. The previous 84
    # forced 3+ lines per cue, which is what overran the text box.
    "font_size": 54,
    # A solid backing plate does the legibility work a heavy stroke used to do,
    # so the outline drops to a hairline that just separates glyph from plate.
    "stroke_width": 2,
    "stroke_color": "#000000",
    "text_fore_color": "#FFFFFF",
    # Black plate behind the caption: guarantees contrast over bright or busy
    # stock footage, which an outline alone does not.
    "subtitle_background": "#000000",
    # ---------------- Script length ----------------
    # Measured output averaged ~56s with a third of uploads over 60s while the
    # judge prompt asked for 30-50s: nothing enforced the ask. These bound it.
    "target_seconds_min": 30,
    "target_seconds_max": 40,
    # Measured across the voice pool at rate 1.0: 2.66-3.02 words/sec
    # depending on voice and passage. 2.85 is the centre of that range.
    "words_per_second": 2.85,
    # ---------------- Learning loop ----------------
    # Number of best/worst performers injected into the script prompt.
    "evidence_sample_size": 5,
    # Videos need this many views before they're treated as a real signal;
    # below it, view count is noise rather than evidence.
    "evidence_min_views": 25,
    "analytics_enabled": True,
    # Bans the diction that reads as machine-written (see _HUMAN_VOICE_RULES).
    "enforce_human_voice": True,
    # ---------------- Compliance ----------------
    # TTS narration of an LLM-written script is synthetic media and must be
    # declared. Separate from monetization; required regardless.
    "declare_synthetic_media": True,
}

DEFAULT_STATE = {"run_count": 0, "recent_topics": [], "feedback_notes": []}

# Title patterns the channel over-used to the point of self-parody: 26% of the
# first 167 uploads opened with one of these verbs, 76% ended in "!". Both read
# as mass-produced to viewers and to YouTube's inauthentic-content classifier.
_BANNED_TITLE_OPENERS = (
    "unlock", "unlocking", "unveil", "unveiling", "unravel",
    "unraveling", "unravelling", "uncover", "uncovering", "discover",
)
_BANNED_TITLE_SUBSTRINGS = ("secret", "you won't believe", "shocking truth")
_EMOJI_RE = re.compile(
    "[" "\U0001f300-\U0001faff" "\U00002600-\U000027bf" "\U0001f1e6-\U0001f1ff" "]",
    flags=re.UNICODE,
)

_JUDGE_TEMPLATE = """You are a ruthless short-form video editor judging script candidates \
for a vertical YouTube Short on the topic: "{topic}".

There are {n} candidate scripts below. Each will be read aloud over stock \
footage in {smin}-{smax} seconds.

{candidates}

Score each candidate on FOUR axes, 0-10 each, for a TOTAL out of 40:
1. hook — does the very first sentence stop the scroll?
2. retention — does every following sentence earn the next one?
3. clarity — instantly understandable to a general audience, no jargon?
4. payoff — does it land something worth having stayed for?

## Calibration (use this scale literally, do not inflate)
- A hook scoring 2/10: "Today we're going to talk about the history of coffee." \
Generic, announces a topic, gives no reason to stay.
- A hook scoring 9/10: "Your coffee costs 400% more than it did in 2019, and \
it is not because of inflation." Specific, concrete, opens a loop.
- A total of 40/40 must be essentially unimprovable. Most competent scripts \
land between 22 and 32. Use the full range; do not cluster everything high.

Then pick the single best candidate.

Finally, write 1-3 SHORT, reusable guidelines that would make the NEXT batch of \
scripts better. They must be general (not specific to this topic), concrete, \
and actionable. Do NOT restate generic advice like "start with a strong hook" \
or "keep a brisk pace" — that is already in the prompt and repeating it teaches \
nothing. Only write a guideline if it names something SPECIFIC you observed in \
these candidates that a writer could act on differently next time.

Return ONLY valid JSON, no prose, no code fence:
{{"winner_index": <0-based int>, "scores": [{{"index": 0, "total": <number 0-40>, "reason": "<one line>"}}], "lessons": ["<guideline>"]}}"""

# Diction that marks a script as machine-written. The "it's not just X, it's Y"
# construction in particular ran through the channel's own descriptions.
_HUMAN_VOICE_RULES = """
## Sound like a person, not a content generator
Never use these words or constructions — they are the tells that make a script
read as AI-written:
- delve, tapestry, testament to, realm, landscape of, navigate the complexities,
  underscore, moreover, furthermore, pivotal, harness, unlock, unveil, embark
- "it's not just X — it's Y"
- "in conclusion", "in today's world", "let's dive in"
- stacked tricolons ("faster, smarter, and more connected than ever")

Write the way someone would actually say it out loud. Prefer concrete nouns and
real numbers over abstractions. Use contractions. Vary sentence length — a short
one after two long ones is what makes speech sound human. Do not end by
summarising what you just said; end on the single most interesting fact."""

_EVIDENCE_TEMPLATE = """
## What actually worked on THIS channel (real YouTube retention data)

These openers held the most viewers. Study what they have in common:
{winners}

These lost viewers fastest. Do not write like this:
{losers}

Write the kind of script that belongs in the first group."""


# --------------------------------------------------------------------------- #
# Persistence helpers
# --------------------------------------------------------------------------- #
def _load_json(file_path: str, default):
    if not path.isfile(file_path):
        return json.loads(json.dumps(default))  # deep copy of default
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"failed to read {file_path}, using defaults: {e}")
        return json.loads(json.dumps(default))


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(_load_json(CONFIG_FILE, {}))
    return cfg


def load_state() -> dict:
    state = dict(DEFAULT_STATE)
    state.update(_load_json(STATE_FILE, {}))
    state.setdefault("recent_topics", [])
    state.setdefault("feedback_notes", [])
    state.setdefault("run_count", 0)
    return state


def save_state(state: dict) -> None:
    os.makedirs(AUTOPILOT_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")


def append_history(entry: dict) -> None:
    os.makedirs(AUTOPILOT_DIR, exist_ok=True)
    with open(HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


# --------------------------------------------------------------------------- #
# Pipeline steps
# --------------------------------------------------------------------------- #
def _read_topics() -> List[str]:
    if not path.isfile(TOPICS_FILE):
        return []
    with open(TOPICS_FILE, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip() and not line.startswith("#")]


def pick_topic(cfg: dict, state: dict) -> str:
    recent = set(state.get("recent_topics", []))
    for topic in _read_topics():
        if topic not in recent:
            return topic

    # Exhausted the seed list (or all are recent): let the LLM invent one.
    avoid = ", ".join(list(recent)[-15:]) or "none"
    prompt = (
        f"Suggest ONE fresh, specific, high-engagement YouTube Shorts topic in "
        f"the niche '{cfg['niche']}'. Avoid anything similar to these recent "
        f"topics: {avoid}. Return only the topic as a short phrase, with no "
        f"quotes, numbering, or preamble."
    )
    try:
        topic = (_generate_response(prompt) or "").strip().strip('"').splitlines()[0]
        if topic:
            return topic[:200]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"LLM topic generation failed: {e}")
    return cfg["niche"]


def word_budget(cfg: dict) -> tuple[int, int]:
    """(min_words, max_words) implied by the target duration and speaking rate."""
    wps = float(cfg.get("words_per_second", 2.85)) or 2.85
    lo = int(float(cfg.get("target_seconds_min", 30)) * wps)
    hi = int(float(cfg.get("target_seconds_max", 40)) * wps)
    return lo, max(hi, lo + 1)


def estimate_seconds(script: str, cfg: dict) -> float:
    wps = float(cfg.get("words_per_second", 2.85)) or 2.85
    return len(str(script).split()) / wps


_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")


def trim_to_budget(script: str, max_words: int) -> str:
    """Trim to the last complete sentence that fits the word budget.

    Cutting mid-sentence destroys the payoff, which is the one thing the whole
    script is built toward — so this only ever drops whole sentences. If even
    the first sentence blows the budget it is returned intact rather than
    mangled; the prompt, not the trimmer, is the primary length control.
    """
    words = str(script).split()
    if len(words) <= max_words:
        return str(script).strip()

    sentences = _SENTENCE_END_RE.split(str(script).strip())
    kept, total = [], 0
    for sentence in sentences:
        n = len(sentence.split())
        if kept and total + n > max_words:
            break
        kept.append(sentence)
        total += n

    trimmed = " ".join(kept).strip()
    logger.info(f"trimmed script {len(words)} -> {len(trimmed.split())} words")
    return trimmed or str(script).strip()


def extract_hook(script: str) -> str:
    """First sentence — the only line that decides whether anyone watches."""
    text = str(script).strip()
    if not text:
        return ""
    return _SENTENCE_END_RE.split(text)[0].strip()[:200]


def pick_voice(cfg: dict, run_no: int) -> str:
    """Rotate deterministically through the voice pool by run number."""
    pool = [v for v in (cfg.get("voice_pool") or []) if v]
    if not pool:
        return cfg.get("voice_name", "en-US-JennyNeural-Female")
    return pool[run_no % len(pool)]


def pick_from_pool(cfg: dict, key: str, run_no: int, fallback):
    """Deterministic per-run choice from a config pool (cut rhythm, transitions)."""
    pool = cfg.get(key) or []
    if not pool:
        return fallback
    return pool[run_no % len(pool)]


def load_history() -> List[dict]:
    if not path.isfile(HISTORY_FILE):
        return []
    rows = []
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def load_performance() -> List[dict]:
    if not path.isfile(PERFORMANCE_FILE):
        return []
    rows = []
    with open(PERFORMANCE_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def refresh_performance(cfg: dict) -> dict:
    """Join history.jsonl against live YouTube Analytics into performance.jsonl.

    This is a full-snapshot rewrite, not an append: a video's metrics keep
    moving for weeks, so the file always holds the latest reading per video.
    Returns {"ok": bool, ...}; never raises — if analytics is unavailable the
    existing snapshot is left untouched and the caller carries on.
    """
    if not cfg.get("analytics_enabled", True):
        return {"ok": False, "error": "analytics disabled in config"}

    history = load_history()
    published = {}
    for row in history:
        vid = (row.get("youtube") or {}).get("video_id")
        if vid:
            # Later runs win, so re-uploads of the same id keep the newest meta.
            published[vid] = row
    if not published:
        return {"ok": False, "error": "no uploaded videos in history"}

    # Only ask about videos old enough for their numbers to have settled.
    settled = [v for v, r in published.items() if youtube_analytics.is_settled(r.get("ts", ""))]
    if not settled:
        return {"ok": False, "error": "no videos past the analytics reporting lag"}

    stats = youtube_analytics.fetch_video_stats(settled)
    if not stats:
        return {"ok": False, "error": "no analytics returned (scope or credentials?)"}

    rows = []
    for vid, metrics in stats.items():
        src = published.get(vid, {})
        rows.append(
            {
                "video_id": vid,
                "url": f"https://youtu.be/{vid}",
                "run": src.get("run"),
                "ts": src.get("ts"),
                "topic": src.get("topic"),
                "hook": src.get("hook", ""),
                "title": src.get("title", ""),
                "voice_name": src.get("voice_name", ""),
                "est_seconds": src.get("est_seconds"),
                "views": metrics.get("views"),
                "avg_view_pct": metrics.get("averageViewPercentage"),
                "avg_view_seconds": metrics.get("averageViewDuration"),
                "subs_gained": metrics.get("subscribersGained"),
                "likes": metrics.get("likes"),
                "shares": metrics.get("shares"),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    rows.sort(key=lambda r: (r["run"] is None, r["run"]))
    os.makedirs(AUTOPILOT_DIR, exist_ok=True)
    with open(PERFORMANCE_FILE, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")

    logger.success(f"performance snapshot refreshed for {len(rows)} videos")
    return {"ok": True, "count": len(rows)}


def _rank_key(row: dict) -> float:
    """Rank by retention, not raw views.

    Views mostly measure how far the algorithm pushed a video; average view
    percentage measures whether viewers stayed once it did. Only the second is
    something a script can control, so that is what the prompt learns from.
    """
    pct = row.get("avg_view_pct")
    return float(pct) if isinstance(pct, (int, float)) else -1.0


def build_evidence_block(cfg: dict) -> str:
    """Render real winners/losers into a prompt fragment, or "" if not enough data.

    Two tiers, because they become available at different times:
      * hooks  — needs `hook` recorded at render time, so it accrues going forward;
      * topics — recoverable for the entire backlog, so it works immediately.
    """
    rows = load_performance()
    min_views = int(cfg.get("evidence_min_views", 25))
    n = int(cfg.get("evidence_sample_size", 5))

    usable = [
        r for r in rows
        if isinstance(r.get("views"), (int, float))
        and r["views"] >= min_views
        and _rank_key(r) >= 0
    ]
    # Need enough on both ends for "best" and "worst" to mean anything.
    if len(usable) < n * 2:
        logger.info(
            f"evidence block skipped: {len(usable)} videos past the "
            f"{min_views}-view floor, need {n * 2}"
        )
        return ""

    usable.sort(key=_rank_key, reverse=True)
    best, worst = usable[:n], usable[-n:]

    def fmt(row: dict) -> str:
        label = row.get("hook") or row.get("title") or row.get("topic") or "(unknown)"
        return (
            f'- "{str(label).strip()[:160]}" '
            f"— {row.get('avg_view_pct', 0):.0f}% avg view, {row.get('views', 0)} views"
        )

    return _EVIDENCE_TEMPLATE.format(
        winners="\n".join(fmt(r) for r in best),
        losers="\n".join(fmt(r) for r in reversed(worst)),
    )


def compose_system_prompt(notes: List[str], cfg: dict | None = None) -> str:
    """Build the script system prompt from real performance first, notes second.

    The evidence block is self-correcting: when a pattern stops working the
    underlying numbers move and the prompt moves with it. The LLM-authored
    notes cannot do that — they are kept only as a fallback for the cold-start
    window before enough analytics have accumulated.
    """
    cfg = cfg or {}
    parts = [DEFAULT_SCRIPT_SYSTEM_PROMPT]

    smin = int(cfg.get("target_seconds_min", 30))
    smax = int(cfg.get("target_seconds_max", 40))
    lo, hi = word_budget(cfg)
    parts.append(
        f"\n## Length (hard requirement)\n"
        f"This is read aloud at ~{cfg.get('words_per_second', 2.85)} words/second and must "
        f"run {smin}-{smax} seconds. Write between {lo} and {hi} words. "
        f"Going over gets the ending cut off mid-sentence."
    )

    if cfg.get("enforce_human_voice", True):
        parts.append(_HUMAN_VOICE_RULES)

    evidence = build_evidence_block(cfg)
    if evidence:
        parts.append(evidence)
    elif notes:
        guidelines = "\n".join(f"- {n}" for n in notes)
        parts.append(f"\n## Learned guidelines (apply these strictly):\n{guidelines}")

    composed = "\n".join(parts)
    return composed[:7900]  # stay under the 8000-char cap enforced by llm.py


def generate_candidates(topic: str, cfg: dict, system_prompt: str) -> List[str]:
    candidates: List[str] = []
    seen = set()
    _, max_words = word_budget(cfg)
    for i in range(max(1, int(cfg["num_candidates"]))):
        script = llm.generate_script(
            video_subject=topic,
            language=cfg["language"],
            paragraph_number=cfg["paragraph_number"],
            custom_system_prompt=system_prompt,
        )
        script = (script or "").strip()
        if not script or "Error: " in script:
            logger.warning(f"candidate {i + 1} was empty/error, skipping")
            continue
        # The prompt states the budget; this is the backstop for when the model
        # ignores it, which it does often enough to have produced 115s uploads.
        script = trim_to_budget(script, max_words)
        if script in seen:
            logger.warning(f"candidate {i + 1} duplicated an earlier one, skipping")
            continue
        candidates.append(script)
        seen.add(script)
    return candidates


def judge_candidates(topic: str, candidates: List[str], cfg: dict | None = None) -> dict:
    """Rank candidates and return {"winner_index", "scores", "lessons"}.

    Candidates are shuffled before being shown to the judge and the winner is
    mapped back to the caller's ordering. Without this the judge is strongly
    position-biased — over the channel's first 169 runs, candidate 0 won 3
    times while candidate 1 won 93, which is a property of the prompt layout
    rather than of the scripts.
    """
    if len(candidates) <= 1:
        return {"winner_index": 0, "scores": [], "lessons": []}

    cfg = cfg or {}
    order = list(range(len(candidates)))
    random.shuffle(order)
    shuffled = [candidates[i] for i in order]

    block = "\n\n".join(f"### Candidate {i}\n{c}" for i, c in enumerate(shuffled))
    prompt = _JUDGE_TEMPLATE.format(
        topic=topic,
        n=len(shuffled),
        candidates=block,
        smin=int(cfg.get("target_seconds_min", 30)),
        smax=int(cfg.get("target_seconds_max", 40)),
    )
    try:
        raw = _generate_response(prompt)
        data = json.loads(_strip_code_fence(raw))
        shown = int(data.get("winner_index", 0))
        if shown < 0 or shown >= len(shuffled):
            shown = 0
        lessons = [str(x).strip() for x in data.get("lessons", []) if str(x).strip()]

        # Map scores back to the caller's indices so history stays comparable.
        scores = []
        for entry in data.get("scores", []) or []:
            if not isinstance(entry, dict):
                continue
            idx = entry.get("index")
            if isinstance(idx, int) and 0 <= idx < len(order):
                entry = {**entry, "index": order[idx]}
            scores.append(entry)

        return {
            "winner_index": order[shown],
            "scores": scores,
            "lessons": lessons[:3],
        }
    except Exception as e:  # noqa: BLE001 - never let judging crash the run
        logger.warning(f"judge failed, defaulting to first candidate: {e}")
        return {"winner_index": 0, "scores": [], "lessons": []}


def render_winner(topic: str, script: str, cfg: dict, run_no: int) -> tuple[str, str, dict]:
    """Render the winning script.

    Returns (final_video_path, task_id, render_meta) where render_meta records
    the varied choices so history can later correlate them with retention.
    """
    voice_name = pick_voice(cfg, run_no)
    clip_duration = pick_from_pool(cfg, "clip_duration_pool", run_no, 5)
    transition = pick_from_pool(cfg, "transition_pool", run_no, "")

    params = VideoParams(
        video_subject=topic,
        video_script=script,  # non-empty -> pipeline skips script generation
        video_count=1,
        video_aspect=cfg["aspect"],
        video_source=cfg["video_source"],
        voice_name=voice_name,
        video_language=cfg["language"],
        subtitle_enabled=cfg["subtitle_enabled"],
        bgm_type=cfg["bgm_type"],
        video_clip_duration=int(clip_duration),
        video_transition_mode=transition or None,
        # Subtitle styling is set here rather than on the shared schema defaults
        # so the WebUI and public API keep their upstream behaviour.
        subtitle_position=cfg.get("subtitle_position", "custom"),
        custom_position=float(cfg.get("custom_position", 78)),
        font_name=cfg.get("font_name", "Anton-Regular.ttf"),
        font_size=int(cfg.get("font_size", 54)),
        stroke_width=float(cfg.get("stroke_width", 2)),
        stroke_color=cfg.get("stroke_color", "#000000"),
        text_fore_color=cfg.get("text_fore_color", "#FFFFFF"),
        text_background_color=cfg.get("subtitle_background", "#000000"),
    )
    task_id = utils.get_uuid()
    result = task.start(task_id=task_id, params=params, stop_at="video")
    if not result or not result.get("videos"):
        raise RuntimeError("rendering produced no video")

    render_meta = {
        "voice_name": voice_name,
        "clip_duration": int(clip_duration),
        "transition": transition or "none",
        "subtitle_position": params.subtitle_position,
        "font_name": params.font_name,
    }
    return result["videos"][0], task_id, render_meta


def sanitize_title(title: str, fallback: str) -> str:
    """Strip the mass-produced title tics the channel over-used.

    LLMs comply with negative style constraints unreliably, so the prompt asks
    and this enforces. Rejected titles fall back to the topic, which is plain
    but never reads as generated.
    """
    cleaned = _EMOJI_RE.sub("", str(title or "")).strip()
    # Collapse "!!!" and drop a lone trailing "!" — 76% of the first 167 titles
    # ended in one, which is a pattern viewers learn to scroll past.
    cleaned = re.sub(r"[!]{2,}", "!", cleaned).strip()
    cleaned = re.sub(r"\s+", " ", cleaned).rstrip("!").strip()

    lowered = cleaned.lower()
    first_word = re.sub(r"[^a-z]", "", lowered.split(" ")[0] if lowered else "")
    if first_word in _BANNED_TITLE_OPENERS or any(
        s in lowered for s in _BANNED_TITLE_SUBSTRINGS
    ):
        logger.info(f"rejected templated title {cleaned!r}, falling back to topic")
        cleaned = _EMOJI_RE.sub("", str(fallback or "")).strip().rstrip("!").strip()

    return (cleaned or str(fallback or "Untitled").strip())[:100]


def upload_to_youtube(topic: str, script: str, video_path: str, cfg: dict) -> dict:
    if not cfg.get("upload_enabled", True):
        logger.info("upload_enabled is false, keeping the video local")
        return {"success": False, "error": "upload disabled"}
    if not youtube_upload.is_configured():
        logger.warning("YouTube not configured; skipping upload")
        return {"success": False, "error": "YouTube not configured"}

    banned = ", ".join(f'"{w}"' for w in _BANNED_TITLE_OPENERS[:6])
    meta = llm.generate_social_metadata(
        video_subject=topic,
        video_script=script,
        language=cfg["language"],
        platform="youtube_shorts",
        extra_constraints=(
            f"The title must NOT begin with any of {banned}, must not contain "
            '"secret", must contain no emoji, and must not end in an '
            "exclamation mark. Prefer a concrete, specific claim or a real "
            "number over a teaser. Write it the way a person would, not the "
            "way a content farm would."
        ),
    )
    title = sanitize_title(meta.get("title", ""), topic)
    hashtags = meta.get("hashtags", [])
    description = meta.get("caption", "")
    tag_line = " ".join(f"#{h.lstrip('#')}" for h in hashtags)
    description = f"{description}\n\n{tag_line}\n#shorts".strip()

    result = youtube_upload.upload_video(
        video_path=video_path,
        title=title,
        description=description,
        tags=hashtags,
        privacy_status=cfg["privacy_status"],
        category_id=cfg["youtube_category_id"],
        contains_synthetic_media=bool(cfg.get("declare_synthetic_media", True)),
    )
    result["title"] = title
    return result


# --------------------------------------------------------------------------- #
# Daily Google-Trends topic refresh
# --------------------------------------------------------------------------- #
TRENDS_RSS_URL = "https://trends.google.com/trending/rss?geo={geo}"

# Google returns 404/empty to the default python-requests UA; pretend to be a
# browser so the trending RSS feed loads.
_TRENDS_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_CURATE_TEMPLATE = """You are a topic curator for a FACELESS YouTube Shorts channel. \
Every video is narrated over generic stock footage — there is no host on camera \
and no licensed clips of real people, brands, or TV shows.

Here are today's raw Google Trends searches, pooled from several regions \
(many are hyper-local news, weather, sports results, or specific people):

{terms}

Turn these into EXACTLY {n} Shorts topics that are:
- globally interesting (NOT a single town's weather/news, NOT a local election);
- renderable from generic stock footage — nature, cities, sport, food, money, \
science, space, tech — so AVOID anything that needs footage of one specific \
living person, a logo/brand, or a copyrighted show;
- phrased as a punchy, curiosity-driving title (no hashtags, numbering, or quotes).

Ground each topic in a real trend above where you can, but GENERALISE it (a \
celebrity -> the sport/field they're famous for; a local budget -> the broader \
economic story; a movie -> how that kind of film/effect is made).

Return ONLY valid JSON, no prose, no code fence:
{{"topics": ["<title>", "<title>", ... exactly {n} of them]}}"""


def fetch_google_trends(geos: List[str]) -> List[str]:
    """Pull the daily trending searches for each geo, newest first, deduped."""
    terms: List[str] = []
    seen = set()
    for geo in geos:
        url = TRENDS_RSS_URL.format(geo=geo)
        try:
            resp = requests.get(url, headers={"User-Agent": _TRENDS_UA}, timeout=30)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
        except (requests.RequestException, ET.ParseError) as e:  # noqa: BLE001
            logger.warning(f"failed to fetch Google Trends for geo={geo}: {e}")
            continue
        # RSS 2.0: channel/item/title carries the trending query.
        for item in root.iter("item"):
            title_el = item.find("title")
            title = (title_el.text or "").strip() if title_el is not None else ""
            key = title.lower()
            if title and key not in seen:
                seen.add(key)
                terms.append(title)
        logger.info(f"Google Trends geo={geo}: {len(terms)} total terms so far")
    return terms


def _curate_topics(raw_terms: List[str], n: int) -> List[str]:
    """Ask the LLM to turn raw trending searches into renderable Shorts topics."""
    if not raw_terms:
        return []
    listed = "\n".join(f"- {t}" for t in raw_terms[:40])
    prompt = _CURATE_TEMPLATE.format(terms=listed, n=n)
    raw = _generate_response(prompt)
    if not raw or "Error: " in raw:
        logger.error(f"topic curation LLM call failed: {raw!r}")
        return []
    try:
        data = json.loads(_strip_code_fence(raw))
        topics = [str(t).strip().strip('"').lstrip("#").strip() for t in data.get("topics", [])]
    except (json.JSONDecodeError, AttributeError, TypeError) as e:
        logger.error(f"could not parse curated topics: {e}; raw={raw!r}")
        return []
    # Dedupe (case-insensitive) while preserving order, then cap at n.
    out: List[str] = []
    seen = set()
    for t in topics:
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out[:n]


def _write_topics(topics: List[str]) -> None:
    header = (
        "# AUTO-GENERATED DAILY by scripts/run_refresh_topics.py from Google Trends.\n"
        "# Do not hand-edit — the daily cron overwrites this file. Tune the\n"
        "# source regions via 'trends_geos' / count via 'daily_topic_count' in\n"
        "# autopilot/config.json. recent_topics_window is kept below the topic\n"
        "# count so the autopilot round-robins these and never invents off-topic.\n"
        f"# Last refreshed: {datetime.now(timezone.utc).isoformat()}\n"
    )
    os.makedirs(AUTOPILOT_DIR, exist_ok=True)
    with open(TOPICS_FILE, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("\n".join(topics) + "\n")


def refresh_topics() -> dict:
    """Fetch today's trends, curate N renderable topics, and lock them in.

    Keeps the round-robin invariant (topic count > recent_topics_window) so the
    autopilot cycles only these topics. On any failure the existing topics.txt is
    left untouched rather than wiped to garbage.
    """
    cfg = load_config()
    window = int(cfg.get("recent_topics_window", 5))
    # Always produce more topics than the window so pick_topic never falls
    # through to LLM invention.
    n = max(int(cfg.get("daily_topic_count", 6)), window + 1)

    raw_terms = fetch_google_trends(list(cfg.get("trends_geos", ["IN", "GB", "US"])))
    if not raw_terms:
        logger.error("no trends fetched; leaving existing topics.txt untouched")
        return {"ok": False, "error": "no trends fetched"}

    topics = _curate_topics(raw_terms, n)
    if len(topics) <= window:
        logger.error(
            f"curation returned {len(topics)} topics (need > {window}); "
            "leaving existing topics.txt untouched"
        )
        return {"ok": False, "error": "too few curated topics", "got": len(topics)}

    _write_topics(topics)

    # New topics -> reset the rotation, but keep the learned feedback + counter.
    state = load_state()
    state["recent_topics"] = []
    save_state(state)

    logger.success(f"refreshed daily topics ({len(topics)}): {topics}")
    return {"ok": True, "topics": topics, "source_terms": raw_terms[:40]}


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def run_once() -> dict:
    cfg = load_config()
    state = load_state()
    run_no = state["run_count"] + 1
    logger.info(f"\n\n===== autopilot run #{run_no} =====")

    # Pull the latest real retention numbers BEFORE composing the prompt, so
    # this run is written against what the channel actually knows today.
    perf = refresh_performance(cfg)
    if not perf.get("ok"):
        logger.info(f"performance refresh skipped: {perf.get('error')}")

    topic = pick_topic(cfg, state)
    logger.info(f"topic: {topic}")

    system_prompt = compose_system_prompt(state["feedback_notes"], cfg)
    candidates = generate_candidates(topic, cfg, system_prompt)
    if not candidates:
        logger.error("no usable script candidates generated; aborting run")
        return {"ok": False, "run": run_no, "topic": topic, "error": "no candidates"}

    verdict = judge_candidates(topic, candidates, cfg)
    winner = candidates[verdict["winner_index"]]
    logger.info(
        f"judge picked candidate {verdict['winner_index']} "
        f"of {len(candidates)}; {len(verdict['lessons'])} new lesson(s)"
    )

    video_path, task_id, render_meta = render_winner(topic, winner, cfg, run_no)
    upload_result = upload_to_youtube(topic, winner, video_path, cfg)

    # Persist learning for the next run.
    notes = (state["feedback_notes"] + verdict["lessons"])[-cfg["max_feedback_notes"]:]
    state["feedback_notes"] = notes
    state["run_count"] = run_no
    state["recent_topics"] = (state["recent_topics"] + [topic])[
        -cfg["recent_topics_window"]:
    ]
    save_state(state)

    entry = {
        "run": run_no,
        "ts": datetime.now(timezone.utc).isoformat(),
        "topic": topic,
        "task_id": task_id,
        "num_candidates": len(candidates),
        "winner_index": verdict["winner_index"],
        "scores": verdict["scores"],
        "lessons": verdict["lessons"],
        "video_path": video_path,
        # Recorded so a later analytics join can attribute retention to the
        # opening line and the render choices that produced it.
        "hook": extract_hook(winner),
        "title": upload_result.get("title", ""),
        "est_seconds": round(estimate_seconds(winner, cfg), 1),
        "word_count": len(winner.split()),
        **render_meta,
        "youtube": upload_result,
    }
    append_history(entry)

    logger.success(
        f"autopilot run #{run_no} done — "
        f"upload {'ok' if upload_result.get('success') else 'skipped/failed'}"
    )
    return {"ok": True, **entry}


if __name__ == "__main__":
    print(json.dumps(run_once(), ensure_ascii=False, default=str))
