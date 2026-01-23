"""Parser for Cursor AI session transcripts."""

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Iterator, Any
import hashlib
import os


@dataclass
class CursorMessage:
    """Represents a parsed message from a Cursor session."""
    uuid: str
    role: str  # 'user' or 'assistant'
    content: str
    timestamp: datetime
    raw_data: Optional[dict] = None


def decode_cursor_project_path(dir_name: str) -> str:
    """Convert encoded Cursor directory name to actual path.

    Cursor uses 'Users-foo-code-repo' format (no leading dash).

    Examples:
        'Users-foo-code-repo' -> '/Users/foo/code/repo'
        'home-user-projects-myapp' -> '/home/user/projects/myapp'
    """
    # Replace dashes with slashes and add leading slash
    parts = dir_name.split('-')
    return '/' + '/'.join(parts)


def extract_cursor_project_info(project_path: str) -> tuple[str, Optional[str]]:
    """Extract project name and optional org from path.

    Returns:
        Tuple of (name, org) where org may be None
    """
    path = Path(project_path)
    parts = path.parts

    # Get meaningful name from path
    # Try to find the last meaningful directory name
    skip_dirs = {'code', 'projects', 'src', 'repos', 'github', 'work', 'personal', 'dev', 'home', 'Users'}

    # Build name from path components after common prefixes
    name_parts = []
    for part in parts:
        if part not in skip_dirs and not part.startswith('.') and part != '/':
            name_parts.append(part)

    name = '-'.join(name_parts[-2:]) if len(name_parts) >= 2 else (name_parts[-1] if name_parts else path.name)

    # Try to extract org
    org = None
    if len(parts) >= 2:
        parent = parts[-2]
        if parent not in skip_dirs and not parent.startswith('.'):
            org = parent

    return name, org


def parse_cursor_txt_content(content: str, file_mtime: datetime) -> list[CursorMessage]:
    """Parse Cursor TXT transcript format.

    Cursor TXT format uses:
    - "user:" marker for user messages
    - "assistant:" marker for assistant messages (or sometimes "A:" in older formats)

    Complex conversations may have tool calls interspersed:
        user:
        <content>
        [Tool call] ...
        [Tool result] ...
        assistant:
        <content>

    Returns list of CursorMessage objects.
    """
    messages = []

    # Split on message boundaries - looking for "user:" or "assistant:" at line start
    # Also handle older "A:" format
    pattern = r'^(user:|assistant:|A:)\s*\n?'
    parts = re.split(pattern, content, flags=re.MULTILINE)

    # parts will be: ['', 'user:', '<content>', 'assistant:', '<content>', ...]
    # or sometimes just ['', 'user:', '<content>'] if no assistant response yet
    i = 1  # Skip empty first element
    message_index = 0

    while i < len(parts):
        role_marker = parts[i].strip().lower()
        content_text = parts[i + 1].strip() if i + 1 < len(parts) else ''

        if role_marker in ('user:', ):
            role = 'user'
        elif role_marker in ('assistant:', 'a:'):
            role = 'assistant'
        else:
            i += 2
            continue

        if content_text:
            # Clean up tool call markers from content
            # Keep the actual content but remove [Tool call], [Tool result] noise
            cleaned_content = content_text

            # For assistant messages, clean up thinking markers
            if role == 'assistant':
                # Remove [Thinking] markers
                cleaned_content = re.sub(r'\[Thinking\]\s*', '', cleaned_content)
                # Remove <think>...</think> blocks but keep inner content readable
                cleaned_content = re.sub(r'<think>\s*', '[Thinking] ', cleaned_content)
                cleaned_content = re.sub(r'\s*</think>', '\n', cleaned_content)

            # Generate a deterministic UUID based on content and position
            content_hash = hashlib.md5(f"{message_index}:{cleaned_content[:200]}".encode()).hexdigest()[:16]
            uuid = f"cursor-{content_hash}"

            messages.append(CursorMessage(
                uuid=uuid,
                role=role,
                content=cleaned_content,
                timestamp=file_mtime,
                raw_data=None
            ))
            message_index += 1

        i += 2

    return messages


def parse_cursor_json_content(content: str, file_mtime: datetime) -> list[CursorMessage]:
    """Parse Cursor JSON transcript format (older format).

    Format: JSON array of {role, text} objects.

    Returns list of CursorMessage objects.
    """
    messages = []

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return messages

    if not isinstance(data, list):
        return messages

    for i, item in enumerate(data):
        if not isinstance(item, dict):
            continue

        role = item.get('role', '').lower()
        text = item.get('text', '')

        if role not in ('user', 'assistant'):
            continue

        if not text.strip():
            continue

        # Generate UUID
        content_hash = hashlib.md5(f"{i}:{text[:200]}".encode()).hexdigest()[:16]
        uuid = f"cursor-{content_hash}"

        messages.append(CursorMessage(
            uuid=uuid,
            role=role,
            content=text,
            timestamp=file_mtime,
            raw_data=item
        ))

    return messages


def parse_cursor_session_file(file_path: Path) -> Iterator[CursorMessage]:
    """Parse a Cursor session file (TXT or JSON format).

    Args:
        file_path: Path to the transcript file

    Yields:
        CursorMessage objects
    """
    if not file_path.exists():
        return

    # Get file modification time as timestamp
    file_mtime = datetime.fromtimestamp(os.path.getmtime(file_path))

    content = file_path.read_text(encoding='utf-8', errors='replace')

    if file_path.suffix == '.json':
        messages = parse_cursor_json_content(content, file_mtime)
    else:  # .txt
        messages = parse_cursor_txt_content(content, file_mtime)

    for msg in messages:
        yield msg


def get_cursor_session_id_from_path(file_path: Path) -> str:
    """Extract session ID from Cursor transcript file path.

    The session ID is the filename without extension.
    Example: '5cf19002-dc02-4ba1-a452-6518ac9032bc.txt' -> '5cf19002-dc02-4ba1-a452-6518ac9032bc'
    """
    return file_path.stem


def get_cursor_project_path_from_file(file_path: Path) -> Optional[str]:
    """Extract project path from Cursor transcript file location.

    Cursor stores transcripts in ~/.cursor/projects/<encoded-path>/agent-transcripts/<session-id>.txt
    """
    parts = file_path.parts

    try:
        # Find 'projects' in path
        projects_idx = parts.index('projects')
        # The project dir is right after 'projects'
        if projects_idx + 1 < len(parts):
            encoded_path = parts[projects_idx + 1]
            return decode_cursor_project_path(encoded_path)
    except (ValueError, IndexError):
        pass

    return None


def is_cursor_transcript_file(file_path: Path) -> bool:
    """Check if a file is a Cursor transcript file."""
    # Must be in agent-transcripts directory
    if 'agent-transcripts' not in file_path.parts:
        return False

    # Must be .txt or .json
    return file_path.suffix in ('.txt', '.json')
