"""
CLI entrypoint for Attention Flow Desk.

Usage:
    python -m src.run <command> [options]

Commands:
    ingest  - Run ingestion for all configured sources
    score   - Compute derived metrics for recent posts
    note    - Generate Daily Close Note
    all     - Run ingest → score → note sequentially
    doctor  - Check API credentials, DB integrity, snapshot density
"""

import argparse
import sys
from datetime import datetime
from typing import Optional

from rich.console import Console
from rich.table import Table

from . import db
from .config import get_config, load_config
from .ingest import run_ingestion
from .metrics.velocity import run_velocity_computation
from .metrics.scoring import run_scoring
from .metrics.clustering import run_clustering
from .publish.flownote import generate_flow_note

console = Console()


def cmd_ingest(args: argparse.Namespace) -> int:
    """Run ingestion for all configured sources."""
    console.print("[bold blue]Starting ingestion...[/bold blue]")

    try:
        result = run_ingestion()

        # Display results
        table = Table(title="Ingestion Results")
        table.add_column("Source")
        table.add_column("Status")
        table.add_column("Actors")
        table.add_column("Posts")
        table.add_column("Snapshots")
        table.add_column("Errors")

        for name, status in result.sources.items():
            status_str = "[green]OK[/green]" if status.success else "[red]FAILED[/red]"
            errors = "; ".join(status.errors[:2]) if status.errors else ""
            table.add_row(
                name,
                status_str,
                str(status.actors_processed),
                str(status.posts_fetched),
                str(status.snapshots_created),
                errors[:50]
            )

        console.print(table)
        console.print(f"\n[bold]Run ID:[/bold] {result.run_id}")
        console.print(f"[bold]Status:[/bold] {result.status}")
        console.print(f"[bold]Total:[/bold] {result.total_posts} posts, {result.total_snapshots} snapshots")

        return 0 if result.status in ("success", "partial") else 1

    except Exception as e:
        console.print(f"[red]Error during ingestion:[/red] {e}")
        return 1


def cmd_score(args: argparse.Namespace) -> int:
    """Compute derived metrics for recent posts."""
    hours = getattr(args, "since_hours", 72) or 72

    console.print(f"[bold blue]Computing metrics for posts from last {hours} hours...[/bold blue]")

    try:
        # Initialize DB if needed
        db.init_db()

        # Step 1: Compute velocities
        console.print("\n[dim]Computing velocities...[/dim]")
        velocity_result = run_velocity_computation(hours=hours)

        console.print(f"  Posts processed: {velocity_result['posts_processed']}")
        console.print(f"  With 6h velocity: {velocity_result['with_6h_velocity']}")
        console.print(f"  With 24h velocity: {velocity_result['with_24h_velocity']}")

        # Step 2: Compute baselines and z-scores
        console.print("\n[dim]Computing baselines and z-scores...[/dim]")
        scoring_result = run_scoring(hours=hours)

        console.print(f"  Baselines computed: {scoring_result['baselines']['baselines_stored']}")
        console.print(f"  Posts scored: {scoring_result['posts_scored']}")
        console.print(f"  With flow score: {scoring_result['with_flow_score']}")

        # Step 3: Detect clusters
        console.print("\n[dim]Detecting clusters...[/dim]")
        cluster_result = run_clustering(hours=48)

        console.print(f"  Clusters detected: {cluster_result['clusters_detected']}")
        for c in cluster_result.get('clusters', [])[:3]:
            console.print(f"    - {c['summary']} ({c['members']} posts)")

        # Summary
        console.print("\n[bold]Summary[/bold]")
        console.print(f"  Velocities: {velocity_result['with_6h_velocity']} posts")
        console.print(f"  Flow scores: {scoring_result['with_flow_score']} posts")
        console.print(f"  Clusters: {cluster_result['clusters_detected']}")

        if velocity_result["with_6h_velocity"] == 0:
            console.print("\n[yellow]Note: No 6h velocities computed. "
                          "Need 2+ ingestion runs 4-8 hours apart.[/yellow]")

        return 0

    except Exception as e:
        console.print(f"[red]Error during scoring:[/red] {e}")
        import traceback
        traceback.print_exc()
        return 1


def cmd_note(args: argparse.Namespace) -> int:
    """Generate Daily Close Note."""
    console.print("[bold blue]Generating Daily Close Note...[/bold blue]")

    try:
        # Initialize DB if needed
        db.init_db()

        result = generate_flow_note()

        if result["success"]:
            console.print(f"\n[green]Note generated successfully![/green]")
            console.print(f"[bold]Output:[/bold] {result['output_path']}")
            console.print(f"[bold]Date:[/bold] {result['note_date']}")
            console.print(f"[bold]Top Movers:[/bold] {result['movers_count']}")
            console.print(f"[bold]Clusters:[/bold] {result['clusters_count']}")
            return 0
        else:
            console.print("[red]Failed to generate note[/red]")
            return 1

    except Exception as e:
        console.print(f"[red]Error generating note:[/red] {e}")
        return 1


