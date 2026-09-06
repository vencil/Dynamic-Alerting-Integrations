#!/usr/bin/env python3
"""test_lint_custom_rules.py — CI Deny-list Linter pytest 測試套件 (Phase 10)。

驗證 lint_custom_rules.py 的核心功能:
  1. Denied function 偵測
  2. Denied pattern 偵測 (含 whitespace 變體)
  3. Required label 檢查
  4. Range vector duration 超限
  5. ConfigMap wrapper 解析
  6. Policy 載入與合併
  7. Duration parsing
  8. Expiry / owner label 檢查

用法:
  python3 -m pytest tests/test_lint_custom_rules.py -v
"""

import itertools
import os
import sys
import tempfile

import pytest
import yaml

# Add scripts/tools to path

import lint_custom_rules  # noqa: E402


# ── Shared Fixture ────────────────────────────────────────────────

@pytest.fixture
def policy():
    """提供預設 lint 政策。"""
    return lint_custom_rules.DEFAULT_POLICY.copy()


# ── 1. Duration parsing ──────────────────────────────────────────

def test_duration_seconds():
    """測試秒數解析。"""
    assert lint_custom_rules.parse_duration_seconds("30s") == 30

def test_duration_minutes():
    """測試分鐘解析。"""
    assert lint_custom_rules.parse_duration_seconds("5m") == 300

def test_duration_hours():
    """測試小時解析。"""
    assert lint_custom_rules.parse_duration_seconds("1h") == 3600

def test_duration_days():
    """測試天數解析。"""
    assert lint_custom_rules.parse_duration_seconds("2d") == 172800

def test_duration_integer_passthrough():
    """測試整數直通。"""
    assert lint_custom_rules.parse_duration_seconds(60) == 60

def test_duration_invalid_returns_none():
    """測試無效字串回傳 None。"""
    assert lint_custom_rules.parse_duration_seconds("abc") is None

def test_duration_empty_returns_none():
    """測試空字串回傳 None。"""
    assert lint_custom_rules.parse_duration_seconds("") is None


def test_lint_holt_winters_detected(policy):
    """測試 holt_winters 函數偵測。"""
    results = lint_custom_rules.lint_expr(
        "holt_winters(my_metric[1h], 0.3, 0.7)", policy, "test.yaml", "TestRule"
    )
    errors = [r for r in results if "holt_winters" in r.message]
    assert len(errors) == 1

def test_lint_predict_linear_detected(policy):
    """測試 predict_linear 函數偵測。"""
    results = lint_custom_rules.lint_expr(
        "predict_linear(disk_free[1h], 3600) < 0", policy, "test.yaml", "TestRule"
    )
    errors = [r for r in results if "predict_linear" in r.message]
    assert len(errors) == 1

def test_lint_safe_function_passes(policy):
    """測試安全函數通過檢查。"""
    results = lint_custom_rules.lint_expr(
        "rate(http_requests_total[5m]) > 100", policy, "test.yaml", "TestRule"
    )
    func_errors = [r for r in results if "denied function" in r.message]
    assert len(func_errors) == 0

def test_lint_function_name_not_substring(policy):
    """測試 metric 名稱中包含函數名稱不被誤判。"""
    results = lint_custom_rules.lint_expr(
        "my_predict_linear_metric > 100", policy, "test.yaml", "TestRule"
    )
    func_errors = [r for r in results if "predict_linear" in r.message]
    assert len(func_errors) == 0


def test_lint_wildcard_regex_detected(policy):
    """測試萬用字元正規表達式偵測。"""
    results = lint_custom_rules.lint_expr(
        'my_metric{job=~".*"} > 0', policy, "test.yaml", "TestRule"
    )
    pat_errors = [r for r in results if "denied pattern" in r.message]
    assert len(pat_errors) >= 1

def test_lint_wildcard_regex_with_space_detected(policy):
    """測試空格變體: =~ ".*"。"""
    results = lint_custom_rules.lint_expr(
        'my_metric{job=~ ".*"} > 0', policy, "test.yaml", "TestRule"
    )
    pat_errors = [r for r in results if "denied pattern" in r.message]
    assert len(pat_errors) >= 1

