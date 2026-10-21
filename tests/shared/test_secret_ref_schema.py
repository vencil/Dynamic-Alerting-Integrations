"""`definitions.secretRef` 的形狀（#1560 裁決 2）。

secretRef 是 Kubernetes Secret 的參照 `{secret: {name, key}}`：name 依 DNS-1123
subdomain，key 依 Secret data key 規則（Kubernetes IsConfigMapKey）。本 PR 只定義、
尚未接到任何欄位（接線須與產生器 render 成 `*_file` 原子上線），所以這裡直接拿
定義本身驗，並釘住「沒有任何地方引用它」，接線時這條要跟著改。
"""
from __future__ import annotations

import json
import os

import jsonschema
import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SCHEMA = os.path.join(_REPO_ROOT, "docs", "schemas", "tenant-config.schema.json")


def _load() -> dict:
    with open(_SCHEMA, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def validator():
    schema = _load()
    sub = {"$schema": schema["$schema"], **schema["definitions"]["secretRef"]}
    jsonschema.Draft7Validator.check_schema(sub)
    return jsonschema.Draft7Validator(sub)


def _ref(name="alerting-receivers", key="slack-api-url"):
    return {"secret": {"name": name, "key": key}}


@pytest.mark.parametrize("doc", [
    _ref(),
    _ref(name="a"),
    _ref(name="team.alerting-1"),
    _ref(name="a" * 253),
    _ref(key="api_url"),
    _ref(key="Slack.API-url_2"),
    _ref(key=".dotfile"),
    _ref(key="a..b"),
    _ref(key="k" * 253),
], ids=lambda d: repr(d)[:60])
def test_valid_references_are_accepted(validator, doc):
    assert list(validator.iter_errors(doc)) == []


@pytest.mark.parametrize("doc", [
    _ref(name="Alerting"),              # 大寫
    _ref(name="a" * 254),               # 超長
    _ref(name="-alerting"),             # 以 - 開頭
    _ref(name="alerting-"),             # 以 - 結尾
    _ref(name=""),
    _ref(name="alerting\n"),           # 結尾換行（Python re 的 $ 會放行）
    _ref(key="."),
    _ref(key=".."),
    _ref(key="..x"),
    _ref(key="a/b"),                    # 含 /
    _ref(key=""),
    _ref(key="k" * 254),
    _ref(key="k\n"),                   # 結尾換行
    {"secret": {"name": "a", "key": "k", "namespace": "x"}},   # 多餘屬性
    {"secret": {"name": "a", "key": "k"}, "optional": True},  # 外層多餘屬性
    {"secret": {"name": "a"}},          # 缺 key
    {"secret": {"key": "k"}},           # 缺 name
    {},
    {"secret": "a/k"},
    "plain-credential-value",
], ids=lambda d: repr(d)[:60])
def test_invalid_references_are_rejected(validator, doc):
    assert list(validator.iter_errors(doc)) != []


def test_secret_ref_is_not_wired_to_any_field_yet():
    """接線（欄位改 oneOf 接受 secretRef）須與產生器同一個變更上線（#1560 裁決 3）。"""
    schema = _load()
    assert "secretRef" in schema["definitions"]
    del schema["definitions"]["secretRef"]
    assert "#/definitions/secretRef" not in json.dumps(schema)
