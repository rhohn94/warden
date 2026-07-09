---
name: project-release
description: Promote dev to main and tag a new version. Use when the user wants to cut a release, tag a version, "release dev to main", or any release/versioning workflow. Required reading before running any release commands.
---

# Project release procedure

Promotes accumulated `dev` work to `main` and tags a version. Full branching
and versioning conventions live in `docs/version-design.md` — read it before
acting. This skill is a checklist.

## Critical preflight (most-violated rules)

1. **`version-history.md` entry must exist on `dev` BEFORE running the
   release command.** Most release scripts verify this. Format: one heading
   per release + 3–8 bullets aimed at end users, not developers. Commit on
   `dev`.
2. **Feature-manifest entries written for each new flagship capability.**
   For every new capability shipped in this release that a downstream project
   would need to adopt or configure after syncing, add an entry to
   `.claude/skills/sync-from-upstream/feature-manifest.md` (fields:
   `feature-id`, `introduced-in: vX.Y`, `summary`, `detect`, `adopt`,
   optional `migrate`). Same discipline as writing the `version-history.md`
   entry. A capability is manifest-worthy if it has an idempotent `adopt`
   step and did not exist in prior releases. Pure internal refactors do not
   require an entry. Commit the manifest update on `dev` alongside the
   version-history entry.
3. **Merge `dev` → `main` first, then check out `main`, then run the release
   script.** Releases only happen from `main`.
4. **Pick the version:** MINOR for ordinary releases, MAJOR for breaking
   changes.

## The recipe

```bash
{release-command}   # e.g. `npm version minor`, `make release`, `just release X Y`
```

Replace `{release-command}` with your project's actual release tooling (see
CLAUDE.md §Project commands and `docs/version-design.md` §Release procedure).

