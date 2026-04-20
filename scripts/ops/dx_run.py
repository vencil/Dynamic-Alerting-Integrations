#!/usr/bin/env python3
"""dx_run.py — Dev Container exec wrapper for Cowork/Windows MCP.

Motivation:
  docker exec's stdout is unreliable under PowerShell/MCP shells (the
  "docker exec stdout is empty" trap — see windows-mcp-playbook §核心原則).
  The only reliable pattern is:

      docker exec <c> bash -c "<cmd> > /workspaces/...<file> 2>&1"
      # then cat the file back

  This wrapper codifies that pattern so ad-hoc sessions don't forget to
  redirect. Every invocation goes through capture-to-file + tee-to-stdout.

Modes (default: --run):
  --run   <cmd...>   Default. Exec <cmd> in container, capture to
                     /workspaces/vibe-k8s-lab/_dx_out.txt, tee to stdout,
                     return container exit code.
  --detach <cmd...>  `docker exec -d` for long-running ops. The wrapper
                     writes a small shell script that runs <cmd> with
                     `exec > file 2>&1` (required — `-d` drops stdout).
                     Prints the output file path; caller reads it later.
  --status           Print container name + running state.
  --up               Start container if stopped. Idempotent.

Container + workspace are configurable via env:
  DX_CONTAINER   — default: vibe-dev-container
  DX_WORKSPACE   — default: /workspaces/vibe-k8s-lab

Called from scripts/ops/dx-run.{sh,bat} and Makefile dc-* targets.
"""
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_CONTAINER = "vibe-dev-container"
DEFAULT_WORKSPACE = "/workspaces/vibe-k8s-lab"
OUT_FILE = "_dx_out.txt"


def _container() -> str:
    return os.environ.get("DX_CONTAINER", DEFAULT_CONTAINER)


def _workspace() -> str:
    return os.environ.get("DX_WORKSPACE", DEFAULT_WORKSPACE)


def _docker_exists() -> bool:
    try:
        r = subprocess.run(
            ["docker", "version", "--format", "{{.Client.Version}}"],
            capture_output=True, check=False, timeout=10,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _container_running(name: str) -> bool:
    r = subprocess.run(
        ["docker", "ps", "--filter", f"name=^{name}$", "--format", "{{.Names}}"],
        capture_output=True, text=True, check=False,
    )
    return name in r.stdout.split()


def _container_exists(name: str) -> bool:
    r = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Names}}"],
        capture_output=True, text=True, check=False,
    )
    return name in r.stdout.split()


def cmd_status() -> int:
    name = _container()
    if not _docker_exists():
        print(f"docker: not available on PATH", file=sys.stderr)
        return 1
    if not _container_exists(name):
        print(f"{name}: container does not exist (run scripts/dev_container_setup.sh)")
        return 2
    if _container_running(name):
        print(f"{name}: running")
        return 0
    print(f"{name}: stopped (run: scripts/ops/dx-run.sh --up)")
    return 3


def cmd_up() -> int:
    name = _container()
    if not _docker_exists():
        print("docker: not available on PATH", file=sys.stderr)
        return 1
    if not _container_exists(name):
        print(f"{name}: container does not exist — create it first", file=sys.stderr)
        return 2
    if _container_running(name):
        print(f"{name}: already running")
        return 0
    r = subprocess.run(["docker", "start", name], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"{name}: failed to start — {r.stderr.strip()}", file=sys.stderr)
        return r.returncode
    # Brief sanity wait for exec readiness.
    time.sleep(1)
    print(f"{name}: started")
    return 0


def cmd_run(cmd: list[str], detach: bool = False) -> int:
    """Exec <cmd> in container with stdout captured to _dx_out.txt."""
    if not cmd:
        print("dx-run: no command provided", file=sys.stderr)
        return 2
    name = _container()
    ws = _workspace()
    if not _docker_exists():
        print("docker: not available on PATH", file=sys.stderr)
        return 1
    if not _container_running(name):
        print(
            f"{name}: not running. Start with: scripts/ops/dx-run.sh --up",
            file=sys.stderr,
        )
        return 3

    # Build the in-container shell command: run user cmd, capture to
    # workspace file so host can cat it even when docker exec drops stdout.
    out_path_in_container = f"{ws}/{OUT_FILE}"
    quoted = " ".join(shlex.quote(a) for a in cmd)
    if detach:
        # -d mode: script must self-redirect (the `-d` flag discards stdout).
        inner = f"exec > {shlex.quote(out_path_in_container)} 2>&1; {quoted}"
        args = ["docker", "exec", "-d", "-w", ws, name, "bash", "-c", inner]
        subprocess.run(args, check=False)
        print(f"[detached] output will be at (host path): {OUT_FILE} inside workspace")
        return 0

    inner = f"{quoted} > {shlex.quote(out_path_in_container)} 2>&1; echo $? > {shlex.quote(out_path_in_container + '.rc')}"
    args = ["docker", "exec", "-w", ws, name, "bash", "-c", inner]
    # We ignore docker exec's stdout here; the real output is in the file.
    rc_exec = subprocess.run(args, check=False).returncode
    if rc_exec != 0:
        # docker exec itself failed (not the user command).
        print(f"docker exec failed with rc={rc_exec}", file=sys.stderr)
        return rc_exec

    # Read captured output back via `docker exec cat`.
    cat = subprocess.run(
        ["docker", "exec", name, "cat", out_path_in_container],
        capture_output=True, check=False,
    )
    sys.stdout.buffer.write(cat.stdout)
    sys.stdout.flush()
    rc_cat = subprocess.run(
        ["docker", "exec", name, "cat", out_path_in_container + ".rc"],
        capture_output=True, text=True, check=False,
    )
    try:
        return int(rc_cat.stdout.strip() or "0")
    except ValueError:
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dx-run",
        description="Dev Container exec wrapper (stdout-capture safe).",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--status", action="store_true", help="Show container status.")
    mode.add_argument("--up", action="store_true", help="Start container if stopped.")
    mode.add_argument("--detach", action="store_true", help="docker exec -d mode.")
    parser.add_argument(
        "cmd", nargs=argparse.REMAINDER,
        help="Command to run inside container (prefix with -- to disambiguate).",
    )
    args = parser.parse_args(argv)

    if args.status:
        return cmd_status()
    if args.up:
        return cmd_up()

    cmd = list(args.cmd)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    return cmd_run(cmd, detach=args.detach)


if __name__ == "__main__":
    sys.exit(main())
