"""
tests/ops/test_deprecate_rule_carriers.py — deprecate_rule writes EVERY
defaults carrier and says 下架完成 only when the key is really gone
(#1609, #1787).

`ThresholdConfig.Defaults` is `map[string]float64`: a string under a root
`defaults:` makes `parsePartialConfig` reject the whole carrier, so 下架
removes the metric's keys, the completion claim is pinned at KEY level, and a
carrier health check mirrors the exporter's decoder. Go/Python links:
`tests/golden/fixtures/deprecate-rule/before.yaml` →
`tests/golden/fixtures/deprecate-rule/after.yaml` (one carrier before and
after the tool; not named `_defaults.yaml` so the reachability lint does not
count it as a shipped conf.d artifact) and
`tests/golden/fixtures/defaults-carrier-oracle.json` (health-check truth
table, Go side is the judge).

Driven through `subprocess` because the contract under test is the
operator-visible report (stdout + rc), not a return value.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import deprecate_rule  # noqa: E402

TOOL = Path(deprecate_rule.__file__).resolve()
GOLDEN = Path(__file__).resolve().parents[1] / "golden" / "fixtures"


def _write(root: Path, name: str, body: str) -> None:
    (root / name).write_text(body, encoding="utf-8")


def _ticket_fixture(root: Path) -> Path:
    """The fixture from #1609: two spellings, deliberately different values."""
    root.mkdir()
    _write(root, "_defaults.yaml", "defaults:\n  cpu_usage: 80\n")
    _write(root, "_defaults.yml", "defaults:\n  cpu_usage: 90\n")
    _write(root, "alpha.yaml", "tenants:\n  alpha:\n    cpu_usage: 85\n")
    return root


def _run_metrics(config_dir: Path, metrics: list[str],
                 *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOL), *metrics, "--config-dir",
         str(config_dir), *extra],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"})


def _run(config_dir: Path, *extra: str) -> subprocess.CompletedProcess:
    return _run_metrics(config_dir, ["cpu_usage"], *extra)


def _step1_lines(stdout: str) -> list[str]:
    return [ln.strip() for ln in stdout.splitlines()
            if ln.strip().startswith("Step 1:")]


def _tail(stdout: str) -> str:
    assert "下架未完成" in stdout, stdout
    return stdout.split("下架未完成", 1)[1]


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob("*") if p.is_file()}


# ===================================================================
# #1609 — every carrier, and the claim only when found == changed
# ===================================================================


def test_execute_writes_every_carrier_and_only_then_says_complete(tmp_path):
    """(a) The ticket fixture: the key is gone from both spellings, rc 0."""
    root = _ticket_fixture(tmp_path / "conf.d")

    r = _run(root, "--execute")

    assert r.returncode == 0, r.stdout + r.stderr
    assert _step1_lines(r.stdout) == ["Step 1: _defaults.yaml",
                                      "Step 1: _defaults.yml"], r.stdout
    assert "下架完成" in r.stdout
    assert "下架未完成" not in r.stdout
    for name in ("_defaults.yaml", "_defaults.yml"):
        text = (root / name).read_text(encoding="utf-8")
        assert "cpu_usage" not in text, (
            f"{name} still carries the metric:\n{text}")
        assert "disable" not in text, (
            f"{name} was written with the sentinel instead of losing the "
            f"key:\n{text}")
    alpha = (root / "alpha.yaml").read_text(encoding="utf-8")
    assert "cpu_usage" not in alpha, alpha


def test_preview_names_both_carriers_and_writes_nothing(tmp_path):
    """(b) Preview mode names every carrier it WOULD write, rc 0, no writes."""
    root = _ticket_fixture(tmp_path / "conf.d")
    before = _tree_bytes(root)

    r = _run(root)

    assert r.returncode == 0, r.stdout + r.stderr
    assert _step1_lines(r.stdout) == ["Step 1: _defaults.yaml",
                                      "Step 1: _defaults.yml"], r.stdout
    assert "預覽模式" in r.stdout
    assert _tree_bytes(root) == before, "preview mode wrote to the tree"


def test_an_unreadable_carrier_degrades_the_whole_run_to_preview(tmp_path):
    """(c) One carrier the exporter cannot read → the run writes NOTHING,
    names it, rc 1 — the other carrier is left as it was too.

    ⚠️ The unreadable shape is a broken YAML document, not mode bits: the
    suite runs as root, where `chmod` denies nothing.
    """
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml", "defaults:\n  cpu_usage: 80\n")
    _write(root, "_defaults.yml", "defaults: [\n")
    _write(root, "alpha.yaml", "tenants:\n  alpha:\n    cpu_usage: 85\n")
    before = _tree_bytes(root)

    r = _run(root, "--execute")

    assert r.returncode == 1, r.stdout + r.stderr
    assert "下架完成！" not in r.stdout, r.stdout
    assert "本輪降級為預覽" in r.stdout, r.stdout
    tail = _tail(r.stdout)
    assert tail.count("_defaults.yml") == 1, tail   # 同一檔同一原因只列一次
    assert _tree_bytes(root) == before, "a degraded run wrote to the tree"


def test_single_carrier_control_keeps_todays_shape(tmp_path):
    """(d) One `_defaults.yaml` → one Step 1 line, 下架完成, rc 0."""
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml", "defaults:\n  cpu_usage: 80\n")
    _write(root, "alpha.yaml", "tenants:\n  alpha:\n    cpu_usage: 85\n")

    r = _run(root, "--execute")

    assert r.returncode == 0, r.stdout + r.stderr
    assert _step1_lines(r.stdout) == ["Step 1: _defaults.yaml"], r.stdout
    assert "下架完成" in r.stdout
    assert "下架未完成" not in r.stdout
    text = (root / "_defaults.yaml").read_text(encoding="utf-8")
    assert "cpu_usage" not in text, text
    assert "已移除 cpu_usage（原值: 80）" in r.stdout, r.stdout


def test_no_carrier_control_names_the_canonical_path_and_is_incomplete(
        tmp_path):
    """(e) No defaults carrier at all → Step 1 names `_defaults.yaml`,
    reports 不存在, and the run is incomplete (rc 1)."""
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "alpha.yaml", "tenants:\n  alpha:\n    cpu_usage: 85\n")

    r = _run(root, "--execute")

    assert _step1_lines(r.stdout) == ["Step 1: _defaults.yaml"], r.stdout
    assert "_defaults.yaml 不存在" in r.stdout, r.stdout
    assert r.returncode == 1, r.stdout + r.stderr
    assert "下架未完成" in r.stdout, r.stdout
    assert "下架完成！" not in r.stdout, r.stdout
    # Step 2 still ran: the tenant override was removed.
    alpha = (root / "alpha.yaml").read_text(encoding="utf-8")
    assert "cpu_usage" not in alpha, alpha


