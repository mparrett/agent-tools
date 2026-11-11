# Tooling runs via uv — no project venv or install step needed.

# Pinned so a ruff release can't fail CI on a PR that didn't touch the code it
# now objects to, and so `just fix` locally can't reformat against a different
# version than the gate. Bump deliberately, in its own commit.
worktree_root := env_var("HOME") / "projects-new/worktrees"
ruff := "ruff@0.16.3"
# The read-only clone that skills and tools load from. REPOS_RO is the directory
# holding every such clone, so one variable covers all of them.
ro := env("REPOS_RO", home_directory() / "repos-read-only") / "agent-tools"

# List available recipes
default:
    @just --list

# fetch + merge --ff-only rather than pull, because pull refuses outright on a
# dirty tree — exactly when you reach for this, having started editing before
# realizing you needed a branch. A fast-forward carries uncommitted work across.
#
# Superseded by `just wt` — this checkout stays on main
branch NAME:
    #!/usr/bin/env bash
    echo "\`just branch\` is retired: it left this shared checkout parked on a" >&2
    echo "feature branch, and the next 'git checkout -b' then branched from that" >&2
    echo "rather than from main." >&2
    echo >&2
    echo "  just wt {{NAME}}     # worktree off a freshly-fetched origin/main" >&2
    exit 1

# Start a worktree off a freshly-fetched origin/main, and print its path
wt NAME:
    #!/usr/bin/env bash
    set -euo pipefail
    # Worktrees live outside the repo so that this checkout's `git status` keeps
    # meaning something -- the same property the read-only clone gets from
    # nobody authoring in it. The dir name flattens the branch's slashes;
    # the branch keeps them.
    root="{{worktree_root}}"
    slug="$(printf '%s' "{{NAME}}" | tr '/' '-')"
    path="$root/agent-tools-$slug"

    if [ -e "$path" ]; then
        echo "already exists: $path" >&2
        exit 1
    fi

    git fetch origin main --quiet
    mkdir -p "$root"
    # The registration outlives the directory, so a path that was rm -rf'd
    # without `worktree remove` still blocks this. Clear that first.
    git worktree prune
    # --no-track matters more than it looks. Without it the new branch tracks
    # origin/main, and with push.default=simple a plain `git push` then fails
    # with a suggestion to run `git push origin HEAD:main` -- which puts the
    # feature branch straight onto main. An agent following the error text does
    # the wrong thing by default.
    git worktree add --no-track -b "{{NAME}}" "$path" origin/main

    echo
    echo "$path"

# Every agent-tools worktree: branch, state, and whether its PR has merged
wt-ls:
    #!/usr/bin/env bash
    set -euo pipefail
    # `awk '{print $2}'` truncates any path containing a space, and the
    # truncation can name a *different* real worktree. Take the whole field.
    git worktree list --porcelain | sed -n 's/^worktree //p' | while read -r path; do
        [ -d "$path" ] || { printf '  %-46s %s\n' "${path##*/}" "STALE REGISTRATION (just wt-prune)"; continue; }
        branch="$(git -C "$path" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"

        # The canonical checkout is the one whose gitdir is the common dir.
        # Comparing path strings breaks as soon as anything above them is a symlink.
        gd="$(git -C "$path" rev-parse --absolute-git-dir 2>/dev/null || echo x)"
        common="$(git -C "$path" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || echo y)"
        [ "$gd" = "$common" ] && kind="canonical" || kind="worktree"

        merged=""
        if [ "$branch" != "main" ] && [ "$kind" = "worktree" ]; then
            pr="$(gh pr list --repo mparrett/agent-tools --state merged --head "$branch" --json number -q '.[0].number' 2>/dev/null || true)"
            [ -n "$pr" ] && merged="merged #$pr"
        fi

        state=""
        [ -n "$(git -C "$path" status --porcelain 2>/dev/null)" ] && state="dirty"

        # `rev-list --count origin/<b>..HEAD` exits 128 printing nothing when the
        # remote branch does not exist, so `|| echo 0` would report "nothing
        # unpushed" for a worktree with commits it has never pushed -- the case
        # this column exists to warn about.
        #
        # A missing ref does not say *why* it is missing, though: never pushed
        # and deleted-after-merge look identical here, and calling the second
        # "never pushed" contradicts the `merged #N` printed beside it. So the
        # merge lookup happens first and answers it; absent that, the wording
        # stays neutral about which it was.
        if [ "$branch" = "HEAD" ]; then
            state="${state:+$state, }detached"
        elif git -C "$path" rev-parse --verify --quiet "origin/$branch" >/dev/null; then
            ahead="$(git -C "$path" rev-list --count "origin/$branch..HEAD")"
            [ "$ahead" != "0" ] && state="${state:+$state, }$ahead unpushed"
        elif [ -z "$merged" ]; then
            state="${state:+$state, }no remote branch"
        fi

        printf '  %-46s %-38s %s\n' "${path##*/}" "$branch${kind:+ ($kind)}" "${state:-${merged:-clean}}${state:+${merged:+ · $merged}}"
    done

