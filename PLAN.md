# Attention Flow Desk - Implementation Plan

## Overview

Build a Python CLI system that tracks engagement metrics from YouTube and Reddit, computes velocity/anomaly metrics using MAD-based baselines, and produces daily Markdown Flow Notes.

**Key output:** `python -m src.run all` produces a Daily Close Note.

---

## Phase 1: Foundation (Core Setup)

### 1.1 Project Structure
```
attention-desk/
├── PROGRESS.md           # Implementation progress tracker (update each session)
├── watchlist.yaml
├── .env.example
├── README.md
├── requirements.txt
├── src/
│   ├── __init__.py
│   ├── run.py              # CLI entrypoint
│   ├── config.py           # Load .env + watchlist.yaml
│   ├── db.py               # SQLite connection + schema
│   ├── ingest/
│   │   ├── __init__.py
│   │   ├── youtube.py
│   │   └── reddit.py
│   ├── metrics/
│   │   ├── __init__.py
│   │   ├── velocity.py
│   │   ├── baseline.py
│   │   ├── scoring.py
│   │   └── clustering.py
│   └── publish/
│       ├── __init__.py
│       └── flownote.py
├── data/                   # SQLite DB location
└── notes/                  # Generated Flow Notes
```

### 1.2 Dependencies (requirements.txt)
```
google-api-python-client>=2.100
praw>=7.7
python-dotenv>=1.0
pyyaml>=6.0
rich>=13.0
jinja2>=3.1
pytest>=7.4
```

### 1.3 Configuration Files

**.env.example:**
```
YOUTUBE_API_KEY=
REDDIT_CLIENT_ID=
REDDIT_CLIENT_SECRET=
REDDIT_USER_AGENT=attention-desk/0.1
DB_PATH=data/desk.db
NOTES_DIR=notes
TIMEZONE=America/Los_Angeles
```

**watchlist.yaml (per spec):**
```yaml
actors:
  youtube:
    - channel_id: "UCxxxx"
      label: "creator_name"
  reddit:
    - subreddit: "Entrepreneur"
      label: "Entrepreneur"

topics:
  - "ai agents"
  - "distribution"

formats:
  - "contrarian hook"
  - "confession + lesson"
```

---

## Phase 2: Database Layer

### 2.1 Schema (per spec section 5)

**runs** - First-class run tracking
```sql
CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,        -- UUID
    started_at TEXT NOT NULL,       -- ISO 8601
    ended_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',  -- success/partial/failed
    sources_ok_json TEXT,           -- per-source health
    counts_json TEXT                -- snapshot + post counts
);
```

**actors**
```sql
CREATE TABLE actors (
    actor_id TEXT PRIMARY KEY,      -- youtube:UC... or reddit:Entrepreneur
    source TEXT NOT NULL,
    label TEXT,
    meta_json TEXT
);
```

**posts**
```sql
CREATE TABLE posts (
    post_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    published_at TEXT NOT NULL,
    meta_json TEXT
);
```

**snapshots** (append-only, per spec)
```sql
CREATE TABLE snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id TEXT NOT NULL REFERENCES posts(post_id),
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    ts TEXT NOT NULL,               -- run timestamp
    view_count INTEGER,
    like_count INTEGER,
    comment_count INTEGER,
    score INTEGER,                  -- Reddit
    num_comments INTEGER,           -- Reddit
    other_json TEXT,
    UNIQUE(post_id, ts)
);
```

**derived_post_metrics** (wide table per spec)
```sql
CREATE TABLE derived_post_metrics (
    post_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    velocity_6h REAL,
    velocity_24h REAL,
    z_views_6h REAL,
    z_comments_6h REAL,
    z_views_24h REAL,
    snapshot_count INTEGER,         -- confidence indicator
    post_age_hours REAL,
    flow_score REAL,
    PRIMARY KEY (post_id, ts)
);
```

**actor_baselines**
```sql
CREATE TABLE actor_baselines (
    actor_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    age_bucket TEXT NOT NULL,       -- 0-6h, 6-24h, 24-72h
    median REAL,
    mad REAL,
    sample_count INTEGER,
    computed_at TEXT NOT NULL,
    PRIMARY KEY (actor_id, metric, age_bucket)
);
```

**clusters**
```sql
CREATE TABLE clusters (
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
```

