"""#3076：test-notification 只測路由產生器實際產出的 receiver。

產生器在路由層略過的項目（`routes[i]` 帶不支援的 key、override 缺
alertname／metric_group、主 receiver 被拒或缺少時整組 overrides／routes）
不送出、不報 DRY_RUN，改報 NOT_RENDERED 並寫明產生器的原因，算失敗。
receiver 清單取自產生器的逐項判定（``_grar_routes.tenant_slot_verdicts``），
tester 不再自己列舉 `_routing`。
"""

import contextlib
import http.server
import json
import os
import subprocess
import sys
import threading

import pytest

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(_TESTS_DIR)
_TOOLS_DIR = os.path.join(_REPO_ROOT, "scripts", "tools")
for _p in [_TOOLS_DIR, os.path.join(_TOOLS_DIR, "ops")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

import generate_alertmanager_routes as gar  # noqa: E402
import notification_tester as nt  # noqa: E402
from _grar_merge import SkippedEntryWarning  # noqa: E402
from factories import make_tenant_yaml, write_yaml  # noqa: E402

_TESTER = os.path.join(_TOOLS_DIR, "ops", "notification_tester.py")
_GENERATOR = os.path.join(_TOOLS_DIR, "ops", "generate_alertmanager_routes.py")
_MATRIX = os.path.join(_REPO_ROOT, "tests", "shared", "routing_policy_parity_matrix.json")

_OK = {"type": "webhook", "url": "https://hooks.example.com/main"}
_OK_O = {"type": "webhook", "url": "https://hooks.example.com/override"}
_OK_R = {"type": "webhook", "url": "https://hooks.example.com/route"}

# 每棵樹：(routing, 期望的 {receiver_name: status})。標籤沿用 tester 既有的
# <tenant>-main／-override-<i>／-route-<i>，索引是原始清單的索引。
TREES = {
    "routes-bogus-key": (
        {"receiver": _OK,
         "routes": [{"match": {"severity": "critical"}, "bogus": 1, "receiver": _OK_R}]},
        {"t1-main": "DRY_RUN", "t1-route-0": "NOT_RENDERED"}),
    "override-no-matcher": (
        {"receiver": _OK, "overrides": [{"receiver": _OK_O}]},
        {"t1-main": "DRY_RUN", "t1-override-0": "NOT_RENDERED"}),
    "no-main": (
        {"overrides": [{"alertname": "A", "receiver": _OK_O}]},
        {"t1-main": "INVALID_CONFIG", "t1-override-0": "NOT_RENDERED"}),
    "main-rejected": (
        {"receiver": {"type": "webhook", "url": "not a url"},
         "overrides": [{"alertname": "A", "receiver": _OK_O}],
         "routes": [{"match": {"severity": "critical"}, "receiver": _OK_R}]},
        {"t1-main": "INVALID_URL", "t1-override-0": "NOT_RENDERED",
         "t1-route-0": "NOT_RENDERED"}),
    "mixed": (
        {"receiver": _OK,
         "overrides": [{"receiver": _OK_O}, {"alertname": "B", "receiver": _OK_O}],
         "routes": [{"match": {"severity": "critical"}, "bogus": 1, "receiver": _OK_R},
                    {"match": {"severity": "warning"}, "receiver": _OK_R}]},
        {"t1-main": "DRY_RUN", "t1-override-0": "NOT_RENDERED",
         "t1-override-1": "DRY_RUN", "t1-route-0": "NOT_RENDERED",
         "t1-route-1": "DRY_RUN"}),
}


def _env():
    env = {k: v for k, v in os.environ.items()
           if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
    env["no_proxy"] = env["NO_PROXY"] = "*"
    return env


def _write_tree(tmp_path, routing):
    write_yaml(str(tmp_path), "t1.yaml", make_tenant_yaml(
        "t1", keys={"mysql_connections": 5}, routing=routing))


def _run_tester(conf_dir, *flags):
    return subprocess.run(
        [sys.executable, _TESTER, "--config-dir", str(conf_dir), "--json",
         "--rate-limit", "0", "--timeout", "5", *flags],
        capture_output=True, text=True, encoding="utf-8", timeout=120, env=_env())


def _statuses(data):
    return {r["receiver_name"]: r["status"]
            for t in data["tenants"] for r in t["receivers"]}


# ═══════════════════════════════════════════════════════════════════════
# CLI：五棵樹
# ═══════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("name", sorted(TREES))
def test_skipped_slots_are_not_rendered_and_fail_ci(tmp_path, name):
    """--dry-run --ci：路由層略過的項目報 NOT_RENDERED，rc 1。"""
    routing, expected = TREES[name]
    _write_tree(tmp_path, routing)
    proc = _run_tester(tmp_path, "--dry-run", "--ci")
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    data = json.loads(proc.stdout)
    assert _statuses(data) == expected
    assert data["status"] == "fail"
    not_rendered = [r for t in data["tenants"] for r in t["receivers"]
                    if r["status"] == "NOT_RENDERED"]
    assert not_rendered and all(r["detail"] for r in not_rendered)
    assert data["summary"]["failed"] == sum(
        1 for s in expected.values() if s != "DRY_RUN")


@pytest.mark.parametrize("name", sorted(TREES))
def test_without_ci_rc_stays_zero_but_status_is_fail(tmp_path, name):
    """不帶 --ci 時 rc 仍是 0，但 JSON status 是 fail。"""
    routing, _expected = TREES[name]
    _write_tree(tmp_path, routing)
    proc = _run_tester(tmp_path, "--dry-run")
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert json.loads(proc.stdout)["status"] == "fail"


def test_not_rendered_detail_is_the_generators_reason(tmp_path):
    """原因取自產生器的 SkippedEntryWarning，不是 tester 自己的字。"""
    routing, _ = TREES["mixed"]
    _write_tree(tmp_path, routing)
    data = json.loads(_run_tester(tmp_path, "--dry-run").stdout)
    detail = {r["receiver_name"]: r["detail"]
              for t in data["tenants"] for r in t["receivers"]}
    assert "unsupported key(s) ['bogus']" in detail["t1-route-0"]
    assert "'alertname' or 'metric_group'" in detail["t1-override-0"]
    assert not detail["t1-route-0"].startswith("WARN")
    assert not detail["t1-route-0"].endswith(", skipping")


@pytest.mark.parametrize("name", ["no-main", "main-rejected"])
def test_children_of_an_unrendered_main_name_the_main(tmp_path, name):
    routing, _ = TREES[name]
    _write_tree(tmp_path, routing)
    data = json.loads(_run_tester(tmp_path, "--dry-run").stdout)
    children = [r for t in data["tenants"] for r in t["receivers"]
                if r["receiver_name"] != "t1-main"]
    assert children
    for r in children:
        assert r["status"] == "NOT_RENDERED", r
        assert "main receiver" in r["detail"], r


def test_text_report_shows_not_rendered(tmp_path):
    routing, _ = TREES["routes-bogus-key"]
    _write_tree(tmp_path, routing)
    reports = nt.run_all_tests(str(tmp_path), dry_run=True, rate_limit=0)
    text = nt.format_text_report(reports)
    assert "NOT_RENDERED" in text and "Overall: FAIL" in text
    assert "unsupported key" in text


class _CountingHandler(http.server.BaseHTTPRequestHandler):
    hits: list = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        type(self).hits.append(self.path)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):  # noqa: D401
        pass


@contextlib.contextmanager
def _counting_server():
    _CountingHandler.hits = []
    srv = http.server.HTTPServer(("127.0.0.1", 0), _CountingHandler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", _CountingHandler.hits
    finally:
        srv.shutdown()
        srv.server_close()
        th.join(timeout=10)


def test_not_rendered_slots_are_never_sent(tmp_path):
    """不帶 --dry-run：只送產生器產出的 receiver。"""
    with _counting_server() as (base, hits):
        routing = {
            "receiver": {"type": "webhook", "url": f"{base}/main"},
            "overrides": [{"receiver": {"type": "webhook", "url": f"{base}/o0"}},
                          {"alertname": "B",
                           "receiver": {"type": "webhook", "url": f"{base}/o1"}}],
            "routes": [{"match": {"severity": "critical"}, "bogus": 1,
                        "receiver": {"type": "webhook", "url": f"{base}/r0"}},
                       {"match": {"severity": "warning"},
                        "receiver": {"type": "webhook", "url": f"{base}/r1"}}],
        }
        _write_tree(tmp_path, routing)
        proc = _run_tester(tmp_path, "--ci")
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert sorted(hits) == ["/main", "/o1", "/r1"]
    assert _statuses(json.loads(proc.stdout)) == {
        "t1-main": "OK", "t1-override-0": "NOT_RENDERED", "t1-override-1": "OK",
        "t1-route-0": "NOT_RENDERED", "t1-route-1": "OK"}


def test_children_of_a_main_rejected_main_are_not_sent(tmp_path):
    with _counting_server() as (base, hits):
        routing = {
            "receiver": {"type": "webhook", "url": "not a url"},
            "overrides": [{"alertname": "A",
                           "receiver": {"type": "webhook", "url": f"{base}/o0"}}],
            "routes": [{"match": {"severity": "critical"},
                        "receiver": {"type": "webhook", "url": f"{base}/r0"}}],
        }
        _write_tree(tmp_path, routing)
        proc = _run_tester(tmp_path)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert hits == []


# ═══════════════════════════════════════════════════════════════════════
# 產生器：被丟掉的子項目也要點名
# ═══════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("name", ["no-main", "main-rejected"])
def test_generator_validate_names_each_dropped_child(tmp_path, name):
    """主 receiver 未產出時，產生器 --validate 為每個子項目各印一行 WARN。"""
    routing, expected = TREES[name]
    _write_tree(tmp_path, routing)
    out_dir = tmp_path / "out"
    proc = subprocess.run(
        [sys.executable, _GENERATOR, "--config-dir", str(tmp_path), "--validate"],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
        cwd=str(out_dir.parent))
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    text = proc.stdout + proc.stderr
    children = [k for k in expected if k != "t1-main"]
    assert children
    for label in children:
        kind, idx = label[len("t1-"):].split("-")
        ref = f"override[{idx}]" if kind == "override" else f"routes[{idx}]"
        assert any(ref in line and "main receiver" in line
                   for line in text.splitlines()), (ref, text)


def test_build_tenant_routes_emits_a_skipped_entry_per_dropped_child():
    routing, _ = TREES["main-rejected"]
    _routes, _receivers, warnings = gar._build_tenant_routes({"t1": routing})
    hits = [w for w in warnings
            if getattr(w, "kind", None) == "parent_receiver_not_rendered"]
    assert [w.field for w in hits] == ["overrides[0]", "routes[0]"]
    assert all(isinstance(w, SkippedEntryWarning) for w in hits)
    assert all(w in gar.blocking_generation_errors(warnings) for w in hits)


def test_slot_verdicts_cover_every_authored_slot():
    routing, _ = TREES["mixed"]
    verdicts = gar.tenant_slot_verdicts("t1", routing)
    assert [(v.slot, v.receiver_name) for v in verdicts] == [
        ("receiver", "tenant-t1"),
        ("overrides[0]", None), ("overrides[1]", "tenant-t1-override-1"),
        ("routes[0]", None), ("routes[1]", "tenant-t1-route-1")]
    assert [v.stage for v in verdicts] == [None, "entry", None, "entry", None]
    assert all(v.reason is not None for v in verdicts if v.receiver_name is None)


@pytest.mark.parametrize("routing,slots", [
    ({"receiver": _OK, "overrides": "x"}, [("receiver", None), ("overrides", "entry")]),
    ({"receiver": _OK, "routes": "x"}, [("receiver", None), ("routes", "entry")]),
    ({"overrides": "x", "routes": [{"match": {"a": "b"}, "receiver": _OK_R}]},
     [("receiver", "receiver"), ("overrides", "parent"), ("routes[0]", "parent")]),
    ({"receiver": _OK, "overrides": [{"alertname": "A"}]},
     [("receiver", None), ("overrides[0]", "receiver")]),
    ({"receiver": _OK, "overrides": ["x"]}, [("receiver", None), ("overrides[0]", "entry")]),
], ids=["overrides-not-list", "routes-not-list", "parent-of-non-list",
        "override-missing-receiver", "override-not-mapping"])
def test_slot_verdict_shapes(routing, slots):
    verdicts = gar.tenant_slot_verdicts("t1", routing)
    assert [(v.slot, v.stage) for v in verdicts] == slots
    assert all(v.reason is not None for v in verdicts if v.stage is not None)


def test_ledger_reasons_are_the_warning_stream_lines():
    """判定的原因就是產生器警告流裡那一行（同一個物件），不是另寫一份。"""
    routing, _ = TREES["mixed"]
    ledger = []
    _routes, _receivers, warnings = gar._build_tenant_routes(
        {"t1": routing}, verdicts=ledger)
    for v in ledger:
        if v.reason is not None:
            assert any(v.reason is w for w in warnings)


# ═══════════════════════════════════════════════════════════════════════
# Parity：tester 測的集合 == 產生器產出的 receiver
# ═══════════════════════════════════════════════════════════════════════
def _generator_name(label: str, tenant: str) -> str:
    rest = label[len(tenant) + 1:]
    return f"tenant-{tenant}" if rest == "main" else f"tenant-{tenant}-{rest}"


def test_tested_set_equals_generator_receivers_across_parity_matrix(tmp_path):
    with open(_MATRIX, encoding="utf-8") as fh:
        trees = json.load(fh)["trees"]
    compared = 0
    for tree in trees:
        root = tmp_path / tree["name"]
        root.mkdir()
        for rel, body in tree["files"].items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            write_yaml(str(path.parent), path.name, body)
        try:
            reports = nt.run_all_tests(str(root), dry_run=True, rate_limit=0)
        except nt.RoutingTreeRefused:
            continue
        tree_obj = gar.load_tenant_tree(str(root))
        _r, receivers, _w = gar._build_tenant_routes(tree_obj.routing_configs)
        generated = {r["name"] for r in receivers}
        for tenant, routing in tree_obj.routing_configs.items():
            for v in gar.tenant_slot_verdicts(tenant, routing):
                if not v.rendered:  # every drop carries the slot's own line
                    assert v.reason is not None, (tree["name"], tenant, v.slot)
                    assert (v.reason.field == v.slot
                            or v.reason.field.startswith(v.slot + ".")), (tree["name"], v)
        tested = {_generator_name(r.receiver_name, rep.tenant)
                  for rep in reports for r in rep.receivers
                  if r.status == nt.STATUS_DRY_RUN}
        listed_not_tested = {_generator_name(r.receiver_name, rep.tenant)
                             for rep in reports for r in rep.receivers
                             if r.status != nt.STATUS_DRY_RUN}
        assert tested == generated, tree["name"]
        assert not (listed_not_tested & generated), tree["name"]
        compared += 1
    assert compared >= 40, compared


def test_explain_route_lists_children_of_a_rejected_main_as_skipped():
    """explain-route 取同一份產生器輸出：主 receiver 被拒時，子路由列在略過清單。"""
    import explain_route as er

    routing, _ = TREES["main-rejected"]
    sub_routes, skipped = er.effective_sub_routes("t1", routing)
    assert sub_routes == []
    assert [w.field for w in skipped] == ["overrides[0]", "routes[0]"]


# ═══════════════════════════════════════════════════════════════════════
# explain-route：同一份逐項判定
# ═══════════════════════════════════════════════════════════════════════
_EXPLAIN = os.path.join(_TOOLS_DIR, "ops", "explain_route.py")


def _run_explain(conf_dir, *flags):
    return subprocess.run(
        [sys.executable, _EXPLAIN, "--config-dir", str(conf_dir), *flags],
        capture_output=True, text=True, encoding="utf-8", timeout=120)


def test_explain_route_lists_children_of_a_missing_main(tmp_path):
    """#3076：租戶沒有主 receiver 時，explain-route 也逐一列出被丟掉的子項目與產生器的原因。"""
    routing = {"overrides": [{"alertname": "A", "receiver": _OK_O}],
               "routes": [{"match": {"severity": "critical"}, "receiver": _OK_R}]}
    _write_tree(tmp_path, routing)
    proc = _run_explain(tmp_path, "--json")
    assert proc.returncode == 0, proc.stderr
    (t1,) = [t for t in json.loads(proc.stdout) if t["tenant"] == "t1"]
    assert t1["sub_routes"] == []
    assert [w.strip() for w in t1["skipped_sub_routes"]] == [
        "WARN: t1: override[0] not rendered: the tenant's main receiver is not rendered, skipping",
        "WARN: t1: routes[0] not rendered: the tenant's main receiver is not rendered, skipping"]
    text = _run_explain(tmp_path)
    assert text.returncode == 0, text.stderr
    assert "Not in effect (skipped by the generator):" in text.stdout
    assert "override[0] not rendered" in text.stdout
    assert "routes[0] not rendered" in text.stdout


def _slot_ref(label: str, tenant: str) -> str:
    kind, _, idx = label[len(tenant) + 1:].partition("-")
    return {"override": f"override[{idx}]", "route": f"routes[{idx}]",
            "overrides": "'overrides'", "routes": "'routes'"}[kind]


# explain-route --json crashes on these trees on main already (a YAML date
# value in `final` is not JSON serializable) — tracked in #3144. Recognised by
# the crash line, not by rc 2: under the test suite a crash ends at
# DA_TEST_CRASH_EXIT_CODE (#3092).
_EXPLAIN_JSON_CRASHES = {"routing-values-yaml11"}


def test_explain_tester_and_generator_agree_across_parity_matrix(tmp_path):
    """explain-route 的生效子路由 == tester 測的子項目 == 產生器產出的子 receiver；
    tester 沒測的子項目，explain-route 都列在略過清單。"""
    with open(_MATRIX, encoding="utf-8") as fh:
        trees = json.load(fh)["trees"]
    compared = 0
    for tree in trees:
        root = tmp_path / tree["name"]
        root.mkdir()
        for rel, body in tree["files"].items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            write_yaml(str(path.parent), path.name, body)
        try:
            reports = nt.run_all_tests(str(root), dry_run=True, rate_limit=0)
        except nt.RoutingTreeRefused:
            continue
        proc = _run_explain(root, "--json")
        if tree["name"] in _EXPLAIN_JSON_CRASHES:
            assert proc.returncode not in (0, 1) and \
                "unexpected internal error (TypeError)" in proc.stderr and \
                "not JSON serializable" in proc.stderr, \
                "fixed? drop it from _EXPLAIN_JSON_CRASHES"
            continue
        assert proc.returncode == 0, (tree["name"], proc.stderr)
        explained = {t["tenant"]: t for t in json.loads(proc.stdout)}
        _r, receivers, _w = gar._build_tenant_routes(
            gar.load_tenant_tree(str(root)).routing_configs)
        generated = {r["name"] for r in receivers}
        for rep in reports:
            t = rep.tenant
            main = f"tenant-{t}"
            gen_children = {n for n in generated
                            if n.startswith(main + "-override-") or n.startswith(main + "-route-")}
            tested = {_generator_name(r.receiver_name, t) for r in rep.receivers
                      if r.status == nt.STATUS_DRY_RUN and not r.receiver_name.endswith("-main")}
            exp = explained[t]
            in_effect = {s["receiver"] for s in exp["sub_routes"]}
            assert in_effect == tested == gen_children, (tree["name"], t)
            dropped = [r.receiver_name for r in rep.receivers
                       if r.status != nt.STATUS_DRY_RUN and not r.receiver_name.endswith("-main")]
            for label in dropped:
                ref = _slot_ref(label, t)
                # a receiver refusal names the slot as <t>-override-<i> / <t>-route-<i>
                assert any(ref in w or f"{label}:" in w
                           for w in exp["skipped_sub_routes"]), (tree["name"], label)
        compared += 1
    assert compared >= 40, compared


def test_explain_route_in_effect_children_come_from_the_verdicts():
    """生效子路由的 source 是產生器的 slot（原始索引），略過的 slot 不列。"""
    import explain_route as er

    routing, _ = TREES["mixed"]
    sub_routes, skipped = er.effective_sub_routes("t1", routing)
    assert [(s["source"], s["receiver"]) for s in sub_routes] == [
        ("overrides[1]", "tenant-t1-override-1"), ("routes[1]", "tenant-t1-route-1")]
    assert [w.field for w in skipped] == ["overrides[0]", "routes[0]"]


def test_explain_route_keeps_group_by_drops_of_a_rendered_child():
    """產出的子路由被丟掉的 group_by 元素（#2503）照舊列在略過清單。"""
    import explain_route as er

    routing = {"receiver": _OK,
               "overrides": [{"alertname": "A", "group_by": ["alertname", True],
                              "receiver": _OK_O}]}
    sub_routes, skipped = er.effective_sub_routes("t1", routing)
    assert [s["source"] for s in sub_routes] == ["overrides[0]"]
    assert [w.field for w in skipped] == ["overrides[0].group_by[1]"]


@pytest.mark.parametrize("where", ["receiver", "overrides[0]", "routes[0]"])
def test_domain_refusal_is_the_reason_of_the_slot_it_drops(where):
    """--policy allowed_domains 拒收的 slot，原因是那一行 domain 拒收，不是別的行。"""
    bad = {"type": "webhook", "url": "https://evil.example.org/hook"}
    routing = {"receiver": bad if where == "receiver" else _OK,
               "overrides": [{"alertname": "A",
                              "receiver": bad if where == "overrides[0]" else _OK_O}],
               "routes": [{"match": {"severity": "critical"},
                           "receiver": bad if where == "routes[0]" else _OK_R}]}
    verdicts = gar.tenant_slot_verdicts("t1", routing, ["*.example.com"])
    (v,) = [v for v in verdicts if v.slot == where]
    assert (v.rendered, v.stage) == (False, "receiver")
    assert v.reason.kind == "receiver_domain_not_allowed"
    assert v.reason.field == ("receiver.url" if where == "receiver"
                              else f"{where}.receiver.url")
    if where == "receiver":
        assert [(c.stage, c.reason.kind) for c in verdicts if c.slot != "receiver"] == [
            ("parent", "parent_receiver_not_rendered")] * 2
