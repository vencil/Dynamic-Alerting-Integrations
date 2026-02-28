# Windows-MCP — Dev Container 操作手冊 (Playbook)

> AI Agent 透過 Windows-MCP Shell / Desktop Commander 操作 Dev Container 的最佳實踐與已知陷阱。
> **相關文件：** [Testing Playbook](testing-playbook.md) (K8s/測試排錯、負載注入陷阱)

## 前提

kubectl/kind/go/helm 僅在 Dev Container (`vibe-dev-container`) 內可用。
Go module 路徑: `components/threshold-exporter/app/` (非根目錄)。

## 核心模式 — docker exec + workspace mount 重定向

```bash
# ✅ 推薦: 重定向在 bash -c 內部，寫入 container 可見路徑
docker exec vibe-dev-container bash -c "\
  kubectl get pods -A > /workspaces/vibe-k8s-lab/output.txt 2>&1"
# → 用 Read tool 讀 /sessions/.../mnt/vibe-k8s-lab/output.txt

# ✅ 多指令串接 (重定向也在 bash -c 內)
docker exec vibe-dev-container bash -c "{ \
  echo '=== Pods ===' ; kubectl get pods -n monitoring ; \
  echo '=== Deploy ===' ; kubectl get deploy -n monitoring ; \
} > /workspaces/vibe-k8s-lab/output.txt 2>&1"

# ❌ 絕對不要: PS 會搶走 > 重定向
docker exec vibe-dev-container kubectl get pods > output.txt
```

## 非同步腳本模式 — 長時間操作

Desktop Commander 的 `docker exec` 在 PowerShell 中約 2-3 秒即返回，不會等待 container 內的命令完成。用以下模式處理超過 3 秒的操作：

```bash
# Step 1: 將完整邏輯寫成腳本（Write tool → scripts/_task.sh）
# Step 2: 腳本內輸出重定向到檔案
#!/bin/bash
{
  # ... 所有操作 ...
  echo "=== Done ==="
} > /workspaces/vibe-k8s-lab/_task_result.txt 2>&1
echo "DONE"   # 給 stdout 一個信號

# Step 3: 透過 Desktop Commander 啟動
docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container bash scripts/_task.sh

# Step 4: 用 Bash tool sleep 等待（不用 Desktop Commander，它也有 timeout）
sleep 30 && echo "WAIT_DONE"

# Step 5: 用 Read tool 讀取結果
Read /sessions/.../mnt/vibe-k8s-lab/_task_result.txt

# Step 6: 事後清理腳本和結果檔
docker exec vibe-dev-container rm -f /workspaces/vibe-k8s-lab/scripts/_task.sh /workspaces/vibe-k8s-lab/_task_result.txt
```

**不要試圖** 在 Desktop Commander 的 `start_process` 中用 `Start-Sleep` 超過 30 秒 — 會觸發 MCP timeout。用 Claude Code 的 `Bash` tool (`sleep N`) 做長等待。

## Desktop Commander cmd shell 模式 — 繞過 PowerShell 限制

當 PowerShell 的管道或引號解析導致 docker exec 失敗時，改用 Desktop Commander 的 `cmd` shell：

```
start_process(command: "docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container bash scripts/_task.sh",
              shell: "cmd", timeout_ms: 60000)
```

**關鍵限制：** `start_process` 的硬上限為 60 秒（無論 `timeout_ms` 設多大）。超過 60s 的操作須拆成多個子腳本，每個 <60s，依序執行。

**典型拆法（以 demo-full Step 6 為例）：**
1. `_inject.sh` — 啟動負載（~10s）
2. `_check.sh` — 等待 alert firing + 查詢狀態（~50s）
3. `_cleanup.sh` — 清除負載（~10s）
4. `_recovery.sh` — 等待 alert 解除 + 驗證（~50s）

## Kubernetes MCP vs docker exec — 選擇策略

| 情境 | 推薦方式 | 原因 |
|------|---------|------|
| 簡單查詢 (get pods/svc) | K8s MCP `kubectl_get` | 直覺、省 token |
| 複雜查詢或多步操作 | `docker exec` via Windows-MCP Shell | K8s MCP 常 timeout 30s |
| 需要 curl Prometheus API | `docker exec` + port-forward | ClusterIP 在 container 外不可達 |
| Context 切換/列表 | K8s MCP `kubectl_context` | 穩定、不需 docker |
| 檔案清理 (mounted workspace) | `docker exec ... rm -f` | Linux VM 無法直接 rm 掛載路徑 |
| 負載注入 + 驗證（多步，含等待） | 寫成腳本 → docker exec | 非同步模式，避免 timeout |

