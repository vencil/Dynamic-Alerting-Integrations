#!/bin/bash
# demo.sh — End-to-end demonstration of Dynamic Alerting workflow
# Usage: make demo              (工具展示，不含負載)
#        make demo-full         (完整展示，含 Live Load)
#        bash scripts/demo.sh --skip-load   (同 make demo)
#
# Demonstrates:
#   1. scaffold_tenant.py — 產生新 tenant config
#   2. migrate_rule.py    — 轉換傳統 alert rules
#   3. migrate_rule.py    — Dry-run 模式
#   4. diagnose / check_alert / patch_config — 即時工具
#   5. Live Load Injection — 真實負載觸發 alert (需要 --skip-load=false)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# --- Options ---
SKIP_LOAD=false
for arg in "$@"; do
  case "$arg" in
    --skip-load) SKIP_LOAD=true ;;
  esac
done

# Demo 用自己的色彩風格（不引入 _lib.sh，避免覆蓋 demo 的 info/step）
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
RED='\033[0;31m'
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

err_msg() {
  echo -e "  ${RED}✗${NC} $1"
}

# --- Alert 狀態查詢（共用邏輯，和 _lib.sh 相同但 demo 不 source _lib.sh）---
_demo_get_alert_status() {
  local alertname=$1 tenant=$2
  curl -sf "http://localhost:9090/api/v1/alerts" 2>/dev/null | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    alerts = [a for a in data['data']['alerts']
              if a.get('labels',{}).get('alertname') == '${alertname}'
              and '${tenant}' in str(a)]
    if any(a['state'] == 'firing' for a in alerts):
        print('firing')
    elif any(a['state'] == 'pending' for a in alerts):
        print('pending')
    else:
        print('inactive')
except:
    print('unknown')
" 2>/dev/null || echo "unknown"
}

_demo_prom_value() {
  local query=$1 default_val=${2:-N/A}
  curl -sf http://localhost:9090/api/v1/query \
    --data-urlencode "query=${query}" 2>/dev/null | \
    python3 -c "
import sys, json
try:
    r = json.load(sys.stdin)['data']['result']
    print(r[0]['value'][1] if r else '${default_val}')
except:
    print('${default_val}')
" 2>/dev/null || echo "${default_val}"
}

# --- Kill port (inline, 不依賴 _lib.sh) ---
_demo_kill_port() {
  local port=$1
  if command -v lsof &>/dev/null; then
    lsof -ti:"${port}" 2>/dev/null | xargs kill -9 2>/dev/null || true
  elif command -v fuser &>/dev/null; then
    fuser -k "${port}/tcp" 2>/dev/null || true
  fi
}

# --- Cleanup trap ---
DEMO_DIR="/tmp/demo-output"
PF_PID=""
demo_cleanup() {
  if [[ "${LOAD_STARTED:-false}" == "true" ]]; then
    echo ""
    warn "Cleaning up load-generator resources..."
    "${SCRIPT_DIR}/run_load.sh" --cleanup 2>/dev/null || true
  fi
  [[ -n "${PF_PID}" ]] && kill "${PF_PID}" 2>/dev/null || true
  rm -rf "$DEMO_DIR"
}
trap demo_cleanup EXIT

LOAD_STARTED=false
rm -rf "$DEMO_DIR"

echo ""
echo -e "${BOLD}============================================${NC}"
echo -e "${BOLD}  Dynamic Alerting — End-to-End Demo${NC}"
if [[ "$SKIP_LOAD" == "false" ]]; then
  echo -e "${BOLD}  Mode: Full (含 Live Load Injection)${NC}"
else
  echo -e "${BOLD}  Mode: Quick (工具展示)${NC}"
