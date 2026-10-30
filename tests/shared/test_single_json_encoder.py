"""#3216: one JSON encoder; a --json run's document goes out through run().

A tool hands its document back (`return Report(doc)`); `_lib_exitcodes.run`
encodes it with `encode_json` — two-space indent, no NaN / Infinity, `date` /
`datetime` as ISO 8601, a type's own `__json__()` — and prints it. A value
the encoder cannot carry is the `output_not_serializable` envelope at the
crash exit code, never a document with `Infinity` in it, never part of one.

The gate below is the ratchet that moves tools onto it: every file that
still prints its own JSON is listed with how many times it does, and the
count may only go down.
"""
from __future__ import annotations

import ast
import datetime
import json
import math
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
TOOLS = REPO / "scripts" / "tools"
sys.path.insert(0, str(TOOLS))
import _lib_exitcodes as lib  # noqa: E402
from _lib_parse_tree import Timestamp  # noqa: E402


# ── the encoder ──────────────────────────────────────────────────────────

def test_one_format_two_space_utf8():
    assert lib.encode_json({"名": [1, 2.5, None, True]}) == (
        '{\n  "名": [\n    1,\n    2.5,\n    null,\n    true\n  ]\n}')


def test_dates_are_iso_8601_and_a_timestamp_is_its_text():
    doc = {"d": datetime.date(2024, 1, 2), "t": datetime.datetime(2024, 1, 2, 3, 4, 5),
           "y": Timestamp("2001-12-14 21:59:43.10 -5")}
    assert json.loads(lib.encode_json(doc)) == {
        "d": "2024-01-02", "t": "2024-01-02T03:04:05", "y": "2001-12-14 21:59:43.10 -5"}


@pytest.mark.parametrize("doc, where", [
    ({"changes": [{}, {}, {}, {"impact_pct": math.inf}]}, "changes[3].impact_pct"),
    ({"x y": {"z": math.nan}}, '["x y"].z'),
    ([-math.inf], "[0]"),
    ({"s": {1, 2}}, "s"),
    ({"p": pathlib.Path("/x")}, "p"),
    ({("t",): 1}, "[\"('t',)\"]"),
])
def test_what_json_cannot_carry_is_named_where_it_is(doc, where):
    with pytest.raises(lib.OutputNotSerializable) as exc:
        lib.encode_json(doc)
    assert exc.value.where == where


def test_a_json_hook_does_not_smuggle_a_value_past_the_rules():
    class Sly:
        def __json__(self):
            return {"v": math.nan}
    with pytest.raises(lib.OutputNotSerializable) as exc:
        lib.encode_json({"a": Sly()})
    assert exc.value.where == "a.v"


# ── run() writes the document ────────────────────────────────────────────

def _run(body: str):
    script = ("import sys; sys.path.insert(0, %r)\n"
              "import math, _lib_exitcodes as lib\n" % str(TOOLS)) + body
    return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                          encoding="utf-8", timeout=60)


def test_run_prints_the_document_and_exits_with_its_status():
    p = _run("lib.run(lambda: lib.Report({'n': 1}, 1))")
    assert p.returncode == 1, p.stderr
    assert json.loads(p.stdout) == {"n": 1}


@pytest.mark.real_crash_exit_code
def test_a_document_json_cannot_carry_is_the_named_envelope_not_part_of_it():
    p = _run("lib.run(lambda: lib.Report({'ok': [1], 'bad': {'x': math.inf}}))")
    assert p.returncode == 2, p.stderr
    doc = json.loads(p.stdout)  # only the envelope: no `"ok"` printed before it
    assert (doc["status"], doc["reason"]) == ("caller_error", "output_not_serializable")
    assert "bad.x" in doc["error"] and "ok" not in doc
    assert "bad.x: inf is not a JSON number" in p.stderr
    assert "Traceback" not in p.stderr


