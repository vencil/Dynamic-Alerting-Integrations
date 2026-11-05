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

from _lib_validation import CREDENTIAL_REF_SITES, receiver_secret_ref_fields, secret_ref_problem

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


_SECRET_REF = {"$ref": "#/definitions/secretRef"}
_PREFIX = "#/definitions/"


def _schema_ref_sites(schema: dict) -> tuple[dict, set[int]]:
    """{(rtype, path): owning property} for every place a receiver's schema
    lets a secretRef stand, read by walking each receiver definition through
    properties ("key"), additionalProperties ("*"), items ("[]"), $ref and
    anyOf / allOf / oneOf (not `not` / `if` / `then`: they only constrain).
    Also the ids of the secretRef members found, to compare with the text
    count."""
    defs = schema["definitions"]
    found: dict[tuple[str, tuple[str, ...]], dict] = {}
    nodes: set[int] = set()

    def walk(rtype, node, path, prop, seen):
        if not isinstance(node, dict):
            return
        if node == _SECRET_REF:
            found[(rtype, path)] = prop
            nodes.add(id(node))
            return
        ref = node.get("$ref")
        if ref:
            name = ref[len(_PREFIX):]
            if name not in seen:
                walk(rtype, defs[name], path, prop, seen | {name})
        for key in ("anyOf", "allOf", "oneOf"):
            for sub in node.get(key, []):
                walk(rtype, sub, path, prop, seen)
        for name, sub in node.get("properties", {}).items():
            walk(rtype, sub, path + (name,), sub, seen)
        if isinstance(node.get("additionalProperties"), dict):
            walk(rtype, node["additionalProperties"], path + ("*",), prop, seen)
        if isinstance(node.get("items"), dict):
            walk(rtype, node["items"], path + ("[]",), prop, seen)

    for branch in defs["receiver"]["oneOf"]:
        name = branch["$ref"][len(_PREFIX):]
        d = defs[name]
        rtype = d["properties"]["type"]["const"]
        walk(rtype, d, (), {}, frozenset({name}))
    return found, nodes


def test_schema_takes_a_reference_exactly_where_the_table_says():
    """⛔ SSOT 對帳：schema 允許 secretRef 的位置（`anyOf: [<值>, secretRef]`）恰好
    是 CREDENTIAL_REF_SITES——頂層欄位與 http_config 葉欄位都算——且每處都標
    x-credential；走訪看到的 secretRef 數等於 schema 文字裡的數量（沒有藏在走不到的地方）。"""
    schema = _load()
    found, nodes = _schema_ref_sites(schema)
    table = {(s.rtype, s.path) for s in CREDENTIAL_REF_SITES}
    assert set(found) == table, (sorted(set(found) - table), sorted(table - set(found)))
    unmarked = sorted(k for k, prop in found.items() if prop.get("x-credential") is not True)
    assert not unmarked, unmarked
    text = json.dumps({k: v for k, v in schema.items() if k != "definitions"})
    text += json.dumps({k: v for k, v in schema["definitions"].items() if k != "secretRef"})
    assert text.count(json.dumps(_SECRET_REF)) == len(nodes)
    # 正向控制：走訪要走得到巢狀列，不只頂層 7 欄。
    assert ("webhook", ("http_config", "http_headers", "*", "secrets", "[]")) in found


def test_top_level_wiring_is_the_tables_and_the_schemas():
    """產生器的頂層視角（receiver_secret_ref_fields，取自資料表）就是 ③-a 的 7 欄，
    schema 讀取器（_receiver_field_schemas 的 secret_ref）讀到的也一樣。"""
    from _lib_validation import _receiver_field_schemas
    got = {rtype: set(receiver_secret_ref_fields(rtype)) for rtype in WIRED}
    assert got == WIRED
    schema_view = {rtype: {f for f, p in props.items() if p.get("secret_ref")}
                   for rtype, props in _receiver_field_schemas().items()}
    assert {r: f for r, f in schema_view.items() if f} == WIRED


def test_table_file_keys_are_the_upstream_file_form():
    """每列 render 成 `<葉>_file`；list 元素例外，併入同層的 `files`
    （prometheus/common Header.Files）。http_config 內的列都有要約束的目的地。"""
    for s in CREDENTIAL_REF_SITES:
        if s.path[-1] == "[]":
            assert s.file_key == "files", s
        else:
            assert s.file_key == f"{s.path[-1]}_file", s
        assert len(s.path) == 1 or s.destination is not None, s


_URL = "https://hook.example.com/a"
_OAUTH = {"client_id": "c", "token_url": "https://token.example.com/t"}


