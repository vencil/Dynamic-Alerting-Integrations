"""憑證明文的單一判定（#1560 ⑤-a，owner 裁決）。

一、分類表的完整性（fail-closed）：tenant schema 上每個標了 `"x-credential": true`
的節點，連同其子樹的每一個葉欄位，都必須**恰好**被分類一次——
`CREDENTIAL_REF_SITES` 的一列（可放參照）、`CREDENTIAL_PLAINTEXT_SITES` 的一列
（不收參照：允許明文或禁止租戶寫），或本檔的 `NOT_CREDENTIALS`（明確列出的非憑證）。
schema 多一個欄位卻沒分類 → 紅；表裡有一列對不到 schema 上真的存在的路徑 → 也紅。

二、`plaintext_credential_findings` 逐列正反例，與 userinfo 判定的邊界。
"""
from __future__ import annotations

import json
import os

import pytest

from _lib_validation import (
    CREDENTIAL_PLAINTEXT_SITES,
    CREDENTIAL_REF_SITES,
    NEEDS_REFERENCE,
    PLAINTEXT_ALLOWED,
    PLAINTEXT_FORBIDDEN,
    PLAINTEXT_POLICIES,
    PLAINTEXT_REFUSED,
    PLAINTEXT_UNLESS_USERINFO,
    PROXY_CREDENTIAL,
    URL_USERINFO,
    value_paths_to_mask,
    plaintext_credential_findings,
    url_has_userinfo,
)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SCHEMA = os.path.join(_REPO_ROOT, "docs", "schemas", "tenant-config.schema.json")
_PREFIX = "#/definitions/"
_SECRET_REF = "#/definitions/secretRef"

#: x-credential 子樹裡**不是**憑證的葉欄位（webhook http_config 底下的設定值）。
#: 路徑寫法同 CREDENTIAL_REF_SITES：`*` 任一 key、`[]` 任一項。
NOT_CREDENTIALS: frozenset[tuple[str, tuple[str, ...]]] = frozenset({
    ("webhook", ("http_config", "authorization", "type")),
    ("webhook", ("http_config", "basic_auth", "username")),
    ("webhook", ("http_config", "enable_http2")),
    ("webhook", ("http_config", "follow_redirects")),
    ("webhook", ("http_config", "no_proxy")),
    ("webhook", ("http_config", "proxy_from_environment")),
    ("webhook", ("http_config", "oauth2", "audience")),
    ("webhook", ("http_config", "oauth2", "client_certificate_key_id")),
    ("webhook", ("http_config", "oauth2", "client_id")),
    ("webhook", ("http_config", "oauth2", "grant_type")),
    ("webhook", ("http_config", "oauth2", "iss")),
    ("webhook", ("http_config", "oauth2", "no_proxy")),
    ("webhook", ("http_config", "oauth2", "proxy_from_environment")),
    ("webhook", ("http_config", "oauth2", "scopes", "[]")),
    ("webhook", ("http_config", "oauth2", "signature_algorithm")),
    ("webhook", ("http_config", "oauth2", "token_url")),
    *(("webhook", (*tls, leaf))
      for tls in (("http_config", "tls_config"), ("http_config", "oauth2", "tls_config"))
      for leaf in ("ca", "cert", "insecure_skip_verify", "max_version", "min_version",
                   "server_name")),
})


def _load() -> dict:
    with open(_SCHEMA, encoding="utf-8") as f:
        return json.load(f)


