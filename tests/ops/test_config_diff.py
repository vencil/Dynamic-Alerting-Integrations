#!/usr/bin/env python3
"""test_config_diff.py — Directory-level Config Diff 測試套件 (Wave 12 pytest 遷移)。"""

import json
import os
import tempfile

import pytest
import yaml


import config_diff as cd  # noqa: E402


# ── 1. Flatten Tenant Config ────────────────────────────────────────

class TestFlattenTenantConfig:

    def test_basic_flatten(self):
        raw = {"mysql_connections": 50, "redis_memory": 1024}
        result = cd.flatten_tenant_config(raw)
        assert result == {"mysql_connections": 50, "redis_memory": 1024}

    def test_skips_reserved_keys(self):
        raw = {"_routing": {"receiver": "slack"}, "_severity_dedup": "enable",
               "mysql_connections": 50}
        result = cd.flatten_tenant_config(raw)
        assert result == {"mysql_connections": 50}

    def test_empty_input(self):
        assert cd.flatten_tenant_config(None) == {}
        assert cd.flatten_tenant_config({}) == {}


# ── 2. Classify Change ──────────────────────────────────────────────

class TestClassifyChange:

    @pytest.mark.parametrize(
        "old, new, expected",
        [
            (None, 50, "added"),
            (50, None, "removed"),
            (80, 50, "tighter"),
            (50, 80, "looser"),
            (50, "disable", "toggled"),
            ("disable", 50, "toggled"),
            # dict vs dict → no numeric compare possible → modified
            ({"default": 50, "schedule": []}, {"default": 70, "schedule": []}, "modified"),
            (50, 50, "unchanged"),
        ],
        ids=[
            "added", "removed", "tighter", "looser",
            "toggled_disable", "toggled_enable", "modified_dict",
            "same_value_unchanged",
        ],
    )
    def test_classify_change(self, old, new, expected):
        assert cd.classify_change(old, new) == expected


# ── 3. Compute Diff ──────────────────────────────────────────────────

class TestComputeDiff:

    def test_basic_diff(self):
        old = {"db-a": {"mysql_connections": 80, "redis_memory": 1024}}
        new = {"db-a": {"mysql_connections": 50, "redis_memory": 1024}}
        diffs = cd.compute_diff(old, new)
        assert "db-a" in diffs
        assert len(diffs["db-a"]) == 1
        assert diffs["db-a"][0]["change"] == "tighter"

    def test_new_tenant(self):
        old = {}
        new = {"db-c": {"pg_cache": 0.9}}
        diffs = cd.compute_diff(old, new)
        assert "db-c" in diffs
        assert diffs["db-c"][0]["change"] == "added"

    def test_removed_tenant(self):
        old = {"db-x": {"mysql_connections": 50}}
        new = {}
        diffs = cd.compute_diff(old, new)
        assert "db-x" in diffs
        assert diffs["db-x"][0]["change"] == "removed"

    def test_no_changes(self):
        old = {"db-a": {"mysql_connections": 50}}
        new = {"db-a": {"mysql_connections": 50}}
        diffs = cd.compute_diff(old, new)
        assert diffs == {}

    def test_multiple_tenants(self):
        old = {"db-a": {"mysql_connections": 50}, "db-b": {"redis_memory": 100}}
        new = {"db-a": {"mysql_connections": 70}, "db-b": {"redis_memory": 100}}
        diffs = cd.compute_diff(old, new)
        assert "db-a" in diffs
        assert "db-b" not in diffs


# ── 4. Load Configs From Dir ────────────────────────────────────────

