"""#3216: these ops tools hand their --json document to `_lib_exitcodes.run`.

Driven as the CLI runs them (a subprocess entering through `run(main)`),
one path each that needs no network answer and no cluster:

* the document is the one encoder's text, byte for byte — two-space indent,
  UTF-8, one trailing newline — and the exit status is the one the path had
  before the move (a single-line `json.dumps` of the old code is red here);
* a document JSON cannot carry (a NaN injected into the Report the tool
  builds) is the crash exit with the `output_not_serializable` envelope and
  no part of the document — a tool that printed its own document again
  would print the NaN at its old status instead.

The ratchet (`tests/shared/test_single_json_encoder.py::OWN_JSON`) is the
static half: none of these files may print JSON itself.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TOOLS = REPO / "scripts" / "tools"
sys.path.insert(0, str(TOOLS))
from _lib_exitcodes import encode_json  # noqa: E402

CONF_D = REPO / "components" / "threshold-exporter" / "config" / "conf.d"
REFUSED = "http://127.0.0.1:9"  # discard port: nothing listens, refused at once

# (tool, argv with {tmp} for the test's directory, exit status of the path)
CASES = [
    ("alert_correlate", ["--input", "{tmp}/alerts.json", "--json", "--ci"], 1),
    ("alert_quality", ["--prometheus", REFUSED, "--json"], 2),
    ("analyze_rule_pack_gaps", ["--config-dir", "{tmp}/conf.d", "--json"], 0),
    ("assemble_config_dir", ["--json"], 2),
    ("assemble_config_dir", ["--sources", "{tmp}/conf.d", "--check", "--json"], 0),
    ("assemble_config_dir", ["--sources", "{tmp}/empty", "--check", "--json"], 1),  # no_carriers
    ("assemble_config_dir", ["--sources", "{tmp}/conf.d", "--output", "{tmp}/stale",
                             "--json"], 1),  # a leftover defaults carrier
    ("byo_check", ["prometheus", "--prometheus", REFUSED, "--json"], 2),
    ("check_alert", ["HighCPU", "db-a", "--prometheus", REFUSED], 2),
    ("config_diff", ["--old-dir", "{tmp}/conf.d", "--new-dir", "{tmp}/conf.d",
                     "--json-output", "--at", "2026-10-10T00:00:00Z"], 0),
    ("cutover_tenant", ["--readiness-json", "{tmp}/ready.json", "--tenant", "t",
                        "--prometheus", REFUSED, "--json-output"], 2),
    ("diagnose", ["tenant-é", "--prometheus", REFUSED], 2),
    ("diagnose", ["db-a", "--config-dir", "{tmp}/conf.d", "--show-inheritance"], 0),
    ("drift_detect", ["--dirs", "{tmp}/conf.d,{tmp}/other", "--json", "--ci"], 1),
    ("explain_keys", ["--config-dir", "{tmp}/nope", "--json"], 2),
    ("explain_keys", ["--config-dir", "{tmp}/conf.d", "--json"], 0),
    ("federation_check", ["central", "--prometheus", REFUSED, "--json"], 2),
    ("generate_rule_pack_split", ["--rule-packs-dir", str(REPO / "rule-packs"),
                                  "--output-dir", "{tmp}/split", "--dry-run", "--json"], 0),
]
IDS = [f"{t}-{i}" for i, (t, _, _) in enumerate(CASES)]

# Every Report the tool builds carries a NaN beside its document.
_NAN_WRAPPER = """
import math, os, runpy, sys
tools_dir, script = sys.argv[1], sys.argv[2]
sys.argv = sys.argv[2:]
sys.path.insert(0, os.path.dirname(script))
sys.path.insert(0, tools_dir)
import _lib_exitcodes
class _NaNReport(_lib_exitcodes.Report):
    __slots__ = ()
    def __init__(self, document, status=0):
        super().__init__({"document": document, "injected": math.nan}, status)