def _credential_subtrees(schema: dict) -> tuple[set, set]:
    """(葉欄位, 所有節點路徑)，每項 `(rtype, path)`：從每個 receiver 定義往下走，
    進入 x-credential 節點後收集其子樹。走 properties（key）、additionalProperties
    （`*`）、items（`[]`），經 $ref / anyOf / oneOf；不走 allOf / not / if / then
    （只做約束）與 secretRef 本身。葉＝沒有任何子節點的節點。"""
    defs = schema["definitions"]
    leaves: set = set()
    nodes: set = set()

    def children(node, seen):
        """[(step, child node, seen)] of *node*, through $ref / anyOf / oneOf."""
        if not isinstance(node, dict):
            return []
        out = []
        ref = node.get("$ref")
        if ref and ref != _SECRET_REF:
            name = ref[len(_PREFIX):]
            if name not in seen:
                out += children(defs[name], seen | {name})
        for key in ("anyOf", "oneOf"):
            for sub in node.get(key, []):
                out += children(sub, seen)
        for name, sub in node.get("properties", {}).items():
            out.append((name, sub, seen))
        if isinstance(node.get("additionalProperties"), dict):
            out.append(("*", node["additionalProperties"], seen))
        if isinstance(node.get("items"), dict):
            out.append(("[]", node["items"], seen))
        return out

    def is_credential(node, seen) -> bool:
        if not isinstance(node, dict):
            return False
        if node.get("x-credential") is True:
            return True
        ref = node.get("$ref")
        return bool(ref and ref != _SECRET_REF and ref[len(_PREFIX):] not in seen
                    and is_credential(defs[ref[len(_PREFIX):]], seen | {ref[len(_PREFIX):]}))

    def walk(rtype, node, path, seen, inside):
        inside = inside or is_credential(node, seen)
        kids = children(node, seen)
        if inside:
            nodes.add((rtype, path))
            if not kids:
                leaves.add((rtype, path))
        for step, sub, sub_seen in kids:
            walk(rtype, sub, path + (step,), sub_seen, inside)

    for branch in defs["receiver"]["oneOf"]:
        name = branch["$ref"][len(_PREFIX):]
        d = defs[name]
        rtype = d["properties"]["type"]["const"]
        walk(rtype, d, (), frozenset({name}), False)
    return leaves, nodes


def _covers(row: tuple[str, ...], path: tuple[str, ...]) -> bool:
    """列路徑 *row* 是否涵蓋 *path*：相同，或是它的前綴（一列管它整個子樹）。"""
    return path[:len(row)] == row


def _classifications():
    return ([("ref", s.rtype, s.path) for s in CREDENTIAL_REF_SITES]
            + [("plaintext", s.rtype, s.path) for s in CREDENTIAL_PLAINTEXT_SITES]
            + [("not-credential", r, p) for r, p in sorted(NOT_CREDENTIALS)])


def test_every_credential_leaf_is_classified_exactly_once():
    """⛔ fail-closed：x-credential 子樹的每一個葉欄位，恰好被一列分類。"""
    leaves, _nodes = _credential_subtrees(_load())
    # 正向控制：走訪要真的走進子樹，不只停在頂層欄位。
    assert ("webhook", ("http_config", "oauth2", "tls_config", "key")) in leaves
    assert ("webhook", ("http_config", "proxy_connect_header", "*", "[]")) in leaves
    assert ("email", ("headers", "*")) in leaves
    assert ("slack", ("api_url",)) in leaves
    unclassified, twice = [], []
    for rtype, path in sorted(leaves):
        hits = [(kind, row) for kind, r, row in _classifications()
                if r == rtype and _covers(row, path)]
        if not hits:
            unclassified.append((rtype, path))
        elif len(hits) > 1:
            twice.append((rtype, path, hits))
    assert not unclassified, f"x-credential leaves no table classifies: {unclassified}"
    assert not twice, f"leaves classified more than once: {twice}"


def test_every_row_names_a_place_the_schema_has():
    """反方向：每一列（三張表）都對得到 schema 上 x-credential 子樹裡真的存在的路徑。"""
    _leaves, nodes = _credential_subtrees(_load())
    stale = [(kind, r, row) for kind, r, row in _classifications() if (r, row) not in nodes]
    assert not stale, stale


