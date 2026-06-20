"""
Direct YouTube Data API v3 uploader.

Free path to publish to your own channel: no third-party SaaS, no extra
dependencies (uses the `requests` dep that is already vendored). Auth is a
long-lived OAuth2 refresh token exchanged for a short-lived access token on
each upload.

Credentials are read from environment variables (preferred, so secrets stay
out of config.toml) with a fallback to config.app:
    YT_CLIENT_ID / YT_CLIENT_SECRET / YT_REFRESH_TOKEN

Quota note: videos.insert costs 1600 units against the default 10,000/day
project quota (~6 uploads/day). To publish more, request a quota increase in
the Google Cloud console for your project.
"""
import json
import os
from typing import List, Optional

import requests
from loguru import logger

from app.config import config

OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"

# YouTube hard limits on the snippet fields.
_MAX_TITLE = 100
_MAX_DESCRIPTION = 5000
_MAX_TAGS_TOTAL = 450  # YouTube caps total tag chars at 500; stay under it.


def _cred(env_name: str, config_key: str) -> str:
    return (os.environ.get(env_name) or config.app.get(config_key, "") or "").strip()


def _load_credentials() -> Optional[dict]:
    client_id = _cred("YT_CLIENT_ID", "youtube_client_id")
    client_secret = _cred("YT_CLIENT_SECRET", "youtube_client_secret")
    refresh_token = _cred("YT_REFRESH_TOKEN", "youtube_refresh_token")
    if not (client_id and client_secret and refresh_token):
        return None
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    }


def is_configured() -> bool:
    return _load_credentials() is not None


def _get_access_token(creds: dict) -> str:
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
    token = resp.json().get("access_token")
    if not token:
        raise ValueError("token endpoint returned no access_token")
    return token


def _clean_tags(tags: Optional[List[str]]) -> List[str]:
    cleaned: List[str] = []
    total = 0
    for tag in tags or []:
        tag = str(tag).lstrip("#").strip()
        if not tag:
            continue
        # +1 accounts for the separator YouTube counts between tags.
        if total + len(tag) + 1 > _MAX_TAGS_TOTAL:
            break
        cleaned.append(tag)
        total += len(tag) + 1
    return cleaned


def upload_video(
    video_path: str,
    title: str,
    description: str = "",
    tags: Optional[List[str]] = None,
    privacy_status: str = "public",
    category_id: str = "22",
    made_for_kids: bool = False,
) -> dict:
    """Upload a single file to YouTube via a resumable session.

    Returns {"success": bool, "video_id": str, "url": str} or
    {"success": False, "error": str}.
    """
    creds = _load_credentials()
    if creds is None:
        logger.warning(
            "YouTube credentials not set "
            "(YT_CLIENT_ID / YT_CLIENT_SECRET / YT_REFRESH_TOKEN). Skipping upload."
        )
        return {"success": False, "error": "YouTube not configured"}

    if not os.path.isfile(video_path):
        return {"success": False, "error": f"video file not found: {video_path}"}

    try:
        access_token = _get_access_token(creds)
    except Exception as e:  # noqa: BLE001 - surface a clean error upstream
        logger.error(f"failed to refresh YouTube access token: {e}")
        return {"success": False, "error": f"token refresh failed: {e}"}

    metadata = {
        "snippet": {
            "title": (title or "Untitled")[:_MAX_TITLE],
            "description": (description or "")[:_MAX_DESCRIPTION],
            "tags": _clean_tags(tags),
            "categoryId": str(category_id),
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": made_for_kids,
        },
    }

    file_size = os.path.getsize(video_path)
    logger.info(
        f"uploading to YouTube: {os.path.basename(video_path)} "
        f"({file_size / 1_048_576:.1f} MB), privacy={privacy_status}"
    )

    try:
        # 1. Initiate resumable upload session.
        init = requests.post(
            UPLOAD_URL,
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Length": str(file_size),
                "X-Upload-Content-Type": "video/*",
            },
            data=json.dumps(metadata),
            timeout=60,
        )
        init.raise_for_status()
        session_url = init.headers.get("Location")
        if not session_url:
            return {"success": False, "error": "no resumable session URL returned"}

        # 2. Upload the bytes in a single PUT (shorts are small).
        with open(video_path, "rb") as f:
            put = requests.put(
                session_url,
                headers={
                    "Content-Type": "video/*",
                    "Content-Length": str(file_size),
                },
                data=f,
                timeout=600,
            )
        put.raise_for_status()
        body = put.json()
        video_id = body.get("id")
        if not video_id:
            return {"success": False, "error": f"upload response missing id: {body}"}

        url = f"https://youtu.be/{video_id}"
        logger.success(f"✅ uploaded to YouTube: {url}")
        return {"success": True, "video_id": video_id, "url": url}

    except requests.exceptions.HTTPError as e:
        detail = e.response.text if e.response is not None else str(e)
        # 403 with quotaExceeded is the most common failure once you pass ~6/day.
        logger.error(f"YouTube upload failed (HTTP): {detail}")
        return {"success": False, "error": detail}
    except requests.exceptions.RequestException as e:
        logger.error(f"YouTube upload failed: {e}")
        return {"success": False, "error": str(e)}
