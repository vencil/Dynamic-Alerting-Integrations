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
    refused), `sealed/` cannot be listed; `gone.yaml` dangles, `dir.yaml/` is
    a config-named directory, `broken.yaml` a syntax error."""
    conf, locked = box / "conf.d", box / "locked"
    for d in (conf, locked, conf / "sealed", conf / "noexec", conf / "dir.yaml"):
        d.mkdir()
        d.chmod(0o755)
    for p, body in {conf / "ok.yaml": "tenants: {ok: {}}\n",
                    conf / "zero.yaml": "tenants: {zero: {}}\n",
                    conf / "broken.yaml": "tenants: {b: [\n",
                    locked / "real.yaml": "tenants: {b: {}}\n",
                    conf / "sealed" / "s.yaml": "tenants: {s: {}}\n",
                    conf / "noexec" / "t.yaml": "tenants: {t: {}}\n",
                    conf / "dir.yaml" / "in.yaml": "tenants: {in: {}}\n"}.items():
        p.write_text(body, encoding="utf-8")
        p.chmod(0o644)
    symlink_or_skip(locked / "real.yaml", conf / "b.yaml")
    symlink_or_skip("nowhere.yaml", conf / "gone.yaml")
    for p, mode in ((conf / "zero.yaml", 0), (locked, 0), (conf / "sealed", 0),
                    (conf / "noexec", 0o444)):
        p.chmod(mode)
    return conf


_WANT_ROLES = {
    "b.yaml": "config", "noexec/t.yaml": "config", "zero.yaml": "config",
    "ok.yaml": "config", "broken.yaml": "config", "dir.yaml/in.yaml": "config",
    "gone.yaml": "not_a_file", "dir.yaml": "not_a_file", "sealed": "unlistable_dir",
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
    # List mode: tree mode's entries, but for the problems only reading finds.
    def without_content_problem(e: lpt.Entry) -> lpt.Entry:
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
                 problem=("read_failure", "the path cannot be statted (stat_error)"))
    got = {role: lpt.Entry(**facts, role=role) for role in sorted(lpt.ROLES)}
    assert {r: (e.config_file, e.not_a_file, e.unlistable_dir) for r, e in got.items()} == {
        "config": (True, False, False),
        "not_a_file": (False, True, False),
        "unlistable_dir": (False, False, True),
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
        "_defaults.yaml", "_defaults.yml", "t1.yaml", "team/_domain_policy.yaml"]
    assert dict(listing.defaults_carriers) == {".": "_defaults.yaml"}
    assert listing.entry("_defaults.yml").not_read == "unselected_defaults_carrier"
    assert listing.entry("team/_domain_policy.yaml").readers == ("generator",)
    assert [e.path for e in listing.config_files()] == [e.path for e in listing.entries]
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
             "role": "config", "problem": None}
    listed = {"schema": lpt.SCHEMA, "da_guard_version": "t", "mode": "list",
              "defaults_carriers": {}, "entries": [entry]}
    import json
    with pytest.raises(lpt.ParseTreeError, match="mode 'list'"):
        lpt.read_document(json.dumps(listed).encode())
    tree = {**listed, "mode": "tree", "files": {"t.yaml": {}}, "rendered_by": {}}
    with pytest.raises(lpt.ParseTreeError, match="list mode with 'files'"):
        lpt.read_document(json.dumps(tree).encode(), check=lpt._check_list("list"))
