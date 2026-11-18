#!/usr/bin/env bash
# shellcheck disable=SC2218  # shellcheck 0.11.0 false positive: die() is defined
# at the top of this file, before every call site (verified by running them).
# Approve (or revoke) a mod request.
#
# The only approval path; a maintainer tool, not part of the agent's surface.
#
# What `approve` does, in this order:
#   1. fetch the issue as it is right now and refuse anything that is not open
#      with a body that is a mod request,
#   2. validate its body with the format script (an invalid body is never
#      approved),
#   3. refuse while another approved request shares a package with this one
#      (whatever its version or diff state), unless
#      this body declares it in `supersedes` -- in which case the declared issue
#      must be a genuine supersede with CI's signed applied record, and is
#      closed first,
#   4. pin sha256(canonical body) in a new issue comment,
#   5. add the "approved" label.
#
# Steps 4 and 5 are the only writes that are not already committed by step 3, so
# a failure there reopens the issue step 3 closed: an approval that half
# happened must not leave the version with no live request at all.
#
# The pin is what binds the approval to content: `update-aur` recomputes the
# hash of the live body and only applies when the newest pin from a maintainer
# matches. The hash covers the fields CI reads and the payloads it applies (see
# BOUND_FIELDS in mod-request.py); editing any of those voids the approval,
# while the free-text `summary` and `evidence` do not.
#
# Step 3 is a gate, not a guarantee: it runs in one clone and cannot be atomic,
# so `update-aur` maps each package to its one live request on the strength of
# this decision rather than re-checking it. Its job is to catch the mistake
# where a maintainer is looking at it.
set -euo pipefail

usage() {
  cat <<'USAGE'
usage: approve-mod-request.sh [--dry-run] <issue-number>
       approve-mod-request.sh [--dry-run] --revoke <issue-number>

  --dry-run   show what would be pinned, change nothing
  --revoke    drop the "approved" label instead of adding it
  -h, --help  this text
USAGE
}

die() {
  printf 'approve-mod-request: %s\n' "$*" >&2
  exit 1
}

issue=""
mode=approve
dry_run=0

while [ $# -gt 0 ]; do
  case "$1" in
    -h | --help)
      usage
      exit 0
      ;;
    --revoke)
      mode=revoke
      shift
      ;;
    --dry-run)
      dry_run=1
      shift
      ;;
    -*)
      die "unknown option: $1 (see --help)"
      ;;
    *)
      [ -z "$issue" ] || die "unexpected argument: $1"
      issue=$1
      shift
      ;;
  esac
done

[ -n "$issue" ] || {
  usage >&2
  exit 2
}
case "$issue" in
  *[!0-9]*) die "issue number must be numeric, got: $issue" ;;
esac

# Without this, a missing tool surfaces as a misleading failure further down
# (no python3 reads as "does not pass the format check", no git as "no origin").
for tool in git gh jq python3 ssh-keygen; do
  command -v "$tool" > /dev/null || die "missing required tool: $tool"
done

# Resolve the format script from this file's own location, so the approver can
# run it from any working directory.
here=$(cd -- "$(dirname -- "$0")" && pwd -P)
root=$(cd -- "$here/.." && pwd -P)
fmt="$here/mod-request.py"
[ -f "$fmt" ] || die "missing $fmt"

# The target repository is this clone's origin; there is no option to point the
# script elsewhere.
url=$(git -C "$root" config --get remote.origin.url 2>/dev/null) ||
  die "no 'origin' remote in $root; cannot tell which repository to approve in"
case "$url" in
  git@github.com:*) repo=${url#git@github.com:} ;;
  ssh://git@github.com/*) repo=${url#ssh://git@github.com/} ;;
  https://github.com/*) repo=${url#https://github.com/} ;;
  *) die "origin is not a GitHub remote: $url" ;;
esac
repo=${repo%.git}
case "$repo" in
  */*/* | /* | */ | "") die "cannot parse owner/repo from origin: $url" ;;
esac

# Approval is a maintainer action. Refuse early rather than leave a pin that
# `update-aur` would silently ignore (its author association is not trusted).
if [ "$dry_run" -eq 0 ]; then
  can_push=$(gh api "repos/$repo" --jq '.permissions.push // false' 2>/dev/null) ||
    die "cannot read repository permissions for $repo"
  [ "$can_push" = "true" ] ||
    die "your gh account has no write access to $repo; approval must come from a maintainer"
