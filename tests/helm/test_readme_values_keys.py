"""test_readme_values_keys.py — chart README parameter tables (#2044)

A chart README's parameter table ("常用覆寫" / "Key values") is what an
operator copies into a values file. A key it lists that the chart's
values.yaml does not declare is a silent no-op: Helm accepts any key, so the
override is dropped without a word. threshold-exporter's README listed three
such keys (`config.directory`, `podDisruptionBudget.enabled`, and a
`rules.mode: disabled` the templates never branch on) — the same class as
#2027.

This pins what a table CAN be checked for mechanically: every key named in
the first column of a parameter table exists in that chart's values.yaml, and
every default the table states equals the value values.yaml sets (#2473:
federation-gateway documented `auditLog.volumeSizeLimit` as `256Mi` while
values.yaml sets a deliberate `1Gi`, and nothing noticed). Whether a template
reads a key is a separate question; values.yaml declaring a key the templates
ignore is its own bug class.

Defaults are compared as `yaml.safe_load(cell token) == yaml.safe_load(values)`
so `1048576` / `true` / `""` / `[]` mean what they mean in a values file. A
token that is not valid YAML on its own (`*`) is compared as a string. ⚠️ A
YAML 1.1 boolean spelled the same on both sides (`off`) is misread identically
by both and cannot be caught by equality.

A default cell that is not a plain value (prose, a subtree summary, "see
values.yaml") must be listed in `_UNCOMPARABLE_DEFAULTS` with a reason. An
unlisted one fails rather than being skipped: a silent skip would let every
new prose default drift unguarded.

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


# ── Defaults ─────────────────────────────────────────────────────────────────

_DEFAULT_HEADERS = {"Default", "預設"}
# The whole cell is one or more `token`s joined by " / ", optionally followed
# by a parenthesised annotation: `""`（＝`v<Chart appVersion>`）, `60` / `10`（…）.
_ANNOTATION = re.compile(r"(?<=`)\s*[（(].*[)）]$")
_ONLY_TOKENS = re.compile(r"`[^`]+`(\s*/\s*`[^`]+`)*")
_BARE_VALUE = re.compile(r"[^\s`|]+")  # da-portal writes defaults without backticks

# (chart, first cell verbatim) -> why its default cell cannot be compared.
# Keyed on the cell, not the line number, so reflowing a README does not break it.
_UNCOMPARABLE_DEFAULTS = {
    ("federation-gateway", "`victorialogs.allowedEndpoints`"):
        "prose summary of an endpoint list",
    ("federation-gateway", "`preflight.image.*`"):
        "subtree shown as repository:tag plus a digest note",
    ("federation-gateway", "`rateLimit.perToken.*` / `perTenant.*` / `perIp.*`"):
        "defers to values.yaml, states no value",
}


def _default_rows(readme: Path):
    """Yield (line_no, first_cell, default_cell) for tables that have a default column."""
    lines = readme.read_text(encoding="utf-8").splitlines()
    col = None
    for i, line in enumerate(lines, 1):
        if not line.startswith("|"):
            col = None
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if col is None:
            if cells and cells[0] in _HEADER_FIRST_CELLS:
                col = next((j for j, c in enumerate(cells) if c in _DEFAULT_HEADERS), -1)
            continue
        if col < 0 or set(cells[0]) <= {"-", ":", " "}:
            continue
        yield i, cells[0], cells[col]


def _default_tokens(cell: str) -> list[str] | None:
    """The plain values a default cell states, or None if it is not plain values."""
    body = _ANNOTATION.sub("", cell)
    if _ONLY_TOKENS.fullmatch(body):
        return _TOKEN.findall(body)
    if _BARE_VALUE.fullmatch(body):
        return [body]
    return None


def _as_yaml(token: str):
    try:
        return yaml.safe_load(token)
    except yaml.YAMLError:
        return token


def _default_cases():
    for readme in sorted(_REPO.glob("helm/*/README.md")):
        values_file = readme.parent / "values.yaml"
        if not values_file.exists():
            continue
        for line_no, key_cell, default_cell in _default_rows(readme):
            yield pytest.param(readme, values_file, line_no, key_cell, default_cell,
                               id=f"{readme.parent.name}:L{line_no}")


_DEFAULT_CASES = list(_default_cases())


def _compare(values: dict, key_cell: str, default_cell: str):
    """Return (compared pairs, mismatches) or None when the row is not comparable."""
    tokens = _TOKEN.findall(key_cell) or [key_cell]
    defaults = _default_tokens(default_cell)
    if defaults is None or len(defaults) != len(tokens) or any(t.endswith(".*") for t in tokens):
        return None
    prev = None
    compared, bad = 0, []
    for tok, dflt in zip(tokens, defaults):
        key, ok = _resolve(values, tok, prev)
        prev = key
        if not ok:
            continue  # test_every_listed_key_exists_in_values reports this
        _, actual = _lookup(values, key)
        compared += 1
        if _as_yaml(dflt) != actual:
            bad.append(f"{key}: README says {dflt!r}, values.yaml has {actual!r}")
    return compared, bad


def test_the_default_scan_compares_every_chart():
    # Vacuity guard: a header rename or an over-broad exemption would turn the
    # comparison below into zero real assertions per chart and a green run.
    compared = {}
    for c in _DEFAULT_CASES:
        readme, values_file, _, key_cell, default_cell = c.values
        values = yaml.safe_load(values_file.read_text(encoding="utf-8")) or {}
        res = _compare(values, key_cell, default_cell)
        if res:
            compared[readme.parent.name] = compared.get(readme.parent.name, 0) + res[0]
    assert {"threshold-exporter", "federation-gateway", "federation-proxy", "da-portal"} <= {
        k for k, n in compared.items() if n > 0
    }, compared


def test_every_uncomparable_exemption_is_still_used():
    live = {(c.values[0].parent.name, c.values[3]) for c in _DEFAULT_CASES}
    stale = set(_UNCOMPARABLE_DEFAULTS) - live
    assert not stale, f"_UNCOMPARABLE_DEFAULTS names rows that no longer exist: {sorted(stale)}"


@pytest.mark.parametrize("readme, values_file, line_no, key_cell, default_cell", _DEFAULT_CASES)
def test_every_listed_default_matches_values(readme, values_file, line_no, key_cell, default_cell):
    where = f"{readme.relative_to(_REPO)}:{line_no}"
    values = yaml.safe_load(values_file.read_text(encoding="utf-8")) or {}
    res = _compare(values, key_cell, default_cell)
    exempt = (readme.parent.name, key_cell) in _UNCOMPARABLE_DEFAULTS
    if res is None:
        assert exempt, (
            f"{where}: default {default_cell!r} for {key_cell} is not a plain value, so it "
            f"cannot be compared with values.yaml. State the value as `token`(s), or add the "
            f"row to _UNCOMPARABLE_DEFAULTS with the reason."
        )
        return
    assert not exempt, (
        f"{where}: {key_cell} is in _UNCOMPARABLE_DEFAULTS but its default is now "
        f"comparable — drop the exemption"
    )
    _, bad = res
    assert not bad, f"{where}: documented default drifted from values.yaml — {bad}"
