"""
Database layer for Attention Flow Desk.
SQLite schema creation and connection management.
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Generator, Optional
from uuid import uuid4

from .config import get_config

# Schema version for migrations
SCHEMA_VERSION = 1

SCHEMA_SQL = """
-- Run tracking
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    sources_ok_json TEXT,
    counts_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_started_at ON runs(started_at);

-- Actors (YouTube channels and Reddit subreddits)
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    label TEXT,
    meta_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_actors_source ON actors(source);

-- Posts (individual content pieces)
CREATE TABLE IF NOT EXISTS posts (
    post_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    published_at TEXT NOT NULL,
    meta_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_posts_actor ON posts(actor_id);
CREATE INDEX IF NOT EXISTS idx_posts_published ON posts(published_at);
CREATE INDEX IF NOT EXISTS idx_posts_source ON posts(source);

-- Snapshots (append-only engagement metrics over time)
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id TEXT NOT NULL REFERENCES posts(post_id),
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    ts TEXT NOT NULL,
    view_count INTEGER,
    like_count INTEGER,
    comment_count INTEGER,
    score INTEGER,
    num_comments INTEGER,
    other_json TEXT,
    UNIQUE(post_id, ts)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_post ON snapshots(post_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON snapshots(ts);
CREATE INDEX IF NOT EXISTS idx_snapshots_run ON snapshots(run_id);

-- Derived post metrics (wide table for computed metrics)
CREATE TABLE IF NOT EXISTS derived_post_metrics (
    post_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    velocity_6h REAL,
    velocity_24h REAL,
    z_views_6h REAL,
    z_comments_6h REAL,
    z_views_24h REAL,
    snapshot_count INTEGER,
    post_age_hours REAL,
    flow_score REAL,
    PRIMARY KEY (post_id, ts)
);

CREATE INDEX IF NOT EXISTS idx_derived_post ON derived_post_metrics(post_id);
CREATE INDEX IF NOT EXISTS idx_derived_ts ON derived_post_metrics(ts);
CREATE INDEX IF NOT EXISTS idx_derived_flow_score ON derived_post_metrics(flow_score);

-- Actor baselines (MAD-based statistical baselines)
CREATE TABLE IF NOT EXISTS actor_baselines (
    actor_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    age_bucket TEXT NOT NULL,
    median REAL,
    mad REAL,
    sample_count INTEGER,
    computed_at TEXT NOT NULL,
    PRIMARY KEY (actor_id, metric, age_bucket)
);

-- Clusters (grouped related posts)
CREATE TABLE IF NOT EXISTS clusters (
    cluster_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    source TEXT,
    cluster_type TEXT,
    member_count INTEGER,
    unique_actor_count INTEGER,
    members_json TEXT,
    summary TEXT,
    strength REAL
);

CREATE INDEX IF NOT EXISTS idx_clusters_ts ON clusters(ts);

-- Note history (for novelty tracking)
CREATE TABLE IF NOT EXISTS note_history (
    note_id INTEGER PRIMARY KEY AUTOINCREMENT,
    generated_at TEXT NOT NULL,
    note_date TEXT NOT NULL,
    output_path TEXT,
    posts_included_json TEXT,
    clusters_included_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_note_history_date ON note_history(note_date);

-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_info (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def get_db_path() -> Path:
    """Get the database path from config."""
    return get_config().db_path


def ensure_db_dir() -> None:
    """Ensure the database directory exists."""
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)


def init_db(db_path: Optional[Path] = None) -> None:
    """Initialize the database schema."""
    if db_path is None:
        db_path = get_db_path()

    db_path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
        # Set busy timeout for concurrent access
        conn.execute("PRAGMA busy_timeout = 5000")
        # Store schema version
        conn.execute(
            "INSERT OR REPLACE INTO schema_info (key, value) VALUES (?, ?)",
            ("version", str(SCHEMA_VERSION))
        )
        conn.commit()


@contextmanager
def get_connection(db_path: Optional[Path] = None) -> Generator[sqlite3.Connection, None, None]:
    """
    Get a database connection with proper settings.

    Usage:
        with get_connection() as conn:
            cursor = conn.execute("SELECT * FROM posts")
    """
    if db_path is None:
        db_path = get_db_path()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")

    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# =============================================================================
# Run Management
# =============================================================================

def create_run() -> str:
    """Create a new run record and return its ID."""
    run_id = str(uuid4())
    started_at = datetime.utcnow().isoformat()

    with get_connection() as conn:
        conn.execute(
            "INSERT INTO runs (run_id, started_at, status) VALUES (?, ?, ?)",
            (run_id, started_at, "running")
        )

    return run_id


def complete_run(
    run_id: str,
    status: str,
    sources_ok: Optional[dict] = None,
    counts: Optional[dict] = None
) -> None:
    """Mark a run as complete with status and optional metadata."""
    ended_at = datetime.utcnow().isoformat()

    with get_connection() as conn:
        conn.execute(
            """UPDATE runs
               SET ended_at = ?, status = ?, sources_ok_json = ?, counts_json = ?
               WHERE run_id = ?""",
            (
                ended_at,
                status,
                json.dumps(sources_ok) if sources_ok else None,
                json.dumps(counts) if counts else None,
                run_id
            )
        )


def get_last_run() -> Optional[dict]:
    """Get the most recent run."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        if row:
            return dict(row)
    return None


# =============================================================================
# Actor Management
# =============================================================================

def upsert_actor(actor_id: str, source: str, label: str, meta: Optional[dict] = None) -> None:
    """Insert or update an actor."""
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO actors (actor_id, source, label, meta_json)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(actor_id) DO UPDATE SET
                   label = excluded.label,
                   meta_json = excluded.meta_json""",
            (actor_id, source, label, json.dumps(meta) if meta else None)
        )


def get_actor(actor_id: str) -> Optional[dict]:
    """Get an actor by ID."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM actors WHERE actor_id = ?", (actor_id,)
        ).fetchone()
        if row:
            return dict(row)
    return None


# =============================================================================
# Post Management
# =============================================================================

def upsert_post(
    post_id: str,
    source: str,
    actor_id: str,
    url: str,
    title: str,
    published_at: str,
    meta: Optional[dict] = None
) -> None:
    """Insert or update a post."""
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO posts (post_id, source, actor_id, url, title, published_at, meta_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(post_id) DO UPDATE SET
                   title = excluded.title,
                   meta_json = excluded.meta_json""",
            (post_id, source, actor_id, url, title, published_at,
             json.dumps(meta) if meta else None)
        )


def get_post(post_id: str) -> Optional[dict]:
    """Get a post by ID."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM posts WHERE post_id = ?", (post_id,)
        ).fetchone()
        if row:
            return dict(row)
    return None


def get_recent_posts(hours: int = 72) -> list[dict]:
    """Get posts from the last N hours."""
    cutoff = datetime.utcnow().isoformat()
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT p.*, a.label as actor_label
               FROM posts p
               JOIN actors a ON p.actor_id = a.actor_id
               WHERE datetime(p.published_at) >= datetime(?, '-' || ? || ' hours')
               ORDER BY p.published_at DESC""",
            (cutoff, hours)
        ).fetchall()
        return [dict(row) for row in rows]


