"""_lib_tenant_values：經 da-guard served-values 讀出 /metrics 實際發出的租戶值（#2115）。

測試用真的 da-guard：conftest 的 session 級 fixture `da_guard_binary` 以 `go build` 建到 tmp 目錄。建不起來
（含沒有 go）一律 fail、不 skip——這支 lib 的全部意義就是「值來自 Go」，
量不到 Go 的測試綠燈等於沒測。
"""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
from pathlib import Path

import pytest

#: Library mechanics on trees Go names paths on, as the judges use them
#: (`judging_run`, #3206); a tool run refuses such a tree instead.
pytestmark = pytest.mark.usefixtures("judging_run")
from _platform_fs import require_file_name, require_shebang_scripts, symlink_or_skip  # noqa: E402

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
    assert "DA_GUARD_BINARY" in msg and "scripts/ops/go_build.sh" in msg

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
    # #3140: a non-finite value is one the exporter refuses — it serves the
    # platform default (used to pin NaN / ±Inf served as written).
    got = tv.load_served_values(conf_d, binary=da_guard)["tenant-a"].values
    assert [(type(got[k]), got[k]) for k in ("mysql_connections", "redis_memory", "container_cpu")] == [
        (float, 80.0), (float, 70.0), (float, 75.0)]


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
    """檔名不是 UTF-8：da-guard 整棵樹拒收（#3206，不論內容），lib raise 已列出的例外並以跳脫後的檔名點名。"""
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": _DEFAULTS,
        "tenant-a.yaml": "tenants:\n  tenant-a:\n    mysql_connections: 70\n",
    })
    require_file_name(b"b\xff.yaml")
    (conf_d / os.fsdecode(b"b\xff.yaml")).write_bytes(b"tenants: [\n")
    with pytest.raises(tv.ServedValuesError) as ei:
        tv.load_served_values(conf_d, binary=da_guard)
    assert ei.value.returncode == 2
    assert '"b\\xff.yaml" is not valid UTF-8' in str(ei.value)


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
    fake.write_text("#!/bin/sh\necho '{\"schema\": \"da-guard.served-values/v1\", \"at\": \"x\", \"parse_failed\": [], \"tenants\": {}}'\n",
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
    fake.write_text("#!/bin/sh\necho '{\"schema\": \"da-guard.served-values/v1\", \"at\": \"x\", \"parse_failed\": [], \"skipped\": [], "
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

    def ident(severity: str, component: str = "mysql", metric: str = "connections") -> tuple:
        return (tv.Series("user_threshold", {"tenant": "tenant-a", "component": component,
                                             "metric": metric, "severity": severity},
                          f"{component}_{metric}", False, {}, {}),)
    warn, crit = ident("warning"), ident("critical")
    assert got.schedules["mysql_connections"] == tv.KeySchedule([
        S("00:00", "06:00", 1000.0, "warning", None, warn), S("06:00", "12:00", 70.0, "warning", None, warn),
        S("12:00", "13:00", None, None, reason="disabled"), S("13:00", "15:00", 70.0, "warning", None, warn),
        S("15:00", "16:00", 700.0, "critical", None, crit), S("16:00", "22:00", 70.0, "warning", None, warn),
        S("22:00", "24:00", 1000.0, "warning", None, warn),
    ], None, None)
    segs = got.schedules["mysql_connections"].segments
    assert all(isinstance(s.value, float) for s in segs if s.value is not None)
    # #2750: series 照 da-guard 給的讀；與 --at 那一刻的 series 同一份 Go 讀數。
    assert got.series["mysql_connections"] == warn
    # 過期：整天是平台預設；expires 照寫、expired 是 Go 在 --at 的判定。
    assert got.schedules["redis_memory"] == tv.KeySchedule(
        [S("00:00", "24:00", 90.0, "warning", None, ident("warning", "redis", "memory"))],
        "2026-01-01T00:00:00Z", True)
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
    fake = _old_da_guard(tmp_path, '{"schema": "da-guard.served-values/v1", "at": "x", "parse_failed": [], "skipped": [], '
                                   '"unreadable": [], "tenants": {}}')
    with pytest.raises(tv.ServedValuesError) as ei:
        tv.load_served_tree(tmp_path, binary=fake)
    assert "aliases" in str(ei.value)
    assert "older than this tool: upgrade or rebuild it" in str(ei.value)


def test_output_without_schedules_is_refused(tmp_path):
    fake = _old_da_guard(tmp_path, '{"schema": "da-guard.served-values/v1", "at": "x", "parse_failed": [], "skipped": [], "unreadable": [], '
                                   '"aliases": {}, "root_defaults_refused": [], "tenants": {"t": {"values": {}, "severities": {}, '
                                   '"series": {}, "unserved": {}, "dropped": {}, "state_filters": {}, "time_boxes": {}, "keys": {}}}}')
    with pytest.raises(tv.ServedValuesError) as ei:
        tv.load_served_tree(tmp_path, binary=fake, schedules=True)
    assert "schedules" in str(ei.value)
    assert "older than this tool: upgrade or rebuild it" in str(ei.value)


def test_output_without_series_is_refused(tmp_path):
    """#2750：da-guard 的輸出沒有 series → 這支 da-guard 比工具舊，不當成「沒有 series」讀。"""
    fake = _old_da_guard(tmp_path, '{"schema": "da-guard.served-values/v1", "at": "x", "parse_failed": [], "skipped": [], "unreadable": [], '
                                   '"aliases": {}, "root_defaults_refused": [], "tenants": {"t": {"values": {}, "severities": {}, '
                                   '"unserved": {}, "dropped": {}, "state_filters": {}, "time_boxes": {}, "keys": {}}}}')
    with pytest.raises(tv.ServedValuesError) as ei:
        tv.load_served_tree(tmp_path, binary=fake)
    assert "series" in str(ei.value) and ei.value.binary_fault
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
    fake = _old_da_guard(tmp_path, '{"schema": "da-guard.served-values/v1", "at": "x", "parse_failed": [], "skipped": [], "unreadable": [], '
                                   '"aliases": {}, "root_defaults_refused": [], "tenants": {"t": {"values": {}, "severities": {}, '
                                   '"series": {}, "unserved": {}, "dropped": {}, "state_filters": {}, "time_boxes": {}, "keys": {}}}}')
    assert tv.load_served_values(tmp_path, binary=fake)["t"].schedules is None


_ARGV_DOC = ('{"schema": "da-guard.served-values/v1", "at": "x", "parse_failed": [], "skipped": [], "unreadable": [], "aliases": {}, "root_defaults_refused": [], '
             '"tenants": {"t": {"values": {}, "severities": {}, "series": {}, "unserved": {}, '
             '"dropped": {}, "schedules": {}, "state_filters": {}, "time_boxes": {}, "keys": {}}}}')


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


# ── load_key_refs（#1822）─────────────────────────────────────────────────


def test_load_key_refs_names_every_spelling_the_exporter_assigns(tmp_path, da_guard):
    """legacy alias、`_critical`、維度鍵都由 exporter 判給 canonical metric；Python 不鏡射。"""
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_threads_running: 30\n",
        "t1.yaml": "tenants:\n  t1:\n    mysql_cpu: \"50\"\n    mysql_cpu_util: 1\n",
        "t2.yaml": "tenants:\n  t2:\n    mysql_cpu_critical: \"90\"\n    mysql_cpu{db='a'}: \"5\"\n",
    })
    tree = tv.load_key_refs(conf_d, ["mysql_threads_running"], binary=da_guard)
    assert tree.parse_failed == [] and tree.unreadable == [] and tree.unscanned == []
    assert [(r.file, r.owner, r.key) for r in tree.refs["mysql_threads_running"]] == [
        ("_defaults.yaml", "", "mysql_threads_running"),
        ("t1.yaml", "t1", "mysql_cpu"),
        ("t2.yaml", "t2", "mysql_cpu_critical"),
        ("t2.yaml", "t2", "mysql_cpu{db='a'}"),
    ]


