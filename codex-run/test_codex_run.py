#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Tests for codex-run.

`codex` is stubbed with a shell script on PATH that records its argv and its
stdin, and writes whatever `-o` points at, so the whole dispatch → background
worker → wait → post path runs without Codex, tokens, or a network. `gh` is
stubbed the same way, answering the one GraphQL question `post` asks — whether
the PR is the viewer's own — from `FAKE_GH_SELF`. `limits` reads a fixture
rollout tree. Run directly or via `just test`.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "codex-run"

FAKE_CODEX = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    # Records argv + stdin, emits a codex-shaped JSONL stream, writes the -o file.
    set -euo pipefail
    log="${FAKE_CODEX_LOG:?}"
    printf '%s\\n' "$@" > "$log.argv"
    pwd -P > "$log.cwd"
    cat > "$log.stdin"
    out=""
    prev=""
    for a in "$@"; do
      if [ "$prev" = "-o" ]; then out="$a"; fi
      prev="$a"
    done
    echo '{"type":"thread.started","thread_id":"thread-fake-1234"}'
    echo '{"type":"turn.started"}'
    echo '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"Verdict: comment\\n\\nlooks fine"}}'
    echo '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":5,"output_tokens":2,"reasoning_output_tokens":0}}'
    if [ -n "${FAKE_CODEX_FAIL:-}" ]; then exit 3; fi
    if [ -n "$out" ]; then printf 'Verdict: comment\\n\\nlooks fine\\n' > "$out"; fi
    """
)


FAKE_GH = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    # Answers the two questions codex-run asks gh: whether the PR is the
    # viewer's own, and what its head SHA is. FAKE_GH_HEAD can differ between
    # the review and the post to stand in for a branch that moved.
    set -euo pipefail
    case "${1:-}" in
      api) printf '%s\\n' "${FAKE_GH_SELF:-false}" ;;
      pr)  printf '%s\\n' "${FAKE_GH_HEAD:-sha-reviewed}" ;;
      *)   exit 1 ;;
    esac
    """
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()


