#!/usr/bin/env python3
"""Tests for init_project.py — Dynamic Alerting project bootstrap scaffolding.

Tests 15 rule packs (13 selectable + 2 auto-enabled), file generation,
YAML structure validation, K8s naming validation, and end-to-end initialization.
"""

import os
import re
import sys
import tempfile
from datetime import datetime

import pytest
import yaml

TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts", "tools", "ops"))

import init_project as ip  # noqa: E402
from _lib_exitcodes import EXIT_CALLER_ERROR  # noqa: E402


# ============================================================
# ── 1. RULE_PACK_CATALOG Structure ──
# ============================================================

class TestRulePackCatalog:
    """Tests for RULE_PACK_CATALOG — catalog structure and completeness."""

    def test_catalog_has_15_packs(self):
        """Verify catalog contains exactly 15 rule packs."""
        assert len(ip.RULE_PACK_CATALOG) == 15

    def test_all_packs_have_required_keys(self):
        """All packs must have 'label' and 'defaults' keys."""
        for pack_name, pack_data in ip.RULE_PACK_CATALOG.items():
            assert 'label' in pack_data, f"{pack_name} missing 'label'"
            assert 'defaults' in pack_data, f"{pack_name} missing 'defaults'"

    def test_selectable_packs_have_defaults(self):
        """Selectable packs (non-auto) must have non-empty defaults."""
        selectable = ip._selectable_rule_packs()
        for pack_name in selectable:
            pack = ip.RULE_PACK_CATALOG[pack_name]
            assert isinstance(pack['defaults'], dict)
            assert len(pack['defaults']) > 0, f"{pack_name} has empty defaults"

    def test_auto_enabled_packs_have_label(self):
        """Auto-enabled packs must have 'auto_enabled': True."""
        auto = ip._auto_enabled_rule_packs()
        for pack_name in auto:
            pack = ip.RULE_PACK_CATALOG[pack_name]
            assert pack.get('auto_enabled') is True
            assert isinstance(pack['label'], str) and len(pack['label']) > 0

    def test_specific_packs_exist(self):
        """Verify all expected packs are in catalog."""
        expected = [
            'mariadb', 'postgresql', 'redis', 'mongodb', 'elasticsearch',
            'oracle', 'db2', 'clickhouse', 'kafka', 'rabbitmq', 'jvm',
            'nginx', 'kubernetes', 'operational', 'platform',
        ]
        for pack in expected:
            assert pack in ip.RULE_PACK_CATALOG, f"{pack} not in catalog"

    def test_mariadb_pack_defaults(self):
        """MariaDB pack must have expected metric keys, in the right tier."""
        mariadb = ip.RULE_PACK_CATALOG['mariadb']['defaults']
        critical = ip.RULE_PACK_CATALOG['mariadb']['critical_overrides']
        assert 'mysql_connections' in mariadb
        assert 'mysql_replication_lag' in mariadb
        assert mariadb['mysql_connections'] == 80
        # #1218: the critical twin keeps its value and changes SECTION — it is
        # asserted against `critical_overrides`, not merely "somewhere in the
        # pack", because the section is the whole difference between a critical
        # threshold and an unconsumed warning series.
        assert critical['mysql_connections_critical'] == 150
        # #1231 rename guard: the catalog must carry the canonical spelling,
        # never the retired mysql_cpu one.
        assert 'mysql_threads_running' in mariadb
        assert 'mysql_threads_running_critical' in critical
        assert 'mysql_cpu' not in mariadb
        assert 'mysql_cpu_critical' not in critical

    def test_operational_and_platform_have_empty_defaults(self):
        """Operational and platform packs have auto-enabled marker, empty defaults."""
        for pack_name in ['operational', 'platform']:
            pack = ip.RULE_PACK_CATALOG[pack_name]
            assert pack.get('auto_enabled') is True
            assert pack['defaults'] == {}


# ============================================================
# ── 2. Rule Pack Partitioning ──
# ============================================================

class TestRulePackPartitioning:
    """Tests for _selectable_rule_packs / _auto_enabled_rule_packs."""

    def test_selectable_rule_packs_count(self):
        """Should return 13 selectable packs."""
        selectable = ip._selectable_rule_packs()
        assert len(selectable) == 13

    def test_auto_enabled_rule_packs_count(self):
        """Should return 2 auto-enabled packs (operational, platform)."""
        auto = ip._auto_enabled_rule_packs()
        assert len(auto) == 2
        assert 'operational' in auto
        assert 'platform' in auto

    def test_selectable_excludes_auto_enabled(self):
        """Selectable packs must not include auto-enabled ones."""
        selectable = set(ip._selectable_rule_packs())
        auto = set(ip._auto_enabled_rule_packs())
        assert selectable.isdisjoint(auto)

    def test_partitioning_is_complete(self):
        """Union of selectable + auto should equal total catalog."""
        selectable = set(ip._selectable_rule_packs())
        auto = set(ip._auto_enabled_rule_packs())
        total = selectable | auto
        assert total == set(ip.RULE_PACK_CATALOG.keys())

    def test_selectable_includes_mariadb_kubernetes(self):
        """Default interactive defaults include mariadb and kubernetes."""
        selectable = ip._selectable_rule_packs()
        assert 'mariadb' in selectable
        assert 'kubernetes' in selectable


# ============================================================
# ── 3. Tenant Name Validation ──
# ============================================================

class TestValidateTenantName:
    """Tests for _validate_tenant_name — K8s naming convention checks."""

    def test_simple_lowercase_name(self):
        """Simple lowercase name is valid."""
        assert ip._validate_tenant_name('db-a') is True
        assert ip._validate_tenant_name('prod-db') is True

    def test_numeric_suffix(self):
        """Names with numeric suffixes are valid."""
        assert ip._validate_tenant_name('db1') is True
        assert ip._validate_tenant_name('svc-01') is True

    def test_max_length_63(self):
        """Names with exactly 63 characters are valid."""
        name_63 = 'a' + '-' * 61 + 'a'
        assert len(name_63) == 63
        assert ip._validate_tenant_name(name_63) is True

    def test_too_long_64_chars(self):
        """Names with 64+ characters are invalid."""
        name_64 = 'a' + '-' * 62 + 'a'
        assert len(name_64) == 64
        assert ip._validate_tenant_name(name_64) is False

    def test_invalid_uppercase(self):
        """Names with uppercase are invalid."""
        assert ip._validate_tenant_name('DB-a') is False
        assert ip._validate_tenant_name('Db-a') is False

    def test_invalid_underscore(self):
        """Names with underscores are invalid."""
        assert ip._validate_tenant_name('db_a') is False

    def test_invalid_leading_hyphen(self):
        """Names starting with hyphen are invalid."""
        assert ip._validate_tenant_name('-db-a') is False

    def test_invalid_trailing_hyphen(self):
        """Names ending with hyphen are invalid."""
        assert ip._validate_tenant_name('db-a-') is False

    def test_invalid_special_chars(self):
        """Names with special chars are invalid."""
        assert ip._validate_tenant_name('db.a') is False
        assert ip._validate_tenant_name('db@a') is False
        assert ip._validate_tenant_name('db a') is False

    def test_single_char(self):
        """Single alphanumeric character is valid."""
        assert ip._validate_tenant_name('a') is True
        assert ip._validate_tenant_name('1') is True

    def test_single_hyphen_is_invalid(self):
        """Single hyphen alone is invalid."""
        assert ip._validate_tenant_name('-') is False

    def test_only_hyphens_is_invalid(self):
        """Names with only hyphens are invalid."""
        assert ip._validate_tenant_name('---') is False


# ============================================================
# ── 4. _gen_defaults_yaml ──
# ============================================================

class TestGenDefaultsYaml:
    """Tests for _gen_defaults_yaml — defaults config generation."""

    def test_basic_structure(self):
        """Generated YAML has defaults, state_filters, _routing_defaults."""
        yaml_str = ip._gen_defaults_yaml(['mariadb'], 'monitoring')
        config = yaml.safe_load(yaml_str)
        assert 'defaults' in config
        assert 'state_filters' in config
        assert '_routing_defaults' in config

    def test_mariadb_defaults_merged(self):
        """MariaDB defaults are merged into 'defaults' key."""
        yaml_str = ip._gen_defaults_yaml(['mariadb'], 'monitoring')
        config = yaml.safe_load(yaml_str)
        defaults = config['defaults']
        assert 'mysql_connections' in defaults
        assert defaults['mysql_connections'] == 80
        # …and its critical twin is NOT here (#1218). See TestCriticalTierPlacement.
        assert 'mysql_connections_critical' not in defaults

    def test_multiple_rule_packs_merged(self):
        """Multiple rule packs' defaults are all merged."""
        yaml_str = ip._gen_defaults_yaml(['mariadb', 'redis'], 'monitoring')
        config = yaml.safe_load(yaml_str)
        defaults = config['defaults']
        # MariaDB keys
        assert 'mysql_connections' in defaults
        # Redis keys
        assert 'redis_memory_usage' in defaults

    def test_state_filters_structure(self):
        """state_filters has expected entries."""
        yaml_str = ip._gen_defaults_yaml(['mariadb'], 'monitoring')
        config = yaml.safe_load(yaml_str)
        state_filters = config['state_filters']
        assert 'container_crashloop' in state_filters
        assert 'container_imagepull' in state_filters
        assert state_filters['container_crashloop']['severity'] == 'critical'

    def test_routing_defaults_structure(self):
        """_routing_defaults has receiver, group_by, timing."""
        yaml_str = ip._gen_defaults_yaml(['mariadb'], 'monitoring')
        config = yaml.safe_load(yaml_str)
        routing = config['_routing_defaults']
        assert 'receiver' in routing
        assert 'group_by' in routing
        assert 'group_wait' in routing
        assert 'group_interval' in routing
        assert 'repeat_interval' in routing

    def test_header_contains_rule_packs(self):
        """YAML header mentions selected rule packs."""
        yaml_str = ip._gen_defaults_yaml(['mariadb', 'kubernetes'], 'monitoring')
        assert 'mariadb' in yaml_str
        assert 'kubernetes' in yaml_str
        assert 'Rule Packs' in yaml_str

    def test_header_contains_generation_marker(self):
        """Header mentions 'da-tools init' for tracking."""
        yaml_str = ip._gen_defaults_yaml(['mariadb'], 'monitoring')
        assert 'da-tools init' in yaml_str

    def test_custom_namespace_used(self):
        """Custom namespace parameter doesn't affect YAML structure."""
        yaml_str = ip._gen_defaults_yaml(['mariadb'], 'custom-ns')
        config = yaml.safe_load(yaml_str)
        # Namespace is in header comment, not in structure
        assert 'defaults' in config


# ============================================================
# ── 4b. _gen_defaults_yaml — declared-key list (#1310) ──
# ============================================================

class TestGenDefaultsDeclaredList:
    """The `optional_overrides:` list — key NAMES the platform recognises but
    assigns no value to.

    This generator writes the customer-side `_defaults.yaml`, which is the file
    tenant-api validates tenant writes against (`--config-dir` is the cloned
    customer repo, and `config.mergeTenantConfig` reads its `_defaults.yaml`).
    Omit the list here and a `da-tools init` customer's tenants get HTTP 400 for
    every declared key even though the chart declares them — the chart list only
    ever reaches threshold-exporter.
    """

    def test_selected_pack_declares_its_flat_optional_keys(self):
        config = yaml.safe_load(ip._gen_defaults_yaml(['oracle'], 'monitoring'))
        assert config['optional_overrides'] == [
            'oracle_wait_time_rate', 'oracle_process_count',
            'oracle_pga_allocated_bytes',
        ]

    def test_list_of_strings_never_a_mapping(self):
        config = yaml.safe_load(ip._gen_defaults_yaml(['db2'], 'monitoring'))
        listed = config['optional_overrides']
        assert isinstance(listed, list) and all(isinstance(k, str) for k in listed)
        for key in listed:
            assert key not in config['defaults']

    def test_pack_without_flat_optional_keys_emits_nothing(self):
        """mariadb's optional tier is `_critical`-only ⇒ no key at all, rather
        than an empty list (which reads as an accident)."""
        config = yaml.safe_load(ip._gen_defaults_yaml(['mariadb'], 'monitoring'))
        assert 'optional_overrides' not in config

    def test_no_critical_key_is_ever_declared(self):
        """#1311: `<base>_critical` resolves via resolveCriticalRows off
        defaults[base]; on the declared list it would be decoration."""
        config = yaml.safe_load(
            ip._gen_defaults_yaml(sorted(ip.RULE_PACK_CATALOG), 'monitoring'))
        assert not [k for k in config['optional_overrides']
                    if k.endswith('_critical')]

    def test_matches_the_other_producer_key_for_key(self):
        """⛔ The whole point of sharing the derivation: `da-tools init` and
        `scaffold-tenant` must declare the SAME keys, or which onboarding tool a
        customer happened to use decides which thresholds their tenants can set.
        """
        import scaffold_tenant

        config = yaml.safe_load(
            ip._gen_defaults_yaml(sorted(ip.RULE_PACK_CATALOG), 'monitoring'))
        db_packs = [k for k in scaffold_tenant.RULE_PACKS if k != 'kubernetes']
        other = scaffold_tenant.generate_defaults(db_packs)['optional_overrides']
        assert config['optional_overrides'] == other

    def test_header_explains_the_tier_rather_than_leaving_a_bare_list(self):
        yaml_str = ip._gen_defaults_yaml(['oracle'], 'monitoring')
        assert 'optional_overrides' in yaml_str
        assert 'asserts no value' in yaml_str
        assert 'Do NOT move one into `defaults:`' in yaml_str


# ============================================================
# ── 5. _gen_tenant_yaml ──
# ============================================================

class TestGenTenantYaml:
    """Tests for _gen_tenant_yaml — tenant config stub generation."""

    def test_basic_structure(self):
        """Generated YAML has tenants key with tenant name."""
        yaml_str = ip._gen_tenant_yaml('db-a', ['mariadb'])
        config = yaml.safe_load(yaml_str)
        assert 'tenants' in config
        assert 'db-a' in config['tenants']

    def test_routing_stub_exists(self):
        """Tenant config includes _routing stub."""
        yaml_str = ip._gen_tenant_yaml('db-a', ['mariadb'])
        config = yaml.safe_load(yaml_str)
        tenant_config = config['tenants']['db-a']
        assert '_routing' in tenant_config
        assert 'receiver' in tenant_config['_routing']
        assert 'url' in tenant_config['_routing']['receiver']

    def test_routing_url_includes_tenant_name(self):
        """Webhook URL includes tenant name."""
        yaml_str = ip._gen_tenant_yaml('prod-db', ['mariadb'])
        config = yaml.safe_load(yaml_str)
        tenant_config = config['tenants']['prod-db']
        url = tenant_config['_routing']['receiver']['url']
        assert 'prod-db' in url

    def test_example_overrides_from_first_pack(self):
        """First 3 metric keys from first pack are included as examples."""
        yaml_str = ip._gen_tenant_yaml('db-a', ['mariadb'])
        config = yaml.safe_load(yaml_str)
        tenant_config = config['tenants']['db-a']
        mariadb_keys = list(ip.RULE_PACK_CATALOG['mariadb']['defaults'].keys())[:3]
        for key in mariadb_keys:
            assert key in tenant_config

    def test_example_values_are_strings(self):
        """Example metric values are converted to strings (YAML 3-state)."""
        yaml_str = ip._gen_tenant_yaml('db-a', ['mariadb'])
        config = yaml.safe_load(yaml_str)
        tenant_config = config['tenants']['db-a']
        mariadb_keys = list(ip.RULE_PACK_CATALOG['mariadb']['defaults'].keys())[:3]
        for key in mariadb_keys:
            # Values should be strings in final YAML
            assert isinstance(tenant_config[key], str)

    def test_header_mentions_tenant_name(self):
        """YAML header mentions the specific tenant."""
        yaml_str = ip._gen_tenant_yaml('my-app', ['mariadb'])
        assert 'my-app.yaml' in yaml_str

    def test_no_examples_with_empty_rule_packs(self):
        """Empty rule_packs list results in no example overrides."""
        yaml_str = ip._gen_tenant_yaml('db-a', [])
        config = yaml.safe_load(yaml_str)
        tenant_config = config['tenants']['db-a']
        # Should only have _routing, no other metrics
        assert len(tenant_config) == 1
        assert '_routing' in tenant_config

    def test_multiple_tenants_independent(self):
        """Different tenant configs are independent."""
        yaml1 = ip._gen_tenant_yaml('db-a', ['mariadb'])
        yaml2 = ip._gen_tenant_yaml('db-b', ['redis'])
        assert 'db-a' in yaml1
        assert 'db-a' not in yaml2
        assert 'db-b' in yaml2


