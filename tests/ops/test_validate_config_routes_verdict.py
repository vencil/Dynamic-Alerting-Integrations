"""#2311: validate-config's `routes` row and `generate-routes --validate`
share ONE verdict function (`_grar_render.evaluate_generated_config`).

Before this, the row ran only `blocking_generation_errors`, so a generation
result `--validate` failed — an ADR-025 inhibit tripwire, an invariant
`assemble_configmap` refuses, an amtool rejection — was a PASS row at exit 0.
The contract pinned here:

* every verdict `--validate` exits 1 on is a FAIL row, and the two agree
  (parity is asserted per case, not assumed);
* amtool absent → WARN (never a fourth status, #2301) with a detail line
  saying the config was NOT validated by Alertmanager; exit code unchanged;
* amtool present but without a verdict → FAIL with ``caller_error`` (exit 2),
  as `--validate` exits 2;
* amtool's own "accepted" line is not an issue: a clean tree is still PASS;
* the shared function returns its lines instead of printing them.

amtool is faked in-process (`_run_binary` + `shutil.which`), so this runs on
every platform and never depends on a real amtool being installed.
"""
from __future__ import annotations

import importlib
import subprocess

import pytest

import _grar_render as render
import generate_alertmanager_routes as gar
import validate_config as vc


@pytest.fixture(autouse=True)
def _current_modules():
    """Patch the module objects the code under test will import NOW.

    validate_config imports generate_alertmanager_routes inside the function,
    so it gets whatever sys.modules holds at call time. Other tests in the
    same worker evict and re-import these modules (e.g. the flat-layout image
    test), so the objects bound at this file's import can be stale: a
    monkeypatch on them would never reach the code under test.
    """
    global render, gar, vc
    render = importlib.import_module("_grar_render")
    gar = importlib.import_module("generate_alertmanager_routes")
    vc = importlib.import_module("validate_config")

_WATCHDOG_RULE = {"source_matchers": ['severity="critical"'],
                  "target_matchers": ['alertname="Watchdog"'], "equal": []}
_SILENCE_RULE = {"source_matchers": ['tenant=~".+"', 'severity="critical"'],
                 "target_matchers": ['alertname="TenantMetricsOverLimit"'],
                 "equal": []}
_ROUTES = [{"receiver": "tenant-t1", "matchers": ['tenant="t1"']}]
_RECEIVERS = [{"name": "tenant-t1",
               "webhook_configs": [{"url": "https://x.example.com/h"}]}]


@pytest.fixture
def tenant_dir(tmp_path):
    d = tmp_path / "conf.d"
    d.mkdir()
    (d / "t1.yaml").write_text(
        "tenants:\n  t1:\n    _routing:\n      receiver:\n"
        "        type: webhook\n        url: https://hooks.example.com/alert\n",
        encoding="utf-8")
    return d


def _fake_amtool(monkeypatch, rc: int | None, output: str = "") -> list:
    """rc None = amtool not on PATH. Returns the list of amtool argvs run."""
    calls: list = []

    def fake(argv, *, timeout, stdin_text=None):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, rc, stdout=output, stderr="")

    monkeypatch.setattr(render, "_run_binary", fake)
    monkeypatch.setattr(render.shutil, "which",
                        lambda name: "/fake/amtool"
                        if (name == "amtool" and rc is not None) else None)
    return calls


def _inject_inhibit(monkeypatch, rule: dict) -> None:
    real = gar.generate_inhibit_rules

    def fake(dedup):
        rules, warns = real(dedup)
        return rules + [rule], warns
    monkeypatch.setattr(gar, "generate_inhibit_rules", fake)


def _refuse_assembly(monkeypatch) -> None:
    def boom(*_a, **_k):
        raise ValueError("fake assembly invariant #2311")
    monkeypatch.setattr(render, "assemble_configmap", boom)


def _validate_rc(tenant_dir, monkeypatch, capsys) -> int:
    monkeypatch.setattr("sys.argv", ["gar", "--config-dir", str(tenant_dir),
                                     "--validate"])
    with pytest.raises(SystemExit) as exc:
        gar.main()
    capsys.readouterr()
    return exc.value.code