def test_load_key_refs_returns_dropped_files_instead_of_raising(tmp_path, da_guard):
    """exit 3：exporter 丟掉的檔是呼叫端要讀的答案，不 raise ParseFailedError；stderr 整份保留。"""
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_threads_running: 30\noptional_overrides: 5\n",
        "t2.yaml": "tenants:\n  t2:\n    mysql_threads_running: \"40\"\n",
    })
    tree = tv.load_key_refs(conf_d, ["mysql_threads_running"], binary=da_guard)
    assert tree.parse_failed == ["_defaults.yaml"]
    assert [r.file for r in tree.refs["mysql_threads_running"]] == ["t2.yaml"]
    assert any("cannot unmarshal" in ln for ln in tree.stderr_lines), tree.stderr_lines


def test_load_key_refs_from_a_da_guard_without_the_subcommand_is_stale(tmp_path):
    """沒有 key-refs 的舊 da-guard（exit 2，`-h` 也 exit 2）被說成太舊，不是樹的錯。"""
    require_shebang_scripts()  # the stand-in da-guard below is a `#!` script
    fake = tmp_path / "old-da-guard"
    fake.write_text("#!/bin/sh\necho 'unexpected argument' >&2\nexit 2\n", encoding="utf-8")
    fake.chmod(0o755)
    with pytest.raises(tv.KeyRefsError) as ei:
        tv.load_key_refs(tmp_path, ["m"], binary=str(fake))
    assert ei.value.stale and ei.value.binary_fault
    assert ei.value.stderr_lines == ["unexpected argument"]


# ── load_custom_alert_pack（#3229）──────────────────────────────────────────

def test_load_custom_alert_pack_reads_the_pack_and_what_it_does_not_compile(tmp_path, da_guard):
    conf_d = _tree(tmp_path, {
        "t.yaml": "tenants:\n  t:\n    _custom_alerts:\n"
                  "      - {recipe: threshold, name: a, metric: m1, op: '>', window: 5m, threshold: '1'}\n"
                  "      - {recipe: threshold, name: b, metric: 'no-dash', op: '>', window: 5m, threshold: '1'}\n",
    })
    pack = tv.load_custom_alert_pack(conf_d, binary=da_guard)
    assert pack.shapes == 1 and [g["name"] for g in pack.groups][-1] == "custom-alerts"
    assert [(e.tenant, e.index, e.name, e.status) for e in pack.not_compiled] == [("t", 1, "b", "rejected")]
    assert pack.block_errors == {} and pack.lifecycle == [] and pack.parse_failed == []


