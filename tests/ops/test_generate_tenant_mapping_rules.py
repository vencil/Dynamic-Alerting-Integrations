"""Tests for generate_tenant_mapping_rules.py (ADR-006)."""
from __future__ import annotations

import os
import sys
import textwrap
import tempfile

import pytest
import yaml

# ---------------------------------------------------------------------------
# sys.path setup (same pattern as other test files)
# ---------------------------------------------------------------------------
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_REPO, 'scripts', 'tools', 'ops'))
sys.path.insert(0, os.path.join(_REPO, 'scripts', 'tools'))

from generate_tenant_mapping_rules import (
    MappingEntry,
    InstanceMapping,
    parse_mapping_file,
    find_mapping_file,
    parse_filter_to_matchers,
    generate_recording_rules,
    format_as_yaml,
    format_as_configmap,
    validate_mappings,
    estimate_cardinality,
    collect_tenant_ids_from_config_dir,
    load_metrics_from_dictionary,
)


# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture
def config_dir():
    """Temporary config directory."""
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def mapping_file(config_dir):
    """Write a sample _instance_mapping.yaml and return its path."""
    content = textwrap.dedent("""\
        instance_tenant_mapping:
          oracle-prod-01:
            - tenant: db-a
              filter: 'schema=~"app_a_.*"'
            - tenant: db-b
              filter: 'schema=~"app_b_.*"'
          db2-shared-01:
            - tenant: db-c
              filter: 'tablespace="ts_client_c"'
    """)
    path = os.path.join(config_dir, '_instance_mapping.yaml')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    return path


@pytest.fixture
def sample_metrics():
    return ['oracle_sessions', 'oracle_tablespace_usage']


@pytest.fixture
def sample_mappings():
    return [
        InstanceMapping('oracle-prod-01', [
            MappingEntry('db-a', 'schema=~"app_a_.*"'),
            MappingEntry('db-b', 'schema=~"app_b_.*"'),
        ]),
        InstanceMapping('db2-shared-01', [
            MappingEntry('db-c', 'tablespace="ts_client_c"'),
        ]),
    ]


# ===========================================================================
# Parsing tests
# ===========================================================================

class TestFindMappingFile:
    def test_found(self, config_dir, mapping_file):
        result = find_mapping_file(config_dir)
        assert result is not None
        assert result.endswith('_instance_mapping.yaml')

    def test_not_found(self, config_dir):
        assert find_mapping_file(config_dir) is None

    def test_yml_extension(self, config_dir):
        path = os.path.join(config_dir, '_instance_mapping.yml')
        with open(path, 'w', encoding='utf-8') as f:
            f.write('instance_tenant_mapping: {}')
        assert find_mapping_file(config_dir) is not None


class TestParseMappingFile:
    def test_basic_parse(self, mapping_file):
        mappings = parse_mapping_file(mapping_file)
        assert len(mappings) == 2

        # Sorted by instance name
        assert mappings[0].instance == 'db2-shared-01'
        assert len(mappings[0].entries) == 1
        assert mappings[0].entries[0].tenant == 'db-c'

        assert mappings[1].instance == 'oracle-prod-01'
        assert len(mappings[1].entries) == 2

    def test_empty_file(self, config_dir):
        path = os.path.join(config_dir, '_instance_mapping.yaml')
        with open(path, 'w', encoding='utf-8') as f:
            f.write('')
        assert parse_mapping_file(path) == []

    def test_missing_tenant_field(self, config_dir):
        path = os.path.join(config_dir, '_instance_mapping.yaml')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(textwrap.dedent("""\
                instance_tenant_mapping:
                  inst-1:
                    - filter: 'schema="x"'
            """))
        mappings = parse_mapping_file(path)
        assert len(mappings) == 0  # Entry skipped due to missing tenant

    def test_empty_filter_skipped(self, config_dir):
        path = os.path.join(config_dir, '_instance_mapping.yaml')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(textwrap.dedent("""\
                instance_tenant_mapping:
                  inst-1:
                    - tenant: db-x
                      filter: ''
            """))
        mappings = parse_mapping_file(path)
        assert len(mappings) == 0

    def test_non_list_entries_warned(self, config_dir, capsys):
        path = os.path.join(config_dir, '_instance_mapping.yaml')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(textwrap.dedent("""\
                instance_tenant_mapping:
                  inst-1: "not a list"
            """))
        mappings = parse_mapping_file(path)
        assert len(mappings) == 0
        assert 'WARN' in capsys.readouterr().err


