#!/usr/bin/env python3
"""validate_mermaid.py — Mermaid 圖渲染驗證

掃描 Markdown 文件中的 Mermaid 區塊，進行語法檢查和可選的渲染驗證。

用法（路徑可給零個到多個；不給就掃當前目錄）:
  python3 scripts/tools/lint/validate_mermaid.py docs/                 # 基本語法檢查
  python3 scripts/tools/lint/validate_mermaid.py docs/ rule-packs/     # 一次掃兩棵樹（#1702）
  python3 scripts/tools/lint/validate_mermaid.py docs/ --render        # 使用 mmdc 完整渲染
  python3 scripts/tools/lint/validate_mermaid.py docs/ --ci            # CI 模式

⛔ 沒有 `--ci` 時**圖的錯誤**不會讓 rc 非零（caller error 仍會 exit 2）。接進閘門的
呼叫端一律要傳 `--ci`，否則那一格只是裝飾（#1702）。
"""

import argparse
import atexit
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import List, Tuple, Dict, Optional

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
import _lib_engine  # noqa: E402

# Puppeteer config required for mmdc to launch headless Chrome inside
# sandboxed CI runners (GitHub Actions, containers, WSL, etc.). Without
# --no-sandbox, mmdc fails with "Failed to launch the browser process!"
# whenever the runner cannot grant the default Chrome sandbox capabilities.
# The config is materialised lazily on first render via
# MermaidValidator._ensure_puppeteer_config and cleaned up at process exit.
_PUPPETEER_CONFIG_JSON = (
    '{"args": ["--no-sandbox", "--disable-setuid-sandbox"]}'
)

# ⛔ #3608/#3611: mermaid-cli exits 1 both for a diagram it cannot parse and
# for a renderer (Chrome / puppeteer) that cannot start — measured on 12.2.0,
# "Parse error on line 3" and "Browser was not found" are both rc 1. Its
# stderr is not read to tell them apart (#3611 Q2). This known-good diagram
# is: rendered first, it proves the renderer works before any diagram is
# judged; rendered again after a diagram fails, it tells "this diagram is
# wrong" (canary still renders) from "the renderer stopped" (it does not).
_CANARY_DIAGRAM = "graph TD\n  A-->B\n"


