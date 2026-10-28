"""#2883 / ADR-036 §12: offboard_tenant and generate_tenant_metadata read
tenant-api's state records (groups, views, federation subsets, the AccountID
registry) as tenant-api reads them — the JSON `tenant-api state show` prints —
not the `_groups.yaml` & co. tenant-api stopped reading from conf.d in #2856,
and not the records' YAML read again in Python.

Every state document here is printed by a tenant-api built from this checkout
(`state_show` fixture, tests/conftest.py), never hand-written, except where a
test feeds a malformed document on purpose."""
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

#: Records tenant-api refuses, by the record they break.
BAD = {
    "_groups.yaml": "groups:\n  g-fin:\n    members: abc\n",
    "_views.yaml": "views:\n  v: {label: a, label: b}\n",
    "_federation.db-a.yaml": "metrics: up\n",
    "_federation_policy.yaml": "whitelist: up\n",
    "_account_registry.yaml": "schema_version: v1\nnext_account_id: 1000\nallocations:\n  db-a: 1000\n",
}


def _confd(root: Path, extra: dict[str, str] | None = None) -> Path:
    d = root / "conf.d"
    d.mkdir()
    (d / "db-a.yaml").write_text("tenants:\n  db-a:\n    mysql_connections: '70'\n", encoding="utf-8")
    for k, v in (extra or {}).items():
        (d / k).write_text(v, encoding="utf-8")
    return d


def test_state_show_document_is_read(state_show):
    st = lts.load_state(state_show(RECORDS))
    assert st.keys == sorted(RECORDS)
    assert sorted(st.groups()) == ["g-fin", "g-other"]
    assert st.views()["v-a"]["filters"] == {"tenant": "db-a"}
    assert st.account_id("db-a") == 1000
    assert st.tenant_api_version


def test_state_from_stdin(state_show, monkeypatch):
    import io
    raw = Path(state_show(RECORDS)).read_bytes()
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw)))
    assert lts.load_state("-").keys == sorted(RECORDS)


@pytest.mark.parametrize("key", sorted(BAD))
def test_a_problem_never_reads_as_empty(state_show, key):
    st = lts.load_state(state_show({**RECORDS, key: BAD[key]}))
    prob = st.problem(key)
    assert prob is not None and prob.kind in lts.PROBLEM_KINDS and prob.message
    assert st.has(key)
    with pytest.raises(lts.RecordProblem, match=key.replace(".", r"\.")):
        st.record(key)
    if key == "_groups.yaml":
        with pytest.raises(lts.RecordProblem):
            st.groups()
    if key == "_views.yaml":
        assert prob.kind == "unreadable"  # a key written twice: parse-tree's verdict
        with pytest.raises(lts.RecordProblem):
            st.views()


# ── the old inputs and other documents are refused, naming the commands ──

def _cm_yaml(root: Path) -> Path:
    lines = ["apiVersion: v1", "kind: ConfigMap", "metadata:", "  name: tenant-api-state", "data:"]
    for k, v in RECORDS.items():
        lines.append(f"  {k}: |")
        lines += [f"    {ln}" for ln in v.rstrip("\n").split("\n")]
    p = root / "state.yaml"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _assert_names_the_commands(msg: str) -> None:
    assert "kubectl exec deploy/tenant-api -c tenant-api -- tenant-api state show" in msg, msg
    assert "docker run -i --rm" in msg and "state show --from -" in msg, msg
    assert f"v{lts.MIN_TENANT_API_VERSION}" in msg, msg


@pytest.mark.parametrize("form", ["configmap-yaml", "configmap-json", "state-dir"])
def test_old_inputs_are_refused_with_the_commands(tmp_path, form):
    if form == "configmap-yaml":
        path = _cm_yaml(tmp_path)
    elif form == "configmap-json":
        path = tmp_path / "cm.json"
        path.write_text(json.dumps({"apiVersion": "v1", "kind": "ConfigMap", "data": RECORDS}),
                        encoding="utf-8")
    else:
        path = tmp_path / "state"
        path.mkdir()
        for k, v in RECORDS.items():
            (path / k).write_text(v, encoding="utf-8")
    with pytest.raises(lts.StateInputError) as e:
        lts.load_state(path)
    _assert_names_the_commands(str(e.value))


@pytest.mark.parametrize("doc", [
    {"schema": "tenant-api.state-show/v2", "records": []},
    {"records": [{"key": "_groups.yaml", "record": {"groups": {}}, "problem": None}]},
    [],
])
def test_unknown_schema_is_refused(tmp_path, doc):
    p = tmp_path / "s.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(lts.StateInputError) as e:
        lts.load_state(p)
    _assert_names_the_commands(str(e.value))


