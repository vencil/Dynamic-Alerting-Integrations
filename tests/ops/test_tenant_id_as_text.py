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

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS = REPO_ROOT / "scripts" / "tools"

import _grar_parse  # noqa: E402
import _lib_io  # noqa: E402
import _lib_yaml_keys  # noqa: E402
import check_routing_profiles as crp  # noqa: E402
import da_assembler  # noqa: E402
import deprecate_rule  # noqa: E402
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
    with pytest.raises(_lib_io.YamlFileError) as ei:
        _lib_io.load_tenant_configs(str(tmp_path))
    assert "python/object/apply" in str(ei.value) and "t.yaml" in str(ei.value), ei.value


# ── #2114 review: error shape and parser are what the readers had ────

def test_a_map_tag_on_a_sequence_keeps_policy_engine_at_rc_2(tmp_path, capsys):
    """`tenants: !!map [a, b]` — the loader leaked TypeError, which escaped
    every `except yaml.YAMLError` and turned policy-engine's rc 2
    caller-error JSON (main) into an rc 1 traceback. Measured end to end."""
    d = _tree(tmp_path, {"conf.d/t.yaml": "tenants: !!map [a, b]\n"})
    pol = tmp_path / "p.yaml"
    pol.write_text("policies:\n  - name: need-cpu\n    target: cpu\n"
                   "    operator: required\n", encoding="utf-8")
    rc = pe.main(["--config-dir", str(d / "conf.d"), "--policy", str(pol), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 2, out
    assert out["status"] == "caller_error" and out["reason"] == "yaml_file_unreadable", out


_TRAILING_TAB_TENANT = "tenants:\n  tx:\n    cpu: 80\t\n"   # pure parser refuses


def test_a_file_the_pure_parser_refuses_declares_nothing(tmp_path, capsys):
    """Every reader stays on the parser it had (all pure; libyaml choice is
    #2123). With `declared_tenant_ids` on libyaml, a tenant file the routing
    reader itself drops as unreadable still DECLARED tx, so the platform's
    `tenants.tx` block silently created the tenant and its `ignored` WARN
    vanished — two answers in one run."""
    d = _tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  cpu: 70\ntenants:\n  tx:\n    cpu: 60\n",
        "tx.yaml": _TRAILING_TAB_TENANT})
    assert declared_tenant_ids(d) == set()
    got = _grar_parse.load_tenant_tree(str(d))
    assert "tx" not in got.dedup_configs, got.dedup_configs
    err = capsys.readouterr().err
    assert "tenants.tx ignored" in err, err


def test_tenant_uniqueness_agrees_with_yaml_syntax_about_one_file(tmp_path):
    """`yaml_syntax` (pure parser) calls the trailing-tab file unreadable;
    `tenant_uniqueness` must not read it anyway and report a duplicate from
    a file the same run says it cannot read."""
    import validate_config as vc
    d = _tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  cpu: 70\n",
        "a.yaml": "tenants:\n  tx:\n    cpu: 80\n",
        "b.yaml": _TRAILING_TAB_TENANT})
    r = vc.check_tenant_uniqueness(str(d))
    assert r["status"] == vc.WARN, r
    assert "b.yaml" in " ".join(r["details"]), r


# ── writers: the id written back is the id that was read (#2216) ──────
#
# The two tools that WRITE a `tenants:` block read it with PyYAML's typing
# and dumped that back: an unquoted `010:` went to disk as `8:`, `yes:` as
# `true:`, `0x1F:` as `31:` — the tenant silently renamed. Three trees:
# UNQ (unquoted spellings PyYAML retypes), QUOTED (the same ids quoted: one
# id each to the exporter, so they must come out identical to UNQ), CTRL
# (`acme`, which no typing touches: its output must stay byte-for-byte what
# main wrote, so a regression in the writer itself still shows here).
# `~` / `null` are left out: the exporter drops a null key (matrix row
# `exporter_key: null`), so there is no tenant id to preserve.

_UNQ = ("010", "yes", "0x1F", "1_000")
_QUOTED = tuple(f'"{t}"' for t in _UNQ)

_MATRIX = json.loads((REPO_ROOT / "tests" / "shared"
                      / "tenant_id_yaml_spelling_matrix.json")
                     .read_text(encoding="utf-8"))
# Every spelling the exporter reads as a tenant (not rejected, not dropped).
_MATRIX_IDS = [r for r in _MATRIX["spellings"]
               if not r.get("rejected") and r["exporter_key"] is not None]

