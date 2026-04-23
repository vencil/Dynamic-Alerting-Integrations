---
title: "Windows-MCP — Dev Container 操作手冊 (Playbook)"
tags: [documentation]
audience: [all]
version: v2.7.0
verified-at-version: v2.8.0
lang: zh
---
# Windows-MCP — Dev Container 操作手冊 (Playbook)

> AI Agent 透過 Windows-MCP Shell / Desktop Commander / Cowork VM 操作 Dev Container 的最佳實踐與已知陷阱。
> **相關文件：** [Testing Playbook](testing-playbook.md)（K8s/測試排錯）· [Benchmark Playbook](benchmark-playbook.md)（方法論、踩坑）· [GitHub Release Playbook](github-release-playbook.md)（push + release 流程）

### Quick Action Index

> AI agent 直接跳到需要的操作步驟，跳過敘事。

| 我要做什麼 | 跳到 |
|-----------|------|
| docker exec 拿輸出 | [§核心原則](#核心原則docker-exec-stdout-為空) |
| port-forward Prometheus/AM | [§Port-Forward 模式](#port-forward-模式) |
| Helm upgrade 衝突 | [§Helm Upgrade 防衝突](#helm-upgrade-防衝突) |
| GitHub API (PS) | [§PowerShell REST API](#powershell-rest-apigithub-等) |
| git 卡住 / FUSE lock | [§Git 操作決策樹](#git-操作決策樹) |
| Windows 逃生門 | [§修復層 C](#修復層-cwindows-原生-git-fallbackfuse-側卡死時的備援路徑) |
| 寫 `.bat` / `.ps1` wrapper | [§MCP Shell Pitfalls](#mcp-shell-pitfalls編寫-bat-ps1-wrapper-時必讀) |
| 環境職責快查 | [§三層環境職責矩陣](#三層環境職責矩陣) |
| 已知陷阱查表 | [§已知陷阱速查](#已知陷阱速查) |

## 環境概覽

| 元件 | 位置 | 備註 |
|------|------|------|
| kubectl / kind / go / helm | Dev Container (`vibe-dev-container`) 內 | Cowork VM 無法直接使用 |
| Python tests | Cowork VM 可直接跑 | `python3 -m pytest tests/ -v` |
| Go build/test | Dev Container 內 | `-w ../components/threshold-exporter/app` |
| 純 Python 工具 (routing-bench 等) | 兩者皆可 | 不依賴 K8s 的優先用 Cowork VM |
| Mounted workspace | `/workspaces/vibe-k8s-lab` (container 內) | 雙向可見 |

## 核心原則：docker exec stdout 為空 🛡️

Windows MCP Shell 執行 `docker exec` 時，**stdout 被 PowerShell 吞掉**。這個陷阱已由 wrapper 自動化（v2.8.0 Plan B），**日常 session 不需記憶 redirect pattern**。

**主路徑（use this）**：

```bash
make dc-test                          # pytest in container
make dc-go-test                       # go test ./...
make dc-run CMD="kubectl get pods -A" # arbitrary command
make dc-status                        # is container running?
make dc-up                            # start container if stopped
```

或直接呼叫 wrapper：`scripts/ops/dx-run.sh <cmd>`（Linux）／`scripts\ops\dx-run.bat <cmd>`（Windows）。wrapper 會 `bash -c "<cmd> > /workspaces/...<file> 2>&1"`、讀回 exit code、把 stdout tee 回 host — 一次解掉 stdout-swallow + `-d` 模式的 redirect 遺漏。

**只有自己寫 one-off `docker exec` 時才需要原始 pattern**：

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

### v2.7.0 LL：`cmd.exe /c batfile` 執行時 PATH 與 PATHEXT 要同時 set

Cowork session 從 PowerShell 呼叫 `cmd.exe /c batfile.bat` 時，子程序**繼承一個最精簡的 Windows PATH**（有時連 `where.exe` 都找不到），且 `PATHEXT` 環境變數會被稀釋。這對 `gh.exe` 特別致命 — `gh` 需要呼叫 `git.exe` 做本地操作，`git.exe` 透過 PATH 查找但 lookup 被 `PATHEXT` 的成員決定。

**症狀**：`gh pr checks 26` 回報 `unable to find git executable in PATH; please install Git for Windows before retrying`，但 Git for Windows 其實裝好。

**根因**：
- 只 `set PATH=...` 不夠：新 PATH 有指向 `git.exe`，但 `PATHEXT` 被子 shell 稀釋（預設可能只剩 `.COM;.EXE`），Windows command resolution 還是漏
- 用 PowerShell 的 `$env:PATH` 在 `Start-Process` 下不會被子 cmd.exe 繼承
- inline `cmd /c "set PATH=...;command"` 也有同樣問題（cmd 會把 set 和 command 當同一行解析）

**正確做法**：寫成 `.bat` 檔，**同時** set `PATH` 和 `PATHEXT`：

```bat
@echo off
set "PATHEXT=.COM;.EXE;.BAT;.CMD"
set "PATH=C:\Windows\System32;C:\Windows;C:\Program Files\Git\cmd;C:\Program Files\Git\bin"
cd /d C:\Users\vencs\vibe-k8s-lab
"C:\Program Files\GitHub CLI\gh.exe" pr checks 26 > output.log 2>&1
```

**驗收**：`gh.exe` 會透過增強的 PATH 找到 `git.exe`，`gh pr checks` / `gh pr view` / `gh pr merge` 全部可用。

**不要做**：
- `cmd /c "set PATH=...&& gh pr checks 26"` — set 不會真的寫入環境
- 只信任 PowerShell 端的 `$env:PATH` — cmd 子 shell 不繼承
- 跳過 `PATHEXT` — Windows 仍會判 `git.exe` 找不到

**相關**：v2.7.0 PR [#26](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/26) Day 9 Session 3（2026-04-18）final-gate loop 驗證此 LL。詳細 session 記錄見 v2.7.0-planning.md §8.13（internal planning doc，`.gitignore` 排除）。

## 黃金法則：複雜指令寫成獨立腳本

只要指令含引號嵌套、管道、JSON 處理、多步邏輯，一律：
1. 用 Write tool 寫 `.sh` 或 `.py` 腳本到 mounted workspace
2. `docker exec bash /workspaces/vibe-k8s-lab/scripts/_task.sh`
3. 結果從重定向檔案讀取
4. 完成後清理暫存腳本

這比嘗試修復 `bash -c "..."` 引號問題更快更可靠。

**Windows 側例外**：若腳本是給 Windows MCP / Desktop Commander 呼叫的 `.bat` / `.ps1`，**不要寫到 `/tmp/` 或 sandbox-only 路徑**，而是放進 `scripts/ops/`（受 `check_ad_hoc_git_scripts` hook 把關）。臨時需求用既有 wrapper 的 `raw <args>` 逃生門；真的缺子命令就擴充 wrapper。詳見 [§MCP Shell Pitfalls](#mcp-shell-pitfalls編寫-bat-ps1-wrapper-時必讀)。

## MCP Shell Pitfalls（編寫 .bat / .ps1 wrapper 時必讀）

Windows MCP (Desktop Commander) 下 `.bat` / `.ps1` 會踩到 **4 個 encoding / parsing 雷**。**任何 session 擴充 `scripts/ops/win_*.bat` 前都必須照這份清單檢查**，否則新 subcommand 在 MCP session 看起來就是「隨機失敗」。

| 雷 | 症狀 | 對策 |
|----|------|------|
| 1. 含空格的路徑被 PowerShell 拆解 | `"C:\Program Files\GitHub CLI\gh.exe"` 變成 `'"C:\Program Files\...\gh.exe"'`，cmd 回 `不是內部或外部命令` | **一律用 8.3 short path**（`C:\PROGRA~1\GITHUB~1\gh.exe`、`C:\PROGRA~1\Git\cmd\git.exe`）+ 外層以 `shell: cmd.exe` 呼叫 `.bat` |
| 2. LF-only line endings → cmd 把 `REM` 當指令 | cmd.exe 報 `'REM' is not recognized`、`'echo' is not recognized` —— 逐行把每個 token 當 command 找 | `.bat` 檔**必須 CRLF**。Write tool 寫入後用 `Get-Content .\file.bat -Encoding Byte \| Select -First 4` 檢查是否有 `0D 0A`。若少了，用 Write tool 重寫（勿用 `sed -i`，見規則 #1） |
| 3. CJK 註解觸發 Desktop Commander encoding bug（**CI-gated ✅**） | `.bat` 內含中文會讓整個 batch parser 看到截斷指令（byte-level 根因見陷阱 #45） | `.bat` 內**全 ASCII + CRLF + no-BOM**（`REM`、English 註解、echo 字串）。CJK 解說只放在對應的 `.md` 旁註，不嵌進 code。**自動攔截**：pre-commit `bat-ascii-purity-check` (`scripts/tools/lint/check_bat_ascii_purity.py`) + pytest `tests/dx/test_bat_label_integrity.py::{test_bat_files_are_ascii_pure,test_bat_files_are_crlf,test_bat_files_have_no_utf8_bom}` |
| 4. cmd `-m "..."` 嵌 em-dash / 全形引號崩潰 | `git commit -m "feat: X — Y"` 每個 space 後的 word 變成 `pathspec` error（見陷阱 #46） | Commit message 一律走檔案（`commit-file` 子命令），`[IO.File]::WriteAllText($p, $m, [Text.UTF8Encoding]::new($false))` 寫無 BOM UTF-8 |

### Wrapper 起手式模板（複製即用）

```bat
@echo off
REM my_wrapper.bat -- <one line purpose>
REM Why this exists:
REM   <why not inline the command? which MCP pitfall does this wrapper avoid?>
REM Usage:
REM   my_wrapper.bat <subcommand> [args...]
REM
REM Rules (see docs/internal/windows-mcp-playbook.md#mcp-shell-pitfalls):
REM   * ASCII only (no CJK in comments or strings)
REM   * CRLF line endings
REM   * 8.3 short path for any exe under "Program Files"
REM   * Set both PATH and PATHEXT (cmd child shell strips them)

setlocal enabledelayedexpansion
set "PYTHONUTF8=1"
chcp 65001 >nul 2>&1
set "PATHEXT=.COM;.EXE;.BAT;.CMD"
set "PATH=C:\Program Files\Git\cmd;C:\Program Files\Git\bin;%PATH%"

REM --- dispatch: never write sibling _foo.bat; extend this wrapper ---
```

### 驗證步驟（新增 subcommand 後自測）

```powershell
# 1. Line ending check (must have 0D 0A)
Get-Content .\scripts\ops\my_wrapper.bat -Encoding Byte -TotalCount 200 | `
  Select-String -Pattern "13,10" -SimpleMatch -Quiet

# 2. ASCII check (fail if any byte >= 0x80)
$bytes = [IO.File]::ReadAllBytes(".\scripts\ops\my_wrapper.bat")
if ($bytes | Where-Object { $_ -ge 0x80 }) { Write-Error "Non-ASCII byte present" }

# 3. Functional smoke test via Desktop Commander
#    Call `cmd /c scripts\ops\my_wrapper.bat <subcmd>` from MCP; confirm no
#    "'REM' is not recognized" or encoding-mangled output.
```

`check_ad_hoc_git_scripts` hook 只把關「腳本放在對的目錄」；encoding / CRLF / BOM 在 PR #45 之後已由 `check_bat_ascii_purity.py` pre-commit hook + `test_bat_label_integrity.py` pytest 雙層攔截（scripts/ops/*.bat 限定，其他 .bat 不受管）。8.3 short path 與 em-dash 引號（雷 1、4）目前仍是人工紀律。違反這 4 條的 session 會一而再踩到同一個坑——這份清單是為了讓下一個 session「讀完 5 分鐘」就能避開 1 小時的 debug。

### MCP Caller Pattern（從 PowerShell 呼叫 `.bat` 的正確姿勢）

寫好了合規的 `.bat` 還有第二關：**怎麼從 MCP PowerShell session 把它叫起來**。PR #44 C5 close-loop 實測發現，「天真」的呼叫法（`& $bat pr-checks`、`Start-Process -Wait`）在 Windows-MCP 下會 **靜默 timeout**（60s RPC 上限），即使 `.bat` 自己 1 秒內就跑完。

根因：MCP 的 PowerShell 傳輸機制會 inherit 子 process 的 **console handle**。即使 `.bat` 早已 `exit /b 0`，只要 console handle 還在 MCP stdout pipe chain 上，RPC 就不會回 — 直到 60s timeout 才截斷。

**唯一穩定的呼叫模板**（三個非可選要素：`CreateNoWindow=$true` + `cmd.exe /s /c` + `WaitForExit(ms)`）：

```powershell
$bat  = "C:\Users\<you>\vibe-k8s-lab\scripts\ops\win_gh.bat"
$t    = "$env:TEMP\vibe-gh-out.txt"
Remove-Item $t -ErrorAction SilentlyContinue

# /s /c 的兩個旗標缺一不可：
#   /c  告訴 cmd.exe 執行後就退出
#   /s  讓 cmd.exe 只剝**一層**外部雙引號（否則嵌在 args 裡的內層引號會被亂剝）
$args = '/s /c "' + '"' + $bat + '" pr-checks > "' + $t + '" 2>&1"'

$psi = New-Object Diagnostics.ProcessStartInfo
$psi.FileName         = "cmd.exe"
$psi.Arguments        = $args
$psi.UseShellExecute  = $false
$psi.CreateNoWindow   = $true     # CRITICAL — 不加這行 MCP 還是會 inherit console handle 然後 hang
$psi.WorkingDirectory = "C:\Users\<you>\vibe-k8s-lab"
$p = [Diagnostics.Process]::Start($psi)
[void]$p.WaitForExit(30000)       # 給一個毫秒為單位的硬 timeout，避免萬一 hang
Get-Content $t -Raw
```

**為什麼三個要素都不能省：**

| 要素 | 省略的症狀 | 作用 |
|------|-----------|------|
| `CreateNoWindow = $true` | MCP RPC 卡 60s 後截斷（`.bat` 明明瞬間跑完） | 阻止 child 建立 console → 斷開 MCP 對 console handle 的 inheritance |
| `cmd.exe /s /c "..."` 的 `/s` | 內層引號被意外剝掉 → `.bat` 收到半截 args / 路徑空字串 | 告訴 cmd 只拆掉最外層的一對引號 — 剛好對應上面 `$args` 組裝的雙包結構 |
| `$p.WaitForExit(ms)` | `.bat` 還在跑 PowerShell 就往下跑 → `Get-Content` 讀到空檔 | 用毫秒 timeout 的同步等待取代「傳 pipe 去等 stdout」；MCP 拿到的是 process handle，不是 open pipe |

> **實測基準**（PR #44 C5 dogfood）：
> - 錯誤寫法（`& $bat`）：`waited=False, exit=None, MCP timeout @ 60s`
> - 缺 `CreateNoWindow`：`waited=False, exit=None, MCP timeout @ 60s`
> - 缺 `/s` 用 `/c` only：`waited=True, exit=0, len=0`（cmd 把 `.bat` 路徑的外層引號剝掉之後、又把內層引號當內容）
> - 三個都加：`waited=True, exit=0, len=39`（正常）

`scripts/ops/win_git_escape.bat` 和 `scripts/ops/win_gh.bat` 的檔頭都嵌入了這段模板作為 in-tree 單一來源，並由 `tests/dx/test_bat_label_integrity.py::test_mcp_caller_pattern_documented` 強制要求 header 包含 `Process.Start` / `WaitForExit` / `CreateNoWindow` / `/s /c` 四個關鍵字 — 任何未來改動都會擋 CI。

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

**JSON body 統一做法（ConvertTo-Json + UTF8 Bytes）：**

```powershell
# ✅ 唯一推薦做法 — 同時支援 ASCII 和 CJK
$payload = @{ tag_name = "v1.9.0"; name = "title"; body = $longText } | ConvertTo-Json -Depth 3
Invoke-RestMethod -Uri $url -Method Post -Headers $headers `
    -Body ([System.Text.Encoding]::UTF8.GetBytes($payload)) `
    -ContentType "application/json; charset=utf-8"
# ⚠️ 必須用 UTF8.GetBytes()，否則 CJK 字元亂碼

# ❌ 單行 JSON 字串 — 不支援 CJK，容易手滑引號配對，不推薦
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
| 14 | PS JSON body CJK 亂碼 | 統一用 `ConvertTo-Json` + `UTF8.GetBytes()` + `charset=utf-8`（見 [§PowerShell REST API](#powershell-rest-apigithub-等)） |
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
| 25 | Fine-grained PAT 權限不足建立 Release | 詳見 [GitHub Release Playbook §PAT 權限](github-release-playbook.md)。摘要：需 **Contents: Read and Write** scope |
| 26 | PAT 查 GHCR packages 回 403 | 需 `packages:read` scope；驗證 image 最快用瀏覽器開 `github.com/{owner}?tab=packages` |
| 27 | `.git/*.lock` 殘留阻擋 git 操作 | **首選**：`bash scripts/session-guards/git_check_lock.sh --clean`（診斷後安全清理）。VM 無法刪除時 fallback Windows MCP `Remove-Item "path\.git\*.lock" -Force`。若連 Windows MCP 也沒有（純 Cowork sandbox + phantom dentry），見 [§修復層 B Level 6 rename-trick](#修復層-bfuse-cache-重建level-1-5)。詳細背景：[§ FUSE Phantom Lock 防治](#fuse-phantom-lock-防治) |
| 28 | `Invoke-RestMethod` 對 GitHub API 頻繁 timeout | Windows MCP PowerShell 的 `Invoke-RestMethod` 對 HTTPS API 極不穩定（模組初始化 + TLS 握手 → 常超過 60s timeout）。改用 `curl.exe` 替代：寫 JSON 到 temp 檔（`[IO.File]::WriteAllText` 無 BOM）→ `curl.exe --data-binary @file` |
| 29 | `mkdocs gh-deploy` site/ 權限錯誤 | MkDocs 建置產生 `site/` 後 Cowork VM 無法再次 `clean_directory`；部署前用 Windows MCP `Remove-Item site/ -Recurse -Force`。也可手動 push：temp repo → `gh-pages` branch → `git push --force` |
| 30 | `ghp_import` TypeError bytes vs str | Python 3.10 + 新版 ghp_import 的 `sys.stdout.write(enc(...))` 回傳 bytes 而非 str。Workaround：手動建 temp git repo、複製 `site/*`、push 到 `gh-pages` branch |
| 31 | Cowork VM proxy 封鎖 `api.github.com` | `git push` 走得通（git 協議通道），但 `requests` / `curl` 對 `api.github.com` 回 403 Forbidden（proxy 層封鎖）。GitHub API 操作必須透過 Windows MCP 的 `curl.exe` |
| 32 | `Set-Content` 預設加 BOM 導致 JSON parse 失敗 | GitHub API `curl.exe --data-binary @file` 讀入含 BOM 的 UTF-8 檔案會回 `Problems parsing JSON`。用 `[IO.File]::WriteAllText($path, $json, [Text.UTF8Encoding]::new($false))` 寫入無 BOM 版本 |
| 33 | MCP `start_process` 的 runtime ≠ 子行程真正執行時間 | `cmd.exe` 啟動 `git push` 後，MCP 可能在 ~1s 就 report「completed exit 0」，log 看起來被截在中間，但 git.exe 其實還在背景跑完。**不要信 MCP runtime**，一律用 side-effect 驗證：`git ls-remote origin HEAD` 比對遠端 SHA，或 `git fetch origin main` 看 refs 有沒有更新。詳見 [§修復層 C：Windows 原生 Git Fallback](#修復層-cwindows-原生-git-fallbackfuse-側卡死時的備援路徑) |
| 34 | Windows `cmd` batch 少了 `PATHEXT` 就找不到 `git.exe` | MCP 繼承到的 `PATHEXT` 可能沒包含 `.EXE`。所有 batch 起手必寫：`set "PATHEXT=.COM;.EXE;.BAT;.CMD;.VBS;.VBE;.JS;.JSE;.WSF;.WSH;.MSC;.PY;.PYW"` |
| 35 | cmd `(echo ... & echo ...)` parenthesized group 被 `%PATH%` 裡的 NVIDIA 閉括號拆掉 | `C:\Program Files (x86)\NVIDIA ...` 的 `)` 會提早結束 group，報 `此時候不應有 \NVIDIA`。**不要用 parenthesized group 包 echo**，改成獨立 `echo` 行 |
| 36 | pre-commit 產生的 `.git/hooks/pre-push` 硬寫死 Linux python 路徑 | `INSTALL_PYTHON=/usr/local/python/3.13.12/bin/python3` 在 Windows 不存在 → fallback 去找 `pre-commit` on PATH，但 Python 通常沒裝 console script shim。解法：把 hook 的第 6 行改成 `INSTALL_PYTHON=/c/Users/<USER>/AppData/Local/Python/bin/python.exe`（Git Bash 吃 POSIX 路徑），或 `pip install --force-reinstall pre-commit` 重建 entry point |
| 37 | `~/.ssh/` 無 private key 但 `credential.helper=manager` 有存 token | Windows 使用者常走 Git Credential Manager 不走 SSH。push 前臨時把 remote URL 切 HTTPS，讓 GCM 自動帶 stored token；push 完切回 SSH：`git remote set-url origin https://github.com/<o>/<r>.git; git push origin main; git remote set-url origin git@github.com:<o>/<r>.git` |
| 38 | pre-commit 範圍模式 `--from-ref A --to-ref B` 的觸發 glob 只看範圍內改動檔案 | 要避免 hook 掃到整個 repo 的累積 drift（例如 `bilingual-structure-check` 對整個 repo 的 `.en.md`），把 trigger glob 會命中的檔案從 commit 範圍內拿掉就夠。例：把 `docs/internal/doc-map.en.md` 以 `git rm --cached` 移出 commit，hook 就 Skipped |
| 39 | Windows clone 的 `rule-packs/` 和 `docs/CHANGELOG.md` 變成 ~13 byte 純文字檔 | Git 物化 symlink 為 target 字串。非 bug，是權限問題——Windows 10+ 預設不允許非 admin 建立 symlink。**解法**：開啟 Developer Mode（見 [§Windows Clone 初次設定](#windows-clone-初次設定-symlink-支援)）|
| 40 | Markdown heading 用 em-dash `—` 時，Python Markdown / MkDocs slugify 產出**單 hyphen** 而非雙 hyphen | 例：`## Windows Clone 初次設定 — Symlink 支援` → slug 是 `windows-clone-初次設定-symlink-支援`（不是 `--symlink-支援`）。em-dash 被當作 space 處理，兩側 space 合併成一個 hyphen。PR #18 因此打到 broken anchor CI fail。**檢測**：本地跑 `python scripts/tools/lint/check_doc_links.py --ci`。**修法**：link 裡的 `--` 改成 `-`，或 heading 改成 ASCII hyphen `-`（會 slugify 成 `--`，但可讀性差） |
| 41 | `git rebase -i --autosquash` 在 MCP 下無法開編輯器 | 想非互動地跑 `git commit --fixup=<sha> && git rebase -i --autosquash <base>`，但 `GIT_SEQUENCE_EDITOR=rem` 或 `=cmd /c rem` 會被 Git for Windows 的 bundled sh 當成 shell command 解讀，報 `rem: command not found`。**正解**：用 `true`（msys 的內建 no-op），並透過 `-c` 臨時設定避免污染 env：`git -c sequence.editor=true -c core.editor=true rebase -i --autosquash <base>`。autosquash 會在 sequence file 寫入後立即以 `true` 結束編輯器，保留預設順序。驗證：`git log --oneline` 看到 `fixup!` 已被摺進 target commit |
| 42 | pre-commit hook CRLF shebang + dual shebang 雙重問題 | Windows 端 `pre-commit install` 產生的 `.git/hooks/pre-commit` 有兩個問題：(1) CRLF 行尾導致 Linux/FUSE 找不到 `#!/bin/sh\r`（報 `cannot run .git/hooks/pre-commit: No such file or directory`）；(2) 修完 CRLF 後，`#!/bin/sh` 無法解析 bash array `ARGS=(...)`（報 `Syntax error: "(" unexpected`）。**修法**：`tr -d '\r' < hook > hook.tmp && mv hook.tmp hook && chmod +x hook`，再把 `#!/bin/sh` 改成 `#!/usr/bin/env bash`。已內建到 `win_git_escape.bat fix-hooks` 子命令 |
| 43 | pre-commit stash + FUSE 交互形成死鎖 | pre-commit 偵測到 unstaged changes → `git stash` → stash 操作在 FUSE 上衝突 → 嘗試 `git checkout -- .` → 建立 `index.lock` → FUSE phantom lock → 整個 git 卡死。**防治**：(1) commit 前先 `git stash` 手動處理 unstaged changes，不要讓 pre-commit 自動 stash；(2) 大量 unstaged files 時改用 Windows 逃生門 commit；(3) 已發生時用 `make git-lock ARGS="--clean"` 或 Windows MCP `Remove-Item` 清鎖 |
| 44 | Phantom lock 薛丁格態：ls 顯示存在但所有操作都失敗 | FUSE dentry cache 殘留的 `.git/index.lock`，`ls` 同時報 "No such file" 卻又列出檔案。`os.unlink` 報 "Operation not permitted"，`os.rename` 報 "No such file"。Level 1 `drop_caches` 和 Level 6 rename-trick 皆無效。**唯一可靠解法**：放棄從 FUSE 側操作，切換到 Windows 原生 git（`win_git_escape.bat`）完成所有 git 操作。這是「逃生門」設計存在的核心理由 |
| 45 | Desktop Commander `start_process` 執行 `.bat` 檔案時編碼損壞 | Desktop Commander 的 `start_process` 直接執行 `.bat` 會對 `@echo off`、`setlocal`、`goto` 等**下游**關鍵字產生亂碼，batch 無法正確解析（症狀：CJK 那行 OK，但「之後幾行」才出現 `'@echo' 不是內部或外部命令`）。**Byte-level 根因**（v2.8.0 PR #45 定位）：`start_process` 啟動的子 `cmd.exe` **繼承父行程 OEM codepage**（zh-TW 是 cp950，en-US 是 cp437），**不是 cp65001**；cmd batch parser 是**逐 byte 讀檔**、**不做 UTF-8 normalization**，當 `.bat` 以 UTF-8 儲存且含 CJK 或 em-dash，任何 byte ≥ 0x80 都可能落在 parser 視為 shell metachar 的範圍（0x80–0xBF 包含 cp1252 多個標點 continuation byte），parser 內部狀態機被破壞，**後續幾行**指令才開始被誤判。**為何 `cmd /c` 不救**：子 cmd 仍繼承父 codepage，byte-level collision 不變。**為何 `chcp 65001` 不救**：chcp 要 parser 執行到那行才生效，preamble (`@echo off` / `setlocal`) 已用錯誤 codepage 讀完。**為何 PowerShell 呼 .bat 能過**：PS runtime 先把 command line decode 成 UTF-16 再交給 cmd，byte collision 發生在更上層。**三條鐵律**（scripts/ops/*.bat only）：(a) 全 ASCII（byte < 0x80），CJK 只放在 `.md`；(b) CRLF 行尾（pitfall #2）；(c) 不得有 UTF-8 BOM（`EF BB BF` 破壞第一道指令）。**CI gate ✅**：`tests/dx/test_bat_label_integrity.py::{test_bat_files_are_ascii_pure,test_bat_files_are_crlf,test_bat_files_have_no_utf8_bom}` + pre-commit `scripts/tools/lint/check_bat_ascii_purity.py`（L1 本地攔 commit，pytest CI 捕逃逸）。`win_git_escape.bat` / `win_gh.bat` / `dx-run.bat` 已在 `e55d9af` + PR #45 改為全 ASCII 🛡️ |
| 46 | cmd `git commit -m` 無法處理 UTF-8 特殊字元（em-dash、CJK） | `git commit -m "feat(ops): playbook audit — harness"` 中的 em-dash（U+2014）不在 cmd codepage 內，導致引號解析崩潰，每個空格後的單字都被當成獨立 pathspec，產生大量 `fatal: pathspec 'xxx' did not match any file` 錯誤。**正解**：永遠用 `git commit -F file.txt` 檔案傳遞 commit message。已內建到 `win_git_escape.bat commit-file` 子命令。UTF-8 檔案用 `[IO.File]::WriteAllText($path, $msg, [Text.UTF8Encoding]::new($false))` 或 `echo msg > file` 產生 |
| 47 | Windows MCP PowerShell 對大型 working tree 的 git 操作 timeout | 當 working tree 有 ~90+ unstaged files 時，透過 Windows MCP PowerShell 執行 `git add` 和 `git status` 會反覆超過 60s timeout（連續 3 次失敗）。原因是 Git 需要 stat 大量檔案 + PowerShell MCP 模組初始化開銷。**繞道**：(1) 改用 Desktop Commander 的 cmd shell（`cmd /c "git add file1 file2"`）；(2) 用 `win_git_escape.bat` 直接操作；(3) 對 fire-and-forget 需求用 `scripts/ops/win_async_exec.ps1` 派工取 PID，sandbox 側輪詢 log，見 [§修復層 C.1 Escape Helpers](#修復層-c1escape-helpersmcp-60s-timeout-fuse-cache-bypass) |
| 48 | Desktop Commander cmd shell 拆解 `--title` 引號 | `gh pr create --title "multi word title"` 在 cmd 內被拆成獨立 arguments。**正解**：把完整命令寫入 `.bat` 檔再執行（bat 內引號正常解析）。PowerShell 可正確處理引號，但有 PATH (#49) 和 timeout (#47) 問題 |
| 49 | `gh` 不在 Desktop Commander PowerShell PATH | Desktop Commander 的 PowerShell shell 找不到 `C:\Program Files\GitHub CLI\gh.exe`，但 cmd 可以。原因：PowerShell MCP 的 PATH 繼承與 cmd 不同。**正解**：用 cmd shell + bat 檔；或在 PowerShell 用全路徑 `& "C:\Program Files\GitHub CLI\gh.exe"` |
| 50 | `gh pr checks --json` 沒有 `conclusion` 欄位 | 可用欄位：`name, state, bucket, description, event, link, startedAt, completedAt, workflow`。`bucket` 值為 `pass/fail/pending/skipping`。很多網路範例用 `conclusion` 是錯的 |
| 51 | Windows cmd console (cp950) 印 emoji 會 UnicodeEncodeError | Python `print()` 在 Windows cmd 預設用 cp950 encoding，遇到 ✅⚠️❌ 等 emoji 直接 crash。**正解**：script 開頭偵測 `cp*` encoding 時強制 `sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')` |
| 52 | Bash 工具傳 Windows 絕對路徑（`C:\...`）產生 FUSE phantom 檔案 | 當 Bash/Shell 工具接收到 `C:\Users\...\_bench.bat` 這類 Windows 絕對路徑作為**位置參數**，FUSE 層會把 `:` 翻成 U+F03A、`\` 翻成 U+F05C（PUA 區碼位），在 Linux 側建出路徑合法但在 Windows 側看到的是 `CUsersvencsvibe-k8s-lab_bench.bat` 這種「中間夾隱形字元」的殘檔。殘檔會被 `git status` 當 untracked 列出但 wildcard（如 `*vibe-k8s-lab*`）匹配不到，須用 regex 比對 `_bench_f1b\|_bench_poll\|_poll\.bat` 等片段。**正解**：(1) 任何跨 Windows 路徑的寫入操作用 Write 工具或 Windows MCP PowerShell，不要塞給 Bash 工具；(2) `.gitignore` 已加 `CUsersvencs*` + `/C:\*` 雙重防守（PR #v2.7.1-doc-hygiene）；(3) 清理用 Windows 側 `Remove-Item -LiteralPath` + regex match，非從 FUSE 側 `rm`（會 `Operation not permitted`） |
| 53 | `win_async_exec.ps1` 派 `gh pr create --title "...(v2.9.0+)"` 時 title 尾段被 cmd /c 吞掉（misleading 成功誤判） | `win_async_exec.ps1` 內部走 `cmd.exe /c "<Command> > log 2>&1"`，cmd 的 parenthesis / operator parsing 會把 nested double-quote 中的 `(` / `+` 當成 grouping char / concat operator，導致 `--title` 參數尾段在 cmd 層被吞掉。**典型症狀**：log 只印出 `Creating PR: docs(governance): ... SSOT +` 後就截斷，PR 實際**建立成功**但 title 缺尾綴，operator 誤判失敗去重試，第二次才撞上 "a pull request already exists"，繞一大圈才察覺。**正解（§黃金法則的具體應用）**：把整段命令寫成獨立 `.ps1`，title 用 single-quoted variable 宣告後 `& gh ... --title $title --body-file _pr_body.md`，再以 `win_async_exec.ps1 -Command 'powershell -File _pr_make.ps1'` 派工，cmd /c 就只看到一個外層命令字串，不會吃到內層 `(` / `+`。**差異於 #48**：#48 是 Desktop Commander cmd 把 `--title "x y z"` 拆成空格切開的多參數；#53 是 win_async_exec 的 cmd /c 把完整引號內字元因 `(` / `+` 提早截斷。兩者配方不同，前者用 .bat 包裝，後者用 .ps1 包裝 |
| 54 | Ad-hoc `_commit.ps1` / `_pr.bat` script proliferation — 每個 session 重寫一次 | PR #39 寫了 `_p39_commit.ps1`；PR #40 寫了 `_p40_commit.ps1`、`_p40_pr.bat`、`_p40_checks.bat`、`_p40_failog.bat`、`_p40_diag.bat`（五隻！）—— 全部 reinvent 既有 `scripts/ops/win_git_escape.bat` 的功能。**根因**：session agent 沒讀 playbook 就動手，每次撞到 FUSE/MCP 問題就寫 throw-away script，下次 session 看不到（被 `.gitignore _*.bat` 藏起來）又重寫一次。**長期解法**（v2.8.0, PR #41）：(1) `scripts/tools/lint/check_ad_hoc_git_scripts.py` 以 whitelist 模式阻擋 `scripts/ops/` / `scripts/tools/` / `tools/` 外的 `*.bat` / `*.ps1` / `*.cmd`；(2) `.gitignore` 不再藏 scratch script（adopt-or-delete 政策）；(3) 新 subcommand 直接擴充 `win_git_escape.bat` / `win_gh.bat`。**下次 session 要寫 `_foo.bat` 前**：先 `scripts/ops/win_gh.bat raw gh ...` 或 `scripts/ops/win_git_escape.bat raw git ...`；真的需要新 subcommand 就擴充 wrapper（whitelist hook 會強制如此）🛡️ |
| 55 | `.bat` wrapper 三要素（Short path + CRLF + ASCII）沒有全備 | PR #41 新增 `win_gh.bat` 初次執行時三次失敗：(1) 行尾 LF → cmd 報 `'REM' is not recognized`（每個 token 當 command 找）；(2) 忘 `set PATH=...Git\cmd...` → gh 報 `unable to find git executable in PATH`；(3) `"C:\Program Files\GitHub CLI\gh.exe"` 在 PowerShell 下被多層 quote 破壞，不管怎麼逃脫都失敗。**三個對策一次到位**：(a) 8.3 short path `C:\PROGRA~1\GITHUB~1\gh.exe`（避免任何 quote 問題）；(b) wrapper 開頭強制 `set "PATH=C:\Program Files\Git\cmd;C:\Program Files\Git\bin;%PATH%"`；(c) 檔案以 CRLF 儲存、全 ASCII（驗證：`Get-Content -Encoding Byte -TotalCount 200` 看到 `0D 0A`）。詳見 [§MCP Shell Pitfalls](#mcp-shell-pitfalls編寫-bat-ps1-wrapper-時必讀) |
| 56 | Squash-merge base PR 造成下游 stacked PR 進入 `mergeStateStatus: DIRTY` → GH 靜默跳過 `pull_request` CI（零 workflow 觸發） | PR #41 堆在 PR #40 分支上推開；PR #40 以 **squash** merge 到 main，PR #40 原 commits (`f5ccb7d`, `84e6ab5`) 在 PR #41 分支還在，跟 main 的 squashed 版本 (`23c189c`) 在 GH server 比對時算「重複但不同 hash」→ 無法自動合成 merge-ref → `mergeStateStatus=DIRTY`。**關鍵副作用**：`on: pull_request` 的 workflow **完全不觸發**（`gh run list --branch <br>` 空，`gh pr checks` 回 `no checks reported`），很容易被誤判成「GH Actions 壞了」或「path filter 過濾掉」。**正解**：(1) 在 Windows 側 `git rebase origin/main`（squashed commits 會自動丟掉，重複 diff 被 cherry-pick 去重）→ `git push --force-with-lease`；(2) 若 squash diff 不完全對得上，手動 `git rebase -i origin/main` drop 掉重複 commits。**常伴陷阱**：同時確認 wrapper 的 `PATHEXT` 有設（#34）—— PR #41 首次 dogfood 時雖然 playbook template 寫了 `set PATHEXT=...` 但 `win_gh.bat` / `win_git_escape.bat` 實際程式碼忘設，撞到使用者 profile 的 `PATHEXT=.CPL` 直接讓 gh 回 `unable to find git executable in PATH`（雖然 PATH 有 Git\cmd）。Template ↔ actual code 之間會 drift，wrapper 起手式固定六行（`setlocal` / `PYTHONUTF8` / `chcp 65001` / `PATHEXT` / `PATH` / `GH_CMD` 或 `GIT_CMD`）缺一不可 🛡️ |
| 57 | FUSE 側 `pre-commit` 跑 `head-blob-hygiene` hook 長時間 0-output 疑似卡死 | `check_head_blob_hygiene.py` 對 HEAD 全部 ~850 個 blob 跑 `git cat-file --batch` + NUL / EOF / YAML 截斷 heuristic。CI 側 ~6 秒，FUSE 側可以**完全卡住 17+ 分鐘 0 output**（PR #46 / PR #47 親踩兩次）。**根因候選**：`git count-objects -v` 同時回報 `warning: garbage found: .git/objects/pack/tmp_pack_*`、`.git/objects/XX/tmp_obj_*` 多筆 FUSE stale temp → `git cat-file` 在某些 pack 上卡住。**診斷**：(a) `tail` pre-commit log 看最後一個成功的 hook 名（確認真卡在 head-blob-hygiene 而非別的）；(b) 觀察 `pre-commit.exe` 程序記憶體 <10 MB 且 10+ 分鐘無進度。**短期處置**：kill `pre-commit.exe` + `git commit --no-verify`，在 commit body 明寫 bypass 理由 + 列出已手動 spot-check 通過的關鍵 hook（例：`pre-commit run file-hygiene --all-files` / `pytest` / `pre-commit run check-techdebt-drift --hook-stage pre-push --all-files`）。**中期**：清 stale temp —— Windows 側 `git fsck --no-reflog` + 手動 `Remove-Item .git/objects/pack/tmp_*` + `.git/objects/*/tmp_*`。**長期**：hook 自身加 progress output（每 100 blobs 印一行）讓「卡住」vs「慢」可區分 🛡️ |
| 58 | `make git-preflight` 把自身 bash 程序誤判為「活躍 git 程序」跳過清理 | preflight helper `scripts/session-guards/git_check_lock.sh` 用 `pgrep git` 偵測 active git 程序決定要不要清 `.git/*.lock`，卻把 Makefile 本身啟動的 bash subshell（其 argv 含 `git` 字串的 path）當成活 git，於是**永遠跳過清理**。**表現**：`make git-preflight` 回報「lock exists but git active → skip」但實際沒有 git 在跑，lock 永久存在。**修法**：過濾自身 PID + parent PID：`pgrep git \| grep -v -E "^($$\|$PPID)$"`；或改偵測 `.git/index.lock` 的 mtime（> 60s 無進度視為 stale）。歸檔於 `v2.8.0-planning.md` §12.4 #2，排入 A-12 子項 (v) 施工週。**手動繞道**：直接 `rm -f .git/*.lock` 或 Windows 側 `Remove-Item .git\*.lock -Force` |
| 59 | `.git/HEAD` 被 null byte 填充至 57 bytes（正常 45）→ `git rev-parse HEAD` fatal | FUSE 寫 cache 在 context compaction 被 drop 時，部分檔案沒 flush 完整，`.git/HEAD` 尾巴殘 NUL bytes。正常內容 `ref: refs/heads/<branch>\n` 約 40-50 bytes；若檔案 ≥ 55 bytes 且尾端 hexdump 全是 `00 00 00`，基本是 FUSE cache loss（見 trap #9）。**診斷**：`wc -c .git/HEAD` + `hexdump -C .git/HEAD \| tail`。**修法**（不需 full fuse-reset）：`printf 'ref: refs/heads/<branch>\n' > .git/HEAD`（若在 FUSE 側失敗則走 Windows 側 `[IO.File]::WriteAllText("C:\...\.git\HEAD", "ref: refs/heads/<branch>`n", [Text.UTF8Encoding]::new($false))`）。**長期**：`scripts/ops/git_check_lock.sh` 加 HEAD 長度 + 首行格式 sanity check，異常即 report + auto-repair。歸檔於 §12.4 #4，排入 A-12 子項 (v) |
| 60 | `generate_doc_map.py` / 類似 regen 工具執行途中遭 FUSE fsync 中斷 → HEAD corruption + 全檔假 "new file" | 長 I/O regen（scan 所有 .md、寫 doc-map.md / tool-map.md）在 FUSE 上被 context-compaction 的 cache drop 攔截，造成輸出檔半寫半空 + `git status` 誤報整份 repo 的已追蹤檔成 new file（index metadata 亂掉）。**表現**：`git status` 印數百行 "new file: docs/..."，實際內容未動。**修法 short-term**：`git reset HEAD -- .` 或 `git update-index --refresh`；嚴重時 `make recover-index`（v2.8.0 PR #44 plumbing 逃生門）。**修法 long-term**：regen 工具加 `--safe` mode — 寫到 `path.tmp` → `os.replace(path.tmp, path)` atomic rename（rename(2) 在同 FS 是 atomic，避免半寫檔案短暫存在）。歸檔於 §12.4 #5，候選 A-9 子項或獨立 tiny PR |
| 61 | PowerShell `Out-File -Encoding utf8` / `Set-Content -Encoding utf8` 寫 commit message 檔案預設加 U+FEFF BOM → commitlint header-trim / subject-empty / type-empty 三層 fail | PS 5.1 的 `utf8` encoding **強制帶 BOM**（`EF BB BF`）；`git commit -F file.txt` 把 BOM 當 subject 的第一個字元，commitlint 解析時 subject 首字非 ASCII alnum → 連 `type-empty` 都 fail（因為 regex `^<type>(<scope>)?: <subject>` 的 `<type>` 吃不到字母）。區別於 #32（JSON parse fail）：這個是 commit message body 被污染。**正解**：一律 `[IO.File]::WriteAllText($path, $msg, [Text.UTF8Encoding]::new($false))`（explicit no-BOM constructor）；或 Bash 側 `printf '%s\n' "$msg" > file.txt`。**Recovery SOP**：已 push 的歷史 commit 用 `git filter-branch --msg-filter "sed '1s/^\xEF\xBB\xBF//'" <range>` 批次去 BOM，然後 `push --force-with-lease`。歸檔於 §12.4 #8，A-13 candidate hook `validate_commit_msg_encoding.py`（偵測 commit message file 首 byte ≠ `0xEF`） |
| 62 | Dev Container 只掛 **主 worktree**，claude worktree 的 Edit 不會進 container — 測試看到舊 source | `scripts/ops/dx-run.sh` 用 `docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container` 進 container，mount 的是 `C:\Users\vencs\vibe-k8s-lab\`（主 worktree），不是 `C:\Users\vencs\vibe-k8s-lab\.claude\worktrees\<name>\`。若在 claude worktree 用 Edit 修 `components/.../xxx_test.go` 後跑 `make dc-go-test` 或 `bash scripts/ops/dx-run.sh go test ...`，**container 看到的是主 worktree 舊檔**，測試結果跟剛改的 code 無關。**症狀**：edit 後 re-run test，錯誤訊息的 line number 對不到已改過的行；`diff claude-worktree-file main-worktree-file` 發現差異。**解法三選一**：(a) **臨時同步**（推薦 for 單 test 驗證）— `cp claude-worktree/path main-worktree/path` → 跑 test → `cd /c/Users/vencs/vibe-k8s-lab && git checkout -- path` revert，claude worktree 保留為 SoT；(b) **分支同步** — 先 commit+push 到 remote branch，主 worktree `git fetch` + apply patch；(c) 改寫 `dx-run.sh` 或 Makefile 吃 `WORKTREE=` 參數把 `-w` 指向任意 worktree（需 container 端 bind mount 覆蓋，複雜度較高）。Session #19 首次踩到，A-10 flake 再量測時才發現 edit 未同步 🛡️ |

## Windows Clone 初次設定 — Symlink 支援

`vibe-k8s-lab` repo 裡有數個重要的 symlink，在 **Windows clone 端** 必須啟用 symlink
支援才能正確 checkout。症狀是某些 `.md` / YAML 檔案變成 ~13 byte 的純文字，
內容是 target 字串（例如 `docs/CHANGELOG.md` 會變成含 `"../CHANGELOG.md"` 的
純文字檔）。

### 已知會被影響的 symlink

| Repo 路徑 | Target | 作用 |
|----------|--------|------|
| `docs/CHANGELOG.md` | `../CHANGELOG.md` | 讓 MkDocs 能 serve repo root 的 CHANGELOG |
| `docs/README-root.md` | `../README.md` | 中文版 README 的 docs-tree 鏡像 |
| `docs/README-root.en.md` | `../README.en.md` | 英文版 README 的 docs-tree 鏡像 |
| `rule-packs/*.yaml`（部分） | `../conf.d/...` | Rule pack hot-reload 來源 |

### 推薦方案：開啟 Windows Developer Mode（一次性設定）

Windows 10 1703+ / Windows 11 內建「開發人員模式」，啟用後 symlink 建立
**不再需要 admin 權限**，對所有工具透明。

```powershell
# 方法 A — Windows Settings UI
# 設定 → 隱私權與安全性 → 開發人員專用 → 「開發人員模式」ON

# 方法 B — Registry（需 admin 一次；啟用後永久）
reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock" `
  /t REG_DWORD /f /v AllowDevelopmentWithoutDevLicense /d 1
```

啟用後，在 Windows clone 端設定 Git 並重新 checkout：

```bash
# 在 Windows Git Bash / MSYS2 / WSL 的 clone 目錄下
git config core.symlinks true

# 強制重建 working tree（已物化的純文字檔會變回真 symlink）
git rm --cached -r .
git reset --hard HEAD
```

### 退路方案：Symlink 物化檢測腳本

如果 Developer Mode 不能開（例如公司 IT 鎖管制原則），至少要偵測物化
情形，避免不小心把 `"../CHANGELOG.md"` 當成真實內容 commit 回去。
建議把下列檢測加到 pre-commit 或 CI：

```bash
for f in docs/CHANGELOG.md docs/README-root.md docs/README-root.en.md; do
  # 真 symlink：git ls-files -s 前綴 120000
  mode=$(git ls-files -s "$f" | awk '{print $1}')
  if [ "$mode" != "120000" ]; then
    echo "ERROR: $f 已物化為一般檔案 (mode=$mode)，將損壞 MkDocs serve"
    exit 1
  fi
done
```

### 工具層配合（已完成）

專案內的文件品質工具都已經知道要跳過這些 symlink proxy，避免在 FUSE 側
或誤物化時覆蓋 target 字串：

- `scripts/tools/dx/doc_coverage.py` — `EXCLUDE_RELATIVE_PATHS`
- `scripts/tools/dx/add_frontmatter.py` — `EXCLUDE_RELATIVE_PATHS` + `os.path.islink` 跳過
- `scripts/tools/dx/generate_doc_map.py` — `SKIP_FILES` / `SKIP_FILENAME_PREFIXES`

寫新的 doc-scanning 工具時**請沿用同一套清單**，否則會在 FUSE 側踩雷
（見 `archive/lessons-learned.md` 的 add_frontmatter.py 事件）。

### v2.7.1 LL：`end-of-file-fixer` 會把 symlink blob 弄壞

**事件**（PR #30 / commit `e148cd9` → fix `e697fec`）：跨 session 清理「FUSE EOF
newline drift」36 檔時，把 `docs/CHANGELOG.md` 這個 symlink 也一起 sweep，
結果 blob 從 15 bytes `../CHANGELOG.md` 變成 16 bytes `../CHANGELOG.md\n`。
Linux CI 的 `readlink()` 解不了 `../CHANGELOG.md\n` 這個路徑，四個 CI job 同時爆：

- **Check Documentation Links**：三處 `../CHANGELOG.md` 反向引用全壞（`docs/design/roadmap-future{,.en}.md` + `docs/internal/design-system-guide.md`）
- **Lint**（Repo name guard）/ **Drift Detection**（repo_name）：`FileNotFoundError: docs/CHANGELOG.md`
- **Go Tests**：watcher 掃描 config dir 時 walk error（symlink loop 失敗）

**根因**：`fix_file_hygiene.py` 本身有 `os.path.islink()` runtime guard，Linux 側
會被擋下；但 Windows clone 若 `core.symlinks=false`（Developer Mode 未開），
symlink 被物化成純文字檔，`os.path.islink()` 回傳 False → 換行照寫進 blob。

**防線**（此事件後固化，two-layer）：

1. **pre-commit `file-hygiene` exclude 正則**（Windows-side safety net）
   ```yaml
   # .pre-commit-config.yaml
   exclude: '^docs/(README-root(\.en)?|CHANGELOG)\.md$'
   ```
   涵蓋 repo 所有 symlink proxy md 檔。新增 symlink 時必須同步擴充這個
   regex，否則下次 Windows-side commit 會重演同樣劇本。
2. **runtime `os.path.islink()` guard**（Linux/FUSE-side 原有防線，仍保留）

**Rule of thumb — 做 bulk cleanup 前先過濾 120000 blob**：

```bash
# 任何「掃全 repo 補換行 / 改 encoding / 做 find-replace」的 bulk commit
# 前，都要先把 symlink 排除：
git ls-files | while read f; do
  mode=$(git ls-files -s "$f" | awk '{print $1}')
  [ "$mode" != "120000" ] && echo "$f"
done > _bulk_candidates.txt
```

**現況確認**（repo 有 4 個 symlink）：

| 路徑 | 類型 | 防線 |
|------|------|------|
| `docs/README-root.md` | file symlink | `file-hygiene` exclude regex ✅ |
| `docs/README-root.en.md` | file symlink | `file-hygiene` exclude regex ✅ |
| `docs/CHANGELOG.md` | file symlink | `file-hygiene` exclude regex ✅（v2.7.1 補） |
| `docs/rule-packs` | dir symlink | 目錄不走 `file-hygiene`，天然安全 |

## FUSE Phantom Lock 防治

FUSE 跨層掛載（Windows NTFS → VirtioFS → Cowork VM → Docker bind mount）是 `.git/*.lock` 殘留的根本原因。以下是分層防治措施（預防 → 偵測 → 修復 → 驗證）：

### ⛔ 明確禁止清單（v2.7.0 Phase .e LL 固化）

以下操作在 FUSE 環境下**確認會壞事**，一律禁用：

| 禁用 | 根因 | 正確做法 |
|------|------|---------|
| `cp .git/index /tmp/xxx` + `GIT_INDEX_FILE=/tmp/xxx git commit-tree` | FUSE 側 `.git/index` 永遠是 stale 的；temp index + commit-tree 產出的 tree 物件不含真實修改 → push 後遠端看到空 commit | 所有 git add/commit/push **必須從 Windows 側執行**：`scripts/ops/win_git_escape.bat` 或 `cd C:\Users\<USER>\vibe-k8s-lab && git add ... && git commit --no-verify -F _msg.txt && git push` |
| `docker exec vibe-dev-container git add ...`（在 FUSE mount 上） | Dev Container bind-mount 看到的是 FUSE 側的 `.git/`，index 讀取在 stat cache 層可能不一致；與 Windows 側併用時會互踩 index lock | git write 操作全集中在 **Windows 原生 git**；Dev Container 只做 `git log` / `git status` / Go test / pre-commit 等唯讀或可重跑的操作 |
| `.bat` 檔案內含 CJK 註解或字串（e.g. 中文的 `rem`） | Desktop Commander `start_process` 讀取 `.bat` 的 encoding 不一致 → batch parser 看到截斷指令；`cmd /c` 間接呼叫同樣失敗 | `.bat` 內**全 ASCII 註解與字串**；CJK 內容只放對應 `.md` playbook。範例：`win_git_escape.bat` 在 commit `e55d9af` 已改全英文註解 🛡️（詳見 [陷阱 #45](#已知陷阱速查)） |
| 任何 git 子命令的 `-i` / `--interactive` flag | MCP shell 無法開啟編輯器；rebase/add/commit 會 hang 直到 timeout | 用非互動替代：`git -c sequence.editor=true -c core.editor=true rebase --autosquash`、`git commit -F file.txt`、`git rm --cached` 直接下 path（詳見 [陷阱 #41](#已知陷阱速查)） |
| 從 FUSE 側用 `rm -f .git/*.lock` 清 phantom lock | FUSE dentry cache 薛丁格態：`ls` 看得到、`unlink` 回 EPERM；清了也只是假象 | 用 `bash scripts/session-guards/git_check_lock.sh --clean`（會自動偵測並給出正確動作建議）；真正清不掉時走 Windows MCP `Remove-Item` 或 §修復層 B Level 6 rename-trick |

> **決策助記**：FUSE 側可以 **read** (stat/cat/diff)，但一切會**寫 NTFS metadata** 的操作（commit / add / lock acquire / 清 lock）都走 Windows 原生 git。

### 預防層：降低 Lock 發生機率

**1. VS Code Git 開關（專案級，不影響其他專案）**

```bash
# Agent session 開始時 — 關閉 VS Code 背景 Git
python scripts/session-guards/vscode_git_toggle.py off

# Session 結束或手動開發時 — 打開
python scripts/session-guards/vscode_git_toggle.py on

# 查看目前狀態
python scripts/session-guards/vscode_git_toggle.py
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
bash scripts/session-guards/git_check_lock.sh

# 診斷 + 清理（只清 >30s 且無活躍 git process 的 stale lock）
bash scripts/session-guards/git_check_lock.sh --clean
```

若 Cowork VM 無法刪除（`Operation not permitted`），腳本會輸出對應的 Windows MCP 指令。

### 跨平台 Line Ending

`.gitattributes` 確保 repo 內一律 LF，避免 CRLF/LF 混用在 FUSE 上造成額外的 diff 雜訊和 index 更新。

### v2.8.0 Resilience Tooling（PR #44 session resilience + token economy bundle）

`win_git_escape.bat` 是 FUSE 側卡死時的 **大逃生門**（需要 Windows-MCP 可用）。PR #44 加了一組更細顆粒度、**從 sandbox 側就能跑** 的工具，涵蓋 index 損壞 / phantom lock / commit-msg 驗證 / pre-push gate 四類 session resilience 問題。

| 工具 | 解決的具體問題 | 典型呼叫 |
|------|--------------|---------|
| `scripts/ops/fuse_plumbing_commit.py` | `.git/index` 鎖住 / phantom 或 partially-corrupt 時仍需 commit — 用 git plumbing (`hash-object` → `update-index --cacheinfo` → `write-tree` → `commit-tree` → 直寫 `refs/heads/<branch>`) 走下去，完全不觸發 `.git/index.lock` 路徑 | `python3 scripts/ops/fuse_plumbing_commit.py --auto --msg _msg.txt <files...>`（`--auto` 偵測 phantom lock 後自動選 plumbing path） |
| `scripts/ops/recover_index.sh` | `.git/index` 已經 corrupt 或寫入一半；用 `git read-tree HEAD` 在 tmp 產生新 index，**以 atomic cp+rename 換掉 `.git/index`**（同 FS 上的 rename 才保證原子性 — trap EXIT 清 staging 檔） | `bash scripts/ops/recover_index.sh`（或 `make recover-index`） |
| `make fuse-locks` | 列出 `.git/*.lock` 殘留、每個 lock 的 age / holder process / FUSE phantom 狀態 | `make fuse-locks` |
| `make fuse-commit MSG=_msg.txt FILES="a b"` | 前項 `fuse_plumbing_commit.py --auto` 的 Make 封裝 | `make fuse-commit MSG=_msg.txt FILES="scripts/ops/x.sh docs/y.md"` |
| `scripts/hooks/commit-msg` | Conventional Commits **本地驗證**（不依賴 PyYAML，手解 `.commitlintrc.yaml` 的 `type-enum` / `scope-enum`）— 讓 Windows 側 `--no-verify` 的 commit 仍有 commit-msg gate | `git commit -F _msg.txt`（hook 自動觸發；session-init hook 會 auto-install）|
| `scripts/tools/dx/pr_preflight.py` | pre-push marker 寫 `.git/.preflight-ok.<SHA>`；**狀態感知**：透過 `gh pr view <branch>` 判斷 PR 狀態，OPEN PR 時 `require_preflight_pass.sh` 才擋，WIP 允許 push 觸發 CI smoke | `make pr-preflight`（pre-push hook 自動 consume marker）|

**什麼時候用哪一條**（決策助記）：

```
git commit 失敗，錯誤訊息是 ...
│
├─ "Unable to create '.git/index.lock': File exists"
│   └─ 先跑 `bash scripts/session-guards/git_check_lock.sh --clean`；
│      清不掉 → `make fuse-locks` 看 phantom 狀態；
│      仍卡 → `python3 scripts/ops/fuse_plumbing_commit.py --auto --msg <file> <paths>`（繞過 index.lock）
│
├─ "fatal: index file corrupt"（或 `git status` 讀 index 崩）
│   └─ `make recover-index`（atomic 重建 `.git/index` from HEAD tree）→ 再跑 `git status`
│
├─ Commit message 被 commitlint 打回（本地沒裝 commitlint 而不自覺）
│   └─ 自家 `scripts/hooks/commit-msg`（session-init 已自動 install）跑 `.commitlintrc.yaml` 的 type/scope 驗證
│
└─ pre-push hook 說 "preflight marker missing"，但 branch 是 WIP 還沒開 PR
    └─ v2.8.0 後：`require_preflight_pass.sh` 用 `gh pr view <branch>` 判 PR 狀態；
       OPEN 才擋，WIP 直接放行 → 適合快速 push 觸發 CI smoke
```

> **為什麼 plumbing 路徑可以繞 phantom lock**：git porcelain (`git commit`) 一定會 acquire `.git/index.lock`；plumbing 直接操作 object database + refs — `hash-object` 寫 blob 到 `.git/objects/`（新檔，無 lock 爭用），`write-tree` / `commit-tree` 寫 tree & commit 物件（同理），最後只 `echo <sha> > .git/refs/heads/<branch>`（單檔 atomic write）。完全不觸發 `.git/index.lock`。

> **為什麼 `recover_index.sh` 要 cp-to-sibling + mv，而不是直接 `git read-tree` 就好**：`git read-tree` 本身也會嘗試 acquire `.git/index.lock`；若 phantom lock 還在、或 VFS 不允許 `rename(tmp_outside_gitdir, .git/index)` 跨 device/FS，會直接 EPERM。正確做法是先在 `.git/` 內用 `cp "$TMP_IDX" "$INDEX.recover.$$"` 做 staging（同 FS），再 `mv "$INDEX.recover.$$" "$INDEX"` 原子換過去；`trap 'rm -f ...' EXIT` 保證半成品會被清乾淨。

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
python scripts/session-guards/vscode_git_toggle.py off

# (b) 清 stale .git/*.lock
bash scripts/session-guards/git_check_lock.sh --clean

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

**Level 6 — Cowork VM 內的 rename-trick（Level 2/4/5 都不可用時的最後救命稻草）**

2026-04-10 遇到的案例：Cowork 桌面無法重選資料夾、沒有 PowerShell、沒有 docker、沒有 sudo。phantom `.git/index.lock`（inode `7599824371576445`）被 stat/exists 看見，但 `ls`、`open`、`unlink`、`shutil.copy` 全部 ENOENT 或 EPERM。同時 `os.unlink` 在整個 `.git/` 下都回 EPERM（FUSE 層 block unlink）。

關鍵觀察：**CREATE 仍可以成功、RENAME 也可以成功**。於是可以繞過：

```python
import os
# (1) 建一個其他名字的檔案
fd = os.open('.git/_scratch.tmp', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
os.close(fd)

# (2) 把它 rename 到 phantom 路徑 — rename 會 override 掉 phantom dentry，
#     讓 .git/index.lock 變成一個真正存在的 0-byte 檔案
os.rename('.git/_scratch.tmp', '.git/index.lock')

# (3) 再 rename 走 — 此時 .git/index.lock 已是真檔，rename 成功後 dentry 消失
os.rename('.git/index.lock', '.git/_old_lock.tmp')

# (4) 驗證 phantom 已清除
assert 'index.lock' not in os.listdir('.git')
assert not os.path.exists('.git/index.lock')

# (5) 測試 git 的 O_CREAT|O_EXCL 現在可以用
fd = os.open('.git/index.lock', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
os.close(fd)
os.rename('.git/index.lock', '.git/_old_lock2.tmp')  # 讓 git 可以自己 acquire
```

清理殘留的 `.git/_old_lock*.tmp` 需要等下次 Level 2/4 cold-restart — 這些 0-byte 檔案不影響 git 操作。

為何 rename 可行：FUSE 的 rename 走 `create+unlink` path 的相反操作（由 userspace driver 代為執行 NTFS 層的 `MoveFileEx`），而 Windows 的 `MoveFileEx` 在 phantom dentry 情況下會對齊到真實 NTFS 狀態，等於強制 dentry 重新 validate 一次。同理，`O_CREAT|O_EXCL` 在 phantom dentry 下會 EEXIST，但 rename-over 不會。

### Git 操作決策樹

```
Git 操作入口
│
├─ 1. make git-preflight（每次 git 操作前必跑）
│  ├─ ✅ 成功 → 在 Cowork VM / Dev Container 正常操作
│  └─ ❌ 失敗（lock 清不掉）
│     │
│     ├─ 2. make fuse-reset（等 10 秒）
│     │  ├─ ✅ 成功 → 正常操作
│     │  └─ ❌ 仍失敗
│     │     │
│     │     ├─ 3. Windows 逃生門
│     │     │  └─ scripts/ops/win_git_escape.bat <command>
│     │     │     不要重新造輪子寫腳本！
│     │     │
│     │     └─ 4. 連 Windows 也失敗 → 回報使用者，不要無限重試
│     │
│     ⚠️ 嘗試上限：每層最多重試 2 次，總計不超過 5 分鐘
```

### 三層環境職責矩陣

| 操作 | 主路徑：Dev Container | 備援：Cowork VM | 逃生門：Windows Native |
|------|---------------------|----------------|---------------------|
| Code editing | ✅ | ✅ Read+Edit | ❌ |
| Go test | ✅ | ❌ | ❌ |
| Python test | ✅ | ✅ | ❌ |
| git add/commit | ✅ | ⚠️ FUSE 風險 | ✅ `win_git_escape.bat` |
| git push | ✅ | ⚠️ FUSE 風險 | ✅ `win_git_escape.bat` |
| git tag | ✅ | ⚠️ FUSE 風險 | ✅ `win_git_escape.bat` |
| gh pr create / pr checks / run view | ✅ | ❌ gh 不在 VM | ✅ `win_gh.bat`（推薦） / `win_git_escape.ps1`（legacy） |
| pre-commit | ✅ | ✅ | ❌ 環境不完整 |
| Helm / K8s | ✅ | ❌ | ❌ |

> **設計原則**：Dev Container 是主路徑。FUSE 卡死時 Windows 是逃生門。目標是不讓 session 卡死。

### 修復層 C：Windows 原生 Git Fallback（FUSE 側卡死時的備援路徑）

FUSE 側 git 操作反覆卡住、或 pre-commit hook 在 FUSE mount 上一直踩到 index lock 時，**Windows 原生 cmd/PowerShell 是第二條可走的路徑**。

**⛔⛔⛔ 鐵則：絕對不要自己寫 `_*.bat` / `_*.ps1` 腳本。**

PR #39 寫了 1 個 `_p39_commit.ps1`。PR #40 寫了 5 個 `_p40_*.bat|.ps1`。每次都 reinvent wheel、每次都踩新坑、每次都留下 cleanup burden。**PR #41 (v2.8.0) 起，`scripts/tools/lint/check_ad_hoc_git_scripts.py` (L1 pre-commit hook) 會 physically block** 任何在 `scripts/ops/`、`scripts/tools/`、`tools/` 之外的 `*.bat`/`*.ps1`/`*.cmd`。這是 whitelist（不是 blacklist regex），不能用新動詞繞過。

**已有的標準化逃生門工具**（Agent session 用這些，不要寫新的）：

| 工具 | 用途 | 典型呼叫 |
|------|------|---------|
| `scripts/ops/win_git_escape.bat` | Git 操作（FUSE 卡死時的主路徑） | `status\|add\|commit-file\|push\|tag\|branch\|log\|diff\|preflight\|pr-preflight\|fix-hooks` |
| `scripts/ops/win_gh.bat` | GitHub CLI（MCP-friendly 短路徑 + CRLF + ASCII）**v2.8.0 新增** | `pr-checks [PR#]\|pr-view [PR#]\|pr-create <flags>\|run-view <ID>\|run-log <ID>\|raw <args>` |
| `scripts/ops/win_git_escape.ps1` | GitHub 操作（Release 流程用，保留 legacy） | `pr-create\|pr-list\|ci-status\|release-create` |
| `make win-commit` | Hook-gated commit（sandbox pre-commit + Windows git 三步） | `make win-commit MSG=_msg.txt FILES="a b"` |
| `scripts/ops/run_hooks_sandbox.sh` | Sandbox 側 pre-commit gate（補 Windows 側 `--no-verify` 漏洞） | `bash scripts/ops/run_hooks_sandbox.sh a.md b.yaml` |
| `scripts/ops/win_async_exec.ps1` | MCP 60s timeout 繞道（派工 + poll log） | `-Command "..." -LogFile _out.log` |
| `scripts/ops/win_read_fresh.ps1` | FUSE dentry cache bypass | `-Path <src> -OutFile <dest>` |

> **子命令缺失？擴充現有 wrapper，不要寫 sibling script。** `win_git_escape.bat` / `win_gh.bat` 都有 `raw <args>` 逃生門可以塞任意命令。真的需要新 subcommand 就開 PR 加進去，下次 session 才能重複使用。

工作模式：

| 操作類型 | 走哪邊 | 原因 |
|---------|-------|------|
| 檔案 Read/Edit/Write | Claude 的檔案 tool（走 FUSE mount） | 雙向可見、原子寫入 |
| `git status` / `git add` / `git commit` / `git push` | `win_git_escape.bat` → Windows 原生 git | git index lock 寫在 Windows NTFS，不走 FUSE metadata |
| Hook-gated commit（pre-commit + stage + commit-file + push） | `make win-commit MSG=... FILES=... [SKIP=...]` | 分層：sandbox 跑 hook-gate，Windows 跑 git；Windows 端 `--no-verify` 是內部實作（陷阱 #36） |
| `gh pr create` / `gh run list` | `win_git_escape.ps1` → Windows 原生 gh | gh CLI 不在 Cowork VM 內 |
| 預期 > 60s 的命令（`gh pr checks`、大型 `git push`、pre-push 全量 hook） | `win_async_exec.ps1` → fire-and-forget + poll log | 避開 MCP RPC 60s timeout（陷阱 #47） |
| 剛被 Windows 側修改的檔案需 sandbox 側讀 | `win_read_fresh.ps1` → Win32 ReadAllBytes → 新 inode | 繞過 FUSE dentry cache（陷阱 #44） |
| pre-commit 執行（手動跑） | 推薦走 `run_hooks_sandbox.sh`（sandbox），或 Windows 原生 Python + `python -m pre_commit` | sandbox 路徑無 FUSE stat 延遲、無 `.git/index` 風險 |

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

**pre-push hook 相容性**：pre-commit 產生的 `.git/hooks/pre-push` 會寫死 Linux python 路徑，修法見上方陷阱 #36。

### 修復層 C.1：Escape Helpers（MCP 60s timeout + FUSE cache bypass）

逃生門路徑本身還會踩到兩類環境問題：

1. **MCP PowerShell 60 秒硬 timeout** — 任何同步執行（即使 `Start-Process -PassThru` 取 PID）只要 MCP RPC 還沒回應就會被截斷（陷阱 #47）。`gh pr checks`、大型 `git push`、pre-push 全量 hook 都會中招。
2. **FUSE dentry / attribute cache** — Windows 側剛寫完的檔案，sandbox 側 `Read` tool 可能仍拿 stale 內容（陷阱 #44、#52）；CI log、pre-commit hook 寫回的檔案尤其常見。

為此打包了 4 個 helper，取代自己寫一次性 PowerShell snippet：

| Helper | 解決問題 | 典型用法 |
|--------|---------|---------|
| `scripts/ops/win_async_exec.ps1` | MCP 60s timeout | 派工 → 拿 PID → sandbox 側輪詢 log 檔案 |
| `scripts/ops/win_read_fresh.ps1` | FUSE dentry cache | 用 Win32 `ReadAllBytes` 讀 source → 寫到新 inode（預設 `<path>.fresh`） |
| `scripts/ops/run_hooks_sandbox.sh` | sandbox-side pre-commit gate（補 Windows 側 `--no-verify` 的漏洞） | `bash scripts/ops/run_hooks_sandbox.sh a.md b.yaml` |
| `make win-commit` | 包 hook-gate + `win_git_escape.bat` 三步（pre-commit → add → commit-file → push） | `make win-commit MSG=_msg.txt FILES="a b" SKIP=head-blob-hygiene` |

#### 1. `win_async_exec.ps1` — fire-and-forget

解決陷阱 #47 的根因：MCP RPC 不等 `Start-Process` 回來就會 timeout。這個 helper `Start-Process -WindowStyle Hidden -PassThru` 後立即輸出 `ASYNC PID=<pid> LOG=<path>`（grep-friendly 格式），MCP 呼叫馬上返回。

三段式流程：

```powershell
# 1. Windows 側派工（MCP 馬上拿回 PID，不會 timeout）
.\scripts\ops\win_async_exec.ps1 `
  -Command "gh pr checks 27" `
  -LogFile _pr.log
# 輸出: ASYNC PID=12345 LOG=C:\...\_pr.log
```

```bash
# 2. Sandbox 側輪詢 log（MCP timeout 不是問題，bash sleep 是 sandbox 本地）
sleep 10
# 3. 用 Read tool 讀最終結果
```

Parameters 設計：
- `-Command`（必填）：cmd.exe 解析，可直接寫 pipeline（`script1.bat && script2.bat`）
- `-LogFile`：預設 `%TEMP%\vibe-async-<timestamp>.log`；已存在會被覆寫避免舊內容誤導
- `-PidFile`：選填；寫入 child PID 方便後續 kill / 檢查
- `-WorkingDirectory`：預設當前目錄，推薦傳絕對路徑（例：`C:\Users\<USER>\vibe-k8s-lab`）

⚠️ **Caveat — nested quote 含 `(` / `+` 會被 cmd /c 吞掉（見陷阱 #53）**：
`-Command` 最終交給 `cmd.exe /c`，如果你傳的命令內含有**雙層引號** + 括號或運算符號（如 `gh pr create --title "docs: promote X to SSOT + retire Y (v2.9.0+)"`），cmd 的 parenthesis / operator parser 會把 title 尾段吃掉，**PR 其實建成功但 title 缺尾**，log 只看到前半截、operator 誤判失敗去重試。**對症解法**：把複雜命令寫成獨立 `.ps1`，內部用 single-quote variable 宣告完整 title，然後 `win_async_exec.ps1 -Command 'powershell -File _pr_make.ps1'` 派工。這是 §黃金法則「複雜指令寫成獨立腳本」的具體應用場景（gh PR create / git 大批次 add 等皆適用）。

#### 2. `win_read_fresh.ps1` — bypass FUSE cache

Windows 側剛修完的檔案（pre-commit hook EOF 換行、CI 剛寫完的 log），sandbox 側 `Read` tool 可能仍拿舊內容。這個 helper 用 `[System.IO.File]::ReadAllBytes()`（走 Win32 原生路徑，不走 FUSE）讀取 source，再 `WriteAllBytes` 到 dest，產生**新 mtime + 新 inode 參照**讓 FUSE 側看到全新 entry。

```powershell
# 典型用途：讀 CI log 到 sandbox 可見的新 inode
.\scripts\ops\win_read_fresh.ps1 -Path _pr.log -OutFile _pr.fresh.log
# 輸出: FRESH SRC=... DEST=... BYTES=1234
# sandbox: Read _pr.fresh.log（保證不 stale）
```

```powershell
# 快速 dump 小檔到 stdout（> 1MB 自動截斷 + warning）
.\scripts\ops\win_read_fresh.ps1 -Path docs\internal\component-health-snapshot.md -PrintToStdout
```

單向設計（Windows → sandbox）。反向（sandbox → Windows 寫檔）直接用 `Write` tool 即可，FUSE write-through 通常即時生效。若某路徑長期 stale 仍不更新，升級到 [§修復層 B Level 1 drop_caches](#修復層-bfuse-cache-重建level-1-5) 做全域重建。

#### 3. `run_hooks_sandbox.sh` — sandbox-side pre-commit gate

`win_git_escape.bat commit-file` 內部跑 `git commit --no-verify`（陷阱 #36：pre-commit 的 shebang 寫死 Linux python path，Windows 側 git.exe 呼叫 hook 直接 404）。這意味著走 Windows 逃生門時，**本地 pre-commit hooks 完全被繞過**——只剩 CI 接住，但 CI 失敗時你已經 push 上去了。

這個 wrapper 在 Cowork VM（sandbox）側跑 pre-commit，補上那個漏洞：

```bash
bash scripts/ops/run_hooks_sandbox.sh scripts/ops/run_hooks_sandbox.sh docs/internal/windows-mcp-playbook.md
# 輸出: HOOKS STATUS=PASS FILES=2 DURATION=28s
```

為什麼 sandbox 側可行：
1. **乾淨 ext4**，無 FUSE dentry cache / lock 陷阱
2. 31 個 auto-stage hook 都是純 Python（+ pyyaml）— 完全不需 docker / Go / Helm
3. 用 `pre-commit run --files` 模式，**繞過 pre-commit 的 stash 邏輯**（避開 FUSE 側 `.git/index` 可能 corrupt 的問題，見規則 #2b）

輸出格式（grep-friendly 最後一行）：
- 成功：`HOOKS STATUS=PASS FILES=<n> DURATION=<s>s`
- 失敗：`HOOKS STATUS=FAIL FILES=<n> DURATION=<s>s LOG=<path>`

#### 4. `make win-commit` — hook-gated Windows commit workflow

`win_git_escape.bat` 已經提供 `add` / `commit-file` / `push` 三個子命令，但實際 session 常需三個連著跑，加上 sandbox hook-gate 一共四步。這個 Make target 把它們封成一行：

```bash
make win-commit MSG=_msg.txt FILES="scripts/ops/run_hooks_sandbox.sh docs/internal/windows-mcp-playbook.md"
```

**執行順序（三階段，每階段失敗即 abort；log 實際印的 label 就是 `[1/3]` / `[2/3]` / `[3/3]`）**：

1. **[1/3] Sandbox hook gate** — 呼叫 `run_hooks_sandbox.sh $(FILES)`，失敗就停；緊急繞道：`SKIP_HOOKS=1`
2. **[2/3] Windows stage + commit** — `cmd /c win_git_escape.bat add $(FILES)` 後接 `commit-file $(MSG)`（內部 `--no-verify`，因 Windows 端 hook 本來就無法執行；add + commit 合併為同一階段、同一 label，不拆 `[2/3a]` / `[2/3b]`）
3. **[3/3] Windows push** — `cmd /c win_git_escape.bat push`（內部 `--no-verify` 避開陷阱 #36 的 pre-push hook）

必填：`MSG=<message-file>`（UTF-8 without BOM）
選填：
- `FILES=<space-separated paths>`（省略時跳過 hook-gate 和 add，假設已 staged）
- `SKIP=<hook1,hook2>`（targeted skip，pre-commit 原生 env var）
- `SKIP_HOOKS=1`（緊急完全繞過 sandbox hook-gate，只在 runner 異常時用）

**分層設計（誠實版）**：

| 層 | 執行位置 | --no-verify？ | 原因 |
|----|---------|--------------|------|
| Sandbox hook-gate | Cowork VM（ext4） | ❌ 不繞過 | 環境完整，hooks 真的有執行 |
| Windows git | Windows（NTFS） | ✅ 內部固定繞過 | 陷阱 #36：Windows git.exe 無法呼叫 hook |

換句話說：**hooks 不是被 `--no-verify` 繞過的，而是移到 sandbox 側跑**。Windows 側的 `--no-verify` 是內部實作細節，不是設計漏洞。

其他設計選擇：
- Message 一律走檔案（`commit-file` 子命令）— 避開陷阱 #46（cmd 對 em-dash/CJK 引號解析崩潰）
- Sandbox 側呼叫時自動 detect `cmd.exe`，不存在則印出可複製的 Windows 指令給 user 手動執行（那種情境下 hook-gate 仍會先跑，結果是 sandbox 驗過再手動收尾）

### 修復層 D：Dev Container Push（pre-push hook 撈到無關 drift 時的備援）

**適用情境**：`.pre-commit-config.yaml` 未設 `default_stages`、或設為多 stage 時，`git push` 會觸發非 `pre-commit` stage 的全量 hook 跑。若其中任一 hook 掃全 repo 而非「只看 staged files」（例：`bilingual-structure-check` 掃 `.en.md` 整份對照），就可能因 **pre-existing drift**（非這次 commits 造成的）而擋住 push。PR #21 就踩到這個坑：`chore/structure-cleanup-2026-04-11` 的內容完全乾淨，但 pre-push 掃到 62 對 ZH/EN 檔案的舊 drift → 23 errors + 18 warnings。

⚠️ **長期解在 Layer 3 不在這個章節**：如果你發現自己要走 Layer 1/2，那代表 `.pre-commit-config.yaml` 需要先確認 Layer 3 已套用。走 Layer 1/2 是「這次 PR 救火」，不是「下次可以當正規流程」。

#### Layer 1 — A/B 驗證 one-liner（機械化 self-check）

pre-push hook 擋路時，第一件事：**證明失敗是否跟這次 commits 有關**。用 `git worktree` 跳到 base commit 重跑同一個 hook，若結果一樣 → drift 跟這次無關，可走 `--no-verify`；若結果不同 → 這次 commits 引入新問題，必須修。

```bash
# 假設 broken hook 是 bilingual-structure-check，當前 branch 是 feat/xxx
BASE=$(git merge-base HEAD origin/main)
WT=/tmp/wt-$BASE

git worktree add "$WT" "$BASE" 2>/dev/null || true
cd "$WT"
pre-commit run bilingual-structure-check --all-files > /tmp/wt-base.log 2>&1
ERR_BASE=$(grep -c "error:" /tmp/wt-base.log || echo 0)

cd - >/dev/null
pre-commit run bilingual-structure-check --all-files > /tmp/wt-head.log 2>&1
ERR_HEAD=$(grep -c "error:" /tmp/wt-head.log || echo 0)

echo "base=$ERR_BASE head=$ERR_HEAD"
# base==head 且 >0 → 100% pre-existing drift，本次 PR 無辜
# head > base → 這次引入了新問題，不要 --no-verify
git worktree remove "$WT"
```

同時驗證 CI **是否真的會跑這個 hook**（CI 用 `pre-commit run <id>` 按名字叫，不會自動跑全部）：

```bash
grep -r "<hook-id>" .github/workflows/ || echo "CI 沒叫這個 hook — pre-push 擋下來是 local-only false positive"
```

#### Layer 2 — 決策樹（明確 if-else）

pre-push hook 失敗後，按順序回答：

```
Q1. base/head error count 一樣嗎？（用 Layer 1 one-liner）
    ├─ 否（head > base）→ 這次 commits 引入新問題，修掉，不要 --no-verify
    └─ 是（pre-existing drift）→ 走 Q2


### 修復層 D · pre-push drift 三層改進（Layer 1-3）

與 §修復層 C · 替代路線 D 互補但不同根因：§替代路線 D 處理「pre-push hook spawn 失敗（Linux python 路徑寫死）」，本節處理「pre-push hook 跑起來了、但掃到**非本次 commits 的 pre-existing drift**」。Windows 原生 git + 容器 git 兩條路徑都可能遇到。

**適用情境**：`.pre-commit-config.yaml` 未設 `default_stages`、或設為多 stage 時，`git push` 會觸發非 `pre-commit` stage 的全量 hook 跑。若其中任一 hook 掃全 repo 而非「只看 staged files」（例：`bilingual-structure-check` 掃 `.en.md` 整份對照），就可能因 **pre-existing drift**（非這次 commits 造成的）而擋住 push。PR #21 就踩到這個坑：`chore/structure-cleanup-2026-04-11` 的內容完全乾淨，但 pre-push 掃到 62 對 ZH/EN 檔案的舊 drift → 23 errors + 18 warnings。

⚠️ **長期解在 Layer 3 不在這個章節**：如果你發現自己要走 Layer 1/2，那代表 `.pre-commit-config.yaml` 需要先確認 Layer 3 已套用。走 Layer 1/2 是「這次 PR 救火」，不是「下次可以當正規流程」。

#### Layer 1 — A/B 驗證 one-liner（機械化 self-check）

pre-push hook 擋路時，第一件事：**證明失敗是否跟這次 commits 有關**。用 `git worktree` 跳到 base commit 重跑同一個 hook，若結果一樣 → drift 跟這次無關，可走 `--no-verify`；若結果不同 → 這次 commits 引入新問題，必須修。

```bash
# 假設 broken hook 是 bilingual-structure-check，當前 branch 是 feat/xxx
BASE=$(git merge-base HEAD origin/main)
WT=/tmp/wt-$BASE

git worktree add "$WT" "$BASE" 2>/dev/null || true
cd "$WT"
pre-commit run bilingual-structure-check --all-files > /tmp/wt-base.log 2>&1
ERR_BASE=$(grep -c "error:" /tmp/wt-base.log || echo 0)

cd - >/dev/null
pre-commit run bilingual-structure-check --all-files > /tmp/wt-head.log 2>&1
ERR_HEAD=$(grep -c "error:" /tmp/wt-head.log || echo 0)

echo "base=$ERR_BASE head=$ERR_HEAD"
# base==head 且 >0 → 100% pre-existing drift，本次 PR 無辜
# head > base → 這次引入了新問題，不要 --no-verify
git worktree remove "$WT"
```

同時驗證 CI **是否真的會跑這個 hook**（CI 用 `pre-commit run <id>` 按名字叫，不會自動跑全部）:

```bash
grep -r "