def test_lint_without_tenant_detected(policy):
    """測試 without(tenant) 偵測。"""
    results = lint_custom_rules.lint_expr(
        "sum without(tenant) (my_metric)", policy, "test.yaml", "TestRule"
    )
    pat_errors = [r for r in results if "without(tenant)" in r.message]
    assert len(pat_errors) >= 1

def test_lint_without_tenant_space_detected(policy):
    """測試空格變體: without (tenant)。"""
    results = lint_custom_rules.lint_expr(
        "sum without (tenant) (my_metric)", policy, "test.yaml", "TestRule"
    )
    pat_errors = [r for r in results if "without(tenant)" in r.message]
    assert len(pat_errors) >= 1

def test_lint_safe_pattern_passes(policy):
    """測試安全 pattern 通過檢查。"""
    results = lint_custom_rules.lint_expr(
        'my_metric{job="mysql"} > 0', policy, "test.yaml", "TestRule"
    )
    pat_errors = [r for r in results if "denied pattern" in r.message]
    assert len(pat_errors) == 0


def test_lint_exceeds_max_range(policy):
    """測試超出最大範圍。"""
    results = lint_custom_rules.lint_expr(
        "rate(my_metric[7d])", policy, "test.yaml", "TestRule"
    )
    range_errors = [r for r in results if "range vector" in r.message]
    assert len(range_errors) == 1

def test_lint_within_max_range(policy):
    """測試在最大範圍內。"""
    results = lint_custom_rules.lint_expr(
        "rate(my_metric[30m])", policy, "test.yaml", "TestRule"
    )
    range_errors = [r for r in results if "range vector" in r.message]
    assert len(range_errors) == 0

def test_lint_exact_max_range_passes(policy):
    """測試精確最大範圍。"""
    results = lint_custom_rules.lint_expr(
        "rate(my_metric[1h])", policy, "test.yaml", "TestRule"
    )
    range_errors = [r for r in results if "range vector" in r.message]
    assert len(range_errors) == 0


def test_lint_missing_tenant_label(policy):
    """測試缺少租戶標籤。"""
    results = lint_custom_rules.lint_labels(
        {"severity": "warning"}, policy, "test.yaml", "TestRule", is_recording=False
    )
    assert len(results) == 1
    assert "tenant" in results[0].message

def test_lint_has_tenant_label(policy):
    """測試有租戶標籤。"""
    results = lint_custom_rules.lint_labels(
        {"severity": "warning", "tenant": "db-a"}, policy, "test.yaml", "TestRule",
        is_recording=False
    )
    assert len(results) == 0

def test_lint_recording_rule_skips_label_check(policy):
    """測試記錄規則跳過標籤檢查。"""
    results = lint_custom_rules.lint_labels(
        {}, policy, "test.yaml", "TestRule", is_recording=True
    )
    assert len(results) == 0


def test_expiry_missing_expiry_warns():
    """測試缺少到期日期警告。"""
    results = lint_custom_rules.check_expiry_label(
        {"owner": "team-a"}, "test.yaml", "TestRule"
    )
    assert len(results) == 1
    assert results[0].severity == "WARN"

def test_expiry_has_expiry_passes():
    """測試有到期日期通過檢查。"""
    results = lint_custom_rules.check_expiry_label(
        {"expiry": "2026-06-30"}, "test.yaml", "TestRule"
    )
    assert len(results) == 0

def test_owner_missing_owner_warns():
    """測試缺少擁有者警告。"""
    results = lint_custom_rules.check_owner_label(
        {"expiry": "2026-06-30"}, "test.yaml", "TestRule"
    )
    assert len(results) == 1
    assert results[0].severity == "WARN"

def test_owner_has_owner_passes():
    """測試有擁有者通過檢查。"""
    results = lint_custom_rules.check_owner_label(
        {"owner": "team-a"}, "test.yaml", "TestRule"
    )
    assert len(results) == 0


