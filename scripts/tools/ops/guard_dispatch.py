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
  2. ADR-017 deepMerge semantics live in pkg/config/. Python
     reproductions of that merge (the Python toolkit has its own,
     used by `describe_tenant.py`) are golden-fixture-tested for
     parity but every additional caller multiplies the contract
     surface. Shelling out is one process boundary, not a contract.

Subcommands:
  defaults-impact   Validate a conf.d/ tree (mapped to
                    da-guard --config-dir ...). The subcommand name
                    is a Python-side organising layer; da-guard's
                    default mode takes flags directly.
  served-values     Print, as JSON, the values the exporter's /metrics
                    serves per tenant (#2115; forwarded as
                    da-guard served-values ...). The reader library is
                    scripts/tools/_lib_tenant_values.py.
  effective         Print, as JSON, every tenant's effective config as
                    tenant-api's /effective resolves it, with profile
                    binding and per-key sources (#2564; forwarded as
                    da-guard effective ...). Read by load_effective in
                    scripts/tools/_lib_tenant_values.py.

Resolution order for the `da-guard` binary:
  1. --da-guard-binary <path>   (explicit override)
  2. $DA_GUARD_BINARY env var
  3. `da-guard` on $PATH        (typical: shipped via tools/v* release)
  4. Friendly error with install instructions

Exit codes (passthrough from da-guard; the stable contract is the comment
at the top of cmd/da-guard/main.go):
  0  clean
  1  guard found errors
  2  caller error / binary missing
  3  config files the exporter cannot decode (#2123)

Usage:
  da-tools guard served-values --config-dir conf.d/ [--at 2026-07-01T03:00:00Z]
  da-tools guard effective --config-dir conf.d/
  da-tools guard defaults-impact --config-dir conf.d/
  da-tools guard defaults-impact --config-dir conf.d/ --scope conf.d/db/ \\
      --required-fields cpu,memory

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
    "  served-values     Print, as JSON, the values the exporter's /metrics\n"
    "                    serves per tenant (--config-dir, optional --at RFC3339).\n"
    "  effective         Print, as JSON, every tenant's effective config as\n"
    "                    tenant-api's /effective resolves it, with profile\n"
    "                    binding and per-key sources (--config-dir).\n"
    "\n"
    "Flags (most common; full list via `da-tools guard defaults-impact --help`):\n"
    "  --config-dir <path>          Required. conf.d/ root.\n"
    "  --scope <path>               Sub-directory to validate (default: whole tree).\n"
    "  --required-fields <a,b,c>    Comma-separated dotted paths every tenant must have.\n"
    "  --cardinality-limit <n>      Per-tenant predicted-metric ceiling (0 disables).\n"
    "                               Omitted: the root _defaults.yaml's\n"
    "                               max_metrics_per_tenant (unset = 500).\n"
    "  --format md|json             Output format (default md).\n"
    "  --output <path>              Write report to file instead of stdout.\n"
    "  --warn-as-error              Treat warnings as errors for exit code.\n"
    "\n"
    "Exit codes (from da-guard):\n"
    "  0  clean    1  guard found errors    2  caller error / binary missing\n"
    "  3  config files the exporter cannot decode (see docs/cli-reference.md §guard)\n"
    "\n"
    "Binary resolution:\n"
    "  1. --da-guard-binary <path>\n"
    "  2. $DA_GUARD_BINARY env var\n"
    "  3. `da-guard` on $PATH (shipped via tools/v* release)\n"
    "\n"
    "Examples:\n"
    "  da-tools guard defaults-impact --config-dir conf.d/\n"
    "  da-tools guard defaults-impact --config-dir conf.d/ \\\n"
    "      --scope conf.d/db/ --format json\n"
)

_USAGE_ZH = (
    "用法: da-tools guard <子命令> [選項]\n"
    "\n"
    "子命令:\n"
    "  defaults-impact   依 C-12 Dangling Defaults Guard 規則\n"
    "                    驗證 conf.d/ 樹 (schema + routing + cardinality)。\n"
    "  served-values     以 JSON 印出 exporter /metrics 對每個租戶實際發出的值\n"
    "                    (--config-dir，可加 --at RFC3339)。\n"
    "  effective         以 JSON 印出每個租戶在 tenant-api /effective 的有效設定，\n"
    "                    含綁定的 profile 與每個 key 的來源 (--config-dir)。\n"
    "\n"
    "常用選項 (完整選項見 `da-tools guard defaults-impact --help`):\n"
    "  --config-dir <path>          必填，conf.d/ 根目錄。\n"
    "  --scope <path>               限定驗證的子目錄 (預設: 整棵樹)。\n"
    "  --required-fields <a,b,c>    每個租戶 effective config 必有的點分路徑欄位 (CSV)。\n"
    "  --cardinality-limit <n>      每租戶 metric 數上限 (0 = 關閉檢查)。\n"
    "                               省略時取根目錄 _defaults.yaml 的\n"
    "                               max_metrics_per_tenant (未設 = 500)。\n"
    "  --format md|json             輸出格式 (預設 md)。\n"
    "  --output <path>              寫到檔案而非 stdout。\n"
    "  --warn-as-error              將 warning 視為 error 影響 exit code。\n"
    "\n"
    "Exit code (來自 da-guard):\n"
    "  0  通過    1  guard 偵測到 error    2  caller error / 找不到 binary\n"
    "  3  exporter 無法 decode 的設定檔（見 docs/cli-reference.md §guard）\n"
    "\n"
    "Binary 解析順序:\n"
    "  1. --da-guard-binary <path>\n"
    "  2. $DA_GUARD_BINARY 環境變數\n"
    "  3. PATH 中的 `da-guard` (由 tools/v* release 提供)\n"
    "\n"
    "範例:\n"
    "  da-tools guard defaults-impact --config-dir conf.d/\n"
    "  da-tools guard defaults-impact --config-dir conf.d/ \\\n"
    "      --scope conf.d/db/ --format json\n"
)

# `defaults-impact` is a Python-side organising layer — da-guard's
# default mode takes flags directly — so it is stripped before
# forwarding (pass_subcommand=False). `served-values` and `effective`
# (#2564) are real da-guard subcommands and are forwarded.
_DISPATCHER = GoBinaryDispatcher(
    binary_name="da-guard",
    cli_alias="guard",
    binary_flag="--da-guard-binary",
    env_var="DA_GUARD_BINARY",
    subcommands={"defaults-impact", "served-values", "effective"},
    pass_subcommand=False,
    usage_en=_USAGE_EN,
    usage_zh=_USAGE_ZH,
    forwarded_subcommands=frozenset({"served-values", "effective"}),
)

# The same resolution, for _lib_tenant_values (which calls da-guard as a
# library rather than as a da-tools subcommand).
DISPATCHER = _DISPATCHER


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
