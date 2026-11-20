#!/usr/bin/env bash
# Runtime half of check_exporter_queries.py (#3833):
#   exporter_queries_runtime.sh <pack>     run the pack's queries file for real
#   exporter_queries_runtime.sh --list     the packs this script can run
#
# Starts the pack's database in a container (image pinned by digest below) and
# albertodonato/query-exporter at the version requirements/ci-constraints.txt
# pins, runs rule-packs/exporters/<pack>.yaml against it as a monitor user,
# scrapes /metrics once every query has run, and hands the scrape to
#   check_exporter_queries.py --pack <pack> --compare-served <scrape>
# which fails when a query did not succeed or a derived name is not served.
#
# The monitor user is created here and given the grants the queries file's own
# header documents — every `#   GRANT …` line there is executed, and this
# script grants nothing else. A query that needs a privilege the header does
# not list therefore fails here (queries_total{status="error"}), not at a
# customer's. (What the database gives every user is still there: a new DB2
# database lets PUBLIC connect and read the catalog.)
#
# Packs:
#   oracle  Oracle Database Free (gvenzl/oracle-free), the FREEPDB1 PDB.
#   db2     Db2 Community Edition 11.5.9 (icr.io/db2_community/db2). Needs a
#           privileged container and ~4 minutes to set up; x86_64 only (so is
#           the DB2 driver in the query-exporter image).
# tests/ops/test_exporter_queries_runtime_wiring.py holds this list equal to
# the registry packs that name a `queries:` file, and each to a CI step.
#
# Needs: docker, curl, and a python3 with pyyaml + query-exporter installed
# (CI installs query-exporter with `-c requirements/ci-constraints.txt`);
# da-read for reading the rule pack.
# Exit: 0 ok / 1 finding / 2 could not run.
set -euo pipefail

PACKS=(oracle db2)
PACK=${1:-}
if [[ "$PACK" == "--list" ]]; then
  printf '%s\n' "${PACKS[@]}"
  exit 0
fi

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
QUERIES="$ROOT/rule-packs/exporters/$PACK.yaml"
MONITOR_PW=Ci_monitor_pw1
DB="qe-db-$$"
EXP="qe-exp-$$"
NET="qe-net-$$"

case "$PACK" in
  oracle)
    DB_IMAGE="gvenzl/oracle-free:23-slim@sha256:6d61d267a3b978c24c5ac1790e62e927416a0aec446bd86e4b3a1527562757bd"
    # No APP_USER: the image would create it with DB_DEVELOPER_ROLE, more than
    # the header documents. The monitor user is created below.
    DB_RUN=(-e ORACLE_PASSWORD=Ci_only_pw1)
    READY='DATABASE IS READY TO USE'
    DSN="oracle+oracledb://monitor:$MONITOR_PW@$DB:1521/?service_name=FREEPDB1"
    ;;
  db2)
    # The linux/amd64 image of the 11.5.9.0 tag.
    DB_IMAGE="icr.io/db2_community/db2:11.5.9.0@sha256:652e99aef2898fe85fa76b61a9a0ac1b2466cc68a8e3e9ea9f089a3421f25272"
    DB_RUN=(--privileged -e LICENSE=accept -e DB2INSTANCE=db2inst1 -e DB2INST1_PASSWORD=Ci_only_pw1
            -e DBNAME=CIDB -e SAMPLEDB=false -e PERSISTENT_HOME=false -e HADR_ENABLED=false)
    READY='Setup has completed'
    DSN="db2+ibm_db://monitor:$MONITOR_PW@$DB:50000/CIDB"
    ;;
  *)
    echo "usage: exporter_queries_runtime.sh $(IFS='|'; echo "${PACKS[*]}") | --list" >&2
    exit 2
    ;;
esac

cannot_run() {
  echo "exporter_queries_runtime: $*" >&2
  exit 2
}
# The last lines of a container's log, for the reader of a failure. Never the
# reason this script ends: with the docker daemon gone `docker logs` fails too,
# and under errexit + pipefail that would replace the exit code being reported.
show_logs() {
  docker logs "$1" 2>&1 | tail -n "$2" >&2 || true
}

