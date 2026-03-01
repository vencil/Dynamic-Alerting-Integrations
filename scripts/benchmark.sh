#!/bin/bash
# ============================================================
# benchmark.sh — 自動化效能基準測試
# Usage: ./scripts/benchmark.sh [--json] [--under-load [--tenants N]] [--scaling-curve]
#
# Modes:
#   (default)        Idle-state benchmark — collect current cluster metrics
#   --under-load     Generate N synthetic tenants, inject load, measure perf
#                    Includes scrape duration, reload latency, memory delta
#   --tenants N      Number of synthetic tenants (default: 100, max: 2000)
#   --scaling-curve  Measure rule evaluation time at 3/6/9 Rule Packs
# ============================================================
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/_lib.sh"

# --- Options ---
JSON_MODE=false
UNDER_LOAD=false
SCALING_CURVE=false
SYNTH_TENANTS=100
while [[ $# -gt 0 ]]; do
  case "$1" in
    --json) JSON_MODE=true; shift ;;
    --under-load) UNDER_LOAD=true; shift ;;
    --scaling-curve) SCALING_CURVE=true; shift ;;
    --tenants) SYNTH_TENANTS="${2:-100}"; shift 2 ;;
    *) shift ;;
  esac
done

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

# --- Scrape Duration ---
SCRAPE_DUR_S=$(prom_scalar 'scrape_duration_seconds{job="threshold-exporter"}' '0')
SCRAPE_DUR_MS=$(python3 -c "print(f'{float(${SCRAPE_DUR_S})*1000:.1f}')" 2>/dev/null || echo "N/A")

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
# Under-Load Mode: synthetic tenants + metric collection
# ============================================================
UL_STATUS="skipped"
UL_TENANTS_INJECTED=0
UL_MEM_BEFORE_MB="N/A"
UL_MEM_AFTER_MB="N/A"
UL_MEM_DELTA_MB="N/A"
UL_SCRAPE_DUR_MS="N/A"
UL_RELOAD_LATENCY_S="N/A"
UL_EVAL_TIME_MS="N/A"
UL_UT_SERIES_AFTER=0
UL_ACTIVE_SERIES_AFTER=0

if [[ "${UNDER_LOAD}" == true ]]; then
  # Validate tenant count
  if [[ "${SYNTH_TENANTS}" -gt 2000 ]]; then
    warn "Capping synthetic tenants to 2000 (requested: ${SYNTH_TENANTS})"
    SYNTH_TENANTS=2000
  fi

  log "Under-load mode: generating ${SYNTH_TENANTS} synthetic tenants..."

  # --- Snapshot: before ---
  UL_MEM_BEFORE_B=$(prom_scalar 'process_resident_memory_bytes{job="prometheus"}' '0')
  UL_MEM_BEFORE_MB=$(python3 -c "print(f'{float(${UL_MEM_BEFORE_B})/1024/1024:.1f}')" 2>/dev/null || echo "N/A")

  # --- Generate synthetic tenant YAML ---
  SYNTH_DIR=$(mktemp -d)
  trap "rm -rf ${SYNTH_DIR}; cleanup" EXIT

  python3 -c "
import yaml, os, stat
tenants = {}
for i in range(${SYNTH_TENANTS}):
    name = f'synth-{i:04d}'
    tenants[name] = {
        'mysql_connections': str(50 + i % 100),
        'mysql_cpu': str(60 + i % 40),
        'container_cpu': str(70 + i % 30),
        'container_memory': str(75 + i % 20),
    }
data = yaml.dump({'tenants': tenants}, default_flow_style=False)
out = '${SYNTH_DIR}/synth-tenants.yaml'
with open(out, 'w') as f:
    f.write(data)
os.chmod(out, 0o600)
print(f'Generated {len(tenants)} tenants → {out}')
"

  # --- Patch ConfigMap with synthetic tenants ---
  # Read current ConfigMap, merge synthetic tenants, apply
  RELOAD_START=$(date +%s%N)

  python3 -c "
import subprocess, yaml, json, sys, os, stat

# Read current ConfigMap
result = subprocess.run(
    ['kubectl', 'get', 'configmap', 'threshold-config', '-n', 'monitoring', '-o', 'json'],
    capture_output=True, text=True
)
if result.returncode != 0:
    print('Failed to read threshold-config ConfigMap', file=sys.stderr)
    sys.exit(1)

cm = json.loads(result.stdout)
config_str = cm['data'].get('thresholds.yaml', '')
config = yaml.safe_load(config_str) or {}

