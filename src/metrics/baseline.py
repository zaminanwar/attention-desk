"""
Baseline computation for Attention Flow Desk.
Computes MAD-based statistical baselines for z-score calculation.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import median
from typing import Optional

from .. import db


# Age buckets for baseline computation
AGE_BUCKETS = [
    ("0-6h", 0, 6),
    ("6-24h", 6, 24),
    ("24-72h", 24, 72),
]

# Minimum samples required for a valid baseline
MIN_SAMPLES = 7

# Maximum posts to consider for baseline
MAX_BASELINE_POSTS = 30


@dataclass
class BaselineResult:
    """Result of baseline computation."""
    actor_id: str
    metric: str
    age_bucket: str
    median: Optional[float]
    mad: Optional[float]
    sample_count: int
    is_valid: bool


def compute_mad(values: list[float]) -> float:
    """
    Compute Median Absolute Deviation.
    MAD = median(|Xi - median(X)|)
    """
    if not values:
        return 0.0

    med = median(values)
    deviations = [abs(v - med) for v in values]
    return median(deviations)


def get_age_bucket(age_hours: float) -> Optional[str]:
    """Determine which age bucket a post falls into."""
    for bucket_name, min_age, max_age in AGE_BUCKETS:
        if min_age <= age_hours < max_age:
            return bucket_name
    return None


def compute_actor_baseline(
    actor_id: str,
    metric: str,
    age_bucket: str,
    max_posts: int = MAX_BASELINE_POSTS
) -> BaselineResult:
    """
    Compute baseline for a specific actor/metric/age_bucket combination.

    Args:
        actor_id: The actor ID
        metric: The metric to compute baseline for (e.g., "velocity_6h")
        age_bucket: Age bucket (e.g., "0-6h")
        max_posts: Maximum posts to consider

    Returns:
        BaselineResult with computed statistics
    """
    result = BaselineResult(
        actor_id=actor_id,
        metric=metric,
        age_bucket=age_bucket,
        median=None,
        mad=None,
        sample_count=0,
        is_valid=False
    )

    # Get age bucket bounds
    bucket_info = None
    for name, min_age, max_age in AGE_BUCKETS:
        if name == age_bucket:
            bucket_info = (min_age, max_age)
            break

    if bucket_info is None:
        return result

    min_age, max_age = bucket_info

    # Query derived metrics for this actor
    with db.get_connection() as conn:
        rows = conn.execute(
            """SELECT d.{metric}, d.post_age_hours
               FROM derived_post_metrics d
               JOIN posts p ON d.post_id = p.post_id
               WHERE p.actor_id = ?
               AND d.{metric} IS NOT NULL
               AND d.post_age_hours >= ?
               AND d.post_age_hours < ?
               ORDER BY d.ts DESC
               LIMIT ?""".format(metric=metric),
            (actor_id, min_age, max_age, max_posts)
        ).fetchall()

    values = [row[0] for row in rows if row[0] is not None]
    result.sample_count = len(values)

    if len(values) >= MIN_SAMPLES:
        result.median = median(values)
        result.mad = compute_mad(values)
        # Valid if MAD > 0 (otherwise z-scores would be undefined)
        result.is_valid = result.mad > 0

    return result


def compute_global_baseline(
    metric: str,
    age_bucket: str,
    max_posts: int = MAX_BASELINE_POSTS * 10
) -> BaselineResult:
    """
    Compute global baseline across all actors.
    Used as fallback for cold-start actors.
    """
    result = BaselineResult(
        actor_id="__global__",
        metric=metric,
        age_bucket=age_bucket,
        median=None,
        mad=None,
        sample_count=0,
        is_valid=False
    )

    # Get age bucket bounds
    bucket_info = None
    for name, min_age, max_age in AGE_BUCKETS:
        if name == age_bucket:
            bucket_info = (min_age, max_age)
            break

    if bucket_info is None:
        return result

    min_age, max_age = bucket_info

    # Query derived metrics across all actors
    with db.get_connection() as conn:
        rows = conn.execute(
            """SELECT d.{metric}
               FROM derived_post_metrics d
               WHERE d.{metric} IS NOT NULL
               AND d.post_age_hours >= ?
               AND d.post_age_hours < ?
               ORDER BY d.ts DESC
               LIMIT ?""".format(metric=metric),
            (min_age, max_age, max_posts)
        ).fetchall()

    values = [row[0] for row in rows if row[0] is not None]
    result.sample_count = len(values)

    if len(values) >= MIN_SAMPLES:
        result.median = median(values)
        result.mad = compute_mad(values)
        result.is_valid = result.mad > 0

    return result


def get_baseline_for_post(
    actor_id: str,
    metric: str,
    age_hours: float
) -> Optional[BaselineResult]:
    """
    Get the appropriate baseline for scoring a post.

    Falls back to global baseline if actor baseline unavailable.
    """
    age_bucket = get_age_bucket(age_hours)
    if age_bucket is None:
        return None

    # Try actor baseline first
    baseline = compute_actor_baseline(actor_id, metric, age_bucket)
    if baseline.is_valid:
        return baseline

    # Fall back to global baseline
    global_baseline = compute_global_baseline(metric, age_bucket)
    if global_baseline.is_valid:
        return global_baseline

    return None


def compute_all_baselines() -> dict:
    """
    Compute and store baselines for all actors.

    Returns summary statistics.
    """
    metrics = ["velocity_6h", "velocity_24h"]

    with db.get_connection() as conn:
        actors = conn.execute("SELECT actor_id FROM actors").fetchall()

    stored = 0
    valid = 0

    for actor_row in actors:
        actor_id = actor_row["actor_id"]

        for metric in metrics:
            for age_bucket, _, _ in AGE_BUCKETS:
                result = compute_actor_baseline(actor_id, metric, age_bucket)

                if result.median is not None:
                    db.upsert_baseline(
                        actor_id=actor_id,
                        metric=metric,
                        age_bucket=age_bucket,
                        median=result.median,
                        mad=result.mad or 0,
                        sample_count=result.sample_count
                    )
                    stored += 1
                    if result.is_valid:
                        valid += 1

    return {
        "actors_processed": len(actors),
        "baselines_stored": stored,
        "valid_baselines": valid,
    }
