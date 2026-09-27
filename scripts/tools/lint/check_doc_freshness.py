#!/usr/bin/env python3
"""文件新鮮度檢查工具。

掃描 docs/ 下的 Markdown 文件，檢測超過指定天數未更新的文件。
v2.5.0 新增。

功能：
- 掃描 docs/ 下所有 .md 文件
- 使用 git log 取得最後修改時間戳
- 標記超過閾值（預設 90 天）的陳舊文件
- `--check` 模式：若發現陳舊文件則 exit 1；沒有陳舊文件、但有量不到年齡的文件則 exit 2
- 量不到（unknown）的兩種來源：git 讀不到時間戳（不是 git repo、git 失敗或逾時），
  以及 shallow clone 裡最後一次修改落在 shallow 邊界 commit 上的文件——那顆 commit
  的時間只是 clone 的深度，不是文件真正的最後修改時間（#2023）
- 已在工作樹、但還沒有任何 commit 的新文件算新鮮（git 成功、只是沒有歷史）
- `--threshold DAYS` 標誌：覆蓋 90 天預設值
- `--verbose` 模式：顯示所有文件及其年齡
- `--exclude` 模式：逗號分隔的排除模式
- 預設排除：ADR 文件（穩定性）、CHANGELOG.md
- 輸出表格：文件路徑、最後修改日期、天數、狀態
- 雙語 CLI 輸出（使用 detect_cli_lang() 模式）
"""

import argparse
import sys
import json
import subprocess
import re
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Tuple, Optional, Dict, Set
import os

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_CALLER_ERROR, EXIT_OK, EXIT_VIOLATION  # noqa: E402

# Constant for ignore file name
IGNORE_FILE_NAME = ".docfreshness-ignore"

# 嘗試導入共用函式庫
try:
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from _lib_python import detect_cli_lang
except ImportError:
    # 如果無法導入，定義簡單的 fallback
    def detect_cli_lang() -> str:
        """Detect CLI language from environment."""
        import os
        lang = os.environ.get('LANG', 'en_US.UTF-8')
        return 'zh' if 'zh' in lang.lower() else 'en'


def i18n_text(zh: str, en: str) -> str:
    """Return localized text based on CLI language."""
    return zh if detect_cli_lang() == 'zh' else en


def _load_ignore_patterns(root_path: Path) -> Set[str]:
    """
    Load ignore patterns from .docfreshness-ignore file.

    Returns a set of patterns. Each pattern can be:
    - Generic: "conf.d/" (matches regardless of issue type)
    - Type-specific: "missing_file:conf.d/" (matches only for specific type)
    """
    ignore_file = root_path / IGNORE_FILE_NAME
    patterns = set()

    if not ignore_file.exists():
        return patterns

    try:
        content = ignore_file.read_text(encoding='utf-8')
        for line in content.splitlines():
            line = line.strip()
            # Skip comments and blank lines
            if not line or line.startswith('#'):
                continue
            patterns.add(line)
    except Exception:
        pass

    return patterns


def _is_ignored(issue: Dict, patterns: Set[str]) -> bool:
    """
    Check if an issue should be ignored based on patterns.

    Args:
        issue: Dict with 'reference' and 'type' keys
        patterns: Set of ignore patterns

    Returns:
        True if the issue matches any pattern
    """
    reference = issue.get('reference', '')
    issue_type = issue.get('type', '')

    for pattern in patterns:
        if ':' in pattern:
            # Type-specific pattern
            type_prefix, path_pattern = pattern.split(':', 1)
            if issue_type == type_prefix and reference.startswith(path_pattern):
                return True
        else:
            # Generic pattern
            if reference.startswith(pattern):
                return True

    return False


