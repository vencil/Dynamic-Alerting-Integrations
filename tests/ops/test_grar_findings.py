"""#2766（ADR-036 §3）：路由產生器輸出結構化 finding。

釘住的契約：

1. **文字一個位元組都不變**：每個 fixture × 模式的 stdout / stderr / 結束碼，
   與加上結構化 finding 之前（``origin/main`` 4372cf86c）擷取的
   ``grar_findings_golden.json`` 完全相同；加了 ``--findings-json`` 也一樣。
2. **warning stream 與拒收訊息的每一行都是 ``Finding``**，``blocks`` 與既有的
   判定（``blocking_generation_errors``、``_policy_errors``、拒收）一致。
3. 範圍內的產生點帶 kind（與 da-guard 同義者同名）、tenant / policy / file /
   field；kind 只能取自 ``FINDING_KINDS`` 這一份目錄。
4. 還沒分類的產生點列在 ``grar_unclassified_findings_baseline.json``，只能變少。
5. ``--findings-json`` 的格式（``da-tools.findings/v1``）、每一種結束都寫、
   原子寫入、寫不進去是結束碼 2。

Tenant ids are fixture names (CLAUDE.md #9).
"""
from __future__ import annotations

import ast
import copy
import json
import pickle
import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _grar_findings_cases as cases  # noqa: E402

import _grar_merge as gm  # noqa: E402
import _grar_validate as gv  # noqa: E402
import generate_alertmanager_routes as gar  # noqa: E402

REPO = cases.REPO
OPS = REPO / "scripts" / "tools" / "ops"
BASELINE = Path(__file__).resolve().parent / "grar_unclassified_findings_baseline.json"
RECORD_KEYS = {"kind", "severity", "blocks", "tenant", "policy", "file",
               "field", "message"}
DOC_KEYS = {"schema", "generator_version", "config_dir", "exit_code",
            "validate", "strict", "findings"}


def _golden() -> dict:
    return json.loads(cases.GOLDEN.read_text(encoding="utf-8"))


def _run(tmp_path: Path, fixture: str, mode: str, *, json_out: bool = True):
    d = cases.build_tree(tmp_path, fixture)
    out = tmp_path / "findings.json"
    extra = ["--findings-json", str(out)] if json_out else []
    res = cases.run_gar(d, cases.MODES[mode], extra)
    doc = (json.loads(out.read_text(encoding="utf-8"))
           if json_out and out.exists() else None)
    return d, res, doc, out


def _records(doc: dict, **match) -> list[dict]:
    return [r for r in doc["findings"]
            if all(r[k] == v for k, v in match.items())]


# ── 1. the text does not move ────────────────────────────────────────────

def test_golden_covers_every_case():
    assert sorted(_golden()) == sorted(f"{f}/{m}" for f, m in cases.CASES)


@pytest.mark.parametrize("fixture,mode", cases.CASES,
                         ids=[f"{f}-{m}" for f, m in cases.CASES])
def test_output_is_byte_identical_to_before_findings(tmp_path, fixture, mode):
    """stdout / stderr / rc equal the pre-#2766 capture — with
    --findings-json too (the flag adds a file, nothing else)."""
    want = _golden()[f"{fixture}/{mode}"]
    for json_out in (False, True):
        sub = tmp_path / ("with" if json_out else "without")
        sub.mkdir()
        d, res, doc, _out = _run(sub, fixture, mode, json_out=json_out)
        got = {"rc": res.returncode,
               "stdout": cases.normalise(res.stdout, d),
               "stderr": cases.normalise(res.stderr, d)}
        assert got == want, (json_out, fixture, mode)
        if json_out:
            assert doc is not None and doc["exit_code"] == res.returncode


# ── 2. --findings-json: the document ─────────────────────────────────────

@pytest.mark.parametrize("fixture,mode", cases.CASES,
                         ids=[f"{f}-{m}" for f, m in cases.CASES])
