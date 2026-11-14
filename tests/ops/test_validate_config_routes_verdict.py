"""#2311: the verdict on a generation result is ONE function
(`_grar_render.evaluate_generated_config`), and validate-config's `routes`
row is `generate-routes --validate` (#3788: both are `validate_tree`).

Before #2311 the row ran only `blocking_generation_errors`, so a generation
result `--validate` failed — an ADR-025 inhibit tripwire, an invariant
`assemble_configmap` refuses, an Alertmanager rejection — was a PASS row at
exit 0. The contract pinned here:

* every verdict `--validate` exits 1 on is a FAIL row, and the two agree
  (parity is asserted per case, not assumed);
* Alertmanager's loader (da-read) is the one reader (#3689, #3793): its
  refusal is FAIL at exit 1; no reading at all is FAIL with ``caller_error``
  (exit 2), as `--validate` exits 2; its acceptance is PASS;
* its "accepted" line is not an issue: a clean tree is still PASS;
* the shared function returns its lines instead of printing them.

The loader's outcome is set in-process (`alertmanager_reading` replaced), so
the verdict mapping runs on every platform; the tests that let it accept use
the real da-read.
"""
from __future__ import annotations

import importlib

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


def _loader(monkeypatch, outcome: str = "real") -> list:
    """Alertmanager's loader as the gate asks it: ``real`` (the real da-read),
    ``refuse`` (it refuses the text), ``unusable`` (no reading could be had).
    Returns the list of texts it was asked about."""
    calls: list = []
    real = render.alertmanager_reading

    def reading(text, **kw):
        calls.append(text)
        if outcome == "refuse":
            raise render.InputRefused("da-read refused the input", ["fake-2311"])
        if outcome == "unusable":
            raise render.ConsumerReadError("da-read was not found")
        return real(text, **kw)

    monkeypatch.setattr(render, "alertmanager_reading", reading)
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

    def test_watchdog_tripwire_blocks_before_alertmanager(self, monkeypatch):
        calls = _loader(monkeypatch)
        v = render.evaluate_generated_config(_ROUTES, _RECEIVERS,
                                             [_WATCHDOG_RULE], [])
        assert any("Watchdog heartbeat" in e for e in v.errors), v.errors
        assert v.alertmanager is None and calls == []
        assert v.exit_code == 1

    def test_tenant_silence_tripwire(self, monkeypatch):
        _loader(monkeypatch)
        v = render.evaluate_generated_config(_ROUTES, _RECEIVERS,
                                             [_SILENCE_RULE], [])
        assert any("TenantMetricsOverLimit" in e for e in v.errors), v.errors
        assert v.exit_code == 1

    def test_extra_errors_block_and_keep_order(self, monkeypatch):
        calls = _loader(monkeypatch)
        # #2489: a dropped-entry line blocks by its type, not its words.
        skipped = importlib.import_module("_grar_validate").skipped_entry_warning
        v = render.evaluate_generated_config(
            _ROUTES, _RECEIVERS, [_WATCHDOG_RULE],
            [skipped("  WARN: x — skipping")], extra_errors=["ERROR (policy): y"])
        assert v.errors[:2] == ["  WARN: x — skipping", "ERROR (policy): y"]
        assert "Watchdog heartbeat" in v.errors[2]
        assert calls == []

    def test_assembly_value_error_is_returned(self, monkeypatch):
        calls = _loader(monkeypatch)
        _refuse_assembly(monkeypatch)
        v = render.evaluate_generated_config(_ROUTES, _RECEIVERS, [], [])
        assert v.errors == []
        assert v.assembly_error == "fake assembly invariant #2311"
        assert v.alertmanager is None and calls == [] and v.exit_code == 1

    @pytest.mark.parametrize("outcome,status,exit_code", [
        ("real", render.AM_ACCEPTED, None),
        ("refuse", render.AM_REJECTED, 1),
        ("unusable", render.AM_UNUSABLE, 2),
    ], ids=["accepted", "rejected", "unusable"])
    def test_alertmanager_status(self, monkeypatch, capsys, outcome, status,
                                 exit_code):
        calls = _loader(monkeypatch, outcome)
        v = render.evaluate_generated_config(_ROUTES, _RECEIVERS, [], [])
        assert v.alertmanager.status == status
        assert v.exit_code == exit_code
        assert len(calls) == 1
        assert (v.alertmanager.reading is not None) == (status == render.AM_ACCEPTED)
        # returned, never printed
        cap = capsys.readouterr()
        assert cap.out == "" and cap.err == "", cap

    def test_loader_refusal_is_rejected(self, monkeypatch, capsys):
        """#3689: the real loader refuses a string where http_config wants a
        bool, without printing the value."""
        receivers = [{"name": "tenant-t1", "webhook_configs": [{
            "url": "https://x.example.com/h",
            "http_config": {"follow_redirects": "CANARY3689"}}]}]
        v = render.evaluate_generated_config(_ROUTES, receivers, [], [])
        assert v.alertmanager.status == render.AM_REJECTED, v.alertmanager
        assert v.exit_code == 1
        assert "config loader refuses" in v.alertmanager.message
        assert "CANARY" not in v.alertmanager.message
        assert capsys.readouterr().err == ""

    def test_no_loader_reading_is_unusable(self, monkeypatch):
        _loader(monkeypatch, "unusable")
        v = render.evaluate_generated_config(_ROUTES, _RECEIVERS, [], [])
        assert v.alertmanager.status == render.AM_UNUSABLE
        assert v.exit_code == 2
        assert "the way Alertmanager does" in v.alertmanager.message


