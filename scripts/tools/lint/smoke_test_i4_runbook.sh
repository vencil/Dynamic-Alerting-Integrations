#!/usr/bin/env bash
# Smoke test for docs/integration/troubleshooting-checklist.md (I-4)
#
# Runs each jq / amtool / promtool / yq invocation referenced in I-4
# against mock JSON / YAML fixtures matching the expected API surface
# shapes, to catch typos before customers do.
#
# Per post-#377 retrospective Q2 + Gemini's "focus on jq filter syntax
# and amtool params" suggestion.
#
# Run: bash scripts/tools/lint/smoke_test_i4_runbook.sh
# Run in dev container: docker exec vibe-dev-container bash /workspaces/vibe-k8s-lab/scripts/tools/lint/smoke_test_i4_runbook.sh

set -u

PASS=0
FAIL=0
FAILURES=()

# Color codes
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

assert_jq() {
    local name="$1"
    local fixture="$2"
    local filter="$3"
    if echo "$fixture" | jq "$filter" > /dev/null 2>&1; then
        echo -e "  ${GREEN}✅${NC} $name"
        PASS=$((PASS + 1))
    else
        echo -e "  ${RED}❌${NC} $name"
        echo "      filter: $filter"
        echo "      error: $(echo "$fixture" | jq "$filter" 2>&1 | head -2)"
        FAIL=$((FAIL + 1))
        FAILURES+=("$name")
    fi
}

assert_amtool() {
    local name="$1"
    local cmd="$2"
    # amtool with --alertmanager.url=invalid will fail to connect, but
    # we're checking argument parsing not connectivity. If error is
    # connection-related, args parsed OK. If error is flag-related, fail.
    local out
    out=$(eval "$cmd" 2>&1)
    if echo "$out" | grep -qE "unknown flag|unknown argument|invalid value|usage:"; then
        echo -e "  ${RED}❌${NC} $name"
        echo "      cmd: $cmd"
        echo "      error: $(echo "$out" | head -2)"
        FAIL=$((FAIL + 1))
        FAILURES+=("$name")
    else
        echo -e "  ${GREEN}✅${NC} $name (args parsed; connectivity error expected)"
        PASS=$((PASS + 1))
    fi
}

assert_promql() {
    local name="$1"
    local query="$2"
    # Use `promtool check rules` with a synthetic rules YAML
    # (canonical PromQL syntax check entry point)
    local rules_yaml
    rules_yaml=$(cat <<EOF
groups:
  - name: smoke
    rules:
      - alert: SmokeTest
        expr: $query
        for: 5m
        labels:
          severity: critical
EOF
)
    local tmpfile
    tmpfile=$(mktemp /tmp/promql-smoke.XXXXXX.yaml)
    echo "$rules_yaml" > "$tmpfile"
    if promtool check rules "$tmpfile" > /dev/null 2>&1; then
        echo -e "  ${GREEN}✅${NC} $name"
        PASS=$((PASS + 1))
    else
        echo -e "  ${RED}❌${NC} $name"
        echo "      query: $query"
        echo "      error: $(promtool check rules "$tmpfile" 2>&1 | head -3)"
        FAIL=$((FAIL + 1))
        FAILURES+=("$name")
    fi
    rm -f "$tmpfile"
}

assert_yaml() {
    local name="$1"
    local yaml="$2"
    if echo "$yaml" | yq eval '.' > /dev/null 2>&1; then
        echo -e "  ${GREEN}✅${NC} $name"
        PASS=$((PASS + 1))
    else
        echo -e "  ${RED}❌${NC} $name"
        echo "      yaml: ${yaml:0:80}..."
        FAIL=$((FAIL + 1))
        FAILURES+=("$name")
    fi
}

echo "════════════════════════════════════════════════════════════════"
echo "I-4 Runbook Smoke Test — jq / amtool / promtool / yq"
echo "════════════════════════════════════════════════════════════════"
echo

# =============================================================================
# Fixtures
# =============================================================================

