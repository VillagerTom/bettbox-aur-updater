#!/usr/bin/env bash
# shellcheck disable=SC2218  # shellcheck 0.11.0 false positive: die() is defined
# at the top of this file, before every call site (verified by running them).
# Apply a mod request to the local AUR checkouts.
#
# A request is a diff nobody has built yet.  This turns one into working-tree
# changes in aur/<pkg>/ -- the payload's diff plus the pkgrel bump a packaging
# change needs -- so it can be built before anything is published.  Nothing is
# committed, staged or pushed: the tree is left dirty on purpose, and the revert
# command is printed.
#
# With -f BODY or ISSUE it applies that one request.  With neither, it applies
# every live request in turn -- that is what the update run calls: one call
# fetches the live requests, runs the gate, and writes.  It reports its
# decisions on stdout, machine-readable:
#
#   #mr-body <issue> <sha256>   the body it gated: the caller re-reads the body
#                               to sign the record and checks this hash
#   #mr-held <pkg> <issue>      the package must not move: still pending
#   #mr-applied <pkg> <issue>   the payload was written into aur/<pkg>
#
# A request is anchored to a base version, so applying it has a version rule: a
# package must have reached target_version (or be moved to it by the run the
# request belongs to), must not be newer than it, and a `next_update` request
# must not be re-applied to the version already published.  The approval gate
# also keeps one live request per package set, so each package maps to at most
# one live request here.
# `--force` ignores that rule -- that is the manual escape hatch; the update run
# never passes it.
set -euo pipefail

