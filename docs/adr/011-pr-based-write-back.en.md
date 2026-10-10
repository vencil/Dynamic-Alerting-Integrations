---
title: "ADR-011: PR-based Write-back Mode"
tags: [adr, architecture, gitops, pr, write-back]
audience: [platform-engineers, developers]
version: v2.9.0
lang: en
---
# ADR-011: PR-based Write-back Mode

> **Language / 語言：** **English (Current)** | [中文](./011-pr-based-write-back.md)

**Decision in brief**: besides committing directly, tenant-api offers an "open a PR" write-back mode, chosen at deployment time with `--write-mode` (or the `TA_WRITE_MODE` environment variable); the default is still a direct commit. In PR mode every write of tenant configuration creates a new branch, commits, pushes, and then opens a PR on GitHub or an MR on GitLab; the configuration only takes effect once the PR is merged. Between single writes, a tenant can have at most one pending PR at a time (batch writes are not covered; see the known limitations).

## Status

✅ **Accepted** (v2.6.0) — Adds a PR write-back mode (`--write-mode pr`) where writes of tenant configuration create GitHub PRs instead of direct commits

2026-10-10: corrected how the write-back mode is configured: it is `--write-mode` / `TA_WRITE_MODE`, not a `_write_mode` setting in conf.d.

## Terms

- **Direct write-back (direct)**: the commit-on-write of [ADR-009](009-tenant-manager-crud-api.md): the API modifies YAML in conf.d/ and commits immediately.
- **PR / MR**: GitHub's Pull Request and GitLab's Merge Request, two names for the same thing. "PR" in this document covers both.
- **GitOps**: an operating model in which a Git repo is the single source of configuration and every change reaches the system through Git.
- **Four-eyes principle**: a change must be reviewed by at least one other person before it takes effect.
- **Eventual consistency**: a successful write does not take effect immediately; in PR mode the configuration only really takes effect after the PR is merged, so there is a "submitted but not yet in effect" state in between.

## Background

### Problem

The direct write-back of [ADR-009](009-tenant-manager-crud-api.md) (UI → tenant-api → git commit) works well in fast-iterating environments, but runs into compliance friction in high-security scenarios:

1. **Four-eyes principle**: regulated industries such as finance and healthcare require configuration changes to be reviewed by at least one person before they take effect.
2. **Reversibility**: with several operators working in parallel, reverting a direct commit means tracking down the commit hash by hand.
3. **CI integration**: some teams want a config change to trigger CI first (lint, dry-run apply, SLA (service level agreement) impact assessment) and only then be merged.
4. **Audit granularity**: a PR carries richer audit information than git log (reviewer, approval time, discussion thread).

### Decision drivers

- Keep the GitOps spirit: the Git repo remains the single source of configuration.
- Backward compatible: the existing direct write-back is unaffected, and PR mode has to be turned on explicitly.
- Eventual consistency is acceptable: in PR mode the UI must clearly mark configuration that is "submitted but not merged".
- Reuse GitHub's PR mechanism instead of building review infrastructure.

## Decision

### Two write-back modes

| `--write-mode` | Behavior |
|------|------|
| `direct` (default) | Commit directly (ADR-009 behavior) |
| `pr` or `pr-github` | Open a GitHub PR |
| `pr-gitlab` | Open a GitLab MR |

GitHub and GitLab implement the same provider-agnostic interfaces (create a PR, track pending PRs), so the handlers do not need to know which platform is behind them.

### Example: turning on PR mode

```bash
TA_WRITE_MODE=pr
TA_GITHUB_REPO=org/repo          # or --github-repo
TA_GITHUB_TOKEN=<token>          # injected from a Kubernetes Secret
# optional: TA_GITHUB_BASE_BRANCH, defaults to main
```

For `pr-gitlab` the counterparts are `TA_GITLAB_PROJECT`, `TA_GITLAB_TOKEN`, and `TA_GITLAB_TARGET_BRANCH`. If the repo or the token is missing, tenant-api fails at startup.

Write flow:

```
write request → write-back mode?
  ├─ direct → commit directly (ADR-009)
  └─ pr     → create branch → commit → push → create PR → return pr_url
```

### PR lifecycle

```
┌──────────┐    create    ┌─────────────┐    merge     ┌──────────┐
│ (UI op)   │ ──────────→ │ pending_review│ ──────────→ │  merged   │
└──────────┘              └─────────────┘              └──────────┘
                               │
                               │ close
                               ▼
                          ┌──────────┐
                          │  closed   │
                          └──────────┘
```

| State | Meaning |
|-------|---------|
| `pending_review` | PR created, awaiting review |
| `merged` | PR merged, the configuration is in effect |
| `closed` | PR closed |

### Example: single-tenant write