def test_defaults_carriers_returns_exact_names_only_sorted(tmp_path):
    """(f) Exact-name carriers in any casing; prefix cousins, tenants and
    directories are not carriers."""
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml", "defaults: {}\n")
    _write(root, "_DEFAULTS.YML", "defaults: {}\n")
    _write(root, "_defaults-multidb.yaml", "defaults: {}\n")
    _write(root, "alpha.yaml", "tenants: {}\n")
    (root / "sub").mkdir()
    _write(root / "sub", "_defaults.yml", "defaults: {}\n")

    got = deprecate_rule.defaults_carriers(root)

    assert got == [root / "_DEFAULTS.YML", root / "_defaults.yaml"], got


def test_non_carrier_files_with_a_defaults_block_follow_the_exporter_rule(
        tmp_path):
    """A tenant file's `defaults:` is dropped by the exporter → named, not
    counted; a `_`-prefixed non-carrier IS merged in but this tool may not
    write it → incomplete, by name, with a reason that says so."""
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml", "defaults:\n  cpu_usage: 80\n")
    _write(root, "alpha.yaml",
           "defaults:\n  cpu_usage: 70\ntenants:\n  alpha:\n    cpu_usage: 85\n")

    r = _run(root, "--execute")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "下架完成" in r.stdout
    assert "alpha.yaml 的 defaults 區塊 exporter 會丟棄" in r.stdout, r.stdout
    assert "未寫入" not in r.stdout, r.stdout

    root2 = tmp_path / "conf.d2"
    root2.mkdir()
    _write(root2, "_defaults.yaml", "defaults:\n  cpu_usage: 80\n")
    _write(root2, "_defaults-multidb.yaml", "defaults:\n  cpu_usage: 60\n")

    r = _run(root2, "--execute")

    assert r.returncode == 1, r.stdout + r.stderr
    tail = _tail(r.stdout)
    assert "_defaults-multidb.yaml" in tail and "非 defaults 載體" in tail, tail
    assert "未寫入" not in tail, tail
    assert "cpu_usage" not in (
        root2 / "_defaults.yaml").read_text(encoding="utf-8")


def test_a_tenant_file_hit_names_the_block_the_exporter_drops(tmp_path):
    """The warning names the block that was hit — `optional_overrides`, not a
    `defaults` block the file does not have."""
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml", "defaults:\n  cpu_usage: 80\n")
    _write(root, "alpha.yaml", "optional_overrides:\n- cpu_usage\n")

    r = _run(root, "--execute")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "alpha.yaml 的 optional_overrides 區塊 exporter 會丟棄" in r.stdout, r.stdout
    assert "alpha.yaml 的 defaults 區塊" not in r.stdout, r.stdout


def test_a_carrier_whose_defaults_key_is_null_does_not_traceback(tmp_path):
    """`defaults:` with no value is an empty block: named no-op, bytes kept."""
    root = tmp_path / "conf.d"
    root.mkdir()
    body = "# header\ndefaults:\n"
    _write(root, "_defaults.yaml", body)

    r = _run(root, "--execute")

    assert "Traceback" not in r.stderr, r.stderr
    assert r.returncode == 0, r.stdout + r.stderr
    assert "_defaults.yaml 沒有 cpu_usage 相關 key，略過" in r.stdout, r.stdout
    assert (root / "_defaults.yaml").read_text(encoding="utf-8") == body


def test_a_carrier_whose_defaults_is_not_a_mapping_is_refused_unchanged(tmp_path):
    """`defaults:` as a list is a broken file the exporter cannot decode
    either: refuse by name, rc 1, bytes untouched — preview too."""
    root = tmp_path / "conf.d"
    root.mkdir()
    body = "# hdr\ndefaults:\n  - cpu_usage\n  - mem\n"
    _write(root, "_defaults.yaml", body)

    for mode in ((), ("--execute",)):
        r = _run(root, *mode)
        assert "Traceback" not in r.stderr, r.stderr
        assert r.returncode == 1, r.stdout + r.stderr
        assert "下架未完成" in r.stdout, r.stdout
        assert "_defaults.yaml" in r.stdout and "不是 mapping（list）" in r.stdout, r.stdout
        assert "下架完成" not in r.stdout.replace("下架未完成", ""), r.stdout
        assert (root / "_defaults.yaml").read_text(encoding="utf-8") == body


# ===================================================================
# #1787 — 下架＝刪 key。以下釘的是「刪掉了什麼」與「沒有新增什麼」。
# ===================================================================


def test_a_carrier_holding_only_the_critical_variant_loses_it_and_gains_nothing(
        tmp_path):
    """只有 `cpu_usage_critical` 的載體：該 key 被刪，且**沒有**新增 base key。"""
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml",
           "defaults:\n  cpu_usage_critical: 95\n  mem_usage: 90\n")

    r = _run(root, "--execute")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "已移除 cpu_usage_critical（原值: 95）" in r.stdout, r.stdout
    data = yaml.safe_load((root / "_defaults.yaml").read_text(encoding="utf-8"))
    assert "cpu_usage_critical" not in data["defaults"], data
    assert "cpu_usage" not in data["defaults"], data
    assert data["defaults"] == {"mem_usage": 90}, data


def test_a_root_without_the_metric_is_a_named_noop_byte_for_byte(tmp_path):
    """root 本來就沒有這個 metric ⇒ 具名回報，且這條 no-op 路徑位元組不變。

    ⛔ 射程只到 no-op 路徑：真的有 key 要刪時寫回是 header 註解之外的整份
    `safe_dump`——其餘註解消失、YAML 1.1 拼法的純量改型。
    """
    root = tmp_path / "conf.d"
    root.mkdir()
    body = ("# platform defaults\n"
            "defaults:\n"
            "  mem_usage: 90\n"
            "state_filters:\n"
            "  container_crashloop:\n"
            "    reasons: [\"CrashLoopBackOff\"]\n"
            "    severity: \"critical\"\n")
    _write(root, "_defaults.yaml", body)
    before = (root / "_defaults.yaml").read_bytes()

    r = _run(root, "--execute")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "_defaults.yaml 沒有 cpu_usage 相關 key，略過" in r.stdout, r.stdout
    assert (root / "_defaults.yaml").read_bytes() == before, (
        "a carrier with nothing to do was rewritten")


