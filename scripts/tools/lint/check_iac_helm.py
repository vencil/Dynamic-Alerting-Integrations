#!/usr/bin/env python3
"""check_iac_helm.py — Container/k8s IaC SAST, Layer 2 (Helm templates).

Epic #448 / TRK-312. Sibling of check_iac_vibe_rules.py (Layer 1, Dockerfile).
Hybrid policy: kube-linter is the engine; this wrapper drives it and adds the
project policy. Two modes:

  Mode A  Source text scan (pre-render) — catches DANGEROUS env patterns even
          when wrapped in `{{ if }}` (which a rendered scan would miss if the
          branch is false). Patterns: *ALLOW_EMPTY_PASSWORD*: "yes" /
          *ALLOW_EMPTY_*: "true" / INSECURE_*: "true". ERROR, NO escape.

  Mode B  Render-then-lint — `helm template <chart> --namespace=lint-test`
          (× each values*.yaml variant) piped to `kube-linter lint`. Plus a
          wrapper rule that parses the rendered YAML for
          securityContext.capabilities.add (kube-linter has no such check).

Severity -> action (the unified table is finalised in TRK-314; L2 uses this):
  CRITICAL -> BLOCK, NO escape: privileged-container / privilege-escalation-
              container / host-network / host-pid / host-ipc / docker-sock.
  HIGH     -> exemptable ONLY via the central EXEMPTIONS registry below:
              run-as-non-root / no-read-only-root-fs / unset-cpu-requirements /
              unset-memory-requirements / capabilities-add (wrapper rule).
              An unregistered HIGH finding BLOCKS (forces a deliberate, audited
              registry entry — same governance as L1's DOCKERFILE_CONTEXTS).
  LOW      -> INFO, non-blocking (every other kube-linter check).

Why a central registry instead of in-chart `# rationale:` comments: `helm
template` strips comments (capabilities driven by values via `toYaml` lose
them), and one audit surface (this dict + docs/internal/iac-lint-baseline.md)
beats grepping every chart. There is therefore NO PR-body bypass for L2 —
exemptions go through the registry (Critical is never exemptable).

Engines are located as binary-on-PATH first (CI installs them), else Docker:
  helm        -> `helm` | docker run alpine/helm:<ver>
  kube-linter -> `kube-linter` | docker run stackrox/kube-linter:<ver>
With --ci the engines are REQUIRED (exit 3 if missing) so Mode B can't silently
skip in CI; without --ci, Mode B is skipped with a WARN when engines are
absent (local dev without helm/kube-linter still gets Mode A).

Usage:
    python3 scripts/tools/lint/check_iac_helm.py [--ci]
    python3 scripts/tools/lint/check_iac_helm.py --list

Exit codes:
    0  no BLOCK findings (WARN/INFO/baseline may be present)
    1  BLOCK findings present (only when --ci)
    2  caller-error: could not RUN the check (helm render or kube-linter
       failed/unparseable on a chart) — fix the chart/engine, then retry
    3  engines required (--ci) but unavailable
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # Docker flat layout
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))  # Repo subdir layout
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
KUBE_LINTER_VERSION = "v0.7.4"
# Supply-chain: pin Docker fallback images by digest (multi-arch index of the tag) so a
# re-pushed/tampered tag can't swap the image. Re-resolve each alongside its version via
# the registry manifest API. #849 follow-up. (check_k8s_manifests.py imports KUBE_LINTER_IMAGE.)
KUBE_LINTER_DIGEST = "sha256:ac7873964c69d7dba2bd235c2bf10a7c61f783d5b08fdeaabc8f31916110329c"
KUBE_LINTER_IMAGE = f"docker.io/stackrox/kube-linter:{KUBE_LINTER_VERSION}@{KUBE_LINTER_DIGEST}"
HELM_VERSION = "3.16.4"
HELM_DIGEST = "sha256:9b25e60ae264940b276e32866d37e3088e70c4e2d1784b964dc3f90346281a74"
HELM_IMAGE = f"docker.io/alpine/helm:{HELM_VERSION}@{HELM_DIGEST}"
LINT_NAMESPACE = "lint-test"

SKIP_DIR_PARTS = {".claude", ".git", "node_modules", ".venv", "venv"}

# --- severity classification of kube-linter checks + wrapper pseudo-checks ---
CRITICAL_CHECKS = {
    "privileged-container",
    "privilege-escalation-container",
    "host-network",
    "host-pid",
    "host-ipc",
    "docker-sock",
}
HIGH_CHECKS = {
    "run-as-non-root",
    "no-read-only-root-fs",
    "unset-cpu-requirements",
    "unset-memory-requirements",
    "capabilities-add",  # wrapper rule (kube-linter has no equivalent)
}

# --- Mode A: dangerous source patterns (pre-render, NO escape) ---
DANGEROUS_SOURCE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("ALLOW_EMPTY_PASSWORD", re.compile(r'ALLOW_EMPTY[_A-Z]*PASSWORD\w*\s*:\s*["\']?\s*(yes|true)\b', re.I)),
    ("ALLOW_EMPTY_*=true", re.compile(r'ALLOW_EMPTY_\w+\s*:\s*["\']?\s*true\b', re.I)),
    ("INSECURE_*=true", re.compile(r'\bINSECURE_\w+\s*:\s*["\']?\s*true\b', re.I)),
]

# --- Central exemption registry (epic #448 / TRK-312) -----------------------
# (chart, check) -> rationale. A registered HIGH finding becomes a recorded
# baseline-High (non-blocking) and is listed in docs/internal/iac-lint-baseline.md.
# CRITICAL is never exemptable here. Adding an entry is a deliberate, reviewable
# act — the single audit surface for "what privileges has the platform opened".
EXEMPTIONS: dict[tuple[str, str], str] = {
    ("mariadb-instance", "run-as-non-root"):
        "mariadb official image starts as root to chown the data dir then drops to mysql",
    ("mariadb-instance", "no-read-only-root-fs"):
        "mariadb-server requires a writable /var/lib/mysql data directory",
    ("tenant-api", "no-read-only-root-fs"):
        "tenant-api gitops writer shells out to git and needs a writable working area",
    ("vector", "run-as-non-root"):
        "log-collector DaemonSet must run as root to read host pod logs under /var/log/pods",
    ("vector", "capabilities-add"):
        "DAC_READ_SEARCH — read host log files owned by other UIDs (paired with the root requirement)",
}


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def find_charts() -> list[str]:
    """Repo-relative chart dirs (those containing a Chart.yaml)."""
    out: list[str] = []
    for p in REPO_ROOT.glob("helm/*/Chart.yaml"):
        rel = p.parent.relative_to(REPO_ROOT)
        if any(part in SKIP_DIR_PARTS for part in rel.parts):
            continue
        out.append(rel.as_posix())
    return sorted(out)


def chart_name(chart_dir: str) -> str:
    return chart_dir.rsplit("/", 1)[-1]


def values_variants(chart_dir: str) -> list[str | None]:
    """None (chart default) + each values*.yaml override, repo-relative."""
    variants: list[str | None] = [None]
    for p in sorted((REPO_ROOT / chart_dir).glob("values*.yaml")):
        # The bare values.yaml is the default (None) — only add *overrides*.
        if p.name == "values.yaml":
            continue
        variants.append((p.relative_to(REPO_ROOT)).as_posix())
    return variants


def helm_source_files() -> list[Path]:
    out: list[Path] = []
    for p in (REPO_ROOT / "helm").rglob("*.yaml"):
        rel = p.relative_to(REPO_ROOT)
        if any(part in SKIP_DIR_PARTS for part in rel.parts):
            continue
        out.append(p)
    return sorted(out)


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested)
# ---------------------------------------------------------------------------
def classify_check(check: str) -> str:
    if check in CRITICAL_CHECKS:
        return "CRITICAL"
    if check in HIGH_CHECKS:
        return "HIGH"
    return "LOW"


def scan_source_line(line: str) -> list[str]:
    """Return labels of dangerous patterns matched in a source line."""
    return [label for label, pat in DANGEROUS_SOURCE_PATTERNS if pat.search(line)]


def find_capabilities_add(rendered_yaml: str) -> list[str]:
    """Container names (best-effort) whose securityContext adds a capability.

    Operates on RENDERED YAML (concrete, post-template). Looks for a
    `capabilities:` block with a non-empty `add:`. Returns the add-lists found.
    """
    out: list[str] = []
    lines = rendered_yaml.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"\s*add:\s*\[", line):
            # inline form: add: ["DAC_READ_SEARCH"]
            m = re.search(r"add:\s*\[([^\]]*)\]", line)
            if m and m.group(1).strip():
                out.append(m.group(1).strip())
        elif re.match(r"\s*add:\s*$", line):
            # block form: add:\n  - CAP
            j = i + 1
            caps = []
            while j < len(lines) and re.match(r"\s*-\s*\S", lines[j]):
                caps.append(lines[j].strip().lstrip("- ").strip())
                j += 1
            if caps:
                out.append(", ".join(caps))
    # Only flag when it's under a capabilities: block (avoid matching unrelated
    # `add:` keys). Cheap guard: require "capabilities:" somewhere in the doc.
    if out and "capabilities:" in rendered_yaml:
        return out
    return []


# ---------------------------------------------------------------------------
# Engines
# ---------------------------------------------------------------------------
def locate_helm() -> tuple[str | None, str | None]:
    if shutil.which("helm"):
        return ("binary", shutil.which("helm"))
    if shutil.which("docker"):
        return ("docker", None)
    return (None, None)


def locate_kube_linter() -> tuple[str | None, str | None]:
    if shutil.which("kube-linter"):
        return ("binary", shutil.which("kube-linter"))
    if shutil.which("docker"):
        return ("docker", None)
    return (None, None)


def engines_available() -> bool:
    return locate_helm()[0] is not None and locate_kube_linter()[0] is not None


def helm_render(chart_dir: str, values_rel: str | None) -> tuple[str | None, str]:
    """Render a chart (optionally with a -f values override). (text|None, err)."""
    mode, binary = locate_helm()
    args = ["template", "r"]
    if mode == "binary":
        cmd = [binary, *args, str(REPO_ROOT / chart_dir)]
        if values_rel:
            cmd += ["-f", str(REPO_ROOT / values_rel)]
        cmd += ["--namespace", LINT_NAMESPACE]
        cwd: str | None = str(REPO_ROOT)
    elif mode == "docker":
        cmd = [
            "docker", "run", "--rm", "-v", f"{REPO_ROOT.as_posix()}:/repo",
            HELM_IMAGE, *args, f"/repo/{chart_dir}",
        ]
        if values_rel:
            cmd += ["-f", f"/repo/{values_rel}"]
        cmd += ["--namespace", LINT_NAMESPACE]
        cwd = None
    else:
        return (None, "helm unavailable")
    try:
        # encoding/errors explicit: helm output carries UTF-8 (em-dashes, CJK
        # in chart comments) which would crash text=True under a cp950/locale
        # default on Windows hosts.
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120)
    except (subprocess.TimeoutExpired, OSError) as e:
        return (None, f"helm invocation failed: {e}")
    if proc.returncode != 0:
        return (None, (proc.stderr or proc.stdout or "render failed").strip().splitlines()[0] if (proc.stderr or proc.stdout) else "render failed")
    return (proc.stdout, "")


def kube_linter_lint(rendered_yaml: str) -> list[dict] | None:
    """Lint a rendered manifest. Returns kube-linter Reports, or None on error."""
    mode, binary = locate_kube_linter()
    if mode is None:
        return None
    with tempfile.TemporaryDirectory() as td:
        manifest = Path(td) / "manifest.yaml"
        manifest.write_text(rendered_yaml, encoding="utf-8")
        cfg_local = Path(td) / ".kube-linter.yaml"
        cfg_src = REPO_ROOT / ".kube-linter.yaml"
        if cfg_src.exists():
            cfg_local.write_text(cfg_src.read_text(encoding="utf-8"), encoding="utf-8")
        if mode == "binary":
            cmd = [binary, "lint", "--format", "json"]
            if cfg_local.exists():
                cmd += ["--config", str(cfg_local)]
            cmd += [str(manifest)]
        else:  # docker
            cmd = [
                "docker", "run", "--rm", "-v", f"{Path(td).as_posix()}:/scan",
                KUBE_LINTER_IMAGE, "lint", "--format", "json",
            ]
            if cfg_local.exists():
                cmd += ["--config", "/scan/.kube-linter.yaml"]
            cmd += ["/scan/manifest.yaml"]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=120)
        except (subprocess.TimeoutExpired, OSError) as e:
            print(f"ERROR: kube-linter invocation failed: {e}", file=sys.stderr)
            return None
        out = (proc.stdout or "").strip()
        if not out:
            return [] if proc.returncode in (0, 1) else None
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            print(f"ERROR: kube-linter JSON parse failed:\n{out[:300]}", file=sys.stderr)
            return None
        return data.get("Reports") or []


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def collect_findings(charts: list[str], strict: bool) -> dict[str, list[str]]:
    findings: dict[str, list[str]] = {
        "BLOCK": [], "WARN": [], "INFO": [], "CALLER_ERROR": []}

    # --- Mode A: source text scan (always runs; no engines needed) ---
    for fp in helm_source_files():
        try:
            text = fp.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError):
            continue
        rel = fp.relative_to(REPO_ROOT).as_posix()
        for ln_no, line in enumerate(text.splitlines(), 1):
            for label in scan_source_line(line):
                findings["BLOCK"].append(
                    f"{rel}:{ln_no} [ModeA {label}] dangerous value pattern "
                    f"(no escape): {line.strip()[:80]}"
                )

    # --- Mode B: render-then-lint ---
    if not engines_available():
        msg = ("helm + kube-linter required for Mode B but unavailable "
               "(install both, or Docker for the containerized fallback)")
        if strict:
            findings["BLOCK"].append(f"[engine] {msg}")
            findings["__engine_error__"] = ["1"]
        else:
            findings["WARN"].append(f"[engine] Mode B skipped — {msg}")
        return findings

    for chart_dir in charts:
        cname = chart_name(chart_dir)
        for values_rel in values_variants(chart_dir):
            variant = values_rel.rsplit("/", 1)[-1] if values_rel else "values.yaml"
            rendered, err = helm_render(chart_dir, values_rel)
            if rendered is None:
                # The engine (helm) failed to RUN the check — that's a
                # caller-error ("couldn't run"), NOT a security finding. Route
                # it to the caller-error sentinel (exit 2) instead of folding it
                # into BLOCK (exit 1, which means "found a non-compliant chart").
                findings["CALLER_ERROR"].append(
                    f"{chart_dir} [{variant}] [render] helm template failed "
                    f"(Mode B gap): {err}"
                )
                findings["__caller_error__"] = ["1"]
                continue
            # kube-linter findings
            reports = kube_linter_lint(rendered)
            if reports is None:
                # kube-linter failed to execute / its output was unparseable —
                # again "couldn't run", not a finding → caller-error (exit 2).
                findings["CALLER_ERROR"].append(
                    f"{chart_dir} [{variant}] [engine] kube-linter failed/unparseable"
                )
                findings["__caller_error__"] = ["1"]
                continue
            seen: set[tuple[str, str]] = set()
            for r in reports:
                check = r.get("Check", "?")
                msg = (r.get("Diagnostic", {}) or {}).get("Message", "")
                _emit(findings, cname, chart_dir, variant, check, msg, seen)
            # wrapper rule: capabilities.add
            for caps in find_capabilities_add(rendered):
                _emit(findings, cname, chart_dir, variant, "capabilities-add",
                      f"adds capability: {caps}", seen)

    return findings


def _emit(findings, cname, chart_dir, variant, check, msg, seen):
    """Classify one finding, apply the exemption registry, route to a bucket."""
    sev = classify_check(check)
    key = (cname, check)
    # de-dupe identical (chart, check) across containers / values variants
    if key in seen and sev != "CRITICAL":
        return
    seen.add(key)
    loc = f"{chart_dir} [{variant}] [{check}]"
    if sev == "CRITICAL":
        findings["BLOCK"].append(f"{loc} CRITICAL (no escape): {msg[:80]}")
    elif sev == "HIGH":
        if key in EXEMPTIONS:
            findings["WARN"].append(
                f"{loc} High — baseline-exempt: {EXEMPTIONS[key]}")
        else:
            findings["BLOCK"].append(
                f"{loc} High UNREGISTERED — fix it OR register in "
                f"check_iac_helm.py EXEMPTIONS with a rationale: {msg[:60]}")
    else:
        findings["INFO"].append(f"{loc} low: {msg[:80]}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.strip().splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--ci", action="store_true",
                        help="Exit non-zero on BLOCK; require engines for Mode B")
    parser.add_argument("--list", action="store_true",
                        help="List charts + values variants and exit")
    args = parser.parse_args()

    charts = find_charts()
    if args.list:
        print("Helm charts -> values variants:")
        for c in charts:
            variants = [v.rsplit("/", 1)[-1] if v else "values.yaml"
                        for v in values_variants(c)]
            print(f"  {c}  ->  {', '.join(variants)}")
        print(f"\nhelm: {locate_helm()[0]} | kube-linter: {locate_kube_linter()[0]}")
        return EXIT_OK

    findings = collect_findings(charts, strict=args.ci)
    engine_error = findings.pop("__engine_error__", None)
    caller_error = findings.pop("__caller_error__", None)

    for action in ("BLOCK", "WARN", "INFO", "CALLER_ERROR"):
        for line in findings[action]:
            print(f"  [{action}] {line}")

    if engine_error:
        return 3

    # A "couldn't run the check" condition (helm render / kube-linter failure)
    # is a caller-error (exit 2: fix the chart/environment, then retry), NOT a
    # lint violation (exit 1: a non-compliant chart). Caller-error wins over the
    # violation count so a broken render isn't misreported as "found a problem".
    if caller_error:
        print(
            f"\nERROR Container SAST Layer 2 (Helm) — could not run the check on "
            f"{len(findings['CALLER_ERROR'])} chart/variant(s) (helm render or "
            f"kube-linter failed). Fix the chart / engine and retry.",
            file=sys.stderr,
        )
        return EXIT_CALLER_ERROR

    n_block = len(findings["BLOCK"])
    n_warn = len(findings["WARN"])
    n_info = len(findings["INFO"])

    if n_block == 0:
        print(
            f"\nOK Container SAST Layer 2 (Helm) — 0 BLOCK / {n_warn} baseline-High "
            f"/ {n_info} INFO across {len(charts)} chart(s).\n"
            f"   容器 SAST 第 2 層通過：0 阻擋；High 走中央豁免登記"
            f"（記於 docs/internal/iac-lint-baseline.md），不擋 merge。"
        )
        return EXIT_OK

    print(
        f"\nFAIL Container SAST Layer 2 (Helm) — {n_block} BLOCK / {n_warn} "
        f"baseline-High / {n_info} INFO.\n"
        f"   修法：Critical（privileged/hostNetwork/hostPID…）必修無 escape；\n"
        f"   未登記 High → 修掉它，或於 check_iac_helm.py EXEMPTIONS 加 "
        f"(chart, check): rationale；\n"
        f"   ModeA 危險值（ALLOW_EMPTY/INSECURE）必修；render 失敗請修 chart。\n"
        f"   詳見 epic #448 / TRK-312 與 docs/internal/iac-lint-baseline.md。"
    )
    return EXIT_VIOLATION if args.ci else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
