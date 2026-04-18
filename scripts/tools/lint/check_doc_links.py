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

        # Bilingual exemption list
        # Internal docs (docs/internal/) are exempt from bilingual requirements per project policy.
        # Only externally-facing docs need EN counterparts. Utility files also exempt.
        self.bilingual_exempt_paths = {
            "docs/internal/",
            "docs/CHANGELOG.md",
            "docs/includes/",
            "docs/tags.md",
            "docs/getting-started/README.md",
            # Navigation index pages (zh-only landing pages with Language switch banners)
            "docs/scenarios/README.md",
            # TODO(v2.7.0): translate vcs-integration-guide to .en.md
            "docs/vcs-integration-guide.md",
        }

        # Load ignore patterns from .doclinkignore
        self._ignore_patterns: Set[str] = self._load_ignore_file()
        
        # 統計
        self.total_links_checked = 0
        self.broken_links = []
        self.broken_anchors = []
        self.section_refs_checked = 0
        self.broken_section_refs = []
        self.verbose_links = []

        # Cache: filepath -> set of anchor ids
        self._heading_cache: Dict[Path, Set[str]] = {}
        
        self.ignored_count = 0

        # Cross-language counterpart check results
        self.missing_counterparts = []

        # 已知的 section 參考格式：§X.Y (主章節.副章節)
        self.section_pattern = re.compile(r'§(\d+)\.(\d+)')
        
        # Cache md file list (rglob is expensive on mounted fs)
        self._md_files_cache: List[Path] = []

        # 已知的 section 集合 (從 CLAUDE.md 和其他文件中解析)
        self.known_sections = self._extract_sections()

    def _load_ignore_file(self) -> Set[str]:
        """Load .doclinkignore patterns.

        Each non-empty, non-comment line is a pattern of the form:
          file.md:link_url
        or just:
          link_url
        Lines starting with # are comments.
        """
        ignore_file = self.repo_root / ".doclinkignore"
        if not ignore_file.exists():
            return set()
        patterns: Set[str] = set()
        for line in ignore_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            patterns.add(line)
        return patterns

    def _is_ignored(self, source_file: Path, link_url: str) -> bool:
        """Check if a broken link should be ignored."""
        if not self._ignore_patterns:
            return False
        rel = source_file.relative_to(self.repo_root).as_posix()
        # Match "file:link" or just "link"
        return (f"{rel}:{link_url}" in self._ignore_patterns
                or link_url in self._ignore_patterns)

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
            except OSError:
                pass
        
        return sections

    def _get_all_md_files(self) -> List[Path]:
        """取得所有要掃描的 Markdown 文件（cached）。

        Excludes gitignored paths (via `git ls-files --others --ignored`).
        Internal time-bound docs like v2.*-planning.md live under
        docs/internal/ and are gitignored — skipping them here avoids
        scanning files that aren't part of the shipping docs corpus.
        """
        if self._md_files_cache:
            return self._md_files_cache

        # Build gitignored-path set (mirrors generate_doc_map.py logic).
        # Uses --directory so whole ignored subdirs show up as single entries;
        # we then walk ancestor chain when checking each candidate.
        import subprocess
        _gitignored: set = set()
        try:
            out = subprocess.run(
                ["git", "ls-files", "--others", "--ignored",
                 "--exclude-standard", "--directory"],
                capture_output=True, text=True, cwd=str(self.repo_root),
                timeout=10,
            )
            if out.returncode == 0:
                for line in out.stdout.splitlines():
                    _gitignored.add(line.rstrip("/"))
        except (subprocess.SubprocessError, FileNotFoundError, OSError):
            pass  # git unavailable → no exclusion (safe default)

        def _is_ignored(p: Path) -> bool:
            rel = p.relative_to(self.repo_root).as_posix()
            if rel in _gitignored:
                return True
            ancestor = Path(rel).parent
            while ancestor.as_posix() not in {"", "."}:
                if ancestor.as_posix() in _gitignored:
                    return True
                ancestor = ancestor.parent
            return False

        md_files = []

        # 掃描 docs/ 和 rule-packs/
        for scan_dir in self.scan_dirs:
            dir_path = self.repo_root / scan_dir
            if dir_path.exists():
                md_files.extend(
                    f for f in dir_path.rglob("*.md") if not _is_ignored(f)
                )

        # 掃描根目錄的 Markdown 文件
        for md_file in self.root_md_files:
            file_path = self.repo_root / md_file
            if file_path.exists() and not _is_ignored(file_path):
                md_files.append(file_path)

        self._md_files_cache = sorted(list(set(md_files)))
        return self._md_files_cache

    @staticmethod
    def _heading_to_anchor(heading_text: str) -> str:
        """Convert heading text to GFM-compatible anchor id.

        GFM rules:
        - Lowercase
        - Remove non-alphanumeric/CJK chars except hyphens and spaces
        - Replace spaces with hyphens
        - Strip leading/trailing hyphens
        """
        text = heading_text.strip()
        # Remove inline markdown: bold, italic, code, links, images
        text = re.sub(r"[*_`]", "", text)
        text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
        text = re.sub(r"!\[([^\]]*)\]\([^\)]+\)", r"\1", text)
        # Remove emoji shortcodes like :rocket:
        text = re.sub(r":[a-zA-Z0-9_+-]+:", "", text)
        # Lowercase
        text = text.lower()
        # Keep alphanumeric, CJK, spaces, hyphens
        text = re.sub(
            r"[^\w\s\u4e00-\u9fff\u3400-\u4dbf\uF900-\uFAFF-]",
            "", text)
        # Replace whitespace with hyphens
        text = re.sub(r"\s+", "-", text)
        # Strip leading/trailing hyphens
        text = text.strip("-")
        return text

    def _get_headings(self, filepath: Path) -> Set[str]:
        """Extract all heading anchors from a Markdown file (cached)."""
        resolved = filepath.resolve()
        if resolved in self._heading_cache:
            return self._heading_cache[resolved]

        anchors: Set[str] = set()
        try:
            with open(resolved, "r", encoding="utf-8") as f:
                lines = f.readlines()

            in_code = False
            for line in lines:
                stripped = line.strip()
                if stripped.startswith("```"):
                    in_code = not in_code
                    continue
                if in_code:
                    continue
                m = re.match(r"^(#{1,6})\s+(.+)", line)
                if m:
                    heading_text = m.group(2).strip()
                    anchor = self._heading_to_anchor(heading_text)
                    if anchor:
                        anchors.add(anchor)
        except OSError:
            pass

        self._heading_cache[resolved] = anchors
        return anchors

    @staticmethod
    def _fuzzy_best(needle: str, haystack: Set[str],
                    threshold: float = 0.5) -> str:
        """Find the best fuzzy match for needle in haystack.

        Uses a simple ratio based on longest common subsequence length.
        Returns the best match or '' if below threshold.
        """
        def _lcs_len(a: str, b: str) -> int:
            m, n = len(a), len(b)
            if m == 0 or n == 0:
                return 0
            prev = [0] * (n + 1)
            for i in range(1, m + 1):
                curr = [0] * (n + 1)
                for j in range(1, n + 1):
                    if a[i - 1] == b[j - 1]:
                        curr[j] = prev[j - 1] + 1
                    else:
                        curr[j] = max(prev[j], curr[j - 1])
                prev = curr
            return prev[n]

        best_score = 0.0
        best_match = ""
        for candidate in haystack:
            max_len = max(len(needle), len(candidate))
            if max_len == 0:
                continue
            score = _lcs_len(needle, candidate) / max_len
            if score > best_score:
                best_score = score
                best_match = candidate
        return best_match if best_score >= threshold else ""

    def _check_anchor(self, target_path: Path, anchor: str,
                      source_file: Path, line_num: int,
                      link_url: str) -> None:
        """Validate that an anchor exists in the target file's headings."""
        if not anchor:
            return
        headings = self._get_headings(target_path)
        if not headings:
            # No headings extracted — skip (might be non-standard format)
            return
        if anchor not in headings:
            best = self._fuzzy_best(anchor, headings)
            self.broken_anchors.append({
                "file": source_file.relative_to(self.repo_root),
                "line": line_num,
                "link": link_url,
                "anchor": anchor,
                "best_match": best,
                "available": sorted(headings)[:5],
            })

    @staticmethod
    def _build_code_block_set(lines: List[str]) -> Set[int]:
        """Pre-compute the set of line numbers inside code blocks.

        Returns a set of 0-based line indices that are inside fenced
        code blocks.  O(n) instead of O(n²).
        """
        in_code: Set[int] = set()
        inside = False
        for i, line in enumerate(lines):
            if line.strip().startswith("```"):
                inside = not inside
                in_code.add(i)
                continue
            if inside:
                in_code.add(i)
        return in_code

    def _is_external_url(self, url: str) -> bool:
        """檢查是否為外部 URL。"""
        return url.startswith("http://") or url.startswith("https://")

    def _resolve_link_path(self, source_file: Path, link: str) -> Tuple[Path, bool]:
        """
        解析相對連結為絕對路徑。

        Returns:
            (resolved_path, is_valid) - resolved_path 可能不存在，is_valid 表示路徑是否有效
        """
        import os as _os

        # 移除 anchor 和 query string (e.g. jsx-loader.html?component=...)
        file_part = link.split("#")[0].split("?")[0]

        if not file_part:
            # 純 anchor，指向同一檔案
            return source_file, True

        # 相對於來源檔案的目錄 — resolve() 讓 symlink (如 docs/README-root.md
        # → ../README.md) 產生的相對連結能正確指向 repo root。
        try:
            source_dir = source_file.resolve().parent
        except OSError:
            source_dir = source_file.parent
        joined = source_dir / file_part

        # 用 lexical normpath 先做一次路徑計算（不 follow symlink），
        # 這樣才能偵測 docs/rule-packs/... 這種跨 symlink 的路徑。
        lexical = Path(_os.path.normpath(str(joined)))
        lexical_rel = ""
        try:
            lexical_rel = lexical.relative_to(self.repo_root).as_posix()
        except ValueError:
            pass

        # FUSE fallback: docs/rule-packs 是指向 ../rule-packs 的 symlink，
        # 但 WSL/Cowork FUSE 會把它材料化成絕對 Windows 路徑導致 broken link。
        # 在 resolve 之前用 lexical 路徑判斷：只要命中 docs/rule-packs/... 就直接
        # 轉到 rule-packs/...，避開 broken symlink 查找。
        if (lexical_rel.startswith("docs/rule-packs/")
                or lexical_rel == "docs/rule-packs"):
            alt_rel = lexical_rel.replace("docs/rule-packs", "rule-packs", 1)
            return self.repo_root / alt_rel, True

        try:
            target_path = joined.resolve()
        except OSError:
            target_path = lexical

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
        except OSError as e:
            print(f"ERROR: Cannot read {md_file}: {e}", file=sys.stderr)
            return
        
        # Markdown 連結模式：[text](path)
        link_pattern = re.compile(r'\[([^\]]+)\]\(([^\)]+)\)')
        code_lines = self._build_code_block_set(lines)

        for line_num, line in enumerate(lines, 1):
            # 跳過程式碼區塊
            if (line_num - 1) in code_lines:
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
                    if self._is_ignored(md_file, link_url):
                        self.ignored_count += 1
                        continue
                    suggestion = self._find_suggestions(link_url, md_file)
                    self.broken_links.append({
                        "file": md_file.relative_to(self.repo_root),
                        "line": line_num,
                        "link": link_url,
                        "target": target_path.relative_to(self.repo_root) if is_valid else link_url,
                        "suggestion": suggestion,
                        "source_line": line.strip()
                    })
                elif "#" in link_url:
                    # File exists — validate anchor
                    if self._is_ignored(md_file, link_url):
                        self.ignored_count += 1
                        continue
                    anchor = link_url.split("#", 1)[1]
                    self._check_anchor(
                        target_path, anchor, md_file, line_num, link_url)
            
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

    def _is_bilingual_exempt(self, doc_path: str) -> bool:
        """Check if a file is exempt from bilingual requirements.

        Internal docs (docs/internal/) are exempt per project policy.
        Utility/infrastructure files (CHANGELOG, includes, tags, etc.) are also exempt.
        Only externally-facing docs need EN counterparts.
        """
        for exempt_prefix in self.bilingual_exempt_paths:
            if doc_path.startswith(exempt_prefix):
                return True
        return False

    def check_cross_language_counterparts(self):
        """Check that zh docs have en counterparts and vice versa.

        For each .en.md file, verify the zh counterpart exists (without .en).
        For each zh .md file (not .en.md), check if an .en.md counterpart exists.
        Only checks docs/ directory (not root files like CHANGELOG.md).

        Files under docs/internal/ and utility files are exempt from bilingual
        requirements per project policy.
        """
        docs_path = self.repo_root / "docs"
        if not docs_path.is_dir():
            return

        # Collect all .en.md files
        en_files = set()
        zh_files = set()
        for f in docs_path.rglob("*.md"):
            rel = f.relative_to(docs_path)
            if f.name.endswith(".en.md"):
                en_files.add(str(rel))
            else:
                zh_files.add(str(rel))

        # Check each .en.md has a zh counterpart
        for en_rel in sorted(en_files):
            zh_rel = en_rel.replace(".en.md", ".md")
            if zh_rel not in zh_files:
                # Skip if exempt
                if self._is_bilingual_exempt(f"docs/{zh_rel}"):
                    continue
                self.missing_counterparts.append({
                    "file": f"docs/{en_rel}",
                    "missing": f"docs/{zh_rel}",
                    "direction": "en→zh",
                })

        # Check each zh .md has an .en.md counterpart (only for files
        # where at least one .en.md exists in the same directory)
        en_dirs = {str(Path(e).parent) for e in en_files}
        for zh_rel in sorted(zh_files):
            # Skip if exempt
            if self._is_bilingual_exempt(f"docs/{zh_rel}"):
                continue
            zh_dir = str(Path(zh_rel).parent)
            if zh_dir not in en_dirs:
                continue  # Skip dirs without any .en.md files
            en_rel = zh_rel.replace(".md", ".en.md")
            if en_rel not in en_files:
                self.missing_counterparts.append({
                    "file": f"docs/{zh_rel}",
                    "missing": f"docs/{en_rel}",
                    "direction": "zh→en",
                })

    def run(self) -> int:
        """執行掃描並返回 exit code。"""
        print("Scanning documentation files for broken links...\n")

        md_files = self._get_all_md_files()
        print(f"Found {len(md_files)} Markdown files to scan.\n")
        
        for md_file in md_files:
            self.scan_file(md_file)

        # Cross-language counterpart check
        self.check_cross_language_counterparts()

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
        print(f"Broken anchors found: {len(self.broken_anchors)}")
        if self.ignored_count:
            print(f"Ignored (via .doclinkignore): {self.ignored_count}")
        print(f"Section references checked: {self.section_refs_checked}")
        print(f"Broken section references: {len(self.broken_section_refs)}")
        if self.missing_counterparts:
            print(f"Missing language counterparts: {len(self.missing_counterparts)}")
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
        
        # 列印破損的 anchor
        if self.broken_anchors:
            print("\n" + "=" * 70)
            print("BROKEN ANCHORS:")
            print("=" * 70)
            for broken in self.broken_anchors:
                print(f"\nFile: {broken['file']}:{broken['line']}")
                print(f"  Link: {broken['link']}")
                print(f"  Missing anchor: #{broken['anchor']}")
                best = broken.get("best_match", "")
                if best:
                    print(f"  Best match: #{best}")
                elif broken['available']:
                    print(f"  Similar: {', '.join(broken['available'][:5])}")

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
        
        # 列印缺失的語言對應檔案
        if self.missing_counterparts:
            print("\n" + "=" * 70)
            print("MISSING LANGUAGE COUNTERPARTS:")
            print("=" * 70)
            for entry in self.missing_counterparts:
                print(f"\n  File: {entry['file']}")
                print(f"  Missing: {entry['missing']}")
                print(f"  Direction: {entry['direction']}")

        print("\n" + "=" * 70)

        # 返回 exit code
        if self.broken_links or self.broken_anchors or self.broken_section_refs:
            return 1
        else:
            print("✓ All links and section references are valid!")
            return 0


