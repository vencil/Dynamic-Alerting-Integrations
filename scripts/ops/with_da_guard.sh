#!/usr/bin/env bash
# Run a command with $DA_GUARD_BINARY pointing at a da-guard (#2115 0-B/B4).
#
# Used by the platform-data-check pre-commit hook: generate_platform_data.py
# reads the tenants through `da-guard served-values`, so the hook needs one.
#
#   $DA_GUARD_BINARY set (non-blank)  -> used as it is; nothing is built.
#   otherwise                         -> `make da-guard-build` builds
#                                        .build/da-guard from this checkout's
#                                        Go source, and that path is exported.
#
# No fallback past that: without go, `make da-guard-build` prints an ERROR
# naming the two ways out and exits non-zero, and so does this script. It
# rebuilds every run (the go build cache makes an unchanged tree take about a
# second) for the reason the Makefile gives: reusing an earlier build would
# check against the old Go semantics after the source changed.
set -euo pipefail

if [ "$#" -eq 0 ]; then
    echo "usage: $0 <command> [args...]" >&2
    exit 2
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
set_to="${DA_GUARD_BINARY:-}"
if [ -z "${set_to//[[:space:]]/}" ]; then
    make -s -C "$repo_root" da-guard-build
    export DA_GUARD_BINARY="$repo_root/.build/da-guard"
fi
exec "$@"
