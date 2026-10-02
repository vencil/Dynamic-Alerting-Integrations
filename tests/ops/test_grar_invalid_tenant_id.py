"""#2341 R8：路由面拒收的租戶 id。

判定函式 `_lib_validation.is_valid_tenant_id`（Go：routingpolicy.IsValidTenantID）
由 tests/shared/routing_policy_parity_matrix.json 的 `tenant_ids` 表釘住；這裡釘
產生器的行為：不合法 id 的租戶什麼都不產出（route、receiver、inhibit rule），
非 strict 印 `WARN … skipping`（`--validate` rc 1），`--strict` 另加 ERROR。
空 id 先前產生 matcher `tenant=""`，Alertmanager 當成「沒有 tenant label」，
平台告警被導到 `tenant-`。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "tools"))
sys.path.insert(0, str(REPO / "scripts" / "tools" / "ops"))
from _grar_parse import load_tenant_tree  # noqa: E402

_GAR = REPO / "scripts" / "tools" / "ops" / "generate_alertmanager_routes.py"

_DEFAULTS = ("defaults:\n  cpu_usage_percent: 80\n"
             "_routing_defaults:\n  receiver:\n    type: webhook\n"
             "    url: https://hooks.example.com/{{tenant}}\n")


def _tree(tmp_path: Path, tid: str) -> Path:
    d = tmp_path / "conf.d"
    d.mkdir(parents=True)
    (d / "_defaults.yaml").write_text(_DEFAULTS, encoding="utf-8")
    (d / "t-ok.yaml").write_text("tenants:\n  t-ok:\n    cpu_usage_percent: '85'\n",
                                 encoding="utf-8")
    (d / "x.yaml").write_text(f"tenants:\n  \"{tid}\":\n    cpu_usage_percent: '85'\n",
                              encoding="utf-8")
    return d


def _gar(d: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_GAR), "--config-dir", str(d), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300)


@pytest.mark.parametrize("tid", ["", " ", "bad tenant", "Bad_Tenant!", "a.b"])
def test_invalid_id_renders_nothing(tmp_path, tid):
    d = _tree(tmp_path, tid)
    dry = _gar(d, "--dry-run")
    assert dry.returncode == 0, dry.stderr
    assert f"tenant id {tid!r} is not a valid tenant id" in dry.stderr, dry.stderr
    assert "skipping" in dry.stderr
    rendered = dry.stdout
    assert f"tenant-{tid}\n" not in rendered and f'tenant="{tid}"' not in rendered, rendered
    assert "tenant-t-ok" in rendered, rendered
    tree = load_tenant_tree(str(d))
    assert tid not in tree.routing_configs and tid not in tree.dedup_configs
    assert _gar(d, "--validate").returncode == 1
    strict = _gar(d, "--dry-run", "--strict")
    assert strict.returncode == 1, strict.stderr
    assert f"ERROR: tenant id {tid!r} is not a valid tenant id" in strict.stderr


@pytest.mark.parametrize("tid", ["UPPER", "Mixed_Case-1", "010"])
def test_ids_an_existing_rule_accepts_still_render(tmp_path, tid):
    """任一份既有規則接受的 id（大寫等）本波不新拒絕。"""
    d = _tree(tmp_path, tid)
    p = _gar(d, "--validate", "--strict")
    assert p.returncode == 0, p.stderr
    assert tid in load_tenant_tree(str(d)).routing_configs
