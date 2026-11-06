"""Extract: a transcript.jsonl -> a **reel** (session meta + an ordered list of beats).

A beat is one normalized content block. The order events happened in *is* the story, so
we keep it flat and ordered and let curation group it. Parsing is stdlib-only and ported
from an earlier session viewer's `parse_transcript` (the tested reference for the on-disk shape).

Beat kinds: user_say, assistant_say, thinking, tool_call, tool_result, subagent-linked
tool_call (carries agentType/description). Tool calls also carry a derived `label`/`arg`/
`stat` so the renderer can show a chip without re-deriving.
"""

import json
from pathlib import Path

MAX_RESULT_CHARS = 20_000

# Synthetic "user" messages Claude injects that aren't the human's prompt. A user_say
# beat is dropped when its text opens with one of these.
#
# Hand-synced with claude-ls's `_WRAPPER_TAGS`, which is the canonical copy. This
# module is stdlib-only on purpose — see sessions.py — so it can't import that one
# without dragging rich and blessed in for a tuple of strings. A drift test in
# claude-ls's suite pins the two together; add a tag there first.
#
# `[request interrupted` is the marker Claude Code writes when the human stops a
# turn: not a tag, and claude-ls has no equivalent, so it stays local.
_INJECTED_PREFIXES = (
    "<local-command-caveat",
    "<local-command-stdout",
    "<command-name",
    "<command-message",
    "<command-args",
    "<command-contents",
    "<command-stdout",
    "<bash-input",
    "<bash-stdout",
    "<bash-stderr",
    "<user-memory-input",
    "<system-reminder",
    "<persisted-output",
    "<user-prompt-submit-hook",
    "<task-notification",
    "<task-id",
    "<tool-use-id",
    "<output-file",
    "[request interrupted",
)


def _iter_lines(path: Path):
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _drop_user_text(role, text, is_meta) -> bool:
    """Whether a user message is synthetic (not the human's prompt) and should not become
    a beat. `isMeta` is Claude Code's own flag for injected turns — skill bodies, hook
    output, tool-sourced messages — and reliably marks them without any text sniffing;
    the prefix list catches the plain-text markers (interrupts) that aren't flagged."""
    return role == "user" and (
        bool(is_meta) or text.lstrip().lower().startswith(_INJECTED_PREFIXES)
    )


def _normalize_result(content) -> tuple[str, bool]:
    """Tool results arrive as a string or a list of blocks. Flatten to text, report
    whether we truncated."""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                parts.append(b.get("text", "") if b.get("type") == "text" else json.dumps(b)[:500])
            else:
                parts.append(str(b))
        text = "\n".join(parts)
    else:
        text = json.dumps(content)
    if len(text) > MAX_RESULT_CHARS:
        return text[
            :MAX_RESULT_CHARS
        ] + f"\n… [truncated {len(text) - MAX_RESULT_CHARS} chars]", True
    return text, False


def _subagent_map(path: Path) -> dict:
    """tool_use id -> {agentType, description} for calls that spawned a subagent.

    The link is on disk: every `<session>/subagents/agent-*.meta.json` sidecar carries
    the `toolUseId` of the call that launched it. Read the tiny sidecars, not the
    transcripts. Degrade to empty on any malformed sidecar."""
    out = {}
    sub_dir = path.with_suffix("") / "subagents"
    if not sub_dir.is_dir():
        return out
    for meta_file in sub_dir.glob("agent-*.meta.json"):
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        tid = meta.get("toolUseId")
        if tid:
            out[tid] = {"agentType": meta.get("agentType"), "description": meta.get("description")}
    return out


def _base(p) -> str:
    return str(p).rsplit("/", 1)[-1] if p else "?"


def _first_line(s: str, n: int = 68) -> str:
    line = (s or "").strip().splitlines()[0] if (s or "").strip() else ""
    return line if len(line) <= n else line[: n - 1] + "…"


def _diff_stat(added: int, removed: int) -> str | None:
    if not added and not removed:
        return None
    bits = []
    if added:
        bits.append(f"+{added}")
    if removed:
        bits.append(f"−{removed}")
    return " ".join(bits)


