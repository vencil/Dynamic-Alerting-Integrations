# shellcheck shell=bash
# Driver of scripts/ops/exporter_capture_runtime.sh for the postgresql pack
# (the contract is in that script's header): the official `postgres` image as
# a streaming-replication pair, postgres_exporter pointed at one node of it.
#
# States:
#   primary   the exporter on the primary (a standby is attached)
#   replica   the exporter on the hot standby, which has replayed a transaction
#
# The exporter connects as `monitor`, a user that holds the pg_monitor role
# and nothing else — what postgres_exporter's README asks for. A metric that
# needs a superuser is therefore missing here, as it would be at a tenant's.

PG_SUPER_PW=Ci_only_pw1
PG_MONITOR_PW=Ci_monitor_pw1

capture_states() {
  printf '%s\n' primary replica
}

capture_exporter_port() {
  echo 9187
}

capture_server_up() {
  local image=$1 primary="$CAP-srv-primary" replica="$CAP-srv-replica"
  run_container "$primary" -e POSTGRES_PASSWORD="$PG_SUPER_PW" "$image"
  # Over TCP: the image's first-run setup server listens on the socket only.
  wait_until 90 "the postgres primary" docker exec "$primary" pg_isready -q -h 127.0.0.1 -U postgres
  {
    printf "CREATE USER monitor PASSWORD '%s';\n" "$PG_MONITOR_PW"
    printf 'GRANT pg_monitor TO monitor;\n'
  } | docker exec -i "$primary" psql -q -v ON_ERROR_STOP=1 -U postgres >/dev/null \
    || cannot_run "could not create the postgres monitor user"
  # The image's pg_hba.conf admits `all` databases, which does not include
  # replication connections.
  docker exec "$primary" bash -c \
    'echo "host replication all all scram-sha-256" >> "$PGDATA/pg_hba.conf" && psql -q -U postgres -c "SELECT pg_reload_conf()"' \
    >/dev/null || cannot_run "could not admit replication connections on the postgres primary"
  # The standby: a base backup of the primary, started in recovery (-R).
  run_container "$replica" --user postgres -e PGPASSWORD="$PG_SUPER_PW" "$image" bash -c \
    'pg_basebackup -h "$0" -U postgres -D "$PGDATA" -R -X stream && chmod 700 "$PGDATA" && exec postgres' \
    "$primary"
  wait_until 90 "the postgres replica" docker exec "$replica" pg_isready -q -h 127.0.0.1 -U postgres
  # A transaction for the standby to replay: until it has replayed one,
  # pg_last_xact_replay_timestamp() is NULL and the replication collector fails.
  docker exec "$primary" psql -q -U postgres -c "CREATE TABLE capture_probe AS SELECT 1 AS i" >/dev/null \
    || cannot_run "could not write to the postgres primary"
  wait_until 30 "replay on the postgres replica" \
    docker exec "$replica" psql -q -U postgres -c "SELECT i FROM capture_probe"
}

capture_exporter_args() {
  printf '%s\n' -e "DATA_SOURCE_NAME=postgresql://monitor:$PG_MONITOR_PW@$CAP-srv-$1:5432/postgres?sslmode=disable"
}

capture_scrape_ready() {
  grep -q '^pg_up 1$' "$1"
}
