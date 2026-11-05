"""`definitions.secretRef` 的形狀與接線（#1560 ③-a，owner 裁決 2026-11-04）。

secretRef 是 Kubernetes Secret 的參照，兩種形狀：

- `{secret: {key}}`：租戶自己的 Secret，名稱由租戶 id 推導（`da-<tenant>`），不收 `name`；
- `{secret: {shared, key}}`：部署端 policy `shared_secrets` 列出的共用 Secret 別名。

key 依 Secret data key 規則（Kubernetes IsConfigMapKey），shared 是 DNS-1123 label。
這裡拿定義本身驗形狀，並釘住「哪 7 個欄位接上它」：產生器的 Python 判定
（`_lib_validation.secret_ref_problem`）與 jsonschema 在同一批案例上給同一個結論。
"""
from __future__ import annotations

import json
import os

import jsonschema
import pytest

from _lib_validation import receiver_secret_ref_fields, secret_ref_problem

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SCHEMA = os.path.join(_REPO_ROOT, "docs", "schemas", "tenant-config.schema.json")

# 可接 secretRef 的欄位（Alertmanager v0.34 各有 `<field>_file`）。email `headers`
# 與 webhook `http_config` 沒有對應的 `*_file`，不接。
WIRED = {
    "webhook": {"url"},
    "rocketchat": {"url"},
    "slack": {"api_url"},
    "teams": {"webhook_url"},
    "pagerduty": {"service_key", "routing_key"},
    "email": {"auth_password"},
}


def _load() -> dict:
    with open(_SCHEMA, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def validator():
    schema = _load()
    sub = {"$schema": schema["$schema"], **schema["definitions"]["secretRef"]}
    jsonschema.Draft7Validator.check_schema(sub)
    return jsonschema.Draft7Validator(sub)


def _ref(key="slack-api-url", **extra):
    return {"secret": {"key": key, **extra}}


VALID = [
    _ref(),
    _ref(key="api_url"),
    _ref(key="Slack.API-url_2"),
    _ref(key=".dotfile"),
    _ref(key="a..b"),
    _ref(key="k" * 253),
    _ref(shared="corp-slack"),
    _ref(shared="a"),
    _ref(shared="a" * 63),
]

INVALID = [
    _ref(key="."),
    _ref(key=".."),
    _ref(key="..x"),
    _ref(key="a/b"),                    # 含 /
    _ref(key=""),
    _ref(key="k" * 254),
    _ref(key="k\n"),                   # 結尾換行（Python re 的 $ 會放行）
    _ref(key=5),
    _ref(shared="Corp"),                # 大寫
    _ref(shared="-corp"),
    _ref(shared="corp-"),
    _ref(shared="corp.slack"),          # label，不收 .
    _ref(shared=""),
    _ref(shared="a" * 64),
    _ref(shared="corp\n"),
    _ref(shared=None),
    {"secret": {"name": "alerting", "key": "k"}},                # 舊形狀：不收 name
    {"secret": {"key": "k", "namespace": "x"}},                  # 多餘屬性
    {"secret": {"key": "k"}, "optional": True},                  # 外層多餘屬性
    {"secret": {"shared": "corp"}},                              # 缺 key
    {"secret": {}},
    {},
    {"secret": "a/k"},
    {"secret": None},
    "plain-credential-value",
]


@pytest.mark.parametrize("doc", VALID, ids=lambda d: repr(d)[:60])
def test_valid_references_are_accepted(validator, doc):
    assert list(validator.iter_errors(doc)) == []
    assert secret_ref_problem(doc) is None


@pytest.mark.parametrize("doc", INVALID, ids=lambda d: repr(d)[:60])
def test_invalid_references_are_rejected(validator, doc):
    assert list(validator.iter_errors(doc)) != []
    assert secret_ref_problem(doc) is not None


def test_secret_ref_is_wired_to_exactly_the_seven_credential_fields():
    """接線：恰好這 7 個欄位以 `anyOf: [<值>, secretRef]` 接受它，其他地方都不引用。"""
    schema = _load()
    defs = schema["definitions"]
    ref = {"$ref": "#/definitions/secretRef"}
    seen: dict[str, set[str]] = {}
    for branch in defs["receiver"]["oneOf"]:
        d = defs[branch["$ref"].rsplit("/", 1)[1]]
        rtype = d["properties"]["type"]["const"]
        for field, prop in d["properties"].items():
            if ref in prop.get("anyOf", []):
                assert prop.get("x-credential") is True, f"{rtype}.{field}"
                seen.setdefault(rtype, set()).add(field)
    assert seen == WIRED
    # 除了這 7 處，schema 裡沒有別處引用 secretRef。
    text = json.dumps({k: v for k, v in schema.items() if k != "definitions"})
    text += json.dumps({k: v for k, v in defs.items() if k != "secretRef"})
    assert text.count(json.dumps(ref)) == sum(len(v) for v in WIRED.values())


def test_python_reads_the_same_wiring():
    """產生器讀到的「哪些欄位接 secretRef」就是 schema 的接線。"""
    got = {rtype: set(receiver_secret_ref_fields(rtype)) for rtype in WIRED}
    assert got == WIRED


@pytest.mark.parametrize("rtype,field,receiver", [
    ("slack", "api_url", {"type": "slack"}),
    ("teams", "webhook_url", {"type": "teams"}),
    ("webhook", "url", {"type": "webhook"}),
    ("rocketchat", "url", {"type": "rocketchat"}),
    ("pagerduty", "routing_key", {"type": "pagerduty"}),
    ("pagerduty", "service_key", {"type": "pagerduty"}),
    ("email", "auth_password", {"type": "email", "to": ["a@example.com"],
                                "from": "b@example.com", "smarthost": "smtp.example.com:587"}),
])
def test_tenant_config_accepts_a_reference_on_each_wired_field(rtype, field, receiver):
    v = jsonschema.Draft7Validator(_load())
    for value in (_ref(), _ref(shared="corp-slack")):
        doc = {"tenants": {"t1": {"_routing": {"receiver": {**receiver, field: value}}}}}
        assert [e.message for e in v.iter_errors(doc)] == [], (rtype, field, value)
    bad = {"tenants": {"t1": {"_routing": {"receiver": {
        **receiver, field: {"secret": {"name": "x", "key": "k"}}}}}}}
    assert list(v.iter_errors(bad)) != []


def test_unwired_fields_refuse_a_reference():
    """`channel`、`headers`、`http_config` 等不接：schema 拒收。"""
    v = jsonschema.Draft7Validator(_load())
    for receiver in (
        {"type": "slack", "api_url": "https://hooks.example.com/x", "channel": _ref()},
        {"type": "email", "to": ["a@example.com"], "from": "b@example.com",
         "smarthost": "smtp.example.com:587", "smarthost_x": _ref()},
        {"type": "email", "to": ["a@example.com"], "from": "b@example.com",
         "smarthost": _ref()},
    ):
        doc = {"tenants": {"t1": {"_routing": {"receiver": receiver}}}}
        assert list(v.iter_errors(doc)) != [], receiver