# ── the shared function itself ──────────────────────────────────────
class TestEvaluateGeneratedConfig:

    def test_watchdog_tripwire_blocks_before_amtool(self, monkeypatch):
        calls = _fake_amtool(monkeypatch, 0, "SUCCESS")
        v = render.evaluate_generated_config(_ROUTES, _RECEIVERS,
                                             [_WATCHDOG_RULE], [])
        assert any("Watchdog heartbeat" in e for e in v.errors), v.errors
        assert v.amtool is None and calls == []
        assert v.exit_code == 1

    def test_tenant_silence_tripwire(self, monkeypatch):
        _fake_amtool(monkeypatch, 0, "SUCCESS")
        v = render.evaluate_generated_config(_ROUTES, _RECEIVERS,
                                             [_SILENCE_RULE], [])
        assert any("TenantMetricsOverLimit" in e for e in v.errors), v.errors
        assert v.exit_code == 1

    def test_extra_errors_block_and_keep_order(self, monkeypatch):
        calls = _fake_amtool(monkeypatch, 0, "SUCCESS")
        # #2489: a dropped-entry line blocks by its type, not its words.
        skipped = importlib.import_module("_grar_validate").skipped_entry_warning
        v = render.evaluate_generated_config(
            _ROUTES, _RECEIVERS, [_WATCHDOG_RULE],
            [skipped("  WARN: x — skipping")], extra_errors=["ERROR (policy): y"])
        assert v.errors[:2] == ["  WARN: x — skipping", "ERROR (policy): y"]
        assert "Watchdog heartbeat" in v.errors[2]
        assert calls == []

    def test_assembly_value_error_is_returned(self, monkeypatch):
        calls = _fake_amtool(monkeypatch, 0, "SUCCESS")
        _refuse_assembly(monkeypatch)
        v = render.evaluate_generated_config(_ROUTES, _RECEIVERS, [], [])
        assert v.errors == []
        assert v.assembly_error == "fake assembly invariant #2311"
        assert v.amtool is None and calls == [] and v.exit_code == 1

    @pytest.mark.parametrize("rc,output,status,exit_code", [
        (None, "", render.AMTOOL_NOT_FOUND, None),
        (0, "SUCCESS", render.AMTOOL_ACCEPTED, None),
        (1, "Checking x  FAILED: nope", render.AMTOOL_REJECTED, 1),
        (127, "cannot run", render.AMTOOL_UNUSABLE, 2),
    ], ids=["not_found", "accepted", "rejected", "unusable"])
    def test_amtool_status(self, monkeypatch, capsys, rc, output, status,
                           exit_code):
        _fake_amtool(monkeypatch, rc, output)
        v = render.evaluate_generated_config(_ROUTES, _RECEIVERS, [], [])
        assert v.amtool.status == status
        assert v.exit_code == exit_code
        # returned, never printed
        cap = capsys.readouterr()
        assert cap.out == "" and cap.err == "", cap


# ── validate-config's routes row, and parity with --validate ─────────
class TestRoutesRowMatchesValidate:

    @pytest.mark.parametrize("case", ["watchdog", "silence", "assembly",
                                      "amtool_rejected"])
    def test_fail_row_where_validate_exits_1(self, tenant_dir, monkeypatch,
                                             capsys, case):
        if case == "amtool_rejected":
            _fake_amtool(monkeypatch, 1, "Checking x  FAILED: fake-2311")
        else:
            _fake_amtool(monkeypatch, 0, "SUCCESS")
        if case == "watchdog":
            _inject_inhibit(monkeypatch, _WATCHDOG_RULE)
        elif case == "silence":
            _inject_inhibit(monkeypatch, _SILENCE_RULE)
        elif case == "assembly":
            _refuse_assembly(monkeypatch)
        row = vc.check_routes(str(tenant_dir))
        assert row["status"] == vc.FAIL, row
        assert row["caller_error"] is False, row
        assert _validate_rc(tenant_dir, monkeypatch, capsys) == 1

    def test_amtool_rejection_is_named_in_details(self, tenant_dir,
                                                  monkeypatch):
        _fake_amtool(monkeypatch, 1, "Checking x  FAILED: fake-2311")
        row = vc.check_routes(str(tenant_dir))
        assert any("fake-2311" in d for d in row["details"]), row
        assert row["hint"] == vc.AMTOOL_REJECTED_ROUTES_HINT

    def test_amtool_unusable_is_a_caller_error(self, tenant_dir, monkeypatch,
                                               capsys):
        _fake_amtool(monkeypatch, 2, "panic: runtime error")
        row = vc.check_routes(str(tenant_dir))
        assert row["status"] == vc.FAIL and row["caller_error"] is True, row
        assert _validate_rc(tenant_dir, monkeypatch, capsys) == 2

    def test_no_amtool_is_warn_with_a_not_validated_line(self, tenant_dir,
                                                         monkeypatch, capsys):
        _fake_amtool(monkeypatch, None)
        row = vc.check_routes(str(tenant_dir))
        assert row["status"] == vc.WARN, row
        assert row["status"] in (vc.PASS, vc.WARN, vc.FAIL)  # no 4th value
        assert any(d.startswith("Not validated by Alertmanager")
                   for d in row["details"]), row
        assert row["hint"] == vc.AMTOOL_NOT_FOUND_ROUTES_HINT
        assert _validate_rc(tenant_dir, monkeypatch, capsys) == 0

    def test_accepted_tree_stays_pass(self, tenant_dir, monkeypatch):
        """amtool's "accepted" line must not be swept into the row as an
        issue — the stderr capture in check_routes used to turn anything
        printed during the row into a WARN."""
        _fake_amtool(monkeypatch, 0, "SUCCESS")
        row = vc.check_routes(str(tenant_dir))
        assert row["status"] == vc.PASS, row
        assert row["details"][0].startswith("1 routes"), row
        assert any("accepted by Alertmanager's parser" in d
                   for d in row["details"]), row

    def test_nothing_generated_never_asks_amtool(self, tmp_path, monkeypatch):
        calls = _fake_amtool(monkeypatch, None)
        d = tmp_path / "conf.d"
        d.mkdir()
        (d / "t1.yaml").write_text(
            "tenants:\n  t1:\n    _severity_dedup: disable\n",
            encoding="utf-8")
        row = vc.check_routes(str(d))
        assert calls == []
        assert not any("Not validated" in x for x in row["details"]), row
