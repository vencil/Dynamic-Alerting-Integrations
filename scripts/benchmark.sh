#!/bin/bash
# ============================================================
# benchmark.sh — 自動化效能基準測試
# Usage: ./scripts/benchmark.sh [--json] [--under-load [--tenants N]] [--scaling-curve]
#        [--routing-bench [--tenants N]] [--alertmanager-bench] [--reload-bench]
#
# Modes:
#   (default)          Idle-state benchmark — collect current cluster metrics
#   --under-load       Generate N synthetic tenants, inject load, measure perf
#                      Includes scrape duration, reload latency, memory delta
#   --tenants N        Number of synthetic tenants (default: 100, max: 2000)
#   --scaling-curve    Measure rule evaluation time at 3/6/9 Rule Packs
#   --routing-bench    Route generation scaling — scaffold N tenants, measure
#                      generate_alertmanager_routes.py wall time + output size
#   --alertmanager-bench  Alertmanager notification latency under load — measure
#                         route matching + inhibit rule evaluation overhead
#   --reload-bench     Alertmanager config reload E2E latency — ConfigMap patch
#                      → configmap-reload detect → /-/reload → new routes active
# ============================================================
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/_lib.sh"

# --- Options ---
JSON_MODE=false
UNDER_LOAD=false
SCALING_CURVE=false
ROUTING_BENCH=false
ALERTMANAGER_BENCH=false
RELOAD_BENCH=false
SYNTH_TENANTS=100
while [[ $# -gt 0 ]]; do
  case "$1" in
    --json) JSON_MODE=true; shift ;;
    --under-load) UNDER_LOAD=true; shift ;;
    --scaling-curve) SCALING_CURVE=true; shift ;;
    --routing-bench) ROUTING_BENCH=true; shift ;;
    --alertmanager-bench) ALERTMANAGER_BENCH=true; shift ;;
    --reload-bench) RELOAD_BENCH=true; shift ;;
    --tenants) SYNTH_TENANTS="${2:-100}"; shift 2 ;;
    *) shift ;;
  esac
done

