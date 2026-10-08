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
from _lib_tenant_values import ParseFailedError  # noqa: E402
import validate_config as vc  # noqa: E402
from _grar_parse import _parse_config_files  # noqa: E402
from _lib_confd import iter_config_files  # noqa: E402

# #2526: diagnose reads the tree through `da-guard effective`.
pytestmark = pytest.mark.usefixtures("da_guard_env")


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


def test_routing_parser_reads_the_tree(tmp_path: pathlib.Path):
    """#2326: the routing parser reads the WHOLE tree, like the exporter.

    Until #2326 this pinned the opposite — `recursive=False` was
    load-bearing, and a tenant in `team-a/` got no route at rc 0 while
    threshold-exporter served its thresholds. ADR-016/017 "Amendment
    2026-09-28" made the routing plane hierarchical; this pins that a nested
    tenant is read, where it lives is recorded (it decides which routing
    layers reach it), and the old "read FLAT … SKIPPED" warning is gone.
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
    assert sorted(parsed["all_tenants"]) == ["acme", "deep"], (
        "the routing parser stopped reading subdirectories")
    assert parsed["tenant_dirs"] == {"acme": ".", "deep": "team-a"}
    assert "FLAT" not in err.getvalue() and "SKIPPED" not in err.getvalue()


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


# ── #1468 → #2526: diagnose never answers from a partial read ───────────
#
# #1468 made diagnose WARN about a file it could not read and still return
# the chain without it (`skipped_unusable_files`). Since #2526 the chain is
# `da-guard effective`'s, and a file the exporter's load drops fails the
# read closed: ParseFailedError (the CLI exits 2 with da-guard's reason),
# never a chain short of a layer.


def _acme_confd(root: pathlib.Path, **files: str) -> pathlib.Path:
    root.mkdir(parents=True, exist_ok=True)
    base = {"_defaults.yaml": "defaults:\n  mysql_threads_running: 80\n",
            "acme.yaml": "tenants:\n  acme:\n    _profile: gold\n"
                         "    mysql_threads_running: 90\n"}
    base.update(files)
    for name, body in base.items():
        if body is not None:
            (root / name).write_text(body, encoding="utf-8")
    return root


@pytest.mark.parametrize("fname,body", [
    ("acme.yaml", "tenants:\n  acme:\n    mysql_threads_running: [90\n"),
    ("acme.yaml", "- not\n- a\n- mapping\n"),
    ("_profiles.yaml", "profiles:\n  gold: [unclosed\n"),
    ("_profiles.yaml", "profiles:\n  - gold\n  - silver\n"),
    ("_profiles.yaml", "[]\n"),
], ids=["tenant-unclosed", "tenant-a-list", "profiles-unclosed",
        "profiles-key-a-list", "profiles-doc-a-list"])
def test_diagnose_fails_closed_on_a_file_the_exporter_drops(
        tmp_path: pathlib.Path, fname: str, body: str):
    root = _acme_confd(tmp_path / "conf.d", **{fname: body})
    with pytest.raises(ParseFailedError, match=fname) as exc:
        diagnose.resolve_inheritance_chain("acme", str(root))
    assert any(fname in ln for ln in exc.value.stderr_lines), exc.value.stderr_lines


@pytest.mark.parametrize("files", [
    {},
    {"_profiles.yaml": ""},
    {"_profiles.yaml": "profiles:\n"},
    {"_defaults.yaml": None},
], ids=["healthy", "empty-profiles-doc", "null-profiles-key", "no-defaults"])
def test_diagnose_legal_shapes_carry_no_caveat(tmp_path: pathlib.Path, files: dict):
    """⛔ Control for the above: each of these loads on the exporter, so the
    chain is answered and nothing is said about a file."""
    root = _acme_confd(tmp_path / "conf.d", **files)
    if not (root / "_profiles.yaml").exists():
        (root / "_profiles.yaml").write_text("profiles:\n  gold:\n    pg_x: 1\n",
                                             encoding="utf-8")
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        chain = diagnose.resolve_inheritance_chain("acme", str(root))
    assert chain["resolved"]["mysql_threads_running"] in (90, "90")  # "90" as written where no root default declares it
    assert "skipped_unusable_files" not in chain
    assert "skipped_unusable_files" not in diagnose._format_chain_summary(chain)
    assert all("binds no profile" in ln for ln in err.getvalue().splitlines()), err.getvalue()


def test_diagnose_names_a_profile_reference_that_binds_nothing(tmp_path: pathlib.Path):
    """A referenced-but-absent profile is a missing chain layer: said on
    stderr, and `profile_name` is None (the exporter binds none)."""
    root = _acme_confd(tmp_path / "conf.d")
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        chain = diagnose.resolve_inheritance_chain("acme", str(root))
    assert chain["profile_name"] is None
    assert "_profile 'gold' binds no profile" in err.getvalue()


def test_diagnose_reads_the_exporters_population(confd_with_dir_named_yaml):
    """A directory named `beta.yaml` is a sub-directory to the exporter's
    walk (empty here), not a config file: the chain is answered and
    diagnose, which no longer selects files itself, says nothing about it."""
    root = confd_with_dir_named_yaml
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        chain = diagnose.resolve_inheritance_chain("acme", str(root))
    assert chain["resolved"] == {"mysql_threads_running": 90}
    assert "beta.yaml" not in err.getvalue()


def test_diagnose_a_defaults_file_that_is_a_directory(tmp_path: pathlib.Path):
    """The exporter walks into it; diagnose must not open it either (one
    line with an absolute errno path was the #1469 shape)."""
    root = tmp_path / "conf.d"
    (root / "_defaults.yaml").mkdir(parents=True)
    (root / "acme.yaml").write_text(
        "tenants:\n  acme:\n    mysql_threads_running: 90\n", encoding="utf-8")
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        res = diagnose.resolve_inheritance_chain("acme", str(root))
    assert res["resolved"] == {"mysql_threads_running": "90"}  # no root default: not served
    assert res["declared"] == []
    assert err.getvalue() == ""


def test_both_readers_name_it_in_the_same_words(confd_with_dir_named_yaml):
    """#1469: `validate-config` and the routing parser phrase a config-named
    directory identically. (`diagnose` was the third reader until #2526; it
    now takes the exporter's walk and names no file itself.)"""
    root = confd_with_dir_named_yaml

    grar_err = io.StringIO()
    with contextlib.redirect_stderr(grar_err):
        _parse_config_files(str(root))
    vc_report = vc.check_yaml_syntax(str(root))

    phrase = "is a directory, not a config file"
    grar_line = [ln for ln in grar_err.getvalue().splitlines() if "beta.yaml" in ln]
    vc_line = [d for d in vc_report["details"] if "beta.yaml" in d]
    assert grar_line, "routing parser went silent about beta.yaml"
    assert vc_line, "validate-config went silent about beta.yaml"
    for label, lines in (("grar", grar_line), ("validate-config", vc_line)):
        assert phrase in lines[0], f"{label} phrased it differently: {lines[0]!r}"
        assert "Errno" not in lines[0], f"{label} leaked the raw exception: {lines[0]!r}"
        assert str(root) not in lines[0], f"{label} leaked an absolute path: {lines[0]!r}"


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
    """`pass` on a tree half of which was never opened is the #1911 shape."""
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


def test_routing_parsers_unusable_pass_covers_the_tree(tmp_path: pathlib.Path):
    """The pass covers exactly the population the reader reads.

    #2326: the reader walks the tree, so its unusable pass does too — a
    DIRECTORY named `team-a/beta.yaml` is named, and booked as a file this
    run could not read (the #1460 refusal), instead of vanishing. Before
    #2326 both were flat and this pinned that it stayed silent.
    """
    root = tmp_path / "conf.d"
    (root / "team-a").mkdir(parents=True)
    (root / "team-a" / "beta.yaml").mkdir()
    (root / "acme.yaml").write_text(
        "tenants:\n  acme:\n    mysql_threads_running: 90\n", encoding="utf-8")

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        parsed = _parse_config_files(str(root))
    assert "team-a/beta.yaml" in err.getvalue()
    assert [f for f, _ in parsed["tenant_file_errors"]] == ["team-a/beta.yaml"]


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
    zero routes. That is the exact shape (#1911 / #1448) this whole change
    set exists to remove: a green light for a directory nothing read.
    """
    root = tmp_path / "conf.d"
    root.mkdir()
    (root / "acme.yaml").write_text(
        "tenants:\n  acme:\n    mysql_threads_running: 90\n", encoding="utf-8")

    # #2326: the reader walks the tree (`list_config_tree` → `os.walk` →
    # `os.scandir`), so the root is made unlistable where the walk lists it.
    real_scandir = os.scandir

    def deny(path="."):
        if os.fspath(path) == os.fspath(root):
            raise PermissionError(13, "Permission denied", os.fspath(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", deny)

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
