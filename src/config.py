"""
Configuration loader for Attention Flow Desk.
Loads environment variables from .env and watchlist from watchlist.yaml.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv


@dataclass
class YouTubeActor:
    """YouTube channel configuration."""
    channel_id: str
    label: str


@dataclass
class RedditActor:
    """Reddit subreddit configuration."""
    subreddit: str
    label: str


@dataclass
class Watchlist:
    """Parsed watchlist configuration."""
    youtube: list[YouTubeActor] = field(default_factory=list)
    reddit: list[RedditActor] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    formats: list[str] = field(default_factory=list)


@dataclass
class Config:
    """Application configuration."""
    # YouTube API
    youtube_api_key: Optional[str]

    # Reddit API
    reddit_client_id: Optional[str]
    reddit_client_secret: Optional[str]
    reddit_user_agent: str

    # Paths
    db_path: Path
    notes_dir: Path

    # Timezone
    timezone: str

    # Flow note settings
    repeat_score_threshold: float

    # Watchlist
    watchlist: Watchlist

    # Base directory (project root)
    base_dir: Path


def find_project_root() -> Path:
    """Find the project root by looking for watchlist.yaml."""
    current = Path(__file__).resolve().parent.parent
    if (current / "watchlist.yaml").exists():
        return current
    # Fallback to current working directory
    cwd = Path.cwd()
    if (cwd / "watchlist.yaml").exists():
        return cwd
    return current


def load_watchlist(path: Path) -> Watchlist:
    """Load and parse watchlist.yaml."""
    if not path.exists():
        return Watchlist()

    with open(path, "r") as f:
        data = yaml.safe_load(f) or {}

    watchlist = Watchlist()

    # Parse actors
    actors = data.get("actors", {})

    for yt in actors.get("youtube", []):
        if isinstance(yt, dict) and "channel_id" in yt:
            watchlist.youtube.append(YouTubeActor(
                channel_id=yt["channel_id"],
                label=yt.get("label", yt["channel_id"])
            ))

    for rd in actors.get("reddit", []):
        if isinstance(rd, dict) and "subreddit" in rd:
            watchlist.reddit.append(RedditActor(
                subreddit=rd["subreddit"],
                label=rd.get("label", rd["subreddit"])
            ))

    # Parse topics and formats
    watchlist.topics = data.get("topics", [])
    watchlist.formats = data.get("formats", [])

    return watchlist


def load_config(env_path: Optional[Path] = None) -> Config:
    """
    Load configuration from .env and watchlist.yaml.

    Args:
        env_path: Optional path to .env file. If not provided, will search
                  in project root.

    Returns:
        Config object with all settings loaded.
    """
    base_dir = find_project_root()

    # Load .env file
    if env_path is None:
        env_path = base_dir / ".env"

    if env_path.exists():
        load_dotenv(env_path)

    # Load watchlist
    watchlist_path = base_dir / "watchlist.yaml"
    watchlist = load_watchlist(watchlist_path)

    # Parse paths relative to base_dir
    db_path_str = os.getenv("DB_PATH", "data/desk.db")
    notes_dir_str = os.getenv("NOTES_DIR", "notes")

    # Make paths absolute if relative
    db_path = Path(db_path_str)
    if not db_path.is_absolute():
        db_path = base_dir / db_path

    notes_dir = Path(notes_dir_str)
    if not notes_dir.is_absolute():
        notes_dir = base_dir / notes_dir

    return Config(
        youtube_api_key=os.getenv("YOUTUBE_API_KEY") or None,
        reddit_client_id=os.getenv("REDDIT_CLIENT_ID") or None,
        reddit_client_secret=os.getenv("REDDIT_CLIENT_SECRET") or None,
        reddit_user_agent=os.getenv("REDDIT_USER_AGENT", "attention-desk/0.1"),
        db_path=db_path,
        notes_dir=notes_dir,
        timezone=os.getenv("TIMEZONE", "America/Los_Angeles"),
        repeat_score_threshold=float(os.getenv("REPEAT_SCORE_THRESHOLD", "0.5")),
        watchlist=watchlist,
        base_dir=base_dir,
    )


# Singleton instance
_config: Optional[Config] = None


def get_config() -> Config:
    """Get the global config instance, loading if necessary."""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def reset_config() -> None:
    """Reset the global config instance (useful for testing)."""
    global _config
    _config = None
