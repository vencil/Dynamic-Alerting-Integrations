#!/usr/bin/env bash
# dx-run.sh — Linux/macOS entrypoint for Dev Container exec wrapper.
#
# Purpose:
#   Thin shell shim that forwards to scripts/ops/dx_run.py (the actual
#   logic). Exists so users can type a familiar `dx-run.sh` name and so
#   Makefile targets don't embed `python3` path hardcoding.
#
# Usage:
#   scripts/ops/dx-run.sh pytest tests/
#   scripts/ops/dx-run.sh go test ./...
#   scripts/ops/dx-run.sh --status
#   scripts/ops/dx-run.sh --up
#   scripts/ops/dx-run.sh --detach bash /workspaces/vibe-k8s-lab/scripts/long_task.sh
#
# Why a wrapper at all:
#   docker exec's stdout is unreliable under PowerShell/MCP shells. The
#   helper does capture-to-file + tee internally so every call is reliable.
#   See docs/internal/windows-mcp-playbook.md §核心原則.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/dx_run.py" "$@"
