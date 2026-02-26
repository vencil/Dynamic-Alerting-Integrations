#!/bin/bash
# ============================================================
# benchmark.sh — 自動化效能基準測試
# Usage: ./scripts/benchmark.sh [--json]
# ============================================================
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/_lib.sh"

# --- Options ---
JSON_MODE=false
[[ "${1:-}" == "--json" ]] && JSON_MODE=true

# --- Cleanup ---
PF_PID=""
cleanup() {
  [[ -n "${PF_PID}" ]] && kill "${PF_PID}" 2>/dev/null || true
  kill_port 9090
}
trap cleanup EXIT

# --- Preflight ---
preflight_check
ensure_kubeconfig

# --- Port-forward to Prometheus ---
kill_port 9090
sleep 1

PROM_POD=$(kubectl get pods -n monitoring -l app=prometheus -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [[ -z "${PROM_POD}" ]]; then
  err "Prometheus pod not found. Is the cluster running? (make setup)"
  exit 1
fi
kubectl port-forward -n monitoring "pod/${PROM_POD}" 9090:9090 &>/dev/null &
PF_PID=$!

# Wait for Prometheus to be reachable
for i in $(seq 1 10); do
  curl -sf -o /dev/null http://localhost:9090/-/ready 2>/dev/null && break
  sleep 2
done
if ! curl -sf -o /dev/null http://localhost:9090/-/ready 2>/dev/null; then
  err "Prometheus not reachable after 20s"
  exit 1
fi

# ============================================================
# Query helper
# ============================================================
prom_query() {
  local query="$1"
  curl -sf http://localhost:9090/api/v1/query \
    --data-urlencode "query=${query}" 2>/dev/null
}

# Extract single scalar value from instant query
prom_scalar() {
  local query="$1" default="${2:-0}"
  local result
  result=$(prom_query "${query}" | python3 -c "
import sys, json
try:
  r = json.load(sys.stdin)['data']['result']
  print(float(r[0]['value'][1]) if r else '${default}')
except: print('${default}')
" 2>/dev/null) || result="${default}"
  echo "${result}"
}

# Extract count of result vector
prom_count() {
  local query="$1" default="${2:-0}"
  local result
  result=$(prom_query "${query}" | python3 -c "
import sys, json
try:
  r = json.load(sys.stdin)['data']['result']
  print(len(r))
except: print('${default}')
" 2>/dev/null) || result="${default}"
  echo "${result}"
}

# ============================================================
# Collect metrics
# ============================================================
DATE=$(date '+%Y-%m-%d %H:%M:%S')

# --- Rule Evaluation ---
TOTAL_RULES=$(prom_scalar 'sum(prometheus_rule_group_rules)')
RULE_GROUPS=$(prom_scalar 'count(prometheus_rule_group_rules)')
EVAL_TIME_S=$(prom_scalar 'sum(prometheus_rule_group_last_duration_seconds)')
EVAL_TIME_MS=$(python3 -c "print(f'{float(${EVAL_TIME_S})*1000:.1f}')" 2>/dev/null || echo "N/A")
P50_S=$(prom_scalar 'avg(prometheus_rule_group_duration_seconds{quantile="0.5"})' '0')
P50_MS=$(python3 -c "print(f'{float(${P50_S})*1000:.2f}')" 2>/dev/null || echo "N/A")
P99_S=$(prom_scalar 'avg(prometheus_rule_group_duration_seconds{quantile="0.99"})' '0')
P99_MS=$(python3 -c "print(f'{float(${P99_S})*1000:.2f}')" 2>/dev/null || echo "N/A")

# --- Resource Usage ---
PROM_CPU=$(prom_scalar 'rate(process_cpu_seconds_total{job="prometheus"}[5m])' '0')
PROM_CPU_FMT=$(python3 -c "print(f'{float(${PROM_CPU}):.3f}')" 2>/dev/null || echo "N/A")
PROM_MEM_B=$(prom_scalar 'process_resident_memory_bytes{job="prometheus"}' '0')
PROM_MEM_MB=$(python3 -c "print(f'{float(${PROM_MEM_B})/1024/1024:.1f}')" 2>/dev/null || echo "N/A")

# threshold-exporter memory — cascade: container cAdvisor → Go heap alloc
EXPORTER_MEM_SOURCE="RSS"
EXPORTER_MEM_JSON=$(prom_query 'container_memory_working_set_bytes{container="threshold-exporter"}' | python3 -c "
import sys, json
try:
  r = json.load(sys.stdin)['data']['result']
  pods = [{'pod': i['metric'].get('pod','?'), 'mb': round(float(i['value'][1])/1024/1024,1)} for i in r]
  print(json.dumps(pods) if pods else '[]')
except: print('[]')
" 2>/dev/null || echo "[]")
if [[ "${EXPORTER_MEM_JSON}" == "[]" ]]; then
  EXPORTER_MEM_SOURCE="heap"
  EXPORTER_MEM_JSON=$(prom_query 'go_memstats_alloc_bytes{job="threshold-exporter"}' | python3 -c "
import sys, json
try:
  r = json.load(sys.stdin)['data']['result']
  pods = [{'pod': i['metric'].get('instance','?'), 'mb': round(float(i['value'][1])/1024/1024,1)} for i in r]
  print(json.dumps(pods) if pods else '[]')
except: print('[]')
" 2>/dev/null || echo "[]")
fi

EXPORTER_MEM_DISPLAY=$(python3 -c "
import json
pods = json.loads('${EXPORTER_MEM_JSON}')
if pods:
  print(' + '.join(f\"{p['mb']}MB\" for p in pods))
else:
  print('N/A')
" 2>/dev/null || echo "N/A")

# --- Storage & Cardinality ---
# TSDB storage = head chunks + WAL
TSDB_HEAD_B=$(prom_scalar 'prometheus_tsdb_head_chunks_storage_size_bytes' '0')
TSDB_WAL_B=$(prom_scalar 'prometheus_tsdb_wal_storage_size_bytes' '0')
TSDB_SIZE_B=$(python3 -c "print(float(${TSDB_HEAD_B}) + float(${TSDB_WAL_B}))" 2>/dev/null || echo "0")
TSDB_SIZE_MB=$(python3 -c "print(f'{float(${TSDB_SIZE_B})/1024/1024:.1f}')" 2>/dev/null || echo "N/A")

ACTIVE_SERIES=$(prom_scalar 'prometheus_tsdb_head_series' '0')
ACTIVE_SERIES_FMT=$(python3 -c "print(f'{int(float(${ACTIVE_SERIES})):,}')" 2>/dev/null || echo "N/A")

UT_SERIES=$(prom_count 'user_threshold' '0')
TENANTS=$(prom_scalar 'count(count by(tenant)(user_threshold))' '0')
TENANTS_INT=$(python3 -c "print(int(float(${TENANTS})))" 2>/dev/null || echo "0")

# --- Scaling Estimate (100 tenants) ---
UT_PER_TENANT=0
if [[ "${TENANTS_INT}" -gt 0 ]]; then
  UT_PER_TENANT=$(python3 -c "print(int(${UT_SERIES} / ${TENANTS_INT}))" 2>/dev/null || echo "0")
fi
EST_UT_100=$(python3 -c "print(${UT_PER_TENANT} * 100)" 2>/dev/null || echo "0")
EST_SERIES_100=$(python3 -c "
base = int(float(${ACTIVE_SERIES})) - ${UT_SERIES}
est = base + ${EST_UT_100}
print(f'{est:,}')
" 2>/dev/null || echo "N/A")
EST_MEM_DELTA_MB=$(python3 -c "
# ~2KB per series (Prometheus rule of thumb)
delta_series = ${EST_UT_100} - ${UT_SERIES}
print(f'{delta_series * 2 / 1024:.0f}')
" 2>/dev/null || echo "N/A")

# ============================================================
# Output
# ============================================================
if [[ "${JSON_MODE}" == true ]]; then
  python3 -c "
import json
data = {
  'timestamp': '${DATE}',
  'cluster': '${CLUSTER_NAME}',
  'rule_evaluation': {
    'total_rules': int(float('${TOTAL_RULES}')),
    'rule_groups': int(float('${RULE_GROUPS}')),
    'eval_time_ms': float('${EVAL_TIME_MS}') if '${EVAL_TIME_MS}' != 'N/A' else None,
    'p50_ms': float('${P50_MS}') if '${P50_MS}' != 'N/A' else None,
    'p99_ms': float('${P99_MS}') if '${P99_MS}' != 'N/A' else None
  },
  'resource_usage': {
    'prometheus_cpu_cores': float('${PROM_CPU_FMT}') if '${PROM_CPU_FMT}' != 'N/A' else None,
    'prometheus_memory_mb': float('${PROM_MEM_MB}') if '${PROM_MEM_MB}' != 'N/A' else None,
    'exporter_pods': json.loads('${EXPORTER_MEM_JSON}'),
    'exporter_memory_source': '${EXPORTER_MEM_SOURCE}'
  },
  'storage_cardinality': {
    'tsdb_storage_mb': float('${TSDB_SIZE_MB}') if '${TSDB_SIZE_MB}' != 'N/A' else None,
    'active_series': int(float('${ACTIVE_SERIES}')),
    'user_threshold_series': int('${UT_SERIES}'),
    'tenants': int('${TENANTS_INT}'),
    'series_per_tenant': int('${UT_PER_TENANT}')
  },
  'scaling_estimate_100_tenants': {
    'est_user_threshold_series': int('${EST_UT_100}'),
    'est_total_series': int(float('${ACTIVE_SERIES}')) - int('${UT_SERIES}') + int('${EST_UT_100}'),
    'est_additional_memory_mb': float('${EST_MEM_DELTA_MB}') if '${EST_MEM_DELTA_MB}' != 'N/A' else None
  }
}
print(json.dumps(data, indent=2))
"
else
  echo ""
  echo "==========================================================="
  echo "  Dynamic Alerting Platform - Performance Benchmark"
  echo "  Date: ${DATE}  Cluster: ${CLUSTER_NAME}"
  echo "==========================================================="
  echo ""
  echo "  Rule Evaluation"
  printf "    %-26s %s\n" "Total Rules" "${TOTAL_RULES%.*}"
  printf "    %-26s %s\n" "Rule Groups" "${RULE_GROUPS%.*}"
  printf "    %-26s %sms\n" "Eval Time / Cycle" "${EVAL_TIME_MS}"
  printf "    %-26s %sms\n" "p50 per-group" "${P50_MS}"
  printf "    %-26s %sms\n" "p99 per-group" "${P99_MS}"
  echo ""
  echo "  Resource Usage"
  printf "    %-26s %s cores\n" "Prometheus CPU (5m avg)" "${PROM_CPU_FMT}"
  printf "    %-26s %sMB RSS\n" "Prometheus Memory" "${PROM_MEM_MB}"
  printf "    %-26s %s %s\n" "Exporter Memory (x2 HA)" "${EXPORTER_MEM_DISPLAY}" "${EXPORTER_MEM_SOURCE}"
  echo ""
  echo "  Storage & Cardinality"
  printf "    %-26s %sMB\n" "TSDB Storage" "${TSDB_SIZE_MB}"
  printf "    %-26s %s\n" "Active Series" "${ACTIVE_SERIES_FMT}"
  printf "    %-26s %s (%s per tenant)\n" "user_threshold Series" "${UT_SERIES}" "${UT_PER_TENANT}"
  printf "    %-26s %s\n" "Tenants" "${TENANTS_INT}"
  echo ""
  echo "  Scaling Estimate (100 tenants)"
  printf "    %-26s ~%s series (+%s)\n" "Est. user_threshold" "${EST_UT_100}" "$((EST_UT_100 - UT_SERIES))"
  printf "    %-26s ~%s\n" "Est. Total Series" "${EST_SERIES_100}"
  printf "    %-26s ~%sMB\n" "Est. Additional Memory" "${EST_MEM_DELTA_MB}"
  echo ""
  echo "==========================================================="
  log "Benchmark complete. Use --json for machine-readable output."
fi
