#!/usr/bin/env bash
# Run a command with the three Go binaries check_admin_config_schema needs
# (ADR-036 §12):
#
#   $TENANT_API_BINARY   `tenant-api config check` judges _rbac / _tenant_orgs
#   $DA_CRDECODE_BINARY  reads the Helm values (embedded RBAC, identity claims)
#   $DA_GUARD_BINARY     `da-guard parse-tree` reads _domain_policy.yaml
#
# Each follows the contract of scripts/ops/with_da_guard.sh: set (non-blank)
# -> used as it is; otherwise `make <name>-build` builds .build/<name> from
# this checkout's Go source and that path is exported; without go the make
# target prints an ERROR naming the two ways out and this exits non-zero.
#
# Only tenant-api is handled here: da-crdecode and da-guard go through their
# own wrappers (with_da_crdecode.sh, with_da_guard.sh), so each "ensure a
# binary" rule exists once.
set -euo pipefail

if [ "$#" -eq 0 ]; then
    echo "usage: $0 <command> [args...]" >&2
    exit 2
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
set_to="${TENANT_API_BINARY:-}"
if [ -z "${set_to//[[:space:]]/}" ]; then
    make -s -C "$repo_root" tenant-api-build
    export TENANT_API_BINARY="$repo_root/.build/tenant-api"
fi
exec bash "$repo_root/scripts/ops/with_da_crdecode.sh" \
    bash "$repo_root/scripts/ops/with_da_guard.sh" "$@"