# Prom /api/v1/query response (scalar/vector result)
PROM_QUERY_VECTOR='{"status":"success","data":{"resultType":"vector","result":[{"metric":{"__name__":"up","job":"prom","instance":"localhost:9090"},"value":[1700000000,"1"]}]}}'
PROM_QUERY_SCALAR='{"status":"success","data":{"resultType":"scalar","result":[1700000000,"42"]}}'

# Prom /api/v1/labels
PROM_LABELS='{"status":"success","data":["__name__","instance","job","tenant"]}'

# Prom /api/v1/label/__name__/values
PROM_LABEL_VALUES='{"status":"success","data":["up","prometheus_tsdb_head_series","mysql_up","redis_connections"]}'

# Prom /api/v1/series
PROM_SERIES='{"status":"success","data":[{"__name__":"mysql_query_count","db":"prod-1","instance":"x"},{"__name__":"mysql_query_count","db":"prod-2","instance":"y"},{"__name__":"redis_connections","instance":"z"}]}'

# Prom /api/v1/rules
PROM_RULES='{"data":{"groups":[{"name":"g1","rules":[{"name":"MyAlert","labels":{"severity":"critical","migration_status":"shadow"}}]}]}}'

# Prom /api/v1/status/runtimeinfo
PROM_RUNTIMEINFO='{"data":{"lastConfigTime":"2026-05-11T10:30:00Z","reloadConfigSuccess":true}}'

# Prom /api/v1/status/config
PROM_CONFIG='{"data":{"yaml":"global:\n  scrape_interval: 15s\n"}}'

# AM /api/v2/alerts (array of alert objects)
AM_ALERTS='[{"labels":{"alertname":"MyAlert","severity":"critical","migration_status":"shadow"},"status":{"state":"active"}},{"labels":{"alertname":"DiskFull","severity":"warning"},"status":{"state":"suppressed"}}]'

# AM /api/v2/silences (array of silence objects)
AM_SILENCES='[{"id":"abc-123","matchers":[{"name":"alertname","value":"MySQLDown","isRegex":false}],"comment":"test","endsAt":"2099-01-01T00:00:00Z"},{"id":"def-456","matchers":[{"name":"severity","value":"critical","isRegex":false}],"comment":"crit silenced","endsAt":"2099-01-01T00:00:00Z"}]'

# da-tools migration-state.json (from schema)
STATE_JSON='{"schema_version":"1.0","generated_at":"2026-05-11T14:00:00Z","discovery":{"tier_a_static":{"orphan_rules":[{"name":"OldAlert","file":"rules.yaml"}],"tenant_id_violations":[{"file":"rules.yaml","line":42,"snippet":"instance=\"db-prod-1\""}]}},"scope":{"clusters":[{"name":"staging-eu","stage":"0_discovery"},{"name":"prod-us","stage":"0_discovery"}]}}'

# da-tools manifest.json
MANIFEST_JSON='{"schema_version":"1.0","states":[{"cluster":"staging-eu","path":".da/state/staging-eu.json"},{"cluster":"prod-us","path":".da/state/prod-us.json"}]}'

# Grafana /api/dashboards/uid/X
GRAFANA_DASHBOARD='{"dashboard":{"uid":"dash-1","panels":[{"title":"CPU","datasource":{"uid":"prometheus","type":"prometheus"}},{"title":"Mem","datasource":{"uid":"prometheus","type":"prometheus"}}]},"meta":{"provisioned":true,"provisionedExternalId":"k8s.yaml","isFolder":false,"slug":"my-dash"}}'

# Grafana /api/datasources
GRAFANA_DATASOURCES='[{"uid":"prometheus","name":"Prom","type":"prometheus"},{"uid":"victoriametrics","name":"VM","type":"prometheus"}]'

# vmagent metrics (sample subset, multi-line text format)
VMAGENT_METRICS='# HELP vmagent_remotewrite_pending_data_bytes Pending data bytes
vmagent_remotewrite_pending_data_bytes{url="http://vm:8480"} 1024
vmagent_remotewrite_errors_total{url="http://vm:8480"} 3
vmagent_remotewrite_conn{url="http://vm:8480"} 5'

