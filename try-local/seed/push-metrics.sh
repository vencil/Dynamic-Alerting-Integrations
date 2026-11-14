#!/bin/sh
# try-local seed (2/2): push synthetic DB metrics to pushgateway.
#
# This is the "signal" half of seed-with-signal: a one-shot push of a high
# connection count for db-demo so rule-pack-mariadb's
# MariaDBHighConnectionsCritical fires (the headline red light at
# :9090/alerts and :9093). Pushgateway retains the series after this exits,
# so the alert stays firing. Part of the FULL stack only (Mode 0 skips it).
set -eu

PUSHGATEWAY_URL="${PUSHGATEWAY_URL:-http://pushgateway:9091}"
SEED_TENANT="${SEED_TENANT:-db-demo}"
THREADS_CONNECTED="${THREADS_CONNECTED:-190}"
MAX_CONNECTIONS="${MAX_CONNECTIONS:-200}"

# Push to job=tenant-exporters (the job the tenant rule packs select);
# prometheus honor_labels keeps the tenant + job labels intact.
#   mysql_up=1                                → suppresses MariaDBDown
#   mysql_global_status_uptime=86400          → suppresses MariaDBRecentRestart
#   mysql_global_status_threads_connected=190
#   mysql_global_variables_max_connections=200 → 95 % > critical(90) → HEADLINE
#   (the key is a share of max_connections since #3778, so both halves are pushed)
cat > /tmp/seed-metrics.prom <<EOF
# TYPE mysql_up gauge
mysql_up{tenant="${SEED_TENANT}"} 1
# TYPE mysql_global_status_uptime gauge
mysql_global_status_uptime{tenant="${SEED_TENANT}"} 86400
# TYPE mysql_global_status_threads_connected gauge
mysql_global_status_threads_connected{tenant="${SEED_TENANT}"} ${THREADS_CONNECTED}
# TYPE mysql_global_variables_max_connections gauge
mysql_global_variables_max_connections{tenant="${SEED_TENANT}"} ${MAX_CONNECTIONS}
EOF

echo "[push-metrics] pushing synthetic metrics for ${SEED_TENANT} (threads_connected=${THREADS_CONNECTED} / max_connections=${MAX_CONNECTIONS} > critical 90%)"
# --retry-connrefused rides out pushgateway's listen lag (it has no compose
# healthcheck — see docker-compose.yaml).
curl -sf --retry 10 --retry-delay 2 --retry-connrefused \
  --data-binary @/tmp/seed-metrics.prom \
  "${PUSHGATEWAY_URL}/metrics/job/tenant-exporters"
echo "[push-metrics] done — MariaDBHighConnectionsCritical should fire within ~30s (rule for:30s)"