def test_the_tool_output_is_the_golden_after_file(tmp_path):
    """`deprecate-rule/before.yaml` as `_defaults.yaml` → tool → bytes equal
    `deprecate-rule/after.yaml`.

    The Go side (`config_deprecation_terminal_shape_test.go`) loads the same
    `after.yaml` through the exporter's loader — that is the byte-level link
    between what the tool writes and what the exporter reads.
    """
    root = tmp_path / "conf.d"
    root.mkdir()
    shutil.copy(GOLDEN / "deprecate-rule" / "before.yaml", root / "_defaults.yaml")

    r = _run(root, "--execute")

    assert r.returncode == 0, r.stdout + r.stderr
    want = (GOLDEN / "deprecate-rule" / "after.yaml").read_bytes()
    assert (root / "_defaults.yaml").read_bytes() == want, (
        (root / "_defaults.yaml").read_text(encoding="utf-8"))


def test_a_key_left_in_a_subdirectory_withholds_the_claim_until_that_subtree_is_run(
        tmp_path):
    """本工具不遞迴寫入，但完成度重掃往下看：子目錄裡的殘留具名、rc 1，並指出
    要對哪個子樹跑 `--plane subtree`；跑過之後 root 再跑就是 rc 0。"""
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml", "defaults:\n  cpu_usage: 80\n  mem_usage: 90\n")
    sub = root / "finance"
    sub.mkdir()
    _write(sub, "_defaults.yaml", "defaults:\n  cpu_usage: 60\n")

    r = _run(root, "--execute")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "cpu_usage" not in (root / "_defaults.yaml").read_text(encoding="utf-8")
    tail = _tail(r.stdout)
    assert "finance/_defaults.yaml" in tail, tail
    assert f"--config-dir {sub} --plane subtree" in tail, tail
    assert "cpu_usage: 60" in (sub / "_defaults.yaml").read_text(encoding="utf-8")

    r2 = _run(sub, "--plane", "subtree", "--execute")
    assert r2.returncode == 0, r2.stdout + r2.stderr
    sub_text = (sub / "_defaults.yaml").read_text(encoding="utf-8")
    assert "cpu_usage" not in sub_text, sub_text
    assert "disable" not in sub_text, sub_text

    r3 = _run(root, "--execute")
    assert r3.returncode == 0, r3.stdout + r3.stderr
    assert "下架完成" in r3.stdout, r3.stdout


def test_a_nested_tenant_files_platform_block_is_a_warning_not_a_residue(tmp_path):
    """子目錄裡租戶檔的 `defaults:` 區塊 exporter 依 basename 丟棄：只警告、
    rc 0。成對反例：同一個區塊放在子樹自有 `_defaults.yaml` 才是殘留。"""
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml", "defaults:\n  mem_usage: 90\n")
    (root / "finance").mkdir()
    _write(root / "finance", "t1.yaml",
           "defaults:\n  cpu_usage: 70\ntenants:\n  t1: {}\n")

    r = _run(root, "--execute")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "finance/t1.yaml 的 defaults 區塊 exporter 會丟棄" in r.stdout, r.stdout

    _write(root / "finance", "_defaults.yaml", "defaults:\n  cpu_usage: 70\n")
    r2 = _run(root, "--execute")
    assert r2.returncode == 1, r2.stdout + r2.stderr
    assert "finance/_defaults.yaml" in _tail(r2.stdout), r2.stdout


def test_a_subtree_without_its_own_carrier_follows_the_guidance_to_green(tmp_path):
    """子目錄租戶檔的 `tenants:` 殘留：root 指引對該子樹跑 `--plane subtree`；
    那一跑沒有載體不算失敗（跳過 Step 1）、刪掉租戶 key、rc 0；root 再跑 rc 0。
    成對反例：`--plane root` 下沒有載體仍是 rc 1。"""
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml", "defaults:\n  cpu_usage: 80\n")
    sub = root / "finance"
    sub.mkdir()
    _write(sub, "t1.yaml", "tenants:\n  t1:\n    cpu_usage: '70'\n")

    r = _run(root, "--execute")
    assert r.returncode == 1, r.stdout + r.stderr
    assert f"--config-dir {sub} --plane subtree" in _tail(r.stdout), r.stdout

    r2 = _run(sub, "--plane", "subtree", "--execute")
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert "此子樹沒有自己的載體，跳過" in r2.stdout, r2.stdout
    assert "不存在" not in r2.stdout, r2.stdout
    assert "cpu_usage" not in (sub / "t1.yaml").read_text(encoding="utf-8")

    r3 = _run(root, "--execute")
    assert r3.returncode == 0, r3.stdout + r3.stderr

    r4 = _run(sub, "--plane", "root", "--execute")
    assert r4.returncode == 1, r4.stdout + r4.stderr
    assert "_defaults.yaml 不存在" in r4.stdout, r4.stdout


def test_a_nested_underscore_file_other_than_defaults_is_not_read_by_the_exporter(
        tmp_path):
    """子目錄裡 `_defaults` 以外的 `_` 檔 exporter 完全不讀（isNestedPlatformFile）：
    只警告「不讀」、rc 0。成對反例：同一內容改名 `_defaults.yaml` 就是子樹殘留
    rc 1。"""
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml", "defaults:\n  cpu_usage: 80\n")
    (root / "finance").mkdir()
    _write(root / "finance", "_shared.yaml", "defaults:\n  cpu_usage: 60\n")

    r = _run(root, "--execute")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "finance/_shared.yaml exporter 不讀子目錄裡" in r.stdout, r.stdout
    assert "併進全域" not in r.stdout, r.stdout

    (root / "finance" / "_shared.yaml").rename(root / "finance" / "_defaults.yaml")
    r2 = _run(root, "--execute")
    assert r2.returncode == 1, r2.stdout + r2.stderr
    assert "finance/_defaults.yaml" in _tail(r2.stdout), r2.stdout


