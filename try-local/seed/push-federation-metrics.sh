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
#
# EARNED-RED CHAOS via a MODE FLAG (Falco event-generator pattern: the USER
# triggers it, so red is exciting not alarming). Each pass reads /seed/.demo-mode
# (written by try-local/chaos.sh; absent/empty ⇒ healthy). WHY a watched flag and
# NOT a one-shot push: a single chaos push would be OVERWRITTEN by this loop's
# next ~15s healthy push and flicker back to green — the red wouldn't stick. The
# flag makes the loop itself emit the bad values every pass, so the red HOLDS
# until `chaos.sh heal`. The mount is `./seed:/seed:ro` (container reads only) —
# but a :ro bind mount still reflects HOST writes into the container, so chaos.sh
# writing the flag on the host is seen here on the very next `cat`. No new control
# service / HTTP surface needed. See the case dispatch in push_once() below.
set -eu

PUSHGATEWAY_URL="${PUSHGATEWAY_URL:-http://pushgateway:9091}"
# Loop cadence. Kept well under the freshness alert threshold (1800s) with wide
# margin; ~15s also makes the "events checked" timeseries visibly alive.
PUSH_INTERVAL="${PUSH_INTERVAL:-15}"
# The mode-flag file the loop watches (chaos.sh writes it; see header). Overridable
# only for tests; the compose mount fixes it at /seed/.demo-mode in the container.
DEMO_MODE_FILE="${DEMO_MODE_FILE:-/seed/.demo-mode}"

echo "[fed-seed] starting continuous federation-revocation mock push to ${PUSHGATEWAY_URL}/metrics/job/demo-seed every ${PUSH_INTERVAL}s (mode flag: ${DEMO_MODE_FILE})"

# Tiny awk-free jitter for events_checked (~38-42): a per-loop counter drives a
# sawtooth so the timeseries visibly wiggles without needing a RNG binary
# (busybox sh in curlimages/curl has no $RANDOM).
# ⛔ Do NOT derive the jitter from `date +%s`: the loop advances the clock by
# ~PUSH_INTERVAL each pass, so `now % N` beats against the interval — whenever
# N divides PUSH_INTERVAL (e.g. 5 | 15) the remainder is CONSTANT and the line
# goes dead flat (a self-inflicted 拍頻 / resonance; caught in Gemini review of
# #1012). A monotonic loop counter is interval-independent and always steps by 1.
_tick=0
# Accumulating fail-closed counter for `stale` mode. reconcile_errors_total is a
# Prometheus counter, so its value must only ever grow while the fault persists;
# rate(...[5m])>0 then lights the fail-closed panel. It stays PINNED (does not
# reset) when we leave `stale` — pushing a constant flattens rate() back to 0,
# and rate() is counter-reset-aware so even the eventual reset settles cleanly.
_stale_errors=0
push_once() {
  now="$(date +%s)"
  _tick=$(( _tick + 1 ))
  # counter-driven sawtooth around 40 → 38..42 (interval-independent).
  jitter=$(( (_tick % 5) - 2 ))   # -2 .. +2
  checked=$(( 40 + jitter ))      # 38 .. 42

  # Read the scenario flag fresh each pass (host-written, see header). Strip ALL
  # whitespace (trailing newline chaos.sh writes, plus any stray spaces from a
  # hand-edited file) so the case match is exact. Absent/empty/unreadable ⇒
  # healthy (the `|| true` keeps set -e from tripping on a missing file).
  mode="$(cat "${DEMO_MODE_FILE}" 2>/dev/null | tr -d '[:space:]' || true)"
  [ -n "${mode}" ] || mode="healthy"

  # Healthy resting values (calm-green). Each `case` arm below overrides ONLY the
  # metric(s) its scenario should flip, keeping everything else green — so e.g.
  # `tamper` still shows fresh reconciles (ts=now) and healthy coverage. ts=now
  # for every non-stale mode is the crux (see header): freshness must stay green
  # unless the scenario IS staleness.
  ts="${now}"                 # freshness input: time()-ts stays small (green)
  tamper=0                    # 0=clean;      >0 → 🔴 critical (Tamper)
  dropped=0                   # 0=intact;     >0 → 🟡 Coverage integrity + erosion
  gateway=0                   # 0=OK;         >0 → 🟡 Gateway fail-open
  # reconcile_errors_total: constant ⇒ rate()=0 (flat green fail-closed panel).

  case "${mode}" in
    healthy)
      : ;;                    # all defaults above = calm-green (current behaviour)
    tamper)
      # THE HEADLINE. A revoked-but-live token reappeared: tamper flips to 🔴
      # critical while everything else stays healthy (fresh, intact) — exactly
      # the "clean board except one screaming red" a real un-revoke looks like.
      tamper=1 ;;
    stale)
      # Reconciler stopped succeeding: last good pass drifts to ~2000s ago
      # (>1800 → 🔴 Stale) AND fail-closed errors accumulate so rate()>0. Both
      # freshness stat + timeseries and the fail-closed panel light up.
      ts=$(( now - 2000 ))
      _stale_errors=$(( _stale_errors + 1 )) ;;
    drift)
      # Schema drift: 3 revocation rows/pass fail to parse → events_dropped>0
      # (🟡 Coverage integrity + 🟡 erosion ratio + a nonzero dropped timeseries).
      dropped=3 ;;
    failopen)
      # Gateway couldn't read the revoked set and fell OPEN (allowed the token):
      # 🟡 Gateway fail-open. The rest stays green — this is a degraded-but-not-
      # breached posture, distinct from tamper.
      gateway=1 ;;
    *)
      # Unknown flag ⇒ fail safe to healthy rather than emit garbage. chaos.sh
      # validates input, so this only fires on a hand-edited flag file.
      echo "[fed-seed] unknown mode '${mode}' — treating as healthy"
      mode="healthy" ;;
  esac

  # ts EVERY iteration (see header). Values above are healthy unless the active
  # scenario overrode them. No labels: platform-global, matches the dashboard.
  cat > /tmp/federation-seed.prom <<EOF
