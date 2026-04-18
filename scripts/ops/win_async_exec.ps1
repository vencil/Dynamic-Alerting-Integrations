<#
.SYNOPSIS
    Windows Async Exec — fire-and-forget batch/command runner for MCP timeout escape.

.DESCRIPTION
    Cowork / Claude Code 的 MCP PowerShell 工具有 60 秒硬 timeout，
    任何同步執行超過 60s 的命令（即使背景 Start-Process）都會被截斷。
    本 helper 把 "派工 + log + pidfile" 包成一行，呼叫端立即拿回 PID，
    再由 sandbox 側用 Bash/Read 輪詢 log 檔案直到工作完成。

    三段式流程（見 windows-mcp-playbook §Escape Helpers）：
      1. Claude 呼叫 win_async_exec.ps1 -Command "..." -LogFile _out.log
         → 立即返回 PID（不會 60s timeout）
      2. Claude 用 Bash sleep/polling 讀 _out.log 直到內容穩定
      3. Claude 用 Read 取出最終結果

.PARAMETER Command
    要執行的 shell 命令字串。會交給 cmd.exe /c 解析，所以可以寫完整的
    "script1.bat && script2.bat" 管線。

.PARAMETER LogFile
    輸出（stdout + stderr）的目標檔案。相對路徑會相對於當前目錄。
    預設為 %TEMP%\vibe-async-<timestamp>.log。

.PARAMETER PidFile
    選填。若指定，寫入 child process PID 方便後續 kill / 檢查。
    預設不寫入。

.PARAMETER WorkingDirectory
    選填。預設為當前目錄。推薦傳 "C:\Users\<USER>\vibe-k8s-lab"。

.EXAMPLE
    # 派 gh pr checks 27 到 log 檔
    .\win_async_exec.ps1 -Command "gh pr checks 27" -LogFile _pr.log
    # sandbox 側等 10 秒後讀檔
    # bash: sleep 10; cat _pr.log

.EXAMPLE
    # 派 git push（含可能 hang 的 push hook）
    .\win_async_exec.ps1 `
        -Command "scripts\ops\win_git_escape.bat push" `
        -LogFile _push.log `
        -PidFile _push.pid `
        -WorkingDirectory "C:\Users\vencs\vibe-k8s-lab"

.NOTES
    - 不支援 stdin 互動（fire-and-forget 本質上單向）。
    - Command 內的雙引號要 PowerShell-escape（用 `" 或 [char]34）。
    - 若 LogFile 已存在會被覆寫（避免追加舊內容誤導）。
    - 本 helper 刻意不等 process 結束；呼叫端自己輪詢。
#>

param(
    [Parameter(Mandatory)]
    [string]$Command,

    [string]$LogFile,
    [string]$PidFile,
    [string]$WorkingDirectory
)

$ErrorActionPreference = 'Stop'

# --- Resolve working dir ---
if (-not $WorkingDirectory) {
    $WorkingDirectory = (Get-Location).Path
}
if (-not (Test-Path $WorkingDirectory)) {
    Write-Error "WorkingDirectory does not exist: $WorkingDirectory"
    exit 1
}

# --- Resolve log file (absolute path so cmd.exe can write to it regardless of cwd) ---
if (-not $LogFile) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $LogFile = Join-Path $env:TEMP "vibe-async-$stamp.log"
}
if (-not [System.IO.Path]::IsPathRooted($LogFile)) {
    $LogFile = Join-Path $WorkingDirectory $LogFile
}

# Truncate old log (avoid confusion with previous run)
if (Test-Path $LogFile) {
    Remove-Item $LogFile -Force
}
# Touch empty file so callers can poll existence immediately
New-Item -ItemType File -Path $LogFile -Force | Out-Null

# --- Build cmd.exe invocation ---
# cmd /c interprets the rest as one command; redirect both streams to LogFile.
# Double quotes inside $Command must already be escaped by caller.
$cmdArgs = @('/c', "$Command > `"$LogFile`" 2>&1")

# --- Fire-and-forget spawn ---
$proc = Start-Process `
    -FilePath 'cmd.exe' `
    -ArgumentList $cmdArgs `
    -WorkingDirectory $WorkingDirectory `
    -WindowStyle Hidden `
    -PassThru

# --- PID bookkeeping ---
if ($PidFile) {
    if (-not [System.IO.Path]::IsPathRooted($PidFile)) {
        $PidFile = Join-Path $WorkingDirectory $PidFile
    }
    Set-Content -Path $PidFile -Value $proc.Id -Encoding ASCII
}

# --- Emit one-line summary for MCP caller to parse ---
# Format: "ASYNC PID=<pid> LOG=<path>" — stable, grep-friendly.
Write-Output "ASYNC PID=$($proc.Id) LOG=$LogFile"
