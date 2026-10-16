"""`da-tools explain-keys`（ADR-038 決定 9）：逐鍵列出來源、讀取端、判決與 disable 的作用，
並以 `--assert` 對照預期結果。以行程方式跑，與 pipeline 一樣讀結束碼。"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import explain_keys as ek

pytestmark = pytest.mark.usefixtures("da_guard_env")

_DEFAULTS = "defaults:\n  mysql_connections: 80\n  mysql_slave_lag: 30\n  redis_memory: 70\n"
_TENANT = ("tenants:\n  db-a:\n    mysql_conections: \"70\"\n    mysql_slave_lagg: disable\n"
           "    redis_memory: disable\n    redis_memory_critical: \"90\"\n    _namespaces: [prod-ns]\n")


def _run(*args):
    return subprocess.run([sys.executable, "-X", "utf8", ek.__file__, *map(str, args)],
                          capture_output=True, encoding="utf-8", errors="replace", timeout=300)


@pytest.fixture
def conf_d(tmp_path):
    d = tmp_path / "conf.d"
    d.mkdir()
    (d / "_defaults.yaml").write_text(_DEFAULTS, encoding="utf-8")
    (d / "db-a.yaml").write_text(_TENANT, encoding="utf-8")
    (d / "db-b.yaml").write_text("tenants:\n  db-b:\n    mysql_connections: 60\n", encoding="utf-8")
    return d


def _json(p):
    doc = json.loads(p.stdout)
    assert doc["schema"] == ek.SCHEMA
    return doc


def test_the_table_lists_every_key_with_its_verdict(conf_d):
    p = _run("--config-dir", conf_d, "--tenant", "db-a")
    assert p.returncode == 0, p.stderr
    lines = p.stdout.splitlines()
    assert lines[0] == "db-a"
    row = {ln.split()[0]: ln.split() for ln in lines[2:] if ln.startswith("  ") and len(ln.split()) == 6}
    assert row["mysql_conections"][1:] == ["db-a.yaml", "—", "unread", "error", "—"]
    assert row["mysql_slave_lagg"][3:] == ["unread", "warning", "noop"]
    assert row["redis_memory"][2:] == ["exporter", "read", "—", "partial"]
    assert row["_namespaces"][2:4] == ["annotation", "read"]
    assert "db-b" not in p.stdout  # --tenant 只列那個租戶
    assert "warning: disable_partial: redis_memory in db-a.yaml; still served: redis_memory_critical" in p.stdout


def test_json_carries_value_and_served(conf_d):
    doc = _json(_run("--config-dir", conf_d, "--json"))
    assert doc["status"] == "ok" and set(doc["tenants"]) == {"db-a", "db-b"}
    e = {x["key"]: x for x in doc["tenants"]["db-b"]}["mysql_connections"]
    assert (e["value"], e["served"], e["layer"], e["file"]) == (60, 60.0, "tenant", "db-b.yaml")
    assert doc["assert_differences"] == []


def test_assert_passes_on_its_own_output_and_fails_on_a_difference(conf_d, tmp_path):
    golden = tmp_path / "golden.json"
    golden.write_text(_run("--config-dir", conf_d, "--json").stdout, encoding="utf-8")
    assert _run("--config-dir", conf_d, "--assert", golden).returncode == 0

    doc = json.loads(golden.read_text(encoding="utf-8"))
    for e in doc["tenants"]["db-a"]:
        if e["key"] == "mysql_conections":
            e["verdict"] = "read"
    golden.write_text(json.dumps(doc), encoding="utf-8")
    p = _run("--config-dir", conf_d, "--assert", golden)
    assert p.returncode == 1
    assert 'ASSERT: db-a: mysql_conections: verdict: expected "read", got "unread"' in p.stderr


def test_assert_compares_only_the_fields_given_but_every_key(conf_d, tmp_path):
    """預期檔可以只寫判決；但租戶的鍵要列全，多一個少一個都算不同。"""
    got = _json(_run("--config-dir", conf_d, "--json", "--tenant", "db-a"))
    slim = {"tenants": {"db-a": [{"key": e["key"], "verdict": e["verdict"]} for e in got["tenants"]["db-a"]]}}
    golden = tmp_path / "slim.json"
    golden.write_text(json.dumps(slim), encoding="utf-8")
    assert _run("--config-dir", conf_d, "--assert", golden).returncode == 0

    slim["tenants"]["db-a"] = [e for e in slim["tenants"]["db-a"] if e["key"] != "_namespaces"]
    golden.write_text(json.dumps(slim), encoding="utf-8")
    p = _run("--config-dir", conf_d, "--assert", golden, "--json")
    assert p.returncode == 1
    assert _json(p)["assert_differences"] == ["db-a: _namespaces: in the result, not expected"]


@pytest.mark.parametrize("entries, reason", [
    ([{"key": "mysql_connections", "verdcit": "read"}], "unknown field"),
    ([{"key": "mysql_connections", "verdict": "unread"}, {"key": "mysql_connections", "verdict": "read"}],
     "listed twice"),
    ("top-level", "unknown top-level"),
])
def test_an_assert_file_that_would_pass_vacuously_is_refused(conf_d, tmp_path, entries, reason):
    golden = tmp_path / "bad.json"
    doc = {"tenants": {"db-b": entries}}
    if entries == "top-level":
        doc = {"tenat": {"db-b": [{"key": "mysql_connections", "verdict": "unread"}]}, "tenants": {}}
    golden.write_text(json.dumps(doc), encoding="utf-8")
    p = _run("--config-dir", conf_d, "--assert", golden, "--json")
    assert p.returncode == 2 and reason in _json(p)["reason"]


@pytest.mark.parametrize("args, reason", [
    (["--tenant", "nope"], "no such tenant"),
    (["--assert", "/nonexistent/golden.json"], "--assert: cannot read"),
])
def test_caller_errors_exit_2_with_one_json_document(conf_d, args, reason):
    p = _run("--config-dir", conf_d, "--json", *args)
    assert p.returncode == 2
    doc = _json(p)
    assert doc["status"] == "caller_error" and reason in doc["reason"] and doc["tenants"] == {}


def test_a_missing_config_dir_is_a_caller_error(tmp_path):
    p = _run("--config-dir", tmp_path / "nope", "--json")
    assert p.returncode == 2 and _json(p)["status"] == "caller_error"


def test_undecidable_threshold_keys_exit_2(tmp_path):
    """沒讀到任何根平台檔：閾值鍵無從判斷，結果照樣印出，結束碼 2（決定 4、7）。"""
    d = tmp_path / "conf.d"
    d.mkdir()
    (d / "ta.yaml").write_text("tenants:\n  ta:\n    mysql_conections: 70\n", encoding="utf-8")
    p = _run("--config-dir", d, "--json")
    assert p.returncode == 2
    doc = _json(p)
    assert doc["undecidable"] is True
    assert doc["tenants"]["ta"][0]["verdict"] == "undecidable"
