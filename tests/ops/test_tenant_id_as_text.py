"""A tenant id is the `tenants:` key's source TEXT in every Python reader (#2114).

The exporter keys tenants by the scalar's text (yaml.v3 → `map[string]…`);
PyYAML's YAML 1.1 typing reads `010` as 8, `0x1F` as 31, `yes` as True and
`123` as the int 123. Before #2114 each Python reader below took PyYAML's
value, and the suite had no numeric or bool-looking tenant id anywhere — a
key-only change passed the whole suite while policy exclusion regressed.
Each test here names what it pins; the loader's own Go-parity table is
tests/shared/test_tenant_id_yaml_spelling_parity.py, the platform overlay
rows are in tests/shared/platform_tenant_overlay_matrix.json.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS = REPO_ROOT / "scripts" / "tools"

import _grar_parse  # noqa: E402
import _lib_io  # noqa: E402
import check_routing_profiles as crp  # noqa: E402
import diagnose  # noqa: E402
import policy_engine as pe  # noqa: E402
from _lib_confd import declared_tenant_ids  # noqa: E402


def _tree(root: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return root


_ROUTING = ('    _routing:\n      receiver:\n        type: webhook\n'
            '        url: "https://hook.example/{{tenant}}"\n')


# ── routing plane ─────────────────────────────────────────────────────

def test_route_generation_emits_the_ids_as_written(tmp_path):
    """Before: rc 1, ``TypeError`` in ``_grar_merge._substitute_tenant`` (a
    bool id from ``yes:`` substituted into ``{{tenant}}``); ``010`` routed as
    ``tenant="8"``, a tenant the exporter does not have."""
    d = _tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  cpu: 80\n",
        "t.yaml": ("tenants:\n  010:\n" + _ROUTING + "  0x1F:\n" + _ROUTING
                   + "  yes:\n" + _ROUTING)})
    p = subprocess.run(
        [sys.executable, str(TOOLS / "ops" / "generate_alertmanager_routes.py"),
         "--config-dir", str(d), "--dry-run"],
        capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr[-2000:]
    for tid in ("010", "0x1F", "yes"):
        assert f'tenant="{tid}"' in p.stdout, (tid, p.stdout)
        assert f"https://hook.example/{tid}" in p.stdout, (tid, p.stdout)
    for wrong in ('tenant="8"', 'tenant="31"', 'tenant="True"'):
        assert wrong not in p.stdout, (wrong, p.stdout)


def test_an_int_looking_tenant_with_its_own_routing_loads(tmp_path):
    """``123:`` with its own ``_routing`` and no platform file: before, the
    int id reached ``{{tenant}}`` substitution and ``load_tenant_tree``
    raised TypeError."""
    d = _tree(tmp_path, {"t.yaml": "tenants:\n  123:\n" + _ROUTING})
    got = _grar_parse.load_tenant_tree(str(d))
    assert set(got.routing_configs) == {"123"}, got.routing_configs
    assert got.routing_configs["123"]["receiver"]["url"] == "https://hook.example/123"


def test_a_domain_policy_tenant_list_meets_the_text_ids(tmp_path):
    """A domain policy's ``tenants: [010]`` must name tenant "010" — with the
    keys as text and the list still typed (8) the policy silently stopped
    applying. Checked on the strict routing path."""
    d = _tree(tmp_path, {
        "_domain_policy.yaml": (
            "domain_policies:\n  p:\n    tenants: [010]\n"
            "    constraints:\n      forbidden_receiver_types: [webhook]\n"),
        "t.yaml": "tenants:\n  010:\n" + _ROUTING})
    _r, _d, warnings, _e, _m = _grar_parse.load_tenant_configs(
        str(d), strict_policies=True)
    hits = [w for w in warnings if "tenant '010'" in w and "forbidden" in w]
    assert hits, warnings


# ── policy engine: exclude_tenants compares text to text ─────────────

_POLICY = ("_policies:\n  - name: need-cpu\n    target: cpu\n"
           "    operator: required\n    exclude_tenants: {exclude}\n")


@pytest.mark.parametrize("tenant_key,exclude,excluded", [
    ("123", "[123]", True),
    ('"123"', "[123]", True),
    ("123", '["123"]', True),
    ("010", "[010]", True),
    ("yes", "[yes]", True),
    ("123", "[zzz]", False),   # control: the rule does fire
])
def test_exclude_tenants_matches_by_text(tmp_path, tenant_key, exclude, excluded):
    """On main the int/int and 010/010 rows were excluded only because BOTH
    sides were typed; the mixed rows ("123" vs [123]) never were. After
    #2114 every spelling of one id matches, and the control still fires."""
    d = _tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mem: 1\n" + _POLICY.format(exclude=exclude),
        "t.yaml": f"tenants:\n  {tenant_key}:\n    mem: 2\n"})
    rules = pe.load_policies(str(d / "_defaults.yaml"))
    configs = pe.load_tenant_configs(str(d))
    result = pe.evaluate_policies(rules, configs)
    assert (result.violations == []) is excluded, result.violations


