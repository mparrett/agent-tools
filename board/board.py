#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["pyyaml"]
# ///
"""A work board kept as markdown cards on a git branch that is never merged.

Each card is `cards/<subject>--<owner>.md`: YAML frontmatter and a short note.
`README.md` on the branch is generated from the cards, so the branch's page on a
git host is the board. Writes go through a temporary index and push straight to
the branch; the caller's checkout and working tree are never touched.

    board init                          # create the branch with board.yml and README
    board ls [SUBJECT]                  # the cards, read from the remote branch
    board set SUBJECT --status S        # write or update your card
    board done SUBJECT                  # remove your card
    board prune                         # remove cards long past their ttl
    board render [--html]               # print the generated README (or HTML)
    board squash                        # replace the branch's history with one commit

The owner comes from --owner, $BOARD_OWNER, or `git config board.owner`.
"""

import argparse
import datetime as dt
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

CARDS = "cards"
CONFIG = "board.yml"
README = "README.md"
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")
REQUIRED = ("title", "subject", "owner", "status", "updated")
DEFAULT_CONFIG = {
    "title": "Work board",
    "statuses": ["claimed", "in-progress", "in-review", "blocked", "parked"],
    "ttl": "3d",
    "prune_after": "14d",
    "subject_url": None,
    "subject_check": None,
    "links": {},
}
RULES = """\
One card per owner per piece of work, `cards/<subject>--<owner>.md`, pushed straight to this
branch. A card is the owner's own word on where their work stands; it is optional, and sign-off
on what to build happens elsewhere. Before starting, check with `board ls <subject>` (or
`git ls-tree --name-only <remote>/<branch> cards/`): a card means someone has it, so ask them.
Two cards on one subject are **contested**; the earlier `since` keeps the work. A card past
`updated` + `ttl` is **stale**: a question for its owner, not a free claim. Delete your card when
the work merges or is dropped. This file is generated: never edit or merge it, regenerate it.
"""
CARD_FORMAT = """\
```markdown
---
title: Short name of the work
subject: the-slug          # required; the same slug for everyone on this work
owner: you                 # required; a person
session: agent-id          # optional; the agent working for the owner
status: in-progress        # required; one of the columns below
since: 2026-10-25T14:02:11Z
updated: 2026-10-25T16:40:05Z # required
ttl: 3d                    # optional; the board's default otherwise
links:                     # optional; any keys
  branch: my-branch
  pr: 123
---

One or two lines on where it stands.
```
"""


class BoardError(Exception):
    pass


def git(*args, env=None, check=True, input=None):
    run = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
        input=input,
    )
    if check and run.returncode != 0:
        raise BoardError(f"git {' '.join(args)}: {run.stderr.strip()}")
    return run


def now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def stamp(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value):
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day, tzinfo=dt.timezone.utc)
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            return parse_time(dt.datetime.fromisoformat(text))
        except ValueError:
            pass
        try:
            return parse_time(dt.date.fromisoformat(text))
        except ValueError:
            pass
    raise BoardError(f"not a time: {value!r}")


def parse_span(value):
    if isinstance(value, dt.timedelta):
        return value
    match = re.fullmatch(r"(\d+)\s*([hdw])", str(value).strip())
    if not match:
        raise BoardError(f"not a span (like 12h, 3d, 2w): {value!r}")
    n, unit = int(match[1]), match[2]
    return (
        dt.timedelta(hours=n) if unit == "h" else dt.timedelta(days=n * (7 if unit == "w" else 1))
    )


