"""_lib_tenant_values：經 da-guard served-values 讀出 /metrics 實際發出的租戶值（#2115）。

測試用真的 da-guard：conftest 的 session 級 fixture `da_guard_binary` 以 `go build` 建到 tmp 目錄。建不起來
（含沒有 go）一律 fail、不 skip——這支 lib 的全部意義就是「值來自 Go」，
量不到 Go 的測試綠燈等於沒測。
"""
from __future__ import annotations

import math
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from _platform_fs import require_file_name, require_shebang_scripts, symlink_or_skip  # noqa: E402

import _lib_io
import _lib_tenant_values as tv
from _lib_io import YamlFileError

@pytest.fixture(scope="session")
def da_guard(da_guard_binary) -> str:
    """conftest 的 `da_guard_binary`：同一次 session build（沒有 go 或建不起來就 fail）。"""
    return da_guard_binary


def _tree(root: Path, files: dict[str, str]) -> Path:
    conf_d = root / "conf.d"
    for rel, body in files.items():
        p = conf_d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return conf_d


_DEFAULTS = "defaults:\n  mysql_connections: 80\n"
_CONTROL = "tenants:\n  tenant-b:\n    _silent_mode: disable\n"

# 同一個值（55）分別只寫在三個地方；tenant-b 是對照組，永遠吃 defaults 的 80。
_WHERE = {
    "defaults": {
        "_defaults.yaml": "defaults:\n  mysql_connections: 55\n",
        "tenant-a.yaml": "tenants:\n  tenant-a:\n    _silent_mode: disable\n",
        "tenant-b.yaml": "tenants:\n  tenant-b:\n    mysql_connections: 80\n",
    },
    "platform-tenants": {
        "_defaults.yaml": _DEFAULTS + "tenants:\n  tenant-a:\n    mysql_connections: 55\n",
        "tenant-a.yaml": "tenants:\n  tenant-a:\n    _silent_mode: disable\n",
        "tenant-b.yaml": _CONTROL,
    },
    "tenant-file": {
        "_defaults.yaml": _DEFAULTS,
        "tenant-a.yaml": "tenants:\n  tenant-a:\n    mysql_connections: 55\n",
        "tenant-b.yaml": _CONTROL,
    },
}


@pytest.mark.parametrize("where", sorted(_WHERE))
def test_value_is_read_wherever_it_is_written(where, tmp_path, da_guard):
    conf_d = _tree(tmp_path, _WHERE[where])
    got = tv.load_served_values(conf_d, binary=da_guard)
    assert got["tenant-a"].values["mysql_connections"] == 55
    assert got["tenant-a"].severities["mysql_connections"] == "warning"
    assert got["tenant-b"].values["mysql_connections"] == 80  # 對照組不變


def test_the_old_root_only_reader_misses_the_first_two(tmp_path):
    """對照：load_tenant_configs 只讀根目錄租戶檔，前兩種樹看不到 55，第三種看得到。
    這支測試釘住「為什麼需要這支 lib」；哪天它轉紅，代表舊 reader 已被修正。"""
    seen = {}
    for where in sorted(_WHERE):
        conf_d = _tree(tmp_path / where, _WHERE[where])
        seen[where] = _lib_io.load_tenant_configs(str(conf_d)).get("tenant-a", {}).get("mysql_connections")
    assert seen["defaults"] != 55 and seen["platform-tenants"] != 55, seen
    assert str(seen["tenant-file"]) == "55", seen


def test_at_is_passed_through(tmp_path, da_guard):
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": _DEFAULTS,
        "tenant-a.yaml": "tenants:\n  tenant-a:\n    mysql_connections:\n      default: \"70\"\n"
                         "      overrides:\n        - window: \"01:00-09:00\"\n          value: \"1000\"\n",
    })
    inside = tv.load_served_values(conf_d, at="2026-07-01T03:00:00Z", binary=da_guard)
    outside = tv.load_served_values(conf_d, at="2026-07-01T12:00:00Z", binary=da_guard)
    assert inside["tenant-a"].values["mysql_connections"] == 1000
    assert outside["tenant-a"].values["mysql_connections"] == 70


def test_disabled_value_is_unserved(tmp_path, da_guard):
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": _DEFAULTS,
        "tenant-a.yaml": "tenants:\n  tenant-a:\n    mysql_connections: disable\n",
    })
    got = tv.load_served_values(conf_d, binary=da_guard)["tenant-a"]
    assert "mysql_connections" not in got.values
    assert got.unserved == {"mysql_connections": "disable"}
    assert got.tenant_id == "tenant-a"