class Env:
    """A throwaway HOME with a fake codex, a project root, and a git repo."""

    def __init__(self) -> None:
        # resolve(): macOS puts tmp under /var, a symlink to /private/var, and
        # the script reports real paths (pwd -P).
        self.tmp = Path(tempfile.mkdtemp(prefix="codex-run-test-")).resolve()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        fake = self.bin / "codex"
        fake.write_text(FAKE_CODEX)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        gh = self.bin / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
        self.runs = self.tmp / "runs"
        self.roots = self.tmp / "projects"
        self.roots.mkdir()
        self.repo = self.roots / "demo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "t@example.com")
        _git(self.repo, "config", "user.name", "t")
        _git(self.repo, "remote", "add", "origin", "git@github.com:acme/demo.git")
        (self.repo / "a.txt").write_text("one\n")
        _git(self.repo, "add", "a.txt")
        _git(self.repo, "commit", "-q", "-m", "first")
        self.log = self.tmp / "fake-codex"

    def run(
        self,
        *args: str,
        env: dict[str, str] | None = None,
        check: bool = True,
        cwd: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        full = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "HOME": str(self.tmp),
            "CODEX_RUN_HOME": str(self.runs),
            "CODEX_RUN_ROOTS": str(self.roots),
            "CODEX_RUN_TEMPLATES": str(self.tmp / "templates"),
            "CODEX_RUN_POLL_SECONDS": "0.2",
            "FAKE_CODEX_LOG": str(self.log),
            **(env or {}),
        }
        return subprocess.run(
            [str(SCRIPT), *args],
            env=full,
            text=True,
            capture_output=True,
            check=check,
            cwd=cwd,
        )

    def run_id(self, proc: subprocess.CompletedProcess[str]) -> str:
        for line in proc.stdout.splitlines():
            if line.startswith("run:"):
                return line.split()[1]
        raise AssertionError(f"no run id in output:\n{proc.stdout}\n{proc.stderr}")

    def meta(self, run_id: str) -> dict:
        return json.loads((self.runs / run_id / "meta.json").read_text())

    def cleanup(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


class CodexRunTest(unittest.TestCase):
    def setUp(self) -> None:
        self.env = Env()
        self.addCleanup(self.env.cleanup)

    # -- argv shape -------------------------------------------------------

    def test_help_works_without_codex_or_jq(self) -> None:
        # Help is what someone runs before installing anything, so it must not
        # stop at the dependency checks. /usr/bin:/bin has neither codex nor,
        # on macOS, jq.
        for arg in ("--help", "-h", "help"):
            proc = subprocess.run(
                [str(SCRIPT), arg],
                env={"PATH": "/usr/bin:/bin", "HOME": str(self.env.tmp)},
                text=True,
                capture_output=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("Usage: codex-run", proc.stdout)
            self.assertNotIn("is required", proc.stderr)

    def test_typos_and_subcommand_help_work_without_codex(self) -> None:
        bare = {"PATH": "/usr/bin:/bin", "HOME": str(self.env.tmp)}
        typo = subprocess.run(
            [str(SCRIPT), "reveiw", "demo", "1"], env=bare, text=True, capture_output=True
        )
        self.assertNotEqual(typo.returncode, 0)
        self.assertIn("unknown command 'reveiw'", typo.stderr)
        self.assertNotIn("is required", typo.stderr)
        sub = subprocess.run(
            [str(SCRIPT), "review", "--help"], env=bare, text=True, capture_output=True
        )
        self.assertEqual(sub.returncode, 0, sub.stderr)
        self.assertIn("Usage: codex-run", sub.stdout)

    def test_a_real_command_still_requires_codex(self) -> None:
        proc = subprocess.run(
            [str(SCRIPT), "ls"],
            env={"PATH": "/usr/bin:/bin", "HOME": str(self.env.tmp)},
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("is required", proc.stderr)

    def test_review_dry_run_builds_networked_sandbox_argv(self) -> None:
        out = self.env.run("review", "demo", "#42", "--effort", "high", "--dry-run").stdout
        argv_line = next(line for line in out.splitlines() if line.startswith("argv:"))
        self.assertIn(f"-C {self.env.repo}", argv_line)
        self.assertIn('sandbox_mode=\\"workspace-write\\"', argv_line)
        self.assertIn("sandbox_workspace_write.network_access=true", argv_line)
        self.assertIn('approval_policy=\\"never\\"', argv_line)
        self.assertIn('model_reasoning_effort=\\"high\\"', argv_line)
        self.assertIn("acme/demo PR #42", out)
        self.assertIn(str(self.env.repo), out)
        # dry-run leaves nothing behind
        self.assertFalse(self.env.runs.exists() and any(self.env.runs.iterdir()))

    def test_full_access_flag(self) -> None:
        out = self.env.run("task", "demo", "hi", "--full-access", "--dry-run").stdout
        self.assertIn('sandbox_mode=\\"danger-full-access\\"', out)
        self.assertNotIn("workspace-write", out)

    def test_slug_resolution_failure_is_an_error(self) -> None:
        proc = self.env.run("review", "nope", "1", "--dry-run", check=False)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("cannot resolve 'nope'", proc.stderr)

    def test_owner_name_resolves_by_repo_name_and_sets_repo(self) -> None:
        # The checkout is a fork (origin says someone else); the argument wins.
        _git(self.env.repo, "remote", "set-url", "origin", "git@github.com:fork/demo.git")
        out = self.env.run("review", "upstream/demo", "9", "--dry-run").stdout
        self.assertIn(f"-C {self.env.repo}", out)
        self.assertIn("upstream/demo PR #9", out)
        # --repo still overrides an owner/name argument
        out = self.env.run("review", "upstream/demo", "9", "--repo", "x/y", "--dry-run").stdout
        self.assertIn("x/y PR #9", out)
        proc = self.env.run("review", "upstream/missing", "9", "--dry-run", check=False)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("cannot resolve 'upstream/missing'", proc.stderr)

    def test_roots_default_to_parent_of_cwd(self) -> None:
        # With CODEX_RUN_ROOTS unset, a sibling directory of the cwd resolves.
        env = {**os.environ, "CODEX_RUN_ROOTS": ""}
        proc = subprocess.run(
            [str(SCRIPT), "task", "demo", "hi", "--dry-run"],
            cwd=self.env.roots / "demo",
            env={**env, "PATH": f"{self.env.bin}:{os.environ['PATH']}", "HOME": str(self.env.tmp)},
            text=True,
            capture_output=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"-C {self.env.repo}", proc.stdout)

    def test_pr_must_be_numeric(self) -> None:
        proc = self.env.run("review", "demo", "abc", "--dry-run", check=False)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("PR must be a number", proc.stderr)

    def test_repo_required_when_origin_unparseable(self) -> None:
        _git(self.env.repo, "remote", "set-url", "origin", "https://example.org/x/y.git")
        proc = self.env.run("review", "demo", "1", "--dry-run", check=False)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("pass --repo", proc.stderr)
        out = self.env.run("review", "demo", "1", "--repo", "x/y", "--dry-run").stdout
        self.assertIn("x/y PR #1", out)

    # -- templates --------------------------------------------------------

    def test_user_template_overrides_default(self) -> None:
        tdir = self.env.tmp / "templates"
        tdir.mkdir()
        (tdir / "review.md").write_text("custom {{repo}} {{pr}} {{dir}} {{slug}}\n")
        out = self.env.run("review", "demo", "7", "--dry-run").stdout
        self.assertIn(f"custom acme/demo 7 {self.env.repo} demo", out)

    def test_review_prompt_names_the_shared_rubric(self) -> None:
        # A plain `codex exec` turn gets no review rubric of its own, so the
        # prompt has to name one -- and the one in this repo, not a paraphrase.
        rubric = SCRIPT.parent.parent / "claude-code-skills/pr-shepherd/references/review-rubric.md"
        self.assertTrue(rubric.is_file())
        out = self.env.run("review", "demo", "3", "--dry-run").stdout
        self.assertIn(f"review rubric at {rubric.resolve()}.", out)
        # The checkout is not the PR's branch; without this Codex can review main.
        self.assertIn("with `gh`", out)

    def test_review_prompt_omits_a_missing_rubric(self) -> None:
        env = {"CODEX_RUN_RUBRIC": str(self.env.tmp / "nope.md")}
        out = self.env.run("review", "demo", "3", "--dry-run", env=env).stdout
        self.assertNotIn("review rubric", out)
        self.assertNotIn("{{rubric_note}}", out)

    def test_init_writes_defaults_once(self) -> None:
        out = self.env.run("init").stdout
        self.assertEqual(out.count("wrote:"), 4)
        self.assertIn("Verdict:", (self.env.tmp / "templates" / "review.md").read_text())
        self.assertEqual(self.env.run("init").stdout.count("exists:"), 4)

    def test_task_prompt_reaches_codex_verbatim(self) -> None:
        # Shell metacharacters must not be evaluated, and a placeholder-looking
        # string inside a user prompt stays literal: substitution happens on the
        # template, and the prompt is substituted in last.
        # `&&`, a lone `&`, and backslashes: bash 5.2's patsub_replacement
        # rewrites `&` in a `${v//p/r}` replacement, which is what this guards.
        prompt = "say $(uname) `id` {{repo}} && echo {{dir}} & \\1 \\& 100%"
        proc = self.env.run("task", "demo", prompt, "--fg")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        sent = (self.env.tmp / "fake-codex.stdin").read_text()
        self.assertEqual(sent, prompt + "\n")

    # -- run lifecycle ----------------------------------------------------

    def test_background_run_wait_show_ls(self) -> None:
        proc = self.env.run("review", "demo", "42")
        run_id = self.env.run_id(proc)
        self.assertTrue(run_id.endswith("-demo-pr42"), run_id)
        waited = self.env.run("wait", run_id, "--timeout", "20")
        self.assertIn("done:", waited.stdout)
        meta = self.env.meta(run_id)
        self.assertEqual(meta["status"], "done")
        self.assertEqual(meta["thread_id"], "thread-fake-1234")
        self.assertEqual(meta["pr"], 42)
        self.assertEqual(meta["exit_code"], 0)
        review = (self.env.runs / run_id / "review.md").read_text()
        self.assertTrue(review.startswith("Verdict: comment"))
        self.assertIn("Verdict: comment", self.env.run("show", run_id).stdout)
        ls = self.env.run("ls").stdout
        self.assertIn(run_id, ls)
        self.assertIn("acme/demo#42", ls)

    def test_failed_codex_marks_run_failed(self) -> None:
        proc = self.env.run("task", "demo", "x", env={"FAKE_CODEX_FAIL": "1"})
        run_id = self.env.run_id(proc)
        waited = self.env.run("wait", run_id, "--timeout", "20", check=False)
        self.assertEqual(waited.returncode, 1)
        self.assertEqual(self.env.meta(run_id)["status"], "failed")
        self.assertEqual(self.env.meta(run_id)["exit_code"], 3)

    def test_foreground_failure_records_codex_exit_code(self) -> None:
        proc = self.env.run("task", "demo", "x", "--fg", env={"FAKE_CODEX_FAIL": "1"}, check=False)
        # The wrapper exits 1 for any failed run (same as `wait`); codex's own
        # code is what meta.json must carry.
        self.assertEqual(proc.returncode, 1, proc.stderr)
        run_id = self.env.run_id(proc)
        self.assertEqual(self.env.meta(run_id)["status"], "failed")
        self.assertEqual(self.env.meta(run_id)["exit_code"], 3)

    def test_same_second_dispatches_get_distinct_run_dirs(self) -> None:
        ids = {self.env.run_id(self.env.run("task", "demo", "x")) for _ in range(3)}
        self.assertEqual(len(ids), 3, ids)
        for run_id in ids:
            self.env.run("wait", run_id, "--timeout", "20")
            self.assertEqual(self.env.meta(run_id)["id"], run_id)

    def test_wait_times_out(self) -> None:
        run_id = "stuck"
        (self.env.runs / run_id).mkdir(parents=True)
        (self.env.runs / run_id / "status").write_text("running\n")
        (self.env.runs / run_id / "meta.json").write_text('{"id":"stuck","output":"result.md"}')
        proc = self.env.run("wait", run_id, "--timeout", "1", check=False)
        self.assertEqual(proc.returncode, 124)

    def test_post_resumes_thread_and_points_at_review_file(self) -> None:
        run_id = self.env.run_id(self.env.run("review", "demo", "42"))
        self.env.run("wait", run_id, "--timeout", "20")
        out = self.env.run("post", run_id, "--dry-run").stdout
        argv_line = next(line for line in out.splitlines() if line.startswith("argv:"))
        self.assertIn("exec resume thread-fake-1234", argv_line)
        self.assertNotIn("-C ", argv_line)
        self.assertIn("workspace-write", argv_line)
        self.assertIn(str(self.env.runs / run_id / "review.body.md"), out)
        self.assertIn("gh pr review --repo acme/demo 42", out)

    # The verdict maps to a flag here rather than in the prompt, because a model
    # asked to pick one has picked wrong: told to fall back to --comment when
    # GitHub refused --request-changes on its author's own PR, it reported the
    # refusal and stopped, leaving the review unposted.
    def test_post_decides_the_review_flag_from_the_verdict(self) -> None:
        run_id = self.env.run_id(self.env.run("review", "demo", "42"))
        self.env.run("wait", run_id, "--timeout", "20")
        (self.env.runs / run_id / "review.md").write_text("Verdict: request changes\n\nfindings\n")

        out = self.env.run("post", run_id, "--dry-run").stdout

        self.assertIn("gh pr review --repo acme/demo 42 --request-changes --body-file", out)
        # `gh pr review` refuses a blank body on --comment and --request-changes,
        # so the body file is assembled here rather than described to the turn.
        body = (self.env.runs / run_id / "review.body.md").read_text()
        self.assertNotIn("Verdict:", body)
        self.assertIn("findings", body)
        self.assertNotIn("Posted as a comment", body)

    def test_post_downgrades_to_a_comment_on_your_own_pr(self) -> None:
        run_id = self.env.run_id(self.env.run("review", "demo", "42"))
        self.env.run("wait", run_id, "--timeout", "20")
        (self.env.runs / run_id / "review.md").write_text("Verdict: approve\n\nlooks good\n")

        out = self.env.run("post", run_id, "--dry-run", env={"FAKE_GH_SELF": "true"}).stdout

        self.assertIn("gh pr review --repo acme/demo 42 --comment --body-file", out)
        # The downgrade has to be visible to whoever reads the review on GitHub,
        # or an approval-strength verdict silently reads as a passing comment.
        body = (self.env.runs / run_id / "review.body.md").read_text()
        self.assertTrue(body.startswith("Posted as a comment:"), body)
        self.assertIn("--approve", body)
        self.assertNotIn("Verdict:", body)
        self.assertIn("looks good", body)

    # An unreachable or unauthenticated gh must not block the post: the verdict
    # still maps to a flag, and GitHub is the backstop if it is the wrong one.
    def test_post_falls_back_to_the_verdict_when_gh_cannot_answer(self) -> None:
        run_id = self.env.run_id(self.env.run("review", "demo", "42"))
        self.env.run("wait", run_id, "--timeout", "20")
        (self.env.runs / run_id / "review.md").write_text("Verdict: request changes\n\nx\n")
        (self.env.bin / "gh").write_text("#!/usr/bin/env bash\nexit 1\n")

        out = self.env.run("post", run_id, "--dry-run").stdout

        self.assertIn("--request-changes", out)

    def test_post_refuses_a_head_that_moved_since_the_review(self) -> None:
        run_id = self.env.run_id(self.env.run("review", "demo", "42"))
        self.env.run("wait", run_id, "--timeout", "20")

        proc = self.env.run(
            "post", run_id, "--dry-run", env={"FAKE_GH_HEAD": "sha-moved"}, check=False
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("moved since this review", proc.stderr)

        forced = self.env.run(
            "post", run_id, "--dry-run", "--force", env={"FAKE_GH_HEAD": "sha-moved"}
        )
        self.assertIn("gh pr review --repo acme/demo 42", forced.stdout)

    # An unanswerable probe is not evidence the head moved, so it must not block.
    def test_post_proceeds_when_the_head_is_unknown(self) -> None:
        run_id = self.env.run_id(self.env.run("review", "demo", "42"))
        self.env.run("wait", run_id, "--timeout", "20")
        (self.env.bin / "gh").write_text("#!/usr/bin/env bash\nexit 1\n")

        out = self.env.run("post", run_id, "--dry-run").stdout

        self.assertIn("gh pr review --repo acme/demo 42", out)

    def test_post_refuses_non_review_and_unfinished_runs(self) -> None:
        task_id = self.env.run_id(self.env.run("task", "demo", "x"))
        self.env.run("wait", task_id, "--timeout", "20")
        proc = self.env.run("post", task_id, check=False)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("not a PR review", proc.stderr)

        (self.env.runs / "pending").mkdir(parents=True)
        (self.env.runs / "pending" / "status").write_text("running\n")
        (self.env.runs / "pending" / "meta.json").write_text('{"id":"pending","kind":"review"}')
        proc = self.env.run("post", "pending", check=False)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("not done", proc.stderr)

    def test_resume_inherits_model_and_effort(self) -> None:
        run_id = self.env.run_id(
            self.env.run("task", "demo", "x", "--model", "m1", "--effort", "low")
        )
        self.env.run("wait", run_id, "--timeout", "20")
        out = self.env.run("resume", run_id, "more", "--dry-run").stdout
        self.assertIn("exec resume thread-fake-1234", out)
        self.assertIn("-m m1", out)
        self.assertIn('model_reasoning_effort=\\"low\\"', out)

    # -- working directory ------------------------------------------------

    def test_resume_runs_in_the_project_directory(self) -> None:
        """`codex exec resume` takes no -C, so the cwd must be the process's own.

        Without it codex inherits the caller's directory and refuses to run
        outside a trusted one, which made `post` fail from anywhere but the
        checkout.
        """
        proc = self.env.run("task", "demo", "hi", cwd=str(self.env.tmp))
        run_id = self.env.run_id(proc)
        self.env.run("wait", run_id, "--timeout", "20")
        # The dispatch run wrote this file, and writes the same path a correct
        # resume would. Remove it so the assertion can only be satisfied by the
        # resumed turn, and wait for that run rather than racing it.
        cwd_log = Path(f"{self.env.log}.cwd")
        cwd_log.unlink()
        resumed = self.env.run_id(self.env.run("resume", run_id, "more", cwd=str(self.env.tmp)))
        self.env.run("wait", resumed, "--timeout", "20")
        self.assertEqual(cwd_log.read_text().strip(), str(self.env.repo))

    def test_dispatch_runs_in_the_project_directory(self) -> None:
        """The same invariant on the dispatch path, which -C alone also satisfied.

        Pins it to the process rather than the flag, so the two paths cannot
        drift apart again.
        """
        proc = self.env.run("task", "demo", "hi", cwd=str(self.env.tmp))
        self.env.run("wait", self.env.run_id(proc), "--timeout", "20")
        started_in = Path(f"{self.env.log}.cwd").read_text().strip()
        self.assertEqual(started_in, str(self.env.repo))

    # -- sandbox writable roots -------------------------------------------

    def test_add_dir_covers_a_linked_worktrees_common_dir(self) -> None:
        """A linked worktree cannot check out a branch without its common dir writable.

        The worktree's git metadata lives in the main repo's .git, outside the
        workspace-write sandbox, so git fails on index.lock with EPERM.
        """
        wt = self.env.tmp / "wt"
        _git(self.env.repo, "worktree", "add", "-q", "--detach", str(wt), "main")
        common = (self.env.repo / ".git").resolve()
        out = self.env.run("task", str(wt), "hi", "--dry-run").stdout
        self.assertIn(f"--add-dir {common}", out)

    def test_add_dir_is_omitted_for_an_ordinary_checkout(self) -> None:
        """Its common dir is already inside the workspace; adding it widens the sandbox."""
        out = self.env.run("task", "demo", "hi", "--dry-run").stdout
        self.assertNotIn("--add-dir", out)

    def test_resume_does_not_pass_add_dir(self) -> None:
        """`codex exec resume` rejects --add-dir, so it must never reach a resumed turn."""
        wt = self.env.tmp / "wt-resume"
        _git(self.env.repo, "worktree", "add", "-q", "--detach", str(wt), "main")
        proc = self.env.run("task", str(wt), "hi")
        run_id = self.env.run_id(proc)
        self.env.run("wait", run_id, "--timeout", "20")
        out = self.env.run("resume", run_id, "more", "--dry-run").stdout
        self.assertIn("exec resume", out)
        self.assertNotIn("--add-dir", out)

    # -- exit codes -------------------------------------------------------

    def test_asking_for_help_succeeds(self) -> None:
        """Every other tool in this repo exits 0 for --help; this one exited 1.

        A non-zero --help breaks `cmd --help && ...` and reads as a failure to
        anything checking status, which is why it is worth a test rather than a
        one-line change.
        """
        for args in (["--help"], ["help"], ["review", "--help"]):
            proc = self.env.run(*args, check=False)
            self.assertEqual(proc.returncode, 0, f"{args} exited {proc.returncode}")
            self.assertIn("Usage: codex-run", proc.stdout)

    def test_a_usage_error_still_fails(self) -> None:
        """The same text on stdout, but a status that says something went wrong."""
        for args in ([], ["bogus-command"]):
            proc = self.env.run(*args, check=False)
            self.assertEqual(proc.returncode, 1, f"{args} exited {proc.returncode}")

    # -- review-local -----------------------------------------------------

    def test_review_local_renders_preview_and_diff(self) -> None:
        if (
            shutil.which("preview-diff") is None
            and not (HERE.parent / "preview-diff" / "preview-diff").exists()
        ):
            self.skipTest("preview-diff not available")
        (self.env.repo / "a.txt").write_text("one\ntwo\n")
        _git(self.env.repo, "commit", "-qam", "second")
        env = {"PATH": f"{self.env.bin}:{HERE.parent / 'preview-diff'}:{os.environ['PATH']}"}
        proc = self.env.run("review-local", "demo", "HEAD~1", "HEAD", "--dry-run", env=env)
        out = proc.stdout
        self.assertIn("HEAD~1...HEAD", out)
        self.assertIn("preview.html", out)
        self.assertIn("change.diff", out)
        # dry-run wipes the run dir, so a real dispatch is what leaves the files.
        proc = self.env.run("review-local", "demo", "HEAD~1", "HEAD", env=env)
        run_id = self.env.run_id(proc)
        self.env.run("wait", run_id, "--timeout", "20")
        rd = self.env.runs / run_id
        self.assertIn("+two", (rd / "change.diff").read_text())
        self.assertGreater((rd / "preview.html").stat().st_size, 1000)
        meta = self.env.meta(run_id)
        self.assertEqual(meta["kind"], "review-local")
        self.assertEqual(meta["base"], "HEAD~1")
        self.assertIn("HEAD~1...HEAD", self.env.run("ls").stdout)

    def test_review_local_diffs_from_the_merge_base(self) -> None:
        """A branch behind its base must not be reviewed against the base's own commits.

        Diffing `base..head` reports files the branch never touched as deletions,
        which reads to a reviewer as a real removal.
        """
        if (
            shutil.which("preview-diff") is None
            and not (HERE.parent / "preview-diff" / "preview-diff").exists()
        ):
            self.skipTest("preview-diff not available")
        # main moves ahead with a file the feature branch never sees.
        (self.env.repo / "base_only.txt").write_text("base\n")
        _git(self.env.repo, "add", "base_only.txt")
        _git(self.env.repo, "commit", "-qm", "base advances")
        # The feature branch forks from before that commit, so it is behind main.
        _git(self.env.repo, "checkout", "-q", "-b", "feat", "main~1")
        (self.env.repo / "feat_only.txt").write_text("feat\n")
        _git(self.env.repo, "add", "feat_only.txt")
        _git(self.env.repo, "commit", "-qm", "feature work")

        env = {"PATH": f"{self.env.bin}:{HERE.parent / 'preview-diff'}:{os.environ['PATH']}"}
        proc = self.env.run("review-local", "demo", "main", "feat", env=env)
        run_id = self.env.run_id(proc)
        self.env.run("wait", run_id, "--timeout", "20")
        diff = (self.env.runs / run_id / "change.diff").read_text()

        self.assertIn("feat_only.txt", diff)
        self.assertNotIn("base_only.txt", diff)

    def test_review_local_with_no_diff_is_an_error(self) -> None:
        env = {"PATH": f"{self.env.bin}:{HERE.parent / 'preview-diff'}:{os.environ['PATH']}"}
        proc = self.env.run(
            "review-local", "demo", "HEAD", "HEAD", "--dry-run", env=env, check=False
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no diff", proc.stderr)
        self.assertFalse(self.env.runs.exists() and any(self.env.runs.iterdir()))

    # -- limits -----------------------------------------------------------

    def test_limits_reads_latest_rate_limits_record(self) -> None:
        sessions = self.env.tmp / ".codex" / "sessions" / "2026" / "09" / "09"
        sessions.mkdir(parents=True)
        older = sessions / "rollout-2026-09-09T10-00-00-aaaa.jsonl"
        newer = sessions / "rollout-2026-09-09T11-00-00-bbbb.jsonl"

        def rec(ts: str, used: int) -> str:
            return json.dumps(
                {
                    "timestamp": ts,
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"model_context_window": 258400},
                        "rate_limits": {
                            "plan_type": "plus",
                            "primary": {
                                "used_percent": used,
                                "window_minutes": 300,
                                "resets_at": 1789034397,
                            },
                            "secondary": {
                                "used_percent": 35,
                                "window_minutes": 10080,
                                "resets_at": 1789447201,
                            },
                        },
                    },
                }
            )

        older.write_text('{"type":"session_meta"}\n' + rec("2026-09-09T17:00:00.000Z", 8) + "\n")
        # The newest file has a turn in flight: no token_count yet.
        newer.write_text('{"type":"session_meta"}\n')
        time.sleep(0.01)
        os.utime(newer, None)
        text = self.env.run("limits").stdout
        self.assertIn("plan: plus", text)
        self.assertIn("5h: 8% used", text)
        self.assertIn("weekly: 35% used", text)
        data = json.loads(self.env.run("limits", "--json").stdout)
        self.assertEqual(data["five_hour"]["used_percent"], 8)
        self.assertEqual(data["context_window"], 258400)

    def test_limits_without_sessions_dir_fails_cleanly(self) -> None:
        proc = self.env.run("limits", check=False)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no Codex sessions dir", proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=1)
