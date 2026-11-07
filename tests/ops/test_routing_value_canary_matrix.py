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
* 每格還要證明那個位置真的被讀了（第 6 輪）：findings 不得為空、要有該鍵角色的
  標記 kind（`_marker_kinds`），且不得出現情境帶不出的 kind（`_allowed_kinds`）。
  沒有判到東西的 run 也沒有 canary 可漏，只驗「沒漏」會把它當成綠。
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
    # Test canaries, not credentials: no real host, user or password.
    return {"receiver_mapping": {"receiver": {"type": "slack",
                                              "api_url": f"https://hooks.example/{canary}"}},
            "url_string": f"https://u:CANARYPW@hooks.example/{canary}",  # trufflehog:ignore
            "key_name": 1}


def _place(where: str, key: str, value: object) -> dict[str, dict]:
    """The conf.d files (name → content) with *value* at *key* of *where*."""
    tenant: dict = {"cpu_usage_percent": "85"}
    files: dict[str, dict] = {"_defaults.yaml": {"defaults": {"cpu_usage_percent": 80}}}
    if where == "tenant_routing":
        block = {"receiver": copy.deepcopy(_SLACK)}
        block[key] = value
        tenant["_routing"] = block
    elif where in ("override_entry", "override_entry_dup"):
        entry = {"alertname": "X", "receiver": copy.deepcopy(_SLACK)}
        if key == "metric_group" and where.endswith("_dup"):
            del entry["alertname"]  # one matcher per entry: the duplicate is on it
        entry[key] = value
        entries = [entry, copy.deepcopy(entry)] if where.endswith("_dup") else [entry]
        tenant["_routing"] = {"receiver": copy.deepcopy(_SLACK), "overrides": entries}
    elif where in ("route_entry", "route_entry_dup"):
        entry = {"match": {"team": "a"}, "receiver": copy.deepcopy(_SLACK)}
        entry[key] = value
        entries = [entry, copy.deepcopy(entry)] if where.endswith("_dup") else [entry]
        tenant["_routing"] = {"receiver": copy.deepcopy(_SLACK), "routes": entries}
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


#: How the matrix writes a value into each routing block of the schemas
#: (round 5, ③): a block key the schemas add without a place here fails
#: `test_every_schema_block_has_a_place` instead of being left untested.
_BLOCK_PLACES = {"_routing": "tenant_routing", "_routing_defaults": "routing_defaults",
                 "_routing_enforced": "routing_enforced", "routing_profiles": "routing_profile"}


def _block_definition(block_key: str) -> str:
    """The routing definition the schemas give *block_key* (the block, not
    its entries: routingOverride / routingRoute are the entries')."""
    for props in (_DEFS["tenantConfig"]["properties"], _PLATFORM["properties"],
                  _PROFILES.get("properties", {})):
        hit = _refs(props.get(block_key, {})) & {"routing", "routingDefaults", "routingEnforced"}
        if hit:
            return sorted(hit)[0]
    raise KeyError(block_key)


_PLACES = {
    **{place: _keys(_block_definition(bk)) for bk, place in _BLOCK_PLACES.items()
       if bk in _schema_block_keys()},
    "override_entry": _keys("routingOverride"),
    "route_entry": _keys("routingRoute"),
    # Round 5, ④: two identical entries — the messages only a repeat triggers.
    "override_entry_dup": _keys("routingOverride"),
    "route_entry_dup": _keys("routingRoute"),
    "tenant": sorted(k for k in _DEFS["tenantConfig"]["properties"]
                     if k.startswith("_routing") and k not in ROUTING_BLOCK_KEYS),
}


def test_every_schema_block_has_a_place():
    """③：矩陣的區塊層位置由 schema 推導；schema 多一個 routing 區塊而這裡沒有寫法 → 紅。"""
    assert set(_BLOCK_PLACES) == _schema_block_keys()
_CASES = [(where, key, kind) for where, keys in sorted(_PLACES.items()) for key in keys
          for kind in ("receiver_mapping", "url_string")]
# Round 5, ② (keys are content too): one unknown key whose NAME is a canary
# URL, in each routing place. A test canary, no real credential.
_KEY_CANARY = "https://u:CANARYPW@hooks.example/CANARY-key-name"  # trufflehog:ignore
_KEY_PLACES = sorted(set(_PLACES) - {"tenant"})
_CASES += [(where, _KEY_CANARY, "key_name") for where in _KEY_PLACES]