# ============================================================
# ── 5b. _gen_tenant_yaml — declared-key stub block (#1321) ──
# ============================================================

# ⛔ The stub-format expectations live in ONE module shared with
# `test_scaffold_tenant.py` (`tests/ops/_stub_declared_shared.py`). Both
# packages pin the SAME block, produced by the same renderer; two hand-kept
# copies meant a format change updated in one of them left the other green and
# no longer measuring anything.
from _stub_declared_shared import (  # noqa: E402
    STUB_PLACEHOLDER,
    STUB_PLACEHOLDER_VALUE,
    paste_declared_lines_under_tenant as _paste_declared_lines_under_tenant,
    shipped_chart_declared_keys,
    stub_key_lines as _stub_key_lines,
)

# The parameterised difference: this generator renders the English stub.
_LANG = 'en'
_STUB_PLACEHOLDER = STUB_PLACEHOLDER[_LANG]

# ⚠️ Kept as a literal `os.path.join` HERE rather than inside the shared
# helper: `verify_diff.build_map` scans only `test_*.py` for path references,
# so moving this join out would drop `helm/threshold-exporter/values.yaml` →
# this module from `text_map` — a chart edit would stop selecting the test
# below that reads the chart.
_SHIPPED_CHART_VALUES = os.path.join(
    REPO_ROOT, 'helm', 'threshold-exporter', 'values.yaml')


def _shipped_chart_declared_keys():
    return shipped_chart_declared_keys(_SHIPPED_CHART_VALUES)


class TestGenTenantYamlDeclaredBlock:
    """The declared tier, told to the only reader who opens this file (#1321).

    `_defaults.yaml` and the rule-pack headers already explain the tier to
    whoever runs the PLATFORM. A tenant reads neither: the one file it opens is
    its own `<tenant>.yaml`, whose header used to say "Omitted keys inherit
    from _defaults.yaml" — true for keys with a platform default, exactly
    backwards for the declared tier, which has nothing to inherit.
    """

    def _declared(self, packs):
        from _registry_lib import shipped_optional_keys_for_packs
        return shipped_optional_keys_for_packs(packs)

    def test_header_no_longer_claims_every_omitted_key_inherits(self):
        yaml_str = ip._gen_tenant_yaml('db-a', ['oracle'])
        assert 'Omitted keys inherit from _defaults.yaml.' not in yaml_str
        assert 'inherits NOTHING' in yaml_str
        assert 'stays silent' in yaml_str

    def test_header_also_names_the_critical_tier(self):
        """Two buckets is not the taxonomy. `<base>_critical` is in neither
        block *as far as the declared list goes*, yet it decides whether a
        critical row exists at all — so a header that stops at two reads as
        complete and is not. Same criterion as the three-way rule-pack header
        split: does what we say buy the tenant protection."""
        yaml_str = ip._gen_tenant_yaml('db-a', ['oracle'])
        assert '`<base>_critical`' in yaml_str

    # -- the third paragraph must be TRUE OF THIS GENERATOR'S OWN OUTPUT ----
    #
    # It was first written as a static sentence ("in neither block; set one and
    # it fires once `<base>` has a value"). That was true of what
    # `scaffold_tenant` wrote and false of what `init_project` wrote, because
    # this module's catalog put 16 `_critical` keys straight into `defaults:` —
    # so the sentence was made conditional on the artifact. #1218 then fixed the
    # artifact: the keys moved to the tenant stub, both generators now ship the
    # `_critical`-free regime, and the paragraph FOLLOWED the change instead of
    # having to be found and rewritten. That is what the derivation bought, and
    # it is why the tests below still read the sibling file rather than pinning
    # whichever regime happens to be current.
    #
    # ⛔ The tests below assert against the SIBLING FILE, parsed — not against a
    # phrase. The pre-#1321 test only checked that a string was present, which a
    # false string passes just as well.

    def test_critical_paragraph_matches_the_sibling_defaults_file(self):
        packs = ['mariadb', 'oracle']
        tenant_text = ip._gen_tenant_yaml('db-a', packs)
        sibling = yaml.safe_load(ip._gen_defaults_yaml(packs, 'monitoring'))
        crit = [k for k in sibling['defaults'] if k.endswith('_critical')]
        assert crit == [], (
            '#1218: no `_defaults.yaml` producer may put a `<base>_critical` '
            'under `defaults:` — see TestCriticalTierPlacement for why, and '
            'check_threshold_reachability for the repo-wide gate')

        from _registry_lib import render_tenant_critical_note_lines
        expected = '\n'.join(
            render_tenant_critical_note_lines(sibling['defaults'], lang='en'))
        assert expected in tenant_text

        # …and two assertions that do not go through the renderer at all: the
        # regime the header is in must match the parsed artifact, BOTH ways.
        # ⛔ Written as two direct assertions rather than
        # `assert (marker in header) == (not crit)`, which the `crit == []`
        # above degrades into a constant-RHS one-sided check that merely LOOKS
        # bidirectional (blind review, #1218).
        header = tenant_text.split('\ntenants:')[0]
        assert 'lists none of them under `defaults:`' in header, header
        assert 'the wrong section' not in header, header

    def test_critical_paragraph_flips_when_defaults_gains_a_critical_key(self):
        """The renderer's own contract, on synthetic input: a `_critical`
        appearing in `defaults:` must move the claim, not just the count.

        ⛔ And the claim it moves TO must be the true one (#1218). The branch
        used to read "the critical row fires without you doing anything" — a
        sentence about a file in which no critical row can exist, rendered into
        the one file a customer opens. Measured in
        `components/threshold-exporter/app/pkg/config/critical_tier_placement_test.go`:
        the key emits `{component="<prefix>", metric="<rest>_critical",
        severity="warning"}` (parseMetricKey splits on the first underscore) and
        nothing else. So this asserts the promise is ABSENT, not just that the
        wording changed — a rewrite that kept the reassurance would pass a
        count-only assertion.
        """
        from _registry_lib import render_tenant_critical_note_lines
        without = '\n'.join(render_tenant_critical_note_lines(
            {'mysql_connections': 80}, lang='en'))
        with_crit = '\n'.join(render_tenant_critical_note_lines(
            {'mysql_connections': 80, 'mysql_connections_critical': 150},
            lang='en'))
        assert 'lists none of them' in without
        assert 'lists none of them' not in with_crit
        assert 'puts 1 of them under `defaults:`' in with_crit
        assert 'the wrong section' in with_crit
        assert 'no critical row exists' in with_crit
        assert 'fires without you doing anything' not in with_crit

    def test_critical_paragraph_is_a_warning_in_both_languages(self):
        """Same claim, both renderings. The zh branch is what a Chinese-locale
        `scaffold_tenant` run writes, and it carried the identical reassurance
        ("critical 列本來就會發射，不需要你做任何事"); fixing only the branch the
        English generator happens to use would leave the other half shipping
        the sentence this issue is about.
        """
        from _registry_lib import render_tenant_critical_note_lines
        misplaced = {'mysql_connections': 80, 'mysql_connections_critical': 150}
        for lang, ok_marker, banned in (
            ('en', 'the wrong section', 'fires without you doing anything'),
            ('zh', '放錯區塊', '本來就會發射'),
        ):
            text = '\n'.join(render_tenant_critical_note_lines(misplaced, lang=lang))
            assert ok_marker in text, (lang, text)
            assert banned not in text, (lang, text)

    def test_block_members_equal_the_derived_set(self):
        """⛔ This pins the WIRING, not the derivation — say so, because the
        name reads like the stronger claim.

        The equality's right-hand side is `shipped_optional_keys_for_packs`,
        which is the very function `_gen_tenant_yaml` calls. A broken derivation
        therefore moves BOTH sides together and leaves this green (measured:
        inverting the predicate and defeating the pack filter are both caught by
        other tests in this class, not by this one). What it does catch is the
        block vanishing, dropping a key, or emitting one twice — worth pinning,
        since the tenant is told to read the block as the complete list for its
        packs, and set equality (not substring) is what makes that readable.

        The last assertion has an independent source: every listed key must also
        appear in the SHIPPED chart values, read from the artifact rather than
        re-derived (same discipline as the reachability gate). That one does not
        move with the generator.
        """
        packs = ['oracle', 'db2', 'clickhouse']
        listed = [k for k, _rhs in _stub_key_lines(
            ip._gen_tenant_yaml('db-a', packs))]
        assert set(listed) == set(self._declared(packs))
        assert len(listed) == len(set(listed)), 'duplicate key line'
        assert set(listed) <= _shipped_chart_declared_keys(), (
            'the stub advertises a key the shipped chart does not declare — a '
            'tenant setting it gets an unknown-key 400 from the only supported '
            'writer')

    def test_block_is_filtered_by_the_selected_packs(self):
        listed = {k for k, _ in _stub_key_lines(
            ip._gen_tenant_yaml('db-a', ['oracle']))}
        assert listed == set(self._declared(['oracle']))
        assert not [k for k in listed if k.startswith('clickhouse_')]

    def test_no_declared_key_is_ever_given_a_value(self):
        """⛔ The regression guard, and the reason the block renders commented
        placeholders at all: writing the registry's number in for the tenant
        would re-arm on this surface the keystroke #1310 removed from the
        interactive prompt (ADR-030 has measured counter-examples for these
        very numbers)."""
        packs = ['oracle', 'db2', 'clickhouse']
        yaml_str = ip._gen_tenant_yaml('db-a', packs)
        declared = set(self._declared(packs))

        # (a) nothing declared may reach the parsed document …
        config = yaml.safe_load(yaml_str)
        assert not declared & set(config['tenants']['db-a'])

        # (b) … nor may a number sit on a key line, commented out or not.
        for key, rhs in _stub_key_lines(yaml_str):
            value = rhs.split('#')[0].strip()
            assert value == _STUB_PLACEHOLDER, f'{key} carries a value: {rhs!r}'

    def test_reference_numbers_are_labelled_as_such(self):
        """The number stays visible (it is the only starting point an operator
        has) but never as an endorsement."""
        yaml_str = ip._gen_tenant_yaml('db-a', ['oracle'])
        assert 'reference start 300 count' in yaml_str
        assert 'not an endorsement' in yaml_str

    def test_pack_without_declared_keys_gets_no_block(self):
        """mariadb's optional tier is `_critical`-only ⇒ nothing to say."""
        yaml_str = ip._gen_tenant_yaml('db-a', ['mariadb'])
        assert not _stub_key_lines(yaml_str)
        assert 'Declared keys' not in yaml_str

    def test_stub_is_still_valid_yaml_and_block_is_all_comments(self):
        yaml_str = ip._gen_tenant_yaml('db-a', ['oracle', 'db2'])
        config = yaml.safe_load(yaml_str)
        assert set(config) == {'tenants'}
        lines = yaml_str.split('\n')
        start = next(i for i, l in enumerate(lines)
                     if l.startswith('# -- Declared keys'))
        assert all(l.startswith('#') or not l.strip()
                   for l in lines[start:])

    def test_header_pointer_agrees_with_whether_the_block_exists(self):
        """⛔ The header's pointer at the block was the last static claim here.

        The block is emitted only when the declared list is non-empty, but the
        header said "Those keys are listed at the end of this file"
        unconditionally — and MOST packs' optional tier is `_critical`-only
        (measured: mariadb / postgresql / redis / mongodb / elasticsearch /
        kafka / rabbitmq / jvm / nginx / kubernetes all derive to []). So a
        tenant onboarded onto any of them got a header pointing at a section
        that is not in its file.

        ⛔ Asserted as an EQUIVALENCE against the artifact, not as "the phrase
        is present": a conditional sentence ("if there are declared keys, see
        the end of the file") would satisfy a presence check while still
        handing the tenant a judgement the generator can make itself.
        """
        from _registry_lib import TENANT_STUB_DECLARED_HEADING
        heading = TENANT_STUB_DECLARED_HEADING[_LANG]
        shapes = [
            ['mariadb'],                          # optional tier all _critical
            ['redis'],                            # ditto — a second one
            ['oracle'],                           # flat declared keys
            ['mariadb', 'oracle', 'clickhouse'],  # mixed
        ]
        for packs in shapes:
            declared = self._declared(packs)
            text = ip._gen_tenant_yaml('db-a', packs)
            header = text.split('\ntenants:')[0]

            assert (heading in text) == bool(declared), packs
            assert ('listed at the end of this file' in header) \
                == bool(declared), packs
            assert ('declare no such key' in header) == (not declared), packs
            if declared:
                assert re.search(rf'\bThe {len(declared)} such keys\b',
                                 header), (packs, header)
            # …and the block the pointer promises is exactly that list.
            assert [k for k, _ in _stub_key_lines(text)] == declared, packs

    def test_block_matches_what_the_sibling_defaults_file_declares(self):
        """⛔ Same generator run writes both files. A stub advertising a key
        the sibling `_defaults.yaml` does not declare earns the tenant an
        HTTP 400 from the only supported writer."""
        packs = ['oracle', 'db2', 'clickhouse']
        declared_in_defaults = yaml.safe_load(
            ip._gen_defaults_yaml(packs, 'monitoring'))['optional_overrides']
        listed = [k for k, _ in _stub_key_lines(
            ip._gen_tenant_yaml('db-a', packs))]
        assert listed == declared_in_defaults

    def test_declared_lines_survive_the_copy_paste_the_prose_prescribes(self):
        """⛔ The stub promises "the indent already lines up" — and until this
        test nothing checked it.

        The stub's indent is a constant in `_registry_lib`
        (`render_tenant_declared_stub_lines`'s `indent=4`); the tenant keys'
        indent is whatever `yaml.dump` happens to produce here. Two independent
        facts, one of them written down as a promise to the tenant. Measured:
        bumping that constant by 4 turned no test red at all, while a reader
        following the prose afterwards gets `YAMLError: while parsing a block
        mapping` in the commonest case (a tenant that already has keys).

        So do the round trip for real — paste every line under the tenant with
        the leading `'# '` removed — and assert BOTH that it parses AND that the
        keys land under this tenant. Parsing alone is not enough: a too-deep
        paste can still be valid YAML while silently nesting the key inside
        whatever mapping precedes it.
        """
        text = ip._gen_tenant_yaml('db-a', ['oracle', 'db2'])
        keys = [k for k, _ in _stub_key_lines(text)]
        assert keys, 'no declared keys to round-trip'

        pasted = _paste_declared_lines_under_tenant(text, 'db-a')
        doc = yaml.safe_load(pasted)  # raises on the drifted indent
        section = doc['tenants']['db-a']
        for key in keys:
            assert key in section, (
                f'{key} did not land under the tenant — the stub indent no '
                f'longer matches what this generator dumps')
            assert section[key] == STUB_PLACEHOLDER_VALUE[_LANG]
        # the tenant's pre-existing keys are still its own
        assert '_routing' in section


# ============================================================
# ── 5c. `<base>_critical` tier placement (#1218 / TRK-344) ──
# ============================================================
#
# The catalog is TWO dicts because the section a key lands in decides whether it
# does anything, and both wrong answers are silent:
#
#   `defaults:`      → resolveBaseRows walks it and does not skip the suffix;
#                      parseMetricKey splits on the FIRST underscore, so
#                      `pg_connections_critical` becomes
#                      {component="pg", metric="connections_critical",
#                       severity="warning"} — one unconsumed series per tenant —
#                      and `tenant:alert_threshold:pg_connections_critical`
#                      (which reads severity="critical") stays empty.
#   `<tenant>.yaml`  → resolveCriticalRows iterates tenant overrides and admits
#                      on defaults[<base>], producing the real critical row.
#
# Both directions are MEASURED, not asserted from reading the resolver:
# components/threshold-exporter/app/pkg/config/critical_tier_placement_test.go.
# The repo-wide gate over all five `_defaults.yaml` producers is
# check_threshold_reachability.py's defaults-tier face; the tests here pin this
# generator's own two files.

def _flat_header(text: str) -> str:
    """The tenant stub's comment header as ONE line.

    ⛔ Assertions about prose must not depend on where the renderer happened to
    wrap. Round-2 blind review rewrote three sentences in this header and broke
    four assertions pinned to a specific `\\n# ` position — none of which were
    testing anything about line breaks. Flattening keeps them about what the
    file SAYS.
    """
    return ' '.join(line.lstrip('#').strip()
                    for line in text.split('\ntenants:')[0].splitlines())


