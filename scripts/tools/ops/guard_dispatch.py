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
  defaults-impact   Validate a conf.d/ tree (mapped to da-guard --config-dir ...)

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
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_python import detect_cli_lang  # noqa: E402

_LANG = detect_cli_lang()

# Subcommands that map straight to a da-guard invocation. The
# subcommand name → da-guard flag set is intentionally narrow:
# we don't second-guess flag values, just route.
_GUARD_SUBCOMMANDS = {
    "defaults-impact",
}


def _msg(en: str, zh: str) -> str:
    """Pick the right language."""
    return zh if _LANG == "zh" else en


def _print_usage() -> None:
    print(_msg(
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
        "      --scope conf.d/db/ --cardinality-limit 500 --format json\n",
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
    ))


def _resolve_binary(args: list[str]) -> tuple[str | None, list[str]]:
    """Find the da-guard binary and return (path, remaining_args).

    Strips --da-guard-binary from the arg list before forwarding.
    Empty path means not found; caller prints a helpful error.
    """
    # Walk args looking for --da-guard-binary in either form.
    cleaned: list[str] = []
    explicit: str | None = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--da-guard-binary":
            if i + 1 >= len(args):
                # Let argparse-equivalent error bubble: the Go
                # binary will reject this anyway, so we just drop
                # the trailing flag and let downstream complain.
                i += 1
                continue
            explicit = args[i + 1]
            i += 2
            continue
        if a.startswith("--da-guard-binary="):
            explicit = a.split("=", 1)[1]
            i += 1
            continue
        cleaned.append(a)
        i += 1

    if explicit:
        return (explicit if os.path.isfile(explicit) else None), cleaned
    env_override = os.environ.get("DA_GUARD_BINARY", "").strip()
    if env_override:
        return (env_override if os.path.isfile(env_override) else None), cleaned
    found = shutil.which("da-guard")
    return found, cleaned


def _print_binary_missing(explicit_attempt: str | None) -> None:
    if explicit_attempt:
        print(_msg(
            f"Error: da-guard binary not found at {explicit_attempt!r}.\n",
            f"錯誤: 在 {explicit_attempt!r} 找不到 da-guard 執行檔。\n"
        ), file=sys.stderr)
    else:
        print(_msg(
            "Error: da-guard binary not found.\n"
            "Resolution order:\n"
            "  1. --da-guard-binary <path>\n"
            "  2. $DA_GUARD_BINARY env var\n"
            "  3. da-guard on $PATH\n"
            "\n"
            "Install options:\n"
            "  - Download from https://github.com/vencil/Dynamic-Alerting-Integrations/releases\n"
            "    (look for tools/v* release assets when v2.8.0 ships)\n"
            "  - Build from source:\n"
            "      cd components/threshold-exporter/app && \\\n"
            "          go build -o /usr/local/bin/da-guard ./cmd/da-guard\n",
            "錯誤: 找不到 da-guard 執行檔。\n"
            "解析順序:\n"
            "  1. --da-guard-binary <path>\n"
            "  2. $DA_GUARD_BINARY 環境變數\n"
            "  3. PATH 中的 da-guard\n"
            "\n"
            "安裝方式:\n"
            "  - 從 https://github.com/vencil/Dynamic-Alerting-Integrations/releases 下載\n"
            "    (v2.8.0 釋出後請看 tools/v* release assets)\n"
            "  - 從原始碼編譯:\n"
            "      cd components/threshold-exporter/app && \\\n"
            "          go build -o /usr/local/bin/da-guard ./cmd/da-guard\n"
        ), file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    """Dispatch da-tools guard <subcommand> args to da-guard binary.

    argv is sys.argv[1:] when called as a script; injectable for tests.
    The first element MUST be the subcommand (entrypoint.py drops the
    'guard' word before forwarding).
    """
    if argv is None:
        argv = sys.argv[1:]

    if not argv or argv[0] in ("-h", "--help", "help"):
        _print_usage()
        return 0

    subcmd = argv[0]
    if subcmd not in _GUARD_SUBCOMMANDS:
        print(_msg(
            f"Error: unknown guard subcommand '{subcmd}'.\n"
            f"Available: {', '.join(sorted(_GUARD_SUBCOMMANDS))}\n"
            "Run `da-tools guard --help` for usage.\n",
            f"錯誤: 未知的 guard 子命令 '{subcmd}'。\n"
            f"可用子命令: {', '.join(sorted(_GUARD_SUBCOMMANDS))}\n"
            "執行 `da-tools guard --help` 查看用法。\n"
        ), file=sys.stderr)
        return 2

    binary, forward_args = _resolve_binary(argv[1:])
    if binary is None:
        # Recover the explicit path the user attempted (if any) for
        # the error message — peek at original argv before we
        # stripped it.
        explicit = None
        for i, a in enumerate(argv):
            if a == "--da-guard-binary" and i + 1 < len(argv):
                explicit = argv[i + 1]
                break
            if a.startswith("--da-guard-binary="):
                explicit = a.split("=", 1)[1]
                break
        _print_binary_missing(explicit)
        return 2

    # Subcommand → flag passthrough. The Go binary has no
    # `defaults-impact` subcommand string itself; the subcommand is
    # an organising layer on the Python side. So strip it before
    # forwarding.
    cmd = [binary] + forward_args
    try:
        # Inherit stdio so the report streams straight to the user.
        # No timeout: guard validation across thousands of tenant configs
        # may legitimately take 10+ minutes; user CTRL-C is the contract.
        result = subprocess.run(cmd, check=False)  # subprocess-timeout: ignore
        return result.returncode
    except FileNotFoundError:
        # Race: binary disappeared between resolve and exec. Treat
        # as missing.
        _print_binary_missing(binary)
        return 2
    except OSError as e:
        print(_msg(
            f"Error: failed to execute da-guard: {e}\n",
            f"錯誤: 執行 da-guard 失敗: {e}\n"
        ), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
