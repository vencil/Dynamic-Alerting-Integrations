"""「值屬於哪個檔」語意矩陣：threshold-recommend／threshold-govern（#2116）。

同一個值（tenant-a 的 `mysql_connections` = 60）依序放在：根 `defaults:`、平台檔
`tenants:`、租戶檔、子樹 `_defaults.yaml`、profile；外加三個形狀——租戶檔寫舊拼法
`mysql_cpu`（ALIAS）、租戶檔在子目錄（NESTED）、同一租戶一個自有一個繼承（MIXED）。
tenant-b 是對照組：永遠在根目錄自己的檔寫 `mysql_connections: "60"`，每格都要開 PR。

歸屬以 `da-guard effective` 的 key_sources 為準（layer == tenant 才是租戶自有）：測試的
期望值同時由 oracle 推出、也寫死，避免兩邊一起錯。

Prometheus 與 tenant-api 以 stub 取代：每條觀測序列回 2000 個 20.0；GET 只讀
`<conf.d>/<tenant>.yaml`（tenant-api 解析租戶檔只看根目錄），否則 404；PUT 回
200 pending_review。跑的是真的 `threshold_govern.run()` 迴圈。

da-guard 由 conftest 的 session fixture 以 `go build` 建出；建不起來就 fail、不 skip。
"""
from __future__ import annotations

import argparse
import collections
import urllib.parse
from pathlib import Path

import pytest
import yaml

import _lib_tenant_values as tv
import threshold_govern as tg
import threshold_recommend as tr

pytestmark = pytest.mark.usefixtures("da_guard_env")

# The root declares every key a tenant overrides: an undeclared key is not
# served at all (#1976), so a realistic tree declares it.
_BASE = "defaults:\n  mysql_connections: 80\n  mysql_threads_running: 80\n"
_B = 'tenants:\n  tenant-b:\n    mysql_connections: "60"\n'           # control
_A_PLAIN = 'tenants:\n  tenant-a:\n    foo_unmapped_key: "3"\n'
_A_OWN = 'tenants:\n  tenant-a:\n    mysql_connections: "60"\n'

# where -> (files, expected tenant-a owned keys, expected inherited
#           mysql_connections source (layer, file) or None, tenant-a outcome)
MATRIX = {
    "defaults": ({"_defaults.yaml": "defaults:\n  mysql_connections: 60\n  mysql_threads_running: 80\n",
                  "tenant-a.yaml": _A_PLAIN, "tenant-b.yaml": _B},
                 set(), ("defaults", "_defaults.yaml"), None),
    "platform-tenants": ({"_defaults.yaml": _BASE,
                          "_platform.yaml": "tenants:\n  tenant-a:\n    mysql_connections: 60\n",
                          "tenant-a.yaml": _A_PLAIN, "tenant-b.yaml": _B},
                         set(), ("platform", "_platform.yaml"), None),
    "tenant-file": ({"_defaults.yaml": _BASE, "tenant-a.yaml": _A_OWN, "tenant-b.yaml": _B},
                    {"mysql_connections"}, None, "pr_opened"),
    "subtree": ({"_defaults.yaml": _BASE,
                 "team/_defaults.yaml": "defaults:\n  mysql_connections: 60\n",
                 "team/tenant-a.yaml": _A_PLAIN, "tenant-b.yaml": _B},
                set(), ("defaults", "team/_defaults.yaml"), None),
    "profile": ({"_defaults.yaml": _BASE,
                 "_profiles.yaml": "profiles:\n  p1:\n    mysql_connections: 60\n",
                 "tenant-a.yaml": 'tenants:\n  tenant-a:\n    _profile: p1\n',
                 "tenant-b.yaml": _B},
                set(), ("profile", "_profiles.yaml"), None),
    "alias": ({"_defaults.yaml": _BASE,
               "tenant-a.yaml": 'tenants:\n  tenant-a:\n    mysql_cpu: "60"\n',
               "tenant-b.yaml": _B},
              {"mysql_cpu"}, ("defaults", "_defaults.yaml"), "pr_opened"),
    "nested": ({"_defaults.yaml": _BASE, "team/tenant-a.yaml": _A_OWN, "tenant-b.yaml": _B},
               {"mysql_connections"}, None, "skipped_nested"),
    "mixed": ({"_defaults.yaml": "defaults:\n  mysql_connections: 60\n  mysql_threads_running: 80\n",
               "tenant-a.yaml": 'tenants:\n  tenant-a:\n    mysql_threads_running: "60"\n',
               "tenant-b.yaml": _B},
              {"mysql_threads_running"}, ("defaults", "_defaults.yaml"), "pr_opened"),
}