QE_VERSION=$(sed -n 's/^query-exporter==\([^[:space:]#]*\).*/\1/p' "$ROOT/requirements/ci-constraints.txt")
[[ -n "$QE_VERSION" ]] || cannot_run "no query-exporter pin in requirements/ci-constraints.txt"
QE_IMAGE="adonato/query-exporter:${QE_VERSION}"
PY=${PYTHON:-python3}

# The grants the queries file documents for the monitor user. None found is a
# header that stopped spelling them as `#   GRANT …` lines, not "none needed".
mapfile -t GRANTS < <(sed -n 's/^#   \(GRANT [^;]*\)$/\1/p' "$QUERIES")
[[ ${#GRANTS[@]} -gt 0 ]] || cannot_run "no '#   GRANT …' line in the header of $QUERIES"
# A GRANT comment in another spelling (a trailing `;`, other indentation) would
# be skipped and surface as a failed query; say so here instead.
spelled=$(grep -cE '^#[[:space:]]*GRANT[[:space:]]' "$QUERIES" || true)
[[ "$spelled" -eq ${#GRANTS[@]} ]] \
  || cannot_run "$QUERIES has $spelled GRANT comment lines, ${#GRANTS[@]} spelled '#   GRANT … ' with no ';'"
# Query names, read the way query-exporter reads them (a YAML mapping).
want=$("$PY" -c 'import sys, yaml; print(len(yaml.safe_load(open(sys.argv[1], encoding="utf-8"))["queries"]))' "$QUERIES") \
  || cannot_run "cannot read the queries of $QUERIES"

WORK=$(mktemp -d)
cleanup() {
  docker rm -f "$DB" "$EXP" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

# Pulled apart from `run`, with retries: a registry hiccup is "could not run"
# (exit 2, named), never a finding.
pull() {
  local n
  for n in 1 2 3; do
    docker pull -q "$1" >/dev/null && return 0
    [[ $n -lt 3 ]] && sleep $((n * 15))
  done
  cannot_run "could not pull $1 (image pull failed 3 times)"
}
pull "$DB_IMAGE"
pull "$QE_IMAGE"

# Starting containers is infrastructure: a failure is "could not run", by name.
docker network create "$NET" >/dev/null || cannot_run "could not create the docker network"
docker run -d --name "$DB" --network "$NET" "${DB_RUN[@]}" "$DB_IMAGE" >/dev/null \
  || cannot_run "could not start the $PACK container"

echo "waiting for $PACK (up to 15 min)…"
ready=
for _ in $(seq 1 180); do
  # Into a variable, not `| grep -q`: under pipefail a grep that exits on its
  # match makes `docker logs` die of SIGPIPE and the pipeline read as "no match".
  logs=$(docker logs "$DB" 2>&1 || true)
  if [[ "$logs" == *"$READY"* ]]; then ready=1; break; fi
  if [[ "$(docker inspect -f '{{.State.Running}}' "$DB" 2>/dev/null)" != true ]]; then break; fi
  sleep 5
done
if [[ -z "$ready" ]]; then
  show_logs "$DB" 20
  cannot_run "$PACK did not become ready"
fi

echo "granting the monitor user what the header of ${QUERIES#"$ROOT/"} documents:"
printf '  %s\n' "${GRANTS[@]}"
if [[ "$PACK" == oracle ]]; then
  {
    printf 'WHENEVER SQLERROR EXIT FAILURE\n'
    printf 'CREATE USER monitor IDENTIFIED BY "%s";\n' "$MONITOR_PW"
    printf '%s;\n' "${GRANTS[@]}"
    printf 'exit\n'
  } | docker exec -i "$DB" sqlplus -s system/Ci_only_pw1@localhost/FREEPDB1 >"$WORK/setup.log" 2>&1 \
    || { cat "$WORK/setup.log" >&2; cannot_run "could not set up the oracle monitor user"; }
elif [[ "$PACK" == db2 ]]; then
  # DB2 authenticates against the operating system: the monitor user is an OS
  # user of the container, with no instance authority (not SYSMON).
  # A new database's automatic-storage tablespaces have no maximum size, which
  # db2_tablespace leaves out (#4093), so it would serve no row. One tablespace
  # per served shape: auto-resize with a maximum, and a fixed-size DMS one.
  # Last, each monitoring table function is called once as the instance owner:
  # the first call on a database that has just been created is slow, and the
  # queries' 8s timeouts would turn that into a finding about the SQL.
  {
    printf 'set -e\n'
    printf 'db2 connect to CIDB\n'
    printf 'db2 "%s"\n' "${GRANTS[@]}"
    printf 'db2 "%s"\n' \
      "CREATE TABLESPACE CI_TS_MAX MANAGED BY AUTOMATIC STORAGE AUTORESIZE YES MAXSIZE 64 M" \
      "CREATE TABLESPACE CI_TS_FIX MANAGED BY DATABASE USING (FILE '/database/data/ci_ts_fix.dat' 2000) AUTORESIZE NO" \
      "SELECT COUNT(*) FROM TABLE(MON_GET_DATABASE(-1))" \
      "SELECT COUNT(*) FROM TABLE(MON_GET_TRANSACTION_LOG(-1))" \
      "SELECT COUNT(*) FROM TABLE(MON_GET_TABLESPACE(NULL, -1))"
  } >"$WORK/setup.sh"
  docker cp "$WORK/setup.sh" "$DB:/tmp/ci_setup.sh" >/dev/null \
    || cannot_run "could not copy the setup script into the db2 container"
  {
    docker exec "$DB" bash -c "useradd monitor && echo 'monitor:$MONITOR_PW' | chpasswd && chmod 644 /tmp/ci_setup.sh" \
      && docker exec "$DB" su - db2inst1 -c "bash /tmp/ci_setup.sh"
  } >"$WORK/setup.log" 2>&1 \
    || { cat "$WORK/setup.log" >&2; cannot_run "could not set up the db2 monitor user and tablespaces"; }
fi

# The liveness query has a database entry of its own (rule-packs/exporters/<pack>.yaml).
cat > "$WORK/databases.yaml" <<YAML
databases:
  $PACK:
    dsn: $DSN
  ${PACK}_liveness:
    dsn: $DSN
YAML
docker run -d --name "$EXP" --network "$NET" -p 127.0.0.1::9560 \
  -v "$WORK/databases.yaml:/config/databases.yaml:ro" \
  -v "$QUERIES:/config/queries.yaml:ro" \
  "$QE_IMAGE" --config /config/databases.yaml --config /config/queries.yaml >/dev/null \
  || cannot_run "could not start query-exporter"
exporter_gone() {
  show_logs "$EXP" 30
  cannot_run "$1"
}
# An exporter that exits at startup (a config it refuses) has no port to report.
PORT=$(docker port "$EXP" 9560/tcp 2>/dev/null | head -n1 | sed 's/.*://') || true
[[ -n "$PORT" ]] || exporter_gone "query-exporter is not listening (it exited at startup?)"

# Every query has run once when each has a queries_total sample; the longest
# interval in a file is 60s, so allow a few. A query that never reports is
# left to --compare-served below, which names it.
echo "waiting for $want queries to run…"
for _ in $(seq 1 60); do
  curl -fsS --max-time 20 "http://127.0.0.1:$PORT/metrics" > "$WORK/scrape.txt" 2>/dev/null || true
  got=$(grep -E '^queries_total\{' "$WORK/scrape.txt" | grep -oE 'query="[^"]+"' | sort -u | wc -l || true)
  [[ "$got" -ge "$want" ]] && break
  sleep 5
done
sleep 20   # one more round of the 15s queries, so a counter has moved once
# --max-time: an exporter that accepts the connection and never answers is
# "did not answer", not a job that hangs until its timeout.
curl -fsS --max-time 20 "http://127.0.0.1:$PORT/metrics" > "$WORK/scrape.txt" \
  || exporter_gone "query-exporter did not answer /metrics"

set +e
"$PY" "$ROOT/scripts/tools/lint/check_exporter_queries.py" --pack "$PACK" --compare-served "$WORK/scrape.txt"
rc=$?
set -e
if [[ $rc -ne 0 ]]; then
  show_logs "$EXP" 30
fi
exit $rc