**note_history** (for "Since Last Close" novelty tracking)
```sql
CREATE TABLE note_history (
    note_id INTEGER PRIMARY KEY AUTOINCREMENT,
    generated_at TEXT NOT NULL,
    note_date TEXT NOT NULL,           -- YYYY-MM-DD local date
    output_path TEXT,
    posts_included_json TEXT,          -- JSON array of post_ids
    clusters_included_json TEXT        -- JSON array of cluster_ids
);

CREATE INDEX idx_note_history_date ON note_history(note_date);
```

**Usage:** Before generating a note, query the most recent note_history entry to get posts_included_json. Posts in that list are "repeats" unless their flow_score increased by threshold.

---

## Phase 3: Ingestion Layer

### 3.1 Run Discipline (per spec 6.1)
- One run = one timestamp shared by all snapshots
- Partial failures allowed but recorded explicitly
- No silent drops

### 3.2 YouTube Ingester (`src/ingest/youtube.py`)
- **Use uploads playlist, NOT search** (1 unit vs 100 units per channel)
  - Convert channel ID: `UCxxxx` → `UUxxxx` for uploads playlist
  - `playlistItems.list(playlistId=UUxxxx, maxResults=20)` = 1 unit
- Batch video stats request (up to 50 videos per call) = 1 unit
- Total: ~2 quota units per channel vs 101 with search
- Track cumulative quota, stop ingestion if approaching 10,000 daily limit
- Retry transient failures with exponential backoff (1s, 2s, 4s)
- Extract: viewCount, likeCount, commentCount

### 3.3 Reddit Ingester (`src/ingest/reddit.py`)
- Fetch newest 50 posts per watchlist subreddit
- Use PRAW with rate limit handling
- Extract: score, num_comments
- Exclude usernames from stored data

### 3.4 Orchestrator (`src/ingest/__init__.py`)
- Create run record at start
- Call each ingester, track per-source status
- Update run record with final status and counts
- Handle partial failures without stopping entire run

---

## Phase 4: Metrics Computation

### 4.1 Velocity (`src/metrics/velocity.py`)

Per spec section 7.1:
```
velocity = (metric_now - metric_then) / hours_elapsed
```

**Window rules (adjusted from spec for robustness):**
- Require >= 2 snapshots total for the post
- 6h window: find snapshot in [t-8h, t-4h], prefer closest to t-6h
- 24h window: find snapshot in [t-28h, t-20h], prefer closest to t-24h
- If no valid comparison snapshot exists, velocity = NULL
- Rationale: Wider windows tolerate missed ingestion runs while still producing meaningful velocity

### 4.2 Baselines (`src/metrics/baseline.py`)

Per spec section 7.3-7.4:
- Compute per actor, per metric, per age bucket
- Use last 30 comparable posts
- Stats: median, MAD (Median Absolute Deviation)
- If MAD = 0 or sample < 7: baseline unavailable

**Cold-start handling:**
- If actor has < 7 posts: use global baseline
- If global baseline unavailable: return NULL, flag as "insufficient history"

### 4.3 Z-Scores (`src/metrics/scoring.py`)

Per spec:
```
z = (metric - median) / (1.4826 * MAD)
```
- Clamp to [-10, 10]
- NULL if baseline invalid

### 4.4 Flow Score

Per spec section 7.5:
```
flow_score = 0.5 * z_comments_6h + 0.3 * z_views_6h + 0.2 * z_views_24h + cluster_bonus
```
- Missing components contribute 0
- Weights are documented constants, not learned

### 4.5 Clustering (`src/metrics/clustering.py`)

Per spec section 7.6:
- Select posts from last 48h where any z >= 2.0
- Group by source and dominant metric
- Require >= 5 posts, >= 3 unique actors
- Label using token overlap in titles + watchlist topic matches

---

## Phase 5: Flow Note Generation

### 5.1 Daily Close Note Structure (`src/publish/flownote.py`)

Output: `notes/YYYY-MM-DD.md`

**Sections per spec 8.1:**

1. **Title:** `ATTENTION FLOW DESK — YYYY-MM-DD`

2. **Since Last Close:** Only movers/clusters not in prior note

3. **Top Movers (Last 24h):** Table with:
   - post (link), source, actor, age
   - views/hr (6h), comments/hr (6h)
   - baseline z, flow score
   - snapshot count (confidence)
   - Max 2 rows per actor
   - Suppress repeats unless score increased by threshold

