"""Python half of the cross-language conf.d name-classification pin (#1537).

One conf.d tree, four independent enumerators, four hand-written name rules —
the #1339 defect class, this time with the file EXTENSION as the divergence
axis. Measured before this pin existed: a root-level ``upper.YAML`` was read
and served by the exporter, was invisible to BOTH Python readers (not yielded,
and not named as unusable either — so no report mentioned it at all), and was
rejected by the write plane, which then also left that live tenant out of the
set federation orphan detection subtracts against.

⛔ Nothing in this module asserts anything about Go, and nothing in it greps
Go source. This repo measured (#1448 blind review) that a Python guard
asserting things about Go source text goes red on legitimate Go refactors and
states the OPPOSITE of the truth when it does; the cheapest way back to green
was to undo a correct refactor. So the two Go readers are named here only in
prose, WITHOUT their paths — spelling a path would make ``verify_diff``
record an edge from that Go file to this test, i.e. tell whoever edits it to
run a test that cannot see it, which is #1448's shape rebuilt in the
dependency map. Their halves of this pin live beside the code they measure.

What every side asserts instead is
``tests/shared/confd_name_classification_matrix.json`` — spelled out here
because this module really does read it — which makes the four readers'
agreement transitive rather than claimed.

The rows are NAMES carrying orthogonal boolean properties, never an expected
file list: the four enumerators do not share a scope (one recurses, one is
flat with the reserved filter as a parameter, one is per-name, one routes
``_defaults.*`` into the inheritance chain), so a single expected-list column
would be a lie. Each consumer asserts only the projection it implements. The
two implemented here:

  * ``_lib_confd._is_config``       == yaml_extension AND NOT hidden
  * ``_lib_io.iter_yaml_files``     == yaml_extension AND NOT reserved_prefix
    (``skip_reserved=True``)           AND NOT hidden
  * ``_lib_io.iter_yaml_files``     == yaml_extension
    (``skip_reserved=False``)          — the parameter gates BOTH prefixes,
                                         so dotfiles come back; measured.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "tools"))

MATRIX = REPO / "tests" / "shared" / "confd_name_classification_matrix.json"

from _lib_confd import _is_config, iter_config_files  # noqa: E402
from _lib_io import iter_yaml_files  # noqa: E402

ROWS = json.loads(MATRIX.read_text(encoding="utf-8"))["rows"]

# Minimum row count. Below the shipped count on purpose — a redundant row may
# legitimately be dropped — but a matrix that has been gutted must not keep
# every consumer green while measuring nothing.
MIN_ROWS = 18


def _carrier(row: dict) -> bool:
    """A name the write plane may address as a tenant."""
    return (row["yaml_extension"]
            and not row["reserved_prefix"]
            and not row["hidden"])


def test_matrix_still_carries_the_shapes_it_exists_for() -> None:
    """A parity pin that measures nothing must fail loudly, not pass quietly.

    A bare count floor is not enough here: twenty all-lowercase rows would
    satisfy it while removing every case this pin exists for. So the floor
    also demands the load-bearing SHAPES, each of which was a live defect.
    """
    assert len(ROWS) >= MIN_ROWS, (
        f"matrix shrank to {len(ROWS)} rows (floor {MIN_ROWS}) — every consumer "
        "of this table would stay green while asserting almost nothing, which "
        "is the empty-set silence #1339 and #1537 are both about"
    )
    names = [r["name"] for r in ROWS]
    assert len(names) == len(set(names)), f"duplicate row names: {names}"

    def has(pred) -> bool:
        return any(pred(r) for r in ROWS)

    checks = {
        "a YAML name whose EXTENSION is not all-lowercase (the #1537 row — "
        "without it the whole pin is a lowercase-only tautology)":
            lambda r: r["yaml_extension"] and r["name"] != r["name"].lower(),
        "a non-lowercase spelling of the _defaults chain carrier (the exporter "
        "compares that name folded; a reader that folds only the extension "
        "hashes the file and then drops its defaults: block)":
            lambda r: r["defaults_file"] and r["name"] != r["name"].lower(),
        "a hidden YAML name (keeps `hidden` from being conflated with "
        "`yaml_extension`)":
            lambda r: r["hidden"] and r["yaml_extension"],
        "a reserved YAML name that is NOT the defaults carrier (separates the "
        "two reserved sub-cases)":
            lambda r: (r["reserved_prefix"] and r["yaml_extension"]
                       and not r["defaults_file"]),
        "a non-YAML name (otherwise nothing pins that the extension test "
        "refuses anything)":
            lambda r: not r["yaml_extension"],
        "a name containing '.yaml' somewhere other than the end (the shape a "
        "contains-style test silently promotes back into live config)":
            lambda r: (".yaml" in r["name"].lower()
                       and not r["yaml_extension"]),
        # ⛔ ONE PER EXTENSION BRANCH, and that is a dogfood finding rather than
        # symmetry for its own sake: the id derivation has a separate branch per
        # extension, and while the matrix carried a mixed-case stem only on the
        # .yml row, corrupting the .yaml branch alone went completely
        # undetected — every .yaml carrier happened to have a lowercase stem.
        "a .yaml tenant carrier whose stem is not all-lowercase (pins that "
        "folding the extension must not RENAME the tenant, on the .yaml branch)":
            lambda r: (_carrier(r) and r["name"].lower().endswith(".yaml")
                       and r["stem"] != r["stem"].lower()),
        "a .yml tenant carrier whose stem is not all-lowercase (same pin, on "
        "the .yml branch — a fix applied to one branch only escapes otherwise)":
            lambda r: (_carrier(r) and r["name"].lower().endswith(".yml")
                       and r["stem"] != r["stem"].lower()),
    }
    missing = [why for why, pred in checks.items() if not has(pred)]
    assert not missing, (
        "the matrix no longer contains rows for: " + "; ".join(missing)
    )


def test_matrix_property_columns_are_self_consistent() -> None:
    """The derived columns must follow from the name, or a row is guesswork.

    ``defaults_file`` and ``stem`` are not free variables — they are functions
    of the name. Checking them here means a hand-added row cannot smuggle in a
    wrong expectation that then teaches every consumer the wrong rule.
    """
    for r in ROWS:
        name = r["name"]
        low = name.lower()
        assert r["yaml_extension"] == (low.endswith(".yaml") or low.endswith(".yml")), name
        assert r["reserved_prefix"] == name.startswith("_"), name
        assert r["hidden"] == name.startswith("."), name
        assert r["defaults_file"] == (low in ("_defaults.yaml", "_defaults.yml")), name
        if r["defaults_file"]:
            assert r["reserved_prefix"] and r["yaml_extension"], (
                f"{name}: defaults_file must imply reserved_prefix AND yaml_extension"
            )
        if _carrier(r):
            expected = name[:-5] if low.endswith(".yaml") else name[:-4]
            assert r["stem"] == expected, (
                f"{name}: stem must be the name minus its extension with the "
                f"stem's ORIGINAL CASE preserved — want {expected!r}, got {r['stem']!r}"
            )
        else:
            assert r["stem"] == "", f"{name}: a non-carrier has no tenant id"


@pytest.mark.parametrize("row", ROWS, ids=[r["name"] for r in ROWS])
def test_is_config_matches_the_matrix(row: dict) -> None:
    """`_is_config` implements exactly `yaml_extension AND NOT hidden`."""
    want = row["yaml_extension"] and not row["hidden"]
    got = _is_config(row["name"])
    assert got == want, (
        f"{row['name']}: _lib_confd._is_config returned {got}, matrix says {want}\n"
        f"  why this row exists: {row['why']}"
    )


def _fs_is_case_insensitive(tmp_path: pathlib.Path) -> bool:
    probe = tmp_path / "_case_probe_lower"
    probe.write_text("x", encoding="utf-8")
    return (tmp_path / "_CASE_PROBE_LOWER").exists()


@pytest.fixture()
def materialised(tmp_path: pathlib.Path) -> pathlib.Path:
    """A flat conf.d holding one file per matrix row.

    CONTENT is held constant — every file is the same valid single-tenant
    document — so the only thing that varies across rows is the NAME. That is
    what makes the enumerators' answers attributable to the classification
    rule rather than to parsing.
    """
    if _fs_is_case_insensitive(tmp_path):
        pytest.skip(
            "filesystem is case-insensitive, so rows differing only by case "
            "(_defaults.yaml/_DEFAULTS.YAML, .hidden.yaml/.HIDDEN.YAML) cannot "
            "coexist: these enumerators CANNOT BE MEASURED here. That is a "
            "different outcome from measuring them and finding them correct — "
            "the predicate-level rows in this module still ran."
        )
    root = tmp_path / "conf.d"
    root.mkdir()
    for i, row in enumerate(ROWS):
        (root / row["name"]).write_text(f"tenants:\n  t{i}: {{}}\n", encoding="utf-8")
    on_disk = {p.name for p in root.iterdir()}
    missing = sorted({r["name"] for r in ROWS} - on_disk)
    assert not missing, (
        f"fixture did not materialise {missing} — the assertions below would "
        "have been silently weaker than they claim to be"
    )
    return root


def test_iter_config_files_matches_the_matrix(materialised: pathlib.Path) -> None:
    """End-to-end: the enumerator must agree with its own predicate.

    `_is_config` being right is not the same claim as `iter_config_files`
    consulting it — the recursive and flat branches of that generator have
    already drifted apart once (#1469, over the regular-file check).
    """
    got = {p.name for p in iter_config_files(materialised)}
    want = {r["name"] for r in ROWS if r["yaml_extension"] and not r["hidden"]}
    assert got == want, (
        f"iter_config_files disagrees with the matrix\n"
        f"  missing (matrix says read, enumerator skipped): {sorted(want - got)}\n"
        f"  extra   (enumerator read, matrix says skip):    {sorted(got - want)}"
    )


def test_iter_yaml_files_skip_reserved_matches_the_matrix(
    materialised: pathlib.Path,
) -> None:
    """Default mode: yaml_extension AND NOT reserved_prefix AND NOT hidden."""
    got = {n for n, _ in iter_yaml_files(str(materialised))}
    want = {r["name"] for r in ROWS if _carrier(r)}
    assert got == want, (
        f"iter_yaml_files(skip_reserved=True) disagrees with the matrix\n"
        f"  missing: {sorted(want - got)}\n"
        f"  extra:   {sorted(got - want)}"
    )


def test_iter_yaml_files_without_skip_reserved_matches_the_matrix(
    materialised: pathlib.Path,
) -> None:
    """`skip_reserved=False` drops the WHOLE prefix filter — dotfiles included.

    Asserted rather than assumed: the parameter reads as if it were about
    reserved control files only, and a reader who believed that would use it
    on a tree containing `.hidden.yaml` and get a file the exporter skips.
    """
    got = {n for n, _ in iter_yaml_files(str(materialised), skip_reserved=False)}
    want = {r["name"] for r in ROWS if r["yaml_extension"]}
    assert got == want, (
        f"iter_yaml_files(skip_reserved=False) disagrees with the matrix\n"
        f"  missing: {sorted(want - got)}\n"
        f"  extra:   {sorted(got - want)}"
    )
