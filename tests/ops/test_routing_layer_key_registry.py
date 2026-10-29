"""產生器的路由層鍵判斷涵蓋登錄表宣告為產生器讀的每個鍵（ADR-038 決定 2）。

`_grar_parse._is_routing_layer_key` 是手寫的（Go 的 `config.IsRoutingLayerKey`
是它的雙胞胎，由 `registered_readers_test.go` 依同一條規則守住），不由登錄表產生。
登錄表（`reserved_keys.json`，經 `sync_schema.py` 產生為 `_lib_constants`）說產生器
讀的鍵，它都要認得；漏了一個，產生器就不會把寫在 profile 或子目錄 defaults 裡的那個
鍵報成 `routing_in_unread_location`，da-guard 的 exit 4 也一樣漏掉它。
"""
from __future__ import annotations

import pytest

import _grar_parse
from _lib_constants import RESERVED_KEY_READERS, RESERVED_PREFIX_READERS

GENERATOR_KEYS = sorted(k for k, readers in RESERVED_KEY_READERS.items() if "generator" in readers)
GENERATOR_PREFIXES = sorted(p for p, readers in RESERVED_PREFIX_READERS if "generator" in readers)


def test_the_registry_declares_generator_keys():
    """Non-vacuous: the parametrised test below has rows."""
    assert GENERATOR_KEYS, RESERVED_KEY_READERS


@pytest.mark.parametrize("key", GENERATOR_KEYS + [p + "zz_probe" for p in GENERATOR_PREFIXES])
def test_every_key_the_generator_reads_is_a_routing_layer_key(key):
    assert _grar_parse._is_routing_layer_key(key), (
        f"the registry says the route generator reads {key!r}: extend "
        "_grar_parse._is_routing_layer_key and Go's config.IsRoutingLayerKey")
