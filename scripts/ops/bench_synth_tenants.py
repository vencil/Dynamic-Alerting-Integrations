#!/usr/bin/env python3
"""bench_synth_tenants.py — inject / remove `benchmark.sh --under-load` synthetic tenants.

One key per tenant, `synth-NNNN.yaml`, holding `tenants: {synth-NNNN: {...}}`.

Refused before anything is written: a `synth-NNNN.yaml` key already exists;
any carrier already declares a `synth-*` tenant
(`patch_config.tenant_declarations`); a carrier cannot be read.
⚠️ Not refused: an emptied `thresholds.yaml: "tenants: {}"` declares nothing.

Cleanup removes exactly the keys this run injected (JSON merge patch → null),
re-reads the ConfigMap and fails if any of them is still there.

Usage (from benchmark.sh):
  bench_synth_tenants.py inject  --tenants N --state-file F
  bench_synth_tenants.py cleanup --state-file F
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

_THIS_DIR = Path(__file__).resolve().parent
_TOOLS = _THIS_DIR.parent / "tools"
sys.path.insert(0, str(_TOOLS))
sys.path.insert(0, str(_TOOLS / "ops"))
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
import patch_config  # noqa: E402

NAMESPACE = "monitoring"
CONFIGMAP = "threshold-config"
SYNTH_PREFIX = "synth-"
MAX_TENANTS = 2000
_KUBECTL_TIMEOUT_S = 120


def synth_tenant_name(i: int) -> str:
    return f"{SYNTH_PREFIX}{i:04d}"


def synth_thresholds(i: int) -> dict[str, str]:
    """Per-tenant thresholds; values spread so tenants are not identical."""
    return {
        "mysql_connections": str(50 + i % 100),
        "mysql_threads_running": str(20 + i % 40),
        "container_cpu": str(70 + i % 30),
        "container_memory": str(75 + i % 20),
    }


def build_synth_carriers(n: int) -> dict[str, str]:
    """`synth-NNNN.yaml` → YAML text declaring exactly that one tenant."""
    carriers = {}
    for i in range(n):
        name = synth_tenant_name(i)
        carriers[f"{name}.yaml"] = yaml.safe_dump(
            {"tenants": {name: synth_thresholds(i)}},
            default_flow_style=False, sort_keys=False)
    return carriers


def injection_conflicts(cm_data: dict, keys: list[str]) -> list[str]:
    """Why injecting `keys` into this ConfigMap would be unsafe; [] if it is not.

    Raises `patch_config.ConfigMapShapeError` when a carrier cannot be read.
    """
    data = cm_data.get("data") or {}
    problems = [f"key {k} already exists" for k in sorted(set(keys) & set(data))]
    declared = patch_config.tenant_declarations(cm_data)
    for tenant in sorted(t for t in declared if t.startswith(SYNTH_PREFIX)):
        problems.append(
            f"tenant {tenant} is already declared by "
            f"{', '.join(sorted(declared[tenant]))}")
    return problems


def cleanup_patch(keys: list[str]) -> dict:
    """JSON merge patch that deletes `keys` (null removes a map member)."""
    return {"data": {k: None for k in keys}}


# ── kubectl ─────────────────────────────────────────────────────────────


def _kubectl(args: list[str]) -> str:
    proc = subprocess.run(["kubectl", *args], capture_output=True, text=True,
                          encoding="utf-8", timeout=_KUBECTL_TIMEOUT_S)
    if proc.returncode != 0:
        raise RuntimeError(
            f"kubectl {' '.join(args)} failed (rc={proc.returncode}): "
            f"{proc.stderr.strip()}")
    return proc.stdout


def read_configmap() -> dict:
    return json.loads(_kubectl(["get", "configmap", CONFIGMAP, "-n", NAMESPACE,
                                "-o", "json"]))


def apply_merge_patch(patch: dict) -> None:
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(patch, fh)
        _kubectl(["patch", "configmap", CONFIGMAP, "-n", NAMESPACE,
                  "--type", "merge", "--patch-file", path])
    finally:
        os.remove(path)


def cmd_inject(n: int, state_file: Path) -> int:
    carriers = build_synth_carriers(n)
    keys = sorted(carriers)
    try:
        problems = injection_conflicts(read_configmap(), keys)
    except patch_config.ConfigMapShapeError as exc:
        print(f"ERROR: refusing to inject synthetic tenants: {exc}",
              file=sys.stderr)
        return EXIT_CALLER_ERROR
    if problems:
        print("ERROR: refusing to inject synthetic tenants into "
              f"{NAMESPACE}/{CONFIGMAP}; a previous run may have left them "
              "behind:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print("Once each key named above holds nothing but synth-* tenants, "
              f"delete it: kubectl patch configmap {CONFIGMAP} -n {NAMESPACE} "
              "--type merge -p '{\"data\":{\"<key>\":null}}'", file=sys.stderr)
        return EXIT_VIOLATION
    # The state file is written BEFORE the patch, so a cleanup trap still
    # knows which keys to remove if the patch lands and this process dies.
    state_file.write_text("\n".join(keys) + "\n", encoding="utf-8", newline="\n")
    apply_merge_patch({"data": carriers})
    print(f"Injected {len(keys)} synthetic tenants, one key each "
          f"({keys[0]} .. {keys[-1]})")
    return EXIT_OK


def cmd_cleanup(state_file: Path) -> int:
    keys = [k for k in state_file.read_text(encoding="utf-8").split() if k]
    if not keys:
        print("No synthetic tenant keys recorded; nothing to remove.")
        return EXIT_OK
    apply_merge_patch(cleanup_patch(keys))
    left = sorted(set(keys) & set(read_configmap().get("data") or {}))
    if left:
        print(f"ERROR: {len(left)} synthetic tenant key(s) are still in "
              f"{NAMESPACE}/{CONFIGMAP} after cleanup: {', '.join(left[:10])}"
              f"{' …' if len(left) > 10 else ''}", file=sys.stderr)
        return EXIT_VIOLATION
    print(f"Removed {len(keys)} synthetic tenant keys")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inject / remove benchmark synthetic tenants in "
                    "threshold-config (one key per tenant).")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_inj = sub.add_parser("inject", help="add N synth-NNNN.yaml keys")
    p_inj.add_argument("--tenants", type=int, required=True)
    p_inj.add_argument("--state-file", type=Path, required=True,
                       help="where to record the injected keys")
    p_cln = sub.add_parser("cleanup", help="remove the recorded keys")
    p_cln.add_argument("--state-file", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        if args.cmd == "inject":
            if not 1 <= args.tenants <= MAX_TENANTS:
                print(f"ERROR: --tenants must be 1..{MAX_TENANTS}",
                      file=sys.stderr)
                return EXIT_CALLER_ERROR
            return cmd_inject(args.tenants, args.state_file)
        return cmd_cleanup(args.state_file)
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR


if __name__ == "__main__":
    sys.exit(main())
