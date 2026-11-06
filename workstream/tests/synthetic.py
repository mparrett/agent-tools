"""Synthetic session store for workstream tests.

D14: nothing real goes in the tree. Every repo, person, pane and id here is
invented; ids come from uuid5 over a fixed namespace so a case names the same
sessions on every run, and a failing assertion can quote them.

The on-disk shapes mirror what the readers parse (claude-ls for transcripts and
Codex rollouts, hooklog for lifecycle and SendMessage, the per-pid registry),
not a simplified model of them: a fixture that only matches our own parser
would pass while the real store breaks it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

NAMESPACE = uuid.UUID("6f1c1d2e-0b7a-4c55-9a51-3e2f5d7c9b10")
CLAUDE_VERSION = "2.1.316"
MODEL = "claude-opus-5-5"
CODEX_MODEL = "gpt-5-codex"
GH_OWNER = "acme"
GH_AUTHOR = "agent-operator"  # every agent pushes as the same human, as in the mining doc

# (from_utc, offset): the local zone in force from that instant on. Includes a
# move east, a move back, and the DST end, so one window holds three offsets.
DEFAULT_ZONE_SCHEDULE: list[tuple[datetime, timedelta]] = [
    (datetime(2026, 10, 19, tzinfo=UTC), timedelta(hours=-7)),
    (datetime(2026, 10, 26, tzinfo=UTC), timedelta(hours=-5)),
    (datetime(2026, 10, 29, 21, 0, tzinfo=UTC), timedelta(hours=-7)),
    (datetime(2026, 11, 1, 9, 0, tzinfo=UTC), timedelta(hours=-8)),
]

_GIT_EPOCH = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


def ts_z(dt: datetime) -> str:
    """Transcript form: UTC, millisecond precision, `Z` suffix."""
    dt = dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def slug_for(path: str | Path) -> str:
    """Claude's project-folder slug; same rule as claude-ls encode_path_to_folder_name."""
    return re.sub(r"[^a-zA-Z0-9]", "-", str(path))


def stable_uuid(kind: str, name: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"{kind}:{name}"))


def pr_url(slug: str, number: int) -> str:
    return f"https://github.com/{slug}/pull/{number}"


@dataclass
class SessionSpec:
    name: str
    id: str
    kind: str  # "claude" | "subagent" | "codex"
    path: Path
    cwd: Path
    start: datetime
    model: str
    parent: SessionSpec | None = None
    origin: str | None = None  # root sid this one was /clear'ed from
    fork_of: str | None = None
    usage_peak: int = 0
    # When set, assistant turns after the last compaction ramp to this instead
    # of continuing the whole-transcript ramp, the way real context drops.
    post_compact_peak: int | None = None
    pid: int | None = None
    pane: str | None = None
    records: list[tuple[datetime, int, dict]] = field(default_factory=list, repr=False)

    @property
    def sid8(self) -> str:
        return self.id[:8]

    @property
    def ref(self) -> str:
        if self.kind == "subagent":
            return f"agent:{self.parent.id}/{self.id}"
        return f"{'codex' if self.kind == 'codex' else 'claude'}:{self.id}"

    @property
    def last_seen(self) -> datetime:
        return max((r[0] for r in self.records), default=self.start)

    @property
    def project_dir(self) -> Path:
        if self.kind == "subagent":
            return self.path.parent.parent.parent
        return self.path.parent

    @property
    def hook_session_id(self) -> str:
        # Hooks fire under the main session's id; a subagent's carry agent_id too.
        return self.parent.id if self.kind == "subagent" else self.id


