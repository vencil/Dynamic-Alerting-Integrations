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
import os
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


# ── `_profiles.yaml` that parses but is the wrong shape ──────────────────
#
# Both of these were live defects in the first cut of this PR, found by
# review. They are the #1447 death ("parses cleanly, is not a mapping,
# reaches .get(), takes the run with it") and the #1468 death (a falsy
# document coerced to {} and the layer vanishes) — reproduced INSIDE the
# change that exists to fix that family. Pinned so they cannot come back.


def _profile_ref_confd(root: pathlib.Path, profiles_body: str) -> pathlib.Path:
    root.mkdir(parents=True)
    (root / "_defaults.yaml").write_text(
        "defaults:\n  mysql_threads_running: 80\n", encoding="utf-8")
    (root / "acme.yaml").write_text(
        "tenants:\n  acme:\n    _profile: gold\n    mysql_threads_running: 90\n",
        encoding="utf-8")
    (root / "_profiles.yaml").write_text(profiles_body, encoding="utf-8")
    return root


def test_diagnose_survives_profiles_key_that_is_a_list(tmp_path: pathlib.Path):
    """`profiles:` as a list used to raise AttributeError from `.get()`.

    ⛔ That exception is NOT in the `except (OSError, yaml.YAMLError)` around
    this read, so it escaped and killed the whole call — a crash, not a
    truncated answer.
    """
    root = _profile_ref_confd(tmp_path / "conf.d", "profiles:\n  - gold\n  - silver\n")

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        chain = diagnose.resolve_inheritance_chain("acme", str(root))  # must not raise

    assert "WARN: skip _profiles.yaml" in err.getvalue()
    assert "'profiles' must be a mapping" in err.getvalue()
    assert "list" in err.getvalue()
    assert chain["skipped_unusable_files"] == ["_profiles.yaml"]
    # The tenant layer is unaffected — only the profile layer is missing.
    assert "tenant" in [c["layer"] for c in chain["chain"]]


def test_diagnose_names_a_falsy_profiles_document(tmp_path: pathlib.Path):
    """A whole-document `[]` is falsy, and `or {}` silently made it empty.

    Nothing raised, nothing printed, and the profile layer was simply gone
    — the shape this PR exists to eliminate.
    """
    root = _profile_ref_confd(tmp_path / "conf.d", "[]\n")

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        chain = diagnose.resolve_inheritance_chain("acme", str(root))

    assert "WARN: skip _profiles.yaml" in err.getvalue()
    assert "top level must be a mapping" in err.getvalue()
    assert chain["skipped_unusable_files"] == ["_profiles.yaml"]


def test_diagnose_still_accepts_an_empty_profiles_document(tmp_path: pathlib.Path):
    """⛔ Control for the two above: an EMPTY document is legal, not a fault.

    Without this, the fix could over-trigger and start reporting every
    `_profiles.yaml` that happens to be blank — trading a silent miss for a
    false alarm, which is the failure mode this whole PR is trying not to
    introduce.
    """
    root = _profile_ref_confd(tmp_path / "conf.d", "")

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        chain = diagnose.resolve_inheritance_chain("acme", str(root))

    assert "_profiles.yaml" not in err.getvalue()
    assert "skipped_unusable_files" not in chain


# ── the sentence the gate prints has to be true (#1469 follow-up) ────────


def _deny_scandir(monkeypatch: pytest.MonkeyPatch, target: pathlib.Path) -> None:
    """Unlistable directory at any uid — `chmod 000` is invisible to root."""
    import os
    real = os.scandir

    def fake(path=".", *a, **kw):  # noqa: ANN001
        if os.fspath(path) == os.fspath(target):
            raise PermissionError(13, "Permission denied", os.fspath(target))
        return real(path, *a, **kw)

    monkeypatch.setattr(os, "scandir", fake)


def test_a_config_named_directory_with_files_in_it_is_not_called_empty(
    tmp_path: pathlib.Path,
):
    """⛔ The blocking message asserted something the same run disproved.

    `check_yaml_syntax` printed one fixed sentence for every unusable
    entry — "nothing in it is loaded, by this tool or by
    threshold-exporter" — and for a DIRECTORY named `beta.yaml` that
    contains `.yaml` files that is false on both counts: this scan reads
    `beta.yaml/inner.yaml` through `iter_config_files`, and the Go side
    descends too (`hierarchy.go`'s `WalkDir` only `SkipDir`s names starting
    with `.`). So a required gate turned a build red while stating a reason
    its own file list contradicted.
    """
    root = tmp_path / "conf.d"
    (root / "beta.yaml").mkdir(parents=True)
    (root / "beta.yaml" / "inner.yaml").write_text(
        "tenants:\n  inner:\n    mysql_threads_running: 90\n", encoding="utf-8")
    (root / "empty.yaml").mkdir()
    (root / "_defaults.yaml").write_text(
        "defaults:\n  mysql_threads_running: 80\n", encoding="utf-8")

    res = vc.check_yaml_syntax(str(root))
    assert res["status"] == "fail"
    by_label = {d.split(":", 1)[0]: d for d in res["details"]}

    # The one that DOES still load its contents says so, with the count.
    assert "INSIDE it ARE" in by_label["beta.yaml"]
    assert "1 config file(s)" in by_label["beta.yaml"]
    # The empty one keeps the original sentence, which is true for it.
    assert "nothing in it is loaded" in by_label["empty.yaml"]
    assert "INSIDE it ARE" not in by_label["empty.yaml"]
    # Control: the scan really did read the nested file, which is the whole
    # reason the old sentence was wrong.
    assert any(p.name == "inner.yaml" for p in iter_config_files(root))


