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
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS = REPO_ROOT / "scripts" / "tools"

import _grar_parse  # noqa: E402
import _lib_io  # noqa: E402
import _lib_tenant_values  # noqa: E402
import _lib_yaml_keys  # noqa: E402
import check_routing_profiles as crp  # noqa: E402
import da_assembler  # noqa: E402
import deprecate_rule  # noqa: E402
import diagnose  # noqa: E402
import patch_config as pc  # noqa: E402
import policy_engine as pe  # noqa: E402
from _lib_confd import declared_tenant_ids  # noqa: E402
from _patch_config_fake import FakeCluster  # noqa: E402


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
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
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
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
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
# id each to the exporter, so they must come out as the same ids as UNQ —
# since #2220 each keeps its own quoting, so not the same bytes), CTRL
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


def _as_text_profiles(path: Path):
    """`_as_text`, plus a `_profile:` VALUE read as its source text — as the
    exporter reads it (`ScheduledValue` keeps the text, #2216)."""
    return _lib_yaml_keys.load_exporter_keys(
        io.StringIO(path.read_text(encoding="utf-8")),
        raw_text_scalars=("_profile",))


@pytest.fixture(scope="module")
def da_guard(tmp_path_factory):
    """da-guard built from this checkout, or None when `go` is missing (the
    test then skips its exporter half; VIBE_REQUIRE_GO=1 fails instead)."""
    if shutil.which("go") is None:
        assert os.environ.get("VIBE_REQUIRE_GO") != "1", \
            "VIBE_REQUIRE_GO=1 but `go` is not on PATH"
        return None
    out = tmp_path_factory.mktemp("da-guard") / "da-guard"
    env = {**os.environ}
    env.setdefault("GOTOOLCHAIN", "auto")
    subprocess.run(["go", "build", "-buildvcs=false", "-o", str(out),
                    "./cmd/da-guard"],
                   cwd=REPO_ROOT / "components" / "threshold-exporter" / "app",
                   env=env, check=True, timeout=600)
    return str(out)


def _served(conf_d: Path, da_guard):
    """{tenant: values} as /metrics serves them (`da-guard served-values`)."""
    if da_guard is None:
        pytest.skip("go not on PATH: the exporter half of this test needs "
                    "da-guard (set VIBE_REQUIRE_GO=1 to fail instead)")
    return {t: v.values for t, v in _lib_tenant_values.load_served_values(
        conf_d, binary=da_guard).items()}


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


def test_deprecate_rule_unq_and_quoted_write_the_same_ids(tmp_path, da_guard):
    """One id to the exporter, so one result — whichever way it was spelled.

    #2220: this pinned the same BYTES, which held only because the write
    quoted every id YAML would retype (`010:` → `'010':`) — the same dump
    that retyped every plain VALUE (`010` → 8). The write now keeps each
    scalar as written, so the two files differ in quoting and nothing else;
    the invariant is unchanged and is asserted where it lives: the ids (and
    values) the exporter reads — by the raw-text reader, and by da-guard."""
    unq = _deprecate_tree(tmp_path / "unq", _UNQ)
    quo = _deprecate_tree(tmp_path / "quoted", _QUOTED)
    _deprecate(unq)
    _deprecate(quo)
    assert _as_text(unq / "t.yaml") == _as_text(quo / "t.yaml")
    unq_text = (unq / "t.yaml").read_text(encoding="utf-8")
    quo_text = (quo / "t.yaml").read_text(encoding="utf-8")
    for t in _UNQ:      # each file keeps its own spelling of the id
        assert f"\n  {t}:\n" in unq_text, unq_text
        assert f"\n  '{t}':\n" in quo_text, quo_text
    served = _served(unq, da_guard)
    assert sorted(served) == sorted(_UNQ), served
    assert served == _served(quo, da_guard)


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


def test_deprecate_rule_keeps_a_profile_reference_bound(tmp_path, da_guard):
    """#2216 blind review: with the profile KEY kept as `'010'` but the
    tenant's `_profile: 010` VALUE still typed, the write left
    `_profile: 8` — a reference to no profile (main renamed both to 8, and
    they happened to meet). The exporter reads both as the text `010`.

    #2220: both are now written back as written (`010:`, `_profile: 010`,
    plain) where the write used to quote them. The oracle moved with that,
    to the raw-text reader and the exporter's served value (`container_cpu`
    from profile `010`, 55 — the default is 80): `describe_tenant` then read
    `_profile` with PyYAML typing (plain `010` → 8, fixed by #2297 and
    pinned in test_profile_ref_as_text.py)."""
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
    tenant = _as_text_profiles(d / "t.yaml")
    assert list(platform["profiles"]) == ["010"], platform
    assert tenant == {"tenants": {"acme": {"_profile": "010"}}}, tenant
    assert _served(d, da_guard)["acme"]["container_cpu"] == 55.0


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


