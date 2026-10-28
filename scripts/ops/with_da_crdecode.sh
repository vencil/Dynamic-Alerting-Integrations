#!/usr/bin/env bash
# Run a command with $DA_CRDECODE_BINARY pointing at a da-crdecode (#3206).
#
# Used by the retire-drift check: check_retire_drift.py reads the namespaces
# manifest the way kubectl does, through da-crdecode (ADR-036 §12), so the
# pre-commit hook and the CI step need one. Same contract as with_da_guard.sh:
#
#   $DA_CRDECODE_BINARY set (non-blank)  -> used as it is; nothing is built.
#   otherwise                            -> `make da-crdecode-build` builds
#                                           .build/da-crdecode from this
#                                           checkout's Go source, and that
#                                           path is exported.
#
# No fallback past that: without go, `make da-crdecode-build` prints an ERROR
# naming the two ways out and exits non-zero, and so does this script.
set -euo pipefail

if [ "$#" -eq 0 ]; then
    echo "usage: $0 <command> [args...]" >&2
    exit 2
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
set_to="${DA_CRDECODE_BINARY:-}"
if [ -z "${set_to//[[:space:]]/}" ]; then
    make -s -C "$repo_root" da-crdecode-build
    export DA_CRDECODE_BINARY="$repo_root/.build/da-crdecode"
fi
exec "$@"