**K8s MCP 已知限制：**
- `kubectl_generic` 易 timeout（超過 30s 的操作改用 docker exec）
- `kubectl_get` 的 `name` 參數為必填，列表操作不方便
- 不支援 pipe、重定向、多指令串接

## Prometheus API 查詢模式

ClusterIP 在 dev container 外不可達。使用一次性 port-forward 模式：

```bash
# 背景 port-forward + curl + 清理（一條指令完成）
docker exec vibe-dev-container bash -c "\
  kubectl port-forward svc/prometheus 9090:9090 -n monitoring &>/dev/null & \
  sleep 2 && \
  curl -sg 'http://localhost:9090/api/v1/query?query=up' \
    > /workspaces/vibe-k8s-lab/prom-result.txt 2>&1 && \
  kill %1 2>/dev/null"
```

### port-forward 端口衝突

若前一次 port-forward 未清理，新的會失敗。在腳本中先殺殘留：

```bash
# 在 bash -c 內
if command -v lsof &>/dev/null; then
  lsof -ti:9090 | xargs kill -9 2>/dev/null || true
elif command -v fuser &>/dev/null; then
  fuser -k 9090/tcp 2>/dev/null || true
fi
sleep 1
kubectl port-forward svc/prometheus 9090:9090 -n monitoring &>/dev/null &
```

### 常用 Benchmark 查詢

```bash
# 多個查詢一次完成
docker exec vibe-dev-container bash -c "\
  kubectl port-forward svc/prometheus 9090:9090 -n monitoring &>/dev/null & sleep 2 && \
  curl -sg 'http://localhost:9090/api/v1/query?query=sum(prometheus_rule_group_rules)' > /workspaces/vibe-k8s-lab/b1.txt && \
  curl -sg 'http://localhost:9090/api/v1/query?query=sum(prometheus_rule_group_last_duration_seconds)' > /workspaces/vibe-k8s-lab/b2.txt && \
  curl -sg 'http://localhost:9090/api/v1/query?query=prometheus_rule_group_duration_seconds' > /workspaces/vibe-k8s-lab/b3.txt && \
  kill %1 2>/dev/null"
```

| 查詢 | 用途 |
|------|------|
| `sum(prometheus_rule_group_rules)` | 規則總數 |
| `count(prometheus_rule_group_rules)` | 群組數 |
| `sum(prometheus_rule_group_last_duration_seconds)` | 每週期總評估時間 |
| `prometheus_rule_group_duration_seconds` | 各百分位 (p50/p99) |
| `count(count by(tenant)(user_threshold))` | 租戶數 |
| `mysql_global_status_threads_connected{tenant="db-a"}` | 即時連線數（驗證負載注入） |
| `tenant:pod_weakest_cpu_percent:max{tenant="db-a"}` | 弱環節 CPU%（驗證 stress-ng） |

## 已知陷阱

| # | 陷阱 | 解法 |
|---|------|------|
| 1 | docker stdout 被 PS 吞掉 | **一律** `bash -c` 內重定向至 `/workspaces/vibe-k8s-lab/*.txt`，再 Read tool |
| 2 | PS `> file.txt` 走 host path | 重定向**必須在 `bash -c "..."` 內部** |
| 3 | `bash -c '...'` 引號被 PS 拆解 | 外層雙引號 `bash -c "..."`，內部單引號 |
| 4 | UTF-8 emoji 輸出消失 | 用 exit code 判斷 (`set -euo pipefail`) |
| 5 | Go test `./...` 找不到 module | `-w /workspaces/vibe-k8s-lab/components/threshold-exporter/app` |
| 6 | 長時間操作 timeout | 寫成腳本 → `docker exec ... bash script.sh` → `sleep N` → Read 結果 |
| 7 | kubeconfig 過期 | `kind export kubeconfig --name dynamic-alerting-cluster` |
| 8 | port-forward 殘留 | `docker exec vibe-dev-container pkill -f port-forward` |
| 9 | Python 引號衝突 | 寫檔再執行，或 `python3 -c "..."` 包單引號 |
| 10 | mounted workspace 無法從 VM 刪檔 | 用 `docker exec ... rm -f` 清理暫存檔 |
| 11 | K8s MCP timeout | Fallback 到 `docker exec` via Windows-MCP Shell |
| 12 | Desktop Commander `Start-Sleep` > 30s | 改用 Bash tool 的 `sleep N`（支援 10 分鐘） |
| 13 | 複雜 Python inline 在 PS 中崩潰 | PS 會解析 `f'{}'`、`for...in` → 寫腳本檔避開 PS 解析 |
| 14 | Desktop Commander `start_process` 硬上限 60s | `timeout_ms` 參數被 cap 在 60s，長操作必須拆成多個 <60s 的子腳本 |
| 15 | PS 管道 docker exec 失敗 (`CantActivateDocumentInPipeline`) | 改用 Desktop Commander 的 `cmd` shell，或在 `bash -c` 內重定向 |
| 16 | `cmd` shell 下 `bash -c "..."` 引號被吞 | 不要透過 cmd 傳遞 inline bash -c，改寫 .sh 到 mounted workspace 再 `docker exec bash script.sh` |
| 17 | K8s MCP 在 Docker Desktop 未啟動時 TLS timeout | 先確認 `docker ps` 正常、Kind 節點 Running，再使用 K8s MCP |

