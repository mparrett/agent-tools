"""Extract: a transcript.jsonl -> a **reel** (session meta + an ordered list of beats).

A beat is one normalized content block. The order events happened in *is* the story, so
we keep it flat and ordered and let curation group it. Parsing is stdlib-only and ported
from an earlier session viewer's `parse_transcript` (the tested reference for the on-disk shape).

Beat kinds: user_say, assistant_say, thinking, tool_call, tool_result, subagent-linked
tool_call (carries agentType/description), context (what the harness injected into the
model's request), and subagent (a launch with no tool call of its own). Tool calls also
carry a derived `label`/`arg`/`stat` so the renderer can show a chip without re-deriving.
"""

import hashlib
import json
import re
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


def _subagent_pool(path: Path) -> dict:
    """agent id -> {"path", "meta"} for every subagent transcript under
    `<session>/subagents/`, with its `.meta.json` sidecar ({} when unreadable)."""
    pool = {}
    sub_dir = path.with_suffix("") / "subagents"
    if not sub_dir.is_dir():
        return pool
    for f in sorted(sub_dir.glob("agent-*.jsonl")):
        try:
            meta = json.loads(f.with_suffix(".meta.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}
        pool[f.stem[len("agent-") :]] = {"path": f, "meta": meta if isinstance(meta, dict) else {}}
    return pool


# A forked skill (e.g. /code-review) launches its agent with no tool call, so its sidecar
# has no toolUseId; the parent records the launch as a system line embedding
# {"agentId": "..."}. Ported from claude-ls's `_spawned_by`, which export --subagents uses.
_AGENT_ID_REF = re.compile(r'"agentId"\s*:\s*"([A-Za-z0-9]+)"')


def _launched_ids(obj: dict, pool: dict) -> list:
    """Agent ids a non-assistant entry launched without a tool call of its own: an
    `agentId` reference in a system/user line, or a Skill tool_result naming one."""
    if obj.get("type") not in ("system", "user"):
        return []
    found = []
    msg = obj.get("message")
    text = (
        obj.get("content")
        if obj.get("type") == "system"
        else (msg.get("content") if isinstance(msg, dict) else None)
    )
    if isinstance(text, str) and "agentId" in text:
        found += [a for a in _AGENT_ID_REF.findall(text) if a in pool]
    result = obj.get("toolUseResult")
    if isinstance(result, dict) and result.get("agentId") in pool:
        found.append(result["agentId"])
    return found


def _subagent_brief(sub_path: Path) -> dict:
    """What an agent chip shows in `--subagents brief`: the prompt it was given, its
    tool counts, and the last thing it said. Reuses extract on the subagent's own
    transcript, without context or nested subagents."""
    try:
        reel = extract(sub_path, context=False)
    except OSError:
        return {"path": str(sub_path)}
    beats = reel["beats"]
    # A forked skill's prompt is flagged isMeta, so it isn't a user_say; the first user
    # entry is the prompt whichever way it was flagged.
    prompt = next(
        (
            t
            for o in _iter_lines(sub_path)
            if o.get("type") == "user" and isinstance(o.get("message"), dict)
            for t in _message_texts(o["message"].get("content"))
            if t.strip()
        ),
        "",
    )
    final = next((b["text"] for b in reversed(beats) if b["kind"] == "assistant_say"), "")
    m = reel["meta"]
    return {
        "path": str(sub_path),
        "prompt": prompt,
        "final": final,
        "tool_calls": m["tool_calls"],
        "first_ts": m["first_ts"],
        "last_ts": m["last_ts"],
    }


# ---- context: what the harness put in the model's request ---------------------
#
# Newer transcripts log each API request's new parts as `api-request-blob` entries.
# Their user/assistant parts copy messages already in the transcript; their system
# parts (token budget, environment updates, reminders, notifications) appear nowhere
# else. `prompt_snapshot` attachments carry the full system prompt.

_BUDGET_RE = re.compile(r"<total_tokens>\s*(\d+)\s+tokens left\s*</total_tokens>")


def _human_tokens(n: int) -> str:
    return f"{n / 1e6:.2f}M" if n >= 1_000_000 else f"{n / 1e3:.0f}k"


_LEAD_TAG_RE = re.compile(r"^<([a-z][a-z0-9_-]*)[^>]*>")
_ANY_TAG_RE = re.compile(r"</?[a-z][a-z0-9_-]*[^>]*>")
# Chip labels for the wrapper tags that open injected text; anything else shows its tag.
_TAG_LABELS = {
    "system-reminder": "reminder",
    "total_tokens": "budget",
    "task-notification": "task",
    "local-command-stdout": "stdout",
    "local-command-caveat": "caveat",
    "command-name": "command",
    "command-message": "command",
    "bash-input": "bash",
    "bash-stdout": "bash",
    "bash-stderr": "bash",
    "user-prompt-submit-hook": "hook",
}


def _context_summary(text: str) -> tuple[str, str]:
    """(label, arg) for a context chip: what kind of injection, and its first words."""
    m = _BUDGET_RE.fullmatch(text.strip())
    if m:
        return "budget", f"{_human_tokens(int(m.group(1)))} tokens left"
    body = text.strip()
    label = "ctx"
    tag = _LEAD_TAG_RE.match(body)
    if tag:
        label = _TAG_LABELS.get(tag.group(1), tag.group(1))
        body = body[tag.end() :]
    summary = re.search(r"<summary>(.*?)</summary>", body, re.S)
    if summary:  # a task notification's own one-line account of itself
        body = summary.group(1)
    if body.lstrip().startswith("[SYSTEM NOTIFICATION"):
        label, body = "notice", body.lstrip().split("\n", 1)[-1]
    lines = (_ANY_TAG_RE.sub(" ", ln).strip().lstrip("#").strip() for ln in body.splitlines())
    first = next((ln for ln in lines if ln), "")
    if label == "ctx" and first.lower().startswith("environment"):
        label = "env"
    return label, _first_line(first, 60)


def _context_beat(text: str, ts, label=None) -> dict:
    lbl, arg = _context_summary(text)
    return {
        "kind": "context",
        "ts": ts,
        "label": label or lbl,
        "arg": arg,
        "text": text,
        # Repeats render as "unchanged since beat N"; the budget varies every request
        # and is one line anyway, so it is never keyed.
        "key": None if lbl == "budget" else hashlib.sha1(text.encode()).hexdigest()[:16],
    }


def _message_texts(content) -> list:
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        ]
    return []


def _conversation_texts(path: Path) -> set:
    """Every text block the transcript's own user/assistant entries carry, so a blob
    part that merely copies one can be told apart from injected context."""
    seen = set()
    for obj in _iter_lines(path):
        if obj.get("type") in ("user", "assistant") and isinstance(obj.get("message"), dict):
            seen.update(t.strip() for t in _message_texts(obj["message"].get("content")) if t)
    return seen


def _context_beats(obj: dict, ts, conversation: set) -> list:
    otype = obj.get("type")
    if otype == "attachment":
        att = obj.get("attachment") or {}
        if att.get("type") == "prompt_snapshot" and att.get("systemPrompt"):
            sp = att["systemPrompt"]
            text = "\n\n".join(sp) if isinstance(sp, list) else str(sp)
            return [_context_beat(text, ts, label="sysprompt")]
        return []
    msg = obj.get("message")
    if otype != "api-request-blob" or not isinstance(msg, dict):
        return []
    role = msg.get("role")
    if role == "assistant":
        return []
    out = []
    for t in _message_texts(msg.get("content")):
        if t.strip() and (role == "system" or t.strip() not in conversation):
            out.append(_context_beat(t, ts))
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


def extract(path: Path, context: bool = True, subagents: str = "none") -> dict:
    """transcript.jsonl -> {"meta": {...}, "beats": [...]}

    context: emit `context` beats for what the harness injected into requests.
    subagents: "none" links agent chips to their sidecar's type/description only;
    "brief" also places every subagent at its launch with a summary of its run."""
    path = Path(path)
    beats = []
    tool_calls: dict[str, int] = {}
    usage_in = usage_out = 0
    first_ts = last_ts = None
    model = cwd = branch = session_id = None
    pool = _subagent_pool(path)
    by_tool = {p["meta"]["toolUseId"]: aid for aid, p in pool.items() if p["meta"].get("toolUseId")}
    brief = subagents == "brief"
    placed: set = set()
    conversation = _conversation_texts(path) if context else set()

    def sub_fields(aid):
        meta = pool[aid]["meta"]
        out = {"agentType": meta.get("agentType"), "description": meta.get("description")}
        if brief:
            placed.add(aid)
            out["subagent"] = _subagent_brief(pool[aid]["path"])
        return out

    for obj in _iter_lines(path):
        ts = obj.get("timestamp")
        if ts:
            first_ts = first_ts or ts
            last_ts = ts
        cwd = obj.get("cwd") or cwd
        branch = obj.get("gitBranch") or branch
        session_id = obj.get("sessionId") or session_id
        otype = obj.get("type")

        if brief:
            for aid in _launched_ids(obj, pool):
                if aid not in placed:
                    beats.append({"kind": "subagent", "ts": ts or last_ts, **sub_fields(aid)})

        # Only the conversation's own entries become say/tool beats. Other entry types
        # also carry a `message` (an api-request-blob copies the request), and reading
        # those as turns put injected context on screen as Claude's prose.
        if otype not in ("user", "assistant"):
            if context:
                beats.extend(_context_beats(obj, ts or last_ts, conversation))
            continue

        msg = obj.get("message")
        role = msg.get("role") if isinstance(msg, dict) else otype
        is_meta = obj.get("isMeta")

        if isinstance(msg, dict):
            model = msg.get("model") or model
            u = msg.get("usage") or {}
            usage_in += u.get("input_tokens", 0) or 0
            usage_out += u.get("output_tokens", 0) or 0

        content = msg.get("content") if isinstance(msg, dict) else None

        def say(text):
            if not text.strip():
                return
            if not _drop_user_text(role, text, is_meta):
                beats.append({"kind": _say_kind(role), "ts": ts, "text": text, "model": model})
            elif context:
                # Injected rather than typed: still worth seeing, as context, not as you.
                beats.append(_context_beat(text, ts))

        if isinstance(content, str):
            say(content)
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
                say(b.get("text", ""))
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
                aid = by_tool.get(b.get("id"))
                if aid:
                    sub = sub_fields(aid)
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

    if brief:
        # A subagent with no recoverable launch still goes in, by its start time, so
        # nothing on disk is silently dropped (same rule as claude-ls export).
        for aid in pool:
            if aid not in placed:
                b = {"kind": "subagent", **sub_fields(aid)}
                b["ts"] = b["subagent"].get("first_ts")
                i = next(
                    (i for i, x in enumerate(beats) if (x.get("ts") or "") > (b["ts"] or "")),
                    len(beats),
                )
                beats.insert(i, b)

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
