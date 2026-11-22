"""The tenant-key membership matrix (#1189 / TRK-337), asserted on the shared
key verdict (ADR-038 §11).

`tests/shared/optional_overrides_membership_matrix.json` was the parity table
of two hand-copies of one rule — Go's `ValidateTenantKeys` and Python's
`validate_tenant_keys`. Neither judges key names any more: whether a key is
read is the shared verdict's (`_lib_key_verdict`, da-guard's readings of the
exporter), and a `version` label is the exporter's own check (served-values'
`key_checks`). The table keeps its meaning: each case is one tenant key over
a platform that values `defaults` and declares `declared`; `refuse` is what
the tenant-api write gate refuses — the verdict reads the key as unread (an
error), or the exporter refuses its `version` label — and `accept` is
neither. A rename NOTICE is advisory and never a refusal.

Tenant ids are fixture names (CLAUDE.md #9).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import _lib_key_verdict as kv
import _lib_tenant_values as tv

pytestmark = pytest.mark.usefixtures("da_guard_env")

_MATRIX = Path(__file__).resolve().parent / "optional_overrides_membership_matrix.json"


def _matrix() -> dict:
    return json.loads(_MATRIX.read_text(encoding="utf-8"))


def _cases() -> list[dict]:
    from _registry_lib import DEPRECATED_KEY_ALIASES  # the authored table
    # A row spelling the live alias stops meaning anything once the #1231
    # window closes (the matrix's own _comment): skipped then, not failed.
    return [c for c in _matrix()["cases"] if DEPRECATED_KEY_ALIASES or not c.get("requires_alias")]


def test_matrix_is_not_truncated():
    """An empty table is green forever: the shape this repo keeps finding."""
    cases = _matrix()["cases"]
    assert len(cases) >= 20, f"matrix looks truncated: {len(cases)}"
    assert {c["verdict"] for c in cases} == {"accept", "refuse"}


def _refused(tmp_path: Path, case: dict) -> list[str]:
    """Why the write gate refuses `case`'s key: the verdict's error findings
    on it, and the exporter's `version` label messages; [] = accepted."""
    m = _matrix()
    root = {"defaults": {k: 80 for k in m["defaults"]}, "optional_overrides": case["declared"]}
    if case.get("dimensions"):
        root["dimensions"] = case["dimensions"]
    (tmp_path / "_defaults.yaml").write_text(yaml.safe_dump(root), encoding="utf-8")
    (tmp_path / "t-a.yaml").write_text(
        yaml.safe_dump({"tenants": {"t-a": {case["key"]: "5"}}}), encoding="utf-8")
    verdict = kv.judge_tree(str(tmp_path))
    # The matrix is about key NAMES (membership); every key is written as
    # "5", which is no value a reserved key's reader reads. A value_unread
    # finding (#3591) is about the value, not whether the key is read.
    why = [kv.finding_line(f, 5) for f in verdict.findings
           if f.severity == kv.ERROR and f.key == case["key"] and f.kind != "value_unread"]
    checks = kv.key_check_lines(verdict)
    return why + [kv.key_check_line(t, c) for t, c in checks if c.kind not in tv.KEY_CHECK_RENAMED_KINDS]


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["name"])
def test_the_shared_verdict_matches_the_matrix(tmp_path, case):
    why = _refused(tmp_path, case)
    got = "refuse" if why else "accept"
    assert got == case["verdict"], (
        f"{case['key']!r} over declared={case['declared']!r}: {got} ({why}), "
        f"the matrix says {case['verdict']} — {case['why']}")