# Merge synthetic tenants
with open('${SYNTH_DIR}/synth-tenants.yaml') as f:
    synth = yaml.safe_load(f)

if 'tenants' not in config:
    config['tenants'] = {}
config['tenants'].update(synth.get('tenants', {}))

# Write merged config
merged = yaml.dump(config, default_flow_style=False)
patch_json = json.dumps({'data': {'thresholds.yaml': merged}})
patch_file = '${SYNTH_DIR}/patch.json'
with open(patch_file, 'w') as f:
    f.write(patch_json)
os.chmod(patch_file, 0o600)

# Apply patch
result = subprocess.run(
    ['kubectl', 'patch', 'configmap', 'threshold-config', '-n', 'monitoring',
     '--type', 'merge', '-p', patch_json],
    capture_output=True, text=True
)
if result.returncode != 0:
    print(f'Patch failed: {result.stderr}', file=sys.stderr)
    sys.exit(1)
print(f'Patched ConfigMap with {len(synth.get(\"tenants\", {}))} synthetic tenants')
" 2>/dev/null

  # --- Wait for exporter reload (SHA-256 change detection) ---
  info "Waiting for exporter hot-reload (up to 90s)..."
  RELOAD_OK=false
  for i in $(seq 1 30); do
    # Check if exporter has reloaded by watching metric count growth
    CURRENT_UT=$(prom_count 'user_threshold' '0')
    if [[ "${CURRENT_UT}" -gt "$((UT_SERIES + SYNTH_TENANTS))" ]] 2>/dev/null; then
      RELOAD_OK=true
      break
    fi
    sleep 3
  done

  RELOAD_END=$(date +%s%N)
  UL_RELOAD_LATENCY_S=$(python3 -c "print(f'{(${RELOAD_END} - ${RELOAD_START}) / 1e9:.1f}')" 2>/dev/null || echo "N/A")

  if [[ "${RELOAD_OK}" == true ]]; then
    log "Reload detected in ${UL_RELOAD_LATENCY_S}s"
  else
    warn "Reload not fully confirmed after 90s — collecting metrics anyway"
  fi

  UL_TENANTS_INJECTED=${SYNTH_TENANTS}

  # --- Wait for Prometheus to scrape new metrics (2 cycles) ---
  sleep 35

  # --- Snapshot: after ---
  UL_MEM_AFTER_B=$(prom_scalar 'process_resident_memory_bytes{job="prometheus"}' '0')
  UL_MEM_AFTER_MB=$(python3 -c "print(f'{float(${UL_MEM_AFTER_B})/1024/1024:.1f}')" 2>/dev/null || echo "N/A")
  UL_MEM_DELTA_MB=$(python3 -c "print(f'{(float(${UL_MEM_AFTER_B}) - float(${UL_MEM_BEFORE_B}))/1024/1024:.1f}')" 2>/dev/null || echo "N/A")

  UL_SCRAPE_DUR_S=$(prom_scalar 'scrape_duration_seconds{job="threshold-exporter"}' '0')
  UL_SCRAPE_DUR_MS=$(python3 -c "print(f'{float(${UL_SCRAPE_DUR_S})*1000:.1f}')" 2>/dev/null || echo "N/A")

  UL_UT_SERIES_AFTER=$(prom_count 'user_threshold' '0')
  UL_ACTIVE_SERIES_AFTER=$(prom_scalar 'prometheus_tsdb_head_series' '0')

  UL_EVAL_TIME_S=$(prom_scalar 'sum(prometheus_rule_group_last_duration_seconds)')
  UL_EVAL_TIME_MS=$(python3 -c "print(f'{float(${UL_EVAL_TIME_S})*1000:.1f}')" 2>/dev/null || echo "N/A")

  # --- Cleanup: remove synthetic tenants from ConfigMap ---
  info "Cleaning up synthetic tenants..."
  python3 -c "
import subprocess, yaml, json, sys

result = subprocess.run(
    ['kubectl', 'get', 'configmap', 'threshold-config', '-n', 'monitoring', '-o', 'json'],
    capture_output=True, text=True
)
cm = json.loads(result.stdout)
config = yaml.safe_load(cm['data'].get('thresholds.yaml', '')) or {}

# Remove synth- tenants
tenants = config.get('tenants', {})
synth_keys = [k for k in tenants if k.startswith('synth-')]
for k in synth_keys:
    del tenants[k]

