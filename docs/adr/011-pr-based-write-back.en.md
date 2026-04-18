---
title: "ADR-011: PR-based Write-back Mode"
tags: [adr, architecture, gitops, pr, write-back]
audience: [platform-engineers, developers]
version: v2.7.0
lang: en
---

# ADR-011: PR-based Write-back Mode

> **Language / 語言：** **English (Current)** | [中文](./011-pr-based-write-back.md)

## Status

✅ **Accepted** (v2.6.0) — Adds `_write_mode: pr` option where UI operations create GitHub PRs instead of direct commits

## Background

### Problem Statement

The commit-on-write model established in ADR-009 (UI → tenant-api → git commit) works well in fast-iteration environments, but encounters compliance friction in high-security scenarios:

1. **Four-eyes principle**: Regulated industries (finance, healthcare) require configuration changes to be reviewed by at least one additional person before taking effect
2. **Change reversibility**: Direct commits in multi-operator environments make reverting harder to track
3. **CI integration**: Some teams want config changes to trigger CI pipelines (lint, dry-run apply, SLA impact assessment) before merging
4. **Audit granularity**: PRs provide richer audit metadata than git log (reviewer, approval time, discussion thread)

### Decision Drivers

- Maintain GitOps spirit: Git repo remains the source of truth
- Backward compatible: Existing `direct` mode is unaffected; `pr` is opt-in
- Eventual consistency is acceptable: UI must clearly indicate "submitted but not yet merged" configs
- Reuse GitHub API: No additional approval infrastructure

## Decision

### Dual-Mode Architecture: `_write_mode: direct | pr`

A new global config `_write_mode` (or environment variable `TA_WRITE_MODE`) sits alongside `_rbac.yaml`:

```yaml
# tenant-api flag or env var
_write_mode: pr   # "direct" (default, ADR-009 behavior) | "pr" (PR-based)
```

**Routing logic** (writer.go layer):

```
WriteRequest → _write_mode?
  ├─ "direct" → existing commit-on-write (ADR-009)
  └─ "pr"     → create-branch → commit → push → create-PR → return pr_url
```

### PR Lifecycle State Model

```
┌──────────┐    create    ┌─────────────┐    merge     ┌──────────┐
│ (UI op)   │ ──────────→ │ pending_review│ ──────────→ │  merged   │
└──────────┘              └─────────────┘              └──────────┘
                               │
                               │ close/conflict
                               ▼
                          ┌──────────┐
                          │  closed   │
                          └──────────┘
```

| State | Semantics | UI Rendering |
|-------|-----------|-------------|
| `pending_review` | PR created, awaiting reviewer | Yellow banner + PR link |
| `merged` | PR merged, config is active | Green notification, banner disappears |
| `closed` | PR closed or has conflicts | Red warning + re-submit button |

### PR Creation Strategy

**Branch naming**: `tenant-api/{tenantID}/{timestamp}` (e.g., `tenant-api/db-a-prod/20260406-143022`)

**Commit content**: Same as direct mode (single tenant YAML modification), author is operator email

**PR metadata**:
```json
{
  "title": "[tenant-api] Update db-a-prod configuration",
  "body": "Operator: alice@example.com\nChanges: _silent_mode → enabled\nSource: tenant-manager UI",
  "head": "tenant-api/db-a-prod/20260406-143022",
  "base": "main",
  "labels": ["tenant-api", "auto-generated"]
}
```

### API Response Format

**Single tenant write** (PR mode):

```json
{
  "status": "pending_review",
  "pr_url": "https://github.com/org/repo/pull/42",
  "pr_number": 42,
  "message": "PR created. Configuration will take effect after merge."
}
```

**Batch operation** (PR mode):

```json
{
  "status": "pending_review",
  "pr_url": "https://github.com/org/repo/pull/43",
  "pr_number": 43,
  "results": [
    {"tenant_id": "db-a-prod", "status": "included"},
    {"tenant_id": "db-b-staging", "status": "included"}
  ],
  "message": "Batch PR created with 2 tenant changes."
}
```

Batch operations consolidate into a **single PR** to avoid overwhelming reviewers.

### Token Permissions & Secret Management

**GitHub mode** (`--write-mode pr` or `pr-github`):

| Item | Specification |
|------|---------------|
| **Token type** | GitHub Fine-grained PAT (recommended) or GitHub App Installation Token |
| **Minimum permissions** | `contents: write` + `pull_requests: write` (target repo only) |
| **Storage** | K8s Secret → env var `TA_GITHUB_TOKEN`; never in ConfigMap or YAML |
| **Rotation policy** | 90-day expiry + Helm pre-upgrade hook to check validity |

**GitLab mode** (`--write-mode pr-gitlab`, added in v2.6.0 Phase E):

| Item | Specification |
|------|---------------|
| **Token type** | GitLab Project Access Token (recommended), Group Access Token, or Personal Access Token |
| **Minimum permissions** | `api` scope (covers MR creation and branch operations) |
| **Storage** | K8s Secret → env var `TA_GITLAB_TOKEN`; never in ConfigMap or YAML |
| **Rotation policy** | 365-day expiry (GitLab default) + Helm pre-upgrade hook to check validity |

### Parallel PR Conflict Handling

**Problem**: Tenant A route change (PR 1) + Tenant B threshold change (PR 2) may conflict if they modify the same file.

**Two-layer mitigation**:

