---
title: "Windows-MCP — Dev Container 操作手冊 (Playbook)"
tags: [documentation]
audience: [all]
version: v2.4.0
lang: zh
---
# Windows-MCP — Dev Container 操作手冊 (Playbook)

> AI Agent 透過 Windows-MCP Shell / Desktop Commander / Cowork VM 操作 Dev Container 的最佳實踐與已知陷阱。
> **相關文件：** [Testing Playbook](testing-playbook.md)（K8s/測試排錯）· [Benchmark Playbook](benchmark-playbook.md)（方法論、踩坑）· [GitHub Release Playbook](github-release-playbook.md)（push + release 流程）

## 環境概覽

| 元件 | 位置 | 備註 |
|------|------|------|
| kubectl / kind / go / helm | Dev Container (`vibe-dev-container`) 內 | Cowork VM 無法直接使用 |
| Python tests | Cowork VM 可直接跑 | `python3 -m pytest tests/ -v` |
| Go build/test | Dev Container 內 | `-w ../components/threshold-exporter/app` |
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
| Go build/test | `docker exec -w ../app vibe-dev-container go ...` | Go 僅在 container 內 |
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

**JSON body 兩種可靠做法：**

```powershell
# 方法 A：單行字串 — 適合短 body、純 ASCII
$b = '{"tag_name":"v1.8.0","name":"v1.8.0","body":"notes","draft":false}'
Invoke-RestMethod -Uri $url -Method Post -Headers $headers -Body $b

# 方法 B：ConvertTo-Json + UTF8 Bytes — 適合長 body、CJK 字元
$payload = @{ tag_name = "v1.9.0"; name = "title"; body = $longText } | ConvertTo-Json -Depth 3
Invoke-RestMethod -Uri $url -Method Post -Headers $headers `
    -Body ([System.Text.Encoding]::UTF8.GetBytes($payload)) `
    -ContentType "application/json; charset=utf-8"
# ⚠️ 必須用 UTF8.GetBytes()，否則 CJK 字元亂碼

# ❌ 外部 .ps1 腳本 — OneDrive 路徑含空格找不到
```

**Headers 模板：**

```powershell
$headers = @{ "Authorization" = "token $token"; "Accept" = "application/vnd.github+json" }
```

詳見 [GitHub Release Playbook](github-release-playbook.md)。

### 長 Body 的建議做法

**優先用 here-string（`@"..."@`）**，避免 File Staging 的 PSObject 陷阱：

```powershell
# ✅ 推薦：here-string 直接定義 body（結果是純 [string]）
$body = @"
## Highlights
- Feature A
- Feature B（支援 CJK）
"@
$payload = @{ name = "title"; body = $body } | ConvertTo-Json -Depth 3
Invoke-RestMethod -Uri $url -Method Patch -Headers $headers `
    -Body ([System.Text.Encoding]::UTF8.GetBytes($payload)) `
    -ContentType "application/json; charset=utf-8"
```

**File Staging 模式**（body 太長超出 here-string 限制時）：

```powershell
# Step 1: Desktop Commander write_file 寫 body 到暫存路徑
#   C:/Users/<user>/AppData/Local/Temp/release-body.txt

# Step 2: PowerShell 讀檔 — ⚠️ 必須 .ToString() 或 [string] 轉型
$bodyText = [string](Get-Content "C:/Users/<user>/AppData/Local/Temp/release-body.txt" -Raw)
$payload = @{ name = "title"; body = $bodyText } | ConvertTo-Json -Depth 3
Invoke-RestMethod -Uri $url -Method Patch -Headers $headers `
    -Body ([System.Text.Encoding]::UTF8.GetBytes($payload)) `
    -ContentType "application/json; charset=utf-8"

# Step 3: 清理
Remove-Item "C:/Users/<user>/AppData/Local/Temp/release-body.txt" -Force
```

> **⚠️ 已知陷阱**：`Get-Content -Raw` 回傳的是 PSObject（帶 PSPath、PSDrive、PSProvider 等 metadata），不是純字串。若直接放入 hashtable 再 `ConvertTo-Json`，會把整個 filesystem metadata 序列化進 JSON body，導致 API payload 變成數千行的物件 dump。必須用 `[string]` cast 或 `.ToString()` 確保是純文字。

## 已知陷阱速查

