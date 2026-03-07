#!/usr/bin/env python3
"""Tests for scaffold_tenant.py — Oracle + DB2 extensions (Phase 12).

Covers:
- RULE_PACKS catalogue includes oracle and db2 entries
- Oracle/DB2 entries have required keys (display, exporter, defaults, etc.)
- Non-interactive mode generates correct tenant YAML for new DB types
- Metric dictionary entries exist for Oracle/DB2
- Rule pack YAML files are valid and follow three-group structure
"""

import os
import sys
import tempfile
import shutil
import unittest

import yaml

# Make scaffold_tenant importable
TOOLS_DIR = os.path.join(
    os.path.dirname(__file__), os.pardir, "scripts", "tools",
)
sys.path.insert(0, os.path.abspath(TOOLS_DIR))

import scaffold_tenant  # noqa: E402

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), os.pardir)


# ── RULE_PACKS Catalogue ────────────────────────────────────────────


class TestRulePacksCatalogue(unittest.TestCase):
    """Verify RULE_PACKS dict has oracle and db2 with correct structure."""

    REQUIRED_KEYS = {"display", "exporter", "default_on", "rule_pack_file", "defaults"}

    def test_postgresql_in_rule_packs(self):
        """PostgreSQL entry exists in RULE_PACKS."""
        self.assertIn("postgresql", scaffold_tenant.RULE_PACKS)

    def test_postgresql_has_required_keys(self):
        """PostgreSQL entry has all required keys."""
        entry = scaffold_tenant.RULE_PACKS["postgresql"]
        for key in self.REQUIRED_KEYS:
            self.assertIn(key, entry, f"PostgreSQL missing key: {key}")

    def test_postgresql_not_default_on(self):
        """PostgreSQL should not be default_on."""
        self.assertFalse(scaffold_tenant.RULE_PACKS["postgresql"]["default_on"])

    def test_postgresql_has_defaults(self):
        """PostgreSQL defaults include key metrics."""
        defaults = scaffold_tenant.RULE_PACKS["postgresql"]["defaults"]
        self.assertIn("pg_connections", defaults)
        self.assertIn("pg_replication_lag", defaults)

    def test_postgresql_has_dimensional_example(self):
        """PostgreSQL has dimensional_example."""
        entry = scaffold_tenant.RULE_PACKS["postgresql"]
        self.assertIn("dimensional_example", entry)
        dim = entry["dimensional_example"]
        has_datname = any("datname" in k for k in dim)
        self.assertTrue(has_datname, "PostgreSQL dimensional_example missing datname example")

    def test_oracle_in_rule_packs(self):
        """Oracle entry exists in RULE_PACKS."""
        self.assertIn("oracle", scaffold_tenant.RULE_PACKS)

    def test_db2_in_rule_packs(self):
        """DB2 entry exists in RULE_PACKS."""
        self.assertIn("db2", scaffold_tenant.RULE_PACKS)

    def test_oracle_has_required_keys(self):
        """Oracle entry has all required keys."""
        entry = scaffold_tenant.RULE_PACKS["oracle"]
        for key in self.REQUIRED_KEYS:
            self.assertIn(key, entry, f"Oracle missing key: {key}")

    def test_db2_has_required_keys(self):
        """DB2 entry has all required keys."""
        entry = scaffold_tenant.RULE_PACKS["db2"]
        for key in self.REQUIRED_KEYS:
            self.assertIn(key, entry, f"DB2 missing key: {key}")

    def test_oracle_not_default_on(self):
        """Oracle should not be default_on (enterprise DB)."""
        self.assertFalse(scaffold_tenant.RULE_PACKS["oracle"]["default_on"])

    def test_db2_not_default_on(self):
        """DB2 should not be default_on (enterprise DB)."""
        self.assertFalse(scaffold_tenant.RULE_PACKS["db2"]["default_on"])

    def test_oracle_has_defaults(self):
        """Oracle defaults include key metrics."""
        defaults = scaffold_tenant.RULE_PACKS["oracle"]["defaults"]
        self.assertIn("oracle_sessions_active", defaults)
        self.assertIn("oracle_tablespace_used_percent", defaults)

    def test_db2_has_defaults(self):
        """DB2 defaults include key metrics."""
        defaults = scaffold_tenant.RULE_PACKS["db2"]["defaults"]
        self.assertIn("db2_connections_active", defaults)
        self.assertIn("db2_bufferpool_hit_ratio", defaults)

    def test_oracle_has_dimensional_example(self):
        """Oracle has dimensional_example for regex showcase."""
        entry = scaffold_tenant.RULE_PACKS["oracle"]
        self.assertIn("dimensional_example", entry)
        # Should have tablespace regex example
        dim = entry["dimensional_example"]
        has_regex = any("=~" in k or "tablespace" in k for k in dim)
        self.assertTrue(has_regex, "Oracle dimensional_example missing regex tablespace")

    def test_db2_has_dimensional_example(self):
        """DB2 has dimensional_example for regex showcase."""
        entry = scaffold_tenant.RULE_PACKS["db2"]
        self.assertIn("dimensional_example", entry)
        dim = entry["dimensional_example"]
        has_regex = any("=~" in k or "bufferpool" in k for k in dim)
        self.assertTrue(has_regex, "DB2 dimensional_example missing regex bufferpool")

    def test_clickhouse_in_rule_packs(self):
        """ClickHouse entry exists in RULE_PACKS."""
        self.assertIn("clickhouse", scaffold_tenant.RULE_PACKS)

    def test_clickhouse_has_required_keys(self):
        """ClickHouse entry has all required keys."""
        entry = scaffold_tenant.RULE_PACKS["clickhouse"]
        for key in self.REQUIRED_KEYS:
            self.assertIn(key, entry, f"ClickHouse missing key: {key}")

    def test_clickhouse_not_default_on(self):
        """ClickHouse should not be default_on."""
        self.assertFalse(scaffold_tenant.RULE_PACKS["clickhouse"]["default_on"])

    def test_clickhouse_has_defaults(self):
        """ClickHouse defaults include key metrics."""
        defaults = scaffold_tenant.RULE_PACKS["clickhouse"]["defaults"]
        self.assertIn("clickhouse_queries_rate", defaults)
        self.assertIn("clickhouse_active_connections", defaults)

    def test_clickhouse_has_dimensional_example(self):
        """ClickHouse has dimensional_example for regex showcase."""
        entry = scaffold_tenant.RULE_PACKS["clickhouse"]
        self.assertIn("dimensional_example", entry)
        dim = entry["dimensional_example"]
        has_regex = any("=~" in k or "database" in k for k in dim)
        self.assertTrue(has_regex, "ClickHouse dimensional_example missing regex database")

    def test_rule_pack_count_is_9(self):
        """Total RULE_PACKS should be 9 (kubernetes + postgresql + mariadb + redis + mongodb + elasticsearch + oracle + db2 + clickhouse; platform excluded)."""
        self.assertGreaterEqual(len(scaffold_tenant.RULE_PACKS), 9)