# =============================================================================
# §1.1.1 NetworkPolicy scrape down
# =============================================================================
echo "── §1.1.1 NetworkPolicy / exporter scrape ──"
# No jq in this section. Commands are pure kubectl + curl.
# Verify exec-into-pod / curl patterns parse (syntax-only).
assert_yaml "§1.1.1 NetworkPolicy ingress YAML" '
spec:
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: vm-ns
      ports:
        - port: 8080
          protocol: TCP'

# =============================================================================
# §1.2.1 Rule evaluator no reload
# =============================================================================
echo
echo "── §1.2.1 Rule evaluator reload ──"
assert_jq "§1.2.1 Prom runtimeinfo .data.lastConfigTime" "$PROM_RUNTIMEINFO" '.data.lastConfigTime // empty'

# =============================================================================
# §1.2.2 Shadow label not removed
# =============================================================================
echo
echo "── §1.2.2 Shadow label removal ──"
assert_jq "§1.2.2 Prom rules .data.groups[].rules[] select by name" "$PROM_RULES" '.data.groups[].rules[] | select(.name | contains("MyAlert")) | .labels'
assert_jq "§1.2.2 AM alerts .[].labels" "$AM_ALERTS" '.[].labels'

# =============================================================================
# §1.3.1 AM matcher order
# =============================================================================
echo
echo "── §1.3.1 AM matcher order ──"
assert_amtool "§1.3.1 amtool config routes test syntax" "amtool config routes test --config.file=/nonexistent severity=critical alertname=Test 2>&1 || true"
# Note: the amtool config routes test command actually reads --config.file, so we expect file-not-found error not flag error

# =============================================================================
# §1.3.2 Silencer disablement drift
# =============================================================================
echo
echo "── §1.3.2 Silencer drift ──"
assert_jq "§1.3.2 AM silences jq .[].matchers[] select alertname" "$AM_SILENCES" '.[].matchers[] | select(.name == "alertname") | .value'
assert_amtool "§1.3.2 amtool silence add syntax" "amtool silence add --alertmanager.url=http://nonexistent:9093 --duration=2h --comment='test' alertname=Test 2>&1 || true"
assert_amtool "§1.3.2 amtool silence query -o json syntax" "amtool silence query -o json --alertmanager.url=http://nonexistent:9093 2>&1 || true"

# =============================================================================
# §1.4.1 vmagent OOMKilled
# =============================================================================
echo
echo "── §1.4.1 vmagent OOM ──"
# No jq here. Just kubectl describe + grep patterns.

# =============================================================================
# §1.4.2 Prom OOM / vminsert 503 (Option 2 queue_config)
# =============================================================================
echo
echo "── §1.4.2 Prom remote_write queue_config ──"
assert_yaml "§1.4.2 prometheus.yml remote_write queue_config" '
remote_write:
  - url: "http://vminsert.vm.svc:8480/insert/0/prometheus"
    queue_config:
      max_samples_per_send: 10000
      max_shards: 30
      capacity: 25000'

# =============================================================================
# §1.4.3 VM disk red/orange/green zones
# =============================================================================
echo
echo "── §1.4.3 VM disk zones ──"
# kubectl edit PVC + manual partition delete patterns. No jq directly.

# =============================================================================
# §1.4.4 Cardinality 暴漲
# =============================================================================
echo
echo "── §1.4.4 Cardinality ──"
assert_jq "§1.4.4 VM labels .data | length" "$PROM_LABELS" '.data | length'
assert_jq "§1.4.4 VM series top-20 by metric (group_by/map/sort)" "$PROM_SERIES" '.data | group_by(.__name__) | map({metric: .[0].__name__, n: length}) | sort_by(-.n) | .[0:20]'
assert_jq "§1.4.4 VM series label keys (drill-down)" "$PROM_SERIES" '.data[] | keys[]'

# =============================================================================
# §1.5.1 HA Prom reload race
# =============================================================================
echo
echo "── §1.5.1 HA reload race ──"
assert_jq "§1.5.1 Prom runtimeinfo lastConfigTime (HA pair check)" "$PROM_RUNTIMEINFO" '.data.lastConfigTime // empty'
assert_jq "§1.5.1 Prom config .data.yaml extraction" "$PROM_CONFIG" '.data.yaml'