class TestCriticalTierPlacement:
    """#1218 — no `<base>_critical` in `defaults:`, all of them in the stub."""

    def test_catalog_tiers_do_not_overlap_and_are_suffix_correct(self):
        """The split is a partition, and the suffix decides which side.

        Enumerating the 16 key names here would pass just as well after someone
        added a 17th to the wrong dict, so the assertion is over EVERY pack.
        """
        for name, pack in ip.RULE_PACK_CATALOG.items():
            base = pack['defaults']
            crit = pack.get('critical_overrides', {})
            assert not (set(base) & set(crit)), f'{name}: key in both tiers'
            misfiled = [k for k in base if k.endswith('_critical')]
            assert misfiled == [], (
                f'{name}: {misfiled} sit in `defaults`, where the critical tier '
                f'cannot see them — move them to `critical_overrides`')
            stray = [k for k in crit if not k.endswith('_critical')]
            assert stray == [], (
                f'{name}: {stray} are in `critical_overrides` without the '
                f'suffix — resolveCriticalRows would never look at them')

    def test_every_critical_key_has_its_base_in_the_same_pack(self):
        """resolveCriticalRows admits on `defaults[<base>]`, and since #1227 a
        dangling `_critical` is a blocking ValidateTenantKeys Error — so a
        seeded key whose base is absent is not merely inert, it makes the
        tenant-api write of the whole stub fail.

        The literal floor is the non-vacuity guard: this loop body runs zero
        times on a catalog with no critical tier at all, and a test that passes
        by never executing its assertion is indistinguishable from one that
        holds.
        """
        checked = 0
        for name, pack in ip.RULE_PACK_CATALOG.items():
            for key in pack.get('critical_overrides', {}):
                base = key[: -len('_critical')]
                assert base in pack['defaults'], (
                    f'{name}: {key} has no base {base!r} under `defaults`')
                checked += 1
        assert checked >= 16, checked

    def test_generated_defaults_never_carry_a_critical_key(self):
        """Over every pack individually AND the full selection — a per-pack loop
        catches the pack a whole-catalog run would drown."""
        packs = list(ip.RULE_PACK_CATALOG)
        for selection in [[p] for p in packs] + [packs]:
            config = yaml.safe_load(ip._gen_defaults_yaml(selection, 'monitoring'))
            offenders = [k for k in config['defaults'] if k.endswith('_critical')]
            assert offenders == [], f'{selection}: {offenders}'

    def test_stub_seeds_the_critical_tier_for_every_selected_pack(self):
        """Not just the first one.

        The pre-#1218 stub copied three keys from `rule_packs[0]` only, so the
        critical tier of packs 2..n existed nowhere a resolver would read it.
        mariadb+kubernetes is the shipped interactive default pairing.
        """
        packs = ['mariadb', 'kubernetes']
        stub = yaml.safe_load(ip._gen_tenant_yaml('db-a', packs))['tenants']['db-a']
        expected = {}
        for p in packs:
            expected.update(ip.RULE_PACK_CATALOG[p]['critical_overrides'])
        assert expected, 'fixture: both packs are supposed to have a critical tier'
        for key, value in expected.items():
            assert stub.get(key) == str(value), (key, stub.get(key), value)

    def test_full_selection_seeds_all_sixteen(self):
        """The count the issue was written about, derived from the catalog on
        both sides so it tracks a 17th key instead of going stale — with a
        literal floor so an empty derivation cannot satisfy it vacuously."""
        packs = list(ip.RULE_PACK_CATALOG)
        stub = yaml.safe_load(ip._gen_tenant_yaml('db-a', packs))['tenants']['db-a']
        catalog_critical = {k for p in packs
                            for k in ip.RULE_PACK_CATALOG[p].get('critical_overrides', {})}
        assert len(catalog_critical) >= 16
        assert catalog_critical <= set(stub)

    def test_stub_header_count_matches_what_it_actually_seeded(self):
        """The pre-fill sentence is rendered from the seeded mapping; pin that
        it says the number the file carries, not a plausible one.

        ⛔ Anchored to the phrase the count is rendered INTO, not to the bare
        digits: `str(n) in text` is a substring test against a header full of
        unrelated numbers, so a header claiming any wrong count that happens to
        share a digit passes it.
        """
        packs = ['mariadb', 'kubernetes']
        text = ip._gen_tenant_yaml('db-a', packs)
        stub = yaml.safe_load(text)['tenants']['db-a']
        seeded = [k for k in stub if k.endswith('_critical')]
        assert re.search(rf'# {len(seeded)} of them are written in below',
                         text), text

    def test_stub_header_states_both_costs_the_tenant_cannot_infer(self):
        """The pre-fill has two consequences a tenant cannot infer from the keys
        alone, and BOTH were mis-stated in the first version (blind review,
        #1218):

        1. `<base>: "disable"` does not cascade to `<base>_critical` — the
           warning row goes and the critical row keeps firing. Measured in
           `pkg/config/critical_tier_placement_test.go`. The very next line of
           this same header says `Set a key to "disable" to suppress that
           metric`, so omitting this makes the file contradict itself.
        2. A dangling `<base>_critical` is write-blocking **on the tenant-api
           path only**. `validate_config.py` — the gate this tool actually
           installs into the customer's CI and pre-commit — reports WARN and
           exits 0 on the same input. The first draft promised "EVERY write of
           this file is rejected" full stop, which is a stop this customer's
           own pipeline does not perform.
        """
        text = ip._gen_tenant_yaml('db-a', ['mariadb'])
        header = _flat_header(text)
        prefill = header.split('of them are written in below')[1]

        # (1) the disable-cascade caveat, and it must sit in the same header as
        # the sentence it qualifies
        assert 'does NOT disable' in prefill, header
        assert 'Set a key to "disable"' in header, header

        # (1b) …and DELETE must not be offered as a synonym for disable.
        # Measured (round-2 blind review): both lines deleted → the warning row
        # comes BACK at the platform default, because an absent key is State 2.
        assert 'DELETING both lines is not the same thing' in prefill, header
        assert 'BOTH lines must say "disable"' in prefill, header

        # (1c) ⛔ "BOTH lines" presumes both lines exist, and usually they do
        # not: base overrides are seeded from rule_packs[0] only while the
        # critical tier is seeded for every selected pack. Measured with
        # `--rule-packs mariadb,redis,kubernetes`: 7 `_critical` keys, 5 with no
        # twin in the file. A remedy the reader cannot carry out is worse than
        # none, so the header has to say the line may be missing (round-6 blind
        # review).
        assert 'most `<base>` lines are NOT in this file' in prefill, header
        assert 'ADD the `<base>: "disable"` line yourself' in prefill, header

        # (2) the rejection claim must name WHICH path blocks, and must state an
        # invariant rather than the current state of the generated CI.
        #
        # ⛔ Two earlier drafts failed this in opposite directions, each one
        # merge away from being false: "EVERY write of this file is rejected"
        # (a stop the customer's pipeline does not perform), then "that job
        # fails before it validates anything" (true only while the generated
        # invocations still pass the unsupported `--ci`, #1380). So the negative
        # assertions below are the load-bearing half — they forbid re-describing
        # the neighbouring defect in a file customers keep.
        assert 'tenant-api' in prefill, header
        assert 'ONLY the tenant-api write path' in prefill, header
        assert 'validate-config' in prefill and 'exits 0' in prefill, header
        assert 'never establishes that this is fixed' in prefill, header
        for time_bound in ('--ci', 'does not accept', 'before it validates',
                           'EVERY write of this file is rejected',
                           # ⛔ and no relative clause re-attributing the TOOL's
                           # behaviour to the generated CI job: "…, which the CI
                           # generated beside this file runs, reports it as a
                           # WARNING and exits 0" reads as "that job goes green",
                           # which is draft 2's defect wearing different words.
                           'the CI generated beside this file runs'):
            assert time_bound not in prefill, (time_bound, header)

        # (3) the only in-tool refresh route is --force, which discards the
        # retuning the same paragraph invites two lines earlier
        assert '--force' in prefill and 'discards every hand edit' in prefill, header

        # the staleness statement must cover BOTH files, never just these lines
        assert 'sibling' in prefill and '_defaults.yaml' in prefill, header
        assert 'one-time copy into your repo' in prefill, header
        # …and it must not re-assert that the platform supplies the values —
        # the paragraph above it (correctly) says the opposite.
        assert 'the platform supplies' not in prefill

    def test_the_missing_twin_claim_matches_what_the_stub_actually_seeds(self):
        """The header's twinless-base caveat is a claim about THIS generator's
        output, so it is measured rather than asserted: a multi-pack selection
        must really produce `_critical` keys whose base is absent, otherwise the
        caveat describes a state that cannot occur and is just noise.

        Measured at the time of writing (`mariadb,redis,kubernetes`): 7 critical
        keys, 5 of them twinless — because the illustrative base overrides come
        from `rule_packs[0]` only while the critical tier is seeded for every
        selected pack (blind review, round 6: the header said "BOTH lines must
        say disable" while most files have only one of the two).
        """
        text = ip._gen_tenant_yaml('acme', ['mariadb', 'redis', 'kubernetes'])
        stub = yaml.safe_load(text)['tenants']['acme']
        crit = [k for k in stub if k.endswith('_critical')]
        base = [k for k in stub
                if not k.startswith('_') and not k.endswith('_critical')]
        twinless = [k for k in crit if k[: -len('_critical')] not in base]

        assert crit, stub
        assert twinless, (
            'no twinless critical key — the header caveat would be vacuous')
        # the base tier really is first-pack-only, which is WHY they are twinless
        assert all(k.startswith('mysql_') for k in base), base

    def test_stub_header_makes_no_deploy_specific_claim(self):
        """⛔ `_gen_tenant_yaml` never receives `deploy`, so any sentence about
        the deployment wiring is a claim it cannot qualify — and two of the
        three `--deploy` values do not generate that wiring at all.

        The first version said "this tool wires conf.d/ straight into the
        threshold-config ConfigMap". True for `kustomize`; for `helm` and
        `argocd` no `kustomize/` tree is produced (pinned below), so the tenant
        of a helm-deployed install was told their `conf.d/` was the live
        surface by a file that had no idea which install it was for.

        Pinned as an ABSENCE plus the fact that made it wrong, because the
        absence alone would pass on an empty header.
        """
        import inspect
        assert 'deploy' not in inspect.signature(ip._gen_tenant_yaml).parameters

        header = _flat_header(ip._gen_tenant_yaml('db-a', ['mariadb']))
        for mechanism in ('ConfigMap', 'kustomize', 'configMapGenerator', 'helm',
                          'argocd'):
            assert mechanism not in header, (mechanism, header)
        # the deploy-independent property it says instead
        assert 'nothing generated here tracks the platform' in header, header

    def test_validate_config_really_warns_and_exits_zero_on_a_dangling_critical(self):
        """⛔ The load-bearing sentence in the tenant header says `da-tools
        validate-config` reports a dangling `<base>_critical` as a WARNING and
        exits 0, so its verdict proves nothing. That was asserted as a SUBSTRING
        only (blind review, round 4) — the third time this PR shipped a claim
        whose truth nothing executed.

        The natural follow-up to #1218 is to make `validate_config` escalate this
        shape to an error. On that day the header would keep telling operators a
        green run means nothing while the tool actually goes red, and every
        string assertion would stay green. So this runs the real validator.
        """
        import subprocess
        import sys
        from pathlib import Path

        validator = str(Path(ip.__file__).resolve().parent / 'validate_config.py')
        with tempfile.TemporaryDirectory() as tmpdir:
            conf = Path(tmpdir) / 'conf.d'
            conf.mkdir()
            (conf / '_defaults.yaml').write_text(
                'defaults:\n  mysql_connections: 80\n', encoding='utf-8')
            (conf / 'db-a.yaml').write_text(
                'tenants:\n  db-a:\n    pg_connections_critical: "150"\n',
                encoding='utf-8')
            env = {**os.environ, 'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1'}
            for extra in ([], ['--strict']):
                run = subprocess.run(
                    [sys.executable, validator, '--config-dir', str(conf), *extra],
                    capture_output=True, text=True, encoding='utf-8',
                    errors='replace', env=env, timeout=180)
                assert run.returncode == 0, (extra, run.returncode, run.stdout[-800:])
                assert 'WARN' in (run.stdout + run.stderr).upper(), (
                    extra, run.stdout[-800:])

    def test_force_really_discards_hand_edits_in_both_files(self):
        """The header tells the operator that `--force` is the only in-tool
        refresh route and that it discards their edits. That was prose with a
        substring assertion behind it (blind review): soften `_check_existing_init`
        to skip existing tenant files, or make `_write_file` existence-aware, and
        the file keeps issuing a destructive warning about behaviour that changed
        while every assertion stays green.

        Runs the real CLI twice, so it pins the behaviour rather than the flag.
        """
        import subprocess
        import sys
        from pathlib import Path

        tool = str(Path(ip.__file__).resolve())
        with tempfile.TemporaryDirectory() as tmpdir:
            base = [sys.executable, tool, '-o', tmpdir, '--non-interactive',
                    '--tenants', 'db-a', '--rule-packs', 'mariadb']
            env = {**os.environ, 'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1'}
            assert subprocess.run(base, capture_output=True, env=env,
                                  timeout=180).returncode == 0

            defaults = Path(tmpdir) / 'conf.d' / '_defaults.yaml'
            tenant = Path(tmpdir) / 'conf.d' / 'db-a.yaml'
            for f in (defaults, tenant):
                f.write_text(f.read_text(encoding='utf-8') + '\n# HAND-EDIT\n',
                             encoding='utf-8')

            # without --force: refuses, and the edits survive
            rerun = subprocess.run(base, capture_output=True, env=env, timeout=180)
            assert rerun.returncode != 0, rerun.stdout[-500:]
            assert all('HAND-EDIT' in f.read_text(encoding='utf-8')
                       for f in (defaults, tenant))

            # with --force: BOTH files are rewritten, both edits gone
            forced = subprocess.run(base + ['--force'], capture_output=True,
                                    env=env, timeout=180)
            assert forced.returncode == 0, forced.stderr[-500:]
            for f in (defaults, tenant):
                assert 'HAND-EDIT' not in f.read_text(encoding='utf-8'), f.name

    def test_only_kustomize_deploy_generates_the_kustomize_tree(self):
        """The fact that made the removed sentence wrong, pinned so it cannot
        quietly become true-again-for-one-value without someone noticing.

        Driven through `run_init` (the real generator writing real files), not
        `_preview_files` — the preview is a second hand-written roster, and
        pinning a claim about the output against a mirror of the output is how
        the sentence this replaces got written in the first place.
        """
        from pathlib import Path

        for method, expected in (('kustomize', True), ('helm', False),
                                 ('argocd', False)):
            with tempfile.TemporaryDirectory() as tmpdir:
                created = ip.run_init({
                    'ci': 'both', 'deploy': method, 'rule_packs': ['mariadb'],
                    'tenants': ['db-a'], 'namespace': 'monitoring',
                    'da_tools_image': 'ghcr.io/vencil/da-tools:latest',
                }, tmpdir)
                on_disk = [p.relative_to(tmpdir).as_posix()
                           for p in Path(tmpdir).rglob('*') if p.is_file()]
            assert any('kustomize/' in p for p in on_disk) is expected, (method, on_disk)
            # The generator's own return value must agree with what it wrote —
            # compared on tmpdir-RELATIVE paths, not basenames (CodeRabbit).
            # Three `kustomization.yaml` files are generated (base + two
            # overlays), so a basename multiset still matches when one of them
            # is written to the wrong directory.
            assert sorted(Path(p).relative_to(tmpdir).as_posix()
                          for p in created) == sorted(on_disk), (
                method, created, on_disk)

    def test_no_prefill_sentence_when_the_selection_has_no_critical_tier(self):
        """db2 / clickhouse ship no `_critical` at all, so the file must not
        point at a section it does not have — the same defect #1321 fixed for
        the declared block, one paragraph over.

        ⛔ Derived from the renderer, never a literal. The first version
        asserted `'pre-filled below' not in text` — a string this module has
        NEVER emitted (the sentence is "of them are written in below"), so it could
        not fail for any input. Measured: with the `if not critical: return ''`
        early return removed, `_gen_tenant_yaml('db-a', ['db2'])` renders
        "# 0 of them are written in below" — the exact #1321 defect this
        test is named after — and the old assertion still passed (blind review).
        """
        # the guard itself, directly
        assert ip._critical_prefill_note({}) == ''

        # …and end to end, against a fingerprint taken from a NON-empty render
        # so the marker is known to be discriminating rather than merely absent
        sample = ip._critical_prefill_note({'zzz_critical': 1})
        marker = sample.split('of them', 1)[1].splitlines()[0]
        assert marker.strip(), sample

        db2 = ip._gen_tenant_yaml('db-a', ['db2'])
        mariadb = ip._gen_tenant_yaml('db-a', ['mariadb'])
        assert marker in mariadb, marker      # the marker really does appear…
        assert marker not in db2, marker      # …and it is absent here
        assert ip._catalog_critical(['db2']) == {}
        assert not [k for k in yaml.safe_load(db2)['tenants']['db-a']
                    if k.endswith('_critical')]

    def test_critical_key_whose_base_vanished_is_not_seeded(self, monkeypatch):
        """A guard for a state the catalog is not in today, so it is exercised
        on a mutated copy rather than asserted to be unreachable.

        `_catalog_critical` reads the module global at call time, so patching
        `ip.RULE_PACK_CATALOG` patches the name the consumer resolves — and the
        first assertion below is the proof the patch took, not a formality: a
        patch aimed at the wrong binding makes the real assertion pass for the
        wrong reason.
        """
        pack = ip.RULE_PACK_CATALOG['mariadb']
        broken = {
            'mariadb': {
                'label': pack['label'],
                'defaults': {k: v for k, v in pack['defaults'].items()
                             if k != 'mysql_connections'},
                'critical_overrides': dict(pack['critical_overrides']),
            }
        }
        monkeypatch.setattr(ip, 'RULE_PACK_CATALOG', broken)
        assert 'mysql_connections' not in ip._catalog_defaults(['mariadb']), (
            'the patch did not take — everything below would be vacuous')

        seeded = ip._catalog_critical(['mariadb'])
        assert 'mysql_connections_critical' not in seeded
        assert 'mysql_threads_running_critical' in seeded


