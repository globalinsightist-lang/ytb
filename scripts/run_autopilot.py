"""Entry point for one autopilot iteration (used by the cron workflow).

Usage:
    uv run python scripts/run_autopilot.py
"""
import json
import os
import sys

# The project is not installed as a package (`[tool.uv] package = false`), so
# the repo root has to be on sys.path for `app.*` to import from a subdirectory.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.autopilot import run_once  # noqa: E402

if __name__ == "__main__":
    result = run_once()
    print(json.dumps(result, ensure_ascii=False, default=str))
    sys.exit(0 if result.get("ok") else 1)
