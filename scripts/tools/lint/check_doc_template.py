#!/usr/bin/env python3
"""文件模板合規性檢查工具。

檢查 docs/ 下的 Markdown 文件是否符合 doc-template.md 定義的結構規範。
v2.3.0 新增。

功能：
- 驗證 frontmatter 存在且包含必須欄位 (title, lang)
- 檢查相關資源 section 是否存在
- 可選：檢查 frontmatter 版本與指定版本一致
- 可選：自動補充缺失的相關資源 section
"""

import argparse
import sys
import re
import yaml
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


class DocTemplateChecker:
    """檢查 Markdown 文件是否符合 doc-template 規範。"""

    RELATED_RESOURCES_PATTERNS = [
        r'^##\s+相關資源\s*$',
        r'^##\s+Related Resources\s*$'
    ]

    def __init__(
        self,
        docs_dir: str = "docs",
        version: Optional[str] = None,
        check_version: bool = False,
        exclude_patterns: Optional[List[str]] = None,
    ):
        """初始化檢查器。

        Args:
            docs_dir: 文檔根目錄
            version: 預期版本（用於版本一致性檢查）
            check_version: 是否檢查版本一致性
            exclude_patterns: 排除的 glob 模式列表
        """
        self.docs_dir = Path(docs_dir)
        self.version = version
        self.check_version = check_version
        self.exclude_patterns = exclude_patterns or [
            "internal/*",
            "assets/*",
            "interactive/*",
            "rule-packs/*",
            "schemas/*",
            "api/*",
            "adr/*"
        ]
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.passes: List[str] = []

    def matches_exclude_pattern(self, file_path: Path) -> bool:
        """檢查文件是否符合排除模式。"""
        relative_path = str(file_path.relative_to(self.docs_dir))
        for pattern in self.exclude_patterns:
            if Path(relative_path).match(pattern):
                return True
        return False

    def check_frontmatter_exists(self, content: str) -> bool:
        """檢查文件是否有 frontmatter。"""
        return content.strip().startswith('---')

    def extract_frontmatter(self, content: str) -> Optional[dict]:
        """提取 YAML frontmatter。"""
        try:
            # 檢查是否以 --- 開頭
            if not content.strip().startswith('---'):
                return None

            # 尋找結尾的 ---
            lines = content.split('\n')
            if len(lines) < 2:
                return None

            # 跳過開頭的 ---
            frontmatter_lines = []
            for i in range(1, len(lines)):
                if lines[i].strip() == '---':
                    # 找到結尾的 ---，提取 frontmatter 文本
                    fm_text = '\n'.join(lines[1:i])
                    try:
                        metadata = yaml.safe_load(fm_text)
                        return metadata if isinstance(metadata, dict) else {}
                    except yaml.YAMLError:
                        return None
            return None
        except Exception:
            return None

    def check_required_fields(self, metadata: dict) -> Tuple[bool, List[str]]:
        """檢查必須的 frontmatter 欄位。"""
        required_fields = ['title', 'lang']
        missing = [f for f in required_fields if f not in metadata]
        return len(missing) == 0, missing

    def has_related_resources_section(self, content: str) -> bool:
        """檢查是否存在相關資源 section。"""
        lines = content.split('\n')
        for i, line in enumerate(lines):
            for pattern in self.RELATED_RESOURCES_PATTERNS:
                if re.match(pattern, line):
                    # 確保後面有內容（不是文件末尾空白）
                    if i + 1 < len(lines):
                        return True
        return False

    def check_version_consistency(self, metadata: dict) -> Tuple[bool, Optional[str]]:
        """檢查版本一致性。"""
        if not self.check_version or not self.version:
            return True, None

        doc_version = metadata.get('version')
        if not doc_version:
            return False, i18n_text(
                f"frontmatter 缺少 version 欄位（預期：{self.version}）",
                f"missing version in frontmatter (expected: {self.version})"
            )

        if doc_version != self.version:
            return False, i18n_text(
                f"版本不一致（frontmatter: {doc_version}, 預期: {self.version}）",
                f"version mismatch (frontmatter: {doc_version}, expected: {self.version})"
            )

        return True, None

    def check_file(self, file_path: Path) -> bool:
        """檢查單一文件。返回 True 表示通過所有檢查（無 errors）。"""
        relative_path = file_path.relative_to(self.docs_dir)
        filename = str(relative_path)

        try:
            content = file_path.read_text(encoding='utf-8')
        except Exception as e:
            self.errors.append(
                i18n_text(
                    f"FAIL: {filename} — 無法讀取文件：{e}",
                    f"FAIL: {filename} — cannot read file: {e}"
                )
            )
            return False

        # 1. 檢查 frontmatter 存在
        if not self.check_frontmatter_exists(content):
            self.errors.append(
                i18n_text(
                    f"FAIL: {filename} — 缺少 frontmatter（文件應以 --- 開頭）",
                    f"FAIL: {filename} — missing frontmatter (file should start with ---)"
                )
            )
            return False

        # 2. 提取並驗證 frontmatter 欄位
        metadata = self.extract_frontmatter(content)
        if metadata is None:
            self.errors.append(
                i18n_text(
                    f"FAIL: {filename} — 無效的 YAML frontmatter",
                    f"FAIL: {filename} — invalid YAML frontmatter"
                )
            )
            return False

        # 3. 檢查必須欄位
        fields_ok, missing_fields = self.check_required_fields(metadata)
        if not fields_ok:
            self.errors.append(
                i18n_text(
                    f"FAIL: {filename} — 缺少必須欄位：{', '.join(missing_fields)}",
                    f"FAIL: {filename} — missing required fields: {', '.join(missing_fields)}"
                )
            )
            return False

        # 4. 檢查版本（如啟用）
        if self.check_version:
            version_ok, version_msg = self.check_version_consistency(metadata)
            if not version_ok:
                self.errors.append(f"FAIL: {filename} — {version_msg}")
                return False

        # 5. 檢查相關資源 section
        if not self.has_related_resources_section(content):
            self.errors.append(
                i18n_text(
                    f"FAIL: {filename} — 缺少相關資源 section（應包含 ## 相關資源 或 ## Related Resources）",
                    f"FAIL: {filename} — missing Related Resources section (should have ## 相關資源 or ## Related Resources)"
                )
            )
            return False

        # 6. 檢查 version 欄位（建議但非必須）
        if 'version' not in metadata:
            self.warnings.append(
                i18n_text(
                    f"WARN: {filename} — 缺少 frontmatter 中的 version 欄位（建議補充）",
                    f"WARN: {filename} — missing version in frontmatter (recommended)"
                )
            )

        # 通過所有檢查
        self.passes.append(f"PASS: {filename}")
        return True

    def check_all_files(self) -> bool:
        """檢查所有 Markdown 文件。返回 True 表示無 errors。"""
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

        return len(self.errors) == 0

    def auto_fix_missing_related_resources(self, file_path: Path) -> bool:
        """自動補充缺失的相關資源 section。"""
        try:
            content = file_path.read_text(encoding='utf-8')
        except Exception as e:
            print(
                i18n_text(
                    f"無法讀取 {file_path}：{e}",
                    f"cannot read {file_path}: {e}"
                ),
                file=sys.stderr
            )
            return False

        # 檢查是否已有相關資源 section
        if self.has_related_resources_section(content):
            return True

        # 補充相關資源 section
        skeleton = i18n_text(
            """

## 相關資源

| 資源 | 說明 |
|------|------|
| <!-- TODO: 補充相關文件連結 --> | |
""",
            """

## Related Resources

| Resource | Description |
|----------|-------------|
| <!-- TODO: Add related documentation links --> | |
"""
        )

        new_content = content + skeleton

        try:
            write_text_secure(str(file_path), new_content)
            return True
        except Exception as e:
            print(
                i18n_text(
                    f"無法寫入 {file_path}：{e}",
                    f"cannot write to {file_path}: {e}"
                ),
                file=sys.stderr
            )
            return False

    def fix_all_files(self) -> bool:
        """自動修復所有缺失的相關資源 section。"""
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
        fixed_count = 0

        for file_path in md_files:
            if self.matches_exclude_pattern(file_path):
                continue

            if self.auto_fix_missing_related_resources(file_path):
                relative_path = file_path.relative_to(self.docs_dir)
                print(
                    i18n_text(
                        f"已修復：{relative_path}",
                        f"fixed: {relative_path}"
                    )
                )
                fixed_count += 1

        print(
            i18n_text(
                f"共修復 {fixed_count} 個文件。",
                f"fixed {fixed_count} files."
            )
        )
        return True

    def print_results(self) -> None:
        """輸出檢查結果。"""
        for msg in self.passes:
            print(msg)

        for msg in self.warnings:
            print(msg, file=sys.stderr)

        for msg in self.errors:
            print(msg, file=sys.stderr)

        # 摘要
        if self.errors:
            print(
                i18n_text(
                    f"\n總結：{len(self.errors)} 個錯誤，{len(self.warnings)} 個警告。",
                    f"\nsummary: {len(self.errors)} errors, {len(self.warnings)} warnings."
                ),
                file=sys.stderr
            )
        else:
            print(
                i18n_text(
                    f"\n✓ 所有文件符合規範。",
                    f"\n✓ all files passed checks."
                )
            )


