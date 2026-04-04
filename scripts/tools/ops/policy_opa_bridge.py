#!/usr/bin/env python3
"""
policy_opa_bridge.py — OPA (Open Policy Agent) bridge for tenant config policy evaluation.

Converts tenant YAML configs to OPA input JSON format, evaluates via OPA (REST API or local binary),
and converts OPA responses back to PolicyResult / Violation format compatible with policy_engine.py.

Modes:
  --opa-url: Call OPA REST API (POST /v1/data/<package>/violations)
  --opa-binary: Call local 'opa eval' subprocess
  --dry-run: Show input JSON without calling OPA
  --policy-path: Path to .rego file(s) for local eval

OPA Input JSON format:
  {
    "tenants": {
      "db-a": { "mysql_connections": "70", "_routing": {...} },
      ...
    },
    "defaults": { "mysql_connections": 80 },
    "rule_packs": ["mariadb", "kubernetes"],
    "platform_version": "v2.3.0"
  }

OPA Response format:
  { "result": [{"msg": "...", "severity": "error|warning", "tenant": "...", "field": "..."}] }

Violation conversion:
  OPA "severity": "error|warning" → Violation "level": "ERROR|WARNING"
  OPA "msg" → Violation "message"
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.request import Request, urlopen
from urllib.error import URLError

# ---------------------------------------------------------------------------
# Repo-layout import compatibility
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
try:
    from _lib_python import (
        detect_cli_lang,
        load_yaml_file,
    )
except ImportError:
    from scripts.tools._lib_python import (  # type: ignore[no-redef]
        detect_cli_lang,
        load_yaml_file,
    )

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------
@dataclass
class Violation:
    """OPA policy violation."""
    tenant: str
    level: str  # ERROR or WARNING
    message: str
    field: str


@dataclass
class PolicyResult:
    """OPA policy evaluation result."""
    violations: list[Violation] = field(default_factory=list)
    tenants_evaluated: int = 0
    policy_package: str = ""

    @property
    def error_count(self) -> int:
        return sum(1 for v in self.violations if v.level == "ERROR")

    @property
    def warning_count(self) -> int:
        return sum(1 for v in self.violations if v.level == "WARNING")

    @property
    def passed(self) -> bool:
        return self.error_count == 0


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------
def load_tenant_configs(config_dir: str) -> dict[str, dict]:
    """Load all tenant configs from config-dir (skip _ prefixed files)."""
    configs: dict[str, dict] = {}

    if not os.path.isdir(config_dir):
        return configs

    for fname in sorted(os.listdir(config_dir)):
        if not fname.endswith((".yaml", ".yml")):
            continue
        if fname.startswith("_"):
            continue

        fpath = os.path.join(config_dir, fname)
        data = load_yaml_file(fpath)
        if not isinstance(data, dict):
            continue

        # Multi-tenant wrapper format
        if "tenants" in data and isinstance(data["tenants"], dict):
            for tenant_name, tenant_cfg in data["tenants"].items():
                if isinstance(tenant_cfg, dict):
                    configs[tenant_name] = tenant_cfg
        else:
            # Flat format — filename (sans extension) is tenant name
            tenant_name = os.path.splitext(fname)[0]
            configs[tenant_name] = data

    return configs


def load_defaults(config_dir: str) -> dict[str, Any]:
    """Load _defaults.yaml."""
    defaults_path = os.path.join(config_dir, "_defaults.yaml")
    if not os.path.isfile(defaults_path):
        return {}
    data = load_yaml_file(defaults_path)
    if isinstance(data, dict):
        return data
    return {}


# ---------------------------------------------------------------------------
# OPA input builder
# ---------------------------------------------------------------------------
def build_opa_input(
    config_dir: str,
    tenant_configs: dict[str, dict],
    defaults: dict,
    rule_packs: Optional[list[str]] = None,
    platform_version: str = "v2.3.0",
) -> dict[str, Any]:
    """Build OPA input JSON from tenant configs.

    Args:
        config_dir: config directory path
        tenant_configs: {tenant_name: config_dict}
        defaults: defaults dict (usually from _defaults.yaml)
        rule_packs: list of rule pack names
        platform_version: platform version string

    Returns:
        OPA input dict ready for JSON serialization
    """
    # Extract threshold defaults (non-underscore keys)
    defaults_thresholds = {
        k: v for k, v in defaults.items()
        if not k.startswith("_")
    }

    # Detect rule packs from config filenames
    if rule_packs is None:
        rule_packs = []

    return {
        "tenants": tenant_configs,
        "defaults": defaults_thresholds,
        "rule_packs": rule_packs,
        "platform_version": platform_version,
    }


# ---------------------------------------------------------------------------
# OPA evaluation
# ---------------------------------------------------------------------------
def call_opa_rest(
    opa_url: str,
    package: str,
    input_data: dict,
) -> list[dict]:
    """Call OPA REST API.

    Args:
        opa_url: Base OPA URL (e.g., http://localhost:8181)
        package: OPA package path (e.g., dynamic_alerting.policy)
        input_data: OPA input dict

    Returns:
        List of violation dicts from OPA result
    """
    endpoint = f"{opa_url.rstrip('/')}/v1/data/{package}/violations"

    request_body = json.dumps({"input": input_data}).encode("utf-8")
    req = Request(endpoint, data=request_body, method="POST")
    req.add_header("Content-Type", "application/json")

    try:
        with urlopen(req, timeout=10) as response:
            response_data = json.loads(response.read().decode("utf-8"))
            result = response_data.get("result", [])
            if isinstance(result, list):
                return result
            return []
    except URLError as e:
        print(f"ERROR: OPA API call failed: {e}", file=sys.stderr)
        return []
    except json.JSONDecodeError as e:
        print(f"ERROR: OPA response parsing failed: {e}", file=sys.stderr)
        return []


def call_opa_binary(
    opa_binary: str,
    policy_path: str,
    package: str,
    input_data: dict,
) -> list[dict]:
    """Call local OPA binary via subprocess.

    Args:
        opa_binary: Path to opa binary (default: 'opa')
        policy_path: Path to .rego file(s)
        package: OPA package path
        input_data: OPA input dict

    Returns:
        List of violation dicts from OPA result
    """
    input_json = json.dumps(input_data)

    cmd = [
        opa_binary,
        "eval",
        f"-d={policy_path}",
        f"{package}/violations",
        "-I",
        input_json,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        if result.returncode != 0:
            print(f"ERROR: OPA eval failed: {result.stderr}", file=sys.stderr)
            return []

        response_data = json.loads(result.stdout)
        result_data = response_data.get("result", [])
        if isinstance(result_data, list):
            return result_data
        return []
    except FileNotFoundError:
        print(f"ERROR: OPA binary not found at {opa_binary}", file=sys.stderr)
        return []
    except subprocess.TimeoutExpired:
        print("ERROR: OPA eval timeout", file=sys.stderr)
        return []
    except json.JSONDecodeError as e:
        print(f"ERROR: OPA output parsing failed: {e}", file=sys.stderr)
        return []


# ---------------------------------------------------------------------------
# OPA response conversion
# ---------------------------------------------------------------------------
def convert_opa_violations(
    opa_violations: list[dict],
    tenants_count: int,
) -> PolicyResult:
    """Convert OPA response to PolicyResult.

    Args:
        opa_violations: List of violation dicts from OPA
        tenants_count: Number of tenants evaluated

    Returns:
        PolicyResult with converted violations
    """
    result = PolicyResult(
        tenants_evaluated=tenants_count,
        policy_package="dynamic_alerting.policy",
    )

    for v in opa_violations:
        if not isinstance(v, dict):
            continue

        try:
            level = v.get("severity", "error").upper()
            if level not in ("ERROR", "WARNING"):
                level = "ERROR"

            violation = Violation(
                tenant=str(v.get("tenant", "unknown")),
                level=level,
                message=str(v.get("msg", "Policy violation")),
                field=str(v.get("field", "")),
            )
            result.violations.append(violation)
        except (KeyError, AttributeError):
            continue

    return result


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------
def generate_text_report(result: PolicyResult, lang: str = "en") -> str:
    """Generate text report."""
    lines: list[str] = []

    if lang == "zh":
        lines.append("═══ OPA 策略評估報告 ═══")
        lines.append(f"租戶數: {result.tenants_evaluated}")
        lines.append(f"錯誤: {result.error_count} | 警告: {result.warning_count}")
    else:
        lines.append("═══ OPA Policy Evaluation Report ═══")
        lines.append(f"Tenants: {result.tenants_evaluated}")
        lines.append(f"Errors: {result.error_count} | Warnings: {result.warning_count}")

    if not result.violations:
        lines.append("")
        lines.append("✓ All policies passed." if lang == "en"
                     else "✓ 所有策略均通過。")
        return "\n".join(lines)

    lines.append("")

    # Group by tenant
    by_tenant: dict[str, list[Violation]] = {}
    for v in result.violations:
        by_tenant.setdefault(v.tenant, []).append(v)

    for tenant in sorted(by_tenant):
        lines.append(f"[{tenant}]")
        for v in by_tenant[tenant]:
            icon = "✗" if v.level == "ERROR" else "⚠"
            lines.append(f"  {icon} [{v.level}] {v.field}: {v.message}")
        lines.append("")

    status = "FAIL" if not result.passed else "PASS"
    lines.append(f"Result: {status}" if lang == "en"
                 else f"結果: {status}")

    return "\n".join(lines)


def generate_json_report(result: PolicyResult) -> dict:
    """Generate JSON report."""
    return {
        "tenants_evaluated": result.tenants_evaluated,
        "error_count": result.error_count,
        "warning_count": result.warning_count,
        "passed": result.passed,
        "violations": [
            {
                "tenant": v.tenant,
                "level": v.level,
                "message": v.message,
                "field": v.field,
            }
            for v in result.violations
        ],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser(lang: str = "en") -> argparse.ArgumentParser:
    """Build CLI argument parser."""
    if lang == "zh":
        parser = argparse.ArgumentParser(
            description="OPA (Open Policy Agent) 策略評估橋接 — 將 tenant 配置轉換為 OPA 輸入並評估。",
        )
        parser.add_argument(
            "--config-dir", required=True,
            help="conf.d/ 目錄路徑（含 tenant YAML）",
        )
        parser.add_argument(
            "--opa-url",
            help="OPA REST API URL（例：http://localhost:8181）",
        )
        parser.add_argument(
            "--opa-binary", default="opa",
            help="OPA 二進檔路徑（預設：opa）",
        )
        parser.add_argument(
            "--policy-package", default="dynamic_alerting.policy",
            help="OPA 套件路徑（預設：dynamic_alerting.policy）",
        )
        parser.add_argument(
            "--policy-path",
            help=".rego 檔案路徑（用於本地評估）",
        )
        parser.add_argument(
            "--json", action="store_true", dest="json_output",
            help="JSON 格式輸出",
        )
        parser.add_argument(
            "--ci", action="store_true",
            help="CI 模式：有 error 級違規時 exit 1",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="僅顯示 OPA 輸入 JSON，不呼叫 OPA",
        )
    else:
        parser = argparse.ArgumentParser(
            description="OPA (Open Policy Agent) bridge — convert tenant configs to OPA input and evaluate.",
        )
        parser.add_argument(
            "--config-dir", required=True,
            help="Path to conf.d/ directory containing tenant YAML files",
        )
        parser.add_argument(
            "--opa-url",
            help="OPA REST API URL (e.g., http://localhost:8181)",
        )
        parser.add_argument(
            "--opa-binary", default="opa",
            help="Path to opa binary (default: opa)",
        )
        parser.add_argument(
            "--policy-package", default="dynamic_alerting.policy",
            help="OPA package path (default: dynamic_alerting.policy)",
        )
        parser.add_argument(
            "--policy-path",
            help="Path to .rego file(s) for local evaluation",
        )
        parser.add_argument(
            "--json", action="store_true", dest="json_output",
            help="Output in JSON format",
        )
        parser.add_argument(
            "--ci", action="store_true",
            help="CI mode: exit 1 if any error-level violations found",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Show OPA input JSON only, do not call OPA",
        )

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point."""
    lang = detect_cli_lang()
    parser = build_parser(lang)
    args = parser.parse_args(argv)

    # Load configs
    tenant_configs = load_tenant_configs(args.config_dir)
    defaults = load_defaults(args.config_dir)

    if not tenant_configs:
        if lang == "zh":
            print(f"未找到 tenant 配置於 {args.config_dir}")
        else:
            print(f"No tenant configs found in {args.config_dir}")
        return 0

    # Build OPA input
    opa_input = build_opa_input(
        args.config_dir,
        tenant_configs,
        defaults,
        rule_packs=None,
        platform_version="v2.3.0",
    )

    # Dry-run mode
    if args.dry_run:
        print(json.dumps(opa_input, indent=2, ensure_ascii=False))
        return 0

    # Evaluate via OPA
    opa_violations: list[dict] = []

    if args.opa_url:
        opa_violations = call_opa_rest(
            args.opa_url,
            args.policy_package,
            opa_input,
        )
    elif args.policy_path:
        opa_violations = call_opa_binary(
            args.opa_binary,
            args.policy_path,
            args.policy_package,
            opa_input,
        )
    else:
        if lang == "zh":
            print("錯誤：必須指定 --opa-url 或 --policy-path")
        else:
            print("ERROR: Must specify --opa-url or --policy-path")
        return 1

    # Convert to PolicyResult
    result = convert_opa_violations(opa_violations, len(tenant_configs))

    # Output
    if args.json_output:
        print(json.dumps(generate_json_report(result), indent=2, ensure_ascii=False))
    else:
        print(generate_text_report(result, lang))

    # Exit code
    if args.ci and not result.passed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
