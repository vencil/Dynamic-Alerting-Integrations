"""#2341 R5：schema 裡 `_routing` 的停用字串 pattern 與 `_lib_validation.is_disabled` 同一集合。

`docs/schemas/tenant-config.schema.json` 的租戶 `_routing` 是 `oneOf: [routing
mapping, 停用字串]`，後者的 pattern 手寫成大小寫不敏感的四個字。這裡逐值比對
pattern（jsonschema 的 `pattern` 是 re.search 語意）與 is_disabled 的判定：刪掉任
一段、少了前後空白或錨點，都會在下面某一格轉紅。
"""
from __future__ import annotations

import itertools
import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "tools"))
from _lib_validation import is_disabled  # noqa: E402

_SCHEMA = json.loads((REPO / "docs" / "schemas" / "tenant-config.schema.json")
                     .read_text(encoding="utf-8"))


def _routing_disable_patterns() -> list[str]:
    """Every string-branch pattern of a `_routing` property with a oneOf."""
    out = []
    stack = [_SCHEMA]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            r = node.get("_routing")
            if isinstance(r, dict) and isinstance(r.get("oneOf"), list):
                out += [b["pattern"] for b in r["oneOf"]
                        if isinstance(b, dict) and b.get("type") == "string"]
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return out


def _cases(word: str):
    """A handful of letter-case spellings of *word*."""
    yield word
    yield word.upper()
    yield word.capitalize()
    yield "".join(c.upper() if i % 2 else c for i, c in enumerate(word))


_WORDS = ("disable", "disabled", "off", "false")
_POSITIVE = sorted({pad_l + w + pad_r
                    for word in _WORDS for w in _cases(word)
                    for pad_l, pad_r in itertools.product(("", " ", "\t"), ("", " ", "  "))})
_NEGATIVE = ["", " ", "disabled-x", "xdisable", "FALSEy", "of", "offf", "no", "on",
             "enable", "true", "disab le", "dis able", "off off", "'off'", "fals",
             "disables", "off\nx", "x\noff", "0"]


def test_there_is_exactly_one_pattern():
    assert len(_routing_disable_patterns()) == 1


@pytest.mark.parametrize("value", _POSITIVE + _NEGATIVE)
def test_pattern_accepts_exactly_what_is_disabled_accepts(value):
    (pattern,) = _routing_disable_patterns()
    assert bool(re.search(pattern, value)) == is_disabled(value), value


def test_the_corpus_has_both_verdicts():
    assert all(is_disabled(v) for v in _POSITIVE)
    assert not any(is_disabled(v) for v in _NEGATIVE)