def _fake_pack_binary(tmp_path, doc: dict):
    require_shebang_scripts()  # the stand-in da-guard below is a `#!` script
    out = tmp_path / "doc.json"
    out.write_text(json.dumps(doc), encoding="utf-8")
    fake = tmp_path / "fake-da-guard"
    fake.write_text(f"#!/bin/sh\ncat '{out}'\n", encoding="utf-8")
    fake.chmod(0o755)
    return str(fake)


_PACK_DOC = {"schema": tv.CUSTOM_ALERT_PACK_SCHEMA, "groups": [], "shapes": 0, "not_compiled": [],
             "block_errors": {}, "lifecycle": [], "parse_failed": [], "skipped": [], "unreadable": []}


@pytest.mark.parametrize("field,bad", [
    ("block_errors", ["t"]),                                              # not a mapping
    ("lifecycle", [{"recipe": "rate", "status": "eol", "tenants": "ab"}]),  # a string, not a list
    ("shapes", "1"),
    ("not_compiled", [{"tenant": "t", "index": 0, "name": "a", "status": "served", "reason": ""}]),
])
def test_load_custom_alert_pack_refuses_a_document_not_of_its_shape(tmp_path, field, bad):
    """A field not of its shape is output that is not da-guard's (`broken`),
    never a traceback, nor a string split into tenant names."""
    binary = _fake_pack_binary(tmp_path, dict(_PACK_DOC, **{field: bad}))
    with pytest.raises(tv.CustomAlertPackError) as ei:
        tv.load_custom_alert_pack(tmp_path, binary=binary)
    assert ei.value.broken, ei.value
    # control: the same document with the field of its shape is read.
    assert tv.load_custom_alert_pack(tmp_path, binary=_fake_pack_binary(tmp_path, _PACK_DOC)).shapes == 0


# ── ADR-037：schema 與 state_filters ────────────────────────────────────────

_SV_HEAD = '{"at": "x", "parse_failed": [], "skipped": [], "unreadable": [], "aliases": {}, "root_defaults_refused": [], '
_SV_TENANT = '"values": {}, "severities": {}, "series": {}, "unserved": {}, "dropped": {}, "time_boxes": {}, "keys": {}'


def _sv_doc(schema: str | None, tenant_extra: str = ', "state_filters": {}') -> str:
    head = _SV_HEAD if schema is None else _SV_HEAD.replace('{', '{"schema": "%s", ' % schema, 1)
    return head + '"tenants": {"t": {' + _SV_TENANT + tenant_extra + '}}}'


def test_real_da_guard_names_the_schema_this_reader_reads(tmp_path, da_guard):
    """真的 da-guard 寫的 schema 就是讀取端認的那個；不一致時讀取本身就會 raise。"""
    conf_d = _tree(tmp_path, {"t.yaml": "tenants:\n  t:\n    _silent_mode: disable\n"})
    proc = subprocess.run([da_guard, "served-values", "--config-dir", str(conf_d)],
                          capture_output=True, text=True, encoding="utf-8", check=True, timeout=120)
    assert json.loads(proc.stdout)["schema"] == tv.SERVED_VALUES_SCHEMA == "da-guard.served-values/v1"
    assert tv.load_served_values(conf_d, binary=da_guard)["t"].state_filters == {}


def test_output_without_schema_is_refused_as_older(tmp_path):
    """沒有 schema 的文件來自早於 ADR-037 的 da-guard：raise 並指出要升級，不靜默照舊讀。"""
    fake = _old_da_guard(tmp_path, _sv_doc(None))
    with pytest.raises(tv.ServedValuesError) as ei:
        tv.load_served_tree(tmp_path, binary=fake)
    assert "'schema'" in str(ei.value)
    assert "older than this tool: upgrade or rebuild it" in str(ei.value)
    assert ei.value.stale and ei.value.binary_fault


def test_output_with_an_unknown_schema_is_refused(tmp_path):
    """認不得的 schema（例如未來的 v2）：raise 並寫出兩個值，不當成舊版、也不照 v1 讀。"""
    fake = _old_da_guard(tmp_path, _sv_doc("da-guard.served-values/v2"))
    with pytest.raises(tv.ServedValuesError) as ei:
        tv.load_served_tree(tmp_path, binary=fake)
    msg = str(ei.value)
    assert "da-guard.served-values/v2" in msg and "da-guard.served-values/v1" in msg
    assert "older than this tool" not in msg
    assert ei.value.binary_fault and not ei.value.stale


