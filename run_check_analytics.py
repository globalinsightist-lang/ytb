"""Read-only diagnostic: does YT_REFRESH_TOKEN actually carry the analytics scope?

A refresh token permanently carries whatever scopes were granted at the moment
it was minted. Re-consenting with an extra scope but keeping the OLD token
changes nothing — which is the usual reason "I'm sure I granted it" and "the
API returns 403" are both true at once. This checks the token itself rather
than what was clicked.

Publishes nothing, uploads nothing, costs no YouTube quota.

Usage (locally, with the three secrets exported):
    YT_CLIENT_ID=... YT_CLIENT_SECRET=... YT_REFRESH_TOKEN=... \\
        uv run python run_check_analytics.py

Or run the "Check Analytics Scope" workflow, which reads the same values from
the repo secrets.

Exit code 0 = analytics is usable, 1 = something needs fixing.
"""
import json
import os
import sys

from app.services import youtube_analytics as ya

REQUIRED = ya.ANALYTICS_SCOPE
HISTORY_FILE = os.path.join(os.path.dirname(__file__), "autopilot", "history.jsonl")


def _uploaded_video_ids() -> list:
    """Read history.jsonl directly rather than importing the autopilot module,
    so this diagnostic stays runnable without the rest of the pipeline."""
    if not os.path.isfile(HISTORY_FILE):
        return []
    ids = []
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            vid = (row.get("youtube") or {}).get("video_id")
            if vid:
                ids.append(vid)
    return ids


def main() -> int:
    if not ya.is_configured():
        print("❌ YT_CLIENT_ID / YT_CLIENT_SECRET / YT_REFRESH_TOKEN are not all set.")
        return 1
    print("✅ credentials present")

    scopes = ya.granted_scopes()
    if scopes is None:
        print("❌ could not exchange the refresh token for an access token.")
        print("   The token is expired, revoked, or the client id/secret don't match it.")
        return 1

    print(f"\ngranted scopes ({len(scopes)}):")
    for s in sorted(scopes):
        print(f"   - {s}")

    has_scope = REQUIRED in scopes
    print(f"\n{'✅' if has_scope else '❌'} {REQUIRED}: {'GRANTED' if has_scope else 'MISSING'}")
    if not has_scope:
        print(
            "\n   Re-consent with BOTH scopes and replace YT_REFRESH_TOKEN:\n"
            "     https://www.googleapis.com/auth/youtube.upload\n"
            f"     {REQUIRED}\n"
            "   Generating a new token is required — adding the scope in the\n"
            "   consent screen does not retrofit a token already issued."
        )
        return 1

    # Scope present is necessary but not sufficient; prove a real query works.
    video_ids = _uploaded_video_ids()
    if not video_ids:
        print("\n⚠️  no uploaded videos in history.jsonl to test a live query against.")
        return 0

    sample = video_ids[-25:]
    print(f"\nrunning a live query against the {len(sample)} most recent videos...")
    stats = ya.fetch_video_stats(sample)
    if not stats:
        print("❌ scope is granted but the query returned nothing. See the error above.")
        return 1

    print(f"✅ analytics returned {len(stats)} rows. Sample:")
    for vid, m in list(stats.items())[:5]:
        print(
            f"   {vid}  views={m.get('views')}  "
            f"avg_view={m.get('averageViewPercentage')}%  "
            f"subs={m.get('subscribersGained')}"
        )
    print("\n✅ the learning loop has ground truth. Nothing further needed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
