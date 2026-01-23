"""Claude API summarization for activity logs."""

import os
from datetime import datetime, date, timedelta
from typing import Optional

from anthropic import Anthropic

from .config import Config, get_config
from .db import Database


DAILY_SUMMARY_PROMPT = """Analyze these Claude Code user requests from {date} and provide a concise summary.

The requests are grouped by project, showing what the user asked Claude to help with.

{conversations}

Format your response as:
## Accomplishments
- [bullet points of what was worked on, inferred from the requests]

## Key Topics
- [main themes or areas of focus]

## Open Items
- [any unresolved questions or follow-ups mentioned]

Keep the summary concise (under 200 words). Focus on the most significant activities.
If there are no conversations, just say "No significant activity recorded."
"""

WEEKLY_SUMMARY_PROMPT = """Synthesize these daily summaries into a comprehensive weekly summary for the week of {start_date} to {end_date}.

{daily_summaries}

Format your response as:
## Week Overview
[1-2 sentence high-level summary]

## Major Accomplishments
- [significant completions across the week]

## Key Decisions Made
- [important decisions that shaped the week's work]

## Projects Worked On
- [list projects and brief description of work done]

## Patterns & Observations
- [any notable patterns, recurring themes, or productivity observations]

## Looking Ahead
- [open items or planned next steps mentioned]

Keep the summary comprehensive but focused on the most impactful activities.
"""

MONTHLY_SUMMARY_PROMPT = """Synthesize these weekly summaries into a comprehensive monthly summary for {month_name} {year}.

{weekly_summaries}

Format your response as:
## Month Overview
[2-3 sentence high-level summary of the month]

## Major Accomplishments
- [significant completions across the month]

## Key Decisions & Direction Changes
- [important decisions that shaped the month's work]

## Projects & Progress
- [list projects and describe progress made]

## Time Investment Patterns
- [observations about where time was spent]

## Lessons & Insights
- [any lessons learned or insights gained]

## Next Month Focus
- [items that should carry forward or need attention]

Be comprehensive but focus on the most significant activities and patterns.
"""

SESSION_CONTEXT_PROMPT = """Analyze this Claude Code conversation and create a detailed context summary that can be used to resume this work in a new session.

**Project:** {project_name}
**Branch:** {git_branch}
**Date:** {session_date}

## Conversation:
{conversation}

---

Create a comprehensive context summary that captures everything needed to continue this work. Include:

## Objective
What was the user trying to accomplish? What was the high-level goal?

## Background & Motivation
Why was this work being done? What problem was being solved? Any important context about the codebase or requirements.

## Key Decisions Made
Important technical or design decisions that were made during the conversation, and the reasoning behind them.

## What Was Implemented
Specific changes that were made - files modified, features added, bugs fixed. Be specific about what code was written or changed.

## Important Considerations
Any constraints, edge cases, gotchas, or important details that came up during the work that should be remembered.

## Current State
Where did the work end up? What's working, what's not? Any known issues?

## Next Steps / Open Items
What still needs to be done? Any unfinished work or follow-up tasks mentioned?

## Acceptance Criteria
If mentioned, what are the criteria for this work to be considered complete?

---

Write this summary as if briefing another developer (or Claude) who needs to pick up this work. Be detailed enough to restore full context, but focus on what matters for continuing the work. Aim for 1-2 pages of content.
"""