_DEPRECATE_HEADER = "# header kept\n"
_DEPRECATE_BODY = ('    mysql_connections: "70"\n'
                   '    mysql_connections_critical: "90"\n'
                   '    container_cpu: "80"\n'
                   '    _severity_dedup: "enable"\n')

# What main wrote for the CTRL tree (measured on 96e963cb, unmodified code).
_DEPRECATE_CTRL_ON_MAIN = ("# header kept\ntenants:\n  acme:\n"
                           "    container_cpu: '80'\n    _severity_dedup: enable\n")
_ASSEMBLER_CTRL_ON_MAIN = (
    "# Auto-generated by da-assembler from ThresholdConfig ns/ctrl\n"
    "# DO NOT EDIT — changes will be overwritten on next reconcile.\n"
    "tenants:\n  acme:\n    mysql_connections: '70'\n"
    "    _severity_dedup: enable\n")


def _as_text(path: Path):
    """Re-read a written file the way the exporter reads it."""
    return _lib_yaml_keys.load_exporter_keys(
        io.StringIO(path.read_text(encoding="utf-8")))


def _deprecate_tree(root: Path, keys) -> Path:
    body = "".join(f"  {k}:\n{_DEPRECATE_BODY}" for k in keys)
    return _tree(root, {"t.yaml": _DEPRECATE_HEADER + "tenants:\n" + body})


def _deprecate(root: Path):
    return deprecate_rule.remove_from_tenants(
        "mysql_connections", str(root), execute=True)


@pytest.mark.parametrize("keys", [_UNQ, _QUOTED], ids=["UNQ", "QUOTED"])
def test_deprecate_rule_writes_back_the_tenant_ids_as_read(tmp_path, keys):
    """Before: UNQ went to disk as `8:` / `true:` / `31:` / `1000:`."""
    d = _deprecate_tree(tmp_path, keys)
    removed = _deprecate(d)
    assert sorted({t for _f, t, _k, _v in removed}) == sorted(_UNQ), removed
    got = _as_text(d / "t.yaml")
    assert got == {"tenants": {t: {"container_cpu": "80", "_severity_dedup": "enable"}
                               for t in _UNQ}}, got
    assert (d / "t.yaml").read_text(encoding="utf-8").startswith(_DEPRECATE_HEADER)


def test_deprecate_rule_unq_and_quoted_write_the_same_bytes(tmp_path):
    """One id to the exporter, so one output — whichever way it was spelled."""
    unq = _deprecate_tree(tmp_path / "unq", _UNQ)
    quo = _deprecate_tree(tmp_path / "quoted", _QUOTED)
    _deprecate(unq)
    _deprecate(quo)
    assert (unq / "t.yaml").read_bytes() == (quo / "t.yaml").read_bytes()


def test_deprecate_rule_ctrl_output_is_what_main_wrote(tmp_path):
    d = _deprecate_tree(tmp_path, ("acme",))
    _deprecate(d)
    assert (d / "t.yaml").read_text(encoding="utf-8") == _DEPRECATE_CTRL_ON_MAIN


def test_deprecate_rule_reports_the_tenant_as_written(tmp_path):
    """The preview names `tenants.010`, not `tenants.8`."""
    d = _deprecate_tree(tmp_path, _UNQ)
    found = deprecate_rule.scan_for_metric("mysql_connections", str(d))
    owners = {sec for f in found for sec, _k, _v in f["occurrences"]}
    assert owners == {f"tenants.{t}" for t in _UNQ}, owners


@pytest.mark.parametrize("row", _MATRIX_IDS, ids=lambda r: r["source"])
def test_deprecate_rule_keeps_every_matrix_spelling(tmp_path, row):
    """Every spelling of the shared Go/Python table comes back as the
    exporter's id (`exporter_key`) after a write."""
    _tree(tmp_path, {"t.yaml": (f"tenants:\n  {row['source']}:\n"
                                '    mysql_connections: "1"\n'
                                '    container_cpu: "2"\n')})
    _deprecate(tmp_path)
    got = _as_text(tmp_path / "t.yaml")
    assert got == {"tenants": {row["exporter_key"]: {"container_cpu": "2"}}}, got