# ===========================================================================
# Filter parsing tests
# ===========================================================================

class TestParseFilterToMatchers:
    def test_single_regex(self):
        result = parse_filter_to_matchers('schema=~"app_a_.*"')
        assert result == ['schema=~"app_a_.*"']

    def test_single_exact(self):
        result = parse_filter_to_matchers('tablespace="ts_a"')
        assert result == ['tablespace="ts_a"']

    def test_multi_matcher(self):
        result = parse_filter_to_matchers('schema=~"app_.*", env="prod"')
        assert len(result) == 2
        assert 'schema=~"app_.*"' in result
        assert 'env="prod"' in result

    def test_not_equal(self):
        result = parse_filter_to_matchers('schema!="system"')
        assert result == ['schema!="system"']

    def test_empty(self):
        assert parse_filter_to_matchers('') == []


# ===========================================================================
# Rule generation tests
# ===========================================================================

class TestGenerateRecordingRules:
    def test_basic_generation(self, sample_mappings, sample_metrics):
        groups = generate_recording_rules(sample_mappings, sample_metrics)
        assert len(groups) == 2

        # First group: oracle-prod-01 with 2 entries × 2 metrics = 4 rules
        g1 = groups[0]
        assert g1['name'] == 'tenant_mapping_oracle-prod-01'
        assert len(g1['rules']) == 4
        assert g1['interval'] == '30s'

        # Check first rule
        r = g1['rules'][0]
        assert r['record'] == 'tenant_mapped:oracle_sessions:current'
        assert 'instance="oracle-prod-01"' in r['expr']
        assert r['labels']['tenant'] == 'db-a'

    def test_second_group(self, sample_mappings, sample_metrics):
        groups = generate_recording_rules(sample_mappings, sample_metrics)
        # Second group: db2-shared-01 with 1 entry × 2 metrics = 2 rules
        g2 = groups[1]
        assert g2['name'] == 'tenant_mapping_db2-shared-01'
        assert len(g2['rules']) == 2
        assert g2['rules'][0]['labels']['tenant'] == 'db-c'

    def test_empty_mappings(self):
        assert generate_recording_rules([], ['m1']) == []

    def test_empty_metrics(self, sample_mappings):
        groups = generate_recording_rules(sample_mappings, [])
        # Groups created but no rules
        for g in groups:
            assert len(g['rules']) == 0

    def test_filter_in_expr(self, sample_mappings, sample_metrics):
        groups = generate_recording_rules(sample_mappings, sample_metrics)
        r = groups[0]['rules'][0]
        assert 'schema=~"app_a_.*"' in r['expr']

    def test_recording_rule_naming_convention(self, sample_mappings):
        groups = generate_recording_rules(sample_mappings, ['cpu_usage'])
        r = groups[0]['rules'][0]
        assert r['record'].startswith('tenant_mapped:')
        assert r['record'].endswith(':current')


# ===========================================================================
# Output formatting tests
# ===========================================================================

class TestFormatAsYaml:
    def test_valid_yaml(self, sample_mappings, sample_metrics):
        groups = generate_recording_rules(sample_mappings, sample_metrics)
        output = format_as_yaml(groups)
        parsed = yaml.safe_load(output)
        assert 'groups' in parsed
        assert len(parsed['groups']) == 2

    def test_roundtrip(self, sample_mappings, sample_metrics):
        groups = generate_recording_rules(sample_mappings, sample_metrics)
        output = format_as_yaml(groups)
        parsed = yaml.safe_load(output)
        assert parsed['groups'][0]['name'] == groups[0]['name']


