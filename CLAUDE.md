# CLAUDE.md — AI 開發上下文指引

## 專案概述 (Current Status)
**Multi-Tenant Dynamic Alerting** 平台。
**當前進度**: Phase 2C 完成 — 目錄模式 (Directory Scanner) 實作 + 整合測試通過 (15/16)。
**核心機制**: Config-driven (ConfigMap 掛載), Hot-reload (SHA-256 hash 比對), 支援單檔與目錄兩種模式。

## Phase 1 完成摘要 (Week 1-4)
- **Week 1-2**: Kind 叢集、MariaDB sidecar、Prometheus Recording Rules 正規化層、threshold-exporter (Go) 三態邏輯 + Helm chart。
- **Week 3**: Scenario B (Weakest Link — cAdvisor 容器資源)、Scenario C (State Matching — kube-state-metrics 狀態乘法)。
- **Week 4**: Scenario D (維護模式 `unless`、複合警報 `and`、多層嚴重度 `_critical` 後綴降級)、Tech Debt 清理、工具轉正 (`patch_config.py`, `check_alert.py`, `diagnose.py`)。

## Phase 2 規劃 (Roadmap)
### 2A — Migration Guide ✅
- `docs/migration-guide.md`: 完整遷移指南，含 Percona MariaDB 五種場景範例、Alertmanager routing 遷移、驗證流程、LLM 輔助批量轉換 Prompt。

### 2B — 多 DB 支援擴展 (待開發)
- 目標: 支援 MongoDB, Redis, Elasticsearch 等 DB 類型。
- 挑戰: 多維度指標 (Index/Queue 級別閾值)、字串狀態 (cluster health green/yellow/red)。
- 設計方向: 擴充 ConfigMap 語法支援標籤選擇器；沿用 Scenario C state_filter 處理狀態型指標。
- 需修改: threshold-exporter Go 程式碼 (config parser)。

### 2C — GitOps Self-Service ✅
- **Directory Scanner**: ConfigMap 拆分為 `_defaults.yaml` + 每租戶 `<tenant>.yaml`，排序合併。
- **邊界規則**: `state_filters` / `defaults` 僅允許在 `_` 前綴檔案，租戶檔僅含 `tenants` 區塊，違規自動忽略 + WARN log。
- **雙模式**: `-config` (單檔) / `-config-dir` (目錄)，自動偵測，向下相容。
- **Hot-reload**: SHA-256 hash 比對 (取代 ModTime，K8s symlink rotation 更可靠)。
- **工具適配**: `patch_config.py` 雙模式自動偵測；`_lib.sh` 共用 `get_cm_value()`。
- **測試**: 20 單元測試通過 + `tests/integration-2c.sh` 整合驗證 (15/16 PASS，1 個 K8s timing)。
- **待擴展**: GitOps Repo + CI/CD pipeline。

### 2D — Migration Tooling (待開發)
- 目標: 自動化遷移工具，讀取傳統 `rules.yml` 並產生 `threshold-config.yaml`。
- 可結合 LLM Prompt (已在 migration-guide.md 中定義)。

## 核心組件與架構 (Architecture)
- **Cluster**: Kind (`dynamic-alerting-cluster`)
- **Namespaces**: `db-a`, `db-b` (Tenants), `monitoring` (Infra)
- **threshold-exporter** (`monitoring` ns, port 8080): YAML → Prometheus Metrics。三態邏輯 + `_critical` 多層嚴重度 + `default_state` 控制。支援單檔 (`-config`) 與目錄 (`-config-dir /etc/threshold-exporter/conf.d`) 兩種模式。
- **kube-state-metrics**: K8s 狀態指標 (Scenario C)。
- **Prometheus Normalization Layer**: `tenant:<component>_<metric>:<function>` 格式。
- **Scenario D 機制**: 維護模式 (`unless`)、複合警報 (`and`)、多層嚴重度降級。

## 開發與操作規範 (Strict Rules)
1. **ConfigMap 修改**: 禁止 `cat <<EOF` 覆寫。用 `kubectl patch` / `helm upgrade` / `patch_config.py`。
2. **Tenant-agnostic**: Go 與 PromQL 中禁止 Hardcode Tenant ID。
3. **三態邏輯**: Custom / Default (省略) / Disable (`"disable"`)。
4. **Doc-as-Code**: 功能完成後同步更新 `CHANGELOG.md`, `CLAUDE.md`, `README.md`。
5. **Makefile**: `make setup` (一鍵部署), `make port-forward` (9090/3000/8080)。

## 專案工具 (scripts/tools/)
- `patch_config.py <tenant> <metric_key> <value>`: 安全局部更新 ConfigMap (三態，自動偵測單檔/目錄模式)。
- `check_alert.py <alert_name> <tenant>`: JSON 回傳 alert 狀態 (firing/pending/inactive)。
- `diagnose.py <tenant>`: Exception-based 健康檢查。

## AI Agent 環境 (MCP Connectivity)