# ============================================================
# ── 6. _gen_github_actions ──
# ============================================================

class TestGenGithubActions:
    """Tests for _gen_github_actions — GitHub Actions workflow generation."""

    def test_basic_yaml_structure(self):
        """Generated GitHub Actions YAML is valid."""
        yaml_str = ip._gen_github_actions('monitoring', 'ghcr.io/vencil/da-tools:latest', 'kustomize')
        # Should start with comment and have name: Dynamic Alerting
        assert 'name: Dynamic Alerting' in yaml_str
        assert 'on:' in yaml_str
        assert 'jobs:' in yaml_str

    def test_validate_stage_present(self):
        """All workflows include validate stage."""
        yaml_str = ip._gen_github_actions('monitoring', 'ghcr.io/vencil/da-tools:latest', 'kustomize')
        assert 'validate:' in yaml_str
        assert 'Validate config' in yaml_str

    def test_generate_stage_present(self):
        """All workflows include generate stage."""
        yaml_str = ip._gen_github_actions('monitoring', 'ghcr.io/vencil/da-tools:latest', 'kustomize')
        assert 'generate:' in yaml_str
        assert 'Generate Alertmanager routes' in yaml_str

    def test_kustomize_apply_stage(self):
        """Kustomize deployment includes apply stage with kustomize build."""
        yaml_str = ip._gen_github_actions('monitoring', 'ghcr.io/vencil/da-tools:latest', 'kustomize')
        assert 'apply:' in yaml_str
        assert 'kustomize build' in yaml_str
        assert 'kubectl apply' in yaml_str

    def test_helm_apply_stage(self):
        """Helm deployment includes apply stage with helm upgrade."""
        yaml_str = ip._gen_github_actions('monitoring', 'ghcr.io/vencil/da-tools:latest', 'helm')
        assert 'apply:' in yaml_str
        assert 'helm upgrade' in yaml_str

    def test_argocd_apply_stage(self):
        """ArgoCD deployment includes apply stage with argocd app sync."""
        yaml_str = ip._gen_github_actions('monitoring', 'ghcr.io/vencil/da-tools:latest', 'argocd')
        assert 'apply:' in yaml_str
        assert 'argocd app sync' in yaml_str

    def test_namespace_interpolation(self):
        """Custom namespace is interpolated into workflow."""
        yaml_str = ip._gen_github_actions('custom-ns', 'ghcr.io/vencil/da-tools:latest', 'kustomize')
        assert 'custom-ns' in yaml_str

    def test_da_tools_image_interpolation(self):
        """DA_TOOLS_IMAGE is interpolated."""
        yaml_str = ip._gen_github_actions('monitoring', 'ghcr.io/custom/da-tools:v1.0', 'kustomize')
        assert 'ghcr.io/custom/da-tools:v1.0' in yaml_str

    def test_pull_request_trigger(self):
        """Workflow triggers on pull_request for conf.d changes."""
        yaml_str = ip._gen_github_actions('monitoring', 'ghcr.io/vencil/da-tools:latest', 'kustomize')
        assert 'pull_request:' in yaml_str
        assert 'conf.d/**' in yaml_str

    def test_push_trigger(self):
        """Workflow triggers on push to main."""
        yaml_str = ip._gen_github_actions('monitoring', 'ghcr.io/vencil/da-tools:latest', 'kustomize')
        assert 'push:' in yaml_str
        assert 'main' in yaml_str

    def test_workflow_dispatch_trigger(self):
        """Workflow supports manual trigger."""
        yaml_str = ip._gen_github_actions('monitoring', 'ghcr.io/vencil/da-tools:latest', 'kustomize')
        assert 'workflow_dispatch:' in yaml_str


# ============================================================
# ── 7. _gen_gitlab_ci ──
# ============================================================

class TestGenGitlabCi:
    """Tests for _gen_gitlab_ci — GitLab CI pipeline generation."""

    def test_basic_yaml_structure(self):
        """Generated GitLab CI YAML is valid."""
        yaml_str = ip._gen_gitlab_ci('monitoring', 'ghcr.io/vencil/da-tools:latest', 'kustomize')
        assert 'stages:' in yaml_str
        assert 'variables:' in yaml_str

    def test_two_stages(self):
        """Pipeline has validate and apply — and NOT generate (#1358).

        The blast-radius job was removed: GitLab runs `script:` inside
        $DA_TOOLS_IMAGE, which carries no `git`, so its baseline could never
        be taken and it reported every tenant as new before failing on
        config-diff's ordinary exit code 1.
        """
        yaml_str = ip._gen_gitlab_ci('monitoring', 'ghcr.io/vencil/da-tools:latest', 'kustomize')
        stages = yaml.safe_load(yaml_str)['stages']
        assert stages == ['validate', 'apply'], stages

    def test_validate_config_job(self):
        """validate-config job is present."""
        yaml_str = ip._gen_gitlab_ci('monitoring', 'ghcr.io/vencil/da-tools:latest', 'kustomize')
        assert 'validate-config:' in yaml_str
        assert 'da-tools validate-config' in yaml_str

    def test_no_blast_radius_job(self):
        """⛔ Absence is the assertion (#1358).

        Parsed, not grepped: the removal left an explanatory comment that
        names `config-diff` and `git`, so a substring test on the raw text
        would pass for the wrong reason — and would keep passing if the job
        came back.
        """
        yaml_str = ip._gen_gitlab_ci('monitoring', 'ghcr.io/vencil/da-tools:latest', 'kustomize')
        doc = yaml.safe_load(yaml_str)
        assert 'generate-routes' not in doc, sorted(doc)
        for name, body in doc.items():
            if not isinstance(body, dict) or 'script' not in body:
                continue
            script = ' '.join(str(x) for x in body['script'])
            assert 'config-diff' not in script, (name, script)
            assert 'git ' not in script, (name, script)

    def test_kustomize_apply_job(self):
        """Kustomize deployment includes apply job."""
        yaml_str = ip._gen_gitlab_ci('monitoring', 'ghcr.io/vencil/da-tools:latest', 'kustomize')
        assert 'apply:' in yaml_str
        assert 'kustomize build' in yaml_str

    def test_helm_apply_job(self):
        """Helm deployment includes helm upgrade."""
        yaml_str = ip._gen_gitlab_ci('monitoring', 'ghcr.io/vencil/da-tools:latest', 'helm')
        assert 'apply:' in yaml_str
        assert 'helm upgrade' in yaml_str

    def test_argocd_apply_job(self):
        """ArgoCD deployment includes argocd app sync."""
        yaml_str = ip._gen_gitlab_ci('monitoring', 'ghcr.io/vencil/da-tools:latest', 'argocd')
        assert 'apply:' in yaml_str
        assert 'argocd app sync' in yaml_str

    def test_da_tools_image_variable(self):
        """DA_TOOLS_IMAGE variable is set."""
        yaml_str = ip._gen_gitlab_ci('monitoring', 'ghcr.io/custom/da-tools:v1.0', 'kustomize')
        assert 'DA_TOOLS_IMAGE: ghcr.io/custom/da-tools:v1.0' in yaml_str

    def test_da_tools_jobs_override_the_image_entrypoint(self):
        """#1408 — every $DA_TOOLS_IMAGE job must clear the ENTRYPOINT.

        The da-tools image is `ENTRYPOINT ["python3", "/opt/da-tools/
        entrypoint.py"]`, not a shell. GitLab's docker executor runs `script:`
        by invoking a shell inside the image, so a bare `image: $DA_TOOLS_IMAGE`
        feeds that invocation to the tool as arguments and the job dies before
        its first script line. The generator already said so three times beside
        the apply-stage images and applied it to the kubectl image only.

        Parsed, not grepped: `'entrypoint' in yaml_str` was true BEFORE the fix
        (the apply job had it), so a substring test here could never have gone
        red.
        """
        for deploy in ('kustomize', 'helm', 'argocd'):
            pipe = yaml.safe_load(
                ip._gen_gitlab_ci('monitoring', 'ghcr.io/vencil/da-tools:latest', deploy)
            )
            jobs = {
                name: body for name, body in pipe.items()
                if isinstance(body, dict) and 'script' in body
            }
            assert jobs, f"--deploy {deploy} produced no jobs to check"
            for name, body in jobs.items():
                image = body.get('image')
                assert isinstance(image, dict), (
                    f"--deploy {deploy}: job {name!r} declares `image: "
                    f"{image!r}` as a bare scalar, so it inherits the image's "
                    "ENTRYPOINT and never reaches its first script line."
                )
                assert image.get('entrypoint') == [''], (
                    f"--deploy {deploy}: job {name!r} has entrypoint "
                    f"{image.get('entrypoint')!r}, expected ['']."
                )


# ============================================================
# ── 8. _gen_kustomize_base ──
# ============================================================

class TestGenKustomizeBase:
    """Tests for _gen_kustomize_base — Kustomize base generation."""

    def test_valid_kustomization_yaml(self):
        """Generated kustomization.yaml is valid YAML."""
        yaml_str = ip._gen_kustomize_base(['db-a', 'db-b'], 'monitoring')
        config = yaml.safe_load(yaml_str)
        assert config is not None
        assert 'apiVersion' in config
        assert config['apiVersion'] == 'kustomize.config.k8s.io/v1beta1'

    def test_kind_is_kustomization(self):
        """Kind must be Kustomization."""
        yaml_str = ip._gen_kustomize_base(['db-a'], 'monitoring')
        config = yaml.safe_load(yaml_str)
        assert config['kind'] == 'Kustomization'

    def test_namespace_set(self):
        """Namespace is set in kustomization."""
        yaml_str = ip._gen_kustomize_base(['db-a'], 'custom-ns')
        config = yaml.safe_load(yaml_str)
        assert config['namespace'] == 'custom-ns'

    def test_configmap_generator_present(self):
        """ConfigMapGenerator is configured."""
        yaml_str = ip._gen_kustomize_base(['db-a', 'db-b'], 'monitoring')
        config = yaml.safe_load(yaml_str)
        assert 'configMapGenerator' in config
        assert len(config['configMapGenerator']) > 0

    def test_configmap_name(self):
        """ConfigMap is named threshold-config."""
        yaml_str = ip._gen_kustomize_base(['db-a'], 'monitoring')
        config = yaml.safe_load(yaml_str)
        cm = config['configMapGenerator'][0]
        assert cm['name'] == 'threshold-config'

    def test_defaults_file_included(self):
        """_defaults.yaml is included in files list."""
        yaml_str = ip._gen_kustomize_base(['db-a'], 'monitoring')
        config = yaml.safe_load(yaml_str)
        files = config['configMapGenerator'][0]['files']
        assert '_defaults.yaml' in files

    def test_all_tenant_files_included(self):
        """All tenant YAML files are included."""
        yaml_str = ip._gen_kustomize_base(['db-a', 'db-b', 'db-c'], 'monitoring')
        config = yaml.safe_load(yaml_str)
        files = config['configMapGenerator'][0]['files']
        assert 'db-a.yaml' in files
        assert 'db-b.yaml' in files
        assert 'db-c.yaml' in files

    def test_behavior_create(self):
        """ConfigMapGenerator behavior is create."""
        yaml_str = ip._gen_kustomize_base(['db-a'], 'monitoring')
        config = yaml.safe_load(yaml_str)
        cm = config['configMapGenerator'][0]
        assert cm['behavior'] == 'create'

    def test_generator_options_disable_hash(self):
        """generatorOptions disables name suffix hash."""
        yaml_str = ip._gen_kustomize_base(['db-a'], 'monitoring')
        config = yaml.safe_load(yaml_str)
        assert 'generatorOptions' in config
        assert config['generatorOptions']['disableNameSuffixHash'] is True

    def test_comment_header(self):
        """Comment header references da-tools init."""
        yaml_str = ip._gen_kustomize_base(['db-a'], 'monitoring')
        assert 'da-tools init' in yaml_str
        assert 'ConfigMap' in yaml_str


# ============================================================
# ── 9. _gen_kustomize_overlay ──
# ============================================================

class TestGenKustomizeOverlay:
    """Tests for _gen_kustomize_overlay — Kustomize overlay generation."""

    def test_valid_kustomization_yaml(self):
        """Generated overlay kustomization.yaml is valid."""
        yaml_str = ip._gen_kustomize_overlay('dev', 'monitoring')
        config = yaml.safe_load(yaml_str)
        assert config is not None
        assert config['kind'] == 'Kustomization'

    def test_kind_is_kustomization(self):
        """Kind must be Kustomization."""
        yaml_str = ip._gen_kustomize_overlay('prod', 'monitoring')
        config = yaml.safe_load(yaml_str)
        assert config['kind'] == 'Kustomization'

    def test_namespace_set(self):
        """Namespace is set in overlay."""
        yaml_str = ip._gen_kustomize_overlay('prod', 'custom-ns')
        config = yaml.safe_load(yaml_str)
        assert config['namespace'] == 'custom-ns'

    def test_base_reference(self):
        """Overlay references ../../base."""
        yaml_str = ip._gen_kustomize_overlay('dev', 'monitoring')
        config = yaml.safe_load(yaml_str)
        assert 'resources' in config
        assert '../../base' in config['resources']

    def test_header_mentions_env(self):
        """Comment header mentions environment."""
        yaml_str = ip._gen_kustomize_overlay('staging', 'monitoring')
        assert 'staging' in yaml_str


# ============================================================
# ── 10. _gen_precommit_snippet ──
# ============================================================

