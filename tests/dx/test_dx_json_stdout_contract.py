"""`--json` stdout contract gate for da-tools **dx** CLI tools (da-tools ROI r5, W1).

THE CONTRACT (strict)
---------------------
When a dx tool is invoked with its JSON-output flag (``--json`` / ``--json-output``
/ ``--format json``), **stdout MUST contain exactly one JSON document — nothing
else**.  Every human-readable line (progress, "📂 Scanned …", warnings, error
prose) belongs on **stderr**.

The observable form of the contract is one line:

    json.loads(stdout)   # must succeed on the FULL stdout text

Full-text ``json.loads`` is deliberate: it simultaneously proves (a) valid JSON
and (b) no leading/trailing prose.  We never scan for "the first ``{``" — a
lenient parse would let prose-contaminated stdout pass, which is the very bug
this gate exists to catch.

WHY THIS FILE EXISTS
--------------------
The ops half of da-tools got this gate in #1112
(``tests/shared/test_json_stdout_contract.py``, 83 recipes over 37 ops tools).
The **dx** half (``scripts/tools/dx/``) never had it.  A human spot-check of five
dx tools (describe_tenant / tenant_verify / coverage_delta / waveform_score /
doc_impact) found them clean, so this gate is primarily a **ratchet** — it locks
in the current good state and stops a future dx tool from regressing.  But it is
*also* executable recon: it actually RUNS every gate-able dx ``--json`` mode, so a
prose-leak the spot-check missed (analogous to the cardinality-URL crash the ops
gate flushed out) lands as a red here, not in a caller's ``| jq``.

SCOPE — WHAT THIS GATE ASSERTS
------------------------------
* Exactly the 14 dx tools that DECLARE a JSON-output flag as an argparse
  ``add_argument`` — 13 via ``--json`` / ``--json-output`` and 1
  (``describe_tenant``) via ``--format`` with a ``json`` choice.
  ``collect_json_tools()`` is **AST-based, not regex-based**, precisely because a
  literal ``"--json"`` substring scan (what the ops gate uses) would also match
  the ``["gh", …, "--json", …]`` *pass-through* arguments in ``pr_preflight`` /
  ``analyze_bench_history`` / (the gh calls inside) ``diag_pr_ci`` /
  ``analyze_tier1_fp_rate`` — those are gh's output selector, NOT the tool's own
  stdout contract, and must not be gated.  Walking ``add_argument`` calls captures
  the tool's *own* declared flags and ignores list-literal pass-throughs.
* ``test_recipe_table_covers_every_json_tool`` fails if a new dx tool grows a
  JSON-output flag without getting a recipe here, so scope cannot silently rot.
* Each recipe drives one distinct terminal path.  ``coverage_delta`` gets two:
  the JSON happy path AND the ``--json --markdown`` contradiction (see below).

EXIT-CODE FACE
--------------
Where a recipe's terminal path has a deterministic 0/1/2 outcome, ``expect_exit``
pins it (clean report → 0; the ``coverage_delta`` reject → 2).  Recipes whose exit
code depends on repo state (``verify_diff --check`` gates on map freshness) leave
``expect_exit=None`` and assert JSON purity only — the honest boundary.  The exit
codes themselves are additionally gated by ``tests/shared/test_tool_exit_codes.py``;
this file's exit assertions are a secondary cross-check tied to the JSON path.
NB ``tenant_verify`` INVERTS 1/2 (2 = verify-failed, per a shipped rollback
runbook); the recipe here uses ``--all`` whose success is a plain 0, so the
inversion is not exercised and not asserted.

HOW EXTERNAL DEPENDENCIES ARE HANDLED (stub, don't skip-and-shrug)
-----------------------------------------------------------------
* **gh** (``diag_pr_ci`` / ``analyze_tier1_fp_rate``) → a fake ``gh`` on PATH
  (``fake_gh_dir``) routes ``api``/``repo view``/``run list`` calls to the recorded
  ``tests/dx/fixtures/diag_pr_ci/`` fixtures (``diag_pr_ci``) and to an empty
  ``run list`` (``analyze_tier1_fp_rate`` — the empty run list short-circuits before
  its ``--jq`` endpoints, so a single canned reply drives a full clean JSON doc).
  **Windows caveat, verified empirically:** Python's ``subprocess`` on Windows
  resolves a bare ``gh`` to ``gh.exe`` and ignores PATHEXT, so a ``.bat``/POSIX
  shim is bypassed and the REAL ``gh`` would run (network / auth / rate-limit).
  These two recipes therefore ``skip`` on ``os.name == 'nt'`` and run for real on
  POSIX (Linux CI / dev container), where this gate is authoritative — identical
  to the ops gate's fake-kubectl boundary.
* **jsonschema** (``waveform_compile`` / ``waveform_score``) → required at import;
  if absent the tool exits 2 with empty stdout (an env artefact, not a contract
  break), so those recipes ``skip`` when ``jsonschema`` can't be imported.  It is
  present in CI's dep step and on the dev host.

SCOPE — WHAT THIS GATE DOES *NOT* ASSERT (honest boundaries)
------------------------------------------------------------
1. **Argparse-rejected invocations are out of scope** — bad/missing flags exit 2
   from argparse before the tool's own logic runs (already gated by
   ``test_tool_exit_codes.py``).  The one exception is a *tool-level* rejection
   that the tool itself must own: ``coverage_delta --json --markdown`` is two
   mutually-exclusive output formats, which the tool rejects with
   ``EXIT_CALLER_ERROR`` + empty stdout — gated here via ``expect_caller_error``.
2. **``inject_waveform --json`` is exempt on hosts without the VM replay harness.**
   Its only JSON terminal path runs *after* ``run_pipeline`` drives a real
   ``vmalert -replay`` against a live ``vmsingle`` — there is no dry-run / offline
   mode, and the pre-pipeline validation-fail path emits NOTHING to stdout (it is
   a rejected-input path, like argparse).  With no ``vmalert`` on PATH the JSON
   path is unreachable, so the recipe ``skip``s (guarded on ``shutil.which`` so it
   RUNS wherever the harness exists) — the single tool this gate cannot execute
   for real here.  Flagged for Wave-2 follow-up, not silently dropped.
3. **Non-JSON output flags** (``--markdown`` / ``--badge`` / ``--format yaml`` /
   ``--summary``) are a different contract and not gated.
4. **Data-shape coverage is one-deep** — each mode is driven with one input shape;
   the contract is about *where the bytes go*, not report content.

FAILURES ARE EXPECTED IF A VIOLATOR EXISTS (that is the point)
-------------------------------------------------------------
Each red ``(tool, mode)`` is a violator for the follow-up fix wave; the failure
message names the tool, the mode, the exit code, and the first 200 chars of the
offending stdout/stderr.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DX_DIR = REPO_ROOT / "scripts" / "tools" / "dx"

# ── Real in-repo fixtures (all asserted to exist by test_fixture_paths_exist) ──
SEED_CONF_D = REPO_ROOT / "try-local" / "seed" / "conf.d"
RULE_PACKS = REPO_ROOT / "rule-packs"
DOCS_DIR = REPO_ROOT / "docs"
ARCH_DOC = DOCS_DIR / "architecture-and-design.md"
WAVEFORM_FIX = REPO_ROOT / "tests" / "dx" / "fixtures" / "waveform"
WAVEFORM_PACK = WAVEFORM_FIX / "selftest_service_up.yaml"
WAVEFORM_TOLERANCES = WAVEFORM_FIX / "tolerances" / "selftest_tolerances.yaml"
DIAG_FIXTURE_DIR = REPO_ROOT / "tests" / "dx" / "fixtures" / "diag_pr_ci"

FIXTURE_PATHS = [
    SEED_CONF_D, RULE_PACKS, DOCS_DIR, ARCH_DOC,
    WAVEFORM_PACK, WAVEFORM_TOLERANCES, DIAG_FIXTURE_DIR,
]

TIMEOUT_S = 120

# scripts/tools/_lib_exitcodes.py is the SSOT (0 = OK, 1 = VIOLATION, 2 = CALLER_ERROR).
EXIT_OK = 0
EXIT_CALLER_ERROR = 2

_HAVE_JSONSCHEMA = importlib.util.find_spec("jsonschema") is not None
_HAVE_VMALERT = shutil.which("vmalert") is not None


# ═══════════════════════════════════════════════════════════════════════════
# Scope discovery — AST-based (NOT the ops gate's regex).
#
# A dx tool is in scope iff it DECLARES a JSON-output flag via `add_argument`:
#   * an option string "--json" or "--json-output", OR
#   * "--format" with a `choices=[..., "json", ...]`  (describe_tenant).
# Walking add_argument calls is what distinguishes a tool's OWN flag from the
# `["gh", ..., "--json", ...]` pass-through list literals that a substring regex
# would wrongly sweep in (pr_preflight / analyze_bench_history / the gh calls
# inside diag_pr_ci & analyze_tier1_fp_rate).
# ═══════════════════════════════════════════════════════════════════════════
def _declares_json_output_flag(path: Path) -> bool:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"):
            continue
        opts = [a.value for a in node.args
                if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        if any(o in ("--json", "--json-output") for o in opts):
            return True
        if "--format" in opts:
            for kw in node.keywords:
                if kw.arg == "choices":
                    choices = [e.value for e in getattr(kw.value, "elts", [])
                               if isinstance(e, ast.Constant)]
                    if "json" in choices:
                        return True
    return False


def collect_json_tools() -> list[str]:
    """Every dx tool that declares a JSON-output flag as an argparse argument."""
    out = []
    for f in sorted(DX_DIR.glob("*.py")):
        if f.name.startswith("_") or f.name == "__init__.py":
            continue
        if _declares_json_output_flag(f):
            out.append(f.stem)
    return out


JSON_TOOLS = collect_json_tools()


# ═══════════════════════════════════════════════════════════════════════════
# Fake gh — shadows the real binary on PATH (POSIX only; see module docstring).
#
# Routes the three gh call shapes the two gh-facing dx tools make:
#   * `gh --version` / `gh auth status`      → succeed (prereq probes)
#   * `gh api /rate_limit`                    → healthy remaining quota
#   * `gh repo view --json nameWithOwner`     → a canned owner/repo
#   * `gh api <endpoint>`                     → the recorded diag_pr_ci fixtures
#   * `gh run list ...`                       → `[]`  (analyze_tier1_fp_rate: an
#                                                empty run list short-circuits
#                                                before its `--jq` endpoints)
# ═══════════════════════════════════════════════════════════════════════════
_FAKE_GH_PY = r'''
import json, os, sys

FIXTURE_DIR = {fixture_dir!r}


def emit(s):
    sys.stdout.write(s if s.endswith("\n") else s + "\n")
    sys.exit(0)


def fixture(name):
    with open(os.path.join(FIXTURE_DIR, name), encoding="utf-8") as fh:
        return fh.read()


argv = sys.argv[1:]

if argv[:1] == ["--version"]:
    emit("gh version 0.0.0-fake")
if argv[:2] == ["auth", "status"]:
    sys.stderr.write("fake-gh: authenticated\n")
    sys.exit(0)
if argv[:2] == ["repo", "view"]:
    emit(json.dumps({{"nameWithOwner": "vencil/Dynamic-Alerting-Integrations"}}))
if argv[:2] == ["run", "list"]:
    emit("[]")
if argv[:2] == ["pr", "view"]:
    emit(json.dumps({{"state": "MERGED", "labels": []}}))
if argv[:1] == ["api"]:
    path = next((a for a in argv[1:] if not a.startswith("-")), "")
    if "/rate_limit" in path:
        emit(json.dumps({{"rate": {{"remaining": 5000, "reset": 9999999999}}}}))
    if path.endswith("/check-runs/7002/annotations"):
        emit(fixture("annotations_7002.json"))
    if path.endswith("/check-runs/7003/annotations"):
        emit(fixture("annotations_7003.json"))
    if "/commits/" in path and "/check-runs" in path:
        emit(fixture("check_runs.json"))
    if "/actions/runs/" in path and path.endswith("/jobs"):
        emit(fixture("jobs_55002.json"))
    if "/pulls/" in path and "/check-runs" not in path:
        emit(fixture("pull_446.json"))
    if path.endswith("/pulls"):
        emit("[]")
    emit("{{}}")
emit("{{}}")
'''


@pytest.fixture(scope="session")
def fake_gh_dir(tmp_path_factory) -> Path:
    """A directory holding a fake `gh`, to be prepended to PATH (POSIX)."""
    d = tmp_path_factory.mktemp("fake_gh")
    impl = d / "fake_gh.py"
    impl.write_text(_FAKE_GH_PY.format(fixture_dir=str(DIAG_FIXTURE_DIR)),
                    encoding="utf-8")

    posix = d / "gh"
    posix.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{impl}" "$@"\n',
        encoding="utf-8", newline="\n",
    )
    posix.chmod(0o755)
    (d / "gh.bat").write_text(
        f'@echo off\r\n"{sys.executable}" "{impl}" %*\r\n', encoding="utf-8",
    )
    return d


# ═══════════════════════════════════════════════════════════════════════════
# Input-file builders
# ═══════════════════════════════════════════════════════════════════════════
def _write(p: Path, text: str) -> str:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return str(p)


def _cobertura(tmp: Path, name: str, total_rate: float) -> str:
    """A minimal Cobertura report (shape from tests/dx/test_coverage_delta.py)."""
    return _write(tmp / name, (
        f'<?xml version="1.0" ?>\n'
        f'<coverage line-rate="{total_rate}" lines-covered="70" lines-valid="100">\n'
        f'  <packages>\n'
        f'    <package name="default" line-rate="{total_rate}">\n'
        f'      <classes>\n'
        f'        <class name="x.py" filename="x.py" line-rate="{total_rate}" '
        f'lines-covered="70" lines-valid="100"><lines/></class>\n'
        f'      </classes>\n'
        f'    </package>\n'
        f'  </packages>\n'
        f'</coverage>\n'
    ))


def _coverage_text(tmp: Path) -> str:
    """A pytest-cov term-missing block (format from coverage_gap_analysis)."""
    return _write(tmp / "coverage.txt", (
        "Name                       Stmts   Miss  Cover   Missing\n"
        "----------------------------------------------------------\n"
        "scripts/tools/dx/a.py        100     30    70%   12-15, 20\n"
        "scripts/tools/dx/b.py         40      0   100%\n"
        "----------------------------------------------------------\n"
        "TOTAL                        140     30    79%\n"
    ))


def _waveform_report(tmp: Path) -> str:
    """A synthetic inject_waveform report (VM-free), shape mirrored from
    tests/dx/test_waveform_score.py `_report()`; one in-window hit → verdict PASS.
    Passes waveform_score.load_report()'s shape/version guard (needs `tool`,
    `records`, `window`, `metadata`, `unattributed_alerts`, and each
    metadata.series entry carrying `fault_window_s`).
    """
    alert = {
        "alertname": "CandidateA", "fire_offset_s": 1000,
        "last_fire_offset_s": 1060, "resolve_offset_s": 1090,
        "firing_sample_count": 3,
        "labels": {"alertname": "CandidateA", "waveform_signature": "0",
                   "waveform_variant": "base", "severity": "warning"},
    }
    record = {
        "signature_index": 0, "fault_class": "selftest-fault",
        "metric": "selftest_metric", "variant": "base", "series": None,
        "expects": "must_detect", "labels": {}, "fired": True, "alerts": [alert],
    }
    meta = {
        "signature_index": 0, "variant": "base", "labels": {},
        "expects": "must_detect", "fault_window_s": [300, 9270],
        "hold_start_s": None,
    }
    report = {
        "tool": "inject-waveform", "pack_id": "synthetic-pack",
        "records": [record], "window": {"span_s": 12000, "step_s": 30},
        "metadata": {"series": [meta]}, "unattributed_alerts": [],
    }
    return _write(tmp / "inject-report.json", json.dumps(report))


# ═══════════════════════════════════════════════════════════════════════════
# Recipes: one per (tool, mode) — i.e. per distinct terminal path.
# ═══════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class Recipe:
    tool: str
    mode: str
    build: Callable[[Path], list[str]]        # (tmp_path) -> argv tail
    expect_exit: int | None = None            # assert exit code when set
    expect_caller_error: bool = False         # reject: exit 2, empty stdout
    needs_gh: bool = False                     # relies on the fake-gh shim
    skip_if: bool = False                      # env-gated skip
    skip_reason: str = ""

    @property
    def id(self) -> str:
        return f"{self.tool}[{self.mode}]"


R = Recipe
RECIPES: list[Recipe] = [
    # ── analyze_tier1_fp_rate — empty run list short-circuits to a clean doc ─
    R("analyze_tier1_fp_rate", "json",
      lambda t: ["--json"], expect_exit=EXIT_OK, needs_gh=True),

    # ── coverage_delta ──────────────────────────────────────────────────────
    R("coverage_delta", "json",
      lambda t: [_cobertura(t, "before.xml", 0.70),
                 _cobertura(t, "after.xml", 0.70), "--json"],
      expect_exit=EXIT_OK),
    # Two mutually-exclusive output formats — a tool-level contradiction the
    # tool must REJECT (exit 2, empty stdout), not serve. (main() lines 464-467.)
    R("coverage_delta", "reject-json-markdown",
      lambda t: [_cobertura(t, "b.xml", 0.70), _cobertura(t, "a.xml", 0.70),
                 "--json", "--markdown"],
      expect_caller_error=True),

    # ── coverage_gap_analysis — saved coverage text (no pytest run) ─────────
    R("coverage_gap_analysis", "json",
      lambda t: ["--json", "--coverage-text", _coverage_text(t)],
      expect_exit=EXIT_OK),

    # ── describe_tenant  (JSON via --format json, the default) ─────────────
    R("describe_tenant", "all-format-json",
      lambda t: ["--all", "--conf-d", str(SEED_CONF_D), "--format", "json"],
      expect_exit=EXIT_OK),

    # ── diag_pr_ci  (gh-facing; fake gh + recorded fixtures) ───────────────
    R("diag_pr_ci", "json",
      lambda t: ["446", "--json"], expect_exit=EXIT_OK, needs_gh=True),

    # ── doc_coverage ────────────────────────────────────────────────────────
    R("doc_coverage", "json",
      lambda t: ["--json", "--repo-root", str(REPO_ROOT)], expect_exit=EXIT_OK),

    # ── doc_impact  (a real doc drives the report path) ────────────────────
    R("doc_impact", "json",
      lambda t: ["--json", str(ARCH_DOC), "--docs-dir", str(DOCS_DIR)],
      expect_exit=EXIT_OK),

    # ── generate_rule_pack_stats  (reads rule-packs/ via __file__) ─────────
    R("generate_rule_pack_stats", "json",
      lambda t: ["--json"], expect_exit=EXIT_OK),

    # ── generate_tenant_metadata ───────────────────────────────────────────
    R("generate_tenant_metadata", "json",
      lambda t: ["--config-dir", str(SEED_CONF_D), "--json"], expect_exit=EXIT_OK),

    # ── inject_waveform — JSON path needs the VM replay harness (exempt) ───
    R("inject_waveform", "json",
      lambda t: [str(WAVEFORM_PACK), "--rules", str(WAVEFORM_FIX / "rules"),
                 "--allow-selftest", "--json"],
      needs_gh=False, skip_if=not _HAVE_VMALERT,
      skip_reason=("inject_waveform's only --json terminal path runs after a real "
                   "`vmalert -replay` against a live vmsingle; no vmalert on PATH "
                   "→ path unreachable. Runs wherever the VM harness exists.")),

    # ── tenant_verify  (--all → EXIT_PASS 0; inversion not exercised) ──────
    R("tenant_verify", "all-json",
      lambda t: ["--all", "--conf-d", str(SEED_CONF_D), "--json"],
      expect_exit=EXIT_OK),

    # ── verify_diff  (--check emits one doc; exit gates on map freshness) ──
    R("verify_diff", "check-json",
      lambda t: ["--check", "--json"], expect_exit=None),

    # ── waveform_compile  (schema validate → one JSON doc) ─────────────────
    R("waveform_compile", "check-json",
      lambda t: [str(WAVEFORM_PACK), "--check", "--allow-selftest", "--json"],
      expect_exit=EXIT_OK, skip_if=not _HAVE_JSONSCHEMA,
      skip_reason="jsonschema not importable (required at import; env artefact)"),

    # ── waveform_score  (synthetic inject report → verdict PASS) ───────────
    R("waveform_score", "json",
      lambda t: [_waveform_report(t), "--tolerances", str(WAVEFORM_TOLERANCES),
                 "--json"],
      expect_exit=EXIT_OK, skip_if=not _HAVE_JSONSCHEMA,
      skip_reason="jsonschema not importable (required at import; env artefact)"),
]


# ═══════════════════════════════════════════════════════════════════════════
# Meta-tests — keep the scope honest
# ═══════════════════════════════════════════════════════════════════════════
def test_fixture_paths_exist():
    """Every in-repo fixture this gate leans on must actually be there."""
    missing = [str(p) for p in FIXTURE_PATHS if not p.exists()]
    assert not missing, f"gate fixtures missing from the repo: {missing}"


def test_recipe_table_covers_every_json_tool():
    """A dx tool that grows a JSON-output flag must gain a recipe, or this rots.

    (13 declare `--json`/`--json-output`; describe_tenant declares `--format json`.)
    """
    covered = {r.tool for r in RECIPES}
    uncovered = sorted(set(JSON_TOOLS) - covered)
    stale = sorted(covered - set(JSON_TOOLS))
    assert not uncovered, (
        f"{len(uncovered)} dx tool(s) declare a JSON-output flag but have no "
        f"recipe in RECIPES: {uncovered}"
    )
    assert not stale, f"RECIPES names dx tool(s) that no longer exist: {stale}"
    assert len(JSON_TOOLS) == 14, (
        f"expected 14 dx JSON-output tools (13 --json/--json-output + 1 "
        f"describe_tenant --format json), found {len(JSON_TOOLS)}: "
        f"{sorted(JSON_TOOLS)}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# The gate
# ═══════════════════════════════════════════════════════════════════════════
def _run(recipe: Recipe, tmp_path: Path, fake_gh: Path) -> subprocess.CompletedProcess:
    script = DX_DIR / f"{recipe.tool}.py"
    env = dict(os.environ)
    # Windows hosts default to cp950 -> a tool with CJK output raises
    # UnicodeEncodeError before we ever see stdout — an env artefact, not the
    # contract under test.
    env["PYTHONIOENCODING"] = "utf-8"
    if recipe.needs_gh:
        env["PATH"] = str(fake_gh) + os.pathsep + env.get("PATH", "")
    return subprocess.run(
        [sys.executable, str(script), *recipe.build(tmp_path)],
        capture_output=True, timeout=TIMEOUT_S, cwd=str(REPO_ROOT), env=env,
    )


@pytest.mark.parametrize("recipe", RECIPES, ids=[r.id for r in RECIPES])
def test_json_mode_emits_exactly_one_json_document(recipe: Recipe, tmp_path: Path,
                                                   fake_gh_dir: Path):
    """JSON-output flag ⇒ stdout is exactly one JSON document, on every terminal path."""
    if recipe.skip_if:
        pytest.skip(f"{recipe.id}: {recipe.skip_reason}")
    if recipe.needs_gh and os.name == "nt":
        pytest.skip(
            f"{recipe.id}: the fake-gh shim cannot intercept on Windows — Python "
            f"subprocess resolves a bare `gh` to `gh.exe` and ignores PATHEXT, so "
            f"the .bat/POSIX shim is bypassed and the REAL gh would run (network / "
            f"auth). Exercised on POSIX (Linux CI / dev container), where this gate "
            f"is authoritative."
        )

    proc = _run(recipe, tmp_path, fake_gh_dir)
    stdout = proc.stdout.decode("utf-8", "replace")
    stderr = proc.stderr.decode("utf-8", "replace")

    def fail(why: str) -> str:
        return (
            f"\n--json stdout contract VIOLATED\n"
            f"  tool  : dx/{recipe.tool}.py\n"
            f"  mode  : {recipe.mode}\n"
            f"  reason: {why}\n"
            f"  exit  : {proc.returncode}\n"
            f"  stdout[:200]: {stdout[:200]!r}\n"
            f"  stderr[:200]: {stderr[:200]!r}\n"
            f"  fix   : route every human-readable line to stderr; emit the one "
            f"JSON document to stdout on THIS path too.\n"
        )

    if recipe.expect_caller_error:
        assert proc.returncode == EXIT_CALLER_ERROR, fail(
            f"contradictory flags must be rejected with EXIT_CALLER_ERROR "
            f"({EXIT_CALLER_ERROR}), got {proc.returncode}"
        )
        assert not stdout.strip(), fail(
            "a rejected invocation must not write anything to stdout"
        )
        return

    assert stdout.strip(), fail("stdout is EMPTY — no JSON document at all")

    try:
        json.loads(stdout)
    except json.JSONDecodeError as e:
        raise AssertionError(fail(
            f"stdout is not a single JSON document ({e}). Either prose is mixed "
            f"in with the JSON, or the path emits no JSON at all."
        )) from None

    if recipe.expect_exit is not None:
        assert proc.returncode == recipe.expect_exit, fail(
            f"expected exit {recipe.expect_exit}, got {proc.returncode}"
        )
