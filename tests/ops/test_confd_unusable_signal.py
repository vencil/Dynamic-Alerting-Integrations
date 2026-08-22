"""Two conf.d readers, one answer — and neither goes quiet (#1469, #1468).

`tests/shared/test_confd_enumeration_contract.py` already pins *how* a
reader may enumerate (recursive, or flat and loud about it). It says
nothing about the readers agreeing on WHICH entries are config files, and
they did not:

    conf.d/beta.yaml as a DIRECTORY          validate-config   generate-routes
    -------------------------------------    ---------------   ---------------
    in the file population                   no                YES  ← #1469
    named in the output                      no                yes

Measured on the base commit of this branch. `_parse_config_files` listed
the directory, tried to `open()` it, and reported `WARN: skip beta.yaml`;
`check_yaml_syntax` walked the same tree through `iter_config_files` and
never saw it at all.

⛔ The fix is BOTH halves, and this file exists because the first half
alone makes things worse: unifying the selection removes the disagreement
by making *both* readers silent — one signal fewer than before. So these
tests assert two things at once, and the second is the one that fails if
someone later "simplifies" `unusable_config_paths` away:

  1. the two populations are equal, and
  2. both readers still NAME the entry that was dropped.

#1468's `resolve_inheritance_chain` is the same defect one directory over:
a chain missing its tenant layer, rc=0, zero bytes of stderr.
"""

from __future__ import annotations

import contextlib
import io
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "tools"))

import diagnose  # noqa: E402
import validate_config as vc  # noqa: E402
from _grar_parse import _parse_config_files  # noqa: E402
from _lib_confd import iter_config_files  # noqa: E402


@pytest.fixture()
def confd_with_dir_named_yaml(tmp_path: pathlib.Path) -> pathlib.Path:
    """Exactly the reproduction in #1469."""
    root = tmp_path / "conf.d"
    root.mkdir()
    (root / "beta.yaml").mkdir()
    (root / "_defaults.yaml").write_text(
        "defaults:\n  mysql_threads_running: 80\n", encoding="utf-8")
    (root / "acme.yaml").write_text(
        "tenants:\n  acme:\n    mysql_threads_running: 90\n", encoding="utf-8")
    return root


def _grar_population_and_stderr(root: pathlib.Path):
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        _parse_config_files(str(root))
    return err.getvalue()


def test_both_readers_see_the_same_files(confd_with_dir_named_yaml):
    """The populations, not just the verdicts, must match."""
    root = confd_with_dir_named_yaml
    recursive = [p.relative_to(root).as_posix()
                 for p in iter_config_files(root)]
    flat = [p.name for p in iter_config_files(root, recursive=False)]
    assert recursive == flat == ["_defaults.yaml", "acme.yaml"], (
        "a directory named beta.yaml is in one reader's population again")


def test_validate_config_names_the_unusable_entry(confd_with_dir_named_yaml):
    """#1469 half two: silence here was the pre-fix state."""
    result = vc.check_yaml_syntax(str(confd_with_dir_named_yaml))
    assert result["status"] == vc.FAIL
    assert "beta.yaml" in result["unusable_files"]
    assert any("beta.yaml" in d for d in result["details"])


def test_routing_parser_still_names_the_unusable_entry(confd_with_dir_named_yaml):
    """#1469 half two, other reader: it said this BEFORE the fix.

    Losing this line would be a regression dressed up as a cleanup.
    """
    stderr = _grar_population_and_stderr(confd_with_dir_named_yaml)
    assert "WARN: skip beta.yaml" in stderr
    # …and it must be the SHARED sentence, not this reader's own guess.
    # Before #1469 this line read "could not be read — IsADirectoryError:
    # [Errno 21] …", i.e. an errno from an `open()` that should never have
    # been attempted; asserting the wording is what makes a revert to
    # `os.listdir()` + `open()` fail here instead of passing quietly.
    assert "is a directory, not a config file" in stderr


def test_routing_parser_stays_flat(tmp_path: pathlib.Path):
    """⛔ `recursive=False` is load-bearing, not incidental.

    generate-routes emits routes for the tenants this reader returns.
    Recursing would silently widen that set — a behaviour change, which
    #1469 explicitly is not.
    """
    root = tmp_path / "conf.d"
    (root / "team-a").mkdir(parents=True)
    (root / "acme.yaml").write_text(
        "tenants:\n  acme:\n    mysql_threads_running: 90\n", encoding="utf-8")
    (root / "team-a" / "deep.yaml").write_text(
        "tenants:\n  deep:\n    mysql_threads_running: 91\n", encoding="utf-8")
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        parsed = _parse_config_files(str(root))
    assert parsed["all_tenants"] == ["acme"], (
        "the flat routing parser started reading subdirectories")
    # …and it says so, which is the #1339 contract this must not break.
    assert "team-a/deep.yaml" in err.getvalue()


