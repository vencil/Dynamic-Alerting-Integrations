"""#2504：routing `match` 裡的 null key（`~:` / `null:` / 空 key）不再被靜默丟掉。

缺陷：`_lib_yaml_keys.ExporterKeyLoader` 為了與 Go exporter 一致，在**每一層**
mapping 都丟 null key。於是 `routes[i].match: {~: x, severity: critical}` 在
產生器眼中只剩 `severity: critical`——route 變寬（fail-open），而 da-guard 對
同一份檔案報 `invalid_route_entry`。

釘住的契約：

* 只有 routing 讀取路徑（`_grar_parse.keep_route_match_null_keys`），而且只在
  產生器讀 route match 的**結構路徑**上（`tenants.<id>._routing.routes[i].match`、
  `routing_profiles.<name>.routes[i].match`）把 null key 放回（讀成 `None`），
  走與 `match: {"a-b": x}` 同一條「不是合法 label name」判定，整條 route 無效
  並 WARN；只有一個 null key 時報的也是這句，而不是誤導的
  「needs a non-empty 'match'」。
* 判定看位置、不看 key 文字：叫 `match` 的 tenant、`_metadata.match` 照舊丟
  null key（第 1 輪以 key 文字判定，tenant 叫 `match` 時整支工具 TypeError）。
* 不改動被共用的物件：`match: *t` 引用某 tenant body 時，route 拿到的是新 dict，
  anchor 那一端照舊看不到 null key。
* 經 routing profile 繼承進來的 routes、以 `<<:` 合併進 match 的 null key
  走同一條判定。
* 無效的 route 不列入 `list_tenant_subroutes`，所以 `require_critical_escalation`
  不會把它當成合規的 PagerDuty route。
* 共用 loader 本身不變：所有讀者在每一層照舊丟 null key。

Fixture 的 tenant 名一律是 demo 名（tenant-agnostic）。
"""
from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

import pytest

from generate_alertmanager_routes import check_domain_policies, load_tenant_tree
from _grar_validate import list_tenant_subroutes, route_entry_matchers
from _grar_parse import _parse_config_files
from _lib_confd import declared_tenant_ids
from _lib_io import strict_load_exporter_keys
from _lib_yaml_keys import load_exporter_keys

REPO = Path(__file__).resolve().parents[2]
_GAR = REPO / "scripts" / "tools" / "ops" / "generate_alertmanager_routes.py"
_VC = REPO / "scripts" / "tools" / "ops" / "validate_config.py"

_T = "demo-apac"
_INVALID = "is not a valid label name, skipping"
_EMPTY = "needs a non-empty 'match'"

_TENANT_FILE = """\
tenants:
  {tenant}:
    _routing:
      receiver:
        type: webhook
        url: https://hooks.example.com/main
      routes:
      - match:
{match}
        receiver:
          type: pagerduty
          service_key: k
"""


def _tree(tmp_path: Path, match_lines: str, extra: dict | None = None) -> Path:
    d = tmp_path / "conf.d"
    d.mkdir(parents=True)
    (d / f"{_T}.yaml").write_text(
        _TENANT_FILE.format(tenant=_T, match=match_lines), encoding="utf-8")
    for name, text in (extra or {}).items():
        (d / name).write_text(text, encoding="utf-8")
    return d


def _gar(d: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_GAR), "--config-dir", str(d), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300)


def _routing(d: Path) -> dict:
    return load_tenant_tree(str(d)).as_tuple()[0]


# Every spelling PyYAML resolves to a null key.
_NULL_KEYS = ["~", "null", "Null", "NULL", '!!null ""', "? "]


def _match(key: str, *more: str) -> str:
    pad = " " * 10
    first = f"{pad}{key}: x" if key != "? " else f"{pad}? \n{pad}: x"
    return "\n".join([first, *(f"{pad}{m}" for m in more)])


