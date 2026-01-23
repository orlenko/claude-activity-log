"""Tests for the database layer."""

import tempfile
from datetime import datetime, date, timedelta
from pathlib import Path

import pytest

from claude_activity.config import Config, DatabaseConfig, WatcherConfig, SummarizerConfig
from claude_activity.db import Database


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config = Config(
            database=DatabaseConfig(path=Path(tmpdir) / "test.db"),
            watcher=WatcherConfig(),
            summarizer=SummarizerConfig()
        )
        db = Database(config)
        yield db


class TestProjectOperations:
    """Tests for project CRUD operations."""

    def test_create_project(self, temp_db):
        project_id = temp_db.get_or_create_project("/Users/foo/code/myrepo", "myrepo", "foo")
        assert project_id is not None
        assert project_id > 0

    def test_get_existing_project(self, temp_db):
        id1 = temp_db.get_or_create_project("/Users/foo/code/myrepo", "myrepo")
        id2 = temp_db.get_or_create_project("/Users/foo/code/myrepo", "myrepo")
        assert id1 == id2

    def test_get_project(self, temp_db):
        project_id = temp_db.get_or_create_project("/Users/foo/code/myrepo", "myrepo", "acme")
        project = temp_db.get_project(project_id)
        assert project is not None
        assert project['path'] == "/Users/foo/code/myrepo"
        assert project['name'] == "myrepo"
        assert project['org'] == "acme"

    def test_get_project_by_path(self, temp_db):
        temp_db.get_or_create_project("/Users/foo/code/myrepo", "myrepo")
        project = temp_db.get_project_by_path("/Users/foo/code/myrepo")
        assert project is not None
        assert project['name'] == "myrepo"

    def test_list_projects(self, temp_db):
        temp_db.get_or_create_project("/path/a", "alpha")
        temp_db.get_or_create_project("/path/b", "beta")
        projects = temp_db.list_projects()
        assert len(projects) == 2


class TestSessionOperations:
    """Tests for session CRUD operations."""

    def test_create_session(self, temp_db):
        project_id = temp_db.get_or_create_project("/path/repo", "repo")
        session_id = temp_db.get_or_create_session("uuid-123", project_id, "main")
        assert session_id is not None
        assert session_id > 0

    def test_get_existing_session(self, temp_db):
        project_id = temp_db.get_or_create_project("/path/repo", "repo")
        id1 = temp_db.get_or_create_session("uuid-123", project_id)
        id2 = temp_db.get_or_create_session("uuid-123", project_id)
        assert id1 == id2

    def test_update_session(self, temp_db):
        project_id = temp_db.get_or_create_project("/path/repo", "repo")
        temp_db.get_or_create_session("uuid-123", project_id)

        now = datetime.now()
        temp_db.update_session("uuid-123", ended_at=now, message_count=10)

        session = temp_db.get_session("uuid-123")
        assert session['message_count'] == 10

    def test_list_sessions(self, temp_db):
        project_id = temp_db.get_or_create_project("/path/repo", "repo")
        temp_db.get_or_create_session("uuid-1", project_id)
        temp_db.get_or_create_session("uuid-2", project_id)

        sessions = temp_db.list_sessions()
        assert len(sessions) == 2

    def test_list_sessions_filter_by_project(self, temp_db):
        p1 = temp_db.get_or_create_project("/path/repo1", "repo1")
        p2 = temp_db.get_or_create_project("/path/repo2", "repo2")
        temp_db.get_or_create_session("uuid-1", p1)
        temp_db.get_or_create_session("uuid-2", p2)

        sessions = temp_db.list_sessions(project_id=p1)
        assert len(sessions) == 1
        assert sessions[0]['project_name'] == "repo1"