def ago(delta):
    hours = int(delta.total_seconds() // 3600)
    return f"{hours // 24}d" if hours >= 24 else (f"{hours}h" if hours > 0 else "<1h")


def parse_card(text, path=""):
    match = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    if not match:
        raise BoardError(f"{path}: no frontmatter")
    try:
        meta = yaml.safe_load(match[1]) or {}
    except yaml.YAMLError as err:
        raise BoardError(f"{path}: frontmatter is not YAML ({err.problem})") from None
    if not isinstance(meta, dict):
        raise BoardError(f"{path}: frontmatter is not a mapping")
    missing = [k for k in REQUIRED if not meta.get(k)]
    if missing:
        raise BoardError(f"{path}: missing {', '.join(missing)}")
    try:
        parse_time(meta["updated"])
        if meta.get("since"):
            parse_time(meta["since"])
        if meta.get("ttl"):
            parse_span(meta["ttl"])
    except BoardError as err:
        raise BoardError(f"{path}: {err}") from None
    if not isinstance(meta.get("links") or {}, dict):
        raise BoardError(f"{path}: links is not a mapping")
    meta["note"] = match[2].strip()
    return meta


def dump_card(card):
    meta = {k: v for k, v in card.items() if k != "note" and v is not None}
    for key in ("since", "updated"):
        if isinstance(meta.get(key), dt.datetime):
            meta[key] = stamp(meta[key])
    front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
    note = card.get("note", "").strip()
    return f"---\n{front}\n---\n" + (f"\n{note}\n" if note else "")


def card_path(subject, owner):
    return f"{CARDS}/{subject}--{owner}.md"


class Board:
    def __init__(self, remote="origin", branch="board", repo="."):
        self.remote, self.branch, self.repo = remote, branch, repo
        self.ref = f"refs/remotes/{remote}/{branch}"

    def git(self, *args, **kw):
        return git("-C", self.repo, *args, **kw)

    def fetch(self):
        # Several agents can share one checkout, so each fetch lands on a ref of its own, and
        # --refmap= stops git also updating the remote-tracking ref, whose lock they would race on.
        private = f"refs/board-fetch/{os.getpid()}"
        run = self.git(
            "fetch",
            "-q",
            "--refmap=",
            self.remote,
            f"+refs/heads/{self.branch}:{private}",
            check=False,
        )
        if run.returncode != 0:
            if "couldn't find remote ref" in run.stderr:
                raise BoardError(f"no branch {self.branch!r} on {self.remote}; run `board init`")
            raise BoardError(run.stderr.strip())
        commit = self.git("rev-parse", private).stdout.strip()
        self.git("update-ref", "-d", private)
        self.track(commit)
        return commit

    def track(self, commit):
        self.git("update-ref", self.ref, commit, check=False)

    def files(self, commit):
        out = self.git("ls-tree", "-r", "--name-only", commit).stdout
        return [line for line in out.splitlines() if line]

    def show(self, commit, path):
        return self.git("show", f"{commit}:{path}").stdout

    def config(self, commit):
        loaded = yaml.safe_load(self.show(commit, CONFIG)) or {}
        return {**DEFAULT_CONFIG, **loaded}

    def cards(self, commit):
        cards, bad = [], []
        for path in self.files(commit):
            if path.startswith(f"{CARDS}/") and path.endswith(".md"):
                try:
                    card = parse_card(self.show(commit, path), path)
                    card["path"] = path
                    cards.append(card)
                except BoardError as err:
                    bad.append(str(err))
        return cards, bad

    def documented(self, rule, subjects):
        """The subjects whose `subject_check` file exists at the rule's ref, in one git call."""
        subjects = sorted(subjects)
        specs = [f"{rule['ref']}:{rule['path'].format(subject=s)}" for s in subjects]
        out = self.git("cat-file", "--batch-check", input="".join(f"{x}\n" for x in specs))
        found = [not line.endswith(" missing") for line in out.stdout.splitlines()]
        return {s for s, ok in zip(subjects, found) if ok}

    def linkable(self, config, cards):
        """The subjects to link in the README, or None when there is no rule to check them by."""
        rule = config.get("subject_check")
        return self.documented(rule, {c["subject"] for c in cards}) if rule else None

    def commit(self, parent, changes, message, config=None):
        """Build a commit from `parent` with `changes` ({path: text or None}) and a fresh README."""
        with tempfile.TemporaryDirectory() as tmp:
            env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
            if parent:
                self.git("read-tree", parent, env=env)
            for path, text in changes.items():
                if text is None:
                    self.git("update-index", "--force-remove", path, env=env)
                else:
                    blob = self.git("hash-object", "-w", "--stdin", input=text).stdout.strip()
                    self.git(
                        "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", env=env
                    )
            tree = self.git("write-tree", env=env).stdout.strip()
            config = config or self.config(tree)
            cards, bad = self.cards(tree)
            known = self.linkable(config, cards)
            readme = render(config, cards, self.remote, self.branch, bad=bad, documented=known)
            blob = self.git("hash-object", "-w", "--stdin", input=readme).stdout.strip()
            self.git("update-index", "--add", "--cacheinfo", f"100644,{blob},{README}", env=env)
            tree = self.git("write-tree", env=env).stdout.strip()
        args = ["commit-tree", tree, "-m", message] + (["-p", parent] if parent else [])
        return self.git(*args).stdout.strip()

    def push(self, commit, lease=None):
        args = ["push", "-q", self.remote, f"{commit}:refs/heads/{self.branch}"]
        if lease is not None:
            args.insert(2, f"--force-with-lease=refs/heads/{self.branch}:{lease}")
        run = self.git(*args, check=False)
        return run.returncode == 0, run.stderr.strip()

    def update(self, change, message, attempts=12):
        """Apply `change(parent) -> {path: text or None}` and push, retrying on a moved branch.

        Only the caller's own paths change and README is regenerated each time, so
        replaying the change onto a fresh fetch is always safe.
        """
        for attempt in range(attempts):
            if attempt:
                time.sleep(random.uniform(0.05, 0.25) * attempt)
            parent = self.fetch()
            changes = change(parent)
            if not changes:
                return parent
            commit = self.commit(parent, changes, message)
            ok, err = self.push(commit)
            if ok:
                self.track(commit)
                return commit
            if "rejected" not in err and "fetch first" not in err and "non-fast-forward" not in err:
                raise BoardError(err)
        raise BoardError(f"the board kept moving; gave up after {attempts} attempts")


def render(config, cards, remote="origin", branch="board", at=None, bad=(), documented=None):
    at = at or now()
    default_ttl = parse_span(config["ttl"])
    columns = list(config["statuses"])
    by_subject = {}
    for card in cards:
        by_subject.setdefault(card["subject"], []).append(card)
    lines = [f"# {config['title']}", ""]
    lines.append(f"Generated {stamp(at)} from {len(cards)} card(s). Do not edit; regenerate.")
    lines.append("")
    contested = {s for s, cs in by_subject.items() if len(cs) > 1}
    stale = [
        c for c in cards if at - parse_time(c["updated"]) > parse_span(c.get("ttl") or default_ttl)
    ]
    summary = [f"{len(cards)} in flight"]
    if contested:
        summary.append(f"{len(contested)} contested")
    if stale:
        summary.append(f"{len(stale)} stale")
    lines += ["**" + " · ".join(summary) + "**", ""]
    extra = sorted({c["status"] for c in cards} - set(columns))
    for status in columns + extra:
        group = sorted(
            (c for c in cards if c["status"] == status),
            key=lambda c: (c["subject"], parse_time(c.get("since") or c["updated"])),
        )
        heading = status if status in columns else f"{status} (not a board status)"
        lines += [f"## {heading} ({len(group)})", ""]
        if not group:
            lines += ["_None._", ""]
            continue
        lines += ["| Work | Owner | Updated | Links | Note |", "|---|---|---|---|---|"]
        for card in group:
            lines.append(row(card, config, by_subject, contested, at, default_ttl, documented))
        lines.append("")
    if bad:
        lines += [f"## Cards that don't parse ({len(bad)})", ""]
        lines += [f"- {cell(err)}" for err in bad] + [""]
        lines += ["Each is left out of the board until its owner fixes or rewrites it.", ""]
    lines += ["## How this board works", "", RULES, "### Card format", "", CARD_FORMAT]
    return "\n".join(lines).rstrip() + "\n"


def cell(text):
    return str(text).replace("|", "\\|").replace("\n", " ")


def link(key, value, config):
    template = (config.get("links") or {}).get(key)
    label = f"{key} {value}" if key not in ("pr", "issue") else f"#{value}"
    if isinstance(value, list):
        return f"{key}: " + ", ".join(cell(v) for v in value)
    return f"[{cell(label)}]({template.format(value=value)})" if template else cell(label)


def row(card, config, by_subject, contested, at, default_ttl, documented=None):
    subject = card["subject"]
    work = f"**{cell(card['title'])}**<br>`{subject}`"
    if config.get("subject_url") and (documented is None or subject in documented):
        work = f"**{cell(card['title'])}**<br>[`{subject}`]({config['subject_url'].format(subject=subject)})"
    flags = []
    if subject in contested:
        since = {
            c["owner"]: parse_time(c.get("since") or c["updated"]) for c in by_subject[subject]
        }
        earliest = min(since.values())
        first = sorted(o for o, t in since.items() if t == earliest)
        if len(first) > 1:
            mark = "tied" if card["owner"] in first else f"after {' and '.join(first)}"
        else:
            mark = "keeps it" if card["owner"] == first[0] else f"after {first[0]}"
        flags.append(f"⚠️ contested, {mark}")
    age = at - parse_time(card["updated"])
    if age > parse_span(card.get("ttl") or default_ttl):
        flags.append(f"🕸️ stale {ago(age)}")
    if flags:
        work += "<br>" + "<br>".join(flags)
    owner = cell(card["owner"]) + (
        f"<br><sub>{cell(card['session'])}</sub>" if card.get("session") else ""
    )
    links = [
        link(k, v, config) for k, v in (card.get("links") or {}).items() if v not in (None, "")
    ]
    note = card.get("note", "").split("\n\n")[0]
    return f"| {work} | {owner} | {ago(age)} ago | {'<br>'.join(links)} | {cell(note)} |"


def resolve_owner(args, board):
    owner = args.owner or os.environ.get("BOARD_OWNER")
    if not owner:
        owner = board.git("config", "board.owner", check=False).stdout.strip()
    if not owner or not SLUG.match(owner):
        raise BoardError("set an owner slug: --owner, $BOARD_OWNER or `git config board.owner`")
    return owner


def check_subject(board, config, subject, allow_new):
    if not SLUG.match(subject):
        raise BoardError(f"subject {subject!r} is not a slug (lowercase, digits, hyphens)")
    rule = config.get("subject_check")
    if not rule or allow_new:
        return
    if not board.documented(rule, {subject}):
        path = rule["path"].format(subject=subject)
        raise BoardError(f"no {path} at {rule['ref']}; check the spelling, or pass --new-subject")


def cmd_init(board, args):
    if (
        board.git(
            "ls-remote", "--exit-code", board.remote, f"refs/heads/{board.branch}", check=False
        ).returncode
        == 0
    ):
        raise BoardError(f"{board.remote}/{board.branch} already exists")
    config = dict(DEFAULT_CONFIG, title=args.title)
    text = yaml.safe_dump({k: v for k, v in config.items() if v not in (None, {})}, sort_keys=False)
    commit = board.commit(None, {CONFIG: text}, "chore: start the work board", config=config)
    ok, err = board.push(commit)
    if not ok:
        raise BoardError(err)
    print(
        f"created {board.remote}/{board.branch}; edit {CONFIG} there to add link and subject rules"
    )


def cmd_ls(board, args):
    commit = board.fetch()
    config = board.config(commit)
    cards, bad = board.cards(commit)
    if args.subject:
        cards = [c for c in cards if c["subject"] == args.subject]
    for err in bad:
        print(f"warning: {err}", file=sys.stderr)
    if not cards:
        print("no cards" + (f" for {args.subject}" if args.subject else ""))
        return 0
    at, default_ttl = now(), parse_span(config["ttl"])
    for card in sorted(cards, key=lambda c: (c["subject"], c["owner"])):
        age = at - parse_time(card["updated"])
        stale = " stale" if age > parse_span(card.get("ttl") or default_ttl) else ""
        print(
            f"{card['subject']:<28} {card['owner']:<10} {card['status']:<12} {ago(age):>4}{stale}  {card['title']}"
        )
    return 3 if args.subject else 0


def cmd_set(board, args):
    owner = resolve_owner(args, board)
    path = card_path(args.subject, owner)

    def change(parent):
        config = board.config(parent)
        check_subject(board, config, args.subject, args.new_subject)
        statuses = config["statuses"]
        if args.status and args.status not in statuses:
            raise BoardError(f"status {args.status!r} is not one of {', '.join(statuses)}")
        old = {}
        if path in board.files(parent):
            try:
                old = parse_card(board.show(parent, path), path)
            except BoardError as err:
                if not args.status:
                    raise BoardError(f"{err}; pass --status to rewrite your card") from None
                print(f"warning: rewriting your unreadable card: {err}", file=sys.stderr)
        elif not args.status:
            raise BoardError("a new card needs --status")
        t = now()
        card = {
            "title": args.title or old.get("title") or args.subject,
            "subject": args.subject,
            "owner": owner,
            "session": args.session or old.get("session") or os.environ.get("BOARD_SESSION"),
            "status": args.status or old["status"],
            "since": old.get("since") or stamp(t),
            "updated": stamp(t),
        }
        card.update({k: v for k, v in old.items() if k not in card and k not in ("note", "path")})
        if args.ttl:
            parse_span(args.ttl)
            card["ttl"] = args.ttl
        links = dict(old.get("links") or {})
        for pair in args.link or []:
            key, _, value = pair.partition("=")
            links[key] = int(value) if value.isdigit() else (value or None)
        card["links"] = {k: v for k, v in links.items() if v is not None} or None
        card["note"] = args.note if args.note is not None else old.get("note", "")
        return {path: dump_card(card)}

    verb = "update" if args.status is None else args.status
    board.update(change, f"chore: {verb} {args.subject} ({owner})")
    print(f"wrote {path}")
    commit = board.fetch()
    rivals = [
        c for c in board.cards(commit)[0] if c["subject"] == args.subject and c["owner"] != owner
    ]
    for rival in rivals:
        print(
            f"warning: {rival['owner']} also has {args.subject} ({rival['status']}, since {rival.get('since')}); talk to them",
            file=sys.stderr,
        )
    return 2 if rivals else 0


def cmd_done(board, args):
    owner = resolve_owner(args, board)
    path = card_path(args.subject, owner)

    def change(parent):
        if path not in board.files(parent):
            raise BoardError(f"no card {path}")
        return {path: None}

    board.update(change, f"chore: done {args.subject} ({owner})")
    print(f"removed {path}")


def cmd_prune(board, args):
    removed = []

    def change(parent):
        config = board.config(parent)
        default_ttl, grace = (
            parse_span(config["ttl"]),
            parse_span(args.after or config["prune_after"]),
        )
        at = now()
        removed[:] = [
            c["path"]
            for c in board.cards(parent)[0]
            if at - parse_time(c["updated"]) > parse_span(c.get("ttl") or default_ttl) + grace
        ]
        return {p: None for p in removed}

    board.update(change, "chore: prune stale cards")
    print("\n".join(f"pruned {p}" for p in removed) or "nothing to prune")


def cmd_render(board, args):
    commit = board.fetch()
    cards, bad = board.cards(commit)
    config = board.config(commit)
    known = board.linkable(config, cards)
    text = render(config, cards, board.remote, board.branch, bad=bad, documented=known)
    if args.html:
        text = html_page(text)
    sys.stdout.write(text)


def html_page(markdown_text):
    gh = subprocess.run(
        ["gh", "api", "markdown", "-f", f"text={markdown_text}", "-f", "mode=gfm"],
        capture_output=True,
        text=True,
    )
    if gh.returncode != 0:
        raise BoardError(f"gh api markdown: {gh.stderr.strip()}")
    return page(gh.stdout)


CSS = "https://cdnjs.cloudflare.com/ajax/libs/github-markdown-css/5.8.1/github-markdown"
# Each stylesheet's media query decides whether it applies: Auto follows the system,
# Light and Dark pin one. The page background is read back from the content so the
# margins match it in either theme.
THEME_SCRIPT = """
const modes = { auto: ["(prefers-color-scheme: light)", "(prefers-color-scheme: dark)"],
  light: ["all", "not all"], dark: ["not all", "all"] };
const order = ["auto", "light", "dark"];
const button = document.getElementById("theme");
let mode = "auto";
try { mode = localStorage.getItem("board-theme") || "auto"; } catch (e) {}
function apply() {
  if (!modes[mode]) mode = "auto";
  document.getElementById("light").media = modes[mode][0];
  document.getElementById("dark").media = modes[mode][1];
  button.textContent = "Theme: " + mode[0].toUpperCase() + mode.slice(1);
  requestAnimationFrame(() => {
    document.documentElement.style.background =
      getComputedStyle(document.querySelector(".markdown-body")).backgroundColor;
  });
}
button.addEventListener("click", () => {
  mode = order[(order.indexOf(mode) + 1) % order.length];
  try { localStorage.setItem("board-theme", mode); } catch (e) {}
  apply();
});
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", apply);
apply();
"""


def page(body_html):
    return (
        "<!doctype html><meta charset=utf-8><meta name=color-scheme content='light dark'>"
        "<meta name=viewport content='width=device-width, initial-scale=1'><title>Work board</title>"
        f'<link id=light rel=stylesheet href="{CSS}-light.min.css" media="(prefers-color-scheme: light)">'
        f'<link id=dark rel=stylesheet href="{CSS}-dark.min.css" media="(prefers-color-scheme: dark)">'
        "<style>body{margin:0}main{max-width:1100px;margin:0 auto;padding:32px 16px}"
        "#theme{position:fixed;top:12px;right:12px;font:inherit;font-size:13px;padding:4px 10px;"
        "border-radius:6px;border:1px solid #8888;background:transparent;color:inherit;cursor:pointer}</style>"
        '<body class="markdown-body"><button id=theme type=button>Theme: Auto</button>'
        f"<main>{body_html}</main><script>{THEME_SCRIPT}</script></body>\n"
    )


def cmd_squash(board, args):
    old = board.fetch()
    tree = board.git("rev-parse", f"{old}^{{tree}}").stdout.strip()
    commit = board.git(
        "commit-tree", tree, "-m", f"chore: squash the board at {stamp(now())}"
    ).stdout.strip()
    ok, err = board.push(commit, lease=old)
    if not ok:
        raise BoardError(f"the board moved during the squash; nothing changed ({err})")
    board.track(commit)
    print(f"squashed {board.remote}/{board.branch} to one commit")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="board", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--remote", default=os.environ.get("BOARD_REMOTE", "origin"))
    parser.add_argument("--branch", default=os.environ.get("BOARD_BRANCH", "board"))
    parser.add_argument("-C", dest="repo", default=".", help="run in this repository")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="create the board branch")
    p.add_argument("--title", default=DEFAULT_CONFIG["title"])
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("ls", help="list cards; with SUBJECT, exit 3 if it has any")
    p.add_argument("subject", nargs="?")
    p.set_defaults(fn=cmd_ls)

    p = sub.add_parser(
        "set", help="write or update your card; exit 2 if someone else has the subject"
    )
    p.add_argument("subject")
    p.add_argument("--status")
    p.add_argument("--title")
    p.add_argument("--note")
    p.add_argument("--ttl")
    p.add_argument("--session")
    p.add_argument("--link", action="append", metavar="KEY=VALUE", help="repeatable; KEY= removes")
    p.add_argument("--owner")
    p.add_argument("--new-subject", action="store_true", help="skip the board's subject check")
    p.set_defaults(fn=cmd_set)

    p = sub.add_parser("done", help="remove your card")
    p.add_argument("subject")
    p.add_argument("--owner")
    p.set_defaults(fn=cmd_done)

    p = sub.add_parser("prune", help="remove cards past ttl plus prune_after")
    p.add_argument("--after", help="override the board's prune_after")
    p.set_defaults(fn=cmd_prune)

    p = sub.add_parser("render", help="print the generated README")
    p.add_argument(
        "--html", action="store_true", help="a standalone HTML page, via `gh api markdown`"
    )
    p.set_defaults(fn=cmd_render)

    p = sub.add_parser(
        "squash", help="replace the branch's history with one commit (--force-with-lease)"
    )
    p.set_defaults(fn=cmd_squash)

    args = parser.parse_args(argv)
    board = Board(args.remote, args.branch, args.repo)
    try:
        return args.fn(board, args) or 0
    except BoardError as err:
        print(f"board: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
