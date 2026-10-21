"""The Python conf.d readers read Go's per-path verdict and list once (#3206).

ADR-036 §7 step 9: which paths a conf.d holds and what each one is — a
config file (`role: config`, read and failing loudly), provably no file
(`not_a_file`), a directory that cannot be listed (`unlistable_dir`) — is
`da-guard parse-tree`'s `entries`, Go's verdict. A Python reader:

- takes the role as is and composes no verdict of its own from the other
  fields or from a stat (`test_*_follows_the_role_only`);
- that holds a tree-mode document takes its listing from that document and
  never lists a second time (`test_*_lists_no_second_time`);
- names what it cannot read without a traceback, also where a permission
  really denies (the `nobody` tests, run as uid 65534 under uid 0).

The unprivileged tests skip where they cannot run, and FAIL instead when
`VIBE_REQUIRE_UNPRIVILEGED_FS` is set (CI).
"""
from __future__ import annotations

import json
import os
import pathlib
import stat
import subprocess
import sys
import textwrap

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
TOOLS = REPO / "scripts" / "tools"
for _d in (TOOLS, TOOLS / "ops", TOOLS / "dx", TOOLS / "lint", REPO / "scripts" / "ops"):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

import _lib_confd  # noqa: E402
import _lib_parse_cache  # noqa: E402
import _lib_parse_tree as pt  # noqa: E402
from _platform_fs import symlink_or_skip, unprivileged_sandbox  # noqa: E402


def _write(root: pathlib.Path, files: dict[str, str]) -> pathlib.Path:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return root


# ── a fake da-guard that writes a document it is handed ─────────────────

def _entry(path, *, role="config", cls="tenant", problem=None, depth=None, readers=None,
           not_read=None):
    return {"path": path, "depth": path.count("/") if depth is None else depth,
            "class": cls, "tenant_named": path.rsplit("/", 1)[-1].split(".")[0]
            if cls == "tenant" else None,
            "defaults_document": False,
            "readers": ["exporter", "generator"] if readers is None else readers,
            "not_read": not_read, "role": role,
            "problem": None if problem is None else {"kind": problem[0], "message": problem[1]}}


