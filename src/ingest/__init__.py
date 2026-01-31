"""
Ingestion orchestrator for Attention Flow Desk.
Coordinates data fetching from all configured sources.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .. import db
from ..config import get_config


@dataclass
class SourceStatus:
    """Status of a single source ingestion."""
    source: str
    success: bool
    actors_processed: int
    posts_fetched: int
    snapshots_created: int
    errors: list[str] = field(default_factory=list)


@dataclass
class IngestionResult:
    """Result of a complete ingestion run."""
    run_id: str
    started_at: str
    ended_at: Optional[str]
    status: str  # success, partial, failed
    sources: dict[str, SourceStatus] = field(default_factory=dict)
    total_posts: int = 0
    total_snapshots: int = 0


def run_ingestion(sources: Optional[list[str]] = None) -> IngestionResult:
    """
    Run ingestion for all configured sources.

    Args:
        sources: Optional list of sources to ingest ("reddit", "youtube").
                 If None, ingests from all sources.

    Returns:
        IngestionResult with status and counts.
    """
    config = get_config()

    # Initialize database if needed
    db.init_db()

    # Create run record
    run_id = db.create_run()
    started_at = datetime.now(timezone.utc).isoformat()
    ts = started_at  # Use same timestamp for all snapshots in this run

    result = IngestionResult(
        run_id=run_id,
        started_at=started_at,
        ended_at=None,
        status="running"
    )

    # Determine which sources to run
    if sources is None:
        sources = []
        if config.watchlist.reddit:
            sources.append("reddit")
        if config.watchlist.youtube:
            sources.append("youtube")

    # Run each source ingester
    for source in sources:
        if source == "reddit":
            result.sources["reddit"] = _ingest_reddit(run_id, ts)
        elif source == "youtube":
            result.sources["youtube"] = _ingest_youtube(run_id, ts)

    # Calculate totals
    for source_status in result.sources.values():
        result.total_posts += source_status.posts_fetched
        result.total_snapshots += source_status.snapshots_created

    # Determine overall status
    all_success = all(s.success for s in result.sources.values())
    any_success = any(s.success for s in result.sources.values())

    if all_success:
        result.status = "success"
    elif any_success:
        result.status = "partial"
    else:
        result.status = "failed"

    # Complete the run
    result.ended_at = datetime.now(timezone.utc).isoformat()

    sources_ok = {name: s.success for name, s in result.sources.items()}
    counts = {
        "posts": result.total_posts,
        "snapshots": result.total_snapshots
    }

    db.complete_run(run_id, result.status, sources_ok, counts)

    return result


def _ingest_reddit(run_id: str, ts: str) -> SourceStatus:
    """Run Reddit ingestion."""
    from .reddit import ingest_all_subreddits

    status = SourceStatus(
        source="reddit",
        success=False,
        actors_processed=0,
        posts_fetched=0,
        snapshots_created=0
    )

    try:
        results = ingest_all_subreddits(run_id, ts)

        for r in results:
            status.actors_processed += 1
            status.posts_fetched += r.posts_fetched
            status.snapshots_created += r.snapshots_created
            if not r.success and r.error:
                status.errors.append(f"{r.subreddit}: {r.error}")

        # Success if at least one subreddit succeeded
        status.success = any(r.success for r in results)

    except Exception as e:
        status.errors.append(str(e))

    return status


def _ingest_youtube(run_id: str, ts: str) -> SourceStatus:
    """Run YouTube ingestion."""
    from .youtube import ingest_all_channels

    status = SourceStatus(
        source="youtube",
        success=False,
        actors_processed=0,
        posts_fetched=0,
        snapshots_created=0
    )

    try:
        results = ingest_all_channels(run_id, ts)

        for r in results:
            status.actors_processed += 1
            status.posts_fetched += r.videos_fetched
            status.snapshots_created += r.snapshots_created
            if not r.success and r.error:
                status.errors.append(f"{r.channel_id}: {r.error}")

        # Success if at least one channel succeeded
        status.success = any(r.success for r in results)

    except Exception as e:
        status.errors.append(str(e))

    return status
