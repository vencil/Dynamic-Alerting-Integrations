#!/usr/bin/env python3
"""bump_docs.py — 版號一致性管理工具

掃描 repo 中的文件、Chart.yaml、VERSION 檔案，批次更新版號引用。
四條版號線獨立管理：--platform / --exporter / --tools / --tenant-api。

Chart.yaml version 與 appVersion 同步，統一由 --exporter 管理。
--exporter 同時更新：Chart.yaml version + appVersion + image tag + OCI chart references。

用法:
  # 更新 exporter 版號 (Chart.yaml version + appVersion + image tag + OCI chart)
  python3 scripts/tools/bump_docs.py --exporter 1.1.0

  # 更新 da-tools 版號 (所有 image tag + VERSION)
  python3 scripts/tools/bump_docs.py --tools 1.1.0

  # 更新 tenant-api 版號 (Chart.yaml + Dockerfile LABEL + image tag)
  python3 scripts/tools/bump_docs.py --tenant-api 2.4.0

  # 更新平台文件版號
  python3 scripts/tools/bump_docs.py --platform 1.1.0

  # 只檢查不修改 (CI lint 用)
  python3 scripts/tools/bump_docs.py --check

  # Dry-run：顯示 before→after diff 但不寫入
  python3 scripts/tools/bump_docs.py --dry-run --platform 2.1.0

  # 限定範圍：只處理 docs/ 下的檔案
  python3 scripts/tools/bump_docs.py --dry-run --scope docs --platform 2.1.0

  # 初始化英文 CHANGELOG
  python3 scripts/tools/bump_docs.py --init-changelog 2.1.0 --changelog-lang en

  # 同時初始化中英文 CHANGELOG
  python3 scripts/tools/bump_docs.py --init-changelog 2.1.0 --changelog-lang all

  # 完整規則審計（顯示所有規則的當前匹配狀態）
  python3 scripts/tools/bump_docs.py --what-if

  # 自動更新散落在文件中的硬編碼計數（工具、Rule Pack、文件數、hooks 等）
  python3 scripts/tools/bump_docs.py --sync-counts

  # 檢查計數是否需要更新
  python3 scripts/tools/bump_docs.py --sync-counts --check

  # 組合使用
  python3 scripts/tools/bump_docs.py --platform 1.1.0 --tools 1.1.0 --exporter 1.1.0 --tenant-api 2.4.0
"""
import argparse
import os
import re
import stat
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Repo root detection
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent.parent  # scripts/tools/dx/ -> repo root

# ---------------------------------------------------------------------------
# Version source-of-truth files
# ---------------------------------------------------------------------------
CHART_YAML = REPO_ROOT / "components" / "threshold-exporter" / "Chart.yaml"
DA_TOOLS_VERSION = REPO_ROOT / "components" / "da-tools" / "app" / "VERSION"
TENANT_API_CHART_YAML = REPO_ROOT / "helm" / "tenant-api" / "Chart.yaml"

# ---------------------------------------------------------------------------
# Replacement rules per version line
# ---------------------------------------------------------------------------
# Each rule: (file_relative_path, pattern_func, replacement_func)
# pattern_func(old_ver) -> regex pattern
# replacement_func(new_ver) -> replacement string

# Semver with optional pre-release suffix (-preview, -beta, etc.)
_SEMVER = r"[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?"
# Strict semver (no suffix) for image tags and chart versions
_SEMVER_STRICT = r"[0-9]+\.[0-9]+\.[0-9]+"


def _build_tools_rules():
    """Build version replacement rules for da-tools (image tags, VERSION file).

    Returns list of rule dicts for the 'tools' version line.
    """
    rules = []
    rules.append({
        "file": "__glob__",
        "glob_dir": "docs",
        "glob_pattern": "**/*.md",
        "desc": "da-tools image tag in docs/**/*.md",
        "pattern": r"ghcr\.io/vencil/da-tools:v?[0-9]+\.[0-9]+\.[0-9]+",
        "replacement": lambda v: f"ghcr.io/vencil/da-tools:v{v}",
    })
    rules.append({
        "file": "__glob__",
        "glob_dir": "docs",
        "glob_pattern": "**/*.jsx",
        "desc": "da-tools image tag in docs/**/*.jsx",
        "pattern": r"ghcr\.io/vencil/da-tools:v?[0-9]+\.[0-9]+\.[0-9]+",
        "replacement": lambda v: f"ghcr.io/vencil/da-tools:v{v}",
    })
    for f in ["README.md", "README.en.md",
              "components/da-tools/README.md",
              "components/threshold-exporter/README.md"]:
        rules.append({
            "file": f,
            "desc": f"da-tools image tag in {f}",
            "pattern": r"ghcr\.io/vencil/da-tools:v?[0-9]+\.[0-9]+\.[0-9]+",
            "replacement": lambda v: f"ghcr.io/vencil/da-tools:v{v}",
        })

    # VERSION file (exact content)
    rules.append({
        "file": "components/da-tools/app/VERSION",
        "desc": "da-tools VERSION file",
        "pattern": r"^[0-9]+\.[0-9]+\.[0-9]+\s*$",
        "replacement": lambda v: f"{v}\n",
        "whole_file": True,
    })

    # da-tools README build.sh version
    rules.append({
        "file": "components/da-tools/README.md",
        "desc": "da-tools build.sh version",
        "pattern": r"\./build\.sh [0-9]+\.[0-9]+\.[0-9]+",
        "replacement": lambda v: f"./build.sh {v}",
    })

    # da-tools README version header
    rules.append({
        "file": "components/da-tools/README.md",
        "desc": "da-tools README version header",
        "pattern": r"\*\*版本\*\*：[0-9]+\.[0-9]+\.[0-9]+（獨立版號",
        "replacement": lambda v: f"**版本**：{v}（獨立版號",
    })

    # da-tools README version strategy table
    rules.append({
        "file": "components/da-tools/README.md",
        "desc": "da-tools version strategy table (da-tools row)",
        "pattern": r"\| \*\*da-tools\*\* \| \*\*v?[0-9]+\.[0-9]+\.[0-9]+\*\*",
        "replacement": lambda v: f"| **da-tools** | **v{v}**",
    })
    rules.append({
        "file": "components/da-tools/README.md",
        "desc": "da-tools version strategy table (git tag)",
        "pattern": r"\*\*`tools/v[0-9]+\.[0-9]+\.[0-9]+`\*\*",
        "replacement": lambda v: f"**`tools/v{v}`**",
    })

    # CI workflow and K8s manifest image tags
    for f in [".github/workflows/config-diff.yaml",
              ".gitlab/ci/config-diff.gitlab-ci.yml",
              "k8s/03-monitoring/cronjob-maintenance-scheduler.yaml"]:
        rules.append({
            "file": f,
            "desc": f"da-tools image tag in {f}",
            "pattern": r"ghcr\.io/vencil/da-tools:v?[0-9]+\.[0-9]+\.[0-9]+",
            "replacement": lambda v: f"ghcr.io/vencil/da-tools:v{v}",
        })

    # mkdocs.yml tools_version
    rules.append({
        "file": "mkdocs.yml",
        "desc": "mkdocs.yml tools_version",
        "pattern": r'tools_version:\s+"' + _SEMVER_STRICT + '"',
        "replacement": lambda v: f'tools_version: "{v}"',
    })

    return rules