# --- Cleanup ---
PF_PID=""
AM_PF_PID=""
cleanup() {
  [[ -n "${PF_PID}" ]] && kill "${PF_PID}" 2>/dev/null || true
  [[ -n "${AM_PF_PID}" ]] && kill "${AM_PF_PID}" 2>/dev/null || true
  kill_port 9090
  kill_port 9093
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
# Routing Bench: scaffold N synthetic tenants → generate routes
# ============================================================
RB_STATUS="skipped"
RB_DATA=""

if [[ "${ROUTING_BENCH}" == true ]]; then
  log "Routing bench: measuring route generation for 2/${SYNTH_TENANTS} tenants..."

  CONF_DIR="${SCRIPT_DIR}/../components/threshold-exporter/config/conf.d"
  TOOLS_DIR="${SCRIPT_DIR}/tools"
  RB_TMPDIR=$(mktemp -d)
  trap "rm -rf ${RB_TMPDIR}; cleanup" EXIT

  RB_RESULTS=()
  for N_TENANTS in 2 10 50 ${SYNTH_TENANTS}; do
    # Deduplicate: skip if SYNTH_TENANTS is in {2,10,50}
    if [[ "${N_TENANTS}" -ne 2 && "${N_TENANTS}" -ne 10 && "${N_TENANTS}" -ne 50 && "${N_TENANTS}" -eq "${SYNTH_TENANTS}" ]] || \
       [[ "${N_TENANTS}" -eq 2 || "${N_TENANTS}" -eq 10 || "${N_TENANTS}" -eq 50 ]]; then

      RB_TENANT_DIR="${RB_TMPDIR}/conf-${N_TENANTS}"
      mkdir -p "${RB_TENANT_DIR}"

      # Copy defaults
      cp "${CONF_DIR}/_defaults.yaml" "${RB_TENANT_DIR}/"

      # Generate N synthetic tenant YAMLs with routing + severity_dedup
      python3 -c "
import yaml, os, stat
RECEIVER_TYPES = ['webhook', 'email', 'slack', 'teams', 'rocketchat', 'pagerduty']
for i in range(${N_TENANTS}):
    name = f'bench-{i:04d}'
    rtype = RECEIVER_TYPES[i % len(RECEIVER_TYPES)]
    tenant = {
        'mysql_connections': str(50 + i % 100),
        'mysql_cpu': str(60 + i % 40),
        '_routing': {
            'receiver': {'type': rtype},
            'group_by': ['alertname', 'severity'],
            'group_wait': '30s',
            'group_interval': '5m',
            'repeat_interval': '4h',
        },
        '_severity_dedup': {'mysql': True},
    }
    # Add receiver-specific fields
    if rtype == 'webhook':
        tenant['_routing']['receiver']['url'] = f'https://hooks.bench.local/{name}'
    elif rtype == 'email':
        tenant['_routing']['receiver']['to'] = [f'{name}@bench.local']
        tenant['_routing']['receiver']['smarthost'] = 'smtp.bench.local:587'
    elif rtype in ('slack', 'teams', 'rocketchat'):
        tenant['_routing']['receiver']['url'] = f'https://{rtype}.bench.local/{name}'
    elif rtype == 'pagerduty':
        tenant['_routing']['receiver']['service_key'] = f'pk-bench-{i:04d}'

    # Add routing overrides for every 5th tenant
    if i % 5 == 0:
        tenant['_routing']['overrides'] = [
            {'alertname': 'MariaDBHighConnections', 'receiver': {'type': 'webhook', 'url': f'https://escalation.bench.local/{name}'}},
        ]

    data = {'tenants': {name: tenant}}
    path = '${RB_TENANT_DIR}/' + f'{name}.yaml'
    with open(path, 'w') as f:
        yaml.dump(data, f, default_flow_style=False)
    os.chmod(path, 0o600)
print(f'Generated ${N_TENANTS} tenant configs → ${RB_TENANT_DIR}/')
" 2>/dev/null

      # Measure route generation (5 rounds, report median)
      RB_TIMES=""
      RB_LINES=0
      RB_ROUTES=0
      RB_INHIBITS=0
      for round in $(seq 1 5); do
        RB_START=$(date +%s%N)
        RB_OUTPUT=$(python3 "${TOOLS_DIR}/generate_alertmanager_routes.py" --config-dir "${RB_TENANT_DIR}" --dry-run 2>/dev/null)
        RB_END=$(date +%s%N)
        RB_ELAPSED_MS=$(python3 -c "print(f'{(${RB_END} - ${RB_START}) / 1e6:.1f}')" 2>/dev/null || echo "0")
        RB_TIMES="${RB_TIMES} ${RB_ELAPSED_MS}"

        if [[ "${round}" -eq 1 ]]; then
          RB_LINES=$(echo "${RB_OUTPUT}" | wc -l)
          # Parse counts from summary line: "--- N route(s), M receiver(s), K inhibit rule(s) ---"
          local summary_line
          summary_line=$(echo "${RB_OUTPUT}" | grep -E '--- [0-9]+ route' || echo "")
          if [[ -n "${summary_line}" ]]; then
            RB_ROUTES=$(echo "${summary_line}" | grep -oP '(\d+) route' | grep -oP '\d+')
            RB_INHIBITS=$(echo "${summary_line}" | grep -oP '(\d+) inhibit' | grep -oP '\d+')
          else
            RB_ROUTES=$(echo "${RB_OUTPUT}" | grep -c '  - matchers:' 2>/dev/null || echo "0")
            RB_INHIBITS=$(echo "${RB_OUTPUT}" | grep -c 'target_matchers:' 2>/dev/null || echo "0")
          fi
        fi
      done

      RB_MEDIAN=$(python3 -c "
vals = sorted([float(x) for x in '${RB_TIMES}'.split()])
n = len(vals)
print(f'{vals[n//2]:.1f}')
" 2>/dev/null || echo "N/A")

      RB_RESULTS+=("{\"tenants\": ${N_TENANTS}, \"wall_time_ms\": ${RB_MEDIAN}, \"output_lines\": ${RB_LINES}, \"routes\": ${RB_ROUTES}, \"inhibit_rules\": ${RB_INHIBITS}}")
      info "  N=${N_TENANTS}: ${RB_MEDIAN}ms, ${RB_LINES} lines, ${RB_ROUTES} routes, ${RB_INHIBITS} inhibit rules"
    fi
  done

  RB_DATA=$(python3 -c "
import json
items = [$(IFS=,; echo "${RB_RESULTS[*]}")]
print(json.dumps(items))
" 2>/dev/null || echo "[]")

  RB_STATUS="completed"
  log "Routing bench complete."
fi

# ============================================================
# Alertmanager Bench: notification latency + inhibit overhead
# ============================================================
AM_STATUS="skipped"
AM_NOTIFICATION_LATENCY_MS="N/A"
AM_ALERTS_RECEIVED=0
AM_NOTIFICATIONS_SENT=0
AM_INHIBITED=0
AM_ROUTE_MATCH_P99_MS="N/A"

if [[ "${ALERTMANAGER_BENCH}" == true ]]; then
  log "Alertmanager bench: measuring notification latency and inhibit overhead..."

  # Port-forward to Alertmanager
  kill_port 9093
  sleep 1
  AM_POD=$(kubectl get pods -n monitoring -l app=alertmanager -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
  if [[ -z "${AM_POD}" ]]; then
    warn "Alertmanager pod not found, skipping alertmanager-bench"
  else
    kubectl port-forward -n monitoring "pod/${AM_POD}" 9093:9093 &>/dev/null &
    AM_PF_PID=$!

    # Wait for Alertmanager
    for i in $(seq 1 10); do
      curl -sf -o /dev/null http://localhost:9093/-/ready 2>/dev/null && break
      sleep 2
    done

    if curl -sf -o /dev/null http://localhost:9093/-/ready 2>/dev/null; then
      # Collect Alertmanager internal metrics via Prometheus
      # (Alertmanager metrics are scraped by Prometheus if configured,
      #  otherwise fall back to direct Alertmanager /metrics endpoint)

      # --- Notification latency ---
      AM_LATENCY_S=$(prom_scalar \
        'histogram_quantile(0.99, sum(rate(alertmanager_notification_latency_seconds_bucket[5m])) by (le))' '0')
      AM_NOTIFICATION_LATENCY_MS=$(python3 -c "
v = float('${AM_LATENCY_S}')
print(f'{v*1000:.1f}' if v > 0 else 'N/A')
" 2>/dev/null || echo "N/A")

      # --- Alerts received vs notifications sent ---
      AM_ALERTS_RECEIVED=$(prom_scalar \
        'sum(increase(alertmanager_alerts_received_total[5m]))' '0')
      AM_ALERTS_RECEIVED=$(python3 -c "print(int(float('${AM_ALERTS_RECEIVED}')))" 2>/dev/null || echo "0")

      AM_NOTIFICATIONS_SENT=$(prom_scalar \
        'sum(increase(alertmanager_notifications_total[5m]))' '0')
      AM_NOTIFICATIONS_SENT=$(python3 -c "print(int(float('${AM_NOTIFICATIONS_SENT}')))" 2>/dev/null || echo "0")

      AM_NOTIFICATIONS_FAILED=$(prom_scalar \
        'sum(increase(alertmanager_notifications_failed_total[5m]))' '0')
      AM_NOTIFICATIONS_FAILED=$(python3 -c "print(int(float('${AM_NOTIFICATIONS_FAILED}')))" 2>/dev/null || echo "0")

      # --- Inhibited alerts (from Alertmanager /api/v2/alerts) ---
      AM_INHIBITED=$(curl -sf http://localhost:9093/api/v2/alerts 2>/dev/null | python3 -c "
import sys, json
try:
    alerts = json.load(sys.stdin)
    print(sum(1 for a in alerts if a.get('status', {}).get('state') == 'suppressed'))
except: print(0)
" 2>/dev/null || echo "0")

      # --- Active inhibit rules count (from config) ---
      AM_INHIBIT_RULES=$(curl -sf http://localhost:9093/api/v2/status 2>/dev/null | python3 -c "
import sys, json
try:
    status = json.load(sys.stdin)
    cfg = status.get('config', {}).get('original', '')
    import yaml
    parsed = yaml.safe_load(cfg)
    rules = parsed.get('inhibit_rules', [])
    print(len(rules))
except: print(0)
" 2>/dev/null || echo "0")

      # --- Alertmanager peer mesh info ---
      AM_CLUSTER_PEERS=$(prom_scalar 'alertmanager_cluster_members' '0')
      AM_CLUSTER_PEERS=$(python3 -c "print(int(float('${AM_CLUSTER_PEERS}')))" 2>/dev/null || echo "0")

      AM_STATUS="completed"
      log "Alertmanager bench complete."
    else
      warn "Alertmanager not reachable after 20s, skipping"
    fi
  fi
fi

# ============================================================
# Reload Bench: Alertmanager config reload E2E latency
# ============================================================
RL_STATUS="skipped"
RL_LATENCIES=""
RL_MEDIAN_MS="N/A"
RL_ROUNDS=5

if [[ "${RELOAD_BENCH}" == true ]]; then
  log "Reload bench: measuring Alertmanager config reload E2E latency (${RL_ROUNDS} rounds)..."

  # Port-forward to Alertmanager if not already
  if [[ -z "${AM_PF_PID}" ]]; then
    kill_port 9093
    sleep 1
    AM_POD=$(kubectl get pods -n monitoring -l app=alertmanager -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
    if [[ -n "${AM_POD}" ]]; then
      kubectl port-forward -n monitoring "pod/${AM_POD}" 9093:9093 &>/dev/null &
      AM_PF_PID=$!
      for i in $(seq 1 10); do
        curl -sf -o /dev/null http://localhost:9093/-/ready 2>/dev/null && break
        sleep 2
      done
    fi
  fi

  if curl -sf -o /dev/null http://localhost:9093/-/ready 2>/dev/null; then
    CONF_DIR="${SCRIPT_DIR}/../components/threshold-exporter/config/conf.d"
    TOOLS_DIR="${SCRIPT_DIR}/tools"

    # Get current Alertmanager config reload timestamp
    RL_BASE_TS=$(prom_scalar 'alertmanager_config_last_reload_success_timestamp_seconds' '0')

    for round in $(seq 1 ${RL_ROUNDS}); do
      # Snapshot: last successful reload timestamp (from Alertmanager /metrics)
      RL_BEFORE_TS=$(curl -sf http://localhost:9093/metrics 2>/dev/null | \
        grep 'alertmanager_config_last_reload_success_timestamp_seconds' | \
        grep -oP '\d+\.?\d*$' || echo "0")

      # Touch ConfigMap to trigger reload — add a harmless annotation
      RL_PATCH_START=$(date +%s%N)
      kubectl annotate configmap alertmanager-config -n monitoring \
        "benchmark.reload-bench/round=${round}-$(date +%s)" --overwrite >/dev/null 2>&1

      # Trigger explicit reload via lifecycle API
      curl -sf -X POST http://localhost:9093/-/reload >/dev/null 2>&1

      # Poll for reload success (configmap-reload sidecar or explicit reload)
      RL_RELOAD_OK=false
      for poll in $(seq 1 20); do
        RL_AFTER_TS=$(curl -sf http://localhost:9093/metrics 2>/dev/null | \
          grep 'alertmanager_config_last_reload_success_timestamp_seconds' | \
          grep -oP '\d+\.?\d*$' || echo "0")
        if python3 -c "exit(0 if float('${RL_AFTER_TS}') > float('${RL_BEFORE_TS}') else 1)" 2>/dev/null; then
          RL_RELOAD_OK=true
          break
        fi
        sleep 0.5
      done

      RL_PATCH_END=$(date +%s%N)
      if [[ "${RL_RELOAD_OK}" == true ]]; then
        RL_ELAPSED_MS=$(python3 -c "print(f'{(${RL_PATCH_END} - ${RL_PATCH_START}) / 1e6:.1f}')" 2>/dev/null || echo "N/A")
        RL_LATENCIES="${RL_LATENCIES} ${RL_ELAPSED_MS}"
      else
        warn "Round ${round}: reload not confirmed within 10s"
        RL_LATENCIES="${RL_LATENCIES} N/A"
      fi
    done

    RL_MEDIAN_MS=$(python3 -c "
vals = sorted([float(x) for x in '${RL_LATENCIES}'.split() if x != 'N/A'])
if vals:
    print(f'{vals[len(vals)//2]:.1f}')
else:
    print('N/A')
" 2>/dev/null || echo "N/A")

    # Also measure generate_alertmanager_routes.py --apply E2E (if cluster has routes)
    RL_APPLY_MS="N/A"
    if [[ -d "${CONF_DIR}" ]]; then
      RL_APPLY_START=$(date +%s%N)
      python3 "${TOOLS_DIR}/generate_alertmanager_routes.py" \
        --config-dir "${CONF_DIR}" --apply --yes --namespace monitoring >/dev/null 2>&1 || true
      RL_APPLY_END=$(date +%s%N)
      RL_APPLY_MS=$(python3 -c "print(f'{(${RL_APPLY_END} - ${RL_APPLY_START}) / 1e6:.1f}')" 2>/dev/null || echo "N/A")
    fi

    RL_STATUS="completed"
    log "Reload bench complete. Median: ${RL_MEDIAN_MS}ms, --apply E2E: ${RL_APPLY_MS}ms"
  else
    warn "Alertmanager not reachable, skipping reload-bench"
  fi
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
  'scaling_curve': json.loads('${SC_DATA}') if '${SC_STATUS}' != 'skipped' else None,
  'routing_bench': json.loads('${RB_DATA}') if '${RB_STATUS}' != 'skipped' else None,
  'alertmanager_bench': {
    'status': '${AM_STATUS}',
    'notification_latency_p99_ms': float('${AM_NOTIFICATION_LATENCY_MS}') if '${AM_NOTIFICATION_LATENCY_MS}' != 'N/A' else None,
    'alerts_received_5m': int('${AM_ALERTS_RECEIVED}'),
    'notifications_sent_5m': int('${AM_NOTIFICATIONS_SENT}'),
    'notifications_failed_5m': int('${AM_NOTIFICATIONS_FAILED}'),
    'inhibited_alerts': int('${AM_INHIBITED}'),
    'active_inhibit_rules': int('${AM_INHIBIT_RULES}'),
    'cluster_peers': int('${AM_CLUSTER_PEERS}')
  } if '${AM_STATUS}' != 'skipped' else None,
  'reload_bench': {
    'status': '${RL_STATUS}',
    'reload_latency_median_ms': float('${RL_MEDIAN_MS}') if '${RL_MEDIAN_MS}' != 'N/A' else None,
    'apply_e2e_ms': float('${RL_APPLY_MS}') if '${RL_APPLY_MS}' != 'N/A' else None,
    'rounds': int('${RL_ROUNDS}')
  } if '${RL_STATUS}' != 'skipped' else None
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

  if [[ "${RB_STATUS}" != "skipped" ]]; then
    echo ""
    echo "  Route Generation Scaling"
    printf "    %-12s %-16s %-14s %-12s %s\n" "Tenants" "Wall Time" "Output Lines" "Routes" "Inhibit Rules"
    printf "    %-12s %-16s %-14s %-12s %s\n" "-------" "---------" "------------" "------" "-------------"
    python3 -c "
import json
for row in json.loads('${RB_DATA}'):
    wt = f\"{row['wall_time_ms']}ms\"
    print(f\"    {row['tenants']:<12} {wt:<16} {row['output_lines']:<14} {row['routes']:<12} {row['inhibit_rules']}\")
" 2>/dev/null
  fi

  if [[ "${AM_STATUS}" != "skipped" ]]; then
    echo ""
    echo "  Alertmanager Notification Performance"
    printf "    %-30s %s\n" "Notification Latency (p99)" "${AM_NOTIFICATION_LATENCY_MS}ms"
    printf "    %-30s %s\n" "Alerts Received (5m)" "${AM_ALERTS_RECEIVED}"
    printf "    %-30s %s\n" "Notifications Sent (5m)" "${AM_NOTIFICATIONS_SENT}"
    printf "    %-30s %s\n" "Notifications Failed (5m)" "${AM_NOTIFICATIONS_FAILED}"
    printf "    %-30s %s\n" "Inhibited Alerts (current)" "${AM_INHIBITED}"
    printf "    %-30s %s\n" "Active Inhibit Rules" "${AM_INHIBIT_RULES}"
    printf "    %-30s %s\n" "Cluster Peers" "${AM_CLUSTER_PEERS}"
  fi

  if [[ "${RL_STATUS}" != "skipped" ]]; then
    echo ""
    echo "  Alertmanager Config Reload Latency (${RL_ROUNDS} rounds)"
    printf "    %-30s %sms\n" "Reload Latency (median)" "${RL_MEDIAN_MS}"
    printf "    %-30s %sms\n" "--apply E2E (generate+merge)" "${RL_APPLY_MS}"
  fi

  echo ""
  echo "==========================================================="
  log "Benchmark complete. Use --json for machine-readable output."
fi
