#!/usr/bin/env python3
"""test_config_diff.py — Directory-level Config Diff 測試套件 (Wave 12 pytest 遷移)。"""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml


import config_diff as cd  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]


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
    """欄位內容取自 rule pack，不是 key 拼法的 CamelCase（舊版寫 *MysqlConnections*）。"""

    def test_names_come_from_the_rule_packs(self):
        assert cd.estimate_affected_alerts("mysql_connections") == \
            "MariaDBHighConnections, MariaDBSystemBottleneck"

    def test_a_key_no_alert_reads(self):
        assert cd.estimate_affected_alerts("cpu") == "—"

    def test_unknown_when_the_rule_packs_are_missing(self, monkeypatch):
        monkeypatch.setattr(cd, "alerts_for_key", lambda key: None)
        assert cd.estimate_affected_alerts("mysql_connections") == "unknown"


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

def _write_tenant(directory, value="80"):
    path = os.path.join(directory, "db-a.yaml")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        yaml.dump({"tenants": {"db-a": {"mysql_connections": value}}}, f)
    return path


def _run_cli(old_dir, new_dir, *extra):
    """Invoke the tool the way a pipeline does — as a process — and read its rc."""
    return subprocess.run(
        [sys.executable, "-X", "utf8", cd.__file__,
         "--old-dir", str(old_dir), "--new-dir", str(new_dir), *extra],
        capture_output=True, encoding="utf-8", errors="replace", timeout=120,
    )


class TestExitCode:
    """Exit codes, exercised through the real entry point.

    ⛔ These used to assert ``(1 if diffs else 0) == 0`` — a reimplementation
    of the exit rule, checked against itself. Nothing ever started the
    process, so exit 2 had no coverage at all, and neither did any path where
    the tool dies before printing. That is how an uncaught exception exiting
    **1** survived: 1 is this tool's "changes detected" code, so every
    consumer following the documented contract reads a crash as a finding —
    while the report it supposedly produced is zero bytes.

    Contract (``scripts/tools/_lib_exitcodes.py``, restated for consumers in
    ``docs/integration/gitops-deployment.md``): 0 no changes / 1 changes
    detected / 2 caller error, which explicitly names malformed input and
    unexpected crashes.
    """

    def test_exit_0_no_changes(self):
        with tempfile.TemporaryDirectory() as d:
            _write_tenant(d)
            p = _run_cli(d, d)
        assert p.returncode == 0, p.stderr
        assert "No changes detected" in p.stdout

    def test_exit_1_changes_detected(self):
        with tempfile.TemporaryDirectory() as old_dir, \
             tempfile.TemporaryDirectory() as new_dir:
            _write_tenant(old_dir, "80")
            _write_tenant(new_dir, "50")
            p = _run_cli(old_dir, new_dir)
        assert p.returncode == 1, p.stderr
        assert "db-a" in p.stdout

    def test_exit_2_missing_dir(self):
        with tempfile.TemporaryDirectory() as new_dir:
            _write_tenant(new_dir)
            p = _run_cli(os.path.join(new_dir, "nope"), new_dir)
        assert p.returncode == 2, p.stderr
        assert p.stdout == ""

    @pytest.mark.parametrize("label,writer,extra,names_file", [
        # The yaml.YAMLError family.
        ("malformed_yaml",
         lambda p: Path(p).write_text("tenants:\n  db-a: [unclosed\n   x: :\n",
                                      encoding="utf-8", newline="\n"),
         (), True),
        # Not UTF-8. Until #1654 open() raised UnicodeDecodeError before the
        # parser was reached — NOT a YAMLError, and carrying no path, so this
        # row recorded names_file=False as a known gap. The shared loader now
        # hands PyYAML the bytes and wraps the ReaderError in YamlFileError
        # with the path attached, so the file is named like the row above.
        ("non_utf8",
         lambda p: Path(p).write_bytes(b"tenants:\n  db-a:\n    note: caf\xe9\n"),
         (), True),
        # A self-referencing anchor: yaml.safe_load ACCEPTS it, and json.dumps
        # is what raises — downstream of every loader, so no try/except placed
        # around the loading step can reach it.
        ("anchor_cycle_json",
         lambda p: Path(p).write_text("tenants: &a\n  db-a:\n    self: *a\n",
                                      encoding="utf-8", newline="\n"),
         ("--format", "json"), False),
    ])
    def test_exit_2_when_the_run_cannot_complete(self, label, writer, extra,
                                                 names_file):
        """Every way the run fails to finish lands on 2, never on 1.

        The three cases sit in three different layers (decode / parse /
        serialize) on purpose: that spread is the argument for catching
        broadly rather than naming exception types, and the third case is the
        one that makes a loader-level handler structurally insufficient.
        """
        with tempfile.TemporaryDirectory() as old_dir, \
             tempfile.TemporaryDirectory() as new_dir:
            _write_tenant(old_dir)
            writer(os.path.join(new_dir, "db-a.yaml"))
            p = _run_cli(old_dir, new_dir, *extra)

        assert p.returncode == 2, (
            f"{label}: expected 2 (caller error), got {p.returncode}; "
            "1 is indistinguishable from 'changes detected'.\n"
            f"stdout={p.stdout[:200]!r}\nstderr={p.stderr[-400:]!r}"
        )
        assert p.stdout == "", (
            f"{label}: a run that did not complete must not leave a partial "
            f"report for a consumer to post; got {p.stdout[:200]!r}"
        )
        # ⛔ This used to be `assert "ERROR" in p.stderr`, which matched the
        # handler's own literal prefix and so was true whenever the handler
        # existed at all — it constrained nothing about the message. What a
        # reader of a CI log actually has to decide is between "the image did
        # not pull", "the mount is wrong" and "one of my config files is
        # broken", so the message has to carry enough to separate those.
        assert str(old_dir) in p.stderr and str(new_dir) in p.stderr, (
            f"{label}: the message must name both directories — the exception "
            f"does not say which side it was reading; got {p.stderr[-300:]!r}"
        )
        # All three of these exception classes are named ``*Error``; str() of
        # some of them is terse enough that the class name is the only clue
        # to what kind of failure it was.
        assert "Error" in p.stderr, (
            f"{label}: the exception type name is missing from the message; "
            f"got {p.stderr[-300:]!r}"
        )
        if names_file:
            # The decisive one: repr() of a YAMLError replaces its Mark
            # objects with memory addresses, str() carries file/line/column.
            # Naming the file is what makes the message actionable, and it is
            # exactly what a regression back to `!r` would take away.
            assert "db-a.yaml" in p.stderr, (
                f"{label}: the message must name the file that failed to "
                f"parse; got {p.stderr[-300:]!r}"
            )
        else:
            # ⚠️ NOT an endorsement — a recorded gap. This exception does not
            # carry a path (a bare "Circular reference detected" raised by
            # json.dumps, downstream of every loader), so an operator is told
            # a config file is broken without being told which one. (The
            # non-UTF-8 row used to sit here too; #1654 closed that one in the
            # shared loader.) Asserting the absence keeps it from being
            # quietly "fixed" by a message that only looks more specific.
            assert "db-a.yaml" not in p.stderr, (
                f"{label}: the message now names the file — good, but this "
                "branch was recording that it could not. Move this case to "
                "names_file=True and delete this note."
            )


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