# ============================================================
# 產生器：null key 讓整條 route 無效，與 `a-b` 走同一條路徑
# ============================================================
class TestGeneratorRejectsNullKey:

    @pytest.mark.parametrize("key", _NULL_KEYS)
    def test_null_key_beside_a_label_invalidates_the_route(self, tmp_path, key):
        d = _tree(tmp_path, _match(key, "severity: critical"))
        proc = _gar(d, "--dry-run", "--strict")
        out = proc.stdout + proc.stderr
        assert f"{_T}: routes[0]: match label" in out and _INVALID in out, out
        assert f"tenant-{_T}-route-0" not in out, out
        assert 'severity="critical"' not in out.split("inhibit_rules:")[0], out

    @pytest.mark.parametrize("key", _NULL_KEYS)
    def test_null_key_alone_reports_the_label_not_an_empty_match(
            self, tmp_path, key):
        d = _tree(tmp_path, _match(key))
        proc = _gar(d, "--dry-run", "--strict")
        out = proc.stdout + proc.stderr
        assert _INVALID in out and _EMPTY not in out, out
        assert f"tenant-{_T}-route-0" not in out, out

    def test_same_outcome_as_an_invalid_label_name(self, tmp_path):
        """與 `a-b` 同一條路徑：同樣的 rc、同樣被略過，只差 label 的寫法。"""
        null_dir = _tree(tmp_path / "n", _match("~", "severity: critical"))
        dash_dir = _tree(tmp_path / "a", _match("a-b", "severity: critical"))
        for mode in (("--dry-run", "--strict"), ("--validate",)):
            null_p, dash_p = _gar(null_dir, *mode), _gar(dash_dir, *mode)
            assert null_p.returncode == dash_p.returncode, (mode, null_p, dash_p)
            null_w = [ln for ln in (null_p.stdout + null_p.stderr).splitlines()
                      if _INVALID in ln]
            dash_w = [ln for ln in (dash_p.stdout + dash_p.stderr).splitlines()
                      if _INVALID in ln]
            assert dash_w, (mode, dash_p)
            assert [w.replace("None", "'a-b'") for w in null_w] == dash_w

    def test_warn_names_the_null_key(self, tmp_path):
        d = _tree(tmp_path, _match("~", "severity: critical"))
        routing = _routing(d)
        entry = routing[_T]["routes"][0]
        assert None in entry["match"]
        assert route_entry_matchers(entry, 0, _T) == (None, [
            f"  WARN: {_T}: routes[0]: match label None is not a valid label "
            "name, skipping"])

    def test_profile_route_with_a_null_key_is_rejected(self, tmp_path):
        d = tmp_path / "conf.d"
        d.mkdir()
        (d / "_routing_profiles.yaml").write_text(
            "routing_profiles:\n"
            "  p-null:\n"
            "    receiver:\n"
            "      type: webhook\n"
            "      url: https://hooks.example.com/main\n"
            "    routes:\n"
            "    - match:\n"
            "        ~: x\n"
            "        severity: critical\n"
            "      receiver:\n"
            "        type: pagerduty\n"
            "        service_key: k\n", encoding="utf-8")
        (d / f"{_T}.yaml").write_text(
            f"tenants:\n  {_T}:\n    _routing_profile: p-null\n",
            encoding="utf-8")
        routing = _routing(d)
        assert list_tenant_subroutes(routing[_T]) == []
        out = _gar(d, "--dry-run", "--strict").stdout
        assert f"tenant-{_T}-route-0" not in out, out

    def test_valid_route_is_unchanged(self, tmp_path):
        """反證：沒有 null key 的 route 照舊產出。"""
        d = _tree(tmp_path, _match("team", "severity: critical")
                  .replace("team: x", "team: app"))
        out = _gar(d, "--dry-run", "--strict").stdout
        assert f"tenant-{_T}-route-0" in out
        assert 'severity="critical"' in out and 'team="app"' in out


# ============================================================
# require_critical_escalation：被略過的 route 不算合規
# ============================================================
class TestEscalationIgnoresTheSkippedRoute:

    _POLICY = {"finance": {"tenants": [_T], "constraints": {
        "require_critical_escalation": True}}}

    def test_only_pd_route_has_a_null_key_is_a_violation(self, tmp_path):
        d = _tree(tmp_path, _match("~", "severity: critical"))
        msgs = check_domain_policies(_routing(d), self._POLICY, strict=True)
        assert any("require_critical_escalation is set but" in m
                   and m.lstrip().startswith("ERROR:") for m in msgs), msgs

    def test_control_without_the_null_key_is_compliant(self, tmp_path):
        d = _tree(tmp_path, "          severity: critical")
        assert check_domain_policies(_routing(d), self._POLICY,
                                     strict=True) == []


