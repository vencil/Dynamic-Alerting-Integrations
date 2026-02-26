# Windows-MCP — Dev Container 操作手冊 (Playbook)

> AI Agent 透過 Windows-MCP Shell / Desktop Commander 操作 Dev Container 的最佳實踐與已知陷阱。
> **相關文件：** [Testing Playbook](testing-playbook.md) (K8s/測試排錯)

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

## Kubernetes MCP vs docker exec — 選擇策略

| 情境 | 推薦方式 | 原因 |
|------|---------|------|
| 簡單查詢 (get pods/svc) | K8s MCP `kubectl_get` | 直覺、省 token |
| 複雜查詢或多步操作 | `docker exec` via Windows-MCP Shell | K8s MCP 常 timeout 30s |
| 需要 curl Prometheus API | `docker exec` + port-forward | ClusterIP 在 container 外不可達 |
| Context 切換/列表 | K8s MCP `kubectl_context` | 穩定、不需 docker |
| 檔案清理 (mounted workspace) | `docker exec ... rm -f` | Linux VM 無法直接 rm 掛載路徑 |

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

**常用 Benchmark 查詢：**
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
| `prometheus_rule_group_last_duration_seconds` | 各群組最近評估時間 |
| `count(count by(tenant)(user_threshold))` | 租戶數 |

## 已知陷阱

| # | 陷阱 | 解法 |
|---|------|------|
| 1 | docker stdout 被 PS 吞掉 | **一律** `bash -c` 內重定向至 `/workspaces/vibe-k8s-lab/*.txt`，再 Read tool |
| 2 | PS `> file.txt` 走 host path | 重定向**必須在 `bash -c "..."` 內部** |
| 3 | `bash -c '...'` 引號被 PS 拆解 | 外層雙引號 `bash -c "..."`，內部單引號 |
| 4 | UTF-8 emoji 輸出消失 | 用 exit code 判斷 (`set -euo pipefail`) |
| 5 | Go test `./...` 找不到 module | `-w /workspaces/vibe-k8s-lab/components/threshold-exporter/app` |
| 6 | 長時間測試 timeout | Desktop Commander `start_process` (支援 600s) |
| 7 | kubeconfig 過期 | `kind export kubeconfig --name dynamic-alerting-cluster` |
| 8 | port-forward 殘留 | `docker exec vibe-dev-container pkill -f port-forward` |
| 9 | Python 引號衝突 | 寫檔再執行，或 `python3 -c "..."` 包單引號 |
| 10 | mounted workspace 無法從 VM 刪檔 | 用 `docker exec ... rm -f` 清理暫存檔 |
| 11 | K8s MCP timeout | Fallback 到 `docker exec` via Windows-MCP Shell |

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

# 暫存檔清理 (必須透過 container)
docker exec vibe-dev-container bash -c "rm -f /workspaces/vibe-k8s-lab/tmp-*.txt"
```