def test_flat_reader_ordering_is_unchanged(tmp_path: pathlib.Path):
    """`sorted(os.listdir())` → `iter_config_files(recursive=False)`.

    Both sort on the bare NAME, so the swap is order-preserving. Pinned
    because "first file wins" decides which `_defaults.yaml` a merge sees.
    """
    root = tmp_path / "conf.d"
    root.mkdir()
    for n in ("Zulu.yaml", "_defaults.yaml", "acme.yml", "Alpha.yaml", "b.yaml"):
        (root / n).write_text("defaults: {}\n", encoding="utf-8")
    assert [p.name for p in iter_config_files(root, recursive=False)] == sorted(
        f.name for f in root.iterdir())


# ── #1468: diagnose returned a truncated chain in silence ───────────────


def _broken_confd(root: pathlib.Path) -> pathlib.Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "_defaults.yaml").write_text(
        "defaults:\n  mysql_threads_running: 80\n", encoding="utf-8")
    # one missing `]` — parses on the defaults file, not on this one
    (root / "acme.yaml").write_text(
        "tenants:\n  acme:\n    mysql_threads_running: [90\n", encoding="utf-8")
    return root


def test_diagnose_reports_the_file_it_could_not_read(tmp_path: pathlib.Path):
    root = _broken_confd(tmp_path / "conf.d")
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        chain = diagnose.resolve_inheritance_chain("acme", str(root))
    # The truncation itself is unchanged — the chain really is short.
    assert [c["layer"] for c in chain["chain"]] == ["defaults"]
    # L1: stderr names the file (0 bytes was the whole bug).
    assert "WARN: skip acme.yaml" in err.getvalue()
    # L2: and it is in the JSON, under validate-config's field name.
    assert chain["skipped_unusable_files"] == ["acme.yaml"]


def test_diagnose_summary_carries_the_caveat(tmp_path: pathlib.Path):
    """`check()` publishes the SUMMARY, so the caveat has to survive it.

    `batch_diagnose.py` imports `check` in-process and reads its stdout;
    a field that stops at `resolve_inheritance_chain` never reaches it.
    """
    root = _broken_confd(tmp_path / "conf.d")
    with contextlib.redirect_stderr(io.StringIO()):
        chain = diagnose.resolve_inheritance_chain("acme", str(root))
    summary = diagnose._format_chain_summary(chain)
    assert summary["skipped_unusable_files"] == ["acme.yaml"]


def test_diagnose_healthy_config_carries_no_caveat(tmp_path: pathlib.Path):
    """The other half: a caveat that shows up on clean runs is noise.

    Without this, "always emit the field" passes every test above.
    """
    root = tmp_path / "conf.d"
    root.mkdir()
    (root / "_defaults.yaml").write_text(
        "defaults:\n  mysql_threads_running: 80\n", encoding="utf-8")
    (root / "acme.yaml").write_text(
        "tenants:\n  acme:\n    mysql_threads_running: 90\n", encoding="utf-8")
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        chain = diagnose.resolve_inheritance_chain("acme", str(root))
    assert [c["layer"] for c in chain["chain"]] == ["defaults", "tenant"]
    assert "skipped_unusable_files" not in chain
    assert err.getvalue() == ""
    assert "skipped_unusable_files" not in diagnose._format_chain_summary(chain)


