"""The graph model and builder.

The design is bipartite, not a session-to-session edge list: sessions are grouped
under each individual artifact they touched (a PR, a branch, a file), and a session
multi-belongs to every group it's part of — that multi-membership is the crossover
the graph exists to surface. Transitive connected-components collapse a monorepo into
one blob, so we never merge across artifacts (only *identical-membership* keys merge,
which is redundancy, not transitivity).

`build_session_graph` is a pure function over `SessionRecord`s — no filesystem, no
seam — so it's fully unit-testable. `load_session_record` (which does touch the seam)
is the bridge from a transcript file to a record.

Ported from `incoming/spike_graph.py` on the design branch.
"""

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import sessions

# Branches shared by nearly every session carry no link signal (mirrors claude-ls /
# the spikes). Kept here rather than imported so the builder stays seam-free.
DEFAULT_BRANCHES = frozenset({"main", "master", "HEAD", "", None})

# Which artifact heads a session's row when it belongs to several (EDA redundancy
# ranking: PR is the tightest deliberate unit, file the broad complement).
PRECEDENCE = {"pr": 0, "branch": 1, "file": 2}
TAG = {"pr": "PR", "branch": "branch", "file": "file"}


def real_branches(branches):
    """Link-bearing branches only — default/empty dropped."""
    return [b for b in branches if b not in DEFAULT_BRANCHES]


@dataclass
class SessionRecord:
    """One session node: the drawable signals the graph groups on, plus the
    metadata the renderer needs. `files` are absolute paths (the shared-file key)."""

    id: str
    prs: list = field(default_factory=list)  # "owner/repo#N"
    branches: list = field(default_factory=list)  # raw; default-filtered at build
    files: list = field(default_factory=list)  # worktree-canonical file keys (shared-file key)
    repos: list = field(
        default_factory=list
    )  # canonical repo roots (multi-belong; not a group key)
    start: str = ""
    end: str = ""
    segments: list = field(default_factory=list)  # [(start_iso, end_iso)] active bursts
    turns: int = 0
    first_user: str = ""  # first user message, truncated (a one-line "what was this?")
    subagents: dict = field(
        default_factory=dict
    )  # toolUseId -> {agentType, description, agent_file}
    origin: str = ""  # the root session this one was /cleared from ("" for a root)


@dataclass
class ArtifactGroup:
    """Sessions that share one artifact — or a set of artifacts with identical
    membership, collapsed into one group (label lists them, most-specific first)."""

    keys: list  # [(kind, value), ...], sorted most-specific-first
    members: list  # session ids, sorted

    @property
    def kind(self) -> str:
        return self.keys[0][0]

    @property
    def value(self):
        return self.keys[0][1]

    @property
    def precedence(self) -> int:
        return min(PRECEDENCE[k] for k, _ in self.keys)


@dataclass
class SessionGraph:
    groups: list  # ArtifactGroup, ordered
    records: dict  # id -> SessionRecord


def artifacts_of(rec: SessionRecord):
    """Every drawable artifact key a session touched: (kind, value)."""
    out = [("pr", p) for p in rec.prs]
    out += [("branch", b) for b in real_branches(rec.branches)]
    out += [("file", f) for f in rec.files]
    return out


def sprawl_score(rec: SessionRecord) -> float:
    """A per-session tangle heuristic (cross-repo breadth dominant, size terms
    sqrt-damped). No longer ranks the scan — that saturated into ties and buried
    real hubs (see `group_weight`). Kept as a session-level signal for the digest
    ("most cross-cutting session"). Simplified from the design's atom 5.
    """
    return round(
        4.0 * max(0, len(rec.repos) - 1)
        + 1.0 * max(0, len(real_branches(rec.branches)) - 1)
        + 1.0 * len(rec.prs)
        + 1.0 * math.sqrt(len(rec.files))
        + 0.2 * math.sqrt(max(0, rec.turns)),
        2,
    )