def test_lint_file_direct_rule_format():
    """測試直接 Prometheus rule group 格式。"""
    content = {
        "groups": [{
            "name": "test_group",
            "rules": [{
                "alert": "TestAlert",
                "expr": "my_metric > 100",
                "labels": {"severity": "warning", "tenant": "db-a",
                           "owner": "team-a", "expiry": "2026-12-31"},
            }]
        }]
    }
    with tempfile.NamedTemporaryFile(
        mode='w', suffix='.yaml', delete=False, encoding='utf-8'
    ) as f:
        yaml.safe_dump(content, f)
        fpath = f.name
    try:
        results, count = lint_custom_rules.lint_file(
            fpath, lint_custom_rules.DEFAULT_POLICY
        )
        assert count == 1
        # Should not have errors (clean rule)
        errors = [r for r in results if r.severity == "ERROR"]
        assert len(errors) == 0
    finally:
        os.unlink(fpath)

def test_lint_file_configmap_wrapper():
    """測試 ConfigMap data wrapper 格式。"""
    inner_yaml = yaml.safe_dump({
        "groups": [{
            "name": "wrapped_group",
            "rules": [{
                "alert": "WrappedAlert",
                "expr": 'holt_winters(my_metric[1h], 0.3, 0.7) > 100',
                "labels": {"severity": "critical"},
            }]
        }]
    })
    content = {"data": {"rules.yaml": inner_yaml}}
    with tempfile.NamedTemporaryFile(
        mode='w', suffix='.yaml', delete=False, encoding='utf-8'
    ) as f:
        yaml.safe_dump(content, f)
        fpath = f.name
    try:
        results, count = lint_custom_rules.lint_file(
            fpath, lint_custom_rules.DEFAULT_POLICY
        )
        assert count == 1
        errors = [r for r in results if r.severity == "ERROR"]
        # Should detect: denied function + missing tenant label
        assert len(errors) >= 2
    finally:
        os.unlink(fpath)

def test_lint_file_empty_file():
    """測試空檔案不應出錯。"""
    with tempfile.NamedTemporaryFile(
        mode='w', suffix='.yaml', delete=False, encoding='utf-8'
    ) as f:
        f.write("")
        fpath = f.name
    try:
        results, count = lint_custom_rules.lint_file(
            fpath, lint_custom_rules.DEFAULT_POLICY
        )
        assert count == 0
        assert len(results) == 0
    finally:
        os.unlink(fpath)

def test_lint_file_nonexistent_file():
    """測試不存在的檔案應回傳 ERROR。"""
    results, count = lint_custom_rules.lint_file(
        "/nonexistent/path.yaml", lint_custom_rules.DEFAULT_POLICY
    )
    assert count == 0
    assert len(results) == 1
    assert results[0].severity == "ERROR"


def test_load_policy_no_policy_uses_defaults():
    """測試無政策時使用預設值。"""
    policy = lint_custom_rules.load_policy(None)
    assert policy == lint_custom_rules.DEFAULT_POLICY

def test_load_policy_custom_policy_overrides():
    """測試自訂政策覆寫。"""
    custom = {"max_range_duration": "2h", "denied_functions": ["my_func"]}
    with tempfile.NamedTemporaryFile(
        mode='w', suffix='.yaml', delete=False, encoding='utf-8'
    ) as f:
        yaml.safe_dump(custom, f)
        fpath = f.name
    try:
        policy = lint_custom_rules.load_policy(fpath)
        assert policy["max_range_duration"] == "2h"
        assert policy["denied_functions"] == ["my_func"]
        # Unspecified keys should retain defaults
        assert policy["required_labels"] == ["tenant"]
    finally:
        os.unlink(fpath)

def test_load_policy_unreadable_policy_is_caller_error():
    """讀不到的 --policy 必須 exit 2，不可回退到內建預設。

    ⛔ 這條原本叫 `test_load_policy_invalid_policy_falls_back`，斷言
    `== DEFAULT_POLICY`（#1556）。「回退」的實際後果是：`lint --policy <打錯的路徑>`
    拿內建規則去 lint、印一行沒人在 CI 裡讀的 stderr 警告、然後 exit 0——
    操作者以為自己的政策生效了。dev-rules #13 把「檔案/路徑不存在」歸在
    EXIT_CALLER_ERROR。
    """
    with pytest.raises(SystemExit) as exc:
        lint_custom_rules.load_policy("/nonexistent/policy.yaml")
    assert exc.value.code == 2


