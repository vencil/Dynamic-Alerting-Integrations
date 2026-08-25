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
  # （--scope 對「指名的版號線」選不到任何規則時 → exit 2，不會回報「已完成」；
  #   bare --check 下被 scope 排除的線會印 SCOPE-EMPTY，不算 violation）
  python3 scripts/tools/bump_docs.py --dry-run --scope docs --platform 2.1.0

  # 初始化英文 CHANGELOG
  python3 scripts/tools/bump_docs.py --init-changelog 2.1.0 --changelog-lang en

  # 同時初始化中英文 CHANGELOG
  python3 scripts/tools/bump_docs.py --init-changelog 2.1.0 --changelog-lang all

  # 完整規則審計（顯示所有規則的當前匹配狀態）
  python3 scripts/tools/bump_docs.py --what-if

  # 自動更新散落在文件中的硬編碼計數（工具、Rule Pack、文件數、hooks 等）
  python3 scripts/tools/bump_docs.py --sync-counts

  # 檢查計數是否需要更新（`make version-check` / `make pre-tag` 會跑這條）
  # ⛔ --sync-counts 不接受版號旗標與 --scope（它只同步計數，過去照收然後靜默丟棄）
  python3 scripts/tools/bump_docs.py --sync-counts --check

  # 組合使用
  python3 scripts/tools/bump_docs.py --platform 1.1.0 --tools 1.1.0 --exporter 1.1.0 --tenant-api 2.4.0