def test_all_three_readers_name_it_in_the_same_words(confd_with_dir_named_yaml):
    """The third reader is `diagnose`, and it used to phrase this its own way.

    #1469 unified the SELECTION predicate across `validate-config` and the
    routing parser. `diagnose` kept a fourth hand-rolled copy
    (`base.iterdir()` + inline suffix/hidden checks), so a config-named
    directory reached its `open()` and surfaced as a raw
    `IsADirectoryError: [Errno 21] Is a directory: /abs/path/...` — a
    different sentence, carrying an absolute path, for the same condition
    the other two now describe identically.

    ⛔ That is the same defect class one layer up: not a wrong answer, but
    three readers giving three accounts of one directory. Wording is the
    part a reader actually sees, so it is pinned here — if someone reverts
    `diagnose` to its own predicate, the raw exception text comes back and
    this fails.
    """
    root = confd_with_dir_named_yaml

    grar_err = io.StringIO()
    with contextlib.redirect_stderr(grar_err):
        _parse_config_files(str(root))

    diag_err = io.StringIO()
    with contextlib.redirect_stderr(diag_err):
        chain = diagnose.resolve_inheritance_chain("acme", str(root))

    vc_report = vc.check_yaml_syntax(str(root))

    phrase = "is a directory, not a config file"
    grar_line = [ln for ln in grar_err.getvalue().splitlines() if "beta.yaml" in ln]
    diag_line = [ln for ln in diag_err.getvalue().splitlines() if "beta.yaml" in ln]
    vc_line = [d for d in vc_report["details"] if "beta.yaml" in d]

    assert grar_line, "routing parser went silent about beta.yaml"
    assert diag_line, "diagnose went silent about beta.yaml"
    assert vc_line, "validate-config went silent about beta.yaml"

    for label, lines in (("grar", grar_line), ("diagnose", diag_line), ("validate-config", vc_line)):
        assert phrase in lines[0], f"{label} phrased it differently: {lines[0]!r}"
        # The raw exception leaked an absolute path; the shared wording must not.
        assert "Errno" not in lines[0], f"{label} leaked the raw exception: {lines[0]!r}"
        assert str(root) not in lines[0], f"{label} leaked an absolute path: {lines[0]!r}"

    # L2: the same entry is machine-readable, not just printed.
    assert chain.get("skipped_unusable_files") == ["beta.yaml"]


def test_diagnose_shares_the_selection_predicate(confd_with_dir_named_yaml):
    """`diagnose` must not re-derive "what counts as a config file".

    Pinned as a POPULATION equality rather than by reading the source: the
    point is not which function is called, it is that a fourth predicate
    cannot drift away from the shared one without this failing.
    """
    root = confd_with_dir_named_yaml
    shared = sorted(p.name for p in iter_config_files(root, recursive=False))

    with contextlib.redirect_stderr(io.StringIO()):
        chain = diagnose.resolve_inheritance_chain("acme", str(root))

    # Everything diagnose either used or explicitly dropped, together, must
    # be exactly the shared population plus the unusable entries it named.
    accounted = set(chain.get("skipped_unusable_files") or [])
    assert "beta.yaml" in accounted
    assert "beta.yaml" not in shared
    assert shared == ["_defaults.yaml", "acme.yaml"]


# ── the two remaining silent paths in `resolve_inheritance_chain` ────────
#
# Coverage showed these two `_skip` calls untested. They are not filler:
# each is a distinct way for the chain to come back short, and #1468 is
# precisely "the chain came back short and nothing said why". An untested
# signal path is one refactor away from being silent again.


def test_diagnose_names_a_tenant_file_that_is_not_a_mapping(tmp_path: pathlib.Path):
    """A bare YAML list parses fine and then has no `tenants:` to read.

    Distinct from the unparseable case: nothing raises, so an early version
    of this loop would simply move on and hand back a chain missing its
    tenant layer — the #1468 symptom exactly, reached by a different door.
    """
    root = tmp_path / "conf.d"
    root.mkdir()
    (root / "_defaults.yaml").write_text(
        "defaults:\n  mysql_threads_running: 80\n", encoding="utf-8")
    (root / "acme.yaml").write_text("- not\n- a\n- mapping\n", encoding="utf-8")

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        chain = diagnose.resolve_inheritance_chain("acme", str(root))

    assert "WARN: skip acme.yaml" in err.getvalue()
    assert "top level must be a mapping" in err.getvalue()
    assert "list" in err.getvalue()
    assert chain["skipped_unusable_files"] == ["acme.yaml"]


def test_diagnose_names_an_unreadable_profiles_file(tmp_path: pathlib.Path):
    """`_profiles.yaml` is the profile layer's only source.

    If it cannot be parsed the chain silently loses that layer, and the
    tenant's `_profile` reference resolves to nothing. The tenant file here
    is deliberately VALID so the only thing wrong is the profiles file —
    otherwise this test would pass for the wrong reason.
    """
    root = tmp_path / "conf.d"
    root.mkdir()
    (root / "_defaults.yaml").write_text(
        "defaults:\n  mysql_threads_running: 80\n", encoding="utf-8")
    (root / "acme.yaml").write_text(
        "tenants:\n  acme:\n    _profile: gold\n    mysql_threads_running: 90\n",
        encoding="utf-8")
    (root / "_profiles.yaml").write_text(
        "profiles:\n  gold: [unclosed\n", encoding="utf-8")

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        chain = diagnose.resolve_inheritance_chain("acme", str(root))

    assert "WARN: skip _profiles.yaml" in err.getvalue()
    assert chain["skipped_unusable_files"] == ["_profiles.yaml"]
    # Control: the tenant layer is intact, so the caveat is about the
    # profiles file alone and not a side effect of a broken tenant file.
    assert "tenant" in [c["layer"] for c in chain["chain"]]
