"""`maintenance_scheduler` 讀到的排程要與 Go 的 `da-guard effective` 一致（#2751）。

#2115 裁決 (c)：保留鍵依「寫法＋繼承」判定，值以 Go 為準。scheduler 原本經 Python 的
`load_tenant_configs` 讀 conf.d，只看得到根目錄租戶檔裡的 `_state_maintenance.recurring`。
修正前（main d3bed81f）實測：

- 租戶檔 `tx.yaml`（對照組）：兩邊都有 tx 的排程。
- 平台檔 `_platform.yaml` 的 `tenants: {tx: {_state_maintenance: …}}`：scheduler `{}`；
  effective 有。
- 子目錄 `team/tx.yaml`：scheduler `{}`（只印 FLAT WARN）；effective 有。
- `_defaults.yaml` 頂層的 `_state_maintenance`：兩邊都沒有（exporter 不繼承它）。

每格比對：`load_recurring_schedules` 的回傳 == 由 effective 的
`effective_config._state_maintenance.recurring` 取出的有效項（cron、duration 皆非空；
reason 缺省為 "Recurring maintenance"）。另外 CLI 對每格 rc 0。

da-guard 不可用時不得靜默回空排程：rc 2、印 ERROR、不印 "No recurring"。

da-guard 由 conftest 的 session fixture 以 `go build` 建出；建不起來就 fail、不 skip。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import _lib_tenant_values as tv
import maintenance_scheduler as ms

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEDULER = REPO_ROOT / "scripts" / "tools" / "ops" / "maintenance_scheduler.py"

pytestmark = pytest.mark.usefixtures("da_guard_env")

_DEFAULTS = "defaults:\n  mysql_connections: 80\n"
_MAINT = ("_state_maintenance:\n  recurring:\n    - cron: \"0 2 * * *\"\n"
          "      duration: \"1h\"\n")


def _indent(text: str, n: int) -> str:
    return "".join(" " * n + line + "\n" for line in text.splitlines())


_TENANT_MAINT = "tenants:\n  tx:\n" + _indent(_MAINT, 4)

# (name, {file: body}); the tenant is always `tx`.
SHAPES = [
    ("tenant-file-CONTROL", {"_defaults.yaml": _DEFAULTS, "tx.yaml": _TENANT_MAINT}),
    ("platform-overlay", {"_defaults.yaml": _DEFAULTS, "_platform.yaml": _TENANT_MAINT,
                          "tx.yaml": "tenants:\n  tx: {}\n"}),
    ("subdirectory-tenant", {"_defaults.yaml": _DEFAULTS, "team/tx.yaml": _TENANT_MAINT}),
    ("defaults-top-level", {"_defaults.yaml": _DEFAULTS + _MAINT,
                            "tx.yaml": "tenants:\n  tx: {}\n"}),
    ("platform-and-tenant", {"_defaults.yaml": _DEFAULTS,
                             "_platform.yaml": _TENANT_MAINT.replace("0 2", "0 3"),
                             "tx.yaml": _TENANT_MAINT}),
    ("with-reason-and-invalid-entry", {
        "_defaults.yaml": _DEFAULTS,
        "tx.yaml": "tenants:\n  tx:\n    _state_maintenance:\n      recurring:\n"
                   "        - cron: \"0 2 * * *\"\n          duration: \"1h\"\n"
                   "          reason: patch\n        - cron: \"0 4 * * *\"\n"}),
    # a root tenant file with no `tenants:`: main read its top-level
    # `_state_maintenance` as tenant `tx`; the exporter reads no tenant from it
    ("root-file-without-tenants", {"_defaults.yaml": _DEFAULTS, "tx.yaml": _MAINT}),
    # a non-string duration (YAML int): skipped with the WARN, no traceback
    ("duration-not-a-string", {
        "_defaults.yaml": _DEFAULTS,
        "tx.yaml": "tenants:\n  tx:\n    _state_maintenance:\n      recurring:\n"
                   "        - cron: \"0 2 * * *\"\n          duration: 3600\n"}),
]


def _tree(root: Path, files: dict[str, str]) -> Path:
    conf_d = root / "conf.d"
    for name, body in files.items():
        p = conf_d / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return conf_d


def _oracle(conf_d: Path) -> dict:
    out = {}
    for tenant, te in tv.load_effective(conf_d).items():
        maint = te.effective_config.get("_state_maintenance")
        if not isinstance(maint, dict):
            continue
        valid = [{"cron": e["cron"].strip(), "duration": e["duration"].strip(),
                  "reason": e.get("reason", "Recurring maintenance")}
                 for e in maint.get("recurring") or []
                 if isinstance(e, dict) and isinstance(e.get("cron"), str)
                 and isinstance(e.get("duration"), str)
                 and e["cron"].strip() and e["duration"].strip()]
        if valid:
            out[tenant] = valid
    return out


def _run(conf_d: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCHEDULER), "--config-dir", str(conf_d)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        env={**os.environ, "PYTHONIOENCODING": "utf-8", **(env or {})})


def test_matrix_is_not_vacuous(tmp_path: Path) -> None:
    """Go reads the schedule in the shapes the old reader missed, and not in
    the defaults one, so equal readings mean something."""
    got = {name: _oracle(_tree(tmp_path / name, files)) for name, files in SHAPES}
    assert len(got) == len(SHAPES)
    for name in ("tenant-file-CONTROL", "platform-overlay", "subdirectory-tenant"):
        assert got[name] == {"tx": [{"cron": "0 2 * * *", "duration": "1h",
                                     "reason": "Recurring maintenance"}]}, name
    assert got["defaults-top-level"] == {}
    assert got["root-file-without-tenants"] == {}
    assert got["duration-not-a-string"] == {}
    assert got["with-reason-and-invalid-entry"]["tx"][0]["reason"] == "patch"


@pytest.mark.parametrize("name,files", SHAPES, ids=[n for n, _ in SHAPES])
def test_scheduler_reads_what_effective_resolves(tmp_path: Path, name: str,
                                                 files: dict[str, str]) -> None:
    conf_d = _tree(tmp_path, files)
    assert ms.load_recurring_schedules(conf_d) == _oracle(conf_d), name
    p = _run(conf_d)
    assert p.returncode == 0, p.stderr
    if _oracle(conf_d):
        assert "No recurring maintenance schedules found" not in p.stderr, p.stderr


@pytest.mark.parametrize("env", [
    {"DA_GUARD_BINARY": "/nonexistent/da-guard"},
], ids=["missing-binary"])
def test_da_guard_unavailable_is_not_an_empty_schedule(tmp_path: Path,
                                                       env: dict[str, str]) -> None:
    conf_d = _tree(tmp_path, dict(SHAPES)["platform-overlay"])
    p = _run(conf_d, env)
    assert p.returncode == 2, p.stderr
    assert "ERROR: da-guard binary not found" in p.stderr, p.stderr
    assert "No recurring maintenance schedules found" not in p.stderr
    assert "Summary:" not in p.stderr


@pytest.mark.parametrize("rc", [1, 2, 3])
def test_da_guard_failing_is_not_an_empty_schedule(tmp_path: Path, rc: int) -> None:
    """A da-guard that exits non-zero (a stand-in that does) → rc 2, named."""
    fake = tmp_path / "da-guard"
    fake.write_text(f"#!/bin/sh\necho 'boom' >&2\nexit {rc}\n", encoding="utf-8")
    fake.chmod(0o755)
    conf_d = _tree(tmp_path, dict(SHAPES)["tenant-file-CONTROL"])
    p = _run(conf_d, {"DA_GUARD_BINARY": str(fake)})
    assert p.returncode == 2, p.stderr
    assert "ERROR:" in p.stderr and "boom" in p.stderr, p.stderr
    assert "No recurring maintenance schedules found" not in p.stderr


def test_root_file_without_tenants_is_named(tmp_path: Path) -> None:
    """#2751 behavior change: a root tenant file with a top-level
    `_state_maintenance` and no `tenants:` yields no schedule (as the
    exporter), and the file is named in a WARN rather than dropped silently."""
    p = _run(_tree(tmp_path, dict(SHAPES)["root-file-without-tenants"]))
    assert p.returncode == 0, p.stderr
    warns = [ln for ln in p.stderr.splitlines() if "WARN" in ln and "tx.yaml" in ln]
    assert warns, p.stderr
    assert "threshold-exporter reads no tenant from it" in warns[0], warns


def test_non_string_duration_is_skipped_with_a_warn(tmp_path: Path) -> None:
    conf_d = _tree(tmp_path, dict(SHAPES)["duration-not-a-string"])
    p = _run(conf_d)
    assert p.returncode == 0, p.stderr
    assert "Traceback" not in p.stderr, p.stderr
    assert "WARN: tx: recurring entry missing cron/duration, skipping" in p.stderr, p.stderr
    assert ms.load_recurring_schedules(conf_d) == {}


def test_tenant_declared_twice_exits_2(tmp_path: Path) -> None:
    """The exporter refuses a tenant id declared in two files; so does the
    scheduler (main read one of them, rc 0)."""
    conf_d = _tree(tmp_path, {"_defaults.yaml": _DEFAULTS, "a.yaml": _TENANT_MAINT,
                              "b.yaml": _TENANT_MAINT})
    p = _run(conf_d)
    assert p.returncode == 2, p.stderr
    assert "duplicate tenant ID" in p.stderr, p.stderr
    assert "No recurring maintenance schedules found" not in p.stderr


def test_parse_failure_survives_a_failing_served_values(tmp_path: Path) -> None:
    """effective refuses a file (exit 3, parse_failed); the served-values
    call made only for the parse reason fails on its own. The error reported
    is effective's (the file named, rc 2), not served-values' failure."""
    fake = tmp_path / "da-guard"
    fake.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = effective ]; then\n"
        "  printf '%s' '{\"schema\": \"da-guard.effective/v1\", \"parse_failed\": [\"tx.yaml\"],"
        " \"unreadable\": [], \"skipped\": [], \"tenants\": {}}'\n"
        "  exit 3\n"
        "fi\n"
        "echo 'served-values-boom' >&2\nexit 2\n", encoding="utf-8")
    fake.chmod(0o755)
    conf_d = _tree(tmp_path, dict(SHAPES)["tenant-file-CONTROL"])
    p = _run(conf_d, {"DA_GUARD_BINARY": str(fake)})
    assert p.returncode == 2, p.stderr
    assert "ERROR: cannot read" in p.stderr and "tx.yaml" in p.stderr, p.stderr
    assert "served-values-boom" not in p.stderr, p.stderr
    assert "No recurring maintenance schedules found" not in p.stderr
