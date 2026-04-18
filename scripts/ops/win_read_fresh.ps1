<#
.SYNOPSIS
    Windows Read Fresh — bypass FUSE dentry cache when reading a file from sandbox.

.DESCRIPTION
    Cowork / Claude Code 的 sandbox 透過 FUSE 掛載 Windows 檔案系統。
    FUSE 會 cache dentry / attribute / 有時甚至內容本身；當一個檔案剛在
    Windows 側被修改（例如 pre-commit hook 改了 EOF 換行、或 CI log
    剛寫入），sandbox 側的 Read tool 仍可能拿到 stale 版本。

    這個 helper 用 Win32 原生 API 讀取 source，寫到 dest（通常是 sandbox
    看得到的 "新 inode"），讓後續 Read tool 繞過 FUSE cache。

    設計重點：
    - Get-Content -Raw 會走 FUSE 路徑；改用 [System.IO.File]::ReadAllBytes
      確保走 Windows 原生路徑。
    - dest 若已存在會被覆寫，產生「新的 mtime + 新的 inode 參照」，
      FUSE 側看到的是完全新的 entry，不受舊 cache 影響。
    - 支援 -PrintToStdout 模式給 debug 用。

.PARAMETER Path
    要讀取的來源檔案（絕對或相對 Windows 路徑）。

.PARAMETER OutFile
    寫入的目的地。預設是 "<Path>.fresh"。
    相對路徑相對於當前目錄。

.PARAMETER PrintToStdout
    讀完後把內容 echo 到 stdout（適合短檔或 debug）。
    若檔案 > 1MB 會自動截斷並加警告。

.EXAMPLE
    # 讀 CI log 到 sandbox 可見的新 inode
    .\win_read_fresh.ps1 -Path _pr.log -OutFile _pr.fresh.log
    # sandbox: Read _pr.fresh.log（保證不 stale）

.EXAMPLE
    # 快速 dump 一個小檔
    .\win_read_fresh.ps1 -Path docs\internal\component-health-snapshot.md -PrintToStdout

.NOTES
    - 本 helper 單向（Windows -> sandbox）。反向需求（sandbox -> Windows
      寫檔）直接用 Write tool 即可，FUSE write-through 通常即時生效。
    - 若 FUSE 長期 cache 某路徑拒不更新，考慮用 windows-mcp-playbook
      §修復層 B Level 1 (drop_caches) 做全域重建。
#>

param(
    [Parameter(Mandatory, Position = 0)]
    [string]$Path,

    [string]$OutFile,

    [switch]$PrintToStdout
)

$ErrorActionPreference = 'Stop'

# --- Resolve source ---
if (-not (Test-Path $Path)) {
    Write-Error "Source path not found: $Path"
    exit 1
}
$absPath = (Resolve-Path $Path).Path

# --- Resolve dest ---
if (-not $OutFile) {
    $OutFile = "$absPath.fresh"
}
if (-not [System.IO.Path]::IsPathRooted($OutFile)) {
    $OutFile = Join-Path (Get-Location).Path $OutFile
}

# --- Read via Win32 (bypasses FUSE routing that `Get-Content` might pick up if invoked from WSL) ---
$bytes = [System.IO.File]::ReadAllBytes($absPath)

# --- Write to dest; overwrite guarantees fresh inode from FUSE's POV ---
$destDir = Split-Path -Parent $OutFile
if ($destDir -and -not (Test-Path $destDir)) {
    New-Item -ItemType Directory -Path $destDir -Force | Out-Null
}
[System.IO.File]::WriteAllBytes($OutFile, $bytes)

# --- Emit summary line (stable format for MCP caller parsing) ---
$size = (Get-Item $OutFile).Length
Write-Output "FRESH SRC=$absPath DEST=$OutFile BYTES=$size"

# --- Optional stdout dump ---
if ($PrintToStdout) {
    if ($size -gt 1MB) {
        Write-Warning "File is $size bytes; stdout dump truncated to first 1MB."
        $truncated = $bytes[0..(1MB - 1)]
        [System.Text.Encoding]::UTF8.GetString($truncated) | Write-Output
    } else {
        [System.Text.Encoding]::UTF8.GetString($bytes) | Write-Output
    }
}
