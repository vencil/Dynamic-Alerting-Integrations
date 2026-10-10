#!/usr/bin/env python3
"""test_generate_tenant_metadata — the extension-SPELLING axis (#1603).

`build_tenant_metadata` feeds `generate_platform_data`, i.e. the portal's
tenant list. Both of its selection sites used to pass `suffixes=(".yaml",)`
while the exporter's scanner (`scanDirHierarchical`, `config_hierarchy.go`) lowercases the entry
name and accepts BOTH `.yaml` and `.yml` — so a tenant declared in `db-a.yml`
was served by the exporter and did not exist for the portal. Measured on two
trees whose contents are byte-identical and differ only in the extension:

    db-a.yaml -> 1 tenant,  rc=0, stderr 0 bytes    <- control
    db-a.yml  -> 0 tenants, rc=0, stderr 0 bytes    <- before
    db-a.yml  -> 1 tenant,  rc=0, stderr 0 bytes    <- after

⚠️ SCOPE. Since #2115 0-B/B4 the tenants come from `da-guard served-values`,
so the walk (spelling, hidden names, recursion, unusable entries) is the
exporter's own; these tests keep pinning that the answer agrees with it on
the spelling and hidden-name axes. The four-position semantics (defaults,
platform `tenants:`, tenant file, subtree) are
`tests/shared/test_served_values_tenant_metadata_matrix.py`'s.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import generate_tenant_metadata as gtm  # noqa: E402

# #2115 0-B/B4: the tool reads the tenants through da-guard served-values.
pytestmark = pytest.mark.usefixtures("da_guard_env")

_DEFAULTS = "defaults:\n  mysql_connections: 100\n"


def _tenant(tid: str, *, domain: str, keys: int) -> str:
    body = "".join(f"    mysql_metric_{i}: {i}\n" for i in range(keys))
    return (
        "tenants:\n"
        f"  {tid}:\n"
        f"{body}"
        "    _metadata:\n"
        f"      domain: {domain}\n"
        "      environment: prod\n"
        "      owner: sre\n"
    )


# Keys whose value does not depend on the conf.d under test, so comparing
# them across two trees tests the environment instead of the subject.
#
#   `generated`          top-level wall clock. The tool itself pops this key
#                        before comparing in both its `--check` paths, so
#                        dropping it is its own convention, not a new one.
#   `last_config_commit` per tenant, from `git rev-parse HEAD` with
#                        `timeout=5` and a SILENT `""` fallback
#                        (`get_git_head_commit`). Blind review
#                        injected that fallback on one of the four calls this
#                        test makes: the equality broke and reported
#                        "`.yaml` and `.yml` produce different portal tenant
#                        lists" — the wrong axis, with the vacuity guards all
#                        green so nothing warned the reader. It is a second
#                        wall clock that simply was not named as one.
#
# ⛔ This list is for values INDEPENDENT of the tree, and that is the only
# admissible reason to grow it. Adding a key because a legitimate change made
# the equality red is how this oracle gets hollowed out one key at a time —
# which is why the concrete counts below are asserted separately.
_TREE_INDEPENDENT_KEYS = ("generated", "last_config_commit")


def _comparable(meta: dict) -> str:
    """Everything the generator produced, minus the values that are not
    functions of the conf.d it read."""
    def scrub(o):
        if isinstance(o, dict):
            return {k: scrub(v) for k, v in o.items()
                    if k not in _TREE_INDEPENDENT_KEYS}
        if isinstance(o, list):
            return [scrub(v) for v in o]
        return o

    return json.dumps(scrub(meta), sort_keys=True, ensure_ascii=False)


def _seed(root: Path, ext: str) -> Path:
    """One conf.d whose every carrier uses `ext`. Bodies never change."""
    root.mkdir(parents=True, exist_ok=True)
    (root / f"_defaults{ext}").write_text(_DEFAULTS, encoding="utf-8")
    (root / f"db-a{ext}").write_text(
        _tenant("db-a", domain="payments", keys=2), encoding="utf-8")
    (root / f"db-b{ext}").write_text(
        _tenant("db-b", domain="search", keys=1), encoding="utf-8")
    return root


class TestExtensionSpellingAxis:
    """`.yml` carriers must produce exactly what `.yaml` ones do — no more."""

    def test_metadata_agrees_across_extension_spellings(self, tmp_path):
        """FLOOR. Two trees, same bytes, different extension → same answer.

        ⛔ Asserted as an EQUALITY between the two runs rather than as
        "`.yml` is accepted". The second is satisfied by a reader that takes
        `.yml` and drops `.yaml`, and it stops meaning anything the day the
        exporter grows a third spelling; the equality keeps saying the right
        thing in both cases.
        """
        a = gtm.build_tenant_metadata(_seed(tmp_path / "yaml", ".yaml"))
        b = gtm.build_tenant_metadata(_seed(tmp_path / "yml", ".yml"))

        # ⛔ Vacuity guard FIRST, and on BOTH sides: two trees this cannot
        # read at all would also compare equal. Pinned as concrete counts
        # rather than "non-empty" so gutting `_comparable` cannot silently
        # disarm the test — that erosion path was measured on the sibling
        # fix (#1672) and it left the equality green.
        for label, meta in (("`.yaml`", a), ("`.yml`", b)):
            assert sorted(meta["tenant_metadata"]) == ["db-a", "db-b"], (
                f"the {label} tree produced tenants "
                f"{sorted(meta.get('tenant_metadata', {}))}, not "
                f"['db-a', 'db-b'] — the equality below would prove nothing"
            )
            assert sorted(meta["dimension_groups"]["by_domain"]) == [
                "payments", "search"], (
                f"the {label} tree lost a domain grouping: "
                f"{meta['dimension_groups']['by_domain']}"
            )

        assert _comparable(a) == _comparable(b), (
            f"generate_tenant_metadata builds a different portal tenant list "
            f"for a conf.d whose only difference is `.yaml` vs `.yml`, both "
            f"of which the exporter serves:\n"
            f"  .yaml: {_comparable(a)}\n  .yml : {_comparable(b)}"
        )

    def test_a_directory_named_like_a_carrier_is_what_the_exporter_makes_of_it(
            self, tmp_path, capsys):
        """A directory named `db-broken.yaml`: the exporter's walk descends
        into it (a sub-directory, here empty) and serves no tenant from it.
        Before #2115 0-B/B4 this tool named it on stderr from its own scan;
        now the walk is da-guard's, so the answer is pinned against it, for
        both spellings."""
        import _lib_tenant_values as tv  # noqa: PLC0415

        for ext in (".yaml", ".yml"):
            root = _seed(tmp_path / ext.lstrip("."), ext)
            (root / f"db-broken{ext}").mkdir()

            meta = gtm.build_tenant_metadata(root)
            assert sorted(meta["tenant_metadata"]) == sorted(tv.load_served_values(root)) == [
                "db-a", "db-b"], meta["tenant_metadata"]

    def test_a_json_carrier_is_not_a_tenant(self, tmp_path):
        """CEILING, by counterexample — that is all a ceiling can be.

        ⛔ You cannot enumerate the complement of an accept-set, so this pins
        counterexamples rather than a rule, and the name says which ones.
        Over-widening a call site to `(".yaml", ".yml", ".json")` is a single
        token, and `has_yaml_extension`'s own docstring says this argument
        gets touched. Measured on this change's base (`eed193a7`): with that
        widening applied and this file's tests absent, the CI-exact suite is
        15909 passed / 185 skipped / rc=0 — nothing else is watching this
        direction.

        ⛔ It does NOT cover dot-prefixed names — this reader counts them and
        the exporter skips them (see the SCOPE block above), so the fixture
        deliberately contains no dot-prefixed name rather than pinning
        today's answer for it.
        """
        root = _seed(tmp_path / "confd", ".yaml")
        # A carrier only an over-wide reader can see, declaring a tenant no
        # other file in the tree declares. ⛔ The tenant id comes from the
        # file CONTENT, not the filename, so asserting on the id is the only
        # thing that actually detects the widening.
        (root / "db-json.json").write_text(
            json.dumps({"tenants": {"db-json": {
                "mysql_metric_0": 0,
                "_metadata": {"domain": "ghost", "environment": "prod"},
            }}}), encoding="utf-8")
        # `_`-prefixed control file — never a tenant carrier whatever the
        # extension.
        (root / "_profiles.yaml").write_text(
            _tenant("db-reserved", domain="ghost", keys=1), encoding="utf-8")

        meta = gtm.build_tenant_metadata(root)

        assert sorted(meta["tenant_metadata"]) == ["db-a", "db-b"], (
            f"a carrier the exporter does not serve reached the portal's "
            f"tenant list: {sorted(meta['tenant_metadata'])}"
        )
        assert "ghost" not in meta["dimension_groups"]["by_domain"], (
            f"a domain grouping was built from a carrier the exporter does "
            f"not serve: {meta['dimension_groups']['by_domain']}"
        )

    def test_every_spelling_the_shared_set_names_is_read(self, tmp_path):
        """FLOOR, derived: one carrier per member of `CONFIG_SUFFIXES`.

        The tests above hard-code `.yaml` and `.yml`, so they stop covering
        the floor the day the exporter grows a third spelling and
        `_lib_confd.CONFIG_SUFFIXES` follows it. This one reads the set
        instead of restating it.

        ⛔ It does NOT re-implement `has_yaml_extension` — that would be the
        predicate checking itself. It uses the shared CONSTANT, whose
        agreement with the exporter's scanner is pinned by
        `tests/shared/confd_name_classification_matrix.json` (asserted from
        the Go side by `confd_name_classification_parity_test.go` and from
        the Python side by
        `tests/shared/test_confd_name_classification_parity.py`).

        ⚠️ Floor only: satisfied by a reader that is too WIDE, which is what
        the counterexample test above is for.
        """
        from _lib_confd import CONFIG_SUFFIXES  # noqa: PLC0415

        # ⛔ Anti-vacuity: an empty set would make the assertion below
        # `[] == []`. Guarding the set is not this test's job, so name who
        # does rather than pretending a floor here would be independent.
        assert len(CONFIG_SUFFIXES) >= 2, (
            f"CONFIG_SUFFIXES collapsed to {CONFIG_SUFFIXES!r}; the shared "
            f"classification matrix should have gone red first"
        )

        root = tmp_path / "confd"
        root.mkdir()
        (root / "_defaults.yaml").write_text(_DEFAULTS, encoding="utf-8")
        for i, suffix in enumerate(CONFIG_SUFFIXES):
            (root / f"t{i}{suffix}").write_text(
                _tenant(f"t{i}", domain=f"d{i}", keys=1), encoding="utf-8")

        meta = gtm.build_tenant_metadata(root)
        expected = sorted(f"t{i}" for i in range(len(CONFIG_SUFFIXES)))

        assert sorted(meta["tenant_metadata"]) == expected, (
            f"one carrier was written per member of CONFIG_SUFFIXES "
            f"({CONFIG_SUFFIXES!r}) and the generator produced "
            f"{sorted(meta['tenant_metadata'])}, expected {expected}"
        )


def _owned(tid: str, owner: str) -> str:
    return (
        "tenants:\n"
        f"  {tid}:\n"
        "    mysql_connections: 70\n"
        "    _metadata:\n"
        f"      owner: {owner}\n"
    )


class TestHiddenEntriesAxis:
    """#2055: `.`-prefixed names are skipped by the exporter's walker, so the
    portal's tenant list must not read them either.

    ⛔ Every test pairs the hidden fixture with a CONTROL whose body is
    byte-identical and whose name is not hidden. Without it, "the tenant is
    absent" is equally satisfied by a reader that reads nothing at all.
    """

    def test_a_hidden_carrier_declares_no_tenant(self, tmp_path):
        body = _owned("ghost", "ghost-team")

        control = _seed(tmp_path / "control", ".yaml")
        (control / "ghost.yaml").write_text(body, encoding="utf-8")
        assert "ghost" in gtm.build_tenant_metadata(control)["tenant_metadata"]

        root = _seed(tmp_path / "confd", ".yaml")
        (root / ".hidden.yaml").write_text(body, encoding="utf-8")
        tenants = gtm.build_tenant_metadata(root)["tenant_metadata"]

        assert sorted(tenants) == ["db-a", "db-b"], (
            f"`.hidden.yaml` is invisible to the exporter but its tenant "
            f"reached the portal list: {sorted(tenants)}"
        )

    def test_a_later_sorting_hidden_file_cannot_overwrite_a_tenant(
            self, tmp_path):
        # `-` (0x2d) sorts before `.` (0x2e), so `.zz.yaml` would be read LAST.
        assert sorted(["-acme.yaml", ".zz.yaml"]) == ["-acme.yaml", ".zz.yaml"]
        import _lib_tenant_values as tv  # noqa: PLC0415

        # Control: the same tenant in two files the exporter reads is a tree
        # it refuses (#2115 0-B/B4: fail-closed, no "last file wins").
        control = tmp_path / "control"
        control.mkdir()
        (control / "-acme.yaml").write_text(
            _owned("acme", "real-team"), encoding="utf-8")
        (control / "zz.yaml").write_text(
            _owned("acme", "ghost-team"), encoding="utf-8")
        with pytest.raises(tv.ServedValuesError, match="duplicate tenant"):
            gtm.build_tenant_metadata(control)

        root = tmp_path / "confd"
        root.mkdir()
        (root / "-acme.yaml").write_text(
            _owned("acme", "real-team"), encoding="utf-8")
        (root / ".zz.yaml").write_text(
            _owned("acme", "ghost-team"), encoding="utf-8")
        got = gtm.build_tenant_metadata(root)["tenant_metadata"]

        assert got["acme"]["owner"] == "real-team", (
            f"hidden `.zz.yaml` overwrote the real tenant's owner: {got}"
        )

    def test_hidden_broken_entries_raise_no_warning(self, tmp_path, capsys):
        """A broken file the exporter reads fails the run (rc 2 at the CLI);
        the same bytes under a hidden name are never read: no error, no
        warning, the tenants as before."""
        import _lib_tenant_values as tv  # noqa: PLC0415
        bad_yaml = "tenants: [unclosed\n"

        control = _seed(tmp_path / "control", ".yaml")
        (control / "broken.yaml").write_text(bad_yaml, encoding="utf-8")
        with pytest.raises(tv.ParseFailedError, match="broken.yaml"):
            gtm.build_tenant_metadata(control)
        capsys.readouterr()

        root = _seed(tmp_path / "confd", ".yaml")
        (root / ".broken-dir.yaml").mkdir()
        (root / ".broken.yaml").write_text(bad_yaml, encoding="utf-8")
        meta = gtm.build_tenant_metadata(root)
        err = capsys.readouterr().err

        assert "WARN" not in err and "broken" not in err, (
            f"a hidden entry the exporter never reads produced a warning: "
            f"{err!r}"
        )
        assert sorted(meta["tenant_metadata"]) == ["db-a", "db-b"]


class TestPagerdutyRoutingChannel:
    """#2137：routing_key-only 的 pagerduty 現在合法；卡片標出 API 版本，不輸出 key 本身。"""

    def _channel(self, receiver):
        return gtm.extract_routing_channel({"_routing": {"receiver": receiver}})

    def test_routing_key_only_is_named_v2(self):
        assert self._channel({"type": "pagerduty", "routing_key": "secret-r"}) == "pagerduty:v2"

    def test_service_key_is_named_v1(self):
        assert self._channel({"type": "pagerduty", "service_key": "secret-k"}) == "pagerduty:v1"

    def test_empty_key_counts_as_unset(self):
        assert self._channel({"type": "pagerduty", "service_key": "", "routing_key": "r"}) == "pagerduty:v2"
        assert self._channel({"type": "pagerduty"}) == ""

    def test_presence_is_the_pipelines_type_rule(self):
        """A non-string key is a config error the pipeline rejects: no v1/v2 label."""
        for bad in (0, False, [], {}):
            assert self._channel({"type": "pagerduty", "service_key": bad, "routing_key": "r"}) == ""
            assert self._channel({"type": "pagerduty", "routing_key": bad}) == ""
        assert self._channel({"type": "pagerduty", "service_key": None, "routing_key": "r"}) == "pagerduty:v2"
        assert self._channel({"type": "pagerduty", "service_key": " "}) == "pagerduty:v1"

    def test_key_value_never_appears(self):
        for recv in ({"type": "pagerduty", "service_key": "secret-k"},
                     {"type": "pagerduty", "routing_key": "secret-r"}):
            assert "secret" not in self._channel(recv)


