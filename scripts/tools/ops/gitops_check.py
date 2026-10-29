#!/usr/bin/env python3
"""gitops_check.py — local conf.d structure check.

``local`` checks a conf.d directory a GitOps repository carries:
_defaults.yaml presence, every file read through ``da-guard parse-tree``,
and the metric / tenant counts.

The ``repo`` and ``sidecar`` subcommands, and the CI-mode flag, were removed
(#3376, #3348): they checked GitOps Native Mode, which is refused since #1349 — the
chart deploys no git-sync container, so ``sidecar`` could only fail, and
``repo`` could not verify a path on GitHub (``git archive --remote`` is not
supported there). Bring them back with the mode, not before.

Usage:
    da-tools gitops-check local --dir <path> [--json]
"""
from __future__ import annotations

import argparse
import json
import os
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
from _lib_paths import classify_path  # noqa: E402  (#3276)
from _lib_parse_tree import ParseTreeError  # noqa: E402

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
    check: str  # 'local'
    status: str  # 'pass', 'fail'
    message: str
    details: Optional[dict[str, Any]] = None


@dataclass
class GitOpsReport:
    """Overall GitOps readiness report."""
    overall_status: str  # 'pass', 'fail'
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
def check_local(dir_path: str) -> CheckResult:
    """Check local configuration directory structure and validity."""
    details = {"directory": dir_path}
    base = Path(dir_path)

    # No directory to check is the caller's input, not a verdict (#3211):
    # the caller-error exit (rc 2, its --json envelope) via `run()`, named
    # by the shared judgement of a caller-given path (#3241 → #3276).
    problem = classify_path(dir_path, "dir", lang=_LANG, flag="--dir")
    if problem is not None:
        raise CallerError(_h(f"ERROR: --dir 無法使用：{problem.message}",
                             f"ERROR: {problem.message}"), problem.reason)

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
# Main entry point
# ---------------------------------------------------------------------------
def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="da-tools gitops-check",
        description=_h(
            "conf.d 本機結構檢查",
            "Local conf.d structure check"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    sub = parser.add_subparsers(dest="action", help=_h("檢查類型", "Check type"))

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

    args = parser.parse_args()
    # #3094: a crash in --json mode still prints one envelope (no subcommand
    # means no `json` attribute)
    if getattr(args, "json", False):
        # dev-rules §13: an early exit keeps the report's keys, zeroed
        arm_crash_envelope(build=lambda: GitOpsReport(
            overall_status="", checks=[],
            timestamp=datetime.now(timezone.utc).isoformat()).to_dict())

    if not args.action:
        parser.print_help()
        sys.exit(EXIT_CALLER_ERROR)

    timestamp = datetime.now(timezone.utc).isoformat()
    try:
        check = check_local(args.dir)
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

    report = GitOpsReport(overall_status=check.status, checks=[check], timestamp=timestamp)

    if args.json:
        print(format_json_report(report.to_dict()))
    else:
        status_symbol = "✓" if check.status == "pass" else "✗"
        print(f"{status_symbol} {check.message}")
        if check.details:
            for key, value in check.details.items():
                if key == "directory":
                    print(f"  {key}: {value}")
                elif isinstance(value, (int, str)):
                    print(f"  {key}: {value}")

    sys.exit(EXIT_OK if check.status == "pass" else EXIT_VIOLATION)


if __name__ == "__main__":
    import _lib_exitcodes  # noqa: E402
    _lib_exitcodes.run(main)
