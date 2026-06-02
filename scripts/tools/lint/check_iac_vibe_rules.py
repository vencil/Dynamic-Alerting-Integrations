#!/usr/bin/env python3
"""check_iac_vibe_rules.py — Container/k8s IaC SAST, Layer 1 (Dockerfile).

Epic #448 / TRK-311. **Hybrid policy** (open-source engine + Vibe wrapper):
hadolint is the engine; this wrapper aggregates hadolint's JSON output,
applies the epic's severity->action reduction, and adds three
project-specific rules that hadolint cannot express:

  V1  HEALTHCHECK-or-rationale — every Dockerfile must declare a HEALTHCHECK
      OR carry a ``# rationale: <reason>`` comment explaining why none is
      needed. distroless runtime stages are auto-exempt (no shell to run a
      HEALTHCHECK probe; Kubernetes liveness/readiness probes cover it).

  V2  No over-broad COPY/ADD — a COPY/ADD source operand must not be a bare
      ``.`` / ``./`` / ``*`` (that copies the whole build context). This is
      the silent-break vector from the v2.7.0 incident that motivated #448
      (a new subdir is silently swept in / left out without review). NOTE:
      the issue body labelled this "DL3025"; the real DL3025 is "use JSON
      notation for CMD/ENTRYPOINT" (kept ON by default) — the broad-COPY
      intent has no native hadolint rule, hence this wrapper rule.
      Scope (accepted residual risk): V2 targets the catastrophic
      context-root sweep (bare ``.`` / ``./`` / ``*`` pulls secrets/temp
      files into the image). Directory-constrained globs like ``src/*`` or
      ``/*.py`` are NOT flagged — they carry a dir constraint and aren't the
      whole-context vector; full coverage would need an AST parser (ROI-
      negative for this floor).

  V3  .dockerignore fix-then-enforce — each Dockerfile's *build context root*
      must have a .dockerignore covering the mandated baseline. The context
      root is where Docker reads .dockerignore — NOT necessarily the
      Dockerfile's own directory (da-portal / tenant-api build from the repo
      root; see DOCKERFILE_CONTEXTS). Baseline membership is tested with the
      ``pathspec`` gitwildmatch parser so equivalent globs (``.git`` ==
      ``.git/`` == ``**/.git``) and comment lines are handled correctly.

Engine (hadolint) finding -> action, by ``level``:
  error    -> BLOCK  (Critical)
  warning  -> WARN   (High; recorded in docs/internal/iac-lint-baseline.md)
  info     -> INFO
  style    -> INFO
Vibe rule violations (V1/V2/V3) and unregistered Dockerfiles -> BLOCK.

The unified Critical/High/Medium/Low -> BLOCK/WARN/INFO table is finalised
across all four layers in TRK-314; Layer 1 uses the BLOCK/WARN reduction
above.

hadolint is located as: ``hadolint`` on PATH, else
``docker run hadolint/hadolint:<ver>``. If neither exists the run aborts
(exit 3) rather than silently passing.

Usage:
    python3 scripts/tools/lint/check_iac_vibe_rules.py [--ci]
    python3 scripts/tools/lint/check_iac_vibe_rules.py --list

Exit codes:
    0  no BLOCK findings (WARN / INFO may be present), or bypass matched
    1  BLOCK findings present (only when --ci)
    3  hadolint engine unavailable (no binary, no docker)

Bypass (docs/internal/lint-policy.md §4): a BLOCK finding judged a
legitimate exception can be waived from the PR body:
    bypass-lint: iac-vibe-rules
    reason: <>=30 words explaining why this is legitimate>
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Make stdout tolerate non-ASCII on Windows shells (cp950, cp1252).
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))  # Repo subdir layout
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION  # noqa: E402
from _lint_helpers import parse_bypass_tag  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
HADOLINT_VERSION = "v2.12.0"
HADOLINT_IMAGE = f"hadolint/hadolint:{HADOLINT_VERSION}"
BYPASS_NAME = "iac-vibe-rules"

# Dockerfile (repo-relative, posix) -> build-context root (repo-relative).
# The context root is where `docker build` reads .dockerignore from. Verified
# against .github/workflows/component-docker-build.yaml (da-portal + tenant-api
# use `context: .`) and each Dockerfile's COPY layout. A discovered Dockerfile
# absent from this map is a BLOCK finding: registering it forces an author to
# declare its build context, which is exactly the review step the #448
# silent-break incident skipped.
DOCKERFILE_CONTEXTS: dict[str, str] = {
    "components/da-tools/app/Dockerfile": "components/da-tools/app",
    "components/da-portal/Dockerfile": ".",
    "components/tenant-api/Dockerfile": ".",
    "components/threshold-exporter/app/Dockerfile": "components/threshold-exporter/app",
    "tests/e2e-bench/driver/Dockerfile": "tests/e2e-bench/driver",
    "tests/e2e-bench/receiver/Dockerfile": "tests/e2e-bench/receiver",
    "helm/federation-gateway/audit-sidecar/Dockerfile": "helm/federation-gateway/audit-sidecar",
}

# Mandated .dockerignore baseline (epic #448 AC 1). Each is (label, probe):
# `label` is what we show the author; `probe` is a representative path used to
# test coverage via pathspec. Entries that don't exist in a given leaf context
# (e.g. `docs/` under tests/e2e-bench/driver/) are harmless no-ops but kept for
# a uniform security/hygiene floor across every build context.
DOCKERIGNORE_BASELINE: list[tuple[str, str]] = [
    (".git/", ".git/HEAD"),
    ("tests/", "tests/probe"),
    ("scripts/", "scripts/probe"),
    ("docs/", "docs/probe"),
    ("*.md", "README.md"),
    (".github/", ".github/probe"),
    ("*.log", "build.log"),
    (".env*", ".env"),
]

# Directories never scanned for Dockerfiles:
#   .claude/.git       — sibling worktrees check out the same tree (double-count)
#   node_modules/.venv — third-party vendored trees may ship their own
#                        Dockerfiles; flagging those as "unregistered" would be
#                        a false-positive BLOCK on a dev machine where the dir
#                        exists (CI lint job has neither, so this is a
#                        local-pre-commit safety net).
SKIP_DIR_PARTS = {".claude", ".git", "node_modules", ".venv", "venv"}


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def find_dockerfiles() -> list[str]:
    """Return repo-relative (posix) paths of every Dockerfile in the tree."""
    out: list[str] = []
    for p in REPO_ROOT.rglob("Dockerfile*"):
        if not p.is_file():
            continue
        rel = p.relative_to(REPO_ROOT)
        if any(part in SKIP_DIR_PARTS for part in rel.parts):
            continue
        # Skip .dockerignore-style siblings; only actual Dockerfiles.
        if p.name != "Dockerfile" and not p.name.startswith("Dockerfile."):
            continue
        out.append(rel.as_posix())
    return sorted(out)


# ---------------------------------------------------------------------------
# Pure rule helpers (unit-tested in tests/lint/test_check_iac_vibe_rules.py)
# ---------------------------------------------------------------------------
def runtime_base_image(content: str) -> str:
    """The base image of the final (runtime) stage — the last FROM line."""
    froms = re.findall(r"(?im)^\s*FROM\s+(\S+)", content)
    return froms[-1] if froms else ""


def is_distroless(content: str) -> bool:
    return "distroless" in runtime_base_image(content).lower()


def has_healthcheck(content: str) -> bool:
    return bool(re.search(r"(?im)^\s*HEALTHCHECK\b", content))


def has_rationale(content: str) -> bool:
    # Horizontal-whitespace classes ([ \t]) so a bare `# rationale:` does NOT
    # match content on the following line (\s would span the newline).
    return bool(re.search(r"(?im)^[ \t]*#[ \t]*rationale[ \t]*:[ \t]*\S", content))


def healthcheck_violation(content: str) -> bool:
    """V1: non-distroless Dockerfile lacking both HEALTHCHECK and rationale."""
    if is_distroless(content):
        return False
    return not (has_healthcheck(content) or has_rationale(content))


def over_broad_copy_lines(content: str) -> list[tuple[int, str]]:
    """V2: COPY/ADD instructions whose source operand is a bare . / ./ / *."""
    out: list[tuple[int, str]] = []
    for i, line in enumerate(content.splitlines(), 1):
        m = re.match(r"(?i)^\s*(COPY|ADD)\s+(.+)$", line)
        if not m:
            continue
        rest = m.group(2)
        if rest.lstrip().startswith("["):
            # JSON/exec form: COPY ["src", "dest"] — sources are quoted; a
            # bare-dot source there is exceedingly rare, leave to review.
            continue
        toks = [t for t in rest.split() if not t.startswith("--")]
        if len(toks) < 2:
            continue
        sources = toks[:-1]
        if any(s in (".", "./", "*") for s in sources):
            out.append((i, line.strip()))
    return out


def dockerignore_baseline_gaps(dockerignore_text: str) -> list[str]:
    """Return baseline labels NOT covered by the given .dockerignore content.

    Uses pathspec's gitwildmatch parser so equivalent glob spellings and
    comment lines are normalised (day-2 hint #2 on issue #448).
    """
    import pathspec

    # Strip a leading "/" (root anchor) from each pattern before building the
    # coverage spec. We only care *whether* a baseline dir is excluded, not the
    # anchoring nuance — and pathspec versions disagree on whether a
    # root-anchored dir pattern (`/tests/`) matches a root-level path
    # (`tests/probe`). Normalising to the unanchored form (`tests/`) matches
    # consistently across versions while keeping the shipped files anchored
    # (anchored is intentional — see the .dockerignore headers — so that e.g.
    # `components/tenant-api/docs/` is NOT pruned from a repo-root build). The
    # `!` negation prefix is preserved so re-includes still subtract.
    norm_lines: list[str] = []
    for raw in dockerignore_text.splitlines():
        s = raw.strip()
        if s.startswith("!/"):
            norm_lines.append("!" + s[2:])
        elif s.startswith("/"):
            norm_lines.append(s[1:])
        else:
            norm_lines.append(raw)
    spec = pathspec.PathSpec.from_lines("gitwildmatch", norm_lines)
    return [
        label
        for label, probe in DOCKERIGNORE_BASELINE
        if not spec.match_file(probe)
    ]


def classify_level(level: str) -> str:
    """Map a hadolint finding level to a wrapper action."""
    if level == "error":
        return "BLOCK"
    if level == "warning":
        return "WARN"
    return "INFO"


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
def locate_engine() -> tuple[str | None, str | None]:
    """Return (mode, path). mode is 'binary' | 'docker' | None."""
    binary = shutil.which("hadolint")
    if binary:
        return ("binary", binary)
    if shutil.which("docker"):
        return ("docker", None)
    return (None, None)


def run_hadolint(dockerfiles: list[str]) -> list[dict] | None:
    """Run hadolint over the given repo-relative Dockerfiles.

    Returns parsed JSON findings, or None if the engine is unavailable / the
    output could not be parsed (caller treats None as a hard error).
    """
    if not dockerfiles:
        # hadolint with no file args reads stdin and would block until the
        # subprocess timeout — short-circuit to an empty result instead.
        return []
    mode, binary = locate_engine()
    cfg = REPO_ROOT / ".hadolint.yaml"
    if mode == "binary":
        cmd = [binary, "--config", str(cfg), "--format", "json", *dockerfiles]
        cwd: str | None = str(REPO_ROOT)
    elif mode == "docker":
        cmd = [
            "docker", "run", "--rm",
            "-v", f"{REPO_ROOT.as_posix()}:/repo",
            "-w", "/repo",
            HADOLINT_IMAGE,
            "hadolint", "--config", ".hadolint.yaml",
            "--format", "json", *dockerfiles,
        ]
        cwd = None
    else:
        return None

    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=180
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"ERROR: hadolint invocation failed: {e}", file=sys.stderr)
        return None

    out = (proc.stdout or "").strip()
    if not out:
        # hadolint prints nothing only on a non-JSON error path; clean runs
        # in --format json still print "[]". Treat empty + nonzero as error.
        if proc.returncode not in (0, 1):
            print(
                f"ERROR: hadolint produced no JSON (exit {proc.returncode}):\n"
                f"{(proc.stderr or '').strip()}",
                file=sys.stderr,
            )
            return None
        return []
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        print(
            f"ERROR: could not parse hadolint JSON output:\n{out[:500]}",
            file=sys.stderr,
        )
        return None


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def _read_pr_body(pr_body_file: str | None) -> str | None:
    if pr_body_file:
        try:
            return Path(pr_body_file).read_text(encoding="utf-8")
        except (FileNotFoundError, PermissionError) as e:
            print(f"WARN: cannot read --pr-body-file {pr_body_file}: {e}",
                  file=sys.stderr)
    return os.environ.get("PR_BODY") or None


def collect_findings(dockerfiles: list[str]) -> dict[str, list[str]]:
    """Run all rules; return {'BLOCK': [...], 'WARN': [...], 'INFO': [...]}."""
    findings: dict[str, list[str]] = {"BLOCK": [], "WARN": [], "INFO": []}

    # Unregistered Dockerfiles (governance) -> BLOCK.
    for df in dockerfiles:
        if df not in DOCKERFILE_CONTEXTS:
            findings["BLOCK"].append(
                f"{df}: [V0 unregistered] Dockerfile not in DOCKERFILE_CONTEXTS — "
                f"declare its build-context root in check_iac_vibe_rules.py"
            )

    registered = [df for df in dockerfiles if df in DOCKERFILE_CONTEXTS]

    # Engine (hadolint) findings.
    hl = run_hadolint(registered)
    if hl is None:
        findings["BLOCK"].append(
            "[engine] hadolint unavailable or unparseable — install the "
            "hadolint binary or Docker (see check_iac_vibe_rules.py)"
        )
        # Signal hard error to caller via a sentinel.
        findings["__engine_error__"] = ["1"]
        return findings
    for f in hl:
        action = classify_level(f.get("level", "info"))
        line = (
            f"{f.get('file')}:{f.get('line')} [{f.get('code')} "
            f"{f.get('level')}] {f.get('message')}"
        )
        findings[action].append(line)

    # Vibe rules V1 / V2 per Dockerfile.
    for df in registered:
        content = (REPO_ROOT / df).read_text(encoding="utf-8")
        if healthcheck_violation(content):
            findings["BLOCK"].append(
                f"{df}: [V1 healthcheck] no HEALTHCHECK and no "
                f"`# rationale:` comment (non-distroless image)"
            )
        for ln, snippet in over_broad_copy_lines(content):
            findings["BLOCK"].append(
                f"{df}:{ln} [V2 broad-copy] over-broad COPY/ADD source: {snippet}"
            )

    # Vibe rule V3 — .dockerignore per unique build context.
    for ctx in sorted(set(DOCKERFILE_CONTEXTS[df] for df in registered)):
        di = REPO_ROOT / (".dockerignore" if ctx == "." else f"{ctx}/.dockerignore")
        ctx_label = "<repo root>" if ctx == "." else ctx
        if not di.exists():
            findings["BLOCK"].append(
                f"{ctx_label}: [V3 dockerignore] missing .dockerignore for "
                f"build context (required by Dockerfiles building here)"
            )
            continue
        gaps = dockerignore_baseline_gaps(di.read_text(encoding="utf-8"))
        if gaps:
            findings["BLOCK"].append(
                f"{di.relative_to(REPO_ROOT).as_posix()}: [V3 dockerignore] "
                f"baseline incomplete — missing: {', '.join(gaps)}"
            )

    return findings


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.strip().splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--ci", action="store_true",
                        help="Exit non-zero on any BLOCK finding")
    parser.add_argument("--list", action="store_true",
                        help="List discovered Dockerfiles + contexts and exit")
    parser.add_argument("--pr-body-file", default=None,
                        help="Path to PR body file for bypass-tag check")
    args = parser.parse_args()

    dockerfiles = find_dockerfiles()

    if args.list:
        print("Discovered Dockerfiles -> build context:")
        for df in dockerfiles:
            ctx = DOCKERFILE_CONTEXTS.get(df, "??? UNREGISTERED")
            print(f"  {df}  ->  {ctx}")
        return EXIT_OK

    findings = collect_findings(dockerfiles)
    engine_error = findings.pop("__engine_error__", None)

    for action in ("BLOCK", "WARN", "INFO"):
        for line in findings[action]:
            print(f"  [{action}] {line}")

    if engine_error:
        return 3

    n_block = len(findings["BLOCK"])
    n_warn = len(findings["WARN"])
    n_info = len(findings["INFO"])

    if n_block == 0:
        print(
            f"\nOK Container SAST Layer 1 — 0 BLOCK / {n_warn} WARN / {n_info} INFO "
            f"across {len(dockerfiles)} Dockerfile(s).\n"
            f"   容器 SAST 第 1 層通過：0 個阻擋項；WARN 為 baseline High "
            f"（記於 docs/internal/iac-lint-baseline.md），不擋 merge。"
        )
        return EXIT_OK

    # Bypass check (lint-policy.md §4) — only downgrades BLOCK.
    pr_body = _read_pr_body(args.pr_body_file)
    bypass_reason = parse_bypass_tag(pr_body, BYPASS_NAME)
    if bypass_reason:
        print(
            f"\n⚠️  BYPASSED via PR body: {bypass_reason}\n"
            f"   {n_block} BLOCK finding(s) above are author-acknowledged; "
            f"reviewer must confirm the bypass is justified."
        )
        return EXIT_OK

    print(
        f"\nFAIL Container SAST Layer 1 — {n_block} BLOCK / {n_warn} WARN / "
        f"{n_info} INFO.\n"
        f"   容器 SAST 第 1 層失敗：{n_block} 個阻擋項（Critical）須修。\n"
        f"   修法：補 HEALTHCHECK 或 `# rationale:` 註解 / 收斂過寬 COPY / "
        f"補齊 build-context 的 .dockerignore baseline / 修 hadolint error。\n"
        f"   合法例外請於 PR description 加：\n"
        f"     bypass-lint: {BYPASS_NAME}\n"
        f"     reason: <>=30 words>\n"
        f"   詳見 docs/internal/lint-policy.md §4 與 epic #448 / TRK-311。"
    )
    return EXIT_VIOLATION if args.ci else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