fi

if [ "$mode" = revoke ]; then
  if [ "$dry_run" -eq 1 ]; then
    printf 'would remove the "approved" label from %s#%s\n' "$repo" "$issue"
    exit 0
  fi
  gh issue edit "$issue" -R "$repo" --remove-label approved
  printf 'revoked: removed the "approved" label from %s#%s\n' "$repo" "$issue"
  exit 0
fi

tmpdir=$(mktemp -d "${MR_TMPDIR:-${TMPDIR:-/tmp}}/mr-approve.XXXXXX")
trap 'rm -rf "$tmpdir"' EXIT
body="$tmpdir/body.md"

# MR_APPROVE_BODY_FILE exists for rehearsals and tests. It cannot be used to
# sneak content past the gate: the pin it produces is the hash of whatever is in
# that file, and `update-aur` compares it against the live issue body.
if [ -n "${MR_APPROVE_BODY_FILE:-}" ]; then
  [ -f "$MR_APPROVE_BODY_FILE" ] ||
    die "MR_APPROVE_BODY_FILE=$MR_APPROVE_BODY_FILE does not exist"
  cp "$MR_APPROVE_BODY_FILE" "$body"
else
  gh issue view "$issue" -R "$repo" --json body,state > "$tmpdir/issue.json" ||
    die "cannot read issue #$issue from $repo"
  jq -r '.body // ""' "$tmpdir/issue.json" > "$body"

  # A pin on a closed issue is never applied: `update-aur` only lists open
  # issues, so the approval would be an artefact that reads like a decision.
  issue_state=$(jq -r '.state // ""' "$tmpdir/issue.json")
  [ "$issue_state" = OPEN ] ||
    die "issue #$issue is ${issue_state:-of unknown state}, not OPEN; reopen it before approving"
fi
[ -s "$body" ] || die "issue #$issue has an empty body"

python3 "$fmt" --root "$root" check -f "$body" > /dev/null ||
  die "issue #$issue does not pass the format check; nothing was approved"

hash=$(python3 "$fmt" --root "$root" hash -f "$body")
[ -n "$hash" ] || die "could not compute the body hash"

state=$(python3 "$fmt" --root "$root" parse -f "$body" | jq -r .diff_state)
target=$(python3 "$fmt" --root "$root" target -f "$body")
[ -n "$target" ] || die "issue #$issue has no target_version; nothing was approved"

# --- one live request per package set ---------------------------------------
# The rules live in `mod-request.py gate`: a `supersedes` that
# only this script checked would still be an unchecked claim the moment CI acted
# on it, so the same evaluator is what CI is required to run at apply time.  This
# script only fetches what the gate reads and performs the one side effect it asks
# for -- closing the replaced issue (before the pin, so no moment leaves two live
# requests for one package).  `approved` is the only label involved, and the pool
# is the same one CI reads.
# gh truncates at the limit silently and a truncated list reads as "nothing
# holds this package", so the limit and the refusal threshold are one variable.
cap=100
gh issue list -R "$repo" --state open --label approved --limit "$cap" \
  --json number,body > "$tmpdir/open-approved.json" ||
  die "cannot list the open approved issues in $repo"
if [ "$(jq 'length' "$tmpdir/open-approved.json")" -ge "$cap" ]; then
  die "$cap open approved issues (the fetch cap); close the stale ones and re-run"
fi

declared=$(python3 "$fmt" --root "$root" parse -f "$body" | jq -r '.supersedes // ""')
gate_args=(--issue "$issue" --body "$body" --open-approved "$tmpdir/open-approved.json")
old=""
if [ -n "$declared" ]; then
  [ "$declared" = "#$issue" ] && die "issue #$issue cannot supersede itself"
  old=${declared#\#}
  gh issue view "$old" -R "$repo" --json body,state,comments > "$tmpdir/old.json" ||
    die "cannot read issue #$old, the one this request says it supersedes"
  gate_args+=(--old "$tmpdir/old.json")
fi

set +e
verdict=$(python3 "$fmt" --root "$root" gate "${gate_args[@]}")
rc=$?
set -e
case "$rc" in
  0 | 1) ;;
  *) die "the gate could not decide (exit $rc); nothing was approved" ;;
esac
if ! jq -e 'has("ok")' <<<"$verdict" > /dev/null 2>&1; then
  die "the gate produced no verdict (exit $rc); nothing was approved"