# ── #1618: scan targets that do not exist / files that cannot be decoded ──

_HOLT_RULE = (
    "groups:\n"
    "- name: g\n"
    "  rules:\n"
    "  - alert: Bad\n"
    "    expr: holt_winters(my_metric[1h], 0.3, 0.7) > 1\n"
    "    labels:\n"
    "      severity: critical\n"
)


@pytest.mark.parametrize("extra", [[], ["--ci"]], ids=["plain", "ci"])
def test_main_missing_target_is_caller_error(tmp_path, monkeypatch, capsys, extra):
    """不存在的掃描目標 → exit 2 並指名路徑；有無 --ci 答案相同。

    ⛔ 之前 collect_files 直接略過它，`lint /typo --ci` 印 "No YAML files
    found." 然後 exit 0——一個沒人掃過的路徑通過了治理閘門。
    """
    missing = tmp_path / "missing"
    monkeypatch.setattr(sys, "argv", ["lint_custom_rules", str(missing), *extra])
    with pytest.raises(SystemExit) as exc:
        lint_custom_rules.main()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert str(missing) in err
    assert "Do not drop the path" in err


def test_main_missing_target_names_only_the_missing_one(tmp_path, monkeypatch, capsys):
    """`lint <真目錄> <不存在>` → 2，訊息只指名不存在的那個。"""
    real = tmp_path / "real"
    real.mkdir()
    missing = tmp_path / "missing"
    monkeypatch.setattr(sys, "argv", ["lint_custom_rules", str(real), str(missing)])
    with pytest.raises(SystemExit) as exc:
        lint_custom_rules.main()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert repr(str(missing)) in err
    assert repr(str(real)) not in err


@pytest.mark.parametrize("extra", [[], ["--ci"]], ids=["plain", "ci"])
def test_main_empty_string_target_is_caller_error(tmp_path, monkeypatch, capsys, extra):
    """`lint ''` → exit 2 and the message shows the empty string as ''.

    Blind-review follow-up to #1618. `Path('')` is `PosixPath('.')`, so
    `exists()` is True and the missing-target guard let it through: measured
    on HEAD (820c7785) from a directory holding rule files, `lint ''` scanned
    the CURRENT DIRECTORY and exited 0 — an unset shell variable turned into
    "lint whatever is here".
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "rules.yaml").write_text(_HOLT_RULE, encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["lint_custom_rules", "", *extra])
    with pytest.raises(SystemExit) as exc:
        lint_custom_rules.main()
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "''" in captured.err
    assert "Do not drop the path" in captured.err
    assert "Scanned" not in captured.out      # nothing in cwd was linted


def test_main_empty_string_alongside_real_dir_names_only_the_empty_one(
        tmp_path, monkeypatch, capsys):
    """`lint <real dir> ''` → 2, message names '' and not the real dir."""
    real = tmp_path / "real"
    real.mkdir()
    monkeypatch.setattr(sys, "argv", ["lint_custom_rules", str(real), ""])
    with pytest.raises(SystemExit) as exc:
        lint_custom_rules.main()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "''" in err
    assert repr(str(real)) not in err


def test_main_existing_empty_dir_is_ok(tmp_path, monkeypatch, capsys):
    """control：存在但沒有 YAML 的目錄 → exit 0，"No YAML files found" 並指名目標。"""
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(sys, "argv", ["lint_custom_rules", str(empty), "--ci"])
    with pytest.raises(SystemExit) as exc:
        lint_custom_rules.main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "No YAML files found" in out
    assert str(empty) in out


_CP950_BYTES = b"groups:\n  - name: \xa4\xa4\n"   # cp950「中」— invalid UTF-8


def test_lint_file_non_utf8_is_one_error_result(tmp_path):
    """非 UTF-8 檔案 → 一筆 ERROR "cannot read file"，不 raise。

    ⛔ UnicodeDecodeError 是 ValueError、不是 OSError；原本的 `except OSError`
    看不到它，整個 run 以 traceback 收場、stdout 0 bytes。
    """
    bad = tmp_path / "aa_bad.yaml"
    bad.write_bytes(_CP950_BYTES)
    results, count = lint_custom_rules.lint_file(str(bad), lint_custom_rules.DEFAULT_POLICY)
    assert count == 0
    assert len(results) == 1
    assert results[0].severity == "ERROR"
    assert "cannot read file" in results[0].message


def test_main_non_utf8_file_is_quarantined_per_file(tmp_path, monkeypatch, capsys):
    """--ci 下：壞檔一筆 ERROR，其他檔案的 finding 照印，整體 exit 1。"""
    (tmp_path / "aa_bad.yaml").write_bytes(_CP950_BYTES)
    (tmp_path / "zz_ok.yaml").write_text(_HOLT_RULE, encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["lint_custom_rules", str(tmp_path), "--ci"])
    with pytest.raises(SystemExit) as exc:
        lint_custom_rules.main()
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "cannot read file" in out
    assert "aa_bad.yaml" in out
    assert "zz_ok.yaml" in out
    assert "holt_winters" in out


def test_group_interval_exceeds_max():
    """測試超出最大求值時間間隔。"""
    policy = {"max_evaluation_interval": "60s"}
    results = lint_custom_rules.lint_group_interval(
        "120s", policy, "test.yaml", "my_group"
    )
    assert len(results) == 1
    assert results[0].severity == "WARN"

def test_group_interval_within_max():
    """測試在最大求值時間間隔內。"""
    policy = {"max_evaluation_interval": "60s"}
    results = lint_custom_rules.lint_group_interval(
        "30s", policy, "test.yaml", "my_group"
    )
    assert len(results) == 0


def test_collect_yaml_files():
    """測試收集 YAML 檔案。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create test files
        for name in ["a.yaml", "b.yml", "c.txt", "d.yaml"]:
            with open(os.path.join(tmpdir, name), 'w') as f:
                f.write("test")
        files = lint_custom_rules.collect_files([tmpdir])
        assert len(files) == 3  # a.yaml, b.yml, d.yaml
        assert all(f.endswith((".yaml", ".yml")) for f in files)

