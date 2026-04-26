#!/usr/bin/env bash
# Orchestration script for `make bench-e2e` — B-1 Phase 2 e2e harness.
#
# Per design §8.1: local-only (5-8 min cold start makes this inappropriate
# for per-PR CI). The companion .github/workflows/bench-e2e-record.yaml
# wires this into a manual-dispatch workflow on main only.
#
# Steps:
#   1. Ensure pre-requisite fixture exists for the chosen E2E_FIXTURE_KIND
#      — if not, generate synthetic-v2 with seed 42 (most common case).
#   2. Stage chosen fixture into tests/e2e-bench/fixture/active/conf.d/.
#   3. Pre-create bench-run-{0..COUNT} placeholder YAMLs (per design §5.1
#      — driver mutates these mid-run; pre-creating avoids fsnotify
#      create-vs-modify event-path divergence).
#   4. `docker compose up --build --abort-on-container-exit` until driver
#      exits naturally (after COUNT+1 runs).
#   5. `python3 aggregate.py` to compute P50/P95/P99 + bootstrap CI +
#      gate banner; output to tests/e2e-bench/bench-results/e2e-{ts}-{kind}.json.
#   6. `docker compose down -v` to clean up.

set -euo pipefail

# Resolve repo root from this script's location (scripts/ops/).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BENCH_DIR="$REPO_ROOT/tests/e2e-bench"

# Cleanup trap: any failure mid-script (set -e) leaves docker compose
# resources running on the host. Without this trap, the next
# `make bench-e2e` would hit port conflicts / volume residue. The trap
# only runs `compose down -v` if the script exits before reaching the
# explicit step-6 teardown — we use a flag so a successful run doesn't
# `down` twice (harmless but noisy).
_cleanup_done=0
_cleanup() {
    if [[ "$_cleanup_done" -eq 1 ]]; then
        return
    fi
    if [[ -d "$BENCH_DIR" ]]; then
        (cd "$BENCH_DIR" && docker compose down -v 2>/dev/null || true)
    fi
}
trap _cleanup EXIT

COUNT="${COUNT:-30}"
FIXTURE_KIND="${E2E_FIXTURE_KIND:-synthetic-v2}"
FIXTURE_TENANT_COUNT="${FIXTURE_TENANT_COUNT:-1000}"
SEED="${SEED:-42}"
BASELINE_GLOB="${BASELINE_GLOB:-}"  # e.g. for customer-anon to compare against synthetic-v2
GATE_THRESHOLD_PCT="${GATE_THRESHOLD_PCT:-30}"

cd "$BENCH_DIR"

echo "[bench-e2e] fixture_kind=$FIXTURE_KIND, tenants=$FIXTURE_TENANT_COUNT, runs=$COUNT, seed=$SEED"

# ---------------------------------------------------------------------------
# Step 1: ensure fixture for the chosen kind exists.
# ---------------------------------------------------------------------------
FIXTURE_SOURCE_DIR="fixture/$FIXTURE_KIND/conf.d"
# Count tenant YAMLs only (NOT _defaults.yaml — the placeholder
# `_defaults.yaml` is shipped in PR-2 to give scanner a hierarchical-mode
# root signal even before generation).
TENANT_FILE_COUNT=$(find "$FIXTURE_SOURCE_DIR" -name "*.yaml" -not -name "_defaults.yaml" 2>/dev/null | wc -l)
if [[ "$TENANT_FILE_COUNT" -eq 0 ]]; then
    if [[ "$FIXTURE_KIND" == "customer-anon" ]]; then
        echo "[bench-e2e] ERROR: customer-anon fixture not present in $FIXTURE_SOURCE_DIR"
        echo "[bench-e2e] See fixture/customer-anon/README.md for sample arrival protocol"
        exit 1
    fi
    layout_arg="hierarchical"
    if [[ "$FIXTURE_KIND" == "synthetic-v2" ]]; then
        layout_arg="synthetic-v2"
    fi
    # generate_tenant_fixture.py refuses to write into a non-empty dir
    # (sane guard against accidental clobber). The shipped placeholder
    # `_defaults.yaml` would trigger that refusal even though we WANT
    # to (re)generate. Clear the dir before invoking the generator;
    # `--with-defaults` will reinstate `_defaults.yaml` correctly.
    echo "[bench-e2e] fixture empty (only placeholder _defaults.yaml) — clearing + generating $FIXTURE_KIND ($FIXTURE_TENANT_COUNT tenants, seed=$SEED)..."
    rm -rf "$FIXTURE_SOURCE_DIR"
    python3 "$REPO_ROOT/scripts/tools/dx/generate_tenant_fixture.py" \
        --layout "$layout_arg" \
        --count "$FIXTURE_TENANT_COUNT" \
        --with-defaults \
        --output "$FIXTURE_SOURCE_DIR" \
        --seed "$SEED"