class MermaidValidator:
    """Mermaid 圖表驗證器"""

    DIAGRAM_TYPES = {
        'graph', 'flowchart', 'sequenceDiagram', 'classDiagram', 'stateDiagram',
        'erDiagram', 'journey', 'gantt', 'pie', 'gitGraph', 'timeline'
    }

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.errors: List[Dict] = []
        # What could not be checked (a file unreadable, mmdc timing out or
        # failing internally) — no verdict, so main() exits the contract's 2
        # rather than reporting it as a diagram error (#3577, #3611).
        self.not_checked: List[str] = []
        self.total_diagrams = 0
        self.valid_diagrams = 0
        # Lazily materialised on first render call.
        self._puppeteer_config_path: Optional[str] = None
        # The renderer, once located and proven by the canary; False once it
        # is known not to work (reported once, nothing more is rendered).
        self._mmdc: "Optional[_lib_engine.Located] | bool" = None

    def _ensure_puppeteer_config(self) -> str:
        """Materialise the puppeteer config file once per instance.

        Returns the path to a JSON file that tells mmdc to launch
        headless Chrome with --no-sandbox / --disable-setuid-sandbox.
        Required on GitHub Actions runners and most container/sandbox
        environments where Chrome cannot grant itself a user namespace.

        The temp file is registered for cleanup at interpreter exit.
        """
        if self._puppeteer_config_path is not None:
            return self._puppeteer_config_path

        fd, path = tempfile.mkstemp(
            prefix='mmdc-puppeteer-', suffix='.json'
        )
        try:
            with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
                f.write(_PUPPETEER_CONFIG_JSON)
        except Exception:
            try:
                os.unlink(path)
            except OSError:
                pass
            raise

        # Best-effort cleanup at exit; ignore failures (file may be gone
        # already if /tmp was wiped between calls).
        def _cleanup(p: str = path) -> None:
            try:
                os.unlink(p)
            except OSError:
                pass

        atexit.register(_cleanup)
        self._puppeteer_config_path = path
        return path

    def validate_file(self, filepath: Path) -> List[Dict]:
        """驗證單個檔案中的所有 Mermaid 區塊"""
        file_errors = []

        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
        except (OSError, ValueError) as e:
            self._not_checked(f'{filepath}: failed to read file: {e}')
            return file_errors

        # Extract Mermaid blocks with line numbers
        lines = content.split('\n')
        i = 0
        while i < len(lines):
            line = lines[i]
            if line.strip().startswith('```mermaid'):
                # Found start of Mermaid block
                start_line = i + 1
                i += 1
                diagram_lines = []

                # Collect diagram content until closing ```
                while i < len(lines) and not lines[i].strip().startswith('```'):
                    diagram_lines.append(lines[i])
                    i += 1

                if i >= len(lines):
                    error = {
                        'file': str(filepath),
                        'line': start_line,
                        'diagram_type': 'unknown',
                        'status': 'error',
                        'message': 'Unclosed mermaid block'
                    }
                    self.errors.append(error)
                    file_errors.append(error)
                    break

                diagram_content = '\n'.join(diagram_lines).strip()
                if diagram_content:
                    self.total_diagrams += 1
                    error = self._validate_diagram(
                        diagram_content,
                        str(filepath),
                        start_line
                    )
                    if error:
                        file_errors.append(error)
                        self.errors.append(error)
                    else:
                        self.valid_diagrams += 1

            i += 1

        return file_errors

    def _validate_diagram(
        self,
        content: str,
        filepath: str,
        line_number: int
    ) -> Optional[Dict]:
        """驗證單個 Mermaid 區塊的語法"""

        # Detect diagram type from first non-empty line
        first_line = next(
            (line.strip() for line in content.split('\n') if line.strip()),
            ''
        )

        diagram_type = self._detect_diagram_type(first_line)

        # Run syntax checks
        syntax_errors = self._check_syntax(content, diagram_type)

        if syntax_errors:
            return {
                'file': filepath,
                'line': line_number,
                'diagram_type': diagram_type,
                'status': 'error',
                'message': '; '.join(syntax_errors)
            }

        if self.verbose:
            print(f"✓ {filepath}:{line_number} ({diagram_type})")

        return None

    def _detect_diagram_type(self, first_line: str) -> str:
        """檢測 Mermaid 圖表類型"""
        for dtype in self.DIAGRAM_TYPES:
            if dtype in first_line:
                return dtype
        return 'unknown'

    def _check_syntax(self, content: str, diagram_type: str) -> List[str]:
        """檢查基本語法錯誤"""
        errors = []

        # Check 1: Unmatched subgraph/end pairs (graph/flowchart types)
        if diagram_type in {'graph', 'flowchart'}:
            subgraph_count = len(re.findall(r'^\s*subgraph\b', content, re.MULTILINE))
            # Match standalone 'end' on its own line (not inside words like "send")
            end_count = len(re.findall(r'^\s*end\s*$', content, re.MULTILINE))
            if subgraph_count != end_count:
                errors.append(
                    f'Unmatched subgraph/end: {subgraph_count} subgraph(s) '
                    f'but {end_count} end(s)'
                )

        # Check 2: Unmatched quotes
        single_quotes = content.count("'") - content.count("\\'")
        double_quotes = content.count('"') - content.count('\\"')
        if single_quotes % 2 != 0:
            errors.append('Unmatched single quotes')
        if double_quotes % 2 != 0:
            errors.append('Unmatched double quotes')

        # Check 3: Unmatched brackets/braces in node definitions
        bracket_errors = self._check_brackets(content)
        errors.extend(bracket_errors)

        # Check 4: Duplicate node IDs (basic check)
        dup_errors = self._check_duplicate_ids(content)
        errors.extend(dup_errors)

        # Check 5: Invalid arrow syntax
        arrow_errors = self._check_arrow_syntax(content)
        errors.extend(arrow_errors)

        return errors

    def _check_brackets(self, content: str) -> List[str]:
        """檢查括號和大括號是否匹配"""
        errors = []
        brackets = {'(': ')', '[': ']', '{': '}'}
        stack = []

        for char in content:
            if char in brackets:
                stack.append(char)
            elif char in brackets.values():
                if not stack or brackets[stack.pop()] != char:
                    errors.append(f'Unmatched closing bracket: {char}')
                    break

        if stack:
            errors.append(f'Unmatched opening brackets: {", ".join(stack)}')

        return errors

    def _check_duplicate_ids(self, content: str) -> List[str]:
        """檢查重複的節點 ID"""
        errors = []

        # Simple regex to extract node IDs
        # Matches patterns like: id[text], id(text), id{text}, id-->other, etc.
        id_pattern = r'^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*[\[\(\{]'
        seen_ids = {}

        for i, line in enumerate(content.split('\n')):
            match = re.match(id_pattern, line.strip())
            if match:
                node_id = match.group(1)
                if node_id in seen_ids:
                    errors.append(
                        f'Duplicate node ID "{node_id}" '
                        f'(first at line {seen_ids[node_id]}, '
                        f'also at line {i + 1})'
                    )
                else:
                    seen_ids[node_id] = i + 1

        return errors

    def _check_arrow_syntax(self, content: str) -> List[str]:
        """檢查箭頭語法"""
        errors = []

        # Valid Mermaid arrow patterns include:
        #   -->, ==>, -.->             (standard arrows)
        #   -->|text|                  (labeled arrows)
        #   -- "text" -->              (labeled arrows, alternate syntax)
        #   ---                        (open link)
        # Invalid patterns: isolated `- >` with space (rare user typo)
        invalid_arrow_pattern = r'(?<!-)-\s+>'

        for i, line in enumerate(content.split('\n'), 1):
            stripped = line.strip()
            # Skip comments and empty lines
            if not stripped or stripped.startswith('%%'):
                continue
            if re.search(invalid_arrow_pattern, stripped):
                errors.append(f'Invalid arrow syntax at line {i}: {stripped}')

        return errors

    def _mmdc_once(self, src: str, timeout: int) -> "_lib_engine.Result":
        """Render *src* once with mmdc; Ran(True) rendered, Ran(False) exit 1."""
        with tempfile.TemporaryDirectory(prefix='mmdc-') as tmp:
            diagram = Path(tmp) / 'diagram.mmd'
            diagram.write_text(src, encoding='utf-8', newline='\n')
            out = Path(tmp) / 'out.png'
            return _lib_engine.run(
                self._mmdc, 'render',
                ['-p', self._ensure_puppeteer_config(), '-i', str(diagram), '-o', str(out)],
                timeout=timeout, expect={'output': str(out)})

    def _canary_renders(self) -> "Optional[str]":
        """None when the known-good diagram renders; else why it did not."""
        result = self._mmdc_once(_CANARY_DIAGRAM, timeout=60)
        if isinstance(result, _lib_engine.NoVerdict):
            return result.message()
        if not result.data:
            return ("mmdc exited 1 on a known-good diagram — the renderer "
                    "(Chrome / puppeteer) is not working:\n"
                    + (result.proc.stderr or '').strip()[-2000:])
        return None

    def _renderer(self) -> bool:
        """Locate mmdc and prove it renders, once. False (reported once) when
        --render was asked for and nothing can render (#3608)."""
        if self._mmdc is None:
            located = _lib_engine.locate('mmdc')
            if isinstance(located, _lib_engine.NoVerdict):
                self._mmdc = False
                self._not_checked(f'--render was requested but {located.reason}')
                return False
            self._mmdc = located
            why = self._canary_renders()
            if why is not None:
                self._mmdc = False
                self._not_checked(f'--render: {why}')
                return False
        return self._mmdc is not False

    def render_with_mmdc(self, filepath: Path) -> bool:
        """使用 mmdc 進行完整渲染驗證"""
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
        except (OSError, ValueError) as e:
            self._not_checked(f'{filepath}: failed to read file: {e}')
            return False

        # Extract and render each Mermaid block
        render_errors = False
        lines = content.split('\n')
        i = 0
        diagram_num = 0

        while i < len(lines):
            line = lines[i]
            if line.strip().startswith('```mermaid'):
                start_line = i + 1
                i += 1
                diagram_lines = []

                while i < len(lines) and not lines[i].strip().startswith('```'):
                    diagram_lines.append(lines[i])
                    i += 1

                diagram_content = '\n'.join(diagram_lines).strip()
                if diagram_content:
                    diagram_num += 1
                    # The renderer is located and proven on the first diagram
                    # there is to render, not per file: a file (or a tree)
                    # with no diagram needs no renderer (#3611 review F1).
                    if not self._renderer():
                        return False
                    if not self._render_diagram(diagram_content, filepath, start_line):
                        render_errors = True

            i += 1

        return not render_errors

    def _render_diagram(
        self,
        content: str,
        filepath: Path,
        line_number: int
    ) -> bool:
        """使用 mmdc 渲染單個圖表"""
        where = f'{filepath}:{line_number}'
        result = self._mmdc_once(content, timeout=30)
        if isinstance(result, _lib_engine.NoVerdict):
            # A hung or crashed renderer: if the canary no longer renders
            # either, stop — every later diagram would wait out its own
            # timeout for the same answer (#3649 review).
            self._no_verdict(where, result)
            return False
        if result.data:
            if self.verbose:
                print(f"✓ mmdc render: {where}")
            return True
        # Exit 1: this diagram, or the renderer? Only the canary can say.
        if self._stop_if_renderer_dead(where):
            return False
        # The renderer works now. A one-off launch failure on this diagram
        # (exit 1, same as a parse error) would read as a finding, so the
        # diagram must fail again with the renderer proven good in between
        # before it is one (#3649 review).
        again = self._mmdc_once(content, timeout=30)
        if isinstance(again, _lib_engine.NoVerdict):
            self._no_verdict(where, again)
            return False
        if again.data:
            if self.verbose:
                print(f"✓ mmdc render: {where} (second attempt)")
            return True
        self.errors.append({
            'file': str(filepath),
            'line': line_number,
            'diagram_type': 'unknown',
            'status': 'error',
            'message': f'mmdc render failed: {again.proc.stderr}',
        })
        return False

    def _no_verdict(self, where: str, result: "_lib_engine.NoVerdict") -> None:
        """One not_checked line for this diagram; if the canary fails too,
        the renderer is stopped and the line says so."""
        why = self._canary_renders()
        if why is not None:
            self._mmdc = False
            self._not_checked(f'{where}: {result.message()}; and {why}')
        else:
            self._not_checked(f'{where}: {result.message()}')

    def _stop_if_renderer_dead(self, where: str) -> bool:
        """Re-run the canary; if it fails, report once, stop rendering, and
        return True (#3611 review F2)."""
        why = self._canary_renders()
        if why is None:
            return False
        self._mmdc = False
        self._not_checked(f'{where}: not rendered, and {why}')
        return True

    def _not_checked(self, why: str) -> None:
        """Record (and say on stderr) one thing that could not be checked."""
        self.not_checked.append(why)
        print(f"ERROR: not checked: {why}", file=sys.stderr)

    def print_summary(self):
        """列印驗證摘要"""
        print("\n" + "=" * 70)
        print("Mermaid Validation Summary")
        print("=" * 70)
        print(f"Total diagrams found: {self.total_diagrams}")
        print(f"Valid diagrams: {self.valid_diagrams}")
        print(f"Errors found: {len(self.errors)}", file=sys.stderr)

        if self.errors:
            print("\n" + "-" * 70)
            print("Errors:", file=sys.stderr)
            print("-" * 70)
            for error in self.errors:
                print(f"\n{error['file']}:{error['line']}")
                print(f"  Type: {error['diagram_type']}")
                print(f"  Status: {error['status']}")
                print(f"  Message: {error['message']}")


