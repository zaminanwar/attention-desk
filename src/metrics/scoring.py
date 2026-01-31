"""
Scoring computation for Attention Flow Desk.
Computes z-scores and flow scores for posts.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from .. import db
from .baseline import get_baseline_for_post
from .velocity import parse_iso_datetime


# Z-score bounds
Z_SCORE_MIN = -10.0
Z_SCORE_MAX = 10.0

# Flow score weights
WEIGHT_Z_COMMENTS_6H = 0.5
WEIGHT_Z_VIEWS_6H = 0.3
WEIGHT_Z_VIEWS_24H = 0.2

# Cluster bonus (added when post is part of a cluster)
CLUSTER_BONUS = 0.5


@dataclass
class ScoreResult:
    """Result of scoring a single post."""
    post_id: str
    z_views_6h: Optional[float] = None
    z_comments_6h: Optional[float] = None
    z_views_24h: Optional[float] = None
    flow_score: Optional[float] = None
    in_cluster: bool = False


def compute_z_score(
    value: Optional[float],
    median: float,
    mad: float
) -> Optional[float]:
    """
    Compute z-score using MAD-based formula.

    z = (value - median) / (1.4826 * MAD)

    The constant 1.4826 makes MAD comparable to standard deviation
    for normally distributed data.
    """
    if value is None or mad <= 0:
        return None

    z = (value - median) / (1.4826 * mad)

    # Clamp to bounds
    return max(Z_SCORE_MIN, min(Z_SCORE_MAX, z))


def compute_flow_score(
    z_comments_6h: Optional[float],
    z_views_6h: Optional[float],
    z_views_24h: Optional[float],
    in_cluster: bool = False
) -> Optional[float]:
    """
    Compute flow score from z-scores.

    flow_score = 0.5 * z_comments_6h + 0.3 * z_views_6h + 0.2 * z_views_24h + cluster_bonus

    Missing components contribute 0.
    """
    score = 0.0
    has_any = False

    if z_comments_6h is not None:
        score += WEIGHT_Z_COMMENTS_6H * z_comments_6h
        has_any = True

    if z_views_6h is not None:
        score += WEIGHT_Z_VIEWS_6H * z_views_6h
        has_any = True

    if z_views_24h is not None:
        score += WEIGHT_Z_VIEWS_24H * z_views_24h
        has_any = True

    if not has_any:
        return None

    if in_cluster:
        score += CLUSTER_BONUS

    return score


def score_post(post_id: str, now: Optional[datetime] = None) -> ScoreResult:
    """
    Compute scores for a single post.

    Args:
        post_id: The post ID to score
        now: Current timestamp (defaults to UTC now)

    Returns:
        ScoreResult with computed z-scores and flow score
    """
    if now is None:
        now = datetime.now(timezone.utc)

    result = ScoreResult(post_id=post_id)

    # Get post info
    post = db.get_post(post_id)
    if not post:
        return result

    # Get current derived metrics
    metrics = db.get_latest_derived_metrics(post_id)
    if not metrics:
        return result

    actor_id = post["actor_id"]
    age_hours = metrics.get("post_age_hours")
    if age_hours is None:
        return result

    # Compute z-scores for each metric
    # For Reddit, we use score/num_comments velocity
    # For YouTube, we use view_count velocity
    velocity_6h = metrics.get("velocity_6h")
    velocity_24h = metrics.get("velocity_24h")

    # Get baseline and compute z-score for 6h velocity
    baseline_6h = get_baseline_for_post(actor_id, "velocity_6h", age_hours)
    if baseline_6h and baseline_6h.is_valid and velocity_6h is not None:
        z = compute_z_score(velocity_6h, baseline_6h.median, baseline_6h.mad)
        # For simplicity, we use the same z for views and comments
        # In a more sophisticated version, these would be separate
        if post["source"] == "youtube":
            result.z_views_6h = z
        else:
            result.z_comments_6h = z

    # Get baseline and compute z-score for 24h velocity
    baseline_24h = get_baseline_for_post(actor_id, "velocity_24h", age_hours)
    if baseline_24h and baseline_24h.is_valid and velocity_24h is not None:
        z = compute_z_score(velocity_24h, baseline_24h.median, baseline_24h.mad)
        result.z_views_24h = z

    # Check if post is in a cluster
    # (This is simplified - full implementation would query clusters)
    result.in_cluster = False

    # Compute flow score
    result.flow_score = compute_flow_score(
        result.z_comments_6h,
        result.z_views_6h,
        result.z_views_24h,
        result.in_cluster
    )

    return result


def score_all_posts(hours: int = 72) -> list[ScoreResult]:
    """
    Score all recent posts.

    Args:
        hours: Only process posts from the last N hours

    Returns:
        List of ScoreResult for each post
    """
    now = datetime.now(timezone.utc)
    results = []

    posts = db.get_recent_posts(hours=hours)
    for post in posts:
        result = score_post(post["post_id"], now)
        results.append(result)

    return results


def store_scores(results: list[ScoreResult]) -> int:
    """
    Store computed scores to derived_post_metrics.

    Returns count of records updated.
    """
    ts = datetime.now(timezone.utc).isoformat()
    updated = 0

    for result in results:
        # Only update if we have any scores
        if (result.z_views_6h is not None or
            result.z_comments_6h is not None or
            result.z_views_24h is not None or
            result.flow_score is not None):

            # Get existing metrics to preserve velocity
            existing = db.get_latest_derived_metrics(result.post_id)

            db.upsert_derived_metrics(
                post_id=result.post_id,
                ts=ts,
                velocity_6h=existing.get("velocity_6h") if existing else None,
                velocity_24h=existing.get("velocity_24h") if existing else None,
                z_views_6h=result.z_views_6h,
                z_comments_6h=result.z_comments_6h,
                z_views_24h=result.z_views_24h,
                snapshot_count=existing.get("snapshot_count") if existing else None,
                post_age_hours=existing.get("post_age_hours") if existing else None,
                flow_score=result.flow_score
            )
            updated += 1

    return updated


def run_scoring(hours: int = 72) -> dict:
    """
    Run scoring computation for all recent posts.

    Returns summary statistics.
    """
    from .baseline import compute_all_baselines

    # First compute/update baselines
    baseline_result = compute_all_baselines()

    # Then score all posts
    results = score_all_posts(hours=hours)
    updated = store_scores(results)

    # Count how many have valid scores
    with_flow_score = sum(1 for r in results if r.flow_score is not None)

    return {
        "posts_scored": len(results),
        "scores_stored": updated,
        "with_flow_score": with_flow_score,
        "baselines": baseline_result,
    }