@pytest.mark.real_crash_exit_code
def test_the_tools_own_crash_code_is_kept():
    """tenant-verify's sanctioned crash code (1) is what a defect exits with."""
    p = _run("lib.set_crash_exit_code(1)\nlib.run(lambda: lib.Report([math.nan]))")
    assert p.returncode == 1, p.stderr


def test_in_process_callers_take_the_same_step(capsys):
    assert lib.emit_result(lib.Report({"a": datetime.date(2024, 1, 2)})) == 0
    assert json.loads(capsys.readouterr().out) == {"a": "2024-01-02"}
    assert lib.emit_result(3) == 3


# ── the ratchet: tools that still print their own JSON ───────────────────

#: ⛔ scripts/tools file -> how many times it prints its own JSON (`print` /
#: `sys.stdout.write` of `json.dumps` / `format_json_report`, or
#: `json.dump(..., sys.stdout)`). Migrate a site to `return Report(doc)` and
#: lower its count here; a file not listed may not do it at all.
OWN_JSON = {
    "_lib_doc_yaml_inventory.py": 1,
    "dx/agent_output_metrics.py": 1,
    "dx/coverage_delta.py": 1,
    "dx/coverage_gap_analysis.py": 1,
    "dx/doc_coverage.py": 1,
    "dx/generate_rule_pack_stats.py": 1,
    "dx/list_subprocess_only_modules.py": 1,
    "dx/scan_component_health.py": 2,
    "dx/tenant_verify.py": 2,
    "dx/verify_diff.py": 2,
    "dx/waveform_compile.py": 1,
    "lint/check_bilingual_content.py": 1,
    "lint/check_bilingual_structure.py": 1,
    "lint/check_build_completeness.py": 1,
    "lint/check_cli_contract.py": 2,
    "lint/check_cli_coverage.py": 1,
    "lint/check_cli_default_drift.py": 2,
    "lint/check_commit_scope_doc.py": 1,
    "lint/check_devcontainer_dep_parity.py": 2,
    "lint/check_doc_datools_cmds.py": 1,
    "lint/check_doc_k8s_refs.py": 1,
    "lint/check_engagement_disclosure.py": 1,
    "lint/check_frontmatter_versions.py": 2,
    "lint/check_image_caveats.py": 1,
    "lint/check_jsx_i18n.py": 1,
    "lint/check_log_egress_policy.py": 1,
    "lint/check_makefile_targets.py": 1,
    "lint/check_metric_dictionary.py": 1,
    "lint/check_pipestatus_errexit.py": 1,
    "lint/check_portal_bundle_size.py": 1,
    "lint/check_portal_rulepack_claims.py": 1,
    "lint/check_property_coverage.py": 1,
    "lint/check_retire_drift.py": 1,
    "lint/check_rulepack_sync.py": 1,
    "lint/check_threshold_unit_sanity.py": 1,
    "lint/check_trk_index_coverage.py": 1,
    "lint/lint_tool_consistency.py": 1,
    "lint/validate_docs_versions.py": 1,
    "ops/alert_correlate.py": 1,
    "ops/alert_quality.py": 2,
    "ops/analyze_rule_pack_gaps.py": 1,
    "ops/assemble_config_dir.py": 7,
    "ops/backtest_threshold.py": 8,
    "ops/batch_diagnose.py": 2,
    "ops/blind_spot_discovery.py": 1,
    "ops/byo_check.py": 1,
    "ops/cardinality_forecasting.py": 3,
    "ops/check_alert.py": 3,
    "ops/config_diff.py": 1,
    "ops/cutover_tenant.py": 1,
    "ops/diagnose.py": 4,
    "ops/discover_instance_mappings.py": 2,
    "ops/drift_detect.py": 1,
    "ops/explain_keys.py": 2,
    "ops/federation_check.py": 1,
    "ops/generate_rule_pack_split.py": 1,
    "ops/gitops_check.py": 2,
    "ops/grafana_import.py": 2,
    "ops/maintenance_scheduler.py": 1,
    "ops/notification_tester.py": 1,
    "ops/onboard_platform.py": 1,
    "ops/operator_check.py": 1,
    "ops/policy_engine.py": 7,
    "ops/policy_opa_bridge.py": 4,
    "ops/rule_pack_diff.py": 1,
    "ops/runtime_audit.py": 2,
    "ops/shadow_verify.py": 1,
    "ops/silencer_drift_check.py": 1,
    "ops/state_reconcile.py": 1,
    "ops/threshold_govern.py": 1,
    "ops/threshold_recommend.py": 2,
    "ops/validate_config.py": 1,
}


