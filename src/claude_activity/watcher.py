"""File watcher daemon for Claude session files."""

import logging
import signal
import sys
import time
from pathlib import Path
from threading import Event, Thread
from typing import Optional
import os

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileModifiedEvent, FileCreatedEvent

from .config import Config, get_config
from .db import Database
from .parser import (
    parse_session_file,
    get_session_id_from_path,
    get_project_path_from_file,
    extract_project_info
)


logger = logging.getLogger(__name__)


class SessionFileHandler(FileSystemEventHandler):
    """Handle changes to Claude session JSONL files."""

    def __init__(self, db: Database, config: Config):
        super().__init__()
        self.db = db
        self.config = config
        self._processing = set()

    def on_created(self, event):
        if not event.is_directory and event.src_path.endswith('.jsonl'):
            self._process_file(Path(event.src_path))

    def on_modified(self, event):
        if not event.is_directory and event.src_path.endswith('.jsonl'):
            self._process_file(Path(event.src_path))

    def _process_file(self, file_path: Path):
        """Process a session file, reading only new content."""
        path_str = str(file_path)

        # Avoid concurrent processing of same file
        if path_str in self._processing:
            return

        self._processing.add(path_str)
        try:
            self._do_process(file_path)
        except Exception as e:
            logger.error(f"Error processing {file_path}: {e}")
        finally:
            self._processing.discard(path_str)

    def _do_process(self, file_path: Path):
        """Actually process the file."""
        if not file_path.exists():
            return

        # Get or create project
        project_path = get_project_path_from_file(file_path)
        if not project_path:
            logger.warning(f"Could not determine project path for {file_path}")
            return

        name, org = extract_project_info(project_path)
        project_id = self.db.get_or_create_project(project_path, name, org)

        # Get or create session
        session_uuid = get_session_id_from_path(file_path)
        session_db_id = self.db.get_or_create_session(session_uuid, project_id)

        # Get last read position
        last_pos = self.db.get_last_position(str(file_path))

        # Parse new messages
        message_count = 0
        first_timestamp = None
        last_timestamp = None
        final_pos = last_pos

        for message, end_pos in parse_session_file(file_path, last_pos):
            result = self.db.insert_message(
                session_db_id=session_db_id,
                uuid=message.uuid,
                msg_type=message.type,
                role=message.role,
                content=message.content,
                model=message.model,
                timestamp=message.timestamp,
                tokens_in=message.tokens_in,
                tokens_out=message.tokens_out
            )
            if result is not None:
                message_count += 1
                # Only use messages with role for session timing (skip system messages)
                if message.role in ('user', 'assistant'):
                    if first_timestamp is None:
                        first_timestamp = message.timestamp
                    last_timestamp = message.timestamp
            final_pos = end_pos

        # Update position tracker
        if final_pos > last_pos:
            self.db.update_position(str(file_path), final_pos)

        # Update session metadata
        if message_count > 0:
            self.db.update_session(
                session_uuid,
                started_at=first_timestamp,
                ended_at=last_timestamp,
                message_count=message_count
            )
            logger.info(f"Processed {message_count} new messages from {file_path.name}")


class Watcher:
    """File watcher daemon."""

    def __init__(self, config: Optional[Config] = None):
        self.config = config or get_config()
        self.db = Database(self.config)
        self.observer: Optional[Observer] = None
        self._stop_event = Event()

    def start(self, blocking: bool = True):
        """Start the file watcher.

        Args:
            blocking: If True, run until stopped. If False, start in background.
        """
        watch_path = self.config.watcher.claude_dir / "projects"

        if not watch_path.exists():
            logger.warning(f"Claude projects directory does not exist: {watch_path}")
            watch_path.mkdir(parents=True, exist_ok=True)

        handler = SessionFileHandler(self.db, self.config)
        self.observer = Observer()
        self.observer.schedule(handler, str(watch_path), recursive=True)
        self.observer.start()

        logger.info(f"Started watching {watch_path}")

        # Process existing files on startup
        self._process_existing_files(watch_path, handler)

        if blocking:
            self._run_until_stopped()

    def _process_existing_files(self, watch_path: Path, handler: SessionFileHandler):
        """Process any existing session files on startup."""
        for jsonl_file in watch_path.rglob("*.jsonl"):
            try:
                handler._process_file(jsonl_file)
            except Exception as e:
                logger.error(f"Error processing existing file {jsonl_file}: {e}")

    def _run_until_stopped(self):
        """Run the watcher until stop signal received."""
        # Set up signal handlers
        def signal_handler(signum, frame):
            logger.info("Received stop signal")
            self.stop()

        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)

        try:
            while not self._stop_event.is_set():
                self._stop_event.wait(timeout=1.0)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self):
        """Stop the file watcher."""
        self._stop_event.set()
        if self.observer:
            self.observer.stop()
            self.observer.join(timeout=5)
            self.observer = None
        logger.info("Watcher stopped")

    def is_running(self) -> bool:
        """Check if watcher is running."""
        return self.observer is not None and self.observer.is_alive()


def get_pid_file() -> Path:
    """Get path to PID file."""
    return Path.home() / ".claude-activity" / "watcher.pid"


def write_pid_file():
    """Write current PID to file."""
    pid_file = get_pid_file()
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))


def read_pid_file() -> Optional[int]:
    """Read PID from file."""
    pid_file = get_pid_file()
    if pid_file.exists():
        try:
            return int(pid_file.read_text().strip())
        except (ValueError, OSError):
            pass
    return None


def remove_pid_file():
    """Remove PID file."""
    pid_file = get_pid_file()
    if pid_file.exists():
        pid_file.unlink()


def is_process_running(pid: int) -> bool:
    """Check if a process is running."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def run_daemon():
    """Run the watcher as a daemon process."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(Path.home() / ".claude-activity" / "watcher.log"),
            logging.StreamHandler()
        ]
    )

    write_pid_file()
    try:
        watcher = Watcher()
        watcher.start(blocking=True)
    finally:
        remove_pid_file()


if __name__ == "__main__":
    run_daemon()
