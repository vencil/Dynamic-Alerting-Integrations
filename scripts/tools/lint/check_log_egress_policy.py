#!/usr/bin/env python3
"""check_log_egress_policy.py — #566 batch D (T4-1/T4-2) egress allowlist gate.

The log-aggregation charts (helm/vector especially) expose two operator-
controllable knobs that, on the GitOps path, are data-exfiltration
primitives:

  1. additionalSinks[].{endpoints,uri} — a sink pointing at an attacker
     URL silently fans out every audit row (tenant_id / token_id /
     query / cost stats).
  2. extraEnv — can repoint Vector's behavior (VECTOR_* reserved vars)
     or hardcode/override a sink credential.

This lint renders the chart(s) and validates the rendered output against
an egress allowlist + an env-override blacklist. It is the *GitOps-path*
control: it catches a malicious values change at PR time. It does NOT
stop someone with direct `kubectl`/`helm --set` cluster access — that is
a RBAC / GitOps-only-write-boundary concern, documented in
docs/internal/platform-log-aggregation-runbook.md §7.5.

The policy is mirrored as illustrative rego in
policies/examples/log-egress.rego (the seam for a future OPA Gatekeeper
runtime control — see runbook §7.5); THIS Python lint is the live gate,
consistent with the repo's other check_*.py gates.

Data flow (per the multi-document YAML caveat):
  helm template <chart> [-f values]
    → yaml.safe_load_all(...)        # multi-doc stream
    → filter None (blank docs)
    → iterate manifests
    → for the Vector ConfigMap, ALSO parse the embedded vector.yaml
      string so sink endpoints are structured, not regex-scraped.

Exit codes:
  0  no violations (or only warnings in non-strict mode)
  1  one or more ERROR-level violations
  2  tooling failure (helm not found, render error, bad args)
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import shutil
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))  # Repo subdir layout
from _lib_exitcodes import EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from typing import Iterable, Optional

import yaml

# Charts whose rendered output carries egress-relevant config.
DEFAULT_CHARTS = ("helm/vector", "helm/victorialogs", "helm/chargeback-aggregator")

# Default egress allowlist — in-cluster destinations that are always OK.
# `*.svc` / `*.svc.cluster.local` cover ClusterIP services (VictoriaLogs
# itself). Operators extend with their SIEM host(s) via --allow-host.
DEFAULT_ALLOWED_HOST_GLOBS = (
    "*.svc",
    "*.svc.cluster.local",
    "localhost",
    "127.0.0.1",
)

# Env var name globs that must NEVER be overridden by extraEnv — these
# steer Vector's own behavior; an attacker repointing them hijacks the
# pipeline (e.g. VECTOR_SELF_NODE_NAME scoping, or any future
# VECTOR_-prefixed config).
RESERVED_ENV_GLOBS = ("VECTOR_*",)

# Env var name globs that are sensitive: allowed ONLY via valueFrom
# (secretKeyRef / configMapKeyRef), never as a literal `value:` (which
# would hardcode a secret into the manifest or let an attacker substitute
# a credential they control).
SENSITIVE_ENV_GLOBS = ("*TOKEN*", "*KEY*", "*SECRET*", "*PASSWORD*", "*CREDENTIAL*")


@dataclass
class Violation:
    level: str  # ERROR | WARNING
    chart: str
    where: str
    message: str


def _host_of(url: str) -> Optional[str]:
    """Extract hostname from a URL or host[:port] string. Returns None
    if unparseable (which is itself a violation — fail closed)."""
    if not url:
        return None
    candidate = url if "://" in url else f"//{url}"
    try:
        netloc = urllib.parse.urlsplit(candidate).netloc or urllib.parse.urlsplit(candidate).path
    except ValueError:
        return None
    # Strip credentials + port.
    netloc = netloc.rsplit("@", 1)[-1]
    host = netloc.split(":", 1)[0].strip()
    return host or None


def _host_allowed(host: Optional[str], allow_globs: Iterable[str]) -> bool:
    if host is None:
        return False
    return any(fnmatch.fnmatch(host, g) for g in allow_globs)


def _matches_any(name: str, globs: Iterable[str]) -> bool:
    up = name.upper()
    return any(fnmatch.fnmatch(up, g.upper()) for g in globs)


def render_chart(chart_dir: Path, values_files: list[Path], sets: list[str]) -> list[dict]:
    """helm template the chart; return parsed manifests (None-filtered)."""
    helm = shutil.which("helm")
    if helm is None:
        print("ERROR: helm not on PATH — cannot render charts", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)
    cmd = [helm, "template", "egress-lint", str(chart_dir), "-n", "monitoring"]
    for vf in values_files:
        cmd += ["-f", str(vf)]
    for s in sets:
        cmd += ["--set", s]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=60)
    except subprocess.CalledProcessError as e:
        print(f"ERROR: helm template {chart_dir} failed:\n{e.stderr}", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def _iter_sink_endpoints(vector_config: dict):
    """Yield (sink_name, url) for every endpoint/uri across all sinks in
    a parsed vector.yaml."""
    for sink_name, sink in (vector_config.get("sinks") or {}).items():
        if not isinstance(sink, dict):
            continue
        # elasticsearch/http sinks use `endpoints` (list) or `endpoint`/`uri`.
        for key in ("endpoints",):
            for url in sink.get(key, []) or []:
                yield sink_name, url
        for key in ("endpoint", "uri"):
            if sink.get(key):
                yield sink_name, sink[key]


def _iter_container_envs(manifest: dict):
    """Yield (container_name, env_entry) for every env entry across pod
    templates in a Deployment / DaemonSet / CronJob / Job."""
    kind = manifest.get("kind")
    spec = manifest.get("spec", {})
    if kind == "CronJob":
        pod_spec = spec.get("jobTemplate", {}).get("spec", {}).get("template", {}).get("spec", {})
    elif kind in ("Deployment", "DaemonSet", "Job", "StatefulSet"):
        pod_spec = spec.get("template", {}).get("spec", {})
    else:
        return
    for c in pod_spec.get("containers", []) or []:
        for e in c.get("env", []) or []:
            yield c.get("name", "?"), e


def lint_chart(chart_dir: Path, manifests: list[dict], allow_globs: list[str]) -> list[Violation]:
    chart = chart_dir.name
    vios: list[Violation] = []

    for m in manifests:
        # ── (1) sink egress allowlist ──────────────────────────────────
        if m.get("kind") == "ConfigMap" and "vector.yaml" in (m.get("data") or {}):
            try:
                vcfg = yaml.safe_load(m["data"]["vector.yaml"]) or {}
            except yaml.YAMLError as e:
                vios.append(Violation("ERROR", chart, "vector.yaml", f"embedded vector config unparseable: {e}"))
                continue
            for sink_name, url in _iter_sink_endpoints(vcfg):
                host = _host_of(url)
                if not _host_allowed(host, allow_globs):
                    vios.append(Violation(
                        "ERROR", chart, f"sinks.{sink_name}",
                        f"egress to non-allowlisted host {host!r} ({url!r}). "
                        f"Add it to the allowlist (--allow-host) only after security review.",
                    ))

        # ── (2) extraEnv override discipline ───────────────────────────
        for cname, env in _iter_container_envs(m):
            name = env.get("name", "")
            is_literal = "value" in env  # literal value, not valueFrom
            # The chart legitimately defines VECTOR_SELF_* via
            # `valueFrom.fieldRef` (downward API — node/pod name).
            # fieldRef can only reference pod metadata, never an
            # attacker-chosen value, so it's safe. Any OTHER form on a
            # reserved name (literal value, or secret/configmap ref) is
            # an operator override that hijacks Vector's behavior.
            via_field_ref = "valueFrom" in env and "fieldRef" in (env.get("valueFrom") or {})
            if _matches_any(name, RESERVED_ENV_GLOBS) and not via_field_ref:
                vios.append(Violation(
                    "ERROR", chart, f"{m.get('kind')}/{cname}.env.{name}",
                    f"overrides Vector-reserved env {name} via {'literal value' if is_literal else 'a non-fieldRef source'}; "
                    f"reserved vars are only legitimate via downward-API fieldRef "
                    f"(the chart's own pattern). An override here hijacks pipeline behavior.",
                ))
            elif _matches_any(name, SENSITIVE_ENV_GLOBS) and is_literal:
                vios.append(Violation(
                    "ERROR", chart, f"{m.get('kind')}/{cname}.env.{name}",
                    f"sensitive env {name} set via literal `value:` — use "
                    f"`valueFrom.secretKeyRef` instead (literal hardcodes a secret / "
                    f"lets an attacker substitute a credential).",
                ))
    return vios


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="Egress allowlist + env-override gate for log-aggregation charts (#566 T4)."
    )
    parser.add_argument("--chart", action="append", default=[],
                        help="Chart dir to lint (repeatable). Default: the 3 log-aggregation charts.")
    parser.add_argument("--values", action="append", default=[],
                        help="values file to render with (repeatable, passed to all charts).")
    parser.add_argument("--set", dest="sets", action="append", default=[],
                        help="helm --set override (repeatable).")
    parser.add_argument("--allow-host", action="append", default=[],
                        help="Additional allowed egress host glob (repeatable).")
    parser.add_argument("--ci", action="store_true", help="CI mode: exit 1 on any ERROR.")
    parser.add_argument("--json", action="store_true", help="Emit violations as JSON.")
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[3]
    charts = [Path(c) for c in args.chart] or [repo_root / c for c in DEFAULT_CHARTS]
    allow_globs = list(DEFAULT_ALLOWED_HOST_GLOBS) + args.allow_host
    values_files = [Path(v) for v in args.values]

    all_vios: list[Violation] = []
    for chart_dir in charts:
        if not (chart_dir / "Chart.yaml").exists():
            continue  # skip non-charts silently (lets default list be lenient)
        manifests = render_chart(chart_dir, values_files, args.sets)
        all_vios.extend(lint_chart(chart_dir, manifests, allow_globs))

    if args.json:
        import json
        print(json.dumps([v.__dict__ for v in all_vios], indent=2))
    else:
        for v in all_vios:
            marker = "❌" if v.level == "ERROR" else "⚠️"
            print(f"  {marker} [{v.chart}] {v.where}: {v.message}")
        errors = sum(1 for v in all_vios if v.level == "ERROR")
        warnings = len(all_vios) - errors
        if not all_vios:
            print("✓ log-egress policy: no violations across "
                  f"{sum(1 for c in charts if (c / 'Chart.yaml').exists())} chart(s)")
        else:
            print(f"\nResult: {errors} error(s), {warnings} warning(s)")

    if any(v.level == "ERROR" for v in all_vios):
        sys.exit(EXIT_VIOLATION)


if __name__ == "__main__":
    main()