def test_collect_single_file():
    """測試收集單一檔案。"""
    with tempfile.NamedTemporaryFile(
        suffix='.yaml', delete=False
    ) as f:
        fpath = f.name
    try:
        files = lint_custom_rules.collect_files([fpath])
        assert len(files) == 1
    finally:
        os.unlink(fpath)


# ── Per-file overrides: GENERATED compiled pack (forecast / predict_linear) ──
#
# Regression for the latent CI conflict found while reviewing #819: the policy
# denies predict_linear + ranges >1h for tenant raw rules, but the forecast
# recipe (ADR-024 能力 B) legitimately compiles to predict_linear with a
# platform-derived lookback of up to 96h inside the GENERATED
# rule-packs/rule-pack-custom-alerts.yaml. The `file_overrides` exemption must
# let the compiled forecast shape through while keeping every other check live.

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
_REAL_POLICY = os.path.join(_REPO_ROOT, ".github", "custom-rule-policy.yaml")

_GENERATED_PACK_RELPATH = os.path.join("rule-packs", "rule-pack-custom-alerts.yaml")


def _compiled_forecast_pack_text():
    """Compile a real forecast declaration (max horizon 48h → lookback 96h =
    [345600s]) through the actual compiler + renderer, so the fixture cannot
    drift from what `make custom-alerts-compile` emits."""
    import compile_custom_alerts as cc
    conf = (
        "tenants:\n"
        "  ta:\n"
        "    _custom_alerts:\n"
        '      - {recipe: forecast, name: disk_full, metric: disk_avail,'
        ' capacity_metric: disk_cap, op: "<", horizon: 48h,'
        ' threshold: "0.15:warning"}\n'
    )
    with tempfile.TemporaryDirectory() as confdir:
        with open(os.path.join(confdir, "a.yaml"), "w", encoding="utf-8") as f:
            f.write(conf)
        pack = cc.build_pack(confdir)
    return cc._render(pack["groups"])


@pytest.fixture
def compiled_forecast_pack(tmp_path):
    """A real compiled forecast pack written at the canonical relative path."""
    text = _compiled_forecast_pack_text()
    assert "predict_linear" in text          # fixture sanity: forecast shape present
    assert "[345600s]" in text               # 96h lookback (horizon 48h × 2)
    fpath = tmp_path / _GENERATED_PACK_RELPATH
    fpath.parent.mkdir(parents=True)
    fpath.write_text(text, encoding="utf-8")
    return fpath