# ============================================================
# 判定看結構路徑、不改共用物件（第 2 輪 F1 / F2）
# ============================================================
def _vc(d: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_VC), "--config-dir", str(d)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300)


_MODES = (("--dry-run",), ("--dry-run", "--strict"), ("--validate",))

# F1: a tenant whose id is `match` — its body is no route match.
_F1 = """\
tenants:
  match:
    ~: '5'
    _routing:
      receiver: {type: webhook, url: https://hooks.example.com/main}
"""

# F2: a route `match` that aliases another tenant's body.
_F2 = """\
tenants:
  demo-a: &t
    ~: '1'
    cpu: '1'
  demo-b:
    _routing:
      receiver: {type: webhook, url: https://hooks.example.com/main}
      routes:
      - match: *t
        receiver: {type: pagerduty, service_key: k}
"""


def _write_tree(tmp_path: Path, files: dict[str, str]) -> Path:
    d = tmp_path / "conf.d"
    for name, text in files.items():
        (d / name).parent.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(text, encoding="utf-8")
    return d


class TestStructuralPathNotKeyText:

    @pytest.mark.parametrize("fname", ["match.yaml", "team/match.yaml"])
    def test_tenant_named_match_reads_as_before(self, tmp_path, fname):
        d = _write_tree(tmp_path, {fname: _F1})
        for mode in _MODES:
            proc = _gar(d, *mode)
            assert proc.returncode == 0, (mode, proc.stdout, proc.stderr)
            assert "Traceback" not in proc.stderr, proc.stderr
        proc = _vc(d)
        assert proc.returncode == 0 and "Traceback" not in proc.stderr, (
            proc.stdout, proc.stderr)
        routing = _routing(d)
        assert None not in routing["match"]

    def test_alias_into_match_does_not_leak_into_the_anchor(self, tmp_path):
        d = _write_tree(tmp_path, {"t.yaml": _F2})
        for mode in _MODES[:2]:
            proc = _gar(d, *mode)
            out = proc.stdout + proc.stderr
            assert proc.returncode == 0 and "Traceback" not in out, (mode, out)
            assert ("WARN: demo-b: routes[0]: match label None is not a valid "
                    "label name, skipping") in out, out
            assert "tenant-demo-b-route-0" not in out, out
            assert "demo-a: unknown key" in out and "None" not in out.replace(
                "match label None", ""), out

    def test_alias_into_match_rc_matches_an_invalid_label(self, tmp_path):
        """`--validate` / validate-config：與 `a-b` 對照組同 rc（被略過的
        route 本來就是阻擋項），且沒有 traceback。"""
        null_d = _write_tree(tmp_path / "n", {"t.yaml": _F2})
        dash_d = _write_tree(tmp_path / "a", {"t.yaml": _F2.replace(
            "    ~: '1'\n", "    a-b: '1'\n")})
        for run in (lambda d: _gar(d, "--validate"), _vc):
            n, a = run(null_d), run(dash_d)
            assert n.returncode == a.returncode, (n.stdout, a.stdout)
            assert "Traceback" not in n.stderr + n.stdout

    def test_anchor_object_is_not_mutated(self, tmp_path):
        from _grar_parse import keep_route_match_null_keys
        d = _write_tree(tmp_path, {"t.yaml": _F2})
        data = strict_load_exporter_keys(io.StringIO(_F2),
                                         raw_text_sequences=("tenants",))
        before = repr(data)
        out = keep_route_match_null_keys(data, str(d / "t.yaml"))
        assert repr(data) == before  # the normal read is untouched
        assert out["tenants"]["demo-a"] == {"cpu": "1"}
        assert out["tenants"]["demo-b"]["_routing"]["routes"][0]["match"] == {
            "cpu": "1", None: ""}  # the probe's placeholder, never built
        assert (out["tenants"]["demo-b"]["_routing"]["routes"][0]["match"]
                is not out["tenants"]["demo-a"])

    def test_metadata_match_still_drops_the_null_key(self, tmp_path):
        d = _write_tree(tmp_path, {f"{_T}.yaml": (
            f"tenants:\n  {_T}:\n    _metadata:\n      match:\n"
            "        ~: x\n        owner: sre\n"
            "    _routing:\n      receiver: {type: webhook, "
            "url: https://hooks.example.com/main}\n"
            "      routes:\n      - match: {severity: critical}\n"
            "        receiver: {type: pagerduty, service_key: k}\n")})
        parsed = _parse_config_files(str(d))
        assert parsed["metadata_configs"][_T] == {"match": {"owner": "sre"}}
        out = _gar(d, "--dry-run", "--strict").stdout
        assert f"tenant-{_T}-route-0" in out

    def test_merge_key_into_match_brings_the_null_key(self, tmp_path):
        d = _write_tree(tmp_path, {f"{_T}.yaml": (
            "base: &m {~: x}\n"
            f"tenants:\n  {_T}:\n"
            "    _metadata: *m\n"
            "    _routing:\n      receiver: {type: webhook, "
            "url: https://hooks.example.com/main}\n"
            "      routes:\n      - match: {<<: *m, severity: critical}\n"
            "        receiver: {type: pagerduty, service_key: k}\n")})
        parsed = _parse_config_files(str(d))
        assert None not in parsed["metadata_configs"].get(_T, {})
        out = _gar(d, "--dry-run", "--strict")
        text = out.stdout + out.stderr
        assert "match label None is not a valid label name" in text, text
        assert f"tenant-{_T}-route-0" not in text