def extract_paths_from_markdown(markdown_text: str) -> Set[str]:
    """
    Extract file paths from markdown code blocks and inline code.

    Looks for patterns like:
    - `path/to/file` in inline code
    - ```
      key: path/to/file
      ```
    """
    paths = set()

    # Pattern for paths with common file extensions or directory indicators
    # Matches: path/to/file.ext or path/to/dir/
    path_pattern = r'(?:^|\s)([a-zA-Z0-9._\-/]+(?:\.[a-zA-Z0-9]+|/))'

    # Extract from inline code and code blocks
    # Match `...path...` or lines starting with path: or similar
    inline_pattern = r'`([a-zA-Z0-9._\-/]+(?:\.[a-zA-Z0-9]+)?)`'

    for match in re.finditer(inline_pattern, markdown_text):
        candidate = match.group(1)
        if '/' in candidate:  # Must look like a path
            paths.add(candidate)

    # Also extract from yaml-like lines
    yaml_pattern = r'(?:path|file):\s*([a-zA-Z0-9._\-/]+(?:\.[a-zA-Z0-9]+)?)'
    for match in re.finditer(yaml_pattern, markdown_text):
        candidate = match.group(1)
        if '/' in candidate:
            paths.add(candidate)

    return paths


_NON_COMMAND_WORDS = {
    'command', 'commands', 'tool', 'tools', 'image', 'images',
    'plugin', 'plugins', 'script', 'scripts', 'module', 'modules',
    'package', 'packages', 'library', 'libraries'
}


def extract_da_tools_commands(markdown_text: str) -> Set[str]:
    """
    Extract da-tools commands from markdown.

    Looks for patterns like:
    - `da-tools <cmd>`
    - `da-tools <cmd> --flag`
    """
    commands = set()

    # Match da-tools commands in code blocks and inline code
    pattern = r'da-tools\s+([a-z][a-z0-9-]*)'

    for match in re.finditer(pattern, markdown_text):
        cmd = match.group(1)
        if cmd not in _NON_COMMAND_WORDS:
            commands.add(cmd)

    return commands


def extract_docker_images(markdown_text: str) -> Dict[str, Tuple[str, str]]:
    """
    Extract Docker image references from markdown.

    Returns dict mapping "image-name:tag" -> (image_name, tag)

    Looks for patterns like:
    - ghcr.io/vencil/threshold-exporter:v1.0.0
    - ghcr.io/vencil/da-tools:v2.1.0
    - ghcr.io/vencil/da-portal:v2.1.0
    """
    images = {}

    # Match Docker image references
    pattern = r'ghcr\.io/vencil/([a-z-]+):([a-zA-Z0-9._\-]+)'

    for match in re.finditer(pattern, markdown_text):
        image_name = match.group(1)
        tag = match.group(2)
        key = f"{image_name}:{tag}"
        images[key] = (image_name, tag)

    return images


def file_exists(root_path: Path, file_path: str) -> bool:
    """Check if a file exists relative to root_path."""
    target = root_path / file_path
    return target.exists()


def collect_existing_tools(root_path: Path) -> Set[str]:
    """
    Collect all existing tool names.

    First tries to extract from docs/cli-reference.md (#### heading style).
    Falls back to scanning scripts/tools/ directory.
    """
    tools = set()

    # Try to extract from cli-reference.md
    cli_ref = root_path / "docs" / "cli-reference.md"
    if cli_ref.exists():
        try:
            content = cli_ref.read_text(encoding='utf-8')
            # Match #### command-name
            for match in re.finditer(r'^####\s+([a-z][a-z0-9-]*)', content, re.MULTILINE):
                tools.add(match.group(1))
            if tools:
                return tools
        except Exception:
            pass

    # Fallback: scan scripts/tools/
    tools_dir = root_path / "scripts" / "tools"
    if tools_dir.exists():
        for py_file in tools_dir.glob("*.py"):
            name = py_file.stem
            # Skip internal modules
            if name.startswith('_lib'):
                continue
            # Convert snake_case to kebab-case
            kebab_name = name.replace('_', '-')
            tools.add(kebab_name)
            tools.add(name)  # Also include original name

    return tools


def extract_chart_version(root_path: Path, component: str) -> Optional[str]:
    """
    Extract version from components/{component}/Chart.yaml.

    Returns the version string or None.
    """
    chart_file = root_path / "components" / component / "Chart.yaml"
    if not chart_file.exists():
        return None

    try:
        content = chart_file.read_text(encoding='utf-8')
        match = re.search(r'^version:\s*([a-zA-Z0-9._\-]+)', content, re.MULTILINE)
        if match:
            return match.group(1)
    except Exception:
        pass

    return None


