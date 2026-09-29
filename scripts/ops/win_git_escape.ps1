<#
.SYNOPSIS
    Windows Git/GitHub Escape Hatch — PowerShell 版

.DESCRIPTION
    當 FUSE 層 git 或 gh CLI 卡死時，用 Windows 原生環境完成 GitHub 操作。
    這是逃生門，不是常態做法。主路徑永遠是 Dev Container。

    安全設計：
    - 不含任何 credential（使用 gh auth status 驗證）
    - 輸出重導至 $env:TEMP\vibe-gh-*.txt
    - UTF-8 無 BOM 處理

.EXAMPLE
    .\win_git_escape.ps1 pr-create -Title "Fix bug" -Body "Description"
    .\win_git_escape.ps1 pr-list
    .\win_git_escape.ps1 pr-view 42
    .\win_git_escape.ps1 ci-status
    .\win_git_escape.ps1 release-create -Tag "v2.7.0" -Title "v2.7.0" -BodyFile "release-notes.md"
    .\win_git_escape.ps1 pr-merge 42
#>

param(
    [Parameter(Position = 0, Mandatory)]
    [ValidateSet('pr-create', 'pr-list', 'pr-view', 'pr-merge',
                 'ci-status', 'release-create', 'auth-check', 'pr-preflight')]
    [string]$Command,

    [Parameter(Position = 1)]
    [string]$Arg1,

    [string]$Title,
    [string]$Body,
    [string]$BodyFile,
    [string]$Tag,
    [string]$Base = 'main',
    [switch]$Draft
)

$ErrorActionPreference = 'Stop'
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)  # 無 BOM

# --- 找 gh CLI ---
$gh = Get-Command gh -ErrorAction SilentlyContinue
if (-not $gh) {
    Write-Error "gh CLI not found. Install: winget install GitHub.cli"
    exit 1
}

# --- 找 Repo ---
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = (Resolve-Path (Join-Path $scriptDir '..\..'))
Push-Location $repoRoot

# The first non-zero rc of a gh (or tool) call is this script's rc (#2236).
# Passed unchanged, rc 8 from `gh pr checks` ("still running") included:
# pending is not a pass, and the caller can tell 8 from 1.
$script:rc = 0
function Save-Rc {
    if ($LASTEXITCODE -ne 0 -and $script:rc -eq 0) { $script:rc = $LASTEXITCODE }
}

try {
    switch ($Command) {
        'auth-check' {
            Write-Host "=== GitHub Auth Status ===" -ForegroundColor Cyan
            & gh auth status; Save-Rc
        }

        'pr-create' {
            if (-not $Title) {
                Write-Error "Usage: win_git_escape.ps1 pr-create -Title 'PR Title' [-Body 'desc'] [-BodyFile notes.md] [-Base main] [-Draft]"
                exit 1
            }
            $args_list = @('pr', 'create', '--title', $Title, '--base', $Base)
            if ($BodyFile -and (Test-Path $BodyFile)) {
                $bodyContent = Get-Content $BodyFile -Raw -Encoding UTF8
                $args_list += @('--body', $bodyContent)
            } elseif ($Body) {
                $args_list += @('--body', $Body)
            }
            if ($Draft) { $args_list += '--draft' }

            Write-Host "Creating PR: $Title" -ForegroundColor Cyan
            & gh @args_list; Save-Rc
        }

        'pr-list' {
            Write-Host "=== Open Pull Requests ===" -ForegroundColor Cyan
            & gh pr list --state open; Save-Rc
        }

        'pr-view' {
            if (-not $Arg1) {
                Write-Error "Usage: win_git_escape.ps1 pr-view <number>"
                exit 1
            }
            & gh pr view $Arg1; Save-Rc
        }

        'pr-merge' {
            if (-not $Arg1) {
                Write-Error "Usage: win_git_escape.ps1 pr-merge <number>"
                exit 1
            }
            Write-Host "Merging PR #$Arg1..." -ForegroundColor Cyan
            & gh pr merge $Arg1 --merge --delete-branch; Save-Rc
        }

        'ci-status' {
            Write-Host "=== CI/CD Status ===" -ForegroundColor Cyan
            $branch = & git branch --show-current 2>$null
            if ($branch) {
                Write-Host "Branch: $branch" -ForegroundColor Yellow
                & gh run list --branch $branch --limit 5; Save-Rc
                Write-Host ""
                Write-Host "PR checks:" -ForegroundColor Yellow
                & gh pr checks 2>$null; Save-Rc
            } else {
                & gh run list --limit 10; Save-Rc
            }
        }

        'release-create' {
            if (-not $Tag) {
                Write-Error "Usage: win_git_escape.ps1 release-create -Tag 'v2.7.0' -Title 'v2.7.0' [-BodyFile notes.md]"
                exit 1
            }
            if (-not $Title) { $Title = $Tag }

            $args_list = @('release', 'create', $Tag, '--title', $Title)
            if ($BodyFile -and (Test-Path $BodyFile)) {
                $args_list += @('--notes-file', $BodyFile)
            } elseif ($Body) {
                $args_list += @('--notes', $Body)
            } else {
                $args_list += '--generate-notes'
            }

            Write-Host "Creating release: $Tag" -ForegroundColor Cyan
            & gh @args_list; Save-Rc
        }

        'pr-preflight' {
            # PR 收尾前檢查 — 呼叫 pr_preflight.py
            $preflight_args = @('scripts/tools/dx/pr_preflight.py', '--skip-hooks')
            if ($Arg1) { $preflight_args += @('--pr', $Arg1) }
            Write-Host "=== PR Preflight Check ===" -ForegroundColor Cyan
            # py -3, not python: `python` can be the Store stub, which exits 0
            # without running the tool (#2236, same shape as #1918).
            & py -3 @preflight_args; Save-Rc
        }
    }
} finally {
    Pop-Location
}
exit $script:rc
