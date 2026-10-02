"""讀取端語意矩陣：值寫在哪裡，讀取端的答案都要與 Go 的 served-values 一致（#2115 0-B／B1）。

讀取端：`blind_spot_discovery.load_monitored_db_types`、
`analyze_rule_pack_gaps.load_tenant_configs(config_dir=...)`（目錄模式）。

同一個值（`custom_mysql_slow: 5`）只寫在四個位置之一：根 `defaults:`、平台檔
`tenants:`、租戶檔、子樹 `_defaults.yaml`。tenant-a 是受測租戶；tenant-b 是對照組，
值永遠寫在自己的租戶檔（7），四個位置下答案都不變。

`mysql_connections` 只有根 defaults 有（80），租戶都沒寫：已監控（mariadb）只能來自
繼承。舊讀取端（`_lib_io.load_tenant_configs`）只讀根目錄租戶檔本身，所以除了
「租戶檔」那一格，tenant-a 的答案都錯（子樹那格整個租戶消失）。

另有幾格：平面格式檔（Go 列進 `skipped`）、Go 丟掉的檔（`parse_failed`，fail-closed
rc 2，訊息附解析原因）、exporter 讀不到的檔（`unreadable`，同樣 rc 2；指向目錄的 symlink
例外：Go 只 WARN，讀取端轉印、rc 0）、
`disable`／沒有預設值的鍵（不算已監控）。

da-guard 由 conftest 的 session fixture 以 `go build` 建出；建不起來就 fail、不 skip。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import _lib_tenant_values as tv
import analyze_rule_pack_gaps as arg
import blind_spot_discovery as bsd
from _platform_fs import require_file_name, symlink_or_skip  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS = REPO_ROOT / "scripts" / "tools" / "ops"

pytestmark = pytest.mark.usefixtures("da_guard_env")

_BASE = "defaults:\n  mysql_connections: 80\n  custom_mysql_slow: 1\n"
_A_PLAIN = "tenants:\n  tenant-a:\n    _silent_mode: disable\n"
_A_VALUE = "tenants:\n  tenant-a:\n    custom_mysql_slow: 5\n"
_B = "tenants:\n  tenant-b:\n    custom_mysql_slow: 7\n"

POSITIONS = {
    "defaults": {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n  custom_mysql_slow: 5\n",
        "tenant-a.yaml": _A_PLAIN,
        "tenant-b.yaml": _B,
    },
    "platform-tenants": {
        "_defaults.yaml": _BASE + _A_VALUE,
        "tenant-a.yaml": _A_PLAIN,
        "tenant-b.yaml": _B,
    },
    "tenant-file": {
        "_defaults.yaml": _BASE,
        "tenant-a.yaml": _A_VALUE,
        "tenant-b.yaml": _B,
    },
    "subtree": {
        "_defaults.yaml": _BASE,
        "team/_defaults.yaml": "defaults:\n  custom_mysql_slow: 5\n",
        "team/tenant-a.yaml": _A_PLAIN,
        "tenant-b.yaml": _B,
    },
}


def _tree(root: Path, files: dict[str, str]) -> Path:
    conf_d = root / "conf.d"
    for rel, body in files.items():
        p = conf_d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return conf_d


@pytest.mark.parametrize("where", sorted(POSITIONS))
def test_the_oracle_serves_the_value_in_every_position(where, tmp_path):
    """前提（非空洞）：Go 在四個位置都發出 tenant-a=5、tenant-b=7。"""
    served = tv.load_served_values(_tree(tmp_path, POSITIONS[where]))
    assert served["tenant-a"].values["custom_mysql_slow"] == 5.0
    assert served["tenant-a"].values["mysql_connections"] == 80.0
    assert served["tenant-b"].values["custom_mysql_slow"] == 7.0


@pytest.mark.parametrize("where", sorted(POSITIONS))
def test_analyze_gaps_dir_mode_matches_served_values(where, tmp_path):
    conf_d = _tree(tmp_path, POSITIONS[where])
    served = tv.load_served_values(conf_d)
    got = arg.load_tenant_configs(config_dir=str(conf_d))
    for tenant in ("tenant-a", "tenant-b"):
        assert got[tenant]["custom_mysql_slow"] == served[tenant].values["custom_mysql_slow"], (where, tenant)
    assert got["tenant-a"]["custom_mysql_slow"] == 5.0, where
    assert got["tenant-b"]["custom_mysql_slow"] == 7.0, where  # 對照組不變
    metrics = {(m["tenant"], m["metric_key"]): m["value"] for m in arg.extract_custom_metrics(got)}
    assert metrics == {("tenant-a", "custom_mysql_slow"): 5.0, ("tenant-b", "custom_mysql_slow"): 7.0}, where


@pytest.mark.parametrize("where", sorted(POSITIONS))
def test_blind_spot_monitored_set_matches_served_values(where, tmp_path):
    conf_d = _tree(tmp_path, POSITIONS[where])
    served = tv.load_served_values(conf_d)
    want: dict[str, set[str]] = {}
    for tenant, t in served.items():
        for key in t.severities:
            db = bsd._infer_db_type_from_metric(key)
            if db:
                want.setdefault(db, set()).add(tenant)
    got = bsd.load_monitored_db_types(str(conf_d))
    assert got == want, where
    # 繼承來的 mysql_connections：兩個租戶在四個位置都算 mariadb 已監控。
    assert got == {"mariadb": {"tenant-a", "tenant-b"}}, where


def test_disabled_and_unserved_keys_are_not_monitored(tmp_path):
    """行為變更（changelog.d）：`disable` 的鍵與沒有預設值、Go 不發的鍵不算已監控。"""
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  redis_memory: 1024\n",
        "tenant-a.yaml": "tenants:\n  tenant-a:\n    redis_memory: disable\n",
        "tenant-b.yaml": "tenants:\n  tenant-b:\n    mysql_slow_queries: 5\n    custom_pg_x: 3\n",
    })
    served = tv.load_served_values(conf_d)
    assert "redis_memory" not in served["tenant-a"].values
    assert "mysql_slow_queries" not in served["tenant-b"].values
    assert bsd.load_monitored_db_types(str(conf_d)) == {"redis": {"tenant-b"}}
    assert arg.load_tenant_configs(config_dir=str(conf_d)) == {
        "tenant-a": {}, "tenant-b": {"redis_memory": 1024.0}}


def _cli(script: str, conf_d: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(OPS / script), "--config-dir", str(conf_d), *extra],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)


_CLIS = [
    ("blind_spot_discovery.py", ("--prometheus", "http://127.0.0.1:1", "--json-output")),
    ("analyze_rule_pack_gaps.py", ("--json",)),
]


@pytest.mark.parametrize("script, extra", _CLIS, ids=[c[0] for c in _CLIS])
def test_flat_file_is_named_from_skipped_and_is_not_a_tenant(script, extra, tmp_path):
    """R3：平面格式檔由 Go 判定（`skipped`），讀取端照欄位具名 WARN，不當租戶。"""
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": _BASE,
        "tenant-b.yaml": _B,
        "flat-a.yaml": "custom_mysql_slow: 5\n",
    })
    assert [s.file for s in tv.load_served_tree(conf_d).skipped] == ["flat-a.yaml"]
    p = _cli(script, conf_d, *extra)
    assert p.returncode == 0, p.stderr
    assert "WARN: flat-a.yaml: declares no tenant" in p.stderr, p.stderr
    assert "flat-a" not in p.stdout, p.stdout


@pytest.mark.parametrize("script, extra", _CLIS, ids=[c[0] for c in _CLIS])
def test_file_the_exporter_drops_fails_closed(script, extra, tmp_path):
    """被 exporter 丟掉的檔（`parse_failed`）：rc 2、指名該檔、沒有 traceback（R5 推翻後的契約）。"""
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": _BASE,
        "tenant-b.yaml": _B,
        "_platform.yaml": "tenants:\n  tenant-b: 3\n",
    })
    p = _cli(script, conf_d, *extra)
    assert p.returncode == 2, (p.returncode, p.stderr)
    assert "_platform.yaml" in p.stderr and "Traceback" not in p.stderr, p.stderr
    assert "cannot unmarshal" in p.stderr, p.stderr  # exporter 自己的解析原因


@pytest.mark.parametrize("script, extra", _CLIS, ids=[c[0] for c in _CLIS])
def test_tree_the_exporter_rejects_fails_closed(script, extra, tmp_path):
    """跨檔重複宣告：Go rc 2，讀取端也 rc 2、一行 ERROR、沒有 traceback。"""
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": _BASE,
        "b1.yaml": _B,
        "b2.yaml": _B,
    })
    p = _cli(script, conf_d, *extra)
    assert p.returncode == 2, (p.returncode, p.stderr)
    assert "duplicate tenant" in p.stderr and "Traceback" not in p.stderr, p.stderr


@pytest.mark.parametrize("script, extra", _CLIS, ids=[c[0] for c in _CLIS])
def test_dangling_symlink_fails_closed(script, extra, tmp_path):
    """懸空 symlink `tenant-b.yaml`：exporter 只 WARN 後跳過、該租戶從 /metrics 消失。
    Go 列進 `unreadable`（rc 3），讀取端 rc 2、ERROR 行指名該檔與原因（行為變更：原為 rc 0）。
    不依賴權限，root 下也會跑。"""
    conf_d = _tree(tmp_path, {"_defaults.yaml": _BASE, "tenant-a.yaml": _A_OK})
    symlink_or_skip("missing.yaml", conf_d / "tenant-b.yaml")
    p = _cli(script, conf_d, *extra)
    assert p.returncode == 2, (p.returncode, p.stderr)
    err = [ln for ln in p.stderr.split("\n") if ln.startswith("ERROR: ")]
    assert len(err) == 1 and "tenant-b.yaml (stat_error)" in err[0], p.stderr
    assert "Traceback" not in p.stderr and p.stdout == "", (p.stdout, p.stderr)


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0,
                    reason="chmod 000 does not stop root (or Windows); the dangling-symlink row covers unreadable")
@pytest.mark.parametrize("name", ["tenant-a.yaml", "_defaults.yaml"])
@pytest.mark.parametrize("script, extra", _CLIS, ids=[c[0] for c in _CLIS])
def test_file_without_read_permission_fails_closed(script, extra, name, tmp_path):
    """讀取權限不足：租戶檔（該租戶消失）或 `_defaults.yaml`（所有鍵變成沒有預設值）
    都是 rc 2、指名該檔（`read_error`），不再 rc 0 只轉印一行 WARN。"""
    conf_d = _tree(tmp_path, {"_defaults.yaml": _BASE, "tenant-a.yaml": _A_OK, "tenant-b.yaml": _B})
    (conf_d / name).chmod(0)
    try:
        p = _cli(script, conf_d, *extra)
    finally:
        (conf_d / name).chmod(0o644)
    assert p.returncode == 2, (p.returncode, p.stderr)
    err = [ln for ln in p.stderr.split("\n") if ln.startswith("ERROR: ")]
    assert len(err) == 1 and f"{name} (read_error)" in err[0], p.stderr
    assert "Traceback" not in p.stderr, p.stderr


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0,
                    reason="chmod 000 does not stop root (or Windows); pkg/config has a Linux walk_error shape for root")
@pytest.mark.parametrize("script, extra", _CLIS, ids=[c[0] for c in _CLIS])
def test_subdir_without_permission_fails_closed(script, extra, tmp_path):
    """讀不到的子目錄（chmod 000）：底下的租戶全部從 /metrics 消失。Go 列進
    `unreadable`（`walk_error`，路徑是該目錄），讀取端 rc 2、指名該目錄（原為 rc 0）。"""
    conf_d = _tree(tmp_path, {"_defaults.yaml": _BASE, "tenant-a.yaml": _A_OK,
                              "team/tenant-b.yaml": _B})
    (conf_d / "team").chmod(0)
    try:
        p = _cli(script, conf_d, *extra)
    finally:
        (conf_d / "team").chmod(0o755)
    assert p.returncode == 2, (p.returncode, p.stderr)
    err = [ln for ln in p.stderr.split("\n") if ln.startswith("ERROR: ")]
    assert len(err) == 1 and "team (walk_error)" in err[0], p.stderr
    assert "Traceback" not in p.stderr, p.stderr


@pytest.mark.parametrize("script, extra", _CLIS, ids=[c[0] for c in _CLIS])
def test_file_the_exporter_cannot_read_is_warned_and_rc_stays_0(script, extra, tmp_path):
    """例外（owner 裁決）：指向目錄的 symlink `tb.yaml`——exporter 本來就不跟進
    （k8s ConfigMap 巢狀路徑會掛成目錄 symlink）。Go 只 WARN 後跳過，不進
    parse_failed／skipped／unreadable，rc 0。讀取端把 da-guard 的那行 WARN 轉印到 stderr，
    stdout 照常是一份 JSON。"""
    conf_d = _tree(tmp_path, {"_defaults.yaml": _BASE, "tenant-b.yaml": _B})
    (conf_d / "realdir").mkdir()
    symlink_or_skip("realdir", conf_d / "tb.yaml")
    p = _cli(script, conf_d, *extra)
    assert p.returncode == 0, p.stderr
    warn = [ln for ln in p.stderr.split("\n") if ln.startswith(_P + "WARN: cannot read ")]
    assert len(warn) == 1 and "tb.yaml" in warn[0], p.stderr
    json.loads(p.stdout)


# ── da-guard 的 stderr：照轉，不分類、不篩選（第 2 輪盲審 F1–F4）──────────────

_A_OK = "tenants:\n  tenant-a:\n    mysql_connections: 70\n"
_P = tv.DA_GUARD_PREFIX
_DATE = re.compile(r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} ")

# rc 0 時 da-guard 寫在 stderr 的幾種形狀；每一格的每一行都要原樣（逐行、保留縮排）出現。
_RC0_SHAPES = {
    # F1：多行 WARN，第二行是縮排的續行。
    "multi-line WARN": ("tenants:\n  tenant-a:\n    _metadata: [1, 2]\n",
                        ["WARN: tenant=tenant-a: failed to parse _metadata: yaml: unmarshal errors:",
                         "  line 1: cannot unmarshal !!seq into config.TenantMetadata"]),
    # F2：rc 0 的 ERROR 行（custom alert 被拒，其餘照常發出）。
    "ERROR line": ("tenants:\n  tenant-a:\n    _custom_alerts: 5\n",
                   ["ERROR: tenant=tenant-a: custom alert \"<block>\" rejected: cannot parse _custom_alerts:",
                    "  line 1: cannot unmarshal !!int `5` into []config.CustomAlertSpec"]),
    # F4：Go `log` 帶日期時間前綴的 WARN。
    "date-prefixed WARN": ("tenants:\n  tenant-a:\n    mysql_connections:\n      default: \"70\"\n"
                           "      expires: bad\n",
                           ["WARN: invalid expires \"bad\" in threshold \"mysql_connections\" for tenant=tenant-a"]),
}


@pytest.mark.parametrize("shape", sorted(_RC0_SHAPES))
@pytest.mark.parametrize("script, extra", _CLIS, ids=[c[0] for c in _CLIS])
def test_da_guard_stderr_is_passed_on_whole_at_rc_0(script, extra, shape, tmp_path):
    body, wants = _RC0_SHAPES[shape]
    conf_d = _tree(tmp_path, {"_defaults.yaml": _BASE, "tenant-b.yaml": _B, "tenant-a.yaml": body})
    direct = subprocess.run([tv.guard_dispatch.DISPATCHER.resolve_binary(None), "served-values",
                             "--config-dir", str(conf_d)],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    assert direct.returncode == 0, direct.stderr  # 前提：da-guard 這格是 rc 0
    p = _cli(script, conf_d, *extra)
    assert p.returncode == 0, p.stderr
    # 工具 stderr 包含 da-guard stderr 的每一個非空行，各自加上固定前綴（只經 safe_label，
    # 這些行沒有控制字元）。兩次執行的日期時間前綴可能差一秒，比對前去掉。
    passed_on = [ln[len(_P):] for ln in p.stderr.split("\n") if ln.startswith(_P)]
    got = [_DATE.sub("", ln) for ln in passed_on]
    da_lines = [_DATE.sub("", ln.rstrip()) for ln in direct.stderr.split("\n") if ln.strip()]
    assert da_lines and all(ln in got for ln in da_lines), (da_lines, got)
    for want in wants:  # 以及每個形狀該有的那幾行
        assert any(want in ln for ln in got), (want, got)
    if shape == "date-prefixed WARN":  # 日期時間前綴本身也照轉，不被剝掉
        assert any(_DATE.match(ln) and "WARN: invalid expires" in ln for ln in passed_on), p.stderr
    json.loads(p.stdout)


@pytest.mark.parametrize("script, extra", _CLIS, ids=[c[0] for c in _CLIS])
def test_several_files_dropped_each_reason_shown_below_one_error_line(script, extra, tmp_path):
    """F3：三個壞檔＋一行無關 WARN。ERROR 首行只講 parse_failed（不含無關 WARN），
    da-guard 的 stderr 逐行縮排附在下面，每個壞檔的原因都在、多行原因不被擠成一行。"""
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": _BASE,
        "tenant-a.yaml": _A_OK,
        "x1.yaml": "tenants:\n  x1:\n    mysql_connections: [1\n",
        "x3.yaml": "tenants:\n  x3: [1]\n",
        "td.yaml": "defaults:\n  mysql_connections: 5\ntenants:\n  td:\n    mysql_connections: 1\n",
    })
    (conf_d / "x2.yaml").write_bytes(b"tenants:\n  x2:\n    mysql_connections: 1 # \xff\n")
    p = _cli(script, conf_d, *extra)
    assert p.returncode == 2, p.stderr
    lines = p.stderr.splitlines()
    err = [i for i, ln in enumerate(lines) if ln.startswith("ERROR: ")]
    assert len(err) == 1, lines
    head, below = lines[err[0]], lines[err[0] + 1:]
    assert "x1.yaml, x2.yaml, x3.yaml" in head and "defaults found" not in head, head
    assert all(ln.startswith(_P) for ln in below), below
    for reason in ("did not find expected", "invalid leading UTF-8 octet",
                   _P + "  line 2: cannot unmarshal !!seq", "defaults found in td.yaml"):
        assert any(reason in ln for ln in below), (reason, below)


# ── 檔名裡的換行：轉印的每一行都在前綴之後，不會出現在第 0 欄（第 3 輪盲審 F6）──

_FORGED = "[OK] forged.yaml"


def _defaults_in_tenant_file(conf_d: Path, name: bytes) -> None:
    """一個租戶檔，內含 `defaults:`：Go 以 `%s` 把檔名印進 rc 0 的 WARN。"""
    require_file_name(name)
    (conf_d / name.decode("utf-8")).write_bytes(
        b"defaults:\n  mysql_connections: 5\ntenants:\n  td:\n    mysql_connections: 1\n")


@pytest.mark.parametrize("script, extra", _CLIS, ids=[c[0] for c in _CLIS])
def test_nel_in_a_file_name_does_not_start_a_line(script, extra, tmp_path):
    """NEL（U+0085）不再被當成換行切開：整行留在前綴之後，NEL 經 safe_label 變成 `?`。"""
    conf_d = _tree(tmp_path, {"_defaults.yaml": _BASE, "tenant-b.yaml": _B})
    _defaults_in_tenant_file(conf_d, b"nel\xc2\x85" + _FORGED.encode())
    p = _cli(script, conf_d, *extra)
    assert p.returncode == 0, p.stderr
    lines = p.stderr.split("\n")
    assert not any(ln.startswith(_FORGED) for ln in lines), lines
    hit = [ln for ln in lines if _FORGED in ln]
    assert hit and all(ln.startswith(_P) for ln in hit), hit
    assert any("nel?" + _FORGED in ln for ln in hit), hit
    assert "\x85" not in p.stderr
    json.loads(p.stdout)


@pytest.mark.parametrize("script, extra", _CLIS, ids=[c[0] for c in _CLIS])
def test_real_newline_in_a_file_name_stays_behind_the_prefix(script, extra, tmp_path):
    """真的 `\n`：Go 以 `%s` 印出時在位元組層就是兩行，這裡分不出來；
    兩段都在前綴之後，偽造的文字不會出現在第 0 欄。"""
    conf_d = _tree(tmp_path, {"_defaults.yaml": _BASE, "tenant-b.yaml": _B})
    _defaults_in_tenant_file(conf_d, b"nl\n" + _FORGED.encode())
    p = _cli(script, conf_d, *extra)
    assert p.returncode == 0, p.stderr
    lines = p.stderr.split("\n")
    assert not any(ln.startswith(_FORGED) for ln in lines), lines
    assert any(ln.startswith(_P + _FORGED) for ln in lines), lines
    json.loads(p.stdout)
