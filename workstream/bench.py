"""Time the workstream commands against copies of the stores, and count the
processes each one starts.

    uv run --directory workstream python bench.py --ws <name> [--gh stub|warm|slow|live]

Nothing real is written. Each command gets its own copy of the weft store and
a GitHub cache in a temporary directory, `timeline` writes there, and `attach --new` lands in
the copied store. Shims for git, gh and weft go first on PATH: they count
each call and pass it on, except that gh is stubbed to fail unless
`--gh live`.

gh modes:
  stub  every gh call fails at once; empty cache (the network-free floor)
  warm  every gh call fails at once; a copy of the real cache, stamped fresh
  slow  every gh call fails after --gh-delay seconds; empty cache
  live  real gh; empty cache (the network tail)

Run it once on main and once on a branch (`--cli` picks the entry point) and
compare. It prints timings and never records them: they belong to one machine.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

COUNTED = ("git", "gh", "weft")


def _real(name: str, path: str) -> str:
    found = shutil.which(name, path=path)
    if not found:
        sys.exit(f"bench: {name} not found on PATH")
    return found


def _shims(root: Path, gh_mode: str, gh_delay: float, path: str) -> Path:
    """A bin dir whose git, gh and weft append their name to calls.log, then
    run the real binary (or, for a stubbed gh, fail)."""
    bindir = root / "shims"
    bindir.mkdir()
    log = root / "calls.log"
    for name in COUNTED:
        if name == "gh" and gh_mode != "live":
            delay = f"sleep {gh_delay}\n" if gh_mode == "slow" else ""
            body = f'{delay}echo "bench: gh stubbed" >&2\nexit 1\n'
        else:
            # workstream's own override names the weft it runs; honour it.
            target = (name == "weft" and os.environ.get("WORKSTREAM_WEFT_BIN")) or _real(name, path)
            body = f'exec {shlex.quote(target)} "$@"\n'
        script = bindir / name
        script.write_text(f"#!/bin/sh\necho {name} >> {shlex.quote(str(log))}\n{body}")
        script.chmod(0o755)
    return bindir


def _copy_store(dst: Path) -> None:
    src = Path(os.environ.get("WEFT_HOME") or Path.home() / ".weft")
    if not (src / "log.jsonl").is_file():
        sys.exit(f"bench: no weft log under {src}")
    shutil.copytree(src, dst)


def _fill_cache(dst: Path, warm: bool) -> None:
    """An empty GitHub cache, or for `warm` a copy of the real one with every
    entry stamped fresh: a copied entry keeps its `fetched_at`, so one older
    than the cache's TTL would be a miss and warm would measure cold."""
    shutil.rmtree(dst, ignore_errors=True)
    dst.mkdir()
    src = _real_cache() / "gh"
    if not (warm and src.is_dir()):
        return
    (dst / "gh").mkdir()
    now = time.time()
    for f in src.glob("*.json"):
        try:
            entry = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(entry, dict) and "fetched_at" in entry:
            entry["fetched_at"] = now
            (dst / "gh" / f.name).write_text(json.dumps(entry), encoding="utf-8")


def _real_cache() -> Path:
    override = os.environ.get("WORKSTREAM_CACHE_DIR")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_CACHE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".cache") / "workstream"


_SESSION_LABELS = {"ls --session", "attach --new", "match"}


