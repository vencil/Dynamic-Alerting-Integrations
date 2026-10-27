"""寫進既有的樹之前，先過共用的 conf.d 拒絕（#3206）：`_lib_write_check.require_output_tree`。

釘住的契約：

1. 輸出目錄已存在、裡面有 Go 點名的路徑：結束碼 2、每一條都點名，呼叫端一個檔都
   還沒寫。
2. 不存在的目錄是新樹；空目錄、只有非設定檔的目錄 Go 判為可服務——不拒絕。
3. 沒有 da-guard 是 `ParseTreeError`（呼叫端給 exit 2），不是拒絕。

產生的 conf.d 文字怎麼判、怎麼寫是 `da-guard write` 的（`_lib_confedit`），不在這裡。

Tenant ids are fixture names (CLAUDE.md #9).
"""
from __future__ import annotations

import pytest

import _lib_parse_tree as pt
import _lib_write_check as wc


def test_an_existing_tree_go_flags_exits_2_naming_every_path(tmp_path, capsys):
    (tmp_path / "broken.yaml").write_text("a: [\n", encoding="utf-8")
    (tmp_path / "x.yaml").mkdir()
    with pytest.raises(SystemExit) as exc:
        wc.require_output_tree(tmp_path)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "broken.yaml" in err and "x.yaml" in err


@pytest.mark.parametrize("make", ["missing", "empty", "not_a_confd", "servable"])
def test_a_fresh_or_servable_directory_goes_on(tmp_path, make):
    root = tmp_path / "out"
    if make != "missing":
        root.mkdir()
    if make == "not_a_confd":
        (root / "README.md").write_text("# notes\n", encoding="utf-8")
    if make == "servable":
        (root / "tenant-a.yaml").write_text("tenants:\n  tenant-a: {}\n", encoding="utf-8")
    wc.require_output_tree(root)


def test_no_da_guard_is_a_parse_tree_error_not_a_refusal(tmp_path, monkeypatch):
    (tmp_path / "tenant-a.yaml").write_text("tenants:\n  tenant-a: {}\n", encoding="utf-8")
    monkeypatch.setenv("DA_GUARD_BINARY", str(tmp_path / "no-such-da-guard"))
    with pytest.raises(pt.ParseTreeError):
        wc.require_output_tree(tmp_path, binary=str(tmp_path / "no-such-da-guard"))
