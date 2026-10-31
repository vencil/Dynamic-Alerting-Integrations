"""Shipped config teaches no dimensional key the rule packs cannot honour (#3475).

The shipped rule packs normalise thresholds with `max by(tenant)(user_threshold{…})`:
the dimension labels are dropped there, so a tenant's `<base>{label="v"}` changes
the whole tenant's threshold (the maximum of the dimension values and the base),
and `disable` on it is a no-op. The one dimension read per label is `version`
(ADR-024). Until a rule pack judges per dimension (#3475), a shipped example of a
dimensional key teaches a behaviour the platform does not have — so the shipped
conf.d trees and the rule-pack headers carry none.

When #3475 makes a pack read a dimension per label, allow that `(base, label)`
here rather than deleting the test.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

import _lib_consumer_read as cr

_ROOT = Path(__file__).resolve().parents[2]

# Every conf.d tree the repo ships to operators or readers.
_SHIPPED_CONFD = (
    "components/threshold-exporter/config/conf.d",
    "try-local/seed/conf.d",
    "rule-packs/recipes/examples/conf.d",
)

# Labels a shipped rule pack reads per value (ADR-024's `version`).
_READ_PER_LABEL = frozenset({"version"})

_DIM_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\{(.*)\}$")
_LABEL = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*=~?")
# A dimensional key written in a rule-pack header comment as a YAML key, quoted or not.
_HEADER_DIM = re.compile(r"""^#.*?["']?([A-Za-z_][A-Za-z0-9_]*)\{([^}]*)\}["']?\s*:""")


def _labels(selector: str) -> set[str]:
    return set(_LABEL.findall(selector))


def _keys(node, path=()):
    if isinstance(node, dict):
        for k, v in node.items():
            yield path + (str(k),)
            yield from _keys(v, path + (str(k),))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _keys(v, path + (str(i),))


def _confd_files() -> list[Path]:
    files = []
    for rel in _SHIPPED_CONFD:
        root = _ROOT / rel
        assert root.is_dir(), f"shipped conf.d tree is gone: {rel}"
        files += sorted(p for p in root.rglob("*") if p.suffix.lower() in (".yaml", ".yml"))
    return files


def _offending(key: str) -> bool:
    m = _DIM_KEY.match(key)
    return bool(m) and not _labels(m.group(2)) <= _READ_PER_LABEL


def test_the_scan_sees_the_shipped_trees():
    """An empty scan is green forever."""
    files = _confd_files()
    assert len(files) >= 5, files
    packs = cr.RULE_PACKS.files_under(_ROOT)
    assert len(packs) >= 10, packs


@pytest.mark.parametrize("line, hit", [
    ('#       "redis_connected_clients{instance=\\"r:6379\\"}": "1000"', True),
    ('#       redis_connected_clients{instance="r:6379"}: 1000', True),
    ('#   tenant_expected_exporter unless on(tenant) (up{job="tenant-exporters"} == 1)', False),
])
def test_the_header_pattern(line, hit):
    assert bool(_HEADER_DIM.match(line)) is hit


@pytest.mark.parametrize("key, offends", [
    ('redis_connected_clients{instance="redis-orders:6379"}', True),
    ("oracle_tablespace_used_percent{tablespace_name=~'SYS.*'}", True),
    ('container_cpu{version="v2"}', False),
    ('container_cpu{version="v2", pod="a"}', True),
    ("container_cpu", False),
])
def test_the_predicate(key, offends):
    assert _offending(key) is offends


def test_shipped_confd_has_no_dimensional_key():
    found = []
    for f in _confd_files():
        for doc in yaml.safe_load_all(f.read_text(encoding="utf-8")):
            for path in _keys(doc):
                if _offending(path[-1]):
                    found.append(f"{f.relative_to(_ROOT)}: {'.'.join(path)}")
    assert not found, (
        "shipped conf.d teaches a dimensional key no shipped rule pack reads per "
        "dimension (max by(tenant) drops the label; #3475):\n" + "\n".join(found))


def test_rule_pack_headers_show_no_dimensional_key():
    found = []
    for f in cr.RULE_PACKS.files_under(_ROOT):
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if not line.startswith("#"):
                break  # the leading comment header only
            m = _HEADER_DIM.match(line)
            if m and not _labels(m.group(2)) <= _READ_PER_LABEL:
                found.append(f"{f.relative_to(_ROOT)}:{n}: {line.strip()}")
    assert not found, (
        "a rule-pack header shows a dimensional key the pack does not read per "
        "dimension (#3475):\n" + "\n".join(found))
