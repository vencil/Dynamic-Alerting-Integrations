"""`da-tools migrate-credentials`（#1560 ⑤-a）與它讀 receiver 的走訪器。

工具只報告：把租戶明文寫的憑證改寫成 Secret 參照（diff／`--write`）、寫出每個租戶的
Secret（`--manifests-out`，0600）、列出待輪替清單。⛔ 憑證值只能進清單檔：每個情境都
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


def test_diff_mode_changes_nothing_and_masks_values(tmp_path):
    root = _tree(tmp_path, {"team-a.yaml": TENANT})
    before = _snapshot(root)
    proc = _run("--config-dir", root)
    assert proc.returncode == 1, proc.stderr
    _no_canary(proc)
    assert _snapshot(root) == before
    assert '-        api_url: "<redacted:0>" # the hook' in proc.stdout
    assert '+            key: "receiver.api_url"' in proc.stdout
    assert "team-a.yaml:9 tenants.team-a._routing.receiver.api_url [team-a]" in proc.stdout
    assert "(not applied: add --write)" in proc.stdout


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
                     f'--manifests-out "{out}"'],
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
    assert "is the conf.d file" in proc.stderr
    assert _snapshot(root) == before


MIXED_PLATFORM = f"""\
_routing_defaults:
  receiver:
    type: pagerduty
    routing_key: "{CANARY}"
_routing_enforced:
  enabled: true
  receiver:
    type: webhook
    url: "https://noc:{CANARY}@noc.example.com/hook"
    http_config:
      bearer_token: "{CANARY}"
"""

MIXED_TENANT = f"""\
tenants:
  team-b:
    _routing:
      receiver:
        type: webhook
        url: "https://hook.example.com/alerts"
        http_config:
          bearer_token: "{CANARY}"
          proxy_url: "http://u:{CANARY}@proxy.example.com:3128"
          proxy_connect_header:
            Proxy-Authorization: ["{CANARY}"]
          http_headers:
            "X Token":
              secrets: ["{CANARY}"]
      overrides:
        - alertname: Mail
          receiver:
            type: email
            to: [ops@example.com]
            from: am@example.com
            smarthost: smtp.example.com:587
            auth_password: "{CANARY}"
        - alertname: Hook
          receiver:
            type: webhook
            url: "https://ops:{CANARY}@hook.example.com/x"
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
                                     "receiver.http_config.bearer_token"]}]
    decisions = {(d["location"], d["reason"]) for d in doc["decisions"]}
    assert decisions == {
        ("_routing_defaults.receiver.routing_key", "platform_place"),
        ("_routing_enforced.receiver.url", "userinfo"),
        ("_routing_enforced.receiver.http_config.bearer_token", "platform_enforced"),
        ("tenants.team-b._routing.receiver.http_config.proxy_url", "proxy_credential"),
        ("tenants.team-b._routing.receiver.http_config.proxy_connect_header",
         "proxy_credential"),
        ('tenants.team-b._routing.receiver.http_config.http_headers.X Token.secrets[0]',
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
    assert doc["diff"] == "" and doc["manifests"] == []


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
