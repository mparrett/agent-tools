#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["rich", "blessed"]
# ///
"""
Claude conversation log viewer and search tool.

Quick start:
    ./claude-ls.py last              # show last assistant response (auto-detects project)
    ./claude-ls.py list              # list recent messages from current project
    ./claude-ls.py browse            # interactive browser

Search:
    ./claude-ls.py search "query"    # find sessions containing text
    ./claude-ls.py search "bug" -C   # show matching context
    ./claude-ls.py list -c chess     # find sessions by project/session name

Run with --help or <command> --help for full options.
"""

import argparse
import concurrent.futures
import functools
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple, TypeVar

from blessed import Terminal
from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text


def draw_split_list_with_preview(
    term,
    header: str,
    items: List[str],
    selected_index: int,
    preview_lines: List[str],
    footer: str = "",
    preview_offset: int = 0,
) -> None:
    """Draw a split-pane view with selectable list on left and preview on right.

    Args:
        term: blessed Terminal instance
        header: Title text shown at top
        items: List of strings for left pane (selectable items)
        selected_index: Currently highlighted item index
        preview_lines: List of strings for right pane (preview content)
        footer: Optional footer text
        preview_offset: Scroll offset for preview pane
    """
    # Layout calculation
    left_total = max(30, int(term.width * 0.45))
    left_content_width = max(1, left_total - 1)
    right_width = max(1, term.width - left_content_width - 4)

    # Header
    header_text = term.truncate(header, term.width - 1)
    print(term.bold + term.center(header_text, term.width - 1) + term.normal + term.clear_eol)
    print("─" * term.width + term.clear_eol)

    # Calculate visible window
    max_rows = term.height - 5
    start_index = 0
    if selected_index >= max_rows:
        start_index = selected_index - max_rows + 1
    visible = items[start_index : start_index + max_rows]

    # Draw rows
    for row, item in enumerate(visible):
        absolute_index = start_index + row
        cursor = "►" if absolute_index == selected_index else " "
        style = term.reverse if absolute_index == selected_index else term.normal
        raw_line = f"{cursor} {item}"
        line_trunc = term.truncate(raw_line, left_content_width)
        left = style + term.ljust(line_trunc, left_content_width) + term.normal

        # Print: leading margin + left content + space + divider + space
        print(" " + left + " │ ", end="")

        preview_idx = preview_offset + row
        if 0 <= preview_idx < len(preview_lines):
            line_to_print = preview_lines[preview_idx]
            print(term.truncate(line_to_print, right_width) + term.clear_eol)
        else:
            print(term.clear_eol)

    # Fill remaining rows
    for extra_row in range(max(0, max_rows - len(visible))):
        r = len(visible) + extra_row
        print(" " * (left_content_width + 1) + " │ ", end="")
        preview_idx = preview_offset + r
        if 0 <= preview_idx < len(preview_lines):
            line_to_print = preview_lines[preview_idx]
            print(term.truncate(line_to_print, right_width) + term.clear_eol)
        else:
            print(term.clear_eol)

    # Footer
    print("─" * term.width + term.clear_eol)
    if footer:
        print(term.truncate(footer, term.width - 1) + term.clear_eol)


# Configuration constants
FILENAME_ABBREVIATION_HEAD = 8  # Characters to show at start of abbreviated filenames
FILENAME_ABBREVIATION_TAIL = 8  # Characters to show at end of abbreviated filenames
MAX_PROJECT_SESSIONS_PREVIEW = 5  # Maximum sessions to show in project preview
HERE_DEEP_SCAN_LIMIT = 40  # Maximum recent sessions scanned per non-anchored project
SESSION_MESSAGE_INDENT = 8  # Spaces to indent message previews under session headers
SESSION_CACHE_MAX_SIZE = 100  # Max cached session messages before clearing


def get_role_color(term, role: str):
    """Get consistent terminal color for message roles."""
    return term.green if role == "assistant" else term.yellow


def to_negative_index(pos: int, total: int) -> int:
    """Convert 0-based position to negative index.

    Example: in a list of 10 items, pos=0 -> -10, pos=9 -> -1
    """
    return pos - total


# Every accepted mode name, aliases included. Kept next to parse_args so adding a
# subparser means adding it here too — _did_you_mean checks argv against this before
# argparse gets a chance to dump the full choice list as an error.
KNOWN_MODES = frozenset(
    {
        "list",
        "get",
        "select",
        "browse",
        "last",
        "search",
        "pending",
        "export",
        "sessions",
        "here",
        "find",
        "info",
        "show",
        "summary",
        "resume",
        "activity",
        "resolve",
        "who",
    }
)

MODE_OVERVIEW = """\
common:  info <id> · search <text> · last · resume <id>

modes by intent:
  one session     info (aka show, summary) · get · export · resume
  find sessions   search · sessions · here (aka find) · pending · who
  browse          browse · select · list · last
  reports         activity · resolve"""


def _did_you_mean(argv) -> None:
    """If the mode-position token isn't a known mode, exit with a short suggestion
    instead of argparse's full-choice-list error (a wall of 15 names helps nobody
    mid-fumble). Flags before the mode are skipped; -h/--help fall through to
    argparse."""
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ("-h", "--help"):
            return
        if tok == "--base-dir":
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        break
    if i >= len(argv):
        return
    tok = argv[i]
    if tok in KNOWN_MODES:
        return
    import difflib

    close = difflib.get_close_matches(tok, sorted(KNOWN_MODES), n=3, cutoff=0.5)
    hint = f" — did you mean: {', '.join(close)}?" if close else ""
    print(f"claude-ls: unknown mode '{tok}'{hint}", file=sys.stderr)
    print(f"\n{MODE_OVERVIEW}", file=sys.stderr)
    print("\nfull list: claude-ls -h", file=sys.stderr)
    sys.exit(2)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Claude conversation log extractor and browser",
        epilog=MODE_OVERVIEW,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--no-color", action="store_true", help="Disable colored output")
    parser.add_argument(
        "--base-dir",
        default=str(Path.home() / ".claude" / "projects"),
        help="Base directory containing projects (default: ~/.claude/projects)",
    )

    subparsers = parser.add_subparsers(dest="mode", metavar="<mode>", help="Available modes")

    # List mode
    list_parser = subparsers.add_parser("list", help="List recent messages with indices")
    list_parser.add_argument(
        "--count", "-n", type=int, default=10, help="Number of messages to show (default: 10)"
    )
    list_parser.add_argument(
        "--role",
        "-r",
        choices=["user", "assistant", "all"],
        default="all",
        help="Filter by message role (default: all)",
    )
    list_parser.add_argument(
        "--session-id",
        "-s",
        type=str,
        help="Specific session ID (default: auto-detect most recent)",
    )
    list_parser.add_argument(
        "--contains",
        "-c",
        type=str,
        help="List sessions containing this substring (case insensitive)",
    )
    list_parser.add_argument(
        "--sort",
        choices=SESSION_SORT_KEYS,
        default=SESSION_SORT_DEFAULT,
        help=f"With --contains -- {SESSION_SORT_HELP}",
    )
    list_parser.add_argument(
        "file", nargs="?", help="Conversation file (.jsonl) - optional if in project dir"
    )

    # Get mode
    get_parser = subparsers.add_parser(
        "get", help="Print raw message text for piping (no header, no metadata)"
    )
    get_parser.add_argument(
        "session_or_file", nargs="?", help="Session ID prefix or .jsonl file path"
    )
    get_parser.add_argument(
        "--index",
        "-i",
        type=int,
        default=-1,
        help="Message index (negative from end, default: -1 = last)",
    )
    get_parser.add_argument(
        "--session-id",
        "-s",
        type=str,
        help="Specific session ID (default: auto-detect most recent)",
    )
    get_parser.add_argument(
        "--tail", type=int, help="Show last N turns instead of a single message"
    )

    # Select mode
    select_parser = subparsers.add_parser("select", help="Interactive message selection")
    select_parser.add_argument(
        "--role",
        "-r",
        choices=["user", "assistant", "all"],
        default="all",
        help="Filter by message role (default: all)",
    )
    select_parser.add_argument(
        "--session-id",
        "-s",
        type=str,
        help="Specific session ID (default: auto-detect most recent)",
    )
    select_parser.add_argument(
        "file", nargs="?", help="Conversation file (.jsonl) - optional if in project dir"
    )

    # Browse mode (project → session → messages)
    browse_parser = subparsers.add_parser(
        "browse", help="Interactive browser: pick project/session and view messages"
    )
    browse_parser.add_argument(
        "--role",
        "-r",
        choices=["user", "assistant", "all"],
        default="all",
        help="Filter by message role (default: all)",
    )
    browse_parser.add_argument(
        "--latest",
        action="store_true",
        help="After selecting project (or if --project provided), open most recent session",
    )
    browse_parser.add_argument(
        "--project", default="", help="Preselect project by name substring or exact folder name"
    )
    browse_parser.add_argument(
        "--count",
        "-n",
        type=int,
        default=20,
        help="Number of recent messages to show in list (default: 20)",
    )

    # Last mode (CWD-aware recent assistant messages)
    last_parser = subparsers.add_parser(
        "last", help="Show recent messages from current directory's conversation"
    )
    last_parser.add_argument(
        "--turns", type=int, default=1, help="Number of turns to show (default: 1)"
    )
    last_parser.add_argument(
        "--role",
        "-r",
        choices=["user", "assistant", "all"],
        default="assistant",
        help="Filter by message role (default: assistant)",
    )
    last_parser.add_argument(
        "--session-id",
        "-s",
        type=str,
        help="Specific session ID (default: auto-detect most recent)",
    )
    last_parser.add_argument(
        "--cwd",
        type=str,
        help="Resolve the session from this directory instead of the process CWD "
        "(for tooling that inspects another Claude process's working dir)",
    )
    last_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the resolved session and recent messages (of --role) as JSON for scripting",
    )
    last_parser.add_argument(
        "file", nargs="?", help="Conversation file (.jsonl) - optional if in project dir"
    )

    # Search mode (search conversation content)
    search_parser = subparsers.add_parser(
        "search", help="Search for sessions containing text in conversation content"
    )
    search_parser.add_argument(
        "query",
        nargs="+",
        help="Text to search for (bare words are joined, so quoting is optional)",
    )
    search_parser.add_argument(
        "--count",
        "-n",
        type=int,
        default=5,
        help="Maximum number of sessions to show (default: 5)",
    )
    search_parser.add_argument(
        "--project",
        "-p",
        type=str,
        help="Limit search to sessions in project matching this substring",
    )
    search_parser.add_argument(
        "--respect-ignore",
        action="store_true",
        help=(
            "Honor .gitignore/.ignore/global-excludes rules when searching "
            "(default: off — the ~/.claude store is searched in full)"
        ),
    )
    search_parser.add_argument(
        "--show-context",
        "-C",
        action="store_true",
        help="Expand to the full multi-snippet view with where edits/PRs landed "
        "(default: one line + one inline snippet per session)",
    )
    search_parser.add_argument(
        "--brief", "-b", action="store_true", help="One line per session, no snippet"
    )
    search_parser.add_argument(
        "--case-sensitive",
        "-s",
        action="store_true",
        help="Case-sensitive match (default: case-insensitive)",
    )
    search_parser.add_argument(
        "--verbose", "-v", action="store_true", help="Show full session file path under each header"
    )
    search_parser.add_argument(
        "--exclude-type",
        "-x",
        action="append",
        default=[],
        metavar="TYPE",
        help="Skip matches whose role tag contains TYPE (substring). "
        "Tags: user, asst, think, tool, tool_in, summ. "
        "Example: -x tool excludes both 'tool' and 'tool_in'. Repeatable.",
    )
    search_parser.add_argument(
        "--only-type",
        "-o",
        action="append",
        default=[],
        metavar="TYPE",
        help="Keep only matches whose role tag contains TYPE (substring). "
        "Inclusive counterpart to -x. Example: -o user. Repeatable.",
    )
    search_parser.add_argument(
        "--max-matches",
        "-m",
        type=int,
        default=3,
        help="Max snippets to show per session (default: 3)",
    )
    search_parser.add_argument(
        "--width",
        "-w",
        type=int,
        default=50,
        help="Chars of context on each side of a match (default: 50)",
    )
    search_parser.add_argument(
        "--since",
        type=str,
        metavar="DATE",
        help="Only sessions modified on/after DATE "
        "(YYYY-MM-DD, 'today', 'yesterday', or 'Nd' for N days ago)",
    )
    search_parser.add_argument(
        "--until",
        type=str,
        metavar="DATE",
        help="Only sessions modified on/before DATE (same formats as --since)",
    )
    search_parser.add_argument(
        "--no-work",
        action="store_true",
        help="Don't derive/show where edits & PRs actually landed (faster)",
    )
    search_parser.add_argument(
        "--resume",
        "-r",
        metavar="#|ID",
        help="Resume a result instead of listing: a number picks the Nth shown "
        "row (1-based); anything else is treated as a session-id substring. "
        "Hands off to `claude -r` with the same tmux safety check as `resume`.",
    )
    search_parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="With -r: skip the double-drive check and resume anyway",
    )
    search_parser.add_argument(
        "--codex",
        action="store_true",
        default=os.environ.get("CLAUDE_LS_CODEX", "") not in ("", "0"),
        help="Also search OpenAI Codex CLI rollouts from ~/.codex/sessions "
        "(or set CLAUDE_LS_CODEX=1). Off by default, matching `sessions --codex`",
    )

    # Pending mode - show sessions waiting for user input
    pending_parser = subparsers.add_parser("pending", help="Show sessions waiting for user input")
    pending_parser.add_argument(
        "--stale",
        "-s",
        type=int,
        default=0,
        help="Only show sessions waiting > N minutes (default: 0 = all)",
    )
    pending_parser.add_argument("--project", "-p", type=str, help="Filter to specific project")
    pending_parser.add_argument("--json", action="store_true", help="Output as JSON for scripting")
    pending_parser.add_argument(
        "--count",
        "--limit",  # the original name, kept so existing invocations keep working
        "-n",
        dest="limit",
        type=int,
        default=10,
        help="Max sessions to show (default: 10)",
    )
    pending_parser.add_argument(
        "--include-agents",
        action="store_true",
        help="Include agent-* subagent sessions (excluded by default)",
    )

    # Export mode (render session as self-contained markdown)
    export_parser = subparsers.add_parser(
        "export", help="Render a session as a markdown document (frontmatter, structure)"
    )
    export_parser.add_argument(
        "file", nargs="?", help="Conversation file (.jsonl) - optional if in project dir"
    )
    export_parser.add_argument(
        "--session-id",
        "-s",
        type=str,
        help="Specific session ID (default: auto-detect most recent)",
    )
    export_parser.add_argument(
        "--output", "-o", type=str, help="Output file path (default: stdout)"
    )
    export_parser.add_argument(
        "--include-thinking", action="store_true", help="Include assistant thinking blocks"
    )
    export_parser.add_argument(
        "--include-tool-output", action="store_true", help="Include tool result output"
    )
    export_parser.add_argument(
        "--tool-output-lines", type=int, default=10, help="Max lines per tool output (default: 10)"
    )
    export_parser.add_argument(
        "--no-follow",
        action="store_true",
        help="Don't include related sessions with the same slug (default: follow)",
    )
    export_group = export_parser.add_mutually_exclusive_group()
    export_group.add_argument(
        "--compact", action="store_true", help="Compact mode: text only, no tool summaries"
    )
    export_group.add_argument(
        "--full", action="store_true", help="Full mode: include thinking and tool output"
    )

    # Sessions mode (list recent sessions with IDs)
    sessions_parser = subparsers.add_parser(
        "sessions", help="List recent sessions with IDs across all projects"
    )
    sessions_parser.add_argument(
        "--count", "-n", type=int, default=10, help="Number of sessions to show (default: 10)"
    )
    sessions_parser.add_argument(
        "--project", "-p", type=str, help="Filter to specific project by name substring"
    )
    sessions_parser.add_argument(
        "--all",
        "-a",
        action="store_true",
        help="Show sessions across all projects (default: CWD project if available, else all)",
    )
    sessions_parser.add_argument(
        "--codex",
        action="store_true",
        default=os.environ.get("CLAUDE_LS_CODEX", "") not in ("", "0"),
        help="Also list OpenAI Codex CLI rollouts from ~/.codex/sessions "
        "(or set CLAUDE_LS_CODEX=1). Off by default: mixing two agents' sessions "
        "isn't always wanted, and resume is Claude-only",
    )
    sessions_parser.add_argument(
        "--sort",
        choices=SESSION_SORT_KEYS,
        default=SESSION_SORT_DEFAULT,
        help=SESSION_SORT_HELP,
    )
    sessions_parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show each session's file path",
    )

    # Here mode (sessions anchored to the current dir, worktree-aware)
    here_parser = subparsers.add_parser(
        "here",
        aliases=["find"],
        help="Show sessions anchored to the current dir (checks the source repo for worktrees)",
    )
    here_parser.add_argument(
        "--count", "-n", type=int, default=10, help="Max sessions to show (default: 10)"
    )
    here_parser.add_argument(
        "--dir",
        "-C",
        type=str,
        default=None,
        help="Look up this directory instead of the current one",
    )
    here_parser.add_argument(
        "--codex",
        action="store_true",
        default=os.environ.get("CLAUDE_LS_CODEX", "") not in ("", "0"),
        help="Also resolve OpenAI Codex CLI rollouts from ~/.codex/sessions "
        "(or set CLAUDE_LS_CODEX=1). Off by default, matching `sessions --codex`",
    )
    here_parser.add_argument(
        "--sort",
        choices=SESSION_SORT_KEYS,
        default=SESSION_SORT_DEFAULT,
        help=SESSION_SORT_HELP,
    )
    here_parser.add_argument(
        "--deep",
        action="store_true",
        help=(
            "Also scan session histories for ones that did work here (recorded cwd "
            "or a file edited under this repo/dir) but started elsewhere "
            f"(up to {HERE_DEEP_SCAN_LIMIT} newest sessions per project; slower)"
        ),
    )

    # Info mode (the canonical session card; `show`/`summary` are aliases)
    info_parser = subparsers.add_parser(
        "info",
        aliases=["show", "summary"],
        help="Session card: who/what/when, work signals, first/last message",
    )
    info_parser.add_argument(
        "session_id", nargs="?", help="Session ID or prefix (default: most recent session for CWD)"
    )
    info_parser.add_argument("file", nargs="?", help="Conversation file (.jsonl) - optional")

    # Who mode (the live fleet by repo: pane, checkout, branch, status, topic)
    who_parser = subparsers.add_parser(
        "who",
        help="Live sessions by repo: pane, checkout @ branch, status, topic, files touched",
    )
    who_parser.add_argument(
        "--me",
        action="store_true",
        help="Only this session and the others in its repo (resolved from the environment)",
    )
    who_parser.add_argument(
        "--dir", "-C", type=str, default=None, help="Only sessions in this directory's repo"
    )
    who_parser.add_argument(
        "--within",
        type=int,
        default=0,
        help="Only sessions with transcript activity in the last N minutes (default: 0 = all)",
    )
    who_parser.add_argument(
        "--json", action="store_true", help="Output as JSON, one object per session"
    )

    # Resume mode (cd to the session's dir and hand off to `claude -r`)
    resume_parser = subparsers.add_parser(
        "resume",
        help="cd into a session's anchoring dir and resume it with `claude -r`",
    )
    resume_parser.add_argument(
        "session",
        nargs="?",
        help="Session ID or any unique substring of it",
    )
    resume_parser.add_argument(
        "--session-id",
        "-s",
        type=str,
        help="Session ID or substring (same as the positional arg)",
    )
    resume_parser.add_argument(
        "--pick",
        type=int,
        metavar="N",
        help="When the id matches several sessions, resume the Nth listed candidate",
    )
    resume_parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Skip the double-drive check and resume anyway",
    )
    resume_parser.add_argument(
        "--attach",
        "-a",
        action="store_true",
        help="If exactly one tmux session is running a Claude here, attach to it "
        "instead of resuming",
    )
    resume_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the cd + claude command instead of running it",
    )

    # Activity mode (week-in-review across sessions + repos)
    activity_parser = subparsers.add_parser(
        "activity", help="Week-in-review: sessions + git activity across projects"
    )
    activity_parser.add_argument(
        "--days",
        "-d",
        type=int,
        default=7,
        help="Window length in days (default: 7)",
    )
    activity_parser.add_argument(
        "--since",
        type=str,
        help="ISO date (YYYY-MM-DD) instead of --days; takes precedence",
    )
    activity_parser.add_argument(
        "--project",
        "-p",
        type=str,
        help="Filter to projects matching substring",
    )
    activity_parser.add_argument(
        "--min-turns",
        type=int,
        default=2,
        help="Skip sessions with fewer user turns in window (default: 2)",
    )

    resolve_parser = subparsers.add_parser(
        "resolve",
        help="Resolve a Claude project folder name or ambiguous path to the real filesystem path",
    )
    resolve_parser.add_argument(
        "input",
        help="A path (~/foo/bar), a session ID, or a flattened folder name. "
        "For folder names starting with '-', pass after '--' (e.g. resolve -- -Users-you-foo).",
    )

    _did_you_mean(sys.argv[1:])
    return parser.parse_args()


class ConversationMessage:
    """Represents a parsed conversation message"""

    def __init__(self, raw_data: Dict[str, Any]):
        self.raw_data = raw_data
        self.type = raw_data.get("type", "")
        self.timestamp_str = raw_data.get("timestamp", "")
        self.uuid = raw_data.get("uuid", "")

        # Parse message content
        message_data = raw_data.get("message", {})
        self.role = message_data.get("role", "")
        # Content can be a string (user input) or list of blocks (structured content)
        raw_content = message_data.get("content", [])
        if isinstance(raw_content, str):
            # User messages often have content as a plain string
            self._plain_text_content = raw_content
            self.content_blocks = []
        else:
            self._plain_text_content = None
            self.content_blocks = raw_content

    @property
    def text_content(self) -> str:
        """First text fragment of the message (plain-string content passes through)."""
        if self._plain_text_content is not None:
            return self._plain_text_content
        return next(
            (t for kind, t in iter_content_fragments(self.content_blocks) if kind == "text"),
            "",
        )

    @property
    def timestamp(self) -> datetime:
        """Parse timestamp"""
        try:
            # Handle both Z and +00:00 timezone formats
            timestamp = self.timestamp_str.replace("Z", "+00:00")
            return datetime.fromisoformat(timestamp)
        except (ValueError, AttributeError):
            return datetime.min

    @property
    def time_str(self) -> str:
        """Format time as HH:MM:SS"""
        try:
            return self.timestamp.strftime("%H:%M:%S")
        except (ValueError, AttributeError):
            return "??:??:??"

    @property
    def preview(self) -> str:
        """Get preview of message content (first 80 chars)"""
        text = self.text_content.replace("\n", " ").strip()
        return text[:80] + "..." if len(text) > 80 else text

    @property
    def has_tool_use(self) -> bool:
        """Check if message contains any tool_use blocks"""
        for block in self.content_blocks:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                return True
        return False

    @property
    def has_tool_result(self) -> bool:
        """Check if message contains any tool_result blocks"""
        for block in self.content_blocks:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                return True
        return False

    @property
    def is_system_message(self) -> bool:
        """Check if this is a system-generated message (not real user input)"""
        text = self.text_content
        # Session continuation summaries
        if text.startswith("This session is being continued from a previous conversation"):
            return True
        # Task notifications from background agents
        if text.startswith("<task-notification>"):
            return True
        # Deliberately NOT is_wrapper_text: this gates load_messages, which feeds
        # the fidelity views (`get`, `last`, `messages`, `browse`). Wrapper tags
        # are the right test for a *summary* — topic, turn count — but several of
        # them carry text the human typed: a slash command with arguments is one
        # message opening with `<command-message>`, and `<bash-input>` is typed by
        # definition. Filtering those here silently deleted 1,056 messages across
        # 280 sessions, including 111 `<command-args>` instructions, and left
        # assistant replies answering nothing. Summarizing callers filter for
        # themselves.
        return False

    def is_valid_message(self) -> bool:
        """Check if this is a valid user or assistant message.

        Filters out:
        - Messages without text content
        - Assistant messages containing tool_use blocks (intermediate tool calls)
        - User messages that are tool_result responses (not actual human input)
        - Session continuation summaries
        """
        if self.type not in ["user", "assistant"]:
            return False
        if self.role not in ["user", "assistant"]:
            return False
        if not self.text_content.strip():
            return False
        # Filter out system-generated messages
        if self.role == "user" and self.is_system_message:
            return False
        # Filter out tool call/result messages
        if self.role == "assistant" and self.has_tool_use:
            return False
        if self.role == "user" and self.has_tool_result:
            return False
        return True


# -------- Filesystem helpers for project/session discovery --------


def get_project_last_activity(project_dir: Path) -> float:
    """Return the last activity time for a project (latest .jsonl mtime),
    falling back to directory mtime if none found."""
    latest: float = 0.0
    # Both guards are OSError only: the project dir can be unreadable or vanish
    # mid-scan (sessions are written live), and a 0.0 result is a meaningful
    # "no activity" the callers already sort on. Anything else here is a bug and
    # should surface rather than read as an empty project.
    try:
        for p in project_dir.iterdir():
            if p.is_file() and p.suffix == ".jsonl":
                m = p.stat().st_mtime
                if m > latest:
                    latest = m
    except OSError:
        pass
    if latest == 0.0:
        try:
            latest = project_dir.stat().st_mtime
        except OSError:
            latest = 0.0
    return latest


def list_projects(base_dir: str) -> List[Path]:
    """Return list of project directories inside the base dir, sorted by last activity desc."""
    root = Path(base_dir).expanduser()
    if not root.exists() or not root.is_dir():
        return []
    projects = [p for p in root.iterdir() if p.is_dir()]
    # Sort by most recent activity first, then by name to stabilize order
    return sorted(
        projects,
        key=lambda p: (-get_project_last_activity(p), p.name.lower()),
    )


def list_sessions(project_dir: Path) -> List[Path]:
    """Return list of session jsonl files sorted by modified time (newest first)."""
    if not project_dir.exists() or not project_dir.is_dir():
        return []
    sessions = [p for p in project_dir.iterdir() if p.is_file() and p.suffix == ".jsonl"]
    return sorted(sessions, key=lambda p: p.stat().st_mtime, reverse=True)


@dataclass(frozen=True)
class SessionRef:
    """One discovered session, independent of how its store is laid out on disk.

    Listing code used to pass around `(project_dir, session_path)` pairs, which
    bakes in the ~/.claude shape: one directory per project, the directory *name*
    carrying the project slug. That assumption is the thing keeping a second
    transcript store out — Codex partitions rollouts by date and records
    the project inside the file, so it has no such directory to key off.

    `project_key` is what the label and cwd helpers consume, so a producer that
    has no project directory can still synthesize one. `project_dir` stays
    available for the Claude path and is None elsewhere.
    """

    path: Path
    mtime: float
    project_key: str
    source: str = "claude"
    project_dir: Optional[Path] = None


_CODEX_HEAD_LINES = 6  # session_meta is the first record; tolerate a few blanks


def codex_sessions_dir() -> Path:
    """Codex CLI's rollout store. `CLAUDE_LS_CODEX_DIR` overrides it (tests, alt homes)."""
    override = os.environ.get("CLAUDE_LS_CODEX_DIR")
    return Path(override) if override else Path.home() / ".codex" / "sessions"


def _codex_session_meta(path: Path) -> Optional[Dict]:
    """The `session_meta` payload from a rollout's head, or None if absent.

    Bounded read — `session_meta` is the first record, so this never walks the
    body. One rollout in a 716-file store predates the record entirely; it just
    doesn't list rather than being special-cased.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= _CODEX_HEAD_LINES:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") == "session_meta":
                    payload = entry.get("payload")
                    return payload if isinstance(payload, dict) else None
    except (OSError, UnicodeDecodeError):
        return None
    return None


def session_short_id(ref: "SessionRef") -> str:
    """The 8-char id shown in listings, per store."""
    return codex_short_id(ref.path.stem) if ref.source == "codex" else ref.path.stem[:8]


def codex_project_key(path: Path, include_subagents: bool = False) -> Optional[str]:
    """The project key for a rollout, or None if it isn't one this tool lists.

    The single answer to "does this file count, and whose project is it?" — a
    rollout must be named `rollout-*.jsonl`, carry a `session_meta` header with a
    `cwd`, and (by default) not be a subagent thread.

    Both the listing walk and content search ask this, by different routes: the
    walk globs the store and asks about every file, while search lets ripgrep
    narrow first and asks only about the hits. When Codex adds a value to exclude,
    one edit here covers both. Two copies of this predicate drifting apart is a
    silent failure — the listing would filter and search wouldn't, with nothing to
    fail a test.
    """
    if not path.name.startswith("rollout-") or path.suffix != ".jsonl":
        return None
    meta = _codex_session_meta(path)
    if not meta:
        return None
    if not include_subagents and meta.get("thread_source") == "subagent":
        return None
    cwd = meta.get("cwd")
    return encode_path_to_folder_name(cwd) if cwd else None


def codex_sessions_by_project(
    slugs: Optional[Set[str]] = None, include_subagents: bool = False
) -> Dict[str, List[Path]]:
    """Rollout paths grouped by project key, newest first, with no per-project cap.

    `iter_codex_session_refs` exists to build a *listing*, so it carries a
    `per_project` cap. Reusing it to answer "which rollouts belong to this
    directory" would silently inherit that cap and hide sessions past the 20th --
    the same mistake that made 35% of a real store unsearchable before search
    grew its own walk. Callers here want every rollout for a slug or none.

    `slugs` narrows the walk to the projects the caller cares about; None groups
    the whole store, which is what the moved-directory scan needs.
    """
    root = codex_sessions_dir()
    out: Dict[str, List[Path]] = {}
    if not root.is_dir():
        return out
    for path in root.rglob("rollout-*.jsonl"):
        key = codex_project_key(path, include_subagents=include_subagents)
        if key is None or (slugs is not None and key not in slugs):
            continue
        out.setdefault(key, []).append(path)
    for paths in out.values():
        paths.sort(key=lambda q: -_safe_mtime(q))
    return out


def _safe_mtime(path: Path) -> float:
    """mtime, or 0 for a file that vanished mid-walk -- a store is live."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


# ---- Session listing sort keys ----
#
# Listings used to order by `touched`, the file's mtime. That is not when the
# conversation last moved: sidecar records (`atis-latch`, `pr-link`) land on
# finished transcripts and other tooling rewrites old files, so a transcript can
# be touched long after its final turn. Of the 12 most-recently-touched
# transcripts in a real store, 8 had drifted from their last message and 4 by
# more than a day (measured 2026-09-18) -- stale enough to float a two-day-old
# conversation to the top of a listing that claims to be newest-first. So
# `active`, read off the last message, is the default, and `touched` is the
# escape hatch for anyone who really does want "when was this file last written".
SESSION_SORT_KEYS = ("active", "touched", "created")

SESSION_SORT_DEFAULT = "active"

SESSION_SORT_HELP = (
    "Order by: active = the last message in the transcript (default); touched = "
    "file mtime, which can sit days ahead of the last message; created = the "
    "first message. The time column follows the key, so what you sort on is "
    "what you see."
)

# Header for the time column under each sort key -- a column labelled "When"
# while sorted by something else is how you end up distrusting the whole table.
SESSION_TIME_HEADER = {"touched": "When", "active": "Active", "created": "Started"}

