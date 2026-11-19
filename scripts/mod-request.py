#!/usr/bin/env python3
"""mod-request.py -- the only implementation of the mod-request issue format.

The issue body is the single source of truth.  It lives in `scripts/` (next to
the approval helper) rather than inside the skill because three consumers share
it: the skill renders bodies with it, `approve-mod-request.sh` validates and
hashes them with it, and CI parses them with it.  Do not re-implement the format
anywhere else -- a second implementation is a silent drift bug.

Body layout (top-level headings are the Issue Form field labels, in form order):

    ### supersedes          issue this request replaces, if any (#12)
    ### summary
    ### target_version
    ### next_update          checkboxes, lone option: ticked = hold it back
    ### type                 dropdown, multi-select: source | bin
    ### variant              dropdown, multi-select: common | compatible
    ### edition              dropdown, multi-select: standard | lite
    ### diff_state           pending | ready
    ### evidence
    ### payload: <pkgname>   one section per package, only when diff_state=ready

The package set is *derived*, never listed: the three selections are matched
against the packages that exist in `aur/*`, and the channel is not selectable --
one request covers the stable and the pre build of a combination alike, each
applying when its own version reaches `target_version`.  Deriving rather than
storing the package list keeps the body (and therefore its hash) independent of
what the repository happens to contain on the day it is parsed.

Each `payload: <pkgname>` section carries exactly two fenced blocks, in order:

    ````diff
    <git -C aur/<pkg> diff, package-relative paths>
    ````

    ````text
    <the message for that package's commit>
    ````

`next_update` answers the one timing question `target_version` cannot: when
`target_version` equals the base semver a package already sits on -- routine in
the pre channel, where `1.20.0pre1`, `pre2`, `pre3` share one base -- a ticked
box means "do not touch the version that is out now; wait for the next one".
CI reads it as: this request never applies in a run that does not move that
package's version.

The hash used for approvals is not the hash of the markdown: `hash` parses the
body first and then sha256s a canonical projection of the fields CI consumes
(`BOUND_FIELDS`: the selections, `target_version`, `next_update`, `diff_state`,
`supersedes`, and each payload's diff + message).  `summary` and `evidence` are
outside it on purpose -- they are the reviewer's context, and editing them must
not silently invalidate a pin that still describes the same work.

`supersedes` is the only relationship a body can declare: the issue it
replaces, spelled `#12` (one canonical spelling; `12` is accepted on the way in
and normalised, so the hash does not depend on typing).  Replacing (rather than
editing) is what the flow prescribes once a request has been applied to some
packages.  It is a claim, not a permission: it takes effect only after `gate`
(see below) has accepted it against issue data, and CI re-runs that check when
it applies the body instead of trusting what was true at approval time.

`gate` is the single implementation of "may this request take effect".  It reads
issue JSON fetched by the caller (`{number, body}` for the requests that already
hold a version, plus `{number, body, state, comments}` for the issue
`supersedes` names), so it never calls `gh` and works the same offline, in the
approval helper, and in CI.  It answers with JSON:

    {"ok": true, "issue": 13, "target_version": "1.20.0",
     "supersedes": "#12", "close": 12, "reasons": []}

`close` is the issue to close (null when it is already closed, absent, or the
verdict is a refusal).  A wrong claim -- another live request shares a package
with this one (one live request per package set, whatever the version or
diff state), the declared issue is a different version,
would be stranded of packages, carries no signed applied record, is
unreadable, or is the request itself -- comes back as `ok: false` with one line
per reason.

How that message is spent is CI's call, per package: a package already at
`target_version` gets it as the whole commit message, and a package this run is
bringing to `target_version` gets it as the body of the `Update to v<ver>`
commit.  One request can hold both cases at once -- pre and stable reach the same
base version at different times -- so the format keeps a single message per
package and does not split it into a title and a body.

A diff may touch any file of that AUR package except `.SRCINFO` and
`.nvchecker.toml` (CI owns them), and must not create symlinks or submodules.

Empty payload sections (the Issue Form writes `_No response_` for a field left
blank) are ignored, so a body rendered from the form and a body rendered by
`render` parse to the same structure.

Four-backtick fences are mandatory.  A diff may itself contain three-backtick
fences; a fence longer than any run the payload may contain keeps the parse
unambiguous.  Any line starting with four or more backticks is rejected by
`check`, so the round trip is always well defined.

Two external tools do parsing this file must not reinvent: `git apply --numstat`
lists the paths a diff touches, and `yq` parses the Issue Form for `check-form`.

Exit codes: 0 ok, 1 error or a refused `gate` verdict, 2 could not decide
(unreadable gate input; also `supersede`'s "yes, but strands packages").
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
import tempfile
import textwrap
from datetime import datetime, timezone
from pathlib import Path

# The one thing a request may say about *when* it applies beyond target_version:
# "not to the version that is already out".  GitHub's checkboxes control is a
# multi-select, so a lone option is how a form spells a boolean.
NEXT_UPDATE_OPTION = "不要应用到当前版本，等该包下一次升级时再启用"

SCALAR_FIELDS = [
    # The one declared relationship between requests.  A request for a version
    # that is already partly applied replaces the old issue rather than editing
    # it, and `supersedes` is where that is said out loud.  It is a claim, not a
    # permission: the approval gate and CI check it with `supersede`.
    {"id": "supersedes", "label": "supersedes", "type": "input"},
    {"id": "summary", "label": "summary", "type": "input"},
    {"id": "target_version", "label": "target_version", "type": "input"},
    {"id": "next_update", "label": "next_update", "type": "checkboxes",
     "options": [NEXT_UPDATE_OPTION]},
    {"id": "type", "label": "type", "type": "dropdown", "multiple": True,
     "options": ["source", "bin"]},
    {"id": "variant", "label": "variant", "type": "dropdown", "multiple": True,
     "options": ["common", "compatible"]},
    {"id": "edition", "label": "edition", "type": "dropdown", "multiple": True,
     "options": ["standard", "lite"]},
    {"id": "diff_state", "label": "diff_state", "type": "dropdown",
     "options": ["pending", "ready"]},
    {"id": "evidence", "label": "evidence", "type": "textarea"},
]
SCALAR_LABELS = {f["label"] for f in SCALAR_FIELDS}

# The package selection is three independent multi-select dimensions, not a list
# of package names.  Order matters: it is the order of the fields in the form.
DIMENSIONS = {f["id"]: f["options"] for f in SCALAR_FIELDS if f.get("multiple")}

# Every package in this repo is the cartesian product of the dimensions plus the
# channel: bettbox[-lite][-compatible][-pre][-bin].  The channel stays implicit
# (a request covers both), so it is parsed here but never offered in the form.
PKG_NAME = re.compile(
    r"^bettbox"
    r"(?:-(?P<edition>lite))?"
    r"(?:-(?P<variant>compatible))?"
    r"(?:-(?P<channel>pre))?"
    r"(?:-(?P<type>bin))?$"
)
DIM_DEFAULTS = {"type": "source", "variant": "common", "edition": "standard"}

PAYLOAD_PREFIX = "payload: "


def package_order(pkg: str) -> tuple[int, str]:
    """Canonical order for every package listing: standard block, then lite.

    The edition groups the form and any rendered body, so the two families stay
    readable apart; names that do not fit the grammar sort with the standard
    block rather than dying here, leaving that verdict to `package_dims`.
    """
    m = PKG_NAME.match(pkg)
    return (1 if m and m.group("edition") == "lite" else 0, pkg)


def payload_label(pkg: str) -> str:
    return PAYLOAD_PREFIX + pkg


def payload_id(pkg: str) -> str:
    return "payload-" + pkg


FENCE = re.compile(r"^(`{3,})(.*)$")
FENCE_CLOSE = re.compile(r"^(`{3,})\s*$")
H3 = re.compile(r"^###\s+(.*\S)\s*$")
BULLET = re.compile(r"^\s*-\s*(?!\[)(.+?)\s*$")
CHECKBOX = re.compile(r"^\s*-\s*\[([ xX])\]\s*(.+?)\s*$")
TARGET_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
# What a package actually publishes after an application: pkgver-pkgrel, e.g.
# "1.20.0pre1-2".  The base version is in the record's summary line; this is the
# exact build the change landed in.
PKG_FULL = re.compile(r"^\d[0-9A-Za-z._+]*-\d+$")
PKGVER_LINE = re.compile(r"^[+-](pkgver|pkgrel)=")
LONG_FENCE = re.compile(r"^`{4,}", re.M)
SECTION_LINE = re.compile(r"^###\s", re.M)
# A binary change is spelled with a "GIT binary patch" block (or, for a patch
# generated with --no-textconv, "Binary files ... differ").  Both are rejected
# before `git apply --numstat` runs: a binary patch carries no readable paths,
# and its base85 block is whitespace sensitive, so it must not go through the
# text pipeline at all.
BINARY_MARKER = re.compile(r"^(?:GIT binary patch|Binary files .* differ)$", re.M)
NO_RESPONSE = "_No response_"
# GitHub refuses an issue body longer than this.  A payload-carrying request is
# mostly diff text, so hitting it is a real possibility rather than a curiosity.
GITHUB_BODY_LIMIT = 65536

 # Paths inside one AUR package directory.  A payload diff is applied with
# `git -C aur/<pkg> apply`, so every path it mentions is package-relative.
# The file list is not parsed here: `git apply --numstat` is the authority (see
# git_numstat), which keeps this check identical to what the apply step sees --
# quoting, spaces and renames included.  It is still not a path *policy*: git
# --numstat happily reports `.git/...` and `../...`, so path_problem decides.
OBJ_MODE = re.compile(r"^(?:new file mode|new mode|old mode) (\d{6})$", re.M)

# Files the request must not touch: CI generates .SRCINFO, and .nvchecker.toml
# drives the version source of truth (see AGENTS.md, "Version model").
DENY_FILES = {".SRCINFO", ".nvchecker.toml"}
# Object kinds a packaging change never needs, and which git apply happily
# creates: symlinks can point outside the repo, gitlinks pull in a commit.
DENY_MODES = {"120000": "symlink", "160000": "submodule"}


class Error(Exception):
    pass


def die(msg: str) -> "None":
    raise Error(msg)


def require_tool(name: str, hint: str = "") -> None:
    """A delegated tool has to exist before it is used.

    Failing here names the absence; without it the caller sees a side effect
    instead (no `git` reads as an unparseable diff, no `yq` as a broken form).
    """
    if shutil.which(name) is None:
        die(f"missing required tool: {name}{hint}")


# --- repository discovery -------------------------------------------------


def find_root(start: str | None = None) -> Path:
    p = Path(start or __file__).resolve()
    for d in [p, *p.parents]:
        if (d / "aur").is_dir() or (d / ".gitmodules").is_file():
            return d
    return Path.cwd()


def discover_packages(root: Path) -> list[str]:
    names = []
    for pkgbuild in sorted((root / "aur").glob("*/PKGBUILD")):
        m = re.search(r"^pkgname=(.+)$", pkgbuild.read_text(encoding="utf-8"), re.M)
        if m:
            names.append(m.group(1).strip())
    if not names:
        die(f"no packages found under {root / 'aur'}")
    return sorted(names, key=package_order)


def package_dims(pkg: str) -> dict[str, str]:
    """The (type, variant, edition) a package name encodes; channel is extra."""
    m = PKG_NAME.match(pkg)
    if not m:
        die(
            f"package '{pkg}' does not fit the name grammar "
            "bettbox[-lite][-compatible][-pre][-bin]; extend PKG_NAME before "
            "using mod requests on it"
        )
    dims = {k: (m.group(k) or v) for k, v in DIM_DEFAULTS.items()}
    dims["channel"] = m.group("channel") or "stable"
    return dims


def resolve_pkgs(selection: dict, packages: list[str]) -> list[str]:
    """Packages matching a selection; a dimension left unset matches nothing."""
    chosen = {k: set(selection.get(k) or []) for k in DIMENSIONS}
    if not all(chosen.values()):
        return []
    return sorted(
        (pkg for pkg in packages
         if all(package_dims(pkg)[dim] in chosen[dim] for dim in DIMENSIONS)),
        key=package_order,
    )


# --- fence-aware scanning -------------------------------------------------


def scan_sections(lines: list[str]) -> list[tuple[str, list[str]]]:
    """Split on top-level '### heading' lines, ignoring headings inside fences."""
    out: list[tuple[str, list[str]]] = []
    label: str | None = None
    buf: list[str] = []
    fence = 0
    for line in lines:
        if fence:
            if FENCE_CLOSE.match(line) and len(FENCE_CLOSE.match(line).group(1)) >= fence:
                fence = 0
            if label is not None:
                buf.append(line)
            continue
        m = FENCE.match(line)
        if m:
            fence = len(m.group(1))
            if label is not None:
                buf.append(line)
            continue
        h = H3.match(line)
        if h:
            if label is not None:
                out.append((label, buf))
            label = h.group(1).strip()
            buf = []
            continue
        if label is not None:
            buf.append(line)
    if label is not None:
        out.append((label, buf))
    return out


def extract_blocks(lines: list[str]) -> list[tuple[str, str]]:
    """Return (info-string, body) for every top-level fenced block."""
    blocks: list[tuple[str, str]] = []
    i = 0
    while i < len(lines):
        m = FENCE.match(lines[i])
        if not m:
            i += 1
            continue
        width = len(m.group(1))
        info = m.group(2).strip()
        j = i + 1
        while j < len(lines):
            c = FENCE_CLOSE.match(lines[j])
            if c and len(c.group(1)) >= width:
                break
            j += 1
        if j >= len(lines):
            die(f"unterminated fenced block opened with: {lines[i]!r}")
        blocks.append((info, "\n".join(lines[i + 1:j])))
        i = j + 1
    return blocks


# --- parse ----------------------------------------------------------------


def dim_values(lines: list[str]) -> list[str]:
    """The selected options of one multi-select dropdown.

    GitHub writes a multi-select as a bullet list:

        ### type

        - source
        - bin

    A single selection may arrive as a bare line instead, so both shapes are
    accepted; anything else still fails `check` as an unknown value.
    """
    values = [m.group(1) for m in map(BULLET.match, lines) if m]
    if not values:
        text = "\n".join(lines).strip()
        if text and text != NO_RESPONSE:
            values = [v.strip() for v in text.split(",")]
    return sorted({v for v in values if v})


def checkbox_value(lines: list[str]) -> bool:
    """A lone-option checkboxes field: ticked or not.

    GitHub writes the state of every box, ticked or not, as "- [X] text" /
    "- [ ] text", so the mark is what carries the value.
    """
    return any(
        m and m.group(1).lower() == "x" for m in map(CHECKBOX.match, lines)
    )


ISSUE_REF = re.compile(r"^#?([1-9][0-9]{0,8})$")


def issue_ref(text: str) -> str | None:
    """`#12` / `12` / blank -> `#12` / `#12` / None.

    One spelling is kept (`#12`) so the body reads the way GitHub links it and
    the hash does not depend on how the requester typed the number.  A
    non-number is handed back as typed: turning it into None would approve a
    request whose reference silently vanished, and raising here would make
    `parse` the place a typo is reported.  `validate` rejects it.
    """
    ref = text.strip()
    if not ref or ref == NO_RESPONSE:
        return None
    m = ISSUE_REF.match(ref)
    return f"#{m.group(1)}" if m else ref


def parse(text: str, packages: list[str] | None = None) -> dict:
    seen: dict[str, list[str]] = {}
    payload_lines: dict[str, list[str]] = {}
    for label, lines in scan_sections(text.split("\n")):
        if label in SCALAR_LABELS:
            if label in seen:
                die(f"duplicate section '### {label}'")
            seen[label] = lines
            continue
        if label.startswith(PAYLOAD_PREFIX):
            pkg = label[len(PAYLOAD_PREFIX):].strip()
            if not pkg:
                die("payload section without a package name: '### payload: '")
            if packages is not None and pkg not in packages:
                die(f"payload section for unknown package '{pkg}' (known: {', '.join(packages)})")
            if pkg in payload_lines:
                die(f"duplicate payload section for '{pkg}'")
            payload_lines[pkg] = lines
            continue
        die(f"unknown section '### {label}' (not a mod-request field)")

    for f in SCALAR_FIELDS:
        if f["label"] not in seen:
            die(f"missing section '### {f['label']}'")

    data: dict = {}

    def scalar(label: str) -> str:
        # GitHub renders an empty optional field as "_No response_"; treat it as
        # empty so a blank textarea does not look like content.
        value = "\n".join(seen[label]).strip()
        return "" if value == NO_RESPONSE else value

    for f in SCALAR_FIELDS:
        if f["id"] in DIMENSIONS:
            data[f["id"]] = dim_values(seen[f["label"]])
        elif f["type"] == "checkboxes":
            data[f["id"]] = checkbox_value(seen[f["label"]])
        elif f["id"] == "supersedes":
            data[f["id"]] = issue_ref(scalar(f["label"]))
        else:
            data[f["id"]] = scalar(f["label"])

    # Payload sections are read whatever diff_state says: a diff left behind
    # under diff_state=pending is a real footgun (it is written but nothing will
    # ever be applied), so it has to surface as a validation error.
    payload: dict[str, dict] = {}
    for pkg, lines in payload_lines.items():
        if "\n".join(lines).strip() in ("", NO_RESPONSE):
            continue
        blocks = extract_blocks(lines)
        if len(blocks) != 2:
            die(f"package '{pkg}': expected 2 fenced blocks (diff, text), got {len(blocks)}")
        (i1, diff), (i2, message) = blocks
        if i1 != "diff" or i2 != "text":
            die(f"package '{pkg}': blocks must be ```diff then ```text, got {i1!r} then {i2!r}")
        payload[pkg] = {
            "diff": diff.strip("\n"),
            "message": message.strip("\n"),
        }
    data["payload"] = payload
    return data


# --- canonical form and hashing -------------------------------------------
#
# The hash is over parsed data, not over markdown: whitespace, section order and
# how a reference was typed cannot change it.  It is then narrowed to the fields
# that actually do something -- an approval binds what CI reads and what lands in
# AUR, not the prose around it.  `summary` and `evidence` are for the reader; no
# consumer writes them anywhere, so fixing a typo there should not cost a new
# approval.  Everything listed below either instructs CI (`supersedes`,
# `target_version`, `next_update`, the three selections, `diff_state`) or is the
# content it applies (`payload`: the diff and the commit message per package).
#
# `supersedes` is bound even though it is a claim: accepting or refusing it is a
# decision about this body, so it has to be part of what was approved.  The
# selections are bound instead of the packages they resolve to, deliberately --
# a body must not change meaning (or hash) because the repository gained a
# package since it was filed; `check` then fails on the missing payload instead.
BOUND_FIELDS = (
    "supersedes",
    "target_version",
    "next_update",
    "type",
    "variant",
    "edition",
    "diff_state",
    "payload",
)
BOUND_DEFAULTS = {"next_update": False, "payload": {}}


def bound(data: dict) -> dict:
    return {k: data.get(k, BOUND_DEFAULTS.get(k)) for k in BOUND_FIELDS}


def canonical(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(obj: dict) -> str:
    return hashlib.sha256(canonical(bound(obj)).encode("utf-8")).hexdigest()


# --- diff inspection ------------------------------------------------------


def git_numstat(diff: str) -> tuple[list[tuple[str, bool]], str | None]:
    """File list of `diff`, straight from git's patch parser.

    `git apply --numstat -z` prints `added<TAB>deleted<TAB>path<NUL>` per file,
    with `-` in place of the numbers for a binary patch.  Using git here instead
    of a regex keeps this check identical to the apply step: C-quoted names,
    spaces, renames and binary hunks are all handled the same way.
    """
    require_tool("git")  # without it every diff would read as unparseable
    try:
        proc = subprocess.run(
            ["git", "apply", "--numstat", "-z", "-"],
            # git apply treats a patch whose last line has no newline as
            # truncated ("corrupt patch at line N"), and the payload stored in
            # the issue body never keeps a trailing newline.
            input=(diff if diff.endswith("\n") else diff + "\n").encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # Keep git's diagnostics in one language so CI logs and the message
            # below do not depend on the runner's locale.
            env={**os.environ, "LC_ALL": "C", "LANGUAGE": "C"},
        )
    except OSError as exc:
        return [], f"cannot run git: {exc}"
    if proc.returncode != 0:
        lines = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        first = lines[0] if lines else "unknown error"
        return [], f"git apply cannot parse this diff: {first}"

    entries: list[tuple[str, bool]] = []
    for field in proc.stdout.split(b"\0"):
        if not field:
            continue
        parts = field.split(b"\t", 2)
        if len(parts) != 3:
            continue
        added, _deleted, path = parts
        entries.append((path.decode("utf-8", "replace"), added == b"-"))
    return entries, None


def path_problem(path: str) -> str | None:
    """Why `path` may not be changed by a mod request, or None when it may."""
    if path == "/dev/null":
        return None
    if path.startswith("/"):
        return f"absolute path {path!r}"
    parts = path.split("/")
    if ".." in parts:
        return f"path {path!r} escapes the package directory"
    if ".git" in parts:
        return f"path {path!r} touches git plumbing"
    if path in DENY_FILES:
        return f"{path!r} is owned by CI (never edit it in a request)"
    return None


def diff_errors(pkg: str, diff: str) -> list[str]:
    errs: list[str] = []
    if not diff.strip():
        return [f"{pkg}: empty diff"]

    for line in diff.split("\n"):
        if PKGVER_LINE.match(line):
            errs.append(f"{pkg}: diff must not touch pkgver/pkgrel (CI owns them)")
            break

    if BINARY_MARKER.search(diff):
        return errs + [f"{pkg}: binary diff is not supported (ship the blob via source=)"]

    entries, parse_error = git_numstat(diff)
    if parse_error:
        return errs + [f"{pkg}: {parse_error}"]
    if not entries:
        errs.append(f"{pkg}: diff touches no files")

    for path, _binary in entries:
        # `_binary` is unreachable now (BINARY_MARKER returns above); it stays in
        # the tuple so a future relaxation of that early return cannot slide a
        # binary patch past the path checks silently.
        why = path_problem(path)
        if why:
            errs.append(f"{pkg}: {why}")

    for m in OBJ_MODE.finditer(diff):
        kind = DENY_MODES.get(m.group(1))
        if kind:
            errs.append(f"{pkg}: diff would create a {kind} (mode {m.group(1)})")
    return errs


# --- validate -------------------------------------------------------------


def validate(data: dict, packages: list[str]) -> list[str]:
    """Check one request.  Accepts both a parsed body and a hand-written data
    file, so absent keys are read as empty rather than raising."""
    errs: list[str] = []

    ref = data.get("supersedes")
    if ref is not None and not (isinstance(ref, str) and ISSUE_REF.match(ref)):
        errs.append(f"supersedes must be an issue number like #12, got {ref!r}")

    summary = data.get("summary") or ""
    if not summary:
        errs.append("summary is empty")
    elif "\n" in summary:
        errs.append("summary must be a single line")

    # Unfenced fields are markdown: a line starting with `### ` would be read as
    # the next section, and a 4+ backtick line would swallow every section after
    # it.  The body would then parse as a different request (or not at all), so
    # reject it where the data is written, not after it is filed.
    for field in ("summary", "evidence"):
        text = data.get(field) or ""
        if SECTION_LINE.search(text):
            errs.append(
                f"{field} must not contain a line starting with '### ' "
                "(it would split the request into sections)"
            )
        if LONG_FENCE.search(text):
            errs.append(
                f"{field} must not contain a 4+ backtick fence at line start "
                "(it would swallow the sections after it)"
            )

    v = data.get("target_version") or ""
    if not TARGET_VERSION.match(v):
        errs.append(
            f"target_version must be a base semver like 1.20.0 (no pre/test suffix), got {v!r}"
        )

    flag = data.get("next_update")
    if flag is not None and not isinstance(flag, bool):
        errs.append(f"next_update must be true or false, got {flag!r}")

    state = data.get("diff_state")
    if state not in ("pending", "ready"):
        errs.append(f"diff_state must be pending or ready, got {state!r}")

    selection: dict[str, list[str]] = {}
    for dim, options in DIMENSIONS.items():
        values = data.get(dim) or []
        unknown = [v for v in values if v not in options]
        if unknown:
            errs.append(
                f"{dim}: unknown value(s) {', '.join(unknown)} "
                f"(allowed: {', '.join(options)})"
            )
        if not values:
            errs.append(f"{dim} is empty (select at least one {dim})")
        selection[dim] = [v for v in values if v in options]

    affected = resolve_pkgs(selection, packages)
    if not affected and all(selection.values()):
        unbuilt = [
            e for e in selection["edition"]
            if not any(package_dims(p)["edition"] == e for p in packages)
        ]
        if unbuilt:
            errs.append(
                f"edition {', '.join(unbuilt)} has no package in this repo yet "
                "(upstream ships it, this repo does not build it)"
            )
        else:
            errs.append(
                "no package matches the selection: "
                + ", ".join(f"{d}={','.join(v)}" for d, v in selection.items())
            )

    payload = data.get("payload") or {}

    if state == "pending":
        if payload:
            errs.append("diff_state=pending must not carry a payload")
        return errs

    if not affected:
        # An unresolved selection is already reported above; comparing payloads
        # against an empty package set would only add noise.
        return errs

    missing = sorted(set(affected) - set(payload))
    extra = sorted(set(payload) - set(affected))
    if missing:
        errs.append(f"payload missing packages: {', '.join(missing)}")
    if extra:
        errs.append(f"payload has packages not covered by the selection: {', '.join(extra)}")
    for pkg in sorted(set(affected) & set(payload)):
        ent = payload[pkg]
        diff, message = ent["diff"], ent["message"]
        if not diff.strip():
            errs.append(f"{pkg}: empty diff")
            continue
        if not message.strip():
            errs.append(f"{pkg}: empty commit message")
        else:
            # A leading blank line would make git's subject empty when the
            # message is used verbatim (the same-version case).
            if not message.split("\n", 1)[0].strip():
                errs.append(f"{pkg}: commit message starts with a blank line")
        if LONG_FENCE.search(diff) or LONG_FENCE.search(message):
            errs.append(f"{pkg}: content contains a 4+ backtick fence at line start")
        errs.extend(diff_errors(pkg, diff))
    return errs


# --- render ---------------------------------------------------------------


def render(data: dict, packages: list[str]) -> str:
    out: list[str] = []
    for f in SCALAR_FIELDS:
        out.append(f"### {f['label']}")
        out.append("")
        if f["id"] in DIMENSIONS:
            chosen = set(data.get(f["id"]) or [])
            for option in DIMENSIONS[f["id"]]:
                if option in chosen:
                    out.append(f"- {option}")
        elif f["type"] == "checkboxes":
            mark = "X" if data.get(f["id"]) else " "
            out.extend(f"- [{mark}] {option}" for option in f["options"])
        elif f["id"] == "supersedes":
            ref = data.get(f["id"])
            out.append("" if ref is None else str(ref).strip())
        else:
            out.append(str(data.get(f["id"]) or "").strip())
        out.append("")

    if data.get("diff_state") == "ready":
        for pkg in sorted(data.get("payload") or {}, key=package_order):
            ent = data["payload"][pkg]
            out.append(f"### {payload_label(pkg)}")
            out.append("")
            out.append("````diff")
            out.append(ent["diff"].strip("\n"))
            out.append("````")
            out.append("")
            out.append("````text")
            out.append(ent["message"].strip("\n"))
            out.append("````")
            out.append("")
    return "\n".join(out).rstrip() + "\n"


# --- form coupling --------------------------------------------------------


def form_document(form_path: Path) -> tuple[dict | None, str | None]:
    """Parse the Issue Form with yq.

    The form is YAML and regexes cannot see its structure: an indentation slip
    once passed the old regex checks while GitHub rejected the whole template.
    yq is the parser here, exactly as `git apply --numstat` is the diff parser.
    """
    require_tool("yq", " (check-form parses the form with it; pacman -S go-yq)")
    try:
        proc = subprocess.run(
            ["yq", "-o=json", "."],
            input=form_path.read_bytes(),
            capture_output=True,
            env={**os.environ, "LC_ALL": "C", "LANGUAGE": "C"},
        )
    except OSError as exc:
        return None, f"cannot run yq: {exc}"
    if proc.returncode != 0:
        lines = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        first = lines[0] if lines else "yq failed"
        return None, f"form is not valid YAML: {first}"
    try:
        return json.loads(proc.stdout), None
    except json.JSONDecodeError as exc:
        return None, f"yq produced no JSON: {exc}"


def check_form(form_path: Path, packages: list[str]) -> list[str]:
    doc, err = form_document(form_path)
    if err:
        return [err]
    body = doc.get("body") if isinstance(doc, dict) else None
    if not isinstance(body, list):
        return [f"{form_path.name}: no 'body:' list"]

    blocks = [b for b in body if isinstance(b, dict) and "id" in b]
    ids = [b["id"] for b in blocks]
    expected = [f["id"] for f in SCALAR_FIELDS] + [payload_id(p) for p in packages]
    errs: list[str] = []
    if ids != expected:
        errs.append(f"form field order/ids {ids} != expected {expected}")

    fields = SCALAR_FIELDS + [
        {"id": payload_id(p), "label": payload_label(p), "type": "textarea"}
        for p in packages
    ]
    by_id = {b["id"]: b for b in blocks}
    for f in fields:
        block = by_id.get(f["id"])
        if block is None:
            continue  # reported through the id mismatch above
        attrs = block.get("attributes") or {}
        if block.get("type") != f["type"]:
            errs.append(
                f"form field {f['id']} has type {block.get('type')!r}, expected {f['type']!r}"
            )
        if attrs.get("label") != f["label"]:
            errs.append(
                f"form field {f['id']} has label {attrs.get('label')!r}, expected {f['label']!r}"
            )
        if f.get("options"):
            # A dropdown lists bare strings, checkboxes list {label: ...} maps.
            raw = attrs.get("options") or []
            if f["type"] == "checkboxes":
                listed = [o.get("label") if isinstance(o, dict) else None for o in raw]
            else:
                listed = [o if isinstance(o, str) else None for o in raw]
            if listed != f["options"]:
                errs.append(
                    f"form {f['type']} {f['id']} options {listed} != {f['options']}"
                )
        if f.get("multiple") and attrs.get("multiple") is not True:
            errs.append(
                f"form dropdown {f['id']} must set 'multiple: true' "
                "(one request may span several values)"
            )
    return errs


# --- the gate: is a declared `supersedes` legal? --------------------------
# A body may claim to replace another issue.  The claim only means something if
# the old request exists, is the same base version, covers no package this one
# drops, and has already been applied to some package.  That is a rule about
# issue data, not about markdown, so it lives here once and both enforcers feed it
# what they fetched: `approve-mod-request.sh` when it pins a body, CI when it
# applies one.  Neither trusts the body alone.


def ref_number(ref: str) -> int:
    return int(ref.lstrip("#"))


def read_target(text: str) -> str:
    """target_version without parsing the whole body.

    A body filed before a field existed still names its version, and scanning
    the open requests has to survive it.
    """
    for label, lines in scan_sections(text.split("\n")):
        if label == "target_version":
            value = "\n".join(lines).strip()
            return "" if value == NO_RESPONSE else value
    return ""


def supersede_verdict(new: dict, old: dict, packages: list[str]) -> tuple[int, str]:
    """0 = a supersede, 2 = a supersede that strands packages, 1 = not one."""
    if new["target_version"] != old["target_version"]:
        return 1, (
            "not a supersede: target_version "
            f"{new['target_version']} != {old['target_version']}"
        )
    new_pkgs = resolve_pkgs(new, packages)
    gap = sorted(set(resolve_pkgs(old, packages)) - set(new_pkgs))
    if gap:
        return 2, (
            "supersede would strand packages not covered by the new request: "
            + ", ".join(gap)
        )
    return 0, f"supersede ok: {new['target_version']} covers {len(new_pkgs)} package(s)"


# --- applied records: a signature over readable data, not a line of markdown -
#
# "Has this request been applied to any package yet?" decides whether a further
# change replaces the issue or edits it in place.  A line of markdown cannot
# answer that -- anyone can type one -- and the comment's author is only a guess
# about account metadata.  So CI posts one record comment per issue and rewrites
# it as it applies packages: the signature sits on its first line, naming the
# issue, the base version, and which packages were applied, when, and in which
# build (`1.20.0pre1-2`), and the tasklist below renders that as progress.  The
# tick is for reading; the signature is what the gate reads.
#
# The data stays readable on purpose.  Signing is authentication, not
# confidentiality: an auditor reads what CI claims and then checks the
# signature.  The key is the AUR SSH key -- the same one CI already pushes with
# -- and its public half is committed at the repository root as
# bettbox-aur-updater.pub.  That asymmetry is the point: producing a record
# needs the private key, while anyone with a clone can verify one, including an
# agent reading the skill.  A symmetric key cannot do that, because checking it
# would hand out the power to forge.  A genuinely signed record pasted onto
# another issue still names the wrong issue, so it proves nothing there either.
APPLIED_KEY_ENV = "MR_APPLIED_KEY_FILE"
APPLIED_PUBKEY_ENV = "MR_APPLIED_PUBKEY_FILE"
APPLIED_NAMESPACE = "mod-request-applied"
# Only CI signs, so the committed file is one plain public key rather than an
# allowed-signers list.  ssh-keygen still wants a principal, so the verifier
# puts this one in front of the key on the way to a one-line allowlist.
APPLIED_PRINCIPAL = "mod-request"
APPLIED_PUBKEY = "bettbox-aur-updater.pub"
PUBKEY_TYPE_PREFIXES = ("ssh-", "ecdsa-", "sk-")
PUBKEY_B64 = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
# What CI has on disk after setup-submodules, and what a maintainer has locally.
APPLIED_KEY_DEFAULT = "~/.ssh/aur"
# One record is one comment: "<!-- mr-applied: {…} -->" on the first line, the
# applied packages inside the signed JSON.  The tasklist under it is a rendering
# of that field, so a tick on its own (an intention, not an application) is not
# a record and never counts.
RECORD = re.compile(r"<!--[ \t]*mr-applied:[ \t]*(?P<record>\{[^\n]*?\})[ \t]*-->")


def applied_key_path() -> Path:
    """The private key CI signs with, and a maintainer signs with locally."""
    raw = os.environ.get(APPLIED_KEY_ENV, "").strip() or APPLIED_KEY_DEFAULT
    path = Path(raw).expanduser()
    if not path.is_file():
        die(
            f"cannot read the applied-record signing key {path} "
            f"(set {APPLIED_KEY_ENV} if it lives elsewhere); it is the AUR SSH key"
        )
    return path


def applied_pubkey_path() -> Path:
    """The committed public half: what makes a record checkable by anyone."""
    raw = os.environ.get(APPLIED_PUBKEY_ENV, "").strip()
    path = Path(raw).expanduser() if raw else find_root() / APPLIED_PUBKEY
    if not path.is_file():
        die(
            f"cannot verify applied records: {path} is missing "
            f"(set {APPLIED_PUBKEY_ENV} if it lives elsewhere); it holds the "
            "public half of the signing key"
        )
    return path


def applied_pubkey_line() -> str:
    """The committed key as one line, or a refusal saying what is wrong."""
    path = applied_pubkey_path()
    text = path.read_text(encoding="utf-8")
    if "PRIVATE KEY" in text:
        die(
            f"{path} is a private key: the repository carries only the public "
            "half (`ssh-ed25519 AAAA…`)"
        )
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if len(lines) != 1:
        die(
            f"{path} must hold exactly one public key line (the "
            f"`ssh-ed25519 AAAA…` form); found {len(lines)}"
        )
    fields = lines[0].split()
    if (
        len(fields) < 2
        or not fields[0].startswith(PUBKEY_TYPE_PREFIXES)
        or not PUBKEY_B64.match(fields[1])
    ):
        die(
            f"{path} is not an SSH public key: expected one `ssh-ed25519 AAAA…` "
            "line, not a private key or an allowlist"
        )
    return " ".join(fields)


def ssh_sign(message: bytes) -> str:
    """Sign bytes with the AUR key; return the signature without its armor.

    `ssh-keygen -Y sign` writes an armored block to <file>.sig.  Only the base64
    body is kept: the record has to stay on one line, and re-armoring it is
    deterministic (see `ssh_verify`).
    """
    require_tool("ssh-keygen", " (an applied record is an SSH signature)")
    key = applied_key_path()
    with tempfile.TemporaryDirectory(prefix="mr-sign.") as tmp:
        data = Path(tmp) / "record"
        data.write_bytes(message)
        proc = subprocess.run(
            [
                "ssh-keygen", "-Y", "sign", "-f", str(key),
                "-n", APPLIED_NAMESPACE, str(data),
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            die(
                f"cannot sign the applied record with {key}: "
                f"{(proc.stderr or proc.stdout).strip()}"
            )
        armor = Path(str(data) + ".sig").read_text(encoding="ascii")
    return "".join(line for line in armor.splitlines() if not line.startswith("-----"))


def ssh_verify(message: bytes, blob: str) -> bool:
    """True only if `blob` is a signature over `message` by the committed key."""
    require_tool("ssh-keygen", " (an applied record is an SSH signature)")
    pubkey = applied_pubkey_line()
    with tempfile.TemporaryDirectory(prefix="mr-verify.") as tmp:
        sig = Path(tmp) / "record.sig"
        body = "\n".join(textwrap.wrap(blob, 70))
        sig.write_text(
            f"-----BEGIN SSH SIGNATURE-----\n{body}\n-----END SSH SIGNATURE-----\n",
            encoding="ascii",
        )
        # ssh-keygen takes an allowlist, not a bare key: the single principal
        # goes in front of the key the record is always checked under.
        signers = Path(tmp) / "allowed_signers"
        signers.write_text(f"{APPLIED_PRINCIPAL} {pubkey}\n", encoding="utf-8")
        proc = subprocess.run(
            [
                "ssh-keygen", "-Y", "verify", "-f", str(signers),
                "-I", APPLIED_PRINCIPAL, "-n", APPLIED_NAMESPACE,
                "-s", str(sig),
            ],
            input=message,
            capture_output=True,
        )
    return proc.returncode == 0


def seal(payload: dict) -> dict:
    """The payload with its signature attached; the fields stay as they were."""
    return {**payload, "sig": ssh_sign(canonical(payload).encode("utf-8"))}


def record_comment(payload: dict, covered: list[str]) -> str:
    """The record comment: signature, one readable line, then the progress list.

    The list is exactly the packages the request covers, ticked where the
    signed `applied` field names them, so the rendering cannot drift from the
    credential: rewriting one without re-signing the other is refused.

    The summary line carries the base version the request targets; an applied
    row adds the full version it landed in (`1.20.0pre1-2`), because the two
    channels reach that base as different builds and only one of them has been
    applied at a time.  An unapplied row names no version at all -- nothing has
    been released for it yet.
    """
    applied = payload["applied"]
    done = sum(1 for pkg in covered if pkg in applied)
    lines = [
        f"<!-- mr-applied: {canonical(seal(payload))} -->",
        f"**Applied at v{payload['version']}** — {done} of {len(covered)} packages",
        "",
    ]
    for pkg in covered:
        entry = applied.get(pkg)
        if entry:
            lines.append(f"- [x] `{pkg}` — {entry['full']}")
        else:
            lines.append(f"- [ ] `{pkg}`")
    return "\n".join(lines)


def unseal(text: str) -> dict | None:
    """The payload, or None unless text carries a signature a listed key made.

    The namespace separates this kind of signature from anything else the AUR
    key might sign, and `kind` says the same thing in the payload itself.
    """
    try:
        sealed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(sealed, dict):
        return None
    sig = sealed.pop("sig", "")
    if not isinstance(sig, str) or not sig:
        return None
    if not ssh_verify(canonical(sealed).encode("utf-8"), sig):
        return None
    return sealed if sealed.get("kind") == "applied" else None


def sealed_applied(
    comments: list, issue: int, version: str, covered: list[str]
) -> tuple[dict[str, dict], int, int]:
    """What this issue's signed record says was applied, and in which build.

    Returns `(applied, seen, verified)`: `applied` maps a covered package to
    `{"at": …, "full": …}` as the record gives it, `seen` counts signed-record
    markers at all and `verified` those whose signature holds.  The two counts
    separate "CI never applied anything" from "someone typed a record", because
    the answers lead to opposite instructions (edit in place vs open a
    replacement).  No key is needed until there is a marker to check, so an
    untouched request stays readable without one.
    """
    comments_with = [
        match
        for comment in comments
        if isinstance(comment, dict)
        for match in RECORD.finditer(comment.get("body") or "")
    ]
    if not comments_with:
        return {}, 0, 0
    wanted = set(covered)
    applied: dict[str, dict] = {}
    verified = 0
    for match in comments_with:
        payload = unseal(match.group("record"))
        if not payload:
            continue
        verified += 1
        if payload.get("issue") != issue or payload.get("version") != version:
            continue
        stamped = payload.get("applied")
        if not isinstance(stamped, dict):
            continue
        for pkg, entry in stamped.items():
            if pkg not in wanted or not isinstance(entry, dict):
                continue
            at = entry.get("at")
            full = entry.get("full")
            if not isinstance(at, str) or not at:
                continue
            if not isinstance(full, str) or not PKG_FULL.match(full):
                continue
            # A package applied twice keeps the first entry: the question is
            # "was it applied", not "when did CI last say so".
            applied.setdefault(pkg, {"at": at, "full": full})
    return applied, len(comments_with), verified


def applied_record(
    comments: list, issue: int, version: str, covered: list[str]
) -> tuple[dict | None, str]:
    """The record that some package of this request was applied.

    Returns `(record, "")`, or `(None, why)` saying what was looked for, so a
    refusal can name it.  The signature has to verify, the record has to name
    this issue and this version, and it has to cover a package this request
    does: a genuine record from elsewhere is still the wrong record here.
    """
    applied, seen, verified = sealed_applied(comments, issue, version, covered)
    if applied:
        return (
            {"kind": "applied", "issue": issue, "version": version, "applied": applied},
            "",
        )
    if not seen:
        return None, (
            "no signed applied record in its comments "
            "(CI posts one when it applies a package)"
        )
    if not verified:
        # A signature that does not verify is indistinguishable from a typed
        # record: say so, rather than implying CI wrote something it did not.
        return None, (
            "its applied record carries no valid signature "
            f"(checked against {APPLIED_PUBKEY})"
        )
    return None, (
        f"its applied record does not name #{issue} at {version} "
        f"(this request covers: {', '.join(sorted(set(covered))) or 'nothing'})"
    )


def gate_verdict(
    issue: int,
    body: str,
    live: list[dict],
    old: dict | None,
    packages: list[str],
) -> dict:
    """Decide whether one request may take effect, given what the caller saw.

    `live` is the caller's list of requests that already hold a version (for the
    approval helper: open + approved; for CI: the ones it is about to apply).
    `old` is the issue named in `supersedes`, fetched in full, or None.
    """
    reasons: list[str] = []
    close: int | None = None
    out: dict = {"ok": False, "issue": issue, "reasons": reasons, "close": None}
    try:
        new = parse(body, packages)
    except Error as exc:
        reasons.append(f"the body does not parse as a mod request ({exc})")
        return out
    out["target_version"] = new["target_version"]
    declared = new.get("supersedes")
    out["supersedes"] = declared

    # A gate that only *parsed* would still act on a body whose fields are
    # wrong, and `ref_number` would crash on `#12a`.  Refuse it here instead:
    # this is the last place the body is looked at before it takes effect.
    errs = validate(new, packages)
    if errs:
        reasons.extend(errs)
        return out

    if declared:
        if declared == f"#{issue}":
            reasons.append(f"issue #{issue} cannot supersede itself")
        elif old is None:
            reasons.append(
                f"cannot read issue {declared}, the one this request says it supersedes"
            )

    # Rule 4: one live request per package set.  Two approved requests sharing
    # a package race for the same commit, and the loser sits unapplied behind a
    # version that has moved past it -- the version rule then reads it as a
    # backport and it never lands.  Any diff_state counts: a pending placeholder
    # holds the set just as much as a ready payload does, so the first request
    # must land and close (or be named in supersedes) before the next one is
    # approved.  The version is not part of the key: two requests for the same
    # version but disjoint selections never meet in one tree and stay parallel.
    new_covered = set(resolve_pkgs(new, packages))
    for other in live:
        n = other.get("number")
        if n == issue:
            continue
        if declared and n == ref_number(declared):
            continue
        try:
            other_covered = set(resolve_pkgs(parse(other.get("body") or "", packages), packages))
        except Error:
            # The apply pass filters bodies that do not parse out of its live
            # set before the gate runs, so such an issue holds no package and
            # cannot race this one -- the same tolerance read_target has.
            continue
        shared = sorted(new_covered & other_covered)
        if shared:
            reasons.append(
                f"#{n}: both requests cover {', '.join(shared)}; one live "
                "request per package set -- land or close it first, or name "
                "it in supersedes"
            )

    # Rule 3: the replacement has to hold up before anything is closed.
    if declared and old is not None:
        if old.get("number") != ref_number(declared):
            reasons.append(
                f"the issue read as {declared} is #{old.get('number')}; "
                "the caller passed the wrong body"
            )
            return out
        try:
            old_data = parse(old.get("body") or "", packages)
        except Error as exc:
            reasons.append(
                f"{declared}: the replaced body does not parse, so it cannot be "
                f"superseded ({exc})"
            )
        else:
            rc, msg = supersede_verdict(new, old_data, packages)
            if rc:
                reasons.append(f"{declared}: {msg}")
            # Nothing was applied until a signed record says so; without one this
            # is a second issue for a version nobody has touched.
            try:
                record, why = applied_record(
                    old.get("comments") or [],
                    ref_number(declared),
                    old_data["target_version"],
                    resolve_pkgs(old_data, packages),
                )
            except Error as exc:
                reasons.append(f"{declared}: {exc}")
            else:
                if record is None:
                    reasons.append(
                        f"{declared}: {why}, so no package was ever applied; "
                        "edit it in place instead"
                    )
        if str(old.get("state") or "").upper() == "OPEN":
            close = ref_number(declared)

    out["ok"] = not reasons
    # Never hand a caller something to close on a refused verdict.
    out["close"] = close if out["ok"] else None
    return out


# --- approval pins --------------------------------------------------------

# Only GitHub's own association counts: a comment's text proves nothing.  This is
# the one definition of "whose pin is an approval" -- the approval script and
# the update run both ask here rather than filtering comments themselves.
MAINTAINER_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")
APPROVAL_PIN = re.compile(r"<!-- mr-approved sha256=([0-9a-f]{64}) -->")


def newest_pin(comments: list) -> str:
    """The hash in the newest pin a maintainer posted, or "" when there is none.

    Newest *trusted* pin, not the newest pin outright: taking the last comment
    that matches would let a third party mask a real approval by posting a fake
    pin afterwards.
    """
    for comment in reversed(comments):
        if not isinstance(comment, dict):
            continue
        if (comment.get("authorAssociation") or "") not in MAINTAINER_ASSOCIATIONS:
            continue
        found = APPROVAL_PIN.search(comment.get("body") or "")
        if found:
            return found.group(1)
    return ""


def load_comments(path: str, what: str) -> list | None:
    try:
        comments = json.loads(read_text(path))
    except json.JSONDecodeError as exc:
        print(f"mod-request: cannot read {what}: {exc}", file=sys.stderr)
        return None
    if not isinstance(comments, list):
        print(f"mod-request: {what} must be a JSON array", file=sys.stderr)
        return None
    return comments


# --- io -------------------------------------------------------------------


def read_text(path: str | None) -> str:
    return sys.stdin.read() if path in (None, "-") else Path(path).read_text(encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=None, help="repo root (default: auto-detect)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("render", help="data JSON -> issue body markdown")
    p.add_argument("--data", "-d", default=None, help="data JSON file (default: stdin)")
    p.add_argument("--out", "-o", default=None, help="output file (default: stdout)")
    p.add_argument("--allow-invalid", action="store_true")

    p = sub.add_parser("parse", help="issue body markdown -> canonical JSON")
    p.add_argument("--file", "-f", default=None, help="issue body file (default: stdin)")

    p = sub.add_parser("hash", help="sha256 of the canonical structure")
    p.add_argument("--file", "-f", default=None)

    p = sub.add_parser("check", help="validate an issue body")
    p.add_argument("--file", "-f", default=None)

    p = sub.add_parser("resolve", help="print the packages a request covers, one per line")
    p.add_argument("--file", "-f", default=None)

    p = sub.add_parser(
        "target",
        help="print only target_version (tolerates a body that is otherwise stale)",
    )
    p.add_argument("--file", "-f", default=None)

    p = sub.add_parser("check-form", help="validate the Issue Form YAML against this script")
    p.add_argument(
        "--form",
        default=None,
        help="form path (default: <root>/.github/ISSUE_TEMPLATE/mod-request.yml)",
    )

    p = sub.add_parser(
        "supersede",
        help="does NEW supersede OLD? (only needed once OLD is partly applied)"
             " 0 yes, 2 yes-with-gap, 1 no",
    )
    p.add_argument("--new", required=True, dest="new_file")
    p.add_argument("--old", required=True, dest="old_file")

    p = sub.add_parser(
        "applied",
        help="print the packages of a request that its signed record says were applied",
    )
    p.add_argument("--issue", required=True, type=int)
    p.add_argument("--body", required=True)
    p.add_argument("--comments", required=True, help="JSON array of the issue's comments")
    p.add_argument("--version", default=None, help="default: the body's target_version")

    p = sub.add_parser(
        "pin",
        help="print the sha256 in the newest maintainer approval pin"
        " (exit 1 when there is none)",
    )
    p.add_argument(
        "--comments",
        required=True,
        help="JSON array of the issue's comments",
    )

    p = sub.add_parser(
        "token",
        help="print the signed applied-record comment CI posts for an issue",
    )
    p.add_argument("--issue", required=True, type=int)
    p.add_argument("--body", required=True, help="the request body (its package selection)")
    p.add_argument("--version", default=None, help="default: the body's target_version")
    p.add_argument(
        "--comments",
        default=None,
        help="JSON array of the issue's comments: the record already there is kept",
    )
    p.add_argument(
        "--pkg",
        required=True,
        action="append",
        dest="pkgs",
        help="a package this run applied and the version it published, "
        "as <package>=<pkgver>-<pkgrel> (repeatable)",
    )

    p = sub.add_parser(
        "gate",
        help="decide whether a request may take effect (reads issue JSON, never gh)",
    )
    p.add_argument("--issue", required=True, type=int)
    p.add_argument("--body", required=True)
    p.add_argument(
        "--open-approved",
        required=True,
        dest="open_approved",
        help="JSON array of {number, body}: requests that already hold a version",
    )
    p.add_argument(
        "--old",
        default=None,
        help="JSON {number, body, state, comments} for the issue supersedes names",
    )
    p.add_argument(
        "--no-close",
        action="store_true",
        help="drop the close hint (CI never closes; the approval helper does)",
    )

    args = ap.parse_args()
    root = find_root(args.root) if args.root else find_root()
    packages = discover_packages(root)

    if args.cmd == "applied":
        if args.issue <= 0:
            die("--issue must be a positive issue number")
        data = parse(read_text(args.body), packages)
        version = args.version or data["target_version"]
        if not TARGET_VERSION.match(version):
            die(f"--version must be a base semver like 1.20.0, got {version!r}")
        try:
            comments = json.loads(read_text(args.comments))
        except json.JSONDecodeError as exc:
            print(f"mod-request: cannot read the comments JSON: {exc}", file=sys.stderr)
            return 2
        if not isinstance(comments, list):
            print("mod-request: --comments must be a JSON array", file=sys.stderr)
            return 2
        applied, seen, verified = sealed_applied(
            comments, args.issue, version, resolve_pkgs(data, packages)
        )
        for pkg in sorted(applied):
            print(pkg)
        if not applied and seen and not verified:
            # Not fatal: nothing is applied, so CI re-applies.  Say it anyway --
            # a record nobody can verify is not progress, and silence would read
            # like one.
            print(
                f"mod-request: the applied record on #{args.issue} carries no "
                f"valid signature (checked against {APPLIED_PUBKEY}); "
                "no package counts as applied",
                file=sys.stderr,
            )
        return 0

    if args.cmd == "pin":
        comments = load_comments(args.comments, "the comments JSON")
        if comments is None:
            return 2
        pin = newest_pin(comments)
        if not pin:
            print(
                "mod-request: no approval pin from a maintainer in these comments",
                file=sys.stderr,
            )
            return 1
        print(pin)
        return 0

    if args.cmd == "token":
        if args.issue <= 0:
            die("--issue must be a positive issue number")
        data = parse(read_text(args.body), packages)
        covered = resolve_pkgs(data, packages)
        version = args.version or data["target_version"]
        if not TARGET_VERSION.match(version):
            die(f"--version must be a base semver like 1.20.0, got {version!r}")
        if version != data["target_version"]:
            die(
                f"--version {version} is not this request's target_version "
                f"{data['target_version']}"
            )
        requested: dict[str, str] = {}
        for spec in args.pkgs:
            pkg, sep, full = spec.partition("=")
            if not sep or not pkg or not full:
                die(
                    f"--pkg {spec!r} must be <package>=<pkgver>-<pkgrel>, "
                    "e.g. bettbox-pre=1.20.0pre1-2"
                )
            if not PKG_FULL.match(full):
                die(
                    f"--pkg {spec!r}: {full!r} is not a full version "
                    "(<pkgver>-<pkgrel>, e.g. 1.20.0pre1-2)"
                )
            requested[pkg] = full
        unknown = sorted(set(requested) - set(covered))
        if unknown:
            die(
                f"package(s) this request does not cover: {', '.join(unknown)} "
                f"(it covers {', '.join(covered)})"
            )
        applied: dict[str, dict] = {}
        if args.comments:
            try:
                comments = json.loads(read_text(args.comments))
            except json.JSONDecodeError as exc:
                print(
                    f"mod-request: cannot read the comments JSON: {exc}",
                    file=sys.stderr,
                )
                return 2
            if not isinstance(comments, list):
                print("mod-request: --comments must be a JSON array", file=sys.stderr)
                return 2
            applied, _, _ = sealed_applied(comments, args.issue, version, covered)
        # The record is rewritten, not appended to: what earlier runs applied is
        # read back from it and re-signed together with this run's packages.
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        merged = dict(applied)
        for pkg, full in sorted(requested.items()):
            merged.setdefault(pkg, {"at": stamp, "full": full})
        payload = {
            "kind": "applied",
            "issue": args.issue,
            "version": version,
            "applied": merged,
        }
        body = record_comment(payload, covered)
        # A signature nobody can check is not a record.  If the signing key and
        # the committed public key ever drift apart, CI would post a record that
        # means nothing -- fail here, where the record is made.
        sealed = RECORD.search(body)
        if sealed is None or unseal(sealed.group("record")) is None:
            die(
                "the record does not verify against "
                f"{APPLIED_PUBKEY}: the key at {applied_key_path()} is not the "
                "one the repository commits, so nobody could tell CI's record "
                "from a typed one"
            )
        print(body)
        return 0

    if args.cmd == "gate":
        try:
            live = json.loads(read_text(args.open_approved))
            old = json.loads(read_text(args.old)) if args.old else None
        except json.JSONDecodeError as exc:
            # The caller's fetch is broken, not the request: say "could not
            # decide" instead of handing back a verdict nobody can trust.
            print(f"mod-request: cannot read the issue JSON the gate needs: {exc}", file=sys.stderr)
            return 2
        verdict = gate_verdict(args.issue, read_text(args.body), live, old, packages)
        if args.no_close:
            verdict["close"] = None
        print(json.dumps(verdict, sort_keys=True))
        return 0 if verdict["ok"] else 1

    if args.cmd == "check-form":
        form = Path(args.form) if args.form else root / ".github/ISSUE_TEMPLATE/mod-request.yml"
        errs = check_form(form, packages)
        for e in errs:
            print(f"mod-request: {e}", file=sys.stderr)
        return 1 if errs else 0

    if args.cmd == "supersede":
        new = parse(read_text(args.new_file), packages)
        old = parse(read_text(args.old_file), packages)
        rc, msg = supersede_verdict(new, old, packages)
        print(msg, file=sys.stdout if rc == 0 else sys.stderr)
        return rc

    if args.cmd == "render":
        data = json.loads(read_text(args.data))
        errs = validate(data, packages)
        if errs and not args.allow_invalid:
            for e in errs:
                print(f"mod-request: {e}", file=sys.stderr)
            return 1
        body = render(data, packages)
        if len(body) > GITHUB_BODY_LIMIT:
            print(
                f"mod-request: the rendered body is {len(body)} characters; GitHub "
                f"caps an issue body at {GITHUB_BODY_LIMIT}, so this cannot be filed",
                file=sys.stderr,
            )
            return 1
        if args.out:
            Path(args.out).write_text(body, encoding="utf-8")
        else:
            sys.stdout.write(body)
        return 0

    if args.cmd == "target":
        # Reading one field must not depend on the rest of the body parsing.
        print(read_target(read_text(args.file)))
        return 0

    data = parse(read_text(args.file), packages)

    if args.cmd == "parse":
        sys.stdout.write(json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n")
        return 0

    if args.cmd == "hash":
        print(digest(data))
        return 0

    if args.cmd == "check":
        errs = validate(data, packages)
        for e in errs:
            print(f"mod-request: {e}", file=sys.stderr)
        if errs:
            return 1
        size = len(read_text(args.file))
        if size > GITHUB_BODY_LIMIT:
            print(
                f"mod-request: the body is {size} characters; GitHub caps an issue "
                f"body at {GITHUB_BODY_LIMIT}, so this was not filed as one request",
                file=sys.stderr,
            )
            return 1
        ref = data.get("supersedes")
        replaces = f", supersedes {ref}" if ref else ""
        print(
            f"ok: {data['target_version']}, "
            f"{len(resolve_pkgs(data, packages))} package(s), "
            f"{data['diff_state']}{replaces}, sha256 {digest(data)}"
        )
        return 0

    if args.cmd == "resolve":
        for pkg in resolve_pkgs(data, packages):
            print(pkg)
        return 0

    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Error as exc:
        print(f"mod-request: {exc}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as exc:
        print(f"mod-request: invalid data JSON: {exc}", file=sys.stderr)
        sys.exit(1)
    except OSError as exc:
        print(f"mod-request: {exc}", file=sys.stderr)
        sys.exit(1)
