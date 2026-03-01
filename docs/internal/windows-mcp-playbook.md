# Windows-MCP — Dev Container 操作手冊 (Playbook)

> AI Agent 透過 Windows-MCP Shell / Desktop Commander 操作 Dev Container 的最佳實踐與已知陷阱。
> **相關文件：** [Testing Playbook](testing-playbook.md) (K8s/測試排錯、負載注入、Benchmark)

## 前提

kubectl/kind/go/helm 僅在 Dev Container (`vibe-dev-container`) 內可用。
Go module 路徑: `components/threshold-exporter/app/` (非根目錄)。
Python 測試可在 Cowork VM 直接跑（`python3 -m pytest tests/ -v`）。

## 核心模式 — docker exec stdout 永遠為空

Windows MCP Shell 執行 `docker exec` 時，**stdout 一律被 PowerShell 吞掉**，無論指令多簡單。唯一可靠做法：在 `bash -c` 內部重定向到 mounted workspace 檔案，再用 Read tool 讀取。

```bash
# ✅ 正確: 輸出重定向在 bash -c 內部
docker exec vibe-dev-container bash -c "\
  kubectl get pods -A > /workspaces/vibe-k8s-lab/_output.txt 2>&1"
# → 再用 Read tool 讀 _output.txt

# ❌ 錯誤: PS 會搶走 > 重定向，stdout 也為空
docker exec vibe-dev-container kubectl get pods > output.txt
docker exec vibe-dev-container kubectl get pods -A
```

**黃金法則：** 只要指令含引號嵌套、管道、複雜邏輯，一律寫成 `.sh` 腳本放 mounted workspace，再 `docker exec bash script.sh`。

## 非同步腳本模式 — 長時間操作 (>60s)

Desktop Commander `start_process` 硬上限 **60 秒**。超過的操作用背景腳本模式：

```bash
# Step 1: 用 Write tool 寫腳本到 mounted workspace
#!/bin/bash
exec > /workspaces/vibe-k8s-lab/_task_result.txt 2>&1  # 腳本內重定向
# ... 所有操作 ...
echo "DONE"

# Step 2: 背景啟動（-d 只接腳本路徑，不接 bash -c "..."）
docker exec -d vibe-dev-container bash /workspaces/vibe-k8s-lab/scripts/_task.sh

# Step 3: 等待（在 Cowork VM 的 Bash tool 用 sleep，不要用 PowerShell）
sleep 120

# Step 4: 用 Read tool 讀取 _task_result.txt → 確認結尾有 "DONE"

# Step 5: 清理
docker exec vibe-dev-container rm -f /workspaces/vibe-k8s-lab/scripts/_task.sh /workspaces/vibe-k8s-lab/_task_result.txt
```

**注意：**
- `docker exec -d` 的 stdout 不會返回 → 必須在腳本開頭 `exec > output.txt 2>&1`
- `docker exec -d` **不能用 `bash -c "..."`**（CMD 引號嵌套失敗）→ 只能接腳本路徑
- 等待用 Cowork VM 的 `sleep`（Bash tool），不要用 `Start-Sleep`（PowerShell timeout 不穩定）

## 工具選擇策略

| 情境 | 推薦方式 | 原因 |
|------|---------|------|
| 簡單 K8s 查詢 | 先試 K8s MCP → fallback docker exec | K8s MCP 常 TLS timeout |
| 多步操作/含等待 | 寫腳本 → `docker exec bash script.sh` | 避免 timeout |
| curl Prometheus API | `docker exec` + port-forward | ClusterIP 在 container 外不可達 |
| Python tests | Cowork VM 直接跑 `python3 -m pytest` | Go 才需要 docker exec |
| Go build/test | `docker exec -w .../app vibe-dev-container go ...` | Go 僅在 container 內 |
| 檔案清理 (mounted workspace) | `docker exec ... rm -f` | Cowork VM 無法直接 rm 掛載路徑 |

**K8s MCP 已知限制：** 常 TLS timeout（Docker Desktop 休眠後更頻繁）、`name` 必填不便列表、不支援 pipe/重定向。遇到 timeout 直接 fallback docker exec，不重試。

## Prometheus API 查詢模式

