#!/usr/bin/env python3
"""validate_mermaid.py — Mermaid 圖渲染驗證

掃描 Markdown 文件中的 Mermaid 區塊，進行語法檢查和可選的渲染驗證。

用法:
  python3 scripts/tools/validate_mermaid.py                    # 基本語法檢查
  python3 scripts/tools/validate_mermaid.py --render            # 使用 mmdc 完整渲染
  python3 scripts/tools/validate_mermaid.py --ci                # CI 模式
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Tuple, Dict, Optional


class MermaidValidator:
    """Mermaid 圖表驗證器"""

    DIAGRAM_TYPES = {
        'graph', 'flowchart', 'sequenceDiagram', 'classDiagram', 'stateDiagram',
        'erDiagram', 'journey', 'gantt', 'pie', 'gitGraph', 'timeline'
    }

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.errors: List[Dict] = []
        self.total_diagrams = 0
        self.valid_diagrams = 0

    def validate_file(self, filepath: Path) -> List[Dict]:
        """驗證單個檔案中的所有 Mermaid 區塊"""
        file_errors = []

        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
        except OSError as e:
            error = {
                'file': str(filepath),
                'line': 0,
                'diagram_type': 'unknown',
                'status': 'error',
                'message': f'Failed to read file: {e}'
            }
            self.errors.append(error)
            file_errors.append(error)
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

    def render_with_mmdc(self, filepath: Path) -> bool:
        """使用 mmdc 進行完整渲染驗證"""
        try:
            # Check if mmdc is available
            subprocess.run(
                ['mmdc', '--version'],
                capture_output=True,
                check=False,
                timeout=5
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            if self.verbose:
                print("Warning: mmdc not found, skipping render validation")
            return True

        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
        except OSError as e:
            print(f"Error reading {filepath}: {e}")
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
        try:
            with tempfile.NamedTemporaryFile(
                mode='w',
                suffix='.mmd',
                delete=False,
                encoding='utf-8'
            ) as tmp:
                tmp.write(content)
                tmp_path = tmp.name

            try:
                # Run mmdc with output to temp PNG (just validation, not stored)
                with tempfile.NamedTemporaryFile(suffix='.png', delete=True) as out:
                    result = subprocess.run(
                        ['mmdc', '-i', tmp_path, '-o', out.name],
                        capture_output=True,
                        text=True,
                        timeout=10,
                        encoding='utf-8'
                    )

                    if result.returncode != 0:
                        error = {
                            'file': str(filepath),
                            'line': line_number,
                            'diagram_type': 'unknown',
                            'status': 'error',
                            'message': f'mmdc render failed: {result.stderr}'
                        }
                        self.errors.append(error)
                        return False

                    if self.verbose:
                        print(f"✓ mmdc render: {filepath}:{line_number}")
                    return True

            finally:
                os.unlink(tmp_path)

        except subprocess.TimeoutExpired:
            error = {
                'file': str(filepath),
                'line': line_number,
                'diagram_type': 'unknown',
                'status': 'error',
                'message': 'mmdc render timeout'
            }
            self.errors.append(error)
            return False
        except (ValueError, TypeError, IndexError) as e:
            error = {
                'file': str(filepath),
                'line': line_number,
                'diagram_type': 'unknown',
                'status': 'error',
                'message': f'mmdc render error: {e}'
            }
            self.errors.append(error)
            return False

    def print_summary(self):
        """列印驗證摘要"""
        print("\n" + "=" * 70)
        print("Mermaid Validation Summary")
        print("=" * 70)
        print(f"Total diagrams found: {self.total_diagrams}")
        print(f"Valid diagrams: {self.valid_diagrams}")
        print(f"Errors found: {len(self.errors)}")

        if self.errors:
            print("\n" + "-" * 70)
            print("Errors:")
            print("-" * 70)
            for error in self.errors:
                print(f"\n{error['file']}:{error['line']}")
                print(f"  Type: {error['diagram_type']}")
                print(f"  Status: {error['status']}")
                print(f"  Message: {error['message']}")


def main():
    """CLI entry point: Mermaid 圖渲染驗證."""
    parser = argparse.ArgumentParser(
        description='Validate Mermaid diagrams in Markdown files'
    )
    parser.add_argument(
        'path',
        nargs='?',
        default='.',
        help='Path to scan for Markdown files (default: current directory)'
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

    # Find all Markdown files
    scan_path = Path(args.path)
    if not scan_path.exists():
        print(f"Error: Path does not exist: {args.path}", file=sys.stderr)
        sys.exit(1)

    if scan_path.is_file():
        md_files = [scan_path] if scan_path.suffix == '.md' else []
    else:
        md_files = sorted(scan_path.glob('**/*.md'))

    if not md_files:
        print(f"No Markdown files found in {args.path}")
        sys.exit(0)

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

    # Exit with appropriate code
    if args.ci and validator.errors:
        sys.exit(1)

    sys.exit(0)


if __name__ == '__main__':
    main()
