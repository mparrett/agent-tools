#!/usr/bin/env python3
"""Snapshot gate for reviews: pin what is reviewed, refuse to deliver against a moved target.

A review reads a fixed set of objects while the branch keeps moving. Two failure
modes follow: reviewing a mutable checkout (whatever `main` or the branch name
resolves to at the moment) and posting findings after the head has advanced past
them. This script makes the invariant in SKILL.md § Review invariants mechanical:

    review_target.py begin --pr 176 [--repo o/r] [--remote origin]
    review_target.py begin --worktree [--base origin/main]
    review_target.py check [--pr 176 | --worktree]
    review_target.py post --pr 176 -- gh pr review 176 --approve --body "..."

`begin` records the PR's base ref and the base and head OIDs from GitHub,
fetches the pull ref and the base into `refs/review/pr-N/{head,base}`, refuses
(exit 2) unless the fetched head is the recorded OID, computes the merge base,
writes a snapshot under `<git-common-dir>/review-snapshots/`, and prints the
diff command to review: explicit objects, never a branch name. `check` reads
GitHub again: unchanged prints `Reviewed head: <oid>` (exit 0); a moved head
exits 3 with the delta to revalidate; a retargeted PR or a moved merge base
exits 4, which means start over. `post` runs `check` and only then the `gh`
command after `--`, so stale findings cannot be posted.

`--worktree` pins HEAD, the tracked diff (binary included), and the contents of
untracked files for self-review; `check --worktree` also refetches the base and
applies the same merge-base rule.

`--repo owner/name` selects the PR's repository for both the GitHub read and the
fetch: the remote whose URL names that repository, else the GitHub URL itself.

Stdlib, git, and gh only, so any agent can run it. Test seam: REVIEW_TARGET_GH
names a substitute for the gh executable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

EXIT_MISMATCH = 2  # fetched object is not what GitHub reported
EXIT_HEAD_MOVED = 3
EXIT_BASE_MOVED = 4  # retargeted, or the merge base moved: start over


def die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr)
    sys.exit(code)


def run(args: List[str], check: bool = True) -> str:
    r = subprocess.run(args, capture_output=True, text=True)
    if check and r.returncode != 0:
        die(f"{' '.join(args[:3])} failed: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout.strip()


def git(*args: str, check: bool = True) -> str:
    return run(["git", *args], check=check)


def gh_pr(pr: int, repo: Optional[str]) -> Dict:
    gh = os.environ.get("REVIEW_TARGET_GH", "gh")
    cmd = [
        gh,
        "pr",
        "view",
        str(pr),
        "--json",
        "number,url,baseRefName,baseRefOid,headRefOid,headRefName,state",
    ]
    if repo:
        cmd += ["--repo", repo]
    return json.loads(run(cmd))


def _repo_of_url(url: str) -> str:
    """owner/name from a remote URL or path, lower-cased, so --repo can be matched."""
    path = re.sub(r"^\w+://[^/]+/|^[\w.-]+@[\w.-]+:", "", url.strip())
    path = path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return "/".join(path.split("/")[-2:]).lower()


def fetch_source(repo: Optional[str], remote: str) -> str:
    """Where to fetch from: the named remote, or for --repo the remote that points at
    that repository, else its GitHub URL. Fetching by name from the wrong remote would
    pin the same-numbered PR of a different repository."""
    if not repo:
        return remote
    want = repo.lower()
    for line in git("remote", "-v").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1] == "(fetch)" and _repo_of_url(parts[1]) == want:
            return parts[0]
    return f"https://github.com/{repo}.git"


def snapshot_dir() -> Path:
    d = Path(git("rev-parse", "--git-common-dir")) / "review-snapshots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def snapshot_path(pr: Optional[int]) -> Path:
    return snapshot_dir() / (f"pr-{pr}.json" if pr else "worktree.json")


def load(path: Path) -> Dict:
    if not path.is_file():
        die(f"no snapshot at {path}; run `begin` first")
    return json.loads(path.read_text())


def save(path: Path, snap: Dict) -> None:
    path.write_text(json.dumps(snap, indent=2, sort_keys=True) + "\n")


def now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def worktree_identity() -> Dict[str, str]:
    """HEAD plus hashes of the tracked diff (binary included) and of every untracked
    file's contents, so any change to what a self-review saw is detectable without
    keeping the text. `git diff HEAD` alone omits untracked files entirely, and a
    status line only names them, so their contents are hashed separately."""
    head = git("rev-parse", "HEAD")
    status = run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"])
    diff = subprocess.run(
        ["git", "diff", "HEAD", "--binary", "--no-color"], capture_output=True
    ).stdout
    untracked = hashlib.sha256()
    for entry in sorted(e for e in status.split("\0") if e.startswith("?? ")):
        path = entry[3:]
        untracked.update(path.encode())
        try:
            untracked.update(Path(path).read_bytes())
        except OSError:
            untracked.update(b"<unreadable>")
    return {
        "head_oid": head,
        "status_hash": _sha(status.encode()),
        "diff_hash": _sha(diff),
        "untracked_hash": untracked.hexdigest()[:16],
    }


def fetch_base(source: str, base_ref: str, tag: str) -> str:
    """Fetch the base into a review ref and return its OID. A review ref, not the
    remote-tracking ref, so a URL source and a named remote behave the same."""
    ref = f"refs/review/{tag}/base"
    git("fetch", "--quiet", source, f"+{base_ref}:{ref}")
    return git("rev-parse", ref)


def begin_pr(pr: int, repo: Optional[str], remote: str) -> Dict:
    info = gh_pr(pr, repo)
    head, base_ref = info["headRefOid"], info["baseRefName"]
    source = fetch_source(repo, remote)
    review_ref = f"refs/review/pr-{pr}/head"
    git("fetch", "--quiet", source, f"+refs/pull/{pr}/head:{review_ref}")
    fetched = git("rev-parse", review_ref)
    if fetched != head:
        die(
            f"refusing to pin: GitHub reports head {head[:12]} but {review_ref} fetched as "
            f"{fetched[:12]} from {source}; the branch moved between the read and the fetch, "
            "or the fetch source is not the PR's repository. Run begin again.",
            EXIT_MISMATCH,
        )
    base_oid = fetch_base(source, base_ref, f"pr-{pr}")
    return {
        "kind": "pr",
        "pr": pr,
        "url": info.get("url"),
        "repo": repo,
        "source": source,
        "base_ref": base_ref,
        "base_oid": base_oid,
        "head_oid": head,
        "merge_base": git("merge-base", base_oid, head),
        "review_ref": review_ref,
        "pinned_at": now_utc(),
    }


def begin_worktree(base: str) -> Dict:
    ident = worktree_identity()
    remote, _, ref = base.partition("/")
    base_oid = fetch_base(remote, ref, "worktree") if ref else git("rev-parse", base)
    return {
        "kind": "worktree",
        "base": base,
        "base_oid": base_oid,
        "merge_base": git("merge-base", base_oid, ident["head_oid"]),
        "pinned_at": now_utc(),
        **ident,
    }


def _base_verdict(snap: Dict, base_oid: str, head: str) -> Optional[str]:
    """None when the objects that would merge are unchanged; else the start-over text."""
    if base_oid == snap["base_oid"]:
        return None
    merge_base = git("merge-base", base_oid, head)
    if merge_base == snap["merge_base"]:
        return None
    return (
        f"BASE MOVED: merge base was {snap['merge_base'][:12]}, now {merge_base[:12]}; "
        "the reviewed diff is no longer what would merge. Start over with begin."
    )


def check_pr(snap: Dict) -> int:
    info = gh_pr(snap["pr"], snap.get("repo"))
    head, base_ref, source = info["headRefOid"], info["baseRefName"], snap["source"]
    if base_ref != snap["base_ref"]:
        print(
            f"RETARGETED: base was {snap['base_ref']}, now {base_ref}; the reviewed diff was "
            "against a different target. Start over with begin."
        )
        return EXIT_BASE_MOVED
    base_oid = fetch_base(source, base_ref, f"pr-{snap['pr']}")
    if head != snap["head_oid"]:
        git(
            "fetch",
            "--quiet",
            source,
            f"+refs/pull/{snap['pr']}/head:{snap['review_ref']}",
            check=False,
        )
        merge_base = git("merge-base", base_oid, head, check=False) or "?"
        print(
            f"HEAD MOVED: pinned {snap['head_oid'][:12]}, GitHub now {head[:12]}. Post nothing.\n"
            f"delta to revalidate: git diff {snap['head_oid']}..{head}\n"
            + (
                "merge base also moved; start the review over.\n"
                if merge_base != snap["merge_base"]
                else "merge base unchanged; revalidate every finding against the delta, then run begin again.\n"
            )
            + f"then: review_target.py begin --pr {snap['pr']}"
            + (f" --repo {snap['repo']}" if snap.get("repo") else "")
        )
        return EXIT_HEAD_MOVED
    verdict = _base_verdict(snap, base_oid, head)
    if verdict:
        print(verdict)
        return EXIT_BASE_MOVED
    note = (
        f"; {base_ref} advanced to {base_oid[:12]}, merge base unchanged"
        if base_oid != snap["base_oid"]
        else ""
    )
    print(
        f"Reviewed head: {head}  (base {base_ref} @ {snap['base_oid'][:12]}, "
        f"pinned {snap['pinned_at']}{note})"
    )
    return 0


def check_worktree(snap: Dict) -> int:
    now = worktree_identity()
    keys = ("head_oid", "status_hash", "diff_hash", "untracked_hash")
    changed = [k for k in keys if now[k] != snap.get(k)]
    if changed:
        paths = git("status", "--porcelain=v1", "--untracked-files=all").splitlines()
        print(
            f"WORKTREE CHANGED since pin ({', '.join(changed)}). Re-review the changed paths:\n"
            + "\n".join(f"  {p}" for p in paths[:40])
        )
        return EXIT_HEAD_MOVED
    remote, _, ref = snap["base"].partition("/")
    base_oid = fetch_base(remote, ref, "worktree") if ref else git("rev-parse", snap["base"])
    verdict = _base_verdict(snap, base_oid, now["head_oid"])
    if verdict:
        print(verdict)
        return EXIT_BASE_MOVED
    note = (
        f"; {snap['base']} advanced to {base_oid[:12]}, merge base unchanged"
        if base_oid != snap["base_oid"]
        else ""
    )
    print(
        f"Reviewed head: {now['head_oid']}  (working diff {now['diff_hash']}, "
        f"pinned {snap['pinned_at']}{note})"
    )
    return 0


def tree_dir() -> Path:
    d = Path(git("rev-parse", "--git-common-dir")) / "review-worktrees"
    d.mkdir(parents=True, exist_ok=True)
    return d


def drop_tree(path: Path) -> None:
    """Delete a review worktree and clear its registration.

    Both halves are needed, and in this order. A worktree's registration
    outlives its directory, so deleting the directory alone leaves the entry
    behind and the next `worktree add` at that path fails with "missing but
    already registered"; and `prune` only clears entries whose directory is
    already gone, so it has to follow the delete rather than lead it.
    """
    shutil.rmtree(path, ignore_errors=True)
    run(["git", "worktree", "prune"], check=False)


def cmd_tree(args: argparse.Namespace) -> int:
    """Materialize the pinned head as a worktree to run executable checks in.

    Reviewing a diff needs only objects, but *running* anything needs a tree, and
    the checkout the reviewer happens to be sitting in is not it. That tree
    belongs to whoever is working in this clone, and on a shared checkout it is
    routinely on someone else's branch -- so a suite run there measures their
    work and reports it as the PR's.
    """
    path = tree_dir() / f"pr-{args.pr}"
    if args.remove:
        drop_tree(path)
        print(f"removed {path}")
        return 0

    head = load(snapshot_path(args.pr))["head_oid"]

    if path.is_dir():
        at = run(["git", "-C", str(path), "rev-parse", "HEAD"], check=False).strip()
        # HEAD alone does not say the tree matches it. Edits left by an earlier
        # check -- or by hand, mid-investigation -- survive a reuse that compares
        # only OIDs, and the next suite then measures something that is not the
        # pinned head. Ignored files are deliberately not dirt: build artifacts
        # are the repo's own declared scratch space, and keeping them across runs
        # is most of what reuse buys.
        dirty = run(["git", "-C", str(path), "status", "--porcelain"], check=False).strip()
        if at == head and not dirty:
            print(f"reusing {path} at {head[:12]}")
            return 0
        if dirty:
            print(
                f"replacing {path}, which has uncommitted changes:\n"
                + "\n".join(f"  {line}" for line in dirty.splitlines()[:10])
            )

    # Unconditional: the path may be absent yet still registered, which is the
    # state that makes `worktree add` fail.
    drop_tree(path)
    git("worktree", "add", "--detach", str(path), head)
    at = run(["git", "-C", str(path), "rev-parse", "HEAD"]).strip()
    if at != head:
        die(f"tree: worktree is at {at[:12]}, expected the pinned head {head[:12]}", 2)
    print(
        f"pr {args.pr} at {head[:12]}: {path}\n"
        f"run checks with: cd {path} && <the narrowest relevant check>\n"
        f"remove with: review_target.py tree --pr {args.pr} --remove"
    )
    return 0


def cmd_begin(args: argparse.Namespace) -> int:
    if args.worktree:
        snap = begin_worktree(args.base)
        save(snapshot_path(None), snap)
        print(
            f"pinned worktree: HEAD {snap['head_oid'][:12]}, base {snap['base']} @ "
            f"{snap['base_oid'][:12]}, merge base {snap['merge_base'][:12]}, "
            f"working diff {snap['diff_hash']}, untracked {snap['untracked_hash']}\n"
            f"review with: git diff {snap['merge_base']}..HEAD && git diff HEAD\n"
            f"snapshot: {snapshot_path(None)}"
        )
        return 0
    snap = begin_pr(args.pr, args.repo, args.remote)
    save(snapshot_path(args.pr), snap)
    print(
        f"pinned pr {args.pr}: base {snap['base_ref']} @ {snap['base_oid'][:12]}, "
        f"head {snap['head_oid'][:12]}, merge base {snap['merge_base'][:12]} "
        f"(fetched from {snap['source']})\n"
        f"review with: git diff {snap['merge_base']}..{snap['head_oid']}\n"
        f"snapshot: {snapshot_path(args.pr)}"
    )
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    snap = load(snapshot_path(None if args.worktree else args.pr))
    return check_worktree(snap) if snap["kind"] == "worktree" else check_pr(snap)


def cmd_post(args: argparse.Namespace) -> int:
    if not args.command:
        die("post: give the gh command after --")
    rc = cmd_check(args)
    if rc != 0:
        print("refusing to post against a moved target", file=sys.stderr)
        return rc
    return subprocess.run(args.command).returncode


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="mode", required=True)

    def target(p: argparse.ArgumentParser) -> None:
        g = p.add_mutually_exclusive_group(required=True)
        g.add_argument("--pr", type=int, help="pull request number")
        g.add_argument("--worktree", action="store_true", help="self-review of the working tree")
        p.add_argument("--repo", help="owner/name of the PR's repository, when not the cwd's")

    b = sub.add_parser("begin", help="pin the target and print the diff command")
    target(b)
    b.add_argument("--remote", default="origin", help="fetch source when --repo is not given")
    b.add_argument("--base", default="origin/main", help="--worktree: the ref to diff against")
    b.set_defaults(fn=cmd_begin)
    c = sub.add_parser("check", help="has the target moved since begin?")
    target(c)
    c.set_defaults(fn=cmd_check)
    t = sub.add_parser("tree", help="worktree at the pinned head, to run checks in")
    t.add_argument("--pr", type=int, required=True, help="pull request number")
    t.add_argument("--remove", action="store_true", help="delete the worktree instead")
    t.set_defaults(fn=cmd_tree)
    p = sub.add_parser("post", help="check, then run the gh command after --")
    target(p)
    p.add_argument("command", nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_post)
    args = ap.parse_args(argv)
    if getattr(args, "command", None) and args.command[0] == "--":
        args.command = args.command[1:]
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