usage() {
  cat <<'USAGE'
usage: apply-mod-request.sh [-f BODY | ISSUE] [--pkg NAME]... [--no-pkgrel]
                            [--dry-run] [--force] [--versions FILE]
                            [--repo OWNER/REPO]
       apply-mod-request.sh --versions FILE [--repo OWNER/REPO] [--no-pkgrel]
                            [--dry-run]

Apply a mod request to the local checkouts.  Without -f/ISSUE every live request
is applied in turn (the update run's shape).

  -f, --file BODY  issue body file ('-' = stdin); default: read ISSUE with gh
  --pkg NAME       apply this package only (repeatable; default: all covered)
  --no-pkgrel      leave pkgrel alone: the caller bumps it instead
  --dry-run        run every check, change nothing
  --force          ignore the version rule: apply to a package that has not
                   reached target_version, is already past it, or would get a
                   next_update request on a version already published.  Manual
                   only; the update run never passes it.
  --versions FILE  per package "<pkg> <current> <target>" lines.  The target is
                   the version the run moves the package to; without the file, a
                   package is taken to stay on its PKGBUILD pkgver.
  --repo OWNER/REPO  repository to read (default: what gh infers from origin)
  -h, --help       this text

Applied one request per call, the report is human-readable.  Applied from the
live requests, it also prints the three machine lines above.
USAGE
}

die() {
  printf 'apply-mod-request: %s\n' "$*" >&2
  exit 1
}

note() {
  printf 'apply-mod-request: %s\n' "$*"
}

# An annotation in Actions, a plain warning elsewhere: the blocked package is the
# difference between "nothing to do" and "the channel is held", and it has to be
# visible without reading the whole log.
warn() {
  if [ "${GITHUB_ACTIONS:-}" = "true" ]; then
    printf '::warning::%s\n' "$*"
  else
    printf 'apply-mod-request: warning: %s\n' "$*" >&2
  fi
}

body_file=""
issue=""
dry_run=0
no_pkgrel=0
force=0
only_pkgs=()
versions=""
repo=""

while [ $# -gt 0 ]; do
  case "$1" in
    -h | --help)
      usage
      exit 0
      ;;
    -f | --file)
      [ $# -ge 2 ] || die "$1 needs an argument"
      body_file=$2
      shift 2
      ;;
    --pkg)
      [ $# -ge 2 ] || die "--pkg needs an argument"
      only_pkgs+=("$2")
      shift 2
      ;;
    --no-pkgrel)
      no_pkgrel=1
      shift
      ;;
    --dry-run)
      dry_run=1
      shift
      ;;
    --force)
      force=1
      shift
      ;;
    --versions)
      [ $# -ge 2 ] || die "--versions needs an argument"
      versions=$2
      shift 2
      ;;
    --repo)
      [ $# -ge 2 ] || die "--repo needs an argument"
      repo=$2
      shift 2
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

# No body and no issue number means "every live request": that shape reads the
# repository, so it needs a version vector to judge against.
live_mode=0
if [ -z "$body_file" ] && [ -z "$issue" ]; then
  live_mode=1
  [ -n "$versions" ] || {
    usage >&2
    exit 2
  }
  [ "$force" -eq 0 ] || die "--force applies one request at a time; give -f BODY or ISSUE"
  [ "${#only_pkgs[@]}" -eq 0 ] || die "--pkg applies one request at a time; give -f BODY or ISSUE"
fi
[ -z "$body_file" ] || [ -z "$issue" ] || die "give either -f BODY or ISSUE, not both"
[ -z "$versions" ] || [ -f "$versions" ] || die "no such versions file: $versions"
[ "$live_mode" -eq 0 ] || { [ -n "$versions" ] || die "no live requests without --versions"; }

need() {
  command -v "$1" > /dev/null || die "missing required tool: $1"
}
for tool in git jq python3 vercmp; do need "$tool"; done
# Reading requests (one by number, or all live ones) needs gh and, for the
# records a live request carries, ssh-keygen.
if [ "$live_mode" -eq 1 ] || [ -n "$issue" ]; then
  for tool in gh ssh-keygen; do need "$tool"; done
fi

# Tool diagnostics go into the report, and CI logs should not depend on the
# runner's locale (the format script pins the same two variables for git).
export LC_ALL=C LANGUAGE=C

# Resolve everything from this file's own location, so the script runs from any
# working directory and always edits the clone it lives in.
here=$(cd -- "$(dirname -- "$0")" && pwd -P)
root=$(cd -- "$here/.." && pwd -P)
fmt="$here/mod-request.py"
[ -f "$fmt" ] || die "missing $fmt"
form() { python3 "$fmt" --root "$root" "$@"; }

# 1.20.0pre1 and 1.20.0 are one version family: a request is anchored to the
# base, and a channel that is still on a pre-release has already reached it.
base_of() { printf '%s\n' "$1" | sed -E 's/pre[0-9]+$//'; }

# ---------------------------------------------------------------------------
# Applying one request.
#
# The body arrives as a file: -f/ISSUE is read into <tmp>/body.md before the
# call, and the live pass writes each live request's body into <tmp>/live/.  The
# checks below are the same either way -- format check, then the version rule.
# ---------------------------------------------------------------------------
call_no=0
applied_here=()
apply_body() { # <body file>
  local body=$1
  local cdir="$tmpdir/c$((call_no += 1))"
  mkdir -p "$cdir"
  applied_here=()


# The format check runs first: it validates every diff against git's own patch
# parser and rejects paths a request may not touch.
form check -f "$body" || die "the body does not pass the format check; nothing applied"

form parse -f "$body" > "$cdir/parsed.json"
state=$(jq -r .diff_state "$cdir/parsed.json")
if [ "$state" != ready ]; then
  printf 'diff_state=%s: nothing to apply (a pending request only gates versions)\n' "$state"
  return 0
fi

resolved=$(form resolve -f "$body") || die "cannot resolve the packages (see the message above)"
[ -n "$resolved" ] || die "the request resolves to no package"
mapfile -t covered <<< "$resolved"

targets=("${covered[@]}")
if [ "${#only_pkgs[@]}" -gt 0 ]; then
  targets=()
  for want in "${only_pkgs[@]}"; do
    found=0
    for pkg in "${covered[@]}"; do
      [ "$pkg" = "$want" ] && found=1
    done
    [ "$found" -eq 1 ] || die "--pkg $want is not covered by this request (covered: ${covered[*]})"
    targets+=("$want")
  done
fi

next_update=$(jq -r .next_update "$cdir/parsed.json")
if [ "$next_update" = true ]; then
  printf 'note: next_update is ticked — the update run applies this only from the next version on\n'
fi

# The version rule: a request is anchored to a base version, so a package the
# request is not (yet or any more) about is skipped rather than patched.  The
# target comes from --versions (the version the run moves the package to);
# without it the package stays where it is.
declare -A run_tgt=()
if [ -n "$versions" ]; then
  while read -r p _c t; do
    [ -n "$p" ] || continue
    run_tgt["$p"]=$t
  done < "$versions"
fi

target_version=$(jq -r .target_version "$cdir/parsed.json")
eligible=()
for pkg in "${targets[@]}"; do
  sub="$root/aur/$pkg"
  [ -d "$sub" ] || die "no such package directory: aur/$pkg"
  cur=$(sed -n 's/^pkgver=//p' "$sub/PKGBUILD")
  [ -n "$cur" ] || die "aur/$pkg: cannot read pkgver from PKGBUILD"
  tgt=${run_tgt[$pkg]:-$cur}
  why=""
  if [ "$(vercmp "$(base_of "$cur")" "$target_version")" -gt 0 ]; then
    why="aur/$pkg publishes v$cur, newer than the request's v$target_version (no backport)"
  elif [ "$(vercmp "$(base_of "$tgt")" "$target_version")" -lt 0 ]; then
    why="aur/$pkg has not reached v$target_version (v$cur -> v$tgt)"
  elif [ "$tgt" = "$cur" ] && [ "$next_update" = true ]; then
    why="next_update is ticked and v$cur is already published"
  fi
  if [ -n "$why" ]; then
    if [ "$force" -eq 1 ]; then
      printf 'apply-mod-request: %s; applying anyway (--force)\n' "$why"
    else
      printf 'apply-mod-request: skipping %s: %s; pass --force to apply anyway\n' "$pkg" "$why"
      continue
    fi
  fi
  eligible+=("$pkg")
done

if [ "${#eligible[@]}" -eq 0 ]; then
  printf 'nothing applied: no covered package may take this request at v%s\n' "$target_version"
  return 0
fi
targets=("${eligible[@]}")

# Pass 1: check every target before touching any of them.  A dirty sibling or a
# patch that only applies to one of two packages must not leave the tree half
# changed -- the printed undo would then describe work that never happened.
declare -a rels=() bumps=() files_of=()
for pkg in "${targets[@]}"; do
  sub="$root/aur/$pkg"
  # Starting from a clean tree is what makes the printed undo command exact.
  [ -z "$(git -C "$sub" status --porcelain)" ] ||
    die "aur/$pkg has local changes; commit or restore them first (this step has to be revertible)"

  diff="$cdir/$pkg.diff"
  jq -j --arg p "$pkg" '.payload[$p].diff' "$cdir/parsed.json" > "$diff"
  [ -s "$diff" ] || die "aur/$pkg: no diff in the payload"
  # parse() strips the block's trailing newline; without one git apply reads the
  # patch as truncated (the same reason the format check appends it).
  printf '\n' >> "$diff"
  msg="$cdir/$pkg.msg"
  jq -j --arg p "$pkg" '.payload[$p].message' "$cdir/parsed.json" > "$msg"
  [ -s "$msg" ] || die "aur/$pkg: no commit message in the payload"
  # jq -j leaves the message without a final newline; the report would read it
  # that way too, and a commit message wants one.
  printf '\n' >> "$msg"

  bump="pkgrel unchanged"
  rel=""
  if [ "$no_pkgrel" -eq 0 ]; then
    rel=$(sed -n 's/^pkgrel=//p' "$sub/PKGBUILD")
    case "$rel" in
      '' | *[!0-9]*)
        die "aur/$pkg: cannot bump pkgrel (PKGBUILD says ${rel:-nothing}); --no-pkgrel leaves it alone"
        ;;
    esac
    bump="pkgrel $rel -> $((rel + 1))"
  fi

  if ! out=$(git -C "$sub" apply --check "$diff" 2>&1); then
    die "aur/$pkg: git apply --check failed: $out"
  fi
  # The real apply below reports whitespace quirks; listing the files should not.
  files=$(git -C "$sub" apply --whitespace=nowarn --numstat "$diff" | cut -f3- | tr '\n' ' ')

  rels+=("$rel")
  bumps+=("$bump")
  files_of+=("${files% }")
done

# Pass 2: apply. Only failures git could not predict in `--check` (permissions, a
# file changing underneath) can land here.
for i in "${!targets[@]}"; do
  pkg=${targets[i]}
  sub="$root/aur/$pkg"
  if [ "$dry_run" -eq 0 ]; then
    git -C "$sub" apply "$cdir/$pkg.diff"
    applied+=("$pkg")
    if [ "$no_pkgrel" -eq 0 ]; then
      sed -i "s/^pkgrel=.*/pkgrel=$((rels[i] + 1))/" "$sub/PKGBUILD"
    fi
    bump=${bumps[i]}
  else
    bump="${bumps[i]} (dry run, nothing changed)"
  fi
  applied_here+=("$pkg")

  printf 'aur/%s: %s\n' "$pkg" "$bump"
  printf '  files:   %s\n' "${files_of[i]}"
  printf '  message:\n'
  sed 's/^/    /' "$cdir/$pkg.msg"
done

[ "$dry_run" -eq 0 ] || return 0
[ "$live_mode" -eq 0 ] || return 0

printf '\nnext: in each directory above run updpkgsums, then makepkg --printsrcinfo > .SRCINFO,\n      then build with makepkg\n'
printf 'undo: git -C <dir> restore . && git -C <dir> clean -fd  (clean removes files the patch added)\n'
}