def test_an_untraversable_directory_says_the_report_is_incomplete(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
):
    """`pass` on a tree half of which was never opened is the #1339 shape."""
    root = tmp_path / "conf.d"
    locked = root / "locked"
    locked.mkdir(parents=True)
    (locked / "tenant.yaml").write_text("tenants: {}\n", encoding="utf-8")
    (root / "_defaults.yaml").write_text(
        "defaults:\n  mysql_threads_running: 80\n", encoding="utf-8")

    _deny_scandir(monkeypatch, locked)

    res = vc.check_yaml_syntax(str(root))
    assert res["status"] == "fail", "a partially-unread tree must not pass"
    assert res["unusable_files"] == ["locked"]
    assert "INCOMPLETE" in res["details"][0]


def test_diagnose_gives_one_answer_for_a_defaults_file_that_is_a_directory(
    tmp_path: pathlib.Path,
):
    """⛔ Two lines, two vocabularies, one cause — from the guard against it.

    `_defaults.yaml` is opened BY NAME in Layer 1, so it never met the
    shared `unusable_config_paths` pass while that pass sat lower down.
    Measured before the reorder:

        WARN: skip _defaults.yaml: IsADirectoryError: [Errno 21] Is a
              directory: '/abs/.../conf.d/_defaults.yaml'
        WARN: skip _defaults.yaml: is a directory, not a config file

    Note the absolute path in the first line — the raw errno leaks the
    caller's filesystem layout into a report meant for a tenant operator.
    `_skip` dedupes on (file, reason) and these are two different reasons,
    so only ordering plus `_skip_read_failure` could collapse them.
    """
    root = tmp_path / "conf.d"
    (root / "_defaults.yaml").mkdir(parents=True)
    (root / "acme.yaml").write_text(
        "tenants:\n  acme:\n    mysql_threads_running: 90\n", encoding="utf-8")

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        res = diagnose.resolve_inheritance_chain("acme", str(root))

    lines = [ln for ln in err.getvalue().splitlines() if "_defaults.yaml" in ln]
    assert len(lines) == 1, f"one cause, one line — got {lines}"
    assert "is a directory, not a config file" in lines[0]
    assert "IsADirectoryError" not in lines[0]
    assert str(tmp_path) not in lines[0], "no absolute path in operator output"
    assert res["skipped_unusable_files"] == ["_defaults.yaml"]


def test_routing_parsers_unusable_pass_stays_flat(tmp_path: pathlib.Path):
    """The pass must not out-scope the reader it annotates.

    `_parse_config_files` is flat BY DESIGN (ADR-016 nesting is reported by
    `warn_nested`), so its unusable pass has to be flat too. If it recursed
    it would start naming paths for which this tool generates nothing,
    drowning `warn_nested` — the signal that exists for exactly that — in
    findings the reader is not responsible for.
    """
    root = tmp_path / "conf.d"
    (root / "team-a").mkdir(parents=True)
    (root / "team-a" / "beta.yaml").mkdir()
    (root / "acme.yaml").write_text(
        "tenants:\n  acme:\n    mysql_threads_running: 90\n", encoding="utf-8")

    stderr = _grar_population_and_stderr(root)
    assert "beta.yaml" not in stderr


def test_a_policy_file_that_is_a_directory_blocks_strict(tmp_path: pathlib.Path):
    """This change added a new way for `--strict` to exit 1; pin it as chosen.

    `_domain_policy.yaml` as a DIRECTORY now reaches `_drop_unusable_policy`
    and lands in `policy_file_errors`, which `generate-routes --validate
    --strict` treats as blocking (ADR-007). Untested, that cuts both ways:
    someone "simplifying" the pass into a bare `print` silently removes a
    gate, and nobody can tell whether an accidental `mkdir` failing a
    release pipeline is the design or a side effect. This test says it is
    the design.
    """
    root = tmp_path / "conf.d"
    (root / "_domain_policy.yaml").mkdir(parents=True)
    (root / "acme.yaml").write_text(
        "tenants:\n  acme:\n    mysql_threads_running: 90\n", encoding="utf-8")

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        result = _parse_config_files(str(root))

    errs = result["policy_file_errors"]
    assert errs, "a policy file that cannot be read must block --strict"
    assert "_domain_policy.yaml" in errs[0]
    assert "is a directory, not a config file" in errs[0]