def _fix_broken_anchors(broken_anchors: list, repo_root: Path) -> int:
    """Fix broken anchor links in source files.

    For each broken anchor with a fuzzy best_match, replaces
    `#old_anchor` with `#best_match` in the source file.

    Returns number of fixes applied.
    """
    # Group fixes by file to batch I/O
    fixes_by_file: Dict[Path, List[tuple]] = defaultdict(list)
    for entry in broken_anchors:
        best = entry.get("best_match", "")
        if not best:
            continue
        src_file = repo_root / entry["file"]
        old_anchor = entry["anchor"]
        fixes_by_file[src_file].append((old_anchor, best, entry["link"]))

    fixed_count = 0
    for fpath, fixes in fixes_by_file.items():
        content = fpath.read_text(encoding="utf-8")
        new_content = content
        for old_anchor, new_anchor, link_url in fixes:
            # Replace the exact link reference
            old_link = f"#{old_anchor}"
            new_link = f"#{new_anchor}"
            if old_link in new_content:
                new_content = new_content.replace(old_link, new_link, 1)
                print(f"  🔧 {fpath.relative_to(repo_root)}: "
                      f"#{old_anchor} → #{new_anchor}")
                fixed_count += 1

        if new_content != content:
            fpath.write_text(new_content, encoding="utf-8")
            os.chmod(fpath, 0o644)

    return fixed_count


def main():
    """CLI entry point: 文件間交叉引用一致性檢查."""
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
        "--fix-anchors",
        action="store_true",
        help="Auto-fix broken #anchor links using fuzzy heading match"
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

    # --fix-anchors: auto-fix broken anchors with fuzzy match
    if args.fix_anchors and checker.broken_anchors:
        fixed = _fix_broken_anchors(checker.broken_anchors, repo_root)
        if fixed > 0:
            print(f"\n🔧 Fixed {fixed} broken anchor(s). Re-run to verify.")

    if args.ci:
        return exit_code
    else:
        # 非 CI 模式下始終返回 0
        return 0


if __name__ == "__main__":
    sys.exit(main())