class TestGenPrecommitSnippet:
    """Tests for _gen_precommit_snippet — pre-commit config generation."""

    def test_valid_yaml_structure(self):
        """Generated snippet is valid YAML."""
        yaml_str = ip._gen_precommit_snippet('ghcr.io/vencil/da-tools:latest')
        config = yaml.safe_load(yaml_str)
        assert 'repos' in config
        assert isinstance(config['repos'], list)

    def test_local_repo(self):
        """Snippet uses local repo type."""
        yaml_str = ip._gen_precommit_snippet('ghcr.io/vencil/da-tools:latest')
        config = yaml.safe_load(yaml_str)
        repo = config['repos'][0]
        assert repo['repo'] == 'local'

    def test_da_validate_config_hook(self):
        """da-validate-config hook is present."""
        yaml_str = ip._gen_precommit_snippet('ghcr.io/vencil/da-tools:latest')
        config = yaml.safe_load(yaml_str)
        hooks = config['repos'][0]['hooks']
        hook_ids = [h['id'] for h in hooks]
        assert 'da-validate-config' in hook_ids

    def test_da_generate_routes_hook(self):
        """da-generate-routes hook is present."""
        yaml_str = ip._gen_precommit_snippet('ghcr.io/vencil/da-tools:latest')
        config = yaml.safe_load(yaml_str)
        hooks = config['repos'][0]['hooks']
        hook_ids = [h['id'] for h in hooks]
        assert 'da-generate-routes' in hook_ids

    def test_hooks_target_conf_d(self):
        """Hooks are limited to conf.d files."""
        yaml_str = ip._gen_precommit_snippet('ghcr.io/vencil/da-tools:latest')
        config = yaml.safe_load(yaml_str)
        hooks = config['repos'][0]['hooks']
        for hook in hooks:
            # Files pattern should reference conf.d
            assert 'conf' in hook['files']

    def test_language_docker_image(self):
        """Hooks use 'docker_image', not 'system'.

        ⛔ This test used to assert `language: system`, which pinned a hook that
        could never run: under `system`, pre-commit shlex-splits `entry` and
        execs it with NO shell, so the `docker run -v ${PWD}/conf.d:…` the
        snippet emitted reached docker as the literal string `${PWD}/conf.d` and
        failed on every commit touching conf.d. The test passed throughout,
        because it asked what the language WAS rather than whether the entry
        could execute — and by pinning the value it made the defect load-bearing.
        `docker_image` has pre-commit build the `docker run` and mount the repo
        at /src itself (pre_commit/languages/docker.py), so no expansion is
        needed. Executability is asserted in
        tests/ops/test_generated_ci_artifacts.py.
        """
        yaml_str = ip._gen_precommit_snippet('ghcr.io/vencil/da-tools:latest')
        config = yaml.safe_load(yaml_str)
        hooks = config['repos'][0]['hooks']
        assert hooks, 'no hooks generated'
        for hook in hooks:
            assert hook['language'] == 'docker_image'

    def test_header_mentions_da_tools(self):
        """Header mentions Dynamic Alerting."""
        yaml_str = ip._gen_precommit_snippet('ghcr.io/vencil/da-tools:latest')
        assert 'Dynamic Alerting' in yaml_str
        assert 'da-tools' in yaml_str


# ============================================================
# ── 11. _gen_da_init_marker ──
# ============================================================

class TestGenDaInitMarker:
    """Tests for _gen_da_init_marker — initialization marker generation."""

    def test_valid_yaml_structure(self):
        """Generated marker is valid YAML."""
        yaml_str = ip._gen_da_init_marker('github', 'kustomize', ['mariadb'], ['db-a'])
        config = yaml.safe_load(yaml_str)
        assert config is not None

    def test_version_field(self):
        """Marker includes version field."""
        yaml_str = ip._gen_da_init_marker('github', 'kustomize', ['mariadb'], ['db-a'])
        config = yaml.safe_load(yaml_str)
        assert 'version' in config
        assert config['version'].startswith('v') or config['version'] == '2.2.0'

    def test_generated_at_field(self):
        """Marker includes generated_at timestamp."""
        yaml_str = ip._gen_da_init_marker('github', 'kustomize', ['mariadb'], ['db-a'])
        config = yaml.safe_load(yaml_str)
        assert 'generated_at' in config
        # Should be ISO format timestamp
        assert 'T' in config['generated_at']

    def test_ci_platform_field(self):
        """Marker includes ci_platform."""
        yaml_str = ip._gen_da_init_marker('gitlab', 'kustomize', ['mariadb'], ['db-a'])
        config = yaml.safe_load(yaml_str)
        assert config['ci_platform'] == 'gitlab'

    def test_deploy_method_field(self):
        """Marker includes deploy_method."""
        yaml_str = ip._gen_da_init_marker('github', 'helm', ['mariadb'], ['db-a'])
        config = yaml.safe_load(yaml_str)
        assert config['deploy_method'] == 'helm'

    def test_rule_packs_field(self):
        """Marker includes rule_packs list."""
        yaml_str = ip._gen_da_init_marker('github', 'kustomize', ['mariadb', 'redis'], ['db-a'])
        config = yaml.safe_load(yaml_str)
        assert config['rule_packs'] == ['mariadb', 'redis']

    def test_tenants_field(self):
        """Marker includes tenants list."""
        yaml_str = ip._gen_da_init_marker('github', 'kustomize', ['mariadb'], ['db-a', 'db-b'])
        config = yaml.safe_load(yaml_str)
        assert config['tenants'] == ['db-a', 'db-b']

    def test_header_warning(self):
        """Header warns not to edit manually."""
        yaml_str = ip._gen_da_init_marker('github', 'kustomize', ['mariadb'], ['db-a'])
        assert 'Do not edit manually' in yaml_str
        assert 'upgrade detection' in yaml_str


# ============================================================
# ── 12. _preview_files ──
# ============================================================

class TestPreviewFiles:
    """Tests for _preview_files — file listing without writing."""

    def test_basic_file_count(self):
        """Basic config produces expected file count."""
        config = {
            'ci': 'github',
            'deploy': 'kustomize',
            'tenants': ['db-a', 'db-b'],
        }
        files = ip._preview_files(config, '/tmp')
        # _defaults.yaml + 2 tenant files + GitHub workflow + kustomize (base + 2 overlays)
        # + pre-commit + marker = 10 files
        assert len(files) >= 10

    def test_includes_conf_d_defaults(self):
        """Preview includes _defaults.yaml."""
        config = {
            'ci': 'github',
            'deploy': 'kustomize',
            'tenants': ['db-a'],
        }
        files = ip._preview_files(config, '/tmp')
        assert any('_defaults.yaml' in f for f in files)

    def test_includes_tenant_files(self):
        """Preview includes all tenant files."""
        config = {
            'ci': 'github',
            'deploy': 'kustomize',
            'tenants': ['db-a', 'db-b'],
        }
        files = ip._preview_files(config, '/tmp')
        file_str = ' '.join(files)
        assert 'db-a.yaml' in file_str
        assert 'db-b.yaml' in file_str

    def test_includes_github_workflow(self):
        """Preview includes GitHub Actions workflow when ci=github."""
        config = {
            'ci': 'github',
            'deploy': 'kustomize',
            'tenants': ['db-a'],
        }
        files = ip._preview_files(config, '/tmp')
        assert any('github/workflows' in f and '.yaml' in f for f in files)

    def test_includes_gitlab_pipeline(self, tmp_path):
        """Preview includes GitLab CI when ci=gitlab.

        ⛔ A real (empty) directory, not `/tmp`. Since #1357 the preview reads
        the target for a pre-existing root `.gitlab-ci.yml`, and `/tmp` on a
        shared machine is not a directory whose contents this test controls.
        """
        config = {
            'ci': 'gitlab',
            'deploy': 'kustomize',
            'tenants': ['db-a'],
        }
        files = ip._preview_files(config, str(tmp_path))
        assert any('gitlab-ci.d' in f for f in files)

    def test_includes_gitlab_root_shell_when_repo_has_none(self, tmp_path):
        """#1357 — the preview lists the root shell on a greenfield repo."""
        config = {'ci': 'gitlab', 'deploy': 'kustomize', 'tenants': ['db-a']}
        files = ip._preview_files(config, str(tmp_path))
        assert any(f.endswith('/.gitlab-ci.yml') for f in files), files

    def test_omits_gitlab_root_shell_when_repo_already_has_one(self, tmp_path):
        """…and does NOT list it when init would refuse to write it.

        An over-promise is the same defect as an under-promise: `--dry-run`
        would be telling a customer their existing root pipeline is about to be
        replaced, which is the fear the flag exists to settle.
        """
        (tmp_path / '.gitlab-ci.yml').write_text('stages: [build]\n', encoding='utf-8')
        config = {'ci': 'gitlab', 'deploy': 'kustomize', 'tenants': ['db-a']}
        files = ip._preview_files(config, str(tmp_path))
        assert not any(f.endswith('/.gitlab-ci.yml') for f in files), files
        assert any('gitlab-ci.d' in f for f in files), files

    def test_both_ci_includes_github_and_gitlab(self):
        """Preview includes both when ci=both."""
        config = {
            'ci': 'both',
            'deploy': 'kustomize',
            'tenants': ['db-a'],
        }
        files = ip._preview_files(config, '/tmp')
        file_str = ' '.join(files)
        assert 'github/workflows' in file_str
        assert 'gitlab-ci.d' in file_str

    def test_kustomize_includes_base_and_overlays(self):
        """Preview includes kustomize base and overlays."""
        config = {
            'ci': 'github',
            'deploy': 'kustomize',
            'tenants': ['db-a'],
        }
        files = ip._preview_files(config, '/tmp')
        file_str = ' '.join(files)
        assert 'kustomize/base' in file_str
        assert 'kustomize/overlays/dev' in file_str
        assert 'kustomize/overlays/prod' in file_str

    def test_non_kustomize_excludes_kustomize_files(self):
        """Preview excludes kustomize when deploy=helm."""
        config = {
            'ci': 'github',
            'deploy': 'helm',
            'tenants': ['db-a'],
        }
        files = ip._preview_files(config, '/tmp')
        assert not any('kustomize' in f for f in files)

    def test_includes_precommit_snippet(self):
        """Preview includes .pre-commit-config.da.yaml."""
        config = {
            'ci': 'github',
            'deploy': 'kustomize',
            'tenants': ['db-a'],
        }
        files = ip._preview_files(config, '/tmp')
        assert any('.pre-commit-config' in f for f in files)

    def test_includes_marker_file(self):
        """Preview includes .da-init.yaml."""
        config = {
            'ci': 'github',
            'deploy': 'kustomize',
            'tenants': ['db-a'],
        }
        files = ip._preview_files(config, '/tmp')
        assert any('.da-init.yaml' in f for f in files)


# ============================================================
# ── 13. run_init End-to-End ──
# ============================================================