# =============================================================================
# Snapshot Management
# =============================================================================

def insert_snapshot(
    post_id: str,
    run_id: str,
    ts: str,
    view_count: Optional[int] = None,
    like_count: Optional[int] = None,
    comment_count: Optional[int] = None,
    score: Optional[int] = None,
    num_comments: Optional[int] = None,
    other: Optional[dict] = None
) -> bool:
    """
    Insert a snapshot. Returns True if inserted, False if duplicate.
    """
    with get_connection() as conn:
        try:
            conn.execute(
                """INSERT INTO snapshots
                   (post_id, run_id, ts, view_count, like_count, comment_count,
                    score, num_comments, other_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (post_id, run_id, ts, view_count, like_count, comment_count,
                 score, num_comments, json.dumps(other) if other else None)
            )
            return True
        except sqlite3.IntegrityError:
            return False


def get_snapshots_for_post(post_id: str) -> list[dict]:
    """Get all snapshots for a post, ordered by timestamp."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM snapshots
               WHERE post_id = ?
               ORDER BY ts DESC""",
            (post_id,)
        ).fetchall()
        return [dict(row) for row in rows]


def get_snapshot_count(post_id: str) -> int:
    """Get the number of snapshots for a post."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM snapshots WHERE post_id = ?",
            (post_id,)
        ).fetchone()
        return row["cnt"] if row else 0


