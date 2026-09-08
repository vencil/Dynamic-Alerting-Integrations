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
import subprocess
import sys
from pathlib import Path

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


def _run(config_dir: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOL), "cpu_usage", "--config-dir",
         str(config_dir), *extra],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"})


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
    """root 本來就沒有這個 metric ⇒ 具名回報 + 位元組不變。

    ⚠️ 比對的是**位元組**而不是 YAML 值：舊行為除了新增一行，還會把整份檔
    重新 `safe_dump` 一次（引號、旗標樣式、註解位置都會動）。一個「沒事做」
    的載體不該有任何一種被改寫。
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
    assert "_shared.yaml" in tail and "重掃仍有引用" in tail, tail
    assert "tenants.shared" in tail and "cpu_usage" in tail, tail
    # 擋住宣稱不等於撤回已做的事：載體那一半仍然被清掉了。
    assert "cpu_usage" not in (root / "_defaults.yaml").read_text(encoding="utf-8")


# ===================================================================
# #1787 契約鏡射：exporter 讀得下去嗎
# ===================================================================


def _non_numeric_defaults(text: str) -> list[tuple[str, object]]:
    """`defaults:` 底下所有**不是** int/float 的 (key, value)。

    這是 `ThresholdConfig.Defaults` 宣告的 `map[string]float64`
    （`components/threshold-exporter/app/pkg/config/types.go:208`）在 Python
    這一側的鏡射：Go 的 yaml decoder 對這個型別碰到字串不是「跳過那個 key」，
    而是對**整份檔**回錯 —— `parsePartialConfig` 因此回 ok=false，那個載體
    連同它的 `state_filters:` / `_routing_defaults:` 一起從設定裡消失。所以
    「值是數字」不是風格偏好，是這份檔被讀進去的前提。

    ⚠️ `bool` 在 Python 是 `int` 的子類別，明確排除：`cpu_usage: yes` 會被
    YAML 解成 `True`，而它在 Go 那邊同樣不是 float64。
    """
    data = yaml.safe_load(text) or {}
    defaults = data.get("defaults") or {}
    return [(k, v) for k, v in defaults.items()
            if isinstance(v, bool) or not isinstance(v, (int, float))]


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