def _build_tenant_api_rules():
    """Build version replacement rules for tenant-api.

    Returns list of rule dicts for the 'tenant-api' version line.
    """
    rules = []

    # helm/tenant-api/Chart.yaml version
    rules.append({
        "file": "helm/tenant-api/Chart.yaml",
        "desc": "tenant-api Chart.yaml version",
        "pattern": r"^version:\s+" + _SEMVER_STRICT,
        "replacement": lambda v: f"version: {v}",
    })
    rules.append({
        "file": "helm/tenant-api/Chart.yaml",
        "desc": "tenant-api Chart.yaml appVersion",
        "pattern": r"^appVersion:\s+" + _SEMVER_STRICT,
        "replacement": lambda v: f"appVersion: {v}",
    })

    # Dockerfile LABEL version
    rules.append({
        "file": "components/tenant-api/Dockerfile",
        "desc": "tenant-api Dockerfile LABEL version",
        "pattern": r'org\.opencontainers\.image\.version="' + _SEMVER_STRICT + '"',
        "replacement": lambda v: f'org.opencontainers.image.version="{v}"',
    })

    # README OCI chart --version
    rules.append({
        "file": "components/tenant-api/README.md",
        "desc": "tenant-api OCI chart --version in README",
        "pattern": r"oci://ghcr\.io/vencil/charts/tenant-api\s+--version\s+" + _SEMVER_STRICT,
        "replacement": lambda v: f"oci://ghcr.io/vencil/charts/tenant-api --version {v}",
    })

    # tenant-api image tag in docs
    rules.append({
        "file": "__glob__",
        "glob_dir": "docs",
        "glob_pattern": "**/*.md",
        "desc": "tenant-api image tag in docs",
        "pattern": r"ghcr\.io/vencil/tenant-api:v?" + _SEMVER_STRICT,
        "replacement": lambda v: f"ghcr.io/vencil/tenant-api:v{v}",
    })

    return rules


def _build_exporter_rules():
    """Build version replacement rules for threshold-exporter.

    Covers Chart.yaml version/appVersion and OCI chart references.
    Returns list of rule dicts for the 'exporter' version line.
    """
    return [
        {
            "file": "components/threshold-exporter/Chart.yaml",
            "desc": "Chart.yaml version (chart release)",
            "pattern": r"^version:\s*[0-9]+\.[0-9]+\.[0-9]+",
            "replacement": lambda v: f"version: {v}",
        },
        {
            "file": "components/threshold-exporter/Chart.yaml",
            "desc": "Chart.yaml appVersion",
            "pattern": r'^appVersion:\s*"[0-9]+\.[0-9]+\.[0-9]+"',
            "replacement": lambda v: f'appVersion: "{v}"',
        },
        {
            "file": "docs/migration-guide.md",
            "desc": "OCI chart --version in migration guide",
            "pattern": r"oci://ghcr\.io/vencil/charts/threshold-exporter --version [0-9]+\.[0-9]+\.[0-9]+",
            "replacement": lambda v: f"oci://ghcr.io/vencil/charts/threshold-exporter --version {v}",
        },
        {
            "file": "components/threshold-exporter/README.md",
            "desc": "OCI chart --version in exporter README",
            "pattern": r"oci://ghcr\.io/vencil/charts/threshold-exporter --version [0-9]+\.[0-9]+\.[0-9]+",
            "replacement": lambda v: f"oci://ghcr.io/vencil/charts/threshold-exporter --version {v}",
        },
        {
            "file": "README.md",
            "desc": "OCI chart --version in Chinese README",
            "pattern": r"oci://ghcr\.io/vencil/charts/threshold-exporter --version [0-9]+\.[0-9]+\.[0-9]+",
            "replacement": lambda v: f"oci://ghcr.io/vencil/charts/threshold-exporter --version {v}",
        },
        {
            "file": "README.en.md",
            "desc": "OCI chart --version in English README",
            "pattern": r"oci://ghcr\.io/vencil/charts/threshold-exporter --version [0-9]+\.[0-9]+\.[0-9]+",
            "replacement": lambda v: f"oci://ghcr.io/vencil/charts/threshold-exporter --version {v}",
        },
        {
            "file": "docs/gitops-deployment.md",
            "desc": "OCI chart --version in gitops deployment guide",
            "pattern": r"oci://ghcr\.io/vencil/charts/threshold-exporter --version [0-9]+\.[0-9]+\.[0-9]+",
            "replacement": lambda v: f"oci://ghcr.io/vencil/charts/threshold-exporter --version {v}",
        },
        {
            "file": "docs/gitops-deployment.en.md",
            "desc": "OCI chart --version in gitops deployment guide (en)",
            "pattern": r"oci://ghcr\.io/vencil/charts/threshold-exporter --version [0-9]+\.[0-9]+\.[0-9]+",
            "replacement": lambda v: f"oci://ghcr.io/vencil/charts/threshold-exporter --version {v}",
        },
        {
            "file": "components/da-tools/README.md",
            "desc": "exporter version in da-tools strategy table",
            "pattern": r"\| threshold-exporter \| v[0-9]+\.[0-9]+\.[0-9]+",
            "replacement": lambda v: f"| threshold-exporter | v{v}",
        },
        {
            "file": "components/da-tools/README.md",
            "desc": "exporter git tag in da-tools strategy table",
            "pattern": r"`exporter/v[0-9]+\.[0-9]+\.[0-9]+`",
            "replacement": lambda v: f"`exporter/v{v}`",
        },
        # OCI chart inline version (colon-style, no --version flag)
        {
            "file": "docs/index.md",
            "desc": "OCI chart inline version in index.md",
            "pattern": r"oci://ghcr\.io/vencil/charts/threshold-exporter:[0-9]+\.[0-9]+\.[0-9]+",
            "replacement": lambda v: f"oci://ghcr.io/vencil/charts/threshold-exporter:{v}",
        },
        # Exporter image tag in API docs
        {
            "file": "docs/api/README.md",
            "desc": "exporter image tag in API docs (zh)",
            "pattern": r"ghcr\.io/vencil/threshold-exporter:v?[0-9]+\.[0-9]+\.[0-9]+",
            "replacement": lambda v: f"ghcr.io/vencil/threshold-exporter:v{v}",
        },
        {
            "file": "docs/api/README.en.md",
            "desc": "exporter image tag in API docs (en)",
            "pattern": r"ghcr\.io/vencil/threshold-exporter:v?[0-9]+\.[0-9]+\.[0-9]+",
            "replacement": lambda v: f"ghcr.io/vencil/threshold-exporter:v{v}",
        },
        # OCI chart --version in scenario docs
        {
            "file": "docs/scenarios/multi-cluster-federation.md",
            "desc": "OCI chart --version in federation scenario (zh)",
            "pattern": r"oci://ghcr\.io/vencil/charts/threshold-exporter --version [0-9]+\.[0-9]+\.[0-9]+",
            "replacement": lambda v: f"oci://ghcr.io/vencil/charts/threshold-exporter --version {v}",
        },
        {
            "file": "docs/scenarios/multi-cluster-federation.en.md",
            "desc": "OCI chart --version in federation scenario (en)",
            "pattern": r"oci://ghcr\.io/vencil/charts/threshold-exporter --version [0-9]+\.[0-9]+\.[0-9]+",
            "replacement": lambda v: f"oci://ghcr.io/vencil/charts/threshold-exporter --version {v}",
        },
        {
            "file": "docs/migration-guide.en.md",
            "desc": "OCI chart --version in migration guide (en)",
            "pattern": r"oci://ghcr\.io/vencil/charts/threshold-exporter --version [0-9]+\.[0-9]+\.[0-9]+",
            "replacement": lambda v: f"oci://ghcr.io/vencil/charts/threshold-exporter --version {v}",
        },
        {
            "file": "mkdocs.yml",
            "desc": "mkdocs.yml exporter_version",
            "pattern": r'exporter_version:\s+"' + _SEMVER_STRICT + '"',
            "replacement": lambda v: f'exporter_version: "{v}"',
        },
    ]


