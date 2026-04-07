#!/usr/bin/env python3
"""check_md_yaml_drift.py — Markdown 內 YAML 範例與 Schema 漂移偵測

掃描 docs/ 下 Markdown 文件中的 ```yaml 區塊，送入 tenant-config.schema.json 校驗。
確保文件上的 YAML 範例永遠是合法配置，schema 變更時自動偵測漂移。

用法:
  python3 scripts/tools/lint/check_md_yaml_drift.py              # 顯示報告
  python3 scripts/tools/lint/check_md_yaml_drift.py --ci          # CI 模式（exit 1 if drift）
  python3 scripts/tools/lint/check_md_yaml_drift.py --verbose     # 顯示所有掃描的區塊

需要: pyyaml, jsonschema
"""

import json
import os
import re
import sys
import argparse
from pathlib import Path
from typing import Dict, List, Tuple

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml not installed. Run: pip install pyyaml", file=sys.stderr)
    sys.exit(2)

try:
    import jsonschema
except ImportError:
    jsonschema = None  # Graceful degradation: skip schema validation


class MdYamlDriftChecker:
    """掃描 Markdown 中的 YAML code blocks 並驗證 schema 合規性。"""

    # YAML 區塊必須包含這些 key 之一才被視為 tenant config 範例
    TENANT_CONFIG_MARKERS = {
        "_thresholds", "_routing", "_silent_mode", "_state_maintenance",
        "_routing_defaults", "_routing_enforced", "_threshold_mode",
        "_write_mode", "_write_back_mode",
    }

    def __init__(self, repo_root: str, verbose: bool = False):
        self.repo_root = Path(repo_root).resolve()
        self.verbose = verbose
        self.schema: Dict = {}
        self._load_schema()

    def _load_schema(self):
        """Load tenant-config.schema.json."""
        schema_path = self.repo_root / "docs" / "schemas" / "tenant-config.schema.json"
        if schema_path.exists():
            try:
                self.schema = json.loads(schema_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                print(f"WARNING: Cannot load schema: {e}", file=sys.stderr)

    def _extract_yaml_blocks(self, filepath: Path) -> List[Tuple[int, str]]:
        """Extract ```yaml code blocks from a markdown file.

        Returns list of (line_number, yaml_content) tuples.
        """
        blocks = []
        try:
            content = filepath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return blocks

        in_yaml = False
        block_start = 0
        block_lines = []

        for i, line in enumerate(content.splitlines(), 1):
            stripped = line.strip()
            if stripped in ("```yaml", "```yml") and not in_yaml:
                in_yaml = True
                block_start = i
                block_lines = []
            elif stripped == "```" and in_yaml:
                in_yaml = False
                if block_lines:
                    blocks.append((block_start, "\n".join(block_lines)))
            elif in_yaml:
                block_lines.append(line)

        return blocks

    def _is_tenant_config(self, data) -> bool:
        """Check if parsed YAML looks like a tenant config snippet."""
        if not isinstance(data, dict):
            return False
        # Direct match
        for key in data:
            if key in self.TENANT_CONFIG_MARKERS:
                return True
        # Nested under tenant name
        for value in data.values():
            if isinstance(value, dict):
                for key in value:
                    if key in self.TENANT_CONFIG_MARKERS:
                        return True
        return False

    def run(self) -> int:
        """Execute YAML drift check."""
        issues = []
        total_blocks = 0
        tenant_blocks = 0
        validated_blocks = 0

        docs_dir = self.repo_root / "docs"
        for md_file in sorted(docs_dir.rglob("*.md")):
            rel_path = str(md_file.relative_to(self.repo_root))
            blocks = self._extract_yaml_blocks(md_file)

            for line_num, yaml_content in blocks:
                total_blocks += 1

                # Try to parse YAML
                try:
                    data = yaml.safe_load(yaml_content)
                except yaml.YAMLError as e:
                    issues.append({
                        "file": rel_path,
                        "line": line_num,
                        "error": f"Invalid YAML: {e}",
                        "type": "parse_error",
                    })
                    continue

                if data is None:
                    continue

                # Check if this looks like tenant config
                if not self._is_tenant_config(data):
                    if self.verbose:
                        print(f"  SKIP {rel_path}:{line_num} (not tenant config)")
                    continue

                tenant_blocks += 1

                # Schema validation (if available)
                if self.schema and jsonschema:
                    try:
                        jsonschema.validate(data, self.schema)
                        validated_blocks += 1
                    except jsonschema.ValidationError as e:
                        issues.append({
                            "file": rel_path,
                            "line": line_num,
                            "error": f"Schema violation: {e.message}",
                            "type": "schema_error",
                        })
                    except jsonschema.SchemaError as e:
                        print(f"WARNING: Schema itself is invalid: {e}", file=sys.stderr)
                        break

                if self.verbose:
                    print(f"  OK   {rel_path}:{line_num}")

        # Report
        parse_errors = [i for i in issues if i["type"] == "parse_error"]
        schema_errors = [i for i in issues if i["type"] == "schema_error"]

        print("=" * 60)
        print("MARKDOWN YAML DRIFT CHECK")
        print("=" * 60)
        print(f"Total YAML blocks scanned:   {total_blocks}")
        print(f"Tenant config blocks:        {tenant_blocks}")
        print(f"Schema-validated OK:         {validated_blocks}")
        print(f"Parse errors:                {len(parse_errors)}")
        print(f"Schema violations:           {len(schema_errors)}")
        if not self.schema:
            print("  (schema not loaded — skipped validation)")
        if not jsonschema:
            print("  (jsonschema not installed — skipped validation)")
        print()

        if issues:
            for issue in issues:
                marker = "PARSE" if issue["type"] == "parse_error" else "SCHEMA"
                print(f"  [{marker}] {issue['file']}:{issue['line']}")
                print(f"           {issue['error']}")
            print()
            print("Fix: Update YAML examples to match current schema,")
            print("     or update docs/schemas/tenant-config.schema.json if schema changed.")
            return 1
        else:
            print("✓ All YAML examples are valid.")
            return 0


def main():
    parser = argparse.ArgumentParser(
        description="Check Markdown YAML examples against tenant-config schema"
    )
    parser.add_argument("--ci", action="store_true", help="CI mode: exit 1 on drift")
    parser.add_argument("--verbose", action="store_true", help="Show all scanned blocks")
    parser.add_argument("--repo-root", default=".", help="Repository root (default: .)")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    if not (repo_root / "docs").exists():
        print(f"ERROR: Cannot find docs/ in {repo_root}", file=sys.stderr)
        return 2

    checker = MdYamlDriftChecker(str(repo_root), verbose=args.verbose)
    exit_code = checker.run()

    if args.ci:
        return exit_code
    return 0


if __name__ == "__main__":
    sys.exit(main())
