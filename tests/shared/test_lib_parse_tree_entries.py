"""`_lib_parse_tree` reads Go's per-path verdict (`role`) and composes none.

`da-guard parse-tree` writes one entry per path in every mode
(`da-guard.parse-tree/v2`, ADR-036 §2 / §7 step 9, #3206). These tests run the
real binary — as an unprivileged uid, so the permission cases are real — and
read its documents through the module's one reader:

- a path whose stat or open a permission refuses is a config file whose read
  fails, never "not a file"; only what the exporter's walk cannot reach is;
- list mode gives tree mode's entries, but for what only reading finds;
- `Entry.config_file` is the role and nothing else.

Under uid 0 the mode bits deny nothing, so the da-guard runs are re-executed
as uid 65534 (`setpriv`). Where that cannot be done the tests skip, or fail
when `VIBE_REQUIRE_UNPRIVILEGED_FS` is set (CI), as the helm-gated tests do
with `VIBE_REQUIRE_HELM`.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

import _lib_parse_tree as lpt  # noqa: E402
from _platform_fs import symlink_or_skip  # noqa: E402

_REQUIRE = os.environ.get("VIBE_REQUIRE_UNPRIVILEGED_FS", "")


def _skip_or_fail(why: str) -> None:
    if _REQUIRE:
        pytest.fail(f"VIBE_REQUIRE_UNPRIVILEGED_FS is set, but {why}: the permission cases of "
                    "the role verdict would go unmeasured")
    pytest.skip(why)


@pytest.fixture
def unprivileged(da_guard_binary):
    """``(box, run)``: *box* a fresh 0755 directory under /var/tmp holding a
    copy of the session's da-guard; ``run(args)`` runs that da-guard with
    *args* as uid 65534 when this process is uid 0 (else as this uid) and
    returns its stdout bytes, failing on a non-zero exit."""
    if os.name == "nt" or not hasattr(os, "geteuid"):
        _skip_or_fail("POSIX mode bits do not apply here")
    prefix: list[str] = []
    if os.geteuid() == 0:
        setpriv = shutil.which("setpriv")
        if setpriv is None:
            _skip_or_fail("running as uid 0 and `setpriv` is not installed")
        prefix = [setpriv, "--reuid=65534", "--regid=65534", "--clear-groups"]
    base = "/var/tmp" if os.path.isdir("/var/tmp") else None
    box = Path(tempfile.mkdtemp(prefix="vibe-roles-", dir=base))
    try:
        box.chmod(0o755)
        exe = box / "da-guard"
        shutil.copy2(da_guard_binary, exe)
        exe.chmod(0o755)

        def run(args: list[str]) -> bytes:
            proc = subprocess.run([*prefix, str(exe), *args], capture_output=True,
                                  check=False, timeout=120, cwd=box)
            assert proc.returncode == 0, proc.stderr.decode(errors="replace")
            return proc.stdout

        yield box, run
    finally:
        for dirpath, dirnames, _files in os.walk(box):
            for n in dirnames:
                try:
                    os.chmod(os.path.join(dirpath, n), 0o755)
                except OSError:
                    pass
        shutil.rmtree(box, ignore_errors=True)


def _build(box: Path) -> Path:
    """A conf.d where permissions really deny: `b.yaml` links into a mode-000
    directory (stat EACCES), `noexec/t.yaml` sits in a directory that may be
    listed but not searched (lstat EACCES), `zero.yaml` is mode 000 (open
    refused), `sealed/` and the config-named `unlist.yaml/` cannot be
    listed; `gone.yaml` dangles, `dir.yaml/` is a config-named directory,
    `broken.yaml` a syntax error."""
    conf, locked = box / "conf.d", box / "locked"
    for d in (conf, locked, conf / "sealed", conf / "noexec", conf / "dir.yaml",
              conf / "unlist.yaml"):
        d.mkdir()
        d.chmod(0o755)
    for p, body in {conf / "ok.yaml": "tenants: {ok: {}}\n",
                    conf / "zero.yaml": "tenants: {zero: {}}\n",
                    conf / "broken.yaml": "tenants: {b: [\n",
                    locked / "real.yaml": "tenants: {b: {}}\n",
                    conf / "sealed" / "s.yaml": "tenants: {s: {}}\n",
                    conf / "noexec" / "t.yaml": "tenants: {t: {}}\n",
                    conf / "dir.yaml" / "in.yaml": "tenants: {in: {}}\n",
                    conf / "unlist.yaml" / "u.yaml": "tenants: {u: {}}\n"}.items():
        p.write_text(body, encoding="utf-8")
        p.chmod(0o644)
    symlink_or_skip(locked / "real.yaml", conf / "b.yaml")
    symlink_or_skip("nowhere.yaml", conf / "gone.yaml")
    for p, mode in ((conf / "zero.yaml", 0), (locked, 0), (conf / "sealed", 0),
                    (conf / "noexec", 0o444), (conf / "unlist.yaml", 0)):
        p.chmod(mode)
    return conf


_WANT_ROLES = {
    "b.yaml": "config", "noexec/t.yaml": "config", "zero.yaml": "config",
    "ok.yaml": "config", "broken.yaml": "config", "dir.yaml/in.yaml": "config",
    "gone.yaml": "not_a_file", "sealed": "unlistable_dir",
    # Directories the walk enters, a config-named one included (#3206).
    "dir.yaml": "dir", "noexec": "dir",
    # A config-named directory that cannot be listed: ONE entry (the walk
    # once recorded it twice, and the reader refused the whole document).
    "unlist.yaml": "unlistable_dir",
}


def test_permission_refusals_are_config_files_in_both_modes(unprivileged) -> None:
    box, run = unprivileged
    conf = _build(box)
    tree = lpt.tree_of(lpt.read_document(run(["parse-tree", "--config-dir", str(conf)])))
    raw_list = lpt.read_document(run(["parse-tree", "--list-only", "--config-dir", str(conf)]),
                                 check=lpt._check_list("list"))
    listed = lpt.entries_of(raw_list)

    assert {e.path: e.role for e in tree.entries} == _WANT_ROLES
    # A refused stat or open is a config file whose read fails — loudly.
    for rel in ("b.yaml", "noexec/t.yaml", "zero.yaml"):
        e = next(x for x in tree.entries if x.path == rel)
        assert e.config_file and e.problem is not None and e.problem[0] == "read_failure", e
        with pytest.raises(lpt.FileUnreadable):
            tree.value(rel)
    # List mode: tree mode's entries, but for what only reading finds — the
    # content problems, and the `unservable` verdict of a file the exporter's
    # load drops for its content (list mode knows the walk's records alone).
    def without_content_problem(e: lpt.Entry) -> lpt.Entry:
        walk_record = e.problem is not None and e.problem[0] in lpt.LIST_PROBLEM_KINDS
        if e.verdict == "unservable" and not walk_record:
            e = lpt.Entry(**{**e.__dict__, "verdict": None})
        if e.problem is not None and e.problem[0] not in lpt.LIST_PROBLEM_KINDS:
            return lpt.Entry(**{**e.__dict__, "problem": None})
        return e
    assert listed == tuple(without_content_problem(e) for e in tree.entries)
    assert any(e.problem and e.problem[0] == "syntax_error" for e in tree.entries)


def test_config_file_is_the_role_and_nothing_else() -> None:
    """The same facts under each role: only the role decides. A reader that
    recomposed the verdict from the problem kind or the class would call one
    of these wrong (a stat refused and a dangling link share their problem)."""
    facts = dict(path="x.yaml", depth=0, cls="tenant", tenant_named="x", defaults_document=False,
                 readers=("exporter", "generator"), not_read=None,
                 problem=("read_failure", "the path cannot be statted (stat_error)"), verdict=None)
    got = {role: lpt.Entry(**facts, role=role) for role in sorted(lpt.ROLES)}
    assert {r: (e.config_file, e.is_dir, e.not_a_file, e.unlistable_dir)
            for r, e in got.items()} == {
        "config": (True, False, False, False),
        "dir": (False, True, False, False),
        "not_a_file": (False, False, True, False),
        "unlistable_dir": (False, False, False, True),
    }


def test_list_tree_and_classify_names_read_the_same_reader(tmp_path, da_guard_env) -> None:
    conf = tmp_path / "conf.d"
    (conf / "team").mkdir(parents=True)
    (conf / "t1.yaml").write_text("tenants: {t1: {}}\n", encoding="utf-8")
    (conf / "_defaults.yaml").write_text("defaults: {}\n", encoding="utf-8")
    (conf / "_defaults.yml").write_text("defaults: {}\n", encoding="utf-8")
    (conf / "team" / "_domain_policy.yaml").write_text("x: 1\n", encoding="utf-8")
    listing = lpt.list_tree(conf)
    assert [e.path for e in listing.entries] == [
        "_defaults.yaml", "_defaults.yml", "t1.yaml", "team", "team/_domain_policy.yaml"]
    assert listing.entry("team").is_dir and listing.entry("team").problem is None
    # A directory is no file a reader reads, and nothing is passed over.
    assert (listing.entry("team").readers, listing.entry("team").not_read) == ((), None)
    assert dict(listing.defaults_carriers) == {".": "_defaults.yaml"}
    assert listing.entry("_defaults.yml").not_read == "unselected_defaults_carrier"
    assert listing.entry("team/_domain_policy.yaml").readers == ("generator",)
    assert [e.path for e in listing.config_files()] == [
        e.path for e in listing.entries if e.path != "team"]
    names = lpt.classify_names(["db-a.yaml", ".h.yaml", "_defaults-multidb.yaml", "n.txt"])
    assert {n: (e.cls, e.tenant_named, e.defaults_document, e.role, e.readers)
            for n, e in names.items()} == {
        "db-a.yaml": ("tenant", "db-a", False, None, None),
        ".h.yaml": ("hidden", None, False, None, None),
        "_defaults-multidb.yaml": ("platform", None, True, None, None),
        "n.txt": ("not_config_name", None, False, None, None),
    }
    with pytest.raises(lpt.ParseTreeError, match="not valid UTF-8"):
        lpt.classify_names(["bad\udcff.yaml"])


def test_a_list_document_is_no_tree_document() -> None:
    """The reader of each mode takes its own mode only: a tree reader handed a
    list document (no content) refuses it whole, and the other way round."""
    entry = {"path": "t.yaml", "depth": 0, "class": "tenant", "tenant_named": "t",
             "defaults_document": False, "readers": ["exporter"], "not_read": None,
             "role": "config", "problem": None, "verdict": None}
    listed = {"schema": lpt.SCHEMA, "da_guard_version": "t", "mode": "list",
              "defaults_carriers": {}, "entries": [entry]}
    import json
    with pytest.raises(lpt.ParseTreeError, match="mode 'list'"):
        lpt.read_document(json.dumps(listed).encode())
    tree = {**listed, "mode": "tree", "files": {"t.yaml": {}}, "rendered_by": {}}
    with pytest.raises(lpt.ParseTreeError, match="list mode with 'files'"):
        lpt.read_document(json.dumps(tree).encode(), check=lpt._check_list("list"))


@pytest.mark.parametrize("role, problem", [
    ("dir", None), ("unlistable_dir", {"kind": "read_failure", "message": "x"})])
def test_a_dir_entry_names_no_reader(role, problem) -> None:
    """Roles `dir` and `unlistable_dir` are read by no reader and pass
    nothing over: an entry that gives a directory readers, or a not_read
    reason, is a document from a da-guard this reader does not understand,
    refused whole."""
    import json
    base = {"path": "team", "depth": 0, "class": "directory", "tenant_named": None,
            "defaults_document": False, "readers": [], "not_read": None,
            "role": role, "problem": problem,
            "verdict": None if problem is None else "unservable"}
    doc = {"schema": lpt.SCHEMA, "da_guard_version": "t", "mode": "list",
           "defaults_carriers": {}}
    listing = lpt.read_document(json.dumps({**doc, "entries": [base]}).encode(),
                                check=lpt._check_list("list"))
    assert listing["entries"][0]["readers"] == []
    for bad in ({"readers": ["exporter"]}, {"not_read": "unselected_defaults_carrier"}):
        with pytest.raises(lpt.ParseTreeError, match=f"a {role} with readers"):
            lpt.read_document(json.dumps({**doc, "entries": [{**base, **bad}]}).encode(),
                              check=lpt._check_list("list"))


@pytest.mark.parametrize("target", ["team", "team/sub", "_d", "sealed", "y.yaml"])
def test_a_write_onto_a_directory_is_refused_not_a_da_guard_failure(unprivileged, target) -> None:
    """F1: a write target that is a directory — entered (`team`, `team/sub`,
    `_d`) or one this uid cannot list (`sealed/`, the config-named `y.yaml/`)
    — is file mode's walk entry (role dir / unlistable_dir, no content), which
    the reader accepts; the write check refuses it as `path_not_read`, never
    as a document from an older da-guard (ParseTreeError). Run as uid 65534,
    so the unlistable directories really are."""
    import _lib_write_check as wc
    box, _run = unprivileged
    conf = box / "conf.d"
    for d in ("team/sub", "_d", "sealed", "y.yaml"):
        (conf / d).mkdir(parents=True)
    for d in (conf, conf / "team", conf / "team/sub", conf / "_d"):
        d.chmod(0o755)
    (conf / "team" / "t.yaml").write_text("tenants: {t: {}}\n", encoding="utf-8")
    (conf / "team" / "t.yaml").chmod(0o644)
    for d in ("sealed", "y.yaml"):
        (conf / d).chmod(0)
    wrapper = box / "da-guard-as-run"
    prefix = (f"{shutil.which('setpriv')} --reuid=65534 --regid=65534 --clear-groups "
              if os.geteuid() == 0 else "")
    wrapper.write_text(f'#!/bin/sh\nexec {prefix}"{box / "da-guard"}" "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    with pytest.raises(wc.WriteRefused) as ei:
        wc.confirm(conf, target, "tenants: {x: {}}\n", {}, binary=str(wrapper))
    assert ei.value.reason == wc.NOT_READ, ei.value
    assert "directory" in str(ei.value) and "older" not in str(ei.value), ei.value


# ── the fail-closed exit (#3206) ─────────────────────────────────────

def _entry(path: str, verdict, problem=("read_failure", "the path cannot be statted")) -> lpt.Entry:
    return lpt.Entry(path=path, depth=0, cls="tenant", tenant_named=None, defaults_document=False,
                     readers=("exporter",), not_read=None, role="config",
                     problem=problem if verdict else None, verdict=verdict)


def test_the_refusal_names_every_path_with_a_verdict_and_exits_2(capsys) -> None:
    """One `ERROR:` line per entry Go gives a verdict — every one, none
    elided — then the tree's line, and exit 2; an entry without one is not
    named, and a document without any returns."""
    many = [_entry(f"t{i:03d}.yaml", "unservable") for i in range(40)]
    entries = [_entry("ok.yaml", None), _entry("team", "tree_refused"), *many]
    with pytest.raises(SystemExit) as ei:
        lpt.refuse_unservable(entries, "conf.d")
    assert ei.value.code == 2
    err = capsys.readouterr().err.splitlines()
    assert len(err) == 1 + 40 + 1, err
    assert err[0].startswith("ERROR: team: the route generator refuses the whole tree")
    assert all(f"ERROR: t{i:03d}.yaml: the exporter cannot read or decode it" in "\n".join(err)
               for i in range(40))
    assert "ok.yaml" not in "\n".join(err)
    assert "41 path(s)" in err[-1]
    lpt.refuse_unservable([_entry("ok.yaml", None)], "conf.d")  # returns


def test_the_refusal_escapes_a_name_for_the_terminal(capsys) -> None:
    with pytest.raises(SystemExit):
        lpt.refuse_unservable([_entry("a\x1b[2Jb.yaml", "unservable")], "conf.d")
    assert "\x1b" not in capsys.readouterr().err


def test_the_reader_refuses_a_flagged_tree_unless_the_run_judges(tmp_path, da_guard_env, capsys) -> None:
    """`ConfdReader.tree` is the exit: a tree with a dangling link ends the
    run (exit 2, the link named) — the listing, the tenant set and a read
    all go through it; inside `judging()` the same tree reads."""
    import _lib_confd
    import _lib_parse_cache as pc
    conf = tmp_path / "conf.d"
    conf.mkdir()
    (conf / "a.yaml").write_text("tenants: {a: {}}\n", encoding="utf-8")
    symlink_or_skip("nowhere", conf / "gone.yaml")
    for take in (lambda r: r.tree(), lambda r: r.listing(), lambda r: pc.tenant_scope(r),
                 lambda r: r.read("a.yaml"), lambda r: _lib_confd.config_listing(conf)):
        with pytest.raises(SystemExit) as ei:
            take(pc.ConfdReader(conf))
        assert ei.value.code == 2
        assert "ERROR: gone.yaml:" in capsys.readouterr().err
    with pc.judging():
        tree = pc.ConfdReader(conf).tree()
    assert [e.path for e in lpt.refused_entries(tree.entries)] == ["gone.yaml"]
