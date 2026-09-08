"""
tests/ops/test_deprecate_rule_carriers.py — deprecate_rule writes EVERY
defaults carrier, and says so only when it did (#1609).

The exporter merges both `_defaults.yaml` and `_defaults.yml` into the
defaults chain (`config_hierarchy.go:216`), while `deprecate_rule --execute`
used to write only the one carrier `resolve_defaults_file` picks — and then
print 「✅ 下架完成」 under a scan that had just listed both. These tests pin
the two halves of the fix: every carrier is written, and the summary may
not claim completion when "found N" and "changed N" disagree.

#1787 changed WHAT a write is: 下架 removes the metric's keys, it does not
set them to the string `"disable"`. `ThresholdConfig.Defaults` is
`map[string]float64` (`threshold-exporter/app/pkg/config/types.go:208`), so a
string under `defaults:` makes `parsePartialConfig` reject the whole carrier —
the platform loses that file's `state_filters:` and `_routing_defaults:` too.
Every assertion below that used to read `== "disable"` now reads "the key is
not there", and the completion claim is pinned at KEY level: after
`--execute`, a rerun of `scan_for_metric` must come back empty.

Driven through `subprocess` because the contract under test is the
operator-visible report (stdout + rc), not a return value.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import deprecate_rule  # noqa: E402

TOOL = Path(deprecate_rule.__file__).resolve()


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
    before = {p.name: p.read_text(encoding="utf-8") for p in root.iterdir()}

    r = _run(root)

    assert r.returncode == 0, r.stdout + r.stderr
    assert _step1_lines(r.stdout) == ["Step 1: _defaults.yaml",
                                      "Step 1: _defaults.yml"], r.stdout
    assert "預覽模式" in r.stdout
    after = {p.name: p.read_text(encoding="utf-8") for p in root.iterdir()}
    assert after == before, "preview mode wrote to the tree"


def test_an_unwritable_carrier_blocks_the_completion_claim(tmp_path):
    """(c) One carrier cannot be read → rc 1, named, and NOT 下架完成.

    ⚠️ The unwritable shape is a broken YAML document, not mode bits: the
    suite runs as root, where `chmod` denies nothing. A carrier the scan
    skips with 無法讀取 is exactly the shape that used to vanish between
    「發現 N 處引用」 and 「下架完成」.

    The other carrier's write must survive: one carrier's failure is a
    reason to withhold the completion claim, not to undo the work done.
    """
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml", "defaults:\n  cpu_usage: 80\n")
    _write(root, "_defaults.yml", "defaults: [\n")
    _write(root, "alpha.yaml", "tenants:\n  alpha:\n    cpu_usage: 85\n")

    r = _run(root, "--execute")

    assert r.returncode == 1, r.stdout + r.stderr
    assert "下架未完成" in r.stdout, r.stdout
    assert "下架完成！" not in r.stdout, r.stdout
    tail = r.stdout.split("下架未完成", 1)[1]
    assert "_defaults.yml" in tail, tail
    assert "無法讀取" in tail, tail
    text = (root / "_defaults.yaml").read_text(encoding="utf-8")
    assert "cpu_usage" not in text, text


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
    reports 不存在, and the run is incomplete (rc 1).

    Rule: a Step 1 result that is not ok and was not in the scan's own
    findings is still a carrier the tool could not write, so the summary
    may not read as clean. Before #1609 this tree printed 「✅ 下架完成」
    over a ❌ Step 1 — the same contradiction as the two-carrier case.
    """
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
    """Blind review: `found` was every file with a `defaults:` block, so a
    tenant file carrying one made the run rc 1 with reason 未寫入 — and no
    rerun could clear it. The exporter's own rule decides: a tenant file's
    `defaults:` is dropped (WARN) → named, not counted; a `_`-prefixed
    non-carrier IS merged in flat mode but this tool may not write it →
    incomplete, by name, with a reason that says so."""
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
    tail = r.stdout.split("下架未完成", 1)[1]
    assert "_defaults-multidb.yaml" in tail and "非 defaults 載體" in tail, tail
    assert "未寫入" not in tail, tail
    assert "cpu_usage" not in (
        root2 / "_defaults.yaml").read_text(encoding="utf-8")


