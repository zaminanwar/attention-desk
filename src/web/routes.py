"""
API routes for Attention Flow Desk.
"""

import json
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from .. import db

api = Blueprint("api", __name__, url_prefix="/api")


@api.route("/stats")
def get_stats():
    """Get aggregate statistics for stats bars."""
    db.init_db()
    stats = db.get_db_stats()

    # Get top movers count
    movers = db.get_top_movers(hours=24, limit=50)

    # Get active clusters
    clusters = db.get_recent_clusters(hours=48)

    return jsonify({
        "top_movers_count": len(movers),
        "new_movers": len([m for m in movers if m.get("post_age_hours", 0) < 6]),
        "active_clusters": len(clusters),
        "actors_tracked": stats["actors_count"],
        "posts_ingested": stats["posts_count"],
        "snapshots_24h": stats["snapshots_24h"],
        "avg_flow_score": round(
            sum(m.get("flow_score", 0) or 0 for m in movers) / len(movers), 1
        ) if movers else 0,
    })


@api.route("/movers")
def get_movers():
    """Get top posts by flow score."""
    db.init_db()
    hours = request.args.get("hours", 24, type=int)
    limit = request.args.get("limit", 20, type=int)

    movers = db.get_top_movers(hours=hours, limit=limit)

    return jsonify([
        {
            "post_id": m["post_id"],
            "title": m.get("title", "Untitled"),
            "url": m.get("url", ""),
            "source": m.get("source", "unknown"),
            "actor_label": m.get("actor_label", "Unknown"),
            "flow_score": m.get("flow_score"),
            "velocity_6h": m.get("velocity_6h"),
            "velocity_24h": m.get("velocity_24h"),
            "z_views_6h": m.get("z_views_6h"),
            "z_comments_6h": m.get("z_comments_6h"),
            "post_age_hours": m.get("post_age_hours"),
            "snapshot_count": m.get("snapshot_count", 0),
            "published_at": m.get("published_at"),
        }
        for m in movers
    ])


@api.route("/clusters")
def get_clusters():
    """Get recent clusters."""
    db.init_db()
    hours = request.args.get("hours", 48, type=int)

    clusters = db.get_recent_clusters(hours=hours)

    result = []
    for c in clusters:
        members = []
        if c.get("members_json"):
            try:
                members = json.loads(c["members_json"])
            except json.JSONDecodeError:
                pass

        result.append({
            "cluster_id": c["cluster_id"],
            "summary": c.get("summary", "Unnamed Cluster"),
            "strength": c.get("strength"),
            "member_count": c.get("member_count", 0),
            "unique_actor_count": c.get("unique_actor_count", 0),
            "source": c.get("source"),
            "ts": c.get("ts"),
            "members": members,
        })

    return jsonify(result)


@api.route("/actors")
def get_actors():
    """Get all tracked actors with their stats."""
    db.init_db()

    with db.get_connection() as conn:
        # Get actors with post counts and latest activity
        rows = conn.execute("""
            SELECT
                a.actor_id,
                a.source,
                a.label,
                COUNT(p.post_id) as post_count,
                MAX(p.published_at) as last_post_at
            FROM actors a
            LEFT JOIN posts p ON a.actor_id = p.actor_id
            GROUP BY a.actor_id
            ORDER BY post_count DESC
        """).fetchall()

        actors = []
        for row in rows:
            actor_id = row["actor_id"]

            # Check if actor has baseline
            baseline = db.get_baseline(actor_id, "velocity_6h", "0-6h")
            has_baseline = baseline is not None and baseline.get("sample_count", 0) >= 7

            # Determine status
            status = None
            if row["last_post_at"]:
                try:
                    last_post = datetime.fromisoformat(row["last_post_at"].replace("Z", "+00:00"))
                    age_hours = (datetime.now(timezone.utc) - last_post).total_seconds() / 3600
                    if age_hours < 6:
                        status = "hot"
                    elif age_hours > 48:
                        status = "stale"
                except (ValueError, TypeError):
                    pass

            actors.append({
                "actor_id": actor_id,
                "source": row["source"],
                "label": row["label"],
                "post_count": row["post_count"],
                "has_baseline": has_baseline,
                "baseline_status": "ready" if has_baseline else "cold",
                "median_velocity": baseline.get("median") if baseline else None,
                "mad": baseline.get("mad") if baseline else None,
                "last_post_at": row["last_post_at"],
                "status": status,
            })

        return jsonify(actors)


