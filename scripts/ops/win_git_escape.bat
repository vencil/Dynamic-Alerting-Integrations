@echo off
REM win_git_escape.bat -- Windows Git Escape Hatch
REM
REM When FUSE-layer git is stuck, use Windows native git to finish the job.
REM This is an ESCAPE HATCH, not the normal workflow. Primary path is Dev Container.
REM
REM Usage:
REM   win_git_escape.bat status
REM   win_git_escape.bat add <file1> [file2...]
REM   win_git_escape.bat commit "commit message"
REM   win_git_escape.bat commit-file <msg-file.txt>     (UTF-8/CJK safe)
REM   win_git_escape.bat push [remote] [branch]
REM   win_git_escape.bat tag <tag-name>
REM   win_git_escape.bat branch <branch-name>
REM   win_git_escape.bat log
REM   win_git_escape.bat diff
REM   win_git_escape.bat preflight
REM   win_git_escape.bat fix-hooks                       (fix CRLF hooks)
REM
REM WARNING: For CJK/em-dash/special chars in commit message, always use commit-file:
REM   echo feat: my message > _msg.txt
REM   win_git_escape.bat commit-file _msg.txt
REM
REM Safety:
REM   - Contains no credentials (uses gh auth or ~/.git-credentials)
REM   - Output redirected to %TEMP%\vibe-git-*.txt
REM   - Auto-sets UTF-8 environment

setlocal enabledelayedexpansion

REM --- Environment setup ---
set "PYTHONUTF8=1"
chcp 65001 >nul 2>&1

REM --- PATHEXT guard: some user profiles have PATHEXT=.CPL only (missing .EXE etc.),
REM --- which breaks cmd.exe's extension-less command resolution (e.g. `git`, `where`).
REM --- Force a sane PATHEXT so subprocess calls work reliably.
set "PATHEXT=.COM;.EXE;.BAT;.CMD;.VBS;.VBE;.JS;.JSE;.WSF;.WSH;.MSC"

REM --- Find Git ---
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

REM --- Find Repo ---
set "REPO_DIR="
if exist "%~dp0..\..\..\.git" (
    REM Navigate from scripts\ops\ up to repo root
    pushd "%~dp0..\.."
    set "REPO_DIR=!CD!"
    popd
) else (
    REM fallback: current directory
    set "REPO_DIR=%CD%"
)

REM --- Output files ---
set "OUT=%TEMP%\vibe-git-out.txt"
set "ERR=%TEMP%\vibe-git-err.txt"

REM --- Command dispatch ---
set "CMD=%~1"
if "%CMD%"=="" goto :usage

pushd "%REPO_DIR%"

REM --- Auto-clean phantom locks (run before every operation) ---
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
REM Get full commit message (%~2 strips outer quotes but keeps spaces)
set "MSG=%~2"
if "%MSG%"=="" (
    echo ERROR: commit message required
    echo Usage: win_git_escape.bat commit "my commit message here"
    echo NOTE: message must be wrapped in double quotes
    goto :done_err
)
REM UTF-8 safety gate (PR #42 Trap #58): reject non-ASCII in -m args, since
REM cmd.exe corrupts them regardless of chcp. Helper prints hint + exits 1.
python "%~dp0commit_helper.py" check-ascii "%MSG%"
if %ERRORLEVEL% NEQ 0 goto :done_err
REM Use %~2 not %2 -- batch auto-handles quotes
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
REM commit-file: pass commit message via file (CJK/em-dash/multiline safe)
REM This is the RECOMMENDED approach -- cmd -m quoting breaks on UTF-8 specials
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
REM UTF-8 safety (PR #42 Trap #58): pipe bytes to `git commit -F -` via Python,
REM since `git commit -F file` reads via Windows codepage and mangles CJK bytes
REM even with chcp 65001 set. The helper does the raw bytes pipe.
python "%~dp0commit_helper.py" commit-file "%MSGFILE%" >"%OUT%" 2>"%ERR%"
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
REM --no-verify: pre-push hook has hardcoded Linux python path (Trap #36)
"%GIT_CMD%" push --no-verify "%REMOTE%" "%BRANCH%" >"%OUT%" 2>"%ERR%"
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
    REM Branch may already exist -- try plain checkout
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
REM pr-preflight: 6-point PR closing check (conflict/CI/hooks/mergeable)
echo === PR Preflight Check ===
set "PR_NUM=%~2"
if "%PR_NUM%"=="" (
    python scripts/tools/dx/pr_preflight.py --skip-hooks
) else (
    python scripts/tools/dx/pr_preflight.py --skip-hooks --pr %PR_NUM%
)
goto :done

:do_fix_hooks
REM fix-hooks: Fix cross-platform issues in pre-commit hooks
REM Problem 1: Windows pre-commit install generates CRLF shebang -> Linux can't find /bin/sh\r
REM Problem 2: #!/bin/sh + bash array ARGS=(...) are incompatible
echo === Fixing git hooks ===
for %%h in ("%REPO_DIR%\.git\hooks\pre-commit" "%REPO_DIR%\.git\hooks\pre-push" "%REPO_DIR%\.git\hooks\pre-merge-commit") do (
    if exist "%%~h" (
        REM Use PowerShell to fix CRLF and shebang
        powershell -NoProfile -Command "$f='%%~h'; $c=Get-Content $f -Raw -Encoding UTF8; $c=$c -replace \"`r`n\",\"`n\"; $c=$c -replace '^#!/bin/sh\n#!/usr/bin/env bash','#!/usr/bin/env bash'; [IO.File]::WriteAllText($f,$c,[Text.UTF8Encoding]::new($false))"
        echo   Fixed: %%~nxh
    )
)
echo === Done ===
goto :done

:usage
echo.
echo win_git_escape.bat -- Windows Git Escape Hatch
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
echo   