fi
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
CLUSTER_ALIVE=false
if kubectl cluster-info &>/dev/null; then
  CLUSTER_ALIVE=true
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
# Step 6: Live Load Injection (optional)
# -----------------------------------------------
if [[ "$SKIP_LOAD" == "false" ]] && [[ "$CLUSTER_ALIVE" == "true" ]]; then
  step "6" "Live Load Injection — Real Alert Demo"

  info "這一步將產生真實負載，觸發 alert pipeline，展示完整循環："
  info "  負載注入 → Alert FIRING → 清除負載 → Alert 自動消失"
  echo ""

  # --- 6a. Setup Prometheus port-forward ---
  info "6a. 建立 Prometheus 連線..."
  PROM_POD=$(kubectl get pods -n monitoring -l app=prometheus \
    --field-selector=status.phase=Running \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  if [[ -z "${PROM_POD}" ]]; then
    err_msg "Prometheus pod 未找到，跳過 load demo"
  else
    _demo_kill_port 9090
    sleep 1
    kubectl port-forward -n monitoring "pod/${PROM_POD}" 9090:9090 &>/dev/null &
    PF_PID=$!
    sleep 3

    if ! curl -sf -o /dev/null http://localhost:9090/-/ready 2>/dev/null; then
      err_msg "無法連線 Prometheus，跳過 load demo"
    else
      info "Prometheus API 已連線"
      echo ""

      # --- 6b. 確認乾淨狀態 ---
      info "6b. 清除任何殘留的 load-generator 資源..."
      "${SCRIPT_DIR}/run_load.sh" --cleanup 2>/dev/null || true
      sleep 3
      echo ""

      # --- 6c. 啟動 stress-ng (Container CPU) ---
      info "6c. 啟動 Container CPU 壓測 (stress-ng, CPU limit=100m)..."
      "${SCRIPT_DIR}/run_load.sh" --tenant db-a --type stress-ng
      LOAD_STARTED=true
      echo ""

      # --- 6d. 啟動 Connection Storm ---
      info "6d. 啟動 Connection Storm (95 idle connections)..."
      "${SCRIPT_DIR}/run_load.sh" --tenant db-a --type connections
      echo ""

      # --- 6e. 等待 alerts 觸發 ---
      info "6e. 等待 Prometheus 偵測負載並觸發 alerts..."
      info "  (需要 ~90 秒: scrape 15s + recording rule eval + alert for duration)"
      echo ""

      WAIT_TOTAL=90
      for i in $(seq "${WAIT_TOTAL}" -10 10); do
        printf "\r  %02d seconds remaining..." "${i}"
        sleep 10
      done
      printf "\r                              \r"
      echo ""

      # --- 6f. 展示 Alert 狀態 ---
      info "6f. Prometheus Alert 狀態："
      echo ""
      curl -sf 'http://localhost:9090/api/v1/alerts' 2>/dev/null | python3 -c "
import sys, json
data = json.load(sys.stdin)
alerts = data.get('data',{}).get('alerts',[])
firing = [a for a in alerts if a.get('state') == 'firing']
pending = [a for a in alerts if a.get('state') == 'pending']
if not alerts:
    print('  (尚無 alerts — 可能需要更多時間)')
for a in firing + pending:
    state = a.get('state','?')
    name = a['labels'].get('alertname','?')
    tenant = a['labels'].get('tenant','n/a')
    sev = a['labels'].get('severity','?')
    icon = 'FIRE' if state == 'firing' else 'PEND'
    print(f'  [{icon}] {name}  tenant={tenant}  severity={sev}')
" 2>/dev/null || echo "  (查詢失敗)"
      echo ""

      # --- 6g. 關鍵指標 ---
      info "6g. 關鍵 Prometheus 指標："
      TC=$(_demo_prom_value 'mysql_global_status_threads_connected{tenant="db-a"}' "N/A")
      CPU=$(_demo_prom_value 'tenant:pod_weakest_cpu_percent:max{tenant="db-a"}' "N/A")
      if [ "$CPU" != "N/A" ]; then
        CPU=$(printf '%.1f' "$CPU" 2>/dev/null || echo "N/A")
      fi
      echo "  mysql_threads_connected (db-a): ${TC} (threshold: 70)"
      echo "  pod_weakest_cpu_percent (db-a): ${CPU}% (threshold: 70%)"
      echo ""

      # --- 6h. 清除負載 ---
      info "6h. 清除所有 load-generator 資源..."
      "${SCRIPT_DIR}/run_load.sh" --cleanup
      LOAD_STARTED=false
      echo ""

      # --- 6i. 等待 alerts 消失 ---
      info "6i. 等待 alerts 自動解除 (~60s)..."
      for i in $(seq 60 -10 10); do
        printf "\r  %02d seconds remaining..." "${i}"
        sleep 10
      done
      printf "\r                              \r"
      echo ""

      # --- 6j. 展示恢復狀態 ---
      info "6j. 恢復後 Alert 狀態："
      echo ""
      curl -sf 'http://localhost:9090/api/v1/alerts' 2>/dev/null | python3 -c "
import sys, json
data = json.load(sys.stdin)
alerts = data.get('data',{}).get('alerts',[])
load_alerts = [a for a in alerts if a['labels'].get('alertname','') in
    ('MariaDBHighConnections','MariaDBHighConnectionsCritical',
     'PodContainerHighCPU','PodContainerHighCPUCritical',
     'PodContainerHighMemory','PodContainerHighMemoryCritical')]
if not load_alerts:
    print('  All load-related alerts resolved!')
else:
    for a in load_alerts:
        state = a.get('state','?')
        name = a['labels'].get('alertname','?')
        print(f'  [{state}] {name} (may need more time to resolve)')
" 2>/dev/null || echo "  (查詢失敗)"
      echo ""
    fi
  fi

elif [[ "$SKIP_LOAD" == "false" ]] && [[ "$CLUSTER_ALIVE" == "false" ]]; then
  warn "Kind cluster 未啟動，跳過 Live Load 展示"
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
echo "    run_load.sh         — Live Load Injection Toolkit"
echo ""
echo "  Makefile 捷徑:"
echo "    make demo           — 快速展示（工具 only）"
echo "    make demo-full      — 完整展示（含 Live Load）"
echo "    make load-connections/load-cpu/load-stress  — 單獨壓測"
echo "    make load-cleanup   — 清除壓測資源"
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
