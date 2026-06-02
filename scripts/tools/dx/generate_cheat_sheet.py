#!/usr/bin/env python3
"""
generate_cheat_sheet.py — Auto-generate da-tools cheat sheet from CLI reference.

Parses docs/cli-reference.md to extract subcommands, descriptions, and key flags,
then generates concise cheat sheets at docs/cheat-sheet.md and/or docs/cheat-sheet.en.md.

Usage:
    python3 generate_cheat_sheet.py [--check] [--lang zh|en|all]
    python3 generate_cheat_sheet.py --lang all          # 中英文

Options:
    --check          Exit 1 if output differs from existing file (CI drift detection)
    --lang LANG      Language: zh (default), en, or all
"""

import os
import re
import stat
import sys
import argparse
from pathlib import Path
from typing import List


# ---------------------------------------------------------------------------
# Repo root detection
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent.parent  # scripts/tools/dx/ -> repo root

sys.path.insert(0, str(SCRIPT_DIR.parent))
from _lib_versions import read_platform_version, read_da_tools_version  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402

# ---------------------------------------------------------------------------
# Bilingual templates
# ---------------------------------------------------------------------------
STRINGS = {
    "zh": {
        "title": "da-tools Quick Reference",
        "h1": "da-tools 快速參考",
        "lang_switcher": '> **Language / 語言：** [English](cheat-sheet.en.md) | **中文（當前）**',
        "intro": "da-tools 命令速查表。完整文件見 [cli-reference.md](cli-reference.md)。",
        "table_header": "| 命令 | 說明 | 常用 Flag | 範例 |",
        "table_sep": "|------|------|----------|------|",
        "section_commands": "## 命令速查",
        "section_tips": "## 快速提示",
        "section_network": "## 網路配置",
        "section_templates": "## 常用樣板",
        "tips": """\
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
  - `lint` / `onboard` / `analyze-gaps` / `config-diff` — 治理工具""",
        "footer_ref": "完整參考見 [cli-reference.md](cli-reference.md)。",
        "lang_tag": "zh",
    },
    "en": {
        "title": "da-tools Quick Reference",
        "h1": "da-tools Quick Reference",
        "lang_switcher": '> **Language / 語言：** **English (Current)** | [中文](cheat-sheet.md)',
        "intro": "da-tools command quick reference. Full docs at [cli-reference.en.md](cli-reference.en.md).",
        "table_header": "| Command | Description | Key Flags | Example |",
        "table_sep": "|---------|-------------|-----------|---------|",
        "section_commands": "## Command Reference",
        "section_tips": "## Quick Tips",
        "section_network": "## Network Configuration",
        "section_templates": "## Common Templates",
        "tips": """\
- **Prometheus API Tools**: Require connectivity to Prometheus HTTP API
  - `check-alert` — Query alert status
  - `diagnose` / `batch-diagnose` — Tenant health check
  - `baseline` — Observe metrics, generate threshold suggestions
  - `validate` — Shadow Monitoring comparison
  - `cutover` — One-click switchover (final migration step)
  - Others: `blind-spot`, `maintenance-scheduler`, `backtest`

- **Config Generation Tools**
  - `generate-routes` — Tenant YAML → Alertmanager fragment
  - `patch-config` — ConfigMap partial update

- **Filesystem Tools** (offline capable)
  - `scaffold` — Generate tenant config
  - `migrate` — Rule format conversion
  - `validate-config` — Config validation
  - `offboard` / `deprecate` — Tenant offboarding / metric deprecation
  - `lint` / `onboard` / `analyze-gaps` / `config-diff` — Governance tools""",
        "footer_ref": "Full reference at [cli-reference.en.md](cli-reference.en.md).",
        "lang_tag": "en",
        "related_resources": """\

## Related Resources

| Resource | Relevance |
|----------|-----------|
| ["da-tools Quick Reference"](./cheat-sheet.md) | ★★★ |
| ["da-tools CLI Reference"](./cli-reference.en.md) | ★★★ |
| ["Glossary"](./glossary.en.md) | ★★ |
| ["Threshold Exporter API Reference"](api/README.en.md) | ★★ |""",
    },
}


def read_cli_reference(ref_path: str) -> str:
    """Read the CLI reference file."""
    with open(ref_path, 'r', encoding='utf-8') as f:
        return f.read()


