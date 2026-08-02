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
        """MariaDB pack must have expected metric keys."""
        mariadb = ip.RULE_PACK_CATALOG['mariadb']['defaults']
        assert 'mysql_connections' in mariadb
        assert 'mysql_replication_lag' in mariadb
        assert mariadb['mysql_connections'] == 80
        assert mariadb['mysql_connections_critical'] == 150
        # #1231 rename guard: the catalog must carry the canonical spelling,
        # never the retired mysql_cpu one.
        assert 'mysql_threads_running' in mariadb
        assert 'mysql_threads_running_critical' in mariadb
        assert 'mysql_cpu' not in mariadb
        assert 'mysql_cpu_critical' not in mariadb

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
        assert defaults['mysql_connections_critical'] == 150

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

# A stub key line, whatever sits on its right-hand side. Deliberately looser
# than "line that carries the placeholder": the regression this guards against
# is a well-meant `key: 300`, so the extractor must still SEE such a line
# rather than filter it out and report a tidy pass.
_STUB_KEY_LINE = re.compile(r'^#\s{3,}([a-z][a-z0-9_]*):(.*)$')
_STUB_PLACEHOLDER = '"<your value>"'


def _stub_key_lines(text):
    """[(key, rhs), …] for the commented key lines of the declared block."""
    return [(m.group(1), m.group(2))
            for m in (_STUB_KEY_LINE.match(l) for l in text.split('\n')) if m]


def _paste_declared_lines_under_tenant(text, tenant):
    """Do literally what the stub tells the tenant to do; return the new text.

    "copy a whole line under your tenant above, drop the leading '# '" — pasted
    as the FIRST entry of the tenant's own mapping, which is both the natural
    reading and the placement that keeps the surrounding indentation
    load-bearing (appending at the very end of a nested block would let a
    too-deep line parse as a sibling of the last nested key). Also applies the
    prose's own precondition: delete a ` {}` flow mapping before pasting under
    it.
    """
    lines = text.split('\n')
    pasted = [ln[2:] for ln in lines if _STUB_KEY_LINE.match(ln)]
    assert pasted, 'nothing to paste — the declared block is missing'
    anchor = re.compile(r'^(\s*)%s:(\s*\{\})?\s*$' % re.escape(tenant))
    out, done = [], False
    for line in lines:
        m = anchor.match(line)
        if m and not done:
            out.append(f'{m.group(1)}{tenant}:')
            out.extend(pasted)
            done = True
            continue
        out.append(line)
    assert done, f'no `{tenant}:` line to paste under'
    return '\n'.join(out)


