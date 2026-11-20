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
  the ConfigMap check (`verify_dashboards`) read it with `json.loads`.
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
    with pytest.raises(_lib_io.DuplicateKeyError):
        getattr(check_metric_dictionary, load)(path)


def test_a_dashboard_file_of_a_directory_with_a_name_twice_is_refused(tmp_path):
    path = tmp_path / "d.json"
    path.write_bytes(DUP_DASHBOARD)
    [result] = grafana_import.import_dashboard(str(path), "cm", "monitoring", dry_run=True,
                                               raw=DUP_DASHBOARD)
    assert (result["status"], result["caller_error"]) == ("fail", True), result
    assert "the name 'title' is given twice in one object" in result["detail"], result


def test_a_dashboard_in_a_configmap_with_a_name_twice_fails_the_check(monkeypatch):
    item = {"metadata": {"name": "cm"}, "data": {"d.json": DUP_DASHBOARD.decode()}}
    monkeypatch.setattr(grafana_import, "kubectl_list", lambda *a, **k: [item])
    checks = grafana_import.verify_dashboards("monitoring")
    assert [(c["check"], c["status"]) for c in checks] == [("cm/cm/d.json", "fail")], checks