def test_parse_failed_raises_yaml_file_error_naming_the_file(tmp_path, da_guard):
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": _DEFAULTS,
        "tenant-a.yaml": "tenants:\n  tenant-a:\n    mysql_connections: 70\n",
        "tenant-b.yaml": "tenants:\n  tenant-b: [1]\n",
    })
    with pytest.raises(YamlFileError) as ei:
        tv.load_served_values(conf_d, binary=da_guard)
    assert isinstance(ei.value, tv.ParseFailedError)
    assert ei.value.path == str(conf_d / "tenant-b.yaml")
    assert "tenant-b.yaml" in str(ei.value)
    assert "\n" not in str(ei.value)  # YamlFileError 的單行契約不變
    # da-guard 的 stderr 整份附在 stderr_lines（exporter 的解析原因在其中），不篩選。
    assert any("cannot unmarshal" in ln for ln in ei.value.stderr_lines), ei.value.stderr_lines
    assert any("cannot be decoded" in ln for ln in ei.value.stderr_lines), ei.value.stderr_lines


def test_nonzero_exit_raises_with_stderr(tmp_path, da_guard):
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": _DEFAULTS,
        "a.yaml": "tenants:\n  tenant-a:\n    mysql_connections: 70\n",
        "b.yaml": "tenants:\n  tenant-a:\n    mysql_connections: 71\n",
    })
    with pytest.raises(tv.ServedValuesError) as ei:
        tv.load_served_values(conf_d, binary=da_guard)
    assert ei.value.returncode == 2
    assert "duplicate tenant" in ei.value.stderr
    assert "duplicate tenant" in str(ei.value)


def test_missing_binary_raises_with_install_hint(tmp_path, monkeypatch):
    monkeypatch.setenv("DA_LANG", "en")
    monkeypatch.delenv("DA_GUARD_BINARY", raising=False)
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(tv.DaGuardNotFoundError) as ei:
        tv.load_served_values(tmp_path)
    msg = str(ei.value)
    assert "da-guard binary not found" in msg
    assert "DA_GUARD_BINARY" in msg and "go build" in msg

    with pytest.raises(tv.DaGuardNotFoundError) as ei:
        tv.load_served_values(tmp_path, binary=str(tmp_path / "nope"))
    assert str(tmp_path / "nope") in str(ei.value)


def test_binary_resolution_order_env_then_path(tmp_path, monkeypatch, da_guard):
    conf_d = _tree(tmp_path, {"_defaults.yaml": _DEFAULTS,
                              "tenant-a.yaml": "tenants:\n  tenant-a:\n    mysql_connections: 70\n"})
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    monkeypatch.setenv("DA_GUARD_BINARY", da_guard)
    assert tv.load_served_values(conf_d)["tenant-a"].values["mysql_connections"] == 70
    monkeypatch.delenv("DA_GUARD_BINARY")
    monkeypatch.setattr(shutil, "which", lambda name: da_guard if name == "da-guard" else None)
    assert tv.load_served_values(conf_d)["tenant-a"].values["mysql_connections"] == 70


def test_every_exception_class_is_exported():
    """呼叫端要接的例外都得能從 __all__ 取到；新增例外卻漏加時轉紅。"""
    exceptions = {name for name, obj in vars(tv).items()
                  if isinstance(obj, type) and issubclass(obj, BaseException)
                  and not name.startswith("_")}
    assert exceptions, "no exception class found — the scan itself is broken"
    assert exceptions <= set(tv.__all__), sorted(exceptions - set(tv.__all__))
    assert {"YamlFileError", "DaGuardNotFoundError", "ServedValuesError"} <= exceptions


def test_env_var_name_is_the_dispatchers():
    """lib 與 `da-tools guard` 共用同一條解析路徑（同一個 dispatcher）。"""
    import guard_dispatch
    assert guard_dispatch.DISPATCHER.env_var == "DA_GUARD_BINARY"
    assert os.path.basename(guard_dispatch.DISPATCHER.binary_name) == "da-guard"


