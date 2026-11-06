#!/usr/bin/env python3
"""SessionStart hook: tell a session who else is live in its repo.

Several Claude Code sessions in one repo is normal: launch from the main
checkout, spawn worktrees. The cost is rarely clobbering; it is confusion. A
question meant for one stream lands in another, and the two duplicate or derail,
because a session has no idea its siblings exist. This prints them, from
`claude-ls who`, and only when there are any: a lone session gets nothing, so
the common case costs zero tokens. Measured 2026-09-05 on a live fleet: a
three-session repo costs about 190 tokens, a one-session repo nothing.

Register it for SessionStart in `~/.claude/settings.json` (see claude-ls/README.md).
Startup, resume, clear, compact and fork each hand the model a context that has
never seen the fleet, so match all of them. Never fails the session: any error goes to
stderr, which is not added to context, and the exit code is 0. Stdlib only;
`claude-ls` is found on PATH or beside this file.

Test seams: SESSION_MATES_WHO_JSON names a file to read instead of running
claude-ls; SESSION_MATES_CLAUDE_LS overrides the executable.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

MAX_MATES = 8
CLIP = 100
SIBLING_CLAUDE_LS = Path(__file__).resolve().parent / "claude-ls.py"


def claude_ls() -> str | None:
    override = os.environ.get("SESSION_MATES_CLAUDE_LS")
    if override:
        return override if os.access(override, os.X_OK) else None
    found = shutil.which("claude-ls")
    if found:
        return found
    return str(SIBLING_CLAUDE_LS) if os.access(SIBLING_CLAUDE_LS, os.X_OK) else None


def fetch_rows(cwd: str) -> list[dict]:
    fixture = os.environ.get("SESSION_MATES_WHO_JSON")
    if fixture:
        return json.loads(Path(fixture).read_text(encoding="utf-8"))
    exe = claude_ls()
    if not exe:
        raise FileNotFoundError("claude-ls: not on PATH and no claude-ls.py beside this hook")
    out = subprocess.run(
        [exe, "who", "-C", cwd, "--json"], capture_output=True, text=True, timeout=8
    )
    if out.returncode != 0:
        raise RuntimeError(f"claude-ls who exited {out.returncode}: {out.stderr.strip()[:200]}")
    rows = json.loads(out.stdout or "[]")
    return rows if isinstance(rows, list) else []


def _clip(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= CLIP else text[: CLIP - 1] + "…"


def _age(minutes) -> str:
    if not isinstance(minutes, int):
        return ""
    if minutes < 60:
        return f"{minutes}m"
    if minutes < 24 * 60:
        return f"{minutes // 60}h"
    return f"{minutes // (24 * 60)}d"


def _short_model(model: str) -> str:
    return model[len("claude-") :] if model.startswith("claude-") else model


def render(rows: list[dict], my_session_id: str, now: datetime | None = None) -> str:
    """The block a session reads. Empty when it has no repo-mates."""
    mates = [r for r in rows if isinstance(r, dict) and r.get("session_id") != my_session_id]
    if not mates:
        return ""
    # The block stays in context for the whole session and gets read as the live
    # fleet hours after it went stale; the stamp is what lets the model notice.
    stamp = (now or datetime.now()).astimezone()
    lines = [
        f"Other live Claude sessions in this repo, from `claude-ls who` as of "
        f"{stamp:%Y-%m-%d %H:%M %Z}. This is a snapshot from session start and goes "
        "stale; run `claude-ls who` for the current view:"
    ]
    for r in mates[:MAX_MATES]:
        status = r.get("status") or "?"
        if status != "busy" and isinstance(r.get("status_age_min"), int):
            status += f" {_age(r['status_age_min'])}"
        if r.get("waiting"):
            status += ", last message was a question for the human"
        where = f"{r.get('worktree') or '?'} @ {r.get('branch') or '?'}"
        model = _short_model(r.get("model") or "") or "?"
        head = f"- {r.get('pane') or 'no pane'} ({(r.get('session_id') or '')[:8]}): {status}, {where}, {model}"
        lines.append(head)
        if r.get("topic"):
            lines.append(f"    topic: {_clip(r['topic'])}")
        if r.get("last_prompt"):
            lines.append(f"    last prompt: {_clip(r['last_prompt'])}")
        touched = r.get("touched") or []
        if touched:
            base = (r.get("toplevel") or "").rstrip("/")
            rel = [p[len(base) + 1 :] if base and p.startswith(base + "/") else p for p in touched]
            lines.append(f"    touching: {', '.join(rel)}")
    if len(mates) > MAX_MATES:
        lines.append(f"- … and {len(mates) - MAX_MATES} more")
    lines.append(
        "If a request here seems to be about one of those streams, say so before acting "
        "and ask whether to take it over or leave it there."
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        cwd = str(payload.get("cwd") or os.getcwd())
        block = render(fetch_rows(cwd), str(payload.get("session_id") or ""))
        if block:
            sys.stdout.write(block)
    except Exception as e:  # a missing fleet view is a nuisance; a broken session start is not
        sys.stderr.write(f"session_mates: {e}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
