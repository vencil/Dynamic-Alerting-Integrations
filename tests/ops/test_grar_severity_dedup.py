"""#3621：路由產生器的 `_severity_dedup` 只照 exporter 的讀法（`da-guard severity-dedup`）。

產生器過去有自己的詞表與合併（`severity_dedup_mode`、平台 `tenants:` 與租戶檔的
覆寫順序），現在每個租戶的模式、exporter 讀不懂的值、寫成排程的值全取自 Go
子命令；產生器只做三件事：

* 每次載入都比對兩邊的租戶集合（扣掉產生器自己拒收的無效 id、以及它刻意不載入的
  非 mapping 條目）；不相等 → exit 2，訊息指名哪邊多了誰（有被 exporter 丟掉的檔就
  指名那些檔）。
* exporter 讀不懂的值（Go 的 LayerValueProblem，不論寫在租戶檔、平台 `tenants:`
  或 profile）與寫成排程的值 → 整棵樹拒收（所有模式 rc 1），kind 沿用
  `severity_dedup_invalid`；值進訊息只經 `shown_value`。
* 子命令跑不起來／太舊／壞掉 → exit 2，訊息指名 da-guard；樹已被更早的原因拒收時
  不問。

租戶 id 都是 fixture 名稱（CLAUDE.md #9）。
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
import _grar_parse  # noqa: E402
import _lib_tenant_values as tv  # noqa: E402
from _grar_parse import load_tenant_tree  # noqa: E402

_GAR = REPO / "scripts" / "tools" / "ops" / "generate_alertmanager_routes.py"
_REFUSAL = "config problem(s) — nothing was generated, written or applied"
_DEDUP_STOP = "cannot read each tenant's _severity_dedup as the exporter reads it"
_DEFAULTS = "defaults:\n  cpu_usage_percent: 80\n"


def _tenant(t: str, extra: str = "") -> str:
    return f"tenants:\n  {t}:\n    cpu_usage_percent: '85'\n{extra}"


def _write(tmp_path: Path, files: dict[str, str]) -> Path:
    d = tmp_path / "conf.d"
    base = {"_defaults.yaml": _DEFAULTS, "t-a.yaml": _tenant("t-a"), "t-b.yaml": _tenant("t-b")}
    for rel, text in {**base, **files}.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(text, encoding="utf-8")
    return d


def _gar(d: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_GAR), "--config-dir", str(d), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300, env={**os.environ, **(env or {})})


# ── the modes are Go's merge and words ──────────────────────────────────

@pytest.mark.parametrize("files,expected", [
    # The platform's per-tenant default, the tenant file silent: disable
    # (the exporter's flat merge).
    ({"_defaults.yaml": _DEFAULTS + "tenants:\n  t-a:\n    _severity_dedup: disable\n"},
     {"t-a": "disable", "t-b": "enable"}),
    # The tenant file wins over the platform entry.
    ({"_defaults.yaml": _DEFAULTS + "tenants:\n  t-a:\n    _severity_dedup: disable\n",
      "t-a.yaml": _tenant("t-a", "    _severity_dedup: Enabled\n")},
     {"t-a": "enable", "t-b": "enable"}),
    # `~` in the tenant file deletes the platform's value: the default, enable.
    ({"_defaults.yaml": _DEFAULTS + "tenants:\n  t-a:\n    _severity_dedup: disable\n",
      "t-a.yaml": _tenant("t-a", "    _severity_dedup: ~\n")},
     {"t-a": "enable", "t-b": "enable"}),
    # A subtree `_defaults.yaml` does not carry it (#3341): not applied.
    ({"sub/_defaults.yaml": "_severity_dedup: disable\n", "sub/t-c.yaml": _tenant("t-c")},
     {"t-a": "enable", "t-b": "enable", "t-c": "enable"}),
    # A threshold profile's `_severity_dedup` is not applied to its tenants.
    ({"_defaults.yaml": _DEFAULTS + "profiles:\n  p1:\n    _severity_dedup: disable\n",
      "t-a.yaml": _tenant("t-a", "    _profile: p1\n")},
     {"t-a": "enable", "t-b": "enable"}),
    # Words: trimmed, any case; an unquoted false.
    ({"t-a.yaml": _tenant("t-a", '    _severity_dedup: " OFF "\n'),
      "t-b.yaml": _tenant("t-b", "    _severity_dedup: false\n")},
     {"t-a": "disable", "t-b": "disable"}),
])
def test_modes_are_the_exporters(tmp_path, files, expected):
    tree = load_tenant_tree(str(_write(tmp_path, files)))
    assert tree.field_type_errors == [], tree.field_type_errors
    assert tree.dedup_configs == expected


def test_a_disabled_tenant_gets_no_inhibit_rule(tmp_path):
    d = _write(tmp_path, {"_defaults.yaml": _DEFAULTS + "tenants:\n  t-a:\n    _severity_dedup: disable\n"})
    p = _gar(d, "--output-alertmanager", "--dry-run")
    assert p.returncode == 0, p.stderr
    assert "INFO: t-a: severity_dedup disabled, skipping inhibit rule" in p.stdout + p.stderr
    assert "INFO: t-b: severity_dedup disabled" not in p.stdout + p.stderr


# ── what the exporter does not read refuses the whole tree ──────────────

@pytest.mark.parametrize("files,where", [
    ({"t-a.yaml": _tenant("t-a", '    _severity_dedup: "no"\n')}, "t-a.yaml: tenant 't-a'"),
    ({"_defaults.yaml": _DEFAULTS + "tenants:\n  t-a:\n    _severity_dedup: purple\n"},
     "_defaults.yaml: platform tenants: entry 't-a'"),
])
def test_an_unread_value_refuses_the_tree_in_every_mode(tmp_path, files, where):
    d = _write(tmp_path, files)
    errs = load_tenant_tree(str(d)).field_type_errors
    assert [(e.kind, e.field) for e in errs] == [("severity_dedup_invalid", "_severity_dedup")], errs
    assert where in errs[0].message and "unknown severity_dedup value" in errs[0].message, errs
    for argv in (["--validate"], ["--output-alertmanager", "--dry-run"], ["--output-configmap", "--dry-run"]):
        p = _gar(d, *argv)
        assert p.returncode == 1, (argv, p.stderr)
        assert _REFUSAL in p.stderr and where in p.stderr, (argv, p.stderr)


def test_a_profile_value_is_not_this_readers(tmp_path):
    """The exporter does not read `_severity_dedup` in a threshold profile at
    all (the key verdict names it unread there, #2914), so it judges no value
    there: no refusal from this reader, and no tenant's mode changes."""
    d = _write(tmp_path, {"_defaults.yaml": _DEFAULTS + "profiles:\n  p1:\n    _severity_dedup: purple\n",
                          "t-a.yaml": _tenant("t-a", "    _profile: p1\n")})
    tree = load_tenant_tree(str(d))
    assert tree.field_type_errors == [] and tree.dedup_configs == {"t-a": "enable", "t-b": "enable"}


def test_an_unread_value_is_shown_only_through_shown_value(tmp_path):
    """#1560 ⑤-b: a value that is not name-like is never quoted — not by the
    generator and not through Go's own words, which quote it."""
    canary = "https://u:CANARYPW@hooks.example/CANARY-dedup"  # trufflehog:ignore
    d = _write(tmp_path, {"t-a.yaml": _tenant("t-a", f"    _severity_dedup: '{canary}'\n")})
    p = _gar(d, "--validate")
    assert p.returncode == 1, p.stderr
    assert "_severity_dedup a string of" in p.stderr, p.stderr
    assert "CANARY" not in p.stdout + p.stderr, p.stderr


@pytest.mark.parametrize("value", [
    '{default: disable, overrides: [{window: "03:00-04:00", value: enable}]}',
    '{default: disable, expires: "2099-01-01T00:00:00Z"}',
])
def test_a_schedule_refuses_the_tree(tmp_path, value):
    d = _write(tmp_path, {"t-a.yaml": _tenant("t-a", f"    _severity_dedup: {value}\n")})
    errs = load_tenant_tree(str(d)).field_type_errors
    assert [(e.kind, e.tenant) for e in errs] == [("severity_dedup_invalid", "t-a")], errs
    assert "t-a.yaml: tenants.t-a._severity_dedup is written with a time window" in errs[0].message
    p = _gar(d, "--output-alertmanager", "--dry-run")
    assert p.returncode == 1 and _REFUSAL in p.stderr, p.stderr


# ── the tenant sets are compared on every load ──────────────────────────

def _fake(modes: dict[str, str], **kw) -> "tv.SeverityDedupTree":
    return tv.SeverityDedupTree(modes, [], [], kw.get("parse_failed", []), [], [])


@pytest.mark.parametrize("go_modes,sides", [
    ({"t-a": "enable"}, "only the generator: t-b; only the exporter: none"),
    ({"t-a": "enable", "t-b": "enable", "t-z": "enable"}, "only the generator: none; only the exporter: t-z"),
])
def test_a_tenant_set_mismatch_stops_with_exit_2(tmp_path, monkeypatch, capsys, go_modes, sides):
    monkeypatch.setattr(tv, "load_severity_dedup", lambda *_a, **_k: _fake(go_modes))
    with pytest.raises(SystemExit) as exc:
        load_tenant_tree(str(_write(tmp_path, {})))
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert _DEDUP_STOP in err and f"internal error" in err and f"({sides})" in err, err


def test_excused_tenants_are_not_a_mismatch(tmp_path):
    """An id the generator refuses on its own (ADR-035) and a null body it
    declines to load: Go loads both; the comparison does not count them."""
    d = _write(tmp_path, {"t-b.yaml": "tenants:\n  t-b: ~\n  Bad_Id:\n    cpu_usage_percent: '1'\n"})
    p = _gar(d, "--validate")
    assert p.returncode == 1, p.stderr
    assert _DEDUP_STOP not in p.stderr, p.stderr
    assert "tenant id 'Bad_Id' is not a valid tenant id" in p.stderr, p.stderr


def test_a_tree_refused_before_is_not_judged_here(tmp_path):
    """A tenant declared twice: the exporter's load rejects the tree too
    (severity-dedup exit 2); the duplicate is the refusal, not a failing
    severity-dedup, and no mode is made up (None)."""
    d = _write(tmp_path, {"sub/t-a.yaml": _tenant("t-a")})
    tree = load_tenant_tree(str(d))
    assert tree.duplicate_tenants and tree.dedup_configs == {"t-a": None, "t-b": None}
    p = _gar(d, "--validate")
    assert _DEDUP_STOP not in p.stderr and "ERROR (duplicate tenant): tenant 't-a'" in p.stderr, p.stderr


def test_a_tree_refused_before_takes_gos_modes_without_comparing(tmp_path, monkeypatch):
    """A tenant file this reader drops (refused, #1460): Go's mode where Go
    has the tenant, no tenant-set comparison, no exit 2."""
    monkeypatch.setattr(tv, "load_severity_dedup", lambda *_a, **_k: _fake({"t-a": "disable", "t-z": "enable"}))
    result = {"dedup_configs": {"t-a": None, "t-b": None},
              "tenant_file_errors": [("t-c.yaml", "dropped")]}
    _grar_parse._resolve_severity_dedup(str(tmp_path), result)
    assert result["dedup_configs"] == {"t-a": "disable", "t-b": None}
    assert not result.get("field_type_errors")


# ── a da-guard that cannot answer ───────────────────────────────────────

def _wrapper(tmp_path: Path, body: str) -> Path:
    """A da-guard that runs the real one, except for `severity-dedup`."""
    real = os.environ["DA_GUARD_BINARY"]
    exe = tmp_path / "bin" / "da-guard"
    exe.parent.mkdir(exist_ok=True)
    exe.write_text(f'#!/bin/sh\nif [ "$1" = severity-dedup ]; then\n{body}\nfi\nexec "{real}" "$@"\n',
                   encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return exe


@pytest.mark.parametrize("body,said", [
    # A da-guard older than the subcommand: it reads the name as a stray
    # argument, `-h` included (exit 2 both times).
    ('echo "flag provided but not defined" >&2; exit 2', "older than this tool"),
    ("exit 7", "an exit code it never uses"),
    ("echo not-json; exit 0", "without the expected JSON"),
])
def test_a_da_guard_that_cannot_answer_stops_with_exit_2(tmp_path, body, said):
    if os.name == "nt":
        pytest.skip("a #! wrapper")
    exe = _wrapper(tmp_path, body)
    d = _write(tmp_path, {})
    p = _gar(d, "--validate", env={"DA_GUARD_BINARY": str(exe)})
    assert p.returncode == 2, p.stderr
    assert _DEDUP_STOP in p.stderr and said in p.stderr, p.stderr
    assert "Traceback" not in p.stderr


def test_one_severity_dedup_run_per_load(tmp_path, monkeypatch):
    calls = []
    real = tv.load_severity_dedup
    monkeypatch.setattr(tv, "load_severity_dedup", lambda *a, **k: calls.append(a) or real(*a, **k))
    load_tenant_tree(str(_write(tmp_path, {})))
    assert len(calls) == 1
    assert _grar_parse._lib_tenant_values is tv


def test_a_profile_schedule_is_not_this_readers_either(tmp_path):
    """A profile's `_severity_dedup` is not applied at all — not even its
    default — so, as for a value the exporter does not read there, a schedule
    there refuses nothing here (severity-dedup lists no `profiles:` entry);
    the profile's own WARN (#2914) is the one that names it."""
    d = _write(tmp_path, {"_defaults.yaml": _DEFAULTS + "profiles:\n  p1:\n    _severity_dedup:\n"
                          "      default: disable\n      expires: \"2099-01-01T00:00:00Z\"\n",
                          "t-a.yaml": _tenant("t-a", "    _profile: p1\n")})
    tree = load_tenant_tree(str(d))
    assert tree.field_type_errors == [] and tree.dedup_configs == {"t-a": "enable", "t-b": "enable"}


def test_an_unknown_mode_lists_no_inhibit_rule():
    """None — no mode was read, the tree being refused (its refusal is the
    run's message) — is not enable: no rule is listed for it (#3621)."""
    from _grar_routes import generate_inhibit_rules
    rules, _notes = generate_inhibit_rules({"t-a": None, "t-b": "disable", "t-c": "enable"})
    assert [r["source_matchers"][-1] for r in rules] == ['tenant="t-c"'], rules


def test_a_null_body_given_a_mapping_by_a_platform_entry_is_compared(tmp_path):
    """A tenant file's null body is excused from the comparison only when this
    reader did not load the tenant: here a platform `tenants:` entry gives it a
    mapping, both sides load it, and its mode is Go's."""
    d = _write(tmp_path, {"_defaults.yaml": _DEFAULTS + "tenants:\n  t-a:\n    _severity_dedup: disable\n",
                          "t-a.yaml": "tenants:\n  t-a: ~\n"})
    tree = load_tenant_tree(str(d))
    assert tree.dedup_configs.get("t-a") == "disable", tree.dedup_configs

