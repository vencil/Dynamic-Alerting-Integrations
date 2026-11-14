"""#3788：validate-config 的 `routes` 列就是 `generate-routes --validate`。

兩者曾經對同一棵樹給不同結論：租戶存在、卻沒有產生任何 route／inhibit rule 的樹，
`--validate` 結束碼 1，`routes` 列 WARN、validate-config 結束碼 0。原因是一份判定被
拆成 `schema`、`routes` 兩列，各自組一份輸入。現在兩邊都只呼叫產生器的
`validate_tree`，`schema` 列移除。

本檔釘住的不變式，逐案例、帶與不帶 `--strict` 各一次、都跑真的 CLI：

* `routes` 列 FAIL **若且唯若** `generate-routes --validate` 結束碼不是 0；
  結束碼 2 時該列 `caller_error` 為真；
* 該列的明細就是那支指令印出的行（不計順序：兩個串流經管線後先後不保證）；
* 每個案例預期的那一行確實出現在兩邊——什麼都沒判的執行過不了；
* 每個案例的結束碼寫明，所以矩陣兩側（通過／擋下）都有案例，不會整張表只測到一邊。

validate-config 的其他列（`guard_errors`、`yaml_quoting`、`json_schema`……）是另外加的
檢查，不在這個不變式裡：這裡只讀 `routes` 列。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_GENERATE = _REPO / "scripts/tools/ops/generate_alertmanager_routes.py"
_VALIDATE = _REPO / "scripts/tools/ops/validate_config.py"

pytestmark = pytest.mark.usefixtures("da_guard_env", "da_crdecode_env", "da_read_env")

_DEFAULTS = "defaults:\n  cpu_usage_percent: 80\n"
_WEBHOOK = ("    _routing:\n      receiver:\n        type: webhook\n"
            "        url: https://hooks.example.com/a\n")
_TENANT = "tenants:\n  t-a:\n    cpu_usage_percent: \"85\"\n"
_ALLOW = "allowed_domains: [allowed.example.com]\n"

#: id → (files, policy text or None, line both sides must print,
#:       --validate's exit code without --strict, with --strict)
CASES = {
    "clean": (
        {"_defaults.yaml": _DEFAULTS, "t-a.yaml": _TENANT + _WEBHOOK},
        None, "OK: all configs valid", 0, 0),
    "no-tenant": (
        {"_defaults.yaml": _DEFAULTS},
        None, "No tenants found in config directory.", 0, 0),
    # The tree #3788 was filed on.
    "nothing-generated": (
        {"_defaults.yaml": _DEFAULTS,
         "t-a.yaml": _TENANT + "    _severity_dedup: disable\n"},
        None, "No valid routes or inhibit rules generated.", 1, 1),
    "receiver-unknown-field": (
        {"_defaults.yaml": _DEFAULTS, "t-a.yaml": _TENANT + _WEBHOOK + "        max_alerts: 3\n"},
        None, "max_alerts", 1, 1),
    "field-type": (
        {"_defaults.yaml": _DEFAULTS,
         "t-a.yaml": _TENANT + _WEBHOOK + "      group_by: alertname\n"},
        None, "ERROR (field type):", 1, 1),
    "strict-only-domain-policy": (
        {"_defaults.yaml": _DEFAULTS,
         "t-a.yaml": _TENANT + _WEBHOOK + "      repeat_interval: \"1h30m\"\n",
         "_domain_policy.yaml": ("domain_policies:\n  fin:\n    tenants: [t-a]\n"
                                 "    constraints:\n      max_repeat_interval: \"1h\"\n")},
        None, "exceeds max '1h'", 0, 1),
    "routing-defaults-routes-ignored": (
        {"_defaults.yaml": _DEFAULTS + "_routing_defaults:\n  routes: []\n",
         "t-a.yaml": _TENANT + _WEBHOOK},
        None, "'routes'", 1, 1),
    "unselected-defaults-carrier": (
        {"_defaults.yaml": _DEFAULTS, "_defaults.yml": "defaults:\n  cpu_usage_percent: 70\n",
         "t-a.yaml": _TENANT + _WEBHOOK},
        None, "_defaults.yml is not read", 1, 1),
    "routing-tree-error": (
        {"_defaults.yaml": _DEFAULTS, "team/_defaults.yaml": "_routing_enforced:\n  enabled: true\n",
         "team/t-a.yaml": _TENANT + _WEBHOOK},
        None, "_routing_enforced is read only at the conf.d root", 2, 2),
    "unread-key": (
        {"_defaults.yaml": _DEFAULTS,
         "t-a.yaml": _TENANT + "    cpu_usage_precent: \"85\"\n" + _WEBHOOK},
        None, "key verdict:", 1, 1),
    # #3873: the key verdict's own cases, now that no other row reports it —
    # a warning alone passes, a key no root platform file lets it judge fails.
    "key-warning-only": (
        {"_defaults.yaml": _DEFAULTS,
         "t-a.yaml": _TENANT + "    cpu_usage_precent: disable\n" + _WEBHOOK},
        None, "WARN: key verdict:", 0, 0),
    "key-undecidable": (
        {"t-a.yaml": _TENANT + _WEBHOOK},
        None, "cannot be judged (undecidable)", 1, 1),
    "unreadable-tenant-file": (
        {"_defaults.yaml": _DEFAULTS, "t-a.yaml": _TENANT + _WEBHOOK,
         "t-b.yaml": "tenants:\n  t-b: [1\n"},
        None, "could not be read", 1, 1),
    "policy-allowlist-refusal": (
        {"_defaults.yaml": _DEFAULTS, "t-a.yaml": _TENANT + _WEBHOOK},
        _ALLOW, "hooks.example.com", 1, 1),
    "policy-unusable": (
        {"_defaults.yaml": _DEFAULTS, "t-a.yaml": _TENANT + _WEBHOOK},
        "allowed_domains: 7\n", "allowed_domains", 2, 2),
}


def _run(tool: Path, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-s", str(tool), *argv],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=300)


def _lines(text: str) -> list[str]:
    return sorted(ln.rstrip() for ln in text.splitlines() if ln.strip())


@pytest.mark.parametrize("strict", [False, True], ids=["plain", "strict"])
@pytest.mark.parametrize("case", sorted(CASES))
def test_routes_row_is_generate_routes_validate(tmp_path, case, strict):
    files, policy, expected, rc_plain, rc_strict = CASES[case]
    conf = tmp_path / "conf.d"
    for name, body in files.items():
        (conf / name).parent.mkdir(parents=True, exist_ok=True)
        (conf / name).write_text(body, encoding="utf-8")
    flags = ["--strict"] if strict else []
    if policy is not None:
        (tmp_path / "policy.yaml").write_text(policy, encoding="utf-8")
        flags += ["--policy", str(tmp_path / "policy.yaml")]

    gen = _run(_GENERATE, "--config-dir", str(conf), "--validate", *flags)
    vc = _run(_VALIDATE, "--config-dir", str(conf), "--json", *flags)
    if case == "policy-unusable":
        # #3797: --policy is judged before any row runs, in both tools, by the
        # one resolver: the same rc 2 and the same line, no routes row (#3617).
        assert gen.returncode == vc.returncode == 2, (gen.stderr, vc.stdout, vc.stderr)
        assert vc.stdout == ""
        assert expected in gen.stdout + gen.stderr and expected in vc.stderr
        return
    (row,) = [r for r in json.loads(vc.stdout) if r["check"] == "routes"]
    where = (gen.returncode, gen.stdout, gen.stderr, row)

    assert gen.returncode == (rc_strict if strict else rc_plain), where
    # The invariant.
    assert (row["status"] == "fail") == (gen.returncode != 0), where
    assert row["caller_error"] is (gen.returncode == 2), where
    # Both sides judged this tree: the line the case is about is in each.
    assert expected in gen.stdout + gen.stderr, where
    assert any(expected in d for d in row["details"]), where
    # The row is that run.
    assert sorted(d.rstrip() for d in row["details"]) == _lines(gen.stdout + gen.stderr), where
    # validate-config can only be at 0 when the row is not FAIL.
    assert vc.returncode != 0 or row["status"] != "fail", where


def test_the_matrix_has_both_sides():
    """A matrix of only refused trees (or only accepted ones) would pin half
    of an "if and only if"."""
    codes = {rc for _f, _p, _e, plain, strict in CASES.values() for rc in (plain, strict)}
    assert codes == {0, 1, 2}, codes
    assert any(plain == 0 and strict != 0 for _f, _p, _e, plain, strict in CASES.values())


def test_validate_config_has_no_schema_row(tmp_path):
    conf = tmp_path / "conf.d"
    conf.mkdir()
    (conf / "_defaults.yaml").write_text(_DEFAULTS, encoding="utf-8")
    (conf / "t-a.yaml").write_text(_TENANT + _WEBHOOK, encoding="utf-8")
    vc = _run(_VALIDATE, "--config-dir", str(conf), "--json")
    assert vc.returncode == 0, (vc.stdout, vc.stderr)
    assert "schema" not in {r["check"] for r in json.loads(vc.stdout)}
