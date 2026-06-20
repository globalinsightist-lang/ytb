"""Entry point for one autopilot iteration (used by the cron workflow).

Usage:
    uv run python run_autopilot.py
"""
import json
import sys

from app.services.autopilot import run_once

if __name__ == "__main__":
    result = run_once()
    print(json.dumps(result, ensure_ascii=False, default=str))
    sys.exit(0 if result.get("ok") else 1)
