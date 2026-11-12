"""`da-tools migrate-credentials`（#1560 ⑤-a）與它讀 receiver 的走訪器。

工具把租戶明文寫的憑證改寫成 Secret 參照（計畫／`--write`）、寫出每個租戶的
Secret（`--manifests-out`，0600）、列出待輪替清單；產生器以同一判定拒收明文（⑤-b）。
⛔ 憑證值只能進清單檔：每個情境都
放一個 canary 值，斷言它不在 stdout／stderr／JSON 報告裡。
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys

import pytest
import yaml

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOL = os.path.join(REPO, "scripts", "tools", "ops", "migrate_credentials.py")
sys.path.insert(0, os.path.join(REPO, "scripts", "tools", "ops"))
sys.path.insert(0, os.path.join(REPO, "scripts", "tools"))

from _grar_parse import raw_receivers  # noqa: E402

sys.path.insert(0, os.path.join(REPO, "tests"))
from _platform_fs import symlink_or_skip  # noqa: E402

CANARY = "cnry-7f1e-do-not-print"
SLACK = f"https://hooks.slack.example.com/services/T0/B0/{CANARY}"

TENANT = f"""\
# team-a's own file
tenants:
  team-a:
    cpu: "80"   # threshold
    _routing:
      # the main receiver
      receiver:
        type: slack
        api_url: "{SLACK}"   # the hook
        channel: '#alerts'