def test_a_reference_the_writer_cannot_clear_blocks_the_completion_claim(
        tmp_path):
    """`_shared.yaml` 的 `tenants:` 區塊：掃描看得見、Step 2 依設計不寫 ⇒ rc 1、
    具名「請手動移除」，不是「重掃仍有引用」；載體那一半仍然被清掉。"""
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml", "defaults:\n  cpu_usage: 80\n")
    _write(root, "_shared.yaml", "tenants:\n  shared:\n    cpu_usage: \"70\"\n")

    r = _run(root, "--execute")

    assert r.returncode == 1, r.stdout + r.stderr
    assert "下架完成！" not in r.stdout, r.stdout
    tail = _tail(r.stdout)
    assert "_shared.yaml" in tail, tail
    assert "tenants.shared" in tail and "cpu_usage" in tail, tail
    assert "本工具依設計不寫任何 `_` 前綴檔的 `tenants:` 區塊，請手動移除" in tail, tail
    assert "重掃仍有引用" not in tail, tail
    assert "cpu_usage" not in (root / "_defaults.yaml").read_text(encoding="utf-8")


def test_a_profile_holding_the_key_is_out_of_reach_in_both_modes(tmp_path):
    """`_profiles.yaml` 的 `profiles:` 平面：掃描列出、兩種模式都判「射程外」rc 1。"""
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml", "defaults:\n  cpu_usage: 80\n")
    _write(root, "_profiles.yaml", "profiles:\n  p1:\n    cpu_usage: '70'\n")
    _write(root, "alpha.yaml", "tenants:\n  alpha:\n    _profile: p1\n")
    before = _tree_bytes(root)

    preview = _run(root)
    assert preview.returncode == 1, preview.stdout + preview.stderr
    assert "[profiles.p1] cpu_usage" in preview.stdout, preview.stdout
    tail = _tail(preview.stdout)
    assert "_profiles.yaml" in tail and "`profiles:` 區塊本工具射程外" in tail, tail
    assert _tree_bytes(root) == before

    executed = _run(root, "--execute")
    assert executed.returncode == 1, executed.stdout + executed.stderr
    assert _tail(executed.stdout) == tail, executed.stdout
    assert "cpu_usage" not in (root / "_defaults.yaml").read_text(encoding="utf-8")


def test_an_unreadable_tenant_file_withholds_the_claim_and_is_named_once(tmp_path):
    """讀不了的租戶檔不是 rc 0：它是一個「量不到」的引用，具名、rc 1，並且
    只在掃描列表與結尾各具名一次。"""
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml", "defaults:\n  cpu_usage: 80\n")
    _write(root, "alpha.yaml", "cpu_usage: [\n")

    r = _run(root, "--execute")

    assert "Traceback" not in r.stderr, r.stderr
    assert r.returncode == 1, r.stdout + r.stderr
    head, tail = r.stdout.split("下架未完成", 1)
    assert head.count("無法讀取") == 1, head
    assert "alpha.yaml" in tail and "無法讀取" in tail, tail
    assert "本工具無法判定" in tail, tail
    # 載體那一半仍然被清掉。
    assert "cpu_usage" not in (root / "_defaults.yaml").read_text(encoding="utf-8")


def test_odd_tenant_file_shapes_do_not_traceback(tmp_path):
    """`tenants:` 為空值的檔是空的；頂層是 list 的檔是讀不了的引用。"""
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml", "defaults:\n  cpu_usage: 80\n")
    _write(root, "alpha.yaml", "tenants:\n")

    r = _run(root, "--execute")
    assert "Traceback" not in r.stderr, r.stderr
    assert r.returncode == 0, r.stdout + r.stderr

    _write(root, "_defaults.yaml", "defaults:\n  cpu_usage: 80\n")
    _write(root, "beta.yaml", "- alpha\n")
    r2 = _run(root, "--execute")
    assert "Traceback" not in r2.stderr, r2.stderr
    assert r2.returncode == 1, r2.stdout + r2.stderr
    assert "beta.yaml" in _tail(r2.stdout), r2.stdout


# ===================================================================
# #1787 契約鏡射：exporter 讀得下去嗎（真值表在 golden fixture）
# ===================================================================

_ORACLE = json.loads((GOLDEN / "defaults-carrier-oracle.json").read_text(
    encoding="utf-8"))["cases"]
_ORACLE_BY_NAME = {c["name"]: c for c in _ORACLE}
assert len(_ORACLE_BY_NAME) == len(_ORACLE), "duplicate case name in the oracle"


# yaml.v3 是 libyaml 的移植；這幾格 pure-Python parser 的判定不同（tab）。
_LIBYAML_ONLY = {"trailing-tab", "tab-after-key-colon", "tab-in-flow"}


@pytest.mark.parametrize("case_name", sorted(_ORACLE_BY_NAME))
def test_carrier_health_matches_the_exporter(case_name):
    """產線的 `carrier_health` 對每一格給出 Go `TestDefaultsCarrierOracle`
    量到的判定：accepted ⇒ 無項；dropped ⇒ 恰一個 UNPARSEABLE（結構性的 key
    為 None）；zero ⇒ 恰一個 DECODES_TO_ZERO。帶 `python` 覆寫欄位的格是刻意的
    fail-closed 近似（Go 接受、本工具整檔擋），以覆寫為準。"""
    case = _ORACLE_BY_NAME[case_name]
    if case_name in _LIBYAML_ONLY and not yaml.__with_libyaml__:
        pytest.skip(f"{case_name}: 沒有 libyaml，pure parser 對 tab 的判定與 yaml.v3 不同")
    got = deprecate_rule.exporter_verdicts(case["doc"].encode("utf-8"))
    want, key = case["exporter"], case["key"]
    if "python" in case:
        assert case["python"] == "unparseable", case
        want, key = "dropped", None
    if want == "accepted":
        assert got == [], f"{case_name}: {case['doc']!r} → {got}"
        return
    assert len(got) == 1, f"{case_name}: {case['doc']!r} → {got}"
    got_key, _raw, kind = got[0]
    assert got_key == key, f"{case_name}: {got}"
    assert kind == (deprecate_rule.UNPARSEABLE if want == "dropped"
                    else deprecate_rule.DECODES_TO_ZERO), f"{case_name}: {got}"


def test_carrier_health_invalid_utf8_bytes():
    """非 UTF-8 位元組進不了 JSON 表：與 Go 側同名的一格，整檔 UNPARSEABLE。"""
    got = deprecate_rule.carrier_health(b"defaults:\n  k: 80\n  neighbour: \xe9\n")
    assert len(got) == 1, got
    assert got[0][0] is None and got[0][2] == deprecate_rule.UNPARSEABLE, got


