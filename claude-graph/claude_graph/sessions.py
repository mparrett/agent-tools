"""The seam.

claude-graph builds on claude-ls's transcript→work-signal parsing rather than
reimplementing it (a second copy is exactly the divergence we keep fixing). claude-ls
is a single-file script whose hyphenated filename isn't a valid module name, so we
load it by path with importlib — the same mechanism its own test suite uses — and
re-export the primitives claude-graph needs.

This is the ONLY file that knows how to reach claude-ls. If the parsing layer is ever
extracted into a shared core, only this module changes.
"""

import importlib.util
import os
from pathlib import Path


def _find_claude_ls() -> Path | None:
    """Locate claude-ls.py. Checks $CLAUDE_LS_PATH, then the monorepo source layout,
    then walks up from the cwd. Returns None if not found."""
    env = os.environ.get("CLAUDE_LS_PATH")
    if env and Path(env).is_file():
        return Path(env)
    # monorepo layout: <repo>/claude-graph/claude_graph/sessions.py -> <repo>/claude-ls/
    cand = Path(__file__).resolve().parent.parent.parent / "claude-ls" / "claude-ls.py"
    if cand.is_file():
        return cand
    for base in [Path.cwd(), *Path.cwd().parents]:
        c = base / "claude-ls" / "claude-ls.py"
        if c.is_file():
            return c
    return None


def _load_claude_ls():
    path = _find_claude_ls()
    if path is None:
        raise RuntimeError(
            "claude-graph needs claude-ls as a library but couldn't find claude-ls.py. "
            "Set CLAUDE_LS_PATH, or run from within the agent-tools monorepo "
            "(claude-graph must live beside claude-ls)."
        )
    spec = importlib.util.spec_from_file_location("claude_ls", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_cl = _load_claude_ls()

# Re-exported primitives (names mirror claude-ls). The leading-underscore claude-ls
# names are internal there but are our public surface here.
WorkSignals = _cl.WorkSignals
derive_work_signals = _cl.derive_work_signals
canonical_repo_root = _cl._canonical_repo_root
repo_root_for = _cl._repo_root_for
list_projects = _cl.list_projects
list_sessions = _cl.list_sessions
extract_session_metadata = _cl.extract_session_metadata
parse_activity_window = _cl.parse_activity_window
# The wrapper-tag vocabulary lives in claude-ls so there's one copy to keep true.
is_wrapper_text = _cl.is_wrapper_text


def projects_dir() -> Path:
    """The Claude Code session store (`~/.claude/projects`) — claude-ls's default base."""
    return Path.home() / ".claude" / "projects"


def iter_session_files():
    """Every top-level session transcript across all projects (newest-first per
    project). Subagent transcripts live in subdirs and are excluded."""
    for project in list_projects(str(projects_dir())):
        yield from list_sessions(project)