def test_deprecate_rule_keeps_the_defaults_carriers_block_keys(tmp_path):
    """`_remove_in_carrier` rewrites `_defaults.yaml` WHOLE, so the
    `profiles:` / `tenants:` blocks in it go through the same dump: a
    profile `yes:` must not come back `true:`, a platform `010:` not `8:`."""
    _tree(tmp_path, {"_defaults.yaml": (
        "defaults:\n  mysql_connections: 70\n  container_cpu: 80\n"
        "profiles:\n  yes:\n    container_cpu: 55\n"
        "tenants:\n  010:\n    container_cpu: 60\n")})
    [(_p, ok, msg, removed)] = deprecate_rule.remove_from_all_defaults(
        "mysql_connections", str(tmp_path), execute=True)
    assert ok and removed, msg
    got = _as_text(tmp_path / "_defaults.yaml")
    assert got == {"defaults": {"container_cpu": 80},
                   "profiles": {"yes": {"container_cpu": 55}},
                   "tenants": {"010": {"container_cpu": 60}}}, got


def test_deprecate_rule_keeps_a_profile_reference_bound(tmp_path):
    """#2216 blind review: with the profile KEY kept as `'010'` but the
    tenant's `_profile: 010` VALUE still typed, the write left
    `_profile: 8` — a reference to no profile (main renamed both to 8, and
    they happened to meet). The exporter reads both as the text `010`."""
    d = _tree(tmp_path, {
        "_defaults.yaml": ("defaults:\n  mysql_connections: 70\n"
                           "  container_cpu: 80\n"
                           'profiles:\n  010:\n    container_cpu: "55"\n'),
        "t.yaml": ("tenants:\n  acme:\n    _profile: 010\n"
                   '    mysql_connections: "60"\n')})
    deprecate_rule.remove_from_all_defaults("mysql_connections", str(d),
                                            execute=True)
    assert _deprecate(d), "the tenant file was not rewritten"
    platform = _as_text(d / "_defaults.yaml")
    tenant = _as_text(d / "t.yaml")
    assert list(platform["profiles"]) == ["010"], platform
    assert tenant == {"tenants": {"acme": {"_profile": "010"}}}, tenant
    p = subprocess.run(
        [sys.executable, str(TOOLS / "dx" / "describe_tenant.py"), "acme",
         "--conf-d", str(d)],
        capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, p.stderr
    eff = json.loads(p.stdout)["effective_config"]
    assert eff["container_cpu"] == "55", eff


def _cr(name: str, tenants_yaml: str) -> str:
    return ("apiVersion: dynamicalerting.io/v1alpha1\nkind: ThresholdConfig\n"
            f"metadata:\n  name: {name}\n  namespace: ns\n"
            f"spec:\n  tenants:\n{tenants_yaml}")


def _assemble(tmp_path: Path, name: str, keys) -> Path:
    body = "".join(f'    {k}:\n      mysql_connections: "70"\n'
                   '      _severity_dedup: "enable"\n' for k in keys)
    cr = tmp_path / f"{name}-cr.yaml"
    cr.write_text(_cr(name, body), encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    assert da_assembler.render_cr_file(cr, out) == 0
    return out / f"{name}.yaml"


@pytest.mark.parametrize("keys", [_UNQ, _QUOTED], ids=["UNQ", "QUOTED"])
def test_da_assembler_renders_the_tenant_ids_as_written(tmp_path, keys):
    """Before: `010:` in the CR rendered as `tenants:\\n  8:`."""
    got = _as_text(_assemble(tmp_path, "t", keys))
    assert got == {"tenants": {t: {"mysql_connections": "70",
                                   "_severity_dedup": "enable"}
                               for t in _UNQ}}, got


def test_da_assembler_unq_and_quoted_render_the_same_body(tmp_path):
    unq = _assemble(tmp_path, "t", _UNQ).read_text(encoding="utf-8")
    quo = _assemble(tmp_path, "t2", _QUOTED).read_text(encoding="utf-8")
    # Only the header line naming the CR differs.
    assert unq.split("\n", 1)[1] == quo.split("\n", 1)[1]


def test_da_assembler_ctrl_output_is_what_main_wrote(tmp_path):
    out = _assemble(tmp_path, "ctrl", ("acme",))
    assert out.read_text(encoding="utf-8") == _ASSEMBLER_CTRL_ON_MAIN


@pytest.mark.parametrize("row", _MATRIX_IDS, ids=lambda r: r["source"])
def test_da_assembler_keeps_every_matrix_spelling(tmp_path, row):
    cr = tmp_path / "cr.yaml"
    cr.write_text(_cr("m", f'    {row["source"]}:\n      container_cpu: "2"\n'),
                  encoding="utf-8")
    assert da_assembler.render_cr_file(cr, tmp_path) == 0
    got = _as_text(tmp_path / "m.yaml")
    assert got == {"tenants": {row["exporter_key"]: {"container_cpu": "2"}}}, got