def _commands(ws: str, session: str | None, out: Path, run: int) -> list[tuple[str, list[str]]]:
    cmds: list[tuple[str, list[str]]] = []
    if session:
        cmds.append(("ls --session", ["ls", "--session", session]))
        cmds.append(
            (
                "attach --new",
                ["attach", "--new", "bench", "--name", f"bench-{run}", "--session", session],
            )
        )
        cmds.append(("match", ["match", "--session", session]))
    cmds += [
        ("show", ["show", ws]),
        ("frontier", ["frontier", ws]),
        ("sync <ws> --dry-run", ["sync", ws, "--dry-run"]),
        ("sync --dry-run", ["sync", "--dry-run"]),
        ("timeline", ["timeline", "-o", str(out / f"timeline-{run}.html")]),
    ]
    return cmds


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--ws", required=True, help="workstream for show, frontier and sync")
    ap.add_argument(
        "--session",
        default=os.environ.get("CLAUDE_CODE_SESSION_ID"),
        help="session for ls, attach and match (default: the caller's own; none skips them)",
    )
    ap.add_argument("--gh", choices=("stub", "warm", "slow", "live"), default="stub")
    ap.add_argument("--gh-delay", type=float, default=5.0, help="seconds per gh call for --gh slow")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--only", help="comma-separated labels to run, e.g. 'match,show'")
    ap.add_argument(
        "--cli",
        default=f"{shlex.quote(sys.executable)} -m workstream",
        help="the workstream entry point to time (default: this checkout's)",
    )
    ap.add_argument(
        "--timeout", type=float, default=300, help="seconds before a command is cut off (300)"
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    labels = {label for label, _ in _commands(args.ws, "s", Path(), 1)}
    unknown = set(args.only.split(",")) - labels if args.only else set()
    if unknown:
        sys.exit(f"bench: unknown --only label(s) {sorted(unknown)}; choose from {sorted(labels)}")
    if args.only and not args.session and set(args.only.split(",")) <= _SESSION_LABELS:
        sys.exit("bench: those commands need a session; pass --session")

    path = os.environ.get("PATH", "")
    rows = []
    with tempfile.TemporaryDirectory(prefix="ws-bench-") as tmp:
        root = Path(tmp)
        bindir = _shims(root, args.gh, args.gh_delay, path)
        cache = root / "cache"
        env = dict(
            os.environ,
            PATH=f"{bindir}{os.pathsep}{path}",
            WEFT_HOME=str(root / "weft"),
            WORKSTREAM_CACHE_DIR=str(cache),
            WORKSTREAM_WEFT_BIN=str(bindir / "weft"),
        )
        # attach's actor attribution and the hook's would otherwise name the caller.
        env.pop("WEFT_ACTOR", None)
        wanted = set(args.only.split(",")) if args.only else None
        log = root / "calls.log"
        for run in range(1, args.runs + 1):
            for label, argv in _commands(args.ws, args.session, root, run):
                if wanted and label not in wanted:
                    continue
                # Every command starts from the same store and cache, so a row
                # does not depend on which commands ran before it: attach --new
                # adds a workstream, and live gh answers fill the cache.
                shutil.rmtree(root / "weft", ignore_errors=True)
                _copy_store(root / "weft")
                _fill_cache(cache, args.gh == "warm")
                log.write_text("")
                started = time.monotonic()
                try:
                    proc = subprocess.run(
                        [*shlex.split(args.cli), *argv],
                        env=env,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        text=True,
                        timeout=args.timeout,
                    )
                    code, err = proc.returncode, proc.stderr
                except subprocess.TimeoutExpired:
                    code, err = "timeout", f"cut off after {args.timeout:g}s"
                took = time.monotonic() - started
                calls = log.read_text().split()
                rows.append(
                    {
                        "run": run,
                        "command": label,
                        "seconds": round(took, 2),
                        "exit": code,
                        **{name: calls.count(name) for name in COUNTED},
                        "stderr": (err or "").strip().splitlines()[-1:] if code else [],
                    }
                )
    if args.json:
        json.dump({"gh": args.gh, "rows": rows}, sys.stdout, indent=2)
        print()
        return 0
    print(f"gh: {args.gh}" + (f" ({args.gh_delay}s per call)" if args.gh == "slow" else ""))
    print(f"{'command':<22}{'run':>4}{'seconds':>9}{'git':>7}{'gh':>6}{'weft':>6}  exit")
    for r in rows:
        print(
            f"{r['command']:<22}{r['run']:>4}{r['seconds']:>9.2f}"
            f"{r['git']:>7}{r['gh']:>6}{r['weft']:>6}  {r['exit']}"
        )
        for line in r["stderr"]:
            print(f"    {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