def _emit(tmp: pathlib.Path, doc: dict, name: str = "da-guard") -> str:
    """An executable that prints *doc* whatever it is asked, and exits 0."""
    from _platform_fs import require_shebang_scripts
    require_shebang_scripts()
    body = tmp / f"{name}.json"
    body.write_text(json.dumps(doc), encoding="utf-8")
    exe = tmp / name
    exe.write_text(f"#!/bin/sh\ncat '{body}'\n", encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return str(exe)


def _list_doc(entries, carriers=None, mode="list"):
    return {"schema": pt.SCHEMA, "da_guard_version": "t", "mode": mode,
            "defaults_carriers": carriers or {}, "entries": entries}


# ── the role, and only the role ─────────────────────────────────────────

def test_the_listing_functions_follow_the_role_only(tmp_path, monkeypatch) -> None:
    """Each pair shares every fact but the role: a reader that composed the
    verdict from the problem kind (a refused stat and a dangling link share
    theirs) or from a stat of its own would call one of each pair wrong."""
    root = _write(tmp_path / "conf.d", {"a.yaml": "x: 1\n"})
    stat_refused = ("read_failure", "the path cannot be statted (stat_error)")
    doc = _list_doc([
        _entry("dangling.yaml", role="not_a_file", problem=stat_refused),
        _entry("locked.yaml", role="config", problem=stat_refused),
        _entry("sub", role="unlistable_dir", cls="directory",
               problem=("read_failure", "the directory cannot be listed (walk_error)")),
        # Directories the walk enters: no file, and only the one with a
        # file's name is named.
        _entry("team", role="dir", cls="directory"),
        _entry("x.yaml", role="dir", cls="directory",
               problem=("not_regular_file", "a directory with a config file name")),
        _entry("z.yaml", role="config"),
    ])
    monkeypatch.setenv("DA_GUARD_BINARY", _emit(tmp_path, doc))
    files = [p.name for p in _lib_confd.iter_config_files(root)]
    tree = _lib_confd.list_config_tree(root)
    assert files == ["locked.yaml", "z.yaml"]
    assert [p.name for p in tree.unusable] == ["dangling.yaml", "sub", "x.yaml"]
    assert [p.name for p in tree.unscannable] == ["sub"]
    assert _lib_confd.not_config_files(tree.listing) == {"dangling.yaml", "sub", "team", "x.yaml"}
    entries = sorted(root / n for n in ("dangling.yaml", "locked.yaml", "team", "x.yaml", "z.yaml"))
    assert [p.name for p in _lib_confd.unusable_config_entries(
        entries, listing=tree.listing)] == ["dangling.yaml", "x.yaml"]
    # The reason is da-guard's words for that same entry, never a stat's.
    assert _lib_confd.unusable_reason(root / "dangling.yaml", tree.listing) == (
        "cannot be read as a config file (read_failure): the path cannot be statted "
        "(stat_error)")
    assert _lib_confd.unusable_reason(root / "gone-now.yaml", tree.listing) == (
        "is not a config file the exporter's walk reads")


def test_a_reader_with_a_tree_lists_no_second_time(tmp_path, da_guard_binary,
                                                   monkeypatch) -> None:
    """A reader that holds a tree-mode document answers "which files" from
    its entries: no `--list-only` run, and every `_lib_confd` question asked
    with `reader=` / its listing comes from that one document."""
    root = _write(tmp_path / "conf.d", {"a.yaml": "tenants: {a: {}}\n",
                                        "_defaults.yaml": "defaults: {}\n",
                                        "sub/b.yaml": "tenants: {b: {}}\n"})
    (root / "dir.yaml").mkdir()
    runs: list[list[str]] = []
    real = pt._run

    def counting(args, *a, **kw):
        runs.append(list(args))
        return real(args, *a, **kw)

    monkeypatch.setattr(pt, "_run", counting)
    reader = _lib_parse_cache.ConfdReader(root, binary=da_guard_binary)
    assert reader.listing() is reader.listing()
    assert [p.name for p in _lib_confd.iter_config_files(root, reader=reader)] == [
        "_defaults.yaml", "a.yaml", "b.yaml"]
    assert _lib_confd.unusable_config_paths(root, reader=reader) == [root / "dir.yaml"]
    assert _lib_confd.resolve_defaults_file(root, reader=reader) == root / "_defaults.yaml"
    assert _lib_confd.declared_tenant_ids(root, reader) == {"a", "b"}
    assert reader.entry(root / "dir.yaml").is_dir and reader.entry(root / "dir.yaml").flagged
    assert reader.entry("missing.yaml") is None
    assert [r for r in runs if "--list-only" in r] == []
    assert len(runs) == 1, runs


@pytest.mark.parametrize("tool", ["deprecate_rule", "offboard_tenant", "migrate_conf_d",
                                  "check_confd_schema", "custom_alerts"])
def test_a_tool_holding_the_tree_lists_no_second_time(tool, tmp_path, da_guard_binary,
                                                      monkeypatch, capsys) -> None:
    """The tools that read the content from a tree-mode document take their
    file list from its entries too (#3206 R4): ONE da-guard run over the tree
    per reader — a second (`--list-only` or tree) run could see another tree,
    and a file in one snapshot and not the other would vanish silently."""
    monkeypatch.setenv("DA_GUARD_BINARY", da_guard_binary)
    root = _write(tmp_path / "conf.d", {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
        "db-a.yaml": "tenants:\n  db-a:\n    mysql_connections: '70'\n",
        "sub/db-b.yaml": "tenants:\n  db-b: {}\n"})
    (root / "dir.yaml").mkdir()
    runs: list[list[str]] = []
    real = pt._run

    def counting(args, *a, **kw):
        if "--config-dir" in args and "--path" not in args:
            runs.append(list(args))
        return real(args, *a, **kw)

    monkeypatch.setattr(pt, "_run", counting)
    with _lib_parse_cache.session():
        if tool == "deprecate_rule":
            import deprecate_rule as m
            m.scan_for_metric("mysql_connections", str(root))
            m.nested_residue("mysql_connections", str(root))
            m.defaults_carriers(str(root))
            m.root_platform_files(str(root))
            m.out_of_reach(root)
        elif tool == "offboard_tenant":
            import offboard_tenant as m
            m.load_all_configs(str(root))
            m.find_config_file("db-a", str(root))
        elif tool == "migrate_conf_d":
            import migrate_conf_d as m
            m.plan_migration(root, _lib_parse_cache.reader_for(root))
        elif tool == "check_confd_schema":
            import check_confd_schema as m
            m.validate_dir(str(root), {"type": "object"}, pytest.importorskip("jsonschema"),
                           unreadable=[])
        else:
            from custom_alerts import loader as m
            m.collect_instances(root)
    assert [r for r in runs if "--list-only" in r] == [], runs
    assert len(runs) == 1, runs


def test_tool_message_comes_from_the_role(tmp_path, da_guard_binary, monkeypatch,
                                          capsys) -> None:
    """`deprecate_rule.root_platform_files` keeps a root `_` file the role
    says is a config file — not `os.path.isfile` (False for a link into a
    directory this uid may not search, which dropped a platform file
    silently) — and leaves out what the role says is none."""
    monkeypatch.setenv("DA_GUARD_BINARY", da_guard_binary)
    root = _write(tmp_path / "conf.d", {"_defaults.yaml": "defaults: {}\n",
                                        "_x.yaml": "x: 1\n"})
    (root / "_d.yaml").mkdir()
    import deprecate_rule as m
    with _lib_parse_cache.session():
        assert [n for n, _r in m.root_platform_files(str(root))] == ["_defaults.yaml",
                                                                    "_x.yaml"]


# ── no traceback where a permission really denies (uid 65534) ───────────

def _probe_denied(run, path: pathlib.Path) -> None:
    """The chmod really denies this uid: stat of *path* fails with EACCES."""
    r = run([sys.executable, "-c",
             "import os, sys\ntry:\n    os.stat(sys.argv[1])\nexcept PermissionError:\n"
             "    sys.exit(7)\nsys.exit(0)\n", str(path)])
    assert r.returncode == 7, ("the sandbox does not deny the stat, so the assertions "
                               f"below would be vacuous: rc {r.returncode} {r.stderr}")


def test_stat_refused_entries_stay_config_files_as_nobody() -> None:
    """A symlink into a mode-000 directory and a file in a mode-444 directory
    cannot be statted — EACCES proves nothing — so both stay config files
    (`iter_config_files` yields them, `unusable_config_paths` does not), and
    `configmap_assemble` refuses (rc 2) because the uniqueness check cannot
    read the linked carrier — never WARNs a tenant out of the ConfigMap."""
    with unprivileged_sandbox(str(REPO)) as (box, run):
        box = pathlib.Path(box)
        conf = _write(box / "conf.d", {"_defaults.yaml": "defaults: {}\n",
                                       "db-a.yaml": "tenants:\n  db-a: {}\n",
                                       "sub/t.yaml": "tenants:\n  t: {}\n"})
        locked = _write(box / "locked", {"real.yaml": "tenants:\n  db-b: {}\n"})
        symlink_or_skip(locked / "real.yaml", conf / "db-b.yaml")
        for p in (*conf.rglob("*"), *locked.rglob("*")):
            p.chmod(0o755 if p.is_dir() else 0o644)
        locked.chmod(0)
        (conf / "sub").chmod(0o444)
        _probe_denied(run, conf / "db-b.yaml")
        probe = textwrap.dedent("""\
            import json, sys
            sys.path.insert(0, sys.argv[1])
            import _lib_confd as c
            from pathlib import Path
            r = Path(sys.argv[2])
            print(json.dumps({
                "files": [p.relative_to(r).as_posix() for p in c.iter_config_files(r)],
                "unusable": [p.relative_to(r).as_posix() for p in c.unusable_config_paths(r)]}))
            """)
        r = run([sys.executable, "-c", probe, str(box / "scripts" / "tools"), str(conf)])
        assert r.returncode == 0, r.stderr
        got = json.loads(r.stdout)
        assert {"db-b.yaml", "sub/t.yaml"} <= set(got["files"]), got
        assert not {"db-b.yaml", "sub/t.yaml"} & set(got["unusable"]), got
        cm = run([sys.executable, str(box / "scripts" / "ops" / "configmap_assemble.py"),
                  "--config-dir", str(conf), "--output", str(box / "out.yaml")])
        assert cm.returncode == 2, cm.stdout + cm.stderr
        assert "absent from the ConfigMap" not in cm.stderr, cm.stderr
        # The refusal is the uniqueness check that cannot read db-b.yaml —
        # not a missing kubectl, which would also be rc 2.
        assert "db-b.yaml" in cm.stderr and "could not measure" in cm.stderr, cm.stderr
        assert "kubectl" not in cm.stderr.splitlines()[-1], cm.stderr


@pytest.mark.parametrize("tool", ["validate_config", "deprecate_rule", "migrate_conf_d"])
def test_a_denied_entry_is_named_without_a_traceback_as_nobody(tool) -> None:
    """A mode-000 file and a link into a mode-000 directory: each tool ends
    with its own words and its own non-zero rc, never a Python traceback —
    the stat that raised `PermissionError` is no longer asked; the role is."""
    with unprivileged_sandbox(str(REPO)) as (box, run):
        box = pathlib.Path(box)
        conf = _write(box / "conf.d", {
            "_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
            "db-a.yaml": "tenants:\n  db-a:\n    mysql_connections: '70'\n",
            "zero.yaml": "tenants:\n  zero: {}\n"})
        locked = _write(box / "locked", {"real.yaml": "tenants:\n  db-b: {}\n"})
        (locked / "d").mkdir()
        symlink_or_skip(locked / "real.yaml", conf / "db-b.yaml")
        symlink_or_skip(locked / "d", conf / "linkdir", target_is_directory=True)
        for p in (*conf.rglob("*"), *locked.rglob("*")):
            if not p.is_symlink():
                p.chmod(0o755 if p.is_dir() else 0o644)
        (conf / "zero.yaml").chmod(0)
        locked.chmod(0)
        _probe_denied(run, conf / "db-b.yaml")
        script, args = {
            "validate_config": ("tools/ops/validate_config.py", ["--config-dir", str(conf)]),
            "deprecate_rule": ("tools/ops/deprecate_rule.py",
                               ["mysql_connections", "--config-dir", str(conf)]),
            "migrate_conf_d": ("tools/dx/migrate_conf_d.py", ["--conf-d", str(conf)]),
        }[tool]
        r = run([sys.executable, str(box / "scripts" / script), *args])
        out = r.stdout + r.stderr
        assert "Traceback" not in out, out[-3000:]
        assert "zero.yaml" in out or "db-b.yaml" in out, out[-3000:]
        # The verdict, not only the absence of a traceback (#3206 N5): a tree
        # it cannot read whole is never a clean run — a tree finding for the
        # validator and the deprecation (rc 1), nothing planned for the
        # migration (rc 2).
        assert r.returncode == {"validate_config": 1, "deprecate_rule": 1,
                                "migrate_conf_d": 2}[tool], (r.returncode, out[-3000:])


def test_an_unlistable_conf_d_beside_a_bad_name_is_named_as_nobody() -> None:
    """conf.d is mode 000 and a SIBLING of it has a name that is not UTF-8.
    The reason for conf.d comes from conf.d's own listing — never from
    re-listing its parent, which da-guard refuses whole for that sibling —
    so validate-config says the directory could not be read, no traceback."""
    with unprivileged_sandbox(str(REPO)) as (box, run):
        box = pathlib.Path(box)
        conf = _write(box / "conf.d", {"db-a.yaml": "tenants:\n  db-a: {}\n"})
        try:
            with open(os.path.join(os.fsencode(box), b"bad-\xff.yaml"), "wb") as fh:
                fh.write(b"x: 1\n")
        except OSError as exc:
            pytest.skip(f"this filesystem refuses a non-UTF-8 name: {exc}")
        conf.chmod(0)
        r = run([sys.executable, str(box / "scripts" / "tools" / "ops" / "validate_config.py"),
                 "--config-dir", str(conf)])
        out = r.stdout + r.stderr
        assert "Traceback" not in out, out[-2000:]
        assert "could not be read" in out, out[-2000:]


# ── names that are not UTF-8: a clean refusal ───────────────────────────

def _bad_name(parent: pathlib.Path, name: bytes, *, directory=False) -> None:
    try:
        target = os.path.join(os.fsencode(parent), name)
        if directory:
            os.mkdir(target)
        else:
            with open(target, "wb") as fh:
                fh.write(b"tenants: {x: {}}\n")
    except OSError as exc:
        pytest.skip(f"this filesystem refuses a non-UTF-8 name: {exc}")


def _run_tool(script: pathlib.Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-X", "utf8", str(script), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=300, check=False)


def test_assemble_config_dir_refuses_a_non_utf8_name_without_a_traceback(
        tmp_path, da_guard_env) -> None:
    """A shard holding a file whose name is not UTF-8: da-guard refuses the
    listing (JSON cannot carry the name), and the tool ends rc 2 with an
    `ERROR:` line — the ParseTreeError used to escape as a traceback."""
    src = _write(tmp_path / "shard", {"a.yaml": "tenants:\n  a: {}\n"})
    _bad_name(src, b"b-\xff.yaml")
    r = _run_tool(TOOLS / "ops" / "assemble_config_dir.py", "--sources", str(src),
                  "--output", str(tmp_path / "out"))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "Traceback" not in r.stderr, r.stderr
    assert "ERROR:" in r.stderr, r.stderr


def test_migrate_conf_d_refuses_a_non_utf8_directory_name(tmp_path, da_guard_env) -> None:
    """`migrate_conf_d` asks `--names` about its subdirectories; one whose
    name is not UTF-8 is refused before da-guard is asked and the run ends
    rc 2 naming it, no traceback."""
    conf = _write(tmp_path / "conf.d", {"a.yaml": "tenants:\n  a: {}\n"})
    _bad_name(conf, b"dir-\xff", directory=True)
    r = _run_tool(TOOLS / "dx" / "migrate_conf_d.py", "--conf-d", str(conf), "--dry-run")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "Traceback" not in r.stderr, r.stderr


# ── one da-guard per run (`use_da_guard`) and the names memo ────────────

def test_an_inner_none_inherits_the_outer_selection() -> None:
    with pt.use_da_guard("/outer/da-guard", 30):
        with pt.use_da_guard(None, 5):
            assert pt._resolve(None, None) == ("/outer/da-guard", 5)
        with pt.use_da_guard("/inner/da-guard"):
            assert pt._resolve(None, None) == ("/inner/da-guard", 30)
        assert pt._resolve(None, None) == ("/outer/da-guard", 30)
        assert pt._resolve("/explicit", 1) == ("/explicit", 1)
    assert pt._resolve(None, None) == (None, pt.DEFAULT_TIMEOUT)


def test_a_thread_does_not_inherit_the_selection() -> None:
    """A ContextVar: a thread started inside the block resolves as if no
    block were open (the docstring says so); one run in a copied context
    does inherit it."""
    import contextvars
    import threading
    seen = {}
    with pt.use_da_guard("/outer/da-guard", 30):
        t = threading.Thread(target=lambda: seen.setdefault("plain", pt._resolve(None, None)))
        t.start()
        t.join()
        ctx = contextvars.copy_context()
        t = threading.Thread(target=lambda: seen.setdefault(
            "copied", ctx.run(pt._resolve, None, None)))
        t.start()
        t.join()
    assert seen["plain"] == (None, pt.DEFAULT_TIMEOUT)
    assert seen["copied"] == ("/outer/da-guard", 30)


def test_the_selection_reaches_a_listing_taken_on_the_tool_s_behalf(tmp_path,
                                                                     monkeypatch) -> None:
    """A library listing that names no binary (`_lib_confd.config_listing`)
    runs the tool's selected da-guard, not `$DA_GUARD_BINARY`."""
    root = _write(tmp_path / "conf.d", {"a.yaml": "x: 1\n"})
    good = _emit(tmp_path, _list_doc([_entry("a.yaml")]), "good")
    decoy = tmp_path / "decoy"
    decoy.write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")
    decoy.chmod(0o755)
    monkeypatch.setenv("DA_GUARD_BINARY", str(decoy))
    with pt.use_da_guard(good):
        assert [p.name for p in _lib_confd.iter_config_files(root)] == ["a.yaml"]
    with pytest.raises(pt.ParseTreeError, match="exited 9"):
        list(_lib_confd.iter_config_files(root))


def _names_doc(cls: str) -> dict:
    return {"schema": pt.SCHEMA, "da_guard_version": "t", "mode": "names",
            "defaults_carriers": {}, "entries": [{
                "path": "x.yaml", "depth": 0, "class": cls,
                "tenant_named": "x" if cls == "tenant" else None,
                "defaults_document": False, "readers": None, "not_read": None,
                "role": None, "problem": None}]}


def test_the_names_memo_follows_the_binary_that_would_run(tmp_path, monkeypatch) -> None:
    """The memo is keyed on the binary a run would EXECUTE (resolved to an
    absolute path) and the current `$DA_GUARD_BINARY` — not on what the
    caller passed (None): moving `$DA_GUARD_BINARY` to another da-guard asks
    that one; asking again the same one runs nothing."""
    a_dir, b_dir = tmp_path / "a", tmp_path / "b"
    a_dir.mkdir()
    b_dir.mkdir()
    monkeypatch.setattr(pt, "_NAMES", {})
    monkeypatch.setenv("DA_GUARD_BINARY", _emit(a_dir, _names_doc("tenant")))
    assert pt.classify_names(["x.yaml"])["x.yaml"].cls == "tenant"
    (a_dir / "da-guard.json").write_text("not json", encoding="utf-8")
    assert pt.classify_names(["x.yaml"])["x.yaml"].cls == "tenant"  # memo: not run again
    monkeypatch.setenv("DA_GUARD_BINARY", _emit(b_dir, _names_doc("platform")))
    assert pt.classify_names(["x.yaml"])["x.yaml"].cls == "platform"


def test_names_mode_refuses_an_answer_for_other_names(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(pt, "_NAMES", {})
    monkeypatch.setenv("DA_GUARD_BINARY", _emit(tmp_path, _names_doc("tenant")))
    with pytest.raises(pt.ParseTreeError, match="did not answer the names"):
        pt.classify_names(["y.yaml"])


def test_a_names_document_with_a_role_is_refused(tmp_path, monkeypatch) -> None:
    doc = _names_doc("tenant")
    doc["entries"][0]["role"] = "config"
    monkeypatch.setattr(pt, "_NAMES", {})
    monkeypatch.setenv("DA_GUARD_BINARY", _emit(tmp_path, doc))
    with pytest.raises(pt.ParseTreeError, match="in names mode"):
        pt.classify_names(["x.yaml"])


@pytest.mark.parametrize("change, match", [
    (lambda d: d.update(files={}), "list mode with 'files'"),
    (lambda d: d.update(mode="tree"), "this reader asked for list"),
    (lambda d: d["entries"][0].update(problem={"kind": "syntax_error", "message": "m"}),
     "of a kind list mode gives"),
    (lambda d: d["entries"].append(dict(d["entries"][0])), "listed twice"),
    (lambda d: d["entries"][0].update(role="maybe"), "role 'maybe'"),
    (lambda d: d.update(defaults_carriers=[]), "defaults_carriers"),
    (lambda d: d.pop("da_guard_version"), "older than this tool"),
])
def test_a_list_document_wrong_anywhere_is_refused_whole(tmp_path, monkeypatch, change,
                                                         match) -> None:
    doc = _list_doc([_entry("a.yaml")])
    change(doc)
    monkeypatch.setenv("DA_GUARD_BINARY", _emit(tmp_path, doc))
    with pytest.raises(pt.ParseTreeError, match=match):
        pt.list_tree(tmp_path)


def test_listing_of_a_tree_is_its_entries(tmp_path, da_guard_binary) -> None:
    root = _write(tmp_path / "conf.d", {"a.yaml": "tenants: {a: {}}\n",
                                        "_defaults.yaml": "defaults: {}\n"})
    tree = pt.load(root, da_guard_binary)
    listing = pt.listing_of(tree, root)
    assert listing.entries == tree.entries
    assert dict(listing.defaults_carriers) == {".": "_defaults.yaml"}
    assert listing.root == root
    assert pt.list_tree(root, da_guard_binary).entries == tree.entries


# ── config_diff's empty side ────────────────────────────────────────────

def test_the_empty_tree_placeholder_is_a_document_the_reader_accepts() -> None:
    """A first import compares against `_EMPTY_TREE`; if it is ever read
    through `check_shape` / `tree_of` it must be accepted, and its version
    says plainly that no da-guard wrote it."""
    import config_diff
    doc = json.loads(json.dumps(config_diff._EMPTY_TREE))
    pt.check_shape(doc)
    tree = pt.tree_of(doc)
    assert tree.entries == () and dict(tree.selected) == {}
    assert "placeholder" in config_diff._EMPTY_TREE["da_guard_version"]


# ── the reader's own refusals, each named (#3206) ───────────────────────

def _entry_change(**kv):
    def change(d):
        d["entries"][0].update(kv)
    return change


@pytest.mark.parametrize("change, match", [
    (lambda d: d["entries"].__setitem__(0, "x"), "is not an object"),
    (lambda d: d["entries"][0].pop("role"), "no role"),
    (_entry_change(extra=1), "fields this reader does not know"),
    (_entry_change(path=""), "has no path"),
    (_entry_change(depth=-1), "depth is not a count"),
    (_entry_change(depth=True), "depth is not a count"),
    (_entry_change(**{"class": "hidden"}), "class 'hidden' is not one"),
    (_entry_change(tenant_named=""), "tenant_named does not match"),
    (_entry_change(defaults_document="no"), "defaults_document is not a boolean"),
    (_entry_change(readers="exporter"), "readers is not a list"),
    (_entry_change(not_read=""), "not_read is not a reason"),
    (_entry_change(not_read="unselected_defaults_carrier"), "with not_read"),
    (_entry_change(problem={"kind": "read_failure"}), "problem is not"),
    (lambda d: d.update(mode="other"), "mode 'other'"),
    (lambda d: d.update(entries={}), "'entries' missing or not a list"),
])
def test_a_list_entry_wrong_anywhere_is_named(tmp_path, monkeypatch, change, match) -> None:
    doc = _list_doc([_entry("a.yaml")])
    change(doc)
    monkeypatch.setenv("DA_GUARD_BINARY", _emit(tmp_path, doc))
    with pytest.raises(pt.ParseTreeError, match=match):
        pt.list_tree(tmp_path)


def _script(tmp: pathlib.Path, body: str) -> str:
    from _platform_fs import require_shebang_scripts
    require_shebang_scripts()
    exe = tmp / "da-guard"
    exe.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    exe.chmod(0o755)
    return str(exe)


def test_a_da_guard_without_list_only_is_named_as_older(tmp_path) -> None:
    """A da-guard that exits 2 for `--list-only` and does not accept it with
    `-h` either is named as older than the tool, by the flag it lacks."""
    exe = _script(tmp_path, "echo 'flag provided but not defined: -list-only' >&2\nexit 2\n")
    with pytest.raises(pt.ParseTreeError, match=r"does not know `parse-tree --list-only`.*older"):
        pt.list_tree(tmp_path, exe)


def test_a_da_guard_that_hangs_is_a_named_timeout(tmp_path) -> None:
    exe = _script(tmp_path, "sleep 5\n")
    with pytest.raises(pt.ParseTreeError, match="did not finish within 0.2s"):
        pt.list_tree(tmp_path, exe, 0.2)


def test_a_da_guard_that_cannot_be_run_is_named(tmp_path) -> None:
    exe = tmp_path / "da-guard"
    exe.write_text("not a program", encoding="utf-8")
    exe.chmod(0o755)
    with pytest.raises(pt.ParseTreeError, match="could not be run"):
        pt.list_tree(tmp_path, str(exe))


def test_no_binary_for_a_listing_is_binary_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("DA_GUARD_BINARY", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(pt.BinaryMissing):
        pt.list_tree(tmp_path)
    with pytest.raises(pt.BinaryMissing):
        pt.classify_names(["never-asked-before.yaml"])


# ── CLI level: one index per conf.d per run (#3206 M2/M3) ──────────────

_CONFD = {
    "_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
    "db-a.yaml": "tenants:\n  db-a:\n    mysql_connections: '70'\n",
    "team/db-b.yaml": "tenants:\n  db-b: {}\n",
}


def _counting_da_guard(tmp: pathlib.Path, real: str) -> tuple[str, pathlib.Path]:
    """A da-guard that logs `<parent command>\\t<args>` per run, then runs the
    real one."""
    from _platform_fs import require_shebang_scripts
    require_shebang_scripts()
    if not os.path.isdir("/proc/self"):
        pytest.skip("no /proc to name the parent process")
    log = tmp / "da-guard-calls.log"
    exe = tmp / "da-guard-counting"
    exe.write_text(
        "#!/bin/sh\n"
        "p=$(tr '\\0' ' ' < /proc/$PPID/cmdline 2>/dev/null)\n"
        f"printf '%s\\t%s\\n' \"$p\" \"$*\" >> '{log}'\n"
        f"exec '{real}' \"$@\"\n", encoding="utf-8")
    exe.chmod(0o755)
    return str(exe), log


def _tree_indexes(log: pathlib.Path, confd: pathlib.Path) -> list[str]:
    """The parse-tree runs that index *confd* whole (tree or list mode; file
    and names mode index no tree), but those the route generator's own
    process makes when a tool renders through it (a separate product run)."""
    out = []
    for line in log.read_text(encoding="utf-8").splitlines() if log.exists() else []:
        parent, _, args = line.partition("\t")
        a = args.split()
        if (a[:1] == ["parse-tree"] and "--path" not in a and "--names" not in a
                and "--config-dir" in a and a[a.index("--config-dir") + 1] == str(confd)
                and "generate_alertmanager_routes" not in parent):
            out.append(args)
    return out


@pytest.mark.parametrize("tool, argv", [
    ("ops/deprecate_rule.py", ["mysql_connections", "--config-dir", "{d}"]),
    ("ops/offboard_tenant.py", ["db-a", "--config-dir", "{d}"]),
    ("ops/validate_config.py", ["--config-dir", "{d}"]),
    ("lint/check_confd_schema.py", ["--config-dir", "{d}"]),
    ("ops/explain_keys.py", ["--config-dir", "{d}", "--tenant", "db-a"]),
    ("ops/config_diff.py", ["--old-dir", "{d}", "--new-dir", "{d2}"]),
])
def test_a_tool_run_indexes_each_conf_d_once(tool, argv, tmp_path, da_guard_binary) -> None:
    """Counted at the da-guard boundary, for the whole CLI run: every
    parse-tree run that indexes a conf.d is logged, and each conf.d is
    indexed ONCE — a second index is a second snapshot, and a file in one and
    not the other vanishes silently. (`deprecate_rule`'s scratch copy is
    another directory; the route generator `config_diff` renders through is
    its own product run.)"""
    exe, log = _counting_da_guard(tmp_path, da_guard_binary)
    d = _write(tmp_path / "conf.d", _CONFD)
    d2 = _write(tmp_path / "conf2.d", {**_CONFD, "db-a.yaml": "tenants:\n  db-a:\n    "
                                       "mysql_connections: '60'\n"})
    args = [a.format(d=d, d2=d2) for a in argv]
    env = dict(os.environ, DA_GUARD_BINARY=exe)
    r = subprocess.run([sys.executable, "-X", "utf8", str(TOOLS / tool), *args],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=600, check=False, env=env, cwd=tmp_path)
    assert "Traceback" not in r.stderr, r.stderr[-3000:]
    for root in ([d, d2] if "--new-dir" in argv else [d]):
        runs = _tree_indexes(log, root)
        assert len(runs) == 1, (f"{tool} indexed {root} {len(runs)} times: {runs}\n"
                                f"rc {r.returncode}\n{r.stderr[-1500:]}")


# ── round 2: the index is complete; nothing is judged by name or stat ────

def test_migrate_conf_d_plans_only_what_the_walk_enters(tmp_path, da_guard_env) -> None:
    """A directory the walk enters (role `dir`, a config-named one too) is
    already nested; a link to a directory is NOT — the exporter does not
    follow it, nothing under it is loaded — so the plan is refused, rc 2,
    naming it, whatever the link is called."""
    base = {"a.yaml": "tenants:\n  a: {}\n", "team/t.yaml": "tenants:\n  t: {}\n",
            "x.yaml/i.yaml": "tenants:\n  i: {}\n"}
    conf = _write(tmp_path / "ok.d", base)
    r = _run_tool(TOOLS / "dx" / "migrate_conf_d.py", "--conf-d", str(conf), "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Skip (nested):      2" in r.stdout, r.stdout
    outside = tmp_path / "outside"
    outside.mkdir()
    for link in ("link.yaml", "team-link"):
        conf = _write(tmp_path / f"{link}.d", base)
        symlink_or_skip(outside, conf / link, target_is_directory=True)
        r = _run_tool(TOOLS / "dx" / "migrate_conf_d.py", "--conf-d", str(conf), "--dry-run")
        assert r.returncode == 2, (link, r.stdout + r.stderr)
        assert link in r.stderr and "Traceback" not in r.stderr, r.stderr


def test_migrate_conf_d_refuses_an_unlistable_directory_at_any_depth(
        tmp_path, da_guard_env, unlistable_dir) -> None:
    import migrate_conf_d
    conf = _write(tmp_path / "conf.d", {"a.yaml": "tenants:\n  a: {}\n",
                                        "team/deep/t.yaml": "tenants:\n  t: {}\n"})
    unlistable_dir(conf, conf / "team" / "deep")
    with pytest.raises(pt.FileUnreadable) as exc:
        migrate_conf_d.plan_migration(conf, _lib_parse_cache.ConfdReader(conf))
    assert exc.value.file == "team/deep"


def test_tenant_files_are_go_s_class_and_role(tmp_path, da_guard_binary) -> None:
    """`tenant_files` is the config files of class tenant (Go's), not a `_`
    name rule; what is no config file is refused by the tenant-set reader
    (operator_generate / migrate_to_operator), named — never dropped."""
    root = _write(tmp_path / "conf.d", {"a.yaml": "tenants:\n  a: {}\n",
                                        "_defaults.yaml": "defaults: {}\n",
                                        "x.yaml/b.yaml": "tenants:\n  b: {}\n"})
    symlink_or_skip("nowhere.yaml", root / "gone.yaml")
    reader = _lib_parse_cache.ConfdReader(root, binary=da_guard_binary)
    assert _lib_parse_cache.tenant_files(reader.tree()) == ["a.yaml", "x.yaml/b.yaml"]
    with pytest.raises(_lib_confd.TenantSetUnknown, match=r"gone\.yaml.*x\.yaml|x\.yaml.*gone"):
        _lib_confd.tree_tenants(root, reader=reader)
    (root / "gone.yaml").unlink()
    import shutil
    shutil.rmtree(root / "x.yaml")
    assert _lib_confd.tree_tenants(root, reader=_lib_parse_cache.ConfdReader(
        root, binary=da_guard_binary)).tenants == ["a"]


def test_an_unlistable_directory_is_named_whatever_its_name(tmp_path, da_guard_env,
                                                            unlistable_dir) -> None:
    """No name filter between Go's flag and the reader (#3206 N1): a
    directory that cannot be listed, or a link to a directory not followed,
    is named though its name is no config file name."""
    root = _write(tmp_path / "conf.d", {"a.yaml": "x: 1\n", "sealed/s.yaml": "x: 1\n"})
    outside = tmp_path / "outside"
    outside.mkdir()
    symlink_or_skip(outside, root / "team", target_is_directory=True)
    unlistable_dir(root, root / "sealed")
    listing = _lib_confd.config_listing(root)
    named = _lib_confd.unusable_config_entries(sorted(root.iterdir()), listing=listing)
    assert [p.name for p in named] == ["sealed", "team"]
    assert listing.entry("sealed").unlistable_dir and listing.entry("team").not_a_file


def test_a_plain_directory_is_an_entry_not_a_finding(tmp_path, da_guard_env) -> None:
    root = _write(tmp_path / "conf.d", {"team/t.yaml": "x: 1\n", "x.yaml/i.yaml": "x: 1\n"})
    listing = _lib_confd.config_listing(root)
    assert listing.entry("team").is_dir and not listing.entry("team").flagged
    assert listing.entry("x.yaml").is_dir and listing.entry("x.yaml").flagged
    tree = _lib_confd.list_config_tree(root)
    assert [p.name for p in tree.unusable] == ["x.yaml"] and tree.unscannable == []
    assert [p.relative_to(root).as_posix() for p in tree.files] == ["team/t.yaml", "x.yaml/i.yaml"]


def test_assemble_config_dir_names_an_unlistable_source_as_nobody() -> None:
    """A source directory this uid cannot list: rc 2 with an `ERROR:` line
    naming it — never a traceback, never an empty merge."""
    with unprivileged_sandbox(str(REPO)) as (box, run):
        box = pathlib.Path(box)
        src = _write(box / "shard", {"a.yaml": "tenants:\n  a: {}\n"})
        for p in src.rglob("*"):
            p.chmod(0o644)
        src.chmod(0)
        r = run([sys.executable, str(box / "scripts" / "tools" / "ops" / "assemble_config_dir.py"),
                 "--sources", str(src), "--output", str(box / "out")])
        assert r.returncode == 2, r.stdout + r.stderr
        assert "Traceback" not in r.stderr and "ERROR:" in r.stderr, r.stderr
        assert "cannot be listed" in r.stderr, r.stderr


def test_init_project_refuses_a_non_utf8_name_with_an_error_line(tmp_path, da_guard_env) -> None:
    """A conf.d holding a name that is not UTF-8: da-guard refuses the tree,
    and init ends rc 2 with one `ERROR:` line, no traceback (#3206 N4)."""
    out = tmp_path / "o"
    (out / "conf.d").mkdir(parents=True)
    _bad_name(out / "conf.d", b"bad-\xff.yaml")
    for deploy in ("helm", "kustomize"):
        r = _run_tool(TOOLS / "ops" / "init_project.py", "--non-interactive", "--tenants", "a",
                      "--deploy", deploy, "--force", "-o", str(out))
        assert r.returncode == 2, (deploy, r.stdout + r.stderr)
        assert "Traceback" not in r.stderr and "ERROR:" in r.stderr, r.stderr


@pytest.mark.parametrize("name", ["sealed", "y.yaml"])
def test_migrate_conf_d_refuses_an_unlistable_directory_as_nobody(name) -> None:
    """M1, for real: a directory this uid cannot list — plain or with a
    config file's name — was rc 0 ("already nested") on the base; it is rc 2,
    named: what is under it is unknown, so no plan is complete."""
    with unprivileged_sandbox(str(REPO)) as (box, run):
        box = pathlib.Path(box)
        conf = _write(box / "conf.d", {"a.yaml": "tenants:\n  a: {}\n",
                                       f"{name}/s.yaml": "tenants:\n  s: {}\n"})
        for p in conf.rglob("*"):
            p.chmod(0o755 if p.is_dir() else 0o644)
        (conf / name).chmod(0)
        _probe_denied(run, conf / name / "s.yaml")
        r = run([sys.executable, str(box / "scripts" / "tools" / "dx" / "migrate_conf_d.py"),
                 "--conf-d", str(conf), "--dry-run"])
        assert r.returncode == 2, r.stdout + r.stderr
        assert name in r.stderr and "Traceback" not in r.stderr, r.stderr
