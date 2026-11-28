"""validate --mapping 讀比對組的方式（issue 1818）：舊格式退回並警告、新格式
不出聲、不是 mapping 的條目點名而不略過。

migrate-rule 寫這份 mapping 的那一半已隨 #4226 移除（prefix-mapping 不再產生）；
validate 改讀新的遷移對照是 #4227。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

_OPS = Path(__file__).resolve().parents[2] / "scripts" / "tools" / "ops"
sys.path.insert(0, str(_OPS))

import validate_migration as vm  # noqa: E402


# ---------------------------------------------------------------------------
# validate 的比對組
# ---------------------------------------------------------------------------


def _mapping_args(path):
    """The namespace `validate --mapping <path>` parses to (#3445: a
    `_lib_input` file declaration, read by `load_mapping_pairs`)."""
    import argparse
    import _lib_input
    parser = argparse.ArgumentParser()
    _lib_input.add_input(parser, "--mapping", decode=vm.MAPPING_DOCUMENT)
    return parser.parse_args(["--mapping", str(path)])


def test_legacy_mapping_file_falls_back_and_warns(tmp_path, capsys):
    p = tmp_path / "prefix-mapping.yaml"
    p.write_text(yaml.safe_dump({
        "custom_x": {"original_metric": "x", "alert_name": "A"},
        "custom_x_critical": {"original_metric": "x", "alert_name": "B"},
    }), encoding="utf-8")
    pairs = vm.load_mapping_pairs(_mapping_args(p))
    assert [(q["old_query"], q["new_query"]) for q in pairs] == [
        ("x", "tenant:custom_x:max"), ("x", "tenant:custom_x:max")]
    err = capsys.readouterr().err
    assert "old_query" in err and "custom_x_critical" in err


def test_new_mapping_file_is_silent(tmp_path, capsys):
    p = tmp_path / "prefix-mapping.yaml"
    p.write_text(yaml.safe_dump({"custom_x": {
        "original_metric": "x", "old_query": "sum by(tenant) (x)",
        "new_query": "tenant:custom_x:sum"}}), encoding="utf-8")
    assert [(q["old_query"], q["new_query"]) for q in vm.load_mapping_pairs(_mapping_args(p))] == [
        ("sum by(tenant) (x)", "tenant:custom_x:sum")]
    assert capsys.readouterr().err == ""


# ---------------------------------------------------------------------------
# promtool 端到端
# ---------------------------------------------------------------------------


def test_a_non_mapping_entry_is_named_not_skipped(tmp_path):
    """手改壞的項（值不是 mapping）點名並以 rc 2 拒收（#4210）。先前是跳過、
    只比對其餘的組，使用者以為那個 key 也比對過了。"""
    from _lib_exitcodes import CallerError
    p = tmp_path / "prefix-mapping.yaml"
    p.write_text(yaml.safe_dump({
        "custom_bad": "not-a-mapping",
        "custom_x": {"original_metric": "x", "old_query": "max by(tenant) (x)",
                     "new_query": "tenant:custom_x:max"}}), encoding="utf-8")
    with pytest.raises(CallerError) as exc:
        vm.load_mapping_pairs(_mapping_args(p))
    assert "custom_bad（值不是映射（實為字串））" in str(exc.value)
    assert "custom_x（" not in str(exc.value)


