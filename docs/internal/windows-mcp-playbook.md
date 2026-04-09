---
title: "Windows-MCP — Dev Container 操作手冊 (Playbook)"
tags: [documentation]
audience: [all]
version: v2.6.0
verified-at-version: v2.6.0
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
| 18 | ~~GitHub Release `already_exists` 422~~ | 🗄️ 已歸檔（PATCH 繞道已固化為 Re-tag SOP）。詳見 [archive/lessons-learned.md](archive/lessons-learned.md) |
| 19 | Dev Container `Exited (255)` 未啟動 | `docker start vibe-dev-container`；每次 session 開始先 `docker ps` 確認 |
| 20 | Benchmark / Go test 複雜指令在 PowerShell 下失敗 | 寫 `.sh` 輔助腳本 → `docker exec [-d] bash script.sh`（見 [Benchmark Playbook → 在 Dev Container 內執行](benchmark-playbook.md#在-dev-container-內執行)）|
| 21 | Go test 從 repo root 執行失敗 | `go.mod` 在 `components/threshold-exporter/app/`，必須 `-w` 指定或 `cd` 進去 |
| 22 | `Get-Content -Raw` 是 PSObject 非純字串 | 放入 hashtable → `ConvertTo-Json` 會序列化 filesystem metadata；用 `[string]` cast 或改用 here-string `@"..."@` |
| 23 | 刪除再重建 GitHub tag 導致 Release 消失 | `git push origin :refs/tags/v*` 會連帶刪除關聯 Release；重推 tag 後須重新 create release |
| 24 | Repo rename 導致 POST API 靜默失敗 | Repo 改名後舊 URL 的 GET 自動 redirect，但 POST 回 307 且 `Invoke-RestMethod` 不跟隨 POST redirect，靜默回 401 Unauthorized。必須用新 repo name（如 `Dynamic-Alerting-Integrations`）或 repo ID URL（`/repositories/{id}/releases`） |
| 25 | Fine-grained PAT 權限不足建立 Release | Fine-grained PAT 預設沒有 Release 寫入權限；需在 token 設定加上 **Contents: Read and Write**。`Bearer` vs `token` prefix 皆可用於 GET，但 POST 需確認權限到位 |
| 26 | PAT 查 GHCR packages 回 403 | GitHub Packages API 需要 `packages:read` scope；PAT 沒此 scope 時 GET `/users/{owner}/packages` 回 403，但 **CI 用 `GITHUB_TOKEN` 有 `packages:write` 所以 push 成功**。驗證 image 是否存在最快的方式是瀏覽器開 `github.com/{owner}?tab=packages`，不繞 API |
| 27 | `.git/*.lock` 殘留阻擋 git 操作 | **首選**：`bash scripts/ops/git_check_lock.sh --clean`（診斷後安全清理）。VM 無法刪除時 fallback Windows MCP `Remove-Item "path\.git\*.lock" -Force`。詳見 [§ FUSE Phantom Lock 防治](#fuse-phantom-lock-防治) |
| 28 | `Invoke-RestMethod` 對 GitHub API 頻繁 timeout | Windows MCP PowerShell 的 `Invoke-RestMethod` 對 HTTPS API 極不穩定（模組初始化 + TLS 握手 → 常超過 60s timeout）。改用 `curl.exe` 替代：寫 JSON 到 temp 檔（`[IO.File]::WriteAllText` 無 BOM）→ `curl.exe --data-binary @file` |
| 29 | `mkdocs gh-deploy` site/ 權限錯誤 | MkDocs 建置產生 `site/` 後 Cowork VM 無法再次 `clean_directory`；部署前用 Windows MCP `Remove-Item site/ -Recurse -Force`。也可手動 push：temp repo → `gh-pages` branch → `git push --force` |
| 30 | `ghp_import` TypeError bytes vs str | Python 3.10 + 新版 ghp_import 的 `sys.stdout.write(enc(...))` 回傳 bytes 而非 str。Workaround：手動建 temp git repo、複製 `site/*`、push 到 `gh-pages` branch |
| 31 | Cowork VM proxy 封鎖 `api.github.com` | `git push` 走得通（git 協議通道），但 `requests` / `curl` 對 `api.github.com` 回 403 Forbidden（proxy 層封鎖）。GitHub API 操作必須透過 Windows MCP 的 `curl.exe` |
| 32 | `Set-Content` 預設加 BOM 導致 JSON parse 失敗 | GitHub API `curl.exe --data-binary @file` 讀入含 BOM 的 UTF-8 檔案會回 `Problems parsing JSON`。用 `[IO.File]::WriteAllText($path, $json, [Text.UTF8Encoding]::new($false))` 寫入無 BOM 版本 |
| 33 | MCP `start_process` 的 runtime ≠ 子行程真正執行時間 | `cmd.exe` 啟動 `git push` 後，MCP 可能在 ~1s 就 report「completed exit 0」，log 看起來被截在中間，但 git.exe 其實還在背景跑完。**不要信 MCP runtime**，一律用 side-effect 驗證：`git ls-remote origin HEAD` 比對遠端 SHA，或 `git fetch origin main` 看 refs 有沒有更新。詳見 [§修復層 C：Windows 原生 Git Fallback](#修復層-cwindows-原生-git-fallback) |
| 34 | Windows `cmd` batch 少了 `PATHEXT` 就找不到 `git.exe` | MCP 繼承到的 `PATHEXT` 可能沒包含 `.EXE`。所有 batch 起手必寫：`set "PATHEXT=.COM;.EXE;.BAT;.CMD;.VBS;.VBE;.JS;.JSE;.WSF;.WSH;.MSC;.PY;.PYW"` |
| 35 | cmd `(echo ... & echo ...)` parenthesized group 被 `%PATH%` 裡的 NVIDIA 閉括號拆掉 | `C:\Program Files (x86)\NVIDIA ...` 的 `)` 會提早結束 group，報 `此時候不應有 \NVIDIA`。**不要用 parenthesized group 包 echo**，改成獨立 `echo` 行 |
| 36 | pre-commit 產生的 `.git/hooks/pre-push` 硬寫死 Linux python 路徑 | `INSTALL_PYTHON=/usr/local/python/3.13.12/bin/python3` 在 Windows 不存在 → fallback 去找 `pre-commit` on PATH，但 Python 通常沒裝 console script shim。解法：把 hook 的第 6 行改成 `INSTALL_PYTHON=/c/Users/<USER>/AppData/Local/Python/bin/python.exe`（Git Bash 吃 POSIX 路徑），或 `pip install --force-reinstall pre-commit` 重建 entry point |
| 37 | `~/.ssh/` 無 private key 但 `credential.helper=manager` 有存 token | Windows 使用者常走 Git Credential Manager 不走 SSH。push 前臨時把 remote URL 切 HTTPS，讓 GCM 自動帶 stored token；push 完切回 SSH：`git remote set-url origin https://github.com/<o>/<r>.git; git push origin main; git remote set-url origin git@github.com:<o>/<r>.git` |
| 38 | pre-commit 範圍模式 `--from-ref A --to-ref B` 的觸發 glob 只看範圍內改動檔案 | 要避免 hook 掃到整個 repo 的累積 drift（例如 `bilingual-structure-check` 對整個 repo 的 `.en.md`），把 trigger glob 會命中的檔案從 commit 範圍內拿掉就夠。例：把 `docs/internal/doc-map.en.md` 以 `git rm --cached` 移出 commit，hook 就 Skipped |

## FUSE Phantom Lock 防治

FUSE 跨層掛載（Windows NTFS → VirtioFS → Cowork VM → Docker bind mount）是 `.git/*.lock` 殘留的根本原因。以下是分層防治措施（預防 → 偵測 → 修復 → 驗證）：

### 預防層：降低 Lock 發生機率

**1. VS Code Git 開關（專案級，不影響其他專案）**

```bash
# Agent session 開始時 — 關閉 VS Code 背景 Git
python scripts/ops/vscode_git_toggle.py off

# Session 結束或手動開發時 — 打開
python scripts/ops/vscode_git_toggle.py on

# 查看目前狀態
python scripts/ops/vscode_git_toggle.py
```

原理：VS Code 即時 hot-reload `.vscode/settings.json`，切換後立即生效。檔案已在 `.gitignore` 排除。

**⚠️ Agent 起手式**：每次 Cowork session 開始，**先跑 `vscode_git_toggle.py off`** 再做任何 git 操作。

**2. Git Config FUSE 調校（路徑條件式，只影響本 repo）**

安裝 `scripts/ops/gitconfig-fuse-tuning.sample`：

```bash
# Windows 端：
copy scripts\ops\gitconfig-fuse-tuning.sample %USERPROFILE%\gitconfig-fuse-tuning
```

然後在 `%USERPROFILE%\.gitconfig` 加入：

```ini
[includeIf "gitdir:C:/Users/<USERNAME>/vibe-k8s-lab/"]
    path = ~/gitconfig-fuse-tuning
```

> 將 `<USERNAME>` 替換為你的 Windows 使用者名稱。路徑用正斜線 `/`、結尾需有 `/`。

效果：`fsmonitor=false` + `trustctime=false` + `untrackedCache=false` + `filesRefLockTimeout=1500`，只在本 repo 生效。

**3. Windows 端降噪**

```powershell
# Defender 排除 .git/ 即時掃描（以系統管理員執行）
Add-MpPreference -ExclusionPath "C:\Users\<USERNAME>\vibe-k8s-lab\.git"
```

### 診斷層：遇到 Lock 時的安全處理

```bash
# 診斷（不刪除，只報告）
bash scripts/ops/git_check_lock.sh

# 診斷 + 清理（只清 >30s 且無活躍 git process 的 stale lock）
bash scripts/ops/git_check_lock.sh --clean
```

若 Cowork VM 無法刪除（`Operation not permitted`），腳本會輸出對應的 Windows MCP 指令。

### 跨平台 Line Ending

`.gitattributes` 確保 repo 內一律 LF，避免 CRLF/LF 混用在 FUSE 上造成額外的 diff 雜訊和 index 更新。

### 修復層 B：FUSE Cache 重建（Level 1 ~ 5）

當檔案殘影 / phantom lock 反覆出現、`rm` 過的檔案還看得到、或 git index 與磁碟內容對不上時，按以下層次逐步重建（輕 → 重）。優先跑 `make fuse-reset`，它會自動串 Level 1 + Level 3。

**Level 1 — Cowork VM 端 drop dentry/inode cache**

```bash
sync
echo 2 | sudo tee /proc/sys/vm/drop_caches   # 需要 sudo；Cowork VM 常沒給
```

只影響 VM 側的 kernel cache。無 sudo 時跳過，不影響後面層級。

**Level 2 — Cowork UI 把 workspace unmount 再重選**（**最實用**）

在 Cowork 桌面應用側邊欄把目前選取的資料夾取消，再重新選一次同樣的資料夾。這會讓 Cowork 重啟 FUSE driver 的 per-session state，等效於 FUSE userspace cache 冷啟動。9 成的殘影問題這一步就能解決。

**Level 3 — Windows 端把壓住 inode 的 process 清掉**

爛掉的 FUSE cache 多半是 Windows 上的 VS Code 或 Git for Windows 背景程序持續握著 file handle，讓 FUSE 以為檔案 busy → 快取無法驗證一致性。對應動作（`make fuse-reset` 自動跑 a/b/c）：

```powershell
# (a) 關 VS Code 背景 Git 掃描
python scripts/ops/vscode_git_toggle.py off

# (b) 清 stale .git/*.lock
bash scripts/ops/git_check_lock.sh --clean

# (c) 砍殘留的 port-forward / helm / kubectl / git process
Get-Process Code, git, pre-commit -ErrorAction SilentlyContinue | Stop-Process -Force
```

**Level 4 — 整個 Session 重啟（核彈選項）**

```bash
make session-cleanup
```

然後**關 Cowork 桌面應用**、重開、開新 session。這會重建 FUSE driver process 跟所有 kernel mount 狀態。

**Level 5 — 深層診斷（最後手段）**

用 Sysinternals `handle64.exe` 列出誰還握著 `vibe-k8s-lab/` 下的 file handle：

```powershell
# 下載 handle64.exe：https://learn.microsoft.com/sysinternals/downloads/handle
handle64.exe -accepteula -nobanner "vibe-k8s-lab"
# 找到 PID 後：
Stop-Process -Id <PID> -Force
```

若仍有殘影，跑 `chkdsk C: /scan`（唯讀掃描，不影響 FUSE）檢查底層 NTFS metadata 是否出錯。

> **驗證重建成功**：`ls -la .git/ | grep -E 'lock|index'`（應該無 `*.lock`）+ `git status -sb`（應該無「殘影檔案」）。

### 修復層 C：Windows 原生 Git Fallback（FUSE 側卡死時的備援路徑）

FUSE 側 git 操作反覆卡住、或 pre-commit hook 在 FUSE mount 上一直踩到 index lock 時，**Windows 原生 cmd/PowerShell 是第二條可走的路徑**。工作模式：

| 操作類型 | 走哪邊 | 原因 |
|---------|-------|------|
| 檔案 Read/Edit/Write | Claude 的檔案 tool（走 FUSE mount） | 雙向可見、原子寫入 |
| `git status` / `git add` / `git commit` / `git push` | Desktop Commander MCP → Windows 原生 `C:\Program Files\Git\cmd\git.exe` | git index lock 寫在 Windows NTFS，不走 FUSE metadata |
| pre-commit 執行 | Windows 原生 Python (`C:\Users\<USER>\AppData\Local\Python\bin\python.exe`) + `python -m pre_commit` | 避開 FUSE stat 延遲 |

兩端共用同一份工作樹，但 git 的檔案鎖、pre-commit 的 hook cache 都在 NTFS 上，不受 FUSE phantom lock 影響。

**Batch 起手式模板**（含所有必填 env，複製即用）：

```batch
@echo off
setlocal
set "PATH=C:\Users\<USER>\AppData\Local\Python\bin;C:\Program Files\Git\cmd;C:\Program Files\Git\usr\bin;C:\Windows\System32;C:\Windows;%PATH%"
set "PATHEXT=.COM;.EXE;.BAT;.CMD;.VBS;.VBE;.JS;.JSE;.WSF;.WSH;.MSC;.PY;.PYW"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "GIT_TERMINAL_PROMPT=0"
cd /d C:\Users\<USER>\vibe-k8s-lab

:: 所有輸出一律重導到檔案 — MCP stdout 擷取不可靠
git status -sb > C:\Users\<USER>\vibe-k8s-lab\_out.log 2>&1
```

然後用 Claude 的 `Read` tool 讀 `_out.log`。**不要**用 `(echo ... & echo ...)` parenthesized group 包 echo（會被 `%PATH%` 裡 NVIDIA 路徑的閉括號拆掉，見陷阱 #35）。

**PowerShell 模式**（需要 `$LASTEXITCODE` 或複雜物件處理時）：

```powershell
$git = 'C:\Program Files\Git\cmd\git.exe'
Start-Process -FilePath $git -ArgumentList 'ls-remote','origin','HEAD' `
  -NoNewWindow -Wait `
  -RedirectStandardOutput 'C:\Users\<USER>\vibe-k8s-lab\_lsr.txt' `
  -RedirectStandardError  'C:\Users\<USER>\vibe-k8s-lab\_lsr_err.txt'
Get-Content C:\Users\<USER>\vibe-k8s-lab\_lsr.txt
```

用 `Start-Process -NoNewWindow -Wait` 比 `& $git args 2>&1 | Tee-Object` 穩得多——後者在 MCP session 下常拿不到 `$LASTEXITCODE`。

**驗證 push 成功的唯一可靠方法**（陷阱 #33：MCP runtime 不可信）：

```powershell
# 跑完 git push 之後，不看 MCP 回報的 runtime/exit，改問遠端
Start-Process -FilePath $git -ArgumentList 'ls-remote','origin','HEAD' `
  -NoNewWindow -Wait -RedirectStandardOutput _lsr.txt
# 比對 _lsr.txt 裡的 SHA 是否 == 本地 HEAD
```

**Auth 路徑切換**：若 `~/.ssh/` 沒有 private key，但 `git config credential.helper` 是 `manager`（Git Credential Manager）、且 Windows Credential Manager 有存 `git:https://x-access-token@github.com` 這種憑證，則臨時切 HTTPS 讓 GCM 自動帶 token：

```batch
git remote set-url origin https://github.com/<owner>/<repo>.git
git push origin main
git remote set-url origin git@github.com:<owner>/<repo>.git
```

**pre-push hook 相容性**：pre-commit 產生的 `.git/hooks/pre-push` 會寫死 Linux python 路徑（陷阱 #36）。兩種修法擇一：

```bash
# 修法 A：把 hook 的 INSTALL_PYTHON 改成 Windows POSIX 路徑（Git Bash 吃這個格式）
INSTALL_PYTHON=/c/Users/<USER>/AppData/Local/Python/bin/python.exe

# 修法 B：把 pre-commit 裝成 console script shim
pip install --force-reinstall pre-commit
# 然後確保 Scripts 目錄在 PATH 上
```

> **什麼時候該走 Fallback C？** (1) `make git-preflight` + Level 2/3 都清過還是 lock；(2) pre-commit 在 FUSE mount 上跑得異常慢（> 10 倍平常）；(3) 檔案改了但 git 看不到 diff（FUSE metadata 不同步）。平常走 Cowork VM 的 bash/git 就好，Fallback C 是**應急路徑，不是常態**。

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