def _build_platform_rules():
    """Build version replacement rules for platform docs.

    Covers doc footers, headers, front matter, README intros, and mkdocs.yml.
    Returns list of rule dicts for the 'platform' version line.
    """
    rules = []

    # Doc footers: **文件版本：** vX.Y.Z or **Document version:** vX.Y.Z
    rules.append({
        "file": "docs/architecture-and-design.md",
        "desc": "architecture-and-design.md footer",
        "pattern": r"\*\*文件版本：\*\*\s*v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?",
        "replacement": lambda v: f"**文件版本：** v{v}",
    })
    rules.append({
        "file": "docs/architecture-and-design.en.md",
        "desc": "architecture-and-design.en.md footer",
        "pattern": r"\*\*Document version:\*\*\s*v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?",
        "replacement": lambda v: f"**Document version:** v{v}",
    })

    # Doc headers with inline version
    rules.append({
        "file": "docs/architecture-and-design.md",
        "desc": "architecture-and-design.md header version",
        "pattern": r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)? 的技術架構",
        "replacement": lambda v: f"v{v} 的技術架構",
    })
    rules.append({
        "file": "docs/architecture-and-design.en.md",
        "desc": "architecture-and-design.en.md header version",
        "pattern": r"\(v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?\)\.",
        "replacement": lambda v: f"(v{v}).",
    })

    # BYO guides version headers
    rules.append({
        "file": "docs/byo-prometheus-integration.md",
        "desc": "BYOP guide version",
        "pattern": r"\*\*版本\*\*：v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?",
        "replacement": lambda v: f"**版本**：v{v}",
    })
    rules.append({
        "file": "docs/byo-alertmanager-integration.md",
        "desc": "BYO Alertmanager guide version",
        "pattern": r"\*\*版本\*\*：v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?",
        "replacement": lambda v: f"**版本**：v{v}",
    })

    # Governance doc version headers
    rules.append({
        "file": "docs/custom-rule-governance.md",
        "desc": "governance doc (zh) version header",
        "pattern": r"\*\*版本\*\*: v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?",
        "replacement": lambda v: f"**版本**: v{v}",
    })
    rules.append({
        "file": "docs/custom-rule-governance.en.md",
        "desc": "governance doc (en) version header",
        "pattern": r"\*\*Version\*\*: v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?",
        "replacement": lambda v: f"**Version**: v{v}",
    })

    # GitOps deployment guide version header
    rules.append({
        "file": "docs/gitops-deployment.md",
        "desc": "gitops-deployment.md version header",
        "pattern": r"\*\*版本\*\*：v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?",
        "replacement": lambda v: f"**版本**：v{v}",
    })

    # English doc version headers (BYO guides and gitops)
    rules.append({
        "file": "docs/byo-prometheus-integration.en.md",
        "desc": "BYOP guide (en) version",
        "pattern": r"\*\*Version\*\*: v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?",
        "replacement": lambda v: f"**Version**: v{v}",
    })
    rules.append({
        "file": "docs/byo-alertmanager-integration.en.md",
        "desc": "BYO Alertmanager guide (en) version",
        "pattern": r"\*\*Version\*\*: v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?",
        "replacement": lambda v: f"**Version**: v{v}",
    })
    rules.append({
        "file": "docs/gitops-deployment.en.md",
        "desc": "gitops-deployment.en.md version header",
        "pattern": r"\*\*Version\*\*: v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?",
        "replacement": lambda v: f"**Version**: v{v}",
    })

    # Federation integration guide version header
    rules.append({
        "file": "docs/federation-integration.md",
        "desc": "federation-integration.md version header",
        "pattern": r"> \*\*v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?\*\*",
        "replacement": lambda v: f"> **v{v}**",
    })
    rules.append({
        "file": "docs/federation-integration.en.md",
        "desc": "federation-integration.en.md version header",
        "pattern": r"> \*\*v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?\*\*",
        "replacement": lambda v: f"> **v{v}**",
    })

    # threshold-exporter README title
    rules.append({
        "file": "components/threshold-exporter/README.md",
        "desc": "threshold-exporter README title version",
        "pattern": r"# Threshold Exporter \(v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?\)",
        "replacement": lambda v: f"# Threshold Exporter (v{v})",
    })

    # NOTE: Chart.yaml version 已移至 _build_exporter_rules()

    # CLAUDE.md project overview (only the "## 專案概覽 (vX.Y.Z)" line)
    rules.append({
        "file": "CLAUDE.md",
        "desc": "CLAUDE.md project overview version",
        "pattern": r"專案概覽 \(v[0-9]+\.[0-9]+[^)]*\)",
        "replacement": lambda v: f"專案概覽 (v{v})",
    })

    # da-tools README platform version reference
    rules.append({
        "file": "components/da-tools/README.md",
        "desc": "da-tools README platform version ref",
        "pattern": r"平台版本（v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?\+）",
        "replacement": lambda v: f"平台版本（v{v}+）",
    })
    rules.append({
        "file": "components/da-tools/README.md",
        "desc": "da-tools version strategy table (platform row)",
        "pattern": r"\| 平台文件 \| v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?",
        "replacement": lambda v: f"| 平台文件 | v{v}",
    })

    # Front matter `version: vX.Y.Z` in all docs/ .md and .jsx files
    for ext in ("**/*.md", "**/*.jsx"):
        rules.append({
            "file": "__glob__",
            "glob_dir": "docs",
            "glob_pattern": ext,
            "desc": f"front matter version: in docs/{ext}",
            "pattern": r"(?<=\n)version:\s*v[0-9]+\.[0-9]+[^\n]*(?=\n)",
            "replacement": lambda v: f"version: v{v}",
        })

    # Doc header blockquote pattern: `> **vX.Y.Z |` (common in doc headers)
    rules.append({
        "file": "__glob__",
        "glob_dir": "docs",
        "glob_pattern": "**/*.md",
        "desc": "doc header blockquote version (> **vX.Y.Z |)",
        "pattern": r"> \*\*v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?\*\*\s*\|",
        "replacement": lambda v: f"> **v{v}** |",
    })

    # Inline text version in doc headers: **v2.0.0-preview** 統一採集 etc.
    rules.append({
        "file": "__glob__",
        "glob_dir": "docs",
        "glob_pattern": "**/*.md",
        "desc": "inline doc header version (bold blockquote, no pipe)",
        "pattern": r"> \*\*v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?\*\*\s*$",
        "replacement": lambda v: f"> **v{v}**",
    })

    # Inline version text: `於 v2.0.0 統一採集` or similar inline version strings in doc content
    rules.append({
        "file": "__glob__",
        "glob_dir": "docs",
        "glob_pattern": "**/*.md",
        "desc": "inline version text in doc content",
        "pattern": r"於\s+v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?(?=\s|\）|。)",
        "replacement": lambda v: f"於 v{v}",
    })

    # **版本**：vX.Y.Z（與... pattern common in doc headers
    rules.append({
        "file": "__glob__",
        "glob_dir": "docs",
        "glob_pattern": "**/*.md",
        "desc": "doc header **版本**：vX.Y.Z pattern",
        "pattern": r"\*\*版本\*\*：v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?(?=（|：)",
        "replacement": lambda v: f"**版本**：v{v}",
    })

    # Footer pattern: **最後更新**：v2.0.0 |
    rules.append({
        "file": "__glob__",
        "glob_dir": "docs",
        "glob_pattern": "**/*.md",
        "desc": "doc footer **最後更新**：vX.Y.Z pattern",
        "pattern": r"\*\*最後更新\*\*：v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?(?=\s*\|)",
        "replacement": lambda v: f"**最後更新**：v{v}",
    })

    # JSON schema "version" field: docs/schemas files
    rules.append({
        "file": "docs/schemas/tenant-config.schema.json",
        "desc": "tenant-config.schema.json version field",
        "pattern": r'"version"\s*:\s*"v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?"',
        "replacement": lambda v: f'"version": "v{v}"',
    })

    # docs/schemas/README.md version header
    rules.append({
        "file": "docs/schemas/README.md",
        "desc": "schemas README version header",
        "pattern": r"\*\*Version\*\*:\s*v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?",
        "replacement": lambda v: f"**Version**: v{v}",
    })

    # Badge data JSON: docs/assets/badge-data.json
    rules.append({
        "file": "docs/assets/badge-data.json",
        "desc": "badge-data.json version field",
        "pattern": r'"version"\s*:\s*"v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?"',
        "replacement": lambda v: f'"version": "v{v}"',
    })

    # mkdocs.yml extra.platform_version / tools_version
    rules.append({
        "file": "mkdocs.yml",
        "desc": "mkdocs.yml extra.platform_version",
        "pattern": r'platform_version:\s*\"[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?"',
        "replacement": lambda v: f'platform_version: "{v}"',
    })

    # README.md / README.en.md intro version
    rules.append({
        "file": "README.md",
        "desc": "README.md intro version",
        "pattern": r"治理平台\*\* v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?",
        "replacement": lambda v: f"治理平台** v{v}",
    })
    rules.append({
        "file": "README.en.md",
        "desc": "README.en.md intro version",
        "pattern": r"Governance Platform\*\* v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?",
        "replacement": lambda v: f"Governance Platform** v{v}",
    })

    # Interactive HTML files version subtitle
    rules.append({
        "file": "docs/interactive/index.html",
        "desc": "interactive index.html subtitle version",
        "pattern": r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?\s+—\s+Multi-Tenant",
        "replacement": lambda v: f"v{v} — Multi-Tenant",
    })

    # Interactive JSX front matter and version consistency
    rules.append({
        "file": "docs/interactive/tools/cli-playground.jsx",
        "desc": "cli-playground.jsx front matter version",
        "pattern": r"(?<=\n)version:\s*v[0-9]+\.[0-9]+[^\n]*(?=\n)",
        "replacement": lambda v: f"version: v{v}",
    })

    rules.append({
        "file": "docs/interactive/tools/cli-playground.jsx",
        "desc": "cli-playground.jsx version consistency output",
        "pattern": r"\[✓\]\s+Version consistency\s+v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?",
        "replacement": lambda v: f"[✓] Version consistency  v{v}",
    })

    rules.append({
        "file": "docs/interactive/tools/platform-demo.jsx",
        "desc": "platform-demo.jsx version display",
        "pattern": r"(?<=\n)version:\s*v[0-9]+\.[0-9]+[^\n]*(?=\n)",
        "replacement": lambda v: f"version: v{v}",
    })

    # Python tools fallback version string: generate_cheat_sheet.py
    rules.append({
        "file": "scripts/tools/dx/generate_cheat_sheet.py",
        "desc": "generate_cheat_sheet.py platform version fallback",
        "pattern": r"version\s*=\s*'v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?'(?=\s*#\s*fallback)",
        "replacement": lambda v: f"version = 'v{v}'",
    })

    return rules


