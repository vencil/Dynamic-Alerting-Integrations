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
#      USER, which must not be the uid that owns the files. One run per row
#      of config-diff's exit-code contract (ADR-037 decision 4), judged on the
#      versioned `--format json` fields rather than on report wording:
#        both sides 0644 → rc 1, `threshold_changes` carries the changed key;
#                          the Markdown report (what customer pipelines
#                          post) is non-empty with rc 1.
#        base side 0600  → rc 1, every rendered view `not_compared`, the one
#                          `platform_settings` row says `_defaults.yaml` is
#                          unreadable, and NO threshold change is reported.
#                          This is the shape the retired config-diff.yaml
#                          burned on (a 0600 baseline read as rc 1 with an
#                          empty report, green).
#        PR side 0600    → rc 2, every rendered view `not_computed`. Both
#                          sides unreadable is handled as the PR side.
#
# Usage:  smoke_da_tools_image.sh <image-ref>
# Needs:  docker (logged in for private images) + python3.
# Exit:   0 all checks passed; 1 a check failed; 2 usage or setup error.
set -euo pipefail

if [ "$#" -ne 1 ] || [ -z "$1" ]; then
    echo "usage: smoke_da_tools_image.sh <image-ref>" >&2
    exit 2
fi
IMG="$1"
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
if ! image_uid="$(docker run --rm --platform "$PLATFORM" --entrypoint id "$IMG" -u)" \
        || [ -z "$image_uid" ]; then
    echo "::error::cannot read the uid ${IMG} runs as (docker run --entrypoint id failed)" >&2
    exit 2
fi
host_uid="$(id -u)"
if [ "$image_uid" = "$host_uid" ]; then
    # The 0600 case would then be readable and prove nothing about the uid
    # boundary a customer's runner and the image actually have.
    echo "::error::the image runs as uid $image_uid, the same uid that owns the staged files — the mount check would measure nothing" >&2
    exit 2
fi

# stage <base-mode> <pr-mode>
stage() {
    local side mode
    rm -rf "$work/old" "$work/new"
    for side in old new; do
        if [ "$side" = old ]; then mode="$1"; else mode="$2"; fi
        install -d -m 0755 "$work/$side"
        for f in "$FIXTURE/$side"/*.yaml; do
            install -m "$mode" "$f" "$work/$side/"
        done
    done
}

# config_diff [extra args…] — prints the rc; stdout/stderr land in $out/$err.
config_diff() {
    local rc=0
    docker run --rm --platform "$PLATFORM" \
        -v "$work/old:/data/old:ro" -v "$work/new:/data/new:ro" \
        "$IMG" config-diff --old-dir /data/old --new-dir /data/new "$@" \
        > "$out" 2> "$err" || rc=$?
    echo "$rc"
}

# json_check <case> — judges $out as config-diff JSON; prints what is wrong.
json_check() {
    python3 - "$1" "$out" <<'PY'
import json, sys
case, path = sys.argv[1], sys.argv[2]
try:
    doc = json.load(open(path, encoding="utf-8"))
except (OSError, ValueError) as exc:
    sys.exit(f"stdout is not JSON ({exc})")
if doc.get("schema") != "da-tools.config-diff/v1":
    sys.exit(f"schema is {doc.get('schema')!r}, this check reads da-tools.config-diff/v1")
views = {k: (v or {}).get("status") for k, v in (doc.get("views") or {}).items()
         if k != "platform"}
changed = [r.get("key") for r in doc.get("threshold_changes") or []]
rows = [(r.get("file"), r.get("change"), r.get("status"))
        for r in doc.get("platform_settings") or []]
want = {"ok": "computed", "base": "not_compared", "pr": "not_computed"}[case]
if not views or set(views.values()) != {want}:
    sys.exit(f"rendered views {views}, expected every one {want!r}")
if case == "ok" and "container_memory" not in changed:
    sys.exit(f"threshold_changes {changed} does not carry container_memory")
if case == "base":
    if changed:
        sys.exit(f"reported threshold changes {changed} from a base it could not read")
    if rows != [("_defaults.yaml", "unreadable", "not_compared")]:
        sys.exit(f"platform_settings {rows}, expected _defaults.yaml unreadable / not_compared")
PY
}

# check <label> <case> <expected-rc> — run JSON, judge rc then fields.
check() {
    local label="$1" case="$2" want="$3" rc why
    rc="$(config_diff --format json)"
    if [ "$rc" -ne "$want" ]; then
        fail "config-diff $label exited $rc, expected $want: $(head -c 400 "$err" | tr '\n' ' ')"
    elif ! why="$(json_check "$case" 2>&1)"; then
        fail "config-diff $label exited $want but its JSON is wrong: $why"
    fi
}

stage 0644 0644
rc="$(config_diff)"
if [ "$rc" -ne 1 ]; then
    fail "config-diff (Markdown) on readable trees exited $rc, expected 1: $(head -c 400 "$err" | tr '\n' ' ')"
elif [ ! -s "$out" ]; then
    fail "config-diff (Markdown) on readable trees exited 1 but wrote no report"
fi
check "on readable trees" ok 1

stage 0600 0644
check "with the base side unreadable to uid $image_uid" base 1

stage 0644 0600
check "with the PR side unreadable to uid $image_uid" pr 2

if [ "$failed" -ne 0 ]; then
    echo "✗ ${IMG} failed the da-tools release smoke" >&2
    exit 1
fi
echo "✓ ${IMG} passed the da-tools release smoke (uid $image_uid, $n subcommands)"