1. **File-level isolation** (already in place): Each tenant has its own YAML file (`conf.d/{tenantID}.yaml`); different tenants' PRs are naturally isolated
2. **Same-tenant concurrency control**: If a tenant already has a pending PR, new writes return 409 + existing PR link
3. **`_groups.yaml` special handling**: Group operations modify a shared file; use advisory lock + auto-rebase before PR creation

### Eventual Consistency Semantics

In PR mode, tenant-manager UI must distinguish two config states:

| State | Data source | Display |
|-------|-------------|---------|
| **Active** | `conf.d/*.yaml` (main branch HEAD) | Normal display |
| **Pending review** | tenant-api in-memory PR tracker | Yellow overlay + "Pending PR" badge |

tenant-api maintains an in-memory PR tracker (periodically syncing with GitHub API), exposing:
- `GET /api/v1/prs` — list all pending PRs
- `GET /api/v1/prs?tenant={id}` — query pending PRs for a specific tenant

### Implementation Layers

| Layer | File | Changes |
|-------|------|---------|
| **Config** | `cmd/server/main.go` | `-write-mode` flag (`direct` / `pr` / `pr-github` / `pr-gitlab`) + env vars |
| **Platform Interface** | `internal/platform/platform.go` (v2.6.0 Phase E) | Provider-agnostic `Client` + `Tracker` interfaces |
| **Writer** | `internal/gitops/writer.go` | `WritePR()` method: branch → commit → push |
| **GitHub Client** | `internal/github/client.go` | Wraps GitHub REST API, implements `platform.Client` |
| **GitHub Tracker** | `internal/github/tracker.go` | In-memory pending PR cache + periodic sync, implements `platform.Tracker` |
| **GitLab Client** | `internal/gitlab/client.go` (v2.6.0 Phase E) | Wraps GitLab REST API v4, implements `platform.Client` |
| **GitLab Tracker** | `internal/gitlab/tracker.go` (v2.6.0 Phase E) | In-memory pending MR cache + periodic sync, implements `platform.Tracker` |
| **Handler** | `internal/handler/tenant_put.go` | Route by write mode → `Write()` or `WritePR()` via `platform.Client` |
| **Handler** | `internal/handler/tenant_batch.go` | Batch PR/MR mode: consolidate into single PR/MR |
| **Handler** | `internal/handler/pr.go` | `GET /api/v1/prs` endpoint via `platform.Tracker` |
| **UI** | `tenant-manager.jsx` | Pending PRs/MRs banner + status overlay |

## Rationale

### Why not Git hooks + auto-merge?

GitHub PRs provide native code review, approval, and CI check integration. Building custom approval workflows duplicates existing ecosystem capabilities.

### Why consolidate batch operations into a single PR?

- Reviewer experience: Review all related changes at once
- Atomicity: All tenant changes in a batch either take effect or don't
- Reduces PR volume: Avoids 20 PRs from a 20-tenant batch

### Why allow only one pending PR per tenant?

- Avoids merge-order ambiguity (PR 1 enables silent, PR 2 disables it — merge order determines outcome)
- Simplifies UI (at most one pending badge per tenant)
- For multiple modifications, operators can update (force-push) the existing PR branch

### Why not split `_groups.yaml` into multiple files?

Evaluated but costs outweigh benefits:
- Group operations are far less frequent than tenant operations; conflict probability is low
- Splitting requires changes to loader, API, and schema
- Advisory lock + auto-rebase sufficiently handles occasional conflicts

## Consequences

### Positive

- Meets compliance requirements for regulated industries (finance, healthcare)
- PRs provide native change tracking, discussion, and CI integration
- Backward compatible: `direct` mode is completely unaffected

### Negative

- **Latency**: Config changes go from "instant" to "await merge" (PR mode)
- **Complexity**: Adds GitHub API dependency, token management, PR tracker
- **Eventual consistency**: UI must handle the "submitted but not active" intermediate state

### Risk Mitigation

| Risk | Mitigation |
|------|-----------|
| GitHub/GitLab API unavailable | Return 503 + degrade hint "temporarily use direct mode or retry later" |
| Token expired | Check token validity on startup + report in `/healthz` |
| PR/MR never merged | Optional `pr_ttl` auto-close (disabled by default) |

## Alternatives Considered

| Alternative | Assessment | Reason for Rejection |
|-------------|-----------|---------------------|
| **GitLab MR** (instead of GitHub PR) | ✅ **Implemented** | v2.6.0 Phase E: `platform.Client` abstraction layer + `internal/gitlab/` package. Enabled via `--write-mode pr-gitlab` |
| **Custom approval queue** | Viable | Reinvents the wheel, lacks CI/CD integration, high maintenance cost |
| **Git branch per-write + manual merge** | Viable | Poor UX; operators must leave UI for Git operations |
| **Write-Ahead Log (WAL)** | Over-engineering | Tenant config doesn't need ACID-level persistence guarantees |

## Related Decisions

- **ADR-009**: Tenant Manager CRUD API — PR mode builds on commit-on-write foundation
- **ADR-010**: Multi-Tenant Grouping — Batch PR consolidation strategy for group operations
- **ADR-008**: Operator Native Integration — PR write-back CRD mapping for Operator mode

## References

- [GitHub REST API: Pulls](https://docs.github.com/en/rest/pulls)
- [GitHub Fine-grained PAT](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)
- [GitLab REST API: Merge Requests](https://docs.gitlab.com/ee/api/merge_requests.html)
- [GitLab Project Access Tokens](https://docs.gitlab.com/ee/user/project/settings/project_access_tokens.html)
- [Four-eyes principle (Wikipedia)](https://en.wikipedia.org/wiki/Two-man_rule)