"""

pytestmark = pytest.mark.usefixtures("da_guard_env")


def _tree(tmp_path, files: dict[str, str]):
    root = tmp_path / "conf.d"
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


def _run(*args, cwd=None):
    return subprocess.run([sys.executable, TOOL, *map(str, args)], capture_output=True,
                          text=True, encoding="utf-8", check=False, timeout=300,
                          cwd=cwd or "/")


def _no_canary(proc):
    assert CANARY not in proc.stdout, "a credential value reached stdout"
    assert CANARY not in proc.stderr, "a credential value reached stderr"


def _snapshot(root):
    return {p: p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


# ── the walker ──────────────────────────────────────────────────────────

def test_walker_lists_every_place_with_its_owner(tmp_path):
    hook = {"type": "webhook", "url": "https://h.example.com/x"}
    rv = yaml.safe_dump
    root = _tree(tmp_path, {
        "_defaults.yaml": rv({"defaults": {}, "_routing_defaults": {
            "receiver": hook, "overrides": [{"alertname": "A", "receiver": hook}]},
            "_routing_enforced": {"enabled": True, "receiver": hook}}),
        "_routing_profiles.yaml": rv({"routing_profiles": {"p1": {
            "receiver": hook, "routes": [{"match": {"a": "b"}, "receiver": hook}]}}}),
        "team-a.yaml": rv({"tenants": {"team-a": {"_routing": {
            "receiver": hook,
            "overrides": [{"alertname": "A", "receiver": hook}],
            "routes": [{"match": {"a": "b"}, "receiver": hook}]},
            "_routing_enforced": {"receiver": hook}}}}),
        "sub/_defaults.yaml": rv({"_routing_defaults": {"receiver": hook}}),
        "sub/team-b.yaml": rv({"tenants": {"team-b": {"_routing": {"receiver": hook}}}}),
    })
    got = {(r.file, r.path, r.tenant, r.place) for r in raw_receivers(str(root)).receivers}
    assert got == {
        ("_defaults.yaml", ("_routing_defaults", "receiver"), None, "routing_defaults"),
        ("_defaults.yaml", ("_routing_defaults", "overrides", 0, "receiver"), None,
         "routing_defaults"),
        ("_defaults.yaml", ("_routing_enforced", "receiver"), None, "routing_enforced"),
        ("_routing_profiles.yaml", ("routing_profiles", "p1", "receiver"), None,
         "routing_profile"),
        ("_routing_profiles.yaml", ("routing_profiles", "p1", "routes", 0, "receiver"), None,
         "routing_profile"),
        ("team-a.yaml", ("tenants", "team-a", "_routing", "receiver"), "team-a",
         "tenant_routing"),
        ("team-a.yaml", ("tenants", "team-a", "_routing", "overrides", 0, "receiver"),
         "team-a", "tenant_routing"),
        ("team-a.yaml", ("tenants", "team-a", "_routing", "routes", 0, "receiver"), "team-a",
         "tenant_routing"),
        ("team-a.yaml", ("tenants", "team-a", "_routing_enforced", "receiver"), "team-a",
         "tenant_enforced"),
        ("sub/_defaults.yaml", ("_routing_defaults", "receiver"), None, "routing_defaults"),
        ("sub/team-b.yaml", ("tenants", "team-b", "_routing", "receiver"), "team-b",
         "tenant_routing"),
    }
    one = next(r for r in raw_receivers(str(root)).receivers if r.tenant == "team-b")
    assert one.receiver == hook        # the value as written


def test_walker_names_a_file_it_cannot_read(tmp_path, judging_run):
    root = _tree(tmp_path, {"team-a.yaml": TENANT, "bad.yaml": "tenants: [\n"})
    found = raw_receivers(str(root))
    assert [f for f, _why in found.unreadable] == ["bad.yaml"]
    assert [r.tenant for r in found.receivers] == ["team-a"]


def test_a_file_that_cannot_be_read_stops_the_run(tmp_path):
    """The reader's shared refusal (a path da-guard validation names): rc 2,
    nothing judged — "no credential written out" cannot be said of it."""
    root = _tree(tmp_path, {"team-a.yaml": TENANT, "bad.yaml": "tenants: [\n"})
    proc = _run("--config-dir", root)
    assert proc.returncode == 2, proc.stderr
    assert "bad.yaml" in proc.stderr
    _no_canary(proc)


# ── the subcommand ──────────────────────────────────────────────────────

def test_the_object_name_is_the_generators():
    import migrate_credentials
    from _grar_merge import own_secret_name
    assert migrate_credentials._object_name("team-a") == own_secret_name("team-a") == "da-team-a"


def test_clean_tree_is_rc0(tmp_path):
    root = _tree(tmp_path, {"team-a.yaml": TENANT.replace(
        f'"{SLACK}"', "{secret: {key: receiver.api_url}}")})
    proc = _run("--config-dir", root)
    assert proc.returncode == 0, proc.stderr
    assert "No credential is written out." in proc.stdout


def test_default_mode_prints_the_plan_and_changes_nothing(tmp_path):
    root = _tree(tmp_path, {"team-a.yaml": TENANT})
    before = _snapshot(root)
    proc = _run("--config-dir", root)
    assert proc.returncode == 1, proc.stderr
    _no_canary(proc)
    assert _snapshot(root) == before
    assert ("team-a.yaml:9 tenants.team-a._routing.receiver.api_url [team-a] -> "
            "{secret: {key: receiver.api_url}}") in proc.stdout
    assert "(not applied: add --write)" in proc.stdout
    doc = json.loads(_run("--config-dir", root, "--json").stdout)
    assert [(r["location"], r["ref_key"]) for r in doc["plan"]] == [
        ("tenants.team-a._routing.receiver.api_url", "receiver.api_url")]
    assert "diff" not in doc


def test_write_rewrites_keeps_comments_and_writes_a_0600_manifest(tmp_path):
    root = _tree(tmp_path, {"team-a.yaml": TENANT})
    out = tmp_path / "secrets"
    proc = _run("--config-dir", root, "--manifests-out", out, "--write")
    assert proc.returncode == 1, proc.stderr       # found: the rotation list stands
    _no_canary(proc)
    text = (root / "team-a.yaml").read_text(encoding="utf-8")
    assert CANARY not in text
    for comment in ("# team-a's own file", "# threshold", "# the main receiver"):
        assert comment in text
    assert yaml.safe_load(text)["tenants"]["team-a"]["_routing"]["receiver"]["api_url"] == {
        "secret": {"key": "receiver.api_url"}}
    manifest = out / "da-team-a.yaml"
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600
    doc = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    assert doc == {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "da-team-a"},
                   "type": "Opaque", "stringData": {"receiver.api_url": SLACK}}
    # The canary is in exactly one place now: the manifest.
    holders = [p for p in tmp_path.rglob("*") if p.is_file()
               and CANARY in p.read_text(encoding="utf-8", errors="replace")]
    assert holders == [manifest]
    again = _run("--config-dir", root)
    assert again.returncode == 0, again.stdout


def test_manifest_is_created_0600_under_a_loose_umask(tmp_path):
    root = _tree(tmp_path, {"team-a.yaml": TENANT})
    out = tmp_path / "secrets"
    proc = subprocess.run(
        ["sh", "-c", f'umask 000 && exec "{sys.executable}" "{TOOL}" --config-dir "{root}" '
                     f'--manifests-out "{out}" --write'],
        capture_output=True, text=True, encoding="utf-8", check=False, timeout=300)
    assert proc.returncode == 1, proc.stderr
    assert stat.S_IMODE((out / "da-team-a.yaml").stat().st_mode) == 0o600


def test_write_needs_manifests_out(tmp_path):
    root = _tree(tmp_path, {"team-a.yaml": TENANT})
    before = _snapshot(root)
    proc = _run("--config-dir", root, "--write")
    assert proc.returncode == 2
    assert "--write needs --manifests-out" in proc.stderr
    assert _snapshot(root) == before


def test_manifests_out_inside_a_git_worktree_is_refused(tmp_path):
    root = _tree(tmp_path, {"team-a.yaml": TENANT})
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    out = repo / "deep" / "secrets"
    before = _snapshot(root)
    proc = _run("--config-dir", root, "--manifests-out", out, "--write")
    assert proc.returncode == 2
    assert "inside the git worktree" in proc.stderr
    assert not out.exists() and not (repo / "deep").exists()
    assert _snapshot(root) == before
    _no_canary(proc)


@pytest.mark.parametrize("where", ["conf", "inside"])
def test_manifests_out_in_the_confd_is_refused(tmp_path, where):
    root = _tree(tmp_path, {"team-a.yaml": TENANT})
    out = root if where == "conf" else root / "secrets"
    proc = _run("--config-dir", root, "--manifests-out", out)
    assert proc.returncode == 2
    assert "is the conf.d or inside it" in proc.stderr
    assert not (root / "secrets").exists()


def test_a_manifest_that_is_a_confd_file_is_refused(tmp_path):
    """#3615 的判定（realpath＋samefile）：清單檔是 conf.d 檔的 hard link → 拒絕。"""
    root = _tree(tmp_path, {"team-a.yaml": TENANT})
    out = tmp_path / "secrets"
    out.mkdir()
    os.link(root / "team-a.yaml", out / "da-team-a.yaml")
    before = _snapshot(root)
    proc = _run("--config-dir", root, "--manifests-out", out, "--write")
    assert proc.returncode == 2
    assert "a file with another link" in proc.stderr
    assert _snapshot(root) == before