| # | 陷阱 | 解法 |
|---|------|------|
| 1 | docker exec stdout 為空 | `bash -c` 內重定向至 workspace 檔案 |
| 2 | `bash -c "..."` 引號被拆解 | 寫成獨立 `.sh` / `.py` 腳本 |
| 3 | PowerShell 編碼亂碼 | MCP Shell 指定 `shell: "cmd"` |
| 4 | `docker exec -d bash -c "..."` 失敗 | `-d` 只接腳本路徑，腳本內 `exec > file 2>&1` |
| 5 | Go test `./...` 找不到 module | `-w ../components/threshold-exporter/app` |
| 6 | `start_process` 硬上限 60s | 寫腳本 → `docker exec -d` → sleep → 讀結果 |
| 7 | kubeconfig 過期 | `kind export kubeconfig --name dynamic-alerting-cluster` |
| 8 | port-forward 殘留 / Pod 重啟後斷開 | `pkill -f port-forward`；重建需等 Pod Ready |
| 9 | mounted workspace 無法從 VM 刪檔 | `docker exec ... rm -f` |
| 10 | K8s MCP TLS timeout | 直接 fallback docker exec |
| 11 | `set -euo pipefail` + 未初始化變數 | 所有條件路徑都要有 default 值 |
| 12 | 彩色輸出 / ANSI 碼污染 JSON | `--json` 模式避免 source `_lib.sh`，或 `2>/dev/null` + 過濾 ANSI |
| 13 | 版號 drift | `make version-check`；修正用 `make bump-docs` |
| 14 | PS JSON body CJK 亂碼 | `ConvertTo-Json` + `[System.Text.Encoding]::UTF8.GetBytes()` + `charset=utf-8` |
| 15 | PS 外部 `.ps1` 腳本路徑含空格 | OneDrive 預設路徑含空格；避免外部腳本，用 inline |
| 16 | PAT push `.github/workflows/` 被 reject | PAT 需含 Workflows scope（詳見 [GitHub Release Playbook](github-release-playbook.md)） |
| 17 | Windows MCP Shell 長 REST body timeout | 用 Desktop Commander `write_file` 寫暫存檔 → PowerShell `Get-Content -Raw` 讀入 → 完成後 `Remove-Item` |
| 18 | GitHub Release `already_exists` 422 | tag 推送後 GitHub 可能自動建 release；改用 PATCH 更新（GET tag → 取 id → PATCH body） |
| 19 | Dev Container `Exited (255)` 未啟動 | `docker start vibe-dev-container`；每次 session 開始先 `docker ps` 確認 |
| 20 | Benchmark / Go test 複雜指令在 PowerShell 下失敗 | 寫 `.sh` 輔助腳本 → `docker exec [-d] bash script.sh`（見 [Benchmark Playbook → 在 Dev Container 內執行](benchmark-playbook.md#在-dev-container-內執行)）|
| 21 | Go test 從 repo root 執行失敗 | `go.mod` 在 `components/threshold-exporter/app/`，必須 `-w` 指定或 `cd` 進去 |
| 22 | `Get-Content -Raw` 是 PSObject 非純字串 | 放入 hashtable → `ConvertTo-Json` 會序列化 filesystem metadata；用 `[string]` cast 或改用 here-string `@"..."@` |
| 23 | 刪除再重建 GitHub tag 導致 Release 消失 | `git push origin :refs/tags/v*` 會連帶刪除關聯 Release；重推 tag 後須重新 create release |
| 24 | Repo rename 導致 POST API 靜默失敗 | Repo 改名後舊 URL 的 GET 自動 redirect，但 POST 回 307 且 `Invoke-RestMethod` 不跟隨 POST redirect，靜默回 401 Unauthorized。必須用新 repo name（如 `Dynamic-Alerting-Integrations`）或 repo ID URL（`/repositories/{id}/releases`） |
| 25 | Fine-grained PAT 權限不足建立 Release | Fine-grained PAT 預設沒有 Release 寫入權限；需在 token 設定加上 **Contents: Read and Write**。`Bearer` vs `token` prefix 皆可用於 GET，但 POST 需確認權限到位 |
| 26 | PAT 查 GHCR packages 回 403 | GitHub Packages API 需要 `packages:read` scope；PAT 沒此 scope 時 GET `/users/{owner}/packages` 回 403，但 **CI 用 `GITHUB_TOKEN` 有 `packages:write` 所以 push 成功**。驗證 image 是否存在最快的方式是瀏覽器開 `github.com/{owner}?tab=packages`，不繞 API |
| 27 | `.git/*.lock` 殘留阻擋 git 操作 | Cowork VM 無法刪除 `.git/index.lock` / `HEAD.lock`（`Operation not permitted`）；用 Windows MCP `Remove-Item "path\.git\*.lock" -Force` 清理。每次 git 操作異常中斷後必須先清 lock |
| 28 | `Invoke-RestMethod` 對 GitHub API 頻繁 timeout | Windows MCP PowerShell 的 `Invoke-RestMethod` 對 HTTPS API 極不穩定（模組初始化 + TLS 握手 → 常超過 60s timeout）。改用 `curl.exe` 替代：寫 JSON 到 temp 檔（`[IO.File]::WriteAllText` 無 BOM）→ `curl.exe --data-binary @file` |
| 29 | `mkdocs gh-deploy` site/ 權限錯誤 | MkDocs 建置產生 `site/` 後 Cowork VM 無法再次 `clean_directory`；部署前用 Windows MCP `Remove-Item site/ -Recurse -Force`。也可手動 push：temp repo → `gh-pages` branch → `git push --force` |
| 30 | `ghp_import` TypeError bytes vs str | Python 3.10 + 新版 ghp_import 的 `sys.stdout.write(enc(...))` 回傳 bytes 而非 str。Workaround：手動建 temp git repo、複製 `site/*`、push 到 `gh-pages` branch |
| 31 | Cowork VM proxy 封鎖 `api.github.com` | `git push` 走得通（git 協議通道），但 `requests` / `curl` 對 `api.github.com` 回 403 Forbidden（proxy 層封鎖）。GitHub API 操作必須透過 Windows MCP 的 `curl.exe` |
| 32 | `Set-Content` 預設加 BOM 導致 JSON parse 失敗 | GitHub API `curl.exe --data-binary @file` 讀入含 BOM 的 UTF-8 檔案會回 `Problems parsing JSON`。用 `[IO.File]::WriteAllText($path, $json, [Text.UTF8Encoding]::new($false))` 寫入無 BOM 版本 |

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
docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container bash -c "python3 ./scripts/tools/dx/bump_docs.py --check > /workspaces/vibe-k8s-lab/_ver.txt 2>&1"
```

## 相關資源

| 資源 | 相關性 |
|------|--------|
| ["GitHub Release — 操作手冊 (Playbook)"](github-release-playbook.md) | ⭐⭐ |
| ["測試注意事項 — 排錯手冊 (Testing Playbook)"](testing-playbook.md) | ⭐⭐ |
| ["Windows-MCP — Dev Container 操作手冊 (Playbook)"](windows-mcp-playbook.md) | ⭐⭐ |