def test_document_shape_and_printed_lines(tmp_path, fixture, mode):
    d, res, doc, out = _run(tmp_path, fixture, mode)
    assert set(doc) == DOC_KEYS
    assert doc["schema"] == "da-tools.findings/v1"
    assert doc["config_dir"] == str(d)
    assert doc["exit_code"] == res.returncode
    version = (REPO / "components" / "da-tools" / "app" / "VERSION").read_text(
        encoding="utf-8").strip()
    assert doc["generator_version"] == version
    printed = {ln.lstrip() for ln in (res.stdout + res.stderr).splitlines()}
    for rec in doc["findings"]:
        assert set(rec) == RECORD_KEYS, rec
        assert rec["kind"] in gm.FINDING_KINDS, rec
        assert rec["severity"] in gm.FINDING_SEVERITIES, rec
        assert rec["blocks"] in gm.FINDING_BLOCKS, rec
        # Only what this run printed (leading indentation stripped).
        assert rec["message"] in printed, rec["message"]
    # Every non-zero exit of these fixtures is itemised by printed findings,
    # one of which blocks — the run_failed backstop is never what carries it.
    assert all(r["kind"] != "run_failed" for r in doc["findings"]), doc
    if doc["exit_code"] != 0:
        assert any(r["blocks"] != "never" for r in doc["findings"]), doc
        assert rec["message"] == rec["message"].lstrip()
    # Format: UTF-8, ensure_ascii=False, sort_keys, newline-terminated, no
    # temp file left beside it.
    raw = out.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert raw == json.dumps(doc, ensure_ascii=False, sort_keys=True,
                             allow_nan=False, indent=2) + "\n"
    assert sorted(p.name for p in out.parent.iterdir()) == ["conf.d", out.name]


def test_strict_policy_violation_document(tmp_path):
    _d, res, doc, _out = _run(tmp_path, "policy", "strict")
    assert res.returncode == doc["exit_code"] == 1
    viol = _records(doc, kind="domain_policy_violation", tenant="alpha",
                    policy="finance")
    assert {r["field"] for r in viol} == {"receiver.type", "repeat_interval",
                                          "group_by"}
    assert all(r["severity"] == "error" and r["blocks"] == "strict"
               and r["message"].startswith("ERROR: domain_policy 'finance'")
               for r in viol)
    sub = _records(doc, kind="domain_policy_violation", tenant="beta")
    assert {r["field"] for r in sub} == {"routes[0].receiver.type"}
    [miss] = _records(doc, kind="critical_escalation_missing")
    assert (miss["tenant"], miss["policy"], miss["field"], miss["blocks"]) == (
        "alpha", "finance", "receiver.type", "strict")
    [leak] = _records(doc, kind="critical_escalation_leak")
    assert (leak["tenant"], leak["severity"], leak["blocks"], leak["field"]) == (
        "beta", "warning", "never", "routes[0].receiver.type")
    unusable = {(r["policy"], r["field"])
                for r in _records(doc, kind="domain_policy_unusable")}
    assert unusable == {("broken", "tenants"), ("finance", "tenants"),
                        ("finance", "constraints.min_group_wait")}


def test_lenient_policy_violation_is_a_warning_that_blocks_under_strict(tmp_path):
    _d, res, doc, _out = _run(tmp_path, "policy", "validate")
    assert res.returncode == 0
    viol = _records(doc, kind="domain_policy_violation")
    assert viol and all(r["severity"] == "warning" and r["blocks"] == "strict"
                        for r in viol)


@pytest.mark.parametrize("fixture,rc,kind,where", [
    ("unreadable", 1, "tenant_file_unreadable", {"file": "alpha.yaml"}),
    ("duplicate", 1, "duplicate_tenant", {"tenant": "alpha"}),
    ("tree", 2, "routing_enforced_below_root",
     {"file": "team/_defaults.yaml", "field": "_routing_enforced"}),
    ("invalid_id", 1, "invalid_tenant_id", {"tenant": "Bad_Id"}),
])
def test_refusal_documents(tmp_path, fixture, rc, kind, where):
    """Each of the four refusals: its rc, its itemised finding (kind + where)
    and its framing lines as ``refusal_summary`` — and nothing else, since a
    refused run prints nothing else."""
    _d, res, doc, _out = _run(tmp_path, fixture, "render")
    assert res.returncode == doc["exit_code"] == rc
    [item] = _records(doc, kind=kind)
    assert {k: item[k] for k in where} == where
    assert (item["severity"], item["blocks"]) == ("error", "always")
    kinds = Counter(r["kind"] for r in doc["findings"])
    assert set(kinds) == {kind, "refusal_summary"}, kinds
    assert all(r["blocks"] == "always" for r in doc["findings"])