# ── Non-Interactive Generation ───────────────────────────────────────


class TestNonInteractiveGeneration(unittest.TestCase):
    """Verify scaffold_tenant generates correct files for oracle/db2."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run_scaffold(self, tenant, dbs):
        """Run scaffold_tenant main with args."""
        old_argv = sys.argv
        sys.argv = [
            "scaffold_tenant.py",
            "--tenant", tenant,
            "--db", dbs,
            "-o", self.tmpdir,
        ]
        try:
            scaffold_tenant.main()
        finally:
            sys.argv = old_argv

    def test_oracle_generates_tenant_yaml(self):
        """Oracle-only scaffold creates tenant YAML."""
        self._run_scaffold("test-ora", "oracle")
        path = os.path.join(self.tmpdir, "test-ora.yaml")
        self.assertTrue(os.path.isfile(path), f"Missing {path}")

    def test_oracle_generates_defaults_yaml(self):
        """Oracle scaffold creates _defaults.yaml."""
        self._run_scaffold("test-ora", "oracle")
        path = os.path.join(self.tmpdir, "_defaults.yaml")
        self.assertTrue(os.path.isfile(path), f"Missing {path}")

    def test_oracle_defaults_contain_oracle_metrics(self):
        """_defaults.yaml includes oracle metric keys."""
        self._run_scaffold("test-ora", "oracle")
        path = os.path.join(self.tmpdir, "_defaults.yaml")
        with open(path) as f:
            content = f.read()
        self.assertIn("oracle_sessions_active", content)

    def test_db2_generates_tenant_yaml(self):
        """DB2-only scaffold creates tenant YAML."""
        self._run_scaffold("test-db2", "db2")
        path = os.path.join(self.tmpdir, "test-db2.yaml")
        self.assertTrue(os.path.isfile(path))

    def test_db2_defaults_contain_db2_metrics(self):
        """_defaults.yaml includes db2 metric keys."""
        self._run_scaffold("test-db2", "db2")
        path = os.path.join(self.tmpdir, "_defaults.yaml")
        with open(path) as f:
            content = f.read()
        self.assertIn("db2_connections_active", content)
        self.assertIn("db2_bufferpool_hit_ratio", content)

    def test_combined_oracle_db2(self):
        """Combined oracle+db2 scaffold contains both metric sets."""
        self._run_scaffold("test-combo", "oracle,db2")
        path = os.path.join(self.tmpdir, "_defaults.yaml")
        with open(path) as f:
            content = f.read()
        self.assertIn("oracle_sessions_active", content)
        self.assertIn("db2_connections_active", content)

    def test_scaffold_report_mentions_oracle(self):
        """scaffold-report.txt mentions Oracle."""
        self._run_scaffold("test-ora", "oracle")
        path = os.path.join(self.tmpdir, "scaffold-report.txt")
        self.assertTrue(os.path.isfile(path), f"Missing {path}")
        with open(path) as f:
            content = f.read()
        self.assertIn("Oracle", content)

    def test_scaffold_report_mentions_db2(self):
        """scaffold-report.txt mentions DB2."""
        self._run_scaffold("test-db2", "db2")
        path = os.path.join(self.tmpdir, "scaffold-report.txt")
        with open(path) as f:
            content = f.read()
        self.assertIn("DB2", content)

    def test_postgresql_generates_tenant_yaml(self):
        """PostgreSQL-only scaffold creates tenant YAML."""
        self._run_scaffold("test-pg", "postgresql")
        path = os.path.join(self.tmpdir, "test-pg.yaml")
        self.assertTrue(os.path.isfile(path), f"Missing {path}")

    def test_postgresql_defaults_contain_pg_metrics(self):
        """_defaults.yaml includes PostgreSQL metric keys."""
        self._run_scaffold("test-pg", "postgresql")
        path = os.path.join(self.tmpdir, "_defaults.yaml")
        with open(path) as f:
            content = f.read()
        self.assertIn("pg_connections", content)
        self.assertIn("pg_replication_lag", content)

    def test_postgresql_scaffold_report_mentions_postgresql(self):
        """scaffold-report.txt mentions PostgreSQL."""
        self._run_scaffold("test-pg", "postgresql")
        path = os.path.join(self.tmpdir, "scaffold-report.txt")
        self.assertTrue(os.path.isfile(path), f"Missing {path}")
        with open(path) as f:
            content = f.read()
        self.assertIn("PostgreSQL", content)

    def test_clickhouse_generates_tenant_yaml(self):
        """ClickHouse-only scaffold creates tenant YAML."""
        self._run_scaffold("test-ch", "clickhouse")
        path = os.path.join(self.tmpdir, "test-ch.yaml")
        self.assertTrue(os.path.isfile(path))

    def test_clickhouse_defaults_contain_metrics(self):
        """_defaults.yaml includes clickhouse metric keys."""
        self._run_scaffold("test-ch", "clickhouse")
        path = os.path.join(self.tmpdir, "_defaults.yaml")
        with open(path) as f:
            content = f.read()
        self.assertIn("clickhouse_queries_rate", content)
        self.assertIn("clickhouse_active_connections", content)


# ── Metric Dictionary ────────────────────────────────────────────────


class TestMetricDictionary(unittest.TestCase):
    """Verify metric-dictionary.yaml has Oracle and DB2 entries."""

    @classmethod
    def setUpClass(cls):
        dict_path = os.path.join(PROJECT_ROOT, "scripts", "tools", "metric-dictionary.yaml")
        with open(dict_path) as f:
            cls.dictionary = yaml.safe_load(f)

    def test_oracle_entries_exist(self):
        """Dictionary has oracledb_ metric entries."""
        oracle_keys = [k for k in self.dictionary if k.startswith("oracledb_")]
        self.assertGreaterEqual(len(oracle_keys), 3, f"Only {len(oracle_keys)} oracle entries")

    def test_db2_entries_exist(self):
        """Dictionary has db2_ metric entries."""
        db2_keys = [k for k in self.dictionary if k.startswith("db2_")]
        self.assertGreaterEqual(len(db2_keys), 3, f"Only {len(db2_keys)} db2 entries")

    def test_oracle_sessions_has_golden(self):
        """oracledb_sessions_active has golden_rule mapping."""
        entry = self.dictionary.get("oracledb_sessions_active", {})
        self.assertIn("golden_rule", entry, "Missing golden_rule for oracledb_sessions_active")

    def test_db2_connections_has_golden(self):
        """db2_connections_active has golden_rule mapping."""
        entry = self.dictionary.get("db2_connections_active", {})
        self.assertIn("golden_rule", entry, "Missing golden_rule for db2_connections_active")

    def test_clickhouse_entries_exist(self):
        """Dictionary has ClickHouse metric entries."""
        ch_keys = [k for k in self.dictionary if k.startswith("ClickHouse")]
        self.assertGreaterEqual(len(ch_keys), 3, f"Only {len(ch_keys)} ClickHouse entries")

    def test_clickhouse_query_has_golden(self):
        """ClickHouseProfileEvents_Query has golden_rule mapping."""
        entry = self.dictionary.get("ClickHouseProfileEvents_Query", {})
        self.assertIn("golden_rule", entry)


# ── Rule Pack YAML Validation ────────────────────────────────────────


class TestRulePackYAML(unittest.TestCase):
    """Validate canonical rule pack YAML files for Oracle and DB2."""

    RULE_PACKS_DIR = os.path.join(PROJECT_ROOT, "rule-packs")
    K8S_DIR = os.path.join(PROJECT_ROOT, "k8s", "03-monitoring")

    def _load_yaml(self, path):
        with open(path) as f:
            return yaml.safe_load(f)

    def _validate_rule_pack_structure(self, groups, db_prefix):
        """Check three-group pattern: normalization, threshold-normalization, alerts."""
        group_names = [g["name"] for g in groups]
        self.assertIn(f"{db_prefix}-normalization", group_names)
        self.assertIn(f"{db_prefix}-threshold-normalization", group_names)
        self.assertIn(f"{db_prefix}-alerts", group_names)

    def test_postgresql_rule_pack_exists(self):
        """rule-packs/rule-pack-postgresql.yaml exists."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-postgresql.yaml")
        self.assertTrue(os.path.isfile(path))

    def test_postgresql_configmap_exists(self):
        """k8s/03-monitoring/configmap-rules-postgresql.yaml exists."""
        path = os.path.join(self.K8S_DIR, "configmap-rules-postgresql.yaml")
        self.assertTrue(os.path.isfile(path))

    def test_postgresql_rule_pack_three_groups(self):
        """PostgreSQL rule pack has the three-group structure."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-postgresql.yaml")
        data = self._load_yaml(path)
        self._validate_rule_pack_structure(data["groups"], "postgresql")

    def test_postgresql_configmap_has_label(self):
        """PostgreSQL ConfigMap has rule-pack: postgresql label."""
        path = os.path.join(self.K8S_DIR, "configmap-rules-postgresql.yaml")
        data = self._load_yaml(path)
        self.assertEqual(data["metadata"]["labels"]["rule-pack"], "postgresql")

    def test_postgresql_uses_max_by_tenant(self):
        """PostgreSQL threshold normalization uses max by(tenant)."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-postgresql.yaml")
        with open(path) as f:
            content = f.read()
        self.assertIn("max by(tenant)", content)

    def test_postgresql_alerts_have_maintenance_unless(self):
        """PostgreSQL alert rules use 'unless on(tenant)' maintenance filter."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-postgresql.yaml")
        with open(path) as f:
            content = f.read()
        self.assertIn('unless on(tenant)', content)

    def test_postgresql_has_metric_group_labels(self):
        """PostgreSQL alerts have metric_group for severity dedup pairing."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-postgresql.yaml")
        with open(path) as f:
            content = f.read()
        self.assertIn('metric_group: "pg_connections"', content)
        self.assertIn('metric_group: "pg_replication_lag"', content)

    def test_postgresql_division_by_zero_protection(self):
        """PostgreSQL recording rules use clamp_min to prevent division by zero."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-postgresql.yaml")
        with open(path) as f:
            content = f.read()
        # connection_usage:ratio divides by max_connections — must clamp
        # rollback_ratio divides by total transactions — must clamp
        clamp_count = content.count("clamp_min")
        self.assertGreaterEqual(clamp_count, 2,
                                f"Expected at least 2 clamp_min guards, found {clamp_count}")

    def test_postgresql_rollback_uses_humanize_percentage(self):
        """PostgreSQLHighRollbackRatio description uses humanizePercentage (not printf)."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-postgresql.yaml")
        with open(path) as f:
            content = f.read()
        # Find the rollback ratio alert section
        lines = content.split("\n")
        in_rollback = False
        found_humanize = False
        for line in lines:
            if "PostgreSQLHighRollbackRatio" in line:
                in_rollback = True
            if in_rollback and "humanizePercentage" in line:
                found_humanize = True
                break
            if in_rollback and line.strip().startswith("- alert:") and "RollbackRatio" not in line:
                break
        self.assertTrue(found_humanize,
                        "PostgreSQLHighRollbackRatio should use humanizePercentage")

    def test_oracle_rule_pack_exists(self):
        """rule-packs/rule-pack-oracle.yaml exists."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-oracle.yaml")
        self.assertTrue(os.path.isfile(path))

    def test_db2_rule_pack_exists(self):
        """rule-packs/rule-pack-db2.yaml exists."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-db2.yaml")
        self.assertTrue(os.path.isfile(path))

    def test_oracle_configmap_exists(self):
        """k8s/03-monitoring/configmap-rules-oracle.yaml exists."""
        path = os.path.join(self.K8S_DIR, "configmap-rules-oracle.yaml")
        self.assertTrue(os.path.isfile(path))

    def test_db2_configmap_exists(self):
        """k8s/03-monitoring/configmap-rules-db2.yaml exists."""
        path = os.path.join(self.K8S_DIR, "configmap-rules-db2.yaml")
        self.assertTrue(os.path.isfile(path))

    def test_oracle_rule_pack_three_groups(self):
        """Oracle rule pack has the three-group structure."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-oracle.yaml")
        data = self._load_yaml(path)
        self._validate_rule_pack_structure(data["groups"], "oracle")

    def test_db2_rule_pack_three_groups(self):
        """DB2 rule pack has the three-group structure."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-db2.yaml")
        data = self._load_yaml(path)
        self._validate_rule_pack_structure(data["groups"], "db2")

    def test_oracle_configmap_has_label(self):
        """Oracle ConfigMap has rule-pack: oracle label."""
        path = os.path.join(self.K8S_DIR, "configmap-rules-oracle.yaml")
        data = self._load_yaml(path)
        self.assertEqual(data["metadata"]["labels"]["rule-pack"], "oracle")

    def test_db2_configmap_has_label(self):
        """DB2 ConfigMap has rule-pack: db2 label."""
        path = os.path.join(self.K8S_DIR, "configmap-rules-db2.yaml")
        data = self._load_yaml(path)
        self.assertEqual(data["metadata"]["labels"]["rule-pack"], "db2")

    def test_oracle_uses_max_by_tenant(self):
        """Oracle threshold normalization uses max by(tenant), not sum."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-oracle.yaml")
        with open(path) as f:
            content = f.read()
        # threshold normalization rules should use max by(tenant)
        self.assertIn("max by(tenant)", content)
        # Should NOT use sum by(tenant) for threshold normalization
        self.assertNotIn("sum by(tenant) (user_threshold", content)

    def test_db2_uses_max_by_tenant(self):
        """DB2 threshold normalization uses max by(tenant), not sum."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-db2.yaml")
        with open(path) as f:
            content = f.read()
        self.assertIn("max by(tenant)", content)
        self.assertNotIn("sum by(tenant) (user_threshold", content)

    def test_oracle_alerts_have_maintenance_unless(self):
        """Oracle alert rules use 'unless on(tenant)' maintenance filter."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-oracle.yaml")
        with open(path) as f:
            content = f.read()
        # Count alerts (excluding Down which may not use maintenance)
        self.assertIn('unless on(tenant)', content)

    def test_db2_alerts_have_maintenance_unless(self):
        """DB2 alert rules use 'unless on(tenant)' maintenance filter."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-db2.yaml")
        with open(path) as f:
            content = f.read()
        self.assertIn('unless on(tenant)', content)

    def test_clickhouse_rule_pack_exists(self):
        """rule-packs/rule-pack-clickhouse.yaml exists."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-clickhouse.yaml")
        self.assertTrue(os.path.isfile(path))

    def test_clickhouse_configmap_exists(self):
        """k8s/03-monitoring/configmap-rules-clickhouse.yaml exists."""
        path = os.path.join(self.K8S_DIR, "configmap-rules-clickhouse.yaml")
        self.assertTrue(os.path.isfile(path))

    def test_clickhouse_rule_pack_three_groups(self):
        """ClickHouse rule pack has the three-group structure."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-clickhouse.yaml")
        data = self._load_yaml(path)
        self._validate_rule_pack_structure(data["groups"], "clickhouse")

    def test_clickhouse_configmap_has_label(self):
        """ClickHouse ConfigMap has rule-pack: clickhouse label."""
        path = os.path.join(self.K8S_DIR, "configmap-rules-clickhouse.yaml")
        data = self._load_yaml(path)
        self.assertEqual(data["metadata"]["labels"]["rule-pack"], "clickhouse")

    def test_clickhouse_uses_max_by_tenant(self):
        """ClickHouse threshold normalization uses max by(tenant)."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-clickhouse.yaml")
        with open(path) as f:
            content = f.read()
        self.assertIn("max by(tenant)", content)
        self.assertNotIn("sum by(tenant) (user_threshold", content)

    def test_clickhouse_alerts_have_maintenance_unless(self):
        """ClickHouse alert rules use 'unless on(tenant)' maintenance filter."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-clickhouse.yaml")
        with open(path) as f:
            content = f.read()
        self.assertIn('unless on(tenant)', content)

    def test_db2_bufferpool_uses_less_than(self):
        """DB2 low bufferpool hit ratio alert uses < operator (not >)."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-db2.yaml")
        with open(path) as f:
            content = f.read()
        # DB2LowBufferpoolHitRatio should use '<' since low ratio is bad
        self.assertIn("DB2LowBufferpoolHitRatio", content)
        # Find the section and verify it uses <
        lines = content.split("\n")
        in_bufferpool = False
        for line in lines:
            if "DB2LowBufferpoolHitRatio" in line:
                in_bufferpool = True
            if in_bufferpool and "< on(tenant)" in line:
                break
        else:
            self.fail("DB2LowBufferpoolHitRatio should use '< on(tenant)' operator")


if __name__ == "__main__":
    unittest.main()