def test_control_doc_with_the_schema_is_read(tmp_path):
    """對照組：同一份文件帶對的 schema 就讀得進來，上面兩個 raise 只來自 schema。"""
    fake = _old_da_guard(tmp_path, _sv_doc(tv.SERVED_VALUES_SCHEMA,
                                          ', "state_filters": {"crashloop": {"severity": "critical"}}'))
    assert tv.load_served_values(tmp_path, binary=fake)["t"].state_filters == {"crashloop": "critical"}


@pytest.mark.parametrize("extra", [
    "",                                                    # 欄位不存在
    ', "state_filters": null',
    ', "state_filters": []',
    ', "state_filters": {"crashloop": "critical"}',
    ', "state_filters": {"crashloop": {}}',
    ', "state_filters": {"crashloop": {"severity": ""}}',
    ', "state_filters": {"crashloop": {"severity": 1}}',
], ids=["missing", "null", "list", "bare-string", "no-severity", "empty-severity", "number"])
def test_state_filters_not_of_their_shape_are_refused(tmp_path, extra):
    """帶 v1 schema 卻沒有（或寫壞）state_filters：是壞掉的輸出，不是舊版，也不當成「沒有開啟的 filter」。"""
    fake = _old_da_guard(tmp_path, _sv_doc(tv.SERVED_VALUES_SCHEMA, extra))
    with pytest.raises(tv.ServedValuesError) as ei:
        tv.load_served_tree(tmp_path, binary=fake)
    assert "state_filters" in str(ei.value)
    assert ei.value.binary_fault and not ei.value.stale


def test_schema_refusal_exits_2_through_the_cli_decorator(tmp_path, capsys):
    """讀取端的 CLI 一律經 exit_on_served_values_error：schema 缺少或認不得都是 rc 2、一行 ERROR。"""
    for schema in (None, "da-guard.served-values/v2"):
        fake = _old_da_guard(tmp_path, _sv_doc(schema))

        @tv.exit_on_served_values_error
        def main():
            tv.load_served_values(tmp_path, binary=fake)

        with pytest.raises(SystemExit) as ei:
            main()
        assert ei.value.code == 2
        assert capsys.readouterr().err.startswith("ERROR: da-guard served-values exited 0 without the expected JSON")


def test_state_filter_severity_only_change_is_visible(tmp_path, da_guard):
    """ADR-037 的缺口：平台只改 state_filters 的 severity，/metrics 的 user_state_filter{severity}
    變了，served-values 的讀數也要跟著變；`_state_<filter>` 的 True/False 則兩邊相同。"""
    def tree(root: Path, sev: str) -> Path:
        return _tree(root, {
            "_defaults.yaml": "defaults:\n  mysql_connections: 80\nstate_filters:\n  crashloop:\n"
                              f"    reasons: [\"CrashLoopBackOff\"]\n    severity: {sev}\n",
            "a.yaml": "tenants:\n  a:\n    mysql_connections: 70\n",
            "b.yaml": "tenants:\n  b:\n    _state_crashloop: disable\n",
        })
    at = "2026-07-01T03:00:00Z"
    crit = tv.load_served_values(tree(tmp_path / "c", "critical"), at=at, binary=da_guard)
    warn = tv.load_served_values(tree(tmp_path / "w", "warning"), at=at, binary=da_guard)
    assert crit["a"].values["_state_crashloop"] is True and warn["a"].values["_state_crashloop"] is True
    assert crit["a"].state_filters == {"crashloop": "critical"}
    assert warn["a"].state_filters == {"crashloop": "warning"}
    assert crit != warn
    # 對照組：關掉 filter 的租戶沒有條目，兩邊相同
    assert crit["b"].state_filters == warn["b"].state_filters == {}
    assert crit["b"].values["_state_crashloop"] is False


def test_output_without_time_boxes_is_refused_as_older(tmp_path):
    fake = _old_da_guard(tmp_path, _sv_doc(tv.SERVED_VALUES_SCHEMA).replace(', "time_boxes": {}', ''))
    with pytest.raises(tv.ServedValuesError) as ei:
        tv.load_served_tree(tmp_path, binary=fake)
    assert "time_boxes" in str(ei.value) and ei.value.stale


@pytest.mark.parametrize("boxes", ['[]', '{"_silent_mode": {}}', '{"_silent_mode": [{"expires": "", "expired": false}]}',
                                   '{"_silent_mode": [{"expires": "x", "expired": "no"}]}'],
                         ids=["list", "not-a-list", "empty-expires", "expired-not-bool"])
def test_time_boxes_not_of_their_shape_are_refused(tmp_path, boxes):
    doc = _sv_doc(tv.SERVED_VALUES_SCHEMA).replace('"time_boxes": {}', f'"time_boxes": {boxes}')
    with pytest.raises(tv.ServedValuesError) as ei:
        tv.load_served_tree(tmp_path, binary=_old_da_guard(tmp_path, doc))
    assert "time_boxes" in str(ei.value) and ei.value.binary_fault and not ei.value.stale


def test_real_da_guard_reports_a_silence_time_box(tmp_path, da_guard):
    conf_d = _tree(tmp_path, {"t.yaml": "tenants:\n  t:\n    _silent_mode: {target: warning, expires: \"2027-01-01T00:00:00Z\"}\n"})
    got = tv.load_served_values(conf_d, at="2026-07-01T00:00:00Z", binary=da_guard)["t"].time_boxes
    assert got == {"_silent_mode": (tv.TimeBox("warning", "2027-01-01T00:00:00Z", False),)}