@pytest.mark.usefixtures("da_guard_env")
class TestProfileDiffEndToEnd:
    """End-to-end profile diff with directories. A changed profile asks
    da-guard which tenants name it (#2115), so da-guard is required."""

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

    def test_da_guard_failure_exits_2_with_its_reason(self, tmp_path):
        """#2115 F1：讀 profile 綁定時 da-guard 失敗（這裡是 `--new-dir` 的根
        `_defaults.yaml` 帶 `_profile: std`，exporter 整份丟掉、effective rc 3）——
        rc 2，stderr 除了 ERROR 行，還要有 da-guard 自己的原因行（`print_load_error`
        轉印）。只靠泛用 `except Exception` 時 rc 也是 2，但原因行會消失。"""
        old_dir, new_dir = tmp_path / "old", tmp_path / "new"
        for d, cpu in ((old_dir, 60), (new_dir, 50)):
            d.mkdir()
            (d / "_profiles.yaml").write_text(
                f"profiles:\n  std:\n    mysql_connections: {cpu}\n", encoding="utf-8")
            (d / "db-a.yaml").write_text("tenants:\n  db-a:\n    _profile: std\n",
                                         encoding="utf-8")
        (new_dir / "_defaults.yaml").write_text(
            "defaults:\n  mysql_connections: 80\n  _profile: std\n", encoding="utf-8")
        p = _run_cli(old_dir, new_dir)
        assert p.returncode == 2, (p.returncode, p.stdout, p.stderr)
        assert "reading the --new-dir profile bindings through da-guard failed" in p.stderr, p.stderr
        reason = [l for l in p.stderr.splitlines()
                  if l.lstrip().startswith("da-guard| ") and "skip unparseable" in l]
        assert reason and "_defaults.yaml" in reason[0], p.stderr

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


# ── Tenant `_` settings (decision H1b) ──────────────────────────────

