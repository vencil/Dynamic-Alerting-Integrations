"""#3257：租戶設定裡的 receiver 不得帶檔案型欄位（`*_file`、`*_ref`、`files`）。

這類欄位會讓 Alertmanager 讀取它自己容器內的檔案（`*_file`、http_headers 的
`files`），或去找它沒有的 secret manager（`*_ref`：設定載得進去、建 receiver 時失敗，
整次 reload 跟著失敗），因此一律拒收，任何深度都算，`http_config` 底下也算。

判斷是一條規則（`_lib_validation.is_file_reference_key`），不是欄位清單；schema 以
`definitions.fileReferenceKey` 寫同一條規則，並以 `definitions.fileReferenceFree`
套在 `definitions.receiver` 上。這裡釘住：

* 規則兩邊一致：Python 判斷函式與 schema 的 pattern 對每個候選鍵給同一個答案；
* schema 在 receiver 可達範圍內沒有任何符合規則的 property（也沒有 `required`）；
* schema 與產生器對同一組 receiver 給同一個裁決（反例與正向控制）；
* 產生器在每個會 render receiver 的位置（租戶 `_routing`、`overrides`、`routes`、
  `_routing_defaults`、`_routing_enforced`、routing profile）都整棵樹拒收，
  所有模式 rc 1、不寫出；不含檔案欄位的 http_config 照常通過；
* `--base-config` 的 `global` 若帶 receiver 會繼承的憑證，產生器拒收；平台自己
  base config 裡 watchdog 的 `url_file` 不受影響。

租戶 id 都是 fixture 名稱（CLAUDE.md #9）。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "tools"))
sys.path.insert(0, str(REPO / "scripts" / "tools" / "ops"))
from _lib_validation import (  # noqa: E402
    FILE_REFERENCE_KEY_PATTERN,
    file_reference_paths,
    is_file_reference_key,
    receiver_file_reference_problem,
    receiver_optional_problem,
)
from _grar_merge import build_receiver_config  # noqa: E402
from _grar_parse import load_tenant_tree  # noqa: E402
from _grar_render import (  # noqa: E402
    assemble_configmap,
    base_global_inherited_credentials,
    load_base_config,
)

_SCHEMA_PATH = REPO / "docs" / "schemas" / "tenant-config.schema.json"
_GAR = REPO / "scripts" / "tools" / "ops" / "generate_alertmanager_routes.py"
_BASE = REPO / "k8s" / "03-monitoring" / "configmap-alertmanager.yaml"
_REFUSAL = "config problem(s) — nothing was generated, written or applied"
_URL = "https://hooks.example.com/a"


def _schema() -> dict:
    with open(_SCHEMA_PATH, encoding="utf-8") as fh:
        return json.load(fh)


# ── 1. the rule, on both sides ───────────────────────────────────────────

def _all_schema_property_names(node, out: set) -> set:
    if isinstance(node, dict):
        props = node.get("properties")
        if isinstance(props, dict):
            out.update(props)
        for v in node.values():
            _all_schema_property_names(v, out)
    elif isinstance(node, list):
        for v in node:
            _all_schema_property_names(v, out)
    return out


# Alertmanager's own file-reference keys (prometheus/common HTTPClientConfig
# and the structs under it, v0.33.1), the keys this rule exists for, plus
# near misses that must NOT match.
_MATCH = ["bearer_token_file", "username_file", "username_ref", "password_file",
          "password_ref", "credentials_file", "credentials_ref", "client_secret_file",
          "client_secret_ref", "client_certificate_key_file", "client_certificate_key_ref",
          "ca_file", "cert_file", "key_file", "ca_ref", "cert_ref", "key_ref", "files",
          "url_file", "api_url_file", "auth_password_file", "x_file", "_ref",
          # Python's `$` also matches before ONE final newline, in the
          # schema's pattern as applied by jsonschema and in the generator
          # alike: refused on both sides.
          "password_file\n", "files\n", "key_ref\n"]
_NO_MATCH = ["file", "files_x", "profile", "refs", "ref", "prefix_files", "file_name",
             "password", "bearer_token", "secrets", "values", "Files", "KEY_FILE",
             "ca", "x-file", "x_file2", "client_secret",
             # a newline that is not the final character does not end the key
             "password_file\nx", "\nfiles", "files\n\n", "x_file \n"]


def test_python_rule_and_schema_pattern_agree():
    pattern = _schema()["definitions"]["fileReferenceKey"]["pattern"]
    # One string, not two spellings of one idea (`str.endswith` disagreed
    # with the pattern on "password_file\n").
    assert pattern == FILE_REFERENCE_KEY_PATTERN
    candidates = set(_MATCH) | set(_NO_MATCH) | _all_schema_property_names(_schema(), set())
    wrong = sorted(k for k in candidates
                   if (re.search(pattern, k) is not None) != is_file_reference_key(k))
    assert not wrong, f"schema pattern and Python rule disagree on: {wrong}"
    assert all(is_file_reference_key(k) for k in _MATCH)
    assert not any(is_file_reference_key(k) for k in _NO_MATCH)
    assert not is_file_reference_key(5) and not is_file_reference_key(None)


def _receiver_reach(defs: dict) -> dict[str, dict]:
    """Every definition reachable from definitions.receiver through $ref."""
    seen: dict[str, dict] = {}

    def walk(node):
        if isinstance(node, dict):
            ref = node.get("$ref", "")
            if ref.startswith("#/definitions/"):
                name = ref[len("#/definitions/"):]
                if name not in seen:
                    seen[name] = defs[name]
                    walk(defs[name])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    seen["receiver"] = defs["receiver"]
    walk(defs["receiver"])
    return seen


def test_schema_has_no_file_reference_property_a_receiver_reaches():
    defs = _schema()["definitions"]
    reach = _receiver_reach(defs)
    # The walk must see the http_config tree, or an empty answer is vacuous.
    assert {"webhookReceiver", "httpConfig", "httpBasicAuth", "httpAuthorization",
            "httpOAuth2", "httpTLSConfig", "httpHeader", "fileReferenceFree"} <= set(reach)
    found = []

    def scan(where, node):
        if isinstance(node, dict):
            for k in node.get("properties", {}) if isinstance(node.get("properties"), dict) else ():
                if is_file_reference_key(k):
                    found.append(f"{where}.properties.{k}")
            req = node.get("required")
            if isinstance(req, list):
                found.extend(f"{where}.required:{k}" for k in req if is_file_reference_key(k))
            for key, v in node.items():
                scan(f"{where}.{key}", v)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                scan(f"{where}[{i}]", v)

    for name, d in reach.items():
        if name == "fileReferenceKey":
            continue  # the rule itself
        scan(name, d)
    assert not found, f"file-reference keys still in the schema: {found}"


def test_receiver_carries_the_rule():
    """The rule is applied once, on the receiver union, not per field."""
    defs = _schema()["definitions"]
    assert {"$ref": "#/definitions/fileReferenceFree"} in defs["receiver"]["allOf"]
    free = defs["fileReferenceFree"]
    assert free["propertyNames"] == {"not": {"$ref": "#/definitions/fileReferenceKey"}}
    assert free["additionalProperties"] == {"$ref": "#/definitions/fileReferenceFree"}
    assert free["items"] == {"$ref": "#/definitions/fileReferenceFree"}


# ── 2. the schema and the generator give one verdict ─────────────────────

def _hook(http_config=None, **extra) -> dict:
    r = {"type": "webhook", "url": _URL, **extra}
    if http_config is not None:
        r["http_config"] = http_config
    return r


REFUSED = {
    "basic_auth.password_file": _hook({"basic_auth": {"username": "u", "password_file": "/p"}}),
    "basic_auth.username_ref": _hook({"basic_auth": {"username_ref": "u", "password": "p"}}),
    "bearer_token_file": _hook({"bearer_token_file": "/t"}),
    "bearer_token_file-empty": _hook({"bearer_token_file": ""}),
    "bearer_token_file-null": _hook({"bearer_token_file": None}),
    "authorization.credentials_file": _hook({"authorization": {"credentials_file": "/c"}}),
    "oauth2.client_secret_file": _hook({"oauth2": {"client_id": "c", "token_url": "https://t.example.com/x",
                                                   "client_secret_file": "/s"}}),
    "oauth2.client_secret_ref": _hook({"oauth2": {"client_id": "c", "token_url": "https://t.example.com/x",
                                                  "client_secret_ref": "s"}}),
    "oauth2.tls_config.key_file": _hook({"oauth2": {"client_id": "c", "token_url": "https://t.example.com/x",
                                                    "tls_config": {"cert": "c", "key_file": "/k"}}}),
    "oauth2.claims-nested": _hook({"oauth2": {"client_id": "c", "token_url": "https://t.example.com/x",
                                              "claims": {"a": {"b_file": "/x"}}}}),
    "tls_config.key_file": _hook({"tls_config": {"cert": "c", "key_file": "/k"}}),
    "tls_config.ca_file": _hook({"tls_config": {"ca_file": "/ca"}}),
    "tls_config.cert_ref": _hook({"tls_config": {"cert_ref": "c", "key": "k"}}),
    "http_headers.files": _hook({"http_headers": {"X-Token": {"files": ["/t"]}}}),
    "proxy_connect_header.files": _hook({"proxy_url": "http://proxy.example.com:3128",
                                         "proxy_connect_header": {"files": ["/t"]}}),
    "proxy_connect_header.x_file": _hook({"proxy_url": "http://proxy.example.com:3128",
                                          "proxy_connect_header": {"X_file": ["v"]}}),
    "email.headers.x_ref": {"type": "email", "to": ["a@example.com"], "from": "am@example.com",
                            "smarthost": "smtp.example.com:587", "headers": {"x_ref": "v"}},
}

ACCEPTED = {
    "no-http_config": _hook(),
    "basic_auth-inline": _hook({"basic_auth": {"username": "u", "password": "p"}}),
    "bearer_token": _hook({"bearer_token": "t"}),
    "authorization-inline": _hook({"authorization": {"type": "Bearer", "credentials": "c"}}),
    "oauth2-inline": _hook({"oauth2": {"client_id": "c", "client_secret": "s",
                                       "token_url": "https://t.example.com/x"}}),
    "tls_config-inline": _hook({"tls_config": {"ca": "pem", "cert": "c", "key": "k",
                                               "server_name": "s", "min_version": "TLS12"}}),
    "http_headers-values-secrets": _hook({"http_headers": {"X-Team": {"values": ["a"], "secrets": ["b"]}}}),
    "proxy": _hook({"proxy_url": "http://proxy.example.com:3128",
                    "proxy_connect_header": {"X-Proxy": ["v"]}}),
    "email": {"type": "email", "to": ["a@example.com"], "from": "am@example.com",
              "smarthost": "smtp.example.com:587", "headers": {"X-Team": "a"}},
}


def _schema_valid(receiver: dict) -> bool:
    doc = {"tenants": {"t1": {"_routing": {"receiver": receiver}}}}
    return not list(jsonschema.Draft7Validator(_schema()).iter_errors(doc))


@pytest.mark.parametrize("name", sorted(REFUSED))
def test_refused_by_schema_and_generator(name):
    receiver = REFUSED[name]
    assert file_reference_paths(receiver), name
    assert not _schema_valid(receiver), f"{name}: the schema accepts it"
    problem = receiver_file_reference_problem(receiver)
    assert problem and "_file or _ref" in problem
    rtype = receiver["type"]
    assert receiver_optional_problem(rtype, receiver) == problem
    cfg, warnings = build_receiver_config(receiver, "t1", tenant_id="t1")
    assert cfg is None, f"{name}: rendered {cfg}"
    assert [w.kind for w in warnings] == ["invalid_receiver_field"]


@pytest.mark.parametrize("name", sorted(ACCEPTED))
def test_accepted_by_schema_and_generator(name):
    receiver = ACCEPTED[name]
    assert file_reference_paths(receiver) == []
    assert _schema_valid(receiver), f"{name}: the schema refuses it"
    assert receiver_file_reference_problem(receiver) is None
    cfg, warnings = build_receiver_config(receiver, "t1", tenant_id="t1")
    assert cfg is not None and not warnings, warnings


def test_paths_name_every_key_at_its_depth():
    receiver = _hook({"basic_auth": {"password_file": "/p", "username_ref": "u"},
                      "http_headers": {"X-A": {"files": ["/x"]}},
                      "oauth2": {"claims": {"l": [{"k_file": 1}]}}})
    assert file_reference_paths(receiver) == [
        "http_config.basic_auth.password_file", "http_config.basic_auth.username_ref",
        "http_config.http_headers.X-A.files", "http_config.oauth2.claims.l[0].k_file"]


def test_a_key_that_is_not_a_string_is_refused():
    # Alertmanager decodes a `!!binary` key to its text (`password_file`);
    # the rule cannot read a bytes key's name, so it refuses any non-string
    # key rather than rely on the file reader rejecting the tag first.
    receiver = _hook({"basic_auth": {b"password_file": "/p"}, "tls_config": {1: "x"}})
    assert file_reference_paths(receiver) == [
        "http_config.basic_auth.b'password_file'", "http_config.tls_config.1"]


# ── 3. the generator refuses the whole tree, wherever the receiver sits ──

_DEFAULTS = "defaults:\n  cpu_usage_percent: 80\n"
_BAD_HC = "http_config:\n  basic_auth:\n    username: u\n    password_file: /etc/alertmanager/alertmanager.yml\n"


def _indent(text: str, n: int) -> str:
    return "".join(" " * n + line + "\n" for line in text.splitlines())


def _recv(url: str, ind: int, http_config: str = "") -> str:
    return _indent(f"receiver:\n  type: webhook\n  url: {url}\n" + _indent(http_config, 2), ind)


def _tenant(t: str, routing: str) -> str:
    return f"tenants:\n  {t}:\n    cpu_usage_percent: '85'\n    _routing:\n{routing}"


def _tree(where: str, http_config: str) -> dict[str, str]:
    good = _recv("https://hooks.example.com/t-a", 6)
    files = {"_defaults.yaml": _DEFAULTS, "t-a.yaml": _tenant("t-a", good),
             "t-b.yaml": _tenant("t-b", _recv("https://hooks.example.com/t-b", 6))}
    if where == "tenant":
        files["t-a.yaml"] = _tenant("t-a", _recv("https://hooks.example.com/t-a", 6, http_config))
    elif where == "override":
        files["t-a.yaml"] = _tenant("t-a", good + "      overrides:\n        - alertname: X\n"
                                    + _recv("https://hooks.example.com/o", 10, http_config))
    elif where == "route":
        files["t-a.yaml"] = _tenant("t-a", good + "      routes:\n        - match: {severity: critical}\n"
                                    + _recv("https://hooks.example.com/r", 10, http_config))
    elif where == "routing_defaults":
        files["_defaults.yaml"] = _DEFAULTS + "_routing_defaults:\n" + _recv(
            "https://hooks.example.com/d", 2, http_config)
    elif where == "routing_enforced":
        files["_defaults.yaml"] = _DEFAULTS + "_routing_enforced:\n  enabled: true\n" + _recv(
            "https://noc.example.com/a", 2, http_config)
    elif where == "routing_defaults_route":
        files["_defaults.yaml"] = _DEFAULTS + "_routing_defaults:\n" + _recv(
            "https://hooks.example.com/d", 2) + "  routes:\n    - match: {severity: critical}\n" + _recv(
            "https://hooks.example.com/dr", 6, http_config)
    elif where == "routing_enforced_override":
        files["_defaults.yaml"] = _DEFAULTS + "_routing_enforced:\n  enabled: true\n" + _recv(
            "https://noc.example.com/a", 2) + "  overrides:\n    - alertname: X\n" + _recv(
            "https://noc.example.com/o", 6, http_config)
    elif where == "routing_enforced_route":
        files["_defaults.yaml"] = _DEFAULTS + "_routing_enforced:\n  enabled: true\n" + _recv(
            "https://noc.example.com/a", 2) + "  routes:\n    - match: {severity: critical}\n" + _recv(
            "https://noc.example.com/r", 6, http_config)
    elif where == "threshold_profile":
        files["_profiles.yaml"] = "profiles:\n  p1:\n    _routing:\n" + _recv(
            "https://hooks.example.com/tp", 6, http_config)
    elif where == "routing_profile":
        files["_routing_profiles.yaml"] = "routing_profiles:\n  p1:\n" + _recv(
            "https://hooks.example.com/p", 4, http_config)
        files["t-a.yaml"] = "tenants:\n  t-a:\n    cpu_usage_percent: '85'\n    _routing_profile: p1\n"
    else:
        raise AssertionError(where)
    return files


# where → the finding's field
_WHERE = {
    "tenant": "_routing.receiver.http_config.basic_auth.password_file",
    "override": "_routing.overrides[0].receiver.http_config.basic_auth.password_file",
    "route": "_routing.routes[0].receiver.http_config.basic_auth.password_file",
    # #3257 review: places a receiver is NOT rendered from today (a WARN
    # names them as unread) — refused all the same, the block being walked
    # whole rather than at the places a receiver is rendered from.
    "routing_defaults_route": "_routing_defaults.routes[0].receiver.http_config.basic_auth.password_file",
    "routing_enforced_override":
        "_routing_enforced.overrides[0].receiver.http_config.basic_auth.password_file",
    "routing_enforced_route": "_routing_enforced.routes[0].receiver.http_config.basic_auth.password_file",
    "threshold_profile": "profiles.p1._routing.receiver.http_config.basic_auth.password_file",
    "routing_defaults": "_routing_defaults.receiver.http_config.basic_auth.password_file",
    "routing_enforced": "_routing_enforced.receiver.http_config.basic_auth.password_file",
    "routing_profile": "routing_profiles.p1.receiver.http_config.basic_auth.password_file",
}


def _write(tmp_path: Path, files: dict[str, str]) -> Path:
    d = tmp_path / "conf.d"
    d.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        (d / rel).write_text(text, encoding="utf-8")
    return d


def _gar(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(_GAR), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=300, env=dict(os.environ))


@pytest.mark.parametrize("where", sorted(_WHERE))
def test_the_tree_records_one_error_per_key(tmp_path, where):
    errs = load_tenant_tree(str(_write(tmp_path, _tree(where, _BAD_HC)))).field_type_errors
    assert [(e.kind, e.field) for e in errs] == [("invalid_receiver_field", _WHERE[where])], errs
    assert "_file or _ref" in errs[0].message


@pytest.mark.parametrize("where", sorted(_WHERE))
def test_a_clean_http_config_in_the_same_place_is_no_error(tmp_path, where):
    clean = "http_config:\n  basic_auth:\n    username: u\n    password: p\n"
    errs = load_tenant_tree(str(_write(tmp_path, _tree(where, clean)))).field_type_errors
    assert errs == []


_MODES = {
    "render": lambda out: ["-o", str(out)],
    "dry-run": lambda out: ["--dry-run"],
    "output-configmap": lambda out: ["--output-configmap", "-o", str(out)],
    "validate-strict": lambda out: ["--validate", "--strict"],
}


@pytest.mark.parametrize("where", ["tenant", "routing_defaults", "override",
                                   "routing_defaults_route", "routing_enforced_override",
                                   "routing_enforced_route", "threshold_profile"])
def test_every_mode_refuses_and_writes_nothing(tmp_path, where):
    d = _write(tmp_path, _tree(where, _BAD_HC))
    out = tmp_path / "out.yaml"
    for mode, argv in _MODES.items():
        p = _gar("--config-dir", str(d), *argv(out))
        assert p.returncode == 1, (mode, p.returncode, p.stderr)
        assert _REFUSAL in p.stderr, (mode, p.stderr)
        assert "password_file" in p.stderr, (mode, p.stderr)
        assert not out.exists(), f"{mode} wrote {out}"
        assert "/etc/alertmanager/alertmanager.yml" not in p.stdout, (mode, p.stdout)


def test_validate_strict_passes_without_file_keys(tmp_path):
    clean = "http_config:\n  basic_auth:\n    username: u\n    password: p\n"
    p = _gar("--config-dir", str(_write(tmp_path, _tree("tenant", clean))), "--validate", "--strict")
    assert p.returncode == 0, p.stderr
    assert _REFUSAL not in p.stderr


# ── 4. the base config ───────────────────────────────────────────────────

def test_platform_base_keeps_its_watchdog_url_file(tmp_path):
    """The platform's own base config is not a tenant receiver: its
    watchdog-heartbeat url_file is rendered as written, and its global
    carries no inherited credential."""
    with open(_BASE, encoding="utf-8") as fh:
        am_yml = yaml.safe_load(fh)["data"]["alertmanager.yml"]
    base_file = tmp_path / "base.yaml"
    base_file.write_text(am_yml, encoding="utf-8")
    base = load_base_config(str(base_file))
    assert base_global_inherited_credentials(base) == []
    d = _write(tmp_path, _tree("tenant", "http_config:\n  bearer_token: t\n"))
    out = tmp_path / "cm.yaml"
    p = _gar("--config-dir", str(d), "--output-configmap", "--base-config", str(base_file),
             "-o", str(out))
    assert p.returncode == 0, p.stderr
    rendered = yaml.safe_load(yaml.safe_load(out.read_text(encoding="utf-8"))["data"]["alertmanager.yml"])
    by_name = {r["name"]: r for r in rendered["receivers"]}
    assert by_name["watchdog-heartbeat"]["webhook_configs"][0]["url_file"]
    tenant_recvs = [r for n, r in by_name.items() if n.startswith("tenant-")]
    assert tenant_recvs
    assert all(file_reference_paths(r) == [] for r in tenant_recvs)


_GLOBAL_CREDENTIALS = {
    "smtp_auth_username": "global:\n  smtp_auth_username: u\n",
    "smtp_auth_password": "global:\n  smtp_auth_password: p\n",
    "smtp_auth_password_file": "global:\n  smtp_auth_password_file: /etc/alertmanager/secrets/smtp\n",
    "smtp_auth_secret": "global:\n  smtp_auth_secret: s\n",
    "smtp_auth_secret_file": "global:\n  smtp_auth_secret_file: /s\n",
    "smtp_auth_identity": "global:\n  smtp_auth_identity: i\n",
    "http_config.basic_auth": "global:\n  http_config:\n    basic_auth:\n      username: u\n      password: p\n",
    "http_config.bearer_token_file": "global:\n  http_config:\n    bearer_token_file: /t\n",
    "http_config.authorization": "global:\n  http_config:\n    authorization:\n      credentials: c\n",
}


@pytest.mark.parametrize("name", sorted(_GLOBAL_CREDENTIALS))
def test_base_global_credential_is_refused(tmp_path, name):
    base_file = tmp_path / "base.yaml"
    base_file.write_text(_GLOBAL_CREDENTIALS[name], encoding="utf-8")
    assert base_global_inherited_credentials(load_base_config(str(base_file))) == [f"global.{name}"]
    d = _write(tmp_path, _tree("tenant", ""))
    out = tmp_path / "cm.yaml"
    p = _gar("--config-dir", str(d), "--output-configmap", "--base-config", str(base_file),
             "-o", str(out))
    assert p.returncode == 1, (p.returncode, p.stderr)
    assert f"global.{name}" in p.stderr
    assert not out.exists()
    with pytest.raises(ValueError, match="global section sets"):
        assemble_configmap(load_base_config(str(base_file)), [], [], [])


def test_base_global_without_credentials_passes(tmp_path):
    base_file = tmp_path / "base.yaml"
    base_file.write_text(
        "global:\n  resolve_timeout: 5m\n  smtp_smarthost: smtp.example.com:587\n"
        "  smtp_from: am@example.com\n  smtp_auth_username: ''\n  smtp_auth_password: ~\n"
        "  http_config:\n    follow_redirects: true\n    tls_config:\n      server_name: s\n",
        encoding="utf-8")
    assert base_global_inherited_credentials(load_base_config(str(base_file))) == []
    d = _write(tmp_path, _tree("tenant", ""))
    p = _gar("--config-dir", str(d), "--output-configmap", "--base-config", str(base_file),
             "--dry-run")
    assert p.returncode == 0, p.stderr


def test_base_global_is_checked_before_the_tenant_scan(tmp_path):
    """A tree with no routes cannot pass a credential base in silence."""
    base_file = tmp_path / "base.yaml"
    base_file.write_text(_GLOBAL_CREDENTIALS["smtp_auth_password"], encoding="utf-8")
    d = _write(tmp_path, {"_defaults.yaml": _DEFAULTS})
    p = _gar("--config-dir", str(d), "--output-configmap", "--base-config", str(base_file),
             "--dry-run")
    assert p.returncode == 1, (p.returncode, p.stderr)
    assert "global.smtp_auth_password" in p.stderr


def test_apply_merge_refuses_a_cluster_global_credential():
    """--apply merges into the cluster's config, whose global is the base."""
    from _grar_render import _merge_routes_receivers_inhibits
    existing = {"global": {"smtp_auth_password": "p"}, "route": {"receiver": "default"},
                "receivers": [{"name": "default"}], "inhibit_rules": []}
    with pytest.raises(ValueError, match="global.smtp_auth_password"):
        _merge_routes_receivers_inhibits(existing, [], [], [])