def test_policies_are_the_owners_decision():
    """政策值逐字寫在這裡（不從表推導）：表改了政策 → 紅，要 owner 重新裁決。"""
    ref = {(s.rtype, s.path): s.plaintext for s in CREDENTIAL_REF_SITES}
    assert ref.pop(("webhook", ("url",))) == PLAINTEXT_UNLESS_USERINFO
    assert set(ref.values()) == {PLAINTEXT_REFUSED}, ref
    plain = {(s.rtype, s.path): s.policy for s in CREDENTIAL_PLAINTEXT_SITES}
    assert plain == {
        ("email", ("headers", "*")): PLAINTEXT_ALLOWED,
        ("webhook", ("http_config", "http_headers", "*", "values", "[]")): PLAINTEXT_ALLOWED,
        ("webhook", ("http_config", "oauth2", "endpoint_params", "*")): PLAINTEXT_ALLOWED,
        ("webhook", ("http_config", "oauth2", "claims")): PLAINTEXT_ALLOWED,
        ("webhook", ("http_config", "proxy_connect_header")): PLAINTEXT_FORBIDDEN,
        ("webhook", ("http_config", "oauth2", "proxy_connect_header")): PLAINTEXT_FORBIDDEN,
        ("webhook", ("http_config", "proxy_url")): PLAINTEXT_UNLESS_USERINFO,
        ("webhook", ("http_config", "oauth2", "proxy_url")): PLAINTEXT_UNLESS_USERINFO,
    }
    assert {s.policy for s in CREDENTIAL_PLAINTEXT_SITES} <= PLAINTEXT_POLICIES


# ── 判定：逐列正反例 ────────────────────────────────────────────────────

_CANARY = "zz-canary-0f3b"
_REF = {"secret": {"key": "k"}}


def _build(rtype: str, path: tuple[str, ...], value) -> dict:
    """*rtype* receiver with *value* at *path*: "*" is the key `X-Name`, and a
    trailing "[]" makes the last key a one-item list of *value*."""
    receiver: dict = {"type": rtype}
    node = receiver
    keys = ["X-Name" if step == "*" else step for step in path if step != "[]"]
    for key in keys[:-1]:
        node = node.setdefault(key, {})
    node[keys[-1]] = [value] if path[-1] == "[]" else value
    return receiver


_REFUSED_ROWS = [s for s in CREDENTIAL_REF_SITES if s.plaintext == PLAINTEXT_REFUSED]


@pytest.mark.parametrize("site", _REFUSED_ROWS, ids=lambda s: f"{s.rtype}.{'.'.join(s.path)}")
def test_reference_only_rows(site):
    """只收參照的列：明文 → needs_reference；參照、null、空字串 → 無。"""
    got = plaintext_credential_findings(_build(site.rtype, site.path, _CANARY))
    assert [(f.site, f.category) for f in got] == [(site.path, NEEDS_REFERENCE)]
    assert _CANARY not in repr(got)          # 回傳值不含憑證值
    for quiet in (_REF, {"secret": {"key": "k", "shared": "corp"}}, None, ""):
        assert plaintext_credential_findings(_build(site.rtype, site.path, quiet)) == []


def test_webhook_url_takes_plaintext_but_not_userinfo():
    ok = {"type": "webhook", "url": "https://hook.example.com/a?token=x"}
    assert plaintext_credential_findings(ok) == []
    bad = {"type": "webhook", "url": f"https://bot:{_CANARY}@hook.example.com/a"}
    got = plaintext_credential_findings(bad)
    assert [(f.path, f.category) for f in got] == [(("url",), URL_USERINFO)]
    assert _CANARY not in repr(got)
    assert plaintext_credential_findings({"type": "webhook", "url": _REF}) == []


@pytest.mark.parametrize("site", [s for s in CREDENTIAL_PLAINTEXT_SITES
                                  if s.policy == PLAINTEXT_ALLOWED],
                         ids=lambda s: f"{s.rtype}.{'.'.join(s.path)}")
def test_allowed_rows_are_never_findings(site):
    value = {"a": _CANARY} if site.path[-1] == "claims" else _CANARY
    assert plaintext_credential_findings(_build(site.rtype, site.path, value)) == []