class TestFormatAsConfigmap:
    def test_valid_configmap_structure(self, sample_mappings, sample_metrics):
        groups = generate_recording_rules(sample_mappings, sample_metrics)
        output = format_as_configmap(groups, namespace='monitoring')
        parsed = yaml.safe_load(output)
        assert parsed['apiVersion'] == 'v1'
        assert parsed['kind'] == 'ConfigMap'
        assert parsed['metadata']['namespace'] == 'monitoring'
        assert 'tenant-mapping-rules.yaml' in parsed['data']

    def test_configmap_labels(self, sample_mappings, sample_metrics):
        groups = generate_recording_rules(sample_mappings, sample_metrics)
        output = format_as_configmap(groups)
        parsed = yaml.safe_load(output)
        labels = parsed['metadata']['labels']
        assert labels['app'] == 'dynamic-alerting'
        assert labels['component'] == 'rule-pack-part1-tenant-mapping'

    def test_embedded_rules_parseable(self, sample_mappings, sample_metrics):
        groups = generate_recording_rules(sample_mappings, sample_metrics)
        output = format_as_configmap(groups)
        parsed = yaml.safe_load(output)
        inner = yaml.safe_load(parsed['data']['tenant-mapping-rules.yaml'])
        assert 'groups' in inner
        assert len(inner['groups']) == 2


# ===========================================================================
# Validation tests
# ===========================================================================

class TestValidateMappings:
    def test_all_valid(self, sample_mappings):
        known = {'db-a', 'db-b', 'db-c'}
        msgs = validate_mappings(sample_mappings, known)
        assert not any(m.startswith('ERROR') for m in msgs)

    def test_unknown_tenant(self, sample_mappings):
        known = {'db-a'}  # Missing db-b, db-c
        msgs = validate_mappings(sample_mappings, known)
        errors = [m for m in msgs if m.startswith('ERROR')]
        assert len(errors) == 2  # db-b and db-c unknown

    def test_multi_instance_info(self):
        mappings = [
            InstanceMapping('inst-1', [MappingEntry('db-a', 'x="1"')]),
            InstanceMapping('inst-2', [MappingEntry('db-a', 'y="2"')]),
        ]
        msgs = validate_mappings(mappings, {'db-a'})
        info = [m for m in msgs if m.startswith('INFO')]
        assert len(info) == 1
        assert 'db-a' in info[0]


class TestCollectTenantIds:
    def test_wrapper_format(self, config_dir):
        with open(os.path.join(config_dir, 'db-a.yaml'), 'w', encoding='utf-8') as f:
            f.write(textwrap.dedent("""\
                tenants:
                  db-a:
                    cpu_usage: "80"
            """))
        ids = collect_tenant_ids_from_config_dir(config_dir)
        assert 'db-a' in ids

    def test_skips_reserved_files(self, config_dir):
        with open(os.path.join(config_dir, '_defaults.yaml'), 'w', encoding='utf-8') as f:
            f.write('defaults:\n  cpu: 90\n')
        with open(os.path.join(config_dir, 'db-a.yaml'), 'w', encoding='utf-8') as f:
            f.write('tenants:\n  db-a:\n    cpu: "80"\n')
        ids = collect_tenant_ids_from_config_dir(config_dir)
        assert 'db-a' in ids
        # _defaults should not appear as tenant


# ===========================================================================
# Cardinality estimation tests
# ===========================================================================

class TestEstimateCardinality:
    def test_basic(self, sample_mappings):
        card = estimate_cardinality(sample_mappings, 10)
        assert card['instances'] == 2
        assert card['mapping_entries'] == 3  # 2 + 1
        assert card['metrics_per_entry'] == 10
        assert card['new_series_estimate'] == 30


