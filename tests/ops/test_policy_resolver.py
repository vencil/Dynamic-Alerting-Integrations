"""#3142／#3208：部署端 `--policy` 由產生器的同一個 resolver 讀，四支工具共用。

generate-routes、validate-config、test-notification、explain-route 的
`--policy` 都經 ``generate_alertmanager_routes.resolve_policy``：同一個旗標、
同一種讀法、同一道 ``min_generator_version`` 閘門（ADR-036 §6）、同一套訊息。
test-notification 與 explain-route 沒帶 `--policy` 時只印 NOTICE、不改結束碼
（ADR-036 §5）。
"""

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(_TESTS_DIR)
_TOOLS_DIR = os.path.join(_REPO_ROOT, "scripts", "tools")
for _p in [_TOOLS_DIR, os.path.join(_TOOLS_DIR, "ops")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

import generate_alertmanager_routes as gar  # noqa: E402
import notification_tester as nt  # noqa: E402
import explain_route as er  # noqa: E402
from factories import make_tenant_yaml, write_yaml  # noqa: E402

_OPS = Path(_TOOLS_DIR) / "ops"
_TESTER = _OPS / "notification_tester.py"
_EXPLAIN = _OPS / "explain_route.py"
_VALIDATE = _OPS / "validate_config.py"
_GENERATOR = _OPS / "generate_alertmanager_routes.py"
_MATRIX = Path(_REPO_ROOT) / "tests" / "shared" / "routing_policy_parity_matrix.json"

_ALLOW = ["*.example.com"]
_OK = {"type": "webhook", "url": "https://hooks.example.com/main"}
_OK_O = {"type": "webhook", "url": "https://hooks.example.com/override"}
_BAD = {"type": "webhook", "url": "https://evil.example.org/hook"}


def _tree(tmp_path, routing):
    conf = tmp_path / "conf.d"
    conf.mkdir()
    write_yaml(str(conf), "t1.yaml", make_tenant_yaml(
        "t1", keys={"mysql_connections": 5}, routing=routing))
    return conf


def _policy(tmp_path, body):
    p = tmp_path / "policy.yaml"
    p.write_text(body, encoding="utf-8")
    return str(p)


def _allow(tmp_path):
    return _policy(tmp_path, "allowed_domains: ['*.example.com']\n")


def _too_new(tmp_path):
    return _policy(tmp_path, 'allowed_domains: []\nmin_generator_version: "999.0.0"\n')


def _run(tool, *argv):
    return subprocess.run([sys.executable, str(tool), *argv], capture_output=True,
                          text=True, encoding="utf-8", timeout=180)


# ── test-notification ────────────────────────────────────────────────


def _tester(conf, *flags):
    return _run(_TESTER, "--config-dir", str(conf), "--json", "--dry-run",
                "--rate-limit", "0", *flags)


def _statuses(doc):
    return {r["receiver_name"]: r["status"]
            for t in doc["tenants"] for r in t["receivers"]}


def test_tester_policy_refused_main_is_not_sent_and_fails_ci(tmp_path):
    conf = _tree(tmp_path, {"receiver": _BAD,
                            "overrides": [{"alertname": "A", "receiver": _OK_O}]})
    pol = _allow(tmp_path)
    ci = _tester(conf, "--policy", pol, "--ci")
    assert ci.returncode == 1, ci.stderr
    doc = json.loads(ci.stdout)
    assert doc["policy_applied"] is True
    assert _statuses(doc) == {"t1-main": "POLICY_REFUSED", "t1-override-0": "NOT_RENDERED"}
    (main,) = [r for t in doc["tenants"] for r in t["receivers"]
               if r["receiver_name"] == "t1-main"]
    assert "not in allowed_domains" in main["detail"]
    assert gar.POLICY_REFUSAL_REMEDY in main["detail"]
    # without --ci the verdict is the same, rc 0 (owner-approved: new path)
    no_ci = _tester(conf, "--policy", pol)
    assert no_ci.returncode == 0, no_ci.stderr
    assert _statuses(json.loads(no_ci.stdout)) == _statuses(doc)
    assert gar.POLICY_NOT_APPLIED_NOTICE not in ci.stderr + no_ci.stderr


def test_tester_policy_refused_override_only(tmp_path):
    conf = _tree(tmp_path, {"receiver": _OK,
                            "overrides": [{"alertname": "A", "receiver": _BAD}]})
    res = _tester(conf, "--policy", _allow(tmp_path), "--ci")
    assert res.returncode == 1, res.stderr
    assert _statuses(json.loads(res.stdout)) == {
        "t1-main": "DRY_RUN", "t1-override-0": "POLICY_REFUSED"}


def test_tester_without_policy_says_so_and_keeps_rc(tmp_path):
    conf = _tree(tmp_path, {"receiver": _BAD})
    res = _tester(conf, "--ci")
    assert res.returncode == 0, res.stderr
    doc = json.loads(res.stdout)
    assert doc["policy_applied"] is False
    assert _statuses(doc) == {"t1-main": "DRY_RUN"}
    assert gar.POLICY_NOT_APPLIED_NOTICE in res.stderr


@pytest.mark.parametrize("kind", ["too_new", "not_a_file", "no_key"])
def test_tester_unusable_policy_is_rc2_with_envelope(tmp_path, kind):
    conf = _tree(tmp_path, {"receiver": _OK})
    pol = {"too_new": lambda: _too_new(tmp_path),
           "not_a_file": lambda: str(tmp_path / "missing.yaml"),
           "no_key": lambda: _policy(tmp_path, "denied_functions: []\n")}[kind]()
    res = _tester(conf, "--policy", pol)
    assert res.returncode == 2, res.stderr
    assert "Traceback" not in res.stderr
    doc = json.loads(res.stdout)
    assert (doc["status"], doc["reason"]) == ("caller_error", "policy_unusable"), doc
    assert doc["tenants"] == [] and doc["policy_applied"] is False


# ── explain-route ────────────────────────────────────────────────────


@pytest.mark.parametrize("where", ["receiver", "overrides[0]"])
def test_explain_names_what_the_policy_refuses(tmp_path, where):
    routing = {"receiver": _BAD if where == "receiver" else _OK,
               "overrides": [{"alertname": "A",
                              "receiver": _BAD if where == "overrides[0]" else _OK_O}]}
    conf = _tree(tmp_path, routing)
    res = _run(_EXPLAIN, "--config-dir", str(conf), "--json", "--policy", _allow(tmp_path))
    assert res.returncode == 0, res.stderr
    (t1,) = json.loads(res.stdout)
    assert t1["policy_applied"] is True
    assert [(r["source"], r["status"]) for r in t1["policy_refused"]] == [
        (where, "POLICY_REFUSED")]
    assert t1["sub_routes"] == []
    text = _run(_EXPLAIN, "--config-dir", str(conf), "--policy", _allow(tmp_path))
    assert "Refused by the --policy allowed_domains" in text.stdout, text.stdout
    # the same tree without --policy: nothing refused, and a NOTICE
    bare = _run(_EXPLAIN, "--config-dir", str(conf), "--json")
    (b1,) = json.loads(bare.stdout)
    assert (b1["policy_applied"], b1["policy_refused"]) == (False, [])
    assert [s["source"] for s in b1["sub_routes"]] == ["overrides[0]"]
    assert gar.POLICY_NOT_APPLIED_NOTICE in bare.stderr


def test_explain_trace_renders_the_tree_under_the_policy(tmp_path):
    """`--trace` 追的是產生器在 policy 下真正產出的樹：被拒收的 receiver 不在裡面。"""
    conf = _tree(tmp_path, {"receiver": _OK,
                            "overrides": [{"alertname": "A", "receiver": _BAD}]})
    parsed = gar._parse_config_files(str(conf))
    _yml, _root, receivers, _types = er.build_trace_tree(parsed, None, _ALLOW)
    assert "tenant-t1" in receivers and "tenant-t1-override-0" not in receivers
    _yml, _root, receivers, _types = er.build_trace_tree(parsed, None, None)
    assert "tenant-t1-override-0" in receivers
    res = _run(_EXPLAIN, "--config-dir", str(conf), "--trace", "--tenant", "t1",
               "--alertname", "A", "--json", "--policy", _allow(tmp_path))
    assert res.returncode == 0, res.stderr
    (trace,) = json.loads(res.stdout)
    assert trace["policy_applied"] is True
    assert [r["source"] for r in trace["policy_refused"]] == ["overrides[0]"]


def test_explain_unusable_policy_is_rc2(tmp_path):
    conf = _tree(tmp_path, {"receiver": _OK})
    for flags in ([], ["--trace", "--tenant", "t1"]):
        res = _run(_EXPLAIN, "--config-dir", str(conf), "--json",
                   "--policy", _too_new(tmp_path), *flags)
        assert res.returncode == 2, res.stderr
        doc = json.loads(res.stdout)
        assert doc["reason"] == "policy_unusable", doc
        assert "min_generator_version" in doc["error"]


# ── validate-config ──────────────────────────────────────────────────


def test_validate_config_refuses_an_unmet_min_generator_version(tmp_path):
    """owner 核可的唯一既有呼叫 rc 變更：0 → 2（routes 與 policy 兩列都擋）。"""
    conf = _tree(tmp_path, {"receiver": _OK})
    res = _run(_VALIDATE, "--config-dir", str(conf), "--json", "--policy", _too_new(tmp_path))
    assert res.returncode == 2, res.stdout + res.stderr
    rows = {r["check"]: r for r in json.loads(res.stdout)}
    for name in ("routes", "policy"):
        assert rows[name]["status"] == "fail" and rows[name]["caller_error"], rows[name]
        assert any("min_generator_version" in d for d in rows[name]["details"]), rows[name]


def test_validate_config_policy_fail_points_at_the_policy_file(tmp_path):
    """#3208：違規時的建議指 `--policy` 檔的 allowed_domains，不指 `_domain_policy.yaml`。"""
    conf = _tree(tmp_path, {"receiver": _BAD})
    res = _run(_VALIDATE, "--config-dir", str(conf), "--json", "--policy", _allow(tmp_path))
    assert res.returncode == 1, res.stdout + res.stderr
    (row,) = [r for r in json.loads(res.stdout) if r["check"] == "policy"]
    assert row["status"] == "fail"
    assert row["suggested_action"] == gar.POLICY_REFUSAL_REMEDY
    assert "_domain_policy.yaml" not in row["suggested_action"]
    assert row["docs_link"].endswith("/cli-reference/#validate-config"), row["docs_link"]


def test_validate_config_and_generate_routes_print_no_notice(tmp_path):
    """NOTICE 只給本來沒有 --policy 的 test-notification／explain-route；
    validate-config 與 generate-routes 沒帶時的提示由 CI 模板負責（ADR-036 §5）。"""
    conf = _tree(tmp_path, {"receiver": _OK})
    for tool, extra in ((_VALIDATE, []), (_GENERATOR, ["--dry-run"])):
        res = _run(tool, "--config-dir", str(conf), *extra)
        assert gar.POLICY_NOT_APPLIED_NOTICE not in res.stderr, tool.name


# ── one resolver ─────────────────────────────────────────────────────


@pytest.mark.parametrize("tool", [_GENERATOR, _VALIDATE, _TESTER, _EXPLAIN],
                         ids=lambda p: p.stem)
def test_every_tool_reads_policy_through_the_one_resolver(tool):
    """結構閘門：這四支只經 `resolve_policy` 讀 --policy，不直接叫
    `load_policy` / `load_policy_document`，也不自己宣告 `--policy`。"""
    tree = ast.parse(tool.read_text(encoding="utf-8"))
    # the resolver's (and the flag's) own body is where the file is read
    tree.body = [n for n in tree.body
                 if not (isinstance(n, ast.FunctionDef)
                         and n.name in ("resolve_policy", "add_policy_argument"))]
    names = {n.attr if isinstance(n, ast.Attribute) else n.id
             for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             for n in [n.func] if isinstance(n, (ast.Attribute, ast.Name))}
    if tool != _GENERATOR:
        assert "resolve_policy" in names, tool.name
    else:
        assert "resolve_policy" in names or "resolve_policy" in {
            f.name for f in ast.parse(tool.read_text(encoding="utf-8")).body
            if isinstance(f, ast.FunctionDef)}, tool.name
    assert not names & {"load_policy", "load_policy_document"}, tool.name
    own_flag = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                and getattr(n.func, "attr", None) == "add_argument"
                and any(isinstance(a, ast.Constant) and a.value == "--policy" for a in n.args)]
    if tool != _VALIDATE:  # validate-config's --policy also feeds lint_custom_rules
        assert own_flag == [], tool.name
        assert "add_policy_argument" in names, tool.name