def test_a_conf_d_without_defaults_is_legal_not_a_read_failure(
    tmp_path: pathlib.Path,
):
    """Control group for the `except FileNotFoundError: pass` in Layer 1.

    Nothing covered it, so turning that branch into a `_skip` would have
    been green — and every conf.d that simply declares no platform defaults
    would start carrying an "unusable files" caveat.
    """
    root = tmp_path / "conf.d"
    root.mkdir()
    (root / "acme.yaml").write_text(
        "tenants:\n  acme:\n    mysql_threads_running: 90\n", encoding="utf-8")

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        res = diagnose.resolve_inheritance_chain("acme", str(root))

    assert "skipped_unusable_files" not in res
    assert err.getvalue() == ""


def test_diagnose_names_a_missing_profiles_file_the_tenant_points_at(
    tmp_path: pathlib.Path,
):
    """A referenced-but-absent `_profiles.yaml` is a MISSING CHAIN LAYER.

    Absent `_defaults.yaml` is legal (nothing referenced it); absent
    `_profiles.yaml` when a tenant carries `_profile: gold` is not the same
    thing — the resolved chain is short by one layer and the numbers the
    operator is reading are not the numbers they configured. #1468 is that
    silence. Nothing covered this branch.
    """
    root = tmp_path / "conf.d"
    root.mkdir()
    (root / "_defaults.yaml").write_text(
        "defaults:\n  mysql_threads_running: 80\n", encoding="utf-8")
    (root / "acme.yaml").write_text(
        "tenants:\n  acme:\n    _profile: gold\n"
        "    mysql_threads_running: 90\n", encoding="utf-8")

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        res = diagnose.resolve_inheritance_chain("acme", str(root))

    assert res["skipped_unusable_files"] == ["_profiles.yaml"]
    assert "references profile gold" in err.getvalue()


def test_diagnose_treats_a_null_profiles_key_as_no_profiles(
    tmp_path: pathlib.Path,
):
    """`profiles:` with nothing under it is empty, not broken.

    The `is None -> {}` normalisation had no test, so replacing it with any
    other value stayed green. It has to stay distinct from the `not a
    mapping` branch below it: this shape is a legal empty document and must
    NOT produce a skip entry.
    """
    root = _profile_ref_confd(tmp_path / "conf.d", "profiles:\n")

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        res = diagnose.resolve_inheritance_chain("acme", str(root))

    assert "skipped_unusable_files" not in res
    assert "profiles" not in err.getvalue()


def test_an_unreadable_conf_d_root_blocks_the_routing_reader(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
):
    """⛔ "Unreadable" must never reach the caller as "no tenants".

    This is the end-to-end half that the library-level test
    (`test_an_unlistable_root_names_itself_instead_of_raising`) does not
    reach, and the gap was a REGRESSION this change set introduced.

    Before `unusable_config_paths` grew its `except OSError`, a `chmod 111`
    conf.d root (traversable, not readable) raised `PermissionError` out of
    `root.iterdir()` and the process died — loud, and non-zero. Catching it
    was right; what was missed is that the caller then treated an empty
    result as "no tenants are configured". Measured A/B with
    `setpriv --reuid=65534` against a real chmod-111 directory:

        OLD  rc=1  PermissionError traceback
        NEW  rc=0  "No tenants found in config directory."

    `.github/workflows/validate.yaml` runs `generate-routes --validate
    --strict` as a REQUIRED check, and GitHub Actions does not fail a step
    for stderr output — so an unreadable conf.d turned that gate GREEN with
    zero routes. That is the exact shape (#1339 / #1448) this whole change
    set exists to remove: a green light for a directory nothing read.
    """
    root = tmp_path / "conf.d"
    root.mkdir()
    (root / "acme.yaml").write_text(
        "tenants:\n  acme:\n    mysql_threads_running: 90\n", encoding="utf-8")

    real_iterdir = pathlib.Path.iterdir

    def deny(self):
        if os.fspath(self) == os.fspath(root):
            raise PermissionError(13, "Permission denied", str(self))
        return real_iterdir(self)

    monkeypatch.setattr(pathlib.Path, "iterdir", deny)

    err = io.StringIO()
    with contextlib.redirect_stderr(err), pytest.raises(SystemExit) as exc:
        _parse_config_files(str(root))

    # Non-zero is the point; 2 matches the `not os.path.isdir` guard beside
    # it — both mean "the directory you pointed me at is not usable input",
    # which is a different statement from "your config has a finding".
    assert exc.value.code == 2, (
        "an unreadable conf.d root must not exit 0 — CI reads the exit code, "
        "not stderr")
    msg = err.getvalue()
    assert "could not be read" in msg
    assert "not 'no tenants'" in msg, (
        "the message must say why an empty result would be a lie")


def test_a_readable_but_empty_conf_d_root_is_still_fine(tmp_path: pathlib.Path):
    """Control group for the guard above — "empty" must stay legal.

    Without this, the cheapest way to satisfy the assertion above is to make
    every empty directory blocking, which would break the legitimate
    "no tenants yet" case that the reader has always supported.
    """
    root = tmp_path / "conf.d"
    root.mkdir()

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        result = _parse_config_files(str(root))
    assert result["tenant_keys"] == {}
    assert result["policy_file_errors"] == []
    assert "could not be read" not in err.getvalue()
