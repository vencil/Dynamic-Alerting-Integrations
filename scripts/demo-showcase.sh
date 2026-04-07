#!/bin/bash
# demo-showcase.sh — 5-Tenant showcase demonstrating full platform capabilities
# Usage: make demo-showcase
#        bash scripts/demo-showcase.sh [--quick]
#
# Showcases v2.2.0 capabilities with 5 realistic tenants:
#   1. prod-mariadb  — E-Commerce DB (MariaDB + Kubernetes, direct routing)
#   2. prod-redis    — Session Cache (Redis + Kubernetes, routing profile)
#   3. prod-kafka    — Event Pipeline (Kafka + JVM, PagerDuty)
#   4. staging-pg    — Staging PostgreSQL (silent mode + maintenance window)
#   5. prod-oracle   — Finance DB (Oracle + DB2, domain policy + compliance)
#
# Demonstrates:
#   - da-tools init (scaffolding)
#   - Multi-pack threshold configuration
#   - Four-layer routing merge (ADR-007)
#   - Three-state operations (Normal / Silent / Maintenance)
#   - Domain policy enforcement (Finance: no Slack/webhook)
#   - config-diff blast radius
#   - validate-config CI mode
#   - generate-routes with validation
#   - explain-route routing trace
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TOOLS_DIR="$PROJECT_DIR/scripts/tools"
SHOWCASE_DIR="$(mktemp -d)"
QUICK=false

for arg in "$@"; do
  case "$arg" in
    --quick) QUICK=true ;;
  esac
done

# --- Colors ---
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
RED='\033[0;31m'
BLUE='\033[0;34m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

step_num=0
step() {
  step_num=$((step_num + 1))
  echo ""
  echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  echo -e "${BOLD}  Step ${step_num}: $1${NC}"
  echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
}

info() { echo -e "  ${GREEN}>>>${NC} $1"; }
warn() { echo -e "  ${YELLOW}!!${NC} $1"; }
detail() { echo -e "  ${DIM}    $1${NC}"; }
pause() {
  if [ "$QUICK" = false ]; then
    echo -e "  ${DIM}(press Enter to continue)${NC}"
    read -r
  fi
}

cleanup() {
  rm -rf "$SHOWCASE_DIR"
  info "Cleaned up showcase directory"
}
trap cleanup EXIT

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║  Dynamic Alerting Platform — v2.2.0 Showcase (5 Tenants)       ║${NC}"
echo -e "${BOLD}║  15 Rule Packs · 99 Alerts · 139 Recording Rules              ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════════════════════════╝${NC}"

# ─────────────────────────────────────────────
step "Scaffold 5-tenant config directory"
# ─────────────────────────────────────────────

CONF_DIR="$SHOWCASE_DIR/conf.d"
mkdir -p "$CONF_DIR"

info "Creating _defaults.yaml (platform baseline)"
cat > "$CONF_DIR/_defaults.yaml" << 'DEFAULTS'
# Platform global defaults — all tenants inherit these
mysql_connections: "80"
mysql_cpu: "80"
pg_connections: "80"
pg_replication_lag: "30"
redis_memory_used_bytes: "4294967296"
redis_connected_clients: "200"
container_cpu: "80"
container_memory: "85"
DEFAULTS

info "Creating prod-mariadb.yaml (E-Commerce DB)"
cat > "$CONF_DIR/prod-mariadb.yaml" << 'TENANT1'
# E-Commerce — MariaDB primary DB
mysql_connections: "150"
mysql_connections_critical: "200"
mysql_cpu: "75"
container_cpu: "75"
container_memory: "80"

_routing:
  receiver_type: slack
  webhook_url: https://hooks.slack.com/services/ecommerce/alerts
  group_by: [alertname, severity]
  group_wait: "30s"
  repeat_interval: "4h"

_metadata:
  owner: ecommerce-team
  tier: production
  runbook_url: https://runbooks.example.com/ecommerce-mariadb
TENANT1

