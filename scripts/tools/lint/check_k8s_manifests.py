#!/usr/bin/env python3
"""check_k8s_manifests.py — Container/k8s IaC SAST, Layer 4 (raw k8s manifests).

Epic #448 / TRK-314. The fourth sibling of the SAST family:
  L1  check_iac_vibe_rules.py      Dockerfile (hadolint + wrapper)
  L2  check_iac_helm.py            Helm charts (render-then-lint, kube-linter)
  L3  check_helm_values_secrets.py secret-shape (values + templates + k8s/)
  L4  THIS                         raw k8s/**/*.yaml (kube-linter, no render)

The ticket originally scoped this as a *stub* ("repo has 0 raw manifests, all go
through Helm"). That premise was stale: `k8s/` carries 42 real manifests
(prometheus / grafana / alertmanager Deployments, a tenant-api Deployment, a
CronJob, ConfigMaps, RBAC, NetworkPolicies, raw Secrets). So this is a REAL
layer, not a stub.

Engine: the SAME kube-linter as L2, but pointed at the raw manifest tree — no
helm render needed (the files are already concrete k8s objects). We reuse L2's
severity classification (classify_check / CRITICAL_CHECKS / HIGH_CHECKS) and the
.kube-linter.yaml config, and apply the SAME central-registry exemption model.

Why a SEPARATE EXEMPTIONS dict from L2's: L2 keys by (chart, check) because a
chart renders to many objects; raw manifests are keyed by (repo-relative path,
check) — the file IS the audit unit. CRITICAL is never exemptable (same as L2).

Severity -> action (the unified table is finalised in TRK-314 — see
docs/internal/iac-lint-baseline.md):
  CRITICAL -> BLOCK, NO escape: privileged / privilege-escalation / host-network
              / host-pid / host-ipc / docker-sock.
  HIGH     -> exemptable ONLY via the EXEMPTIONS registry below (an unregistered
              HIGH BLOCKS — forces a deliberate, audited entry).
  LOW      -> INFO, non-blocking.

NB: there is NO Mode-A source scan here (unlike L2). L2's ALLOW_EMPTY/INSECURE_*
regex would FALSE-POSITIVE on legitimate raw-manifest keys (e.g. Prometheus
scrape `insecure_skip_verify: true`), and raw manifests carry no `{{ if }}`
template branches for a pre-render scan to recover — so the kube-linter pass is
the whole of L4's *workload* scan. Hardcoded *secret values* in raw Secrets are
covered by L3 (its scope includes k8s/**/*.yaml).

One native (non-kube-linter) rule rides along — PSS namespace labels (#1018):
every `kind: Namespace` under k8s/ must carry BOTH
`pod-security.kubernetes.io/warn` and `.../audit` labels with a value in
{privileged, baseline, restricted}; `enforce` is OPTIONAL (the #1018 rollout is
phased — enforce flips per-ns after soak) but validated when present.
INFORMATIONAL ONLY: findings land in WARN and NEVER block (--ci exit stays 0)
— soak-stage discipline so in-flight PRs are not retroactively gated; revisit
the severity at enforce-flip time. Engine-independent (pure PyYAML parse; a
missing PyYAML is surfaced as an explicit WARN, never a silent skip — CI
installs it).

Engine is binary-on-PATH first (CI installs it), else Docker:
  kube-linter -> `kube-linter` | docker run stackrox/kube-linter:<ver>
With --ci the engine is REQUIRED (exit 3 if missing) so the scan can't silently
skip in CI; without --ci it is skipped with a WARN when absent.

Usage:
    python3 scripts/tools/lint/check_k8s_manifests.py [--ci]
    python3 scripts/tools/lint/check_k8s_manifests.py --list

Exit codes:
    0  no BLOCK findings (WARN/INFO/baseline may be present)
    1  BLOCK findings present (only when --ci)
    2  caller-error: could not RUN the check (kube-linter failed/unparseable on
       k8s/) — fix the engine/manifests, then retry
    3  engine required (--ci) but unavailable
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

# Reuse L2's engine plumbing + severity classification (single source of truth
# for kube-linter version/image and CRITICAL/HIGH check sets).
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # Repo subdir layout
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from check_iac_helm import (  # noqa: E402
    KUBE_LINTER_IMAGE,
    SKIP_DIR_PARTS,
    classify_check,
    locate_kube_linter,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
MANIFEST_ROOT = "k8s"

# --- Central exemption registry (epic #448 / TRK-314) -----------------------
# (repo-relative manifest path, check) -> rationale. A registered HIGH finding
# becomes a recorded baseline-High (non-blocking) and is listed in
# docs/internal/iac-lint-baseline.md. CRITICAL is never exemptable. Adding an
# entry is a deliberate, reviewable act — the single audit surface for "what
# privileges has the platform opened" in raw manifests.
EXEMPTIONS: dict[tuple[str, str], str] = {
    ("k8s/04-tenant-api/deployment.yaml", "no-read-only-root-fs"):
        "tenant-api git-clone init + api container need a writable workspace to "
        "clone/commit conf.d (same rationale as the helm chart's tenant-api "
        "exemption); the oauth2-proxy sidecar IS read-only",
}


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def find_manifest_files() -> list[Path]:
    """Repo-relative raw k8s manifests (k8s/**/*.{yaml,yml}), worktrees excluded.

    Used for the reported count + --list. kube-linter itself scans the whole
    dir (both extensions) regardless; matching both here keeps the count honest
    if a `.yml` manifest is ever added (self-review #A — scope == hook trigger).
    """
    out: list[Path] = []
    root = REPO_ROOT / MANIFEST_ROOT
    if not root.is_dir():
        return out
    for ext in ("*.yaml", "*.yml"):
        for p in root.rglob(ext):
            rel = p.relative_to(REPO_ROOT)
            if any(part in SKIP_DIR_PARTS for part in rel.parts):
                continue
            out.append(p)
    return sorted(set(out))


def manifest_root_exists() -> bool:
    return (REPO_ROOT / MANIFEST_ROOT).is_dir()


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested)
# ---------------------------------------------------------------------------
def normalize_relpath(file_path: str) -> str:
    """kube-linter FilePath -> repo-relative posix path.

    FilePath may be `/repo/k8s/...` (docker mount), an absolute host path, or a
    bare `k8s/...` (binary mode, cwd=repo). Anchor on the `k8s/` root segment so
    all three normalise to the same key the EXEMPTIONS dict uses.
    """
    p = file_path.replace("\\", "/")
    idx = p.rfind(f"{MANIFEST_ROOT}/")
    if idx != -1:
        return p[idx:]
    return p.lstrip("/")


def is_exempt(relpath: str, check: str) -> bool:
    return (relpath, check) in EXEMPTIONS


# ---------------------------------------------------------------------------
# PSS namespace-label rule (#1018) — native, engine-independent, WARN-only
# ---------------------------------------------------------------------------
PSS_PREFIX = "pod-security.kubernetes.io/"
PSS_REQUIRED_MODES = ("warn", "audit")   # every Namespace must declare both
PSS_OPTIONAL_MODES = ("enforce",)        # phased rollout: optional, validated
PSS_VALID_LEVELS = {"privileged", "baseline", "restricted"}


def pss_label_findings(doc: object, relpath: str) -> list[str]:
    """Findings for ONE parsed YAML doc (pure — unit-tested).

    Non-Namespace kinds and non-mapping docs yield nothing. Values are
    validated against the three official PSS levels; the *-version companion
    labels are deliberately NOT validated (pinning is an enforce-flip-time
    concern, see docs/internal/pss-enforcement-runbook.md).
    """
    if not isinstance(doc, dict) or doc.get("kind") != "Namespace":
        return []
    meta = doc.get("metadata") or {}
    name = meta.get("name", "?")
    labels = meta.get("labels") or {}
    out: list[str] = []
    for mode in PSS_REQUIRED_MODES:
        key = PSS_PREFIX + mode
        if key not in labels:
            out.append(
                f"[pss] {relpath} ns/{name}: missing `{key}` label — every "
                f"Namespace manifest must declare warn+audit PSS levels "
                f"(#1018 phased rollout; enforce stays optional until flip)")
        elif str(labels[key]) not in PSS_VALID_LEVELS:
            out.append(
                f"[pss] {relpath} ns/{name}: `{key}: {labels[key]}` is not a "
                f"valid PSS level (privileged|baseline|restricted)")
    for mode in PSS_OPTIONAL_MODES:
        key = PSS_PREFIX + mode
        if key in labels and str(labels[key]) not in PSS_VALID_LEVELS:
            out.append(
                f"[pss] {relpath} ns/{name}: `{key}: {labels[key]}` is not a "
                f"valid PSS level (privileged|baseline|restricted)")
    return out


def collect_pss_findings(files: list[Path] | None = None) -> list[str]:
    """PSS-label findings across raw manifests (default: the L4 file set).

    PyYAML-absent and per-file parse failures degrade to an EXPLICIT note in
    the returned list (still WARN-routed) — an informational rule must never
    silently skip, but must also never take the whole L4 run down."""
    try:
        import yaml
    except ImportError:
        return [
            "[pss] PyYAML unavailable — namespace PSS-label rule (#1018) "
            "skipped (pip install pyyaml; CI installs it)"]
    if files is None:
        files = find_manifest_files()
    out: list[str] = []
    for p in files:
        try:
            rel = p.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            rel = p.name
        try:
            docs = list(yaml.safe_load_all(p.read_text(encoding="utf-8")))
        except (yaml.YAMLError, OSError, UnicodeDecodeError) as e:
            out.append(
                f"[pss] {rel}: unreadable/unparseable "
                f"({e.__class__.__name__}) — PSS-label rule skipped this file")
            continue
        for doc in docs:
            out.extend(pss_label_findings(doc, rel))
    return out


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
def engine_available() -> bool:
    return locate_kube_linter()[0] is not None


def kube_linter_lint_dir(target_rel: str = MANIFEST_ROOT) -> list[dict] | None:
    """Lint a manifest directory with kube-linter. Returns Reports, or None on
    engine/parse error. Uses the repo's .kube-linter.yaml when present."""
    mode, binary = locate_kube_linter()
    if mode is None:
        return None
    cfg = REPO_ROOT / ".kube-linter.yaml"
    if mode == "binary":
        cmd = [binary, "lint", "--format", "json"]
        if cfg.exists():
            cmd += ["--config", str(cfg)]
        cmd += [str(REPO_ROOT / target_rel)]
        cwd: str | None = str(REPO_ROOT)
    else:  # docker — mount the repo, lint /repo/<target>
        cmd = [
            "docker", "run", "--rm",
            "-v", f"{REPO_ROOT.as_posix()}:/repo",
            KUBE_LINTER_IMAGE, "lint", "--format", "json",
        ]
        if cfg.exists():
            cmd += ["--config", "/repo/.kube-linter.yaml"]
        cmd += [f"/repo/{target_rel}"]
        cwd = None
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=180)
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"ERROR: kube-linter invocation failed: {e}", file=sys.stderr)
        return None
    out = (proc.stdout or "").strip()
    if not out:
        # kube-linter exits 1 when it finds issues, 0 when clean — both are
        # "ran OK"; anything else (with no JSON) is a real failure.
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
def collect_findings(strict: bool) -> dict[str, list[str]]:
    findings: dict[str, list[str]] = {
        "BLOCK": [], "WARN": [], "INFO": [], "CALLER_ERROR": []}

    if not manifest_root_exists():
        # AC4 trigger condition: enable automatically once k8s/ appears. It
        # exists now (42 manifests), so this branch is the "future-proof" guard.
        findings["INFO"].append(
            f"[scope] no {MANIFEST_ROOT}/ directory — Layer 4 idle (no raw manifests)")
        return findings

    # PSS namespace-label rule (#1018): engine-independent, so it runs BEFORE
    # (and regardless of) kube-linter availability. WARN-only by design —
    # soak-stage discipline, never blocks (--ci exit unaffected).
    findings["WARN"].extend(collect_pss_findings())

    if not engine_available():
        msg = ("kube-linter required but unavailable "
               "(install the binary, or Docker for the containerized fallback)")
        if strict:
            findings["BLOCK"].append(f"[engine] {msg}")
            findings["__engine_error__"] = ["1"]
        else:
            findings["WARN"].append(f"[engine] L4 skipped — {msg}")
        return findings

    reports = kube_linter_lint_dir()
    if reports is None:
        # kube-linter failed to execute / its output was unparseable — a
        # "couldn't run the check" condition (caller-error, exit 2), NOT a
        # finding of a non-compliant manifest (which is BLOCK / exit 1).
        findings["CALLER_ERROR"].append(
            "[engine] kube-linter failed/unparseable on k8s/")
        findings["__caller_error__"] = ["1"]
        return findings

    seen: set[tuple[str, str]] = set()
    for r in reports:
        check = r.get("Check", "?")
        msg = (r.get("Diagnostic", {}) or {}).get("Message", "")
        fp = ((r.get("Object", {}) or {}).get("Metadata", {}) or {}).get("FilePath", "")
        relpath = normalize_relpath(fp) if fp else "?"
        _emit(findings, relpath, check, msg, seen)
    return findings


