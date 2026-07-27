"""Entry point for the daily Google-Trends topic refresh (used by the cron).

Fetches today's trending searches, asks the LLM to curate them into a handful
of renderable Shorts topics, and writes them to autopilot/topics.txt. The
render/judge/upload autopilot then round-robins over those topics all day.

Usage:
    uv run python scripts/run_refresh_topics.py
"""
import json
import os
import sys

# The project is not installed as a package (`[tool.uv] package = false`), so
# the repo root has to be on sys.path for `app.*` to import from a subdirectory.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.autopilot import refresh_topics  # noqa: E402

if __name__ == "__main__":
    result = refresh_topics()
    print(json.dumps(result, ensure_ascii=False, default=str))
    sys.exit(0 if result.get("ok") else 1)