def test_alertmanager_pod_mounts_no_service_account_token():
    """Neither container calls the Kubernetes API, so no token is mounted."""
    path = REPO / "k8s" / "03-monitoring" / "deployment-alertmanager.yaml"
    with open(path, encoding="utf-8") as fh:
        deployment = yaml.safe_load(fh)
    assert deployment["kind"] == "Deployment"
    assert deployment["spec"]["template"]["spec"]["automountServiceAccountToken"] is False


# ── 5. the scan covers every receiver a routing block renders ────────────

_BLOCKS = {
    # block → (field_prefix as the reader passes it, the root the scan names)
    "routing": ("", "_routing."),
    "routing_defaults": ("_routing_defaults.", "_routing_defaults."),
    "routing_enforced": ("_routing_enforced.", "_routing_enforced."),
}


def _full_block() -> dict:
    hook = {"type": "webhook", "url": _URL}
    return {"receiver": dict(hook),
            "overrides": [{"alertname": "X", "receiver": dict(hook)}],
            "routes": [{"match": {"severity": "critical"}, "receiver": dict(hook)}]}


@pytest.mark.parametrize("block", sorted(_BLOCKS))
def test_every_rendered_receiver_is_inside_the_scan(block):
    """Each receiver `routing_block_receivers` hands to the renderer, with a
    file-reference key planted in it, is named by the scan — the rendered
    receiver paths are a subset of what the rule walks. (The converse is
    not asked: the scan walks more, on purpose.)"""
    from _grar_validate import routing_block_file_reference_errors, routing_block_receivers
    prefix, root = _BLOCKS[block]
    positions = routing_block_receivers(_full_block(), block=block, where="w",
                                        field_prefix=prefix)
    assert positions, "no rendered receiver: the subset would be vacuous"
    for _recv_obj, _label, path in positions:
        obj = _full_block()
        # plant the key in the receiver at *path* (relative to the block)
        rel = path[len(prefix):]
        target = obj
        for part in re.findall(r"[^.\[\]]+|\[\d+\]", rel):
            target = target[int(part[1:-1])] if part.startswith("[") else target[part]
        target["http_config"] = {"bearer_token_file": "/t"}
        got = [f for f, _m in routing_block_file_reference_errors(
            obj, block=block, where="w", field_prefix=prefix)]
        want = f"{root}{rel}.http_config.bearer_token_file"
        assert got == [want], (block, path, got)


