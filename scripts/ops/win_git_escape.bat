@echo off
REM win_git_escape.bat -- Windows Git Escape Hatch
REM
REM When FUSE-layer git is stuck, use Windows native git to finish the job.
REM This is an ESCAPE HATCH, not the normal workflow. Primary path is Dev Container.
REM
REM ============================================================================
REM  MCP PowerShell caller pattern (IMPORTANT -- prevents stdout-hang in MCP)
REM ============================================================================
REM  When invoking from Windows-MCP PowerShell, plain `& this.bat args` or a
REM  naive `Process.Start("cmd.exe", "/c ...")` hangs because the MCP
REM  transport inherits the child's console handle and buffers stdout across
REM  the pipe chain. Dogfooded (PR #44 C5 close-loop):
REM
REM    $bat  = "<tree>\scripts\ops\win_git_escape.bat"
REM    $t    = Join-Path $env:TEMP ("vibe-bat-out-" + [guid]::NewGuid() + ".txt")
REM    $args = '/s /c "' + '"' + $bat + '" push > "' + $t + '" 2>&1"'
REM    $psi = New-Object Diagnostics.ProcessStartInfo
REM    $psi.FileName         = "cmd.exe"
REM    $psi.Arguments        = $args
REM    $psi.UseShellExecute  = $false
REM    $psi.CreateNoWindow   = $true     # CRITICAL -- breaks console inherit
REM    $psi.WorkingDirectory = "<tree>"   # must be inside the tree $bat is in
REM    $p = [Diagnostics.Process]::Start($psi)
REM    [void]$p.WaitForExit(30000)       # WaitForExit(ms) breaks hangs
REM    Get-Content $t -Raw
REM    Remove-Item $t
REM
REM  One file per call (the GUID): two calls sharing a fixed name overwrite
REM  each other's output (#2275).
REM
REM  Three things matter, and the first TWO are not optional:
REM    1) CreateNoWindow = $true     -- without it MCP still inherits the
REM                                     child console handle and the 30s
REM                                     WaitForExit silently becomes a 60s
REM                                     MCP transport timeout.
REM    2) cmd.exe /s /c "..."        -- the /s flag makes cmd strip exactly
REM                                     the outer pair of quotes, no matter
REM                                     how many inner quotes there are.
REM                                     Without /s the triple/quadruple-
REM                                     quote dance is fragile and often
REM                                     launches an empty command (exit=0,
REM                                     0 bytes output -- looks like pass).
REM    3) WaitForExit(ms)            -- gives MCP a process handle to wait
REM                                     on instead of an open pipe.
REM
REM  See windows-mcp-playbook "MCP Shell Pitfalls" / "FUSE Phantom Lock Prevention".
REM ============================================================================
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
REM WARNING: For CJK/em-dash/special chars in commit message, always use commit-file.
REM   The file must be UTF-8. cmd's echo writes the console codepage (cp950 on
REM   zh-TW), so write non-ASCII messages from PowerShell:
REM   [IO.File]::WriteAllText("_msg.txt", $msg, [Text.UTF8Encoding]::new($false))
REM   win_git_escape.bat commit-file _msg.txt
REM
REM Safety:
REM   - Contains no credentials (uses gh auth or ~/.git-credentials)
REM   - Writes no files of its own: git prints straight to stdout (2>&1), so
REM     two calls at once cannot read each other's output (#2275)
REM   - Auto-sets UTF-8 environment

REM Delayed expansion (enabled below) rewrites every `!` in a path, and the
REM rewritten path can name another tree. Refuse such a location first, while
REM `!` is still an ordinary character.
setlocal DisableDelayedExpansion
set "SELF=%~dp0"
if "%SELF:!=%"=="%SELF%" goto :self_ok
echo ERROR: this script's path contains "!", which it cannot work with: "%SELF%"
exit /b 1
:self_ok
setlocal EnableDelayedExpansion

REM --- Environment setup ---
set "PYTHONUTF8=1"
chcp 65001 >nul 2>&1
REM git prints to the console now (no output file), so log/diff/branch would
REM start a pager and wait for a key.
set "GIT_PAGER=cat"

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

REM --- Find Python (for commit_helper.py UTF-8 safety layer) ---
REM Prefer `py` (PEP 397 launcher) over `python`, because on many Windows
REM installs `where python` resolves to the Microsoft Store shim first -- a
REM reparse-point stub that exits 0 without executing the script, so
REM `git commit` silently returns 0 with no commit landed. The `py`
REM launcher always resolves to a real interpreter. Fall back to `python`
REM only if `py` is not installed, then to the usual AppData install paths.
set "PY_CMD="
where py >nul 2>&1 && set "PY_CMD=py"
if "%PY_CMD%"=="" (
    where python >nul 2>&1 && set "PY_CMD=python"
)
if "%PY_CMD%"=="" (
    if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
)
if "%PY_CMD%"=="" (
    if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
)
if "%PY_CMD%"=="" (
    if exist "%LOCALAPPDATA%\Python\bin\python.exe" set "PY_CMD=%LOCALAPPDATA%\Python\bin\python.exe"
)
REM If still unset, commit/commit-file/pr-preflight fail with a clear error below.
REM Non-commit operations (status/add/push/log/diff) don't need python.

