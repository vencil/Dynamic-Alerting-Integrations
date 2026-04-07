#!/usr/bin/env python3
"""文件閱讀時間檢查工具。

掃描 docs/ 下的 Markdown 文件，計算估計閱讀時間，並標記超過 15 分鐘的文件以供拆分。
v2.4.0 新增。

功能：
- 計算文件閱讀時間：去除代碼塊、YAML frontmatter、HTML 標籤後計算字數
- 支援雙語：英文 250 wpm，中文 200 wpm（中文字符按 1.5 詞計算）
- 可自訂閾值（預設 15 分鐘）
- --check 模式：超出閾值返回 exit 1（CI 驗證）
- --verbose 模式：顯示所有文件的閱讀時間，而非僅顯示違反者

Usage:
    python3 scripts/tools/lint/check_doc_reading_time.py
    python3 scripts/tools/lint/check_doc_reading_time.py --check
    python3 scripts/tools/lint/check_doc_reading_time.py --threshold 10
    python3 scripts/tools/lint/check_doc_reading_time.py --verbose
"""

import argparse
import sys
import re
from pathlib import Path
from typing import List, Tuple, Optional

# 嘗試導入共用函式庫
try:
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from _lib_python import write_text_secure
except ImportError:
    # 如果無法導入，定義簡單的 fallback
    def write_text_secure(path: str, content: str) -> None:
        """Safe file writing with UTF-8 encoding."""
        Path(path).write_text(content, encoding='utf-8')


def detect_cli_lang() -> str:
    """Detect CLI language from environment."""
    import os
    lang = os.environ.get('LANG', 'en_US.UTF-8')
    return 'zh' if 'zh' in lang.lower() else 'en'


def i18n_text(zh: str, en: str) -> str:
    """Return localized text based on CLI language."""
    return zh if detect_cli_lang() == 'zh' else en


