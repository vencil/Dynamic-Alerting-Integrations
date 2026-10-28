#!/usr/bin/env bash
# Run a command with the three Go binaries check_admin_config_schema needs
# (ADR-036 §12):
#
#   $TENANT_API_BINARY   `tenant-api config check` judges _rbac / _tenant_orgs
#   $DA_CRDECODE_BINARY  reads the Helm values (embedded RBAC, identity claims)
#   $DA_GUARD_BINARY     `da-guard parse-tree` reads _domain_policy.yaml
#
# Each, as in scripts/ops/with_da_guard.sh:
#
#   set (non-blank)  -> used as it is; nothing is built.
#   otherwise        -> `make <name>-build` builds .build/<name> from this
#                       checkout's Go source, and that path is exported.
#
# No fallback past that: without go the make target prints an ERROR naming the
# two ways out and exits non-zero, and so does this script. It rebuilds every
# run for the reason the Makefile gives: reusing an earlier build would check
# against the old Go semantics after the source changed.
set -euo pipefail

if [ "$#" -eq 0 ]; then
    echo "usage: $0 <command> [args...]" >&2
    exit 2
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

ensure() {
    local var="$1" target="$2" out="$3"
    local set_to="${!var:-}"
    if [ -z "${set_to//[[:space:]]/}" ]; then
        make -s -C "$repo_root" "$target"
        export "$var=$repo_root/.build/$out"
    fi
}

ensure TENANT_API_BINARY tenant-api-build tenant-api
ensure DA_CRDECODE_BINARY da-crdecode-build da-crdecode
ensure DA_GUARD_BINARY da-guard-build da-guard
exec "$@"