class TestRunInit:
    """Tests for run_init — end-to-end file creation."""

    def test_creates_conf_d_defaults(self):
        """run_init creates conf.d/_defaults.yaml."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                'ci': 'github',
                'deploy': 'kustomize',
                'rule_packs': ['mariadb'],
                'tenants': ['db-a'],
                'namespace': 'monitoring',
                'da_tools_image': 'ghcr.io/vencil/da-tools:latest',
            }
            created = ip.run_init(config, tmpdir)
            defaults_path = os.path.join(tmpdir, 'conf.d', '_defaults.yaml')
            assert os.path.isfile(defaults_path)
            assert defaults_path in created

    def test_creates_tenant_files(self):
        """run_init creates tenant YAML files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                'ci': 'github',
                'deploy': 'kustomize',
                'rule_packs': ['mariadb'],
                'tenants': ['db-a', 'db-b'],
                'namespace': 'monitoring',
                'da_tools_image': 'ghcr.io/vencil/da-tools:latest',
            }
            created = ip.run_init(config, tmpdir)
            db_a_path = os.path.join(tmpdir, 'conf.d', 'db-a.yaml')
            db_b_path = os.path.join(tmpdir, 'conf.d', 'db-b.yaml')
            assert os.path.isfile(db_a_path)
            assert os.path.isfile(db_b_path)

    def test_creates_github_workflow(self):
        """run_init creates GitHub Actions workflow."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                'ci': 'github',
                'deploy': 'kustomize',
                'rule_packs': ['mariadb'],
                'tenants': ['db-a'],
                'namespace': 'monitoring',
                'da_tools_image': 'ghcr.io/vencil/da-tools:latest',
            }
            created = ip.run_init(config, tmpdir)
            workflow_path = os.path.join(tmpdir, '.github', 'workflows', 'dynamic-alerting.yaml')
            assert os.path.isfile(workflow_path)

    def test_creates_gitlab_pipeline(self):
        """run_init creates GitLab CI pipeline."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                'ci': 'gitlab',
                'deploy': 'kustomize',
                'rule_packs': ['mariadb'],
                'tenants': ['db-a'],
                'namespace': 'monitoring',
                'da_tools_image': 'ghcr.io/vencil/da-tools:latest',
            }
            created = ip.run_init(config, tmpdir)
            pipeline_path = os.path.join(tmpdir, '.gitlab-ci.d', 'dynamic-alerting.yml')
            assert os.path.isfile(pipeline_path)

    def test_creates_gitlab_root_shell(self):
        """#1357 — run_init also wires the pipeline into a root .gitlab-ci.yml."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                'ci': 'gitlab',
                'deploy': 'kustomize',
                'rule_packs': ['mariadb'],
                'tenants': ['db-a'],
                'namespace': 'monitoring',
                'da_tools_image': 'ghcr.io/vencil/da-tools:latest',
            }
            created = ip.run_init(config, tmpdir)
            root_path = os.path.join(tmpdir, '.gitlab-ci.yml')
            assert os.path.isfile(root_path)
            assert root_path in created
            doc = yaml.safe_load(open(root_path, encoding='utf-8').read())
            assert doc == {
                'include': [{'local': '.gitlab-ci.d/dynamic-alerting.yml'}]
            }, doc

    def test_leaves_an_existing_gitlab_root_shell_alone(self):
        """…and never touches one the customer already had (option B, #1357)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root_path = os.path.join(tmpdir, '.gitlab-ci.yml')
            original = 'stages: [build]\n\nbuild:\n  script:\n    - echo hi\n'
            with open(root_path, 'w', encoding='utf-8', newline='\n') as fh:
                fh.write(original)
            config = {
                'ci': 'gitlab',
                'deploy': 'kustomize',
                'rule_packs': ['mariadb'],
                'tenants': ['db-a'],
                'namespace': 'monitoring',
                'da_tools_image': 'ghcr.io/vencil/da-tools:latest',
            }
            created = ip.run_init(config, tmpdir)
            assert open(root_path, encoding='utf-8').read() == original
            assert root_path not in created

    def test_github_only_writes_no_gitlab_root_shell(self):
        """The root shell would `include:` a file --ci github never wrote."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                'ci': 'github',
                'deploy': 'kustomize',
                'rule_packs': ['mariadb'],
                'tenants': ['db-a'],
                'namespace': 'monitoring',
                'da_tools_image': 'ghcr.io/vencil/da-tools:latest',
            }
            ip.run_init(config, tmpdir)
            assert not os.path.exists(os.path.join(tmpdir, '.gitlab-ci.yml'))

    # ── Root-shell status detection (adversarial review round 1) ──────────
    #
    # Each of these pins a way the tool previously told the customer something
    # false about a file it refuses to touch. The status is what selects both
    # the sentence and the snippet SHAPE, and one of the wrong answers deletes
    # the customer's own `include:` entries.

    def _status_for(self, tmpdir, body):
        root = os.path.join(tmpdir, '.gitlab-ci.yml')
        with open(root, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(body)
        return ip._gitlab_root_shell_status(tmpdir)

    def test_root_file_mentioning_the_path_only_in_a_comment_is_not_wired(self):
        """A comment is not an include. Reading it as one prints an all-clear.

        The wired branch does not merely stay quiet — it says "nothing to do"
        and the closing line promises GitLab will validate. So a false positive
        here is #1357 re-created and then certified, with no in-tool route back
        (`--force` never rewrites an existing root file).
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            status = self._status_for(
                tmpdir,
                '# TODO: wire in .gitlab-ci.d/dynamic-alerting.yml someday\n'
                'stages: [build]\n',
            )
            assert status == ip._GL_ROOT_NEEDS_INCLUDE, status

    def test_root_file_with_its_own_include_asks_for_a_list_item(self):
        """⛔ Handing this repo an `include:` BLOCK destroys their entries.

        `include:` is a top-level mapping key; a second one is a duplicate key
        and YAML keeps exactly one. A customer who does what we print would
        silently lose their SAST/security includes.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            status = self._status_for(
                tmpdir,
                'include:\n'
                '  - local: .gitlab-ci.d/security.yml\n'
                '  - template: Jobs/SAST.gitlab-ci.yml\n'
                'stages: [build]\n',
            )
            assert status == ip._GL_ROOT_NEEDS_APPEND, status
            snippet = ip._gitlab_root_snippet_for(status)
            assert 'include:' not in snippet, snippet
            assert snippet.strip() == '- local: .gitlab-ci.d/dynamic-alerting.yml'

    def test_non_list_include_is_never_told_to_append_a_list_item(self):
        """⛔ `include:` also accepts a scalar and a single mapping.

        Telling the owner of one of those to "append this list item under your
        existing include:" produces a block sequence under a scalar — a YAML
        syntax error that stops their ENTIRE root pipeline from loading, not
        just ours. Measured before this split existed: the bare-string,
        single-mapping and empty-list shapes all failed to parse after
        following the printed instruction; only a true list survived.
        """
        for name, body in {
            'bare string': "include: 'templates/base.yml'\nstages: [build]\n",
            'single mapping': 'include:\n  template: Security/SAST.yml\n',
            'empty list': 'include: []\nstages: [build]\n',
        }.items():
            with tempfile.TemporaryDirectory() as tmpdir:
                status = self._status_for(tmpdir, body)
                assert status == ip._GL_ROOT_NEEDS_CONVERT, (name, status)

    def test_gitlab_strips_leading_slashes_so_we_must_too(self):
        """`local: /path` and `local: path` are the same file to GitLab.

        Its own docs use the leading slash in every `include:local` example,
        so comparing raw strings made a correctly wired repo read as unwired —
        and we then asked for a second include of the file it already loads.
        """
        for spelling in ('/.gitlab-ci.d/dynamic-alerting.yml',
                         './.gitlab-ci.d/dynamic-alerting.yml'):
            with tempfile.TemporaryDirectory() as tmpdir:
                status = self._status_for(
                    tmpdir, f'include:\n  - local: {spelling}\n')
                assert status == ip._GL_ROOT_ALREADY_WIRED, (spelling, status)

    def test_root_shell_only_names_stages_the_pipeline_declares(self):
        """⛔ The shell is shipped INTO the customer's repo and tells them
        which `stage:` values their own jobs may use. It named `generate`
        for two commits after #1358 deleted that stage — a customer following
        it gets a pipeline GitLab refuses to build, so nothing runs at all,
        validation included. Derive, do not retype.
        """
        shell = ip._gen_gitlab_root_shell()
        declared = yaml.safe_load(
            ip._gen_gitlab_ci('monitoring', 'img', 'kustomize'))['stages']
        assert list(ip._GL_STAGES) == declared, (ip._GL_STAGES, declared)
        named = re.search(r'drawn from that list \(([^)]*)\)', shell)
        assert named, shell
        assert [x.strip() for x in named.group(1).split(',')] == declared

    def test_force_does_not_rewrite_an_existing_root_gitlab_ci(self):
        """⛔ The `--force` help promises this exception; nothing tested it.

        `--force` is the most destructive flag the tool has — it exists to
        discard hand edits — and the one file it must never touch is the one
        that may be the customer's ENTIRE pipeline. A mutation that ORs
        `--force` into the root-shell write condition left the whole suite
        green, so the promise lived only in prose.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root_path = os.path.join(tmpdir, '.gitlab-ci.yml')
            original = 'stages: [build]\n\nbuild:\n  script:\n    - echo hi\n'
            with open(root_path, 'w', encoding='utf-8', newline='\n') as fh:
                fh.write(original)
            config = {
                'ci': 'gitlab',
                'deploy': 'kustomize',
                'rule_packs': ['mariadb'],
                'tenants': ['db-a'],
                'namespace': 'monitoring',
                'da_tools_image': 'ghcr.io/vencil/da-tools:latest',
                'force': True,
            }
            # Two runs: the second is the "already initialised, forced" shape.
            ip.run_init(config, tmpdir)
            created = ip.run_init(config, tmpdir)
            assert open(root_path, encoding='utf-8').read() == original, (
                '--force rewrote an existing root .gitlab-ci.yml, deleting '
                "the customer's own pipeline")
            assert root_path not in created

    def test_root_file_already_including_ours_is_wired(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            status = self._status_for(
                tmpdir,
                'include:\n  - local: .gitlab-ci.d/dynamic-alerting.yml\n',
            )
            assert status == ip._GL_ROOT_ALREADY_WIRED, status

    def test_include_shorthand_string_form_is_understood(self):
        """`include:` also accepts a bare string; that is still wired."""
        with tempfile.TemporaryDirectory() as tmpdir:
            status = self._status_for(
                tmpdir, 'include: .gitlab-ci.d/dynamic-alerting.yml\n')
            assert status == ip._GL_ROOT_ALREADY_WIRED, status

    def test_gitlab_custom_tags_degrade_safely_not_to_a_false_all_clear(self):
        """`!reference` is ordinary in a real pipeline and SafeLoader raises.

        ⛔ Pinning the DIRECTION of that loss, which is the whole point. A
        tag-tolerant loader would classify this file as wired, but installing
        one needs `yaml.load(..., Loader=...)` and the repo's SAST rule
        rejects that — it cannot tell a SafeLoader subclass from an unsafe
        load, and widening a security lint to win a nicer sentence is the
        wrong trade.

        So this file lands in UNPARSEABLE and its owner is asked to check the
        include they in fact already have: one redundant reminder. What must
        never happen is the other direction — a file GitLab does not load
        being reported as wired — and UNPARSEABLE cannot produce that.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            status = self._status_for(
                tmpdir,
                'include:\n  - local: .gitlab-ci.d/dynamic-alerting.yml\n'
                'job:\n  script:\n    - !reference [.setup, script]\n',
            )
            # It now classifies as needs-append: the document did not parse,
            # but a line scan can still see the `include:` key, and that is
            # exactly enough to pick the SAFE snippet shape (the list item)
            # without ever claiming the pipeline is wired.
            assert status == ip._GL_ROOT_NEEDS_APPEND, status
            assert status != ip._GL_ROOT_ALREADY_WIRED
            assert 'include:' not in ip._gitlab_root_snippet_for(status)
            assert not ip._gitlab_root_shell_is_needed(tmpdir)

    def test_unparseable_without_an_include_key_gets_the_block(self):
        """No parse AND no `include:` line — the whole block is safe here."""
        with tempfile.TemporaryDirectory() as tmpdir:
            status = self._status_for(
                tmpdir, 'job:\n  script:\n    - !reference [.a, script]\n')
            assert status == ip._GL_ROOT_UNPARSEABLE, status
            assert 'include:' in ip._gitlab_root_snippet_for(status)

    def test_unparseable_root_file_is_never_reported_as_wired(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            status = self._status_for(tmpdir, 'include: [unclosed\n')
            # The property that matters is never "wired" — the shape choice
            # may legitimately come from the line scan.
            assert status != ip._GL_ROOT_ALREADY_WIRED, status
            assert not ip._gitlab_root_shell_is_needed(tmpdir)

    def test_dangling_symlink_at_the_root_path_is_not_written_through(self):
        """`exists()` follows symlinks, so a broken one reads as "no file".

        The writer follows it too, planting our shell outside --output-dir.
        This is the one existence check whose job is protecting a
        customer-owned path, so it must fail closed.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            outside = os.path.join(tmpdir, 'outside', 'ESCAPE.yml')
            os.makedirs(os.path.dirname(outside))
            target = os.path.join(tmpdir, 'repo')
            os.makedirs(target)
            os.symlink(outside, os.path.join(target, '.gitlab-ci.yml'))
            config = {
                'ci': 'gitlab',
                'deploy': 'kustomize',
                'rule_packs': ['mariadb'],
                'tenants': ['db-a'],
                'namespace': 'monitoring',
                'da_tools_image': 'ghcr.io/vencil/da-tools:latest',
            }
            ip.run_init(config, target)
            assert not os.path.exists(outside), (
                'run_init followed a dangling symlink and wrote the root shell '
                'outside --output-dir')

    def test_creates_kustomize_base(self):
        """run_init creates kustomize/base/kustomization.yaml."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                'ci': 'github',
                'deploy': 'kustomize',
                'rule_packs': ['mariadb'],
                'tenants': ['db-a'],
                'namespace': 'monitoring',
                'da_tools_image': 'ghcr.io/vencil/da-tools:latest',
            }
            created = ip.run_init(config, tmpdir)
            kust_path = os.path.join(tmpdir, 'kustomize', 'base', 'kustomization.yaml')
            assert os.path.isfile(kust_path)

    def test_creates_kustomize_overlays(self):
        """run_init creates kustomize overlays (dev, prod)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                'ci': 'github',
                'deploy': 'kustomize',
                'rule_packs': ['mariadb'],
                'tenants': ['db-a'],
                'namespace': 'monitoring',
                'da_tools_image': 'ghcr.io/vencil/da-tools:latest',
            }
            created = ip.run_init(config, tmpdir)
            dev_path = os.path.join(tmpdir, 'kustomize', 'overlays', 'dev', 'kustomization.yaml')
            prod_path = os.path.join(tmpdir, 'kustomize', 'overlays', 'prod', 'kustomization.yaml')
            assert os.path.isfile(dev_path)
            assert os.path.isfile(prod_path)

    def test_creates_precommit_snippet(self):
        """run_init creates .pre-commit-config.da.yaml."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                'ci': 'github',
                'deploy': 'kustomize',
                'rule_packs': ['mariadb'],
                'tenants': ['db-a'],
                'namespace': 'monitoring',
                'da_tools_image': 'ghcr.io/vencil/da-tools:latest',
            }
            created = ip.run_init(config, tmpdir)
            precommit_path = os.path.join(tmpdir, '.pre-commit-config.da.yaml')
            assert os.path.isfile(precommit_path)

    def test_creates_marker_file(self):
        """run_init creates .da-init.yaml marker."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                'ci': 'github',
                'deploy': 'kustomize',
                'rule_packs': ['mariadb'],
                'tenants': ['db-a'],
                'namespace': 'monitoring',
                'da_tools_image': 'ghcr.io/vencil/da-tools:latest',
            }
            created = ip.run_init(config, tmpdir)
            marker_path = os.path.join(tmpdir, '.da-init.yaml')
            assert os.path.isfile(marker_path)

    def test_returns_list_of_created_files(self):
        """run_init returns list of all created file paths."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                'ci': 'github',
                'deploy': 'kustomize',
                'rule_packs': ['mariadb'],
                'tenants': ['db-a'],
                'namespace': 'monitoring',
                'da_tools_image': 'ghcr.io/vencil/da-tools:latest',
            }
            created = ip.run_init(config, tmpdir)
            assert isinstance(created, list)
            assert len(created) > 0
            # All paths should exist
            for path in created:
                assert os.path.exists(path), f"{path} was not created"

    def test_defaults_yaml_is_valid_yaml(self):
        """Created _defaults.yaml is valid YAML."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                'ci': 'github',
                'deploy': 'kustomize',
                'rule_packs': ['mariadb'],
                'tenants': ['db-a'],
                'namespace': 'monitoring',
                'da_tools_image': 'ghcr.io/vencil/da-tools:latest',
            }
            ip.run_init(config, tmpdir)
            defaults_path = os.path.join(tmpdir, 'conf.d', '_defaults.yaml')
            with open(defaults_path, 'r', encoding='utf-8') as f:
                config_data = yaml.safe_load(f)
            assert 'defaults' in config_data

    def test_tenant_yaml_is_valid_yaml(self):
        """Created tenant YAML is valid YAML."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                'ci': 'github',
                'deploy': 'kustomize',
                'rule_packs': ['mariadb'],
                'tenants': ['db-a'],
                'namespace': 'monitoring',
                'da_tools_image': 'ghcr.io/vencil/da-tools:latest',
            }
            ip.run_init(config, tmpdir)
            tenant_path = os.path.join(tmpdir, 'conf.d', 'db-a.yaml')
            with open(tenant_path, 'r', encoding='utf-8') as f:
                tenant_data = yaml.safe_load(f)
            assert 'tenants' in tenant_data

    def test_marker_contains_correct_metadata(self):
        """Marker file contains correct metadata."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                'ci': 'both',
                'deploy': 'helm',
                'rule_packs': ['mariadb', 'redis'],
                'tenants': ['db-a', 'db-b'],
                'namespace': 'custom-ns',
                'da_tools_image': 'ghcr.io/vencil/da-tools:latest',
            }
            ip.run_init(config, tmpdir)
            marker_path = os.path.join(tmpdir, '.da-init.yaml')
            with open(marker_path, 'r', encoding='utf-8') as f:
                marker_data = yaml.safe_load(f)
            assert marker_data['ci_platform'] == 'both'
            assert marker_data['deploy_method'] == 'helm'
            assert 'mariadb' in marker_data['rule_packs']
            assert 'redis' in marker_data['rule_packs']
            assert 'db-a' in marker_data['tenants']
            assert 'db-b' in marker_data['tenants']


# ============================================================
# ── 14. CI/Deploy Combinations (Parametrized) ──
# ============================================================

# ⛔ Hand-written on purpose — do NOT rewrite this as a product of
# `_build_parser()`'s `choices`. It is the INDEPENDENT source that
# tests/ops/test_generated_ci_artifacts.py compares its parser-derived matrix
# against (anti-vacuity floor). Derive both from the same call and the
# comparison becomes a tautology: a `--deploy` choice silently dropped from
# the CLI would shrink the matrix and every test would still pass, just with
# fewer cases. Adding a fourth deploy method therefore reds that comparison
# until this list is updated too — which is the point.
CI_DEPLOY_COMBINATIONS = [
    ('github', 'kustomize'),
    ('github', 'helm'),
    ('github', 'argocd'),
    ('gitlab', 'kustomize'),
    ('gitlab', 'helm'),
    ('gitlab', 'argocd'),
    ('both', 'kustomize'),
    ('both', 'helm'),
    ('both', 'argocd'),
]


@pytest.mark.parametrize('ci,deploy', CI_DEPLOY_COMBINATIONS)
class TestCiDeployCombinations:
    """Tests for all CI/deploy combinations."""

    def test_valid_combination(self, ci, deploy):
        """All CI/deploy combinations produce valid output."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                'ci': ci,
                'deploy': deploy,
                'rule_packs': ['mariadb'],
                'tenants': ['db-a'],
                'namespace': 'monitoring',
                'da_tools_image': 'ghcr.io/vencil/da-tools:latest',
            }
            created = ip.run_init(config, tmpdir)
            assert len(created) > 0
            # All files should exist
            for path in created:
                assert os.path.exists(path)


# ============================================================
# ── 15. Marker File Detection ──
# ============================================================

class TestMarkerFileDetection:
    """Tests for existing .da-init.yaml marker detection (via run_init return)."""

    def test_marker_file_prevents_reinit(self):
        """Existing .da-init.yaml marker can be detected for re-init prevention."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                'ci': 'github',
                'deploy': 'kustomize',
                'rule_packs': ['mariadb'],
                'tenants': ['db-a'],
                'namespace': 'monitoring',
                'da_tools_image': 'ghcr.io/vencil/da-tools:latest',
            }
            created1 = ip.run_init(config, tmpdir)
            marker_path = os.path.join(tmpdir, '.da-init.yaml')
            assert os.path.isfile(marker_path)

            # Now marker exists; re-init in same dir would fail in main()
            # but we verify the marker file is readable
            with open(marker_path, 'r', encoding='utf-8') as f:
                marker_data = yaml.safe_load(f)
            assert 'version' in marker_data
            assert 'generated_at' in marker_data

    def test_marker_preserves_initialization_context(self):
        """Marker preserves all initialization parameters."""
        with tempfile.TemporaryDirectory() as tmpdir:
            original_config = {
                'ci': 'gitlab',
                'deploy': 'argocd',
                'rule_packs': ['postgresql', 'kafka', 'elasticsearch'],
                'tenants': ['prod-db', 'staging-db'],
                'namespace': 'infra',
                'da_tools_image': 'ghcr.io/custom/da-tools:v2.0',
            }
            ip.run_init(original_config, tmpdir)
            marker_path = os.path.join(tmpdir, '.da-init.yaml')
            with open(marker_path, 'r', encoding='utf-8') as f:
                marker = yaml.safe_load(f)

            assert marker['ci_platform'] == 'gitlab'
            assert marker['deploy_method'] == 'argocd'
            assert set(marker['rule_packs']) == {'postgresql', 'kafka', 'elasticsearch'}
            assert marker['tenants'] == ['prod-db', 'staging-db']


# ============================================================
# ── 16. File Content Validation ──
# ============================================================