def test_a_carrier_whose_defaults_key_is_null_does_not_traceback(tmp_path):
    """Blind review: `defaults:` with no value parses to None; `pk in None`
    raised TypeError → bare traceback, rc 1 — the code the docs now reserve
    for 下架未完成. It is an empty defaults block.

    #1787: an empty block holds no `cpu_usage`, so this is now the named
    no-op — the tool used to "handle" it by APPENDING `cpu_usage: disable`,
    i.e. by putting the metric INTO a file that did not have it."""
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
    """Re-review: `defaults:` as a list (or scalar) was replaced by a fresh
    mapping — 「✅ 下架完成」 rc 0 with the original block gone. The exporter
    cannot decode that file either; it is the operator's to repair, not the
    tool's to overwrite. Refuse by name, rc 1, bytes untouched — preview too."""
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
    """只有 `cpu_usage_critical` 的載體：該 key 被刪，且**沒有**新增 base key。

    這是舊行為最尖銳的一格：寫入端只認 BASE key，所以它在這棵樹上做的事是
    「把 `cpu_usage: disable` 加進去」——原本要下架的 `_critical` 原封不動，
    而檔案多了一個它本來沒有的 metric，值還是個字串。修法把掃描與處置接到
    同一份 `metric_pattern_keys` 上，所以掃描報得出來的那一個，就是被刪掉的
    那一個。
    """
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml",
           "defaults:\n  cpu_usage_critical: 95\n  mem_usage: 90\n")

    r = _run(root, "--execute")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "已移除 cpu_usage_critical（原值: 95）" in r.stdout, r.stdout
    data = yaml.safe_load((root / "_defaults.yaml").read_text(encoding="utf-8"))
    # 刪掉的那一個不在了……
    assert "cpu_usage_critical" not in data["defaults"], data
    # ……而且沒有長出一個 base key（舊行為正是在這裡把好檔寫壞的）。
    assert "cpu_usage" not in data["defaults"], data
    assert data["defaults"] == {"mem_usage": 90}, data