class SyntheticStore:
    def __init__(
        self,
        root: Path,
        zone_schedule: list[tuple[datetime, timedelta]] | None = None,
    ):
        root.mkdir(parents=True, exist_ok=True)
        # Resolved so paths match what git and realpath report (/var vs /private/var).
        self.root = root.resolve()
        self.zone_schedule = sorted(zone_schedule or DEFAULT_ZONE_SCHEDULE)
        self.projects_dir = self.root / "claude" / "projects"
        self.sessions_dir = self.root / "claude" / "sessions"
        self.codex_dir = self.root / "codex" / "sessions"
        self.hooklog_dir = self.root / "hooklog"
        self.weft_home = self.root / "weft"
        self.cache_dir = self.root / "cache"
        for d in (
            self.projects_dir,
            self.sessions_dir,
            self.codex_dir,
            self.hooklog_dir,
            self.weft_home,
            self.cache_dir,
            self.root / "home",
        ):
            d.mkdir(parents=True, exist_ok=True)
        self.repos: dict[str, Path] = {}
        self.worktrees: dict[Path, dict[str, Any]] = {}
        self.sessions: dict[str, SessionSpec] = {}
        self._hooks: list[tuple[datetime, int, dict]] = []
        self._seq = 0
        self._git_commits = 0
        self._branch_cache: dict[Path, str] = {}

    # ------------------------------------------------------------------ env

    def env(self) -> dict[str, str]:
        return {
            "WORKSTREAM_PROJECTS_DIR": str(self.projects_dir),
            "CLAUDE_LS_CODEX_DIR": str(self.codex_dir),
            "CLAUDE_LS_SESSIONS_DIR": str(self.sessions_dir),
            "CLAUDE_HOOK_LOG_DIR": str(self.hooklog_dir),
            "WEFT_HOME": str(self.weft_home),
            "WORKSTREAM_CACHE_DIR": str(self.cache_dir),
        }

    @property
    def hooklog_path(self) -> Path:
        return self.hooklog_dir / "hook_events.log"

    def offset_at(self, at: datetime) -> timedelta:
        offset = self.zone_schedule[0][1]
        for since, off in self.zone_schedule:
            if at >= since:
                offset = off
        return offset

    def local_naive(self, at: datetime) -> datetime:
        return (at.astimezone(UTC) + self.offset_at(at)).replace(tzinfo=None)

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    # ------------------------------------------------------------------ git

    def _git_env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        when = (_GIT_EPOCH + timedelta(minutes=self._git_commits)).isoformat()
        env.update(
            GIT_CONFIG_GLOBAL="/dev/null",
            GIT_CONFIG_NOSYSTEM="1",
            HOME=str(self.root / "home"),
            GIT_AUTHOR_NAME="Synthetic Dev",
            GIT_AUTHOR_EMAIL="dev@example.invalid",
            GIT_COMMITTER_NAME="Synthetic Dev",
            GIT_COMMITTER_EMAIL="dev@example.invalid",
            GIT_AUTHOR_DATE=when,
            GIT_COMMITTER_DATE=when,
        )
        return env

    def git(self, cwd: Path, *args: str) -> str:
        out = subprocess.run(
            ["git", "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false", *args],
            cwd=cwd,
            env=self._git_env(),
            check=True,
            capture_output=True,
            text=True,
        )
        return out.stdout.strip()

    def commit(self, checkout: Path, relpath: str, content: str, message: str) -> str:
        target = checkout / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        self.git(checkout, "add", relpath)
        self._git_commits += 1
        self.git(checkout, "commit", "-q", "-m", message)
        return self.git(checkout, "rev-parse", "HEAD")

    def repo(self, name: str, remote: bool = True) -> Path:
        if name in self.repos:
            return self.repos[name]
        path = self.root / "repos" / name
        path.mkdir(parents=True)
        self.git(path, "init", "-q")
        # Claude's worktrees live under .claude/ inside the repo; keep them out
        # of the main checkout's status so "dirty" means real work.
        with open(path / ".git" / "info" / "exclude", "a") as f:
            f.write(".claude/\n")
        self.commit(path, "README.md", f"# {name}\n", "initial")
        if remote:
            bare = self.root / "remotes" / f"{name}.git"
            bare.parent.mkdir(parents=True, exist_ok=True)
            self.git(self.root, "init", "-q", "--bare", str(bare))
            self.git(path, "remote", "add", "origin", str(bare))
            self.git(path, "push", "-q", "-u", "origin", "main")
        self.repos[name] = path
        return path

    def clone(self, repo: Path, dest: Path) -> Path:
        """A second checkout of `repo` somewhere else (Codex Desktop's habit)."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        self.git(self.root, "clone", "-q", str(repo), str(dest))
        return dest

    def checkout(self, repo: Path, branch: str, create: bool = True) -> None:
        self.git(repo, "switch", "-q", *(["-c"] if create else []), branch)

    def worktree(
        self,
        repo: Path,
        name: str,
        unmerged_commit: bool = True,
        push: bool = False,
        branch: str | None = None,
        message: str | None = None,
    ) -> Path:
        branch = branch or f"wt/{name}"
        path = repo / ".claude" / "worktrees" / name
        self.git(repo, "worktree", "add", "-q", "-b", branch, str(path), "main")
        sha = None
        if unmerged_commit:
            sha = self.commit(
                path,
                f"src/{name.replace('-', '_')}.py",
                f"# {name}\nFIXED = True\n",
                message or f"fix: {name.replace('-', ' ')}",
            )
        if push:
            self.git(path, "push", "-q", "-u", "origin", branch)
        self.worktrees[path] = {"repo": repo, "branch": branch, "commit": sha, "pushed": push}
        return path

    def retire_worktree(self, path: Path, merge: bool = True) -> None:
        """End a slice the usual way: squash-merge the worktree's branch into
        main (GitHub's button, done locally), then remove the worktree. The
        branch itself stays, as `git worktree remove` leaves it."""
        info = self.worktrees[path]
        repo = info["repo"]
        if merge:
            self.git(repo, "merge", "-q", "--squash", info["branch"])
            self.git(repo, "commit", "-q", "-m", f"{info['branch']} (squashed)")
        self.git(repo, "worktree", "remove", "--force", str(path))

    def _git_branch(self, cwd: Path) -> str:
        try:
            return self.git(cwd, "branch", "--show-current")
        except (subprocess.CalledProcessError, FileNotFoundError, NotADirectoryError):
            return ""

    # ---------------------------------------------------------------- hooks

    def hook(self, event: str, session: SessionSpec, at: datetime, **fields: Any) -> None:
        """One hooklog line, stamped in the local zone in force at `at`."""
        data: dict[str, Any] = {
            "session_id": session.hook_session_id,
            "transcript_path": str(
                session.parent.path if session.kind == "subagent" else session.path
            ),
            "cwd": str(session.cwd),
            "hook_event_name": event,
        }
        if session.kind == "subagent":
            data["agent_id"] = session.id
        data.update(fields)
        self._hooks.append((at.astimezone(UTC), self._next_seq(), data))
        self._flush_hooks()

    def _flush_hooks(self) -> None:
        with open(self.hooklog_path, "w") as f:
            for at, _, data in sorted(self._hooks, key=lambda h: (h[0], h[1])):
                line = {"timestamp": self.local_naive(at).isoformat(timespec="microseconds")}
                line["hook_data"] = data
                f.write(json.dumps(line, separators=(",", ":")) + "\n")

    def _tool_hooks(
        self, session: SessionSpec, at: datetime, tool: str, tin: dict, result: Any
    ) -> None:
        tool_use_id = f"toolu_{stable_uuid('tool', f'{session.id}:{at.isoformat()}:{tool}')[:24]}"
        if tool == "Bash" and isinstance(result, str):
            response: Any = {
                "stdout": result,
                "stderr": "",
                "interrupted": False,
                "isImage": False,
            }
        else:
            response = result
        common = {"tool_name": tool, "tool_input": tin, "tool_use_id": tool_use_id}
        self.hook("PreToolUse", session, at, **common)
        self.hook(
            "PostToolUse", session, at + timedelta(seconds=1), tool_response=response, **common
        )

    # ----------------------------------------------------------- transcripts

    def _add(self, spec: SessionSpec, at: datetime, record: dict) -> None:
        spec.records.append((at.astimezone(UTC), self._next_seq(), record))

    def _base(self, spec: SessionSpec, at: datetime, rtype: str) -> dict:
        rec: dict[str, Any] = {
            "type": rtype,
            "uuid": stable_uuid("record", f"{spec.id}:{len(spec.records)}"),
            "parentUuid": None,
            "timestamp": ts_z(at),
            "sessionId": spec.parent.id if spec.kind == "subagent" else spec.id,
            "cwd": str(spec.cwd),
            "gitBranch": self._branch_cache.get(spec.cwd, ""),
            "version": CLAUDE_VERSION,
            "userType": "external",
            "isSidechain": spec.kind == "subagent",
        }
        if spec.kind == "subagent":
            rec["agentId"] = spec.id
        if spec.origin:
            rec["session_id"] = spec.origin
        return rec

    def _user(self, spec: SessionSpec, at: datetime, content: Any, **extra: Any) -> None:
        rec = self._base(spec, at, "user")
        rec["message"] = {"role": "user", "content": content}
        rec.update(extra)
        self._add(spec, at, rec)

    def _assistant(self, spec: SessionSpec, at: datetime, content: list) -> None:
        rec = self._base(spec, at, "assistant")
        rec["message"] = {
            "role": "assistant",
            "model": spec.model,
            "content": content,
            "usage": {},  # filled at flush so the ramp covers late additions
        }
        self._add(spec, at, rec)

    def _tool(
        self, spec: SessionSpec, at: datetime, tool: str, tin: dict, result: Any, hooks: bool
    ) -> None:
        tid = f"toolu_{stable_uuid('tool', f'{spec.id}:{at.isoformat()}:{tool}')[:24]}"
        self._assistant(spec, at, [{"type": "tool_use", "id": tid, "name": tool, "input": tin}])
        text = result if isinstance(result, str) else json.dumps(result)
        self._user(
            spec,
            at + timedelta(seconds=2),
            [{"type": "tool_result", "tool_use_id": tid, "content": text}],
        )
        if hooks:
            self._tool_hooks(spec, at, tool, tin, result)

    def _flush(self, spec: SessionSpec) -> None:
        ordered = sorted(spec.records, key=lambda r: (r[0], r[1]))
        if spec.kind == "codex":
            lines = [rec for _, _, rec in ordered]
        else:
            assistants = [rec for _, _, rec in ordered if rec.get("type") == "assistant"]
            after: list[dict] = []
            if spec.post_compact_peak is not None:
                summaries = [i for i, (_, _, r) in enumerate(ordered) if r.get("isCompactSummary")]
                if summaries:
                    after = [
                        r for _, _, r in ordered[summaries[-1] :] if r.get("type") == "assistant"
                    ]
                    assistants = [r for r in assistants if not any(r is a for a in after)]
            for group, peak in ((assistants, spec.usage_peak), (after, spec.post_compact_peak)):
                n = len(group)
                for i, rec in enumerate(group):
                    ctx = max(8_000, round(peak * (i + 1) / n))
                    rec["message"]["usage"] = {
                        "input_tokens": 3,
                        "cache_creation_input_tokens": 500,
                        "cache_read_input_tokens": max(0, ctx - 503),
                        "output_tokens": 200,
                    }
            prev = None
            lines = []
            for _, _, rec in ordered:
                rec["parentUuid"] = prev
                if rec.get("type") in ("user", "assistant", "system"):
                    prev = rec["uuid"]
                lines.append(rec)
        spec.path.parent.mkdir(parents=True, exist_ok=True)
        with open(spec.path, "w") as f:
            for rec in lines:
                f.write(json.dumps(rec, separators=(",", ":")) + "\n")

    @staticmethod
    def _timeline(
        start: datetime, prompts: list, tools: list
    ) -> tuple[list[tuple[datetime, str]], list[tuple[datetime, str, dict, Any]]]:
        timed_prompts = []
        for i, p in enumerate(prompts):
            if isinstance(p, tuple):
                timed_prompts.append(p)
            else:
                timed_prompts.append((start + timedelta(minutes=10 * i), p))
        timed_tools = []
        for j, t in enumerate(tools):
            at = t[0] if t[0] is not None else start + timedelta(minutes=2, seconds=45 * j)
            timed_tools.append((at, t[1], t[2], t[3]))
        return timed_prompts, timed_tools

    def session(
        self,
        name: str,
        cwd: Path,
        prompts: list,
        tools: list | tuple = (),
        *,
        start: datetime,
        model: str = MODEL,
        usage_peak: int = 60_000,
        origin: SessionSpec | str | None = None,
        fork_of: SessionSpec | None = None,
        compactions: int = 0,
        post_compact_peak: int | None = None,
        last_seen: datetime | None = None,
        final: str | None = None,
        start_source: str | None = "startup",
        end: tuple[datetime, str] | None = None,
    ) -> SessionSpec:
        """Write a main Claude transcript and its hooklog lifecycle.

        prompts: str (placed every 10 min from start) or (at, text).
        tools: (at|None, tool_name, input, result); result str or dict.
        origin: the session this one was /clear'ed from (records carry its root).
        fork_of: copies the parent's records up to `start`, under the new id.
        end: (at, reason) for a SessionEnd; None leaves the session open.
        """
        sid = stable_uuid("claude", name)
        cwd = Path(cwd)
        if isinstance(origin, SessionSpec):
            origin = origin.origin or origin.id
        spec = SessionSpec(
            name=name,
            id=sid,
            kind="claude",
            path=self.projects_dir / slug_for(cwd) / f"{sid}.jsonl",
            cwd=cwd,
            start=start,
            model=model,
            origin=origin,
            fork_of=fork_of.id if fork_of else None,
            usage_peak=usage_peak,
            post_compact_peak=post_compact_peak,
        )
        self._remember_branch(cwd)
        if fork_of is not None:
            for at, _, rec in sorted(fork_of.records, key=lambda r: (r[0], r[1])):
                if at > start:
                    break
                copy = json.loads(json.dumps(rec))
                copy.update(
                    uuid=stable_uuid("record", f"{sid}:{len(spec.records)}"),
                    sessionId=sid,
                )
                self._add(spec, at, copy)

        if start_source:
            self.hook(
                "SessionStart",
                spec,
                start - timedelta(seconds=1),
                source=start_source,
                model=model,
            )
        timed_prompts, timed_tools = self._timeline(start, prompts, tools)
        for at, text in timed_prompts:
            self.hook("UserPromptSubmit", spec, at - timedelta(milliseconds=200), prompt=text)
            self._user(spec, at, text)
            self._assistant(spec, at + timedelta(seconds=4), [{"type": "text", "text": "On it."}])
        for at, tool, tin, result in timed_tools:
            self._tool(spec, at, tool, tin, result, hooks=True)

        end_at = last_seen or spec.last_seen
        if final is not None or last_seen is not None:
            self._assistant(
                spec,
                last_seen or end_at + timedelta(seconds=10),
                [{"type": "text", "text": final or "Waiting on you."}],
            )
        self._compact(spec, compactions)
        if end is not None:
            self.hook("SessionEnd", spec, end[0], reason=end[1])
        self.sessions[name] = spec
        self._flush(spec)
        return spec

    def _compact(self, spec: SessionSpec, n: int) -> None:
        if n <= 0:
            return
        first, last = spec.start, spec.last_seen
        for k in range(1, n + 1):
            at = first + (last - first) * k / (n + 1)
            self.hook("PreCompact", spec, at - timedelta(seconds=2), trigger="auto")
            rec = self._base(spec, at, "system")
            rec.update(
                subtype="compact_boundary",
                content="Conversation compacted",
                compactMetadata={"trigger": "auto", "preTokens": spec.usage_peak},
            )
            self._add(spec, at, rec)
            self._user(
                spec,
                at + timedelta(seconds=1),
                "This session is being continued from a previous conversation "
                "that ran out of context. The summary below covers the earlier portion.",
                isCompactSummary=True,
                isVisibleInTranscriptOnly=True,
            )
            self.hook("SessionStart", spec, at + timedelta(seconds=2), source="compact")

    def _remember_branch(self, cwd: Path) -> None:
        if cwd not in self._branch_cache:
            self._branch_cache[cwd] = self._git_branch(cwd) if cwd.is_dir() else ""

    def subagent(
        self,
        parent: SessionSpec,
        name: str,
        prompt: str,
        tools: list | tuple = (),
        *,
        start: datetime,
        agent_type: str = "general-purpose",
        description: str = "",
        agent_name: str | None = None,
        usage_peak: int = 30_000,
        final: str = "Done.",
    ) -> SessionSpec:
        agent_id = uuid.uuid5(NAMESPACE, f"agent:{name}").hex[:16]
        base = parent.project_dir / parent.id / "subagents" / f"agent-{agent_id}"
        spec = SessionSpec(
            name=name,
            id=agent_id,
            kind="subagent",
            path=base.with_suffix(".jsonl"),
            cwd=parent.cwd,
            start=start,
            model=parent.model,
            parent=parent,
            usage_peak=usage_peak,
        )
        self._remember_branch(parent.cwd)
        tin = {"description": description or name, "prompt": prompt, "subagent_type": agent_type}
        self._tool(parent, start - timedelta(seconds=1), "Agent", tin, f"agentId: {agent_id}", True)
        self._user(spec, start, prompt)
        _, timed_tools = self._timeline(start, [], tools)
        for at, tool, tin2, result in timed_tools:
            self._tool(spec, at, tool, tin2, result, hooks=False)
        done = spec.last_seen + timedelta(seconds=20)
        self._assistant(spec, done, [{"type": "text", "text": final}])
        self.hook("SubagentStop", spec, done + timedelta(seconds=1), agent_type=agent_type)
        meta = {
            "agentType": agent_type,
            "description": description or name,
            "name": agent_name or name.rsplit("/", 1)[-1],
            "spawnDepth": 1,
            "finished": True,
        }
        spec.path.parent.mkdir(parents=True, exist_ok=True)
        base.with_suffix(".meta.json").write_text(json.dumps(meta))
        self.sessions[name] = spec
        self._flush(spec)
        self._flush(parent)
        return spec

    # ---------------------------------------------------------------- codex

    def codex(
        self,
        name: str,
        cwd: Path,
        prompts: list,
        commands: list,
        *,
        start: datetime,
        parent: SessionSpec | None = None,
        source: str = "vscode",
    ) -> SessionSpec:
        """A Codex rollout. commands: str, or (at, command) / (at, command, output)."""
        rid = stable_uuid("codex", name)
        stamp = start.astimezone(UTC).strftime("%Y-%m-%dT%H-%M-%S")
        path = self.codex_dir / start.strftime("%Y/%m/%d") / f"rollout-{stamp}-{rid}.jsonl"
        spec = SessionSpec(
            name=name,
            id=rid,
            kind="codex",
            path=path,
            cwd=Path(cwd),
            start=start,
            model=CODEX_MODEL,
            parent=parent,
        )
        payload: dict[str, Any] = {
            "id": rid,
            "timestamp": ts_z(start),
            "cwd": str(cwd),
            "originator": "codex_cli_rs",
            "cli_version": "0.1",
            "source": source,
        }
        if parent is not None:
            payload["parent_thread_id"] = parent.id
            payload["thread_source"] = "subagent"
        self._add(
            spec, start, {"timestamp": ts_z(start), "type": "session_meta", "payload": payload}
        )
        ctx = {"cwd": str(cwd), "model": CODEX_MODEL}
        self._add(spec, start, {"timestamp": ts_z(start), "type": "turn_context", "payload": ctx})
        for i, p in enumerate(prompts):
            at, text = p if isinstance(p, tuple) else (start + timedelta(minutes=10 * i), p)
            item = {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            }
            self._add(spec, at, {"timestamp": ts_z(at), "type": "response_item", "payload": item})
            ev = {"type": "user_message", "message": text}
            self._add(spec, at, {"timestamp": ts_z(at), "type": "event_msg", "payload": ev})
        for j, c in enumerate(commands):
            if isinstance(c, str):
                at, cmd, output = start + timedelta(minutes=1, seconds=30 * j), c, ""
            else:
                at, cmd, output = (*c, "")[:3]
            call_id = f"call_{stable_uuid('call', f'{rid}:{j}')[:12]}"
            call = {
                "type": "function_call",
                "name": "shell",
                "call_id": call_id,
                "arguments": json.dumps({"command": ["bash", "-lc", cmd]}),
            }
            self._add(spec, at, {"timestamp": ts_z(at), "type": "response_item", "payload": call})
            out = {"type": "function_call_output", "call_id": call_id, "output": output}
            later = at + timedelta(seconds=1)
            self._add(
                spec, later, {"timestamp": ts_z(later), "type": "response_item", "payload": out}
            )
        self.sessions[name] = spec
        self._flush(spec)
        return spec

    # ------------------------------------------------------------- registry

    def _pid(self, spec: SessionSpec) -> int:
        if spec.pid is None:
            spec.pid = 30_000 + int(hashlib.sha256(spec.id.encode()).hexdigest()[:4], 16) % 20_000
        return spec.pid

    def _pane(self, spec: SessionSpec) -> str:
        return spec.pane or f"{spec.cwd.name}-{spec.id[:2]}"

    @staticmethod
    def socket(pid: int) -> str:
        return f"/tmp/cc-socks/{pid}.sock"

    def register(
        self,
        session: SessionSpec,
        name: str,
        pid: int,
        former_names: list | tuple = (),
        status: str = "idle",
        kind: str = "interactive",
    ) -> Path:
        """Registry file. former_names: (name, until, SessionSpec|sid) triples."""
        session.pid, session.pane = pid, name
        former = []
        for fname, until, who in former_names:
            sid = who.id if isinstance(who, SessionSpec) else who
            former.append({"name": fname, "until": int(until.timestamp() * 1000), "sessionId": sid})
        since = max((f["until"] for f in former), default=int(session.start.timestamp() * 1000))
        rec = {
            "pid": pid,
            "sessionId": session.id,
            "cwd": str(session.cwd),
            "startedAt": int(session.start.timestamp() * 1000),
            "name": name,
            "nameSince": since,
            "formerNames": former,
            "messagingSocketPath": self.socket(pid),
            "status": status,
            "kind": kind,
            "version": CLAUDE_VERSION,
        }
        path = self.sessions_dir / f"{pid}.json"
        path.write_text(json.dumps(rec))
        return path

    # -------------------------------------------------------------- messages

    def send_message(
        self,
        sender: SessionSpec,
        recipient: SessionSpec,
        body: str,
        at: datetime,
        to_raw: str | None = None,
        summary: str | None = None,
    ) -> str:
        """SendMessage as all three stores see it; returns the msg_id.

        The sender's transcript gets the tool call, the hooklog the Pre/Post
        pair, and the recipient's transcript the isMeta peer record whose body
        matches exactly (the D5 resolution key).
        """
        sender_pid = self._pid(sender)
        sender_name = self._pane(sender)
        if to_raw is None:
            to_raw = recipient.pane or f"uds:{self.socket(self._pid(recipient))}"
        kind = "session" if to_raw.startswith("uds:") else "name"
        summary = summary or body.split(".")[0][:60]
        msg_id = stable_uuid("msg", f"{sender.id}:{recipient.id}:{at.isoformat()}")
        tin = {
            "to": to_raw,
            "summary": summary,
            "message": body,
            "type": "message",
            "recipient": to_raw,
            "recipient_kind": kind,
            "content": body[:50],
        }
        response = {
            "success": True,
            "message": f"Message sent to {to_raw}",
            "display": f"“{summary}”",
            "msg_id": msg_id,
        }
        self._tool(sender, at, "SendMessage", tin, response, hooks=False)
        self._tool_hooks(sender, at, "SendMessage", tin, response)
        frm = f"uds:{self.socket(sender_pid)}"
        delivered = at + timedelta(seconds=2)
        text = (
            "Another Claude session sent a message:\n"
            f'<cross-session-message from="{frm}" from-name="{sender_name}" from-mode="prompting">\n'
            f"{body}\n</cross-session-message>"
        )
        origin = {
            "kind": "peer",
            "from": frm,
            "verifiedPeerPid": sender_pid,
            "msg_id": msg_id,
            "name": sender_name,
            "fromMode": "prompting",
            "body": body,
        }
        self._user(
            recipient, delivered, [{"type": "text", "text": text}], isMeta=True, origin=origin
        )
        self._flush(sender)
        self._flush(recipient)
        return msg_id


# ====================================================================
# Acceptance cases. Shapes follow the 2026-11-02 continuity mining doc;
# every name, id, number and path is invented.
# ====================================================================


def _review(login: str, state: str, at: datetime) -> dict:
    return {"author": {"login": login}, "state": state, "submittedAt": ts_z(at)}


def _gh_pr(
    slug: str,
    number: int,
    state: str,
    head: str,
    updated: datetime,
    reviews: list | tuple = (),
    decision: str | None = None,
    merged: datetime | None = None,
    draft: bool = False,
    requested: list | tuple = (),
) -> dict:
    return {
        "number": number,
        "url": pr_url(slug, number),
        "state": state,
        "isDraft": draft,
        "reviewDecision": decision,
        "updatedAt": ts_z(updated),
        "mergedAt": ts_z(merged) if merged else None,
        "headRefName": head,
        "author": {"login": GH_AUTHOR},
        "reviews": list(reviews),
        "reviewRequests": [{"login": r} for r in requested],
    }


def _bash(at: datetime | None, command: str, output: str = "") -> tuple:
    return (at, "Bash", {"command": command, "description": command.split()[0]}, output)


def case_audio_seam(store: SyntheticStore) -> dict:
    """Mass retirement, a three-hop pasted-id chain across two repos, and a
    review that landed mid-chain that nobody addressed."""
    orchard = store.repo("orchard")
    trellis = store.repo("trellis")
    slug, num = f"{GH_OWNER}/trellis", 57
    url = pr_url(slug, num)
    wt = store.worktree(
        trellis, "mixer-seam", push=True, branch="mixer-seam", message="mixer: add the seam"
    )

    keeper = store.session(
        "seam/trellis-keeper",
        trellis,
        [(utc(2026, 10, 20, 16, 5), "keep the trellis PR queue moving; you own merges here")],
        [_bash(utc(2026, 10, 20, 16, 8), f"gh pr list --repo {slug}", "no open pull requests")],
        start=utc(2026, 10, 20, 16, 5),
        usage_peak=90_000,
        last_seen=utc(2026, 10, 22, 2, 0),
    )
    store.register(keeper, "trellis-4f", 41001)

    root = store.session(
        "seam/root",
        orchard,
        [
            (
                utc(2026, 10, 21, 21, 52),
                "wire the mixer seam from orchard into trellis and open a PR",
            ),
            (utc(2026, 10, 26, 15, 0), "status on the trellis PR?"),
        ],
        [
            (
                utc(2026, 10, 21, 21, 58),
                "Edit",
                {
                    "file_path": str(wt / "src/mixer_seam.py"),
                    "old_string": "",
                    "new_string": "SEAM = 1\n",
                },
                "The file has been updated.",
            ),
            _bash(
                utc(2026, 10, 21, 22, 2),
                f"git -C {wt} push -u origin mixer-seam",
                "set up to track",
            ),
            _bash(
                utc(2026, 10, 21, 22, 4),
                f"gh pr create --repo {slug} --head mixer-seam --title 'mixer seam'",
                url,
            ),
        ],
        start=utc(2026, 10, 21, 21, 52),
        usage_peak=240_000,
        compactions=1,
        last_seen=utc(2026, 10, 29, 12, 57, 40),
        end=(utc(2026, 10, 29, 12, 58, 2), "other"),
    )
    store.send_message(
        root,
        keeper,
        "Taking over merging trellis PRs for the mixer seam.",
        utc(2026, 10, 21, 22, 7),
    )

    continuation = store.session(
        "seam/continuation",
        orchard,
        [
            (
                utc(2026, 10, 22, 4, 53),
                f"continue from session {root.id}; the trellis PR is the thing",
            )
        ],
        [
            _bash(
                utc(2026, 10, 22, 4, 55),
                f"gh pr view {num} --repo {slug}",
                f"state: OPEN\nurl: {url}",
            )
        ],
        start=utc(2026, 10, 22, 4, 53),
        usage_peak=280_000,
        compactions=2,
        last_seen=utc(2026, 10, 29, 13, 35, 30),
        end=(utc(2026, 10, 29, 13, 36), "other"),
    )

    pickup = store.session(
        "seam/pickup",
        trellis,
        [
            (
                utc(2026, 10, 29, 12, 59, 37),
                f"see session {continuation.sid8}... it had a lot of work up in the air",
            ),
            (utc(2026, 10, 30, 23, 40), "can we merge past the other reviewer?"),
            (utc(2026, 11, 1, 18, 30), "ok, merge past it?"),
        ],
        [
            _bash(
                utc(2026, 10, 29, 13, 2),
                f"gh pr view {num} --repo {slug} --json reviews",
                f"reviewer-one COMMENTED; reviewer-two APPROVED\n{url}",
            )
        ],
        start=utc(2026, 10, 29, 12, 59, 37),
        usage_peak=300_000,
        last_seen=utc(2026, 11, 1, 18, 42),
    )
    store.register(pickup, "trellis-85", 41002)
    reviewer_sub = store.subagent(
        pickup,
        "seam/pickup-review",
        "review the mixer seam diff for correctness",
        [
            (
                utc(2026, 10, 29, 14, 1),
                "Read",
                {"file_path": str(wt / "src/mixer_seam.py")},
                "SEAM = 1",
            )
        ],
        start=utc(2026, 10, 29, 14, 0),
        description="review mixer seam diff",
        agent_name="code-review",
    )

    review_at = utc(2026, 10, 30, 3, 52)  # 20:52 local under the -7h step

    shepherd = store.session(
        "seam/shepherd",
        trellis,
        [
            (
                utc(2026, 10, 31, 1, 12),
                "find which session i was using to shepherd the mixer change... "
                "then maybe you just take over",
            )
        ],
        [
            _bash(
                utc(2026, 10, 31, 1, 13),
                "claude-ls search mixer",
                f"{pickup.sid8}  trellis  mixer seam",
            ),
            _bash(
                utc(2026, 10, 31, 1, 14), f"claude-ls summary {pickup.sid8}", f"PRs: {slug}#{num}"
            ),
        ],
        start=utc(2026, 10, 31, 1, 12),
        end=(utc(2026, 10, 31, 1, 40), "clear"),
    )
    cleared = store.session(
        "seam/shepherd-cleared",
        trellis,
        [(utc(2026, 10, 31, 1, 41), "take over the mixer PR from here")],
        start=utc(2026, 10, 31, 1, 41),
        origin=shepherd,
        start_source="clear",
        usage_peak=120_000,
        last_seen=utc(2026, 10, 31, 5, 30),
    )
    store.register(
        cleared,
        "trellis-12",
        41003,
        former_names=[("trellis-11", utc(2026, 10, 31, 1, 40), shepherd)],
    )

    reviews_json = json.dumps(
        [
            _review("reviewer-one", "COMMENTED", utc(2026, 10, 24, 18, 0)),
            _review("reviewer-two", "APPROVED", utc(2026, 10, 28, 16, 0)),
            _review("reviewer-one", "CHANGES_REQUESTED", review_at),
        ]
    )
    conventions = store.session(
        "seam/conventions",
        trellis,
        [
            (
                utc(2026, 10, 31, 5, 44, 53),
                "find the session where i was just talking about repo conventions...",
            )
        ],
        [
            (
                utc(2026, 10, 31, 5, 46),
                "ListAgents",
                {},
                "16 live peers: trellis-12, trellis-85, ...",
            ),
            _bash(
                utc(2026, 10, 31, 5, 50),
                f"gh pr view {num} --repo {slug} --json reviews",
                reviews_json,
            ),
        ],
        start=utc(2026, 10, 31, 5, 44, 53),
        end=(utc(2026, 10, 31, 6, 10), "prompt_input_exit"),
    )
    conventions_fork = store.session(
        "seam/conventions-fork",
        trellis,
        [(utc(2026, 10, 31, 5, 45, 30), "same question; just list the candidates")],
        start=utc(2026, 10, 31, 5, 45),
        fork_of=conventions,
        start_source="fork",
        end=(utc(2026, 10, 31, 6, 5), "other"),
    )

    fresh = store.session(
        "seam/fresh",
        orchard,
        [(utc(2026, 11, 2, 16, 5), "where were we on the mixer thing?")],
        [
            _bash(
                utc(2026, 11, 2, 16, 6),
                f"gh pr list --repo {slug} --head mixer-seam",
                f"{num}\tmixer seam\tmixer-seam\tOPEN\n{url}",
            )
        ],
        start=utc(2026, 11, 2, 16, 5),
    )

    pr_ref = f"gh:{slug}#{num}"
    return {
        "name": "audio-seam",
        "sessions": {
            "keeper": keeper,
            "root": root,
            "continuation": continuation,
            "pickup": pickup,
            "pickup_review": reviewer_sub,
            "shepherd": shepherd,
            "shepherd_cleared": cleared,
            "conventions": conventions,
            "conventions_fork": conventions_fork,
        },
        "fresh": fresh,
        "repos": {"orchard": orchard, "trellis": trellis},
        "worktrees": {"mixer-seam": wt},
        "chain": [root.id, continuation.id, pickup.id],
        "forks": (conventions.id, conventions_fork.id),
        "workstreams": [
            {
                "name": "mixer-seam",
                "title": "Mixer seam from orchard into trellis",
                "anchors": [pr_ref, "branch:trellis@mixer-seam"],
                "canonical": [root, continuation, pickup, cleared],
                "ancillary": [shepherd, conventions, conventions_fork],
            },
            {
                "name": "trellis-queue",
                "title": "Trellis PR queue",
                "anchors": [],
                "canonical": [keeper],
                "ancillary": [],
            },
        ],
        "expected": "mixer-seam",
        "loose_end": {
            "kind": "unaddressed_review",
            "ref": pr_ref,
            "author": "reviewer-one",
            "state": "CHANGES_REQUESTED",
            "at": review_at,
        },
        "gh": {
            f"{slug}#{num}": _gh_pr(
                slug,
                num,
                "OPEN",
                "mixer-seam",
                review_at,
                reviews=json.loads(reviews_json),
                decision="CHANGES_REQUESTED",
            )
        },
    }


def case_pr_ownership(store: SyntheticStore) -> dict:
    """Two sessions ask who owns a PR seconds apart; a third opened it from its worktree."""
    grove = store.repo("grove")
    slug, num = f"{GH_OWNER}/grove", 203
    url = pr_url(slug, num)
    branch = "wt/fix-main-tests"
    wt = store.worktree(grove, "fix-main-tests", push=True, message="tests: unbreak main")

    owner = store.session(
        "own/opener",
        grove,
        [(utc(2026, 10, 21, 19, 0), "main's tests are red; fix them in a worktree and open a PR")],
        [
            (
                utc(2026, 10, 21, 19, 1),
                "EnterWorktree",
                {"name": "fix-main-tests"},
                {"worktreePath": str(wt), "worktreeBranch": branch, "message": "Entered worktree"},
            ),
            (
                utc(2026, 10, 21, 19, 10),
                "Edit",
                {
                    "file_path": str(wt / "src/fix_main_tests.py"),
                    "old_string": "",
                    "new_string": "OK\n",
                },
                "The file has been updated.",
            ),
            _bash(
                utc(2026, 10, 21, 19, 20), f"git -C {wt} push -u origin {branch}", "set up to track"
            ),
            _bash(utc(2026, 10, 21, 19, 21), f"gh pr create --repo {slug} --head {branch}", url),
        ],
        start=utc(2026, 10, 21, 19, 0),
        last_seen=utc(2026, 10, 21, 20, 30),
        end=(utc(2026, 10, 21, 20, 31), "prompt_input_exit"),
    )

    question = f"were we working #{num}, or was that the other session?"
    view = f"title: tests: unbreak main\nauthor: {GH_AUTHOR}\nstate: OPEN\nurl: {url}"
    asker_one = store.session(
        "own/asker-one",
        grove,
        [
            (utc(2026, 10, 21, 22, 0), "tidy the asset loader"),
            (utc(2026, 10, 22, 3, 26, 56), question),
        ],
        [_bash(utc(2026, 10, 22, 3, 27, 20), f"gh pr view {num}", view)],
        start=utc(2026, 10, 21, 22, 0),
        usage_peak=180_000,
        last_seen=utc(2026, 10, 22, 4, 0),
    )
    asker_two = store.session(
        "own/asker-two",
        grove,
        [
            (utc(2026, 10, 22, 3, 0), "look at the save-file migration"),
            (utc(2026, 10, 22, 3, 27, 5), question),
        ],
        [
            _bash(utc(2026, 10, 22, 3, 27, 30), f"gh pr view {num}", view),
            _bash(
                utc(2026, 10, 22, 3, 28),
                "claude-ls who",
                f"{owner.sid8}  grove  {branch}  opened #{num} (ended)",
            ),
        ],
        start=utc(2026, 10, 22, 3, 0),
        last_seen=utc(2026, 10, 22, 3, 40),
    )

    fresh = store.session(
        "own/fresh",
        grove,
        [(utc(2026, 11, 2, 16, 30), "is the tests fix still going?")],
        [_bash(utc(2026, 11, 2, 16, 31), f"gh pr view {num} --repo {slug}", view)],
        start=utc(2026, 11, 2, 16, 30),
    )

    pr_ref = f"gh:{slug}#{num}"
    return {
        "name": "pr-ownership",
        "sessions": {"owner": owner, "asker_one": asker_one, "asker_two": asker_two},
        "fresh": fresh,
        "repos": {"grove": grove},
        "worktrees": {"fix-main-tests": wt},
        "workstreams": [
            {
                "name": "main-tests",
                "title": "Unbreak grove main tests",
                "anchors": [pr_ref, f"branch:grove@{branch}", f"wt:{wt}"],
                "canonical": [owner],
                "ancillary": [],
            },
            {
                "name": "asset-loader",
                "title": "Grove asset loader tidy",
                "anchors": [],
                "canonical": [asker_one],
                "ancillary": [],
            },
            {
                "name": "save-migration",
                "title": "Grove save-file migration",
                "anchors": [],
                "canonical": [asker_two],
                "ancillary": [],
            },
        ],
        "expected": "main-tests",
        "loose_end": {"kind": "owner", "ref": pr_ref, "session": owner.id, "branch": branch},
        "gh": {
            f"{slug}#{num}": _gh_pr(slug, num, "OPEN", branch, utc(2026, 10, 21, 19, 21)),
        },
    }


def case_duplicate_bug(store: SyntheticStore) -> dict:
    """Two sessions on one symptom; the first's fix is stranded in a worktree
    of a repo with no remote, and that session was dropped."""
    kiln = store.repo("kiln", remote=False)
    branch = "wt/fix-default-track"
    message = "pick the original audio track, not the dubbed one"
    wt = store.worktree(kiln, "fix-default-track", message=message)
    sha = store.worktrees[wt]["commit"]

    first = store.session(
        "dup/first",
        kiln,
        [(utc(2026, 10, 31, 0, 29), "exported clips sound sped up and in the wrong language")],
        [
            (
                utc(2026, 10, 31, 0, 31),
                "EnterWorktree",
                {"name": "fix-default-track"},
                {"worktreePath": str(wt), "worktreeBranch": branch, "message": "Entered worktree"},
            ),
            (
                utc(2026, 10, 31, 0, 50),
                "Edit",
                {
                    "file_path": str(wt / "src/fix_default_track.py"),
                    "old_string": "",
                    "new_string": "FIXED = True\n",
                },
                "The file has been updated.",
            ),
            _bash(
                utc(2026, 10, 31, 0, 55),
                f"git -C {wt} commit -am '{message}'",
                f"[{branch} {sha[:7]}] {message}",
            ),
        ],
        start=utc(2026, 10, 31, 0, 29),
        usage_peak=140_000,
        last_seen=utc(2026, 11, 2, 5, 59),
    )
    # Resumed once and never ended: the last lifecycle event is a SessionStart.
    store.hook("SessionStart", first, utc(2026, 11, 2, 5, 58), source="resume", model=MODEL)

    second = store.session(
        "dup/second",
        kiln,
        [(utc(2026, 10, 31, 0, 37), "clips export too fast and dubbed; why?")],
        [
            (utc(2026, 10, 31, 0, 38), "Read", {"file_path": str(kiln / "README.md")}, "# kiln"),
            _bash(
                utc(2026, 10, 31, 0, 40, 46),
                "claude-ls who",
                f"Another live session ({first.sid8}) already has a fix-default-track worktree "
                f"open on this exact bug: {wt} ({branch})",
            ),
        ],
        start=utc(2026, 10, 31, 0, 37),
        end=(utc(2026, 10, 31, 0, 45), "prompt_input_exit"),
    )

    fresh = store.session(
        "dup/fresh",
        kiln,
        [(utc(2026, 11, 2, 17, 10), "the clips still sound wrong; did we ever fix that?")],
        [
            _bash(
                utc(2026, 11, 2, 17, 11),
                f"git -C {kiln} worktree list",
                f"{kiln}  {store.git(kiln, 'rev-parse', '--short', 'main')} [main]\n"
                f"{wt}  {sha[:7]} [{branch}]",
            )
        ],
        start=utc(2026, 11, 2, 17, 10),
    )

    wt_ref = f"wt:{wt}"
    return {
        "name": "duplicate-bug",
        "sessions": {"first": first, "second": second},
        "fresh": fresh,
        "repos": {"kiln": kiln},
        "worktrees": {"fix-default-track": wt},
        "workstreams": [
            {
                "name": "default-track",
                "title": "Exported clips use the dubbed track",
                "anchors": [wt_ref, f"branch:kiln@{branch}"],
                "canonical": [first],
                "ancillary": [second],
                # part-of children in weft; `depends_on` indexes this list.
                "tasks": [
                    {"title": "Reproduce the dubbed-track export", "state": "done"},
                    {"title": "Land the stranded default-track fix", "state": "open"},
                    {
                        "title": "Add an export regression test",
                        "state": "open",
                        "depends_on": 1,
                    },
                    {"title": "Re-encode clips as a workaround", "state": "dropped"},
                ],
            }
        ],
        "expected": "default-track",
        "loose_end": {
            "kind": "unmerged_commit",
            "ref": wt_ref,
            "commit": sha,
            "branch": branch,
            "session": first.id,
            "remote": None,
        },
        "gh": {},
    }


def case_cross_repo_merge(store: SyntheticStore) -> dict:
    """A session rooted in bramble merges meadow PRs while a live meadow
    session owns their readiness."""
    meadow = store.repo("meadow")
    bramble = store.repo("bramble")
    slug = f"{GH_OWNER}/meadow"
    head_319 = "feat/origin-319"
    store.checkout(meadow, head_319)  # left checked out in the main checkout, as in the case
    desktop = store.clone(meadow, store.root / "elsewhere" / "meadow-desktop")

    owner_sibling = store.session(
        "merge/readiness",
        meadow,
        [(utc(2026, 10, 30, 23, 10), "get the origin PRs merge-ready")],
        start=utc(2026, 10, 30, 23, 10),
        end=(utc(2026, 10, 30, 23, 16), "other"),
    )
    owner = store.session(
        "merge/readiness-fork",
        meadow,
        [(utc(2026, 10, 30, 23, 18), "carry on: own merge readiness for the origin PRs")],
        [
            _bash(utc(2026, 10, 31, 0, 5), f"gh pr ready 318 --repo {slug}", pr_url(slug, 318)),
            _bash(utc(2026, 10, 31, 0, 6), f"gh pr ready 319 --repo {slug}", pr_url(slug, 319)),
            _bash(
                utc(2026, 10, 31, 0, 8),
                f"gh pr edit 319 --repo {slug} --add-reviewer reviewer-two",
                pr_url(slug, 319),
            ),
            _bash(utc(2026, 10, 31, 0, 12), f"git -C {meadow} switch {head_319}", "Switched"),
        ],
        start=utc(2026, 10, 30, 23, 17),
        fork_of=owner_sibling,
        start_source="fork",
        usage_peak=360_000,
        last_seen=utc(2026, 11, 2, 14, 40),
    )
    store.register(owner, "meadow-b2", 41101, status="busy")

    lookup = store.session(
        "merge/lookup",
        meadow,
        [(utc(2026, 11, 2, 5, 14), "find the bramble sessions for the unit callouts work")],
        [
            _bash(
                utc(2026, 11, 2, 5, 16),
                "claude-ls who",
                "bramble: two candidate sessions; trellis: two candidate sessions",
            )
        ],
        start=utc(2026, 11, 2, 5, 14),
        end=(utc(2026, 11, 2, 5, 40), "other"),
    )

    merges = [(310, utc(2026, 11, 2, 12, 10)), (313, utc(2026, 11, 2, 12, 40))]
    merges += [(316, utc(2026, 11, 2, 13, 20)), (319, utc(2026, 11, 2, 14, 28))]
    merger = store.session(
        "merge/merger",
        bramble,
        [
            (utc(2026, 11, 2, 3, 18), "render placeholder unit callouts for the meadow game"),
            (utc(2026, 11, 2, 14, 33, 6), "why are you merging"),
            (utc(2026, 11, 2, 14, 33, 55), "wasn't the other session on it?"),
        ],
        [
            (
                utc(2026, 11, 2, 3, 30),
                "Edit",
                {
                    "file_path": str(bramble / "callouts/units.py"),
                    "old_string": "",
                    "new_string": "X\n",
                },
                "The file has been updated.",
            ),
            *[
                _bash(
                    at,
                    f"gh pr merge {n} --repo {slug} --squash",
                    f"Squashed and merged pull request {slug}#{n}\n{pr_url(slug, n)}",
                )
                for n, at in merges
            ],
        ],
        start=utc(2026, 11, 2, 3, 18),
        usage_peak=150_000,
        last_seen=utc(2026, 11, 2, 14, 40),
    )
    store.register(merger, "bramble-bb", 41102)
    msg_id = store.send_message(
        merger,
        owner,
        f"I merged {slug}#310, #313, #316 and #319 from a bramble session. "
        "Handing meadow merges back to you.",
        utc(2026, 11, 2, 14, 36, 10),
    )

    codex_main = store.codex(
        "merge/codex-desktop",
        desktop,
        ["check the callout assets render in meadow"],
        ["git status", (utc(2026, 11, 2, 9, 3), "rg -n callout src", "src/units.py:3: callout")],
        start=utc(2026, 11, 2, 9, 0),
    )
    codex_guard = store.codex(
        "merge/codex-guardian",
        desktop,
        ["guard: review shell commands before they run"],
        ["git diff --stat"],
        start=utc(2026, 11, 2, 9, 5),
        parent=codex_main,
        source="exec",
    )

    fresh = store.session(
        "merge/fresh",
        meadow,
        [(utc(2026, 11, 2, 18, 0), "what's left on the origin PRs?")],
        [
            _bash(
                utc(2026, 11, 2, 18, 1),
                f"gh pr list --repo {slug}",
                f"318\torigin follow-up\tOPEN\n{pr_url(slug, 318)}",
            )
        ],
        start=utc(2026, 11, 2, 18, 0),
    )

    gh = {
        f"{slug}#{n}": _gh_pr(
            slug, n, "MERGED", f"feat/origin-{n}", at, merged=at, decision="APPROVED"
        )
        for n, at in merges
    }
    gh[f"{slug}#318"] = _gh_pr(
        slug,
        318,
        "OPEN",
        "feat/origin-318",
        utc(2026, 10, 31, 0, 5),
        decision="REVIEW_REQUIRED",
        requested=["reviewer-two"],
    )
    gh[f"{slug}#319"]["reviewRequests"] = [{"login": "reviewer-two"}]
    gh[f"{slug}#319"]["reviewDecision"] = "REVIEW_REQUIRED"

    return {
        "name": "cross-repo-merge",
        "sessions": {
            "owner_sibling": owner_sibling,
            "owner": owner,
            "lookup": lookup,
            "merger": merger,
            "codex": codex_main,
            "codex_guardian": codex_guard,
        },
        "fresh": fresh,
        "repos": {"meadow": meadow, "bramble": bramble, "meadow-desktop": desktop},
        "worktrees": {},
        "messages": {"handover": msg_id},
        "workstreams": [
            {
                "name": "origin-readiness",
                "title": "Origin PRs merge readiness",
                "anchors": [f"gh:{slug}#318", f"gh:{slug}#319"],
                "canonical": [owner],
                "ancillary": [owner_sibling, lookup],
            },
            {
                "name": "unit-callouts",
                "title": "Placeholder unit callouts",
                "anchors": [],
                "canonical": [merger],
                "ancillary": [codex_main],
            },
        ],
        "expected": "origin-readiness",
        "loose_end": {
            "kind": "foreign_merge",
            "ref": f"gh:{slug}#319",
            "by": merger.id,
            "owner": owner.id,
            "at": merges[-1][1],
        },
        "gh": gh,
    }


def case_sync(store: SyntheticStore) -> dict:
    """A session that wrote to a PR, a branch and a worktree without ever
    naming them as anchors, beside the weak signals sync must not turn into
    anchors: a `gh pr list` that prints other PRs, a read of a second repo,
    and that repo's PR named in a prompt. A reader session in another
    workstream only looks at the same repo.

    Not in CASES: build_all feeds the acceptance and CLI fixtures, whose
    candidate lists this case's sessions would change."""
    loom = store.repo("loom", remote=False)
    quarry = store.repo("quarry", remote=False)
    # GitHub-shaped origins, never fetched: they name `owner/repo` for a bare
    # `gh pr merge 31`, the way a real checkout's origin does.
    store.git(loom, "remote", "add", "origin", f"git@github.com:{GH_OWNER}/loom.git")
    store.git(quarry, "remote", "add", "origin", f"git@github.com:{GH_OWNER}/quarry.git")
    branch = "sync-seam"
    message = "loom: accrue anchors from evidence"
    wt = store.worktree(loom, "sync-seam", branch=branch, message=message)
    sha = store.worktrees[wt]["commit"]
    slug, other = f"{GH_OWNER}/loom", f"{GH_OWNER}/quarry"
    pr, listed, reviewed, mentioned, foreign, noted = 31, (29, 30), 27, 28, 7, 12
    docs_branch, notes_branch = "sync-docs", "sync-notes"
    pr_list = json.dumps([{"url": pr_url(slug, n)} for n in listed])

    worker = store.session(
        "sync/worker",
        loom,
        [
            (
                utc(2026, 11, 1, 18, 0),
                "anchors only accrue on attach --anchor; make them follow the work",
            ),
            (
                utc(2026, 11, 1, 19, 30),
                f"also fold in what {slug}#{mentioned} was after, and ignore {other}#{foreign}",
            ),
        ],
        [
            (
                utc(2026, 11, 1, 18, 2),
                "EnterWorktree",
                {"name": "sync-seam"},
                {"worktreePath": str(wt), "worktreeBranch": branch, "message": "Entered worktree"},
            ),
            (
                utc(2026, 11, 1, 18, 20),
                "Edit",
                {
                    "file_path": str(wt / "src/sync_seam.py"),
                    "old_string": "",
                    "new_string": "FIXED = True\n",
                },
                "The file has been updated.",
            ),
            _bash(
                utc(2026, 11, 1, 18, 25),
                f"git -C {wt} commit -am '{message}'",
                f"[{branch} {sha[:7]}] {message}\n 1 file changed, 2 insertions(+)",
            ),
            _bash(
                utc(2026, 11, 1, 18, 26),
                f"git -C {wt} push -u origin {branch}",
                f"To github.com:{slug}.git\n * [new branch]      {branch} -> {branch}\n"
                f"branch '{branch}' set up to track 'origin/{branch}'.",
            ),
            _bash(
                utc(2026, 11, 1, 18, 28),
                f'cd {wt} && gh pr create --title "{message}" --body "Anchors from evidence."',
                pr_url(slug, pr),
            ),
            _bash(utc(2026, 11, 1, 18, 40), f"gh pr list --repo {slug} --json url", pr_list),
            _bash(
                utc(2026, 11, 1, 18, 45),
                f"gh api repos/{slug}/pulls/{reviewed}/reviews -f event=APPROVE",
                '{"state": "APPROVED"}',
            ),
            _bash(utc(2026, 11, 1, 18, 50), f"git -C {quarry} log --oneline -3", "abc1234 initial"),
            _bash(
                utc(2026, 11, 1, 18, 55),
                f"git -C {loom} push origin main",
                f"To github.com:{slug}.git\n   1a2b3c4..5d6e7f8  main -> main",
            ),
            _bash(
                utc(2026, 11, 1, 19, 0),
                f'cd {wt} && gh pr merge {pr} --squash --subject "loom: say \\"why\\" (#{pr})"'
                " 2>&1 | tail -1",
                f"✓ Squashed and merged pull request #{pr} ({message})",
            ),
            # A heredoc whose body reads like commands and PR refs, then a
            # quiet push: only the push after the delimiter is a command.
            _bash(
                utc(2026, 11, 1, 19, 5),
                f"cat > {wt}/NOTES.md <<'EOF'\n"
                "next: git push origin junk\n"
                f'see {slug}#{noted} and "{GH_OWNER}/bramble",#319\n'
                "EOF\n"
                f"git -C {wt} push -q origin {docs_branch} 2>/dev/null",
                "",
            ),
            # Output cut to its last line, so the refspec is all there is.
            _bash(
                utc(2026, 11, 1, 19, 10),
                f"git -C {wt} push -u origin {notes_branch} 2>&1 | tail -1",
                f"branch '{notes_branch}' set up to track 'origin/{notes_branch}'.",
            ),
        ],
        start=utc(2026, 11, 1, 18, 0),
    )

    reader = store.session(
        "sync/reader",
        quarry,
        [(utc(2026, 11, 1, 20, 0), f"what did {pr_url(slug, pr)} change?")],
        [
            _bash(
                utc(2026, 11, 1, 20, 1), f"git -C {loom} log --oneline -5", f"{sha[:7]} {message}"
            ),
            _bash(utc(2026, 11, 1, 20, 2), f"gh pr view {pr} --repo {slug}", message),
        ],
        start=utc(2026, 11, 1, 20, 0),
    )

    return {
        "name": "sync",
        "sessions": {"worker": worker, "reader": reader},
        "repos": {"loom": loom, "quarry": quarry},
        "worktrees": {"sync-seam": wt},
        "slug": slug,
        "expected": {
            f"wt:{wt}",
            f"branch:loom@{branch}",
            f"gh:{slug}#{pr}",
            f"gh:{slug}#{reviewed}",
            f"gh:{slug}#{mentioned}",
            f"branch:loom@{docs_branch}",
            f"branch:loom@{notes_branch}",
        },
        "never": {
            *(f"gh:{slug}#{n}" for n in listed),
            f"gh:{other}#{foreign}",
            "branch:loom@main",
            "branch:loom@junk",
            f"gh:{slug}#{noted}",
            f"gh:{GH_OWNER}/bramble#319",
            f"wt:{loom}",
            f"wt:{quarry}",
        },
    }


def case_two_claims(store: SyntheticStore) -> dict:
    """One session, two workstreams in turn: canonical on the bobbin rewrite
    from 09:05 until it releases at 12:00, then canonical on the spool cache
    from 13:00 on. Each stint opens a PR, commits on a branch, spawns a
    subagent and messages a helper session, which is ancillary on the spool
    cache. Claim times are returned for hand-built Workstreams: weft stamps a
    claim with the wall clock, which a fixture cannot set.

    Not in CASES, for the same reason as case_sync."""
    spindle = store.repo("spindle", remote=False)
    store.git(spindle, "remote", "add", "origin", f"git@github.com:{GH_OWNER}/spindle.git")
    slug = f"{GH_OWNER}/spindle"
    early_pr, late_pr = 41, 52
    early_branch, late_branch = "bobbin-rewrite", "spool-cache"
    day = (2026, 10, 30)

    helper = store.session(
        "two/helper",
        spindle,
        [(utc(*day, 10, 15), "read the bobbin diff when it lands")],
        start=utc(*day, 10, 15),
        last_seen=utc(*day, 14, 30),
    )
    store.register(helper, "spindle-h1", 42001)
    worker = store.session(
        "two/worker",
        spindle,
        [
            (utc(*day, 9, 0), "rewrite the bobbin loader"),
            (utc(*day, 13, 0), "now add the spool cache"),
        ],
        [
            _bash(
                utc(*day, 9, 30),
                f'cd {spindle} && gh pr create --title "bobbin rewrite" --body x',
                pr_url(slug, early_pr),
            ),
            _bash(
                utc(*day, 9, 40),
                f"git -C {spindle} commit -am 'bobbin: rewrite'",
                f"[{early_branch} 1a2b3c4] bobbin: rewrite\n 1 file changed",
            ),
            _bash(
                utc(*day, 13, 30),
                f'cd {spindle} && gh pr create --title "spool cache" --body x',
                pr_url(slug, late_pr),
            ),
            _bash(
                utc(*day, 13, 40),
                f"git -C {spindle} commit -am 'spool: cache'",
                f"[{late_branch} 5d6e7f8] spool: cache\n 1 file changed",
            ),
        ],
        start=utc(*day, 9, 0),
        last_seen=utc(*day, 14, 30),
    )
    store.register(worker, "spindle-w1", 42002)
    early_sub = store.subagent(
        worker, "two/early-survey", "survey the bobbin loader", start=utc(*day, 10, 0)
    )
    late_sub = store.subagent(
        worker, "two/late-survey", "survey the spool layer", start=utc(*day, 13, 50)
    )
    store.send_message(worker, helper, "Bobbin rewrite is up for a look.", utc(*day, 10, 30))
    store.send_message(worker, helper, "Spool cache is in.", utc(*day, 14, 0))
    return {
        "name": "two-claims",
        "sessions": {"worker": worker, "helper": helper},
        "subagents": {"early": early_sub, "late": late_sub},
        "slug": slug,
        "early": {f"gh:{slug}#{early_pr}", f"branch:spindle@{early_branch}"},
        "late": {f"gh:{slug}#{late_pr}", f"branch:spindle@{late_branch}"},
        "a_claim": utc(*day, 9, 5),
        "a_release": utc(*day, 12, 0),
        "b_claim": utc(*day, 13, 0),
    }


CASES = {
    "audio-seam": case_audio_seam,
    "pr-ownership": case_pr_ownership,
    "duplicate-bug": case_duplicate_bug,
    "cross-repo-merge": case_cross_repo_merge,
}


def build_all(store: SyntheticStore) -> dict[str, dict]:
    return {name: build(store) for name, build in CASES.items()}
