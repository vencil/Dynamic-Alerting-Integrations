"""#3424 — the reader binaries' environment variables: one register, and a
relative path in any of them made absolute before a directory change.

`_lib_consumer_read.READER_BINARY_ENVS` names da-guard's, da-read's and
da-crdecode's variable; `absolute_reader_binaries()` makes each relative
value absolute against the current working directory. The generator
service calls it at start-up, before its judgement process changes into a
job directory and runs the route generator there (da-guard, da-read,
da-crdecode): before #3424 it made only `$DA_GUARD_BINARY` absolute, so a
relative `$DA_READ_BINARY` / `$DA_CRDECODE_BINARY` named nothing in the job
directory.

The behaviour test runs the three real binaries through their own
resolvers (`_lib_consumer_read.resolve_binary` for da-read and da-crdecode,
`_lib_parse_tree` → `guard_dispatch` for da-guard) from another directory —
and, as its control, shows the same setting without the call finds none of
them there.

Two AST guards over `scripts/tools/**/*.py` keep it that way: no string
constant or keyword argument outside the register is one of the env names,
and a module that calls `chdir` also calls `absolute_reader_binaries` (or is
in `_CHDIR_EXEMPT` with its reason; an entry that no longer applies is red).
"""
from __future__ import annotations

import ast
import os
import shutil
import sys
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parents[2] / "scripts" / "tools"
for _p in (_TOOLS, _TOOLS / "ops"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import _lib_consumer_read as cr  # noqa: E402
import _lib_parse_tree as lpt  # noqa: E402
from _tree import REPO_ROOT, repo_files  # noqa: E402

_ALERTMANAGER = "route:\n  receiver: x\nreceivers:\n  - name: x\n"


def test_the_register_names_the_three_readers() -> None:
    assert sorted(cr.READER_BINARY_ENVS) == sorted(
        [cr.DA_GUARD_ENV, cr.DA_READ_ENV, cr.CRDECODE_ENV])
    assert len(set(cr.READER_BINARY_ENVS)) == 3


def test_the_other_readers_use_the_register() -> None:
    """The env name a resolver reads is the register's, not a copy."""
    import da_assembler
    import guard_dispatch
    assert guard_dispatch.DISPATCHER.env_var == cr.DA_GUARD_ENV
    assert da_assembler.CRDECODE_ENV is cr.CRDECODE_ENV


def test_every_relative_value_becomes_absolute_against_the_cwd(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    env = {name: f"bin/{name.lower()}" for name in cr.READER_BINARY_ENVS}
    env["UNRELATED"] = "rel/path"
    cr.absolute_reader_binaries(env)
    for name in cr.READER_BINARY_ENVS:
        assert env[name] == str(tmp_path / "bin" / name.lower()), name
    assert env["UNRELATED"] == "rel/path"


@pytest.mark.parametrize("value", [None, "", " ", "\t", "/abs/reader"])
def test_unset_blank_or_absolute_is_left_as_it_is(tmp_path, monkeypatch, value) -> None:
    monkeypatch.chdir(tmp_path)
    env = {} if value is None else {name: value for name in cr.READER_BINARY_ENVS}
    before = dict(env)
    cr.absolute_reader_binaries(env)
    assert env == before


def test_without_an_argument_it_changes_this_process_environment(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    for name in cr.READER_BINARY_ENVS:
        monkeypatch.setenv(name, f"./{name}")
    cr.absolute_reader_binaries()
    for name in cr.READER_BINARY_ENVS:
        assert os.environ[name] == str(tmp_path / name)


def _relative_readers(tmp_path: Path, monkeypatch, da_guard: str, da_read: str,
                      da_crdecode: str) -> tuple[Path, Path]:
    """The three real binaries under <tmp>/shell/bin, named by relative
    paths as a shell in <tmp>/shell would; returns (shell dir, elsewhere)."""
    shell, elsewhere = tmp_path / "shell", tmp_path / "elsewhere"
    (shell / "bin").mkdir(parents=True)
    elsewhere.mkdir()
    for src, name, env in ((da_guard, "da-guard", cr.DA_GUARD_ENV),
                           (da_read, "da-read", cr.DA_READ_ENV),
                           (da_crdecode, "da-crdecode", cr.CRDECODE_ENV)):
        shutil.copy2(src, shell / "bin" / name)
        monkeypatch.setenv(env, f"bin/{name}")
    monkeypatch.chdir(shell)
    return shell, elsewhere


def _run_all_three(conf_d: Path) -> None:
    """Each reader through its own resolver, from the current directory."""
    assert lpt.load(str(conf_d), None, 60).value("t.yaml") == {"a": 1}
    assert cr.kubernetes_text_documents("kind: A\n") == [{"kind": "A"}]
    assert cr.alertmanager_config(_ALERTMANAGER)["route"]["receiver"] == "x"


def test_after_the_call_all_three_run_from_another_directory(
        tmp_path, monkeypatch, da_guard_binary, da_read_binary, da_crdecode_binary) -> None:
    """#3424's done condition: relative settings, the call, a directory
    change — and da-guard, da-read and da-crdecode are each still found and
    run by the resolvers the tools use."""
    shell, elsewhere = _relative_readers(tmp_path, monkeypatch, da_guard_binary,
                                         da_read_binary, da_crdecode_binary)
    conf_d = tmp_path / "conf.d"
    conf_d.mkdir()
    (conf_d / "t.yaml").write_text("a: 1\n", encoding="utf-8")
    cr.absolute_reader_binaries()
    for name in cr.READER_BINARY_ENVS:
        assert os.path.isabs(os.environ[name]), name
    monkeypatch.chdir(elsewhere)
    _run_all_three(conf_d)
    assert cr.resolve_binary(None, cr.DA_READ_ENV, cr.DA_READ_BINARY_NAME) == str(
        shell / "bin" / "da-read")
    assert cr.resolve_binary(None, cr.CRDECODE_ENV, cr.CRDECODE_BINARY_NAME) == str(
        shell / "bin" / "da-crdecode")


def test_control_without_the_call_none_of_them_is_found_elsewhere(
        tmp_path, monkeypatch, da_guard_binary, da_read_binary, da_crdecode_binary) -> None:
    """The same setting without `absolute_reader_binaries`: from another
    directory each relative path names nothing — what the behaviour test
    above would see if the call did nothing."""
    _, elsewhere = _relative_readers(tmp_path, monkeypatch, da_guard_binary,
                                     da_read_binary, da_crdecode_binary)
    conf_d = tmp_path / "conf.d"
    conf_d.mkdir()
    (conf_d / "t.yaml").write_text("a: 1\n", encoding="utf-8")
    monkeypatch.chdir(elsewhere)
    with pytest.raises(lpt.BinaryMissing):
        lpt.load(str(conf_d), None, 60)
    with pytest.raises(cr.ConsumerReadError):
        cr.kubernetes_text_documents("a: 1\n")
    with pytest.raises(cr.ConsumerReadError):
        cr.alertmanager_config(_ALERTMANAGER)


# ── guard: the register is the only place the names are written, and every
#    module that changes directory makes the readers absolute first ─────────

_REGISTER = "scripts/tools/_lib_consumer_read.py"
_ENV_NAMES = frozenset({"DA_GUARD_BINARY", "DA_READ_BINARY", "DA_CRDECODE_BINARY"})

#: Modules that change directory without calling `absolute_reader_binaries`,
#: each with the reason it needs none (checked by reading the module).
_CHDIR_EXEMPT = {
    "scripts/tools/ops/_gsvc_child.py":
        "the judgement process: forked by generator_service, whose main made "
        "every reader binary absolute before the service started",
    "scripts/tools/lint/check_cli_contract.py":
        "changes into a scratch dir only around argparse's parse_args (a "
        "type= callable must not touch the checkout) and back in finally; no "
        "reader binary runs there",
}


def _tools_sources() -> "list[tuple[str, str]]":
    out = []
    for path in repo_files(".py"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel.startswith("scripts/tools/"):
            out.append((rel, path.read_text(encoding="utf-8")))
    return out


def _env_name_literals(sources: "list[tuple[str, str]]") -> "list[str]":
    """Every string constant that IS one of the env names (a longer string
    that mentions one is not), and every keyword argument named like one,
    outside the register."""
    found = []
    for rel, text in sources:
        if rel == _REGISTER:
            continue
        for node in ast.walk(ast.parse(text, filename=rel)):
            if isinstance(node, ast.Constant) and node.value in _ENV_NAMES:
                found.append(f"{rel}:{node.lineno}: {node.value!r}")
            elif isinstance(node, ast.keyword) and node.arg in _ENV_NAMES:
                found.append(f"{rel}:{node.value.lineno}: {node.arg}=")
    return found


def _called_names(tree: ast.AST) -> "set[str]":
    """The name of every function called (`f()` → f, `m.f()` → f)."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                names.add(node.func.attr)
    return names


def _chdir_problems(sources: "list[tuple[str, str]]",
                    exempt: "dict[str, str]") -> "list[str]":
    """A module that calls chdir without calling `absolute_reader_binaries`
    and is not exempt; an exempt module that no longer calls chdir, or that
    calls the function after all, or that is gone (a dead entry)."""
    problems, seen = [], set()
    for rel, text in sources:
        called = _called_names(ast.parse(text, filename=rel))
        chdirs = "chdir" in called
        absolutes = "absolute_reader_binaries" in called
        if rel in exempt:
            seen.add(rel)
            if absolutes:
                problems.append(f"{rel}: exempt, but calls absolute_reader_binaries — drop the entry")
            elif not chdirs:
                problems.append(f"{rel}: exempt, but no longer changes directory — drop the entry")
        elif chdirs and not absolutes:
            problems.append(f"{rel}: changes directory without calling "
                            "_lib_consumer_read.absolute_reader_binaries() first")
    problems += [f"{rel}: exempt but not found" for rel in sorted(set(exempt) - seen)]
    return problems


def test_no_env_name_literal_outside_the_register() -> None:
    sources = _tools_sources()
    assert any(rel == _REGISTER for rel, _ in sources), "the scan did not see the register"
    assert _env_name_literals(sources) == []


def test_every_module_that_changes_directory_makes_the_readers_absolute() -> None:
    sources = _tools_sources()
    assert len(sources) > 100, len(sources)
    assert _chdir_problems(sources, _CHDIR_EXEMPT) == []


def test_the_guards_see_what_they_are_for() -> None:
    """Each guard, on a source that breaks it, says so."""
    src = ('import os\nos.environ.get("DA_READ_BINARY")\n'
           'dict(os.environ, DA_GUARD_BINARY="g")\n'
           '"set $DA_GUARD_BINARY to it"\n'
           'os.chdir("/")\n')
    bad = [("scripts/tools/x.py", src)]
    assert _env_name_literals(bad) == ["scripts/tools/x.py:2: 'DA_READ_BINARY'",
                                       "scripts/tools/x.py:3: DA_GUARD_BINARY="]
    assert _env_name_literals([(_REGISTER, src)]) == []
    assert len(_chdir_problems(bad, {})) == 1
    fixed = [("scripts/tools/x.py", "import os\nabsolute_reader_binaries()\nos.chdir('/')\n")]
    assert _chdir_problems(fixed, {}) == []
    assert len(_chdir_problems(fixed, {"scripts/tools/x.py": "r"})) == 1      # redundant entry
    assert len(_chdir_problems([("scripts/tools/x.py", "pass\n")],
                               {"scripts/tools/x.py": "r"})) == 1             # no longer chdirs
    assert _chdir_problems([], {"scripts/tools/gone.py": "r"}) == [
        "scripts/tools/gone.py: exempt but not found"]
