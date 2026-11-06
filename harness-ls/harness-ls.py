#!/usr/bin/env -S uv run --quiet --with rich python3
"""Survey what lands in Claude Code's effective context, ranked by token cost.

`du -sh` for the context window. Everything reported here is *predicted* by
statically resolving the same config the harness reads — it is an estimate, and
it drifts when Claude Code changes its loading rules. What the tool cannot see at
all (the server-side system prompt, built-in tool schemas, MCP schemas that only
exist after a server connects) is reported as unattributed rather than as zero.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.table import Table

# Rough bytes-per-token for English prose + markdown. Deliberately crude: the job
# of the default run is ranking sources against each other, not billing. Use
# --exact when a number needs to be defensible.
CHARS_PER_TOKEN = 4

# Hook events whose output is injected into the conversation. The rest of the
# events (PreToolUse, Stop, ...) can block or annotate but cost no context.
INJECTING_HOOKS = {
    "SessionStart": "once, at startup",
    "UserPromptSubmit": "EVERY turn",
    "PreCompact": "once per compaction",
}

# @-import recursion limit, matching the harness's own documented depth.
MAX_IMPORT_DEPTH = 5

# Instruction filenames read from each directory on the cwd->root walk.
INSTRUCTION_NAMES = ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md")

WHEN_ORDER = ["always", "per-turn", "on-demand", "behavioral"]
WHEN_LABELS = {
    "always": "ALWAYS-ON — every session pays this before you type",
    "per-turn": "PER-TURN — paid again on every message",
    "on-demand": "ON-DEMAND — not loaded yet; this is what it costs when it is",
    "behavioral": "BEHAVIORAL — no context cost, but it changes what happens",
}


@dataclass
class Source:
    """One thing that occupies (or shapes) the context window."""

    name: str
    when: str
    origin: str
    chars: int = 0
    detail: str = ""
    kind: str = ""
    # Individual entries behind an aggregate, for drill-down views.
    items: list = field(default_factory=list)

    @property
    def tokens(self):
        return round(self.chars / CHARS_PER_TOKEN)

    def as_dict(self):
        return {
            "name": self.name,
            "when": self.when,
            "origin": self.origin,
            "kind": self.kind,
            "chars": self.chars,
            "tokens": self.tokens,
            "detail": self.detail,
            "items": self.items,
        }


# --------------------------------------------------------------------------
# config discovery
# --------------------------------------------------------------------------


def claude_home():
    """The config dir, honoring CLAUDE_CONFIG_DIR the way the harness does."""
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))


def read_json(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def read_text(path):
    try:
        with open(path, errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def settings_layers(project_dir):
    """The four settings layers, lowest precedence first.

    Returned even when absent, so the audit view can show which layers are empty.
    """
    home = claude_home()
    layers = [
        ("enterprise", Path("/Library/Application Support/ClaudeCode/managed-settings.json")),
        ("user", home / "settings.json"),
    ]
    if project_dir:
        layers.append(("project", project_dir / ".claude" / "settings.json"))
        layers.append(("local", project_dir / ".claude" / "settings.local.json"))
    return [(name, path, read_json(path)) for name, path in layers if path]


def merged_settings(project_dir):
    """Shallow merge of the settings layers in precedence order.

    Shallow is enough for what this tool reads (hooks, mcpServers, enabledPlugins);
    the harness merges some keys more cleverly, which is noted in the audit view.
    """
    merged = {}
    for _, _, data in settings_layers(project_dir):
        merged.update(data)
    return merged


def parse_frontmatter(text):
    """Pull top-level keys out of a leading `---` block.

    Hand-rolled rather than pulling in a YAML dep: skills and commands only need
    `name` and `description`, but descriptions are frequently folded across lines
    or written as block scalars, so continuations have to be joined.
    """
    match = re.match(r"^---\r?\n(.*?)\r?\n---", text, re.S)
    if not match:
        return {}
    keys = {}
    current = None
    for line in match.group(1).splitlines():
        head = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if head:
            current = head.group(1)
            value = head.group(2).strip()
            # Block scalar markers carry no content of their own.
            keys[current] = "" if value in (">", "|", ">-", "|-") else value
        elif current and line.strip():
            keys[current] = (keys[current] + " " + line.strip()).strip()
        elif not line.strip():
            current = None
    return keys


def catalog_entry_chars(name, description):
    """What one catalog line costs once injected.

    Only the name and description reach the context — the rest of a SKILL.md
    frontmatter (allowed-tools, model, ...) does not, so counting whole
    frontmatter blocks overstates the bill.
    """
    return len(f"- {name}: {description}\n")


# --------------------------------------------------------------------------
# instructions (CLAUDE.md chain, imports, memory)
# --------------------------------------------------------------------------


def resolve_imports(path, base_dir, seen, depth=0):
    """Follow `@path` imports out of an instruction file.

    Returns [(path, chars)] for files pulled in transitively. Cycles are cut by
    `seen`, which the caller shares across the whole chain so a file imported from
    two places is still only billed once.
    """
    if depth >= MAX_IMPORT_DEPTH:
        return []
    # Self-seed the guard: a file that imports itself (directly or through a
    # ring) must not be walked twice even when the caller passed a bare set.
    seen.add(str(path))
    found = []
    for line in read_text(path).splitlines():
        # Imports sit at the start of a line; `@` mid-sentence is not an import,
        # and neither is an email address or a decorator inside a code fence.
        match = re.match(r"^\s{0,3}@([\w./~\\-]+)", line)
        if not match:
            continue
        raw = match.group(1)
        # Expand ~ against Path.home() rather than os.path.expanduser so imports
        # anchor to the same home the instruction walk stops at.
        target = Path.home() / raw[2:] if raw.startswith("~/") else Path(raw)
        if not target.is_absolute():
            target = (base_dir / target).resolve()
        if not target.is_file() or str(target) in seen:
            continue
        seen.add(str(target))
        found.append((target, len(read_text(target))))
        found.extend(resolve_imports(target, target.parent, seen, depth + 1))
    return found


def instruction_sources(project_dir):
    """The instruction chain: user-global, then every dir from cwd up to home."""
    sources = []
    seen = set()
    home = claude_home()

    user_md = home / "CLAUDE.md"
    if user_md.is_file():
        seen.add(str(user_md))
        chars = len(read_text(user_md))
        imports = resolve_imports(user_md, user_md.parent, seen)
        sources.append(
            Source(
                name="~/.claude/CLAUDE.md",
                when="always",
                origin="user",
                kind="instructions",
                chars=chars + sum(c for _, c in imports),
                detail=f"{len(imports)} import(s)" if imports else "",
                items=[{"path": str(p), "chars": c} for p, c in imports],
            )
        )

    if not project_dir:
        return sources

    # Walk cwd -> home. Above home the harness stops looking, and scanning / for
    # stray CLAUDE.md files would be both slow and wrong.
    stop = Path.home()
    chain = []
    current = project_dir.resolve()
    while True:
        chain.append(current)
        if current == stop or current == current.parent:
            break
        current = current.parent

    for directory in chain:
        for name in INSTRUCTION_NAMES:
            for candidate in (directory / name, directory / ".claude" / name):
                if not candidate.is_file() or str(candidate) in seen:
                    continue
                seen.add(str(candidate))
                chars = len(read_text(candidate))
                imports = resolve_imports(candidate, candidate.parent, seen)
                where = "project" if directory == project_dir.resolve() else "ancestor"
                sources.append(
                    Source(
                        name=display_path(candidate),
                        when="always",
                        origin=where,
                        kind="instructions",
                        chars=chars + sum(c for _, c in imports),
                        detail=f"{len(imports)} import(s)" if imports else "",
                        items=[{"path": str(p), "chars": c} for p, c in imports],
                    )
                )
    return sources


def project_slug(project_dir):
    """The ~/.claude/projects/ directory name for a path (slashes become dashes)."""
    return str(Path(project_dir).resolve()).replace("/", "-")


def memory_sources(project_dir):
    """MEMORY.md is always injected; the memories it indexes are recalled lazily."""
    if not project_dir:
        return []
    root = claude_home() / "projects" / project_slug(project_dir) / "memory"
    index = root / "MEMORY.md"
    if not index.is_file():
        return []
    out = [
        Source(
            name="memory/MEMORY.md",
            when="always",
            origin="user",
            kind="memory",
            chars=len(read_text(index)),
            detail=f"{len(read_text(index).splitlines())} entries",
        )
    ]
    bodies = [p for p in sorted(root.glob("*.md")) if p.name != "MEMORY.md"]
    if bodies:
        out.append(
            Source(
                name="memory bodies",
                when="on-demand",
                origin="user",
                kind="memory",
                chars=sum(len(read_text(p)) for p in bodies),
                detail=f"{len(bodies)} files, injected only when recalled",
                items=[{"path": p.name, "chars": len(read_text(p))} for p in bodies],
            )
        )
    return out


# --------------------------------------------------------------------------
# plugins
# --------------------------------------------------------------------------


def enabled_plugin_roots(project_dir):
    """Install paths of plugins that actually apply here.

    Two gates, both of which matter: `enabledPlugins` in the merged settings can
    switch a plugin off without uninstalling it, and an entry installed with
    project scope only applies inside that project.
    """
    installed = read_json(claude_home() / "plugins" / "installed_plugins.json")
    enabled = merged_settings(project_dir).get("enabledPlugins", {})
    here = str(project_dir.resolve()) if project_dir else None
    roots = []
    for plugin_id, entries in (installed.get("plugins") or {}).items():
        if not enabled.get(plugin_id, False):
            continue
        for entry in entries:
            if entry.get("scope") == "project" and entry.get("projectPath") != here:
                continue
            path = Path(entry.get("installPath", ""))
            if path.is_dir():
                roots.append((plugin_id, path))
                break
    return roots


# --------------------------------------------------------------------------
# catalogs (skills, commands, agents)
# --------------------------------------------------------------------------


def body_summary(text):
    """First meaningful line of a file, used when frontmatter has no description.

    Slash commands rarely carry frontmatter; the harness lists them by their
    opening line instead, heading marker stripped ("# Orient" -> "Orient").
    """
    body = re.sub(r"^---\r?\n.*?\r?\n---\r?\n", "", text, flags=re.S)
    for line in body.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped
    return ""


def collect_catalog(project_dir, subdir, pattern, kind):
    """Gather one catalog across user, project, and enabled-plugin scopes."""
    entries = []
    by_directory = pattern.endswith("SKILL.md")

    def add(path, origin):
        text = read_text(path)
        meta = parse_frontmatter(text)
        # Skills are identified by their containing directory, everything else by
        # filename; an explicit frontmatter `name` wins either way.
        default_name = path.parent.name if by_directory else path.stem
        name = meta.get("name") or default_name
        description = meta.get("description") or body_summary(text)
        entries.append(
            {
                "name": name,
                "origin": origin,
                "path": str(path),
                "chars": catalog_entry_chars(name, description),
                "described": bool(description),
            }
        )

    for base, origin in [(claude_home(), "user")] + (
        [(project_dir / ".claude", "project")] if project_dir else []
    ):
        for path in sorted((base / subdir).glob(pattern)) if (base / subdir).is_dir() else []:
            add(path, origin)

    for plugin_id, root in enabled_plugin_roots(project_dir):
        directory = root / subdir
        if directory.is_dir():
            for path in sorted(directory.glob(pattern)):
                add(path, f"plugin:{plugin_id}")

    if not entries:
        return None

    plugin_count = sum(1 for e in entries if e["origin"].startswith("plugin:"))
    detail = f"{len(entries)} entries"
    if plugin_count:
        detail += f" ({plugin_count} from plugins)"
    return Source(
        name=f"{kind} catalog",
        when="always",
        origin="mixed" if plugin_count else "user",
        kind=kind,
        chars=sum(e["chars"] for e in entries),
        detail=detail,
        items=sorted(entries, key=lambda e: -e["chars"]),
    )


def skill_body_source(project_dir, catalog):
    """Skill *bodies* — the real cost of invoking one, versus listing it."""
    if not catalog:
        return None
    total = sum(len(read_text(Path(e["path"]))) for e in catalog.items)
    return Source(
        name="skill bodies",
        when="on-demand",
        origin="mixed",
        kind="skills",
        chars=total,
        detail=f"{len(catalog.items)} files; one invocation costs its whole body",
    )


# --------------------------------------------------------------------------
# hooks, MCP, environment
# --------------------------------------------------------------------------


def hook_sources(project_dir):
    """Hooks that write into the conversation, separated from hooks that don't."""
    out = []
    for layer, path, data in settings_layers(project_dir):
        hooks = data.get("hooks") or {}
        if not hooks:
            continue
        injecting = {k: v for k, v in hooks.items() if k in INJECTING_HOOKS}
        other = {k: v for k, v in hooks.items() if k not in INJECTING_HOOKS}
        for event, matchers in injecting.items():
            commands = hook_commands(event, matchers, path)
            count = len(commands)
            when = "per-turn" if event == "UserPromptSubmit" else "always"
            out.append(
                Source(
                    name=f"hook: {event}",
                    when=when,
                    origin=layer,
                    kind="hooks",
                    # Output size is unknowable without running the hook, and
                    # running arbitrary commands to measure them is not this
                    # tool's job. Reported as unattributed, never as zero.
                    chars=0,
                    detail=f"{count} command(s) — injects {INJECTING_HOOKS[event]}, size unmeasured",
                    items=commands,
                )
            )
        if other:
            commands = [c for e, m in other.items() for c in hook_commands(e, m, path)]
            out.append(
                Source(
                    name="hooks (non-injecting)",
                    when="behavioral",
                    origin=layer,
                    kind="hooks",
                    detail=", ".join(sorted(other)),
                    items=commands,
                )
            )
    return out


