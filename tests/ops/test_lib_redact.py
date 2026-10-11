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
