#!/bin/bash
# demo.sh — End-to-end demonstration of Dynamic Alerting workflow
# Usage: make demo  (or: bash scripts/demo.sh)
#
# Demonstrates:
#   1. scaffold_tenant.py — 產生新 tenant config
#   2. patch_config.py    — 動態修改閾值
#   3. migrate_rule.py    — 轉換傳統 alert rules
#   4. diagnose.py        — Tenant 健康檢查
#   5. check_alert.py     — Alert 狀態查詢
set -euo pipefail

# Colors (ASCII only for Windows compatibility)
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

step() {
  echo ""
  echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  echo -e "${BOLD}  Step $1: $2${NC}"
  echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
}

info() {
  echo -e "  ${GREEN}>>>${NC} $1"
}

warn() {
  echo -e "  ${YELLOW}!!${NC} $1"
}

DEMO_DIR="/tmp/demo-output"
rm -rf "$DEMO_DIR"

echo ""
echo -e "${BOLD}============================================${NC}"
echo -e "${BOLD}  Dynamic Alerting — End-to-End Demo${NC}"
echo -e "${BOLD}============================================${NC}"

# -----------------------------------------------
step "1" "Exporter Catalog (scaffold_tenant.py --catalog)"
# -----------------------------------------------
info "顯示支援的 exporter 清單..."
python3 scripts/tools/scaffold_tenant.py --catalog
echo ""

# -----------------------------------------------
step "2" "Scaffold New Tenant (scaffold_tenant.py)"
# -----------------------------------------------
info "產生 db-demo tenant config (MariaDB + Redis)..."
python3 scripts/tools/scaffold_tenant.py \
  --tenant db-demo \
  --db mariadb,redis \
  -o "$DEMO_DIR/scaffold"
echo ""
info "生成的 tenant config:"
cat "$DEMO_DIR/scaffold/db-demo.yaml"
echo ""
info "生成的 platform defaults:"
cat "$DEMO_DIR/scaffold/_defaults.yaml"
echo ""
info "部署指引:"
grep -A5 "helm upgrade" "$DEMO_DIR/scaffold/scaffold-report.txt" || true

# -----------------------------------------------
step "3" "Migrate Legacy Rules (migrate_rule.py)"
# -----------------------------------------------
if [ -f "tests/legacy-dummy.yml" ]; then
  info "轉換傳統 alert rules..."
  python3 scripts/tools/migrate_rule.py \
    tests/legacy-dummy.yml \
    -o "$DEMO_DIR/migration" \
    2>/dev/null || true
  echo ""
  info "生成的 tenant-config.yaml:"
  cat "$DEMO_DIR/migration/tenant-config.yaml" 2>/dev/null || echo "  (skipped)"
  echo ""
  info "生成的 migration-report.txt:"
  cat "$DEMO_DIR/migration/migration-report.txt" 2>/dev/null || echo "  (skipped)"
else
  warn "tests/legacy-dummy.yml 不存在，跳過遷移示範"
fi

# -----------------------------------------------
step "4" "Dry-Run Migration (migrate_rule.py --dry-run)"
# -----------------------------------------------
if [ -f "tests/legacy-dummy.yml" ]; then
  info "Dry-run 模式預覽..."
  python3 scripts/tools/migrate_rule.py \
    tests/legacy-dummy.yml \
    --dry-run \
    2>/dev/null || true
else
  warn "tests/legacy-dummy.yml 不存在，跳過 dry-run 示範"
fi

# -----------------------------------------------
step "5" "Live Cluster Tools (需要 Kind cluster)"
# -----------------------------------------------
# Check if cluster is available
if kubectl cluster-info &>/dev/null; then
  info "偵測到 Kind cluster，執行即時檢查..."
  echo ""

  info "5a. diagnose.py — Tenant 健康檢查 (db-a):"
  python3 scripts/tools/diagnose.py db-a 2>/dev/null || warn "diagnose 失敗 (可能需要 port-forward)"
  echo ""

  info "5b. check_alert.py — Alert 狀態查詢:"
  python3 scripts/tools/check_alert.py MariaDBDown db-a 2>/dev/null || warn "check_alert 失敗 (可能需要 port-forward)"
  echo ""

  info "5c. patch_config.py — 動態修改閾值 (db-a mysql_connections=50):"
  python3 scripts/tools/patch_config.py db-a mysql_connections 50 2>/dev/null || warn "patch_config 失敗"
  echo ""
  info "  還原閾值..."
  python3 scripts/tools/patch_config.py db-a mysql_connections 70 2>/dev/null || true

else
  warn "Kind cluster 未啟動，跳過即時工具示範"
  info "執行 'make setup' 啟動 cluster 後重試"
fi

# -----------------------------------------------
# Summary
# -----------------------------------------------
echo ""
echo -e "${BOLD}============================================${NC}"
echo -e "${BOLD}  Demo 完成！${NC}"
echo -e "${BOLD}============================================${NC}"
echo ""
echo "  工具一覽:"
echo "    scaffold_tenant.py  — 互動式 tenant config 產生器"
echo "    migrate_rule.py     — 傳統 alert rules 遷移工具"
echo "    patch_config.py     — 動態閾值更新"
echo "    diagnose.py         — Tenant 健康檢查"
echo "    check_alert.py      — Alert 狀態查詢"
echo ""
echo "  Rule Packs:"
echo "    rule-packs/rule-pack-kubernetes.yaml      (已預載)"
echo "    rule-packs/rule-pack-mariadb.yaml          (已預載)"
echo "    rule-packs/rule-pack-redis.yaml            (已預載)"
echo "    rule-packs/rule-pack-mongodb.yaml          (已預載)"
echo "    rule-packs/rule-pack-elasticsearch.yaml    (已預載)"
echo "    rule-packs/rule-pack-platform.yaml         (已預載)"
echo ""
echo "  詳見: docs/migration-guide.md"
echo "        rule-packs/README.md"

# Clean up
rm -rf "$DEMO_DIR"
