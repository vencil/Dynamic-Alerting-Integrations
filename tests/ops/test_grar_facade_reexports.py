"""Facade re-export guard for generate_alertmanager_routes.py.

v2.8.0 PR-3a split the 1645-line monolith into 5 ``_grar_*`` helper modules
and left ``generate_alertmanager_routes.py`` as a CLI facade that re-exports
every public + private symbol so existing test imports keep working. The
300+ test functions across tests/ops/ cover ~1972 helper LOC THROUGH that
facade — if a re-export silently drops out (or gets shadowed by a same-name
def), the coverage evaporates without any test failing.

This guard pins the module → symbol map as a constant and asserts two
directions of drift:

* Forward (identity): ``facade.<name> is source_module.<name>`` — a stricter
  check than ``hasattr``; a re-bound, copied, or shadowed symbol fails and
  the parametrize id names exactly which symbol broke.
* Reverse (completeness): the set of ``# noqa: ... F401``-marked imports in
  the facade source (parsed via ``ast``) must equal the pinned map — adding
  a new re-export without registering it here fails, closing the
  "re-export added but never guarded" one-way-drift hole; a pinned entry
  that vanishes from the facade also fails with its name.

sys.path for ``generate_alertmanager_routes`` / ``_grar_*`` imports is
provided by tests/conftest.py (scripts/tools + scripts/tools/ops).

Convention the reverse check relies on: the ``noqa: ... F401`` marker must
sit on the ``from`` line itself (the facade's existing style). A marker on
a closing-paren line is not attributed to the import node, and that
re-export would escape the completeness check.
"""
import ast
import importlib
from pathlib import Path

import pytest

import generate_alertmanager_routes as facade


# ── Pinned re-export map (module → symbols) ──────────────────────────
# Deliberately spelled out symbol-by-symbol instead of introspected: the
# whole point is that drift in the facade must fail AGAINST this list,
# not silently follow it.
EXPECTED_REEXPORTS: dict[str, tuple[str, ...]] = {
    "_lib_python": (
        "write_text_secure",
        "PLATFORM_DEFAULTS",
    ),
    "_grar_validate": (
        "POLICY_ERROR_PREFIX",  # ADR-007 --strict blocking-prefix SSOT
        "_extract_host",
        "_validate_profile_refs",
        "assert_equal_labels_gated",  # #1132 equal-label-gated invariant
        "assert_watchdog_inhibit_immunity",
        "check_domain_policies",
        "find_ungated_equal_label_inhibits",  # #1132 finder
        "find_watchdog_suppressing_inhibits",
        "load_policy",
        "validate_receiver_domains",
        "validate_tenant_keys",
    ),
    "_grar_merge": (
        "_apply_timing_params",
        "_contains_tenant_placeholder",
        "_substitute_tenant",
        "build_receiver_config",
        "merge_routing_with_defaults",
    ),
    "_grar_parse": (
        "_merge_tenant_routing",
        "_parse_config_files",
        "_parse_platform_config",
        "_parse_tenant_overrides",
        "load_tenant_configs",
    ),
    "_grar_routes": (
        "_build_enforced_routes",
        "_build_inhibit_rules",
        "_build_override_matchers",
        "_build_override_route",
        "_build_custom_alert_routes",
        "_build_watchdog_route",
        "_build_synthetic_probe_route",
        "_build_sentinel_sinkhole_route",
        "_build_per_tenant_enforced_route",
        "_build_single_enforced_route",
        "_build_tenant_routes",
        "_process_override_receiver",
        "_validate_override_matcher",
        "expand_routing_overrides",
        "generate_inhibit_rules",
        "generate_routes",
    ),
    "_grar_render": (
        "_apply_merged_configmap",
        "_merge_routes_receivers_inhibits",
        "_read_existing_configmap",
        "_reload_alertmanager",
        "apply_to_configmap",
        "assemble_configmap",
        "load_base_config",
        "render_output",
    ),
}

_ALL_PAIRS = [
    (module_name, symbol)
    for module_name, symbols in EXPECTED_REEXPORTS.items()
    for symbol in symbols
]


# ── Forward: identity per symbol ─────────────────────────────────────
class TestFacadeReexportIdentity:
    """facade.<sym> must be the SAME object as source_module.<sym>."""

    @pytest.mark.parametrize(
        "module_name,symbol",
        _ALL_PAIRS,
        ids=[f"{m}.{s}" for m, s in _ALL_PAIRS],
    )
    def test_symbol_is_same_object(self, module_name, symbol):
        source = importlib.import_module(module_name)
        assert hasattr(facade, symbol), (
            f"facade lost re-export: {symbol!r} (from {module_name}) is no "
            f"longer an attribute of generate_alertmanager_routes — tests "
            f"importing it through the facade would stop covering "
            f"{module_name}"
        )
        assert getattr(facade, symbol) is getattr(source, symbol), (
            f"facade.{symbol} is not {module_name}.{symbol}: the facade "
            f"attribute exists but is a DIFFERENT object (shadowed or "
            f"re-bound) — facade-routed tests no longer exercise the "
            f"helper-module implementation"
        )


# ── Reverse: F401 re-export set ⇔ pinned map ─────────────────────────
def _f401_reexports_in_facade_source() -> set[tuple[str, str]]:
    """Parse the facade source; collect (module, name) for every import
    whose ``from`` line carries an F401 noqa marker (= re-export marker)."""
    source = Path(facade.__file__).read_text(encoding="utf-8")
    lines = source.splitlines()
    pairs = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            # The `# noqa: ..., F401` comment sits on the `from X import (`
            # header line, which is the ImportFrom node's lineno.
            if "F401" in lines[node.lineno - 1]:
                for alias in node.names:
                    pairs.add((node.module, alias.asname or alias.name))
    return pairs


class TestFacadeReexportCompleteness:
    """Every F401 re-export in the facade must be pinned here (and vice
    versa), so the guard cannot silently under-cover new re-exports."""

    def test_f401_set_matches_pinned_map(self):
        actual = _f401_reexports_in_facade_source()
        pinned = set(_ALL_PAIRS)

        unpinned = actual - pinned
        stale = pinned - actual
        problems = []
        if unpinned:
            problems.append(
                "F401 re-export(s) present in the facade but MISSING from "
                f"EXPECTED_REEXPORTS (add them to the guard): {sorted(unpinned)}"
            )
        if stale:
            problems.append(
                "EXPECTED_REEXPORTS entr(ies) no longer F401 re-exports in "
                f"the facade (update the guard): {sorted(stale)}"
            )
        assert not problems, "\n".join(problems)

    def test_pinned_map_is_nonempty_sanity(self):
        # Guard-of-the-guard: an accidentally emptied map would make every
        # other test in this file vacuously pass.
        assert len(_ALL_PAIRS) >= 40
        assert set(EXPECTED_REEXPORTS) >= {
            "_grar_validate", "_grar_merge", "_grar_parse",
            "_grar_routes", "_grar_render",
        }
