"""#1560 ⑤-b（第 4 輪，owner 裁決）：conf.d 的值不進訊息——動態 canary 矩陣。

這是「值不進訊息」的**主守衛**。`tests/shared/test_no_value_repr_in_messages.py`
的 AST 掃描只攔 repr 類寫法（`!r`、`repr(`、`%r`），抓不到 f-string 直接插值，
記為死路；這裡改成照行為測：

* routing 相關的鍵由 schema 推導，不手寫鍵名：`tenant-config.schema.json` 的
  `routing`／`routingOverride`／`routingRoute`／`routingEnforced`／`routingDefaults`
  定義的 properties，以及 `tenantConfig._routing_profile`；routing 區塊的頂層鍵
  （`_routing`、`_routing_defaults`、`_routing_enforced`、`routing_profiles`）由
  三份 schema 裡引用這些定義的 property 推導，並與 `_grar_parse.ROUTING_BLOCK_KEYS`
  釘在一起。
* 每個位置（租戶 `_routing`、overrides 項目、routes 項目、`_routing_defaults`、
  profile、根層 `_routing_enforced`）的每個鍵，各塞兩種值：一個裝著帶 canary
  的 receiver 的 mapping，與一個帶 userinfo 的 canary URL 字串。
* 跑 `--validate --strict --findings-json`：stdout、stderr、findings 都不得出現
  canary。PATH 裡不放 amtool：amtool 的原文轉印是另一條通道（另開票）。
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.usefixtures("judging_run")

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "tools"))
sys.path.insert(0, str(REPO / "scripts" / "tools" / "ops"))
from _grar_parse import ROUTING_BLOCK_KEYS  # noqa: E402

_GAR = REPO / "scripts" / "tools" / "ops" / "generate_alertmanager_routes.py"
_SCHEMAS = REPO / "docs" / "schemas"
_TENANT = json.loads((_SCHEMAS / "tenant-config.schema.json").read_text(encoding="utf-8"))
_PLATFORM = json.loads((_SCHEMAS / "platform-defaults.schema.json").read_text(encoding="utf-8"))
_PROFILES = json.loads((_SCHEMAS / "routing-profiles.schema.json").read_text(encoding="utf-8"))
_DEFS = _TENANT["definitions"]
#: the tenant-config definitions of a routing block (and its entries).
_ROUTING_DEFS = ("routing", "routingOverride", "routingRoute", "routingEnforced",
                 "routingDefaults")


def _keys(name: str) -> list[str]:
    return sorted(_DEFS[name]["properties"])


def _refs(node: object) -> set[str]:
    """Every `$ref` target's definition name anywhere in *node*."""
    out: set[str] = set()
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            out.add(ref.rsplit("/", 1)[-1])
        for v in node.values():
            out |= _refs(v)
    elif isinstance(node, list):
        for v in node:
            out |= _refs(v)
    return out


def _schema_block_keys() -> set[str]:
    """The property names of the three schemas whose value references a
    routing definition: the routing blocks."""
    return {k for props in (_DEFS["tenantConfig"]["properties"], _PLATFORM["properties"],
                            _PROFILES.get("properties", {}))
            for k, v in props.items() if _refs(v) & set(_ROUTING_DEFS)}


def test_the_routing_block_keys_are_the_schemas():
    """`ROUTING_BLOCK_KEYS`（深掃的範圍）就是 schema 裡引用 routing 定義的那些鍵。"""
    assert _schema_block_keys() == set(ROUTING_BLOCK_KEYS)


_SLACK = {"type": "slack", "api_url": {"secret": {"key": "slack-url"}}}


def _canary_values(tag: str) -> dict[str, object]:
    canary = f"CANARY-{tag}"
    return {"receiver_mapping": {"receiver": {"type": "slack",
                                              "api_url": f"https://hooks.example/{canary}"}},
            "url_string": f"https://u:CANARYPW@hooks.example/{canary}"}