@pytest.mark.parametrize("site", [s for s in CREDENTIAL_PLAINTEXT_SITES
                                  if s.policy == PLAINTEXT_FORBIDDEN],
                         ids=lambda s: ".".join(s.path))
def test_proxy_connect_header_is_refused_whatever_its_value(site):
    for value in ({"Proxy-Authorization": [_CANARY]}, {}, None):
        got = plaintext_credential_findings(_build(site.rtype, site.path, value))
        assert [(f.path, f.category) for f in got] == [(site.path, PROXY_CREDENTIAL)], value
    assert plaintext_credential_findings({"type": "webhook", "http_config": {}}) == []


@pytest.mark.parametrize("site", [s for s in CREDENTIAL_PLAINTEXT_SITES
                                  if s.policy == PLAINTEXT_UNLESS_USERINFO],
                         ids=lambda s: ".".join(s.path))
def test_proxy_url_is_refused_with_userinfo_only(site):
    bad = _build(site.rtype, site.path, f"http://u:{_CANARY}@proxy.example.com:3128")
    got = plaintext_credential_findings(bad)
    assert [(f.path, f.category) for f in got] == [(site.path, PROXY_CREDENTIAL)]
    assert _CANARY not in repr(got)
    for ok in ("http://proxy.example.com:3128", None, ""):
        assert plaintext_credential_findings(_build(site.rtype, site.path, ok)) == []


def test_findings_name_every_list_item_and_header():
    r = {"type": "webhook", "url": "https://h.example.com",
         "http_config": {"http_headers": {
             "A": {"secrets": ["x", _REF, "y"], "values": ["v"]},
             "B": {"secrets": ["z"]}}}}
    got = [f.path for f in plaintext_credential_findings(r)]
    hh = ("http_config", "http_headers")
    assert got == [hh + ("A", "secrets", 0), hh + ("A", "secrets", 2), hh + ("B", "secrets", 0)]


def test_a_receiver_of_no_known_shape_has_none():
    assert plaintext_credential_findings("slack") == []
    assert plaintext_credential_findings({"api_url": _CANARY}) == []           # no type
    assert plaintext_credential_findings({"type": "slack", "api_url": _CANARY},
                                         rtype="nope") == []


@pytest.mark.parametrize("url,expected", [
    ("https://user:pw@hook.example.com/a", True),
    ("https://user@hook.example.com/a", True),          # 只有 user 也算
    ("https://@hook.example.com/a", True),              # 空的 userinfo 仍是 userinfo
    ("https://hook.example.com/a?to=ops@example.com", False),   # @ 在 query
    ("https://hook.example.com/a/b@c", False),          # @ 在 path
    ("https://hook.example.com/a#x@y", False),          # @ 在 fragment
    ("socks5://u:p@proxy.example.com:1080", True),      # 非 http scheme 的 authority 也算
    ("mailto:ops@example.com", False),                  # 沒有 authority
    ("hook.example.com:80@evil.example", False),        # 沒有 // 就沒有 authority
    ("https://[::1@x/", True),                          # 解析器拒收、帶 @：當成有
])
def test_userinfo_boundaries(url, expected):
    assert url_has_userinfo(url) is expected


def test_masking_paths_cover_every_written_out_credential_value():
    """遮罩清單（工具顯示文字前用）涵蓋兩張表所有明文值，參照與非憑證欄位不在內。"""
    r = {"type": "webhook", "url": "https://h.example.com",
         "http_config": {"basic_auth": {"username": "u", "password": "p"},
                         "bearer_token": _REF,
                         "oauth2": {"client_id": "c", "token_url": "https://t.example.com",
                                    "claims": {"a": {"b": "c"}}},
                         "proxy_connect_header": {"P": ["v"]}}}
    assert value_paths_to_mask(r) == [
        ("url",), ("http_config", "basic_auth", "password"),
        ("http_config", "oauth2", "claims", "a", "b"),
        ("http_config", "proxy_connect_header", "P", 0)]