def hook_commands(event, matchers, path):
    """Flatten a settings hook block into one row per command."""
    rows = []
    for matcher in matchers or []:
        for hook in matcher.get("hooks", []) or []:
            rows.append(
                {
                    "event": event,
                    "matcher": matcher.get("matcher", "") or "*",
                    "command": hook.get("command", hook.get("type", "?")),
                    "source": str(path),
                }
            )
    return rows


def mcp_sources(project_dir):
    """MCP servers from all three places they can be declared."""
    servers = []
    for layer, path, data in settings_layers(project_dir):
        for name in data.get("mcpServers") or {}:
            servers.append((name, layer, str(path)))
    if project_dir:
        for name in read_json(project_dir / ".mcp.json").get("mcpServers") or {}:
            servers.append((name, "project .mcp.json", str(project_dir / ".mcp.json")))
    for plugin_id, root in enabled_plugin_roots(project_dir):
        # A plugin can declare servers either in a standalone .mcp.json or inline
        # in its manifest; the official plugins use the manifest.
        for manifest in (root / ".mcp.json", root / ".claude-plugin" / "plugin.json"):
            for name in read_json(manifest).get("mcpServers") or {}:
                servers.append((name, f"plugin:{plugin_id}", str(manifest)))
    if not servers:
        return []
    return [
        Source(
            name="MCP servers",
            when="always",
            origin="mixed",
            kind="mcp",
            # Tool schemas arrive from the server at connect time, so their size
            # cannot be predicted from config alone.
            chars=0,
            detail=f"{len(servers)} server(s) — tool schemas unmeasurable offline",
            items=[{"name": n, "origin": o, "source": s} for n, o, s in servers],
        )
    ]


