#!/usr/bin/env python3
"""check_orphan_docs.py — 孤兒文件偵測

掃描 docs/ 下所有 .md 文件，確認每個文件至少被一個其他文件引用（連結或 include）。
自動排除工具消費的文件（tags.md、includes/*.md 等）。

用法:
  python3 scripts/tools/lint/check_orphan_docs.py              # 顯示報告
  python3 scripts/tools/lint/check_orphan_docs.py --ci          # CI 模式（exit 1 if orphans）
  python3 scripts/tools/lint/check_orphan_docs.py --verbose     # 顯示所有連結追蹤
"""

import os
import re
import sys
import argparse
from pathlib import Path
from typing import Dict, Set


class OrphanDocChecker:
    """掃描並偵測沒有被任何其他文件引用的 .md 文件。"""

    # 這些文件由自動化工具消費，不需要人工連結
    DEFAULT_IGNORE_PATTERNS = {
        "docs/tags.md",              # MkDocs Material tags plugin
        "docs/includes/",            # MkDocs snippets / 預處理器
        "docs/assets/",              # 靜態資源（JSON、HTML、CSS）
        "docs/internal/archive/",    # 已歸檔文件
    }

    # 這些文件是已知的入口點，不需要被其他文件引用
    ENTRY_POINTS = {
        "README.md",
        "README.en.md",
        "CHANGELOG.md",
        "CLAUDE.md",
        "docs/index.md",
    }

    def __init__(self, repo_root: str, verbose: bool = False):
        self.repo_root = Path(repo_root).resolve()
        self.verbose = verbose
        self.ignore_patterns: Set[str] = set(self.DEFAULT_IGNORE_PATTERNS)
        self._load_ignore_file()

    def _load_ignore_file(self):
        """Load .docorphan-ignore patterns if exists."""
        ignore_file = self.repo_root / ".docorphan-ignore"
        if ignore_file.exists():
            for line in ignore_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    self.ignore_patterns.add(line)

    def _should_ignore(self, rel_path: str) -> bool:
        """Check if a file should be excluded from orphan detection."""
        if rel_path in self.ENTRY_POINTS:
            return True
        for pattern in self.ignore_patterns:
            if pattern.endswith("/"):
                if rel_path.startswith(pattern):
                    return True
            elif rel_path == pattern:
                return True
        return False

    def _collect_md_files(self) -> Set[str]:
        """Collect all .md files under docs/ and component READMEs."""
        md_files: Set[str] = set()
        scan_dirs = ["docs", "components", "rule-packs"]
        root_mds = ["README.md", "README.en.md", "CHANGELOG.md", "CLAUDE.md"]

        for root_md in root_mds:
            p = self.repo_root / root_md
            if p.exists():
                md_files.add(root_md)

        for scan_dir in scan_dirs:
            d = self.repo_root / scan_dir
            if not d.exists():
                continue
            for md in d.rglob("*.md"):
                rel = str(md.relative_to(self.repo_root)).replace("\\", "/")
                md_files.add(rel)

        return md_files

    def _extract_links_from_file(self, filepath: Path) -> Set[str]:
        """Extract all relative .md link targets from a markdown file."""
        links: Set[str] = set()
        link_re = re.compile(r'\[([^\]]*)\]\(([^)]+)\)')

        try:
            content = filepath.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeDecodeError):
            return links

        file_dir = filepath.parent
        for match in link_re.finditer(content):
            target = match.group(2)
            # Remove anchor
            target = target.split("#")[0].strip()
            if not target:
                continue
            # Skip external links
            if target.startswith(("http://", "https://", "mailto:", "computer://", "/")):
                continue
            # Resolve relative path
            resolved = (file_dir / target).resolve()
            try:
                rel = str(resolved.relative_to(self.repo_root)).replace("\\", "/")
                links.add(rel)
            except ValueError:
                pass  # Link points outside repo

        return links

    def run(self) -> int:
        """Execute orphan check. Return 0 if clean, 1 if orphans found."""
        all_md = self._collect_md_files()

        # Build reference graph: which files are referenced by others
        referenced: Set[str] = set()
        for md_rel in all_md:
            filepath = self.repo_root / md_rel
            links = self._extract_links_from_file(filepath)
            for link in links:
                if link in all_md:
                    referenced.add(link)
            if self.verbose:
                print(f"  {md_rel}: {len(links)} links extracted")

        # Find orphans
        orphans = []
        ignored = 0
        for md_rel in sorted(all_md):
            if self._should_ignore(md_rel):
                ignored += 1
                continue
            if md_rel not in referenced:
                orphans.append(md_rel)

        # Report
        print("=" * 60)
        print("ORPHAN DOCUMENT CHECK")
        print("=" * 60)
        print(f"Total .md files scanned:  {len(all_md)}")
        print(f"Ignored (entry/tool):     {ignored}")
        print(f"Referenced by others:     {len(referenced)}")
        print(f"Orphans found:            {len(orphans)}")
        print()

        if orphans:
            print("ORPHAN FILES (not linked from any other document):")
            for o in orphans:
                print(f"  ✗ {o}")
            print()
            print("Fix: Add a link to each orphan from at least one other document,")
            print("     or add the path to .docorphan-ignore if it's consumed by tooling.")
            return 1
        else:
            print("✓ No orphan documents found.")
            return 0


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Detect orphan documents — .md files with no inbound links"
    )
    parser.add_argument("--ci", action="store_true", help="CI mode: exit 1 on orphans")
    parser.add_argument("--verbose", action="store_true", help="Show all link extraction details")
    parser.add_argument("--repo-root", default=".", help="Repository root (default: .)")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    if not (repo_root / "docs").exists():
        print(f"ERROR: Cannot find docs/ in {repo_root}", file=sys.stderr)
        return 2

    checker = OrphanDocChecker(str(repo_root), verbose=args.verbose)
    exit_code = checker.run()

    if args.ci:
        return exit_code
    return 0


if __name__ == "__main__":
    sys.exit(main())
