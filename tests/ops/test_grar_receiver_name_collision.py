"""#2279: generated receiver names must be unique, and a base receiver must not
shadow a generated one.

The tenant id has no character set that keeps the generated names apart:
tenant ``<t>``'s ``routes[0]`` receiver is ``tenant-<t>-route-0``, which is also
the MAIN receiver of a tenant called ``<t>-route-0`` (same for
``-override-<n>``). Measured before this: ``--validate`` rc 0, validate-config
PASS, and the written ConfigMap carried two receivers of that name — which
Alertmanager refuses to load.

Separately, ``--output-configmap --base-config`` kept a base receiver and
DROPPED the generated one of the same name — rc 0, amtool accepting, alerts
going to the base's stale endpoint.

The contract pinned here (owner decision on #2279), all with amtool NOT on
PATH so the verdict cannot be amtool's:

* duplicate generated names → ``--validate`` rc 1 naming BOTH sources; render /
  ``--output-configmap`` rc 1 with nothing written, with or without --strict;
  validate-config's ``routes`` row FAIL — through the same predicate;
* base shadowing a generated name → ``--output-configmap`` rc 1, nothing
  written; the four platform placeholder names are NOT shadowing;
* a tree without collisions is unaffected (control).

Tenant ids are fixture names (CLAUDE.md #9).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from factories import make_routing_config, make_tenant_yaml

import _grar_render as render
import _grar_validate as gv
import generate_alertmanager_routes as gar
import validate_config as vc

_GAR = (Path(__file__).resolve().parents[2] / "scripts" / "tools" / "ops"
        / "generate_alertmanager_routes.py")

_BASE_T = "t1"


def _write_tenant(d: Path, tenant: str, routing: dict) -> None:
    (d / f"{tenant}.yaml").write_text(
        make_tenant_yaml(tenant, keys={"mysql_connections": "70"},
                         routing=routing),
        encoding="utf-8")


def _routing_with(kind: str) -> dict:
    """`_BASE_T`'s routing carrying one `routes` or `overrides` entry."""
    r = make_routing_config(_BASE_T)
    child = {"receiver": {"type": "webhook",
                          "url": "https://hooks.example.com/child"}}
    if kind == "routes":
        child["match"] = {"severity": "critical"}
    else:
        child["alertname"] = "SomeAlert"
    r[kind] = [child]
    return r


# kind → (the colliding tenant id, the source the base tenant's entry is named by)
_KINDS = {
    "routes": (f"{_BASE_T}-route-0", f"tenant '{_BASE_T}' routes[0]"),
    "overrides": (f"{_BASE_T}-override-0", f"tenant '{_BASE_T}' overrides[0]"),
}


@pytest.fixture(params=sorted(_KINDS))
def clash(request, tmp_path):
    """A conf.d where `_BASE_T`'s child receiver and another tenant's main
    receiver get the same name. Returns (dir, kind)."""
    kind = request.param
    d = tmp_path / "conf.d"
    d.mkdir()
    _write_tenant(d, _BASE_T, _routing_with(kind))
    other, _src = _KINDS[kind]
    _write_tenant(d, other, make_routing_config(other))
    return d, kind


def _empty_path(tmp_path) -> str:
    d = tmp_path / "empty-path"
    d.mkdir(exist_ok=True)
    return str(d)


def _gar(argv, tmp_path):
    env = dict(os.environ, PATH=_empty_path(tmp_path))
    r = subprocess.run([sys.executable, "-s", str(_GAR), *argv],
                       capture_output=True, timeout=180, env=env)
    return r.returncode, r.stdout.decode("utf-8", "replace"), \
        r.stderr.decode("utf-8", "replace")


