---
title: Conventional Commits Guide
sidebar_label: Commit Convention
description: How to write Conventional Commits for automated changelog generation
tags: [commits, conventions, internal]
audience: [contributors, maintainers]
version: v2.9.0
lang: zh
---

# Conventional Commits Guide

This project uses [Conventional Commits](https://www.conventionalcommits.org/) to standardize commit messages and enable automated changelog generation. All commits must follow this format to pass CI validation.

## Commit Message Format

```
type(scope): description

[optional body]

[optional footer(s)]
```

### Type

The type specifies the category of change. Must be one of:

- **feat**: A new feature or capability
- **fix**: A bug fix
- **docs**: Documentation changes only (README, guides, architecture docs)
- **style**: Code style changes without functional impact (formatting, linting)
- **refactor**: Code refactoring without feature changes or bug fixes
- **perf**: Performance improvements
- **test**: Adding or updating tests
- **build**: Changes to build configuration, Dockerfile, or dependencies
- **ci**: Changes to CI/CD configuration or workflows
- **chore**: Other changes that don't modify code or docs (version bumps, licenses)
- **revert**: Reverts a previous commit

### Scope

Scope is **optional** but if provided **must** be from the allowed list (`scope-enum` level 2). Scopeless commits like `chore: ...` / `ci: ...` are accepted. **Source of truth**: `.commitlintrc.yaml` `scope-enum`.

**Design invariant**: scope = 「PR 動到的產品介面 / 長期跨切關注點」。Initiative / development phase / 版號 **不放 scope**（用 PR labels、branch name、或 `chore(release)` 表達）。Compound scope（如 `dx+e2e`）違反 conventional commits 的 one-scope 原則 — 拆 commit 或選主導 scope。

**Allowed scopes (full list, mirrors SOT)**:

Deliverable artifacts:
- **exporter**: threshold-exporter Go binary（NOT `threshold-exporter` — 寫 verbose name 會被 commitlint reject）
- **tenant-api**: tenant-api Go service
- **portal**: da-portal（JSX 工具集 host）
- **tools**: da-tools CLI / `scripts/tools/`
- **rule-packs**: rule pack content
- **helm**: Helm chart
- **k8s**: Kubernetes manifests / deployments

Cross-cutting concerns:
- **docs**: documentation
- **ci**: GitHub Actions / pipeline
- **e2e**: Playwright suite
- **jsx**: JSX 工具技術面（`_common/`, jsx-loader, 共用 hooks 等）
- **a11y**: accessibility (WCAG / axe-core)
- **dx**: developer-experience tooling
- **ops**: operational scripts / runbooks
- **lint**: linter scripts / pre-commit hooks

Doc sub-categories (高頻使用，故給獨立 scope):
- **audit**: security / drift / health audit 報告
- **playbook**: `docs/internal/*-playbook.md` 系列

Release wrap:
- **release**: `chore(release): vX.Y.Z` 版本發布 commit（取代過去 `chore(v2.7.0)` 那種版號式 scope）

> **Common pitfall**: typing the verbose component name (`threshold-exporter`) instead of the short scope (`exporter`). commitlint will reject; copy the allowed scope from the CI failure message or `.commitlintrc.yaml`.

### Description

A brief, imperative description of the change (lowercase, no period):

- ✅ "add validation for tenant keys"
- ✅ "fix cardinality limit check"
- ❌ "Added validation"
- ❌ "Fixed cardinality limit check."

## Footer / Trailers

Optional trailers go on separate lines after a blank line below the body. Use them for tracking-system links and authorship.

### Planning ID trailer (`Resolves TRK-NNN`) — v2.8.1+ namespace policy

When a commit resolves a registered tracking item, include a trailer naming the ID:

```
Resolves: TRK-228
Closes: TRK-103
Fixes: Trap #12
```

⛔ **The colon is load-bearing.** `check_planning_status_sync.py` reads these with `git log --format='%(trailers:key=Resolves,…)'`, and git only recognises `Key: value` lines as trailers. A colon-less `Resolves TRK-228` is not merely ignored: sitting in the last paragraph it makes git treat the **whole** paragraph as prose, so `Refs:` / `Self-Review-Pass-2:` / `Co-Authored-By:` next to it vanish too (both `git interpret-trailers --parse` and `%(trailers)` return nothing).

The verb (`Resolves` / `Closes` / `Fixes` / `Fix`) is **case-insensitive**; the ID itself must be `\b`-bounded so that `TRK-1` does not eat `TRK-100`. CI's `check_planning_status_sync.py` (chunk 2b, pending) verifies that the matching `frontmatter status:` flips to `done` and the `pr_ref:` field is populated in the same PR.

**Namespace rules (effective v2.8.1, per [ADR-019 §Namespace Policy](../adr/019-planning-ssot.md#namespace-policy三-namespace-共存)):**

- **`TRK-NNN`** is the **only** namespace for new tracking items — unifies the legacy `TECH-DEBT-NNN` / `TD-NN` / `HA-NN` / `REG-NN` four-way split. Numeric ranges encode the source namespace (see [`planning-id-mapping.md`](planning-id-mapping.md) §編號分區).
- Legacy IDs in old commit messages / external citations still work during the transition window — CI auto-translates via the mapping doc but emits a warning. New commits must use `TRK-NNN`.
- **`ADR-NNN`** (architecture decision history) and **`S#NNN`** (sprint ledger) remain independent namespaces — referencing them in commit body is informational, **no auto-close behaviour** (they are not backlog items).

**Edge cases that do not need a trailer:**

- Pure refactors / pure docs touch-ups not tied to a registered TRK item
- Cross-cutting cleanup spanning many TRK items (list the IDs in prose in the body instead)
- Hot-fix / revert commits (refer back to the original PR / commit hash)

> **See also:** [`dev-rules.md` §P1](dev-rules.md) for the rationale (why the trailer matters for `status:` ↔ git-log linkage) and the frontmatter spec the trailer is paired with.

### GitHub issue auto-close

GitHub itself parses these verbs against `#NNN` style references to auto-close issues on PR merge:

```
Fixes #228
Closes #242
```

That bare form is for PR bodies and commit-body prose. GitHub also accepts the colon form (`Fixes: #228`), and **inside a commit's trailer paragraph the colon form is the only safe one** — a bare `Fixes #228` line there voids the whole block (⛔ above).

This is **orthogonal** to the TRK trailer above — issues live in GitHub; TRK items live in the repo. A PR commonly carries both:

```
Resolves: TRK-228
Fixes: #242
```

⛔ **GitHub's parser reads adjacency, not meaning.** A closing verb next to `#NNN` anywhere in a PR body or commit message links the issue for auto-close — including a sentence that *negates* it, quotes it, or explains this very trap. To say "this PR does not close N", write `Refs: #N` and name the verb without the number beside it. After opening a PR or editing its body, check the link list: `gh pr view <N> --json closingIssuesReferences`.

### Trailer block traps

- **Every line after the header is capped at 100 characters** by the local `commit-msg` hook (`pr_preflight.py`, `POST_HEADER_MAX_LINE_LENGTH`) — trailers included, independent of commitlint's own body limit.
- **A body line that starts with `---` is a patch divider to `git interpret-trailers`**: everything after it, trailer block included, is dropped from `--parse` output (`git log --format='%(trailers)'` still sees it, so the two readers disagree). Do not start a body line with `---`.
- **One contiguous last paragraph, all `Key: value`.** A blank line inside it, or one line without a colon, splits or voids the block (see the colon note above).

### Co-Authorship

For paired / agent-assisted work:

```
Co-Authored-By: Name <email>
```

Example complete commit message:

```
fix(exporter): tighten cardinality guard for nested group_left joins

Adds an upper bound on cross-product cardinality during PromQL rule
evaluation so that misbehaving rule packs cannot OOM the exporter.

Resolves: TRK-228
Fixes: #242
Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

## Common Scenarios

### Adding a Feature

```
feat(exporter): add support for regex dimension thresholds

This enables the use of `=~` operator in threshold dimensions
for matching multiple label values with a single rule.
```

### Fixing a Bug

```
fix(tools): correct migration rule validation for nested scopes

The regex pattern was not escaping special characters in label names.
```

### Updating Documentation

```
docs(getting-started): clarify N:1 tenant mapping examples

Added concrete examples for routing configuration in multi-namespace scenarios.
```

### Dependency or Build Changes

```
build(deps): update prometheus client library to 1.17.0
```

### CI/CD Pipeline Changes

```
ci(workflows): add commitlint validation on pull requests
```

## Breaking Changes

For commits that introduce breaking changes, add `BREAKING CHANGE:` in the footer:

```
feat(exporter)!: remove deprecated _silent_mode_expires field

BREAKING CHANGE: The _silent_mode_expires field is no longer supported.
Use recurring maintenance windows instead.
```

Or use the `!` syntax:

```
refactor(tools)!: rename shadow_monitoring to advanced_monitoring

BREAKING CHANGE: All references to shadow_monitoring must be updated to advanced_monitoring.
```

## Mapping to CHANGELOG

Conventional Commits are automatically parsed to generate the CHANGELOG:

| Type      | CHANGELOG Section      | Example |
|-----------|------------------------|---------|
| `feat`    | Features               | "Add validation for tenant keys" |
| `fix`     | Bug Fixes              | "Fix cardinality limit check" |
| `perf`    | Performance            | "Optimize alert routing performance" |
| `docs`    | Documentation          | "Clarify N:1 tenant mapping examples" |
| `revert`  | Reverts                | "Revert unsafe threshold change" |

Breaking changes (marked with `!` or `BREAKING CHANGE:`) appear prominently at the top or in a dedicated section.

Other types (`style`, `refactor`, `test`, `build`, `ci`, `chore`) are grouped and may be collapsed in the CHANGELOG.

### Editing `CHANGELOG.md` by hand

- **A rebase can silently drop or duplicate a bullet with zero conflict markers** — equal bullet *counts* hide it. After any rebase that touched `CHANGELOG.md`, compare bullet **sets** against the oracle `expected = main ∪ (mine − base)` (`base` = the fork point before this rebase). `mine ∪ main` is the wrong oracle: it reports bullets that upstream legitimately rewrote as "missing". A union-style conflict resolution errs the other way — it keeps both the old and the rewritten text of one bullet — so look for extras, not only losses. Then run `python3 scripts/tools/dx/bump_docs.py --sync-counts --check`: two PRs that each bumped the same count rebase cleanly into a wrong number.
- **An edit at a section boundary can swallow the next `### heading`** (the last bullet under `### Added`, right above `### Fixed`): every entry below then files under the wrong section, and nothing is red. After editing, list the headings: `sed -n '/^## \[Unreleased\]/,/^## \[v/p' CHANGELOG.md | grep '^###'` (keep both `^` anchors: bullets and the placeholder comment quote those headings mid-line, and an unanchored range stops at the first quote).
- **Links from `CHANGELOG.md` to anything outside `docs/` use the absolute GitHub URL.** The mkdocs strict gate exempts only `CHANGELOG.md` → `docs/<…>.md` links (`mkdocs_strict_check.sh`); a link to a non-`.md` file under `docs/`, or to `helm/`, `scripts/`, `try-local/`, fails `MkDocs Build Verification`.

## CI Validation

All pull requests are automatically validated by GitHub Actions (`.github/workflows/commitlint.yaml`).

**Two independent jobs run on every PR — both must pass:**

| Job | What it lints | When it triggers |
|---|---|---|
| `Validate PR title (for squash-merge)` | The PR title (becomes the squash-merged commit message on main) | Every `pull_request` open / edit / synchronize / reopen |
| `Validate commits on PR` | **Each individual commit** on the PR branch (`base.sha`..`head.sha`) | Same triggers |

> **Pitfall (codified after PR #147)**: editing only the PR title fixes job 1 but **not** job 2. If the underlying commits have a bad scope, you must amend (or soft-reset + recommit) the commit messages and force-push. The branch's commit history is its own SOT — the PR title is just the squash-merge message. Both surfaces validate, neither inherits from the other.

The validation enforces:
- Valid type from the approved list
- Valid scope from the `.commitlintrc.yaml` `scope-enum` (or none)
- Non-empty description

If validation fails, the CI check will block merging until both surfaces are corrected.

## Fixing Invalid Commits

If your commits don't pass validation, you can:

1. **Amend the last commit**:
   ```bash
   git commit --amend
   git push --force-with-lease
   ```

2. **Interactive rebase** to fix multiple commits:
   ```bash
   git rebase -i origin/main
   git push --force-with-lease
   ```

3. **Update PR title** (squash-merge surface):
   - Edit the PR title on GitHub — `Validate PR title` job re-runs automatically
   - **This does NOT fix the per-commit job** if the underlying commits also have a bad scope. You must also amend / rebase the commit messages and force-push (steps 1 / 2). Both surfaces validate independently.

4. **Soft-reset + recommit** (when amend hangs in pre-commit; FUSE escape pattern):
   ```bash
   # Files unchanged from prior commit — sandbox hooks already verified them
   git reset --soft HEAD~1
   # Use the project's Windows escape so commit-msg passes UTF-8 cleanly
   scripts/ops/win_git_escape.bat commit-file _msg.txt
   git push origin <branch> --force-with-lease
   ```

## Examples for This Project

### Platform Engineer adding a feature

```
feat(helm): add support for custom alertmanager retention policy

Allows tenants to specify custom retention periods for alert history
through the tenant YAML configuration.
```

### Fixing a rule pack bug

```
fix(rule-packs): correct JVM memory usage threshold in gc-duration rule

The previous threshold was triggering false positives during normal GC cycles.
Changed from 95% to 88% to align with production observations.
```

### Documentation update

```
docs(architecture-and-design): add federation scenario examples

Expanded §2.11 with three concrete multi-cluster federation patterns
matching common deployment topologies.
```

### Dependency update

```
build(deps): bump kind cluster version to 1.29.1
```

## 相關資源

暫無相關資源。


- [Conventional Commits Specification](https://www.conventionalcommits.org/)
- [commitlint Documentation](https://commitlint.js.org/)
- CHANGELOG.md - Auto-generated from these commits