# ── ADR-038 決定 5：keys 與 routing ─────────────────────────────────────────

def test_output_without_keys_is_refused_as_older(tmp_path):
    """沒有 keys 的文件來自早於 ADR-038 第二步的 da-guard——它還把 `_routing` 放在
    values 裡：當成舊版拒讀，不靜默讀成「沒有任何鍵」。"""
    fake = _old_da_guard(tmp_path, _sv_doc(tv.SERVED_VALUES_SCHEMA).replace(', "keys": {}', ''))
    with pytest.raises(tv.ServedValuesError) as ei:
        tv.load_served_tree(tmp_path, binary=fake)
    assert "keys" in str(ei.value) and ei.value.stale


@pytest.mark.parametrize("keys", [
    '[]',
    '{"k": {}}',
    '{"k": {"exporter": "yes"}}',
    '{"k": {"exporter": true, "reason": "no_row"}}',
    '{"k": {"exporter": false}}',
    '{"k": {"exporter": false, "reason": "typo"}}',
    '{"k": {"exporter": false, "reason": "no_row", "registered": "no"}}',
    '{"k": {"exporter": false, "reason": "disabled", "disable": "maybe"}}',
    '{"k": {"exporter": false, "reason": "disabled", "disable": "noop", "served_family": ["k_critical"]}}',
    '{"k": {"exporter": true, "refused": "nan"}}',
    '{"k": {"exporter": false, "reason": "value_non_finite"}}',
    '{"k": {"exporter": false, "reason": "no_row", "refused": "non_finite"}}',
    '{"k": {"exporter": false, "reason": "root_default_refused"}}',
], ids=["list", "no-exporter", "exporter-not-bool", "read-with-reason", "unread-without-reason",
        "unknown-reason", "registered-not-bool", "unknown-disable", "family-without-partial",
        "unknown-refusal", "refused-reason-without-refusal", "refusal-without-refused-reason",
        "root-reason-without-refusal"])
def test_keys_not_of_their_shape_are_refused(tmp_path, keys):
    doc = _sv_doc(tv.SERVED_VALUES_SCHEMA).replace('"keys": {}', f'"keys": {keys}')
    with pytest.raises(tv.ServedValuesError) as ei:
        tv.load_served_tree(tmp_path, binary=_old_da_guard(tmp_path, doc))
    assert "keys" in str(ei.value) and ei.value.binary_fault and not ei.value.stale


def test_real_da_guard_names_a_refused_value_apart_from_a_disable(tmp_path, da_guard):
    """#3140: `refused` / `value_non_finite` per key and a no-row segment's
    `reason` are read as da-guard writes them (the facts PR-side readers use
    instead of re-reading the value)."""
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\noptional_overrides:\n  - oracle_process_count\n",
        "t.yaml": "tenants:\n  t:\n    mysql_connections: \"NaN\"\n    oracle_process_count: \"-Inf\"\n"
                  "    mysql_connections_critical:\n      default: \"95\"\n      overrides:\n"
                  "        - window: \"01:00-02:00\"\n          value: \"Inf\"\n"
                  "        - window: \"03:00-04:00\"\n          value: disable\n",
    })
    got = tv.load_served_values(conf_d, at="2026-07-01T12:00:00Z", binary=da_guard, schedules=True)["t"]
    assert got.keys["mysql_connections"] == tv.ServedKey(True, refused="non_finite")
    assert got.keys["oracle_process_count"] == tv.ServedKey(False, "value_non_finite", refused="non_finite")
    reasons = [(s.start, s.reason) for s in got.schedules["mysql_connections_critical"].segments
               if s.value is None]
    assert reasons == [("01:00", "value_non_finite"), ("03:00", "disabled")]


def test_real_da_guard_names_a_root_refused_default_the_tenant_does_not_write(tmp_path, da_guard):
    """#3140, #3250: a root `defaults:` value the resolver refuses leaves a
    tenant that does not write the key no row; `keys[]` names it
    (`root_default_refused`, `refused` why) instead of leaving the key out,
    so a reader does not take the lost row for a removal, and the tree names
    it once with the text written (`root_defaults_refused`)."""
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n  redis_memory: NaN\n  pg_connections: abc\n",
        "t.yaml": "tenants:\n  t:\n    mysql_connections: \"70\"\n",
    })
    tree = tv.load_served_tree(conf_d, binary=da_guard)
    got = tree.tenants["t"]
    assert got.keys["redis_memory"] == tv.ServedKey(False, "root_default_refused", refused="non_finite")
    assert got.keys["pg_connections"] == tv.ServedKey(False, "root_default_refused", refused="unparsed")
    assert "redis_memory" not in got.values and "redis_memory" not in (got.unserved or {})
    assert tree.root_defaults_refused == (tv.RootDefaultRefused("pg_connections", "abc", "unparsed"),
                                          tv.RootDefaultRefused("redis_memory", "NaN", "non_finite"))


