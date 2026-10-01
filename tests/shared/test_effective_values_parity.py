"""_lib_tenant_values.load_effective：經 da-guard effective 讀出 tenant-api /effective 的答案（#2564）。

Python 這一半讀 tests/shared/effective_values_matrix.json：同一個值（55）分別只寫在
根目錄 defaults、根目錄平台檔 `tenants:`、租戶檔、子樹 `_defaults.yaml`、profile 綁定
（含 #2515 的 mapping 形 `_profile`）其中一處，load_effective() 的值、來源、profile
與 merged_hash 必須等於表上的答案；對照組 tenant-b（值只寫在自己的租戶檔）不變。
Go 那一半（components/threshold-exporter/app/cmd/da-guard/effective_test.go）要求同一張
表等於 config.ResolveEffective（tenant-api /effective 呼叫的函式），所以兩邊一致。

測試用真的 da-guard：session 級 fixture 以 `go build` 建到 tmp 目錄。建不起來一律 fail、
不 skip——這支 lib 的全部意義就是「值來自 Go」。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import _lib_tenant_values as tv
from _lib_io import YamlFileError

REPO_ROOT = Path(__file__).resolve().parents[2]
APP = REPO_ROOT / "components" / "threshold-exporter" / "app"
MATRIX = json.loads((Path(__file__).parent / "effective_values_matrix.json").read_text(encoding="utf-8"))

# 鍵集合要精確：拼錯的鍵被讀成「沒有」，那一列就什麼都沒測卻照樣綠。
TOP_KEYS = {"_comment", "trees"}
TREE_KEYS = {"name", "files", "expect"}
EXPECT_KEYS = {"value", "source", "profile", "merged_hash"}
KEY = "mysql_connections"


@pytest.fixture(scope="session")
def da_guard(tmp_path_factory) -> str:
    go = shutil.which("go")
    if go is None:
        pytest.fail("`go` is not on PATH: da-guard cannot be built, so nothing here can be measured")
    out = tmp_path_factory.mktemp("da-guard") / "da-guard"
    proc = subprocess.run(
        [go, "build", "-buildvcs=false", "-o", str(out), "./cmd/da-guard"],
        cwd=APP, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=600)
    if proc.returncode != 0:
        pytest.fail(f"go build da-guard failed (rc={proc.returncode}):\n{proc.stderr}")
    return str(out)


def _tree(root: Path, files: dict[str, str]) -> Path:
    conf_d = root / "conf.d"
    for rel, body in files.items():
        p = conf_d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return conf_d


def _reading(t: tv.TenantEffective) -> dict:
    """一個租戶在表上那幾欄的讀數。"""
    src = t.key_sources.get(KEY)
    source = None if src is None else {"layer": src.layer, "file": src.file,
                                       **({"level": src.level} if src.level is not None else {})}
    return {"value": t.effective_config.get(KEY), "source": source,
            "profile": t.profile, "merged_hash": t.merged_hash}


def _mismatches(tree: dict, readings: dict[str, dict]) -> list[str]:
    out = []
    if set(readings) != set(tree["expect"]):
        out.append(f"tenants {sorted(readings)} != {sorted(tree['expect'])}")
    for tenant, want in tree["expect"].items():
        got = readings.get(tenant, {})
        for col in sorted(EXPECT_KEYS):
            if got.get(col) != want[col]:
                out.append(f"{tenant}.{col}: got {got.get(col)!r}, want {want[col]!r}")
    return out


def test_matrix_shape():
    assert set(MATRIX) == TOP_KEYS
    names = [t["name"] for t in MATRIX["trees"]]
    assert len(names) == len(set(names))
    # 票面要求的四個位置 + profile 綁定都在表上。
    assert {"root-defaults", "platform-tenants", "tenant-file", "subtree-defaults", "profile"} <= set(names)
    for tree in MATRIX["trees"]:
        assert set(tree) == TREE_KEYS, tree["name"]
        assert set(tree["expect"]) == {"tenant-a", "tenant-b"}, tree["name"]
        for exp in tree["expect"].values():
            assert set(exp) == EXPECT_KEYS, tree["name"]
        # 對照組：值只在自己的租戶檔，每棵樹都一樣。
        assert tree["expect"]["tenant-b"] == MATRIX["trees"][0]["expect"]["tenant-b"], tree["name"]


@pytest.mark.parametrize("tree", MATRIX["trees"], ids=lambda t: t["name"])
def test_load_effective_matches_the_matrix(tree, tmp_path, da_guard):
    got = tv.load_effective(_tree(tmp_path, tree["files"]), binary=da_guard)
    assert _mismatches(tree, {t: _reading(e) for t, e in got.items()}) == []


def test_matrix_catches_a_reader_that_reads_the_wrong_field(tmp_path, da_guard):
    """must-trigger：讀錯欄位的 reader 必須被這張表抓到，否則上面的綠燈什麼都沒證明。"""
    wrong = {
        "source_hash as merged_hash": lambda t, _all: {**_reading(t), "merged_hash": t.source_hash},
        "every key from the tenant file": lambda t, _all: {
            **_reading(t), "source": {"layer": "tenant", "file": t.source_file}},
        "no profile binding": lambda t, _all: {**_reading(t), "profile": None},
        "the control's value": lambda t, all_: {**_reading(t), "value": all_["tenant-b"].effective_config.get(KEY)},
    }
    caught = {name: [] for name in wrong}
    for i, tree in enumerate(MATRIX["trees"]):
        got = tv.load_effective(_tree(tmp_path / str(i), tree["files"]), binary=da_guard)
        assert _mismatches(tree, {t: _reading(e) for t, e in got.items()}) == []  # 先確認正確讀法是綠的
        for name, read in wrong.items():
            if _mismatches(tree, {t: read(e, got) for t, e in got.items()}):
                caught[name].append(tree["name"])
    assert caught["source_hash as merged_hash"] == [t["name"] for t in MATRIX["trees"]], caught
    for name, trees in caught.items():
        assert trees, f"the matrix does not catch a reader that reads {name}"


def test_parse_failed_raises_yaml_file_error_naming_the_file(tmp_path, da_guard):
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
        "tenant-a.yaml": "tenants:\n  tenant-a:\n    mysql_connections: 70\n",
        "tenant-b.yaml": "tenants:\n  tenant-b: [1]\n",
    })
    with pytest.raises(YamlFileError) as ei:
        tv.load_effective(conf_d, binary=da_guard)
    assert ei.value.path == str(conf_d / "tenant-b.yaml")


def test_root_defaults_the_exporter_drops_raises_yaml_file_error(tmp_path, da_guard):
    """exporter 因內容型別錯整份丟掉根 `_defaults.yaml` 時，walker 的寬鬆 chain 解析仍讀得到
    它的值；load_effective 不可把這些 /metrics 不會用的值當答案交出去。"""
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n  _silent_mode: warning\n",
        "tenant-a.yaml": "tenants:\n  tenant-a:\n    _silent_mode: critical\n",
    })
    with pytest.raises(YamlFileError) as ei:
        tv.load_effective(conf_d, binary=da_guard)
    assert ei.value.path == str(conf_d / "_defaults.yaml")


def test_chain_file_the_resolve_rejects_raises_yaml_file_error(tmp_path, da_guard):
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
        "sub/_defaults.yaml": "defaults: [\n",
        "sub/tenant-a.yaml": "tenants:\n  tenant-a:\n    mysql_connections: 70\n",
    })
    with pytest.raises(YamlFileError) as ei:
        tv.load_effective(conf_d, binary=da_guard)
    assert ei.value.path == str(conf_d / "sub" / "_defaults.yaml")


def test_duplicate_tenant_raises_effective_error_with_stderr(tmp_path, da_guard):
    conf_d = _tree(tmp_path, {
        "a.yaml": "tenants:\n  tenant-a:\n    mysql_connections: 70\n",
        "b.yaml": "tenants:\n  tenant-a:\n    mysql_connections: 71\n",
    })
    with pytest.raises(tv.EffectiveError) as ei:
        tv.load_effective(conf_d, binary=da_guard)
    assert ei.value.returncode == 2
    assert "duplicate tenant" in ei.value.stderr and "duplicate tenant" in str(ei.value)
    assert isinstance(ei.value, tv.DaGuardError)


def test_empty_tree_raises_effective_error(tmp_path, da_guard):
    (tmp_path / "conf.d").mkdir()
    with pytest.raises(tv.EffectiveError) as ei:
        tv.load_effective(tmp_path / "conf.d", binary=da_guard)
    assert ei.value.returncode == 2 and "no .yaml files found" in ei.value.stderr


def _fake_da_guard(tmp_path: Path, stdout: str, rc: int = 0) -> str:
    script = tmp_path / "fake-da-guard"
    script.write_text(f"#!/bin/sh\ncat <<'EOF'\n{stdout}\nEOF\nexit {rc}\n", encoding="utf-8")
    script.chmod(0o755)
    return str(script)


def _doc(**tenant_over) -> dict:
    tenant = {"tenant_id": "t", "source_file": "t.yaml", "source_hash": "a", "merged_hash": "b",
              "defaults_chain": [], "effective_config": {"k": 1}, "profile": None,
              "key_sources": {"k": {"layer": "tenant", "file": "t.yaml"}}}
    tenant.update(tenant_over)
    return {"schema": tv.EFFECTIVE_SCHEMA, "parse_failed": [], "tenants": {"t": tenant}}


def test_fake_binary_baseline_reads(tmp_path):
    """下面幾支 fail-closed 測試的基準：同一支假 binary 給正確的文件時讀得出來。"""
    got = tv.load_effective(tmp_path, binary=_fake_da_guard(tmp_path, json.dumps(_doc())))
    assert got["t"].key_sources == {"k": tv.KeySource("tenant", "t.yaml", None)}


@pytest.mark.parametrize("doc", [
    {**_doc(), "schema": "da-guard.effective/v2"},
    {k: v for k, v in _doc().items() if k != "schema"},
    _doc(key_sources={}),
    _doc(key_sources={"k": {"layer": "chain", "file": "t.yaml"}}),
    _doc(key_sources={"k": {"layer": "defaults", "file": "_defaults.yaml"}}),
    _doc(key_sources={"k": {"layer": "tenant", "file": "t.yaml", "level": 0}}),
    _doc(tenant_id="other"),
], ids=["schema-v2", "no-schema", "key-unattributed", "unknown-layer", "defaults-without-level",
        "tenant-with-level", "tenant-id-mismatch"])
def test_output_not_in_the_expected_shape_raises(doc, tmp_path):
    with pytest.raises(tv.EffectiveError):
        tv.load_effective(tmp_path, binary=_fake_da_guard(tmp_path, json.dumps(doc)))


def test_missing_binary_raises_with_install_hint(tmp_path, monkeypatch):
    monkeypatch.setenv("DA_LANG", "en")
    monkeypatch.delenv("DA_GUARD_BINARY", raising=False)
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(tv.DaGuardNotFoundError) as ei:
        tv.load_effective(tmp_path)
    assert "DA_GUARD_BINARY" in str(ei.value)


def test_subprocess_gets_a_timeout(tmp_path, monkeypatch, da_guard):
    seen = {}
    real_run = subprocess.run

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(tv.subprocess, "run", spy)
    conf_d = _tree(tmp_path, {"tenant-a.yaml": "tenants:\n  tenant-a:\n    mysql_connections: 70\n"})
    tv.load_effective(conf_d, binary=da_guard, timeout=7)
    assert seen.get("timeout") == 7
