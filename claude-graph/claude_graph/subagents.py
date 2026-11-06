"""Subagent traversal — the one filesystem walk claude-ls lacks.

A session's subagents live under `<session-id>/subagents/` as `agent-<id>.jsonl`
transcripts, each with an `agent-<id>.meta.json` sidecar carrying the `toolUseId` of
the Agent/Task call that spawned it (plus `agentType`, `description`). The link is
written on disk, not inferred — so the graph can hang each subagent under the exact
call that launched it.

Ported from an earlier session viewer (`server.py`). Parse defensively: a missing or malformed
sidecar means that call shows no child, never an error.
"""

import json
import sys
from pathlib import Path


def subagent_dir(session_path) -> Path:
    """Where a session's subagents live: `<session-file-without-.jsonl>/subagents`.
    (Under the session-id directory, not the slug dir — a gotcha found while porting.)"""
    return Path(session_path).with_suffix("") / "subagents"


def find_subagent_sessions(session_path) -> dict:
    """Map each spawning `toolUseId` -> `{agentType, description, agent_file}`.

    Returns an empty dict when the session spawned none (or the dir is absent). Reads
    only the tiny `.meta.json` sidecars, not the transcripts.
    """
    out: dict = {}
    sub_dir = subagent_dir(session_path)
    if not sub_dir.is_dir():
        return out
    for meta_file in sorted(sub_dir.glob("agent-*.meta.json")):
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            # Degrade to "no child", but say so — a silent skip would hide the format
            # drift this guard exists to catch.
            print(
                f"claude-graph: skipping unreadable sidecar {meta_file.name}: {e}",
                file=sys.stderr,
            )
            continue
        tid = meta.get("toolUseId")
        transcript = sub_dir / (meta_file.name[: -len(".meta.json")] + ".jsonl")
        if tid and transcript.is_file():
            out[tid] = {
                "agentType": meta.get("agentType"),
                "description": (meta.get("description") or "").strip(),
                "agent_file": str(transcript),
            }
    return out
