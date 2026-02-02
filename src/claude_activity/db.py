"""SQLite database operations for Claude Activity Logger."""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Optional, Iterator, Any

from .config import Config, get_config
from .timestamps import utc_now, to_utc


# Custom adapters and converters for datetime handling (Python 3.12+ compatible)
def adapt_datetime(val: datetime) -> str:
    """Convert datetime to ISO format string for storage.

    All timestamps are stored in UTC. See timestamps.py for convention.
    If the datetime has timezone info, it's converted to UTC first.
    """
    # Convert to UTC (handles both timezone-aware and naive)
    val = to_utc(val)
    return val.strftime('%Y-%m-%d %H:%M:%S.%f') if val.microsecond else val.strftime('%Y-%m-%d %H:%M:%S')


def adapt_date(val: date) -> str:
    """Convert date to ISO format string for storage."""
    return val.isoformat()


def convert_datetime(val: bytes) -> datetime:
    """Convert stored timestamp back to datetime.

    All timestamps are stored in UTC (without timezone info).
    See timestamps.py for the timestamp convention.
    """
    try:
        text = val.decode('utf-8')
        # Handle various formats
        # Remove timezone suffix if present for parsing
        if '+' in text:
            text = text.split('+')[0]
        elif text.endswith('Z'):
            text = text[:-1]
        # Try parsing with microseconds first, then without
        for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S'):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        # Fallback: return current UTC time
        return utc_now()
    except Exception:
        return utc_now()


def convert_date(val: bytes) -> date:
    """Convert stored date back to date object."""
    try:
        text = val.decode('utf-8')
        return datetime.strptime(text, '%Y-%m-%d').date()
    except Exception:
        return date.today()


# Register adapters and converters
sqlite3.register_adapter(datetime, adapt_datetime)
sqlite3.register_adapter(date, adapt_date)
sqlite3.register_converter('TIMESTAMP', convert_datetime)
sqlite3.register_converter('DATE', convert_date)


SCHEMA = """
-- Projects/repositories
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    name TEXT,
    org TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Conversation sessions
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    session_id TEXT UNIQUE NOT NULL,
    project_id INTEGER REFERENCES projects(id),
    git_branch TEXT,
    started_at TIMESTAMP,
    ended_at TIMESTAMP,
    message_count INTEGER DEFAULT 0,
    source TEXT DEFAULT 'claude_code',  -- 'claude_code' or 'cursor'
    pending_question TEXT,  -- JSON with question data if awaiting user input
    pending_question_time TIMESTAMP  -- When the question was asked
);

-- Individual messages
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    session_id INTEGER REFERENCES sessions(id),
    uuid TEXT UNIQUE,
    type TEXT NOT NULL,
    role TEXT,
    content TEXT,
    model TEXT,
    timestamp TIMESTAMP NOT NULL,
    tokens_in INTEGER,
    tokens_out INTEGER
);

-- Summaries (daily, weekly, monthly)
CREATE TABLE IF NOT EXISTS summaries (
    id INTEGER PRIMARY KEY,
    project_id INTEGER REFERENCES projects(id),
    period_type TEXT NOT NULL,
    period_start DATE NOT NULL,
    period_end DATE NOT NULL,
    summary TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(project_id, period_type, period_start)
);

-- Track which files we've already processed
CREATE TABLE IF NOT EXISTS processed_files (
    file_path TEXT PRIMARY KEY,
    last_position INTEGER DEFAULT 0,
    last_modified TIMESTAMP
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_summaries_period ON summaries(period_type, period_start);
"""


