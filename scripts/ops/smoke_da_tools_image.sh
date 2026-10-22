#!/usr/bin/env bash
# smoke_da_tools_image.sh — #3258: run the candidate da-tools image before it
# is promoted, so a packaging defect fails the release instead of reaching a
# customer's pipeline first.
#
# Called from `.github/workflows/release.yaml` (release-da-tools) on the
# candidate DIGEST, after Trivy and before signing / promotion. Nothing else in
# the release executes the image, and the PR-time image build stubs out tools/
# and the Go binaries, so without this step an entrypoint that no longer
# matches COMMAND_MAP, a missing Python dependency, or a container uid that
# cannot read the mounted files is first seen by a customer.
#
# What it runs, each as its own `docker run` the way a customer runs it:
#   1. `da-tools --help`, then `<cmd> --help` for every COMMAND_MAP key read
#      from this checkout's entrypoint.py (the source the image was built
#      from). rc must be 0 and stdout non-empty.
#   2. `config-diff` on tests/fixtures/da-tools-smoke/{old,new} — the trees
#      differ only in `_defaults.yaml` — mounted read-only, as the image's own
#      USER, which must not be the uid that owns the files:
#        files 0644 → rc 1 and a report naming `_defaults.yaml` and the
#                     changed key;
#        files 0600 → rc 2: an unreadable file is a caller error, never
#                     "changes found" (rc 1) or "no changes" (rc 0).
#
# Usage:  smoke_da_tools_image.sh <image-ref>
# Needs:  docker (logged in for private images) + python3.
# Exit:   0 all checks passed; 1 a check failed; 2 usage or setup error.
set -euo pipefail

IMG="${1:?usage: smoke_da_tools_image.sh <image-ref>}"
PLATFORM="linux/amd64"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENTRYPOINT="$ROOT/components/da-tools/app/entrypoint.py"
FIXTURE="$ROOT/tests/fixtures/da-tools-smoke"

for side in old new; do
    [ -d "$FIXTURE/$side" ] || { echo "::error::fixture tree $FIXTURE/$side is missing" >&2; exit 2; }
done

cmds_file="$(mktemp)"; out="$(mktemp)"; err="$(mktemp)"
# `mktemp -d` is 0700, which the container user cannot traverse; the
# directories are opened up explicitly so only the FILE mode varies.
work="$(mktemp -d)"
trap 'rm -rf "$work" "$out" "$err" "$cmds_file"' EXIT
chmod 0755 "$work"

# COMMAND_MAP is read with ast, not by importing entrypoint.py: the import
# runs the module's top level, and a dict literal is all this needs.
if ! python3 - "$ENTRYPOINT" > "$cmds_file" <<'PY'
import ast, sys
tree = ast.parse(open(sys.argv[1], encoding="utf-8").read())
maps = [n.value for n in tree.body if isinstance(n, ast.Assign)
        and any(getattr(t, "id", None) == "COMMAND_MAP" for t in n.targets)]
if len(maps) != 1:
    sys.exit(f"expected one COMMAND_MAP assignment in {sys.argv[1]}, found {len(maps)}")
keys = list(ast.literal_eval(maps[0]))
if not keys:
    sys.exit("COMMAND_MAP is empty")
print("\n".join(keys))
PY
then
    echo "::error::cannot read COMMAND_MAP from $ENTRYPOINT" >&2
    exit 2
fi

failed=0
fail() { echo "::error::$*" >&2; failed=1; }

# ── 1. every subcommand starts ────────────────────────────────────────────
help_check() {
    local label="$1"; shift
    local rc=0
    docker run --rm --platform "$PLATFORM" "$IMG" "$@" > "$out" 2> "$err" || rc=$?
    if [ "$rc" -ne 0 ]; then
        fail "$label exited $rc: $(head -c 400 "$err" | tr '\n' ' ')"
    elif [ ! -s "$out" ]; then
        fail "$label exited 0 but printed nothing on stdout"
    fi
}

help_check "da-tools --help" --help
n=0
while IFS= read -r cmd; do
    help_check "da-tools $cmd --help" "$cmd" --help
    n=$((n + 1))
done < "$cmds_file"
echo "▸ --help ran for $n COMMAND_MAP subcommand(s)"

# ── 2. config-diff reads mounted trees as the image's own user ────────────
image_uid="$(docker run --rm --platform "$PLATFORM" --entrypoint id "$IMG" -u)"
host_uid="$(id -u)"
if [ "$image_uid" = "$host_uid" ]; then
    # The 0600 case would then be readable and prove nothing about the uid
    # boundary a customer's runner and the image actually have.
    echo "::error::the image runs as uid $image_uid, the same uid that owns the staged files — the mount check would measure nothing" >&2
    exit 2
fi

stage() {
    local mode="$1" side
    rm -rf "$work/old" "$work/new"
    for side in old new; do
        install -d -m 0755 "$work/$side"
        for f in "$FIXTURE/$side"/*.yaml; do
            install -m "$mode" "$f" "$work/$side/"
        done
    done
}

config_diff() {
    local rc=0
    docker run --rm --platform "$PLATFORM" \
        -v "$work/old:/data/old:ro" -v "$work/new:/data/new:ro" \
        "$IMG" config-diff --old-dir /data/old --new-dir /data/new \
        > "$out" 2> "$err" || rc=$?
    echo "$rc"
}

stage 0644
rc="$(config_diff)"
if [ "$rc" -ne 1 ]; then
    fail "config-diff on 0644 files exited $rc, expected 1 (changes found): $(head -c 400 "$err" | tr '\n' ' ')"
elif [ ! -s "$out" ]; then
    fail "config-diff on 0644 files exited 1 but wrote no report"
elif ! grep -q '_defaults.yaml' "$out" || ! grep -q 'container_memory' "$out"; then
    fail "config-diff on 0644 files exited 1 but its report names neither _defaults.yaml nor container_memory"
fi

stage 0600
rc="$(config_diff)"
if [ "$rc" -ne 2 ]; then
    fail "config-diff on 0600 files (unreadable to uid $image_uid) exited $rc, expected 2 — rc 0/1 would read as a verdict about files it could not open"
fi

if [ "$failed" -ne 0 ]; then
    echo "✗ ${IMG} failed the da-tools release smoke" >&2
    exit 1
fi
echo "✓ ${IMG} passed the da-tools release smoke (uid $image_uid, $n subcommands)"
