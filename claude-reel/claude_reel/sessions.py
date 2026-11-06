"""Session discovery over the Claude Code store (`~/.claude/projects`).

Stdlib-only on purpose. claude-graph reuses claude-ls via an importlib seam, but that
pulls claude-ls's whole import graph (blessed, rich) just to list files — and all
claude-reel needs from the store is *which transcript*. Parsing lives in extract.py, so a
direct glob keeps the tool dependency-free.

Store layout: top-level session transcripts at `<project>/<session-id>.jsonl`; subagent
transcripts live under `<project>/<session-id>/subagents/`, so a non-recursive
`<project>/*.jsonl` glob excludes them.
"""

from pathlib import Path


def projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def iter_session_files():
    """Top-level session transcripts across all projects, newest-first by mtime."""
    base = projects_dir()
    if not base.is_dir():
        return []
    files = []
    for project in base.iterdir():
        if not project.is_dir():
            continue
        for sf in project.glob("*.jsonl"):
            try:
                files.append((sf.stat().st_mtime, sf))
            except OSError:
                continue
    files.sort(key=lambda t: t[0], reverse=True)
    return [sf for _, sf in files]


def resolve_session(target: str) -> Path | None:
    """A session id / substring / path -> a transcript Path. A direct `.jsonl` path
    wins; otherwise the newest session whose file stem contains `target`."""
    p = Path(target).expanduser()
    if p.is_file():
        return p
    for sf in iter_session_files():
        if target in sf.stem:
            return sf
    return None