class Database:
    """SQLite database wrapper for Claude activity data."""

    def __init__(self, config: Optional[Config] = None):
        self.config = config or get_config()
        self.db_path = self.config.database.path
        self._init_db()

    def _init_db(self):
        """Initialize database with schema."""
        self.config.ensure_directories()
        with self.connection() as conn:
            conn.executescript(SCHEMA)
            # Migration: add source column if it doesn't exist
            cursor = conn.execute("PRAGMA table_info(sessions)")
            columns = [row['name'] for row in cursor.fetchall()]
            if 'source' not in columns:
                conn.execute("ALTER TABLE sessions ADD COLUMN source TEXT DEFAULT 'claude_code'")
            # Migration: add pending_question columns if they don't exist
            if 'pending_question' not in columns:
                conn.execute("ALTER TABLE sessions ADD COLUMN pending_question TEXT")
            if 'pending_question_time' not in columns:
                conn.execute("ALTER TABLE sessions ADD COLUMN pending_question_time TIMESTAMP")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_pending ON sessions(pending_question_time)")

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Context manager for database connections."""
        conn = sqlite3.connect(self.db_path, detect_types=sqlite3.PARSE_DECLTYPES)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # Project operations
    def get_or_create_project(self, path: str, name: Optional[str] = None, org: Optional[str] = None) -> int:
        """Get existing project or create new one, returning project ID."""
        with self.connection() as conn:
            cursor = conn.execute("SELECT id FROM projects WHERE path = ?", (path,))
            row = cursor.fetchone()
            if row:
                return row["id"]

            cursor = conn.execute(
                "INSERT INTO projects (path, name, org) VALUES (?, ?, ?)",
                (path, name, org)
            )
            return cursor.lastrowid

    def get_project(self, project_id: int) -> Optional[dict]:
        """Get project by ID."""
        with self.connection() as conn:
            cursor = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_project_by_path(self, path: str) -> Optional[dict]:
        """Get project by path."""
        with self.connection() as conn:
            cursor = conn.execute("SELECT * FROM projects WHERE path = ?", (path,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def list_projects(self) -> list[dict]:
        """List all projects."""
        with self.connection() as conn:
            cursor = conn.execute("SELECT * FROM projects ORDER BY name")
            return [dict(row) for row in cursor.fetchall()]

    # Session operations
    def get_or_create_session(
        self,
        session_id: str,
        project_id: int,
        git_branch: Optional[str] = None,
        started_at: Optional[datetime] = None,
        source: str = 'claude_code'
    ) -> int:
        """Get existing session or create new one, returning session ID."""
        with self.connection() as conn:
            cursor = conn.execute("SELECT id FROM sessions WHERE session_id = ?", (session_id,))
            row = cursor.fetchone()
            if row:
                return row["id"]

            cursor = conn.execute(
                "INSERT INTO sessions (session_id, project_id, git_branch, started_at, source) VALUES (?, ?, ?, ?, ?)",
                (session_id, project_id, git_branch, started_at, source)
            )
            return cursor.lastrowid

    def update_session(
        self,
        session_id: str,
        started_at: Optional[datetime] = None,
        ended_at: Optional[datetime] = None,
        message_count: Optional[int] = None
    ):
        """Update session metadata."""
        with self.connection() as conn:
            updates = []
            params = []
            # Only update started_at if it's earlier than existing or not set
            if started_at is not None:
                updates.append("started_at = CASE WHEN started_at IS NULL OR started_at > ? THEN ? ELSE started_at END")
                params.extend([started_at, started_at])
            if ended_at is not None:
                updates.append("ended_at = ?")
                params.append(ended_at)
            if message_count is not None:
                updates.append("message_count = message_count + ?")
                params.append(message_count)

            if updates:
                params.append(session_id)
                conn.execute(
                    f"UPDATE sessions SET {', '.join(updates)} WHERE session_id = ?",
                    params
                )

    def update_session_pending_question(
        self,
        session_id: str,
        pending_question: Optional[str],
        pending_question_time: Optional[datetime]
    ):
        """Update session's pending question fields.

        Args:
            session_id: The session UUID
            pending_question: JSON string with question data, or None to clear
            pending_question_time: When the question was asked, or None to clear
        """
        with self.connection() as conn:
            conn.execute(
                "UPDATE sessions SET pending_question = ?, pending_question_time = ? WHERE session_id = ?",
                (pending_question, pending_question_time, session_id)
            )

    def get_session(self, session_id: str) -> Optional[dict]:
        """Get session by UUID."""
        with self.connection() as conn:
            cursor = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def list_sessions(
        self,
        project_id: Optional[int] = None,
        since: Optional[datetime] = None,
        limit: int = 50
    ) -> list[dict]:
        """List sessions with optional filtering."""
        with self.connection() as conn:
            query = "SELECT s.*, p.name as project_name, p.path as project_path FROM sessions s LEFT JOIN projects p ON s.project_id = p.id"
            conditions = []
            params = []

            if project_id is not None:
                conditions.append("s.project_id = ?")
                params.append(project_id)
            if since is not None:
                conditions.append("s.started_at >= ?")
                params.append(since)

            if conditions:
                query += " WHERE " + " AND ".join(conditions)

            query += " ORDER BY s.started_at DESC LIMIT ?"
            params.append(limit)

            cursor = conn.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

    # Message operations
    def insert_message(
        self,
        session_db_id: int,
        uuid: str,
        msg_type: str,
        role: Optional[str],
        content: Optional[str],
        model: Optional[str],
        timestamp: datetime,
        tokens_in: Optional[int] = None,
        tokens_out: Optional[int] = None
    ) -> Optional[int]:
        """Insert a message, returning message ID. Returns None if duplicate."""
        with self.connection() as conn:
            try:
                cursor = conn.execute(
                    """INSERT INTO messages
                       (session_id, uuid, type, role, content, model, timestamp, tokens_in, tokens_out)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (session_db_id, uuid, msg_type, role, content, model, timestamp, tokens_in, tokens_out)
                )
                return cursor.lastrowid
            except sqlite3.IntegrityError:
                # Duplicate UUID, skip
                return None

    def get_messages_for_session(self, session_db_id: int) -> list[dict]:
        """Get all messages for a session."""
        with self.connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY timestamp",
                (session_db_id,)
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_messages_in_range(
        self,
        start: datetime,
        end: datetime,
        project_id: Optional[int] = None
    ) -> list[dict]:
        """Get messages in a time range, optionally filtered by project."""
        with self.connection() as conn:
            if project_id is not None:
                cursor = conn.execute(
                    """SELECT m.*, s.session_id as session_uuid, p.name as project_name
                       FROM messages m
                       JOIN sessions s ON m.session_id = s.id
                       JOIN projects p ON s.project_id = p.id
                       WHERE m.timestamp >= ? AND m.timestamp < ? AND s.project_id = ?
                       ORDER BY m.timestamp""",
                    (start, end, project_id)
                )
            else:
                cursor = conn.execute(
                    """SELECT m.*, s.session_id as session_uuid, p.name as project_name
                       FROM messages m
                       JOIN sessions s ON m.session_id = s.id
                       LEFT JOIN projects p ON s.project_id = p.id
                       WHERE m.timestamp >= ? AND m.timestamp < ?
                       ORDER BY m.timestamp""",
                    (start, end)
                )
            return [dict(row) for row in cursor.fetchall()]

    # Processed files tracking
    def get_last_position(self, file_path: str) -> int:
        """Get last read position for a file."""
        with self.connection() as conn:
            cursor = conn.execute(
                "SELECT last_position FROM processed_files WHERE file_path = ?",
                (file_path,)
            )
            row = cursor.fetchone()
            return row["last_position"] if row else 0

    def update_position(self, file_path: str, position: int, modified: Optional[datetime] = None):
        """Update last read position for a file."""
        with self.connection() as conn:
            conn.execute(
                """INSERT INTO processed_files (file_path, last_position, last_modified)
                   VALUES (?, ?, ?)
                   ON CONFLICT(file_path) DO UPDATE SET
                   last_position = excluded.last_position,
                   last_modified = excluded.last_modified""",
                (file_path, position, modified or utc_now())
            )

    # Summary operations
    def save_summary(
        self,
        period_type: str,
        period_start: date,
        period_end: date,
        summary: str,
        project_id: Optional[int] = None
    ):
        """Save a summary, replacing if exists."""
        with self.connection() as conn:
            # Delete existing summary first (handles NULL project_id correctly)
            if project_id is not None:
                conn.execute(
                    "DELETE FROM summaries WHERE project_id = ? AND period_type = ? AND period_start = ?",
                    (project_id, period_type, period_start)
                )
            else:
                conn.execute(
                    "DELETE FROM summaries WHERE project_id IS NULL AND period_type = ? AND period_start = ?",
                    (period_type, period_start)
                )
            # Insert new summary
            conn.execute(
                """INSERT INTO summaries (project_id, period_type, period_start, period_end, summary)
                   VALUES (?, ?, ?, ?, ?)""",
                (project_id, period_type, period_start, period_end, summary)
            )

    def get_summary(
        self,
        period_type: str,
        period_start: date,
        project_id: Optional[int] = None
    ) -> Optional[dict]:
        """Get a specific summary."""
        with self.connection() as conn:
            if project_id is not None:
                cursor = conn.execute(
                    "SELECT * FROM summaries WHERE period_type = ? AND period_start = ? AND project_id = ?",
                    (period_type, period_start, project_id)
                )
            else:
                cursor = conn.execute(
                    "SELECT * FROM summaries WHERE period_type = ? AND period_start = ? AND project_id IS NULL",
                    (period_type, period_start)
                )
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_summaries_in_range(
        self,
        period_type: str,
        start: date,
        end: date,
        project_id: Optional[int] = None
    ) -> list[dict]:
        """Get summaries in a date range."""
        with self.connection() as conn:
            if project_id is not None:
                cursor = conn.execute(
                    """SELECT * FROM summaries
                       WHERE period_type = ? AND period_start >= ? AND period_start <= ? AND project_id = ?
                       ORDER BY period_start""",
                    (period_type, start, end, project_id)
                )
            else:
                cursor = conn.execute(
                    """SELECT * FROM summaries
                       WHERE period_type = ? AND period_start >= ? AND period_start <= ? AND project_id IS NULL
                       ORDER BY period_start""",
                    (period_type, start, end)
                )
            return [dict(row) for row in cursor.fetchall()]

    def get_unsummarized_days(self, project_id: Optional[int] = None) -> list[date]:
        """Get dates that have messages but no daily summary."""
        with self.connection() as conn:
            if project_id is not None:
                cursor = conn.execute(
                    """SELECT DISTINCT DATE(m.timestamp) as msg_date
                       FROM messages m
                       JOIN sessions s ON m.session_id = s.id
                       WHERE s.project_id = ?
                       AND DATE(m.timestamp) NOT IN (
                           SELECT period_start FROM summaries
                           WHERE period_type = 'daily' AND project_id = ?
                       )
                       AND DATE(m.timestamp) < DATE('now')
                       ORDER BY msg_date""",
                    (project_id, project_id)
                )
            else:
                cursor = conn.execute(
                    """SELECT DISTINCT DATE(m.timestamp) as msg_date
                       FROM messages m
                       WHERE DATE(m.timestamp) NOT IN (
                           SELECT period_start FROM summaries
                           WHERE period_type = 'daily' AND project_id IS NULL
                       )
                       AND DATE(m.timestamp) < DATE('now')
                       ORDER BY msg_date"""
                )
            return [datetime.strptime(row["msg_date"], "%Y-%m-%d").date() for row in cursor.fetchall()]

    # Statistics
    def get_stats(self, since: Optional[datetime] = None) -> dict:
        """Get overall statistics."""
        with self.connection() as conn:
            stats = {}

            if since:
                cursor = conn.execute("SELECT COUNT(*) as count FROM sessions WHERE started_at >= ?", (since,))
            else:
                cursor = conn.execute("SELECT COUNT(*) as count FROM sessions")
            stats["total_sessions"] = cursor.fetchone()["count"]

            if since:
                cursor = conn.execute(
                    "SELECT COUNT(*) as count FROM messages m JOIN sessions s ON m.session_id = s.id WHERE s.started_at >= ?",
                    (since,)
                )
            else:
                cursor = conn.execute("SELECT COUNT(*) as count FROM messages")
            stats["total_messages"] = cursor.fetchone()["count"]

            cursor = conn.execute("SELECT COUNT(*) as count FROM projects")
            stats["total_projects"] = cursor.fetchone()["count"]

            return stats