4. **Emerging Clusters:** cluster type, member count, unique actors, strength, representative links

5. **Flow Summary:** 3-7 bullets, each containing:
   - Observation (numbers + sample size)
   - Interpretation (one sentence, descriptive)
   - Invalidation trigger

6. **Next Watch:** 3-5 descriptive monitoring points (no recommendations)

7. **Data Status:** Last successful ingest per source, snapshot count (24h), degradation warnings

### 5.2 Novelty Tracking

Track which posts were included in previous notes:
- Add `note_history` table or flag on derived_post_metrics
- "Since Last Close" section only shows items not in prior note

---

## Phase 6: CLI Implementation

### 6.1 Entrypoint (`src/run.py`)

```
python -m src.run <command>
```

**Commands:**
- `ingest` - Run ingestion for all configured sources
- `score` - Compute derived metrics for recent posts
- `note` - Generate Daily Close Note
- `all` - Run ingest → score → note sequentially
- `doctor` - Check API credentials, DB integrity, snapshot density

**Flags:**
- `--since-hours N` - Limit time window
- `--dry-run` - Show what would happen without writes

### 6.2 Bootstrap Workflow (First-Time Setup)

New installations need multiple ingestion runs before meaningful output:

```bash
# 1. Verify configuration
python -m src.run doctor

# 2. First ingestion (establishes baseline snapshots)
python -m src.run ingest

# 3. Wait 4-6 hours (or run again immediately for testing)
python -m src.run ingest

# 4. Now velocity can be computed
python -m src.run score

# 5. First note (may still show limited data)
python -m src.run note
```

`doctor` command should output:
- "Insufficient snapshot history for velocity (need 2+ runs)"
- "Actor baselines not yet established (need 7+ posts per actor)"

---

## Phase 7: Testing

### Critical Tests Required:
1. **Velocity window validity** - Verify [t-7h, t-5h] rule for 6h window
2. **MAD baseline correctness** - Test with known values
3. **Z-score clamping** - Verify [-10, 10] bounds
4. **Cluster thresholds** - Verify 5 posts, 3 actors minimum
5. **Partial failure handling** - One source fails, others continue

---

## Collaboration & GitHub Setup

### Initial Repository Setup

```bash
# In the project directory
git init
git add .
git commit -m "Initial project setup with implementation plan"

# Create GitHub repo (using gh CLI)
gh repo create attention-desk --private --source=. --push
# Or use --public if you want it public
```

### What Goes in the Repo

```
attention-desk/
├── .gitignore           # Exclude .env, data/*.db, __pycache__
├── PLAN.md              # Copy of implementation plan (this document)
├── PROGRESS.md          # Updated by whoever works on it last
├── .env.example         # Template (no real secrets)
├── watchlist.yaml       # Can include example or real watchlist
└── src/                 # Implementation
```

### .gitignore Contents

```
# Secrets
.env

# Database
data/*.db

# Python
__pycache__/
*.pyc
.pytest_cache/

# IDE
.vscode/
.idea/

# Notes output (optional - may want to include)
# notes/
```

### Collaboration Workflow

1. **Before starting work:**
   - Pull latest: `git pull origin main`
   - Read PROGRESS.md to see current status
   - Claim what you're working on (update PROGRESS.md, commit, push)

2. **While working:**
   - Make commits at checkpoints
   - Keep PROGRESS.md "In Progress" section current

3. **After finishing a session:**
   - Update PROGRESS.md with what's done/remaining
   - Commit and push
   - Optional: Open a PR if working on a branch

### For Claude in New Context Windows

When starting a new session, tell Claude:

> "Clone/read the repo at [URL]. Read PLAN.md and PROGRESS.md, then continue implementation from where it left off."

Or if working locally:

> "Read the plan at attention-desk/PLAN.md and continue implementation. Check PROGRESS.md for current status."

---

## Progress Tracking (Cross-Session Continuity)

### PROGRESS.md File

Create `PROGRESS.md` in project root. Update this file at the end of each work session:

