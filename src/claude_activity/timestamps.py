"""Centralized timestamp utilities for Claude Activity Logger.

TIMESTAMP CONVENTION
====================

All timestamps in this application follow these rules:

1. STORAGE (Database):
   - All timestamps are stored in UTC
   - Stored as naive datetime objects (no timezone info attached)
   - SQLite stores them as 'YYYY-MM-DD HH:MM:SS.ffffff' strings
   - Example: '2026-01-24 01:29:24.759000' represents UTC time

2. PARSING (Input):
   - When parsing timestamps from external sources (JSONL, APIs, files):
     * ISO 8601 strings with 'Z' or '+00:00' -> converted to UTC
     * Unix timestamps -> interpreted as UTC
     * Naive strings without timezone -> assumed to be UTC
   - Use `parse_timestamp()` for all external timestamp parsing
   - Use `utc_now()` for current time in any code that stores data

3. DISPLAY (Output):
   - Convert to local time only at the moment of display
   - Use `utc_to_local()` in templates and CLI output
   - Never store local times in the database

4. QUERIES:
   - When filtering by "today" or date ranges, convert local date bounds to UTC
   - Use `local_to_utc()` for query bounds
   - Example: "today" in PST means 08:00 UTC yesterday to 08:00 UTC today

USAGE EXAMPLES
==============

Storing current time:
    from .timestamps import utc_now
    timestamp = utc_now()  # Not datetime.now()!

Parsing external timestamp:
    from .timestamps import parse_timestamp
    ts = parse_timestamp(data.get('timestamp'))

Displaying timestamp:
    from .timestamps import utc_to_local
    local_time = utc_to_local(stored_timestamp)
    print(local_time.strftime('%Y-%m-%d %H:%M'))

Querying by date:
    from .timestamps import get_today_utc_range
    start, end = get_today_utc_range()
    db.query("WHERE timestamp >= ? AND timestamp < ?", start, end)
"""

from datetime import datetime, date, timedelta, timezone
from typing import Any, Optional, Tuple


