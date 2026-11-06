#!/usr/bin/env python3
"""PostToolUse hook: log .md file writes/edits to ~/.claude/md-feed/index.jsonl.

Reads tool input from stdin, appends one JSON line per qualifying event.
Never blocks Claude — always exits 0 even on internal errors.
"""

import fcntl
import json
import os
import sys
import time
from pathlib import Path

from feedlib import derive_category, derive_project

FEED_PATH = Path(
    os.environ.get("MD_FEED_PATH") or str(Path.home() / ".claude" / "md-feed" / "index.jsonl")
)

WATCHED_TOOLS = {"Write", "Edit", "MultiEdit"}
SKIP_SEGMENTS = {
    "node_modules",
    ".git",
    "dist",
    "build",
    ".venv",
    "venv",
    "__pycache__",
    ".next",
    ".cache",
}


def should_skip(file_path: Path, home: Path) -> bool:
    name_lower = file_path.name.lower()
    if not (name_lower.endswith(".md") or name_lower.endswith(".markdown")):
        return True
    if file_path.name == "CLAUDE.md":
        return True
    # Skip anything under ~/.claude/ EXCEPT ~/.claude/plans/
    try:
        file_path.relative_to(home / ".claude")
        try:
            file_path.relative_to(home / ".claude" / "plans")
        except ValueError:
            return True
    except ValueError:
        pass
    if any(seg in SKIP_SEGMENTS for seg in file_path.parts):
        return True
    return False


def main() -> None:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return
        data = json.loads(raw)

        tool_name = data.get("tool_name", "")
        if tool_name not in WATCHED_TOOLS:
            return

        tool_input = data.get("tool_input") or {}
        file_path_str = tool_input.get("file_path") or ""
        if not file_path_str:
            return

        file_path = Path(file_path_str).expanduser()
        if not file_path.is_absolute():
            cwd = data.get("cwd") or os.getcwd()
            file_path = Path(cwd) / file_path
        # Canonicalize symlinks (macOS /tmp → /private/tmp) so a file always
        # enters the feed under one path string — `mdfeed add` resolves too,
        # and both dedupe-by-path and the viewer's allowlist compare strings.
        file_path = file_path.resolve()

        home = Path.home()
        if should_skip(file_path, home):
            return

        entry = {
            "ts": time.time(),
            "path": str(file_path),
            "tool": tool_name,
            "cwd": data.get("cwd") or "",
            "project": derive_project(file_path, data.get("cwd") or ""),
            "category": derive_category(file_path, home),
        }

        FEED_PATH.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with open(FEED_PATH, "a", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.write(line)
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except Exception:
        # Never block Claude. Swallow everything.
        return


if __name__ == "__main__":
    main()