def _prints_own_json(call: ast.Call) -> bool:
    f = ast.unparse(call.func)
    if f in ("print", "sys.stdout.write") and call.args and isinstance(call.args[0], ast.Call):
        inner = ast.unparse(call.args[0].func)
        return inner == "json.dumps" or inner.split(".")[-1] == "format_json_report"
    return f == "json.dump" and len(call.args) > 1 and ast.unparse(call.args[1]) == "sys.stdout"


def own_json_counts(root: pathlib.Path = TOOLS) -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in sorted(root.rglob("*.py")):
        if path.name == "_lib_exitcodes.py":
            continue
        n = sum(1 for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                if isinstance(node, ast.Call) and _prints_own_json(node))
        if n:
            counts[path.relative_to(root).as_posix()] = n
    return counts


def test_no_tool_prints_more_of_its_own_json_than_listed():
    now = own_json_counts()
    grew = {f: (n, OWN_JSON.get(f, 0)) for f, n in now.items() if n > OWN_JSON.get(f, 0)}
    assert not grew, ("print a --json document by `return Report(doc)` (run() encodes it), "
                      f"not yourself — (now, allowed): {grew}")
    shrank = {f: (now.get(f, 0), n) for f, n in OWN_JSON.items() if now.get(f, 0) < n}
    assert not shrank, f"migrated — lower OWN_JSON to what is left (now, listed): {shrank}"


def test_the_ratchet_sees_each_shape(tmp_path):
    (tmp_path / "t.py").write_text(
        "import json, sys\nfrom _lib_io import format_json_report\n"
        "print(json.dumps({}))\nprint(format_json_report({}))\n"
        "sys.stdout.write(json.dumps({}))\njson.dump({}, sys.stdout)\n"
        "json.dump({}, open('x', 'w'))\nprint(json.dumps({}), file=sys.stderr)\n",
        encoding="utf-8")
    # the last-but-one writes a file and is not stdout; the last is counted
    # (a document printed to any stream by the tool itself)
    assert own_json_counts(tmp_path) == {"t.py": 5}


# ── #3144: explain-route on a value YAML reads as a date ─────────────────

@pytest.mark.usefixtures("da_guard_env")
def test_explain_route_writes_a_yaml_date_as_its_text(tmp_path):
    """#3144: an unquoted `team: 2001-12-15` was `date is not JSON
    serializable` (a crash, rc 2). It is the text as written now — what
    the generator and Alertmanager read — and the run is rc 0."""
    matrix = json.loads((REPO / "tests" / "shared" / "routing_policy_parity_matrix.json")
                        .read_text(encoding="utf-8"))
    [tree] = [t for t in matrix["trees"] if t["name"] == "routing-values-yaml11"]
    for rel, body in tree["files"].items():
        (tmp_path / rel).write_text(body, encoding="utf-8")
    assert "team: 2001-12-15" in tree["files"]["_platform.yaml"]
    p = subprocess.run([sys.executable, str(TOOLS / "ops" / "explain_route.py"),
                        "--config-dir", str(tmp_path), "--json"],
                       capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert p.returncode == 0, p.stderr[-3000:]
    assert '"2001-12-15"' in p.stdout
    json.loads(p.stdout)