def test_the_written_root_defaults_still_decodes_as_map_string_float64(tmp_path):
    """執行後 root `_defaults.yaml` 餵產線的 `carrier_health` → 無項，並附一支
    合成負向控制（`disable` 哨兵餵同一支 → 紅）。"""
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml",
           "defaults:\n  cpu_usage: 80\n  cpu_usage_critical: 95\n"
           "  mem_usage: 90\n  disk_usage: 85\n")

    r = _run(root, "--execute")
    assert r.returncode == 0, r.stdout + r.stderr

    written = (root / "_defaults.yaml").read_bytes()
    assert deprecate_rule.carrier_health(written) == [], written
    assert yaml.safe_load(written)["defaults"] == {"mem_usage": 90,
                                                   "disk_usage": 85}, written

    old_behaviour = (b"defaults:\n  cpu_usage: disable\n"
                     b"  cpu_usage_critical: 95\n  mem_usage: 90\n"
                     b"  disk_usage: 85\n")
    assert deprecate_rule.carrier_health(old_behaviour) == [
        ("cpu_usage", "disable", deprecate_rule.UNPARSEABLE)], (
        "the same checker must call the old shape out")


# ===================================================================
# 述詞與批次
# ===================================================================


def test_a_neighbouring_metric_sharing_the_prefix_is_not_this_metrics_key(
        tmp_path):
    """`container_cpu` 的下架不能被 `container_cpu_throttle_critical` 卡住，
    也不得刪它——那是另一個指標的資料。"""
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml",
           "defaults:\n  container_cpu: 80\n  container_cpu_throttle: 25\n")
    _write(root, "alpha.yaml",
           "tenants:\n  alpha:\n    container_cpu_critical: '95'\n"
           "    container_cpu_throttle_critical: '50'\n"
           "    container_cpu_throttle{pod=\"x\"}: '60'\n")

    r = _run_metrics(root, ["container_cpu"], "--execute")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "下架完成" in r.stdout, r.stdout
    assert "下架未完成" not in r.stdout, r.stdout
    defaults = yaml.safe_load(
        (root / "_defaults.yaml").read_text(encoding="utf-8"))["defaults"]
    assert defaults == {"container_cpu_throttle": 25}, defaults
    tenant = yaml.safe_load(
        (root / "alpha.yaml").read_text(encoding="utf-8"))["tenants"]["alpha"]
    assert "container_cpu_critical" not in tenant, tenant
    assert tenant["container_cpu_throttle_critical"] == "50", tenant
    assert tenant['container_cpu_throttle{pod="x"}'] == "60", tenant


def test_the_health_residue_subtracts_every_metric_in_the_batch(tmp_path):
    """`deprecate cpu_usage old_metric` 對 `old_metric: disable` 是 rc 0：體檢的
    殘留扣掉的是**整次調用**要刪的 key，不是正在處理的那一個 metric 的。"""
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml",
           "defaults:\n  cpu_usage: 80\n  old_metric: disable\n  mem_usage: 90\n")
    _write(root, "alpha.yaml", "tenants:\n  alpha:\n    cpu_usage: '85'\n")

    r = _run_metrics(root, ["cpu_usage", "old_metric"], "--execute")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "下架完成" in r.stdout, r.stdout
    assert "下架未完成" not in r.stdout, r.stdout
    defaults = yaml.safe_load(
        (root / "_defaults.yaml").read_text(encoding="utf-8"))["defaults"]
    assert defaults == {"mem_usage": 90}, defaults


def test_a_plain_batch_of_two_live_metrics_stays_clean(tmp_path):
    """上一支的合法呼叫對照：兩個普通 metric 一次下架仍是 rc 0。"""
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml",
           "defaults:\n  cpu_usage: 80\n  mem_usage: 90\n  disk_usage: 85\n")
    _write(root, "alpha.yaml",
           "tenants:\n  alpha:\n    cpu_usage: '85'\n    mem_usage: '70'\n")

    r = _run_metrics(root, ["cpu_usage", "mem_usage"], "--execute")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "下架完成" in r.stdout, r.stdout
    defaults = yaml.safe_load(
        (root / "_defaults.yaml").read_text(encoding="utf-8"))["defaults"]
    assert defaults == {"disk_usage": 85}, defaults


# ===================================================================
# 載體體檢：紅則整輪降級為預覽
# ===================================================================


def test_a_non_numeric_residue_from_another_metric_withholds_the_claim(
        tmp_path):
    """載體還留著**別的** metric 的非數值 defaults ⇒ rc 1、具名、本輪不寫；
    本輪自己要刪的那個 key 不算殘留。"""
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml",
           "defaults:\n  cpu_usage: 80\n  old_metric: disable\n  mem_usage: 90\n")

    r = _run(root, "--execute")

    assert r.returncode == 1, r.stdout + r.stderr
    assert "將在下次 reload 時生效" not in r.stdout, r.stdout
    tail = _tail(r.stdout)
    assert "_defaults.yaml" in tail and "old_metric" in tail, tail
    assert "值 disable exporter 讀不成數字" in tail, tail
    assert "map[string]float64" in tail, tail
    assert "本輪降級為預覽" in r.stdout, r.stdout
    assert "cpu_usage: 80" in (
        root / "_defaults.yaml").read_text(encoding="utf-8")

    root2 = tmp_path / "conf.d2"
    root2.mkdir()
    _write(root2, "_defaults.yaml",
           "defaults:\n  cpu_usage: disable\n  mem_usage: 90\n")
    r2 = _run(root2, "--execute")
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert "下架完成" in r2.stdout, r2.stdout


def test_a_blocked_execute_run_is_identical_to_preview(tmp_path):
    """體檢紅 ⇒ 整輪降級為預覽：租戶檔也不寫、動作標籤跟著、理由集合與預覽
    相同——同一棵樹兩種模式 (a) 全樹位元組相同 (b) rc 相同 (c) 結尾相同。"""
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml", "defaults:\n  cpu_usage: 80\n  neighbour: 1:30\n")
    _write(root, "alpha.yaml",
           "tenants:\n  alpha:\n    cpu_usage: \"85\"\n    window: 1:30\n")
    before = _tree_bytes(root)

    preview = _run(root)
    assert preview.returncode == 1, preview.stdout + preview.stderr
    assert _tree_bytes(root) == before

    executed = _run(root, "--execute")
    assert executed.returncode == preview.returncode, executed.stdout
    assert _tree_bytes(root) == before, "a degraded run wrote to the tree"
    assert _tail(executed.stdout) == _tail(preview.stdout), executed.stdout
    assert "本輪降級為預覽" in executed.stdout, executed.stdout
    assert "將移除（本輪未寫入）" in executed.stdout, executed.stdout
    assert "已移除" not in executed.stdout, executed.stdout
    assert "重掃仍有引用" not in executed.stdout, executed.stdout


