# Windows-MCP — Dev Container 操作手冊 (Playbook)

> AI Agent 透過 Windows-MCP Shell / Desktop Commander / Cowork VM 操作 Dev Container 的最佳實踐與已知陷阱。
> **相關文件：** [Testing Playbook](testing-playbook.md) (K8s/測試排錯、Benchmark) | [GitHub Release Playbook](github-release-playbook.md) (push + release 流程)

## 環境概覽

| 元件 | 位置 | 備註 |
|------|------|------|
| kubectl / kind / go / helm | Dev Container (`vibe-dev-container`) 內 | Cowork VM 無法直接使用 |
| Python tests | Cowork VM 可直接跑 | `python3 -m pytest tests/ -v` |
| Go build/test | Dev Container 內 | `-w .../components/threshold-exporter/app` |
| 純 Python 工具 (routing-bench 等) | 兩者皆可 | 不依賴 K8s 的優先用 Cowork VM |
| Mounted workspace | `/workspaces/vibe-k8s-lab` (container 內) | 雙向可見 |

## 核心原則：docker exec stdout 為空

Windows MCP Shell 執行 `docker exec` 時，**stdout 被 PowerShell 吞掉**。唯一可靠做法：

```bash
# ✅ bash -c 內部重定向到 mounted workspace
docker exec vibe-dev-container bash -c "\
  kubectl get pods -A > /workspaces/vibe-k8s-lab/_output.txt 2>&1"
# → 再用 Read tool 讀 _output.txt

# ❌ 以下全部不可靠
docker exec vibe-dev-container kubectl get pods > output.txt   # PS 搶走重定向
docker exec vibe-dev-container kubectl get pods -A              # stdout 為空
```

## Shell 選擇：用 cmd 不用 PowerShell

PowerShell 對 docker exec 有額外的編碼/引號問題：

- PowerShell 會自動轉碼 UTF-8 輸出，產生亂碼
- 巢狀引號被 PowerShell 預處理後再傳給 docker，導致語法錯誤
- **Windows MCP Shell 指定 `shell: "cmd"` 可避免多數問題**

```bash
# ✅ 用 cmd shell
docker exec vibe-dev-container bash /workspaces/vibe-k8s-lab/scripts/_task.sh

# ❌ PowerShell 下巢狀引號被拆解
docker exec vibe-dev-container bash -c "echo '{\"key\": \"value\"}'"
```

## 黃金法則：複雜指令寫成獨立腳本

只要指令含引號嵌套、管道、JSON 處理、多步邏輯，一律：
1. 用 Write tool 寫 `.sh` 或 `.py` 腳本到 mounted workspace
2. `docker exec bash /workspaces/vibe-k8s-lab/scripts/_task.sh`
3. 結果從重定向檔案讀取
4. 完成後清理暫存腳本

這比嘗試修復 `bash -c "..."` 引號問題更快更可靠。

## 長時間操作 (>60s)

Desktop Commander `start_process` 硬上限 **60 秒**（`timeout_ms` 參數無效）。超過的操作用背景腳本：

```bash
# Step 1: Write tool 寫腳本
#!/bin/bash
exec > /workspaces/vibe-k8s-lab/_result.txt 2>&1
# ... 操作 ...
echo "DONE"

# Step 2: 背景啟動（-d 只接腳本路徑）
docker exec -d vibe-dev-container bash /workspaces/vibe-k8s-lab/scripts/_task.sh

# Step 3: Cowork VM Bash tool 等待
sleep 120

# Step 4: Read tool 讀 _result.txt，確認結尾有 "DONE"

# Step 5: 清理暫存
docker exec vibe-dev-container rm -f /workspaces/vibe-k8s-lab/scripts/_task.sh /workspaces/vibe-k8s-lab/_result.txt
```

**注意：** `docker exec -d` 的 stdout 不返回 → 腳本開頭必須 `exec > file 2>&1`。

## 工具選擇策略

| 情境 | 推薦方式 | 原因 |
|------|---------|------|
| 純 Python（routing-bench、validate）| Cowork VM 直接跑 | 最快，無 docker 開銷 |
| K8s 查詢 | 先試 K8s MCP → fallback docker exec | K8s MCP 常 TLS timeout |
| 多步 K8s 操作 | 寫腳本 → `docker exec bash script.sh` | 避免 timeout + 引號問題 |
| Prometheus / Alertmanager API | docker exec + port-forward | ClusterIP 在 container 外不可達 |
| Go build/test | `docker exec -w .../app vibe-dev-container go ...` | Go 僅在 container 內 |
| 檔案清理 (mounted workspace) | `docker exec ... rm -f` | Cowork VM 無法直接 rm 掛載路徑 |

**K8s MCP 已知限制：** 常 TLS timeout、`name` 必填、不支援 pipe/重定向。timeout 直接 fallback docker exec，不重試。

