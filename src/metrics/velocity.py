"""
Velocity calculation for Attention Flow Desk.
Computes engagement velocity using snapshot deltas.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from .. import db


@dataclass
class VelocityResult:
    """Result of velocity calculation for a single post."""
    post_id: str
    velocity_6h: Optional[float] = None
    velocity_24h: Optional[float] = None
    snapshot_count: int = 0
    post_age_hours: Optional[float] = None


def parse_iso_datetime(iso_str: str) -> datetime:
    """Parse ISO 8601 datetime string to datetime object."""
    # Handle both with and without timezone
    if iso_str.endswith("Z"):
        iso_str = iso_str[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(iso_str)
    except ValueError:
        # Fallback for formats without timezone
        dt = datetime.fromisoformat(iso_str.replace("Z", ""))
        return dt.replace(tzinfo=timezone.utc)


def find_comparison_snapshot(
    snapshots: list[dict],
    now: datetime,
    target_hours: float,
    window_start_hours: float,
    window_end_hours: float
) -> Optional[dict]:
    """
    Find the best comparison snapshot within a time window.

    Args:
        snapshots: List of snapshot dicts, sorted by ts descending
        now: Current timestamp
        target_hours: Ideal hours ago for comparison (e.g., 6 for 6h velocity)
        window_start_hours: Start of acceptable window (e.g., 8 for t-8h)
        window_end_hours: End of acceptable window (e.g., 4 for t-4h)

    Returns:
        Best matching snapshot or None if none found in window.
    """
    target_time = now - timedelta(hours=target_hours)
    window_start = now - timedelta(hours=window_start_hours)
    window_end = now - timedelta(hours=window_end_hours)

    best_snapshot = None
    best_distance = None

    for snap in snapshots:
        snap_time = parse_iso_datetime(snap["ts"])

        # Check if within window
        if window_start <= snap_time <= window_end:
            distance = abs((snap_time - target_time).total_seconds())
            if best_distance is None or distance < best_distance:
                best_snapshot = snap
                best_distance = distance

    return best_snapshot


def calculate_velocity(
    current_value: Optional[int],
    comparison_value: Optional[int],
    hours_elapsed: float
) -> Optional[float]:
    """
    Calculate velocity (change per hour).

    Returns None if inputs are invalid.
    """
    if current_value is None or comparison_value is None:
        return None
    if hours_elapsed <= 0:
        return None

    delta = current_value - comparison_value
    return delta / hours_elapsed


def compute_post_velocity(post_id: str, now: Optional[datetime] = None) -> VelocityResult:
    """
    Compute velocity metrics for a single post.

    Args:
        post_id: The post ID to compute velocity for
        now: Current timestamp (defaults to UTC now)

    Returns:
        VelocityResult with computed velocities
    """
    if now is None:
        now = datetime.now(timezone.utc)

    result = VelocityResult(post_id=post_id)

    # Get post info
    post = db.get_post(post_id)
    if not post:
        return result

    # Calculate post age
    published_at = parse_iso_datetime(post["published_at"])
    age_delta = now - published_at
    result.post_age_hours = age_delta.total_seconds() / 3600

    # Get snapshots
    snapshots = db.get_snapshots_for_post(post_id)
    result.snapshot_count = len(snapshots)

    # Need at least 2 snapshots for velocity
    if len(snapshots) < 2:
        return result

    # Most recent snapshot
    current_snap = snapshots[0]
    current_time = parse_iso_datetime(current_snap["ts"])

    # Determine which metric to use based on source
    if post["source"] == "reddit":
        current_metric = current_snap.get("score") or current_snap.get("num_comments")
        metric_key = "score" if current_snap.get("score") is not None else "num_comments"
    else:  # youtube
        current_metric = current_snap.get("view_count")
        metric_key = "view_count"

    # Calculate 6h velocity
    # Window: [t-8h, t-4h], prefer closest to t-6h
    snap_6h = find_comparison_snapshot(
        snapshots, current_time,
        target_hours=6, window_start_hours=8, window_end_hours=4
    )
    if snap_6h:
        snap_6h_time = parse_iso_datetime(snap_6h["ts"])
        hours_elapsed = (current_time - snap_6h_time).total_seconds() / 3600
        comparison_value = snap_6h.get(metric_key)
        result.velocity_6h = calculate_velocity(current_metric, comparison_value, hours_elapsed)

    # Calculate 24h velocity
    # Window: [t-28h, t-20h], prefer closest to t-24h
    snap_24h = find_comparison_snapshot(
        snapshots, current_time,
        target_hours=24, window_start_hours=28, window_end_hours=20
    )
    if snap_24h:
        snap_24h_time = parse_iso_datetime(snap_24h["ts"])
        hours_elapsed = (current_time - snap_24h_time).total_seconds() / 3600
        comparison_value = snap_24h.get(metric_key)
        result.velocity_24h = calculate_velocity(current_metric, comparison_value, hours_elapsed)

    return result


def compute_all_velocities(hours: int = 72) -> list[VelocityResult]:
    """
    Compute velocities for all recent posts.

    Args:
        hours: Only process posts from the last N hours

    Returns:
        List of VelocityResult for each post
    """
    now = datetime.now(timezone.utc)
    results = []

    posts = db.get_recent_posts(hours=hours)
    for post in posts:
        result = compute_post_velocity(post["post_id"], now)
        results.append(result)

    return results


def store_velocities(results: list[VelocityResult]) -> int:
    """
    Store computed velocities to derived_post_metrics.

    Returns count of records stored.
    """
    ts = datetime.now(timezone.utc).isoformat()
    stored = 0

    for result in results:
        db.upsert_derived_metrics(
            post_id=result.post_id,
            ts=ts,
            velocity_6h=result.velocity_6h,
            velocity_24h=result.velocity_24h,
            snapshot_count=result.snapshot_count,
            post_age_hours=result.post_age_hours
        )
        stored += 1

    return stored


def run_velocity_computation(hours: int = 72) -> dict:
    """
    Run velocity computation for all recent posts and store results.

    Returns summary statistics.
    """
    results = compute_all_velocities(hours=hours)
    stored = store_velocities(results)

    # Count how many have valid velocities
    with_6h = sum(1 for r in results if r.velocity_6h is not None)
    with_24h = sum(1 for r in results if r.velocity_24h is not None)

    return {
        "posts_processed": len(results),
        "velocities_stored": stored,
        "with_6h_velocity": with_6h,
        "with_24h_velocity": with_24h,
    }