merged = yaml.dump(config, default_flow_style=False)
patch_json = json.dumps({'data': {'thresholds.yaml': merged}})
subprocess.run(
    ['kubectl', 'patch', 'configmap', 'threshold-config', '-n', 'monitoring',
     '--type', 'merge', '-p', patch_json],
    capture_output=True, text=True
)
print(f'Removed {len(synth_keys)} synthetic tenants')
" 2>/dev/null

  UL_STATUS="completed"
  log "Under-load benchmark complete."
fi

# ============================================================
# Scaling Curve Mode: measure eval time at 3/6/9 Rule Packs
# ============================================================
SC_STATUS="skipped"
SC_DATA=""

if [[ "${SCALING_CURVE}" == true ]]; then
  log "Scaling curve mode: measuring rule evaluation at 3/6/9 Rule Packs..."

  # Rule Packs grouped by tier:
  #   Tier 1 (3): mariadb, kubernetes, platform  (base set)
  #   Tier 2 (6): + redis, mongodb, elasticsearch
  #   Tier 3 (9): + oracle, db2, clickhouse      (full set = current state)
  TIER2_CMS="prometheus-rules-redis prometheus-rules-mongodb prometheus-rules-elasticsearch"
  TIER3_CMS="prometheus-rules-oracle prometheus-rules-db2 prometheus-rules-clickhouse"
  K8S_DIR="${SCRIPT_DIR}/../k8s/03-monitoring"

  # --- Measure at 9 Rule Packs (current state) ---
  sleep 20  # ensure stable eval metrics
  SC_9_GROUPS=$(prom_scalar 'count(prometheus_rule_group_rules)')
  SC_9_RULES=$(prom_scalar 'sum(prometheus_rule_group_rules)')
  SC_9_EVAL_S=$(prom_scalar 'sum(prometheus_rule_group_last_duration_seconds)')
  SC_9_EVAL_MS=$(python3 -c "print(f'{float(${SC_9_EVAL_S})*1000:.1f}')" 2>/dev/null || echo "N/A")

  # --- Remove tier 3 → measure at 6 Rule Packs ---
  info "Removing tier 3 (oracle, db2, clickhouse)..."
  for cm in ${TIER3_CMS}; do
    kubectl delete configmap "${cm}" -n monitoring --ignore-not-found >/dev/null 2>&1
  done
  # Trigger Prometheus config reload
  kubectl delete pod -l app=prometheus -n monitoring --wait=false >/dev/null 2>&1 || true
  sleep 45  # wait for pod restart + 2 eval cycles

  SC_6_GROUPS=$(prom_scalar 'count(prometheus_rule_group_rules)')
  SC_6_RULES=$(prom_scalar 'sum(prometheus_rule_group_rules)')
  SC_6_EVAL_S=$(prom_scalar 'sum(prometheus_rule_group_last_duration_seconds)')
  SC_6_EVAL_MS=$(python3 -c "print(f'{float(${SC_6_EVAL_S})*1000:.1f}')" 2>/dev/null || echo "N/A")

  # --- Remove tier 2 → measure at 3 Rule Packs ---
  info "Removing tier 2 (redis, mongodb, elasticsearch)..."
  for cm in ${TIER2_CMS}; do
    kubectl delete configmap "${cm}" -n monitoring --ignore-not-found >/dev/null 2>&1
  done
  kubectl delete pod -l app=prometheus -n monitoring --wait=false >/dev/null 2>&1 || true
  sleep 45

  SC_3_GROUPS=$(prom_scalar 'count(prometheus_rule_group_rules)')
  SC_3_RULES=$(prom_scalar 'sum(prometheus_rule_group_rules)')
  SC_3_EVAL_S=$(prom_scalar 'sum(prometheus_rule_group_last_duration_seconds)')
  SC_3_EVAL_MS=$(python3 -c "print(f'{float(${SC_3_EVAL_S})*1000:.1f}')" 2>/dev/null || echo "N/A")

  # --- Restore all Rule Packs ---
  info "Restoring all Rule Pack ConfigMaps..."
  for f in "${K8S_DIR}"/configmap-rules-*.yaml; do
    kubectl apply -f "${f}" -n monitoring >/dev/null 2>&1
  done
  kubectl delete pod -l app=prometheus -n monitoring --wait=false >/dev/null 2>&1 || true
  sleep 30
  log "Rule Packs restored to 9."

  SC_STATUS="completed"
  SC_DATA=$(python3 -c "
import json
data = [
    {'rule_packs': 3, 'rule_groups': int(float('${SC_3_GROUPS}')), 'total_rules': int(float('${SC_3_RULES}')), 'eval_time_ms': float('${SC_3_EVAL_MS}') if '${SC_3_EVAL_MS}' != 'N/A' else None},
    {'rule_packs': 6, 'rule_groups': int(float('${SC_6_GROUPS}')), 'total_rules': int(float('${SC_6_RULES}')), 'eval_time_ms': float('${SC_6_EVAL_MS}') if '${SC_6_EVAL_MS}' != 'N/A' else None},
    {'rule_packs': 9, 'rule_groups': int(float('${SC_9_GROUPS}')), 'total_rules': int(float('${SC_9_RULES}')), 'eval_time_ms': float('${SC_9_EVAL_MS}') if '${SC_9_EVAL_MS}' != 'N/A' else None}
]
print(json.dumps(data))
" 2>/dev/null || echo "[]")

  log "Scaling curve benchmark complete."
fi

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
    'exporter_memory_source': '${EXPORTER_MEM_SOURCE}',
    'scrape_duration_ms': float('${SCRAPE_DUR_MS}') if '${SCRAPE_DUR_MS}' != 'N/A' else None
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
  },
  'under_load': {
    'status': '${UL_STATUS}',
    'synthetic_tenants': int('${UL_TENANTS_INJECTED}'),
    'reload_latency_s': float('${UL_RELOAD_LATENCY_S}') if '${UL_RELOAD_LATENCY_S}' != 'N/A' else None,
    'memory_before_mb': float('${UL_MEM_BEFORE_MB}') if '${UL_MEM_BEFORE_MB}' != 'N/A' else None,
    'memory_after_mb': float('${UL_MEM_AFTER_MB}') if '${UL_MEM_AFTER_MB}' != 'N/A' else None,
    'memory_delta_mb': float('${UL_MEM_DELTA_MB}') if '${UL_MEM_DELTA_MB}' != 'N/A' else None,
    'scrape_duration_ms': float('${UL_SCRAPE_DUR_MS}') if '${UL_SCRAPE_DUR_MS}' != 'N/A' else None,
    'eval_time_ms': float('${UL_EVAL_TIME_MS}') if '${UL_EVAL_TIME_MS}' != 'N/A' else None,
    'user_threshold_series': int('${UL_UT_SERIES_AFTER}'),
    'active_series': int(float('${UL_ACTIVE_SERIES_AFTER}')) if '${UL_ACTIVE_SERIES_AFTER}' != '0' else None
  } if '${UL_STATUS}' != 'skipped' else None,
  'scaling_curve': json.loads('${SC_DATA}') if '${SC_STATUS}' != 'skipped' else None
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
  printf "    %-26s %sms\n" "Scrape Duration" "${SCRAPE_DUR_MS}"
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

  if [[ "${UL_STATUS}" != "skipped" ]]; then
    echo ""
    echo "  Under-Load Results (${UL_TENANTS_INJECTED} synthetic tenants)"
    printf "    %-26s %ss\n" "Reload Latency" "${UL_RELOAD_LATENCY_S}"
    printf "    %-26s %sMB → %sMB (Δ %sMB)\n" "Prometheus Memory" "${UL_MEM_BEFORE_MB}" "${UL_MEM_AFTER_MB}" "${UL_MEM_DELTA_MB}"
    printf "    %-26s %sms\n" "Scrape Duration (after)" "${UL_SCRAPE_DUR_MS}"
    printf "    %-26s %sms\n" "Eval Time (after)" "${UL_EVAL_TIME_MS}"
    printf "    %-26s %s → %s\n" "user_threshold Series" "${UT_SERIES}" "${UL_UT_SERIES_AFTER}"
    printf "    %-26s %s\n" "Active Series (after)" "${UL_ACTIVE_SERIES_AFTER}"
  fi

  if [[ "${SC_STATUS}" != "skipped" ]]; then
    echo ""
    echo "  Rule Evaluation Scaling Curve"
    printf "    %-14s %-14s %-14s %s\n" "Rule Packs" "Rule Groups" "Total Rules" "Eval Time"
    printf "    %-14s %-14s %-14s %s\n" "----------" "-----------" "-----------" "---------"
    python3 -c "
import json
for row in json.loads('${SC_DATA}'):
    et = f\"{row['eval_time_ms']}ms\" if row['eval_time_ms'] is not None else 'N/A'
    print(f\"    {row['rule_packs']:<14} {row['rule_groups']:<14} {row['total_rules']:<14} {et}\")
" 2>/dev/null
  fi

  echo ""
  echo "==========================================================="
  log "Benchmark complete. Use --json for machine-readable output."
fi
