"""`check_portal_bundle_size.py` 的上限比對守衛 (#1884)。

⛔ **這支閘門正在服役，但「它到底有沒有在比對上限」一直沒有測試。** 呼叫點是
`ci.yml` 的「Enforce portal bundle size budget (TRK-232a)」與 `make
portal-bundle-budget`，兩者都帶 `--ci`。#1884 量到的核心事實是：

    同一個超標狀態，帶 `--ci` 是 rc 1，不帶是 rc 0。

⇒ 任何只看 rc 又不帶 `--ci` 的測試，**結構上打不到違規分支**。一支照樣走
`DIST_DIR.glob("*.js")` 算出正確檔數與位元組數、卻完全不比對三條上限的實作，
會通過本檔以外的全部既有測試。

⚠️ **每一格都先斷言 fixture 真的造出它宣稱的狀態**（位元組數 vs 上限），再問結論。
少了這半，斷言會平凡為真——例如「未超標 ⇒ 無 violation」在一支永遠回空的實作上
也是綠的，所以它的孿生格「超標 ⇒ 有 violation」才是那一格的意義所在。

⚠️ 注入方式是 monkeypatch 模組層的 `DIST_DIR`：它是由 `__file__` 推出來的常數，
但 `run_check()` 在**呼叫時**才讀它，所以不需要改生產碼加 `--dist-dir`。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

import check_portal_bundle_size as cpbs  # noqa: E402  (path via conftest)

EXIT_OK = 0
EXIT_VIOLATION = 1


@pytest.fixture
def argv(monkeypatch):
    """設定 `sys.argv`——`main()` 不收 argv 參數。"""
    def _set(*args: str) -> None:
        monkeypatch.setattr(sys, "argv", ["check_portal_bundle_size", *args])
    return _set


@pytest.fixture
def dist(tmp_path, monkeypatch):
    """把模組層的 `DIST_DIR` 指向一個空的合成 dist 目錄，並回傳它。"""
    d = tmp_path / "dist"
    d.mkdir()
    monkeypatch.setattr(cpbs, "DIST_DIR", d)
    return d


def _write(dist_dir: Path, name: str, size: int) -> Path:
    p = dist_dir / name
    p.write_bytes(b"x" * size)
    assert p.stat().st_size == size, "fixture 沒有寫出宣稱的位元組數"
    return p


def _kinds(violations) -> list[str]:
    return sorted({v["kind"] for v in violations})


# ---------------------------------------------------------------------------
# #1884 的頭條：rc 通道只有在 --ci 下才承載「有沒有違規」
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flags, expected_rc", [
    ((), EXIT_OK),                      # 不帶 --ci：超標照樣 rc 0
    (("--ci",), EXIT_VIOLATION),        # 帶 --ci：同一個狀態才變 rc 1
])
def test_only_ci_turns_a_violation_into_a_nonzero_rc(dist, argv, capsys, flags, expected_rc):
    """⛔ 同一個超標狀態，兩種 argv 下 rc 不同——這就是 #1884 說的「通道不承載資訊」。

    ⇒ 這一格同時是本檔其餘各格的前提：它們都必須帶 `--ci` 才問得到 rc。
    """
    _write(dist, "tool.js", cpbs.PER_TOOL_LIMIT + 1)
    violations, _ = cpbs.run_check()
    assert _kinds(violations) == ["per-tool-exceeded"], (
        f"前置條件：fixture 必須真的違規，實得 {_kinds(violations)}"
    )

    argv(*flags)
    rc = cpbs.main()
    capsys.readouterr()
    assert rc == expected_rc, f"argv={flags} 期待 rc={expected_rc}，實得 {rc}"


# ---------------------------------------------------------------------------
# 三條上限各自要有一格，而且都要雙向
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("delta, expected", [
    (0, []),                        # 剛好在上限：判定是 `>`，不違規
    (1, ["per-tool-exceeded"]),     # 多一個 byte 就違規
])
def test_per_tool_limit_is_actually_compared(dist, delta, expected):
    """`PER_TOOL_LIMIT` 有沒有真的被比對。邊界取在上限本身，因為判定是嚴格大於。"""
    size = cpbs.PER_TOOL_LIMIT + delta
    _write(dist, "tool.js", size)
    assert (size > cpbs.PER_TOOL_LIMIT) is bool(expected), (
        f"前置條件不成立：{size} vs 上限 {cpbs.PER_TOOL_LIMIT}"
    )
    violations, stats = cpbs.run_check()
    assert _kinds(violations) == expected
    assert stats["file_count"] == 1, "反空轉：它必須真的掃到那個檔"


@pytest.mark.parametrize("delta, expected", [
    (0, []),
    (1, ["shared-chunk-exceeded"]),
])
def test_shared_chunk_limit_is_actually_compared(dist, delta, expected):
    """`SHARED_CHUNK_LIMIT` 有沒有真的被比對（`chunk-` 前綴的檔走這條）。"""
    size = cpbs.SHARED_CHUNK_LIMIT + delta
    _write(dist, "chunk-vendor.js", size)
    assert (size > cpbs.SHARED_CHUNK_LIMIT) is bool(expected)
    violations, stats = cpbs.run_check()
    assert _kinds(violations) == expected
    assert stats["largest_chunk"]["name"] == "chunk-vendor.js", (
        "反空轉：它必須把這個檔認成 chunk 而不是 tool"
    )


@pytest.mark.parametrize("n_files, expected", [
    (cpbs.TOTAL_LIMIT // cpbs.PER_TOOL_LIMIT, []),          # 40 檔：總和未超
    (cpbs.TOTAL_LIMIT // cpbs.PER_TOOL_LIMIT + 1, ["total-exceeded"]),
])
def test_total_limit_is_actually_compared(dist, n_files, expected):
    """`TOTAL_LIMIT` 有沒有真的被比對。

    ⚠️ 每個檔都寫成**剛好等於** `PER_TOOL_LIMIT`，所以沒有任何一個單檔違規——
    這樣 `total-exceeded` 才是被隔離出來的那一條，不會被 per-tool 的噪音蓋住。
    """
    for i in range(n_files):
        _write(dist, f"tool{i}.js", cpbs.PER_TOOL_LIMIT)
    violations, stats = cpbs.run_check()
    assert stats["total_bytes"] == n_files * cpbs.PER_TOOL_LIMIT
    assert (stats["total_bytes"] > cpbs.TOTAL_LIMIT) is bool(expected), (
        f"前置條件不成立：{stats['total_bytes']} vs 上限 {cpbs.TOTAL_LIMIT}"
    )
    assert _kinds(violations) == expected


# ---------------------------------------------------------------------------
# classify() 的 `chunk-` 前綴——用行為驗，不是直接呼叫它
# ---------------------------------------------------------------------------


def test_the_chunk_prefix_routes_a_file_to_the_other_limit(dist):
    """⛔ **同樣的位元組數、只差檔名前綴，結論必須相反。**

    `PER_TOOL_LIMIT + 1` 對 tool 是違規，對 chunk 遠在 `SHARED_CHUNK_LIMIT` 之下。
    ⇒ 這一格直接驗行為而不是單獨呼叫 `classify()`：一支 `classify()` 正確、但
    上限套錯邊的實作，單元測 `classify()` 照樣全綠。
    """
    size = cpbs.PER_TOOL_LIMIT + 1
    assert size > cpbs.PER_TOOL_LIMIT and size < cpbs.SHARED_CHUNK_LIMIT, (
        "前置條件：這個尺寸必須只越過 tool 上限，不越過 chunk 上限"
    )

    _write(dist, "tool.js", size)
    assert _kinds(cpbs.run_check()[0]) == ["per-tool-exceeded"]

    (dist / "tool.js").unlink()
    _write(dist, "chunk-tool.js", size)
    assert _kinds(cpbs.run_check()[0]) == [], (
        "`chunk-` 前綴的檔套的應是 SHARED_CHUNK_LIMIT，不該報 per-tool 違規"
    )


# ---------------------------------------------------------------------------
# dist 不存在：回 violation 而不是丟例外
# ---------------------------------------------------------------------------


def test_a_missing_dist_dir_is_a_violation_not_a_crash(tmp_path, monkeypatch, argv, capsys):
    """`DIST_DIR` 不存在時回 `dist-missing` violation。

    ⚠️ 容易被漏掉，因為它不走 glob 那條路。而且它**必須**是 violation 而非例外：
    CI 要的是「沒 build 就擋下來並說人話」，不是 traceback。
    """
    missing = tmp_path / "never-built"
    assert not missing.exists(), "前置條件：這個目錄必須真的不存在"
    monkeypatch.setattr(cpbs, "DIST_DIR", missing)

    violations, stats = cpbs.run_check()
    assert _kinds(violations) == ["dist-missing"]
    assert stats["file_count"] == 0

    argv("--ci")
    assert cpbs.main() == EXIT_VIOLATION, "沒 build 也要擋下 CI"
    out = capsys.readouterr().out
    assert "dist-missing" in out, "訊息要指名是哪一類問題，否則 CI log 讀不出所以然"
