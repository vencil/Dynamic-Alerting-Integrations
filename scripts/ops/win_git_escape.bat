@echo off
REM win_git_escape.bat — Windows Git Escape Hatch
REM
REM 當 FUSE 層 git 卡死時，用 Windows 原生 git 完成操作。
REM 這是逃生門，不是常態做法。主路徑永遠是 Dev Container。
REM
REM 用法：
REM   win_git_escape.bat status
REM   win_git_escape.bat add <file1> [file2...]
REM   win_git_escape.bat commit "commit message"
REM   win_git_escape.bat commit-file <msg-file.txt>     ← UTF-8/CJK 安全
REM   win_git_escape.bat push [remote] [branch]
REM   win_git_escape.bat tag <tag-name>
REM   win_git_escape.bat branch <branch-name>
REM   win_git_escape.bat log
REM   win_git_escape.bat diff
REM   win_git_escape.bat preflight
REM   win_git_escape.bat fix-hooks                       ← 修復 CRLF hook
REM
REM ⚠️ commit message 含 CJK/em-dash/特殊字元時，永遠用 commit-file：
REM   echo feat: my message > _msg.txt
REM   win_git_escape.bat commit-file _msg.txt
REM
REM 安全設計：
REM   - 不含任何 credential（使用 gh auth 或 ~/.git-credentials）
REM   - 輸出重導至 %TEMP%\vibe-git-*.txt
REM   - 自動設定 UTF-8 環境

setlocal enabledelayedexpansion

REM --- 環境設定 ---
set "PYTHONUTF8=1"
chcp 65001 >nul 2>&1

REM --- 找 Git ---
set "GIT_CMD="
where git >nul 2>&1 && set "GIT_CMD=git"
if "%GIT_CMD%"=="" (
    if exist "C:\Program Files\Git\cmd\git.exe" (
        set "GIT_CMD=C:\Program Files\Git\cmd\git.exe"
    )
)
if "%GIT_CMD%"=="" (
    echo ERROR: git not found in PATH or default location
    exit /b 1
)

REM --- 找 Repo ---
set "REPO_DIR="
if exist "%~dp0..\..\..\.git" (
    REM 從 scripts\ops\ 往上推到 repo root
    pushd "%~dp0..\.."
    set "REPO_DIR=!CD!"
    popd
) else (
    REM fallback: 當前目錄
    set "REPO_DIR=%CD%"
)

REM --- 輸出檔 ---
set "OUT=%TEMP%\vibe-git-out.txt"
set "ERR=%TEMP%\vibe-git-err.txt"

REM --- 指令分派 ---
set "CMD=%~1"
if "%CMD%"=="" goto :usage

pushd "%REPO_DIR%"

REM --- 自動清理 phantom lock（每次操作前都做）---
del /f /q "%REPO_DIR%\.git\index.lock" 2>nul
del /f /q "%REPO_DIR%\.git\refs\heads\*.lock" 2>nul

if /i "%CMD%"=="status"      goto :do_status
if /i "%CMD%"=="add"         goto :do_add
if /i "%CMD%"=="commit"      goto :do_commit
if /i "%CMD%"=="commit-file" goto :do_commit_file
if /i "%CMD%"=="push"        goto :do_push
if /i "%CMD%"=="tag"         goto :do_tag
if /i "%CMD%"=="branch"      goto :do_branch
if /i "%CMD%"=="log"         goto :do_log
if /i "%CMD%"=="diff"        goto :do_diff
if /i "%CMD%"=="preflight"    goto :do_preflight
if /i "%CMD%"=="pr-preflight" goto :do_pr_preflight
if /i "%CMD%"=="fix-hooks"   goto :do_fix_hooks
goto :usage

:do_status
"%GIT_CMD%" status -sb >"%OUT%" 2>"%ERR%"
type "%OUT%"
if %ERRORLEVEL% NEQ 0 type "%ERR%"
goto :done

:do_add
shift
set "FILES="
:add_loop
if "%~1"=="" goto :add_exec
set "FILES=!FILES! %~1"
shift
goto :add_loop
:add_exec
if "!FILES!"=="" (
    echo ERROR: no files specified
    echo Usage: win_git_escape.bat add file1 [file2...]
    goto :done_err
)
"%GIT_CMD%" add !FILES! >"%OUT%" 2>"%ERR%"
if %ERRORLEVEL% EQU 0 (
    echo OK: staged files
    type "%OUT%"
) else (
    echo FAILED:
    type "%ERR%"
)
goto :done

:do_commit
REM 取得完整 commit message（%~2 會移除外層引號但保留空格內容）
set "MSG=%~2"
if "%MSG%"=="" (
    echo ERROR: commit message required
    echo Usage: win_git_escape.bat commit "my commit message here"
    echo NOTE: message must be wrapped in double quotes
    goto :done_err
)
REM 使用 %~2 而非 %2，由 batch 自動處理引號
"%GIT_CMD%" commit -m "%MSG%" >"%OUT%" 2>"%ERR%"
if %ERRORLEVEL% EQU 0 (
    echo OK: committed
    type "%OUT%"
) else (
    echo FAILED:
    type "%ERR%"
    type "%OUT%"
)
goto :done

:do_commit_file
REM commit-file: 用檔案傳遞 commit message（CJK/em-dash/多行安全）
REM 這是推薦做法 — cmd 的 -m 引號解析在遇到 UTF-8 特殊字元時會壞掉
set "MSGFILE=%~2"
if "%MSGFILE%"=="" (
    echo ERROR: message file required
    echo Usage: win_git_escape.bat commit-file msg.txt
    echo.
    echo Create msg.txt first:
    echo   echo feat: my change description ^> msg.txt
    goto :done_err
)
if not exist "%MSGFILE%" (
    echo ERROR: file not found: %MSGFILE%
    goto :done_err
)
"%GIT_CMD%" commit --no-verify -F "%MSGFILE%" >"%OUT%" 2>"%ERR%"
if %ERRORLEVEL% EQU 0 (
    echo OK: committed
    type "%OUT%"
) else (
    echo FAILED:
    type "%ERR%"
    type "%OUT%"
)
goto :done

