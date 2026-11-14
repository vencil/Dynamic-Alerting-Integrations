"""#1560 ③-a：receiver 憑證欄位的 Secret 參照——render 與綁定檢查。

owner 裁決（2026-11-04）：參照不寫 Secret 名稱。

* `{secret: {key}}` 是所屬租戶自己的 Secret，名稱固定 `da-<tenant>`；
* `{secret: {shared, key}}` 的別名必須列在部署端 policy 的 `shared_secrets`，
  且該條目的 `fields` 要列出這個欄位的完整路徑（⑤-b：`api_url`、
  `http_config.basic_auth.password`）；
* `auth_password` 與 `http_config` 各列：租戶擁有的 receiver 不能用共用 Secret；用自己的
  也要 policy 有 `allowed_domains` 且目的地通過（密碼會送到租戶自己填的 smarthost）。
  沒有所屬租戶的 `_routing_enforced` 反過來只能用共用 Secret，同樣受 allowlist、
  `follow_redirects: false`、不設 proxy 約束（⑤-b，2-c 推廣）。

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
    DEFAULT_MOUNT_ROOT, SecretBinding, SharedSecret, SkippedEntryWarning,
    build_receiver_config,
)
from _grar_validate import (  # noqa: E402
    PolicyInputError, load_policy_document, validate_receiver_domains,
)
from types import MappingProxyType  # noqa: E402

_GAR = REPO / "scripts" / "tools" / "ops" / "generate_alertmanager_routes.py"

OWN = {"secret": {"key": "slack-url"}}
CORP = {"secret": {"shared": "corp-slack", "key": "url"}}
BINDING = SecretBinding(DEFAULT_MOUNT_ROOT, MappingProxyType({
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
    assert entry[f"{field}_file"] == f"{DEFAULT_MOUNT_ROOT}/da-t-a/slack-url"


def test_shared_reference_renders_the_policy_secret_name():
    cfg, warnings = build_receiver_config(_slack(CORP), "t-a", owner_tenant="t-a",
                                          secrets=BINDING)
    assert warnings == []
    assert _entry(cfg) == {"api_url_file": f"{DEFAULT_MOUNT_ROOT}/corp-alerting/url"}


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
    assert entry["auth_password_file"] == f"{DEFAULT_MOUNT_ROOT}/da-t-a/smtp-password"


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
     "secret_ref_shared_confined"),
    # 4. auth_password with the own Secret, but no allowed_domains / smarthost outside
    ({**SMTP, "auth_password": OWN}, {}, "secret_ref_destination_unconfined"),
    ({**SMTP, "auth_password": OWN}, {"allowed_domains": []},
     "secret_ref_destination_unconfined"),
    ({**SMTP, "smarthost": "smtp.attacker.example:587", "auth_password": OWN},
     {"allowed_domains": ["*.example.com"]}, "secret_ref_destination_unconfined"),
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
    binding = SecretBinding(DEFAULT_MOUNT_ROOT, MappingProxyType({
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
    # #1560 ⑤-b: a field is a full row path; the top-level `http_config`
    # (which opened eight rows at once) names none, and the message names
    # the paths it may have meant.
    ("shared_secrets:\n  corp: {secret: corp, fields: [http_config]}\n",
     r"a field is a full path now; under \['http_config'\] the paths are .*"
     r"http_config\.basic_auth\.password"),
    ("shared_secrets:\n  corp: {secret: corp, fields: [basic_auth.password]}\n",
     "may use a shared Secret"),
    ("shared_secrets:\n  corp: {secret: corp, fields: ['http_config.http_headers.X-Token.secrets']}\n",
     "may use a shared Secret"),
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
    # #1211 gap 2: a mode is required; the merged output carries the receiver.
    r = _run(d, "--output-alertmanager", "--policy", _policy(tmp_path, _SHARED_POLICY),
             "--secret-mount-root", "/mnt/s", "-o", str(out))
    assert r.returncode == 0, r.stderr
    receivers = yaml.safe_load(out.read_text(encoding="utf-8"))["receivers"]
    main = next(x for x in receivers if x["name"] == "tenant-t-a")
    assert main["slack_configs"] == [{"api_url_file": "/mnt/s/corp-alerting/url"}]


@pytest.mark.parametrize("root", ["relative/path", "/a/../b", "/a\nb"])
def test_unusable_mount_root_is_a_caller_error(tmp_path, root):
    r = _run(_tree(tmp_path, _SHARED_RECEIVER), "--output-alertmanager",
             "--secret-mount-root", root, "--dry-run")
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "--secret-mount-root" in r.stderr


def test_validate_fails_on_a_binding_violation(tmp_path):
    d = _tree(tmp_path, _SHARED_RECEIVER)
    r = _run(d, "--validate", "--policy", _policy(tmp_path, ""))
    assert r.returncode != 0
    assert "shared_secrets does not list" in r.stdout + r.stderr


def vc_gen():
    import generate_alertmanager_routes as gen
    return gen


def test_validate_config_routes_row_fails_on_a_binding_violation(tmp_path):
    d = _tree(tmp_path, _SHARED_RECEIVER)
    row = vc.check_routes(str(d), vc_gen().resolve_policy_file(_policy(tmp_path, "")))
    assert row["status"] == vc.FAIL, row
    row = vc.check_routes(str(d), vc_gen().resolve_policy_file(_policy(tmp_path, _SHARED_POLICY)))
    assert row["status"] != vc.FAIL, row


def test_unusable_shared_secrets_policy_is_rc_2(tmp_path):
    r = _run(_tree(tmp_path, _SHARED_RECEIVER), "--output-alertmanager", "--dry-run", "--policy", _policy(
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
    assert _entry(cfg) == {"url_file": f"{DEFAULT_MOUNT_ROOT}/da-t-a/slack-url",
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
        ("secret_ref_proxy_url", "receiver.http_config.proxy_url")]
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
        ("secret_ref_proxy_url", "overrides[0].receiver.http_config.proxy_url")]


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
            {"routing_key_file": f"{DEFAULT_MOUNT_ROOT}/da-{tenant}/pd-key"}]


# ── F4：經 generate-routes 的正向路徑 ─────────────────────────────────────

def test_override_with_a_shared_secret_renders_through_generate_routes():
    routing = _routing(_slack("https://hooks.example.com/main"),
                       overrides=[{"alertname": "A", "receiver": _slack(CORP)}])
    _r, receivers, warnings = gar.generate_routes({"t-a": routing}, tenants=["t-a"],
                                                  secrets=BINDING)
    assert not [w for w in warnings if isinstance(w, SkippedEntryWarning)]
    override = next(r for r in receivers if r["name"] == "tenant-t-a-override-0")
    assert override["slack_configs"] == [
        {"api_url_file": f"{DEFAULT_MOUNT_ROOT}/corp-alerting/url"}]


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
    want = f"{DEFAULT_MOUNT_ROOT}/da-t-a/smtp-password"
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


@pytest.mark.parametrize("receiver", [None, "slack", ["type", "slack"],
                                      {"type": "carrier-pigeon", "url": OWN}, {"url": OWN}])
def test_a_receiver_that_is_not_a_known_mapping_has_no_secret_references(receiver):
    # A malformed receiver is refused for its shape elsewhere; it must not be
    # read as carrying references (nor crash the reference lookup).
    from _grar_merge import _receiver_secret_refs
    assert _receiver_secret_refs(receiver) == []


# ── ③-c：http_config 的認證子欄位（#1560 owner 裁決第 2-a 列）────────────────
# 可收參照的位置、render 成的 key、送達目的地都取自 CREDENTIAL_REF_SITES。
# 規則與 auth_password 同一條：只收租戶自己的 Secret；policy 要有 allowed_domains，
# 且目的地（url；oauth2 三項是 oauth2.token_url）明文並通過。

from _lib_validation import CREDENTIAL_REF_SITES  # noqa: E402

_NESTED = [s for s in CREDENTIAL_REF_SITES if len(s.path) > 1]
_ALLOWED = ["*.example.com"]
_TOKEN_URL = "https://token.example.com/t"


def _ref_at(path: tuple, value, **http_config) -> dict:
    """A webhook receiver (url on the allowlist, follow_redirects: false)
    with *value* at site *path* ("*" → header X-Token, "[]" → a one-item
    list), plus what that path needs (oauth2 client_id / token_url, a
    client cert for a client key) and the extra http_config keys given."""
    receiver = {"type": "webhook", "url": "https://hook.example.com/a",
                "http_config": {"follow_redirects": False, **http_config}}
    node = receiver
    for i, step in enumerate(path):
        key = "X-Token" if step == "*" else step
        rest = path[i + 1:]
        if rest == ("[]",):
            node[key] = [value]
            break
        if not rest:
            node[key] = value
            break
        node = node.setdefault(key, {})
        if key == "oauth2":
            node.setdefault("client_id", "c")
            node.setdefault("token_url", _TOKEN_URL)
        if key == "tls_config" and rest == ("key",):
            node.setdefault("cert", "client-cert-pem")
    return receiver


def _at(value, path: tuple):
    for step in path:
        value = value[step]
    return value


def _build(receiver, **kwargs):
    kwargs.setdefault("owner_tenant", "t-a")
    kwargs.setdefault("allowed_domains", _ALLOWED)
    return build_receiver_config(receiver, "t-a", tenant_id="t-a", **kwargs)


@pytest.mark.parametrize("site", _NESTED, ids=lambda s: ".".join(s.path))
def test_each_http_config_row_renders_its_file_key(site):
    receiver = _ref_at(site.path, {"secret": {"key": "cred"}})
    cfg, warnings = _build(receiver)
    assert warnings == [], warnings
    entry = _entry(cfg)
    want = f"{DEFAULT_MOUNT_ROOT}/da-t-a/cred"
    if site.path[-1] == "[]":
        header = _at(entry, ("http_config", "http_headers", "X-Token"))
        assert header == {"files": [want]}           # secrets 只剩參照 → 整個拿掉
    else:
        holder = _at(entry, site.path[:-1])
        assert site.path[-1] not in holder
        assert holder[site.file_key] == want
        if site.path[-2:] == ("tls_config", "key"):
            # 同層的 cert 原樣保留（#3616 review）
            assert holder == {"cert": "client-cert-pem", "key_file": want}
    from _grar_merge import secret_ref_paths
    assert secret_ref_paths(entry, "receiver") == []


def test_header_secrets_keep_plain_values_and_move_references_in_order():
    receiver = _ref_at(("http_config", "http_headers", "*", "secrets", "[]"), "x")
    receiver["http_config"]["http_headers"] = {
        "X-A": {"values": ["v"], "secrets": ["lit-1", {"secret": {"key": "h1"}}, "lit-2",
                                             {"secret": {"key": "h2"}}]},
        "X-B": {"values": ["only-values"]},
    }
    cfg, warnings = _build(receiver)
    assert warnings == []
    headers = _entry(cfg)["http_config"]["http_headers"]
    root = f"{DEFAULT_MOUNT_ROOT}/da-t-a"
    assert headers == {"X-A": {"values": ["v"], "secrets": ["lit-1", "lit-2"],
                               "files": [f"{root}/h1", f"{root}/h2"]},
                       "X-B": {"values": ["only-values"]}}


def test_rendering_does_not_change_the_tenants_receiver():
    receiver = _ref_at(("http_config", "basic_auth", "password"), {"secret": {"key": "pw"}})
    import copy
    before = copy.deepcopy(receiver)
    cfg, _w = _build(receiver)
    assert cfg is not None and receiver == before


def _kinds(receiver, **kwargs):
    cfg, warnings = _build(receiver, **kwargs)
    assert cfg is None, cfg
    assert all(isinstance(w, SkippedEntryWarning) and w.blocks == "validate" for w in warnings)
    return [(w.kind, w.field) for w in warnings]


_PW = ("http_config", "basic_auth", "password")
_CS = ("http_config", "oauth2", "client_secret")


@pytest.mark.parametrize("site", _NESTED, ids=lambda s: ".".join(s.path))
def test_a_shared_secret_is_never_taken_on_an_http_config_row(site):
    receiver = _ref_at(site.path, {"secret": {"shared": "corp-slack", "key": "k"}})
    got = _kinds(receiver, secrets=BINDING)
    assert ("secret_ref_shared_confined",
            "receiver." + ".".join(str(p) for p in site.path).replace(".[]", "[0]")
            .replace(".*", ".X-Token")) in got, got


@pytest.mark.parametrize("site", _NESTED, ids=lambda s: ".".join(s.path))
@pytest.mark.parametrize("allowed", [None, []])
def test_no_allowed_domains_refuses_every_http_config_row(site, allowed):
    got = _kinds(_ref_at(site.path, {"secret": {"key": "k"}}), allowed_domains=allowed)
    assert [k for k, _f in got] == ["secret_ref_destination_unconfined"]


@pytest.mark.parametrize("path,dest_edit", [
    # url（webhook 的 url）：不在 allowlist、本身是參照
    (_PW, {"url": "https://hook.attacker.example/a"}),
    (_PW, {"url": {"secret": {"key": "url"}}}),
    # oauth2.token_url：不在 allowlist、缺漏、是參照（schema 只收字串，產生器照樣擋）
    (_CS, {"token_url": "https://token.attacker.example/t"}),
    (_CS, {"token_url": None}),
    (_CS, {"token_url": {"secret": {"key": "tok"}}}),
])
def test_a_destination_outside_or_unreadable_refuses_the_reference(path, dest_edit):
    receiver = _ref_at(path, {"secret": {"key": "k"}})
    if "url" in dest_edit:
        receiver["url"] = dest_edit["url"]
    else:
        receiver["http_config"]["oauth2"]["token_url"] = dest_edit["token_url"]
    got = _kinds(receiver)
    assert ("secret_ref_destination_unconfined", "receiver." + ".".join(path)) in got, got


_OAUTH2_ROWS = [s.path for s in _NESTED if s.path[:2] == ("http_config", "oauth2")]


@pytest.mark.parametrize("path", _OAUTH2_ROWS, ids=".".join)
@pytest.mark.parametrize("url,token_url", [
    # 換出的 access token 隨每個送往 url 的請求出去：url 也是目的地
    ("https://hook.attacker.example/a", _TOKEN_URL),
    ({"secret": {"key": "url"}}, _TOKEN_URL),
    ("https://hook.example.com/a", "https://token.attacker.example/t"),
])
def test_oauth2_rows_are_confined_by_token_url_and_by_url(path, url, token_url):
    """oauth2 的憑證送往 token_url，換到的 access token 送往 url：兩者都要明文並通過
    allowlist，任一不過就拒。"""
    receiver = _ref_at(path, {"secret": {"key": "k"}})
    receiver["url"] = url
    receiver["http_config"]["oauth2"]["token_url"] = token_url
    assert ("secret_ref_destination_unconfined", "receiver." + ".".join(path)) in _kinds(receiver)


@pytest.mark.parametrize("allowed", [None, []])
def test_no_allowed_domains_is_said_once_naming_every_destination(allowed):
    cfg, warnings = _build(_ref_at(_CS, {"secret": {"key": "k"}}), allowed_domains=allowed)
    assert cfg is None and [w.kind for w in warnings] == ["secret_ref_destination_unconfined"]
    assert warnings[0].count("has no allowed_domains") == 1, warnings[0]
    assert "http_config.oauth2.token_url, url" in warnings[0], warnings[0]


@pytest.mark.parametrize("value", [None, "", 5])
def test_a_missing_destination_is_named_as_not_set(value):
    """盲審 F3：目的地沒寫、空字串或不是字串時，訊息說「沒設或不是字串」，不是「host None 不在 allowlist」。"""
    receiver = _ref_at(_CS, {"secret": {"key": "k"}})
    receiver["http_config"]["oauth2"]["token_url"] = value
    cfg, warnings = _build(receiver)
    assert cfg is None and [w.kind for w in warnings] == ["secret_ref_destination_unconfined"]
    assert "is missing or not a non-empty string" in warnings[0], warnings[0]


def test_oauth2_rows_pass_when_both_destinations_pass():
    cfg, warnings = _build(_ref_at(_CS, {"secret": {"key": "k"}}))
    assert warnings == [] and cfg is not None


def test_an_http_config_reference_with_no_owner_is_refused():
    got = _kinds(_ref_at(_PW, {"secret": {"key": "k"}}), owner_tenant=None)
    assert got == [("secret_ref_no_owner", "receiver.http_config.basic_auth.password")]


_ALL_TOP = frozenset(s.path[0] for s in CREDENTIAL_REF_SITES
                     if len(s.path) == 1 and not s.destinations)
_SHARED_ALL = SecretBinding(DEFAULT_MOUNT_ROOT, MappingProxyType({
    "corp-slack": SharedSecret("corp-alerting", _ALL_TOP)}))
_PROXY_CASES = [(s, proxy) for s in CREDENTIAL_REF_SITES for proxy in s.proxies]


def _proxied_receiver(site) -> tuple[dict, dict]:
    """A receiver whose reference on *site* makes a proxy refusable, and the
    build kwargs: the tenant's own Secret on a row with destinations, a listed
    shared Secret on a row whose content is the destination."""
    if site.destinations:
        return _ref_at(site.path, {"secret": {"key": "k"}}), {}
    receiver = {"type": site.rtype, site.path[0]: {"secret": {"shared": "corp-slack", "key": "k"}}}
    return receiver, {"secrets": _SHARED_ALL}


@pytest.mark.parametrize("site,proxy_key", _PROXY_CASES,
                         ids=[f"{s.rtype}.{'.'.join(s.path)}->{'.'.join(p)}" for s, p in _PROXY_CASES])
def test_the_proxy_the_request_goes_through_is_refused(site, proxy_key):
    """全表：每一列的每一個 proxy（CREDENTIAL_REF_SITES 的 proxies）設了就拒。"""
    receiver, kwargs = _proxied_receiver(site)
    cfg, warnings = _build(receiver, **kwargs)
    assert warnings == [] and cfg is not None, warnings      # 正向控制：沒設 proxy 時放行
    node = receiver
    for step in proxy_key[:-1]:
        node = node.setdefault(step, {})
    node[proxy_key[-1]] = "http://proxy.example.com:3128"
    if proxy_key[1:] == ("oauth2", "proxy_url"):
        node.setdefault("client_id", "c")
        node.setdefault("token_url", _TOKEN_URL)
    assert _kinds(receiver, **kwargs) == [("secret_ref_proxy_url", "receiver." + ".".join(proxy_key))]


def test_every_row_has_a_proxy_case():
    """與 destination 測試同樣以全表參數化：有 proxy 的列都在上面的案例裡。"""
    assert {s for s, _p in _PROXY_CASES} == {s for s in CREDENTIAL_REF_SITES if s.proxies}


# 期望值逐字寫在這裡，不從 CREDENTIAL_REF_SITES 推導：表少列一個目的地或 proxy 時，
# 上面由表生成的案例會跟著少、仍然全綠，這裡會紅。
_URL_DEST = ("url",)
_TOKEN_DEST = ("http_config", "oauth2", "token_url")
_HTTP_PROXY_KEY = ("http_config", "proxy_url")
_OAUTH2_PROXY_KEY = ("http_config", "oauth2", "proxy_url")
_EXPECTED_ROWS = {
    ("webhook", ("url",)): (set(), {_HTTP_PROXY_KEY}),
    ("rocketchat", ("url",)): (set(), {_HTTP_PROXY_KEY}),
    ("slack", ("api_url",)): (set(), {_HTTP_PROXY_KEY}),
    ("teams", ("webhook_url",)): (set(), {_HTTP_PROXY_KEY}),
    ("pagerduty", ("service_key",)): (set(), {_HTTP_PROXY_KEY}),
    ("pagerduty", ("routing_key",)): (set(), {_HTTP_PROXY_KEY}),
    ("email", ("auth_password",)): ({("smarthost",)}, set()),
    ("webhook", ("http_config", "basic_auth", "password")): ({_URL_DEST}, {_HTTP_PROXY_KEY}),
    ("webhook", ("http_config", "authorization", "credentials")): ({_URL_DEST}, {_HTTP_PROXY_KEY}),
    ("webhook", ("http_config", "bearer_token")): ({_URL_DEST}, {_HTTP_PROXY_KEY}),
    ("webhook", ("http_config", "tls_config", "key")): ({_URL_DEST}, {_HTTP_PROXY_KEY}),
    ("webhook", ("http_config", "http_headers", "*", "secrets", "[]")):
        ({_URL_DEST}, {_HTTP_PROXY_KEY}),
    ("webhook", ("http_config", "oauth2", "client_secret")):
        ({_TOKEN_DEST, _URL_DEST}, {_OAUTH2_PROXY_KEY, _HTTP_PROXY_KEY}),
    ("webhook", ("http_config", "oauth2", "client_certificate_key")):
        ({_TOKEN_DEST, _URL_DEST}, {_OAUTH2_PROXY_KEY, _HTTP_PROXY_KEY}),
    ("webhook", ("http_config", "oauth2", "tls_config", "key")):
        ({_TOKEN_DEST, _URL_DEST}, {_OAUTH2_PROXY_KEY, _HTTP_PROXY_KEY}),
}


def test_every_rows_destinations_and_proxies_are_the_expected_ones():
    """oauth2 三列：目的地 {token_url, url}、proxy {oauth2.proxy_url, http_config.proxy_url}；
    其餘 http_config 列：{url}、{http_config.proxy_url}；auth_password：{smarthost}、無 proxy；
    頂層內容即目的地的列：沒有目的地。"""
    got = {(s.rtype, s.path): (set(s.destinations), set(s.proxies)) for s in CREDENTIAL_REF_SITES}
    assert len(got) == len(CREDENTIAL_REF_SITES)   # 沒有重複列
    assert got == _EXPECTED_ROWS


_TLS_KEY = ("http_config", "tls_config", "key")


@pytest.mark.parametrize("path,proxy_key,value", [
    # 只送往 url 的憑證不走 oauth2.proxy_url（那是 token 請求的 proxy）
    (_TLS_KEY, ("http_config", "oauth2", "proxy_url"), "http://proxy.example.com:3128"),
    # 空值／null 視同沒設
    (_PW, ("http_config", "proxy_url"), ""),
    (_PW, ("http_config", "proxy_url"), None),
])
def test_a_proxy_on_another_request_or_unset_is_not_refused(path, proxy_key, value):
    receiver = _ref_at(path, {"secret": {"key": "k"}})
    if proxy_key[1] == "oauth2":
        receiver["http_config"]["oauth2"] = {"client_id": "c", "token_url": _TOKEN_URL}
    _at(receiver, proxy_key[:-1])[proxy_key[-1]] = value
    cfg, warnings = _build(receiver)
    assert warnings == [] and cfg is not None, warnings


_REDIRECT_ROWS = [s for s in CREDENTIAL_REF_SITES if s.follows_redirects]


@pytest.mark.parametrize("site", _REDIRECT_ROWS, ids=lambda s: ".".join(s.path))
@pytest.mark.parametrize("follow", ["absent", True, None,
                                    # 盲審 F2：要的是布林 false，字串 "false"／"no" 不算
                                    "false", "no", 0])
def test_a_credential_resent_on_redirect_needs_follow_redirects_false(site, follow):
    receiver = _ref_at(site.path, {"secret": {"key": "k"}})
    if follow == "absent":
        del receiver["http_config"]["follow_redirects"]
    else:
        receiver["http_config"]["follow_redirects"] = follow
    assert _kinds(receiver) == [("secret_ref_follow_redirects",
                                 "receiver.http_config.follow_redirects")]


def test_follow_redirects_is_not_asked_of_the_tls_client_key():
    """TLS client key 從不送出（只用在 handshake），不必關掉 redirect。"""
    receiver = _ref_at(_TLS_KEY, {"secret": {"key": "k"}})
    del receiver["http_config"]["follow_redirects"]
    cfg, warnings = _build(receiver)
    assert warnings == [] and cfg is not None


# 逐字期望值，不從表推導：哪幾列在 redirect 時會被重送（#3616 review）。
_EXPECTED_REDIRECT_ROWS = {
    ("http_config", "basic_auth", "password"),
    ("http_config", "authorization", "credentials"),
    ("http_config", "bearer_token"),
    ("http_config", "http_headers", "*", "secrets", "[]"),
    ("http_config", "oauth2", "client_secret"),
    ("http_config", "oauth2", "client_certificate_key"),
    ("http_config", "oauth2", "tls_config", "key"),
}


def test_the_rows_resent_on_redirect_are_the_expected_ones():
    assert {s.path for s in _REDIRECT_ROWS} == _EXPECTED_REDIRECT_ROWS


def test_a_bearer_token_reference_still_conflicts_with_basic_auth():
    receiver = _ref_at(("http_config", "bearer_token"), {"secret": {"key": "k"}},
                       basic_auth={"username": "u", "password": "p"})
    cfg, warnings = _build(receiver)
    assert cfg is None and [w.kind for w in warnings] == ["invalid_receiver_field"]
    # 是「至多一個」的衝突，不是「bearer_token 必須是字串」
    assert "accepts at most one" in warnings[0], warnings


@pytest.mark.parametrize("site", _NESTED, ids=lambda s: ".".join(s.path))
def test_a_malformed_reference_on_an_http_config_row_is_an_invalid_field(site):
    receiver = _ref_at(site.path, {"secret": {"name": "da-t-b", "key": "k"}})
    assert [k for k, _f in _kinds(receiver)] == ["invalid_receiver_field"]


@pytest.mark.parametrize("path", [
    ("http_config", "basic_auth", "username"),
    ("http_config", "authorization", "type"),
    ("http_config", "oauth2", "client_id"),
    ("http_config", "tls_config", "ca"),
])
def test_a_reference_on_an_http_config_leaf_off_the_table_is_still_a_leftover(path):
    receiver = _ref_at(path, {"secret": {"key": "k"}})
    got = _kinds(receiver)
    assert ("secret_ref_unsupported_field", "receiver." + ".".join(path)) in got, got


def test_notification_tester_skips_a_receiver_with_an_http_config_reference():
    report = nt.test_tenant_receivers(
        "t-a", {"_routing": _routing(_ref_at(_PW, {"secret": {"key": "k"}}))},
        rate_limit=0, allowed_domains=_ALLOWED)
    [result] = report.receivers
    assert result.status == nt.STATUS_SKIPPED, result
    assert "http_config.basic_auth.password" in result.detail


def test_redaction_masks_an_http_config_reference_with_its_container():
    import _lib_redact
    receiver = _ref_at(_PW, {"secret": {"key": "k"}})
    assert _lib_redact.redact({"receiver": receiver})["receiver"]["http_config"] == \
        _lib_redact.REDACTED


def test_http_config_reference_renders_through_the_cli(tmp_path):
    d = _tree(tmp_path, (
        "        type: webhook\n"
        "        url: https://hook.example.com/a\n"
        "        http_config:\n"
        "          follow_redirects: false\n"
        "          basic_auth: {username: u, password: {secret: {key: hook-pw}}}\n"))
    out = tmp_path / "routes.yaml"
    r = _run(d, "--output-alertmanager", "--policy", _policy(tmp_path, ""), "-o", str(out))
    assert r.returncode == 0, r.stderr
    receivers = yaml.safe_load(out.read_text(encoding="utf-8"))["receivers"]
    main = next(x for x in receivers if x["name"] == "tenant-t-a")
    assert main["webhook_configs"][0]["http_config"]["basic_auth"] == {
        "username": "u", "password_file": f"{DEFAULT_MOUNT_ROOT}/da-t-a/hook-pw"}
    # 同一棵樹、policy 沒有 allowed_domains：--validate 非 0
    p = tmp_path / "bare-policy.yaml"
    p.write_text("shared_secrets: {}\n", encoding="utf-8")
    r = _run(d, "--validate", "--policy", str(p))
    assert r.returncode != 0 and "allowed_domains" in r.stdout + r.stderr


# ── URL 類目的地必須帶 http:// 或 https://（smarthost 維持 host:port）────────

@pytest.mark.parametrize("path,where,value", [
    (_PW, "url", "hook.example.com:80@evil.example"),
    (_PW, "url", "hook.example.com/a"),
    (_CS, "token_url", "hook.example.com:80@evil.example"),
    (_CS, "token_url", "ftp://token.example.com/t"),
])
def test_a_url_destination_without_an_http_scheme_is_refused(path, where, value):
    """token_url 沒有格式檢查，只有這條規則擋；url 先被 receiverHttpUrl 格式擋下
    （invalid_receiver_field），規則本身另以 _destination_problem 直接釘住。"""
    from _grar_merge import _destination_problem
    receiver = _ref_at(path, {"secret": {"key": "k"}})
    if where == "url":
        receiver["url"] = value
        assert [k for k, _f in _kinds(receiver)] == ["invalid_receiver_field"]
        assert _destination_problem(receiver, ("url",), _ALLOWED) is not None
    else:
        receiver["http_config"]["oauth2"]["token_url"] = value
        assert _kinds(receiver) == [("secret_ref_destination_unconfined",
                                     "receiver." + ".".join(path))]


@pytest.mark.parametrize("value", ["HTTPS://token.example.com/t", "http://token.example.com/t"])
def test_a_url_destination_with_either_scheme_in_any_case_passes(value):
    receiver = _ref_at(_CS, {"secret": {"key": "k"}})
    receiver["http_config"]["oauth2"]["token_url"] = value
    cfg, warnings = _build(receiver)
    assert warnings == [] and cfg is not None, warnings


def test_the_smarthost_stays_host_port():
    cfg, warnings = _build({**SMTP, "auth_password": {"secret": {"key": "pw"}}})
    assert warnings == [] and cfg is not None


# ── 不合法的 URL 是該 receiver 的 finding，不是整支程式的例外（#3616 review）────

_BROKEN_URL = "https://[broken/token"


def test_extract_host_reads_a_malformed_url_as_no_host():
    from _grar_validate import _extract_host
    assert _extract_host(_BROKEN_URL) is None
    assert _extract_host("https://hook.example.com/a") == "hook.example.com"


def test_a_malformed_token_url_refuses_only_that_receiver():
    receiver = _ref_at(_CS, {"secret": {"key": "k"}})
    receiver["http_config"]["oauth2"]["token_url"] = _BROKEN_URL
    got = _kinds(receiver)
    assert got == [("secret_ref_destination_unconfined", "receiver." + ".".join(_CS))]


def test_a_malformed_url_in_the_domain_check_is_an_unparseable_host():
    w = validate_receiver_domains({"type": "webhook", "url": "https://[broken/x"}, "t-a",
                                  _ALLOWED, tenant_id="t-a")
    assert [x.kind for x in w] == ["receiver_host_unparseable"]


def test_a_malformed_token_url_does_not_stop_the_other_tenants():
    bad = _ref_at(_CS, {"secret": {"key": "k"}})
    bad["http_config"]["oauth2"]["token_url"] = _BROKEN_URL
    good = _slack("https://hooks.example.com/x")
    _r, receivers, warnings = gar.generate_routes(
        {"t-a": _routing(bad), "t-b": _routing(good)}, _ALLOWED, tenants=["t-a", "t-b"])
    assert [r["name"] for r in receivers] == ["tenant-t-b"]
    assert ("secret_ref_destination_unconfined", "t-a") in {
        (w.kind, w.tenant) for w in warnings if isinstance(w, SkippedEntryWarning)}


# ── ⑤-b：policy `fields` 是完整路徑；沒有所屬租戶的 enforced 可用共用 Secret（2-c 推廣）──

from _lib_validation import shared_secret_field_texts, site_path_text  # noqa: E402


def test_shared_secret_fields_are_the_table_paths():
    """可填的值由 CREDENTIAL_REF_SITES 推導，一列一個寫法；萬用步照字面寫。"""
    assert {site_path_text(s.path) for s in CREDENTIAL_REF_SITES} == shared_secret_field_texts()
    assert site_path_text(("http_config", "http_headers", "*", "secrets", "[]")) \
        == "http_config.http_headers.*.secrets[]"
    assert {"api_url", "url", "auth_password", "http_config.basic_auth.password",
            "http_config.oauth2.tls_config.key", "http_config.tls_config.key",
            "http_config.http_headers.*.secrets[]"} <= shared_secret_field_texts()
    assert "http_config" not in shared_secret_field_texts()


def test_policy_reads_full_field_paths(tmp_path):
    doc = load_policy_document(_policy(tmp_path, (
        "shared_secrets:\n"
        "  corp-smtp: {secret: corp-smtp, fields: [auth_password]}\n"
        "  corp-hook: {secret: corp-hook, fields: [http_config.basic_auth.password,\n"
        "              'http_config.http_headers.*.secrets[]']}\n")))
    assert doc.shared_secrets["corp-smtp"] == SharedSecret("corp-smtp", frozenset({"auth_password"}))
    assert doc.shared_secrets["corp-hook"].fields == frozenset(
        {"http_config.basic_auth.password", "http_config.http_headers.*.secrets[]"})


_HOOK_SHARED = {"secret": {"shared": "corp-hook", "key": "pw"}}
_SMTP_SHARED = {"secret": {"shared": "corp-smtp", "key": "pw"}}


def _bind(**fields) -> SecretBinding:
    return SecretBinding(DEFAULT_MOUNT_ROOT, MappingProxyType({
        alias: SharedSecret(alias, frozenset(f)) for alias, f in fields.items()}))


_PW_BINDING = _bind(**{"corp-hook": {"http_config.basic_auth.password"},
                       "corp-smtp": {"auth_password"}})


def _platform(receiver, **kwargs):
    """build_receiver_config on the receiver no tenant owns."""
    kwargs.setdefault("allowed_domains", _ALLOWED)
    kwargs.setdefault("secrets", _PW_BINDING)
    return build_receiver_config(receiver, "platform-enforced", owner_tenant=None,
                                 field="_routing_enforced.receiver", **kwargs)


def test_platform_enforced_takes_a_shared_secret_on_auth_password():
    cfg, warnings = _platform({**SMTP, "auth_password": _SMTP_SHARED})
    assert warnings == []
    assert _entry(cfg)["auth_password_file"] == f"{DEFAULT_MOUNT_ROOT}/corp-smtp/pw"


def test_platform_enforced_takes_a_shared_secret_on_the_listed_http_config_path():
    cfg, warnings = _platform(_ref_at(_PW, _HOOK_SHARED))
    assert warnings == []
    assert _at(_entry(cfg), ("http_config", "basic_auth"))["password_file"] \
        == f"{DEFAULT_MOUNT_ROOT}/corp-hook/pw"


@pytest.mark.parametrize("path", [("http_config", "authorization", "credentials"),
                                  ("http_config", "bearer_token")])
def test_a_listed_path_does_not_open_its_siblings(path):
    """`fields: [http_config.basic_auth.password]` 只開那一列（舊寫法比 path[0]，一開 8 列）。"""
    cfg, warnings = _platform(_ref_at(path, _HOOK_SHARED))
    assert cfg is None
    assert [w.kind for w in warnings] == ["secret_ref_shared_field_not_allowed"]
    assert "http_config.basic_auth.password" in warnings[0]


def test_a_top_level_name_in_fields_opens_no_http_config_row():
    """舊寫法比對 `path[0]`：`fields: [http_config]` 一次開 8 列。現在比完整路徑，
    即使繞過 policy 讀取端直接給這個集合，也不開任何一列。"""
    binding = _bind(**{"corp-hook": {"http_config"}})
    for site in _NESTED:
        cfg, warnings = _platform(_ref_at(site.path, _HOOK_SHARED), secrets=binding)
        assert cfg is None, site.path
        assert "secret_ref_shared_field_not_allowed" in [w.kind for w in warnings], site.path


@pytest.mark.parametrize("receiver,kwargs,kind", [
    # allowed_domains 仍要非空並放行目的地
    ({**SMTP, "auth_password": _SMTP_SHARED}, {"allowed_domains": None},
     "secret_ref_destination_unconfined"),
    ({**SMTP, "smarthost": "smtp.attacker.example:587", "auth_password": _SMTP_SHARED}, {},
     "secret_ref_destination_unconfined"),
    (_ref_at(_PW, _HOOK_SHARED, ), {"allowed_domains": ["*.other.example"]},
     "secret_ref_destination_unconfined"),
    # follow_redirects 仍要寫成 false
    (_ref_at(_PW, _HOOK_SHARED, follow_redirects=True), {}, "secret_ref_follow_redirects"),
    # 不得設 proxy
    (_ref_at(_PW, _HOOK_SHARED, proxy_url="http://proxy.example.com:3128"), {},
     "secret_ref_proxy_url"),
    # 別名沒列這個欄位
    ({**SMTP, "auth_password": _HOOK_SHARED}, {}, "secret_ref_shared_field_not_allowed"),
])
def test_platform_enforced_shared_secret_keeps_every_other_rule(receiver, kwargs, kind):
    cfg, warnings = _platform(receiver, **kwargs)
    assert cfg is None
    assert kind in [w.kind for w in warnings], warnings


@pytest.mark.parametrize("receiver", [{**SMTP, "auth_password": _SMTP_SHARED},
                                      _ref_at(_PW, _HOOK_SHARED)])
def test_a_tenant_owned_receiver_still_never_takes_a_shared_secret_there(receiver):
    cfg, warnings = build_receiver_config(receiver, "t-a", tenant_id="t-a", owner_tenant="t-a",
                                          secrets=_PW_BINDING, allowed_domains=_ALLOWED)
    assert cfg is None and [w.kind for w in warnings] == ["secret_ref_shared_confined"]


def test_the_tenant_placeholder_enforced_receiver_takes_only_its_own_secret():
    """`{{tenant}}` 版的 enforced 歸各租戶：受限列上的共用 Secret 照舊拒。"""
    configs = {"t-a": _routing(_slack(OWN))}
    templated = {"receiver": {**SMTP, "to": ["noc+{{tenant}}@example.com"],
                              "auth_password": _SMTP_SHARED}}
    _r, receivers, warnings = gar.generate_routes(
        configs, enforced_routing=templated, tenants=["t-a"], secrets=_PW_BINDING,
        allowed_domains=_ALLOWED)
    assert "platform-enforced-t-a" not in [r["name"] for r in receivers]
    assert "secret_ref_shared_confined" in [getattr(w, "kind", None) for w in warnings]
    plain = {"receiver": {**SMTP, "auth_password": _SMTP_SHARED}}
    _r, receivers, warnings = gar.generate_routes(
        configs, enforced_routing=plain, tenants=["t-a"], secrets=_PW_BINDING,
        allowed_domains=_ALLOWED)
    noc = next(r for r in receivers if r["name"] == "platform-enforced")
    assert noc["email_configs"][0]["auth_password_file"].endswith("/corp-smtp/pw")