MIXED_PLATFORM = f"""\
_routing_defaults:
  receiver:
    type: pagerduty
    routing_key: "{CANARY}-a"
_routing_enforced:
  enabled: true
  receiver:
    type: webhook
    url: "https://noc:{CANARY}-b@noc.example.com/hook"
    http_config:
      bearer_token: "{CANARY}-c"
"""

MIXED_TENANT = f"""\
tenants:
  team-b:
    _routing:
      receiver:
        type: webhook
        url: "https://hook.example.com/alerts"
        http_config:
          bearer_token: "{CANARY}-d"
          proxy_url: "http://u:{CANARY}-e@proxy.example.com:3128"
          proxy_connect_header:
            Proxy-Authorization: ["{CANARY}-f"]
          http_headers:
            "X Token":
              secrets: ["{CANARY}-g"]
      overrides:
        - alertname: Mail
          receiver:
            type: email
            to: [ops@example.com]
            from: am@example.com
            smarthost: smtp.example.com:587
            auth_password: "{CANARY}-h"
        - alertname: Hook
          receiver:
            type: webhook
            url: "https://ops:{CANARY}-i@hook.example.com/x"
    _routing_enforced:
      receiver:
        type: slack
        api_url: "{SLACK}"
"""


def test_json_report_sorts_what_is_rewritten_from_what_is_left(tmp_path):
    root = _tree(tmp_path, {"_defaults.yaml": MIXED_PLATFORM, "team-b.yaml": MIXED_TENANT})
    proc = _run("--config-dir", root, "--json")
    assert proc.returncode == 1, proc.stderr
    _no_canary(proc)
    doc = json.loads(proc.stdout)
    assert doc["schema"] == "da-tools.migrate-credentials/v1"
    assert doc["manifests"] == [{"name": "da-team-b", "tenant": "team-b", "path": None,
                                 "entries": [
                                     "overrides.0.receiver.auth_password",
                                     "receiver.http_config.bearer_token"],
                                 "written": False}]
    decisions = {(d["location"], d["reason"]) for d in doc["decisions"]}
    assert decisions == {
        ("_routing_defaults.receiver.routing_key", "platform_place"),
        ("_routing_enforced.receiver.url", "userinfo_platform"),
        ("_routing_enforced.receiver.http_config.bearer_token", "platform_enforced"),
        ("tenants.team-b._routing.receiver.http_config.proxy_url", "proxy_credential"),
        ("tenants.team-b._routing.receiver.http_config.proxy_connect_header",
         "proxy_credential"),
        # a header name outside the HTTP-token charset is not printed
        ('tenants.team-b._routing.receiver.http_config.http_headers.<key>.secrets[0]',
         "key_invalid"),
        ("tenants.team-b._routing.overrides[1].receiver.url", "userinfo"),
        ("tenants.team-b._routing_enforced.receiver.api_url", "unread_place"),
    }
    assert len(doc["rotate"]) == 10
    assert all(set(r) == {"file", "line", "line_is_field", "field", "location", "tenant",
                          "category"} for r in doc["rotate"])
    by_loc = {r["location"]: r for r in doc["rotate"]}
    assert by_loc["_routing_defaults.receiver.routing_key"]["tenant"] is None
    assert by_loc["_routing_defaults.receiver.routing_key"]["line"] == 4
    hook = by_loc["tenants.team-b._routing.receiver.http_config.bearer_token"]
    assert (hook["line"], hook["line_is_field"]) == (8, True)
    mail = by_loc["tenants.team-b._routing.overrides[0].receiver.auth_password"]
    assert (mail["line"], mail["line_is_field"]) == (15, False)   # `overrides:`
    # The rewrite leaves references the generator still refuses: not done.
    blocked = {(b["kind"], b["field"]) for b in doc["blocked_after_rewrite"]}
    assert blocked == {
        ("secret_ref_follow_redirects", "receiver.http_config.follow_redirects"),
        ("secret_ref_destination_unconfined", "receiver.http_config.bearer_token"),
        ("secret_ref_proxy_url", "receiver.http_config.proxy_url")}
    assert any("not complete" in n for n in doc["notes"])
    assert any("is a list written whole" in n for n in doc["notes"])


def test_a_taken_key_is_left_for_a_person(tmp_path):
    taken = TENANT.replace("        channel: '#alerts'\n", "        channel: '#alerts'\n"
                           "      overrides:\n"
                           "        - alertname: A\n"
                           "          receiver:\n"
                           "            type: slack\n"
                           "            api_url: {secret: {key: receiver.api_url}}\n")
    root = _tree(tmp_path, {"team-a.yaml": taken})
    proc = _run("--config-dir", root, "--json")
    assert proc.returncode == 1, proc.stderr
    doc = json.loads(proc.stdout)
    assert [(d["location"], d["reason"]) for d in doc["decisions"]] == [
        ("tenants.team-a._routing.receiver.api_url", "key_taken")]
    assert doc["plan"] == [] and doc["manifests"] == []


MAIL = f"""\
tenants:
  team-c:
    _routing:
      receiver:
        type: email
        to: [ops@example.com]
        from: am@example.com
        smarthost: smtp.example.com:587
        auth_password: "{CANARY}"
"""