class TestLoadConfigsFromDir:

    def test_basic_loading_flat(self):
        """Flat format (legacy): {metric: value} without tenants: wrapper."""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "db-a.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"mysql_connections": 50, "_routing": {}}, f)
            result = cd.load_configs_from_dir(d)
            assert "db-a" in result
            assert "mysql_connections" in result["db-a"]
            assert "_routing" not in result["db-a"]

    def test_basic_loading_wrapped(self):
        """Wrapped format (actual conf.d/): {tenants: {name: {metric: value}}}."""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "db-a.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"tenants": {"db-a": {
                    "mysql_connections": "70",
                    "_routing": {"receiver": {"type": "webhook"}},
                }}}, f)
            result = cd.load_configs_from_dir(d)
            assert "db-a" in result
            assert "mysql_connections" in result["db-a"]
            assert "_routing" not in result["db-a"]

    def test_wrapped_multi_tenant_in_file(self):
        """Multiple tenants in a single wrapped YAML file."""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "teams.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"tenants": {
                    "db-a": {"mysql_connections": "70"},
                    "db-b": {"redis_memory": "1024"},
                }}, f)
            result = cd.load_configs_from_dir(d)
            assert "db-a" in result
            assert "db-b" in result
            assert result["db-a"] == {"mysql_connections": "70"}

    def test_skips_defaults_and_hidden(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "_defaults.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"mysql_connections": 99}, f)
            with open(os.path.join(d, ".hidden.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"x": 1}, f)
            result = cd.load_configs_from_dir(d)
            assert result == {}

    def test_missing_dir(self):
        result = cd.load_configs_from_dir("/nonexistent")
        assert result == {}


# ── 5. Estimate Affected Alerts ─────────────────────────────────────

class TestEstimateAffectedAlerts:

    def test_basic_conversion(self):
        assert cd.estimate_affected_alerts("mysql_connections") == "*MysqlConnections*"

    def test_single_word(self):
        assert cd.estimate_affected_alerts("cpu") == "*Cpu*"


# ── 6. Render Markdown ──────────────────────────────────────────────

class TestRenderMarkdown:

    def test_no_changes(self):
        md = cd.render_markdown({}, "old", "new")
        assert "No changes detected" in md

    def test_with_changes(self):
        diffs = {
            "db-a": [{"key": "mysql_connections", "old": 80, "new": 50, "change": "tighter"}]
        }
        md = cd.render_markdown(diffs, "old", "new")
        assert "db-a" in md
        assert "mysql_connections" in md
        assert "tighter" in md
        assert "Summary:" in md
        assert "1 tenant(s) changed" in md

    def test_format_value_disabled(self):
        assert cd._format_value("disable") == "disabled"
        assert cd._format_value(None) == "—"
        assert cd._format_value(50) == "50"
        assert cd._format_value({"schedule": []}) == "(scheduled)"

    def test_with_profile_changes(self):
        """The '## Profile Changes' section renders all three pd shapes:
        a modified profile with a key-diff table and >10 affected tenants
        (truncation), an added profile, and a profile with no key diffs.
        """
        profile_diffs = [
            {
                "profile": "mysql-prod",
                "change": "modified",
                "affected_count": 12,
                "affected_tenants": [f"t{i}" for i in range(12)],
                "key_diffs": [
                    {"key": "mysql_connections", "old": 80, "new": 50,
                     "change": "tighter"},
                ],
            },
            {
                "profile": "redis-new",
                "change": "added",
                "affected_count": 1,
                "affected_tenants": ["t-redis"],
                "key_diffs": [],
            },
            {
                "profile": "empty-prof",
                "change": "modified",
                "affected_count": 0,
                "affected_tenants": [],
                "key_diffs": [],
            },
        ]
        md = cd.render_markdown({}, "old", "new", profile_diffs=profile_diffs)

        assert "## Profile Changes" in md
        # modified profile: header + truncated tenant list + key-diff table
        assert "Profile: mysql-prod (modified) — 12 tenant(s) affected" in md
        assert "(+2 more)" in md          # 12 affected, first 10 shown
        assert "Tenants:" in md
        assert "| mysql_connections |" in md
        assert "tighter" in md
        # added profile with no key diffs → "New profile with N keys" line
        assert "New profile with 0 keys" in md
        # third profile (modified, no key diffs, no tenants) still renders
        assert "empty-prof" in md


# ── 7. CLI ───────────────────────────────────────────────────────────

class TestCLI:

    def test_required_args(self):
        parser = cd.build_parser()
        args = parser.parse_args(["--old-dir", "/a", "--new-dir", "/b"])
        assert args.old_dir == "/a"
        assert args.new_dir == "/b"

    def test_json_flag(self):
        parser = cd.build_parser()
        args = parser.parse_args(["--old-dir", "/a", "--new-dir", "/b", "--json-output"])
        assert args.json_output

    def test_missing_required(self):
        parser = cd.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])


# ── 8. End-to-End ────────────────────────────────────────────────────

class TestEndToEnd:

    def test_directory_comparison_flat(self):
        with tempfile.TemporaryDirectory() as old_dir, \
             tempfile.TemporaryDirectory() as new_dir:
            # Old config (flat)
            with open(os.path.join(old_dir, "db-a.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"mysql_connections": 80, "redis_memory": 1024}, f)
            # New config — tighter mysql, same redis
            with open(os.path.join(new_dir, "db-a.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"mysql_connections": 50, "redis_memory": 1024}, f)

            old = cd.load_configs_from_dir(old_dir)
            new = cd.load_configs_from_dir(new_dir)
            diffs = cd.compute_diff(old, new)

            assert len(diffs) == 1
            assert diffs["db-a"][0]["key"] == "mysql_connections"
            assert diffs["db-a"][0]["change"] == "tighter"

    def test_directory_comparison_wrapped(self):
        """End-to-end test with actual conf.d/ format (tenants: wrapper)."""
        with tempfile.TemporaryDirectory() as old_dir, \
             tempfile.TemporaryDirectory() as new_dir:
            # Old config (wrapped format)
            with open(os.path.join(old_dir, "db-a.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"tenants": {"db-a": {
                    "mysql_connections": "80",
                    "_routing": {"receiver": {"type": "webhook"}},
                }}}, f)
            # New config — tighter mysql
            with open(os.path.join(new_dir, "db-a.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"tenants": {"db-a": {
                    "mysql_connections": "50",
                    "_routing": {"receiver": {"type": "webhook"}},
                }}}, f)

            old = cd.load_configs_from_dir(old_dir)
            new = cd.load_configs_from_dir(new_dir)
            diffs = cd.compute_diff(old, new)

            assert len(diffs) == 1
            assert "db-a" in diffs
            assert diffs["db-a"][0]["key"] == "mysql_connections"
            assert diffs["db-a"][0]["change"] == "tighter"



# ── Exit Code Tests (v1.11.0 CI integration) ─────────────────────

class TestExitCode:
    """config_diff.py exit codes for CI pipeline integration."""

    def test_exit_0_no_changes(self):
        """Identical directories → exit 0."""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "db-a.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"tenants": {"db-a": {"mysql_connections": "80"}}}, f)
            old = cd.load_configs_from_dir(d)
            new = cd.load_configs_from_dir(d)
            diffs = cd.compute_diff(old, new)
            # Exit code logic: 1 if diffs else 0
            assert (1 if diffs else 0) == 0

    def test_exit_1_changes_detected(self):
        """Different directories → exit 1 (signal to CI)."""
        with tempfile.TemporaryDirectory() as old_dir, \
             tempfile.TemporaryDirectory() as new_dir:
            with open(os.path.join(old_dir, "db-a.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"tenants": {"db-a": {"mysql_connections": "80"}}}, f)
            with open(os.path.join(new_dir, "db-a.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"tenants": {"db-a": {"mysql_connections": "50"}}}, f)
            old = cd.load_configs_from_dir(old_dir)
            new = cd.load_configs_from_dir(new_dir)
            diffs = cd.compute_diff(old, new)
            assert (1 if diffs else 0) == 1


# ── 9. Profile Key Diff (v1.12.0 fine-grained) ───────────────────

class TestProfileKeyDiff:
    """Fine-grained profile content diff."""

    def test_added_profile(self):
        """New profile should show all keys as added."""
        diffs = cd.compute_profile_key_diff(None, {"mysql_connections": 80, "redis_memory": 1024})
        assert len(diffs) == 2
        assert all(d["change"] == "added" for d in diffs)

    def test_removed_profile(self):
        """Removed profile should show all keys as removed."""
        diffs = cd.compute_profile_key_diff({"mysql_connections": 80}, None)
        assert len(diffs) == 1
        assert diffs[0]["change"] == "removed"

    def test_modified_key(self):
        """Changed key should show tighter/looser."""
        diffs = cd.compute_profile_key_diff(
            {"mysql_connections": 80},
            {"mysql_connections": 50}
        )
        assert len(diffs) == 1
        assert diffs[0]["change"] == "tighter"

    def test_no_changes(self):
        """Identical profiles should produce no diffs."""
        diffs = cd.compute_profile_key_diff(
            {"mysql_connections": 80}, {"mysql_connections": 80})
        assert diffs == []


class TestCustomAlertDiff:
    """Custom alert recipe diffing (ADR-024 Capability B, #741).

    Regression for the ops-review blind spot: `_custom_alerts` is a reserved
    ('_'-prefixed) key, so flatten_tenant_config drops it from the metric diff.
    These tests pin the dedicated recipe-diff path that surfaces add/remove/
    modify of recipes so config_diff never reports 'No changes detected' for a
    real alerting change.
    """

    def _recipe(self, name, threshold="150:warning", mode="page", **extra):
        r = {
            "recipe": "threshold",
            "name": name,
            "metric": "mysql_global_status_threads_connected",
            "op": ">",
            "window": "5m",
            "threshold": threshold,
            "mode": mode,
        }
        r.update(extra)
        return r

    def _write(self, d, tenant, recipes):
        with open(os.path.join(d, f"{tenant}.yaml"), "w", encoding="utf-8") as f:
            yaml.dump({"tenants": {tenant: {
                "mysql_connections": "100",
                "_custom_alerts": recipes,
            }}}, f)

    def test_added_recipe(self):
        with tempfile.TemporaryDirectory() as old_dir, \
             tempfile.TemporaryDirectory() as new_dir:
            self._write(old_dir, "db-b", [])
            self._write(new_dir, "db-b", [self._recipe("mariadb_conns_high")])

            old_ca = cd.load_custom_alerts_from_dir(old_dir)
            new_ca = cd.load_custom_alerts_from_dir(new_dir)
            diff = cd.compute_custom_alert_diff(old_ca, new_ca)

            assert "db-b" in diff
            assert len(diff["db-b"]) == 1
            assert diff["db-b"][0]["name"] == "mariadb_conns_high"
            assert diff["db-b"][0]["change"] == "added"

    def test_removed_recipe(self):
        with tempfile.TemporaryDirectory() as old_dir, \
             tempfile.TemporaryDirectory() as new_dir:
            self._write(old_dir, "db-b", [self._recipe("mariadb_conns_high")])
            self._write(new_dir, "db-b", [])

            diff = cd.compute_custom_alert_diff(
                cd.load_custom_alerts_from_dir(old_dir),
                cd.load_custom_alerts_from_dir(new_dir),
            )
            assert diff["db-b"][0]["change"] == "removed"

    def test_modified_recipe_surfaces_field_changes(self):
        with tempfile.TemporaryDirectory() as old_dir, \
             tempfile.TemporaryDirectory() as new_dir:
            self._write(old_dir, "db-b", [self._recipe("x", threshold="150:warning")])
            self._write(new_dir, "db-b", [self._recipe("x", threshold="200:critical")])

            diff = cd.compute_custom_alert_diff(
                cd.load_custom_alerts_from_dir(old_dir),
                cd.load_custom_alerts_from_dir(new_dir),
            )
            entry = diff["db-b"][0]
            assert entry["change"] == "modified"
            fields = {fc["field"]: fc for fc in entry["field_changes"]}
            assert "threshold" in fields
            assert fields["threshold"]["old"] == "150:warning"
            assert fields["threshold"]["new"] == "200:critical"

    def test_mode_toggle_is_surfaced(self):
        """page → silent (or vice versa) is a routing-impacting change."""
        with tempfile.TemporaryDirectory() as old_dir, \
             tempfile.TemporaryDirectory() as new_dir:
            self._write(old_dir, "db-b", [self._recipe("x", mode="page")])
            self._write(new_dir, "db-b", [self._recipe("x", mode="silent")])

            diff = cd.compute_custom_alert_diff(
                cd.load_custom_alerts_from_dir(old_dir),
                cd.load_custom_alerts_from_dir(new_dir),
            )
            fields = {fc["field"]: fc for fc in diff["db-b"][0]["field_changes"]}
            assert fields["mode"]["old"] == "page"
            assert fields["mode"]["new"] == "silent"

    def test_no_change_when_identical(self):
        with tempfile.TemporaryDirectory() as old_dir, \
             tempfile.TemporaryDirectory() as new_dir:
            for d in (old_dir, new_dir):
                self._write(d, "db-b", [self._recipe("x")])
            diff = cd.compute_custom_alert_diff(
                cd.load_custom_alerts_from_dir(old_dir),
                cd.load_custom_alerts_from_dir(new_dir),
            )
            assert diff == {}

    def test_summary_counts_custom_alert_only_tenant(self):
        """CodeRabbit #773: a custom-alert-only change must count toward
        'N tenant(s) changed', not print '0 tenant(s) changed'."""
        diff = {"db-b": [{
            "name": "x", "change": "added", "old": None,
            "new": self._recipe("x"), "field_changes": [],
        }]}
        md = cd.render_markdown({}, "o", "n", custom_alert_diffs=diff)
        assert "0 tenant(s) changed" not in md
        assert "1 tenant(s) changed" in md

    def test_summary_unions_metric_and_custom_alert_tenants(self):
        """Same tenant changed via both metric + custom alert counts once."""
        metric = {"db-b": [{"key": "mysql_connections", "old": 80, "new": 50,
                            "change": "tighter"}]}
        ca = {"db-b": [{"name": "x", "change": "added", "old": None,
                        "new": self._recipe("x"), "field_changes": []}],
              "db-c": [{"name": "y", "change": "added", "old": None,
                        "new": self._recipe("y"), "field_changes": []}]}
        md = cd.render_markdown(metric, "o", "n", custom_alert_diffs=ca)
        assert "2 tenant(s) changed" in md  # db-b (union, once) + db-c

    def test_render_markdown_surfaces_custom_alerts(self):
        """The PR #771 scenario must NOT render as 'No changes detected'."""
        diff = {"db-b": [{
            "name": "mariadb_conns_high", "change": "added",
            "old": None,
            "new": self._recipe("mariadb_conns_high"),
            "field_changes": [],
        }]}
        md = cd.render_markdown({}, "old", "new", custom_alert_diffs=diff)
        assert "No changes detected" not in md
        assert "Custom Alert Changes" in md
        assert "mariadb_conns_high" in md
        assert "db-b" in md

    def test_load_skips_recipes_without_name(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "db-b", [{"recipe": "threshold", "metric": "m"}])
            assert cd.load_custom_alerts_from_dir(d) == {}

    def test_disable_threshold_is_semantically_highlighted(self):
        """Reef 1: threshold → 'disable' is a silencing, not a param tweak.
        Must be flagged so a reviewer can't skim past it."""
        diff = {"db-b": [{
            "name": "x", "change": "modified",
            "old": self._recipe("x", threshold="100:critical"),
            "new": self._recipe("x", threshold="disable"),
            "field_changes": [
                {"field": "threshold", "old": "100:critical", "new": "disable"},
            ],
        }]}
        md = cd.render_markdown({}, "o", "n", custom_alert_diffs=diff)
        assert "DISABLED" in md
        assert ":warning:" in md

    def test_disable_highlight_covers_full_disabled_set(self):
        """R2: parity with exporter — off/disabled/false + :severity suffix."""
        for disabled_val in ("off", "disabled", "false", "disable:warning"):
            diff = {"db-b": [{
                "name": "x", "change": "modified",
                "old": self._recipe("x", threshold="100:critical"),
                "new": self._recipe("x", threshold=disabled_val),
                "field_changes": [
                    {"field": "threshold", "old": "100:critical", "new": disabled_val},
                ],
            }]}
            md = cd.render_markdown({}, "o", "n", custom_alert_diffs=diff)
            assert "DISABLED" in md, f"missed disabled form: {disabled_val!r}"

    def test_threshold_disabled_helper_parity(self):
        assert cd._threshold_disabled("disable")
        assert cd._threshold_disabled("off:warning")
        assert not cd._threshold_disabled("150:critical")
        assert not cd._threshold_disabled("150")

    def test_mode_page_to_silent_is_highlighted(self):
        diff = {"db-b": [{
            "name": "x", "change": "modified",
            "old": self._recipe("x", mode="page"), "new": self._recipe("x", mode="silent"),
            "field_changes": [{"field": "mode", "old": "page", "new": "silent"}],
        }]}
        md = cd.render_markdown({}, "o", "n", custom_alert_diffs=diff)
        assert "paging suppressed" in md

    def test_disable_to_enable_not_flagged_as_silencing(self):
        """Re-enabling (disable → value) must NOT carry the silencing warning."""
        diff = {"db-b": [{
            "name": "x", "change": "modified",
            "old": self._recipe("x", threshold="disable"),
            "new": self._recipe("x", threshold="100:critical"),
            "field_changes": [{"field": "threshold", "old": "disable", "new": "100:critical"}],
        }]}
        md = cd.render_markdown({}, "o", "n", custom_alert_diffs=diff)
        assert "DISABLED (alert silenced)" not in md

    def test_format_recipe_value_neutralizes_backticks(self):
        """F5: a backtick is the only code-span break-out; it must be stripped."""
        assert "`" not in cd._format_recipe_value("evil`backtick")
        # _code_span wraps in exactly two backticks (open/close), none from value
        span = cd._code_span("a`b`c")
        assert span.count("`") == 2

    def test_render_neutralizes_injection_in_recipe_value(self):
        """F5: a crafted selectors value cannot break out of its code span.
        Every code span is a backtick PAIR, so a leaked value-backtick would
        make the total count odd. Balanced count == no break-out."""
        evil = {"label": "v\"</details><script>alert(1)</script>`backtick`"}
        diff = {"t": [{
            "name": "evil_recipe", "change": "modified",
            "old": self._recipe("evil_recipe"),
            "new": self._recipe("evil_recipe", selectors=evil),
            "field_changes": [{"field": "selectors", "old": None, "new": evil}],
        }]}
        md = cd.render_markdown({}, "o", "n", custom_alert_diffs=diff)
        assert md.count("`") % 2 == 0, "unbalanced backticks → code-span break-out"
        # The value's own backticks are neutralized to single-quotes
        assert "`backtick`" not in md

    def test_render_markdown_truncates_oversized_output(self):
        """Reef 2: never exceed GitHub's comment limit — truncate instead of
        letting the bot 422 and post nothing on a high-risk PR."""
        # ~2000 tenants each with a recipe → far over the limit
        big = {
            f"tenant-{i:05d}": [{
                "name": f"recipe_{i}", "change": "added",
                "old": None, "new": self._recipe(f"recipe_{i}"), "field_changes": [],
            }]
            for i in range(2000)
        }
        md = cd.render_markdown({}, "o", "n", custom_alert_diffs=big)
        assert len(md) <= cd.COMMENT_SAFETY_LIMIT
        assert len(md) < cd.GITHUB_COMMENT_HARD_LIMIT
        assert "truncated" in md

    def test_newline_in_value_does_not_shatter_markdown(self):
        """Reef 4: a raw newline in a value breaks the code span / list item."""
        assert "\n" not in cd._format_recipe_value("line1\nline2")
        assert "\n" not in cd._format_recipe_value("a\r\nb")
        diff = {"t": [{
            "name": "x", "change": "modified",
            "old": self._recipe("x"),
            "new": self._recipe("x", selectors={"k": "a\nb\nc"}),
            "field_changes": [{"field": "selectors", "old": None, "new": {"k": "a\nb\nc"}}],
        }]}
        md = cd.render_markdown({}, "o", "n", custom_alert_diffs=diff)
        # No bullet line should be split by an injected newline from the value
        for line in md.splitlines():
            assert line.count("`") % 2 == 0, f"unbalanced code span: {line!r}"

    def test_string_typed_custom_alerts_does_not_crash(self):
        """Reef 5: mistyped `_custom_alerts: 'oops'` must not iterate chars and
        AttributeError → CI bot crash. Strict isinstance(list) guard skips it."""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "t.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"tenants": {"t": {"_custom_alerts": "to be added"}}}, f)
            assert cd.load_custom_alerts_from_dir(d) == {}  # no crash, no recipes

    def test_dict_typed_custom_alerts_does_not_crash(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "t.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"tenants": {"t": {"_custom_alerts": {"recipe": "x"}}}}, f)
            assert cd.load_custom_alerts_from_dir(d) == {}

    def test_none_to_empty_list_is_equivalent_no_diff(self):
        """Reef 6: missing key ≡ empty list → no phantom change."""
        with tempfile.TemporaryDirectory() as od, tempfile.TemporaryDirectory() as nd:
            with open(os.path.join(od, "t.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"tenants": {"t": {"mysql_connections": "1"}}}, f)  # no key
            with open(os.path.join(nd, "t.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"tenants": {"t": {"mysql_connections": "1",
                                             "_custom_alerts": []}}}, f)  # empty
            diff = cd.compute_custom_alert_diff(
                cd.load_custom_alerts_from_dir(od),
                cd.load_custom_alerts_from_dir(nd),
            )
            assert diff == {}

    def test_json_output_includes_custom_alert_diffs(self):
        with tempfile.TemporaryDirectory() as old_dir, \
             tempfile.TemporaryDirectory() as new_dir:
            self._write(old_dir, "db-b", [])
            self._write(new_dir, "db-b", [self._recipe("x")])
            ca = cd.compute_custom_alert_diff(
                cd.load_custom_alerts_from_dir(old_dir),
                cd.load_custom_alerts_from_dir(new_dir),
            )
            output = {"metric_diffs": {}, "profile_diffs": [], "custom_alert_diffs": ca}
            parsed = json.loads(json.dumps(output, default=str))
            assert "custom_alert_diffs" in parsed
            assert parsed["custom_alert_diffs"]["db-b"][0]["change"] == "added"


class TestProfileDiffEndToEnd:
    """End-to-end profile diff with directories."""

    def test_profile_modified_with_key_diffs(self):
        """Modified profile should include key_diffs in result."""
        with tempfile.TemporaryDirectory() as old_dir, \
             tempfile.TemporaryDirectory() as new_dir:
            # Old profile
            with open(os.path.join(old_dir, "_profiles.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"profiles": {"standard": {
                    "mysql_connections": 80, "redis_memory": 1024
                }}}, f)
            # New profile — tighter mysql
            with open(os.path.join(new_dir, "_profiles.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"profiles": {"standard": {
                    "mysql_connections": 50, "redis_memory": 1024
                }}}, f)
            # Tenant referencing profile
            for d in (old_dir, new_dir):
                with open(os.path.join(d, "db-a.yaml"), "w", encoding="utf-8") as f:
                    yaml.dump({"tenants": {"db-a": {"_profile": "standard"}}}, f)

            results = cd.compute_profile_diff(old_dir, new_dir)
            assert len(results) == 1
            assert results[0]["profile"] == "standard"
            assert results[0]["change"] == "modified"
            assert len(results[0]["key_diffs"]) == 1
            assert results[0]["key_diffs"][0]["key"] == "mysql_connections"
            assert results[0]["key_diffs"][0]["change"] == "tighter"

    def test_profile_added_with_key_diffs(self):
        """Added profile should list all keys as added."""
        with tempfile.TemporaryDirectory() as old_dir, \
             tempfile.TemporaryDirectory() as new_dir:
            # No profile in old
            with open(os.path.join(old_dir, "_profiles.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"profiles": {}}, f)
            # New profile
            with open(os.path.join(new_dir, "_profiles.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"profiles": {"new-profile": {
                    "mysql_connections": 80
                }}}, f)

            results = cd.compute_profile_diff(old_dir, new_dir)
            assert len(results) == 1
            assert results[0]["change"] == "added"
            assert len(results[0]["key_diffs"]) == 1
            assert results[0]["key_diffs"][0]["change"] == "added"

    def test_json_output_includes_key_diffs(self):
        """JSON output should include key_diffs in profile_diffs."""
        with tempfile.TemporaryDirectory() as old_dir, \
             tempfile.TemporaryDirectory() as new_dir:
            with open(os.path.join(old_dir, "_profiles.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"profiles": {"s": {"x": 80}}}, f)
            with open(os.path.join(new_dir, "_profiles.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"profiles": {"s": {"x": 50}}}, f)

            profile_diffs = cd.compute_profile_diff(old_dir, new_dir)
            output = {"metric_diffs": {}, "profile_diffs": profile_diffs}
            j = json.dumps(output, default=str)
            parsed = json.loads(j)
            assert "key_diffs" in parsed["profile_diffs"][0]
