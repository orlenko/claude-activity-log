"""CLI interface for Claude Activity Logger."""

import os
import signal
import subprocess
import sys
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.markdown import Markdown
from rich.text import Text

from .config import get_config
from .db import Database
from .queries import QueryHelper, get_week_range, get_month_range
from .summarizer import Summarizer
from .watcher import (
    Watcher,
    read_pid_file,
    is_process_running,
    get_pid_file,
    remove_pid_file
)

console = Console()


@click.group()
@click.version_option(version="0.1.0")
def cli():
    """Claude Activity Logger - Monitor and summarize Claude Code sessions."""
    pass


# ============= Daemon commands =============

@cli.command()
@click.option('--foreground', '-f', is_flag=True, help='Run in foreground instead of daemon')
def start(foreground: bool):
    """Start the watcher daemon."""
    pid = read_pid_file()
    if pid and is_process_running(pid):
        console.print("[yellow]Watcher is already running[/yellow]")
        return

    if foreground:
        console.print("[green]Starting watcher in foreground...[/green]")
        from .watcher import run_daemon
        run_daemon()
    else:
        # Start as background process using -m to ensure proper imports
        python = sys.executable
        process = subprocess.Popen(
            [python, "-m", "claude_activity.watcher"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )
        # Wait briefly for process to start and write PID file
        import time
        time.sleep(0.5)
        pid = read_pid_file()
        if pid and is_process_running(pid):
            console.print(f"[green]Started watcher daemon (PID: {pid})[/green]")
        else:
            console.print(f"[yellow]Watcher started but may have exited. Check ~/.claude-activity/watcher.log[/yellow]")


@cli.command()
def stop():
    """Stop the watcher daemon."""
    pid = read_pid_file()
    if not pid:
        console.print("[yellow]No watcher PID file found[/yellow]")
        return

    if not is_process_running(pid):
        console.print("[yellow]Watcher process not running, cleaning up PID file[/yellow]")
        remove_pid_file()
        return

    try:
        os.kill(pid, signal.SIGTERM)
        console.print(f"[green]Stopped watcher daemon (PID: {pid})[/green]")
    except OSError as e:
        console.print(f"[red]Error stopping watcher: {e}[/red]")


@cli.command()
def status():
    """Show watcher daemon status."""
    config = get_config()
    db = Database(config)
    stats = db.get_stats()

    pid = read_pid_file()
    if pid and is_process_running(pid):
        status_text = f"[green]Running[/green] (PID: {pid})"
    else:
        status_text = "[red]Stopped[/red]"

    table = Table(title="Claude Activity Logger Status")
    table.add_column("Property", style="cyan")
    table.add_column("Value")

    table.add_row("Daemon Status", status_text)
    table.add_row("Database Path", str(config.database.path))
    table.add_row("Watch Directory", str(config.watcher.claude_dir / "projects"))
    table.add_row("Total Projects", str(stats['total_projects']))
    table.add_row("Total Sessions", str(stats['total_sessions']))
    table.add_row("Total Messages", str(stats['total_messages']))

    console.print(table)


# ============= View commands =============

@cli.command()
@click.option('--repo', '-r', help='Filter by repository name')
@click.option('--detailed', '-d', is_flag=True, help='Show detailed message list')
def today(repo: Optional[str], detailed: bool):
    """Show today's activity summary."""
    helper = QueryHelper()

    project_id = None
    if repo:
        project_id = helper.get_project_id_by_name(repo)
        if not project_id:
            console.print(f"[red]Project not found: {repo}[/red]")
            return

    activity = helper.get_today_activity(project_id)

    # Header
    title = f"Activity for {activity['date']}"
    if repo:
        title += f" ({repo})"

    panel_content = f"""
**Sessions:** {activity['sessions']}
**Messages:** {activity['total_messages']} ({activity['user_messages']} user, {activity['assistant_messages']} assistant)
**Projects:** {', '.join(activity['projects']) or 'None'}
**Tokens:** {activity['tokens_in']:,} in / {activity['tokens_out']:,} out
"""
    console.print(Panel(Markdown(panel_content), title=title))

    if detailed and activity['messages']:
        console.print("\n[bold]Messages:[/bold]\n")
        for msg in activity['messages'][:50]:  # Limit to 50
            role = msg.get('role', 'unknown')
            full_content = msg.get('content') or ''
            content = full_content[:200]
            if len(full_content) > 200:
                content += "..."
            timestamp = msg.get('timestamp', '')
            if isinstance(timestamp, datetime):
                timestamp = timestamp.strftime("%H:%M:%S")

            role_color = "blue" if role == 'user' else "green"
            console.print(f"[dim]{timestamp}[/dim] [{role_color}]{role}[/{role_color}]: {content}\n")


@cli.command()
@click.option('--repo', '-r', help='Filter by repository name')
@click.option('--generate', '-g', is_flag=True, help='Generate summary if not exists')
@click.option('--last', '-l', is_flag=True, help='Show last week instead of current')
@click.option('--offset', '-o', default=0, help='Week offset (0=current, -1=last week, etc.)')
def week(repo: Optional[str], generate: bool, last: bool, offset: int):
    """Show this week's summary."""
    helper = QueryHelper()
    config = get_config()

    project_id = None
    if repo:
        project_id = helper.get_project_id_by_name(repo)
        if not project_id:
            console.print(f"[red]Project not found: {repo}[/red]")
            return

    # --last is shorthand for --offset -1
    week_offset = -1 if last else offset
    week_start, week_end = get_week_range(week_offset)

    db = Database(config)
    summary = db.get_summary('weekly', week_start, project_id)

    if not summary and generate:
        console.print("[dim]Generating weekly summary...[/dim]")
        summarizer = Summarizer(config, db)
        summary_text = summarizer.generate_weekly_summary(week_start, project_id)
        if summary_text:
            summary = {'summary': summary_text}

    title = f"Week of {week_start} to {week_end}"
    if repo:
        title += f" ({repo})"

    if summary:
        console.print(Panel(Markdown(summary['summary']), title=title))
    else:
        console.print(f"[yellow]No summary available for {title}[/yellow]")
        console.print("[dim]Use --generate to create one, or run 'claude-activity summarize'[/dim]")


@cli.command()
@click.option('--repo', '-r', help='Filter by repository name')
@click.option('--generate', '-g', is_flag=True, help='Generate summary if not exists')
def month(repo: Optional[str], generate: bool):
    """Show this month's summary."""
    helper = QueryHelper()
    config = get_config()

    project_id = None
    if repo:
        project_id = helper.get_project_id_by_name(repo)
        if not project_id:
            console.print(f"[red]Project not found: {repo}[/red]")
            return

    month_start, month_end = get_month_range(0)

    db = Database(config)
    summary = db.get_summary('monthly', month_start, project_id)

    if not summary and generate:
        console.print("[dim]Generating monthly summary...[/dim]")
        summarizer = Summarizer(config, db)
        summary_text = summarizer.generate_monthly_summary(
            month_start.year, month_start.month, project_id
        )
        if summary_text:
            summary = {'summary': summary_text}

    title = f"{month_start.strftime('%B %Y')}"
    if repo:
        title += f" ({repo})"

    if summary:
        console.print(Panel(Markdown(summary['summary']), title=title))
    else:
        console.print(f"[yellow]No summary available for {title}[/yellow]")
        console.print("[dim]Use --generate to create one, or run 'claude-activity summarize'[/dim]")


@cli.command()
@click.option('--repo', '-r', help='Filter by repository name')
@click.option('--since', '-s', help='Show sessions since date (YYYY-MM-DD)')
@click.option('--limit', '-n', default=20, help='Number of sessions to show')
def sessions(repo: Optional[str], since: Optional[str], limit: int):
    """List recent sessions."""
    helper = QueryHelper()

    project_id = None
    if repo:
        project_id = helper.get_project_id_by_name(repo)
        if not project_id:
            console.print(f"[red]Project not found: {repo}[/red]")
            return

    since_dt = None
    if since:
        try:
            since_dt = datetime.strptime(since, "%Y-%m-%d")
            # Convert local date to UTC for database query
            from .queries import local_to_utc
            since_dt = local_to_utc(since_dt)
        except ValueError:
            console.print("[red]Invalid date format. Use YYYY-MM-DD[/red]")
            return

    sessions_list = helper.get_recent_sessions(project_id, since_dt, limit)
    # Filter out sessions with no actual messages (only system messages)
    sessions_list = [s for s in sessions_list if s.get('user_count', 0) > 0 or s.get('assistant_count', 0) > 0]

    if not sessions_list:
        console.print("[yellow]No sessions found[/yellow]")
        return

    table = Table(title="Recent Sessions")
    table.add_column("Session ID")
    table.add_column("Project")
    table.add_column("Started", style="cyan")
    table.add_column("Messages")
    table.add_column("Branch")

    from .queries import utc_to_local
    for s in sessions_list:
        # Show first 12 chars - enough to uniquely identify and tab-complete
        session_id = s['session_id'][:12]
        project = s.get('project_name') or 'Unknown'
        started = s.get('started_at', '')
        if isinstance(started, datetime):
            # Convert from UTC to local time for display
            started = utc_to_local(started).strftime("%Y-%m-%d %H:%M")
        messages = f"{s.get('user_count', 0)}u / {s.get('assistant_count', 0)}a"
        branch = s.get('git_branch') or '-'

        table.add_row(f"[cyan]{session_id}[/cyan]", project, started, messages, branch)

    console.print(table)
    console.print("\n[dim]Tip: View session details with: claude-activity session <session-id>[/dim]")


@cli.command()
@click.argument('session_id')
def session(session_id: str):
    """View a specific session's details."""
    helper = QueryHelper()

    # Try to find session by prefix match
    db = Database()
    with db.connection() as conn:
        cursor = conn.execute(
            "SELECT session_id FROM sessions WHERE session_id LIKE ?",
            (f"{session_id}%",)
        )
        row = cursor.fetchone()
        if row:
            session_id = row['session_id']

    detail = helper.get_session_detail(session_id)

    if not detail:
        console.print(f"[red]Session not found: {session_id}[/red]")
        return

    # Session info
    project_name = detail.get('project', {}).get('name') or 'Unknown'
    started = detail.get('started_at', '')
    if isinstance(started, datetime):
        started = started.strftime("%Y-%m-%d %H:%M:%S")

    console.print(Panel(
        f"**Project:** {project_name}\n"
        f"**Started:** {started}\n"
        f"**Branch:** {detail.get('git_branch') or 'N/A'}\n"
        f"**Messages:** {len(detail.get('messages', []))}",
        title=f"Session {session_id[:16]}..."
    ))

    # Messages
    messages = detail.get('messages', [])
    if messages:
        console.print("\n[bold]Conversation:[/bold]\n")
        for msg in messages:
            role = msg.get('role')
            if not role:
                continue

            content = msg.get('content') or ''

            # Skip empty messages and tool-only messages
            content_stripped = content.strip()
            if not content_stripped:
                continue
            if content_stripped.startswith('[Tool:') or content_stripped == '[Tool Result]':
                continue

            timestamp = msg.get('timestamp', '')
            if isinstance(timestamp, datetime):
                timestamp = timestamp.strftime("%H:%M:%S")

            role_color = "blue" if role == 'user' else "green"

            # Truncate long messages
            if len(content) > 500:
                content = content[:500] + "\n[dim]...(truncated)[/dim]"

            console.print(f"[dim]{timestamp}[/dim] [{role_color}][bold]{role}[/bold][/{role_color}]")
            console.print(content)
            console.print()


# ============= Summarization commands =============

@cli.command()
@click.option('--force', '-f', is_flag=True, help='Regenerate existing summaries')
@click.option('--repo', '-r', help='Filter by repository name')
def summarize(force: bool, repo: Optional[str]):
    """Generate summaries for unsummarized periods."""
    config = get_config()
    helper = QueryHelper(config)

    project_id = None
    if repo:
        project_id = helper.get_project_id_by_name(repo)
        if not project_id:
            console.print(f"[red]Project not found: {repo}[/red]")
            return

    console.print("[dim]Generating summaries...[/dim]")

    try:
        summarizer = Summarizer(config)
        results = summarizer.summarize_unsummarized(project_id, force)

        console.print(f"\n[green]Generated summaries:[/green]")
        console.print(f"  Daily:  {results['daily']}")
        console.print(f"  Weekly: {results['weekly']}")
        console.print(f"  Monthly: {results['monthly']}")
    except Exception as e:
        console.print(f"[red]Error generating summaries: {e}[/red]")
        raise


# ============= Project commands =============

@cli.command()
def projects():
    """List all tracked projects."""
    db = Database()
    project_list = db.list_projects()

    if not project_list:
        console.print("[yellow]No projects tracked yet[/yellow]")
        return

    table = Table(title="Tracked Projects")
    table.add_column("ID", style="dim")
    table.add_column("Name", style="cyan")
    table.add_column("Path")
    table.add_column("Org")

    for p in project_list:
        table.add_row(
            str(p['id']),
            p.get('name') or '-',
            p.get('path') or '-',
            p.get('org') or '-'
        )

    console.print(table)


# ============= Search commands =============

@cli.command()
@click.argument('query')
@click.option('--repo', '-r', help='Filter by repository name')
@click.option('--limit', '-n', default=20, help='Number of results')
def search(query: str, repo: Optional[str], limit: int):
    """Search messages by content."""
    helper = QueryHelper()

    project_id = None
    if repo:
        project_id = helper.get_project_id_by_name(repo)
        if not project_id:
            console.print(f"[red]Project not found: {repo}[/red]")
            return

    results = helper.search_messages(query, project_id, limit)

    if not results:
        console.print(f"[yellow]No messages found matching: {query}[/yellow]")
        return

    console.print(f"[bold]Found {len(results)} messages:[/bold]\n")

    for msg in results:
        role = msg.get('role', 'unknown')
        content = msg.get('content') or ''
        timestamp = msg.get('timestamp', '')
        project = msg.get('project_name', 'Unknown')
        session_id = msg.get('session_uuid', '')[:12] if msg.get('session_uuid') else ''

        if isinstance(timestamp, datetime):
            timestamp = timestamp.strftime("%Y-%m-%d %H:%M")

        if not content:
            continue

        # Highlight query in content
        snippet_start = max(0, content.lower().find(query.lower()) - 50)
        snippet_end = min(len(content), snippet_start + 200)
        snippet = content[snippet_start:snippet_end]
        if snippet_start > 0:
            snippet = "..." + snippet
        if snippet_end < len(content):
            snippet = snippet + "..."

        role_color = "blue" if role == 'user' else "green"
        console.print(f"[dim]{timestamp}[/dim] [cyan]{session_id}[/cyan] {project} [{role_color}]{role}[/{role_color}]")
        console.print(f"  {snippet}\n")

    console.print("[dim]Tip: View full session with: claude-activity session <session-id>[/dim]")


if __name__ == "__main__":
    cli()
