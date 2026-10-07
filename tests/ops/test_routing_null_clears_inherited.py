"""#2521: a tenant's ``_routing.overrides: ~`` / ``routes: ~`` clears what it
would inherit (≡ ``[]``), and the schema says so.

The generator merges ``_routing_defaults`` → routing profile → the tenant's
``_routing`` one top-level key at a time (shallow), so an explicit null in the
tenant replaces the inherited list with nothing — the same as ``[]``. Pinned
here for the generator and validate-config; da-guard and tenant-api have
their own pins (cmd/da-guard routing_source_test.go, tenant-api
receiver_shape_test.go). Not to be confused with a SUBDIRECTORY level's
``_routing_defaults.overrides: ~``, which the generator refuses (exit 2,
``routing_defaults_null_below_root``) — unchanged.
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

# #2115 0-B／B3: validate-config 的 profiles 列經 da-guard 讀租戶，每次都需要 da-guard
# （conftest 以 `go build` 建出；建不起來就 fail、不 skip）。
pytestmark = pytest.mark.usefixtures("da_guard_env")

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS_DIR = REPO_ROOT / "scripts" / "tools" / "ops"

_DEFAULTS = (
    "_routing_defaults:\n"
    "  receiver:\n    type: webhook\n    url: https://hooks.example.com/d\n"
    "  overrides:\n"
    "    - alertname: InheritedX\n"
    "      receiver:\n        type: webhook\n        url: https://hooks.example.com/x\n")
_PROFILES = (
    "routing_profiles:\n  team:\n"
    "    receiver:\n      type: webhook\n      url: https://hooks.example.com/p\n"
    "    routes:\n      - match: {severity: critical}\n"
    "        receiver:\n          type: webhook\n          url: https://hooks.example.com/c\n")


def _tree(tmp_path: Path, keyword: str, spelling: str) -> Path:
    d = tmp_path / "conf.d"
    d.mkdir()
    (d / "_defaults.yaml").write_text(_DEFAULTS, encoding="utf-8")
    (d / "_routing_profiles.yaml").write_text(_PROFILES, encoding="utf-8")
    for t, routing in (("opt-out", f"      {keyword}: {spelling}\n"), ("sibling", None)):
        body = f"tenants:\n  {t}:\n    _routing_profile: team\n"
        if routing is not None:
            body += "    _routing:\n" + routing
        (d / f"{t}.yaml").write_text(body, encoding="utf-8")
    return d


def _children(tmp_path: Path, keyword: str, spelling: str) -> dict[str, list[str]]:
    gar = importlib.import_module("generate_alertmanager_routes")
    routing, dedup, schema_warnings, enforced, _mc = gar.load_tenant_configs(
        str(_tree(tmp_path, keyword, spelling)))
    routes, _r, warnings = gar.generate_routes(routing, enforced_routing=enforced,
                                               tenants=dedup)
    assert gar.blocking_generation_errors(schema_warnings + warnings) == []
    out = {}
    for r in routes:
        name = r.get("receiver", "")
        for t in ("opt-out", "sibling"):
            if name == f"tenant-{t}":
                out[t] = [c["receiver"] for c in r.get("routes", [])]
    return out


@pytest.mark.parametrize("spelling", ["~", "null", "[]"])
def test_overrides_null_clears_the_inherited_overrides(tmp_path, spelling):
    kids = _children(tmp_path, "overrides", spelling)
    assert kids["opt-out"] == ["tenant-opt-out-route-0"], kids
    assert kids["sibling"] == ["tenant-sibling-override-0", "tenant-sibling-route-0"], kids


@pytest.mark.parametrize("spelling", ["~", "null", "[]"])
def test_routes_null_clears_the_profile_routes(tmp_path, spelling):
    kids = _children(tmp_path, "routes", spelling)
    assert kids["opt-out"] == ["tenant-opt-out-override-0"], kids
    assert kids["sibling"] == ["tenant-sibling-override-0", "tenant-sibling-route-0"], kids


@pytest.mark.parametrize("keyword", ["overrides", "routes"])
def test_validate_config_and_generator_accept_it(tmp_path, keyword):
    d = _tree(tmp_path, keyword, "~")
    env = {**os.environ, "PATH": str(tmp_path), "PYTHONIOENCODING": "utf-8"}
    for argv in ([str(OPS_DIR / "validate_config.py"), "--config-dir", str(d)],
                 [str(OPS_DIR / "generate_alertmanager_routes.py"),
                  "--config-dir", str(d), "--validate"]):
        r = subprocess.run([sys.executable, *argv], capture_output=True, text=True,
                           encoding="utf-8", timeout=120, env=env)
        assert r.returncode == 0, r.stdout + r.stderr