"""
import argparse
import os
import re
import stat
import sys
from pathlib import Path, PurePosixPath

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lib_toolcount import count_by_subdir, count_scope  # noqa: E402

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
# The six version lines are a FIXED EXPECTATION, not whatever happened to parse
# ---------------------------------------------------------------------------
# Every consumer used to iterate `read_current_versions().items()` — i.e. the
# set of lines was defined by what the SSOT regexes managed to read. So a line
# whose source-of-truth became unreadable did not fail; it CEASED TO EXIST, and
# with it every rule it owned. Measured: rewording CLAUDE.md's project-overview
# sentence (same version number, different phrasing) removed the platform line
# and its ~2170 expanded rules, and `--check` still printed "✅ All version
# references are consistent." with exit 0.
#
# Fixing the tenant-api instance of this (#1407 D-4) left the CLASS alive, so
# the expectation is now declared here and the reads are checked AGAINST it.
# A new line must be added in three places — this tuple, _build_rules(), and
# read_current_versions() — and the tests below pin all three to each other.
#
# The description is printed verbatim in the NO-SSOT diagnostic, so it must say
# WHICH file and WHICH shape stopped parsing.
VERSION_LINE_SOURCES = {
    "platform": ("CLAUDE.md",
                 "`**Multi-Tenant Dynamic Alerting 平台 (vX.Y.Z)**` in the "
                 "專案概覽 lead-in line"),
    "exporter": ("helm/threshold-exporter/Chart.yaml", '`appVersion: "X.Y.Z"`'),
    "tools": ("components/da-tools/app/VERSION", "the whole file (bare X.Y.Z)"),
    "portal": ("helm/da-portal/Chart.yaml", '`appVersion: "X.Y.Z"`'),
    "recipe-preview": ("helm/recipe-preview/Chart.yaml", '`appVersion: "X.Y.Z"`'),
    "tenant-api": ("helm/tenant-api/Chart.yaml", "`version: X.Y.Z`"),
}
VERSION_LINES = tuple(VERSION_LINE_SOURCES)

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

# Semver with optional pre-release suffix (-preview, -rc1, -beta, etc.)
#
# ⛔ There is deliberately NO "strict" (suffix-less) variant any more. Rules
# used to mix `_SEMVER` into image tags and chart versions, which is
# wrong in BOTH directions once a release candidate exists:
#
#   read side   `appVersion: "2.10.0-rc1"` does not match a strict pattern, so
#               the exporter line silently vanished (see VERSION_LINE_SOURCES).
#   write side  worse — a strict pattern matches the `2.10.0` PREFIX of an
#               already-correct `2.10.0-rc1`, so the value never compares equal
#               to the replacement. `--check` reports eternal drift and a write
#               appends the suffix again: `2.10.0-rc1-rc1`.
#
# `test_rule_patterns_are_idempotent_on_prerelease_versions` is the mechanical
# guard: no rule pattern may match a proper prefix of its own output.
_SEMVER = r"[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?"


def _shields_escape(value: str) -> str:
    """Escape a value for one field of a shields.io static-badge URL.

    `https://img.shields.io/badge/<label>-<message>-<color>` splits on single
    dashes, so a dash that is part of a field's TEXT has to be doubled. Only
    the README version badge needs this, and only since `_SEMVER` grew a
    pre-release suffix — see the rule in _build_platform_rules().
    """
    return value.replace("-", "--")


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
        "pattern": r"ghcr\.io/vencil/da-tools:v?" + _SEMVER,
        "replacement": lambda v: f"ghcr.io/vencil/da-tools:v{v}",
        # docs/**/*.md reaches docs/CHANGELOG.md (symlink to the root one).
        # A pinned image quoted inside a released entry is a record of what
        # that release shipped — see the ⛔ note in _build_platform_rules().
        "skip_released_changelog": True,
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
            "pattern": r"ghcr\.io/vencil/da-tools:v?" + _SEMVER,
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
            "pattern": r"ghcr\.io/vencil/da-tools:v?" + _SEMVER,
            "replacement": lambda v: f"ghcr.io/vencil/da-tools:v{v}",
        })

    # VERSION file (exact content)
    rules.append({
        "file": "components/da-tools/app/VERSION",
        "desc": "da-tools VERSION file",
        "pattern": r"^" + _SEMVER + r"\s*$",
        "replacement": lambda v: f"{v}\n",
        "whole_file": True,
    })

    # da-tools README build.sh version
    rules.append({
        "file": "components/da-tools/README.md",
        "desc": "da-tools build.sh version",
        "pattern": r"\./build\.sh " + _SEMVER,
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
        "pattern": r"\| \*\*da-tools\*\* \| \*\*v?" + _SEMVER + r"\*\*",
        "replacement": lambda v: f"| **da-tools** | **v{v}**",
    })
    rules.append({
        "file": "components/da-tools/README.md",
        "desc": "da-tools version strategy table (git tag)",
        "pattern": r"\*\*`tools/v" + _SEMVER + r"`\*\*",
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
            "pattern": r"ghcr\.io/vencil/da-tools:v?" + _SEMVER,
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
        "pattern": r'"v?' + _SEMVER + r'"',
        "replacement": lambda v: f'"v{v}"',
        "require_match": True,
    })
    rules.append({
        "file": "helm/federation-reconciler/Chart.yaml",
        "desc": "federation-reconciler Chart.yaml appVersion (the da-tools image it ships)",
        "pattern": r'^appVersion:\s*"v?' + _SEMVER + '"',
        "replacement": lambda v: f'appVersion: "v{v}"',
        "require_match": True,
    })

    # mkdocs.yml tools_version
    rules.append({
        "file": "mkdocs.yml",
        "desc": "mkdocs.yml tools_version",
        "pattern": r'tools_version:\s+"' + _SEMVER + '"',
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
        "pattern": r"^version:\s+" + _SEMVER,
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
        "pattern": r'org\.opencontainers\.image\.version="' + _SEMVER + '"',
        "replacement": lambda v: f'org.opencontainers.image.version="{v}"',
    })

    # ⚠️ No rule for the README's `helm install ... oci://.../tenant-api`
    # snippet. It deliberately carries NO `--version` flag — the README says
    # 「版本見 Releases / CHANGELOG；省略 --version 取最新，或 --version <x.y.z>
    # 釘版」, i.e. unpinned-by-design so the doc cannot go stale. The old rule
    # required a literal `--version <semver>` there and so matched nothing
    # forever. Deleted, not repointed: there is no version string to drive.

    # ⚠️ Deleted: the `tenant-api image tag in docs` glob (docs/**/*.md). It
    # expanded to 260 files and matched in ZERO — GLOB-DEAD. Verified
    # repo-wide: no file under docs/ pins that image at all. The one doc-shaped
    # mention, components/tenant-api/README.md, deliberately writes
    # `ghcr.io/vencil/tenant-api:<version>` as a placeholder, which is the
    # right answer for prose and nothing for a regex to drive.
    #
    # The pins that DO exist are below (Dockerfile) and outside this tool's
    # reach — see the note there.

    # tenant-api image tag in the Dockerfile's build/run instructions.
    #
    # These two comment lines said `:2.4.0` while the chart said 2.9.20 — five
    # minor versions of rot, in the copy-paste command a person actually runs.
    # Nothing covered them: the deleted glob only looked in docs/.
    #
    # `require_match` is explicit (not just the hand-written default) because
    # this rule exists precisely BECAUSE the shape was uncovered; if someone
    # rewrites the comment, it must fail loudly rather than quietly return to
    # being uncovered.
    rules.append({
        "file": "components/tenant-api/Dockerfile",
        "desc": "tenant-api image tag in Dockerfile build/run comments",
        "pattern": r"ghcr\.io/vencil/tenant-api:v?" + _SEMVER,
        # v-PREFIXED, unlike the `:2.4.0` these comments carried. That is not a
        # cosmetic change: release.yaml publishes this image as
        # `:v${version}` (see its header comment), so the un-prefixed tag a
        # reader copy-pastes out of `docker run` does not exist in the
        # registry. `v?` in the pattern absorbs both spellings on the way in.
        "replacement": lambda v: f"ghcr.io/vencil/tenant-api:v{v}",
        "require_match": True,
    })

    # da-tools README versioning table — the tenant-api row.
    #
    # Every OTHER row of that five-row table already had a dedicated rule
    # (平台文件 / threshold-exporter / da-tools / da-portal). The tenant-api
    # row was the only one without, so it sat at v2.8.0 while this line's
    # SSOT said 2.9.20.
    #
    # ⚠️ It tracks Chart.yaml `version:` (this line's SSOT), NOT `appVersion`.
    # That distinction is the whole reason three OTHER stale-looking
    # `tenant-api:v2.7.0` strings in this repo are correct and must be left
    # alone: k8s/04-tenant-api/deployment.yaml, docs/assets/platform-data.json
    # (which is GENERATED from the deploy SSOT — a rule here would fight
    # generate_platform_data.py) and the portal's images.js are IMAGE-RUNTIME
    # pins, and the chart deploys `:v${appVersion}` on purpose. This row is a
    # RELEASE-LINE reference: it literally names the git tag `tenant-api/vX.Y.Z`,
    # which release.yaml gates against `version:`.
    #
    # Version and tag sit on one line and are always equal, so one rule owns
    # both — the same shape as the 平台文件 row in _build_platform_rules().
    rules.append({
        "file": "components/da-tools/README.md",
        "desc": "tenant-api version + git tag in da-tools strategy table",
        "pattern": (r"\| tenant-api \| v" + _SEMVER
                    + r" \| `tenant-api/v" + _SEMVER + "`"),
        "replacement": lambda v: f"| tenant-api | v{v} | `tenant-api/v{v}`",
        "require_match": True,
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
            "pattern": r"^version:\s*" + _SEMVER,
            "replacement": lambda v: f"version: {v}",
        },
        {
            "file": "helm/threshold-exporter/Chart.yaml",
            "desc": "Chart.yaml appVersion",
            "pattern": r'^appVersion:\s*"' + _SEMVER + '"',
            "replacement": lambda v: f'appVersion: "{v}"',
        },
        {
            "file": "docs/migration-guide.md",
            "desc": "OCI chart --version in migration guide",
            "pattern": r"oci://ghcr\.io/vencil/charts/threshold-exporter --version " + _SEMVER,
            "replacement": lambda v: f"oci://ghcr.io/vencil/charts/threshold-exporter --version {v}",
        },
        {
            "file": "components/threshold-exporter/README.md",
            "desc": "OCI chart --version in exporter README",
            "pattern": r"oci://ghcr\.io/vencil/charts/threshold-exporter --version " + _SEMVER,
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
            "pattern": r"oci://ghcr\.io/vencil/charts/threshold-exporter --version " + _SEMVER,
            "replacement": lambda v: f"oci://ghcr.io/vencil/charts/threshold-exporter --version {v}",
        },
        {
            "file": "docs/integration/gitops-deployment.en.md",
            "desc": "OCI chart --version in gitops deployment guide (en)",
            "pattern": r"oci://ghcr\.io/vencil/charts/threshold-exporter --version " + _SEMVER,
            "replacement": lambda v: f"oci://ghcr.io/vencil/charts/threshold-exporter --version {v}",
        },
        {
            "file": "components/da-tools/README.md",
            "desc": "exporter version in da-tools strategy table",
            "pattern": r"\| threshold-exporter \| v" + _SEMVER,
            "replacement": lambda v: f"| threshold-exporter | v{v}",
        },
        {
            "file": "components/da-tools/README.md",
            "desc": "exporter git tag in da-tools strategy table",
            "pattern": r"`exporter/v" + _SEMVER + "`",
            "replacement": lambda v: f"`exporter/v{v}`",
        },
        # ⚠️ Deleted: OCI chart inline version in docs/index.md. That page was
        # rewritten into a router ("想試哪個？…") and contains no `oci://`
        # reference at all now.
        # Exporter image tag in API docs
        {
            "file": "docs/api/README.md",
            "desc": "exporter image tag in API docs (zh)",
            "pattern": r"ghcr\.io/vencil/threshold-exporter:v?" + _SEMVER,
            "replacement": lambda v: f"ghcr.io/vencil/threshold-exporter:v{v}",
        },
        {
            "file": "docs/api/README.en.md",
            "desc": "exporter image tag in API docs (en)",
            "pattern": r"ghcr\.io/vencil/threshold-exporter:v?" + _SEMVER,
            "replacement": lambda v: f"ghcr.io/vencil/threshold-exporter:v{v}",
        },
        # OCI chart --version in scenario docs
        {
            "file": "docs/scenarios/multi-cluster-federation.md",
            "desc": "OCI chart --version in federation scenario (zh)",
            "pattern": r"oci://ghcr\.io/vencil/charts/threshold-exporter --version " + _SEMVER,
            "replacement": lambda v: f"oci://ghcr.io/vencil/charts/threshold-exporter --version {v}",
        },
        {
            "file": "docs/scenarios/multi-cluster-federation.en.md",
            "desc": "OCI chart --version in federation scenario (en)",
            "pattern": r"oci://ghcr\.io/vencil/charts/threshold-exporter --version " + _SEMVER,
            "replacement": lambda v: f"oci://ghcr.io/vencil/charts/threshold-exporter --version {v}",
        },
        {
            "file": "docs/migration-guide.en.md",
            "desc": "OCI chart --version in migration guide (en)",
            "pattern": r"oci://ghcr\.io/vencil/charts/threshold-exporter --version " + _SEMVER,
            "replacement": lambda v: f"oci://ghcr.io/vencil/charts/threshold-exporter --version {v}",
        },
        {
            "file": "mkdocs.yml",
            "desc": "mkdocs.yml exporter_version",
            "pattern": r'exporter_version:\s+"' + _SEMVER + '"',
            "replacement": lambda v: f'exporter_version: "{v}"',
        },
    ]


def _build_platform_rules():
    """Build version replacement rules for platform docs.

    Covers doc footers, headers, front matter, README intros, and mkdocs.yml.
    Returns list of rule dicts for the 'platform' version line.

    ⛔ EVERY rule that globs `docs/**/*.md` MUST set
    `skip_released_changelog: True`.

    `docs/CHANGELOG.md` is a SYMLINK to the root CHANGELOG.md, so it is a
    member of every one of those 260-file expansions — a glob rule cannot
    opt out of seeing it. Version strings inside released `## [vX.Y.Z]`
    entries are historical facts about what a past release did, not pointers
    to the current version, so rewriting them corrupts frozen history and
    reports it as an ordinary UPDATE.

    Only the `於 v` rule carried the flag, because that is the one shape that
    had already burned us (PR #503, where the stop-gap was to reword 於→在 to
    dodge the regex). Three more live patterns were widened onto that file
    since — `**文件版本：**`, `**Document version:**`, and `**最後更新**：`
    (the last had been inert only because of a `(?=\\s*\\|)` lookahead, which
    was removed) — none of which had the flag. There are zero live instances
    inside frozen entries today, so nothing is corrupted; the flag is the
    control, and `test_every_changelog_reaching_glob_skips_frozen_history`
    now derives the required set from the glob expansions themselves rather
    than from a hand-kept list, so a NEW docs glob cannot be added without it.
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
        "skip_released_changelog": True,
    })
    rules.append({
        "file": "__glob__",
        "glob_dir": "docs",
        "glob_pattern": "**/*.md",
        "desc": "doc footer **Document version:** vX.Y.Z",
        "pattern": r"\*\*Document version:\*\*\s*v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?",
        "replacement": lambda v: f"**Document version:** v{v}",
        "skip_released_changelog": True,
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
            # docs/**/*.md reaches docs/CHANGELOG.md. Front matter is above
            # the first `## [vX.Y.Z]` heading so it stays live and is still
            # bumped; the flag costs nothing here and keeps the invariant
            # "every docs glob carries it" mechanical instead of case-by-case.
            "skip_released_changelog": True,
        })

    # Doc header blockquote pattern: `> **vX.Y.Z |` (common in doc headers)
    rules.append({
        "file": "__glob__",
        "glob_dir": "docs",
        "glob_pattern": "**/*.md",
        "desc": "doc header blockquote version (> **vX.Y.Z |)",
        "pattern": r"> \*\*v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?\*\*\s*\|",
        "replacement": lambda v: f"> **v{v}** |",
        "skip_released_changelog": True,
    })

    # ⚠️ Deleted: `inline doc header version (bold blockquote, no pipe)`,
    # pattern `> \*\*vX.Y.Z\*\*\s*$`. It expanded to 260 files and matched in
    # ZERO of them — GLOB-DEAD, found the moment glob health became a group
    # property instead of a per-file one.
    #
    # Checked before deleting: the shape it describes (a blockquote whose
    # ENTIRE content is a bold version) exists nowhere in docs/**. The two
    # live shapes are both covered elsewhere:
    #   `> **vX.Y.Z** |`    the sibling glob below/above — 26 matches.
    #   `> **vX.Y.Z** — …`  docs/integration/federation-integration{,.en}.md,
    #                       which have their own hand-written rules (and those
    #                       are require_match ON, i.e. stronger than a glob).
    # So this is obsolete, not broken: there is no file it would drive if the
    # pattern were "fixed", and widening it to `> **vX.Y.Z**` + anything would
    # just double-cover what those three rules already own.

    # Inline version text: `於 v2.0.0 統一採集` or similar inline version
    # strings in doc content.
    #
    # `skip_released_changelog`: see the ⛔ note at the top of this function —
    # this was the first rule to need it (PR #503), and it is now required of
    # EVERY docs/**/*.md glob rule, not just this one.
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
        "skip_released_changelog": True,
    })

    # Footer pattern: **最後更新**：vX.Y.Z
    #
    # The `(?=\s*\|)` lookahead this pattern used to carry required the footer
    # to continue with a pipe — and the ONE footer of this shape in the repo,
    # docs/internal/design-system-guide.md, ends the line right after the
    # version. So the rule matched nothing, anywhere, for its whole life, and
    # that footer sat at v2.6.0 while the platform shipped 2.9.0.
    #
    # ⛔ Note for the next person tempted to widen a rule instead of testing
    # it: the sibling `**文件版本：**` rule above was widened into a glob in
    # this same branch, with a comment saying it got "the same treatment as the
    # **最後更新** footer rule below". The treatment being copied was a rule
    # that had never matched anything. Copying a pattern is not evidence the
    # pattern works — GLOB-DEAD in apply_rules() is.
    rules.append({
        "file": "__glob__",
        "glob_dir": "docs",
        "glob_pattern": "**/*.md",
        "desc": "doc footer **最後更新**：vX.Y.Z pattern",
        "pattern": r"\*\*最後更新\*\*：v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?",
        "replacement": lambda v: f"**最後更新**：v{v}",
        "skip_released_changelog": True,
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
    #
    # ⚠️ This is the ONE replacement here that is not a plain f-string of the
    # version, and the reason is the badge URL grammar, not taste.
    # shields.io's static route is `/badge/<label>-<message>-<color>`: the
    # THREE fields are separated by single dashes, so a dash that belongs to
    # a field's text must be written doubled (`--`). A release version has no
    # dash, but a RELEASE CANDIDATE does — once `_SEMVER` was widened to
    # accept pre-release suffixes, `--platform 2.10.0-rc1` started emitting
    # `badge/version-v2.10.0-rc1-brightgreen`, i.e. FOUR fields, which
    # renders as the wrong badge for the entire rc window.
    #
    # Nothing would ever have flagged it: the pattern matches its own output
    # (so it is idempotent, and the prerelease-idempotency test passes), the
    # value round-trips, and `--check` reports "consistent" the whole time.
    # The only symptom is a broken image in the README.
    #
    # Escaping is confined to this rule. The pattern needs no change: `-` is
    # inside its suffix character class, so it matches the doubled form on
    # the way back in.
    for _readme in ("README.md", "README.en.md"):
        rules.append({
            "file": _readme,
            "desc": f"{_readme} version badge",
            "pattern": r"badge/version-v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?-brightgreen",
            "replacement": lambda v: f"badge/version-v{_shields_escape(v)}-brightgreen",
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
        "pattern": r"^version:\s*" + _SEMVER,
        "replacement": lambda v: f"version: {v}",
    })
    rules.append({
        "file": "helm/da-portal/Chart.yaml",
        "desc": "da-portal Chart.yaml appVersion",
        "pattern": r'^appVersion:\s*"' + _SEMVER + '"',
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
        "pattern": r"oci://ghcr\.io/vencil/charts/da-portal --version " + _SEMVER,
        "replacement": lambda v: f"oci://ghcr.io/vencil/charts/da-portal --version {v}",
    })

    # da-portal image tag in README
    rules.append({
        "file": "components/da-portal/README.md",
        "desc": "da-portal image tag in README",
        "pattern": r"ghcr\.io/vencil/da-portal:v?" + _SEMVER,
        "replacement": lambda v: f"ghcr.io/vencil/da-portal:v{v}",
    })

    # da-portal image tag in QUICKSTART.md.
    #
    # components/da-portal/README.md had a rule; QUICKSTART.md — the file
    # whose 「馬上試（≤ 2 分鐘）」 block is the copy-paste command a first-time
    # reader actually runs — did not, so it sat at v2.8.0 while every other
    # `da-portal:` pin in the repo was already v2.9.0. A stale pin here is
    # worse than a stale doc footer: the reader runs the OLD portal and
    # judges the product by it.
    #
    # `require_match` is explicit (not just the hand-written default)
    # because this rule exists precisely BECAUSE the string was uncovered:
    # if the 馬上試 block is rewritten, it must die loudly rather than
    # quietly go back to being uncovered.
    rules.append({
        "file": "components/da-portal/QUICKSTART.md",
        "desc": "da-portal image tag in QUICKSTART",
        "pattern": r"ghcr\.io/vencil/da-portal:v?" + _SEMVER,
        "replacement": lambda v: f"ghcr.io/vencil/da-portal:v{v}",
        "require_match": True,
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
            "pattern": r"^version:\s*" + _SEMVER,
            "replacement": lambda v: f"version: {v}",
        },
        {
            "file": "helm/recipe-preview/Chart.yaml",
            "desc": "recipe-preview Chart.yaml appVersion",
            "pattern": r'^appVersion:\s*"' + _SEMVER + '"',
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
    """Count the Python tools `_lib_toolcount.COUNT_SUBDIRS` covers.

    Returns ``(total_count, {subdir: count})``. Both halves are derived
    from that tuple, so widening it cannot make this disagree with the
    gate that checks the number this writes.

    ⛔ #1511: this used to hold its own three hard-coded directory paths
    and — unlike that gate — skipped no filename prefixes at all. The two
    agreed only because those three directories happen to hold no `_lib*`
    or `__init__.py`. Measured **on `5cff2359`** with one
    `scripts/tools/lint/_lib_probe.py` added: this wrote 221 into README,
    the gate warned "found 221, actual is 220", `--fix` wrote 220 back,
    and `--sync-counts --check` immediately called it outdated again.

    ⛔ The first repair for THAT still re-added the total from three
    hard-coded keys (`counts["ops"] + counts["dx"] + counts["lint"]`)
    while the gate counted `COUNT_SUBDIRS`. Measured **on `5cff2359`**
    with a fourth subdirectory declared: gate 221, this 220 — the same
    divergence one layer down, in the commit that claimed to remove it.

    ⚠️ Both figures are anchored on purpose. The tool count moves whenever
    anyone adds a tool, so an unanchored "measured N" goes stale without
    anybody editing this file — the invariant is that the two sides agree,
    not that they agree on any particular number.
    """
    tools_dir = REPO_ROOT / "scripts" / "tools"
    return len(count_scope(tools_dir)), count_by_subdir(tools_dir)


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


# The count-rule set is a FIXED EXPECTATION, exactly like VERSION_LINES.
#
# Every group used to be wrapped in `if <count> > 0:`, so a count source that
# could not be read did not fail — it deleted its own rules. Reported repro:
# move docs/assets/tool-registry.yaml away and the JSX-count rule stops
# existing, so `--sync-counts --check` (a CI step,
# .github/workflows/validate.yaml) exits 0 while docs/internal/dev-rules.md
# keeps a stale number and nothing anywhere says so.
#
# `>= 3` — the old test's floor — cannot see one of five disappear. The IDs
# below are pinned by test_count_rule_ids_are_pinned; the rules are now built
# UNCONDITIONALLY and an unreadable source becomes a NO-SOURCE diagnosis
# instead of a vanished rule.
COUNT_RULE_IDS = (
    "precommit-hook-breakdown",
    "dev-rules-jsx-tools",
    "readme-rule-pack-badge",
    "readme-python-tools",
    "readme-en-python-tools",
)


def _build_count_rules():
    """Build count replacement rules for the files named in COUNT_RULE_IDS.

    ⚠️ Not "CLAUDE.md and README.md" — that was true before the docs count was
    removed, and the surviving five rules also drive `README.en.md` and
    `docs/internal/dev-rules.md`. A stale scope sentence here is how a caller
    concludes a file is covered when no rule points at it.

    Returns list of rule dicts for count syncing, one per COUNT_RULE_IDS entry
    and always in that order. Each rule carries:

      id          stable identity, pinned by the tests.
      source_ok   False when the count could not be READ (missing/empty
                  source). apply_count_updates() turns that into NO-SOURCE —
                  a third diagnosis kept apart from MISSING (the doc being
                  patched is gone) and DEAD (the doc is there but the sentence
                  moved), because the three have three different fixes.
      source      human-readable "which file feeds this count".
    """
    # Only the counts that a surviving rule actually embeds are read here.
    # The per-directory ops/dx/lint split and the docs count are still
    # reported by `--sync-counts` (main() calls the _count_* helpers
    # directly) — they just no longer have a doc sentence to patch.
    total_tools, _breakdown = _count_python_tools()
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
    rules.append({
        "id": "precommit-hook-breakdown",
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
        "source": ".pre-commit-config.yaml (hook stages)",
        "source_ok": auto_n + manual_n + push_n > 0,
    })

    # dev-rules.md: 互動工具 SOP 章節「N 個 JSX 互動工具」count
    # Phase .c 期間自 39 增至 43；hardcoded count is drift surface.
    rules.append({
        "id": "dev-rules-jsx-tools",
        "file": "docs/internal/dev-rules.md",
        "desc": f"dev-rules.md: JSX 互動工具 count ({jsx_tools} tools)",
        "pattern": r"專案有\s+\*\*\d+\s+個\s+JSX\s+互動工具\*\*",
        "replacement": lambda _: f"專案有 **{jsx_tools} 個 JSX 互動工具**",
        "is_count": True,
        "source": "docs/assets/tool-registry.yaml",
        "source_ok": jsx_tools > 0,
    })

    # README.md: 15 個 Rule Pack (in badge)
    rules.append({
        "id": "readme-rule-pack-badge",
        "file": "README.md",
        "desc": f"README.md: Rule Pack badge ({rule_packs} packs)",
        "pattern": r"badge/rule%20packs-(\d+)-orange",
        "replacement": lambda _: f"badge/rule%20packs-{rule_packs}-orange",
        "is_count": True,
        "source": "docs/assets/platform-data.json (fallback: k8s/03-monitoring/)",
        "source_ok": rule_packs > 0,
    })

    # README.md / README.en.md: Python tools count in the repo-map row.
    # Same scope as _count_python_tools (scripts/tools/{ops,dx,lint}); the
    # repo-map phrasing differs from CLAUDE.md so needs its own rule (else drifts).
    rules.append({
        "id": "readme-python-tools",
        "file": "README.md",
        "desc": f"README.md: Python tools in repo-map ({total_tools} tools)",
        "pattern": r"`scripts/tools/\{ops,dx,lint\}`\s*下\s*\d+\s*個\s*Python\s*工具",
        "replacement": lambda _: f"`scripts/tools/{{ops,dx,lint}}` 下 {total_tools} 個 Python 工具",
        "is_count": True,
        "source": "scripts/tools/{ops,dx,lint}/*.py",
        "source_ok": total_tools > 0,
    })
    rules.append({
        "id": "readme-en-python-tools",
        "file": "README.en.md",
        "desc": f"README.en.md: Python tools in repo-map ({total_tools} tools)",
        "pattern": r"\+\s*\d+\s*Python tools under\s*`scripts/tools/\{ops,dx,lint\}`",
        "replacement": lambda _: f"+ {total_tools} Python tools under `scripts/tools/{{ops,dx,lint}}`",
        "is_count": True,
        "source": "scripts/tools/{ops,dx,lint}/*.py",
        "source_ok": total_tools > 0,
    })

    # Self-check: the built set must BE the declared expectation. Without this,
    # deleting a rule here and forgetting COUNT_RULE_IDS would reintroduce the
    # very "the set is whatever got built" semantics this replaced.
    built = tuple(r["id"] for r in rules)
    if built != COUNT_RULE_IDS:
        raise AssertionError(
            f"_build_count_rules() produced {built}, expected {COUNT_RULE_IDS}. "
            f"Adding or removing a count rule means updating COUNT_RULE_IDS in "
            f"the same edit — the pinned set is what makes a vanished rule a "
            f"failure instead of silence.")

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
        if not rule.get("source_ok", True):
            # NO-SOURCE: the count itself could not be read. Distinct from
            # MISSING (the doc to patch is gone) and DEAD (the doc is there,
            # the sentence moved) because the fix is different again: restore
            # or repoint the SOURCE. Previously this state deleted the rule,
            # so `--sync-counts --check` exited 0 with a stale number in the
            # doc and no output naming it at all.
            changes.append((
                "NO-SOURCE", rule["desc"],
                f"count source unreadable: {rule['source']} — this rule has "
                f"no number to sync, so {rule['file']} keeps whatever it "
                f"says today. Restore the source, or delete the rule (and its "
                f"COUNT_RULE_IDS entry) if the count is retired."))
            continue

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
        m = re.search(r'^appVersion:\s*"(' + _SEMVER + ')"', content, re.MULTILINE)
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
        if re.match(r"^" + _SEMVER + "$", ver):
            versions["tools"] = ver

    # Portal version from da-portal Chart.yaml (version == appVersion).
    if DA_PORTAL_CHART_YAML.exists():
        content = DA_PORTAL_CHART_YAML.read_text(encoding="utf-8")
        m = re.search(
            r'^appVersion:\s*"(' + _SEMVER + ')"', content, re.MULTILINE
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
            r'^version:\s*"?(' + _SEMVER + ')"?', content, re.MULTILINE
        )
        if m:
            versions["tenant-api"] = m.group(1)

    # recipe-preview version from its Chart.yaml (sync-bump; version == appVersion).
    if RECIPE_PREVIEW_CHART_YAML.exists():
        content = RECIPE_PREVIEW_CHART_YAML.read_text(encoding="utf-8")
        m = re.search(
            r'^appVersion:\s*"(' + _SEMVER + ')"', content, re.MULTILINE
        )
        if m:
            versions["recipe-preview"] = m.group(1)

    return versions


def missing_version_lines(versions=None):
    """Which of the six lines failed to read their source-of-truth?

    Returns [(line, source_path, expected_shape), ...] for every line in
    VERSION_LINES that `read_current_versions()` did NOT produce.

    This is the whole point of VERSION_LINES existing. `read_current_versions`
    reports what it could parse; only a comparison against a FIXED expectation
    can tell "this line is at 2.9.0" apart from "this line is gone" — and the
    second one used to be indistinguishable from "there is no such line",
    which is why an unreadable SSOT silently deleted that line's entire rule
    set while every gate stayed green.
    """
    if versions is None:
        versions = read_current_versions()
    return [(line, *VERSION_LINE_SOURCES[line])
            for line in VERSION_LINES if not versions.get(line)]


def _path_is_under(path: str, scope: str) -> bool:
    """Path containment by SEGMENT, not by string prefix.

    ⛔ `str.startswith` made `--scope doc` select `docs/**` — a one-character
    typo that passes both emptiness guards (it still selects the glob rules)
    while silently dropping every hand-written `docs/**` rule. "Selected
    something" is not "selected what you meant", and the guards only ever
    checked the former.
    """
    if scope == ".":
        return True
    parts = PurePosixPath(path.replace("\\", "/")).parts
    want = PurePosixPath(scope.replace("\\", "/")).parts
    return parts[:len(want)] == want


def _filter_by_scope(rules, scope):
    """Filter rules to those whose target lies under `scope`."""
    if not scope:
        return rules
    scope = scope.rstrip("/").rstrip("\\")
    filtered = []
    for rule in rules:
        f = rule.get("file", "")
        if f == "__glob__":
            # ⛔ Either direction of containment selects the rule. A glob
            # rooted at `docs` and a scope of `docs/integration` overlap —
            # the glob owns files inside the scope — but requiring the
            # glob_dir to be under the scope dropped the whole group, so
            # `--scope docs/integration` silently skipped the front-matter
            # rule that governs the very files being scoped.
            gd = rule.get("glob_dir", "")
            if _path_is_under(gd, scope) or _path_is_under(scope, gd):
                filtered.append(rule)
        elif _path_is_under(f, scope):
            filtered.append(rule)
    return filtered


def _scoped_rules(rules, scope):
    """Rules for `scope`, narrowed on both sides of glob expansion.

    ⛔ Two filters, deliberately. The first (pre-expansion) must be generous
    so a glob rooted ABOVE the scope is not dropped — `--scope
    docs/integration` needs the `docs/**` front-matter rule, because that
    rule is what governs the files being scoped. The second (post-expansion)
    must be strict, or that same generosity applies the rule to every file
    in the tree and a scoped bump quietly becomes a repo-wide one.

    Sentinels (a collapsed glob) are kept: they carry a diagnosis, not a
    file, and dropping them would restore the silence they exist to break.
    """
    selected = _filter_by_scope(rules, scope)
    expanded = _expand_glob_rules(selected)
    if not scope:
        # ⛔ Always expanded, even with no scope. `--what-if` iterates this
        # result directly and treats each entry as a file, so returning the
        # unexpanded `__glob__` sentinels made every glob read as MISSING.
        # `apply_rules` re-expands, which is a no-op on already-expanded
        # rules, so both callers can rely on one shape.
        return expanded
    # ⛔ Which globs did this scope ACTUALLY narrow? Marking every glob as
    # narrowed whenever a scope was passed at all suppressed genuine,
    # repo-wide GLOB-DEAD: `--scope docs` (the tool's own --help example)
    # selects exactly the same files as no scope for a glob rooted at `docs`,
    # yet the health verdict for that glob vanished. The suppression is only
    # justified where the subset is genuinely smaller than the whole.
    total = {}
    kept = {}
    for rule in expanded:
        gid = rule.get("glob_id")
        if gid is None:
            continue
        total[gid] = total.get(gid, 0) + 1
        if rule.get("glob_collapsed") or _path_is_under(rule.get("file", ""),
                                                        scope):
            kept[gid] = kept.get(gid, 0) + 1
    out = []
    for rule in expanded:
        if not (rule.get("glob_collapsed")
                or _path_is_under(rule.get("file", ""), scope)):
            continue
        gid = rule.get("glob_id")
        if gid is not None and kept.get(gid, 0) < total.get(gid, 0):
            rule = dict(rule, scope_narrowed=True)
        out.append(rule)
    return out


def _requires_match(rule):
    """Is zero matches a DEAD rule (True) or a legitimate no-op (False)?

    Default ON for hand-written rules, OFF for glob-expanded ones; an
    explicit `require_match` key always wins. See apply_rules() for why the
    two classes have opposite defaults.
    """
    if "require_match" in rule:
        return bool(rule["require_match"])
    return not rule.get("from_glob", False)


GLOB_EMPTY_FILE = "__glob_empty__"


def _glob_id(rule):
    """Stable identity of a `__glob__` rule, used to group its expansion."""
    return f"{rule['glob_dir']}/{rule['glob_pattern']} ({rule['desc']})"


def _expand_glob_rules(rules):
    """Expand __glob__ rules into per-file rules.

    Each expanded rule carries `from_glob: True`. That marker is what lets
    apply_rules() default `require_match` ON for hand-written rules while
    leaving it OFF here: a hand-written rule names ONE file and one shape, so
    zero matches means it is broken, whereas a glob rule is fanned out across
    every file in a tree and legitimately matches nothing in most of them.

    Each expanded rule ALSO carries `glob_id`, because per-file `require_match`
    is not the glob's health check — the group's total is. See apply_rules()'s
    GLOB-DEAD pass.

    A glob that expands to ZERO files gets one sentinel entry
    (`file: GLOB_EMPTY_FILE`, `glob_collapsed: True`) instead of nothing at
    all. Emitting nothing is what made a collapsed glob invisible to every
    runtime gate: no rule, therefore no SKIP, no DEAD, no MISSING, and a
    rule-count floor cannot notice a few hundred entries going missing out of
    ~2900.

    Every OTHER key is copied verbatim. It used to copy a hard-coded list of
    five, so `require_match` / `whole_file` / `pair_anchor` / `pair_key` set on
    a glob rule were silently dropped — while apply_rules' own docstring
    promised "opt OUT with an explicit `require_match: False`". Nothing set
    them today; a silent no-op that only bites the next author is exactly the
    failure mode this file exists to remove.
    """
    expanded = []
    for rule in rules:
        if rule.get("file") != "__glob__":
            expanded.append(rule)
            continue

        gid = _glob_id(rule)
        glob_dir = REPO_ROOT / rule["glob_dir"]
        found = 0
        for fpath in sorted(glob_dir.glob(rule["glob_pattern"])):
            rel = fpath.relative_to(REPO_ROOT)
            per_file = dict(rule)
            per_file.pop("glob_dir", None)
            per_file.pop("glob_pattern", None)
            per_file.update({
                # ⛔ `as_posix()`, not `str()`. Every rule's "file" is a
                # repo-relative KEY: callers compare it against literals like
                # "docs/CHANGELOG.md" and against `--scope` prefixes, and the
                # rule tables themselves are written with forward slashes. On
                # Windows `str(Path)` yields backslashes, so those lookups all
                # missed — which did not merely go red, it made
                # `_changelog_reaching_rules()` return an EMPTY set and its
                # consumer pass vacuously. A guard that reports success by
                # examining nothing is worse than one that fails.
                "file": rel.as_posix(),
                "desc": f"{rule['desc'].split(' in ')[0]} in {rel.as_posix()}",
                "from_glob": True,
                "glob_id": gid,
                "glob_desc": rule["desc"],
            })
            expanded.append(per_file)
            found += 1

        if found == 0:
            expanded.append({
                "file": GLOB_EMPTY_FILE,
                "desc": rule["desc"],
                "pattern": rule["pattern"],
                "replacement": rule["replacement"],
                "from_glob": True,
                "glob_id": gid,
                "glob_collapsed": True,
                "glob_dir": rule["glob_dir"],
                "glob_pattern": rule["glob_pattern"],
            })
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
                    in most FILES is the normal case, not a defect. Its
                    health is therefore asserted at the GROUP level, below.

    Opt OUT with an explicit `require_match: False` plus a comment naming
    the reason — never silently, or the signal decays back to noise.

    Two group-level diagnoses cover what per-file `require_match` cannot see.
    Both are runtime, not test-only: `make pre-tag` never runs pytest, so a
    pytest-only tripwire is not a release gate.

      GLOB-EMPTY  the glob expanded to zero FILES — it is pointed at a tree
                  that no longer holds those files (JSX moved to
                  tools/portal/src/ in TRK-230 and two `docs/**/*.jsx` globs
                  went silent for two releases).
      GLOB-DEAD   the glob expanded fine but matched NOTHING across its whole
                  expansion. Three rules were in this state when it was added,
                  one of them rotting a real footer:
                  docs/internal/design-system-guide.md sat at v2.6.0 because
                  the `**最後更新**` pattern carried a `(?=\\s*\\|)` lookahead
                  and that footer has no pipe. "Expands to >=1 file" — the
                  previous, test-only assertion — passes happily for all
                  three: 260 files, zero matches.
    """
    rules = _expand_glob_rules(rules)
    changes = []
    # glob_id -> [total matches across the expansion, desc]
    glob_hits = {}

    def _note_glob(rule, hits):
        gid = rule.get("glob_id")
        if gid is None:
            return
        # ⛔ GLOB-DEAD is a property of the WHOLE glob, not of whatever the
        # caller scoped. Under `--scope docs/integration` a `docs/**` rule
        # legitimately matches nothing in that subset while being perfectly
        # healthy across the tree, and reporting it dead would make a scoped
        # bump fail for a defect that does not exist. Health is assessed on
        # the full expansion — run without `--scope` for that.
        if rule.get("scope_narrowed"):
            return
        entry = glob_hits.setdefault(gid, [0, rule.get("glob_desc", rule["desc"])])
        entry[0] += hits

    for rule in rules:
        if rule.get("glob_collapsed"):
            _note_glob(rule, 0)
            changes.append((
                "GLOB-EMPTY", rule["desc"],
                f"glob {rule['glob_dir']}/{rule['glob_pattern']} expanded to "
                f"ZERO files — the whole rule is invisible (no per-file rule "
                f"exists, so there is no SKIP/DEAD/MISSING to see). Point "
                f"glob_dir/glob_pattern at where those files live now, or "
                f"delete the rule if that tree is gone."))
            continue

        fpath = REPO_ROOT / rule["file"]
        if not fpath.exists():
            _note_glob(rule, 0)
            changes.append(("SKIP", rule["desc"], f"file not found: {rule['file']}"))
            continue

        content = fpath.read_text(encoding="utf-8")

        if rule.get("whole_file"):
            # A whole-file rule owns the file outright, so its contribution to
            # the group is "this file is driven", not a match count.
            _note_glob(rule, 1)
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
            _note_glob(rule, len(old_values))
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
        _note_glob(rule, len(matches))
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

        # ⛔ `any`, not `all`. A file legitimately holds a MIX: someone
        # hand-fixed one occurrence and left the other, which is exactly the
        # state `replaced N occurrence(s)` is worded for. Under `all` that file
        # reports "already up to date", nothing is written, `--check` stays
        # green, and the stale pin is never bumped again. Every fixture in the
        # suite wrote exactly ONE occurrence, so the two were indistinguishable
        # everywhere (#1407, seventh-round mutation pass).
        needs_update = any(m != replacement for m in matches)
        if needs_update:
            new_content = re.sub(pattern, replacement, scan_text,
                                 flags=re.MULTILINE) + frozen_tail
            # Build diff detail. ⛔ Name a value that actually CHANGES: with a
            # mixed file, `sorted(set(matches))[0]` can be the occurrence that
            # was already correct, and the line then reads
            # "replaced 2 occurrence(s): v2.10.0 → v2.10.0" — a no-op report
            # for a real rewrite.
            unique_old = sorted(set(matches))
            stale = [m for m in unique_old if m != replacement]
            diff_detail = (f"replaced {len(matches)} occurrence(s): "
                           f"{(stale or unique_old)[0]} → {replacement}")
            if dry_run:
                diff_detail = f"[dry-run] {diff_detail}"
            changes.append(("UPDATE", rule["desc"], diff_detail))
            if not check_only and not dry_run:
                fpath.write_text(new_content, encoding="utf-8", newline="\n")
                os.chmod(fpath, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
        else:
            changes.append(("OK", rule["desc"], "already up to date"))

    # Group-level verdict. A glob whose ENTIRE expansion matched nothing is not
    # "a tree where the string happens to be absent" — it is a pattern that
    # describes a shape the repo no longer contains, i.e. a rule that bumps
    # nothing. GLOB-EMPTY already spoke for the zero-file case, so it is not
    # re-reported here: one defect, one diagnosis.
    for gid, (hits, desc) in sorted(glob_hits.items()):
        if hits == 0 and not any(
                c[0] == "GLOB-EMPTY" and c[1] == desc for c in changes):
            changes.append((
                "GLOB-DEAD", desc,
                f"glob {gid} expanded to files but matched NOTHING in ANY of "
                f"them — per-file `require_match` is off for globs, so every "
                f"one of those files reported OK while the rule bumped "
                f"nothing. Fix the \"pattern\", narrow the glob to where the "
                f"shape really lives, or delete the rule with a stated "
                f"reason."))

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


_SCOPE_HINT = ("(it is matched against each rule's \"file\" / \"glob_dir\" "
               "prefix, repo-relative, e.g. docs, components, helm; use '.' "
               "for root-level files)")


def _require_semver_shape(requested: list) -> None:
    """Reject a version argument this tool cannot round-trip.

    ⛔ The damage necessarily precedes the detection, which is why this is a
    guard and not a check. Every rule matches on the OLD value, so the first
    run with a malformed version always succeeds: `--platform 2.10.0+build.5`
    (`+` is outside `_SEMVER`'s suffix class) and the truncation typo
    `--platform 2.10` both report `✅ Done. 339 update(s) applied.` and exit
    0. Only afterwards does `--check` go permanently red — and the first
    thing an engineer does is re-run the same command, which appends the
    suffix a second time: `v2.10.0+build.5+build.5`.

    This is the failure `_SEMVER`'s own comment describes ("a write appends
    the suffix again"), reached from the input side. `_SEMVER` is the shape
    every rule pattern is built from, so it is exactly the right acceptance
    test: a value that does not match it cannot be written and then matched
    back. The repo is not at fault here, so this is a caller error.
    """
    bad = [(line, ver) for line, ver in requested
           if not re.fullmatch(_SEMVER, ver.lstrip('v'))]
    if not bad:
        return
    for line, ver in bad:
        print(f"ERROR: --{line} {ver!r} is not a version this tool can "
              f"round-trip (expected MAJOR.MINOR.PATCH with an optional "
              f"-suffix, e.g. 2.10.0 or 2.10.0-rc1).", file=sys.stderr)
    print("Nothing was written. A malformed version would be applied to "
          "hundreds of files, report success, and then append its suffix "
          "again on the re-run.", file=sys.stderr)
    sys.exit(EXIT_CALLER_ERROR)


def _require_nonempty_scope(all_rules, scope):
    """A `--scope` that selects ZERO rules ANYWHERE is a caller error.

    Measured: `--check --scope nosuchdir` printed "✅ All version references
    are consistent." and exited 0 — the same green as a real clean run, from a
    command that checked nothing at all. A typo'd scope in a release script is
    therefore indistinguishable from success.

    ⚠️ This is the REPO-WIDE floor only. It sums the filter over all six
    lines while the bump/check loops filter PER LINE, so a scope that is
    non-empty globally but empty for the line being worked on sails past it
    and then evaluates nothing — exactly the hole this guard's own docstring
    claims to close. `_require_nonempty_line_scope()` below is the per-line
    half; both are needed (this one still catches the typo'd scope in bare
    `--check`, where no single line is "requested").

    Exits EXIT_CALLER_ERROR (2, "you invoked me wrong"), NOT EXIT_VIOLATION
    (1, "the repo is wrong") — nothing is drifting; the filter is.
    """
    if not scope:
        return
    total = sum(len(_scoped_rules(rules, scope))
                for rules in all_rules.values())
    if total == 0:
        print(f"ERROR: --scope {scope!r} selected ZERO rules. Nothing would "
              f"be checked or bumped, so this cannot report success. Check "
              f"the path {_SCOPE_HINT}.", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)


def _require_nonempty_line_scope(all_rules, scope, requested_lines):
    """A `--scope` that selects zero rules for a REQUESTED line is a caller error.

    Measured, with the repo-wide guard above in place and passing:

        bump_docs.py --tenant-api 9.9.9 --scope docs
          → "✅ Done. 0 update(s) applied."          exit 0
        bump_docs.py --check --tenant-api 9.9.9 --scope docs
          → "✅ All version references are already up to date."  exit 0

    `--scope docs` selects ~2100 rules across the platform and tools lines, so
    the repo-wide sum is nowhere near zero — but tenant-api owns nothing under
    docs/, and tenant-api is the ONLY line the caller asked about. Both
    commands evaluated exactly nothing and reported success for the work they
    were asked to do. In a release script that is the same failure the
    repo-wide guard exists to prevent, one level down.

    `requested_lines` is what the invocation actually asks for — the lines
    carrying a version flag. It is deliberately NOT "all six": a bare
    `--check --scope docs` legitimately means "check the docs tree", and
    recipe-preview owning nothing there is the scope working as asked, not a
    defect. Those cases are REPORTED instead (SCOPE-EMPTY), so they stop
    being invisible without turning every scoped check into an error.

    Called BEFORE any line is processed, so a bad invocation cannot write
    half a bump and then fail.
    """
    if not scope:
        return
    empty = [line for line in requested_lines
             if not _scoped_rules(all_rules.get(line, []), scope)]
    if empty:
        print(f"ERROR: --scope {scope!r} selected ZERO rules for the "
              f"requested version line(s): {', '.join(empty)}. Those lines "
              f"would be reported as done/consistent without a single rule "
              f"being evaluated. Drop --scope, or scope to a directory that "
              f"line owns {_SCOPE_HINT}.", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)


def _flag_name(dest: str) -> str:
    """argparse `dest` → the option string a caller typed."""
    return "--" + dest.replace("_", "-")


def _scope_empty_note(line, all_rules, scope):
    """SCOPE-EMPTY text for a line the caller did not single out, or None.

    Distinct label, kept apart from NO-SSOT / MISSING / DEAD / GLOB-EMPTY /
    GLOB-DEAD as those are from each other, because it means something none of
    them do: the SSOT read fine and every rule is healthy — `--scope` simply
    excluded all of them, so this line was NOT looked at. Previously a
    scope-excluded line printed nothing at all, so `--check --scope helm`
    dropped the entire platform line in silence and still said
    "✅ All version references are consistent."
    """
    if not scope or _scoped_rules(all_rules.get(line, []), scope):
        return None
    return (f"--scope {scope!r} excluded all "
            f"{len(all_rules.get(line, []))} rule group(s) on this line — it "
            f"was NOT checked. Widen or drop --scope to cover it.")


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
                        help="Auto-update hardcoded counts (pre-commit hooks, "
                             "JSX tools, rule packs, Python tools) across "
                             "CLAUDE.md, README.md, README.en.md and "
                             "docs/internal/dev-rules.md")

    args = parser.parse_args()

    # --sync-counts: auto-update all hardcoded counts
    if args.sync_counts:
        # Count rules are a hand-written, single-file set with no glob_dir, so
        # `--scope` has nothing to filter on and was simply ignored; the six
        # version flags are not read on this path either. Both were accepted
        # in silence, so `--sync-counts --platform 2.10.0` did the counts and
        # NOT the bump while exiting 0 — a release script could lose a whole
        # version bump that way and never see a word about it. Reject the
        # combination instead of pretending to honour it. EXIT_CALLER_ERROR
        # (2) for the same reason as the --scope guards: the repo is fine, the
        # invocation is not.
        # ⛔ The list must be the COMPLEMENT of what this branch honours, not
        # an enumeration of what someone remembered. It shipped naming the six
        # version flags and `--scope`, and stopped there — so
        # `--sync-counts --init-changelog 2.10.0` printed "✅ Done." and exited
        # 0 having written no CHANGELOG stub at all: the exact silent discard
        # the message below says the guard exists to end, one flag over. And
        # `--init-changelog` is the one that WRITES.
        #
        # Derived from argparse so a flag added later cannot join the gap in
        # silence: everything the parser declares, minus the two this branch
        # actually acts on.
        # ⚠️ Compare against the PARSER's default, not against None/False:
        # `--changelog-lang` defaults to a non-empty string, so an
        # emptiness test flags it on every run and the guard rejects a bare
        # `--sync-counts`.
        # ⛔ The honoured set is what THIS BRANCH READS, and nothing else —
        # `apply_count_updates(check_only=args.check, dry_run=args.dry_run)` is
        # the whole of it. Getting this list wrong in either direction is a
        # defect: too small and a legitimate `--sync-counts --dry-run` is
        # rejected (measured — it broke
        # `test_plain_sync_counts_also_fails_on_no_source`), too large and the
        # silent-discard hole reopens.
        # ⛔ The question is "did the caller PASS this flag", not "does its
        # value differ from the default". Those come apart for any flag with a
        # non-empty default: `--changelog-lang` defaults to "zh", so
        # `--sync-counts --changelog-lang zh` was accepted in silence — the
        # very shape this guard exists to end, reached by explicitly typing
        # the default. Value-comparison is kept as a UNION term so a caller
        # that builds the namespace programmatically (no argv) is still
        # caught; presence is what argv-driven runs are judged on.
        _HONOURED = {"sync_counts", "check", "dry_run", "help"}
        _opt_to_dest = {
            opt: act.dest
            for act in parser._actions for opt in act.option_strings
        }
        # ⛔ argparse accepts unambiguous PREFIXES by default (`allow_abbrev`),
        # so an exact-name lookup is not "did the caller pass this flag" — it
        # is "did the caller spell it out in full". Measured: `--changelog-la`
        # and `--change` both reach argparse as `--changelog-lang` and both
        # slipped through with rc=0, i.e. the same silent discard this guard
        # exists to end, one abbreviation over. Resolving prefixes here rather
        # than setting `allow_abbrev=False` keeps the CLI's accepted spellings
        # unchanged — this is a detection fix, not a behaviour change.
        def _dest_for(name: str) -> str | None:
            if name in _opt_to_dest:
                return _opt_to_dest[name]
            if not name.startswith("--"):
                return None
            hits = {d for o, d in _opt_to_dest.items() if o.startswith(name)}
            return hits.pop() if len(hits) == 1 else None

        passed = set()
        for token in sys.argv[1:]:
            if not token.startswith("-"):
                continue
            dest = _dest_for(token.split("=", 1)[0])
            if dest is not None:
                passed.add(dest)
        ignored = sorted(
            _flag_name(dest)
            for dest, value in vars(args).items()
            if dest not in _HONOURED
            and (dest in passed or value != parser.get_default(dest))
        )
        if ignored:
            print(f"ERROR: --sync-counts does not accept {', '.join(ignored)} "
                  f"— it syncs hardcoded COUNTS, which have no version and no "
                  f"scope filter, and those flags were previously accepted and "
                  f"then silently discarded. Run --sync-counts on its own, and "
                  f"the version bump as a separate command.", file=sys.stderr)
            sys.exit(EXIT_CALLER_ERROR)

        total_tools, tool_breakdown = _count_python_tools()
        rule_packs = _count_rule_packs()
        jsx_tools = _count_jsx_tools()
        docs = _count_docs()
        hooks = _count_precommit_hooks()

        print("Current counts detected:")
        print(f"  Python tools (total): {total_tools}")
        # ⛔ #1511: iterate the breakdown instead of printing three named
        # locals. A fourth counted subdirectory used to vanish from this
        # listing while the total silently stayed on the old three.
        for _subdir, _count in tool_breakdown.items():
            print(f"    - {_subdir}/: {_count}")
        print(f"  Rule Packs: {rule_packs}")
        print(f"  JSX tools: {jsx_tools}")
        print(f"  Documentation files: {docs}")
        print(f"  Pre-commit hooks: {hooks}")
        print()

        changes = apply_count_updates(check_only=args.check, dry_run=args.dry_run)
        for status, desc, detail in changes:
            # DEAD / MISSING / NO-SOURCE are ❌, never ⚠️ — they fail the gate
            # now, and an icon that reads as "advisory" is what let 7 dead
            # count rules pass as green ticks.
            icon = {"UPDATE": "📝", "OK": "✅", "DEAD": "❌",
                    "MISSING": "❌", "NO-SOURCE": "❌"}[status]
            print(f"  {icon} {desc}: {detail}")

        update_count = sum(1 for s, _, _ in changes if s == "UPDATE")
        dead_counts = sum(1 for s, _, _ in changes if s == "DEAD")
        missing_counts = sum(1 for s, _, _ in changes if s == "MISSING")
        no_source_counts = sum(1 for s, _, _ in changes if s == "NO-SOURCE")

        # A dead/missing/source-less count rule is a defect regardless of
        # mode: in --check it must fail CI, and in a plain `--sync-counts` run
        # it must not report "Done, 0 updated" as if everything were synced.
        if no_source_counts:
            print(f"\n❌ {no_source_counts} count rule(s) could not READ their "
                  f"count source (NO-SOURCE). The rule still exists but has "
                  f"no number to sync — restore the source file, or retire "
                  f"the rule together with its COUNT_RULE_IDS entry.")
        if missing_counts:
            print(f"\n❌ {missing_counts} count rule(s) point at a file that "
                  f"does not exist (MISSING). Fix the \"file\" path in "
                  f"_build_count_rules(), or delete the rule.")
        if dead_counts:
            print(f"\n❌ {dead_counts} count rule(s) matched NOTHING (DEAD). "
                  f"A rule that matches nothing syncs nothing — fix the "
                  f"\"pattern\" in _build_count_rules().")

        broken_counts = dead_counts or missing_counts or no_source_counts
        if args.check:
            if update_count > 0:
                print(f"\n❌ {update_count} count(s) are outdated. Run without --check to apply.")
                sys.exit(EXIT_VIOLATION)
            elif broken_counts:
                sys.exit(EXIT_VIOLATION)
            else:
                print("\n✅ All counts are already up to date.")
        elif args.dry_run:
            if update_count > 0:
                print(f"\n🔍 Dry run: {update_count} count(s) would be updated.")
            elif not broken_counts:
                print("\n✅ Dry run: all counts are already up to date.")
            if broken_counts:
                sys.exit(EXIT_VIOLATION)
        else:
            print(f"\n✅ Done. {update_count} count(s) updated.")
            if broken_counts:
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
        # Iterate the EXPECTATION, not the parse result. Printing only what
        # parsed is what made the failure look like nothing at all: an
        # unreadable CLAUDE.md simply produced a five-line list, and five
        # lines look exactly as healthy as six unless you count them.
        for line in VERSION_LINES:
            ver = versions.get(line)
            print(f"  {line}: {ver}" if ver else f"  {line}: ❌ NOT FOUND")
        missing = missing_version_lines(versions)
        if missing:
            print(f"\n❌ {len(missing)} version line(s) have an unreadable "
                  f"source-of-truth (NO-SSOT):")
            for line, src, shape in missing:
                print(f"  - {line}: could not read {shape} from {src}")
            sys.exit(EXIT_VIOLATION)
        return

    # --what-if: comprehensive rule audit — show all rules and their status
    if args.what_if:
        versions = read_current_versions()
        if not versions:
            print("ERROR: Cannot read current versions from source files", file=sys.stderr)
            sys.exit(EXIT_CALLER_ERROR)

        all_rules = _build_rules()
        _require_nonempty_scope(all_rules, args.scope)
        total_rules = 0
        matched = 0
        unmatched = 0
        missing = 0
        no_ssot = 0
        glob_empty = 0
        glob_dead = 0

        for line in VERSION_LINES:
            note = _scope_empty_note(line, all_rules, args.scope)
            if note:
                # Not counted as a violation: --what-if is an audit and the
                # caller narrowed the scope on purpose. It IS printed, because
                # a silently-absent line is indistinguishable from a healthy
                # one in the Summary below.
                print(f"\n⚠️  {line}: SCOPE-EMPTY — {note}")
                continue

            ver = versions.get(line)
            if not ver:
                # Was `⚠️  … version not found in source-of-truth` + continue,
                # with no effect on the exit code. So the run that lost 2170
                # platform rules printed one warning line and still summarised
                # "670 rules, 670 ✅, 0 DEAD, 0 MISSING" at exit 0.
                no_ssot += 1
                src, shape = VERSION_LINE_SOURCES[line]
                print(f"\n❌ {line}: NO-SSOT — could not read {shape} from "
                      f"{src}. Every rule on this line is UNEVALUATED (not "
                      f"passing): {len(all_rules.get(line, []))} rule "
                      f"group(s) skipped.")
                continue

            # ⛔ Same narrowing every other mode uses. This branch built its
            # own `_expand_glob_rules(_filter_by_scope(...))` and therefore
            # applied only the GENEROUS half of the two-sided filter, so
            # `--what-if --scope docs/integration` audited the whole
            # `docs/**` tree (2090 rules) while `--dry-run` with the same
            # scope touched 5 files. `--scope` meant nothing here.
            rules = _scoped_rules(all_rules.get(line, []), args.scope)
            # glob_id -> total matches across the whole expansion
            glob_hits = {}

            print(f"\n{'='*60}")
            print(f"  {line.upper()} (current: {ver}) — "
                  f"{len(rules)} rule(s)")
            print(f"{'='*60}")

            for rule in rules:
                total_rules += 1
                desc = rule["desc"]

                if rule.get("glob_collapsed"):
                    glob_empty += 1
                    glob_hits.setdefault(rule["glob_id"], [0, desc])
                    print(f"  ❌ {desc}")
                    print(f"       GLOB-EMPTY: {rule['glob_dir']}/"
                          f"{rule['glob_pattern']} expanded to ZERO files")
                    continue

                gid = rule.get("glob_id")
                if gid is not None:
                    glob_hits.setdefault(gid, [0, desc])

                fpath = REPO_ROOT / rule["file"]

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
                    if gid is not None:
                        glob_hits[gid][0] += len(old_values)
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
                    if gid is not None:
                        glob_hits[gid][0] += 1
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
                if gid is not None:
                    glob_hits[gid][0] += len(matches)
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

            # Group verdict for this line's globs. Per-file rows above are all
            # ✅ for a glob that matches nothing anywhere — that is the whole
            # point of `require_match` defaulting OFF for them — so the only
            # place the defect can surface is here.
            # ⛔ Same suppression `apply_rules` applies: a glob whose
            # expansion this `--scope` genuinely narrowed cannot be judged
            # from the subset. `--what-if` computes its own tally, so the
            # rule has to be repeated here rather than inherited.
            _narrowed_gids = {r.get("glob_id") for r in rules
                              if r.get("scope_narrowed")}
            for gid, (hits, gdesc) in sorted(glob_hits.items()):
                if gid in _narrowed_gids:
                    continue
                if hits == 0 and not any(
                        r.get("glob_id") == gid and r.get("glob_collapsed")
                        for r in rules):
                    glob_dead += 1
                    print(f"  ❌ {gdesc}")
                    print(f"       GLOB-DEAD: {gid} expanded to files but "
                          f"matched NOTHING in ANY of them")

        print(f"\n{'='*60}")
        print(f"  Summary: {total_rules} rules, "
              f"{matched} ✅, {unmatched} ❌ DEAD/drift, {missing} ❌ MISSING, "
              f"{glob_empty} ❌ GLOB-EMPTY, {glob_dead} ❌ GLOB-DEAD, "
              f"{no_ssot} ❌ NO-SSOT")
        print(f"{'='*60}")
        # `missing` 也算 violation：規則指向不存在的檔案，跟 pattern 撈不到
        # 一樣是「這條規則什麼都沒 bump」。舊版只看 unmatched，所以 14 條指向
        # 已搬走檔案的規則能讓 --what-if exit 0（#1407）。
        #
        # `no_ssot` / `glob_empty` / `glob_dead` 同理，而且更狠：那三種狀態下
        # 規則根本沒被評估過，Summary 的 ✅ 數字是在數「我有看的那些」。一條
        # 版號線整個消失時舊版仍印 exit 0（#1407 第二輪）。
        sys.exit(EXIT_VIOLATION if (unmatched or missing or no_ssot
                                    or glob_empty or glob_dead) else EXIT_OK)

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
        _require_nonempty_scope(all_rules, args.scope)
        has_drift = False

        # Iterate the six DECLARED lines, not `versions.items()`. Iterating the
        # parse result meant a line whose SSOT stopped parsing was not checked
        # and not reported — it ceased to exist, taking its rules with it, and
        # this branch printed "✅ All version references are consistent."
        # Reproduced by rewording one sentence in CLAUDE.md: 2170 platform
        # rules gone, exit 0 (#1407 second round).
        for line in VERSION_LINES:
            note = _scope_empty_note(line, all_rules, args.scope)
            if note:
                # Printed, not fatal — see _scope_empty_note(). A bare
                # `--check --scope docs` is a legitimate "check the docs
                # tree"; what was wrong was doing it in total silence.
                print(f"  SCOPE-EMPTY [{line}] {note}")
                continue

            ver = versions.get(line)
            if not ver:
                has_drift = True
                src, shape = VERSION_LINE_SOURCES[line]
                print(f"  NO-SSOT [{line}] could not read {shape} from {src} "
                      f"— every rule on this line is UNEVALUATED. Restore the "
                      f"string, or update read_current_versions() + "
                      f"VERSION_LINE_SOURCES together if the shape moved on "
                      f"purpose.")
                continue

            rules = _scoped_rules(all_rules.get(line, []), args.scope)
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
                elif status in ("GLOB-EMPTY", "GLOB-DEAD"):
                    # 兩種 glob 層級的診斷，與 per-file 的 DEAD/MISSING 分開
                    # 標籤：per-file require_match 對 glob 是關的，所以整條
                    # glob 撈不到時，每個檔案都印綠勾，只有 group 這層看得見。
                    has_drift = True
                    print(f"  {status} [{line}] {desc}: {detail}")

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
    _require_nonempty_scope(all_rules, args.scope)

    requested = [(line, ver) for line, ver in
                 [("platform", args.platform),
                  ("exporter", args.exporter),
                  ("tools", args.tools),
                  ("portal", args.portal),
                  ("recipe-preview", args.recipe_preview),
                  ("tenant-api", args.tenant_api)]
                 if ver]
    # BEFORE the loop, so a scope that silently selects nothing for one of the
    # requested lines cannot bump the other lines first and then be discovered.
    _require_nonempty_line_scope(all_rules, args.scope,
                                 [line for line, _ in requested])
    _require_semver_shape(requested)

    total_updates = 0
    dead_rules = 0
    missing_rules = 0
    glob_broken = 0

    for line, new_ver in requested:

        # Strip leading 'v' if provided
        new_ver = new_ver.lstrip("v")

        print(f"\n{'='*60}")
        print(f"  {line.upper()} → {new_ver}")
        print(f"{'='*60}")

        rules = _scoped_rules(all_rules.get(line, []), args.scope)
        changes = apply_rules(rules, new_ver,
                              check_only=args.check, dry_run=args.dry_run)

        for status, desc, detail in changes:
            # SKIP 用 ❌ 不用 ⚠️ ——它現在會讓 bump 失敗，圖示不該再讀成「可忽略」。
            icon = {"UPDATE": "📝", "OK": "✅", "SKIP": "❌", "DEAD": "❌",
                    "GLOB-EMPTY": "❌", "GLOB-DEAD": "❌"}[status]
            print(f"  {icon} [{status}] {desc}: {detail}"
                  if status in ("GLOB-EMPTY", "GLOB-DEAD")
                  else f"  {icon} {desc}: {detail}")
            if status == "UPDATE":
                total_updates += 1
            elif status == "DEAD":
                dead_rules += 1
            elif status == "SKIP":
                missing_rules += 1
            elif status in ("GLOB-EMPTY", "GLOB-DEAD"):
                glob_broken += 1

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

    if glob_broken:
        print(f"\n❌ {glob_broken} glob rule(s) are GLOB-EMPTY (expanded to no "
              f"files) or GLOB-DEAD (expanded but matched nothing anywhere). "
              f"A release bump ran with those trees UNTOUCHED — fix "
              f"glob_dir/glob_pattern or the \"pattern\" in _build_*_rules().")

    if dead_rules or missing_rules or glob_broken:
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