# Retire a worktree: refuses unless it is clean and pushed
wt-rm NAME:
    #!/usr/bin/env bash
    set -euo pipefail
    # Resolve against the worktrees git actually knows about, matching either the
    # branch (what `just wt` was given) or the directory (what `wt-ls` prints).
    # Deriving the path from the branch alone only works for worktrees this
    # justfile created; one made by hand is then unremovable by the tool that is
    # supposed to retire it.
    slug="$(printf '%s' "{{NAME}}" | tr '/' '-')"
    path=""
    by_dir=""
    while read -r candidate; do
        [ -d "$candidate" ] || continue
        base="${candidate##*/}"
        cbranch="$(git -C "$candidate" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '')"
        # Branch beats directory, rather than whichever git listed first: a name
        # can match one worktree by branch and another by directory, and
        # resolving that by listing order removes an arbitrary one of the two.
        if [ "$cbranch" = "{{NAME}}" ] || [ "$candidate" = "{{NAME}}" ]; then
            path="$candidate"
            break
        fi
        if [ "$base" = "agent-tools-$slug" ] || [ "$base" = "{{NAME}}" ]; then
            by_dir="${by_dir:-$candidate}"
        fi
    done < <(git worktree list --porcelain | sed -n 's/^worktree //p')
    path="${path:-${by_dir:-}}"

    if [ -z "$path" ]; then
        echo "no worktree matching '{{NAME}}' (tried branch, directory and path):" >&2
        just wt-ls >&2
        exit 1
    fi

    gd="$(git -C "$path" rev-parse --absolute-git-dir)"
    common="$(git -C "$path" rev-parse --path-format=absolute --git-common-dir)"
    [ "$gd" = "$common" ] && { echo "$path is the canonical checkout, not a worktree" >&2; exit 1; }

    branch="$(git -C "$path" rev-parse --abbrev-ref HEAD)"
    # Detached: `rev-parse --abbrev-ref HEAD` says "HEAD", and `origin/HEAD`
    # resolves to origin/main, so every check below would silently measure
    # against main rather than against anything this worktree is doing.
    if [ "$branch" = "HEAD" ]; then
        echo "$path is on a detached HEAD, so nothing here can be checked against a branch." >&2
        echo "Remove it by hand if you are sure:" >&2
        echo "  git worktree remove --force $path && git worktree prune" >&2
        exit 1
    fi
    if [ -n "$(git -C "$path" status --porcelain)" ]; then
        echo "$path has uncommitted changes; not removing:" >&2
        git -C "$path" status --short >&2
        exit 1
    fi
    # An unpushed commit here has no copy anywhere. `git log origin/<b>..HEAD`
    # prints nothing *and exits 128* once the remote branch is gone, so the
    # exit status has to be part of the test, not just the output.
    if git -C "$path" rev-parse --verify --quiet "origin/$branch" >/dev/null; then
        unpushed="$(git -C "$path" log --oneline "origin/$branch..HEAD")"
        if [ -n "$unpushed" ]; then
            echo "$path has commits not on origin/$branch; not removing:" >&2
            echo "$unpushed" >&2
            exit 1
        fi
    elif [ "$branch" != "main" ]; then
        echo "there is no origin/$branch, so nothing here can be checked against it." >&2
        echo "That is either a branch never pushed or one deleted after merging;" >&2
        echo "push it, or confirm it merged, before removing $path." >&2
        exit 1
    fi

    git worktree remove --force "$path"
    git worktree prune
    echo "removed $path"
    echo
    # Deleting the branch stays a human decision: this cannot tell a branch
    # abandoned on purpose from one whose PR is about to be reopened.
    echo "The branch is untouched. If it has merged and nothing targets it:"
    echo "  git branch -D $branch && git push origin --delete $branch"

