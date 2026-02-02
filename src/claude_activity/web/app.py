"""Flask application for Claude Activity Logger web UI.

For timestamp handling conventions, see timestamps.py.
"""

import os
import re
from datetime import datetime, date, timedelta
from typing import Optional
from markupsafe import Markup

from flask import Flask, render_template, request, jsonify, redirect, url_for

from ..config import get_config
from ..db import Database
from ..queries import QueryHelper, get_week_range, get_month_range
from ..summarizer import Summarizer
from ..watcher import read_pid_file, is_process_running
from ..timestamps import utc_to_local, utc_now, timeago as ts_timeago


def create_app(config=None):
    """Create and configure the Flask application."""
    app = Flask(__name__,
                template_folder='templates',
                static_folder='static')

    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-key-change-in-production')

    if config is None:
        config = get_config()

    # Store config for use in routes
    app.claude_config = config

    @app.context_processor
    def inject_globals():
        """Inject global variables into all templates."""
        pid = read_pid_file()
        daemon_running = pid and is_process_running(pid)
        # Use local time for display in templates
        return {
            'daemon_running': daemon_running,
            'daemon_pid': pid if daemon_running else None,
            'now': utc_to_local(utc_now()),  # Current local time for display
        }

    @app.template_filter('localtime')
    def localtime_filter(dt):
        """Convert UTC datetime to local time string.

        All stored timestamps are in UTC. This filter converts for display.
        See timestamps.py for the timestamp convention.
        """
        if dt is None:
            return ''
        if isinstance(dt, datetime):
            return utc_to_local(dt).strftime('%Y-%m-%d %H:%M:%S')
        return str(dt)

    @app.template_filter('localdate')
    def localdate_filter(dt):
        """Convert UTC datetime to local date string.

        All stored timestamps are in UTC. This filter converts for display.
        See timestamps.py for the timestamp convention.
        """
        if dt is None:
            return ''
        if isinstance(dt, datetime):
            return utc_to_local(dt).strftime('%Y-%m-%d')
        return str(dt)

    @app.template_filter('shorttime')
    def shorttime_filter(dt):
        """Convert UTC datetime to short time string (HH:MM:SS).

        All stored timestamps are in UTC. This filter converts for display.
        """
        if dt is None:
            return ''
        if isinstance(dt, datetime):
            return utc_to_local(dt).strftime('%H:%M:%S')
        return str(dt)

    @app.template_filter('timeago')
    def timeago_filter(dt):
        """Convert UTC datetime to relative time string.

        All stored timestamps are in UTC. This filter handles the conversion.
        See timestamps.py for the timestamp convention.
        """
        if dt is None:
            return ''
        if isinstance(dt, datetime):
            return ts_timeago(dt)
        return str(dt)

    @app.template_filter('markdown')
    def markdown_filter(text):
        """Convert markdown to HTML."""
        if not text:
            return ''

        # Escape HTML first
        text = str(text)

        # Convert markdown to HTML (simple implementation)
        lines = text.split('\n')
        html_lines = []
        in_list = False

        for line in lines:
            stripped = line.strip()

            # Headers
            if stripped.startswith('## '):
                if in_list:
                    html_lines.append('</ul>')
                    in_list = False
                html_lines.append(f'<h2 class="text-lg font-semibold mt-4 mb-2 text-gray-900 dark:text-white">{stripped[3:]}</h2>')
            elif stripped.startswith('### '):
                if in_list:
                    html_lines.append('</ul>')
                    in_list = False
                html_lines.append(f'<h3 class="text-base font-semibold mt-3 mb-2 text-gray-900 dark:text-white">{stripped[4:]}</h3>')
            # Bullet points
            elif stripped.startswith('- '):
                if not in_list:
                    html_lines.append('<ul class="list-disc list-inside space-y-1 mb-3 text-gray-700 dark:text-gray-300">')
                    in_list = True
                # Handle bold text in list items
                item_text = stripped[2:]
                item_text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', item_text)
                html_lines.append(f'<li>{item_text}</li>')
            # Empty line
            elif not stripped:
                if in_list:
                    html_lines.append('</ul>')
                    in_list = False
                html_lines.append('<br>')
            # Regular paragraph
            else:
                if in_list:
                    html_lines.append('</ul>')
                    in_list = False
                # Handle bold text
                para_text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', stripped)
                html_lines.append(f'<p class="mb-2 text-gray-700 dark:text-gray-300">{para_text}</p>')

        if in_list:
            html_lines.append('</ul>')

        return Markup('\n'.join(html_lines))

    # ============= Main Routes =============

    @app.route('/')
    def index():
        """Dashboard home page."""
        helper = QueryHelper(config)
        db = Database(config)

        # Get today's activity
        today_activity = helper.get_today_activity()

        # Get stats
        stats = db.get_stats()

        # Get recent projects with their sessions (tree structure)
        recent_activity = helper.get_recent_projects_with_sessions(
            project_limit=4,
            sessions_per_project=3
        )

        return render_template('index.html',
                             today=today_activity,
                             stats=stats,
                             recent_activity=recent_activity)

    @app.route('/today')
    def today():
        """Today's activity page."""
        helper = QueryHelper(config)
        project_id = request.args.get('project_id', type=int)

        activity = helper.get_today_activity(project_id)

        # Get projects for filter dropdown
        db = Database(config)
        projects = db.list_projects()

        return render_template('today.html',
                             activity=activity,
                             projects=projects,
                             selected_project_id=project_id)

    @app.route('/sessions')
    def sessions():
        """Sessions list page."""
        helper = QueryHelper(config)
        db = Database(config)

        project_id = request.args.get('project_id', type=int)
        page = request.args.get('page', 1, type=int)
        per_page = 20

        sessions_list = helper.get_recent_sessions(
            project_id=project_id,
            limit=per_page + 1  # Get one extra to check if there's more
        )

        # Filter out empty sessions
        sessions_list = [s for s in sessions_list
                        if s.get('user_count', 0) > 0 or s.get('assistant_count', 0) > 0]

        has_more = len(sessions_list) > per_page
        sessions_list = sessions_list[:per_page]

        projects = db.list_projects()

        return render_template('sessions.html',
                             sessions=sessions_list,
                             projects=projects,
                             selected_project_id=project_id,
                             page=page,
                             has_more=has_more)

    @app.route('/session/<session_id>')
    def session_detail(session_id):
        """Single session detail page."""
        helper = QueryHelper(config)
        db = Database(config)

        # Try to find session by prefix match
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
            return render_template('error.html',
                                 message=f"Session not found: {session_id}"), 404

        # Filter messages
        messages = detail.get('messages', [])
        filtered_messages = []
        for msg in messages:
            role = msg.get('role')
            content = msg.get('content') or ''

            if not role or not content.strip():
                continue
            if content.strip().startswith('[Tool:') or content.strip() == '[Tool Result]':
                continue

            filtered_messages.append(msg)

        detail['messages'] = filtered_messages

        return render_template('session.html', session=detail)

    @app.route('/projects')
    def projects():
        """Projects list page."""
        db = Database(config)
        projects_list = db.list_projects()

        # Enrich with session counts
        for p in projects_list:
            with db.connection() as conn:
                cursor = conn.execute(
                    "SELECT COUNT(*) as count FROM sessions WHERE project_id = ?",
                    (p['id'],)
                )
                p['session_count'] = cursor.fetchone()['count']

        return render_template('projects.html', projects=projects_list)

    @app.route('/project/<int:project_id>')
    def project_detail(project_id):
        """Single project detail page."""
        db = Database(config)
        helper = QueryHelper(config)

        project = db.get_project(project_id)
        if not project:
            return render_template('error.html',
                                 message=f"Project not found: {project_id}"), 404

        sessions_list = helper.get_recent_sessions(project_id=project_id, limit=50)
        sessions_list = [s for s in sessions_list
                        if s.get('user_count', 0) > 0 or s.get('assistant_count', 0) > 0]

        return render_template('project.html',
                             project=project,
                             sessions=sessions_list)

    @app.route('/search')
    def search():
        """Search page."""
        helper = QueryHelper(config)
        db = Database(config)

        query = request.args.get('q', '').strip()
        project_id = request.args.get('project_id', type=int)

        results = []
        if query:
            results = helper.search_messages(query, project_id, limit=50)

        projects = db.list_projects()

        return render_template('search.html',
                             query=query,
                             results=results,
                             projects=projects,
                             selected_project_id=project_id)

    @app.route('/live')
    def live():
        """Live activity feed page - shows recent activity in real-time."""
        db = Database(config)
        project_id = request.args.get('project_id', type=int)

        # Get all projects for filter dropdown
        projects = db.list_projects()

        # Get initial batch of live entries
        entries = _get_live_entries(db, project_id, limit=100)

        return render_template('live.html',
                             entries=entries,
                             projects=projects,
                             selected_project_id=project_id)

    def _get_live_entries(db: Database, project_id: Optional[int] = None, limit: int = 100, since_id: Optional[int] = None) -> list[dict]:
        """Get recent message entries for live feed.

        Returns entries in chronological order (oldest first) for appending to feed.
        Each entry contains: id, timestamp, project_name, role, content_preview
        """
        with db.connection() as conn:
            if since_id is not None:
                # Incremental update: get new entries after since_id in chronological order
                query = """
                    SELECT m.id, m.timestamp, m.role, m.content,
                           s.session_id, p.name as project_name, p.path as project_path
                    FROM messages m
                    JOIN sessions s ON m.session_id = s.id
                    JOIN projects p ON s.project_id = p.id
                    WHERE m.role IN ('user', 'assistant')
                      AND m.id > ?
                """
                params = [since_id]
                if project_id is not None:
                    query += " AND s.project_id = ?"
                    params.append(project_id)
                query += " ORDER BY m.id ASC LIMIT ?"
                params.append(limit)
            else:
                # Initial load: get recent entries, then reverse for chronological order
                query = """
                    SELECT m.id, m.timestamp, m.role, m.content,
                           s.session_id, p.name as project_name, p.path as project_path
                    FROM messages m
                    JOIN sessions s ON m.session_id = s.id
                    JOIN projects p ON s.project_id = p.id
                    WHERE m.role IN ('user', 'assistant')
                """
                params = []
                if project_id is not None:
                    query += " AND s.project_id = ?"
                    params.append(project_id)
                query += " ORDER BY m.id DESC LIMIT ?"
                params.append(limit)

            cursor = conn.execute(query, params)
            rows = [dict(row) for row in cursor.fetchall()]

        # For initial load, reverse to get chronological order
        if since_id is None:
            rows = list(reversed(rows))

        # Process entries to create preview
        entries = []
        for row in rows:
            content = row.get('content', '') or ''

            # Clean up content - remove tool markers, get first meaningful line
            lines = content.strip().split('\n')
            preview = ''
            for line in lines:
                line = line.strip()
                if line and not line.startswith('[Tool:') and line != '[Tool Result]':
                    preview = line[:150]
                    if len(line) > 150:
                        preview += '...'
                    break

            if not preview:
                preview = '(no content)'

            entries.append({
                'id': row['id'],
                'timestamp': row['timestamp'],
                'project_name': row['project_name'],
                'project_path': row['project_path'],
                'session_id': row['session_id'],
                'role': row['role'],
                'preview': preview
            })

        return entries

    # ============= Summary Routes =============

    @app.route('/summaries')
    def summaries():
        """Summaries overview page."""
        db = Database(config)

        # Get recent summaries
        with db.connection() as conn:
            cursor = conn.execute("""
                SELECT s.*, p.name as project_name
                FROM summaries s
                LEFT JOIN projects p ON s.project_id = p.id
                ORDER BY s.period_start DESC
                LIMIT 50
            """)
            summaries_list = [dict(row) for row in cursor.fetchall()]

        return render_template('summaries.html', summaries=summaries_list)

    @app.route('/summary/week')
    @app.route('/summary/week/<int:offset>')
    def week_summary(offset=0):
        """Weekly summary page."""
        db = Database(config)
        helper = QueryHelper(config)

        project_id = request.args.get('project_id', type=int)
        week_start, week_end = get_week_range(offset)

        summary = db.get_summary('weekly', week_start, project_id)

        # Get daily activity for the week
        daily_activity = []
        for i in range(7):
            day = week_start + timedelta(days=i)
            if day <= date.today():
                day_summary = db.get_summary('daily', day, project_id)
                daily_activity.append({
                    'date': day,
                    'summary': day_summary
                })

        projects = db.list_projects()

        return render_template('week_summary.html',
                             week_start=week_start,
                             week_end=week_end,
                             offset=offset,
                             summary=summary,
                             daily_activity=daily_activity,
                             projects=projects,
                             selected_project_id=project_id)

    @app.route('/summary/month')
    @app.route('/summary/month/<int:year>/<int:month>')
    def month_summary(year=None, month=None):
        """Monthly summary page."""
        db = Database(config)

        if year is None or month is None:
            today = date.today()
            year = today.year
            month = today.month

        project_id = request.args.get('project_id', type=int)
        month_start, month_end = get_month_range(0)

        # Adjust for requested month
        month_start = date(year, month, 1)
        if month == 12:
            month_end = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            month_end = date(year, month + 1, 1) - timedelta(days=1)

        summary = db.get_summary('monthly', month_start, project_id)

        # Get weekly summaries for the month
        weekly_summaries = db.get_summaries_in_range('weekly', month_start, month_end, project_id)

        projects = db.list_projects()

        return render_template('month_summary.html',
                             year=year,
                             month=month,
                             month_start=month_start,
                             month_end=month_end,
                             summary=summary,
                             weekly_summaries=weekly_summaries,
                             projects=projects,
                             selected_project_id=project_id)

    # ============= API Routes (for HTMX) =============

    @app.route('/api/generate-summary', methods=['POST'])
    def generate_summary():
        """Generate a summary via HTMX."""
        period_type = request.form.get('period_type')
        period_start = request.form.get('period_start')
        project_id = request.form.get('project_id', type=int)

        try:
            db = Database(config)
            summarizer = Summarizer(config, db)

            if period_type == 'daily':
                target_date = datetime.strptime(period_start, '%Y-%m-%d').date()
                summary = summarizer.generate_daily_summary(target_date, project_id, force=True)
            elif period_type == 'weekly':
                week_start = datetime.strptime(period_start, '%Y-%m-%d').date()
                summary = summarizer.generate_weekly_summary(week_start, project_id, force=True)
            elif period_type == 'monthly':
                month_start = datetime.strptime(period_start, '%Y-%m-%d').date()
                summary = summarizer.generate_monthly_summary(
                    month_start.year, month_start.month, project_id, force=True
                )
            else:
                return jsonify({'error': 'Invalid period type'}), 400

            if summary:
                return render_template('partials/summary_content.html',
                                     summary={'summary': summary})
            else:
                return render_template('partials/summary_content.html',
                                     summary=None,
                                     message="No activity found for this period.")

        except Exception as e:
            return render_template('partials/summary_content.html',
                                 summary=None,
                                 error=str(e))

    @app.route('/api/daemon/status')
    def daemon_status():
        """Get daemon status for HTMX polling."""
        pid = read_pid_file()
        running = pid and is_process_running(pid)
        return render_template('partials/daemon_status.html',
                             running=running,
                             pid=pid if running else None)

    @app.route('/api/activity/live')
    def live_activity():
        """Get live activity feed for HTMX polling."""
        helper = QueryHelper(config)
        activity = helper.get_today_activity()
        recent_sessions = helper.get_recent_sessions(limit=5)
        recent_sessions = [s for s in recent_sessions
                         if s.get('user_count', 0) > 0 or s.get('assistant_count', 0) > 0]

        return render_template('partials/live_activity.html',
                             activity=activity,
                             recent_sessions=recent_sessions)

    @app.route('/api/session-context/<session_id>', methods=['POST'])
    def generate_session_context(session_id):
        """Generate a detailed context summary for a session."""
        try:
            db = Database(config)
            summarizer = Summarizer(config, db)

            # Find session by prefix
            with db.connection() as conn:
                cursor = conn.execute(
                    "SELECT session_id FROM sessions WHERE session_id LIKE ?",
                    (f"{session_id}%",)
                )
                row = cursor.fetchone()
                if row:
                    session_id = row['session_id']
                else:
                    return render_template('partials/session_context.html',
                                         error="Session not found")

            context = summarizer.generate_session_context(session_id)

            if context:
                return render_template('partials/session_context.html',
                                     context=context)
            else:
                return render_template('partials/session_context.html',
                                     error="Could not generate context (session may be empty)")

        except Exception as e:
            return render_template('partials/session_context.html',
                                 error=str(e))

    @app.route('/api/live/entries')
    def api_live_entries():
        """Get live feed entries for HTMX polling."""
        db = Database(config)
        project_id = request.args.get('project_id', type=int)
        since_id = request.args.get('since_id', type=int)
        limit = request.args.get('limit', 50, type=int)

        entries = _get_live_entries(db, project_id, limit=limit, since_id=since_id)

        return render_template('partials/live_entries.html', entries=entries)

    @app.route('/api/session-context/<session_id>/raw', methods=['GET'])
    def get_session_context_raw(session_id):
        """Get raw session context for copying."""
        try:
            db = Database(config)
            summarizer = Summarizer(config, db)

            # Find session by prefix
            with db.connection() as conn:
                cursor = conn.execute(
                    "SELECT session_id FROM sessions WHERE session_id LIKE ?",
                    (f"{session_id}%",)
                )
                row = cursor.fetchone()
                if row:
                    session_id = row['session_id']
                else:
                    return jsonify({'error': 'Session not found'}), 404

            context = summarizer.generate_session_context(session_id)

            if context:
                return jsonify({'context': context})
            else:
                return jsonify({'error': 'Could not generate context'}), 404

        except Exception as e:
            return jsonify({'error': str(e)}), 500

    return app


def run_server(host='127.0.0.1', port=5000, debug=False):
    """Run the Flask development server."""
    app = create_app()
    app.run(host=host, port=port, debug=debug)


if __name__ == '__main__':
    run_server(debug=True)