@pytest.mark.parametrize("records", [
    [{"key": "_groups.yaml", "record": None, "problem": None}],
    [{"key": "_groups.yaml", "record": {"groups": {}}, "problem": {"kind": "rejected", "message": "x"}}],
    [{"key": "_groups.yaml", "record": None, "problem": {"kind": "other", "message": "x"}}],
    [{"key": "db-a.yaml", "record": {}, "problem": None}],
    [{"key": "_groups.yaml", "record": {}, "problem": None}] * 2,
])
def test_malformed_entries_are_refused(tmp_path, records):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"schema": lts.STATE_SHOW_SCHEMA, "records": records}), encoding="utf-8")
    with pytest.raises(lts.StateInputError):
        lts.load_state(p)


def test_empty_records_are_refused(tmp_path, tenant_api_binary):
    # An empty store (a backup with no records is refused by tenant-api itself).
    store = tmp_path / "state"
    store.mkdir()
    proc = subprocess.run([tenant_api_binary, "state", "show", "--state-store", "dir",
                           "--state-dir", str(store), "--config-dir", str(tmp_path / "conf.d")],
                          capture_output=True, check=False, timeout=60)
    assert proc.returncode == 0, proc.stderr
    p = tmp_path / "state.json"
    p.write_bytes(proc.stdout)
    assert json.loads(proc.stdout)["records"] == []
    with pytest.raises(lts.StateInputError, match="no records"):
        lts.load_state(p)


def test_missing_file_is_refused(tmp_path):
    with pytest.raises(lts.StateInputError):
        lts.load_state(tmp_path / "missing.json")


# ── offboard_tenant ─────────────────────────────────────────────────────

def test_offboard_lists_every_state_reference(tmp_path, state_show):
    conf = _confd(tmp_path)
    can_proceed, report = ot.run_precheck("db-a", str(conf), lts.load_state(state_show(RECORDS)))
    text = "\n".join(report)
    assert "群組 g-fin 的成員" in text and "PUT /api/v1/groups/g-fin" in text
    assert "g-other" not in text
    assert "view v-a 的 filters（tenant）" in text and "v-prod" not in text
    assert "federation 子集紀錄 _federation.db-a.yaml" in text
    assert "AccountID 配發 1000" in text
    assert "未判定" not in text
    assert can_proceed  # references are warnings: cleaned through the API after the file goes


def test_offboard_without_state_is_undetermined(tmp_path):
    conf = _confd(tmp_path)
    _, report = ot.run_precheck("db-a", str(conf), None)
    assert any("state store 引用：未判定" in ln for ln in report)


@pytest.mark.parametrize("key", ["_groups.yaml", "_views.yaml", "_federation.db-a.yaml"])
def test_offboard_names_a_used_record_with_a_problem(tmp_path, state_show, key):
    conf = _confd(tmp_path)
    st = lts.load_state(state_show({**RECORDS, key: BAD[key]}))
    assert [k for k, _ in ot.undetermined_state_records("db-a", st)] == [key]
    _, report = ot.run_precheck("db-a", str(conf), st)
    lines = [ln for ln in report if "未判定" in ln]
    assert len(lines) == 1 and key in lines[0] and st.problem(key).kind in lines[0], report


@pytest.mark.parametrize("key", ["_federation_policy.yaml", "_account_registry.yaml"])
def test_offboard_does_not_block_on_records_it_does_not_use(tmp_path, state_show, key):
    st = lts.load_state(state_show({**RECORDS, key: BAD[key]}))
    assert ot.undetermined_state_records("db-a", st) == []
    if key == "_account_registry.yaml":
        _, info = ot.check_state_references("db-a", st)
        assert any("AccountID 帳本 tenant-api 無法使用" in ln for ln in info), info


def _run_offboard(conf: Path, *a: str):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, str(OFFBOARD), "db-a", "--config-dir", str(conf), *a],
        capture_output=True, encoding="utf-8", timeout=120, env=env)


def test_offboard_execute_needs_state_or_explicit_skip(tmp_path):
    conf = _confd(tmp_path)
    r = _run_offboard(conf, "--execute")
    assert r.returncode == 2, r.stdout + r.stderr
    assert (conf / "db-a.yaml").exists()
    r = _run_offboard(conf, "--execute", "--skip-state-check")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (conf / "db-a.yaml").exists()


def test_offboard_execute_refused_on_an_undetermined_record(tmp_path, state_show):
    conf = _confd(tmp_path)
    state = state_show({**RECORDS, "_groups.yaml": BAD["_groups.yaml"]})
    r = _run_offboard(conf, "--state", state, "--execute")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "_groups.yaml" in r.stdout and "--skip-state-check" in r.stdout
    assert (conf / "db-a.yaml").exists()
    r = _run_offboard(conf, "--state", state, "--execute", "--skip-state-check")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (conf / "db-a.yaml").exists()


