#!/usr/bin/env python3
"""bump_docs.py — 版號一致性管理工具

掃描 repo 中的文件、Chart.yaml、VERSION 檔案，批次更新版號引用。
六條版號線獨立管理：--platform / --exporter / --tools / --portal / --recipe-preview / --tenant-api。

Chart.yaml version 與 appVersion 同步，統一由 --exporter 管理。
--exporter 同時更新：Chart.yaml version + appVersion + image tag + OCI chart references。

用法:
  # 更新 exporter 版號 (Chart.yaml version + appVersion + image tag + OCI chart)
  python3 scripts/tools/bump_docs.py --exporter 1.1.0

  # 更新 da-tools 版號 (所有 image tag + VERSION)
  python3 scripts/tools/bump_docs.py --tools 1.1.0

  # 更新 da-portal 版號 (Chart.yaml + README 標題 + image tag + OCI chart)
  python3 scripts/tools/bump_docs.py --portal 2.8.0

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

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402

# ---------------------------------------------------------------------------
# Repo root detection
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent.parent  # scripts/tools/dx/ -> repo root

# ---------------------------------------------------------------------------
# Version source-of-truth files
# ---------------------------------------------------------------------------
CHART_YAML = REPO_ROOT / "helm" / "threshold-exporter" / "Chart.yaml"
DA_TOOLS_VERSION = REPO_ROOT / "components" / "da-tools" / "app" / "VERSION"
TENANT_API_CHART_YAML = REPO_ROOT / "helm" / "tenant-api" / "Chart.yaml"
DA_PORTAL_CHART_YAML = REPO_ROOT / "helm" / "da-portal" / "Chart.yaml"
RECIPE_PREVIEW_CHART_YAML = REPO_ROOT / "helm" / "recipe-preview" / "Chart.yaml"

# ---------------------------------------------------------------------------
# Replacement rules per version line
# ---------------------------------------------------------------------------
# Each rule: (file_relative_path, pattern_func, replacement_func)
# pattern_func(old_ver) -> regex pattern
# replacement_func(new_ver) -> replacement string

# Interactive portal sources. TRK-230 (Option C) moved every JSX tool out of
# `docs/interactive/tools/` to here; esbuild bundles them into
# docs/assets/dist/. Rules that still globbed `docs/**/*.jsx` after the move
# expanded to nothing and went silent — hence the single named constant, so a
# future move breaks one line instead of drifting rule by rule (#1407).
PORTAL_JSX_DIR = "tools/portal/src/interactive/tools"

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
    # Portal JSX/JS sources. The glob USED to say glob_dir "docs" +
    # "**/*.jsx", but the JSX moved to tools/portal/src/ in TRK-230 and
    # `find docs -name '*.jsx'` has returned 0 ever since. A glob that
    # expands to zero files emits zero rules — so it was invisible to
    # EVERY gate: nothing to count, nothing to mark DEAD, nothing to
    # fail on (#1407). See the per-glob expansion floor in
    # tests/dx/test_bump_docs.py, which now makes that collapse a test
    # failure instead of silence.
    #
    # The `.js` sibling is here because the same image pin lives in the
    # non-JSX helpers under cli-playground/ — repointing only the .jsx
    # glob would have left an identically stale tag next door.
    for _ext in ("**/*.jsx", "**/*.js"):
        rules.append({
            "file": "__glob__",
            "glob_dir": PORTAL_JSX_DIR,
            "glob_pattern": _ext,
            "desc": f"da-tools image tag in {PORTAL_JSX_DIR}/{_ext}",
            "pattern": r"ghcr\.io/vencil/da-tools:v?[0-9]+\.[0-9]+\.[0-9]+",
            "replacement": lambda v: f"ghcr.io/vencil/da-tools:v{v}",
        })
    # ⚠️ README.md, README.en.md and components/threshold-exporter/README.md
    # were in this list and matched nothing: none of them pins a da-tools
    # image any more. They name the image unpinned in prose
    # (`ghcr.io/vencil/da-tools` container) or use `:latest`. Only the
    # da-tools README carries real `:vX.Y.Z` pins, so only it keeps a rule.
    #
    # ⛔ The portal test is in this list on purpose. `cli-playground/engine.js`
    # emits the pinned image into the command it builds, and the vitest suite
    # asserts that command as a literal string — so bumping the source without
    # the expectation turns the portal suite red for a reason that has nothing
    # to do with what it tests (command assembly). Measured: repointing the
    # portal glob in this same change did exactly that.
    #
    # This does NOT make the assertion tautological. The test grades the
    # docker wrapper — flags, env, argument order — and the version is
    # incidental to it; keeping the two in step is the same service this tool
    # performs for every README that quotes a pinned image.
    for f in ["components/da-tools/README.md",
              "tools/portal/tests/cli-playground-engine.test.ts"]:
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

    # da-tools README H1 title — `# da-tools (vX.Y.Z)`. This replaced the
    # old `**版本**：X.Y.Z（獨立版號` rule, which matched nothing after the
    # README adopted the `# component (vX.Y.Z)` title format — a dead rule
    # is exactly how that title silently drifted to a stale version.
    rules.append({
        "file": "components/da-tools/README.md",
        "desc": "da-tools README title version",
        "pattern": r"# da-tools \(v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?\)",
        "replacement": lambda v: f"# da-tools (v{v})",
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
    #
    # 這裡只列「repo 內真的存在」的檔案。`.gitlab/ci/config-diff.gitlab-ci.yml`
    # 曾在此列，但該檔在 v2.1.0 就被刪了——GitLab 那條路徑現在由
    # `da-tools init --ci gitlab` 產生，不是 checked-in 範本。規則指向不存在的
    # 檔案時 apply_rules() 只會回 SKIP，過去所有 gate 都不看 SKIP，於是這條
    # 規則靜靜死了五個版本（#1407）。B 段已讓 missing 變成 failing signal。
    for f in [".github/workflows/config-diff.yaml",
              "k8s/03-monitoring/cronjob-maintenance-scheduler.yaml",
              "k8s/03-monitoring/cronjob-threshold-govern.yaml"]:
        rules.append({
            "file": f,
            "desc": f"da-tools image tag in {f}",
            "pattern": r"ghcr\.io/vencil/da-tools:v?[0-9]+\.[0-9]+\.[0-9]+",
            "replacement": lambda v: f"ghcr.io/vencil/da-tools:v{v}",
        })

    # helm/federation-reconciler — the chart RUNS a da-tools image, so its pin
    # is on the da-tools release line, not the chart's own `version:`.
    #
    # This pin was invisible to bump_docs for two releases: a Helm values pin is
    # `repository:` + `tag:` on TWO lines, and every rule here is line-oriented.
    # The consequence was not merely a stale doc — it was structural: the
    # image-pin capability gate (
    # scripts/tools/lint/check_image_pin_capability.py) exempts this chart *because* v2.9.0 lacks
    # the reconciler script, and that exemption's exit condition is "the pin
    # gets bumped". With nothing mechanically bumping the pin, the exemption
    # could never go stale, so a critical ADR-028 control would stay broken
    # with the gate green forever. These two rules ARE that exit driver.
    #
    # Both are `require_match`: if the shape moves, they must die LOUDLY.
    rules.append({
        "file": "helm/federation-reconciler/values.yaml",
        "desc": "da-tools image tag in helm/federation-reconciler/values.yaml (two-line repository/tag pin)",
        "pair_anchor": r"^[ \t]*repository:[ \t]*ghcr\.io/vencil/da-tools[ \t]*$",
        "pair_key": "tag",
        "pattern": r'"v?' + _SEMVER_STRICT + r'"',
        "replacement": lambda v: f'"v{v}"',
        "require_match": True,
    })
    rules.append({
        "file": "helm/federation-reconciler/Chart.yaml",
        "desc": "federation-reconciler Chart.yaml appVersion (the da-tools image it ships)",
        "pattern": r'^appVersion:\s*"v?' + _SEMVER_STRICT + '"',
        "replacement": lambda v: f'appVersion: "v{v}"',
        "require_match": True,
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
    # ⚠️ There is deliberately NO rule for helm/tenant-api/Chart.yaml
    # `appVersion`. Unlike every other chart here, tenant-api's appVersion is
    # decoupled from its version ON PURPOSE: `version` tracks chart/template
    # changes (and is gated against the git tag by the "Verify Chart.yaml
    # version matches tag" step in .github/workflows/release.yaml), while appVersion
    # names the last PUBLISHED binary, which release.yaml's L3 digest step
    # probes as `:v${appVersion}` — see the ⚠️ comment above "Verify image
    # digest" in the release-tenant-api job, and the Chart.yaml comment
    # explaining why it is not bumped in feature PRs.
    #
    # A rule was here (pattern `^appVersion:\s+<semver>`) and it never fired
    # only because the value is quoted (`appVersion: "2.7.0"`). Had the quotes
    # ever gone away, `--tenant-api X` would have silently overwritten the
    # pinned binary version with the chart version and broken that invariant.
    # Deleted rather than "fixed": the correct behaviour is no rule at all.

    # Dockerfile LABEL version
    rules.append({
        "file": "components/tenant-api/Dockerfile",
        "desc": "tenant-api Dockerfile LABEL version",
        "pattern": r'org\.opencontainers\.image\.version="' + _SEMVER_STRICT + '"',
        "replacement": lambda v: f'org.opencontainers.image.version="{v}"',
    })

    # ⚠️ No rule for the README's `helm install ... oci://.../tenant-api`
    # snippet. It deliberately carries NO `--version` flag — the README says
    # 「版本見 Releases / CHANGELOG；省略 --version 取最新，或 --version <x.y.z>
    # 釘版」, i.e. unpinned-by-design so the doc cannot go stale. The old rule
    # required a literal `--version <semver>` there and so matched nothing
    # forever. Deleted, not repointed: there is no version string to drive.

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
            "file": "helm/threshold-exporter/Chart.yaml",
            "desc": "Chart.yaml version (chart release)",
            "pattern": r"^version:\s*[0-9]+\.[0-9]+\.[0-9]+",
            "replacement": lambda v: f"version: {v}",
        },
        {
            "file": "helm/threshold-exporter/Chart.yaml",
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
        # ⚠️ Deleted: the same OCI `--version` rule for README.md and
        # README.en.md. Neither README contains a `helm install` / `oci://`
        # snippet at all any more — install instructions were routed out to
        # the integration guides (which keep their own rules above). Nothing
        # to drive, so no rule.
        {
            "file": "docs/integration/gitops-deployment.md",
            "desc": "OCI chart --version in gitops deployment guide",
            "pattern": r"oci://ghcr\.io/vencil/charts/threshold-exporter --version [0-9]+\.[0-9]+\.[0-9]+",
            "replacement": lambda v: f"oci://ghcr.io/vencil/charts/threshold-exporter --version {v}",
        },
        {
            "file": "docs/integration/gitops-deployment.en.md",
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
        # ⚠️ Deleted: OCI chart inline version in docs/index.md. That page was
        # rewritten into a router ("想試哪個？…") and contains no `oci://`
        # reference at all now.
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
    #
    # These were pinned to docs/architecture-and-design{,.en}.md, where the
    # footer no longer exists (that doc now carries its version only in front
    # matter). But the SHAPE is alive elsewhere — the docs/scenarios/ guides
    # gitops-ci-integration.md and hands-on-lab.md still end with it, and both
    # sat at v2.7.0 unnoticed. So the fix is not "delete", it is "widen to the
    # tree": a glob finds the footer wherever it lives and cannot be
    # invalidated by one file dropping it. Same treatment as the sibling
    # **最後更新**： footer rule below.
    rules.append({
        "file": "__glob__",
        "glob_dir": "docs",
        "glob_pattern": "**/*.md",
        "desc": "doc footer **文件版本：** vX.Y.Z",
        "pattern": r"\*\*文件版本：\*\*\s*v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?",
        "replacement": lambda v: f"**文件版本：** v{v}",
    })
    rules.append({
        "file": "__glob__",
        "glob_dir": "docs",
        "glob_pattern": "**/*.md",
        "desc": "doc footer **Document version:** vX.Y.Z",
        "pattern": r"\*\*Document version:\*\*\s*v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?",
        "replacement": lambda v: f"**Document version:** v{v}",
    })

    # ⚠️ Two architecture-and-design header rules were deleted here, both
    # genuinely obsolete rather than broken:
    #   - `vX.Y.Z 的技術架構`  — that phrasing exists nowhere in the repo any
    #     more (the intro was rewritten around 「架構 Hub」).
    #   - `(vX.Y.Z).` in the .en doc — same rewrite, and the pattern was
    #     dangerously loose besides: it would have matched ANY parenthesised
    #     version followed by a period, anywhere in the file.
    # Both docs still get their version bumped, via the front-matter glob
    # below (`version: v2.9.0` in their YAML header), so nothing lost cover.

    # BYO guides version headers
    rules.append({
        "file": "docs/integration/byo-prometheus-integration.md",
        "desc": "BYOP guide version",
        "pattern": r"\*\*版本\*\*：v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?",
        "replacement": lambda v: f"**版本**：v{v}",
    })
    rules.append({
        "file": "docs/integration/byo-alertmanager-integration.md",
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
        "file": "docs/integration/gitops-deployment.md",
        "desc": "gitops-deployment.md version header",
        "pattern": r"\*\*版本\*\*：v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?",
        "replacement": lambda v: f"**版本**：v{v}",
    })

    # English doc version headers (BYO guides and gitops)
    rules.append({
        "file": "docs/integration/byo-prometheus-integration.en.md",
        "desc": "BYOP guide (en) version",
        "pattern": r"\*\*Version\*\*: v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?",
        "replacement": lambda v: f"**Version**: v{v}",
    })
    rules.append({
        "file": "docs/integration/byo-alertmanager-integration.en.md",
        "desc": "BYO Alertmanager guide (en) version",
        "pattern": r"\*\*Version\*\*: v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?",
        "replacement": lambda v: f"**Version**: v{v}",
    })
    rules.append({
        "file": "docs/integration/gitops-deployment.en.md",
        "desc": "gitops-deployment.en.md version header",
        "pattern": r"\*\*Version\*\*: v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?",
        "replacement": lambda v: f"**Version**: v{v}",
    })

    # Federation integration guide version header
    rules.append({
        "file": "docs/integration/federation-integration.md",
        "desc": "federation-integration.md version header",
        "pattern": r"> \*\*v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?\*\*",
        "replacement": lambda v: f"> **v{v}**",
    })
    rules.append({
        "file": "docs/integration/federation-integration.en.md",
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

    # CLAUDE.md project overview platform version.
    # Format (since v2.6.0, when the heading was simplified):
    #   ## 專案概覽
    #
    #   **Multi-Tenant Dynamic Alerting 平台 (v2.6.0)** — ...
    # The version lives in the bold lead-in line, not the heading itself,
    # so the anchor is the platform name + version, not the heading text.
    rules.append({
        "file": "CLAUDE.md",
        "desc": "CLAUDE.md project overview version",
        "pattern": r"Multi-Tenant Dynamic Alerting 平台 \(v[0-9]+\.[0-9]+[^)]*\)",
        "replacement": lambda v: f"Multi-Tenant Dynamic Alerting 平台 (v{v})",
    })

    # ⚠️ Deleted: `平台版本（vX.Y.Z+）` in components/da-tools/README.md. That
    # sentence is gone from the README; the file's platform version now lives
    # only in the versioning-strategy table, which the next rule drives.
    rules.append({
        "file": "components/da-tools/README.md",
        "desc": "da-tools version strategy table (platform row + git tag)",
        "pattern": r"\| 平台文件 \| v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)? \| `v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?`",
        "replacement": lambda v: f"| 平台文件 | v{v} | `v{v}`",
    })

    # Front matter `version: vX.Y.Z`.
    #
    # Two different trees, NOT one: the .md front matter lives under docs/,
    # the .jsx front matter under PORTAL_JSX_DIR. The old code globbed both
    # extensions under docs/ — correct for .md, dead for .jsx since TRK-230
    # moved the JSX out. Zero expansion is silent (no rule, so no SKIP and no
    # DEAD), which is how 44 of these files sat at v2.7.0 while the platform
    # SSOT said 2.9.0 and every gate reported green (#1407).
    for _glob_dir, _ext in (("docs", "**/*.md"), (PORTAL_JSX_DIR, "**/*.jsx")):
        rules.append({
            "file": "__glob__",
            "glob_dir": _glob_dir,
            "glob_pattern": _ext,
            "desc": f"front matter version: in {_glob_dir}/{_ext}",
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

    # Inline version text: `於 v2.0.0 統一採集` or similar inline version
    # strings in doc content.
    #
    # `skip_released_changelog`: CHANGELOG.md is scanned via the docs/**/*.md
    # glob (docs/CHANGELOG.md symlinks to the root CHANGELOG.md). Version
    # strings inside released `## [vX.Y.Z]` entries are historical facts —
    # `於 v<old>` there records what a past release did, not a pointer to
    # the current version — so they must never be bumped. Without this flag
    # the rule false-matched a historical `已於 v<old> …` sentence and tried
    # to flip it to the version being bumped to (PR #503; the stop-gap there
    # was to reword 於→在 to dodge the regex). See
    # _split_at_released_changelog().
    rules.append({
        "file": "__glob__",
        "glob_dir": "docs",
        "glob_pattern": "**/*.md",
        "desc": "inline version text in doc content",
        "pattern": r"於\s+v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?(?=\s|\）|。)",
        "replacement": lambda v: f"於 v{v}",
        "skip_released_changelog": True,
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

    # README.md / README.en.md platform version.
    #
    # Both rules used to anchor on an intro sentence (「…治理平台** vX.Y.Z」 /
    # "…Governance Platform** vX.Y.Z") that no longer exists — the H1 is now
    # a plain product name. The version did not leave the READMEs, it moved
    # into the shields.io badge on the badge row, so these are repointed
    # rather than deleted. Identical shape in both files, hence one loop.
    for _readme in ("README.md", "README.en.md"):
        rules.append({
            "file": _readme,
            "desc": f"{_readme} version badge",
            "pattern": r"badge/version-v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?-brightgreen",
            "replacement": lambda v: f"badge/version-v{v}-brightgreen",
        })

    # Interactive hub footer version.
    #
    # The old pattern (`vX.Y.Z — Multi-Tenant`) described a subtitle that was
    # rewritten: the footer now reads `Dynamic Alerting Platform — <a
    # href="../CHANGELOG/">vX.Y.Z</a> |`. Stale regex, live target — so this
    # is repointed, not deleted. It had been dead long enough for the footer
    # to fall to v2.7.0 while the platform shipped 2.9.0.
    #
    # ⚠️ NOT covered, deliberately: the hero badge in the same file reads
    # `INTERACTIVE TOOLS HUB v2.6` — two-component and plausibly the hub's
    # own version rather than the platform's, so bumping it is a judgement
    # call for the owner, not something to guess at from a regex.
    rules.append({
        "file": "docs/interactive/index.html",
        "desc": "interactive index.html footer version",
        "pattern": r"Dynamic Alerting Platform — <a href=\"\.\./CHANGELOG/\">v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?</a>",
        "replacement": lambda v: f"Dynamic Alerting Platform — <a href=\"../CHANGELOG/\">v{v}</a>",
    })

    # Interactive JSX front matter and version consistency
    #
    # 這些 JSX 已在 TRK-230 (Option C) 搬到 `tools/portal/src/`，由 esbuild
    # bundle 進 docs/assets/dist/。規則留在舊的 `docs/interactive/tools/`
    # 路徑上，於是靜默 SKIP 到今天——front matter 還停在 v2.7.0（#1407）。
    rules.append({
        "file": "tools/portal/src/interactive/tools/cli-playground.jsx",
        "desc": "cli-playground.jsx front matter version",
        "pattern": r"(?<=\n)version:\s*v[0-9]+\.[0-9]+[^\n]*(?=\n)",
        "replacement": lambda v: f"version: v{v}",
    })

    # ⚠️ 這條的目標字串不在 cli-playground.jsx —— `[✓] Version consistency`
    # 是 playground 的模擬輸出，住在拆分出去的 commands.js 裡。原規則同時搞錯
    # 目錄與檔名，repoint 時一併修正（#1407）。
    rules.append({
        "file": "tools/portal/src/interactive/tools/cli-playground/commands.js",
        "desc": "cli-playground commands.js version consistency output",
        "pattern": r"\[✓\]\s+Version consistency\s+v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?",
        "replacement": lambda v: f"[✓] Version consistency  v{v}",
    })

    rules.append({
        "file": "tools/portal/src/interactive/tools/platform-demo.jsx",
        "desc": "platform-demo.jsx version display",
        "pattern": r"(?<=\n)version:\s*v[0-9]+\.[0-9]+[^\n]*(?=\n)",
        "replacement": lambda v: f"version: v{v}",
    })

    return rules


def _build_portal_rules():
    """Build version replacement rules for da-portal.

    da-portal is the 5th release line (`portal/v*`). It was historically
    NOT covered by bump_docs.py at all — no `portal` line existed — which
    is why its README title and helm `--version` silently drifted to a
    stale v2.7.0 after the v2.8.0 release (`--check` had no rule to catch
    it). Covers Chart.yaml, the README title / OCI chart ref / image tag,
    and the da-portal row of the da-tools README versioning table.

    Returns list of rule dicts for the 'portal' version line.
    """
    rules = []

    # helm/da-portal/Chart.yaml version + appVersion
    rules.append({
        "file": "helm/da-portal/Chart.yaml",
        "desc": "da-portal Chart.yaml version",
        "pattern": r"^version:\s*[0-9]+\.[0-9]+\.[0-9]+",
        "replacement": lambda v: f"version: {v}",
    })
    rules.append({
        "file": "helm/da-portal/Chart.yaml",
        "desc": "da-portal Chart.yaml appVersion",
        "pattern": r'^appVersion:\s*"[0-9]+\.[0-9]+\.[0-9]+"',
        "replacement": lambda v: f'appVersion: "{v}"',
    })

    # da-portal README H1 title — `# da-portal (vX.Y.Z)`
    rules.append({
        "file": "components/da-portal/README.md",
        "desc": "da-portal README title version",
        "pattern": r"# da-portal \(v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?\)",
        "replacement": lambda v: f"# da-portal (v{v})",
    })

    # da-portal README OCI chart --version
    rules.append({
        "file": "components/da-portal/README.md",
        "desc": "da-portal OCI chart --version in README",
        "pattern": r"oci://ghcr\.io/vencil/charts/da-portal --version [0-9]+\.[0-9]+\.[0-9]+",
        "replacement": lambda v: f"oci://ghcr.io/vencil/charts/da-portal --version {v}",
    })

    # da-portal image tag in README
    rules.append({
        "file": "components/da-portal/README.md",
        "desc": "da-portal image tag in README",
        "pattern": r"ghcr\.io/vencil/da-portal:v?[0-9]+\.[0-9]+\.[0-9]+",
        "replacement": lambda v: f"ghcr.io/vencil/da-portal:v{v}",
    })

    # da-tools README versioning table — da-portal row (version + git tag)
    rules.append({
        "file": "components/da-tools/README.md",
        "desc": "da-portal version in da-tools strategy table",
        "pattern": r"\| da-portal \| v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?",
        "replacement": lambda v: f"| da-portal | v{v}",
    })
    rules.append({
        "file": "components/da-tools/README.md",
        "desc": "da-portal git tag in da-tools strategy table",
        "pattern": r"`portal/v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?`",
        "replacement": lambda v: f"`portal/v{v}`",
    })

    return rules


def _build_recipe_preview_rules():
    """Build version replacement rules for recipe-preview (#657).

    recipe-preview bundles a byte-identical snapshot of the platform compiler,
    so it is "sync-bumped" to the platform version like exporter/portal — NOT an
    independent cadence (prevents bundled-compiler drift). The ONLY version
    source is helm/recipe-preview/Chart.yaml (version == appVersion); the
    release.yaml `recipe-preview/v*` job gates Chart.yaml version == tag, and the
    image-tag invariant (#682) derives the image from appVersion. The README
    carries no version string (no H1 version / image tag / OCI --version), so —
    unlike portal — there is nothing else to rewrite.

    Returns list of rule dicts for the 'recipe-preview' version line.
    """
    return [
        {
            "file": "helm/recipe-preview/Chart.yaml",
            "desc": "recipe-preview Chart.yaml version",
            "pattern": r"^version:\s*[0-9]+\.[0-9]+\.[0-9]+",
            "replacement": lambda v: f"version: {v}",
        },
        {
            "file": "helm/recipe-preview/Chart.yaml",
            "desc": "recipe-preview Chart.yaml appVersion",
            "pattern": r'^appVersion:\s*"[0-9]+\.[0-9]+\.[0-9]+"',
            "replacement": lambda v: f'appVersion: "{v}"',
        },
    ]


def _build_rules():
    """Build all version replacement rules, grouped by version line.

    Returns {"platform": [...], "exporter": [...], "tools": [...],
    "portal": [...], "recipe-preview": [...], "tenant-api": [...]}.
    """
    return {
        "platform": _build_platform_rules(),
        "exporter": _build_exporter_rules(),
        "tools": _build_tools_rules(),
        "portal": _build_portal_rules(),
        "recipe-preview": _build_recipe_preview_rules(),
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

    Returns count of tools (by counting '- key:' entries). Accepts both
    `tools:\n  - key:` (nested) and top-level `- key:` shapes — the
    registry's actual format uses top-level (no extra indent), and the
    older nested pattern was a silent miss source until v2.8.0.
    """
    registry = REPO_ROOT / "docs" / "assets" / "tool-registry.yaml"
    if not registry.exists():
        return 0

    content = registry.read_text(encoding="utf-8")
    count = len(re.findall(r"^[ \t]*-\s+key:", content, re.MULTILINE))
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


def _count_precommit_hook_stages():
    """Return (auto_count, manual_count, pre_push_count) for hooks.

    `default_stages: [pre-commit]` makes hooks lacking explicit `stages`
    auto-run; `stages: [manual]` and `stages: [pre-push]` opt out.
    """
    config = REPO_ROOT / ".pre-commit-config.yaml"
    if not config.exists():
        return 0, 0, 0

    auto = manual = push = 0
    try:
        import yaml  # local import — yaml is a transitive dep, not in core
        cfg = yaml.safe_load(config.read_text(encoding="utf-8"))
    except Exception:
        return 0, 0, 0

    for repo in cfg.get("repos", []) or []:
        for hook in repo.get("hooks", []) or []:
            stages = hook.get("stages")
            if stages is None or "pre-commit" in stages:
                auto += 1
            elif "manual" in stages:
                manual += 1
            elif "pre-push" in stages:
                push += 1
            else:
                # Unknown stage — count as auto for safety.
                auto += 1
    return auto, manual, push


def _build_count_rules():
    """Build count replacement rules for CLAUDE.md and README.md.

    Returns list of rule dicts for count syncing.
    """
    # Only the counts that a surviving rule actually embeds are read here.
    # The per-directory ops/dx/lint split and the docs count are still
    # reported by `--sync-counts` (main() calls the _count_* helpers
    # directly) — they just no longer have a doc sentence to patch.
    total_tools, _ops, _dx, _lint = _count_python_tools()
    rule_packs = _count_rule_packs()
    jsx_tools = _count_jsx_tools()

    rules = []

    # ⚠️ SEVEN CLAUDE.md count rules were deleted here (#1407). All seven
    # matched nothing, and `--sync-counts --check` printed each as a green
    # "✅ … no match (pattern not found)" — the exact failure mode this pass
    # closes. Each was checked individually against the whole repo before
    # deletion; all are obsolete rather than broken, because CLAUDE.md was
    # condensed and the sentences they anchored on no longer exist in ANY
    # form, here or elsewhere:
    #
    #   Python tools total 「N 個 Python 工具（不含共用函式庫）」 — phrase gone
    #                      repo-wide. The equivalent count in README.md /
    #                      README.en.md ("N 個 Python 工具" in the repo-map
    #                      row) keeps its own rules below and is live.
    #   ops/ dx/ lint/     the three-column tool table they patched no longer
    #                      exists in CLAUDE.md; there is no such table
    #                      anywhere now (`| \`ops/\` |` has zero hits).
    #   Rule Pack count    「N 個 Rule Pack（N 個 optional」 gone; the
    #                      README.md badge rule below still covers it.
    #   JSX tools          「互動工具生態（N JSX tools）」 survives only inside
    #                      frozen CHANGELOG history; the live count is
    #                      dev-rules.md's, which has its own rule below.
    #   docs count         「完整文件對照表（N 個文件…）」 — the surviving
    #                      cross-references in README.md / docs/index.md
    #                      carry no number at all.
    #
    # CLAUDE.md is a deliberately trimmed context file, so re-inserting seven
    # count sentences just to give these rules something to match would be
    # the tail wagging the dog. Deleting is the honest option; what remains
    # below is every count sentence that actually exists.

    # CLAUDE.md: pre-commit hook breakdown (auto-run + manual-stage + pre-push)
    # Source-of-truth count derived dynamically from .pre-commit-config.yaml stages.
    auto_n, manual_n, push_n = _count_precommit_hook_stages()
    if auto_n + manual_n + push_n > 0:
        rules.append({
            "file": "CLAUDE.md",
            "desc": (
                f"CLAUDE.md: pre-commit hook breakdown "
                f"({auto_n} auto + {manual_n} manual + {push_n} push)"
            ),
            "pattern": (
                r"\d+\s+auto-run\s+\+\s+\d+\s+manual-stage"
                r"(?:\s+\+\s+\d+\s+pre-push)?\s+hooks"
            ),
            "replacement": lambda _: (
                f"{auto_n} auto-run + {manual_n} manual-stage "
                f"+ {push_n} pre-push hooks"
            ),
            "is_count": True,
        })

    # dev-rules.md: 互動工具 SOP 章節「N 個 JSX 互動工具」count
    # Phase .c 期間自 39 增至 43；hardcoded count is drift surface.
    if jsx_tools > 0:
        rules.append({
            "file": "docs/internal/dev-rules.md",
            "desc": f"dev-rules.md: JSX 互動工具 count ({jsx_tools} tools)",
            "pattern": r"專案有\s+\*\*\d+\s+個\s+JSX\s+互動工具\*\*",
            "replacement": lambda _: f"專案有 **{jsx_tools} 個 JSX 互動工具**",
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

    # README.md / README.en.md: Python tools count in the repo-map row.
    # Same scope as _count_python_tools (scripts/tools/{ops,dx,lint}); the
    # repo-map phrasing differs from CLAUDE.md so needs its own rule (else drifts).
    if total_tools > 0:
        rules.append({
            "file": "README.md",
            "desc": f"README.md: Python tools in repo-map ({total_tools} tools)",
            "pattern": r"`scripts/tools/\{ops,dx,lint\}`\s*下\s*\d+\s*個\s*Python\s*工具",
            "replacement": lambda _: f"`scripts/tools/{{ops,dx,lint}}` 下 {total_tools} 個 Python 工具",
            "is_count": True,
        })
        rules.append({
            "file": "README.en.md",
            "desc": f"README.en.md: Python tools in repo-map ({total_tools} tools)",
            "pattern": r"\+\s*\d+\s*Python tools under\s*`scripts/tools/\{ops,dx,lint\}`",
            "replacement": lambda _: f"+ {total_tools} Python tools under `scripts/tools/{{ops,dx,lint}}`",
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
            # MISSING, not SKIP. Count rules are every bit as much a gate as
            # version rules — .github/workflows/validate.yaml runs
            # `--sync-counts --check` — and a rule whose file is gone syncs
            # no count at all. Same MISSING/DEAD split as apply_rules(),
            # kept separate because the fixes differ (see below).
            changes.append(("MISSING", rule["desc"],
                            f"file not found: {rule['file']} — this rule syncs "
                            f"no count. Fix the \"file\" path in "
                            f"_build_count_rules(), or delete the rule if its "
                            f"target is gone."))
            continue

        content = fpath.read_text(encoding="utf-8")
        pattern = rule["pattern"]
        replacement = rule["replacement"](None)

        matches = re.findall(pattern, content, re.MULTILINE)
        if not matches:
            # Count rules are all hand-written and single-file — there is no
            # glob fan-out here — so zero matches is unconditionally a defect,
            # exactly the require_match-by-default rule that apply_rules()
            # applies to hand-written version rules.
            changes.append(("DEAD", rule["desc"],
                            f"pattern {pattern!r} matched NOTHING in "
                            f"{rule['file']} — the sentence this count is "
                            f"embedded in changed shape, so the count stopped "
                            f"being synced. Fix the \"pattern\"."))
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
                fpath.write_text(new_content, encoding="utf-8", newline="\n")
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

    # Platform version from CLAUDE.md.
    # Since v2.6.0 the heading "## 專案概覽" no longer carries a version
    # suffix; the version moved to the bold lead-in line below the heading
    # ("**Multi-Tenant Dynamic Alerting 平台 (vX.Y.Z)** — ..."). The
    # anchor here must stay in sync with the write rule in
    # _build_platform_rules() above.
    claude_md = REPO_ROOT / "CLAUDE.md"
    if claude_md.exists():
        content = claude_md.read_text(encoding="utf-8")
        m = re.search(
            r"Multi-Tenant Dynamic Alerting 平台 \(v([0-9]+\.[0-9]+[^)]*)\)",
            content,
        )
        if m:
            versions["platform"] = m.group(1)

    # da-tools version from VERSION file
    if DA_TOOLS_VERSION.exists():
        ver = DA_TOOLS_VERSION.read_text(encoding="utf-8").strip()
        if re.match(r"^[0-9]+\.[0-9]+\.[0-9]+$", ver):
            versions["tools"] = ver

    # Portal version from da-portal Chart.yaml (version == appVersion).
    if DA_PORTAL_CHART_YAML.exists():
        content = DA_PORTAL_CHART_YAML.read_text(encoding="utf-8")
        m = re.search(
            r'^appVersion:\s*"([0-9]+\.[0-9]+\.[0-9]+)"', content, re.MULTILINE
        )
        if m:
            versions["portal"] = m.group(1)

    # tenant-api version from helm/tenant-api/Chart.yaml `version:`.
    #
    # `version:`, NOT `appVersion:` — the two are decoupled on purpose for
    # this chart (see _build_tenant_api_rules). `version` is the release
    # line's identity: .github/workflows/release.yaml gates it to equal the
    # `tenant-api/v*` tag, and the image is pushed as `:v${that}`.
    #
    # Omitting this line was not a cosmetic gap: read_current_versions() is
    # what `--check` iterates, so ALL tenant-api rules were unreachable and
    # `--what-if` just printed "version not found in source-of-truth" and
    # moved on with no effect on the exit code (#1407).
    if TENANT_API_CHART_YAML.exists():
        content = TENANT_API_CHART_YAML.read_text(encoding="utf-8")
        m = re.search(
            r'^version:\s*"?([0-9]+\.[0-9]+\.[0-9]+)"?', content, re.MULTILINE
        )
        if m:
            versions["tenant-api"] = m.group(1)

    # recipe-preview version from its Chart.yaml (sync-bump; version == appVersion).
    if RECIPE_PREVIEW_CHART_YAML.exists():
        content = RECIPE_PREVIEW_CHART_YAML.read_text(encoding="utf-8")
        m = re.search(
            r'^appVersion:\s*"([0-9]+\.[0-9]+\.[0-9]+)"', content, re.MULTILINE
        )
        if m:
            versions["recipe-preview"] = m.group(1)

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


def _requires_match(rule):
    """Is zero matches a DEAD rule (True) or a legitimate no-op (False)?

    Default ON for hand-written rules, OFF for glob-expanded ones; an
    explicit `require_match` key always wins. See apply_rules() for why the
    two classes have opposite defaults.
    """
    if "require_match" in rule:
        return bool(rule["require_match"])
    return not rule.get("from_glob", False)


def _expand_glob_rules(rules):
    """Expand __glob__ rules into per-file rules.

    Each expanded rule carries `from_glob: True`. That marker is what lets
    apply_rules() default `require_match` ON for hand-written rules while
    leaving it OFF here: a hand-written rule names ONE file and one shape, so
    zero matches means it is broken, whereas a glob rule is fanned out across
    every file in a tree and legitimately matches nothing in most of them.
    """
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
                    "from_glob": True,
                    "skip_released_changelog": rule.get(
                        "skip_released_changelog", False),
                })
        else:
            expanded.append(rule)
    return expanded


# Keep-a-Changelog released-version heading: `## [vX.Y.Z]`. Everything from
# the first such heading downward is frozen history. `## [Unreleased]` does
# not match this, so in-flight content above the first cut version stays
# scannable.
_RELEASED_CHANGELOG_HEADING = re.compile(
    r"^## \[v[0-9]+\.[0-9]+\.[0-9]+", re.MULTILINE)


def _split_at_released_changelog(content):
    """Split `content` into (live, frozen) at the first released-version
    CHANGELOG heading.

    Released CHANGELOG entries record what shipped in a past version, so the
    version strings inside them are historical facts that bump_docs must not
    rewrite (PR #503: a `已於 v<old> …` sentence in a released entry was a
    fact about that past release, not a stale current-version reference).
    Returns the in-flight prefix as `live` and the frozen entries as
    `frozen`; `live + frozen == content`.

    Files with no `## [vX.Y.Z]` heading (every doc except CHANGELOG) come
    back as fully-live with an empty `frozen`, so callers can treat this as
    a no-op for them.
    """
    m = _RELEASED_CHANGELOG_HEADING.search(content)
    if not m:
        return content, ""
    return content[:m.start()], content[m.start():]


def _indent_width(line: str) -> int:
    return len(line) - len(line.lstrip())


def _rewrite_anchored_pair(content: str, rule: dict, new_value: str):
    """Rewrite `<pair_key>: <value>` in the mapping that carries `pair_anchor`.

    Exists because a Helm image pin is a TWO-LINE shape —

        image:
          repository: ghcr.io/vencil/da-tools
          tag: "v2.9.0"

    — that no single line-oriented rule can address safely. Matching `tag:`
    alone would rewrite the wrong image the moment a chart gains a second one
    (a sidecar), and matching both lines with one regex would have to hardcode
    the indentation into the replacement (silently re-indenting, i.e. breaking,
    a chart that uses 4 spaces). So: find the anchor LINE, then rewrite the
    sibling key at exactly the anchor's indentation, reusing that indentation
    verbatim. `helm/federation-reconciler/values.yaml` is precisely the pin
    that went unbumped for two releases because no rule could see it.

    Returns (new_content, old_values). `old_values == []` means the shape was
    not found at all — the caller reports that as DEAD rather than "no match,
    already up to date", because a silently-dead rule is the whole failure mode
    this exists to end.
    """
    anchor_re = re.compile(rule["pair_anchor"])
    value_re = re.compile(rule["pattern"])
    key = rule["pair_key"]
    lines = content.split("\n")
    old_values: list[str] = []

    for index, line in enumerate(lines):
        if not anchor_re.match(line):
            continue
        indent = _indent_width(line)
        # The anchor's siblings: contiguous lines that are blank, comments, or
        # at least as deeply indented. A line at a SHALLOWER indent ends the
        # mapping (it belongs to the parent), which bounds the rewrite.
        start = index
        while start > 0:
            prev = lines[start - 1]
            if not prev.strip() or prev.lstrip().startswith("#") or _indent_width(prev) >= indent:
                start -= 1
            else:
                break
        end = index + 1
        while end < len(lines):
            nxt = lines[end]
            if not nxt.strip() or nxt.lstrip().startswith("#") or _indent_width(nxt) >= indent:
                end += 1
            else:
                break

        for j in range(start, end):
            sibling = lines[j]
            if _indent_width(sibling) != indent:
                continue
            stripped = sibling.strip()
            if not stripped.startswith(f"{key}:"):
                continue
            value = stripped[len(key) + 1:].strip()
            if not value_re.fullmatch(value):
                continue
            old_values.append(value)
            lines[j] = f"{' ' * indent}{key}: {new_value}"

    return "\n".join(lines), old_values


def apply_rules(rules, new_version, check_only=False, dry_run=False):
    """Apply a set of replacement rules. Returns list of (status, desc, detail) tuples.

    Args:
        rules: Replacement rules from _build_rules().
        new_version: Target version string.
        check_only: If True, don't modify files (for --check mode).
        dry_run: If True, don't modify files but show before→after diffs.

    A rule may set `skip_released_changelog: True` to exclude frozen
    `## [vX.Y.Z]` CHANGELOG entries from its scan (see
    _split_at_released_changelog).

    Two options govern rules whose target must never quietly disappear:

      `pair_anchor` / `pair_key`  two-line YAML pin (see
                                  _rewrite_anchored_pair); `pattern` then
                                  matches the VALUE only.
      `require_match`             zero matches is a DEAD rule, not an "OK".

    `require_match` DEFAULTS TO ON for hand-written rules and OFF for
    glob-expanded ones (`from_glob`), because the two have opposite null
    semantics:

      hand-written  names one file and one shape. Zero matches means the
                    shape moved and the rule stopped bumping anything —
                    always a defect. It used to be recorded as
                    ("OK", "no match (may already be updated)"), which is
                    how ~16 rules read green while the strings they were
                    meant to drive rotted (#1407).
      glob-expanded fans one pattern across a whole tree; matching nothing
                    in most files is the normal case, not a defect. The
                    glob's own health is asserted differently — every
                    `__glob__` must expand to >=1 file (see
                    tests/dx/test_bump_docs.py).

    Opt OUT with an explicit `require_match: False` plus a comment naming
    the reason — never silently, or the signal decays back to noise.
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
                    fpath.write_text(new_content, encoding="utf-8", newline="\n")
                    os.chmod(fpath, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
            else:
                changes.append(("OK", rule["desc"], "already up to date"))
            continue

        if rule.get("pair_anchor"):
            new_value = rule["replacement"](new_version)
            new_content, old_values = _rewrite_anchored_pair(content, rule, new_value)
            if not old_values:
                changes.append(("DEAD", rule["desc"],
                                f"anchor {rule['pair_anchor']!r} + key "
                                f"'{rule['pair_key']}' matched NOTHING in "
                                f"{rule['file']} — the file's shape moved and "
                                f"this rule stopped bumping anything. Fix the "
                                f"rule (do not delete it): a dead rule reads as "
                                f"green while the version silently rots."))
            elif new_content != content:
                changes.append(("UPDATE", rule["desc"],
                                f"{'[dry-run] ' if dry_run else ''}replaced "
                                f"{len(old_values)} occurrence(s): "
                                f"{sorted(set(old_values))[0]} → {new_value}"))
                if not check_only and not dry_run:
                    fpath.write_text(new_content, encoding="utf-8", newline="\n")
                    os.chmod(fpath, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
            else:
                changes.append(("OK", rule["desc"], "already up to date"))
            continue

        pattern = rule["pattern"]
        replacement = rule["replacement"](new_version)

        # Released CHANGELOG entries are frozen history — never bump version
        # refs inside them. `frozen_tail` is "" for ordinary files.
        scan_text, frozen_tail = content, ""
        if rule.get("skip_released_changelog"):
            scan_text, frozen_tail = _split_at_released_changelog(content)

        matches = re.findall(pattern, scan_text, re.MULTILINE)
        if not matches:
            if _requires_match(rule):
                changes.append(("DEAD", rule["desc"],
                                f"pattern {pattern!r} matched NOTHING in "
                                f"{rule['file']} — this rule is the mechanical "
                                f"driver for that file's version and has "
                                f"stopped driving it. Fix the rule (do not "
                                f"delete it)."))
            else:
                changes.append(("OK", rule["desc"], "no match (may already be updated)"))
            continue

        needs_update = any(m != replacement for m in matches)
        if needs_update:
            new_content = re.sub(pattern, replacement, scan_text,
                                 flags=re.MULTILINE) + frozen_tail
            # Build diff detail
            unique_old = sorted(set(matches))
            diff_detail = (f"replaced {len(matches)} occurrence(s): "
                           f"{unique_old[0]} → {replacement}")
            if dry_run:
                diff_detail = f"[dry-run] {diff_detail}"
            changes.append(("UPDATE", rule["desc"], diff_detail))
            if not check_only and not dry_run:
                fpath.write_text(new_content, encoding="utf-8", newline="\n")
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
                                     encoding="utf-8", newline="\n")
                os.chmod(changelog,
                         stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP
                         | stat.S_IROTH)
                print(f"✅ Created {changelog.name} with v{version} stub "
                      f"({today})")
                continue
            else:
                print(f"ERROR: {changelog} not found", file=sys.stderr)
                sys.exit(EXIT_CALLER_ERROR)

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
        changelog.write_text(new_content, encoding="utf-8", newline="\n")
        os.chmod(changelog,
                 stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP
                 | stat.S_IROTH)
        print(f"✅ Inserted v{version} stub into {changelog.name} "
              f"({today})")


def main():
    """CLI entry point: 版號一致性管理工具."""
    try_utf8_stdout()
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
    parser.add_argument("--portal", metavar="VER",
                        help="New da-portal version (e.g. 2.8.0)")
    parser.add_argument("--recipe-preview", metavar="VER",
                        help="New recipe-preview version (sync-bump; e.g. 2.9.0)")
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
            # DEAD / MISSING are ❌, never ⚠️ — they fail the gate now, and an
            # icon that reads as "advisory" is what let 7 dead count rules
            # pass as green ticks.
            icon = {"UPDATE": "📝", "OK": "✅",
                    "DEAD": "❌", "MISSING": "❌"}[status]
            print(f"  {icon} {desc}: {detail}")

        update_count = sum(1 for s, _, _ in changes if s == "UPDATE")
        dead_counts = sum(1 for s, _, _ in changes if s == "DEAD")
        missing_counts = sum(1 for s, _, _ in changes if s == "MISSING")

        # A dead/missing count rule is a defect regardless of mode: in
        # --check it must fail CI, and in a plain `--sync-counts` run it must
        # not report "Done, 0 updated" as if everything were synced.
        if missing_counts:
            print(f"\n❌ {missing_counts} count rule(s) point at a file that "
                  f"does not exist (MISSING). Fix the \"file\" path in "
                  f"_build_count_rules(), or delete the rule.")
        if dead_counts:
            print(f"\n❌ {dead_counts} count rule(s) matched NOTHING (DEAD). "
                  f"A rule that matches nothing syncs nothing — fix the "
                  f"\"pattern\" in _build_count_rules().")

        if args.check:
            if update_count > 0:
                print(f"\n❌ {update_count} count(s) are outdated. Run without --check to apply.")
                sys.exit(EXIT_VIOLATION)
            elif dead_counts or missing_counts:
                sys.exit(EXIT_VIOLATION)
            else:
                print("\n✅ All counts are already up to date.")
        elif args.dry_run:
            if update_count > 0:
                print(f"\n🔍 Dry run: {update_count} count(s) would be updated.")
            elif not (dead_counts or missing_counts):
                print("\n✅ Dry run: all counts are already up to date.")
            if dead_counts or missing_counts:
                sys.exit(EXIT_VIOLATION)
        else:
            print(f"\n✅ Done. {update_count} count(s) updated.")
            if dead_counts or missing_counts:
                sys.exit(EXIT_VIOLATION)
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
            sys.exit(EXIT_CALLER_ERROR)

        all_rules = _build_rules()
        total_rules = 0
        matched = 0
        unmatched = 0
        missing = 0

        for line in ("platform", "exporter", "tools", "portal", "recipe-preview", "tenant-api"):
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
                    print(f"  ❌ {desc}")
                    print(f"       MISSING: file not found: {rule['file']}")
                    continue

                content = fpath.read_text(encoding="utf-8")
                pattern = rule["pattern"]
                replacement = rule["replacement"](ver)

                if rule.get("pair_anchor"):
                    _, old_values = _rewrite_anchored_pair(content, rule, replacement)
                    if not old_values:
                        unmatched += 1
                        print(f"  ❌ {desc}")
                        print(f"       DEAD: anchor {rule['pair_anchor']!r} + key "
                              f"'{rule['pair_key']}' matched nothing")
                    elif all(v == replacement for v in old_values):
                        matched += 1
                        print(f"  ✅ {desc}")
                        print(f"       matched: {replacement} "
                              f"({len(old_values)} occurrence(s))")
                    else:
                        unmatched += 1
                        print(f"  ❌ {desc}")
                        print(f"       found: {sorted(set(old_values))}")
                        print(f"       expected: {replacement}")
                    continue

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

                scan_text = content
                if rule.get("skip_released_changelog"):
                    scan_text, _ = _split_at_released_changelog(content)

                matches = re.findall(pattern, scan_text, re.MULTILINE)
                if not matches and _requires_match(rule):
                    unmatched += 1
                    print(f"  ❌ {desc}")
                    print(f"       DEAD: pattern matched nothing in an "
                          f"unglobbed rule (require_match)")
                elif not matches:
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
              f"{matched} ✅, {unmatched} ❌ DEAD/drift, {missing} ❌ MISSING")
        print(f"{'='*60}")
        # `missing` 也算 violation：規則指向不存在的檔案，跟 pattern 撈不到
        # 一樣是「這條規則什麼都沒 bump」。舊版只看 unmatched，所以 14 條指向
        # 已搬走檔案的規則能讓 --what-if exit 0（#1407）。
        sys.exit(EXIT_VIOLATION if (unmatched > 0 or missing > 0) else EXIT_OK)

    # --check mode: read current versions and verify all references match
    # `args.tenant_api` belongs in this guard like the other five: without it
    # `--check --tenant-api 9.9.9` fell through to the bare-check branch,
    # which re-reads the CURRENT versions — so the flag was silently ignored
    # and the command exited 0 while claiming to have checked 9.9.9 (#1407).
    if args.check and not (args.platform or args.exporter or args.tools
                           or args.portal or args.recipe_preview
                           or args.tenant_api):
        versions = read_current_versions()
        if not versions:
            print("ERROR: Cannot read current versions from source files", file=sys.stderr)
            sys.exit(EXIT_CALLER_ERROR)

        all_rules = _build_rules()
        has_drift = False

        for line, ver in versions.items():
            rules = _filter_by_scope(all_rules.get(line, []), args.scope)
            changes = apply_rules(rules, ver, check_only=True)
            for status, desc, detail in changes:
                if status == "UPDATE":
                    has_drift = True
                    print(f"  DRIFT  [{line}] {desc}: {detail}")
                elif status == "DEAD":
                    # A rule that matches nothing would otherwise report OK
                    # forever while the file it is supposed to drive rots.
                    has_drift = True
                    print(f"  DEAD   [{line}] {desc}: {detail}")
                elif status == "SKIP":
                    # 檔案不存在 = 規則指向的東西被搬走或刪掉了。過去這裡只印
                    # 一行 SKIP 就放行，於是 14 條規則在 `make version-check`
                    # 綠燈下靜靜死了好幾個版本（#1407）——release gate 說「版號
                    # 全部一致」，其實是「我沒去看那些檔案」。
                    #
                    # MISSING 與 DEAD 是兩種不同的診斷，標籤刻意分開：
                    #   MISSING = 檔案不在了 → 修 _build_*_rules() 的 "file"，
                    #             或該規則已無對象就整條刪掉。
                    #   DEAD    = 檔案在、pattern 撈不到 → 修 "pattern"。
                    has_drift = True
                    print(f"  MISSING [{line}] {desc}: {detail}")

        if has_drift:
            print("\n❌ Version drift (or a DEAD / MISSING rule) detected. Run "
                  "bump_docs.py with version flags to fix drift; fix a DEAD "
                  "pattern or a MISSING file path in _build_*_rules().")
            sys.exit(EXIT_VIOLATION)
        else:
            print("✅ All version references are consistent.")
            sys.exit(EXIT_OK)

    # Explicit bump mode
    if not (args.platform or args.exporter or args.tools or args.portal
            or args.recipe_preview or args.tenant_api):
        parser.print_help()
        sys.exit(EXIT_CALLER_ERROR)

    all_rules = _build_rules()
    total_updates = 0
    dead_rules = 0
    missing_rules = 0

    for line, new_ver in [("platform", args.platform),
                          ("exporter", args.exporter),
                          ("tools", args.tools),
                          ("portal", args.portal),
                          ("recipe-preview", args.recipe_preview),
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
            # SKIP 用 ❌ 不用 ⚠️ ——它現在會讓 bump 失敗，圖示不該再讀成「可忽略」。
            icon = {"UPDATE": "📝", "OK": "✅", "SKIP": "❌", "DEAD": "❌"}[status]
            print(f"  {icon} {desc}: {detail}")
            if status == "UPDATE":
                total_updates += 1
            elif status == "DEAD":
                dead_rules += 1
            elif status == "SKIP":
                missing_rules += 1

    # MISSING 與 DEAD 在這裡同等對待，理由一致：explicit bump 是 release 動作，
    # 「這條規則沒 bump 到任何東西」不論成因是 pattern 撈不到（DEAD）還是檔案
    # 不在（MISSING），結果都是某個版號引用被留在舊版本、而 release 流程回報成功。
    # 兩者刻意分開計數與分開報訊，因為修法不同：MISSING 修 "file"、DEAD 修
    # "pattern"（#1407）。
    if missing_rules:
        print(f"\n❌ {missing_rules} rule(s) point at a file that does not exist "
              f"(MISSING). A rule with no file bumps nothing — fix the \"file\" "
              f"path in _build_*_rules(), or delete the rule if its target is gone.")

    if dead_rules:
        print(f"\n❌ {dead_rules} rule(s) matched NOTHING (DEAD). A rule that "
              f"matches nothing bumps nothing — fix it in _build_*_rules().")

    if dead_rules or missing_rules:
        sys.exit(EXIT_VIOLATION)

    if args.check:
        if total_updates > 0:
            print(f"\n❌ {total_updates} file(s) would be updated. Run without --check to apply.")
            sys.exit(EXIT_VIOLATION)
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