def test_policy_file_unusable_document(tmp_path):
    _d, res, doc, _out = _run(tmp_path, "policy_file", "validate_strict")
    assert res.returncode == 1
    [rec] = doc["findings"]
    assert (rec["kind"], rec["file"], rec["blocks"], rec["severity"]) == (
        "domain_policy_unusable", "_domain_policy.yaml", "strict", "error")


def test_skipped_entry_document(tmp_path):
    _d, _res, doc, _out = _run(tmp_path, "skipped", "validate")
    got = {(r["kind"], r["tenant"], r["field"]) for r in doc["findings"]
           if r["kind"] != "unclassified"}
    assert got == {
        ("routing_not_mapping", "gamma", "_routing"),
        ("conflicting_override_matcher", "alpha", "overrides[0]"),
        ("missing_receiver_field", "alpha", "overrides[1].receiver"),
        ("missing_receiver_field", "alpha", "routes[0].receiver"),
        ("routing_group_by_invalid", "alpha", "group_by[1]"),
        ("timing_value_replaced", "alpha", "group_wait"),
        ("unknown_receiver_type", "beta", "receiver.type"),
    }
    assert all(r["blocks"] == "validate" for r in doc["findings"]
               if r["kind"] != "unclassified")


def test_caller_error_exit_still_writes_the_document(tmp_path):
    d = cases.build_tree(tmp_path, "clean")
    out = tmp_path / "f.json"
    res = cases.run_gar(d, ["--validate", "--yes"], ["--findings-json", str(out)])
    assert res.returncode == 2, res.stderr
    doc = json.loads(out.read_text(encoding="utf-8"))
    # A caller error prints no finding, so the document carries the one
    # finding it adds itself: a reader of the findings alone must not pass it.
    assert doc["exit_code"] == 2
    [rec] = doc["findings"]
    assert (rec["kind"], rec["severity"], rec["blocks"]) == (
        "run_failed", "error", "always")


def test_validate_only_lines_join_the_findings_once(monkeypatch):
    """A --validate verdict error that is not a stream line (the inhibit
    tripwire) is recorded as blocking under --validate; one that IS a stream
    line already recorded is not recorded twice; the verdict's warnings are
    recorded as non-blocking."""
    from _grar_render import GeneratedConfigVerdict
    stream = gm.skipped_entry_warning("  WARN: alpha: bad entry, skipping")
    trip = "  WARN: generated inhibit_rules[0] would silence a platform alert"
    note = "  NOTICE: equal-label not presence-gated"
    monkeypatch.setattr(gar, "evaluate_generated_config",
                        lambda *a, **k: GeneratedConfigVerdict(
                            errors=[stream, trip], warnings=[note]))
    sink = [stream]
    with pytest.raises(SystemExit) as exc:
        gar._validate_mode([], [], [], [stream], sink)
    assert exc.value.code == 1
    assert [(str(f), f.blocks) for f in sink] == [
        (stream, "validate"), (note, "never"), (trip, "validate")]
    assert sink[0] is stream


def _pigeon_tree(root: Path) -> Path:
    """One tenant whose only receiver is unusable and whose dedup is off:
    nothing renders, so every mode exits 1 ("No valid routes ...") although
    the one finding blocks only under --validate (blind review B1)."""
    d = root / "conf.d"
    d.mkdir()
    (d / "_defaults.yaml").write_text(cases._DEFAULTS, encoding="utf-8")
    (d / "pigeon.yaml").write_text(
        cases._tenant("pigeon", "      receiver:\n        type: carrier-pigeon\n",
                      "    _severity_dedup: disable\n"), encoding="utf-8")
    return d


@pytest.mark.parametrize("argv,validate,strict", [
    (["--dry-run"], False, False),
    (["--strict", "--dry-run"], False, True),
    (["--validate"], True, False),
])
def test_exit_1_without_a_blocking_line_still_blocks(tmp_path, argv, validate,
                                                      strict):
    d = _pigeon_tree(tmp_path)
    out = tmp_path / "f.json"
    res = cases.run_gar(d, argv, ["--findings-json", str(out)])
    assert res.returncode == 1, res.stderr
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert (doc["exit_code"], doc["validate"], doc["strict"]) == (1, validate,
                                                                  strict)
    in_force = {"always"} | ({"validate"} if validate else set()) \
        | ({"strict"} if strict else set())
    assert any(r["blocks"] in in_force for r in doc["findings"]), doc
    # The backstop is added only when no printed line blocks in this mode.
    assert any(r["kind"] == "run_failed" for r in doc["findings"]) is (
        not validate), doc