def test_threshold_values_are_floats(tmp_path, da_guard):
    """契約是 float：JSON 的整數（`70`）也要轉成 float，不能以 int 交出去。"""
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": _DEFAULTS,
        "tenant-a.yaml": "tenants:\n  tenant-a:\n    mysql_connections: 70\n    _custom_alerts:\n"
                         "      - {recipe: threshold, name: q, metric: qd, op: \">\", window: 5m, threshold: \"100:warning\"}\n",
    })
    got = tv.load_served_values(conf_d, binary=da_guard)["tenant-a"]
    v = got.values["mysql_connections"]
    assert type(v) is float and v == 70.0
    row = got.values["_custom_alerts"][0]
    assert type(row["value"]) is float and row["value"] == 100.0


def test_non_finite_thresholds_become_floats(tmp_path, da_guard):
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": _DEFAULTS + "  redis_memory: 70\n  container_cpu: 75\n",
        "tenant-a.yaml": "tenants:\n  tenant-a:\n    mysql_connections: NaN\n    redis_memory: \"+Inf\"\n"
                         "    container_cpu: \"-inf:critical\"\n",
    })
    got = tv.load_served_values(conf_d, binary=da_guard)["tenant-a"].values
    assert isinstance(got["mysql_connections"], float) and math.isnan(got["mysql_connections"])
    assert got["redis_memory"] == float("inf")
    assert got["container_cpu"] == float("-inf")


def test_subprocess_gets_a_timeout(tmp_path, monkeypatch, da_guard):
    """repo 規則：subprocess 一律帶 timeout；呼叫端給的值要原樣傳下去。"""
    seen = {}
    real_run = subprocess.run

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(tv.subprocess, "run", spy)
    conf_d = _tree(tmp_path, {"_defaults.yaml": _DEFAULTS,
                              "tenant-a.yaml": "tenants:\n  tenant-a:\n    mysql_connections: 70\n"})
    tv.load_served_values(conf_d, binary=da_guard)
    assert seen.get("timeout") == tv.DEFAULT_TIMEOUT
    tv.load_served_values(conf_d, binary=da_guard, timeout=7)
    assert seen.get("timeout") == 7


def test_dropped_rows_are_reported_apart(tmp_path, da_guard):
    """exporter 建不出 series 的列（例如 `__` 開頭的 label）不在 values，列在 dropped。"""
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": _DEFAULTS,
        "tenant-a.yaml": "tenants:\n  tenant-a:\n    mysql_connections{__x=\"x\"}: 5\n",
    })
    got = tv.load_served_values(conf_d, binary=da_guard)["tenant-a"]
    key = 'mysql_connections{__x="x"}'
    assert key not in got.values
    assert got.unserved[key] == "5"
    assert got.dropped[key] and "not a valid label name" in got.dropped[key][0]
    assert got.values["mysql_connections"] == 80.0


def test_dropped_rows_do_not_log_warn_on_stderr(tmp_path, da_guard):
    """被丟的列只進 JSON 的 dropped，不該在 da-guard 的 stderr 印出 WARN。"""
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": _DEFAULTS,
        "tenant-a.yaml": "tenants:\n  tenant-a:\n    mysql_connections{__name__=\"x\"}: 5\n"
                         "    mysql_connections{__x=\"y\"}: 4\n    mysql_connections{q=\"ok\"}: 6\n",
    })
    proc = subprocess.run([da_guard, "served-values", "--config-dir", str(conf_d)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert "WARN" not in proc.stderr, proc.stderr
    got = tv.load_served_values(conf_d, binary=da_guard)["tenant-a"]
    assert set(got.dropped) == {'mysql_connections{__name__="x"}', 'mysql_connections{__x="y"}'}


def test_non_utf8_file_name_raises_served_values_error(tmp_path, da_guard):
    """壞檔的檔名不是 UTF-8：da-guard 的 stderr 帶原始 bytes，lib 仍 raise 已列出的例外並點名該檔。"""
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": _DEFAULTS,
        "tenant-a.yaml": "tenants:\n  tenant-a:\n    mysql_connections: 70\n",
    })
    require_file_name(b"b\xff.yaml")
    (conf_d / os.fsdecode(b"b\xff.yaml")).write_bytes(b"tenants: [\n")
    with pytest.raises(tv.ServedValuesError) as ei:
        tv.load_served_values(conf_d, binary=da_guard)
    assert ei.value.returncode == 2
    assert 'parse_failed[0]: "b\\xff.yaml"' in str(ei.value)


# ── skipped：exporter 讀了、但不當租戶的檔（#2115 R3）────────────────────────

