#!/usr/bin/env bash
# A rule pack's raw selectors, checked against its real exporter (#3833):
#   exporter_capture_runtime.sh <pack>     capture and check one pack
#   exporter_capture_runtime.sh --list     the packs this script can run
#
# What to start is the registry's, not this script's: the pack's `exporters:`
# entry in rule-packs/threshold-registry.yaml names the exporter image (tag +
# digest), the flags it is started with, the server image and the server
# states. Per entry this script starts the server, then per state the exporter
# with exactly those flags, scrapes /metrics once the exporter reports the
# server up, and hands the scrapes to
#   check_exporter_capture.py --pack <pack> --capture <index>:<state>=<scrape> …
# which fails when a raw selector the pack's rules read selects no series of
# any capture.
#
# What is NOT the registry's — how this server is started, how it is put in a
# state, what the exporter is told to connect to, what "up" looks like in a
# scrape — is the pack's driver, scripts/ops/exporter_capture/<pack>.sh,
# sourced below. A driver defines:
#   capture_states                 print the states it can produce, one a line
#   capture_server_up <image>      start the server(s) with run_container and
#                                  return when every state can be scraped
#   capture_exporter_args <state>  print the `docker run` arguments (one a
#                                  line) that point the exporter at that state
#   capture_exporter_port          print the port the exporter listens on
#   capture_scrape_ready <file>    succeed when the scrape in <file> shows the
#                                  exporter reached the server
# and may call cannot_run, run_container and wait_until; $CAP prefixes every
# container name and $NET is the network.
#
# Needs: docker, curl, python3 with pyyaml + jsonschema; da-read and
# da-crdecode for reading the rule pack (CI: scripts/ops/with_da_*.sh).
# Exit: 0 ok / 1 finding / 2 could not run (docker missing, an image that
# cannot be pulled, a server that never becomes ready, an exporter that never
# reports it up — each by name, never as a finding).
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
PY=${PYTHON:-python3}
CHECK="$ROOT/scripts/tools/lint/check_exporter_capture.py"
PACK=${1:-}

cannot_run() {
  echo "exporter_capture_runtime: $*" >&2
  exit 2
}

if [[ "$PACK" == "--list" ]]; then
  exec "$PY" "$CHECK" --list
fi
DRIVER="$ROOT/scripts/ops/exporter_capture/$PACK.sh"
if [[ -z "$PACK" || "$#" -ne 1 || ! -f "$DRIVER" ]]; then
  echo "usage: exporter_capture_runtime.sh <pack> | --list   (packs: $("$PY" "$CHECK" --list | tr '\n' ' '))" >&2
  exit 2
fi

# The plan: `<word> <value>` lines, an `exporter <index>` line opening each entry.
PLAN=$("$PY" "$CHECK" --plan "$PACK") || cannot_run "cannot read the exporters: entry of $PACK"
IMAGES=() SERVERS=() FLAGS=() STATES=()
while read -r word value; do
  case "$word" in
    exporter) IMAGES+=("") SERVERS+=("") FLAGS+=("") STATES+=("") ;;
    image)    IMAGES[-1]=$value ;;
    server)   SERVERS[-1]=$value ;;
    flag)     FLAGS[-1]+="$value"$'\n' ;;
    state)    STATES[-1]+="$value"$'\n' ;;
    name)     ;;
    *)        cannot_run "unknown line in the plan of $PACK: $word $value" ;;
  esac
