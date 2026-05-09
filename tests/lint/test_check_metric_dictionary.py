"""Tests for scripts/tools/lint/check_metric_dictionary.py.

Gap 4 (HA-7 backlog) — lint tool self-test coverage. This is the
P1 entry for `check_metric_dictionary.py`. Pattern from the playbook:
≥1 positive case (`--ci` exit 0) + ≥1 negative case (intentional
violation → caller-error) + unit-level coverage of the parsers.

Covers:
  - load_dictionary_metrics (legacy keys + maps_to values)
  - load_dictionary_golden_rules (golden_rule extraction)
  - extract_rule_pack_metrics (PromQL token scan over rule-pack YAML +
    k8s ConfigMaps), including PROMQL_KEYWORDS filtering
  - check_dictionary_coverage (stale-entry warnings)
  - main CLI: empty registry, valid registry, JSON output, CI mode
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "tools" / "lint" / "check_metric_dictionary.py"

_spec = importlib.util.spec_from_file_location("check_metric_dictionary", _SCRIPT)
mod = importlib.util.module_from_spec(_spec)
sys.modules["check_metric_dictionary"] = mod
_spec.loader.exec_module(mod)


# ============================================================
# Helpers
# ============================================================


def _write(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


# ============================================================
# load_dictionary_metrics
# ============================================================


class TestLoadDictionaryMetrics:

    def test_missing_file_returns_empty_set(self, tmp_path):
        # Property: nonexistent path → empty set (no raise).
        assert mod.load_dictionary_metrics(tmp_path / "nope.yaml") == set()

    def test_returns_legacy_keys_and_maps_to(self, tmp_path):
        # Property: both the dict key (legacy metric) AND the
        # `maps_to:` target are surfaced.
        d = tmp_path / "metric-dict.yaml"
        _write(d,
            "old_metric_name:\n"
            "  maps_to: new_metric_name\n"
            "  rule_pack: db\n"
            "another_legacy:\n"
            "  maps_to: another_canonical\n"
            "  rule_pack: cache\n"
        )
        result = mod.load_dictionary_metrics(d)
        assert "old_metric_name" in result
        assert "new_metric_name" in result
        assert "another_legacy" in result
        assert "another_canonical" in result

    def test_entry_without_maps_to_only_yields_key(self, tmp_path):
        # Property: an entry without `maps_to` only contributes its key.
        d = tmp_path / "metric-dict.yaml"
        _write(d,
            "self_referential:\n"
            "  rule_pack: db\n"
            "  description: something\n"
        )
        result = mod.load_dictionary_metrics(d)
        assert result == {"self_referential"}

    def test_non_dict_top_level_returns_empty_set(self, tmp_path):
        # Property: malformed YAML (list at top level) → empty set.
        d = tmp_path / "metric-dict.yaml"
        _write(d, "- just_a_list\n- of_strings\n")
        assert mod.load_dictionary_metrics(d) == set()

    def test_empty_file_returns_empty_set(self, tmp_path):
        d = tmp_path / "metric-dict.yaml"
        _write(d, "")
        assert mod.load_dictionary_metrics(d) == set()


# ============================================================
# load_dictionary_golden_rules
# ============================================================


class TestLoadDictionaryGoldenRules:

    def test_missing_file_returns_empty(self, tmp_path):
        assert mod.load_dictionary_golden_rules(tmp_path / "nope.yaml") == set()

    def test_returns_golden_rule_field_only(self, tmp_path):
        d = tmp_path / "metric-dict.yaml"
        _write(d,
            "metric_a:\n"
            "  golden_rule: HighLatency\n"
            "  rule_pack: db\n"
            "metric_b:\n"
            "  golden_rule: HighErrorRate\n"
            "  maps_to: errors_total\n"
            "metric_c:\n"
            "  rule_pack: cache  # no golden_rule\n"
        )
        result = mod.load_dictionary_golden_rules(d)
        assert result == {"HighLatency", "HighErrorRate"}

    def test_non_dict_returns_empty(self, tmp_path):
        d = tmp_path / "metric-dict.yaml"
        _write(d, "- a\n- b\n")
        assert mod.load_dictionary_golden_rules(d) == set()


# ============================================================
# extract_rule_pack_metrics
# ============================================================


class TestExtractRulePackMetrics:

    def test_missing_dirs_returns_empty(self, tmp_path):
        # Property: when neither rule-pack source nor k8s dir exists,
        # the result is an empty mapping (not a raise).
        result = mod.extract_rule_pack_metrics(
            tmp_path / "nope-rule-packs",
            tmp_path / "nope-k8s",
        )
        assert result == {}

    def test_extracts_metrics_from_rule_pack(self, tmp_path):
        # Property: rule-pack-*.yaml files are parsed, metric-like
        # tokens in `expr` fields are extracted, keyed by pack name
        # (file stem with `rule-pack-` prefix stripped).
        rule_packs = tmp_path / "rule-packs"
        rule_packs.mkdir()
        _write(rule_packs / "rule-pack-db.yaml",
            "groups:\n"
            "  - name: db-rules\n"
            "    rules:\n"
            "      - alert: HighLatency\n"
            "        expr: rate(db_query_duration_seconds_total[5m])\n"
            "      - alert: ConnectionsHigh\n"
            "        expr: db_connections_active > 100\n"
        )
        result = mod.extract_rule_pack_metrics(rule_packs, tmp_path / "no-k8s")
        assert "db" in result
        # Real metric names get extracted (longer than 3 chars,
        # not in _PROMQL_KEYWORDS).
        assert "db_query_duration_seconds_total" in result["db"]
        assert "db_connections_active" in result["db"]
        # PromQL keywords are filtered out.
        assert "rate" not in result["db"]

    def test_short_tokens_filtered_out(self, tmp_path):
        # Property: tokens of 3 or fewer chars are filtered (the regex
        # captures `[a-z_][a-z0-9_]+` which has min length 2 but the
        # extractor adds `len > 3` filter).
        rule_packs = tmp_path / "rule-packs"
        rule_packs.mkdir()
        _write(rule_packs / "rule-pack-x.yaml",
            "groups:\n"
            "  - name: x\n"
            "    rules:\n"
            "      - alert: A\n"
            "        expr: ab + abc + abcd\n"  # ab/abc filtered, abcd kept
        )
        result = mod.extract_rule_pack_metrics(rule_packs, tmp_path / "nx")
        # `ab` and `abc` are too short; `abcd` survives.
        assert "abcd" in result["x"]
        assert "ab" not in result["x"]
        assert "abc" not in result["x"]

    def test_promql_keyword_filter(self, tmp_path):
        # Property: every keyword in _PROMQL_KEYWORDS is filtered, even
        # if it appears in metric position.
        rule_packs = tmp_path / "rule-packs"
        rule_packs.mkdir()
        # Build an expr that mentions every common keyword.
        keywords = ["rate", "sum", "count", "histogram_quantile",
                     "tenant", "severity", "alertname"]
        expr = " + ".join(f"{k}(real_metric_total)" for k in keywords)
        _write(rule_packs / "rule-pack-x.yaml",
            f"groups:\n"
            f"  - name: x\n"
            f"    rules:\n"
            f"      - alert: A\n"
            f"        expr: '{expr}'\n"
        )
        result = mod.extract_rule_pack_metrics(rule_packs, tmp_path / "nx")
        for kw in keywords:
            assert kw not in result["x"], (
                f"keyword {kw!r} leaked into metrics: {result['x']!r}"
            )
        # The real metric is captured.
        assert "real_metric_total" in result["x"]

    def test_empty_groups_yields_empty_set(self, tmp_path):
        rule_packs = tmp_path / "rule-packs"
        rule_packs.mkdir()
        _write(rule_packs / "rule-pack-empty.yaml", "groups: []\n")
        result = mod.extract_rule_pack_metrics(rule_packs, tmp_path / "nx")
        assert result == {"empty": set()}

    def test_k8s_configmap_extraction(self, tmp_path):
        # Property: k8s ConfigMaps with embedded YAML rules are also scanned.
        k8s = tmp_path / "k8s"
        k8s.mkdir()
        _write(k8s / "configmap-rules-db.yaml",
            "kind: ConfigMap\n"
            "metadata:\n"
            "  name: rules-db\n"
            "data:\n"
            "  rules.yaml: |\n"
            "    groups:\n"
            "      - name: db\n"
            "        rules:\n"
            "          - alert: A\n"
            "            expr: db_metric_from_configmap_total > 0\n"
        )
        result = mod.extract_rule_pack_metrics(tmp_path / "no-rp", k8s)
        assert "db" in result
        assert "db_metric_from_configmap_total" in result["db"]

    def test_k8s_non_configmap_yaml_skipped(self, tmp_path):
        # Property: a configmap-rules-*.yaml that isn't kind=ConfigMap is skipped.
        k8s = tmp_path / "k8s"
        k8s.mkdir()
        _write(k8s / "configmap-rules-broken.yaml",
            "kind: NotAConfigMap\n"
            "data:\n"
            "  rules.yaml: |\n"
            "    groups:\n"
            "      - name: x\n"
            "        rules:\n"
            "          - expr: should_not_appear_total\n"
        )
        result = mod.extract_rule_pack_metrics(tmp_path / "no-rp", k8s)
        # Empty pack (or no pack at all) — no metrics from non-CM file.
        for metrics in result.values():
            assert "should_not_appear_total" not in metrics


# ============================================================
# check_dictionary_coverage — stale-entry warnings
# ============================================================


class TestCheckDictionaryCoverage:

    def test_no_stale_when_legacy_used(self, tmp_path, monkeypatch):
        # Property: when the legacy key IS used in some Rule Pack, no warning.
        d = tmp_path / "metric-dict.yaml"
        _write(d,
            "old_metric:\n"
            "  maps_to: new_metric\n"
            "  rule_pack: db\n"
        )
        monkeypatch.setattr(mod, "METRIC_DICT", d)
        rule_pack_metrics = {"db": {"old_metric"}}  # legacy key in use
        issues = mod.check_dictionary_coverage(
            {"old_metric", "new_metric"}, set(), rule_pack_metrics)
        assert issues == []

    def test_no_stale_when_maps_to_used(self, tmp_path, monkeypatch):
        # Property: legacy key absent but `maps_to:` target present → not stale.
        # (Migration is in progress; new name is in use, old name is the
        # bridge that the dictionary holds.)
        d = tmp_path / "metric-dict.yaml"
        _write(d,
            "old_metric:\n"
            "  maps_to: new_metric\n"
            "  rule_pack: db\n"
        )
        monkeypatch.setattr(mod, "METRIC_DICT", d)
        rule_pack_metrics = {"db": {"new_metric"}}  # only maps_to target used
        issues = mod.check_dictionary_coverage(
            {"old_metric", "new_metric"}, set(), rule_pack_metrics)
        assert issues == []

    def test_stale_when_neither_used(self, tmp_path, monkeypatch):
        # Property: when NEITHER the legacy key NOR the maps_to target
        # appears in any Rule Pack expr, the entry is stale → warning.
        d = tmp_path / "metric-dict.yaml"
        _write(d,
            "ghost_metric:\n"
            "  maps_to: also_ghost\n"
            "  rule_pack: db\n"
        )
        monkeypatch.setattr(mod, "METRIC_DICT", d)
        rule_pack_metrics = {"db": {"unrelated_metric"}}
        issues = mod.check_dictionary_coverage(
            {"ghost_metric", "also_ghost"}, set(), rule_pack_metrics)
        assert len(issues) == 1
        assert issues[0]["check"] == "stale-entry"
        assert issues[0]["severity"] == "warning"
        assert "ghost_metric" in issues[0]["message"]


# ============================================================
# main — CLI / exit codes
# ============================================================


class TestMainCLI:

    def test_missing_metric_dict_exits_zero(self, tmp_path, monkeypatch, capsys):
        # Property: when the dictionary is missing entirely, lint exits
        # 0 (with a stderr WARNING) — bootstrap-friendly behavior.
        monkeypatch.setattr(mod, "METRIC_DICT", tmp_path / "nope.yaml")
        # main() reads sys.argv; clear it to no extra args.
        monkeypatch.setattr(sys, "argv", ["check_metric_dictionary"])
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 0
        captured = capsys.readouterr()
        assert "WARNING" in captured.err
        assert "不存在" in captured.err  # zh diagnostic per the tool

    def test_clean_dictionary_exits_zero(self, tmp_path, monkeypatch, capsys):
        # Positive: a dictionary whose every entry is in active use → exit 0.
        d = tmp_path / "metric-dict.yaml"
        _write(d,
            "old_metric:\n"
            "  maps_to: new_metric\n"
            "  golden_rule: SomeAlert\n"
            "  rule_pack: db\n"
        )
        monkeypatch.setattr(mod, "METRIC_DICT", d)

        rule_packs = tmp_path / "rule-packs"
        rule_packs.mkdir()
        _write(rule_packs / "rule-pack-db.yaml",
            "groups:\n  - name: db\n    rules:\n"
            "      - alert: A\n"
            "        expr: new_metric > 0\n"
        )
        monkeypatch.setattr(mod, "RULE_PACKS_DIR", rule_packs)
        monkeypatch.setattr(mod, "K8S_RULES_DIR", tmp_path / "no-k8s")
        monkeypatch.setattr(sys, "argv", ["check_metric_dictionary"])

        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "完全一致" in out  # success line per the tool

    def test_stale_entry_warning_does_not_fail_ci(
        self, tmp_path, monkeypatch, capsys
    ):
        # Negative: a stale entry produces a WARNING (not error). Per the
        # current CI contract, --ci only fails on errors; warnings are
        # informational. (Verified by reading the tool's `if args.ci and
        # errors:` guard at the bottom of main.)
        d = tmp_path / "metric-dict.yaml"
        _write(d,
            "ghost_metric:\n"
            "  maps_to: also_ghost\n"
            "  rule_pack: db\n"
        )
        monkeypatch.setattr(mod, "METRIC_DICT", d)

        rule_packs = tmp_path / "rule-packs"
        rule_packs.mkdir()
        _write(rule_packs / "rule-pack-db.yaml",
            "groups:\n  - name: db\n    rules:\n"
            "      - alert: A\n"
            "        expr: completely_unrelated_metric > 0\n"
        )
        monkeypatch.setattr(mod, "RULE_PACKS_DIR", rule_packs)
        monkeypatch.setattr(mod, "K8S_RULES_DIR", tmp_path / "no-k8s")
        monkeypatch.setattr(sys, "argv", ["check_metric_dictionary", "--ci"])

        with pytest.raises(SystemExit) as exc:
            mod.main()
        # Warnings only: --ci returns 0 when there are no errors.
        assert exc.value.code == 0
        out = capsys.readouterr().out
        # Stale-entry warning is surfaced in the human-readable output.
        assert "stale-entry" in out or "ghost_metric" in out

    def test_json_output_shape(self, tmp_path, monkeypatch, capsys):
        # Property: --json emits a JSON document with expected shape.
        d = tmp_path / "metric-dict.yaml"
        _write(d,
            "old:\n"
            "  maps_to: new\n"
            "  rule_pack: db\n"
        )
        monkeypatch.setattr(mod, "METRIC_DICT", d)
        monkeypatch.setattr(mod, "RULE_PACKS_DIR", tmp_path / "no-rp")
        monkeypatch.setattr(mod, "K8S_RULES_DIR", tmp_path / "no-k8s")
        monkeypatch.setattr(sys, "argv", ["check_metric_dictionary", "--json"])

        with pytest.raises(SystemExit):
            mod.main()
        payload = json.loads(capsys.readouterr().out)
        assert payload["check"] == "metric-dictionary"
        assert "dictionary_entries" in payload
        assert "rule_pack_metrics" in payload
        assert "rule_packs_scanned" in payload
        assert "issues" in payload
        assert "summary" in payload
        assert "errors" in payload["summary"]
        assert "warnings" in payload["summary"]


# ============================================================
# Repo registry passes its own validator (regression-style smoke test)
# ============================================================


class TestRepoRegistry:

    def test_repo_metric_dictionary_runs_clean_or_warn_only(self, monkeypatch):
        """The shipped metric-dictionary.yaml is scanned by its own lint.

        We accept any exit code 0/1: the tool never returns errors today
        (only warnings), but we ASSERT that running on the actual repo
        files doesn't raise / SystemExit with anything weird (e.g., 2 = config
        error). This is a regression guard against e.g. a rule-pack file
        becoming unparseable YAML.
        """
        monkeypatch.setattr(sys, "argv", ["check_metric_dictionary"])
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code in (0, 1), (
            f"unexpected exit code {exc.value.code} from repo scan"
        )
