#!/usr/bin/env python3
"""check_iac_vibe_rules.py — Container/k8s IaC SAST, Layer 1 (Dockerfile).

Epic #448 / TRK-311. **Hybrid policy** (open-source engine + Vibe wrapper):
hadolint is the engine; this wrapper aggregates hadolint's JSON output,
applies the epic's severity->action reduction, and adds four
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

  V4  No lint directive hadolint has no switch for (#3643) — a
      ``# hadolint shell=<x>`` comment, and a ``# shellcheck ...``
      comment. ``--disable-ignore-pragma`` turns off ``ignore=`` only: the
      shell pragma still makes hadolint skip ShellCheck for every RUN after
      it (an error-level SC finding disappears), and inside a heredoc RUN a
      comment that starts with ``shellcheck`` (even ``# shellchecked``) is
      read by ShellCheck as its own directive, ``disable=`` included
      (measured, v2.12.0). The spellings flagged are a superset of the ones hadolint
      acts on. A ``SHELL [...]`` instruction and a heredoc's ``#!`` line
      change what the image runs, not only what the linter reads, and are
      not flagged.

Engine (hadolint) finding -> action, by ``level``:
  error    -> BLOCK  (Critical)
  warning  -> WARN   (High; recorded in docs/internal/iac-lint-baseline.md)
  info     -> INFO
  style    -> INFO
Vibe rule violations (V1/V2/V3/V4) and unregistered Dockerfiles -> BLOCK.

The unified Critical/High/Medium/Low -> BLOCK/WARN/INFO table is finalised
across all four layers in TRK-314; Layer 1 uses the BLOCK/WARN reduction
above.

hadolint is located through ``_lib_engine``: ``hadolint`` on PATH, else
``docker run hadolint/hadolint:<ver>``. If neither exists the run is exit 2
(no verdict) with or without ``--ci`` (#3611, owner 2026-11-06) — never a
green run that skipped the engine. To skip the hook, use pre-commit's own
``SKIP=iac-sast-check`` (shown as Skipped, not Passed).

⛔ #3439: a hadolint run counts only when it shows that it ran. It is called
with ``--no-fail``, so a run that read every file exits 0 whatever it found
(the wrapper, not hadolint's ``failure-threshold``, turns levels into
BLOCK), and ``--format json`` prints a JSON array even when clean (``[]``).
Anything else — a non-zero exit (``docker run`` with no daemon exits 1 with
an empty stdout; a file hadolint cannot open), an empty stdout, output that
is not an array of findings — is the engine not having run: exit 2 (a
caller error: no verdict; #3577, #3611), its
stderr shown. It was read as "no finding", rc 0.

⛔ #3643: a run counts only when it also shows that it ran with this repo's
levels. hadolint's levels and trusted registries live in one place, the
``HADOLINT_RULES`` / ``HADOLINT_TRUSTED_REGISTRIES`` registry below; there
is no ``.hadolint.yaml`` (hadolint drops a config it cannot parse, or a key
it does not know, and exits 0 at its default levels — a BLOCK became a
WARN). The registry is passed as flags; hadolint gets an explicit ``{}``
config (so it discovers none), no ``HADOLINT_*`` variable, and
``--disable-ignore-pragma`` (an inline ``# hadolint ignore=`` does not waive
a finding; the bypass below is the waiver). The directives that flag does
not reach are V4's, a BLOCK finding. The same run lints a canary Dockerfile
on stdin: every registered rule must come back on it exactly once, at its
registered level (the canary holds one untrusted FROM and one FROM per
trusted registry, so DL3026 once means the registries were applied; only
code and level are read, not the line). Anything else is exit 2.

Usage:
    python3 scripts/tools/lint/check_iac_vibe_rules.py [--ci]
    python3 scripts/tools/lint/check_iac_vibe_rules.py --list

Exit codes:
    0  no BLOCK findings (WARN / INFO may be present), or bypass matched
    1  BLOCK findings present (only when --ci)
    2  no verdict: hadolint unavailable (no binary, no docker), did not
       run (non-zero exit, no JSON array on stdout; see above), or did not
       report the canary at the registered levels (#3643)

Bypass (docs/internal/lint-policy.md §4): a BLOCK finding judged a
legitimate exception can be waived from the PR body:
    bypass-lint: iac-vibe-rules
    reason: <>=30 words explaining why this is legitimate>
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

# Make stdout tolerate non-ASCII on Windows shells (cp950, cp1252).
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))  # Repo subdir layout
from _lib_exitcodes import EXIT_CALLER_ERROR, EXIT_OK, EXIT_VIOLATION  # noqa: E402
import _lib_engine  # noqa: E402
from _lint_helpers import parse_bypass_tag  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
HADOLINT_VERSION = "v2.12.0"
# SHA-256 of the release binary `hadolint-Linux-x86_64` at HADOLINT_VERSION (upstream
# .sha256, re-verified once on bump). This module is the SSOT for both values:
# `.claude/hooks/session-start.sh` reads these two lines at runtime (#3627) — keep each
# a single-line string literal — and ci.yml's copy is bound by
# tests/shared/test_toolchain_pin_parity.py.
HADOLINT_SHA256 = "56de6d5e5ec427e17b74fa48d51271c7fc0d61244bf5c90e828aab8362d55010"
# Supply-chain: pin the Docker fallback by digest (multi-arch index of the tag) so a
# re-pushed/tampered tag can't swap the image. On version bump, re-resolve with:
#   docker buildx imagetools inspect hadolint/hadolint:<ver>   (take the top-level Digest)
# Do NOT use `docker inspect` — on an arm64 host it can yield an arch-specific digest that
# breaks the amd64 CI fallback. #849 follow-up.
HADOLINT_DIGEST = "sha256:30a8fd2e785ab6176eed53f74769e04f125afb2f74a6c52aef7d463583b6d45e"
HADOLINT_IMAGE = f"hadolint/hadolint:{HADOLINT_VERSION}@{HADOLINT_DIGEST}"
BYPASS_NAME = "iac-vibe-rules"

_HADOLINT_LEVELS = ("error", "warning", "info", "style")


@dataclass(frozen=True)
class HadolintRule:
    """One row of the level registry.

    ``level`` is what this repo gives the rule; ``default`` is hadolint's own
    level for it at HADOLINT_VERSION (re-measured against the engine by
    tests/lint/test_check_iac_vibe_rules.py); ``canary`` is one Dockerfile
    instruction that trips the rule exactly once — the proof that the level
    reached the engine in this very run (``hadolint_canary``)."""
    level: str
    default: str
    canary: str


# ⛔ The only source of hadolint's levels and trusted registries (#3643).
# There is no `.hadolint.yaml`: hadolint drops a config file it cannot parse,
# and a key or rule code it does not know, without failing — so the wrapper
# passes these as command-line flags (`hadolint_flags`) and checks in the
# same run that they were applied (`hadolint_canary`). Running `hadolint
# <Dockerfile>` by hand therefore shows hadolint's defaults, not this table;
# run this script instead.
#
# AC1's rule set (DL3007 / DL3008 / DL3009 / DL3018 / DL3019 / DL3025) is ON
# by hadolint default and none of it is silenced; rows here only set a level.
#
# ⚠️ Which canary proves what. A row whose level differs from hadolint's
# default (DL3007) is the evidence that the level flags were applied: without
# them it comes back at the default. A row at its default level (DL3018,
# DL3026) proves no such thing: it comes back the same with or without its
# flag, and only shows that the rule still fires on its instruction (drift
# from a hadolint bump). DL3026 proves the trusted-registry flags instead: with none
# applied it never fires, and with one missing it fires on that registry's
# FROM in the canary as well (a second hit).
HADOLINT_RULES: dict[str, HadolintRule] = {
    # :latest (or untagged) base images are a reproducibility + supply-chain
    # risk -> error, so the wrapper treats it as Critical (BLOCK).
    "DL3007": HadolintRule(level="error", default="warning",
                           canary="FROM docker.io/library/alpine:latest"),
    # apk version pinning. The platform deliberately uses `apk --no-cache` +
    # monthly rebuild + Trivy CRITICAL/HIGH gate instead of brittle Alpine
    # version pins (Alpine drops old package versions from the repo quickly,
    # which turns a pinned `apk add pkg=x.y.z` into a hard build break). Kept
    # at warning => baseline High (non-blocking), recorded in
    # docs/internal/iac-lint-baseline.md. Rationale mirrors the comment in
    # components/da-portal/Dockerfile.
    "DL3018": HadolintRule(level="warning", default="warning",
                           canary="RUN apk add --no-cache curl"),
    # Only allowed registries in FROM (see HADOLINT_TRUSTED_REGISTRIES).
    "DL3026": HadolintRule(level="error", default="error",
                           canary="FROM untrusted.invalid/canary:1.0"),
}

# Base-image registries we publish from / pull bases from. Keeps DL3026
# meaningful without flagging our own bases.
HADOLINT_TRUSTED_REGISTRIES: tuple[str, ...] = ("docker.io", "gcr.io", "ghcr.io")


def _check_hadolint_registry() -> None:
    """Import-time: every row carries a usable canary, and the registry as a
    whole can prove that it was applied."""
    for code, rule in HADOLINT_RULES.items():
        if rule.level not in _HADOLINT_LEVELS or rule.default not in _HADOLINT_LEVELS:
            raise ValueError(f"HADOLINT_RULES[{code}]: level must be one of {_HADOLINT_LEVELS}")
        line = rule.canary
        if not line.strip() or "\n" in line or line.lstrip().startswith("#"):
            raise ValueError(f"HADOLINT_RULES[{code}]: canary must be one Dockerfile "
                             "instruction on one line")
    if not any(r.level != r.default for r in HADOLINT_RULES.values()):
        raise ValueError("HADOLINT_RULES: no row differs from hadolint's default level, "
                         "so no canary can show the level flags were applied")
    if not HADOLINT_TRUSTED_REGISTRIES or "DL3026" not in HADOLINT_RULES:
        raise ValueError("HADOLINT_TRUSTED_REGISTRIES needs at least one registry and a "
                         "DL3026 row: its canary is what shows the registries were applied")
    if "-" in HADOLINT_RULES:
        raise ValueError("HADOLINT_RULES: `-` is not a rule code")


_check_hadolint_registry()

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
    "components/recipe-preview/Dockerfile": ".",
    "components/tenant-api/Dockerfile": ".",
    "components/threshold-exporter/app/Dockerfile": ".",
    "tests/e2e-bench/driver/Dockerfile": "tests/e2e-bench/driver",
    "tests/e2e-bench/receiver/Dockerfile": ".",
    "helm/federation-gateway/audit-sidecar/Dockerfile": "helm/federation-gateway/audit-sidecar",
    "helm/vector/projection-gate/Dockerfile": "helm/vector/projection-gate",
    # try-local's generator service runtime (#2903): built by the demo
    # compose stack, never published; the service code is bind-mounted. The
    # repo root since #3530: its da-guard stage builds the exporter's Go.
    "try-local/generator/Dockerfile": ".",
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
        if p.name.endswith(".dockerignore"):  # BuildKit's per-Dockerfile ignore file
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


# V4. Horizontal whitespace only between the parts, any case, anywhere on a
# line: wider than what hadolint v2.12.0 acts on (lower case, a comment line
# of its own), so that no spelling it does act on gets through — the real
# engine is asked in tests/lint/test_check_iac_vibe_rules.py.
_H = r"[^\S\r\n]"
_LINT_DIRECTIVES: tuple[tuple[str, "re.Pattern[str]", str], ...] = (
    ("hadolint shell pragma",
     re.compile(rf"#{_H}*hadolint{_H}+shell{_H}*=", re.IGNORECASE),
     "hadolint skips ShellCheck for the RUNs after it; if the image's shell "
     "really differs, say so with a SHELL [...] instruction"),
    ("shellcheck directive",
     re.compile(rf"#{_H}*shellcheck", re.IGNORECASE),
     "inside a heredoc RUN ShellCheck reads it as its own directive; fix the "
     "finding, or word the comment without a leading `shellcheck`"),
)


def lint_directive_lines(content: str) -> list[tuple[int, str, str]]:
    """V4: ``(line, what, why)`` for each line carrying a lint directive that
    hadolint has no flag to turn off."""
    out: list[tuple[int, str, str]] = []
    for i, line in enumerate(content.splitlines(), 1):
        for what, pattern, why in _LINT_DIRECTIVES:
            if pattern.search(line):
                out.append((i, what, why))
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
def locate_engine() -> "_lib_engine.Located | _lib_engine.NoVerdict":
    return _lib_engine.locate("hadolint", docker_image=HADOLINT_IMAGE)


def hadolint_flags() -> list[str]:
    """The level and trusted-registry flags, generated from the registry."""
    flags: list[str] = []
    for code, rule in HADOLINT_RULES.items():
        flags += [f"--{rule.level}", code]
    for registry in HADOLINT_TRUSTED_REGISTRIES:
        flags += ["--trusted-registry", registry]
    return flags


def hadolint_canary() -> str:
    """The canary Dockerfile sent on stdin with every run (#3643): one FROM
    per trusted registry (a DL3026 hit on one would be a second hit, which
    the proof's count catches), then every
    registered rule's ``canary`` instruction — each under an inline ignore
    pragma for its own code, and the whole file under a global one, so a run
    in which pragmas were honoured loses the findings the proof looks for."""
    lines = [f"# hadolint global ignore={','.join(HADOLINT_RULES)}"]
    lines += [f"FROM {registry}/canary/trusted:1.0"
              for registry in HADOLINT_TRUSTED_REGISTRIES]
    for code, rule in HADOLINT_RULES.items():
        lines += [f"# hadolint ignore={code}", rule.canary]
    return "\n".join(lines) + "\n"


def hadolint_canary_expectation() -> dict[str, list[str]]:
    """Rule code -> the levels the canary must come back with: each rule's
    instruction trips it once, at the registered level."""
    return {code: [rule.level] for code, rule in HADOLINT_RULES.items()}


def hadolint_env() -> dict[str, str]:
    """This process's environment without any ``HADOLINT_*`` variable: they
    override the command line (measured, v2.12.0: ``HADOLINT_IGNORE``,
    ``HADOLINT_OVERRIDE_ERROR`` and ``HADOLINT_TRUSTED_REGISTRIES`` each beat
    the matching flag)."""
    return {k: v for k, v in os.environ.items()
            if not k.upper().startswith("HADOLINT_")}


def run_hadolint(dockerfiles: list[str]) -> "_lib_engine.Result":
    """Run hadolint over the given repo-relative Dockerfiles: a Ran whose
    ``data`` is the findings, or a NoVerdict — the caller's exit 2 — when
    the engine is unavailable, the run does not show that it ran (#3439), or
    the canary sent on stdin did not come back at the registered levels
    (#3643; see the module docstring and ``_lib_engine._hadolint``).
    """
    if not dockerfiles:
        # Nothing to lint is an empty, real answer; no engine run to prove.
        return _lib_engine.Ran([], None)
    if "-" in dockerfiles:   # hadolint's name for stdin: the canary's slot
        raise ValueError("`-` is not a Dockerfile path")
    located = locate_engine()
    if isinstance(located, _lib_engine.NoVerdict):
        return located
    binary = located.mode == "binary"
    with tempfile.TemporaryDirectory(prefix="hadolint-config-") as td:
        # An explicit --config stops hadolint from discovering one (cwd, XDG,
        # home). It holds `{}` rather than nothing: an empty file or /dev/null
        # is a parse error to v2.12.0 — measured, it then prints the error on
        # every run, still applies the flags and still discovers no config,
        # but that is what it happens to do after failing, not a contract.
        Path(td, "hadolint.yaml").write_text("{}\n", encoding="utf-8", newline="\n")
        config = f"{td}/hadolint.yaml" if binary else "/hadolint-config/hadolint.yaml"
        # --no-fail: exit 0 means "ran", never "found nothing at the threshold";
        # what a finding is worth is classify_level's answer, not hadolint's.
        args = ["--config", config, "--disable-ignore-pragma", "--no-fail",
                "--format", "json", *hadolint_flags(), *dockerfiles, "-"]
        return _lib_engine.run(
            located, "lint", args, timeout=180,
            cwd=str(REPO_ROOT) if binary else None,
            # -i: without it the container's stdin is closed and the canary
            # never arrives (which the proof then reads as no verdict).
            docker_run=["-i", "-v", f"{td}:/hadolint-config:ro",
                        "-v", f"{REPO_ROOT.as_posix()}:/repo", "-w", "/repo"],
            entrypoint="hadolint", stdin_text=hadolint_canary(),
            env=hadolint_env(),
            expect={"canary": hadolint_canary_expectation()})


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
    """Run all rules; return {'BLOCK': [...], 'WARN': [...], 'INFO': [...]}.

    An engine that is missing, or that does not show it ran, is exit 2 with
    or without ``--ci`` (#3611 Q1)."""
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
    if isinstance(hl, _lib_engine.NoVerdict):
        print(f"ERROR: {hl.message()}", file=sys.stderr)
        findings["BLOCK"].append(
            "[engine] hadolint unavailable, did not run, or did not run at "
            "the registered levels (the ERROR line above says which) — "
            "install the hadolint binary, or Docker with a running daemon; "
            "for a canary mismatch see HADOLINT_RULES in check_iac_vibe_rules.py"
        )
        # Signal hard error to caller via a sentinel.
        findings["__engine_error__"] = ["1"]
        return findings
    for f in hl.data:
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
        for ln, what, why in lint_directive_lines(content):
            findings["BLOCK"].append(
                f"{df}:{ln} [V4 lint-directive] {what}: {why}"
            )

    # Vibe rule V3 — the ignore file each build actually reads. BuildKit reads
    # `<Dockerfile>.dockerignore` (next to the Dockerfile) IN PLACE OF the
    # context root's .dockerignore when it exists (#3425: the exporter and the
    # e2e receiver build from the repo root and narrow the context that way),
    # so that file is the one the baseline is checked on.
    effective: dict[str, str] = {}
    for df in registered:
        own = f"{df}.dockerignore"
        if (REPO_ROOT / own).is_file():
            effective[own] = own
        else:
            ctx = DOCKERFILE_CONTEXTS[df]
            effective[ctx] = ".dockerignore" if ctx == "." else f"{ctx}/.dockerignore"
    for ctx, rel in sorted(effective.items()):
        di = REPO_ROOT / rel
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
                        help="Exit 1 on any BLOCK finding")
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

    if engine_error:  # the scan did not run: a caller error (#3577, #3611)
        return EXIT_CALLER_ERROR

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
        f"   修法：補 HEALTHCHECK 或 `# rationale:` 註解 / 收斂過寬 COPY / 移除 lint 指令註解 / "
        f"補齊 build-context 的 .dockerignore baseline / 修 hadolint error。\n"
        f"   合法例外請於 PR description 加：\n"
        f"     bypass-lint: {BYPASS_NAME}\n"
        f"     reason: <>=30 words>\n"
        f"   詳見 docs/internal/lint-policy.md §4 與 epic #448 / TRK-311。"
    )
    return EXIT_VIOLATION if args.ci else EXIT_OK


if __name__ == "__main__":
    import _lib_exitcodes
    _lib_exitcodes.run(main)
