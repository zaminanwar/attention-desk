"""
YouTube ingester for Attention Flow Desk.
Uses YouTube Data API v3 with uploads playlist for efficient quota usage.
"""

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from ..config import Config, get_config
from .. import db


@dataclass
class IngestResult:
    """Result of ingesting from a single YouTube channel."""
    channel_id: str
    success: bool
    videos_fetched: int
    snapshots_created: int
    error: Optional[str] = None


def create_youtube_client(config: Config):
    """Create a YouTube API client from config."""
    if not config.youtube_api_key:
        return None

    return build("youtube", "v3", developerKey=config.youtube_api_key)


def channel_to_uploads_playlist(channel_id: str) -> str:
    """
    Convert a channel ID to its uploads playlist ID.
    UC... → UU...
    """
    if channel_id.startswith("UC"):
        return "UU" + channel_id[2:]
    return channel_id


def with_retry(func, max_attempts: int = 3, base_delay: float = 1.0):
    """Execute a function with exponential backoff retry."""
    last_error = None
    for attempt in range(max_attempts):
        try:
            return func()
        except (ConnectionError, TimeoutError, HttpError) as e:
            last_error = e
            # Don't retry on quota errors or auth errors
            if isinstance(e, HttpError):
                if e.resp.status in (401, 403):
                    raise
            if attempt < max_attempts - 1:
                time.sleep(base_delay * (2 ** attempt))
    raise last_error


def ingest_channel(
    youtube,
    channel_id: str,
    label: str,
    run_id: str,
    ts: str,
    max_videos: int = 20
) -> IngestResult:
    """
    Ingest videos from a single YouTube channel.

    Uses the uploads playlist method for efficiency:
    - playlistItems.list: 1 quota unit
    - videos.list (batch): 1 quota unit
    Total: ~2 units per channel vs 101 with search

    Args:
        youtube: YouTube API client
        channel_id: YouTube channel ID (UCxxxx format)
        label: Label for the actor
        run_id: Current run ID
        ts: Timestamp for this run
        max_videos: Maximum videos to fetch

    Returns:
        IngestResult with counts and status
    """
    actor_id = f"youtube:{channel_id}"

    try:
        # Ensure actor exists
        db.upsert_actor(actor_id, "youtube", label, {"channel_id": channel_id})

        # Get uploads playlist ID
        uploads_playlist_id = channel_to_uploads_playlist(channel_id)

        # Fetch recent uploads (1 quota unit)
        def fetch_playlist_items():
            return youtube.playlistItems().list(
                playlistId=uploads_playlist_id,
                part="snippet",
                maxResults=max_videos
            ).execute()

        playlist_response = with_retry(fetch_playlist_items)
        items = playlist_response.get("items", [])

        if not items:
            return IngestResult(
                channel_id=channel_id,
                success=True,
                videos_fetched=0,
                snapshots_created=0
            )

        # Extract video IDs
        video_ids = []
        video_snippets = {}
        for item in items:
            snippet = item.get("snippet", {})
            video_id = snippet.get("resourceId", {}).get("videoId")
            if video_id:
                video_ids.append(video_id)
                video_snippets[video_id] = snippet

        # Fetch video statistics in batch (1 quota unit for up to 50 videos)
        def fetch_video_stats():
            return youtube.videos().list(
                id=",".join(video_ids),
                part="statistics,contentDetails"
            ).execute()

        stats_response = with_retry(fetch_video_stats)
        video_stats = {
            v["id"]: v for v in stats_response.get("items", [])
        }

        videos_fetched = 0
        snapshots_created = 0

        for video_id in video_ids:
            snippet = video_snippets.get(video_id, {})
            stats = video_stats.get(video_id, {})

            post_id = f"youtube:{video_id}"
            url = f"https://www.youtube.com/watch?v={video_id}"
            title = snippet.get("title", "Untitled")
            published_at = snippet.get("publishedAt", ts)

            # Store post metadata
            meta = {
                "channel_id": channel_id,
                "thumbnail": snippet.get("thumbnails", {}).get("medium", {}).get("url"),
            }

            # Upsert the post
            db.upsert_post(
                post_id=post_id,
                source="youtube",
                actor_id=actor_id,
                url=url,
                title=title,
                published_at=published_at,
                meta=meta
            )
            videos_fetched += 1

            # Extract statistics
            statistics = stats.get("statistics", {})
            view_count = int(statistics.get("viewCount", 0)) if statistics.get("viewCount") else None
            like_count = int(statistics.get("likeCount", 0)) if statistics.get("likeCount") else None
            comment_count = int(statistics.get("commentCount", 0)) if statistics.get("commentCount") else None

            # Create snapshot
            if db.insert_snapshot(
                post_id=post_id,
                run_id=run_id,
                ts=ts,
                view_count=view_count,
                like_count=like_count,
                comment_count=comment_count
            ):
                snapshots_created += 1

        return IngestResult(
            channel_id=channel_id,
            success=True,
            videos_fetched=videos_fetched,
            snapshots_created=snapshots_created
        )

    except HttpError as e:
        error_msg = str(e)
        if e.resp.status == 403:
            error_msg = "Quota exceeded or access denied"
        elif e.resp.status == 404:
            error_msg = "Channel or playlist not found"
        return IngestResult(
            channel_id=channel_id,
            success=False,
            videos_fetched=0,
            snapshots_created=0,
            error=error_msg
        )
    except Exception as e:
        return IngestResult(
            channel_id=channel_id,
            success=False,
            videos_fetched=0,
            snapshots_created=0,
            error=str(e)
        )


def ingest_all_channels(run_id: str, ts: str) -> list[IngestResult]:
    """
    Ingest from all configured YouTube channels.

    Args:
        run_id: Current run ID
        ts: Timestamp for this run

    Returns:
        List of IngestResult, one per channel
    """
    config = get_config()
    results = []

    youtube = create_youtube_client(config)
    if youtube is None:
        # Return error result for each channel
        for actor in config.watchlist.youtube:
            results.append(IngestResult(
                channel_id=actor.channel_id,
                success=False,
                videos_fetched=0,
                snapshots_created=0,
                error="YouTube API key not configured"
            ))
        return results

    for actor in config.watchlist.youtube:
        result = ingest_channel(
            youtube=youtube,
            channel_id=actor.channel_id,
            label=actor.label,
            run_id=run_id,
            ts=ts
        )
        results.append(result)

    return results
