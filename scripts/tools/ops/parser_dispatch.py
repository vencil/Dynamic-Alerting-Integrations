#!/usr/bin/env python3
"""
parser_dispatch.py — `da-tools parser` Python entrypoint.

Forwards `da-tools parser <subcommand> [...]` to the `da-parser`
Go binary (built from components/threshold-exporter/app/cmd/da-parser).
Python is the user-facing CLI wrapper that everyone already runs;
the actual rule parsing + dialect classification lives in Go for two
reasons:

  1. `metricsql.Parse` and `prometheus/promql/parser` are Go-only;
     a Python re-implementation would either reach for Cython /
     bindings (build complexity) or duplicate parser semantics
     (maintenance trap).
  2. The Go library (components/threshold-exporter/internal/parser)
     is the single source of truth for ParsedRule shape, dialect
     classification, and the VM-only function allowlist. Shelling
     out is one process boundary, not a contract.

Subcommands (mirrors `da-parser <subcommand> [flags]` directly):

  import       Parse PrometheusRule YAML(s) into a JSON ParseResult
               for downstream C-9 Profile Builder consumption.
  allowlist    Print the embedded VM-only function allowlist (text
               or json format). Useful for customer audits and for
               building local allowlist diff alarms.

Like `batchpr_dispatch.py` (and unlike `guard_dispatch.py`), the
subcommand IS preserved in the forwarded args — `da-parser` itself
takes subcommands at the binary boundary.

Resolution order for the `da-parser` binary:

  1. --da-parser-binary <path>     (explicit override)
  2. $DA_PARSER_BINARY env var
  3. `da-parser` on $PATH          (typical: shipped via tools/v* release)
  4. Friendly error with install instructions

Exit codes (passthrough from da-parser):

  0  parse OK, no portability gate failures
  1  --fail-on-non-portable / --fail-on-ambiguous gate triggered
  2  caller error (bad flags, missing/invalid path, IO failure,
                   binary missing)

Usage:

  da-tools parser import --input rules.yaml > parsed.json
  da-tools parser import --input rules.yaml --fail-on-non-portable
  da-tools parser allowlist --format json
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

# Subcommands map straight to a da-parser invocation. Validation
# only — the Go binary owns the per-subcommand flag schema.
_PARSER_SUBCOMMANDS = {
    "import",
    "allowlist",
}


def _msg(en: str, zh: str) -> str:
    """Pick the right language."""
    return zh if _LANG == "zh" else en


def _print_usage() -> None:
    print(_msg(
        "Usage: da-tools parser <subcommand> [flags]\n"
        "\n"
        "Subcommands:\n"
        "  import          Parse PrometheusRule YAML into JSON ParseResult.\n"
        "                  Required: --input <path|->. Common flags: --output,\n"
        "                  --validate-strict-prom, --fail-on-non-portable,\n"
        "                  --fail-on-ambiguous, --generated-by.\n"
        "  allowlist       Print the embedded VM-only function allowlist.\n"
        "                  --format text|json (default text).\n"
        "\n"
        "Common flags (full list via `da-tools parser <subcommand> --help`):\n"
        "  --input <path>         PrometheusRule YAML; '-' = stdin.\n"
        "  --output <path>        JSON ParseResult; '-' = stdout (default).\n"
        "  --validate-strict-prom Run prometheus/promql/parser per rule (default true).\n"
        "  --fail-on-non-portable Exit 1 if any rule prom_compatible=false.\n"
        "  --fail-on-ambiguous    Exit 1 if any rule dialect=ambiguous.\n"
        "\n"
        "Binary resolution:\n"
        "  1. --da-parser-binary <path>\n"
        "  2. $DA_PARSER_BINARY env var\n"
        "  3. `da-parser` on $PATH (shipped via tools/v* release)\n"
        "\n"
        "Examples:\n"
        "  da-tools parser import --input rules.yaml > parsed.json\n"
        "  da-tools parser import --input - --fail-on-non-portable < rules.yaml\n"
        "  da-tools parser allowlist --format json\n",
        "用法: da-tools parser <子命令> [選項]\n"
        "\n"
        "子命令:\n"
        "  import          解析 PrometheusRule YAML，輸出 JSON ParseResult。\n"
        "                  必填: --input <path|->. 常用: --output、\n"
        "                  --validate-strict-prom、--fail-on-non-portable、\n"
        "                  --fail-on-ambiguous、--generated-by。\n"
        "  allowlist       列印內嵌的 VM-only 函數白名單。\n"
        "                  --format text|json (預設 text)。\n"
        "\n"
        "常用選項 (完整選項見 `da-tools parser <子命令> --help`):\n"
        "  --input <path>         PrometheusRule YAML 檔；'-' = stdin。\n"
        "  --output <path>        JSON ParseResult 路徑；'-' = stdout (預設)。\n"
        "  --validate-strict-prom 對每條規則跑 prometheus/promql/parser (預設 true)。\n"
        "  --fail-on-non-portable 任一 rule prom_compatible=false 即 exit 1。\n"
        "  --fail-on-ambiguous    任一 rule dialect=ambiguous 即 exit 1。\n"
        "\n"
        "Binary 解析順序:\n"
        "  1. --da-parser-binary <path>\n"
        "  2. $DA_PARSER_BINARY 環境變數\n"
        "  3. PATH 中的 `da-parser` (由 tools/v* release 提供)\n"
        "\n"
        "範例:\n"
        "  da-tools parser import --input rules.yaml > parsed.json\n"
        "  da-tools parser import --input - --fail-on-non-portable < rules.yaml\n"
        "  da-tools parser allowlist --format json\n"
    ))


def _resolve_binary(args: list[str]) -> tuple[str | None, list[str]]:
    """Find the da-parser binary and return (path, remaining_args).

    Strips --da-parser-binary from the arg list before forwarding.
    None path means not found; caller prints a helpful error.
    """
    cleaned: list[str] = []
    explicit: str | None = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--da-parser-binary":
            if i + 1 >= len(args):
                # Trailing flag without a value — drop it; downstream
                # would have rejected anyway. We still need to NOT
                # forward the bare flag.
                i += 1
                continue
            explicit = args[i + 1]
            i += 2
            continue
        if a.startswith("--da-parser-binary="):
            explicit = a.split("=", 1)[1]
            i += 1
            continue
        cleaned.append(a)
        i += 1

    if explicit:
        return (explicit if os.path.isfile(explicit) else None), cleaned
    env_override = os.environ.get("DA_PARSER_BINARY", "").strip()
    if env_override:
        return (env_override if os.path.isfile(env_override) else None), cleaned
    found = shutil.which("da-parser")
    return found, cleaned


def _print_binary_missing(explicit_attempt: str | None) -> None:
    if explicit_attempt:
        print(_msg(
            f"Error: da-parser binary not found at {explicit_attempt!r}.\n",
            f"錯誤: 在 {explicit_attempt!r} 找不到 da-parser 執行檔。\n"
        ), file=sys.stderr)
    else:
        print(_msg(
            "Error: da-parser binary not found.\n"
            "Resolution order:\n"
            "  1. --da-parser-binary <path>\n"
            "  2. $DA_PARSER_BINARY env var\n"
            "  3. da-parser on $PATH\n"
            "\n"
            "Install options:\n"
            "  - Download from https://github.com/vencil/Dynamic-Alerting-Integrations/releases\n"
            "    (look for tools/v* release assets when v2.8.0 ships)\n"
            "  - Build from source:\n"
            "      cd components/threshold-exporter/app && \\\n"
            "          go build -o /usr/local/bin/da-parser ./cmd/da-parser\n",
            "錯誤: 找不到 da-parser 執行檔。\n"
            "解析順序:\n"
            "  1. --da-parser-binary <path>\n"
            "  2. $DA_PARSER_BINARY 環境變數\n"
            "  3. PATH 中的 da-parser\n"
            "\n"
            "安裝方式:\n"
            "  - 從 https://github.com/vencil/Dynamic-Alerting-Integrations/releases 下載\n"
            "    (v2.8.0 釋出後請看 tools/v* release assets)\n"
            "  - 從原始碼編譯:\n"
            "      cd components/threshold-exporter/app && \\\n"
            "          go build -o /usr/local/bin/da-parser ./cmd/da-parser\n"
        ), file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    """Dispatch da-tools parser <subcommand> args to da-parser binary.

    argv is sys.argv[1:] when called as a script; injectable for tests.
    The first element MUST be the subcommand (entrypoint.py drops the
    'parser' word before forwarding).
    """
    if argv is None:
        argv = sys.argv[1:]

    if not argv or argv[0] in ("-h", "--help", "help"):
        _print_usage()
        return 0

    subcmd = argv[0]
    if subcmd not in _PARSER_SUBCOMMANDS:
        print(_msg(
            f"Error: unknown parser subcommand '{subcmd}'.\n"
            f"Available: {', '.join(sorted(_PARSER_SUBCOMMANDS))}\n"
            "Run `da-tools parser --help` for usage.\n",
            f"錯誤: 未知的 parser 子命令 '{subcmd}'。\n"
            f"可用子命令: {', '.join(sorted(_PARSER_SUBCOMMANDS))}\n"
            "執行 `da-tools parser --help` 查看用法。\n"
        ), file=sys.stderr)
        return 2

    binary, forward_args = _resolve_binary(argv[1:])
    if binary is None:
        # Recover the explicit path the user attempted (if any) for
        # the error message — peek at original argv before stripping.
        explicit = None
        for i, a in enumerate(argv):
            if a == "--da-parser-binary" and i + 1 < len(argv):
                explicit = argv[i + 1]
                break
            if a.startswith("--da-parser-binary="):
                explicit = a.split("=", 1)[1]
                break
        _print_binary_missing(explicit)
        return 2

    # Subcommand IS forwarded — like batchpr_dispatch.py. The
    # da-parser binary is itself a multi-subcommand dispatcher
    # (cmd/da-parser/main.go switches on os.Args[1]).
    cmd = [binary, subcmd] + forward_args
    try:
        # Inherit stdio so the JSON streams straight to the user.
        # No timeout: parsing 10K+ rules may legitimately take 10+ min;
        # user CTRL-C is the contract.
        result = subprocess.run(cmd, check=False)  # subprocess-timeout: ignore
        return result.returncode
    except FileNotFoundError:
        _print_binary_missing(binary)
        return 2
    except OSError as e:
        print(_msg(
            f"Error: failed to execute da-parser: {e}\n",
            f"錯誤: 執行 da-parser 失敗: {e}\n"
        ), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