def _emit(findings, relpath, check, msg, seen):
    """Classify one finding, apply the exemption registry, route to a bucket."""
    sev = classify_check(check)
    key = (relpath, check)
    # de-dupe identical (path, check) across multiple containers in one object
    if key in seen and sev != "CRITICAL":
        return
    seen.add(key)
    loc = f"{relpath} [{check}]"
    if sev == "CRITICAL":
        findings["BLOCK"].append(f"{loc} CRITICAL (no escape): {msg[:80]}")
    elif sev == "HIGH":
        if is_exempt(relpath, check):
            findings["WARN"].append(
                f"{loc} High — baseline-exempt: {EXEMPTIONS[key]}")
        else:
            findings["BLOCK"].append(
                f"{loc} High UNREGISTERED — fix it OR register in "
                f"check_k8s_manifests.py EXEMPTIONS with a rationale: {msg[:60]}")
    else:
        findings["INFO"].append(f"{loc} low: {msg[:80]}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.strip().splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--ci", action="store_true",
                        help="Exit non-zero on BLOCK; require the engine")
    parser.add_argument("--list", action="store_true",
                        help="List discovered raw manifests + engine status, then exit")
    args = parser.parse_args()

    if args.list:
        files = find_manifest_files()
        print(f"Raw k8s manifests under {MANIFEST_ROOT}/ ({len(files)}):")
        for f in files:
            print(f"  {f.relative_to(REPO_ROOT).as_posix()}")
        print(f"\nkube-linter: {locate_kube_linter()[0]}")
        print(f"EXEMPTIONS registered: {len(EXEMPTIONS)}")
        return EXIT_OK

    findings = collect_findings(strict=args.ci)
    engine_error = findings.pop("__engine_error__", None)
    caller_error = findings.pop("__caller_error__", None)

    for action in ("BLOCK", "WARN", "INFO", "CALLER_ERROR"):
        for line in findings[action]:
            print(f"  [{action}] {line}")

    if engine_error:
        return 3

    # "Couldn't run the check" (kube-linter failed/unparseable) is a
    # caller-error (exit 2: fix the engine/manifests, then retry), distinct from
    # a non-compliant-manifest finding (exit 1). Caller-error wins.
    if caller_error:
        print(
            "\nERROR Container SAST Layer 4 (raw k8s) — could not run kube-linter "
            "on k8s/ (engine failed or output unparseable). Fix the engine / "
            "manifests and retry.",
            file=sys.stderr,
        )
        return EXIT_CALLER_ERROR

    n_block = len(findings["BLOCK"])
    n_warn = len(findings["WARN"])
    n_info = len(findings["INFO"])
    n_files = len(find_manifest_files())

    if n_block == 0:
        print(
            f"\nOK Container SAST Layer 4 (raw k8s) — 0 BLOCK / {n_warn} "
            f"baseline-High / {n_info} INFO across {n_files} manifest(s).\n"
            f"   容器 SAST 第 4 層通過：0 阻擋；High 走中央豁免登記"
            f"（記於 docs/internal/iac-lint-baseline.md），不擋 merge。"
        )
        return EXIT_OK

    print(
        f"\nFAIL Container SAST Layer 4 (raw k8s) — {n_block} BLOCK / {n_warn} "
        f"baseline-High / {n_info} INFO.\n"
        f"   修法：Critical（privileged/hostNetwork/hostPID…）必修無 escape；\n"
        f"   未登記 High → 修掉它，或於 check_k8s_manifests.py EXEMPTIONS 加 "
        f"(path, check): rationale。\n"
        f"   詳見 epic #448 / TRK-314 與 docs/internal/iac-lint-baseline.md。"
    )
    return EXIT_VIOLATION if args.ci else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
