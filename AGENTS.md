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

- Stable channel tracks the latest non-pre release (`v1.19.3`); pre channel tracks the highest `-pre` release (`v1.19.4-pre1`). The channels are **independent series** — a pre bump never touches the stable packages and vice versa. Pre channel has 4 packages, stable 3 (`bettbox-pre-bin` is pre-only because the stable slot `bettbox-bin` belongs to another maintainer).
- pkgver never contains a hyphen (makepkg rejects it): pre versions are spelled `1.19.4pre1`. `_pkgver="${pkgver/pre/-pre}"` re-inserts the hyphen for the tarball URL (`archive/v1.19.4-pre1.tar.gz`) and the extracted source dir.
- Source PKGBUILDs select `APP_ENV` from the version: `local app_env=stable; [[ "${pkgver}" == *pre* ]] && app_env=pre`.
- Each package carries its own `.nvchecker.toml` (GitHub releases API — drafts and tag-only releases are excluded; junk tags like `v1.19.2-test` never surface):
  - stable: `use_max_release = true` + `include_regex = '^v\d+\.\d+\.\d+$'` + `prefix = 'v'` → target `1.19.3`
  - pre: `use_max_release = true` + `include_prereleases = true` + `include_regex = '^v\d+\.\d+\.\d+-pre\d+$'` + `from_pattern`/`to_pattern` (`\1pre\2`) → target `1.19.4pre1` (no `prefix`)
  - The GitHub token goes through a keyfile in CI (`[keys] github = ...`).
- The pre-bin `.deb` asset is cached under `bettbox-compatible-<ver>-x86_64.deb` (URL built from `_pkgver`, keeps the `compatible` name); the pre source tarball downloads from `archive/v1.19.4-pre1.tar.gz`.

## CI workflows

Job overview, `force` / `dry_run` inputs and the check → update → parent-pointer flow are in [README.md](README.md). Operational details an agent needs on top:

- **`update-aur.yaml`** — per-package loop resolves the target via that package's `.nvchecker.toml`, skips unless the target changed **or** `force`, then patches pkgver/pkgrel → `updpkgsums` → `makepkg --printsrcinfo` → commit + `git push origin HEAD:master`.
  - Parent-pointer subject on a real bump follows per-channel inconsistency: `fix: sync all packages to v<ver> in <stable|pre> channel` / `fix: sync all packages to v<ver>` / `Update to v<ver>`; with `force`: `fix: force update source hashes of v<ver>`.
  - `dry_run` previews PKGBUILD/.SRCINFO diffs and skips the pointer steps entirely.
- **`sync-from-aur.yaml`** — manual reverse sync (AUR → parent); parent-pointer commit is `chore: sync AUR submodules to latest`.

## Key behaviors an agent must know

- `.SRCINFO` is **always** regenerated via `makepkg --printsrcinfo`, run as the `builder` user inside the container job. Never hand-edit `.SRCINFO`.
- Checksums are refreshed by `updpkgsums`, which downloads every source (including the `sha256sums_x86_64/_aarch64` arrays) and is idempotent for the local hook/desktop files.
- Updates are atomic **per channel** (3 stable packages, 4 pre packages): if one package in a channel is behind, the whole channel gets bumped.
- The `force` input bumps `pkgrel` while keeping the version — used to push a checksum/hash refresh without a version change (`fix:` prefix, body lists nothing since no versions moved).

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

Only one maintainer edits the AUR packages, and changes come from exactly two sources: pushes from this repo and the CI `update-aur` workflow. AUR `master` therefore only ever advances linearly — an AUR push is always a fast-forward with no reconcile/force needed.

## Local changes without update-pkgver.sh

