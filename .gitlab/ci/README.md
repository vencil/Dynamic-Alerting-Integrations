# GitLab CI Templates — DEPRECATED

> **Status**: Deprecated as of v2.0.0.
> **Primary CI**: GitHub Actions (`.github/workflows/`)

These GitLab CI templates are provided as **community references** for
organisations that run GitLab. They are **not actively maintained** and
may fall behind the canonical GitHub Actions workflows.

## Files

| Template | Purpose |
|----------|---------|
| `config-diff.gitlab-ci.yml` | Blast-radius report on config MRs |
| `docs-ci.gitlab-ci.yml` | Documentation lint / render pipeline |

## If You Use These

1. Pin the `da-tools` image tag in `config-diff.gitlab-ci.yml` to a
   known-good version before using in production.
2. Cross-check tool paths against the current `scripts/tools/` layout —
   subdirectory prefixes (`ops/`, `dx/`, `lint/`) were introduced in
   v2.0.0.
3. Contributions welcome — please open a PR if you update these for
   newer versions.