A well-written release command should:
- Verify you are on `main` with a clean tree and an unused tag.
- Bump the version in the project's version file and any lockfiles.
- Run the test suite.
- Build release artifacts.
- Archive or publish artifacts.
- Commit the version bump.
- Tag the commit `v{MAJOR}.{MINOR}` (or your project's tag convention).

## Emit the per-run telemetry artifact (best-effort, v3.14 #82)

Immediately **after the version tag is created** (and before the human-gated
push below), emit one per-run metadata artifact for the release (consumer:
Mission Control's Pulse pillar). This is **write-only dev telemetry** — it
writes one gitignored JSON file under `.claude/cache/runs/<run_id>.json` and
**never gates, blocks, or rolls back the release**. Run it best-effort:

```bash
python3 .claude/skills/token-measure/run_metadata.py --emit \
  --outcome pass \
  --release {MAJOR}.{MINOR} \
  --config .claude/grimoire-config.json \
  --items <work-items in this release> --items-passed <items that passed> \
  --started-at <ISO-8601 start of the release run> \
  --wall-clock-secs <run seconds, if known> \
  [--transcript <session .jsonl, if known>] \
  || echo "run_metadata: emit skipped (telemetry is best-effort)"
```

- `--outcome` is `pass` on a clean tag. `paradigm` / `profile` / `model`
  auto-fill from `grimoire-config.json` via `--config`.
- **Graceful by contract:** absent inputs degrade to null/zero, a missing
  transcript yields all-zero tokens, the helper never raises out of `--emit`,
  and the `|| echo` swallows any non-zero exit — the release is never blocked
  by telemetry. (On Copilot the helper lives at `scripts/run_metadata.py`.)

## Push to origin (post-release)

A completed release is the **single** trigger moment for the integration
master to push to origin — once per release. (`release-phase-merge` no longer
pushes; `dev` stayed local through integration.) Outside this moment, do not
push.

1. Inspect what's ahead of origin on both branches and the tag:
   ```bash
   git fetch origin
   git log --oneline origin/dev..dev
   git log --oneline origin/main..main
   git tag --contains origin/main..HEAD
   ```
2. Propose the push and wait for explicit user confirmation, then push `dev`,
   `main`, and the version tag **together** in one go:
   ```bash
   git push origin dev main --follow-tags
   ```

The `push-guard.sh` hook permits `main`, `dev`, and version tags by default
(see `.claude/push-allowlist` for project additions); pushing requires the
integration marker. Destructive flags are denied — see `push-guard.sh`. This
push remains human-gated and marker-gated.

## GitHub Release (authoritative — the distribution artifact, v3.23; channels + signing, v3.27; canonical tarball + `release.json`, v3.29)

The GitHub Release is the **authoritative distribution point** downstream
consumers depend on (`docs/design/release-distribution-design.md`): every release
**always** produces one, it carries the `version-history` notes, and it attaches
the **per-flavor `.zip` distributables**, the **canonical `grimoire-v{X.Y}.tar.gz`
primary artifact**, the generalized **`release.json`** manifest, plus the
**signing assets** (`SHA256SUMS` always; `SHA256SUMS.minisig` when signing is
configured). It runs at the same single post-release moment as the push — no
longer optional. It degrades only when `gh` is genuinely unavailable, and then
**loudly** (skipping it means downstream has no artifact for this version, and
release-transport consumers fall back to git clone).

**`release.json` (v3.29).** The single manifest / channel-of-record — the
generalized, kind-discriminated manifest (`schema_version`, `name`, `version`,
`channel`, nullable `git_sha`, `artifact_kind`, `primary_artifact` +
`primary_artifact_sha256`, `signature` = null, `assets[]{name,sha256,bytes}`;
schema in `dependency-channel-design.md` §2). It **replaces** the retired
`RELEASE-META.json` (a strict superset). The framework builder always emits
`artifact_kind: asset-bundle` with the canonical `.tar.gz` as the primary
artifact. `SHA256SUMS` now also covers `release.json`.

**Channels (v3.27).** Pick the channel for this release:
- **stable** — the normal `v{MAJOR}.{MINOR}` release on `main`. Default.
- **beta** — a `--prerelease` published off the `version/{X.Y}` staging branch
  (reuse the existing staging convention; no new branch types). Beta assets carry
  a `-beta` filename suffix and `release.json` records `channel: beta`.

Consumers pin a channel with `UPSTREAM_CHANNEL=stable|beta` in `sync-from-upstream`.

1. **Build the distributables + signing assets** from the repo root:
   ```bash
   python3 .claude/skills/project-release/build_distributables.py \
     --version {MAJOR}.{MINOR} --channel stable   # or --channel beta
   # → dist/grimoire-<flavor>-v{MAJOR}.{MINOR}[-beta].zip … plus the canonical
   #   dist/grimoire-v{MAJOR}.{MINOR}[-beta].tar.gz, dist/release.json,
   #   dist/SHA256SUMS, and (if signing) dist/SHA256SUMS.minisig
   ```
   Flavors are auto-discovered (any top-level dir carrying a `.grimoire-flavor`
   marker), so future flavors are included automatically. The build is
   deterministic; `dist/` is gitignored. Run `--self-test` to verify the helper.
2. **Signing contract.** `SHA256SUMS` is **always** emitted (the integrity
   floor). The `minisign` signature `SHA256SUMS.minisig` is emitted only when the
   `minisign` tool is on `PATH` **and** `MINISIGN_SECRET_KEY` names a key file;
   when either is absent the builder prints a **loud** unsigned-build warning and
   proceeds — never a silent skip. To sign, install `minisign` and export
   `MINISIGN_SECRET_KEY=/path/to/key` (the public half is embedded in consuming
   apps at build time, never fetched at runtime — see the design doc).
3. **Extract notes** for the version from `docs/version-history.md` (the 3–8
   user-facing bullets) into a temp file for `--notes-file`.
4. **Create the Release from the tag, attaching every built asset** (zips +
   the canonical `.tar.gz` + `release.json` + `SHA256SUMS` + `SHA256SUMS.minisig`
   when present). Add `--prerelease` for the beta channel and the channel label:
   ```bash
   gh release create v{MAJOR}.{MINOR} \
     --title "v{MAJOR}.{MINOR}" \
     --notes-file <extracted-notes> \
     dist/*                                  # stable
   # beta: gh release create v{MAJOR}.{MINOR}-beta.N --prerelease --title … dist/*
   gh release edit v{MAJOR}.{MINOR} --add-label channel:stable   # or channel:beta
   ```
5. **Degrade loudly.** If `gh` is absent/unauthenticated, do **not** silently
   skip: report that the tag shipped WITHOUT a published Release+assets, that
   downstream `UPSTREAM_TRANSPORT=release` consumers will fall back to git clone
   until it is published, and keep the built `dist/*` (reproducible from the
   tag) so they can be attached later.

Like the push, this is integration-master-only and runs at the single
post-release moment. Under `autonomous-push.enabled` it proceeds unattended
alongside the push; otherwise it is part of the same human-gated moment.

## Post-release cleanup (final ordered step)

After the tag is created and the human-gated push completes, run the
post-release branch + worktree cleanup as the **final ordered step** of the
release. Only the **marker-blessed integration master** runs this (it is the
only actor permitted to touch sibling worktrees). This step is governed by
the existing protocol in `docs/integration-workflow.md` §Dead-worktree
cleanup → §Post-release cleanup step — read it and follow it exactly; the
summary below does not override it.

For **each** work-item branch/worktree of the just-shipped release:

1. **Verify dead-ness** — the branch is merged (an ancestor of the release
   tip) AND the worktree is clean. Skip any branch that is not both.
2. **Preserve or explicitly report** any uncommitted work (stash or patch per
   the protocol) — never discard silently.
3. **Unlock** any locked worktree, then `git worktree remove <path>` (it
   refuses on a dirty tree — the safety net). `--force` is allowed **only**
   for disposable untracked artifacts (e.g. `__pycache__`) **and only with an
   explicit logged note** of what is discarded — never a silent `--force`.
4. `git worktree prune`, then `git branch -d` the merged feature branches and
   any stale `worktree-*` placeholder branches (merge-safe `-d`; `-D` only
   with explicit user confirmation).

Report the tally: worktrees removed, branches deleted, work preserved/skipped.

## Reminders

- After release, new work-item worktrees must still root on `dev`, never on
  `main`. See `worktree-preflight` skill.
- If the recipe fails mid-run, stop and read the error. Do not retry blindly —
  partial state (bumped version files, dangling tag) may need cleanup first.

## Anti-patterns

- Shipping an adoptable capability without a manifest entry — old projects that
  sync will silently miss it and receive no adoption prompt; the capability
  lands inert. When in doubt, add the entry (false positives are filtered by
  `detect`).
