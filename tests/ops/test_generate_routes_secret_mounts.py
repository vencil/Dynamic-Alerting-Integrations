"""#1560：`generate-routes --secret-mounts-out`——產出的設定實際要讀的 Secret 掛載清單。

清單只從**產出的那份設定**（以 Alertmanager 的讀法讀回）走訪，不另從 conf.d 推導：
每個 `*_file` 欄位與 `http_headers.<name>.files`；`<root>/<名稱>/<key>` 列進
`secrets`，其餘路徑（根目錄外、或根目錄下不是兩段）列進 `other_files`。

* 單元：`required_mounts` 的分類、排序與空清單；
* CLI：租戶自己的、共用的、base config 帶來的、根目錄外的、自訂 `--secret-mount-root`、
  多租戶排序穩定、空清單；以及不讀它的模式、與 `-o`／`--base-config` 同檔、
  `-` 與 stdout 上的設定相撞，全是結束碼 2 且什麼都不寫。

租戶 id 都是 fixture 名稱（CLAUDE.md #9）。
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "tools"))
sys.path.insert(0, str(REPO / "scripts" / "tools" / "ops"))
sys.path.insert(0, str(REPO / "tests"))
from _platform_fs import symlink_or_skip  # noqa: E402
from _grar_merge import DEFAULT_MOUNT_ROOT, MOUNT_LIST_SCHEMA, required_mounts  # noqa: E402

_GAR = REPO / "scripts" / "tools" / "ops" / "generate_alertmanager_routes.py"
ROOT = DEFAULT_MOUNT_ROOT


# ── required_mounts: the walk and the classification ─────────────────────

def test_receiver_and_global_file_fields_are_listed_by_secret():
    cfg = {
        "global": {"slack_api_url_file": f"{ROOT}/corp-alerting/slack"},
        "receivers": [
            {"name": "tenant-t-a", "webhook_configs": [
                {"url_file": f"{ROOT}/da-t-a/hook",
                 "http_config": {"authorization": {"credentials_file": f"{ROOT}/da-t-a/token"}}}]},
            {"name": "tenant-t-b", "slack_configs": [{"api_url_file": f"{ROOT}/da-t-b/slack"}]},
        ],
    }
    doc = required_mounts(cfg, ROOT)
    assert doc == {
        "schema": MOUNT_LIST_SCHEMA, "mount_root": ROOT,
        "mounts": [{"name": "corp-alerting", "keys": ["slack"]},
                    {"name": "da-t-a", "keys": ["hook", "token"]},
                    {"name": "da-t-b", "keys": ["slack"]}],
        "other_files": [],
    }


def test_http_header_files_are_listed():
    cfg = {"receivers": [{"name": "r", "webhook_configs": [{
        "url": "https://hooks.example.com/x",
        "http_config": {"http_headers": {"X-Token": {"files": [f"{ROOT}/da-t-a/hdr"]}}}}]}]}
    assert required_mounts(cfg, ROOT)["mounts"] == [{"name": "da-t-a", "keys": ["hdr"]}]


@pytest.mark.parametrize("path", [
    "/etc/ssl/ca.pem",                       # outside the root
    f"{ROOT}/only-one-segment",              # under it, not <name>/<key>
    f"{ROOT}/name/sub/key",                  # under it, a key with a '/'
    f"{ROOT}//key",                          # an empty name
    f"{ROOT}-other/name/key",                # a sibling that shares the prefix
])
def test_a_path_that_is_not_root_name_key_goes_to_other_files(path):
    doc = required_mounts({"receivers": [{"name": "r", "webhook_configs": [
        {"url_file": f"{ROOT}/da-t-a/hook", "http_config": {"tls_config": {"ca_file": path}}}]}]},
        ROOT)
    assert doc["mounts"] == [{"name": "da-t-a", "keys": ["hook"]}]
    assert doc["other_files"] == [path]


def test_a_custom_root_and_a_trailing_slash_read_alike():
    cfg = {"receivers": [{"name": "r", "webhook_configs": [{"url_file": "/mnt/s/da-t-a/hook"}]}]}
    assert required_mounts(cfg, "/mnt/s")["mounts"] == [{"name": "da-t-a", "keys": ["hook"]}]
    assert required_mounts(cfg, "/mnt/s/")["mounts"] == [{"name": "da-t-a", "keys": ["hook"]}]
    # Under the default root the same path is not a Secret this list can name.
    assert required_mounts(cfg, ROOT)["other_files"] == ["/mnt/s/da-t-a/hook"]


def test_order_and_duplicates_do_not_change_the_document():
    def cfg(order):
        return {"receivers": [{"name": t, "webhook_configs": [
            {"url_file": f"{ROOT}/da-{t}/hook"}, {"url_file": f"{ROOT}/da-{t}/hook"}]}
            for t in order]}
    a = required_mounts(cfg(["t-b", "t-a", "t-c"]), ROOT)
    b = required_mounts(cfg(["t-c", "t-a", "t-b"]), ROOT)
    assert a == b
    assert [m["name"] for m in a["mounts"]] == ["da-t-a", "da-t-b", "da-t-c"]
    assert all(m["keys"] == ["hook"] for m in a["mounts"])


def test_a_config_without_file_fields_gives_an_empty_list():
    doc = required_mounts({"receivers": [{"name": "r", "webhook_configs": [
        {"url": "https://hooks.example.com/x"}]}]}, ROOT)
    assert doc["mounts"] == [] and doc["other_files"] == []


@pytest.mark.parametrize("template,mounts,other", [
    # literal name, literal key: both listed
    (f"{ROOT}/tmpl/alerts.tmpl", [{"name": "tmpl", "keys": ["alerts.tmpl"]}], []),
    # literal name, glob key: the Secret must be mounted, no key recorded
    (f"{ROOT}/tmpl/*.tmpl", [{"name": "tmpl", "keys": []}], []),
    (f"{ROOT}/tmpl/a?.tmpl", [{"name": "tmpl", "keys": []}], []),
    # a glob in the name segment cannot name a Secret
    (f"{ROOT}/*/alerts.tmpl", [], [f"{ROOT}/*/alerts.tmpl"]),
    (f"{ROOT}/tm[pq]l/alerts.tmpl", [], [f"{ROOT}/tm[pq]l/alerts.tmpl"]),
    # outside the root, or another shape under it
    ("/etc/am/*.tmpl", [], ["/etc/am/*.tmpl"]),
    (f"{ROOT}/tmpl/sub/x.tmpl", [], [f"{ROOT}/tmpl/sub/x.tmpl"]),
])
def test_top_level_templates_are_listed(template, mounts, other):
    doc = required_mounts({"templates": [template]}, ROOT)
    assert doc["mounts"] == mounts and doc["other_files"] == other


def test_a_template_glob_key_does_not_hide_a_literal_key_of_the_same_secret():
    doc = required_mounts({"templates": [f"{ROOT}/tmpl/*.tmpl"], "receivers": [
        {"name": "r", "webhook_configs": [{"url_file": f"{ROOT}/tmpl/hook"}]}]}, ROOT)
    assert doc["mounts"] == [{"name": "tmpl", "keys": ["hook"]}]


def test_values_are_never_in_the_document():
    cfg = {"receivers": [{"name": "r", "slack_configs": [
        {"api_url": "https://hooks.slack.com/services/T0/B0/value-that-must-not-appear",
         "api_url_file": f"{ROOT}/da-t-a/slack"}]}]}
    assert "value-that-must-not-appear" not in json.dumps(required_mounts(cfg, ROOT))


# ── through the CLI ──────────────────────────────────────────────────────

_POLICY = ("allowed_domains: ['*.example.com']\n"
           "shared_secrets:\n"
           "  corp-slack: {secret: corp-alerting, fields: [api_url]}\n")

_BASE = f"""route:
  receiver: default
