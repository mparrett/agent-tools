#!/usr/bin/env bash
# collect.sh — pull normalized PR/issue facts for the pr-dashboard skill.
#
# Emits ONE JSON object to stdout. This is the "facts" layer: numbers, branches,
# stack links (base->head), CI state, review decision, diff size, and the
# issue<->PR links that connect open issues to the work covering them. The LLM's
# job downstream is only grouping + prioritization, never re-deriving these.
#
# Usage:
#   collect.sh [--author <login|@me>|--all|--waiting-on <login|@me>] [--merged-since <date>] [repo ...]
#
#   --author @me         (default) only PRs you opened — the "where's my work" view
#   --author <login>     only PRs by that user
#   --all                every open PR — the "maintainer / what must I review" view
#   --waiting-on <login> other people's open PRs where <login> owes the next move:
#                        a pending review request, an approval the head has moved
#                        past, changes requested that the author has answered, or
#                        an open thread <login> is in where the author spoke last.
#   --merged-since <date> also list PRs merged on/after <date>, for a "what landed"
#                        recap. <date> is YYYY-MM-DD, or today|yesterday|Nd (e.g. 7d).
#
#   repos default to the current directory's GitHub repo (owner/repo).
#
# Examples:
#   collect.sh                                   # my open PRs in the current repo
#   collect.sh --all                             # everything open here (maintainer triage)
#   collect.sh --all --merged-since today        # open PRs + everything merged today
#   collect.sh --author me --merged-since 7d owner/a   # my last-week merges in owner/a
set -euo pipefail

# Resolve a friendly date token to YYYY-MM-DD. Works on macOS (date -v) and
# GNU/Linux (date -d); an explicit YYYY-MM-DD passes straight through.
resolve_date() {
  case "$1" in
    today)     date +%F ;;
    yesterday) date -v-1d +%F 2>/dev/null || date -d 'yesterday' +%F ;;
    [0-9]*d)   local n="${1%d}"; date -v-"${n}"d +%F 2>/dev/null || date -d "$n days ago" +%F ;;
    *)         printf '%s' "$1" ;;
  esac
}

AUTHOR="@me"
WAITING_ON=""
SINCE=""
REPOS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --all) AUTHOR=""; shift ;;
    --author) AUTHOR="$2"; shift 2 ;;
    --waiting-on) WAITING_ON="$2"; AUTHOR=""; shift 2 ;;
    --merged-since|--since) SINCE=$(resolve_date "$2"); shift 2 ;;
    -h|--help) awk 'NR>1 && /^#/{sub(/^# ?/,"");print;next} NR>1{exit}' "$0"; exit 0 ;;
    *) REPOS+=("$1"); shift ;;
  esac
done
command -v gh >/dev/null || { echo "gh not found" >&2; exit 1; }
command -v jq >/dev/null || { echo "jq not found" >&2; exit 1; }