# HELP federation_revocation_tamper_suspected Suspected un-revokes (revoked-but-live tokens absent from live set). 0=clean.
# TYPE federation_revocation_tamper_suspected gauge
federation_revocation_tamper_suspected ${tamper}
# HELP federation_revocation_last_reconcile_timestamp_seconds Unix ts of the last successful reconcile pass.
# TYPE federation_revocation_last_reconcile_timestamp_seconds gauge
federation_revocation_last_reconcile_timestamp_seconds ${ts}
# HELP federation_revocation_events_checked Live revocation events reconciled last pass.
# TYPE federation_revocation_events_checked gauge
federation_revocation_events_checked ${checked}
# HELP federation_revocation_events_dropped Event-marked rows that failed to parse last pass (schema drift). 0=intact.
# TYPE federation_revocation_events_dropped gauge
federation_revocation_events_dropped ${dropped}
# HELP federation_revocation_reconcile_errors_total Fail-closed marker; each failed pass increments. Flat=>rate()=0.
# TYPE federation_revocation_reconcile_errors_total counter
federation_revocation_reconcile_errors_total ${_stale_errors}
# HELP federation_gateway_revocation_load_errors Gateway revoked-set read failures (fail-open). 0=OK.
# TYPE federation_gateway_revocation_load_errors gauge
federation_gateway_revocation_load_errors ${gateway}
EOF

  # --retry-connrefused rides out pushgateway listen lag on first boot (it has
  # no compose healthcheck — mirrors push-metrics.sh). Non-fatal on transient
  # push failure so the loop keeps the freshness sawtooth alive.
  curl -sf --retry 10 --retry-delay 2 --retry-connrefused \
    --data-binary @/tmp/federation-seed.prom \
    "${PUSHGATEWAY_URL}/metrics/job/demo-seed" \
    && echo "[fed-seed] pushed (mode=${mode} ts=${ts} checked=${checked} tamper=${tamper} dropped=${dropped} gateway=${gateway} errs=${_stale_errors})" \
    || echo "[fed-seed] push failed (transient?) — will retry next loop"
}

# Push immediately so the board is populated seconds after `up`, then loop.
while true; do
  push_once
  sleep "${PUSH_INTERVAL}"
done
