#!/usr/bin/env python3
"""test_lib_toolcount.py — the single definition of "what is a Python tool".

⛔ #1511: three modules implemented this predicate independently — the
gate that CHECKS the "N 個 Python 工具" number, the writer that puts it
into README.md / README.en.md, and the generator that writes
docs/internal/tool-map.md. The writer skipped no filename prefixes at
all, so the three agreed only because `ops/`, `dx/` and `lint/` happen to
hold no `_lib*` or `__init__.py`.

Every assertion here runs against a SYNTHETIC tree, not the repo's own.
On the repo tree the interesting shapes are absent, so a real-tree test
cannot tell a working predicate from a stub: emptying
`TOOL_SKIP_PREFIXES` leaves today's count untouched. The tree built by
`_build_tree` contains one of every shape the predicate has to judge.
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
            assert tc.is_tool_file(Path(name)), name
        for name in ("_lib_io.py", "__init__.py", "notes.txt", "README.md"):
            assert not tc.is_tool_file(Path(name)), name

    def test_the_counted_helpers_are_counted_on_purpose(self):
        """⚠️ `_lib` is the convention; `_grar_*` and `_registry_lib.py` are
        helper modules that do not follow it, so they count as tools today.
        Pinned so "the count includes some libraries" stays a known fact
        rather than a surprise the next reader has to re-derive.
        """
        assert tc.is_tool_file(Path("_grar_parse.py"))
        assert tc.is_tool_file(Path("_observed_map_lib.py"))


class TestTheTwoScopes:
    def test_count_scope_is_the_documented_scope(self, tmp_path):
        tools = _build_tree(tmp_path)

        assert _names(tc.count_scope(tools)) == [
            "alpha.py", "beta.py", "delta.py", "gamma.py"]
        assert tc.count_by_subdir(tools) == {"ops": 2, "dx": 1, "lint": 1}

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

        assert tc.count_by_subdir(tools) == {"ops": 1, "dx": 0, "lint": 0}
        assert tc.count_scope(tmp_path / "nowhere") == []


class TestAllThreeConsumers:
    """The three call sites must read one definition, not three.

    Measured against the pre-#1511 implementations on this exact tree:
    the checker said 4, the writer said 6 (it counted `dx/__init__.py` and
    `lint/_lib_helper.py`). From that gap `bump_docs --sync-counts` wrote
    the writer's number into README, the gate warned that it did not match
    the checker's, `--fix` wrote the checker's back, and neither tool could
    clear the warning.
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

        assert checker._count_python_tools() == 4
        assert writer._count_python_tools() == (4, 2, 1, 1)

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

    def test_unifying_the_two_scopes_would_turn_a_live_gate_red(
            self, tmp_path, monkeypatch):
        """⛔ Why the generator must NOT adopt `count_scope`.

        `check_tool_map_coverage` requires every repo-root tool to appear
        in tool-map.md. A tool map built from `count_scope` omits them, so
        "one scope for all three" would trade this warning for the tidier
        code. Both directions are asserted — the wrong scope must be
        reported AND the right scope must be silent — or this only proves
        the gate is noisy.
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