def test_tester_explain_and_generator_agree_under_a_policy_across_parity_matrix(tmp_path):
    """parity 加 policy 維度：同一份 allowlist 下，tester 測的 == 產生器產出的；
    tester 標 POLICY_REFUSED 的 slot == 產生器以 domain 拒收的 slot == explain 列出的。"""
    trees = json.loads(_MATRIX.read_text(encoding="utf-8"))["trees"]
    compared = refused_seen = 0
    for tree in trees:
        root = tmp_path / tree["name"]
        root.mkdir()
        for rel, body in tree["files"].items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            write_yaml(str(path.parent), path.name, body)
        try:
            reports = nt.run_all_tests(str(root), dry_run=True, rate_limit=0,
                                       allowed_domains=_ALLOW)
        except nt.RoutingTreeRefused:
            continue
        routing_configs = gar.load_tenant_tree(str(root)).routing_configs
        parsed = gar._parse_config_files(str(root))
        for rep in reports:
            t = rep.tenant
            gen_refused = {v.slot for v in gar.tenant_slot_verdicts(t, routing_configs[t], _ALLOW)
                           if not v.rendered and gar.is_policy_refusal(v.reason)}
            tester_refused = {r.receiver_name for r in rep.receivers
                              if r.status == nt.STATUS_POLICY_REFUSED}
            assert len(tester_refused) == len(gen_refused), (tree["name"], t)
            if t in parsed.get("all_tenants", []):
                exp = er.explain_tenant_routing(parsed, t, _ALLOW)
                assert {r["source"] for r in exp["policy_refused"]} == gen_refused, \
                    (tree["name"], t)
            refused_seen += len(gen_refused)
        _r, receivers, _w = gar._build_tenant_routes(routing_configs, _ALLOW)
        generated = {r["name"] for r in receivers}
        tested = {f"tenant-{rep.tenant}" if r.receiver_name == f"{rep.tenant}-main"
                  else f"tenant-{rep.tenant}-{r.receiver_name[len(rep.tenant) + 1:]}"
                  for rep in reports for r in rep.receivers if r.status == nt.STATUS_DRY_RUN}
        assert tested == generated, tree["name"]
        compared += 1
    assert compared >= 40, compared
    assert refused_seen >= 20, refused_seen


