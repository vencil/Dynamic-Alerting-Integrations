#!/usr/bin/env python3
"""
batchpr_dispatch.py — `da-tools batch-pr` Python entrypoint.

Forwards `da-tools batch-pr <subcommand> [...]` to the `da-batchpr`
Go binary (built from components/threshold-exporter/app/cmd/da-batchpr).
Python is the user-facing CLI wrapper that everyone already runs;
the actual batch-PR orchestration lives in Go for two reasons:

  1. The orchestration shells out to git + gh CLIs and handles
     per-PR / per-target failure isolation. A Python re-implementation
     would duplicate the shell-out boundary + the in-memory orchestration
     against the same interfaces.
  2. The Go library (components/threshold-exporter/internal/batchpr)
     is the single source of truth for `Apply()` / `Refresh()` /
     `RefreshSource()` semantics. Shelling out is one process boundary,
     not a contract.

Subcommands (mirrors `da-batchpr <subcommand> [flags]` directly):

  apply           Open or update tenant chunk PRs from a Plan +
                  C-9 emit output.
  refresh         Rebase tenant branches after Base PR merges
                  (PR-3 mode).
  refresh-source  Re-apply data-layer hot-fix files into existing
                  tenant branches (PR-4 mode).

Unlike `guard_dispatch.py`, the subcommand IS preserved in the
forwarded args — `da-batchpr` itself takes subcommands at the binary
boundary (matches the dispatcher pattern in cmd/da-batchpr/main.go).

Resolution order for the `da-batchpr` binary:

  1. --da-batchpr-binary <path>   (explicit override)
  2. $DA_BATCHPR_BINARY env var
  3. `da-batchpr` on $PATH        (typical: shipped via tools/v* release)
  4. Friendly error with install instructions

Exit codes (passthrough from da-batchpr):

  0  clean run, all targets succeeded or skipped acceptably
  1  per-target failures (or refresh conflicts)
  2  caller error (bad flags, missing/invalid path, IO failure,
                   binary missing)

Usage:

  da-tools batch-pr apply --plan plan.json --emit-dir ./emit/ \\
      --repo owner/name --workdir ./repo
  da-tools batch-pr refresh --input refresh.json --workdir ./repo
  da-tools batch-pr refresh-source --input refresh-source.json \\
      --patches-dir ./patches/ --workdir ./repo

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
    "Usage: da-tools batch-pr <subcommand> [flags]\n"
    "\n"
    "Subcommands:\n"
    "  apply           Open or update tenant chunk PRs from a Plan +\n"
    "                  C-9 emit output. Requires --plan, --emit-dir,\n"
    "                  --repo, --workdir.\n"
    "  refresh         Rebase tenant branches after Base PR merges.\n"
    "                  Requires --input (RefreshInput JSON) + --workdir.\n"
    "  refresh-source  Re-apply data-layer hot-fix files into existing\n"
    "                  tenant branches. Requires --input + --patches-dir\n"
    "                  + --workdir.\n"
    "\n"
    "Common flags (full list via `da-tools batch-pr <subcommand> --help`):\n"
    "  --report <path>        Markdown report ('-' = stdout, default '-').\n"
    "  --result-json <path>   JSON result ('-' = stdout, '' = skip; default skip).\n"
    "  --dry-run              Run orchestration without git/GitHub API calls.\n"
    "\n"
    "Binary resolution:\n"
    "  1. --da-batchpr-binary <path>\n"
    "  2. $DA_BATCHPR_BINARY env var\n"
    "  3. `da-batchpr` on $PATH (shipped via tools/v* release)\n"
    "\n"
    "Examples:\n"
    "  da-tools batch-pr apply \\\n"
    "      --plan plan.json --emit-dir ./emit/ \\\n"
    "      --repo vencil/customer --workdir ./customer-repo\n"
    "  da-tools batch-pr refresh --input refresh.json --workdir ./repo\n"
    "  da-tools batch-pr refresh-source \\\n"
    "      --input refresh-source.json \\\n"
    "      --patches-dir ./patches/ --workdir ./repo\n"
)

_USAGE_ZH = (
    "用法: da-tools batch-pr <子命令> [選項]\n"
    "\n"
    "子命令:\n"
    "  apply           從 Plan + C-9 emit 輸出開出或更新 tenant chunk PR。\n"
    "                  必填: --plan, --emit-dir, --repo, --workdir。\n"
    "  refresh         Base PR merge 後 rebase tenant branches。\n"
    "                  必填: --input (RefreshInput JSON) + --workdir。\n"
    "  refresh-source  把 data-layer hot-fix 檔案套進現有 tenant branches。\n"
    "                  必填: --input + --patches-dir + --workdir。\n"
    "\n"
    "常用選項 (完整選項見 `da-tools batch-pr <子命令> --help`):\n"
    "  --report <path>        Markdown 報表 ('-' = stdout, 預設 '-')。\n"
    "  --result-json <path>   JSON 結果 ('-' = stdout, '' = skip; 預設 skip)。\n"
    "  --dry-run              跑 orchestration 但不執行 git/GitHub API。\n"
    "\n"
    "Binary 解析順序:\n"
    "  1. --da-batchpr-binary <path>\n"
    "  2. $DA_BATCHPR_BINARY 環境變數\n"
    "  3. PATH 中的 `da-batchpr` (由 tools/v* release 提供)\n"
    "\n"
    "範例:\n"
    "  da-tools batch-pr apply \\\n"
    "      --plan plan.json --emit-dir ./emit/ \\\n"
    "      --repo vencil/customer --workdir ./customer-repo\n"
    "  da-tools batch-pr refresh --input refresh.json --workdir ./repo\n"
    "  da-tools batch-pr refresh-source \\\n"
    "      --input refresh-source.json \\\n"
    "      --patches-dir ./patches/ --workdir ./repo\n"
)

# da-batchpr is itself a multi-subcommand dispatcher (cmd/da-batchpr/
# main.go switches on os.Args[1]); forward the subcommand as-is.
_DISPATCHER = GoBinaryDispatcher(
    binary_name="da-batchpr",
    cli_alias="batch-pr",
    binary_flag="--da-batchpr-binary",
    env_var="DA_BATCHPR_BINARY",
    subcommands={"apply", "refresh", "refresh-source"},
    pass_subcommand=True,
    usage_en=_USAGE_EN,
    usage_zh=_USAGE_ZH,
)


def main(argv: list[str] | None = None) -> int:
    """Dispatch da-tools batch-pr <subcommand> args to da-batchpr binary.

    argv is sys.argv[1:] when called as a script; injectable for tests.
    The first element MUST be the subcommand (entrypoint.py drops the
    'batch-pr' word before forwarding).
    """
    if argv is None:
        argv = sys.argv[1:]
    return _DISPATCHER.dispatch(argv)


if __name__ == "__main__":
    sys.exit(main())