def _place(where: str, key: str, value: object) -> dict[str, dict]:
    """The conf.d files (name → content) with *value* at *key* of *where*."""
    tenant: dict = {"cpu_usage_percent": "85"}
    files: dict[str, dict] = {"_defaults.yaml": {"defaults": {"cpu_usage_percent": 80}}}
    if where == "tenant_routing":
        block = {"receiver": copy.deepcopy(_SLACK)}
        block[key] = value
        tenant["_routing"] = block
    elif where == "override_entry":
        entry = {"alertname": "X", "receiver": copy.deepcopy(_SLACK)}
        entry[key] = value
        tenant["_routing"] = {"receiver": copy.deepcopy(_SLACK), "overrides": [entry]}
    elif where == "route_entry":
        entry = {"match": {"team": "a"}, "receiver": copy.deepcopy(_SLACK)}
        entry[key] = value
        tenant["_routing"] = {"receiver": copy.deepcopy(_SLACK), "routes": [entry]}
    elif where == "routing_defaults":
        block = {"receiver": copy.deepcopy(_SLACK)}
        block[key] = value
        files["_defaults.yaml"]["_routing_defaults"] = block
    elif where == "routing_profile":
        block = {"receiver": copy.deepcopy(_SLACK)}
        block[key] = value
        files["_routing_profiles.yaml"] = {"routing_profiles": {"p": block}}
        tenant["_routing_profile"] = "p"
    elif where == "routing_enforced":
        block = {"enabled": True, "receiver": {"type": "webhook",
                                               "url": "https://noc.example.com/a"}}
        block[key] = value
        files["_defaults.yaml"]["_routing_enforced"] = block
    elif where == "tenant":
        tenant[key] = value
    files["t-a.yaml"] = {"tenants": {"t-a": tenant}}
    return files


_PLACES = {
    "tenant_routing": _keys("routing"),
    "override_entry": _keys("routingOverride"),
    "route_entry": _keys("routingRoute"),
    "routing_defaults": _keys("routingDefaults"),
    "routing_profile": _keys("routing"),
    "routing_enforced": _keys("routingEnforced"),
    "tenant": sorted(k for k in _DEFS["tenantConfig"]["properties"]
                     if k.startswith("_routing") and k not in ROUTING_BLOCK_KEYS),
}
_CASES = [(where, key, kind) for where, keys in sorted(_PLACES.items()) for key in keys
          for kind in ("receiver_mapping", "url_string")]


def test_the_matrix_covers_every_place_and_key():
    assert ("tenant", "_routing_profile") in {(w, k) for w, k, _ in _CASES}
    for where in ("tenant_routing", "override_entry", "route_entry", "routing_defaults",
                  "routing_profile", "routing_enforced"):
        assert {"group_wait", "receiver", "repeat_interval"} <= set(_PLACES[where]), where


@pytest.mark.parametrize("where,key,kind", _CASES,
                         ids=[f"{w}-{k}-{v}" for w, k, v in _CASES])
def test_no_routing_value_reaches_a_message(tmp_path, where, key, kind):
    tag = f"{where}-{key}-{kind}"
    value = _canary_values(tag)[kind]
    d = tmp_path / "conf.d"
    for name, body in _place(where, key, value).items():
        (d / name).parent.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
    empty = tmp_path / "bin"
    empty.mkdir()
    out = tmp_path / "f.json"
    p = subprocess.run([sys.executable, str(_GAR), "--config-dir", str(d), "--validate",
                        "--strict", "--findings-json", str(out)],
                       capture_output=True, text=True, encoding="utf-8", timeout=300,
                       env={**os.environ, "PATH": str(empty)})
    assert "Traceback" not in p.stderr, p.stderr
    text = p.stdout + p.stderr + (out.read_text(encoding="utf-8") if out.exists() else "")
    assert "CANARY" not in text, text
