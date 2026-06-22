"""
Self-improving autopilot loop for one cron iteration.

`run_once()` performs a full cycle:
  1. Pick a topic (round-robin over autopilot/topics.txt, else LLM-invented).
  2. Generate N candidate scripts with the *current* evolving system prompt.
  3. LLM-as-judge ranks the candidates and emits reusable lessons.
  4. Render only the winning script into a final video (reuses the pipeline).
  5. Upload the final cut to YouTube.
  6. Persist the lessons into the system prompt for the next run.

All persistent learning lives under autopilot/ (a *tracked* directory) so a
CI job can commit the updated state back to the repo between runs — storage/
is gitignored and would be wiped on every fresh checkout.
"""
import json
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from os import path
from typing import List

import requests
from loguru import logger

from app.models.schema import VideoParams
from app.services import llm, task, youtube_upload
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

DEFAULT_CONFIG = {
    "niche": "fascinating science and history facts",
    "language": "en",
    "paragraph_number": 1,
    "num_candidates": 4,
    "aspect": "9:16",
    "video_source": "pexels",
    "voice_name": "en-US-JennyNeural-Female",
    "bgm_type": "random",
    "subtitle_enabled": True,
    "upload_enabled": True,
    "privacy_status": "public",
    "youtube_category_id": "22",
    "max_feedback_notes": 12,
    "recent_topics_window": 5,
    # Daily Google-Trends topic refresh (run_refresh_topics.py).
    "trends_geos": ["IN", "GB", "US"],
    "daily_topic_count": 6,
}

DEFAULT_STATE = {"run_count": 0, "recent_topics": [], "feedback_notes": []}

_JUDGE_TEMPLATE = """You are a ruthless short-form video editor judging script candidates \
for a vertical YouTube Short on the topic: "{topic}".

There are {n} candidate scripts below. Each will be read aloud over stock \
footage in ~30-50 seconds.

{candidates}

Score each candidate from 0-10 on: hook strength (the very first sentence must \
stop the scroll), pacing/retention, clarity for a general audience, and a \
satisfying payoff. Then pick the single best candidate.

Finally, write 1-3 SHORT, reusable guidelines that would make the NEXT batch of \
scripts better. The guidelines must be general (not specific to this topic), \
concrete, and actionable.

Return ONLY valid JSON, no prose, no code fence:
{{"winner_index": <0-based int>, "scores": [{{"index": 0, "total": <number>, "reason": "<one line>"}}], "lessons": ["<guideline>"]}}"""


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


def compose_system_prompt(notes: List[str]) -> str:
    if not notes:
        return DEFAULT_SCRIPT_SYSTEM_PROMPT
    guidelines = "\n".join(f"- {n}" for n in notes)
    composed = (
        f"{DEFAULT_SCRIPT_SYSTEM_PROMPT}\n\n"
        f"## Learned guidelines (apply these strictly):\n{guidelines}"
    )
    return composed[:7900]  # stay under the 8000-char cap enforced by llm.py


def generate_candidates(topic: str, cfg: dict, system_prompt: str) -> List[str]:
    candidates: List[str] = []
    seen = set()
    for i in range(max(1, int(cfg["num_candidates"]))):
        script = llm.generate_script(
            video_subject=topic,
            language=cfg["language"],
            paragraph_number=cfg["paragraph_number"],
            custom_system_prompt=system_prompt,
        )
        script = (script or "").strip()
        if script and "Error: " not in script and script not in seen:
            candidates.append(script)
            seen.add(script)
        else:
            logger.warning(f"candidate {i + 1} was empty/duplicate/error, skipping")
    return candidates


def judge_candidates(topic: str, candidates: List[str]) -> dict:
    if len(candidates) <= 1:
        return {"winner_index": 0, "scores": [], "lessons": []}

    block = "\n\n".join(
        f"### Candidate {i}\n{c}" for i, c in enumerate(candidates)
    )
    prompt = _JUDGE_TEMPLATE.format(topic=topic, n=len(candidates), candidates=block)
    try:
        raw = _generate_response(prompt)
        data = json.loads(_strip_code_fence(raw))
        winner = int(data.get("winner_index", 0))
        if winner < 0 or winner >= len(candidates):
            winner = 0
        lessons = [str(x).strip() for x in data.get("lessons", []) if str(x).strip()]
        return {
            "winner_index": winner,
            "scores": data.get("scores", []),
            "lessons": lessons[:3],
        }
    except Exception as e:  # noqa: BLE001 - never let judging crash the run
        logger.warning(f"judge failed, defaulting to first candidate: {e}")
        return {"winner_index": 0, "scores": [], "lessons": []}


def render_winner(topic: str, script: str, cfg: dict) -> tuple[str, str]:
    """Render the winning script and return (final_video_path, task_id)."""
    params = VideoParams(
        video_subject=topic,
        video_script=script,  # non-empty -> pipeline skips script generation
        video_count=1,
        video_aspect=cfg["aspect"],
        video_source=cfg["video_source"],
        voice_name=cfg["voice_name"],
        video_language=cfg["language"],
        subtitle_enabled=cfg["subtitle_enabled"],
        bgm_type=cfg["bgm_type"],
    )
    task_id = utils.get_uuid()
    result = task.start(task_id=task_id, params=params, stop_at="video")
    if not result or not result.get("videos"):
        raise RuntimeError("rendering produced no video")
    return result["videos"][0], task_id


def upload_to_youtube(topic: str, script: str, video_path: str, cfg: dict) -> dict:
    if not cfg.get("upload_enabled", True):
        logger.info("upload_enabled is false, keeping the video local")
        return {"success": False, "error": "upload disabled"}
    if not youtube_upload.is_configured():
        logger.warning("YouTube not configured; skipping upload")
        return {"success": False, "error": "YouTube not configured"}

    meta = llm.generate_social_metadata(
        video_subject=topic,
        video_script=script,
        language=cfg["language"],
        platform="youtube_shorts",
    )
    hashtags = meta.get("hashtags", [])
    description = meta.get("caption", "")
    tag_line = " ".join(f"#{h.lstrip('#')}" for h in hashtags)
    description = f"{description}\n\n{tag_line}\n#shorts".strip()

    return youtube_upload.upload_video(
        video_path=video_path,
        title=meta.get("title", topic),
        description=description,
        tags=hashtags,
        privacy_status=cfg["privacy_status"],
        category_id=cfg["youtube_category_id"],
    )


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
        "# AUTO-GENERATED DAILY by run_refresh_topics.py from Google Trends.\n"
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

    topic = pick_topic(cfg, state)
    logger.info(f"topic: {topic}")

    system_prompt = compose_system_prompt(state["feedback_notes"])
    candidates = generate_candidates(topic, cfg, system_prompt)
    if not candidates:
        logger.error("no usable script candidates generated; aborting run")
        return {"ok": False, "run": run_no, "topic": topic, "error": "no candidates"}

    verdict = judge_candidates(topic, candidates)
    winner = candidates[verdict["winner_index"]]
    logger.info(
        f"judge picked candidate {verdict['winner_index']} "
        f"of {len(candidates)}; {len(verdict['lessons'])} new lesson(s)"
    )

    video_path, task_id = render_winner(topic, winner, cfg)
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