def check_doc_file(
    md_file: Path,
    root_path: Path,
    version: str,
    existing_tools: Set[str],
    issues: List[Dict]
) -> None:
    """
    Check a single markdown file for documentation issues.

    Detects:
    - Missing file references
    - Missing da-tools commands
    - Docker image version mismatches

    Appends issues to the issues list.
    """
    try:
        content = md_file.read_text(encoding='utf-8')
    except Exception:
        return

    # Extract and check file paths
    paths = extract_paths_from_markdown(content)
    for path in paths:
        if not file_exists(root_path, path):
            issues.append({
                'type': 'missing_file',
                'reference': path,
                'file': str(md_file.relative_to(root_path)) if md_file.is_relative_to(root_path) else str(md_file),
            })

    # Extract and check da-tools commands
    commands = extract_da_tools_commands(content)
    for cmd in commands:
        if cmd not in existing_tools:
            issues.append({
                'type': 'missing_command',
                'reference': cmd,
                'file': str(md_file.relative_to(root_path)) if md_file.is_relative_to(root_path) else str(md_file),
            })

    # Extract and check Docker image versions
    images = extract_docker_images(content)
    for image_key, (image_name, tag) in images.items():
        expected_version = extract_chart_version(root_path, image_name)
        if expected_version and tag != f"v{expected_version}":
            issues.append({
                'type': 'version_mismatch',
                'reference': image_key,
                'file': str(md_file.relative_to(root_path)) if md_file.is_relative_to(root_path) else str(md_file),
            })


_UNKNOWN = object()  # git could not answer: not a repo, failed, or timed out