# =============================================================================
# §1.5.2 Dashboard No-Data (UID drift)
# =============================================================================
echo
echo "── §1.5.2 Dashboard UID drift ──"
assert_jq "§1.5.2 Grafana dashboard panels datasource" "$GRAFANA_DASHBOARD" '.dashboard.panels[] | {title, datasource}'
assert_jq "§1.5.2 Grafana datasources list" "$GRAFANA_DATASOURCES" '.[] | {uid, name, type}'
assert_jq "§1.5.2 Grafana .meta.provisioned check" "$GRAFANA_DASHBOARD" '.meta | {provisioned, provisionedExternalId, isFolder, slug}'
# The dashboard JSON walk for UID rewrite (uses .walk function)
assert_jq "§1.5.2 dashboard .walk UID rewrite" "$GRAFANA_DASHBOARD" '.dashboard | walk(if type == "object" and .uid == "prometheus" then .uid = "victoriametrics" else . end)'

# =============================================================================
# §1.6.1 Dual-write metric drift
# =============================================================================
echo
echo "── §1.6.1 Dual-write drift ──"
# count(up) was the buggy approach (caught in PR #399). New approach uses storage-side metrics:
assert_jq "§1.6.1 Prom tsdb_head_series numeric extract" "$PROM_QUERY_VECTOR" '.data.result[0].value[1] | tonumber'

# =============================================================================
# §2.1.1 PromQL syntax error
# =============================================================================
echo
echo "── §2.1.1 PromQL parse ──"
assert_promql "§2.1.1 simple PromQL" 'mysql_up == 0'
assert_promql "§2.1.1 with for clause" 'rate(http_requests_total[5m]) > 100'

# =============================================================================
# §2.1.2 Hardcoded tenant id
# =============================================================================
echo
echo "── §2.1.2 Tenant id violations ──"
assert_jq "§2.1.2 .discovery.tier_a_static.tenant_id_violations[]" "$STATE_JSON" '.discovery.tier_a_static.tenant_id_violations[]'
assert_jq "§2.1.2 .tenant_id_violations | length" "$STATE_JSON" '.discovery.tier_a_static.tenant_id_violations | length'

# =============================================================================
# §2.1.3 Orphan rule
# =============================================================================
echo
echo "── §2.1.3 Orphan rule ──"
assert_jq "§2.1.3 orphan_rules[]" "$STATE_JSON" '.discovery.tier_a_static.orphan_rules[]'
assert_jq "§2.1.3 orphan_rules | length" "$STATE_JSON" '.discovery.tier_a_static.orphan_rules | length'
assert_amtool "§2.1.3 amtool alert add syntax" "amtool alert add alertname=test_orphan severity=critical --alertmanager.url=http://nonexistent:9093 2>&1 || true"

# =============================================================================
# §2.2 da-guard 4-layer (no jq, uses da-tools subcommands)
# =============================================================================
# Skipped — covered by da-tools own test surface

# =============================================================================
# §2.3 Migration state inconsistency
# =============================================================================
echo
echo "── §2.3 Migration state ──"
assert_jq "§2.3 read schema_version" "$STATE_JSON" '.schema_version'
# Form 2 schema migrate jq filter
assert_jq "§2.3 schema 1.0→1.1 migration jq" "$STATE_JSON" '.schema_version = "1.1" | .gate_log = (.gate_log // [])'
# Form 3 manifest rebuild
assert_jq "§2.3 manifest pattern" '{"schema_version":"1.0","states":[]}' '.states += [{"cluster":"new-cluster","path":".da/state/new-cluster.json"}]'
# Prevention: state-split — extract per-cluster
assert_jq "§2.3 state-split per-cluster extract" "$STATE_JSON" '.scope.clusters[]'

# =============================================================================
# Summary
# =============================================================================
echo
echo "════════════════════════════════════════════════════════════════"
TOTAL=$((PASS + FAIL))
echo "Total: $TOTAL  Pass: $PASS  Fail: $FAIL"
echo "════════════════════════════════════════════════════════════════"

if [ $FAIL -gt 0 ]; then
    echo
    echo "Failed:"
    for f in "${FAILURES[@]}"; do
        echo "  - $f"
    done
    exit 1
fi