# Default to the current directory's GitHub repo when none named.
if [[ ${#REPOS[@]} -eq 0 ]]; then
  cur=$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || true)
  [[ -n "$cur" ]] && REPOS=("$cur") || {
    echo "No repo given and current dir isn't a GitHub repo. Pass owner/repo [owner/repo ...]." >&2; exit 1; }
fi

PR_FIELDS='number,title,headRefName,baseRefName,isCrossRepository,isDraft,mergeable,mergeStateStatus,reviewDecision,labels,url,updatedAt,additions,deletions,author,body,statusCheckRollup,closingIssuesReferences'

# Extract issue numbers an issue<->PR link implies from a PR body. GitHub's own
# closing keywords (close/fix/resolve + #N) are the reliable signal; we union
# these with the API's closingIssuesReferences because many repos wire issues
# through the body rather than the "Development" sidebar link.
BODY_CLOSES_DEF='
def body_closes($b):
  [ ($b // "") | scan("(?i)(?:clos(?:e|es|ed)|fix(?:es|ed)?|resolv(?:e|es|ed))\\s+#([0-9]+)") | .[0] | tonumber ];
def pr_closes:
  ( ([ .closingIssuesReferences[]?.number ] + body_closes(.body)) | unique );
'

# Open-PR normalizer: derives ciState, stackedOn (base matches another open PR's
# head), and closes (issue links); trims heavy fields to a short body excerpt.
# Only same-repo heads count as a parent: a fork's branch can never be a PR's
# base, and a fork PR opened from its own `main` would otherwise make every PR
# into `main` look stacked on it.
JQ_PROG='
# Classify one rollup entry. Two shapes coexist: modern CheckRun (has .status +
# .conclusion) and legacy StatusContext (has only .state). Reserve "pass" for
# explicitly green terminal states so CANCELLED/ERROR/etc. never read as green.
def cistate:
  if has("status") then                      # CheckRun
    if .status != "COMPLETED" then "pending"
    elif (.conclusion == "SUCCESS" or .conclusion == "NEUTRAL" or .conclusion == "SKIPPED") then "pass"
    else "fail" end
  elif has("state") then                      # legacy StatusContext
    if .state == "SUCCESS" then "pass"
    elif (.state == "PENDING" or .state == "EXPECTED") then "pending"
    else "fail" end
  else "pending" end;
def ci($r):
  ($r // []) as $r
  | if ($r|length)==0 then "none"
    else ($r | map(cistate)) as $s
      | if ($s|any(. == "fail")) then "fail"
        elif ($s|any(. == "pending")) then "pending"
        else "pass" end
    end;
. as $all
| (map(select(.isCrossRepository | not) | {(.headRefName): .number}) | add // {}) as $heads
| map({
    number, title,
    branch: .headRefName,
    base: .baseRefName,
    draft: .isDraft,
    mergeable,
    mergeState: .mergeStateStatus,    # CLEAN / BEHIND / BLOCKED / DIRTY / UNSTABLE / ...
    review: .reviewDecision,          # APPROVED / REVIEW_REQUIRED / CHANGES_REQUESTED / null
    ci: ci(.statusCheckRollup),       # pass / fail / pending / none
    closes: pr_closes,                # issue #s this PR would close (refs + body keywords)
    labels: [.labels[].name],
    author: .author.login,
    adds: .additions, dels: .deletions,
    url, updatedAt,
    stackedOn: ($heads[.baseRefName] // null),   # PR# this is stacked on, else null (=off default branch)
    bodyExcerpt: ((.body // "") | gsub("\r";"") | .[0:400])
  })'

# owedBy / owedWhy: whose turn it is, and on what evidence. "author",
# "reviewer", "maintainer", or null for a draft.
# Applied after lastTouch is merged in, because it needs both that and .review.
#
# reviewDecision alone cannot answer this. GitHub holds it at CHANGES_REQUESTED
# until the *same reviewer* submits a new review — the author pushing a fix,
# replying, or a second reviewer approving all leave it set. On a repo whose
# reviewers don't habitually re-review it decays into "someone once objected",
# which is a fact about the past, not about whose turn it is. Pairing it with
# lastTouch (most recent human timeline event) is what separates "author still
# owes work" from "the badge is stale and a reviewer owes a second look".
#
# "author-responded" is gated on lastTouch.by == .author, not on "something
# happened since the request". A reviewer's own follow-up comment is also a
# post-request touch, and treating it as a response hands the ball back to the
# reviewer while the author has not moved — the same staleness this field
# exists to remove, one event later. Anyone other than the author touching it
# leaves the PR owed by the author.
#
# Two inputs can be absent rather than false, and both report why="unknown"
# rather than guessing. lastTouch is best-effort and degrades to null on a
# GraphQL failure. `mergeable` is UNKNOWN whenever GitHub is still recomputing
# the merge — which it does for every open PR each time the base branch moves,
# so after any merge a whole board reads UNKNOWN for a while. Testing
# `!= "CONFLICTING"` there silently reports conflicted PRs as ready-to-merge:
# observed on #648/#650/#666, all three genuinely DIRTY. A missing measurement
# must not be able to masquerade as an answer — the whole failure this field
# exists to prevent.
OWED_JQ='
def owed:
  if .draft then {by: null, why: null}
  elif .review == "APPROVED" then
    (if .mergeable == "CONFLICTING"   then {by: "author",     why: "needs-rebase"}
     elif .mergeable == "MERGEABLE"   then {by: "maintainer", why: "ready-to-merge"}
     else                                  {by: null,         why: "unknown"} end)
  elif .review == "CHANGES_REQUESTED" then
    (if .lastTouch == null                                then {by: null,       why: "unknown"}
     elif .lastTouch.kind == "review:approved"            then {by: "reviewer", why: "peer-approved"}
     elif .lastTouch.by == .author                        then {by: "reviewer", why: "author-responded"}
     else                                                      {by: "author",   why: "changes-requested"} end)
  else {by: "reviewer", why: "unreviewed"} end;
map(owed as $o | . + {owedBy: $o.by, owedWhy: $o.why})'

# mergeReady: "can I even think about merging this", as a checklist of facts.
# Each check is pass / fail / warn / unknown at level "block" or "warn". The
# verdict is "blocked" on any failed block-level check, "unknown" when a
# block-level input was not measured (a missing fact must not read as ready),
# and otherwise "ready" -- warnings ride along without blocking.
#
# Conflicts are reported as GitHub reports them. In some repos a CONFLICTING
# box is a generated file a local merge driver resolves, which GitHub cannot
# run; the board does not try to tell those apart.
MERGE_READY_JQ='
def c($k; $lvl; $st; $note): {check: $k, level: $lvl, state: $st, note: $note};
map(. as $p | [
  (if .draft then c("draft"; "block"; "fail"; "draft") else c("draft"; "block"; "pass"; null) end),
  (if .ci == "pass" then c("ci"; "block"; "pass"; null)
   elif .ci == "none" then c("ci"; "warn"; "warn"; "no CI checks")
   else c("ci"; "block"; "fail"; "CI " + .ci) end),
  (if .approvals == null then c("approval"; "block"; "unknown"; "approvals not measured")
   elif (.approvals.atHead | length) > 0 then c("approval"; "block"; "pass"; null)
   elif (.approvals.stale | length) > 0 then c("approval"; "block"; "fail"; "approval predates head")
   else c("approval"; "block"; "fail"; "no approval") end),
  (if .changesRequested == null then c("changes"; "block"; "unknown"; "reviews not measured")
   elif (.changesRequested | length) > 0 then c("changes"; "block"; "fail"; "changes requested by " + (.changesRequested | join(", ")))
   else c("changes"; "block"; "pass"; null) end),
  (if .mergeable == "MERGEABLE" then c("conflicts"; "block"; "pass"; null)
   elif .mergeable == "CONFLICTING" then c("conflicts"; "block"; "fail"; "merge conflicts")
   else c("conflicts"; "block"; "unknown"; "GitHub still computing mergeability") end),
  (if .threads == null then c("threads"; "block"; "unknown"; "threads not measured")
   elif .threads.unresolved == 0 then c("threads"; "block"; "pass"; null)
   else c("threads"; "block"; "fail"; (.threads.unresolved | tostring) + " unresolved thread(s)"
          + (if .threads.replied != null then ", " + ((.threads.unresolved - .threads.replied) | tostring) + " unanswered" else "" end)) end),
  (if .threads == null or .threads.resolvedUnanswered == null then c("resolved-unanswered"; "warn"; "unknown"; null)
   elif .threads.resolvedUnanswered > 0 then c("resolved-unanswered"; "warn"; "warn"; (.threads.resolvedUnanswered | tostring) + " resolved without a reply")
   else c("resolved-unanswered"; "warn"; "pass"; null) end),
  (if .stackedOn != null then c("stack"; "block"; "fail"; "merge #" + (.stackedOn | tostring) + " first")
   elif .base != $default then c("stack"; "block"; "fail"; "base is " + .base)
   else c("stack"; "block"; "pass"; null) end),
  (if .reviewers == null then c("pending-reviews"; "warn"; "unknown"; null)
   elif (.reviewers.requested | length) > 0 then c("pending-reviews"; "warn"; "warn"; "awaiting " + (.reviewers.requested | join(", ")))
   else c("pending-reviews"; "warn"; "pass"; null) end),
  (if .mergeState == "BEHIND" then c("behind"; "warn"; "warn"; "behind " + $default)
   else c("behind"; "warn"; "pass"; null) end)
] as $checks
| . + {mergeReady: {
    verdict: (if ($checks | any(.level == "block" and .state == "fail")) then "blocked"
              elif ($checks | any(.level == "block" and .state == "unknown")) then "unknown"
              else "ready" end),
    checks: $checks}})'

# waitingOn: why an open PR by someone else is waiting on $who, one string per
# reason; PRs with none are dropped. Each reason is a move only $who can make.
# "changes requested, author has answered" leans on lastTouch being the
# author's -- the same evidence owedBy uses for author-responded.
WAITING_JQ='
map(select(.author != $who and (.draft | not))
  | . + {waitingOn: ([
      (if ((.reviewers.requested // []) | index($who)) then "review requested" else empty end),
      (if ((.approvals.stale // []) | index($who)) then "your approval predates the head" else empty end),
      (if (((.changesRequested // []) | index($who)) and .lastTouch.by == .author)
       then "you requested changes; the author has answered" else empty end),
      (if ((.threads.repliedTo // {})[$who] // 0) > 0
       then "\((.threads.repliedTo[$who]) | tostring) thread(s) answered, your move" else empty end)
    ])}
  | select(.waitingOn | length > 0))'

# Merged-PR normalizer (lighter: no CI/review/mergeable once landed).
MERGED_JQ='
map({
  number, title,
  branch: .headRefName,
  mergedAt: .mergedAt,
  closes: pr_closes,
  labels: [.labels[].name],
  author: .author.login,
  adds: .additions, dels: .deletions,
  url
}) | sort_by(.mergedAt) | reverse'

# lastTouch: who most recently did *something* on an open PR, and what kind —
# a comment, a review (state change), a plain commit, or a force-push. Not in
# `gh pr list --json` (no such field); requires one extra GraphQL round trip
# per repo, batched as one aliased query covering every open PR at once
# rather than one call per PR. Best-effort: a failure here degrades to
# `lastTouch: null` on every PR, it never fails the whole collect.sh run.
# Bot actors (CI comment bots etc. — __typename "Bot") are dropped before
# picking the max: a chatty github-actions comment bot would otherwise mask
# the last actual human touch, which is the whole point of this field.
# Two more exclusions: PENDING reviews (an unsubmitted draft, visible only to
# its author — not a real touch, and it has no verb in the UI), and commits
# whose author isn't linked to a GitHub account (no `user.login`) — those
# carry only a display name, which can't be matched against the PR's login
# `author`, so they'd defeat the self-push suppression downstream.
LAST_TOUCH_JQ='
  .data.repository
  | to_entries
  | map({
      number: (.key | ltrimstr("pr_") | tonumber),
      touch: ([ .value.timelineItems.nodes[] |
        if .__typename == "IssueComment" and .author.__typename != "Bot" then
          {kind: "comment", by: .author.login, at: .createdAt}
        elif .__typename == "PullRequestReview" and .author.__typename != "Bot" and .state != "PENDING" then
          {kind: ("review:" + (.state // "commented" | ascii_downcase)), by: .author.login, at: .createdAt}
        elif .__typename == "PullRequestCommit" and .commit.author.user.login != null then
          {kind: "commit", by: .commit.author.user.login, at: .commit.committedDate}
        elif .__typename == "HeadRefForcePushedEvent" and .actor.__typename != "Bot" then
          {kind: "force-push", by: .actor.login, at: .createdAt}
        else empty end
      ] | sort_by(.at) | last)
    })
  | map(select(.touch != null))
  | map({(.number | tostring): .touch})
  | add // {}'

# approvals / threads: two review signals reviewDecision flattens away.
#
# approvals.atHead vs .stale — reviewDecision stays APPROVED after the author
# pushes again, so "approved" can describe code nobody has seen. An approval
# counts as at-head only when its review was submitted on the current
# headRefOid. latestOpinionatedReviews holds each reviewer's latest
# approve/request-changes, so a reviewer who approved and later objected is not
# counted as approving. A server-side "Update branch" moves the head too, which
# makes every approval stale — correct, since the merged result is unreviewed.
#
# threads.unresolved — unresolved inline review threads, with .outdated the
# subset whose anchor lines have since changed. A PR can be APPROVED with
# threads still open, and nothing else on the board shows that. .replied
# counts the open threads whose last comment is the PR author's: the author
# has answered and the reviewer owes the next move (resolve or push back).
# The last speaker, not "did the author ever comment", because a reviewer's
# follow-up hands the thread back to the author. .resolvedUnanswered counts
# resolved threads where someone else commented and the author never did:
# a comment that got closed without a reply. Only the last 20 comments of a
# thread are fetched; a longer thread without the author among them is
# unmeasured, so the count goes null.
#
# .repliedTo maps each other participant to the number of open threads they
# are in where the author spoke last -- threads waiting on them, which is what
# --waiting-on reads.
#
# changesRequested — reviewers whose latest opinionated review requests
# changes. It blocks the merge even when someone else has approved.
#
# reviewers — {requested, reviewed}: review requests still pending, and
# everyone else who has submitted a review. GitHub drops a request once its
# reviewer reviews, so neither list alone is "who is on this PR".
# .lastRequestedAt is the newest review (re-)request, which the template uses
# to tell "just asked" from "due a nudge".
#
# comments — conversation comments plus every inline review comment.
#
# Either is null when not measured (GraphQL failure, or more than one page),
# never 0: a truncated count would read as "nothing outstanding".
REVIEW_SIGNALS_JQ='
  (.data.repository // {})
  | with_entries(
      .key |= ltrimstr("pr_")
      | .value |= {
          approvals: (
            if (.headRefOid == null or .latestOpinionatedReviews == null
                or .latestOpinionatedReviews.pageInfo.hasNextPage) then null
            else .headRefOid as $h
              | [ .latestOpinionatedReviews.nodes[] | select(.state == "APPROVED") ] as $a
              | { atHead: [ $a[] | select(.commit.oid == $h) | .author.login ],
                  stale:  [ $a[] | select(.commit.oid != $h) | .author.login ] }
            end),
          changesRequested: (
            if (.latestOpinionatedReviews == null or .latestOpinionatedReviews.pageInfo.hasNextPage) then null
            else [ .latestOpinionatedReviews.nodes[] | select(.state == "CHANGES_REQUESTED") | .author.login ]
            end),
          threads: (
            if (.reviewThreads == null or .reviewThreads.pageInfo.hasNextPage) then null
            else .author.login as $author
              | [ .reviewThreads.nodes[] | select(.isResolved | not) ] as $u
              | { unresolved: ($u | length),
                  outdated: ([ $u[] | select(.isOutdated) ] | length),
                  replied:  (if ($u | all(.comments != null))
                             then [ $u[] | select((.comments.nodes | last).author.login == $author) ] | length
                             else null end),
                  repliedTo: (if ($u | all(.comments != null))
                             then [ $u[] | select((.comments.nodes | last).author.login == $author)
                                    | [ .comments.nodes[].author.login | select(. != null and . != $author) ] | unique[] ]
                                  | group_by(.) | map({(.[0]): length}) | add // {}
                             else null end),
                  resolvedUnanswered: (
                    [ .reviewThreads.nodes[] | select(.isResolved) ] as $r
                    | if ($r | all(.comments != null)) | not then null
                      else [ $r[] | [ .comments.nodes[].author.login ] as $by
                             | if ($by | index($author)) then "answered"
                               elif .comments.totalCount > ($by | length) then "unknown"
                               elif ($by | any(. != $author)) then "unanswered"
                               else "answered" end ] as $v
                        | if ($v | index("unknown")) then null
                          else [ $v[] | select(. == "unanswered") ] | length end
                      end) }
            end),
          reviewers: (
            if (.reviewRequests == null or .latestReviews == null
                or .reviewRequests.pageInfo.hasNextPage or .latestReviews.pageInfo.hasNextPage) then null
            else .author.login as $author
              | { requested: [ .reviewRequests.nodes[].requestedReviewer | (.login // .name) | select(. != null) ],
                  reviewed:  [ .latestReviews.nodes[] | select(.state != "PENDING") | .author.login
                               | select(. != null and . != $author) ] | unique,
                  lastRequestedAt: (.reviewRequestedAt.nodes[0].createdAt // null) }
            end),
          comments: (
            if (.comments.totalCount == null or .reviewThreads == null
                or .reviewThreads.pageInfo.hasNextPage
                or (.reviewThreads.nodes | any(.comments.totalCount == null))) then null
            else .comments.totalCount + ([ .reviewThreads.nodes[].comments.totalCount ] | add // 0)
            end)
        })'

# One aliased pullRequest(number: N) field per PR, each asking for its last 8
# timeline events among the four kinds above (still cheap — a few dozen nodes
# per PR at most) and, since itemTypes filters before pagination, that's
# already the most recent matching events in order; the wider-than-minimal
# window is margin against a chatty CI bot occupying the tail end.
build_pr_graph_query() {
  local owner="$1" name="$2"; shift 2
  local frag=""
  for n in "$@"; do
    frag+=" pr_$n: pullRequest(number: $n) { timelineItems(last: 8, itemTypes: [ISSUE_COMMENT, PULL_REQUEST_REVIEW, PULL_REQUEST_COMMIT, HEAD_REF_FORCE_PUSHED_EVENT]) { nodes { __typename ... on IssueComment { author { login __typename } createdAt } ... on PullRequestReview { author { login __typename } createdAt state } ... on PullRequestCommit { commit { committedDate author { user { login } name } } } ... on HeadRefForcePushedEvent { actor { login __typename } createdAt } } } headRefOid latestOpinionatedReviews(first: 100) { pageInfo { hasNextPage } nodes { state author { login } commit { oid } } } reviewThreads(first: 100) { pageInfo { hasNextPage } nodes { isResolved isOutdated comments(last: 20) { totalCount nodes { author { login } } } } } author { login } comments { totalCount } reviewRequests(first: 100) { pageInfo { hasNextPage } nodes { requestedReviewer { ... on User { login } ... on Team { name } ... on Mannequin { login } } } } latestReviews(first: 100) { pageInfo { hasNextPage } nodes { state author { login } } } reviewRequestedAt: timelineItems(last: 1, itemTypes: [REVIEW_REQUESTED_EVENT]) { nodes { ... on ReviewRequestedEvent { createdAt } } } }"
  done
  printf 'query { repository(owner: "%s", name: "%s") { %s } }' "$owner" "$name" "$frag"
}

# The raw response, or {} on failure; LAST_TOUCH_JQ and REVIEW_SIGNALS_JQ each
# read their own fields out of it, so one round trip feeds both.
fetch_pr_graph() {
  local owner="$1" name="$2"; shift 2
  [[ $# -eq 0 ]] && { echo '{}'; return; }
  local q; q=$(build_pr_graph_query "$owner" "$name" "$@")
  # Capture rather than `gh ... || echo '{}'`: on failure gh still prints
  # GitHub's JSON error body to stdout, and the fallback appended to it made
  # two JSON documents that --argjson rejects, aborting the whole run.
  local out
  if out=$(gh api graphql -f query="$q" 2>/dev/null); then printf '%s' "$out"; else echo '{}'; fi
}

# Run a gh command, keeping stdout clean for JSON while capturing stderr. On
# failure, surface gh's real diagnostic (auth, 404, rate limit) and abort —
# rather than swallowing it and letting an empty result masquerade downstream
# as "invalid JSON text".
gh_or_die() {
  local what="$1"; shift
  local out errfile status
  errfile=$(mktemp)
  if out=$("$@" 2>"$errfile"); then
    rm -f "$errfile"
    printf '%s' "$out"
  else
    status=$?
    echo "collect.sh: $what failed (exit $status):" >&2
    cat "$errfile" >&2
    rm -f "$errfile"
    return 1     # caller must `|| exit 1` — set -e doesn't propagate out of $(...)
  fi
}

emit_repo() {
  local repo="$1"
  local defbranch base_sha prs issues merged closed_issues

  # First real API call — hard-fail here gives a clean signal that the repo is
  # missing or auth is broken, before anything else runs.
  defbranch=$(gh_or_die "gh repo view ($repo)" \
    gh repo view "$repo" --json defaultBranchRef -q .defaultBranchRef.name) || return 1
  # Cosmetic base-commit sha; a soft fallback is fine (non-fatal if unresolved).
  base_sha=$(gh api "repos/$repo/commits/$defbranch" -q '.sha[0:7]' 2>/dev/null || echo "???????")

  # Build the pr-list argv as an array so an empty --author (the --all view)
  # simply omits the flag — no nounset-unsafe expansion of an empty array.
  local prlist=(gh pr list --repo "$repo" --state open --limit 100 --json "$PR_FIELDS")
  [[ -n "$AUTHOR" ]] && prlist+=(--author "$AUTHOR")

  # pipefail carries a gh_or_die failure through the jq pipe to `|| return 1`.
  prs=$(gh_or_die "gh pr list ($repo)" "${prlist[@]}" | jq "$BODY_CLOSES_DEF$JQ_PROG") || return 1

  # lastTouch + review-signal enrichment (best-effort — see LAST_TOUCH_JQ).
  # The `${a[@]+"${a[@]}"}` guard is load-bearing on macOS: bash 3.2 treats
  # "${a[@]}" on an *empty* array as an unset variable, so under `set -u` a repo
  # with zero open PRs aborted the whole run instead of emitting a board with an
  # empty prs list. fetch_pr_graph already returns {} for no arguments.
  local pr_nums=()
  while IFS= read -r n; do pr_nums+=("$n"); done < <(echo "$prs" | jq -r '.[].number')
  local graph last_touch signals
  graph=$(fetch_pr_graph "${repo%%/*}" "${repo##*/}" ${pr_nums[@]+"${pr_nums[@]}"})
  last_touch=$(echo "$graph" | jq "$LAST_TOUCH_JQ" 2>/dev/null || echo '{}')
  signals=$(echo "$graph" | jq "$REVIEW_SIGNALS_JQ" 2>/dev/null || echo '{}')
  prs=$(echo "$prs" | jq --argjson lt "$last_touch" --argjson sg "$signals" \
          'map((.number|tostring) as $k
               | . + {lastTouch: ($lt[$k] // null),
                      approvals: ($sg[$k].approvals // null),
                      threads:   ($sg[$k].threads // null),
                      changesRequested: ($sg[$k].changesRequested // null),
                      reviewers: ($sg[$k].reviewers // null),
                      comments:  ($sg[$k].comments // null)})' \
        | jq "$OWED_JQ" | jq --arg default "$defbranch" "$MERGE_READY_JQ")
  if [[ -n "$WAITING_ON" ]]; then
    prs=$(echo "$prs" | jq --arg who "$WAITING_ON" "$WAITING_JQ")
  fi

  # Merged-since recap (optional).
  merged='[]'
  if [[ -n "$SINCE" ]]; then
    local mlist=(gh pr list --repo "$repo" --state merged --limit 100 --search "merged:>=$SINCE" \
      --json number,title,headRefName,mergedAt,url,additions,deletions,author,body,labels,closingIssuesReferences)
    [[ -n "$AUTHOR" ]] && mlist+=(--author "$AUTHOR")
    merged=$(gh_or_die "gh pr list merged ($repo)" "${mlist[@]}" | jq "$BODY_CLOSES_DEF$MERGED_JQ") || return 1
  fi

  issues=$(gh_or_die "gh issue list ($repo)" \
    gh issue list --repo "$repo" --state open --limit 100 --json number,title,labels,url,updatedAt \
             | jq 'map({number,title,labels:[.labels[].name],url,updatedAt})') || return 1

  # Recently-closed issues (only with --merged-since): a PR that genuinely
  # closes an issue closes it AT merge time, so the issue drops out of the
  # open list — "just landed" coverage can only ever come from closed issues.
  closed_issues='[]'
  if [[ -n "$SINCE" ]]; then
    closed_issues=$(gh_or_die "gh issue list closed ($repo)" \
      gh issue list --repo "$repo" --state closed --limit 100 --search "closed:>=$SINCE" \
               --json number,title,labels,url,closedAt \
               | jq 'map({number,title,labels:[.labels[].name],url,closedAt})') || return 1
  fi

  # Attach linkedPrs to each issue: the reverse of pr.closes. This is the
  # connective tissue — it turns a flat issue list into a coverage map: open
  # issues are unclaimed or in-flight; recently-closed ones with a merged PR
  # are the "just landed" rows.
  jq -n --arg repo "$repo" --arg branch "$defbranch" --arg sha "$base_sha" \
        --argjson prs "$prs" --argjson issues "$issues" --argjson merged "$merged" \
        --argjson closed "$closed_issues" \
    'def linked($n):
       [ $prs[]    | select((.closes // []) | index($n)) | {number, url, review, ci, state:"open"} ]
     + [ $merged[] | select((.closes // []) | index($n)) | {number, url, state:"merged"} ];
     { repo:$repo, defaultBranch:$branch, baseSha:$sha,
       prCount:($prs|length), mergedCount:($merged|length),
       prs:$prs, mergedPrs:$merged,
       openIssues:   [ $issues[] as $iss | $iss + { linkedPrs: linked($iss.number) } ],
       closedIssues: [ $closed[] as $iss | $iss + { linkedPrs: linked($iss.number) } ] }'
}

# NOTE: current date intentionally not stamped into output — the caller stamps
# it. (--merged-since resolves relative tokens via `date` above; that's an input
# boundary, not output state.)
if [[ "$WAITING_ON" == "@me" ]]; then
  WAITING_ON=$(gh_or_die "gh api user" gh api user -q .login) || exit 1
fi

sections=()
for r in "${REPOS[@]}"; do
  section=$(emit_repo "$r") || exit 1   # explicit: emit_repo already reported why
  sections+=("$section")
done
printf '%s\n' "${sections[@]}" | jq -s --arg author "${AUTHOR:-ALL}" --arg since "$SINCE" --arg who "$WAITING_ON" \
  '{scope:$author, waitingOn:(if $who=="" then null else $who end),
    mergedSince:(if $since=="" then null else $since end), repos:.}'