# ── review findings (#3142) ──────────────────────────────────────────


def test_explain_names_a_refused_enforced_receiver(tmp_path):
    """被 allowlist 拒收的平台強制路由 receiver 也列在 policy_refused。"""
    conf = _tree(tmp_path, {"receiver": _OK})
    (conf / "_defaults.yaml").write_text(
        "defaults: {}\n_routing_enforced:\n  enabled: true\n  receiver:\n"
        "    type: webhook\n    url: https://noc.evil.org/hook\n", encoding="utf-8")
    res = _run(_EXPLAIN, "--config-dir", str(conf), "--json", "--policy", _allow(tmp_path))
    assert res.returncode == 0, res.stderr
    (t1,) = json.loads(res.stdout)
    assert [(r["source"], r["status"]) for r in t1["policy_refused"]] == [
        ("_routing_enforced.receiver", "POLICY_REFUSED")]
    bare = _run(_EXPLAIN, "--config-dir", str(conf), "--json")
    assert json.loads(bare.stdout)[0]["policy_refused"] == []


def test_validate_config_version_error_gets_the_version_remedy(tmp_path):
    conf = _tree(tmp_path, {"receiver": _OK})
    res = _run(_VALIDATE, "--config-dir", str(conf), "--json", "--policy", _too_new(tmp_path))
    rows = {r["check"]: r for r in json.loads(res.stdout)}
    for name in ("routes", "policy"):
        assert rows[name]["suggested_action"] == gar.POLICY_VERSION_REMEDY, rows[name]


