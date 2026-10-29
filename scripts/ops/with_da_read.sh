#!/usr/bin/env bash
# Run a command with $DA_READ_BINARY pointing at a da-read (#3206).
#
# Used by the scrape-reachability check: check_scrape_reachability.py reads
# the Prometheus config the way Prometheus does, through `da-read prometheus`
# (ADR-036 §12), so the pre-commit hook needs one. Same contract as
# with_da_crdecode.sh / with_da_guard.sh:
#
#   $DA_READ_BINARY set (non-blank)  -> used as it is; nothing is built.
#   otherwise                        -> `make da-read-build` builds
#                                       .build/da-read from this checkout's
#                                       Go source (components/da-read), and
#                                       that path is exported.
#
# No fallback past that: without go, `make da-read-build` prints an ERROR
# naming the two ways out and exits non-zero, and so does this script.
set -euo pipefail

if [ "$#" -eq 0 ]; then
    echo "usage: $0 <command> [args...]" >&2
    exit 2
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
set_to="${DA_READ_BINARY:-}"
if [ -z "${set_to//[[:space:]]/}" ]; then
    make -s -C "$repo_root" da-read-build
    export DA_READ_BINARY="$repo_root/.build/da-read"
fi
exec "$@"