def test_skipped_names_files_that_declare_no_tenant(tmp_path, da_guard):
    """平面格式檔（無 `tenants:`）由 Go 判定、列進 skipped；lib 原樣交出檔名與原因。"""
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": _DEFAULTS,
        "tenant-a.yaml": "tenants:\n  tenant-a:\n    mysql_connections: 70\n",
        "flat-t.yaml": "mysql_connections: 5\n",
        "team/flat-u.yml": "mysql_connections: 6\n",
    })
    tree = tv.load_served_tree(conf_d, binary=da_guard)
    assert [s.file for s in tree.skipped] == ["flat-t.yaml", "team/flat-u.yml"]
    assert all(s.reason.startswith("declares no tenant") for s in tree.skipped)
    assert set(tree.tenants) == {"tenant-a"}  # 平面檔不是租戶


def test_skipped_is_empty_on_a_clean_tree(tmp_path, da_guard):
    conf_d = _tree(tmp_path, {"_defaults.yaml": _DEFAULTS,
                              "tenant-a.yaml": "tenants:\n  tenant-a:\n    mysql_connections: 70\n"})
    assert tv.load_served_tree(conf_d, binary=da_guard).skipped == []


def test_print_load_warnings_prints_one_named_line_per_file(tmp_path, da_guard, capsys):
    require_file_name("flat[31m.yaml")
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": _DEFAULTS,
        "tenant-a.yaml": "tenants:\n  tenant-a:\n    mysql_connections: 70\n",
        "flat\x1b[31m.yaml": "mysql_connections: 5\n",
    })
    tv.print_load_warnings(tv.load_served_tree(conf_d, binary=da_guard))
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 1, lines
    assert lines[0].startswith("WARN: flat") and ": declares no tenant" in lines[0]
    assert "\x1b" not in lines[0]  # 檔名來自樹，印到終端前已跳脫


def test_output_without_skipped_is_refused(tmp_path):
    """舊版 da-guard（JSON 沒有 skipped）：不靜默當成「沒有略過的檔」，而是 raise。"""
    require_shebang_scripts()  # the stand-in da-guard below is a `#!` script
    fake = tmp_path / "old-da-guard"
    fake.write_text("#!/bin/sh\necho '{\"at\": \"x\", \"parse_failed\": [], \"tenants\": {}}'\n",
                    encoding="utf-8")
    fake.chmod(0o755)
    with pytest.raises(tv.ServedValuesError) as ei:
        tv.load_served_tree(tmp_path, binary=str(fake))
    assert "skipped" in str(ei.value)
    assert "older than this tool: upgrade or rebuild it" in str(ei.value)


def test_output_without_unreadable_is_refused(tmp_path):
    """da-guard 的 JSON 沒有 unreadable（早於該欄位的版本）：不當成「每個檔都讀得到」，而是 raise。"""
    require_shebang_scripts()  # the stand-in da-guard below is a `#!` script
    fake = tmp_path / "old-da-guard"
    fake.write_text("#!/bin/sh\necho '{\"at\": \"x\", \"parse_failed\": [], \"skipped\": [], "
                    "\"tenants\": {}}'\n", encoding="utf-8")
    fake.chmod(0o755)
    with pytest.raises(tv.ServedValuesError) as ei:
        tv.load_served_tree(tmp_path, binary=str(fake))
    assert "unreadable" in str(ei.value)
    assert "older than this tool: upgrade or rebuild it" in str(ei.value)


def test_unreadable_raises_parse_failed_error_naming_file_and_reason(tmp_path, da_guard):
    """exporter 讀不到的檔（懸空 symlink）：raise ParseFailedError，path 指向該檔，
    訊息帶封閉值原因，`unreadable` 原樣交出。"""
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": _DEFAULTS,
        "tenant-a.yaml": "tenants:\n  tenant-a:\n    mysql_connections: 70\n",
    })
    symlink_or_skip("missing.yaml", conf_d / "tenant-b.yaml")
    with pytest.raises(tv.ParseFailedError) as ei:
        tv.load_served_tree(conf_d, binary=da_guard)
    assert ei.value.path == str(conf_d / "tenant-b.yaml")
    assert ei.value.unreadable == [tv.UnreadableFile("tenant-b.yaml", "stat_error")]
    assert "cannot read 1 path(s): tenant-b.yaml (stat_error)" in str(ei.value)