def _build_rules():
    """Build all version replacement rules, grouped by version line.

    Returns {"platform": [...], "exporter": [...], "tools": [...], "tenant-api": [...]}.
    """
    return {
        "platform": _build_platform_rules(),
        "exporter": _build_exporter_rules(),
        "tools": _build_tools_rules(),
        "tenant-api": _build_tenant_api_rules(),
    }


# ---------------------------------------------------------------------------
# Count sync: automatic count updates for metrics scattered across docs
# ---------------------------------------------------------------------------

def _count_python_tools():
    """Count Python tools in scripts/tools/{ops,dx,lint}/ directories.

    Returns (total_count, ops_count, dx_count, lint_count).
    """
    ops_dir = REPO_ROOT / "scripts" / "tools" / "ops"
    dx_dir = REPO_ROOT / "scripts" / "tools" / "dx"
    lint_dir = REPO_ROOT / "scripts" / "tools" / "lint"

    ops_count = len(list(ops_dir.glob("*.py"))) if ops_dir.exists() else 0
    dx_count = len(list(dx_dir.glob("*.py"))) if dx_dir.exists() else 0
    lint_count = len(list(lint_dir.glob("*.py"))) if lint_dir.exists() else 0

    total = ops_count + dx_count + lint_count
    return total, ops_count, dx_count, lint_count


