# E2E alert fire-through harness (B-1 Phase 2)

Local-only harness measuring end-to-end alert latency through the full
stack: tenant config write → exporter scan/reload → Prometheus scrape +
evaluation → Alertmanager dispatch → webhook receiver. Implements the
5-anchor measurement protocol from
[`docs/internal/design/phase-b-e2e-harness.md`](../../docs/internal/design/phase-b-e2e-harness.md).

This is **PR-2 of 3** in the B-1 Phase 2 rollout — it stands up the
stack and produces per-run JSON. Aggregation (P50/P95/P99 + bootstrap
CI) and `make bench-e2e` Makefile target arrive in PR-3.

## Why local-only (not CI)

Per design doc §8.1: cold start (compose up + healthcheck + 30 runs ×
2 phases + teardown) is ~5–8 minutes wall-clock. Running on every PR
would dominate CI cost without producing actionable signal. PR-3 will
gate this to a manually-dispatched workflow on `main`; for now,
maintainer or operator runs on demand.

## Quick start (≥ 30-tenant fixture, ~5 min wall-clock)

```bash
cd tests/e2e-bench

# 1. Generate a fixture once (PR-1 tool — already merged in PR #78).
python3 ../../scripts/tools/dx/generate_tenant_fixture.py \
    --layout synthetic-v2 --count 1000 --with-defaults \
    --output fixture/synthetic-v2/conf.d --seed 42

# 2. Stage the chosen fixture into the run-time `active` dir.
mkdir -p fixture/active/conf.d
cp -r fixture/synthetic-v2/conf.d/* fixture/active/conf.d/

# 3. Pre-create the bench-run-{i} placeholder files (per design §5.1
#    — driver mutates these mid-run; pre-creating avoids fsnotify
#    create-vs-modify event-path divergence).
for i in $(seq 0 30); do
    cat > "fixture/active/conf.d/bench-run-${i}.yaml" <<EOF
tenants:
  bench-run-${i}:
    bench_trigger: "100"
EOF
done

# 4. Run the harness. COUNT=N means 1 warm-up run + N measurement runs.
#    `--abort-on-container-exit driver` waits specifically for *driver*
#    to exit (it terminates naturally after COUNT+1 cycles), avoiding a
#    bring-down on receiver/exporter unrelated exits.
COUNT=30 docker compose up --build --abort-on-container-exit driver

# 5. Inspect per-run outputs and run aggregator.
ls bench-results/
cat bench-results/per-run-0001.json | jq
python3 aggregate.py
cat bench-results/e2e-*.json | jq

# 6. Cleanup.
docker compose down -v
```