class TestFileContentValidation:
    """Tests for validity of generated file contents."""

    def test_github_workflow_parses_as_yaml(self):
        """Generated GitHub workflow is valid YAML."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                'ci': 'github',
                'deploy': 'kustomize',
                'rule_packs': ['mariadb'],
                'tenants': ['db-a'],
                'namespace': 'monitoring',
                'da_tools_image': 'ghcr.io/vencil/da-tools:latest',
            }
            ip.run_init(config, tmpdir)
            workflow_path = os.path.join(tmpdir, '.github', 'workflows', 'dynamic-alerting.yaml')
            with open(workflow_path, 'r', encoding='utf-8') as f:
                workflow = yaml.safe_load(f)
            assert workflow is not None
            assert 'jobs' in workflow

    def test_gitlab_pipeline_parses_as_yaml(self):
        """Generated GitLab pipeline is valid YAML."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                'ci': 'gitlab',
                'deploy': 'kustomize',
                'rule_packs': ['mariadb'],
                'tenants': ['db-a'],
                'namespace': 'monitoring',
                'da_tools_image': 'ghcr.io/vencil/da-tools:latest',
            }
            ip.run_init(config, tmpdir)
            pipeline_path = os.path.join(tmpdir, '.gitlab-ci.d', 'dynamic-alerting.yml')
            with open(pipeline_path, 'r', encoding='utf-8') as f:
                pipeline = yaml.safe_load(f)
            assert pipeline is not None
            assert 'stages' in pipeline

    def test_kustomize_base_parses_as_yaml(self):
        """Generated kustomize base is valid YAML."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                'ci': 'github',
                'deploy': 'kustomize',
                'rule_packs': ['mariadb'],
                'tenants': ['db-a'],
                'namespace': 'monitoring',
                'da_tools_image': 'ghcr.io/vencil/da-tools:latest',
            }
            ip.run_init(config, tmpdir)
            base_path = os.path.join(tmpdir, 'kustomize', 'base', 'kustomization.yaml')
            with open(base_path, 'r', encoding='utf-8') as f:
                base = yaml.safe_load(f)
            assert base is not None
            assert 'configMapGenerator' in base

    def test_precommit_parses_as_yaml(self):
        """Generated pre-commit config is valid YAML."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                'ci': 'github',
                'deploy': 'kustomize',
                'rule_packs': ['mariadb'],
                'tenants': ['db-a'],
                'namespace': 'monitoring',
                'da_tools_image': 'ghcr.io/vencil/da-tools:latest',
            }
            ip.run_init(config, tmpdir)
            precommit_path = os.path.join(tmpdir, '.pre-commit-config.da.yaml')
            with open(precommit_path, 'r', encoding='utf-8') as f:
                precommit = yaml.safe_load(f)
            assert precommit is not None
            assert 'repos' in precommit


# ============================================================
# ── 17. Edge Cases ──
# ============================================================

class TestEdgeCases:
    """Tests for edge cases and boundary conditions."""

    def test_single_tenant(self):
        """Initialization with single tenant works."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                'ci': 'github',
                'deploy': 'kustomize',
                'rule_packs': ['mariadb'],
                'tenants': ['db-a'],
                'namespace': 'monitoring',
                'da_tools_image': 'ghcr.io/vencil/da-tools:latest',
            }
            created = ip.run_init(config, tmpdir)
            assert len(created) > 0

    def test_many_tenants(self):
        """Initialization with many tenants works."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                'ci': 'github',
                'deploy': 'kustomize',
                'rule_packs': ['mariadb'],
                'tenants': [f'db-{i}' for i in range(10)],
                'namespace': 'monitoring',
                'da_tools_image': 'ghcr.io/vencil/da-tools:latest',
            }
            created = ip.run_init(config, tmpdir)
            # Should have defaults + 10 tenant files + workflow + kustomize + other
            assert len(created) >= 10

    def test_single_rule_pack(self):
        """Initialization with single rule pack works."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                'ci': 'github',
                'deploy': 'kustomize',
                'rule_packs': ['mariadb'],
                'tenants': ['db-a'],
                'namespace': 'monitoring',
                'da_tools_image': 'ghcr.io/vencil/da-tools:latest',
            }
            created = ip.run_init(config, tmpdir)
            defaults_path = os.path.join(tmpdir, 'conf.d', '_defaults.yaml')
            with open(defaults_path, 'r', encoding='utf-8') as f:
                defaults = yaml.safe_load(f)
            assert 'mysql_connections' in defaults['defaults']

    def test_all_selectable_rule_packs(self):
        """Initialization with all 13 selectable packs works."""
        with tempfile.TemporaryDirectory() as tmpdir:
            selectable = ip._selectable_rule_packs()
            config = {
                'ci': 'github',
                'deploy': 'kustomize',
                'rule_packs': selectable,
                'tenants': ['db-a'],
                'namespace': 'monitoring',
                'da_tools_image': 'ghcr.io/vencil/da-tools:latest',
            }
            created = ip.run_init(config, tmpdir)
            assert len(created) > 0
            defaults_path = os.path.join(tmpdir, 'conf.d', '_defaults.yaml')
            with open(defaults_path, 'r', encoding='utf-8') as f:
                defaults = yaml.safe_load(f)
            # Should have metrics from all packs
            assert len(defaults['defaults']) > 50  # 65 keys from 13 packs

    def test_custom_namespace(self):
        """Initialization with custom namespace works."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                'ci': 'github',
                'deploy': 'kustomize',
                'rule_packs': ['mariadb'],
                'tenants': ['db-a'],
                'namespace': 'custom-monitoring',
                'da_tools_image': 'ghcr.io/vencil/da-tools:latest',
            }
            created = ip.run_init(config, tmpdir)
            kust_path = os.path.join(tmpdir, 'kustomize', 'base', 'kustomization.yaml')
            with open(kust_path, 'r', encoding='utf-8') as f:
                kust = yaml.safe_load(f)
            assert kust['namespace'] == 'custom-monitoring'

    def test_custom_da_tools_image(self):
        """Initialization with custom da-tools image works."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                'ci': 'github',
                'deploy': 'kustomize',
                'rule_packs': ['mariadb'],
                'tenants': ['db-a'],
                'namespace': 'monitoring',
                'da_tools_image': 'registry.example.com/da-tools:v1.0',
            }
            created = ip.run_init(config, tmpdir)
            workflow_path = os.path.join(tmpdir, '.github', 'workflows', 'dynamic-alerting.yaml')
            with open(workflow_path, 'r', encoding='utf-8') as f:
                content = f.read()
            assert 'registry.example.com/da-tools:v1.0' in content


class TestMainTenantValidation:
    """Test tenant name validation in main() non-interactive path."""

    def test_invalid_tenant_name_exits(self, capsys):
        """Non-interactive mode rejects invalid K8s tenant names."""
        sys.argv = ['init', '--ci', 'github', '--tenants', 'UPPER_CASE',
                     '--non-interactive', '-o', '/tmp/test-ignore']
        with pytest.raises(SystemExit) as exc:
            ip.main()
        assert exc.value.code == EXIT_CALLER_ERROR
        captured = capsys.readouterr()
        assert 'UPPER_CASE' in captured.err

    def test_empty_tenants_exits(self, capsys):
        """Non-interactive mode rejects empty tenant list."""
        sys.argv = ['init', '--ci', 'github', '--tenants', ',,,',
                     '--non-interactive', '-o', '/tmp/test-ignore']
        with pytest.raises(SystemExit) as exc:
            ip.main()
        assert exc.value.code == EXIT_CALLER_ERROR

    def test_mixed_valid_invalid_exits(self, capsys):
        """Rejects if any tenant name is invalid."""
        sys.argv = ['init', '--ci', 'github', '--tenants', 'good-name,Bad!Name',
                     '--non-interactive', '-o', '/tmp/test-ignore']
        with pytest.raises(SystemExit) as exc:
            ip.main()
        assert exc.value.code == EXIT_CALLER_ERROR
        captured = capsys.readouterr()
        assert 'Bad!Name' in captured.err

    def test_valid_tenants_pass(self):
        """Valid K8s-compliant names pass validation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sys.argv = ['init', '--ci', 'github', '--tenants', 'db-a,prod-01',
                         '--non-interactive', '-o', tmpdir]
            ip.main()
            # Should succeed and create files
            assert os.path.isfile(os.path.join(tmpdir, '.da-init.yaml'))


class TestGitOpsNativeMode:
    """Test --config-source git generates git-sync sidecar overlay."""

    def test_git_source_creates_overlay(self):
        """--config-source git generates kustomize/overlays/gitops/."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                'ci': 'github', 'deploy': 'kustomize',
                'rule_packs': ['mariadb'], 'tenants': ['db-a'],
                'namespace': 'monitoring',
                'da_tools_image': 'ghcr.io/vencil/da-tools:latest',
                'config_source': 'git',
                'git_repo': 'git@github.com:example/configs.git',
                'git_branch': 'main',
                'git_path': 'conf.d',
            }
            created = ip.run_init(config, tmpdir)
            gitops_dir = os.path.join(tmpdir, 'kustomize', 'overlays', 'gitops')
            assert os.path.isfile(os.path.join(gitops_dir, 'kustomization.yaml'))
            assert os.path.isfile(os.path.join(gitops_dir, 'git-sync-patch.yaml'))

    def test_git_sync_patch_contains_repo_url(self):
        """git-sync-patch.yaml references the configured repo URL."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                'ci': 'github', 'deploy': 'kustomize',
                'rule_packs': ['mariadb'], 'tenants': ['db-a'],
                'namespace': 'monitoring',
                'da_tools_image': 'ghcr.io/vencil/da-tools:latest',
                'config_source': 'git',
                'git_repo': 'https://github.com/myorg/myrepo.git',
                'git_branch': 'production',
                'git_path': 'alerting/conf.d',
            }
            ip.run_init(config, tmpdir)
            patch_path = os.path.join(tmpdir, 'kustomize', 'overlays', 'gitops', 'git-sync-patch.yaml')
            with open(patch_path, encoding='utf-8') as f:
                content = f.read()
            assert 'https://github.com/myorg/myrepo.git' in content
            assert 'production' in content
            assert 'alerting/conf.d' in content

    def test_configmap_mode_no_gitops_overlay(self):
        """Default configmap mode does not create gitops overlay."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                'ci': 'github', 'deploy': 'kustomize',
                'rule_packs': ['mariadb'], 'tenants': ['db-a'],
                'namespace': 'monitoring',
                'da_tools_image': 'ghcr.io/vencil/da-tools:latest',
                'config_source': 'configmap',
                'git_repo': None, 'git_branch': 'main', 'git_path': 'conf.d',
            }
            ip.run_init(config, tmpdir)
            gitops_dir = os.path.join(tmpdir, 'kustomize', 'overlays', 'gitops')
            assert not os.path.exists(gitops_dir)

    def test_marker_records_config_source(self):
        """Marker file records config_source and git_repo."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                'ci': 'github', 'deploy': 'kustomize',
                'rule_packs': ['mariadb'], 'tenants': ['db-a'],
                'namespace': 'monitoring',
                'da_tools_image': 'ghcr.io/vencil/da-tools:latest',
                'config_source': 'git',
                'git_repo': 'git@github.com:example/configs.git',
                'git_branch': 'main', 'git_path': 'conf.d',
            }
            ip.run_init(config, tmpdir)
            with open(os.path.join(tmpdir, '.da-init.yaml'), encoding='utf-8') as f:
                marker = yaml.safe_load(f)
            assert marker['config_source'] == 'git'
            assert marker['git_repo'] == 'git@github.com:example/configs.git'

    def test_git_sync_kustomization_references_patch(self):
        """kustomization.yaml references the git-sync-patch.yaml."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                'ci': 'github', 'deploy': 'kustomize',
                'rule_packs': ['mariadb'], 'tenants': ['db-a'],
                'namespace': 'monitoring',
                'da_tools_image': 'ghcr.io/vencil/da-tools:latest',
                'config_source': 'git',
                'git_repo': 'git@github.com:example/configs.git',
                'git_branch': 'main', 'git_path': 'conf.d',
            }
            ip.run_init(config, tmpdir)
            kust_path = os.path.join(tmpdir, 'kustomize', 'overlays', 'gitops', 'kustomization.yaml')
            with open(kust_path, encoding='utf-8') as f:
                kust = yaml.safe_load(f)
            assert 'patches' in kust
            assert any('git-sync-patch.yaml' in str(p) for p in kust['patches'])

    def test_git_sync_patch_has_init_container(self):
        """git-sync-patch.yaml includes initContainer with --one-time."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                'ci': 'github', 'deploy': 'kustomize',
                'rule_packs': ['mariadb'], 'tenants': ['db-a'],
                'namespace': 'monitoring',
                'da_tools_image': 'ghcr.io/vencil/da-tools:latest',
                'config_source': 'git',
                'git_repo': 'git@github.com:example/configs.git',
                'git_branch': 'main', 'git_path': 'conf.d',
            }
            ip.run_init(config, tmpdir)
            patch_path = os.path.join(tmpdir, 'kustomize', 'overlays', 'gitops', 'git-sync-patch.yaml')
            with open(patch_path, encoding='utf-8') as f:
                patch = yaml.safe_load(f)
            spec = patch['spec']['template']['spec']
            # Verify initContainer exists with --one-time
            init_containers = spec.get('initContainers', [])
            assert len(init_containers) == 1
            assert init_containers[0]['name'] == 'git-sync-init'
            assert '--one-time' in init_containers[0]['args']

    def test_git_sync_custom_period(self):
        """--git-period sets the sidecar polling interval."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                'ci': 'github', 'deploy': 'kustomize',
                'rule_packs': ['mariadb'], 'tenants': ['db-a'],
                'namespace': 'monitoring',
                'da_tools_image': 'ghcr.io/vencil/da-tools:latest',
                'config_source': 'git',
                'git_repo': 'git@github.com:example/configs.git',
                'git_branch': 'main', 'git_path': 'conf.d',
                'git_period': 30,
            }
            ip.run_init(config, tmpdir)
            patch_path = os.path.join(tmpdir, 'kustomize', 'overlays', 'gitops', 'git-sync-patch.yaml')
            with open(patch_path, encoding='utf-8') as f:
                content = f.read()
            assert '--period=30s' in content

    def test_git_sync_exporter_reads_current_symlink(self):
        """Exporter config-dir path includes /current/ for git-sync symlink."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                'ci': 'github', 'deploy': 'kustomize',
                'rule_packs': ['mariadb'], 'tenants': ['db-a'],
                'namespace': 'monitoring',
                'da_tools_image': 'ghcr.io/vencil/da-tools:latest',
                'config_source': 'git',
                'git_repo': 'git@github.com:example/configs.git',
                'git_branch': 'main', 'git_path': 'alerting/conf.d',
            }
            ip.run_init(config, tmpdir)
            patch_path = os.path.join(tmpdir, 'kustomize', 'overlays', 'gitops', 'git-sync-patch.yaml')
            with open(patch_path, encoding='utf-8') as f:
                patch = yaml.safe_load(f)
            exporter = patch['spec']['template']['spec']['containers'][0]
            assert exporter['name'] == 'threshold-exporter'
            assert '/data/config/current/alerting/conf.d' in exporter['args'][0]


# ============================================================
# ── Customer-delivered image pins (#1337 ②④) ──
# ============================================================

