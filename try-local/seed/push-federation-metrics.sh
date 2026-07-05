#!/bin/sh
# try-local seed (continuous): mock the federation-revocation reconciler's metrics.
#
# WHY A CONTINUOUS LOOP, NOT A ONE-SHOT (this is correctness, not polish):
# The Grafana "Reconciler freshness" panel reads `time() - last_reconcile_ts`.
# Pushgateway does NOT honor timestamps injected in the exposition text — it
# stamps every sample at scrape time and just holds the last pushed VALUE. So a
# one-shot push of last_reconcile_ts=<now> would freeze that value; `time()`
# keeps advancing, the delta climbs, and within ~30m the freshness panel flips
# to a FALSE red "Stale" (and the tamper headline's "clean is only a live
# all-clear when freshness is green" caveat would make the whole board read as
# untrustworthy). Re-pushing last_reconcile_ts=<now> every loop keeps the delta
# a small live green sawtooth — exactly what a healthy 300s-interval reconciler
# looks like. The real reconciler (helm/federation-reconciler) emits these from
# real revocation-log-vs-live-set reconciliation; here a tiny mock stands in so
# trial users see the SHAPE of a healthy security posture without needing K8s +
# VictoriaLogs.
#
# The 6 metrics + healthy resting values (calm-green): see the table in
# try-local/README.md "Act 2". All are platform-global with ZERO labels, so the
# SOT dashboard (k8s/03-monitoring/federation-revocation-dashboard.json, mounted
# read-only) queries them by name only. Pushed to job=demo-seed so the series is
# obviously synthetic in Prometheus (honor_labels keeps that job label).
set -eu

PUSHGATEWAY_URL="${PUSHGATEWAY_URL:-http://pushgateway:9091}"
# Loop cadence. Kept well under the freshness alert threshold (1800s) with wide
# margin; ~15s also makes the "events checked" timeseries visibly alive.
PUSH_INTERVAL="${PUSH_INTERVAL:-15}"

echo "[fed-seed] starting continuous federation-revocation mock push to ${PUSHGATEWAY_URL}/metrics/job/demo-seed every ${PUSH_INTERVAL}s"

# Tiny awk-free jitter for events_checked (~38-42): a per-loop counter drives a
# sawtooth so the timeseries visibly wiggles without needing a RNG binary
# (busybox sh in curlimages/curl has no $RANDOM).
# ⛔ Do NOT derive the jitter from `date +%s`: the loop advances the clock by
# ~PUSH_INTERVAL each pass, so `now % N` beats against the interval — whenever
# N divides PUSH_INTERVAL (e.g. 5 | 15) the remainder is CONSTANT and the line
# goes dead flat (a self-inflicted 拍頻 / resonance; caught in Gemini review of
# #1012). A monotonic loop counter is interval-independent and always steps by 1.
_tick=0
push_once() {
  now="$(date +%s)"
  _tick=$(( _tick + 1 ))
  # counter-driven sawtooth around 40 → 38..42 (interval-independent).
  jitter=$(( (_tick % 5) - 2 ))   # -2 .. +2
  checked=$(( 40 + jitter ))      # 38 .. 42

  # ts=now EVERY iteration — the crux (see header). tamper/dropped/load-errors
  # flat 0 = honest healthy; reconcile_errors_total a constant 0 counter so
  # rate()=0 (flat green). No labels: platform-global, matches the dashboard.
  cat > /tmp/federation-seed.prom <<EOF
# HELP federation_revocation_tamper_suspected Suspected un-revokes (revoked-but-live tokens absent from live set). 0=clean.
# TYPE federation_revocation_tamper_suspected gauge
federation_revocation_tamper_suspected 0
# HELP federation_revocation_last_reconcile_timestamp_seconds Unix ts of the last successful reconcile pass.
# TYPE federation_revocation_last_reconcile_timestamp_seconds gauge
federation_revocation_last_reconcile_timestamp_seconds ${now}
# HELP federation_revocation_events_checked Live revocation events reconciled last pass.
# TYPE federation_revocation_events_checked gauge
federation_revocation_events_checked ${checked}
# HELP federation_revocation_events_dropped Event-marked rows that failed to parse last pass (schema drift). 0=intact.
# TYPE federation_revocation_events_dropped gauge
federation_revocation_events_dropped 0
# HELP federation_revocation_reconcile_errors_total Fail-closed marker; each failed pass increments. Flat=>rate()=0.
# TYPE federation_revocation_reconcile_errors_total counter
federation_revocation_reconcile_errors_total 0
# HELP federation_gateway_revocation_load_errors Gateway revoked-set read failures (fail-open). 0=OK.
# TYPE federation_gateway_revocation_load_errors gauge
federation_gateway_revocation_load_errors 0
EOF

  # --retry-connrefused rides out pushgateway listen lag on first boot (it has
  # no compose healthcheck — mirrors push-metrics.sh). Non-fatal on transient
  # push failure so the loop keeps the freshness sawtooth alive.
  curl -sf --retry 10 --retry-delay 2 --retry-connrefused \
    --data-binary @/tmp/federation-seed.prom \
    "${PUSHGATEWAY_URL}/metrics/job/demo-seed" \
    && echo "[fed-seed] pushed (ts=${now} checked=${checked})" \
    || echo "[fed-seed] push failed (transient?) — will retry next loop"
}

# Push immediately so the board is populated seconds after `up`, then loop.
while true; do
  push_once
  sleep "${PUSH_INTERVAL}"
done
