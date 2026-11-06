"""The seam: the only module that knows how to reach claude-ls.

claude-ls is a single-file script with a hyphenated name, so it is loaded by
path with importlib (the mechanism its own tests and claude-graph use) and the
primitives this package needs are re-exported here under stable names. Several
are underscore-private in claude-ls; deleting or reshaping one there breaks
this package silently, which is why agent-tools' CLAUDE.md names this file as
a seam to grep before touching them.

Store locations are also resolved here so tests can point every reader at a
synthetic store with environment variables alone.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path


def _find_claude_ls() -> Path | None:
    env = os.environ.get("CLAUDE_LS_PATH")
    if env and Path(env).is_file():
        return Path(env)
    # monorepo layout: <repo>/workstream/workstream/seam.py -> <repo>/claude-ls/
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
            "workstream needs claude-ls as a library but couldn't find claude-ls.py. "
            "Set CLAUDE_LS_PATH, or run from within the agent-tools monorepo "
            "(workstream must live beside claude-ls)."
        )
    spec = importlib.util.spec_from_file_location("claude_ls", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_cl = _load_claude_ls()

# Transcript reading
load_raw_entries = _cl.load_raw_entries
extract_session_metadata = _cl.extract_session_metadata
SessionMetadata = _cl.SessionMetadata
derive_work_signals = _cl.derive_work_signals
WorkSignals = _cl.WorkSignals
session_chain = _cl.session_chain
session_origin = _cl.session_origin
is_wrapper_text = _cl.is_wrapper_text
user_prompt_text = _cl.user_prompt_text
extract_text = _cl._extract_text
tail_entries = _cl._tail_entries
parse_ts = _cl._parse_ts
EDIT_TOOLS = _cl._EDIT_TOOLS
PR_URL_RE = _cl._PR_URL_RE

# Repos and workspaces
repo_root_for = _cl._repo_root_for
canonical_repo_root = _cl._canonical_repo_root
linked_worktree_main = _cl._git_linked_worktree_main

# Stores
list_projects = _cl.list_projects
list_sessions = _cl.list_sessions
file_is_codex = _cl._file_is_codex
entries_are_codex = _cl._entries_are_codex
codex_sessions_dir = _cl.codex_sessions_dir
iter_codex_session_refs = _cl.iter_codex_session_refs
codex_project_key = _cl.codex_project_key
codex_session_meta = _cl._codex_session_meta
live_registry = _cl._live_registry
sessions_registry_dir = _cl._sessions_registry_dir


def projects_dir() -> Path:
    """The Claude Code session store. WORKSTREAM_PROJECTS_DIR overrides it (tests)."""
    override = os.environ.get("WORKSTREAM_PROJECTS_DIR")
    return Path(override) if override else Path.home() / ".claude" / "projects"


def hooklog_dir() -> Path:
    """Where hooklog writes; the same variable hooklog itself reads."""
    override = os.environ.get("CLAUDE_HOOK_LOG_DIR")
    return Path(override) if override else Path.home() / "logs" / "claude-hooks"


def weft_bin() -> str:
    return os.environ.get("WORKSTREAM_WEFT_BIN") or "weft"


# The caller session, for `match`/`attach` defaults.
me_session_id = _cl._me_session_id