# ===========================================================================
# Metric dictionary loading
# ===========================================================================

class TestLoadMetricsDictionary:
    def test_basic_load(self, config_dir):
        path = os.path.join(config_dir, 'metric-dictionary.yaml')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(textwrap.dedent("""\
                mysql_threads_connected:
                  maps_to: mysql_connections
                  rule_pack: mariadb
                mysql_slow_queries:
                  maps_to: mysql_slow_queries
                  rule_pack: mariadb
                redis_connections:
                  maps_to: redis_connections
                  rule_pack: redis
            """))
        metrics = load_metrics_from_dictionary(path)
        assert 'mysql_connections' in metrics
        assert 'mysql_slow_queries' in metrics
        assert 'redis_connections' in metrics
        # Deduped: maps_to values are unique
        assert len(metrics) == len(set(metrics))

    def test_empty_file(self, config_dir):
        path = os.path.join(config_dir, 'empty.yaml')
        with open(path, 'w', encoding='utf-8') as f:
            f.write('')
        assert load_metrics_from_dictionary(path) == []


# ===========================================================================
# CLI integration test
# ===========================================================================

class TestCLI:
    def test_dry_run(self, config_dir, mapping_file):
        """Test CLI dry-run mode via subprocess."""
        import subprocess
        script = os.path.join(_REPO, 'scripts', 'tools', 'ops',
                              'generate_tenant_mapping_rules.py')
        result = subprocess.run(  # subprocess-timeout: ignore
            [sys.executable, script,
             '--config-dir', config_dir,
             '--metrics', 'oracle_sessions,oracle_tablespace_usage',
             '--dry-run'],
            capture_output=True, text=True, encoding='utf-8'
        )
        assert result.returncode == 0
        assert 'Tenant Mapping Rules Summary' in result.stdout
        assert 'oracle-prod-01' in result.stdout

    def test_yaml_output(self, config_dir, mapping_file):
        import subprocess
        script = os.path.join(_REPO, 'scripts', 'tools', 'ops',
                              'generate_tenant_mapping_rules.py')
        result = subprocess.run(  # subprocess-timeout: ignore
            [sys.executable, script,
             '--config-dir', config_dir,
             '--metrics', 'cpu_usage',
             '--format', 'yaml'],
            capture_output=True, text=True, encoding='utf-8'
        )
        assert result.returncode == 0
        parsed = yaml.safe_load(result.stdout)
        assert 'groups' in parsed

    def test_validate_unknown_tenant(self, config_dir, mapping_file):
        """Validation should fail when tenant doesn't exist in config-dir."""
        import subprocess
        script = os.path.join(_REPO, 'scripts', 'tools', 'ops',
                              'generate_tenant_mapping_rules.py')
        result = subprocess.run(  # subprocess-timeout: ignore
            [sys.executable, script,
             '--config-dir', config_dir,
             '--metrics', 'cpu_usage',
             '--validate', '--dry-run'],
            capture_output=True, text=True, encoding='utf-8'
        )
        # Should fail because db-a, db-b, db-c don't exist as tenant YAML files
        assert result.returncode == 1
        assert 'ERROR' in result.stderr

    def test_no_mapping_file_exits_clean(self, config_dir):
        """No _instance_mapping.yaml → exit 0 with INFO message."""
        import subprocess
        script = os.path.join(_REPO, 'scripts', 'tools', 'ops',
                              'generate_tenant_mapping_rules.py')
        result = subprocess.run(  # subprocess-timeout: ignore
            [sys.executable, script,
             '--config-dir', config_dir,
             '--metrics', 'cpu_usage'],
            capture_output=True, text=True, encoding='utf-8'
        )
        assert result.returncode == 0
        assert 'no _instance_mapping.yaml' in result.stderr
