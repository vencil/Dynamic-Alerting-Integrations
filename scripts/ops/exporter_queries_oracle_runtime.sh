#!/usr/bin/env bash
# Runtime half of check_exporter_queries.py for the Oracle pack (#3833).
#
# Starts Oracle Database Free (gvenzl/oracle-free, pinned by digest below) and
# albertodonato/query-exporter at the version requirements/ci-constraints.txt
# pins, runs rule-packs/exporters/oracle.yaml against it, scrapes /metrics once
# every query has run, and hands the scrape to
#   check_exporter_queries.py --pack oracle --compare-served <scrape>
# which fails when a query did not succeed or a derived name is not served.
#
# Needs: docker, curl, and a python3 with pyyaml + query-exporter installed
# (CI's Lint Rule Packs job has pyyaml and installs query-exporter
# with `-c requirements/ci-constraints.txt`); da-read for reading the rule pack.
# Exit: 0 ok / 1 finding / 2 could not run.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
ORACLE_IMAGE="gvenzl/oracle-free:23-slim@sha256:6d61d267a3b978c24c5ac1790e62e927416a0aec446bd86e4b3a1527562757bd"
QE_VERSION=$(sed -n 's/^query-exporter==\([^[:space:]#]*\).*/\1/p' "$ROOT/requirements/ci-constraints.txt")
if [[ -z "$QE_VERSION" ]]; then
  echo "exporter_queries_oracle_runtime: no query-exporter pin in requirements/ci-constraints.txt" >&2
  exit 2
fi
QE_IMAGE="adonato/query-exporter:${QE_VERSION}"
NET="qe-oracle-$$"
WORK=$(mktemp -d)
PY=${PYTHON:-python3}

cleanup() {
  docker rm -f "qe-ora-$$" "qe-exp-$$" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

docker network create "$NET" >/dev/null
docker run -d --name "qe-ora-$$" --network "$NET" \
  -e ORACLE_PASSWORD=Ci_only_pw1 -e APP_USER=monitor -e APP_USER_PASSWORD=Ci_monitor_pw1 \
  "$ORACLE_IMAGE" >/dev/null

echo "waiting for Oracle (up to 10 min)…"
for _ in $(seq 1 120); do
  if docker logs "qe-ora-$$" 2>&1 | grep -q 'DATABASE IS READY TO USE'; then ready=1; break; fi
  sleep 5
done
if [[ -z "${ready:-}" ]]; then
  docker logs "qe-ora-$$" 2>&1 | tail -n 20 >&2
  echo "exporter_queries_oracle_runtime: Oracle did not become ready" >&2
  exit 2
fi
# The monitor user reads v$ / dba_* views (rule-packs/exporters/oracle.yaml header).
docker exec "qe-ora-$$" bash -c \
  "printf 'GRANT SELECT_CATALOG_ROLE TO monitor;\nexit\n' | sqlplus -s system/Ci_only_pw1@localhost/FREEPDB1" >/dev/null

cat > "$WORK/databases.yaml" <<EOF
databases:
  oracle:
    dsn: oracle+oracledb://monitor:Ci_monitor_pw1@qe-ora-$$:1521/?service_name=FREEPDB1
EOF
docker run -d --name "qe-exp-$$" --network "$NET" -p 127.0.0.1::9560 \
  -v "$WORK/databases.yaml:/config/databases.yaml:ro" \
  -v "$ROOT/rule-packs/exporters/oracle.yaml:/config/queries.yaml:ro" \
  "$QE_IMAGE" --config /config/databases.yaml --config /config/queries.yaml >/dev/null
PORT=$(docker port "qe-exp-$$" 9560/tcp | head -n1 | sed 's/.*://')

# Every query has run once when each has a queries_total sample; the longest
# interval in the file is 60s, so allow a few.
want=$(grep -cE '^  oracle_[a-z_]+:$' "$ROOT/rule-packs/exporters/oracle.yaml")
echo "waiting for $want queries to run…"
for _ in $(seq 1 60); do
  curl -fsS "http://127.0.0.1:$PORT/metrics" > "$WORK/scrape.txt" 2>/dev/null || true
  got=$(grep -E '^queries_total\{' "$WORK/scrape.txt" | grep -oE 'query="[^"]+"' | sort -u | wc -l || true)
  [[ "$got" -ge "$want" ]] && break
  sleep 5
done
sleep 20   # one more round of the 15s queries, so a counter has moved once
curl -fsS "http://127.0.0.1:$PORT/metrics" > "$WORK/scrape.txt"

set +e
"$PY" "$ROOT/scripts/tools/lint/check_exporter_queries.py" --pack oracle --compare-served "$WORK/scrape.txt"
rc=$?
set -e
if [[ $rc -ne 0 ]]; then
  docker logs "qe-exp-$$" 2>&1 | tail -n 30 >&2
fi
exit $rc
