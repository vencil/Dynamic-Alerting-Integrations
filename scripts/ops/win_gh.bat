@echo off
REM win_gh.bat -- Windows GitHub CLI wrapper for MCP (Desktop Commander) sessions.
REM
REM Why this exists:
REM   Desktop Commander's default PowerShell shell mangles paths of the form
REM   "C:\Program Files\GitHub CLI\gh.exe" (the space + quoting round-trip
REM   breaks). The fix is to (a) always invoke cmd.exe explicitly, (b) use
REM   the 8.3 short path (C:\PROGRA~1\GITHUB~1\gh.exe), and (c) keep that
REM   logic in ONE place instead of letting each session reinvent it.
REM
REM Usage:
REM   win_gh.bat pr-checks [PR#]          List CI checks (pass/fail/pending)
REM   win_gh.bat pr-view [PR#]            Show PR summary
REM   win_gh.bat pr-create --title ... --body-file ... --head ...
REM                                       Create PR (forwards flags to gh)
REM   win_gh.bat run-view <RUN_ID>        Show CI run summary
REM   win_gh.bat run-log <RUN_ID>         Show failed-step logs
REM   win_gh.bat raw <args...>            Escape hatch: `gh <args>` verbatim
REM
REM DO NOT write _pr_checks.bat / _pr_log.bat / etc. — extend this wrapper.
REM
REM See docs/internal/windows-mcp-playbook.md (§MCP Shell Pitfalls, §修復層 C).

setlocal enabledelayedexpansion

REM --- Environment setup (mirrors win_git_escape.bat) ---
set "PYTHONUTF8=1"
chcp 65001 >nul 2>&1

REM --- Find gh.exe ---
REM 8.3 short path avoids quoting issues when invoked via MCP/PowerShell.
REM See §MCP Shell Pitfalls for why this matters.
set "GH_CMD="
if exist "C:\Program Files\GitHub CLI\gh.exe" (
    set "GH_CMD=C:\PROGRA~1\GITHUB~1\gh.exe"
)
if "%GH_CMD%"=="" (
    where gh >nul 2>&1 && set "GH_CMD=gh"
)
if "%GH_CMD%"=="" (
    echo ERROR: gh.exe not found. Install GitHub CLI from https://cli.github.com/
    exit /b 1
)

REM --- gh needs git on PATH to resolve repo context ---
set "PATH=C:\Program Files\Git\cmd;C:\Program Files\Git\bin;%PATH%"

REM --- PATHEXT guard: some user profiles have PATHEXT=.CPL only (missing .EXE etc.)
REM --- which makes cmd.exe unable to resolve "git" even when folder is on PATH.
REM --- Force a sane PATHEXT so gh's internal `git` shell-out works.
set "PATHEXT=.COM;.EXE;.BAT;.CMD;.VBS;.VBE;.JS;.JSE;.WSF;.WSH;.MSC"

REM --- Find repo ---
set "REPO_DIR="
if exist "%~dp0..\..\.git" (
    pushd "%~dp0..\.."
    set "REPO_DIR=!CD!"
    popd
) else (
    set "REPO_DIR=%CD%"
)
pushd "%REPO_DIR%"

REM --- Dispatch ---
set "CMD=%~1"
if "%CMD%"=="" goto :usage

if /i "%CMD%"=="pr-checks"  goto :do_pr_checks
if /i "%CMD%"=="pr-view"    goto :do_pr_view
if /i "%CMD%"=="pr-create"  goto :do_pr_create
if /i "%CMD%"=="run-view"   goto :do_run_view
if /i "%CMD%"=="run-log"    goto :do_run_log
if /i "%CMD%"=="raw"        goto :do_raw
goto :usage

:do_pr_checks
set "PR=%~2"
if "%PR%"=="" (
    "%GH_CMD%" pr checks
) else (
    "%GH_CMD%" pr checks %PR%
)
goto :done

:do_pr_view
set "PR=%~2"
if "%PR%"=="" (
    "%GH_CMD%" pr view
) else (
    "%GH_CMD%" pr view %PR%
)
goto :done

:do_pr_create
REM Forward all remaining args verbatim to gh pr create.
shift
set "ARGS="
:pr_create_loop
if "%~1"=="" goto :pr_create_exec
set "ARGS=!ARGS! "%~1""
shift
goto :pr_create_loop
:pr_create_exec
"%GH_CMD%" pr create !ARGS!
goto :done

:do_run_view
set "RUN=%~2"
if "%RUN%"=="" (
    echo ERROR: run ID required
    echo Usage: win_gh.bat run-view 12345678
    goto :done_err
)
"%GH_CMD%" run view %RUN%
goto :done

:do_run_log
set "RUN=%~2"
if "%RUN%"=="" (
    echo ERROR: run ID required
    echo Usage: win_gh.bat run-log 12345678
    goto :done_err
)
"%GH_CMD%" run view %RUN% --log-failed
goto :done

:do_raw
REM Escape hatch for uncovered subcommands.
shift
set "ARGS="
:raw_loop
if "%~1"=="" goto :raw_exec
set "ARGS=!ARGS! "%~1""
shift
goto :raw_loop
:raw_exec
"%GH_CMD%" !ARGS!
goto :done

:usage
echo.
echo win_gh.bat -- Windows GitHub CLI wrapper
echo.
echo Subcommands:
echo   pr-checks [PR#]                   List CI checks
echo   pr-view [PR#]                     Show PR summary
echo   pr-create ^<flags^>                 Create PR (forwards to gh pr create)
echo   run-view ^<RUN_ID^>                 Show CI run summary
echo   run-log ^<RUN_ID^>                  Show failed-step logs
echo   raw ^<args^>                        Escape hatch: gh ^<args^> verbatim
echo.
echo Do NOT write _pr_checks.bat / _gh.bat / etc. Extend this wrapper.
echo.
goto :done_err

:done
popd
exit /b 0

:done_err
popd
exit /b 1