REM --- Repo: the work tree this copy lives in (scripts\ops\..\..) ---
pushd "%~dp0..\.."
set "REPO_DIR=%CD%"
popd

REM --- Command dispatch ---
set "CMD=%~1"
if "%CMD%"=="" goto :usage

REM --- Inherited repo-local variables (a git hook exports GIT_DIR and
REM --- GIT_INDEX_FILE) would point the calls below at another repo or index,
REM --- and some of them would also satisfy the tree check. Git lists them.
for /f "delims=" %%v in ('"%GIT_CMD%" rev-parse --local-env-vars') do set "%%v="

REM --- The caller must be inside the tree this copy lives in. Commands run in
REM --- the caller's directory, so relative arguments (add's paths,
REM --- commit-file's message file) resolve the way git resolves them.
REM --- Read with delayed expansion off: it rewrites every `!` in the path, and
REM --- the rewritten name can be another tree (`w!x!` becomes `w`). A location
REM --- with a `!` is refused outright.
setlocal DisableDelayedExpansion
set "CWD_TOP="
for /f "delims=" %%t in ('"%GIT_CMD%" rev-parse --show-toplevel 2^>nul') do set "CWD_TOP=%%t"
REM --- Echo before endlocal: after it delayed expansion is back on and would
REM --- strip the `!` from the message below.
if not defined CWD_TOP (
    "%GIT_CMD%" rev-parse --show-toplevel 2>&1
    echo FAILED: the current directory is not in a git work tree
    endlocal
    goto :done_err
)
if not "%CWD_TOP:!=%"=="%CWD_TOP%" (
    echo FAILED: the current directory's tree path contains "!", which this script cannot work with
    endlocal
    goto :done_err
)
endlocal & set "CWD_TOP=%CWD_TOP%"
set "CWD_TOP=!CWD_TOP:/=\!"
if /i not "!CWD_TOP!"=="!REPO_DIR!" (
    echo FAILED: this copy of the script works on !REPO_DIR!
    echo         but the current directory is in !CWD_TOP!
    echo         Run it from inside that tree, or use the copy in the tree you mean.
    goto :done_err
)

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
"%GIT_CMD%" status -sb 2>&1 || goto :failed
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
"%GIT_CMD%" add !FILES! 2>&1 || goto :failed
echo OK: staged files
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
if "%PY_CMD%"=="" (
    echo ERROR: python not found. Install Python or the `py` launcher, then retry.
    echo Looked in PATH, py launcher, and %%LOCALAPPDATA%%\Programs\Python\*
    goto :done_err
)
"%PY_CMD%" "%~dp0commit_helper.py" check-ascii "%MSG%"
if %ERRORLEVEL% NEQ 0 goto :done_err
REM Use %~2 not %2 -- batch auto-handles quotes
"%GIT_CMD%" commit -m "%MSG%" 2>&1 || goto :failed
echo OK: committed
goto :done

