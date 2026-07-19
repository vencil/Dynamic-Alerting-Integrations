"""Canonical exit-code contract for da-tools CLI tools (#452 Track A).

Single source of truth for the 0/1/2 exit-code convention shared by the
Python tool suite and the Go binaries (da-guard / da-parser / da-batchpr).
Before #452 the convention lived in three divergent ad-hoc spots
(_lib_godispatch._EXIT_CALLER_ERROR, diag_pr_ci.EXIT_*, trufflehog_to_sarif
.EXIT_*); this module unifies the base contract so call sites and the
test gate (tests/shared/test_tool_exit_codes.py) don't drift.

Go equivalent (stable contract, mirrored here):
  components/threshold-exporter/app/cmd/da-guard/main.go  (// Exit codes ...)
  components/threshold-exporter/app/cmd/da-parser/main.go

Semantics
---------
EXIT_OK (0)
    Clean run. Nothing to fix; the tool did its job and found no problem.

EXIT_VIOLATION (1)
    The tool ran correctly and found something the USER must act on:
    rule violations, drift detected, validation failures, findings,
    per-target failures, `--ci` / `--strict` fail-on-finding. CI gates
    treat this as "fail the check". User-actionable.

EXIT_CALLER_ERROR (2)
    The tool could NOT do its job because of how it was invoked or its
    environment: bad/missing args, file/path not found, cannot reach
    Prometheus / API, malformed input YAML/JSON, missing prerequisite
    binary, IO failure, or an unexpected crash. System-actionable
    (fix the invocation or environment, then retry). argparse already
    exits 2 on unrecognised flags, which is consistent with this.

Allowed extensions
------------------
A tool MAY define higher codes (>= 3) for finer-grained caller-error
subtypes, as long as 0/1/2 keep the meanings above. The only sanctioned
>= 3 extension today is diag_pr_ci.py's EXIT_NETWORK_BLOCKED = 3 (documented
in docs/internal/windows-mcp-playbook.md trap #64 — "switch host" hint,
distinct from exit 2 "gh missing/unauthenticated").

One tool — dx/tenant_verify.py — INVERTS 1/2 (exit 2 = verification
finding, exit 1 = caller error) as a sanctioned pre-SSOT exception: its
codes are load-bearing in a shipped customer rollback runbook that keys on
"exit 2 = mismatch" (see that file's header for the full rationale). It is
the sole exception; do NOT repurpose 1 or 2 anywhere else — new tools MUST
follow the base violation / caller-error meanings above.

New subcommands MUST follow this contract — see docs/internal/dev-rules.md.
"""
from __future__ import annotations

import sys
from typing import Final, NoReturn

# Import-time stdout hardening (cp950-class consoles): every da-tools CLI
# imports at least one of the four root libs (_lib_compat / _lib_python /
# _lib_godispatch / this module) at module level — i.e. before
# argparse.parse_args() can print --help — so chain-importing _lib_compat
# here guarantees unencodable characters degrade instead of crashing.
# Gate: tests/shared/test_console_encoding_resilience.py
import _lib_compat  # noqa: F401  (import-time side effect; see _lib_compat)

EXIT_OK: Final[int] = 0
EXIT_VIOLATION: Final[int] = 1
EXIT_CALLER_ERROR: Final[int] = 2


def die_caller_error(message: str) -> NoReturn:
    """Print *message* to stderr and exit EXIT_CALLER_ERROR (2).

    Convenience for the common "bad invocation / unusable environment"
    bail-out so call sites don't hand-roll the print + sys.exit(2) pair.
    """
    print(message, file=sys.stderr)
    sys.exit(EXIT_CALLER_ERROR)
