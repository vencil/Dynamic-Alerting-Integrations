"""validate-config `unsupplied_defaults`（#3830）：平台預設出貨、這棵樹卻沒有供給的閾值鍵。

哪些鍵預設出貨問 registry（生成模組的 `DEFAULT_ARMED_KEYS`，由 `arming: default` 推導），
這裡不抄清單：每支測試的鍵都從 registry 取。pack 有沒有部署看兩種證據——root 平台檔
帶該 pack 的任一鍵，或租戶生效的 `_metadata.db_type`。永遠只是建議：最多 WARN，
不影響 exit code。租戶 id 是 fixture 名（CLAUDE.md #9）。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import _registry_lib as lib
import validate_config as vc
from _lib_exporter_presence import EXPORTER_PRESENCE

pytestmark = pytest.mark.usefixtures("da_guard_env")

_REPO = Path(__file__).resolve().parents[2]
_DOC = lib.committed_registry_doc()
_KEYS = lib.registry_keys(_DOC)
_ARMED = lib.default_armed_by_pack(_DOC)
#: (pack, key) for every key the registry ships armed.
_ARMED_PAIRS = [(pack, key) for pack, keys in _ARMED.items() for key in keys]
#: Packs with at least two armed keys: one can stand as the evidence while
#: the other is missing.
_MULTI = sorted(pack for pack, keys in _ARMED.items() if len(keys) > 1)
#: Armed packs a tenant can declare in `_metadata.db_type`.
_DB_PACKS = sorted(set(_ARMED) & set(EXPORTER_PRESENCE))


def _tree(root: Path, files: dict[str, str]) -> str:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return str(root)


def _defaults(keys, listed=()) -> str:
    text = "defaults:\n" + "".join(f"  {k}: {_KEYS[k]['value']}\n" for k in keys) if keys else ""
    if listed:
        text += "optional_overrides:\n" + "".join(f"  - {k}\n" for k in listed)
    return text or "defaults: {}\n"


def _cli_row(tree: str) -> dict:
    """The row as the whole tool reports it (`--json`): a tree with a file
    that does not read is judged through `main`'s reader, not a bare call."""
    r = subprocess.run([sys.executable, str(_REPO / "scripts/tools/ops/validate_config.py"),
                        "--config-dir", tree, "--json"],
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    rows = [c for c in json.loads(r.stdout) if c["check"] == "unsupplied_defaults"]
    assert len(rows) == 1, r.stdout + r.stderr
    return rows[0]


def _named(result) -> list[str]:
    """The keys the row's lines name, in order."""
    return [d.split("`")[1] for d in result["details"] if d.startswith("unsupplied `")]


def test_the_registry_ships_something_armed():
    """下面每支以 registry 參數化的測試都靠這幾個集合非空：空了就是什麼都沒測。"""
    assert _ARMED_PAIRS and _MULTI and _DB_PACKS


@pytest.mark.parametrize("pack,key", [(p, k) for p, k in _ARMED_PAIRS if p in _MULTI])
def test_an_armed_key_the_root_file_lacks_is_named(tmp_path, pack, key):
    """缺 key：root 帶同 pack 的其他鍵（pack 有部署）、獨缺這一個 → 點名它，而且只點名它。"""
    others = [k for k in _ARMED[pack] if k != key]
    r = vc.check_unsupplied_defaults(_tree(tmp_path, {"_defaults.yaml": _defaults(others)}))
    assert r["status"] == vc.WARN, r
    assert _named(r) == [key], r
    line = r["details"][0]
    assert f"the {pack} pack ships it armed" in line
    assert f"platform value {_KEYS[key]['value']}" in line
    assert f"_defaults.yaml carries {', '.join(sorted(others))}" in line


@pytest.mark.parametrize("pack", sorted(_ARMED))
def test_every_armed_key_set_passes(tmp_path, pack):
    """有 key：該 pack 預設出貨的鍵全在 `defaults:` → PASS，並說判了幾個。"""
    r = vc.check_unsupplied_defaults(_tree(tmp_path, {"_defaults.yaml": _defaults(_ARMED[pack])}))
    assert r["status"] == vc.PASS, r
    assert r["details"] == [f"{len(_ARMED[pack])} default-armed threshold(s) of 1 deployed pack(s): "
                            "each is set or listed in _defaults.yaml"]


@pytest.mark.parametrize("pack,key", [(p, k) for p, k in _ARMED_PAIRS if p in _MULTI])
def test_a_key_listed_in_optional_overrides_is_a_deliberate_opt_out(tmp_path, pack, key):
    """列在 optional：平台刻意不給值的寫法 → 不點名。"""
    others = [k for k in _ARMED[pack] if k != key]
    r = vc.check_unsupplied_defaults(_tree(tmp_path, {
        "_defaults.yaml": _defaults(others, listed=[key])}))
    assert r["status"] == vc.PASS, r


def test_a_pack_with_no_evidence_is_not_judged(tmp_path):
    """pack 未部署：root 只帶一個 pack 的鍵、沒有租戶宣告 db_type → 其他 pack 的鍵不點名。"""
    pack = _MULTI[0]
    r = vc.check_unsupplied_defaults(_tree(tmp_path, {
        "_defaults.yaml": _defaults(_ARMED[pack]),
        "ta.yaml": "tenants:\n  ta: {}\n"}))
    assert r["status"] == vc.PASS, r
    assert "of 1 deployed pack(s)" in r["details"][0]


def test_a_tree_with_no_armed_pack_at_all_passes(tmp_path):
    operator_only = next(k for k, e in _KEYS.items()
                         if e["tier"] == "defaults" and e["pack"] not in _ARMED)
    r = vc.check_unsupplied_defaults(_tree(tmp_path, {"_defaults.yaml": _defaults([operator_only])}))
    assert r["status"] == vc.PASS, r
    assert "0 default-armed threshold(s) of 0 deployed pack(s)" in r["details"][0]


@pytest.mark.parametrize("pack", _DB_PACKS)
@pytest.mark.parametrize("written", ["{pack}", "[{pack}]"])
def test_a_declared_db_type_deploys_the_pack_without_any_of_its_keys(tmp_path, pack, written):
    """root 完全沒有該 pack 的鍵，租戶宣告了 db_type（字串或列表）→ 該 pack 的鍵全數點名。"""
    r = vc.check_unsupplied_defaults(_tree(tmp_path, {
        "_defaults.yaml": "defaults: {}\n",
        "ta.yaml": f"tenants:\n  ta:\n    _metadata:\n      db_type: {written.format(pack=pack)}\n"}))
    assert r["status"] == vc.WARN, r
    assert _named(r) == _ARMED[pack], r
    assert all(f"{pack} is deployed here: db_type of tenant(s) ta" in d for d in r["details"])


def test_a_db_type_a_root_platform_file_writes_for_the_tenant_counts(tmp_path):
    """db_type 讀的是 /metrics 送出的生效值：root 平台檔的 `tenants:` 代寫的也算。"""
    pack = _DB_PACKS[0]
    r = vc.check_unsupplied_defaults(_tree(tmp_path, {
        "_defaults.yaml": f"defaults: {{}}\ntenants:\n  ta:\n    _metadata:\n      db_type: {pack}\n",
        "ta.yaml": "tenants:\n  ta: {}\n"}))
    assert _named(r) == _ARMED[pack], r


def test_a_db_type_with_no_armed_key_names_nothing(tmp_path):
    kind = next((k for k in sorted(EXPORTER_PRESENCE) if k not in _ARMED), None)
    if kind is None:
        pytest.skip("every db_type's pack ships an armed key")
    r = vc.check_unsupplied_defaults(_tree(tmp_path, {
        "_defaults.yaml": "defaults: {}\n",
        "ta.yaml": f"tenants:\n  ta:\n    _metadata:\n      db_type: {kind}\n"}))
    assert r["status"] == vc.PASS, r


def test_a_retired_spelling_in_the_root_file_is_evidence_of_its_pack(tmp_path):
    """root 只剩舊拼法（#1231 的別名）：照 exporter 的正規拼法歸 pack（問 da-guard），
    該 pack 算有部署。"""
    old, new = next(((o, n) for o, n in lib.DEPRECATED_KEY_ALIASES.items()
                     if _KEYS[n]["pack"] in _ARMED), (None, None))
    if old is None:
        pytest.skip("the registry has no deprecated alias onto a pack with an armed key")
    pack = _KEYS[new]["pack"]
    r = vc.check_unsupplied_defaults(_tree(tmp_path, {
        "_defaults.yaml": f"optional_overrides:\n  - {old}\n"}))
    assert _named(r) == _ARMED[pack], r
    assert f"_defaults.yaml carries {old}" in r["details"][0]


def test_a_dimension_key_supplies_its_base(tmp_path):
    """`defaults:` 的維度鍵（`key{label="v"}`）算有供給該鍵。"""
    pack = _MULTI[0]
    first, rest = _ARMED[pack][0], _ARMED[pack][1:]
    r = vc.check_unsupplied_defaults(_tree(tmp_path, {
        "_defaults.yaml": _defaults(rest) + f'  {first}{{version="v2"}}: {_KEYS[first]["value"]}\n'}))
    assert r["status"] == vc.PASS, r


def test_the_yml_carrier_is_read(tmp_path):
    """root 平台檔由 parse-tree 選：`_defaults.yml` 也是。"""
    pack = _MULTI[0]
    r = vc.check_unsupplied_defaults(_tree(tmp_path, {"_defaults.yml": _defaults(_ARMED[pack][1:])}))
    assert _named(r) == _ARMED[pack][:1], r
    assert "_defaults.yml neither sets it" in r["details"][0]


def test_a_key_only_a_subtree_carrier_declares_does_not_supply_it(tmp_path):
    """子樹的 `_defaults.yaml` 宣告的鍵送不上 /metrics（#1976）：不算供給。"""
    pack = _MULTI[0]
    first, rest = _ARMED[pack][0], _ARMED[pack][1:]
    r = vc.check_unsupplied_defaults(_tree(tmp_path, {
        "_defaults.yaml": _defaults(rest),
        "team/_defaults.yaml": _defaults([first])}))
    assert _named(r) == [first], r


def test_no_root_platform_file_and_no_db_type_is_nothing_to_check(tmp_path):
    r = vc.check_unsupplied_defaults(_tree(tmp_path, {"ta.yaml": "tenants:\n  ta: {}\n"}))
    assert r["status"] == vc.PASS, r
    assert r["details"] == ["no root _defaults.yaml, and no tenant declares a db_type whose "
                            "pack ships an armed threshold — nothing to check"]


@pytest.mark.parametrize("files", [{}, {"team/_defaults.yaml": "defaults: {}\n"}],
                         ids=["no-carrier", "subtree-carrier-only"])
def test_no_root_platform_file_supplies_nothing_to_a_declared_pack(tmp_path, files):
    """沒有 root 平台檔、租戶宣告了 db_type：該 pack 的鍵一個都沒供給，全數點名，不是 PASS。"""
    pack = _DB_PACKS[0]
    r = vc.check_unsupplied_defaults(_tree(tmp_path, {
        **files, "ta.yaml": f"tenants:\n  ta:\n    _metadata:\n      db_type: {pack}\n"}))
    assert r["status"] == vc.WARN, r
    assert _named(r) == _ARMED[pack], r
    assert all("this tree has no root _defaults.yaml to set it in" in d for d in r["details"])


def test_a_retired_key_in_the_root_file_is_evidence_of_its_replacements_pack(tmp_path):
    """root 只剩退役的鍵（#3829）：它顯示該 pack 有部署，但不算供給了替代鍵（單位可能不同）。"""
    retired = lib.retired_key_packs(_DOC)
    old, pack = next(((o, p) for o, p in retired.items() if p in _ARMED), (None, None))
    if old is None:
        pytest.skip("the registry has no retired key replaced by a key of an armed pack")
    for written in (old, old + "_critical"):
        r = vc.check_unsupplied_defaults(_tree(tmp_path / written, {
            "_defaults.yaml": f"optional_overrides:\n  - {written}\n"}))
        assert _named(r) == _ARMED[pack], (written, r)
        assert f"_defaults.yaml carries {written}" in r["details"][0]


@pytest.mark.parametrize("root", ["defaults: [unclosed\n", "- a\n- b\n", "defaults:\n  k: 1\n  k: 2\n"],
                         ids=["syntax", "top-level-list", "duplicate-key"])
def test_a_root_file_that_does_not_read_is_not_checked_never_a_fail(tmp_path, root):
    """root 平台檔讀不了：這一列說沒判（WARN），不是 FAIL——讀不了是 yaml_syntax 那列的事。"""
    pack = _DB_PACKS[0]
    tree = _tree(tmp_path, {"_defaults.yaml": root,
                            "ta.yaml": f"tenants:\n  ta:\n    _metadata:\n      db_type: {pack}\n"})
    r = _cli_row(tree)
    assert r["status"] == vc.WARN, r
    assert r["details"][0].startswith("not checked: "), r


@pytest.mark.parametrize("files", [
    {"_defaults.yaml": "defaults: {}\n", "ta.yaml": "tenants:\n  ta: {}\n",
     "tb.yaml": "tenants:\n  tb:\n    _metadata:\n      db_type: %s\n    x: [unclosed\n"},
    {"_defaults.yaml": "defaults: 5\n",
     "ta.yaml": "tenants:\n  ta:\n    _metadata:\n      db_type: %s\n"},
], ids=["tenant-file", "root-file"])
def test_a_file_the_load_drops_is_not_checked_not_a_pass(tmp_path, files):
    """exporter 載入時丟掉的檔：租戶檔的 db_type 讀不到、root 檔則什麼都沒供給 → 說沒判。"""
    pack = _DB_PACKS[0]
    r = _cli_row(_tree(tmp_path, {
        rel: (text % pack if "%s" in text else text) for rel, text in files.items()}))
    assert r["status"] == vc.WARN, r
    assert r["details"][0].startswith("not checked: ") and "load drops" in r["details"][0], r


def test_a_da_guard_that_cannot_answer_is_not_checked_never_a_pass(tmp_path, monkeypatch):
    """da-guard 答不出來：WARN、說沒判，不是 PASS 也不是 FAIL。"""
    def broken(*_a, **_k):
        raise vc.DaGuardError("da-guard exited 2", 2, "")
    monkeypatch.setattr(vc, "load_served_tree", broken)
    pack = _MULTI[0]
    r = vc.check_unsupplied_defaults(_tree(tmp_path, {"_defaults.yaml": _defaults(_ARMED[pack])}))
    assert r["status"] == vc.WARN, r
    assert r["details"] == ["not checked: whether every default-armed threshold is supplied "
                            "was not judged (DaGuardError: da-guard exited 2)"]
    assert r["hint"] == vc._UNSUPPLIED_DEFAULTS_NOT_CHECKED_HINT


@pytest.mark.parametrize("where,exc", [
    ("load_served_tree", AttributeError("a bug")),
    ("_registry", KeyError("DEFAULT_ARMED_KEYS")),
    ("_registry", ValueError("a broken generated module")),
])
def test_a_defect_of_this_tool_is_not_swallowed(tmp_path, monkeypatch, where, exc):
    """這支工具自己的缺陷（生成模組壞了、程式錯）不包成「not checked」：那句 hint 會叫人去修 da-guard。"""
    def defect(*_a, **_k):
        raise exc
    if where == "_registry":
        monkeypatch.setattr(lib, "packaged_default_armed", defect)
    else:
        monkeypatch.setattr(vc, where, defect)
    with pytest.raises(type(exc)):
        vc.check_unsupplied_defaults(_tree(tmp_path, {"_defaults.yaml": _defaults(_ARMED[_MULTI[0]])}))


def test_it_never_changes_the_exit_code(tmp_path):
    """整支工具跑：這一列 WARN、其他列照舊，exit code 0，hint 說怎麼處理。"""
    pack = _MULTI[0]
    first = _ARMED[pack][0]
    tree = _tree(tmp_path, {"_defaults.yaml": _defaults(_ARMED[pack][1:]),
                            "ta.yaml": "tenants:\n  ta: {}\n"})
    r = subprocess.run([sys.executable, str(_REPO / "scripts/tools/ops/validate_config.py"),
                        "--config-dir", tree], capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[WARN] unsupplied_defaults" in r.stdout
    assert f"unsupplied `{first}`" in r.stdout
    assert "list it under `optional_overrides:`" in r.stdout


def test_the_generated_table_is_the_registrys_armed_set():
    """生成模組的 `DEFAULT_ARMED_KEYS` 就是 registry 的 `arming: default`：image 裡沒有
    registry YAML，這一列讀的是它。"""
    assert lib.packaged_default_armed() == {p: tuple(k) for p, k in _ARMED.items()}
    assert sorted(k for keys in _ARMED.values() for k in keys) == sorted(
        k for k, e in _KEYS.items() if lib.is_chart_armed(e))
    assert lib.packaged_retired_key_packs() == lib.retired_key_packs(_DOC)
    assert lib.retired_key_packs(_DOC) == {
        old: _KEYS[e["replaced_by"]]["pack"] for old, e in _DOC["retired_keys"].items()
        if e.get("replaced_by")}


def test_every_db_type_is_a_registry_pack():
    """db_type 直接當 pack 名用：兩個命名空間要一致，否則宣告了也對不上。"""
    assert set(EXPORTER_PRESENCE) <= set(_DOC["packs"])


def test_the_shipped_example_trees_supply_every_armed_key():
    """repo 自帶的範例樹（開發範例、try-local seed）不該被自己的檢查點名。"""
    for rel in ("components/threshold-exporter/config/conf.d", "try-local/seed/conf.d"):
        r = vc.check_unsupplied_defaults(str(_REPO / rel))
        assert r["status"] == vc.PASS, (rel, r)