# ── validate-config's routes row, and parity with --validate ─────────
class TestRoutesRowMatchesValidate:

    @pytest.mark.parametrize("case", ["watchdog", "silence", "assembly",
                                      "alertmanager_rejected"])
    def test_fail_row_where_validate_exits_1(self, tenant_dir, monkeypatch,
                                             capsys, case):
        _loader(monkeypatch,
                "refuse" if case == "alertmanager_rejected" else "real")
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

    def test_alertmanager_rejection_is_named_in_details(self, tenant_dir,
                                                        monkeypatch):
        _loader(monkeypatch, "refuse")
        row = vc.check_routes(str(tenant_dir))
        assert any("fake-2311" in d for d in row["details"]), row
        assert row["hint"] == vc.AM_REJECTED_ROUTES_HINT

    def test_alertmanager_unusable_is_a_caller_error(self, tenant_dir,
                                                     monkeypatch, capsys):
        _loader(monkeypatch, "unusable")
        row = vc.check_routes(str(tenant_dir))
        assert row["status"] == vc.FAIL and row["caller_error"] is True, row
        assert row["hint"] == vc.AM_UNUSABLE_ROUTES_HINT
        assert _validate_rc(tenant_dir, monkeypatch, capsys) == 2

    def test_accepted_tree_is_pass(self, tenant_dir, monkeypatch, capsys):
        """The loader's "accepted" line is not an issue: the row is PASS
        when `--validate` exits 0 without a warning. #3793: PASS whatever is
        on PATH; there is no "accepted, but not fully checked" WARN."""
        _loader(monkeypatch)
        row = vc.check_routes(str(tenant_dir))
        assert row["status"] == vc.PASS, row
        assert any(d.startswith("Validation: 1 route(s)") for d in row["details"]), row
        assert any("Alertmanager's config loader (da-read) accepted" in d
                   for d in row["details"]), row
        assert _validate_rc(tenant_dir, monkeypatch, capsys) == 0

    def test_nothing_generated_never_asks_alertmanager(self, tmp_path,
                                                       monkeypatch):
        calls = _loader(monkeypatch)
        d = tmp_path / "conf.d"
        d.mkdir()
        (d / "t1.yaml").write_text(
            "tenants:\n  t1:\n    _severity_dedup: disable\n",
            encoding="utf-8")
        row = vc.check_routes(str(d))
        assert calls == []
        # #3788: `--validate` refuses a tree that renders nothing, so the row does.
        assert row["status"] == vc.FAIL and row["caller_error"] is False, row
        assert "No valid routes or inhibit rules generated." in row["details"], row
        assert row["hint"] == vc.NOTHING_GENERATED_ROUTES_HINT, row