@api.route("/status")
def get_status():
    """Get system status information."""
    db.init_db()
    stats = db.get_db_stats()
    last_run = db.get_last_run()

    # Calculate time since last ingest
    last_ingest_ago = None
    if last_run and last_run.get("started_at"):
        try:
            started = datetime.fromisoformat(last_run["started_at"].replace("Z", "+00:00"))
            delta = datetime.now(timezone.utc) - started
            last_ingest_ago = int(delta.total_seconds() / 60)  # minutes
        except (ValueError, TypeError):
            pass

    # Parse run counts if available
    run_counts = {}
    if last_run and last_run.get("counts_json"):
        try:
            run_counts = json.loads(last_run["counts_json"])
        except json.JSONDecodeError:
            pass

    return jsonify({
        "database": {
            "status": "healthy",
            "posts_count": stats["posts_count"],
            "snapshots_count": stats["snapshots_count"],
            "actors_count": stats["actors_count"],
            "runs_count": stats["runs_count"],
            "snapshots_24h": stats["snapshots_24h"],
        },
        "last_run": {
            "run_id": last_run.get("run_id") if last_run else None,
            "status": last_run.get("status") if last_run else None,
            "started_at": last_run.get("started_at") if last_run else None,
            "ended_at": last_run.get("ended_at") if last_run else None,
            "minutes_ago": last_ingest_ago,
            "counts": run_counts,
        },
        "last_snapshot_at": stats.get("last_snapshot_at"),
    })


@api.route("/post/<post_id>")
def get_post_detail(post_id: str):
    """Get detailed information for a single post."""
    db.init_db()

    post = db.get_post(post_id)
    if not post:
        return jsonify({"error": "Post not found"}), 404

    # Get actor info
    actor = db.get_actor(post["actor_id"])

    # Get snapshots for velocity chart
    snapshots = db.get_snapshots_for_post(post_id)

    # Get derived metrics
    metrics = db.get_latest_derived_metrics(post_id)

    return jsonify({
        "post_id": post_id,
        "title": post.get("title"),
        "url": post.get("url"),
        "source": post.get("source"),
        "published_at": post.get("published_at"),
        "actor": {
            "actor_id": post.get("actor_id"),
            "label": actor.get("label") if actor else None,
            "source": actor.get("source") if actor else None,
        },
        "metrics": {
            "flow_score": metrics.get("flow_score") if metrics else None,
            "velocity_6h": metrics.get("velocity_6h") if metrics else None,
            "velocity_24h": metrics.get("velocity_24h") if metrics else None,
            "z_views_6h": metrics.get("z_views_6h") if metrics else None,
            "z_comments_6h": metrics.get("z_comments_6h") if metrics else None,
            "post_age_hours": metrics.get("post_age_hours") if metrics else None,
            "snapshot_count": metrics.get("snapshot_count") if metrics else 0,
        },
        "snapshots": [
            {
                "ts": s.get("ts"),
                "view_count": s.get("view_count"),
                "like_count": s.get("like_count"),
                "comment_count": s.get("comment_count"),
                "score": s.get("score"),
                "num_comments": s.get("num_comments"),
            }
            for s in snapshots[:20]  # Limit to last 20 snapshots
        ],
    })


@api.route("/notes")
def get_notes():
    """Get list of generated notes."""
    db.init_db()

    with db.get_connection() as conn:
        rows = conn.execute("""
            SELECT * FROM note_history
            ORDER BY generated_at DESC
            LIMIT 50
        """).fetchall()

        notes = []
        for row in rows:
            posts_included = []
            clusters_included = []

            if row["posts_included_json"]:
                try:
                    posts_included = json.loads(row["posts_included_json"])
                except json.JSONDecodeError:
                    pass

            if row["clusters_included_json"]:
                try:
                    clusters_included = json.loads(row["clusters_included_json"])
                except json.JSONDecodeError:
                    pass

            notes.append({
                "note_id": row["note_id"],
                "note_date": row["note_date"],
                "generated_at": row["generated_at"],
                "output_path": row["output_path"],
                "movers_count": len(posts_included),
                "clusters_count": len(clusters_included),
            })

        return jsonify(notes)


@api.errorhandler(Exception)
def handle_error(e):
    """Handle exceptions."""
    return jsonify({"error": str(e)}), 500