def test_forecast_in_generated_pack_passes_real_policy(compiled_forecast_pack):
    """forecast shape 出現在 compiled pack 時，真 policy 檔不誤殺（0 ERROR）。"""
    policy = lint_custom_rules.load_policy(_REAL_POLICY)
    assert os.path.isfile(_REAL_POLICY)      # guard: fallback-to-defaults would hide drift
    results, count = lint_custom_rules.lint_file(str(compiled_forecast_pack), policy)
    errors = [r for r in results if r.severity == "ERROR"]
    assert count > 0
    assert errors == [], [str(r) for r in errors]


def test_forecast_in_generated_pack_passes_default_policy(compiled_forecast_pack):
    """validate_config 無 --policy 時走 DEFAULT_POLICY，行為須一致。"""
    results, _ = lint_custom_rules.lint_file(
        str(compiled_forecast_pack), lint_custom_rules.DEFAULT_POLICY
    )
    errors = [r for r in results if r.severity == "ERROR"]
    assert errors == [], [str(r) for r in errors]


def test_generated_pack_without_marker_stays_denied(compiled_forecast_pack):
    """缺 GENERATED 檔頭 → 豁免不生效：predict_linear 照殺 + 標記 ERROR。"""
    text = compiled_forecast_pack.read_text(encoding="utf-8")
    stripped = "\n".join(
        line for line in text.splitlines() if not line.startswith("#")
    )
    compiled_forecast_pack.write_text(stripped, encoding="utf-8")
    results, _ = lint_custom_rules.lint_file(
        str(compiled_forecast_pack), lint_custom_rules.DEFAULT_POLICY
    )
    messages = [r.message for r in results if r.severity == "ERROR"]
    assert any("GENERATED" in m for m in messages)
    assert any("predict_linear" in m for m in messages)


def test_override_scoped_to_path_only(compiled_forecast_pack, tmp_path):
    """同內容、不同檔名 → 不豁免（override 綁定 path）。"""
    other = tmp_path / "rule-packs" / "rule-pack-handwritten.yaml"
    other.write_text(
        compiled_forecast_pack.read_text(encoding="utf-8"), encoding="utf-8"
    )
    results, _ = lint_custom_rules.lint_file(
        str(other), lint_custom_rules.DEFAULT_POLICY
    )
    messages = [r.message for r in results if r.severity == "ERROR"]
    assert any("predict_linear" in m for m in messages)


def test_override_keeps_other_checks_live(tmp_path):
    """豁免檔內其餘 deny 照跑：holt_winters / 超過 96h range 仍是 ERROR。"""
    text = (
        "# GENERATED test fixture — mimics compile_custom_alerts header\n"
        "groups:\n"
        "- name: g\n"
        "  rules:\n"
        "  - record: custom:metric:x\n"
        "    expr: holt_winters(my_metric[1h], 0.3, 0.7)\n"
        "  - record: custom:metric:y\n"
        "    expr: avg_over_time(my_metric[7d])\n"
    )
    fpath = tmp_path / _GENERATED_PACK_RELPATH
    fpath.parent.mkdir(parents=True)
    fpath.write_text(text, encoding="utf-8")
    results, _ = lint_custom_rules.lint_file(
        str(fpath), lint_custom_rules.DEFAULT_POLICY
    )
    messages = [r.message for r in results if r.severity == "ERROR"]
    assert any("holt_winters" in m for m in messages)
    assert any("range vector [7d]" in m for m in messages)


def test_committed_pack_and_policy_in_sync():
    """釘住三方同步：committed pack 帶 GENERATED 標記、真 policy 檔與
    DEFAULT_POLICY 的 file_overrides 等價（避免 validate_config 路徑漂移）。"""
    committed = os.path.join(_REPO_ROOT, _GENERATED_PACK_RELPATH)
    with open(committed, encoding="utf-8") as f:
        head = list(itertools.islice(f, lint_custom_rules.GENERATED_MARKER_SCAN_LINES))
    assert any(lint_custom_rules.GENERATED_MARKER in line for line in head)

    real = lint_custom_rules.load_policy(_REAL_POLICY)
    assert real["file_overrides"] == lint_custom_rules.DEFAULT_POLICY["file_overrides"]