def _tree(root: Path, files: dict[str, str]) -> Path:
    conf_d = root / "conf.d"
    for rel, body in files.items():
        p = conf_d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return conf_d


class _StubAPI:
    """tenant-api: GET reads `<conf_d>/<tenant>.yaml` at the top level only."""

    def __init__(self, conf_d: Path):
        self.conf_d = conf_d
        self.gets: list[str] = []
        self.puts: dict[str, str] = {}

    def get(self, url, timeout=None, headers=None):
        tenant = urllib.parse.unquote(url.rsplit("/", 1)[1])
        self.gets.append(tenant)
        f = self.conf_d / f"{tenant}.yaml"
        if not f.is_file():
            return None, "HTTP Error 404: Not Found"
        return {"raw_yaml": f.read_text(encoding="utf-8")}, None

    def put(self, url, body, headers, timeout):
        tenant = urllib.parse.unquote(url.rsplit("/", 1)[1])
        self.puts[tenant] = body
        return 200, {"status": "pending_review", "pr_url": f"https://git/pr/{len(self.puts)}",
                     "pr_number": len(self.puts)}, None


@pytest.fixture
def stubs(monkeypatch):
    monkeypatch.setattr(tr, "query_prometheus_range",
                        lambda url, promql, *a, **k: ([20.0] * 2000, None))
    monkeypatch.setattr(tg.time, "sleep", lambda s: None)

    def install(conf_d: Path) -> _StubAPI:
        api = _StubAPI(conf_d)
        monkeypatch.setattr(tg, "http_get_json", api.get)
        monkeypatch.setattr(tg, "_http_put_yaml", api.put)
        return api
    return install


def _args(conf_d: Path) -> argparse.Namespace:
    return argparse.Namespace(
        config_dir=str(conf_d), prometheus="http://prom.stub", tenant=None, lookback="7d",
        min_samples=100, min_delta_pct=25.0, max_prs=100, apply=True,
        tenant_api_url="http://tenant-api.stub", identity_email="g@x", identity_groups="g",
        auth_token=None, auth_token_file=None, throttle_seconds=0, timeout=5)


def _owned_by_oracle(conf_d: Path, tenant: str) -> set[str]:
    eff = tv.load_effective(conf_d)[tenant]
    return {k for k, ks in eff.key_sources.items()
            if ks.layer == "tenant" and not tr.is_reserved_key(k)}


@pytest.mark.parametrize("where", sorted(MATRIX))
def test_recommend_splits_own_from_inherited(where, tmp_path, stubs):
    files, own, inherited_src, _ = MATRIX[where]
    conf_d = _tree(tmp_path, files)
    reports = {r.tenant: r for r in tr.run_analysis(str(conf_d), prometheus_url="http://prom.stub")}
    a = reports["tenant-a"]
    filler = {"foo_unmapped_key"}  # tenant-own, no observed-map entry: listed, skipped
    assert {r.key for r in a.keys} - filler == own == _owned_by_oracle(conf_d, "tenant-a") - filler, where
    by_key = {r.key: r for r in a.inherited}
    if inherited_src is not None:
        r = by_key["mysql_connections"]
        assert (r.source_layer, r.source_file) == inherited_src, where
        # listed only: not queried, no recommended value (#2116)
        assert (r.recommended, r.promql) == (None, ""), where
    assert all(r.source_layer == "tenant" for r in a.keys), where
    # control
    b = reports["tenant-b"]
    assert [(r.key, r.source_layer, r.source_file) for r in b.keys] == [
        ("mysql_connections", "tenant", "tenant-b.yaml")], where


