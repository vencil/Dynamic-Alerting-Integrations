"""#3703：conf.d 的值不進訊息——routing 以外的保留鍵那一面的動態 canary 矩陣。

`test_routing_value_canary_matrix.py` 守的是 routing 區塊；這裡守其餘的保留鍵。
兩邊量的是同一條規則：設定作者寫的值（與鍵名）不會原樣出現在 route generator
的訊息裡（stdout、stderr、`--findings-json`）。

這一面的訊息有一段不是 Python 寫的：key verdict 的 `reason` 是 Go
（`pkg/config` 的 `LayerValueProblem.Problem`）寫好的句子，Python 原樣印出。所以
規則在 Go 那邊是「句子只由常數組成」，這裡照行為量整條路：

* 保留鍵由登記表 `reserved_keys.json` 推導，不手寫鍵名：`keys` 的每個鍵，在
  登記表說有 reader 讀它的每一層；有 reader 的 prefix（`_state_`）以
  `_PREFIX_KEYS` 的實例代表。登記表多一個鍵、多一個有 reader 的 prefix、或
  多一層而這裡沒有對應 → `test_the_matrix_is_the_registry` 紅。
* 每格塞三種形狀：帶 userinfo 的 canary URL 字串、鍵名是 canary 的 mapping、
  裝著那個 mapping 的 list。另有 `_DEEP`：canary 放在 mapping 值的各個欄位裡。
* 跑 `--validate --strict --findings-json`，比對**不分大小寫**——
  `_silent_mode`、`_severity_dedup` 的 reader 會先把值轉小寫。
* 每格還要證明那個位置真的被判了：該有的 finding kind 在、且沒有多的。
  generator 對它什麼都不說的格子（`_NOT_JUDGED`）逐一列出並寫理由；哪天它
  開始說話，那格會紅，逼人重新分類。

⚠️ 不在這個矩陣裡：key verdict 文件的 `key_checks`（`version` label、已改名
的鍵）。那兩種訊息是 exporter 的 `KeyValidation` 原句，仍帶鍵名與 label 值。
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.usefixtures("judging_run")

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_routing_value_canary_matrix import _GAR, validate_run  # noqa: E402
from _lib_validation import key_segment, shown_key  # noqa: E402

_REGISTRY = json.loads((REPO / "components" / "threshold-exporter" / "app" / "pkg" / "config"
                        / "reserved_keys.json").read_text(encoding="utf-8"))


def _canary(tag: str) -> str:
    # A test canary, not a credential: no real host, user or password. Mixed
    # case on purpose (a reader that lower-cases must not get it through).
    return f"https://u:CANARYPW@hooks.example/CaNaRy-{tag}"  # trufflehog:ignore


def _no_canary(text: str) -> None:
    assert "canary" not in text.lower(), text


_FORMS = {
    "url_string": lambda c: c,
    "canary_key_mapping": lambda c: {c: "x"},
    "canary_key_list": lambda c: [{c: "x"}],
}

#: The state filters the root declares, so a `_state_<filter>` is a key the
#: exporter reads (an undeclared one is the verdict's unread key instead).
_STATE_FILTERS = {"maintenance": {"reasons": [], "severity": "warning"},
                  "container_crashloop": {"reasons": ["CrashLoopBackOff"], "severity": "critical"}}

#: A registered prefix some reader reads → the keys that stand for it here:
#: the one filter that takes the structured form, and any other.
_PREFIX_KEYS = {"_state_": ("_state_maintenance", "_state_container_crashloop")}


def _files(tenant: dict, defaults: dict | None = None, **more: dict) -> dict[str, dict]:
    root = {"defaults": {"cpu_usage_percent": 80}, "state_filters": _STATE_FILTERS}
    root.update(defaults or {})
    return {"_defaults.yaml": root, "t-a.yaml": {"tenants": {"t-a": tenant}}, **more}


def _place(layer: str, key: str, value: object) -> dict[str, dict]:
    """The conf.d files with *value* at *key* in registry layer *layer*."""
    tenant: dict = {"cpu_usage_percent": "85"}
    return _LAYER_WRITERS[layer](tenant, key, value)


#: How the matrix writes a key into each layer of the registry.
_LAYER_WRITERS = {
    "tenant": lambda tenant, key, value: _files({**tenant, key: value}),
    "platform_tenants": lambda tenant, key, value: _files(
        tenant, {"tenants": {"t-a": {key: value}}}),
    "profile": lambda tenant, key, value: _files(
        {**tenant, "_profile": "p"}, **{"_profiles.yaml": {"profiles": {"p": {key: value}}}}),
    "platform_top": lambda tenant, key, value: _files(tenant, {key: value}),
}

_VALUE_UNREAD = {"key_value_unread"}
_REFUSED = {"refusal_summary"}

#: Per registry key (or reader-carrying prefix): the finding kinds a canary
#: at that key causes — one set for every form, or a set per form. An empty
#: set is a place the generator says nothing about: it needs a `_NOT_JUDGED`
#: reason.
_EXPECTED: dict[str, "set[str] | dict[str, set[str]]"] = {
    "_silent_mode": _VALUE_UNREAD,
    "_metadata": _VALUE_UNREAD,
    "_profile": _VALUE_UNREAD,
    "_state_": _VALUE_UNREAD,
    # The generator reads it too, and refuses the tree before the verdict.
    "_severity_dedup": {"severity_dedup_invalid"} | _REFUSED,
    # Policy-as-Code refuses the rules, so no verdict is given.
    "_policies": {"key_verdict_incomplete"},
    "_namespaces": set(),
    "_custom_alerts": set(),
    # The routing keys: the routing matrix walks inside them; here, the key
    # itself holding a canary.
    "_routing": {"url_string": {"routing_not_mapping"},
                 "canary_key_mapping": {"routing_key_ignored", "missing_receiver_field"},
                 "canary_key_list": {"routing_not_mapping"}},
    "_routing_profile": {"url_string": {"unknown_routing_profile"},
                         "canary_key_mapping": {"routing_profile_invalid"} | _REFUSED,
                         "canary_key_list": {"routing_profile_invalid"} | _REFUSED},
    "_routing_defaults": {"url_string": {"routing_defaults_not_mapping"},
                          "canary_key_mapping": {"routing_key_ignored", "missing_receiver_field"},
                          "canary_key_list": {"routing_defaults_not_mapping"}},
    "_routing_enforced": {"url_string": set(),
                          "canary_key_mapping": {"routing_key_ignored"},
                          "canary_key_list": set()},
}

#: The places the generator reports nothing about, each with why. `*`: every
#: form. A place listed here that starts producing a finding fails its cell.
_NOT_JUDGED = {
    ("_namespaces", "*"): "an annotation: no reader on the alert path reads it, so none judges "
                          "its value",
    ("_custom_alerts", "*"): "the generator reads the recipes the exporter serves (the #1211 "
                             "delivery check) and reports nothing about a list that serves none; "
                             "compile-custom-alerts is the tool that judges the list",
    ("_routing_enforced", "url_string"): "a `_routing_enforced` that is not a mapping is read as "
                                         "absent, and nothing says so",
    ("_routing_enforced", "canary_key_list"): "as `url_string`: not a mapping, read as absent",
}

#: Kinds whose finding names the key itself in `field`: the proof that THIS
#: key was judged.
_FIELD_IS_KEY = {"key_value_unread", "severity_dedup_invalid", "routing_not_mapping",
                 "unknown_routing_profile", "routing_profile_invalid",
                 "routing_defaults_not_mapping"}


def _registry_entries() -> dict[str, dict]:
    """Registry key or reader-carrying prefix → its entry."""
    return {**_REGISTRY["keys"],
            **{p: e for p, e in _REGISTRY["prefixes"].items() if e["read_by"]}}


def _layers(entry: dict) -> list[str]:
    return sorted({layer for layers in entry["read_by"].values() for layer in layers})


def _expected(name: str, form: str) -> set[str]:
    e = _EXPECTED[name]
    return e[form] if isinstance(e, dict) else e


_CASES = [(layer, key, name, form)
          for name, entry in sorted(_registry_entries().items())
          for key in _PREFIX_KEYS.get(name, (name,))
          for layer in _layers(entry)
          for form in _FORMS]


def test_the_matrix_is_the_registry():
    """登記表的每個鍵、每個有 reader 的 prefix、每一層，這裡都有位置；沒有多的。"""
    entries = _registry_entries()
    assert set(_EXPECTED) == set(entries)
    assert set(_PREFIX_KEYS) == {p for p, e in _REGISTRY["prefixes"].items() if e["read_by"]}
    for prefix, keys in _PREFIX_KEYS.items():
        assert keys and all(k.startswith(prefix) and k != prefix for k in keys), (prefix, keys)
    read_layers = {layer for e in entries.values() for layer in _layers(e)}
    assert read_layers <= set(_LAYER_WRITERS), read_layers - set(_LAYER_WRITERS)
    assert set(_LAYER_WRITERS) <= set(_REGISTRY["layers"])
    # Every key the exporter reads is in the matrix in each layer it reads it.
    cells = {(layer, name) for layer, _key, name, _form in _CASES}
    for name, entry in entries.items():
        for layer in entry["read_by"].get("exporter", []):
            assert (layer, name) in cells, (layer, name)


def test_every_silent_place_is_declared():
    """沒有 finding 的格子都列在 `_NOT_JUDGED`、附理由；列了的也真的是空的。"""
    silent = {(name, form) for name in _EXPECTED for form in _FORMS if not _expected(name, form)}
    declared = {(name, form) for name, f in _NOT_JUDGED for form in (_FORMS if f == "*" else (f,))}
    assert silent == declared, (sorted(silent - declared), sorted(declared - silent))
    assert all(reason.strip() for reason in _NOT_JUDGED.values())


def _judged(findings: list[dict], key: str, expected: set[str]) -> None:
    """The place was judged: exactly the kinds it causes, and the ones that
    name a key name THIS key."""
    kinds = {f["kind"] for f in findings}
    assert kinds == expected, (sorted(kinds), sorted(expected), findings)
    for f in findings:
        if f["kind"] in _FIELD_IS_KEY:
            assert f["field"] == key, f


@pytest.mark.parametrize("layer,key,name,form", _CASES,
                         ids=[f"{layer}-{key}-{form}" for layer, key, _n, form in _CASES])
def test_no_reserved_value_reaches_a_message(tmp_path, layer, key, name, form):
    value = _FORMS[form](_canary(f"{layer}-{key}-{form}"))
    findings, text = validate_run(tmp_path, _place(layer, key, value))
    _judged(findings, key, _expected(name, form))
    _no_canary(text)


_C = _canary("deep")
_SUN = {"weekdays": ["sun"], "window": "02:00-06:00"}
#: A canary inside a value: (id, key, value, the kinds it causes). An empty
#: set carries its reason in `_DEEP_NOT_JUDGED`.
_DEEP = [
    ("state-target", "_state_maintenance", {"target": _C}, _VALUE_UNREAD),
    ("state-expires", "_state_maintenance", {"target": "enable", "expires": _C}, _VALUE_UNREAD),
    ("state-enabled", "_state_maintenance", {"target": "enable", "enabled": _C}, _VALUE_UNREAD),
    ("state-recurring", "_state_maintenance", {"recurring": _C}, _VALUE_UNREAD),
    ("state-recurring-field", "_state_maintenance", {"recurring": [{_C: 1}]}, _VALUE_UNREAD),
    ("state-recurring-weekday", "_state_maintenance",
     {"recurring": [{**_SUN, "weekdays": [_C]}]}, _VALUE_UNREAD),
    ("state-recurring-window", "_state_maintenance",
     {"recurring": [{**_SUN, "window": _C}]}, _VALUE_UNREAD),
    ("state-recurring-reason", "_state_maintenance",
     {"recurring": [{**_SUN, "reason": [_C]}]}, _VALUE_UNREAD),
    ("state-reason", "_state_maintenance", {"target": "enable", "reason": _C}, set()),
    ("silent-target", "_silent_mode", {"target": _C}, _VALUE_UNREAD),
    ("silent-target-list", "_silent_mode", {"target": [_C]}, _VALUE_UNREAD),
    ("silent-expires", "_silent_mode", {"target": "all", "expires": _C}, _VALUE_UNREAD),
    ("silent-reason", "_silent_mode", {"target": "all", "reason": _C}, set()),
    ("metadata-field-list", "_metadata", {"owner": [_C]}, _VALUE_UNREAD),
    ("metadata-field", "_metadata", {"owner": _C}, set()),
]
_DEEP_NOT_JUDGED = {
    "state-reason": "`reason` is free text; the value is one the exporter reads",
    "silent-reason": "`reason` is free text; the value is one the exporter reads",
    "metadata-field": "a metadata field is free text, served as a label of "
                      "tenant_metadata_info — not a message",
}


def test_every_silent_deep_place_is_declared():
    assert {i for i, _k, _v, kinds in _DEEP if not kinds} == set(_DEEP_NOT_JUDGED)


@pytest.mark.parametrize("key,value,expected", [c[1:] for c in _DEEP], ids=[c[0] for c in _DEEP])
def test_no_value_inside_a_reserved_value_reaches_a_message(tmp_path, key, value, expected):
    findings, text = validate_run(tmp_path, _place("tenant", key, value))
    _judged(findings, key, expected)
    _no_canary(text)


def test_an_invalid_tenant_id_is_not_printed(tmp_path):
    """租戶 id 是作者寫的：不合法的那個不進訊息，也不進 finding 的 `tenant` 欄位。"""
    files = _files({"cpu_usage_percent": "85"})
    files["t-a.yaml"]["tenants"][_canary("tenant-id")] = {"cpu_usage_percent": "85"}
    findings, text = validate_run(tmp_path, files)
    assert {f["kind"] for f in findings} == {"invalid_tenant_id"} | _REFUSED, findings
    [f] = [f for f in findings if f["kind"] == "invalid_tenant_id"]
    assert f"tenant id '{shown_key(_canary('tenant-id'))}'" in f["message"], f
    assert f["tenant"] is None, f
    # What locates it instead: the file that declares it.
    assert f["file"] == "t-a.yaml" and f["message"].lstrip().startswith("t-a.yaml: "), f
    _no_canary(text)


def test_two_unprinted_tenant_ids_are_two_findings(tmp_path):
    """兩個長度相同、都不印出來的不合法 id，分在兩個檔：是兩筆分得開的 finding
    （tenant-api 用 kind／file／message 比對哪一筆是寫入前就有的）。"""
    files = _files({"cpu_usage_percent": "85"})
    files["a.yaml"] = {"tenants": {_canary("id-1"): {"cpu_usage_percent": "85"}}}
    files["b.yaml"] = {"tenants": {_canary("id-2"): {"cpu_usage_percent": "85"}}}
    findings, text = validate_run(tmp_path, files)
    found = [f for f in findings if f["kind"] == "invalid_tenant_id"]
    assert sorted(f["file"] for f in found) == ["a.yaml", "b.yaml"], found
    assert len({f["message"] for f in found}) == 2, found
    _no_canary(text)


def test_a_duplicate_tenant_id_is_not_printed(tmp_path):
    """兩個租戶檔宣告同一個 id：那個 id 也是作者寫的。"""
    files = _files({"cpu_usage_percent": "85"})
    for name in ("a.yaml", "b.yaml"):
        files[name] = {"tenants": {_canary("duplicate"): {"cpu_usage_percent": "85"}}}
    findings, text = validate_run(tmp_path, files)
    assert {f["kind"] for f in findings} == {"duplicate_tenant"} | _REFUSED, findings
    [f] = [f for f in findings if f["kind"] == "duplicate_tenant"]
    assert f"tenant '{shown_key(_canary('duplicate'))}'" in f["message"] and f["tenant"] is None, f
    assert "a.yaml, b.yaml" in f["message"], f
    _no_canary(text)


def test_a_platform_entry_for_no_tenant_is_not_printed(tmp_path):
    """根平台檔 `tenants:` 裡沒有租戶檔宣告的 id：那行 WARN（只在 stderr，不是
    finding）說它被忽略，不印那個 id。"""
    files = _files({"cpu_usage_percent": "85"},
                   {"tenants": {_canary("platform-id"): {"_silent_mode": "purple"}}})
    _findings, text = validate_run(tmp_path, files)
    shown = shown_key(_canary("platform-id"))
    assert (f"WARN: _defaults.yaml: tenants.{shown} ignored — no tenant file declares tenant "
            f"'{shown}'") in text, text
    _no_canary(text)


@pytest.mark.parametrize("value", [
    "maybe",                                                   # a value the exporter does not read
    {"default": "enable", "expires": "2099-01-01T00:00:00Z"},  # a schedule on a reserved key
], ids=["unread_value", "schedule"])
def test_a_severity_dedup_finding_does_not_carry_an_unvouched_id(tmp_path, value):
    """`_severity_dedup` 由 Go 判（da-guard severity-dedup），在寫它的那一層判——
    根平台檔 `tenants:` 裡沒有租戶檔宣告的 id 也判。那個 id 是作者寫的：不進訊息，
    也不進 finding 的 `tenant`。"""
    files = _files({"cpu_usage_percent": "85"},
                   {"tenants": {_canary("dedup-owner"): {"_severity_dedup": value}}})
    findings, text = validate_run(tmp_path, files)
    assert {f["kind"] for f in findings} == {"severity_dedup_invalid"} | _REFUSED, findings
    [f] = [f for f in findings if f["kind"] == "severity_dedup_invalid"]
    assert (f["tenant"], f["file"], f["field"]) == (None, "_defaults.yaml", "_severity_dedup"), f
    assert shown_key(_canary("dedup-owner")) in f["message"], f
    _no_canary(text)


def test_a_threshold_profile_name_is_not_printed(tmp_path):
    """threshold profile 的名字是作者寫的鍵：profile 裡寫了 routing 層的鍵時，那行
    「不會被 render」的訊息與 `field` 都不帶名字。"""
    name = _canary("profile")
    files = _files({"cpu_usage_percent": "85", "_profile": name},
                   **{"_profiles.yaml": {"profiles": {name: {"_severity_dedup": "disable"}}}})
    findings, text = validate_run(tmp_path, files)
    [f] = [f for f in findings if f["kind"] == "routing_in_unread_location"]
    assert f["field"] == f"profiles.{key_segment(name)}._severity_dedup", f
    assert f"`_profile: {shown_key(name)}`" in f["message"], f
    _no_canary(text)


def test_the_available_profiles_are_listed_through_shown_key(tmp_path):
    """`; available:` 的每個名字只走 `shown_key`，沒有別的加工：名稱形的照印
    （鍵 `123`、`true` 也是——reader 把鍵都當文字交出來），其他的印替身。"""
    receiver = {"receiver": {"type": "webhook", "url": "https://noc.example.com/a"}}
    files = _files({"cpu_usage_percent": "85", "_routing_profile": "nope"},
                   **{"_routing_profiles.yaml": {"routing_profiles": {
                       "p-one": receiver, 123: receiver, True: receiver, "Gold tier": receiver}}})
    findings, _text = validate_run(tmp_path, files)
    [f] = [f for f in findings if f["kind"] == "unknown_routing_profile"]
    assert f["message"].endswith(f"; available: 123, {shown_key('Gold tier')}, p-one, true"), f


def test_the_id_of_a_tenant_body_that_is_not_loaded_is_not_printed(tmp_path):
    """`tenants:` 底下 body 是 null 的那一筆不會被載入；那行 WARN（只在 stderr）
    說是哪個檔、什麼形狀，不印它的 id。"""
    files = _files({"cpu_usage_percent": "85"})
    files["t-a.yaml"]["tenants"][_canary("unloaded")] = None
    _findings, text = validate_run(tmp_path, files)
    assert (f"WARN: t-a.yaml: tenant '{shown_key(_canary('unloaded'))}'" in text
            and " must be a mapping, got NoneType — that tenant is not loaded" in text), text
    _no_canary(text)


def test_the_id_of_a_list_body_is_not_printed_before_the_run_stops(tmp_path):
    """body 是 list 時 exporter 整個檔都不載入，產生器在比對兩邊的租戶後以 rc 2
    停下（不是判決，所以不經 `validate_run`）；停下之前印的那行 WARN 同樣不印 id。"""
    files = _files({"cpu_usage_percent": "85"})
    files["t-a.yaml"]["tenants"][_canary("unloaded")] = [1]
    d = tmp_path / "conf.d"
    d.mkdir()
    for name, body in files.items():
        (d / name).write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
    p = subprocess.run([sys.executable, str(_GAR), "--config-dir", str(d), "--validate", "--strict"],
                       capture_output=True, text=True, encoding="utf-8", timeout=300)
    text = p.stdout + p.stderr
    assert p.returncode == 2 and "Traceback" not in text, (p.returncode, text)
    assert (f"WARN: t-a.yaml: tenant '{shown_key(_canary('unloaded'))}'" in text
            and " must be a mapping, got list — that tenant is not loaded" in text), text
    _no_canary(text)


# ── #3703 決策 A：被遮住的「定位用」名字印成摘要替身，兩個名字是兩筆 finding ──

_STAND_IN = re.compile(r"<key:[0-9a-f]{6}>")
#: Two names that are not name-like and have the same length: what used to
#: print as the same `a string of N chars`.
_TWINS = (_canary("one"), _canary("two"))
assert len(_TWINS[0]) == len(_TWINS[1])


def _key(f: dict) -> tuple:
    """tenant-api's `findingKey` (internal/generator/gate.go): what decides
    whether a finding was there before the write."""
    return tuple(f.get(k) for k in ("kind", "tenant", "policy", "file", "field", "message"))


def _base_and_new(tmp_path: Path, kind: str, base: dict, new: dict) -> tuple[list, list]:
    """The *kind* findings of the base tree and of the tree a write makes; no
    canary in either run."""
    out = []
    for label, files in (("base", base), ("new", new)):
        (tmp_path / label).mkdir()
        findings, text = validate_run(tmp_path / label, files)
        _no_canary(text)
        out.append([f for f in findings if f["kind"] == kind])
    return out[0], out[1]


def _two_findings_one_kept(base: list, new: list) -> None:
    """The new tree's two findings are two keys, each naming its name by a
    stand-in; the base tree's one is still one of them, unchanged."""
    assert len(base) == 1 and len(new) == 2, (base, new)
    assert len({_key(f) for f in new}) == 2, new
    for f in new:
        assert _STAND_IN.search(f["message"]), f
    assert _key(base[0]) in {_key(f) for f in new}, (base, new)