def test_offboard_execute_not_blocked_by_an_unused_record(tmp_path, state_show):
    conf = _confd(tmp_path)
    state = state_show({**RECORDS, "_federation_policy.yaml": BAD["_federation_policy.yaml"]})
    r = _run_offboard(conf, "--state", state, "--execute")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (conf / "db-a.yaml").exists()


def test_offboard_does_not_answer_from_legacy_confd_groups(tmp_path):
    conf = _confd(tmp_path, {"_groups.yaml": "groups:\n  g: {members: [db-a]}\n"})
    configs = ot.load_all_configs(str(conf))
    assert "_groups.yaml" not in configs
    assert ot.check_cross_references("db-a", configs) == []


def _main(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["offboard_tenant.py", *argv])
    ot.main()


def test_offboard_main_with_state_executes(tmp_path, monkeypatch, capsys, state_show):
    conf = _confd(tmp_path)
    _main(monkeypatch, ["db-a", "--config-dir", str(conf), "--state", state_show(RECORDS), "--execute"])
    out = capsys.readouterr().out
    assert "群組 g-fin 的成員" in out and "已刪除" in out
    assert not (conf / "db-a.yaml").exists()


def test_offboard_main_refuses_execute_without_state(tmp_path, monkeypatch):
    conf = _confd(tmp_path)
    with pytest.raises(SystemExit) as e:
        _main(monkeypatch, ["db-a", "--config-dir", str(conf), "--execute"])
    assert e.value.code == 2
    assert (conf / "db-a.yaml").exists()


def test_offboard_main_old_input_is_a_caller_error(tmp_path, monkeypatch, capsys):
    conf = _confd(tmp_path)
    with pytest.raises(SystemExit) as e:
        _main(monkeypatch, ["db-a", "--config-dir", str(conf), "--state", str(_cm_yaml(tmp_path)),
                            "--execute"])
    assert e.value.code == 2
    _assert_names_the_commands(capsys.readouterr().err)
    assert (conf / "db-a.yaml").exists()


# F2: tenant-api imports conf.d's legacy files at startup while the store has
# no such record, so a reference there comes back after the next restart.
def test_offboard_reports_legacy_files_the_store_lacks(tmp_path, state_show):
    conf = _confd(tmp_path, {"_groups.yaml": "groups:\n  old: {members: [db-a]}\n"})
    (conf / "_federation").mkdir()
    (conf / "_federation" / "db-a.yaml").write_text("metrics: [up]\n", encoding="utf-8")
    st = lts.load_state(state_show({"_views.yaml": "views: {}\n"}))
    refs, _ = ot.check_state_references("db-a", st, str(conf))
    whats = [w for w, _ in refs]
    assert any("_groups.yaml" in w and "下次開機會匯入" in w for w in whats), whats
    assert any("_federation/db-a.yaml" in w for w in whats), whats


def test_offboard_ignores_legacy_files_the_store_already_holds(tmp_path, state_show):
    conf = _confd(tmp_path, {"_groups.yaml": "groups:\n  old: {members: [db-a]}\n"})
    st = lts.load_state(state_show({"_groups.yaml": "groups: {}\n"}))
    refs, _ = ot.check_state_references("db-a", st, str(conf))
    assert refs == []


# ── generate_tenant_metadata ────────────────────────────────────────────

def test_metadata_custom_groups_from_state(tmp_path, capsys, state_show):
    conf = _confd(tmp_path, {"_groups.yaml": "groups:\n  stale: {members: [db-a]}\n"})
    groups = gtm._load_custom_groups(lts.load_state(state_show(RECORDS)), conf)
    assert sorted(groups) == ["g-fin", "g-other"]
    assert groups["g-fin"]["members"] == ["db-a", "db-b"]
    assert "is not read here" in capsys.readouterr().err
    assert gtm._load_custom_groups(None, conf) == {}


def _metadata_main(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["generate_tenant_metadata.py", *argv])
    gtm.main()


def test_metadata_groups_problem_exits_nonzero(tmp_path, monkeypatch, capsys, state_show):
    conf = _confd(tmp_path)
    state = state_show({**RECORDS, "_groups.yaml": BAD["_groups.yaml"]})
    with pytest.raises(SystemExit) as e:
        _metadata_main(monkeypatch, ["--config-dir", str(conf), "--state", state, "--dry-run"])
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "_groups.yaml" in err and "rejected" in err


def test_metadata_old_input_exits_nonzero(tmp_path, monkeypatch, capsys):
    conf = _confd(tmp_path)
    with pytest.raises(SystemExit) as e:
        _metadata_main(monkeypatch, ["--config-dir", str(conf), "--state", str(_cm_yaml(tmp_path))])
    assert e.value.code == 2
    _assert_names_the_commands(capsys.readouterr().err)