class TestMessageOperations:
    """Tests for message operations."""

    def test_insert_message(self, temp_db):
        project_id = temp_db.get_or_create_project("/path/repo", "repo")
        session_db_id = temp_db.get_or_create_session("uuid-123", project_id)

        msg_id = temp_db.insert_message(
            session_db_id=session_db_id,
            uuid="msg-uuid-1",
            msg_type="user",
            role="user",
            content="Hello!",
            model=None,
            timestamp=datetime.now()
        )
        assert msg_id is not None

    def test_duplicate_message_ignored(self, temp_db):
        project_id = temp_db.get_or_create_project("/path/repo", "repo")
        session_db_id = temp_db.get_or_create_session("uuid-123", project_id)
        now = datetime.now()

        id1 = temp_db.insert_message(
            session_db_id=session_db_id,
            uuid="msg-uuid-1",
            msg_type="user",
            role="user",
            content="Hello!",
            model=None,
            timestamp=now
        )
        id2 = temp_db.insert_message(
            session_db_id=session_db_id,
            uuid="msg-uuid-1",  # Same UUID
            msg_type="user",
            role="user",
            content="Hello again!",
            model=None,
            timestamp=now
        )

        assert id1 is not None
        assert id2 is None  # Duplicate should return None

    def test_get_messages_for_session(self, temp_db):
        project_id = temp_db.get_or_create_project("/path/repo", "repo")
        session_db_id = temp_db.get_or_create_session("uuid-123", project_id)
        now = datetime.now()

        temp_db.insert_message(session_db_id, "msg-1", "user", "user", "Hello", None, now)
        temp_db.insert_message(session_db_id, "msg-2", "assistant", "assistant", "Hi!", "claude", now)

        messages = temp_db.get_messages_for_session(session_db_id)
        assert len(messages) == 2

    def test_get_messages_in_range(self, temp_db):
        project_id = temp_db.get_or_create_project("/path/repo", "repo")
        session_db_id = temp_db.get_or_create_session("uuid-123", project_id)

        yesterday = datetime.now() - timedelta(days=1)
        today = datetime.now()

        temp_db.insert_message(session_db_id, "msg-old", "user", "user", "Old", None, yesterday)
        temp_db.insert_message(session_db_id, "msg-new", "user", "user", "New", None, today)

        start = datetime.now() - timedelta(hours=1)
        end = datetime.now() + timedelta(hours=1)
        messages = temp_db.get_messages_in_range(start, end)
        assert len(messages) == 1
        assert messages[0]['uuid'] == "msg-new"


class TestProcessedFilesTracking:
    """Tests for file position tracking."""

    def test_initial_position(self, temp_db):
        pos = temp_db.get_last_position("/some/file.jsonl")
        assert pos == 0

    def test_update_position(self, temp_db):
        temp_db.update_position("/some/file.jsonl", 1000)
        pos = temp_db.get_last_position("/some/file.jsonl")
        assert pos == 1000

    def test_update_position_multiple_times(self, temp_db):
        temp_db.update_position("/some/file.jsonl", 1000)
        temp_db.update_position("/some/file.jsonl", 2000)
        pos = temp_db.get_last_position("/some/file.jsonl")
        assert pos == 2000


class TestSummaryOperations:
    """Tests for summary operations."""

    def test_save_and_get_summary(self, temp_db):
        today = date.today()
        temp_db.save_summary(
            period_type='daily',
            period_start=today,
            period_end=today,
            summary="Test summary content"
        )

        summary = temp_db.get_summary('daily', today)
        assert summary is not None
        assert summary['summary'] == "Test summary content"

    def test_summary_upsert(self, temp_db):
        today = date.today()
        temp_db.save_summary('daily', today, today, "First version")
        temp_db.save_summary('daily', today, today, "Updated version")

        summary = temp_db.get_summary('daily', today)
        assert summary['summary'] == "Updated version"

    def test_project_specific_summary(self, temp_db):
        project_id = temp_db.get_or_create_project("/path/repo", "repo")
        today = date.today()

        temp_db.save_summary('daily', today, today, "Global summary")
        temp_db.save_summary('daily', today, today, "Project summary", project_id)

        global_summary = temp_db.get_summary('daily', today)
        project_summary = temp_db.get_summary('daily', today, project_id)

        assert global_summary['summary'] == "Global summary"
        assert project_summary['summary'] == "Project summary"

    def test_get_summaries_in_range(self, temp_db):
        d1 = date.today() - timedelta(days=2)
        d2 = date.today() - timedelta(days=1)
        d3 = date.today()

        temp_db.save_summary('daily', d1, d1, "Day 1")
        temp_db.save_summary('daily', d2, d2, "Day 2")
        temp_db.save_summary('daily', d3, d3, "Day 3")

        summaries = temp_db.get_summaries_in_range('daily', d1, d2)
        assert len(summaries) == 2


class TestStatistics:
    """Tests for statistics queries."""

    def test_get_stats(self, temp_db):
        project_id = temp_db.get_or_create_project("/path/repo", "repo")
        session_db_id = temp_db.get_or_create_session("uuid-123", project_id)
        temp_db.insert_message(session_db_id, "msg-1", "user", "user", "Hello", None, datetime.now())

        stats = temp_db.get_stats()
        assert stats['total_projects'] == 1
        assert stats['total_sessions'] == 1
        assert stats['total_messages'] == 1