def _write_settings(directory, tenant_cfg):
    path = Path(directory) / "db-a.yaml"
    path.write_text(yaml.safe_dump({"tenants": {"db-a": tenant_cfg}}),
                    encoding="utf-8", newline="\n")


class TestTenantSettingChanges:
    """`_` 開頭的租戶設定改了，要報出來、rc=1。

    ⛔ 過去 flatten_tenant_config 丟掉所有 `_` key，而沒有別處報它們：拿掉
    `_state_maintenance`（該租戶告警全部恢復）得到 "No changes detected"、rc=0。
    """

    BASE = {
        "mysql_connections": "80",
        "_silent_mode": "warning",
        "_state_container_crashloop": "disable",
        "_severity_dedup": "enable",
        "_routing": {"receiver": {"type": "webhook", "url": "https://a.example"},
                     "group_wait": "30s"},
        "_profile": "standard-db",
        "_metadata": {"db_type": "mariadb"},
        "_namespaces": ["ns1"],
        "_state_maintenance": {"target": "all", "expires": "2099-01-01T00:00:00Z"},
    }

    @pytest.mark.parametrize("key", [
        "_silent_mode", "_state_container_crashloop", "_severity_dedup",
        "_routing", "_profile", "_metadata", "_namespaces", "_state_maintenance",
    ])
    def test_removing_any_setting_is_reported(self, tmp_path, key):
        old, new = tmp_path / "old", tmp_path / "new"
        old.mkdir(), new.mkdir()
        _write_settings(old, self.BASE)
        _write_settings(new, {k: v for k, v in self.BASE.items() if k != key})
        p = _run_cli(old, new)
        assert p.returncode == 1, p.stdout + p.stderr
        assert "No changes detected" not in p.stdout
        assert f"- `{key}` (removed)" in p.stdout

    def test_a_key_not_known_today_is_reported_too(self, tmp_path):
        """讀的是所有 `_` key，不是一份已知清單：日後新增的保留 key 也看得到。"""
        old, new = tmp_path / "old", tmp_path / "new"
        old.mkdir(), new.mkdir()
        _write_settings(old, self.BASE)
        _write_settings(new, {**self.BASE, "_future_key": "on"})
        p = _run_cli(old, new)
        assert p.returncode == 1
        assert "- `_future_key` (added) — `on`" in p.stdout

    def test_nested_change_names_the_leaf(self, tmp_path):
        old, new = tmp_path / "old", tmp_path / "new"
        old.mkdir(), new.mkdir()
        _write_settings(old, self.BASE)
        changed = {**self.BASE, "_routing": {
            "receiver": {"type": "webhook", "url": "https://a.example"},
            "group_wait": "1m"}}
        _write_settings(new, changed)
        p = _run_cli(old, new)
        assert "- `_routing.group_wait`: `30s` → `1m`" in p.stdout
        assert "receiver" not in p.stdout


    def test_custom_alerts_stay_in_their_own_section(self):
        diff = cd.compute_setting_diff(
            {"t": {"_custom_alerts": []}},
            {"t": {"_custom_alerts": [{"name": "x"}]}})
        # load_settings_from_dir drops it; the diff itself is generic.
        assert diff
        with tempfile.TemporaryDirectory() as d:
            _write_settings(d, {"_custom_alerts": [{"name": "x"}],
                                "_silent_mode": "all"})
            assert cd.load_settings_from_dir(d) == {"db-a": {"_silent_mode": "all"}}

    def test_unchanged_settings_are_not_reported(self, tmp_path):
        _write_settings(tmp_path, self.BASE)
        p = _run_cli(tmp_path, tmp_path)
        assert p.returncode == 0
        assert "Tenant Setting Changes" not in p.stdout

    def test_json_carries_setting_diffs(self, tmp_path):
        old, new = tmp_path / "old", tmp_path / "new"
        old.mkdir(), new.mkdir()
        _write_settings(old, self.BASE)
        _write_settings(new, {**self.BASE, "_silent_mode": "all"})
        p = _run_cli(old, new, "--format", "json")
        assert p.returncode == 1
        doc = json.loads(p.stdout)
        assert doc["setting_diffs"] == {"db-a": [{
            "key": "_silent_mode", "old": "warning", "new": "all",
            "change": "modified"}]}

    def test_a_backtick_in_a_value_cannot_break_the_code_span(self, tmp_path):
        """值來自 PR 的檔案，屬不可信輸入（F5）。"""
        old, new = tmp_path / "old", tmp_path / "new"
        old.mkdir(), new.mkdir()
        _write_settings(old, self.BASE)
        _write_settings(new, {**self.BASE, "_profile": "x` **bold** `y"})
        p = _run_cli(old, new)
        line = next(ln for ln in p.stdout.splitlines() if ln.startswith("- `_profile`"))
        assert line.count("`") == 6, line