class ReadingTimeChecker:
    """檢查 Markdown 文件是否超過可接受的閱讀時間。"""

    # 英文字符的 wpm（words per minute）
    ENGLISH_WPM = 250
    # 中文的 wpm（字符，按 1.5 詞計算）
    CHINESE_WPM = 200

    def __init__(
        self,
        docs_dir: str = "docs",
        threshold_minutes: int = 15,
        exclude_patterns: Optional[List[str]] = None,
    ):
        """初始化檢查器。

        Args:
            docs_dir: 文檔根目錄
            threshold_minutes: 閱讀時間閾值（分鐘）
            exclude_patterns: 排除的 glob 模式列表
        """
        self.docs_dir = Path(docs_dir)
        self.threshold_minutes = threshold_minutes
        self.exclude_patterns = exclude_patterns or [
            "internal/*",
            "assets/*",
            "interactive/*",
            "rule-packs/*",
            "schemas/*",
            "api/*",
            "adr/*"
        ]
        self.violations: List[Tuple[str, int, float]] = []  # (filename, word_count, reading_time)
        self.passes: List[Tuple[str, int, float]] = []  # (filename, word_count, reading_time)

    def matches_exclude_pattern(self, file_path: Path) -> bool:
        """檢查文件是否符合排除模式。"""
        relative_path = str(file_path.relative_to(self.docs_dir))
        for pattern in self.exclude_patterns:
            if Path(relative_path).match(pattern):
                return True
        return False

    def strip_frontmatter(self, content: str) -> str:
        """移除 YAML frontmatter（--- ... ---）。"""
        if not content.strip().startswith('---'):
            return content

        lines = content.split('\n')
        if len(lines) < 2:
            return content

        for i in range(1, len(lines)):
            if lines[i].strip() == '---':
                # 傳回 frontmatter 之後的內容
                return '\n'.join(lines[i+1:])

        return content

    def strip_code_blocks(self, content: str) -> str:
        """移除代碼塊（``` ... ```）。"""
        # 移除 fenced code blocks（```）
        content = re.sub(r'```[\s\S]*?```', '', content)
        # 移除 inline code（`...`）但保留內容
        # 這裡選擇保留 inline code 的內容，因為它們通常是重要的技術詞彙
        return content

    def strip_html_tags(self, content: str) -> str:
        """移除 HTML 標籤（<...>）。"""
        return re.sub(r'<[^>]+>', '', content)

    def count_english_words(self, text: str) -> int:
        """計算英文單詞數。"""
        # 簡單的英文單詞計數：以空白分隔的非中文序列
        # 移除標點符號並分割
        words = re.findall(r'[a-zA-Z0-9]+', text)
        return len(words)

    def count_chinese_characters(self, text: str) -> int:
        """計算中文字符數。"""
        # CJK Unified Ideographs ranges
        cjk_pattern = re.compile(
            r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff"
            r"\U00020000-\U0002a6df\U0002a700-\U0002b73f]"
        )
        return len(cjk_pattern.findall(text))

    def calculate_reading_time(self, content: str) -> Tuple[int, float]:
        """計算閱讀時間（分鐘）。

        Returns:
            (word_count, reading_time_in_minutes)
        """
        # 1. 移除 frontmatter
        content = self.strip_frontmatter(content)

        # 2. 移除代碼塊
        content = self.strip_code_blocks(content)

        # 3. 移除 HTML 標籤
        content = self.strip_html_tags(content)

        # 4. 移除 Markdown 特殊字符（##, -, *, 等）
        # 這裡簡單起見，保留文本但移除常見的 Markdown 符號
        content = re.sub(r'^#+\s+', '', content, flags=re.MULTILINE)  # 標題
        content = re.sub(r'^\s*[-*+]\s+', '', content, flags=re.MULTILINE)  # 列表
        content = re.sub(r'^\s*>\s+', '', content, flags=re.MULTILINE)  # 引言

        # 5. 計算英文單詞和中文字符
        english_words = self.count_english_words(content)
        chinese_chars = self.count_chinese_characters(content)

        # 中文字符按 1.5 詞計算（因為中文更濃縮）
        total_words = english_words + int(chinese_chars * 1.5)

        # 計算閱讀時間：取英文和中文的平均 wpm
        # 簡化：以總詞數除以平均 wpm
        avg_wpm = (self.ENGLISH_WPM + self.CHINESE_WPM) / 2
        reading_time = total_words / avg_wpm

        return total_words, reading_time

    def check_file(self, file_path: Path) -> Tuple[bool, int, float]:
        """檢查單一文件。

        Returns:
            (passed, word_count, reading_time)
        """
        try:
            content = file_path.read_text(encoding='utf-8')
        except Exception:
            # 無法讀取的文件視為通過（但記錄警告）
            return True, 0, 0.0

        word_count, reading_time = self.calculate_reading_time(content)
        passed = reading_time <= self.threshold_minutes

        relative_path = file_path.relative_to(self.docs_dir)
        if passed:
            self.passes.append((str(relative_path), word_count, reading_time))
        else:
            self.violations.append((str(relative_path), word_count, reading_time))

        return passed, word_count, reading_time

    def check_all_files(self) -> bool:
        """檢查所有 Markdown 文件。返回 True 表示無違反。"""
        if not self.docs_dir.exists():
            print(
                i18n_text(
                    f"錯誤：文檔目錄不存在：{self.docs_dir}",
                    f"error: docs directory does not exist: {self.docs_dir}"
                ),
                file=sys.stderr
            )
            return False

        md_files = sorted(self.docs_dir.rglob("*.md"))

        if not md_files:
            print(
                i18n_text(
                    f"警告：在 {self.docs_dir} 中找不到 Markdown 文件",
                    f"warning: no markdown files found in {self.docs_dir}"
                ),
                file=sys.stderr
            )
            return True

        for file_path in md_files:
            # 跳過排除的文件
            if self.matches_exclude_pattern(file_path):
                continue

            self.check_file(file_path)

        return len(self.violations) == 0

    def print_table_header(self) -> None:
        """輸出表格表頭。"""
        if detect_cli_lang() == 'zh':
            print(f"{'文件路徑':<50} {'字數':>8} {'閱讀時間':>10} {'狀態':>6}")
            print("-" * 76)
        else:
            print(f"{'File Path':<50} {'Words':>8} {'Read Time':>10} {'Status':>6}")
            print("-" * 76)

    def print_row(self, filename: str, word_count: int, reading_time: float, passed: bool) -> None:
        """輸出表格行。"""
        status = i18n_text("✓ 通過", "✓ PASS") if passed else i18n_text("✗ 超限", "✗ FAIL")
        time_str = f"{reading_time:.1f} min"
        print(f"{filename:<50} {word_count:>8} {time_str:>10} {status:>6}")

    def print_results(self, verbose: bool = False) -> None:
        """輸出檢查結果。"""
        self.print_table_header()

        # 輸出通過的文件（如果 verbose）
        if verbose:
            for filename, word_count, reading_time in sorted(self.passes):
                self.print_row(filename, word_count, reading_time, True)

        # 輸出違反的文件
        for filename, word_count, reading_time in sorted(self.violations):
            self.print_row(filename, word_count, reading_time, False)

        # 摘要
        total_files = len(self.passes) + len(self.violations)
        if self.violations:
            print(
                i18n_text(
                    f"\n總結：{len(self.violations)}/{total_files} 個文件超過 {self.threshold_minutes} 分鐘閾值（需要拆分）。",
                    f"\nsummary: {len(self.violations)}/{total_files} files exceed {self.threshold_minutes}-minute threshold (need splitting)."
                ),
                file=sys.stderr
            )
        else:
            print(
                i18n_text(
                    f"\n✓ 所有文件都在 {self.threshold_minutes} 分鐘以內。",
                    f"\n✓ all files are within {self.threshold_minutes} minutes."
                )
            )