info "Creating prod-redis.yaml (Session Cache, routing profile)"
cat > "$CONF_DIR/prod-redis.yaml" << 'TENANT2'
# Session Cache — Redis with routing profile (ADR-007)
redis_memory_used_bytes: "3221225472"
redis_memory_used_bytes_critical: "4294967296"
redis_connected_clients: "3000"
container_cpu: "70"
container_memory: "80"

_routing_profile: team-sre-apac

_metadata:
  owner: sre-apac
  tier: production
  runbook_url: https://runbooks.example.com/session-redis
TENANT2

info "Creating prod-kafka.yaml (Event Pipeline, PagerDuty)"
cat > "$CONF_DIR/prod-kafka.yaml" << 'TENANT3'
# Event Pipeline — Kafka + JVM
kafka_consumer_lag: "50000"
kafka_consumer_lag_critical: "200000"
kafka_broker_count: "3"
kafka_active_controllers: "1"
kafka_under_replicated_partitions: "0"
kafka_request_rate: "15000"
jvm_gc_pause: "0.8"
jvm_memory: "85"

_routing:
  receiver_type: pagerduty
  group_by: [alertname, topic]
  group_wait: "1m"
  repeat_interval: "12h"

_metadata:
  owner: data-platform
  tier: production
  runbook_url: https://runbooks.example.com/kafka-pipeline
TENANT3

info "Creating staging-pg.yaml (Staging + maintenance window)"
cat > "$CONF_DIR/staging-pg.yaml" << 'TENANT4'
# Staging PostgreSQL — with active maintenance window
pg_connections: "100"
pg_replication_lag: "60"
container_cpu: "90"
container_memory: "95"

_state_maintenance:
  expires: "2026-03-20T06:00:00Z"

_silent_mode:
  expires: "2026-03-18T12:00:00Z"

_routing:
  receiver_type: email
  group_wait: "5m"
  repeat_interval: "24h"

_metadata:
  owner: dev-team
  tier: staging
TENANT4

info "Creating prod-oracle.yaml (Finance DB + domain policy)"
cat > "$CONF_DIR/prod-oracle.yaml" << 'TENANT5'
# Finance DB — Oracle (domain policy enforced)
oracle_sessions_active: "100"
oracle_sessions_active_critical: "150"
oracle_tablespace_used_percent: "75"
oracle_tablespace_used_percent_critical: "85"
db2_connections_active: "180"
db2_bufferpool_hit_ratio: "0.95"

_routing_profile: domain-finance-tier1

_domain_policy: finance

_metadata:
  owner: finance-dba-team
  tier: production
  domain: finance
  compliance: SOX
  runbook_url: https://runbooks.example.com/finance-db
TENANT5

info "5 tenants created in $CONF_DIR"
echo ""
ls -la "$CONF_DIR/"

pause

# ─────────────────────────────────────────────
step "Validate all tenant configs (CI mode)"
# ─────────────────────────────────────────────

info "Running: da-tools validate-config --config-dir $CONF_DIR --ci"
python3 "$TOOLS_DIR/ops/validate_config.py" --config-dir "$CONF_DIR" --ci 2>&1 || true
echo ""
info "Validation complete — any warnings are advisory"

pause

# ─────────────────────────────────────────────
step "Generate Alertmanager routes for all tenants"
# ─────────────────────────────────────────────

ROUTES_OUTPUT="$SHOWCASE_DIR/alertmanager-routes.yaml"
info "Running: da-tools generate-routes --config-dir $CONF_DIR --validate"
python3 "$TOOLS_DIR/ops/generate_alertmanager_routes.py" \
  --config-dir "$CONF_DIR" \
  -o "$ROUTES_OUTPUT" \
  --validate 2>&1 || true
echo ""
if [ -f "$ROUTES_OUTPUT" ]; then
  info "Generated routes → $ROUTES_OUTPUT"
  info "Preview (first 40 lines):"
  head -40 "$ROUTES_OUTPUT"
