"""describe_tenant 遇到不是 mapping 的租戶 body：與 Go 一致、整檔具名拒收（#2115）。

Go（exporter／da-guard）把 `tenants:` 解進 `map[string]map[string]ScheduledValue`；
任一租戶 body 是 list 或純量，整份檔解不出來：served-values 把檔列進 parse_failed、
**同檔的其他租戶也不服務**，effective rc 3。修正前（main 3ed61d7e）describe 的兩個形狀：

1. 樹內租戶檔 `tenants: {tx: [a]}`：一般 `describe tx` rc 1 + AttributeError traceback；
   同檔的 `describe ty` rc 0（Go 不服務 ty）。現在整檔跳過、stderr 具名，兩者都是
   「找不到租戶」rc 2——與既有「租戶檔 parse 不了就跳過」同一條路。
2. `--what-if <副本> --replaces <根平台檔>`，副本 `tenants: {t1: [a]}`：rc 0、
   body 被 `platform_blocks` 靜默丟掉（t1 的平台值當成被拿掉）。現在 rc 2、點名副本與租戶。
   `--replaces` 自己的租戶檔（#2755）原本只看 `t1` 的 body，現在同檔任一租戶都算。

oracle 是 Go：每格把同一份內容寫進樹裡、讓 da-guard 讀，拒收與否跟著它走；
mapping body 是必響的對照組。da-guard 由 conftest 以 `go build` 建出；建不起來就 fail。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import _lib_tenant_values as tv

REPO_ROOT = Path(__file__).resolve().parents[2]
DESCRIBE = REPO_ROOT / "scripts" / "tools" / "dx" / "describe_tenant.py"

pytestmark = pytest.mark.usefixtures("da_guard_env")

_DEFAULTS = "defaults:\n  mysql_connections: 80\n"
_PROFILES = "profiles:\n  p1:\n    mysql_connections: 20\n"
# (id, the body as written, whether Go decodes the file)
BODIES = [
    ("list", "[a]", False),
    ("str", "a", False),
    ("int", "7", False),
    ("mapping-CONTROL", "{mysql_connections: 9}", True),
    ("null-CONTROL", "~", True),
]


def _write(conf_d: Path, files: dict) -> Path:
    for rel, body in files.items():
        p = conf_d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return conf_d


def _go_decodes(conf_d: Path) -> bool:
    try:
        tv.load_effective(conf_d)
    except tv.ParseFailedError:
        return False
    return True


def _served_tenants(conf_d: Path) -> set:
    """The tenants `da-guard served-values` serves — its JSON is written on
    exit 3 too (`_lib_tenant_values` raises there instead)."""
    p = subprocess.run([os.environ["DA_GUARD_BINARY"], "served-values", "--config-dir", str(conf_d)],
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert p.returncode in (0, 3), p.stderr
    return set(json.loads(p.stdout)["tenants"])


def _describe(conf_d: Path, tid: str, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(DESCRIBE), tid, "--conf-d", str(conf_d), "--format", "json", *extra],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"})


def test_matrix_is_not_vacuous():
    assert {b[2] for b in BODIES} == {True, False}


@pytest.mark.parametrize("name,body,decodes", BODIES, ids=[b[0] for b in BODIES])
@pytest.mark.parametrize("tid", ["tx", "ty"], ids=["the-bad-tenant", "its-neighbour"])
def test_tree_tenant_file_with_a_non_mapping_body(tmp_path, name, body, decodes, tid):
    """形狀 1：樹內租戶檔。Go 拒收整檔時，describe 也不描述檔內任何租戶（rc 2、不出 traceback、
    stderr 點名檔案與租戶）；Go 讀得了時照常描述（rc 0）。"""
    conf_d = _write(tmp_path / "conf.d", {
        "_defaults.yaml": _DEFAULTS,
        "tx.yaml": f"tenants:\n  tx: {body}\n  ty: {{}}\n",
        "tz.yaml": "tenants:\n  tz: {}\n",
    })
    assert _go_decodes(conf_d) is decodes, "the oracle no longer reads this shape as expected"
    p = _describe(conf_d, tid)
    assert "Traceback" not in p.stderr, p.stderr
    if decodes:
        assert p.returncode == 0, p.stderr
        assert json.loads(p.stdout)["tenant_id"] == tid
        return
    # Go serves neither tenant of that file (the oracle's own words).
    assert _served_tenants(conf_d) == {"tz"}
    assert p.returncode == 2, (p.returncode, p.stdout, p.stderr)
    assert p.stdout == "", p.stdout
    assert f"skipped {conf_d / 'tx.yaml'}" in p.stderr, p.stderr
    assert "tenant 'tx' is a" in p.stderr and "not a mapping" in p.stderr, p.stderr
    assert f"Tenant '{tid}' not found" in p.stderr, p.stderr


@pytest.mark.parametrize("name,body,decodes", BODIES, ids=[b[0] for b in BODIES])
def test_tree_tenant_file_skip_leaves_the_rest_of_the_tree(tmp_path, name, body, decodes):
    """整檔跳過只拿掉那一檔：其他檔的租戶照常描述（`--all`）。"""
    conf_d = _write(tmp_path / "conf.d", {
        "_defaults.yaml": _DEFAULTS,
        "tx.yaml": f"tenants:\n  tx: {body}\n  ty: {{}}\n",
        "tz.yaml": "tenants:\n  tz: {}\n",
    })
    p = subprocess.run([sys.executable, str(DESCRIBE), "--all", "--conf-d", str(conf_d)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=120)
    assert p.returncode == 0, p.stderr
    assert set(json.loads(p.stdout)) == ({"tx", "ty", "tz"} if decodes else {"tz"})


@pytest.mark.parametrize("name,body,decodes", BODIES, ids=[b[0] for b in BODIES])
@pytest.mark.parametrize("platform,base", [("_defaults.yaml", _DEFAULTS), ("_profiles.yaml", _PROFILES)],
                         ids=["defaults", "profiles"])
def test_what_if_standing_in_for_a_root_platform_file(tmp_path, name, body, decodes, platform, base):
    """形狀 2：`--replaces` 根平台檔，副本的 `tenants:` 有非 mapping body。"""
    tree = {"_defaults.yaml": _DEFAULTS, "_profiles.yaml": _PROFILES,
            "t1.yaml": "tenants:\n  t1: {}\n"}
    tree[platform] = base + "tenants:\n  t1: {mysql_connections: 7}\n"
    conf_d = _write(tmp_path / "conf.d", tree)
    new = base + f"tenants:\n  t1: {body}\n"
    copy = tmp_path / "copy.yaml"
    copy.write_text(new, encoding="utf-8")
    substituted = _write(tmp_path / "substituted", {**tree, platform: new})
    assert _go_decodes(substituted) is decodes, "the oracle no longer reads this shape as expected"

    p = _describe(conf_d, "t1", "--what-if", str(copy), "--replaces", str(conf_d / platform))
    assert "Traceback" not in p.stderr, p.stderr
    if decodes:
        assert p.returncode == 0, p.stderr
        got = json.loads(p.stdout)
        assert got["what_if_merged_hash"] == tv.load_effective(substituted)["t1"].merged_hash, got
        return
    assert p.returncode == 2, (p.returncode, p.stdout, p.stderr)
    assert p.stdout == "", p.stdout
    assert f"--what-if file {copy.resolve()} has an unsupported shape" in p.stderr, p.stderr
    assert "tenant 't1' is a" in p.stderr and "not a mapping" in p.stderr, p.stderr


@pytest.mark.parametrize("name,body,decodes", BODIES, ids=[b[0] for b in BODIES])
def test_what_if_own_tenant_file_with_another_tenants_bad_body(tmp_path, name, body, decodes):
    """#2755 只看被描述租戶自己的 body；同檔另一個租戶的 body 不是 mapping 時，Go 一樣
    整檔拒收，t1 也不服務。"""
    tree = {"_defaults.yaml": _DEFAULTS, "t1.yaml": "tenants:\n  t1: {}\n  t9: {}\n"}
    conf_d = _write(tmp_path / "conf.d", tree)
    new = f"tenants:\n  t1: {{mysql_connections: 5}}\n  t9: {body}\n"
    copy = tmp_path / "copy.yaml"
    copy.write_text(new, encoding="utf-8")
    substituted = _write(tmp_path / "substituted", {**tree, "t1.yaml": new})
    assert _go_decodes(substituted) is decodes, "the oracle no longer reads this shape as expected"

    p = _describe(conf_d, "t1", "--what-if", str(copy), "--replaces", str(conf_d / "t1.yaml"))
    assert "Traceback" not in p.stderr, p.stderr
    if decodes:
        assert p.returncode == 0, p.stderr
        got = json.loads(p.stdout)
        assert got["what_if_merged_hash"] == tv.load_effective(substituted)["t1"].merged_hash, got
        return
    assert p.returncode == 2, (p.returncode, p.stdout, p.stderr)
    assert "tenant 't9' is a" in p.stderr and "not a mapping" in p.stderr, p.stderr
