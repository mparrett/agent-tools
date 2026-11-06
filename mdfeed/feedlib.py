"""Shared derivation logic for the mdfeed viewer and its PostToolUse hook.

Both entry points run with this directory as sys.path[0] (the viewer as a uv
script, the hook as a bare-python3 script invoked by absolute path), so a
plain sibling import works for both. Keep this module stdlib-only — the hook
has no dependency resolution.
"""

import re
from pathlib import Path
from typing import Optional

PLAN_BASENAME_RE = re.compile(r"^plan(\.[\w-]+)?\.md$", re.IGNORECASE)


def _git_root_name(start: Path) -> str:
    for parent in [start] + list(start.parents):
        if (parent / ".git").exists():
            return parent.name
    return ""


def derive_project(file_path: Path, cwd: str) -> str:
    """Nearest .git basename of the file; for files outside any repo (e.g.
    plans), fall back to cwd's git root, then cwd's basename, then a slice of
    the file's own parents."""
    name = _git_root_name(file_path)
    if name:
        return name
    if cwd:
        cwd_path = Path(cwd)
        name = _git_root_name(cwd_path)
        if name:
            return name
        return cwd_path.name or cwd
    parts = [p for p in file_path.parts if p not in ("/", "")]
    return "/".join(parts[-3:-1]) if len(parts) >= 2 else (parts[-1] if parts else "")


def derive_category(file_path: Path, home: Optional[Path] = None) -> str:
    home = home or Path.home()
    try:
        file_path.relative_to(home / ".claude" / "plans")
        return "plan"
    except ValueError:
        pass
    if PLAN_BASENAME_RE.match(file_path.name):
        return "plan"
    return "note"