@pytest.mark.parametrize("allowed,blocked", [
    (None, {("secret_ref_destination_unconfined", "receiver.auth_password")}),
    ("['*.example.com']", set()),
    ("['other.example.org']", {("secret_ref_destination_unconfined", "receiver.auth_password")}),
])
def test_the_binding_after_the_rewrite_is_judged_under_the_policy(tmp_path, allowed, blocked):
    """auth_password 改成參照後，要 policy 的 allowed_domains 涵蓋 smarthost 才綁得上。"""
    root = _tree(tmp_path, {"team-c.yaml": MAIL})
    argv = ["--config-dir", root, "--json"]
    if allowed is not None:
        policy = tmp_path / "policy.yaml"
        policy.write_text(f"allowed_domains: {allowed}\n", encoding="utf-8")
        argv += ["--policy", policy]
    proc = _run(*argv)
    assert proc.returncode == 1, proc.stderr
    _no_canary(proc)
    doc = json.loads(proc.stdout)
    assert {(b["kind"], b["field"]) for b in doc["blocked_after_rewrite"]} == blocked
    assert any("not complete" in n for n in doc["notes"]) == bool(blocked)


@pytest.mark.parametrize("argv", [["--bogus"], []])
def test_usage_errors_are_rc2(argv):
    assert _run(*argv).returncode == 2


def test_missing_config_dir_is_rc2(tmp_path):
    proc = _run("--config-dir", tmp_path / "nope")
    assert proc.returncode == 2
    assert "ERROR" in proc.stderr


# ── fix round (PR #3672 review) ─────────────────────────────────────────

import migrate_credentials as mc  # noqa: E402


def _all_outputs(root, *extra):
    """Run both output modes (text and --json) and return the procs."""
    return [_run("--config-dir", root, *extra), _run("--config-dir", root, "--json", *extra)]


R1_SUBSTRING = f"""\
tenants:
  team-e:
    _routing:
      receiver:
        type: email
        to: [ops@example.com]
        from: "{CANARY}-hdr@example.com"
        auth_password: "pw-{CANARY[:4]}-moved"
        headers:
          X-Token: "{CANARY}-hdr"
        smarthost: smtp.example.com:587
"""

R1_COMMENT = f"""\
tenants:
  team-w:
    _routing:
      receiver:
        type: webhook
        # it used to be https://hook.example.com/{CANARY}-u
        url: "https://hook.example.com/{CANARY}-u"
        http_config:
          bearer_token: "tok-moved"
"""

R1_FLOW_MERGE = f"""\
_base: &base {{type: slack, api_url: "https://hooks.example.com/{CANARY}-m"}}
tenants:
  team-m:
    _routing:
      receiver: {{<<: *base, channel: "#m"}}
"""


@pytest.mark.parametrize("text", [R1_SUBSTRING, R1_COMMENT, R1_FLOW_MERGE],
                         ids=["value-inside-another-field", "value-in-a-comment",
                              "flow-merge-key"])
def test_no_value_reaches_any_output_whatever_the_file_says_around_it(tmp_path, text):
    root = _tree(tmp_path, {"t.yaml": text})
    for proc in _all_outputs(root):
        assert proc.returncode == 1, proc.stderr
        _no_canary(proc)


E9 = f"""\
_base: &base
  type: slack
  api_url: "https://hooks.example.com/services/{CANARY}-e9"
tenants:
  team-a:
    _routing:
      receiver:
        <<: *base
        channel: "#a"
"""


def test_e9_the_place_is_rewritten_and_its_merge_source_is_not_promised(tmp_path):
    """The promise is the credential place: with `<<: *base`, the rewrite
    sets the receiver's own api_url, which overrides the merged one — read
    back, the place is a reference. The anchor `_base` still holds the old
    value: no receiver place, so the tool does not see it again; rotation
    is what makes it dead (cli-reference)."""
    root = _tree(tmp_path, {"team-a.yaml": E9})
    out = tmp_path / "secrets"
    proc = _run("--config-dir", root, "--manifests-out", out, "--write", "--json")
    assert proc.returncode == 1, proc.stderr
    _no_canary(proc)
    doc = json.loads(proc.stdout)
    assert doc["written"] is True and doc["decisions"] == []
    text = (root / "team-a.yaml").read_text(encoding="utf-8")
    assert yaml.safe_load(text)["tenants"]["team-a"]["_routing"]["receiver"]["api_url"] == {
        "secret": {"key": "receiver.api_url"}}
    assert CANARY in text                              # the anchor's copy: not promised
    again = _run("--config-dir", root)
    assert again.returncode == 0 and "No credential is written out." in again.stdout