def test_exit_on_served_values_error_is_rc2_one_line(capsys):
    @tv.exit_on_served_values_error
    def main():
        raise tv.ServedValuesError("da-guard served-values exited 2", 2, "duplicate tenant x\n")

    with pytest.raises(SystemExit) as ei:
        main()
    assert ei.value.code == 2
    err = capsys.readouterr().err
    assert err.startswith("ERROR: da-guard served-values exited 2") and "duplicate tenant" in err


def test_da_guard_warn_lines_are_kept_on_a_successful_run(tmp_path, da_guard, capsys):
    """rc 0 時 da-guard 的 WARN（例如指向目錄的 symlink 讀不到）不被丟掉：
    收進 `warnings`，`print_load_warnings` 逐行跳脫後轉印。"""
    conf_d = _tree(tmp_path, {"_defaults.yaml": _DEFAULTS,
                              "tenant-a.yaml": "tenants:\n  tenant-a:\n    mysql_connections: 70\n"})
    (conf_d / "realdir").mkdir()
    symlink_or_skip("realdir", conf_d / "tb.yaml")
    tree = tv.load_served_tree(conf_d, binary=da_guard)
    assert len(tree.stderr_lines) == 1, tree.stderr_lines
    assert tree.stderr_lines[0].startswith("WARN: cannot read ") and "tb.yaml" in tree.stderr_lines[0]
    tv.print_load_warnings(tree)
    assert capsys.readouterr().err.splitlines() == [tv.DA_GUARD_PREFIX + ln for ln in tree.stderr_lines]


def test_missing_binary_message_names_only_what_these_tools_take(monkeypatch, capsys):
    """decorator 不轉印 dispatcher 的訊息（那段講 `da-tools guard` 的 --da-guard-binary 旗標）。"""
    monkeypatch.delenv("DA_GUARD_BINARY", raising=False)
    @tv.exit_on_served_values_error
    def main():
        raise tv.DaGuardNotFoundError("Error: da-guard binary not found.\nResolution order:\n  1. --da-guard-binary <path>")

    with pytest.raises(SystemExit) as ei:
        main()
    assert ei.value.code == 2
    err = capsys.readouterr().err
    assert err.splitlines() == [f"ERROR: {tv.MISSING_BINARY_MESSAGE}"]
    assert "--da-guard-binary" not in err and "v2.8.0" not in err
    assert "$DA_GUARD_BINARY" in err and "$PATH" in err and "/usr/local/bin/da-guard" in err


def test_da_guard_binary_env_naming_no_file_is_said_so(monkeypatch, tmp_path, capsys):
    """F5：`$DA_GUARD_BINARY` 有設但那個路徑沒有檔案時，訊息印出該值並說是它不存在。"""
    missing = tmp_path / "no-such-da-guard"
    monkeypatch.setenv("DA_GUARD_BINARY", str(missing))

    @tv.exit_on_served_values_error
    def main():
        tv.load_served_tree(tmp_path)

    with pytest.raises(SystemExit) as ei:
        main()
    assert ei.value.code == 2
    err = capsys.readouterr().err
    assert err.splitlines() == [err.rstrip("\n")], err  # 一行
    assert f"$DA_GUARD_BINARY is set to '{missing}'" in err
    assert "no file exists at that path" in err


def test_served_values_error_stderr_is_printed_line_by_line(capsys):
    @tv.exit_on_served_values_error
    def main():
        raise tv.ServedValuesError("da-guard served-values exited 2", 2, "first\nsecond \x1b[31m\n")

    with pytest.raises(SystemExit):
        main()
    assert capsys.readouterr().err.splitlines() == [
        "ERROR: da-guard served-values exited 2",
        tv.DA_GUARD_PREFIX + "first", tv.DA_GUARD_PREFIX + "second ?[31m"]


# --- schedules / aliases（#2115 (c)）：只解析 da-guard 給的，不解讀排程 ---

_SCHEDULED = {
    "_defaults.yaml": "defaults:\n  mysql_connections: 80\n  mysql_threads_running: 30\n  redis_memory: 90\n",
    "tenant-a.yaml": (
        "tenants:\n  tenant-a:\n"
        "    mysql_connections:\n      default: \"70\"\n      overrides:\n"
        "        - window: \"22:00-06:00\"\n          value: \"1000\"\n"
        "        - window: \"12:00-13:00\"\n          value: \"disable\"\n"
        "        - window: \"15:00-16:00\"\n          value: \"700:critical\"\n"
        "    mysql_cpu: 44\n"
        "    redis_memory:\n      default: \"95\"\n      expires: \"2026-01-01T00:00:00Z\"\n"
    ),
}