def test_a_healthy_carrier_is_still_written(tmp_path):
    """成對反例：沒有殘留時照常寫，`--plane subtree` 也照常寫。"""
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml", "defaults:\n  cpu_usage: 80\n  mem_usage: 90\n")

    r = _run(root, "--execute")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "cpu_usage" not in (root / "_defaults.yaml").read_text(encoding="utf-8")

    sub = tmp_path / "sub"
    sub.mkdir()
    _write(sub, "_defaults.yaml",
           'defaults:\n  cpu_usage: 80\n  _state_maintenance: "disable"\n')
    r2 = _run(sub, "--plane", "subtree", "--execute")
    assert r2.returncode == 0, r2.stdout + r2.stderr
    text = (sub / "_defaults.yaml").read_text(encoding="utf-8")
    assert "cpu_usage" not in text, text
    assert "disable" in text, text


def test_every_underscore_file_on_the_root_plane_is_health_checked(tmp_path):
    """體檢母體是這一層所有 `_` 前綴檔：`_shared.yaml` 的壞 `defaults:` 值也
    擋下整輪（exporter 會把它併進全域、整份丟掉）。"""
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml", "defaults:\n  cpu_usage: 80\n")
    _write(root, "_shared.yaml", "defaults:\n  x: disable\n")
    before = _tree_bytes(root)

    r = _run(root, "--execute")

    assert r.returncode == 1, r.stdout + r.stderr
    assert "本輪降級為預覽" in r.stdout, r.stdout
    tail = _tail(r.stdout)
    assert "載體體檢 → _shared.yaml" in tail and "disable" in tail, tail
    assert _tree_bytes(root) == before, "a degraded run wrote to the tree"


def test_a_tenant_files_bad_defaults_value_only_warns(tmp_path):
    """租戶檔的 `defaults:` 值不合格 ⇒ 只警告（exporter 會整份丟掉那個租戶檔），
    不擋寫入、不影響 rc，而且與 `--plane` 無關。"""
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml", "defaults:\n  cpu_usage: 80\n")
    _write(root, "alpha.yaml",
           "defaults:\n  x: disable\ntenants:\n  alpha:\n    cpu_usage: '85'\n")

    r = _run(root, "--execute")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "exporter 會整份丟掉這個租戶檔" in r.stdout, r.stdout
    assert "alpha.yaml" in r.stdout.split("exporter 會整份丟掉", 1)[0], r.stdout
    assert "cpu_usage" not in (root / "_defaults.yaml").read_text(encoding="utf-8")
    assert "cpu_usage" not in (root / "alpha.yaml").read_text(encoding="utf-8")

    sub = tmp_path / "finance"
    sub.mkdir()
    _write(sub, "alpha.yaml",
           "defaults:\n  x: disable\ntenants:\n  alpha:\n    cpu_usage: '85'\n")
    r2 = _run(sub, "--plane", "subtree", "--execute")
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert "exporter 會整份丟掉這個租戶檔" in r2.stdout, r2.stdout


def test_a_tenant_file_the_exporter_cannot_decode_is_warned_about(tmp_path):
    """租戶檔有重複 key：本工具讀得下（last wins）、exporter 整份丟——警告、rc 0。
    成對反例：乾淨的租戶檔沒有任何 ⚠️。"""
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml", "defaults:\n  cpu_usage: 80\n")
    _write(root, "alpha.yaml",
           "defaults:\n  x: 1\n  x: 2\ntenants:\n  alpha:\n    mem_usage: '1'\n")

    r = _run(root, "--execute")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "alpha.yaml exporter 讀不進去" in r.stdout and "重複 key" in r.stdout, r.stdout

    root2 = tmp_path / "conf.d2"
    root2.mkdir()
    _write(root2, "_defaults.yaml", "defaults:\n  cpu_usage: 80\n")
    _write(root2, "alpha.yaml", "tenants:\n  alpha:\n    cpu_usage: '85'\n")
    r2 = _run(root2, "--execute")
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert "⚠️" not in r2.stdout, r2.stdout


def test_an_empty_value_only_warns_and_the_run_still_writes(tmp_path):
    """`key:`（空值）不是 exporter 讀不進去：只警告（講 0 閾值），照寫、rc 0。
    成對反例：同一位置換成字串就是 blocking——整輪降級、零寫入、rc 1。"""
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml", "defaults:\n  cpu_usage: 80\n  legacy_key:\n")
    _write(root, "alpha.yaml", "tenants:\n  alpha:\n    cpu_usage: '85'\n")

    r = _run(root, "--execute")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "下架未完成" not in r.stdout, r.stdout
    assert "⚠️" in r.stdout and "legacy_key" in r.stdout and "解成 0" in r.stdout, r.stdout
    assert "永遠觸發" not in r.stdout, r.stdout
    assert "cpu_usage" not in (root / "_defaults.yaml").read_text(encoding="utf-8")
    assert "cpu_usage" not in (root / "alpha.yaml").read_text(encoding="utf-8")

    root2 = tmp_path / "conf.d2"
    root2.mkdir()
    _write(root2, "_defaults.yaml", "defaults:\n  cpu_usage: 80\n  legacy_key: x\n")
    _write(root2, "alpha.yaml", "tenants:\n  alpha:\n    cpu_usage: '85'\n")
    before = _tree_bytes(root2)
    r2 = _run(root2, "--execute")
    assert r2.returncode == 1, r2.stdout + r2.stderr
    assert "本輪降級為預覽" in r2.stdout, r2.stdout
    assert _tree_bytes(root2) == before