class TestGeneratedNameCollision:

    def test_generate_routes_names_both_sources(self, clash):
        d, kind = clash
        routing, _dedup, _sw, enforced, _md = gar.load_tenant_configs(str(d))
        _routes, receivers, warnings = gar.generate_routes(
            routing, enforced_routing=enforced)
        other, src = _KINDS[kind]
        names = [r["name"] for r in receivers]
        assert names.count(f"tenant-{other}") == 2, names
        lines = [w for w in warnings if gv.is_receiver_name_collision(w)]
        assert len(lines) == 1, warnings
        assert src in lines[0], lines[0]
        assert f"tenant '{other}' main receiver" in lines[0], lines[0]

    def test_validate_exits_1_and_names_both_sides(self, clash, tmp_path):
        d, kind = clash
        rc, out, err = _gar(["--config-dir", str(d), "--validate"], tmp_path)
        other, src = _KINDS[kind]
        assert rc == 1, err
        assert "OK: all configs valid" not in out
        assert src in err and f"tenant '{other}' main receiver" in err, err

    @pytest.mark.parametrize("mode", [[], ["--output-configmap"]],
                             ids=["render", "output-configmap"])
    @pytest.mark.parametrize("strict", [False, True], ids=["plain", "strict"])
    def test_write_modes_exit_1_and_write_nothing(self, clash, tmp_path,
                                                   mode, strict):
        d, _kind = clash
        out = tmp_path / "out.yaml"
        argv = ["--config-dir", str(d), *mode, "-o", str(out)]
        if strict:
            argv.append("--strict")
        rc, _out, err = _gar(argv, tmp_path)
        assert rc == 1, err
        assert not out.exists(), "a config with duplicate receiver names was written"
        assert "duplicate receiver name" in err, err

    def test_validate_config_routes_row_fails(self, clash):
        d, _kind = clash
        row = vc.check_routes(str(d))
        assert row["status"] == vc.FAIL, row
        assert row["hint"] == vc.RECEIVER_COLLISION_ROUTES_HINT, row

    def test_control_no_collision_is_unaffected(self, tmp_path):
        """Same shape minus the colliding tenant: rc 0, file written, PASS."""
        d = tmp_path / "conf.d"
        d.mkdir()
        _write_tenant(d, _BASE_T, _routing_with("routes"))
        _write_tenant(d, "t2", make_routing_config("t2"))
        rc, out, err = _gar(["--config-dir", str(d), "--validate"], tmp_path)
        assert rc == 0, err
        assert "OK: all configs valid" in out
        cm = tmp_path / "cm.yaml"
        rc, _out, err = _gar(["--config-dir", str(d), "--output-configmap",
                              "-o", str(cm)], tmp_path)
        assert rc == 0, err
        assert cm.is_file()
        assert vc.check_routes(str(d))["status"] == vc.PASS


class TestSharedPredicate:
    """validate-config and `_validate_mode` must call ONE predicate (#2164)."""

    def test_both_callers_use_the_shared_function(self):
        import inspect
        assert "blocking_generation_errors(" in inspect.getsource(gar._validate_mode)
        assert "gen.blocking_generation_errors(" in inspect.getsource(vc.check_routes)
        assert "gen.blocking_generation_errors(" in inspect.getsource(vc.check_schema)

    def test_collision_line_is_not_a_policy_error(self):
        """A duplicate name is blocking WITHOUT --strict; the policy prefix
        means "escalated by --strict", so the two must not overlap."""
        line = gv.receiver_name_collisions([("x", "a"), ("x", "b")])[0]
        assert not line.lstrip().startswith(gv.POLICY_ERROR_PREFIX)
        assert gv.blocking_generation_errors([line]) == [line]


class TestBaseShadowing:

    def _tree(self, tmp_path) -> Path:
        d = tmp_path / "conf.d"
        d.mkdir()
        _write_tenant(d, _BASE_T, make_routing_config(_BASE_T))
        return d

    def _base(self, tmp_path, receivers: list[dict]) -> Path:
        b = tmp_path / "base.yml"
        b.write_text(yaml.safe_dump({
            "route": {"receiver": "default"},
            "receivers": [{"name": "default"}, *receivers],
        }), encoding="utf-8")
        return b

    def test_base_receiver_with_a_generated_name_is_refused(self, tmp_path):
        d = self._tree(tmp_path)
        base = self._base(tmp_path, [{
            "name": f"tenant-{_BASE_T}",
            "webhook_configs": [{"url": "https://stale.example.com/from-base"}]}])
        out = tmp_path / "cm.yaml"
        rc, _out, err = _gar(["--config-dir", str(d), "--output-configmap",
                              "--base-config", str(base), "-o", str(out)],
                             tmp_path)
        assert rc == 1, err
        assert not out.exists()
        assert f"'tenant-{_BASE_T}'" in err, err
        assert "Traceback" not in err, err

    def test_platform_placeholder_names_in_base_are_not_shadowing(self, tmp_path):
        """These four are injected only if absent — a richer base definition
        (watchdog-heartbeat's url_file) is the intended shape."""
        d = self._tree(tmp_path)
        placeholders = sorted(render._platform_placeholder_receiver_names())
        assert set(placeholders) == {
            "custom-alerts-firehose", "watchdog-heartbeat",
            "synthetic-receiver", "sentinel-sinkhole"}, placeholders
        base = self._base(tmp_path, [
            {"name": n, "webhook_configs": [{"url": f"https://p.example.com/{n}"}]}
            for n in placeholders])
        out = tmp_path / "cm.yaml"
        rc, _out, err = _gar(["--config-dir", str(d), "--output-configmap",
                              "--base-config", str(base), "-o", str(out)],
                             tmp_path)
        assert rc == 0, err
        am = yaml.safe_load(yaml.safe_load(out.read_text(encoding="utf-8"))
                            ["data"]["alertmanager.yml"])
        names = [r["name"] for r in am["receivers"]]
        for n in placeholders:
            assert names.count(n) == 1, names
