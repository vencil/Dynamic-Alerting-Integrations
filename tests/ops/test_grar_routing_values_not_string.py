"""#2431：matcher 值（`routes[i].match.<label>`、`overrides[i].alertname` /
`metric_group`）PyYAML 讀出來不是字串 → `--strict` 報 ERROR（blocking）。

釘住的契約：

* 述詞只有一個：`_grar_validate.routing_values_not_string`，判的是**解析後**
  的 routing（租戶檔、根目錄 overlay、`_routing_defaults`、routing profile
  來的值都算）；Go 側同一述詞是 `routingpolicy.ValuesNotString`，兩邊由
  `tests/shared/routing_policy_parity_matrix.json` 的 `values_not_string`
  欄對齊。
* `--strict`（`--validate` 與 render 兩條路）：每個值一行 `ERROR:`，rc 1，
  訊息要求加引號。
* 非 strict 行為不變：routes 照舊 `WARN … skipping`、overrides 照舊以
  truthiness 判斷、以 `str()` 渲染（`alertname: yes` → `alertname="True"`）。
* 加了引號的值（`"yes"`、`"1:30"`）不報。

Fixture 的 tenant 名一律是 demo 名（tenant-agnostic）。
"""
from __future__ import annotations

import datetime
import subprocess
import sys
from pathlib import Path

import pytest

from _grar_validate import (
    POLICY_ERROR_PREFIX,
    route_entry_matchers,
    routing_values_not_string,
    value_not_string_message,
)
from generate_alertmanager_routes import load_tenant_tree

REPO = Path(__file__).resolve().parents[2]
_GAR = REPO / "scripts" / "tools" / "ops" / "generate_alertmanager_routes.py"
_T = "demo-apac"

_MAIN = ("      receiver:\n        type: pagerduty\n"
         "        service_key: main-key\n")


def _route(value: str) -> str:
    return (_MAIN + "      routes:\n      - match:\n"
            f"          team: {value}\n"
            "        receiver:\n          type: slack\n"
            "          api_url: https://hooks.slack.com/services/T/B/x\n")


def _override(line: str) -> str:
    return (_MAIN + f"      overrides:\n      - {line}\n"
            "        receiver:\n          type: webhook\n"
            "          url: https://hooks.example.com/ov\n")


def _tree(tmp_path: Path, routing: str | None = None,
          extra: dict[str, str] | None = None, profile: str | None = None) -> Path:
    d = tmp_path / "conf.d"
    d.mkdir()
    (d / "_defaults.yaml").write_text("defaults:\n  cpu_usage_percent: 80\n",
                                      encoding="utf-8")
    body = f"tenants:\n  {_T}:\n    cpu_usage_percent: '85'\n"
    if profile:
        body += f"    _routing_profile: {profile}\n"
    if routing:
        body += "    _routing:\n" + routing
    (d / f"{_T}.yaml").write_text(body, encoding="utf-8")
    for name, text in (extra or {}).items():
        (d / name).write_text(text, encoding="utf-8")
    return d


def _gar(d: Path, *flags: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_GAR), "--config-dir", str(d), *flags],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300)


def _errors(d: Path, strict: bool) -> list[str]:
    tree = load_tenant_tree(str(d), strict_policies=strict)
    return [w for w in tree.schema_warnings if "must be a string, got" in w
            and w.lstrip().startswith(POLICY_ERROR_PREFIX)]


class TestPredicate:
    def test_fields_in_render_order(self):
        rc = {"overrides": [{"alertname": True}, {"metric_group": "db"},
                            {"alertname": None, "metric_group": 90}, "not-a-dict"],
              "routes": [{"match": {"team": "yes", "zone": datetime.date(2001, 12, 15)}},
                         {"match": "not-a-mapping"}, {"match": {"b": 5, "a": None}}]}
        assert [f for f, _ in routing_values_not_string(rc)] == [
            "overrides[0].alertname", "overrides[2].alertname",
            "overrides[2].metric_group", "routes[0].match.zone",
            "routes[2].match.b", "routes[2].match.a"]

    def test_key_that_is_no_label_name_is_not_judged(self):
        """F1：key 不是合法 label 名稱時，route_entry_matchers 已略過整條，
        值不再判（`2001-12-15: yes` 也不報）。"""
        rc = {"routes": [{"match": {"2001-12-15": True, ".inf": "x", "1": None,
                                    "team\n": True, "team": 90}}]}
        assert [f for f, _ in routing_values_not_string(rc)] == ["routes[0].match.team"]

    def test_trailing_newline_is_no_label_name(self):
        """F2：`$` 會吃結尾 `\\n`；label 檢查用 fullmatch，與 Go labelNameRE 一致。"""
        matchers, warns = route_entry_matchers(
            {"match": {"team\n": "x"}, "receiver": {"type": "webhook"}}, 0, _T)
        assert matchers is None and "is not a valid label name" in warns[0], warns

    @pytest.mark.parametrize("rc", [None, "disable", {}, {"overrides": "x", "routes": 1},
                                    {"overrides": [{"alertname": ""}],
                                     "routes": [{"match": {"team": ""}}]}])
    def test_nothing_to_report(self, rc):
        assert routing_values_not_string(rc) == []

    def test_message_asks_for_quotes(self):
        msg = value_not_string_message(_T, "routes[0].match.team", True)
        assert msg == (f"tenant '{_T}': routes[0].match.team must be a string, "
                       "got bool True — quote it in YAML (e.g. team: \"...\") "
                       "so the route generator reads it as text")