def _count_rule_packs():
    """Count Rule Packs from platform-data.json (source of truth).

    Falls back to counting configmap-rules-*.yaml in k8s/03-monitoring/.
    platform-data.json includes all packs (14 optional yaml + 1 platform ConfigMap = 15).
    """
    # Primary: platform-data.json is the source of truth
    platform_data = REPO_ROOT / "docs" / "assets" / "platform-data.json"
    if platform_data.exists():
        import json
        try:
            data = json.loads(platform_data.read_text(encoding="utf-8"))
            packs = data.get("rulePacks", {})
            if isinstance(packs, (dict, list)) and len(packs) > 0:
                return len(packs)
        except (json.JSONDecodeError, KeyError):
            pass

    # Fallback: count yaml files
    monitoring_dir = REPO_ROOT / "k8s" / "03-monitoring"
    if not monitoring_dir.exists():
        return 0
    rule_packs = [f for f in monitoring_dir.glob("configmap-rules-*.yaml")
                  if not f.name.endswith("-platform.yaml")]
    return len(rule_packs)


def _count_jsx_tools():
    """Count interactive tools registered in docs/assets/tool-registry.yaml.

    Returns count of tools (by counting '- key:' entries).
    """
    registry = REPO_ROOT / "docs" / "assets" / "tool-registry.yaml"
    if not registry.exists():
        return 0

    content = registry.read_text(encoding="utf-8")
    count = len(re.findall(r"^  - key:", content, re.MULTILINE))
    return count


def _count_docs():
    """Count documentation files in docs/ directory.

    Returns count of *.md files.
    """
    docs_dir = REPO_ROOT / "docs"
    if not docs_dir.exists():
        return 0

    count = len(list(docs_dir.glob("**/*.md")))
    return count


def _count_precommit_hooks():
    """Count pre-commit hooks in .pre-commit-config.yaml.

    Returns count of hooks (by counting '- id:' entries).
    """
    config = REPO_ROOT / ".pre-commit-config.yaml"
    if not config.exists():
        return 0

    content = config.read_text(encoding="utf-8")
    count = len(re.findall(r"^\s+- id:", content, re.MULTILINE))
    return count


def _build_count_rules():
    """Build count replacement rules for CLAUDE.md and README.md.

    Returns list of rule dicts for count syncing.
    """
    total_tools, ops_tools, dx_tools, lint_tools = _count_python_tools()
    rule_packs = _count_rule_packs()
    jsx_tools = _count_jsx_tools()
    docs = _count_docs()
    hooks = _count_precommit_hooks()

    rules = []

    # CLAUDE.md: 84 個 Python 工具（不含共用函式庫）
    if total_tools > 0:
        rules.append({
            "file": "CLAUDE.md",
            "desc": f"CLAUDE.md: Python tools total ({total_tools} tools)",
            "pattern": r"(\d+)\s*個\s*Python\s*工具（不含共用函式庫）",
            "replacement": lambda _: f"{total_tools} 個 Python 工具（不含共用函式庫）",
            "is_count": True,
        })

    # CLAUDE.md: ops/ count in table (currently 44)
    if ops_tools > 0:
        rules.append({
            "file": "CLAUDE.md",
            "desc": f"CLAUDE.md: ops/ tools ({ops_tools} tools)",
            "pattern": r"\| `ops/` \| 運維工具.*?\| \d+ \|",
            "replacement": lambda _: f"| `ops/` | 運維工具（scaffold, diagnose, migrate, validate, alert-quality, alert-correlate, drift-detect, policy, forecast, notification-test, threshold-recommend, tenant-mapping, explain-route, discover-mappings, init, config-history, gitops-check, operator-generate, operator-check, rule-pack-split, policy-opa-bridge...） | {ops_tools} |",
            "is_count": True,
        })

    # CLAUDE.md: dx/ count in table (currently 20)
    if dx_tools > 0:
        rules.append({
            "file": "CLAUDE.md",
            "desc": f"CLAUDE.md: dx/ tools ({dx_tools} tools)",
            "pattern": r"\| `dx/` \| DX.*?\| \d+ \|",
            "replacement": lambda _: f"| `dx/` | DX 自動化（generate_*, bump_docs, sync_*, coverage_gap_analysis, generate_tenant_metadata...） | {dx_tools} |",
            "is_count": True,
        })

    # CLAUDE.md: lint/ count in table (currently 19)
    if lint_tools > 0:
        rules.append({
            "file": "CLAUDE.md",
            "desc": f"CLAUDE.md: lint/ tools ({lint_tools} tools)",
            "pattern": r"\| `lint/` \| 文件.*?\| \d+ \|",
            "replacement": lambda _: f"| `lint/` | 文件 CI lint（check_*, validate_docs_*, lint_*, check_cli_coverage, check_bilingual_content, check_frontmatter_versions, check_routing_profiles, check_doc_template, check_portal_i18n...） | {lint_tools} |",
            "is_count": True,
        })

    # CLAUDE.md: 15 個 Rule Pack in architecture section
    if rule_packs > 0:
        rules.append({
            "file": "CLAUDE.md",
            "desc": f"CLAUDE.md: Rule Pack count ({rule_packs} packs)",
            "pattern": r"(\d+)\s+個\s+Rule\s+Pack（(\d+)\s+個\s+optional",
            "replacement": lambda _: f"{rule_packs} 個 Rule Pack（{rule_packs - 1} 個 optional",
            "is_count": True,
        })

    # CLAUDE.md: 30 JSX tools in section header
    if jsx_tools > 0:
        rules.append({
            "file": "CLAUDE.md",
            "desc": f"CLAUDE.md: JSX tools ({jsx_tools} tools)",
            "pattern": r"互動工具生態（(\d+)\s+JSX\s+tools）",
            "replacement": lambda _: f"互動工具生態（{jsx_tools} JSX tools）",
            "is_count": True,
        })

    # CLAUDE.md: 91 個文件 in doc navigation section
    if docs > 0:
        rules.append({
            "file": "CLAUDE.md",
            "desc": f"CLAUDE.md: docs count ({docs} files)",
            "pattern": r"完整文件對照表（(\d+)\s+個文件，含受眾與內容摘要）",
            "replacement": lambda _: f"完整文件對照表（{docs} 個文件，含受眾與內容摘要）",
            "is_count": True,
        })

    # CLAUDE.md: 13 個 auto-run hooks
    if hooks > 0:
        rules.append({
            "file": "CLAUDE.md",
            "desc": f"CLAUDE.md: pre-commit hooks ({hooks} hooks)",
            "pattern": r"(\d+)\s+個\s+auto-run\s+hooks（每次\s+commit）",
            "replacement": lambda _: f"{hooks} 個 auto-run hooks（每次 commit）",
            "is_count": True,
        })

    # README.md: 15 個 Rule Pack (in badge)
    if rule_packs > 0:
        rules.append({
            "file": "README.md",
            "desc": f"README.md: Rule Pack badge ({rule_packs} packs)",
            "pattern": r"badge/rule%20packs-(\d+)-orange",
            "replacement": lambda _: f"badge/rule%20packs-{rule_packs}-orange",
            "is_count": True,
        })

    return rules


