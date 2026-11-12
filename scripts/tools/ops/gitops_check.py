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
from _lib_python import detect_cli_lang  # noqa: E402
from _lib_exitcodes import (  # noqa: E402
    EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR, Report)
# ADR-036 step 4 (#2906): the conf.d is read through `da-guard parse-tree`.
import _lib_input  # noqa: E402
import _lib_parse_cache  # noqa: E402
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
    """Check local configuration directory structure and validity.

    ⛔ *dir_path* is the one `main` judged (`_lib_parse_cache.declared_trees`,
    #3751): no directory to check is the caller's input, not a verdict
    (#3211, #3241 → #3276), refused there naming --dir — never judged again
    here; the tree is read through the run's reader of it (`reader_for`)."""
    details = {"directory": dir_path}
    base = Path(dir_path)

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
def main() -> "Report | None":
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
    # #3751: a conf.d with no config file is the check's own fail (no
    # `_defaults.yaml`, rc 1), as before; the path and a root da-guard
    # cannot list are refused naming the flag.
    _lib_input.add_input(
        local_parser, "--dir", select=_lib_input.DA_GUARD_LISTS,
        empty=_lib_input.EMPTY_ALLOWED, required=True,
        help=_h("配置目錄路徑", "Config directory path"),
    )
    local_parser.add_argument(
        "--json",
        action="store_true",
        help=_h("輸出 JSON 格式", "Output JSON format"),
    )

    args = parser.parse_args()

    if not args.action:
        parser.print_help()
        sys.exit(EXIT_CALLER_ERROR)

    timestamp = datetime.now(timezone.utc).isoformat()
    # One reader of --dir for the run: the one `declared_trees` judged.
    with _lib_parse_cache.session():
        return _local(args, timestamp)


def _local(args: argparse.Namespace, timestamp: str) -> "Report | None":
    try:
        # #3751: judged once — the path, a root da-guard cannot list — each
        # refusal naming --dir (a CallerError: rc 2 through `run()`, nothing
        # on stdout).
        _lib_parse_cache.declared_trees(args, "dir", lang=_LANG)
        check = check_local(str(args.dir))  # the value judged, as the caller wrote it
    except ParseTreeError as exc:
        # ADR-036 step 4: no usable `da-guard parse-tree` — the tree was
        # not read, so there is no verdict: a caller error, not a fail.
        text = " ".join(str(exc).split())
        print(f"ERROR: cannot read {args.dir} through `da-guard parse-tree`: {text}",
              file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    report = GitOpsReport(overall_status=check.status, checks=[check], timestamp=timestamp)

    status = EXIT_OK if check.status == "pass" else EXIT_VIOLATION
    if args.json:
        return Report(report.to_dict(), status)  # #3216: run() encodes it
    status_symbol = "✓" if check.status == "pass" else "✗"
    print(f"{status_symbol} {check.message}")
    if check.details:
        for key, value in check.details.items():
            if key == "directory":
                print(f"  {key}: {value}")
            elif isinstance(value, (int, str)):
                print(f"  {key}: {value}")

    sys.exit(status)


if __name__ == "__main__":
    import _lib_exitcodes  # noqa: E402
    _lib_exitcodes.run(main)
