"""`_lib_parse_tree`: the one reader of `da-guard parse-tree`'s document.

ADR-036 makes Go the only YAML parser of conf.d; a Python tool reads a file's
content from parse-tree's JSON. migration-audit had the only reader; #1516 P3
(config-diff's carrier table) needs the same one, so it moved here and both
use it. What the document must look like, and every way of refusing one, is
held through migration-audit's CLI in `test_migration_audit.py`
(`test_cli_refuses_a_da_guard_answer_it_cannot_read`,
`test_cli_refuses_a_parse_tree_document_of_the_wrong_shape`); this module
holds the library's own contract and that migration-audit goes through it
instead of keeping a copy.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "tools"))
sys.path.insert(0, str(ROOT / "scripts" / "tools" / "ops"))

import _lib_parse_tree as lpt  # noqa: E402
import migration_audit as ma  # noqa: E402


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "conf.d"
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


def test_run_reads_each_file_as_tagged_nodes(tmp_path, da_guard_binary) -> None:
    root = _tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  cpu: 80\n_routing_defaults:\n  receiver:\n    type: email\n",
        "team/t1.yaml": "tenants:\n  t1:\n    cpu: \"90\"\n",
    })
    doc = lpt.run(root, da_guard_binary, 60)
    assert doc["schema"] == lpt.SCHEMA
    assert set(doc["files"]) == {"_defaults.yaml", "team/t1.yaml"}
    d = doc["files"]["_defaults.yaml"]
    assert set(d) == {"defaults", "_routing_defaults"}
    cpu = d["defaults"]["cpu"]
    assert lpt.is_scalar(cpu) and cpu["text"] == "80" and cpu["tag"] == "!!int", cpu
    quoted = doc["files"]["team/t1.yaml"]["tenants"]["t1"]["cpu"]
    assert quoted["tag"] == "!!str" and quoted["value"] == "90", quoted
    assert not lpt.is_scalar(d["_routing_defaults"])


def test_no_binary_is_its_own_error(tmp_path) -> None:
    root = _tree(tmp_path, {"t.yaml": "a: 1\n"})
    with pytest.raises(lpt.BinaryMissing) as e:
        lpt.run(root, str(tmp_path / "no-such-da-guard"), 60)
    assert isinstance(e.value, lpt.ParseTreeError)
    assert not str(e.value).startswith("Error: "), str(e.value)


def test_a_failing_binary_is_a_parse_tree_error(tmp_path) -> None:
    root = _tree(tmp_path, {"t.yaml": "a: 1\n"})
    fake = tmp_path / "fake-da-guard"
    fake.write_text("#!/bin/sh\necho boom >&2\nexit 3\n", encoding="utf-8")
    fake.chmod(0o755)
    with pytest.raises(lpt.ParseTreeError, match="exited 3: boom") as e:
        lpt.run(root, str(fake), 60)
    assert not isinstance(e.value, lpt.BinaryMissing)


@pytest.mark.parametrize("node, scalar", [
    ({"text": "80", "tag": "!!int", "value": "80"}, True),
    ({"text": "x", "tag": "!!str", "value": None}, True),
    ({"text": {"text": "x", "tag": "!!str", "value": "x"}}, False),  # a mapping whose key is "text"
    ({"text": "x"}, False),
    ({"a": 1}, False),
    ([], False),
    ("80", False),
])
def test_is_scalar(node, scalar) -> None:
    assert lpt.is_scalar(node) is scalar


# ── migration-audit goes through this module, it does not keep a copy ───

def test_migration_audit_reads_through_the_shared_reader(tmp_path, monkeypatch) -> None:
    calls = []

    def fake(root, binary, timeout):
        calls.append((root, binary, timeout))
        return {"sentinel": True}

    monkeypatch.setattr(lpt, "run", fake)
    assert ma.run_parse_tree(tmp_path, "bin", 5) == {"sentinel": True}
    assert calls == [(tmp_path, "bin", 5)]
    assert ma._is_v3_scalar is lpt.is_scalar
    assert (ma.PARSE_TREE_SUBCOMMAND, ma.PARSE_TREE_SCHEMA) == (lpt.SUBCOMMAND, lpt.SCHEMA)


@pytest.mark.parametrize("err, audit_note", [
    (lpt.BinaryMissing("da-guard not found"), True),
    (lpt.ParseTreeError("da-guard parse-tree exited 3"), False),
])
def test_migration_audit_turns_the_shared_errors_into_its_own(tmp_path, monkeypatch, err,
                                                              audit_note) -> None:
    def fail(*_a):
        raise err

    monkeypatch.setattr(lpt, "run", fail)
    with pytest.raises(ma.AuditError) as e:
        ma.run_parse_tree(tmp_path, None, 5)
    assert str(e.value).startswith(str(err))
    assert ("does not parse the YAML another way" in str(e.value)) is audit_note


def test_the_image_ships_the_module() -> None:
    build = (ROOT / "components/da-tools/app/build.sh").read_text(encoding="utf-8")
    assert any(line.strip() == "_lib_parse_tree.py" for line in build.splitlines()), (
        "build.sh does not copy _lib_parse_tree.py: migration-audit would ImportError in the image")