# ============================================================
# 探測讀取的失敗面不大於主讀取（第 3 輪 F3）
# ============================================================
# 主讀取遇 null key 直接略過、不建它的值；探測讀取也不可以建，否則一個
# SafeLoader 建不出來的值（壞 timestamp、`=`、`!!int x`、自訂 tag、python
# 物件）會讓探測整檔失敗、靜默退回，route 照樣被放寬。
_F3_FILE = """\
tenants:
  {tenant}:
{metadata}    _routing:
      receiver:
        type: webhook
        url: https://hooks.example.com/main
      routes:
      - match:
          {first}
          severity: critical
        receiver:
          type: pagerduty
          service_key: k
"""

_F3_CASES = {
    # the null key's own value is not constructible
    "p1-bad-timestamp": ("~: 2024-13-45", ""),
    "p2-value-tag": ("~: =", ""),
    "p3-bad-int-tag": ("~: !!int x", ""),
    "p4-custom-tag": ("~: !vault x", ""),
    "p9-python-object": ("~: !!python/object/apply:os.getcwd []", ""),
    # the match is plain; a null key ELSEWHERE in the file has such a value
    "p5-metadata-bad-timestamp": ("~: x", "    _metadata: {~: 2024-13-45}\n"),
    "p6-metadata-custom-tag": ("~: x", "    _metadata: {owner: a, ~: !secret x}\n"),
}


def _f3_tree(tmp_path: Path, first: str, metadata: str) -> Path:
    d = tmp_path / "conf.d"
    d.mkdir(parents=True)
    (d / f"{_T}.yaml").write_text(_F3_FILE.format(
        tenant=_T, metadata=metadata, first=first), encoding="utf-8")
    return d