def test_schedules_are_parsed_as_da_guard_writes_them(tmp_path, da_guard):
    conf_d = _tree(tmp_path, _SCHEDULED)
    got = tv.load_served_values(conf_d, at="2026-07-01T03:00:00Z", binary=da_guard, schedules=True)["tenant-a"]
    S = tv.ScheduleSegment
    assert got.schedules["mysql_connections"] == tv.KeySchedule([
        S("00:00", "06:00", 1000.0, "warning"), S("06:00", "12:00", 70.0, "warning"),
        S("12:00", "13:00", None, None), S("13:00", "15:00", 70.0, "warning"),
        S("15:00", "16:00", 700.0, "critical"), S("16:00", "22:00", 70.0, "warning"),
        S("22:00", "24:00", 1000.0, "warning"),
    ], None, None)
    segs = got.schedules["mysql_connections"].segments
    assert all(isinstance(s.value, float) for s in segs if s.value is not None)
    # 過期：整天是平台預設；expires 照寫、expired 是 Go 在 --at 的判定。
    assert got.schedules["redis_memory"] == tv.KeySchedule(
        [S("00:00", "24:00", 90.0, "warning")], "2026-01-01T00:00:00Z", True)
    # --at 那一刻的 values 與 schedules 對應段一致（同一份 Go 讀數）。
    assert got.values["mysql_connections"] == 1000


def test_ungatherable_segment_is_parsed_with_its_error(tmp_path, da_guard):
    """--at 以外的時段 /metrics Gather 不起來：da-guard 不 exit 2，該段帶 error；這裡只照搬。"""
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
        "tenant-a.yaml": (
            "tenants:\n  tenant-a:\n    mysql_connections_critical: 95\n"
            "    mysql_connections:\n      default: \"70\"\n      overrides:\n"
            "        - window: \"15:00-16:00\"\n          value: \"700:critical\"\n"),
    })
    got = tv.load_served_values(conf_d, at="2026-07-01T03:00:00Z", binary=da_guard, schedules=True)["tenant-a"]
    segs = got.schedules["mysql_connections"].segments
    assert [s[:4] for s in segs] == [("00:00", "15:00", 70.0, "warning"), ("15:00", "16:00", None, None),
                                     ("16:00", "24:00", 70.0, "warning")]
    assert segs[0].error is None and segs[2].error is None
    assert segs[1].error and "HTTP 500" in segs[1].error


def test_aliases_are_the_exporters_table(tmp_path, da_guard):
    """舊拼法寫的 key 經 da-guard 給的別名表找得到；表不在 Python 端維護。"""
    conf_d = _tree(tmp_path, _SCHEDULED)
    tree = tv.load_served_tree(conf_d, binary=da_guard, schedules=True)
    assert "mysql_cpu" in tree.aliases
    canon = tree.aliases["mysql_cpu"]
    assert tree.tenants["tenant-a"].values[canon] == 44
    assert "mysql_cpu" not in tree.tenants["tenant-a"].schedules
    assert canon in tree.tenants["tenant-a"].schedules


def _old_da_guard(tmp_path: Path, doc: str) -> str:
    require_shebang_scripts()  # the stand-in da-guard is a `#!` script
    fake = tmp_path / "old-da-guard"
    fake.write_text(f"#!/bin/sh\necho '{doc}'\n", encoding="utf-8")
    fake.chmod(0o755)
    return str(fake)


def test_output_without_aliases_is_refused(tmp_path):
    fake = _old_da_guard(tmp_path, '{"at": "x", "parse_failed": [], "skipped": [], '
                                   '"unreadable": [], "tenants": {}}')
    with pytest.raises(tv.ServedValuesError) as ei:
        tv.load_served_tree(tmp_path, binary=fake)
    assert "aliases" in str(ei.value)
    assert "older than this tool: upgrade or rebuild it" in str(ei.value)


