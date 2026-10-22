"""#3284：`group_by` 不是 list（字串、數字、布林、mapping）時，所有模式整棵樹拒收。

過去 `group_by: alertname` 被產生器當成「沒有 group_by」：route 上完全沒有這個欄位，
租戶想依 alertname 分組，實際卻繼承上層的分組，`--validate --strict` 照樣 rc 0
（fail-open）。`routing_group_by_invalid`（#2503）只判 list 的元素，不判值本身的型別。

釘住的契約：

* 產生器讀 `group_by` 的每一處都判：租戶 `_routing`（租戶檔與根目錄平台檔的
  `tenants:` 區塊）的主 route、`overrides[i]`、`routes[i]`；任一層 `_routing_defaults`
  的主 route 與 `overrides[i]`；routing profile 的主 route、`overrides[i]`、`routes[i]`；
  `_routing_enforced`。
* 拒收＝`field_type_errors`（`ERROR (field type):`，blocks `always`）：render、
  `--dry-run`、`--validate`、`--strict` 都 rc 1、什麼都不寫出；validate-config 的
  schema 列 FAIL。finding kind 沿用 `routing_group_by_invalid`。
* 不把字串轉成 list；訊息指出正確寫法 `group_by: [alertname]`。
* null 照舊＝沒寫（不輸出 group_by、繼承上層），`[]` 照舊＝不輸出。

租戶 id 都是 fixture 名稱（CLAUDE.md #9）。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "tools"))
sys.path.insert(0, str(REPO / "scripts" / "tools" / "ops"))
from _grar_parse import load_tenant_tree  # noqa: E402
from _grar_validate import routing_block_group_by_type_errors  # noqa: E402
import validate_config as vc  # noqa: E402

_GAR = REPO / "scripts" / "tools" / "ops" / "generate_alertmanager_routes.py"
_REFUSAL = "config problem(s) — nothing was generated, written or applied"


def _gar(d: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_GAR), "--config-dir", str(d), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300)

_DEFAULTS = "defaults:\n  cpu_usage_percent: 80\n"
_HOOK = ("      receiver:\n        type: webhook\n"
         "        url: https://hooks.example.com/{t}\n")
_SUB_HOOK = ("          receiver:\n            type: webhook\n"
             "            url: https://hooks.example.com/sub\n")


def _tenant(t: str, extra: str = "") -> str:
    return (f"tenants:\n  {t}:\n    cpu_usage_percent: '85'\n"
            f"    _routing:\n{_HOOK.format(t=t)}{extra}")


def _main(v: str) -> dict[str, str]:
    return {"t-a.yaml": _tenant("t-a", f"      group_by: {v}\n")}


def _override(v: str) -> dict[str, str]:
    return {"t-a.yaml": _tenant("t-a", (
        "      overrides:\n        - alertname: X\n" + _SUB_HOOK
        + f"          group_by: {v}\n"))}


def _route(v: str) -> dict[str, str]:
    return {"t-a.yaml": _tenant("t-a", (
        "      routes:\n        - match: {severity: critical}\n" + _SUB_HOOK
        + f"          group_by: {v}\n"))}


def _platform_tenants(v: str) -> dict[str, str]:
    """A root platform file's `tenants:` block is the tenant's own value
    (for a tenant some tenant file declares; here without its own _routing)."""
    return {"_defaults.yaml": _DEFAULTS + (
        "tenants:\n  t-b:\n    _routing:\n      receiver:\n        type: webhook\n"
        f"        url: https://hooks.example.com/c\n      group_by: {v}\n"),
        "t-b.yaml": "tenants:\n  t-b:\n    cpu_usage_percent: '85'\n"}


def _defaults(v: str) -> dict[str, str]:
    return {"_defaults.yaml": _DEFAULTS + (
        "_routing_defaults:\n  receiver:\n    type: webhook\n"
        f"    url: https://hooks.example.com/d\n  group_by: {v}\n")}


def _defaults_override(v: str) -> dict[str, str]:
    return {"_defaults.yaml": _DEFAULTS + (
        "_routing_defaults:\n  receiver:\n    type: webhook\n"
        "    url: https://hooks.example.com/d\n  overrides:\n"
        "    - alertname: X\n      receiver:\n        type: webhook\n"
        f"        url: https://hooks.example.com/o\n      group_by: {v}\n")}


def _subdir_defaults(v: str) -> dict[str, str]:
    return {"team/_defaults.yaml": (
        f"_routing_defaults:\n  group_by: {v}\n"),
        "team/t-d.yaml": _tenant("t-d")}


def _profile(v: str) -> dict[str, str]:
    return {"_routing_profiles.yaml": (
        "routing_profiles:\n  p1:\n    receiver:\n      type: webhook\n"
        f"      url: https://hooks.example.com/p\n    group_by: {v}\n")}


def _profile_route(v: str) -> dict[str, str]:
    return {"_routing_profiles.yaml": (
        "routing_profiles:\n  p1:\n    receiver:\n      type: webhook\n"
        "      url: https://hooks.example.com/p\n    routes:\n"
        "      - match: {severity: critical}\n        receiver:\n"
        "          type: webhook\n          url: https://hooks.example.com/r\n"
        f"        group_by: {v}\n")}


def _enforced(v: str) -> dict[str, str]:
    return {"_defaults.yaml": _DEFAULTS + (
        "_routing_enforced:\n  enabled: true\n  receiver:\n    type: webhook\n"
        f"    url: https://noc.example.com/a\n  group_by: {v}\n")}


# location → (files builder, the finding's field)
LOCATIONS = {
    "tenant": (_main, "group_by"),
    "tenant.overrides": (_override, "overrides[0].group_by"),
    "tenant.routes": (_route, "routes[0].group_by"),
    "platform-tenants": (_platform_tenants, "group_by"),
    "routing_defaults": (_defaults, "_routing_defaults.group_by"),
    "routing_defaults.overrides": (_defaults_override,
                                   "_routing_defaults.overrides[0].group_by"),
    "subdir.routing_defaults": (_subdir_defaults, "_routing_defaults.group_by"),
    "profile": (_profile, "routing_profiles.p1.group_by"),
    "profile.routes": (_profile_route, "routing_profiles.p1.routes[0].group_by"),
    "routing_enforced": (_enforced, "_routing_enforced.group_by"),
}
# YAML text as written: every one is a scalar or mapping, never a list.
REFUSED = ["alertname", "'alertname, tenant'", "''", "8", "true", "false", "0",
           "{alertname: 1}"]
ACCEPTED = ["~", "[]", "[alertname]"]


def _write(tmp_path: Path, files: dict[str, str]) -> Path:
    d = tmp_path / "conf.d"
    d.mkdir(parents=True, exist_ok=True)
    base = {"_defaults.yaml": _DEFAULTS, "t-a.yaml": _tenant("t-a"),
            "t-b.yaml": _tenant("t-b")}
    for rel, text in {**base, **files}.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(text, encoding="utf-8")
    return d


def _errors(d: Path):
    return [e for e in load_tenant_tree(str(d)).field_type_errors]


_CASES = [(loc, v) for loc in LOCATIONS for v in REFUSED]
_OK = [(loc, v) for loc in LOCATIONS for v in ACCEPTED]


# ── the predicate ────────────────────────────────────────────────────────

@pytest.mark.parametrize("block, obj, fields", [
    ("routing", {"group_by": "alertname", "overrides": [{"group_by": 8}],
                 "routes": [{"group_by": {"a": 1}}, "not-a-dict"]},
     ["group_by", "overrides[0].group_by", "routes[0].group_by"]),
    # _routing_defaults: its `routes` is dropped before any merge, not read.
    ("routing_defaults", {"group_by": True, "overrides": [{"group_by": ""}],
                          "routes": [{"group_by": "x"}]},
     ["group_by", "overrides[0].group_by"]),
    # _routing_enforced renders only its own group_by.
    ("routing_enforced", {"group_by": "x", "overrides": [{"group_by": "x"}]},
     ["group_by"]),
    ("routing", {"group_by": None, "overrides": [{"group_by": []}],
                 "routes": [{"group_by": ["alertname", 8]}]}, []),
    ("routing", "not-a-dict", []),
])
def test_predicate_fields(block, obj, fields):
    got = routing_block_group_by_type_errors(obj, block=block, where="w")
    assert [f for f, _msg in got] == fields


def test_message_names_the_value_and_the_list_form():
    [(field, msg)] = routing_block_group_by_type_errors(
        {"group_by": "alertname"}, block="routing", where="tenant 't-a': _routing")
    assert field == "group_by"
    assert "must be a list" in msg and "string 'alertname'" in msg, msg
    assert "group_by: [alertname]" in msg, msg
    assert msg.startswith("tenant 't-a': _routing.group_by "), msg


# ── the reader's verdict, location by location ───────────────────────────

@pytest.mark.parametrize("loc,value", _CASES, ids=[f"{n}={v}" for n, v in _CASES])
def test_a_non_list_is_one_field_type_error(tmp_path, loc, value):
    build, field = LOCATIONS[loc]
    errs = _errors(_write(tmp_path, build(value)))
    assert [(e.kind, e.field) for e in errs] == [("routing_group_by_invalid", field)], errs


@pytest.mark.parametrize("loc,value", _OK, ids=[f"{n}={v}" for n, v in _OK])
def test_null_empty_and_a_list_are_accepted(tmp_path, loc, value):
    d = _write(tmp_path, LOCATIONS[loc][0](value))
    assert _errors(d) == []
    p = _gar(d, "--dry-run")
    assert p.returncode == 0, p.stderr
    assert _REFUSAL not in p.stderr


# ── every mode refuses, nothing is written ───────────────────────────────

_MODES = {
    "render": lambda out: ["-o", str(out)],
    "dry-run": lambda out: ["--dry-run"],
    "output-configmap": lambda out: ["--output-configmap", "-o", str(out)],
    "validate": lambda out: ["--validate"],
    "strict": lambda out: ["--strict", "-o", str(out)],
    "validate-strict": lambda out: ["--validate", "--strict"],
}


@pytest.mark.parametrize("loc", sorted(LOCATIONS))
def test_every_mode_refuses_and_writes_nothing(tmp_path, loc):
    d = _write(tmp_path, LOCATIONS[loc][0]("alertname"))
    out = tmp_path / "out.yaml"
    for mode, argv in _MODES.items():
        p = _gar(d, *argv(out))
        assert p.returncode == 1, (mode, p.returncode, p.stderr)
        assert _REFUSAL in p.stderr, (mode, p.stderr)
        assert "ERROR (field type):" in p.stderr and "group_by: [alertname]" in p.stderr, \
            (mode, p.stderr)
        assert not out.exists(), f"{mode} wrote {out}"
        assert "OK:" not in p.stdout, (mode, p.stdout)


def test_issue_repro_validate_strict(tmp_path):
    """The #3284 tree: before, rc 0 and a route with no group_by at all."""
    d = tmp_path / "conf.d"
    d.mkdir()
    (d / "t-a.yaml").write_text(
        "tenants:\n  t-a:\n    _routing:\n"
        "      receiver: {type: webhook, url: \"https://example.com/h\"}\n"
        "      group_by: alertname\n", encoding="utf-8")
    p = _gar(d, "--validate", "--strict")
    assert p.returncode == 1, (p.stdout, p.stderr)
    assert "t-a: _routing.group_by must be a list" in p.stderr, p.stderr
    assert "group_by: [alertname]" in p.stderr, p.stderr