```markdown
# Attention Flow Desk - Implementation Progress

## Current Status
Phase: [1-7] - [Phase Name]
Last Updated: YYYY-MM-DD HH:MM
Last Commit: [short hash] - [commit message]

## Completed
- [x] Phase 1: Foundation
  - [x] Project structure created
  - [x] requirements.txt
  - [x] config.py (loads .env + watchlist.yaml)
- [ ] Phase 2: Database
  - [x] schema.py (all tables defined)
  - [ ] db.py (connection + init)
...

## In Progress
- Currently implementing: [specific file/function]
- Blocked by: [nothing / specific issue]
- Next action: [specific next step]

## Decisions Made
- 2024-XX-XX: Using uploads playlist for YouTube (quota)
- 2024-XX-XX: Widened velocity windows to [t-8h, t-4h]

## Known Issues
- [Issue description] → [planned resolution]
```

### Implementation Checkpoints

Each phase ends with a working state that can be resumed:

| Checkpoint | Marker | Resume Point |
|------------|--------|--------------|
| Phase 1 done | `config.py` loads without error | Can run `python -c "from src.config import load_config; load_config()"` |
| Phase 2 done | Tables exist in SQLite | Can run `python -m src.run doctor` (partial) |
| Phase 3 done | Snapshots in DB | Can run `python -m src.run ingest` |
| Phase 4 done | Derived metrics computed | Can run `python -m src.run score` |
| Phase 5 done | Notes generated | Can run `python -m src.run note` |
| Phase 6 done | CLI complete | Can run `python -m src.run all` |
| Phase 7 done | Tests pass | `pytest` passes |

### Git Commit Strategy

Commit at each sub-milestone with descriptive messages:

```
feat(db): add schema for runs, actors, posts, snapshots tables
feat(ingest): implement Reddit ingester with PRAW
feat(ingest): implement YouTube ingester with uploads playlist
feat(metrics): implement velocity calculation with window rules
feat(metrics): implement MAD-based baselines
feat(publish): implement flow note markdown generation
feat(cli): implement all commands (ingest, score, note, all, doctor)
test: add critical unit tests for velocity, baseline, z-score
```

### Resumption Protocol

When starting a new session:

1. **Read PROGRESS.md** to understand current state
2. **Run checkpoint command** for current phase to verify state
3. **Check git log** for recent commits
4. **Continue from "In Progress" section**

When ending a session:

1. **Commit current work** (even if incomplete, use WIP prefix)
2. **Update PROGRESS.md** with current status
3. **Document any decisions made** in Decisions Made section
4. **Note any blockers** in Known Issues section

---

## Build Order (Fastest Path to Working System)

| Step | Deliverable | Validates |
|------|-------------|-----------|
| 0 | PROGRESS.md + project skeleton | File structure exists, progress tracking ready |
| 1 | Config loading (.env + watchlist.yaml) | `python -c "from src.config import load_config; load_config()"` works |
| 2 | Database schema + connection | Tables created, UNIQUE constraints work |
| 3 | Reddit ingester (simpler to test) | Snapshots written, run tracked |
| 4 | Basic velocity calculation | Velocity computed for posts with 2+ snapshots |
| 5 | Minimal Flow Note (just top movers) | End-to-end `python -m src.run all` works |
| 6 | YouTube ingester | Both sources working |
| 7 | Full baseline + z-score computation | Actor-relative scoring works |
| 8 | Clustering | Clusters detected and included in notes |
| 9 | Full Flow Note sections | Complete Daily Close Note |
| 10 | Doctor command + tests | System health checks pass |

---

## Key Files to Implement

1. `src/config.py` - Load .env and watchlist.yaml
2. `src/db.py` - Schema creation, connection management
3. `src/ingest/reddit.py` - Reddit PRAW ingestion
4. `src/ingest/youtube.py` - YouTube Data API ingestion
5. `src/metrics/velocity.py` - Window-aware velocity calculation
6. `src/metrics/baseline.py` - MAD-based baselines with cold-start handling
7. `src/metrics/scoring.py` - Z-scores and flow score
8. `src/publish/flownote.py` - Markdown generation
9. `src/run.py` - CLI commands

---

## Known Issues & Mitigations

### Issue 1: YouTube API Quota Exceeded
**Problem:** Using search.list (100 units) per channel will exceed 10,000 daily quota with moderate watchlist.
**Solution:** Use channel uploads playlist instead:
```python
# Get uploads playlist ID from channel
uploads_playlist_id = "UU" + channel_id[2:]  # UC... → UU...
# playlistItems.list costs 1 unit vs search.list at 100 units
youtube.playlistItems().list(playlistId=uploads_playlist_id, part='snippet', maxResults=20)
```
**Cost:** 1 unit per channel + 1 unit for video stats batch = ~2 units per channel vs 101.