def test_usage_error_replaces_a_stale_document(tmp_path):
    d = cases.build_tree(tmp_path, "clean")
    out = tmp_path / "f.json"
    out.write_text('{"exit_code": 0, "findings": []}\n', encoding="utf-8")
    res = cases.run_gar(d, ["--dry-run", "--no-such-flag"],
                        ["--findings-json", str(out)])
    assert res.returncode == 2, res.stderr
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["exit_code"] == 2
    assert [r["kind"] for r in doc["findings"]] == ["run_failed"]


def test_an_exception_still_writes_a_failing_document(tmp_path, monkeypatch):
    out = tmp_path / "f.json"
    monkeypatch.setattr(sys, "argv", ["gar", "--config-dir", str(tmp_path),
                                      "--dry-run", "--findings-json", str(out)])

    def boom(_args, findings):
        findings.append(gm.Finding("  WARN: x", blocks="never"))
        raise BrokenPipeError("stdout closed")

    monkeypatch.setattr(gar, "_run", boom)
    with pytest.raises(BrokenPipeError):
        gar.main()
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["exit_code"] == 1
    assert [r["kind"] for r in doc["findings"]] == ["unclassified", "run_failed"]


def test_an_unmarked_line_fails_closed():
    assert gm.as_finding("  WARN: from nowhere").blocks == "always"


@pytest.mark.parametrize("exit_code,findings,validate,strict,backstop", [
    (0, [], False, False, False),
    (1, [], True, True, True),
    (1, ["strict"], True, False, True),     # strict-only finding, not --strict
    (1, ["strict"], False, True, False),
    (1, ["validate"], False, False, True),
    (1, ["validate"], True, False, False),
    (1, ["never"], True, True, True),
    (1, ["always"], False, False, False),
])
def test_run_failed_backstop(exit_code, findings, validate, strict, backstop):
    doc = gar.findings_document(
        [gm.Finding("  WARN: x", blocks=b) for b in findings],
        config_dir="c", exit_code=exit_code, validate=validate, strict=strict)
    added = [r for r in doc["findings"] if r["kind"] == "run_failed"]
    assert bool(added) is backstop, doc


def test_unwritable_findings_path_is_a_caller_error(tmp_path):
    d = cases.build_tree(tmp_path, "clean")
    out = tmp_path / "absent" / "f.json"
    res = cases.run_gar(d, ["--dry-run"], ["--findings-json", str(out)])
    assert res.returncode == 2
    assert "Traceback" not in res.stderr
    [line] = [ln for ln in res.stderr.splitlines() if ln.startswith("ERROR:")]
    assert str(out) in line and "--findings-json" in line
    assert not (tmp_path / "absent").exists()


def test_rewrite_replaces_the_previous_document(tmp_path):
    d = cases.build_tree(tmp_path, "clean")
    out = tmp_path / "f.json"
    out.write_text("stale", encoding="utf-8")
    res = cases.run_gar(d, ["--dry-run"], ["--findings-json", str(out)])
    assert res.returncode == 0
    assert json.loads(out.read_text(encoding="utf-8"))["exit_code"] == 0


# ── 3. Finding objects, in process ───────────────────────────────────────

def _stream(tmp_path: Path, fixture: str, *, strict: bool,
            allowed_domains=None) -> tuple[list[str], list[str]]:
    """(stream lines, refusal lines) the generator would produce."""
    d = cases.build_tree(tmp_path / f"{fixture}-{strict}", fixture)
    tree = gar.load_tenant_tree(str(d), strict_policies=strict)
    _rc, refusal = gar.tree_refusal(
        tree.files_read, tree.tenant_file_errors, tree.duplicate_tenants,
        tree.routing_tree_problems, tree.invalid_tenant_ids)
    _routes, _recv, route_w = gar.generate_routes(
        tree.routing_configs, allowed_domains=allowed_domains,
        enforced_routing=tree.enforced_routing, tenants=tree.dedup_configs)
    _rules, dedup_w = gar.generate_inhibit_rules(tree.dedup_configs)
    return list(tree.schema_warnings) + route_w + dedup_w, refusal


_ALL = [(f, s) for f in cases.FIXTURES for s in (False, True)]