def _webhook_with(path: tuple[str, ...], value) -> dict:
    """A valid webhook receiver with *value* at *path* ("*" → a header name,
    a trailing "[]" → a one-item list), plus the siblings that path needs to
    be valid (oauth2's client_id / token_url, a client cert for a client key)."""
    receiver: dict = {"type": "webhook", "url": _URL}
    node = receiver
    for i, step in enumerate(path):
        key = "X-Token" if step == "*" else step
        rest = path[i + 1:]
        if rest == ("[]",):
            node[key] = [value]
            break
        if not rest:
            node[key] = value
            break
        node = node.setdefault(key, {})
        if key == "oauth2":
            for k, v in _OAUTH.items():
                node.setdefault(k, v)
        if key == "tls_config" and rest == ("key",):
            node.setdefault("cert", "client-cert-pem")
    return receiver


def _doc(receiver: dict) -> dict:
    return {"tenants": {"t1": {"_routing": {"receiver": receiver}}}}


_NESTED = [s for s in CREDENTIAL_REF_SITES if len(s.path) > 1]


@pytest.mark.parametrize("site", _NESTED, ids=lambda s: ".".join(s.path))
def test_schema_accepts_a_reference_on_each_http_config_row(site):
    v = jsonschema.Draft7Validator(_load())
    for value in (_ref(), _ref(shared="corp")):   # 形狀；能不能綁定是產生器的事
        assert [e.message for e in v.iter_errors(_doc(_webhook_with(site.path, value)))] == []
    bad = _webhook_with(site.path, {"secret": {"name": "x", "key": "k"}})
    assert list(v.iter_errors(_doc(bad))) != []


def _http_config_leaves(schema: dict) -> set[tuple[str, ...]]:
    """Every leaf property path under webhook http_config (a property with no
    properties of its own), "*" for a header name, through $ref / anyOf."""
    defs = schema["definitions"]
    out: set[tuple[str, ...]] = set()

    def walk(node, path, seen):
        if not isinstance(node, dict):
            return
        ref = node.get("$ref")
        if ref and ref != _SECRET_REF["$ref"]:
            name = ref[len(_PREFIX):]
            if name not in seen:
                walk(defs[name], path, seen | {name})
        for sub in node.get("anyOf", []):
            walk(sub, path, seen)
        props = node.get("properties")
        if props:
            for name, sub in props.items():
                walk(sub, path + (name,), seen)
        elif path[-1] == "http_headers" and isinstance(node.get("additionalProperties"), dict):
            walk(node["additionalProperties"], path + ("*",), seen)
        elif not ref and "anyOf" not in node:
            out.add(path)

    walk(defs["webhookReceiver"]["properties"]["http_config"], ("http_config",), frozenset())
    return out


def test_schema_refuses_a_reference_on_every_other_http_config_leaf():
    """表上沒有的 http_config 葉欄位一律拒收參照（其餘部分都合法，拒收只來自參照本身）；
    oauth2 claims 任一層也拒。"""
    schema = _load()
    v = jsonschema.Draft7Validator(schema)
    rows = {s.path for s in _NESTED}
    others = sorted(_http_config_leaves(schema) - rows - {p[:-1] for p in rows if p[-1] == "[]"})
    # 正向控制：走訪找得到它該找的葉欄位。
    assert {("http_config", "basic_auth", "username"), ("http_config", "proxy_url"),
            ("http_config", "oauth2", "token_url"), ("http_config", "tls_config", "cert"),
            ("http_config", "http_headers", "*", "values")} <= set(others), others
    accepted = []
    for path in others:
        for where in (path, path + ("[]",)):
            if not list(v.iter_errors(_doc(_webhook_with(where, _ref())))):
                accepted.append(where)
    assert not accepted, accepted
    for claims in ({"a": _ref()}, {"a": [{"b": _ref(shared="corp")}]}):
        receiver = _webhook_with(("http_config", "oauth2", "claims"), claims)
        assert list(v.iter_errors(_doc(receiver))) != [], claims


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


def test_a_reference_counts_as_set_in_the_schemas_pairs():
    """參照在「至多一個」與「cert／key 互相要求」裡算有設，與 Alertmanager 對 render 後
    `*_file` 的判定一致（bearer_token_file 與 basic_auth 互斥、key_file 要 cert）。"""
    v = jsonschema.Draft7Validator(_load())
    both = _webhook_with(("http_config", "bearer_token"), _ref())
    both["http_config"]["basic_auth"] = {"username": "u"}
    assert list(v.iter_errors(_doc(both))) != []
    lone_key = _webhook_with(("http_config", "tls_config", "key"), _ref())
    del lone_key["http_config"]["tls_config"]["cert"]
    assert list(v.iter_errors(_doc(lone_key))) != []
    lone_cert = {"type": "webhook", "url": _URL, "http_config": {"tls_config": {"cert": "c"}}}
    assert list(v.iter_errors(_doc(lone_cert))) != []


def test_a_header_named_secret_is_refused_like_the_generator_refuses_it():
    v = jsonschema.Draft7Validator(_load())
    receiver = {"type": "webhook", "url": _URL,
                "http_config": {"http_headers": {"secret": {"values": ["v"]}}}}
    assert list(v.iter_errors(_doc(receiver))) != []
    receiver["http_config"]["http_headers"] = {"X-Secret": {"values": ["v"]}}
    assert list(v.iter_errors(_doc(receiver))) == []