def test_nested_path_does_not_get_exemption(compiled_forecast_pack, tmp_path):
    """對抗式 review FINDING 1：租戶把合法檔名塞到 rule-packs/ 樹的 NESTED 路徑
    （suffix 仍命中 override.path）不得取得豁免——否則 predict_linear + 96h range
    被遞迴掃描放行，而 drift gate 只看單一 canonical 路徑、抓不到此檔。"""
    text = compiled_forecast_pack.read_text(encoding="utf-8")  # 帶真 GENERATED 檔頭
    nested = tmp_path / "rule-packs" / "custom" / "rule-packs" / "rule-pack-custom-alerts.yaml"
    nested.parent.mkdir(parents=True)
    nested.write_text(text, encoding="utf-8")
    results, _ = lint_custom_rules.lint_file(
        str(nested), lint_custom_rules.DEFAULT_POLICY
    )
    messages = [r.message for r in results if r.severity == "ERROR"]
    assert any("predict_linear" in m for m in messages), \
        "nested path must be fully linted, not exempted"


def test_override_cannot_relax_non_whitelisted_check(tmp_path):
    """對抗式 review FINDING 3：override.policy 設 OVERRIDABLE_POLICY_KEYS 以外的 key
    （如清空 required_labels）不得生效——該 key 被忽略且回報 ERROR，原檢查照跑。"""
    text = (
        "# GENERATED test fixture\n"
        "groups:\n"
        "- name: g\n"
        "  rules:\n"
        "  - alert: NoTenant\n"          # 缺 tenant label → required_labels 應照殺
        "    expr: my_metric > 1\n"
        "    labels: {severity: warning}\n"
    )
    fpath = tmp_path / _GENERATED_PACK_RELPATH
    fpath.parent.mkdir(parents=True)
    fpath.write_text(text, encoding="utf-8")
    policy = lint_custom_rules.DEFAULT_POLICY.copy()
    policy["file_overrides"] = [{
        "path": _GENERATED_PACK_RELPATH.replace(os.sep, "/"),
        "require_generated_marker": True,
        "policy": {"required_labels": []},   # 攻擊：想關掉 tenant label 強制
    }]
    results, _ = lint_custom_rules.lint_file(str(fpath), policy)
    messages = [r.message for r in results if r.severity == "ERROR"]
    assert any("required label 'tenant'" in m for m in messages)   # 仍照殺
    assert any("may only relax" in m for m in messages)            # 且回報忽略


def test_malformed_override_entry_does_not_crash(tmp_path):
    """對抗式 review QUAL-1：file_overrides 含非 dict（字串）項不得 traceback。"""
    fpath = tmp_path / _GENERATED_PACK_RELPATH
    fpath.parent.mkdir(parents=True)
    fpath.write_text("# GENERATED\ngroups: []\n", encoding="utf-8")
    policy = lint_custom_rules.DEFAULT_POLICY.copy()
    policy["file_overrides"] = ["oops-a-string", None, {"path": "other.yaml"}]
    results, count = lint_custom_rules.lint_file(str(fpath), policy)  # 不應 raise
    assert count == 0


def test_lint_result_error_format():
    """測試 LintResult 錯誤格式。"""
    r = lint_custom_rules.LintResult("test.yaml", "MyRule", None, "ERROR", "bad thing")
    assert str(r) == "ERROR: test.yaml [MyRule] - bad thing"

def test_lint_result_with_line_hint():
    """測試 LintResult 包含行提示。"""
    r = lint_custom_rules.LintResult("test.yaml", "MyRule", 42, "WARN", "warning")
    assert str(r) == "WARN: test.yaml:42 [MyRule] - warning"

def test_lint_result_no_rule_name():
    """測試 LintResult 無規則名稱。"""
    r = lint_custom_rules.LintResult("test.yaml", None, None, "ERROR", "msg")
    assert str(r) == "ERROR: test.yaml - msg"