def test_a_root_without_the_metric_is_a_named_noop_byte_for_byte(tmp_path):
    """root 本來就沒有這個 metric ⇒ 具名回報，且**這條 no-op 路徑**位元組不變。

    ⚠️ 比對的是**位元組**而不是 YAML 值：舊行為除了新增一行，還會把整份檔
    重新 `safe_dump` 一次（引號、旗標樣式、註解位置都會動）。一個「沒事做」
    的載體不該有任何一種被改寫。

    ⛔ 這個保證的射程只到 no-op 路徑（二輪 F-05）。真的有 key 要刪時，寫回仍
    然走既有的 `save_yaml_file`，也就是 header 註解之外**整份 safe_dump 重寫**
    ——引號、flow 樣式、非 header 位置的註解都會動。本輪沒有換 dumper，不要把
    這條斷言讀成「本工具永遠只改它動到的行」。
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


def test_root_and_subtree_carriers_both_lose_the_key(tmp_path):
    """root 與子樹的 defaults 載體都是刪 key。

    ⚠️ 這支工具是**平面**的（`defaults_carriers` 只列 `--config-dir` 那一層），
    所以子樹要自己指過去——測試把這個限制一起釘住：對 root 跑完之後子樹**還
    在**（並且 `warn_nested` 已具名警告），指到子樹再跑才清掉。兩次都是刪，
    沒有任何一次寫進 `disable`：子樹載體一旦帶字串，`parsePartialConfig` 丟
    掉的是整份子樹檔（`types.go:208`），而 root 已無此 metric 之後子樹還持有
    它，會落進 `resolve.go:1383` 的 unknown-key blocking error。
    """
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml", "defaults:\n  cpu_usage: 80\n  mem_usage: 90\n")
    sub = root / "finance"
    sub.mkdir()
    _write(sub, "_defaults.yaml", "defaults:\n  cpu_usage: 60\n")

    r = _run(root, "--execute")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "cpu_usage" not in (root / "_defaults.yaml").read_text(encoding="utf-8")
    # 平面：子樹沒被碰，但也沒被靜默略過。
    assert "finance" in r.stdout + r.stderr, r.stdout + r.stderr
    assert "cpu_usage: 60" in (sub / "_defaults.yaml").read_text(encoding="utf-8")

    r2 = _run(sub, "--execute")
    assert r2.returncode == 0, r2.stdout + r2.stderr
    sub_text = (sub / "_defaults.yaml").read_text(encoding="utf-8")
    assert "cpu_usage" not in sub_text, sub_text
    assert "disable" not in sub_text, sub_text


def test_a_reference_the_writer_cannot_clear_blocks_the_completion_claim(
        tmp_path):
    """重掃仍有引用 ⇒ rc 1、具名、且不印「下架完成」(#1787 key 級不變式)。

    語料選的是一個**扁平掃描看得到、但 Step 2 依設計不寫**的載體：
    `remove_from_tenants` 跳過所有 `_` 開頭的檔名（它是為了跳過
    `_defaults.yaml` 而寫的），所以 `_shared.yaml` 的 `tenants:` 區塊會活過
    整輪處置。舊的完成度判定是檔案級的（「每個 defaults 載體都寫到了嗎」），
    它對這棵樹回「✅ 下架完成」——而那個 key 還在。

    ⚠️ 具名的限制：重掃跟掃描共用同一支平面 `scan_for_metric`，所以它看得見
    的殘留才擋得住。真正藏在巢狀目錄裡的租戶檔兩邊都看不見，那一格由
    `warn_nested` 具名，不由本斷言負責。
    """
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml", "defaults:\n  cpu_usage: 80\n")
    _write(root, "_shared.yaml", "tenants:\n  shared:\n    cpu_usage: \"70\"\n")

    r = _run(root, "--execute")

    assert r.returncode == 1, r.stdout + r.stderr
    assert "下架未完成" in r.stdout, r.stdout
    assert "下架完成！" not in r.stdout, r.stdout
    tail = r.stdout.split("下架未完成", 1)[1]
    assert "_shared.yaml" in tail, tail
    assert "tenants.shared" in tail and "cpu_usage" in tail, tail
    # 三輪 F-04：這是**依設計不歸本工具**的殘留，不是寫入失敗。訊息要說
    # 「請手動移除」，不是「重掃仍有引用」——後者會把人送去查一個沒發生的 bug。
    assert "本工具依設計不寫 `_` 前綴租戶檔，請手動移除" in tail, tail
    assert "重掃仍有引用" not in tail, tail
    # 擋住宣稱不等於撤回已做的事：載體那一半仍然被清掉了。
    assert "cpu_usage" not in (root / "_defaults.yaml").read_text(encoding="utf-8")


# ===================================================================
# #1787 契約鏡射：exporter 讀得下去嗎
# ===================================================================


def _non_numeric_defaults(text: str) -> list[tuple[str, object]]:
    """`defaults:` 底下所有**不是** int/float 的 (key, value)。

    ⛔ 判定式**不在這裡**：這支只負責把 YAML 文字剖成 `defaults:` 這個 mapping，
    然後把它交給產線的 `deprecate_rule.non_numeric_defaults`（二輪 F-02，
    D-05d「控制項必須呼叫產線那份」）。工具自己在寫回後也是呼叫同一支來決定
    要不要收回「下架完成」的宣稱，所以下面的正／負兩臂測到的是產線行為，
    不是一份寫在測試裡的等價品。

    產線那支的理由：`ThresholdConfig.Defaults` 宣告成 `map[string]float64`
    （`components/threshold-exporter/app/pkg/config/types.go:208`），Go 的
    yaml decoder 對這個型別碰到字串不是「跳過那個 key」，而是對**整份檔**回錯
    —— `parsePartialConfig` 因此回 ok=false，那個載體連同它的 `state_filters:`
    / `_routing_defaults:` 一起從設定裡消失。
    """
    data = yaml.safe_load(text) or {}
    return [(k, v) for k, v, _kind
            in deprecate_rule.non_numeric_defaults(data.get("defaults"))]


def test_the_written_root_defaults_still_decodes_as_map_string_float64(tmp_path):
    """執行後 root `_defaults.yaml` 的 `defaults:` 值全是 int/float，
    並附一支合成負向控制（舊行為的輸出餵給**同一支**檢查函式 → 紅）。

    D-05d：控制項呼叫產線那一份。兩臂共用 `_non_numeric_defaults`，所以
    「通過」不可能是因為檢查函式對什麼都說 OK。
    """
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml",
           "defaults:\n  cpu_usage: 80\n  cpu_usage_critical: 95\n"
           "  mem_usage: 90\n  disk_usage: 85\n")

    r = _run(root, "--execute")
    assert r.returncode == 0, r.stdout + r.stderr

    written = (root / "_defaults.yaml").read_text(encoding="utf-8")
    assert _non_numeric_defaults(written) == [], written
    # 反不變式：不是靠「defaults 變空」通過的。
    assert yaml.safe_load(written)["defaults"] == {"mem_usage": 90,
                                                   "disk_usage": 85}, written

    # 合成負向控制 —— 舊行為在同一棵樹上會寫出的形狀。
    old_behaviour = ("defaults:\n  cpu_usage: disable\n"
                     "  cpu_usage_critical: 95\n  mem_usage: 90\n"
                     "  disk_usage: 85\n")
    assert _non_numeric_defaults(old_behaviour) == [("cpu_usage", "disable")], (
        "the same checker must call the old shape out")


# ===================================================================
# #1787 二輪盲審 — F-01 / F-02 / F-03
# ===================================================================


def test_a_neighbouring_metric_sharing_the_prefix_is_not_this_metrics_key(
        tmp_path):
    """F-01：`container_cpu` 的下架不能被 `container_cpu_throttle_critical` 卡住。

    這是 stock 樹的形狀（`da-tools init --rule-packs mariadb,kubernetes` 會同時
    給出 `container_cpu` 與 `container_cpu_throttle`，租戶檔各帶一個
    `_critical`）。掃描端用的是子字串 (`metric_key in key`)、移除端用的是精確
    名字，兩邊述詞不同 ⇒ 掃描把**另一個活指標**的 key 報成本 metric 的引用，
    重掃於是判它殘留、rc 1，而且沒有任何重跑清得掉。

    ⛔ 反向也要成立：那個鄰居是另一個指標的**資料**，本工具不得刪它。舊的移除
    端子字串分支會刪掉 `container_cpu_throttle{pod="x"}`。
    """
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml",
           "defaults:\n  container_cpu: 80\n  container_cpu_throttle: 25\n")
    _write(root, "db-a.yaml",
           "tenants:\n  db-a:\n    container_cpu_critical: '95'\n"
           "    container_cpu_throttle_critical: '50'\n"
           "    container_cpu_throttle{pod=\"x\"}: '60'\n")

    r = _run_metrics(root, ["container_cpu"], "--execute")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "下架完成" in r.stdout, r.stdout
    assert "下架未完成" not in r.stdout, r.stdout
    defaults = yaml.safe_load(
        (root / "_defaults.yaml").read_text(encoding="utf-8"))["defaults"]
    assert "container_cpu" not in defaults, defaults
    assert defaults == {"container_cpu_throttle": 25}, defaults
    tenant = yaml.safe_load(
        (root / "db-a.yaml").read_text(encoding="utf-8"))["tenants"]["db-a"]
    assert "container_cpu_critical" not in tenant, tenant
    # 鄰居的兩個形狀都必須原封不動。
    assert tenant["container_cpu_throttle_critical"] == "50", tenant
    assert tenant['container_cpu_throttle{pod="x"}'] == "60", tenant


def test_a_batch_run_judges_completeness_once_after_every_metric(tmp_path):
    """F-01：`deprecate a b` 的判定在**兩個都處理完之後**跑，只跑一次。

    語料選的是「第二個 metric 才會清掉的東西」：載體帶著舊版留下的
    `old_metric: disable`，而 `old_metric` 正是本輪要下架的第二個 metric。
    判定若留在 per-metric 迴圈裡，處理 `cpu_usage` 的那一輪會看到它、判成
    非數值殘留 rc 1——而下一輪就要把它刪掉。判定是一次調用的性質，不是一個
    metric 的性質。

    ⚠️ 只有**跨 metric**的判定（載體體檢）能區分這兩個位置。同一個 metric 的
    重掃在迴圈內外等價（述詞是精確的，A 的重掃看不見 B 的 key），所以搬出迴圈
    對重掃那一半是結構整理，不是新的偵測力——不要把這條測試讀成它證明了那個。
    """
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml",
           "defaults:\n  cpu_usage: 80\n  old_metric: disable\n  mem_usage: 90\n")
    _write(root, "db-a.yaml", "tenants:\n  db-a:\n    cpu_usage: '85'\n")

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
    _write(root, "db-a.yaml",
           "tenants:\n  db-a:\n    cpu_usage: '85'\n    mem_usage: '70'\n")

    r = _run_metrics(root, ["cpu_usage", "mem_usage"], "--execute")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "下架完成" in r.stdout, r.stdout
    defaults = yaml.safe_load(
        (root / "_defaults.yaml").read_text(encoding="utf-8"))["defaults"]
    assert defaults == {"disk_usage": 85}, defaults


def test_a_non_numeric_residue_from_another_metric_withholds_the_claim(
        tmp_path):
    """F-02：載體還留著**別的** metric 的非數值 defaults ⇒ rc 1，具名型別。

    舊版工具留下的 `old_metric: disable` 不在本輪的掃描射程裡（它是另一個
    metric），但 exporter 會因為它把整份載體丟掉——所以「下架完成！
    threshold-exporter 將在下次 reload 時生效」是一句假話：下次 reload 什麼
    都不會生效。判定用的是產線的 `non_numeric_defaults`。
    """
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml",
           "defaults:\n  cpu_usage: 80\n  old_metric: disable\n  mem_usage: 90\n")

    r = _run(root, "--execute")

    assert r.returncode == 1, r.stdout + r.stderr
    assert "下架未完成" in r.stdout, r.stdout
    assert "將在下次 reload 時生效" not in r.stdout, r.stdout
    tail = r.stdout.split("下架未完成", 1)[1]
    assert "_defaults.yaml" in tail and "old_metric" in tail, tail
    assert "str" in tail and "map[string]float64" in tail, tail
    # 擋住宣稱不等於撤回已做的事。
    assert "cpu_usage" not in (
        root / "_defaults.yaml").read_text(encoding="utf-8")
    # 而本輪自己要刪的那個 key，不會被自己的體檢當成殘留。
    root2 = tmp_path / "conf.d2"
    root2.mkdir()
    _write(root2, "_defaults.yaml",
           "defaults:\n  cpu_usage: disable\n  mem_usage: 90\n")
    r2 = _run(root2, "--execute")
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert "下架完成" in r2.stdout, r2.stdout


def test_preview_reaches_the_same_verdict_as_execute(tmp_path):
    """F-03：預覽模式不得對一棵 `--execute` 會拒絕的樹回「乾淨」。

    `_shared.yaml` 的 `tenants:` 區塊是掃描看得見、Step 2 依設計不寫的殘留
    （`remove_from_tenants` 跳過所有 `_` 開頭檔名）。預覽算的是「掃描結果 −
    本輪計畫刪除的 key」，所以它和 `--execute` 的重掃給出同一個判斷——
    #1609 :553 那句「預覽不能讀起來比執行乾淨」才是一個性質，不是註解。
    """
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml", "defaults:\n  cpu_usage: 80\n")
    _write(root, "_shared.yaml",
           "tenants:\n  shared:\n    cpu_usage: \"70\"\n")
    before = {p.name: p.read_bytes() for p in root.iterdir()}

    preview = _run(root)

    assert preview.returncode == 1, preview.stdout + preview.stderr
    assert "下架未完成" in preview.stdout, preview.stdout
    tail = preview.stdout.split("下架未完成", 1)[1]
    assert "_shared.yaml" in tail, tail
    # 兩種模式**同一句**（三輪 F-04）：依設計不碰的東西，預覽與執行沒有差別。
    assert "本工具依設計不寫 `_` 前綴租戶檔，請手動移除" in tail, tail
    # 預覽仍然什麼都不寫。
    assert {p.name: p.read_bytes() for p in root.iterdir()} == before

    # 同一棵樹、同一個判斷。
    executed = _run(root, "--execute")
    assert executed.returncode == preview.returncode, executed.stdout


def test_preview_on_a_clean_tree_still_reads_clean(tmp_path):
    """F-03 的誤紅面：預覽把「乾淨的樹」判成未完成，這條修法就沒有價值。

    ⭐ 這是上一支的成對反例（D-05d「反例要成對」）：只釘「髒樹會被擋」，
    收緊述詞可以靠**全部拒絕**通過那一半。
    """
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml", "defaults:\n  cpu_usage: 80\n  mem_usage: 90\n")
    _write(root, "db-a.yaml", "tenants:\n  db-a:\n    cpu_usage: '85'\n")

    r = _run(root)

    assert r.returncode == 0, r.stdout + r.stderr
    assert "預覽模式" in r.stdout, r.stdout
    assert "下架未完成" not in r.stdout, r.stdout


# ===================================================================
# #1787 三輪盲審 — F-01 / F-02 / F-03 / F-09
# ===================================================================

# `config_subtree_reach_test.go` 的 `state-filter-disable` 那棵樹，原樣。
# 子樹載體帶字串值是**合法且生效**的設定（那支 Go 測試斷言維護窗確實被關掉），
# 所以工具對它套 root 的 `map[string]float64` 規則就是在判一條活著的設定有罪。
_SUBTREE_ROOT = ("defaults:\n  mysql_connections: 80\n"
                 "state_filters:\n  maintenance:\n    severity: warning\n"
                 "    default_state: enable\n")
_SUBTREE_CHILD = 'defaults:\n  _state_maintenance: "disable"\n'


def test_a_subtree_carrier_is_not_judged_by_the_root_type_rule(tmp_path):
    """F-01：`--config-dir` 指到子樹時不跑型別體檢。

    工具自己的 usage 就教人這樣用（子樹載體要指過去才會被處理）。二輪的體檢
    對每個載體無條件套 root 的型別規則，於是這棵樹回 rc 1、訊息教人刪掉
    `_state_maintenance: "disable"` ——那是一條**正在生效**的維護窗，而且沒有
    任何重跑清得掉那個 rc 1。
    """
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml", _SUBTREE_ROOT)
    sub = root / "finance"
    sub.mkdir()
    _write(sub, "_defaults.yaml", _SUBTREE_CHILD)
    _write(sub, "t1.yaml", "tenants:\n  t1: {}\n")

    r = _run(sub, "--execute")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "下架未完成" not in r.stdout, r.stdout
    assert "子樹載體不做型別體檢" in r.stdout, r.stdout
    # 那條設定原封不動。
    assert "disable" in (sub / "_defaults.yaml").read_text(encoding="utf-8")


def test_the_same_shape_at_the_root_is_still_judged(tmp_path):
    """F-01 的成對反例：同一個字串值放在 **root** 載體上仍然 rc 1。

    ⭐ 沒有這一半，「子樹跳過體檢」最便宜的實作就是把體檢整支關掉。
    """
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml",
           'defaults:\n  _state_maintenance: "disable"\n  cpu_usage: 80\n')

    r = _run(root, "--execute")

    assert r.returncode == 1, r.stdout + r.stderr
    tail = r.stdout.split("下架未完成", 1)[1]
    assert "_state_maintenance" in tail and "map[string]float64" in tail, tail
    assert "子樹載體不做型別體檢" not in r.stdout, r.stdout


def test_the_shipped_golden_subtree_fixture_runs_clean(tmp_path):
    """F-01：本 repo 既有的正典子樹形狀（`full-l0-l3/conf.d/db/`）必須 rc 0。

    ⭐ 這是**合成案例以外**的證人：`level: L1` / `threshold: {...}` /
    `pages: [...]` 不是我為了通過而挑的值，是 golden fixture 一直長這樣。
    複製到 tmp 跑，避免動到 fixture 本身。
    """
    fixture = (Path(__file__).resolve().parents[1] / "golden" / "fixtures"
               / "full-l0-l3" / "conf.d")
    if not (fixture / "db" / "_defaults.yaml").exists():
        pytest.skip(f"golden fixture moved: {fixture}")
    root = tmp_path / "conf.d"
    shutil.copytree(fixture, root)

    r = _run(root / "db", "--execute")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "下架未完成" not in r.stdout, r.stdout


def test_the_declared_tier_is_deprecated_with_the_valued_one(tmp_path):
    """F-02：`optional_overrides:` 是同一個載體的第二個平面，一起清。

    一個離開 `defaults:` 卻留在 `optional_overrides:` 的 key，平台仍然認得它
    （`ValidateTenantKeys` 放行、`resolveDeclaredRows` 對有寫值的租戶發 row），
    所以「下架完成」蓋在一個**租戶重新寫上去就會復活**的指標上。
    """
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml",
           "defaults:\n  cpu_usage: 80\n  mem_usage: 90\n"
           "optional_overrides:\n- cpu_usage\n- oracle_process_count\n")

    r = _run(root, "--execute")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "下架完成" in r.stdout, r.stdout
    # 掃描要先看得見它，處置才可能清得掉它。
    assert "optional_overrides" in r.stdout, r.stdout
    data = yaml.safe_load((root / "_defaults.yaml").read_text(encoding="utf-8"))
    assert data["defaults"] == {"mem_usage": 90}, data
    assert data["optional_overrides"] == ["oracle_process_count"], data


def test_a_declared_only_carrier_empties_the_list_rather_than_leaving_it(
        tmp_path):
    """F-02：清空的 `optional_overrides:` 整個拿掉，不留 `[]`。

    `init_project` 對空 list 的既有立場就是不寫它（「空 list 讀起來像意外」）。
    """
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml",
           "defaults:\n  mem_usage: 90\noptional_overrides:\n- cpu_usage\n")

    r = _run(root, "--execute")

    assert r.returncode == 0, r.stdout + r.stderr
    text = (root / "_defaults.yaml").read_text(encoding="utf-8")
    assert "optional_overrides" not in text, text
    assert yaml.safe_load(text) == {"defaults": {"mem_usage": 90}}, text


def test_an_empty_value_is_reported_as_a_zero_threshold_not_as_a_dropped_file(
        tmp_path):
    """F-03：`key:`（空值）的訊息講的是 0 閾值，不是「整份載體被丟」。

    Go 實測 `cpu_usage: null` 是 ok=**true**、解成 0。把它和字串寫成同一個
    理由，是把一個沒發生的事寫給 operator 看——而他照著那句話做的動作
    （「這份檔會被丟掉」）跟真正的後果（多一條永遠觸發的 0 閾值）不同。
    """
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml", "defaults:\n  cpu_usage: 80\n  legacy_key:\n")

    r = _run(root, "--execute")

    assert r.returncode == 1, r.stdout + r.stderr
    tail = r.stdout.split("下架未完成", 1)[1]
    assert "legacy_key" in tail and "解成 0" in tail, tail
    assert "整份丟棄" not in tail, tail


def test_a_write_that_silently_does_nothing_is_not_masked_by_the_rescan(
        tmp_path, monkeypatch, capsys):
    """F-09：`predict=False` 不扣除計畫刪除的 key —— 這條註解現在有證人。

    ⛔ 二輪寫下「否則寫入失敗會被這支函式自己遮掉」卻沒有任何測試（把
    `predict=False` 改成 `predict=True` 之後全綠）。這裡把寫入變成**回報成功
    但什麼都不寫**——工具自己認為它刪掉了 key——然後斷言重掃仍然把它列出來、
    rc 1。in-process 呼叫 `main()`，因為要換掉的是產線的寫檔函式。
    """
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
    assert "下架未完成" in out, out
    assert "重掃仍有引用" in out and "cpu_usage" in out, out
    assert "下架完成！" not in out, out
    # 這一格的前提：檔案真的沒被寫。
    assert (root / "_defaults.yaml").read_bytes() == before