# Clear worktree registrations whose directories are gone
wt-prune:
    git worktree prune -v

# Commits your branch adds over origin/main — every one should be yours (run before a PR)
mine:
    #!/usr/bin/env bash
    set -euo pipefail
    # Against origin/main, not the local ref: a local `main` that has not been
    # pulled reports commits as yours that merged days ago.
    git fetch origin main --quiet
    git log --oneline origin/main..HEAD

# Format all code in place
fmt:
    uvx {{ruff}} format .

# Check formatting without writing (CI)
fmt-check:
    uvx {{ruff}} format --check .

# Lint
lint:
    uvx {{ruff}} check .

# Run tests
test:
    uv run mdfeed/test_mdfeed.py
    uv run --with pytest --with playwright mdfeed/test_mdfeed_browser.py
    uv run mdserve/test_mdserve.py
    uv run --with pytest --with playwright mdserve/test_mdserve_browser.py
    uv run claude-ls/test_claude_ls.py
    uv run claude-ls/test_session_mates.py
    uv run claude-ps/test_claude_ps.py
    uv run claude-watch/test_claude_watch.py
    uv run harness-ls/test_harness_ls.py
    uv run memory-ls/test_memory_ls.py
    uv run codex-run/test_codex_run.py
    uv run --with pytest codex-review-scan/test_codex_review_scan.py
    uv run --directory claude-reel pytest -q
    uv run --directory claude-graph pytest -q
    uv run --directory workstream pytest -q
    # CI has no claude CLI; the mod's suite runs wherever one is installed.
    @if command -v claude >/dev/null; then claude plugin test workstream/mod; else echo "skip workstream/mod: no claude CLI"; fi
    uv run --with pytest --with playwright htmldiff/test_htmldiff.py
    uv run --with pytest --with playwright usage-viz/test_usage_viz.py
    uv run --with pytest claude-code-skills/pr-shepherd/test_review_target.py
    uv run claude-code-skills/pr-dashboard/test_watch.py
    uv run claude-code-skills/pr-dashboard/test_collect.py
    uv run claude-code-skills/pr-dashboard/test_render.py
    uv run --with pytest --with playwright claude-code-skills/pr-dashboard/test_template.py
    uv run --with pytest claude-code-skills/workstream-map/test_build.py
    uv run --with pytest claude-code-skills/workstream-map/test_fetch.py
    uv run --with pytest --with playwright claude-code-skills/workstream-map/test_template.py
    uv run --with pytest --with playwright --directory preview-diff pytest -q
    uv run skill-lint/test_skill_lint.py
    uv run board/test_board.py
    uv run test_install_recipes.py

# Lint and apply autofixes
fix:
    uvx {{ruff}} check --fix .

# Apply lint autofixes, then format
tidy: fix fmt

# CI gate: lint + format check, no writes
check: lint fmt-check

