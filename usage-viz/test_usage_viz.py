#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest", "playwright"]
# ///
"""Tests for usage-viz: the pure helpers, and the daily page rendered in Chromium.

The page is rendered from a synthetic payload, not from ccusage, so the suite needs
no logs and no network. Needs the chromium binary once:

    uvx playwright install chromium
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.machinery
import importlib.util
import subprocess
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent


def _load():
    # The script has no .py extension, so spec_from_file_location needs a loader.
    loader = importlib.machinery.SourceFileLoader("usage_viz", str(HERE / "usage-viz"))
    spec = importlib.util.spec_from_loader("usage_viz", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


uv = _load()


@pytest.mark.parametrize(
    "name, want",
    [
        ("claude-opus-5-5", "Opus 5.5"),
        ("claude-opus-5", "Opus 5"),
        # A dated snapshot shares its model's series.
        ("claude-haiku-4-5-20251001", "Haiku 4.5"),
        ("gpt-6.1-sol", "gpt-6.1-sol"),
    ],
)
def test_pretty_model(name, want):
    assert uv.pretty_model(name) == want


def test_project_label_folds_worktrees_and_subdirs():
    prefix = uv.sanitize("/Users/x/projects-new") + "-"
    known = sorted({"agent-tools", "agent"}, key=len, reverse=True)
    label = lambda rest: uv.project_label(prefix + rest, prefix, known)  # noqa: E731
    assert label("agent-tools") == "agent-tools"
    assert label("agent-tools-wt-fix") == "agent-tools"
    assert label("worktrees-agent-tools-import-usage-viz") == "agent-tools"
    assert label("agent-tools-claude-ls") == "agent-tools"
    # Longest prefix wins, so "agent" doesn't swallow "agent-tools".
    assert label("agent-other") == "agent"
    assert label("unknown-repo") == "unknown-repo"


def test_project_label_outside_projects_dir():
    prefix = uv.sanitize("/Users/x/projects-new") + "-"
    home = uv.sanitize(Path.home()) + "-"
    assert uv.project_label(home + "scratch", prefix, []) == "~/scratch"
    assert uv.project_label("-opt-elsewhere", prefix, []) == "-opt-elsewhere"


def test_project_labels_without_projects_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    prefix, known = uv.project_labels()
    assert known == []
    assert prefix == uv.sanitize(tmp_path / "projects-new") + "-"


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_commit_skips_gitignored_output(tmp_path, capsys):
    _git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text("out/\n")
    out = tmp_path / "out"
    out.mkdir()
    page = out / "page.html"
    page.write_text("x")
    uv.commit([page], "usage: test", argparse.Namespace(no_commit=False))
    assert "gitignored" in capsys.readouterr().err
    log = subprocess.run(["git", "-C", str(tmp_path), "log"], capture_output=True)
    assert log.returncode != 0  # still no commits


def test_commit_commits_only_its_files(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "other.txt").write_text("leave me")
    page = tmp_path / "page.html"
    page.write_text("x")
    uv.commit([page], "usage: test", argparse.Namespace(no_commit=False))
    files = subprocess.run(
        ["git", "-C", str(tmp_path), "ls-files"], capture_output=True, text=True
    ).stdout.split()
    assert files == ["page.html"]


def _payload(breakdown: bool) -> dict:
    start = dt.date(2026, 9, 1)
    rows = []
    for i in range(40):
        day = (start + dt.timedelta(days=i)).isoformat()
        tokens = 100_000_000 + i * 3_000_000
        row = {
            "date": day,
            "tokens": tokens,
            "claude": tokens - 1_000_000,
            "cost": 150.0 + i,
            "cacheRead": int(tokens * 0.96),
            "wakeups": 20 if i >= 25 else 0,
            "models": ["claude-opus-5-5" if i >= 20 else "claude-opus-5"],
            "input": 50_000,
            "cacheWrite": int(tokens * 0.03),
            "output": int(tokens * 0.003),
        }
        if breakdown:
            row["byModel"] = {
                "Opus 5.5" if i >= 20 else "Opus 5": {
                    "cost": 140.0,
                    "tokens": tokens,
                    "requests": 900,
                },
                "gpt-6.1-sol": {"cost": 0.5, "tokens": 1_000_000, "requests": 40},
            }
            row["byProject"] = {
                f"repo-{k}": {"cost": 20.0 + k, "tokens": tokens // 8, "requests": 100}
                for k in range(7)
            }
        rows.append(row)
    return {
        "rows": rows,
        "loopStart": rows[25]["date"],
        "marker": {"date": rows[20]["date"], "label": "Opus 5.5"},
        "generated": rows[-1]["date"],
        "breakdown": breakdown,
    }


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.mark.parametrize("breakdown", [False, True])
@pytest.mark.parametrize("width", [1100, 390])
def test_daily_page_renders(tmp_path, browser, breakdown, width):
    page_path = uv.render("daily.html", _payload(breakdown), tmp_path / "daily.html")
    page = browser.new_page(viewport={"width": width, "height": 900})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: m.type == "error" and errors.append(m.text))
    page.goto(page_path.as_uri())
    page.wait_for_load_state("load")

    assert errors == []
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert page.locator("#c-model").count() == (1 if breakdown else 0)
    assert page.locator("#c-cache").count() == 1

    if breakdown:
        # The tooltip is the one place the split runs per hovered row.
        svg = page.locator("#c-model")
        svg.scroll_into_view_if_needed()
        box = svg.bounding_box()
        page.mouse.move(box["x"] + box["width"] - 30, box["y"] + box["height"] / 2)
        tip = page.locator("#tip").inner_text()
        assert "Opus 5.5" in tip
        page.click('[data-metric="requests"]')
        assert errors == []
    page.close()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
