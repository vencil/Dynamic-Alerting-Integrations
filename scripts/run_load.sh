#!/bin/bash
# ============================================================
# run_load.sh — Phase 6: Live Load Injection Toolkit
# 真實負載注入，讓 Grafana/Alertmanager 在 Demo 時展現動態警報
#
# Usage:
#   ./scripts/run_load.sh --tenant db-a --type connections
#   ./scripts/run_load.sh --tenant db-a --type cpu
#   ./scripts/run_load.sh --tenant db-a --type stress-ng
#   ./scripts/run_load.sh --cleanup
#   ./scripts/run_load.sh --tenant db-a --type connections --dry-run
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

# ---- Defaults ----
TENANT=""
LOAD_TYPE=""
DRY_RUN=false
CLEANUP=false
LABEL_APP="load-generator"

# ---- Usage ----
usage() {
  cat <<USAGE
Usage: $(basename "$0") [OPTIONS]

OPTIONS:
  --tenant TENANT    Target tenant namespace (e.g., db-a, db-b)
  --type TYPE        Load type: connections | cpu | stress-ng
  --dry-run          Print K8s manifest without applying
  --cleanup          Remove all load-generator resources
  -h, --help         Show this help

EXAMPLES:
  $(basename "$0") --tenant db-a --type connections    # 連線數風暴
  $(basename "$0") --tenant db-a --type cpu            # CPU 與慢查詢
  $(basename "$0") --tenant db-a --type stress-ng      # 容器 CPU 極限
  $(basename "$0") --cleanup                           # 清除所有壓測
USAGE
  exit 0
}

# ---- Argument Parsing ----
while [[ $# -gt 0 ]]; do
  case "$1" in
    --tenant)  TENANT="$2"; shift 2 ;;
    --type)    LOAD_TYPE="$2"; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    --cleanup) CLEANUP=true; shift ;;
    -h|--help) usage ;;
    *) err "Unknown option: $1"; usage ;;
  esac
done

# ---- Ensure kubeconfig ----
ensure_kubeconfig

# ---- apply_or_print: respects --dry-run ----
apply_or_print() {
  local manifest="$1"
  if $DRY_RUN; then
    info "Dry-run mode — manifest preview:"
    echo "---"
    echo "$manifest"
    echo "---"
  else
    echo "$manifest" | kubectl apply -f -
  fi
}

# ============================================================
# Cleanup
# ============================================================
do_cleanup() {
  info "Cleaning up all load-generator resources..."
  local ns_list
  ns_list=$(kubectl get namespaces -o jsonpath='{.items[*].metadata.name}' | tr ' ' '\n' | grep '^db-')

  local found=false
  for ns in $ns_list; do
    local jobs pods
    jobs=$(kubectl get jobs -n "$ns" -l "app=${LABEL_APP}" --no-headers 2>/dev/null | wc -l)
    pods=$(kubectl get pods -n "$ns" -l "app=${LABEL_APP}" --no-headers 2>/dev/null | wc -l)
    if [[ "$jobs" -gt 0 || "$pods" -gt 0 ]]; then
      found=true
      kubectl delete jobs -n "$ns" -l "app=${LABEL_APP}" --ignore-not-found 2>/dev/null
      kubectl delete pods -n "$ns" -l "app=${LABEL_APP}" --ignore-not-found 2>/dev/null
      log "Cleaned namespace: $ns (jobs=$jobs, pods=$pods)"
    fi
  done

  if ! $found; then
    info "No load-generator resources found."
  else
    log "All load-generator resources removed."
  fi
}