receivers:
  - name: default
  - name: watchdog-heartbeat
    webhook_configs:
      - url_file: {ROOT}/watchdog-heartbeat/watchdog-heartbeat-url
        http_config:
          tls_config:
            ca_file: /etc/ssl/ca.pem
"""


def _tree(tmp_path: Path, tenants: dict[str, str]) -> Path:
    d = tmp_path / "conf.d"
    d.mkdir()
    (d / "_defaults.yaml").write_text("defaults:\n  cpu_usage_percent: 80\n", encoding="utf-8")
    for tenant, receiver in tenants.items():
        (d / f"{tenant}.yaml").write_text(
            f"tenants:\n  {tenant}:\n    cpu_usage_percent: '85'\n    _routing:\n"
            f"      receiver:\n{receiver}", encoding="utf-8")
    return d


_OWN = "        type: webhook\n        url: {secret: {key: hook}}\n"
_SHARED = "        type: slack\n        api_url: {secret: {shared: corp-slack, key: url}}\n"
_PLAIN = "        type: webhook\n        url: https://hooks.example.com/x\n"


def _run(tmp_path: Path, d: Path, *args: str) -> subprocess.CompletedProcess:
    policy = tmp_path / "policy.yaml"
    policy.write_text(_POLICY, encoding="utf-8")
    return subprocess.run([sys.executable, str(_GAR), "--config-dir", str(d),
                           "--policy", str(policy), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=300)


def _base(tmp_path: Path) -> str:
    p = tmp_path / "base.yml"
    p.write_text(_BASE, encoding="utf-8")
    return str(p)


@pytest.mark.parametrize("mode", ["--output-alertmanager", "--output-configmap"])
def test_own_shared_and_base_paths_are_listed(tmp_path, mode):
    d = _tree(tmp_path, {"t-b": _SHARED, "t-a": _OWN, "t-c": _PLAIN})
    out, mounts = tmp_path / "out.yaml", tmp_path / "mounts.json"
    r = _run(tmp_path, d, mode, "--base-config", _base(tmp_path),
             "-o", str(out), "--secret-mounts-out", str(mounts))
    assert r.returncode == 0, r.stderr
    doc = json.loads(mounts.read_text(encoding="utf-8"))
    assert doc == {
        "schema": MOUNT_LIST_SCHEMA, "mount_root": ROOT,
        "mounts": [{"name": "corp-alerting", "keys": ["url"]},
                    {"name": "da-t-a", "keys": ["hook"]},
                    {"name": "watchdog-heartbeat", "keys": ["watchdog-heartbeat-url"]}],
        "other_files": ["/etc/ssl/ca.pem"],
    }
    assert out.is_file()
    assert "Mount list: 3 Secret(s)" in r.stderr


def test_a_custom_mount_root_is_the_lists_root(tmp_path):
    d = _tree(tmp_path, {"t-a": _OWN})
    mounts = tmp_path / "mounts.json"
    r = _run(tmp_path, d, "--output-alertmanager", "--base-config", _base(tmp_path),
             "--secret-mount-root", "/mnt/s", "-o", str(tmp_path / "am.yml"),
             "--secret-mounts-out", str(mounts))
    assert r.returncode == 0, r.stderr
    doc = json.loads(mounts.read_text(encoding="utf-8"))
    assert doc["mount_root"] == "/mnt/s"
    assert doc["mounts"] == [{"name": "da-t-a", "keys": ["hook"]}]
    # The base's own path is under the default root, not this one.
    assert doc["other_files"] == [f"{ROOT}/watchdog-heartbeat/watchdog-heartbeat-url",
                                  "/etc/ssl/ca.pem"]


def test_the_document_is_byte_stable_across_runs(tmp_path):
    d = _tree(tmp_path, {"t-c": _OWN, "t-a": _OWN, "t-b": _OWN})
    texts = []
    for i in range(2):
        m = tmp_path / f"m{i}.json"
        r = _run(tmp_path, d, "--output-alertmanager", "-o", str(tmp_path / f"o{i}.yml"),
                 "--secret-mounts-out", str(m))
        assert r.returncode == 0, r.stderr
        texts.append(m.read_text(encoding="utf-8"))
    assert texts[0] == texts[1]
    assert [s["name"] for s in json.loads(texts[0])["mounts"]] == ["da-t-a", "da-t-b", "da-t-c"]


def test_no_reference_gives_an_empty_list(tmp_path):
    d = _tree(tmp_path, {"t-a": _PLAIN})
    mounts = tmp_path / "mounts.json"
    r = _run(tmp_path, d, "--output-alertmanager", "--secret-mounts-out", str(mounts))
    assert r.returncode == 0, r.stderr
    doc = json.loads(mounts.read_text(encoding="utf-8"))
    assert doc["mounts"] == [] and doc["other_files"] == []
    assert "receivers:" in r.stdout  # the config itself still went to stdout


@pytest.mark.parametrize("mode", [["--validate"], ["--apply", "--yes"],
                                  ["--output-alertmanager", "--dry-run"]])
def test_a_mode_that_writes_no_file_refuses_the_flag(tmp_path, mode):
    d = _tree(tmp_path, {"t-a": _OWN})
    mounts = tmp_path / "mounts.json"
    r = _run(tmp_path, d, *mode, "--secret-mounts-out", str(mounts))
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "--secret-mounts-out is not read" in r.stderr
    assert not mounts.exists()


def test_the_same_file_as_the_output_is_refused(tmp_path):
    d = _tree(tmp_path, {"t-a": _OWN})
    out = tmp_path / "out.yml"
    r = _run(tmp_path, d, "--output-alertmanager", "-o", str(out),
             "--secret-mounts-out", str(out))
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "same file as -o/--output" in r.stderr
    assert not out.exists()


def test_the_base_config_is_never_overwritten(tmp_path):
    d = _tree(tmp_path, {"t-a": _OWN})
    base = _base(tmp_path)
    r = _run(tmp_path, d, "--output-alertmanager", "--base-config", base,
             "-o", str(tmp_path / "out.yml"), "--secret-mounts-out", base)
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "--secret-mounts-out names the --base-config file" in r.stderr
    assert Path(base).read_text(encoding="utf-8") == _BASE


def test_stdout_list_beside_a_stdout_config_is_refused_before_printing(tmp_path):
    d = _tree(tmp_path, {"t-a": _OWN})
    r = _run(tmp_path, d, "--output-alertmanager", "--secret-mounts-out", "-")
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "--secret-mounts-out" in r.stderr
    assert "receivers:" not in r.stdout


def test_stdout_list_with_the_config_in_a_file(tmp_path):
    d = _tree(tmp_path, {"t-a": _OWN})
    r = _run(tmp_path, d, "--output-alertmanager", "-o", str(tmp_path / "am.yml"),
             "--secret-mounts-out", "-")
    assert r.returncode == 0, r.stderr
    # stdout carries the list alone: the run's own lines moved to stderr.
    doc = json.loads(r.stdout)
    assert doc["mounts"] == [{"name": "da-t-a", "keys": ["hook"]}]
    assert "Written to" in r.stderr


def test_the_list_and_the_findings_both_on_stdout_are_refused(tmp_path):
    d = _tree(tmp_path, {"t-a": _OWN})
    r = _run(tmp_path, d, "--output-alertmanager", "-o", str(tmp_path / "am.yml"),
             "--secret-mounts-out", "-", "--findings-json", "-")
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "standard output was asked for by" in r.stderr
    assert not (tmp_path / "am.yml").exists()


def test_an_unwritable_list_path_is_rc_2_naming_the_flag(tmp_path):
    d = _tree(tmp_path, {"t-a": _OWN})
    r = _run(tmp_path, d, "--output-alertmanager", "-o", str(tmp_path / "am.yml"),
             "--secret-mounts-out", str(tmp_path / "absent" / "m.json"))
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "--secret-mounts-out" in r.stderr and "Traceback" not in r.stderr


@pytest.mark.parametrize("mode", [["--validate"], ["--output-alertmanager", "--dry-run"]])
@pytest.mark.parametrize("with_o", [False, True])
def test_an_unread_stdout_list_is_refused_as_unread_not_as_a_stdout_clash(tmp_path, mode, with_o):
    """A mode that never reads --secret-mounts-out must not let it take part
    in sharing stdout: the refusal is the flag-not-read one. With -o the
    findings document shares stdout with it; without -o `--findings-json -`
    alone already clashes with the routes output (#3107 v2), so that shape
    pairs the list with the routes output only."""
    d = _tree(tmp_path, {"t-a": _OWN})
    other = (["-o", str(tmp_path / "am.yml"), "--findings-json", "-"] if with_o else [])
    r = _run(tmp_path, d, *mode, *other, "--secret-mounts-out", "-")
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "--secret-mounts-out is not read in" in r.stderr
    assert "standard output was asked for by" not in r.stderr


def test_every_stdout_claimant_is_named_when_they_clash(tmp_path):
    d = _tree(tmp_path, {"t-a": _OWN})
    r = _run(tmp_path, d, "--output-alertmanager", "--secret-mounts-out", "-",
             "--findings-json", "-")
    assert r.returncode == 2, (r.stdout, r.stderr)
    named = r.stderr[r.stderr.index("asked for by:"):]
    assert all(x in named for x in ("the routes output (no -o)", "--findings-json",
                                    "--secret-mounts-out"))


# ── #1560 (CodeRabbit on #3615): one judgement of every output destination ──

def _out_args(tmp_path):
    return _tree(tmp_path, {"t-a": _OWN}), tmp_path / "out.yml"


def _alias(target: Path, kind: str) -> Path:
    """*target* itself, or a symlink / hard link to it."""
    if kind == "same":
        return target
    link = target.with_name(f"{kind}-of-{target.name}")
    if kind == "symlink":
        symlink_or_skip(target, link)
    else:
        try:
            os.link(target, link)
        except (OSError, NotImplementedError) as exc:  # a filesystem without hard links
            pytest.skip(f"cannot make a hard link here: {exc}")
    return link


@pytest.mark.parametrize("first,second", [
    ("-o", "--findings-json"), ("-o", "--secret-mounts-out"),
    ("--findings-json", "--secret-mounts-out")])
@pytest.mark.parametrize("alias", ["same", "symlink", "hardlink"])
def test_two_outputs_that_are_one_file_are_refused(tmp_path, first, second, alias):
    d, _ = _out_args(tmp_path)
    target = tmp_path / "one.file"
    target.write_text("kept\n", encoding="utf-8")
    other = _alias(target, alias)
    flags = {first: str(target), second: str(other)}
    argv = ["--output-alertmanager"]
    for flag, value in flags.items():
        argv += [flag, value]
    if "-o" not in flags:
        argv += ["-o", str(tmp_path / "am.yml")]
    r = _run(tmp_path, d, *argv)
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "names the same file as" in r.stderr, r.stderr
    assert not (tmp_path / "am.yml").exists()
    # Nothing was written — the --findings-json document either, although it
    # is written on every exit: a taken destination is not written.
    assert target.read_text(encoding="utf-8") == "kept\n"


@pytest.mark.parametrize("flag", ["-o", "--findings-json", "--secret-mounts-out"])
@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_a_link_to_the_base_config_is_the_base_config(tmp_path, flag, kind):
    d, _ = _out_args(tmp_path)
    base = _base(tmp_path)
    alias = _alias(Path(base), kind)
    argv = ["--output-alertmanager", "--base-config", base, flag, str(alias)]
    if flag != "-o":
        argv += ["-o", str(tmp_path / "am.yml")]
    r = _run(tmp_path, d, *argv)
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert f"{flag if flag != '-o' else '-o/--output'} names the --base-config file" in r.stderr
    assert Path(base).read_text(encoding="utf-8") == _BASE


@pytest.mark.parametrize("other", ["--secret-mounts-out", "--findings-json"])
def test_an_o_that_is_stdout_owns_stdout(tmp_path, other):
    d, _ = _out_args(tmp_path)
    r = _run(tmp_path, d, "--output-alertmanager", "-o", "/dev/stdout", other, "-")
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "-o/--output /dev/stdout" in r.stderr and other in r.stderr
    assert "receivers:" not in r.stdout


def test_an_o_named_dash_is_a_file_not_stdout(tmp_path):
    """`-o -` is a file called `-` (the routes writer is not an operator
    output), so the list may take stdout beside it."""
    d, _ = _out_args(tmp_path)
    r = subprocess.run([sys.executable, str(_GAR), "--config-dir", str(d),
                        "--output-alertmanager", "-o", "-", "--secret-mounts-out", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=300, cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["schema"] == MOUNT_LIST_SCHEMA
    assert (tmp_path / "-").is_file()


def test_the_base_configs_templates_reach_the_list_through_the_cli(tmp_path):
    d = _tree(tmp_path, {"t-a": _PLAIN})
    base = tmp_path / "base.yml"
    base.write_text(f"templates:\n  - {ROOT}/am-templates/*.tmpl\n" + _BASE, encoding="utf-8")
    mounts = tmp_path / "mounts.json"
    r = _run(tmp_path, d, "--output-alertmanager", "--base-config", str(base),
             "-o", str(tmp_path / "am.yml"), "--secret-mounts-out", str(mounts))
    assert r.returncode == 0, r.stderr
    doc = json.loads(mounts.read_text(encoding="utf-8"))
    assert {"name": "am-templates", "keys": []} in doc["mounts"]


def test_devices_collide_with_nothing(tmp_path):
    """`/dev/null` for two outputs replaces nothing (it is not a regular
    file): no clash, as before #1560's one judgement."""
    if not os.path.exists("/dev/null") or not stat.S_ISCHR(os.stat("/dev/null").st_mode):
        pytest.skip("no /dev/null character device on this host")
    d, _ = _out_args(tmp_path)
    r = _run(tmp_path, d, "--output-alertmanager", "-o", "/dev/null",
             "--findings-json", "/dev/null", "--secret-mounts-out", "/dev/null")
    assert r.returncode == 0, (r.stdout, r.stderr)