def main():
    """CLI entry point: Mermaid 圖渲染驗證."""
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description='Validate Mermaid diagrams in Markdown files'
    )
    parser.add_argument(
        'paths',
        nargs='*',
        default=['.'],
        help='Paths to scan for Markdown files (default: current directory)'
    )
    parser.add_argument(
        '--render',
        action='store_true',
        help='Use mmdc for full rendering validation (requires mermaid-cli)'
    )
    parser.add_argument(
        '--ci',
        action='store_true',
        help='Exit with code 1 if errors found (for CI/CD pipelines)'
    )
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Print verbose output'
    )

    args = parser.parse_args()

    # Find all Markdown files under every requested path. Accepting more than
    # one is what lets a caller cover two disjoint trees in a single run;
    # docs-ci.yaml still passes one path per invocation and is unaffected.
    md_files = []
    seen = set()
    for raw_path in args.paths:
        scan_path = Path(raw_path)
        if not scan_path.exists():
            print(f"Error: Path does not exist: {raw_path}", file=sys.stderr)
            sys.exit(EXIT_CALLER_ERROR)

        candidates = ([scan_path] if scan_path.is_file()
                      else sorted(scan_path.glob('**/*.md')))
        for md_file in candidates:
            if md_file.suffix != '.md':
                continue
            # ⛔ Key is the path AS CONSTRUCTED, not `.resolve()`: resolving
            # also collapses a symlink onto its target, and docs/ holds three
            # of those — measured on Linux, the default `.` scan drops 332→329
            # under a resolving key and stays 332 under this one.
            if md_file in seen:
                continue
            seen.add(md_file)
            md_files.append(md_file)

    if not md_files:
        print(f"No Markdown files found in {', '.join(args.paths)}")
        sys.exit(EXIT_OK)

    # Validate
    validator = MermaidValidator(verbose=args.verbose)

    for md_file in md_files:
        if args.verbose:
            print(f"Checking {md_file}...")
        validator.validate_file(md_file)

        if args.render:
            validator.render_with_mmdc(md_file)

    # Print summary
    validator.print_summary()

    # Exit with appropriate code. Something not checked is no verdict — the
    # contract's 2, with or without --ci, and ahead of any error found (#3577).
    if validator.not_checked:
        print(f"ERROR: {len(validator.not_checked)} item(s) could not be "
              f"checked; see the lines above", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)
    if args.ci and validator.errors:
        sys.exit(EXIT_VIOLATION)

    sys.exit(EXIT_OK)


if __name__ == '__main__':
    import _lib_exitcodes
    _lib_exitcodes.run(main)