def test_exclude_tenants_from_a_standalone_policy_file(tmp_path, capsys):
    """The ``--policy`` path reads its file itself (not through
    ``load_policies``); it must read the list the same way."""
    d = _tree(tmp_path, {"conf.d/t.yaml": "tenants:\n  010:\n    mem: 2\n"})
    pol = tmp_path / "p.yaml"
    pol.write_text("policies:\n  - name: need-cpu\n    target: cpu\n"
                   "    operator: required\n    exclude_tenants: [010]\n",
                   encoding="utf-8")
    rc = pe.main(["--config-dir", str(d / "conf.d"), "--policy", str(pol), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["violations"] == [], out


# ── shared readers ───────────────────────────────────────────────────

def test_load_tenant_configs_keys_are_the_source_text(tmp_path):
    """``123:`` in one file and ``"123":`` in another are ONE tenant, as in
    the exporter; ``010`` / ``yes`` keep their spelling; ids are ``str``."""
    d = _tree(tmp_path, {
        "a.yaml": "tenants:\n  123:\n    x: 1\n  010:\n    x: 2\n",
        "b.yaml": 'tenants:\n  "123":\n    x: 3\n  yes:\n    x: 4\n'})
    got = _lib_io.load_tenant_configs(str(d))
    assert got == {"123": {"x": 3}, "010": {"x": 2}, "yes": {"x": 4}}, got
    assert all(isinstance(k, str) for k in got)


def test_declared_tenant_ids_are_the_source_text(tmp_path):
    d = _tree(tmp_path, {
        "a.yaml": "tenants:\n  010: {}\n  0x1F: {}\n",
        "team/b.yaml": "tenants:\n  yes: {}\n  123: {}\n"})
    assert declared_tenant_ids(d) == {"010", "0x1F", "yes", "123"}


def test_describe_tenant_overlays_the_platform_block_for_text_ids(tmp_path):
    """``describe_tenant 010`` used to answer "not found" (the id was ``8``)
    and ``yes`` was ``True``; with the platform block keyed the same way
    both get the platform's value."""
    d = _tree(tmp_path, {
        "_defaults.yaml": ("defaults:\n  mysql_connections: 80\ntenants:\n"
                           "  010:\n    mysql_connections: \"60\"\n"
                           "  yes:\n    mysql_connections: \"61\"\n"),
        "t.yaml": "tenants:\n  010: {}\n  yes: {}\n"})
    script = str(TOOLS / "dx" / "describe_tenant.py")
    for tid, want in (("010", "60"), ("yes", "61")):
        p = subprocess.run([sys.executable, script, tid, "--conf-d", str(d)],
                           capture_output=True, text=True, timeout=30)
        assert p.returncode == 0, (tid, p.stderr)
        out = json.loads(p.stdout)
        assert out["effective_config"]["mysql_connections"] == want, (tid, out)


def test_diagnose_finds_a_platform_profile_for_a_text_id(tmp_path):
    """The CLI tenant is a str; ``123:`` in the platform file was the int
    123, so the platform's ``_profile`` never reached tenant "123"."""
    d = _tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  cpu: 80\ntenants:\n  123:\n    _profile: gold\n",
        "t.yaml": 'tenants:\n  "123":\n    cpu: 90\n'})
    assert diagnose.lookup_tenant_profile("123", str(d)) == "gold"


def test_check_routing_profiles_reads_text_ids(tmp_path):
    """Platform ``123:`` + tenant file ``"123":`` is one tenant, so the
    platform's ``_routing_profile`` is that tenant's reference; a domain
    policy's ``[010]`` names the tenant ``010:``."""
    d = _tree(tmp_path, {
        "_defaults.yaml": "tenants:\n  123:\n    _routing_profile: team-a\n",
        "_routing_profiles.yaml": "routing_profiles:\n  team-a:\n    group_wait: 30s\n",
        "_domain_policy.yaml": "domain_policies:\n  p:\n    tenants: [010]\n",
        "t.yaml": 'tenants:\n  "123": {}\n  010: {}\n'})
    data = crp._collect_data(str(d))
    assert data["profile_refs"] == {"123": "team-a"}, data["profile_refs"]
    assert data["tenant_ids"] == {"123", "010"}, data["tenant_ids"]
    assert data["platform_orphans"] == [], data["platform_orphans"]
    msgs = crp.validate(data, strict=True)
    assert not [m for m in msgs if "not found" in m], msgs


def test_a_python_object_tag_in_a_tenant_file_is_still_refused(tmp_path):
    """The readers switched loaders; the refusal must have come along
    (``load_tenant_configs`` → YamlFileError, a yaml.YAMLError)."""
    _tree(tmp_path, {"t.yaml": "tenants:\n  t: !!python/object/apply:os.system ['echo x']\n"})
    with pytest.raises(_lib_io.YamlFileError):
        _lib_io.load_tenant_configs(str(tmp_path))
    assert os.path.exists(tmp_path / "t.yaml")