### Kubernetes MCP Server
- Context: `kind-dynamic-alerting-cluster`。全功能 kubectl 操作。
- Prometheus 查詢: `exec_in_pod` → `wget -qO- "http://localhost:9090/api/v1/query?query=<PromQL>"`
- Exporter 查詢: `exec_in_pod` → `wget -qO- "http://threshold-exporter.monitoring.svc:8080/metrics"`
- **注意**: Kubernetes MCP 直接連 Kind 叢集，若 Kind 未啟動會 timeout。先確認叢集存在再操作。

### Windows-MCP — Dev Container 操作模式 (最佳實踐)
kubectl/kind/go 僅在 Dev Container (`vibe-dev-container`) 內可用，不可直接從 Windows-MCP Shell 執行。

**核心模式 — `Start-Process` + 檔案重定向**:
```powershell
# 正確方式: 用 Start-Process 執行 docker exec，將輸出寫入檔案
Start-Process -FilePath 'C:\Program Files\Docker\Docker\resources\bin\docker.exe' `
  -ArgumentList @('exec','-w','/workspaces/vibe-k8s-lab','vibe-dev-container','<command>','<args>') `
  -NoNewWindow -Wait `
  -RedirectStandardOutput 'C:\temp\out.txt' `
  -RedirectStandardError 'C:\temp\err.txt'

# 讀取結果 (用 ReadAllText，不要用 Get-Content — 後者常有 pipeline 問題)
[System.IO.File]::ReadAllText('C:\temp\out.txt')
```

**常見陷阱與解法**:
1. **`docker` 直接呼叫無輸出**: PowerShell pipeline 問題。不要用 `docker ps | Select-Object`，改用 `Start-Process` + 檔案重定向。
2. **`bash -c '...'` 引號被吞**: PowerShell 會拆解 bash -c 後的引號。解法: 拆成獨立 arguments 傳入 `-ArgumentList @()`，或先用簡單指令確認可達性。
3. **Go 測試**: `docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container go test -v ./...` — 注意 `-w` 設工作目錄。
4. **長時間測試**: Windows-MCP Shell 預設 30s timeout。長測試用 `Desktop Commander start_process` (支援 600s)。
5. **kubeconfig 過期**: Dev Container 重啟後需重新匯出: `kind export kubeconfig --name dynamic-alerting-cluster --kubeconfig /root/.kube/config`。
6. **Port-forward 殘留**: 測試失敗後 port-forward 不會自動清理。下次測試前先: `docker exec vibe-dev-container pkill -f port-forward`。
7. **PyYAML**: Dev Container 內需確保已安裝: `pip3 install pyyaml`。`_lib.sh` 的 `get_cm_value()` 依賴此套件。

### 指令快速參考
```bash
# Dev Container 內 — 透過 docker exec 執行
docker exec vibe-dev-container kind get clusters
docker exec vibe-dev-container kubectl get pods -A
docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container go test -v ./...
docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container make component-build COMP=threshold-exporter
docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container make component-deploy COMP=threshold-exporter
docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container bash tests/integration-2c.sh
```

## 測試注意事項 (Testing Caveats)

### 測試前置準備
1. **確認 Dev Container 運行**: `docker ps` 應看到 `vibe-dev-container`。
2. **確認 Kind 叢集**: `docker exec vibe-dev-container kind get clusters` → `dynamic-alerting-cluster`。
3. **確認 kubeconfig**: `docker exec vibe-dev-container kubectl get nodes`。失敗則重新匯出 (見上方)。
4. **確認 PyYAML**: `docker exec vibe-dev-container python3 -c "import yaml"` 失敗則安裝。
5. **清理殘留 port-forward**: `docker exec vibe-dev-container pkill -f port-forward`。

### 已知問題與修復
1. **Helm upgrade 不清理舊 ConfigMap key**: 從單檔升級到目錄模式後，`config.yaml` key 殘留。
   - **現象**: Exporter WARN log `state_filters found in config.yaml`。
   - **修復**: `kubectl delete cm threshold-config -n monitoring` → `make component-deploy`。
2. **ConfigMap Volume Propagation 延遲**: K8s ConfigMap volume mount 更新延遲 30-90 秒。
   - **現象**: `patch_config.py` 更新 ConfigMap 後，exporter 的 `/metrics` 不立即反映。
   - **修復**: 整合測試中 hot-reload 驗證需等 45+ 秒 (`integration-2c.sh` 已調整)。
3. **Shell 腳本中 JSON 嵌入**: `_lib.sh` 的 `get_cm_value()` 不可用 `'''${var}'''` 內嵌 JSON（多行 YAML 值會破壞 Python string）。
   - **已修復**: 改用 `kubectl ... | python3 -c "json.load(sys.stdin)"`。
4. **Metrics label 順序**: `user_threshold` 的 label 順序為 `component, metric, severity, tenant`。grep 時注意不要假設 `tenant` 在 `metric` 前面。
   - **正確**: `grep 'metric="connections".*tenant="db-a"'`
   - **錯誤**: `grep 'tenant="db-a".*metric="connections"'`
5. **Scenario 測試殘留值**: 場景測試中斷後，ConfigMap 中 tenant 值可能停留在測試值 (如 mysql_connections=5)。
   - **修復**: 測試前用 `patch_config.py` 恢復預設值，或檢查 `get_cm_value` 確認當前值。
