#!/usr/bin/env python3
"""migrate_ssot_language.py — SSOT 語言切換遷移工具

將文件對從「中文主 (.md) + 英文輔 (.en.md)」反轉為
「英文主 (.md) + 中文輔 (.zh.md)」，同步更新 frontmatter lang 標籤
和雙語導航連結。

設計原則：
- 全量操作需在同一個 commit 中完成（避免中間態破壞 MkDocs build）
- --dry-run 模式只顯示將要執行的操作，不修改任何檔案
- --directory 可限定只遷移某個目錄（試點用）
- 遷移腳本不更新 mkdocs.yml — 那需要手動 + 全量遷移時一併處理

用法:
  # 全量掃描（dry-run）
  python3 scripts/tools/dx/migrate_ssot_language.py --dry-run

  # 試點遷移 getting-started/ (dry-run)
  python3 scripts/tools/dx/migrate_ssot_language.py --dry-run --directory docs/getting-started

  # 執行遷移（需搭配 git mv）
  python3 scripts/tools/dx/migrate_ssot_language.py --directory docs/getting-started --execute

  # CI 驗證：檢查是否有未遷移的 .en.md 或已遷移但 frontmatter 不一致的檔案
  python3 scripts/tools/dx/migrate_ssot_language.py --check

參考：docs/internal/ssot-language-evaluation.md §4 Migration Path
"""

import argparse
import re
import sys
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent

# Directories to scan for bilingual pairs
SCAN_DIRS = [
    REPO_ROOT / "docs",
    REPO_ROOT / "rule-packs",
]

# Root-level files with bilingual pairs
SCAN_ROOT_FILES = [
    REPO_ROOT / "README.md",
]

# Directories exempt from bilingual requirements
EXEMPT_DIRS = {
    "docs/internal/",
    "docs/includes/",
}

# Files that should not be renamed (special handling)
SKIP_FILES = {
    "CHANGELOG.md",
    "CHANGELOG.en.md",
}


class MigrationAction(NamedTuple):
    """A single rename action in the migration plan."""
    source: Path       # Current file path
    target: Path       # Target file path after migration
    action: str        # 'rename' | 'update_frontmatter' | 'update_nav_link'
    description: str   # Human-readable description


class MigrationPlan:
    """Collects and executes migration actions."""

    def __init__(self):
        self.actions: List[MigrationAction] = []
        self.errors: List[str] = []
        self.warnings: List[str] = []

    def add(self, action: MigrationAction):
        self.actions.append(action)

    def add_error(self, msg: str):
        self.errors.append(msg)

    def add_warning(self, msg: str):
        self.warnings.append(msg)

    @property
    def rename_count(self) -> int:
        return sum(1 for a in self.actions if a.action == "rename")


def discover_en_md_pairs(
    directory: Optional[Path] = None,
) -> List[tuple[Path, Path]]:
    """Find all .md / .en.md pairs (legacy pattern).

    Returns list of (zh_file, en_file) tuples.
    """
    pairs = []

    scan_dirs = SCAN_DIRS if directory is None else [REPO_ROOT / directory]
    scan_root = SCAN_ROOT_FILES if directory is None else []

    for scan_dir in scan_dirs:
        if not scan_dir.is_dir():
            continue
        for en_file in sorted(scan_dir.rglob("*.en.md")):
            # Skip exempt dirs
            rel = str(en_file.relative_to(REPO_ROOT))
            if any(rel.startswith(d) for d in EXEMPT_DIRS):
                continue
            # Skip special files
            if en_file.name in SKIP_FILES:
                continue

            zh_file = en_file.parent / en_file.name.replace(".en.md", ".md")
            if zh_file.is_file():
                pairs.append((zh_file, en_file))
            else:
                # Orphan .en.md — no zh counterpart
                pass

    for root_file in scan_root:
        if root_file.name in SKIP_FILES:
            continue
        if root_file.is_file():
            en_name = root_file.stem + ".en" + root_file.suffix
            en_file = root_file.parent / en_name
            if en_file.is_file():
                pairs.append((root_file, en_file))

    return pairs


