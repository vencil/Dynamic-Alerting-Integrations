#!/usr/bin/env python3
"""bump_docs.py — 版號一致性管理工具

掃描 repo 中的文件、Chart.yaml、VERSION 檔案，批次更新版號引用。
三條版號線獨立管理：--platform / --exporter / --tools。

用法:
  # 更新 da-tools 版號 (所有 image tag + VERSION)
  python3 scripts/tools/bump_docs.py --tools 0.2.0

  # 更新平台文件版號
  python3 scripts/tools/bump_docs.py --platform 0.10.0

  # 更新 exporter 版號 (appVersion + image tag)
  python3 scripts/tools/bump_docs.py --exporter 0.6.0

  # 只檢查不修改 (CI lint 用)
  python3 scripts/tools/bump_docs.py --check

  # 組合使用
  python3 scripts/tools/bump_docs.py --platform 0.10.0 --tools 0.2.0 --exporter 0.6.0
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
        "docs/byo-prometheus-integration.md",
        "docs/migration-guide.md",
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
    exporter_rules = [
        {
            "file": "components/threshold-exporter/Chart.yaml",
            "desc": "Chart.yaml appVersion",
            "pattern": r'^appVersion:\s*"[0-9]+\.[0-9]+\.[0-9]+"',
            "replacement": lambda v: f'appVersion: "{v}"',
        },
        {
            "file": "docs/migration-guide.md",
            "desc": "Helm --set image.tag in migration guide",
            "pattern": r"--set image\.tag=[0-9]+\.[0-9]+\.[0-9]+",
            "replacement": lambda v: f"--set image.tag={v}",
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
    platform_doc_files = [
        ("docs/architecture-and-design.md", r"v[0-9]+\.[0-9]+\.[0-9]+", "header+footer"),
        ("docs/architecture-and-design.en.md", r"v[0-9]+\.[0-9]+\.[0-9]+", "header+footer"),
        ("components/threshold-exporter/README.md", r"v[0-9]+\.[0-9]+\.[0-9]+", "header"),
    ]
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

    # BYOP guide version header
    platform_rules.append({
        "file": "docs/byo-prometheus-integration.md",
        "desc": "BYOP guide version",
        "pattern": r"\*\*版本\*\*：v[0-9]+\.[0-9]+\.[0-9]+",
        "replacement": lambda v: f"**版本**：v{v}",
    })

    # threshold-exporter README title
    platform_rules.append({
        "file": "components/threshold-exporter/README.md",
        "desc": "threshold-exporter README title version",
        "pattern": r"# Threshold Exporter \(v[0-9]+\.[0-9]+\.[0-9]+\)",
        "replacement": lambda v: f"# Threshold Exporter (v{v})",
    })

    # Chart.yaml version (chart structure version)
    platform_rules.append({
        "file": "components/threshold-exporter/Chart.yaml",
        "desc": "Chart.yaml version (chart structure)",
        "pattern": r"^version:\s*[0-9]+\.[0-9]+\.[0-9]+",
        "replacement": lambda v: f"version: {v}",
    })

    # CLAUDE.md project overview
    platform_rules.append({
        "file": "CLAUDE.md",
        "desc": "CLAUDE.md project overview version",
        "pattern": r"v[0-9]+\.[0-9]+\.[0-9]+\)",
        "replacement": lambda v: f"v{v})",
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

    # Platform version from Chart.yaml 'version' field
    if CHART_YAML.exists():
        content = CHART_YAML.read_text()
        m = re.search(r"^version:\s*([0-9]+\.[0-9]+\.[0-9]+)", content, re.MULTILINE)
        if m:
            versions["platform"] = m.group(1)
        m = re.search(r'^appVersion:\s*"([0-9]+\.[0-9]+\.[0-9]+)"', content, re.MULTILINE)
        if m:
            versions["exporter"] = m.group(1)

    # da-tools version from VERSION file
    if DA_TOOLS_VERSION.exists():
        ver = DA_TOOLS_VERSION.read_text().strip()
        if re.match(r"^[0-9]+\.[0-9]+\.[0-9]+$", ver):
            versions["tools"] = ver

    return versions


def apply_rules(rules, new_version, check_only=False):
    """Apply a set of replacement rules. Returns (changes_count, details)."""
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
