@echo off
REM dx-run.bat -- Windows entrypoint for Dev Container exec wrapper.
REM
REM Why this exists:
REM   docker exec stdout is swallowed under PowerShell/MCP shells.
REM   dx_run.py (Python) captures to file + tees back reliably.
REM   See docs\internal\windows-mcp-playbook.md "Core Principle" section.
REM
REM Usage:
REM   scripts\ops\dx-run.bat pytest tests\
REM   scripts\ops\dx-run.bat go test ./...
REM   scripts\ops\dx-run.bat --status
REM   scripts\ops\dx-run.bat --up
REM   scripts\ops\dx-run.bat --detach bash /workspaces/vibe-k8s-lab/scripts/long.sh
REM
REM Rules (see docs\internal\windows-mcp-playbook.md #mcp-shell-pitfalls):
REM   * ASCII only
REM   * CRLF line endings
REM   * set PATHEXT so cmd.exe can resolve python.exe

set "PATHEXT=.COM;.EXE;.BAT;.CMD;.VBS;.VBE;.JS;.JSE;.WSF;.WSH;.MSC"
python "%~dp0dx_run.py" %*
exit /b %ERRORLEVEL%