# Symlink every tool in bin/ into DIR, e.g. `just link ~/bin`
#
# Links point at this clone's bin/ rather than at the scripts, so a tool that
# moves inside the repo keeps working through the link already installed.
link DIR:
    #!/usr/bin/env bash
    set -euo pipefail
    root="$(pwd)"
    # quote(), not "{{DIR}}": just interpolates the raw argument into the script
    # text, so an unquoted path substitutes and executes -- `just link '/tmp/x$(id)'`
    # ran `id`, and a path holding a double quote broke the script outright.
    dest={{ quote(DIR) }}
    mkdir -p "$dest"
    for tool in bin/*; do
        name="$(basename "$tool")"
        want="$root/bin/$name"
        have="$dest/$name"
        if [ -L "$have" ] && [ "$(readlink "$have")" = "$want" ]; then
            echo "  ok        $name"
        elif [ -e "$have" ] || [ -L "$have" ]; then
            # Never clobber: it may be another clone's tool, or an unrelated
            # command that happens to share the name.
            echo "  skipped   $name -> $(readlink "$have" 2>/dev/null || echo 'a regular file')"
        else
            ln -s "$want" "$have"
            echo "  linked    $name"
        fi
    done

# Build the Finder droplet that opens .md files in mdfeed, e.g. `just install-finder-app`
#
# macOS only. The bundle is compiled, not committed: see the source's header for
# why it has to be an app at all, and why the paths are baked in here.
#
# VIA is the mdview the app calls, and defaults to this clone's. Pass a path you
# control instead -- `just install-finder-app ~/Applications ~/bin/mdview` --
# and a symlink at that path decides which checkout the app runs, so switching
# checkouts costs an `ln -sfn` rather than a rebuild.
install-finder-app DIR="~/Applications" VIA="":
    #!/usr/bin/env bash
    set -euo pipefail
    if [ "$(uname -s)" != "Darwin" ]; then
        echo "install-finder-app is macOS-only (osacompile, LaunchServices)" >&2
        exit 1
    fi
    uv_bin="$(command -v uv || true)"
    if [ -z "$uv_bin" ]; then
        echo "uv is not on PATH; the droplet needs its directory baked in" >&2
        exit 1
    fi
    root="$(pwd)"
    dest={{ quote(DIR) }}  # see `link`
    dest="${dest/#\~/$HOME}"
    via={{ quote(VIA) }}
    via="${via/#\~/$HOME}"
    via="${via:-$root/bin/mdview}"
    if [ ! -e "$via" ]; then
        # A dangling link is legitimate: pointing the app at a path that only
        # exists once a branch merges is one way to use this before then.
        echo "note: $via does not exist yet" >&2
    fi
    # The path is compiled in, so a baked worktree path outlives the worktree.
    if [ "$via" = "$root/bin/mdview" ] &&
       [ "$(git rev-parse --git-dir)" != "$(git rev-parse --git-common-dir)" ]; then
        echo "warning: this is a linked worktree, and its path is now inside the app." >&2
        echo "         Retiring the worktree breaks it. Re-run with a stable VIA, e.g." >&2
        echo "         just install-finder-app $dest ~/bin/mdview" >&2
    fi
    app="$dest/Open in mdfeed.app"
    mkdir -p "$dest"
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    # Bash substitution rather than sed, which would read a `|` or `&` in a
    # path as syntax. Bash has the same trap: from 5.2 (patsub_replacement) an
    # unquoted `&` in the replacement expands to the match. Quoting the
    # replacement turns that off, but only in this form -- a bare assignment.
    # Inside "${...}", bash 4.2 and older (macOS's /bin/bash is 3.2) keep the
    # inner quotes as literal characters.
    src="$(cat mdfeed/finder/open-in-mdfeed.applescript)"
    uv_dir="$(dirname "$uv_bin")"
    src=${src//__MDVIEW__/"$via"}
    src=${src//__PATH__/"$uv_dir"}
    printf '%s\n' "$src" > "$tmp/droplet.applescript"
    osacompile -o "$app" "$tmp/droplet.applescript"
    # osacompile declares the droplet a handler for "*" / "****", and Finder
    # leaves wildcard handlers out of the Open With list -- the app was only
    # reachable through "Other...". Naming the Markdown UTI puts it in the list.
    # The edit invalidates osacompile's ad-hoc signature, so sign it again.
    plutil -replace CFBundleDocumentTypes -json \
        '[{"CFBundleTypeName":"Markdown","CFBundleTypeRole":"Viewer","LSHandlerRank":"Alternate","LSItemContentTypes":["net.daringfireball.markdown"]}]' \
        "$app/Contents/Info.plist"
    # osacompile also leaves out CFBundleIdentifier. LaunchServices records a
    # type's default handler by bundle ID, so without one "Change All..." has
    # nothing to store: it looks applied, and `open x.md` still uses the old app.
    plutil -replace CFBundleIdentifier -string local.agent-tools.open-in-mdfeed \
        "$app/Contents/Info.plist"
    codesign --force --sign - "$app"
    # Register it now, so it appears under Open With without a Finder restart.
    /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$app"
    echo
    echo "built $app"
    echo "  wrapping $via"
    echo
    echo "To use it: right-click a .md file -> Open With -> Open in mdfeed."
    echo "To make it the default for every .md: Get Info on one (cmd-I),"
    echo "set 'Open with' to it, then click 'Change All...'."

# Remove the symlinks `just link DIR` created, and only those
unlink DIR:
    #!/usr/bin/env bash
    set -euo pipefail
    root="$(pwd)"
    dest={{ quote(DIR) }}  # see `link`
    for tool in bin/*; do
        name="$(basename "$tool")"
        have="$dest/$name"
        # Only a link pointing into this clone is ours to remove.
        if [ -L "$have" ] && [ "$(readlink "$have")" = "$root/bin/$name" ]; then
            rm "$have"
            echo "  removed   $name"
        elif [ -e "$have" ] || [ -L "$have" ]; then
            echo "  left      $name (not this clone's)"
        else
            echo "  absent    $name"
        fi
    done

# Symlink every skill in claude-code-skills/ into DIR, e.g. `just link-skills ~/.claude/skills`
link-skills DIR:
    #!/usr/bin/env bash
    set -euo pipefail
    # The bin/ counterpart is `link`. A skill is a directory, so the link points
    # at the directory and edits inside the repo need no resync.
    root="$(pwd)"
    dest={{ quote(DIR) }}  # see `link` for why quote() and not "{{DIR}}"
    mkdir -p "$dest"
    for skill in claude-code-skills/*/; do
        name="$(basename "$skill")"
        want="$root/claude-code-skills/$name"
        have="$dest/$name"
        if [ -L "$have" ] && [ "$(readlink "$have")" = "$want" ]; then
            echo "  ok        $name"
        elif [ -e "$have" ] || [ -L "$have" ]; then
            # Never clobber: it may be another clone's skill, or a hand-written
            # one that happens to share the name.
            echo "  skipped   $name -> $(readlink "$have" 2>/dev/null || echo 'a regular directory')"
        else
            ln -s "$want" "$have"
            echo "  linked    $name"
        fi
    done

