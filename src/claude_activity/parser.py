"""JSONL conversation parser for Claude Code sessions.

For timestamp handling conventions, see timestamps.py.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Iterator, Any

from .timestamps import parse_timestamp as ts_parse_timestamp, utc_now


# Cache for the common prefix (computed once per run)
_common_prefix_cache: Optional[str] = None


@dataclass
class ParsedMessage:
    """Represents a parsed message from a Claude session."""
    uuid: str
    type: str
    role: Optional[str]
    content: Optional[str]
    model: Optional[str]
    timestamp: datetime
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    cwd: Optional[str] = None  # Working directory from message
    git_branch: Optional[str] = None  # Git branch from message
    raw_data: Optional[dict] = None


def decode_project_path(dir_name: str) -> str:
    """Convert encoded directory name to actual path.

    Examples:
        '-Users-foo-code-repo' -> '/Users/foo/code/repo'
        '-home-user-projects-myapp' -> '/home/user/projects/myapp'
    """
    if not dir_name.startswith('-'):
        return dir_name
    # Replace first dash with /, then all remaining dashes with /
    # But we need to handle multi-dash sequences and edge cases
    parts = dir_name[1:].split('-')
    return '/' + '/'.join(parts)


def get_common_project_prefix(claude_projects_dir: Optional[Path] = None) -> str:
    """Find the common prefix across all project directories.

    Scans ~/.claude/projects/ to find what prefix all project dirs share
    (typically '-Users-username-') and returns it for stripping.

    Returns:
        The common prefix string (e.g., '-Users-vorlenko-')
    """
    global _common_prefix_cache

    if _common_prefix_cache is not None:
        return _common_prefix_cache

    if claude_projects_dir is None:
        claude_projects_dir = Path.home() / ".claude" / "projects"

    if not claude_projects_dir.exists():
        _common_prefix_cache = ""
        return ""

    # Get all project directory names
    dir_names = [d.name for d in claude_projects_dir.iterdir() if d.is_dir() and d.name.startswith('-')]

    if not dir_names:
        _common_prefix_cache = ""
        return ""

    if len(dir_names) == 1:
        # With only one project, strip up to and including username
        # e.g., '-Users-vorlenko-code-repo' -> find '-Users-vorlenko-'
        parts = dir_names[0].split('-')
        # parts = ['', 'Users', 'vorlenko', 'code', 'repo']
        # We want to keep everything after the username (index 3+)
        if len(parts) >= 3:
            _common_prefix_cache = '-'.join(parts[:3]) + '-'  # '-Users-vorlenko-'
        else:
            _common_prefix_cache = ""
        return _common_prefix_cache

    # Find longest common prefix
    prefix = dir_names[0]
    for name in dir_names[1:]:
        while not name.startswith(prefix):
            # Remove last character
            prefix = prefix[:-1]
            if not prefix:
                break

    # Ensure prefix ends at a dash boundary for clean splits
    if prefix and not prefix.endswith('-'):
        last_dash = prefix.rfind('-')
        if last_dash > 0:
            prefix = prefix[:last_dash + 1]
        else:
            prefix = ""

    _common_prefix_cache = prefix
    return prefix


def extract_project_name_from_dir(dir_name: str, claude_projects_dir: Optional[Path] = None) -> str:
    """Extract meaningful project name from encoded directory name.

    Strips the common prefix (e.g., '-Users-vorlenko-') and returns the rest.

    Examples:
        '-Users-vorlenko-code-ops' -> 'code-ops'
        '-Users-vorlenko-personal-claude-activity-log' -> 'personal-claude-activity-log'
    """
    prefix = get_common_project_prefix(claude_projects_dir)

    if prefix and dir_name.startswith(prefix):
        return dir_name[len(prefix):]

    # Fallback: return everything after the first three components
    # (typically '', 'Users', 'username')
    parts = dir_name.split('-')
    if len(parts) > 3:
        return '-'.join(parts[3:])

    return dir_name.lstrip('-')


def extract_project_info(project_path: str, claude_projects_dir: Optional[Path] = None) -> tuple[str, Optional[str]]:
    """Extract project name and optional org from path.

    Uses the common prefix across all projects to derive a meaningful name.

    Returns:
        Tuple of (name, org) where org may be None
    """
    path = Path(project_path)

    # Get the encoded directory name format for extracting the name
    # Convert path like /Users/vorlenko/code/ops to -Users-vorlenko-code-ops
    encoded = '-' + '-'.join(path.parts[1:])  # Skip leading '/'

    # Extract meaningful name by stripping common prefix
    name = extract_project_name_from_dir(encoded, claude_projects_dir)

    # Try to extract org from path patterns
    org = None
    parts = path.parts

    # Check for patterns like .../org/repo or .../username/repo
    if len(parts) >= 2:
        parent = parts[-2]
        # Common code directories to skip as org
        skip_dirs = {'code', 'projects', 'src', 'repos', 'github', 'work', 'personal', 'dev', 'home', 'Users'}
        if parent not in skip_dirs and not parent.startswith('.'):
            org = parent

    return name, org


def extract_text_content(content: Any) -> Optional[str]:
    """Extract text content from various message content formats."""
    if content is None:
        return None

    if isinstance(content, str):
        return content

    if isinstance(content, list):
        # Handle content blocks format
        text_parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get('type') == 'text':
                    text_parts.append(block.get('text', ''))
                elif block.get('type') == 'tool_use':
                    # Summarize tool use
                    tool_name = block.get('name', 'unknown')
                    text_parts.append(f"[Tool: {tool_name}]")
                elif block.get('type') == 'tool_result':
                    text_parts.append("[Tool Result]")
            elif isinstance(block, str):
                text_parts.append(block)
        return '\n'.join(text_parts) if text_parts else None

    if isinstance(content, dict):
        if 'text' in content:
            return content['text']
        if 'message' in content:
            return extract_text_content(content['message'])

    return str(content)


def parse_timestamp(ts: Any) -> datetime:
    """Parse timestamp from various formats.

    Delegates to the centralized parse_timestamp in timestamps.py.
    See timestamps.py for full documentation of the timestamp convention.
    """
    return ts_parse_timestamp(ts)


def parse_message(line: str) -> Optional[ParsedMessage]:
    """Parse a single JSONL line into a ParsedMessage."""
    try:
        data = json.loads(line.strip())
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict):
        return None

    # Extract message type
    msg_type = data.get('type', 'unknown')

    # Skip certain types that aren't useful for logging
    if msg_type in ('ping', 'heartbeat'):
        return None

    # Extract UUID (try various field names)
    uuid = data.get('uuid') or data.get('id') or data.get('message_id')
    if not uuid:
        # Generate a pseudo-UUID from timestamp and hash
        import hashlib
        content_hash = hashlib.md5(line.encode()).hexdigest()[:12]
        uuid = f"gen-{content_hash}"

    # Extract role
    role = data.get('role')
    if not role and msg_type == 'user':
        role = 'user'
    elif not role and msg_type == 'assistant':
        role = 'assistant'

    # Extract content
    content = None
    if 'content' in data:
        content = extract_text_content(data['content'])
    elif 'message' in data:
        msg = data['message']
        if isinstance(msg, dict) and 'content' in msg:
            content = extract_text_content(msg['content'])
            role = role or msg.get('role')
        elif isinstance(msg, str):
            content = msg
    elif 'text' in data:
        content = data['text']

    # Extract timestamp
    timestamp = parse_timestamp(
        data.get('timestamp') or
        data.get('created_at') or
        data.get('time') or
        utc_now()
    )

    # Extract model
    model = data.get('model')
    if not model and 'message' in data and isinstance(data['message'], dict):
        model = data['message'].get('model')

    # Extract token usage
    tokens_in = None
    tokens_out = None
    usage = data.get('usage') or (data.get('message', {}) or {}).get('usage')
    if usage and isinstance(usage, dict):
        tokens_in = usage.get('input_tokens')
        tokens_out = usage.get('output_tokens')

    # Extract cwd and git_branch (available in most messages)
    cwd = data.get('cwd')
    git_branch = data.get('gitBranch')

    return ParsedMessage(
        uuid=uuid,
        type=msg_type,
        role=role,
        content=content,
        model=model,
        timestamp=timestamp,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cwd=cwd,
        git_branch=git_branch,
        raw_data=data
    )


def parse_session_file(
    file_path: Path,
    start_position: int = 0
) -> Iterator[tuple[ParsedMessage, int]]:
    """Parse a session JSONL file, yielding messages and their end positions.

    Args:
        file_path: Path to the JSONL file
        start_position: Byte position to start reading from

    Yields:
        Tuples of (ParsedMessage, end_position)
    """
    with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
        f.seek(start_position)

        while True:
            line = f.readline()
            if not line:
                break

            end_pos = f.tell()

            if not line.strip():
                continue

            message = parse_message(line)
            if message:
                yield message, end_pos


def get_session_id_from_path(file_path: Path) -> str:
    """Extract session ID from file path.

    The session ID is typically the filename without extension.
    Example: '5cf19002-dc02-4ba1-a452-6518ac9032bc.jsonl' -> '5cf19002-dc02-4ba1-a452-6518ac9032bc'
    """
    return file_path.stem


def get_project_path_from_file(file_path: Path) -> Optional[str]:
    """Extract project path from session file location.

    Claude stores sessions in ~/.claude/projects/<encoded-path>/<session-id>.jsonl
    """
    parts = file_path.parts

    try:
        # Find 'projects' in path
        projects_idx = parts.index('projects')
        if projects_idx + 1 < len(parts) - 1:  # Ensure there's a dir after 'projects' and before filename
            encoded_path = parts[projects_idx + 1]
            return decode_project_path(encoded_path)
    except (ValueError, IndexError):
        pass

    return None


def extract_pending_question(messages: list[dict]) -> Optional[dict]:
    """Check if session has an unanswered AskUserQuestion.

    Looks for the last assistant message containing an AskUserQuestion tool_use block
    and checks if there's a subsequent user message with a matching tool_result.

    Args:
        messages: List of message dicts from the database (with 'role', 'content', 'timestamp')

    Returns:
        Dict with question data if pending, None if answered or no question.
        Format: {
            "tool_use_id": "toolu_...",
            "question": "What tech stack do you prefer?",
            "header": "Tech Stack",
            "timestamp": datetime
        }
    """
    if not messages:
        return None

    # Find the last assistant message with AskUserQuestion tool_use
    last_question = None
    last_question_idx = -1

    for i, msg in enumerate(messages):
        if msg.get('role') != 'assistant':
            continue

        content = msg.get('content', '')
        if not content:
            continue

        # Parse content to find tool_use blocks
        # Content may be stored as text with [Tool: ...] markers or as raw JSON
        raw_data = msg.get('raw_data')
        if raw_data and isinstance(raw_data, dict):
            msg_content = raw_data.get('message', {}).get('content', [])
            if isinstance(msg_content, list):
                for block in msg_content:
                    if isinstance(block, dict) and block.get('type') == 'tool_use' and block.get('name') == 'AskUserQuestion':
                        tool_input = block.get('input', {})
                        questions = tool_input.get('questions', [])
                        if questions:
                            first_q = questions[0]
                            last_question = {
                                'tool_use_id': block.get('id'),
                                'question': first_q.get('question', ''),
                                'header': first_q.get('header', ''),
                                'timestamp': msg.get('timestamp')
                            }
                            last_question_idx = i

    if not last_question:
        return None

    # Check if there's a subsequent user message with a matching tool_result
    for msg in messages[last_question_idx + 1:]:
        if msg.get('role') != 'user':
            continue

        raw_data = msg.get('raw_data')
        if raw_data and isinstance(raw_data, dict):
            msg_content = raw_data.get('message', {}).get('content', [])
            if isinstance(msg_content, list):
                for block in msg_content:
                    if isinstance(block, dict) and block.get('type') == 'tool_result':
                        if block.get('tool_use_id') == last_question['tool_use_id']:
                            # Question has been answered
                            return None

    # Question is still pending
    return last_question


def extract_pending_question_from_raw_messages(raw_messages: list[dict]) -> Optional[dict]:
    """Check if session has a pending tool_use awaiting user input.

    Detects two types of pending input:
    1. AskUserQuestion - explicit questions to the user
    2. Any tool_use without a matching tool_result - waiting for permission/approval

    Args:
        raw_messages: List of raw message dicts from JSONL parsing

    Returns:
        Dict with pending input data if found, None otherwise.
        Format: {
            "tool_use_id": "toolu_...",
            "tool_name": "Bash" or "AskUserQuestion",
            "question": "What tech stack?" (for AskUserQuestion) or None,
            "header": "Tech Stack" (for AskUserQuestion) or None,
            "timestamp": datetime
        }
    """
    if not raw_messages:
        return None

    # Collect all tool_use blocks and their indices
    tool_uses = []  # [(index, tool_use_id, tool_name, question_data, timestamp), ...]

    for i, data in enumerate(raw_messages):
        if not isinstance(data, dict):
            continue

        # Check if this is an assistant message
        role = data.get('role')
        msg = data.get('message', {})
        if isinstance(msg, dict):
            role = role or msg.get('role')
            content = msg.get('content', [])
        else:
            content = data.get('content', [])

        if role != 'assistant':
            continue

        if not isinstance(content, list):
            continue

        # Collect all tool_use blocks from this message
        timestamp = data.get('timestamp') or data.get('created_at')
        for block in content:
            if isinstance(block, dict) and block.get('type') == 'tool_use':
                tool_name = block.get('name', 'Unknown')
                tool_use_id = block.get('id')

                # Extract question data if it's AskUserQuestion
                question_data = None
                if tool_name == 'AskUserQuestion':
                    tool_input = block.get('input', {})
                    questions = tool_input.get('questions', [])
                    if questions:
                        first_q = questions[0]
                        question_data = {
                            'question': first_q.get('question', ''),
                            'header': first_q.get('header', '')
                        }

                tool_uses.append((i, tool_use_id, tool_name, question_data, timestamp))

    if not tool_uses:
        return None

    # Collect all tool_result IDs
    answered_tool_ids = set()
    for data in raw_messages:
        if not isinstance(data, dict):
            continue

        role = data.get('role')
        msg = data.get('message', {})
        if isinstance(msg, dict):
            role = role or msg.get('role')
            content = msg.get('content', [])
        else:
            content = data.get('content', [])

        if role != 'user':
            continue

        if not isinstance(content, list):
            continue

        for block in content:
            if isinstance(block, dict) and block.get('type') == 'tool_result':
                answered_tool_ids.add(block.get('tool_use_id'))

    # Find the last unanswered tool_use
    for idx, tool_use_id, tool_name, question_data, timestamp in reversed(tool_uses):
        if tool_use_id not in answered_tool_ids:
            result = {
                'tool_use_id': tool_use_id,
                'tool_name': tool_name,
                'timestamp': parse_timestamp(timestamp) if timestamp else None
            }
            if question_data:
                result['question'] = question_data['question']
                result['header'] = question_data['header']
            return result

    # All tool_uses have been answered
    return None