fi

if [ "$rc" -ne 0 ]; then
  {
    printf 'approve-mod-request: refusing to approve #%s for %s:\n' \
      "$issue" "$(jq -r '.target_version // "?"' <<<"$verdict")"
    jq -r '.reasons[]' <<<"$verdict" | sed 's/^/  /'
    printf 'one live request per package set; nothing was approved.\n'
    cat <<EOF
ways out:
  - nothing applied yet: edit that request in place (a body edit needs a new
    approval anyway), do not file a second issue for the same package
    set
  - it was applied and this request replaces it: put its number in supersedes
  - it is done with: close it as superseded, or revoke it
      gh issue close <n> -R $repo -c "Closed in favour of #$issue"
      $0 --revoke <n>
EOF
  } >&2
  exit 1
fi

# Closing happens before the pin: from the moment this script returns, the
# version has exactly one live request again.
closed=""
close_num=$(jq -r '.close // ""' <<<"$verdict")
if [ -n "$close_num" ]; then
  closed=$close_num
  if [ "$dry_run" -eq 1 ]; then
    printf 'would close #%s as superseded ("Closed in favour of #%s")\n' "$closed" "$issue"
  else
    gh issue close "$closed" -R "$repo" -c "Closed in favour of #$issue" > /dev/null ||
      die "cannot close #$closed; nothing was approved (close it yourself and re-run)"
  fi
fi

# The close above is the one write this script cannot take back cheaply, so it is
# undone by hand if the approval does not complete: a version with no live
# request is worse than a request that stays open a little longer.
reopen_rolled_back() {
  [ -n "$closed" ] || return 0
  printf 'approve-mod-request: %s; reopening #%s so %s still has a live request\n' \
    "$1" "$closed" "$target" >&2
  gh issue reopen "$closed" -R "$repo" \
    -c "Reopened: approving #$issue did not complete, so this request still holds v$target." \
    > /dev/null 2>&1 ||
    printf 'approve-mod-request: could not reopen #%s -- reopen it by hand\n' "$closed" >&2
}

marker="<!-- mr-approved sha256=$hash -->"
comment="$marker

Approved the bound content of this request (sha256 \`${hash:0:12}…\`): the three
selections, target_version, next_update, diff_state, supersedes, and every
payload (diff + message). Editing any of those voids this approval and needs a
new one; \`summary\` and \`evidence\` are free text and do not affect the hash."

if [ "$dry_run" -eq 1 ]; then
  printf 'repo:   %s\nissue:  #%s\ndiff_state: %s\nsupersedes: %s\nsha256: %s\n\n' \
    "$repo" "$issue" "$state" "${declared:-(none)}" "$hash"
  printf 'would post this comment:\n%s\n\nwould add the "approved" label\n' "$comment"
  exit 0
fi

if ! url=$(gh api "repos/$repo/issues/$issue/comments" -f body="$comment" --jq '.html_url'); then
  reopen_rolled_back "the approval pin could not be posted"
  die "the approval pin could not be posted; nothing was approved"
fi
# `mod-request` arrives with the Issue Form, `approved` is this script's own:
# create it when the repository has no such label yet, or the write below would
# fail with the pin already posted.  `--force` makes this idempotent (an existing
# label keeps the same description and colour).  A failure here is not fatal on
# its own -- the add-label below is the authoritative attempt and reports it.
gh label create approved -R "$repo" --force \
  --description "mod request approved for the update pipeline" --color 0E8A16 \
  > /dev/null 2>&1 || true
# Without the label CI never lists the issue, so a pin alone is inert -- which is
# the safe direction, but it is not an approval either.
if ! gh issue edit "$issue" -R "$repo" --add-label approved > /dev/null; then
  reopen_rolled_back 'the "approved" label could not be added'
  die 'the "approved" label could not be added; nothing was approved'
fi

printf 'approved %s#%s\n  sha256: %s\n  pin:    %s\n' "$repo" "$issue" "$hash" "$url"
if [ -n "$closed" ]; then
  printf '  closed: #%s as superseded ("Closed in favour of #%s")\n' "$closed" "$issue"
fi
if [ "$state" = "pending" ]; then
  printf '  note:   diff_state=pending — this only gates version bumps, nothing will be applied\n'
else
  printf '  note:   editing a bound field from now on invalidates this approval\n'
fi
