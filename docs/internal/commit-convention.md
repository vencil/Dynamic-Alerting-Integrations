---
title: Conventional Commits Guide
sidebar_label: Commit Convention
description: How to write Conventional Commits for automated changelog generation
tags: [commits, conventions, internal]
audience: [contributors, maintainers]
version: v2.7.0
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

The scope is optional but recommended. **Source of truth**: `.commitlintrc.yaml` `scope-enum` rule (this list drifts; CI is the authority).

Common scopes (most-used subset; see `.commitlintrc.yaml` for the full enforced list):

- **exporter**: threshold-exporter code or deployment (NOT `threshold-exporter` — that's a common mistake)
- **tools**: tools in `scripts/tools/`
- **docs**: docs / guides
- **rule-packs**: rule pack definitions
- **ci**: GitHub Actions / GitLab CI infrastructure
- **k8s**: Kubernetes manifests / deployments
- **helm**: Helm chart configuration
- **ops** / **dx** / **lint**: tool-map sub-categories (matches `scripts/tools/{ops,dx,lint}/`)
- **phase-a** / **phase-b** / **phase-c**: development phase scopes (use these for phase-bundle PRs that span multiple components)
- **scanner** / **golden** / **config** / **session-init**: component / sub-area scopes for narrow refactors
- **adr** / **playbook** / **planning**: doc-only sub-categories (use when commit is purely about ADR / playbook / planning archive content)

> **Common pitfall**: typing the verbose component name (`threshold-exporter`, `tenant-api`) instead of the short scope (`exporter`). commitlint will reject; check `.commitlintrc.yaml` if unsure. The CI failure message lists every allowed scope — copy from there.

### Description

A brief, imperative description of the change (lowercase, no period):

- ✅ "add validation for tenant keys"
- ✅ "fix cardinality limit check"
- ❌ "Added validation"
- ❌ "Fixed cardinality limit check."

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