def discover_zh_md_pairs(
    directory: Optional[Path] = None,
) -> List[tuple[Path, Path]]:
    """Find all .md / .zh.md pairs (new pattern).

    Returns list of (en_file, zh_file) tuples.
    """
    pairs = []

    scan_dirs = SCAN_DIRS if directory is None else [REPO_ROOT / directory]

    for scan_dir in scan_dirs:
        if not scan_dir.is_dir():
            continue
        for zh_file in sorted(scan_dir.rglob("*.zh.md")):
            rel = str(zh_file.relative_to(REPO_ROOT))
            if any(rel.startswith(d) for d in EXEMPT_DIRS):
                continue

            en_file = zh_file.parent / zh_file.name.replace(".zh.md", ".md")
            if en_file.is_file():
                pairs.append((en_file, zh_file))

    return pairs


def update_frontmatter_lang(content: str, new_lang: str) -> str:
    """Update the 'lang:' field in YAML frontmatter."""
    lines = content.split("\n")
    if not lines or lines[0].strip() != "---":
        return content

    in_fm = True
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            break
        if re.match(r"^lang:\s*", lines[i]):
            lines[i] = f"lang: {new_lang}"
            break
    else:
        # No closing --- found, return unchanged
        return content

    return "\n".join(lines)


def update_nav_link(content: str, old_suffix: str, new_suffix: str, partner_name: str) -> str:
    """Update the bilingual navigation link in the first 20 lines.

    Looks for patterns like [English](foo.en.md) or [中文](foo.md)
    and updates the link target to the new filename.
    """
    lines = content.split("\n")
    limit = min(20, len(lines))

    for i in range(limit):
        # Match markdown links pointing to the old partner file
        if old_suffix in lines[i]:
            lines[i] = lines[i].replace(old_suffix, new_suffix)

    return "\n".join(lines)


def build_migration_plan(
    directory: Optional[Path] = None,
) -> MigrationPlan:
    """Build a migration plan for converting .en.md → .zh.md pattern.

    The rename is a two-step process per pair:
    1. foo.md (ZH) → foo.zh.md
    2. foo.en.md (EN) → foo.md

    This must be done carefully to avoid conflicts (both steps
    touch 'foo.md').
    """
    plan = MigrationPlan()
    pairs = discover_en_md_pairs(directory)

    if not pairs:
        plan.add_warning("No .en.md pairs found to migrate.")
        return plan

    for zh_file, en_file in pairs:
        rel_zh = zh_file.relative_to(REPO_ROOT)
        rel_en = en_file.relative_to(REPO_ROOT)
        stem = zh_file.stem  # e.g., 'for-platform-engineers'

        # Target paths
        new_zh = zh_file.parent / f"{stem}.zh.md"
        new_en = zh_file  # foo.md stays as foo.md but with English content

        # Check for conflicts
        if new_zh.exists():
            plan.add_error(f"Target already exists: {new_zh.relative_to(REPO_ROOT)}")
            continue

        # Step 1: foo.md (ZH) → foo.zh.md
        plan.add(MigrationAction(
            source=zh_file,
            target=new_zh,
            action="rename",
            description=f"{rel_zh} → {new_zh.relative_to(REPO_ROOT)}",
        ))

        # Step 2: foo.en.md (EN) → foo.md
        plan.add(MigrationAction(
            source=en_file,
            target=new_en,
            action="rename",
            description=f"{rel_en} → {new_en.relative_to(REPO_ROOT)}",
        ))

        # Step 3: Update frontmatter in new foo.zh.md (lang: zh → zh stays, but add note)
        plan.add(MigrationAction(
            source=new_zh,
            target=new_zh,
            action="update_frontmatter",
            description=f"{new_zh.relative_to(REPO_ROOT)}: frontmatter lang → zh",
        ))

        # Step 4: Update frontmatter in new foo.md (lang: en)
        plan.add(MigrationAction(
            source=new_en,
            target=new_en,
            action="update_frontmatter",
            description=f"{new_en.relative_to(REPO_ROOT)}: frontmatter lang → en",
        ))

        # Step 5: Update nav links in both files
        plan.add(MigrationAction(
            source=new_zh,
            target=new_zh,
            action="update_nav_link",
            description=f"{new_zh.relative_to(REPO_ROOT)}: nav link → {stem}.md",
        ))

        plan.add(MigrationAction(
            source=new_en,
            target=new_en,
            action="update_nav_link",
            description=f"{new_en.relative_to(REPO_ROOT)}: nav link → {stem}.zh.md",
        ))

    return plan


