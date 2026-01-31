# Attention Flow Desk

An opinionated, desk-style attention intelligence system for a single operator.

Designed to surface fresh acceleration, relative outperformance, and multi-actor confluence, expressed through evidence-backed written notes.

**Primary output:** Daily Close Notes suitable for paid distribution.

## Quick Start

```bash
# 1. Clone the repo
git clone <repo-url>
cd attention-desk

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure
cp .env.example .env
# Edit .env with your API keys
# Edit watchlist.yaml with your targets

# 4. Verify setup
python -m src.run doctor

# 5. Run ingestion (need 2+ runs before meaningful notes)
python -m src.run ingest

# 6. Generate note (after 2+ ingestion runs)
python -m src.run all
```

## Commands

```bash
python -m src.run <command>
```

| Command | Description |
|---------|-------------|
| `ingest` | Fetch latest posts from YouTube/Reddit |
| `score` | Compute velocity, z-scores, flow scores |
| `note` | Generate Daily Close Note |
| `all` | Run ingest → score → note |
| `doctor` | Check configuration and data health |

## Configuration

### .env
```
YOUTUBE_API_KEY=your_key
REDDIT_CLIENT_ID=your_id
REDDIT_CLIENT_SECRET=your_secret
```

### watchlist.yaml
```yaml
actors:
  youtube:
    - channel_id: "UCxxxx"
      label: "Channel Name"
  reddit:
    - subreddit: "Entrepreneur"
      label: "Entrepreneur"
```

## Development

See [PLAN.md](PLAN.md) for full implementation details.
See [PROGRESS.md](PROGRESS.md) for current implementation status.

## What This Is Not

- Not a dashboard or UI
- Not predictive or ML-based
- Not automated alerts
- Not recommendations or tactics

Flow is observational, not predictive.
