"""Entry point for the daily Google-Trends topic refresh (used by the cron).

Fetches today's trending searches, asks the LLM to curate them into a handful
of renderable Shorts topics, and writes them to autopilot/topics.txt. The
render/judge/upload autopilot then round-robins over those topics all day.

Usage:
    uv run python run_refresh_topics.py
"""
import json
import sys

from app.services.autopilot import refresh_topics

if __name__ == "__main__":
    result = refresh_topics()
    print(json.dumps(result, ensure_ascii=False, default=str))
    sys.exit(0 if result.get("ok") else 1)