```bash
# port-forward + curl（寫成腳本或 bash -c，輸出導到檔案）
docker exec vibe-dev-container bash -c "\
  kubectl port-forward svc/prometheus 9090:9090 -n monitoring &>/dev/null & \
  sleep 2 && \
  curl -sg 'http://localhost:9090/api/v1/query?query=up' \
    > /workspaces/vibe-k8s-lab/_prom.txt 2>&1 && \
  kill %1 2>/dev/null"
```

**port-forward 殘留處理：** `pkill -f "port-forward.*prometheus"` 或 `fuser -k 9090/tcp`。

**⚠️ port-forward 在 Pod 重啟後會斷開。** 如果刪除/重建 Prometheus Pod（如 scaling-curve benchmark），必須重新建立 port-forward。流程：等待新 Pod Running → kill 舊 port-forward → 建立新 port-forward → sleep 2s。

**常用查詢：**

| 查詢 | 用途 |
|------|------|
| `sum(prometheus_rule_group_rules)` | 規則總數 |
| `count(prometheus_rule_group_rules)` | 群組數 |
| `sum(prometheus_rule_group_last_duration_seconds)` | 每週期總評估時間 |
| `count(count by(tenant)(user_threshold))` | 租戶數 |

## Helm Upgrade 防衝突

ConfigMap 被 `kubectl patch` 修改過 → Helm field-manager conflict：

```bash
# Step 1: server-side apply 取回 ownership
kubectl apply --server-side --force-conflicts --field-manager=helm \
  -f <(helm template threshold-exporter components/threshold-exporter/ -n monitoring)
# Step 2: 正常 helm upgrade
helm upgrade threshold-exporter components/threshold-exporter/ -n monitoring
```

## 已知陷阱

| # | 陷阱 | 解法 |
|---|------|------|
| 1 | docker exec stdout 永遠為空 | `bash -c` 內重定向至 workspace 檔案，再 Read tool 讀 |
| 2 | `bash -c "..."` 引號被 shell 拆解 | 寫成 `.sh` 腳本（黃金法則） |
| 3 | `docker exec -d bash -c "..."` 背景+引號雙殺 | `-d` 只接腳本路徑，腳本內 `exec > file 2>&1` |
| 4 | Go test `./...` 找不到 module | `-w .../components/threshold-exporter/app` |
| 5 | `start_process` 硬上限 60s | 寫腳本 → `docker exec -d` → sleep → 讀結果 |
| 6 | kubeconfig 過期 | `kind export kubeconfig --name dynamic-alerting-cluster` |
| 7 | port-forward 殘留 | `pkill -f port-forward` |
| 8 | port-forward 在 Pod 重啟後斷開 | 重新等待 Pod Ready → 建立新 port-forward |
| 9 | mounted workspace 無法從 VM 刪檔 | `docker exec ... rm -f` |
| 10 | K8s MCP TLS timeout | 直接 fallback docker exec，不重試 |
| 11 | `set -euo pipefail` + 未初始化變數 | 所有條件路徑都要有 default 值 |
| 12 | `_lib.sh` 彩色輸出污染 JSON | `--json` 模式腳本避免 source `_lib.sh`，或 `2>/dev/null` + 過濾 ANSI |
| 13 | 版號 drift（手動改 docs 但沒跑 bump_docs） | GA 前務必 `make version-check`，修正用 `make bump-docs` |

## 指令快速參考

```bash
# Pod 狀態（輸出導到檔案再讀）
docker exec vibe-dev-container bash -c "kubectl get pods -A > /workspaces/vibe-k8s-lab/_out.txt 2>&1"
# Go build/vet
docker exec -w /workspaces/vibe-k8s-lab/components/threshold-exporter/app vibe-dev-container go build -o /dev/null .
# Go micro-benchmark
docker exec -w /workspaces/vibe-k8s-lab/components/threshold-exporter/app vibe-dev-container go test -bench=. -benchmem -count=5 ./...
# Shell tests
docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container bash tests/test-migrate-tool.sh
# 負載注入
docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container ./scripts/run_load.sh --tenant db-a --type composite
docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container ./scripts/run_load.sh --cleanup
# 暫存檔清理
docker exec vibe-dev-container rm -f /workspaces/vibe-k8s-lab/_*.txt /workspaces/vibe-k8s-lab/_*.json
# 版號一致性檢查
docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container bash -c "python3 ./scripts/tools/bump_docs.py --check > /workspaces/vibe-k8s-lab/_ver.txt 2>&1"
```
