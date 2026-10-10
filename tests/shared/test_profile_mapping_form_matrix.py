"""`_profile` 寫成 mapping 時，describe_tenant 的答案要與 Go 的 da-guard effective 一致（#2515）。

Go 的讀法（pkg/config `withProfileText` ← `ScheduledValue.UnmarshalYAML`）：

- 寫出 `default:` 鍵的 mapping（`{default: '010'}`，排程值的寫法）：取 default 的文字。
  yaml.v3 把純量解進 Go string 時保留原文（`010` 是 "010"、`1.50` 是 "1.50"），null 是 ""，
  `!!binary` 是解碼後的位元組；其餘鍵不選 profile。
- 沒寫 `default:`、但經 merge key 帶進來（`{<<: {default: x}}`）：ScheduledValue 看到 `<<`，
  走任意 mapping 的分支，以 merged mapping 的 yaml.v3 `Marshal` 文字（`default: x\\n`）當
  profile 名稱。**describe 刻意不鏡像**（減法裁決：不移植 yaml.v3 Marshal）：`_profile`
  留成 generic mapping、不選 profile，並在 stderr 印 WARNING 點名檔案與租戶。fixture 不定義
  以 Marshal 文字命名的 profile，所以兩邊的「值」相同；`_profile` 允許不同（merged_hash
  自 #1549 起是 describe 從 da-guard 讀來的，兩邊相同）——見 `MERGE_KEY_SHAPES` 那組已知分歧列。
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
import config_diff
from _platform_fs import symlink_or_skip

REPO_ROOT = Path(__file__).resolve().parents[2]
DESCRIBE = REPO_ROOT / "scripts" / "tools" / "dx" / "describe_tenant.py"

pytestmark = pytest.mark.usefixtures("da_guard_env")

_DEFAULTS = "defaults:\n  mysql_connections: 80\n"
# One profile, `010`. No profile is named by a yaml.v3 Marshal text
# (`default: "010"`): the merge-key rows are a known divergence on `_profile`
# only (merged_hash is read from da-guard since #1549), so their served value
# must still agree.
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
    # control for the alias row: an ANCHORED (written) `default` key counts.
    ("anchored-written-key", "{&k default : '010'}", 10),
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
    # #2515 review F2: an ALIAS key is not a written `default` to yaml.v3
    # (it compares the alias node's own Value) — same arbitrary-mapping branch.
    ("alias-key", "{a: &k default, *k : '010'}"),
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


@pytest.mark.parametrize("where", WHERE)
@pytest.mark.parametrize("name,profile,served",
                         SHAPES + [(n, p, 80) for n, p in MERGE_KEY_SHAPES],
                         ids=[s[0] for s in SHAPES] + [s[0] for s in MERGE_KEY_SHAPES])
def test_config_diff_blast_radius_is_the_tenants_go_binds(tmp_path, name, profile, served, where):
    """config_diff 的 profile 爆炸半徑（`affected_tenants`）要是 Go 綁到該 profile 的租戶（#2115）。

    修正前 config_diff 只認字串 `_profile`：`{default: '010'}` 這類 Go 綁 `010` 的寫法列成 `[]`；
    根平台檔 `tenants:` 裡寫的 `_profile` 也完全看不到。oracle 是 Go 服務的值（10 = 綁到
    `010`，80 = 沒綁）——與 config_diff 現在問 da-guard 的那個欄位無關，所以不是套套邏輯。"""
    (tmp_path / "old").mkdir()
    (tmp_path / "new").mkdir()
    old = _tree(tmp_path / "old", profile, where)
    (old / "_profiles.yaml").write_text('profiles:\n  "010":\n    mysql_connections: 20\n',
                                        encoding="utf-8")
    new = _tree(tmp_path / "new", profile, where)
    assert _oracle(new)["value"] == served, (name, _oracle(new))   # 前提
    diffs = {d["profile"]: d for d in config_diff.compute_profile_diff(str(old), str(new))}
    assert set(diffs) == {"010"}, diffs                              # 必響：010 確實改了
    assert diffs["010"]["affected_tenants"] == (["t1"] if served == 10 else []), (name, where)


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
    """已知分歧列：值與 Go 相同（兩邊都沒綁 profile）；`_profile` 允許不同
    （Go 的 `_profile` 是 Marshal 文字，describe 是 generic mapping，不斷言相等）；stderr 有
    WARNING，點名檔案與租戶，每個檔、每個租戶只印一次。"""
    conf_d = _tree(tmp_path, profile, where)
    oracle = _oracle(conf_d)
    assert isinstance(oracle["_profile"], str) and "default: " in oracle["_profile"], oracle
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
    assert "`_profile` in the effective config shown here differs" in lines[0], lines[0]


def test_written_default_and_plain_mappings_do_not_warn(tmp_path):
    """必不響：直接寫出 `default:`（含 merge 下的直接寫出）與沒有 default 的 mapping 不印 WARNING。"""
    for i, profile in enumerate(["{default: '010'}", "{<<: {default: 'zzz'}, default: '010'}",
                                 "{foo: '010'}"]):
        (tmp_path / str(i)).mkdir()
        err = _describe_proc(_tree(tmp_path / str(i), profile, "tenant-file")).stderr
        assert "merge key" not in err, (profile, err)



def test_warning_is_printed_once_on_the_what_if_path(tmp_path):
    """T1：what-if 路徑上 WARNING 每檔恰好 1 行，**同一個檔被讀兩次也一樣**。

    樹內多一個 `_alias.yaml -> _defaults.yaml` symlink，scanner 把同一個檔（解析後路徑相同）
    讀兩次；去重以解析後路徑為鍵，所以樹內那一側只印 1 行，`--what-if` 副本另印 1 行。

    ⛔ 這格要殺的突變是「拿掉 `_UNMIRRORED_PROFILE_WARNED` 去重」（#2739 留言 B）：突變後樹內
    那一側變 2 行。舊版只用「樹內檔 + 樹外副本」兩個不同的檔，每檔本來就只讀一次，突變下照樣
    綠——#2097 F1 之後樹內檔不能再當 `--what-if`，原本的同檔重讀路徑消失，改由 symlink 觸發。"""
    conf_d = _tree(tmp_path, "{<<: {default: '010'}}", "platform-tenants")
    carrier = conf_d / "_defaults.yaml"
    symlink_or_skip("_defaults.yaml", conf_d / "_alias.yaml")
    copy = tmp_path / "edit" / "_defaults.yaml"
    copy.parent.mkdir()
    copy.write_text(carrier.read_text(encoding="utf-8"), encoding="utf-8")
    p = subprocess.run([sys.executable, str(DESCRIBE), "t1", "--conf-d", str(conf_d),
                        "--what-if", str(copy), "--replaces", str(carrier), "--format", "json"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=120, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert p.returncode == 0, p.stderr
    lines = [l for l in p.stderr.splitlines() if "only through a merge key" in l]
    tree_side = [l for l in lines if str(copy) not in l and str(conf_d) in l]
    assert len(tree_side) == 1, p.stderr
    assert sum(str(copy) in l for l in lines) == 1, p.stderr
    assert len(lines) == 2, p.stderr
