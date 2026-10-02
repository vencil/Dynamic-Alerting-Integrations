"""#2341 R5：`_routing` 不是 mapping 也不是停用字串、`_routing_defaults` 不是 mapping。

產生器的判定（1A）：非 strict 印 `WARN … skipping`、該租戶不出任何 route
（不改走 defaults），`--validate` 因此 rc 1；`--strict` 另加 ERROR 行，
`--dry-run --strict` 也 rc 1。跨語言的判定由
tests/shared/routing_policy_parity_matrix.json 的 `routing-not-mapping` /
`routing-defaults-not-mapping-below-root` 兩棵樹釘住；這裡釘的是 CLI 行為。
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
_VC = REPO / "scripts" / "tools" / "ops" / "validate_config.py"

_DEFAULTS = ("defaults:\n  cpu_usage_percent: 80\n"
             "_routing_defaults:\n  receiver:\n    type: webhook\n"
             "    url: https://hooks.example.com/default\n")
_OK_TENANT = ("tenants:\n  t-ok:\n    cpu_usage_percent: '85'\n")


def _tree(tmp_path: Path, routing: str, extra: dict | None = None) -> Path:
    d = tmp_path / "conf.d"
    d.mkdir(parents=True)
    (d / "_defaults.yaml").write_text(_DEFAULTS, encoding="utf-8")
    (d / "t-ok.yaml").write_text(_OK_TENANT, encoding="utf-8")
    (d / "t-x.yaml").write_text(
        f"tenants:\n  t-x:\n    cpu_usage_percent: '85'\n    _routing: {routing}\n",
        encoding="utf-8")
    for name, text in (extra or {}).items():
        (d / name).write_text(text, encoding="utf-8")
    return d


def _gar(d: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_GAR), "--config-dir", str(d), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300)


_REFUSED = ["slack", "'slack'", "[x]", "~", "false", "off", "no", "0", "''"]
_LINE = "t-x: _routing must be a mapping or a disabling string"


class TestTenantRoutingNotMapping:

    @pytest.mark.parametrize("value", _REFUSED)
    def test_refused_renders_nothing_and_blocks_validate(self, tmp_path, value):
        d = _tree(tmp_path, value)
        dry = _gar(d, "--dry-run")
        assert dry.returncode == 0, dry.stderr
        assert _LINE in dry.stderr and "skipping" in dry.stderr, dry.stderr
        # 不改走 defaults：該租戶沒有任何 route／receiver。
        assert "tenant-t-x" not in dry.stdout, dry.stdout
        assert "tenant-t-ok" in dry.stdout, dry.stdout
        assert _gar(d, "--validate").returncode == 1
        strict = _gar(d, "--dry-run", "--strict")
        assert strict.returncode == 1, strict.stderr
        assert "ERROR: tenant 't-x': _routing must be a mapping" in strict.stderr
        assert "t-x" not in load_tenant_tree(str(d)).routing_configs

    @pytest.mark.parametrize("value", ["false", "off", "no", "true", "on", "yes"])
    def test_unquoted_boolean_gets_the_quote_hint(self, tmp_path, value):
        out = _gar(_tree(tmp_path, value), "--dry-run").stderr
        assert "got boolean" in out and "write a quoted string ('off') or disable" in out, out

    def test_a_string_gets_no_boolean_hint(self, tmp_path):
        out = _gar(_tree(tmp_path, "slack"), "--dry-run").stderr
        assert "got string 'slack'" in out and "quoted string ('off')" not in out, out

    @pytest.mark.parametrize("value", ["disable", "'off'", "' DISABLED '", "'false'", "Disabled"])
    def test_disabling_string_still_disables(self, tmp_path, value):
        d = _tree(tmp_path, value)
        for mode in (("--validate",), ("--validate", "--strict")):
            p = _gar(d, *mode)
            assert p.returncode == 0, (mode, p.stderr)
            assert "_routing must be a mapping" not in p.stderr
        assert "t-x" not in load_tenant_tree(str(d)).routing_configs

    def test_empty_mapping_is_no_override(self, tmp_path):
        d = _tree(tmp_path, "{}")
        assert _gar(d, "--validate", "--strict").returncode == 0
        rc = load_tenant_tree(str(d)).routing_configs["t-x"]
        assert rc["receiver"]["type"] == "webhook"

    def test_root_platform_overlay_is_judged_too(self, tmp_path):
        """租戶檔沒寫 `_routing`，根目錄平台檔的 `tenants.<id>._routing` 一樣被讀。"""
        d = _tree(tmp_path, "{}", {
            "_platform.yaml": "tenants:\n  t-ok:\n    _routing: slack\n"})
        p = _gar(d, "--validate")
        assert p.returncode == 1 and "t-ok: _routing must be a mapping" in p.stderr, p.stderr

    def test_validate_config_blocks(self, tmp_path):
        d = _tree(tmp_path, "slack")
        vc = subprocess.run([sys.executable, str(_VC), "--config-dir", str(d)],
                            capture_output=True, text=True, encoding="utf-8",
                            timeout=300)
        assert vc.returncode == 1, (vc.stdout, vc.stderr)
        assert "Traceback" not in vc.stdout + vc.stderr


class TestRoutingDefaultsNotMapping:

    @pytest.mark.parametrize("where", ["_defaults.yaml", "team/_defaults.yaml"])
    def test_blocking_at_root_and_below(self, tmp_path, where):
        d = tmp_path / "conf.d"
        (d / "team").mkdir(parents=True)
        (d / "_defaults.yaml").write_text("defaults:\n  cpu_usage_percent: 80\n",
                                          encoding="utf-8")
        (d / "team" / "t-a.yaml").write_text(
            "tenants:\n  t-a:\n    _routing:\n      receiver:\n        type: webhook\n"
            "        url: https://hooks.example.com/a\n", encoding="utf-8")
        p = d / where
        p.write_text(p.read_text(encoding="utf-8") if p.exists() else "", encoding="utf-8")
        with p.open("a", encoding="utf-8") as f:
            f.write("_routing_defaults: off\n")
        line = f"WARN: _routing_defaults in {where} must be a mapping, got boolean False"
        dry = _gar(d, "--dry-run")
        assert dry.returncode == 0 and line in dry.stderr and "skipping" in dry.stderr, dry.stderr
        assert _gar(d, "--validate").returncode == 1
        strict = _gar(d, "--dry-run", "--strict")
        assert strict.returncode == 1, strict.stderr
        assert f"ERROR: _routing_defaults in {where} must be a mapping" in strict.stderr

    def test_null_stays_silent(self, tmp_path):
        d = _tree(tmp_path, "{}", {"_platform.yaml": "_routing_defaults: ~\n"})
        p = _gar(d, "--validate", "--strict")
        assert p.returncode == 0, p.stderr
        assert "_routing_defaults in" not in p.stderr
