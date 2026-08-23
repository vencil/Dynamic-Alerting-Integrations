#!/usr/bin/env python3
"""test_lib_toolcount.py — the single definition of "what is a Python tool".

⛔ #1511: three modules implemented this predicate independently — the
gate that CHECKS the "N 個 Python 工具" number, the writer that puts it
into README.md / README.en.md, and the generator that writes
docs/internal/tool-map.md. The writer skipped no filename prefixes at
all, so the three agreed only because `ops/`, `dx/` and `lint/` happen to
hold no `_lib*` or `__init__.py`.

Every assertion that touches a filesystem runs against a SYNTHETIC tree,
not the repo's own. On the repo tree the interesting shapes are absent,
so a real-tree test cannot tell a working predicate from a stub: measured
on `5cff2359`, emptying `TOOL_SKIP_PREFIXES` leaves the published count
at 220. The tree built by `_build_tree` contains one of every shape the
predicate has to judge.

⚠️ `TestThePredicate` is the exception and says so: it feeds bare
`Path("name.py")` strings and touches no tree at all, so it pins the
predicate's answer for a filename — never that any such file exists here.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_TOOLS = REPO_ROOT / "scripts" / "tools"
for _p in (str(_TOOLS), str(_TOOLS / "lint"), str(_TOOLS / "dx")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _lib_toolcount as tc  # noqa: E402

_STUB = ('#!/usr/bin/env python3\n'
         '"""stub.py — a stub tool."""\n\n\n'
         'def main():\n'
         '    return 0\n')

# Every shape the predicate has to judge, and why it is here.
_TREE = (
    "ops/alpha.py",           # tool
    "ops/beta.py",            # tool
    "dx/gamma.py",            # tool
    "dx/__init__.py",         # skipped: package marker
    "dx/custom/__init__.py",  # nested package — a flat glob cannot see it
    "dx/custom/loader.py",    # library inside that package
    "lint/delta.py",          # tool
    "lint/_lib_helper.py",    # skipped: shared library
    "validate_all.py",        # repo-root tool: tool-map scope only
    "_lib_shared.py",         # skipped at the root too
)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _build_tree(root: Path) -> Path:
    """Write `_TREE` under `root/scripts/tools` and return that directory."""
    tools = root / "scripts" / "tools"
    for rel in _TREE:
        path = tools / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_STUB, encoding="utf-8", newline="\n")
    (tools / "notes.txt").write_text("not python\n",
                                     encoding="utf-8", newline="\n")
    return tools


def _names(entries):
    return sorted(path.name for _subdir, path in entries)


class TestThePredicate:
    def test_it_judges_both_directions(self):
        """A predicate tested only on tools drifts into "accept anything"."""
        for name in ("alpha.py", "validate_all.py", "check_x.py"):
            assert tc.is_tool_file(Path(name)), (
                "%s must be judged a tool; the predicate has drifted "
                "towards rejecting everything" % name)
        for name in ("_lib_io.py", "__init__.py", "notes.txt", "README.md"):
            assert not tc.is_tool_file(Path(name)), (
                "%s must NOT be judged a tool; the predicate has drifted "
                "towards accepting everything" % name)

    def test_underscore_helpers_without_the_lib_prefix_are_still_counted(self):
        """⛔ This pins a CONSEQUENCE nobody has decided to accept.

        `_lib` is the convention for a shared module, and the counted
        subdirectories hold helper modules that do not carry it, so the
        published number includes them. `verify_diff_rules.yaml` matches
        the same files as 「子目錄共用 helper」 — the repo holds both
        opinions at once.

        ⚠️ Read on before "fixing" this: narrowing the predicate here
        LOWERS the number in README.md and README.en.md, so it is a
        decision about the published sentence, not a cleanup. Change this
        test together with that sentence and the tracking ticket, or not
        at all. Deleting the assertions to go green removes the only
        place the trade-off is written down.

        ⚠️ This asserts on a filename, not on the tree — it stays true
        even if every such file is renamed. The live count is measured in
        `TestTheTwoScopes`.
        """
        assert tc.is_tool_file(Path("_grar_parse.py")), (
            "the predicate stopped counting non-`_lib` helper modules; "
            "if that is intended, README's count changes with it")
        assert tc.is_tool_file(Path("_registry_lib.py")), (
            "the predicate stopped counting non-`_lib` helper modules; "
            "if that is intended, README's count changes with it")


class TestTheTwoScopes:
    def test_count_scope_is_the_documented_scope(self, tmp_path):
        tools = _build_tree(tmp_path)

        assert _names(tc.count_scope(tools)) == [
            "alpha.py", "beta.py", "delta.py", "gamma.py"]
        expected = dict.fromkeys(tc.COUNT_SUBDIRS, 0)
        expected.update({"ops": 2, "dx": 1, "lint": 1})
        assert tc.count_by_subdir(tools) == expected, (
            "if this failed only because COUNT_SUBDIRS gained a member, "
            "the expectation above just needs the new key; check first "
            "whether the checker and the writer still agree — that is the "
            "failure worth reading")

    def test_the_skipping_is_doing_work_on_this_tree(self, tmp_path):
        """Must-fire control: an empty `TOOL_SKIP_PREFIXES` must be visible.

        Without this the expectations above would also hold for a tree
        with nothing to skip, and they would prove nothing.
        """
        tools = _build_tree(tmp_path)

        raw_lint = sorted(f.name for f in (tools / "lint").glob("*.py"))
        raw_dx = sorted(f.name for f in (tools / "dx").glob("*.py"))
        assert raw_lint == ["_lib_helper.py", "delta.py"]
        assert raw_dx == ["__init__.py", "gamma.py"]
        counts = tc.count_by_subdir(tools)
        assert counts["lint"] == len(raw_lint) - 1
        assert counts["dx"] == len(raw_dx) - 1

    def test_a_nested_package_stays_invisible(self, tmp_path):
        """⛔ Not `rglob`. Measured on the repo tree at `5cff2359`: 224 vs
        220, because `dx/custom_alerts/` is library code with no `main()`.
        """
        tools = _build_tree(tmp_path)

        assert "loader.py" not in _names(tc.count_scope(tools))
        assert "loader.py" not in _names(tc.tool_map_scope(tools))

    def test_tool_map_scope_adds_the_repo_root_and_nothing_else(
            self, tmp_path):
        tools = _build_tree(tmp_path)

        extra = set(_names(tc.tool_map_scope(tools))) - set(
            _names(tc.count_scope(tools)))
        assert extra == {"validate_all.py"}
        assert "_lib_shared.py" not in _names(tc.tool_map_scope(tools))

    def test_root_entries_are_tagged_so_a_caller_can_tell_them_apart(
            self, tmp_path):
        tools = _build_tree(tmp_path)

        tagged = {path.name: subdir for subdir, path
                  in tc.tool_map_scope(tools)}
        assert tagged["validate_all.py"] is None
        assert tagged["alpha.py"] == "ops"
        assert tagged["delta.py"] == "lint"

    def test_a_missing_subdirectory_reports_zero_rather_than_vanishing(
            self, tmp_path):
        tools = tmp_path / "scripts" / "tools"
        (tools / "ops").mkdir(parents=True)
        (tools / "ops" / "only.py").write_text(_STUB, encoding="utf-8",
                                               newline="\n")

        expected = dict.fromkeys(tc.COUNT_SUBDIRS, 0)
        expected["ops"] = 1
        assert tc.count_by_subdir(tools) == expected, (
            "every declared subdirectory must report a number, including "
            "the missing ones — a caller printing the breakdown would "
            "otherwise drop a line without noticing")
        assert tc.count_scope(tmp_path / "nowhere") == []


class TestAllThreeConsumers:
    """The three call sites must read one definition, not three.

    Measured against the pre-#1511 implementations on this exact tree:
    the checker said 4, the writer said `(6, 2, 2, 2)` (it counted
    `dx/__init__.py` and `lint/_lib_helper.py`). From that gap
    `bump_docs --sync-counts` wrote the writer's number into README, the
    gate warned that it did not match the checker's, `--fix` wrote the
    checker's back, and neither tool could clear the warning.

    ⛔ The first repair for that shared the scan but not the total: the
    writer re-added `counts["ops"] + counts["dx"] + counts["lint"]`.
    Measured with a fourth subdirectory declared in `COUNT_SUBDIRS`:
    checker 221, writer 220. `test_the_breakdown_follows_the_declared_scope`
    is the control for that, and it is the reason the writer returns a
    mapping instead of a fixed-width tuple.
    """

    def _consumers(self, tmp_path, monkeypatch):
        tools = _build_tree(tmp_path)
        checker = _load("vdv_toolcount",
                        _TOOLS / "lint" / "validate_docs_versions.py")
        writer = _load("bump_docs_toolcount", _TOOLS / "dx" / "bump_docs.py")
        generator = _load("gtm_toolcount",
                          _TOOLS / "dx" / "generate_tool_map.py")
        monkeypatch.setattr(checker, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(writer, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(generator, "TOOLS_ROOT", tools)
        return checker, writer, generator

    def test_the_checker_and_the_writer_cannot_disagree(
            self, tmp_path, monkeypatch):
        checker, writer, _generator = self._consumers(tmp_path, monkeypatch)

        total, breakdown = writer._count_python_tools()
        expected = dict.fromkeys(tc.COUNT_SUBDIRS, 0)
        expected.update({"ops": 2, "dx": 1, "lint": 1})
        assert checker._count_python_tools() == 4
        assert total == 4
        assert breakdown == expected, (
            "if this failed only because COUNT_SUBDIRS gained a member, "
            "the fixture tree just needs a file for it; the failure worth "
            "reading is checker != writer above")

    def test_the_breakdown_follows_the_declared_scope(
            self, tmp_path, monkeypatch):
        """⛔ Control for the half the first repair missed.

        Sharing the scan is not enough: the writer used to re-add its
        total from three hard-coded keys, so a fourth declared
        subdirectory made it disagree with the checker again — the same
        divergence one layer down. This declares one and requires both
        sides to move.

        ⚠️ The name is deliberately one no real subdirectory uses. An
        earlier version appended `"release"`, which is also the name the
        tripwire's own fixture uses; had it ever become a real counted
        subdirectory the tuple would have held it twice.
        """
        checker, writer, _generator = self._consumers(tmp_path, monkeypatch)
        extra = tmp_path / "scripts" / "tools" / "zzscope"
        extra.mkdir()
        (extra / "cut_release.py").write_text(_STUB, encoding="utf-8",
                                              newline="\n")
        monkeypatch.setattr(tc, "COUNT_SUBDIRS",
                            tc.COUNT_SUBDIRS + ("zzscope",))

        total, breakdown = writer._count_python_tools()
        checked = checker._count_python_tools()
        assert checked == 5, (
            "the checker read %d, not 5, for a tree holding 5 counted "
            "files across the declared subdirectories" % checked)
        assert total == 5, (
            "the writer read %d while the checker read %d: its total is "
            "bound to something other than COUNT_SUBDIRS, so it will "
            "write a number the gate does not check" % (total, checked))
        assert breakdown.get("zzscope") == 1, (
            "the breakdown dropped a declared subdirectory silently; "
            "`--sync-counts` prints it, so the line would just vanish. "
            "Got: %r" % (breakdown,))

    def test_a_repeated_subdirectory_is_not_counted_twice(
            self, tmp_path, monkeypatch):
        """⛔ A duplicate must not inflate the published number.

        Measured before `scan` collapsed them: a tuple holding the same
        name twice walked the directory twice and reported 5 files for a
        tree holding 4, with `count_by_subdir` saying `2`. A wrong number
        that looks entirely normal is the failure this module exists to
        remove, so it must not be reachable through its own scope knob.
        """
        checker, writer, _generator = self._consumers(tmp_path, monkeypatch)
        monkeypatch.setattr(tc, "COUNT_SUBDIRS",
                            tc.COUNT_SUBDIRS + ("ops",))

        total, breakdown = writer._count_python_tools()
        assert checker._count_python_tools() == 4, "duplicate inflated the gate"
        assert total == 4, "duplicate inflated the writer's total"
        assert breakdown["ops"] == 2, (
            "duplicate inflated the breakdown: %r" % (breakdown,))

    def test_the_generator_keeps_the_root_and_only_the_root(
            self, tmp_path, monkeypatch):
        checker, _writer, generator = self._consumers(tmp_path, monkeypatch)

        categorized = generator.gather_tools()
        listed = sorted(name for rows in categorized.values()
                        for name, _desc in rows)
        assert listed == ["alpha.py", "beta.py", "delta.py", "gamma.py",
                          "validate_all.py"]
        assert len(listed) == checker._count_python_tools() + 1
        assert "validate_all.py" in [n for n, _d in categorized["ops"]]

    def test_unifying_the_two_scopes_leaves_an_unclearable_warning(
            self, tmp_path, monkeypatch):
        """⛔ Why the generator must NOT adopt `count_scope`.

        `check_tool_map_coverage` requires every repo-root tool to appear
        in tool-map.md. A tool map built from `count_scope` omits them, so
        "one scope for all three" buys a report no regeneration can clear.

        ⚠️ Named for what was measured, not for what sounds worse: this
        check emits `warn`, and `validate_docs_versions --ci` exits **0**
        with the warning present. An earlier name said "turn a live gate
        red"; that was false, and this module argues elsewhere that a
        warning nobody can satisfy is the disease, so the accurate name is
        also the stronger one.

        Both directions are asserted — the wrong scope must be reported
        AND the right scope must be silent — or this only proves the gate
        is noisy.
        """
        checker, _writer, _generator = self._consumers(tmp_path, monkeypatch)
        internal = tmp_path / "docs" / "internal"
        internal.mkdir(parents=True)
        monkeypatch.setattr(checker, "DOCS_DIR", tmp_path / "docs")
        tools = tmp_path / "scripts" / "tools"
        tool_map = internal / "tool-map.md"

        def _write_map(entries):
            tool_map.write_text(
                "".join("- `%s`\n" % path.name for _subdir, path in entries),
                encoding="utf-8", newline="\n")

        _write_map(tc.count_scope(tools))
        missed = checker.check_tool_map_coverage()
        assert [i.message for i in missed] == [
            "tool not listed in tool-map: validate_all.py"]

        _write_map(tc.tool_map_scope(tools))
        assert checker.check_tool_map_coverage() == []