def test_output_without_root_defaults_refused_is_refused_as_older(tmp_path):
    doc = _sv_doc(tv.SERVED_VALUES_SCHEMA).replace('"root_defaults_refused": [], ', '', 1)
    with pytest.raises(tv.ServedValuesError) as ei:
        tv.load_served_tree(tmp_path, binary=_old_da_guard(tmp_path, doc))
    assert "root_defaults_refused" in str(ei.value) and ei.value.stale


@pytest.mark.parametrize("entries", ['[{"key": "k", "text": "x"}]', '[{"key": "k", "text": "x", "refused": "typo"}]',
                                     '{"k": 1}'])
def test_root_defaults_refused_not_of_its_shape_is_refused(tmp_path, entries):
    doc = _sv_doc(tv.SERVED_VALUES_SCHEMA).replace('"root_defaults_refused": []', '"root_defaults_refused": ' + entries, 1)
    with pytest.raises(tv.ServedValuesError) as ei:
        tv.load_served_tree(tmp_path, binary=_old_da_guard(tmp_path, doc))
    assert "root_defaults_refused" in str(ei.value) and ei.value.binary_fault


@pytest.mark.parametrize("reason", ["typo", None])
def test_a_segment_reason_not_of_its_shape_is_refused(reason):
    seg = {"from": "00:00", "to": "24:00", "value": 5, "severity": "warning", "series": [
        {"name": "user_threshold", "labels": {}, "metric_key": "k", "legacy_twin": False,
         "dimensions": {}, "dimensions_regex": {}}], "reason": "disabled"}
    if reason is not None:
        seg = {"from": "00:00", "to": "24:00", "value": None, "reason": reason}
    with pytest.raises(ValueError):
        tv._key_schedule({"segments": [seg]})


def test_routing_not_a_mapping_is_refused(tmp_path):
    doc = _sv_doc(tv.SERVED_VALUES_SCHEMA, ', "state_filters": {}, "routing": "x"')
    with pytest.raises(tv.ServedValuesError) as ei:
        tv.load_served_tree(tmp_path, binary=_old_da_guard(tmp_path, doc))
    assert "routing" in str(ei.value) and ei.value.binary_fault


def test_real_da_guard_reports_keys_and_routing(tmp_path, da_guard):
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
        "t.yaml": "tenants:\n  t:\n    mysql_conections: 70\n    mysql_connections: 60\n"
                  "    _routing: {receiver: {type: webhook, url: 'https://h.example/t'}}\n",
    })
    got = tv.load_served_values(conf_d, binary=da_guard)["t"]
    assert got.keys["mysql_connections"] == tv.ServedKey(True)
    assert got.keys["mysql_conections"] == tv.ServedKey(False, "no_row")
    assert got.keys["_routing"] == tv.ServedKey(False, "not_exporter_key", True,
                                                {"generator": ("tenant", "platform_tenants")})
    assert "_routing" not in got.values
    assert got.routing["receiver"] == {"type": "webhook", "url": "https://h.example/t"}


def test_disable_effects_are_asked_for_and_read(tmp_path, da_guard):
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n  redis_memory: 70\n",
        "t.yaml": "tenants:\n  t:\n    mysql_conections: disable\n    mysql_connections: disable\n"
                  "    redis_memory: disable\n    redis_memory_critical: 90\n",
    })
    got = tv.load_served_values(conf_d, binary=da_guard, disable_effects=True)["t"].keys
    assert got["mysql_conections"].disable == "noop"
    assert got["mysql_connections"].disable == "effective"
    assert (got["redis_memory"].disable, got["redis_memory"].served_family) == \
        ("partial", ("redis_memory_critical",))
    # 對照：沒要求就不算（也不付重算的成本）
    plain = tv.load_served_values(conf_d, binary=da_guard)["t"].keys
    assert plain["mysql_conections"].disable is None


def test_custom_alerts_are_read_entry_by_entry(tmp_path, da_guard):
    """#3042：served-values 的 `custom_alerts` 依宣告順序讀成 CustomAlertEntry；
    整段解不開的租戶沒有條目，`custom_alerts_error` 說明原因。"""
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
        "a.yaml": "tenants:\n  ta:\n    _custom_alerts:\n"
                  "      - {recipe: threshold, name: a, metric: m1, op: '>', window: 5m, threshold: '80:critical'}\n"
                  "      - {recipe: threshold, name: b, metric: m2, op: '>', window: 5m, threshold: disable}\n",
        "b.yaml": "tenants:\n  tb:\n    _custom_alerts: 'not: [a list'\n",
    })
    tree = tv.load_served_tree(conf_d, binary=da_guard)
    ta = tree.tenants["ta"].custom_alerts
    assert [(e.index, e.status, e.severities) for e in ta] == [(0, "served", ("critical",)),
                                                              (1, "disabled", ("warning",))]
    assert ta[0].recipe_id == "threshold__m1__gt__w5m__for1m" and ta[0].spec["name"] == "a"
    assert tree.tenants["tb"].custom_alerts == () and tree.tenants["tb"].custom_alerts_error