def test_label_names_under_match_are_not_judged():
    """A label may be called `log_file`; nothing under `match` reaches a
    receiver. Its values are still walked."""
    from _grar_validate import routing_block_file_reference_errors
    block = {"receiver": {"type": "webhook", "url": _URL},
             "routes": [{"match": {"log_file": "x", "files": "y"},
                         "receiver": {"type": "webhook", "url": _URL}}]}
    assert routing_block_file_reference_errors(block, block="routing", where="w") == []
    block["routes"][0]["match"]["log_file"] = {"key_file": "/k"}
    got = [f for f, _m in routing_block_file_reference_errors(block, block="routing", where="w")]
    assert got == ["_routing.routes[0].match.log_file.key_file"]


@pytest.mark.parametrize("block,want", [
    # a header named `match` inside a receiver is not a label map
    ({"receiver": {"type": "webhook", "url": _URL,
                   "http_config": {"http_headers": {"match": {"files": ["/f"]}}}}},
     ["_routing.receiver.http_config.http_headers.match.files"]),
    ({"routes": [{"match": {"a": "b"},
                  "receiver": {"type": "webhook", "url": _URL,
                               "http_config": {"http_headers": {"match": {"files": ["/f"]}}}}}]},
     ["_routing.routes[0].receiver.http_config.http_headers.match.files"]),
    # _routing_enforced carries `match` at its root: still label names
    ({"match": {"log_file": "x"}, "receiver": {"type": "webhook", "url": _URL}}, []),
])
def test_match_is_a_label_map_only_outside_a_receiver(block, want):
    from _grar_validate import routing_block_file_reference_errors
    got = [f for f, _m in routing_block_file_reference_errors(block, block="routing", where="w")]
    assert got == want