# ── da_assembler --render-cr: VALUES and metadata.name as written ──────
#
# #2331: the CR was read with PyYAML typing and dumped back, so an unquoted
# value was retyped on the way through — `010` written as `8`, `0x1F` as
# `31`, `12:30` as `750` (the `:30` severity suffix gone), a timestamp as
# `2026-01-02 03:04:05+00:00`. The exporter's value moved with it.
# #2372: `metadata.name` was read the same way, so `name: 010` rendered
# `8.yaml` (and `yes` → `True.yaml`) while the body still said `010` —
# tenant-api finds a tenant by file name and answered 404.

_VALUES_BLOCK = ("    t1:\n"
                 "      mysql_connections: {q}010{q}\n"
                 "      container_cpu: {q}0x1F{q}\n"
                 "      pg_connections: {q}12:30{q}\n"
                 "      _metadata:\n"
                 "        owner: {q}2026-01-02T03:04:05Z{q}\n")
_VALUES_DEFAULTS = ("defaults:\n  mysql_connections: 1\n  container_cpu: 1\n"
                    "  pg_connections: 1\n")


def _render_values(tmp_path: Path, q: str) -> tuple[str, Path]:
    """(the CR's tenants block, the conf.d file the assembler wrote)."""
    block = _VALUES_BLOCK.format(q=q)
    cr = tmp_path / "cr.yaml"
    cr.write_text(_cr("t1", block), encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    assert da_assembler.render_cr_file(cr, out) == 0
    return block, out / "t1.yaml"


def test_da_assembler_writes_unquoted_values_back_as_written(tmp_path):
    """Before: `mysql_connections: 8`, `container_cpu: 31`,
    `pg_connections: 750`, `owner: 2026-01-02 03:04:05+00:00`."""
    _block, out = _render_values(tmp_path, "")
    body = out.read_text(encoding="utf-8")
    for line in ("mysql_connections: 010\n", "container_cpu: 0x1F\n",
                 "pg_connections: 12:30\n", "owner: 2026-01-02T03:04:05Z\n"):
        assert line in body, (line, body)


def test_da_assembler_quoted_values_stay_quoted(tmp_path):
    """Control: a quoted value was a string before and still is."""
    _block, out = _render_values(tmp_path, '"')
    body = out.read_text(encoding="utf-8")
    for line in ("mysql_connections: '010'\n", "container_cpu: '0x1F'\n",
                 "pg_connections: '12:30'\n",
                 "owner: '2026-01-02T03:04:05Z'\n"):
        assert line in body, (line, body)


@pytest.mark.parametrize("q", ["", '"'], ids=["UNQ", "QUOTED"])
def test_da_assembler_serves_what_the_cr_says(tmp_path, da_guard, q):
    """Oracle: the exporter's served values for the rendered file equal those
    for the CR's `tenants:` block copied verbatim into conf.d. Before (UNQ):
    10 → 8, 12 → 750, the `:30` severity lost, the timestamp text changed."""
    block, out = _render_values(tmp_path, q)
    (out.parent / "_defaults.yaml").write_text(_VALUES_DEFAULTS,
                                               encoding="utf-8")
    ref = _tree(tmp_path / "ref", {
        "_defaults.yaml": _VALUES_DEFAULTS,
        "t1.yaml": "tenants:\n" + "".join(
            line[2:] + "\n" for line in block.splitlines())})
    if da_guard is None:
        pytest.skip("go not on PATH: this test needs da-guard "
                    "(set VIBE_REQUIRE_GO=1 to fail instead)")
    got = _lib_tenant_values.load_served_values(out.parent, binary=da_guard)
    want = _lib_tenant_values.load_served_values(ref, binary=da_guard)
    assert {t: (v.values, v.severities) for t, v in got.items()} == \
        {t: (v.values, v.severities) for t, v in want.items()}


@pytest.mark.parametrize("name,want", [
    ("010", None), ("0x1F", None), ("yes", None),
    ('"010"', "010"), ('"0x1F"', "0x1F"), ('"yes"', "yes"), ("abc", "abc"),
    ("2026-01-02", "2026-01-02"),
    ("2026-01-02T03:04:05Z", "2026-01-02T03:04:05Z"),
], ids=["010", "0x1F", "yes", "q010", "q0x1F", "qyes", "abc", "date",
        "datetime"])
def test_da_assembler_names_the_file_as_the_cr_does(tmp_path, name, want):
    """Before: `name: 010` → `8.yaml`, `0x1F` → `31.yaml`, `yes` →
    `True.yaml`; the header named the same wrong CR.

    #2371: an unquoted name YAML 1.1 (PyYAML) types as a number / bool is
    refused (rc 2, nothing written: `want` None); a quoted one, and an
    unquoted date / datetime, names the file as written."""
    cr = tmp_path / "cr.yaml"
    cr.write_text(_cr(name, '    "010":\n      mysql_connections: "70"\n'),
                  encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    rc = da_assembler.render_cr_file(cr, out)
    if want is None:
        assert rc == 2
        assert list(out.iterdir()) == []
        return
    assert rc == 0
    assert sorted(p.name for p in out.iterdir()) == [f"{want}.yaml"]
    header = (out / f"{want}.yaml").read_text(encoding="utf-8").split("\n")[0]
    assert header.endswith(f"ThresholdConfig ns/{want}"), header


# A spec block that is not a mapping is not written (as before #2331). Read
# PyYAML-typed, `defaults: 0` / `stateFilters: false` were falsy and dropped;
# read as their text (RawPlain "0") they are truthy, and writing them out
# made the exporter skip the whole tenant file (YamlFileError, ValueError).
_SPEC_TENANT = '    t1:\n      mysql_connections: "70"\n'


def _cr_with_spec_extra(extra: str) -> str:
    return _cr("t1", _SPEC_TENANT) + extra


@pytest.mark.parametrize("extra", [
    "  defaults: 0\n", "  defaults: false\n", "  defaults: no\n",
    "  stateFilters: false\n", "  stateFilters: 0\n",
    "  defaults: 0\n  stateFilters: false\n",
], ids=["defaults-0", "defaults-false", "defaults-no", "stateFilters-false",
        "stateFilters-0", "both"])
def test_da_assembler_drops_a_scalar_spec_block(tmp_path, da_guard, extra):
    cr = tmp_path / "cr.yaml"
    cr.write_text(_cr_with_spec_extra(extra), encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    assert da_assembler.render_cr_file(cr, out) == 0
    (out / "_defaults.yaml").write_text("defaults:\n  mysql_connections: 1\n",
                                        encoding="utf-8")
    assert _served(out, da_guard)["t1"]["mysql_connections"] == 70.0
    got = _as_text(out / "t1.yaml")
    assert got == {"tenants": {"t1": {"mysql_connections": "70"}}}, got


@pytest.mark.parametrize("tenants", ["0", "false"])
def test_da_assembler_drops_a_scalar_tenants_block(tmp_path, tenants):
    cr = tmp_path / "cr.yaml"
    cr.write_text(_cr("t1", "").replace("  tenants:\n",
                                        f"  tenants: {tenants}\n"),
                  encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    assert da_assembler.render_cr_file(cr, out) == 0
    assert _as_text(out / "t1.yaml") == {}


def test_da_assembler_still_writes_mapping_spec_blocks(tmp_path, da_guard):
    """Control: a mapping `stateFilters` is written as before."""
    extra = ("  stateFilters:\n    container_crashloop:\n"
             "      reasons: [CrashLoopBackOff]\n      severity: critical\n")
    cr = tmp_path / "cr.yaml"
    cr.write_text(_cr_with_spec_extra(extra), encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    assert da_assembler.render_cr_file(cr, out) == 0
    got = _as_text(out / "t1.yaml")
    assert got["state_filters"] == {"container_crashloop": {
        "reasons": ["CrashLoopBackOff"], "severity": "critical"}}, got
    (out / "_defaults.yaml").write_text("defaults:\n  mysql_connections: 1\n",
                                        encoding="utf-8")
    assert _served(out, da_guard)["t1"]["mysql_connections"] == 70.0


# ── patch-config: the tenant patched is the tenant named (#2216, #2237) ──
#
# `_patch_carrier` rewrote the carrier from `yaml.safe_load(old)`: a lone
# `010:` / `8:` / `yes:` came back renamed and the write was refused (#2216,
# fail-closed — a numeric tenant could not be patched at all), and two keys
# the exporter reads as two tenants but PyYAML as one value (`8` and `010`)
# were folded into one, the other tenant overwritten (#2237). Keys are now
# read as source text; the whole carrier is still re-dumped, so the other
# tenant's block is compared as the exporter reads it.

_PC_BLOCK = "    connections: '1'\n    mem: '5'\n"
_PC_DEFAULTS = "defaults:\n  connections: 100\n  mem: 80\n"


def _pc_cm(mode: str, tenants_body: str) -> dict:
    """A ConfigMap whose one tenant carrier holds `tenants_body`: multi-file
    (`_defaults.yaml` + `t.yaml`) or legacy (one `config.yaml`)."""
    if mode == "legacy":
        return {"data": {"config.yaml": _PC_DEFAULTS + "tenants:\n" + tenants_body}}
    return {"data": {"_defaults.yaml": _PC_DEFAULTS,
                     "t.yaml": "tenants:\n" + tenants_body}}


def _pc_tenants(text: str) -> dict:
    return _lib_yaml_keys.load_exporter_keys(io.StringIO(text))["tenants"]


@pytest.mark.parametrize("mode", ["multi-file", "legacy"])
@pytest.mark.parametrize("row", _MATRIX_IDS, ids=lambda r: r["source"])
def test_patch_config_keeps_every_matrix_spelling(row, mode):
    """Every spelling of the shared Go/Python table: patched under the
    exporter's id, and read back as that id after the write."""
    tid = row["exporter_key"]
    cm = _pc_cm(mode, f"  {row['source']}:\n" + _PC_BLOCK)
    patch = pc.build_patch(cm, mode, tid, "connections", "3")
    assert patch is not None
    (key, text), = patch["data"].items()
    assert key == ("config.yaml" if mode == "legacy" else "t.yaml")
    assert _pc_tenants(text) == {tid: {"connections": "3", "mem": "5"}}, text


# Pairs the exporter reads as TWO tenants and PyYAML as one value.
_PC_COLLISIONS = [("8", "010"), ("true", "yes"), ("1", "true"),
                  ("16", "0x10"), ("750", "12:30")]


@pytest.mark.parametrize("patch_first", [True, False],
                         ids=["patch-first", "patch-second"])
@pytest.mark.parametrize("pair", _PC_COLLISIONS, ids="/".join)
def test_patch_config_leaves_the_colliding_tenant_alone(pair, patch_first):
    """#2237: `8:` and `010:` in one legacy `config.yaml`. Before: the write
    folded them and `8` got `010`'s block (the post-write check then rolled
    it back, rc 1). Now only the named tenant changes."""
    a, b = pair
    cm = _pc_cm("legacy", f"  {a}:\n    connections: '2'\n    mem: '50'\n"
                          f"  {b}:\n" + _PC_BLOCK)
    target, other = (a, b) if patch_first else (b, a)
    before = _pc_tenants(cm["data"]["config.yaml"])
    patch = pc.build_patch(cm, "legacy", target, "connections", "3")
    after = _pc_tenants(patch["data"]["config.yaml"])
    assert set(after) == {a, b}, after
    assert after[other] == before[other], after
    assert after[target] == {**before[target], "connections": "3"}, after


def test_patch_config_keeps_a_profile_outside_tenants_typed():
    """Only `tenants.<id>._profile` is read as text. The loader's opt-in
    matches the key name at every depth: `defaults: {_profile: 010}` came
    back `'010'`, and the exporter, which decodes `defaults:` as a float64
    map, then refused the whole config (review of eecc80f1). The tenant's
    own `_profile: 010` in the same file still stays text."""
    text = (_PC_DEFAULTS + "  _profile: 010\n"
            "tenants:\n  acme:\n    _profile: 010\n    connections: '1'\n")
    cm = _pc_cm("legacy", "")
    cm["data"]["config.yaml"] = text
    patch = pc.build_patch(cm, "legacy", "acme", "connections", "3")
    written = patch["data"]["config.yaml"]
    got = _lib_yaml_keys.load_exporter_keys(io.StringIO(written))
    assert got["defaults"] == {"connections": 100, "mem": 80, "_profile": 8}, written
    assert all(isinstance(v, (int, float)) for v in got["defaults"].values()), written
    # #2220: the tenant's `_profile: 010` is now written back plain, as it
    # was read (the write used to quote it), so this reads it the way the
    # exporter does — the scalar's text — instead of with PyYAML's typing,
    # which says 8 for the plain spelling. Same invariant: the reference is
    # still "010" while the `defaults:` one above stays a number.
    tenants = _lib_yaml_keys.load_exporter_keys(
        io.StringIO(written), raw_text_scalars=("_profile",))["tenants"]
    assert tenants == {"acme": {"_profile": "010", "connections": "3"}}, written


def test_patch_config_refuses_a_write_that_changes_the_declared_tenants(monkeypatch):
    """The post-rewrite set guard: with the re-read typed again (the reader
    before #2237), `010:` is written as `8:` beside a new `010:` — the key
    would declare {8, 010} where it declared {010}. Refused, nothing sent."""
    import yaml
    # #2220: the rewrite reads through `strict_load_for_rewrite` now (was
    # `strict_load_exporter_keys`); the typed stand-in is unchanged.
    monkeypatch.setattr(pc, "strict_load_for_rewrite",
                        lambda text, **_kw: yaml.safe_load(text))
    cm = _pc_cm("multi-file", "  010:\n" + _PC_BLOCK)
    with pytest.raises(pc.ConfigMapShapeError, match="tenants it declares"):
        pc.build_patch(cm, "multi-file", "010", "connections", "3")


def test_patch_config_keeps_a_profile_reference_bound(tmp_path, da_guard):
    """`_profile: 010` names profile `010:`. With only the KEY read as text
    the rewrite still wrote `_profile: 8` — the binding silently broken at
    rc 0 (#2237 step 0). Same shape as the deprecate_rule test above,
    including its #2220 change of oracle: `_profile: 010` is written back
    plain; the raw-text reader and da-guard's served `container_cpu` (55
    from profile `010`, not the default 80) pin the binding. (The Python
    readers' own agreement with that value: test_profile_ref_as_text.py.)"""
    defaults = ("defaults:\n  connections: 100\n  container_cpu: 80\n"
                'profiles:\n  010:\n    container_cpu: "55"\n')
    cm = {"data": {"_defaults.yaml": defaults,
                   "t.yaml": ("tenants:\n  acme:\n    _profile: 010\n"
                              "    connections: '1'\n")}}
    patch = pc.build_patch(cm, "multi-file", "acme", "connections", "3")
    written = patch["data"]["t.yaml"]
    d = _tree(tmp_path, {"_defaults.yaml": defaults, "t.yaml": written})
    assert _as_text_profiles(d / "t.yaml")["tenants"] == {
        "acme": {"_profile": "010", "connections": "3"}}, written
    assert _served(d, da_guard)["acme"]["container_cpu"] == 55.0


def _render_as_text(data, pod=None):
    """`_patch_config_fake.render_thresholds`, keyed by source text as the
    real exporter is (that toy reads with PyYAML typing, which would fold
    `8` and `010` into one tenant's series)."""
    lines = []
    for key in sorted(data):
        if key.startswith("_") or not data[key]:
            continue
        doc = _lib_yaml_keys.load_exporter_keys(io.StringIO(data[key])) or {}
        for tenant, block in sorted((doc.get("tenants") or {}).items()):
            for metric, value in sorted((block or {}).items()):
                if not metric.startswith("_"):
                    lines.append(f'user_threshold{{metric="{metric}",'
                                 f'tenant="{tenant}"}} {value}')
    return "\n".join(lines) + "\n"


def test_patch_config_apply_sends_one_patch_for_the_colliding_pair(capsys):
    """The #2237 fixture through `main()`'s apply path. Before: rc 1 and two
    patches (the write, then the rollback the post-write check forced)."""
    data = {"config.yaml": _PC_DEFAULTS + (
        "tenants:\n  8:\n    connections: '2'\n    mem: '50'\n"
        "  010:\n" + _PC_BLOCK)}
    cluster = FakeCluster(data, render=_render_as_text, mode="single-file")
    argv = ["patch_config.py", "010", "connections", "3",
            "--poll-interval", "0.001", "--reload-timeout", "0.05"]
    try:
        with mock.patch("patch_config.run_cmd", side_effect=cluster), \
                mock.patch("sys.argv", argv):
            try:
                pc.main()
                code = 0
            except SystemExit as exc:
                code = exc.code
    finally:
        pc.SIGNALS.reset()  # apply keeps its signal handlers otherwise
    assert code == 0, capsys.readouterr()
    assert len(cluster.patches) == 1, cluster.patches
    assert _pc_tenants(cluster.data["config.yaml"]) == {
        "8": {"connections": "2", "mem": "50"},
        "010": {"connections": "3", "mem": "5"}}