@pytest.mark.parametrize("fixture,strict", _ALL,
                         ids=[f"{f}-{'strict' if s else 'lenient'}" for f, s in _ALL])
def test_every_stream_and_refusal_line_is_a_finding(tmp_path, capsys,
                                                    fixture, strict):
    stream, refusal = _stream(tmp_path, fixture, strict=strict,
                              allowed_domains=["*.nowhere.example"])
    plain = [ln for ln in stream + refusal if not isinstance(ln, gm.Finding)]
    assert not plain, plain
    for f in stream + refusal:
        assert f.kind in gm.FINDING_KINDS, f
        assert f.severity in gm.FINDING_SEVERITIES and f.blocks in gm.FINDING_BLOCKS


@pytest.mark.parametrize("fixture,strict", _ALL,
                         ids=[f"{f}-{'strict' if s else 'lenient'}" for f, s in _ALL])
def test_blocks_restates_todays_rule(tmp_path, capsys, fixture, strict):
    """``blocks`` decides nothing; it must agree with the predicates that do."""
    stream, refusal = _stream(tmp_path, fixture, strict=strict,
                              allowed_domains=["*.nowhere.example"])
    for f in stream:
        blocking = gv.blocking_generation_errors([f]) == [f]
        policy_err = gar._policy_errors([f]) == [f]
        tree_or_collision = (gv.is_routing_tree_error(f)
                             or gv.is_receiver_name_collision(f))
        assert (f.blocks in ("validate", "always")) == blocking, f
        assert (f.blocks == "always") == tree_or_collision, f
        if policy_err:
            assert f.blocks == "strict" and f.severity == "error", f
        if f.blocks == "strict" and not strict:
            assert not f.lstrip().startswith(gv.POLICY_ERROR_PREFIX), f
        assert f.severity == gm._severity_of(f), f
    for f in refusal:
        assert (f.blocks, f.severity) == ("always", "error"), f


def test_finding_keeps_text_type_and_fields():
    f = gm.Finding("  WARN: x", kind="invalid_route_entry", blocks="validate",
                   tenant="alpha", field="routes[0]")
    assert f == "  WARN: x" and isinstance(f, str) and f.severity == "warning"
    for clone in (copy.copy(f), copy.deepcopy(f), pickle.loads(pickle.dumps(f))):
        assert type(clone) is gm.Finding and clone._attrs() == f._attrs()
        assert clone == f
    s = gm.skipped_entry_warning("  WARN: y, skipping", kind="invalid_route_entry")
    assert isinstance(s, gm.Finding) and isinstance(s, gm.SkippedEntryWarning)
    assert (s.blocks, s.kind) == ("validate", "invalid_route_entry")
    assert type(pickle.loads(pickle.dumps(s))) is gm.SkippedEntryWarning
    r = gm.replaced_value_warning("  WARN: z", kind="timing_value_replaced")
    assert isinstance(r, gm.Finding) and r.blocks == "validate"
    # The one deliberate re-format keeps type and fields.
    e = s.with_text("  WARN: y?, skipping")
    assert type(e) is gm.SkippedEntryWarning and e.kind == s.kind
    assert e.as_record()["message"] == "WARN: y?, skipping"
    with pytest.raises(TypeError):
        gm.Finding("  WARN: no blocks", kind="invalid_route_entry")


def test_check_domain_policies_fields():
    routing = {"alpha": {"receiver": {"type": "webhook",
                                      "url": "https://h.example.com/a"},
                         "routes": [{"match": {"team": "x"},
                                     "receiver": {"type": "slack",
                                                  "api_url": "https://h.example.com/s"}}]}}
    pol = {"p": {"tenants": ["alpha"],
                 "constraints": {"forbidden_receiver_types": ["slack"],
                                 "enforce_group_by": ["severity"]}}}
    for strict in (False, True):
        out = gar.check_domain_policies(routing, pol, strict=strict)
        assert out and all(isinstance(f, gm.Finding) for f in out)
        got = {(f.kind, f.tenant, f.policy, f.field, f.blocks) for f in out}
        assert got == {
            ("domain_policy_violation", "alpha", "p", "routes[0].receiver.type", "strict"),
            ("domain_policy_violation", "alpha", "p", "group_by", "strict"),
            ("domain_policy_violation", "alpha", "p", "routes[0].group_by", "strict"),
        }, got
        assert {f.severity for f in out} == {"error" if strict else "warning"}