## Port-Forward 模式

### Prometheus

```bash
docker exec vibe-dev-container bash -c "\
  kubectl port-forward svc/prometheus 9090:9090 -n monitoring &>/dev/null & \
  sleep 2 && \
  curl -sg 'http://localhost:9090/api/v1/query?query=up' \
    > /workspaces/vibe-k8s-lab/_prom.txt 2>&1 && \
  kill %1 2>/dev/null"
```

### Alertmanager

```bash
docker exec vibe-dev-container bash -c "\
  kubectl port-forward svc/alertmanager 9093:9093 -n monitoring &>/dev/null & \
  sleep 2 && \
  curl -sg 'http://localhost:9093/api/v2/status' \
    > /workspaces/vibe-k8s-lab/_am.txt 2>&1 && \
  kill %1 2>/dev/null"
```

**殘留處理：** `pkill -f "port-forward.*prometheus"` 或 `fuser -k 9090/tcp`。

**Pod 重啟後 port-forward 斷開：** 等新 Pod Running → kill 舊 port-forward → 建新 → sleep 2s。

## Helm Upgrade 防衝突

ConfigMap 被 `kubectl patch` 修改過 → Helm field-manager conflict：

```bash
# Step 1: server-side apply 取回 ownership
kubectl apply --server-side --force-conflicts --field-manager=helm \
  -f <(helm template threshold-exporter components/threshold-exporter/ -n monitoring)
# Step 2: 正常 helm upgrade
helm upgrade threshold-exporter components/threshold-exporter/ -n monitoring
```

## PowerShell REST API（GitHub 等）

Windows MCP PowerShell 是 Cowork VM 無法直連的 API（如 `api.github.com`）的橋樑。

**JSON body 最佳實踐：**

```powershell
# ✅ 單行字串賦值 — 最可靠
$b = '{"tag_name":"v1.8.0","name":"v1.8.0","body":"notes","draft":false}'
Invoke-RestMethod -Uri $url -Method Post -Headers $headers -Body $b

# ❌ @{} + ConvertTo-Json — & 字元問題、型別轉換不穩定
# ❌ Heredoc / 多行字串 — quote mangling
# ❌ 外部 .ps1 腳本 — OneDrive 路徑含空格找不到
```

**Headers 模板：**

```powershell
$headers = @{ "Authorization" = "Bearer $token"; "Accept" = "application/vnd.github+json"; "X-GitHub-Api-Version" = "2022-11-28"; "Content-Type" = "application/json" }
```

詳見 [GitHub Release Playbook](github-release-playbook.md)。

## 已知陷阱速查

| # | 陷阱 | 解法 |
|---|------|------|
| 1 | docker exec stdout 為空 | `bash -c` 內重定向至 workspace 檔案 |
| 2 | `bash -c "..."` 引號被拆解 | 寫成獨立 `.sh` / `.py` 腳本 |
| 3 | PowerShell 編碼亂碼 | MCP Shell 指定 `shell: "cmd"` |
| 4 | `docker exec -d bash -c "..."` 失敗 | `-d` 只接腳本路徑，腳本內 `exec > file 2>&1` |
| 5 | Go test `./...` 找不到 module | `-w .../components/threshold-exporter/app` |
| 6 | `start_process` 硬上限 60s | 寫腳本 → `docker exec -d` → sleep → 讀結果 |
| 7 | kubeconfig 過期 | `kind export kubeconfig --name dynamic-alerting-cluster` |
| 8 | port-forward 殘留 / Pod 重啟後斷開 | `pkill -f port-forward`；重建需等 Pod Ready |
| 9 | mounted workspace 無法從 VM 刪檔 | `docker exec ... rm -f` |
| 10 | K8s MCP TLS timeout | 直接 fallback docker exec |
| 11 | `set -euo pipefail` + 未初始化變數 | 所有條件路徑都要有 default 值 |
| 12 | 彩色輸出 / ANSI 碼污染 JSON | `--json` 模式避免 source `_lib.sh`，或 `2>/dev/null` + 過濾 ANSI |
| 13 | 版號 drift | `make version-check`；修正用 `make bump-docs` |
| 14 | PS `ConvertTo-Json` / heredoc 產 JSON 失敗 | 用 `$b = '{"k":"v"}'` 單行字串（見上方 REST API 章節） |
| 15 | PS 外部 `.ps1` 腳本路徑含空格 | OneDrive 預設路徑含空格；避免外部腳本，用 inline 單行 |

## 指令快速參考

```bash
# Pod 狀態
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
# 版號一致性
docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container bash -c "python3 ./scripts/tools/bump_docs.py --check > /workspaces/vibe-k8s-lab/_ver.txt 2>&1"
```