def get_git_last_commit(file_path: Path):
    """
    取得文件最後一次修改的 commit（full SHA）與 committer 時間戳（unix seconds）。

    Returns:
        (sha, timestamp)；文件還沒有任何 commit 時為 (None, None)；
        git 無法回答（不是 git repo、失敗或逾時）時為 _UNKNOWN。
    """
    try:
        result = subprocess.run(
            ['git', 'log', '-1', '--format=%H %ct', '--', str(file_path)],
            capture_output=True,
            text=True,
            timeout=5
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return _UNKNOWN
    if result.returncode != 0:
        return _UNKNOWN
    out = result.stdout.strip()
    if not out:
        return (None, None)
    try:
        sha, ts = out.split()
        return (sha, int(ts))
    except ValueError:
        return _UNKNOWN


def get_shallow_boundaries() -> Set[str]:
    """shallow clone 的邊界 commit（`.git/shallow` 列出的 SHA）；非 shallow 時為空集合。

    邊界 commit 在 shallow clone 裡沒有父 commit，於是 `git log -1 -- <file>`
    對「最後修改在邊界之外」的文件回的就是邊界 commit，時間只反映 clone 深度。
    讀不到 shallow 檔以外的 git 錯誤不在這裡判斷：那種情況每個文件的
    `git log` 也會失敗，逐檔就會變成 unknown。
    """
    try:
        result = subprocess.run(
            ['git', 'rev-parse', '--git-path', 'shallow'],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return set()
    if result.returncode != 0:
        return set()
    shallow = Path(result.stdout.strip())
    try:
        return set(shallow.read_text(encoding='utf-8').split())
    except FileNotFoundError:
        return set()


def calculate_days_since_update(timestamp: int) -> float:
    """計算從 unix timestamp 至今的天數。"""
    now = datetime.now(timezone.utc).timestamp()
    return (now - timestamp) / (24 * 3600)


class DocFreshnessChecker:
    """檢查 Markdown 文件新鮮度的檢查器。"""

    def __init__(
        self,
        docs_dir: str = "docs",
        threshold_days: int = 90,
        exclude_patterns: Optional[List[str]] = None,
        verbose: bool = False,
    ):
        """
        初始化檢查器。

        Args:
            docs_dir: 文檔根目錄
            threshold_days: 標記陳舊的天數閾值
            exclude_patterns: 排除的 glob 模式列表
            verbose: 是否顯示所有文件及其年齡
        """
        self.docs_dir = Path(docs_dir)
        self.threshold_days = threshold_days
        self.verbose = verbose
        # 預設排除：ADR、CHANGELOG
        self.exclude_patterns = exclude_patterns or [
            "adr/*",
            "CHANGELOG.md",
        ]
        self.results: List[Dict] = []
        self.errors: List[str] = []

    def matches_exclude_pattern(self, file_path: Path) -> bool:
        """檢查文件是否符合排除模式。"""
        relative_path = str(file_path.relative_to(self.docs_dir))
        for pattern in self.exclude_patterns:
            if Path(relative_path).match(pattern):
                return True
        return False

    def check_freshness(self) -> Tuple[bool, List[Dict]]:
        """
        掃描所有 .md 文件並檢查新鮮度。

        Returns:
            (all_fresh: bool, results: List[Dict])
            results 包含每個文件的檢查結果
        """
        md_files = sorted(self.docs_dir.glob('**/*.md'))

        if not md_files:
            self.errors.append(
                i18n_text(
                    f"警告：在 {self.docs_dir} 中未找到任何 .md 文件",
                    f"Warning: no .md files found in {self.docs_dir}"
                )
            )
            return True, []

        all_fresh = True
        boundaries = get_shallow_boundaries()

        for file_path in md_files:
            relative_path = file_path.relative_to(self.docs_dir)

            # 檢查是否排除
            if self.matches_exclude_pattern(file_path):
                continue

            # 取得 git 最後修改的 commit 與時間戳
            commit = get_git_last_commit(file_path)
            unknown_reason = None
            if commit is _UNKNOWN:
                unknown_reason = 'git'
                timestamp = None
            elif commit[0] is None:
                # 還沒有任何 commit：正在新增的文件，年齡為 0
                timestamp = int(datetime.now(timezone.utc).timestamp())
            elif commit[0] in boundaries:
                unknown_reason = 'shallow'
                timestamp = None
            else:
                timestamp = commit[1]

            if unknown_reason is not None:
                # 量不到：不可併入 fresh（#2023）
                status = 'unknown'
                days_since = None
                is_stale = False
            else:
                days_since = calculate_days_since_update(timestamp)
                is_stale = days_since > self.threshold_days
                status = 'stale' if is_stale else 'fresh'

                if is_stale:
                    all_fresh = False

            result = {
                'file': str(relative_path),
                'status': status,
                'unknown_reason': unknown_reason,
                'days_since': days_since,
                'timestamp': timestamp,
            }
            self.results.append(result)

            # verbose 模式下顯示所有文件
            if self.verbose:
                self._print_file_result(result)

        return all_fresh, self.results

    def _print_file_result(self, result: Dict) -> None:
        """打印單一文件檢查結果。"""
        file = result['file']
        status = result['status']
        days_since = result['days_since']

        if status == 'unknown':
            print(f"  {file:60s} | {i18n_text('未知', 'unknown'):8s}")
        else:
            status_symbol = '✓' if status == 'fresh' else '✗'
            status_label = i18n_text('新鮮', 'fresh') if status == 'fresh' else i18n_text('陳舊', 'stale')
            days_str = f"{days_since:.1f}d"
            print(f"  {file:60s} | {days_str:>8s} | {status_label:8s} {status_symbol}")

    def report(self) -> str:
        """生成檢查結果報告。"""
        lines = []
        stale_count = sum(1 for r in self.results if r['status'] == 'stale')
        fresh_count = sum(1 for r in self.results if r['status'] == 'fresh')
        unknown_count = sum(1 for r in self.results if r['status'] == 'unknown')

        lines.append("")
        lines.append(i18n_text("文件新鮮度檢查報告", "Document Freshness Report"))
        lines.append("-" * 80)
        lines.append(
            i18n_text(
                f"閾值：{self.threshold_days} 天 | 新鮮: {fresh_count} | 陳舊: {stale_count} | 未知: {unknown_count}",
                f"Threshold: {self.threshold_days} days | Fresh: {fresh_count} | Stale: {stale_count} | Unknown: {unknown_count}"
            )
        )
        lines.append("")

        if stale_count > 0:
            lines.append(i18n_text("陳舊文件（需要更新）：", "Stale Files (need update):"))
            lines.append("")
            lines.append(f"{'File':<60} | {'Days':<8} | {'Status':<8}")
            lines.append("-" * 80)
            for result in self.results:
                if result['status'] == 'stale':
                    self._print_file_result(result)
            lines.append("")

        if self.verbose and (fresh_count > 0 or unknown_count > 0):
            lines.append(i18n_text("新鮮文件：", "Fresh Files:"))
            lines.append("")
            lines.append(f"{'File':<60} | {'Days':<8} | {'Status':<8}")
            lines.append("-" * 80)
            for result in self.results:
                if result['status'] in ('fresh', 'unknown'):
                    self._print_file_result(result)
            lines.append("")

        for error in self.errors:
            lines.append(f"⚠️  {error}")
            lines.append("")

        return '\n'.join(lines)


def main():
    """主程式入口。"""
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description=i18n_text(
            "檢測文件新鮮度（距上次 git commit 的天數）",
            "Detect document freshness (days since last git commit)"
        )
    )
    parser.add_argument(
        '--docs-dir',
        default='docs',
        help=i18n_text(
            "文檔根目錄（預設：docs）",
            "Documentation root directory (default: docs)"
        )
    )
    parser.add_argument(
        '--threshold',
        type=int,
        default=90,
        help=i18n_text(
            "標記陳舊的天數閾值（預設：90）",
            "Days threshold to mark as stale (default: 90)"
        )
    )
    parser.add_argument(
        '--exclude',
        type=str,
        default='adr/*,CHANGELOG.md',
        help=i18n_text(
            "逗號分隔的排除模式（預設：adr/*,CHANGELOG.md）",
            "Comma-separated exclude patterns (default: adr/*,CHANGELOG.md)"
        )
    )
    parser.add_argument(
        '--verbose',
        action='store_true',
        help=i18n_text(
            "顯示所有文件及其年齡",
            "Show all files with their ages"
        )
    )
    parser.add_argument(
        '--check',
        action='store_true',
        help=i18n_text(
            "檢查模式：有陳舊文件 exit 1；沒有陳舊但有年齡量不到的文件 exit 2",
            "Check mode: exit 1 if stale files found; else exit 2 if any file's age cannot be measured"
        )
    )
    parser.add_argument(
        '--json',
        action='store_true',
        help=i18n_text(
            "輸出 JSON 格式結果",
            "Output results in JSON format"
        )
    )

    args = parser.parse_args()

    exclude_patterns = [p.strip() for p in args.exclude.split(',') if p.strip()]

    checker = DocFreshnessChecker(
        docs_dir=args.docs_dir,
        threshold_days=args.threshold,
        exclude_patterns=exclude_patterns,
        verbose=args.verbose,
    )

    all_fresh, results = checker.check_freshness()

    if args.json:
        output = {
            'all_fresh': all_fresh,
            'threshold_days': args.threshold,
            'results': results,
            'summary': {
                'fresh': sum(1 for r in results if r['status'] == 'fresh'),
                'stale': sum(1 for r in results if r['status'] == 'stale'),
                'unknown': sum(1 for r in results if r['status'] == 'unknown'),
            }
        }
        print(json.dumps(output, indent=2))
    else:
        print(checker.report())

    if args.check and not all_fresh:
        sys.exit(EXIT_VIOLATION)

    unknown = [r for r in results if r['status'] == 'unknown']
    if args.check and unknown:
        # 量不到 ≠ 量了沒事：沒有陳舊文件時，只要有文件年齡未知就不能回 0（#2023）
        print(i18n_text(
            f"無法判定 {len(unknown)} 份文件的新鮮度：",
            f"cannot determine freshness of {len(unknown)} file(s):"), file=sys.stderr)
        for r in unknown:
            why = (i18n_text("最後修改落在 shallow clone 邊界，請先 git fetch --unshallow",
                             "last change is at the shallow-clone boundary; run git fetch --unshallow")
                   if r['unknown_reason'] == 'shallow'
                   else i18n_text("git 讀不到時間戳", "git could not read a timestamp"))
            print(f"  {r['file']}: {why}", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    sys.exit(EXIT_OK)


if __name__ == '__main__':
    main()
