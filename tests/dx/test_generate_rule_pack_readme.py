"""Regression tests for scripts/tools/dx/generate_rule_pack_readme.py.

Pins the two failure modes that left rule-packs/README.md badly stale on main
behind a dead drift gate (the generator never ran successfully, so nobody
noticed):

1. **Path default** — the argparse ``--rule-packs-dir`` default must resolve to
   the real repo-root ``rule-packs/`` dir. The original bug used one too few
   ``.parent`` hops (``parents[2]`` → ``scripts/rule-packs/``, which does not
   exist), so every invocation exited 2 (FileNotFoundError) and the
   ``validate_all`` ``--check`` gate silently no-op'd.

2. **Prose pack-count** — the header / "Dynamic Runbook Injection" "N 個 Rule
   Pack" figure must be DERIVED and equal the AUTHORITATIVE count
   (``validate_docs_versions.count_rule_packs``), not a hardcoded literal that
   silently goes stale (it was frozen at "11" while the platform shipped 15).

conftest.py already puts scripts/tools{,/dx,/lint} on sys.path, so the tool
modules import directly.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import generate_rule_pack_readme as grpr  # noqa: E402  (path via conftest)
import validate_docs_versions as vdv  # noqa: E402  (path via conftest)

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATOR = REPO_ROOT / "scripts" / "tools" / "dx" / "generate_rule_pack_readme.py"


def test_default_rule_packs_dir_resolves_to_repo_root():
    """Regression: parents[3] (repo root), not parents[2] (scripts/)."""
    default = grpr.DEFAULT_RULE_PACKS_DIR
    assert default == REPO_ROOT / "rule-packs"
    assert default.is_dir(), f"{default} is not a directory (path-default bug?)"
    # ...and it actually holds rule-pack YAMLs, so generate_table_rows won't bail.
    assert list(default.glob("rule-pack-*.yaml")), "no rule-pack-*.yaml under default dir"


def test_prose_pack_count_is_derived_and_matches_authoritative():
    """Prose count derives from the authoritative pack SET, not a literal.

    count_preloaded_packs (configmap set − custom-alerts) must equal
    validate_docs_versions.count_rule_packs()['pack_count'] (rule-packs ∪
    configmaps − custom-alerts) — the same set, so they must agree now and as
    packs are added. (Guards against the old hardcoded "11" / "15" and against
    a derivation that only coincidentally lands on the right number.)
    """
    generated = grpr.count_preloaded_packs(grpr.DEFAULT_RULE_PACKS_DIR)
    authoritative = vdv.count_rule_packs()["pack_count"]
    assert generated == authoritative, (
        f"prose pack-count {generated} != authoritative {authoritative}"
    )


def test_check_mode_passes_with_default_dir():
    """End-to-end: ``--check`` with the default dir exits 0 (gate live + in sync).

    Before the path-default fix this exited 2 (FileNotFoundError) regardless of
    drift — the exact symptom that made the validate_all gate dead.
    """
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--check"],
        capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=60,
    )
    assert result.returncode == 0, (
        f"--check exited {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
