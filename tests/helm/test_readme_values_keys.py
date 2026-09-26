"""test_readme_values_keys.py — chart README parameter tables (#2044)

A chart README's parameter table ("常用覆寫" / "Key values") is what an
operator copies into a values file. A key it lists that the chart's
values.yaml does not declare is a silent no-op: Helm accepts any key, so the
override is dropped without a word. threshold-exporter's README listed three
such keys (`config.directory`, `podDisruptionBudget.enabled`, and a
`rules.mode: disabled` the templates never branch on) — the same class as
#2027.

This pins the half a table CAN be checked for mechanically: every key named
in the first column of a parameter table exists in that chart's values.yaml.
(Whether a template reads it is a separate question; values.yaml declaring a
key the templates ignore is its own bug class.)

Accepted first-cell shapes, all seen in this repo's READMEs:
  `a.b`                     one key
  `a.b` / `a.c`             several keys
  `a.b.*`                   a subtree — `a.b` must exist and be a mapping
  `a.b.c` / `.d`            a sibling of the previous key (`a.b.d`)
  `a.b.*` / `c.*`           a sibling subtree of the previous key (`a.c`)
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parent.parent.parent
_HEADER_FIRST_CELLS = {"參數", "Key", "Parameter"}
_TOKEN = re.compile(r"`([^`]+)`")
_BARE_KEY = re.compile(r"[A-Za-z_][\w.]*(\.\*)?")  # da-portal writes keys without backticks


def _param_tables(readme: Path):
    """Yield (line_no, first_cell) for each row of each parameter table."""
    lines = readme.read_text(encoding="utf-8").splitlines()
    in_table = False
    for i, line in enumerate(lines, 1):
        if not line.startswith("|"):
            in_table = False
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not in_table:
            if cells and cells[0] in _HEADER_FIRST_CELLS:
                in_table = True
            continue
        if set(cells[0]) <= {"-", ":", " "}:
            continue  # separator row
        yield i, cells[0]


def _lookup(values: dict, dotted: str):
    node = values
    for seg in dotted.split("."):
        if not isinstance(node, dict) or seg not in node:
            return False, None
        node = node[seg]
    return True, node


def _resolve(values: dict, token: str, prev: str | None) -> tuple[str, bool]:
    """Return (the absolute key the token names, whether values.yaml has it)."""
    wildcard = token.endswith(".*")
    key = token[:-2] if wildcard else token
    candidates = [key]
    if prev:
        parent = prev.rsplit(".", 1)[0] if "." in prev else ""
        if key.startswith("."):
            candidates = [parent + key if parent else key[1:]]
        elif parent and not _lookup(values, key)[0]:
            candidates.append(f"{parent.rsplit('.', 1)[0]}.{key}" if "." in parent else key)
            candidates.append(f"{parent}.{key}")
    for cand in candidates:
        found, node = _lookup(values, cand)
        if found and (not wildcard or isinstance(node, dict)):
            return cand, True
    return candidates[0], False


def _cases():
    for readme in sorted(_REPO.glob("helm/*/README.md")):
        values_file = readme.parent / "values.yaml"
        if not values_file.exists():
            continue
        for line_no, cell in _param_tables(readme):
            yield pytest.param(readme, values_file, line_no, cell,
                               id=f"{readme.parent.name}:L{line_no}")


_CASES = list(_cases())


def test_the_scan_finds_the_tables():
    # Vacuity guard: a parser that stops matching the table header would turn
    # every assertion below into zero parametrized cases and a green run.
    charts = {c.values[0].parent.name for c in _CASES}
    assert {"threshold-exporter", "federation-gateway", "federation-proxy", "da-portal"} <= charts, charts


@pytest.mark.parametrize("readme, values_file, line_no, cell", _CASES)
def test_every_listed_key_exists_in_values(readme, values_file, line_no, cell):
    values = yaml.safe_load(values_file.read_text(encoding="utf-8")) or {}
    tokens = _TOKEN.findall(cell) or ([cell] if _BARE_KEY.fullmatch(cell) else [])
    assert tokens, f"{readme.relative_to(_REPO)}:{line_no}: no `key` in first cell {cell!r}"
    prev = None
    missing = []
    for tok in tokens:
        key, ok = _resolve(values, tok, prev)
        if not ok:
            missing.append(tok)
        prev = key
    assert not missing, (
        f"{readme.relative_to(_REPO)}:{line_no} lists {missing} but "
        f"{values_file.relative_to(_REPO)} declares no such key — copying it is a silent no-op"
    )
