"""ADR-035 D3（#2341 R8 之後）：tenant id 不合法時，產生器整棵樹拒收。

判定函式 `_lib_validation.is_valid_tenant_id`（Go：routingpolicy.IsValidTenantID）
讀 schema 的 `definitions.tenantId`（DNS-1123 label），由
tests/shared/routing_policy_parity_matrix.json 的 `tenant_ids` 表釘住。這裡釘產生器的
行為：conf.d 只要宣告一個不合法 id，**每一種模式**（render、`--dry-run`、
`--output-configmap`、`--apply`、`--validate`、`--strict`）都 rc 1、不寫出也不套用任何
設定。這是第 2 波 1A 規則（印 WARN、跳過、render rc 0）的例外：跳過整個租戶會部署出
少了它的 Alertmanager 設定，它的告警落到 root receiver，而出貨的 `default` 是空
receiver——等於靜默丟棄。

`load_tenant_tree` 本身仍只是不產出該租戶（validate-config 經由同一條 `WARN … skipping`
列出所有不合法 id）。租戶 id 都是 fixture 名稱（CLAUDE.md #9）。
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "tools"))
sys.path.insert(0, str(REPO / "scripts" / "tools" / "ops"))
from _grar_parse import load_tenant_tree  # noqa: E402
import validate_config as vc  # noqa: E402

_GAR = REPO / "scripts" / "tools" / "ops" / "generate_alertmanager_routes.py"

_DEFAULTS = ("defaults:\n  cpu_usage_percent: 80\n"
             "_routing_defaults:\n  receiver:\n    type: webhook\n"
             "    url: https://hooks.example.com/{{tenant}}\n")

_REFUSAL = "invalid tenant id(s) — nothing was generated, written or applied"

# Refused by the #2341 R8 rule already, and refused now.
_ALWAYS_INVALID = ["", " ", "bad tenant", "Bad_Tenant!", "a.b"]
# ADR-035: accepted (rendered, rc 0) by the #2341 R8 rule, refused now.
_NEWLY_INVALID = ["UPPER", "Mixed_Case-1", "Team_A", "_x", "x_", "-x", "a" * 64]


def _tree(tmp_path: Path, tid: str, body: str = "\n    cpu_usage_percent: '85'") -> Path:
    d = tmp_path / "conf.d"
    d.mkdir(parents=True)
    (d / "_defaults.yaml").write_text(_DEFAULTS, encoding="utf-8")
    (d / "t-ok.yaml").write_text("tenants:\n  t-ok:\n    cpu_usage_percent: '85'\n",
                                 encoding="utf-8")
    (d / "x.yaml").write_text(f"tenants:\n  \"{tid}\":{body}\n", encoding="utf-8")
    return d


def _fake_kubectl(tmp_path: Path) -> tuple[Path, Path]:
    """A `kubectl` that records every call: --apply must never reach it."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    calls = tmp_path / "kubectl-calls.log"
    exe = bindir / "kubectl"
    exe.write_text(f"#!/bin/sh\necho \"$@\" >> '{calls}'\nexit 0\n", encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return bindir, calls


def _gar(d: Path, *args: str, path: str | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if path is not None:
        env["PATH"] = path
    return subprocess.run(
        [sys.executable, str(_GAR), "--config-dir", str(d), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300, env=env)


# A tenant body that is not a mapping is never LOADED, but it is declared:
# the id is judged all the same (blind review F2 — `Team_D:` null used to
# exit 0 in every mode with only the "must be a mapping" WARN).
_BODIES = {"mapping": "\n    cpu_usage_percent: '85'", "null": "", "scalar": " x",
           "list": "\n    - a"}


@pytest.mark.parametrize("body", sorted(_BODIES))
@pytest.mark.parametrize("tid", _ALWAYS_INVALID + _NEWLY_INVALID)
def test_every_mode_refuses_and_writes_nothing(tmp_path, tid, body):
    d = _tree(tmp_path, tid, _BODIES[body])
    out = tmp_path / "out.yaml"
    bindir, calls = _fake_kubectl(tmp_path)
    modes = {
        "render": ["-o", str(out)],
        "render-stdout": [],
        "dry-run": ["--dry-run"],
        "output-configmap": ["--output-configmap", "-o", str(out)],
        "output-configmap-dry-run": ["--output-configmap", "--dry-run"],
        "apply": ["--apply", "--yes"],
        "validate": ["--validate"],
        "strict": ["--strict", "-o", str(out)],
        "validate-strict": ["--validate", "--strict"],
    }
    for mode, argv in modes.items():
        p = _gar(d, *argv, path=f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
        assert p.returncode == 1, (mode, p.returncode, p.stderr)
        assert _REFUSAL in p.stderr, (mode, p.stderr)
        assert f"tenant id {tid!r} is not a valid tenant id" in p.stderr, (mode, p.stderr)
        assert "DNS-1123" in p.stderr, (mode, p.stderr)  # cites the rule's description
        assert not out.exists(), f"{mode} wrote {out}"
        # Nothing rendered to stdout either, not even the valid tenant's route.
        assert "tenant-t-ok" not in p.stdout, (mode, p.stdout)
        assert "OK: all configs valid" not in p.stdout, (mode, p.stdout)
    assert not calls.exists(), f"--apply ran kubectl: {calls.read_text(encoding='utf-8')}"


@pytest.mark.parametrize("tid", ["t-ok2", "010", "a" * 63])
def test_a_valid_id_still_renders(tmp_path, tid):
    d = _tree(tmp_path, tid)
    p = _gar(d, "--validate", "--strict")
    assert p.returncode == 0, p.stderr
    dry = _gar(d, "--dry-run")
    assert dry.returncode == 0, dry.stderr
    assert f"tenant-{tid}" in dry.stdout and "tenant-t-ok" in dry.stdout, dry.stdout
    assert tid in load_tenant_tree(str(d)).routing_configs


@pytest.mark.parametrize("body", sorted(_BODIES))
@pytest.mark.parametrize("tid", ["UPPER", "bad tenant"])
def test_the_tree_skips_it_and_validate_config_lists_it(tmp_path, tid, body):
    """The library still renders nothing for the id (the generator's refusal
    is main()'s), and validate-config reports it through the blocking
    `WARN … skipping` line it already FAILs on — from the same record, for a
    body that is not a mapping too."""
    d = _tree(tmp_path, tid, _BODIES[body])
    tree = load_tenant_tree(str(d))
    assert tid not in tree.routing_configs and tid not in tree.dedup_configs
    assert tree.invalid_tenant_ids == [tid]
    assert "t-ok" in tree.routing_configs
    row = vc.check_schema(str(d))
    assert row["status"] == vc.FAIL, row
    assert any(f"tenant id {tid!r} is not a valid tenant id" in m and "skipping" in m
               for m in row.get("details", [])), row


def test_every_tenant_refused_still_blocks(tmp_path):
    """租戶 id 全部不合法、沒有東西可產出時，仍是同一個拒收（不是 `No tenants found` rc 0）。"""
    d = tmp_path / "conf.d"
    d.mkdir()
    (d / "_defaults.yaml").write_text(_DEFAULTS, encoding="utf-8")
    (d / "t.yaml").write_text("tenants:\n  1.5e3:\n    cpu_usage_percent: '85'\n",
                              encoding="utf-8")
    for argv in (["--dry-run"], ["--validate"], ["--dry-run", "--strict"]):
        p = _gar(d, *argv)
        assert p.returncode == 1, (argv, p.stderr)
        assert "tenant id '1.5e3' is not a valid tenant id" in p.stderr, p.stderr
        assert "No tenants found" not in p.stdout, p.stdout