def apply_count_updates(check_only=False, dry_run=False, verbose=False):
    """Apply count replacement rules across docs.

    Args:
        check_only: If True, don't modify files (for --check mode).
        dry_run: If True, don't modify files but show before→after diffs.
        verbose: If True, show detailed output.

    Returns list of (status, desc, detail) tuples.
    """
    rules = _build_count_rules()
    changes = []

    for rule in rules:
        fpath = REPO_ROOT / rule["file"]
        if not fpath.exists():
            changes.append(("SKIP", rule["desc"], f"file not found: {rule['file']}"))
            continue

        content = fpath.read_text(encoding="utf-8")
        pattern = rule["pattern"]
        replacement = rule["replacement"](None)

        matches = re.findall(pattern, content, re.MULTILINE)
        if not matches:
            changes.append(("OK", rule["desc"], "no match (pattern not found)"))
            continue

        # Check if update is needed
        new_content = re.sub(pattern, replacement, content, flags=re.MULTILINE)
        if new_content == content:
            changes.append(("OK", rule["desc"], "already up to date"))
        else:
            unique_old = sorted(set(matches))
            diff_detail = (f"replaced {len(matches)} occurrence(s): "
                          f"{unique_old[0]} → {replacement}")
            if dry_run:
                diff_detail = f"[dry-run] {diff_detail}"
            changes.append(("UPDATE", rule["desc"], diff_detail))
            if not check_only and not dry_run:
                fpath.write_text(new_content, encoding="utf-8")
                os.chmod(fpath, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)

    return changes


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def read_current_versions():
    """Read current versions from source-of-truth files."""
    versions = {}

    # Exporter version from Chart.yaml (version = appVersion = exporter version)
    if CHART_YAML.exists():
        content = CHART_YAML.read_text(encoding="utf-8")
        m = re.search(r'^appVersion:\s*"([0-9]+\.[0-9]+\.[0-9]+)"', content, re.MULTILINE)
        if m:
            versions["exporter"] = m.group(1)

    # Platform version from CLAUDE.md "專案概覽 (vX.Y.Z)"
    claude_md = REPO_ROOT / "CLAUDE.md"
    if claude_md.exists():
        content = claude_md.read_text(encoding="utf-8")
        m = re.search(r"專案概覽 \(v([0-9]+\.[0-9]+[^)]*)\)", content)
        if m:
            versions["platform"] = m.group(1)

    # da-tools version from VERSION file
    if DA_TOOLS_VERSION.exists():
        ver = DA_TOOLS_VERSION.read_text(encoding="utf-8").strip()
        if re.match(r"^[0-9]+\.[0-9]+\.[0-9]+$", ver):
            versions["tools"] = ver

    return versions


def _filter_by_scope(rules, scope):
    """Filter rules to only include files under scope directory."""
    if not scope:
        return rules
    # Normalize scope: strip trailing slash
    scope = scope.rstrip("/").rstrip("\\")
    filtered = []
    for rule in rules:
        f = rule.get("file", "")
        if f == "__glob__":
            # Check glob_dir
            if rule.get("glob_dir", "").startswith(scope) or scope == ".":
                filtered.append(rule)
        elif f.startswith(scope + "/") or f.startswith(scope + "\\"):
            filtered.append(rule)
        elif "/" not in f and "\\" not in f:
            # Root-level files: include if scope is "."
            if scope == ".":
                filtered.append(rule)
    return filtered


def _expand_glob_rules(rules):
    """Expand __glob__ rules into per-file rules."""
    expanded = []
    for rule in rules:
        if rule.get("file") == "__glob__":
            glob_dir = REPO_ROOT / rule["glob_dir"]
            for fpath in sorted(glob_dir.glob(rule["glob_pattern"])):
                rel = fpath.relative_to(REPO_ROOT)
                expanded.append({
                    "file": str(rel),
                    "desc": f"{rule['desc'].split(' in ')[0]} in {rel}",
                    "pattern": rule["pattern"],
                    "replacement": rule["replacement"],
                })
        else:
            expanded.append(rule)
    return expanded