def test_a_tab_the_exporter_accepts_is_named_as_the_tools_own_limit(
        tmp_path, monkeypatch, capsys):
    """yaml.v3（libyaml）收得下的 tab，本工具的 pure parser 讀不了：擋寫入，但
    訊息說「本工具讀不了」而不是「exporter 讀不進去」，只列一次、不附
    `--plane subtree`。沒有 libyaml 時同一格的 exporter 判定未知，同樣不冒充。
    成對反例：拿掉 tab 就 rc 0、照寫。"""
    body = "defaults:\n  cpu_usage: 80\t\n  mem_usage: 90\n"
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml", body)

    r = _run(root, "--execute")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "本工具讀不了（pure parser 限制）" in r.stdout, r.stdout
    assert "exporter 讀不進這份檔" not in r.stdout, r.stdout
    if yaml.__with_libyaml__:
        assert "exporter 讀得進去" in r.stdout, r.stdout
    tail = _tail(r.stdout)
    assert tail.count("_defaults.yaml") == 1, tail
    assert "--plane subtree" not in tail, tail
    assert (root / "_defaults.yaml").read_text(encoding="utf-8") == body

    # 沒有 libyaml 的那一支：scanner 錯誤本身就標成本工具的限制。
    monkeypatch.setattr(deprecate_rule, "_LOADER", yaml.SafeLoader)
    got = deprecate_rule.carrier_health(body.encode("utf-8"))
    assert len(got) == 1 and got[0][0] is None, got
    assert got[0][2] == deprecate_rule.UNPARSED_BY_TOOL, got
    assert "exporter 讀得進去" not in got[0][1], got
    monkeypatch.setattr(sys, "argv",
                        ["deprecate_rule.py", "cpu_usage",
                         "--config-dir", str(root), "--execute"])
    with pytest.raises(SystemExit) as exc:
        deprecate_rule.main()
    out = capsys.readouterr().out
    assert exc.value.code == 1, out
    assert "本工具讀不了（pure parser 限制）" in out, out
    assert "exporter 讀不進這份檔" not in out, out
    assert (root / "_defaults.yaml").read_text(encoding="utf-8") == body

    _write(root, "_defaults.yaml", body.replace("\t", ""))
    r2 = _run(root, "--execute")
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert "cpu_usage" not in (root / "_defaults.yaml").read_text(encoding="utf-8")


def test_a_write_that_silently_does_nothing_is_not_masked_by_the_rescan(
        tmp_path, monkeypatch, capsys):
    """`--execute` 後的判定是真實重掃：寫檔函式回報成功但什麼都不寫 ⇒
    「重掃仍有引用」rc 1。"""
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml", "defaults:\n  cpu_usage: 80\n  mem_usage: 90\n")
    before = (root / "_defaults.yaml").read_bytes()

    monkeypatch.setattr(deprecate_rule, "save_yaml_file",
                        lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv",
                        ["deprecate_rule.py", "cpu_usage",
                         "--config-dir", str(root), "--execute"])

    with pytest.raises(SystemExit) as exc:
        deprecate_rule.main()

    out = capsys.readouterr().out
    assert exc.value.code == 1, out
    assert "重掃仍有引用" in out and "cpu_usage" in out, out
    assert "下架完成！" not in out, out
    assert (root / "_defaults.yaml").read_bytes() == before


# ===================================================================
# 預覽＝執行的判斷；--plane
# ===================================================================


def test_preview_reaches_the_same_verdict_as_execute(tmp_path):
    """預覽模式不得對一棵 `--execute` 會拒絕的樹回「乾淨」。"""
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml", "defaults:\n  cpu_usage: 80\n")
    _write(root, "_shared.yaml",
           "tenants:\n  shared:\n    cpu_usage: \"70\"\n")
    before = _tree_bytes(root)

    preview = _run(root)

    assert preview.returncode == 1, preview.stdout + preview.stderr
    tail = _tail(preview.stdout)
    assert "_shared.yaml" in tail, tail
    assert "本工具依設計不寫任何 `_` 前綴檔的 `tenants:` 區塊，請手動移除" in tail, tail
    assert _tree_bytes(root) == before

    executed = _run(root, "--execute")
    assert executed.returncode == preview.returncode, executed.stdout


def test_preview_on_a_clean_tree_still_reads_clean(tmp_path):
    """成對反例：預覽把乾淨的樹判成未完成，上一條就沒有價值。"""
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml", "defaults:\n  cpu_usage: 80\n  mem_usage: 90\n")
    _write(root, "alpha.yaml", "tenants:\n  alpha:\n    cpu_usage: '85'\n")

    r = _run(root)

    assert r.returncode == 0, r.stdout + r.stderr
    assert "預覽模式" in r.stdout, r.stdout
    assert "下架未完成" not in r.stdout, r.stdout


# `config_subtree_reach_test.go` 的 `state-filter-disable` 那棵樹：子樹載體的
# 字串值是**合法且生效**的設定。
_SUBTREE_ROOT = ("defaults:\n  mysql_connections: 80\n"
                 "state_filters:\n  maintenance:\n    severity: warning\n"
                 "    default_state: enable\n")
_SUBTREE_CHILD = 'defaults:\n  _state_maintenance: "disable"\n'


def test_plane_subtree_skips_the_root_type_rule(tmp_path):
    """`--plane subtree` 跳過載體體檢，而且是顯式的。"""
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml", _SUBTREE_ROOT)
    sub = root / "finance"
    sub.mkdir()
    _write(sub, "_defaults.yaml", _SUBTREE_CHILD)
    _write(sub, "t1.yaml", "tenants:\n  t1: {}\n")

    r = _run(sub, "--plane", "subtree", "--execute")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "下架未完成" not in r.stdout, r.stdout
    assert "子樹載體不做型別體檢" in r.stdout, r.stdout
    assert "disable" in (sub / "_defaults.yaml").read_text(encoding="utf-8")


def test_a_subtree_directory_without_a_carrier_prints_no_type_check_notice(
        tmp_path):
    """沒有載體的子樹目錄不印 ℹ️——那行講的是載體。"""
    sub = tmp_path / "finance"
    sub.mkdir()
    _write(sub, "t1.yaml", "tenants:\n  t1:\n    cpu_usage: '70'\n")

    r = _run(sub, "--plane", "subtree")

    assert "子樹載體不做型別體檢" not in r.stdout, r.stdout


def test_the_default_plane_is_root_and_it_names_the_escape_hatch(tmp_path):
    """預設（不給旗標）是 root，體檢照跑，rc 1 的訊息講出 `--plane subtree`。"""
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml",
           'defaults:\n  _state_maintenance: "disable"\n  cpu_usage: 80\n')

    r = _run(root, "--execute")

    assert r.returncode == 1, r.stdout + r.stderr
    tail = _tail(r.stdout)
    assert "_state_maintenance" in tail and "map[string]float64" in tail, tail
    assert "--plane subtree" in tail, tail
    assert "子樹載體不做型別體檢" not in r.stdout, r.stdout


@pytest.mark.parametrize("fixture_name", ["full-l0-l3", "mixed-mode"])
def test_the_shipped_golden_subtrees_run_clean_under_plane_subtree(
        tmp_path, fixture_name):
    """既有的兩個正典子樹形狀，`--plane subtree` 下都是 rc 0、位元組不變。"""
    fixture = GOLDEN / fixture_name / "conf.d"
    if not (fixture / "db" / "_defaults.yaml").exists():
        pytest.skip(f"golden fixture moved: {fixture}")
    root = tmp_path / "conf.d"
    shutil.copytree(fixture, root)
    before = _tree_bytes(root)

    r = _run(root / "db", "--plane", "subtree", "--execute")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "下架未完成" not in r.stdout, r.stdout
    assert _tree_bytes(root) == before


