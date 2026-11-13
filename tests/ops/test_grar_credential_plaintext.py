"""#1560 ⑤-b：寫出來的憑證是產生器的 finding（BREAKING）。

判定只有一個函式（`_lib_validation.plaintext_credential_findings`），看**每個檔裡寫的
原始值**，不看合併後的結果（owner 裁決 2-b）：沒人綁的 profile、被租戶蓋掉的
`_routing_defaults`、沒有 plane 讀的位置都要判到。每一處是 `credential_plaintext`
（`CredentialRefusal`，`--validate` 下擋）；判定讀不了的 receiver 是
`credential_unjudged`（第 5 輪審查 N-A）。行內只有檔名、位置、類別，⛔ 不印值。

擋不擋看被寫入的那個檔（ADR-038 決定 5）：產生器容器把這筆寫入改到的檔交給
`credential_scope`，其他檔裡的只是 NOTICE。

租戶 id 都是 fixture 名稱（CLAUDE.md #9）。
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("judging_run")

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "tools"))
sys.path.insert(0, str(REPO / "scripts" / "tools" / "ops"))
import generate_alertmanager_routes as gar  # noqa: E402
import validate_config as vc  # noqa: E402
from _grar_merge import CredentialRefusal  # noqa: E402

_GAR = REPO / "scripts" / "tools" / "ops" / "generate_alertmanager_routes.py"
_MIGRATE = REPO / "scripts" / "tools" / "ops" / "migrate_credentials.py"

CANARY = "cnry-5b-do-not-print"
_DEFAULTS = "defaults:\n  cpu_usage_percent: 80\n"
_HOOK = ("      receiver:\n        type: webhook\n"
         "        url: https://hooks.example.com/{t}\n")


def _tenant(t: str, receiver: str = _HOOK) -> str:
    return (f"tenants:\n  {t}:\n    cpu_usage_percent: '85'\n    _routing:\n"
            + receiver.replace("{t}", t))


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    d = tmp_path / "conf.d"
    for rel, text in {"_defaults.yaml": _DEFAULTS, "t-a.yaml": _tenant("t-a"),
                      **files}.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(text, encoding="utf-8")
    return d


def _validate(d: Path, *extra: str) -> tuple[int, str, list[dict]]:
    out = d.parent / "findings.json"
    p = subprocess.run([sys.executable, str(_GAR), "--config-dir", str(d), "--validate",
                        "--findings-json", str(out), *extra],
                       capture_output=True, text=True, encoding="utf-8", timeout=300)
    doc = json.loads(out.read_text(encoding="utf-8"))
    return p.returncode, p.stdout + p.stderr, doc["findings"]


def _credential(findings: list[dict]) -> list[tuple]:
    return sorted((f["kind"], f["file"], f["field"], f["blocks"]) for f in findings
                  if f["kind"].startswith("credential_"))


# ── 1. 每一類都擋，只印位置與類別 ─────────────────────────────────────────

_SLACK = f"        type: slack\n        api_url: https://hooks.slack.example.com/{CANARY}\n"
_CASES = [
    # needs_reference：只收參照的欄位寫了值
    ("needs_reference", "      receiver:\n" + _SLACK,
     "tenants.t-a._routing.receiver.api_url"),
    # webhook url 帶 userinfo
    ("userinfo", f"      receiver:\n        type: webhook\n"
                 # A test canary, not a credential: no real host, user or password.
                 f"        url: https://u:{CANARY}@hooks.example.com/a\n",  # trufflehog:ignore
     "tenants.t-a._routing.receiver.url"),
    # 代理憑證：proxy_connect_header 出現就擋、proxy_url 帶 userinfo
    ("proxy_credential", "      receiver:\n        type: webhook\n"
                         "        url: https://hooks.example.com/a\n"
                         "        http_config:\n          proxy_connect_header:\n"
                         f"            Proxy-Authorization: [Basic {CANARY}]\n",
     "tenants.t-a._routing.receiver.http_config.proxy_connect_header"),
    ("proxy_credential", "      receiver:\n        type: webhook\n"
                         "        url: https://hooks.example.com/a\n"
                         "        http_config:\n"
                         # A test canary, not a credential: no real host, user or password.
                         f"          proxy_url: http://u:{CANARY}@proxy.example.com:3128\n",  # trufflehog:ignore
     "tenants.t-a._routing.receiver.http_config.proxy_url"),
    # http_config 的認證子欄位
    ("needs_reference", "      receiver:\n        type: webhook\n"
                        "        url: https://hooks.example.com/a\n"
                        "        http_config:\n          basic_auth:\n"
                        f"            username: u\n            password: {CANARY}\n",
     "tenants.t-a._routing.receiver.http_config.basic_auth.password"),
]


@pytest.mark.parametrize("category,receiver,field", _CASES,
                         ids=[f"{c[0]}-{c[2].rsplit('.', 1)[-1]}" for c in _CASES])
def test_each_category_refuses_under_validate_and_never_prints_the_value(
        tmp_path, category, receiver, field):
    d = _tree(tmp_path, {"t-a.yaml": _tenant("t-a", receiver)})
    rc, text, findings = _validate(d)
    assert rc == 1, text
    assert ("credential_plaintext", "t-a.yaml", field, "validate") in _credential(findings)
    line = next(f["message"] for f in findings if f["field"] == field)
    assert line.startswith("ERROR (credential): t-a.yaml: ") and f"({category})" in line
    assert CANARY not in text and CANARY not in json.dumps(findings)


def test_the_proxy_finding_names_the_replacement(tmp_path):
    d = _tree(tmp_path, {"t-a.yaml": _tenant("t-a", _CASES[2][1])})
    _rc, text, _f = _validate(d)
    assert "HTTPS_PROXY" in text and "proxy_from_environment: true" in text


def test_a_reference_and_an_allowed_plaintext_place_pass(tmp_path):
    receiver = ("      receiver:\n        type: webhook\n"
                "        url: https://hooks.example.com/a\n"
                "        http_config:\n          http_headers:\n"
                "            X-Team: {values: [team-a]}\n")
    d = _tree(tmp_path, {"t-a.yaml": _tenant("t-a", receiver),
                         "t-b.yaml": _tenant("t-b", "      receiver:\n        type: slack\n"
                                                    "        api_url: {secret: {key: slack-url}}\n")})
    rc, text, findings = _validate(d)
    assert rc == 0, text
    assert _credential(findings) == []


# ── 2. 看每個檔寫的原始值，不看合併後的值 ─────────────────────────────────

def test_an_overridden_defaults_value_is_still_refused(tmp_path):
    """租戶自己寫了 receiver，合併後 `_routing_defaults` 的 receiver 不在了——值仍在檔裡。"""
    d = _tree(tmp_path, {"_defaults.yaml": _DEFAULTS + "_routing_defaults:\n" + _SLACK.replace(
        "        ", "    ").replace("    type", "  receiver:\n    type", 1)})
    rc, text, findings = _validate(d)
    assert rc == 1, text
    assert _credential(findings) == [("credential_plaintext", "_defaults.yaml",
                                      "_routing_defaults.receiver.api_url", "validate")]


def test_a_profile_no_tenant_binds_is_refused(tmp_path):
    d = _tree(tmp_path, {"_routing_profiles.yaml": (
        "routing_profiles:\n  unbound:\n    receiver:\n      type: pagerduty\n"
        f"      service_key: {CANARY}\n")})
    rc, text, findings = _validate(d)
    assert rc == 1, text
    assert _credential(findings) == [("credential_plaintext", "_routing_profiles.yaml",
                                      "routing_profiles.unbound.receiver.service_key",
                                      "validate")]
    assert CANARY not in text


def test_a_place_no_plane_reads_is_refused(tmp_path):
    """租戶檔裡的 `_routing_enforced` 沒有 plane 讀，但值在 repo 裡。"""
    d = _tree(tmp_path, {"t-a.yaml": _tenant("t-a") + (
        "    _routing_enforced:\n      receiver:\n        type: slack\n"
        f"        api_url: https://hooks.slack.example.com/{CANARY}\n")})
    rc, _text, findings = _validate(d)
    assert rc == 1
    assert ("credential_plaintext", "t-a.yaml",
            "tenants.t-a._routing_enforced.receiver.api_url", "validate") in _credential(findings)


# ── 3. 判不了的 receiver（N-A）──────────────────────────────────────────

@pytest.mark.parametrize("files,field,reason", [
    ({"_routing_profiles.yaml": "routing_profiles:\n  p:\n    receiver:\n"
      f"      type: Slack\n      api_url: https://x.example.com/{CANARY}\n"},
     "routing_profiles.p.receiver", "receiver_type_unknown"),
    ({"_routing_profiles.yaml": "routing_profiles:\n  p:\n    receiver:\n"
      f"      api_url: https://x.example.com/{CANARY}\n"},
     "routing_profiles.p.receiver", "receiver_type_unknown"),
    ({"_routing_profiles.yaml": "routing_profiles:\n  p:\n    receiver:\n"
      "      type: ''\n"}, "routing_profiles.p.receiver", "receiver_type_unknown"),
    ({"_routing_profiles.yaml": "routing_profiles:\n  p:\n    receiver:\n"
      "      type: [slack]\n"}, "routing_profiles.p.receiver", "receiver_type_unknown"),
    ({"_routing_profiles.yaml": "routing_profiles:\n  p:\n    receiver: hook\n"},
     "routing_profiles.p.receiver", "receiver_not_mapping"),
    ({"_defaults.yaml": _DEFAULTS + "_routing_defaults:\n  overrides:\n    x:\n"
      "      receiver:\n        type: slack\n"
      f"        api_url: https://x.example.com/{CANARY}\n"},
     "_routing_defaults.overrides", "slot_not_list"),
    ({"_routing_profiles.yaml": "routing_profiles:\n  p:\n    routes:\n"
      f"      - [https://x.example.com/{CANARY}]\n"},
     "routing_profiles.p.routes[0]", "item_not_mapping"),
])
def test_a_receiver_that_cannot_be_judged_is_refused(tmp_path, files, field, reason):
    d = _tree(tmp_path, files)
    rc, text, findings = _validate(d)
    assert rc == 1, text
    got = [f for f in findings if f["kind"] == "credential_unjudged"]
    # Round 3: a receiver inside a slot of the wrong shape is also named
    # where it stands (`not_walked`) — two lines rather than none.
    inner = ([(f"{field}.x.receiver", "validate")] if reason == "slot_not_list" else [])
    assert sorted((f["field"], f["blocks"]) for f in got) == sorted([(field, "validate")] + inner)
    assert f"({reason})" in next(f for f in got if f["field"] == field)["message"]
    assert CANARY not in text


def test_a_tenants_own_routing_is_judged_by_both(tmp_path):
    """盲審 F2：租戶 `_routing` 的同類形狀錯誤 render 也報；不去重——寧可兩條。"""
    d = _tree(tmp_path, {"t-a.yaml": _tenant("t-a", "      receiver:\n        type: Slack\n"
                                                    "        api_url: {secret: {key: k}}\n")})
    rc, _text, findings = _validate(d)
    assert rc == 1
    kinds = [f["kind"] for f in findings]
    assert "unknown_receiver_type" in kinds and "credential_unjudged" in kinds


def test_an_overridden_tenant_routing_is_still_judged(tmp_path):
    """盲審 F2（案例 e14）：同一租戶的 `_routing` 在兩個被讀的檔裡，被蓋掉的那份
    不會被 render——它的 receiver 仍要判到。"""
    d = _tree(tmp_path, {"_platform.yaml": (
        "tenants:\n  t-a:\n    _routing:\n      receiver:\n        type: bogus\n"
        f"        api_url: https://hooks.slack.example.com/{CANARY}\n")})
    rc, text, findings = _validate(d)
    assert rc == 1, text
    assert ("credential_unjudged", "_platform.yaml", "tenants.t-a._routing.receiver",
            "validate") in _credential(findings)
    assert CANARY not in text


@pytest.mark.parametrize("routing,field,kind", [
    ("_routing:\n  receiver:\n    type: slack\n"
     f"    api_url: https://hooks.slack.example.com/{CANARY}\n",
     "_routing.receiver.api_url", "credential_plaintext"),
    ("_routing:\n  routes:\n    - match: {severity: critical}\n      receiver:\n"
     f"        type: pagerduty\n        service_key: {CANARY}\n",
     "_routing.routes[0].receiver.service_key", "credential_plaintext"),
    ("_routing:\n  overrides: {x: 1}\n", "_routing.overrides", "credential_unjudged"),
])
def test_a_top_level_routing_is_judged(tmp_path, routing, field, kind):
    """盲審 F3（案例 e26）：檔案頂層的 `_routing` 沒有 plane 讀，值仍在 repo 裡。"""
    d = _tree(tmp_path, {"t-a.yaml": routing + _tenant("t-a")})
    rc, text, findings = _validate(d)
    assert rc == 1, text
    assert (kind, "t-a.yaml", field, "validate") in _credential(findings)
    assert CANARY not in text


def test_migrate_credentials_lists_a_top_level_routing(tmp_path):
    d = _tree(tmp_path, {"t-a.yaml": "_routing:\n  receiver:\n    type: slack\n"
                         f"    api_url: https://hooks.slack.example.com/{CANARY}\n"
                         + _tenant("t-a")})
    p = subprocess.run([sys.executable, str(_MIGRATE), "--config-dir", str(d), "--json"],
                       capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert p.returncode == 1, p.stderr
    doc = json.loads(p.stdout)
    assert [(r["file"], r["location"], r["reason"]) for r in doc["decisions"]] == [
        ("t-a.yaml", "_routing.receiver.api_url", "unread_place")]
    assert CANARY not in p.stdout + p.stderr


# ── 4. validate-config 的 routes 列；只 render 的模式 ─────────────────────

def test_validate_config_routes_row_fails(tmp_path):
    d = _tree(tmp_path, {"t-a.yaml": _tenant("t-a", _CASES[0][1])})
    result = vc.check_routes(str(d))
    assert result["status"] == vc.FAIL, result
    assert any("ERROR (credential): t-a.yaml" in line for line in result["details"])
    assert CANARY not in json.dumps(result)


def test_the_finding_is_its_own_blocking_type(tmp_path):
    d = _tree(tmp_path, {"t-a.yaml": _tenant("t-a", _CASES[0][1])})
    found = gar.credential_findings(gar.load_tenant_tree(str(d)))
    assert [type(f) for f in found] == [CredentialRefusal]
    assert gar.blocking_generation_errors(found) == found


# ── 5. 產生器容器：擋不擋看被寫入的那個檔（ADR-038 決定 5）───────────────

def _scoped(d: Path, scope: frozenset[str]) -> tuple[int, list[dict]]:
    out = d.parent / "scoped.json"
    sink = io.StringIO()
    rc = 0
    with redirect_stdout(sink), redirect_stderr(sink):
        try:
            gar.main(["--config-dir", str(d), "--validate", "--findings-json", str(out)],
                     key_verdict=False, credential_scope=scope)
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 1
    assert CANARY not in sink.getvalue()
    return rc, json.loads(out.read_text(encoding="utf-8"))["findings"]


def test_a_value_in_a_file_the_write_does_not_change_does_not_refuse_it(tmp_path):
    d = _tree(tmp_path, {
        "_defaults.yaml": _DEFAULTS + "_routing_defaults:\n  receiver:\n    type: slack\n"
                          f"    api_url: https://hooks.slack.example.com/{CANARY}\n",
        "t-b.yaml": _tenant("t-b")})
    rc, findings = _scoped(d, frozenset({"t-b.yaml"}))
    assert rc == 0, findings
    assert _credential(findings) == [("credential_plaintext", "_defaults.yaml",
                                      "_routing_defaults.receiver.api_url", "never")]
    assert next(f for f in findings if f["kind"] == "credential_plaintext")[
        "message"].startswith("NOTICE (credential): _defaults.yaml")


def test_a_value_already_in_the_written_file_refuses_the_write(tmp_path):
    """寫入沒碰到、但原本就在被寫入那個檔裡的明文也擋。"""
    d = _tree(tmp_path, {"t-a.yaml": _tenant("t-a", _CASES[0][1])})
    rc, findings = _scoped(d, frozenset({"t-a.yaml"}))
    assert rc == 1
    assert _credential(findings) == [("credential_plaintext", "t-a.yaml",
                                      "tenants.t-a._routing.receiver.api_url", "validate")]


def test_the_container_hands_the_written_files_as_the_scope():
    """`_gsvc_child` 把寫入（刪除除外）的路徑交給 credential_scope。"""
    src = (REPO / "scripts" / "tools" / "ops" / "_gsvc_child.py").read_text(encoding="utf-8")
    assert ("credential_scope=frozenset(w.path for w in job.request.writes\n"
            "                                            if not w.delete)") in src


# ── 6. migrate-credentials 列出判不了的 receiver ──────────────────────────

def test_migrate_credentials_lists_what_it_cannot_judge(tmp_path):
    d = _tree(tmp_path, {"_routing_profiles.yaml": "routing_profiles:\n  p:\n    receiver:\n"
                         f"      type: Slack\n      api_url: https://x.example.com/{CANARY}\n"})
    p = subprocess.run([sys.executable, str(_MIGRATE), "--config-dir", str(d), "--json"],
                       capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert p.returncode == 1, p.stderr
    doc = json.loads(p.stdout)
    assert doc["unjudged"] == [{"file": "_routing_profiles.yaml",
                                "location": "routing_profiles.p.receiver",
                                "tenant": None, "reason": "receiver_type_unknown"}]
    assert CANARY not in p.stdout + p.stderr


# ── 7. 訊息不引用憑證值：跨兩張表每一列的 canary（盲審 F1）──────────────
# 格式、型別、目的地檢查把欄位值放進訊息時，一律經 `receiver_value_repr`：
# 路徑在 CREDENTIAL_REF_SITES／CREDENTIAL_PLAINTEXT_SITES 任一列之上、之下或
# 就是那一列時印 `<masked>`。每一列放三種寫錯的值（不合格式的字串、壞掉的
# 參照、該放 mapping／list 的地方放字串），跑 `--validate --strict
# --findings-json`，stdout、stderr、findings 都不得出現 canary。
# Alertmanager 閘門（da-read）的拒收訊息不帶值，由
# test_generate_routes_validate_parity.py 釘住，不在這個 helper 的範圍。

from _lib_python import RECEIVER_TYPES  # noqa: E402
from _lib_validation import CREDENTIAL_PLAINTEXT_SITES, CREDENTIAL_REF_SITES  # noqa: E402

_BASES = {
    "webhook": {"type": "webhook", "url": "https://hooks.example.com/a"},
    "rocketchat": {"type": "rocketchat", "url": "https://chat.example.com/h"},
    "slack": {"type": "slack", "api_url": {"secret": {"key": "slack-url"}}},
    "teams": {"type": "teams", "webhook_url": {"secret": {"key": "teams-url"}}},
    "pagerduty": {"type": "pagerduty", "routing_key": {"secret": {"key": "pd"}}},
    "email": {"type": "email", "to": ["ops@example.com"], "from": "am@example.com",
              "smarthost": "smtp.example.com:587"},
}
_ROWS = ([(s.rtype, s.path) for s in CREDENTIAL_REF_SITES]
         + [(s.rtype, s.path) for s in CREDENTIAL_PLAINTEXT_SITES])
_LEAF = f"{CANARY} not-a-url@["


def _receiver_with(rtype: str, path: tuple, variant: str) -> dict:
    """The *rtype* base receiver with a bad value on row *path*: ``leaf`` a
    string that is no URL, ``ref`` a broken reference, ``container`` a
    string where the mapping / list holding the leaf belongs."""
    import copy
    receiver = copy.deepcopy(_BASES[rtype])
    if rtype == "pagerduty" and path == ("routing_key",):
        receiver = {"type": "pagerduty"}
    steps = ["X-Canary" if st == "*" else st for st in path]
    leaf = {"leaf": _LEAF, "ref": {"secret": CANARY}}.get(variant)
    if variant == "container":
        cut = len(steps) - (2 if steps[-1] == "[]" else 1)
        steps, leaf = steps[:cut], _LEAF
    node = receiver
    for i, st in enumerate(steps):
        last = i == len(steps) - 1
        if st == "[]":
            continue
        nxt = steps[i + 1] if i + 1 < len(steps) else None
        if last or nxt == "[]" and i + 1 == len(steps) - 1:
            node[st] = [leaf] if nxt == "[]" else leaf
            break
        node = node.setdefault(st, {})
        if st == "oauth2":
            node.setdefault("client_id", "c")
            node.setdefault("token_url", "https://token.example.com/t")
        if st == "tls_config":
            node.setdefault("cert", "client-cert-pem")
    return receiver


_CANARY_CASES = [(rtype, path, v) for rtype, path in _ROWS
                 for v in ("leaf", "ref", "container") if not (v == "container" and len(path) == 1)]


@pytest.mark.parametrize("rtype,path,variant", _CANARY_CASES,
                         ids=[f"{r}-{'.'.join(p)}-{v}" for r, p, v in _CANARY_CASES])
def test_no_message_quotes_a_credential_value(tmp_path, rtype, path, variant):
    import yaml
    assert rtype in RECEIVER_TYPES
    receiver = _receiver_with(rtype, path, variant)
    d = _tree(tmp_path, {"t-a.yaml": yaml.safe_dump({"tenants": {"t-a": {
        "cpu_usage_percent": "85", "_routing": {"receiver": receiver}}}})})
    assert CANARY in (d / "t-a.yaml").read_text(encoding="utf-8")
    empty = tmp_path / "bin"
    empty.mkdir()
    out = tmp_path / "f.json"
    p = subprocess.run([sys.executable, str(_GAR), "--config-dir", str(d), "--validate",
                        "--strict", "--findings-json", str(out)],
                       capture_output=True, text=True, encoding="utf-8", timeout=300,
                       env={**__import__("os").environ, "PATH": str(empty)})
    assert p.returncode in (0, 1), p.stderr
    text = p.stdout + p.stderr + out.read_text(encoding="utf-8")
    assert CANARY not in text, text


# ── 8. 不變式：routing 區塊裡每個 receiver 形狀的值，不是被走訪器判過，就是 finding ──
# （第 3 輪，換主體：深掃取代逐一列舉形狀。）同一個帶明文的 receiver 用各種
# 走訪器不認得的方式包起來，每一種都要有 credential finding、`--validate` rc 1。

_R = {"type": "slack", "api_url": f"https://hooks.slack.example.com/{CANARY}"}
_TENANT_ONLY = "tenants:\n  t-a:\n    cpu_usage_percent: '85'\n"
_WRAPS = [
    ("tenant_routing_list", {"t-a.yaml": {"tenants": {"t-a": {"_routing": [{"receiver": _R}]}}}}),
    ("receiver_as_string", {"_routing_profiles.yaml": {"routing_profiles": {"p": {
        "routes": [{"match": {"x": "y"}, "receiver": f"https://h.example.com/{CANARY}"}]}}}}),
    ("nested_routes", {"t-a.yaml": {"tenants": {"t-a": {"_routing": {
        "receiver": {"type": "slack", "api_url": {"secret": {"key": "k"}}},
        "routes": [{"match": {"a": "b"}, "receiver": {"type": "slack", "api_url": {
            "secret": {"key": "k"}}}, "routes": [{"match": {"c": "d"}, "receiver": _R}]}]}}}}}),
    ("extra_mapping_layer", {"t-a.yaml": {"tenants": {"t-a": {"_routing": {
        "receiver": {"type": "slack", "api_url": {"secret": {"key": "k"}}},
        "extra": {"receiver": _R}}}}}}),
    ("profile_as_list", {"_routing_profiles.yaml": {"routing_profiles": {"p": [{"receiver": _R}]}}}),
    ("profiles_as_list", {"_routing_profiles.yaml": {"routing_profiles": [{"receiver": _R}]}}),
    ("root_enforced_as_list", {"_platform.yaml": {"_routing_enforced": [{"receiver": _R}]}}),
    ("defaults_as_list", {"_platform.yaml": {"_routing_defaults": [{"receiver": _R}]}}),
    ("top_level_routing_list", {"t-b.yaml": {"_routing": [{"receiver": _R}],
                                             "tenants": {"t-b": {"cpu_usage_percent": "85"}}}}),
    ("typed_mapping_no_receiver_key", {"_routing_profiles.yaml": {"routing_profiles": {"p": {
        "routes": [{"match": {"x": "y"}, "fallback": _R}]}}}}),
    ("enforced_overrides", {"_platform.yaml": {"_routing_enforced": {
        "enabled": True, "receiver": {"type": "webhook", "url": "https://noc.example.com/a"},
        "overrides": [{"alertname": "X", "receiver": _R}]}}}),
]


@pytest.mark.parametrize("files", [w[1] for w in _WRAPS], ids=[w[0] for w in _WRAPS])
def test_a_wrapped_receiver_is_never_silent(tmp_path, files):
    import yaml
    d = _tree(tmp_path, {name: yaml.safe_dump(body, sort_keys=False)
                         for name, body in files.items()})
    rc, text, findings = _validate(d)
    assert rc == 1, text
    assert _credential(findings), findings
    assert all(f["blocks"] == "validate" for f in findings if f["kind"].startswith("credential_"))
    assert CANARY not in text and CANARY not in json.dumps(findings)


def test_the_shipped_trees_have_no_credential_finding():
    """對照組：出貨的 conf.d 不因深掃多出 finding。"""
    for rel in ("components/threshold-exporter/config/conf.d", "try-local/seed/conf.d",
                "rule-packs/recipes/examples/conf.d",
                "components/da-tools/app/examples/cardinality-demo/conf.d",
                "tests/fixtures/da-tools-smoke/new/conf.d",
                "tests/fixtures/da-tools-smoke/old/conf.d"):
        tree = gar.load_tenant_tree(str(REPO / rel))
        assert gar.credential_findings(tree) == [], rel


# ── 9. 深掃的誤報（第 4 輪 F-C、F-D）──────────────────────────────────────

_REF = {"type": "slack", "api_url": {"secret": {"key": "k"}}}


@pytest.mark.parametrize("match", [{"receiver": "team-a", "severity": "critical"},
                                   {"type": "slack"}, {"receiver": "team-a", "type": "slack"}],
                         ids=["label-receiver", "label-type", "both"])
def test_match_labels_named_receiver_or_type_are_labels(tmp_path, match):
    """F-C：`match` 底下的鍵是 label；只有 mapping 值才算 receiver 形狀。"""
    import yaml
    d = _tree(tmp_path, {"t-a.yaml": yaml.safe_dump({"tenants": {"t-a": {
        "cpu_usage_percent": "85", "_routing": {
            "receiver": _REF, "routes": [{"match": match, "receiver": _REF}]}}}})})
    rc, text, findings = _validate(d)
    assert rc == 0, text
    assert _credential(findings) == []


def test_a_mapping_under_match_is_still_a_receiver(tmp_path):
    import yaml
    d = _tree(tmp_path, {"t-a.yaml": yaml.safe_dump({"tenants": {"t-a": {
        "cpu_usage_percent": "85", "_routing": {
            "receiver": _REF, "routes": [{"match": {"receiver": {
                "type": "slack", "api_url": f"https://h.example/{CANARY}"}}, "receiver": _REF}]}}}})})
    rc, text, findings = _validate(d)
    assert rc == 1
    assert ("credential_unjudged", "t-a.yaml",
            "tenants.t-a._routing.routes[0].match.receiver", "validate") in _credential(findings)
    assert CANARY not in text


def test_a_route_entry_with_a_type_label_is_not_a_receiver(tmp_path):
    """F-D：`overrides[i]`／`routes[i]` 的直接項目是 route entry，不套 `type` 規則。"""
    import yaml
    d = _tree(tmp_path, {"t-a.yaml": yaml.safe_dump({"tenants": {"t-a": {
        "cpu_usage_percent": "85", "_routing": {
            "receiver": _REF, "overrides": [{"alertname": "x", "type": "slack",
                                             "receiver": _REF}]}}}})})
    _rc, _text, findings = _validate(d)
    assert not [f for f in findings if f["kind"] == "credential_unjudged"], findings


# ── 憑證欄位的形狀讀不了：既不是 finding 也不是參照，沒人綁的 profile 又沒有 schema 檢查 ──

_UNBOUND = "routing_profiles:\n  unbound:\n    receiver:\n{body}"


@pytest.mark.parametrize("body,field", [
    ("      type: slack\n      api_url: [https://hooks.slack.com/services/T/B/" + CANARY + "]\n",
     "routing_profiles.unbound.receiver.api_url"),
    ("      type: slack\n      api_url: {url: https://hooks.slack.com/services/T/B/" + CANARY + "}\n",
     "routing_profiles.unbound.receiver.api_url"),
    # A container of the wrong shape on the way to the site.
    ("      type: webhook\n      url: https://noc.example.com/a\n"
     "      http_config: [https://u:" + CANARY + "@proxy.example.com]\n",
     "routing_profiles.unbound.receiver.http_config"),
    ("      type: webhook\n      url: https://noc.example.com/a\n"
     "      http_config:\n        http_headers:\n          X-Token:\n            secrets: " + CANARY + "\n",
     "routing_profiles.unbound.receiver.http_config.http_headers.X-Token.secrets"),
])
def test_a_credential_field_that_is_not_a_value_is_unjudged(tmp_path, body, field):
    d = _tree(tmp_path, {"_routing_profiles.yaml": _UNBOUND.format(body=body)})
    rc, text, findings = _validate(d)
    assert rc == 1, text
    assert _credential(findings) == [("credential_unjudged", "_routing_profiles.yaml", field,
                                      "validate")]
    assert "credential_not_value" in text
    assert CANARY not in text


def test_a_reference_at_the_same_place_is_still_judged_fine(tmp_path):
    d = _tree(tmp_path, {"_routing_profiles.yaml": _UNBOUND.format(
        body="      type: slack\n      api_url: {secret: {key: slack-url}}\n")})
    rc, text, findings = _validate(d)
    assert _credential(findings) == [], text


def test_migrate_credentials_lists_a_credential_field_that_is_not_a_value(tmp_path):
    d = _tree(tmp_path, {"_routing_profiles.yaml": _UNBOUND.format(
        body=f"      type: slack\n      api_url: [https://hooks.slack.com/services/T/B/{CANARY}]\n")})
    p = subprocess.run([sys.executable, str(_MIGRATE), "--config-dir", str(d), "--json"],
                       capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert p.returncode == 1, p.stderr
    doc = json.loads(p.stdout)
    assert [(u["file"], u["reason"]) for u in doc["unjudged"]] == [
        ("_routing_profiles.yaml", "credential_not_value")]
    assert CANARY not in p.stdout + p.stderr


# ── 比 Python 遞迴上限還深的巢狀，深掃時不得 RecursionError ─────────────────

def test_the_deep_scan_walks_a_tree_deeper_than_the_recursion_limit():
    from _grar_parse import _not_walked
    depth = sys.getrecursionlimit() * 3
    deep: object = "x"
    for _ in range(depth):
        deep = [deep]
    hidden: object = {"receiver": {"type": "slack", "api_url": "u"}}
    for _ in range(depth):
        hidden = [hidden]
    data = {"tenants": {"t-a": {"_metadata": {"x": deep}, "_routing": {"x": hidden}}}}
    found = _not_walked(data, set())
    assert found == [("tenants", "t-a", "_routing", "x", *([0] * depth), "receiver")]


def test_a_deep_value_outside_routing_does_not_crash_the_generator(tmp_path):
    depth = 3000
    d = _tree(tmp_path, {"t-a.yaml": (
        "tenants:\n  t-a:\n    cpu_usage_percent: '85'\n    _metadata:\n      x: "
        + "[" * depth + "]" * depth + "\n")})
    rc, text, _ = _validate(d)
    assert "RecursionError" not in text
    assert rc in (0, 1), text


_MALFORMED_REFS = [
    "{secret: {key: k, value: '" + CANARY + "'}}",
    "{secret: {key: 'https://u:" + CANARY + "@hooks.example.com/x'}}",
    "{secret: {key: k}, value: 'https://hooks.example.com/" + CANARY + "'}",
    "{secret: {}, plaintext: '" + CANARY + "'}",
]
_UNRENDERED = [
    ("_routing_profiles.yaml",
     "routing_profiles:\n  unbound:\n    receiver:\n      type: slack\n      api_url: {ref}\n",
     "routing_profiles.unbound.receiver.api_url"),
    ("_defaults.yaml",
     _DEFAULTS + "_routing_defaults:\n  receiver:\n    type: slack\n    api_url: {ref}\n",
     "_routing_defaults.receiver.api_url"),
    ("t-a.yaml",
     "_routing:\n  receiver:\n    type: slack\n    api_url: {ref}\n" + _tenant("t-a"),
     "_routing.receiver.api_url"),
]


@pytest.mark.parametrize("ref", _MALFORMED_REFS)
@pytest.mark.parametrize("fname,text,field", _UNRENDERED)
def test_a_malformed_reference_where_nothing_renders_is_unjudged(tmp_path, ref, fname, text, field):
    d = _tree(tmp_path, {fname: text.replace("{ref}", ref)})
    rc, out, findings = _validate(d)
    assert rc == 1, out
    assert ("credential_unjudged", fname, field, "validate") in _credential(findings), findings
    assert CANARY not in out
    p = subprocess.run([sys.executable, str(_MIGRATE), "--config-dir", str(d), "--json"],
                       capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert p.returncode == 1, p.stderr
    assert (fname, "credential_not_value") in {(u["file"], u["reason"])
                                               for u in json.loads(p.stdout)["unjudged"]}
    assert CANARY not in p.stdout + p.stderr
