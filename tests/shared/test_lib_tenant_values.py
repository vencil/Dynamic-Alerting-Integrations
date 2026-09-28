"""_lib_tenant_values：經 da-guard served-values 讀出 /metrics 實際發出的租戶值（#2115）。

測試用真的 da-guard：session 級 fixture 以 `go build` 建到 tmp 目錄。建不起來
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

import _lib_io
import _lib_tenant_values as tv
from _lib_io import YamlFileError

REPO_ROOT = Path(__file__).resolve().parents[2]
APP = REPO_ROOT / "components" / "threshold-exporter" / "app"


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
    assert ei.value.path == str(conf_d / "tenant-b.yaml")
    assert "tenant-b.yaml" in str(ei.value)


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
    (conf_d / os.fsdecode(b"b\xff.yaml")).write_bytes(b"tenants: [\n")
    with pytest.raises(tv.ServedValuesError) as ei:
        tv.load_served_values(conf_d, binary=da_guard)
    assert ei.value.returncode == 2
    assert 'parse_failed[0]: "b\\xff.yaml"' in str(ei.value)