def derive_tool_summary(name: str, inp: dict) -> tuple[str, str, str | None]:
    """(label, arg, stat) for a tool call's chip. label is the short kind word, arg the
    subject (file / command / pattern), stat an optional right-aligned note."""
    inp = inp or {}
    if name in ("Edit", "MultiEdit"):
        edits = inp.get("edits") or [
            {"old_string": inp.get("old_string", ""), "new_string": inp.get("new_string", "")}
        ]
        add = sum(len((e.get("new_string") or "").splitlines()) for e in edits)
        rem = sum(len((e.get("old_string") or "").splitlines()) for e in edits)
        return "edit", _base(inp.get("file_path")), _diff_stat(add, rem)
    if name == "Write":
        add = len((inp.get("content") or "").splitlines())
        return "write", _base(inp.get("file_path")), _diff_stat(add, 0)
    if name == "Read":
        return "read", _base(inp.get("file_path")), None
    if name == "Bash":
        return "run", _first_line(inp.get("command", "")), None
    if name in ("Grep", "Glob"):
        return name.lower(), _first_line(inp.get("pattern") or inp.get("query") or "", 48), None
    if name in ("Task", "Agent"):
        return (
            "agent",
            (inp.get("subagent_type") or "agent"),
            _first_line(inp.get("description", ""), 40),
        )
    if name == "TodoWrite":
        todos = inp.get("todos") or []
        return "todo", f"{len(todos)} items", None
    if name in ("WebFetch", "WebSearch"):
        return "web", _first_line(inp.get("url") or inp.get("query") or "", 48), None
    return name.lower(), _first_line(json.dumps(inp), 48), None


def extract(path: Path) -> dict:
    """transcript.jsonl -> {"meta": {...}, "beats": [...]}"""
    path = Path(path)
    beats = []
    tool_calls: dict[str, int] = {}
    usage_in = usage_out = 0
    first_ts = last_ts = None
    model = cwd = branch = session_id = None
    sub_map = _subagent_map(path)

    for obj in _iter_lines(path):
        ts = obj.get("timestamp")
        if ts:
            first_ts = first_ts or ts
            last_ts = ts
        cwd = obj.get("cwd") or cwd
        branch = obj.get("gitBranch") or branch
        session_id = obj.get("sessionId") or session_id

        msg = obj.get("message")
        otype = obj.get("type")
        role = msg.get("role") if isinstance(msg, dict) else otype
        is_meta = obj.get("isMeta")

        if isinstance(msg, dict):
            model = msg.get("model") or model
            u = msg.get("usage") or {}
            usage_in += u.get("input_tokens", 0) or 0
            usage_out += u.get("output_tokens", 0) or 0

        content = msg.get("content") if isinstance(msg, dict) else None

        if isinstance(content, str):
            if content.strip() and not _drop_user_text(role, content, is_meta):
                beats.append({"kind": _say_kind(role), "ts": ts, "text": content, "model": model})
            continue
        if not isinstance(content, list):
            continue

        for b in content:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "thinking":
                beats.append(
                    {"kind": "thinking", "ts": ts, "text": b.get("thinking", b.get("text", ""))}
                )
            elif bt == "text":
                text = b.get("text", "")
                if text.strip() and not _drop_user_text(role, text, is_meta):
                    beats.append({"kind": _say_kind(role), "ts": ts, "text": text, "model": model})
            elif bt == "tool_use":
                name = b.get("name", "?")
                tool_calls[name] = tool_calls.get(name, 0) + 1
                label, arg, stat = derive_tool_summary(name, b.get("input", {}))
                beat = {
                    "kind": "tool_call",
                    "ts": ts,
                    "id": b.get("id"),
                    "name": name,
                    "input": b.get("input", {}),
                    "label": label,
                    "arg": arg,
                    "stat": stat,
                }
                sub = sub_map.get(b.get("id"))
                if sub:
                    beat.update(sub)
                    beat["label"] = "agent"
                    beat["arg"] = sub.get("agentType") or beat["arg"]
                    if sub.get("description"):
                        beat["stat"] = _first_line(sub["description"], 40)
                beats.append(beat)
            elif bt == "tool_result":
                text, truncated = _normalize_result(b.get("content"))
                beats.append(
                    {
                        "kind": "tool_result",
                        "ts": ts,
                        "tool_use_id": b.get("tool_use_id"),
                        "is_error": bool(b.get("is_error")),
                        "text": text,
                        "truncated": truncated,
                    }
                )

    meta = {
        "session_id": session_id or path.stem,
        "project": _base(cwd),
        "cwd": cwd,
        "branch": branch,
        "model": model,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "tool_calls": tool_calls,
        "input_tokens": usage_in,
        "output_tokens": usage_out,
    }
    return {"meta": meta, "beats": beats}


def _say_kind(role) -> str:
    return "user_say" if role == "user" else "assistant_say"
