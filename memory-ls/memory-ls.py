#!/usr/bin/env -S uv run --quiet --with rich python3
"""Scan Claude Code's auto-memory store for advice that has gone wrong.

Not a tidiness linter. Scoped against a real store (50 projects, 327 entries),
most structural checks reported nothing worth acting on: wikilinks resolve 91%
of the time and the rest are forward references the write rules explicitly
allow, duplicate descriptions were zero, and frontmatter was valid everywhere
but one file. What the data did turn up is *wrong advice* — a fifth of the
absolute paths memories cite no longer exist, and a third of all entries cite a
`#N` whose state nobody has rechecked since it was written.

So the checks are dead paths, stale `#N` references, index drift, and facts
written into the index rather than into entries — the last two because they are
cheap and because the index is the part loaded unconditionally. Read-only: only
index drift is safely mechanical, and the checks that matter need judgment.

The reference check is the only one that leaves the machine, so it is opt-in
behind `--online`. A local run still counts the references it did not check and
says so, because a scan that silently skips a third of the entries reads like a
clean bill of health.

## The on-disk schema this reads

Claude Code's, not ours — undocumented, and free to change under us. What the
code assumes, in one place so it can be re-checked against a real store:

    ~/.claude/projects/                     store root
      <flattened-cwd>/                      one dir per project; '/' -> '-'
        sessions-index.json                 has `originalPath`: the real cwd
        <session-uuid>.jsonl                transcripts; each line has `cwd`
        memory/
          MEMORY.md                         index, loaded into context each session
          <slug>.md                         one entry, one fact

An entry opens with frontmatter carrying `name`, `description`, and a nested
`metadata.type` of user/feedback/project/reference; the body links to other
entries as `[[name]]`, resolved by the `name` field rather than the filename.
The index lists entries as `- [Title](file.md) — hook`.

Two consequences the checks lean on. The flattened directory name is lossy —
'-' is either a separator or a literal hyphen — which is why the recorded path
is preferred over reversing it. And `MEMORY.md` lives *inside* `memory/`, not
beside it; looking in the wrong place reports every entry as unlinked.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from rich.console import Console
from rich.table import Table
from rich.text import Text

MEMORY_DIRNAME = "memory"
INDEX_NAME = "MEMORY.md"

# Absolute paths as they appear in prose: `~/...` or a literal `/Users/<name>/...`.
# The character class stops at whitespace, backticks, quotes and markdown
# punctuation so a path at the end of a sentence doesn't swallow the period. It
# deliberately *admits* glob and placeholder characters so TEMPLATE_MARKERS can
# reject them: stopping before the `*` in `~/backups/foo-*.bundle` would leave a
# concrete-looking prefix that stats as dead.
PATH_RE = re.compile(r"""(?<![\w/.~-])(?:~|/Users/[A-Za-z0-9._-]+)(?:/[^\s`'"()\[\],;:?|]+)+""")

# Inline code spans, which is where most cited paths live — and the only form
# that survives a space in a directory name, since PATH_RE has to stop at
# whitespace. `~/Library/Application Support/...` is a real path and a false
# "dead path" if read with the bare regex.
CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")

# Brace expansions, ellipses and placeholders are illustrative spellings, not
# paths anyone can stat. They were a third of naive matches in the survey, and
# every one of them would have reported as dead.
TEMPLATE_MARKERS = ("{", "}", "…", "...", "<", ">", "*", "$")

# Trailing punctuation that belongs to the sentence rather than the path.
PATH_TRAILING = ".,;:)"

# `#123`. The lookbehind keeps `##` headings and `foo#1` out. It does *not*
# exclude six-digit hex colors — `#123456` in a CSS snippet reads as a
# reference — which is the same rare, accepted noise as an ordinal written
# `Open Question #1`. A number that doesn't exist simply resolves to nothing.
ISSUE_RE = re.compile(r"(?<![\w#])#(\d{1,6})\b")

# `owner/repo#123` or `owner/repo #123` — a reference to some *other* repo, 6%
# of the references in the survey. These have to be resolved against the repo
# they name: looking `upstream/proj #123` up in the project's own origin either
# 404s or, worse, finds an unrelated #123 and reports its title as fact.
#
# Tight on purpose, because a false match here does more than waste a lookup: the
# matched span is blanked before the bare sweep, so a wrongly-qualified number
# never gets checked against the project's own repo either. The owner pattern is
# GitHub's real rule (alphanumeric and hyphens, no dots or underscores), the repo
# may not end in a dot, both need two characters, and at most one space may
# separate them from the `#`. In the surveyed store that admits all 47 genuine
# references — 42 written closed-up, 5 with a space — while rejecting the two
# prose accidents it used to match, `Baseline w/o #522` and `~2-3 min/PR.  #581`.
# The lookbehind is PATH_RE's: without it the tail of a path picks up a
# following number, and `~/docs/notes.md #12` reads as owner `docs`, repo
# `notes.md`.
QUALIFIED_ISSUE_RE = re.compile(
    r"(?<![\w/.~-])([A-Za-z0-9][A-Za-z0-9-]+)/([A-Za-z0-9_-][A-Za-z0-9._-]*[A-Za-z0-9_-])"
    r" ?#(\d{1,6})\b"
)

# Markdown link targets in the index: `- [Title](file.md) — hook`.
INDEX_LINK_RE = re.compile(r"\]\(([^)]+\.md)\)")

# GitHub remotes, SSH or HTTPS. One store has `git@github.com/owner/name`, with a
# slash where the colon belongs, so the separator has to accept both.
REMOTE_RE = re.compile(r"github\.com[:/]+([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+?)(?:\.git)?/?$")

# Aliases per GraphQL query. Well under GitHub's node limit, and it keeps a
# project with 60+ references to a couple of round trips.
REFS_PER_QUERY = 50

# Issue/PR states that mean the work is no longer in flight.
LANDED_STATES = frozenset({"CLOSED", "MERGED"})

# Rough bytes-per-token for markdown prose, matching harness-ls. Deliberately
# crude: the job is showing that an index has a per-session cost at all, not
# billing for it.
CHARS_PER_TOKEN = 4

KIND_LABELS = {
    "dead-path": "dead path",
    "stale-ref": "stale ref",
    "unlinked": "unlinked",
    "dangling": "dangling",
    "name-slug": "name/slug",
    "index-prose": "index prose",
}


@dataclass
class Entry:
    """One `memory/<slug>.md` file."""

    path: Path
    name: str = ""
    description: str = ""
    mem_type: str = ""
    text: str = ""

    @property
    def filename(self) -> str:
        return self.path.name


@dataclass
class Finding:
    kind: str
    entry: str  # filename, or "" when the finding is about the index itself
    detail: str
    subject: str = ""  # the path / ref / link the finding is about


@dataclass
class ProjectReport:
    slug: str
    path: str  # resolved working directory, display form
    entries: int
    findings: List[Finding] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    refs: int = 0  # distinct `#N` the stale-ref check covers, checked or not
    index_chars: int = 0  # size of MEMORY.md, which every session in the project pays


def body_start(lines: Sequence[str]) -> int:
    """Index of the first line after the frontmatter block, 0 if there is none.

    The single answer to "where does the frontmatter end". Both readers of that
    boundary go through here: the parser, to know which lines hold fields, and
    the search, to know which lines are prose. Two copies disagreeing by so much
    as a `.strip()` makes a stretch of a file that neither one reads — invisible
    to search and absent from the parse, with nothing failing.

    An unterminated block is treated as no frontmatter at all, so the whole file
    stays searchable rather than being swallowed by a stray opening fence.
    """
    if not lines or lines[0].strip() != "---":
        return 0
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return index + 1
    return 0


def _unquote(value: str) -> str:
    """Drop the surrounding quotes YAML allows around a scalar.

    Without this the quotes travel: descriptions are shown beside every search
    hit and emitted in `--json`, so a consumer would receive `"\\"Protocol for
    …\\""` and have to strip them itself.
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_frontmatter(text: str) -> Dict[str, str]:
    """Pull `name`, `description` and `metadata.type` out of a memory file.

    Hand-rolled rather than a YAML dependency: the write rules fix the shape to
    two scalars and one single-key nested block, and this tool is otherwise
    stdlib plus rich.
    """
    lines = text.splitlines()
    end = body_start(lines)
    if not end:
        return {}
    body = lines[1 : end - 1]

    out: Dict[str, str] = {}
    in_metadata = False
    for line in body:
        if not line.strip():
            continue
        indented = line[:1].isspace()
        key, _, value = line.strip().partition(":")
        key, value = key.strip(), value.strip()
        if not indented:
            in_metadata = key == "metadata"
            if not in_metadata and value:
                out[key] = _unquote(value)
        elif in_metadata and key == "type":
            out["type"] = _unquote(value)
    return out


def load_entry(path: Path) -> Entry:
    text = path.read_text(errors="replace")
    fm = parse_frontmatter(text)
    return Entry(
        path=path,
        name=fm.get("name", ""),
        description=fm.get("description", ""),
        mem_type=fm.get("type", ""),
        text=text,
    )


def iter_memory_dirs(base: Path) -> List[Path]:
    """Every `<project>/memory/` under the projects root, sorted by project."""
    if not base.is_dir():
        return []
    return sorted((p / MEMORY_DIRNAME for p in base.iterdir()), key=lambda p: p.parent.name.lower())


def load_entries(memory_dir: Path) -> List[Entry]:
    files = sorted(f for f in memory_dir.glob("*.md") if f.name != INDEX_NAME)
    return [load_entry(f) for f in files]


# --- project path resolution -------------------------------------------------


def _recorded_path(project_dir: Path) -> Optional[str]:
    """The working directory as Claude Code itself recorded it.

    Preferred over reversing the folder name because it is lossless: the slug
    replaces '/' with '-', so a directory with a real hyphen in its name reads
    back ambiguously. Covered 31 of 50 projects in the survey — sessions get
    pruned, and the index file outlives them unevenly.
    """
    index = project_dir / "sessions-index.json"
    if index.is_file():
        try:
            recorded = json.loads(index.read_text(errors="replace")).get("originalPath")
        except (OSError, ValueError):
            recorded = None
        if recorded:
            return str(recorded)

    for session in sorted(project_dir.glob("*.jsonl"))[:3]:
        try:
            with session.open(errors="replace") as fh:
                for i, line in enumerate(fh):
                    if i > 5:
                        break
                    try:
                        cwd = json.loads(line).get("cwd")
                    except ValueError:
                        continue
                    if cwd:
                        return str(cwd)
        except OSError:
            continue
    return None


def resolve_slug(folder_name: str) -> List[Path]:
    """Directories on disk that the flattened folder name could mean.

    Walks top-down, merging the next 1..k hyphen-separated tokens at each step,
    because '-' in the slug is either a separator or a literal hyphen and
    nothing in the name says which.
    """
    parts = [p for p in folder_name.lstrip("-").split("-") if p]
    if not parts:
        return []
    results: List[Path] = []

    def walk(current: Path, idx: int) -> None:
        if idx >= len(parts):
            results.append(current)
            return
        for k in range(1, len(parts) - idx + 1):
            candidate = current / "-".join(parts[idx : idx + k])
            try:
                if candidate.is_dir():
                    walk(candidate, idx + k)
            except OSError:
                continue

    walk(Path("/"), 0)
    return results


def project_path(project_dir: Path) -> Optional[Path]:
    """Best guess at the directory a project's memories are about.

    None when nothing resolves, which is the honest answer for a project whose
    directory was deleted or renamed — and the signal to skip the checks that
    need a git remote rather than guess at one.
    """
    recorded = _recorded_path(project_dir)
    if recorded:
        expanded = Path(os.path.expanduser(recorded))
        if expanded.is_dir():
            return expanded
    resolved = resolve_slug(project_dir.name)
    if resolved:
        # Shortest wins: fewest separators means the most hyphens were kept
        # literal, which is the right reading for worktree-style names.
        return min(resolved, key=lambda p: len(p.parts))
    return None


def display_path(path: Optional[Path], slug: str) -> str:
    if path is None:
        return f"{slug} (unresolved)"
    text = str(path)
    home = str(Path.home())
    return "~" + text[len(home) :] if text.startswith(home) else text


# --- checks ------------------------------------------------------------------


def _is_absolute_citation(text: str) -> bool:
    return text.startswith("~/") or text.startswith("/Users/")


def _exists(candidate: str) -> bool:
    return Path(os.path.expanduser(candidate)).exists()


def _path_of_code_span(span: str) -> str:
    """The path a code span cites, whether it is a bare path or a command line.

    A span opening with a path is one of two things, and whitespace cannot tell
    them apart: a path containing a space (`~/Library/Application Support/…`) or
    an invocation with arguments (`~/3p/let-go/lg -w dist main.lg`). Ask the
    filesystem instead of guessing at the shape — if the whole span doesn't
    resolve but its first token does, it was a command, and the cited path is
    alive. Shape heuristics get the flag case and still read
    `lg test/run.lg` as one dead path.
    """
    if " " not in span or _exists(span):
        return span
    head = span.split()[0]
    return head if _exists(head) else span


def extract_paths(text: str) -> List[str]:
    """Concrete absolute paths cited in an entry, templated forms dropped.

    Code spans are read first and whole, so a path containing a space stays one
    path; the bare regex then sweeps what is left for paths written in plain
    prose. Reading the whole file with the regex alone reports every
    space-containing path as dead at its first space.

    A consequence worth knowing: a span is only read as a citation when it
    *opens* with a path, and the span is removed before the prose sweep — so a
    path buried mid-span, as in `cd ~/foo/bar`, is checked by neither. Eleven
    such citations exist in the surveyed store and none of them are dead, so
    the yield is zero today; reaching them means tokenizing the span, which
    also means deciding which token of `cp a b` is a path.
    """
    candidates: List[str] = []

    def remember(raw: str) -> None:
        # A trailing slash is a spelling choice, not a different path; without
        # this, `~/src/proj/` and `~/src/proj` report as two dead paths.
        candidate = raw.strip().rstrip(PATH_TRAILING).rstrip("/")
        if candidate and not any(marker in candidate for marker in TEMPLATE_MARKERS):
            candidates.append(candidate)

    for span in CODE_SPAN_RE.findall(text):
        content = span.strip()
        if _is_absolute_citation(content):
            remember(_path_of_code_span(content))

    for raw in PATH_RE.findall(CODE_SPAN_RE.sub(" ", text)):
        remember(raw)

    seen = set()
    return [c for c in candidates if not (c in seen or seen.add(c))]


def check_dead_paths(entries: Sequence[Entry]) -> List[Finding]:
    """Absolute paths an entry cites that no longer exist.

    The write rules already say to verify a named path before recommending it;
    this is that check run over the whole store at once. A fifth of the cited
    paths failed it in the survey, clustered in deleted worktrees and a docs
    migration.
    """
    findings: List[Finding] = []
    for entry in entries:
        for cited in extract_paths(entry.text):
            if not _exists(cited):
                findings.append(
                    Finding("dead-path", entry.filename, "no longer exists", subject=cited)
                )
    return findings


def check_index_drift(memory_dir: Path, entries: Sequence[Entry]) -> List[Finding]:
    """Entries missing from `MEMORY.md`, and index lines pointing at nothing.

    The index is the part loaded into context every session, so an entry that
    never got a line is an entry nothing will ever recall.
    """
    index = memory_dir / INDEX_NAME
    if not index.is_file():
        if not entries:
            return []
        return [Finding("dangling", INDEX_NAME, f"no {INDEX_NAME}, but {len(entries)} entries")]

    text = index.read_text(errors="replace")
    linked = {Path(target).name for target in INDEX_LINK_RE.findall(text)}
    present = {entry.filename for entry in entries}

    findings = [
        Finding("unlinked", name, f"no line in {INDEX_NAME}") for name in sorted(present - linked)
    ]
    findings += [
        Finding("dangling", INDEX_NAME, "index links to a missing file", subject=name)
        for name in sorted(linked - present)
    ]
    return findings


def check_index_prose(memory_dir: Path, entries: Sequence[Entry]) -> List[Finding]:
    """Facts written into `MEMORY.md` in a project that also keeps entry files.

    The index is loaded into context at the start of every session in its
    project; an entry file is read only when something recalls it. A fact living
    in the index is therefore paid for continuously rather than on demand, which
    is the cost the per-entry split exists to avoid.

    Deliberately scoped to projects that have entry files. Eight of the fifty
    surveyed keep *everything* in the index and have no entries at all — that is
    a different convention rather than drift, and reporting it would bury the
    four projects where facts and pointers have genuinely mixed.
    """
    index = memory_dir / INDEX_NAME
    if not entries or not index.is_file():
        return []

    text = index.read_text(errors="replace")
    bullets = [line.strip() for line in text.splitlines() if line.strip().startswith("-")]
    prose = [b for b in bullets if not INDEX_LINK_RE.search(b)]
    if not prose:
        return []

    tokens = len(text) // CHARS_PER_TOKEN
    return [
        Finding(
            "index-prose",
            INDEX_NAME,
            f"facts inline, not pointers; the index costs ~{tokens:,} tokens (est.) every session",
            subject=f"{len(prose)} of {len(bullets)} lines",
        )
    ]


def check_name_slug(entries: Sequence[Entry]) -> List[Finding]:
    """Frontmatter `name` disagreeing with the filename.

    Off by default. Links resolve by either spelling, so this breaks nothing —
    and 57% of a real store diverges, which is more findings than anyone
    intends to act on. Available for a deliberate convention sweep.
    """
    findings: List[Finding] = []
    for entry in entries:
        if entry.name and entry.name != entry.path.stem:
            findings.append(
                Finding(
                    "name-slug",
                    entry.filename,
                    f"frontmatter name is {entry.name!r}",
                    subject=entry.path.stem,
                )
            )
    return findings


# --- issue/PR state ----------------------------------------------------------


def repo_slug(path: Optional[Path]) -> Optional[Tuple[str, str]]:
    """(owner, name) for a directory's GitHub remote, `origin` preferred.

    Reads every remote rather than only `origin`, because a fork or a clone of
    someone else's project often keeps its GitHub remote under another name and
    would otherwise look like a repository with no remote at all.
    """
    if path is None or not path.is_dir():
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "remote", "-v"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None

    found: Dict[str, Tuple[str, str]] = {}
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        match = REMOTE_RE.search(parts[1])
        if match:
            found.setdefault(parts[0], (match.group(1), match.group(2)))
    if not found:
        return None
    return found.get("origin") or found[sorted(found)[0]]


def _refs_query(numbers: Sequence[int]) -> str:
    fields = (
        "__typename ... on Issue { number state title } ... on PullRequest { number state title }"
    )
    aliases = " ".join(f"r{n}: issueOrPullRequest(number: {n}) {{ {fields} }}" for n in numbers)
    return f"query($owner: String!, $name: String!) {{ repository(owner: $owner, name: $name) {{ {aliases} }} }}"


def lookup_refs(owner: str, name: str, numbers: Sequence[int]) -> Dict[int, Dict[str, str]]:
    """State and title for each `#N`, batched. Unknown numbers are simply absent.

    `gh api graphql` exits non-zero when *any* alias 404s but still returns the
    aliases that resolved, so the exit code is ignored and stdout is parsed
    either way. Checking the exit code instead would throw away a whole batch
    over one deleted reference.
    """
    out: Dict[int, Dict[str, str]] = {}
    for start in range(0, len(numbers), REFS_PER_QUERY):
        chunk = numbers[start : start + REFS_PER_QUERY]
        try:
            proc = subprocess.run(
                [
                    "gh",
                    "api",
                    "graphql",
                    "-f",
                    f"query={_refs_query(chunk)}",
                    # -f, not -F: `-F` applies magic type conversion, so an
                    # all-digit owner or repo name would be sent as a JSON
                    # number against a `String!` variable and error the batch.
                    "-f",
                    f"owner={owner}",
                    "-f",
                    f"name={name}",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            return out
        try:
            payload = json.loads(proc.stdout or "{}")
        except ValueError:
            continue
        repo = (payload.get("data") or {}).get("repository") or {}
        for key, value in repo.items():
            if not value or not key.startswith("r"):
                continue
            number = value.get("number")
            if number is not None:
                out[int(number)] = {
                    "state": value.get("state", ""),
                    "title": value.get("title", ""),
                    "type": "PR" if value.get("__typename") == "PullRequest" else "issue",
                }
    return out


def gh_unavailable_reason() -> Optional[str]:
    """None when `gh` can be used; otherwise why it can't, in `gh`'s own words.

    Worth the extra care because `gh auth status` validates the token against
    the API, so a transient network failure is indistinguishable from a missing
    login unless the actual error is reported. Asserting "unavailable or
    unauthenticated" for every failure sent someone to re-authenticate a login
    that was fine.
    """
    try:
        proc = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, timeout=15)
    except FileNotFoundError:
        return "`gh` is not installed"
    except subprocess.TimeoutExpired:
        return "`gh auth status` timed out after 15s"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"`gh` could not be run ({type(exc).__name__})"
    if proc.returncode != 0:
        return f"`gh auth status` failed: {_gh_diagnosis(proc.stderr or proc.stdout)}"
    return None


# What `gh auth status` says when it is unhappy, as opposed to the account
# details and remediation hints it prints alongside.
GH_TROUBLE_RE = re.compile(r"invalid|expired|failed|error|not logged in|no accounts", re.I)


def _gh_diagnosis(output: str) -> str:
    """The line of `gh auth status` worth repeating, plus its own fix if given.

    Not the last line: `gh` ends with `To forget about this account, run: gh
    auth logout …`, so taking the tail reports housekeeping advice as the
    diagnosis. The failure is stated several lines earlier.
    """
    lines = [ln.strip().lstrip("X-• ").strip() for ln in output.splitlines() if ln.strip()]
    if not lines:
        return "no output"
    diagnosis = next((ln for ln in lines if GH_TROUBLE_RE.search(ln)), lines[-1])
    remedy = next((ln for ln in lines if "gh auth login" in ln), "")
    return f"{diagnosis.rstrip('.')} — {remedy}" if remedy else diagnosis


RefKey = Tuple[Optional[str], Optional[str], int]


def extract_refs(
    entries: Sequence[Entry], all_types: bool, repo: Optional[Tuple[str, str]] = None
) -> Dict[RefKey, List[str]]:
    """(owner, name, number) -> the entries citing it.

    The single extraction both the counter and the check read. Two traversals
    keyed even slightly differently drift silently — the count advertised as
    "not checked" would stop matching what `--online` actually looks up, and
    nothing would fail.

    `repo` attributes bare `#N` to the project's own repository; without it they
    key as `(None, None, n)`, which still counts but cannot be looked up.
    """
    wanted = [e for e in entries if all_types or e.mem_type == "project"]
    bare_owner, bare_name = repo if repo else (None, None)

    cited: Dict[RefKey, List[str]] = {}
    for entry in wanted:
        # Qualified references are matched and blanked first, so the bare sweep
        # can't also claim their numbers for the project's own repo.
        remainder = entry.text
        for match in QUALIFIED_ISSUE_RE.finditer(entry.text):
            key = (match.group(1), match.group(2), int(match.group(3)))
            cited.setdefault(key, []).append(entry.filename)
            remainder = remainder.replace(match.group(0), " ")
        for raw in ISSUE_RE.findall(remainder):
            cited.setdefault((bare_owner, bare_name, int(raw)), []).append(entry.filename)
    return cited


def check_stale_refs(
    cited: Dict[RefKey, List[str]], repo: Optional[Tuple[str, str]] = None
) -> List[Finding]:
    """References whose work has already landed, from an extracted citation map.

    Callers scope which entries are read (see `extract_refs`): `project` ones by
    default, because that is the type whose whole job is describing work in
    flight. A merged `#N` in a `reference` entry is usually just a citation, and
    flagging those buries the ones that matter.

    `repo` is optional because a project without a GitHub remote can still cite
    `owner/repo#123`, which names its own repository and needs nothing from the
    local checkout. Only bare `#N` go unresolved there, and they are dropped
    individually rather than taking the whole project's references with them.
    """
    if not cited:
        return []
    owner, name = repo if repo else (None, None)

    # Unattributed bare references fall back to the project's own repo, which is
    # what `extract_refs` already did when it was given one.
    by_repo: Dict[Tuple[str, str], Dict[int, List[str]]] = {}
    for (repo_owner, repo_name, number), filenames in cited.items():
        resolved = (repo_owner or owner, repo_name or name)
        if resolved[0] is None or resolved[1] is None:
            continue  # a bare `#N` with no repo to attribute it to
        by_repo.setdefault(resolved, {}).setdefault(number, []).extend(filenames)

    findings: List[Finding] = []
    for (repo_owner, repo_name), numbers in by_repo.items():
        states = lookup_refs(repo_owner, repo_name, sorted(numbers))
        qualified = (repo_owner, repo_name) != (owner, name)
        for number in sorted(numbers):
            info = states.get(number)
            if not info or info["state"] not in LANDED_STATES:
                continue
            ref = f"{repo_owner}/{repo_name}#{number}" if qualified else f"#{number}"
            detail = f"{info['state'].lower()} {info['type']} — {info['title']}"
            for filename in sorted(set(numbers[number])):
                findings.append(Finding("stale-ref", filename, detail, subject=ref))
    return sorted(findings, key=lambda f: (f.entry, f.subject))


# --- scan --------------------------------------------------------------------


def scan(
    base: Path,
    only: Optional[str] = None,
    online: bool = False,
    strict: bool = False,
    all_types: bool = False,
) -> Tuple[List[ProjectReport], List[str]]:
    """Run every enabled check over the store. Returns (reports, global notes).

    `online` is taken at face value; the caller is responsible for having
    checked that `gh` works. The CLI does that up front and refuses to run
    rather than downgrading, so a request for the reference check is never
    quietly answered without it.
    """
    notes: List[str] = []
    reports: List[ProjectReport] = []
    for memory_dir in iter_memory_dirs(base):
        if not memory_dir.is_dir():
            continue
        slug = memory_dir.parent.name
        if only and only.lower() not in slug.lower():
            continue

        entries = load_entries(memory_dir)
        path = project_path(memory_dir.parent)
        report = ProjectReport(slug=slug, path=display_path(path, slug), entries=len(entries))

        report.findings += check_dead_paths(entries)
        report.findings += check_index_drift(memory_dir, entries)
        report.findings += check_index_prose(memory_dir, entries)
        index = memory_dir / INDEX_NAME
        report.index_chars = len(index.read_text(errors="replace")) if index.is_file() else 0
        if strict:
            report.findings += check_name_slug(entries)

        # Reading the remote is a local git call, so it happens either way: it
        # attributes bare `#N` to a repo, which is what makes the advertised
        # count equal the number of lookups `--online` would perform.
        cited = extract_refs(entries, all_types)
        repo = repo_slug(path) if cited else None
        if repo:
            cited = extract_refs(entries, all_types, repo)
        report.refs = len(cited)

        if online:
            report.findings += check_stale_refs(cited, repo)
            # Say what the miss cost. "No GitHub remote" is true but reads as a
            # footnote next to 240 unchecked references.
            unattributed = sum(1 for owner, _, _ in cited if owner is None) if not repo else 0
            if unattributed:
                report.skipped.append(
                    f"stale-ref: no GitHub remote — {unattributed} bare "
                    f"{'reference' if unattributed == 1 else 'references'} not checked"
                )

        reports.append(report)

    if not online:
        pending = sum(r.refs for r in reports)
        if pending:
            citing = sum(1 for r in reports if r.refs)
            notes.append(
                f"{pending} issue/PR {'reference' if pending == 1 else 'references'} across "
                f"{citing} {'project' if citing == 1 else 'projects'} not checked — "
                "re-run with --online to see which have already landed"
            )
    return reports, notes


# --- output ------------------------------------------------------------------

SELF_EVIDENT_KINDS = frozenset({"dead-path", "unlinked"})

KIND_STYLES = {
    "dead-path": "red",
    "index-prose": "cyan",
    "stale-ref": "yellow",
    "unlinked": "cyan",
    "dangling": "cyan",
    "name-slug": "magenta",
}


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def render(console: Console, reports: List[ProjectReport], notes: List[str], strict: bool) -> None:
    total_entries = sum(r.entries for r in reports)
    flagged = [r for r in reports if r.findings]

    console.print()
    console.print(
        Text.assemble(
            ("MEMORY STORE", "bold"),
            (
                f" — {plural(len(reports), 'project')}, "
                f"{total_entries} {'entry' if total_entries == 1 else 'entries'}",
                "dim",
            ),
        )
    )

    for report in flagged:
        console.print()
        console.print(Text(report.path, style="bold"))
        table = Table(
            box=None, show_header=False, pad_edge=False, padding=(0, 2, 0, 0), expand=True
        )
        table.add_column(no_wrap=True)
        # The entry filename is capped against the terminal width rather than
        # left to its natural size. Rich shrinks the flexible column first, so a
        # long filename in a narrow terminal starves the subject column to zero
        # and prints a finding with no path or reference in it at all — losing
        # the one column that says what the finding is about. `min_width` alone
        # does not win that fight.
        table.add_column(
            no_wrap=True,
            overflow="ellipsis",
            style="dim",
            max_width=max(12, console.width - 40),
        )
        table.add_column(overflow="fold", ratio=1, min_width=12)
        for finding in report.findings:
            label = KIND_LABELS.get(finding.kind, finding.kind)
            # The kind already says "dead path" / "unlinked"; repeating the
            # detail beside it just steals width from the subject itself.
            detail = "" if finding.kind in SELF_EVIDENT_KINDS else finding.detail
            message = Text()
            if finding.subject:
                message.append(finding.subject)
                if detail:
                    message.append("  ")
            if detail:
                message.append(detail, style="dim")
            table.add_row(
                Text(label, style=KIND_STYLES.get(finding.kind, "")),
                finding.entry,
                message,
            )
        console.print(table)

    counts: Dict[str, int] = {}
    for report in reports:
        for finding in report.findings:
            counts[finding.kind] = counts.get(finding.kind, 0) + 1

    console.print()
    if counts:
        summary = ", ".join(f"{counts[k]} {KIND_LABELS.get(k, k)}" for k in sorted(counts))
        console.print(Text(f"{plural(sum(counts.values()), 'finding')}: {summary}", style="bold"))
    else:
        console.print(Text("no findings", style="bold green"))

    # The readable path, as the finding sections use — not the flattened slug.
    # A skip line is the only place a project is named without its findings
    # underneath, so it is the one place the encoded form can't be decoded from
    # context.
    for report in reports:
        for skip in report.skipped:
            console.print(Text(f"  {report.path}: {skip}", style="dim"))
    for note in notes:
        console.print(Text(f"  {note}", style="dim"))
    # The standing cost of the store, whether or not anything is wrong with it.
    # Index files are the part loaded unconditionally, so this is the bill the
    # memory system runs up before a single entry is recalled.
    index_tokens = sum(r.index_chars for r in reports) // CHARS_PER_TOKEN
    if index_tokens:
        console.print(
            Text(
                f"  indexes total ~{index_tokens:,} tokens (est.), each loaded every session "
                "in its own project",
                style="dim",
            )
        )
    if not strict:
        console.print(Text("  name/slug divergence not checked (--strict)", style="dim"))
    console.print()


def as_json(reports: List[ProjectReport], notes: List[str]) -> str:
    payload = {
        "projects": [
            {
                "project": r.slug,
                "path": r.path,
                "entries": r.entries,
                # Distinct references the stale-ref check covers. Present either
                # way, so a consumer can tell a partial scan from a clean one
                # without string-matching the prose in `notes`.
                "references": r.refs,
                "index_chars": r.index_chars,
                "skipped": r.skipped,
                "findings": [
                    {
                        "kind": f.kind,
                        "entry": f.entry,
                        "subject": f.subject,
                        "detail": f.detail,
                    }
                    for f in r.findings
                ],
            }
            for r in reports
        ],
        "notes": notes,
        "totals": {
            "projects": len(reports),
            "entries": sum(r.entries for r in reports),
            "findings": sum(len(r.findings) for r in reports),
        },
    }
    return json.dumps(payload, indent=2)


# --- search ------------------------------------------------------------------


@dataclass
class Match:
    """One matching line, with enough of its entry to judge it without opening."""

    entry: str
    mem_type: str
    description: str
    line_no: int
    line: str


@dataclass
class SearchResult:
    project: str  # readable working directory
    slug: str
    matches: List[Match] = field(default_factory=list)


def search_entries(memory_dir: Path, pattern: "re.Pattern[str]") -> List[Match]:
    """Lines matching `pattern` in a project's memories.

    `MEMORY.md` is searched alongside the entry files. Eight of the fifty
    projects surveyed keep every fact in the index and have no entry files at
    all, so skipping it would silently return nothing for them.
    """
    matches: List[Match] = []
    for path in sorted(memory_dir.glob("*.md")):
        entry = load_entry(path)
        kind = entry.mem_type or ("index" if path.name == INDEX_NAME else "")

        lines = entry.text.splitlines()
        body_from = body_start(lines)
        found = [
            Match(path.name, kind, entry.description, n, line.strip())
            for n, line in enumerate(lines[body_from:], start=body_from + 1)
            if pattern.search(line)
        ]
        # A term that only appears in the frontmatter still means the entry
        # matched, but the header already prints `name` and `description`, so
        # repeating those lines underneath it says the same thing twice. Line 0
        # is the header standing in for the hit.
        if not found and (pattern.search(entry.name) or pattern.search(entry.description)):
            found = [Match(path.name, kind, entry.description, 0, "")]
        matches.extend(found)
    return matches


def search(
    base: Path, pattern: "re.Pattern[str]", only: Optional[str] = None
) -> List[SearchResult]:
    results: List[SearchResult] = []
    for memory_dir in iter_memory_dirs(base):
        if not memory_dir.is_dir():
            continue
        slug = memory_dir.parent.name
        if only and only.lower() not in slug.lower():
            continue
        found = search_entries(memory_dir, pattern)
        if found:
            path = project_path(memory_dir.parent)
            results.append(SearchResult(project=display_path(path, slug), slug=slug, matches=found))
    return results


def render_search(console: Console, results: List[SearchResult], term: str) -> None:
    total = sum(len(r.matches) for r in results)
    entries = sum(len({m.entry for m in r.matches}) for r in results)

    console.print()
    console.print(
        Text.assemble(
            ("MEMORY STORE", "bold"),
            (f" — {term!r} in {entries} {'entry' if entries == 1 else 'entries'}", "dim"),
        )
    )

    for result in results:
        console.print()
        console.print(Text(result.project, style="bold"))
        last_entry = ""
        for match in result.matches:
            if match.entry != last_entry:
                header = Text(f"  {match.entry}", style="cyan")
                if match.mem_type:
                    header.append(f"  {match.mem_type}", style="dim")
                if match.description:
                    header.append(f"  — {match.description}", style="dim")
                console.print(header)
                last_entry = match.entry
            if match.line_no:
                console.print(
                    Text.assemble((f"    {match.line_no}:", "dim"), (f" {match.line}", "")),
                    overflow="fold",
                )

    console.print()
    if total:
        console.print(
            Text(
                f"{total} {'match' if total == 1 else 'matches'} in {entries} "
                f"{'entry' if entries == 1 else 'entries'} across "
                f"{plural(len(results), 'project')}",
                style="bold",
            )
        )
    else:
        console.print(Text(f"no memory mentions {term!r}", style="bold"))
    console.print()


def search_as_json(results: List[SearchResult], term: str) -> str:
    payload = {
        "term": term,
        "projects": [
            {
                "project": r.slug,
                "path": r.project,
                "matches": [
                    {
                        "entry": m.entry,
                        "type": m.mem_type,
                        "description": m.description,
                        "line": m.line_no,
                        "text": m.line,
                    }
                    for m in r.matches
                ],
            }
            for r in results
        ],
        "totals": {
            "projects": len(results),
            "matches": sum(len(r.matches) for r in results),
        },
    }
    return json.dumps(payload, indent=2)


def scope_problem(base: Path, only: Optional[str]) -> Optional[str]:
    """Why there is nothing to look at, or None when there is.

    Checked once, before either mode runs. Both modes report "nothing" for their
    own reasons — no findings, no matches — and neither of those should be the
    answer to a mistyped `--base` or `--project`, which is a question that was
    never asked of the store at all.
    """
    stores = [d for d in iter_memory_dirs(base) if d.is_dir()]
    if not stores:
        return f"no memory stores found under {base}"
    if only and not any(only.lower() in d.parent.name.lower() for d in stores):
        return f"no project matches {only!r} under {base}"
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memory-ls",
        description="Report memories that have gone stale: dead paths, landed work described as in flight, index drift.",
    )
    parser.add_argument(
        "--search",
        metavar="PATTERN",
        help="list memories matching PATTERN (case-insensitive regex) instead of checking them",
    )
    parser.add_argument(
        "--project", metavar="MATCH", help="only projects whose slug contains MATCH"
    )
    parser.add_argument(
        "--base",
        type=Path,
        default=Path.home() / ".claude" / "projects",
        help="projects root (default: ~/.claude/projects)",
    )
    parser.add_argument(
        "--online",
        action="store_true",
        help="also check issue/PR state via `gh` (the only network this tool does)",
    )
    parser.add_argument(
        "--all-types",
        action="store_true",
        help="check #N references in every entry, not just `project` ones",
    )
    parser.add_argument(
        "--strict", action="store_true", help="also report frontmatter name vs filename divergence"
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    # Arguments before environment. A malformed pattern is wrong no matter what
    # the store contains, so reporting the store first answers a question the
    # user can't act on and hides the one they can.
    pattern = None
    if args.search is not None:
        # Reject rather than ignore. These configure checks that `--search`
        # doesn't run, and silently dropping them would answer a narrower
        # question than the one that was asked.
        conflicting = [n for n in ("online", "all_types", "strict") if getattr(args, n)]
        if conflicting:
            flags = ", ".join(f"--{n.replace('_', '-')}" for n in conflicting)
            print(
                f"memory-ls: --search does not run checks, so {flags} has no effect",
                file=sys.stderr,
            )
            return 2
        try:
            pattern = re.compile(args.search, re.IGNORECASE)
        except re.error as exc:
            print(f"memory-ls: --search pattern is not valid regex: {exc}", file=sys.stderr)
            return 2

    problem = scope_problem(args.base, args.project)
    if problem:
        print(f"memory-ls: {problem}", file=sys.stderr)
        return 2

    if pattern is not None:
        results = search(args.base, pattern, only=args.project)
        if args.json:
            print(search_as_json(results, args.search))
        else:
            render_search(Console(), results, args.search)
        return 0 if results else 1

    # Fail before scanning, not after. `--online` asks for a specific check, and
    # answering with a full report whose footer admits the check never ran hides
    # the failure exactly where it is easiest to miss — under the findings.
    if args.online:
        reason = gh_unavailable_reason()
        if reason:
            print(f"memory-ls: --online needs `gh`, and {reason}", file=sys.stderr)
            print("Re-run without --online for the local checks.", file=sys.stderr)
            return 2

    reports, notes = scan(
        args.base,
        only=args.project,
        online=args.online,
        strict=args.strict,
        all_types=args.all_types,
    )
    if args.json:
        print(as_json(reports, notes))
    else:
        render(Console(), reports, notes, args.strict)
    return 0


if __name__ == "__main__":
    sys.exit(main())
