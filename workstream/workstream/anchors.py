"""Anchor refs and their read-time liveness (D7).

An anchor is a ref a workstream hangs off: a worktree, a branch, a PR, a
capture file, a session. weft stores the ref; whether the thing behind it is
still dirty, pushed, merged, open or running is asked of git, gh and the
registry at the moment someone looks, never stored.

Every probe is bounded (5 s) and never raises. A probe that cannot answer
leaves its keys at None and says why in `warnings`, because a frontier with
one unreachable anchor is still worth showing.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import seam

PROBE_TIMEOUT = 5
GH_CACHE_TTL = 600
_UNMERGED_LIST_MAX = 10

GhRunner = Callable[[list[str]], tuple[int, str]]


@dataclass
class Anchor:
    ref: str
    scheme: str
    value: str
    liveness: dict = field(default_factory=dict)
    # Reasons only, never prefixed with the ref: every reader prints them
    # beside the ref already, and a prefix here doubled it in loose ends.
    warnings: list[str] = field(default_factory=list)


def parse_ref(ref: str) -> tuple[str, str]:
    """Split `scheme:value`. An unrecognised or missing scheme comes back as
    given (or "") so the caller can warn instead of guessing."""
    scheme, sep, value = ref.partition(":")
    if not sep:
        return "", ref
    return scheme, value


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- subprocess plumbing ---------------------------------------------------


def _run(argv: list[str], cwd: str | None = None) -> tuple[int, str]:
    """(returncode, stdout). Timeouts and missing binaries come back as
    nonzero codes rather than exceptions, so callers have one failure path."""
    env = dict(os.environ)
    # `git status` refreshes the index and takes index.lock to do it; a probe
    # that only reads must not collide with a session committing in that tree.
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        out = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return 124, ""
    except (FileNotFoundError, OSError):
        return 127, ""
    return out.returncode, out.stdout


def _git(path: str, *args: str) -> tuple[int, str]:
    rc, out = _run(["git", "-C", path, *args])
    return rc, out.strip()


def _ref_exists(path: str, ref: str) -> bool:
    rc, _ = _git(path, "rev-parse", "--verify", "--quiet", ref)
    return rc == 0


def _default_branch(repo: str) -> tuple[str | None, list[str]]:
    """The default branch name and the refs that stand for it (local and
    remote-tracking, whichever exist).

    origin/HEAD is what the remote calls its default; a clone made with
    `git init` + `remote add` never gets that symref, hence the fallbacks.
    """
    name = None
    rc, out = _git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if rc == 0 and out.startswith("origin/"):
        name = out.removeprefix("origin/")
    else:
        for cand in ("main", "master"):
            if _ref_exists(repo, f"refs/heads/{cand}") or _ref_exists(
                repo, f"refs/remotes/origin/{cand}"
            ):
                name = cand
                break
    if name is None:
        return None, []
    refs = [
        r for r in (f"refs/heads/{name}", f"refs/remotes/origin/{name}") if _ref_exists(repo, r)
    ]
    return name, refs


# --- wt: -------------------------------------------------------------------


def _probe_wt(path: str, a: Anchor, ctx: _Ctx | None = None) -> None:
    lv = a.liveness
    lv.update(
        exists=False,
        dirty=None,
        branch=None,
        detached=None,
        upstream=None,
        unpushed=None,
        unmerged=None,
        unmerged_commits=[],
        merged_into_default=None,
        merged_via=None,
        default_branch=None,
        main_checkout=None,
        linked=None,
    )
    p = Path(path)
    if not p.is_dir():
        return
    lv["exists"] = True

    rc, inside = _git(path, "rev-parse", "--is-inside-work-tree")
    if rc != 0 or inside != "true":
        # A scratch dir is a legitimate workspace; it just has no git state.
        a.warnings.append("not a git work tree")
        return

    # The main checkout is the one whose git dir *is* the common dir. Comparing
    # path strings instead breaks as soon as anything above them is a symlink.
    rc, dirs = _git(
        path, "rev-parse", "--path-format=absolute", "--absolute-git-dir", "--git-common-dir"
    )
    lines = dirs.splitlines()
    if rc == 0 and len(lines) == 2:
        git_dir, common = (os.path.normpath(x) for x in lines)
        lv["linked"] = git_dir != common
        lv["main_checkout"] = (
            os.path.dirname(common) if os.path.basename(common) == ".git" else common
        )
    else:
        a.warnings.append("could not resolve git-common-dir")

    rc, status = _git(path, "status", "--porcelain")
    lv["dirty"] = bool(status) if rc == 0 else None
    if rc != 0:
        a.warnings.append("git status failed")

    rc, branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0:
        # An unborn HEAD (fresh `git init`, no commits) lands here.
        a.warnings.append("HEAD does not resolve")
        return
    lv["detached"] = branch == "HEAD"
    lv["branch"] = None if lv["detached"] else branch

    if not lv["detached"]:
        _probe_unpushed(path, branch, a)

    repo = lv["main_checkout"] or path
    name, refs = _default_branch(repo)
    lv["default_branch"] = name
    if not refs:
        a.warnings.append("no default branch found (tried origin/HEAD, main, master)")
        return
    rc, count = _git(path, "rev-list", "--count", "HEAD", "--not", *refs)
    if rc == 0 and count.isdigit():
        lv["unmerged"] = int(count)
        if lv["unmerged"]:
            rc, log = _git(
                path,
                "log",
                "--format=%h %s",
                f"--max-count={_UNMERGED_LIST_MAX}",
                "HEAD",
                "--not",
                *refs,
            )
            lv["unmerged_commits"] = log.splitlines() if rc == 0 else []
        # `unmerged` stays the reachability count; `merged_into_default` is the
        # verdict, which a squash merge can make true while the count is not 0.
        lv["merged_into_default"] = lv["unmerged"] == 0
        if not lv["merged_into_default"] and ctx is not None:
            lv["merged_via"] = _squash_merged(path, "HEAD", lv["branch"], refs, ctx, a)
            lv["merged_into_default"] = lv["merged_via"] is not None
    else:
        a.warnings.append(f"could not count commits outside {name}")


def _probe_unpushed(path: str, branch: str, a: Anchor) -> None:
    """Commits on HEAD the remote does not have, or why that can't be counted.

    `rev-list --count <missing-ref>..HEAD` exits 128 printing nothing, so any
    "failure means 0" shortcut reports "nothing unpushed" for a branch that has
    never been pushed -- the exact case this field exists to flag. A missing
    remote ref also can't say whether it was never pushed or deleted after a
    merge, so the wording stays neutral ("no upstream") and `unmerged` answers
    the merge question separately.
    """
    lv = a.liveness
    rc, remotes = _git(path, "remote")
    if rc == 0 and not remotes:
        lv["unpushed"] = "no remote"
        return

    rc, upstream = _git(path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    target = None
    if rc == 0 and upstream:
        lv["upstream"] = upstream
        if _ref_exists(path, upstream):
            target = upstream
    else:
        # `@{upstream}` refuses to resolve once the remote branch is gone, but
        # the tracking config survives; report it so "gone" is distinguishable
        # from "never configured".
        rc_r, remote = _git(path, "config", f"branch.{branch}.remote")
        rc_m, merge = _git(path, "config", f"branch.{branch}.merge")
        if rc_r == 0 and rc_m == 0 and remote and merge:
            lv["upstream"] = f"{remote}/{merge.removeprefix('refs/heads/')}"
    if (
        target is None
        and lv["upstream"] is None
        and _ref_exists(path, f"refs/remotes/origin/{branch}")
    ):
        # Pushed without -u: no tracking config, but the remote has the branch.
        target = f"origin/{branch}"

    if target is None:
        lv["unpushed"] = "no upstream"
        return
    rc, count = _git(path, "rev-list", "--count", f"{target}..HEAD")
    if rc == 0 and count.isdigit():
        lv["unpushed"] = int(count)
    else:
        a.warnings.append(f"could not count commits ahead of {target}")


# --- branch: ---------------------------------------------------------------


def _repo_candidates(basename: str, repo_hints: dict[str, str] | None) -> list[Path]:
    cands = []
    if repo_hints and basename in repo_hints:
        cands.append(Path(repo_hints[basename]).expanduser())
    home = Path.home() / "projects-new"
    cands += [home / basename, home / "3p" / basename]
    return cands


def _probe_branch(value: str, a: Anchor, ctx: _Ctx) -> None:
    lv = a.liveness
    lv.update(
        repo=None, exists_local=None, exists_remote=None, merged_into_default=None, merged_via=None
    )
    basename, sep, branch = value.partition("@")
    if not sep or not basename or not branch:
        a.warnings.append("expected <repo-basename>@<branch>")
        return
    repo = next(
        (c for c in _repo_candidates(basename, ctx.repo_hints) if (c / ".git").exists()), None
    )
    if repo is None:
        a.warnings.append(f"repo {basename!r} not found locally")
        return
    r = str(repo)
    lv["repo"] = r
    # Remote state is as of the last fetch; probing never fetches, since a
    # read-only look must not take seconds per anchor or touch the network.
    lv["exists_local"] = _ref_exists(r, f"refs/heads/{branch}")
    lv["exists_remote"] = _ref_exists(r, f"refs/remotes/origin/{branch}")
    tip = (
        f"refs/heads/{branch}"
        if lv["exists_local"]
        else f"refs/remotes/origin/{branch}"
        if lv["exists_remote"]
        else None
    )
    name, refs = _default_branch(r)
    if tip is None and refs:
        # Deleted locally and on the remote, the way a retired worktree's
        # branch ends: git has nothing left to compare, so GitHub's record of
        # a PR merged from this head is the only witness.
        lv["merged_via"] = _merged_pr_head_only(r, branch, ctx, a)
        lv["merged_into_default"] = True if lv["merged_via"] else None
        return
    if tip is None or not refs:
        if not refs:
            a.warnings.append("no default branch found")
        return
    lv["merged_into_default"] = any(
        _run(["git", "-C", r, "merge-base", "--is-ancestor", tip, ref])[0] == 0 for ref in refs
    )
    if not lv["merged_into_default"]:
        lv["merged_via"] = _squash_merged(r, tip, branch, refs, ctx, a)
        lv["merged_into_default"] = lv["merged_via"] is not None


# --- squash merges ---------------------------------------------------------
#
# A squash or rebase merge lands the branch's content on the default branch
# under new commits, so the tip stays unreachable and reachability alone calls
# the branch unmerged forever. Two further witnesses, asked in this order:
# GitHub, which knows which PR merged from this head branch; then the tree
# test, for branches that never had a PR.


def _squash_merged(
    git_dir: str, tip: str, branch: str | None, refs: list[str], ctx: _Ctx, a: Anchor
) -> dict | None:
    """`merged_via` for a branch reachability calls unmerged, or None when
    neither witness can say it landed."""
    if branch:
        via = _merged_pr(git_dir, tip, branch, ctx, a)
        if via is not None:
            return via
    return {"tree": True} if _tree_already_on(git_dir, tip, refs) else None


_REMOTE_RE = re.compile(
    r"^(?:[\w.-]+@(?P<h1>[\w.-]+):|(?:ssh|https?|git)://(?:[^@/]+@)?(?P<h2>[\w.-]+)(?::\d+)?/)"
    r"(?P<owner>[\w.-]+)/(?P<name>[\w.-]+?)(?:\.git)?/?$"
)


def gh_repo_from_url(url: str) -> str | None:
    """`owner/name` (or `host/owner/name` off github.com) for `gh --repo`,
    from an ssh, scp-style or https remote URL; None for a local path."""
    m = _REMOTE_RE.match(url.strip())
    if not m:
        return None
    host = m.group("h1") or m.group("h2")
    slug = f"{m.group('owner')}/{m.group('name')}"
    return slug if host == "github.com" else f"{host}/{slug}"


def _merged_pr(git_dir: str, tip: str, branch: str, ctx: _Ctx, a: Anchor) -> dict | None:
    rc, url = _git(git_dir, "remote", "get-url", "origin")
    repo = gh_repo_from_url(url) if rc == 0 else None
    if repo is None:
        return None
    rows = _merged_prs_by_head(repo, branch, ctx, a)
    # A head branch name is not an identity: it gets reused after its PR
    # merges, and a fresh branch can share an old PR's name. A PR is credited
    # only when git shows the local tip at or before that PR's head; an
    # unknown head object (pruned, never fetched) proves nothing, so the tree
    # test decides instead.
    landed = [
        r
        for r in rows or []
        if r.get("headRefOid")
        and _run(["git", "-C", git_dir, "merge-base", "--is-ancestor", tip, r["headRefOid"]])[0]
        == 0
    ]
    if not landed:
        return None
    pr = max(landed, key=lambda r: r.get("mergedAt") or "")
    return {
        "pr": pr.get("number"),
        "merged_at": pr.get("mergedAt"),
        "commit": (pr.get("mergeCommit") or {}).get("oid"),
    }


def _merged_pr_head_only(git_dir: str, branch: str, ctx: _Ctx, a: Anchor) -> dict | None:
    """`merged_via` from the newest merged PR whose head was `branch`, for a
    branch with no tip left to check against the PR's head. Branch names get
    reused, so this is weaker than `_merged_pr`, and says so (`head_only`)."""
    rc, url = _git(git_dir, "remote", "get-url", "origin")
    repo = gh_repo_from_url(url) if rc == 0 else None
    rows = _merged_prs_by_head(repo, branch, ctx, a) if repo else None
    if not rows:
        return None
    pr = max(rows, key=lambda r: r.get("mergedAt") or "")
    return {
        "pr": pr.get("number"),
        "merged_at": pr.get("mergedAt"),
        "commit": (pr.get("mergeCommit") or {}).get("oid"),
        "head_only": True,
    }


def _merged_prs_by_head(repo: str, branch: str, ctx: _Ctx, a: Anchor) -> list[dict] | None:
    """Merged PRs whose head is `branch`, cached like PR probes. An empty
    list is an answer and is cached; None (gh failed) is not."""
    now = ctx.now() if callable(ctx.now) else ctx.now
    key = re.sub(r"[^\w.-]", "_", f"head__{repo}__{branch}")
    cached = _cache_get(key, now, any_age=ctx.gh_cache_only)
    if cached is not None and isinstance(cached.get("rows"), list):
        return cached["rows"]
    if ctx.gh_cache_only:
        return None
    rc, out = ctx.gh_runner(
        [
            "gh", "pr", "list", "--repo", repo, "--state", "merged", "--head", branch,
            "--json", "number,mergedAt,mergeCommit,headRefOid",
        ]
    )  # fmt: skip
    if rc != 0:
        return None
    try:
        rows = json.loads(out)
    except ValueError:
        return None
    if not isinstance(rows, list):
        return None
    rows = [r for r in rows if isinstance(r, dict)]
    err = _cache_put(key, now, {"rows": rows})
    if err:
        a.warnings.append(str(err))
    return rows


def _tree_already_on(git_dir: str, tip: str, refs: list[str]) -> bool:
    """True when merging `tip` into a default ref would change nothing: the
    branch's changes are already there. A conflict (exit 1) or a different
    tree is inconclusive, never proof of unmerged work."""
    for ref in refs:
        rc, merged = _git(git_dir, "merge-tree", "--write-tree", ref, tip)
        if rc != 0:
            continue
        rc, base = _git(git_dir, "rev-parse", f"{ref}^{{tree}}")
        if rc == 0 and merged.splitlines()[:1] == [base]:
            return True
    return False


# --- gh: -------------------------------------------------------------------


def _default_gh_runner(argv: list[str]) -> tuple[int, str]:
    return _run(argv)


def gh_cache_dir() -> Path:
    override = os.environ.get("WORKSTREAM_CACHE_DIR")
    if override:
        base = Path(override)
    else:
        xdg = os.environ.get("XDG_CACHE_HOME")
        base = (Path(xdg) if xdg else Path.home() / ".cache") / "workstream"
    return base / "gh"


def _cache_get(key: str, now: float, *, any_age: bool = False) -> dict | None:
    """The cached payload for `key` (with its `fetched_at`) if younger than
    GH_CACHE_TTL, or of any age with `any_age`. A missing, unreadable or
    stale entry is a miss."""
    try:
        cached = json.loads((gh_cache_dir() / f"{key}.json").read_text(encoding="utf-8"))
        if isinstance(cached, dict) and (
            any_age or now - float(cached["fetched_at"]) < GH_CACHE_TTL
        ):
            return cached
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _cache_put(key: str, now: float, payload: dict) -> str | None:
    """Write `payload` stamped with `now`, atomically. Returns a warning on
    failure rather than raising: a cold cache only costs a later gh call."""
    cache = gh_cache_dir() / f"{key}.json"
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        # One temp file per writer: `workstream timeline` builds frontiers on
        # threads, and two of them can probe the same anchor.
        tmp = cache.with_name(f"{cache.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps({**payload, "fetched_at": now}), encoding="utf-8")
        tmp.replace(cache)
    except OSError as e:
        return f"cache write failed ({e.__class__.__name__})"
    return None


_PR_FIELDS = "state,isDraft,reviewDecision,updatedAt,mergedAt,headRefName,reviews"
_ISSUE_FIELDS = "state,updatedAt,closedAt"


def _shape_pr(d: dict) -> dict:
    reviews = d.get("reviews") or []
    return {
        "kind": "pr",
        "state": d.get("state"),
        "is_draft": d.get("isDraft"),
        "review_decision": d.get("reviewDecision") or None,
        "updated_at": d.get("updatedAt"),
        "merged_at": d.get("mergedAt"),
        "head_ref": d.get("headRefName"),
        "reviews": [
            {
                "author": (r.get("author") or {}).get("login"),
                "state": r.get("state"),
                "submitted_at": r.get("submittedAt"),
            }
            for r in reviews[-3:]
        ],
    }


def _shape_issue(d: dict) -> dict:
    return {
        "kind": "issue",
        "state": d.get("state"),
        "updated_at": d.get("updatedAt"),
        "closed_at": d.get("closedAt"),
    }


def _probe_gh(
    value: str, a: Anchor, runner: GhRunner, now: float, cache_only: bool = False
) -> None:
    repo, sep, num = value.partition("#")
    if not sep or repo.count("/") != 1 or not num.isdigit():
        a.warnings.append("expected <owner>/<repo>#<n>")
        return
    owner, name = repo.split("/")
    key = f"{owner}__{name}__{num}"
    cached = _cache_get(key, now, any_age=cache_only)
    if cached is not None and isinstance(cached.get("liveness"), dict):
        a.liveness.update(cached["liveness"], cached=True, fetched_at=_iso(cached["fetched_at"]))
        return
    if cache_only:
        # Liveness, not a warning: warnings become loose ends, and an unprobed
        # PR is a gap in this capture, not a problem with the work.
        a.liveness.update(kind=None, state=None, cached=False, not_probed="no cached gh answer")
        return

    shaped = None
    rc, out = runner(["gh", "pr", "view", num, "--repo", repo, "--json", _PR_FIELDS])
    if rc == 0:
        shaped = _parse_gh(out, _shape_pr)
    if shaped is None:
        # Issues and PRs share a number space; `pr view` on an issue number
        # fails the same way a missing PR does, so the issue view decides.
        rc, out = runner(["gh", "issue", "view", num, "--repo", repo, "--json", _ISSUE_FIELDS])
        if rc == 0:
            shaped = _parse_gh(out, _shape_issue)
    if shaped is None:
        a.liveness.update(kind=None, state=None)
        a.warnings.append("neither gh pr view nor gh issue view answered")
        return

    a.liveness.update(shaped, cached=False, fetched_at=_iso(now))
    err = _cache_put(key, now, {"liveness": shaped})
    if err:
        a.warnings.append(str(err))


def _parse_gh(out: str, shape: Callable[[dict], dict]) -> dict | None:
    try:
        d = json.loads(out)
    except ValueError:
        return None
    return shape(d) if isinstance(d, dict) else None


# --- file: / frontier: -----------------------------------------------------


def _probe_file(path: str, a: Anchor) -> None:
    try:
        st = Path(path).stat()
    except FileNotFoundError:
        a.liveness.update(exists=False, mtime=None)
        return
    except OSError as e:
        a.liveness.update(exists=None, mtime=None)
        a.warnings.append(f"stat failed ({e.__class__.__name__})")
        return
    a.liveness.update(exists=True, mtime=_iso(st.st_mtime))


# --- claude: / codex: / agent: ---------------------------------------------


def _probe_session(a: Anchor, registry: list[dict] | None) -> None:
    # CONTRACT: last_seen and peak_context come from transcripts, which
    # sessions.py reads; they stay None here and frontier fills them from its
    # SessionFacts rather than this module re-reading the store.
    lv = a.liveness
    lv.update(live=None, pid=None, last_seen=None, peak_context=None)
    sid = a.value
    if a.scheme == "agent":
        # A subagent runs inside its parent's process, so the parent's
        # registry record is the only liveness signal it has.
        sid = sid.split("/", 1)[0]
    if not sid:
        a.warnings.append("empty session id")
        return
    if registry is None:
        a.warnings.append("session registry unavailable")
        return
    hit = next((r for r in registry if str(r.get("sessionId", "")).startswith(sid)), None)
    if hit is not None:
        lv.update(live=True, pid=hit.get("pid"))
    elif a.scheme == "codex":
        # Codex writes no registry, so absence is not evidence it has exited.
        a.warnings.append("codex sessions are not in the registry; liveness unknown")
    else:
        lv["live"] = False


def _load_registry() -> tuple[list[dict] | None, str | None]:
    try:
        return list(seam.live_registry()), None
    except Exception as e:  # noqa: BLE001 -- another program's files; never fail a probe
        return None, f"registry read failed ({e.__class__.__name__})"


# --- entry points ----------------------------------------------------------


class _Ctx:
    """Shared across one liveness_many call: the registry is read once."""

    def __init__(self, repo_hints, gh_runner, now, registry, gh_cache_only=False):
        self.repo_hints = repo_hints
        self.gh_runner = gh_runner or _default_gh_runner
        # Answer gh probes from the cache at any age and never spawn gh: for
        # callers on a wall budget, where a cold cache would mean one network
        # round trip per PR anchor.
        self.gh_cache_only = gh_cache_only
        self.now = now
        self._registry = registry
        self._registry_loaded = registry is not None
        self._registry_err: str | None = None

    def registry(self) -> list[dict] | None:
        if not self._registry_loaded:
            self._registry, self._registry_err = _load_registry()
            self._registry_loaded = True
        return self._registry


def _probe(ref: str, ctx: _Ctx) -> Anchor:
    scheme, value = parse_ref(ref)
    a = Anchor(ref=ref, scheme=scheme, value=value)
    try:
        if scheme == "wt":
            _probe_wt(value, a, ctx)
        elif scheme == "branch":
            _probe_branch(value, a, ctx)
        elif scheme == "gh":
            now = ctx.now() if callable(ctx.now) else ctx.now
            _probe_gh(value, a, ctx.gh_runner, now, ctx.gh_cache_only)
        elif scheme in ("file", "frontier"):
            _probe_file(value, a)
        elif scheme in ("claude", "codex", "agent"):
            reg = ctx.registry()
            if ctx._registry_err:
                a.warnings.append(str(ctx._registry_err))
            _probe_session(a, reg)
        elif scheme == "ws":
            pass  # a workstream ref has no external state of its own
        else:
            a.warnings.append(f"unknown anchor scheme {scheme!r}")
    except Exception as e:  # noqa: BLE001 -- a probe must never fail the caller
        a.warnings.append(f"probe failed ({e.__class__.__name__}: {e})")
    return a


def liveness(
    ref: str,
    *,
    repo_hints: dict[str, str] | None = None,
    gh_runner: GhRunner | None = None,
    now: float | Callable[[], float] = time.time,
    registry: list[dict] | None = None,
    gh_cache_only: bool = False,
) -> Anchor:
    """Probe one anchor now. `now` (epoch seconds, or a callable) and
    `gh_runner` exist so tests can freeze the gh cache clock and never call
    the real gh; `registry` likewise replaces the live session registry.
    `gh_cache_only` answers gh refs from the cache alone, at any age."""
    return _probe(ref, _Ctx(repo_hints, gh_runner, now, registry, gh_cache_only))


def liveness_many(
    refs: Iterable[str],
    *,
    repo_hints: dict[str, str] | None = None,
    gh_runner: GhRunner | None = None,
    now: float | Callable[[], float] = time.time,
    registry: list[dict] | None = None,
    gh_cache_only: bool = False,
) -> list[Anchor]:
    ctx = _Ctx(repo_hints, gh_runner, now, registry, gh_cache_only)
    return [_probe(r, ctx) for r in refs]