def env_source(project_dir):
    """The environment block: cwd, branch, git status snapshot, recent commits."""
    if not project_dir:
        return None

    def git(*args):
        try:
            done = subprocess.run(
                ["git", "-C", str(project_dir), *args],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return done.stdout if done.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            return ""

    status = git("status", "--short")
    log = git("log", "--oneline", "-5")
    if not log and not status:
        return None
    # The block also carries cwd, platform, model and shell lines; ~400 chars of
    # boilerplate is a closer floor than counting only the git output.
    chars = len(status) + len(log) + 400
    lines = len(status.splitlines())
    detail = f"{lines} changed file(s) in git status" if lines else "clean tree"
    return Source(
        name="environment block",
        when="always",
        origin="harness",
        kind="env",
        chars=chars,
        detail=detail,
    )


def settings_source(project_dir):
    """Behavioral settings — zero context cost, real behavioral weight."""
    out = []
    for layer, path, data in settings_layers(project_dir):
        if not data:
            continue
        perms = data.get("permissions") or {}
        rule_count = sum(len(v) for v in perms.values() if isinstance(v, list))
        bits = []
        if rule_count:
            bits.append(f"{rule_count} permission rules")
        for key in ("statusLine", "env", "additionalDirectories", "outputStyle", "model"):
            if data.get(key):
                bits.append(key)
        if not bits:
            continue
        out.append(
            Source(
                name=f"settings ({layer})",
                when="behavioral",
                origin=layer,
                kind="settings",
                detail=", ".join(bits),
                items=[{"path": str(path), "keys": sorted(data)}],
            )
        )
    return out


# --------------------------------------------------------------------------
# survey
# --------------------------------------------------------------------------


def survey(project_dir):
    sources = []
    sources += instruction_sources(project_dir)
    sources += memory_sources(project_dir)

    skills = collect_catalog(project_dir, "skills", "*/SKILL.md", "skills")
    commands = collect_catalog(project_dir, "commands", "*.md", "commands")
    agents = collect_catalog(project_dir, "agents", "*.md", "agents")
    for catalog in (skills, commands, agents):
        if catalog:
            sources.append(catalog)
    bodies = skill_body_source(project_dir, skills)
    if bodies:
        sources.append(bodies)

    sources += hook_sources(project_dir)
    sources += mcp_sources(project_dir)
    env = env_source(project_dir)
    if env:
        sources.append(env)
    sources += settings_source(project_dir)
    return sources


def count_exact(sources):
    """Re-count with the real tokenizer. Mutates each Source's char budget.

    Kept behind a flag: it needs a key and a network round trip per source, which
    would make the common case slow enough that nobody runs it casually.
    """
    try:
        import anthropic
    except ImportError:
        print(
            "--exact needs the anthropic package. Re-run with:\n"
            "  uv run --with rich --with anthropic harness-ls/harness-ls.py --exact",
            file=sys.stderr,
        )
        raise SystemExit(2)
    client = anthropic.Anthropic()
    for source in sources:
        text = source_text(source)
        if not text:
            continue
        result = client.messages.count_tokens(
            model="claude-opus-5",
            messages=[{"role": "user", "content": text}],
        )
        # Store back as chars so the single tokens property stays the one path.
        source.chars = result.input_tokens * CHARS_PER_TOKEN


def source_text(source):
    """Best-effort reconstruction of what a source contributes, for --exact."""
    if source.kind in ("instructions", "memory"):
        path = Path(os.path.expanduser(source.name.replace("~", str(Path.home()), 1)))
        return read_text(path) if path.is_file() else ""
    if source.items and all("name" in i for i in source.items):
        return "\n".join(f"- {i['name']}" for i in source.items)
    return ""


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def display_path(path):
    text = str(path)
    home = str(Path.home())
    return text.replace(home, "~", 1) if text.startswith(home) else text


def fmt_tokens(tokens):
    if tokens == 0:
        return "?"
    if tokens >= 1000:
        return f"{tokens / 1000:.1f}k"
    return str(tokens)


def render(console, sources, project_dir):
    where = display_path(project_dir) if project_dir else "user-global only (--no-project)"
    console.print(f"\n[bold]EFFECTIVE CONTEXT[/bold] — {where}\n")

    for when in WHEN_ORDER:
        group = [s for s in sources if s.when == when]
        if not group:
            continue
        group.sort(key=lambda s: -s.chars)
        table = Table(box=None, pad_edge=False, show_header=False)
        table.add_column("tokens", justify="right", style="bold", width=7)
        table.add_column("source", style="cyan", no_wrap=True)
        table.add_column("origin", style="dim", no_wrap=True)
        table.add_column("detail", style="dim", overflow="fold")
        for source in group:
            table.add_row(fmt_tokens(source.tokens), source.name, source.origin, source.detail)
        console.print(f"[dim]{WHEN_LABELS[when]}[/dim]")
        console.print(table)
        console.print()

    always = sum(s.tokens for s in sources if s.when == "always")
    per_turn = sum(s.tokens for s in sources if s.when == "per-turn")
    unknown = [s.name for s in sources if s.chars == 0 and s.when in ("always", "per-turn")]
    console.print(f"[bold]{fmt_tokens(always)}[/bold] predicted always-on", end="")
    if per_turn:
        console.print(f" · [bold]{fmt_tokens(per_turn)}[/bold] per turn", end="")
    console.print(
        "\n[dim]Unattributed: server-side system prompt, built-in tool schemas"
        + (f", {', '.join(unknown)}" if unknown else "")
        + ".\nEstimated at ~4 chars/token; use --exact for real counts.[/dim]\n"
    )


# Drill-down column order. Anything not listed here is appended alphabetically.
COLUMN_ORDER = ["tokens", "name", "origin", "described", "event", "path", "source", "matchers"]


def render_drilldown(console, sources, kind):
    group = [s for s in sources if s.kind == kind]
    if not group:
        console.print(f"[yellow]no sources of kind '{kind}'[/yellow]")
        return
    for source in group:
        console.print(f"\n[bold]{source.name}[/bold] [dim]({source.detail})[/dim]")
        if not source.items:
            continue
        rows = [dict(item) for item in source.items]
        for row in rows:
            if "chars" in row:
                row["tokens"] = round(row.pop("chars") / CHARS_PER_TOKEN)
        rows.sort(key=lambda r: -r.get("tokens", 0))

        # A column every row agrees on carries no information in a listing this
        # dense — the uniform value goes in the caption instead.
        columns, uniform = [], []
        for key in {k for row in rows for k in row}:
            values = {json.dumps(row.get(key), sort_keys=True) for row in rows}
            (uniform if len(values) == 1 and len(rows) > 1 else columns).append(key)
        columns.sort(key=lambda c: (COLUMN_ORDER.index(c) if c in COLUMN_ORDER else 99, c))

        table = Table(box=None, pad_edge=False)
        for column in columns:
            table.add_column(
                column,
                justify="right" if column == "tokens" else "left",
                style="cyan" if column == "name" else "dim",
                overflow="ellipsis",
                # Only the path-ish columns refuse to wrap. Marking every column
                # no_wrap leaves rich no slack, and it collapses a column to zero
                # width to compensate — the token counts, in practice.
                no_wrap=column in ("path", "source"),
                min_width={"name": 20, "command": 24, "event": 16}.get(column),
            )
        for row in rows:
            table.add_row(*[cell(row.get(column, ""), column) for column in columns])
        console.print(table)
        if uniform:
            shared = ", ".join(f"{k}={rows[0].get(k)}" for k in sorted(uniform))
            console.print(f"[dim]all entries: {shared}[/dim]")
    console.print()


def cell(value, column):
    if not isinstance(value, str):
        value = json.dumps(value)
    text = display_path(value)
    # Paths repeat their prefix down the column; the tail is what distinguishes
    # them, so trim from the left.
    if column in ("path", "source") and len(text) > 46:
        return "…" + text[-45:]
    return text if len(text) <= 90 else text[:87] + "..."


def main():
    parser = argparse.ArgumentParser(
        description="Survey what lands in Claude Code's effective context, ranked by cost."
    )
    parser.add_argument(
        "drilldown",
        nargs="?",
        choices=[
            "skills",
            "commands",
            "agents",
            "instructions",
            "hooks",
            "mcp",
            "settings",
            "memory",
        ],
        help="show the individual entries behind one source",
    )
    parser.add_argument("--project", metavar="DIR", help="survey DIR instead of the cwd")
    parser.add_argument("--no-project", action="store_true", help="user-global sources only")
    parser.add_argument(
        "--json", action="store_true", dest="as_json", help="machine-readable output"
    )
    parser.add_argument(
        "--exact", action="store_true", help="count tokens via the API, not chars/4"
    )
    parser.add_argument("--color", action="store_true", help="force ANSI color")
    args = parser.parse_args()

    project_dir = None if args.no_project else Path(args.project or os.getcwd())
    if project_dir and not project_dir.is_dir():
        print(f"not a directory: {project_dir}", file=sys.stderr)
        return 2

    sources = survey(project_dir)
    if args.exact:
        count_exact(sources)

    if args.as_json:
        payload = {
            "root": str(project_dir) if project_dir else None,
            "estimator": "exact" if args.exact else f"chars/{CHARS_PER_TOKEN}",
            "totals": {
                when: sum(s.tokens for s in sources if s.when == when) for when in WHEN_ORDER
            },
            "sources": [s.as_dict() for s in sources],
        }
        print(json.dumps(payload, indent=2))
        return 0

    console = Console(force_terminal=True if args.color else None)
    if args.drilldown:
        render_drilldown(console, sources, args.drilldown)
    else:
        render(console, sources, project_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