# ============================================================
# Scenario A: Connection Storm
# ============================================================
load_connections() {
  local tenant="$1"
  local job_name="load-conn-${tenant}"
  local target_host="mariadb.${tenant}.svc.cluster.local"
  local num_connections=95
  local sleep_duration=600

  info "Scenario A: Connection Storm → ${tenant}"
  info "Opening ${num_connections} idle connections to ${target_host}:3306..."

  local manifest
  manifest=$(cat <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: ${job_name}
  namespace: ${tenant}
  labels:
    app: ${LABEL_APP}
    type: connections
    tenant: ${tenant}
spec:
  ttlSecondsAfterFinished: 600
  backoffLimit: 0
  template:
    metadata:
      labels:
        app: ${LABEL_APP}
        type: connections
        tenant: ${tenant}
    spec:
      restartPolicy: Never
      containers:
        - name: conn-storm
          image: python:3.12-alpine
          env:
            - name: MARIADB_ROOT_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: mariadb-credentials
                  key: MARIADB_ROOT_PASSWORD
          command:
            - /bin/sh
            - -c
            - |
              echo "=== Connection Storm: ${num_connections} connections ==="
              echo "Target: ${target_host}:3306"
              pip install --quiet PyMySQL 2>/dev/null
              python3 -c "
              import pymysql, time, os, sys
              HOST='${target_host}'; PORT=3306; USER='root'
              PASS=os.environ['MARIADB_ROOT_PASSWORD']
              TARGET=${num_connections}; HOLD=${sleep_duration}
              conns=[]
              for i in range(1, TARGET+1):
                  try:
                      c=pymysql.connect(host=HOST,port=PORT,user=USER,password=PASS,connect_timeout=5,read_timeout=HOLD)
                      conns.append(c)
                  except Exception as e:
                      print(f'  Connection {i} failed: {e}',flush=True)
                  if i%25==0:
                      print(f'  Progress: {i}/{TARGET} connections opened...',flush=True)
              print(f'=== Holding {len(conns)} connections for {HOLD}s ===',flush=True)
              try:
                  time.sleep(HOLD)
              except KeyboardInterrupt:
                  pass
              finally:
                  for c in conns:
                      try: c.close()
                      except: pass
                  print(f'=== Connection Storm complete: {len(conns)} connections released ===',flush=True)
              "
          resources:
            requests:
              cpu: 100m
              memory: 128Mi
            limits:
              cpu: 300m
              memory: 256Mi
EOF
  )

  apply_or_print "$manifest"

  if ! $DRY_RUN; then
    log "Job '${job_name}' created in namespace '${tenant}'"
    info "Expected alerts: MariaDBHighConnections → MariaDBHighConnectionsCritical"
    info "Monitor: kubectl logs -n ${tenant} job/${job_name} -f"
  fi
}

# ============================================================
# Scenario B: CPU & Slow Query Burn (sysbench)
# ============================================================
load_cpu() {
  local tenant="$1"
  local job_name="load-cpu-${tenant}"
  local target_host="mariadb.${tenant}.svc.cluster.local"
  local threads=16
  local duration=300
  local table_size=50000

  info "Scenario B: CPU & Slow Query Burn → ${tenant}"
  info "Running sysbench OLTP (${threads} threads, ${duration}s) against ${target_host}..."

  local manifest
  manifest=$(cat <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: ${job_name}
  namespace: ${tenant}
  labels:
    app: ${LABEL_APP}
    type: cpu
    tenant: ${tenant}
spec:
  ttlSecondsAfterFinished: 600
  backoffLimit: 0
  template:
    metadata:
      labels:
        app: ${LABEL_APP}
        type: cpu
        tenant: ${tenant}
    spec:
      restartPolicy: Never
      containers:
        - name: cpu-burn
          image: severalnines/sysbench
          env:
            - name: MARIADB_ROOT_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: mariadb-credentials
                  key: MARIADB_ROOT_PASSWORD
          command:
            - /bin/bash
            - -c
            - |
              echo "=== CPU & Slow Query Burn ==="
              echo "Target: ${target_host}:3306 | Threads: ${threads} | Duration: ${duration}s"

              # Phase 1: Prepare test data
              echo "--- Phase 1: Preparing test table (${table_size} rows) ---"
              sysbench \
                /usr/share/sysbench/oltp_read_write.lua \
                --mysql-host="${target_host}" \
                --mysql-port=3306 \
                --mysql-user=root \
                --mysql-password="\${MARIADB_ROOT_PASSWORD}" \
                --mysql-db=vibe_db \
                --table-size=${table_size} \
                --tables=1 \
                prepare

              # Phase 2: Run OLTP workload
              echo "--- Phase 2: Running OLTP workload ---"
              sysbench \
                /usr/share/sysbench/oltp_read_write.lua \
                --mysql-host="${target_host}" \
                --mysql-port=3306 \
                --mysql-user=root \
                --mysql-password="\${MARIADB_ROOT_PASSWORD}" \
                --mysql-db=vibe_db \
                --table-size=${table_size} \
                --tables=1 \
                --threads=${threads} \
                --time=${duration} \
                --report-interval=30 \
                run

              # Phase 3: Cleanup test data
              echo "--- Phase 3: Cleanup ---"
              sysbench \
                /usr/share/sysbench/oltp_read_write.lua \
                --mysql-host="${target_host}" \
                --mysql-port=3306 \
                --mysql-user=root \
                --mysql-password="\${MARIADB_ROOT_PASSWORD}" \
                --mysql-db=vibe_db \
                --tables=1 \
                cleanup

              echo "=== CPU & Slow Query Burn complete ==="
          resources:
            requests:
              cpu: 100m
              memory: 128Mi
            limits:
              cpu: 500m
              memory: 256Mi
EOF
  )

  apply_or_print "$manifest"

  if ! $DRY_RUN; then
    log "Job '${job_name}' created in namespace '${tenant}'"
    info "Expected alerts: MariaDBHighSlowQueries, MariaDBSystemBottleneck (composite)"
    info "Monitor: kubectl logs -n ${tenant} job/${job_name} -f"
  fi
}

