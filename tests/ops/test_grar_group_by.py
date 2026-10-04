"""#2503：`group_by` 的元素必須是非空字串、不可重複，`...` 不可與其他 label 並存。

量測依據（Alertmanager v0.34.1，部署 manifest 未開 feature flag ⇒ UTF-8 label
驗證）：`[alertname, 8]`／`[alertname, true]` 會被**接受**，但以名叫 `"8"`／
`"true"` 的 label 分組（靜默分錯組）；`[alertname, true, true]`、
`[alertname, alertname]`（重複）、`[alertname, "..."]`（wildcard 混用）、
`[alertname, ""]`（空字串）整份 config 被拒收；`["..."]` 單獨合法；加引號的
`"8"` 合法（不套 classic label regex）。

釘住的契約：

* 述詞只有一個：`_grar_validate.group_by_problems`（單一列表）與
  `routing_group_by_invalid`（解析後 routing 的主 route、`overrides[i]`、
  `routes[i]`）；Go 側是 `routingpolicy.GroupByInvalid`，兩邊由
  `tests/shared/routing_policy_parity_matrix.json` 的 `group_by_invalid` 欄對齊。
* 產生器會輸出 group_by 的五個位置——租戶主 route、`overrides[i]`、
  `routes[i]`、`_routing_enforced`（單一與 `{{tenant}}` 逐租戶）——全部經同一個
  修正函式。
* `--strict`：每個問題元素一行 `ERROR:`，rc 1。
* 非 strict：`WARN … skipping` 並修正後輸出（略過非字串／空字串、略過後出現的
  重複、混用時略過 `...`）；修正後為空就不輸出 group_by。

Fixture 的 tenant 名一律是 demo 名（tenant-agnostic）。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from _grar_validate import (
    POLICY_ERROR_PREFIX,
    blocking_generation_errors,
    group_by_problems,
    routing_group_by_invalid,
)
from generate_alertmanager_routes import generate_routes, load_tenant_tree

REPO = Path(__file__).resolve().parents[2]
_GAR = REPO / "scripts" / "tools" / "ops" / "generate_alertmanager_routes.py"
_T = "demo-apac"

_MAIN = ("      receiver:\n        type: pagerduty\n"
         "        service_key: main-key\n")
_WEBHOOK = ("        receiver:\n          type: webhook\n"
            "          url: https://hooks.example.com/x\n")


def _tree(tmp_path: Path, routing: str, extra: dict[str, str] | None = None) -> Path:
    d = tmp_path / "conf.d"
    d.mkdir()
    (d / "_defaults.yaml").write_text("defaults:\n  cpu_usage_percent: 80\n",
                                      encoding="utf-8")
    body = (f"tenants:\n  {_T}:\n    cpu_usage_percent: '85'\n"
            "    _routing:\n" + routing)
    (d / f"{_T}.yaml").write_text(body, encoding="utf-8")
    for name, text in (extra or {}).items():
        (d / name).write_text(text, encoding="utf-8")
    return d


def _gar(d: Path, *flags: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_GAR), "--config-dir", str(d), *flags],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300)


def _strict_errors(d: Path) -> list[str]:
    tree = load_tenant_tree(str(d), strict_policies=True)
    return [w.strip() for w in tree.schema_warnings
            if w.lstrip().startswith(POLICY_ERROR_PREFIX) and "group_by[" in w]


def _render(d: Path) -> tuple[list[dict], list[str]]:
    tree = load_tenant_tree(str(d))
    routes, _receivers, warnings = generate_routes(
        tree.routing_configs, enforced_routing=tree.enforced_routing,
        tenants=tree.dedup_configs)
    return routes, warnings


def _tenant_route(routes: list[dict]) -> dict:
    return next(r for r in routes if r.get("matchers") == [f'tenant="{_T}"'])


class TestPredicate:
    @pytest.mark.parametrize("group_by, kept, problems", [
        (["alertname", "tenant"], ["alertname", "tenant"], []),
        (["..."], ["..."], []),
        (["alertname", "8"], ["alertname", "8"], []),       # quoted: a UTF-8 label name
        (["alertname", "a b"], ["alertname", "a b"], []),   # AM v0.34.1 accepts it
        (["alertname", True, 8], ["alertname"],
         [(1, "not_string", True), (2, "not_string", 8)]),
        ([True, True], [], [(0, "not_string", True), (1, "not_string", True)]),
        (["alertname", None], ["alertname"], [(1, "not_string", None)]),
        (["alertname", ""], ["alertname"], [(1, "empty", "")]),
        (["alertname", "alertname", "tenant", "tenant"], ["alertname", "tenant"],
         [(1, "duplicate", "alertname"), (3, "duplicate", "tenant")]),
        (["alertname", "..."], ["alertname"], [(1, "wildcard_mixed", "...")]),
        # F2 (#2503 round 2): AM's repeat check skips the wildcard —
        # ['...', '...'] is SUCCESS in amtool v0.34.1, so no finding and no
        # repair; every '...' beside a label is wildcard_mixed.
        (["...", "alertname", "..."], ["alertname"],
         [(0, "wildcard_mixed", "..."), (2, "wildcard_mixed", "...")]),
        (["...", "..."], ["...", "..."], []),
        (["alertname", "...", "..."], ["alertname"],
         [(1, "wildcard_mixed", "..."), (2, "wildcard_mixed", "...")]),
        (["...", 8], ["..."], [(1, "not_string", 8)]),       # 8 dropped first: '...' alone
        ([], [], []),
    ])
    def test_rules(self, group_by, kept, problems):
        assert group_by_problems(group_by) == (kept, problems)

    def test_routing_fields_in_render_order(self):
        rc = {"group_by": ["alertname", True],
              "overrides": [{"group_by": ["x", "x"]}, "not-a-dict", {"group_by": "x"}],
              "routes": [{"group_by": ["a", "..."]}, {"group_by": []}]}
        assert routing_group_by_invalid(rc) == [
            ("group_by[1]", "not_string", True),
            ("overrides[0].group_by[1]", "duplicate", "x"),
            ("routes[0].group_by[1]", "wildcard_mixed", "...")]

    @pytest.mark.parametrize("rc", [None, "disable", {}, {"group_by": "alertname"},
                                    {"overrides": "x", "routes": 1}])
    def test_nothing_to_report(self, rc):
        assert routing_group_by_invalid(rc) == []


class TestStrict:
    """`--strict`：每個位置、每個問題元素一行 ERROR。"""

    def test_issue_repro_tenant_main(self, tmp_path):
        d = _tree(tmp_path, _MAIN + "      group_by: [alertname, on, 8]\n")
        errs = _strict_errors(d)
        assert [e.split(": ", 2)[2].split(" ")[0] for e in errs] == ["group_by[1]", "group_by[2]"], errs
        assert "group_by[1] is bool True, not a string" in errs[0], errs
        assert "group_by[2] is int 8, not a string" in errs[1], errs

    def test_override_and_routes(self, tmp_path):
        routing = (_MAIN + "      overrides:\n      - alertname: X\n"
                   "        group_by: [alertname, alertname]\n" + _WEBHOOK
                   + "      routes:\n      - match: {team: db}\n"
                   "        group_by: [alertname, \"...\"]\n" + _WEBHOOK)
        errs = _strict_errors(_tree(tmp_path, routing))
        assert len(errs) == 2, errs
        assert "overrides[0].group_by[1] repeats label 'alertname'" in errs[0], errs
        assert "routes[0].group_by[1] is '...' alongside other labels" in errs[1], errs

    def test_routing_defaults_and_profile_are_judged_resolved(self, tmp_path):
        extra = {"_routing_defaults.yaml": "_routing_defaults:\n  group_by: [alertname, yes]\n",
                 "_routing_profiles.yaml": "routing_profiles:\n  p:\n    routes:\n"
                 "    - match: {team: db}\n      group_by: ['']\n"
                 "      receiver: {type: webhook, url: https://hooks.example.com/x}\n"}
        d = _tree(tmp_path, _MAIN)
        (d / f"{_T}.yaml").write_text(
            f"tenants:\n  {_T}:\n    cpu_usage_percent: '85'\n"
            "    _routing_profile: p\n    _routing:\n" + _MAIN, encoding="utf-8")
        for name, text in extra.items():
            (d / name).write_text(text, encoding="utf-8")
        errs = _strict_errors(d)
        assert len(errs) == 2, errs
        assert f"tenant '{_T}': group_by[1] is bool True" in errs[0], errs
        assert f"tenant '{_T}': routes[0].group_by[0] is an empty string" in errs[1], errs

    def test_routing_enforced(self, tmp_path):
        enforced = ("_routing_enforced:\n  enabled: true\n  receiver:\n"
                    "    type: webhook\n    url: https://noc.example.com/x\n"
                    "  group_by: [alertname, 8]\n")
        errs = _strict_errors(_tree(tmp_path, _MAIN, {"_routing_enforced.yaml": enforced}))
        assert errs == ["ERROR: _routing_enforced: group_by[1] is int 8, not a string — "
                        "quote it in YAML (e.g. \"8\", \"on\") so it is a label name; "
                        "Alertmanager would group by a label named after its text"], errs

    def test_routing_enforced_per_tenant_judged_after_substitution(self, tmp_path):
        """F1（第 2 輪）：`{{tenant}}` 逐租戶的 enforced route 判代換後的列表；
        代換後才重複（租戶 id 恰為 `alertname`）也要報，strict 不比 `--validate` 寬。"""
        enforced = ("_routing_enforced:\n  enabled: true\n  receiver:\n"
                    "    type: webhook\n    url: https://noc.example.com/x\n"
                    "  group_by: [alertname, \"{{tenant}}\"]\n")
        d = _tree(tmp_path, _MAIN, {"_routing_enforced.yaml": enforced,
                                    "alertname.yaml": "tenants:\n  alertname:\n"
                                    "    cpu_usage_percent: '85'\n    _routing:\n" + _MAIN})
        errs = _strict_errors(d)
        assert errs == ["ERROR: _routing_enforced (alertname): group_by[1] repeats label "
                        "'alertname' listed earlier — Alertmanager refuses a repeated "
                        "non-wildcard group_by label; remove it"], errs
        r = _gar(d, "--validate", "--strict")
        assert r.returncode == 1, r.stdout + r.stderr
        assert "ERROR: _routing_enforced (alertname): group_by[1]" in r.stderr

    @pytest.mark.parametrize("receiver", [
        "",                                                     # no receiver: no route
        "  receiver:\n    type: bogus\n",                       # receiver refused: no route
    ])
    def test_routing_enforced_not_rendered_is_not_judged(self, tmp_path, receiver):
        """F4（第 2 輪）：產生器不輸出的 enforced route，其 group_by 不判。"""
        enforced = ("_routing_enforced:\n  enabled: true\n" + receiver
                    + "  group_by: [alertname, 8]\n")
        d = _tree(tmp_path, _MAIN, {"_routing_enforced.yaml": enforced})
        assert _strict_errors(d) == []

    def test_quoted_is_clean(self, tmp_path):
        d = _tree(tmp_path, _MAIN + "      group_by: [alertname, \"on\", \"8\"]\n")
        assert _strict_errors(d) == []

    @pytest.mark.parametrize("mode", [["--dry-run", "--strict"], ["--validate", "--strict"]])
    def test_cli_strict_rc1(self, tmp_path, mode):
        d = _tree(tmp_path, _MAIN + "      group_by: [alertname, on, 8]\n")
        r = _gar(d, *mode)
        assert r.returncode == 1, r.stdout + r.stderr
        assert f"ERROR: tenant '{_T}': group_by[1] is bool True, not a string" in r.stderr
        assert "DRY RUN OUTPUT" not in r.stdout


class TestNonStrictRepair:
    """非 strict：WARN … skipping，輸出修正後的 group_by；五個位置同一套。"""

    @pytest.mark.parametrize("written, rendered", [
        ("[alertname, on, 8]", ["alertname"]),
        ("[alertname, true, true]", ["alertname"]),
        ("[alertname, \"...\"]", ["alertname"]),
        ("[alertname, alertname, tenant]", ["alertname", "tenant"]),
        ("[\"...\", \"...\"]", ["...", "..."]),   # F2: AM accepts it, rendered as written
        ("[on, 8]", None),        # emptied: no group_by key at all
        ("[\"\"]", None),
        ("[alertname, \"8\"]", ["alertname", "8"]),
    ])
    def test_tenant_main(self, tmp_path, written, rendered):
        d = _tree(tmp_path, _MAIN + f"      group_by: {written}\n")
        routes, warnings = _render(d)
        assert _tenant_route(routes).get("group_by") == rendered
        gb_warns = [w for w in warnings if "group_by[" in w]
        clean = written in ("[alertname, \"8\"]", "[\"...\", \"...\"]")
        assert bool(gb_warns) == (not clean), warnings
        assert gb_warns == blocking_generation_errors(gb_warns), gb_warns  # WARN … skipping
        assert not any("ERROR" in w for w in warnings), warnings

    def test_override_and_routes(self, tmp_path):
        routing = (_MAIN + "      group_by: [tenant]\n"
                   "      overrides:\n      - alertname: X\n"
                   "        group_by: [alertname, on]\n" + _WEBHOOK
                   + "      - alertname: Y\n        group_by: [8]\n" + _WEBHOOK
                   + "      routes:\n      - match: {team: db}\n"
                   "        group_by: [alertname, \"...\", alertname]\n" + _WEBHOOK)
        routes, warnings = _render(_tree(tmp_path, routing))
        children = _tenant_route(routes)["routes"]
        assert children[0]["group_by"] == ["alertname"]
        assert "group_by" not in children[1]   # emptied → inherits the tenant's [tenant]
        assert children[2]["group_by"] == ["alertname"]
        assert sorted(w.split(": ", 3)[2] for w in warnings if "group_by[" in w) == [
            "override[0]", "override[1]", "routes[0]", "routes[0]"], warnings

    @pytest.mark.parametrize("receiver_url, matcher", [
        ("https://noc.example.com/x", None),
        ("https://noc.example.com/{{tenant}}", f'tenant="{_T}"'),
    ])
    def test_routing_enforced(self, tmp_path, receiver_url, matcher):
        enforced = ("_routing_enforced:\n  enabled: true\n  receiver:\n"
                    f"    type: webhook\n    url: \"{receiver_url}\"\n"
                    "  group_by: [alertname, 8, alertname]\n")
        d = _tree(tmp_path, _MAIN, {"_routing_enforced.yaml": enforced})
        routes, warnings = _render(d)
        enforced_routes = [r for r in routes if r.get("continue") is True
                           and str(r.get("receiver", "")).startswith("platform-enforced")]
        assert len(enforced_routes) == 1, routes
        assert enforced_routes[0]["group_by"] == ["alertname"]
        if matcher:
            assert enforced_routes[0]["matchers"][0] == matcher
        assert len([w for w in warnings if "_routing_enforced" in w and "group_by[" in w]) == 2, warnings

    def test_enforce_group_by_judges_the_rendered_list(self, tmp_path):
        """`enforce_group_by` 判的是修正後輸出的列表；不可雜湊的元素（`{a: 1}`）
        修前在 `set()` 這裡 TypeError、rc 1。"""
        policy = ("domain_policies:\n  d:\n    tenants: [" + _T + "]\n"
                  "    constraints:\n      enforce_group_by: [tenant]\n")
        d = _tree(tmp_path, _MAIN + "      group_by: [alertname, {a: 1}, tenant]\n",
                  {"_domain_policy.yaml": policy})
        r = _gar(d, "--dry-run")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "missing required labels" not in r.stderr
        assert f"WARN: {_T}: group_by[1] is dict {{'a': 1}}, not a string" in r.stderr

    def test_cli_dry_run_rc0_and_output(self, tmp_path):
        d = _tree(tmp_path, _MAIN + "      group_by: [alertname, on, 8]\n")
        r = _gar(d, "--dry-run")
        assert r.returncode == 0, r.stdout + r.stderr
        assert (f"WARN: {_T}: group_by[1] is bool True, not a string — quote it in "
                "YAML") in r.stderr
        assert "ERROR" not in r.stderr
        body = r.stdout.split("--- DRY RUN OUTPUT ---\n", 1)[1].split("\n\n--- ", 1)[0]
        doc = yaml.safe_load(body)
        assert _tenant_route(doc["route"]["routes"])["group_by"] == ["alertname"], body