class TestProbeFailsNoMoreThanTheMainRead:

    @pytest.mark.parametrize("case", sorted(_F3_CASES))
    def test_route_is_skipped_and_rc_matches_an_invalid_label(
            self, tmp_path, case):
        first, metadata = _F3_CASES[case]
        d = _f3_tree(tmp_path / "n", first, metadata)
        dash = _f3_tree(tmp_path / "a", "a-b: x", metadata)
        proc = _gar(d, "--dry-run")
        out = proc.stdout + proc.stderr
        assert "Traceback" not in out, out
        assert (f"WARN: {_T}: routes[0]: match label None is not a valid "
                "label name, skipping") in out, out
        assert f"tenant-{_T}-route-0" not in out, out
        n, a = _gar(d, "--validate", "--strict"), _gar(dash, "--validate",
                                                       "--strict")
        assert n.returncode == a.returncode != 0, (n.stdout, n.stderr,
                                                   a.stdout, a.stderr)

    @pytest.mark.parametrize("case", sorted(_F3_CASES))
    def test_probe_succeeds_where_the_main_read_succeeds(self, tmp_path, case):
        """`except` 退回在這些輸入上不觸發：主讀取成功時，探測讀取也成功。"""
        from _grar_parse import _probe_null_keys
        first, metadata = _F3_CASES[case]
        d = _f3_tree(tmp_path, first, metadata)
        path = d / f"{_T}.yaml"
        with open(path, encoding="utf-8") as fh:
            assert isinstance(strict_load_exporter_keys(
                fh, raw_text_sequences=("tenants",)), dict)
        assert isinstance(_probe_null_keys(str(path)), dict)

    def test_python_object_under_a_null_key_is_never_built(self, tmp_path):
        """主讀取與探測讀取都不建它：有副作用的 payload 也沒有執行。"""
        marker = tmp_path / "executed"
        d = _f3_tree(tmp_path, f"~: !!python/object/apply:os.mkdir "
                               f"['{marker.as_posix()}']", "")
        routing = _routing(d)
        assert not marker.exists()
        assert list_tenant_subroutes(routing[_T]) == []

    def test_strict_messages_unchanged_for_the_plain_case(self, tmp_path):
        """`{~: x, severity: critical}`：除了 route 被略過的那行 WARN，
        `--validate --strict` 沒有多報或少報（與 `a-b` 對照組逐行相同，只差
        label 的寫法）。"""
        n = _gar(_f3_tree(tmp_path / "n", "~: x", ""), "--validate", "--strict")
        a = _gar(_f3_tree(tmp_path / "a", "a-b: x", ""), "--validate",
                 "--strict")
        assert (n.stdout + n.stderr).replace("None", "'a-b'") == (
            a.stdout + a.stderr).replace(str(tmp_path / "a"),
                                         str(tmp_path / "n"))


# ============================================================
# Loader：共用 loader 本身不變
# ============================================================
_DOC = """\
tenants:
  ~:
    cpu_usage_percent: '1'
  demo-apac:
    _routing:
      routes:
      - match:
          ~: x
          severity: critical
    labels:
      ~: dropped
"""


class TestLoaderScope:

    def test_shared_loader_still_drops_every_null_key(self):
        expected = {"tenants": {"demo-apac": {
            "_routing": {"routes": [{"match": {"severity": "critical"}}]},
            "labels": {}}}}
        assert load_exporter_keys(io.StringIO(_DOC)) == expected
        assert strict_load_exporter_keys(io.StringIO(_DOC)) == expected

    def test_routing_reader_tenant_level_null_key_still_dropped(self, tmp_path):
        """tenants 層的 `~:` 仍被丟掉：產生器看到的 tenant 集合不變。"""
        d = _tree(tmp_path, "          severity: critical")
        (d / "_platform.yaml").write_text(
            "tenants:\n  ~:\n    _routing:\n      receiver:\n"
            "        type: webhook\n        url: https://hooks.example.com/x\n",
            encoding="utf-8")
        assert sorted(_routing(d)) == [_T]

    def test_existing_reader_tenant_level_null_key_unchanged(self, tmp_path):
        """既有讀者（`_lib_confd.declared_tenant_ids`）照舊丟掉 tenants 層的
        null key，也不受 routing 讀取路徑影響。"""
        d = tmp_path / "conf.d"
        d.mkdir()
        (d / f"{_T}.yaml").write_text(
            f"tenants:\n  ~:\n    cpu_usage_percent: '1'\n  {_T}:\n"
            "    _routing:\n      routes:\n      - match: {~: x}\n",
            encoding="utf-8")
        _routing(d)  # the routing reader runs first in the same process
        assert declared_tenant_ids(d) == {_T}

    def test_null_key_probe_loader_refuses_python_objects(self, tmp_path):
        """第二次讀取用的 loader 仍是 SafeLoader：python 物件 payload 被拒。"""
        import yaml
        from _grar_parse import _NullKeyAsTextLoader
        f = tmp_path / "x.yaml"
        f.write_text("a: !!python/object/apply:os.system ['true']\n",
                     encoding="utf-8")
        with open(f, encoding="utf-8") as fh:
            loader = _NullKeyAsTextLoader(fh)
            try:
                with pytest.raises(yaml.constructor.ConstructorError):
                    loader.get_single_data()
            finally:
                loader.dispose()