def test_output_without_schedules_is_refused(tmp_path):
    fake = _old_da_guard(tmp_path, '{"at": "x", "parse_failed": [], "skipped": [], "unreadable": [], '
                                   '"aliases": {}, "tenants": {"t": {"values": {}, "severities": {}, '
                                   '"unserved": {}, "dropped": {}}}}')
    with pytest.raises(tv.ServedValuesError) as ei:
        tv.load_served_tree(tmp_path, binary=fake, schedules=True)
    assert "schedules" in str(ei.value)
    assert "older than this tool: upgrade or rebuild it" in str(ei.value)


def test_schedules_are_asked_for_only_on_request(tmp_path, da_guard):
    """不帶 schedules=True 時不傳 --schedules：schedules 為 None，其餘讀數與帶旗標時相同。"""
    conf_d = _tree(tmp_path, _SCHEDULED)
    plain = tv.load_served_tree(conf_d, at="2026-07-01T03:00:00Z", binary=da_guard)
    full = tv.load_served_tree(conf_d, at="2026-07-01T03:00:00Z", binary=da_guard, schedules=True)
    assert all(t.schedules is None for t in plain.tenants.values())
    assert all(t.schedules for t in full.tenants.values())
    assert plain.aliases == full.aliases and plain.aliases
    assert {k: t._replace(schedules=None) for k, t in full.tenants.items()} == plain.tenants


def test_output_without_schedules_is_fine_when_not_asked_for(tmp_path):
    fake = _old_da_guard(tmp_path, '{"at": "x", "parse_failed": [], "skipped": [], "unreadable": [], '
                                   '"aliases": {}, "tenants": {"t": {"values": {}, "severities": {}, '
                                   '"unserved": {}, "dropped": {}}}}')
    assert tv.load_served_values(tmp_path, binary=fake)["t"].schedules is None


_ARGV_DOC = ('{"at": "x", "parse_failed": [], "skipped": [], "unreadable": [], "aliases": {}, '
             '"tenants": {"t": {"values": {}, "severities": {}, "unserved": {}, "dropped": {}, '
             '"schedules": {}}}}')


@pytest.mark.parametrize("asked", [False, True])
def test_schedules_flag_is_passed_only_when_asked_for(tmp_path, asked):
    """假 da-guard 把 argv 逐行寫進檔案：schedules=False 時不帶 --schedules，True 時帶。"""
    require_shebang_scripts()  # the stand-in da-guard is a `#!` script
    argv = tmp_path / "argv"
    fake = tmp_path / "argv-da-guard"
    fake.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > '{argv}'\necho '{_ARGV_DOC}'\n", encoding="utf-8")
    fake.chmod(0o755)
    tv.load_served_tree(tmp_path, at="2026-07-01T03:00:00Z", binary=str(fake), schedules=asked)
    got = argv.read_text(encoding="utf-8").splitlines()
    assert got[0] == tv.SUBCOMMAND and "--config-dir" in got  # 確定讀到的是這次的 argv
    assert ("--schedules" in got) is asked


def test_da_guard_without_the_schedules_flag_is_named_as_too_old(tmp_path):
    """舊版 da-guard 不認得 --schedules（Go flag：exit 2）：錯誤訊息與其他缺欄位時一樣指出要升級。"""
    require_shebang_scripts()  # the stand-in da-guard is a `#!` script
    fake = tmp_path / "pre-schedules-da-guard"
    fake.write_text("#!/bin/sh\necho 'flag provided but not defined: -schedules' >&2\nexit 2\n",
                    encoding="utf-8")
    fake.chmod(0o755)
    with pytest.raises(tv.ServedValuesError) as ei:
        tv.load_served_tree(tmp_path, binary=str(fake), schedules=True)
    assert ei.value.returncode == 2
    assert "older than this tool: upgrade or rebuild it" in str(ei.value)
    assert ei.value.stale and ei.value.binary_fault
    # 其他 exit 2（同一組 argv 加 `-h` 回 0 的 da-guard：認得子命令與旗標）不被說成「太舊」
    # ——判定看 `-h` 的結束碼，不看 stderr 的字（#2725）。
    other = tmp_path / "other-da-guard"
    other.write_text('#!/bin/sh\nfor a in "$@"; do [ "$a" = -h ] && exit 0; done\n'
                     "echo 'flag provided but not defined: -schedules' >&2\nexit 2\n",
                     encoding="utf-8")
    other.chmod(0o755)
    with pytest.raises(tv.ServedValuesError) as ei:
        tv.load_served_tree(tmp_path, binary=str(other), schedules=True)
    assert "older than this tool" not in str(ei.value)
    assert not ei.value.binary_fault
