"""
Reddit ingester for Attention Flow Desk.
Uses PRAW to fetch posts from configured subreddits.
"""

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import praw
from praw.exceptions import PRAWException

from ..config import Config, get_config
from .. import db


@dataclass
class IngestResult:
    """Result of ingesting from a single subreddit."""
    subreddit: str
    success: bool
    posts_fetched: int
    snapshots_created: int
    error: Optional[str] = None


def create_reddit_client(config: Config) -> Optional[praw.Reddit]:
    """Create a PRAW Reddit client from config."""
    if not config.reddit_client_id or not config.reddit_client_secret:
        return None

    return praw.Reddit(
        client_id=config.reddit_client_id,
        client_secret=config.reddit_client_secret,
        user_agent=config.reddit_user_agent,
    )


def with_retry(func, max_attempts: int = 3, base_delay: float = 1.0):
    """Execute a function with exponential backoff retry."""
    last_error = None
    for attempt in range(max_attempts):
        try:
            return func()
        except (ConnectionError, TimeoutError, PRAWException) as e:
            last_error = e
            if attempt < max_attempts - 1:
                time.sleep(base_delay * (2 ** attempt))
    raise last_error


def ingest_subreddit(
    reddit: praw.Reddit,
    subreddit_name: str,
    label: str,
    run_id: str,
    ts: str,
    limit: int = 50
) -> IngestResult:
    """
    Ingest posts from a single subreddit.

    Args:
        reddit: PRAW Reddit instance
        subreddit_name: Name of the subreddit
        label: Label for the actor
        run_id: Current run ID
        ts: Timestamp for this run
        limit: Maximum posts to fetch

    Returns:
        IngestResult with counts and status
    """
    actor_id = f"reddit:{subreddit_name.lower()}"

    try:
        # Ensure actor exists
        db.upsert_actor(actor_id, "reddit", label)

        # Fetch posts with retry
        def fetch_posts():
            subreddit = reddit.subreddit(subreddit_name)
            return list(subreddit.new(limit=limit))

        posts = with_retry(fetch_posts)

        posts_fetched = 0
        snapshots_created = 0

        for submission in posts:
            post_id = f"reddit:{submission.id}"
            url = f"https://reddit.com{submission.permalink}"
            title = submission.title
            # Convert UTC timestamp
            published_at = datetime.fromtimestamp(
                submission.created_utc, tz=timezone.utc
            ).isoformat()

            # Store post metadata (exclude username for privacy)
            meta = {
                "subreddit": subreddit_name,
                "is_self": submission.is_self,
                "upvote_ratio": submission.upvote_ratio,
            }

            # Upsert the post
            db.upsert_post(
                post_id=post_id,
                source="reddit",
                actor_id=actor_id,
                url=url,
                title=title,
                published_at=published_at,
                meta=meta
            )
            posts_fetched += 1

            # Create snapshot
            if db.insert_snapshot(
                post_id=post_id,
                run_id=run_id,
                ts=ts,
                score=submission.score,
                num_comments=submission.num_comments,
                other={"upvote_ratio": submission.upvote_ratio}
            ):
                snapshots_created += 1

        return IngestResult(
            subreddit=subreddit_name,
            success=True,
            posts_fetched=posts_fetched,
            snapshots_created=snapshots_created
        )

    except Exception as e:
        return IngestResult(
            subreddit=subreddit_name,
            success=False,
            posts_fetched=0,
            snapshots_created=0,
            error=str(e)
        )


def ingest_all_subreddits(run_id: str, ts: str) -> list[IngestResult]:
    """
    Ingest from all configured subreddits.

    Args:
        run_id: Current run ID
        ts: Timestamp for this run

    Returns:
        List of IngestResult, one per subreddit
    """
    config = get_config()
    results = []

    reddit = create_reddit_client(config)
    if reddit is None:
        # Return error result for each subreddit
        for actor in config.watchlist.reddit:
            results.append(IngestResult(
                subreddit=actor.subreddit,
                success=False,
                posts_fetched=0,
                snapshots_created=0,
                error="Reddit credentials not configured"
            ))
        return results

    for actor in config.watchlist.reddit:
        result = ingest_subreddit(
            reddit=reddit,
            subreddit_name=actor.subreddit,
            label=actor.label,
            run_id=run_id,
            ts=ts
        )
        results.append(result)

    return results