**Easier path** (PR-3 integration): from repo root, `make bench-e2e`
runs the entire 1-6 sequence with EXIT-trap cleanup (failure mid-
script doesn't leave a dangling stack). See [`scripts/ops/bench_e2e_run.sh`](../../scripts/ops/bench_e2e_run.sh).

## Fixture kinds

Three kinds, controlled by `E2E_FIXTURE_KIND` env (default `synthetic-v2`):

| Kind | Source | Use |
|---|---|---|
| `synthetic-v1` | `--layout hierarchical` (uniform) | Phase 1 baseline reuse for delta comparisons |
| `synthetic-v2` | `--layout synthetic-v2` (Zipf+power-law) | Phase 2 main baseline (default) |
| `customer-anon` | Out-of-band customer delivery (gitignored) | Calibration gate ±30% comparator |

See `fixture/customer-anon/README.md` for the customer sample arrival
protocol.

## Per-run output schema

Each run writes `bench-results/per-run-{run_id:04d}.json` containing
the 5-anchor breakdown for both fire and resolve phases:

```json
{
  "run_id": 7,
  "warm_up": false,
  "fixture_kind": "synthetic-v2",
  "gate_status": "pending",
  "fire": {
    "T0_unix_ns": 1714032001000000000,
    "T1_unix_ns": 1714032001050000000,
    "T2_unix_ns": 1714032001195000000,
    "T3_unix_ns": 1714032005120000000,
    "T4_unix_ns": 1714032005165000000,
    "stage_ms": {"A": 50, "B": 145, "C": 3925, "D": 45},
    "e2e_ms": 4165,
    "stage_ab_skipped": false
  },
  "resolve": {
    "T0_unix_ns": 1714032015000000000,
    "T1_unix_ns": 1714032015048000000,
    "T2_unix_ns": 1714032015190000000,
    "T3_unix_ns": 1714032020110000000,
    "T4_unix_ns": 1714032020155000000,
    "stage_ms": {"A": -1, "B": -1, "C": 4920, "D": 45},
    "e2e_ms": 5155,
    "stage_ab_skipped": true
  }
}
```

`stage_ms` value `-1` = stage skipped or upstream anchor missing.
Per design §5.2 the resolve phase has `stage_ab_skipped=true` because
the resolve path doesn't mutate the fixture (no scan/reload), so A/B
are not measurable in the strict sense.

## Components

| Service | Image / Build | Role |
|---|---|---|
| `threshold-exporter` | `build: ../../components/threshold-exporter/app` | Watches `fixture/active/conf.d/`, emits `user_threshold` + new `da_config_last_{scan,reload}_complete_unixtime_seconds` gauges (T1/T2 anchors, added in PR #78) |
| `prometheus` | `prom/prometheus:v2.55.0` | Scrapes exporter + pushgateway; evaluates `actual_metric_value > user_threshold` rule |
| `pushgateway` | `prom/pushgateway:v1.10.0` | Receives driver-injected `actual_metric_value{tenant=bench-run-N}` |
| `alertmanager` | `prom/alertmanager:v0.27.0` | Routes firing/resolved alerts to receiver (`send_resolved: true`) |
| `receiver` | `build: ./receiver` | Custom Go ring-buffer webhook target; `/posts?since=...&tenant_id=...` query API |
| `driver` | `build: ./driver` | Python orchestrator; runs **inside** compose so all timestamps share the host kernel clock (no skew) |

## Run isolation contract

Each driver run uses tenant `bench-run-{i}` (i in 0..COUNT). All metrics
and alerts carry that label. Alertmanager `group_by: [tenant]` keeps
each run in its own group → no cross-run dedup interference. Pushgateway
metrics are explicitly DELETEd in the driver's `finally:` block to
prevent stale state from bleeding into the next run.

## Known limitations (closed in PR-3)

- **No aggregation**: per-run JSONs are individual; computing P50/P95/P99
  + bootstrap 95% CI is PR-3 work.
- **No `make bench-e2e` Makefile target**: invocation is manual until PR-3.
- **No CI workflow**: by design (§8.1), this is local-only until the
  PR-3 nightly workflow lands.
- **`gate_status` always `pending`**: calibration logic that flips to
  `passed/failed/voided` against customer-anon comparison is PR-3.

## Failure modes & debugging

- **Driver run hangs at "polling exporter gauge"**: exporter never
  completed its first scan. Check `docker compose logs threshold-exporter`
  for fixture parse errors.
- **All `T3_unix_ns: 0`**: alert rule didn't match. Inspect Prometheus
  UI at `http://localhost:9090/alerts` while harness is paused, or
  query `actual_metric_value > on(tenant) group_left(metric) user_threshold{metric="bench_trigger"}`
  manually.
- **Receiver gets 0 posts**: Alertmanager → receiver route broken.
  Check `docker compose logs alertmanager` for webhook delivery errors;
  verify `http://localhost:9093/#/status` shows the receiver as healthy.

## Cross-refs

- Design doc: `docs/internal/design/phase-b-e2e-harness.md`
- Ops cookbook: `docs/internal/benchmark-playbook.md` §v2.8.0 Phase 2 e2e
- PR #78 (B-1 Phase 2 PR-1): added the two timestamp gauges + synthetic-v2 fixture mode
- PR #59 (B-1 Phase 1): synthetic baseline; this harness extends it to e2e