@pytest.fixture
def worktree(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    return repo


def test_r3_a_symlinked_manifest_into_a_worktree_is_refused(tmp_path, worktree):
    root = _tree(tmp_path, {"team-a.yaml": TENANT})
    out = tmp_path / "secrets"
    out.mkdir()
    target = worktree / "leak.yaml"
    target.write_text("keep\n", encoding="utf-8")
    symlink_or_skip(target, out / "da-team-a.yaml")
    before = _snapshot(root)
    proc = _run("--config-dir", root, "--manifests-out", out, "--write")
    assert proc.returncode == 2
    assert "inside the git worktree" in proc.stderr
    assert target.read_text(encoding="utf-8") == "keep\n"
    assert _snapshot(root) == before
    _no_canary(proc)


def test_r3_a_symlinked_manifest_elsewhere_is_refused(tmp_path):
    root = _tree(tmp_path, {"team-a.yaml": TENANT})
    out = tmp_path / "secrets"
    out.mkdir()
    target = tmp_path / "elsewhere.yaml"
    target.write_text("keep\n", encoding="utf-8")
    symlink_or_skip(target, out / "da-team-a.yaml")
    proc = _run("--config-dir", root, "--manifests-out", out)
    assert proc.returncode == 2
    assert "symlink" in proc.stderr
    assert target.read_text(encoding="utf-8") == "keep\n"


def test_r3_a_hardlinked_manifest_is_refused(tmp_path):
    root = _tree(tmp_path, {"team-a.yaml": TENANT})
    out = tmp_path / "secrets"
    out.mkdir()
    other = tmp_path / "other.txt"
    other.write_text("keep\n", encoding="utf-8")
    os.link(other, out / "da-team-a.yaml")
    proc = _run("--config-dir", root, "--manifests-out", out)
    assert proc.returncode == 2
    assert "another link" in proc.stderr
    assert other.read_text(encoding="utf-8") == "keep\n"


def test_r3_the_manifest_path_is_a_direct_child_named_after_a_valid_tenant(tmp_path, capsys):
    assert mc._manifest_path(str(tmp_path), "team-a") == str(tmp_path / "da-team-a.yaml")
    for bad in ("../x", "a/b", "..", "A_B"):
        with pytest.raises(SystemExit) as e:
            mc._manifest_path(str(tmp_path), bad)
        assert e.value.code == 2
    assert not list(tmp_path.iterdir())


def test_r3_r4_a_tenant_id_with_dotdot_writes_nothing(tmp_path):
    root = _tree(tmp_path, {"t.yaml": 'tenants:\n  "../x":\n    _routing:\n      receiver:\n'
                                      f'        type: slack\n        api_url: "{SLACK}"\n'})
    out = tmp_path / "secrets"
    proc = _run("--config-dir", root, "--manifests-out", out, "--write", "--json")
    assert proc.returncode == 1, proc.stderr
    _no_canary(proc)
    doc = json.loads(proc.stdout)
    assert [d["reason"] for d in doc["decisions"]] == ["tenant_id_invalid"]
    assert doc["manifests"] == [] and not out.exists() and not (tmp_path / "x").exists()


def test_r4_r9_still_refused_rows(monkeypatch, tmp_path):
    class W(str):
        def __new__(cls, kind, tenant, field):
            o = str.__new__(cls, "line quoting " + CANARY)
            o.kind, o.tenant, o.field = kind, tenant, field
            return o
    results = iter([({"a", "b"}, []), ({"a"}, [
        W("secret_ref_proxy_url", "a", "overrides[3].receiver.http_config.proxy_url"),
        W(f"secret_ref_{CANARY}", "a", "receiver.api_url"),
        W("secret_ref_follow_redirects", "Bad Tenant", f"receiver.{CANARY}"),
        W("secret_ref_no_owner", "a",
          "_routing_enforced.receiver.http_config.http_headers.X-Tok.secrets[0]"),
    ])])
    monkeypatch.setattr(mc, "_judge_tree", lambda root, policy: next(results))
    root = _tree(tmp_path, {"a.yaml": "tenants:\n  a: {}\n"})
    rows = mc._still_refused(str(root), {}, None)
    assert CANARY not in repr(rows)
    assert rows == [
        {"kind": "tenant_dropped", "tenant": "b", "field": None},
        {"kind": "secret_ref_proxy_url", "tenant": "a",
         "field": "overrides[i].receiver.http_config.proxy_url"},
        {"kind": "other", "tenant": "a", "field": "receiver.api_url"},
        {"kind": "secret_ref_follow_redirects", "tenant": None, "field": "other"},
        {"kind": "secret_ref_no_owner", "tenant": "a",
         "field": "_routing_enforced.receiver.http_config.http_headers.*.secrets[i]"},
    ]


MAIL_BLOCKED = f"""\
tenants:
  team-c:
    _routing:
      receiver:
        type: email
        to: [ops@example.com]
        from: am@example.com
        smarthost: smtp.example.com:587
        auth_password: "{CANARY}"
"""


def test_r5_write_writes_nothing_while_the_rewrite_is_blocked(tmp_path):
    root = _tree(tmp_path, {"t.yaml": MAIL_BLOCKED})
    out = tmp_path / "secrets"
    before = _snapshot(root)
    proc = _run("--config-dir", root, "--manifests-out", out, "--write", "--json")
    assert proc.returncode == 1, proc.stderr
    _no_canary(proc)
    doc = json.loads(proc.stdout)
    assert doc["written"] is False and doc["blocked_after_rewrite"]
    assert doc["manifests"] and not any(m["written"] for m in doc["manifests"])
    assert any("--write wrote nothing" in n for n in doc["notes"])
    assert _snapshot(root) == before and not out.exists()


def test_r6_a_file_no_plane_reads_is_not_a_clean_answer(tmp_path):
    root = _tree(tmp_path, {
        "sub/_defaults.yaml": "defaults: {}\n",
        "sub/_defaults.yml": f'defaults: {{}}\n_routing_defaults:\n  receiver:\n'
                             f'    type: slack\n    api_url: "{SLACK}"\n',
        "sub/t1.yaml": 'tenants:\n  t1:\n    cpu: "1"\n'})
    proc = _run("--config-dir", root)
    assert proc.returncode == 1, proc.stderr
    assert "No credential is written out." not in proc.stdout
    assert "sub/_defaults.yml (unselected_defaults_carrier)" in proc.stdout
    doc = json.loads(_run("--config-dir", root, "--json").stdout)
    assert doc["unread"] == [{"file": "sub/_defaults.yml",
                              "reason": "unselected_defaults_carrier"}]
    assert doc["rotate"] == []


R7 = f"""\
tenants:
  team-a:
    _routing:
      receiver:
        type: slack
        api_url: "{SLACK}"
      overrides:
        - alertname: A
          group_wait_x: 010
          receiver:
            type: slack
            api_url: "https://hooks.slack.example.com/services/{CANARY}-o"
"""


def test_r7_only_the_list_that_cannot_be_written_back_is_left(tmp_path):
    root = _tree(tmp_path, {"t.yaml": R7})
    proc = _run("--config-dir", root, "--json")
    assert proc.returncode == 1, proc.stderr
    _no_canary(proc)
    doc = json.loads(proc.stdout)
    assert [(d["location"], d["reason"]) for d in doc["decisions"]] == [
        ("tenants.team-a._routing.overrides[0].receiver.api_url", "list_not_rewritable")]
    assert [m["entries"] for m in doc["manifests"]] == [["receiver.api_url"]]


def test_r8_a_tenant_files_top_level_enforced_route_is_unread(tmp_path):
    root = _tree(tmp_path, {"team-a.yaml": f"""\
_routing_enforced:
  enabled: true
  receiver: {{type: slack, api_url: "{SLACK}"}}
tenants:
  team-a:
    cpu: "1"
"""})
    found = raw_receivers(str(root))
    assert [(r.place, r.read) for r in found.receivers] == [("routing_enforced", False)]
    doc = json.loads(_run("--config-dir", root, "--json").stdout)
    assert [(d["location"], d["reason"]) for d in doc["decisions"]] == [
        ("_routing_enforced.receiver.api_url", "unread_place")]
    assert len(doc["rotate"]) == 1


def test_r8_a_failed_confd_write_names_the_manifests(monkeypatch, capsys):
    def refuse(*_a, **_k):
        raise mc._lib_confedit.EditCallerError(
            {"path": "t.yaml", "status": "caller_error", "reason": "bad_path",
             "message": "quoted " + CANARY}, "write")
    monkeypatch.setattr(mc._lib_confedit, "write_all", refuse)
    with pytest.raises(SystemExit) as e:
        mc._write_confd("conf.d", [], [{"path": "/x/da-t.yaml"}])
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "conf.d was not changed" in err and "/x/da-t.yaml" in err
    assert CANARY not in err


def test_r9_vocabulary():
    assert mc._vocab_kind("secret_ref_proxy_url") == "secret_ref_proxy_url"
    assert mc._vocab_kind("anything_else") == "other"
    assert mc._vocab_field("routes[12].receiver.http_config.oauth2.token_url") == \
        "routes[i].receiver.http_config.oauth2.token_url"
    assert mc._vocab_field("receiver.free.text") == "other"
    assert mc._vocab_tenant("team-a") == "team-a" and mc._vocab_tenant("Bad Id") is None




# ── round 3: no text of a file reaches the report (review2 shapes) ──────

CORE = "cnry7f1e"

#: name → (the conf.d files, the reason a person gets the file, or None when
#: the rewrite goes through; the text that must never be printed). A copy of
#: a value elsewhere in the file (x1c's _metadata, x2b's anchor, s3's comment
#: and title) is not the tool's to find: the place itself is rewritten.
SHAPES = {
    # x1c: a two-line block scalar, the same lines again under _metadata.
    "x1c": ({"team-a.yaml": f"""\
tenants:
  team-a:
    _routing:
      receiver:
        type: pagerduty
        routing_key: |-
          {CORE}-part1
          {CORE}-part2
    _metadata:
      note: |-
        {CORE}-part1
        {CORE}-part2
"""}, None, CORE),
    # x2b: a tab-escaped value in an anchor merged into the receiver.
    "x2b": ({"team-a.yaml": f"""\
_base: &base
  type: slack
  api_url: "https://hooks.slack.example.com/T/B/{CORE}\\t"
tenants:
  team-a:
    _routing:
      receiver:
        <<: *base
        channel: "#x"
"""}, None, CORE),
    # x2d: the anchor defined in a flow mapping under the merge key itself.
    "x2d": ({"team-a.yaml": f"""\
tenants:
  team-a:
    _routing:
      receiver:
        <<: &base {{type: slack, api_url: "https://hooks.slack.example.com/T/B/{CORE}\\t"}}
        channel: "#x"
"""}, None, CORE),
    # x2e: backslash and quote escapes, merged into basic_auth.
    "x2e": ({"team-a.yaml": f"""\
_auth: &auth
  username: bob
  password: "{CORE}\\\\q\\"z"
tenants:
  team-a:
    _routing:
      receiver:
        type: webhook
        url: https://h.example.com/x
        http_config:
          basic_auth:
            <<: *auth
"""}, None, CORE),
    # b16: a header named Authorization, an oauth2 secret, an endpoint_params
    # key named `secret` (allowed, stays): the rewrite is refused by the
    # binding, so --write writes nothing.
    "b16": ({"team-a.yaml": f"""\
tenants:
  team-a:
    _routing:
      receiver:
        type: webhook
        url: https://h.example.com/x
        http_config:
          http_headers:
            Authorization:
              secrets: ["Bearer {CORE}"]
          oauth2:
            client_id: id
            client_secret: "{CORE}-cs"
            token_url: https://t.example.com
            endpoint_params:
              secret: "{CORE}-ep"
"""}, None, CORE),
    # x5: a value of three characters.
    "x5": ({"team-a.yaml": """\
tenants:
  team-a:
    _routing:
      receiver:
        type: pagerduty
        routing_key: "Q7z"
"""}, None, "Q7z"),
    # s3: the old value in the comment of the line rewritten (da-guard
    # write would drop that comment), and part of it in another field.
    "s3": ({"team-a.yaml": f"""\
tenants:
  team-a:
    _routing:
      receiver:
        type: slack
        api_url: "https://hooks.slack.example.com/T/B/{CORE}"   # was https://hooks.slack.example.com/T/B/{CORE}
        channel: '#alerts'
        title: "{CORE}-in-title"
"""}, None, CORE),
    # Escapes: tab, backslash, quote, NEL (\\N) in one value.
    "escapes": ({"team-a.yaml": f"""\
tenants:
  team-a:
    _routing:
      receiver:
        type: pagerduty
        routing_key: "{CORE}\\t\\\\\\"\\N-x"
"""}, None, CORE),
    # A multi-line PEM-shaped value.
    "pem": ({"team-a.yaml": f"""\
tenants:
  team-a:
    _routing:
      receiver:
        type: pagerduty
        routing_key: |
          -----BEGIN PRIVATE KEY-----
          {CORE}AAAAline1
          {CORE}BBBBline2
          -----END PRIVATE KEY-----
"""}, None, CORE),
}


def _files_holding(base, text):
    return sorted(str(p.relative_to(base)) for p in base.rglob("*") if p.is_file()
                  and text in p.read_text(encoding="utf-8", errors="replace"))


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_values_only_ever_reach_the_manifest(tmp_path, shape):
    """⛔ Every mode and every error path: the value's text is on no output
    stream and in no file but the manifest (or still where it was, in a file
    the tool did not rewrite)."""
    files, held, core = SHAPES[shape]
    root = _tree(tmp_path, files)
    original = _snapshot(root)
    out = tmp_path / "secrets"
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    runs = [
        (["--config-dir", root], {1}),
        (["--config-dir", root, "--json"], {1}),
        (["--config-dir", root, "--manifests-out", out], {1}),
        (["--config-dir", root, "--write"], {2}),
        (["--config-dir", root, "--manifests-out", repo / "s", "--write"], {1, 2}),
        (["--config-dir", root, "--manifests-out", out, "--write", "--json"], {1}),
    ]
    docs = []
    for argv, rcs in runs:
        proc = _run(*argv)
        assert proc.returncode in rcs, (argv, proc.returncode, proc.stderr)
        assert core not in proc.stdout and core not in proc.stderr, argv
        if "--json" in argv:
            docs.append(json.loads(proc.stdout))
    last = docs[-1]
    allowed = {str(p.relative_to(tmp_path)) for p in out.glob("da-*.yaml")}
    leftover = [f for f in _files_holding(tmp_path, core)
                if f not in allowed and not f.startswith("conf.d/")]
    assert leftover == []
    if held is not None:
        assert held in {d["reason"] for d in last["decisions"]}
        assert last["written"] is False and not out.exists()
        assert _snapshot(root) == original
        assert _run("--config-dir", root).returncode == 1      # still listed
    elif last["blocked_after_rewrite"]:
        assert last["written"] is False and not out.exists()
    else:
        assert last["written"] is True
        rewritten = {r["location"] for r in last["plan"]}
        assert rewritten
        after = json.loads(_run("--config-dir", root, "--json").stdout)
        assert not rewritten & {r["location"] for r in after["rotate"]}
        manifest = yaml.safe_load((out / "da-team-a.yaml").read_text(encoding="utf-8"))
        assert all(core in v for v in manifest["stringData"].values())


def test_manifests_out_without_write_only_checks_the_path(tmp_path):
    """N3: without --write nothing is written; the plan says where it would be."""
    root = _tree(tmp_path, {"team-a.yaml": TENANT})
    out = tmp_path / "secrets"
    proc = _run("--config-dir", root, "--manifests-out", out, "--json")
    assert proc.returncode == 1, proc.stderr
    doc = json.loads(proc.stdout)
    assert doc["manifests"] == [{"name": "da-team-a", "tenant": "team-a",
                                 "path": str(out / "da-team-a.yaml"),
                                 "entries": ["receiver.api_url"], "written": False}]
    assert not out.exists()
    worktree_out = tmp_path / "repo" / "s"
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    assert _run("--config-dir", root, "--manifests-out", worktree_out).returncode == 2


def test_a_user_key_outside_the_http_token_charset_is_not_printed():
    assert mc._safe_key("X-Token") == "X-Token"
    for bad in ("X Token", "a\tb", "x" * 65, "é", ""):
        assert mc._safe_key(bad) == "<key>"


def test_a_crash_names_the_exception_type_only(tmp_path):
    """A crash inside the tool's work: the shared crash line and exit code,
    the exception's type name, a fixed text — never its message (it may
    quote a value), and no traceback of frames."""
    root = _tree(tmp_path, {"team-a.yaml": TENANT})
    wrapper = (
        "import os, runpy, sys\n"
        "tools, script = sys.argv[1], sys.argv[2]\n"
        "sys.argv = sys.argv[2:]\n"
        "sys.path.insert(0, os.path.dirname(script)); sys.path.insert(0, tools)\n"
        "import _lib_validation\n"
        "def boom(*a, **k):\n"
        f"    raise KeyError({SLACK!r})\n"
        "_lib_validation.plaintext_credential_findings = boom\n"
        "import migrate_credentials\n"
        "migrate_credentials.plaintext_credential_findings = boom\n"
        "runpy.run_path(script, run_name='__main__')\n")
    proc = subprocess.run(
        [sys.executable, "-c", wrapper, os.path.join(REPO, "scripts", "tools"), TOOL,
         "--config-dir", str(root), "--json"],
        capture_output=True, text=True, encoding="utf-8", check=False, timeout=300)
    # The shared crash code: 2, or the test run's override (tests/conftest.py).
    assert proc.returncode == int(os.environ.get("DA_TEST_CRASH_EXIT_CODE", "2")), proc.stderr
    assert proc.stdout == ""
    _no_canary(proc)
    assert proc.stderr.splitlines() == [
        f"KeyError: {mc.CRASH_TEXT}",
        f"ERROR: unexpected internal error (KeyError): {mc.CRASH_TEXT}"]


def test_a_rewrite_that_does_not_take_stops_the_whole_write(monkeypatch, tmp_path, capsys):
    """The dry-run rewrite read back still has the value at the place (here:
    da-guard made to hand back the file unchanged) ⇒ rewrite_not_effective,
    and --write writes nothing at all, the good file included."""
    root = _tree(tmp_path, {"team-a.yaml": TENANT,
                            "team-x.yaml": TENANT.replace("team-a", "team-x")})
    real = mc._dry_run

    def unchanged_for_team_x(config_dir, edits):
        out = real(config_dir, edits)
        if "team-x.yaml" in out and any("secret" in json.dumps(op) for op in edits["team-x.yaml"]):
            out["team-x.yaml"] = (root / "team-x.yaml").read_text(encoding="utf-8")
        return out
    monkeypatch.setattr(mc, "_dry_run", unchanged_for_team_x)
    out = tmp_path / "secrets"
    before = _snapshot(root)
    report = mc.main(["--config-dir", str(root), "--manifests-out", str(out), "--write",
                      "--json"])
    doc = report.document
    assert report.status == 1
    assert {(d["file"], d["reason"]) for d in doc["decisions"]} == {
        ("team-x.yaml", "rewrite_not_effective")}
    assert doc["written"] is False and not out.exists() and _snapshot(root) == before
    assert CANARY not in json.dumps(doc)


def test_after_the_write_the_files_are_read_back_and_judged(monkeypatch, tmp_path, capsys):
    """R3 TOCTOU: a write that did not land (here: `da-guard write` made a
    no-op) is caught by reading the files back — rc 2, the places named,
    the manifests written named, no value."""
    root = _tree(tmp_path, {"team-a.yaml": TENANT})
    real = mc._lib_confedit.write_all
    monkeypatch.setattr(mc._lib_confedit, "write_all",
                        lambda *a, **k: real(*a, **k) if k.get("dry_run") else [])
    out = tmp_path / "secrets"
    with pytest.raises(SystemExit) as e:
        mc.main(["--config-dir", str(root), "--manifests-out", str(out), "--write"])
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "conf.d was written, but these places are still written out" in err
    assert "team-a.yaml: tenants.team-a._routing.receiver.api_url" in err
    assert str(out / "da-team-a.yaml") in err and CANARY not in err


def test_a_manifest_write_failing_part_way_names_what_was_written(monkeypatch, tmp_path,
                                                                 capsys):
    """R5: the second manifest fails ⇒ rc 2, the first one named, conf.d untouched."""
    root = _tree(tmp_path, {"team-a.yaml": TENANT,
                            "team-b.yaml": TENANT.replace("team-a", "team-b")})
    real = mc._lib_io.write_operator_output
    calls = []

    def second_fails(path, *a, **k):
        calls.append(path)
        if len(calls) == 2:
            raise mc._lib_io.OutputWriteError(path, OSError(28, "No space left on device"))
        return real(path, *a, **k)
    monkeypatch.setattr(mc._lib_io, "write_operator_output", second_fails)
    out = tmp_path / "secrets"
    before = _snapshot(root)
    with pytest.raises(SystemExit) as e:
        mc.main(["--config-dir", str(root), "--manifests-out", str(out), "--write"])
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "conf.d was not changed" in err and str(out / "da-team-a.yaml") in err
    assert _snapshot(root) == before and CANARY not in err


def test_a_data_key_the_report_would_not_print_is_not_derived(tmp_path):
    """R6: the key passes the Secret data key rule but is longer than the
    report's key rule allows (64) ⇒ key_invalid, nothing rewritten."""
    name = "X-" + "a" * 50
    root = _tree(tmp_path, {"t.yaml": f"""\
tenants:
  team-a:
    _routing:
      receiver:
        type: webhook
        url: https://h.example.com/x
        http_config:
          http_headers:
            {name}:
              secrets: ["{CANARY}"]
"""})
    doc = json.loads(_run("--config-dir", root, "--json").stdout)
    assert [d["reason"] for d in doc["decisions"]] == ["key_invalid"]
    assert doc["plan"] == []


@pytest.mark.parametrize("stage", ["write", "verify"])
def test_a_changed_config_dir_names_what_is_already_on_disk(stage, monkeypatch):
    """#3724: --config-dir changed since its judgement is a refusal naming the
    flag (`InputChanged`); when it is met at the write or after it, the line
    still says what the run left on disk — the Secret manifests written, and
    whether conf.d was changed."""
    from _lib_exitcodes import InputChanged
    gone = InputChanged("--config-dir /c: changed after it was judged: x")

    def boom(*_a, **_k):
        raise gone

    manifests = [{"path": "/out/da-t.yaml"}]
    if stage == "write":
        monkeypatch.setattr(mc._lib_confedit, "write_all", boom)
        call, said = (lambda: mc._write_confd("/c", [], manifests)), "conf.d was not changed"
    else:
        monkeypatch.setattr(mc, "_read_text", boom)
        call, said = (lambda: mc._verify_written("/c", None, ["t.yaml"], [], manifests),
                      "conf.d was written before that")
    with pytest.raises(InputChanged) as exc:
        call()
    assert str(exc.value).startswith(str(gone)) and said in str(exc.value)
    assert "The Secret manifests were written: /out/da-t.yaml" in str(exc.value)