class TestShippedDeclaredListIsSettable:
    """#1311 驗收，#3924 改寫：平台「認得但不主張值」的那批 registry 鍵，租戶
    要寫得進去（ADR-038 §11 共用鍵判決的**放行**路徑）。

    #3924 前，讓它們設得進去的是出貨的 `optional_overrides:` 清單；現在它們全在
    讀取端的內建層裡，出貨清單是空的，**不列任何清單**也要放行。所以這裡改讀
    registry 的成員（`tier: optional_overrides` 的平鍵），並驗兩件事：產物確實
    出貨空清單，以及這批鍵在空清單下仍被放行。

    ⭐ 判準來源沿用上面那張判決矩陣的**期望值**，不在本檔另發明一套；下面的
    `test_matrix_still_defines_the_two_verdicts_this_test_leans_on` 就是在釘住那張
    表沒有被改成別的意思。

    ⛔ 為什麼 `defaults` 要驗兩態：若某個 key 只有在它**同時**還躺在 `defaults:`
    裡才被放行，讓它 settable 的就是根目錄的值、不是內建層。所以除了矩陣自己的
    defaults fixture，另跑一次 `defaults=∅`。
    """

    _HELM_VALUES = "helm/threshold-exporter/values.yaml"
    _DEV_DEFAULTS = "components/threshold-exporter/config/conf.d/_defaults.yaml"
    _TRY_LOCAL_SEED = "try-local/seed/conf.d/_defaults.yaml"

    @staticmethod
    def _repo_root():
        from pathlib import Path
        return Path(__file__).resolve().parents[2]

    @classmethod
    def _matrix(cls):
        return _matrix()

    @classmethod
    def _shipped_from(cls, rel_path: str, getter) -> list:
        doc = yaml.safe_load(
            (cls._repo_root() / rel_path).read_text(encoding="utf-8")) or {}
        return getter(doc)

    @classmethod
    def _shipped(cls) -> list:
        """The registry keys the platform recognises without a value."""
        import _registry_lib as rl  # noqa: PLC0415
        return sorted(k for k, e in rl.registry_keys(rl.load_registry()).items()
                      if e.get("tier") == "optional_overrides"
                      and rl.is_shipped_optional_key(k))

    def test_the_shipped_artifacts_carry_no_declared_list(self):
        """#3924：三份產物都不列 registry 鍵——內建層已經涵蓋，列了不改變任何事。
        而推導端不得為空，否則底下兩個迴圈會永遠綠。"""
        helm = self._shipped_from(
            self._HELM_VALUES,
            lambda d: (d.get("thresholdConfig") or {}).get("optional_overrides") or [])
        dev = self._shipped_from(
            self._DEV_DEFAULTS, lambda d: d.get("optional_overrides") or [])
        seed = self._shipped_from(
            self._TRY_LOCAL_SEED, lambda d: d.get("optional_overrides") or [])
        assert helm == dev == seed == [], (helm, dev, seed)
        assert self._shipped(), "no registry key without a platform value — vacuous"

    def test_matrix_still_defines_the_two_verdicts_this_test_leans_on(self):
        """本測試的判準完全外包給矩陣；矩陣若被改寫成別的意思，這裡要先紅，
        而不是繼續拿一個已經不存在的契約當靠山。"""
        cases = self._matrix()["cases"]
        on_list = [c for c in cases if c["key"] in c["declared"]]
        assert any(c["verdict"] == "accept" and not c["key"].endswith("_critical")
                   and "{" not in c["key"] for c in on_list), \
            "matrix no longer says a flat key ON the list is settable"
        assert any(c["verdict"] == "refuse" and c["key"].endswith("_critical")
                   for c in on_list), \
            "matrix no longer says a `_critical` key ON the list is refused"

    @staticmethod
    def _unread(tmp_path: Path, defaults: list[str], written: list[str]) -> list[str]:
        """The verdict's error findings on a tenant writing every `written` key,
        with NO `optional_overrides:` list at the root (#3924)."""
        root = {"defaults": {k: 80 for k in defaults}}
        (tmp_path / "_defaults.yaml").write_text(yaml.safe_dump(root), encoding="utf-8")
        (tmp_path / "t-1.yaml").write_text(
            yaml.safe_dump({"tenants": {"t-1": {k: "5" for k in written}}}), encoding="utf-8")
        verdict = kv.judge_tree(str(tmp_path))
        return [kv.finding_line(f, 5) for f in verdict.findings if f.severity == kv.ERROR]

    def test_every_shipped_key_is_accepted_with_the_matrix_defaults(self, tmp_path):
        shipped = self._shipped()
        refused = self._unread(tmp_path, self._matrix()["defaults"], shipped)
        assert not refused, (
            "shipped declared keys a tenant still cannot write:\n" + "\n".join(refused))

    def test_every_shipped_key_is_accepted_on_the_declaration_alone(self, tmp_path):
        """`defaults=∅`、也沒有清單——內建層本身就必須足以讓 key 設得進去。"""
        refused = self._unread(tmp_path, [], self._shipped())
        assert not refused, (
            "the built-in layer alone does not make these settable (#3924):\n" + "\n".join(refused))