def utc_now() -> datetime:
    """Get current time in UTC as a naive datetime.

    Use this instead of datetime.now() for any timestamp that will be stored.
    Returns a naive datetime in UTC (no tzinfo attached) for SQLite compatibility.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_utc(dt: datetime) -> datetime:
    """Convert a datetime to UTC naive datetime.

    Args:
        dt: A datetime object (timezone-aware or naive)

    Returns:
        Naive datetime in UTC

    If the input is timezone-aware, it's converted to UTC.
    If the input is naive, it's assumed to already be in UTC.
    """
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def get_local_offset() -> timedelta:
    """Get the current local timezone offset from UTC.

    Returns:
        timedelta representing the offset (e.g., -8 hours for PST)
    """
    local_now = datetime.now()
    utc_now_time = datetime.now(timezone.utc).replace(tzinfo=None)
    return local_now - utc_now_time


def utc_to_local(dt: datetime) -> datetime:
    """Convert a UTC datetime to local time for display.

    Args:
        dt: Naive datetime assumed to be in UTC

    Returns:
        Naive datetime in local time

    Use this only for display purposes. Never store the result.
    """
    if dt is None:
        return None
    offset = get_local_offset()
    return dt + offset


def local_to_utc(dt: datetime) -> datetime:
    """Convert a local datetime to UTC for database queries.

    Args:
        dt: Naive datetime in local time

    Returns:
        Naive datetime in UTC

    Use this when constructing date range queries from local dates.
    """
    offset = get_local_offset()
    return dt - offset


def get_today_utc_range() -> Tuple[datetime, datetime]:
    """Get the UTC datetime range for "today" in local time.

    Returns:
        Tuple of (start, end) datetimes in UTC representing local midnight to midnight

    Example: If it's Jan 24 in PST (UTC-8), returns:
        start = Jan 24 08:00 UTC (midnight PST)
        end = Jan 25 08:00 UTC (midnight PST next day)
    """
    today = date.today()
    local_start = datetime.combine(today, datetime.min.time())
    local_end = datetime.combine(today + timedelta(days=1), datetime.min.time())
    return local_to_utc(local_start), local_to_utc(local_end)


def parse_timestamp(ts: Any) -> datetime:
    """Parse a timestamp from various formats into a UTC naive datetime.

    Args:
        ts: Can be:
            - datetime object (with or without timezone)
            - Unix timestamp (int/float, seconds or milliseconds)
            - ISO 8601 string ('2026-01-24T01:29:24Z', '2026-01-24T01:29:24+00:00')
            - SQLite format string ('2026-01-24 01:29:24.759000')

    Returns:
        Naive datetime in UTC

    Handles:
        - 'Z' suffix (replaced with +00:00)
        - Millisecond unix timestamps (> 1e12)
        - Microseconds in ISO strings
        - Missing timezone (assumes UTC)
    """
    if ts is None:
        return utc_now()

    if isinstance(ts, datetime):
        return to_utc(ts)

    if isinstance(ts, (int, float)):
        # Unix timestamp - always UTC
        if ts > 1e12:  # Milliseconds
            return datetime.utcfromtimestamp(ts / 1000)
        return datetime.utcfromtimestamp(ts)

    if isinstance(ts, str):
        # ISO format string
        try:
            # Handle 'Z' suffix
            ts_str = ts.replace('Z', '+00:00')

            # Truncate microseconds if too long (some APIs send nanoseconds)
            if '.' in ts_str:
                parts = ts_str.split('.')
                if len(parts) == 2:
                    # Split fractional seconds from timezone
                    if '+' in parts[1]:
                        frac, tz = parts[1].split('+', 1)
                        tz = '+' + tz
                    elif parts[1].count('-') > 0:
                        # Handle negative timezone like -08:00
                        frac_parts = parts[1].rsplit('-', 1)
                        if len(frac_parts) == 2 and ':' in frac_parts[1]:
                            frac = frac_parts[0]
                            tz = '-' + frac_parts[1]
                        else:
                            frac = parts[1]
                            tz = ''
                    else:
                        frac = parts[1]
                        tz = ''

                    # Truncate to 6 digits (microseconds)
                    if len(frac) > 6:
                        frac = frac[:6]
                    ts_str = parts[0] + '.' + frac + tz

            parsed = datetime.fromisoformat(ts_str)
            return to_utc(parsed)

        except ValueError:
            pass

    # Fallback: return current UTC time
    return utc_now()


def format_local_time(dt: datetime, fmt: str = '%Y-%m-%d %H:%M:%S') -> str:
    """Format a UTC datetime as local time string.

    Args:
        dt: Naive datetime in UTC
        fmt: strftime format string

    Returns:
        Formatted string in local time
    """
    if dt is None:
        return ''
    return utc_to_local(dt).strftime(fmt)


def format_local_date(dt: datetime, fmt: str = '%Y-%m-%d') -> str:
    """Format a UTC datetime as local date string.

    Args:
        dt: Naive datetime in UTC
        fmt: strftime format string

    Returns:
        Formatted string in local time
    """
    if dt is None:
        return ''
    return utc_to_local(dt).strftime(fmt)


def timeago(dt: datetime) -> str:
    """Format a UTC datetime as a relative time string.

    Args:
        dt: Naive datetime in UTC

    Returns:
        Human-readable relative time (e.g., "5m ago", "2h ago", "3d ago")
    """
    if dt is None:
        return ''

    now = utc_now()
    diff = now - dt

    if diff.days > 30:
        return utc_to_local(dt).strftime('%b %d, %Y')
    elif diff.days > 0:
        return f"{diff.days}d ago"
    elif diff.seconds > 3600:
        return f"{diff.seconds // 3600}h ago"
    elif diff.seconds > 60:
        return f"{diff.seconds // 60}m ago"
    else:
        return "just now"