# Remove the symlinks `just link-skills DIR` created, and only those
unlink-skills DIR:
    #!/usr/bin/env bash
    set -euo pipefail
    root="$(pwd)"
    dest={{ quote(DIR) }}  # see `link`
    for skill in claude-code-skills/*/; do
        name="$(basename "$skill")"
        have="$dest/$name"
        # Only a link pointing into this clone is ours to remove.
        if [ -L "$have" ] && [ "$(readlink "$have")" = "$root/claude-code-skills/$name" ]; then
            rm "$have"
            echo "  removed   $name"
        elif [ -e "$have" ] || [ -L "$have" ]; then
            echo "  left      $name (not this clone's)"
        else
            echo "  absent    $name"
        fi
    done

# Fast-forward a read-only clone to origin/main, refusing if anything changed under it
sync:
    #!/usr/bin/env bash
    set -euo pipefail
    # 256-color, not the 16 basic slots: terminal themes remap those freely.
    if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
        ok=$'\e[1;38;5;78m'; amb=$'\e[38;5;214m'; bad=$'\e[1;38;5;196m'
        hi=$'\e[1;38;5;231m'; dim=$'\e[2m'; rst=$'\e[0m'
    else
        ok=''; amb=''; bad=''; hi=''; dim=''; rst=''
    fi
    # For a clone that exists to be *read* through symlinks rather than worked
    # in -- skills and tools load from it, nothing is authored in it. That makes
    # its `git status` a tripwire: a modification there arrived out of band,
    # from a plugin, a script, or a session that edited a skill through its
    # ~/.claude path without knowing it was writing into a working tree.
    # Pulling over that would destroy the only copy, so stop and show it.
    branch="$(git rev-parse --abbrev-ref HEAD)"
    if [ "$branch" != "main" ]; then
        echo "${bad}⚠${rst} on '$branch', not main -- a read-only clone should track main" >&2
        exit 1
    fi
    if [ -n "$(git status --porcelain)" ]; then
        echo "${bad}⚠${rst} ${hi}out-of-band changes under this clone; not pulling over them:${rst}" >&2
        echo >&2
        git status --short >&2
        echo >&2
        echo "Commit them from a working clone, or discard them here once they are" >&2
        echo "landed upstream. 'git stash' first if you are unsure -- there is no" >&2
        echo "remote copy of anything listed above." >&2
        exit 1
    fi
    # Tag where we are *before* moving, not after. The version worth rolling
    # back to is the one the agents were already running -- that is the only
    # commit there is evidence about. A tag applied after the pull names the
    # thing nobody has exercised yet.
    #
    # These are local on purpose. They record what this machine was running,
    # which is not a fact the shared repo wants, and pushing a sync point per
    # machine would bury the repo's real tags if it ever grows any.
    before="$(git rev-parse HEAD)"
    git fetch origin --no-tags
    target="$(git rev-parse origin/main)"

    if [ "$before" = "$target" ]; then
        echo "${dim}already at origin/main ($(git rev-parse --short HEAD)); nothing to sync${rst}"
        exit 0
    fi

    stamp="synced/$(date -u +%Y-%m-%dT%H%M%S)"
    git tag -a "$stamp" -m "state before syncing $(git rev-parse --short HEAD)..$(git rev-parse --short origin/main)"
    # A tag saying "synced" when the pull then failed is a lie the next rollback
    # would act on, so it does not outlive the failure.
    if ! git pull --ff-only; then
        git tag -d "$stamp" >/dev/null
        exit 1
    fi

    echo
    echo "${ok}Landed $(git rev-list --count "$before".."$target") commit(s) — this is what changed"
    echo "about how the agents behave:${rst}"
    echo
    git --no-pager log --no-decorate --format="  ${amb}%h${rst} %s" "$before".."$target"
    echo
    echo "${dim}Roll back with:${rst}  git -C $(pwd) reset --hard ${amb}$stamp${rst}"
    echo "${dim}Bisect with:${rst}     git -C $(pwd) bisect start main ${amb}$stamp${rst}"
    echo

    # Keep the list readable. These are breadcrumbs, not history worth keeping
    # forever, and an unbounded set makes `git tag` useless for finding the one
    # you want.
    git tag --list 'synced/*' --sort=-refname | tail -n +21 | xargs -r git tag -d >/dev/null

    echo "Re-run 'just link ~/bin' and 'just link-skills ~/.claude/skills' to pick"
    echo "up anything newly added; both skip what is already linked."

# Make merged work live: sync the read-only clone, then link anything new from it
ship:
    #!/usr/bin/env bash
    set -euo pipefail
    # 256-color, not the 16 basic slots: terminal themes remap those freely.
    if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
        ok=$'\e[1;38;5;78m'; amb=$'\e[38;5;214m'; bad=$'\e[1;38;5;196m'
        hi=$'\e[1;38;5;231m'; dim=$'\e[2m'; rst=$'\e[0m'
    else
        ok=''; amb=''; bad=''; hi=''; dim=''; rst=''
    fi
    ro={{ quote(ro) }}
    if [ ! -d "$ro/.git" ]; then
        echo "${bad}⚠${rst} no read-only clone at $ro (set REPOS_RO to the directory holding it)" >&2
        exit 1
    fi
    # sync deploys origin/main and nothing else, so refuse when the work in
    # front of you is not part of it -- shipping would quietly leave it out.
    git fetch origin main --quiet
    branch="$(git rev-parse --abbrev-ref HEAD)"
    if [ "$branch" = "main" ]; then
        ahead="$(git rev-list --count origin/main..HEAD)"
        if [ "$ahead" != "0" ]; then
            echo "${bad}⚠${rst} main has $ahead commit(s) not on origin/main; they would not ship" >&2
            exit 1
        fi
    else
        # Squash merges leave the branch's commits off main, so ancestry cannot
        # answer this; the merged PR can.
        pr="$(gh pr list --repo mparrett/agent-tools --state merged --head "$branch" --json number -q '.[0].number' 2>/dev/null || true)"
        if [ -z "$pr" ]; then
            echo "${bad}⚠${rst} $branch has no merged PR; origin/main does not include it yet" >&2
            exit 1
        fi
        echo "${dim}$branch merged as #$pr${rst}"
    fi
    in_ro() { just --justfile "$ro/justfile" --working-directory "$ro" "$@"; }
    # The clone's own sync, so the version that runs is the one installed.
    in_ro sync
    echo
    for step in "link $HOME/bin" "link-skills $HOME/.claude/skills"; do
        # shellcheck disable=SC2086
        out="$(in_ro $step)"
        printf '%s\n' "$out" | grep '^  linked' || true
        skipped="$(printf '%s\n' "$out" | grep -c '^  skipped' || true)"
        [ "$skipped" = "0" ] || echo "${dim}${step%% *}: $skipped skipped, pointing somewhere other than $ro${rst}"
    done
    echo "${ok}shipped.${rst}"

# Report how far behind origin/main a read-only clone is, and when it last synced
stale:
    #!/usr/bin/env bash
    set -euo pipefail
    # Staleness is invisible to any check that trusts local refs: a clone that
    # has not fetched reports its own stale origin/main as the truth and looks
    # up to date. This one was six commits behind while claiming to be current,
    # which meant every session was loading skills six commits old with nothing
    # saying so. So fetch first, always.
    git fetch origin --no-tags --quiet
    behind="$(git rev-list --count HEAD..origin/main)"
    last="$(git tag --list 'synced/*' --sort=-refname | head -1)"

    echo "clone:       $(pwd)"
    echo "at:          $(git rev-parse --short HEAD) $(git log -1 --format=%s)"
    echo "last synced: ${last:-never (no synced/* tag)}"

    if [ "$behind" -eq 0 ]; then
        echo "status:      up to date with origin/main"
        exit 0
    fi

    echo "status:      BEHIND origin/main by $behind commit(s)"
    echo
    git --no-pager log --oneline --no-decorate HEAD..origin/main | sed 's/^/  /'
    echo
    echo "Run 'just sync' to fast-forward (it tags this state first)."

# Render the claude-graph demo (synthetic data); pass --svg PATH to export
graph-demo *args:
    uv run claude-graph/tools/demo.py {{args}}