## Helm Upgrade 防衝突流程

ConfigMap 被 `kubectl patch` 修改過 → Helm field-manager conflict：

```bash
# Step 1: server-side apply 取回 ownership
docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container bash -c "\
  kubectl apply --server-side --force-conflicts --field-manager=helm \
    -f <(helm template threshold-exporter components/threshold-exporter/ -n monitoring) \
  > /workspaces/vibe-k8s-lab/output.txt 2>&1"

# Step 2: 正常 helm upgrade
docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container bash -c "\
  helm upgrade threshold-exporter components/threshold-exporter/ -n monitoring \
  > /workspaces/vibe-k8s-lab/output.txt 2>&1"
```

**注意：** `helm upgrade --force` 與 server-side apply 互斥，不要用 `--force`。

## Cowork VM 直接執行 vs docker exec

Cowork 環境的 Linux VM 已有 Python3 + pytest，**Python 測試可直接在 VM 內跑**（不需 docker exec）：

```bash
# ✅ Cowork VM 直接執行 Python tests
cd /sessions/.../mnt/vibe-k8s-lab && python3 -m pytest tests/ -v

# Go tests 仍需 docker exec（Go 工具鏈僅在 Dev Container 內）
docker exec -w /workspaces/vibe-k8s-lab/components/threshold-exporter/app \
  vibe-dev-container bash -c "go test ./... -v > /workspaces/vibe-k8s-lab/.go-test.txt 2>&1"
```

### Go test count 擷取

`--- PASS` 前綴的三個 dash 會被 grep 誤解為 option：

```bash
# ❌ grep 把 --- 當 option
grep -cF "--- PASS" .go-test.txt

# ✅ 用 -- 終止 option 解析
grep -cF -- "--- PASS" .go-test.txt
```

## 指令快速參考

```bash
# 叢集 & Pod 狀態
docker exec vibe-dev-container bash -c "kubectl get pods -A > /workspaces/vibe-k8s-lab/output.txt 2>&1"

# Go 編譯 & 靜態分析
docker exec -w /workspaces/vibe-k8s-lab/components/threshold-exporter/app vibe-dev-container go build -o /dev/null .
docker exec -w /workspaces/vibe-k8s-lab/components/threshold-exporter/app vibe-dev-container go vet ./...

# Python 工具測試
docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container bash tests/test-migrate-tool.sh
docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container bash tests/test-scaffold.sh

# K8s manifests apply
docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container bash -c "\
  kubectl apply -f k8s/03-monitoring/ > /workspaces/vibe-k8s-lab/output.txt 2>&1"

# HA 驗證 (一次看完)
docker exec vibe-dev-container bash -c "{ \
  kubectl get deploy threshold-exporter -n monitoring ; \
  kubectl get pods -n monitoring -l app=threshold-exporter ; \
  kubectl get pdb -n monitoring ; \
} > /workspaces/vibe-k8s-lab/output.txt 2>&1"

# 負載注入 (在 dev container 內執行)
docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container ./scripts/run_load.sh --tenant db-a --type connections
docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container ./scripts/run_load.sh --tenant db-a --type composite
docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container ./scripts/run_load.sh --cleanup

# 暫存檔清理 (必須透過 container)
docker exec vibe-dev-container bash -c "rm -f /workspaces/vibe-k8s-lab/tmp-*.txt /workspaces/vibe-k8s-lab/_*.txt"
```
