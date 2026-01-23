"""Tests for the JSONL parser."""

import json
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from claude_activity.parser import (
    decode_project_path,
    extract_project_info,
    extract_text_content,
    parse_timestamp,
    parse_message,
    parse_session_file,
    get_session_id_from_path,
    get_project_path_from_file,
)


class TestDecodeProjectPath:
    """Tests for decode_project_path function."""

    def test_basic_path(self):
        assert decode_project_path("-Users-foo-code-repo") == "/Users/foo/code/repo"

    def test_linux_path(self):
        assert decode_project_path("-home-user-projects-myapp") == "/home/user/projects/myapp"

    def test_single_component(self):
        assert decode_project_path("-Users") == "/Users"

    def test_no_leading_dash(self):
        # Should return unchanged if no leading dash
        assert decode_project_path("Users-foo") == "Users-foo"


class TestExtractProjectInfo:
    """Tests for extract_project_info function."""

    def test_simple_path(self):
        # With common prefix stripping, we keep path context after username
        name, org = extract_project_info("/Users/foo/code/myrepo")
        assert name == "code-myrepo"
        assert org is None  # 'code' is in skip list

    def test_org_path(self):
        name, org = extract_project_info("/Users/foo/acme/myrepo")
        assert name == "acme-myrepo"
        assert org == "acme"

    def test_github_style(self):
        name, org = extract_project_info("/Users/foo/mycompany/project")
        assert name == "mycompany-project"
        assert org == "mycompany"

    def test_deep_path(self):
        name, org = extract_project_info("/Users/foo/personal/killerwebapps/naha-webapp")
        assert name == "personal-killerwebapps-naha-webapp"
        assert org == "killerwebapps"


class TestExtractTextContent:
    """Tests for extract_text_content function."""

    def test_string_content(self):
        assert extract_text_content("Hello world") == "Hello world"

    def test_none_content(self):
        assert extract_text_content(None) is None

    def test_content_blocks(self):
        content = [
            {"type": "text", "text": "Hello"},
            {"type": "text", "text": "World"}
        ]
        assert extract_text_content(content) == "Hello\nWorld"

    def test_tool_use_block(self):
        content = [
            {"type": "text", "text": "Let me help"},
            {"type": "tool_use", "name": "read_file"}
        ]
        result = extract_text_content(content)
        assert "Let me help" in result
        assert "[Tool: read_file]" in result

    def test_dict_with_text(self):
        content = {"text": "Message text"}
        assert extract_text_content(content) == "Message text"


class TestParseTimestamp:
    """Tests for parse_timestamp function."""

    def test_datetime_passthrough(self):
        dt = datetime(2024, 1, 15, 10, 30, 0)
        assert parse_timestamp(dt) == dt

    def test_unix_seconds(self):
        ts = 1705312200  # 2024-01-15 10:30:00 UTC (approximately)
        result = parse_timestamp(ts)
        assert isinstance(result, datetime)

    def test_unix_milliseconds(self):
        ts = 1705312200000  # milliseconds
        result = parse_timestamp(ts)
        assert isinstance(result, datetime)

    def test_iso_string(self):
        ts = "2024-01-15T10:30:00"
        result = parse_timestamp(ts)
        assert result.year == 2024
        assert result.month == 1
        assert result.day == 15


class TestParseMessage:
    """Tests for parse_message function."""

    def test_user_message(self):
        line = json.dumps({
            "uuid": "test-uuid-123",
            "type": "user",
            "role": "user",
            "content": "Hello, Claude!",
            "timestamp": "2024-01-15T10:30:00"
        })
        msg = parse_message(line)
        assert msg is not None
        assert msg.uuid == "test-uuid-123"
        assert msg.type == "user"
        assert msg.role == "user"
        assert msg.content == "Hello, Claude!"

    def test_assistant_message_with_model(self):
        line = json.dumps({
            "uuid": "test-uuid-456",
            "type": "assistant",
            "role": "assistant",
            "content": "Hello! How can I help?",
            "model": "claude-sonnet-4-20250514",
            "timestamp": "2024-01-15T10:31:00",
            "usage": {"input_tokens": 10, "output_tokens": 20}
        })
        msg = parse_message(line)
        assert msg is not None
        assert msg.model == "claude-sonnet-4-20250514"
        assert msg.tokens_in == 10
        assert msg.tokens_out == 20

    def test_skip_ping(self):
        line = json.dumps({"type": "ping", "timestamp": "2024-01-15T10:30:00"})
        msg = parse_message(line)
        assert msg is None

    def test_invalid_json(self):
        msg = parse_message("not valid json")
        assert msg is None

    def test_empty_line(self):
        msg = parse_message("   ")
        assert msg is None


class TestParseSessionFile:
    """Tests for parse_session_file function."""

    def test_parse_file(self):
        messages = [
            {"uuid": "1", "type": "user", "content": "Hello", "timestamp": "2024-01-15T10:30:00"},
            {"uuid": "2", "type": "assistant", "content": "Hi there!", "timestamp": "2024-01-15T10:30:05"},
        ]

        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            for msg in messages:
                f.write(json.dumps(msg) + '\n')
            temp_path = Path(f.name)

        try:
            parsed = list(parse_session_file(temp_path))
            assert len(parsed) == 2
            assert parsed[0][0].uuid == "1"
            assert parsed[1][0].uuid == "2"
        finally:
            temp_path.unlink()

    def test_incremental_parse(self):
        messages = [
            {"uuid": "1", "type": "user", "content": "First", "timestamp": "2024-01-15T10:30:00"},
            {"uuid": "2", "type": "user", "content": "Second", "timestamp": "2024-01-15T10:31:00"},
        ]

        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            for msg in messages:
                f.write(json.dumps(msg) + '\n')
            temp_path = Path(f.name)

        try:
            # First parse - get all
            first_parse = list(parse_session_file(temp_path, 0))
            assert len(first_parse) == 2
            end_pos = first_parse[-1][1]

            # Add more messages
            with open(temp_path, 'a') as f:
                f.write(json.dumps({"uuid": "3", "type": "user", "content": "Third", "timestamp": "2024-01-15T10:32:00"}) + '\n')

            # Second parse - from last position
            second_parse = list(parse_session_file(temp_path, end_pos))
            assert len(second_parse) == 1
            assert second_parse[0][0].uuid == "3"
        finally:
            temp_path.unlink()


class TestGetSessionIdFromPath:
    """Tests for get_session_id_from_path function."""

    def test_uuid_extraction(self):
        path = Path("/home/user/.claude/projects/-Users-foo-code/5cf19002-dc02-4ba1-a452-6518ac9032bc.jsonl")
        assert get_session_id_from_path(path) == "5cf19002-dc02-4ba1-a452-6518ac9032bc"


class TestGetProjectPathFromFile:
    """Tests for get_project_path_from_file function."""

    def test_extract_project_path(self):
        path = Path("/Users/foo/.claude/projects/-Users-foo-code-myrepo/session.jsonl")
        result = get_project_path_from_file(path)
        assert result == "/Users/foo/code/myrepo"

    def test_no_projects_dir(self):
        path = Path("/Users/foo/random/path/session.jsonl")
        result = get_project_path_from_file(path)
        assert result is None