_TAIL_SCAN_BYTES = 256 * 1024
_HEAD_SCAN_BYTES = 256 * 1024

# What counts as a message, per store. Claude names the record type; a Codex
# rollout wraps its turn in `payload` and types it there. Accepting any payload
# is not close enough: a rollout's lifecycle records (`task_complete`,
# `token_count`, `thread_settings_applied`) carry timestamps too, and in a census
# of 120 real rollouts the last timestamped record was one of those in every
# single one (2026-09-18). Sorting on that is sorting on bookkeeping.
_CLAUDE_MESSAGE_TYPES = frozenset({"user", "assistant"})
_CODEX_MESSAGE_TYPES = frozenset({"message", "user_message", "agent_message"})


def _is_message_record(obj: dict) -> bool:
    """Is this transcript record a turn someone took, in either store?"""
    if obj.get("type") in _CLAUDE_MESSAGE_TYPES:
        return True
    payload = obj.get("payload")
    return isinstance(payload, dict) and payload.get("type") in _CODEX_MESSAGE_TYPES


def _scan_message_times(
    path_str: str,
    offset: int = 0,
    stop_early: bool = False,
    budget: Optional[int] = None,
) -> Optional[float]:
    """Epoch of a message at or after `offset`: the first one when `stop_early`,
    otherwise the last. `budget` stops the walk after that many bytes. None when
    the window holds no message at all."""
    found: Optional[float] = None
    try:
        with open(path_str, "rb") as fh:
            if offset:
                fh.seek(offset)
                fh.readline()  # the seek lands mid-line, and that fragment isn't JSON
            for raw in fh:
                try:
                    obj = json.loads(raw)
                except ValueError:  # JSONDecodeError, plus UnicodeDecodeError on junk
                    continue
                if isinstance(obj, dict) and _is_message_record(obj):
                    ts = _parse_ts(obj.get("timestamp", ""))
                    if ts:
                        found = ts.timestamp()
                        if stop_early:
                            return found
                if budget is not None and fh.tell() > budget:
                    break
    except OSError:
        return None
    return found


def _scan_last_message_time(path_str: str, offset: int) -> Optional[float]:
    """Epoch of the last message at or after `offset`, or None if there is none."""
    return _scan_message_times(path_str, offset)


@functools.lru_cache(maxsize=4096)
def _cached_last_message_time(path_str: str, _mtime: float, size: int) -> Optional[float]:
    # Keyed on mtime and size as well as path: a live transcript grows under us.
    if size <= _TAIL_SCAN_BYTES:
        return _scan_last_message_time(path_str, 0)
    latest = _scan_last_message_time(path_str, size - _TAIL_SCAN_BYTES)
    # A tail of nothing but sidecar lines is unlikely but possible; pay for the
    # whole read rather than fall back to the mtime this key exists to distrust.
    return latest if latest is not None else _scan_last_message_time(path_str, 0)


@functools.lru_cache(maxsize=4096)
def _cached_first_message_time(path_str: str, _mtime: float, _size: int) -> Optional[float]:
    # Bounded, because "the first message" is a head fact: a transcript that opens
    # with a megabyte of metadata is not worth reading to the end to place in a list.
    return _scan_message_times(path_str, stop_early=True, budget=_HEAD_SCAN_BYTES)


def first_message_time(path: Path) -> Optional[float]:
    """Epoch of the first message in a transcript, or None if it holds none.

    Not `_head_facts().started`, which is the first *timestamped record* of any
    kind. Every Codex rollout opens with a timestamped `session_meta`, so that
    number is when the session was created, not when anyone said anything.
    """
    try:
        st = path.stat()
    except OSError:
        return None
    return _cached_first_message_time(str(path), st.st_mtime, st.st_size)


def last_message_time(path: Path) -> Optional[float]:
    """Epoch of the last message in a transcript, or None if it holds none.

    Read from the tail of the file rather than taken from its mtime: see
    SESSION_SORT_KEYS for why those two disagree.
    """
    try:
        st = path.stat()
    except OSError:
        return None
    return _cached_last_message_time(str(path), st.st_mtime, st.st_size)


def session_sort_time(
    path: Path, key: str = SESSION_SORT_DEFAULT, mtime: Optional[float] = None
) -> float:
    """The epoch a listing should order and label a session by, under `key`.

    Falls back to the mtime when the transcript carries no usable timestamp: an
    empty or truncated file still has to sort somewhere.
    """
    fallback = mtime if mtime is not None else _safe_mtime(path)
    ts = None
    if key == "created":
        ts = first_message_time(path)
    elif key == "active":
        ts = last_message_time(path)
    return ts if ts is not None else fallback


# A message can't be written before the file that holds it, so a transcript's
# mtime is an upper bound on both other sort keys. Checked against a real store:
# of 831 transcripts with a last-message time, none overshot its mtime by even a
# second (2026-09-18). Walking candidates newest-mtime-first therefore lets the
# ranking stop as soon as the next mtime can no longer beat the worst entry in
# the top N -- which is what lets a listing rank a whole store exactly while
# reading a few dozen files instead of every one.
#
# The slack is insurance against the one way that bound could fail: a clock that
# stepped backwards between a message being written and the file being closed.
_SORT_BOUND_SLACK_S = 3600

T = TypeVar("T")


def rank_sessions(
    entries: Sequence[Tuple[Path, float, T]], key: str, limit: Optional[int] = None
) -> List[T]:
    """Order `(path, mtime, item)` triples newest-first under `key`, returning the
    items. With `limit`, only the top N -- and only as much of the store is read
    as proving that N takes.
    """
    if limit is not None and limit <= 0:
        # `del scored[0:]` would empty the ledger and the cutoff read below would
        # raise. A caller asking for no rows wants no rows, not a traceback.
        return []
    ordered = sorted(entries, key=lambda e: -e[1])
    if key == "touched":
        return [item for _path, _mtime, item in ordered[:limit]]

    scored: List[Tuple[float, int, T]] = []
    cutoff: Optional[float] = None  # worst score currently inside the top `limit`
    for i, (path, mtime, item) in enumerate(ordered):
        if cutoff is not None and mtime <= cutoff - _SORT_BOUND_SLACK_S:
            break  # every remaining candidate has an older mtime, so an older score
        # The enumeration index keeps ties in mtime order, so a listing doesn't
        # reshuffle between runs when two sessions share a timestamp.
        scored.append((session_sort_time(path, key, mtime), i, item))
        if limit is not None and len(scored) >= limit:
            scored.sort(key=lambda entry: (-entry[0], entry[1]))
            del scored[limit:]
            cutoff = scored[-1][0]
    scored.sort(key=lambda entry: (-entry[0], entry[1]))
    return [item for _score, _i, item in scored[:limit]]


def iter_codex_session_refs(
    per_project: Optional[int] = 20,
    include_subagents: bool = False,
) -> List[SessionRef]:
    """Codex rollouts as refs, newest first.

    Rollouts are partitioned by date rather than project and record their working
    directory inside the file, so grouping costs one bounded head-read each. That
    measured ~80ms for 716 rollouts, which is why there's no cache here — it would
    be complexity bought against a cost that isn't there.

    `project_key` is the *same* slug the Claude store uses for that directory, so a
    repo worked in both agents collapses into one project instead of two.

    Subagent threads are excluded by default, mirroring the Claude path (they live
    in subdirs there). They're 28% of a real store and are noise in a session list.
    """
    root = codex_sessions_dir()
    if not root.is_dir():
        return []
    by_project: Dict[str, List[SessionRef]] = {}
    for path in root.rglob("rollout-*.jsonl"):
        key = codex_project_key(path, include_subagents=include_subagents)
        if key is None:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        by_project.setdefault(key, []).append(
            SessionRef(path=path, mtime=mtime, project_key=key, source="codex")
        )
    # Cap per project, matching the Claude walk, before the global sort.
    refs: List[SessionRef] = []
    for group in by_project.values():
        group.sort(key=lambda r: -r.mtime)
        refs.extend(group[:per_project] if per_project is not None else group)
    refs.sort(key=lambda r: -r.mtime)
    return refs


def iter_session_refs(
    base_dir: str,
    project_keys: Optional[Set[str]] = None,
    per_project: Optional[int] = 20,
    include_codex: bool = False,
) -> List[SessionRef]:
    """Sessions across the given projects (default: all), newest first.

    The one place that knows how sessions are laid out. A second store is a
    producer added here, not a change at the call sites.

    `list_projects` / `list_sessions` are deliberately left as-is underneath:
    claude-graph re-exports both through its seam, so their signatures are
    public API from this repo's point of view.

    Scoping is by project *key*, not by Claude project directory. A directory
    list can only ever name projects that exist under ~/.claude, which would
    silently drop every project that has only ever run under Codex — keys are
    the one identifier both stores share.
    """
    scope = list_projects(base_dir)
    if project_keys is not None:
        scope = [p for p in scope if p.name in project_keys]
    refs: List[SessionRef] = []
    for project in scope:
        sessions = list_sessions(project)
        for session in sessions[:per_project] if per_project is not None else sessions:
            try:
                mtime = session.stat().st_mtime
            except OSError:
                # Vanished between the directory read and the stat — sessions are
                # written live, so this is a normal race, not an error.
                continue
            refs.append(
                SessionRef(
                    path=session,
                    mtime=mtime,
                    project_key=project.name,
                    source="claude",
                    project_dir=project,
                )
            )
    if include_codex:
        codex = iter_codex_session_refs(per_project=per_project)
        if project_keys is not None:
            codex = [r for r in codex if r.project_key in project_keys]
        refs.extend(codex)
    refs.sort(key=lambda r: -r.mtime)
    return refs


def format_project_identifier(project_folder_name: str, max_segments: int = 3) -> str:
    """Create a human-friendly identifier from a flattened path-like folder name.

    Many project folders are path-flattened using '-' separators (e.g., '-Users-you-projects-myrepo').
    We split on '-' and join the last segments with '/', ignoring empty parts, to give a compact tail view.
    """
    parts = [part for part in project_folder_name.split("-") if part]
    if not parts:
        return project_folder_name
    tail = parts[-max_segments:]
    return "/".join(tail)


def _naive_cwd(folder_name: str) -> str:
    """Naive reverse-encoding (every '-' becomes '/'). Used as fallback when
    the filesystem walk turns up nothing.
    """
    path = "/" + folder_name.lstrip("-").replace("-", "/")
    home = str(Path.home())
    if path.startswith(home):
        path = "~" + path[len(home) :]
    return path


def resolve_project_folder(folder_name: str) -> List[Path]:
    """Resolve a Claude flattened project folder name to existing filesystem paths.

    Claude flattens cwd by replacing '/' with '-', which collides with real
    hyphens in directory names (e.g. `worktrees-acme/PROJ-12-fix` and
    `worktrees/acme/PROJ/12/fix` flatten to the same string). This walks
    the filesystem top-down, at each step trying to merge the next 1..k tokens
    with '-' to find a directory that actually exists.

    Returns all valid resolutions (typically 1; rarely more if multiple
    interpretations correspond to real directories).
    """
    parts = [p for p in folder_name.lstrip("-").split("-") if p]
    if not parts:
        return []

    results: List[Path] = []

    def walk(current: Path, idx: int) -> None:
        if idx >= len(parts):
            results.append(current)
            return
        for k in range(1, len(parts) - idx + 1):
            segment = "-".join(parts[idx : idx + k])
            candidate = current / segment
            try:
                if candidate.is_dir():
                    walk(candidate, idx + k)
            except OSError:
                continue

    walk(Path("/"), 0)
    return results


@functools.lru_cache(maxsize=512)
def project_folder_to_cwd(folder_name: str) -> str:
    """Reconstruct the working directory for a Claude project folder.

    First tries an FS-walking resolver (disambiguates '/' vs '-'). Falls back
    to the naive '-'→'/' substitution if nothing on disk matches (e.g. the
    project directory has been moved or deleted).
    """
    resolved = resolve_project_folder(folder_name)
    if len(resolved) == 1:
        path = str(resolved[0])
    elif len(resolved) > 1:
        # Multiple matches — pick the shortest path (fewest separators = most
        # hyphens preserved, usually the right one for worktree-style names).
        path = str(min(resolved, key=lambda p: len(p.parts)))
    else:
        return _naive_cwd(folder_name)
    home = str(Path.home())
    if path.startswith(home):
        path = "~" + path[len(home) :]
    return path


def find_project_by_hint(projects: List[Path], hint: str) -> Optional[Path]:
    """Find a project by substring match (case-insensitive). Falls back to exact if multiple."""
    if not hint:
        return None
    hint_lower = hint.lower()
    matches = [
        p
        for p in projects
        if hint_lower in p.name.lower() or hint_lower in format_project_identifier(p.name).lower()
    ]
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    # Prefer exact folder name match if ambiguous
    for p in matches:
        if p.name.lower() == hint_lower:
            return p
    return matches[0]


def find_project_key_by_hint(keys: List[str], hint: str) -> Optional[str]:
    """`find_project_by_hint` over project keys rather than Claude directories.

    Same matching and tie-breaking, but a key doesn't require a directory to
    exist — which is what lets `--project` select a project that only ever ran
    under Codex.
    """
    if not hint:
        return None
    hint_lower = hint.lower()
    matches = [
        k
        for k in keys
        if hint_lower in k.lower() or hint_lower in format_project_identifier(k).lower()
    ]
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    for k in matches:
        if k.lower() == hint_lower:
            return k
    return matches[0]


def encode_path_to_folder_name(path: str) -> str:
    """Encode an absolute path into Claude's project-folder slug.

    Claude replaces every non-alphanumeric character with '-' (1:1, no collapsing),
    per the Claude Code sessions docs — so '/a/b.c_d' becomes '-a-b-c-d' and the
    leading '/' becomes the leading '-'. Replacing only '/' (the obvious separator)
    silently misses dirs containing '.', '_', spaces, etc., so the exact-match
    auto-detect would fail for them.
    """
    return re.sub(r"[^a-zA-Z0-9]", "-", path)


def cwd_to_project_name(cwd: Optional[str] = None) -> str:
    """Convert a working directory to Claude's flattened project folder naming convention.

    Defaults to the process CWD; pass `cwd` to resolve some other directory (e.g.
    a sibling Claude process's working dir, for fleet tooling).

    Example: /Users/you/projects/my-repo -> -Users-you-projects-my-repo
    """
    return encode_path_to_folder_name(cwd if cwd is not None else os.getcwd())


def find_project_from_cwd(base_dir: str, cwd: Optional[str] = None) -> Optional[Path]:
    """Find the Claude project that matches a working directory (default: process CWD).

    Returns the project path if found, None otherwise.
    Uses exact match only - no fuzzy matching to avoid ambiguity.
    """
    projects = list_projects(base_dir)
    if not projects:
        return None

    # Try exact match only (no fuzzy matching to avoid picking wrong project)
    expected_name = cwd_to_project_name(cwd)
    for project in projects:
        if project.name == expected_name:
            return project

    return None


# Backstop on how many projects get a header read while looking for a renamed
# anchor. It doesn't bind on a normal store (~100 projects) and isn't there to
# make the scan fast — measured at ~20ms across a 95-project store — only to keep
# a pathological store from turning a miss message into a pause.
MOVED_PROJECT_SCAN_LIMIT = 200

# Candidates given the edited-path probe. It has to cover more than the print
# limit: corroboration is what does the ranking, so a budget of `limit` could only
# reorder projects that were already going to be shown — and a directory renamed
# *because* work there wound down is exactly the one that ranks low on activity.
# Vanished siblings accumulate and cluster — a store here carried several under a
# single scratch parent — so the busy ones would otherwise fill every slot.
MOVED_PROJECT_CORROBORATE_LIMIT = 12

# The probe reads a transcript HEAD, not a whole transcript: enough lines to find
# a few edited paths, then stop. Transcripts reach 28 MB here, and the directory
# you renamed is where the big one lives — full reads are what made the first
# version too expensive to run on more than a handful of candidates.
MOVED_EVIDENCE_LINES = 2000
MOVED_EVIDENCE_PROBES = 8

# Distinct surviving paths before a candidate is called a rename. One is not
# enough: candidates are siblings in one parent, so they are disproportionately
# alike, and a lone README.md or package.json matches across unrelated projects.
MOVED_EVIDENCE_HITS = 2

# Header reads per project before giving up on finding a recorded cwd. Claude
# stamps one on the opening event, so the newest session almost always has it;
# a summary-only stub is the case worth stepping past.
MOVED_PROJECT_HEADER_TRIES = 3


@dataclass(frozen=True)
class MovedProject:
    """A vanished directory whose sessions may belong to the cwd under a new name."""

    project: Path
    old_cwd: str
    sessions: int
    corroborated: bool  # files it edited are present under the current directory


def _dir_entry_exists(parent: str, name: str, on_error: bool) -> bool:
    """Case-exact test for `name` in `parent`.

    `os.path.exists` is case-INSENSITIVE on APFS, so it reports a directory renamed
    `foo` -> `FOO` as still present. The slug match that stranded the sessions is
    case-sensitive, so that rename is exactly a case this has to see.

    `on_error` is what an unreadable or missing parent means to the caller, and the
    two callers disagree: for "is the old directory still there?" an indeterminate
    answer must not read as gone (that would invent a rename candidate), while for
    "did this file survive?" it must not read as present (that would invent
    evidence). Neither has a safe default, so it's required.
    """
    try:
        return name in os.listdir(parent)
    except OSError:
        return on_error


