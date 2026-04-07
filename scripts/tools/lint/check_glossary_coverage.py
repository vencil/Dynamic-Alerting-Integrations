#!/usr/bin/env python3
"""check_glossary_coverage.py — 術語表覆蓋率檢查

掃描 docs/ 下 .md 文件中以 backtick 標記的專有名詞，確認它們出現在 glossary.md 中。
重點偵測「文件反覆使用但 glossary 未收錄」的術語，降低新讀者理解門檻。

用法:
  python3 scripts/tools/lint/check_glossary_coverage.py              # 顯示報告
  python3 scripts/tools/lint/check_glossary_coverage.py --ci          # CI 模式
  python3 scripts/tools/lint/check_glossary_coverage.py --verbose     # 詳細顯示
"""

import os
import re
import sys
import argparse
from pathlib import Path
from collections import Counter
from typing import Set, Dict


class GlossaryCoverageChecker:
    """掃描文件中的術語，比對 glossary.md 覆蓋率。"""

    # 明確不是術語的 backtick 內容模式
    SKIP_PATTERNS = [
        re.compile(r'^[\d\.\-\+\*\/\=\<\>\!\&\|\~\^\%]+$'),  # 純數字/運算符
        re.compile(r'^[\w\-]+\.(py|go|md|yaml|yml|json|jsx|html|css|sh|txt|toml|cfg|conf|env|lock|mermaid|svg)$'),  # 檔名
        re.compile(r'^(https?://|mailto:|/|\./)'),  # URL/路徑
        re.compile(r'^[\w\-]+/[\w\-/]+'),  # 路徑 (a/b/c)
        re.compile(r'^--[\w\-]'),  # CLI flag
        re.compile(r'^\$'),  # 變數
        re.compile(r'^[\w_]+\('),  # 函式呼叫
        re.compile(r'^(true|false|null|nil|none|yes|no)$', re.IGNORECASE),  # 布林/null
        re.compile(r'^\d'),  # 數字開頭
        re.compile(r'^[\w\-]+:[\w\-]'),  # key:value
        re.compile(r'^[a-z][a-z0-9_\-]*$'),  # 純小寫 kebab/snake_case（通常是 code）
        re.compile(r'^\{'),  # template/placeholder
        re.compile(r'^<'),   # XML/HTML tag
        re.compile(r'^"'),   # quoted value
        re.compile(r'^docker\s|^kubectl\s|^make\s|^git\s|^pip\s|^npm\s'),  # shell 命令
        re.compile(r'^ghcr\.io/'),  # container image ref
        re.compile(r'^v\d+\.\d+'),  # 版號
        re.compile(r'^[\w\-]+\s[\w\-]+\s'),  # 多字 command（3+ words = code snippet）
        re.compile(r'^\w+\.[\w\.]+\w$'),  # 帶 dots 的 identifier (e.g. config.yaml.j2)
        re.compile(r'^_\w+'),  # underscore-prefixed config field (e.g. _routing, _silent_mode)
        re.compile(r'^\w+\s*:\s'),  # YAML key-value inline (e.g. "optional: true")
        re.compile(r'^(max|min|sum|avg|count|rate|by)\s*[\(\{]', re.IGNORECASE),  # PromQL func
        re.compile(r'[\w\-]+\.(md|en\.md)$'),  # 文件名引用 (e.g. architecture-and-design.en.md)
        re.compile(r'[\w\-]+/$'),  # 目錄引用 (e.g. conf.d/, rule-packs/)
        re.compile(r'^[A-Z][a-zA-Z]+(High|Low|Down|Up|Full|Slow|Error|Fail|Critical|Warning)\w*$'),  # Alert rule name
        re.compile(r'^[A-Z][a-zA-Z]+Ref$'),  # Kubernetes ref (e.g. secretKeyRef)
    ]

    # 最低出現次數（只報告在多個文件中反覆出現的術語）
    MIN_FILE_COUNT = 3

    def __init__(self, repo_root: str, verbose: bool = False):
        self.repo_root = Path(repo_root).resolve()
        self.verbose = verbose
        self.glossary_terms: Set[str] = set()

    def _load_glossary(self):
        """Parse glossary.md，提取所有收錄的術語（bold 行的主詞）。"""
        glossary_path = self.repo_root / "docs" / "glossary.md"
        if not glossary_path.exists():
            print("WARNING: docs/glossary.md not found", file=sys.stderr)
            return

        content = glossary_path.read_text(encoding="utf-8", errors="replace")
        # 匹配 **Term** 或 **Term (Abbrev)** 格式
        term_re = re.compile(r'^\*\*(.+?)\*\*', re.MULTILINE)
        for match in term_re.finditer(content):
            raw = match.group(1).strip()
            # 提取主詞和括號內的縮寫
            self.glossary_terms.add(raw.lower())
            # 也加入括號內的縮寫
            paren_match = re.search(r'\(([^)]+)\)', raw)
            if paren_match:
                self.glossary_terms.add(paren_match.group(1).strip().lower())
            # 也加入括號外的主詞
            main_term = re.sub(r'\s*\([^)]*\)\s*', '', raw).strip()
            if main_term:
                self.glossary_terms.add(main_term.lower())

    def _is_term_candidate(self, text: str) -> bool:
        """判斷 backtick 內容是否可能是術語（而非 code snippet）。"""
        text = text.strip()
        if not text or len(text) > 60 or len(text) < 2:
            return False
        for pat in self.SKIP_PATTERNS:
            if pat.search(text):
                return False
        return True

    def _is_in_glossary(self, term: str) -> bool:
        """Check if term (case-insensitive) is covered by glossary."""
        return term.lower() in self.glossary_terms

    def run(self) -> int:
        """Execute glossary coverage check."""
        self._load_glossary()
        if not self.glossary_terms:
            print("WARNING: No terms found in glossary.md")
            return 0

        backtick_re = re.compile(r'(?<!`)`([^`\n]+)`(?!`)')
        # term -> set of files it appears in
        term_files: Dict[str, Set[str]] = {}

        # Scan docs/
        docs_dir = self.repo_root / "docs"
        skip_dirs = {"internal", "includes", "assets", "schemas"}

        for md_file in docs_dir.rglob("*.md"):
            # Skip internal/includes/assets
            rel_parts = md_file.relative_to(docs_dir).parts
            if rel_parts and rel_parts[0] in skip_dirs:
                continue
            if md_file.name == "glossary.md" or md_file.name == "glossary.en.md":
                continue

            try:
                content = md_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            rel_path = str(md_file.relative_to(self.repo_root))
            in_code_block = False
            for line in content.splitlines():
                stripped = line.strip()
                if stripped.startswith("```"):
                    in_code_block = not in_code_block
                    continue
                if in_code_block:
                    continue
                for match in backtick_re.finditer(line):
                    term = match.group(1).strip()
                    if self._is_term_candidate(term):
                        if term not in term_files:
                            term_files[term] = set()
                        term_files[term].add(rel_path)

        # Find uncovered terms appearing in multiple files
        uncovered = {}
        for term, files in term_files.items():
            if len(files) >= self.MIN_FILE_COUNT and not self._is_in_glossary(term):
                uncovered[term] = files

        # Sort by file count descending
        sorted_uncovered = sorted(uncovered.items(), key=lambda x: -len(x[1]))

        # Report
        print("=" * 60)
        print("GLOSSARY COVERAGE CHECK")
        print("=" * 60)
        print(f"Glossary terms:       {len(self.glossary_terms)}")
        print(f"Unique terms in docs: {len(term_files)}")
        print(f"Uncovered (≥{self.MIN_FILE_COUNT} files): {len(sorted_uncovered)}")
        print()

        if sorted_uncovered:
            print(f"UNCOVERED TERMS (appear in ≥{self.MIN_FILE_COUNT} files but not in glossary):")
            for term, files in sorted_uncovered[:30]:
                print(f"  ✗ `{term}` — {len(files)} files")
                if self.verbose:
                    for f in sorted(files)[:5]:
                        print(f"      {f}")
            if len(sorted_uncovered) > 30:
                print(f"  ... and {len(sorted_uncovered) - 30} more")
            print()
            print("Fix: Add missing terms to docs/glossary.md (or check if they are code, not terms).")
            return 1
        else:
            print("✓ All frequently-used terms are covered in glossary.")
            return 0


def main():
    parser = argparse.ArgumentParser(
        description="Check glossary coverage for frequently-used backtick terms"
    )
    parser.add_argument("--ci", action="store_true", help="CI mode: exit 1 on uncovered terms")
    parser.add_argument("--verbose", action="store_true", help="Show file details per term")
    parser.add_argument("--repo-root", default=".", help="Repository root (default: .)")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    if not (repo_root / "docs").exists():
        print(f"ERROR: Cannot find docs/ in {repo_root}", file=sys.stderr)
        return 2

    checker = GlossaryCoverageChecker(str(repo_root), verbose=args.verbose)
    exit_code = checker.run()

    if args.ci:
        return exit_code
    return 0


if __name__ == "__main__":
    sys.exit(main())
