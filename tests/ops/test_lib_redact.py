"""_lib_redact：報告裡哪些欄位是憑證、怎麼印（#1516）。

規則跟 Alertmanager 一樣以欄位是什麼決定（它把這些欄位定型為 Secret／SecretURL），
有變的憑證照報「有變」、前後值都換成 REDACTED（kubectl diff 的 `*** (before)/(after)` 同理）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import _lib_redact as r
import config_diff as cd
import _render_diff as rd


REPO = Path(__file__).resolve().parents[2]


def test_the_field_list_is_tenant_apis_credmask():
    """與 tenant-api 的 credmask.credentialKeys 同一份：任一邊加減欄位而另一邊沒跟上就紅。"""
    src = (REPO / "components/tenant-api/internal/credmask/credmask.go").read_text(encoding="utf-8")
    block = re.search(r"var credentialKeys = map\[string\]bool\{(.*?)\n\}", src, re.S)
    assert block, "credmask.go no longer declares credentialKeys as a map literal"
    go_keys = set(re.findall(r'"([^"]+)":\s*true', block.group(1)))
    assert go_keys and go_keys == set(r.SECRET_FIELDS), go_keys ^ set(r.SECRET_FIELDS)


@pytest.mark.parametrize("field", ["credentials", "client_secret", "api_key", "bot_token",
                                   "url", "webhook_url", "routing_key", "password", "URL", "Api_Url"])
def test_alertmanager_secret_fields_are_redacted(field):
    old, new = r.redact_pair(field, "S1", "S2")
    assert (old, new) == (r.REDACTED, r.REDACTED)


def test_containers_are_redacted_whole():
    """http_config／tls_config／header map 整段遮：裡面藏憑證的位置多到列不完。"""
    receiver = {"http_config": {"authorization": {"type": "Bearer", "credentials": "S"},
                                "oauth2": {"client_id": "id", "client_secret": "S"}},
                "url": "https://h/S", "send_resolved": True}
    got = r.redact(receiver)
    assert got == {"http_config": r.REDACTED, "url": r.REDACTED, "send_resolved": True}


@pytest.mark.parametrize("field", ["runbook_url", "group_wait", "to", "channel", "client_id", "type"])
def test_non_credentials_print_as_written(field):
    assert r.redact_pair(field, "a", "b") == ("a", "b")


def test_added_or_removed_credential_still_reads_as_added_or_removed():
    assert r.redact_pair("url", None, "S") == (None, r.REDACTED)
    assert r.redact_pair("url", "S", None) == (r.REDACTED, None)


def test_config_diff_setting_change_uses_the_shared_rule():
    old = {"t": {"_routing": {"receiver": {"http_config": {"authorization": {"credentials": "S1"}}}}}}
    new = {"t": {"_routing": {"receiver": {"http_config": {"authorization": {"credentials": "S2"}}}}}}
    diff = cd.compute_setting_diff(old, new)
    assert "S1" not in repr(diff) and "S2" not in repr(diff)
    assert [(e["old"], e["new"]) for e in diff["t"]] == [(r.REDACTED, r.REDACTED)]


def test_render_diff_receiver_change_uses_the_shared_rule():
    def cfg(secret, runbook):
        return {"route": {"receiver": "x"}, "receivers": [{"name": "x", "webhook_configs": [{
            "url": "https://h/u", "http_config": {"oauth2": {"client_secret": secret}}}],
            "annotations": {"runbook_url": runbook}}]}
    (rc,) = [c for c in rd.diff_routes(cfg("S1", "a"), cfg("S2", "b")) if isinstance(c, rd.ReceiverChange)]
    assert rc.changes["webhook_configs.0.http_config.oauth2.client_secret"] == (r.REDACTED, r.REDACTED)
    assert "S1" not in repr(rc) and "S2" not in repr(rc)
    assert rc.changes["annotations.runbook_url"] == ("a", "b")  # 對照：名字含 url 但不是憑證
    assert rd.MASKED == r.REDACTED


def test_config_diff_does_not_descend_into_a_credential_container():
    """兩側都是 mapping 時 _mapping_changes 會往下走；憑證容器不得往下走，否則
    `http_headers.X-Token.values` 這種葉子（鍵名本身不像憑證）就會印出原值。"""
    def body(tok):
        return {"t": {"_routing": {"receiver": {"type": "webhook", "url": "u", "http_config": {
            "http_headers": {"X-Token": {"values": [tok]}}}}}}}
    diff = cd.compute_setting_diff(body("S1"), body("S2"))
    assert "S1" not in repr(diff) and "S2" not in repr(diff), diff
    assert [e["key"] for e in diff["t"]] == ["_routing.receiver.http_config"]


def test_a_receiver_carried_by_a_profile_is_redacted(tmp_path, da_guard_env):
    """`_profiles.yaml` 的 profile 可以帶 receiver：經 profile 改到租戶的憑證，同一條規則遮。
    profile 差異不再單獨比對，而是看綁到它的租戶實際讀到什麼（ADR-037）。"""
    import json
    import subprocess
    import sys

    def tree(d, secret, cpu):
        d.mkdir()
        (d / "_defaults.yaml").write_text("defaults:\n  container_cpu: 80\n", encoding="utf-8")
        (d / "_profiles.yaml").write_text(
            f"profiles:\n  std:\n    container_cpu: {cpu}\n    _routing:\n      receiver:\n"
            f"        type: webhook\n        url: https://h/{secret}\n", encoding="utf-8")
        (d / "tx.yaml").write_text("tenants:\n  tx:\n    _profile: std\n", encoding="utf-8")
        return d
    old, new = tree(tmp_path / "old", "S1", 55), tree(tmp_path / "new", "S2", 60)
    p = subprocess.run([sys.executable, cd.__file__, "--old-dir", str(old), "--new-dir", str(new),
                        "--at", "2026-10-10T00:00:00Z", "--format", "json"],
                       capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert p.returncode == 1, p.stderr
    assert "S1" not in p.stdout + p.stderr and "S2" not in p.stdout + p.stderr
    doc = json.loads(p.stdout)
    assert {"key": "_routing.receiver.url", "old": r.REDACTED, "new": r.REDACTED,
            "change": "modified", "tenants": ["tx"]} in doc["setting_changes"]
    # 對照：同一個 profile 裡不是憑證的值照印
    assert [(c["key"], c["old"], c["new"]) for c in doc["threshold_changes"]] == [("container_cpu", 55, 60)]


# ── 以值遮罩（#2865）：憑證經 YAML alias 或手抄出現在非憑證欄位 ──────────────


def test_credential_values_takes_what_credential_fields_hold():
    doc = {"receiver": {"type": "webhook", "url": "https://h/S1", "send_resolved": True,
                        "http_config": {"authorization": {"credentials": "S2"}, "follow_redirects": True},
                        "headers": {"X-Token": ["S3", " "]}},
           "owner": "https://h/S1", "group_wait": "30s", "token": 42, "limit": 7,
           "list": [{"api_key": "S4"}], "_routing_text": "type: webhook\nurl: https://h/S5\n"}
    # 數字以文字收（`service_key: 1234…` 也是憑證）；布林、非憑證欄位的數字不收
    assert r.credential_values(doc) == {"https://h/S1", "S2", "S3", "S4", "42"}
    # 路由鍵寫成文字時整段收（diff_served 也整段遮）
    got = r.credential_values(doc, text_key=lambda k: k.startswith("_routing"))
    assert got == {"https://h/S1", "S2", "S3", "S4", "42", "type: webhook\nurl: https://h/S5\n"}
    # 路由鍵是 mapping 時只收其中憑證欄位的值，`type` 這類不收
    assert r.credential_values({"_routing": {"type": "webhook"}},
                               text_key=lambda k: k.startswith("_routing")) == set()


def test_mask_values_masks_inside_strings_and_keys_and_counts():
    Pair = rd.ValueChange
    value = {"a": "see https://h/S1 now", "https://h/S1": [Pair("t", "f", "k", "S2", 3)],
             "n": 7, "b": True, "none": None}
    masked, n = r.mask_values(value, {"https://h/S1", "S2"})
    assert masked == {"a": f"see {r.REDACTED} now", r.REDACTED: [Pair("t", "f", "k", r.REDACTED, 3)],
                      "n": 7, "b": True, "none": None}
    assert type(masked[r.REDACTED][0]) is Pair
    assert n == 3
    assert r.mask_values(value, set()) == (value, 0)


def test_mask_values_is_one_pass_longest_first():
    # 較長的先遮（含另一個秘密者整段遮）；放進去的 REDACTED 不會再被比對
    masked, n = r.mask_values(["xS1y", "S1", "dact"], {"S1", "xS1y", "dact"})
    assert masked == [r.REDACTED] * 3 and n == 3
    masked, _ = r.mask_values(["S1"], {"S1", "dact"})
    assert masked == [r.REDACTED]


def test_render_diff_credentials_reads_both_renders():
    cfg = {"route": {"receiver": "tenant-tx"}, "receivers": [{"name": "tenant-tx", "webhook_configs": [
        {"url": "https://h/S1", "send_resolved": True}]}]}
    assert rd.credentials(cfg) == {"https://h/S1"}
    tvs = {"tx": r_tenant(values={"_routing": {"receiver": {"type": "webhook", "url": "https://h/S2"}},
                                  "_metadata": {"owner": "https://h/S2"}},
                          unserved={"_routing": "url: https://h/S3"})}
    assert rd.credentials(tvs) == {"https://h/S2", "url: https://h/S3"}
    # `_routing_profile` 是 profile 名稱，不是 receiver：不收，否則名稱出現處全被遮
    named = {"tx": r_tenant(values={"_routing_profile": "cpu"}, unserved={"_routing_profile": "cpu"})}
    assert rd.credentials(named) == set()


def r_tenant(**fields):
    import _lib_tenant_values as tv
    base = {f: {} for f in tv.TenantValues._fields}
    base.update(fields)
    return tv.TenantValues(**base)


def _tree(d, secret, cpu, *, alias):
    d.mkdir()
    url = f"https://hooks.example/T0/{secret}"
    owner = "*u" if alias else url
    (d / "tx.yaml").write_text(
        "tenants:\n  tx:\n"
        f"    container_cpu: {cpu}\n"
        f"    _routing:\n      receiver: {{type: webhook, url: &u {url}}}\n"
        f"    _metadata:\n      owner: {owner}\n", encoding="utf-8")
    return d


def _run(old, new, *extra):
    import subprocess
    import sys
    return subprocess.run([sys.executable, cd.__file__, "--old-dir", str(old), "--new-dir", str(new),
                           "--at", "2026-10-10T00:00:00Z", *extra],
                          capture_output=True, text=True, encoding="utf-8", timeout=300)


@pytest.mark.parametrize("alias", [True, False], ids=["yaml-alias", "copied-by-hand"])
def test_a_credential_in_a_non_credential_field_is_masked_by_value(tmp_path, da_guard_env, alias):
    """`_metadata.owner` 不是憑證欄位，exporter 照送；它的值與 receiver 的 url 相同時，
    依值遮（#2865）。對照：同一棵樹不是憑證的閾值變更照印。"""
    import json

    old = _tree(tmp_path / "old", "SEKRETOLD", 80, alias=alias)
    new = _tree(tmp_path / "new", "SEKRETNEW", 90, alias=alias)
    js, md = _run(old, new, "--format", "json"), _run(old, new)
    for p in (js, md):
        assert p.returncode == 1, p.stderr
        assert "SEKRET" not in p.stdout + p.stderr, p.stdout
    doc = json.loads(js.stdout)
    assert {"key": "_metadata.owner", "old": r.REDACTED, "new": r.REDACTED,
            "change": "modified", "tenants": ["tx"]} in doc["setting_changes"]
    assert doc["masked_by_value"] == 2
    assert "Masked by value: 2 place(s)" in md.stdout
    # 對照：不是憑證的值照印
    assert [(c["key"], c["old"], c["new"]) for c in doc["threshold_changes"]] == [("container_cpu", "80", "90")]


