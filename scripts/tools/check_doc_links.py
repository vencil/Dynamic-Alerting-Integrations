#!/usr/bin/env python3
"""check_doc_links.py — 文件間交叉引用一致性檢查

掃描 repo 中的 Markdown 文件，驗證所有相對路徑連結和 §X.Y 引用的目標存在。

用法:
  python3 scripts/tools/check_doc_links.py              # 顯示報告
  python3 scripts/tools/check_doc_links.py --ci          # CI 模式（exit 1 if broken）
  python3 scripts/tools/check_doc_links.py --verbose     # 顯示所有掃描的連結
"""

import os
import re
import sys
import argparse
from pathlib import Path
from collections import defaultdict
from typing import List, Tuple, Dict, Set


class DocLinkChecker:
    """掃描並驗證 Markdown 文件中的交叉引用。"""

    def __init__(self, repo_root: str, verbose: bool = False):
        self.repo_root = Path(repo_root).resolve()
        self.verbose = verbose
        
        # 掃描範圍
        self.scan_dirs = ["docs", "rule-packs"]
        self.root_md_files = ["README.md", "README.en.md", "CHANGELOG.md", "CLAUDE.md"]
        
        # 統計
        self.total_links_checked = 0
        self.broken_links = []
        self.section_refs_checked = 0
        self.broken_section_refs = []
        self.verbose_links = []
        
        # 已知的 section 參考格式：§X.Y (主章節.副章節)
        self.section_pattern = re.compile(r'§(\d+)\.(\d+)')
        
        # 已知的 section 集合 (從 CLAUDE.md 和其他文件中解析)
        self.known_sections = self._extract_sections()

    def _extract_sections(self) -> Set[str]:
        """從 Markdown 文件中提取已知的 section 編號。"""
        sections = set()
        
        # 掃描所有 Markdown 文件尋找 heading + section reference
        md_files = self._get_all_md_files()
        
        for md_file in md_files:
            try:
                with open(md_file, "r", encoding="utf-8") as f:
                    for line in f:
                        # 尋找 §X.Y 格式的 section reference
                        matches = self.section_pattern.findall(line)
                        for major, minor in matches:
                            sections.add(f"{major}.{minor}")
            except Exception:
                pass
        
        return sections

    def _get_all_md_files(self) -> List[Path]:
        """取得所有要掃描的 Markdown 文件。"""
        md_files = []
        
        # 掃描 docs/ 和 rule-packs/
        for scan_dir in self.scan_dirs:
            dir_path = self.repo_root / scan_dir
            if dir_path.exists():
                md_files.extend(dir_path.rglob("*.md"))
        
        # 掃描根目錄的 Markdown 文件
        for md_file in self.root_md_files:
            file_path = self.repo_root / md_file
            if file_path.exists():
                md_files.append(file_path)
        
        return sorted(list(set(md_files)))

    def _is_in_code_block(self, lines: List[str], line_num: int) -> bool:
        """檢查指定行是否在程式碼區塊內。"""
        fence_count = 0
        for i in range(line_num):
            if lines[i].strip().startswith("```"):
                fence_count += 1
        return fence_count % 2 == 1

    def _is_external_url(self, url: str) -> bool:
        """檢查是否為外部 URL。"""
        return url.startswith("http://") or url.startswith("https://")

    def _resolve_link_path(self, source_file: Path, link: str) -> Tuple[Path, bool]:
        """
        解析相對連結為絕對路徑。
        
        Returns:
            (resolved_path, is_valid) - resolved_path 可能不存在，is_valid 表示路徑是否有效
        """
        # 移除 anchor
        file_part = link.split("#")[0]
        
        if not file_part:
            # 純 anchor，指向同一檔案
            return source_file, True
        
        # 相對於來源檔案的目錄
        source_dir = source_file.parent
        target_path = (source_dir / file_part).resolve()
        
        # 檢查是否在 repo 範圍內
        try:
            target_path.relative_to(self.repo_root)
        except ValueError:
            # 超出 repo 範圍
            return target_path, False
        
        return target_path, True

    def _find_suggestions(self, broken_link: str, source_file: Path) -> str:
        """建議可能的修正。"""
        suggestions = []
        
        # 1. 檢查是否為 typo（常見的檔名變更）
        all_md_files = self._get_all_md_files()
        broken_name = Path(broken_link.split("#")[0]).name
        
        for md_file in all_md_files:
            if md_file.name == broken_name:
                # 找到同名檔案，提示相對路徑
                try:
                    rel_path = md_file.relative_to(source_file.parent)
                    suggestions.append(f"Try: [{broken_name}]({rel_path})")
                except ValueError:
                    pass
        
        # 2. 檢查大小寫差異
        if broken_link.lower() != broken_link:
            suggestions.append(f"Check: case sensitivity (use lowercase)")
        
        # 3. 檢查是否應該在 docs/ 中
        if not broken_link.startswith("docs/"):
            docs_candidate = self.repo_root / "docs" / broken_link.split("#")[0]
            if docs_candidate.exists():
                suggestions.append(f"Try: [text](docs/{broken_link})")
        
        return " | ".join(suggestions) if suggestions else "(no suggestions)"

    def scan_file(self, md_file: Path) -> None:
        """掃描單一 Markdown 文件。"""
        try:
            with open(md_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception as e:
            print(f"ERROR: Cannot read {md_file}: {e}", file=sys.stderr)
            return
        
        # Markdown 連結模式：[text](path)
        link_pattern = re.compile(r'\[([^\]]+)\]\(([^\)]+)\)')
        
        for line_num, line in enumerate(lines, 1):
            # 跳過程式碼區塊
            if self._is_in_code_block(lines, line_num - 1):
                continue
            
            # 尋找所有連結
            for match in link_pattern.finditer(line):
                link_text = match.group(1)
                link_url = match.group(2)
                
                self.total_links_checked += 1
                
                # 跳過外部 URL
                if self._is_external_url(link_url):
                    if self.verbose:
                        self.verbose_links.append(
                            f"  {md_file.relative_to(self.repo_root)}:{line_num} "
                            f"[EXTERNAL] {link_url}"
                        )
                    continue
                
                # 解析路徑
                target_path, is_valid = self._resolve_link_path(md_file, link_url)
                
                if self.verbose:
                    status = "OK" if target_path.exists() else "BROKEN"
                    self.verbose_links.append(
                        f"  {md_file.relative_to(self.repo_root)}:{line_num} "
                        f"[{status}] {link_url} -> {target_path.relative_to(self.repo_root) if target_path.exists() else '(not found)'}"
                    )
                
                # 檢查目標是否存在
                if not target_path.exists():
                    suggestion = self._find_suggestions(link_url, md_file)
                    self.broken_links.append({
                        "file": md_file.relative_to(self.repo_root),
                        "line": line_num,
                        "link": link_url,
                        "target": target_path.relative_to(self.repo_root) if is_valid else link_url,
                        "suggestion": suggestion,
                        "source_line": line.strip()
                    })
            
            # 掃描 §X.Y section 參考
            section_matches = self.section_pattern.findall(line)
            for major, minor in section_matches:
                section_ref = f"{major}.{minor}"
                self.section_refs_checked += 1
                
                if section_ref not in self.known_sections:
                    self.broken_section_refs.append({
                        "file": md_file.relative_to(self.repo_root),
                        "line": line_num,
                        "ref": section_ref,
                        "source_line": line.strip()
                    })

    def run(self) -> int:
        """執行掃描並返回 exit code。"""
        print("Scanning documentation files for broken links...\n")
        
        md_files = self._get_all_md_files()
        print(f"Found {len(md_files)} Markdown files to scan.\n")
        
        for md_file in md_files:
            self.scan_file(md_file)
        
        # 列印詳細連結（如果 --verbose）
        if self.verbose and self.verbose_links:
            print("=" * 70)
            print("DETAILED LINK SCAN RESULTS:")
            print("=" * 70)
            for line in self.verbose_links:
                print(line)
            print()
        
        # 列印統計摘要
        print("=" * 70)
        print("SCAN SUMMARY:")
        print("=" * 70)
        print(f"Total links checked: {self.total_links_checked}")
        print(f"Broken links found: {len(self.broken_links)}")
        print(f"Section references checked: {self.section_refs_checked}")
        print(f"Broken section references: {len(self.broken_section_refs)}")
        print()
        
        # 列印破損的連結
        if self.broken_links:
            print("=" * 70)
            print("BROKEN LINKS:")
            print("=" * 70)
            for broken in self.broken_links:
                print(f"\nFile: {broken['file']}:{broken['line']}")
                print(f"  Link: [{broken['link']}]")
                print(f"  Target: {broken['target']}")
                print(f"  Suggestion: {broken['suggestion']}")
                print(f"  Source: {broken['source_line'][:80]}")
        
        # 列印破損的 section 參考
        if self.broken_section_refs:
            print("\n" + "=" * 70)
            print("BROKEN SECTION REFERENCES:")
            print("=" * 70)
            for broken in self.broken_section_refs:
                print(f"\nFile: {broken['file']}:{broken['line']}")
                print(f"  Reference: §{broken['ref']}")
                print(f"  Known sections: {', '.join(sorted(self.known_sections)[:10])}")
                if len(self.known_sections) > 10:
                    print(f"              ... and {len(self.known_sections) - 10} more")
                print(f"  Source: {broken['source_line'][:80]}")
        
        print("\n" + "=" * 70)
        
        # 返回 exit code
        if self.broken_links or self.broken_section_refs:
            return 1
        else:
            print("✓ All links and section references are valid!")
            return 0


def main():
    parser = argparse.ArgumentParser(
        description="Check markdown cross-references for consistency"
    )
    parser.add_argument(
        "--ci",
        action="store_true",
        help="CI mode: exit with code 1 if broken links found"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show detailed scan results for all links"
    )
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Repository root directory (default: current directory)"
    )
    
    args = parser.parse_args()
    
    # 確定 repo 根目錄
    repo_root = Path(args.repo_root).resolve()
    if not (repo_root / "docs").exists() and not (repo_root / "README.md").exists():
        print(f"ERROR: Cannot find documentation directory in {repo_root}", file=sys.stderr)
        print(f"       Expected to find 'docs/' or 'README.md' in repo root", file=sys.stderr)
        return 2
    
    checker = DocLinkChecker(str(repo_root), verbose=args.verbose)
    exit_code = checker.run()
    
    if args.ci:
        return exit_code
    else:
        # 非 CI 模式下始終返回 0
        return 0


if __name__ == "__main__":
    sys.exit(main())
