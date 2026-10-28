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
   - Verifies the git-sync Secret and the threshold-exporter git-sync
     container; either missing is a fail, and a cluster kubectl cannot
     read is a caller error (exit 2; #3210)

Supports --json output and --ci exit code mode for CI/CD integration.

Usage:
    da-tools gitops-check repo --url <git-url> [--branch main] [--path configs/]
    da-tools gitops-check local --dir <path>
    da-tools gitops-check sidecar [--namespace monitoring]
    da-tools gitops-check repo --url <git-url> --json
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from _lib_python import detect_cli_lang, format_json_report  # noqa: E402
from _lib_exitcodes import (  # noqa: E402
    EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR, CallerError, arm_crash_envelope,
)
# ADR-036 step 4 (#2906): the conf.d is read through `da-guard parse-tree`.
import _lib_parse_cache  # noqa: E402
from _lib_parse_tree import ParseTreeError  # noqa: E402
from _lib_kubectl import KubectlError, kubectl_get  # noqa: E402  (#3210)

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
# `git archive --remote` naming a path the branch does not have — git's own
# wording (from the server, in its locale). Any other failure (the server
# does not run upload-archive, a ref it refuses) stays "cannot verify".
_PATHSPEC_MISSING = "did not match any files"


def _run_cmd(cmd: list[str], timeout: int = 10,
             keep_stdout: bool = True) -> tuple[bool, str, str]:
    """Run shell command, return (success, stdout, stderr).

    keep_stdout=False discards stdout unread, for a command whose stdout is
    binary (git archive writes a tar): decoding it as text is what made the
    path check fail on a cp950 host (#1374).
    """
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE if keep_stdout else subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
            check=False,
        )
        return result.returncode == 0, (result.stdout or "").strip(), result.stderr.strip()
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
    # That branch, as git itself resolves a branch name (#3211): `main` is
    # refs/heads/main — never refs/heads/feature/main, which merely ends in
    # it — and `heads/x` / `refs/heads/x` name refs/heads/x.
    wanted = {f"refs/heads/{branch}", f"refs/{branch}", branch}
    branch_exists = any(b in wanted and b.startswith("refs/heads/") for b in branches)

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
        timeout=10, keep_stdout=False,
    )
    if path_ok:
        details["config_path_verified"] = True
    elif _PATHSPEC_MISSING in path_err:
        # The server DID run `git archive` and answered that the path is not
        # in that branch (#3211): that is a verdict, not "cannot verify".
        details["config_path_verified"] = False
        return CheckResult(
            check="repo",
            status="fail",
            message=_h(
                f"配置路徑不存在於分支 {branch}: {path}",
                f"Config path not found in branch {branch}: {path}"
            ),
            details=details,
        )
    else:
        # Not a hard failure — server may not support git archive --remote
        details["config_path_verified"] = False
        details["config_path_note"] = _h(
            "無法遠端驗證路徑（可能不支援 git archive --remote），請用 local 子命令確認",
            "Cannot verify path remotely (git archive --remote may not be supported), use local subcommand to confirm"
        )

    return CheckResult(
        check="repo",
        status="pass",  # the path check is best-effort when the server cannot answer
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
    base = Path(dir_path)

    # No directory to check is the caller's input, not a verdict (#3211):
    # the caller-error exit (rc 2, its --json envelope) via `run()`.
    # A path it cannot look at — EACCES under a parent the caller cannot
    # enter (#3241) — is named too, not a crash. `os.stat`, not
    # `Path.is_dir`: which errnos `is_dir` swallows changes across Python
    # versions (3.13 raises EACCES, 3.14 returns False), and "not there" is
    # decided here, by errno: ENOENT / ENOTDIR / ELOOP.
    try:
        is_dir = stat.S_ISDIR(os.stat(base).st_mode)
    except OSError as e:
        if e.errno not in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP):
            raise CallerError(_h(f"ERROR: 無法存取 --dir: {dir_path}（{e.strerror}）",
                                 f"ERROR: cannot access --dir: {dir_path} ({e.strerror})"),
                              reason="dir_not_accessible") from None
        is_dir = False
    if not is_dir:
        raise CallerError(_h(f"ERROR: --dir 不是目錄: {dir_path}",
                             f"ERROR: --dir is not a directory: {dir_path}"),
                          reason="dir_not_found")

    # ADR-036 step 4 (#2906): which files there are and what each holds is
    # `da-guard parse-tree`'s answer — the files the exporter selects, read
    # by yaml.v3 — never a Python walk or a PyYAML parse of the tree. No
    # usable da-guard raises ParseTreeError (exit 2 in `main`).
    reader = _lib_parse_cache.reader_for(base)
    tree = reader.tree()

    # #1588 / #1674: the root carrier the exporter reads, in any casing,
    # `.yaml` over `.yml` — parse-tree's `defaults_carriers`.
    defaults_rel = _lib_parse_cache.root_defaults_carrier(tree)
    if defaults_rel is None:
        return CheckResult(
            check="local",
            status="fail",
            message=_h(
                "缺少 _defaults.yaml（必需）",
                "Missing _defaults.yaml (required)"
            ),
            details=details,
        )

    # Every file the exporter reads (tenant files at any depth, and the
    # platform `_` files). A file it cannot read is a path da-guard validation
    # names: the shared conf.d refusal (#3206) named it and stopped the run at
    # `reader.tree()` above, so every file read here is readable.
    tenant_files = []
    total_alerts = 0
    tenant_set = set(_lib_parse_cache.tenant_files(tree))
    for rel in sorted(tree.selected):
        if rel == defaults_rel:
            continue
        if rel not in tenant_set:
            continue
        data = reader.read(rel)
        # Count metric keys (top-level keys without the `_` prefix)
        metric_count = sum(1 for k in data if not k.startswith("_"))
        tenant_files.append({"name": rel, "metrics": metric_count})
        total_alerts += metric_count
    not_read = sorted(f"{rel} ({why})" for rel, why in tree.not_read.items())
    if not_read:
        details["not_read"] = not_read

    # Build result
    details["defaults_file"] = "present"
    details["tenant_files"] = len(tenant_files)
    details["total_metrics"] = total_alerts

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

    Both reads are ``_lib_kubectl.kubectl_get`` by name (#3210; needs only
    ``get``): a read kubectl does not answer (cluster unreachable, forbidden,
    kubectl missing or timed out) raises ``KubectlError`` — a caller error,
    never "not deployed". None is the answer "absent", and anything absent
    is a fail.
    """
    details: dict[str, Any] = {"namespace": namespace}
    secret_exists = kubectl_get("secret", "git-sync-credentials", namespace=namespace) is not None
    details["git_sync_secret"] = "present" if secret_exists else "missing"

    deployment = kubectl_get("deployment", "threshold-exporter", namespace=namespace)
    deployments = [deployment] if deployment is not None else []
    containers = [str(c.get("name")) for d in deployments
                  for c in (((d.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or ()
                  if isinstance(c, dict)]
    has_sidecar = "git-sync" in containers
    details["deployment"] = "present" if deployments else "missing"
    details["sidecar_present"] = has_sidecar
    details["containers"] = containers

    if secret_exists and has_sidecar:
        return CheckResult(check="sidecar", status="pass", message=_h(
            "git-sync 邊車已部署，Secret 存在",
            "git-sync sidecar deployed, secret exists"), details=details)
    missing = [what for what, ok in (("Secret git-sync-credentials", secret_exists),
                                     ("git-sync container in Deployment threshold-exporter", has_sidecar))
               if not ok]
    return CheckResult(check="sidecar", status="fail", message=_h(
        "git-sync 配置缺件：" + "、".join(missing),
        "git-sync configuration missing: " + ", ".join(missing)), details=details)


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
    # #3094: a crash in --json mode still prints one envelope (`--json` lives
    # on every subcommand; no subcommand means no attribute)
    if getattr(args, "json", False):
        # dev-rules §13: an early exit keeps the report's keys, zeroed
        arm_crash_envelope(build=lambda: GitOpsReport(
            overall_status="", checks=[],
            timestamp=datetime.now(timezone.utc).isoformat()).to_dict())

    if not args.action:
        parser.print_help()
        sys.exit(EXIT_CALLER_ERROR)

    # Run appropriate check
    timestamp = datetime.now(timezone.utc).isoformat()

    checks = []
    if args.action == "repo":
        checks.append(check_repo(args.url, args.branch, args.path))
    elif args.action == "local":
        try:
            checks.append(check_local(args.dir))
        except ParseTreeError as exc:
            # ADR-036 step 4: no usable `da-guard parse-tree` — the tree was
            # not read, so there is no verdict: a caller error, not a fail.
            text = " ".join(str(exc).split())
            print(f"ERROR: cannot read {args.dir} through `da-guard parse-tree`: {text}",
                  file=sys.stderr)
            if args.json:
                print(format_json_report(GitOpsReport(
                    overall_status="fail", timestamp=timestamp,
                    checks=[CheckResult(check="local", status="fail", message=text,
                                        details={"directory": args.dir,
                                                 "reason": "da_guard_failed"})],
                ).to_dict()))
            sys.exit(EXIT_CALLER_ERROR)
    elif args.action == "sidecar":
        try:
            checks.append(check_sidecar(args.namespace))
        except KubectlError as exc:
            # #3210: kubectl gave no answer — no verdict: a caller error.
            text = " ".join(str(exc).split())
            print(f"ERROR: cannot read the cluster: {text}", file=sys.stderr)
            if args.json:
                print(format_json_report(GitOpsReport(
                    overall_status="fail", timestamp=timestamp,
                    checks=[CheckResult(check="sidecar", status="fail", message=text,
                                        details={"namespace": args.namespace,
                                                 "reason": KubectlError.reason})],
                ).to_dict()))
            sys.exit(EXIT_CALLER_ERROR)

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
        sys.exit(EXIT_OK if overall_status != "fail" else EXIT_VIOLATION)
    else:
        # Normal mode: fail on anything other than pass
        sys.exit(EXIT_OK if overall_status == "pass" else EXIT_VIOLATION)


if __name__ == "__main__":
    import _lib_exitcodes  # noqa: E402
    _lib_exitcodes.run(main)
