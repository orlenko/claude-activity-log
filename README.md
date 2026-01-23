# Claude Activity Logger

A background daemon that monitors all your Claude Code sessions, logs conversations to a SQLite database, and provides daily/weekly/monthly AI-powered summaries.

## Features

- **Background Monitoring**: Watches `~/.claude/projects/` for new session files and logs all conversations automatically
- **SQLite Storage**: Efficiently stores messages with full metadata (timestamps, tokens, git branch, etc.)
- **AI Summaries**: Generate daily, weekly, and monthly summaries using Claude API (Haiku model for cost efficiency)
- **Rich CLI**: Beautiful terminal output with colored tables and formatted panels
- **Web UI**: Modern browser-based dashboard with full conversation viewer, search, and summary generation
- **Search**: Full-text search across all your conversations
- **Incremental Processing**: Only processes new messages, minimizing resource usage

## Installation

### Prerequisites

- Python 3.11+
- An Anthropic API key (for AI summaries)

### Install from source

```bash
git clone https://github.com/orlenko/claude-activity-log.git
cd claude-activity-log
pip install -e .
```

### Set up your API key (for summaries)

```bash
export ANTHROPIC_API_KEY="your-api-key-here"
```

Get your API key from [console.anthropic.com](https://console.anthropic.com/) under Settings > API Keys.

## Usage

### Start the Daemon

```bash
# Start in background
claude-activity start

# Or run in foreground (useful for debugging)
claude-activity start -f
```

### Check Status

```bash
claude-activity status
```

### View Activity

```bash
# Today's activity summary
claude-activity today
claude-activity today --detailed  # Include message list

# This week's AI summary
claude-activity week
claude-activity week --generate   # Generate if not exists
claude-activity week --last       # Last week

# This month's AI summary
claude-activity month
claude-activity month --generate
```

### Browse Sessions

```bash
# List recent sessions
claude-activity sessions
claude-activity sessions --limit 50
claude-activity sessions --repo myproject

# View a specific session
claude-activity session abc123def456
```

### Search Conversations

```bash
claude-activity search "authentication"
claude-activity search "bug fix" --repo myproject
```

### Generate Summaries

```bash
# Generate summaries for all unsummarized periods
claude-activity summarize

# Regenerate all summaries
claude-activity summarize --force
```

### List Projects

```bash
claude-activity projects
```

### Stop the Daemon

```bash
claude-activity stop
```

### Web UI

Launch a beautiful browser-based dashboard:

```bash
# Start web server (default: http://127.0.0.1:5000)
claude-activity web

# Custom host and port
claude-activity web --host 0.0.0.0 --port 8080

# Debug mode (auto-reload on code changes)
claude-activity web --debug
```

The web UI provides:
- **Dashboard**: Overview of today's activity, recent sessions, and quick links
- **Sessions Browser**: List and filter sessions by project, with full conversation viewer
- **Project Explorer**: Browse all tracked projects and their sessions
- **Search**: Search across all conversations with highlighted results
- **Summaries**: View and generate daily/weekly/monthly AI summaries with one click
- **Live Status**: Real-time daemon status indicator

## Auto-Start on Login (macOS)

To have the daemon start automatically when you log in:

```bash
# Copy the launchd plist
cp launchd/com.claude-activity.watcher.plist ~/Library/LaunchAgents/

# Load it
launchctl load ~/Library/LaunchAgents/com.claude-activity.watcher.plist
```

To unload:

```bash
launchctl unload ~/Library/LaunchAgents/com.claude-activity.watcher.plist
```

## Configuration

Configuration is stored in `~/.config/claude-activity/config.toml` (optional):

```toml
[database]
path = "~/.claude-activity/activity.db"

[watcher]
claude_dir = "~/.claude"
poll_interval = 1.0  # seconds

[summarizer]
model = "claude-3-5-haiku-latest"
auto_summarize = true
summarize_hour = 23  # 11 PM local time
```

## Data Storage

- **Database**: `~/.claude-activity/activity.db`
- **Logs**: `~/.claude-activity/watcher.log`
- **PID file**: `~/.claude-activity/watcher.pid`

## How It Works

1. The watcher daemon monitors `~/.claude/projects/` for JSONL session files
2. New messages are parsed and stored in SQLite with full metadata
3. The CLI queries the database to show activity, sessions, and search results
4. AI summaries are generated on-demand using Claude API, aggregating daily → weekly → monthly

## License

MIT