fi

# ---------------------------------------------------------------------------
# Step 2: stage fixture into active/.
# ---------------------------------------------------------------------------
echo "[bench-e2e] staging fixture into fixture/active/conf.d/"
rm -rf fixture/active/conf.d
mkdir -p fixture/active/conf.d
cp -r "$FIXTURE_SOURCE_DIR/." fixture/active/conf.d/

# ---------------------------------------------------------------------------
# Step 3: pre-create bench-run-{0..COUNT} placeholder tenants.
#
# Sentinel value (bench_trigger: "1") differs from what driver.py writes
# during the fire phase (bench_trigger: "100"). This guarantees the
# first fire-phase fixture write produces a real content-hash diff so
# `diffAndReload` actually triggers and `last_reload_complete_unixtime_
# seconds` advances → driver's T2 poll succeeds.
#
# History: first CI run (24937326270) wrote "100" here AND in the
# driver, producing identical bytes. Watcher saw mtime change, scan
# computed identical hash, no reload triggered → driver polled T2
# until timeout (60s) for every run, taking ~5 min/run × 6 runs before
# 30-min workflow timeout cancelled. Per-run-*.json all showed
# `T2_unix_ns: 0, stage_ab_skipped: true` for fire phase, the
# diagnostic that pinpointed the bug.
echo "[bench-e2e] pre-creating $((COUNT + 1)) bench-run-* placeholder tenants (sentinel value differs from driver write)"
for i in $(seq 0 "$COUNT"); do
    cat > "fixture/active/conf.d/bench-run-${i}.yaml" <<EOF
tenants:
  bench-run-${i}:
    bench_trigger: "1"
EOF
done

# ---------------------------------------------------------------------------
# Step 4: run compose stack until driver exits.
# ---------------------------------------------------------------------------
mkdir -p bench-results
echo "[bench-e2e] launching docker-compose (driver runs $((COUNT + 1)) cycles)..."
COUNT="$COUNT" E2E_FIXTURE_KIND="$FIXTURE_KIND" \
    docker compose up --build --abort-on-container-exit driver

# Driver exit code propagates via --abort-on-container-exit.
# If we get here, it succeeded.

# ---------------------------------------------------------------------------
# Step 5: aggregate.
# ---------------------------------------------------------------------------
echo "[bench-e2e] aggregating per-run JSONs..."
agg_args=(--results-dir bench-results --gate-threshold-pct "$GATE_THRESHOLD_PCT")
if [[ -n "$BASELINE_GLOB" ]]; then
    agg_args+=(--baseline-glob "$BASELINE_GLOB")
fi
python3 aggregate.py "${agg_args[@]}"

# ---------------------------------------------------------------------------
# Step 6: cleanup. Mark done so the EXIT trap doesn't double-down.
# ---------------------------------------------------------------------------
echo "[bench-e2e] tearing down stack..."
docker compose down -v
_cleanup_done=1

echo "[bench-e2e] done. Aggregate JSON in $BENCH_DIR/bench-results/"