done <<<"$PLAN"
[[ ${#IMAGES[@]} -gt 0 ]] || cannot_run "the plan of $PACK names no exporter"

# shellcheck source=/dev/null
source "$DRIVER"
mapfile -t CAN < <(capture_states)
for i in "${!IMAGES[@]}"; do
  while read -r state; do
    [[ -z "$state" ]] && continue
    [[ " ${CAN[*]} " == *" $state "* ]] \
      || cannot_run "the registry asks for the state '$state', which ${DRIVER#"$ROOT/"} cannot produce (${CAN[*]})"
  done <<<"${STATES[$i]}"
done

command -v docker >/dev/null 2>&1 || cannot_run "docker is not installed"
docker info >/dev/null 2>&1 || cannot_run "the docker daemon is not reachable"

CAP="cap-$$"
NET="$CAP-net"
CONTAINERS=()
WORK=$(mktemp -d)
cleanup() {
  [[ ${#CONTAINERS[@]} -gt 0 ]] && docker rm -f -v "${CONTAINERS[@]}" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

# The last lines of every container's log, for the reader of a failure. Never
# the reason this script ends: with the daemon gone `docker logs` fails too.
show_logs() {
  local name
  for name in "${CONTAINERS[@]}"; do
    echo "── $name ──" >&2
    docker logs "$name" 2>&1 | tail -n 20 >&2 || true
  done
}
# run_container <name> <docker run arguments…>: detached, on $NET, removed at exit.
run_container() {
  local name=$1
  shift
  CONTAINERS+=("$name")
  docker run -d --name "$name" --network "$NET" "$@" >/dev/null \
    || cannot_run "could not start the container $name"
}
# wait_until <tries> <what> <command…>: the command every 2s until it succeeds.
wait_until() {
  local tries=$1 what=$2 _
  shift 2
  for _ in $(seq 1 "$tries"); do
    "$@" >/dev/null 2>&1 && return 0
    sleep 2
  done
  show_logs
  cannot_run "$what did not become ready"
}
# Pulled apart from `run`, with retries: a registry hiccup is "could not run".
pull() {
  local n
  for n in 1 2 3; do
    docker pull -q "$1" >/dev/null && return 0
    [[ $n -lt 3 ]] && sleep $((n * 15))
  done
  cannot_run "could not pull $1 (image pull failed 3 times)"
}

docker network create "$NET" >/dev/null || cannot_run "could not create the docker network"
PORT_IN=$(capture_exporter_port)
CAPTURES=()
for i in "${!IMAGES[@]}"; do
  pull "${SERVERS[$i]}"
  pull "${IMAGES[$i]}"
  mapfile -t flags < <(printf '%s' "${FLAGS[$i]}")
  echo "starting ${SERVERS[$i]%@*} for ${IMAGES[$i]%@*} ${flags[*]}"
  capture_server_up "${SERVERS[$i]}"
  while read -r state; do
    [[ -z "$state" ]] && continue
    exp="$CAP-exp-$i-$state"
    scrape="$WORK/$i.$state.prom"
    mapfile -t args < <(capture_exporter_args "$state")
    run_container "$exp" -p "127.0.0.1::$PORT_IN" "${args[@]}" "${IMAGES[$i]}" "${flags[@]}"
    # An exporter that exits at startup (a flag it refuses) has no port to report.
    port=$(docker port "$exp" "$PORT_IN/tcp" 2>/dev/null | head -n1 | sed 's/.*://') || true
    if [[ -z "$port" ]]; then
      show_logs
      cannot_run "the exporter for the state '$state' is not listening (it exited at startup?)"
    fi
    # --max-time: an exporter that accepts the connection and never answers is
    # "never reported the server up", not a job that hangs until its timeout.
    ready=
    for _ in $(seq 1 60); do
      if curl -fsS --max-time 5 "http://127.0.0.1:$port/metrics" > "$scrape" 2>/dev/null \
          && capture_scrape_ready "$scrape"; then
        ready=1
        break
      fi
      sleep 2
    done
    if [[ -z "$ready" ]]; then
      show_logs
      cannot_run "the exporter for the state '$state' never reported the server up"
    fi
    echo "captured $i:$state ($(grep -vc '^#' "$scrape" || true) samples)"
    CAPTURES+=(--capture "$i:$state=$scrape")
  done <<<"${STATES[$i]}"
  docker rm -f -v "${CONTAINERS[@]}" >/dev/null 2>&1 || true
  CONTAINERS=()
done

set +e
"$PY" "$CHECK" --pack "$PACK" "${CAPTURES[@]}"
rc=$?
set -e
exit $rc