class TestEnvironmentIsNotInferredFromTheName:
    """#2830: `environment` is the one the exporter reads from `_metadata`
    (tenant_metadata_info, tenant-api's list/search), never one guessed from
    the tenant name — the portal falls back to this file when tenant-api is
    down, so a guess would show the tenant in two environments. Same rule as
    db_type (#2115 B4)."""

    def test_a_name_pattern_sets_no_environment(self, tmp_path):
        root = tmp_path / "conf.d"
        root.mkdir()
        (root / "_defaults.yaml").write_text(_DEFAULTS, encoding="utf-8")
        for tid in ("prod-x", "staging-y", "dev-z"):
            (root / f"{tid}.yaml").write_text(
                f"tenants:\n  {tid}:\n    _metadata:\n      owner: sre\n", encoding="utf-8")
        (root / "t4.yaml").write_text(
            "tenants:\n  t4:\n    _metadata:\n      environment: production\n", encoding="utf-8")
        meta = gtm.build_tenant_metadata(root)
        envs = {t: m["environment"] for t, m in meta["tenant_metadata"].items()}
        assert envs == {"prod-x": "", "staging-y": "", "dev-z": "", "t4": "production"}
        grouped = {t for g in meta["tenant_groups"].values() for t in g["tenants"]}
        assert grouped == {"t4"}, meta["tenant_groups"]