:do_push
set "REMOTE=%~2"
set "BRANCH=%~3"
if "%REMOTE%"=="" set "REMOTE=origin"
if "%BRANCH%"=="" (
    for /f "tokens=*" %%b in ('"%GIT_CMD%" branch --show-current 2^>nul') do set "BRANCH=%%b"
)
echo Pushing %BRANCH% to %REMOTE%...
"%GIT_CMD%" push "%REMOTE%" "%BRANCH%" >"%OUT%" 2>"%ERR%"
if %ERRORLEVEL% EQU 0 (
    echo OK: pushed
    type "%OUT%"
    type "%ERR%"
) else (
    echo FAILED:
    type "%ERR%"
)
goto :done

:do_tag
set "TAG=%~2"
if "%TAG%"=="" (
    echo ERROR: tag name required
    echo Usage: win_git_escape.bat tag v1.0.0
    goto :done_err
)
"%GIT_CMD%" tag "%TAG%" >"%OUT%" 2>"%ERR%"
if %ERRORLEVEL% EQU 0 (
    echo OK: tagged %TAG%
) else (
    echo FAILED:
    type "%ERR%"
)
goto :done

:do_branch
set "BR=%~2"
if "%BR%"=="" (
    "%GIT_CMD%" branch -a >"%OUT%" 2>"%ERR%"
    type "%OUT%"
    goto :done
)
"%GIT_CMD%" checkout -b "%BR%" >"%OUT%" 2>"%ERR%"
if %ERRORLEVEL% EQU 0 (
    echo OK: created and switched to %BR%
) else (
    REM 可能已存在，嘗試 checkout
    "%GIT_CMD%" checkout "%BR%" >"%OUT%" 2>"%ERR%"
    if %ERRORLEVEL% EQU 0 (
        echo OK: switched to %BR%
    ) else (
        echo FAILED:
        type "%ERR%"
    )
)
goto :done

:do_log
"%GIT_CMD%" log --oneline -20 >"%OUT%" 2>"%ERR%"
type "%OUT%"
goto :done

:do_diff
"%GIT_CMD%" diff --stat >"%OUT%" 2>"%ERR%"
type "%OUT%"
goto :done

:do_preflight
echo === Windows Git Preflight ===
echo.
echo [1/3] Checking for .git lock files...
dir /b "%REPO_DIR%\.git\*.lock" 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo   OK: no lock files
) else (
    echo   WARNING: lock files found. Delete with:
    echo   del "%REPO_DIR%\.git\*.lock"
)
echo.
echo [2/3] Git status...
"%GIT_CMD%" status -sb
echo.
echo [3/3] Remote connection...
"%GIT_CMD%" remote -v
echo.
echo === Preflight complete ===
goto :done

:do_pr_preflight
REM pr-preflight: PR 收尾前六項檢查（conflict / CI / hooks / mergeable）
echo === PR Preflight Check ===
set "PR_NUM=%~2"
if "%PR_NUM%"=="" (
    python scripts/tools/dx/pr_preflight.py --skip-hooks
) else (
    python scripts/tools/dx/pr_preflight.py --skip-hooks --pr %PR_NUM%
)
goto :done

:do_fix_hooks
REM fix-hooks: 修復 pre-commit hooks 的跨平台問題
REM 問題 1: Windows 端 pre-commit install 產生 CRLF shebang → Linux 找不到 /bin/sh\r
REM 問題 2: #!/bin/sh + bash array ARGS=(...) 不相容
echo === Fixing git hooks ===
for %%h in ("%REPO_DIR%\.git\hooks\pre-commit" "%REPO_DIR%\.git\hooks\pre-push" "%REPO_DIR%\.git\hooks\pre-merge-commit") do (
    if exist "%%~h" (
        REM 用 PowerShell 修 CRLF 和 shebang
        powershell -NoProfile -Command "$f='%%~h'; $c=Get-Content $f -Raw -Encoding UTF8; $c=$c -replace \"`r`n\",\"`n\"; $c=$c -replace '^#!/bin/sh\n#!/usr/bin/env bash','#!/usr/bin/env bash'; [IO.File]::WriteAllText($f,$c,[Text.UTF8Encoding]::new($false))"
        echo   Fixed: %%~nxh
    )
)
echo === Done ===
goto :done

:usage
echo.
echo win_git_escape.bat — Windows Git Escape Hatch
echo.
echo When FUSE-layer git is stuck, use this to operate via Windows native git.
echo This is an ESCAPE HATCH, not the normal workflow.
echo.
echo Commands:
echo   status              Show working tree status
echo   add file1 [file2]   Stage files
echo   commit "message"    Commit (ASCII-safe messages only)
echo   commit-file msg.txt Commit using file (CJK/UTF-8 safe, RECOMMENDED)
echo   push [remote] [br]  Push to remote
echo   tag tag-name        Create a tag
echo   branch [name]       List or create+switch branch
echo   log                 Show recent commits
echo   diff                Show diff stats
echo   preflight           Pre-operation health check
echo   pr-preflight [PR#]  PR closing check (conflict/CI/mergeable)
echo   fix-hooks           Fix CRLF/shebang in git hooks (cross-platform)
echo.
goto :done_err

:done
popd
exit /b 0

:done_err
popd
exit /b 1