def test_the_findings_document_names_the_field(tmp_path):
    d = _write(tmp_path, _override("alertname"))
    doc_path = tmp_path / "findings.json"
    p = _gar(d, "--dry-run", "--findings-json", str(doc_path))
    assert p.returncode == 1, p.stderr
    doc = json.loads(doc_path.read_text(encoding="utf-8"))
    items = [r for r in doc["findings"] if r["kind"] != "refusal_summary"]
    assert [(r["kind"], r["tenant"], r["field"], r["severity"], r["blocks"])
            for r in items] == [("routing_group_by_invalid", "t-a",
                                 "overrides[0].group_by", "error", "always")], items


@pytest.mark.parametrize("loc", sorted(LOCATIONS))
def test_validate_config_schema_row_fails(tmp_path, loc):
    d = _write(tmp_path, LOCATIONS[loc][0]("alertname"))
    row = vc.check_schema(str(d))
    assert row["status"] == vc.FAIL, row
    assert any("ERROR (field type):" in m and "group_by" in m for m in row["details"]), row


@pytest.mark.parametrize("loc", sorted(LOCATIONS))
def test_validate_config_routes_row_fails(tmp_path, loc):
    """The routes row has --validate semantics; --validate refuses the tree."""
    d = _write(tmp_path, LOCATIONS[loc][0]("alertname"))
    row = vc.check_routes(str(d))
    assert row["status"] == vc.FAIL, row
    assert any("ERROR (field type):" in m and "group_by" in m for m in row["details"]), row


# ── null keeps its meaning ───────────────────────────────────────────────

def test_null_renders_no_group_by_and_inherits(tmp_path):
    d = _write(tmp_path, _main("~"))
    out = tmp_path / "out.yaml"
    p = _gar(d, "-o", str(out))
    assert p.returncode == 0, p.stderr
    doc = yaml.safe_load(out.read_text(encoding="utf-8"))
    [route] = [r for r in doc["route"]["routes"] if r.get("matchers") == ['tenant="t-a"']]
    assert "group_by" not in route, route
