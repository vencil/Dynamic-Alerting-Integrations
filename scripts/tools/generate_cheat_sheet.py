#!/usr/bin/env python3
"""
generate_cheat_sheet.py — Auto-generate da-tools cheat sheet from CLI reference.

Parses docs/cli-reference.md to extract subcommands, descriptions, and key flags,
then generates a concise cheat sheet at docs/cheat-sheet.md.

Usage:
    python3 generate_cheat_sheet.py [--check] [--output PATH]

Options:
    --check          Exit 1 if output differs from existing file (CI drift detection)
    --output PATH    Write to custom location (default: docs/cheat-sheet.md)
"""

import os
import sys
import argparse
import re
from pathlib import Path
from typing import List, Tuple, Optional


def read_cli_reference(ref_path: str) -> str:
    """Read the CLI reference file."""
    with open(ref_path, 'r', encoding='utf-8') as f:
        return f.read()


def extract_commands(content: str) -> List[dict]:
    """
    Extract command definitions from cli-reference.md.

    Looks for #### heading level (command names) and extracts:
    - Command name (from heading)
    - One-line description (first sentence after heading)
    - Key flags/options (most common 2-3 flags per command)
    """
    commands = []

    # Split by #### (command sections)
    sections = re.split(r'^#### ', content, flags=re.MULTILINE)

    # Skip first empty element
    for section in sections[1:]:
        lines = section.split('\n')
        if not lines:
            continue

        # First line is command name
        cmd_name = lines[0].strip()

        # Filter out non-command sections (Docker templates, code blocks, etc.)
        # Valid command names are kebab-case without spaces
        if not re.match(r'^[a-z][\w-]*$', cmd_name):
            continue

        # Look for description in first 50 lines
        description = ""
        for i, line in enumerate(lines[1:50]):
            line = line.strip()
            # Skip empty lines, headings, code blocks
            if line and not line.startswith('#') and not line.startswith('```'):
                # Take first sentence only
                match = re.match(r'^([^。.!?]+[。.!?]?)', line)
                if match:
                    description = match.group(1).strip()
                    if description.endswith(('。', '.')):
                        description = description[:-1]
                    break

        # Extract key flags from options table
        # Look for "| 選項 |" or "| Option |" table
        key_flags = extract_key_flags(section)

        if cmd_name and description:
            commands.append({
                'name': cmd_name,
                'description': description,
                'flags': key_flags,
            })

    return commands


def extract_key_flags(section: str) -> str:
    """Extract the most common 2-3 flags from a command section."""
    flags = []

    # Look for flag patterns like --option or -o
    flag_pattern = r'^\s*\|\s*`(-[-\w]+(?:\s+<\w+>)?)`'

    for line in section.split('\n'):
        match = re.search(flag_pattern, line)
        if match:
            flag = match.group(1)
            # Skip generic flags (help, version, etc.)
            if flag not in ['--help', '--version']:
                flags.append(flag)
                if len(flags) >= 3:
                    break

    return ', '.join(flags) if flags else ''