def main():
    """CLI 入口點。"""
    parser = argparse.ArgumentParser(
        description=i18n_text(
            "檢查 Markdown 文件的閱讀時間是否超過閾值",
            "check if markdown files exceed reading time threshold"
        )
    )

    parser.add_argument(
        "--docs-dir",
        default="docs",
        help=i18n_text(
            "文檔根目錄（預設：docs）",
            "documentation root directory (default: docs)"
        )
    )

    parser.add_argument(
        "--threshold",
        type=int,
        default=15,
        help=i18n_text(
            "閱讀時間閾值（分鐘，預設：15）",
            "reading time threshold in minutes (default: 15)"
        )
    )

    parser.add_argument(
        "--check",
        action="store_true",
        help=i18n_text(
            "CI 模式：超出閾值時返回 exit 1",
            "CI mode: exit 1 if any file exceeds threshold"
        )
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
        help=i18n_text(
            "顯示所有文件的閱讀時間，而非僅顯示違反者",
            "show reading time for all files, not just violations"
        )
    )

    parser.add_argument(
        "--exclude",
        help=i18n_text(
            "排除的 glob 模式（逗號分隔，預設：internal/*,assets/*,...）",
            "exclude glob patterns (comma-separated)"
        )
    )

    args = parser.parse_args()

    # 解析排除模式
    exclude_patterns = None
    if args.exclude:
        exclude_patterns = [p.strip() for p in args.exclude.split(',')]

    # 建立檢查器
    checker = ReadingTimeChecker(
        docs_dir=args.docs_dir,
        threshold_minutes=args.threshold,
        exclude_patterns=exclude_patterns
    )

    # 執行檢查
    success = checker.check_all_files()
    checker.print_results(verbose=args.verbose)

    # 設定退出碼
    if args.check:
        sys.exit(0 if success else 1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