The per-package `update-pkgver.sh` scripts are deprecated and gone — the version/checksum/.SRCINFO pipeline lives entirely in `update-aur.yaml`. To preview or push a refresh without a version change, run `update-aur.yaml` via `workflow_dispatch` with `force=true` (add `dry_run=true` to preview first). Direct submodule edits (e.g. a release-critical fix) still go through: edit → commit inside `aur/<pkg>` → `git push origin HEAD:master` (detached-HEAD safe, see above) → parent pointer via `sync-from-aur.yaml` — see "Syncing a local submodule change".

## Manually editing AUR packages

Routine version bumps go through CI; any manual change follows **one uniform flow**: edit & push to AUR → run `sync-from-aur` → pull the parent repo.

**Local environment prep** (before any edit):

- **Fetch AUR origin first and check for drift** — `git -C aur/<pkg> fetch origin` (AUR is the submodule's origin). Compare AUR HEAD with the parent's recorded pointer: `git rev-parse HEAD:aur/<pkg>` vs `git -C aur/<pkg> rev-parse origin/master`. If they differ, the parent is behind AUR — **trigger `sync-from-aur.yaml` before editing**, so you start from the current AUR state instead of a stale pointer.
- Work in a clean `aur/<pkg>` worktree (or a separate AUR clone). Submodules sit in detached HEAD — that's fine for edits: commit and push while detached (`git push origin HEAD:master`), no branch switch needed; if not checked out, `git submodule update --init aur/<pkg>`. Mechanics in "Submodules run detached HEAD".
- Tools (Arch host or container, run as **non-root** user): `makepkg` (pacman) and `updpkgsums` (pacman-contrib) — makepkg refuses to run as root; `bash -n` needs only bash. CI does the same work as the throwaway `builder` user inside the container.
- Publishing needs the AUR SSH key + keyscanned known_hosts in `$HOME/.ssh` (`aur`, `known_hosts`) — the material CI provisions in Job container; locally it must live in your own `$HOME/.ssh`.
- Keep the channel consistent: pkgver spelling and `APP_ENV` follow the package's `*-pre` suffix — see [Version model](#version-model).

**Edit + push to AUR:**

1. Edit the PKGBUILD — either in a separate AUR clone, or inside `aur/<pkg>` (mechanics in "Submodules run detached HEAD").
2. If `source=` changed, refresh hashes with `updpkgsums` (re-downloads every source incl. the arch-specific arrays, rewrites `sha256sums`).
3. Regenerate metadata: `makepkg --printsrcinfo > .SRCINFO` — never hand-edit `.SRCINFO`.
4. Sanity-check: `bash -n PKGBUILD`.
5. Commit with a conventional message and push to AUR (`git push` from a separate clone, or `git -C aur/<pkg> push origin HEAD:master`).

**Sync the parent pointer — always `sync-from-aur`, never a manual parent commit:**

- Run `sync-from-aur.yaml` (manual workflow_dispatch): it fetches AUR HEAD into the submodules, commits `chore: sync AUR submodules to latest` on the parent (body lists each changed `aur/<pkg>` with its `git log --oneline`), and pushes it. No-op when the pointers already match.
- Then pull the parent repo locally: `git pull` — the pointer commit was pushed by the workflow, not by your clone (see "Syncing a local submodule change").

## Conventions

- Commits use conventional prefixes: `Update to v<ver>`, `fix: force update...`, `fix: sync all packages to...`, `feat: add packages...` (new packages/submodules), `chore: sync AUR submodules to latest` (pointer sync only).
- First commits of brand-new AUR packages use `Init <pkgname> <ver>` (e.g. `Init bettbox-pre 1.19.4pre1`).
- `.gitignore` excludes `*.deb`, `*.pkg.tar.*`, `pkg/`, `src/` (build artifacts).
- Package version is always upstream tag minus `v` prefix. `pre` versions use `pre` (not `-pre`) in pkgver; `_pkgver` converts to `-pre` for the tarball URL.

## Local-only docs

`.localdocs/` is the gitignored home for machine-local working notes that must never enter the repository. Keep anything you don't want tracked under `.localdocs/`.
