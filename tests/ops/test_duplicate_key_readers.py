"""#4064: every reader of a file a declared input also reads refuses a key
(YAML) or a name (JSON) given twice, as the declared input does.

`_lib_input.decode_bytes` refuses it for every declared YAML / JSON input
(`tests/shared/test_caller_paths.py::test_a_declared_document_refuses_content_it_cannot_read`
runs that for each declaration). The same kinds of file had a second reading
that kept the last value in silence — one file, two answers:

* the metric dictionary: `migrate_rule.load_metric_dictionary` (and so
  `onboard`) and the `check_metric_dictionary` lint read it with
  `yaml.safe_load`, while analyze-gaps and generate-tenant-mapping refused it;
* a dashboard: `grafana-import --dashboard` refused it, `--dashboard-dir` and
  the ConfigMap check (`verify_dashboards`) read it with `json.loads`;
* a state file of `state-reconcile --state-dir` (selected `*.json`, as
  `--dashboard-dir`), read with `json.load`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parents[2] / "scripts" / "tools"
for _p in (_TOOLS, _TOOLS / "ops", _TOOLS / "lint"):
    sys.path.insert(0, str(_p))

import _lib_io  # noqa: E402
import check_metric_dictionary  # noqa: E402
import grafana_import  # noqa: E402
import migrate_rule  # noqa: E402

DUP_DICTIONARY = "mysql_up:\n  maps_to: a\nmysql_up:\n  maps_to: b\n"
DUP_DASHBOARD = b'{"title": "a", "title": "b", "panels": []}'


def test_migrate_rule_refuses_a_dictionary_with_a_key_twice(tmp_path):
    (tmp_path / "metric-dictionary.yaml").write_text(DUP_DICTIONARY, encoding="utf-8")
    with pytest.raises(_lib_io.YamlFileError) as exc:
        migrate_rule.load_metric_dictionary(str(tmp_path))
    assert "duplicate key 'mysql_up'" in str(exc.value)


@pytest.mark.parametrize("load", ["load_dictionary_metrics", "load_dictionary_golden_rules"])
def test_the_dictionary_lint_refuses_a_key_twice(tmp_path, load):
    path = tmp_path / "metric-dictionary.yaml"
    path.write_text(DUP_DICTIONARY, encoding="utf-8")
    with pytest.raises(_lib_io.YamlFileError) as exc:
        getattr(check_metric_dictionary, load)(path)
    assert exc.value.path == str(path)


def test_the_dictionary_lint_names_the_file_not_a_crash(tmp_path, monkeypatch, capsys):
    """The lint's own run: rc 2, one line naming the dictionary — not a
    traceback with `<unicode string>` for the file (#4064 review)."""
    from _lib_exitcodes import CallerError, exit_on_crash
    path = tmp_path / "metric-dictionary.yaml"
    path.write_text(DUP_DICTIONARY, encoding="utf-8")
    monkeypatch.setattr(check_metric_dictionary, "METRIC_DICT", path)
    monkeypatch.setattr(sys, "argv", ["check_metric_dictionary", "--ci"])
    with pytest.raises((SystemExit, CallerError)) as exc:
        check_metric_dictionary.main()
    if isinstance(exc.value, CallerError):
        with pytest.raises(SystemExit) as done:
            exit_on_crash(exc.value)
        code = done.value.code
    else:
        code = exc.value.code
    err = capsys.readouterr().err
    assert code == 2, err
    assert str(path) in err and "duplicate key 'mysql_up'" in err, err
    assert "Traceback" not in err and "unexpected internal error" not in err, err


def test_a_dashboard_file_of_a_directory_with_a_name_twice_is_refused(tmp_path):
    path = tmp_path / "d.json"
    path.write_bytes(DUP_DASHBOARD)
    [result] = grafana_import.import_dashboard(str(path), "cm", "monitoring", dry_run=True,
                                               raw=DUP_DASHBOARD)
    assert (result["status"], result["caller_error"]) == ("fail", True), result
    assert result["detail"] == ("Invalid JSON in d.json: not valid JSON: "
                                "the name 'title' is given twice in one object"), result


def test_a_dashboard_in_a_configmap_with_a_name_twice_fails_the_check(monkeypatch):
    item = {"metadata": {"name": "cm"}, "data": {"d.json": DUP_DASHBOARD.decode()}}
    monkeypatch.setattr(grafana_import, "kubectl_list", lambda *a, **k: [item])
    checks = grafana_import.verify_dashboards("monitoring")
    assert [(c["check"], c["status"]) for c in checks] == [("cm/cm/d.json", "fail")], checks


def test_a_state_file_with_a_name_twice_is_refused(tmp_path):
    """`state-reconcile --state-dir` selects `*.json` as `--dashboard-dir`
    does; its files are decoded as every caller file is (#4064 review)."""
    import state_reconcile
    path = tmp_path / "c1.json"
    path.write_text('{"schema_version": "0.1", "schema_version": "1.0"}', encoding="utf-8")
    data, err = state_reconcile.read_state(path)
    assert data is None
    assert err == "not valid JSON: the name 'schema_version' is given twice in one object"
