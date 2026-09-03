#!/usr/bin/env python3
"""test_generate_tenant_metadata — the extension-SPELLING axis (#1603).

`build_tenant_metadata` feeds `generate_platform_data`, i.e. the portal's
tenant list. Both of its selection sites used to pass `suffixes=(".yaml",)`
while the exporter's scanner (`config_hierarchy.go:195`) lowercases the entry
name and accepts BOTH `.yaml` and `.yml` — so a tenant declared in `db-a.yml`
was served by the exporter and did not exist for the portal. Measured on two
trees whose contents are byte-identical and differ only in the extension:

    db-a.yaml -> 1 tenant,  rc=0, stderr 0 bytes    <- control
    db-a.yml  -> 0 tenants, rc=0, stderr 0 bytes    <- before
    db-a.yml  -> 1 tenant,  rc=0, stderr 0 bytes    <- after

⚠️ SCOPE. These pin the extension-SPELLING axis only. The module's other
divergences are not covered here, and no open ticket covers them EITHER —
read the per-axis notes below rather than the ticket numbers:

  * Recursion: this reader is flat (`config_dir.iterdir()`) and says so out
    loud — `warn_nested` prints the nested files it cannot see. That is
    `test_confd_enumeration_contract.py`'s axis.
  * Hidden names: dot-prefixed carriers reach the loop below (the module
    imports `is_reserved_name` but not `is_hidden_name`) while the exporter
    skips them (`config_hierarchy.go:181,190`). Pre-existing and unchanged
    here; closing it DELETES tenants that appear today, so it is a separate
    behaviour change.
    ⚠️ #1339 (the family ticket) is closed. #1589 IS open on the hidden
    axis, but its subject is the exporter's `pkg/config` enumerator and the
    path-vs-basename distinction — it does not cover a Python reader
    counting `.hidden.yaml` as a tenant, so this disclosure still has to
    carry itself.
  * Entries `is_file()` drops are named on stderr here — that half of #1607
    IS wired up in this module, unlike `gitops_check`.
"""

from __future__ import annotations

import json
from pathlib import Path

import generate_tenant_metadata as gtm  # noqa: E402

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
#                        (`generate_tenant_metadata.py:243`). Blind review
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

    def test_an_unreadable_yml_carrier_is_named_not_skipped(
            self, tmp_path, capsys):
        """FLOOR, second site: `unusable_config_entries` must see `.yml` too.

        The equality above only exercises the `has_yaml_extension` site. This
        one covers the other one — reverting it alone leaves the equality
        green, which is how the same gap survived the first round of the
        sibling fix (#1663).

        ⛔ The unreadable carrier is a DIRECTORY named like a config file,
        not a broken symlink: symlink creation needs administrator rights on
        Windows, so a symlink fixture would be skipped on the very host most
        of this repo's maintainers use.

        ⛔ The assertion names the REASON, not just the filename, and that is
        load-bearing. This module has a SECOND stderr station that prints the
        same basename — the `except Exception` around `yaml.safe_load` in the
        loop below. Blind review combined "revert this site" with "drop the
        `is_file()` filter" and the filename-only assertion stayed green: the
        directory reached `open()`, raised `OSError`, and the parse-failure
        handler printed the very string being asserted on. Pinning
        `unusable_reason`'s own words tells the two stations apart.

        ⚠️ Only the REASON fragment is pinned, not the whole line: the
        surrounding wording is deliberately not pinned anywhere (measured —
        rewording it turns nothing red), and pinning a prefix here would
        quietly make this the module's message-format test as well.
        """
        for ext in (".yaml", ".yml"):
            root = _seed(tmp_path / ext.lstrip("."), ext)
            (root / f"db-broken{ext}").mkdir()

            gtm.build_tenant_metadata(root)
            err = capsys.readouterr().err

            named = [ln for ln in err.splitlines()
                     if f"db-broken{ext}" in ln
                     and "is a directory, not a config file" in ln]
            assert len(named) == 1, (
                f"the `unusable` report must name `db-broken{ext}` exactly "
                f"once, with the reason it could not be used; stderr was "
                f"{err!r}"
            )

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