def cmd_all(args: argparse.Namespace) -> int:
    """Run ingest → score → note sequentially."""
    console.print("[bold blue]Running full pipeline: ingest → score → note[/bold blue]\n")

    # Ingest
    console.print("[bold]Step 1: Ingest[/bold]")
    console.print("-" * 40)
    ingest_result = cmd_ingest(args)
    console.print()

    # Score
    console.print("[bold]Step 2: Score[/bold]")
    console.print("-" * 40)
    score_result = cmd_score(args)
    console.print()

    # Note
    console.print("[bold]Step 3: Note[/bold]")
    console.print("-" * 40)
    note_result = cmd_note(args)
    console.print()

    # Summary
    if ingest_result == 0 and score_result == 0 and note_result == 0:
        console.print("[green bold]Pipeline completed successfully![/green bold]")
        return 0
    else:
        console.print("[yellow]Pipeline completed with some issues.[/yellow]")
        return 1


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check system health."""
    console.print("[bold blue]Running system health check...[/bold blue]\n")

    issues = []
    warnings = []

    # Check config
    console.print("[bold]Configuration[/bold]")
    try:
        config = load_config()
        console.print(f"  Base directory: {config.base_dir}")
        console.print(f"  Database path: {config.db_path}")
        console.print(f"  Notes directory: {config.notes_dir}")
        console.print(f"  Timezone: {config.timezone}")

        # Check API credentials
        if config.youtube_api_key:
            console.print("  YouTube API: [green]Configured[/green]")
        else:
            warnings.append("YouTube API key not configured")
            console.print("  YouTube API: [yellow]Not configured[/yellow]")

        if config.reddit_client_id and config.reddit_client_secret:
            console.print("  Reddit API: [green]Configured[/green]")
        else:
            warnings.append("Reddit API credentials not configured")
            console.print("  Reddit API: [yellow]Not configured[/yellow]")

        # Check watchlist
        yt_count = len(config.watchlist.youtube)
        rd_count = len(config.watchlist.reddit)
        console.print(f"  Watchlist: {yt_count} YouTube channels, {rd_count} Reddit subreddits")

        if yt_count == 0 and rd_count == 0:
            issues.append("No actors configured in watchlist.yaml")

    except Exception as e:
        issues.append(f"Configuration error: {e}")
        console.print(f"  [red]Error loading config: {e}[/red]")

    console.print()

    # Check database
    console.print("[bold]Database[/bold]")
    try:
        db.init_db()
        stats = db.get_db_stats()

        console.print(f"  Runs: {stats['runs_count']}")
        console.print(f"  Actors: {stats['actors_count']}")
        console.print(f"  Posts: {stats['posts_count']}")
        console.print(f"  Snapshots: {stats['snapshots_count']}")
        console.print(f"  Snapshots (24h): {stats['snapshots_24h']}")
        console.print(f"  Last run: {stats['last_run_at'] or 'Never'}")

        if stats['runs_count'] == 0:
            warnings.append("No ingestion runs yet - run 'ingest' to start collecting data")
        elif stats['runs_count'] == 1:
            warnings.append("Only 1 run recorded - velocity requires 2+ runs")

        if stats['snapshots_24h'] == 0 and stats['runs_count'] > 0:
            warnings.append("No snapshots in last 24 hours - ingestion may be stale")

    except Exception as e:
        issues.append(f"Database error: {e}")
        console.print(f"  [red]Error: {e}[/red]")

    console.print()

    # Summary
    console.print("[bold]Summary[/bold]")
    if issues:
        for issue in issues:
            console.print(f"  [red]ERROR:[/red] {issue}")
    if warnings:
        for warning in warnings:
            console.print(f"  [yellow]WARNING:[/yellow] {warning}")

    if not issues and not warnings:
        console.print("  [green]All checks passed![/green]")
        return 0
    elif issues:
        console.print(f"\n  {len(issues)} error(s), {len(warnings)} warning(s)")
        return 1
    else:
        console.print(f"\n  {len(warnings)} warning(s)")
        return 0


def main(argv: Optional[list[str]] = None) -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Attention Flow Desk - Engagement Analytics CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # ingest
    p_ingest = subparsers.add_parser("ingest", help="Run ingestion for all configured sources")

    # score
    p_score = subparsers.add_parser("score", help="Compute derived metrics")
    p_score.add_argument("--since-hours", type=int, default=72,
                         help="Process posts from the last N hours (default: 72)")

    # note
    p_note = subparsers.add_parser("note", help="Generate Daily Close Note")

    # all
    p_all = subparsers.add_parser("all", help="Run ingest → score → note")
    p_all.add_argument("--since-hours", type=int, default=72,
                       help="Process posts from the last N hours (default: 72)")

    # doctor
    p_doctor = subparsers.add_parser("doctor", help="Check system health")

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    commands = {
        "ingest": cmd_ingest,
        "score": cmd_score,
        "note": cmd_note,
        "all": cmd_all,
        "doctor": cmd_doctor,
    }

    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