class TestStrictErrorsPerLayer:
    """每一層寫進來的值，解析後都被判；非 strict 不產 ERROR 行。"""

    @pytest.mark.parametrize("value, got", [
        ("yes", "got bool True"), ("on", "got bool True"), ("1:30", "got int 90"),
        ("2001-12-15", "got date datetime.date(2001, 12, 15)"),
        ("~", "got NoneType None"), ("!!int 5", "got int 5")])
    def test_tenant_file_match_value(self, tmp_path, value, got):
        d = _tree(tmp_path, _route(value))
        errs = _errors(d, strict=True)
        assert len(errs) == 1 and "routes[0].match.team must be a string, " + got in errs[0], errs
        assert _errors(d, strict=False) == []

    def test_quoted_match_value_is_clean(self, tmp_path):
        assert _errors(_tree(tmp_path, _route('"yes"')), strict=True) == []

    def test_platform_overlay(self, tmp_path):
        overlay = "tenants:\n  " + _T + ":\n    _routing:\n" + _override("alertname: yes")
        d = _tree(tmp_path, extra={"_platform.yaml": overlay})
        assert [e.split(": ", 2)[-1].split(" must")[0] for e in _errors(d, strict=True)] \
            == ["overrides[0].alertname"]

    def test_routing_defaults(self, tmp_path):
        defaults = "_routing_defaults:\n" + "".join(
            line[4:] + "\n" for line in _override("metric_group: 1:30").splitlines())
        d = _tree(tmp_path, extra={"_routing_defaults.yaml": defaults})
        errs = _errors(d, strict=True)
        assert len(errs) == 1 and "overrides[0].metric_group must be a string, got int 90" in errs[0]

    def test_routing_profile(self, tmp_path):
        profiles = "routing_profiles:\n  p:\n" + "".join(
            line[2:] + "\n" for line in _route("~").splitlines())
        d = _tree(tmp_path, extra={"_routing_profiles.yaml": profiles}, profile="p")
        errs = _errors(d, strict=True)
        assert len(errs) == 1 and "routes[0].match.team must be a string, got NoneType None" in errs[0]

    def test_tenant_override_alertname_no(self, tmp_path):
        errs = _errors(_tree(tmp_path, _override("alertname: no")), strict=True)
        assert len(errs) == 1 and "overrides[0].alertname must be a string, got bool False" in errs[0]


class TestCli:
    """`--strict` 下 blocking（rc 1）；不加 `--strict` 行為不變。"""

    def test_validate_strict_fails_on_override_yes(self, tmp_path):
        d = _tree(tmp_path, _override("alertname: yes"))
        r = _gar(d, "--validate", "--strict")
        assert r.returncode == 1, r.stdout + r.stderr
        assert (f"ERROR: tenant '{_T}': overrides[0].alertname must be a string, "
                "got bool True — quote it in YAML") in r.stderr

    def test_render_strict_refuses_before_writing(self, tmp_path):
        d = _tree(tmp_path, _route("1:30"))
        r = _gar(d, "--dry-run", "--strict")
        assert r.returncode == 1, r.stdout + r.stderr
        assert "blocking error(s) under --strict" in r.stderr
        assert "DRY RUN OUTPUT" not in r.stdout

    def test_non_strict_keeps_the_legacy_rendering(self, tmp_path):
        d = _tree(tmp_path, _override("alertname: yes"))
        r = _gar(d, "--dry-run")
        assert r.returncode == 0, r.stdout + r.stderr
        assert 'alertname="True"' in r.stdout
        assert "must be a string" not in r.stderr

    def test_non_strict_route_value_still_warns_and_skips(self, tmp_path):
        d = _tree(tmp_path, _route("yes"))
        r = _gar(d, "--dry-run")
        assert r.returncode == 0, r.stdout + r.stderr
        assert ("WARN: demo-apac: routes[0]: match value for 'team' must be a "
                "string, got bool True (quote it in YAML), skipping") in r.stderr
        assert "ERROR:" not in r.stderr

    def test_quoted_values_pass_strict(self, tmp_path):
        d = _tree(tmp_path, _override('alertname: "yes"'))
        r = _gar(d, "--validate", "--strict")
        assert r.returncode in (0, 2), r.stdout + r.stderr  # 2 only if amtool is unusable
        assert "must be a string" not in r.stderr