# =============================================================================
# Derived Metrics
# =============================================================================

def upsert_derived_metrics(
    post_id: str,
    ts: str,
    velocity_6h: Optional[float] = None,
    velocity_24h: Optional[float] = None,
    z_views_6h: Optional[float] = None,
    z_comments_6h: Optional[float] = None,
    z_views_24h: Optional[float] = None,
    snapshot_count: Optional[int] = None,
    post_age_hours: Optional[float] = None,
    flow_score: Optional[float] = None
) -> None:
    """Insert or update derived metrics for a post."""
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO derived_post_metrics
               (post_id, ts, velocity_6h, velocity_24h, z_views_6h, z_comments_6h,
                z_views_24h, snapshot_count, post_age_hours, flow_score)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(post_id, ts) DO UPDATE SET
                   velocity_6h = excluded.velocity_6h,
                   velocity_24h = excluded.velocity_24h,
                   z_views_6h = excluded.z_views_6h,
                   z_comments_6h = excluded.z_comments_6h,
                   z_views_24h = excluded.z_views_24h,
                   snapshot_count = excluded.snapshot_count,
                   post_age_hours = excluded.post_age_hours,
                   flow_score = excluded.flow_score""",
            (post_id, ts, velocity_6h, velocity_24h, z_views_6h, z_comments_6h,
             z_views_24h, snapshot_count, post_age_hours, flow_score)
        )


def get_latest_derived_metrics(post_id: str) -> Optional[dict]:
    """Get the most recent derived metrics for a post."""
    with get_connection() as conn:
        row = conn.execute(
            """SELECT * FROM derived_post_metrics
               WHERE post_id = ?
               ORDER BY ts DESC
               LIMIT 1""",
            (post_id,)
        ).fetchone()
        if row:
            return dict(row)
    return None


def get_top_movers(hours: int = 24, limit: int = 20) -> list[dict]:
    """Get top posts by flow score from the last N hours."""
    cutoff = datetime.utcnow().isoformat()
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT d.*, p.title, p.url, p.source, p.published_at, a.label as actor_label
               FROM derived_post_metrics d
               JOIN posts p ON d.post_id = p.post_id
               JOIN actors a ON p.actor_id = a.actor_id
               WHERE datetime(d.ts) >= datetime(?, '-' || ? || ' hours')
               AND d.flow_score IS NOT NULL
               ORDER BY d.flow_score DESC
               LIMIT ?""",
            (cutoff, hours, limit)
        ).fetchall()
        return [dict(row) for row in rows]


# =============================================================================
# Baselines
# =============================================================================