@pytest.mark.parametrize("where", sorted(MATRIX))
def test_govern_opens_prs_for_own_keys_only(where, tmp_path, stubs):
    files, own, _, a_outcome = MATRIX[where]
    conf_d = _tree(tmp_path, files)
    api = stubs(conf_d)
    res = tg.run(_args(conf_d))
    by_tenant = {o.tenant: o for o in res.outcomes}

    assert all(o.status != "error" for o in res.outcomes), [(o.tenant, o.message) for o in res.outcomes]
    assert not tg._is_systemic_failure(res.outcomes, True), where
    # control: tenant-b's own key opens, in every position.
    assert (by_tenant["tenant-b"].status, by_tenant["tenant-b"].keys) == ("pr_opened", ["mysql_connections"])
    if a_outcome is None:
        assert "tenant-a" not in by_tenant and "tenant-a" not in api.gets, where
    else:
        assert by_tenant["tenant-a"].status == a_outcome, where
        assert set(by_tenant["tenant-a"].keys) == own, where
    if a_outcome == "skipped_nested":
        assert "tenant-a" not in api.gets, "a nested tenant must not reach tenant-api"
    if a_outcome == "pr_opened":
        # written back in the spelling the file writes; every other line as it was
        new = yaml.safe_load(api.puts["tenant-a"])["tenants"]["tenant-a"]
        old = yaml.safe_load((conf_d / "tenant-a.yaml").read_text(encoding="utf-8"))["tenants"]["tenant-a"]
        assert set(new) == set(old), where
        assert {k for k in new if new[k] != old[k]} == own, where
    inherited = {(k.tenant, k.key) for k in res.inherited}
    assert not (inherited & {(p.tenant, c.key) for p in res.plans for c in p.changes}), where


def test_inherited_keys_never_trip_the_breaker(tmp_path, stubs):
    """More tenants than MAX_CONSECUTIVE_ERRORS that inherit their only mapped
    key, plus as many nested tenants that own one: no request for any of them,
    no error, and the control after them still opens (#2116 must-fire)."""
    n = tg.MAX_CONSECUTIVE_ERRORS + 2
    files = {"_defaults.yaml": "defaults:\n  mysql_connections: 60\n  mysql_threads_running: 80\n",
             "_platform.yaml": "tenants:\n" + "".join(
                 f"  p{i}:\n    mysql_threads_running: 60\n" for i in range(n)),
             "zz-control.yaml": 'tenants:\n  zz-control:\n    mysql_connections: "60"\n'}
    for i in range(n):
        files[f"d{i}.yaml"] = f'tenants:\n  d{i}:\n    foo_unmapped_key: "3"\n'
        files[f"p{i}.yaml"] = f'tenants:\n  p{i}:\n    foo_unmapped_key: "3"\n'
        files[f"team/n{i}.yaml"] = f'tenants:\n  n{i}:\n    mysql_connections: "60"\n'
    conf_d = _tree(tmp_path, files)
    api = stubs(conf_d)
    res = tg.run(_args(conf_d))
    st = collections.Counter(o.status for o in res.outcomes)
    assert st == {"skipped_nested": n, "pr_opened": 1}, st
    assert api.gets == ["zz-control"]
    assert not tg._is_systemic_failure(res.outcomes, True)
    assert len(res.inherited) >= 2 * n


def test_a_nested_skip_does_not_dilute_a_real_outage(tmp_path):
    """`skipped_nested` sent no request: it must not water down the systemic
    ratio of a run in which every request failed."""
    errs = [tg.TenantOutcome(f"t{i}", "error") for i in range(tg.MAX_CONSECUTIVE_ERRORS)]
    skips = [tg.TenantOutcome(f"s{i}", "skipped") for i in range(3)]
    nested = [tg.TenantOutcome(f"n{i}", "skipped_nested") for i in range(50)]
    assert tg._is_systemic_failure(errs + skips + nested, True)


def test_export_patch_never_writes_an_inherited_value(tmp_path, stubs):
    conf_d = _tree(tmp_path, MATRIX["mixed"][0])
    patch = tr.format_export_patch(
        tr.run_analysis(str(conf_d), prometheus_url="http://prom.stub", tenant_filter="tenant-a"))
    doc = yaml.safe_load(patch)
    assert set(doc["tenants"]["tenant-a"]) == {"mysql_threads_running"}
    own = next(ln for ln in patch.splitlines() if ln.strip().startswith("mysql_threads_running:"))
    assert "# was 60 (from tenant-a.yaml)" in own
    inh = [ln for ln in patch.splitlines() if "(inherited) mysql_connections" in ln]
    assert len(inh) == 1 and inh[0].lstrip().startswith("#")
    assert "mysql_connections: inherited, is 60 (from _defaults.yaml)" in inh[0]
    assert '"' not in inh[0], "an inherited key carries no recommended value"