class TestCustomerDeliveredImagePins:
    """Every container image ref handed to a customer must name a version.

    These refs land in files the CUSTOMER executes: the GitLab apply stage
    carries `environment: name: production` plus cluster-write credentials,
    and the git-sync patch is applied straight into their cluster. A floating
    tag there moves under them with no change on their side — which is what
    had already happened when #1337 was written: `alpine/helm:latest` walked
    existing pipelines across the Helm 3 -> 4 major boundary, and
    `bitnami/kubectl` lost every version tag when Broadcom moved the free
    catalog to `latest`-only.

    ⛔ Scope is deliberately THIRD-PARTY refs. First-party `ghcr.io/vencil/*`
    currency is the release flow's job, not a pin here: #902 considered
    first-party digest pinning and REJECTED it. Widening this guard to
    first-party would silently reverse that decision.

    ⚠️ Honest boundary — this suite checks FLOATING vs CONCRETE, never
    CURRENCY. Reverting git-sync to the stale v4.4.0 leaves every test below
    green (measured, not assumed), because v4.4.0 is a perfectly concrete tag.
    Nothing offline can tell you a pin has gone stale; that needs a registry
    round-trip, and git-sync publishes no advisory feed to subscribe to
    either. Do not read a green run here as "the pins are current".
    """

    _FIRST_PARTY = 'ghcr.io/vencil/'
    _FLOATING = ('latest', 'main', 'master', 'edge', 'stable')
    _FLOATING_RE = re.compile(r'[\w.\-/]+:(?:latest|main|master|edge|stable)\b')

    @staticmethod
    def _pipeline(deploy, da_tools_image='ghcr.io/vencil/da-tools:v9.9.9'):
        return yaml.safe_load(ip._gen_gitlab_ci('monitoring', da_tools_image, deploy))

    @staticmethod
    def _tag_of(ref):
        """The tag portion, or None when the ref names none.

        ⛔ Splits after the LAST `/` so a registry PORT is not read as a tag.
        A naive `rpartition(':')` reports `quay.io:443/argoproj/argocd` as
        tag=`443/argoproj/argocd` — concrete-looking, while Docker resolves it
        to `:latest`. That exact ref survived an earlier version of this suite.
        """
        last = ref.split('@', 1)[0].rsplit('/', 1)[-1]
        return last.rsplit(':', 1)[1] if ':' in last else None

    def test_pin_table_entries_are_concrete(self):
        """Every pinned ref names a repository AND a non-floating tag."""
        refs = [ref for _, ref in ip._GITLAB_APPLY_IMAGES.values()] + [ip.GIT_SYNC_IMAGE]
        assert len(refs) >= 4, 'anti-vacuity: the pin table shrank unexpectedly'
        for ref in refs:
            tag = self._tag_of(ref)
            assert tag is not None, (
                f'{ref!r} names no tag — Docker resolves that to `:latest`, so '
                f'the omission is a floating reference written a different way')
            assert tag not in self._FLOATING, f'{ref!r} uses a floating tag'
            assert not re.fullmatch(r'v?\d+', tag), (
                f'{ref!r} pins only a major version ({tag!r}), which upstream '
                f'moves — pin the full release')

    def test_helm_image_stays_on_the_helm_3_line(self):
        """The Helm 3 pin is a DECISION, so it needs a gate, not a comment.

        `alpine/helm:latest` resolving to Helm 4 is the headline defect this
        change exists to fix; a later "just bump it" would reproduce it exactly,
        and every other assertion in this class would stay green because 4.x is
        a perfectly concrete tag. Bumping to Helm 4 is allowed — but it has to
        be a deliberate edit here, taken together with the generated
        `helm upgrade` flags, which Helm 4 changed.
        """
        _, ref = ip._GITLAB_APPLY_IMAGES['helm']
        tag = self._tag_of(ref)
        major = tag.lstrip('v').split('.', 1)[0]
        assert major == '3', (
            f'helm image is pinned to {ref!r} (major {major}). Helm 4 carries '
            f'"backward incompatible changes ... to the flags and output of the '
            f'Helm CLI"; if this move is intended, update the generated '
            f'`helm upgrade` invocation and this test together.')

    # Substring that must appear in the image chosen for each deploy method.
    # Derived from the tool the branch actually runs, so a table entry pointing
    # at the wrong image fails even though that image is perfectly concrete.
    #
    # ⛔ This checks the repository NAME, which is a far weaker property than
    # "the image can run the branch's commands". `vendor/kubectl-notreally:v1`
    # passes. Capability cannot be checked offline at all — see
    # `_MEASURED_UNUSABLE` for the one thing that CAN be pinned about it.
    _IMAGE_IDENTITY = {
        'kustomize': ('k8s', 'kubectl'),
        'helm': ('helm',),
        'argocd': ('argocd',),
    }

    # Repositories MEASURED to be unusable for a GitLab `script:` job, with the
    # date and the observation. Offline we cannot prove an image *works*; we can
    # refuse to silently re-adopt one we already proved does not.
    #
    # ⛔ This exists because the attractive-looking wrong answer is written down
    # in `init_project.py` itself: that file explains at length why
    # `registry.k8s.io/kubectl` is the Kubernetes project's own image and ships
    # no `latest` tag. Both facts are true, and a future reader can reasonably
    # conclude it is the better pin — which is exactly how the regression this
    # ledger guards against was introduced the first time.
    @staticmethod
    def _repo_of(ref):
        """Repository portion of a ref, tolerating a registry port.

        ⛔ A naive `rsplit(':', 1)[0]` turns `quay.io:443/argoproj/argocd` into
        `quay.io` — the same trap the tag parser in the try-local guard had to
        fix. Only strip a colon that lives in the LAST path segment.
        """
        repo = ref.split('@', 1)[0]
        return repo.rsplit(':', 1)[0] if ':' in repo.rsplit('/', 1)[-1] else repo

    _MEASURED_UNUSABLE = {
        'registry.k8s.io/kubectl':
            'distroless-static: no /bin/sh (measured 2026-08-05, '
            '`docker run --entrypoint sh` -> exec: "sh": not found). GitLab '
            'writes the script: block into a shell, so the job cannot start '
            'and no `entrypoint:` override can rescue it.',
        'bitnami/kubectl':
            'free namespace carries only `latest` since 2025-09-29 (every '
            'version tag 404s), and the image ships no `kustomize` binary '
            'while the kustomize branch runs `kustomize build`.',
    }

    def test_pin_table_covers_exactly_the_methods_the_generator_branches_on(self):
        """Adding a row to the pin table must not skip the identity check.

        `_IMAGE_IDENTITY` used to be the parametrize source AND the lookup, so a
        new pin-table row was simply never visited — measured: adding a `flux`
        row left all tests green while `--deploy flux` fell through to the
        argocd script running inside whatever image the new row named.
        """
        assert set(ip._GITLAB_APPLY_IMAGES) == set(self._IMAGE_IDENTITY), (
            'pin table and identity table disagree:\n'
            f'  in pin table only: {sorted(set(ip._GITLAB_APPLY_IMAGES) - set(self._IMAGE_IDENTITY))}\n'
            f'  in identity only:  {sorted(set(self._IMAGE_IDENTITY) - set(ip._GITLAB_APPLY_IMAGES))}')
        assert set(ip._GITLAB_APPLY_IMAGES) == set(self._SCRIPT_MARKER), (
            'pin table and script-marker table disagree — a method with no '
            'marker would make test_apply_image_matches_the_command_it_runs '
            'unable to identify its branch')

    @pytest.mark.parametrize('deploy', sorted(_IMAGE_IDENTITY))
    def test_pin_table_avoids_images_measured_unusable(self, deploy):
        """A repository we already proved cannot run the job stays out.

        ⚠️ The inverse does NOT hold: absence from this ledger is not evidence
        that an image works. This is a memory of past measurements, not a
        capability check.
        """
        _, ref = ip._GITLAB_APPLY_IMAGES[deploy]
        repo = self._repo_of(ref)
        assert repo not in self._MEASURED_UNUSABLE, (
            f'{deploy!r} is mapped to {repo!r}, which was measured unusable: '
            f'{self._MEASURED_UNUSABLE[repo]}')

    @pytest.mark.parametrize('deploy', sorted(_IMAGE_IDENTITY))
    def test_pin_table_maps_each_method_to_an_image_of_the_right_kind(self, deploy):
        """The pin TABLE itself can be wrong, and nothing else notices.

        Swapping the kustomize and helm rows leaves every other test in this
        class green while emitting `kubectl apply` into an image that has no
        kubectl — on a job carrying production cluster credentials. The variable
        NAME is not evidence either; it is chosen in the same dict.
        """
        var, ref = ip._GITLAB_APPLY_IMAGES[deploy]
        repo = self._repo_of(ref).lower()
        wanted = self._IMAGE_IDENTITY[deploy]
        assert any(w in repo for w in wanted), (
            f'{deploy!r} is mapped to {ref!r}, whose repository does not even '
            f'name any of {wanted}. ⚠️ Passing this proves only that the NAME '
            f'mentions the tool — not that the image contains it')
        assert any(w in var.lower() for w in wanted), (
            f'{deploy!r} declares {var!r}, which does not name {wanted}')

    @pytest.mark.parametrize('deploy', ['kustomize', 'helm', 'argocd'])
    def test_apply_stage_uses_a_declared_variable_not_a_literal(self, deploy):
        """The job must reference `$VAR` and `variables:` must declare that VAR.

        ⚠️ Honest about what this proves: both sides read `_gitlab_apply_image`,
        so the value comparison is agree-by-construction and can only catch the
        rendering losing a line — NOT a wrong pin. The pin's correctness is
        covered by `test_pin_table_maps_each_method_to_an_image_of_the_right_kind`
        and `test_apply_image_matches_the_command_it_runs`, which re-derive from
        an independent side.
        """
        doc = self._pipeline(deploy)
        image = doc['apply']['image']
        assert isinstance(image, dict), (
            f'apply stage must use the mapping form so it can carry '
            f'`entrypoint: [""]`; got {image!r}')
        assert image.get('entrypoint') == [''], (
            'the entrypoint override is what lets a tool-entrypoint image '
            '(alpine/helm) run a GitLab `script:` block at all')
        name = image['name']
        assert name.startswith('$'), f'apply stage pins a literal ref: {name!r}'
        var = name[1:]
        expected_var, expected_ref = ip._gitlab_apply_image(deploy)
        assert var == expected_var, f'job references ${var}, table says ${expected_var}'
        assert doc['variables'].get(var) == expected_ref, (
            f'variables: declares {doc["variables"].get(var)!r} but the pin '
            f'table says {expected_ref!r}')

    # Which apply branch a rendered job came from, judged by the command it
    # runs. Used to check the image against the script rather than trusting
    # that both were derived from the same lookup.
    _SCRIPT_MARKER = {
        'kustomize': 'kustomize build',
        'helm': 'helm upgrade',
        'argocd': 'argocd app sync',
    }

    @pytest.mark.parametrize('deploy', ['kustomize', 'helm', 'argocd', 'not-a-real-method'])
    def test_apply_image_matches_the_command_it_runs(self, deploy):
        """The runner image and the command must come from the SAME branch.

        `_build_gitlab_apply_stage` chooses the script, `_gitlab_apply_image`
        chooses the image, and both treat an unrecognised deploy method as
        argocd. If those two fallbacks ever diverge the job would run — say —
        `argocd app sync` inside a kubectl image, which starts a production
        deploy and then fails on a missing binary.

        ⛔ Asserting only "the variable is declared" does NOT catch that: both
        sides read the same lookup, so they agree by construction. The branch
        is therefore re-derived here from the rendered script text, which is an
        independent signal.
        """
        doc = self._pipeline(deploy)
        script = ' '.join(doc['apply']['script'])
        name = doc['apply']['image']['name']
        assert name.startswith('$') and not name.startswith('$$'), (
            f'expected a single-$ variable reference, got {name!r} — GitLab '
            f'reads `$$` as a literal `$` and would pull an image by that name')
        var = name[1:]
        assert var in doc['variables'], f'{var} referenced but never declared'

        branches = [b for b, marker in self._SCRIPT_MARKER.items() if marker in script]
        assert len(branches) == 1, (
            f'could not identify the apply branch from the rendered script '
            f'(matched {branches}); the markers need updating')
        expected_var, _ = ip._gitlab_apply_image(branches[0])
        assert var == expected_var, (
            f'script runs the {branches[0]!r} branch but the image comes from '
            f'${var} (expected ${expected_var})')

    def test_run_init_threads_the_requested_image_into_every_artifact(self):
        """End-to-end: the WIRING, not just the function.

        ⛔ The bug this change fixed lived in the call site — `run_init` was
        passing nothing, so the snippet fell back to a hardcoded ghcr.io ref.
        Testing `_gen_precommit_snippet` alone cannot see that, and every other
        `run_init` test passes exactly the value the old hardcode used, so they
        are blind to it too. This one uses a sentinel that appears in no
        default, so a regression at the call site has nowhere to hide.
        """
        sentinel = 'registry.internal:5000/team/da-tools:v9.9.9'
        with tempfile.TemporaryDirectory() as tmpdir:
            ip.run_init({
                'ci': 'both',
                'deploy': 'kustomize',
                'rule_packs': ['mariadb'],
                'tenants': ['db-a'],
                'namespace': 'monitoring',
                'da_tools_image': sentinel,
            }, tmpdir)
            checked = 0
            for rel in ('.pre-commit-config.da.yaml',
                        os.path.join('.gitlab-ci.d', 'dynamic-alerting.yml'),
                        os.path.join('.github', 'workflows', 'dynamic-alerting.yaml')):
                path = os.path.join(tmpdir, rel)
                assert os.path.isfile(path), f'{rel} was not generated'
                body = open(path, encoding='utf-8').read()
                assert sentinel in body, f'{rel} ignored --da-tools-image'
                assert 'ghcr.io/vencil/da-tools' not in body, (
                    f'{rel} still carries the built-in default alongside the '
                    f'requested image')
                checked += 1
            assert checked == 3, 'anti-vacuity: expected three artifacts to carry it'

    def test_git_sync_containers_all_use_the_pinned_ref(self):
        """Both the init container and the sidecar track one constant."""
        patch = yaml.safe_load(ip._gen_git_sync_deployment(
            'monitoring', 'https://example.com/r.git', 'main', 'conf.d'))
        spec = patch['spec']['template']['spec']
        found = [c['image'] for c in spec.get('initContainers', []) + spec['containers']
                 if 'git-sync' in c['name']]
        assert len(found) >= 2, 'anti-vacuity: expected an init container AND a sidecar'
        assert set(found) == {ip.GIT_SYNC_IMAGE}, f'git-sync refs drifted: {sorted(set(found))}'

    def test_precommit_snippet_honours_the_requested_image(self):
        """`--da-tools-image` must reach the snippet.

        It used to be hardcoded, so a customer pointing at their own registry
        still got a snippet that pulled from ghcr.io. The negative half of the
        assertion is what pins that: the default must NOT survive an override.
        """
        snippet = ip._gen_precommit_snippet('registry.internal/da-tools:v2.9.0')
        assert 'registry.internal/da-tools:v2.9.0' in snippet
        assert 'ghcr.io/vencil/da-tools' not in snippet

    def test_only_first_party_refs_may_float(self):
        """Class-level guard over every generated customer artifact.

        Keyed on the floating tag itself rather than on a list of known
        images, so a NEW third-party image added to any of these generators is
        caught without anyone remembering to update this test.

        ⛔ That claim is only true because the artifacts are obtained by RUNNING
        `run_init` into a temp dir and walking everything it wrote. An earlier
        version listed the generators by hand, and a floating ref planted in
        `_gen_kustomize_base` or `_gen_git_sync_kustomization` sailed through —
        the list, not the scan, was the hole.
        """
        offenders, scanned = [], 0
        for deploy in ('kustomize', 'helm', 'argocd'):
            with tempfile.TemporaryDirectory() as tmpdir:
                # config_source=git so the git-sync overlay is generated too.
                ip.run_init({
                    'ci': 'both',
                    'deploy': deploy,
                    'rule_packs': ['mariadb'],
                    'tenants': ['db-a'],
                    'namespace': 'monitoring',
                    'da_tools_image': 'ghcr.io/vencil/da-tools:latest',
                    'config_source': 'git',
                    'git_repo': 'https://example.com/r.git',
                    'git_branch': 'main',
                    'git_path': 'conf.d',
                    'git_period': 60,
                }, tmpdir)
                for root, _dirs, files in os.walk(tmpdir):
                    for fn in files:
                        path = os.path.join(root, fn)
                        scanned += 1
                        with open(path, encoding='utf-8', errors='replace') as fh:
                            for line in fh:
                                for ref in self._FLOATING_RE.findall(line):
                                    if not ref.startswith(self._FIRST_PARTY):
                                        offenders.append(
                                            f'{os.path.relpath(path, tmpdir)}: {line.strip()}')

        # Floor from the walk itself, not from a list this test maintains.
        assert scanned >= 24, (
            f'anti-vacuity: only {scanned} generated files scanned across three '
            f'deploy methods — run_init stopped emitting, or the walk is broken')
        assert not offenders, (
            'floating third-party refs emitted to customers:\n  '
            + '\n  '.join(sorted(set(offenders))))

    def test_the_floating_detector_actually_detects(self):
        """Positive control for the regex above.

        Without this, a regex that silently matches nothing would make
        `test_only_first_party_refs_may_float` pass forever. Uses the exact
        refs #1337 removed.
        """
        for bad in ('bitnami/kubectl:latest', 'alpine/helm:latest', 'argoproj/argocd:latest'):
            assert self._FLOATING_RE.findall(f'      image: {bad}') == [bad]
        # ...and must not fire on a pinned ref or on a GitHub runner label.
        assert not self._FLOATING_RE.findall('    runs-on: ubuntu-latest')
        assert not self._FLOATING_RE.findall(f'      image: {ip.GIT_SYNC_IMAGE}')
