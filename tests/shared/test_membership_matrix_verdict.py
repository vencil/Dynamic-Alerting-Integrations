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

pytestmark = pytest.mark.usefixtures("da_guard_env")

_MATRIX = Path(__file__).resolve().parent / "optional_overrides_membership_matrix.json"


def _matrix() -> dict:
    return json.loads(_MATRIX.read_text(encoding="utf-8"))


def _cases() -> list[dict]:
    from _grar_validate import DEPRECATED_KEY_ALIASES
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
    (tmp_path / "_defaults.yaml").write_text(yaml.safe_dump(root), encoding="utf-8")
    (tmp_path / "t-a.yaml").write_text(
        yaml.safe_dump({"tenants": {"t-a": {case["key"]: "5"}}}), encoding="utf-8")
    verdict = kv.judge_tree(str(tmp_path))
    why = [kv.finding_line(f, 5) for f in verdict.findings
           if f.severity == kv.ERROR and f.key == case["key"]]
    checks, unchecked = kv.key_check_lines(verdict)
    assert not unchecked, "this da-guard gives no key checks"
    return why + [msg for kind, _, msg in checks if kind == kv.KEY_CHECK_VERSION_LABEL]


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["name"])
def test_the_shared_verdict_matches_the_matrix(tmp_path, case):
    why = _refused(tmp_path, case)
    got = "refuse" if why else "accept"
    assert got == case["verdict"], (
        f"{case['key']!r} over declared={case['declared']!r}: {got} ({why}), "
        f"the matrix says {case['verdict']} — {case['why']}")


class TestShippedDeclaredListIsSettable:
    """#1311 驗收：出貨的 `optional_overrides:` 清單，每個 key 都要落在
    共用鍵判決的**放行**路徑上（ADR-038 §11）。

    ⭐ 判準來源沿用上面那張判決矩陣的**期望值**，不在本檔另發明一套；下面的
    `test_matrix_still_defines_the_two_verdicts_this_test_leans_on` 就是在釘住那張
    表沒有被改成別的意思。實際執行斷言的是**共用的鍵判決**（ADR-038 §11，
    da-guard 讀 exporter 的結果）：出貨清單上的 key 寫進租戶檔，判決不得讀成
    沒有讀取端讀。

    讀的是**產物**（helm values ＋ dev `_defaults.yaml` ＋ try-local seed），不是
    registry 的 `tier: optional_overrides` 宣告——registry 說我們想出貨什麼，產物
    才是 operator 真的拿到什麼，而 #1310 之前兩者相差整整一個清單。

    ⛔ 為什麼 `defaults` 要驗兩態：宣告清單的意義是「**光憑列在清單上**就設得
    進去」。若某個 key 只有在它**同時**還躺在 `defaults:` 裡才被放行，那讓它
    settable 的是舊的那一格、不是這一格，清單對它就是裝飾。所以除了矩陣自己
    的 defaults fixture，另跑一次 `defaults=∅`——這也正是 `<base>_critical`
    被排除的機械理由（`resolveCriticalRows` 讀 `defaults[base]`），任何人把一
    個 `_critical` 塞進清單，本測試就會紅。
    """

    _HELM_VALUES = "helm/threshold-exporter/values.yaml"
    _DEV_DEFAULTS = "components/threshold-exporter/config/conf.d/_defaults.yaml"
    # ⛔ The third copy. NOT hand-written any more (it was until #1176/PR #1344):
    # the seed carries its own generated block — surface id `try-local-optional`
    # — and `check_threshold_registry.py --regen` splices it like the other two,
    # so `--ci`'s stale-surface check is a second gate on this file.
    #
    # The two gates are complementary, not redundant — measured, not assumed:
    #   * drop a key INSIDE the markers → BOTH red (--ci stale-surface + the
    #     equality assertion below);
    #   * append a key AFTER the `<<<` end marker → --ci exits 0 (it diffs the
    #     marker-delimited block, not the key), and the assertion below is the
    #     only thing that fails, because it reads the PARSED list.
    # try-local is the showcase a reader meets first, so a list there that
    # disagrees with the shipped surfaces teaches the wrong contract; that
    # second failure mode has no other reader.
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
        return cls._shipped_from(
            cls._HELM_VALUES,
            lambda d: (d.get("thresholdConfig") or {}).get("optional_overrides") or [])

    def test_the_shipped_artifacts_carry_the_same_non_empty_list(self):
        """空清單會讓底下每個迴圈變成永遠綠——本 repo 一再抓到的形狀。三份產物
        （#1176/PR #1344 起連 try-local seed 在內）都由同一個 renderer 生成，
        內容分歧代表其中一面漏跑 `--regen`。這條斷言不是它們唯一的 gate——`--ci`
        的 stale-surface 也擋——但比對的是**解析後**的清單而非 marker 內的區塊
        文字，所以 marker 之外被塞進同一個 key 的項目只有這裡會紅。"""
        helm = self._shipped()
        dev = self._shipped_from(
            self._DEV_DEFAULTS, lambda d: d.get("optional_overrides") or [])
        seed = self._shipped_from(
            self._TRY_LOCAL_SEED, lambda d: d.get("optional_overrides") or [])
        assert helm, f"{self._HELM_VALUES} thresholdConfig.optional_overrides is empty"
        assert helm == dev == seed, (
            f"shipped declared list diverges across the surfaces:\n"
            f"  {self._HELM_VALUES}: {helm}\n  {self._DEV_DEFAULTS}: {dev}\n"
            f"  {self._TRY_LOCAL_SEED}: {seed}")

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
    def _unread(tmp_path: Path, defaults: list[str], declared: list[str]) -> list[str]:
        """The verdict's error findings on a tenant writing every `declared` key."""
        root = {"defaults": {k: 80 for k in defaults}, "optional_overrides": declared}
        (tmp_path / "_defaults.yaml").write_text(yaml.safe_dump(root), encoding="utf-8")
        (tmp_path / "t-1.yaml").write_text(
            yaml.safe_dump({"tenants": {"t-1": {k: "5" for k in declared}}}), encoding="utf-8")
        verdict = kv.judge_tree(str(tmp_path))
        return [kv.finding_line(f, 5) for f in verdict.findings if f.severity == kv.ERROR]

    def test_every_shipped_key_is_accepted_with_the_matrix_defaults(self, tmp_path):
        shipped = self._shipped()
        refused = self._unread(tmp_path, self._matrix()["defaults"], shipped)
        assert not refused, (
            "shipped declared keys a tenant still cannot write:\n" + "\n".join(refused))

    def test_every_shipped_key_is_accepted_on_the_declaration_alone(self, tmp_path):
        """`defaults=∅` — 清單本身就必須足以讓 key 設得進去。"""
        refused = self._unread(tmp_path, [], self._shipped())
        assert not refused, (
            "declared-list membership alone does not make these settable — the "
            "list is decoration for them (#1311):\n" + "\n".join(refused))
