#!/usr/bin/env python3
"""Render the store side of the victorialogs E2E from the REAL helm/victorialogs chart (#1295).

The victorialogs stack puts a real vmauth between the gateway and the mock log
store, configured exactly as the chart configures it in the pod: the same
image, the same args, the same auth.yml — with `auth.mode: enforce` and a
Secret named for every registered consumer, so a consumer that does not
authenticate gets 401 instead of slipping through observe mode.

Writes into <out>/ (rendered/victorialogs/):
  vmauth/auth.yml        the chart's ConfigMap key, verbatim
  vmauth.compose.yml     a compose override for the `vmauth` service: image,
                         args and the password env vars, all from the
                         rendered Deployment (no second pin to drift), and
                         the host port the driver uses
  consumers.json         {name: {"password", "paths"}} for the pytest driver
  gateway-password       the federation-gateway consumer's password (no
                         newline), mounted where the gateway chart expects it
Prints `<vmauth listen port> <backend port>` for the runner.

Passwords are random hex per run (the format the chart documents). Nothing
here is a secret worth keeping: the stack is torn down after the run.
"""
from __future__ import annotations

import json
import secrets
import subprocess
import sys
from pathlib import Path

import yaml


def _flag(args: list[str], name: str) -> str:
    hits = [a.split("=", 1)[1] for a in args if a.startswith(f"-{name}=")]
    if len(hits) != 1:
        raise SystemExit(f"render_vmauth: expected one -{name}= in vmauth args, got {args}")
    return hits[0]


def main(chart: Path, out: Path) -> None:
    values = yaml.safe_load((chart / "values.yaml").read_text(encoding="utf-8"))
    consumers = values["auth"]["consumers"]
    # Secret names are only references here; the env vars carry the values.
    override = {"auth": {"mode": "enforce", "existingSecrets": {
        c["name"]: f"e2e-{c['name']}" for c in consumers}}}
    override_file = out / "victorialogs-values.yaml"
    override_file.write_text(yaml.safe_dump(override), encoding="utf-8")
    rendered = subprocess.run(
        ["helm", "template", "e2e", str(chart), "-n", "monitoring", "-f", str(override_file)],
        capture_output=True, text=True, encoding="utf-8", timeout=120, check=True).stdout
    docs = [d for d in yaml.safe_load_all(rendered) if d]
    cm = next(d for d in docs if d["kind"] == "ConfigMap" and "auth.yml" in d.get("data", {}))
    pod = next(d for d in docs if d["kind"] == "Deployment")["spec"]["template"]["spec"]
    vmauth = next(c for c in pod["containers"] if c["name"] == "vmauth")

    args = vmauth["args"]
    config_path = _flag(args, "auth.config")
    listen_port = _flag(args, "httpListenAddr").rsplit(":", 1)[1]

    passwords = {c["name"]: secrets.token_hex(32) for c in consumers}
    by_secret = {f"e2e-{c['name']}": c["name"] for c in consumers}
    env = {}
    for e in vmauth.get("env") or []:
        ref = e["valueFrom"]["secretKeyRef"]
        env[e["name"]] = passwords[by_secret[ref["name"]]]
    if len(env) != len(consumers):
        raise SystemExit(f"render_vmauth: {len(consumers)} consumers but {len(env)} password env vars")

    (out / "vmauth").mkdir(parents=True, exist_ok=True)
    (out / "vmauth" / "auth.yml").write_text(cm["data"]["auth.yml"], encoding="utf-8")
    compose = {"services": {"vmauth": {
        "image": vmauth["image"],
        "command": args,
        "environment": env,
        "volumes": [f"./rendered/victorialogs/vmauth/auth.yml:{config_path}:ro"],
        # Published for the consumer scenarios (the driver talks to vmauth
        # directly, as each registered consumer).
        "ports": [f"${{E2E_VL_VMAUTH_PORT:-18428}}:{listen_port}"],
    }}}
    (out / "vmauth.compose.yml").write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")
    (out / "consumers.json").write_text(json.dumps(
        {c["name"]: {"password": passwords[c["name"]], "paths": c["paths"]} for c in consumers},
        indent=2), encoding="utf-8")
    (out / "gateway-password").write_text(passwords["federation-gateway"], encoding="utf-8")
    print(listen_port, values["auth"]["backendPort"])


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
