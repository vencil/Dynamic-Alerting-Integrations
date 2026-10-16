"""#2914：閾值 profile 裡的 `_routing`／`_severity_dedup`，exporter 與路由產生器答案一致。

重現樹取自 #2914：租戶以 `_profile` 綁定 `_profiles.yaml` 的 profile，profile 寫了
`_routing`（一份）或 `_severity_dedup`（另一份）。修正前 exporter（`da-guard
served-values`）把它們填進租戶，產生器不讀閾值 profile，兩邊答案不同；修正後
exporter 也不套用。

每棵樹都在同一支測試裡同時問兩邊，斷言答案相等：

- exporter：`_lib_tenant_values.load_served_values` 的 `routing`（exporter module 的
  路由 resolver 讀到的 `_routing`：有沒有、receiver 是什麼）與 `values["_severity_dedup"]`。
- 產生器：`_grar_parse.load_tenant_tree` 的 `routing_configs`（有沒有、receiver）與
  `dedup_configs`。

對照組把同一個值寫在租戶自己的檔案：兩邊都套用，答案仍相等，而且是「有」——
證明這支測試分得出「兩邊都沒有」與「兩邊都有」，不是恆真。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools" / "ops"))
import _lib_tenant_values as tv  # noqa: E402
from _grar_parse import load_tenant_tree  # noqa: E402

pytestmark = pytest.mark.usefixtures("da_guard_env")

_RECEIVER = "receiver: {type: webhook, url: \"https://h.example/profile\"}"
_DEFAULTS = "defaults:\n  container_cpu: 80\n"

TREES = {
    # #2914 的重現：profile 帶 `_routing`。
    "profile-routing": ({
        "_defaults.yaml": _DEFAULTS,
        "_profiles.yaml": f"profiles:\n  std:\n    container_cpu: 55\n    _routing: {{{_RECEIVER}}}\n",
        "tx.yaml": "tenants:\n  tx:\n    _profile: std\n",
    }, False),
    # 同一個問題的 `_severity_dedup` 版。
    "profile-severity-dedup": ({
        "_defaults.yaml": _DEFAULTS,
        "_profiles.yaml": "profiles:\n  std:\n    container_cpu: 55\n    _severity_dedup: disable\n",
        "tx.yaml": "tenants:\n  tx:\n    _profile: std\n",
    }, False),
    # 對照組：寫在租戶自己的條目，兩邊都讀。
    "control-tenant-own-entry": ({
        "_defaults.yaml": _DEFAULTS,
        "_profiles.yaml": "profiles:\n  std:\n    container_cpu: 55\n",
        "tx.yaml": f"tenants:\n  tx:\n    _profile: std\n    _severity_dedup: disable\n    _routing: {{{_RECEIVER}}}\n",
    }, True),
}


def _build(root: Path, files: dict[str, str]) -> Path:
    conf_d = root / "conf.d"
    for rel, body in files.items():
        p = conf_d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return conf_d


def _receiver(routing) -> dict | None:
    if not isinstance(routing, dict):
        return None
    recv = routing.get("receiver")
    return {k: recv[k] for k in ("type", "url") if k in recv} if isinstance(recv, dict) else None


@pytest.mark.parametrize("name", sorted(TREES))
def test_exporter_and_generator_agree(name: str, tmp_path: Path) -> None:
    files, applied = TREES[name]
    conf_d = _build(tmp_path, files)

    served = tv.load_served_values(conf_d)["tx"]
    exporter = served.values
    generator = load_tenant_tree(str(conf_d))

    # 閾值這一側本來就一致（profile 的 55 兩邊都套用），先釘住前提。
    assert exporter["container_cpu"] == 55, exporter

    exp_dedup = exporter["_severity_dedup"]
    gen_dedup = generator.dedup_configs["tx"]
    assert exp_dedup == gen_dedup, (name, exp_dedup, gen_dedup)

    exp_recv = _receiver(served.routing)
    gen_recv = _receiver(generator.routing_configs.get("tx"))
    assert exp_recv == gen_recv, (name, exp_recv, gen_recv)

    # 不是恆真：對照組兩邊都「有」，profile 那兩棵兩邊都「沒有」。
    if applied:
        assert gen_dedup == "disable" and gen_recv == {"type": "webhook", "url": "https://h.example/profile"}
    else:
        assert gen_dedup == "enable" and gen_recv is None