# A Slack incoming-webhook URL is the credential itself.
_SECRET_OLD = "https://hooks.slack.com/services/T0/B0/oldSECRETvalue"
_SECRET_NEW = "https://hooks.slack.com/services/T0/B0/newSECRETvalue"


class TestCredentialsAreNotPrinted:
    """receiver 的憑證欄位改了要報「有變」，但新舊值都不印。

    報告會貼成 PR comment：本文會寄給每個 watcher，改寫 git 歷史清掉檔案後
    也還在；輪替外洩憑證的 PR 還會把舊值再印一次。
    """

    BASE = {"_routing": {"receiver": {"type": "slack", "api_url": _SECRET_OLD},
                         "group_wait": "30s"}}

    def _run(self, tmp_path, old_cfg, new_cfg, *extra):
        old, new = tmp_path / "old", tmp_path / "new"
        old.mkdir(), new.mkdir()
        _write_settings(old, old_cfg)
        _write_settings(new, new_cfg)
        return _run_cli(old, new, *extra)

    def test_a_rotated_credential_is_reported_without_its_values(self, tmp_path):
        rotated = {"_routing": {"receiver": {"type": "slack", "api_url": _SECRET_NEW},
                                "group_wait": "30s"}}
        p = self._run(tmp_path, self.BASE, rotated)
        assert p.returncode == 1, p.stdout + p.stderr
        assert "- `_routing.receiver.api_url`: `<redacted>` → `<redacted>`" \
            in p.stdout
        assert "SECRET" not in p.stdout

    def test_json_redacts_too(self, tmp_path):
        rotated = {"_routing": {"receiver": {"type": "slack", "api_url": _SECRET_NEW},
                                "group_wait": "30s"}}
        p = self._run(tmp_path, self.BASE, rotated, "--format", "json")
        assert "SECRET" not in p.stdout
        assert json.loads(p.stdout)["setting_diffs"]["db-a"] == [{
            "key": "_routing.receiver.api_url", "old": "<redacted>",
            "new": "<redacted>", "change": "modified"}]

    @pytest.mark.parametrize("direction", ["added", "removed"])
    def test_a_whole_routing_block_is_redacted_inside(self, tmp_path, direction):
        without = {"_silent_mode": "all"}
        with_routing = {**without, **self.BASE}
        old, new = ((without, with_routing) if direction == "added"
                    else (with_routing, without))
        p = self._run(tmp_path, old, new)
        assert p.returncode == 1
        assert f"- `_routing` ({direction})" in p.stdout
        assert "SECRET" not in p.stdout
        assert "<redacted>" in p.stdout

    def test_credentials_inside_a_list_are_redacted(self):
        diff = cd.compute_setting_diff(
            {"t": {"_routing": {"overrides": []}}},
            {"t": {"_routing": {"overrides": [
                {"receiver": {"type": "pagerduty", "routing_key": "pdSECRET"}}]}}})
        assert "SECRET" not in json.dumps(diff)
        assert diff["t"][0]["new"] == [
            {"receiver": {"type": "pagerduty", "routing_key": "<redacted>"}}]

    def test_a_non_credential_change_still_prints_values(self, tmp_path):
        changed = {"_routing": {"receiver": {"type": "slack", "api_url": _SECRET_OLD},
                                "group_wait": "1m"}}
        p = self._run(tmp_path, self.BASE, changed)
        assert "- `_routing.group_wait`: `30s` → `1m`" in p.stdout
        assert "receiver" not in p.stdout

    def test_the_credential_list_covers_the_schema(self):
        """schema 裡長得像憑證的欄位，每一個都得歸類：要嘛遮、要嘛明列不是。

        日後 schema 新增 receiver 欄位而這裡沒跟上，這條就會紅，不會悄悄印出來。
        """
        schema = json.loads((REPO_ROOT / "docs" / "schemas"
                             / "tenant-config.schema.json").read_text(encoding="utf-8"))
        names = set()

        def walk(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    if k == "properties" and isinstance(v, dict):
                        names.update(v)
                    walk(v)
            elif isinstance(node, list):
                for x in node:
                    walk(x)
        walk(schema)
        looks_secret = {n for n in names
                        if re.search(r"pass|key|token|secret|url", n, re.I)}
        not_credentials = {"runbook_url", "icon_url", "client_url"}
        assert looks_secret - not_credentials == set(cd._CREDENTIAL_KEYS)