def execute_plan(plan: MigrationPlan, use_git: bool = False) -> bool:
    """Execute migration plan.

    When use_git=True, uses subprocess to call git mv (recommended).
    When use_git=False, uses Path.rename (for testing).

    Returns True if all actions succeeded.
    """
    import subprocess

    success = True
    rename_pairs = []

    # Collect rename pairs (must execute in correct order)
    for action in plan.actions:
        if action.action == "rename":
            rename_pairs.append((action.source, action.target))

    # Execute renames: first all .md → .zh.md, then all .en.md → .md
    # This avoids the conflict where .md is both source and target
    phase1 = [(s, t) for s, t in rename_pairs if ".zh.md" in t.name]
    phase2 = [(s, t) for s, t in rename_pairs if ".zh.md" not in t.name]

    for source, target in phase1 + phase2:
        rel_s = source.relative_to(REPO_ROOT)
        rel_t = target.relative_to(REPO_ROOT)
        if use_git:
            result = subprocess.run(
                ["git", "mv", str(rel_s), str(rel_t)],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                print(f"  ✗ git mv failed: {rel_s} → {rel_t}")
                print(f"    {result.stderr.strip()}")
                success = False
            else:
                print(f"  ✓ git mv {rel_s} → {rel_t}")
        else:
            try:
                source.rename(target)
                print(f"  ✓ mv {rel_s} → {rel_t}")
            except OSError as e:
                print(f"  ✗ mv failed: {rel_s} → {rel_t}: {e}")
                success = False

    # Execute frontmatter updates
    for action in plan.actions:
        if action.action == "update_frontmatter":
            filepath = action.target
            if not filepath.exists():
                print(f"  ✗ File not found for frontmatter update: {filepath}")
                success = False
                continue

            content = filepath.read_text(encoding="utf-8")
            new_lang = "zh" if ".zh.md" in filepath.name else "en"
            updated = update_frontmatter_lang(content, new_lang)
            if updated != content:
                filepath.write_text(updated, encoding="utf-8")
                print(f"  ✓ frontmatter lang → {new_lang}: {filepath.relative_to(REPO_ROOT)}")
            else:
                print(f"  · frontmatter unchanged: {filepath.relative_to(REPO_ROOT)}")

    # Execute nav link updates
    for action in plan.actions:
        if action.action == "update_nav_link":
            filepath = action.target
            if not filepath.exists():
                print(f"  ✗ File not found for nav update: {filepath}")
                success = False
                continue

            content = filepath.read_text(encoding="utf-8")
            if ".zh.md" in filepath.name:
                # In zh file: link should point to English (.md) partner
                stem = filepath.name.replace(".zh.md", "")
                old_link = f"{stem}.en.md"
                new_link = f"{stem}.md"
            else:
                # In en file (.md): link should point to Chinese (.zh.md) partner
                stem = filepath.stem
                old_link = f"{stem}.md"  # Was pointing to old zh file
                # But wait — the old en file pointed to stem.md
                # After rename, it should point to stem.zh.md
                # The old .en.md file had a link like [中文](stem.md)
                # Now it should be [中文](stem.zh.md)
                new_link = f"{stem}.zh.md"

            updated = content.replace(old_link, new_link) if old_link in content else content
            if updated != content:
                filepath.write_text(updated, encoding="utf-8")
                print(f"  ✓ nav link updated: {filepath.relative_to(REPO_ROOT)}")
            else:
                print(f"  · nav link unchanged: {filepath.relative_to(REPO_ROOT)}")

    return success


def check_consistency(directory: Optional[Path] = None) -> int:
    """Check for migration consistency issues.

    Returns number of issues found.
    """
    issues = 0

    # Find legacy pairs (.en.md)
    legacy_pairs = discover_en_md_pairs(directory)
    # Find new pairs (.zh.md)
    new_pairs = discover_zh_md_pairs(directory)

    print(f"Legacy pattern (.en.md): {len(legacy_pairs)} pairs")
    print(f"New pattern (.zh.md):    {len(new_pairs)} pairs")

    if legacy_pairs and new_pairs:
        print("\n⚠ Mixed patterns detected — migration is incomplete.")
        issues += 1

        # Report which dirs are migrated vs not
        legacy_dirs = {str(p[0].parent.relative_to(REPO_ROOT)) for p in legacy_pairs}
        new_dirs = {str(p[0].parent.relative_to(REPO_ROOT)) for p in new_pairs}
        print(f"\n  Legacy dirs: {', '.join(sorted(legacy_dirs))}")
        print(f"  New dirs:    {', '.join(sorted(new_dirs))}")

    # Check frontmatter consistency
    for en_file, zh_file in new_pairs:
        en_content = en_file.read_text(encoding="utf-8")
        zh_content = zh_file.read_text(encoding="utf-8")

        if "lang: zh" in en_content.split("---")[1] if "---" in en_content else "":
            print(f"  ✗ {en_file.relative_to(REPO_ROOT)}: frontmatter says zh but file is .md (should be en)")
            issues += 1
        if "lang: en" in zh_content.split("---")[1] if "---" in zh_content else "":
            print(f"  ✗ {zh_file.relative_to(REPO_ROOT)}: frontmatter says en but file is .zh.md (should be zh)")
            issues += 1

    return issues


def print_plan(plan: MigrationPlan):
    """Pretty-print a migration plan."""
    print(f"Migration plan: {plan.rename_count // 2} file pairs "
          f"({plan.rename_count} renames + "
          f"{sum(1 for a in plan.actions if a.action != 'rename')} updates)")
    print()

    if plan.errors:
        print("Blocking issues:", file=sys.stderr)
        for err in plan.errors:
            print(f"  ✗ {err}", file=sys.stderr)
        print()

    if plan.warnings:
        print("Warnings:")
        for warn in plan.warnings:
            print(f"  ⚠ {warn}")
        print()

    # Group by pair
    current_pair = None
    for action in plan.actions:
        pair_key = action.source.stem.replace(".en", "").replace(".zh", "")
        if pair_key != current_pair:
            current_pair = pair_key
            print(f"  [{pair_key}]")

        icon = {"rename": "→", "update_frontmatter": "📝", "update_nav_link": "🔗"}
        print(f"    {icon.get(action.action, '?')} {action.description}")

    print()


def main():
    parser = argparse.ArgumentParser(
        description="SSOT 語言切換遷移工具 — .en.md → .zh.md 反轉")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show migration plan without executing")
    parser.add_argument("--execute", action="store_true",
                        help="Execute the migration (use with --directory for pilot)")
    parser.add_argument("--check", action="store_true",
                        help="Check migration consistency (CI mode)")
    parser.add_argument("--directory", type=str, default=None,
                        help="Limit scope to a directory (relative to repo root)")
    parser.add_argument("--git", action="store_true",
                        help="Use git mv for renames (preserves history)")
    parser.add_argument("--ci", action="store_true",
                        help="Exit 1 on any inconsistency (for CI)")
    args = parser.parse_args()

    if args.check:
        issues = check_consistency(
            Path(args.directory) if args.directory else None,
        )
        if issues and args.ci:
            sys.exit(1)
        sys.exit(0)

    directory = Path(args.directory) if args.directory else None
    plan = build_migration_plan(directory)

    if args.dry_run or not args.execute:
        print("=" * 60)
        print("SSOT Language Migration — Dry Run")
        print("=" * 60)
        print()
        print_plan(plan)

        if plan.errors:
            print("⛔ Plan has errors — fix before executing.")
            sys.exit(1)

        print("To execute: add --execute flag")
        if not args.git:
            print("Tip: add --git to use git mv (preserves history)")
        sys.exit(0)

    if args.execute:
        if plan.errors:
            print("⛔ Plan has errors — cannot execute.")
            for err in plan.errors:
                print(f"  ✗ {err}")
            sys.exit(1)

        print(f"Executing migration: {plan.rename_count // 2} file pairs...")
        success = execute_plan(plan, use_git=args.git)
        if not success:
            print("\n⛔ Some actions failed. Check output above.")
            sys.exit(1)
        print(f"\n✓ Migration complete. Run lint to verify:")
        print(f"  pre-commit run bilingual-structure-check --all-files")
        print(f"  pre-commit run bilingual-content-check --all-files")
        sys.exit(0)

    # Default: show plan
    print_plan(plan)


if __name__ == "__main__":
    main()
