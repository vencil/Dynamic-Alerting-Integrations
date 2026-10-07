"""#2326 step 2 — the routing plane reads a hierarchical conf.d.

Measured on the base of this change (``ce4057b2``): root ``_defaults.yaml``
with ``_routing_defaults``, a root tenant ``roota.yaml`` and a nested
``team/nestedb.yaml``::

    $ generate-routes --config-dir $T --dry-run
    WARN: routing generator: read … FLAT (top level only), but 1 config
          file(s) live in subdirectories and were SKIPPED: team/nestedb.yaml
    Found 1 tenant(s) with routing config: roota
    rc=0

The route tree a customer deploys had no route for ``nestedb`` — its alerts
fell to the catch-all — while threshold-exporter served its thresholds.
ADR-017 / ADR-016 / ADR-007 "Amendment 2026-09-28" (option P2) make the
routing plane hierarchical; this file pins the semantics listed there:

* tenants at any depth get routes; hidden directories and README-only
  directories contribute nothing (controls);
* (a) `_routing_defaults` along the chain — shallow per top-level key,
  deeper wins; `receiver` / `overrides` null below the root → rc 2;
* (b) `_routing_enforced` below the root → rc 2;
* (c) profiles visible to their subtree; one name in two files → rc 2;
* (d) subtree policies scoped + additive; an entry naming a tenant outside
  the subtree is ERROR under --strict, WARN otherwise, worded apart from
  "not found";
* (e) one tenant id in two files → rc 2;
* (f) a nested platform file's `tenants:` block → WARN, read by no plane.

⛔ rc 2 has several unrelated causes in this tool (#1642), so every refusal
below checks the MESSAGE as well as the code, and that nothing was written.
The cross-language half of the same semantics is the ``hier-*`` trees of
``tests/shared/routing_policy_parity_matrix.json``.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
_GAR = REPO / "scripts" / "tools" / "ops" / "generate_alertmanager_routes.py"
_LINT = REPO / "scripts" / "tools" / "lint" / "check_routing_profiles.py"
_VC = REPO / "scripts" / "tools" / "ops" / "validate_config.py"
sys.path.insert(0, str(REPO / "scripts" / "tools"))
sys.path.insert(0, str(REPO / "scripts" / "tools" / "ops"))
from _grar_merge import chain_levels, level_contains  # noqa: E402
from _grar_parse import load_tenant_tree  # noqa: E402

# #2115 0-B／B3: validate-config 的 profiles 列經 da-guard 讀租戶，每次都需要 da-guard
# （conftest 以 `go build` 建出；建不起來就 fail、不 skip）。
pytestmark = pytest.mark.usefixtures("da_guard_env")

EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR = 0, 1, 2
_TREE_ERROR = "routing-tree error(s) — nothing was generated, written or applied"

_EMAIL_RD = ("_routing_defaults:\n  receiver:\n    type: email\n"
             "    to: [oncall@example.com]\n    from: alerting@example.com\n"
             "    smarthost: smtp.example.com:587\n  group_wait: 30s\n")
_SLACK_RD = ("_routing_defaults:\n  receiver:\n    type: slack\n"
             "    api_url: https://hooks.slack.com/services/T/B/x\n")


def _tenant(tid: str, extra: str = "") -> str:
    return f"tenants:\n  {tid}:\n    mysql_connections: \"70\"\n{extra}"


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return root


def _gar(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-s", str(_GAR), *argv],
                          capture_output=True, text=True, encoding="utf-8",
                          timeout=180)


def _issue_tree(tmp_path: Path) -> Path:
    return _write(tmp_path / "conf.d", {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n" + _EMAIL_RD,
        "roota.yaml": _tenant("roota"),
        "team/nestedb.yaml": _tenant("nestedb"),
    })


# ── the ticket's tree, and the controls ──────────────────────────────────


class TestNestedTenantsAreRouted:
    def test_issue_tree_routes_the_nested_tenant(self, tmp_path):
        res = _gar("--config-dir", str(_issue_tree(tmp_path)), "--dry-run")
        assert res.returncode == EXIT_OK, res.stderr
        assert "Found 2 tenant(s) with routing config: nestedb, roota" in res.stdout
        assert 'tenant="nestedb"' in res.stdout and "receiver: tenant-nestedb" in res.stdout
        assert "FLAT" not in res.stderr and "SKIPPED" not in res.stderr, res.stderr

    def test_validate_strict_is_ok(self, tmp_path):
        res = _gar("--config-dir", str(_issue_tree(tmp_path)), "--validate", "--strict")
        assert res.returncode == EXIT_OK, res.stderr
        assert "OK: all configs valid" in res.stdout

    def test_readme_only_and_hidden_directories_contribute_nothing(self, tmp_path):
        d = _issue_tree(tmp_path)
        _write(d, {"docs/README.md": "# notes\n",
                   ".git/stray.yaml": _tenant("ghost"),
                   ".hidden/_defaults.yaml": _SLACK_RD})
        tree = load_tenant_tree(str(d))
        assert set(tree.routing_configs) == {"roota", "nestedb"}
        assert tree.routing_configs["nestedb"]["receiver"]["type"] == "email"
        assert tree.routing_tree_problems == []


# ── (a) the `_routing_defaults` chain ────────────────────────────────────


def test_chain_levels_and_containment():
    assert chain_levels(".") == [] and chain_levels("") == []
    assert chain_levels("a/b") == ["a", "a/b"]
    assert level_contains(".", "x") and level_contains("team", "team/sub")
    assert not level_contains("team", "team-b")
    assert not level_contains("team/sub", "team")


class TestRoutingDefaultsChain:
    @pytest.fixture
    def tree(self, tmp_path):
        return load_tenant_tree(str(_write(tmp_path / "conf.d", {
            "_defaults.yaml": _EMAIL_RD,
            "t-root.yaml": _tenant("t-root"),
            "team/_defaults.yaml": _SLACK_RD + "  group_interval: 7m\n",
            "team/t-team.yaml": _tenant("t-team"),
            "team/sub/_defaults.yaml": ("_routing_defaults:\n  group_wait: 45s\n"
                                        "  receiver:\n    type: pagerduty\n"
                                        "    service_key: k\n"),
            "team/sub/t-deep.yaml": _tenant("t-deep"),
            "other/t-other.yaml": _tenant("t-other"),
        })))

    def test_deeper_level_wins(self, tree):
        rc = tree.routing_configs
        assert rc["t-root"]["receiver"]["type"] == "email"
        assert rc["t-team"]["receiver"]["type"] == "slack"
        # Both subdirectory levels write `receiver`: the deeper one wins.
        assert rc["t-deep"]["receiver"]["type"] == "pagerduty"
        assert rc["t-deep"]["group_interval"] == "7m"         # inherited from team/
        assert rc["t-deep"]["group_wait"] == "45s"            # its own level
        assert rc["t-team"]["group_wait"] == "30s"            # the root's

    def test_merge_is_shallow_per_top_level_key(self, tree):
        # The deeper `receiver` replaces the root's WHOLE: a deep merge would
        # keep the email fields under a slack receiver.
        assert set(tree.routing_configs["t-team"]["receiver"]) == {"type", "api_url"}

    def test_a_sibling_subtree_is_not_on_the_chain(self, tree):
        assert tree.routing_configs["t-other"]["receiver"]["type"] == "email"

    def test_only_the_directory_carrier_is_read(self, tmp_path):
        tree = load_tenant_tree(str(_write(tmp_path / "conf.d", {
            "_defaults.yaml": _EMAIL_RD,
            "team/_platform.yaml": _SLACK_RD,
            "team/t-team.yaml": _tenant("t-team"),
        })))
        assert tree.routing_configs["t-team"]["receiver"]["type"] == "email"
        # Named, not silently dropped (review F3; see the f3 tests below).
        assert any("team/_platform.yaml is not read" in w for w in tree.schema_warnings)


@pytest.mark.parametrize("key", ["receiver", "overrides"])
@pytest.mark.parametrize("mode", [
    pytest.param(["--dry-run"], id="dry-run"),
    pytest.param(["--validate", "--strict"], id="validate-strict"),
    pytest.param(["-o", "{out}"], id="render"),
    pytest.param(["--output-configmap", "-o", "{out}"], id="output-configmap"),
])
def test_null_receiver_or_overrides_below_root_is_refused(tmp_path, key, mode):
    d = _write(tmp_path / "conf.d", {
        "_defaults.yaml": _EMAIL_RD,
        "t-root.yaml": _tenant("t-root"),
        "team/_defaults.yaml": f"_routing_defaults:\n  {key}: null\n",
        "team/t-team.yaml": _tenant("t-team"),
    })
    out = tmp_path / "out.yaml"
    res = _gar("--config-dir", str(d), *[a.replace("{out}", str(out)) for a in mode])
    assert res.returncode == EXIT_CALLER_ERROR, res.stderr
    assert _TREE_ERROR in res.stderr
    assert f"team/_defaults.yaml: _routing_defaults.{key} is null" in res.stderr
    assert not out.exists()
    assert "OK" not in res.stdout and "route:" not in res.stdout


def test_null_group_wait_below_root_is_legal(tmp_path):
    """Control: the four timing fields may be null (ADR-017 null rules)."""
    d = _write(tmp_path / "conf.d", {
        "_defaults.yaml": _EMAIL_RD,
        "team/_defaults.yaml": "_routing_defaults:\n  group_wait: null\n",
        "team/t-team.yaml": _tenant("t-team"),
    })
    res = _gar("--config-dir", str(d), "--validate", "--strict")
    assert res.returncode == EXIT_OK, res.stderr


# ── (b) `_routing_enforced` below the root ──────────────────────────────


@pytest.mark.parametrize("where", ["team/_defaults.yaml", "team/t-team.yaml"])
def test_routing_enforced_below_root_is_refused(tmp_path, where):
    files = {"_defaults.yaml": _EMAIL_RD, "team/t-team.yaml": _tenant("t-team")}
    files[where] = files.get(where, "") + "_routing_enforced:\n  enabled: false\n"
    d = _write(tmp_path / "conf.d", files)
    res = _gar("--config-dir", str(d), "--dry-run")
    assert res.returncode == EXIT_CALLER_ERROR, res.stderr
    assert f"{where}: _routing_enforced is read only at the conf.d root" in res.stderr


def test_routing_enforced_at_the_root_is_still_read(tmp_path):
    d = _write(tmp_path / "conf.d", {
        "_defaults.yaml": _EMAIL_RD + ("_routing_enforced:\n  enabled: true\n"
                                       "  receiver:\n    type: webhook\n"
                                       "    url: https://noc.example.com/a\n"),
        "team/t-team.yaml": _tenant("t-team"),
    })
    res = _gar("--config-dir", str(d), "--dry-run")
    assert res.returncode == EXIT_OK, res.stderr
    assert "Platform enforced routing: ENABLED" in res.stdout


# ── (c) routing profiles ────────────────────────────────────────────────

_PROFILE = ("routing_profiles:\n  {name}:\n    receiver:\n      type: slack\n"
            "      api_url: https://hooks.slack.com/services/T/B/p\n")


def test_profiles_are_visible_to_their_subtree_only(tmp_path):
    tree = load_tenant_tree(str(_write(tmp_path / "conf.d", {
        "_defaults.yaml": _EMAIL_RD,
        "team/_routing_profiles.yaml": _PROFILE.format(name="team-p"),
        "team/sub/t-in.yaml": _tenant("t-in", "    _routing_profile: team-p\n"),
        "other/t-out.yaml": _tenant("t-out", "    _routing_profile: team-p\n"),
    })))
    assert tree.routing_configs["t-in"]["receiver"]["type"] == "slack"
    assert tree.routing_configs["t-out"]["receiver"]["type"] == "email"
    unknown = [w for w in tree.schema_warnings if "t-out: _routing_profile" in w]
    assert unknown and "references unknown profile 'team-p'" in unknown[0]
    assert "team/_routing_profiles.yaml" in unknown[0]   # says where it IS
    assert tree.routing_tree_problems == []


@pytest.mark.parametrize("files, later", [
    pytest.param({"_routing_profiles.yaml": _PROFILE.format(name="p1"),
                  "_routing_profiles.yml": _PROFILE.format(name="p1")},
                 "_routing_profiles.yml", id="root-yaml-and-yml"),
    pytest.param({"_routing_profiles.yaml": _PROFILE.format(name="p1"),
                  "team/_routing_profiles.yaml": _PROFILE.format(name="p1")},
                 "team/_routing_profiles.yaml", id="root-and-subtree"),
    # A directory whose name sorts BEFORE `_`: the root file is still first.
    pytest.param({"_routing_profiles.yaml": _PROFILE.format(name="p1"),
                  "0team/_routing_profiles.yaml": _PROFILE.format(name="p1")},
                 "0team/_routing_profiles.yaml", id="root-first-whatever-the-name"),
])
def test_one_profile_name_in_two_files_is_refused(tmp_path, files, later):
    d = _write(tmp_path / "conf.d", {"_defaults.yaml": _EMAIL_RD,
                                     "t-a.yaml": _tenant("t-a"), **files})
    res = _gar("--config-dir", str(d), "--dry-run")
    assert res.returncode == EXIT_CALLER_ERROR, res.stderr
    assert f"routing profile 'p1' is defined in both" in res.stderr
    assert f"and {later} —" in res.stderr


# ── (d) domain policies below the root ──────────────────────────────────

_ROOT_POLICY = ("domain_policies:\n  ops:\n    tenants: [t-root, t-team]\n"
                "    constraints:\n      allowed_receiver_types: [email, slack]\n")
_TEAM_POLICY = ("domain_policies:\n  fin:\n    tenants: [t-team{extra}]\n"
                "    constraints:\n      forbidden_receiver_types: [slack]\n")


def _policy_tree(tmp_path: Path, extra: str = "") -> Path:
    return _write(tmp_path / "conf.d", {
        "_defaults.yaml": _SLACK_RD,
        "_domain_policy.yaml": _ROOT_POLICY,
        "team/_domain_policy.yaml": _TEAM_POLICY.format(extra=extra),
        "t-root.yaml": _tenant("t-root"),
        "team/t-team.yaml": _tenant("t-team"),
    })


def test_subtree_policy_applies_within_its_subtree(tmp_path):
    res = _gar("--config-dir", str(_policy_tree(tmp_path)), "--validate", "--strict")
    assert res.returncode == EXIT_VIOLATION, res.stderr
    assert "domain_policy 'fin', tenant 't-team': receiver type 'slack' is forbidden" in res.stderr
    assert "t-root': receiver type" not in res.stderr


def test_policies_are_additive(tmp_path):
    """The root allows slack; the subtree forbids it — the tenant must meet
    both, so the subtree tightens; a root policy never loosens it."""
    tree = load_tenant_tree(str(_policy_tree(tmp_path)), strict_policies=True)
    lines = [w for w in tree.schema_warnings if "t-team" in w and "domain_policy" in w]
    assert len(lines) == 1 and "'fin'" in lines[0], lines


@pytest.mark.parametrize("strict, prefix, rc", [
    pytest.param(True, "ERROR:", EXIT_VIOLATION, id="strict"),
    pytest.param(False, "WARN:", EXIT_OK, id="lenient"),
])
def test_subtree_policy_naming_an_outside_tenant(tmp_path, strict, prefix, rc):
    d = _policy_tree(tmp_path, extra=", t-root, t-nowhere")
    # Only the out-of-scope entry is under test: drop the in-scope violation.
    (d / "_defaults.yaml").write_text(_EMAIL_RD, encoding="utf-8")
    res = _gar("--config-dir", str(d), "--validate", *(["--strict"] if strict else []))
    assert res.returncode == rc, res.stderr
    # (--validate prints the line once as a finding and again under FAIL.)
    line = sorted({ln for ln in res.stderr.splitlines()
                   if "outside this policy's subtree" in ln})
    assert len(line) == 1, res.stderr
    assert line[0].lstrip().startswith(prefix)
    assert "names tenant 't-root'" in line[0] and "team/_domain_policy.yaml" in line[0]
    assert "not found" not in line[0]
    # A tenant no file declares is not this finding (the lint names it).
    assert "t-nowhere" not in res.stderr


def test_policy_scope_skips_shapes_check_domain_policies_owns():
    """A policy that is not a mapping, a `tenants:` that is not a list and an
    entry that is not a string are `check_domain_policies`' findings; the
    scope check steps over them and still judges the well-formed entries."""
    from _grar_validate import check_policy_scope
    msgs, rows = check_policy_scope(
        "team",
        {"a-scalar": "oops",
         "b-map-tenants": {"tenants": {"t-root": 1}},
         "c-mixed": {"tenants": [{"t-root": 1}, "t-root", "t-team"]}},
        {"t-root": ".", "t-team": "team"},
        source="team/_domain_policy.yaml")
    assert rows == [("c-mixed", "t-root")]
    assert len(msgs) == 1 and msgs[0].lstrip().startswith("WARN:"), msgs


# ── (e) one tenant id, two files ────────────────────────────────────────


def test_duplicate_tenant_id_is_refused(tmp_path):
    d = _write(tmp_path / "conf.d", {
        "_defaults.yaml": _EMAIL_RD,
        "t-x.yaml": _tenant("t-x"),
        "team/t-x.yaml": _tenant("t-x"),
    })
    out = tmp_path / "out.yaml"
    res = _gar("--config-dir", str(d), "-o", str(out))
    # #2315's refusal (rc 1, validate-config's own scan) runs first and owns
    # this verdict; the routing-tree refusal (rc 2) does not repeat it.
    assert res.returncode == EXIT_VIOLATION, res.stderr
    assert "tenant 't-x' is declared in 2 files: t-x.yaml, team/t-x.yaml" in res.stderr
    assert "routing-tree error" not in res.stderr
    assert not out.exists()


@pytest.mark.parametrize("files, needle", [
    # #2315's refusal: named from `duplicate_tenants`, not a routing-tree kind.
    ({"t-x.yaml": _tenant("t-x"), "team/t-x.yaml": _tenant("t-x")},
     "tenant 't-x' is declared in 2 files: t-x.yaml, team/t-x.yaml"),
    # A #2326 routing-tree kind.
    ({"t-x.yaml": _tenant("t-x"),
      "team/_defaults.yaml": "_routing_enforced:\n  enabled: true\n",
      "team/t-y.yaml": _tenant("t-y")},
     "ERROR (routing tree):"),
], ids=["duplicate-tenant", "routing-tree"])
def test_explain_route_names_a_tree_generate_routes_refuses(tmp_path, files, needle):
    """explain-route is a diagnostic, so it still explains — but a tree the
    generator refuses outright is said so first, whichever check refuses it."""
    d = _write(tmp_path / "conf.d", {"_defaults.yaml": _EMAIL_RD, **files})
    res = subprocess.run(
        [sys.executable, "-s", str(REPO / "scripts" / "tools" / "ops" / "explain_route.py"),
         "--config-dir", str(d), "--tenant", "t-x"],
        capture_output=True, text=True, encoding="utf-8", timeout=60)
    line = [ln for ln in res.stderr.splitlines() if needle in ln]
    assert line and line[0].endswith("generate-routes refuses this tree"), res.stderr


# ── (f) a nested platform file's `tenants:` block ───────────────────────


def test_nested_platform_tenants_block_is_named_not_read(tmp_path):
    d = _write(tmp_path / "conf.d", {
        "_defaults.yaml": _EMAIL_RD,
        "team/_defaults.yaml": "tenants:\n  t-team:\n    _severity_dedup: disable\n",
        "team/t-team.yaml": _tenant("t-team"),
    })
    res = _gar("--config-dir", str(d), "--dry-run")
    assert res.returncode == EXIT_OK, res.stderr
    assert "team/_defaults.yaml: 'tenants:' block ignored" in res.stderr
    tree = load_tenant_tree(str(d))
    assert tree.dedup_configs["t-team"] == "enable"


# ── the other readers of the same tree ──────────────────────────────────


def test_validate_config_schema_row_fails_on_a_tree_error(tmp_path):
    d = _write(tmp_path / "conf.d", {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n" + _EMAIL_RD,
        "team/_defaults.yaml": "_routing_enforced:\n  enabled: true\n",
        "team/t-team.yaml": _tenant("t-team"),
    })
    res = subprocess.run([sys.executable, "-s", str(_VC), "--config-dir", str(d)],
                         capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert res.returncode == EXIT_VIOLATION, (res.stdout, res.stderr)
    assert "ERROR (routing tree): team/_defaults.yaml: _routing_enforced" in res.stdout


def test_routing_profiles_lint_reads_the_tree(tmp_path):
    d = _write(tmp_path / "conf.d", {
        "_defaults.yaml": _EMAIL_RD,
        "team/_routing_profiles.yaml": _PROFILE.format(name="team-p"),
        "team/_domain_policy.yaml": _TEAM_POLICY.format(extra=", t-root, t-nowhere"),
        "team/t-team.yaml": _tenant("t-team", "    _routing_profile: team-p\n"),
        "t-root.yaml": _tenant("t-root", "    _routing_profile: team-p\n"),
    })
    res = subprocess.run([sys.executable, "-s", str(_LINT), "--config-dir", str(d), "--strict"],
                         capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert res.returncode == EXIT_VIOLATION, res.stderr
    err = res.stderr
    assert "tenant 't-root': _routing_profile references unknown profile 'team-p'" in err
    assert "tenant 't-team'" not in err
    assert "tenant 't-root' lives outside this policy's subtree team/" in err
    assert "tenant 't-nowhere' not found in config-dir" in err


# ── review round (#2326 review F1–F4) ───────────────────────────────────


def test_f1_enforced_in_the_unselected_carrier_spelling_is_refused(tmp_path):
    """(b) is about ANY file below the root: the carrier spelling the chain
    does not read is still refused for `_routing_enforced` (Go's LoadTree
    refuses it too). Its other keys stay unread — a `receiver: null` there is
    not the (a) finding."""
    d = _write(tmp_path / "conf.d", {
        "_defaults.yaml": _EMAIL_RD,
        "team/_defaults.yaml": _SLACK_RD,
        "team/_defaults.yml": ("_routing_enforced:\n  enabled: true\n"
                               "_routing_defaults:\n  receiver: null\n"),
        "team/t-team.yaml": _tenant("t-team"),
    })
    res = _gar("--config-dir", str(d), "--validate", "--strict")
    assert res.returncode == EXIT_CALLER_ERROR, res.stderr
    assert "team/_defaults.yml: _routing_enforced is read only at the conf.d root" in res.stderr
    assert "_routing_defaults.receiver is null" not in res.stderr


def test_f1_a_clean_unselected_spelling_is_not_refused(tmp_path):
    """Control: the unselected spelling alone is only the multi-carrier WARN."""
    d = _write(tmp_path / "conf.d", {
        "_defaults.yaml": _EMAIL_RD,
        "team/_defaults.yaml": _SLACK_RD,
        "team/_defaults.yml": "_routing_defaults:\n  receiver: null\n",
        "team/t-team.yaml": _tenant("t-team"),
    })
    res = _gar("--config-dir", str(d), "--validate", "--strict")
    assert res.returncode == EXIT_OK, res.stderr


def test_f2_a_null_body_still_declares_the_tenant(tmp_path):
    """A declaration is the key, whatever its value (the exporter walker's
    rule): `t-x:` with no body beside a real `t-x` is the duplicate the
    exporter refuses."""
    d = _write(tmp_path / "conf.d", {
        "_defaults.yaml": _EMAIL_RD,
        "a/t-x.yaml": "tenants:\n  t-x:\n",
        "b/t-x.yaml": _tenant("t-x"),
    })
    res = _gar("--config-dir", str(d), "--dry-run")
    assert res.returncode == EXIT_VIOLATION, res.stderr
    assert "tenant 't-x' is declared in 2 files: a/t-x.yaml, b/t-x.yaml" in res.stderr
    tree = load_tenant_tree(str(d))
    assert tree.duplicate_tenants == {"t-x": ["a/t-x.yaml", "b/t-x.yaml"]}
    # The routing plane's own record agrees (parity with Go LoadTree).
    assert [p[:3] for p in tree.routing_tree_problems] == [
        ("duplicate_tenant", "b/t-x.yaml", "tenants.t-x")]


def test_f3_routing_defaults_in_a_nested_non_carrier_file(tmp_path):
    """At the root any `_` file carries `_routing_defaults`; below it only
    the defaults carrier does. The moved file is named, blocking under
    --validate (da-guard: routing_in_unread_location, error)."""
    d = _write(tmp_path / "conf.d", {
        "_defaults.yaml": _EMAIL_RD,
        "team/_routing.yaml": "_routing_defaults:\n  repeat_interval: 1h\n",
        "team/t-team.yaml": _tenant("t-team"),
    })
    res = _gar("--config-dir", str(d), "--validate")
    assert res.returncode == EXIT_VIOLATION, res.stderr
    assert "_routing_defaults in team/_routing.yaml is not read" in res.stderr
    tree = load_tenant_tree(str(d))
    assert [p[:3] for p in tree.routing_tree_problems] == [
        ("routing_in_unread_location", "team/_routing.yaml", "_routing_defaults")]
    assert "repeat_interval" not in tree.routing_configs["t-team"]


def test_f3_a_nested_tenant_file_is_not_this_finding(tmp_path):
    """Control: a TENANT file never carries `_routing_defaults`, at the root
    either — a stderr WARN there as here, not the moved-file finding."""
    d = _write(tmp_path / "conf.d", {
        "_defaults.yaml": _EMAIL_RD,
        "team/t-team.yaml": _tenant("t-team") + "_routing_defaults:\n  repeat_interval: 1h\n",
    })
    res = _gar("--config-dir", str(d), "--validate")
    assert res.returncode == EXIT_OK, res.stderr


@pytest.mark.parametrize("policy_file", ["_domain_policy.yaml", "team/_domain_policy.yaml"])
@pytest.mark.parametrize("strict, rc", [(True, EXIT_VIOLATION), (False, EXIT_OK)])
def test_f4_a_mapping_in_a_policy_tenants_list_does_not_crash(tmp_path, policy_file,
                                                              strict, rc):
    d = _write(tmp_path / "conf.d", {
        "_defaults.yaml": _EMAIL_RD,
        policy_file: ("domain_policies:\n  fin:\n    tenants: [t-team, {x: 1}]\n"
                      "    constraints:\n      forbidden_receiver_types: [slack]\n"),
        "team/t-team.yaml": _tenant("t-team"),
    })
    res = _gar("--config-dir", str(d), "--validate", *(["--strict"] if strict else []))
    assert "Traceback" not in res.stderr, res.stderr
    assert res.returncode == rc, res.stderr
    if strict:
        assert "'tenants' entry must be a tenant id, got dict" in res.stderr


def test_an_unparseable_unselected_carrier_spelling_is_not_refused(tmp_path):
    """F1's reader carries nothing from a file that does not parse — Go's
    LoadTree skips it the same way — so only the multi-carrier WARN."""
    d = _write(tmp_path / "conf.d", {
        "_defaults.yaml": _EMAIL_RD,
        "team/_defaults.yaml": _SLACK_RD,
        "team/_defaults.yml": "a: [1, 2\n",
        "team/t-team.yaml": _tenant("t-team"),
    })
    res = _gar("--config-dir", str(d), "--validate", "--strict")
    assert res.returncode == EXIT_OK, res.stderr
    assert "has 2 defaults carriers" in res.stderr


def test_a_non_mapping_level_contributes_nothing(tmp_path):
    """A subdirectory `_routing_defaults` that is not a mapping is named and
    skipped: the tenant inherits the levels above it unchanged. #2341: the
    line is blocking (`WARN … skipping`), so `--validate` fails on it."""
    d = _write(tmp_path / "conf.d", {
        "_defaults.yaml": _EMAIL_RD,
        "team/_defaults.yaml": "_routing_defaults: [a, b]\n",
        "team/t-team.yaml": _tenant("t-team"),
    })
    res = _gar("--config-dir", str(d), "--dry-run")
    assert res.returncode == EXIT_OK, res.stderr
    assert ("_routing_defaults in team/_defaults.yaml must be a mapping, got list"
            in res.stderr)
    assert _gar("--config-dir", str(d), "--validate").returncode == EXIT_VIOLATION
    tree = load_tenant_tree(str(d))
    assert tree.routing_configs["t-team"]["receiver"]["type"] == "email"


_ROOT_NOT_A_MAPPING = [("[a, b]", "list"), ("not-a-mapping", "string"),
                       ("42", "int"), ("false", "boolean")]


@pytest.mark.parametrize("value, kind", _ROOT_NOT_A_MAPPING,
                         ids=[k for _v, k in _ROOT_NOT_A_MAPPING])
def test_a_non_mapping_root_contributes_nothing(tmp_path, value, kind):
    """#2412: the ROOT gets the subdirectory verdict above — named, no layer
    — where it used to die in `resolve_routing_defaults`' `dict(root)` with a
    traceback (generate-routes `--validate --strict` and explain-route at rc
    1; validate-config's routing rows too). #2341: the line is blocking, so
    `--validate --strict` and validate-config fail on it (rc 1, no traceback).
    Go's `routingDefaultsFromNode` agrees (parity matrix
    `hier-a-root-routing-defaults-not-a-mapping`); a level below still
    contributes its own."""
    d = _write(tmp_path / "conf.d", {
        "_defaults.yaml": f"_routing_defaults: {value}\n",
        "t-root.yaml": _tenant("t-root"),
        "team/_defaults.yaml": _SLACK_RD,
        "team/t-team.yaml": _tenant("t-team"),
    })
    warn = (f"WARN: _routing_defaults in _defaults.yaml must be a mapping, "
            f"got {kind} ")
    res = _gar("--config-dir", str(d), "--validate", "--strict")
    assert res.returncode == EXIT_VIOLATION, res.stderr
    assert "Traceback" not in res.stderr, res.stderr
    assert warn in res.stderr
    for tenant in ("t-root", "t-team"):
        exp = subprocess.run(
            [sys.executable, "-s", str(REPO / "scripts" / "tools" / "ops" / "explain_route.py"),
             "--config-dir", str(d), "--tenant", tenant],
            capture_output=True, text=True, encoding="utf-8", timeout=60)
        assert exp.returncode == EXIT_OK, (tenant, exp.stderr)
        assert warn in exp.stderr, (tenant, exp.stderr)
    vc = subprocess.run([sys.executable, "-s", str(_VC), "--config-dir", str(d)],
                        capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert vc.returncode == EXIT_VIOLATION, (vc.stdout, vc.stderr)
    assert "Traceback" not in vc.stdout + vc.stderr, (vc.stdout, vc.stderr)
    tree = load_tenant_tree(str(d))
    assert "t-root" not in tree.routing_configs
    assert tree.routing_configs["t-team"]["receiver"]["type"] == "slack"


def test_a_null_root_stays_silent(tmp_path):
    """#2412 control: `_routing_defaults: null` at the root was already an
    empty layer without a word, and still is — no WARN, rc 0."""
    d = _write(tmp_path / "conf.d", {
        "_defaults.yaml": "_routing_defaults: null\n",
        "t-root.yaml": _tenant("t-root"),
    })
    res = _gar("--config-dir", str(d), "--validate", "--strict")
    assert res.returncode == EXIT_OK, res.stderr
    assert "_routing_defaults" not in res.stderr, res.stderr
    assert "t-root" not in load_tenant_tree(str(d)).routing_configs


@pytest.mark.parametrize("first, second, want", [
    (_EMAIL_RD, "_routing_defaults: 42\n", None),
    ("_routing_defaults: [a, b]\n", _EMAIL_RD, "email"),
], ids=["later-not-a-mapping", "earlier-not-a-mapping"])
def test_a_later_root_file_replaces_the_root_defaults_whole(tmp_path, first,
                                                            second, want):
    """#2412: among root `_` files (name order) the later
    `_routing_defaults` replaces the earlier WHOLE, even when it is not a
    mapping — Go's "defaults then become nil" (parity matrix
    `hier-a-root-later-file-*` / `-earlier-file-*`). #2341: the non-mapping
    one is still named, and blocking."""
    d = _write(tmp_path / "conf.d", {
        "_defaults.yaml": first,
        "_platform.yaml": second,
        "t-root.yaml": _tenant("t-root"),
    })
    res = _gar("--config-dir", str(d), "--validate", "--strict")
    assert res.returncode == EXIT_VIOLATION, res.stderr
    rc = load_tenant_tree(str(d)).routing_configs.get("t-root")
    assert (rc["receiver"]["type"] if rc else None) == want, rc


def test_routes_in_a_nested_level_are_stripped_and_block_validate(tmp_path):
    """#2245 as at the root: `routes` never belong to the defaults — named,
    dropped from the level, and blocking under --validate."""
    d = _write(tmp_path / "conf.d", {
        "_defaults.yaml": _EMAIL_RD,
        "team/_defaults.yaml": _SLACK_RD + "  routes:\n  - match: {a: b}\n",
        "team/t-team.yaml": _tenant("t-team"),
    })
    msg = "_routing_defaults in team/_defaults.yaml: 'routes' is not supported here"
    tree = load_tenant_tree(str(d))
    assert "routes" not in tree.routing_configs["t-team"]
    assert tree.routing_configs["t-team"]["receiver"]["type"] == "slack"
    res = _gar("--config-dir", str(d), "--validate", "--strict")
    assert res.returncode == EXIT_VIOLATION, res.stderr
    assert msg in res.stderr
