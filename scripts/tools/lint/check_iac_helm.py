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

Engines are located through _lib_engine, binary-on-PATH first (CI and
session-start.sh install them), else Docker:
  helm        -> `helm` | docker run alpine/helm:<ver>
  kube-linter -> `kube-linter` | docker run stackrox/kube-linter:<ver>
The engines are REQUIRED with or without --ci (#3611, owner 2026-11-06): an
engine that is missing, or that ran without proving it linted something, is
exit 2 — never a green Mode-B-less run. To skip the hook, use pre-commit's
own `SKIP=iac-helm-sast-check` (shown as Skipped, not Passed).

Usage:
    python3 scripts/tools/lint/check_iac_helm.py [--ci]
    python3 scripts/tools/lint/check_iac_helm.py --list

Exit codes:
    0  no BLOCK findings (WARN/INFO/baseline may be present)
    1  BLOCK findings present (only when --ci)
    2  no verdict: an engine is missing, or helm / kube-linter did not run on
       a chart (failed, rendered nothing, printed no report) — fix the
       chart / environment, then retry
"""
from __future__ import annotations

import argparse
import os
import re
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
import _lib_engine  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
KUBE_LINTER_VERSION = "v0.7.4"
# Supply-chain: pin Docker fallback images by digest (multi-arch index of the tag) so a
# re-pushed/tampered tag can't swap the image. On version bump, re-resolve each with:
#   docker buildx imagetools inspect <image>:<ver>   (take the top-level Digest)
# Do NOT use `docker inspect` — on an arm64 host it can yield an arch-specific digest that
# breaks the amd64 CI fallback. #849 follow-up. (check_k8s_manifests.py imports KUBE_LINTER_IMAGE.)
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
    # ("mariadb-instance", "run-as-non-root") was DE-REGISTERED by #1018: the
    # chart now pins pod-level runAsNonRoot + runAsUser/Group 999 (PSS
    # restricted-readiness), so a regression re-introducing the finding BLOCKS.
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
def locate_helm() -> "_lib_engine.Located | _lib_engine.NoVerdict":
    return _lib_engine.locate("helm", docker_image=HELM_IMAGE)


def locate_kube_linter() -> "_lib_engine.Located | _lib_engine.NoVerdict":
    return _lib_engine.locate("kube-linter", docker_image=KUBE_LINTER_IMAGE)


def describe(located) -> str:
    """One word for `--list`: where the engine runs from, or why it cannot."""
    if isinstance(located, _lib_engine.NoVerdict):
        return f"unavailable ({located.reason})"
    return located.describe()


# Values a chart refuses to render without, applied to every variant as
# `--set` (#2855: tenant-api needs writeBack.mode). The production shape is
# set, so kube-linter judges what an operator deploys: the PR mode with its
# forge flags and Secret references. A variant file must not set these keys —
# `--set` would override it.
RENDER_BASE_SETS: dict[str, list[str]] = {
    "tenant-api": [
        "writeBack.mode=pr-github",
        "gitRepoUrl=https://github.example/acme/conf.git",
        "writeBack.token.secretName=tenant-api-vcs",
        "writeBack.github.repo=acme/conf",
    ],
}


# Values files a chart refuses to render without, applied before the variant's
# -f: helm/victorialogs defaults to auth.mode=enforce and needs a Secret named
# per consumer (#1295). Its baseline restates the consumer registry and
# tests/helm/test_victorialogs_vmauth.py holds the two equal, so kube-linter
# judges the production shape (enforce, Secret-backed env vars).
RENDER_BASE_VALUES: dict[str, str] = {
    "victorialogs": "tests/helm/victorialogs_render_baseline.yaml",
}


def helm_render(chart_dir: str, values_rel: str | None) -> "_lib_engine.Result":
    """Render a chart (optionally with a -f values override): a Ran whose
    ``data`` is the rendered text, or a NoVerdict (helm missing, failed, or
    rendered nothing)."""
    located = locate_helm()
    if isinstance(located, _lib_engine.NoVerdict):
        return located
    args = ["template", "r"]
    for kv in RENDER_BASE_SETS.get(chart_name(chart_dir), []):
        args += ["--set", kv]
    root = str(REPO_ROOT) if located.mode == "binary" else "/repo"
    args.append(f"{root}/{chart_dir}")
    # Before the variant's -f, so a variant still overrides it.
    if chart_name(chart_dir) in RENDER_BASE_VALUES:
        args += ["-f", f"{root}/{RENDER_BASE_VALUES[chart_name(chart_dir)]}"]
    if values_rel:
        args += ["-f", f"{root}/{values_rel}"]
    args += ["--namespace", LINT_NAMESPACE]
    return _lib_engine.run(
        located, "template", args, timeout=120,
        cwd=str(REPO_ROOT) if located.mode == "binary" else None,
        docker_run=["-v", f"{REPO_ROOT.as_posix()}:/repo"])


def kube_linter_lint(rendered_yaml: str) -> "_lib_engine.Result":
    """Lint a rendered manifest: a Ran whose ``data`` is kube-linter's
    Reports, or a NoVerdict (missing, failed, or printed no report)."""
    located = locate_kube_linter()
    if isinstance(located, _lib_engine.NoVerdict):
        return located
    with tempfile.TemporaryDirectory() as td:
        manifest = Path(td) / "manifest.yaml"
        manifest.write_text(rendered_yaml, encoding="utf-8", newline="\n")
        cfg_local = Path(td) / ".kube-linter.yaml"
        cfg_src = REPO_ROOT / ".kube-linter.yaml"
        if cfg_src.exists():
            cfg_local.write_text(cfg_src.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
        root = td if located.mode == "binary" else "/scan"
        args = ["lint", "--format", "json"]
        if cfg_local.exists():
            args += ["--config", f"{root}/.kube-linter.yaml"]
        args.append(f"{root}/manifest.yaml")
        return _lib_engine.run(
            located, "lint", args, timeout=120,
            docker_run=["-v", f"{Path(td).as_posix()}:/scan"])


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def collect_findings(charts: list[str]) -> dict[str, list[str]]:
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
    # Required with or without --ci (#3611 Q1): no engine is no verdict.
    missing = [loc for loc in (locate_helm(), locate_kube_linter())
               if isinstance(loc, _lib_engine.NoVerdict)]
    if missing:
        for nv in missing:
            findings["CALLER_ERROR"].append(f"[engine] Mode B cannot run: {nv.reason}")
        findings["__engine_error__"] = ["1"]
        return findings

    for chart_dir in charts:
        cname = chart_name(chart_dir)
        for values_rel in values_variants(chart_dir):
            variant = values_rel.rsplit("/", 1)[-1] if values_rel else "values.yaml"
            render = helm_render(chart_dir, values_rel)
            if isinstance(render, _lib_engine.NoVerdict):
                # helm did not RUN the check — no verdict (exit 2), NOT a
                # security finding (exit 1, "found a non-compliant chart").
                findings["CALLER_ERROR"].append(
                    f"{chart_dir} [{variant}] [render] {render.reason}")
                findings["__caller_error__"] = ["1"]
                continue
            rendered = render.data
            lint = kube_linter_lint(rendered)
            if isinstance(lint, _lib_engine.NoVerdict):
                findings["CALLER_ERROR"].append(
                    f"{chart_dir} [{variant}] [engine] {lint.reason}")
                findings["__caller_error__"] = ["1"]
                continue
            seen: set[tuple[str, str]] = set()
            for r in lint.data:
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
                        help="Exit 1 on BLOCK findings")
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
        print(f"\nhelm: {describe(locate_helm())} | "
              f"kube-linter: {describe(locate_kube_linter())}")
        return EXIT_OK

    findings = collect_findings(charts)
    engine_error = findings.pop("__engine_error__", None)
    caller_error = findings.pop("__caller_error__", None)

    for action in ("BLOCK", "WARN", "INFO", "CALLER_ERROR"):
        for line in findings[action]:
            print(f"  [{action}] {line}")

    if engine_error:  # the scan did not run: a caller error (#3577, #3611)
        return EXIT_CALLER_ERROR

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
    import _lib_exitcodes
    _lib_exitcodes.run(main)