# ============================================================
# Scenario C: Container Weakest Link (stress-ng)
# ============================================================
load_stress_ng() {
  local tenant="$1"
  local pod_name="load-stress-${tenant}"
  local cpu_limit="100m"
  local stress_duration="300"

  info "Scenario C: Container Weakest Link → ${tenant}"
  info "Deploying CPU stress pod with limit ${cpu_limit} (will be throttled)..."

  local manifest
  manifest=$(cat <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: ${pod_name}
  namespace: ${tenant}
  labels:
    app: ${LABEL_APP}
    type: stress-ng
    tenant: ${tenant}
spec:
  restartPolicy: Never
  containers:
    - name: cpu-stress
      image: alpine:3.19
      command:
        - /bin/sh
        - -c
        - |
          echo "=== CPU Stress: 2 workers, limit ${cpu_limit}, duration ${stress_duration} ==="
          # Spawn 2 CPU-burn workers (exceed limit to cause throttling)
          for i in 1 2; do
            (while true; do :; done) &
          done
          echo "Workers started (PIDs: \$(jobs -p))"
          # Run for specified duration then exit
          sleep ${stress_duration}
          echo "=== CPU Stress complete ==="
          kill \$(jobs -p) 2>/dev/null
      resources:
        requests:
          cpu: 50m
          memory: 32Mi
        limits:
          cpu: ${cpu_limit}
          memory: 64Mi
EOF
  )

  apply_or_print "$manifest"

  if ! $DRY_RUN; then
    log "Pod '${pod_name}' created in namespace '${tenant}'"
    info "CPU limit: ${cpu_limit} → will cause heavy throttling"
    info "Expected alert: PodContainerHighCPU (weakest link detection)"
    info "Monitor: kubectl top pod -n ${tenant} ${pod_name}"
  fi
}

# ============================================================
# Main
# ============================================================

# Handle cleanup first
if $CLEANUP; then
  do_cleanup
  exit 0
fi

# Validate required args
if [[ -z "$TENANT" ]]; then
  err "Missing --tenant. Use -h for help."
  exit 1
fi

if [[ -z "$LOAD_TYPE" ]]; then
  err "Missing --type. Use -h for help."
  exit 1
fi

# Validate tenant namespace exists (skip in dry-run mode)
if ! $DRY_RUN; then
  if ! kubectl get namespace "$TENANT" &>/dev/null; then
    err "Namespace '${TENANT}' does not exist. Available tenants:"
    kubectl get namespaces -o name | grep 'db-' | sed 's|namespace/|  |'
    exit 1
  fi
fi

# Delete existing job/pod for this scenario before creating new one
if ! $DRY_RUN; then
  case "$LOAD_TYPE" in
    connections)
      kubectl delete job "load-conn-${TENANT}" -n "$TENANT" --ignore-not-found &>/dev/null
      ;;
    cpu)
      kubectl delete job "load-cpu-${TENANT}" -n "$TENANT" --ignore-not-found &>/dev/null
      ;;
    stress-ng)
      kubectl delete pod "load-stress-${TENANT}" -n "$TENANT" --ignore-not-found &>/dev/null
      ;;
  esac
  # Brief pause for resource cleanup
  sleep 2
fi

# Dispatch
case "$LOAD_TYPE" in
  connections) load_connections "$TENANT" ;;
  cpu)         load_cpu "$TENANT" ;;
  stress-ng)   load_stress_ng "$TENANT" ;;
  *)
    err "Unknown load type: '${LOAD_TYPE}'"
    err "Valid types: connections, cpu, stress-ng"
    exit 1
    ;;
esac