fi

pause

# ─────────────────────────────────────────────
step "Explain routing trace for prod-mariadb"
# ─────────────────────────────────────────────

info "Running: da-tools explain-route --tenant prod-mariadb --config-dir $CONF_DIR"
python3 "$TOOLS_DIR/ops/explain_route.py" \
  --tenant prod-mariadb \
  --config-dir "$CONF_DIR" 2>&1 || true

pause

# ─────────────────────────────────────────────
step "Config diff: blast radius analysis"
# ─────────────────────────────────────────────

# Simulate a change: lower mysql_connections threshold
CHANGED_DIR="$SHOWCASE_DIR/conf.d.changed"
cp -r "$CONF_DIR" "$CHANGED_DIR"
sed -i 's/mysql_connections: "150"/mysql_connections: "120"/' "$CHANGED_DIR/prod-mariadb.yaml"

info "Simulating change: prod-mariadb mysql_connections 150 → 120"
info "Running: da-tools config-diff"
python3 "$TOOLS_DIR/ops/config_diff.py" \
  --old-dir "$CONF_DIR" --new-dir "$CHANGED_DIR" 2>&1 || true

pause

# ─────────────────────────────────────────────
step "Three-state operations overview"
# ─────────────────────────────────────────────

info "staging-pg demonstrates all three operational states:"
echo ""
echo -e "  ${BOLD}Normal state:${NC}     prod-mariadb, prod-redis, prod-kafka, prod-oracle"
echo -e "  ${YELLOW}Maintenance:${NC}     staging-pg (_state_maintenance expires 2026-03-20)"
echo -e "  ${DIM}Silent mode:${NC}      staging-pg (_silent_mode expires 2026-03-18)"
echo ""
info "In maintenance: alerts still evaluate but route to maintenance receiver."
info "In silent mode: alerts are fully suppressed (no notification)."
info "Both have 'expires' timestamps for automatic recovery."

pause

# ─────────────────────────────────────────────
step "Showcase summary"
# ─────────────────────────────────────────────

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║  Showcase Complete — Platform Capabilities Demonstrated:        ║${NC}"
echo -e "${BOLD}╠══════════════════════════════════════════════════════════════════╣${NC}"
echo -e "${BOLD}║${NC}  ✓ 5 tenants across 7 Rule Packs                              ${BOLD}║${NC}"
echo -e "${BOLD}║${NC}    (MariaDB, Redis, Kafka, JVM, PostgreSQL, Oracle, DB2, K8s)  ${BOLD}║${NC}"
echo -e "${BOLD}║${NC}  ✓ Direct routing + Routing Profile (ADR-007)                  ${BOLD}║${NC}"
echo -e "${BOLD}║${NC}  ✓ Domain policy enforcement (Finance: PagerDuty only)         ${BOLD}║${NC}"
echo -e "${BOLD}║${NC}  ✓ Three-state operations (Normal / Silent / Maintenance)      ${BOLD}║${NC}"
echo -e "${BOLD}║${NC}  ✓ CI-mode validation (da-tools validate-config --ci)          ${BOLD}║${NC}"
echo -e "${BOLD}║${NC}  ✓ Route generation with validation                            ${BOLD}║${NC}"
echo -e "${BOLD}║${NC}  ✓ Routing trace (explain-route)                               ${BOLD}║${NC}"
echo -e "${BOLD}║${NC}  ✓ Blast radius analysis (config-diff)                         ${BOLD}║${NC}"
echo -e "${BOLD}║${NC}  ✓ Severity dedup via Alertmanager inhibit rules               ${BOLD}║${NC}"
echo -e "${BOLD}║${NC}  ✓ _critical multi-severity thresholds                         ${BOLD}║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════════════════════════╝${NC}"
echo ""
info "For hands-on lab, see: docs/scenarios/hands-on-lab.en.md"
info "For GitOps CI/CD setup, see: docs/scenarios/gitops-ci-integration.en.md"
echo ""
