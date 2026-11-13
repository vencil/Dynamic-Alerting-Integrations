"""#3689：``--validate`` 與 ``--output-alertmanager`` 對「Alertmanager 收不收」
給同一個結論，且兩條路都不把租戶寫的值印到終端。

兩條路曾經問的不是同一個讀者：``--validate`` 對 ``--output-*`` 拒收的
receiver 值印 OK、rc 0。量到 94 個 receiver 值、33 個欄位位置，其中 15 個型別正確、是 Alertmanager 的跨欄位規則
（``oauth2`` 沒有 ``client_id``、``tls_config.cert`` 沒有 key……）。

案例**由 schema 推導**，不是手寫清單：``definitions.receiver.oneOf`` 的每一種
receiver、其底下每一個葉節點（``$ref`` / ``oneOf`` / ``anyOf`` / ``allOf``、
``additionalProperties``、``items`` 都展開），各寫入一個 schema 不允許的型別
（字串、清單、mapping、整數）。schema 加一個欄位，這裡就多幾個案例；加一種
receiver 而沒有給它一份合法的起點，``test_every_receiver_type_has_a_base`` 紅。
三個跨欄位規則另外明列（型別都對，只有 Alertmanager 的規則拒收），並斷言兩條路
都走到 Alertmanager、都 rc 1——它們不能因為被別的檢查先擋下而空過。

每個案例斷言：

* ``--validate`` 的 rc == ``--output-alertmanager`` 的 rc——只比 ``--validate``
  沒有因自己獨有的檢查擋下的案例（其 findings 裡沒有 ``blocks == "validate"``
  的紀錄：那些是產生器略過一個不能用的項目，``--validate`` 有意比寫檔嚴格）；
* 金絲雀值不出現在兩條路的 stdout / stderr——產生器自己逐項列出的 finding
  （``--findings-json`` 裡那一筆的 message）除外：那些訊息顯示非憑證欄位的值、
  遮罩憑證欄位，是 #1560 ⑤-b 的既定規則（``_lib_validation.MASKED_VALUE``），
  由它自己的測試釘住。Alertmanager 那道閘門（da-read）的訊息不是
  finding，一律不得帶值。

``PATH`` 指向 ``os.devnull``（與 ``_grar_findings_cases.run_gar`` 同）：PATH
上裝了什麼都不影響結論。產生器在行程內跑
（數百次子行程太慢）。
"""
from __future__ import annotations

import copy
import importlib
import json
import os
import sys
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_SCHEMA = json.loads((_REPO / "docs" / "schemas" / "tenant-config.schema.json")
                     .read_text(encoding="utf-8"))
_DEFS = _SCHEMA["definitions"]

pytestmark = pytest.mark.usefixtures("da_guard_env", "da_crdecode_env", "da_read_env")

#: yaml.v2 quotes a value of more than 10 bytes by its first 7, so the
#: prefix is what a leak would show.
CANARY = "CANARYVAL12345"
_LEAK = CANARY[:7]
_WRONG = {"string": CANARY, "array": [CANARY], "object": {"k": CANARY}, "integer": 7}

#: One receiver of each type that the generator and Alertmanager both accept:
#: the starting point each wrong value is written into.
_BASES = {
    "webhook": {"type": "webhook", "url": "https://hooks.example.com/a"},
    "email": {"type": "email", "to": ["a@example.com"],
              "smarthost": "smtp.example.com:587", "from": "b@example.com"},
    "slack": {"type": "slack", "api_url": "https://hooks.slack.com/x"},
    "teams": {"type": "teams", "webhook_url": "https://example.webhook.office.com/x"},
    "rocketchat": {"type": "rocketchat", "url": "https://chat.example.com/hooks/x"},
    "pagerduty": {"type": "pagerduty", "routing_key": "abc"},
}

#: Correct types, refused by Alertmanager's own cross-field rules.
_CROSS_FIELD = {
    "oauth2-without-client_id": {"oauth2": {"token_url": "https://t.example.com/x"}},
    "tls-cert-without-key": {"tls_config": {"cert": "x"}},
    "oauth2-no_proxy-without-proxy_url": {"oauth2": {
        "client_id": "a", "token_url": "https://t.example.com/x",
        "no_proxy": "localhost"}},
}


def _resolve(node: dict) -> dict:
    if "$ref" in node:
        target = _DEFS[node["$ref"].rsplit("/", 1)[-1]]
        return {**target, **{k: v for k, v in node.items() if k != "$ref"}}
    return node


def _leaves(node: dict, path: tuple, out: dict, depth: int = 0) -> None:
    """Every path under *node* with the JSON types the schema allows there."""
    if depth > 14:
        return
    node = _resolve(dict(node))
    for key in ("oneOf", "anyOf", "allOf"):
        for alt in node.get(key, []):
            _leaves(alt, path, out, depth + 1)
    t = node.get("type")
    out.setdefault(path, set()).update(t if isinstance(t, list) else [t] if t else [])
    for name, sub in node.get("properties", {}).items():
        _leaves(sub, path + (name,), out, depth + 1)
    extra = node.get("additionalProperties")
    if isinstance(extra, dict):
        _leaves(extra, path + ("H",), out, depth + 1)  # H: a sample map key
    if isinstance(node.get("items"), dict):
        _leaves(node["items"], path + (0,), out, depth + 1)


