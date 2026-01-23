"""Configuration management for Claude Activity Logger."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import tomllib
except ImportError:
    import tomli as tomllib


DEFAULT_CONFIG = {
    "database": {
        "path": "~/.claude-activity/activity.db",
    },
    "watcher": {
        "claude_dir": "~/.claude",
        "cursor_dir": "~/.cursor",
        "poll_interval": 1.0,
    },
    "summarizer": {
        "model": "claude-3-5-haiku-latest",  # Use Haiku for cost efficiency
        "auto_summarize": True,
        "summarize_hour": 23,
    },
}


@dataclass
class DatabaseConfig:
    path: Path = field(default_factory=lambda: Path.home() / ".claude-activity" / "activity.db")

    def __post_init__(self):
        if isinstance(self.path, str):
            self.path = Path(self.path).expanduser()


@dataclass
class WatcherConfig:
    claude_dir: Path = field(default_factory=lambda: Path.home() / ".claude")
    cursor_dir: Path = field(default_factory=lambda: Path.home() / ".cursor")
    poll_interval: float = 1.0

    def __post_init__(self):
        if isinstance(self.claude_dir, str):
            self.claude_dir = Path(self.claude_dir).expanduser()
        if isinstance(self.cursor_dir, str):
            self.cursor_dir = Path(self.cursor_dir).expanduser()


@dataclass
class SummarizerConfig:
    model: str = "claude-3-5-haiku-latest"  # Haiku for cost efficiency
    auto_summarize: bool = True
    summarize_hour: int = 23


@dataclass
class Config:
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    watcher: WatcherConfig = field(default_factory=WatcherConfig)
    summarizer: SummarizerConfig = field(default_factory=SummarizerConfig)

    @classmethod
    def load(cls, config_path: Optional[Path] = None) -> "Config":
        """Load configuration from file, falling back to defaults."""
        if config_path is None:
            config_path = Path.home() / ".config" / "claude-activity" / "config.toml"

        config_data = DEFAULT_CONFIG.copy()

        if config_path.exists():
            with open(config_path, "rb") as f:
                user_config = tomllib.load(f)
                # Merge user config with defaults
                for section, values in user_config.items():
                    if section in config_data:
                        config_data[section].update(values)
                    else:
                        config_data[section] = values

        return cls(
            database=DatabaseConfig(path=config_data["database"]["path"]),
            watcher=WatcherConfig(
                claude_dir=config_data["watcher"]["claude_dir"],
                cursor_dir=config_data["watcher"].get("cursor_dir", "~/.cursor"),
                poll_interval=config_data["watcher"]["poll_interval"],
            ),
            summarizer=SummarizerConfig(
                model=config_data["summarizer"]["model"],
                auto_summarize=config_data["summarizer"]["auto_summarize"],
                summarize_hour=config_data["summarizer"]["summarize_hour"],
            ),
        )

    def ensure_directories(self):
        """Create necessary directories if they don't exist."""
        self.database.path.parent.mkdir(parents=True, exist_ok=True)


def get_config() -> Config:
    """Get the global configuration instance."""
    return Config.load()