def test_receiver_kinds_follow_the_field_states():
    """``_exactly_one_kind`` / the required-field split, from the data."""
    def kind(receiver):
        _cfg, w = gm.build_receiver_config(receiver, "ctx", tenant_id="alpha")
        [line] = w
        assert line.tenant == "alpha"
        return line.kind, line.field
    assert kind("nope") == ("missing_receiver_field", "receiver")
    assert kind({"url": "x"}) == ("missing_receiver_field", "receiver.type")
    assert kind({"type": "fax"}) == ("unknown_receiver_type", "receiver.type")
    assert kind({"type": "webhook"})[0] == "missing_receiver_field"
    assert kind({"type": "webhook", "url": 5})[0] == "invalid_receiver_field"
    assert kind({"type": "pagerduty"})[0] == "missing_receiver_field"
    assert kind({"type": "pagerduty", "service_key": "a",
                 "routing_key": "b"})[0] == "conflicting_receiver_field"
    assert kind({"type": "pagerduty", "service_key": 5})[0] == "invalid_receiver_field"


# ── 4. the kind catalog ──────────────────────────────────────────────────

def _producer_sources() -> list[Path]:
    files = sorted(OPS.glob("_grar_*.py")) + [OPS / "generate_alertmanager_routes.py"]
    assert len(files) >= 6, files
    return files


def _kind_literals(tree: ast.AST) -> list[tuple[int, str]]:
    """Every string a ``kind=`` keyword or a ``"kind":`` dict entry can be."""
    out = []
    for node in ast.walk(tree):
        values = []
        if isinstance(node, ast.keyword) and node.arg == "kind":
            values.append(node.value)
        keys = {k.value for k in getattr(node, "keys", [])
                if isinstance(k, ast.Constant)}
        # A dict of Finding fields (``**where``) — not e.g. a ConfigMap's.
        if isinstance(node, ast.Dict) and "kind" in keys and keys & {
                "tenant", "policy", "file", "field", "blocks"}:
            values.extend(v for k, v in zip(node.keys, node.values)
                          if isinstance(k, ast.Constant) and k.value == "kind")
        for v in values:
            out.extend((c.lineno, c.value) for c in ast.walk(v)
                       if isinstance(c, ast.Constant) and isinstance(c.value, str))
    return out


def test_every_kind_literal_is_in_the_catalog():
    bad = []
    seen = set()
    for path in _producer_sources():
        for lineno, kind in _kind_literals(ast.parse(path.read_text(encoding="utf-8"))):
            seen.add(kind)
            if kind not in gm.FINDING_KINDS:
                bad.append(f"{path.name}:{lineno}: {kind}")
    assert not bad, bad
    assert len(seen) > 20, seen


def test_catalog_has_no_dead_kind_and_one_line_descriptions():
    text = "\n".join(p.read_text(encoding="utf-8") for p in _producer_sources()
                     if p.name != "_grar_merge.py")
    dynamic = set(gar.BLOCKING_TREE_KINDS) | {"domain_policy_out_of_scope"}
    for kind, desc in gm.FINDING_KINDS.items():
        assert kind == kind.lower() and " " not in kind, kind
        assert desc.strip() and "\n" not in desc, kind
        if kind in (gm.UNCLASSIFIED, "missing_receiver_field",
                    "unknown_receiver_type", "invalid_receiver_field",
                    "conflicting_receiver_field", "timing_value_replaced"):
            continue  # produced in _grar_merge itself
        assert f'"{kind}"' in text or kind in dynamic, kind
    assert dynamic <= set(gm.FINDING_KINDS)


# ── 5. the shrink-only baseline of unclassified producer sites ───────────

_MARKERS = {"skipped_entry_warning", "replaced_value_warning", "Finding",
            "SkippedEntryWarning", "ReplacedValueWarning"}