# Scan-ranking weights. A shared PR is a tighter, more deliberate signal than a
# shared branch, which beats an incidentally co-edited file — so a group's weight
# scales its session count by the anchoring artifact's kind. Validated against the
# live store: this leads with the real multi-session hub, where the old max-sprawl
# sort ranked it 32nd.
KIND_PRIOR = {"pr": 1.0, "branch": 0.7, "file": 0.5}

# A file group made *entirely* of workspace furniture — memory/handoff/notes docs
# and dotfiles many sessions touch incidentally — is penalized rather than counted
# as shared work. Identity-based (what the file is), not frequency-based: a widely
# shared *code* file is a genuine hub and must not be caught here. The spike showed
# frequency (IDF) can't tell a 7-session hub from a 7-session MEMORY.md; kind can.
SCAFFOLD_PENALTY = 0.2
_SCAFFOLD_BASENAMES = frozenset(
    {
        "MEMORY.md",
        "CLAUDE.md",
        "README.md",
        "key_facts.md",
        "decisions.md",
        "open-threads.md",
        "bugs.md",
        "findings.md",
        "notes.md",
        "TODO.md",
        "justfile",
    }
)
_SCAFFOLD_PATH_HINTS = ("project_notes", "handoff", "/notes/")


def _is_scaffold_file(path: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    low = path.lower()
    return (
        base in _SCAFFOLD_BASENAMES
        or base.startswith(".")  # dotfiles: .gitignore, .let-go-version, …
        or any(h in low for h in _SCAFFOLD_PATH_HINTS)
    )


def is_scaffold_group(group: "ArtifactGroup") -> bool:
    """A file group made entirely of workspace furniture. A group anchored by a
    PR/branch is never scaffolding."""
    return group.kind == "file" and all(_is_scaffold_file(v) for _, v in group.keys)


def group_weight(group: "ArtifactGroup") -> float:
    """Scan-ranking weight: sessions shared, scaled by the anchoring artifact's kind
    (PR > branch > file), with pure-scaffolding file groups penalized. Higher leads
    the map."""
    w = KIND_PRIOR[group.kind] * len(group.members)
    if is_scaffold_group(group):
        w *= SCAFFOLD_PENALTY
    return w


def build_session_graph(records, *, hub: int = 8) -> SessionGraph:
    """Bucket sessions under each artifact, keep real groups, merge identical-
    membership keys. Pure — no seam, no filesystem.

    - keep groups with >=2 members
    - hub-suppress `file` keys touched by more than `hub` sessions (real but too
      broad to draw — one file edited across a whole project)
    - fileset-merge: keys with an identical member-set collapse into one group,
      across kinds too (identical membership is redundant regardless of type)
    """
    records = list(records)
    by_id = {r.id: r for r in records}

    buckets: dict = {}  # (kind, value) -> set(ids)
    for r in records:
        for key in artifacts_of(r):
            buckets.setdefault(key, set()).add(r.id)

    kept = {
        key: ids
        for key, ids in buckets.items()
        if len(ids) >= 2 and not (key[0] == "file" and len(ids) > hub)
    }

    by_members: dict = {}  # frozenset(ids) -> [keys]
    for key, ids in kept.items():
        by_members.setdefault(frozenset(ids), []).append(key)

    groups = []
    for members, keys in by_members.items():
        keys_sorted = sorted(keys, key=lambda k: (PRECEDENCE[k[0]], str(k[1])))
        groups.append(ArtifactGroup(keys=keys_sorted, members=sorted(members)))

    # Deterministic order: precedence (PR<branch<file), then larger groups, then value.
    # Sprawl-led ordering arrives with the renderer (PR 4).
    groups.sort(key=lambda g: (g.precedence, -len(g.members), str(g.value)))
    return SessionGraph(groups=groups, records=by_id)


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _rel_rooted_key(path: str, wt_root, canon_root) -> str:
    """Pure: a file's identity as ``<canon_root>/<path-relative-to-wt_root>``.

    ``wt_root`` is the file's own git root (the *worktree* root for a linked
    worktree); ``canon_root`` is that worktree's canonical *main* repo. Re-rooting the
    worktree-relative path onto the main repo collapses every worktree copy of a file
    onto one identity. Falls back to the raw path when either root is missing or the
    path isn't under its worktree (identity unrecoverable — keep it distinct).
    """
    if wt_root and canon_root and path.startswith(wt_root):
        rel = path[len(wt_root) :].lstrip("/")
        return f"{canon_root}/{rel}"
    return path


_FILE_KEY_CACHE: dict = {}  # raw path -> canonical key (paths recur across sessions)


def _canonical_file_key(path: str) -> str:
    """Worktree-stable file identity (see `_rel_rooted_key`). Touches the seam, so it
    lives here rather than in the pure builder; cached because one file is edited
    across many sessions."""
    if path not in _FILE_KEY_CACHE:
        from . import sessions

        _FILE_KEY_CACHE[path] = _rel_rooted_key(
            path, sessions.repo_root_for(path), sessions.canonical_repo_root(path)
        )
    return _FILE_KEY_CACHE[path]


def disambiguate_basenames(paths) -> dict:
    """Map each file-key path to the shortest trailing path-suffix that is unique among
    paths sharing its basename. Unique basenames map to the bare basename; collisions
    (the same filename in genuinely different repos — post worktree-merge) get just
    enough leading segments to tell them apart, e.g. `xsofy/README.md` vs
    `let-go/README.md`."""
    by_base: dict = {}
    for p in set(paths):
        by_base.setdefault(_basename(p), []).append(p)
    out: dict = {}
    for base, group in by_base.items():
        if len(group) == 1:
            out[group[0]] = base
            continue
        segs = {p: p.split("/") for p in group}
        for p in group:
            parts = segs[p]
            chosen = base
            for k in range(2, len(parts) + 1):  # basename already collides; add parents
                suffix = "/".join(parts[-k:])
                if sum(1 for q in group if "/".join(segs[q][-k:]) == suffix) == 1:
                    chosen = suffix
                    break
            out[p] = chosen
    return out


def group_label_segments(group: ArtifactGroup, *, file_display=None, max_vals=None):
    """The label broken into `(kind, text)` segments so the renderer can color each by
    kind. Most-specific (PR) first. A merged fileset renders as `N files (a, b, …)`.
    `file_display` optionally overrides the shown name per file key (see
    `disambiguate_basenames`); absent, files show their basename. `max_vals` caps the
    values listed per kind (`PR a#1, a#2 +4`) for space-tight contexts."""
    fd = file_display or {}
    segments = []
    for kind in ("pr", "branch", "file"):
        vals = [v for k, v in group.keys if k == kind]
        if not vals:
            continue
        if kind == "file":
            names = [fd.get(v) or _basename(v) for v in vals]
            if len(names) == 1:
                segments.append((kind, f"file {names[0]}"))
            else:
                cap = min(3, max_vals) if max_vals else 3
                shown = ", ".join(names[:cap]) + (", …" if len(names) > cap else "")
                segments.append((kind, f"{len(names)} files ({shown})"))
        else:
            shown_vals = vals[:max_vals] if max_vals else vals
            text = ", ".join(f"{TAG[kind]} {v}" for v in shown_vals)
            if len(vals) > len(shown_vals):
                text += f" +{len(vals) - len(shown_vals)}"
            segments.append((kind, text))
    return segments


def group_label(group: ArtifactGroup, *, file_display=None) -> str:
    """Human label: one segment per kind, most-specific (PR) first (see
    `group_label_segments`)."""
    return " · ".join(seg for _, seg in group_label_segments(group, file_display=file_display))


def _first_user_text(content, limit: int = 160) -> str:
    """First human line from a user message's content (str or block list), squished.
    Skips tool_result blocks so we get the actual prompt, not a tool echo, and returns
    "" for a synthetic wrapper (slash/`!` command scaffolding or a background-task
    notification) so the caller falls through to the real prompt."""
    text = ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        for b in content:
            if isinstance(b, str):
                text = b
                break
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                text = b["text"]
                break
    text = " ".join(text.split())
    # Claude Code injects synthetic "user" messages that aren't the human's prompt
    # (slash-command scaffolding, `!`-bash echoes, task notifications). Returning ""
    # lets the caller fall through to real prose. The vocabulary lives in claude-ls
    # and reaches us through the seam, so there's one copy to keep true.
    if sessions.is_wrapper_text(text):
        return ""
    return text[:limit]


def parse_ts(iso: str):
    """ISO timestamp -> AWARE datetime, tolerant of the store's trailing 'Z'.
    A naive input (fixtures, synthetic data) is taken as local time — everything
    downstream compares datetimes, and one naive stamp would poison every
    comparison. Returns None on garbage."""
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.astimezone()


# A session left open overnight isn't *working* overnight. Entry timestamps split into
# activity segments on this idle gap, so concurrency means "both actively working",
# not "both windows open". 30min ≈ the point where a lull reads as walked-away.
SEGMENT_GAP_MINUTES = 30


def activity_segments(timestamps, gap_minutes: int = SEGMENT_GAP_MINUTES) -> list:
    """Gap-split sorted ISO timestamps into [(start_iso, end_iso)] active bursts.
    Pure; input order doesn't matter."""
    stamped = sorted((t, iso) for iso in timestamps if (t := parse_ts(iso)) is not None)
    if not stamped:
        return []
    gap = timedelta(minutes=gap_minutes)
    segs = []
    seg_start, prev = stamped[0], stamped[0]
    for cur in stamped[1:]:
        if cur[0] - prev[0] > gap:
            segs.append((seg_start[1], prev[1]))
            seg_start = cur
        prev = cur
    segs.append((seg_start[1], prev[1]))
    return segs


def load_session_record(path) -> "SessionRecord | None":
    """Build a SessionRecord from a session transcript file (uses the seam)."""
    import json

    from . import sessions
    from .subagents import find_subagent_sessions

    try:
        text = Path(path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    entries = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    if not entries:
        return None

    ws = sessions.derive_work_signals(entries)
    meta = sessions.extract_session_metadata(entries)

    # Canonical repos (worktree-normalized) from the RAW cwds the session recorded —
    # a session multi-belongs to every distinct repo it touched (Round 4).
    repos, seen = [], set()
    turns = 0
    first_user = ""
    raw_cwds, seen_cwd = [], set()
    timestamps = []
    for e in entries:
        if e.get("type") == "user" and isinstance(e.get("message"), dict):
            turns += 1
            if not first_user:
                first_user = _first_user_text(e["message"].get("content"))
        ts = e.get("timestamp")
        if ts:
            timestamps.append(ts)
        c = e.get("cwd")
        if c and c not in seen_cwd:
            seen_cwd.add(c)
            raw_cwds.append(c)
    for c in raw_cwds:
        root = sessions.canonical_repo_root(c)
        if root and root not in seen:
            seen.add(root)
            repos.append(root)

    # Worktree-canonical file keys, de-duped: sibling worktrees of one repo edit the
    # same logical file at different absolute paths — collapse them to one key so they
    # form a single group rather than N look-alike ones.
    files, seen_f = [], set()
    for p in ws.edited_paths:
        k = _canonical_file_key(p)
        if k not in seen_f:
            seen_f.add(k)
            files.append(k)

    return SessionRecord(
        id=Path(path).stem,
        prs=list(ws.prs),
        branches=list(ws.branches),
        files=files,
        repos=repos,
        start=meta.start_time.isoformat() if meta.start_time else "",
        end=meta.end_time.isoformat() if meta.end_time else "",
        segments=activity_segments(timestamps),
        turns=turns,
        first_user=first_user,
        subagents=find_subagent_sessions(path),
        # `/clear` keeps the pane and starts a new transcript that names the root
        # session on every record; the timeline draws the chain as one lane.
        origin=getattr(meta, "origin_session_id", "") or "",
    )