class Summarizer:
    """Generate summaries of Claude activity using the Claude API."""

    def __init__(self, config: Optional[Config] = None, db: Optional[Database] = None):
        self.config = config or get_config()
        self.db = db or Database(self.config)
        self.client = Anthropic()  # Uses ANTHROPIC_API_KEY env var

    def _format_messages_for_summary(self, messages: list[dict], max_total_chars: int = 50000) -> str:
        """Format messages for the summary prompt.

        Only includes user messages to save tokens - user prompts contain
        enough context to understand what was worked on.
        """
        # Group messages by project and session for better context
        by_project: dict[str, list[dict]] = {}
        for msg in messages:
            project = msg.get('project_name', 'Unknown')
            if project not in by_project:
                by_project[project] = []
            by_project[project].append(msg)

        formatted = []
        total_chars = 0

        for project, proj_messages in by_project.items():
            user_messages = [m for m in proj_messages if m.get('role') == 'user']
            assistant_count = len([m for m in proj_messages if m.get('role') == 'assistant'])

            if not user_messages:
                continue

            formatted.append(f"## Project: {project}")
            formatted.append(f"({len(user_messages)} requests, {assistant_count} responses)")

            for msg in user_messages:
                content = msg.get('content') or ''
                # Aggressive truncation for individual messages
                if len(content) > 300:
                    content = content[:300] + "..."

                # Check total limit
                if total_chars + len(content) > max_total_chars:
                    formatted.append("... (truncated due to length)")
                    break

                formatted.append(f"- {content}")
                total_chars += len(content)

            if total_chars > max_total_chars:
                break

            formatted.append("")  # Blank line between projects

        return "\n".join(formatted) if formatted else "No conversations recorded."

    def _call_claude(self, prompt: str) -> str:
        """Call Claude API to generate summary."""
        response = self.client.messages.create(
            model=self.config.summarizer.model,
            max_tokens=2000,
            messages=[
                {"role": "user", "content": prompt}
            ]
        )
        return response.content[0].text

    def generate_daily_summary(
        self,
        target_date: date,
        project_id: Optional[int] = None,
        force: bool = False
    ) -> Optional[str]:
        """Generate a daily summary for a specific date.

        Args:
            target_date: The date to summarize
            project_id: Optional project filter
            force: If True, regenerate even if summary exists

        Returns:
            The generated summary, or None if no data
        """
        # Check if summary already exists
        if not force:
            existing = self.db.get_summary('daily', target_date, project_id)
            if existing:
                return existing['summary']

        # Get messages for the date
        start = datetime.combine(target_date, datetime.min.time())
        end = datetime.combine(target_date + timedelta(days=1), datetime.min.time())

        messages = self.db.get_messages_in_range(start, end, project_id)

        # Filter to user/assistant messages only
        messages = [m for m in messages if m.get('role') in ('user', 'assistant')]

        if not messages:
            return None

        # Format and generate summary
        conversations = self._format_messages_for_summary(messages)
        prompt = DAILY_SUMMARY_PROMPT.format(
            date=target_date.strftime("%Y-%m-%d"),
            conversations=conversations
        )

        summary = self._call_claude(prompt)

        # Save summary
        self.db.save_summary(
            period_type='daily',
            period_start=target_date,
            period_end=target_date,
            summary=summary,
            project_id=project_id
        )

        return summary

    def generate_weekly_summary(
        self,
        week_start: date,
        project_id: Optional[int] = None,
        force: bool = False
    ) -> Optional[str]:
        """Generate a weekly summary from daily summaries.

        Args:
            week_start: The Monday of the week to summarize
            project_id: Optional project filter
            force: If True, regenerate even if summary exists

        Returns:
            The generated summary, or None if no data
        """
        week_end = week_start + timedelta(days=6)

        # Check if summary already exists
        if not force:
            existing = self.db.get_summary('weekly', week_start, project_id)
            if existing:
                return existing['summary']

        # Get daily summaries for the week
        daily_summaries = self.db.get_summaries_in_range('daily', week_start, week_end, project_id)

        if not daily_summaries:
            # Try to generate missing daily summaries first
            for i in range(7):
                day = week_start + timedelta(days=i)
                if day < date.today():  # Don't summarize future or today
                    self.generate_daily_summary(day, project_id)

            daily_summaries = self.db.get_summaries_in_range('daily', week_start, week_end, project_id)

        if not daily_summaries:
            return None

        # Format daily summaries
        formatted = []
        for ds in daily_summaries:
            formatted.append(f"### {ds['period_start']}\n{ds['summary']}")

        prompt = WEEKLY_SUMMARY_PROMPT.format(
            start_date=week_start.strftime("%Y-%m-%d"),
            end_date=week_end.strftime("%Y-%m-%d"),
            daily_summaries="\n\n".join(formatted)
        )

        summary = self._call_claude(prompt)

        # Save summary
        self.db.save_summary(
            period_type='weekly',
            period_start=week_start,
            period_end=week_end,
            summary=summary,
            project_id=project_id
        )

        return summary

    def generate_monthly_summary(
        self,
        year: int,
        month: int,
        project_id: Optional[int] = None,
        force: bool = False
    ) -> Optional[str]:
        """Generate a monthly summary from weekly summaries.

        Args:
            year: Year
            month: Month (1-12)
            project_id: Optional project filter
            force: If True, regenerate even if summary exists

        Returns:
            The generated summary, or None if no data
        """
        month_start = date(year, month, 1)
        if month == 12:
            month_end = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            month_end = date(year, month + 1, 1) - timedelta(days=1)

        # Check if summary already exists
        if not force:
            existing = self.db.get_summary('monthly', month_start, project_id)
            if existing:
                return existing['summary']

        # Get weekly summaries for the month
        weekly_summaries = self.db.get_summaries_in_range('weekly', month_start, month_end, project_id)

        if not weekly_summaries:
            # Try to generate missing weekly summaries
            current = month_start
            while current <= month_end:
                # Find Monday of this week
                monday = current - timedelta(days=current.weekday())
                if monday >= month_start - timedelta(days=6):  # Include partial weeks
                    self.generate_weekly_summary(monday, project_id)
                current += timedelta(days=7)

            weekly_summaries = self.db.get_summaries_in_range('weekly', month_start, month_end, project_id)

        if not weekly_summaries:
            return None

        # Format weekly summaries
        formatted = []
        for ws in weekly_summaries:
            formatted.append(f"### Week of {ws['period_start']}\n{ws['summary']}")

        month_name = month_start.strftime("%B")
        prompt = MONTHLY_SUMMARY_PROMPT.format(
            month_name=month_name,
            year=year,
            weekly_summaries="\n\n".join(formatted)
        )

        summary = self._call_claude(prompt)

        # Save summary
        self.db.save_summary(
            period_type='monthly',
            period_start=month_start,
            period_end=month_end,
            summary=summary,
            project_id=project_id
        )

        return summary

    def summarize_unsummarized(
        self,
        project_id: Optional[int] = None,
        force: bool = False
    ) -> dict:
        """Generate summaries for all unsummarized periods.

        Returns:
            Dict with counts of generated summaries
        """
        results = {'daily': 0, 'weekly': 0, 'monthly': 0}

        # Generate missing daily summaries
        unsummarized_days = self.db.get_unsummarized_days(project_id)
        for day in unsummarized_days:
            try:
                summary = self.generate_daily_summary(day, project_id, force)
                if summary:
                    results['daily'] += 1
            except Exception as e:
                print(f"Error summarizing {day}: {e}")

        # Generate weekly summaries for complete weeks
        today = date.today()
        # Find the Monday of last week
        last_monday = today - timedelta(days=today.weekday() + 7)

        # Check last 4 weeks
        for i in range(4):
            week_monday = last_monday - timedelta(weeks=i)
            existing = self.db.get_summary('weekly', week_monday, project_id)
            if not existing or force:
                try:
                    summary = self.generate_weekly_summary(week_monday, project_id, force)
                    if summary:
                        results['weekly'] += 1
                except Exception as e:
                    print(f"Error summarizing week of {week_monday}: {e}")

        return results

    def generate_session_context(self, session_id: str) -> Optional[str]:
        """Generate a detailed context summary for a session that can be used to resume work.

        This creates a comprehensive summary suitable for pasting into a new Claude session
        to restore context and continue work on a feature/branch.

        Args:
            session_id: The session UUID (can be partial prefix)

        Returns:
            The context summary, or None if session not found
        """
        # Find session by prefix
        with self.db.connection() as conn:
            cursor = conn.execute(
                "SELECT id, session_id, project_id, git_branch, started_at FROM sessions WHERE session_id LIKE ?",
                (f"{session_id}%",)
            )
            session = cursor.fetchone()

        if not session:
            return None

        session = dict(session)
        session_db_id = session['id']

        # Get project info
        project_name = "Unknown"
        if session.get('project_id'):
            project = self.db.get_project(session['project_id'])
            if project:
                project_name = project.get('name') or project.get('path') or "Unknown"

        # Get all messages for this session
        messages = self.db.get_messages_for_session(session_db_id)

        # Filter to user/assistant messages only, skip tool-only messages
        filtered_messages = []
        for msg in messages:
            role = msg.get('role')
            content = msg.get('content') or ''

            if role not in ('user', 'assistant'):
                continue

            content_stripped = content.strip()
            if not content_stripped:
                continue
            if content_stripped.startswith('[Tool:') or content_stripped == '[Tool Result]':
                continue

            filtered_messages.append(msg)

        if not filtered_messages:
            return None

        # Format conversation for the prompt
        # Include both user and assistant messages for full context
        conversation_parts = []
        for msg in filtered_messages:
            role = msg.get('role', 'unknown')
            content = msg.get('content') or ''

            # Truncate very long messages but keep more than daily summaries
            if len(content) > 2000:
                content = content[:2000] + "\n... [truncated]"

            conversation_parts.append(f"**{role.upper()}:** {content}")

        conversation = "\n\n".join(conversation_parts)

        # Limit total conversation size
        if len(conversation) > 100000:
            conversation = conversation[:100000] + "\n\n... [conversation truncated due to length]"

        # Get session date
        session_date = session.get('started_at')
        if isinstance(session_date, datetime):
            session_date = session_date.strftime("%Y-%m-%d")
        else:
            session_date = str(session_date) if session_date else "Unknown"

        # Generate the context summary
        prompt = SESSION_CONTEXT_PROMPT.format(
            project_name=project_name,
            git_branch=session.get('git_branch') or "Unknown",
            session_date=session_date,
            conversation=conversation
        )

        # Use a larger model for better context extraction if available
        response = self.client.messages.create(
            model="claude-sonnet-4-20250514",  # Use Sonnet for better quality
            max_tokens=4000,
            messages=[
                {"role": "user", "content": prompt}
            ]
        )

        return response.content[0].text