_lib_exitcodes.Report = _NaNReport
runpy.run_path(script, run_name="__main__")
"""


@pytest.fixture
def tree(tmp_path):
    shutil.copytree(CONF_D, tmp_path / "conf.d", ignore=shutil.ignore_patterns("examples"))
    (tmp_path / "other").mkdir()
    shutil.copy(CONF_D / "_defaults.yaml", tmp_path / "other" / "_defaults.yaml")
    shutil.copy(CONF_D / "db-b.yaml", tmp_path / "other" / "db-b.yaml")
    (tmp_path / "alerts.json").write_text(json.dumps([
        {"labels": {"alertname": n, "tenant": t, "severity": "critical", "namespace": "ns"},
         "startsAt": f"2026-03-15T10:0{i}:00Z", "endsAt": f"2026-03-15T10:0{i + 5}:00Z"}
        for i, (n, t) in enumerate([("A", "db-a"), ("B", "db-b")])]), encoding="utf-8")
    (tmp_path / "ready.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "empty").mkdir()
    (tmp_path / "stale").mkdir()
    shutil.copy(CONF_D / "_defaults.yaml", tmp_path / "stale" / "_defaults.yml")
    return tmp_path


def _run(tree: Path, tool: str, argv: list[str], wrapper: str | None = None):
    args = [a.replace("{tmp}", str(tree)) for a in argv]
    script = str(TOOLS / "ops" / f"{tool}.py")
    cmd = ([sys.executable, "-c", wrapper, str(TOOLS), script, *args] if wrapper
           else [sys.executable, script, *args])
    env = {k: v for k, v in os.environ.items() if k != "PROMETHEUS_URL"}
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          cwd=tree, env=env, timeout=300)


@pytest.mark.usefixtures("da_guard_env")
@pytest.mark.parametrize("tool, argv, status", CASES, ids=IDS)
def test_the_document_is_the_one_encoders_text_at_the_paths_status(tree, tool, argv, status):
    p = _run(tree, tool, argv)
    assert p.returncode == status, p.stderr[-2000:]
    doc = json.loads(p.stdout)
    assert p.stdout == encode_json(doc) + "\n", p.stdout[:500]
    if "tenant-é" in argv:
        assert "tenant-é" in p.stdout  # UTF-8, not a \\u escape (it was one)


@pytest.mark.real_crash_exit_code
@pytest.mark.usefixtures("da_guard_env")
@pytest.mark.parametrize("tool, argv, status", CASES, ids=IDS)
def test_a_nan_in_the_document_is_the_envelope_not_the_document(tree, tool, argv, status):
    p = _run(tree, tool, argv, _NAN_WRAPPER)
    assert p.returncode == 2, p.stderr[-2000:]
    env = json.loads(p.stdout)  # one document: the envelope alone
    assert (env["status"], env["reason"]) == ("caller_error", "output_not_serializable"), env
    assert "injected: nan is not a JSON number" in p.stderr
    assert "NaN" not in p.stdout


# ── the --json status of paths a subprocess cannot reach offline ─────────
# Before #3216 the text-mode tests pinned these codes for --json too (one
# exit for both); now the --json one rides on the Report, so it is pinned
# here, in-process, the step run() takes (`emit_result`).

sys.path.insert(0, str(TOOLS / "ops"))
from _lib_exitcodes import emit_result  # noqa: E402
import byo_check  # noqa: E402
import cutover_tenant  # noqa: E402
import discover_instance_mappings  # noqa: E402
import federation_check  # noqa: E402

_FAIL = {"check": "x", "status": "fail", "detail": "d"}
_PASS = {"check": "x", "status": "pass", "detail": "d"}


def _rc(main, *args):
    try:
        return emit_result(main(*args))
    except SystemExit as exc:
        return exc.code


@pytest.mark.parametrize("checks, status", [
    ([_PASS], 0), ([_FAIL], 1), ([{**_FAIL, "caller_error": True}], 2)],
    ids=["pass", "fail", "caller-error"])
def test_byo_check_json_status(monkeypatch, capsys, checks, status):
    monkeypatch.setattr(byo_check, "check_prometheus", lambda args: checks)
    monkeypatch.setattr(sys, "argv", ["byo_check", "prometheus", "--json"])
    assert _rc(byo_check.main) == status
    assert json.loads(capsys.readouterr().out)["status"] == ("pass" if status == 0 else "fail")


@pytest.mark.parametrize("checks, status", [
    ([_PASS], 0), ([_FAIL], 1), ([{**_FAIL, "caller_error": True}], 2)],
    ids=["pass", "fail", "caller-error"])
def test_federation_check_json_status(monkeypatch, capsys, checks, status):
    monkeypatch.setattr(federation_check, "check_edge", lambda url: checks)
    monkeypatch.setattr(sys, "argv", ["federation_check", "edge", "--json"])
    assert _rc(federation_check.main) == status
    assert json.loads(capsys.readouterr().out)["status"] == ("pass" if status == 0 else "fail")


@pytest.mark.parametrize("report, status", [
    ({"success": True}, 0),
    ({"success": False, "failed_step": "s", "message": "m"}, 1),
    ({"success": False, "failed_step": "s", "message": "m", "caller_error": True}, 2)],
    ids=["success", "step-failed", "caller-error"])
def test_cutover_json_status(monkeypatch, capsys, report, status):
    monkeypatch.setattr(cutover_tenant, "apply_cutover", lambda **kw: report)
    monkeypatch.setattr(sys, "argv", ["cutover_tenant", "--readiness-json", "r.json",
                                      "--tenant", "t", "--json-output"])
    assert _rc(cutover_tenant.main) == status
    assert json.loads(capsys.readouterr().out) == report


def test_discover_mappings_json_success_is_0(monkeypatch, capsys):
    monkeypatch.setattr(discover_instance_mappings, "query_prometheus_label_values",
                        lambda *a, **kw: {"schema": {"s1", "s2", "s3"}})
    assert _rc(discover_instance_mappings.main, [
        "--prometheus", "http://prom:9090", "--instance", "x:1", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert len(doc["instance_tenant_mapping"]["x:1"]) == 3
