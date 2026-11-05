# shellcheck shell=bash
# Sourced, not run: `ensure_da_binary <da-guard|da-read|da-crdecode>`.
#
# Same contract as with_da_guard.sh / with_da_read.sh / with_da_crdecode.sh,
# for scripts that run many commands and cannot wrap each one:
#
#   $DA_<NAME>_BINARY set (non-blank) -> used as it is; nothing is built.
#   otherwise                         -> `make <name>-build` builds
#                                        .build/<name> from this checkout's Go
#                                        source, and that path is exported.
#
# A failed build returns non-zero after naming the reader and the variable;
# the caller decides to stop. The route generator needs all three
# (tests/dx/test_shell_scripts_provide_da_binaries.py measures it).

_DA_BINARIES_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

ensure_da_binary() {
  local name="$1" var
  case "$name" in
    da-guard) var=DA_GUARD_BINARY ;;
    da-read) var=DA_READ_BINARY ;;
    da-crdecode) var=DA_CRDECODE_BINARY ;;
    *) echo "ensure_da_binary: unknown reader '$name'" >&2; return 2 ;;
  esac
  local set_to="${!var:-}"
  if [ -n "${set_to//[[:space:]]/}" ]; then
    return 0
  fi
  if ! make -C "$_DA_BINARIES_REPO" --no-print-directory "${name}-build"; then
    echo "ERROR: could not build ${name} (see above) — set ${var} to one, or install Go" >&2
    return 1
  fi
  export "${var}=${_DA_BINARIES_REPO}/.build/${name}"
}