def test_a_custom_alert_refused_for_its_threshold_carries_gos_class(tmp_path, da_guard):
    """#3209：exporter 因 `threshold:` 拒收的條目帶 Go 寫的 `refused`（non_finite／unparsed）；
    因其他欄位拒收的、停用的、送出的都沒有。"""
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
        "a.yaml": "tenants:\n  ta:\n    _custom_alerts:\n"
                  "      - {recipe: threshold, name: a, metric: m1, op: '>', window: 5m, threshold: 'NaN'}\n"
                  "      - {recipe: threshold, name: b, metric: m2, op: '>', window: 5m, threshold: abc}\n"
                  "      - {recipe: threshold, name: c, metric: m3, op: '>', window: 5x, threshold: '80'}\n"
                  "      - {recipe: threshold, name: d, metric: m4, op: '>', window: 5m, threshold: disable}\n"
                  "      - {recipe: threshold, name: e, metric: m5, op: '>', window: 5m, threshold: '80'}\n",
    })
    ta = tv.load_served_tree(conf_d, binary=da_guard).tenants["ta"].custom_alerts
    assert [(e.status, e.refused) for e in ta] == [
        ("rejected", "non_finite"), ("rejected", "unparsed"), ("rejected", None),
        ("disabled", None), ("served", None)]
    # #3234、#3235：Go 的判定逐筆帶著——門檻值拒收是 value_not_served、其他欄位拒收是
    # custom_alert_rejected；停用與送出的沒有。
    assert [e.finding for e in ta] == ["value_not_served", "value_not_served", "custom_alert_rejected",
                                       None, None]


def _custom_alerts_doc(entry: str) -> str:
    return ('{"schema": "da-guard.served-values/v1", "at": "x", "parse_failed": [], "skipped": [], '
            '"unreadable": [], "aliases": {}, "root_defaults_refused": [], "tenants": {"t": {"values": {}, "severities": {}, '
            '"series": {}, "unserved": {}, "dropped": {}, "state_filters": {}, "time_boxes": {}, '
            f'"keys": {{}}, "custom_alerts": [{entry}], "custom_alerts_finding": null}}}}}}')


@pytest.mark.parametrize("entry, why", [
    ('{"index": 0, "spec": {}, "status": "bogus", "finding": null}', "status"),
    ('{"index": 0, "spec": {}, "status": "served", "finding": null}', "recipe_id"),
    ('{"index": 0, "spec": {}, "status": "rejected", "refused": "bogus", "finding": "value_not_served"}', "refused"),
    ('{"index": 0, "spec": {}, "status": "disabled", "recipe_id": "r", "severities": ["warning"], '
     '"refused": "unparsed", "finding": null}', "refused"),
    ('{"index": 0, "spec": {}, "status": "served", "recipe_id": "r", "severities": ["warning"], '
     '"finding": "custom_alert_rejected"}', "finding"),
    ('{"index": 0, "spec": {}, "status": "rejected", "finding": 1}', "finding"),
], ids=["unknown-status", "served-without-recipe-id", "unknown-refused", "refused-not-rejected",
        "finding-on-served", "finding-not-a-string"])
def test_a_custom_alert_entry_not_of_its_shape_is_refused(tmp_path, entry, why):
    fake = _old_da_guard(tmp_path, _custom_alerts_doc(entry))
    with pytest.raises(tv.ServedValuesError) as ei:
        tv.load_served_tree(tmp_path, binary=fake)
    assert "custom alerts that are not of their shape" in str(ei.value) and why in str(ei.value)
    assert not ei.value.stale


def test_custom_alerts_without_a_finding_are_refused_as_older(tmp_path):
    """#3234、#3235：沒有 `finding` 的條目來自早於這個欄位的 da-guard——當成舊版拒讀，
    不靜默讀成「沒有 finding」（那會讓 validate-config 對不送的告警 PASS）。"""
    fake = _old_da_guard(tmp_path, _custom_alerts_doc('{"index": 0, "spec": {}, "status": "rejected"}'))
    with pytest.raises(tv.ServedValuesError) as ei:
        tv.load_served_tree(tmp_path, binary=fake)
    assert "finding" in str(ei.value) and ei.value.stale


def test_a_tenant_without_custom_alerts_finding_is_refused_as_older(tmp_path):
    """#3235：tenant 有 `custom_alerts` 卻沒有 `custom_alerts_finding`（舊版 da-guard）。"""
    doc = _custom_alerts_doc('{"index": 0, "spec": {}, "status": "rejected", "finding": "custom_alert_rejected"}')
    doc = doc.replace(', "custom_alerts_finding": null', '')
    with pytest.raises(tv.ServedValuesError) as ei:
        tv.load_served_tree(tmp_path, binary=_old_da_guard(tmp_path, doc))
    assert "finding" in str(ei.value) and ei.value.stale


@pytest.mark.parametrize("extra", ['"custom_alerts_finding": 1', '"custom_alerts_finding": "custom_alert_rejected"'],
                         ids=["not-a-string", "without-an-error"])