def upsert_baseline(
    actor_id: str,
    metric: str,
    age_bucket: str,
    median: float,
    mad: float,
    sample_count: int
) -> None:
    """Insert or update a baseline."""
    computed_at = datetime.utcnow().isoformat()
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO actor_baselines
               (actor_id, metric, age_bucket, median, mad, sample_count, computed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(actor_id, metric, age_bucket) DO UPDATE SET
                   median = excluded.median,
                   mad = excluded.mad,
                   sample_count = excluded.sample_count,
                   computed_at = excluded.computed_at""",
            (actor_id, metric, age_bucket, median, mad, sample_count, computed_at)
        )


def get_baseline(actor_id: str, metric: str, age_bucket: str) -> Optional[dict]:
    """Get a baseline for an actor/metric/age_bucket combination."""
    with get_connection() as conn:
        row = conn.execute(
            """SELECT * FROM actor_baselines
               WHERE actor_id = ? AND metric = ? AND age_bucket = ?""",
            (actor_id, metric, age_bucket)
        ).fetchone()
        if row:
            return dict(row)
    return None


def get_global_baseline(metric: str, age_bucket: str) -> Optional[dict]:
    """Get the global baseline (average across all actors)."""
    with get_connection() as conn:
        row = conn.execute(
            """SELECT AVG(median) as median, AVG(mad) as mad, SUM(sample_count) as sample_count
               FROM actor_baselines
               WHERE metric = ? AND age_bucket = ?""",
            (metric, age_bucket)
        ).fetchone()
        if row and row["median"] is not None:
            return dict(row)
    return None


# =============================================================================
# Clusters
# =============================================================================

def insert_cluster(
    ts: str,
    source: Optional[str],
    cluster_type: Optional[str],
    member_count: int,
    unique_actor_count: int,
    members: list[str],
    summary: Optional[str] = None,
    strength: Optional[float] = None
) -> int:
    """Insert a cluster and return its ID."""
    with get_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO clusters
               (ts, source, cluster_type, member_count, unique_actor_count,
                members_json, summary, strength)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (ts, source, cluster_type, member_count, unique_actor_count,
             json.dumps(members), summary, strength)
        )
        return cursor.lastrowid


def get_recent_clusters(hours: int = 48) -> list[dict]:
    """Get clusters from the last N hours."""
    cutoff = datetime.utcnow().isoformat()
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM clusters
               WHERE datetime(ts) >= datetime(?, '-' || ? || ' hours')
               ORDER BY strength DESC""",
            (cutoff, hours)
        ).fetchall()
        return [dict(row) for row in rows]


# =============================================================================
# Note History
# =============================================================================

def record_note(
    note_date: str,
    output_path: str,
    posts_included: list[str],
    clusters_included: list[int]
) -> int:
    """Record a generated note and return its ID."""
    generated_at = datetime.utcnow().isoformat()
    with get_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO note_history
               (generated_at, note_date, output_path, posts_included_json, clusters_included_json)
               VALUES (?, ?, ?, ?, ?)""",
            (generated_at, note_date, output_path,
             json.dumps(posts_included), json.dumps(clusters_included))
        )
        return cursor.lastrowid


def get_last_note() -> Optional[dict]:
    """Get the most recent note history entry."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM note_history ORDER BY generated_at DESC LIMIT 1"
        ).fetchone()
        if row:
            result = dict(row)
            if result.get("posts_included_json"):
                result["posts_included"] = json.loads(result["posts_included_json"])
            else:
                result["posts_included"] = []
            if result.get("clusters_included_json"):
                result["clusters_included"] = json.loads(result["clusters_included_json"])
            else:
                result["clusters_included"] = []
            return result
    return None


# =============================================================================
# Utility Functions
# =============================================================================

def get_db_stats() -> dict[str, Any]:
    """Get database statistics for doctor command."""
    with get_connection() as conn:
        stats = {}

        # Count tables
        for table in ["runs", "actors", "posts", "snapshots",
                      "derived_post_metrics", "actor_baselines", "clusters", "note_history"]:
            row = conn.execute(f"SELECT COUNT(*) as cnt FROM {table}").fetchone()
            stats[f"{table}_count"] = row["cnt"] if row else 0

        # Get recent activity
        row = conn.execute(
            "SELECT MAX(started_at) as last_run FROM runs"
        ).fetchone()
        stats["last_run_at"] = row["last_run"] if row else None

        row = conn.execute(
            "SELECT MAX(ts) as last_snapshot FROM snapshots"
        ).fetchone()
        stats["last_snapshot_at"] = row["last_snapshot"] if row else None

        # Snapshots in last 24h
        cutoff = datetime.utcnow().isoformat()
        row = conn.execute(
            """SELECT COUNT(*) as cnt FROM snapshots
               WHERE datetime(ts) >= datetime(?, '-24 hours')""",
            (cutoff,)
        ).fetchone()
        stats["snapshots_24h"] = row["cnt"] if row else 0

        return stats
