"""Tests for gen_am_duration_json (#2711).

am-duration.json is DERIVED from tenant-config.schema.json definitions.duration
(#2490) and imported by the portal's parseDuration. These pin that the
generator copies the rule verbatim, refuses a schema without a usable
definition, and that the committed copy matches (the invariant the
pre-commit / CI drift gate enforces).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_DX = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools", "dx")
sys.path.insert(0, _DX)

import gen_am_duration_json as gen  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]


def _schema() -> dict:
    return json.loads((_REPO / gen.SCHEMA_REL).read_text(encoding="utf-8"))


def test_render_copies_pattern_min_length_and_description():
    doc = json.loads(gen.render(_schema()))
    definition = _schema()["definitions"]["duration"]
    assert doc["pattern"] == definition["pattern"]
    assert doc["minLength"] == definition["minLength"]
    assert doc["description"] == definition["description"]
    assert "DO NOT EDIT" in doc["_generated"]


def test_render_is_deterministic():
    assert gen.render(_schema()) == gen.render(_schema())


@pytest.mark.parametrize("definitions", [
    {},
    {"duration": "not-an-object"},
    {"duration": {"pattern": "^a$", "minLength": 1}},
    {"duration": {"pattern": "", "minLength": 1, "description": "d"}},
    {"duration": {"pattern": "^a$", "minLength": 1, "description": 1}},
    {"duration": {"pattern": "^a$", "description": "d"}},
    {"duration": {"pattern": "^a$", "minLength": "1", "description": "d"}},
    {"duration": {"pattern": "^a$", "minLength": True, "description": "d"}},
])
def test_render_refuses_an_unusable_definition(definitions):
    with pytest.raises(ValueError, match="definitions.duration"):
        gen.render({"definitions": definitions})


def test_committed_copy_matches_schema():
    assert gen.OUT_RELS, "expected a portal copy"
    want = gen.render(_schema())
    for rel in gen.OUT_RELS:
        committed = (_REPO / rel).read_text(encoding="utf-8")
        assert committed == want, f"{rel}: run `make am-duration-json` to resync"


def test_committed_copy_is_lf_on_disk():
    # read_text() normalizes CRLF, so the match check above is blind to it;
    # the portal bundles the working-tree bytes.
    for rel in gen.OUT_RELS:
        assert b"\r\n" not in (_REPO / rel).read_bytes(), f"{rel}: has CRLF"


def test_check_reports_drift(monkeypatch, tmp_path, capsys):
    schema = _schema()
    (tmp_path / "docs" / "schemas").mkdir(parents=True)
    (tmp_path / gen.SCHEMA_REL).write_text(json.dumps(schema), encoding="utf-8")
    monkeypatch.setattr(gen, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["gen_am_duration_json.py", "--check"])
    assert gen.main() == 1  # copy missing

    monkeypatch.setattr(sys, "argv", ["gen_am_duration_json.py"])
    assert gen.main() == 0
    monkeypatch.setattr(sys, "argv", ["gen_am_duration_json.py", "--check"])
    assert gen.main() == 0

    schema["definitions"]["duration"]["pattern"] = "^[0-9]+s$"
    (tmp_path / gen.SCHEMA_REL).write_text(json.dumps(schema), encoding="utf-8")
    assert gen.main() == 1
    assert "stale" in capsys.readouterr().err


def test_check_reports_a_crlf_copy(monkeypatch, tmp_path, capsys):
    (tmp_path / "docs" / "schemas").mkdir(parents=True)
    (tmp_path / gen.SCHEMA_REL).write_text(json.dumps(_schema()), encoding="utf-8")
    monkeypatch.setattr(gen, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["gen_am_duration_json.py"])
    assert gen.main() == 0
    copy = tmp_path / gen.OUT_RELS[0]
    copy.write_bytes(copy.read_bytes().replace(b"\n", b"\r\n"))
    monkeypatch.setattr(sys, "argv", ["gen_am_duration_json.py", "--check"])
    assert gen.main() == 1
    assert "stale" in capsys.readouterr().err


def test_unusable_schema_is_a_caller_error(monkeypatch, tmp_path):
    monkeypatch.setattr(gen, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["gen_am_duration_json.py", "--check"])
    assert gen.main() == 2  # schema file missing