def test_custom_alerts_finding_not_of_its_shape_is_refused(tmp_path, extra):
    doc = _custom_alerts_doc('{"index": 0, "spec": {}, "status": "rejected", "finding": "custom_alert_rejected"}')
    doc = doc.replace('"custom_alerts_finding": null', extra)
    with pytest.raises(tv.ServedValuesError) as ei:
        tv.load_served_tree(tmp_path, binary=_old_da_guard(tmp_path, doc))
    assert "custom_alerts_finding" in str(ei.value) and not ei.value.stale


def test_partial_reads_the_rest_of_a_tree_with_a_file_that_does_not_decode(tmp_path, da_guard):
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
        "a.yaml": "tenants:\n  ta:\n    mysql_connections: 70\n",
        "bad.yaml": "tenants:\n  tb: [unclosed\n",
    })
    with pytest.raises(tv.ParseFailedError):
        tv.load_served_tree(conf_d, binary=da_guard)
    tree = tv.load_served_tree(conf_d, binary=da_guard, partial=True)
    assert "ta" in tree.tenants and tree.parse_failed == ["bad.yaml"]


# ── --tenant 子集模式（ADR-038 §5）──────────────────────────────────────────

def test_the_subset_readers_return_the_requested_tenants_and_the_absent(tmp_path, da_guard):
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
        "a.yaml": "tenants:\n  t-a:\n    mysql_connections: 50\n",
        "b.yaml": "tenants:\n  t-b:\n    mysql_connections: 60\n",
    })
    served = tv.load_served_tree(conf_d, binary=da_guard, tenants=iter(["t-b", "t-gone", "t-b"]))
    assert sorted(served.tenants) == ["t-b"] and served.absent == ("t-gone",)
    effective = tv.load_effective_tree(conf_d, binary=da_guard, tenants=["t-b", "t-gone"])
    assert sorted(effective.tenants) == ["t-b"] and effective.absent == ("t-gone",)
    whole = tv.load_served_tree(conf_d, binary=da_guard)
    assert sorted(whole.tenants) == ["t-a", "t-b"] and whole.absent == ()
    assert whole.tenants["t-b"].values == served.tenants["t-b"].values
    with pytest.raises(ValueError):
        tv.load_served_tree(conf_d, binary=da_guard, tenants=[])


@pytest.mark.parametrize("change", [
    {"tenants_requested": ["t-x"]},                             # not what was asked
    {"tenants_absent": ["t-a"]},                                # computed and absent at once
    {"tenants_absent": []},                                     # t-b neither computed nor absent
])
def test_a_subset_document_that_does_not_answer_the_request_is_refused(tmp_path, change):
    """A requested tenant missing from both `tenants` and `tenants_absent`
    would read as one the exporter does not serve: the document is not
    da-guard's (`broken`), never a reading."""
    require_shebang_scripts()  # the stand-in da-guard below is a `#!` script
    doc = json.loads(_sv_doc(tv.SERVED_VALUES_SCHEMA).replace('"t": {', '"t-a": {'))
    doc["tenants"]["t-a"].update({"keys": {}, "custom_alerts": []})
    doc.update({"tenants_requested": ["t-a", "t-b"], "tenants_absent": ["t-b"]}, **change)
    out = tmp_path / "doc.json"
    out.write_text(json.dumps(doc), encoding="utf-8")
    fake = tmp_path / "fake-da-guard"
    fake.write_text(f"#!/bin/sh\ncat '{out}'\n", encoding="utf-8")
    fake.chmod(0o755)
    with pytest.raises(tv.ServedValuesError) as ei:
        tv.load_served_tree(tmp_path, binary=str(fake), tenants=["t-a", "t-b"])
    assert ei.value.broken, ei.value


@pytest.mark.parametrize("tenants, error", [
    ("t-a", TypeError),          # a bare string: its characters are not the tenants
    (b"t-a", TypeError),
    ([1], TypeError),            # not str(1): a tenant id is a string
    ([None], TypeError),
    (["t-\udcff"], ValueError),  # not UTF-8: JSON cannot carry it back
    ([], ValueError),            # no tenant is not the whole tree
    (["t-a", ""], ValueError),
])
def test_a_request_that_is_not_tenant_ids_is_the_callers_mistake(tenants, error):
    with pytest.raises(error):
        tv.requested_tenants(tenants)


def test_a_request_is_sorted_and_deduplicated():
    assert tv.requested_tenants(iter(["t-b", "t-a", "t-b"])) == ["t-a", "t-b"]
    assert tv.requested_tenants(None) is None


def test_key_checks_of_the_wrong_shape_are_refused_not_read_as_none():
    """served-values' key_checks is the exporter's word: a document of the
    wrong shape (a field missing, a non-string message) is a broken da-guard,
    never "no message"."""
    assert tv._key_checks({"version_labels": ["a"], "renames": []}) == tv.KeyChecks(("a",), ())
    for bad in ({"version_labels": []}, {"version_labels": [1], "renames": []},
                {"version_labels": "a", "renames": []}, ["a"]):
        with pytest.raises((TypeError, KeyError)):
            tv._key_checks(bad)