def test_nothing_masked_by_value_says_nothing(tmp_path, da_guard_env):
    """對照組：owner 與 url 不同時不遮、不註記，且 owner 的變更照印。"""
    import json

    def tree(d, owner):
        d.mkdir()
        (d / "tx.yaml").write_text(
            "tenants:\n  tx:\n    _routing:\n      receiver: {type: webhook, url: https://h/S1}\n"
            f"    _metadata:\n      owner: {owner}\n", encoding="utf-8")
        return d
    old, new = tree(tmp_path / "old", "alice"), tree(tmp_path / "new", "bob")
    js, md = _run(old, new, "--format", "json"), _run(old, new)
    doc = json.loads(js.stdout)
    assert doc["masked_by_value"] == 0
    assert {"key": "_metadata.owner", "old": "alice", "new": "bob",
            "change": "modified", "tenants": ["tx"]} in doc["setting_changes"]
    assert "Masked by value" not in md.stdout


def test_compare_collects_credentials_from_both_sides_and_both_views(monkeypatch):
    """每一側、每個面向渲染成功就收；一側失敗不影響另一側已收的。"""
    monkeypatch.setattr(rd, "has_config", lambda d: True)
    served = {"base": {"tx": r_tenant(values={"_routing": {"receiver": {"url": "SB"}}})},
              "pr": {"tx": r_tenant(values={"_routing": {"receiver": {"url": "SP"}}})}}
    routes = {"base": {"route": {}, "receivers": [{"name": "x", "webhook_configs": [{"url": "RB"}]}]},
              "pr": {"route": {}, "receivers": [{"name": "x", "webhook_configs": [{"url": "RP"}]}]}}
    monkeypatch.setattr(rd, "render_served", lambda d, at, **kw: served[d])
    monkeypatch.setattr(rd, "render_routes", lambda d, **kw: routes[d])
    assert rd.compare("base", "pr", "2026-10-10T00:00:00Z").secrets == {"SB", "SP", "RB", "RP"}

    def broken(d, **kw):
        if d == "base":
            raise rd.RenderError(rd.RenderFailure("config", "bad"))
        return routes[d]
    monkeypatch.setattr(rd, "render_routes", broken)
    got = rd.compare("base", "pr", "2026-10-10T00:00:00Z")
    assert got.routes.status == "not_compared"
    assert got.secrets == {"SB", "SP", "RP"}



