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
    "  da-tools parser allowlist --format json\n"
)

_USAGE_ZH = (
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
)

# da-parser is itself a multi-subcommand dispatcher (cmd/da-parser/
# main.go switches on os.Args[1]); forward the subcommand as-is.
_DISPATCHER = GoBinaryDispatcher(
    binary_name="da-parser",
    cli_alias="parser",
    binary_flag="--da-parser-binary",
    env_var="DA_PARSER_BINARY",
    subcommands={"import", "allowlist"},
    pass_subcommand=True,
    usage_en=_USAGE_EN,
    usage_zh=_USAGE_ZH,
)


def main(argv: list[str] | None = None) -> int:
    """Dispatch da-tools parser <subcommand> args to da-parser binary.

    argv is sys.argv[1:] when called as a script; injectable for tests.
    The first element MUST be the subcommand (entrypoint.py drops the
    'parser' word before forwarding).
    """
    if argv is None:
        argv = sys.argv[1:]
    return _DISPATCHER.dispatch(argv)


if __name__ == "__main__":
    sys.exit(main())