def _shipped_chart_declared_keys():
    """The declared list as it is actually SHIPPED, read from the artifact.

    Deliberately not `shipped_optional_keys_for_packs`: that is the function the
    generator itself calls, so it cannot disagree with the generator. The chart
    values file is written by a different producer (the registry `--regen`
    block) and is the reachability gate's own assertion source.
    """
    values = os.path.join(REPO_ROOT, 'helm', 'threshold-exporter', 'values.yaml')
    with open(values, encoding='utf-8') as f:
        shipped = yaml.safe_load(f)['thresholdConfig']['optional_overrides']
    assert shipped, f'{values} ships an empty declared list — nothing to check against'
    return set(shipped)


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
    # it fires once `<base>` has a value"). Measured: true of what
    # `scaffold_tenant` writes, FALSE of what `init_project` writes — this
    # module's `RULE_PACK_CATALOG` puts 16 `_critical` keys straight into
    # `defaults:` (11 of its 15 packs), so `da-tools init --rule-packs mariadb`
    # emits `mysql_connections_critical: 150` under `defaults:` in the very
    # `_defaults.yaml` it writes next to the stub. For those keys rule 1 is the
    # applicable one and the tenant needs to do nothing.
    #
    # ⛔ The two tests below assert against the SIBLING FILE, parsed — not
    # against a phrase. That is the whole point: the previous test only checked
    # that a string was present, which a false string passes just as well.

    def test_critical_paragraph_matches_the_sibling_defaults_file(self):
        packs = ['mariadb', 'oracle']
        tenant_text = ip._gen_tenant_yaml('db-a', packs)
        sibling = yaml.safe_load(ip._gen_defaults_yaml(packs, 'monitoring'))
        crit = [k for k in sibling['defaults'] if k.endswith('_critical')]
        assert crit, (
            'fixture assumption broken: this generator is supposed to be the '
            'one that DOES put _critical keys in `defaults:`')

        from _registry_lib import render_tenant_critical_note_lines
        expected = '\n'.join(
            render_tenant_critical_note_lines(sibling['defaults'], lang='en'))
        assert expected in tenant_text

        # …and one assertion that does not go through the renderer at all: the
        # regime word in the header must agree with the parsed artifact.
        header = tenant_text.split('\ntenants:')[0]
        assert ('lists none of them under `defaults:`' in header) == (not crit)
        assert str(len(crit)) in header

    def test_critical_paragraph_flips_when_defaults_gains_a_critical_key(self):
        """The renderer's own contract, on synthetic input: a `_critical`
        appearing in `defaults:` must move the claim, not just the count."""
        from _registry_lib import render_tenant_critical_note_lines
        without = '\n'.join(render_tenant_critical_note_lines(
            {'mysql_connections': 80}, lang='en'))
        with_crit = '\n'.join(render_tenant_critical_note_lines(
            {'mysql_connections': 80, 'mysql_connections_critical': 150},
            lang='en'))
        assert 'lists none of them' in without
        assert 'lists none of them' not in with_crit
        assert 'gives 1 of them a value' in with_crit

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
            assert section[key] == '<your value>'
        # the tenant's pre-existing keys are still its own
        assert '_routing' in section


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

    def test_three_stages(self):
        """Pipeline has validate, generate, apply stages."""
        yaml_str = ip._gen_gitlab_ci('monitoring', 'ghcr.io/vencil/da-tools:latest', 'kustomize')
        assert '- validate' in yaml_str
        assert '- generate' in yaml_str
        assert '- apply' in yaml_str

    def test_validate_config_job(self):
        """validate-config job is present."""
        yaml_str = ip._gen_gitlab_ci('monitoring', 'ghcr.io/vencil/da-tools:latest', 'kustomize')
        assert 'validate-config:' in yaml_str
        assert 'da-tools validate-config' in yaml_str

    def test_generate_routes_job(self):
        """generate-routes job is present."""
        yaml_str = ip._gen_gitlab_ci('monitoring', 'ghcr.io/vencil/da-tools:latest', 'kustomize')
        assert 'generate-routes:' in yaml_str
        assert 'da-tools generate-routes' in yaml_str

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
        yaml_str = ip._gen_precommit_snippet()
        config = yaml.safe_load(yaml_str)
        assert 'repos' in config
        assert isinstance(config['repos'], list)

    def test_local_repo(self):
        """Snippet uses local repo type."""
        yaml_str = ip._gen_precommit_snippet()
        config = yaml.safe_load(yaml_str)
        repo = config['repos'][0]
        assert repo['repo'] == 'local'

    def test_da_validate_config_hook(self):
        """da-validate-config hook is present."""
        yaml_str = ip._gen_precommit_snippet()
        config = yaml.safe_load(yaml_str)
        hooks = config['repos'][0]['hooks']
        hook_ids = [h['id'] for h in hooks]
        assert 'da-validate-config' in hook_ids

    def test_da_generate_routes_hook(self):
        """da-generate-routes hook is present."""
        yaml_str = ip._gen_precommit_snippet()
        config = yaml.safe_load(yaml_str)
        hooks = config['repos'][0]['hooks']
        hook_ids = [h['id'] for h in hooks]
        assert 'da-generate-routes' in hook_ids

    def test_hooks_target_conf_d(self):
        """Hooks are limited to conf.d files."""
        yaml_str = ip._gen_precommit_snippet()
        config = yaml.safe_load(yaml_str)
        hooks = config['repos'][0]['hooks']
        for hook in hooks:
            # Files pattern should reference conf.d
            assert 'conf' in hook['files']

    def test_language_system(self):
        """Hooks use 'system' language."""
        yaml_str = ip._gen_precommit_snippet()
        config = yaml.safe_load(yaml_str)
        hooks = config['repos'][0]['hooks']
        for hook in hooks:
            assert hook['language'] == 'system'

    def test_header_mentions_da_tools(self):
        """Header mentions Dynamic Alerting."""
        yaml_str = ip._gen_precommit_snippet()
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

    def test_includes_gitlab_pipeline(self):
        """Preview includes GitLab CI when ci=gitlab."""
        config = {
            'ci': 'gitlab',
            'deploy': 'kustomize',
            'tenants': ['db-a'],
        }
        files = ip._preview_files(config, '/tmp')
        assert any('gitlab-ci.d' in f for f in files)

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

@pytest.mark.parametrize('ci,deploy', [
    ('github', 'kustomize'),
    ('github', 'helm'),
    ('github', 'argocd'),
    ('gitlab', 'kustomize'),
    ('gitlab', 'helm'),
    ('gitlab', 'argocd'),
    ('both', 'kustomize'),
    ('both', 'helm'),
    ('both', 'argocd'),
])
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