# ── 6. global.http_config.http_headers counts only with secrets / files ──

@pytest.mark.parametrize("headers,refused", [
    ({"X-Team": {"values": ["a"]}}, []),
    ({"X-Team": {"values": ["a"], "secrets": []}}, []),
    ({"X-Team": {"secrets": ["s"]}}, ["global.http_config.http_headers.X-Team"]),
    ({"X-Team": {"files": ["/f"]}}, ["global.http_config.http_headers.X-Team"]),
    ({"X-A": {"values": ["a"]}, "X-B": {"secrets": ["s"]}}, ["global.http_config.http_headers.X-B"]),
])
def test_base_global_http_headers(headers, refused):
    base = {"global": {"http_config": {"http_headers": headers}}}
    assert base_global_inherited_credentials(base) == refused


@pytest.mark.parametrize("glob", [
    {"http_config": {"tls_config": {"cert_file": "/c", "key_file": "/k"}}},
    {"http_config": {"tls_config": {"cert": "c", "key": "k"}}},
    {"smtp_tls_config": {"cert_file": "/c", "key_file": "/k"}},
])
def test_base_global_tls_client_key_is_not_refused(glob):
    """The private key only proves the certificate's identity in the mTLS
    handshake; it is never sent (see the comment on the rule)."""
    assert base_global_inherited_credentials({"global": glob}) == []
