# AGENTS.md

## Repo purpose

Manages AUR packages for [Bettbox](https://github.com/appshubcc/Bettbox). The repo contains no application code — only PKGBUILDs, CI workflows, and AUR submodule pointers. The package inventory, dual-channel model and project description live in [README.md](README.md); this file is the operational playbook (what an agent must know to edit/update/debug the repo).

## Structure

The package inventory (seven `aur/*/` submodules, stable / pre channels, arch + build type per package) is the table in [README.md](README.md). Agent-relevant rules on top of it:

- The channel is the `*-pre` pkgname suffix (no suffix = stable).
- All seven packages normalize to one install layout and `provides=bettbox=<ver>` (via `${pkgname%-…}`) and conflict with each other — keep new packages consistent with that.
- Each `aur/<pkg>` is a git submodule at `ssh://aur@aur.archlinux.org/<pkgname>.git`; README's 子模块管理 has the clone/add commands.

### Multi-arch `-bin` packages and `${arch}` in per-arch sources

Upstream names its Linux debs by Debian architecture (`amd64`/`arm64`), which never matches the AUR names (`x86_64`/`aarch64`), so a dual-arch `-bin` PKGBUILD spells out **both** the cache name and the URL per architecture. `${arch}` must not be used there: makepkg binds it to the **host** architecture while expanding per-arch arrays, so `source_aarch64` would expand `${arch}` to `x86_64` and collide with `source_x86_64`. See `aur/bettbox-pre-bin/PKGBUILD`.

## Version model

- **The pre channel is a superset of stable.** The pre channel's meaning is "install the newest release upstream has":
  - stable accepts formal tags only; its target is the highest formal release;
  - pre accepts formal *and* `-pre` tags; its target is whichever of the two versions higher;
  - the two channel targets are equal exactly when no `-pre` is newer than the latest formal tag. Upstream ships a `-pre` series ahead of every formal tag, so pre sits ahead through most of a release cycle and the two coincide only in the window after a formal release. Either way each channel resolves its target on its own.
- nvchecker does the ordering per PEP440: `-pre1` normalizes to `rc1`, so `v1.19.4` > `v1.19.4-pre1` and `use_max_release = true` picks the formal tag on its own.
- The pre channel has 4 packages, stable 3 (`bettbox-pre-bin` is pre-only because the stable slot `bettbox-bin` belongs to another maintainer).
- pkgver never contains a hyphen (makepkg rejects it): pre versions are spelled `1.19.4pre1`. `_pkgver="${pkgver/pre/-pre}"` re-inserts the hyphen for the tarball URL (`archive/v1.19.4-pre1.tar.gz`) and the extracted source dir. It is a no-op at a formal pkgver and still required while a `-pre` is ahead — do not remove it.
- Source PKGBUILDs select `APP_ENV` from the version: `local app_env=stable; [[ "${pkgver}" == *pre* ]] && app_env=pre`. **This line needs no per-channel adjustment**: upstream's own rule is "no `-` in the ref means a stable build", so pre picking up a formal tag installs that tag's `APP_ENV=stable` artifact. pkgver tracks exactly what gets installed.
- Each package carries its own `.nvchecker.toml` (GitHub releases API — drafts and tag-only releases are excluded; junk tags like `v1.19.2-test` never surface). The GitHub token goes through a keyfile in CI (`[keys] github = ...`), **not** the `GITHUB_TOKEN` env var (that returns 403).
- **`.nvchecker.toml` must be pushed together with PKGBUILD.** Pushing only the PKGBUILD leaves the pre-only regex on AUR, so the next `update-aur` re-resolves `1.19.4pre1` and reverts the `1.19.4` you just published.
- The pre-bin `.deb` asset is cached under `bettbox-compatible-<ver>-x86_64.deb` (URL built from `_pkgver`, keeps the `compatible` name); source tarballs come from `archive/v${_pkgver}.tar.gz`, i.e. `archive/v1.19.4.tar.gz` at a formal version.

## CI workflows

Updating is **one** pipeline, `update-aur.yaml`: it resolves the channel targets and hands the whole request pass to one script call in the same run — nothing is handed over between steps, so nothing can go stale. `scripts/apply-mod-request.sh` is an applier only (there is no verdict mode), and the version rule exists once, in it. `update-aur-approval.yaml` is the only other file: it owns the issue trigger and calls that pipeline, so the two histories stay apart (see below).

- **`update-aur.yaml`** — the only place a version is resolved, and the only pipeline: one job, one pass.
  - `schedule` (cron) and `repository_dispatch` of type `bettbox-release` from the appshubcc/Bettbox release job — chase the resolved targets. A dispatch has no inputs: `force` and `dry_run` come from `client_payload` (booleans), and the sender sets `force` when a Release for that tag already existed, i.e. the same version is being re-published.
  - `workflow_dispatch` — a human run. Its inputs are `force` (bypass the per-package skip, bump `pkgrel` with the version unchanged — for a checksum/hash refresh) and `dry_run` (preview only). Job-level `env` maps `inputs.* || client_payload.* || 'false'` into `FORCE`/`DRY_RUN`, so every trigger feeds one pair of string variables.
  - `workflow_call` from `update-aur-approval.yaml` — an approval, identified by the `approval` input rather than by anything in the payload: inside a called workflow the event name is the call itself, so the caller states the intent explicitly. A called job may only keep or reduce its token, so the caller grants `contents: write` + `issues: write` and passes the three secrets by name (`secrets: inherit` would work here too, but naming them keeps the contract in the caller).
  - `Check and update AUR packages` is one step, in two phases. It first runs nvchecker per package (each package's own `.nvchecker.toml`), writes `<pkg> <cur> <tgt>` to `/tmp/aur-targets.txt`, and emits `inconsistent` and `version` (the highest target, chosen with `vercmp` so `1.19.4` outranks `1.19.4pre3`). That file is the version vector everything downstream reads.
  - The same step then calls `scripts/apply-mod-request.sh --versions /tmp/aur-targets.txt --repo … --no-pkgrel` **with no body and no issue** (see Mod requests): the script lists the open, `approved` issues itself (`approved` is the only label — the body decides whether an issue is a request, and the per-issue check stays because the view is the fresher call), keeps those pinned to the body as it is now, runs `mod-request.py gate` on each (failing closed), and applies each payload where the version rule allows. It reports three machine lines — `#mr-body <issue> <sha256>`, `#mr-held <pkg> <issue>`, `#mr-applied <pkg> <issue>`; the step re-reads every gated body with `gh issue view` and aborts unless its hash still matches `#mr-body`, so the commit message and the signed record come from the body that was actually applied. `--no-pkgrel` is always passed: CI owns pkgrel (version moves → `pkgrel=1`, `force` → +1, request-only → +1).
  - The per-package loop then patches pkgver/pkgrel → `updpkgsums` → `makepkg --printsrcinfo` → commit + `git push origin HEAD:master`. Skipping is per package and neither pass replaces the other — see Key behaviors.
  - A `pending` request yields `#mr-held` and holds that package while the rest of the loop proceeds; `force` does not override it (force refreshes hashes, it does not decide an unadapted version may ship).
  - Once every push in the loop is done, CI rewrites that issue's signed record **once** (`token --body … --comments … --pkg <pkg>=<pkgver>-<pkgrel>`, one `--pkg` per package) and closes it once every covered package is in it. A request-only run has no version to report, so the parent-pointer subject names the requests instead (see Conventions).
  - The step emits `worked` (true only once a commit was made); the two parent-pointer steps run only when it is true and `DRY_RUN` is off.
  - The end of the step writes a **run summary** (`$GITHUB_STEP_SUMMARY`): which package was applied or held by which request, whether a commit was made, and the script's own per-request notes (`not due yet`, `next_update`, …). On an approval it also emits one `::notice title=Mod request approval::applied=N held=M` — the machine-readable verdict the approval workflow's prune sweep reads back (annotations are the only part of a finished run the API exposes; the summary is UI only). A green run that changed nothing is a decision, not an empty log — this is where that answer stays readable.
  - Requires `issues: write`; `github-cli` provides `gh`, `pacman-contrib` provides `vercmp`, and `nvchecker` resolves the targets inside the Arch container. Records are signed with the AUR key `setup-submodules` already provisions at `~/.ssh/aur` — no extra secret. A step right after `setup-submodules` derives that key's public half and refuses the run unless it matches `bettbox-aur-updater.pub`: `token` would otherwise reject the signature only *after* the commits are pushed, leaving a push with no record.
  - Parent-pointer subject on a real bump follows per-channel inconsistency: `fix: sync all packages to v<ver> in <stable|pre> channel` / `fix: sync all packages to v<ver>` / `Update to v<ver>`; with `force`: `fix: force update source hashes of v<ver>`. The `in <channel> channel` subject appears when exactly one channel's target moved, which is whenever the two targets differ — i.e. while a `-pre` is newer than the latest formal tag.
- **`update-aur-approval.yaml`** — the issue trigger, and the only place a run can come from a comment:
  - `issue_comment` on an approval pin — `approve-mod-request.sh` posts a new pin on every approval, so a re-approval also fires; the `approved` label does not change there (`--add-label` is a no-op and emits no event), which is why the label is not the trigger. Nothing removes and re-adds that label to fake an event: the label is half of the live predicate, and a `pending` request would drop out of the gate for the length of two API calls — exactly when a version must not move. Tolerating a re-run is harmless (the run re-reads the issue from scratch), and a hand-added label has no pin and does nothing — labels need triage+ to touch, so a third party cannot add or strip `approved` at all; the forgeable surface is comments, which is why the pin checks `authorAssociation`. `GITHUB_TOKEN`-created comments are suppressed like any other event, so approvals have to come from a person's token. The pin gate is the `triage` job's `if`, so anything else on an issue yields a skipped job.
  - **`triage` runs an inline pre-check before the pipeline is called at all.** An approval folds every target onto the published version, so the pipeline applies exactly when the request is `ready`, `next_update` is unticked, and a covered package already sits on the request's base version — all three are provable from the issue body and the submodule checkouts, which is the same version source the pipeline reads (not the AUR RPC: the two can disagree while a sync lags). Only a proven no-op skips the `workflow_call`; anything unproven — parse failure, unreadable issue, a package outside `aur/*` — runs the pipeline anyway, because a wasted no-op run costs minutes while a dropped approval sits until cron. The cron run is the safety net for every miss: it re-reads the issue and applies a ready request even without a triggering comment. The pipeline still re-checks the pin, the label, the body and the version rule itself; triage only keeps a proven no-op from starting the container.
  - An approval is a statement about the *request*, not about a release: `approval: true` collapses the targets to each package's **published** version (`awk '{print $1,$2,$2}'` over the vector, "nothing moves"). A `ready` request whose `next_update` is unticked then applies at once as a packaging change; `pending`, `next_update`, or an unpublished target give the run nothing to do, which is the correct "wait" answer. Every other trigger resolves the real targets.
  - A `prune` job sweeps the no-op runs. A run cannot delete itself — the API deletes only runs that have completed (or are more than two weeks old), and cancelling first would kill the job before it could retry — so every run sweeps the completed runs behind it, keeping the rows that say something: a run whose verdict annotation reads `applied=N` with `N > 0` stays, one with `applied=0` (triage proved it, or the pipeline landed nothing) goes, and one with no verdict never ran the pipeline at all (a comment that was not a pin). Failures are never touched — that is the one row that has to stay until someone has looked at it — and a listing or annotation read failure keeps the run rather than mistaking it for a no-op. Pruning is housekeeping, so every failure in it is a warning, never a failed run.
- **Concurrency** — `update-aur` with `cancel-in-progress: false` is declared **once**, on the pipeline's job (`jobs.update-aur-packages.concurrency`), and nowhere else. Job-level concurrency is the form a called workflow shares with its caller, so that one key serializes scheduled, dispatched and approved runs; declaring the same group a second time (either side) could leave the approval run waiting on itself. The `triage` job deliberately stays outside the group: it only reads the issue and the parent's recorded submodule state, which a concurrent pipeline run cannot move. The effect is unchanged: a run never resolves a target while another run it did not see is still writing, and an approval's "previous run" is always finished.
- **`sync-from-aur.yaml`** — manual reverse sync (AUR → parent); parent-pointer commit is `chore: sync AUR submodules to latest`.

## Key behaviors an agent must know

- `.SRCINFO` is **always** regenerated via `makepkg --printsrcinfo`, run as the `builder` user inside the container job. Never hand-edit `.SRCINFO`.
- Checksums are refreshed by `updpkgsums`, which downloads every source (including the `sha256sums_x86_64/_aarch64` arrays) and is idempotent for the local hook/desktop files.
- Updates are atomic **per channel** (3 stable packages, 4 pre packages): if one package in a channel is behind, the whole channel gets bumped. When both channel targets coincide a run advances all seven packages at once; when they differ each channel advances on its own and the pointer-commit subject names the channel that moved.
- One per-package skip guards writes, and it is the only one: a package whose pkgver already equals its target is skipped whole on a non-force run (no pkgver/pkgrel write, no `updpkgsums`, no `makepkg --printsrcinfo`, no commit), so a run that moves one channel does not reset the other channel's already-settled `pkgrel`. There is no run-level "nothing to do" gate: the loop always visits all seven packages, and the parent-pointer steps wait for `worked`, which only turns true once a commit exists.
- `force` bypasses the per-package skip — the condition tests `!= "true"` and the loop reads it from the job env — so it is the only input that touches a package whose version has not moved.
- The `force` input bumps `pkgrel` while keeping the version — used to push a checksum/hash refresh without a version change (`fix:` prefix, body lists nothing since no versions moved).
- Mod requests sit *above* the per-package skip: a `pending` request holds a package the channel would otherwise move, and a `ready` one can make a run do work with no version moving. Blocking and applying are per package, so one held package does not stall the other six.

## CI container quirks

The update job runs git as root and makepkg as a throwaway `builder` user inside the container, over a workspace owned by the runner user:

- `git config --global --add safe.directory '*'` is set in **both** `setup-submodules` and the main step so root git ops tolerate the live owner transition (`chown -R builder "$GITHUB_WORKSPACE"` happens mid-job).
- OpenSSH resolves `~/.ssh` from the **passwd home** (`/root`), not `$HOME` (`/github/home`) — so every `GIT_SSH_COMMAND` uses explicit `$HOME`-expanded paths: `ssh -i $HOME/.ssh/aur -o IdentitiesOnly=yes -o UserKnownHostsFile=$HOME/.ssh/known_hosts`. `setup-submodules` writes the key and the keyscan'd known_hosts into `$HOME/.ssh`.
- makepkg refuses to run as root: the `run_makepkg` wrapper runs `runuser -u builder -- env ...` with `BUILDDIR/PKGDEST/SRCDEST` under `/tmp`.

## Submodules run detached HEAD

`git submodule update --init` / `--remote` (and `git clone --recurse-submodules`, plus the `git checkout origin/master` in `sync-from-aur.yaml`) checks submodules out in detached HEAD. The local `master` branch is **not** moved by `--remote`, so it lags one commit behind HEAD; with a dirty PKGBUILD/.SRCINFO tree, a plain `git checkout master` then fails with "local changes would be overwritten".

Workaround: commit and push while staying detached — no branch switch needed (this is also what CI does via `git push origin HEAD:master`). `git checkout -B master HEAD` is optional, only to re-attach the local branch for a human-editable state:

```sh
git -C aur/bettbox add PKGBUILD .SRCINFO
git -C aur/bettbox commit -m "fix(package): ..."
git -C aur/bettbox push origin HEAD:master        # publish to AUR; enough on its own
# optional, omitted by CI:
git -C aur/bettbox checkout -B master HEAD       # fast-forward local master to the new commit
```

Cleaner alternatives: if the tree is clean, re-attach first with `git -C aur/<pkg> checkout -B master HEAD`, or fetch with `git -C aur/<pkg> submodule update --remote --merge` to keep `master` attached and up to date.

## Syncing a local submodule change

`sync-from-aur.yaml` is **reverse-direction only** (AUR → parent): it pulls AUR HEAD into the parent pointers. It does **not** push submodule changes — publish those to AUR yourself first. The parent pointer moves through **this workflow alone** (uniform flow); there is no manual parent-commit alternative.

For any change (local submodule edit or AUR-side edit → AUR → parent):

1. Push the AUR commits first, so the parent pointer never dangles when the workflow runs (applies to every `aur/*/` submodule):

```sh
for sub in aur/*/; do
  git -C "$sub" push origin HEAD:master            # publish to AUR
done
```

2. Run `sync-from-aur.yaml` (manual workflow_dispatch). It fetches AUR HEAD into the submodules and commits the parent pointer as `chore: sync AUR submodules to latest`, then pushes the parent — no manual parent commit involved.
3. Pull the parent repo back locally: `git pull` (or `git fetch && git merge --ff-only origin/master`) — the pointer commit was pushed by the workflow, so your clone must fetch it.

**Commit message:** AUR submodule pointers are `chore: sync AUR submodules to latest` — always produced by `sync-from-aur.yaml`. When the pointers moved, the workflow appends a body listing each changed `aur/<pkg>` followed by its indented `git log --oneline` lines.

First-time additions of new packages (new submodules in the parent) are **not** a pointer sync: they use `feat: add packages <pkg1>, <pkg2>, ...` (e.g. `feat: add packages bettbox-pre, bettbox-compatible-pre, bettbox-compatible-pre-bin`).

Re-running it is a no-op consistency check: it skips committing when the parent pointers already match AUR HEAD.

## AUR remoteness

Each AUR package repository is **single-branch**: `master` is the only branch and holds the only tip that exists, so an older release has no branch to be re-edited on. That is a property of AUR rather than a choice made here, and it produces two consequences: a request can never be a **backport** (see Mod requests), and an AUR push can always be a fast-forward.

Only one maintainer edits the AUR packages, and changes come from exactly two sources: pushes from this repo and the CI `update-aur` workflow. AUR `master` therefore only ever advances linearly, with no reconcile or force needed.

## Local changes without update-pkgver.sh

The per-package `update-pkgver.sh` scripts are deprecated and gone — the version/checksum/.SRCINFO pipeline lives entirely in `update-aur.yaml`. To preview or push a refresh without a version change, run `update-aur.yaml` via `workflow_dispatch` with `force=true` (add `dry_run=true` to preview first). Direct submodule edits (e.g. a release-critical fix) still go through: edit → commit inside `aur/<pkg>` → `git push origin HEAD:master` (detached-HEAD safe, see above) → parent pointer via `sync-from-aur.yaml` — see "Syncing a local submodule change".

## Manually editing AUR packages

Routine version bumps go through CI; any manual change follows **one uniform flow**: edit & push to AUR → run `sync-from-aur` → pull the parent repo.

**Local environment prep** (before any edit):

- **Fetch AUR origin first and check for drift** — `git -C aur/<pkg> fetch origin` (AUR is the submodule's origin). Compare AUR HEAD with the parent's recorded pointer: `git rev-parse HEAD:aur/<pkg>` vs `git -C aur/<pkg> rev-parse origin/master`. If they differ, the parent is behind AUR — **trigger `sync-from-aur.yaml` before editing**, so you start from the current AUR state instead of a stale pointer.
- Work in a clean `aur/<pkg>` worktree (or a separate AUR clone). Submodules sit in detached HEAD — that's fine for edits: commit and push while detached (`git push origin HEAD:master`), no branch switch needed; if not checked out, `git submodule update --init aur/<pkg>`. Mechanics in "Submodules run detached HEAD".
- Tools (Arch host or container, run as **non-root** user): `makepkg` (pacman) and `updpkgsums` (pacman-contrib) — makepkg refuses to run as root; `bash -n` needs only bash. CI does the same work as the throwaway `builder` user inside the container.
- Publishing needs the AUR SSH key + keyscanned known_hosts in `$HOME/.ssh` (`aur`, `known_hosts`) — the material CI provisions in Job container; locally it must live in your own `$HOME/.ssh`.
- Keep the channel consistent: a `*-pre` package's pkgver must equal what its own `.nvchecker.toml` resolves to, spelled `1.19.4` for a formal tag and `1.19.4pre1` for a `-pre` one. The spelling follows the resolved target, and `APP_ENV` follows the pkgver — see [Version model](#version-model).

**Edit + push to AUR:**

1. Edit the PKGBUILD — either in a separate AUR clone, or inside `aur/<pkg>` (mechanics in "Submodules run detached HEAD").
2. If `source=` changed, refresh hashes with `updpkgsums` (re-downloads every source incl. the arch-specific arrays, rewrites `sha256sums`).
3. Regenerate metadata: `makepkg --printsrcinfo > .SRCINFO` — never hand-edit `.SRCINFO`.
4. Sanity-check: `bash -n PKGBUILD`.
5. Commit with a conventional message and push to AUR (`git push` from a separate clone, or `git -C aur/<pkg> push origin HEAD:master`).

**Sync the parent pointer — always `sync-from-aur`, never a manual parent commit:**

- Run `sync-from-aur.yaml` (manual workflow_dispatch): it fetches AUR HEAD into the submodules, commits `chore: sync AUR submodules to latest` on the parent (body lists each changed `aur/<pkg>` with its `git log --oneline`), and pushes it. No-op when the pointers already match.
- Then pull the parent repo locally: `git pull` — the pointer commit was pushed by the workflow, not by your clone (see "Syncing a local submodule change").

## Mod requests

Proposed changes to packaging content go through a **mod request** (M-R), not a
direct edit: a request is anchored to a base semver (`1.20.0`) rather than to a
channel, so one request covers both the `-pre` builds and the same-numbered
formal release. While an open request covers a package at a version, the update
pipeline must not advance that package past it.

**Before touching any PKGBUILD, confirm the `mod-request` skill is loaded.** If
it is not in the current session's available skill list, read
`.agents/skills/mod-request/SKILL.md` and follow it — do not reconstruct the
issue format from memory. The format is defined by exactly one implementation,
`scripts/mod-request.py`; render, parse, hash and validate with that
script, and never hand-write the body.

Non-negotiables (the skill has the full list):

- `target_version` is a base semver; `1.20.0pre1` and `1.20.0` are one family.
- The affected packages are not listed: `type` × `variant` × `edition` are three
  multi-select dimensions resolved against the packages that exist in `aur/*`.
  The channel is implicit, so a selection covers the stable and `-pre` twins
  together and needs a diff for each of them.
- An approval binds what CI acts on and what lands in AUR: the three selections,
  `target_version`, `next_update`, `diff_state`, `supersedes`, and every payload
  (diff + message). `summary` and `evidence` are outside the hash and may be
  edited without re-approval — they are never applied anywhere.
- No backport: a consequence of AUR's single branch (see AUR remoteness), not a
  policy here — the only tip that exists is the current one, so a change can
  never be aimed at an older published version.
- `next_update` holds a request back from the version already published: it
  applies only in a run that moves that package's version.
- Immediate or deferred is decided per package, not by the approval: `ready` with
  `next_update` unticked applies to the published version as a `pkgrel` bump the
  moment it is approved (the approval fires `update-aur-approval.yaml`, which
  runs the pipeline against the published versions), while `pending`,
  `next_update`, or a target the packages have not reached wait for the run that
  moves the version.
- A request diff is package-relative and may touch any file of that AUR package,
  except `.SRCINFO` (CI generates it) and `.nvchecker.toml` (version source of
  truth). It must not touch `pkgver=`, `pkgrel=`, git plumbing, or create
  symlinks/submodules — CI owns the first four.
- A further change to the same version edits the existing issue in place
  **unless it has already been applied to at least one package** — then it is a
  new issue, and an old issue's progress list is never reused.
- The record is one comment per issue, rewritten once per run that applies
  changes to it: an HTML comment on the first line carrying the record's data and
  its SSH signature, one visible progress line under it (the target base
  version), then the whole selection as a tasklist — the summary names the base,
  so an applied row carries the full version it landed in and an unapplied row
  carries none:
  ``<!-- mr-applied: {…} -->``, ``**Applied at v1.20.0** — 1 of 2 packages``,
  ``- [x] `bettbox` — 1.20.0-1``, ``- [ ] `bettbox-pre` ``. All three parts are
  the record: the progress line and the tasklist are the signed `applied` map
  rendered for reading, so editing a tick does not change what the record says.
  The signed data is readable: `{kind, issue, version, applied}` where `applied`
  maps a package to `{at, full}` — when it was applied and the `pkgver-pkgrel`
  it published — plus an SSH signature (`sig`) over exactly those fields. It is
  signed with the AUR SSH key CI already pushes with (`mod-request.py token`,
  which reads the comment already there and re-signs it together with this run's
  package; private key default `~/.ssh/aur`, `MR_APPLIED_KEY_FILE` to point
  elsewhere) and verified against the public half committed at
  `bettbox-aur-updater.pub`. The asymmetry is deliberate: a tick
  anyone can type proves nothing, and any reader — including an agent following
  the skill — can check a real record with `mod-request.py applied`, no
  secret required. The gate reads the signature: it must verify, name the issue
  and version it was found on, and name a package that request covers. Signing
  authenticates, it does not hide — the claim is readable, and a record pasted
  onto another issue proves nothing.
- A new issue of that kind names the one it replaces in `supersedes` (`#12`,
  one canonical spelling; an empty field means "no relationship"). The claim is
  checked with `supersede` (same `target_version`, package set covered) and the
  replaced issue must already carry such a record — otherwise nothing has been
  applied yet and the body should have been edited in place. The replaced issue
  is closed when the replacement is accepted, not when CI applies it.
- The private key is a credential, not a convenience: it already is the push
  credential, so losing it is losing both. The committed public key is what
  makes a record checkable; without it (or without `ssh-keygen`) the gate
  refuses (fail closed) rather than trusting the comment. Verification never
  needs the private key, so an auditor can reproduce CI's decision from a plain
  clone. Rotating the AUR key means updating `bettbox-aur-updater.pub` in the
  same commit — `token` self-checks against it and refuses to sign with a key
  the repository does not carry.
- The claim is only as good as the check behind it: `scripts/mod-request.py
  gate` is the single evaluator of it, and CI must run it before applying,
  failing closed rather than trusting the body or the approval. The package-set
  linearity the gate enforces is then trusted at apply time, not re-checked.
- One live request per package set: a diff is defined against one tree state,
  and the format has no way to say "on top of another request" — two approved
  requests sharing a package leave the second one's base ambiguous, and
  whichever ordering a run picks, one diff lands on a tree its author did not
  validate (hunk conflicts, or a combination nobody built). The approval gate
  refuses the second request for the same package set — any version, any
  `diff_state`, a pending placeholder included — until the first has landed
  and closed, or is named in `supersedes`. The version is not part of the key:
  two requests for the same version with disjoint selections never meet in one
  tree and stay parallel. The apply pass re-runs the gate and maps each package
  to its one live request, trusting the gate's linearity.
- CI owes the record back: the per-package loop only records which packages it
  applied, and after the loop — every push done — CI runs
  `mod-request.py token --issue … --body … --comments … --pkg <pkg>=<pkgver>-<pkgrel>`
  (one `--pkg` per package of that run, read from the PKGBUILD it just pushed)
  once per issue, then POSTs the result as the issue's record comment or
  PATCHes the one already there.
  No record means the next change to that version is expected to edit the issue
  in place, so a missing write silently breaks the replacement path.
- A request is also bounded by what an issue can hold: the body must fit in
  GitHub's 65536-character limit (`check`/`render` refuse anything larger) and
  `summary`/`evidence` must not contain a line starting with `### ` or a 4+
  backtick fence, which would split or swallow the sections after it.
- "No backport" is a CI obligation, not something the offline gate can see: a
  request whose `target_version` is below what a package already publishes must
  never be applied to it. CI compares the request's target with the package's
  current version and skips (or fails) rather than moving it backwards.
- That version rule lives in exactly one place, `scripts/apply-mod-request.sh`:
  the package must have reached `target_version` (or be moved to it by the run),
  must not be newer, and a `next_update` request is not re-applied to the version
  already published. `--force` ignores it and is **manual only** — CI calls the
  script in live mode (no body, no issue) with the rule on and never passes it.
  Running the script with `--versions FILE` rehearses against a run's version
  vector; without it each package is taken to stay on its PKGBUILD `pkgver`.
- The agent files and audits requests; it does not decide whether one may be
  applied, and author identity grants no privilege. This section describes the
  request format only.

**Repo setup (one-time, maintainer):** nothing to do by hand — the only label,
`approved`, is created by `scripts/approve-mod-request.sh` on first use (with
`--force`, so re-running is safe). A request is recognised by its body, so a
third party files one with plain `gh issue create` or the Issue Form; labels need
triage+ to touch, so only a maintainer can approve.

## Conventions

- Commits use conventional prefixes: `Update to v<ver>`, `fix: force update...`, `fix: sync all packages to...`, `feat: add packages...` (new packages/submodules), `chore: sync AUR submodules to latest` (pointer sync only). A request-only run uses `fix(<pkg>): apply mod request #<n> at v<ver>` for one package, `fix: apply mod requests #a, #b at v<ver>` when they share a version, and `fix: apply mod requests #a, #b` otherwise.
- First commits of brand-new AUR packages use `Init <pkgname> <ver>` (e.g. `Init bettbox-pre 1.19.4pre1`).
- `.gitignore` excludes `*.deb`, `*.pkg.tar.*`, `pkg/`, `src/` (build artifacts).
- Package version is always upstream tag minus `v` prefix. `pre` versions use `pre` (not `-pre`) in pkgver; `_pkgver` converts to `-pre` for the tarball URL.

## Local-only docs

`.localdocs/` is the gitignored home for machine-local working notes that must never enter the repository. Keep anything you don't want tracked under `.localdocs/`.