`PUT /api/v1/tenants/db-a-prod` by operator `alice@example.com`. tenant-api creates a branch named `tenant-api/{tenant ID}/{UTC time}`, for example `tenant-api/db-a-prod/20260406-143022`; the commit content is the same as with direct write-back (only this tenant's YAML changes), and the author is the operator's email. The PR it creates:

```json
{
  "title": "[tenant-api] Update db-a-prod configuration",
  "body": "**Operator:** alice@example.com\n**Source:** tenant-manager UI\n**Tenant:** db-a-prod",
  "head": "tenant-api/db-a-prod/20260406-143022",
  "base": "main"
}
```

After the PR is created, the `tenant-api` and `auto-generated` labels are added (on GitLab they are sent along when the MR is created). The API response:

```json
{
  "status": "pending_review",
  "tenant_id": "db-a-prod",
  "pr_url": "https://github.com/org/repo/pull/42",
  "pr_number": 42,
  "message": "PR/MR created. Configuration will take effect after merge."
}
```

### Example: batch write

A batch operation is consolidated into **one PR** (one PR holding several tenants' changes), so reviewers are not flooded with PRs:

```json
{
  "status": "pending_review",
  "pr_url": "https://github.com/org/repo/pull/43",
  "pr_number": 43,
  "results": [
    {"tenant_id": "db-a-prod", "status": "included"},
    {"tenant_id": "db-b-staging", "status": "included"}
  ],
  "summary": "2 included in PR/MR, 0 failed",
  "message": "Batch PR/MR created with 2 tenant changes."
}
```

### Token permissions and secret management

**GitHub** (`--write-mode pr` or `pr-github`):

| Item | Specification |
|------|---------------|
| **Token type** | GitHub Fine-grained Personal Access Token (recommended) or GitHub App Installation Token |
| **Minimum permissions** | `contents: write` + `pull_requests: write` (target repo only) |
| **Storage** | Kubernetes Secret → environment variable `TA_GITHUB_TOKEN`; never put it in a ConfigMap or YAML |

**GitLab** (`--write-mode pr-gitlab`):

| Item | Specification |
|------|---------------|
| **Token type** | GitLab Project Access Token (recommended), Group Access Token, or Personal Access Token |
| **Minimum permissions** | `api` scope (covers MR creation and branch operations) |
| **Storage** | Kubernetes Secret → environment variable `TA_GITLAB_TOKEN`; never put it in a ConfigMap or YAML |

### Conflicts between parallel PRs

**Problem**: when two pending PRs change the same file, merging one of them can leave the other in a Git conflict.

**Approach**:

1. Each PR rewrites only its own tenant's config file (a batch PR changes the files of the tenants in the batch).
2. For a single write (`PUT /api/v1/tenants/{id}`), if the tenant already has a pending PR, the write returns 409 together with a link to the existing PR:

```json
{
  "code": "PENDING_PR_EXISTS",
  "error": "pending_pr_exists",
  "existing_pr_url": "https://github.com/org/repo/pull/42",
  "message": "A pending PR/MR for db-a-prod already exists or is being created. Merge or close it first.",
  "pr_number": 42,
  "request_id": "<request ID>"
}
```

Changes made outside the API (manual commits pushed to a PR branch, a force-push to the base branch) can also cause conflicts. tenant-api does not resolve them: conflicts on GitLab MRs are recorded in logs and metrics, while GitHub PRs do not expose their conflict state.

### Showing eventual consistency

In PR mode the tenant-manager UI has to distinguish two configuration states:

| State | Data source | Display |
|-------|-------------|---------|
| **In effect** | `conf.d/*.yaml` (HEAD of the base branch) | Normal display |
| **Pending review** | the pending-PR list tenant-api keeps in memory | A notice at the top of the page, "N pending PR(s) — config changes awaiting review", listing links to the first 3 PRs (the rest shown as "+N more"); a `PR #N` badge on the tenant card |

tenant-api keeps the list of pending PRs in memory, syncs it periodically with the GitHub / GitLab API, and exposes:

- `GET /api/v1/prs` — list all pending PRs
- `GET /api/v1/prs?tenant={id}` — query the pending PR of a specific tenant

## Rationale

### Why not Git hooks + auto-merge?

GitHub's PR mechanism natively integrates code review, approval, and CI checks. Building our own approval flow would reinvent the wheel and miss out on the existing ecosystem.

### Why consolidate a batch into one PR?

- Reviewer experience: all related changes are reviewed at once.
- Atomicity: the tenant changes in a batch either all take effect or none do.
- Fewer PRs: a 20-tenant batch does not produce 20 PRs.

### Why allow only one pending PR per tenant?

- It avoids ambiguity from merge order (PR 1 enables silent mode, PR 2 disables it, and the final state depends on which merges last).
- It keeps the UI simple (at most one pending marker per tenant).

### Why not split `_groups.yaml` into several files?

In PR mode, group definitions are still committed directly (see "Known limitations"), and their conflicts are handled by direct write-back's HEAD conflict detection. The evaluation found the cost of splitting outweighs the benefit:

- Group operations are far less frequent than tenant operations, so conflicts are unlikely.
- Splitting would require changing the loader, the API, and the schema throughout.

## Consequences and known limitations

**What we get**

- Meets the compliance requirements of high-security environments such as finance and healthcare.
- PRs provide native change tracking, discussion, and CI integration.
- Backward compatible: direct write-back mode is not affected at all.

**What we accept**

- **Latency**: in PR mode a configuration change goes from "effective immediately" to "wait for the merge".
- **Complexity**: a dependency on the GitHub / GitLab API, token management, and tracking of pending PRs.
- **Eventual consistency**: the UI has to handle the "submitted but not in effect" intermediate state.
- **No tenant-config writes while GitHub / GitLab is unavailable**: the API then returns 503 and asks to retry later.
- **Token problems do not stop startup**: the token is checked at startup, but a failure is only logged as a warning and startup continues.

**Known limitations**

- **Groups and saved views bypass PRs**: in PR mode, writes to group definitions (`_groups.yaml`) and saved views (`_views.yaml`) are still committed directly, so the four-eyes principle does not cover them.
- **Batch writes do not check for pending PRs**: the "one pending PR per tenant" check runs only for single writes; the PR path of batch writes (`POST /api/v1/tenants/batch`, `POST /api/v1/groups/{id}/batch`) does not perform it, and the branch it creates is named `tenant-api/batch/<UTC time>`. The single-write check does not see PRs opened by batch writes either: while a batch PR is unmerged, a single write can still open another PR for the same tenant.
- **Single replica only**: the "one pending PR per tenant" check lives in the memory of a single process and is not coordinated across Pods, so more replicas can open duplicate PRs for the same tenant. The Helm chart refuses to render when `replicaCount` is greater than 1.

## Alternatives considered

| Alternative | Assessment | Why not |
|-------------|-----------|---------|
| **A custom approval queue** | Viable | Reinvents the wheel, lacks CI/CD integration, high maintenance cost |
| **A branch per write + manual merge** | Viable | Poor experience; operators have to leave the UI and work in Git by hand |
| **Write-ahead log (WAL: write every change to a log first, then apply it)** | Over-engineering | Tenant configuration does not need database-grade (ACID) durability guarantees |

## Implementation

| Layer | File | Content |
|---|------|------|
| **Config** | `cmd/server/main.go` | `--write-mode` flag (`direct` / `pr` / `pr-github` / `pr-gitlab`) and its environment variables |
| **Platform interface** | `internal/platform/platform.go` | Provider-agnostic `Client` and `Tracker` interfaces |
| **Writer** | `internal/gitops/writer_pr.go` | `WritePR()`: create branch → commit → push |
| **GitHub** | `internal/github/client.go`, `internal/github/tracker.go` | Wraps the GitHub REST API; caches pending PRs and syncs them periodically |
| **GitLab** | `internal/gitlab/client.go`, `internal/gitlab/tracker.go` | Wraps the GitLab REST API v4; caches pending MRs and syncs them periodically |
| **Handler** | `internal/handler/tenant_put.go` | Chooses a direct commit or a PR according to the write-back mode |
| **Handler** | `internal/handler/tenant_batch.go` | Consolidates a batch into one PR in PR mode |
| **Handler** | `internal/handler/pr.go` | `GET /api/v1/prs` |
| **UI** | `tenant-manager.jsx` | Pending-PR notice and markers |

All paths except the UI are under `components/tenant-api/`.

## Related

- [ADR-009: Tenant Manager CRUD API Architecture](009-tenant-manager-crud-api.md) — PR mode builds on direct write-back
- [ADR-010: Multi-Tenant Grouping Architecture](010-multi-tenant-grouping.md) — how group batch operations are consolidated into a PR
- [ADR-008: Operator-Native Integration Path](008-operator-native-integration-path.md) — how PR write-back maps to CRDs (Kubernetes custom resources) in Operator mode
- [GitHub REST API: Pulls](https://docs.github.com/en/rest/pulls)
- [GitHub Fine-grained PAT](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)
- [GitLab REST API: Merge Requests](https://docs.gitlab.com/ee/api/merge_requests.html)
- [GitLab Project Access Tokens](https://docs.gitlab.com/ee/user/project/settings/project_access_tokens.html)
- [Four-eyes principle (Wikipedia)](https://en.wikipedia.org/wiki/Two-man_rule)