def test_validate_config_allowlist_advice_only_for_an_allowlist_refusal(tmp_path):
    """租戶 id 含 "domain" 的路由 WARN 被 policy 列的字串過濾撿到時，建議不是
    「改 allowlist」，而是路由的建議（那一行講的是路由）。"""
    conf = _tree(tmp_path, {"receiver": _OK, "overrides": [{"receiver": _OK_O}]})
    (conf / "t1.yaml").rename(conf / "domain-a.yaml")
    text = (conf / "domain-a.yaml").read_text(encoding="utf-8").replace("t1:", "domain-a:")
    (conf / "domain-a.yaml").write_text(text, encoding="utf-8")
    res = _run(_VALIDATE, "--config-dir", str(conf), "--json", "--policy", _allow(tmp_path))
    (row,) = [r for r in json.loads(res.stdout) if r["check"] == "policy"]
    assert row["status"] == "fail", row
    assert row["suggested_action"] != gar.POLICY_REFUSAL_REMEDY, row
    assert "allowed_domains" not in row["suggested_action"], row


def test_explain_profile_expansion_prints_no_notice(tmp_path):
    conf = _tree(tmp_path, {"receiver": _OK})
    res = _run(_EXPLAIN, "--config-dir", str(conf), "--show-profile-expansion")
    assert res.returncode == 0, res.stderr
    assert gar.POLICY_NOT_APPLIED_NOTICE not in res.stderr
