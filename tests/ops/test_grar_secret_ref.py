"""#1560 ③-a：receiver 憑證欄位的 Secret 參照——render 與綁定檢查。

owner 裁決（2026-11-04）：參照不寫 Secret 名稱。

* `{secret: {key}}` 是所屬租戶自己的 Secret，名稱固定 `da-<tenant>`；
* `{secret: {shared, key}}` 的別名必須列在部署端 policy 的 `shared_secrets`，
  且該條目的 `fields` 要列出這個欄位；
* `auth_password` 不能用共用 Secret；用自己的也要 policy 有 `allowed_domains` 且
  smarthost 通過（密碼會送到租戶自己填的 smarthost）。

ConfigMap 路徑 render 成 `<field>_file: <root>/<secret>/<key>`、拿掉原欄位。檢查對每個
租戶「有效的」receiver 判（`_routing_defaults`／`overrides`／`routes`／`_routing_enforced`
合併、`{{tenant}}` 替換之後）。違反時該 receiver 不 render，是 `SkippedEntryWarning`：
`--validate` 非 0、validate-config 的 routes 列 FAIL。

租戶 id 都是 fixture 名稱（CLAUDE.md #9）。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.usefixtures("judging_run")

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "tools"))
sys.path.insert(0, str(REPO / "scripts" / "tools" / "ops"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate_alertmanager_routes as gar  # noqa: E402
import notification_tester as nt  # noqa: E402
import validate_config as vc  # noqa: E402
from _grar_merge import (  # noqa: E402
    DEFAULT_SECRET_MOUNT_ROOT, SecretBinding, SharedSecret, SkippedEntryWarning,
    build_receiver_config,
)
from _grar_validate import (  # noqa: E402
    PolicyInputError, load_policy_document, validate_receiver_domains,
)
from types import MappingProxyType  # noqa: E402

_GAR = REPO / "scripts" / "tools" / "ops" / "generate_alertmanager_routes.py"

OWN = {"secret": {"key": "slack-url"}}
CORP = {"secret": {"shared": "corp-slack", "key": "url"}}
BINDING = SecretBinding(DEFAULT_SECRET_MOUNT_ROOT, MappingProxyType({
    "corp-slack": SharedSecret("corp-alerting", frozenset({"api_url", "webhook_url", "url"})),
}))
SMTP = {"type": "email", "to": ["ops@example.com"], "from": "am@example.com",
        "smarthost": "smtp.example.com:587"}


def _slack(value) -> dict:
    return {"type": "slack", "api_url": value}


def _entry(cfg: dict) -> dict:
    (key, [entry]), = cfg.items()
    return entry


# ── render ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("receiver,field", [
    ({"type": "webhook", "url": OWN}, "url"),
    ({"type": "rocketchat", "url": OWN}, "url"),
    ({"type": "slack", "api_url": OWN}, "api_url"),
    ({"type": "teams", "webhook_url": OWN}, "webhook_url"),
    ({"type": "pagerduty", "routing_key": OWN}, "routing_key"),
    ({"type": "pagerduty", "service_key": OWN}, "service_key"),
])
def test_own_reference_renders_as_the_file_field(receiver, field):
    cfg, warnings = build_receiver_config(receiver, "t-a", owner_tenant="t-a")
    assert warnings == []
    entry = _entry(cfg)
    assert field not in entry
    assert entry[f"{field}_file"] == f"{DEFAULT_SECRET_MOUNT_ROOT}/da-t-a/slack-url"


def test_shared_reference_renders_the_policy_secret_name():
    cfg, warnings = build_receiver_config(_slack(CORP), "t-a", owner_tenant="t-a",
                                          secrets=BINDING)
    assert warnings == []
    assert _entry(cfg) == {"api_url_file": f"{DEFAULT_SECRET_MOUNT_ROOT}/corp-alerting/url"}


def test_mount_root_is_configurable():
    binding = SecretBinding("/run/secrets/", BINDING.shared_secrets)
    cfg, _ = build_receiver_config(_slack(OWN), "t-a", owner_tenant="t-a", secrets=binding)
    assert _entry(cfg) == {"api_url_file": "/run/secrets/da-t-a/slack-url"}


def test_auth_password_own_reference_under_allowed_domains_renders():
    cfg, warnings = build_receiver_config(
        {**SMTP, "auth_password": {"secret": {"key": "smtp-password"}}}, "t-a",
        owner_tenant="t-a", allowed_domains=["*.example.com"])
    assert warnings == []
    entry = _entry(cfg)
    assert "auth_password" not in entry
    assert entry["auth_password_file"] == f"{DEFAULT_SECRET_MOUNT_ROOT}/da-t-a/smtp-password"


def test_plain_values_are_unchanged():
    cfg, warnings = build_receiver_config(_slack("https://hooks.example.com/x"), "t-a",
                                          owner_tenant="t-a")
    assert warnings == [] and _entry(cfg) == {"api_url": "https://hooks.example.com/x"}


def test_a_reference_without_an_owner_argument_is_a_caller_bug():
    with pytest.raises(TypeError, match="owner_tenant"):
        build_receiver_config(_slack(OWN), "t-a")


# ── the binding rules: every violation drops the receiver ─────────────────

@pytest.mark.parametrize("receiver,kwargs,kind", [
    # 1. a shared alias the policy does not list (or no policy at all)
    (_slack({"secret": {"shared": "other", "key": "k"}}), {"secrets": BINDING},
     "secret_ref_shared_unknown"),
    (_slack(CORP), {}, "secret_ref_shared_unknown"),
    # 2. the entry does not list this field
    ({"type": "pagerduty", "routing_key": CORP}, {"secrets": BINDING},
     "secret_ref_shared_field_not_allowed"),
    # 3. auth_password with a shared Secret
    ({**SMTP, "auth_password": CORP},
     {"secrets": BINDING, "allowed_domains": ["*.example.com"]},
     "secret_ref_shared_auth_password"),
    # 4. auth_password with the own Secret, but no allowed_domains / smarthost outside
    ({**SMTP, "auth_password": OWN}, {}, "secret_ref_auth_password_unconfined"),
    ({**SMTP, "auth_password": OWN}, {"allowed_domains": []},
     "secret_ref_auth_password_unconfined"),
    ({**SMTP, "smarthost": "smtp.attacker.example:587", "auth_password": OWN},
     {"allowed_domains": ["*.example.com"]}, "secret_ref_auth_password_unconfined"),
])
def test_binding_violations_drop_the_receiver(receiver, kwargs, kind):
    cfg, warnings = build_receiver_config(receiver, "t-a", tenant_id="t-a",
                                          owner_tenant="t-a", **kwargs)
    assert cfg is None
    assert [w.kind for w in warnings] == [kind]
    assert all(isinstance(w, SkippedEntryWarning) for w in warnings)
    assert warnings[0].blocks == "validate" and warnings[0].tenant == "t-a"


def test_own_reference_with_no_owner_is_refused():
    cfg, warnings = build_receiver_config(_slack(OWN), "platform-enforced",
                                          owner_tenant=None, secrets=BINDING)
    assert cfg is None and [w.kind for w in warnings] == ["secret_ref_no_owner"]


@pytest.mark.parametrize("receiver", [
    _slack({"secret": {"name": "da-t-b", "key": "k"}}),       # 舊形狀：不收 name
    _slack({"secret": {"key": "../x"}}),
    {"type": "slack", "api_url": "https://hooks.example.com/x", "channel": OWN},
    {**SMTP, "smarthost": OWN},
    {**SMTP, "auth_password": {"key": "x"}},
])
def test_malformed_or_misplaced_references_are_invalid_fields(receiver):
    cfg, warnings = build_receiver_config(receiver, "t-a", owner_tenant="t-a",
                                          secrets=BINDING, allowed_domains=["*.example.com"])
    assert cfg is None and [w.kind for w in warnings] == ["invalid_receiver_field"]


# ── the domain allowlist: a dedicated branch, not "cannot parse host" ────

@pytest.mark.parametrize("receiver", [
    {"type": "webhook", "url": OWN},
    _slack(CORP),
    {"type": "teams", "webhook_url": OWN},
    {"type": "rocketchat", "url": CORP},
])
def test_domain_check_does_not_refuse_a_url_held_in_a_secret(receiver):
    assert validate_receiver_domains(receiver, "t-a", ["*.example.com"]) == []


def test_domain_check_still_refuses_a_plain_url_outside_the_list():
    w = validate_receiver_domains(_slack("https://hooks.attacker.example/x"), "t-a",
                                  ["*.example.com"])
    assert [x.kind for x in w] == ["receiver_domain_not_allowed"]


# ── the effective receiver: layers, {{tenant}}, every slot ───────────────

def _routing(receiver, **extra) -> dict:
    return {"receiver": receiver, **extra}


def test_defaults_receiver_binds_to_each_inheriting_tenant():
    defaults = {"receiver": _slack(OWN)}
    configs = {t: gar.merge_routing_with_defaults(defaults, {}, t) for t in ("t-a", "t-b")}
    _r, receivers, warnings = gar.generate_routes(configs, tenants=configs)
    by_name = {r["name"]: r for r in receivers}
    assert by_name["tenant-t-a"]["slack_configs"][0]["api_url_file"].endswith("/da-t-a/slack-url")
    assert by_name["tenant-t-b"]["slack_configs"][0]["api_url_file"].endswith("/da-t-b/slack-url")
    assert not [w for w in warnings if isinstance(w, SkippedEntryWarning)]


def test_tenant_placeholder_is_substituted_before_the_check():
    defaults = {"receiver": _slack({"secret": {"shared": "{{tenant}}-slack", "key": "url"}})}
    binding = SecretBinding(DEFAULT_SECRET_MOUNT_ROOT, MappingProxyType({
        "t-a-slack": SharedSecret("team-a-slack", frozenset({"api_url"}))}))
    configs = {t: gar.merge_routing_with_defaults(defaults, {}, t) for t in ("t-a", "t-b")}
    _r, receivers, warnings = gar.generate_routes(configs, tenants=configs, secrets=binding)
    assert [r["name"] for r in receivers] == ["tenant-t-a"]
    assert receivers[0]["slack_configs"][0]["api_url_file"].endswith("/team-a-slack/url")
    assert [(w.kind, w.tenant) for w in warnings if isinstance(w, SkippedEntryWarning)] == [
        ("secret_ref_shared_unknown", "t-b")]


def test_overrides_and_routes_are_bound_to_the_tenant():
    routing = _routing(
        _slack("https://hooks.example.com/main"),
        overrides=[{"alertname": "A", "receiver": _slack(OWN)}],
        routes=[{"match": {"severity": "critical"},
                 "receiver": {"type": "pagerduty", "routing_key": CORP}}])
    _r, receivers, warnings = gar.generate_routes({"t-a": routing}, tenants=["t-a"],
                                                  secrets=BINDING)
    names = [r["name"] for r in receivers]
    assert "tenant-t-a-override-0" in names and "tenant-t-a-route-0" not in names
    override = next(r for r in receivers if r["name"] == "tenant-t-a-override-0")
    assert override["slack_configs"][0]["api_url_file"].endswith("/da-t-a/slack-url")
    assert [(w.kind, w.field) for w in warnings if isinstance(w, SkippedEntryWarning)] == [
        ("secret_ref_shared_field_not_allowed", "routes[0].receiver.routing_key")]


def test_per_tenant_enforced_receiver_is_owned_by_the_tenant():
    enforced = {"receiver": {"type": "webhook", "url": {"secret": {"key": "noc-{{tenant}}"}}}}
    configs = {"t-a": _routing(_slack("https://hooks.example.com/a"))}
    _r, receivers, warnings = gar.generate_routes(configs, enforced_routing=enforced,
                                                  tenants=["t-a", "t-b"])
    by_name = {r["name"]: r for r in receivers}
    assert by_name["platform-enforced-t-a"]["webhook_configs"][0]["url_file"].endswith(
        "/da-t-a/noc-t-a")
    assert by_name["platform-enforced-t-b"]["webhook_configs"][0]["url_file"].endswith(
        "/da-t-b/noc-t-b")


def test_platform_wide_enforced_receiver_takes_only_a_shared_secret():
    configs = {"t-a": _routing(_slack("https://hooks.example.com/a"))}
    own = {"receiver": {"type": "webhook", "url": OWN}}
    _r, receivers, warnings = gar.generate_routes(configs, enforced_routing=own,
                                                  tenants=["t-a"], secrets=BINDING)
    assert "platform-enforced" not in [r["name"] for r in receivers]
    assert "secret_ref_no_owner" in [getattr(w, "kind", None) for w in warnings]
    shared = {"receiver": {"type": "webhook", "url": CORP}}
    _r, receivers, _w = gar.generate_routes(configs, enforced_routing=shared,
                                            tenants=["t-a"], secrets=BINDING)
    noc = next(r for r in receivers if r["name"] == "platform-enforced")
    assert noc["webhook_configs"][0]["url_file"].endswith("/corp-alerting/url")


# ── the deployment policy's shared_secrets ───────────────────────────────

def _policy(tmp_path: Path, body: str) -> str:
    p = tmp_path / "policy.yaml"
    p.write_text("allowed_domains: ['*.example.com']\n" + body, encoding="utf-8")
    return str(p)


def test_policy_reads_shared_secrets(tmp_path):
    doc = load_policy_document(_policy(tmp_path, (
        "shared_secrets:\n"
        "  corp-slack: {secret: corp-alerting, fields: [api_url, webhook_url, url]}\n")))
    assert dict(doc.shared_secrets) == {
        "corp-slack": SharedSecret("corp-alerting", frozenset({"api_url", "webhook_url", "url"}))}
    assert dict(load_policy_document(_policy(tmp_path, "")).shared_secrets) == {}


@pytest.mark.parametrize("body,needle", [
    ("shared_secrets: [a]\n", "expected a mapping"),
    ("shared_secrets:\n  Corp: {secret: corp, fields: [url]}\n", "DNS-1123 label"),
    ("shared_secrets:\n  corp: {secret: corp}\n", "must be exactly"),
    ("shared_secrets:\n  corp: {secret: corp, fields: [url], extra: 1}\n", "must be exactly"),
    ("shared_secrets:\n  corp: {secret: da-shared, fields: [url]}\n", "starts with 'da-'"),
    ("shared_secrets:\n  corp: {secret: Corp_Secret, fields: [url]}\n", "Secret name"),
    ("shared_secrets:\n  corp: {secret: corp, fields: url}\n", "non-empty list"),
    ("shared_secrets:\n  corp: {secret: corp, fields: []}\n", "non-empty list"),
    ("shared_secrets:\n  corp: {secret: corp, fields: [auth_password]}\n",
     "auth_password never takes a shared Secret"),
    ("shared_secrets:\n  corp: {secret: corp, fields: [channel]}\n", "may use a shared Secret"),
])
def test_unusable_shared_secrets_make_the_policy_unusable(tmp_path, body, needle):
    with pytest.raises(PolicyInputError, match=needle):
        load_policy_document(_policy(tmp_path, body))


# ── the two entry points: --validate and validate-config's routes row ────

def _tree(tmp_path: Path, receiver: str) -> Path:
    d = tmp_path / "conf.d"
    d.mkdir()
    (d / "_defaults.yaml").write_text("defaults:\n  cpu_usage_percent: 80\n", encoding="utf-8")
    (d / "t-a.yaml").write_text(
        "tenants:\n  t-a:\n    cpu_usage_percent: '85'\n    _routing:\n"
        f"      receiver:\n{receiver}", encoding="utf-8")
    return d


_SHARED_RECEIVER = ("        type: slack\n"
                    "        api_url: {secret: {shared: corp-slack, key: url}}\n")
_SHARED_POLICY = ("shared_secrets:\n"
                  "  corp-slack: {secret: corp-alerting, fields: [api_url]}\n")


def _run(d: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(_GAR), "--config-dir", str(d), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=300)


def test_render_writes_the_file_field(tmp_path):
    d = _tree(tmp_path, _SHARED_RECEIVER)
    out = tmp_path / "routes.yaml"
    r = _run(d, "--policy", _policy(tmp_path, _SHARED_POLICY),
             "--secret-mount-root", "/mnt/s", "-o", str(out))
    assert r.returncode == 0, r.stderr
    receivers = yaml.safe_load(out.read_text(encoding="utf-8"))["receivers"]
    main = next(x for x in receivers if x["name"] == "tenant-t-a")
    assert main["slack_configs"] == [{"api_url_file": "/mnt/s/corp-alerting/url"}]


@pytest.mark.parametrize("root", ["relative/path", "/a/../b", "/a\nb"])
def test_unusable_mount_root_is_a_caller_error(tmp_path, root):
    r = _run(_tree(tmp_path, _SHARED_RECEIVER), "--secret-mount-root", root, "--dry-run")
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "--secret-mount-root" in r.stderr


def test_validate_fails_on_a_binding_violation(tmp_path):
    d = _tree(tmp_path, _SHARED_RECEIVER)
    r = _run(d, "--validate", "--policy", _policy(tmp_path, ""))
    assert r.returncode != 0
    assert "shared_secrets does not list" in r.stdout + r.stderr


def test_validate_config_routes_row_fails_on_a_binding_violation(tmp_path):
    d = _tree(tmp_path, _SHARED_RECEIVER)
    row = vc.check_routes(str(d), _policy(tmp_path, ""))
    assert row["status"] == vc.FAIL, row
    row = vc.check_routes(str(d), _policy(tmp_path, _SHARED_POLICY))
    assert row["status"] != vc.FAIL, row


def test_unusable_shared_secrets_policy_is_rc_2(tmp_path):
    r = _run(_tree(tmp_path, _SHARED_RECEIVER), "--dry-run", "--policy", _policy(
        tmp_path, "shared_secrets:\n  corp-slack: {secret: da-x, fields: [api_url]}\n"))
    assert r.returncode == 2 and "da-" in r.stderr


# ── test-notification: a credential in a Secret is never sent ────────────

def test_notification_tester_skips_a_receiver_whose_credential_is_in_a_secret():
    report = nt.test_tenant_receivers("t-a", {"_routing": _routing(_slack(CORP))},
                                      rate_limit=0, secrets=BINDING)
    [result] = report.receivers
    assert result.status == nt.STATUS_SKIPPED, result
    assert "Secret reference" in result.detail
    report = nt.test_tenant_receivers("t-a", {"_routing": _routing(_slack(CORP))},
                                      rate_limit=0)
    assert report.receivers[0].status != nt.STATUS_SKIPPED


# ── redaction: a reference is masked whole, like the value it replaces ───

def test_config_diff_redacts_a_reference_by_its_field_name():
    """`_lib_redact` 與 tenant-api credmask 同一條規則：依 key 名、整包遮，不看值的形狀
    （參照只是座標，但 key 的文字也可能是誤貼的憑證；fail-closed）。"""
    import _lib_redact
    assert _lib_redact.redact({"receiver": _slack(CORP)}) == {
        "receiver": {"type": "slack", "api_url": _lib_redact.REDACTED}}


# ── F1：參照不得殘留在 Alertmanager 輸出（http_config 任一層、email headers）──
# 產生器把 http_config / headers 整包照抄；render 之後若還有帶 `secret` 鍵的 mapping，
# 就會原樣寫進 Alertmanager 設定。檢查在輸出上做，所以不分欄位、不分層。

_HOOK = {"type": "webhook", "url": "https://hook.example.com/a"}
_OAUTH = {"client_id": "id", "token_url": "https://token.example.com/t"}


@pytest.mark.parametrize("receiver,path", [
    ({**_HOOK, "http_config": OWN}, "receiver.http_config"),
    ({**_HOOK, "http_config": {"oauth2": {**_OAUTH, "claims": {"sub": OWN}}}},
     "receiver.http_config.oauth2.claims.sub"),
    # 多層巢狀：mapping 裡的 list 裡的 mapping
    ({**_HOOK, "http_config": {"oauth2": {**_OAUTH, "claims": {
        "a": [{"b": {"c": [1, CORP]}}]}}}},
     "receiver.http_config.oauth2.claims.a[0].b.c[1]"),
    # 形狀壞掉的參照也算（is_secret_ref 看 `secret` 鍵的值是不是 mapping）
    ({**_HOOK, "http_config": {"oauth2": {**_OAUTH, "claims": {
        "x": {"secret": {"name": "da-t-b", "key": "k"}}}}}},
     "receiver.http_config.oauth2.claims.x"),
    ({**SMTP, "headers": {"X-Token": OWN}}, "receiver.headers.X-Token"),
    ({**SMTP, "headers": OWN}, "receiver.headers"),
])
def test_a_reference_left_in_the_output_drops_the_receiver(receiver, path):
    cfg, warnings = build_receiver_config(receiver, "t-a", tenant_id="t-a",
                                          owner_tenant="t-a", secrets=BINDING)
    assert cfg is None
    assert [(w.kind, w.field, w.tenant) for w in warnings] == [
        ("secret_ref_unsupported_field", path, "t-a")]
    assert warnings[0].blocks == "validate"


def test_every_leftover_reference_is_named():
    receiver = {**_HOOK, "http_config": {"oauth2": {**_OAUTH, "claims": {"a": OWN, "b": CORP}}}}
    cfg, warnings = build_receiver_config(receiver, "t-a", owner_tenant="t-a")
    assert cfg is None
    assert sorted(w.field for w in warnings) == [
        "receiver.http_config.oauth2.claims.a", "receiver.http_config.oauth2.claims.b"]


def test_a_rendered_reference_is_not_a_leftover():
    """欄位本身接參照時 render 成 `<field>_file`，不算殘留；其他 http_config 照抄。"""
    receiver = {"type": "webhook", "url": OWN,
                "http_config": {"oauth2": {**_OAUTH, "claims": {"sub": "x"}}}}
    cfg, warnings = build_receiver_config(receiver, "t-a", owner_tenant="t-a")
    assert warnings == []
    assert _entry(cfg) == {"url_file": f"{DEFAULT_SECRET_MOUNT_ROOT}/da-t-a/slack-url",
                           "http_config": receiver["http_config"]}


def test_leftover_reference_in_the_main_slot_is_refused_through_generate_routes():
    bad = {**SMTP, "headers": {"X-Token": OWN}}
    _r, receivers, warnings = gar.generate_routes({"t-a": _routing(bad)}, tenants=["t-a"],
                                                  secrets=BINDING)
    assert receivers == []
    got = [(w.kind, w.field) for w in warnings if isinstance(w, SkippedEntryWarning)]
    assert ("secret_ref_unsupported_field", "receiver.headers.X-Token") in got


def test_leftover_reference_in_each_sub_slot_is_refused_through_generate_routes():
    # The main receiver is valid, so overrides and routes each reach
    # build_receiver_config themselves: a refused main would turn them into
    # parent_receiver_not_rendered and prove nothing about their own scan.
    good = {"type": "slack", "api_url": "https://hooks.slack.com/services/T0/B0/x"}
    bad = {**SMTP, "headers": {"X-Token": OWN}}
    routing = _routing(good, overrides=[{"alertname": "A", "receiver": bad}],
                       routes=[{"match": {"severity": "critical"}, "receiver": bad}])
    _r, receivers, warnings = gar.generate_routes({"t-a": routing}, tenants=["t-a"],
                                                  secrets=BINDING)
    got = {(w.kind, w.field) for w in warnings if isinstance(w, SkippedEntryWarning)}
    assert ("secret_ref_unsupported_field", "overrides[0].receiver.headers.X-Token") in got
    assert ("secret_ref_unsupported_field", "routes[0].receiver.headers.X-Token") in got
    assert [r["name"] for r in receivers] == ["tenant-t-a"]


@pytest.mark.parametrize("receiver", [
    {**SMTP, "headers": {"secret": "v"}},
    {**_HOOK, "http_config": {"oauth2": {**_OAUTH, "endpoint_params": {"secret": "v"}}}},
    {**_HOOK, "http_config": {"oauth2": {**_OAUTH, "claims": {"secret": None}}}},
])
def test_a_key_named_secret_holding_a_scalar_is_not_a_reference(receiver):
    cfg, warnings = build_receiver_config(receiver, "t-a", tenant_id="t-a",
                                          owner_tenant="t-a", secrets=BINDING)
    assert cfg is not None, warnings
    assert not [w for w in warnings
                if getattr(w, "kind", "").startswith("secret_ref_")], warnings


def test_an_http_header_named_secret_is_refused_as_a_known_limit():
    # http_headers values are mappings ({values: [...]}), so a header named
    # `secret` has the shape of a reference and is refused like one. Telling
    # the two apart would take a second rule on the inner keys; a header with
    # that name is rare enough to stay refused (documented in the schema).
    receiver = {**_HOOK, "http_config": {"http_headers": {"secret": {"values": ["v"]}}}}
    cfg, warnings = build_receiver_config(receiver, "t-a", tenant_id="t-a",
                                          owner_tenant="t-a", secrets=BINDING)
    assert cfg is None
    assert ("secret_ref_unsupported_field", "receiver.http_config.http_headers") in {
        (w.kind, w.field) for w in warnings if isinstance(w, SkippedEntryWarning)}


def test_a_nested_malformed_reference_is_named_once():
    from _grar_merge import secret_ref_paths
    value = {"a": {"secret": {"secret": {"key": "k"}}}}
    assert secret_ref_paths(value, "r") == ["r.a"]


def test_a_scalar_secret_mapping_in_a_credential_field_is_still_refused():
    cfg, warnings = build_receiver_config(
        {"type": "slack", "api_url": {"secret": "https://hooks.slack.com/x"}}, "t-a",
        tenant_id="t-a", owner_tenant="t-a", secrets=BINDING)
    assert cfg is None, warnings


def test_schema_refuses_a_reference_inside_http_config_at_any_depth():
    import jsonschema
    with open(REPO / "docs" / "schemas" / "tenant-config.schema.json", encoding="utf-8") as f:
        v = jsonschema.Draft7Validator(yaml.safe_load(f))

    def doc(receiver):
        return {"tenants": {"t-a": {"_routing": {"receiver": receiver}}}}

    for plain in (
        {**_HOOK, "http_config": {"oauth2": {**_OAUTH, "claims": {"a": [{"b": "x"}]}}}},
        {**_HOOK, "http_config": {"oauth2": {**_OAUTH, "claims": {"secret": "x"}}}},
    ):
        assert list(v.iter_errors(doc(plain))) == [], plain
    for receiver in (
        {**_HOOK, "http_config": {"oauth2": {**_OAUTH, "claims": {"a": OWN}}}},
        {**_HOOK, "http_config": {"oauth2": {**_OAUTH, "claims": {"a": [{"b": CORP}]}}}},
        {**_HOOK, "http_config": OWN},
        {**SMTP, "headers": {"X-Token": OWN}},
    ):
        assert list(v.iter_errors(doc(receiver))) != [], receiver


# ── F2：用了共用 Secret 的 receiver 不收租戶自填的 proxy_url ──────────────

def _shared_hook(**http_config) -> dict:
    return {"type": "webhook", "url": CORP, "http_config": http_config}


def test_shared_reference_with_a_proxy_url_is_refused():
    cfg, warnings = build_receiver_config(
        _shared_hook(proxy_url="http://proxy.attacker.example:3128"), "t-a",
        tenant_id="t-a", owner_tenant="t-a", secrets=BINDING)
    assert cfg is None
    assert [(w.kind, w.field) for w in warnings] == [
        ("secret_ref_shared_proxy_url", "receiver.http_config.proxy_url")]
    assert "corp-slack" in warnings[0]


@pytest.mark.parametrize("receiver", [
    _shared_hook(proxy_url=""),                      # 空值視同沒設
    _shared_hook(proxy_url=None),
    _shared_hook(proxy_from_environment=True),       # Alertmanager 自己的環境
    {"type": "webhook", "url": OWN,                  # 自己的 Secret：代理是自己的
     "http_config": {"proxy_url": "http://proxy.example.com:3128"}},
])
def test_proxy_rule_admits_what_does_not_leak_a_shared_destination(receiver):
    cfg, warnings = build_receiver_config(receiver, "t-a", owner_tenant="t-a",
                                          secrets=BINDING)
    assert warnings == [] and cfg is not None


def test_shared_proxy_rule_holds_through_generate_routes():
    routing = _routing(_slack("https://hooks.example.com/main"), overrides=[
        {"alertname": "A", "receiver": _shared_hook(proxy_url="http://p.example.com:3128")}])
    _r, receivers, warnings = gar.generate_routes({"t-a": routing}, tenants=["t-a"],
                                                  secrets=BINDING)
    assert "tenant-t-a-override-0" not in [r["name"] for r in receivers]
    assert [(w.kind, w.field) for w in warnings if isinstance(w, SkippedEntryWarning)] == [
        ("secret_ref_shared_proxy_url", "overrides[0].receiver.http_config.proxy_url")]


# ── F3：routes[i] 的參照歸該租戶 ─────────────────────────────────────────

def test_routes_receiver_own_reference_renders_the_tenants_path():
    routing = _routing(_slack("https://hooks.example.com/main"), routes=[
        {"match": {"severity": "critical"},
         "receiver": {"type": "pagerduty", "routing_key": {"secret": {"key": "pd-key"}}}}])
    configs = {"t-a": routing, "t-b": routing}
    _r, receivers, warnings = gar.generate_routes(configs, tenants=list(configs),
                                                  secrets=BINDING)
    assert not [w for w in warnings if isinstance(w, SkippedEntryWarning)]
    by_name = {r["name"]: r for r in receivers}
    for tenant in ("t-a", "t-b"):
        route = by_name[f"tenant-{tenant}-route-0"]
        assert route["pagerduty_configs"] == [
            {"routing_key_file": f"{DEFAULT_SECRET_MOUNT_ROOT}/da-{tenant}/pd-key"}]


# ── F4：經 generate-routes 的正向路徑 ─────────────────────────────────────

def test_override_with_a_shared_secret_renders_through_generate_routes():
    routing = _routing(_slack("https://hooks.example.com/main"),
                       overrides=[{"alertname": "A", "receiver": _slack(CORP)}])
    _r, receivers, warnings = gar.generate_routes({"t-a": routing}, tenants=["t-a"],
                                                  secrets=BINDING)
    assert not [w for w in warnings if isinstance(w, SkippedEntryWarning)]
    override = next(r for r in receivers if r["name"] == "tenant-t-a-override-0")
    assert override["slack_configs"] == [
        {"api_url_file": f"{DEFAULT_SECRET_MOUNT_ROOT}/corp-alerting/url"}]


_PASSWORD = {**SMTP, "auth_password": {"secret": {"key": "smtp-password"}}}


def test_auth_password_own_reference_renders_in_every_tenant_slot():
    """policy 有 allowed_domains、smarthost 通過：main／override／routes／帶 {{tenant}} 的
    enforced 都放行，並 render 成該租戶的路徑。"""
    routing = _routing(_PASSWORD, overrides=[{"alertname": "A", "receiver": _PASSWORD}],
                       routes=[{"match": {"severity": "critical"}, "receiver": _PASSWORD}])
    enforced = {"receiver": {**_PASSWORD, "to": ["noc+{{tenant}}@example.com"]}}
    _r, receivers, warnings = gar.generate_routes(
        {"t-a": routing}, ["*.example.com"], enforced, tenants=["t-a"], secrets=BINDING)
    assert not [w for w in warnings if isinstance(w, SkippedEntryWarning)]
    by_name = {r["name"]: r for r in receivers}
    want = f"{DEFAULT_SECRET_MOUNT_ROOT}/da-t-a/smtp-password"
    for name in ("tenant-t-a", "tenant-t-a-override-0", "tenant-t-a-route-0",
                 "platform-enforced-t-a"):
        [entry] = by_name[name]["email_configs"]
        assert entry["auth_password_file"] == want and "auth_password" not in entry, name


def _explain_parsed(routing: dict) -> dict:
    return {"routing_defaults": {}, "routing_profiles": {}, "tenant_profile_refs": {},
            "explicit_routing": {"t-a": routing}, "enforced_routing": None,
            "all_tenants": ["t-a"], "dedup_configs": {}, "domain_policies": {},
            "disabled_tenants": set()}


def test_explain_route_reads_the_policys_shared_secrets(da_crdecode_env):
    """explain-route 帶 secrets 時，與產生器同樣認得共用 Secret：主 receiver 用共用 Secret
    而放行，其下 override 的網域拒收才看得到；不帶就整條 tenant route 不產出。"""
    import explain_route as er
    routing = _routing(_slack(CORP), overrides=[
        {"alertname": "A", "receiver": _slack("https://hooks.attacker.example/x")},
        {"alertname": "B", "receiver": _slack(CORP)}])
    parsed = _explain_parsed(routing)
    allowed = ["*.example.com"]

    explanation = er.explain_tenant_routing(parsed, "t-a", allowed, BINDING)
    assert [x["source"] for x in explanation["policy_refused"]] == ["overrides[0]"]
    assert len(explanation["sub_routes"]) == 1
    trace = er.trace_alert_routing(parsed, "t-a", "A", allowed_domains=allowed,
                                   secrets=BINDING)
    assert [x["source"] for x in trace["policy_refused"]] == ["overrides[0]"]

    unbound = er.explain_tenant_routing(parsed, "t-a", allowed, None)
    assert unbound["policy_refused"] == [] and unbound["sub_routes"] == []
