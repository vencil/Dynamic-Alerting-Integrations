"""#2883: offboard_tenant and generate_tenant_metadata read tenant-api's state
store (groups, views, federation subsets, the AccountID registry), not the
`_groups.yaml` & co. tenant-api stopped reading from conf.d in #2856."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
for sub in ("scripts/tools", "scripts/tools/ops", "scripts/tools/dx"):
    sys.path.insert(0, str(_ROOT / sub))

import _lib_tenant_state as lts  # noqa: E402
import offboard_tenant as ot  # noqa: E402
import generate_tenant_metadata as gtm  # noqa: E402

OFFBOARD = _ROOT / "scripts" / "tools" / "ops" / "offboard_tenant.py"

RECORDS = {
    "_groups.yaml": "groups:\n  g-fin:\n    label: Finance\n    members:\n      - db-a\n      - db-b\n"
                    "  g-other:\n    label: Other\n    members: [db-b]\n",
    "_views.yaml": "views:\n  v-a:\n    label: A only\n    filters:\n      tenant: db-a\n"
                   "  v-prod:\n    label: Prod\n    filters:\n      environment: prod\n",
    "_federation.db-a.yaml": "metrics:\n  - up\n",
    "_account_registry.yaml": "schema_version: v1\nnext_account_id: 1001\nallocations:\n  db-a: 1000\n",
}


def _state_dir(root: Path) -> Path:
    d = root / "state"
    d.mkdir()
    for k, v in RECORDS.items():
        (d / k).write_text(v, encoding="utf-8")
    (d / ".lock").write_text("", encoding="utf-8")
    return d


def _cm_yaml(root: Path) -> Path:
    lines = ["apiVersion: v1", "kind: ConfigMap", "metadata:", "  name: tenant-api-state", "data:"]
    for k, v in RECORDS.items():
        lines.append(f"  {k}: |")
        lines += [f"    {ln}" for ln in v.rstrip("\n").split("\n")]
    p = root / "state.yaml"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _cm_json(root: Path) -> Path:
    p = root / "state.json"
    p.write_text(json.dumps({"apiVersion": "v1", "kind": "ConfigMap", "data": RECORDS}), encoding="utf-8")
    return p


def _confd(root: Path, extra: dict[str, str] | None = None) -> Path:
    d = root / "conf.d"
    d.mkdir()
    (d / "db-a.yaml").write_text("tenants:\n  db-a:\n    mysql_connections: '70'\n", encoding="utf-8")
    for k, v in (extra or {}).items():
        (d / k).write_text(v, encoding="utf-8")
    return d


@pytest.mark.parametrize("form", ["dir", "yaml", "json"])
def test_three_inputs_read_alike(tmp_path, form):
    path = {"dir": _state_dir, "yaml": _cm_yaml, "json": _cm_json}[form](tmp_path)
    st = lts.load_state(path)
    assert st.records == RECORDS
    assert sorted(st.groups()) == ["g-fin", "g-other"]
    assert st.views()["v-a"]["filters"] == {"tenant": "db-a"}
    assert st.account_id("db-a") == 1000


def test_load_state_refuses_other_documents(tmp_path):
    p = tmp_path / "list.yaml"
    p.write_text("kind: List\nitems: []\n", encoding="utf-8")
    with pytest.raises(lts.StateInputError):
        lts.load_state(p)
    with pytest.raises(lts.StateInputError):
        lts.load_state(tmp_path / "missing.yaml")


def test_duplicate_key_in_a_record_is_an_error(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    (d / "_groups.yaml").write_text("groups:\n  g: {members: [a]}\n  g: {members: [b]}\n", encoding="utf-8")
    with pytest.raises(lts.YamlFileError, match=r"_groups\.yaml"):
        lts.load_state(d).groups()


@pytest.mark.parametrize("form", ["dir", "yaml", "json"])
def test_offboard_lists_every_state_reference(tmp_path, form):
    path = {"dir": _state_dir, "yaml": _cm_yaml, "json": _cm_json}[form](tmp_path)
    conf = _confd(tmp_path)
    can_proceed, report = ot.run_precheck("db-a", str(conf), lts.load_state(path))
    text = "\n".join(report)
    assert "群組 g-fin 的成員" in text and "PUT /api/v1/groups/g-fin" in text
    assert "g-other" not in text
    assert "view v-a 的 filters（tenant）" in text and "v-prod" not in text
    assert "federation 子集紀錄 _federation.db-a.yaml" in text
    assert "AccountID 配發 1000" in text
    assert can_proceed  # references are warnings: cleaned through the API after the file goes


def test_offboard_without_state_is_undetermined(tmp_path):
    conf = _confd(tmp_path)
    _, report = ot.run_precheck("db-a", str(conf), None)
    assert any("state store 引用：未判定" in ln for ln in report)


def test_offboard_execute_needs_state_or_explicit_skip(tmp_path):
    conf = _confd(tmp_path)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    run = lambda *a: subprocess.run(  # noqa: E731
        [sys.executable, str(OFFBOARD), "db-a", "--config-dir", str(conf), *a],
        capture_output=True, encoding="utf-8", timeout=60, env=env)
    r = run("--execute")
    assert r.returncode == 2, r.stdout + r.stderr
    assert (conf / "db-a.yaml").exists()
    r = run("--execute", "--skip-state-check")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (conf / "db-a.yaml").exists()


def test_offboard_does_not_answer_from_legacy_confd_groups(tmp_path):
    conf = _confd(tmp_path, {"_groups.yaml": "groups:\n  g: {members: [db-a]}\n"})
    configs = ot.load_all_configs(str(conf))
    assert "_groups.yaml" not in configs
    assert ot.check_cross_references("db-a", configs) == []


def test_metadata_custom_groups_from_state(tmp_path, capsys):
    conf = _confd(tmp_path, {"_groups.yaml": "groups:\n  stale: {members: [db-a]}\n"})
    groups = gtm._load_custom_groups(lts.load_state(_state_dir(tmp_path)), conf)
    assert sorted(groups) == ["g-fin", "g-other"]
    assert groups["g-fin"]["members"] == ["db-a", "db-b"]
    assert "is not read here" in capsys.readouterr().err
    assert gtm._load_custom_groups(None, conf) == {}


def _main(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["offboard_tenant.py", *argv])
    ot.main()


def test_offboard_main_with_state_executes(tmp_path, monkeypatch, capsys):
    conf = _confd(tmp_path)
    _main(monkeypatch, ["db-a", "--config-dir", str(conf), "--state", str(_cm_yaml(tmp_path)), "--execute"])
    out = capsys.readouterr().out
    assert "群組 g-fin 的成員" in out and "已刪除" in out
    assert not (conf / "db-a.yaml").exists()


def test_offboard_main_refuses_execute_without_state(tmp_path, monkeypatch):
    conf = _confd(tmp_path)
    with pytest.raises(SystemExit) as e:
        _main(monkeypatch, ["db-a", "--config-dir", str(conf), "--execute"])
    assert e.value.code == 2
    assert (conf / "db-a.yaml").exists()


def test_offboard_main_bad_state_is_a_caller_error(tmp_path, monkeypatch, capsys):
    conf = _confd(tmp_path)
    bad = tmp_path / "list.yaml"
    bad.write_text("kind: List\nitems: []\n", encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        _main(monkeypatch, ["db-a", "--config-dir", str(conf), "--state", str(bad)])
    assert e.value.code == 2
    assert "not a ConfigMap document" in capsys.readouterr().err


# F1: a copy that is not tenant-api's state store must not read as "no
# references" — offboard would then delete the tenant unchecked.
@pytest.mark.parametrize("name,body", [
    ("tc.yaml", "kind: ConfigMap\ndata:\n  db-a.yaml: |\n    tenants: {db-a: {}}\n"),
    ("fed.yaml", "kind: ConfigMap\ndata:\n  store.json: '{}'\n"),
    ("empty.yaml", "kind: ConfigMap\nmetadata: {name: tenant-api-state}\n"),
])
def test_load_state_refuses_foreign_or_empty_configmaps(tmp_path, name, body):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    with pytest.raises(lts.StateInputError):
        lts.load_state(p)


def test_load_state_refuses_confd_and_empty_dirs(tmp_path):
    conf = _confd(tmp_path)
    with pytest.raises(lts.StateInputError, match="not tenant-api state records"):
        lts.load_state(conf)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(lts.StateInputError, match="no records"):
        lts.load_state(empty)


def test_offboard_execute_with_wrong_configmap_deletes_nothing(tmp_path, monkeypatch, capsys):
    conf = _confd(tmp_path)
    wrong = tmp_path / "tc.yaml"
    wrong.write_text("kind: ConfigMap\ndata:\n  db-a.yaml: |\n    tenants: {db-a: {}}\n", encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        _main(monkeypatch, ["db-a", "--config-dir", str(conf), "--state", str(wrong), "--execute"])
    assert e.value.code == 2
    assert (conf / "db-a.yaml").exists()


# F2: tenant-api imports conf.d's legacy files at startup while the store has
# no such record, so a reference there comes back after the next restart.
def test_offboard_reports_legacy_files_the_store_lacks(tmp_path):
    conf = _confd(tmp_path, {"_groups.yaml": "groups:\n  old: {members: [db-a]}\n"})
    (conf / "_federation").mkdir()
    (conf / "_federation" / "db-a.yaml").write_text("metrics: [up]\n", encoding="utf-8")
    st = tmp_path / "state"
    st.mkdir()
    (st / "_views.yaml").write_text("views: {}\n", encoding="utf-8")
    refs, _ = ot.check_state_references("db-a", lts.load_state(st), str(conf))
    whats = [w for w, _ in refs]
    assert any("_groups.yaml" in w and "下次開機會匯入" in w for w in whats), whats
    assert any("_federation/db-a.yaml" in w for w in whats), whats


def test_offboard_ignores_legacy_files_the_store_already_holds(tmp_path):
    conf = _confd(tmp_path, {"_groups.yaml": "groups:\n  old: {members: [db-a]}\n"})
    st = tmp_path / "state"
    st.mkdir()
    (st / "_groups.yaml").write_text("groups: {}\n", encoding="utf-8")
    refs, _ = ot.check_state_references("db-a", lts.load_state(st), str(conf))
    assert refs == []
