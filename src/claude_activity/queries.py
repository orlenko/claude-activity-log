"""Query helpers for CLI commands.

For timestamp handling conventions, see timestamps.py.
"""

import json
from datetime import datetime, date, timedelta
from typing import Optional

from .db import Database
from .config import Config, get_config
from .timestamps import (
    utc_to_local,
    local_to_utc,
    get_today_utc_range,
    get_local_offset,
    utc_now,
)


# Pending questions older than this are not highlighted (considered abandoned)
PENDING_QUESTION_MAX_AGE_DAYS = 3


def get_today_range() -> tuple[datetime, datetime]:
    """Get datetime range for today in UTC (for querying UTC-stored timestamps).

    Delegates to timestamps.get_today_utc_range().
    """
    return get_today_utc_range()


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
        """Get recent sessions with message counts and first message snippet.

        Sessions with pending questions (asked within the last 3 days) are sorted first.
        """
        # Calculate cutoff for pending questions
        pending_cutoff = utc_now() - timedelta(days=PENDING_QUESTION_MAX_AGE_DAYS)

        # Get sessions with custom sorting (pending questions first)
        with self.db.connection() as conn:
            query = """
                SELECT s.*, p.name as project_name, p.path as project_path,
                    CASE
                        WHEN s.pending_question IS NOT NULL
                             AND s.pending_question_time >= ?
                        THEN 1
                        ELSE 0
                    END as has_active_pending
                FROM sessions s
                LEFT JOIN projects p ON s.project_id = p.id
            """
            conditions = []
            params = [pending_cutoff]

            if project_id is not None:
                conditions.append("s.project_id = ?")
                params.append(project_id)
            if since is not None:
                conditions.append("s.started_at >= ?")
                params.append(since)

            if conditions:
                query += " WHERE " + " AND ".join(conditions)

            # Sort: pending questions first (by question time desc), then by start time desc
            query += """
                ORDER BY has_active_pending DESC,
                         CASE WHEN has_active_pending = 1 THEN s.pending_question_time ELSE s.started_at END DESC
                LIMIT ?
            """
            params.append(limit)

            cursor = conn.execute(query, params)
            sessions = [dict(row) for row in cursor.fetchall()]

        # Enrich with message data and parse pending question
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

            # Parse pending question JSON and check if it's still active
            session['pending_question_data'] = None
            if session.get('pending_question') and session.get('has_active_pending'):
                try:
                    session['pending_question_data'] = json.loads(session['pending_question'])
                except (json.JSONDecodeError, TypeError):
                    pass

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

    def get_recent_projects_with_sessions(
        self,
        project_limit: int = 4,
        sessions_per_project: int = 3
    ) -> list[dict]:
        """Get projects sorted by most recent session, with their recent sessions.

        Sessions with pending questions (asked within the last 3 days) are highlighted.
        Projects with pending questions are sorted first.

        Returns a tree structure:
        [
            {
                'project': {...},
                'last_activity': datetime,
                'has_pending': bool,
                'sessions': [
                    {'session': {...}, 'first_message': '...', 'pending_question_data': {...}},
                    ...
                ]
            },
            ...
        ]
        """
        pending_cutoff = utc_now() - timedelta(days=PENDING_QUESTION_MAX_AGE_DAYS)

        with self.db.connection() as conn:
            # Get projects with their most recent session timestamp and pending status
            cursor = conn.execute("""
                SELECT p.*,
                       MAX(s.started_at) as last_activity,
                       MAX(CASE
                           WHEN s.pending_question IS NOT NULL
                                AND s.pending_question_time >= ?
                           THEN 1
                           ELSE 0
                       END) as has_pending
                FROM projects p
                JOIN sessions s ON s.project_id = p.id
                GROUP BY p.id
                ORDER BY has_pending DESC, last_activity DESC
                LIMIT ?
            """, (pending_cutoff, project_limit))
            projects_with_activity = [dict(row) for row in cursor.fetchall()]

        result = []
        for proj in projects_with_activity:
            # Get recent sessions for this project with pending question sorting
            with self.db.connection() as conn:
                cursor = conn.execute("""
                    SELECT s.*, p.name as project_name, p.path as project_path,
                        CASE
                            WHEN s.pending_question IS NOT NULL
                                 AND s.pending_question_time >= ?
                            THEN 1
                            ELSE 0
                        END as has_active_pending
                    FROM sessions s
                    LEFT JOIN projects p ON s.project_id = p.id
                    WHERE s.project_id = ?
                    ORDER BY has_active_pending DESC,
                             CASE WHEN has_active_pending = 1 THEN s.pending_question_time ELSE s.started_at END DESC
                    LIMIT ?
                """, (pending_cutoff, proj['id'], sessions_per_project))
                sessions = [dict(row) for row in cursor.fetchall()]

            # Enrich sessions with message counts and first message
            enriched_sessions = []
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
                        first_line = content.split('\n')[0]
                        if len(first_line) > 100:
                            first_line = first_line[:100] + '...'
                        session['first_message'] = first_line
                        break

                # Parse pending question JSON
                session['pending_question_data'] = None
                if session.get('pending_question') and session.get('has_active_pending'):
                    try:
                        session['pending_question_data'] = json.loads(session['pending_question'])
                    except (json.JSONDecodeError, TypeError):
                        pass

                # Only include sessions with actual messages
                if session['user_count'] > 0 or session['assistant_count'] > 0:
                    enriched_sessions.append(session)

            if enriched_sessions:
                result.append({
                    'project': proj,
                    'last_activity': proj['last_activity'],
                    'has_pending': bool(proj.get('has_pending')),
                    'sessions': enriched_sessions
                })

        return result

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
