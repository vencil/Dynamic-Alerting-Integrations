"""`_profile` 寫成 mapping 時，describe_tenant 的答案要與 Go 的 da-guard effective 一致（#2515）。

Go 的讀法（pkg/config `withProfileText` ← `ScheduledValue.UnmarshalYAML`）：

- 寫出 `default:` 鍵的 mapping（`{default: '010'}`，排程值的寫法）：取 default 的文字。
  yaml.v3 把純量解進 Go string 時保留原文（`010` 是 "010"、`1.50` 是 "1.50"），null 是 ""，
  `!!binary` 是解碼後的位元組；其餘鍵不選 profile。
- 沒寫 `default:`、但經 merge key 帶進來（`{<<: {default: x}}`）：ScheduledValue 看到 `<<`，
  走任意 mapping 的分支，以 merged mapping 的 yaml.v3 `Marshal` 文字（`default: x\\n`）當
  profile 名稱。describe 以前刻意不鏡像（留成 generic mapping 並印 WARNING）；#2906 起它讀
  da-guard effective，`MERGE_KEY_SHAPES` 那組原本的已知分歧列改為四樣全部相同。
- 其他 mapping（完全沒有 `default`）、sequence、null：原樣保留，不選 profile。

每格在租戶檔與根平台檔 `tenants:` 兩個位置各量一次，比對三樣東西：effective_config 的
`_profile`、被 profile 改掉的值（mysql_connections）、merged_hash。修正前（main 59e58c81）
describe 對 mapping 一律不綁 profile、`_profile` 原樣留成 dict，除了「沒有 default」與
對照組之外每格都分歧。

`{default: [..]}` / `{default: {..}}`：yaml.v3 無法把它解成字串，exporter 拒收整份檔
（rc 3）；這原也是已知分歧，#2906 起 describe 一樣不描述該檔的租戶。

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

REPO_ROOT = Path(__file__).resolve().parents[2]
DESCRIBE = REPO_ROOT / "scripts" / "tools" / "dx" / "describe_tenant.py"

pytestmark = pytest.mark.usefixtures("da_guard_env")

_DEFAULTS = "defaults:\n  mysql_connections: 80\n"
# One profile, `010`. No profile is named by a yaml.v3 Marshal text
# (`default: "010"`), so the merge-key rows bind nothing on either side.
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
    # `{default: !!binary MDEw}` was a row (bound "010"): since ADR-036 step 6
    # (#3206) parse-tree refuses `!!binary` and the exporter drops the file.
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
# `default` only through a merge key (#2515): Go elects the merged mapping's
# yaml.v3 Marshal text (an unknown profile here). A known divergence until
# #2906; describe now shows Go's reading.
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
    """config_diff 對 profile 變更報出的租戶，要是 Go 綁到該 profile 的租戶（#2115、#2120）。

    修正前 config_diff 只認字串 `_profile`：`{default: '010'}` 這類 Go 綁 `010` 的寫法列成 `[]`；
    根平台檔 `tenants:` 裡寫的 `_profile` 也完全看不到。ADR-037 起 config_diff 比的是
    served-values 的值，oracle 是 da-guard effective 的值（10 = 綁到 `010`，80 = 沒綁）。"""
    (tmp_path / "old").mkdir()
    (tmp_path / "new").mkdir()
    old = _tree(tmp_path / "old", profile, where)
    (old / "_profiles.yaml").write_text('profiles:\n  "010":\n    mysql_connections: 20\n',
                                        encoding="utf-8")
    new = _tree(tmp_path / "new", profile, where)
    assert _oracle(new)["value"] == served, (name, _oracle(new))   # 前提
    p = subprocess.run([sys.executable, config_diff.__file__, "--old-dir", str(old),
                        "--new-dir", str(new), "--at", "2026-10-10T00:00:00Z", "--format", "json"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    if "!!binary" in profile:
        # ADR-036 step 4 (#2906): the route generator reads parse-tree, which
        # cannot read a `!!binary` file (tag_not_allowed) — so config_diff's
        # render of that tree is refused and nothing is compared (rc 2).
        assert p.returncode == 2, (name, where, p.returncode, p.stderr[-1000:])
        assert "da-guard parse-tree" in p.stderr, (name, where, p.stderr[-1000:])
        return
    doc = json.loads(p.stdout)
    # 必響：010 確實改了（#1516 P3b 起以 parse-tree 讀出的值比較，列在 platform_settings）。
    # 只斷言「包含」：`!!binary` 寫在 `_defaults.yaml` 的那格，parse-tree 讀不了該檔，
    # 它另以 unreadable 列出（沒有比對，rc 1）。
    rows = {(r["file"], r["key"], r["change"]): r["status"] for r in doc["platform_settings"]}
    assert ("_profiles.yaml", "profiles", "modified") in rows, doc
    reached = [t for c in doc["threshold_changes"] if c["key"] == "mysql_connections"
               for t in c["tenants"]]
    assert reached == (["t1"] if served == 10 else []), (name, where, doc["threshold_changes"])
    # #2962: a profile no tenant binds is read here but renders nothing new — "no
    # effect", which does not raise the exit code; a bound one renders (rc 1).
    status = rows[("_profiles.yaml", "profiles", "modified")]
    assert status == (config_diff.SETTING_RENDERED if served == 10 else config_diff.SETTING_NO_EFFECT), (
        name, where, status)
    assert p.returncode == (1 if served == 10 or doc["uncovered_files"] else 0), (
        name, where, p.stderr[-1000:])


def test_the_comparison_can_ring(tmp_path):
    """必響對照組：同一個比對，拿 mapping 那格的 describe 對「沒寫 `_profile`」的樹的 Go
    答案，必須判為不同——證明上面的相等不是比對本身量不到差異。"""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    mapped = _tree(tmp_path / "a", "{default: '010'}", "tenant-file")
    unbound = _tree(tmp_path / "b", "~", "tenant-file")
    assert _reading(mapped) != _oracle(unbound)


@pytest.mark.parametrize("profile", ["{default: ['010']}", "{default: {a: 1}}"])
def test_a_non_scalar_default_is_refused_as_go_refuses_it(tmp_path, profile):
    """exporter 拒收整份檔（rc 3）。這原是已知分歧（describe 不鏡像整檔拒收）；#2906 起
    describe 讀 da-guard effective，所以 t1 一樣不描述：rc 2、點名該檔。"""
    conf_d = _tree(tmp_path, profile, "tenant-file")
    with pytest.raises(tv.ParseFailedError):
        tv.load_effective(conf_d)
    p = subprocess.run([sys.executable, str(DESCRIBE), "-c", str(conf_d), "t1", "--format", "json"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    assert p.returncode == 2, p.stderr
    assert "WARNING: skipped t1.yaml — " in p.stderr and "Tenant 't1' not found" in p.stderr, p.stderr


@pytest.mark.parametrize("where", WHERE)
@pytest.mark.parametrize("name,profile", MERGE_KEY_SHAPES, ids=[s[0] for s in MERGE_KEY_SHAPES])
def test_merge_key_only_default_is_read_as_go_reads_it(tmp_path, name, profile, where):
    """原是已知分歧列（describe 留成 generic mapping、印 WARNING）；#2906 起 describe 的值
    就是 da-guard effective 的，`_profile` 也是 Go 的 Marshal 文字，四樣全部相同、不再警告。"""
    conf_d = _tree(tmp_path, profile, where)
    oracle = _oracle(conf_d)
    assert isinstance(oracle["_profile"], str) and "default: " in oracle["_profile"], oracle
    assert oracle["bound"] == []                         # 前提：Marshal 文字不是任何 profile
    assert _reading(conf_d) == oracle
    assert "merge key" not in _describe_proc(conf_d, "-s").stderr


def test_written_default_and_plain_mappings_do_not_warn(tmp_path):
    """必不響：直接寫出 `default:`（含 merge 下的直接寫出）與沒有 default 的 mapping 不印 WARNING。"""
    for i, profile in enumerate(["{default: '010'}", "{<<: {default: 'zzz'}, default: '010'}",
                                 "{foo: '010'}"]):
        (tmp_path / str(i)).mkdir()
        err = _describe_proc(_tree(tmp_path / str(i), profile, "tenant-file")).stderr
        assert "merge key" not in err, (profile, err)