### Issue 2: Velocity Window Too Strict
**Problem:** Spec's [t-7h, t-5h] window with 2h ingestion means only one snapshot qualifies. One missed run = NULL velocity.
**Solution:** Widen window to [t-8h, t-4h] and use closest snapshot within window:
```python
# Find best comparison snapshot (closest to 6h ago)
target_time = now - timedelta(hours=6)
window_start = now - timedelta(hours=8)
window_end = now - timedelta(hours=4)
# Select snapshot closest to target_time within window
```

### Issue 3: First Run Empty Note
**Problem:** First run has no prior snapshots, so velocity/z-scores are all NULL.
**Solution:**
- `doctor` command warns: "System needs 2+ ingestion runs before meaningful notes"
- First note includes "Data Status" section explaining bootstrap state
- Add `--bootstrap` flag that runs ingest twice with 4h simulated gap for testing

### Issue 4: Timezone Inconsistency
**Problem:** APIs return UTC, note filenames should be local date.
**Solution:** Store all timestamps as UTC in database. Convert to local only at note generation:
```python
from zoneinfo import ZoneInfo
local_tz = ZoneInfo(config.timezone)  # e.g., "America/Los_Angeles"
local_date = datetime.now(local_tz).date()
filename = f"notes/{local_date}.md"
```

### Issue 5: Cluster Labeling Underspecified
**Problem:** "Token overlap in titles" lacks algorithm details.
**Solution:** Specific implementation:
```python
def extract_tokens(title: str) -> set[str]:
    # Lowercase, keep only alphanumeric, split on whitespace
    words = re.findall(r'\b[a-z]{4,}\b', title.lower())
    # Remove common stop words
    stop = {'this', 'that', 'with', 'from', 'have', 'will', 'what', 'just', 'about', 'like', 'your', 'they', 'been', 'more', 'when', 'some', 'there', 'were', 'would', 'into', 'which'}
    return {w for w in words if w not in stop}

def cluster_similarity(posts: list[Post]) -> float:
    # Jaccard similarity of token sets
    token_sets = [extract_tokens(p.title) for p in posts]
    intersection = set.intersection(*token_sets) if token_sets else set()
    union = set.union(*token_sets) if token_sets else set()
    return len(intersection) / len(union) if union else 0

# Cluster if >= 3 shared tokens among >= 3 actors
MIN_SHARED_TOKENS = 3
```

### Issue 6: Repeat Suppression Threshold Undefined
**Problem:** Spec says suppress unless "score ↑ ≥ threshold" but no default value.
**Solution:** Add to config with sensible default:
```yaml
# In .env
REPEAT_SCORE_THRESHOLD=0.5  # Only re-include if flow_score increased by 0.5+
```
Document in notes: "Posts from prior note suppressed unless flow_score increased by ≥0.5"

### Issue 7: No Retry Logic for API Failures
**Problem:** Transient network errors cause source to be marked failed.
**Solution:** Implement exponential backoff:
```python
import time
def with_retry(func, max_attempts=3, base_delay=1.0):
    for attempt in range(max_attempts):
        try:
            return func()
        except (ConnectionError, TimeoutError) as e:
            if attempt == max_attempts - 1:
                raise
            time.sleep(base_delay * (2 ** attempt))  # 1s, 2s, 4s
```

### Issue 8: SQLite Concurrent Access
**Problem:** Running ingest + score simultaneously could cause lock contention.
**Solution:**
- Use `PRAGMA busy_timeout=5000` (wait up to 5s for locks)
- Document that commands should run sequentially via cron, not parallel
- `run.py all` runs commands in sequence by design

---

## Verification Checklist

After implementation, verify:

- [ ] `python -m src.run doctor` passes all checks
- [ ] `python -m src.run ingest` creates snapshots with correct run tracking
- [ ] `python -m src.run score` computes velocities with proper NULL handling
- [ ] `python -m src.run note` produces readable Markdown with all sections
- [ ] `python -m src.run all` works end-to-end
- [ ] Repeated runs improve velocity accuracy (more snapshots = better deltas)
- [ ] Partial source failure doesn't crash entire run
- [ ] Notes show snapshot counts as confidence indicators