def main():
    """CLI 入口點。"""
    parser = argparse.ArgumentParser(
        description=i18n_text(
            "檢查 Markdown 文件是否符合 doc-template 規範",
            "check markdown files against doc-template standards"
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
        "--version",
        help=i18n_text(
            "預期版本字符串（例：v2.3.0）",
            "expected version string (e.g., v2.3.0)"
        )
    )

    parser.add_argument(
        "--check-version",
        action="store_true",
        help=i18n_text(
            "檢查 frontmatter 版本是否與 --version 一致",
            "check if frontmatter version matches --version"
        )
    )

    parser.add_argument(
        "--exclude",
        help=i18n_text(
            "排除的 glob 模式（逗號分隔，預設：internal/*,assets/*,...）",
            "exclude glob patterns (comma-separated)"
        )
    )

    parser.add_argument(
        "--fix",
        action="store_true",
        help=i18n_text(
            "自動補充缺失的相關資源 section",
            "auto-add missing Related Resources section"
        )
    )

    args = parser.parse_args()

    # 解析排除模式
    exclude_patterns = None
    if args.exclude:
        exclude_patterns = [p.strip() for p in args.exclude.split(',')]

    # 建立檢查器
    checker = DocTemplateChecker(
        docs_dir=args.docs_dir,
        version=args.version,
        check_version=args.check_version,
        exclude_patterns=exclude_patterns
    )

    # 執行 fix 或 check
    if args.fix:
        success = checker.fix_all_files()
    else:
        success = checker.check_all_files()
        checker.print_results()

    # 設定退出碼：有錯誤則返回 1
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