def test_two_unprinted_profile_names_are_two_findings(tmp_path):
    """重現 1：`profiles:` 裡兩個不是名稱形、長度相同的 threshold profile 名字。"""
    body = {"_severity_dedup": "disable"}
    base, new = _base_and_new(
        tmp_path, "routing_in_unread_location",
        _files({"cpu_usage_percent": "85"}, {"profiles": {_TWINS[0]: body}}),
        _files({"cpu_usage_percent": "85"}, {"profiles": {_TWINS[0]: body, _TWINS[1]: body}}))
    _two_findings_one_kept(base, new)
    assert {f["field"] for f in new} == {f"profiles.{key_segment(n)}._severity_dedup" for n in _TWINS}


def test_two_unprinted_duplicate_tenant_ids_are_two_findings(tmp_path):
    """重現 2：`a.yaml`、`b.yaml` 都宣告同一個不合法 id；寫入再讓兩檔都宣告第二個。"""
    t = {"cpu_usage_percent": "85"}
    base_files = _files(t, **{n: {"tenants": {_TWINS[0]: t}} for n in ("a.yaml", "b.yaml")})
    new_files = _files(t, **{n: {"tenants": {_TWINS[0]: t, _TWINS[1]: t}}
                             for n in ("a.yaml", "b.yaml")})
    base, new = _base_and_new(tmp_path, "duplicate_tenant", base_files, new_files)
    _two_findings_one_kept(base, new)
    assert all(f["tenant"] is None for f in new), new