# ---------------------------------------------------------------------------
# Applying every live request.
#
# A live request is open, labelled `approved`, and carries an approval pin whose
# sha256 is the body as it is now.  That rule has one implementation
# (`mod-request.py pin`), the gate another (`gate`), and "may this package take
# the request" is `apply_body` above: this pass only decides which package goes
# to which live request and hands them over.
#
# It fails closed: two live requests for one version, a body the gate refuses, a
# signed record that cannot be read, a request covering a package the version
# vector does not describe -- the run stops instead of choosing.
#
# The decisions go to stdout, machine-readable, because the caller writes the
# commits and the signed record:
#   #mr-body <issue> <sha256>   the body that was gated: the caller re-reads the
#                               body and checks this hash before signing
#   #mr-held <pkg> <issue>      the package must not move (request is pending)
#   #mr-applied <pkg> <issue>   the payload is in aur/<pkg>'s working tree
# ---------------------------------------------------------------------------
apply_live() {
  local dir="$tmpdir/live"
  local repo_args=()
  [ -z "$repo" ] || repo_args=(-R "$repo")
  mkdir -p "$dir/bodies" "$dir/issues"

  # `approved` narrows the pool to what can possibly be live, so the fetch cap
  # below is far above what this repo can hold; the body decides whether an
  # issue is a request at all (non-parsing bodies are filtered out below), and
  # the per-issue label check stays because the view is the fresher call.
  #
  # `gh issue list` truncates at its limit without saying so, and a truncated
  # list reads as "no request covers this version".  The same number is
  # therefore both the ask and the refusal threshold.
  local cap=100
  gh issue list "${repo_args[@]}" --state open --label approved --limit "$cap" \
    --json number > "$dir/open.json" ||
    die "cannot list the open approved issues${repo:+ in $repo}"
  local open_count
  open_count=$(jq 'length' "$dir/open.json")
  [ "$open_count" -lt "$cap" ] ||
    die "$cap open approved issues (the fetch cap); close the stale ones and re-run"
  if [ "$open_count" -eq 0 ]; then
    note "no open approved issues; nothing to apply"
    return 0
  fi

  # --- who is live --------------------------------------------------------
  local num approved pin body_hash
  local -a numbers=() live=()
  : > "$dir/live.ndjson"
  mapfile -t numbers < <(jq -r '.[].number' "$dir/open.json")
  for num in "${numbers[@]}"; do
    gh issue view "$num" "${repo_args[@]}" \
      --json number,body,state,labels,comments > "$dir/issues/$num.json" ||
      die "cannot read issue #$num"
    jq -r '.body // ""' "$dir/issues/$num.json" > "$dir/bodies/$num.md"

    approved=$(jq -r '(.labels // [])
      | map(if type == "object" then .name else . end)
      | index("approved") != null' "$dir/issues/$num.json")
    [ "$approved" = "true" ] || continue
    [ -s "$dir/bodies/$num.md" ] || {
      warn "#$num is approved but its body is empty; not live"
      continue
    }

    jq '.comments // []' "$dir/issues/$num.json" > "$dir/comments-$num.json"
    if ! pin=$(form pin --comments "$dir/comments-$num.json" 2> /dev/null); then
      warn "#$num is approved but carries no approval pin from a maintainer; not live"
      continue
    fi
    if ! body_hash=$(form hash -f "$dir/bodies/$num.md" 2> /dev/null); then
      warn "#$num does not parse as a mod request; not live"
      continue
    fi
    if [ "$pin" != "$body_hash" ]; then
      warn "#$num was edited after approval (body ${body_hash:0:12}…, pin ${pin:0:12}…); not live until it is approved again"
      continue
    fi

    printf '%s\n' "$body_hash" > "$dir/hash-$num.txt"
    jq -nc --argjson n "$num" --rawfile b "$dir/bodies/$num.md" \
      '{number: $n, body: $b}' >> "$dir/live.ndjson"
    live+=("$num")
  done
  jq -s . "$dir/live.ndjson" > "$dir/live.json"

  if [ "${#live[@]}" -eq 0 ]; then
    note "no live mod requests ($open_count open approved); nothing to apply"
    return 0
  fi

  # --- gate every live request -------------------------------------------
  # The same evaluator the approval path runs: a body that was legal to approve
  # is legal to apply, and a `supersedes` claim is checked rather than believed.
  # CI never closes anything -- closing belongs to the approval that accepts a
  # replacement, which is why --no-close drops the hint.
  local body covered declared old verdict gate_rc gate_args
  for num in "${live[@]}"; do
    body="$dir/bodies/$num.md"
    form parse -f "$body" > "$dir/parsed-$num.json" || die "#$num: the body does not parse"
    covered=$(form resolve -f "$body") || die "#$num: cannot resolve the covered packages"
    printf '%s\n' "$covered" > "$dir/covered-$num.txt"

    # The record is per package: what earlier runs applied is read back so the
    # twin still waits for its own commit when it reaches the version.
    form applied --issue "$num" --body "$body" \
      --comments "$dir/comments-$num.json" > "$dir/applied-$num.txt" ||
      die "#$num: cannot read the applied records (ssh-keygen, and bettbox-aur-updater.pub in the tree?)"

    gate_args=(--issue "$num" --body "$body" \
      --open-approved "$dir/live.json" --no-close)
    declared=$(jq -r '.supersedes // ""' "$dir/parsed-$num.json")
    if [ -n "$declared" ]; then
      old=${declared#\#}
      [ "$old" != "$num" ] || die "#$num declares supersedes: #$num (itself)"
      if [ ! -f "$dir/issues/$old.json" ]; then
        gh issue view "$old" "${repo_args[@]}" --json number,body,state,comments \
          > "$dir/issues/$old.json" ||
          die "#$num: cannot read issue #$old, the one it says it supersedes"
      fi
      gate_args+=(--old "$dir/issues/$old.json")
    fi

    set +e
    verdict=$(form gate "${gate_args[@]}")
    gate_rc=$?
    set -e
    case "$gate_rc" in
      0 | 1) ;;
      *) die "#$num: the gate could not decide (exit $gate_rc)" ;;
    esac
    if [ "$gate_rc" -ne 0 ]; then
      {
        printf 'apply-mod-request: the gate refuses the live request #%s:\n' "$num"
        jq -r '.reasons[]' <<< "$verdict" | sed 's/^/  /'
        printf 'nothing was applied.\n'
      } >&2
      exit 1
    fi
  done

  # --- which request takes which package ----------------------------------
  # The approval gate keeps one live request per package set (Rule 4), so a
  # package maps to at most one live request and the map below simply trusts
  # that.  Two live requests for the same version are fine as long as their
  # selections are disjoint: they never meet in one tree, and each package
  # still carries one diff.
  local pkg cur tgt cur_base tgt_base moving r_ver r_state r_next num
  local -A known=() req_of=() hold_of=() apply_of=() pkgs_of=()
  while read -r pkg _cur _tgt; do
    [ -n "$pkg" ] || continue
    known["$pkg"]=1
  done < "$versions"
  for num in "${live[@]}"; do
    while IFS= read -r pkg; do
      [ -n "$pkg" ] || continue
      [ -n "${known[$pkg]:-}" ] ||
        die "a request covers $pkg, which is not in $versions (stale package list)"
      req_of[$pkg]=$num
    done < "$dir/covered-$num.txt"
  done

  while read -r pkg cur tgt; do
    [ -n "$pkg" ] || continue
    num=${req_of[$pkg]:-}
    [ -n "$num" ] || continue
    cur_base=$(base_of "$cur")
    tgt_base=$(base_of "$tgt")
    moving=false
    [ "$tgt" != "$cur" ] && moving=true

    r_ver=$(jq -r '.target_version' "$dir/parsed-$num.json")
    r_state=$(jq -r '.diff_state' "$dir/parsed-$num.json")
    r_next=$(jq -r '.next_update' "$dir/parsed-$num.json")

    if grep -qxF -- "$pkg" "$dir/applied-$num.txt"; then
      continue
    fi

    # The request is older than what the package publishes.  Applying it would
    # rewrite a released version, and keeping it as a gate would hold the
    # package forever for a version it can never visit again.
    if [ "$(vercmp "$cur_base" "$r_ver")" -gt 0 ]; then
      warn "#$num targets v$r_ver but $pkg already publishes v$cur: not applied (no backport), and it no longer gates $pkg -- close it or re-anchor it"
      continue
    fi
    # Not due yet: the run does not reach the request's version.
    if [ "$(vercmp "$tgt_base" "$r_ver")" -lt 0 ]; then
      note "#$num: $pkg is not due yet (target v$tgt < v$r_ver)"
      continue
    fi

    if $moving; then
      if [ "$r_state" = "ready" ]; then
        apply_of["$pkg"]=$num
      else
        # The version must not ship without the change the pending request
        # stands for.
        hold_of["$pkg"]=$num
        warn "$pkg is held at v$cur: mod request #$num targets a version it is not written as ready for (write it or close it)"
      fi
    elif [ "$r_state" = "ready" ] && [ "$r_next" != "true" ]; then
      apply_of["$pkg"]=$num
    elif [ "$r_state" = "ready" ]; then
      note "#$num: $pkg already publishes v$cur and next_update is ticked, so this version is left alone"
    fi
  done < "$versions"

  # --- report the decisions, then hand each request its packages ----------
  while read -r pkg _cur _tgt; do
    [ -n "$pkg" ] || continue
    if [ -n "${hold_of[$pkg]:-}" ]; then
      printf '#mr-held %s %s\n' "$pkg" "${hold_of[$pkg]}"
    fi
    if [ -n "${apply_of[$pkg]:-}" ]; then
      pkgs_of["${apply_of[$pkg]}"]+=" $pkg"
    fi
  done < "$versions"

  local list
  for num in "${live[@]}"; do
    [ -n "${pkgs_of[$num]:-}" ] || continue
    printf '#mr-body %s %s\n' "$num" "$(cat "$dir/hash-$num.txt")"
    # shellcheck disable=SC2206  # the list is package names, no spaces in them
    only_pkgs=(${pkgs_of[$num]})
    apply_body "$dir/bodies/$num.md"
    for pkg in "${applied_here[@]}"; do
      printf '#mr-applied %s %s\n' "$pkg" "$num"
    done
    only_pkgs=()
  done

  list=$(printf '#%s, ' "${live[@]}")
  note "${#live[@]} live request(s): ${list%, }"
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
tmpdir=$(mktemp -d "${MR_TMPDIR:-${TMPDIR:-/tmp}}/mr-apply.XXXXXX")
# A failure part-way through leaves some packages patched and others not.  That
# is not silently rolled back (the point of a rehearsal is to look at it), but it
# is announced: the undo command is exact only for a clean start.
applied=()
cleanup() {
  status=$?
  if [ "$status" -ne 0 ] && [ "${#applied[@]}" -gt 0 ]; then
    printf '\napplied before the failure (left dirty on purpose):\n' >&2
    printf '  aur/%s\n' "${applied[@]}" >&2
    printf 'undo: git -C aur/<pkg> restore . && git -C aur/<pkg> clean -fd\n' >&2
  fi
  rm -rf "$tmpdir"
}
trap cleanup EXIT

if [ "$live_mode" -eq 1 ]; then
  apply_live
else
  body="$tmpdir/body.md"
  if [ -n "$body_file" ]; then
    if [ "$body_file" = - ]; then
      cat > "$body"
    else
      [ -f "$body_file" ] || die "no such body file: $body_file"
      cp "$body_file" "$body"
    fi
  else
    case "$issue" in
      *[!0-9]*) die "issue number must be numeric, got: $issue" ;;
    esac
    gh issue view "$issue" --json body -q '.body // ""' > "$body" ||
      die "cannot read issue #$issue"
  fi
  [ -s "$body" ] || die "the body is empty"
  apply_body "$body"
fi
