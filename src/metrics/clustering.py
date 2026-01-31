"""
Clustering computation for Attention Flow Desk.
Groups related high-performing posts into clusters.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .. import db
from ..config import get_config


# Cluster thresholds
MIN_Z_SCORE = 2.0
MIN_POSTS = 5
MIN_ACTORS = 3
MIN_SHARED_TOKENS = 3

# Common stop words to exclude from token matching
STOP_WORDS = {
    'this', 'that', 'with', 'from', 'have', 'will', 'what', 'just',
    'about', 'like', 'your', 'they', 'been', 'more', 'when', 'some',
    'there', 'were', 'would', 'into', 'which', 'than', 'then', 'them',
    'these', 'those', 'could', 'should', 'being', 'does', 'doing',
    'their', 'here', 'where', 'while', 'after', 'before', 'other',
}


@dataclass
class ClusterCandidate:
    """A candidate cluster of related posts."""
    source: str
    post_ids: list[str] = field(default_factory=list)
    actor_ids: set[str] = field(default_factory=set)
    titles: list[str] = field(default_factory=list)
    shared_tokens: set[str] = field(default_factory=set)
    topic_matches: list[str] = field(default_factory=list)
    avg_z_score: float = 0.0


@dataclass
class ClusterResult:
    """Result of cluster detection."""
    cluster_id: int
    source: str
    member_count: int
    unique_actor_count: int
    summary: str
    strength: float
    members: list[str]


def extract_tokens(title: str) -> set[str]:
    """
    Extract meaningful tokens from a title.
    Keeps only alphanumeric words of 4+ characters, excluding stop words.
    """
    words = re.findall(r'\b[a-z]{4,}\b', title.lower())
    return {w for w in words if w not in STOP_WORDS}


def token_overlap(titles: list[str]) -> set[str]:
    """
    Find tokens that appear in all titles.
    """
    if not titles:
        return set()

    token_sets = [extract_tokens(t) for t in titles]
    if not token_sets:
        return set()

    # Start with first set, intersect with all others
    shared = token_sets[0].copy()
    for ts in token_sets[1:]:
        shared &= ts

    return shared


def find_topic_matches(titles: list[str], topics: list[str]) -> list[str]:
    """
    Find watchlist topics that match any of the titles.
    """
    matches = []
    combined_text = " ".join(titles).lower()

    for topic in topics:
        if topic.lower() in combined_text:
            matches.append(topic)

    return matches


def get_cluster_eligible_posts(hours: int = 48) -> list[dict]:
    """
    Get posts eligible for clustering (z-score >= 2.0 in last N hours).
    """
    cutoff = datetime.now(timezone.utc).isoformat()

    with db.get_connection() as conn:
        rows = conn.execute(
            """SELECT p.post_id, p.source, p.actor_id, p.title,
                      d.z_views_6h, d.z_comments_6h, d.z_views_24h, d.flow_score
               FROM posts p
               JOIN derived_post_metrics d ON p.post_id = d.post_id
               WHERE datetime(d.ts) >= datetime(?, '-' || ? || ' hours')
               AND (
                   COALESCE(d.z_views_6h, 0) >= ? OR
                   COALESCE(d.z_comments_6h, 0) >= ? OR
                   COALESCE(d.z_views_24h, 0) >= ?
               )
               ORDER BY d.flow_score DESC""",
            (cutoff, hours, MIN_Z_SCORE, MIN_Z_SCORE, MIN_Z_SCORE)
        ).fetchall()

    return [dict(row) for row in rows]


def group_posts_by_source(posts: list[dict]) -> dict[str, list[dict]]:
    """Group posts by their source."""
    groups = {}
    for post in posts:
        source = post.get("source", "unknown")
        if source not in groups:
            groups[source] = []
        groups[source].append(post)
    return groups


def try_form_cluster(
    posts: list[dict],
    source: str,
    topics: list[str]
) -> Optional[ClusterCandidate]:
    """
    Try to form a cluster from a group of posts.

    Returns a ClusterCandidate if the group meets cluster criteria.
    """
    if len(posts) < MIN_POSTS:
        return None

    # Check unique actors
    actor_ids = {p["actor_id"] for p in posts}
    if len(actor_ids) < MIN_ACTORS:
        return None

    # Check token overlap
    titles = [p["title"] for p in posts]
    shared = token_overlap(titles)

    if len(shared) < MIN_SHARED_TOKENS:
        # Try with a subset that might have better overlap
        # Start with top-scoring posts
        for subset_size in range(len(posts), MIN_POSTS - 1, -1):
            subset = posts[:subset_size]
            subset_titles = [p["title"] for p in subset]
            subset_shared = token_overlap(subset_titles)

            subset_actors = {p["actor_id"] for p in subset}
            if len(subset_actors) >= MIN_ACTORS and len(subset_shared) >= MIN_SHARED_TOKENS:
                posts = subset
                titles = subset_titles
                shared = subset_shared
                actor_ids = subset_actors
                break
        else:
            # Couldn't find a valid subset with enough token overlap
            # Still allow cluster if topic matches
            topic_matches = find_topic_matches(titles, topics)
            if not topic_matches:
                return None

    # Calculate average z-score
    z_scores = []
    for p in posts:
        z = max(
            p.get("z_views_6h") or 0,
            p.get("z_comments_6h") or 0,
            p.get("z_views_24h") or 0
        )
        z_scores.append(z)
    avg_z = sum(z_scores) / len(z_scores) if z_scores else 0

    # Find topic matches
    topic_matches = find_topic_matches(titles, topics)

    return ClusterCandidate(
        source=source,
        post_ids=[p["post_id"] for p in posts],
        actor_ids=actor_ids,
        titles=titles,
        shared_tokens=shared,
        topic_matches=topic_matches,
        avg_z_score=avg_z
    )


def generate_cluster_summary(candidate: ClusterCandidate) -> str:
    """Generate a human-readable summary for a cluster."""
    parts = []

    if candidate.topic_matches:
        parts.append(f"Topic: {', '.join(candidate.topic_matches[:2])}")

    if candidate.shared_tokens:
        tokens = sorted(candidate.shared_tokens)[:5]
        parts.append(f"Keywords: {', '.join(tokens)}")

    if parts:
        return " | ".join(parts)

    return f"{candidate.source.title()} cluster ({len(candidate.post_ids)} posts)"


def detect_clusters(hours: int = 48) -> list[ClusterResult]:
    """
    Detect clusters in recent high-performing posts.

    Args:
        hours: Time window to consider

    Returns:
        List of detected clusters
    """
    config = get_config()
    topics = config.watchlist.topics

    # Get eligible posts
    posts = get_cluster_eligible_posts(hours=hours)
    if not posts:
        return []

    # Group by source
    groups = group_posts_by_source(posts)

    results = []
    ts = datetime.now(timezone.utc).isoformat()

    for source, source_posts in groups.items():
        candidate = try_form_cluster(source_posts, source, topics)

        if candidate:
            summary = generate_cluster_summary(candidate)

            # Store cluster in database
            cluster_id = db.insert_cluster(
                ts=ts,
                source=candidate.source,
                cluster_type="topic" if candidate.topic_matches else "token_overlap",
                member_count=len(candidate.post_ids),
                unique_actor_count=len(candidate.actor_ids),
                members=candidate.post_ids,
                summary=summary,
                strength=candidate.avg_z_score
            )

            results.append(ClusterResult(
                cluster_id=cluster_id,
                source=candidate.source,
                member_count=len(candidate.post_ids),
                unique_actor_count=len(candidate.actor_ids),
                summary=summary,
                strength=candidate.avg_z_score,
                members=candidate.post_ids
            ))

    return results


def run_clustering(hours: int = 48) -> dict:
    """
    Run cluster detection for recent posts.

    Returns summary statistics.
    """
    clusters = detect_clusters(hours=hours)

    return {
        "clusters_detected": len(clusters),
        "clusters": [
            {
                "id": c.cluster_id,
                "source": c.source,
                "members": c.member_count,
                "actors": c.unique_actor_count,
                "summary": c.summary,
                "strength": c.strength,
            }
            for c in clusters
        ]
    }