def extract_commands(content: str) -> List[dict]:
    """Extract command definitions from cli-reference.md.

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

        # Filter out non-command sections
        if not re.match(r'^[a-z][\w-]*$', cmd_name):
            continue

        # Look for description in first 50 lines
        description = ""
        for line in lines[1:50]:
            line = line.strip()
            if line and not line.startswith('#') and not line.startswith('```'):
                match = re.match(r'^([^。.!?]+[。.!?]?)', line)
                if match:
                    description = match.group(1).strip()
                    if description.endswith(('。', '.')):
                        description = description[:-1]
                    break

        # Extract key flags from options table
        key_flags = _extract_key_flags(section)

        if cmd_name and description:
            commands.append({
                'name': cmd_name,
                'description': description,
                'flags': key_flags,
            })

    return commands


def _extract_key_flags(section: str) -> str:
    """Extract the most common 2-3 flags from a command section."""
    flags = []
    flag_pattern = r'^\s*\|\s*`(-[-\w]+(?:\s+<\w+>)?)`'

    for line in section.split('\n'):
        match = re.search(flag_pattern, line)
        if match:
            flag = match.group(1)
            if flag not in ['--help', '--version']:
                flags.append(flag)
                if len(flags) >= 3:
                    break

    return ', '.join(flags) if flags else ''


def _read_versions():
    """Read platform and da-tools versions from source-of-truth files."""
    return read_platform_version(), read_da_tools_version()


def create_cheat_sheet_content(commands: List[dict], version: str,
                               tools_version: str = "1.11.0",
                               lang: str = "zh") -> str:
    """Generate the cheat sheet markdown content."""
    s = STRINGS[lang]

    # Build table rows
    rows = []
    for cmd in commands:
        cmd_col = f'`{cmd["name"]}`'
        desc_col = cmd['description'][:50]
        if len(cmd['description']) > 50:
            desc_col += '...'
        flags_col = cmd['flags'] if cmd['flags'] else '-'
        example_col = f'`da-tools {cmd["name"]} --help`'
        rows.append(f'| {cmd_col} | {desc_col} | {flags_col} | {example_col} |')

    table = '\n'.join(rows)

    # Assemble
    parts = [
        f'---\ntitle: "{s["title"]}"\ntags: [reference, cli, cheat-sheet]\n'
        f'audience: [all]\nversion: {version}\nlang: {s["lang_tag"]}\n---\n',
        f'# {s["h1"]}\n',
        f'{s["lang_switcher"]}\n',
        f'{s["intro"]}\n',
        f'{s["section_commands"]}\n',
        f'{s["table_header"]}',
        f'{s["table_sep"]}',
        table,
        f'\n{s["section_tips"]}\n',
        s["tips"],
        f'\n{s["section_network"]}\n',
        '```bash',
        '# K8s internal',
        'export PROMETHEUS_URL=http://prometheus.monitoring.svc.cluster.local:9090',
        '',
        '# Docker Desktop',
        'export PROMETHEUS_URL=http://host.docker.internal:9090',
        '',
        '# Linux Docker (--network=host)',
        'export PROMETHEUS_URL=http://localhost:9090',
        '```',
        f'\n{s["section_templates"]}\n',
        '```bash',
        '# Basic command',
        'docker run --rm --network=host \\',
        '  -e PROMETHEUS_URL=$PROMETHEUS_URL \\',
        f'  ghcr.io/vencil/da-tools:v{tools_version} \\',
        '  <command> [arguments]',
        '',
        '# With local files',
        'docker run --rm --network=host \\',
        '  -v $(pwd)/conf.d:/etc/config:ro \\',
        '  -e PROMETHEUS_URL=$PROMETHEUS_URL \\',
        f'  ghcr.io/vencil/da-tools:v{tools_version} \\',
        '  <command> --config-dir /etc/config',
        '```',
        '',
        '---',
        '',
        s["footer_ref"],
    ]

    # English has related resources section
    if lang == "en" and "related_resources" in s:
        parts.append(s["related_resources"])

    return '\n'.join(parts) + '\n'


def _get_output_path(lang: str) -> Path:
    """Return the output path for a given language."""
    if lang == "en":
        return REPO_ROOT / "docs" / "cheat-sheet.en.md"
    return REPO_ROOT / "docs" / "cheat-sheet.md"


def main():
    """CLI entry point: Auto-generate da-tools cheat sheet from CLI reference."""
    parser = argparse.ArgumentParser(
        description='Generate da-tools cheat sheet from cli-reference.md'
    )
    parser.add_argument(
        '--check',
        action='store_true',
        help='Exit 1 if output differs from existing file (CI drift detection)'
    )
    parser.add_argument(
        '--lang', choices=['zh', 'en', 'all'], default='zh',
        help='Language: zh (default), en, or all'
    )
    # Legacy --output for backward compat (only for zh)
    parser.add_argument(
        '--output', default=None,
        help=argparse.SUPPRESS,
    )

    args = parser.parse_args()

    # Determine source
    cli_ref_path = REPO_ROOT / 'docs' / 'cli-reference.md'
    if not cli_ref_path.exists():
        print(f'Error: {cli_ref_path} not found', file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    content = read_cli_reference(str(cli_ref_path))
    commands = extract_commands(content)

    if not commands:
        print('Error: No commands extracted from cli-reference.md',
              file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    version, tools_version = _read_versions()
    langs = ['zh', 'en'] if args.lang == 'all' else [args.lang]

    has_drift = False

    for lang in langs:
        cheat_sheet = create_cheat_sheet_content(
            commands, version, tools_version=tools_version, lang=lang)

        # Legacy --output override (zh only)
        if args.output and lang == "zh":
            output_path = REPO_ROOT / args.output
        else:
            output_path = _get_output_path(lang)

        if args.check:
            if output_path.exists():
                existing = output_path.read_text(encoding='utf-8')
                if existing == cheat_sheet:
                    print(f'Cheat sheet is up-to-date: {output_path}')
                else:
                    print(f'Cheat sheet is outdated: {output_path}',
                          file=sys.stderr)
                    has_drift = True
            else:
                print(f'Cheat sheet does not exist: {output_path}',
                      file=sys.stderr)
                has_drift = True
        else:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(cheat_sheet, encoding='utf-8')
            os.chmod(output_path,
                     stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP
                     | stat.S_IROTH)
            print(f'Generated cheat sheet: {output_path}')
            print(f'Commands extracted: {len(commands)}')

    if args.check and has_drift:
        sys.exit(EXIT_VIOLATION)

    return EXIT_OK


if __name__ == '__main__':
    sys.exit(main())