# ===================================================================
# 宣告層 optional_overrides；defaults 清空
# ===================================================================


def test_the_declared_tier_is_deprecated_with_the_valued_one(tmp_path):
    """`optional_overrides:` 是同一個載體的第二個平面，一起清。"""
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml",
           "defaults:\n  cpu_usage: 80\n  mem_usage: 90\n"
           "optional_overrides:\n- cpu_usage\n- oracle_process_count\n")

    r = _run(root, "--execute")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "optional_overrides" in r.stdout, r.stdout
    data = yaml.safe_load((root / "_defaults.yaml").read_text(encoding="utf-8"))
    assert data["defaults"] == {"mem_usage": 90}, data
    assert data["optional_overrides"] == ["oracle_process_count"], data


def test_a_declared_only_carrier_empties_the_list_rather_than_leaving_it(
        tmp_path):
    """清空的 `optional_overrides:` 整個拿掉，不留 `[]`。"""
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml",
           "defaults:\n  mem_usage: 90\noptional_overrides:\n- cpu_usage\n")

    r = _run(root, "--execute")

    assert r.returncode == 0, r.stdout + r.stderr
    text = (root / "_defaults.yaml").read_text(encoding="utf-8")
    assert "optional_overrides" not in text, text
    assert yaml.safe_load(text) == {"defaults": {"mem_usage": 90}}, text


def test_deleting_the_last_default_removes_the_block_and_a_null_block_stays_null(
        tmp_path):
    """`defaults:` 與 `optional_overrides:` 對稱：清空就整個拿掉；本輪沒動到的
    `defaults:` 原本是空值就還是空值，不會被無中生有成 `{}`。"""
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml", "# h\ndefaults:\n  cpu_usage: 80\n")

    r = _run(root, "--execute")
    assert r.returncode == 0, r.stdout + r.stderr
    text = (root / "_defaults.yaml").read_text(encoding="utf-8")
    assert "defaults" not in text, text

    _write(root, "_defaults.yaml", "# h\ndefaults:\noptional_overrides:\n- cpu_usage\n- x\n")
    r2 = _run(root, "--execute")
    assert r2.returncode == 0, r2.stdout + r2.stderr
    data = yaml.safe_load((root / "_defaults.yaml").read_text(encoding="utf-8"))
    assert data == {"defaults": None, "optional_overrides": ["x"]}, data


def test_a_non_carrier_underscore_file_declaring_the_metric_is_not_ignored(
        tmp_path):
    """`_shared.yaml` 的 `optional_overrides:` 會被 exporter 併進全域：掃描列得
    出來、Step 1 依設計不寫 ⇒ rc 1，具名。"""
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml", "defaults:\n  cpu_usage: 80\n")
    _write(root, "_shared.yaml", "optional_overrides:\n- cpu_usage\n")

    r = _run(root, "--execute")

    assert r.returncode == 1, r.stdout + r.stderr
    assert "下架完成！" not in r.stdout, r.stdout
    tail = _tail(r.stdout)
    assert "_shared.yaml" in tail, tail
    assert "非 defaults 載體" in tail and "需手動處理" in tail, tail


def test_the_scan_itself_lists_the_declared_tier(tmp_path):
    """`[optional_overrides]` 出現在 Step 1 之前的「發現 N 處引用」。"""
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml",
           "defaults:\n  mem_usage: 90\noptional_overrides:\n- cpu_usage\n")

    r = _run(root)

    head = r.stdout.split("Step 1:", 1)[0]
    assert "發現 1 處引用" in head, r.stdout
    assert "[optional_overrides] cpu_usage" in head, r.stdout


def test_a_declared_only_carrier_does_not_grow_an_empty_defaults_block(
        tmp_path):
    """只有宣告層的載體處置後不得長出 `defaults: {}`。"""
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml",
           "# hdr\noptional_overrides:\n- cpu_usage\n- oracle_process_count\n")

    r = _run(root, "--execute")

    assert r.returncode == 0, r.stdout + r.stderr
    text = (root / "_defaults.yaml").read_text(encoding="utf-8")
    assert "defaults" not in text, text
    assert yaml.safe_load(text) == {
        "optional_overrides": ["oracle_process_count"]}, text


# ===================================================================
# 射程外的載體：印一次
# ===================================================================


def test_files_the_tool_cannot_reach_are_named_before_the_metric_loop(tmp_path):
    """子目錄、`_` 前綴檔（含 `_defaults.yaml` 自己）的 `tenants:`／`profiles:`
    區塊：一次調用印一次，不是每個 metric 印一次。"""
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml",
           "defaults:\n  cpu_usage: 80\ntenants:\n  own:\n    disk_usage: '1'\n")
    _write(root, "_shared.yaml", "tenants:\n  shared:\n    disk_usage: '70'\n")
    _write(root, "_profiles.yaml", "profiles:\n  p1:\n    disk_usage: '70'\n")
    (root / "finance").mkdir()
    _write(root / "finance", "t1.yaml", "tenants:\n  t1:\n    disk_usage: '70'\n")

    r = _run_metrics(root, ["cpu_usage", "mem_usage"], "--execute")

    banner = "本工具射程外"
    assert r.stdout.count(banner) == 1, r.stdout
    block = r.stdout.split(banner, 1)[1].split("Processing:", 1)[0]
    assert "finance/" in block and "不遞迴寫入" in block, block
    assert "_shared.yaml" in block and "`tenants:` 區塊" in block, block
    assert "_defaults.yaml" in block, block
    assert "_profiles.yaml" in block and "`profiles:` 區塊" in block, block
    # 沒有一個持有本輪的 metric ⇒ 列出來不等於未完成。
    assert r.returncode == 0, r.stdout + r.stderr


def test_a_clean_flat_tree_says_nothing_about_unreachable_files(tmp_path):
    """成對反例：沒有那幾類檔時不印那段。"""
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml", "defaults:\n  cpu_usage: 80\n")
    _write(root, "alpha.yaml", "tenants:\n  alpha:\n    cpu_usage: '85'\n")

    r = _run(root, "--execute")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "本工具射程外" not in r.stdout, r.stdout
