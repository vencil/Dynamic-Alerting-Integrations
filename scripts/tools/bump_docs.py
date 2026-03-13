#!/usr/bin/env python3
"""bump_docs.py — 版號一致性管理工具

掃描 repo 中的文件、Chart.yaml、VERSION 檔案，批次更新版號引用。
三條版號線獨立管理：--platform / --exporter / --tools。

Chart.yaml version 與 appVersion 同步，統一由 --exporter 管理。
--exporter 同時更新：Chart.yaml version + appVersion + image tag + OCI chart references。

用法:
  # 更新 exporter 版號 (Chart.yaml version + appVersion + image tag + OCI chart)
  python3 scripts/tools/bump_docs.py --exporter 1.1.0

  # 更新 da-tools 版號 (所有 image tag + VERSION)
  python3 scripts/tools/bump_docs.py --tools 1.1.0

  # 更新平台文件版號
  python3 scripts/tools/bump_docs.py --platform 1.1.0

  # 只檢查不修改 (CI lint 用)
  python3 scripts/tools/bump_docs.py --check

  # 組合使用
  python3 scripts/tools/bump_docs.py --platform 1.1.0 --tools 1.1.0 --exporter 1.1.0
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
REPO_ROOT = SCRIPT_DIR.parent.parent  # scripts/tools/ -> repo root

# ---------------------------------------------------------------------------
# Version source-of-truth files
# ---------------------------------------------------------------------------
CHART_YAML = REPO_ROOT / "components" / "threshold-exporter" / "Chart.yaml"
DA_TOOLS_VERSION = REPO_ROOT / "components" / "da-tools" / "app" / "VERSION"

# ---------------------------------------------------------------------------
# Replacement rules per version line
# ---------------------------------------------------------------------------
# Each rule: (file_relative_path, pattern_func, replacement_func)
# pattern_func(old_ver) -> regex pattern
# replacement_func(new_ver) -> replacement string


def _build_rules():
    """Build replacement rules. Called after REPO_ROOT is resolved."""

    # --- da-tools image tag rules ---
    da_tools_image_files = [
        "components/da-tools/README.md",
        "components/threshold-exporter/README.md",
        "docs/byo-alertmanager-integration.md",
        "docs/byo-prometheus-integration.md",
        "docs/migration-guide.md",
        "docs/custom-rule-governance.md",
        "docs/custom-rule-governance.en.md",
        "README.md",
        "README.en.md",
        "docs/shadow-monitoring-sop.md",
        "docs/byo-alertmanager-integration.en.md",
        "docs/byo-prometheus-integration.en.md",
        "docs/shadow-monitoring-sop.en.md",
        "docs/migration-guide.en.md",
        # Include snippets (single-source for embedded content)
        "docs/includes/docker-usage-pattern.md",
        "docs/includes/docker-usage-pattern.en.md",
    ]

    tools_rules = []
    for f in da_tools_image_files:
        tools_rules.append({
            "file": f,
            "desc": f"da-tools image tag in {f}",
            "pattern": r"ghcr\.io/vencil/da-tools:[0-9]+\.[0-9]+\.[0-9]+",
            "replacement": lambda v: f"ghcr.io/vencil/da-tools:{v}",
        })

    # VERSION file (exact content)
    tools_rules.append({
        "file": "components/da-tools/app/VERSION",
        "desc": "da-tools VERSION file",
        "pattern": r"^[0-9]+\.[0-9]+\.[0-9]+\s*$",
        "replacement": lambda v: f"{v}\n",
        "whole_file": True,
    })

    # da-tools README build.sh version
    tools_rules.append({
        "file": "components/da-tools/README.md",
        "desc": "da-tools build.sh version",
        "pattern": r"\./build\.sh [0-9]+\.[0-9]+\.[0-9]+",
        "replacement": lambda v: f"./build.sh {v}",
    })

    # da-tools README version header
    tools_rules.append({
        "file": "components/da-tools/README.md",
        "desc": "da-tools README version header",
        "pattern": r"\*\*版本\*\*：[0-9]+\.[0-9]+\.[0-9]+（獨立版號",
        "replacement": lambda v: f"**版本**：{v}（獨立版號",
    })

    # da-tools README version strategy table
    tools_rules.append({
        "file": "components/da-tools/README.md",
        "desc": "da-tools version strategy table (da-tools row)",
        "pattern": r"\| \*\*da-tools\*\* \| \*\*v?[0-9]+\.[0-9]+\.[0-9]+\*\*",
        "replacement": lambda v: f"| **da-tools** | **v{v}**",
    })
    tools_rules.append({
        "file": "components/da-tools/README.md",
        "desc": "da-tools version strategy table (git tag)",
        "pattern": r"\*\*`tools/v[0-9]+\.[0-9]+\.[0-9]+`\*\*",
        "replacement": lambda v: f"**`tools/v{v}`**",
    })

    # --- exporter version rules ---
    # Chart.yaml version 與 appVersion 同步（chart 版號 = exporter 版號）
    exporter_rules = [
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
    ]

    # --- platform version rules ---
    platform_rules = []

    # Doc footers: **文件版本：** vX.Y.Z or **Document version:** vX.Y.Z
    platform_rules.append({
        "file": "docs/architecture-and-design.md",
        "desc": "architecture-and-design.md footer",
        "pattern": r"\*\*文件版本：\*\*\s*v[0-9]+\.[0-9]+\.[0-9]+",
        "replacement": lambda v: f"**文件版本：** v{v}",
    })
    platform_rules.append({
        "file": "docs/architecture-and-design.en.md",
        "desc": "architecture-and-design.en.md footer",
        "pattern": r"\*\*Document version:\*\*\s*v[0-9]+\.[0-9]+\.[0-9]+",
        "replacement": lambda v: f"**Document version:** v{v}",
    })

    # Doc headers with inline version
    platform_rules.append({
        "file": "docs/architecture-and-design.md",
        "desc": "architecture-and-design.md header version",
        "pattern": r"v[0-9]+\.[0-9]+\.[0-9]+ 的技術架構",
        "replacement": lambda v: f"v{v} 的技術架構",
    })
    platform_rules.append({
        "file": "docs/architecture-and-design.en.md",
        "desc": "architecture-and-design.en.md header version",
        "pattern": r"\(v[0-9]+\.[0-9]+\.[0-9]+\)\.",
        "replacement": lambda v: f"(v{v}).",
    })

    # BYO guides version headers
    platform_rules.append({
        "file": "docs/byo-prometheus-integration.md",
        "desc": "BYOP guide version",
        "pattern": r"\*\*版本\*\*：v[0-9]+\.[0-9]+\.[0-9]+",
        "replacement": lambda v: f"**版本**：v{v}",
    })
    platform_rules.append({
        "file": "docs/byo-alertmanager-integration.md",
        "desc": "BYO Alertmanager guide version",
        "pattern": r"\*\*版本\*\*：v[0-9]+\.[0-9]+\.[0-9]+",
        "replacement": lambda v: f"**版本**：v{v}",
    })

    # Governance doc version headers
    platform_rules.append({
        "file": "docs/custom-rule-governance.md",
        "desc": "governance doc (zh) version header",
        "pattern": r"\*\*版本\*\*: v[0-9]+\.[0-9]+\.[0-9]+",
        "replacement": lambda v: f"**版本**: v{v}",
    })
    platform_rules.append({
        "file": "docs/custom-rule-governance.en.md",
        "desc": "governance doc (en) version header",
        "pattern": r"\*\*Version\*\*: v[0-9]+\.[0-9]+\.[0-9]+",
        "replacement": lambda v: f"**Version**: v{v}",
    })

    # GitOps deployment guide version header
    platform_rules.append({
        "file": "docs/gitops-deployment.md",
        "desc": "gitops-deployment.md version header",
        "pattern": r"\*\*版本\*\*：v[0-9]+\.[0-9]+\.[0-9]+",
        "replacement": lambda v: f"**版本**：v{v}",
    })

    # English doc version headers (BYO guides and gitops)
    platform_rules.append({
        "file": "docs/byo-prometheus-integration.en.md",
        "desc": "BYOP guide (en) version",
        "pattern": r"\*\*Version\*\*: v[0-9]+\.[0-9]+\.[0-9]+",
        "replacement": lambda v: f"**Version**: v{v}",
    })
    platform_rules.append({
        "file": "docs/byo-alertmanager-integration.en.md",
        "desc": "BYO Alertmanager guide (en) version",
        "pattern": r"\*\*Version\*\*: v[0-9]+\.[0-9]+\.[0-9]+",
        "replacement": lambda v: f"**Version**: v{v}",
    })
    platform_rules.append({
        "file": "docs/gitops-deployment.en.md",
        "desc": "gitops-deployment.en.md version header",
        "pattern": r"\*\*Version\*\*: v[0-9]+\.[0-9]+\.[0-9]+",
        "replacement": lambda v: f"**Version**: v{v}",
    })

    # Federation integration guide version header
    platform_rules.append({
        "file": "docs/federation-integration.md",
        "desc": "federation-integration.md version header",
        "pattern": r"> \*\*v[0-9]+\.[0-9]+\.[0-9]+\*\*",
        "replacement": lambda v: f"> **v{v}**",
    })
    platform_rules.append({
        "file": "docs/federation-integration.en.md",
        "desc": "federation-integration.en.md version header",
        "pattern": r"> \*\*v[0-9]+\.[0-9]+\.[0-9]+\*\*",
        "replacement": lambda v: f"> **v{v}**",
    })

    # threshold-exporter README title
    platform_rules.append({
        "file": "components/threshold-exporter/README.md",
        "desc": "threshold-exporter README title version",
        "pattern": r"# Threshold Exporter \(v[0-9]+\.[0-9]+\.[0-9]+\)",
        "replacement": lambda v: f"# Threshold Exporter (v{v})",
    })

    # NOTE: Chart.yaml version 已移至 exporter_rules（chart 版號 = exporter 版號）

    # CLAUDE.md project overview (only the "## 專案概覽 (vX.Y.Z)" line)
    platform_rules.append({
        "file": "CLAUDE.md",
        "desc": "CLAUDE.md project overview version",
        "pattern": r"專案概覽 \(v[0-9]+\.[0-9]+\.[0-9]+\)",
        "replacement": lambda v: f"專案概覽 (v{v})",
    })

    # da-tools README platform version reference
    platform_rules.append({
        "file": "components/da-tools/README.md",
        "desc": "da-tools README platform version ref",
        "pattern": r"平台版本（v[0-9]+\.[0-9]+\.[0-9]+\+）",
        "replacement": lambda v: f"平台版本（v{v}+）",
    })
    platform_rules.append({
        "file": "components/da-tools/README.md",
        "desc": "da-tools version strategy table (platform row)",
        "pattern": r"\| 平台文件 \| v[0-9]+\.[0-9]+\.[0-9]+",
        "replacement": lambda v: f"| 平台文件 | v{v}",
    })

    # Front matter `version: vX.Y.Z` in all docs/ .md files
    # Uses a glob scan instead of per-file rules
    platform_rules.append({
        "file": "__glob__",
        "glob_dir": "docs",
        "glob_pattern": "**/*.md",
        "desc": "front matter version: in docs/*.md",
        "pattern": r"(?<=\n)version:\s*v[0-9]+\.[0-9]+\.[0-9]+(?=\n)",
        "replacement": lambda v: f"version: v{v}",
    })

    # cli-reference da-tools container image version in header
    for f in ("docs/cli-reference.md", "docs/cli-reference.en.md"):
        platform_rules.append({
            "file": f,
            "desc": f"cli-reference container image version in {f}",
            "pattern": r"ghcr\.io/vencil/da-tools:v[0-9]+\.[0-9]+\.[0-9]+",
            "replacement": lambda v: f"ghcr.io/vencil/da-tools:v{v}",
        })

    # mkdocs.yml extra.platform_version / tools_version
    platform_rules.append({
        "file": "mkdocs.yml",
        "desc": "mkdocs.yml extra.platform_version",
        "pattern": r'platform_version:\s*"[0-9]+\.[0-9]+\.[0-9]+"',
        "replacement": lambda v: f'platform_version: "{v}"',
    })

    # README.md / README.en.md intro version
    platform_rules.append({
        "file": "README.md",
        "desc": "README.md intro version",
        "pattern": r"治理平台\*\* v[0-9]+\.[0-9]+\.[0-9]+",
        "replacement": lambda v: f"治理平台** v{v}",
    })
    platform_rules.append({
        "file": "README.en.md",
        "desc": "README.en.md intro version",
        "pattern": r"Governance Platform\*\* v[0-9]+\.[0-9]+\.[0-9]+",
        "replacement": lambda v: f"Governance Platform** v{v}",
    })

    return {
        "platform": platform_rules,
        "exporter": exporter_rules,
        "tools": tools_rules,
    }


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def read_current_versions():
    """Read current versions from source-of-truth files."""
    versions = {}

    # Exporter version from Chart.yaml (version = appVersion = exporter version)
    if CHART_YAML.exists():
        content = CHART_YAML.read_text()
        m = re.search(r'^appVersion:\s*"([0-9]+\.[0-9]+\.[0-9]+)"', content, re.MULTILINE)
        if m:
            versions["exporter"] = m.group(1)

    # Platform version from CLAUDE.md "專案概覽 (vX.Y.Z)"
    claude_md = REPO_ROOT / "CLAUDE.md"
    if claude_md.exists():
        content = claude_md.read_text()
        m = re.search(r"專案概覽 \(v([0-9]+\.[0-9]+\.[0-9]+)\)", content)
        if m:
            versions["platform"] = m.group(1)

    # da-tools version from VERSION file
    if DA_TOOLS_VERSION.exists():
        ver = DA_TOOLS_VERSION.read_text().strip()
        if re.match(r"^[0-9]+\.[0-9]+\.[0-9]+$", ver):
            versions["tools"] = ver

    return versions


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
                    "desc": f"front matter version in {rel}",
                    "pattern": rule["pattern"],
                    "replacement": rule["replacement"],
                })
        else:
            expanded.append(rule)
    return expanded


def apply_rules(rules, new_version, check_only=False):
    """Apply a set of replacement rules. Returns (changes_count, details)."""
    rules = _expand_glob_rules(rules)
    changes = []
    for rule in rules:
        fpath = REPO_ROOT / rule["file"]
        if not fpath.exists():
            changes.append(("SKIP", rule["desc"], f"file not found: {rule['file']}"))
            continue

        content = fpath.read_text()

        if rule.get("whole_file"):
            new_content = rule["replacement"](new_version)
            if content.strip() != new_content.strip():
                changes.append(("UPDATE", rule["desc"],
                                f"{content.strip()} → {new_content.strip()}"))
                if not check_only:
                    fpath.write_text(new_content)
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
            changes.append(("UPDATE", rule["desc"],
                            f"replaced {len(matches)} occurrence(s)"))
            if not check_only:
                fpath.write_text(new_content)
                os.chmod(fpath, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
        else:
            changes.append(("OK", rule["desc"], "already up to date"))

    return changes


def main():
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
    parser.add_argument("--check", action="store_true",
                        help="Check only, don't modify files (exit 1 if outdated)")
    parser.add_argument("--show-current", action="store_true",
                        help="Show current versions from source-of-truth files")

    args = parser.parse_args()

    if args.show_current:
        versions = read_current_versions()
        print("Current versions (from source-of-truth files):")
        for line, ver in sorted(versions.items()):
            print(f"  {line}: {ver}")
        return

    # --check mode: read current versions and verify all references match
    if args.check and not (args.platform or args.exporter or args.tools):
        versions = read_current_versions()
        if not versions:
            print("ERROR: Cannot read current versions from source files")
            sys.exit(1)

        all_rules = _build_rules()
        has_drift = False

        for line, ver in versions.items():
            rules = all_rules.get(line, [])
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
    if not (args.platform or args.exporter or args.tools):
        parser.print_help()
        sys.exit(1)

    all_rules = _build_rules()
    total_updates = 0

    for line, new_ver in [("platform", args.platform),
                          ("exporter", args.exporter),
                          ("tools", args.tools)]:
        if not new_ver:
            continue

        # Strip leading 'v' if provided
        new_ver = new_ver.lstrip("v")

        print(f"\n{'='*60}")
        print(f"  {line.upper()} → {new_ver}")
        print(f"{'='*60}")

        rules = all_rules.get(line, [])
        changes = apply_rules(rules, new_ver, check_only=args.check)

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
    else:
        print(f"\n✅ Done. {total_updates} update(s) applied.")


if __name__ == "__main__":
    main()