def test_two_unprinted_tenant_ids_in_one_file_are_two_findings(tmp_path):
    """重現 3：同一個檔裡兩個不合法、長度相同的 id——檔名分不開它們，替身分得開。"""
    t = {"cpu_usage_percent": "85"}
    base_files = _files(t, **{"x.yaml": {"tenants": {_TWINS[0]: t}}})
    new_files = _files(t, **{"x.yaml": {"tenants": {_TWINS[0]: t, _TWINS[1]: t}}})
    base, new = _base_and_new(tmp_path, "invalid_tenant_id", base_files, new_files)
    _two_findings_one_kept(base, new)
    assert {f["file"] for f in new} == {"x.yaml"}, new


@pytest.mark.parametrize("files,kind", [
    ({"_routing_profiles.yaml": {"routing_profiles": {
        _C: {"receiver": {"type": "webhook", "url": "https://noc.example.com/a"}}}},
      "sub/_routing_profiles.yaml": {"routing_profiles": {
          _C: {"receiver": {"type": "webhook", "url": "https://noc.example.com/a"}}}}},
     "routing_profile_duplicate"),
    ({"_domain_policy.yaml": {"domain_policies": {_C: {"tenants": 5}}}},
     "domain_policy_unusable"),
], ids=["routing_profile_duplicate", "domain_policy_unusable"])
def test_a_routing_profile_or_domain_policy_name_is_not_printed(tmp_path, files, kind):
    """routing profile 與 domain policy 的名字也是作者寫的、拿來定位的鍵：訊息、
    `field`、`policy` 都只帶替身。重複的 routing profile 讓整棵樹以 rc 2 停下
    （不是判決，所以不經 `validate_run`），findings 檔照寫。"""
    d = tmp_path / "conf.d"
    for name, body in _files({"cpu_usage_percent": "85"}, **files).items():
        (d / name).parent.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
    out = tmp_path / "f.json"
    p = subprocess.run([sys.executable, str(_GAR), "--config-dir", str(d), "--validate", "--strict",
                        "--findings-json", str(out)],
                       capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert p.returncode in (1, 2) and "Traceback" not in p.stderr, (p.returncode, p.stderr)
    written = out.read_text(encoding="utf-8")
    text = p.stdout + p.stderr + written
    [f] = [f for f in json.loads(written)["findings"] if f["kind"] == kind]
    assert shown_key(_C) in f["message"], f
    _no_canary(text)


def test_shown_key_stand_in():
    """替身的規則：名稱形的字串照印；其餘（空字串、非字串的鍵）是其文字
    （非字串取 `str()`）UTF-8 的 SHA-256 前 6 個十六進位字。同名同替身。"""
    def digest(text: str) -> str:
        return f"<key:{hashlib.sha256(text.encode('utf-8')).hexdigest()[:6]}>"

    assert shown_key("p-one") == key_segment("p-one") == "p-one"
    for name in ("Gold tier", "", "x" * 65, "ünï", _C):
        assert shown_key(name) == key_segment(name) == digest(name), name
        assert _STAND_IN.fullmatch(shown_key(name)), name
    assert shown_key("Gold tier") != shown_key("Iron tier")
    assert shown_key(1) == digest("1") and shown_key(True) == digest("True")
    assert shown_key(b"password_file") == digest("b'password_file'")
