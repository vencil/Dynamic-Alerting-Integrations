"""#2315: one tenant id declared by two tenant files is refused by the generator.

The threshold-exporter's walker rejects such a tree WHOLE
(``*DuplicateTenantError``), and so does da-guard — but da-guard does not run
on a change to a tenant file, and the generator merged the two blocks key by
key and routed on whichever file sorted last. Measured before this, on a
conf.d with ``t-a.yaml`` and ``t-a2.yaml`` both declaring ``t-a``:
``--validate`` / ``--validate --strict --policy`` / render /
``--output-configmap`` all rc 0.

The contract pinned here (owner decision on #2315 (c)), with amtool NOT on
PATH so no verdict can be amtool's:

* two tenant files declaring one id → rc 1 in every mode, with or without
  ``--strict``, naming both files, nothing written;
* validate-config's output is unchanged: its ``tenant_uniqueness`` row runs
  the same scan and already FAILs on this state;
* "declares" is validate-config's ``tenant_uniqueness`` definition, shared
  (``_lib_confd.tenant_declarations``): any non-``_`` file, at any depth,
  keyed by the ``tenants:`` key text (not the file name) — so ``x.yaml`` +
  ``x.yml`` and a root + nested pair are duplicates, while a ``_``-prefixed
  platform file's ``tenants.<id>`` overlay is NOT a second declaration.

Tenant ids are fixture names (CLAUDE.md #9).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from factories import make_routing_config, make_tenant_yaml

import _grar_validate as gv
import generate_alertmanager_routes as gar
import validate_config as vc

_GAR = (Path(__file__).resolve().parents[2] / "scripts" / "tools" / "ops"
        / "generate_alertmanager_routes.py")

_T = "t1"


def _tenant_body(tenant: str, url: str = "https://hooks.example.com/x") -> str:
    return make_tenant_yaml(
        tenant, keys={"mysql_connections": "70"},
        routing=make_routing_config(tenant, url=url))


_DEFAULTS = "defaults:\n  mysql_connections: 80\n"


def _conf(tmp_path: Path, files: dict[str, str]) -> Path:
    d = tmp_path / "conf.d"
    for rel, body in {"_defaults.yaml": _DEFAULTS, **files}.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return d


# shape → the files, and the labels the refusal must name
_SHAPES = {
    "two-root-files": (
        {f"{_T}.yaml": _tenant_body(_T, "https://hooks.example.com/a"),
         f"{_T}-copy.yaml": _tenant_body(_T, "https://hooks.example.com/b")},
        [f"{_T}-copy.yaml", f"{_T}.yaml"]),
    "yaml-and-yml": (
        {f"{_T}.yaml": _tenant_body(_T),
         f"{_T}.yml": _tenant_body(_T)},
        [f"{_T}.yaml", f"{_T}.yml"]),
    "root-and-nested": (
        {f"{_T}.yaml": _tenant_body(_T),
         f"team/{_T}.yaml": _tenant_body(_T)},
        [f"{_T}.yaml", f"team/{_T}.yaml"]),
}


@pytest.fixture(params=sorted(_SHAPES))
def dup(request, tmp_path):
    files, labels = _SHAPES[request.param]
    return _conf(tmp_path, files), labels


def _gar(argv, tmp_path):
    empty = tmp_path / "empty-path"
    empty.mkdir(exist_ok=True)
    env = dict(os.environ, PATH=str(empty))
    r = subprocess.run([sys.executable, "-s", str(_GAR), *argv],
                       capture_output=True, timeout=180, env=env)
    return r.returncode, r.stdout.decode("utf-8", "replace"), \
        r.stderr.decode("utf-8", "replace")


class TestDuplicateTenantIsRefused:

    def test_tree_carries_the_record_and_one_line_names_every_file(self, dup):
        d, labels = dup
        tree = gar.load_tenant_tree(str(d))
        assert tree.duplicate_tenants == {_T: labels}
        lines = gv.duplicate_tenant_errors(tree.duplicate_tenants)
        assert len(lines) == 1, lines
        assert f"tenant '{_T}'" in lines[0], lines[0]
        assert f"{len(labels)} files: {', '.join(labels)}" in lines[0], lines[0]
        # Not a warning-stream line: validate-config's rows stay as on main.
        assert not [w for w in tree.schema_warnings
                    if gv.DUPLICATE_TENANT_PREFIX in w], tree.schema_warnings

    @pytest.mark.parametrize("extra", [[], ["--strict"]], ids=["plain", "strict"])
    def test_validate_exits_1(self, dup, tmp_path, extra):
        d, labels = dup
        rc, out, err = _gar(["--config-dir", str(d), "--validate", *extra],
                            tmp_path)
        assert rc == 1, err
        assert "OK: all configs valid" not in out
        for label in labels:
            assert label in err, err

    @pytest.mark.parametrize("mode", [[], ["--output-configmap"]],
                             ids=["render", "output-configmap"])
    @pytest.mark.parametrize("strict", [False, True], ids=["plain", "strict"])
    def test_write_modes_exit_1_and_write_nothing(self, dup, tmp_path, mode,
                                                   strict):
        d, _labels = dup
        out = tmp_path / "out.yaml"
        argv = ["--config-dir", str(d), *mode, "-o", str(out)]
        if strict:
            argv.append("--strict")
        rc, _out, err = _gar(argv, tmp_path)
        assert rc == 1, err
        assert not out.exists(), "routes were written for a tree the exporter rejects"
        assert gv.DUPLICATE_TENANT_PREFIX in err, err

    def test_validate_config_reports_it_through_tenant_uniqueness(self, dup):
        d, _labels = dup
        assert vc.check_tenant_uniqueness(str(d))["status"] == vc.FAIL


class TestNotADuplicate:
    """Shapes that must stay green: each asserts rc 0 AND no duplicate line."""

    def _assert_clean(self, d: Path, tmp_path: Path) -> None:
        tree = gar.load_tenant_tree(str(d))
        assert tree.duplicate_tenants == {}
        rc, out, err = _gar(["--config-dir", str(d), "--validate"], tmp_path)
        assert rc == 0, err
        assert "OK: all configs valid" in out

    def test_platform_overlay_is_not_a_second_declaration(self, tmp_path):
        """`_platform.yaml`'s `tenants.<id>` is the platform's per-tenant
        default for a tenant that exists elsewhere, not a declaration."""
        d = _conf(tmp_path, {
            "_platform.yaml": f"tenants:\n  {_T}:\n    _severity_dedup: disable\n",
            f"{_T}.yaml": _tenant_body(_T)})
        self._assert_clean(d, tmp_path)

    def test_distinct_tenants_in_one_and_in_several_files(self, tmp_path):
        both = make_tenant_yaml("t2", keys={"mysql_connections": "70"},
                                routing=make_routing_config("t2"))
        both += make_tenant_yaml("t3", keys={"mysql_connections": "70"},
                                 routing=make_routing_config("t3")
                                 ).replace("tenants:\n", "", 1)
        d = _conf(tmp_path, {f"{_T}.yaml": _tenant_body(_T), "pair.yaml": both})
        tree = gar.load_tenant_tree(str(d))
        assert set(tree.routing_configs) == {_T, "t2", "t3"}
        self._assert_clean(d, tmp_path)

    def test_file_name_does_not_declare(self, tmp_path):
        """The id is the `tenants:` key, not the file stem: `t2.yaml`
        declaring t1 next to nothing else is one declaration."""
        d = _conf(tmp_path, {"t2.yaml": _tenant_body(_T)})
        self._assert_clean(d, tmp_path)


class TestSharedPredicate:

    def test_generator_and_validate_config_share_one_scan(self):
        """#2315 round 4: ONE definition of "which files declare a tenant"."""
        import inspect
        import _grar_parse
        assert "tenant_declarations(" in inspect.getsource(
            _grar_parse._parse_config_files)
        assert "tenant_declarations(" in inspect.getsource(
            vc.check_tenant_uniqueness)

    @pytest.mark.parametrize("second", [
        f"tenants:\n  {_T}:\n    mysql_connections: \"70\"\n",
        # The exporter cannot decode this file (da-guard exit 3); both tools
        # still count it as a declaration — the line says to fix it first.
        f"tenants:\n  {_T}: [1]\n",
    ], ids=["decodable", "exporter-cannot-decode"])
    def test_the_two_verdicts_agree(self, tmp_path, second):
        d = _conf(tmp_path, {f"{_T}.yaml": _tenant_body(_T),
                             f"{_T}-2.yaml": second})
        tree = gar.load_tenant_tree(str(d))
        lines = gv.duplicate_tenant_errors(tree.duplicate_tenants)
        assert len(lines) == 1, lines
        assert "da-guard exit 3" in lines[0], lines[0]
        assert vc.check_tenant_uniqueness(str(d))["status"] == vc.FAIL

    def test_duplicate_line_is_not_a_policy_error(self):
        """Blocks WITHOUT --strict; the policy prefix means "escalated by
        --strict", so the two must not overlap."""
        line = gv.duplicate_tenant_errors({"x": ["a.yaml", "b.yaml"]})[0]
        assert not line.lstrip().startswith(gv.POLICY_ERROR_PREFIX)
