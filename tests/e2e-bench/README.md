# E2E alert fire-through harness

Local-only harness measuring end-to-end alert latency through the full
stack: tenant config write → exporter scan/reload → Prometheus scrape +
evaluation → Alertmanager dispatch → webhook receiver. Implements the
5-anchor measurement protocol from
[`docs/internal/design/phase-b-e2e-harness.md`](../../docs/internal/design/phase-b-e2e-harness.md).

The harness produces per-run JSON with the 5-anchor breakdown; an
aggregator (`aggregate.py`) computes P50/P95/P99 + bootstrap 95% CI
and applies the ±30% calibration gate against a customer-anon baseline.

## Why local-only (not CI on every PR)

Per design doc §8.1: cold start (compose up + healthcheck + 30 runs ×
2 phases + teardown) is ~5–8 minutes wall-clock. Running on every PR
would dominate CI cost without producing actionable signal. The nightly
`bench-e2e-record.yaml` workflow (manually-dispatched on `main`) is the
canonical CI invocation; for ad-hoc maintainer runs use `make bench-e2e`.

## Quick start (≥ 30-tenant fixture, ~5 min wall-clock)

**Easiest** — from repo root:

```bash
make bench-e2e                   # 30 runs × synthetic-v2 (default)
make bench-e2e COUNT=10          # quick sanity
make bench-e2e E2E_FIXTURE_KIND=customer-anon   # if customer fixture staged
make bench-e2e-aggregate         # aggregate the per-run JSONs
```

[`scripts/ops/bench_e2e_run.sh`](../../scripts/ops/bench_e2e_run.sh)
wraps the entire fixture-stage → compose-up → run → cleanup sequence
with EXIT-trap teardown (failure mid-script doesn't leave a dangling
stack). The Makefile target shells out to it.

**Manual** (when you need to inspect intermediate state):

```bash
cd tests/e2e-bench

# 1. Generate a fixture once.
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

## Fixture kinds

Three kinds, controlled by `E2E_FIXTURE_KIND` env (default `synthetic-v2`):

| Kind | Source | Use |
|---|---|---|
| `synthetic-v1` | `--layout hierarchical` (uniform) | Earlier-baseline reuse for delta comparisons |
| `synthetic-v2` | `--layout synthetic-v2` (Zipf+power-law) | Main baseline (default) |
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

`gate_status` is `"pending"` in **per-run** output by design — the
calibration verdict is computed by `aggregate.py` against the customer
baseline, not per-run. The aggregate JSON's `gate_status` is the real
verdict (`passed` / `failed` / `voided`); see
[`aggregate.py::determine_gate_status`](aggregate.py) for the matrix.

## Components

| Service | Image / Build | Role |
|---|---|---|
| `threshold-exporter` | `build: ../../components/threshold-exporter/app` | Watches `fixture/active/conf.d/`, emits `user_threshold` + `da_config_last_{scan,reload}_complete_unixtime_seconds` gauges (T1/T2 anchors) |
| `prometheus` | `prom/prometheus:v3.11.2` | Scrapes exporter + pushgateway; evaluates `actual_metric_value > user_threshold` rule |
| `pushgateway` | `prom/pushgateway:v1.10.0` | Receives driver-injected `actual_metric_value{tenant=bench-run-N}` |
| `alertmanager` | `prom/alertmanager:v0.32.0` | Routes firing/resolved alerts to receiver (`send_resolved: true`) |
| `receiver` | `build: ./receiver` | Custom Go ring-buffer webhook target; `/posts?since=...&tenant_id=...` query API |
| `driver` | `build: ./driver` | Python orchestrator; runs **inside** compose so all timestamps share the host kernel clock (no skew) |

## Run isolation contract

Each driver run uses tenant `bench-run-{i}` (i in 0..COUNT). All metrics
and alerts carry that label. Alertmanager `group_by: [tenant]` keeps
each run in its own group → no cross-run dedup interference. Pushgateway
metrics are explicitly DELETEd in the driver's `finally:` block to
prevent stale state from bleeding into the next run.

## Calibration gate

`aggregate.py` reads the most recent `synthetic-v2` aggregate JSON and
compares the current run's P95 against it within ±30%. The gate writes
one of three statuses to the aggregate output:

| Status | Meaning |
|---|---|
| `passed` | P95 within ±30% of customer baseline |
| `failed` | P95 outside ±30% — investigate before merging |
| `voided` | No customer baseline staged (no comparison done) |

Design rationale: §6.5 of the e2e-harness design doc. The ±30% envelope
absorbs CI runner-noise without admitting genuine regressions.

## CI

Two GitHub Actions workflows touch this harness:

- **[`bench-e2e-record.yaml`](../../.github/workflows/bench-e2e-record.yaml)**
  — manually-dispatched (`workflow_dispatch`); runs `make bench-e2e` and
  uploads aggregate JSON as workflow artifact. Inputs: `fixture_kind` /
  `count`. Use to refresh baselines or to capture a new measurement
  on `main`.
- **[`release-attach-bench-baseline.yaml`](../../.github/workflows/release-attach-bench-baseline.yaml)**
  — attaches the latest `bench-baseline.txt` artifact to GitHub Releases
  on tag push.

There is no per-PR bench job by design (§8.1 cost rationale above).

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
- **`gate_status: voided`**: customer-anon fixture not staged. Either
  stage one (see `fixture/customer-anon/README.md`) or accept voided
  status for synthetic-only runs.

## Cross-refs

- Design doc: [`docs/internal/design/phase-b-e2e-harness.md`](../../docs/internal/design/phase-b-e2e-harness.md)
- Ops cookbook: [`docs/internal/benchmark-playbook.md`](../../docs/internal/benchmark-playbook.md) §e2e harness
- Aggregate logic: [`aggregate.py`](aggregate.py) (calibration gate matrix)
- Driver: [`driver/driver.py`](driver/driver.py)