def test_secrets_never_print():
    """收集到的值放在 RenderDiff 上：repr／str 不得帶出原文（log、traceback）。"""
    got = rd.RenderDiff("t", rd.ViewResult("computed", ""), rd.ViewResult("computed", ""),
                        r.Secrets({"https://h/S1"}))
    assert "S1" not in repr(got) and "S1" not in str(got.secrets)
    assert got.secrets == {"https://h/S1"}


def test_a_short_value_is_masked_only_whole():
    """太短的值當子字串遮會把時間戳、路徑、閾值的數字全遮掉；整值相同才遮。"""
    masked, n = r.mask_values(["0", "2026-10-10T00:00:00Z", 0, "80", 80.5],
                              {"0", "80"})
    assert masked == [r.REDACTED, "2026-10-10T00:00:00Z", r.REDACTED, r.REDACTED, 80.5]
    assert n == 3
    long_ = "x" * r.MIN_SUBSTRING_SECRET
    # 門檻上：夠長的才在較長字串裡遮；短一個字元的只整值遮
    short = long_[1:]
    assert r.mask_values([f"a{long_}b", f"y{short}z", short], {long_, short})[0] == [
        f"a{r.REDACTED}b", f"y{short}z", r.REDACTED]


def test_keys_masked_alike_are_not_merged():
    """兩個鍵遮成同一個 REDACTED 時不得合併，否則一筆變更靜默消失。"""
    masked, n = r.mask_values({"team-SECRET-AAAA": [1], "team-SECRET-BBBB": [2], "x": [3]},
                              {"team-SECRET-AAAA", "team-SECRET-BBBB"})
    assert sorted(masked.values()) == [[1], [2], [3]]
    assert set(masked) == {r.REDACTED, f"{r.REDACTED} #2", "x"} and n == 2


def test_a_numeric_credential_in_a_non_credential_field_is_masked(tmp_path, da_guard_env):
    """數字型憑證（PagerDuty `service_key`）經 alias 放到 `_metadata.owner`：同樣依值遮。"""
    import json

    def tree(d, key, cpu):
        d.mkdir()
        (d / "tx.yaml").write_text(
            "tenants:\n  tx:\n"
            f"    container_cpu: {cpu}\n"
            f"    _routing:\n      receiver: {{type: pagerduty, service_key: &k {key}}}\n"
            "    _metadata:\n      owner: *k\n", encoding="utf-8")
        return d
    old, new = tree(tmp_path / "old", 98765432109876, 80), tree(tmp_path / "new", 98765432101111, 90)
    js = _run(old, new, "--format", "json")
    assert js.returncode == 1, js.stderr
    assert "9876543210" not in js.stdout + js.stderr, js.stdout
    doc = json.loads(js.stdout)
    assert doc["masked_by_value"] == 2
    assert [(c["key"], c["old"], c["new"]) for c in doc["threshold_changes"]] == [("container_cpu", "80", "90")]
