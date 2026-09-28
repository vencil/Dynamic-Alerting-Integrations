"""
Golden parity test for describe_tenant.py deep_merge + inheritance.

This test is the trump card for ADR-017 semantic verification:
- Runs describe_tenant.py against every scenario captured in golden.json
- Compares source_hash + merged_hash + effective_config against golden.json
- If any hash diverges, either:
    (a) Python describe_tenant.py logic changed → bug or intentional update (regen golden)
    (b) A Go port produces different hashes → semantic drift, fix the Go side

What the fixtures cover (NOT every ADR-017 clause; see "Known gaps" below):
- flat:              no defaults chain
- l0-only:           root _defaults + tenant override (scalar)
- full-l0-l3:        4-level inheritance, array replace, tenant override
- mixed-mode:        flat + hierarchical tenants in same conf.d
- array-replace:     arrays replaced (not concat)
- opt-out-null:      null on a NON-reserved key (a scalar and a nested
                     threshold key) does not delete it: the inherited
                     default survives. The "null deletes a reserved `_`
                     key" branch is reserved-null-delete's, not this one's.
- opt-out-null-threshold: real flat-metric-key shape — null keeps the
                     inherited default, "disable" is the opt-out (#1339)
- null-body:         a tenant declared with a null body (`tenant-x:` and
                     nothing under it) inherits every default (#1677 F2);
                     lives in the opt-out-null-threshold tree as its own file
- metadata-skipped:  _metadata never propagates
- wrapper-siblings:  `defaults:` wrapper WITH sibling top-level keys — the
                     shape the shipped platform file has. See
                     build_and_capture.py for what it does and does not buy.
- carrier-selection: ONE defaults carrier per directory (#1674) — a
                     `_defaults.yaml` + `_defaults.yml` pair reads the
                     `.yaml` only, and a subtree `_DEFAULTS.YML` enters the
                     chain; two tenants in the mixed-mode tree's `carrier/`
                     subtree (no new conf.d root)
- canonical-json-escaping: a tenant string with `<` `>` `&` and CJK, so
                     the ensure_ascii=False (Python) and no-HTML-escape (Go)
                     clauses of the canonical JSON move merged_hash (#1550)
- reserved-nested-null: null on non-reserved sub-keys of an inherited
                     reserved key (`_x.*`): an inherited value is retained,
                     an uninherited null is dropped (#1550; was `_routing`
                     until #2417, which da-guard rejects in `defaults:`)
- reserved-null-delete: a `_` key inherited from L0 and nulled in an L1
                     `_defaults.yaml` is deleted; a sibling `_` key survives
                     (#1550)
The last three are subtrees of the mixed-mode tree, like carrier-selection.

Chain discovery is checked on both sides from the tree itself: here by
describe_tenant, and on the Go side by TestGoldenParity_ScannerChainOrder
(the exporter's /metrics chain) and TestGoldenParity_ResolveEffective
(pkg/config ResolveEffective, behind tenant-api `/effective`).

Known gaps (a mutation there leaves this oracle green):
- ADR-017's `_routing` null opt-out is enforced by the route generator
  (_grar_merge.py over `_routing_defaults` + the tenant file's `_routing`),
  which neither merge implementation runs; no golden row exercises
  `_routing` (#2417).
- Go's EffectiveConfig leg compares Go canonicalJSON against Go
  canonicalJSON, so it is blind to a Go-side escaping change; the hash legs
  (MergedHash, ResolveEffective) are what catch that.

What IS guarded beyond the hashes: test_fixture_trees_have_no_orphans
fails on any yaml under a fixture conf.d tree that golden.json does not
declare (#1551), since describe_tenant's recursive scan would read it; and
test_parity_runs_every_golden_case_on_every_platform fails if the parity
test grows a skip mark or a golden case points at a missing fixture tree
(#1550 item 2: it used to be skipped wholesale on Windows over a
path-separator field, taking the hash assertions with it).

Regenerate golden.json by running tests/golden/build_and_capture.py after
intentional semantic changes.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from _tree import repo_files

HERE = Path(__file__).parent
REPO_ROOT = HERE.parent.parent
DESCRIBE = REPO_ROOT / "scripts" / "tools" / "dx" / "describe_tenant.py"
GOLDEN = json.loads((HERE / "golden.json").read_text(encoding="utf-8"))

# Keys every golden entry must carry for the parity assertions to mean
# anything. build_and_capture.py writes an entry with only `error` when a
# scenario fails to regenerate.
_REQUIRED_KEYS = ("source_file", "source_hash", "merged_hash",
                  "effective_config", "defaults_chain")

# Files under a fixture conf.d tree that golden.json intentionally does NOT
# declare (neither a scenario's source_file nor a defaults_chain entry).
# Each needs a reason; anything not declared and not listed here is an
# orphan. Paths are relative to tests/golden/fixtures/, POSIX.
_INTENTIONAL_EXTRAS = {
    # carrier-selection (#1674): the losing half of a `_defaults.yaml` +
    # `_defaults.yml` pair. Its presence IS the scenario: both sides must
    # read the `.yaml` only, so this file must exist and stay out of every
    # chain.
    "mixed-mode/conf.d/carrier/_defaults.yml",
}


def _fixture_path(fixture_dir: str) -> Path:
    return HERE / "fixtures" / fixture_dir / "conf.d"


def _run_describe(conf_d: Path, tenant_id: str) -> dict:
    """Invoke describe_tenant.py as a subprocess; return parsed JSON output."""
    cmd = [
        sys.executable,
        str(DESCRIBE),
        tenant_id,
        "--conf-d", str(conf_d),
        "--show-sources",
        "--format", "json",
    ]
    # encoding='utf-8' so Windows hosts (where Python's default text-mode
    # decoder is the system codepage, e.g. cp950) decode the child Python's
    # UTF-8 output correctly. Pairs with the session-scoped
    # PYTHONIOENCODING=utf-8 fixture in tests/conftest.py.
    result = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    if result.returncode != 0:
        pytest.fail(f"describe_tenant failed for {tenant_id}: {result.stderr}")
    return json.loads(result.stdout)


@pytest.mark.parametrize("golden", GOLDEN, ids=lambda g: f"{g['scenario']}/{g['tenant_id']}")
def test_merge_parity_python(golden: dict):
    """Verify current describe_tenant output matches captured golden hashes.

    This guards against:
    - Accidental changes to deep_merge semantics
    - Changes to canonical JSON representation (separator/sort/encoding)
    - Changes to SHA-256 truncation (currently 16 hex chars)
    """
    # A scenario build_and_capture failed to regenerate is written with only
    # an `error` field. Say so, instead of a KeyError on the first hash
    # comparison that reads like a hash mismatch (#1551).
    missing = [k for k in _REQUIRED_KEYS if k not in golden]
    if missing:
        pytest.fail(
            f"golden 是壞的: {golden.get('scenario')}/{golden.get('tenant_id')} "
            f"lacks {missing}"
            + (f"; captured error: {golden['error'][:200]}" if "error" in golden else "")
            + ". Fix the scenario and re-run build_and_capture.py (it exits "
              "non-zero when any scenario fails)."
        )

    conf_d = _fixture_path(golden["fixture_dir"])
    assert conf_d.exists(), f"Fixture dir missing: {conf_d}"

    result = _run_describe(conf_d, golden["tenant_id"])

    assert result["source_hash"] == golden["source_hash"], \
        f"source_hash drift for {golden['scenario']}"
    assert result["merged_hash"] == golden["merged_hash"], \
        f"merged_hash drift for {golden['scenario']}"
    assert result["effective_config"] == golden["effective_config"], \
        f"effective_config drift for {golden['scenario']}"
    assert result["defaults_chain"] == golden["defaults_chain"], \
        f"defaults_chain order drift for {golden['scenario']}"
    # Same `/` contract as defaults_chain (#1550): both go through
    # describe_tenant's _report_path.
    assert result["source_file"] == golden["source_file"], \
        f"source_file drift for {golden['scenario']}"


def test_parity_runs_every_golden_case_on_every_platform():
    """Floor for test_merge_parity_python (#1550 item 2).

    It was once skipped wholesale on Windows over a path-separator field,
    and the three hash / config assertions went with it while the run
    stayed green. Fail if it grows any mark besides its parametrize (a
    module-level `pytestmark` included), or if a golden case points at a
    fixture tree that does not exist.
    """
    assert GOLDEN, "golden.json has no cases"
    marks = [m.name for m in getattr(test_merge_parity_python, "pytestmark", [])]
    assert marks == ["parametrize"], f"parity test must run everywhere; marks: {marks}"
    assert "pytestmark" not in globals(), "a module-level pytestmark reaches the parity test"
    missing = sorted({g["fixture_dir"] for g in GOLDEN
                      if not _fixture_path(g["fixture_dir"]).is_dir()})
    assert not missing, f"golden cases point at missing fixture trees: {missing}"


def _fixture_tree_yaml() -> set[str]:
    """Every yaml under tests/golden/fixtures/*/conf.d/, fixtures-relative.

    Uses tests/_tree.py (git ls-files: tracked plus untracked-not-ignored),
    so a stray file is seen before it is `git add`-ed. The suffix check is
    case-insensitive because describe_tenant reads `_DEFAULTS.YML` too.
    """
    fixtures = (HERE / "fixtures").resolve()
    out: set[str] = set()
    for p in repo_files():
        try:
            rel = p.resolve().relative_to(fixtures)
        except ValueError:
            continue
        parts = rel.parts
        if len(parts) < 3 or parts[1] != "conf.d":
            continue
        if p.suffix.lower() in (".yaml", ".yml"):
            out.add(rel.as_posix())
    return out


def _declared_by_golden() -> set[str]:
    """source_file ∪ defaults_chain of every golden entry, fixtures-relative."""
    out: set[str] = set()
    for g in GOLDEN:
        base = f"{g['fixture_dir']}/conf.d"
        if g.get("source_file"):
            out.add(f"{base}/{g['source_file']}")
        for rel in g.get("defaults_chain") or []:
            out.add(f"{base}/{rel}")
    return out


def test_fixture_trees_have_no_orphans():
    """The fixture trees hold exactly what golden.json declares (#1551).

    describe_tenant scans conf.d recursively, so an undeclared yaml (a file
    a builder stopped writing that reset() could not delete, or a stray
    sub-directory with its own `_defaults.yaml`) can change a fixture's
    merge result while every hash assertion keeps reading only the paths
    golden.json names. Both directions are checked: undeclared files on
    disk, and declared or allow-listed files that are gone.
    """
    on_disk = _fixture_tree_yaml()
    declared = _declared_by_golden()
    assert on_disk, "enumerated zero fixture yaml; the scan ran on the wrong tree"

    orphans = sorted(on_disk - declared - _INTENTIONAL_EXTRAS)
    missing = sorted(declared - on_disk)
    stale_allow = sorted(_INTENTIONAL_EXTRAS - on_disk)
    shadowed_allow = sorted(_INTENTIONAL_EXTRAS & declared)
    problems = []
    if orphans:
        problems.append(
            "orphan yaml under a fixture conf.d tree, not declared by "
            f"golden.json nor allow-listed: {orphans}")
    if missing:
        problems.append(f"golden.json declares files that are not in the tree: {missing}")
    if stale_allow:
        problems.append(f"_INTENTIONAL_EXTRAS names files that do not exist: {stale_allow}")
    if shadowed_allow:
        problems.append(
            f"_INTENTIONAL_EXTRAS names files golden.json declares: {shadowed_allow}")
    assert not problems, "\n".join(problems)


# -------------------------------------------------------------------------
# Go parity lives in the Go tree, not here.
# -------------------------------------------------------------------------
#
# A `test_merge_parity_go` used to sit here, shelling out to
# `components/threshold-exporter/bin/threshold-exporter dump-merged`.
# Removed: nothing in the repo builds that path (its own docstring was the
# only mention of it), no `dump-merged` subcommand exists in the Go
# sources, and measured in the Dev Container — which does have Go — the
# whole parametrized set reported `skipped`. It asserted nothing while
# calling itself "the single most important test for ADR-017 conformance".
#
# The Go end of the parity contract is
# components/threshold-exporter/app/config_golden_parity_test.go, which
# reads this same golden.json directly and runs under `Go Tests`.