def apply_rules(rules, new_version, check_only=False, dry_run=False):
    """Apply a set of replacement rules. Returns list of (status, desc, detail) tuples.

    Args:
        rules: Replacement rules from _build_rules().
        new_version: Target version string.
        check_only: If True, don't modify files (for --check mode).
        dry_run: If True, don't modify files but show before→after diffs.
    """
    rules = _expand_glob_rules(rules)
    changes = []
    for rule in rules:
        fpath = REPO_ROOT / rule["file"]
        if not fpath.exists():
            changes.append(("SKIP", rule["desc"], f"file not found: {rule['file']}"))
            continue

        content = fpath.read_text(encoding="utf-8")

        if rule.get("whole_file"):
            new_content = rule["replacement"](new_version)
            if content.strip() != new_content.strip():
                diff_detail = f"{content.strip()} → {new_content.strip()}"
                changes.append(("UPDATE", rule["desc"], diff_detail))
                if not check_only and not dry_run:
                    fpath.write_text(new_content, encoding="utf-8")
                    os.chmod(fpath, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
            else:
                changes.append(("OK", rule["desc"], "already up to date"))
            continue

        pattern = rule["pattern"]
        replacement = rule["replacement"](new_version)

        matches = re.findall(pattern, content, re.MULTILINE)
        if not matches:
            changes.append(("OK", rule["desc"], "no match (may already be updated)"))
            continue

        needs_update = any(m != replacement for m in matches)
        if needs_update:
            new_content = re.sub(pattern, replacement, content, flags=re.MULTILINE)
            # Build diff detail
            unique_old = sorted(set(matches))
            diff_detail = (f"replaced {len(matches)} occurrence(s): "
                           f"{unique_old[0]} → {replacement}")
            if dry_run:
                diff_detail = f"[dry-run] {diff_detail}"
            changes.append(("UPDATE", rule["desc"], diff_detail))
            if not check_only and not dry_run:
                fpath.write_text(new_content, encoding="utf-8")
                os.chmod(fpath, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
        else:
            changes.append(("OK", rule["desc"], "already up to date"))

    return changes


def _init_changelog_entry(version: str, lang: str = "zh"):
    """Insert a new version header stub at the top of CHANGELOG.

    Args:
        version: Semver string (without leading 'v').
        lang: 'zh' for CHANGELOG.md, 'en' for CHANGELOG.en.md,
              'all' for both.
    """
    from datetime import date

    targets = []
    if lang in ("zh", "all"):
        targets.append("zh")
    if lang in ("en", "all"):
        targets.append("en")

    today = date.today().isoformat()  # Local date — intentional for release notes

    for target_lang in targets:
        if target_lang == "zh":
            changelog = REPO_ROOT / "CHANGELOG.md"
            stub = (
                f"\n## [v{version}] — TITLE ({today})\n"
                f"\n"
                f"ONE-LINE SUMMARY\n"
                f"\n"
                f"### 版號\n"
                f"\n"
                f"- (填入版號變更)\n"
                f"\n"
                f"---\n"
            )
        else:
            changelog = REPO_ROOT / "CHANGELOG.en.md"
            stub = (
                f"\n## [v{version}] — TITLE ({today})\n"
                f"\n"
                f"ONE-LINE SUMMARY\n"
                f"\n"
                f"### Versions\n"
                f"\n"
                f"- (fill in version changes)\n"
                f"\n"
                f"---\n"
            )

        if not changelog.exists():
            # Create new file with minimal front matter
            if target_lang == "en":
                initial = (
                    "---\n"
                    "title: Changelog (English)\n"
                    "---\n"
                    "\n"
                    "# Changelog\n"
                )
                changelog.write_text(initial + stub + "\n",
                                     encoding="utf-8")
                os.chmod(changelog,
                         stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP
                         | stat.S_IROTH)
                print(f"✅ Created {changelog.name} with v{version} stub "
                      f"({today})")
                continue
            else:
                print(f"ERROR: {changelog} not found", file=sys.stderr)
                sys.exit(1)

        content = changelog.read_text(encoding="utf-8")

        # Insert after front matter (after second ---) and first blank line
        fm_end = 0
        if content.startswith("---"):
            second_dash = content.find("---", 3)
            if second_dash != -1:
                fm_end = content.find("\n", second_dash) + 1

        # Find first ## heading (existing first version entry)
        first_heading = content.find("\n## ", fm_end)
        if first_heading == -1:
            insert_pos = fm_end
        else:
            insert_pos = first_heading

        new_content = content[:insert_pos] + stub + content[insert_pos:]
        changelog.write_text(new_content, encoding="utf-8")
        os.chmod(changelog,
                 stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP
                 | stat.S_IROTH)
        print(f"✅ Inserted v{version} stub into {changelog.name} "
              f"({today})")


def main():
    """CLI entry point: 版號一致性管理工具."""
    parser = argparse.ArgumentParser(
        description="Bump version references across docs and configs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--platform", metavar="VER",
                        help="New platform version (e.g. 0.10.0)")
    parser.add_argument("--exporter", metavar="VER",
                        help="New exporter version (e.g. 0.6.0)")
    parser.add_argument("--tools", metavar="VER",
                        help="New da-tools version (e.g. 0.2.0)")
    parser.add_argument("--tenant-api", metavar="VER",
                        help="New tenant-api version (e.g. 2.4.0)")
    parser.add_argument("--check", action="store_true",
                        help="Check only, don't modify files (exit 1 if outdated)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show before→after diffs without modifying files")
    parser.add_argument("--scope", metavar="DIR",
                        help="Limit to files under DIR (e.g. docs, components)")
    parser.add_argument("--init-changelog", metavar="VER",
                        help="Insert new CHANGELOG version header stub")
    parser.add_argument("--changelog-lang", choices=["zh", "en", "all"],
                        default="zh",
                        help="Language for --init-changelog: zh (default), "
                             "en, or all")
    parser.add_argument("--show-current", action="store_true",
                        help="Show current versions from source-of-truth files")
    parser.add_argument("--what-if", action="store_true",
                        help="Show all rules with current match status "
                             "(comprehensive rule audit)")
    parser.add_argument("--sync-counts", action="store_true",
                        help="Auto-update hardcoded counts (tools, rule packs, "
                             "JSX tools, docs, hooks) across CLAUDE.md and README.md")

    args = parser.parse_args()

    # --sync-counts: auto-update all hardcoded counts
    if args.sync_counts:
        total_tools, ops_tools, dx_tools, lint_tools = _count_python_tools()
        rule_packs = _count_rule_packs()
        jsx_tools = _count_jsx_tools()
        docs = _count_docs()
        hooks = _count_precommit_hooks()

        print("Current counts detected:")
        print(f"  Python tools (total): {total_tools}")
        print(f"    - ops/: {ops_tools}")
        print(f"    - dx/: {dx_tools}")
        print(f"    - lint/: {lint_tools}")
        print(f"  Rule Packs: {rule_packs}")
        print(f"  JSX tools: {jsx_tools}")
        print(f"  Documentation files: {docs}")
        print(f"  Pre-commit hooks: {hooks}")
        print()

        changes = apply_count_updates(check_only=args.check, dry_run=args.dry_run)
        for status, desc, detail in changes:
            icon = {"UPDATE": "📝", "OK": "✅", "SKIP": "⚠️ "}[status]
            print(f"  {icon} {desc}: {detail}")

        update_count = sum(1 for s, _, _ in changes if s == "UPDATE")
        if args.check:
            if update_count > 0:
                print(f"\n❌ {update_count} count(s) are outdated. Run without --check to apply.")
                sys.exit(1)
            else:
                print("\n✅ All counts are already up to date.")
        elif args.dry_run:
            if update_count > 0:
                print(f"\n🔍 Dry run: {update_count} count(s) would be updated.")
            else:
                print("\n✅ Dry run: all counts are already up to date.")
        else:
            print(f"\n✅ Done. {update_count} count(s) updated.")
        return

    # --init-changelog: insert a new version stub at the top of CHANGELOG.md
    if args.init_changelog:
        _init_changelog_entry(args.init_changelog.lstrip("v"),
                              lang=args.changelog_lang)
        return

    if args.show_current:
        versions = read_current_versions()
        print("Current versions (from source-of-truth files):")
        for line, ver in sorted(versions.items()):
            print(f"  {line}: {ver}")
        return

    # --what-if: comprehensive rule audit — show all rules and their status
    if args.what_if:
        versions = read_current_versions()
        if not versions:
            print("ERROR: Cannot read current versions from source files", file=sys.stderr)
            sys.exit(1)

        all_rules = _build_rules()
        total_rules = 0
        matched = 0
        unmatched = 0
        missing = 0

        for line in ("platform", "exporter", "tools", "tenant-api"):
            ver = versions.get(line)
            if not ver:
                print(f"\n⚠️  {line}: version not found in source-of-truth")
                continue

            rules = _expand_glob_rules(
                _filter_by_scope(all_rules.get(line, []), args.scope))

            print(f"\n{'='*60}")
            print(f"  {line.upper()} (current: {ver}) — "
                  f"{len(rules)} rule(s)")
            print(f"{'='*60}")

            for rule in rules:
                total_rules += 1
                fpath = REPO_ROOT / rule["file"]
                desc = rule["desc"]

                if not fpath.exists():
                    missing += 1
                    print(f"  ⚠️  {desc}")
                    print(f"       file not found: {rule['file']}")
                    continue

                content = fpath.read_text(encoding="utf-8")
                pattern = rule["pattern"]
                replacement = rule["replacement"](ver)

                if rule.get("whole_file"):
                    if content.strip() == replacement.strip():
                        matched += 1
                        print(f"  ✅ {desc}")
                        print(f"       matched: {content.strip()}")
                    else:
                        unmatched += 1
                        print(f"  ❌ {desc}")
                        print(f"       current: {content.strip()}")
                        print(f"       expected: {replacement.strip()}")
                    continue

                matches = re.findall(pattern, content, re.MULTILINE)
                if not matches:
                    matched += 1
                    print(f"  ✅ {desc}")
                    print(f"       no match (pattern already resolved)")
                elif all(m == replacement for m in matches):
                    matched += 1
                    print(f"  ✅ {desc}")
                    print(f"       matched: {replacement} "
                          f"({len(matches)} occurrence(s))")
                else:
                    unmatched += 1
                    unique = sorted(set(matches))
                    print(f"  ❌ {desc}")
                    print(f"       found: {unique}")
                    print(f"       expected: {replacement}")

        print(f"\n{'='*60}")
        print(f"  Summary: {total_rules} rules, "
              f"{matched} ✅, {unmatched} ❌, {missing} ⚠️")
        print(f"{'='*60}")
        sys.exit(1 if unmatched > 0 else 0)

    # --check mode: read current versions and verify all references match
    if args.check and not (args.platform or args.exporter or args.tools):
        versions = read_current_versions()
        if not versions:
            print("ERROR: Cannot read current versions from source files", file=sys.stderr)
            sys.exit(1)

        all_rules = _build_rules()
        has_drift = False

        for line, ver in versions.items():
            rules = _filter_by_scope(all_rules.get(line, []), args.scope)
            changes = apply_rules(rules, ver, check_only=True)
            for status, desc, detail in changes:
                if status == "UPDATE":
                    has_drift = True
                    print(f"  DRIFT  [{line}] {desc}: {detail}")
                elif status == "SKIP":
                    print(f"  SKIP   [{line}] {desc}: {detail}")

        if has_drift:
            print("\n❌ Version drift detected. Run bump_docs.py with version flags to fix.")
            sys.exit(1)
        else:
            print("✅ All version references are consistent.")
            sys.exit(0)

    # Explicit bump mode
    if not (args.platform or args.exporter or args.tools or args.tenant_api):
        parser.print_help()
        sys.exit(1)

    all_rules = _build_rules()
    total_updates = 0

    for line, new_ver in [("platform", args.platform),
                          ("exporter", args.exporter),
                          ("tools", args.tools),
                          ("tenant-api", args.tenant_api)]:
        if not new_ver:
            continue

        # Strip leading 'v' if provided
        new_ver = new_ver.lstrip("v")

        print(f"\n{'='*60}")
        print(f"  {line.upper()} → {new_ver}")
        print(f"{'='*60}")

        rules = _filter_by_scope(all_rules.get(line, []), args.scope)
        changes = apply_rules(rules, new_ver,
                              check_only=args.check, dry_run=args.dry_run)

        for status, desc, detail in changes:
            icon = {"UPDATE": "📝", "OK": "✅", "SKIP": "⚠️ "}[status]
            print(f"  {icon} {desc}: {detail}")
            if status == "UPDATE":
                total_updates += 1

    if args.check:
        if total_updates > 0:
            print(f"\n❌ {total_updates} file(s) would be updated. Run without --check to apply.")
            sys.exit(1)
        else:
            print("\n✅ All version references are already up to date.")
    elif args.dry_run:
        if total_updates > 0:
            print(f"\n🔍 Dry run: {total_updates} file(s) would be updated.")
        else:
            print("\n✅ Dry run: all version references are already up to date.")
    else:
        print(f"\n✅ Done. {total_updates} update(s) applied.")


if __name__ == "__main__":
    main()