def _receiver_types() -> dict[str, dict]:
    """type const → its definition, from ``definitions.receiver.oneOf``."""
    out = {}
    for alt in _DEFS["receiver"]["oneOf"]:
        d = _resolve(alt)
        out[d["properties"]["type"]["const"]] = d
    return out


def _set(obj, path: tuple, value) -> None:
    cur = obj
    for i, seg in enumerate(path[:-1]):
        nxt = path[i + 1]
        if isinstance(cur, dict):
            if not isinstance(cur.get(seg), (dict, list)):
                cur[seg] = [] if isinstance(nxt, int) else {}
        else:
            while len(cur) <= seg:
                cur.append([] if isinstance(nxt, int) else {})
        cur = cur[seg]
    last = path[-1]
    if isinstance(cur, list):
        while len(cur) <= last:
            cur.append(None)
    cur[last] = value


def _cases() -> list[tuple[str, dict, bool]]:
    """(id, receiver, whether Alertmanager must be the one refusing it)."""
    cases = []
    for rtype, definition in sorted(_receiver_types().items()):
        if rtype not in _BASES:
            continue  # test_every_receiver_type_has_a_base names it
        paths: dict = {}
        _leaves(definition, (), paths)
        for path, types in sorted(paths.items(), key=lambda kv: str(kv[0])):
            # `type` picks the receiver; a Secret reference is judged by the
            # credential rules (#1560), not by Alertmanager's loader.
            if not path or path[-1] == "type" or "secret" in path or not types:
                continue
            for wname, wval in _WRONG.items():
                if wname in types or (wname == "integer" and "number" in types):
                    continue
                receiver = copy.deepcopy(_BASES[rtype])
                _set(receiver, path, copy.deepcopy(wval))
                cases.append((f"{rtype}.{'.'.join(map(str, path))}={wname}",
                              receiver, False))
    for name, http_config in _CROSS_FIELD.items():
        cases.append((f"cross-field:{name}",
                      {**_BASES["webhook"], "http_config": http_config}, True))
    return cases


_CASES = _cases()


def test_every_receiver_type_has_a_base():
    assert sorted(_receiver_types()) == sorted(_BASES)


def _run(gar, monkeypatch, capsys, conf_d: Path, argv: list[str], findings: Path):
    monkeypatch.setattr(sys, "argv", ["generate_alertmanager_routes.py", "--config-dir",
                                      str(conf_d), *argv, "--findings-json", str(findings)])
    try:
        gar.main()
        rc = 0
    except SystemExit as exc:
        rc = 0 if exc.code is None else exc.code
    cap = capsys.readouterr()
    return rc, cap.out + cap.err, json.loads(findings.read_text(encoding="utf-8"))


@pytest.mark.parametrize("receiver,am_refuses", [c[1:] for c in _CASES],
                         ids=[c[0] for c in _CASES])
def test_validate_and_output_agree_and_never_echo_the_value(
        receiver, am_refuses, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PATH", os.devnull)  # nothing on PATH decides the verdict
    gar = importlib.import_module("generate_alertmanager_routes")
    conf_d = tmp_path / "conf.d"
    conf_d.mkdir()
    (conf_d / "_defaults.yaml").write_text("defaults:\n  mysql_connection_usage_percent: 80\n",
                                           encoding="utf-8")
    (conf_d / "t1.yaml").write_text(yaml.safe_dump(
        {"tenants": {"t1": {"_routing": {"receiver": receiver},
                            "mysql_connection_usage_percent": "70"}}}), encoding="utf-8")

    v_rc, v_text, v_doc = _run(gar, monkeypatch, capsys, conf_d, ["--validate"],
                               tmp_path / "v.json")
    o_rc, o_text, o_doc = _run(gar, monkeypatch, capsys, conf_d,
                               ["--output-alertmanager", "-o", str(tmp_path / "am.yml")],
                               tmp_path / "o.json")

    itemised = {r["message"].strip() for doc in (v_doc, o_doc) for r in doc["findings"]}
    for text in (v_text, o_text):
        leaks = [ln for ln in text.splitlines()
                 if _LEAK in ln and ln.strip() not in itemised]
        assert leaks == [], text
    validate_only = any(r["blocks"] == "validate" for r in v_doc["findings"])
    if am_refuses:
        assert not validate_only and o_rc == 1, (v_rc, o_rc, v_text, o_text)
    if validate_only:
        return  # --validate's own stricter check; Alertmanager was not asked
    assert v_rc == o_rc, f"--validate rc {v_rc}, --output rc {o_rc}\n{v_text}\n---\n{o_text}"
