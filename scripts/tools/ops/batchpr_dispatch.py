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
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_python import detect_cli_lang  # noqa: E402

_LANG = detect_cli_lang()

# Subcommands map straight to a da-batchpr invocation. Validation
# only — the Go binary owns the per-subcommand flag schema.
_BATCHPR_SUBCOMMANDS = {
    "apply",
    "refresh",
    "refresh-source",
}


def _msg(en: str, zh: str) -> str:
    """Pick the right language."""
    return zh if _LANG == "zh" else en


def _print_usage() -> None:
    print(_msg(
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
        "      --patches-dir ./patches/ --workdir ./repo\n",
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
    ))


def _resolve_binary(args: list[str]) -> tuple[str | None, list[str]]:
    """Find the da-batchpr binary and return (path, remaining_args).

    Strips --da-batchpr-binary from the arg list before forwarding.
    None path means not found; caller prints a helpful error.
    """
    cleaned: list[str] = []
    explicit: str | None = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--da-batchpr-binary":
            if i + 1 >= len(args):
                # Trailing flag without a value — drop it; downstream
                # would have rejected anyway. We still need to NOT
                # forward the bare flag.
                i += 1
                continue
            explicit = args[i + 1]
            i += 2
            continue
        if a.startswith("--da-batchpr-binary="):
            explicit = a.split("=", 1)[1]
            i += 1
            continue
        cleaned.append(a)
        i += 1

    if explicit:
        return (explicit if os.path.isfile(explicit) else None), cleaned
    env_override = os.environ.get("DA_BATCHPR_BINARY", "").strip()
    if env_override:
        return (env_override if os.path.isfile(env_override) else None), cleaned
    found = shutil.which("da-batchpr")
    return found, cleaned


def _print_binary_missing(explicit_attempt: str | None) -> None:
    if explicit_attempt:
        print(_msg(
            f"Error: da-batchpr binary not found at {explicit_attempt!r}.\n",
            f"錯誤: 在 {explicit_attempt!r} 找不到 da-batchpr 執行檔。\n"
        ), file=sys.stderr)
    else:
        print(_msg(
            "Error: da-batchpr binary not found.\n"
            "Resolution order:\n"
            "  1. --da-batchpr-binary <path>\n"
            "  2. $DA_BATCHPR_BINARY env var\n"
            "  3. da-batchpr on $PATH\n"
            "\n"
            "Install options:\n"
            "  - Download from https://github.com/vencil/Dynamic-Alerting-Integrations/releases\n"
            "    (look for tools/v* release assets when v2.8.0 ships)\n"
            "  - Build from source:\n"
            "      cd components/threshold-exporter/app && \\\n"
            "          go build -o /usr/local/bin/da-batchpr ./cmd/da-batchpr\n",
            "錯誤: 找不到 da-batchpr 執行檔。\n"
            "解析順序:\n"
            "  1. --da-batchpr-binary <path>\n"
            "  2. $DA_BATCHPR_BINARY 環境變數\n"
            "  3. PATH 中的 da-batchpr\n"
            "\n"
            "安裝方式:\n"
            "  - 從 https://github.com/vencil/Dynamic-Alerting-Integrations/releases 下載\n"
            "    (v2.8.0 釋出後請看 tools/v* release assets)\n"
            "  - 從原始碼編譯:\n"
            "      cd components/threshold-exporter/app && \\\n"
            "          go build -o /usr/local/bin/da-batchpr ./cmd/da-batchpr\n"
        ), file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    """Dispatch da-tools batch-pr <subcommand> args to da-batchpr binary.

    argv is sys.argv[1:] when called as a script; injectable for tests.
    The first element MUST be the subcommand (entrypoint.py drops the
    'batch-pr' word before forwarding).
    """
    if argv is None:
        argv = sys.argv[1:]

    if not argv or argv[0] in ("-h", "--help", "help"):
        _print_usage()
        return 0

    subcmd = argv[0]
    if subcmd not in _BATCHPR_SUBCOMMANDS:
        print(_msg(
            f"Error: unknown batch-pr subcommand '{subcmd}'.\n"
            f"Available: {', '.join(sorted(_BATCHPR_SUBCOMMANDS))}\n"
            "Run `da-tools batch-pr --help` for usage.\n",
            f"錯誤: 未知的 batch-pr 子命令 '{subcmd}'。\n"
            f"可用子命令: {', '.join(sorted(_BATCHPR_SUBCOMMANDS))}\n"
            "執行 `da-tools batch-pr --help` 查看用法。\n"
        ), file=sys.stderr)
        return 2

    binary, forward_args = _resolve_binary(argv[1:])
    if binary is None:
        # Recover the explicit path the user attempted (if any) for
        # the error message — peek at original argv before stripping.
        explicit = None
        for i, a in enumerate(argv):
            if a == "--da-batchpr-binary" and i + 1 < len(argv):
                explicit = argv[i + 1]
                break
            if a.startswith("--da-batchpr-binary="):
                explicit = a.split("=", 1)[1]
                break
        _print_binary_missing(explicit)
        return 2

    # Subcommand IS forwarded — unlike guard_dispatch.py. The
    # da-batchpr binary is itself a multi-subcommand dispatcher
    # (cmd/da-batchpr/main.go switches on os.Args[1]).
    cmd = [binary, subcmd] + forward_args
    try:
        # Inherit stdio so the report streams straight to the user.
        # No timeout: batch-PR apply across 10K rules / hundreds of dirs
        # may legitimately take 10+ minutes; user CTRL-C is the contract.
        result = subprocess.run(cmd, check=False)  # subprocess-timeout: ignore
        return result.returncode
    except FileNotFoundError:
        _print_binary_missing(binary)
        return 2
    except OSError as e:
        print(_msg(
            f"Error: failed to execute da-batchpr: {e}\n",
            f"錯誤: 執行 da-batchpr 失敗: {e}\n"
        ), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