#: Round 6, N-2: the finding that proves the run read the value at that
#: place — a run that read nothing (or judged the wrong thing) has no canary
#: to leak either. Keyed by the key's role, not by place; a case whose
#: findings hold none of its marker kinds fails.
_TIMING = ("group_wait", "group_interval", "repeat_interval")
_MATCHERS = ("alertname", "metric_group")
#: the override entry was read (its receiver repeats the block's, or its
#: matchers clash with another entry's).
_OVERRIDE_READ = {"redundant_override_receiver", "duplicate_override_matcher",
                  "conflicting_override_matcher"}


def _marker_kinds(where: str, key: str, kind: str) -> set[str]:
    if kind == "key_name":
        # Route entries refuse a key they do not know; every other place
        # ignores it, and says so.
        return {"invalid_route_entry"} if where.startswith("route_entry") else {"routing_key_ignored"}
    if key in _TIMING:
        return {"timing_value_replaced"}
    if key == "group_by":
        return {"routing_group_by_invalid"}
    if key == "receiver":
        return {"missing_receiver_field"}
    if key == "overrides":
        return {"invalid_override_entry"}
    if key == "routes":
        return {"invalid_route_entry"}
    if key == "enabled":
        return {"routing_enforced_enabled_invalid"}
    if key == "match":
        return {"routing_enforced_match_invalid"} if where == "routing_enforced" else {"invalid_route_entry"}
    if key in _MATCHERS:
        # A URL is a valid matcher string: only the entry having been read
        # shows; a mapping is not a string.
        return {"routing_value_not_string"} if kind == "receiver_mapping" else _OVERRIDE_READ
    if key == "_routing_profile":
        # ⚠️ A string naming no profile is a WARN the findings classify as
        # `unclassified` (no kind of its own yet); a mapping is a type error.
        return {"routing_profile_invalid"} if kind == "receiver_mapping" else {"unclassified"}
    raise KeyError((where, key, kind))


def _allowed_kinds(where: str, key: str, kind: str) -> set[str]:
    """The marker plus what the case's surroundings legitimately add."""
    out = _marker_kinds(where, key, kind) | {"refusal_summary"}
    if where.startswith("override_entry"):
        out |= _OVERRIDE_READ
    if kind == "receiver_mapping" or key in ("receiver", "overrides", "routes"):
        # A receiver the walk does not reach (a mapping where a value goes,
        # a receiver missing its fields, a list that is not one).
        out.add("credential_unjudged")
    if kind == "receiver_mapping" and key == "match":
        out.add("routing_value_not_string")
    return out


def test_the_matrix_covers_every_place_and_key():
    assert ("tenant", "_routing_profile") in {(w, k) for w, k, _ in _CASES}
    for where in ("tenant_routing", "override_entry", "route_entry", "routing_defaults",
                  "routing_profile", "routing_enforced"):
        assert {"group_wait", "receiver", "repeat_interval"} <= set(_PLACES[where]), where


@pytest.mark.parametrize("where,key,kind", _CASES,
                         ids=[f"{w}-{'canary-key' if v == 'key_name' else k}-{v}"
                              for w, k, v in _CASES])
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
    # Round 5, ①: a run that judged nothing must not pass — no da-guard is
    # rc 2, "stopping before any work", with no canary to find.
    assert p.returncode in (0, 1), (p.returncode, p.stderr)
    assert out.exists(), p.stderr
    findings = json.loads(out.read_text(encoding="utf-8"))["findings"]
    kinds = {f["kind"] for f in findings}
    assert "run_failed" not in kinds, findings
    # Round 6, N-2: the place was read — its marker finding is there, and
    # nothing else the case cannot cause.
    assert findings, "no finding: the value at this place was not judged"
    assert kinds & _marker_kinds(where, key, kind), (sorted(kinds), _marker_kinds(where, key, kind))
    assert kinds <= _allowed_kinds(where, key, kind), sorted(kinds - _allowed_kinds(where, key, kind))
    text = p.stdout + p.stderr + out.read_text(encoding="utf-8")
    assert "CANARY" not in text, text