def _codex_edited_paths_head(session: Path, want: int, max_lines: int) -> List[str]:
    """`_edited_paths_head` for a rollout: apply_patch headers, not tool_use blocks.

    Codex records an edit as an `apply_patch` payload whose text carries
    `*** Update File: <path>`, so the paths come from a regex over the record text
    rather than from a typed field. Each line is decoded and the regex run over
    the strings inside it, because a shell call's `arguments` is a JSON string
    holding more JSON: against the raw line the header sits two levels of escaping
    deep and the filename captures the trailing backslash of `\n`. Still a bounded
    head-read -- one json.loads per line, the same as the Claude branch, rather
    than the whole-transcript parse `derive_work_signals` does.

    Paths are returned absolute. apply_patch writes them relative to the session's
    cwd, and the caller compares them against a candidate directory with
    `os.path.relpath`, so a relative path here would silently resolve against
    wherever claude-ls happens to be running.
    """
    base_cwd = (_codex_session_meta(session) or {}).get("cwd") or ""
    out: List[str] = []
    try:
        with open(session, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= max_lines or len(out) >= want:
                    break
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Decode first, then match. A shell call's `arguments` is a JSON
                # string holding more JSON, so the patch header sits two levels of
                # escaping deep in the raw line -- matching there captures the
                # trailing backslash of `\\n` as part of the filename.
                for text in _iter_str_values(entry):
                    for m in _CODEX_PATCH_FILE_RE.finditer(text):
                        fp = m.group(1).strip()
                        if not fp:
                            continue
                        if not os.path.isabs(fp):
                            if not base_cwd:
                                continue  # unanchored: nothing to judge it against
                            fp = os.path.normpath(os.path.join(base_cwd, fp))
                        if fp not in out:
                            out.append(fp)
                        if len(out) >= want:
                            break
    except OSError:
        return out
    return out


def _iter_str_values(obj, depth: int = 6):
    """Every string in a decoded record, depth-bounded so a pathological one can't
    walk forever."""
    if depth < 0:
        return
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_str_values(v, depth - 1)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_str_values(v, depth - 1)


def _edited_paths_head(
    session: Path, want: int = MOVED_EVIDENCE_PROBES, max_lines: int = MOVED_EVIDENCE_LINES
) -> List[str]:
    """First `want` distinct file paths a session edited, from its opening lines.

    `derive_work_signals` needs the whole transcript parsed, which is far too much
    to spend on a diagnostic — this walks the head and stops as soon as it has
    enough. Decode errors are replaced rather than raised: a transcript that isn't
    valid UTF-8 must not turn a miss message into a traceback.
    """
    if _file_is_codex(session):
        return _codex_edited_paths_head(session, want, max_lines)

    out: List[str] = []
    try:
        with open(session, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= max_lines or len(out) >= want:
                    break
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue  # a line can be valid JSON and not an object (a bare array)
                msg = entry.get("message")
                content = msg.get("content") if isinstance(msg, dict) else None
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    if block.get("name") not in _EDIT_TOOLS:
                        continue
                    tin = block.get("input")
                    if not isinstance(tin, dict):
                        continue
                    fp = tin.get("file_path") or tin.get("notebook_path") or ""
                    if fp and fp not in out:
                        out.append(fp)
    except OSError:
        return out
    return out


def _moved_file_evidence(old_cwd: str, start: str, sessions: List[Path]) -> bool:
    """True when files the stranded sessions edited under `old_cwd` survive under `start`.

    A rename keeps the directory's contents; a deletion doesn't. Without this, a
    scratch directory someone removed is indistinguishable from a rename — and a
    store accumulates those, so the hint would name three deleted siblings with the
    same confidence as a real rename.

    Walks several sessions because the newest one may be all discussion and no
    edits, and requires MOVED_EVIDENCE_HITS distinct survivors so that one
    ubiquitous filename can't carry a candidate on its own. Distinct is across all
    the sessions, not within one: two sessions of a project editing the same
    README.md is the ordinary case, and counting it twice is exactly the
    single-common-filename result the threshold exists to reject.
    """
    hits = 0
    checked = 0
    # Per call, though nothing observable depends on that: paths are absolute and
    # each candidate has its own old_cwd, so two candidates' keys never collide.
    probed: Set[str] = set()
    for session in sessions[:MOVED_PROJECT_HEADER_TRIES]:
        for path in _edited_paths_head(session):
            if path in probed:
                continue
            probed.add(path)
            rel = os.path.relpath(path, old_cwd)
            if rel == os.pardir or rel.startswith(os.pardir + os.sep):
                continue  # edited outside the old dir; says nothing about the rename
            probe_parent, probe_name = os.path.split(os.path.join(start, rel))
            if probe_name and _dir_entry_exists(probe_parent, probe_name, on_error=False):
                hits += 1
                if hits >= MOVED_EVIDENCE_HITS:
                    return True
            checked += 1
            if checked >= MOVED_EVIDENCE_PROBES:
                return False
    return False


def find_moved_project_candidates(
    base_dir: str, cwd: Optional[str] = None, limit: int = 3, codex: bool = False
) -> List[MovedProject]:
    """Projects that look like this directory under a previous name.

    Renaming a directory strands its sessions: Claude files them under the slug of
    the path they were launched from, and `find_project_from_cwd` matches that slug
    exactly, on purpose. Nothing on disk records the rename, so this deliberately
    does NOT resolve it — no fuzzy slug matching, no silent adoption. It gathers
    candidates for a hint a human can act on, which is the difference between a
    command that says nothing and one that says where its sessions went.

    The evidence is the transcript, not the folder name: a session stamps its launch
    `cwd` verbatim, which reads back losslessly where a slug cannot ('-' can't be
    told from '/'). A candidate is a project whose recorded cwd (a) is gone from
    disk, (b) sat in the same parent directory as `cwd` — a vanished sibling, which
    is what a rename in place looks like from the new name — and (c) re-encodes to
    that project's own folder name, since otherwise the `--dir <old>` the caller is
    told to run would resolve to a different project or none at all.

    `corroborated` marks the ones whose sessions edited files that exist under `cwd`
    now (see `_moved_file_evidence`); those sort first, because a plain deletion
    satisfies (a)-(c) just as well as a rename does. Corroboration therefore has to
    reach further down the list than `limit`, or it could only reorder candidates
    that were already going to be printed.
    """
    start = os.path.abspath(os.path.expanduser(cwd if cwd is not None else os.getcwd()))
    parent = os.path.dirname(start)
    # Symlinks: compare the parents by identity, not spelling, so reaching the same
    # directory through a linked path still recognizes its siblings.
    parent_real = os.path.realpath(parent)

    # Both stores, one loop: a rename strands rollouts exactly as it strands Claude
    # sessions, and a project worked in both agents encodes to the same slug, so
    # its two halves must be judged as one candidate rather than offered twice.
    scan: List[Tuple[Path, str, List[Path]]] = [
        (proj, proj.name, list_sessions(proj))
        for proj in list_projects(base_dir)[:MOVED_PROJECT_SCAN_LIMIT]
    ]
    if codex:
        claude_slugs = {name for _, name, _ in scan}
        for slug, rollouts in codex_sessions_by_project().items():
            if slug in claude_slugs:
                # Same directory under both agents. Extending the Claude entry keeps
                # the session count honest and the candidate single.
                for entry in scan:
                    if entry[1] == slug:
                        entry[2].extend(rollouts)
                        break
            else:
                scan.append((codex_sessions_dir(), slug, rollouts))

    found: List[MovedProject] = []
    budget = MOVED_PROJECT_CORROBORATE_LIMIT
    for project, project_name, sessions in scan:
        if not sessions:
            continue
        recorded = next(
            (
                c
                for c in (
                    read_session_header(s).get("cwd") for s in sessions[:MOVED_PROJECT_HEADER_TRIES]
                )
                if c
            ),
            None,
        )
        if not recorded or recorded == start:
            continue
        old_parent, old_name = os.path.split(recorded.rstrip(os.sep))
        # '/' splits to ('', '') and a bare name to ('', name); realpath('') is the
        # process cwd, which would let an unrelated parent compare equal.
        if not old_parent or not old_name:
            continue
        if os.path.realpath(old_parent) != parent_real:
            continue
        if _dir_entry_exists(old_parent, old_name, on_error=True):
            continue
        # The hint tells the caller to run `here --dir <old>`, which re-derives the
        # slug from that path. A project whose folder name disagrees with its own
        # recorded cwd would send them to a command that lands nowhere, so it isn't
        # offered. This does drop a real class silently: a worktree-launched session
        # can record the main checkout rather than the worktree it ran in.
        if encode_path_to_folder_name(recorded) != project_name:
            continue
        corroborated = False
        if budget:
            budget -= 1
            corroborated = _moved_file_evidence(recorded, start, sessions)
        found.append(MovedProject(project, recorded, len(sessions), corroborated))

    found.sort(key=lambda m: not m.corroborated)  # stable: keeps activity order within a group
    return found[:limit]


def print_moved_project_hint(base_dir: str, cwd: Optional[str] = None, codex: bool = False) -> None:
    """Name vanished sibling directories whose sessions may be this directory's.

    Goes to stderr with the other diagnostics, so a caller piping the command's
    real output is unaffected.
    """
    candidates = find_moved_project_candidates(base_dir, cwd, codex=codex)
    for m in candidates:
        plural = "" if m.sessions == 1 else "s"
        if m.corroborated:
            why = f"and its {m.sessions} session{plural} edited files that are here — renamed?"
        else:
            why = f"({m.sessions} session{plural}) — this directory's old name?"
        print(f"  {_tilde(m.old_cwd)} is gone {why}", file=sys.stderr)
        # The command has to reproduce the scan that found the candidate. Without
        # --codex, a Codex-only project is named and then shows nothing when the
        # reader runs what they were told to run.
        flag = " --codex" if codex else ""
        print(
            f"    claude-ls here{flag} --dir {shlex.quote(_tilde(m.old_cwd))}",
            file=sys.stderr,
        )


def copy_to_clipboard(text: str) -> Tuple[bool, str]:
    """Copy text to clipboard on macOS using pbcopy."""
    try:
        if platform.system() != "Darwin":
            return (False, "Clipboard copy only supported on macOS")
        p = subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=False)
        return (p.returncode == 0, "Copied to clipboard" if p.returncode == 0 else "pbcopy failed")
    except OSError as e:
        # pbcopy missing or unspawnable. Nothing else here can fail: the platform
        # check is a string compare and encoding a str to UTF-8 always succeeds.
        return (False, f"Clipboard error: {e}")


def get_session_last_message(
    session_path: Path, cache: Dict[str, Optional[Tuple[str, str]]]
) -> Optional[Tuple[str, str]]:
    """Get the last message from a session file with caching.

    Returns:
        Tuple of (role, preview_text) for the most recent message, or None if no messages found.
    """
    cache_key = str(session_path)

    # Check cache first
    if cache_key in cache:
        return cache[cache_key]

    try:
        # TODO: Optimize to read only last few lines instead of entire file
        messages = load_messages(str(session_path), "all")
        if not messages:
            cache[cache_key] = None
            return None

        # Get the most recent message
        last_msg = messages[-1]
        result = (last_msg.role, last_msg.preview)

        # Cache the result
        cache[cache_key] = result
        return result

    except (FileNotFoundError, PermissionError):
        # File access issues - cache the failure to avoid repeated attempts
        cache[cache_key] = None
        return None
    except (json.JSONDecodeError, KeyError, IndexError):
        # File parsing/format issues - cache the failure
        cache[cache_key] = None
        return None
    except (OSError, UnicodeDecodeError):
        # What's left after the two arms above: a path that's a directory, a
        # transcript that isn't UTF-8, or an I/O error mid-read. Deliberately
        # still uncached — an unreadable file now may be readable next pass, and
        # this cache lives for the whole run.
        return None


def read_session_header(session_file, max_lines: int = 40) -> Dict[str, Optional[str]]:
    """Extract the session's recorded working directory and git branch.

    Claude stamps `cwd` and `gitBranch` onto the transcript's opening event lines
    (an absolute path and the live branch). Reading them back is lossless — unlike
    decoding the flattened folder name, which can't tell a path '/' from a literal
    '-'. We scan a small head window and stop as soon as both are found, so this
    stays cheap even on huge transcripts. Missing/unreadable file or absent fields
    degrade to None. Returns {"cwd": <str|None>, "git_branch": <str|None>}.

    First occurrence, not most-recent, is deliberate: `cwd` is stamped per-event
    and ~40% of sessions visit several directories, but the LAUNCH dir is the one a
    live process reports via `lsof` (its OS cwd stays put even as the session `cd`s
    around). Fleet tooling joins a session to a running process on this value, so
    the first (anchor) cwd is what makes that join land; the most-recent cwd would
    be a transient subdir the process never chdir's into. Surfacing where a session
    later roamed is a separate concern (a tail read), tracked outside this helper.
    """
    if _file_is_codex(session_file):
        # A rollout stamps its launch cwd once, in the session_meta header, rather
        # than on every event -- same fact, different place. Without this the four
        # callers that ask a session where it ran get None for every rollout.
        meta = _codex_session_meta(Path(session_file)) or {}
        return {"cwd": meta.get("cwd"), "git_branch": meta.get("git_branch")}

    cwd = branch = None
    try:
        with open(session_file, "r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= max_lines:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # A line may be valid JSON yet not an object (e.g. a bare array);
                # skip it rather than let .get() raise and break the "never raises"
                # contract callers like pending --json depend on.
                if not isinstance(obj, dict):
                    continue
                if cwd is None and obj.get("cwd"):
                    cwd = obj["cwd"]
                if branch is None and obj.get("gitBranch"):
                    branch = obj["gitBranch"]
                if cwd is not None and branch is not None:
                    break
    except OSError:
        pass
    return {"cwd": cwd, "git_branch": branch}


# ====================================================================
# Lineage across /clear
#
# `/clear` keeps the process, pane and registry entry, mints a new session id,
# and starts a new transcript. Every record the process writes after that
# carries `session_id`, the session it was first started as, beside
# `sessionId`, the current one. It is a root pointer: a pane cleared three
# times has three transcripts all naming the first. Read it from the head: a
# session later resumed with `claude -r` in a fresh process appends records
# without the field (seen 2026-09-05), so the tail can be silent. Retention
# deletes roots before their descendants, so a chain is gathered by shared
# origin, never walked from its root.
# ====================================================================

_HEAD_FACTS_LINES = 80


@dataclass(frozen=True)
class _HeadFacts:
    session_id: str
    origin: Optional[str]  # the root session's id when this transcript was cleared from it
    started: Optional[float]  # epoch of the first timestamp


@functools.lru_cache(maxsize=4096)
def _head_facts(path_str: str) -> _HeadFacts:
    sid = origin = None
    started = None
    try:
        with open(path_str, "r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= _HEAD_FACTS_LINES:
                    break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if started is None:
                    ts = _parse_ts(obj.get("timestamp", ""))
                    if ts:
                        started = ts.timestamp()
                if obj.get("type") not in ("user", "assistant"):
                    continue
                sid = sid or obj.get("sessionId")
                if not origin and obj.get("session_id"):
                    origin = obj["session_id"]
                if sid and origin and started is not None:
                    break
    except OSError:
        pass
    if not sid:
        sid = Path(path_str).stem
    return _HeadFacts(sid, origin if origin and origin != sid else None, started)


def session_origin(session_file) -> Optional[str]:
    """The root session this transcript was `/clear`ed from, or None for a root
    session and for transcripts from builds that predate the field."""
    return _head_facts(str(session_file)).origin


@dataclass(frozen=True)
class ChainMember:
    path: Path
    session_id: str
    started: float
    current: bool


def session_chain(session_file) -> List[ChainMember]:
    """Every transcript in the same project folder on this session's pane
    lineage, oldest first: the root, if still on disk, and each session cleared
    from it. A root with no descendants yields just itself."""
    session_file = Path(session_file)
    root = session_origin(session_file) or session_file.stem
    members: List[ChainMember] = []
    for f in list_sessions(session_file.parent):
        if f.name.startswith("agent-"):
            continue
        if f.stem == root or session_origin(f) == root:
            facts = _head_facts(str(f))
            try:
                fallback = f.stat().st_mtime
            except OSError:
                fallback = 0.0
            started = facts.started if facts.started is not None else fallback
            members.append(ChainMember(f, f.stem, started, f == session_file))
    members.sort(key=lambda m: m.started)
    return members


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def format_lineage(chain: List[ChainMember], session_id: str, origin: Optional[str]) -> str:
    """One line for the card: where this session sits among the sessions its
    pane has been cleared through. '' when it is alone and was never cleared."""
    if not origin and len(chain) <= 1:
        return ""
    ids = [m.session_id for m in chain]
    parts: List[str] = []
    if origin:
        gone = "" if origin in ids else " (root transcript gone)"
        parts.append(f"cleared from {origin[:8]}{gone}")
    if len(chain) > 1:
        pos = ids.index(session_id) + 1 if session_id in ids else 0
        if pos:
            parts.append(f"{_ordinal(pos)} of {len(chain)} in this pane")
            earlier = [i[:8] for i in ids[: pos - 1]][-3:]
            later = [i[:8] for i in ids[pos:]][:3]
            if earlier:
                parts.append("earlier " + ", ".join(earlier))
            if later:
                parts.append("later " + ", ".join(later))
        else:
            parts.append(f"{len(chain)} in this pane")
    return " · ".join(parts)


def _live_descendant(
    chain: List[ChainMember], session_id: str
) -> Optional[Tuple[Dict, ChainMember]]:
    """(registry record, member) when a later member of this session's chain is
    running right now. Resuming the earlier session would open its transcript
    in a second process while the pane carries on with the later one."""
    ids = [m.session_id for m in chain]
    if session_id not in ids:
        return None
    later = {m.session_id: m for m in chain[ids.index(session_id) + 1 :]}
    if not later:
        return None
    for rec in _live_registry():
        if rec.get("sessionId") in later:
            return rec, later[rec["sessionId"]]
    return None


def _lineage_note(origin: Optional[str]) -> str:
    return f"↺ cleared from {origin[:8]}" if origin else ""


def split_flattened_name(name: str) -> List[str]:
    """Split hyphen-flattened folder name into non-empty segments."""
    return [part for part in name.split("-") if part]


def collapsed_project_label(name: str, tail_segments: int = 2) -> str:
    """Return a compact label using only the last N segments, joined with '/'."""
    parts = split_flattened_name(name)
    if not parts:
        return name
    tail = parts[-tail_segments:] if len(parts) >= tail_segments else parts
    return "/".join(tail)


# -------- Colored header helpers (deterministic per prefix) --------
_project_header_color_cache: Dict[str, str] = {}


def _get_header_color(prefix_key: str, term) -> str:
    """Deterministically map a prefix key to a terminal color name."""
    if prefix_key in _project_header_color_cache:
        return _project_header_color_cache[prefix_key]
    colors = [
        "red",
        "green",
        "yellow",
        "blue",
        "magenta",
        "cyan",
        "bright_red",
        "bright_green",
        "bright_yellow",
        "bright_blue",
        "bright_magenta",
        "bright_cyan",
    ]
    digest = hashlib.sha256(prefix_key.encode()).hexdigest()
    idx = int(digest[:8], 16) % len(colors)
    color_name = colors[idx]
    _project_header_color_cache[prefix_key] = color_name
    return color_name


def _muted_color(term) -> str:
    """Return a muted gray color sequence, falling back for low-color terms."""
    # blessed degrades on its own — every TERM value, including unset and unknown,
    # reports 0 colors and still returns a usable (empty) sequence. So these guards
    # exist for a `term` that isn't a real Terminal: no .color attribute, or one
    # that isn't callable. Narrow to that, so a genuine blessed failure isn't
    # silently rendered as unstyled text.
    try:
        if getattr(term, "number_of_colors", 0) >= 256:
            return term.color(240)  # dark gray
    except (AttributeError, TypeError):
        pass
    # Fallback to bright_black (index 8) if 16-color
    try:
        return term.color(8)
    except (AttributeError, TypeError):
        return ""


def build_project_list_display(
    projects_list: List[Path], term
) -> Tuple[List[str], List[Optional[int]]]:
    """Build grouped project labels with multi-level headers.

    - Top-level header: first 2 segments in brackets, prefixed with a colored small circle.
    - Second-level header: next N segments (up to 2) shown with a leading '>' indicator.
    - Item rows: last two segments joined with '-' and a leading hyphen.
    Headers are non-selectable (mapped to None). Grouping is adjacency-based.
    """
    lines: List[str] = []
    mapping: List[Optional[int]] = []
    current_top: Optional[str] = None
    current_sub: Optional[str] = None

    for idx, proj in enumerate(projects_list):
        parts = split_flattened_name(proj.name)

        # Derive suffix (last two tokens)
        if len(parts) >= 2:
            suffix = "-".join(parts[-2:])
        elif len(parts) == 1:
            suffix = parts[0]
        else:
            suffix = proj.name

        # Derive hierarchical headers from remaining prefix
        prefix_parts = parts[:-2] if len(parts) > 2 else []

        # Chunk into pairs for readability
        def as_chunk(p: List[str], start: int) -> str:
            return "-".join(p[start : start + 2]) if start < len(p) else ""

        top = as_chunk(prefix_parts, 0) if prefix_parts else "(no-prefix)"
        sub = (
            as_chunk(prefix_parts, 2)
            if len(prefix_parts) > 2
            else ("-".join(prefix_parts[1:]) if len(prefix_parts) > 1 else "")
        )

        # Emit top header if changed
        if top != current_top:
            # Top-level header: muted gray color for subtlety
            muted = _muted_color(term)
            header = f"{muted}⏺ [{top}]{term.normal}"
            lines.append(header)
            mapping.append(None)
            # Spacer row after top header
            lines.append("")
            mapping.append(None)
            current_top = top
            current_sub = None

        # Emit sub header when present and changed
        if sub and sub != current_sub:
            # Sub header: muted gray rule with label
            # Insert a spacer row before every sub-header under the same top
            lines.append("")
            mapping.append(None)
            muted = _muted_color(term)
            sub_line = f"  {muted}── {sub} ──{term.normal}"
            lines.append(sub_line)
            mapping.append(None)
            # Spacer row after sub header
            lines.append("")
            mapping.append(None)
            current_sub = sub

        # Item row
        proj_color_name = _get_header_color(proj.name, term)
        proj_color = getattr(term, proj_color_name, term.cyan)
        # Colored circle indicator, plain project name
        lines.append("    " + f"{proj_color}⏺{term.normal} {suffix}")
        mapping.append(idx)

    return lines, mapping


def load_messages(file_path: str, role_filter: str = "all") -> List[ConversationMessage]:
    """Load and filter messages from a conversation file.

    Propagates OSError (and UnicodeDecodeError on a non-text transcript) rather
    than exiting, so per-row callers — listing previews, the browse preview pane —
    can drop one unreadable session instead of taking the process down with it.
    This used to `sys.exit(1)` here, which raises SystemExit and so slipped past
    those callers' handlers entirely. Top-level modes that do want a hard
    stop call `load_messages_or_exit`.
    """
    if _file_is_codex(file_path):
        return _codex_load_messages(file_path, role_filter)

    messages = []
    skipped_lines = 0

    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            try:
                data = json.loads(line)
                msg = ConversationMessage(data)

                # Filter valid messages
                if not msg.is_valid_message():
                    continue

                # Apply role filter
                if role_filter != "all" and msg.role != role_filter:
                    continue

                messages.append(msg)

            except json.JSONDecodeError:
                skipped_lines += 1
                continue

    if skipped_lines > 0:
        print(f"Warning: Skipped {skipped_lines} malformed JSON line(s)", file=sys.stderr)

    return messages


def load_messages_or_exit(file_path: str, role_filter: str = "all") -> List[ConversationMessage]:
    """`load_messages` for top-level modes, where an unreadable transcript is a
    hard stop with a clean message rather than a traceback.

    The per-row callers deliberately don't use this: they want one bad session to
    cost one row, not the whole run.
    """
    try:
        return load_messages(file_path, role_filter)
    except FileNotFoundError:
        print(f"Error: File '{file_path}' not found", file=sys.stderr)
        sys.exit(1)
    except PermissionError:
        print(f"Error: Permission denied reading '{file_path}'", file=sys.stderr)
        sys.exit(1)
    except (OSError, UnicodeDecodeError) as e:
        print(f"Error reading '{file_path}': {e}", file=sys.stderr)
        sys.exit(1)


def extract_recent_turns(
    messages: List[ConversationMessage], turns: int, role: str = "all"
) -> List[ConversationMessage]:
    """Extract the last N turns from conversation.

    A turn is a user message + all subsequent assistant messages until the next user.
    When role='all', returns both sides. When role='assistant' or 'user', returns
    only that role's messages.

    Args:
        messages: List of conversation messages (should be in chronological order)
        turns: Number of turns to extract
        role: 'all' for both user+assistant, or 'user'/'assistant' for one side

    Returns:
        List of messages in chronological order (oldest first)
    """
    if role == "all":
        # A turn = user message + all subsequent assistant messages until next user
        # Find positions of user messages
        user_indices = [i for i, m in enumerate(messages) if m.role == "user"]

        if not user_indices:
            # No user messages - just return last N assistant messages
            return extract_recent_turns(messages, turns, "assistant")

        # Get the last N user message positions
        start_indices = user_indices[-turns:] if len(user_indices) >= turns else user_indices

        # Start from the first of these user messages
        start_idx = start_indices[0]

        # Return all messages from that point forward
        return messages[start_idx:]
    else:
        # Single role - just get last N of that role
        filtered = []
        for message in reversed(messages):
            if message.role == role:
                filtered.append(message)
                if len(filtered) >= turns:
                    break
        return list(reversed(filtered))


def order_session_paths(
    paths: Iterable[Path], key: str = SESSION_SORT_DEFAULT, limit: Optional[int] = None
) -> List[Path]:
    """Session files newest-first under `key` -- the ordering the listings use.

    The one answer to "which of these is the most recent", so that the session a
    command picks when you name none is the session at the top of `sessions`.
    """
    return rank_sessions([(path, _safe_mtime(path), path) for path in paths], key, limit)


def get_recent_sessions_across_projects(
    base_dir: str, limit: int = 3
) -> List[Tuple[Path, Path, float]]:
    """Get the most recent sessions across all projects.

    Returns (project_path, session_path, when) tuples, newest first, where `when`
    is the last-message time the rows were ranked by -- the same value the
    listings show, not the file's mtime.
    """
    # Tuple shape is kept for callers; the layout knowledge lives in iter_session_refs.
    refs = iter_session_refs(base_dir, per_project=None)
    ranked = rank_sessions([(r.path, r.mtime, r) for r in refs], SESSION_SORT_DEFAULT, limit)
    return [
        (r.project_dir, r.path, session_sort_time(r.path, SESSION_SORT_DEFAULT, r.mtime))
        for r in ranked
    ]


def codex_short_id(stem: str) -> str:
    """Display id for a rollout stem (`rollout-<timestamp>-<uuid>`).

    `stem[:8]` — right for a Claude session, whose stem *is* the uuid — yields the
    literal "rollout-" here, identical for every row.
    """
    tail = stem.rsplit("-", 5)
    return tail[-5][:8] if len(tail) >= 6 else stem[:8]


def short_id_for_path(session_file: Path) -> str:
    """The 8-char display id for a transcript, whichever store it came from."""
    return (
        codex_short_id(session_file.stem) if _file_is_codex(session_file) else session_file.stem[:8]
    )


def session_recorded_cwd(session_file: Path) -> Optional[str]:
    """The working directory a transcript records, whichever store it came from.

    Claude stamps `cwd` on its opening events; Codex puts it in `session_meta`.
    """
    if _file_is_codex(session_file):
        return (_codex_session_meta(session_file) or {}).get("cwd") or None
    return read_session_header(session_file)["cwd"]


def _codex_session_preview(session_path: Path) -> Tuple[str, str, int]:
    """Codex counterpart of get_session_preview: (slug, first user turn, turns).

    Reads `event_msg`/`user_message` — what the user actually typed — rather than
    `response_item`, which also carries synthetic `<environment_context>` blocks
    and would surface one of those as the session's topic. Codex has no slug.
    """
    first_user = ""
    turn_count = 0
    try:
        with open(session_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "event_msg":
                    continue
                payload = entry.get("payload") or {}
                if payload.get("type") != "user_message":
                    continue
                text = (payload.get("message") or "").strip()
                if not text:
                    continue
                turn_count += 1
                if not first_user:
                    first_user = text.replace("\n", " ")[:80]
    except (OSError, UnicodeDecodeError):
        pass
    return ("", first_user, turn_count)


# Claude Code wraps non-prose user messages in these tags: slash-command
# scaffolding, `!`-bash echoes, memory input, background-task notifications.
# Treating one as a session's topic buries the real first message — 225 of 743
# transcripts in a real store open with `<local-command-caveat>` alone.
#
# This set is the canonical copy for the repo. claude-graph re-exports it through
# the seam in `claude_graph/sessions.py`; claude-reel keeps a hand-synced copy
# because it is stdlib-only on purpose and importing this module would drag rich
# and blessed in (a drift test pins the two together). Re-derive it from a real
# store rather than by reasoning — see CLAUDE.md.
#
# Matched by leading tag *name* rather than a full-string prefix, so a multi-line
# block can't slip past while prose that merely mentions a tag stays untouched.
# The name must be followed immediately by `>`, so an attributed tag would NOT
# match — which is correct here, since every wrapper Claude Code emits is bare
# and the only attributed leading tags in a real store are pasted HTML
# (`<div id=`, `<svg xmlns=`). This vocabulary was first worked out in claude-graph's
# `_WRAPPER_TAGS`; claude-reel keeps a third copy. Owning it here — the package
# both of them already read through the seam — is the obvious consolidation, but
# that's a cross-package change and belongs in its own PR.
_WRAPPER_TAGS = frozenset(
    {
        # slash-command scaffolding + captured local-command output
        "local-command-caveat",
        "command-name",
        "command-message",
        "command-args",
        "command-contents",
        "command-stdout",
        "local-command-stdout",
        # `!`-bash command echoes
        "bash-input",
        "bash-stdout",
        "bash-stderr",
        # memory input + background-task completion notifications
        "user-memory-input",
        "task-notification",
        "task-id",
        "tool-use-id",
        "output-file",
        # harness injections, all found by the census in CLAUDE.md.
        # `persisted-output` is the "Output too large, saved to …" stub.
        # `user-prompt-submit-hook` comes from claude-reel and isn't observed here yet.
        "system-reminder",
        "persisted-output",
        "user-prompt-submit-hook",
    }
)
_LEADING_TAG = re.compile(r"^<([a-z][a-z0-9-]*)>")
_COMMAND_NAME_RE = re.compile(r"<command-name>([^<]+)</command-name>")


def is_wrapper_text(text: str) -> bool:
    """True when a user message is Claude Code scaffolding, not something typed."""
    m = _LEADING_TAG.match(text.lstrip())
    return bool(m) and m.group(1) in _WRAPPER_TAGS


def user_prompt_text(entry: Dict) -> str:
    """The prose a human typed in a user record, or '' for tool results, wrapper
    scaffolding, and anything that is not a user record."""
    if entry.get("type") != "user":
        return ""
    content = (entry.get("message") or {}).get("content", "")
    text = ""
    if isinstance(content, str):
        text = content.strip()
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "").strip()
                if text:
                    break
    return "" if not text or is_wrapper_text(text) else text


def first_answered_prompt(entries) -> str:
    """The first prompt that got a reply, or the last unanswered one.

    Enter submits, and a line typed over a slow link lands in pieces: the
    transcript then holds "Lets", then "Lets build the fleet view", with no
    assistant record between them. The earlier one was superseded and says
    nothing about the session. Any prompt an assistant record follows, however
    short ("Deploy"), is a real first message and is kept.
    """
    candidate = ""
    for entry in entries:
        if entry.get("type") == "assistant":
            if candidate:
                return candidate
        else:
            text = user_prompt_text(entry)
            if text:
                candidate = text
    return candidate


# A user message that is nothing but a bracketed placeholder is Claude Code
# standing in for something that isn't text -- a pasted image, an interrupted
# turn. Not prose anyone typed, so it can't be a topic: two sessions in a real
# store listed one as their whole topic, absolute path and all (2026-09-19).
# Matched against the untruncated text, since the closing bracket of a long one
# falls outside the 80-char preview.
_PLACEHOLDER_TEXT_RE = re.compile(r"\[[^\]]*\]\Z")


def is_placeholder_text(text: str) -> bool:
    """Is this message body a stand-in Claude Code wrote, rather than prose?"""
    return bool(_PLACEHOLDER_TEXT_RE.fullmatch(text.strip()))


def get_session_preview(session_path: Path, topic_only: bool = False) -> Tuple[str, str, int]:
    """Quickly extract slug, first user message, and message count from a session file.

    Reads only enough lines to find the slug and first user message, then counts
    user/assistant messages for turn count. `topic_only` stops at the first
    answered prompt, which fixes the topic; the turn count is then partial.

    Returns (slug, first_user_preview, turn_count).
    """
    if _file_is_codex(session_path):
        return _codex_session_preview(session_path)

    slug = ""
    first_user = ""
    candidate = ""  # a prompt nothing has answered yet; the next prompt supersedes it
    command_hint = ""
    turn_count = 0
    try:
        with open(session_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not slug:
                    slug = entry.get("slug", "")
                if entry.get("type") == "user":
                    msg = entry.get("message", {})
                    content = msg.get("content", "")
                    if isinstance(content, str) and content.strip():
                        text = content.strip()
                    elif isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                text = block.get("text", "").strip()
                                if text:
                                    break
                        else:
                            text = ""
                    else:
                        text = ""
                    # Skip tool results and system messages
                    if text and is_placeholder_text(text):
                        # Counts as a turn -- someone pasted it -- but can't be
                        # the topic, so the next real prompt supersedes it.
                        turn_count += 1
                    elif text and is_wrapper_text(text):
                        # Keep the slash command as a fallback topic: some sessions
                        # are *only* a command, and "/clear" beats "(empty)".
                        if not command_hint:
                            m = _COMMAND_NAME_RE.search(text)
                            if m:
                                command_hint = m.group(1).strip()
                    elif text:
                        turn_count += 1
                        if not first_user:
                            candidate = text.replace("\n", " ")[:80]
                elif entry.get("type") == "assistant":
                    if candidate and not first_user:
                        first_user = candidate  # answered, so it was a real prompt
                        if topic_only:
                            break
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        # UnicodeDecodeError is a ValueError, not an OSError, so a transcript
        # that isn't text at all used to escape this handler and take the whole
        # listing down with a traceback. Returns whatever was decoded
        # before the failure, which for a small file is nothing — text-mode
        # iteration decodes a chunk at a time, so the error can land before the
        # first line is yielded. An empty row beats a dead listing either way.
        pass
    return (slug, first_user or candidate or command_hint, turn_count)


def matching_sessions(base_dir: str, session_id: str, include_codex: bool = True) -> List[Path]:
    """All session files whose name contains session_id, in a stable order.

    A short or partial id is a substring of several distinct sessions (a 2-char
    prefix collides freely), so a match set of >1 is expected; the anchoring
    directory — not the id — is what tells them apart.

    Ordered by path, deterministically. `resume` lists the candidates and the user
    reruns with `--pick N`; an active transcript written between those two calls
    must not reorder the list and point the same N at a different session. mtime
    would reorder; the path doesn't.

    Codex rollouts are included, and unlike *listing* them this isn't opt-in: an
    id typed by hand is unambiguous intent, whereas a listing that silently
    doubles is not. Matching is on filename alone — no header reads — so adding
    the second store costs a glob. Actions that only work on Claude sessions
    (`resume`) refuse by source, rather than by pretending the session is missing.
    """
    matches = []
    for project in list_projects(base_dir):
        for session in list_sessions(project):
            if session_id in session.name:
                matches.append(session)
    if include_codex:
        root = codex_sessions_dir()
        if root.is_dir():
            matches.extend(p for p in root.rglob("rollout-*.jsonl") if session_id in p.name)
    matches.sort(key=str)
    return matches


def print_session_candidates(session_id: str, matches: List[Path]) -> None:
    """Print numbered, metadata-rich candidates for an ambiguous match to stderr.

    Shows each match's anchoring directory and mtime — the fields that actually
    distinguish same-id sessions — so the listing is a usable menu for `--pick`,
    not a wall of identical UUIDs.
    """
    print(f"Multiple sessions match '{session_id}':", file=sys.stderr)
    for i, s in enumerate(matches, 1):
        # The cwd recorded in the transcript is what resume treats as authoritative;
        # show that (not the lossy slug decode) so the listing matches where --pick
        # would actually land. Fall back to the slug only if the header lacks it —
        # and only for Claude, since a rollout's parent directory is a date.
        is_codex = _file_is_codex(s)
        cwd = session_recorded_cwd(s)
        if not cwd:
            cwd = "(unknown)" if is_codex else project_folder_to_cwd(s.parent.name)
        try:
            when = datetime.fromtimestamp(s.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        except OSError:
            when = "?"
        # Source is load-bearing in this menu: the candidates differ in what you can
        # then do with them, not just where they live.
        tag = "  [codex]" if is_codex else ""
        print(f"  {i}  {when}  {cwd}  ({short_id_for_path(s)}){tag}", file=sys.stderr)


def find_session_across_projects(base_dir: str, session_id: str) -> Optional[Path]:
    """Resolve a session id to a single file, or print candidates when ambiguous."""
    matches = matching_sessions(base_dir, session_id)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        print_session_candidates(session_id, matches)
    return None  # zero or ambiguous


def search_sessions(
    base_dir: str, substring: str, limit: int = 20, sort: str = SESSION_SORT_DEFAULT
) -> List[Tuple[Path, Path, float]]:
    """Search for sessions containing substring (case insensitive).

    Searches both project names and session IDs.
    Returns list of (project_path, session_path, time) sorted newest first, where
    `time` is whichever timestamp `sort` selected -- the caller displays it, so
    the column always shows the value the rows were ordered by.
    """
    substring_lower = substring.lower()
    matches: List[Tuple[Path, float, Tuple[Path, Path, float]]] = []
    for project in list_projects(base_dir):
        project_name_lower = project.name.lower()
        project_matches = substring_lower in project_name_lower
        for session in list_sessions(project):
            if project_matches or substring_lower in session.name.lower():
                try:
                    mtime = session.stat().st_mtime
                except OSError:
                    continue
                matches.append((session, mtime, (project, session, mtime)))
    ranked = rank_sessions(matches, sort, limit)
    return [(proj, sess, session_sort_time(sess, sort, mtime)) for proj, sess, mtime in ranked]


def search_session_content(
    base_dir: str,
    query: str,
    project_filter: Optional[str] = None,
    case_sensitive: bool = False,
    since_epoch: Optional[float] = None,
    until_epoch: Optional[float] = None,
    respect_ignore: bool = False,
    include_codex: bool = False,
) -> List[Tuple[Path, Path, float]]:
    """Search for sessions containing query in conversation content using rg.

    Returns ALL matching (project_path, session_path, mtime) tuples sorted by
    mtime descending — the caller applies the display limit, so it can report the
    full match total.
    Pre-filters via rg over raw JSONL; semantic/prose filtering happens later
    in get_session_matches. ``since_epoch``/``until_epoch`` filter by mtime
    before the limit is applied.

    With ``include_codex``, the rollout store joins the scan as one more directory,
    and the per-file work happens *after* rg narrows things down. A rollout carries
    its project inside the file — the store is partitioned by date — so the project
    key, and whether the thread is a subagent, cost a head-read each. Reading them
    for every rollout up front would be ~700 reads per search to answer a question
    about the handful that matched; the same head-read on the hits alone is the
    same answer for a fraction of the work, and can't be capped or truncated on the
    way in. The `project_path` returned for a rollout is a synthetic Path carrying
    the project *key*, which is all the display and `-p` filtering ever read off it.
    """
    filter_lower = project_filter.lower() if project_filter else None
    projects = list_projects(base_dir)
    if filter_lower is not None:
        projects = [p for p in projects if filter_lower in p.name.lower()]

    codex_root = codex_sessions_dir() if include_codex else None
    if codex_root is not None and not codex_root.is_dir():
        codex_root = None

    if not projects and codex_root is None:
        return []

    if shutil.which("rg") is None:
        # rg is required for content search. Failing loudly beats the old silent
        # `return []`, which was indistinguishable from a genuine no-match.
        print(
            "error: ripgrep (rg) is required for content search but was not found on PATH.\n"
            "Install it (e.g. `brew install ripgrep`) and retry.",
            file=sys.stderr,
        )
        sys.exit(1)

    case_flag = [] if case_sensitive else ["-i"]
    # --no-ignore --hidden: the session store lives under ~/.claude, so by default
    # we search it exhaustively regardless of the user's gitignore / global
    # core.excludesFile / .ignore config, which ripgrep otherwise honors and would
    # silently skip — making real matches vanish. --respect-ignore opts
    # back into ripgrep's normal ignore handling.
    ignore_flags = [] if respect_ignore else ["--no-ignore", "--hidden"]
    targets = [str(p) for p in projects]
    if codex_root is not None:
        targets.append(str(codex_root))
    cmd = ["rg", "-l", *ignore_flags, *case_flag, "--", query] + targets
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        print(f"warning: content search timed out after 30s for '{query}'", file=sys.stderr)
        return []
    except OSError as e:
        # Spawn failed outright (argv too long, fork failure). A traceback here
        # would be the only unhandled path in an otherwise carefully degraded
        # function.
        print(f"error: could not run ripgrep: {e}", file=sys.stderr)
        sys.exit(2)
    matching_files = result.stdout.strip().split("\n") if result.stdout.strip() else []
    # Exit 0 = matches, 1 = no matches (normal). >=2 is a real error (bad pattern,
    # IO failure). With no usable output that's a *failed* search, not an empty
    # one — exit nonzero so it can't masquerade as "No sessions found" (and so
    # callers/scripts can tell the two apart). With partial output (e.g. one
    # unreadable file mid-scan), keep the results but warn.
    if result.returncode >= 2:
        detail = result.stderr.strip() or f"ripgrep exited with status {result.returncode}"
        if not matching_files:
            print(f"error: content search failed:\n{detail}", file=sys.stderr)
            sys.exit(2)
        print(
            f"warning: ripgrep reported errors (results may be partial):\n{detail}",
            file=sys.stderr,
        )

    codex_root_str = str(codex_root) + os.sep if codex_root is not None else None

    matches = []
    for file_path in matching_files:
        if not file_path.endswith(".jsonl"):
            continue
        session = Path(file_path)
        if codex_root_str is not None and file_path.startswith(codex_root_str):
            # A rollout's parent is a date directory, not a project — the header
            # holds both the project and whether this file counts at all, and now
            # that rg has narrowed the field it's one bounded read per hit.
            key = codex_project_key(session)
            if key is None:
                continue
            if filter_lower is not None and filter_lower not in key.lower():
                continue
            project = Path(key)
        else:
            project = session.parent
        try:
            mtime = session.stat().st_mtime
        except OSError:
            continue
        if since_epoch is not None and mtime < since_epoch:
            continue
        if until_epoch is not None and mtime > until_epoch:
            continue
        matches.append((mtime, project, session))

    matches.sort(key=lambda x: -x[0])
    return [(proj, sess, mtime) for mtime, proj, sess in matches]


# Prose extraction: only walk into known human-readable fields. Skips
# `signature`/`encrypted_index`/cache tokens that can incidentally contain
# the query as a base64 substring.


def iter_content_fragments(content: Any) -> Iterator[Tuple[str, str]]:
    """Yield (kind, text) fragments from a message.content payload.

    kind ∈ {"text", "think", "tool_in", "tool"}. This is the single owner of the
    Claude block-type vocabulary — every text-extraction path routes through here,
    so teaching the tool a second transcript format (e.g. Codex's input_text/
    output_text blocks) is a change in one place, not four.
    """
    if isinstance(content, str):
        yield ("text", content)
        return
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text" and isinstance(block.get("text"), str):
            yield ("text", block["text"])
        elif btype == "thinking" and isinstance(block.get("thinking"), str):
            yield ("think", block["thinking"])
        elif btype == "tool_use":
            tin = block.get("input")
            if isinstance(tin, dict):
                for key in ("command", "description", "query", "pattern", "prompt"):
                    val = tin.get(key)
                    if isinstance(val, str):
                        yield ("tool_in", val)
        elif btype == "tool_result":
            inner = block.get("content")
            if isinstance(inner, str):
                yield ("tool", inner)
            elif isinstance(inner, list):
                for sub in inner:
                    if (
                        isinstance(sub, dict)
                        and sub.get("type") == "text"
                        and isinstance(sub.get("text"), str)
                    ):
                        yield ("tool", sub["text"])


def iter_session_prose(file_path: Path) -> Iterator[Tuple[str, str]]:
    """Yield (role_tag, text) for prose fields in the session file."""
    if _file_is_codex(file_path):
        yield from _codex_iter_prose_entries(load_raw_entries(str(file_path)))
        return
    try:
        f = open(file_path, "r", encoding="utf-8", errors="ignore")
    except OSError:
        return
    with f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            etype = entry.get("type")
            if etype == "summary" and isinstance(entry.get("summary"), str):
                yield ("summ", entry["summary"])
                continue
            msg = entry.get("message")
            if not isinstance(msg, dict):
                continue
            top_role = "asst" if etype == "assistant" else "user"
            for kind, text in iter_content_fragments(msg.get("content")):
                yield (top_role if kind == "text" else kind, text)


_WORD_BOUNDARY_RE = re.compile(r"[\s\n\r\t.,;:!?()\[\]{}'\"<>/\\|=+*&^%$#@~`-]")


def _boundary_score(text: str, start: int, end: int) -> int:
    """+1 for each side of the match that sits next to whitespace/punct or edge.

    Used to demote substring hits inside base64/UUID blobs (no whitespace nearby).
    """
    score = 0
    if start == 0 or _WORD_BOUNDARY_RE.match(text[start - 1]):
        score += 1
    if end >= len(text) or _WORD_BOUNDARY_RE.match(text[end]):
        score += 1
    return score


# Role tags for tool input/output — ranked below prose when choosing snippets.
_TOOLISH_ROLES = {"tool", "tool_in"}


def get_session_matches(
    session_path: Path,
    query: str,
    max_matches: int = 3,
    case_sensitive: bool = False,
    exclude_types: Optional[set] = None,
    include_types: Optional[set] = None,
    width: int = 50,
) -> List[Tuple[str, str]]:
    """Return up to max_matches (role_tag, snippet) tuples for query in session prose.

    Skips matches in non-prose JSON fields (signature, encrypted_index). Ranks
    matches by word-boundary score so substring-of-base64 hits sink below real
    word hits. Role tags in ``exclude_types`` are dropped entirely; when
    ``include_types`` is given, only those role tags are kept. ``width`` sets the
    chars of context shown on each side of a match.
    """
    if not query:
        return []
    haystack_flags = 0 if case_sensitive else re.IGNORECASE
    pattern = re.compile(re.escape(query), haystack_flags)

    exclude = exclude_types or set()
    include = include_types or set()
    candidates: List[Tuple[int, str, str]] = []  # (score, role, snippet)
    seen_snippets: set = set()
    prose_n = 0  # prose (non-tool) candidates collected so far

    # Keep scanning until we have enough PROSE candidates to fill the request, or
    # we hit a generous hard cap. A flat "stop after N candidates" would let a run
    # of early tool hits crowd out later assistant/user prose, defeating the
    # prose-over-tool ranking below (it can only rank what it has seen).
    hard_cap = max(max_matches * 8, 40)

    def _enough() -> bool:
        return prose_n >= max_matches or len(candidates) >= hard_cap

    for role, text in iter_session_prose(session_path):
        if any(ex in role for ex in exclude):
            continue
        if include and not any(inc in role for inc in include):
            continue
        is_prose = role not in _TOOLISH_ROLES
        for m in pattern.finditer(text):
            score = _boundary_score(text, m.start(), m.end())
            ctx_start = max(0, m.start() - width)
            ctx_end = min(len(text), m.end() + width)
            snippet = text[ctx_start:ctx_end]
            snippet = re.sub(r"\s+", " ", snippet).strip()
            if ctx_start > 0:
                snippet = "…" + snippet
            if ctx_end < len(text):
                snippet = snippet + "…"
            max_snippet = 2 * width + len(query) + 40
            if len(snippet) > max_snippet:
                snippet = snippet[: max_snippet - 3] + "…"
            if snippet in seen_snippets:
                continue
            seen_snippets.add(snippet)
            candidates.append((score, role, snippet))
            if is_prose:
                prose_n += 1
            if _enough():
                break
        if _enough():
            break

    # Rank prose (assistant/user/thinking/summary) above tool output, then by
    # boundary score — so the single snippet shown by default is a real sentence,
    # not a filename or log line that happens to contain the query.
    candidates.sort(key=lambda c: (c[1] in _TOOLISH_ROLES, -c[0]))
    return [(role, snippet) for _, role, snippet in candidates[:max_matches]]


def resolve_session_file(args, base_dir: str) -> Optional[Path]:
    """Resolve session file from args: explicit file, session-id, or auto-detect.

    Priority: args.file > args.session_id (global search) > auto-detect from CWD
    """
    # Explicit positional provided: a real file path, or a bare session id.
    if hasattr(args, "file") and args.file:
        candidate = Path(args.file)
        if candidate.exists():
            return candidate
        # Not a path on disk — treat it as a session id and search all projects.
        session = find_session_across_projects(base_dir, args.file)
        if session:
            return session
        print(f"No file or session found matching: {args.file}", file=sys.stderr)
        return None

    # Session ID provided - search across all projects
    if hasattr(args, "session_id") and args.session_id:
        session = find_session_across_projects(base_dir, args.session_id)
        if session:
            return session
        print(f"No session found matching: {args.session_id}", file=sys.stderr)
        return None

    # Auto-detect project from CWD (or an explicit --cwd override, for fleet tooling
    # that resolves another process's working dir rather than its own).
    cwd_override = getattr(args, "cwd", None)
    project = find_project_from_cwd(base_dir, cwd_override)
    if not project:
        cwd = cwd_override if cwd_override is not None else os.getcwd()
        expected_name = cwd_to_project_name(cwd_override)
        print(f"No Claude project found for current directory: {cwd}", file=sys.stderr)
        print(f"Expected project name: {expected_name}", file=sys.stderr)
        print_moved_project_hint(base_dir, cwd_override)
        # Show recent sessions as hints
        recent = get_recent_sessions_across_projects(base_dir, limit=3)
        if recent:
            print("\nRecent sessions (use -s/--session-id):", file=sys.stderr)
            for proj, sess, when in recent:
                proj_name = format_project_identifier(proj.name)
                sess_id = sess.stem  # Full session ID
                time_str = datetime.fromtimestamp(when).strftime("%Y-%m-%d %H:%M")
                print(f"  {sess_id}  {time_str}  ({proj_name})", file=sys.stderr)
        return None

    sessions = list_sessions(project)
    if not sessions:
        project_name = format_project_identifier(project.name)
        print(f"No conversation sessions found in project: {project_name}", file=sys.stderr)
        return None

    # Most recent by the same key the listings sort on, so `last` opens the session
    # `sessions` puts at the top rather than whichever file was written to last.
    return order_session_paths(sessions, limit=1)[0]


def list_sessions_matching(args, console: Optional[Console]):
    """List sessions matching a substring."""
    matches = search_sessions(
        args.base_dir,
        args.contains,
        limit=args.count,
        sort=getattr(args, "sort", SESSION_SORT_DEFAULT),
    )

    if not matches:
        print(f"No sessions found containing '{args.contains}'")
        return

    for proj, sess, when in matches:
        proj_name = format_project_identifier(proj.name)
        time_str = datetime.fromtimestamp(when).strftime("%Y-%m-%d %H:%M")
        if args.no_color or console is None:
            print(f"{sess.stem}  {time_str}  ({proj_name})")
        else:
            text = Text()
            text.append(f"{sess.stem}  ", style="white")
            text.append(f"{time_str}  ", style="dim")
            text.append(f"({proj_name})", style="cyan")
            console.print(text)


# Patterns that suggest assistant is waiting for user input
WAITING_PATTERNS = [
    r"\?\s*$",  # Ends with question mark
    r'\?["\']?\s*$',  # Question mark before closing quote
    r"(?i)please\s+(test|review|check|try|verify|confirm)",
    r"(?i)let me know",
    r"(?i)would you like",
    r"(?i)should I\b",
    r"(?i)want me to",
    r"(?i)ready for (you|your|testing|review)",
    r"(?i)when you[\'re]* ready",
    r"(?i)feel free to",
]


def looks_like_waiting(text: str) -> bool:
    """Check if message looks like it's waiting for user input."""
    import re

    for pattern in WAITING_PATTERNS:
        if re.search(pattern, text):
            return True
    return False


def format_staleness(minutes: int) -> str:
    """Format minutes as human-readable staleness."""
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    mins = minutes % 60
    if hours < 24:
        return f"{hours}h {mins}m" if mins else f"{hours}h"
    days = hours // 24
    return f"{days}d"


def pending_mode(args, console: Optional[Console]):
    """Show sessions where assistant is waiting for user input (questions).

    Permission prompts (tool approval) are a different signal and this doesn't
    detect them. `logmonitor pending` does, but it ships from elsewhere — the
    no-results hint only mentions it when it's on PATH.
    """
    import time

    base_dir = Path(args.base_dir)
    results = []
    cache: Dict[str, Optional[Tuple[str, str]]] = {}

    for project_dir in base_dir.iterdir():
        if not project_dir.is_dir():
            continue

        project_name = collapsed_project_label(project_dir.name)

        # Filter by project if specified
        if args.project and args.project.lower() not in project_name.lower():
            continue

        for session_file in project_dir.glob("*.jsonl"):
            try:
                mtime = session_file.stat().st_mtime
            except OSError:
                continue

            last_msg = get_session_last_message(session_file, cache)

            if not last_msg:
                continue

            role, preview = last_msg

            # Only assistant messages that look like waiting
            if role != "assistant":
                continue

            if not looks_like_waiting(preview):
                continue

            # Calculate staleness
            minutes_waiting = int((time.time() - mtime) / 60)

            # Filter by staleness threshold
            if args.stale and minutes_waiting < args.stale:
                continue

            # The full id, not a prefix. Fleet callers join on this field — claude-ps
            # matches it against the session id its hook registry records, which is
            # the whole uuid — so a truncated value here is a join that silently never
            # matches. The listing below still shows the short form; truncation is a
            # display concern.
            session_id = session_file.stem

            # Skip agent-* subagent sessions by default
            if session_id.startswith("agent-") and not args.include_agents:
                continue

            # Real cwd/branch from the transcript, so fleet callers can join a
            # waiting session to a process by absolute path rather than the lossy
            # flattened folder name.
            header = read_session_header(session_file)
            results.append(
                {
                    "session_id": session_id,
                    "project": project_name,
                    "cwd": header["cwd"],
                    "git_branch": header["git_branch"],
                    "minutes_waiting": minutes_waiting,
                    "preview": preview[:100],
                    "path": str(session_file),
                }
            )

    # Sort by staleness (newest-waiting first, most useful for manager)
    results.sort(key=lambda x: x["minutes_waiting"])
    results = results[: args.limit]

    # JSON output
    if args.json:
        print(json.dumps(results, indent=2))
        return

    # No results
    if not results:
        # `logmonitor` is a separate tool that isn't shipped from this repo, so
        # only point at it when it's actually installed — otherwise this reads as
        # "run X" for an X the reader has no way to get.
        hint = " (For permission prompts: logmonitor pending)" if shutil.which("logmonitor") else ""
        msg = f"No sessions waiting on questions.{hint}"
        if console:
            console.print(f"[dim]{msg}[/dim]")
        else:
            print(msg)
        return

    # Pretty output
    if console:
        console.print(f"[bold]Sessions waiting on questions ({len(results)}):[/bold]\n")
        for r in results:
            staleness = format_staleness(r["minutes_waiting"])
            console.print(
                f"[cyan]{r['session_id'][:8]}[/cyan]  {r['project']}  "
                f"[dim]waiting {staleness}[/dim]"
            )
            console.print(f'  [italic]"{r["preview"]}..."[/italic]\n')
    else:
        print(f"Sessions waiting on questions ({len(results)}):\n")
        for r in results:
            staleness = format_staleness(r["minutes_waiting"])
            print(f"{r['session_id'][:8]}  {r['project']}  waiting {staleness}")
            print(f'  "{r["preview"]}..."\n')


_ROLE_STYLES = {
    "user": "cyan",
    "asst": "green",
    "think": "magenta",
    "tool": "yellow",
    "tool_in": "yellow",
    "summ": "white",
}


def _parse_date_arg(value: str, end_of_day: bool = False) -> Optional[float]:
    """Parse a --since/--until value to a local epoch timestamp.

    Accepts YYYY-MM-DD, 'today', 'yesterday', or 'Nd' (N days ago). Returns
    None on empty input; exits on malformed input.
    """
    if not value:
        return None
    v = value.strip().lower()
    now = datetime.now()
    if v == "today":
        dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif v == "yesterday":
        dt = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    elif re.fullmatch(r"\d+d", v):
        dt = (now - timedelta(days=int(v[:-1]))).replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        try:
            dt = datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            print(
                f"Invalid date: {value!r} (expected YYYY-MM-DD, 'today', 'yesterday', or 'Nd')",
                file=sys.stderr,
            )
            sys.exit(1)
    if end_of_day:
        dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
    return dt.timestamp()


def search_content_mode(args, console: Optional[Console]):
    """Search for sessions containing query in conversation content.

    Default output is compact: one line per session (id, time, project) plus the
    single best inline snippet, capped at --count, with a footer reporting the
    full match total and how to narrow. -C expands to the multi-snippet view
    (with where edits/PRs landed); -b drops the snippet.
    """
    query = " ".join(args.query) if isinstance(args.query, list) else args.query
    project_filter = getattr(args, "project", None)
    force_context = getattr(args, "show_context", False)
    brief = getattr(args, "brief", False)
    case_sensitive = getattr(args, "case_sensitive", False)
    verbose = getattr(args, "verbose", False)
    exclude_types = set(getattr(args, "exclude_type", []) or [])
    include_types = set(getattr(args, "only_type", []) or [])
    max_matches = getattr(args, "max_matches", 3)
    width = getattr(args, "width", 50)
    count = getattr(args, "count", 5)
    show_work = not getattr(args, "no_work", False)
    no_color = args.no_color or console is None
    since_epoch = _parse_date_arg(getattr(args, "since", None))
    until_epoch = _parse_date_arg(getattr(args, "until", None), end_of_day=True)

    matches = search_session_content(
        args.base_dir,
        query,
        project_filter=project_filter,
        case_sensitive=case_sensitive,
        since_epoch=since_epoch,
        until_epoch=until_epoch,
        respect_ignore=getattr(args, "respect_ignore", False),
        include_codex=getattr(args, "codex", False),
    )

    if not matches:
        print(f"No sessions found containing '{query}'")
        # A resume that found nothing is a failure, not a no-op — make it
        # detectable in scripts (`search … -r 1`).
        sys.exit(1 if getattr(args, "resume", None) is not None else 0)

    total = len(matches)
    # -C shows the full multi-snippet block; default/brief show one snippet (still
    # computed for brief, to confirm a real prose hit and skip noise-only
    # sessions). A narrower window keeps the default snippet on one line.
    per_session = max_matches if force_context else 1
    snippet_width = width if force_context else min(width, 30)

    enriched: List[Tuple[Path, Path, float, List[Tuple[str, str]]]] = []
    for proj, sess, mtime in matches:
        if len(enriched) >= count:  # checked first, so -n 0 / negative shows none
            break
        snippets = get_session_matches(
            sess,
            query,
            max_matches=per_session,
            case_sensitive=case_sensitive,
            exclude_types=exclude_types,
            include_types=include_types,
            width=snippet_width,
        )
        if not snippets:
            continue  # query only hit base64/excluded fields — not real prose
        enriched.append((proj, sess, mtime, snippets))

    # -r picks a result and hands off to the resume machinery instead of listing.
    # An offset addresses the rows we'd show (enriched, in display order); a
    # non-numeric value is a session-id substring resolved downstream.
    resume_sel = getattr(args, "resume", None)
    if resume_sel is not None:
        if not enriched:
            print(f"No results to resume for '{query}'.", file=sys.stderr)
            sys.exit(1)
        if resume_sel.isdigit():
            idx = int(resume_sel)
            if not (1 <= idx <= len(enriched)):
                print(
                    f"-r {idx} is out of range — {len(enriched)} "
                    f"result{'s' if len(enriched) != 1 else ''} shown "
                    f"(use 1-{len(enriched)}, or -n to show more).",
                    file=sys.stderr,
                )
                sys.exit(1)
            ident = enriched[idx - 1][1].stem
        else:
            ident = resume_sel
        # resume_mode resolves the id, locates the anchoring dir, and runs the
        # tmux safety check. -f passes through to skip that check; attach/dry-run
        # aren't exposed here — use the dedicated `resume` command for those.
        args.session = ident
        args.session_id = None
        args.force = getattr(args, "force", False)
        args.attach = False
        args.dry_run = False
        resume_mode(args, console)
        return

    # count <= 0 is an explicit "show none" — fall through to the breadth footer
    # rather than the no-match message (the sessions do exist; we just didn't list
    # any).
    if not enriched and count > 0:
        if exclude_types:
            print(
                f"No matches for '{query}' after excluding types: "
                f"{', '.join(sorted(exclude_types))}"
            )
        else:
            print(f"No prose matches for '{query}' (only base64/encoded hits found)")
        return

    hl_pattern = re.compile(re.escape(query), 0 if case_sensitive else re.IGNORECASE)

    def _append_highlighted(text_obj: Text, snippet: str) -> None:
        pos = 0
        for m in hl_pattern.finditer(snippet):
            if m.start() > pos:
                text_obj.append(snippet[pos : m.start()], style="dim white")
            text_obj.append(snippet[m.start() : m.end()], style="bold yellow")
            pos = m.end()
        if pos < len(snippet):
            text_obj.append(snippet[pos:], style="dim white")

    # Number rows so `-r N` is discoverable: the leading index is the offset.
    idx_w = len(str(len(enriched)))
    for row_i, (proj, sess, mtime, snippets) in enumerate(enriched, start=1):
        idx_str = f"{row_i:>{idx_w}}"
        proj_name = format_project_identifier(proj.name)
        if force_context:
            # Expanded: full id, working dir, where edits/PRs landed, all snippets.
            working_dir = project_folder_to_cwd(proj.name)
            time_str = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
            path_display = str(sess).replace(str(Path.home()), "~", 1)
            work_line = ""
            if show_work:
                entries = load_raw_entries(str(sess))
                if entries:
                    work_line = work_signals_oneline(derive_work_signals(entries))
            if no_color:
                print(f"{idx_str}  {sess.stem}  {time_str}  ({proj_name})  {working_dir}")
                if work_line:
                    print(f"    ↳ {work_line}")
                if verbose:
                    print(f"    -> {path_display}")
                for role, snippet in snippets:
                    print(f"    [{role}] {snippet}")
            else:
                text = Text()
                text.append(f"{idx_str}  ", style="dim cyan")
                text.append(f"{sess.stem}  ", style="white")
                text.append(f"{time_str}  ", style="dim")
                text.append(f"({proj_name})  ", style="cyan")
                text.append(working_dir, style="dim blue")
                console.print(text)
                if work_line:
                    wtext = Text()
                    wtext.append("    ↳ ", style="dim")
                    wtext.append(work_line, style="green")
                    console.print(wtext)
                if verbose:
                    path_text = Text()
                    path_text.append("    -> ", style="dim")
                    path_text.append(path_display, style="dim blue")
                    console.print(path_text)
                for role, snippet in snippets:
                    role_style = _ROLE_STYLES.get(role, "white")
                    ctx = Text()
                    ctx.append("    ", style="dim")
                    ctx.append(f"[{role}] ", style=role_style)
                    _append_highlighted(ctx, snippet)
                    console.print(ctx)
        else:
            # Compact: one line — id, time, project, and (unless -b) one snippet.
            # stem[:8] is the literal "rollout-" for every rollout, so route both
            # stores through the shared helper.
            short_id = short_id_for_path(sess)
            time_str = datetime.fromtimestamp(mtime).strftime("%m-%d %H:%M")
            snippet = snippets[0][1]
            if no_color:
                line = f"{idx_str}  {short_id}  {time_str}  {proj_name}"
                if not brief:
                    line += f"  {snippet}"
                print(line)
                if verbose:
                    print(f"    -> {str(sess).replace(str(Path.home()), '~', 1)}")
            else:
                text = Text()
                text.append(f"{idx_str}  ", style="dim cyan")
                text.append(f"{short_id}  ", style="white")
                text.append(f"{time_str}  ", style="dim")
                text.append(proj_name, style="cyan")
                if not brief:
                    text.append("  ")
                    _append_highlighted(text, snippet)
                console.print(text)
                if verbose:
                    path_text = Text()
                    path_text.append("    -> ", style="dim")
                    path_text.append(str(sess).replace(str(Path.home()), "~", 1), style="dim blue")
                    console.print(path_text)

    # Footer: report the full breadth and how to narrow / expand. `total` is the
    # number of sessions whose text contains the query (ripgrep's -l count) — an
    # honest breadth signal. We say "containing" rather than "matched" because the
    # listed rows are further qualified to ones with a real prose hit, so a few of
    # `total` may not surface a snippet; conflating the two would misreport.
    shown = len(enriched)
    sess_word = "session" if total == 1 else "sessions"
    footer = (
        f"{shown} of {total} {sess_word} containing '{query}'"
        if shown < total
        else f"{total} {sess_word} containing '{query}'"
    )
    hints = []
    if shown < total:
        hints.append("narrow: -p <project> · --since 7d · longer query · -n for more")
    if not force_context:
        hints.append("-C for full snippets")
    if shown:
        hints.append("-r <#> to resume")
    if hints:
        footer += " — " + " · ".join(hints)
    if no_color:
        print(footer)
    else:
        console.print(footer, style="dim")


def list_messages(args, console: Optional[Console]):
    """List recent messages with indices"""
    # If --contains specified, list matching sessions instead
    if hasattr(args, "contains") and args.contains:
        list_sessions_matching(args, console)
        return

    session_file = resolve_session_file(args, args.base_dir)
    if not session_file:
        sys.exit(1)

    messages = load_messages_or_exit(str(session_file), args.role)

    if not messages:
        print("No messages found")
        return

    # Get last N messages
    recent_messages = messages[-args.count :]

    if args.no_color or console is None:
        # fzf-friendly plain text output
        for i, msg in enumerate(recent_messages):
            neg_index = to_negative_index(i, len(recent_messages))
            print(f"{neg_index}: [{msg.role}] {msg.time_str} - {msg.preview}")
    else:
        # Rich colored output
        table = Table(show_header=True, header_style="bold blue", box=box.SIMPLE)
        table.add_column("Index", style="dim", width=8)
        table.add_column("Time", style="dim", width=12)
        table.add_column("Role", width=10)
        table.add_column("Content", ratio=1)

        for i, msg in enumerate(recent_messages):
            neg_index = to_negative_index(i, len(recent_messages))
            role_color = "green" if msg.role == "assistant" else "yellow"

            table.add_row(
                f"[{neg_index}]", msg.time_str, Text(msg.role, style=role_color), msg.preview
            )

        console.print(f"\nShowing last {len(recent_messages)} messages:")
        console.print(table)


def get_message(args, console: Optional[Console]):
    """Extract specific message content by index or session ID."""
    # Handle positional session_or_file: could be a session ID prefix or a .jsonl path
    positional = getattr(args, "session_or_file", None)
    if positional:
        if positional.endswith(".jsonl") or "/" in positional:
            args.file = positional
        else:
            # Treat as session ID if not already set
            if not getattr(args, "session_id", None):
                args.session_id = positional

    session_file = resolve_session_file(args, args.base_dir)
    if not session_file:
        sys.exit(1)

    messages = load_messages_or_exit(str(session_file), "all")

    if not messages:
        print("No messages found", file=sys.stderr)
        sys.exit(1)

    # --tail mode: show last N turns
    tail = getattr(args, "tail", None)
    if tail:
        recent = extract_recent_turns(messages, tail, "all")
        for i, msg in enumerate(recent):
            if i > 0:
                print()
            role_color = "green" if msg.role == "assistant" else "yellow"
            if console and not args.no_color:
                console.print(f"[{role_color}][{msg.role}][/{role_color}] {msg.text_content}")
            else:
                print(f"[{msg.role}] {msg.text_content}")
        return

    # Single message by index
    try:
        if args.index >= 0:
            if args.index >= len(messages):
                print(
                    f"Error: Index {args.index} out of range (0 to {len(messages) - 1})",
                    file=sys.stderr,
                )
                sys.exit(1)
            selected_msg = messages[args.index]
        else:
            if abs(args.index) > len(messages):
                print(
                    f"Error: Index {args.index} out of range (-{len(messages)} to -1)",
                    file=sys.stderr,
                )
                sys.exit(1)
            selected_msg = messages[args.index]

        print(selected_msg.text_content)

    except IndexError:
        print(f"Error: Message index {args.index} not found", file=sys.stderr)
        sys.exit(1)


def select_message(args, console: Optional[Console]):
    """Interactive message selection using blessed"""
    session_file = resolve_session_file(args, args.base_dir)
    if not session_file:
        sys.exit(1)

    messages = load_messages_or_exit(str(session_file), args.role)

    if not messages:
        print("No messages found")
        return

    # For no-color mode, fall back to simple numbered selection
    if args.no_color or console is None:
        print("Interactive selection not available in no-color mode. Showing last 10 messages:")
        args.count = min(10, len(messages))
        list_messages(args, console)
        print("\nTo get a specific message, use: get --index <negative_index>")
        return

    term = Terminal()
    recent_messages = messages[-min(20, len(messages)) :]
    selected_index = 0

    try:
        with term.fullscreen(), term.cbreak(), term.hidden_cursor():
            while True:
                # Clear screen and draw interface
                print(term.clear(), end="")

                # Header
                print(term.bold + "📋 Conversation Messages" + term.normal)
                print("↑/↓/j/k: Navigate  Enter: Select  q: Quit")
                print("─" * term.width)

                # Message list
                for i, msg in enumerate(recent_messages):
                    neg_index = to_negative_index(i, len(recent_messages))
                    cursor = "►" if i == selected_index else " "

                    if i == selected_index:
                        # Highlight selected line
                        line_style = term.reverse
                    else:
                        line_style = term.normal

                    role_color = get_role_color(term, msg.role)

                    # Format line: cursor [index] role time preview
                    line = f"{cursor} [{neg_index:2}] {role_color}{msg.role:9}{term.normal} {msg.time_str} {msg.preview}"
                    print(line_style + line[: term.width - 1] + term.normal)

                # Preview section
                if recent_messages:
                    selected_msg = recent_messages[selected_index]
                    preview_text = selected_msg.text_content

                    print("\n" + "─" * term.width)
                    print(term.bold + f"Preview: {selected_msg.role} message" + term.normal)
                    print("─" * term.width)

                    # Show preview (limited to available screen space)
                    preview_lines = preview_text.split("\n")
                    max_preview_lines = max(1, term.height - len(recent_messages) - 8)

                    for line in preview_lines[:max_preview_lines]:
                        print(line[: term.width - 1])

                    if len(preview_lines) > max_preview_lines:
                        print("...")

                # Get user input
                key = term.inkey(timeout=None)

                if key.lower() == "q" or key.code == term.KEY_ESCAPE:
                    break
                elif key.code == term.KEY_ENTER:
                    # Output selected message and exit
                    print(term.clear(), end="")
                    print(selected_msg.text_content)
                    break
                elif key.code == term.KEY_UP or key.lower() == "k":
                    selected_index = max(0, selected_index - 1)
                elif key.code == term.KEY_DOWN or key.lower() == "j":
                    selected_index = min(len(recent_messages) - 1, selected_index + 1)
                elif key.isdigit():
                    # Jump to number (0-9)
                    digit = int(key)
                    if digit < len(recent_messages):
                        selected_index = digit
    except KeyboardInterrupt:
        pass


def browse_mode(args, console: Optional[Console]):
    """Interactive browser: select project → session → messages.

    Keys:
      - ↑/↓/j/k to navigate, Enter to select
      - b to go back, q to quit
      - c to copy selected message content (message view only)
    """
    base_dir = args.base_dir
    projects = list_projects(base_dir)

    if not projects:
        print(f"No projects found under {base_dir}")
        return

    term = Terminal()

    # Cache for session last messages to avoid repeated file reads
    session_message_cache: Dict[str, Optional[Tuple[str, str]]] = {}

    selected_project = None
    if getattr(args, "project", ""):
        selected_project = find_project_by_hint(projects, args.project)

    with term.fullscreen(), term.cbreak(), term.hidden_cursor():
        # Build display and initial selection row
        display_lines, index_map = build_project_list_display(projects, term)
        if selected_project is not None:
            try:
                target_idx = projects.index(selected_project)
                selectable_rows = [i for i, v in enumerate(index_map) if v == target_idx]
                proj_row = selectable_rows[0] if selectable_rows else 0
            except ValueError:
                proj_row = 0
        else:
            proj_row = next((i for i, v in enumerate(index_map) if v is not None), 0)

        while selected_project is None:
            # Build dynamic preview for the currently highlighted row
            preview_lines: List[str] = []
            mapped = index_map[proj_row]
            if mapped is None:
                preview_lines = ["Select a project to preview recent sessions…"]
            else:
                proj = projects[mapped]
                sess = order_session_paths(
                    list_sessions(proj),
                    limit=min(MAX_PROJECT_SESSIONS_PREVIEW, term.height // 3),
                )
                if not sess:
                    preview_lines = ["(No sessions found)"]
                else:
                    # Helper function for abbreviating session filenames
                    def abbreviate_filename(filename: str) -> str:
                        if (
                            len(filename)
                            <= FILENAME_ABBREVIATION_HEAD + FILENAME_ABBREVIATION_TAIL + 1
                        ):
                            return filename
                        return f"{filename[:FILENAME_ABBREVIATION_HEAD]}…{filename[-FILENAME_ABBREVIATION_TAIL:]}"

                    # Build enhanced preview with last message context
                    muted = _muted_color(term)
                    for session_file in sess:
                        # Session header line: timestamp + abbreviated filename (muted).
                        # The time shown is the one these rows are ordered by --
                        # labelling them with the mtime instead is how a reader
                        # starts distrusting the whole pane.
                        timestamp = datetime.fromtimestamp(
                            session_sort_time(session_file)
                        ).strftime("%Y-%m-%d %H:%M")
                        abbreviated_name = abbreviate_filename(session_file.name)
                        header_line = f"{muted}{timestamp}  {abbreviated_name}{term.normal}"
                        preview_lines.append(header_line)

                        # Get last message for this session (with cache size limit)
                        if len(session_message_cache) > SESSION_CACHE_MAX_SIZE:
                            session_message_cache.clear()
                        last_msg = get_session_last_message(session_file, session_message_cache)
                        if last_msg:
                            role, preview_text = last_msg
                            # Apply consistent role coloring
                            role_color = get_role_color(term, role)
                            indent = " " * SESSION_MESSAGE_INDENT
                            message_line = (
                                f"{indent}{role_color}[{role}]{term.normal} {preview_text}"
                            )
                            preview_lines.append(message_line)
                        else:
                            # No messages found
                            indent = " " * SESSION_MESSAGE_INDENT
                            no_msg_line = f"{indent}{muted}(No messages found){term.normal}"
                            preview_lines.append(no_msg_line)

                        # Add spacing between sessions
                        preview_lines.append("")
            print(term.clear(), end="")
            draw_split_list_with_preview(
                term,
                header="📁 Select Project  (↑/↓/j/k navigate, →/Enter select, q quit)",
                items=display_lines,
                selected_index=proj_row,
                preview_lines=preview_lines,
                footer=f"Base: {base_dir}",
            )
            key = term.inkey(timeout=None)
            if key.lower() == "q" or key.code == term.KEY_ESCAPE:
                return
            if key.code == term.KEY_ENTER or key.code == term.KEY_RIGHT:
                mapped = index_map[proj_row]
                if mapped is not None:
                    selected_project = projects[mapped]
                    break
            if key.code == term.KEY_UP or key.lower() == "k":
                next_row = max(0, proj_row - 1)
                # Skip headers
                while next_row > 0 and index_map[next_row] is None:
                    next_row -= 1
                proj_row = next_row
            elif key.code == term.KEY_DOWN or key.lower() == "j":
                next_row = min(len(display_lines) - 1, proj_row + 1)
                while next_row < len(display_lines) - 1 and index_map[next_row] is None:
                    next_row += 1
                proj_row = next_row

        sessions = order_session_paths(list_sessions(selected_project))
        if not sessions:
            print(term.clear(), end="")
            print(f"No sessions found in {selected_project}")
            return

        selected_session: Optional[Path] = None
        if getattr(args, "latest", False):
            selected_session = sessions[0]
        else:

            def abbreviate_session_filename(filename: str) -> str:
                if len(filename) <= FILENAME_ABBREVIATION_HEAD + FILENAME_ABBREVIATION_TAIL + 1:
                    return filename
                return f"{filename[:FILENAME_ABBREVIATION_HEAD]}…{filename[-FILENAME_ABBREVIATION_TAIL:]}"

            sess_index = 0
            while selected_session is None:
                # Left list: abbreviated file name and timestamp
                items = [
                    f"{abbreviate_session_filename(s.name)}  "
                    f"({datetime.fromtimestamp(session_sort_time(s)).strftime('%Y-%m-%d %H:%M:%S')})"
                    for s in sessions
                ]

                # Right preview: parse selected session and show last few messages
                preview_lines: List[str] = []
                try:
                    parsed = load_messages(str(sessions[sess_index]), "all")
                    if parsed:
                        # Show newest first in preview
                        tail = list(reversed(parsed[-min(10, len(parsed)) :]))
                        preview_lines = [f"{m.time_str} [{m.role}] {m.preview}" for m in tail]
                    else:
                        preview_lines = ["(No messages found)"]
                except (OSError, UnicodeDecodeError):
                    # Same pair last_mode narrows to — load_messages handles the
                    # rest itself. In a TUI a bug must not masquerade as a
                    # damaged session, since the traceback is the only signal.
                    preview_lines = ["(Unable to read session)"]

                print(term.clear(), end="")
                draw_split_list_with_preview(
                    term,
                    header=f"🗂 Select Session in {format_project_identifier(selected_project.name)}  (↑/↓/j/k navigate, →/Enter select, ←/b back, q quit)",
                    items=items,
                    selected_index=sess_index,
                    preview_lines=preview_lines,
                    footer=str(selected_project),
                )
                key = term.inkey(timeout=None)
                if key.lower() == "q" or key.code == term.KEY_ESCAPE:
                    return
                if key.lower() == "b" or key.code == term.KEY_LEFT:
                    selected_project = None
                    break
                if key.code == term.KEY_ENTER or key.code == term.KEY_RIGHT:
                    selected_session = sessions[sess_index]
                    break
                if key.code == term.KEY_UP or key.lower() == "k":
                    sess_index = max(0, sess_index - 1)
                elif key.code == term.KEY_DOWN or key.lower() == "j":
                    sess_index = min(len(sessions) - 1, sess_index + 1)

            if selected_project is None:
                return browse_mode(args, console)

        if selected_session is None:
            return

        msgs = load_messages_or_exit(str(selected_session), getattr(args, "role", "all"))
        if not msgs:
            print(term.clear(), end="")
            print("No messages found")
            return

        recent_n = max(1, getattr(args, "count", 20))
        # Newest first in the list view
        recent_msgs = list(reversed(msgs[-recent_n:]))
        sel = 0
        show_json = False
        preview_offset = 0

        while True:
            items = []
            for i, m in enumerate(recent_msgs):
                # recent_msgs is reversed (newest first), so i=0 is -1, i=1 is -2, etc.
                neg_index = -(i + 1)
                role = "assistant" if m.role == "assistant" else "user"
                role_color = get_role_color(term, role)
                role_str = f"{role_color}{role:9}{term.normal}"
                items.append(f"[{neg_index:2}] {role_str} {m.time_str}  {m.preview}")

            selected_msg = recent_msgs[sel]

            if show_json:
                try:
                    pretty = json.dumps(selected_msg.raw_data, indent=2, ensure_ascii=False)
                    preview_lines = pretty.split("\n")
                except (TypeError, ValueError):
                    # json.dumps raises TypeError on a non-serializable value and
                    # ValueError on circular references. Nothing else applies.
                    preview_lines = ["(Unable to pretty-print JSON)"]
            else:
                preview_text = selected_msg.text_content
                preview_lines = preview_text.split("\n")

            print(term.clear(), end="")
            draw_split_list_with_preview(
                term,
                header=f"📋 Messages in {selected_session.name}  (↑/↓/j/k navigate, →/Enter print & exit, c copy, ←/b back, q quit, d display, [/ ] scroll)",
                items=items,
                selected_index=sel,
                preview_lines=preview_lines,
                footer=str(selected_session),
                preview_offset=preview_offset,
            )

            key = term.inkey(timeout=None)
            if key.lower() == "q" or key.code == term.KEY_ESCAPE:
                break
            if key.lower() == "b" or key.code == term.KEY_LEFT:
                args.latest = False
                return browse_mode(args, console)
            if key.code == term.KEY_ENTER or key.code == term.KEY_RIGHT:
                print(term.clear(), end="")
                print(selected_msg.text_content)
                break
            if key.lower() == "c":
                ok, msg = copy_to_clipboard(selected_msg.text_content)
                print(term.move_xy(0, term.height - 1) + ("✔ " if ok else "✖ ") + msg)
                sys.stdout.flush()
            elif key.code == term.KEY_PGUP or key.lower() == "[":
                # Scroll preview up
                max_rows = term.height - 5
                preview_offset = (
                    max(0, preview_offset - max(1, max_rows // 2))
                    if key.code == term.KEY_PGUP
                    else max(0, preview_offset - 1)
                )
            elif key.code == term.KEY_PGDOWN or key.lower() == "]":
                # Scroll preview down
                max_rows = term.height - 5
                max_offset = max(0, len(preview_lines) - max_rows)
                preview_offset = (
                    min(max_offset, preview_offset + max(1, max_rows // 2))
                    if key.code == term.KEY_PGDOWN
                    else min(max_offset, preview_offset + 1)
                )
            elif key.lower() == "d":
                show_json = not show_json
                preview_offset = 0
            elif key.code == term.KEY_UP or key.lower() == "k":
                sel = max(0, sel - 1)
                preview_offset = 0
            elif key.code == term.KEY_DOWN or key.lower() == "j":
                sel = min(len(recent_msgs) - 1, sel + 1)
                preview_offset = 0


def last_mode(args, console: Optional[Console]):
    """Show recent message pairs from current directory's conversation."""
    selected_session = resolve_session_file(args, args.base_dir)
    if not selected_session:
        sys.exit(1)

    messages = load_messages_or_exit(str(selected_session), "all")

    if not messages:
        print("No messages found in conversation")
        sys.exit(1)

    # Extract recent turns
    recent_messages = extract_recent_turns(messages, args.turns, args.role)
    if not recent_messages:
        print(f"No {args.role} messages found")
        sys.exit(1)

    if getattr(args, "json", False):
        # Prefer the last assistant turn as the single "resting state" line; fall
        # back to whatever the last message is (e.g. role='user').
        assistants = [m for m in recent_messages if m.role == "assistant"]
        last_text = (assistants[-1] if assistants else recent_messages[-1]).text_content
        try:
            mtime = selected_session.stat().st_mtime
        except OSError:
            mtime = None
        # The session's real working dir and branch, read from the transcript
        # (lossless), so a fleet caller joins on absolute path — not the flattened
        # folder name. Falls back to the resolve-from dir if the header lacks cwd.
        header = read_session_header(selected_session)
        payload = {
            "session_id": selected_session.stem,
            "session_file": str(selected_session),
            "project": collapsed_project_label(selected_session.parent.name),
            "project_folder": selected_session.parent.name,
            "cwd": header["cwd"] or getattr(args, "cwd", None) or os.getcwd(),
            "git_branch": header["git_branch"],
            "mtime": mtime,
            "role": args.role,
            "turns": args.turns,
            "last_text": last_text,
            "messages": [{"role": m.role, "text": m.text_content} for m in recent_messages],
        }
        print(json.dumps(payload, indent=2))
        return

    # Format and display the messages
    project_name = format_project_identifier(selected_session.parent.name)
    session_name = selected_session.name

    # Build header description
    role_desc = "messages" if args.role == "all" else f"{args.role} messages"
    turn_desc = f"{args.turns} turn{'s' if args.turns > 1 else ''}"

    # Show header info
    if console and not args.no_color:
        console.print(f"[dim]Last {turn_desc} ({role_desc}) from {project_name}[/dim]")
        console.print(f"[dim]Session: {session_name}[/dim]")
        console.print()
    else:
        print(f"Last {turn_desc} ({role_desc}) from {project_name}")
        print(f"Session: {session_name}")
        print()

    # Display each message
    for i, msg in enumerate(recent_messages):
        if i > 0:
            print()  # Add space between messages

        # Show message with role
        role_color = "green" if msg.role == "assistant" else "yellow"
        if console and not args.no_color:
            console.print(f"[{role_color}][{msg.role}][/{role_color}] {msg.text_content}")
        else:
            print(f"[{msg.role}] {msg.text_content}")


# -------- Export mode: render session as self-contained markdown --------

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def load_raw_entries(file_path: str) -> List[Dict]:
    """Parse all JSONL lines as raw dicts. Unlike load_messages(), keeps everything."""
    entries = []
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


@dataclass
class SessionMetadata:
    session_id: str = ""
    slug: str = ""
    origin_session_id: str = ""  # the root session this one was /cleared from, if any
    model: str = ""  # the model answering now: a session can switch mid-way
    model_first: str = ""
    version: str = ""
    git_branch: str = ""
    cwd: str = ""
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    compaction_count: int = 0
    compaction_events: List[Dict] = field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    plan_content: str = ""
    logical_parent_uuids: List[str] = field(default_factory=list)
    session_count: int = 1

    @property
    def duration_str(self) -> str:
        if not self.start_time or not self.end_time:
            return "unknown"
        delta = self.end_time - self.start_time
        total_seconds = int(delta.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours}h {minutes}m {seconds}s"
        if minutes:
            return f"{minutes}m {seconds}s"
        return f"{seconds}s"


def _parse_ts(ts_str: str) -> Optional[datetime]:
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _format_time(ts_str: str) -> str:
    ts = _parse_ts(ts_str)
    return ts.strftime("%H:%M:%S") if ts else "??:??:??"


def extract_session_metadata(entries: List[Dict]) -> SessionMetadata:
    """Single pass over entries to collect session metadata."""
    if _entries_are_codex(entries):
        return _codex_extract_metadata(entries)
    meta = SessionMetadata()
    for entry in entries:
        if not meta.session_id:
            meta.session_id = entry.get("sessionId", "")
        if not meta.slug:
            meta.slug = entry.get("slug", "")
        if not meta.version:
            meta.version = entry.get("version", "")
        if not meta.git_branch:
            meta.git_branch = entry.get("gitBranch", "")
        if not meta.cwd:
            meta.cwd = entry.get("cwd", "")
        if not meta.origin_session_id:
            origin = entry.get("session_id")
            if origin and origin != entry.get("sessionId"):
                meta.origin_session_id = origin

        # Model from assistant messages. The last one seen is the model answering
        # now; the first is kept so the card can say the session switched. A
        # "<synthetic>" model marks a record the harness made up, not a reply.
        if entry.get("type") == "assistant":
            model = entry.get("message", {}).get("model", "")
            if model and not model.startswith("<"):
                meta.model_first = meta.model_first or model
                meta.model = model

        # Timestamps
        ts = _parse_ts(entry.get("timestamp", ""))
        if ts:
            if meta.start_time is None or ts < meta.start_time:
                meta.start_time = ts
            if meta.end_time is None or ts > meta.end_time:
                meta.end_time = ts

        # Token usage from assistant messages
        if entry.get("type") == "assistant":
            usage = entry.get("message", {}).get("usage", {})
            meta.total_input_tokens += usage.get("input_tokens", 0)
            meta.total_input_tokens += usage.get("cache_read_input_tokens", 0)
            meta.total_input_tokens += usage.get("cache_creation_input_tokens", 0)
            meta.total_output_tokens += usage.get("output_tokens", 0)

        # Compaction events
        if entry.get("type") == "system" and entry.get("subtype") == "compact_boundary":
            compact_meta = entry.get("compactMetadata", {})
            meta.compaction_count += 1
            meta.compaction_events.append(
                {
                    "timestamp": entry.get("timestamp", ""),
                    "trigger": compact_meta.get("trigger", "unknown"),
                    "pre_tokens": compact_meta.get("preTokens", 0),
                }
            )
            parent_uuid = entry.get("logicalParentUuid")
            if parent_uuid:
                meta.logical_parent_uuids.append(parent_uuid)

        # Plan content (take the last one if multiple)
        if entry.get("type") == "user" and entry.get("planContent"):
            meta.plan_content = entry["planContent"]

    return meta


# ====================================================================
# Work-location derivation
#
# Sessions are filed under the directory Claude was *launched* from, but the
# work often lands in a different repo (e.g. launched from ~/src/project-a,
# edits go to ~/src/project-b). The launch cwd is the only location the
# rest of the tool surfaces; these helpers mine the tool calls for where the
# edits/PRs *actually* happened so `summary` and `search` can show it.
# ====================================================================

# github.com/<org>/<repo>[.git]/pull/<n>  (also matches git@ / ssh forms)
_PR_URL_RE = re.compile(r"github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/pull/(\d+)")
_CHECKOUT_RE = re.compile(r"git\s+(?:checkout|switch)\s+(?:-b\s+|-c\s+)?([^\s;&|]+)")
_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def _tilde(path_str: str) -> str:
    """Collapse $HOME prefix to ~ for display."""
    home = str(Path.home())
    if path_str == home:
        return "~"
    if path_str.startswith(home + "/"):
        return "~" + path_str[len(home) :]
    return path_str


def _short_root(tilde_path: str) -> str:
    """Last two path segments — distinguishes worktrees without full paths."""
    parts = [p for p in tilde_path.split("/") if p and p != "~"]
    return "/".join(parts[-2:]) if parts else tilde_path


@functools.lru_cache(maxsize=4096)
def _repo_root_for(path_str: str) -> Optional[str]:
    """Walk up from a path to the enclosing git repo root (dir or file `.git`).

    Cached. Returns None when the path no longer exists or has no `.git`
    ancestor (e.g. the work happened in a since-deleted worktree).
    """
    try:
        p = Path(path_str)
        cur = p if p.is_dir() else p.parent
    except (OSError, ValueError):
        return None
    for _ in range(40):  # bound the climb
        try:
            if (cur / ".git").exists():
                return str(cur)
        except OSError:
            return None
        if cur == cur.parent:
            break
        cur = cur.parent
    return None


_WORKTREE_GITDIR = "/.git/worktrees/"


def _main_repo_from_gitfile(dotgit: Path) -> Optional[str]:
    """A linked worktree's `.git` is a *file* ``gitdir: <main>/.git/worktrees/<name>``.
    Return the canonical main-repo root (the path before ``/.git/worktrees/``), or
    None if the file is missing/unparseable or points anywhere else.

    A submodule's `.git` is a file too, ``gitdir: ../../.git/modules/<name>``, and
    it is its own repository: splitting that on ``/.git/`` yielded ``../..``, an
    invalid identity that grouped unrelated submodule sessions together. Only a
    ``worktrees`` pointer is followed, and a relative one is resolved against the
    directory that holds the `.git` file, which is how git writes them.
    """
    try:
        first = dotgit.read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, IndexError, ValueError):
        return None
    if not first.startswith("gitdir:"):
        return None
    gitdir = first.split(":", 1)[1].strip()
    if _WORKTREE_GITDIR not in gitdir:
        return None
    root = gitdir.split(_WORKTREE_GITDIR, 1)[0]
    if not root:
        return None
    if not os.path.isabs(root):
        root = os.path.normpath(os.path.join(str(dotgit.parent), root))
    return root


def _canonical_repo_root(path_str: str) -> Optional[str]:
    """Enclosing git repo root, worktree-normalized to the canonical *main* repo.

    ``_repo_root_for`` stops at the nearest ``.git`` — which for a linked worktree is
    a ``.git`` *file* at the worktree's own root, so sibling worktrees of one repo
    resolve apart. This resolves that file's ``gitdir:`` pointer to the main repo, so
    all worktrees of a repo share one identity. Returns None for a non-repo dir or a
    since-deleted worktree (identity unrecoverable from disk). No in-tree caller —
    this is claude-graph's public ``canonical_repo_root`` (re-exported via the seam
    in claude_graph/sessions.py), so it must stay even though vulture flags it.
    """
    root = _repo_root_for(path_str)
    if root is None:
        return None
    dotgit = Path(root) / ".git"
    try:
        is_file = dotgit.is_file()
    except OSError:
        return root
    if is_file:
        return _main_repo_from_gitfile(dotgit) or root
    return root


@dataclass
class WorkSignals:
    launch_cwd: str = ""
    cwds: List[str] = field(default_factory=list)  # distinct, in order seen
    repo_edits: Dict[str, int] = field(default_factory=dict)  # tilde root -> edit count
    edited_paths: List[str] = field(default_factory=list)  # distinct abs paths, in order
    repos_are_dirs: bool = False  # True if fell back to dirnames
    prs: List[str] = field(default_factory=list)  # "org/repo#123"
    branches: List[str] = field(default_factory=list)

    @property
    def primary_root(self) -> Optional[str]:
        if not self.repo_edits:
            return None
        return max(self.repo_edits.items(), key=lambda kv: kv[1])[0]

    @property
    def has_signal(self) -> bool:
        return bool(self.repo_edits or self.prs or self.branches)


def derive_work_signals(entries: List[Dict]) -> WorkSignals:
    """Mine tool calls for where edits/PRs actually landed."""
    if _entries_are_codex(entries):
        return _codex_derive_work_signals(entries)
    ws = WorkSignals()
    seen_cwd: set = set()
    seen_paths: set = set()
    pr_seen: set = set()
    branch_seen: set = set()
    repo_edits: Dict[str, int] = defaultdict(int)
    fallback_dirs: Dict[str, int] = defaultdict(int)

    for entry in entries:
        cwd = entry.get("cwd", "")
        if cwd and cwd not in seen_cwd:
            seen_cwd.add(cwd)
            ws.cwds.append(_tilde(cwd))

        if entry.get("type") not in ("assistant", "user"):
            continue
        msg = entry.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue

        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = block.get("name", "")
            tin = block.get("input")
            if not isinstance(tin, dict):
                continue
            if name in _EDIT_TOOLS:
                fp = tin.get("file_path") or tin.get("notebook_path") or ""
                if fp:
                    if fp not in seen_paths:
                        seen_paths.add(fp)
                        ws.edited_paths.append(fp)
                    root = _repo_root_for(fp)
                    if root:
                        repo_edits[_tilde(root)] += 1
                    else:
                        fallback_dirs[_tilde(str(Path(fp).parent))] += 1
            elif name == "Bash":
                cmd = tin.get("command", "")
                for m in _CHECKOUT_RE.finditer(cmd):
                    b = m.group(1)
                    if b and not b.startswith("-") and b not in branch_seen:
                        branch_seen.add(b)
                        ws.branches.append(b)

        # PR URLs can appear anywhere: bash commands, gh output, prose.
        for _, text in iter_content_fragments(content):
            for m in _PR_URL_RE.finditer(text):
                key = f"{m.group(1)}/{m.group(2)}#{m.group(3)}"
                if key not in pr_seen:
                    pr_seen.add(key)
                    ws.prs.append(key)

    if repo_edits:
        ws.repo_edits = dict(repo_edits)
    elif fallback_dirs:
        ws.repo_edits = dict(fallback_dirs)
        ws.repos_are_dirs = True
    ws.launch_cwd = ws.cwds[0] if ws.cwds else ""
    return ws


def format_work_signals(ws: WorkSignals) -> List[str]:
    """Plain display lines for `summary`. Empty if no signal."""
    if not ws.has_signal:
        return []
    lines: List[str] = []
    if ws.repo_edits:
        ranked = sorted(ws.repo_edits.items(), key=lambda kv: -kv[1])
        parts = [f"{_short_root(root)} ({n} edit{'s' if n != 1 else ''})" for root, n in ranked[:4]]
        label = "Edited dirs" if ws.repos_are_dirs else "Worked in"
        lines.append(f"{label}: {', '.join(parts)}")
    if ws.prs:
        lines.append(f"PRs:       {', '.join(ws.prs[:6])}")
    if ws.branches:
        lines.append(f"Branches:  {', '.join(ws.branches[:6])}")
    # The headline insight: launched somewhere, worked elsewhere.
    primary = ws.primary_root
    if (
        primary
        and ws.launch_cwd
        and not (
            ws.launch_cwd == primary
            or ws.launch_cwd.startswith(primary + "/")
            or primary.startswith(ws.launch_cwd + "/")
        )
    ):
        lines.append(
            f"           ↳ launched from {_short_root(ws.launch_cwd)}, "
            f"but edits landed in {_short_root(primary)}"
        )
    return lines


def work_signals_oneline(ws: WorkSignals) -> str:
    """Compact single-line summary for `search` results. '' if no signal."""
    if not ws.has_signal:
        return ""
    bits: List[str] = []
    primary = ws.primary_root
    if primary:
        n = ws.repo_edits[primary]
        bits.append(f"{_short_root(primary)} ({n} edit{'s' if n != 1 else ''})")
    if ws.prs:
        bits.append(" · ".join(ws.prs[:2]))
    if not bits and ws.branches:
        bits.append(ws.branches[0])
    return "  ".join(bits)


# ====================================================================
# Codex rollout source
#
# OpenAI's Codex CLI writes transcripts to ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
# in a different shape: every record wraps a typed `payload`, session facts live in a
# `session_meta` header, and there is no named Edit tool — file edits ride inside
# `apply_patch` heredocs in exec commands. These functions normalize that format into
# the SAME SessionMetadata / WorkSignals / prose the Claude path produces, so the seam
# below (extract_session_metadata / derive_work_signals / iter_session_prose) can serve
# both sources and every downstream mode works unchanged. Validated against real
# rollouts before wiring.
# ====================================================================

_CODEX_RECORD_TYPES = {"session_meta", "event_msg", "response_item", "turn_context", "world_state"}
# apply_patch file markers ride inside JS-escaped exec strings, so a path terminates at
# an escaped newline (\n), a real newline, or a closing quote.
_CODEX_PATCH_FILE_RE = re.compile(r"\*\*\* (?:Update|Add|Delete) File: (.+?)(?:\\n|[\"\n]|$)")


def _codex_payload(entry: Dict) -> Dict:
    p = entry.get("payload")
    return p if isinstance(p, dict) else {}


def _entries_are_codex(entries: List[Dict]) -> bool:
    """Codex records carry a top-level type in _CODEX_RECORD_TYPES (or a payload dict);
    Claude records are user/assistant/summary/system with a `message`. The two type
    vocabularies are disjoint, so a peek at the first real record decides it."""
    for e in entries[:5]:
        if not isinstance(e, dict):
            continue
        if e.get("type") in _CODEX_RECORD_TYPES or isinstance(e.get("payload"), dict):
            return True
        if "message" in e or "sessionId" in e or e.get("type") in ("user", "assistant", "summary"):
            return False
    return False


def _file_is_codex(file_path) -> bool:
    """Sniff a session file's format from its first parseable record."""
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    return _entries_are_codex([json.loads(raw)])
                except json.JSONDecodeError:
                    continue
    except OSError:
        return False
    return False


def _codex_iter_prose_entries(entries: List[Dict]) -> Iterator[Tuple[str, str]]:
    """(role_tag, text) over a Codex rollout, mirroring iter_content_fragments' kinds.

    Uses the UI-facing event_msg turns (user_message/agent_message/agent_reasoning) and
    the exec tool I/O; skips response_item/message, which duplicates the model I/O and
    would double-count every turn.
    """
    for e in entries:
        p = _codex_payload(e)
        pt = p.get("type")
        if pt == "user_message":
            msg = p.get("message")
            if isinstance(msg, str) and msg.strip():
                yield ("user", msg)
        elif pt == "agent_message":
            msg = p.get("message")
            if isinstance(msg, str) and msg.strip():
                yield ("asst", msg)
        elif pt == "agent_reasoning":
            msg = p.get("text")
            if isinstance(msg, str) and msg.strip():
                yield ("think", msg)
        elif pt in ("custom_tool_call", "function_call"):
            for k in ("input", "arguments"):
                v = p.get(k)
                if isinstance(v, str) and v:
                    yield ("tool_in", v)
        elif pt in ("custom_tool_call_output", "function_call_output"):
            v = p.get("output")
            if isinstance(v, str) and v:
                yield ("tool", v)


def _codex_extract_metadata(entries: List[Dict]) -> SessionMetadata:
    """Codex counterpart of extract_session_metadata. Token totals are cumulative in
    Codex's token_count events, so take the latest rather than summing."""
    meta = SessionMetadata()
    for e in entries:
        p = _codex_payload(e)
        etype = e.get("type")
        if etype == "session_meta":
            meta.session_id = meta.session_id or p.get("session_id", "")
            meta.cwd = meta.cwd or p.get("cwd", "")
            meta.version = meta.version or p.get("cli_version", "")
        elif etype == "turn_context":
            meta.model = meta.model or p.get("model", "")
        if p.get("type") == "token_count":
            usage = (p.get("info") or {}).get("total_token_usage") or {}
            if usage:
                meta.total_input_tokens = usage.get("input_tokens", meta.total_input_tokens)
                meta.total_output_tokens = usage.get("output_tokens", meta.total_output_tokens)
        ts = _parse_ts(e.get("timestamp", ""))
        if ts:
            if meta.start_time is None or ts < meta.start_time:
                meta.start_time = ts
            if meta.end_time is None or ts > meta.end_time:
                meta.end_time = ts
    return meta


def _codex_derive_work_signals(entries: List[Dict]) -> WorkSignals:
    """Codex counterpart of derive_work_signals. Edits have no named Edit tool — they
    ride inside apply_patch heredocs in exec commands — and branches/PRs are mined from
    the same command/output text. Repo attribution reuses the Claude _repo_root_for path."""
    ws = WorkSignals()
    seen_paths: set = set()
    pr_seen: set = set()
    branch_seen: set = set()
    repo_edits: Dict[str, int] = defaultdict(int)
    fallback_dirs: Dict[str, int] = defaultdict(int)

    base_cwd = ""
    for e in entries:
        if e.get("type") == "session_meta":
            cwd = _codex_payload(e).get("cwd", "")
            if cwd:
                base_cwd = base_cwd or cwd
                if _tilde(cwd) not in ws.cwds:
                    ws.cwds.append(_tilde(cwd))

    for role, text in _codex_iter_prose_entries(entries):
        if role in ("tool_in", "tool"):
            for m in _CODEX_PATCH_FILE_RE.finditer(text):
                fp = m.group(1).strip()
                # apply_patch paths are often relative; resolve against the rollout's
                # cwd so a repo is never misattributed to wherever claude-ls itself runs.
                if fp and not os.path.isabs(fp) and base_cwd:
                    fp = os.path.normpath(os.path.join(base_cwd, fp))
                if fp and fp not in seen_paths:
                    seen_paths.add(fp)
                    ws.edited_paths.append(fp)
                    root = _repo_root_for(fp)
                    if root:
                        repo_edits[_tilde(root)] += 1
                    else:
                        fallback_dirs[_tilde(str(Path(fp).parent))] += 1
            for m in _CHECKOUT_RE.finditer(text):
                # Codex wraps git commands in JSON/JS, so the token can carry trailing
                # quote/brace/paren garbage that a git ref can never contain — strip it.
                b = m.group(1).rstrip("\"'\\){},")
                if b and not b.startswith("-") and b not in branch_seen:
                    branch_seen.add(b)
                    ws.branches.append(b)
        # PR URLs appear anywhere: exec commands, tool output, agent prose.
        for m in _PR_URL_RE.finditer(text):
            key = f"{m.group(1)}/{m.group(2)}#{m.group(3)}"
            if key not in pr_seen:
                pr_seen.add(key)
                ws.prs.append(key)

    if repo_edits:
        ws.repo_edits = dict(repo_edits)
    elif fallback_dirs:
        ws.repo_edits = dict(fallback_dirs)
        ws.repos_are_dirs = True
    ws.launch_cwd = ws.cwds[0] if ws.cwds else ""
    return ws


def _codex_as_claude_entries(entries: List[Dict]) -> List[Dict]:
    """Project the UI-facing user/agent turns of a rollout onto the Claude-shaped
    conversation entries the message layer and render_markdown consume, so the
    message modes (summary/list/last/get) and export produce a real conversation."""
    out: List[Dict] = []
    for e in entries:
        p = _codex_payload(e)
        pt = p.get("type")
        if pt == "user_message":
            role = "user"
        elif pt == "agent_message":
            role = "assistant"
        else:
            continue
        text = p.get("message")
        if not isinstance(text, str) or not text.strip():
            continue
        out.append(
            {
                "type": role,
                "timestamp": e.get("timestamp", ""),
                "message": {"role": role, "content": text},
            }
        )
    return out


def _codex_load_messages(file_path: str, role_filter: str = "all") -> List[ConversationMessage]:
    """Codex counterpart of load_messages, over the projected Claude-shaped turns."""
    out: List[ConversationMessage] = []
    for entry in _codex_as_claude_entries(load_raw_entries(file_path)):
        if role_filter != "all" and entry["message"]["role"] != role_filter:
            continue
        out.append(ConversationMessage(entry))
    return out


def format_tool_use_summary(tool_name: str, tool_input: Dict) -> str:
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        if len(cmd) > 120:
            cmd = cmd[:117] + "..."
        return f"Bash: `{cmd}`"
    if tool_name in ("Read", "Edit", "Write"):
        path = tool_input.get("file_path", "")
        return f"{tool_name}: `{path}`"
    if tool_name == "Grep":
        pattern = tool_input.get("pattern", "")
        path = tool_input.get("path", ".")
        return f"Grep: `{pattern}` in {path}"
    if tool_name == "Glob":
        pattern = tool_input.get("pattern", "")
        return f"Glob: `{pattern}`"
    if tool_name == "Task":
        desc = tool_input.get("description", "")
        if not desc:
            desc = tool_input.get("prompt", "")[:80]
        return f"Task: {desc}"
    if tool_name == "WebFetch":
        url = tool_input.get("url", "")
        return f"WebFetch: `{url}`"
    if tool_name == "WebSearch":
        query = tool_input.get("query", "")
        return f"WebSearch: `{query}`"
    return f"{tool_name}(...)"


def format_tool_result_summary(content, max_lines: int = 10) -> Optional[str]:
    if not content:
        return None
    if isinstance(content, list):
        text_parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(block.get("text", ""))
        text = "\n".join(text_parts)
    elif isinstance(content, str):
        text = content
    else:
        return None

    text = strip_ansi(text)
    lines = text.split("\n")
    if len(lines) <= max_lines:
        return text
    truncated = lines[:max_lines]
    truncated.append(f"... ({len(lines) - max_lines} more lines)")
    return "\n".join(truncated)


def parse_task_notification(content: str) -> Optional[Dict]:
    if not content or "<task-notification>" not in content:
        return None
    result = {}
    for tag in ("task-id", "status", "summary"):
        match = re.search(rf"<{tag}>(.*?)</{tag}>", content, re.DOTALL)
        if match:
            result[tag.replace("-", "_")] = match.group(1).strip()
    return result if result else None


@dataclass
class ExportOptions:
    include_thinking: bool = False
    include_tool_output: bool = False
    tool_output_lines: int = 10
    compact_mode: bool = False


def _extract_text(content) -> str:
    """All text fragments of a message.content payload, joined by newlines."""
    return "\n".join(t for kind, t in iter_content_fragments(content) if kind == "text")


def render_markdown(entries: List[Dict], metadata: SessionMetadata, options: ExportOptions) -> str:
    # Codex records don't fit the Claude conversation loop below; project them onto
    # the user/assistant entries it renders so exports carry the conversation, not
    # just the metadata table.
    if _entries_are_codex(entries):
        entries = _codex_as_claude_entries(entries)

    out: List[str] = []

    # Title
    title = metadata.slug or metadata.session_id or "Session Export"
    out.append(f"# Session: {title}")
    out.append("")

    # Metadata table
    out.append("| Field | Value |")
    out.append("|-------|-------|")
    if metadata.session_id:
        out.append(f"| Session ID | `{metadata.session_id}` |")
    if metadata.model:
        out.append(f"| Model | {metadata.model} |")
    if metadata.version:
        out.append(f"| Version | {metadata.version} |")
    if metadata.cwd:
        out.append(f"| Working Directory | `{metadata.cwd}` |")
    if metadata.git_branch:
        out.append(f"| Git Branch | {metadata.git_branch} |")
    if metadata.start_time:
        out.append(f"| Started | {metadata.start_time.strftime('%Y-%m-%d %H:%M:%S UTC')} |")
    if metadata.duration_str != "unknown":
        out.append(f"| Duration | {metadata.duration_str} |")
    if metadata.total_input_tokens or metadata.total_output_tokens:
        out.append(f"| Input Tokens | {metadata.total_input_tokens:,} |")
        out.append(f"| Output Tokens | {metadata.total_output_tokens:,} |")
    if metadata.compaction_count:
        out.append(f"| Compactions | {metadata.compaction_count} |")
    if metadata.session_count > 1:
        out.append(f"| Session Files | {metadata.session_count} |")
    out.append("")

    # Session lineage
    if metadata.logical_parent_uuids:
        out.append("## Session Lineage")
        out.append("")
        for uuid in metadata.logical_parent_uuids:
            out.append(f"- `{uuid}`")
        out.append("")

    # Plan content
    if metadata.plan_content:
        out.append("## Plan")
        out.append("")
        out.append(metadata.plan_content)
        out.append("")

    # Conversation body
    out.append("---")
    out.append("")

    for entry in entries:
        entry_type = entry.get("type", "")
        ts_str = entry.get("timestamp", "")
        time_str = _format_time(ts_str)

        # Session boundary marker (multi-file stitching)
        if entry_type == "session_boundary":
            sf = entry.get("session_file", "")
            out.append("---")
            out.append("")
            out.append(f"> **Session continued** — `{Path(sf).stem}`")
            out.append("")
            out.append("---")
            out.append("")
            continue

        # Skip non-conversation types
        if entry_type in ("progress", "file-history-snapshot"):
            continue

        # System entries
        if entry_type == "system":
            if entry.get("subtype") == "compact_boundary":
                compact_meta = entry.get("compactMetadata", {})
                trigger = compact_meta.get("trigger", "unknown")
                pre_tokens = compact_meta.get("preTokens", 0)
                out.append("---")
                out.append("")
                out.append(f"> **Compaction** ({trigger}) at {time_str}")
                if pre_tokens:
                    out.append(f"> Pre-compaction tokens: {pre_tokens:,}")
                out.append("")
                out.append("---")
                out.append("")
            continue

        # Queue operations (subagent notifications)
        if entry_type == "queue-operation":
            if entry.get("operation") == "enqueue":
                notification = parse_task_notification(entry.get("content", ""))
                if notification:
                    status = notification.get("status", "")
                    summary = notification.get("summary", "")
                    task_id = notification.get("task_id", "")
                    out.append(f"> **Subagent** [{task_id}] {status}: {summary}")
                    out.append("")
            continue

        # User entries
        if entry_type == "user":
            message = entry.get("message", {})
            content = message.get("content", "")

            # Compaction summary
            if entry.get("isCompactSummary"):
                text = _extract_text(content)
                out.append("### Compaction Summary")
                out.append("")
                out.append(text)
                out.append("")
                continue

            # Task notification in string content
            if isinstance(content, str) and content.startswith("<task-notification>"):
                notification = parse_task_notification(content)
                if notification:
                    status = notification.get("status", "")
                    summary = notification.get("summary", "")
                    task_id = notification.get("task_id", "")
                    out.append(f"> **Task Notification** [{task_id}] {status}: {summary}")
                    out.append("")
                    continue

            # Tool-result-only messages (no human text)
            if isinstance(content, list):
                has_text = any(isinstance(b, dict) and b.get("type") == "text" for b in content)
                has_tool_result = any(
                    isinstance(b, dict) and b.get("type") == "tool_result" for b in content
                )
                if has_tool_result and not has_text:
                    if options.include_tool_output:
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "tool_result":
                                summary = format_tool_result_summary(
                                    block.get("content", ""), options.tool_output_lines
                                )
                                if summary:
                                    out.append("<details><summary>Tool Output</summary>")
                                    out.append("")
                                    out.append("```")
                                    out.append(summary)
                                    out.append("```")
                                    out.append("")
                                    out.append("</details>")
                                    out.append("")
                    continue

            # Normal user message
            text = _extract_text(content)
            if text.strip():
                out.append(f"### User ({time_str})")
                out.append("")
                out.append(text)
                out.append("")
            continue

        # Assistant entries
        if entry_type == "assistant":
            message = entry.get("message", {})
            content = message.get("content", [])

            if isinstance(content, str):
                if content.strip():
                    out.append(f"### Assistant ({time_str})")
                    out.append("")
                    out.append(content)
                    out.append("")
                continue

            text_parts: List[str] = []
            tool_summaries: List[str] = []

            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type", "")

                if block_type == "text":
                    text = block.get("text", "")
                    if text.strip():
                        text_parts.append(text)

                elif block_type == "tool_use" and not options.compact_mode:
                    tool_name = block.get("name", "unknown")
                    tool_input = block.get("input", {})
                    tool_summaries.append(format_tool_use_summary(tool_name, tool_input))

                elif block_type == "thinking" and options.include_thinking:
                    thinking_text = block.get("thinking", "")
                    if thinking_text.strip():
                        text_parts.append(
                            f"<details><summary>Thinking</summary>\n\n{thinking_text}\n\n</details>"
                        )

            if text_parts or tool_summaries:
                out.append(f"### Assistant ({time_str})")
                out.append("")
                for text in text_parts:
                    out.append(text)
                    out.append("")
                for summary in tool_summaries:
                    out.append(f"**Tool**: {summary}")
                    out.append("")

    return "\n".join(out)


def _read_slug(path: Path) -> Optional[str]:
    """Read slug from first few entries of a JSONL session file."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= 60:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    slug = entry.get("slug", "")
                    if slug:
                        return slug
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return None


def find_related_sessions(session_path: Path) -> List[Path]:
    """Every session file that continues this one's story, ordered by first entry
    timestamp: the files sharing its slug, and its pane's lineage across /clear
    (the root and every session cleared from it). `export` concatenates them
    with a boundary between files, so a pane's history reads as one document."""
    related = {session_path}
    slug = _read_slug(session_path)
    if slug:
        related.update(
            sib for sib in session_path.parent.glob("*.jsonl") if _read_slug(sib) == slug
        )
    related.update(m.path for m in session_chain(session_path))
    return sorted(related, key=lambda f: _head_facts(str(f)).started or 0.0)


def export_mode(args, console: Optional[Console]):
    """Export a session as a self-contained markdown document."""
    session_file = resolve_session_file(args, args.base_dir)
    if not session_file:
        sys.exit(1)

    # Build options from args
    options = ExportOptions(
        include_thinking=getattr(args, "include_thinking", False),
        include_tool_output=getattr(args, "include_tool_output", False),
        tool_output_lines=getattr(args, "tool_output_lines", 10),
        compact_mode=getattr(args, "compact", False),
    )
    # --full preset
    if getattr(args, "full", False):
        options.include_thinking = True
        options.include_tool_output = True

    # Discover related session files
    if not getattr(args, "no_follow", False):
        session_files = find_related_sessions(session_file)
    else:
        session_files = [session_file]

    # Load entries from all files, inserting session boundary markers
    all_entries = []
    for i, sf in enumerate(session_files):
        if i > 0:
            all_entries.append(
                {
                    "type": "session_boundary",
                    "session_file": str(sf),
                    "timestamp": "",
                }
            )
        file_entries = load_raw_entries(str(sf))
        if file_entries and all_entries and all_entries[-1].get("type") == "session_boundary":
            all_entries[-1]["timestamp"] = file_entries[0].get("timestamp", "")
        all_entries.extend(file_entries)

    if not all_entries:
        print(f"No entries found in {session_file}", file=sys.stderr)
        sys.exit(1)

    metadata = extract_session_metadata(all_entries)
    metadata.session_count = len(session_files)
    markdown = render_markdown(all_entries, metadata, options)

    # Output
    output_path = getattr(args, "output", None)
    if output_path:
        Path(output_path).write_text(markdown, encoding="utf-8")
        if console:
            console.print(f"[green]Exported to {output_path}[/green]")
            files_note = f" across {len(session_files)} files" if len(session_files) > 1 else ""
            console.print(
                f"[dim]{len(all_entries)} entries{files_note}, {len(markdown.splitlines())} lines[/dim]"
            )
        else:
            print(f"Exported to {output_path}", file=sys.stderr)
    else:
        print(markdown)


@dataclass(frozen=True)
class _HereMatch:
    mtime: float
    path: str
    label: str
    touched: bool = False


def here_mode(args, console: Optional[Console]):
    """List sessions anchored to the current directory.

    Expands the cwd to its git repo root and — for a linked worktree — the main
    checkout, so conversations started in the source repo surface alongside ones
    started in the worktree. Results are merged and sorted by recency; a dim
    footnote notes when sessions came from a dir other than the cwd.
    """
    base_dir = args.base_dir
    if getattr(args, "dir", None):
        start = os.path.abspath(os.path.expanduser(args.dir))
    else:
        start = os.getcwd()

    related = _anchor_related_dirs(start)
    projects = list_projects(base_dir)
    by_name = {p.name: p for p in projects}
    related_real = {os.path.realpath(p): (p, label) for p, label in related}

    # Cheap path: exact folder-name match for each related dir.
    matched_projects: List[Tuple[Path, str, str]] = []
    for path, label in related:
        proj = by_name.get(_normalize_to_folder_name(path))
        if proj:
            matched_projects.append((proj, path, label))

    found: Dict[Path, _HereMatch] = {}
    for proj, path, label in matched_projects:
        for sess in list_sessions(proj):
            if sess in found:
                continue
            try:
                found[sess] = _HereMatch(sess.stat().st_mtime, path, label)
            except OSError:
                continue

    want_codex = getattr(args, "codex", False)
    # A rollout records its cwd inside the file but encodes to the same slug, so
    # the anchor test is the same one the Claude store gets -- matched by slug per
    # related dir, not by where the file happens to sit on disk.
    by_slug = {_normalize_to_folder_name(path): (path, label) for path, label in related}
    matched_slugs: Set[str] = set()
    if want_codex:
        for slug, rollouts in codex_sessions_by_project(set(by_slug)).items():
            matched_slugs.add(slug)
            path, label = by_slug[slug]
            for sess in rollouts:
                if sess in found:
                    continue
                found[sess] = _HereMatch(_safe_mtime(sess), path, label)

    deep_scan_truncated = False
    if getattr(args, "deep", False):
        matched_project_paths = {proj for proj, _, _ in matched_projects}
        # Both stores: --deep's promise is "did work here but started elsewhere",
        # which is a property of the transcript, not of which agent wrote it. A
        # rollout that edited files here is exactly what the flag describes.
        deep_groups: List[List[Path]] = [
            list_sessions(proj) for proj in projects if proj not in matched_project_paths
        ]
        if want_codex:
            deep_groups += [
                rollouts
                for slug, rollouts in codex_sessions_by_project().items()
                if slug not in matched_slugs
            ]
        for sessions in deep_groups:
            if len(sessions) > HERE_DEEP_SCAN_LIMIT:
                deep_scan_truncated = True
            for sess in sessions[:HERE_DEEP_SCAN_LIMIT]:
                if sess in found:
                    continue
                hit = next(
                    (related_real[d] for d in _session_anchor_dirs(sess) if d in related_real),
                    None,
                )
                if hit:
                    path, label = hit
                    found[sess] = _HereMatch(_safe_mtime(sess), path, label, touched=True)

    if not found:
        what = "conversations" if want_codex else "Claude conversations"
        print(f"No {what} anchored at {_tilde(start)}.")
        checked = ", ".join(_tilde(p) for p, _ in related)
        print(f"  Checked: {checked}", file=sys.stderr)
        # A rename is the one miss --deep can't help with: the stranded sessions
        # record the old path, so they match neither the slug nor an anchor dir.
        # Both hints can be right at once, so a candidate doesn't suppress --deep.
        print_moved_project_hint(base_dir, start, codex=want_codex)
        if not getattr(args, "deep", False):
            print(
                "  Try --deep to also scan sessions that did work here but started elsewhere.",
                file=sys.stderr,
            )
        return

    sort_key = getattr(args, "sort", SESSION_SORT_DEFAULT)
    ranked = rank_sessions(
        [(sess, match.mtime, (sess, match)) for sess, match in found.items()],
        sort_key,
        args.count,
    )
    anchored_paths = list(
        dict.fromkeys(
            match.path for _, match in ranked if not match.touched and match.label != "cwd"
        )
    )
    touched_paths = list(dict.fromkeys(match.path for _, match in ranked if match.touched))
    # Same rule as the sessions listing: the column appears when both stores could
    # be on screen, so a row's source is never left to be guessed.
    show_source = want_codex

    if console and not args.no_color:
        table = Table(show_header=True, header_style="bold blue", box=box.SIMPLE, expand=True)
        table.add_column("#", style="dim", width=3)
        table.add_column("", style="yellow", width=1)
        table.add_column("ID", width=8)
        if show_source:
            table.add_column("Src", style="dim", width=6)
        table.add_column(SESSION_TIME_HEADER[sort_key], style="dim", width=11)
        table.add_column("T", style="dim", width=4, justify="right")
        table.add_column("Topic", ratio=1, no_wrap=False)
        for i, (sess, match) in enumerate(ranked):
            slug, first_user, turn_count = get_session_preview(sess)
            time_str = datetime.fromtimestamp(
                session_sort_time(sess, sort_key, match.mtime)
            ).strftime("%m-%d %H:%M")
            topic = first_user or slug or "(empty)"
            cell = Text(topic)
            note = "" if _file_is_codex(sess) else _lineage_note(session_origin(sess))
            if note:
                cell.append("\n" + note, style="dim")
            row = [str(i + 1), "~" if match.touched else "", short_id_for_path(sess)]
            if show_source:
                row.append(_session_source(sess))
            row += [time_str, str(turn_count) if turn_count else "-", cell]
            table.add_row(*row)
        console.print(table)
        if anchored_paths:
            extra = ", ".join(_tilde(p) for p in anchored_paths)
            console.print(f"[dim]Includes sessions anchored at {extra}.[/dim]")
        if touched_paths:
            extra = ", ".join(_tilde(p) for p in touched_paths)
            console.print(f"[dim]~ Sessions touched {extra} but were launched elsewhere.[/dim]")
        if deep_scan_truncated:
            console.print(
                f"[dim]Deep scan was limited to the {HERE_DEEP_SCAN_LIMIT} newest "
                "sessions per project.[/dim]"
            )
        console.print(f"[dim]{_here_footer(show_source)}[/dim]")
    else:
        for i, (sess, match) in enumerate(ranked):
            slug, first_user, turn_count = get_session_preview(sess)
            time_str = datetime.fromtimestamp(
                session_sort_time(sess, sort_key, match.mtime)
            ).strftime("%m-%d %H:%M")
            topic = first_user or slug or "(empty)"
            marker = "~" if match.touched else " "
            src = f"  {_session_source(sess):6s}" if show_source else ""
            print(
                f"{i + 1:3} {marker} {short_id_for_path(sess)}{src}  {time_str}  "
                f"[{turn_count}t]  {topic}"
            )
            note = "" if _file_is_codex(sess) else _lineage_note(session_origin(sess))
            if note:
                print(f"      {note}")
        if anchored_paths:
            extra = ", ".join(_tilde(p) for p in anchored_paths)
            print(f"(includes sessions anchored at {extra})")
        if touched_paths:
            extra = ", ".join(_tilde(p) for p in touched_paths)
            print(f"(~ sessions touched {extra} but were launched elsewhere)")
        if deep_scan_truncated:
            print(
                f"(deep scan was limited to the {HERE_DEEP_SCAN_LIMIT} newest sessions per project)"
            )


# True for every row, both stores: the three id commands resolve rollouts too.
# `resume` is the one that doesn't, and it says so when handed one rather than
# being pre-announced here for a row nobody may want to resume.
_SESSIONS_FOOTER = "Use: summary <id>  |  get <id> --tail 5  |  last -s <id> --turns 5"


def _session_source(session_file: Path) -> str:
    """The store a transcript came from, spelled as the `Src` column spells it."""
    return "codex" if _file_is_codex(session_file) else "claude"


def _here_footer(mixed: bool) -> str:
    """`here`'s footer, which must not advertise an action the rows can't take.

    `here` shows one store or both, and only Claude rows can be resumed -- resume
    rejects a rollout by design. With rollouts on screen and no way to tell which
    row is which, `resume <id>` is advice that fails for an unmarked subset, so
    the mixed listing borrows the sessions footer, which drops it for the same
    reason.
    """
    if mixed:
        return _SESSIONS_FOOTER
    return "Use: summary <id>  |  resume <id>  |  last -s <id> --turns 5"


def sessions_mode(args, console: Optional[Console]):
    """List recent sessions with IDs, project, time, and first message preview."""
    base_dir = args.base_dir
    limit = args.count
    verbose = getattr(args, "verbose", False)

    include_codex = getattr(args, "codex", False)
    sort_key = getattr(args, "sort", SESSION_SORT_DEFAULT)

    # Decide scope as project *keys*: None means everything. Passing a concrete
    # project list here would scope Codex to directories that exist under
    # ~/.claude, hiding every Codex-only project.
    if hasattr(args, "project") and args.project:
        keys = [p.name for p in list_projects(base_dir)]
        if include_codex:
            keys += [r.project_key for r in iter_codex_session_refs()]
        key = find_project_key_by_hint(sorted(set(keys)), args.project)
        if not key:
            print(f"No project matching '{args.project}'", file=sys.stderr)
            sys.exit(1)
        scope_keys = {key}
    elif not getattr(args, "all", False):
        cwd_proj = find_project_from_cwd(base_dir)
        # No Claude project for this directory keeps the existing fallback of
        # listing everything, rather than scoping to a key with nothing behind it.
        scope_keys = {cwd_proj.name} if cwd_proj else None
    else:
        scope_keys = None

    # Gather sessions. The per-project cap inside the walk selects by mtime, which
    # is the number this sort exists to distrust -- 20 sidecar-touched transcripts
    # in one project can bury the conversation that really is the most recent.
    # Under any other key the walk hands over everything and `rank_sessions` bounds
    # the work instead: exactly ranked, with no cap to hide behind.
    refs = iter_session_refs(
        base_dir,
        project_keys=scope_keys,
        include_codex=include_codex,
        per_project=20 if sort_key == "touched" else None,
    )
    all_sessions = rank_sessions([(r.path, r.mtime, r) for r in refs], sort_key, limit)

    if not all_sessions:
        print("No sessions found")
        return

    # The source column only appears when a second store is in play, so the
    # default listing stays exactly as it was.
    show_source = include_codex

    if console and not args.no_color:
        table = Table(show_header=True, header_style="bold blue", box=box.SIMPLE, expand=True)
        table.add_column("#", style="dim", width=3)
        table.add_column("ID", width=8)
        if show_source:
            table.add_column("Src", style="dim", width=6)
        table.add_column("Project", style="cyan", width=18)
        table.add_column(SESSION_TIME_HEADER[sort_key], style="dim", width=11)
        table.add_column("T", style="dim", width=4, justify="right")
        table.add_column("Topic", ratio=1, no_wrap=False)

        for i, ref in enumerate(all_sessions):
            slug, first_user, turn_count = get_session_preview(ref.path)
            proj_label = collapsed_project_label(ref.project_key)
            time_str = datetime.fromtimestamp(
                session_sort_time(ref.path, sort_key, ref.mtime)
            ).strftime("%m-%d %H:%M")
            short_id = session_short_id(ref)
            topic = first_user or slug or "(empty)"
            turns_str = str(turn_count) if turn_count else "-"

            cell = Text(topic)
            note = _lineage_note(session_origin(ref.path)) if ref.source == "claude" else ""
            if note:
                cell.append("\n" + note, style="dim")
            row = [str(i + 1), short_id]
            if show_source:
                row.append(ref.source)
            row += [proj_label, time_str, turns_str, cell]
            table.add_row(*row)

        console.print(table)
        if verbose:
            # Paths go under the table rather than in a column: a rollout path is
            # far too wide to sit inline. The index ties them back to the rows.
            for i, ref in enumerate(all_sessions):
                console.print(f"[dim]{i + 1:3}  -> {_tilde(str(ref.path))}[/dim]")
        console.print(f"[dim]{_SESSIONS_FOOTER}[/dim]")
    else:
        for i, ref in enumerate(all_sessions):
            slug, first_user, turn_count = get_session_preview(ref.path)
            proj_label = collapsed_project_label(ref.project_key)
            time_str = datetime.fromtimestamp(
                session_sort_time(ref.path, sort_key, ref.mtime)
            ).strftime("%m-%d %H:%M")
            short_id = session_short_id(ref)
            topic = first_user or slug or "(empty)"
            src = f"  {ref.source:6s}" if show_source else ""
            print(
                f"{i + 1:3}  {short_id}{src}  {time_str}  "
                f"{proj_label:20s}  [{turn_count}t]  {topic}"
            )
            note = _lineage_note(session_origin(ref.path)) if ref.source == "claude" else ""
            if note:
                print(f"     {note}")
            if verbose:
                print(f"     -> {_tilde(str(ref.path))}")
        print(_SESSIONS_FOOTER)


def _normalize_to_folder_name(input_str: str) -> str:
    """Normalize a resolve-input into a Claude project folder name.

    Accepts:
      - a folder name already in flattened form (`-Users-you-foo`)
      - an absolute path or `~/...` path (re-flatten with the Claude rule)
      - a relative-looking fragment with '/' (treated like a path)
    """
    s = os.path.expanduser(input_str.strip())
    if s.startswith("-"):
        return s  # already a flattened slug
    if s.startswith("/"):
        return encode_path_to_folder_name(s)
    # Relative fragment: encode and prepend the leading '-' Claude uses.
    return "-" + encode_path_to_folder_name(s)


# How long after its last write a transcript still counts as possibly-live. Claude
# appends on every turn, so a hot file means something may be driving the session.
_LIVE_TRANSCRIPT_WINDOW_S = 300


def _process_table() -> List[Tuple[int, int, str]]:
    """(pid, ppid, argv) for every process, or [] if ps is unavailable."""
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,args="], capture_output=True, text=True, timeout=5
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
    if out.returncode != 0:
        return []
    rows: List[Tuple[int, int, str]] = []
    for ln in out.stdout.splitlines():
        parts = ln.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
        except ValueError:
            continue
    return rows


def _is_claude_argv(argv: str) -> bool:
    """True when this process *is* the Claude CLI, not something that mentions it.

    Matched on the executable alone, so a pane sitting on `rg claude` or an editor
    holding claude-ls.py open doesn't read as a live session.
    """
    head = argv.split(None, 1)[0] if argv else ""
    if os.path.basename(head).lstrip("-") == "claude":
        return True
    # Background jobs exec the versioned binary directly:
    # ~/.local/share/claude/versions/2.1.302 --session-id <id> …
    parent = os.path.dirname(head)
    return (
        os.path.basename(parent) == "versions"
        and os.path.basename(os.path.dirname(parent)) == "claude"
    )


def _claude_driving_session(full_id: str, table: List[Tuple[int, int, str]]) -> Optional[int]:
    """pid of a Claude explicitly resumed on this id (`claude -r <id>`), if live.

    A bare `claude` carries no session id in its argv, so this catches resumes
    (including ours) and nothing else. The session registry is the exact signal;
    this covers Claudes too old to write one.
    """
    for pid, _ppid, argv in table:
        if not _is_claude_argv(argv):
            continue
        if full_id in argv.split()[1:]:
            return pid
    return None


def _tmux_list_panes(fmt: str) -> Optional[List[List[str]]]:
    """`tmux list-panes -a -F fmt`, one tab-split row per pane, or None when
    tmux can't answer."""
    try:
        out = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", fmt],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if out.returncode != 0:
        return None
    return [ln.split("\t") for ln in out.stdout.splitlines()]


def _tmux_panes() -> List[Tuple[str, int, str]]:
    """(session_name, pane_pid, pane_cwd) per live pane, or [] if tmux is absent."""
    rows = _tmux_list_panes("#{session_name}\t#{pane_pid}\t#{pane_current_path}") or []
    panes: List[Tuple[str, int, str]] = []
    for parts in rows:
        if len(parts) != 3:
            continue
        try:
            panes.append((parts[0], int(parts[1]), parts[2]))
        except ValueError:
            continue
    return panes


def _path_within(path: str, root: str) -> bool:
    """True when `path` is `root` or sits under it. Ancestors don't count."""
    try:
        p = os.path.realpath(path)
    except OSError:
        return False
    return p == root or p.startswith(root.rstrip(os.sep) + os.sep)


def _subtree_has(root_pid: int, kids: Dict[int, List[int]], targets: set) -> bool:
    """Whether root_pid or any descendant is in `targets`."""
    stack, seen = [root_pid], {root_pid}
    while stack:
        cur = stack.pop()
        if cur in targets:
            return True
        for k in kids.get(cur, ()):
            if k not in seen:
                seen.add(k)
                stack.append(k)
    return False


def _claude_tmux_sessions(
    target_dir: str, table: List[Tuple[int, int, str]], ignore_pids: Iterable[int] = ()
) -> List[str]:
    """tmux sessions running a live Claude anchored at `target_dir`.

    Matched on the pane's real cwd and its process tree, never on the session
    name: a session called `foo-chained` is not `foo`, and a session merely named
    after the project may hold no Claude at all (it once blocked the wrong
    sessions). Panes in a subdirectory count — launching claude from a subdir is
    normal — but ancestors don't.

    The trade is recall on one edge: a Claude driving a worktree session from a
    pane parked in the main repo no longer matches. The transcript-mtime check in
    `resume_mode` is the second net under that case.

    `ignore_pids` drops Claudes the caller has already accounted for — the ones
    the session registry says are driving some other session.
    """
    panes = _tmux_panes()
    if not panes:
        return []
    skip = set(ignore_pids)
    claude_pids = {pid for pid, _ppid, argv in table if _is_claude_argv(argv) and pid not in skip}
    if not claude_pids:
        return []
    kids: Dict[int, List[int]] = {}
    for pid, ppid, _argv in table:
        kids.setdefault(ppid, []).append(pid)

    root = os.path.realpath(target_dir)
    found: List[str] = []
    for name, pane_pid, cwd in panes:
        if name in found or not _path_within(cwd, root):
            continue
        if _subtree_has(pane_pid, kids, claude_pids):
            found.append(name)
    return found


def _seconds_since_write(path) -> Optional[float]:
    try:
        return max(0.0, time.time() - os.path.getmtime(path))
    except OSError:
        return None


def _worktree_main_repo(path_str: str) -> Optional[str]:
    """Map a worktree path to its main checkout when the worktree is gone.

    Convention: <base>/worktrees-<repo>/<branch> -> <base>/<repo>. Returns the
    sibling main-repo path if it exists on disk, else None.
    """
    if not path_str:
        return None
    parts = Path(path_str).parts
    for i, part in enumerate(parts):
        m = re.match(r"worktrees[-_](.+)", part)
        if m:
            candidate = Path(*parts[:i]) / m.group(1)
            return str(candidate) if candidate.is_dir() else None
    return None


def _git_linked_worktree_main(path_str: str) -> Optional[str]:
    """If path_str sits inside a *linked* git worktree, return the main
    worktree's toplevel; else None.

    Uses git plumbing, so it works regardless of directory naming (unlike
    _worktree_main_repo, which keys off a path convention). A linked worktree's
    git-dir differs from its git-common-dir (`<main>/.git`); the main checkout
    is that common dir's parent.
    """
    try:
        out = subprocess.run(
            [
                "git",
                "-C",
                path_str,
                "rev-parse",
                "--path-format=absolute",
                "--git-dir",
                "--git-common-dir",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if out.returncode != 0:
        return None
    lines = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
    if len(lines) != 2:
        return None
    git_dir, common_dir = lines
    if os.path.normpath(git_dir) == os.path.normpath(common_dir):
        return None  # main checkout, not a linked worktree
    main_root = os.path.dirname(common_dir)
    if not Path(main_root).is_dir():
        return None
    try:
        top = subprocess.run(
            ["git", "-C", main_root, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if top.returncode == 0 and top.stdout.strip():
            return top.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return main_root


def _anchor_related_dirs(start: str) -> List[Tuple[str, str]]:
    """Ordered, de-duplicated (path, label) dirs whose Claude projects might hold
    sessions 'anchored' at `start`: the dir itself, its git repo root, and — when
    `start` is a linked worktree — the matching dir in the main checkout.

    The worktree mapping preserves the subdirectory: from `/wt/pkg` it yields
    `/main-repo/pkg` (where the same-subdir session would be anchored) as well as
    the main repo root, so `here` works from inside a worktree subdir, not just
    its root.
    """
    out: List[Tuple[str, str]] = []
    seen: set = set()

    def add(p: Optional[str], label: str) -> None:
        if not p:
            return
        # Dedup on realpath (identity) but keep the literal path in the result:
        # callers flatten it to a project-folder slug, and Claude records the
        # literal cwd, not its realpath — so the literal is what must match.
        key = os.path.realpath(p)
        if key in seen:
            return
        seen.add(key)
        out.append((p, label))

    add(start, "cwd")
    repo_root = _repo_root_for(start)
    add(repo_root, "repo root")

    main_top = _git_linked_worktree_main(start) or _worktree_main_repo(start)
    if main_top:
        # Mirror the subdir within the worktree onto the main checkout first, so
        # the most specific match is preferred; then the bare main root.
        if repo_root:
            rel = os.path.relpath(start, repo_root)
            if rel not in (".", ""):
                add(os.path.normpath(os.path.join(main_top, rel)), "main repo")
        add(main_top, "main repo")
    return out


def _codex_anchor_dirs(entries: List[Dict]) -> set:
    """`_session_anchor_dirs` for a rollout.

    A rollout stamps no `cwd` on each event and has no `tool_use` blocks: the
    launch dir lives in `session_meta` and edits ride inside `apply_patch` text.
    `_codex_derive_work_signals` already normalizes both -- resolving relative
    patch paths against the session's cwd -- so this reuses it rather than growing
    a second reader of that shape. Two parsers for apply_patch would drift, and
    the one that drifts silently is the one only `--deep` calls.
    """
    ws = _codex_derive_work_signals(entries)
    dirs: set = set()
    for c in ws.cwds:
        # WorkSignals stores cwds tilde-abbreviated for display.
        dirs.add(os.path.realpath(os.path.expanduser(c)))
    for fp in ws.edited_paths:
        root = _repo_root_for(fp)
        dirs.add(os.path.realpath(root if root else str(Path(fp).parent)))
    return dirs


def _session_anchor_dirs(session_path: Path) -> set:
    """Realpaths a session is associated with: every recorded cwd, plus the git
    repo root (or parent dir) of every file it edited.

    `here --deep` matches against these so it catches sessions that did work in a
    dir without being launched there — a session started elsewhere that edited
    files in this repo counts, not just one whose cwd happened to be here.

    Edits outside any git repo fall back to the file's parent dir (matching
    `derive_work_signals()`), so they match only on an exact dir, not a repo
    root. This is looser than repo-rooted matching but is scoped to `--deep`.
    """
    entries = load_raw_entries(str(session_path))
    if _entries_are_codex(entries):
        return _codex_anchor_dirs(entries)

    dirs: set = set()
    for entry in entries:
        c = entry.get("cwd", "")
        if c:
            dirs.add(os.path.realpath(c))
        if entry.get("type") not in ("assistant", "user"):
            continue
        msg = entry.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            if block.get("name", "") not in _EDIT_TOOLS:
                continue
            tin = block.get("input")
            if not isinstance(tin, dict):
                continue
            fp = tin.get("file_path") or tin.get("notebook_path") or ""
            if not fp:
                continue
            root = _repo_root_for(fp)
            # No enclosing repo (e.g. a loose file): match on its parent dir.
            dirs.add(os.path.realpath(root if root else str(Path(fp).parent)))
    return dirs


# ====================================================================
# Who mode: the live fleet, by repo
#
# Claude Code (2.1.2xx) writes `~/.claude/sessions/<pid>.json` for every running
# session — session id, launch cwd, tmux pane, busy/idle status, a derived or
# auto name, version — and the file lives exactly as long as the process. That
# is the process↔session linkage claude-ps once needed its own hook to record.
# `who` joins it to git (repo identity, worktree, branch) and to the transcript
# tail (topic, last prompt, files touched, model, waiting-on-a-question), then
# groups by repo so two sessions on one checkout sit next to each other. From
# inside a session, `--me` resolves the caller from its environment.
# ====================================================================

_WHO_TAIL_BYTES = 256 * 1024
_WHO_TOUCHED_MAX = 3
_WHO_ANCESTRY_LIMIT = 12


def _sessions_registry_dir() -> Path:
    """Claude Code's per-process registry (override with CLAUDE_LS_SESSIONS_DIR)."""
    override = os.environ.get("CLAUDE_LS_SESSIONS_DIR")
    return Path(override) if override else Path.home() / ".claude" / "sessions"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


def _proc_starts(pids: List[int]) -> Dict[int, str]:
    """{pid: start time} in `ps -o lstart` form, the form the registry's
    `procStart` uses. {} when ps is unavailable — callers then trust the pid."""
    if not pids:
        return {}
    try:
        out = subprocess.run(
            ["ps", "-o", "pid=,lstart=", "-p", ",".join(str(p) for p in pids)],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return {}
    starts: Dict[int, str] = {}
    for ln in out.stdout.splitlines():
        parts = ln.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            starts[int(parts[0])] = parts[1].strip()
    return starts


_LSTART_FMT = "%a %b %d %H:%M:%S %Y"


def _proc_start_matches(recorded: str, actual_local: str) -> Optional[bool]:
    """Whether the registry's `procStart` names the same process start as
    `ps -o lstart`. None when either side does not parse.

    Both use lstart's layout, but the registry writes the instant in UTC and ps
    prints local time: one process read "Wed Sep  2 21:16:00 2026" in the file
    and "Wed Sep  2 14:16:00 2026" from ps on a PDT machine (2026-09-04). A
    string compare rejects every live session; compare instants instead.
    """
    try:
        rec = datetime.strptime(" ".join(recorded.split()), _LSTART_FMT)
        act = datetime.strptime(" ".join(actual_local.split()), _LSTART_FMT)
    except (ValueError, TypeError):
        return None
    rec_utc = rec.replace(tzinfo=timezone.utc)
    act_utc = act.astimezone()  # naive is read as local time
    return abs((rec_utc - act_utc).total_seconds()) <= 2


def _registry_entries(
    registry_dir: Path, table=None, starts=None, include_parked: bool = False
) -> List[Dict]:
    """Registry records whose process is still the Claude that wrote them.

    A record outlives its session when Claude is killed or the machine reboots,
    and the pid it names can be reused by anything, so liveness alone would
    report a text editor as a session. Two checks: the pid must currently run a
    `claude` executable, and when the record carries `procStart` it must agree
    with the live process's start time. Malformed files are skipped; the
    registry is another program's output.
    """
    if not registry_dir.is_dir():
        return []
    rows = _process_table() if table is None else table
    claude_pids = {pid for pid, _ppid, argv in rows if _is_claude_argv(argv)}
    candidates: List[Dict] = []
    for path in sorted(registry_dir.glob("*.json")):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        pid, sid = rec.get("pid"), rec.get("sessionId")
        if not isinstance(pid, int) or not isinstance(sid, str) or not sid:
            continue
        # Sent to the background: the process lingers with its last status, but
        # the session now runs as the job named here, which has its own record.
        if rec.get("parkedJobId") and not include_parked:
            continue
        alive = pid in claude_pids if rows else _pid_alive(pid)
        if alive:
            candidates.append(rec)
    live_starts = (_proc_starts if starts is None else starts)([r["pid"] for r in candidates])
    out: List[Dict] = []
    for rec in candidates:
        recorded, actual = rec.get("procStart"), live_starts.get(rec["pid"])
        if recorded and actual and _proc_start_matches(recorded, actual) is False:
            continue  # pid reused since the record was written
        out.append(rec)
    return out


def _agents_session_ids() -> Optional[Set[str]]:
    """Session ids `claude agents --json` reports running, or None when it
    can't answer: no `claude` on PATH, an error, unparseable output, or a
    registry override (the CLI only reads the default registry)."""
    if os.environ.get("CLAUDE_LS_SESSIONS_DIR"):
        return None
    try:
        out = subprocess.run(
            ["claude", "agents", "--json"], capture_output=True, text=True, timeout=10
        )
        rows = json.loads(out.stdout) if out.returncode == 0 else None
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError, ValueError):
        return None
    if not isinstance(rows, list):
        return None
    if not all(isinstance(r, dict) and r.get("sessionId") for r in rows):
        return None  # a shape this code doesn't know; don't filter by it
    return {r["sessionId"] for r in rows}


# One `claude agents` answer per process: a resume consults the running set
# up to three times, and each call is a CLI startup.
_AGENTS_ANSWER: Dict[str, Optional[Set[str]]] = {}


def _live_registry(table=None) -> List[Dict]:
    """Registry records for the sessions running now.

    Membership comes from `claude agents --json` when it answers, since Claude
    Code owns the rules for which records count; the files add what it omits
    (tmux pane, procStart, status timestamps). Without it, `_registry_entries`'
    own checks stand in.
    """
    entries = _registry_entries(_sessions_registry_dir(), table)
    if "ids" not in _AGENTS_ANSWER:
        _AGENTS_ANSWER["ids"] = _agents_session_ids()
    running = _AGENTS_ANSWER["ids"]
    # "Nothing is running" beside live registry records is a disagreement, and
    # the resume guard must not resolve one by seeing no sessions at all.
    if running is not None and (running or not entries):
        entries = [r for r in entries if r["sessionId"] in running]
    return _with_live_tmux(entries, table)


# One pane listing per process, for the same reason as `_AGENTS_ANSWER`.
_TMUX_ADDRESSES: Dict[str, Optional[Dict[str, Tuple[str, int]]]] = {}


def _tmux_pane_addresses() -> Optional[Dict[str, Tuple[str, int]]]:
    """{pane id: ("session:@window.%pane", pane pid)} for every pane on the
    tmux server this process reaches, or None when tmux can't answer."""
    if "panes" not in _TMUX_ADDRESSES:
        rows = _tmux_list_panes("#{pane_id}\t#{session_name}:#{window_id}.#{pane_id}\t#{pane_pid}")
        panes = None
        if rows is not None:
            panes = {r[0]: (r[1], int(r[2])) for r in rows if len(r) == 3 and r[2].isdigit()}
        _TMUX_ADDRESSES["panes"] = panes
    return _TMUX_ADDRESSES["panes"]


def _with_live_tmux(entries: List[Dict], table=None) -> List[Dict]:
    """Records with `tmux` rewritten to where the pane is now.

    Claude Code writes the address once, at launch, so renaming the tmux
    session leaves it naming one that no longer exists, and resume told the
    user to attach there (2026-10-22). The pane id survives a rename, so look
    it up. Pane ids are numbered per tmux server, though, and a Claude on
    another socket can share one with an unrelated pane here: a pane counts
    only when the Claude descends from its shell. Anything else keeps the
    recorded address, which is at least on the right server.
    """
    if not any(r.get("tmux") for r in entries):
        return entries
    panes = _tmux_pane_addresses()
    if not panes:
        return entries
    rows = _process_table() if table is None else table
    parents = {pid: ppid for pid, ppid, _argv in rows}
    out = []
    for rec in entries:
        hit = panes.get((rec.get("tmux") or "").rpartition(".")[2])
        if hit:
            address, pane_pid = hit
            cur = rec["pid"]
            for _ in range(_WHO_ANCESTRY_LIMIT):
                if cur == pane_pid:
                    rec = {**rec, "tmux": address}
                    break
                cur = parents.get(cur, 0)
                if cur <= 1:
                    break
        out.append(rec)
    return out


def _continued_in(session_file) -> Optional[str]:
    """The session id this transcript was continued in when it was sent to the
    background, or None. Only a pointer with no conversation after it counts:
    resuming the old id in the foreground later makes it current again."""
    target = None
    for rec in _tail_entries(Path(session_file), 64 * 1024):
        kind = rec.get("type")
        if kind == "continued-in" and rec.get("continuedInSessionId"):
            target = rec["continuedInSessionId"]
        elif kind in ("user", "assistant"):
            target = None
    return target


def _me_session_id(
    registry: List[Dict], env: Optional[Dict[str, str]] = None, table=None
) -> Optional[str]:
    """The session this process runs inside, or None outside any session.

    Claude Code exports CLAUDE_CODE_SESSION_ID and CLAUDE_PID into the shells it
    spawns, so a Bash tool call knows itself directly. A hook or a shell spawned
    some other way may lack them; then the process ancestry is walked until a
    registered pid appears, which is how `claude-ps` finds the caller's own
    session that `pgrep -f claude` omits.
    """
    env = os.environ if env is None else env
    by_pid = {r["pid"]: r["sessionId"] for r in registry}
    ids = set(by_pid.values())
    sid = env.get("CLAUDE_CODE_SESSION_ID", "")
    if sid and (sid in ids or not registry):
        return sid
    pid_s = env.get("CLAUDE_PID", "")
    if pid_s.isdigit() and int(pid_s) in by_pid:
        return by_pid[int(pid_s)]
    rows = _process_table() if table is None else table
    parents = {pid: ppid for pid, ppid, _argv in rows}
    cur = os.getpid()
    for _ in range(_WHO_ANCESTRY_LIMIT):
        if cur in by_pid:
            return by_pid[cur]
        cur = parents.get(cur, 0)
        if cur <= 1:
            break
    return sid or None


def _git_facts(cwd: str) -> Tuple[str, str]:
    """(toplevel, branch) for a checkout; ('', '') outside git or when git fails.
    Branch is `HEAD` when detached, which is what a reader wants to see."""
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return "", ""
    if out.returncode != 0:
        return "", ""
    lines = [ln.strip() for ln in out.stdout.splitlines()]
    return (lines[0] if lines else ""), (lines[1] if len(lines) > 1 else "")


@dataclass
class _TailFacts:
    model: str = ""
    last_prompt: str = ""
    touched: List[str] = field(default_factory=list)  # distinct, most recent last
    waiting: bool = False
    last_ts: str = ""


def _tail_entries(path: Path, nbytes: int = _WHO_TAIL_BYTES) -> List[Dict]:
    """The transcript's last `nbytes` as records; a partial first line is dropped."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - nbytes))
            data = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = data.splitlines()
    if size > nbytes:
        lines = lines[1:]
    out: List[Dict] = []
    for ln in lines:
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _tail_facts(path: Path) -> _TailFacts:
    """What the session is doing now, from its tail: the model actually answering
    (a session can change model mid-way, so the head is the wrong place to look),
    the last thing the human typed, the files most recently edited, and whether
    the last assistant message reads as a question left for the human."""
    tf = _TailFacts()
    last_role = ""
    last_assistant_text = ""
    for rec in _tail_entries(path):
        ts = rec.get("timestamp")
        if isinstance(ts, str) and ts:
            tf.last_ts = ts
        kind = rec.get("type")
        msg = rec.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if kind == "assistant":
            model = msg.get("model", "")
            if model and not model.startswith("<"):  # "<synthetic>": a harness-made record
                tf.model = model
            if not isinstance(content, list):
                continue
            texts: List[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use" and block.get("name", "") in _EDIT_TOOLS:
                    tin = block.get("input")
                    fp = (
                        (tin.get("file_path") or tin.get("notebook_path"))
                        if isinstance(tin, dict)
                        else ""
                    )
                    if fp:
                        if fp in tf.touched:
                            tf.touched.remove(fp)
                        tf.touched.append(fp)
                elif block.get("type") == "text" and block.get("text", "").strip():
                    texts.append(block["text"].strip())
            if texts:
                last_assistant_text = " ".join(texts)
                last_role = "assistant"
        elif kind == "user" and not (
            rec.get("isMeta") or rec.get("isSidechain") or rec.get("isCompactSummary")
        ):
            text = _extract_text(content) if isinstance(content, (str, list)) else ""
            if isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content
            ):
                continue
            text = text.strip()
            if text and not is_wrapper_text(text):
                tf.last_prompt = text.replace("\n", " ")[:80]
                last_role = "user"
    tf.waiting = last_role == "assistant" and looks_like_waiting(last_assistant_text)
    return tf


@dataclass
class WhoRow:
    pid: int
    session_id: str
    name: str
    status: str
    status_age_min: Optional[int]
    tmux: str
    pane: str
    cwd: str  # where `claude` was launched
    repo: str  # canonical main repo, shared by all its worktrees
    toplevel: str  # the checkout the edits land in (a worktree, or the launch checkout)
    worktree: str
    branch: str
    topic: str
    model: str
    last_prompt: str
    touched: List[str]
    waiting: bool
    last_active_min: Optional[int]
    version: str
    me: bool = False
    shares_checkout_with: List[str] = field(default_factory=list)
    origin: str = ""  # the root session this one was /cleared from, if any
    chain_len: int = 1  # transcripts on disk in this pane's lineage, itself included


def _who_transcript(base_dir: str, cwd: str, session_id: str) -> Optional[Path]:
    direct = Path(base_dir) / encode_path_to_folder_name(cwd) / f"{session_id}.jsonl"
    if direct.is_file():
        return direct
    hits = [p for p in matching_sessions(base_dir, session_id, include_codex=False)]
    return hits[0] if len(hits) == 1 else None


def _who_working_checkout(touched: List[str]) -> str:
    """The checkout the session's most recent edit landed in, or '' if none did.

    The registry's cwd is where `claude` was launched, and the common shape here
    is launch from the main checkout, then work in a worktree — so the launch
    dir says "main" for a session that has not touched main in hours. Where the
    edits land is the stream."""
    for path in reversed(touched):
        root = _repo_root_for(path)
        if root:
            return root
    return ""


def _who_row(rec: Dict, base_dir: str, now_ms: float) -> WhoRow:
    cwd = rec.get("cwd") or ""
    tmux = rec.get("tmux") or ""
    pane = tmux.split(":", 1)[0] if tmux else ""
    status_at = rec.get("statusUpdatedAt")
    status_age = int((now_ms - status_at) / 60000) if isinstance(status_at, (int, float)) else None

    # Claude Code names a session from its content ("agentic-ops-control-loop",
    # nameSource "auto"); a "derived" name is the pane name and says nothing. The
    # slug is a random phrase ("witty-forging-walrus"), so it is the last resort.
    name = rec.get("name") or ""
    topic = name if rec.get("nameSource") == "auto" else ""
    model, last_prompt, touched, waiting, last_active = "", "", [], False, None
    origin, chain_len = "", 1
    transcript = _who_transcript(base_dir, cwd, rec["sessionId"]) if cwd else None
    if transcript:
        origin = session_origin(transcript) or ""
        chain_len = len(session_chain(transcript))
        if not topic:
            slug, first_user, _turns = get_session_preview(transcript, topic_only=True)
            topic = first_user or slug
        tf = _tail_facts(transcript)
        model, last_prompt, waiting = tf.model, tf.last_prompt, tf.waiting
        touched = tf.touched[-_WHO_TOUCHED_MAX:]
        secs = _seconds_since_write(transcript)
        last_active = int(secs / 60) if secs is not None else None

    launch_top, branch = _git_facts(cwd) if cwd else ("", "")
    toplevel = _who_working_checkout(touched) or launch_top
    if toplevel and toplevel != launch_top:
        branch = _git_facts(toplevel)[1]
    repo = (_canonical_repo_root(toplevel or cwd) if (toplevel or cwd) else None) or toplevel or cwd
    if toplevel and os.path.realpath(toplevel) != os.path.realpath(repo):
        worktree = os.path.basename(toplevel.rstrip(os.sep))
    else:
        worktree = "main" if toplevel else ""

    return WhoRow(
        pid=rec["pid"],
        session_id=rec["sessionId"],
        name=name,
        status=rec.get("status") or "",
        status_age_min=status_age,
        tmux=tmux,
        pane=pane,
        cwd=cwd,
        repo=repo,
        toplevel=toplevel,
        worktree=worktree,
        branch=branch,
        topic=topic or name,
        model=model,
        last_prompt=last_prompt,
        touched=touched,
        waiting=waiting,
        last_active_min=last_active,
        version=rec.get("version") or "",
        origin=origin,
        chain_len=chain_len,
    )


def _who_mark_shared_checkouts(rows: List[WhoRow]) -> None:
    """Two live sessions whose edits land in one checkout. Common and fine
    (launch from main, spawn a worktree), so it is information, not an alarm:
    it is the case where a question meant for one stream reaches the other."""
    by_top: Dict[str, List[WhoRow]] = defaultdict(list)
    for r in rows:
        if r.toplevel:
            by_top[os.path.realpath(r.toplevel)].append(r)
    for group in by_top.values():
        if len(group) < 2:
            continue
        for r in group:
            r.shares_checkout_with = [
                o.pane or o.name or o.session_id[:8] for o in group if o is not r
            ]


def _who_rows(args, registry: List[Dict], me: Optional[str]) -> List[WhoRow]:
    now_ms = time.time() * 1000
    rows = [_who_row(rec, args.base_dir, now_ms) for rec in registry]
    for r in rows:
        r.me = me is not None and r.session_id == me
    _who_mark_shared_checkouts(rows)

    if getattr(args, "dir", None):
        start = os.path.abspath(os.path.expanduser(args.dir))
        want = _canonical_repo_root(start) or _repo_root_for(start) or start
        want_real = os.path.realpath(want)
        rows = [r for r in rows if os.path.realpath(r.repo) == want_real]
    if getattr(args, "me", False):
        mine = [r for r in rows if r.me]
        if mine:
            my_repo = os.path.realpath(mine[0].repo)
            rows = [r for r in rows if os.path.realpath(r.repo) == my_repo]
    within = getattr(args, "within", 0) or 0
    if within:
        rows = [r for r in rows if r.last_active_min is not None and r.last_active_min <= within]

    def recency(r: WhoRow) -> float:
        return r.last_active_min if r.last_active_min is not None else float("inf")

    newest_by_repo: Dict[str, float] = {}
    for r in rows:
        newest_by_repo[r.repo] = min(newest_by_repo.get(r.repo, float("inf")), recency(r))
    rows.sort(key=lambda r: (newest_by_repo[r.repo], r.repo, recency(r), r.pid))
    return rows


def _who_status_text(r: WhoRow) -> str:
    text = r.status or "?"
    if r.status_age_min is not None and r.status != "busy":
        text += f" {format_staleness(r.status_age_min)}"
    if r.waiting:
        text += " ?"
    return text


def _who_short_model(model: str) -> str:
    return model[len("claude-") :] if model.startswith("claude-") else model


def _who_touched_text(r: WhoRow) -> str:
    base = r.toplevel or r.cwd
    out = []
    for p in r.touched:
        if base and p.startswith(base.rstrip(os.sep) + os.sep):
            out.append(os.path.relpath(p, base))
        else:
            out.append(_tilde(p))
    return " ".join(out)


def _who_lineage_text(r: WhoRow) -> str:
    n = f"{r.chain_len} sessions in this pane"
    return f"cleared from {r.origin[:8]} · {n}" if r.origin else n


def _who_as_of(now: Optional[datetime] = None) -> str:
    # Agents run `who` and read the result back from their context hours later as
    # though it were live. The stamp travels with the text, so a stale view says so.
    now = (now or datetime.now()).astimezone()
    return f"as of {now:%Y-%m-%d %H:%M %Z}; a snapshot, re-run `claude-ls who` for the current view"


def who_mode(args, console: Optional[Console]):
    """Live sessions grouped by repo, with the identity facts an operator (or a
    session asked about another session's work) needs to tell streams apart."""
    registry = _live_registry()
    me = _me_session_id(registry)
    if getattr(args, "me", False) and not any(r["sessionId"] == me for r in registry):
        msg = "Not inside a registered Claude session (no CLAUDE_CODE_SESSION_ID, no registered ancestor)."
        print(msg, file=sys.stderr)
        sys.exit(1)
    rows = _who_rows(args, registry, me)

    if args.json:
        print(json.dumps([asdict(r) for r in rows], indent=2))
        return

    print(_who_as_of())
    if not rows:
        print("No live Claude sessions." if not registry else "No live Claude sessions match.")
        return

    groups: List[Tuple[str, List[WhoRow]]] = []
    for r in rows:
        if groups and groups[-1][0] == r.repo:
            groups[-1][1].append(r)
        else:
            groups.append((r.repo, [r]))

    shared_any = any(r.shares_checkout_with for r in rows)
    waiting_any = any(r.waiting for r in rows)
    if console and not args.no_color:
        for repo, members in groups:
            label = _tilde(repo) if repo else "(no repo)"
            console.print(f"[bold]{label}[/bold]  [dim]{len(members)} live[/dim]")
            table = Table(show_header=True, header_style="bold blue", box=box.SIMPLE, expand=True)
            table.add_column("", width=1)
            table.add_column("pane", no_wrap=True)
            table.add_column("id", width=8, style="cyan")
            table.add_column("status", no_wrap=True)
            table.add_column("last", style="dim", no_wrap=True, justify="right")
            table.add_column("checkout @ branch", no_wrap=True)
            table.add_column("model", style="dim", no_wrap=True)
            table.add_column("topic / last prompt / touching", ratio=1)
            for r in members:
                checkout = (
                    f"{'! ' if r.shares_checkout_with else ''}{r.worktree} @ {r.branch}".strip()
                )
                last = format_staleness(r.last_active_min) if r.last_active_min is not None else "-"
                detail = Text(r.topic or "(no topic)")
                if r.last_prompt:
                    detail.append(f"\n↳ {r.last_prompt}", style="dim")
                touched = _who_touched_text(r)
                if touched:
                    detail.append(f"\n↳ touching {touched}", style="dim")
                if r.origin or r.chain_len > 1:
                    detail.append(f"\n↳ {_who_lineage_text(r)}", style="dim")
                table.add_row(
                    "*" if r.me else "",
                    r.pane or "-",
                    r.session_id[:8],
                    _who_status_text(r),
                    last,
                    checkout,
                    _who_short_model(r.model) or "-",
                    detail,
                )
            console.print(table)
        notes = []
        if me and any(r.me for r in rows):
            notes.append("* this session")
        if shared_any:
            notes.append("! edits land in a checkout another live session also edits")
        if waiting_any:
            notes.append("? last message looks like a question for you")
        if notes:
            console.print("[dim]" + "   ".join(notes) + "[/dim]")
    else:
        for repo, members in groups:
            print(f"{_tilde(repo) if repo else '(no repo)'}  ({len(members)} live)")
            for r in members:
                marks = ("*" if r.me else " ") + ("!" if r.shares_checkout_with else " ")
                last = format_staleness(r.last_active_min) if r.last_active_min is not None else "-"
                print(
                    f"{marks} {r.pane or '-':22s} {r.session_id[:8]}  {_who_status_text(r):12s} "
                    f"{last:>6s}  {r.worktree} @ {r.branch}  {_who_short_model(r.model) or '-'}"
                )
                print(f"     {r.topic or '(no topic)'}")
                if r.last_prompt:
                    print(f"     ↳ {r.last_prompt}")
                touched = _who_touched_text(r)
                if touched:
                    print(f"     ↳ touching {touched}")
                if r.origin or r.chain_len > 1:
                    print(f"     ↳ {_who_lineage_text(r)}")
        notes = []
        if me and any(r.me for r in rows):
            notes.append("* this session")
        if shared_any:
            notes.append("! edits land in a checkout another live session also edits")
        if waiting_any:
            notes.append("? last message looks like a question for you")
        if notes:
            print("   ".join(notes))


# 256-color, not the 16 theme slots: themes remap those freely (one in use
# here draws "red" as teal), and a refusal has to read as red.
_ANSI = {"red": "38;5;196", "yellow": "38;5;214", "white": "1;38;5;231"}


def _alert(color: str, message: str, console: Optional[Console]) -> None:
    """Print `⚠ message` to stderr, the glyph in `color` and the message bold white.
    Raw ANSI rather than rich, which would parse `[...]` in paths as markup and
    re-wrap the line."""
    if console is None or os.environ.get("NO_COLOR") or not sys.stderr.isatty():
        print(f"⚠ {message}", file=sys.stderr)
        return
    print(f"\033[{_ANSI[color]}m⚠\033[0m \033[{_ANSI['white']}m{message}\033[0m", file=sys.stderr)


def resume_mode(args, console: Optional[Console]):
    """Resolve a (partial) session id, cd into its anchoring dir, and hand off
    to `claude -r`. Guards against double-driving a session already live in a
    matching tmux session. Falls back to the main repo when an anchoring
    worktree has been removed."""
    ident = getattr(args, "session_id", None) or getattr(args, "session", None)
    if not ident:
        print("Provide a session id (positional or -s/--session-id).", file=sys.stderr)
        sys.exit(1)

    matches = matching_sessions(args.base_dir, ident)
    pick = getattr(args, "pick", None)
    if not matches:
        print(f"No session matches '{ident}'.", file=sys.stderr)
        sys.exit(1)
    if len(matches) == 1:
        session_file = matches[0]
    elif pick is not None:
        if pick < 1 or pick > len(matches):
            print(f"--pick {pick} is out of range (1..{len(matches)}).", file=sys.stderr)
            print_session_candidates(ident, matches)
            sys.exit(1)
        session_file = matches[pick - 1]
    else:
        # Same id under several directories: the anchoring dir is the differentiator,
        # so list the candidates and let the user choose with --pick.
        print_session_candidates(ident, matches)
        print(
            f"\nRe-run with --pick <n> to choose, e.g. `claude-ls resume {ident} --pick 1`.",
            file=sys.stderr,
        )
        sys.exit(1)
    # Resolution finds rollouts so this can refuse them by name rather than by
    # reporting a session that plainly exists as missing. `claude -r` only speaks
    # Claude's store, and there is no Codex equivalent to hand off to.
    if _file_is_codex(session_file):
        print(
            f"{short_id_for_path(session_file)} is a Codex rollout — `claude -r` can't resume it.",
            file=sys.stderr,
        )
        print(
            f"  Read it instead: claude-ls summary {_tilde(str(session_file))}",
            file=sys.stderr,
        )
        sys.exit(1)

    full_id = session_file.stem

    # Anchoring dir: the cwd recorded in the session itself is authoritative;
    # fall back to reconstructing it from the project folder slug.
    entries = load_raw_entries(str(session_file))
    meta = extract_session_metadata(entries)
    reconstructed = os.path.expanduser(project_folder_to_cwd(session_file.parent.name))
    target_dir = None
    fallback_note = ""
    if meta.cwd and Path(meta.cwd).is_dir():
        target_dir = meta.cwd
    elif Path(reconstructed).is_dir():
        target_dir = reconstructed
    else:
        # Anchoring dir is gone — likely a merged-and-removed worktree. Follow it
        # back to the main repo checkout so the session can still be resumed.
        for src in (meta.cwd, reconstructed):
            main_repo = _worktree_main_repo(src)
            if main_repo:
                target_dir = main_repo
                fallback_note = (
                    f"original worktree is gone ({_tilde(src)}); "
                    f"resuming in the main repo {_tilde(main_repo)}"
                )
                break
    if not target_dir:
        print(
            f"Can't locate the session's directory "
            f"(recorded cwd: {meta.cwd or 'unknown'}) — it may have been moved or deleted.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Double-drive guard, strongest evidence first. Deliberately not name
    # matching: that fired on idle sessions who merely shared a project name, and
    # stayed silent for Claudes whose session name — or absence of tmux — didn't
    # look like the project. Both failure modes cost more than they saved.
    if not args.force:
        # Sent to the background: the conversation carries on under the job's
        # id, and resuming this one would fork it from where it was handed off.
        successor = _continued_in(session_file)
        if successor:
            running = any(r["sessionId"] == successor for r in _live_registry())
            state = "running" if running else "not running"
            _alert(
                "red",
                f"{full_id[:8]} was sent to the background: it continues as "
                f"{successor[:8]} ({state}).",
                console,
            )
            print(
                "  Resuming it would fork the conversation from before the hand-off.",
                file=sys.stderr,
            )
            print(
                f"  Or:      claude-ls resume {successor[:8]}   ·   "
                "re-run with --force (resume anyway).",
                file=sys.stderr,
            )
            sys.exit(2)

        # A /cleared session's pane lives on under a later id. Resuming the
        # earlier one would open its transcript in a second process beside it.
        live = _live_descendant(session_chain(session_file), full_id)
        if live:
            rec, member = live
            pane = rec.get("tmux") or ""
            where = f" its pane {pane}" if pane else " its pane"
            _alert(
                "red",
                f"{full_id[:8]} was /cleared:{where} now runs {member.session_id[:8]}, "
                "later in the same lineage.",
                console,
            )
            if args.attach and pane:
                tmux_cmd = (
                    ["tmux", "switch-client", "-t", pane]
                    if os.environ.get("TMUX")
                    else ["tmux", "attach", "-t", pane]
                )
                if args.dry_run:
                    print(" ".join(tmux_cmd))
                    return
                _exec(tmux_cmd)
            print(
                "  Resuming it would open the old transcript in a second process.", file=sys.stderr
            )
            if pane:
                print(f"  Attach:  tmux attach -t {pane}", file=sys.stderr)
            print(
                f"  Or:      claude-ls resume {member.session_id[:8]}   ·   "
                "re-run with --force (resume anyway).",
                file=sys.stderr,
            )
            sys.exit(2)

        table = _process_table()
        registry = _live_registry(table)
        driver = next((r for r in registry if r.get("sessionId") == full_id), None)
        if driver is not None:
            pane = driver.get("tmux") or ""
            if args.attach and pane:
                tmux_cmd = (
                    ["tmux", "switch-client", "-t", pane]
                    if os.environ.get("TMUX")
                    else ["tmux", "attach", "-t", pane]
                )
                if args.dry_run:
                    print(" ".join(tmux_cmd))
                    return
                _exec(tmux_cmd)
            where = f" in tmux {pane}" if pane else " outside tmux"
            _alert(
                "red",
                f"{full_id[:8]} is already live{where} (pid {driver['pid']}).",
                console,
            )
            print("  Refusing to resume to avoid double-driving it.", file=sys.stderr)
            if pane:
                print(f"  Attach:  tmux attach -t {pane}", file=sys.stderr)
                print(
                    "  Or:      re-run with --attach (jump to it) or --force (resume anyway).",
                    file=sys.stderr,
                )
            else:
                print("  Or:      re-run with --force (resume anyway).", file=sys.stderr)
            sys.exit(2)

        # The registry names the session every registered Claude drives, so one
        # in this directory that isn't on full_id is a neighbour, not a clash —
        # the cwd match below was a false positive for every such case. Only
        # Claudes the registry can't account for (versions predating it) still
        # fall through to the cwd heuristic.
        root = os.path.realpath(target_dir)
        neighbours = [
            r for r in registry if r.get("cwd") and _path_within(os.path.realpath(r["cwd"]), root)
        ]
        # argv records the launch session, which can change via /resume. Let
        # the registry override it, keeping argv as a fallback for older Claudes.
        # Every pid with a record, including the ones `_live_registry` drops
        # (parked, or not listed by `claude agents`): those are accounted for,
        # and the cwd heuristic below is only for Claudes that have none.
        registered_pids = {
            r["pid"]
            for r in _registry_entries(_sessions_registry_dir(), table, include_parked=True)
        }
        unregistered = [row for row in table if row[0] not in registered_pids]
        driver_pid = _claude_driving_session(full_id, unregistered)
        live_sessions = _claude_tmux_sessions(target_dir, table, ignore_pids=registered_pids)

        if args.attach and live_sessions:
            if len(live_sessions) == 1:
                tname = live_sessions[0]
                tmux_cmd = (
                    ["tmux", "switch-client", "-t", tname]
                    if os.environ.get("TMUX")
                    else ["tmux", "attach", "-t", tname]
                )
                if args.dry_run:
                    print(" ".join(tmux_cmd))
                    return
                _exec(tmux_cmd)
            # Several real candidates: picking one for the user is how the old
            # code sent people to the wrong session.
            _alert(
                "red",
                f"{len(live_sessions)} tmux sessions are running a Claude in "
                f"{_tilde(target_dir)}: {', '.join(live_sessions)}",
                console,
            )
            print(
                "  --attach needs a single target — attach explicitly with "
                "`tmux attach -t <name>`.",
                file=sys.stderr,
            )
            sys.exit(2)

        if driver_pid is not None or live_sessions:
            if driver_pid is not None:
                _alert(
                    "red",
                    f"this exact session is already being driven: "
                    f"`claude -r {full_id[:8]}` is live as pid {driver_pid}.",
                    console,
                )
            if live_sessions:
                _alert(
                    "red",
                    f"a Claude is running in {_tilde(target_dir)} "
                    f"(tmux: {', '.join(live_sessions)}).",
                    console,
                )
            print("  Refusing to resume to avoid double-driving it.", file=sys.stderr)
            # Surface where a resume *would* land, so the warning is actionable
            # without re-running under --dry-run.
            print(f"  Would resume in: {_tilde(target_dir)}", file=sys.stderr)
            if fallback_note:
                print(f"  Note: {fallback_note}", file=sys.stderr)
            if live_sessions:
                extra = (
                    f"   (also: {', '.join(live_sessions[1:])})" if len(live_sessions) > 1 else ""
                )
                print(f"  Attach:  tmux attach -t {live_sessions[0]}{extra}", file=sys.stderr)
            print(
                "  Or:      re-run with --attach (jump to tmux) or --force (resume anyway).",
                file=sys.stderr,
            )
            sys.exit(2)

        if neighbours:
            names = ", ".join(r.get("name") or str(r["pid"]) for r in neighbours)
            print(
                f"· {len(neighbours)} other Claude(s) in {_tilde(target_dir)} "
                f"on different sessions: {names}",
                file=sys.stderr,
            )

        # Weak signal, so it cautions rather than refuses: nothing observable is
        # driving the session, but its transcript was written moments ago. A file
        # is equally hot right after you quit a session you now want back, and
        # blocking that would be wrong far more often than it would be right.
        idle = _seconds_since_write(session_file)
        if idle is not None and idle < _LIVE_TRANSCRIPT_WINDOW_S:
            _alert(
                "yellow",
                f"transcript was written {int(idle)}s ago — if a Claude is driving this "
                "session outside tmux, resuming will double-drive it.",
                console,
            )

    claude_cmd = ["claude", "-r", full_id]
    if fallback_note:
        _alert("yellow", fallback_note, console)
    if args.dry_run:
        print(f"cd {target_dir} && {' '.join(claude_cmd)}")
        return
    print(f"Resuming {full_id[:8]} in {_tilde(target_dir)}", file=sys.stderr)
    try:
        os.chdir(target_dir)
    except OSError as e:
        print(f"Could not cd into {target_dir}: {e}", file=sys.stderr)
        sys.exit(1)
    _exec(claude_cmd)


def _exec(cmd: List[str]) -> None:
    """Replace this process with cmd; clean error if the binary isn't found."""
    try:
        os.execvp(cmd[0], cmd)
    except FileNotFoundError:
        print(f"`{cmd[0]}` not found on PATH.", file=sys.stderr)
        sys.exit(127)


def resolve_mode(args, console: Optional[Console]):
    """Resolve a Claude project folder name or ambiguous path to real FS path(s)."""
    raw = args.input
    home = str(Path.home())

    # Session-ID short-circuit: if it looks like a UUID prefix, find its project
    sess_input = raw.strip()
    if re.fullmatch(r"[0-9a-fA-F-]{6,}", sess_input):
        sess_path = find_session_across_projects(args.base_dir, sess_input)
        if sess_path and _file_is_codex(sess_path):
            # A rollout's parent directory is the day it ran, not a project slug.
            # Its project is the cwd in `session_meta` — encoded to the same slug
            # the listing groups it under, so both agree on one identity.
            cwd = session_recorded_cwd(sess_path)
            if not cwd:
                print(
                    f"{sess_input} is a Codex rollout with no recorded working directory.",
                    file=sys.stderr,
                )
                sys.exit(1)
            folder_name = encode_path_to_folder_name(cwd)
        elif sess_path:
            folder_name = sess_path.parent.name
        else:
            print(f"No session found matching: {sess_input}", file=sys.stderr)
            sys.exit(1)
    else:
        folder_name = _normalize_to_folder_name(raw)

    resolved = resolve_project_folder(folder_name)

    def shorten(p: Path) -> str:
        s = str(p)
        return "~" + s[len(home) :] if s.startswith(home) else s

    if args.no_color or console is None:
        print(f"input:  {raw}")
        print(f"folder: {folder_name}")
        if not resolved:
            print("resolved: (no filesystem match)")
            print(f"naive:  {_naive_cwd(folder_name)}")
            sys.exit(1)
        if len(resolved) == 1:
            print(f"resolved: {shorten(resolved[0])}")
        else:
            print("resolved:")
            for p in resolved:
                print(f"  {shorten(p)}")
    else:
        console.print(Text.assemble(("input:  ", "dim"), (raw, "white")))
        console.print(Text.assemble(("folder: ", "dim"), (folder_name, "cyan")))
        if not resolved:
            console.print(Text.assemble(("resolved: ", "dim"), ("(no filesystem match)", "red")))
            console.print(
                Text.assemble(("naive:  ", "dim"), (_naive_cwd(folder_name), "dim yellow"))
            )
            sys.exit(1)
        if len(resolved) == 1:
            console.print(Text.assemble(("resolved: ", "dim"), (shorten(resolved[0]), "green")))
        else:
            console.print(Text("resolved:", style="dim"))
            for p in resolved:
                console.print(Text.assemble(("  ", "dim"), (shorten(p), "green")))


def summary_mode(args, console: Optional[Console]):
    """Quick summary of a session: first/last message, turn count, duration."""
    base_dir = args.base_dir

    # Resolve session: positional session_id or auto-detect
    session_file = None
    if hasattr(args, "session_id") and args.session_id:
        # An explicit path on disk (e.g. a Codex rollout outside ~/.claude) wins over
        # a session-id lookup, so cross-store files are usable by path.
        as_path = Path(args.session_id)
        if as_path.exists() and as_path.is_file():
            session_file = as_path
        else:
            session_file = find_session_across_projects(base_dir, args.session_id)
        if not session_file:
            print(f"No session found matching: {args.session_id}", file=sys.stderr)
            sys.exit(1)
    elif hasattr(args, "file") and args.file:
        session_file = Path(args.file)
    else:
        project = find_project_from_cwd(base_dir)
        if project:
            sessions = order_session_paths(list_sessions(project), limit=1)
            if sessions:
                session_file = sessions[0]
        if not session_file:
            print(
                "No session found. Provide a session ID or run from a project directory.",
                file=sys.stderr,
            )
            sys.exit(1)

    # Load entries for metadata
    entries = load_raw_entries(str(session_file))
    if not entries:
        print("Empty session file", file=sys.stderr)
        sys.exit(1)

    metadata = extract_session_metadata(entries)
    work = derive_work_signals(entries)
    messages = load_messages_or_exit(str(session_file), "all")

    # Extract key info. Summary is a summarizing view, so it counts and quotes
    # only prose the human typed — the same test the listing and `activity` use.
    # `load_messages` deliberately keeps the wrapper messages for the fidelity
    # views; the filter belongs here.
    #
    # Not quite identical to the listing: `load_messages` also drops
    # continuation-summary messages, which `get_session_preview` counts, so a
    # resumed session can differ by one turn. Pre-existing, 3 sessions in 743.
    user_msgs = [m for m in messages if m.role == "user" and not is_wrapper_text(m.text_content)]
    assistant_msgs = [m for m in messages if m.role == "assistant"]
    first_user = first_answered_prompt(entries).replace("\n", " ")[:120] or "(none)"
    last_user = user_msgs[-1].text_content.replace("\n", " ")[:120] if user_msgs else "(none)"
    last_assistant = assistant_msgs[-1].preview if assistant_msgs else "(none)"

    # The transcript records the real cwd; the project folder name is that path
    # flattened, which cannot tell `projects-new/my-config` from
    # `projects/new/claude/general`. Decode the folder only when no cwd was
    # recorded (Codex rollouts sit in a date dir, so their fallback is "codex").
    is_codex = _file_is_codex(session_file)
    if metadata.cwd:
        proj_label = _tilde(metadata.cwd)
    else:
        proj_label = "codex" if is_codex else format_project_identifier(session_file.parent.name)

    model_line = metadata.model
    if metadata.model_first and metadata.model_first != metadata.model:
        model_line += f" (started as {metadata.model_first})"
    compactions = ""
    if metadata.compaction_count:
        kinds = Counter(e.get("trigger", "unknown") for e in metadata.compaction_events)
        detail = ", ".join(f"{k} ×{n}" if n > 1 else k for k, n in kinds.items())
        compactions = f"{metadata.compaction_count} ({detail})"
    lineage = ""
    if not is_codex:
        origin = metadata.origin_session_id or session_origin(session_file)
        lineage = format_lineage(session_chain(session_file), session_file.stem, origin)

    # Start → last-active in local time; the human's day, not the store's UTC.
    when = ""
    if metadata.start_time:
        lo = metadata.start_time.astimezone()
        when = f"{lo:%Y-%m-%d %H:%M}"
        if metadata.end_time:
            hi = metadata.end_time.astimezone()
            when += f" → {hi:%H:%M}" if hi.date() == lo.date() else f" → {hi:%m-%d %H:%M}"

    # resume/claude-graph are Claude-only; a rollout stem has no usable short id and
    # neither destination handles Codex, so suppress the footer rather than advertise
    # commands that won't work.
    if is_codex:
        followups = ""
    else:
        short_id = session_file.stem.split("-")[0]
        followups = (
            f"resume: claude-ls resume {short_id}   ·   related work: claude-graph {short_id}"
        )

    if console and not args.no_color:
        console.print(f"[bold]Session:[/bold] {session_file.stem}")
        console.print(f"[bold]Project:[/bold] [cyan]{proj_label}[/cyan]")
        if metadata.slug:
            console.print(f"[bold]Slug:[/bold] {metadata.slug}")
        if when:
            console.print(f"[bold]When:[/bold] [cyan]{when}[/cyan]")
        console.print(f"[bold]Duration:[/bold] {metadata.duration_str}")
        console.print(f"[bold]Turns:[/bold] {len(user_msgs)} user, {len(assistant_msgs)} assistant")
        if metadata.model:
            console.print(f"[bold]Model:[/bold] {model_line}")
        if metadata.total_input_tokens or metadata.total_output_tokens:
            console.print(
                f"[bold]Tokens:[/bold] {metadata.total_input_tokens:,} in / {metadata.total_output_tokens:,} out"
            )
        if compactions:
            console.print(f"[bold]Compactions:[/bold] {compactions}")
        if lineage:
            console.print(f"[bold]Lineage:[/bold] {lineage}")
        for line in format_work_signals(work):
            if "↳" in line:
                console.print(f"[yellow]{line}[/yellow]")
            else:
                label, _, rest = line.partition(":")
                console.print(f"[bold]{label}:[/bold]{rest}")
        console.print()
        console.print(f"[bold]First message:[/bold] {first_user}")
        if len(user_msgs) > 1:
            console.print(f"[bold]Last message:[/bold]  {last_user}")
        console.print(f"[bold]Last response:[/bold] {last_assistant}")
        if followups:
            console.print()
            console.print(f"[dim]{followups}[/dim]")
    else:
        print(f"Session:  {session_file.stem}")
        print(f"Project:  {proj_label}")
        if metadata.slug:
            print(f"Slug:     {metadata.slug}")
        if when:
            print(f"When:     {when}")
        print(f"Duration: {metadata.duration_str}")
        print(f"Turns:    {len(user_msgs)} user, {len(assistant_msgs)} assistant")
        if metadata.model:
            print(f"Model:    {model_line}")
        if compactions:
            print(f"Compactions: {compactions}")
        if lineage:
            print(f"Lineage:  {lineage}")
        for line in format_work_signals(work):
            print(line)
        print()
        print(f"First message: {first_user}")
        if len(user_msgs) > 1:
            print(f"Last message:  {last_user}")
        print(f"Last response: {last_assistant}")
        if followups:
            print()
            print(followups)


# ====================================================================
# Activity mode (week-in-review across sessions + repos)
# ====================================================================


@dataclass
class ActivityWindow:
    start: datetime  # tz-aware UTC
    end: datetime
    label: str


@dataclass
class SessionActivity:
    session_id: str
    project_folder: str
    cwd: str
    slug: str
    first_user_msg: str
    last_asst_msg: str
    turn_count_in_window: int
    start_time: datetime  # earliest in-window entry
    end_time: datetime  # latest in-window entry
    model: str
    concurrent_with: List[str] = field(default_factory=list)


@dataclass
class RepoActivity:
    cwd: str
    branch: str
    commits_in_window: int
    ahead: int
    behind: int
    dirty: bool
    worktrees: List[str]
    session_count: int = 0
    label: str = ""  # short display label (populated by aggregator)


@dataclass
class ActivityReport:
    window: ActivityWindow
    sessions: List[SessionActivity]
    repos: Dict[str, RepoActivity]


def parse_activity_window(args) -> ActivityWindow:
    """Build a UTC-aware time window from --since or --days."""
    now = datetime.now(timezone.utc)
    if getattr(args, "since", None):
        try:
            naive = datetime.strptime(args.since, "%Y-%m-%d")
        except ValueError:
            print(
                f"Invalid --since date: {args.since!r} (expected YYYY-MM-DD)",
                file=sys.stderr,
            )
            sys.exit(1)
        # Treat as local-midnight, convert to UTC
        start = naive.astimezone().astimezone(timezone.utc)
    else:
        days = max(1, args.days)
        start = now - timedelta(days=days)
    label = f"{start.astimezone().strftime('%b %-d')}–{now.astimezone().strftime('%b %-d')}"
    return ActivityWindow(start=start, end=now, label=label)


def _extract_first_text(content) -> str:
    """Pull the first non-empty text fragment from a message.content payload."""
    for kind, t in iter_content_fragments(content):
        if kind == "text" and t.strip():
            return t.strip()
    return ""


def count_user_turns_in_window(entries: List[Dict], window: ActivityWindow) -> int:
    """Count real user turns (skipping tool-results and notifications) in window."""
    count = 0
    for entry in entries:
        if entry.get("type") != "user":
            continue
        ts = _parse_ts(entry.get("timestamp", ""))
        if not ts or not (window.start <= ts <= window.end):
            continue
        text = _extract_first_text(entry.get("message", {}).get("content", ""))
        if text and not is_wrapper_text(text):
            count += 1
    return count


def extract_session_activity(
    session_path: Path, window: ActivityWindow
) -> Optional[SessionActivity]:
    """Read a session and return its activity record, or None if outside window."""
    # Cheap mtime gate — file untouched within window has no in-window entries
    try:
        mtime = session_path.stat().st_mtime
    except OSError:
        return None
    if datetime.fromtimestamp(mtime, tz=timezone.utc) < window.start:
        return None

    entries = load_raw_entries(str(session_path))
    if not entries:
        return None

    in_window_ts: List[datetime] = []
    first_user_msg = ""
    last_asst_msg = ""
    for entry in entries:
        ts = _parse_ts(entry.get("timestamp", ""))
        if not ts or not (window.start <= ts <= window.end):
            continue
        in_window_ts.append(ts)
        etype = entry.get("type")
        msg = entry.get("message", {})
        if etype == "user" and not first_user_msg:
            text = _extract_first_text(msg.get("content", ""))
            if text and not is_wrapper_text(text):
                first_user_msg = text.replace("\n", " ")[:80]
        elif etype == "assistant":
            text = _extract_first_text(msg.get("content", []))
            if text:
                last_asst_msg = text.replace("\n", " ")[:80]

    if not in_window_ts:
        return None

    turn_count = count_user_turns_in_window(entries, window)
    if turn_count == 0:
        return None

    meta = extract_session_metadata(entries)
    cwd_display = project_folder_to_cwd(session_path.parent.name)

    return SessionActivity(
        session_id=session_path.stem,
        project_folder=session_path.parent.name,
        cwd=cwd_display,
        slug=meta.slug,
        first_user_msg=first_user_msg or "(no text)",
        last_asst_msg=last_asst_msg,
        turn_count_in_window=turn_count,
        start_time=min(in_window_ts),
        end_time=max(in_window_ts),
        model=meta.model,
    )


def collect_session_activities(
    base_dir: str,
    window: ActivityWindow,
    min_turns: int,
    project_filter: Optional[str],
) -> List[SessionActivity]:
    projects = list_projects(base_dir)
    if project_filter:
        filt = project_filter.lower()
        projects = [p for p in projects if filt in p.name.lower()]

    results: List[SessionActivity] = []
    for proj in projects:
        for sess in list_sessions(proj):
            try:
                mtime = sess.stat().st_mtime
            except OSError:
                continue
            if datetime.fromtimestamp(mtime, tz=timezone.utc) < window.start:
                continue
            activity = extract_session_activity(sess, window)
            if activity and activity.turn_count_in_window >= min_turns:
                results.append(activity)

    results.sort(key=lambda s: s.start_time, reverse=True)
    return results


# ---- Git activity ----


def _git(cwd: str, *args: str, timeout: int = 5) -> str:
    """Inline git wrapper. Returns stdout or empty string on any failure."""
    try:
        r = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def _collect_repo(cwd: str, window: ActivityWindow) -> RepoActivity:
    expanded = os.path.expanduser(cwd)
    if not _git(expanded, "rev-parse", "--git-dir"):
        return RepoActivity(
            cwd=cwd,
            branch="(not a git repo)",
            commits_in_window=0,
            ahead=0,
            behind=0,
            dirty=False,
            worktrees=[],
        )

    branch = _git(expanded, "rev-parse", "--abbrev-ref", "HEAD") or "(detached)"

    since_iso = window.start.isoformat()
    log_out = _git(expanded, "log", f"--since={since_iso}", "--oneline")
    commits = len(log_out.splitlines()) if log_out else 0

    dirty = bool(_git(expanded, "status", "--porcelain"))

    ahead, behind = 0, 0
    if _git(expanded, "rev-parse", "--abbrev-ref", "@{u}"):
        rev = _git(expanded, "rev-list", "--left-right", "--count", "@{u}...HEAD")
        if rev:
            parts = rev.split()
            if len(parts) == 2:
                try:
                    behind = int(parts[0])
                    ahead = int(parts[1])
                except ValueError:
                    pass

    wt_out = _git(expanded, "worktree", "list", "--porcelain")
    worktrees = [
        line[len("worktree ") :] for line in wt_out.splitlines() if line.startswith("worktree ")
    ]

    return RepoActivity(
        cwd=cwd,
        branch=branch,
        commits_in_window=commits,
        ahead=ahead,
        behind=behind,
        dirty=dirty,
        worktrees=worktrees,
    )


def collect_repo_activities(
    cwds: List[str],
    window: ActivityWindow,
) -> Dict[str, RepoActivity]:
    if not cwds:
        return {}
    unique = list(set(cwds))
    results: Dict[str, RepoActivity] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        future_to_cwd = {ex.submit(_collect_repo, c, window): c for c in unique}
        for fut in concurrent.futures.as_completed(future_to_cwd):
            cwd = future_to_cwd[fut]
            try:
                results[cwd] = fut.result()
            except OSError:
                # _collect_repo can only fail environmentally: _git swallows
                # every subprocess error and returns "". So this drops a repo
                # whose path went away mid-run, and lets a real bug propagate
                # instead of silently shortening the activity report.
                continue
    return results


# ---- Concurrency detection ----


def mark_concurrency(sessions: List[SessionActivity]) -> None:
    """Populate each session's concurrent_with list with overlapping session ids."""
    by_start = sorted(sessions, key=lambda s: s.start_time)
    for i, a in enumerate(by_start):
        for b in by_start[i + 1 :]:
            if b.start_time >= a.end_time:
                break  # sorted by start; no further overlaps with a
            # Overlap iff a.start < b.end AND b.start < a.end
            if a.start_time < b.end_time and b.start_time < a.end_time:
                a.concurrent_with.append(b.session_id)
                b.concurrent_with.append(a.session_id)


# ---- Renderer ----


def _activity_short_label(activity: SessionActivity) -> str:
    """Project tail + short session id, used in concurrency annotations."""
    tail = collapsed_project_label(activity.project_folder)
    return f"{tail}:{activity.session_id[:8]}"


def render_activity_terminal(
    report: ActivityReport,
    console: Optional[Console],
    no_color: bool,
) -> None:
    sessions = report.sessions
    repos = report.repos
    use_rich = console is not None and not no_color

    total_commits = sum(r.commits_in_window for r in repos.values())
    header = (
        f"Activity · {report.window.label} · "
        f"{len(repos)} project{'s' if len(repos) != 1 else ''} · "
        f"{len(sessions)} session{'s' if len(sessions) != 1 else ''} · "
        f"{total_commits} commit{'s' if total_commits != 1 else ''}"
    )

    if use_rich:
        console.print(f"\n[bold]{header}[/bold]\n")
    else:
        print()
        print(header)
        print()

    # ---- Project summary ----
    if repos:
        ordered = sorted(repos.items(), key=lambda kv: -kv[1].session_count)
        if use_rich:
            table = Table(
                show_header=True,
                header_style="bold blue",
                box=box.SIMPLE,
                expand=False,
            )
            table.add_column("Project", style="cyan", no_wrap=True)
            table.add_column("Branch", no_wrap=True)
            table.add_column("Sess", justify="right", style="dim")
            table.add_column("Commits", justify="right")
            table.add_column("↑/↓", justify="right", style="dim")
            table.add_column("Dirty", justify="center")
            table.add_column("WT", justify="right", style="dim")
            for cwd, repo in ordered:
                ab = f"↑{repo.ahead} ↓{repo.behind}" if (repo.ahead or repo.behind) else ""
                dirty_mark = "●" if repo.dirty else ""
                wt_count = str(len(repo.worktrees)) if len(repo.worktrees) > 1 else ""
                table.add_row(
                    repo.label or cwd,
                    repo.branch,
                    str(repo.session_count),
                    str(repo.commits_in_window),
                    ab,
                    dirty_mark,
                    wt_count,
                )
            console.print(table)
        else:
            print(
                f"{'Project':<24} {'Branch':<25} "
                f"{'Sess':>5} {'Commits':>8} {'↑/↓':>10} {'Dirty':>5} {'WT':>3}"
            )
            for cwd, repo in ordered:
                ab = f"↑{repo.ahead} ↓{repo.behind}" if (repo.ahead or repo.behind) else ""
                dirty_mark = "●" if repo.dirty else ""
                wt_count = str(len(repo.worktrees)) if len(repo.worktrees) > 1 else ""
                print(
                    f"{(repo.label or cwd):<24} {repo.branch:<25} "
                    f"{repo.session_count:>5} {repo.commits_in_window:>8} "
                    f"{ab:>10} {dirty_mark:>5} {wt_count:>3}"
                )

    # ---- Chronology ----
    if use_rich:
        console.print("\n[bold]Chronology[/bold]")
    else:
        print("\nChronology")
        print("==========")

    by_day: Dict[str, List[SessionActivity]] = defaultdict(list)
    day_order: List[str] = []  # preserves newest-first ordering from `sessions`
    for s in sessions:
        day_key = s.start_time.astimezone().strftime("%a %b %-d")
        if day_key not in by_day:
            day_order.append(day_key)
        by_day[day_key].append(s)

    id_to_label = {s.session_id: _activity_short_label(s) for s in sessions}

    for day in day_order:
        day_sessions = sorted(by_day[day], key=lambda s: s.start_time)
        if use_rich:
            console.print(f"\n[bold yellow]{day}[/bold yellow]")
            console.print("─" * max(len(day), 12))
        else:
            print(f"\n{day}")
            print("─" * max(len(day), 12))

        for s in day_sessions:
            start_dt = s.start_time.astimezone()
            end_dt = s.end_time.astimezone()
            start_local = start_dt.strftime("%H:%M")
            end_local = end_dt.strftime("%H:%M")
            day_delta = (end_dt.date() - start_dt.date()).days
            end_suffix = f"+{day_delta}d" if day_delta else ""
            time_range = f"{start_local}━{end_local}{end_suffix}"
            proj_label = collapsed_project_label(s.project_folder)
            topic = s.slug or s.first_user_msg or "(no topic)"
            if len(topic) > 60:
                topic = topic[:57] + "..."
            lane = "┃" if s.concurrent_with else " "

            # Pad time_range to a consistent width (HH:MM━HH:MM = 11 chars + opt suffix)
            time_col = f"{time_range:<14}"
            prefix_plain = f" {time_col}  {proj_label:<24} {lane}  "
            if use_rich:
                console.print(
                    f" [dim]{time_col}[/dim]  "
                    f"[cyan]{proj_label:<24}[/cyan] {lane}  "
                    f"{topic}  [dim]{s.turn_count_in_window}t[/dim]"
                )
            else:
                print(f"{prefix_plain}{topic}  {s.turn_count_in_window}t")

            if s.concurrent_with:
                labels = sorted({id_to_label.get(cid, cid[:8]) for cid in s.concurrent_with})
                MAX_SHOWN = 3
                if len(labels) > MAX_SHOWN:
                    shown = labels[:MAX_SHOWN]
                    summary = f"{', '.join(shown)} and {len(labels) - MAX_SHOWN} more"
                else:
                    summary = ", ".join(labels)
                indent = " " * (len(prefix_plain) - 2)
                line = f"{indent}└─ concurrent with {summary}"
                if use_rich:
                    console.print(f"[dim]{line}[/dim]")
                else:
                    print(line)


def activity_mode(args, console: Optional[Console]) -> None:
    window = parse_activity_window(args)

    sessions = collect_session_activities(
        args.base_dir,
        window,
        min_turns=args.min_turns,
        project_filter=args.project,
    )

    if not sessions:
        msg = f"No sessions in window ({window.label})"
        if console and not args.no_color:
            console.print(f"[dim]{msg}[/dim]")
        else:
            print(msg)
        return

    cwds = list({s.cwd for s in sessions})
    repos = collect_repo_activities(cwds, window)
    # Populate session_count and a friendly display label per repo
    cwd_to_label: Dict[str, str] = {}
    for s in sessions:
        cwd_to_label.setdefault(s.cwd, collapsed_project_label(s.project_folder))
    for cwd in cwds:
        if cwd in repos:
            repos[cwd].session_count = sum(1 for s in sessions if s.cwd == cwd)
            repos[cwd].label = cwd_to_label.get(cwd, cwd)

    mark_concurrency(sessions)

    report = ActivityReport(window=window, sessions=sessions, repos=repos)
    render_activity_terminal(report, console, args.no_color)


def main():
    args = parse_args()

    # Show help if no mode specified (avoids launching interactive TUI unexpectedly).
    # MODE_OVERVIEW is the single source for this — a second hand-written list here
    # is exactly how the old quick-start drifted to advertising stale verbs.
    if not args.mode:
        print("Usage: claude-ls <mode> [options]")
        print()
        print(MODE_OVERVIEW)
        print("\nRun claude-ls -h for mode descriptions, or <mode> --help for options.")
        sys.exit(0)

    # Setup console. With no tty to measure (a `!` command, an agent's Bash call, a
    # pipe), rich falls back to 80 columns, and `who`'s no_wrap columns leave its
    # topic column a word wide. An explicit COLUMNS still wins.
    width = None if sys.stdout.isatty() or "COLUMNS" in os.environ else 200
    console = None if args.no_color else Console(width=width)

    # Route to appropriate mode
    if args.mode == "list":
        list_messages(args, console)
    elif args.mode == "get":
        get_message(args, console)
    elif args.mode == "select":
        select_message(args, console)
    elif args.mode == "browse":
        browse_mode(args, console)
    elif args.mode == "last":
        last_mode(args, console)
    elif args.mode == "search":
        search_content_mode(args, console)
    elif args.mode == "pending":
        pending_mode(args, console)
    elif args.mode == "export":
        export_mode(args, console)
    elif args.mode == "sessions":
        sessions_mode(args, console)
    elif args.mode in ("here", "find"):
        here_mode(args, console)
    elif args.mode in ("info", "show", "summary"):
        summary_mode(args, console)
    elif args.mode == "resume":
        resume_mode(args, console)
    elif args.mode == "resolve":
        resolve_mode(args, console)
    elif args.mode == "activity":
        activity_mode(args, console)
    elif args.mode == "who":
        who_mode(args, console)


if __name__ == "__main__":
    main()
