"""Tests for gen_recipe_status_json (ADR-024 §8 A1, #741 #6).

recipe-status.json is DERIVED from shape.py RECIPE_STATUS (the SSOT) and consumed
by the Go tenant-api via go:embed. These pin that the generator covers every
recipe, is deterministic, and the committed artifact matches the SSOT (the same
invariant the CI/pre-commit drift gate enforces).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_DX = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools", "dx")
sys.path.insert(0, _DX)

import gen_recipe_status_json as gen  # noqa: E402
from custom_alerts import shape as shp  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]


def test_render_covers_every_recipe_and_matches_ssot():
    doc = json.loads(gen.render())
    assert set(doc["statuses"]) == set(shp.RECIPES)
    for r in shp.RECIPES:
        assert doc["statuses"][r] == shp.recipe_status(r)


def test_render_is_deterministic_and_key_sorted():
    assert gen.render() == gen.render()
    keys = list(json.loads(gen.render())["statuses"].keys())
    assert keys == sorted(keys)


def test_committed_copies_match_ssot():
    # Both committed copies (Go go:embed + portal import) must equal render() —
    # i.e. `make recipe-status-json` was run after any RECIPE_STATUS change.
    assert len(gen.OUT_RELS) >= 2, "expected a Go + a portal copy"
    for rel in gen.OUT_RELS:
        committed = (_REPO / rel).read_text(encoding="utf-8")
        assert committed == gen.render(), f"{rel}: run `make recipe-status-json` to resync"


def test_committed_copies_are_lf_on_disk():
    # Raw-bytes guard: the portal copy is bundled by esbuild, which reads the
    # working-tree bytes verbatim — a CRLF copy (Path.write_text default on
    # Windows) taints the sourcemap and re-hashes the shared chunk, redding the
    # Portal Tests dist gate. The match-SSOT check above is blind to this because
    # read_text() universal-newlines normalizes CRLF→LF. Pin LF on disk.
    for rel in gen.OUT_RELS:
        raw = (_REPO / rel).read_bytes()
        assert b"\r\n" not in raw, f"{rel}: has CRLF; generator must write newline='\\n'"