:do_commit_file
REM commit-file: pass commit message via file (CJK/em-dash/multiline safe)
REM This is the RECOMMENDED approach -- cmd -m quoting breaks on UTF-8 specials
set "MSGFILE=%~2"
if "%MSGFILE%"=="" (
    echo ERROR: message file required
    echo Usage: win_git_escape.bat commit-file msg.txt
    echo.
    echo Create msg.txt first, as UTF-8. cmd echo writes the console codepage,
    echo so use it only for an ASCII-only message:
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
if "%PY_CMD%"=="" (
    echo ERROR: python not found. Install Python or the `py` launcher, then retry.
    echo Looked in PATH, py launcher, and %%LOCALAPPDATA%%\Programs\Python\*
    goto :done_err
)
"%PY_CMD%" "%~dp0commit_helper.py" commit-file "%MSGFILE%" 2>&1 || goto :failed
echo OK: committed
goto :done

:do_push
set "REMOTE=%~2"
set "BRANCH=%~3"
if "%REMOTE%"=="" set "REMOTE=origin"
if "%BRANCH%"=="" (
    for /f "tokens=*" %%b in ('"%GIT_CMD%" branch --show-current 2^>nul') do set "BRANCH=%%b"
)
echo Pushing %BRANCH% to %REMOTE%...
REM No --no-verify here (#1487). It is all-or-nothing, and the one guard
REM it would also disarm -- the direct-push-to-main gate, dev-rules #12 --
REM is the only one with no flag of its own. Bypass the other two by name
REM instead. Trap #36 no longer applies: since #1689 the pre-push guards
REM are plain bash, not a pre-commit-generated hook with a Linux python
REM path. Scoped by the setlocal at the top of this file.
set "MKDOCS_STRICT_BYPASS=1"
set "GIT_PREFLIGHT_BYPASS=1"
"%GIT_CMD%" push "%REMOTE%" "%BRANCH%" 2>&1
if errorlevel 1 (
    echo FAILED:
    REM A rejecting guard must reach the caller (#1472 shape): a bare
    REM `goto :done` is `exit /b 0`.
    goto :done_err
)
echo OK: pushed
goto :done

:do_tag
set "TAG=%~2"
if "%TAG%"=="" (
    echo ERROR: tag name required
    echo Usage: win_git_escape.bat tag v1.0.0
    goto :done_err
)
"%GIT_CMD%" tag "%TAG%" 2>&1 || goto :failed
echo OK: tagged %TAG%
goto :done

:do_branch
set "BR=%~2"
if "%BR%"=="" (
    "%GIT_CMD%" branch -a 2>&1 || goto :failed
    goto :done
)
REM switch, not checkout: `checkout <name>` also takes a path and would
REM discard that path's uncommitted changes (`branch .`).
"%GIT_CMD%" show-ref --verify --quiet "refs/heads/%BR%" >nul 2>&1 && goto :branch_switch
"%GIT_CMD%" switch -c "%BR%" 2>&1 || goto :failed
echo OK: created and switched to %BR%
goto :done
:branch_switch
"%GIT_CMD%" switch "%BR%" 2>&1 || goto :failed
echo OK: switched to %BR%
goto :done

:do_log
"%GIT_CMD%" log --oneline -20 2>&1 || goto :failed
goto :done

:do_diff
"%GIT_CMD%" diff --stat 2>&1 || goto :failed
goto :done

:do_preflight
echo === Windows Git Preflight ===
echo.
echo [1/3] Checking for .git lock files...
REM The common git dir holds refs and packed-refs and, under worktrees\, every
REM linked tree's own locks. Listed, never deleted: a lock left by a crashed
REM (e.g. FUSE-side) git and one held by a running git look the same.
for /f "delims=" %%p in ('"%GIT_CMD%" rev-parse --git-common-dir') do set "LOCK_DIR=%%~fp"
dir /s /b "%LOCK_DIR%\*.lock" 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo   OK: no lock files
) else (
    echo   WARNING: lock files above. If no git process is using one, delete it with del.
)
echo.
echo [2/3] Git status...
set "PF_FAIL="
"%GIT_CMD%" status -sb || set "PF_FAIL=1"
echo.
echo [3/3] Remote connection...
"%GIT_CMD%" remote -v
echo.
if defined PF_FAIL (
    echo === Preflight FAILED: see git errors above ===
    goto :done_err
)
echo === Preflight complete ===
goto :done

:do_pr_preflight
REM pr-preflight: PR closing check -- calls pr_preflight.py
echo === PR Preflight Check ===
pushd "%REPO_DIR%"
if "%PY_CMD%"=="" (
    echo ERROR: python not found. Install Python or the `py` launcher, then retry.
    goto :done_err
)
set "PR_ARGS="
if not "%~2"=="" set "PR_ARGS=--pr %~2"
"%PY_CMD%" scripts/tools/dx/pr_preflight.py --skip-hooks %PR_ARGS%
REM Propagate the tool's rc (#1472); a bare `goto :done` is `exit /b 0`.
if %ERRORLEVEL% NEQ 0 goto :done_err
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
echo   diff                Show diff --stat
echo   preflight           Quick 3-point preflight (locks/status/remote)
echo   pr-preflight [N]    PR closing check
echo   fix-hooks           Fix CRLF/shebang issues in .git/hooks/*
echo.
echo Tip: For commit messages with CJK, em-dash, or other non-ASCII, write the
echo file as UTF-8 from PowerShell - cmd echo writes the console codepage:
echo   [IO.File]::WriteAllText^("_msg.txt", $msg, [Text.UTF8Encoding]::new^($false^)^)
echo   win_git_escape.bat commit-file _msg.txt
echo.
REM rc 1, like win_gh.bat: a mistyped or missing subcommand lands here, and
REM rc 0 read as success -- `win_git_escape.bat raw ...` did exactly that (#1920).
goto :done_err

REM --- A git (or commit_helper) call failed: return 1. ---
REM Its reason is already on stdout: each call runs with 2>&1.
REM A bare `goto :done` here is `exit /b 0` -- the #1472 / #1918 shape.
:failed
echo FAILED:
goto :done_err

REM --- Exit label (success): return 0 (endlocal also restores the cwd). ---
REM Without this label cmd.exe returns errorlevel=1 silently, making
REM successful commands look failed.
:done
endlocal
exit /b 0

REM --- Exit label (failure): return 1. ---
:done_err
endlocal
exit /b 1
