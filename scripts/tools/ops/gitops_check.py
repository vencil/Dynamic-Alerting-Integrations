#!/usr/bin/env python3
"""gitops_check.py — GitOps Native Mode readiness validator.

Validates GitOps readiness for the Dynamic Alerting platform by checking:

1. **repo** — Git repository accessibility and structure
   - Uses git ls-remote (no full clone) for fast validation
   - Verifies branch existence and config path availability

2. **local** — Local configuration structure validation
   - Checks _defaults.yaml presence in conf.d/
   - Validates YAML file parseability
   - Counts metrics and tenant configurations

3. **sidecar** — K8s git-sync deployment readiness
   - Verifies git-sync secret existence (optional)
   - Checks threshold-exporter sidecar presence

Supports --json output and --ci exit code mode for CI/CD integration.

Usage:
    da-tools gitops-check repo --url <git-url> [--branch main] [--path configs/]
    da-tools gitops-check local --dir <path>
    da-tools gitops-check sidecar [--namespace monitoring]
    da-tools gitops-check repo --url <git-url> --json
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from _lib_python import detect_cli_lang, format_json_report  # noqa: E402

# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------
_LANG = detect_cli_lang()


def _h(zh: str, en: str) -> str:
    """Bilingual help text selector."""
    return zh if _LANG == "zh" else en


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------
@dataclass
class CheckResult:
    """Single check result."""
    check: str  # 'repo', 'local', 'sidecar'
    status: str  # 'pass', 'fail', 'warn'
    message: str
    details: Optional[dict[str, Any]] = None


@dataclass
class GitOpsReport:
    """Overall GitOps readiness report."""
    overall_status: str  # 'pass', 'fail', 'warn'
    checks: list[CheckResult]
    timestamp: str

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return {
            "overall_status": self.overall_status,
            "timestamp": self.timestamp,
            "checks": [
                {
                    "check": c.check,
                    "status": c.status,
                    "message": c.message,
                    "details": c.details or {},
                }
                for c in self.checks
            ],
        }


# ---------------------------------------------------------------------------
# Git repo checks
# ---------------------------------------------------------------------------
def _run_cmd(cmd: list[str], timeout: int = 10) -> tuple[bool, str, str]:
    """Run shell command, return (success, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
    except FileNotFoundError:
        return False, "", _h(
            f"命令不存在: {cmd[0]}",
            f"Command not found: {cmd[0]}"
        )
    except subprocess.TimeoutExpired:
        return False, "", _h(
            f"命令超時: {' '.join(cmd)}",
            f"Command timeout: {' '.join(cmd)}"
        )
    except Exception as e:
        return False, "", str(e)


def check_repo(
    url: str,
    branch: str = "main",
    path: str = "configs/",
) -> CheckResult:
    """Check Git repository accessibility and structure.

    Uses git ls-remote to validate without full clone.
    """
    details = {
        "url": url,
        "branch": branch,
        "config_path": path,
    }

    # Step 1: Test URL accessibility with git ls-remote
    success, stdout, stderr = _run_cmd(["git", "ls-remote", "--heads", url])
    if not success:
        return CheckResult(
            check="repo",
            status="fail",
            message=_h(
                f"無法訪問 Git 倉庫: {stderr or '連接失敗'}",
                f"Cannot access Git repository: {stderr or 'connection failed'}"
            ),
            details=details,
        )

    # Step 2: Check if branch exists
    branches = [line.split("\t")[1] for line in stdout.split("\n") if line.strip()]
    branch_ref = f"refs/heads/{branch}"
    branch_exists = any(b == branch_ref or b.endswith(f"/{branch}") for b in branches)

    if not branch_exists:
        return CheckResult(
            check="repo",
            status="fail",
            message=_h(
                f"分支不存在: {branch}",
                f"Branch not found: {branch}"
            ),
            details={**details, "available_branches": len(branches)},
        )

    # Step 3: Best-effort path verification via git archive --remote
    # Not all Git servers support this (GitHub/GitLab do, Gitea may not).
    details["branch_found"] = True
    details["branch_count"] = len(branches)

    path_ok, path_out, path_err = _run_cmd(
        ["git", "archive", f"--remote={url}", branch, path],
        timeout=10,
    )
    if path_ok:
        details["config_path_verified"] = True
    else:
        # Not a hard failure — server may not support git archive --remote
        details["config_path_verified"] = False
        details["config_path_note"] = _h(
            "無法遠端驗證路徑（可能不支援 git archive --remote），請用 local 子命令確認",
            "Cannot verify path remotely (git archive --remote may not be supported), use local subcommand to confirm"
        )

    status = "pass"
    if not path_ok and "config_path_note" in details:
        status = "pass"  # path check is best-effort, not a failure

    return CheckResult(
        check="repo",
        status=status,
        message=_h(
            f"Git 倉庫可訪問，分支 {branch} 存在"
            + ("，配置路徑已驗證" if details.get("config_path_verified") else ""),
            f"Git repository accessible, branch {branch} exists"
            + (", config path verified" if details.get("config_path_verified") else "")
        ),
        details=details,
    )


# ---------------------------------------------------------------------------
# Local config checks
# ---------------------------------------------------------------------------
def check_local(dir_path: str) -> CheckResult:
    """Check local configuration directory structure and validity."""
    details = {"directory": dir_path}

    # Check directory exists
    if not os.path.isdir(dir_path):
        return CheckResult(
            check="local",
            status="fail",
            message=_h(
                f"目錄不存在: {dir_path}",
                f"Directory not found: {dir_path}"
            ),
            details=details,
        )

    # Check _defaults.yaml exists
    defaults_path = os.path.join(dir_path, "_defaults.yaml")
    if not os.path.isfile(defaults_path):
        return CheckResult(
            check="local",
            status="fail",
            message=_h(
                "缺少 _defaults.yaml（必需）",
                "Missing _defaults.yaml (required)"
            ),
            details=details,
        )

    # Validate _defaults.yaml
    try:
        with open(defaults_path, encoding="utf-8") as f:
            yaml.safe_load(f)
    except yaml.YAMLError as e:
        return CheckResult(
            check="local",
            status="fail",
            message=_h(
                f"_defaults.yaml 無效: {e}",
                f"_defaults.yaml is invalid: {e}"
            ),
            details=details,
        )

    # Scan tenant files
    tenant_files = []
    parse_errors = []
    total_alerts = 0

    try:
        for filename in sorted(os.listdir(dir_path)):
            if not filename.endswith(".yaml") or filename.startswith("_"):
                continue

            file_path = os.path.join(dir_path, filename)
            if not os.path.isfile(file_path):
                continue

            try:
                with open(file_path, encoding="utf-8") as f:
                    data = yaml.safe_load(f)

                # Count metric keys (tenant YAML is flat key-value,
                # skip internal keys starting with _)
                metric_count = 0
                if isinstance(data, dict):
                    metric_count = sum(
                        1 for k in data
                        if not k.startswith("_")
                    )

                tenant_files.append({
                    "name": filename,
                    "metrics": metric_count,
                })
                total_alerts += metric_count
            except yaml.YAMLError as e:
                parse_errors.append({"file": filename, "error": str(e)})
    except OSError as e:
        return CheckResult(
            check="local",
            status="fail",
            message=_h(
                f"無法掃描目錄: {e}",
                f"Cannot scan directory: {e}"
            ),
            details=details,
        )

    # Build result
    details["defaults_file"] = "present"
    details["tenant_files"] = len(tenant_files)
    details["total_metrics"] = total_alerts
    if parse_errors:
        details["parse_errors"] = parse_errors

    if parse_errors:
        return CheckResult(
            check="local",
            status="fail",
            message=_h(
                f"{len(parse_errors)} 個 YAML 檔案無效",
                f"{len(parse_errors)} YAML files are invalid"
            ),
            details=details,
        )

    return CheckResult(
        check="local",
        status="pass",
        message=_h(
            f"配置結構有效，{len(tenant_files)} 個租戶檔案，{total_alerts} 個指標",
            f"Configuration structure valid, {len(tenant_files)} tenant files, {total_alerts} metrics"
        ),
        details=details,
    )


# ---------------------------------------------------------------------------
# K8s sidecar checks
# ---------------------------------------------------------------------------
def check_sidecar(namespace: str = "monitoring") -> CheckResult:
    """Check K8s git-sync sidecar deployment readiness.

    This is a best-effort check; missing kubectl is not a failure.
    """
    details = {"namespace": namespace}

    # Check if kubectl is available
    success, _, _ = _run_cmd(["kubectl", "version", "--short"], timeout=5)
    if not success:
        return CheckResult(
            check="sidecar",
            status="warn",
            message=_h(
                "kubectl 不可用，跳過 K8s 檢查",
                "kubectl not available, skipping K8s checks"
            ),
            details=details,
        )

    # Check if git-sync secret exists
    secret_cmd = ["kubectl", "get", "secret", "git-sync-credentials",
                  "-n", namespace, "--no-headers"]
    secret_exists, _, _ = _run_cmd(secret_cmd, timeout=10)
    details["git_sync_secret"] = "present" if secret_exists else "missing"

    # Check if threshold-exporter deployment has git-sync sidecar
    deploy_cmd = [
        "kubectl", "get", "deployment", "threshold-exporter",
        "-n", namespace, "-o", "jsonpath={.spec.template.spec.containers[*].name}",
    ]
    success, stdout, _ = _run_cmd(deploy_cmd, timeout=10)

    has_sidecar = False
    if success and stdout:
        containers = stdout.split()
        has_sidecar = "git-sync" in containers
        details["sidecar_present"] = has_sidecar
        details["containers"] = containers

    # Determine status
    if secret_exists and has_sidecar:
        status = "pass"
        message = _h(
            "git-sync 邊車已部署，Secret 存在",
            "git-sync sidecar deployed, secret exists"
        )
    elif secret_exists or has_sidecar:
        status = "warn"
        message = _h(
            "部分 git-sync 配置缺失",
            "Partial git-sync configuration"
        )
    else:
        status = "warn"
        message = _h(
            "git-sync 邊車未部署或未配置",
            "git-sync sidecar not deployed or not configured"
        )

    return CheckResult(
        check="sidecar",
        status=status,
        message=message,
        details=details,
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="da-tools gitops-check",
        description=_h(
            "GitOps 原生模式就緒性檢驗工具",
            "GitOps Native Mode readiness validator"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Subcommands first
    sub = parser.add_subparsers(dest="action", help=_h("檢查類型", "Check type"))

    # repo subcommand
    repo_parser = sub.add_parser(
        "repo",
        help=_h("檢查 Git 倉庫訪問性", "Check Git repository accessibility"),
    )
    repo_parser.add_argument(
        "--url",
        required=True,
        help=_h("Git 倉庫 URL", "Git repository URL"),
    )
    repo_parser.add_argument(
        "--branch",
        default="main",
        help=_h("檢查的分支（預設: main）", "Branch to check (default: main)"),
    )
    repo_parser.add_argument(
        "--path",
        default="configs/",
        help=_h("配置路徑（預設: configs/）", "Config path (default: configs/)"),
    )
    repo_parser.add_argument(
        "--json",
        action="store_true",
        help=_h("輸出 JSON 格式", "Output JSON format"),
    )
    repo_parser.add_argument(
        "--ci",
        action="store_true",
        help=_h("CI 模式（警告也退出碼 0，僅失敗退出碼 1）",
                "CI mode (warnings exit 0, only failures exit 1)"),
    )

    # local subcommand
    local_parser = sub.add_parser(
        "local",
        help=_h("檢查本機配置結構", "Check local config structure"),
    )
    local_parser.add_argument(
        "--dir",
        required=True,
        help=_h("配置目錄路徑", "Config directory path"),
    )
    local_parser.add_argument(
        "--json",
        action="store_true",
        help=_h("輸出 JSON 格式", "Output JSON format"),
    )
    local_parser.add_argument(
        "--ci",
        action="store_true",
        help=_h("CI 模式（警告也退出碼 0，僅失敗退出碼 1）",
                "CI mode (warnings exit 0, only failures exit 1)"),
    )

    # sidecar subcommand
    sidecar_parser = sub.add_parser(
        "sidecar",
        help=_h("檢查 K8s git-sync 邊車就緒性", "Check K8s git-sync sidecar readiness"),
    )
    sidecar_parser.add_argument(
        "--namespace",
        default="monitoring",
        help=_h("K8s 命名空間（預設: monitoring）", "K8s namespace (default: monitoring)"),
    )
    sidecar_parser.add_argument(
        "--json",
        action="store_true",
        help=_h("輸出 JSON 格式", "Output JSON format"),
    )
    sidecar_parser.add_argument(
        "--ci",
        action="store_true",
        help=_h("CI 模式（警告也退出碼 0，僅失敗退出碼 1）",
                "CI mode (warnings exit 0, only failures exit 1)"),
    )

    args = parser.parse_args()

    if not args.action:
        parser.print_help()
        sys.exit(1)

    # Run appropriate check
    timestamp = datetime.now(timezone.utc).isoformat()

    checks = []
    if args.action == "repo":
        checks.append(check_repo(args.url, args.branch, args.path))
    elif args.action == "local":
        checks.append(check_local(args.dir))
    elif args.action == "sidecar":
        checks.append(check_sidecar(args.namespace))

    # Determine overall status
    statuses = [c.status for c in checks]
    if "fail" in statuses:
        overall_status = "fail"
    elif "warn" in statuses:
        overall_status = "warn"
    else:
        overall_status = "pass"

    report = GitOpsReport(
        overall_status=overall_status,
        checks=checks,
        timestamp=timestamp,
    )

    # Output
    if args.json:
        print(format_json_report(report.to_dict()))
    else:
        # Human-readable output
        check = checks[0]
        status_symbol = "✓" if check.status == "pass" else "⚠" if check.status == "warn" else "✗"
        print(f"{status_symbol} {check.message}")
        if check.details:
            for key, value in check.details.items():
                if key in ("url", "branch", "config_path", "directory", "namespace"):
                    print(f"  {key}: {value}")
                elif isinstance(value, (int, str)) and key not in ("parse_errors", "containers"):
                    print(f"  {key}: {value}")

    # Exit code
    if args.ci:
        # CI mode: only fail on "fail" status
        sys.exit(0 if overall_status != "fail" else 1)
    else:
        # Normal mode: fail on anything other than pass
        sys.exit(0 if overall_status == "pass" else 1)


if __name__ == "__main__":
    main()