def unclassified_sites(source: str, rel: str) -> Counter:
    """``rel::function`` → number of producer calls that leave a line
    unclassified: ``unclassified(…)``, and a Finding constructor / marker
    helper called with neither ``kind=`` nor ``**`` (it defaults to
    ``unclassified``). The helpers' own definitions are not producers."""
    tree = ast.parse(source)
    out: Counter = Counter()

    def visit(node: ast.AST, func: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = func
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = child.name if func == "<module>" else f"{func}.{child.name}"
            if isinstance(child, ast.Call):
                callee = child.func
                cname = getattr(callee, "id", None) or getattr(callee, "attr", None)
                kws = {k.arg for k in child.keywords}
                if cname == "unclassified":
                    out[f"{rel}::{func}"] += 1
                elif (cname in _MARKERS and "kind" not in kws
                      and None not in kws):
                    out[f"{rel}::{func}"] += 1
            visit(child, name)

    visit(tree, "<module>")
    return out


_HELPER_DEFS = {"unclassified", "as_finding", "skipped_entry_warning",
                "replaced_value_warning", "Finding.__new__",
                "Finding.with_text", "_rebuild_finding"}


def _current_sites() -> dict[str, int]:
    total: Counter = Counter()
    for path in _producer_sources():
        rel = path.relative_to(REPO).as_posix()
        for key, n in unclassified_sites(path.read_text(encoding="utf-8"), rel).items():
            if key.split("::", 1)[1] in _HELPER_DEFS and rel.endswith("_grar_merge.py"):
                continue
            total[key] += n
    return dict(total)


def _baseline_sites() -> dict[str, int]:
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert data.get("ticket") == "#2766"
    sites = data["sites"]
    assert all(isinstance(v, int) and not isinstance(v, bool) and v >= 1
               for v in sites.values()), sites
    return sites


def test_unclassified_sites_match_the_shrink_only_baseline():
    now, allowed = _current_sites(), _baseline_sites()
    errors = []
    for key, n in sorted(now.items()):
        if n > allowed.get(key, 0):
            errors.append(f"{key}: {n} unclassified producer call(s), baseline "
                          f"allows {allowed.get(key, 0)} — give the new line a "
                          f"kind from FINDING_KINDS (the baseline only shrinks)")
    for key, n in sorted(allowed.items()):
        if now.get(key, 0) < n:
            errors.append(f"stale baseline row {key}: baseline says {n}, "
                          f"{now.get(key, 0)} remain — lower it in "
                          f"{BASELINE.name} (a row at 0 is deleted)")
    assert not errors, "\n".join(errors)


@pytest.mark.parametrize("source,expected", [
    ('def f():\n    w.append(unclassified("  WARN: x", blocks="never"))\n',
     {"x.py::f": 1}),
    ('def f():\n    w.append(skipped_entry_warning("  WARN: x, skipping"))\n',
     {"x.py::f": 1}),
    ('def f():\n    w.append(skipped_entry_warning("  WARN: x, skipping", kind="a"))\n',
     {}),
    ('def f():\n    w.append(gm.Finding(t, blocks="never", **where))\n', {}),
    ('def f():\n    def g():\n        return Finding(t, blocks="never")\n',
     {"x.py::f.g": 1}),
    ('w = [unclassified(x, blocks="never") for x in y]\n', {"x.py::<module>": 1}),
])
def test_site_scanner_controls(source, expected):
    assert dict(unclassified_sites(source, "x.py")) == expected


def test_baseline_is_not_vacuous():
    assert sum(_current_sites().values()) >= 1


@pytest.mark.parametrize("argv,expected", [
    (["--config-dir", "c", "--findings-json", "f.json", "--strict"],
     ("f.json", "c", False, True)),
    (["--config-dir=c", "--findings-json=f.json", "--validate"],
     ("f.json", "c", True, False)),
    (["--config-dir", "c", "--findings-json"], (None, "c", False, False)),
    (["--config-dir", "c", "--", "--findings-json", "f.json"],
     (None, "c", False, False)),
    (["--findings-json", "--validate"], (None, None, True, False)),
])
def test_early_findings_args(argv, expected):
    e = gar._early_findings_args(argv)
    assert (e.findings_json, e.config_dir, e.validate, e.strict) == expected


def test_unwritable_path_keeps_the_exception(tmp_path, monkeypatch, capsys):
    """A crash with an unwritable PATH: the ERROR line is printed and the
    original exception (its traceback) still propagates — not SystemExit."""
    out = tmp_path / "absent" / "f.json"
    monkeypatch.setattr(sys, "argv", ["gar", "--config-dir", str(tmp_path),
                                      "--dry-run", "--findings-json", str(out)])

    def boom(_args, _findings):
        raise RuntimeError("crash")

    monkeypatch.setattr(gar, "_run", boom)
    with pytest.raises(RuntimeError, match="crash"):
        gar.main()
    assert "--findings-json" in capsys.readouterr().err
    assert not out.exists()