def create_cheat_sheet_content(commands: List[dict], version: str) -> str:
    """Generate the cheat sheet markdown content."""

    # Build table rows
    rows = []
    for cmd in commands:
        # Command name (code formatted)
        cmd_col = f'`{cmd["name"]}`'

        # Description (first 50 chars)
        desc_col = cmd['description'][:50]
        if len(cmd['description']) > 50:
            desc_col += '...'

        # Key flags
        flags_col = cmd['flags'] if cmd['flags'] else '-'

        # Example (basic)
        example_col = f'`da-tools {cmd["name"]} --help`'

        rows.append(f'| {cmd_col} | {desc_col} | {flags_col} | {example_col} |')

    # Build header
    header = f'''---
title: "da-tools Quick Reference"
tags: [reference, cli, cheat-sheet]
audience: [all]
version: {version}
lang: zh
---

# da-tools 快速參考

> **Language / 語言：** [English](cheat-sheet.en.md) | **中文（當前）**

da-tools 命令速查表。完整文件見 [cli-reference.md](cli-reference.md)。

## 命令速查

| 命令 | 說明 | 常用 Flag | 範例 |
|------|------|----------|------|
'''

    table = '\n'.join(rows)

    footer = '''

## 快速提示

- **Prometheus API 工具**：需要能連到 Prometheus HTTP API
  - `check-alert` — 查詢 alert 狀態
  - `diagnose` / `batch-diagnose` — Tenant 健康檢查
  - `baseline` — 觀測指標，產出閾值建議
  - `validate` — Shadow Monitoring 雙軌比對
  - `cutover` — 一鍵切換（遷移最後一步）
  - 其他：`blind-spot`、`maintenance-scheduler`、`backtest`

- **配置生成工具**
  - `generate-routes` — Tenant YAML → Alertmanager fragment
  - `patch-config` — ConfigMap 快速更新

- **檔案系統工具**（離線可用）
  - `scaffold` — 產生 tenant 配置
  - `migrate` — 規則格式轉換
  - `validate-config` — 配置驗證
  - `offboard` / `deprecate` — Tenant 下架／指標棄用
  - `lint` / `onboard` / `analyze-gaps` / `config-diff` — 治理工具

## 網路配置

```bash
# K8s 內部
export PROMETHEUS_URL=http://prometheus.monitoring.svc.cluster.local:9090

# Docker Desktop
export PROMETHEUS_URL=http://host.docker.internal:9090

# Linux Docker (--network=host)
export PROMETHEUS_URL=http://localhost:9090
```

## 常用樣板

```bash
# 基本命令
docker run --rm --network=host \\
  -e PROMETHEUS_URL=$PROMETHEUS_URL \\
  ghcr.io/vencil/da-tools:v1.13.0 \\
  <command> [arguments]

# 搭配本地檔案
docker run --rm --network=host \\
  -v $(pwd)/conf.d:/etc/config:ro \\
  -e PROMETHEUS_URL=$PROMETHEUS_URL \\
  ghcr.io/vencil/da-tools:v1.13.0 \\
  <command> --config-dir /etc/config
```

---

完整參考見 [cli-reference.md](cli-reference.md)。
'''

    return header + table + footer


def main():
    parser = argparse.ArgumentParser(
        description='Generate da-tools cheat sheet from cli-reference.md'
    )
    parser.add_argument(
        '--check',
        action='store_true',
        help='Exit 1 if output differs from existing file (CI drift detection)'
    )
    parser.add_argument(
        '--output',
        default='docs/cheat-sheet.md',
        help='Write to custom location (default: docs/cheat-sheet.md)'
    )

    args = parser.parse_args()

    # Determine base directory
    script_dir = Path(__file__).parent.parent.parent  # Navigate to repo root
    cli_ref_path = script_dir / 'docs' / 'cli-reference.md'
    output_path = script_dir / args.output

    # Verify input file exists
    if not cli_ref_path.exists():
        print(f'Error: {cli_ref_path} not found', file=sys.stderr)
        sys.exit(1)

    # Read and parse CLI reference
    content = read_cli_reference(str(cli_ref_path))
    commands = extract_commands(content)

    # Extract version from cli-reference frontmatter
    version_match = re.search(r'version: (v\d+\.\d+\.\d+)', content)
    version = version_match.group(1) if version_match else 'v1.13.0'

    if not commands:
        print('Error: No commands extracted from cli-reference.md', file=sys.stderr)
        sys.exit(1)

    # Generate cheat sheet content
    cheat_sheet = create_cheat_sheet_content(commands, version)

    # Handle --check mode
    if args.check:
        if output_path.exists():
            with open(output_path, 'r', encoding='utf-8') as f:
                existing = f.read()
            if existing == cheat_sheet:
                print(f'Cheat sheet is up-to-date: {output_path}')
                sys.exit(0)
            else:
                print(f'Cheat sheet is outdated: {output_path}', file=sys.stderr)
                sys.exit(1)
        else:
            print(f'Cheat sheet does not exist: {output_path}', file=sys.stderr)
            sys.exit(1)

    # Write output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(cheat_sheet)

    # Set restrictive permissions
    os.chmod(str(output_path), 0o600)

    print(f'Generated cheat sheet: {output_path}')
    print(f'Commands extracted: {len(commands)}')

    return 0


if __name__ == '__main__':
    sys.exit(main())
