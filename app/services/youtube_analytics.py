"""
YouTube Analytics API v2 reader — the autopilot's ground truth.

The autopilot used to "learn" purely from an LLM judging its own scripts
against each other, with no signal from the platform. That loop had no anchor
and converged on generic writing advice. This module closes it: it pulls the
real per-video retention numbers so the script prompt can be rebuilt from what
actually held viewers on this channel.

Auth reuses the uploader's OAuth2 refresh-token flow, but needs an ADDITIONAL
scope that upload-only tokens do not carry:

    https://www.googleapis.com/auth/yt-analytics.readonly

A token minted for uploads alone gets a 403 here. Re-consent once with both
scopes and replace YT_REFRESH_TOKEN. Until that happens every function in this
module degrades to "no data" rather than raising — a missing analytics scope
must never take down the render/upload pipeline.

Reporting lag: YouTube Analytics is not real-time. Figures for the last ~48h
are incomplete, so callers should only trust rows for videos at least two days
old (see MIN_AGE_DAYS).
"""
import os
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests
from loguru import logger

from app.services.youtube_upload import OAUTH_TOKEN_URL, _load_credentials

REPORTS_URL = "https://youtubeanalytics.googleapis.com/v2/reports"
ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"

# YouTube Analytics lags ~48h; rows younger than this are still settling.
MIN_AGE_DAYS = 2

# The Analytics API caps `filters=video==` at 500 ids; stay well under.
_BATCH_SIZE = 200

# averageViewPercentage is the closest available proxy for "did the algorithm
# keep pushing this" — it is the metric the prompt rebuild is keyed on.
_METRICS = (
    "views,estimatedMinutesWatched,averageViewDuration,"
    "averageViewPercentage,subscribersGained,likes,shares"
)


def _access_token() -> Optional[str]:
    creds = _load_credentials()
    if creds is None:
        logger.warning("YouTube credentials not set; skipping analytics fetch")
        return None
    try:
        resp = requests.post(
            OAUTH_TOKEN_URL,
            data={
                "client_id": creds["client_id"],
                "client_secret": creds["client_secret"],
                "refresh_token": creds["refresh_token"],
                "grant_type": "refresh_token",
            },
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("access_token")
    except requests.RequestException as e:
        logger.error(f"failed to refresh access token for analytics: {e}")
        return None


def is_configured() -> bool:
    """True when upload credentials exist. Does NOT prove the analytics scope
    was granted — only a live call can, and `fetch_video_stats` reports that."""
    return _load_credentials() is not None


def _query_batch(token: str, video_ids: List[str], start: str, end: str) -> Dict[str, dict]:
    params = {
        "ids": "channel==MINE",
        "startDate": start,
        "endDate": end,
        "metrics": _METRICS,
        "dimensions": "video",
        "filters": "video==" + ",".join(video_ids),
        "maxResults": len(video_ids),
    }
    resp = requests.get(
        REPORTS_URL,
        params=params,
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    if resp.status_code == 403:
        # Almost always the missing yt-analytics.readonly scope.
        raise PermissionError(resp.text)
    resp.raise_for_status()
    body = resp.json()

    headers = [h["name"] for h in body.get("columnHeaders", [])]
    out: Dict[str, dict] = {}
    for row in body.get("rows", []) or []:
        record = dict(zip(headers, row))
        vid = record.pop("video", None)
        if vid:
            out[vid] = record
    return out


def fetch_video_stats(
    video_ids: List[str],
    start_date: str = "2020-01-01",
    end_date: Optional[str] = None,
) -> Dict[str, dict]:
    """Return {video_id: {views, averageViewPercentage, ...}} for the ids given.

    Returns {} (never raises) when credentials are absent, the analytics scope
    was not granted, or the API errors — the caller treats "no data" as "skip
    the evidence block", which keeps the pipeline running.
    """
    video_ids = [v for v in dict.fromkeys(video_ids) if v]
    if not video_ids:
        return {}

    token = _access_token()
    if not token:
        return {}

    end_date = end_date or date.today().isoformat()
    stats: Dict[str, dict] = {}
    for i in range(0, len(video_ids), _BATCH_SIZE):
        batch = video_ids[i : i + _BATCH_SIZE]
        try:
            stats.update(_query_batch(token, batch, start_date, end_date))
        except PermissionError as e:
            logger.error(
                "YouTube Analytics returned 403 — the refresh token is most "
                f"likely missing the '{ANALYTICS_SCOPE}' scope. Re-consent and "
                f"replace YT_REFRESH_TOKEN. Detail: {str(e)[:300]}"
            )
            return {}
        except requests.RequestException as e:
            logger.error(f"analytics query failed for batch {i // _BATCH_SIZE}: {e}")
            continue

    logger.info(f"fetched analytics for {len(stats)}/{len(video_ids)} videos")
    return stats


def is_settled(published_iso: str, min_age_days: int = MIN_AGE_DAYS) -> bool:
    """True when a video is old enough that its analytics have stopped moving."""
    try:
        ts = datetime.fromisoformat(str(published_iso).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - ts >= timedelta(days=min_age_days)
