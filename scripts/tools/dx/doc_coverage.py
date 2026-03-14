#!/usr/bin/env python3
"""doc_coverage.py — 文件覆蓋率 Dashboard

掃描文件庫，產出雙語覆蓋率、front matter 完整度、連結健康度報告。

用法:
  python3 scripts/tools/doc_coverage.py              # 文字報告
  python3 scripts/tools/doc_coverage.py --json        # JSON 輸出
  python3 scripts/tools/doc_coverage.py --badge       # Shield.io badge JSON
  python3 scripts/tools/doc_coverage.py --ci          # CI 模式（exit 1 if below 80%）
"""

import os
import re
import sys
import json
import argparse
from pathlib import Path
from collections import defaultdict
from typing import Dict, List, Tuple, Set
from dataclasses import dataclass, asdict


@dataclass
class FileCoverage:
    """單一檔案的覆蓋率資訊"""
    path: str
    has_frontmatter: bool
    frontmatter_complete: bool
    has_bilingual: bool
    title: str = ""
    tags: List[str] = None
    audience: List[str] = None
    version: str = ""
    lang: str = ""

    def __post_init__(self):
        if self.tags is None:
            self.tags = []
        if self.audience is None:
            self.audience = []


class DocCoverageAnalyzer:
    """掃描文件庫的覆蓋率分析器"""

    REQUIRED_FRONTMATTER_FIELDS = {"title", "tags", "audience", "version", "lang"}

    def __init__(self, repo_root: str):
        self.repo_root = Path(repo_root).resolve()
        self.scan_dirs = ["docs", "rule-packs"]
        self.root_md_files = ["README.md", "README.en.md", "CHANGELOG.md", "CLAUDE.md"]

        # 覆蓋率結果
        self.file_coverage: List[FileCoverage] = []
        self.bilingual_pairs = 0  # 同時有 .md 和 .en.md 的數量
        self.files_with_frontmatter = 0
        self.files_with_complete_frontmatter = 0
        self.total_links_checked = 0
        self.broken_links: List[Dict] = []

        # 已知的 section 參考集合
        self.known_sections: Set[str] = set()
        self.section_pattern = re.compile(r'§(\d+)\.(\d+)')

    def _get_all_md_files(self) -> List[Path]:
        """取得所有要掃描的 Markdown 文件"""
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

    def _extract_frontmatter(self, file_path: Path) -> Tuple[bool, Dict[str, any]]:
        """解析 YAML front matter

        Returns:
            (has_frontmatter, fields_dict)
        """
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            return False, {}

        # 檢查是否以 --- 開始
        if not content.startswith("---"):
            return False, {}

        # 找到結束的 ---
        lines = content.split("\n")
        if len(lines) < 3:
            return False, {}

        end_idx = None
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                end_idx = i
                break

        if end_idx is None:
            return False, {}

        # 解析 YAML front matter
        frontmatter_lines = lines[1:end_idx]
        fields = {}

        for line in frontmatter_lines:
            if ":" not in line:
                continue

            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()

            # 簡單的 YAML 解析（不使用完整 YAML 庫）
            # 移除引號
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            elif value.startswith("'") and value.endswith("'"):
                value = value[1:-1]

            # 解析清單
            if value.startswith("[") and value.endswith("]"):
                value = [v.strip().strip('"\'') for v in value[1:-1].split(",")]

            fields[key] = value

        return True, fields

    def _is_frontmatter_complete(self, fields: Dict) -> bool:
        """檢查 front matter 是否完整"""
        for required_field in self.REQUIRED_FRONTMATTER_FIELDS:
            if required_field not in fields or not fields[required_field]:
                return False
        return True

    def _has_bilingual_pair(self, file_path: Path) -> bool:
        """檢查是否存在雙語對（.md 和 .en.md）"""
        # 只檢查非 .en.md 的檔案
        if file_path.name.endswith(".en.md"):
            return False

        # 尋找對應的 .en.md
        base_name = file_path.stem  # 移除 .md
        en_path = file_path.parent / f"{base_name}.en.md"

        return en_path.exists()

    def _extract_sections(self) -> None:
        """從 Markdown 文件中提取已知的 section 編號"""
        md_files = self._get_all_md_files()

        for md_file in md_files:
            try:
                with open(md_file, "r", encoding="utf-8") as f:
                    for line in f:
                        matches = self.section_pattern.findall(line)
                        for major, minor in matches:
                            self.known_sections.add(f"{major}.{minor}")
            except Exception:
                pass

    def _is_external_url(self, url: str) -> bool:
        """檢查是否為外部 URL"""
        return url.startswith("http://") or url.startswith("https://")

    def _resolve_link_path(self, source_file: Path, link: str) -> Tuple[Path, bool]:
        """解析相對連結為絕對路徑

        Returns:
            (resolved_path, is_valid)
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

    def _is_in_code_block(self, lines: List[str], line_num: int) -> bool:
        """檢查指定行是否在程式碼區塊內"""
        fence_count = 0
        for i in range(line_num):
            if lines[i].strip().startswith("```"):
                fence_count += 1
        return fence_count % 2 == 1

    def _check_links_in_file(self, file_path: Path) -> None:
        """檢查單一檔案中的所有連結"""
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception:
            return

        link_pattern = re.compile(r'\[([^\]]+)\]\(([^\)]+)\)')

        for line_num, line in enumerate(lines, 1):
            # 跳過程式碼區塊
            if self._is_in_code_block(lines, line_num - 1):
                continue

            for match in link_pattern.finditer(line):
                link_url = match.group(2)

                self.total_links_checked += 1

                # 跳過外部 URL
                if self._is_external_url(link_url):
                    continue

                # 解析路徑
                target_path, is_valid = self._resolve_link_path(file_path, link_url)

                # 檢查目標是否存在
                if not target_path.exists():
                    self.broken_links.append({
                        "file": str(file_path.relative_to(self.repo_root)),
                        "line": line_num,
                        "link": link_url,
                    })

    def analyze(self) -> None:
        """執行完整的覆蓋率分析"""
        self._extract_sections()
        md_files = self._get_all_md_files()

        # 用來追蹤雙語對
        base_names_with_en = set()

        for file_path in md_files:
            # 取得 front matter
            has_fm, fm_fields = self._extract_frontmatter(file_path)

            if has_fm:
                self.files_with_frontmatter += 1

            is_fm_complete = self._is_frontmatter_complete(fm_fields) if has_fm else False
            if is_fm_complete:
                self.files_with_complete_frontmatter += 1

            # 檢查雙語覆蓋（跳過 .en.md 檔案本身）
            has_bilingual = False
            if not file_path.name.endswith(".en.md"):
                has_bilingual = self._has_bilingual_pair(file_path)
                if has_bilingual:
                    # 記錄基礎名稱以計算雙語對數量
                    base_name = file_path.stem
                    base_names_with_en.add(base_name)

            # 創建覆蓋率記錄
            coverage = FileCoverage(
                path=str(file_path.relative_to(self.repo_root)),
                has_frontmatter=has_fm,
                frontmatter_complete=is_fm_complete,
                has_bilingual=has_bilingual,
                title=fm_fields.get("title", ""),
                tags=fm_fields.get("tags", []),
                audience=fm_fields.get("audience", []),
                version=fm_fields.get("version", ""),
                lang=fm_fields.get("lang", ""),
            )
            self.file_coverage.append(coverage)

            # 檢查連結
            self._check_links_in_file(file_path)

        self.bilingual_pairs = len(base_names_with_en)

    def get_statistics(self) -> Dict:
        """取得統計數據"""
        total_md_files = len([f for f in self.file_coverage if not f.path.endswith(".en.md")])

        # 排除 .en.md 檔案用於雙語覆蓋率計算
        non_en_files = [f for f in self.file_coverage if not f.path.endswith(".en.md")]
        bilingual_coverage = self.bilingual_pairs / len(non_en_files) * 100 if non_en_files else 0

        frontmatter_coverage = self.files_with_complete_frontmatter / len(self.file_coverage) * 100 if self.file_coverage else 0

        link_health = 100 - (len(self.broken_links) / self.total_links_checked * 100) if self.total_links_checked > 0 else 100

        return {
            "total_files": len(self.file_coverage),
            "files_with_frontmatter": self.files_with_frontmatter,
            "files_with_complete_frontmatter": self.files_with_complete_frontmatter,
            "frontmatter_coverage_percent": round(frontmatter_coverage, 1),
            "bilingual_pairs": self.bilingual_pairs,
            "non_bilingual_files": total_md_files - self.bilingual_pairs,
            "bilingual_coverage_percent": round(bilingual_coverage, 1),
            "total_links_checked": self.total_links_checked,
            "broken_links": len(self.broken_links),
            "link_health_percent": round(link_health, 1),
        }

    def print_text_report(self) -> None:
        """輸出文字報告"""
        stats = self.get_statistics()

        print("=" * 70)
        print("DOCUMENTATION COVERAGE DASHBOARD")
        print("=" * 70)
        print()

        # Front Matter 覆蓋率
        print("📋 FRONT MATTER COMPLETENESS")
        print("-" * 70)
        print(f"Complete: {stats['files_with_complete_frontmatter']}/{stats['total_files']} "
              f"({stats['frontmatter_coverage_percent']}%)")
        print()

        # 雙語覆蓋率
        print("🌐 BILINGUAL COVERAGE")
        print("-" * 70)
        print(f"Paired: {stats['bilingual_pairs']} files with .en.md")
        print(f"Coverage: {stats['non_bilingual_files']} .md files without .en.md "
              f"({stats['bilingual_coverage_percent']}%)")
        print()

        # 連結健康度
        print("🔗 LINK HEALTH")
        print("-" * 70)
        print(f"Checked: {stats['total_links_checked']} links")
        print(f"Broken: {stats['broken_links']} links ({100 - stats['link_health_percent']}%)")
        print(f"Health: {stats['link_health_percent']}%")
        print()

        # 詳細表格
        print("📊 PER-FILE STATUS")
        print("-" * 70)
        print(f"{'File':<45} {'FM':<3} {'Bilingual':<10} {'Status':<10}")
        print("-" * 70)

        for coverage in sorted(self.file_coverage, key=lambda x: x.path):
            # 跳過 .en.md 檔案在表格中（它們不需要雙語標記）
            fm_status = "✓" if coverage.frontmatter_complete else "✗"
            bilingual_status = "✓" if coverage.has_bilingual else "✗" if not coverage.path.endswith(".en.md") else "-"

            # 檢查該檔案是否有破損連結
            has_broken = any(bl["file"] == coverage.path for bl in self.broken_links)
            file_status = "❌ BROKEN" if has_broken else "✓ OK"

            file_display = coverage.path[-40:] if len(coverage.path) > 40 else coverage.path
            print(f"{file_display:<45} {fm_status:<3} {bilingual_status:<10} {file_status:<10}")

        print()

        # 破損連結詳情（如果有）
        if self.broken_links:
            print("=" * 70)
            print("⚠️  BROKEN LINKS DETAILS")
            print("=" * 70)
            for broken in sorted(self.broken_links, key=lambda x: x["file"]):
                print(f"{broken['file']}:{broken['line']}")
                print(f"  → {broken['link']}")
            print()

    def get_json_report(self) -> Dict:
        """取得 JSON 格式的報告"""
        stats = self.get_statistics()

        return {
            "timestamp": "",
            "statistics": stats,
            "files": [asdict(f) for f in self.file_coverage],
            "broken_links": self.broken_links,
        }

    def get_badge_json(self) -> Dict:
        """取得 shield.io badge 格式的 JSON"""
        stats = self.get_statistics()

        # 計算平均覆蓋率
        avg_coverage = (
            stats["frontmatter_coverage_percent"] +
            stats["bilingual_coverage_percent"] +
            stats["link_health_percent"]
        ) / 3

        # 決定顏色
        if avg_coverage >= 90:
            color = "green"
        elif avg_coverage >= 70:
            color = "yellow"
        elif avg_coverage >= 50:
            color = "orange"
        else:
            color = "red"

        return {
            "schemaVersion": 1,
            "label": "docs coverage",
            "message": f"{round(avg_coverage, 0):.0f}%",
            "color": color,
        }


def main():
    parser = argparse.ArgumentParser(
        description="Documentation coverage dashboard"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON report"
    )
    parser.add_argument(
        "--badge",
        action="store_true",
        help="Output shield.io badge JSON"
    )
    parser.add_argument(
        "--ci",
        action="store_true",
        help="CI mode: exit 1 if any metric below 80 percent"
    )
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Repository root directory (default: current directory)"
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=80,
        help="CI threshold percentage (default: 80)"
    )

    args = parser.parse_args()

    # 解析 repo root
    repo_root = Path(args.repo_root).resolve()
    if not repo_root.exists():
        print(f"ERROR: Repository root not found: {repo_root}", file=sys.stderr)
        return 1

    # 執行分析
    analyzer = DocCoverageAnalyzer(str(repo_root))
    analyzer.analyze()

    # 輸出結果
    if args.json:
        report = analyzer.get_json_report()
        print(json.dumps(report, indent=2, ensure_ascii=False))
    elif args.badge:
        badge = analyzer.get_badge_json()
        print(json.dumps(badge, indent=2, ensure_ascii=False))
    else:
        analyzer.print_text_report()

    # CI 模式檢查
    if args.ci:
        stats = analyzer.get_statistics()

        fm_ok = stats["frontmatter_coverage_percent"] >= args.threshold
        bilingual_ok = stats["bilingual_coverage_percent"] >= args.threshold
        link_ok = stats["link_health_percent"] >= args.threshold

        if not fm_ok:
            coverage_pct = stats['frontmatter_coverage_percent']
            threshold_pct = args.threshold
            print(f"ERROR: Front matter coverage {coverage_pct}% "
                  f"below threshold {threshold_pct}%", file=sys.stderr)
        if not bilingual_ok:
            coverage_pct = stats['bilingual_coverage_percent']
            threshold_pct = args.threshold
            print(f"ERROR: Bilingual coverage {coverage_pct}% "
                  f"below threshold {threshold_pct}%", file=sys.stderr)
        if not link_ok:
            health_pct = stats['link_health_percent']
            threshold_pct = args.threshold
            print(f"ERROR: Link health {health_pct}% "
                  f"below threshold {threshold_pct}%", file=sys.stderr)

        if not (fm_ok and bilingual_ok and link_ok):
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
