"""Tests for the summarizer module."""

import tempfile
from datetime import datetime, date, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from claude_activity.config import Config, DatabaseConfig, WatcherConfig, SummarizerConfig
from claude_activity.db import Database
from claude_activity.summarizer import Summarizer


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
        yield db, config


@pytest.fixture
def mock_anthropic():
    """Mock the Anthropic client."""
    with patch('claude_activity.summarizer.Anthropic') as mock:
        client_instance = Mock()
        mock.return_value = client_instance

        # Mock the messages.create response
        response = Mock()
        response.content = [Mock(text="## Accomplishments\n- Did stuff\n\n## Key Decisions\n- Made choices")]
        client_instance.messages.create.return_value = response

        yield client_instance


class TestSummarizer:
    """Tests for Summarizer class."""

    def test_format_messages_for_summary(self, temp_db, mock_anthropic):
        db, config = temp_db
        summarizer = Summarizer(config, db)

        messages = [
            {'role': 'user', 'content': 'Hello', 'project_name': 'test-project'},
            {'role': 'assistant', 'content': 'Hi there!', 'project_name': 'test-project'},
        ]

        result = summarizer._format_messages_for_summary(messages)
        # New format groups by project and only includes user messages
        assert "## Project: test-project" in result
        assert "- Hello" in result
        # Assistant messages should NOT be included (saves tokens)
        assert "Hi there!" not in result

    def test_format_messages_truncates_long_content(self, temp_db, mock_anthropic):
        db, config = temp_db
        summarizer = Summarizer(config, db)

        long_content = "x" * 500
        messages = [
            {'role': 'user', 'content': long_content, 'project_name': 'test'},
        ]

        result = summarizer._format_messages_for_summary(messages)
        assert "..." in result
        assert len(result) < 500  # Should be truncated to ~300 chars

    def test_format_empty_messages(self, temp_db, mock_anthropic):
        db, config = temp_db
        summarizer = Summarizer(config, db)

        result = summarizer._format_messages_for_summary([])
        assert result == "No conversations recorded."

    def test_generate_daily_summary(self, temp_db, mock_anthropic):
        db, config = temp_db

        # Add some test data
        project_id = db.get_or_create_project("/path/repo", "repo")
        session_db_id = db.get_or_create_session("uuid-123", project_id)

        yesterday = date.today() - timedelta(days=1)
        timestamp = datetime.combine(yesterday, datetime.min.time().replace(hour=10))

        db.insert_message(session_db_id, "msg-1", "user", "user", "Hello", None, timestamp)
        db.insert_message(session_db_id, "msg-2", "assistant", "assistant", "Hi!", "claude", timestamp)

        summarizer = Summarizer(config, db)
        summary = summarizer.generate_daily_summary(yesterday)

        assert summary is not None
        assert "Accomplishments" in summary

        # Verify it was saved
        saved = db.get_summary('daily', yesterday)
        assert saved is not None

    def test_daily_summary_uses_cache(self, temp_db, mock_anthropic):
        db, config = temp_db

        yesterday = date.today() - timedelta(days=1)
        db.save_summary('daily', yesterday, yesterday, "Cached summary")

        summarizer = Summarizer(config, db)
        summary = summarizer.generate_daily_summary(yesterday)

        assert summary == "Cached summary"
        # API should not have been called
        mock_anthropic.messages.create.assert_not_called()

    def test_daily_summary_force_regenerate(self, temp_db, mock_anthropic):
        db, config = temp_db

        # Add test data
        project_id = db.get_or_create_project("/path/repo", "repo")
        session_db_id = db.get_or_create_session("uuid-123", project_id)

        yesterday = date.today() - timedelta(days=1)
        timestamp = datetime.combine(yesterday, datetime.min.time().replace(hour=10))
        db.insert_message(session_db_id, "msg-1", "user", "user", "Hello", None, timestamp)

        # Save existing summary
        db.save_summary('daily', yesterday, yesterday, "Old summary")

        summarizer = Summarizer(config, db)
        summary = summarizer.generate_daily_summary(yesterday, force=True)

        # Should have called API
        mock_anthropic.messages.create.assert_called_once()
        assert "Accomplishments" in summary

    def test_generate_weekly_summary(self, temp_db, mock_anthropic):
        db, config = temp_db

        # Find last Monday
        today = date.today()
        last_monday = today - timedelta(days=today.weekday() + 7)

        # Add daily summaries for the week
        for i in range(5):
            day = last_monday + timedelta(days=i)
            db.save_summary('daily', day, day, f"Day {i} summary")

        summarizer = Summarizer(config, db)
        summary = summarizer.generate_weekly_summary(last_monday)

        assert summary is not None
        mock_anthropic.messages.create.assert_called()

    def test_summarize_unsummarized(self, temp_db, mock_anthropic):
        db, config = temp_db

        # Add messages from 2 days ago
        project_id = db.get_or_create_project("/path/repo", "repo")
        session_db_id = db.get_or_create_session("uuid-123", project_id)

        two_days_ago = date.today() - timedelta(days=2)
        timestamp = datetime.combine(two_days_ago, datetime.min.time().replace(hour=10))
        db.insert_message(session_db_id, "msg-1", "user", "user", "Hello", None, timestamp)

        summarizer = Summarizer(config, db)
        results = summarizer.summarize_unsummarized()

        assert results['daily'] >= 1


class TestPromptFormatting:
    """Tests for prompt formatting."""

    def test_daily_prompt_includes_date(self, temp_db, mock_anthropic):
        db, config = temp_db

        project_id = db.get_or_create_project("/path/repo", "repo")
        session_db_id = db.get_or_create_session("uuid-123", project_id)

        yesterday = date.today() - timedelta(days=1)
        timestamp = datetime.combine(yesterday, datetime.min.time().replace(hour=10))
        db.insert_message(session_db_id, "msg-1", "user", "user", "Test message", None, timestamp)

        summarizer = Summarizer(config, db)
        summarizer.generate_daily_summary(yesterday)

        # Check the prompt that was sent
        call_args = mock_anthropic.messages.create.call_args
        prompt = call_args.kwargs['messages'][0]['content']

        assert yesterday.strftime("%Y-%m-%d") in prompt
        assert "Test message" in prompt
