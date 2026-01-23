"""Query helpers for CLI commands."""

from datetime import datetime, date, timedelta, timezone
from typing import Optional

from .db import Database
from .config import Config, get_config


def get_local_timezone_offset() -> timedelta:
    """Get the local timezone offset from UTC."""
    local_now = datetime.now()
    utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
    return local_now - utc_now


def local_to_utc(dt: datetime) -> datetime:
    """Convert a naive local datetime to UTC for database queries."""
    offset = get_local_timezone_offset()
    return dt - offset


def utc_to_local(dt: datetime) -> datetime:
    """Convert a UTC datetime to local time for display."""
    offset = get_local_timezone_offset()
    return dt + offset


def get_today_range() -> tuple[datetime, datetime]:
    """Get datetime range for today in UTC (for querying UTC-stored timestamps)."""
    today = date.today()
    # Local midnight today and tomorrow
    local_start = datetime.combine(today, datetime.min.time())
    local_end = datetime.combine(today + timedelta(days=1), datetime.min.time())
    # Convert to UTC for database comparison
    return local_to_utc(local_start), local_to_utc(local_end)


def get_week_range(week_offset: int = 0) -> tuple[date, date]:
    """Get date range for a week.

    Args:
        week_offset: 0 for current week, -1 for last week, etc.
    """
    today = date.today()
    # Find Monday of the current week
    monday = today - timedelta(days=today.weekday())
    # Apply offset
    monday = monday + timedelta(weeks=week_offset)
    sunday = monday + timedelta(days=6)
    return monday, sunday


def get_month_range(month_offset: int = 0) -> tuple[date, date]:
    """Get date range for a month.

    Args:
        month_offset: 0 for current month, -1 for last month, etc.
    """
    today = date.today()

    # Calculate target month
    year = today.year
    month = today.month + month_offset

    while month <= 0:
        month += 12
        year -= 1
    while month > 12:
        month -= 12
        year += 1

    month_start = date(year, month, 1)

    # Get month end
    if month == 12:
        month_end = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        month_end = date(year, month + 1, 1) - timedelta(days=1)

    return month_start, month_end


class QueryHelper:
    """Helper class for CLI queries."""

    def __init__(self, config: Optional[Config] = None):
        self.config = config or get_config()
        self.db = Database(self.config)

    def get_project_id_by_name(self, name: str) -> Optional[int]:
        """Find project ID by name (partial match)."""
        projects = self.db.list_projects()
        for p in projects:
            if p['name'] and name.lower() in p['name'].lower():
                return p['id']
            if p['path'] and name.lower() in p['path'].lower():
                return p['id']
        return None

    def get_today_activity(self, project_id: Optional[int] = None) -> dict:
        """Get activity summary for today."""
        start, end = get_today_range()
        messages = self.db.get_messages_in_range(start, end, project_id)

        user_messages = [m for m in messages if m.get('role') == 'user']
        assistant_messages = [m for m in messages if m.get('role') == 'assistant']

        # Calculate tokens
        total_in = sum(m.get('tokens_in') or 0 for m in messages)
        total_out = sum(m.get('tokens_out') or 0 for m in messages)

        # Get unique sessions
        session_ids = set(m.get('session_uuid') for m in messages if m.get('session_uuid'))

        # Get unique projects
        project_names = set(m.get('project_name') for m in messages if m.get('project_name'))

        return {
            'date': date.today(),
            'total_messages': len(messages),
            'user_messages': len(user_messages),
            'assistant_messages': len(assistant_messages),
            'sessions': len(session_ids),
            'projects': list(project_names),
            'tokens_in': total_in,
            'tokens_out': total_out,
            'messages': messages
        }

    def get_recent_sessions(
        self,
        project_id: Optional[int] = None,
        since: Optional[datetime] = None,
        limit: int = 20
    ) -> list[dict]:
        """Get recent sessions with message counts and first message snippet."""
        sessions = self.db.list_sessions(project_id=project_id, since=since, limit=limit)

        # Enrich with message data
        for session in sessions:
            messages = self.db.get_messages_for_session(session['id'])
            user_messages = [m for m in messages if m.get('role') == 'user']
            session['user_count'] = len(user_messages)
            session['assistant_count'] = len([m for m in messages if m.get('role') == 'assistant'])

            # Get first user message as snippet
            session['first_message'] = None
            for msg in user_messages:
                content = msg.get('content') or ''
                content = content.strip()
                if content and not content.startswith('[Tool:'):
                    # Get first line or first 150 chars
                    first_line = content.split('\n')[0]
                    if len(first_line) > 150:
                        first_line = first_line[:150] + '...'
                    session['first_message'] = first_line
                    break

        return sessions

    def get_session_detail(self, session_id: str) -> Optional[dict]:
        """Get detailed session information."""
        session = self.db.get_session(session_id)
        if not session:
            return None

        messages = self.db.get_messages_for_session(session['id'])
        session['messages'] = messages

        # Get project info
        if session.get('project_id'):
            project = self.db.get_project(session['project_id'])
            session['project'] = project

        return session

    def get_stats_summary(self, since: Optional[datetime] = None) -> dict:
        """Get overall statistics."""
        return self.db.get_stats(since)

    def search_messages(
        self,
        query: str,
        project_id: Optional[int] = None,
        limit: int = 50
    ) -> list[dict]:
        """Search messages by content."""
        # Simple search - could be enhanced with FTS
        with self.db.connection() as conn:
            if project_id:
                cursor = conn.execute(
                    """SELECT m.*, s.session_id as session_uuid, p.name as project_name
                       FROM messages m
                       JOIN sessions s ON m.session_id = s.id
                       JOIN projects p ON s.project_id = p.id
                       WHERE m.content LIKE ? AND s.project_id = ?
                       ORDER BY m.timestamp DESC
                       LIMIT ?""",
                    (f"%{query}%", project_id, limit)
                )
            else:
                cursor = conn.execute(
                    """SELECT m.*, s.session_id as session_uuid, p.name as project_name
                       FROM messages m
                       JOIN sessions s ON m.session_id = s.id
                       LEFT JOIN projects p ON s.project_id = p.id
                       WHERE m.content LIKE ?
                       ORDER BY m.timestamp DESC
                       LIMIT ?""",
                    (f"%{query}%", limit)
                )
            return [dict(row) for row in cursor.fetchall()]
