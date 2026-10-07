"""`_profile` 寫成 mapping 時，describe_tenant 的答案要與 Go 的 da-guard effective 一致（#2515）。

Go 的讀法（pkg/config `withProfileText` ← `ScheduledValue.UnmarshalYAML`）：

- 寫出 `default:` 鍵的 mapping（`{default: '010'}`，排程值的寫法）：取 default 的文字。
  yaml.v3 把純量解進 Go string 時保留原文（`010` 是 "010"、`1.50` 是 "1.50"），null 是 ""，
  `!!binary` 是解碼後的位元組；其餘鍵不選 profile。
- 沒寫 `default:`、但經 merge key 帶進來（`{<<: {default: x}}`）：ScheduledValue 看到 `<<`，
  走任意 mapping 的分支，以 merged mapping 的 yaml.v3 `Marshal` 文字（`default: x\\n`）當
  profile 名稱。**describe 刻意不鏡像**（減法裁決：不移植 yaml.v3 Marshal）：`_profile`
  留成 generic mapping、不選 profile，並在 stderr 印 WARNING 點名檔案與租戶。fixture 不定義
  以 Marshal 文字命名的 profile，所以兩邊的「值」相同；`_profile` 與 merged_hash 允許不同
  ——見 `MERGE_KEY_SHAPES` 那組已知分歧列。
- 其他 mapping（完全沒有 `default`）、sequence、null：原樣保留，不選 profile。

每格在租戶檔與根平台檔 `tenants:` 兩個位置各量一次，比對三樣東西：effective_config 的
`_profile`、被 profile 改掉的值（mysql_connections）、merged_hash。修正前（main 59e58c81）
describe 對 mapping 一律不綁 profile、`_profile` 原樣留成 dict，除了「沒有 default」與
對照組之外每格都分歧。

`{default: [..]}` / `{default: {..}}` 也是刻意**不**鏡像的一格：yaml.v3 無法把它解成字串，
exporter 拒收整份檔（rc 3）；describe 不鏡像整檔拒收（`_read_profiles` 的先例），本檔把
這個已知分歧釘住，免得哪天被當成「一致」。

da-guard 由 conftest 的 session fixture 以 `go build` 建出；建不起來就 fail、不 skip。
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
# One profile, `010`. No profile is named by a yaml.v3 Marshal text
# (`default: "010"`): the merge-key rows are a known divergence on `_profile`
# and merged_hash only, so their served value must still agree.
_PROFILES = 'profiles:\n  "010":\n    mysql_connections: 10\n'

# (name, `_profile:` as written, the value Go serves for mysql_connections)
SHAPES = [
    ("scalar-CONTROL", "'010'", 10),
    ("map-quoted", "{default: '010'}", 10),
    ("map-bare-010", "{default: 010}", 10),
    ("map-null-default", "{default: ~}", 80),
    ("map-empty-default", "{default: ''}", 80),
    ("map-padded", "{default: ' 010 '}", 10),
    ("map-with-overrides", "{default: '010', overrides: []}", 10),
    ("map-with-expiry", "{default: '010', expires: '2099-01-01T00:00:00Z', reason: x}", 10),
    ("map-binary-default", "{default: !!binary MDEw}", 10),
    ("map-date-default", "{default: 2026-12-31}", 80),
    ("map-bool-default", "{default: true}", 80),
    ("map-float-default", "{default: 1.50}", 80),
    ("map-quoted-key", "{\"default\": '010'}", 10),
    ("map-no-default-CONTROL", "{foo: '010'}", 80),
    ("merge-under-written-default", "{<<: {default: 'zzz'}, default: '010'}", 10),
    ("sequence-CONTROL", "['010']", 80),
    ("null-CONTROL", "~", 80),
]
WHERE = ("tenant-file", "platform-tenants")
# Known divergence (#2515 減法): `default` only through a merge key. Go elects
# the merged mapping's yaml.v3 Marshal text (an unknown profile here); describe
# keeps the generic mapping and elects nothing, and says so on stderr.
MERGE_KEY_SHAPES = [
    ("merge-key", "{<<: {default: '010'}}"),
    ("merge-key-bare", "{<<: {default: 010}}"),
    ("merge-key-plus", "{<<: {default: '010'}, reason: x}"),
    ("merge-key-nested", "{<<: {default: '010', n: [1, {x: yes}], s: 'a: b'}}"),
]


def _tree(root: Path, profile: str, where: str) -> Path:
    conf_d = root / "conf.d"
    conf_d.mkdir()
    (conf_d / "_profiles.yaml").write_text(_PROFILES, encoding="utf-8")
    block = f"tenants:\n  t1:\n    _profile: {profile}\n"
    if where == "tenant-file":
        (conf_d / "_defaults.yaml").write_text(_DEFAULTS, encoding="utf-8")
        (conf_d / "t1.yaml").write_text(block, encoding="utf-8")
    else:
        (conf_d / "_defaults.yaml").write_text(_DEFAULTS + block, encoding="utf-8")
        (conf_d / "t1.yaml").write_text("tenants:\n  t1:\n    container_cpu: 60\n",
                                        encoding="utf-8")
    return conf_d


def _describe(conf_d: Path, *extra: str) -> dict:
    return json.loads(_describe_proc(conf_d, *extra).stdout)


def _describe_proc(conf_d: Path, *extra: str) -> subprocess.CompletedProcess:
    p = subprocess.run([sys.executable, str(DESCRIBE), "-c", str(conf_d), "t1",
                        "--format", "json", *extra],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=120, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert p.returncode == 0, p.stderr
    return p


def _reading(conf_d: Path) -> dict:
    plain = _describe(conf_d)
    sources = _describe(conf_d, "-s")
    return {
        "_profile": plain["effective_config"].get("_profile", "<absent>"),
        "value": plain["effective_config"].get("mysql_connections"),
        "bound": [o["profile"] for o in plain.get("profile_overlay", [])],
        "merged_hash": sources["merged_hash"],
    }


def _oracle(conf_d: Path) -> dict:
    t = tv.load_effective(conf_d)["t1"]
    return {
        "_profile": t.effective_config.get("_profile", "<absent>"),
        "value": t.effective_config.get("mysql_connections"),
        "bound": [t.profile] if t.profile else [],
        "merged_hash": t.merged_hash,
    }


def test_matrix_is_not_vacuous() -> None:
    names = [n for n, _, _ in SHAPES]
    assert len(names) == len(set(names))
    # Both answers occur — bound and unbound — so equal readings mean
    # something, and every Go branch named in the docstring has a row.
    assert {v for _, _, v in SHAPES} == {10, 80}
    for row in ("map-quoted", "map-null-default", "merge-under-written-default",
                "map-no-default-CONTROL", "scalar-CONTROL"):
        assert row in names, row


@pytest.mark.parametrize("where", WHERE)
@pytest.mark.parametrize("name,profile,served", SHAPES, ids=[s[0] for s in SHAPES])
def test_describe_reads_profile_as_go_does(tmp_path, name, profile, served, where):
    conf_d = _tree(tmp_path, profile, where)
    oracle = _oracle(conf_d)
    assert oracle["value"] == served, (name, oracle)   # 前提：Go 的讀法如 docstring 所述
    got = _reading(conf_d)
    assert got == oracle, (name, where)


def test_the_comparison_can_ring(tmp_path):
    """必響對照組：同一個比對，拿 mapping 那格的 describe 對「沒寫 `_profile`」的樹的 Go
    答案，必須判為不同——證明上面的相等不是比對本身量不到差異。"""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    mapped = _tree(tmp_path / "a", "{default: '010'}", "tenant-file")
    unbound = _tree(tmp_path / "b", "~", "tenant-file")
    assert _reading(mapped) != _oracle(unbound)


@pytest.mark.parametrize("profile", ["{default: ['010']}", "{default: {a: 1}}"])
def test_a_non_scalar_default_is_a_known_divergence(tmp_path, profile):
    """exporter 拒收整份檔；describe 不鏡像整檔拒收，`_profile` 留成 mapping、不選 profile。"""
    conf_d = _tree(tmp_path, profile, "tenant-file")
    with pytest.raises(tv.ParseFailedError):
        tv.load_effective(conf_d)
    got = _reading(conf_d)
    assert isinstance(got["_profile"], dict) and got["bound"] == [] and got["value"] == 80, got


@pytest.mark.parametrize("where", WHERE)
@pytest.mark.parametrize("name,profile", MERGE_KEY_SHAPES, ids=[s[0] for s in MERGE_KEY_SHAPES])
def test_merge_key_only_default_is_a_named_known_divergence(tmp_path, name, profile, where):
    """已知分歧列：值與 Go 相同（兩邊都沒綁 profile）；`_profile` 與 merged_hash 允許不同
    （Go 的 `_profile` 是 Marshal 文字，describe 是 generic mapping，不斷言相等）；stderr 有
    WARNING，點名檔案與租戶，每個檔、每個租戶只印一次。"""
    conf_d = _tree(tmp_path, profile, where)
    oracle = _oracle(conf_d)
    assert isinstance(oracle["_profile"], str) and oracle["_profile"].startswith("default: ")
    assert oracle["bound"] == []                         # 前提：Marshal 文字不是任何 profile
    got = _reading(conf_d)
    assert got["value"] == oracle["value"] == 80, got
    assert got["bound"] == [] and isinstance(got["_profile"], dict), got
    assert "default" in got["_profile"], got
    err = _describe_proc(conf_d, "-s").stderr
    fname = "t1.yaml" if where == "tenant-file" else "_defaults.yaml"
    lines = [l for l in err.splitlines() if "only through a merge key" in l]
    assert len(lines) == 1, err
    assert lines[0].startswith("WARNING: ") and fname in lines[0] and "tenant 't1'" in lines[0]
    assert "merged_hash differ" in lines[0], lines[0]


def test_written_default_and_plain_mappings_do_not_warn(tmp_path):
    """必不響：直接寫出 `default:`（含 merge 下的直接寫出）與沒有 default 的 mapping 不印 WARNING。"""
    for i, profile in enumerate(["{default: '010'}", "{<<: {default: 'zzz'}, default: '010'}",
                                 "{foo: '010'}"]):
        (tmp_path / str(i)).mkdir()
        err = _describe_proc(_tree(tmp_path / str(i), profile, "tenant-file")).stderr
        assert "merge key" not in err, (profile, err)
