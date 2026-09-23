"""Tests for generate_platform_data.py — tenant metadata 整合的 fail-loud 契約。

dev-rules #5（fail-loud validation）：真正的 loader 失敗必須炸掉，不得 silent
swallow。但「刻意缺席」（沒有 conf.d / 沒有 generator script）仍須保留寬容
fallback——這兩者不可混為一談，故正反兩面都釘。

背景：`_load_tenant_metadata()` 原本 `except Exception` 一律吞掉、印個 WARNING
就回傳 ({}, {})，於是 `build_platform_data()` 會整個略過 tenant_metadata /
tenant_groups 兩個 key，`make platform-data` 照樣 exit 0 寫出殘缺的
platform-data.json。殘缺產物一旦被 commit，drift gate 比對的是同樣殘缺的
輸出，就再也擋不住。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DX_DIR = _REPO_ROOT / "scripts" / "tools" / "dx"


def _load_module():
    """每次載入一份新的 module（避免測試間 monkeypatch 互相污染）。"""
    spec = importlib.util.spec_from_file_location(
        "_gpd_under_test", str(_DX_DIR / "generate_platform_data.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestRealFailureIsFatal:
    """真正的失敗 → TenantMetadataError（不是 ({}, {})）。"""

    def test_generator_import_error_raises(self, tmp_path, monkeypatch):
        mod = _load_module()
        broken = tmp_path / "generate_tenant_metadata.py"
        broken.write_text('raise RuntimeError("boom")\n', encoding="utf-8")
        monkeypatch.setattr(mod, "SCRIPT_DIR", tmp_path)

        with pytest.raises(mod.TenantMetadataError, match="boom"):
            mod._load_tenant_metadata()

    def test_build_fn_exception_raises(self, tmp_path, monkeypatch):
        mod = _load_module()
        stub = tmp_path / "generate_tenant_metadata.py"
        stub.write_text(
            "def build_tenant_metadata(config_dir):\n"
            "    raise ValueError('bad yaml')\n",
            encoding="utf-8")
        monkeypatch.setattr(mod, "SCRIPT_DIR", tmp_path)

        with pytest.raises(mod.TenantMetadataError, match="bad yaml"):
            mod._load_tenant_metadata()

    def test_main_exits_nonzero_instead_of_writing_truncated_json(
            self, monkeypatch, capsys):
        """main() 把 TenantMetadataError 轉成乾淨的非 0 exit，不寫殘缺檔案。"""
        mod = _load_module()

        def _boom():
            raise mod.TenantMetadataError("tenant metadata generation failed: boom")

        monkeypatch.setattr(mod, "build_platform_data", _boom)
        monkeypatch.setattr(sys, "argv", ["generate_platform_data.py", "--dry-run"])

        with pytest.raises(SystemExit) as excinfo:
            mod.main()

        assert excinfo.value.code != 0
        assert "boom" in capsys.readouterr().err


class TestIntentionalAbsenceStillFallsBack:
    """刻意缺席 → ({}, {})，不得因為 fail-loud 而誤傷。"""

    def test_missing_conf_d_falls_back(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)  # 沒有 conf.d
        assert mod._load_tenant_metadata() == ({}, {})

    def test_missing_generator_script_falls_back(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.setattr(mod, "SCRIPT_DIR", tmp_path)  # 沒有 generator
        assert mod._load_tenant_metadata() == ({}, {})

    def test_generator_without_build_fn_falls_back(self, tmp_path, monkeypatch):
        mod = _load_module()
        stub = tmp_path / "generate_tenant_metadata.py"
        stub.write_text("# no build_tenant_metadata here\n", encoding="utf-8")
        monkeypatch.setattr(mod, "SCRIPT_DIR", tmp_path)
        assert mod._load_tenant_metadata() == ({}, {})


class TestHappyPath:
    def test_repo_conf_d_yields_tenant_metadata(self):
        mod = _load_module()
        _groups, meta = mod._load_tenant_metadata()
        assert meta, "真實 conf.d 應產出 tenant metadata"


class TestDeclaredKeys:
    """`declaredKeys` — 平台認得但不主張值的那一格，portal 側的資料源（#1321）。

    ⛔ 這裡刻意不重算成員，而是拿 `_registry_lib.shipped_optional_keys_for_packs`
    當斷言來源——那正是 generator 呼叫的同一支，也是兩個 onboarding 生成器與出貨
    `optional_overrides:` 清單用的同一支。若哪天有人在 generator 裡另寫一套判準，
    集合相等就會斷。
    """

    @staticmethod
    def _derived() -> dict:
        ops = str(_REPO_ROOT / "scripts" / "tools" / "ops")
        if ops not in sys.path:
            sys.path.insert(0, ops)
        import scaffold_tenant  # noqa: PLC0415
        from _registry_lib import shipped_optional_keys_for_packs  # noqa: PLC0415

        out = {}
        for pack in scaffold_tenant.RULE_PACKS:
            keys = shipped_optional_keys_for_packs([pack])
            if keys:
                out[pack] = set(keys)
        return out

    def test_members_equal_the_shipped_derivation_per_pack(self):
        mod = _load_module()
        packs = mod.load_scaffold_rule_packs()
        got = {p: {r["key"] for r in rows}
               for p, rows in mod.build_declared_keys(packs).items()}
        assert got == self._derived()

    def test_is_non_vacuous(self):
        # 沒有這條，上面那條在「兩邊都空」時會真空通過。
        assert self._derived(), "推導端不該是空的——registry 有 9 個平鍵"

    def test_never_emits_a_critical_spelling(self):
        # `<base>_critical` 走 resolveCriticalRows 讀 defaults[base]，不屬這一格；
        # 列進來等於在 portal 上廣告一條死路（#1311）。
        mod = _load_module()
        rows = mod.build_declared_keys(mod.load_scaffold_rule_packs())
        emitted = [r["key"] for v in rows.values() for r in v]
        assert [k for k in emitted if k.endswith("_critical")] == []

    def test_row_shape_carries_reference_meta(self):
        # `valueCounterexample` (#1176) is SPARSE — present only for keys whose
        # shipped number has a measured counter-example, absent otherwise, so
        # "no field" reads as "nothing measured" and never as "validated".
        # Required core stays closed: an unexpected field here would reach the
        # portal's hand-written fallback with nothing comparing the two.
        required = {"key", "value", "unit", "desc"}
        optional = {"valueCounterexample"}
        mod = _load_module()
        rows = mod.build_declared_keys(mod.load_scaffold_rule_packs())
        seen_optional = 0
        for pack_rows in rows.values():
            for row in pack_rows:
                assert required <= set(row), row
                assert set(row) <= required | optional, row
                seen_optional += "valueCounterexample" in row
        assert seen_optional, (
            "no declared row carries valueCounterexample — the declared tier is "
            "the one a tenant can fill in TODAY, so a bare reference number "
            "there is the most likely to be copied (#1320 D1)")

    def test_lands_at_the_top_level_not_under_rulepacks(self):
        # 放進 rulePacks[*] 會被 rule-packs.js 那份手寫 fallback 的 per-pack
        # deep-equal gate 拉著要求手抄一份——正是這條線在消滅的東西。
        mod = _load_module()
        data = mod.build_platform_data()
        assert "declaredKeys" in data
        for pack in data["rulePacks"].values():
            assert "declaredKeys" not in pack


class TestThePortalOfflineFallbackIsGenerated:
    """#1226 §2 — the portal's offline Rule Pack catalog is a projection now.

    ⛔ What these cells protect is not the file's content but the CLAIM that
    nothing has to be hand-copied any more. The defect they stand in for: #1215
    added `mysql_replication_lag` to the MariaDB defaults, the hand-typed mirror
    in `rule-packs.js` never got it, and the Vitest gate that compares the two is
    path-gated on `tools/portal/**` — a change that touches no portal file never
    runs it. So the projection is checked here, in a suite with a catch-all path
    filter, and by `--check` in the unconditional `platform-data-check` hook.
    """

    _FALLBACK_REL = (
        "tools/portal/src/interactive/tools/_common/data/rule-packs-fallback.json"
    )
    _DRIFT_GATE_REL = "tools/portal/tests/rule-packs-fallback-drift.test.ts"

    def test_the_fallback_on_disk_is_what_the_generator_would_write(self):
        """Byte equality, so a hand edit cannot survive anywhere."""
        import json

        mod = _load_module()
        expected = json.dumps(
            mod.build_fallback(mod.build_platform_data()),
            indent=2, ensure_ascii=False) + "\n"
        on_disk = (_REPO_ROOT / self._FALLBACK_REL).read_text(encoding="utf-8")
        assert on_disk == expected, (
            f"{self._FALLBACK_REL} is not what the generator produces — run "
            f"`make platform-data`. ⛔ Do NOT edit that file: it is the portal's "
            f"offline catalog and its whole point is that it cannot disagree "
            f"with docs/assets/platform-data.json."
        )

    def test_the_projection_matches_the_field_list_the_portal_gate_compares(self):
        """⛔ The two ends of the same contract, held to each other.

        `_FALLBACK_FIELDS` here decides what the generated file CARRIES;
        `carried()` in the portal's drift gate decides what that gate COMPARES. A
        field added on one side only is invisible in both directions: a new
        carried field nothing compares is ungated, and a compared field nothing
        carries fails on every pack. Reading the gate's own source is what makes
        this a comparison rather than a second transcription of the list.
        """
        import re

        gate_src = (_REPO_ROOT / self._DRIFT_GATE_REL).read_text(encoding="utf-8")
        body = gate_src[gate_src.index("function carried(pack: any)"):]
        body = body[:body.index("\n}")]
        # `label: pack.label,` / `required: pack.required ?? false,`
        compared = set(re.findall(r"^\s{4}(\w+):\s*pack\.", body, re.MULTILINE))
        assert compared, (
            f"read no compared fields out of {self._DRIFT_GATE_REL} — the "
            f"`carried()` projection moved or was renamed. Re-point this test at "
            f"it; do not delete the comparison."
        )
        mod = _load_module()
        carried_here = {name for name, _default in mod._FALLBACK_FIELDS}
        assert carried_here == compared, (
            f"the generated fallback carries {sorted(carried_here)} while the "
            f"portal drift gate compares {sorted(compared)}. The difference is "
            f"either a field nothing checks or a check with nothing behind it:\n"
            f"  only generated: {sorted(carried_here - compared)}\n"
            f"  only compared:  {sorted(compared - carried_here)}"
        )

    def test_pack_and_default_key_ORDER_survives_the_projection(self):
        """Order is user-visible, so it is part of the contract.

        `getAllMetricKeys` iterates `Object.entries(pack.defaults)` and the portal
        renders packs in `packOrder`, so a projection that sorted either would
        reorder autocomplete lists with nothing red to say so.
        """
        mod = _load_module()
        data = mod.build_platform_data()
        fallback = mod.build_fallback(data)

        assert fallback["packOrder"] == data["packOrder"]
        assert list(fallback["rulePacks"]) == data["packOrder"]
        for pack_id in data["packOrder"]:
            source_defaults = data["rulePacks"][pack_id].get("defaults") or {}
            assert (list(fallback["rulePacks"][pack_id]["defaults"])
                    == list(source_defaults)), pack_id
        # …and a pack with several defaults really exists, or the loop above is
        # satisfied by sixteen single-key maps.
        assert max(len(p["defaults"]) for p in fallback["rulePacks"].values()) >= 3

    def test_check_reds_on_a_hand_edited_fallback(self, tmp_path, monkeypatch,
                                                  capsys):
        """The must-fire control for the hook that guards this file.

        ⛔ Driven through `main()` with `--check`, not by calling the comparison
        directly: what protects the file in CI is that entry point, and a test of
        an inner helper would stay green if the `--check` branch stopped reading
        the fallback at all.
        """
        import json

        mod = _load_module()
        dirty = tmp_path / "rule-packs-fallback.json"
        good = mod.build_fallback(mod.build_platform_data())
        first_pack = good["packOrder"][0]
        good["rulePacks"][first_pack]["label"] = "Hand Edited"
        dirty.write_text(json.dumps(good, indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")
        monkeypatch.setattr(mod, "FALLBACK_PATH", dirty)
        monkeypatch.setattr(sys, "argv",
                            ["generate_platform_data.py", "--check"])

        with pytest.raises(SystemExit) as ei:
            mod.main()
        assert ei.value.code != 0
        out = capsys.readouterr().out
        assert "rule-packs-fallback.json" in out
        assert "make platform-data" in out

    def test_the_hook_that_runs_check_actually_watches_both_files(self):
        """⛔ Found by mutation, not by design.

        Deleting the fallback path from `platform-data-check`'s `files:` regex
        left every other cell in this class green: they call the tool, and the
        tool still compares both files. What that regex decides is whether a
        LOCAL `pre-commit` run — which only sees staged paths — reaches the hook
        at all when the fallback is the only thing edited by hand, which is
        precisely the #1226 shape.

        ⚠️ Bounded honestly: CI's Lint job runs this hook with `--all-files` and
        has no `if:`/`needs:`, so the regex is not the last line of defence. What
        it buys is the local red, at commit time, instead of a CI round trip.
        """
        import re

        config = yaml.safe_load(
            (_REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
        hooks = [h for repo in config["repos"] for h in repo.get("hooks", [])
                 if h.get("id") == "platform-data-check"]
        assert len(hooks) == 1, "platform-data-check is not declared exactly once"
        pattern = re.compile(hooks[0]["files"])
        for rel in ("docs/assets/platform-data.json", self._FALLBACK_REL):
            assert pattern.match(rel), (
                f"the platform-data-check hook does not watch {rel}, so editing "
                f"it alone stages no file the hook reacts to and the local "
                f"pre-commit run passes over the change"
            )

    def test_check_passes_on_the_real_pair(self, monkeypatch, capsys):
        """The must-not-fire control: `--check` is not simply always red.

        Without this cell, a `--check` that exited non-zero unconditionally would
        satisfy the one above while making `make platform-data` impossible to
        land.
        """
        mod = _load_module()
        monkeypatch.setattr(sys, "argv",
                            ["generate_platform_data.py", "--check"])
        mod.main()  # returns instead of raising SystemExit
        out = capsys.readouterr().out
        assert "rule-packs-fallback.json is up to date" in out
