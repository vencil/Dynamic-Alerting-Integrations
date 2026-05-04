#!/usr/bin/env python3
"""
guard_dispatch.py — `da-tools guard` Python entrypoint.

Forwards `da-tools guard <subcommand> [...]` to the `da-guard` Go
binary (built from components/threshold-exporter/app/cmd/da-guard).
Python is the user-facing CLI wrapper that everyone already runs;
the actual guard logic lives in Go for two reasons:

  1. The guard library (components/.../internal/guard) operates on
     the same effective-config maps the threshold-exporter produces
     at runtime — duplicating in Python would invite drift.
  2. ADR-018 deepMerge semantics live in pkg/config/. Python
     reproductions of that merge (the Python toolkit has its own,
     used by `describe_tenant.py`) are golden-fixture-tested for
     parity but every additional caller multiplies the contract
     surface. Shelling out is one process boundary, not a contract.

Subcommands:
  defaults-impact   Validate a conf.d/ tree (mapped to
                    da-guard --config-dir ...). The subcommand name
                    is a Python-side organising layer; da-guard
                    itself takes flags directly with no subcommand.

Resolution order for the `da-guard` binary:
  1. --da-guard-binary <path>   (explicit override)
  2. $DA_GUARD_BINARY env var
  3. `da-guard` on $PATH        (typical: shipped via tools/v* release)
  4. Friendly error with install instructions

Exit codes (passthrough from da-guard):
  0  clean
  1  guard found errors
  2  caller error / binary missing

Usage:
  da-tools guard defaults-impact --config-dir conf.d/
  da-tools guard defaults-impact --config-dir conf.d/ --scope conf.d/db/ \\
      --required-fields cpu,memory --cardinality-limit 500

v2.8.0 PR-2: dispatcher boilerplate (binary resolution, subcommand
allowlist, bilingual help, missing-binary hints, subprocess passthrough)
moved to _lib_godispatch.GoBinaryDispatcher. This file is the per-tool
config + main() entry only.
"""
from __future__ import annotations

import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_godispatch import GoBinaryDispatcher  # noqa: E402

_USAGE_EN = (
    "Usage: da-tools guard <subcommand> [flags]\n"
    "\n"
    "Subcommands:\n"
    "  defaults-impact   Validate a conf.d/ tree against the C-12 Dangling\n"
    "                    Defaults Guard (schema + routing + cardinality).\n"
    "\n"
    "Flags (most common; full list via `da-tools guard defaults-impact --help`):\n"
    "  --config-dir <path>          Required. conf.d/ root.\n"
    "  --scope <path>               Sub-directory to validate (default: whole tree).\n"
    "  --required-fields <a,b,c>    Comma-separated dotted paths every tenant must have.\n"
    "  --cardinality-limit <n>      Per-tenant predicted-metric ceiling (0 disables).\n"
    "  --format md|json             Output format (default md).\n"
    "  --output <path>              Write report to file instead of stdout.\n"
    "  --warn-as-error              Treat warnings as errors for exit code.\n"
    "\n"
    "Binary resolution:\n"
    "  1. --da-guard-binary <path>\n"
    "  2. $DA_GUARD_BINARY env var\n"
    "  3. `da-guard` on $PATH (shipped via tools/v* release)\n"
    "\n"
    "Examples:\n"
    "  da-tools guard defaults-impact --config-dir conf.d/\n"
    "  da-tools guard defaults-impact --config-dir conf.d/ \\\n"
    "      --scope conf.d/db/ --cardinality-limit 500 --format json\n"
)

_USAGE_ZH = (
    "用法: da-tools guard <子命令> [選項]\n"
    "\n"
    "子命令:\n"
    "  defaults-impact   依 C-12 Dangling Defaults Guard 規則\n"
    "                    驗證 conf.d/ 樹 (schema + routing + cardinality)。\n"
    "\n"
    "常用選項 (完整選項見 `da-tools guard defaults-impact --help`):\n"
    "  --config-dir <path>          必填，conf.d/ 根目錄。\n"
    "  --scope <path>               限定驗證的子目錄 (預設: 整棵樹)。\n"
    "  --required-fields <a,b,c>    每個租戶 effective config 必有的點分路徑欄位 (CSV)。\n"
    "  --cardinality-limit <n>      每租戶 metric 數上限 (0 = 關閉檢查)。\n"
    "  --format md|json             輸出格式 (預設 md)。\n"
    "  --output <path>              寫到檔案而非 stdout。\n"
    "  --warn-as-error              將 warning 視為 error 影響 exit code。\n"
    "\n"
    "Binary 解析順序:\n"
    "  1. --da-guard-binary <path>\n"
    "  2. $DA_GUARD_BINARY 環境變數\n"
    "  3. PATH 中的 `da-guard` (由 tools/v* release 提供)\n"
    "\n"
    "範例:\n"
    "  da-tools guard defaults-impact --config-dir conf.d/\n"
    "  da-tools guard defaults-impact --config-dir conf.d/ \\\n"
    "      --scope conf.d/db/ --cardinality-limit 500 --format json\n"
)

# guard's subcommand is a Python-side organising layer — da-guard
# itself takes flags directly without a subcommand string. So we
# strip the subcommand before forwarding (pass_subcommand=False).
_DISPATCHER = GoBinaryDispatcher(
    binary_name="da-guard",
    cli_alias="guard",
    binary_flag="--da-guard-binary",
    env_var="DA_GUARD_BINARY",
    subcommands={"defaults-impact"},
    pass_subcommand=False,
    usage_en=_USAGE_EN,
    usage_zh=_USAGE_ZH,
)


def main(argv: list[str] | None = None) -> int:
    """Dispatch da-tools guard <subcommand> args to da-guard binary.

    argv is sys.argv[1:] when called as a script; injectable for tests.
    The first element MUST be the subcommand (entrypoint.py drops the
    'guard' word before forwarding).
    """
    if argv is None:
        argv = sys.argv[1:]
    return _DISPATCHER.dispatch(argv)


if __name__ == "__main__":
    sys.exit(main())
