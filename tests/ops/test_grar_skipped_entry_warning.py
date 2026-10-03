"""#2489: "a config entry was dropped as unusable" is decided by the line's
TYPE (``SkippedEntryWarning``), not by the words in it.

``blocking_generation_errors`` used to test ``"WARN" in w and "skipping" in
w``. A WARN line carries the operator's values, so a plain clamp WARN whose
value was ``skipping`` (``repeat_interval: "skipping"``) failed
validate-config and ``--validate`` at rc 1, while ``"banana"`` passed at
rc 0. Pinned here:

1. the regression: a value spelled ``skipping`` no longer blocks, through
   both CLIs;
2. equivalence: for every producer of a dropped-entry line (one trigger per
   producer), the new predicate blocks exactly the lines the old one did on
   non-adversarial values — and the mark survives from the producer to EVERY
   caller of the predicate (a re-formatted line would silently stop
   blocking, i.e. fail open);
3. the guard: a ``WARN …, skipping`` string literal under scripts/tools/ops/
   that is not built by ``skipped_entry_warning`` is refused, so a new
   producer cannot silently be non-blocking — with a test proving the guard
   catches one.
"""
from __future__ import annotations

import ast
import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS_DIR = REPO_ROOT / "scripts" / "tools" / "ops"
HELPER = "skipped_entry_warning"

_TENANT_HEAD = "tenants:\n  t1:\n"
_RECEIVER = ("    _routing:\n      receiver:\n        type: webhook\n"
             "        url: https://hooks.example.com/alert\n")


def _legacy_blocking(warnings: list[str]) -> list[str]:
    """The predicate before #2489, kept verbatim as the equivalence oracle."""
    import _grar_validate as gv
    return [w for w in warnings
            if ("WARN" in w and "skipping" in w)
            or gv.is_receiver_name_collision(w)
            or gv.is_routing_tree_error(w)]


def _write_tree(root: Path, files: dict[str, str]) -> Path:
    d = root / "conf.d"
    for rel, text in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return d


def _tenant(routing_body: str, tenant: str = "t1") -> dict[str, str]:
    return {f"{tenant}.yaml": f"tenants:\n  {tenant}:\n    _routing:\n"
            + routing_body}


def _with_receiver(extra: str) -> dict[str, str]:
    return {"t1.yaml": _TENANT_HEAD + _RECEIVER + extra}


# (id, conf.d files, allowed_domains or None, text that names the line)
# One trigger per dropped-entry producer under scripts/tools/ops/.
CASES = [
    # _grar_merge.build_receiver_config
    ("merge:receiver-not-object", _tenant('      receiver: "slack"\n'), None,
     "'receiver' must be an object with 'type', skipping"),
    ("merge:receiver-type-missing",
     _tenant("      receiver:\n        url: https://hooks.example.com/a\n"),
     None, "missing required 'receiver.type', skipping"),
    ("merge:receiver-type-unknown",
     _tenant("      receiver:\n        type: carrier-pigeon\n"), None,
     "unknown receiver type 'carrier-pigeon'"),
    ("merge:required-field", _tenant("      receiver:\n        type: webhook\n"),
     None, "receiver type 'webhook'"),
    ("merge:exactly-one",
     _tenant("      receiver:\n        type: pagerduty\n"
             "        service_key: a\n        routing_key: b\n"),
     None, "requires exactly one of"),
    ("merge:optional-field",
     _tenant("      receiver:\n        type: webhook\n"
             "        url: https://hooks.example.com/a\n"
             "        send_resolved: maybe\n"),
     None, "receiver type 'webhook'"),
    # _grar_parse
    ("parse:root-defaults-routes",
     {"_defaults.yaml": "_routing_defaults:\n  routes: []\n",
      **_with_receiver("")},
     None, "'routes' is not supported here"),
    ("parse:enforced-enabled-not-bool",
     {"_defaults.yaml": "_routing_enforced:\n  enabled: 'n'\n",
      **_with_receiver("")},
     None, "'enabled' must be a YAML boolean"),
    ("parse:routing-defaults-not-mapping",
     {"_defaults.yaml": "_routing_defaults: [1]\n", **_with_receiver("")},
     None, "must be a mapping, got list"),
    ("parse:nested-unread-carrier",
     {"team/_extra.yaml": "_routing_defaults:\n  group_wait: 30s\n",
      "team/t1.yaml": _TENANT_HEAD + _RECEIVER},
     None, "is not read: below the conf.d root"),
    ("parse:nested-defaults-routes",
     {"team/_defaults.yaml": "_routing_defaults:\n  routes: []\n",
      "team/t1.yaml": _TENANT_HEAD + _RECEIVER},
     None, "'routes' is not supported here"),
    ("parse:invalid-tenant-id",
     {"t1.yaml": "tenants:\n  Bad_Tenant:\n" + _RECEIVER}, None,
     "is not a valid tenant id"),
    # _grar_routes
    ("routes:group-by-element", _with_receiver("      group_by: [1]\n"), None,
     "group_by[0] is int"),
    ("routes:override-no-matcher",
     _with_receiver("      overrides:\n        - receiver:\n"
                    "            type: webhook\n"
                    "            url: https://hooks.example.com/b\n"),
     None, "must have either 'alertname' or 'metric_group'"),
    ("routes:override-both-matchers",
     _with_receiver("      overrides:\n        - alertname: A\n"
                    "          metric_group: g\n"),
     None, "has both 'alertname' and 'metric_group'"),
    ("routes:override-no-receiver",
     _with_receiver("      overrides:\n        - alertname: A\n"), None,
     "override[0] missing 'receiver'"),
    ("routes:overrides-not-list", _with_receiver("      overrides: x\n"), None,
     "'overrides' must be a list"),
    ("routes:override-not-dict", _with_receiver("      overrides: [x]\n"),
     None, "override[0] must be a dict"),
    ("routes:routes-not-list", _with_receiver("      routes: x\n"), None,
     "'routes' must be a list"),
    ("routes:route-no-receiver",
     _with_receiver("      routes:\n        - match: {team: a}\n"), None,
     "routes[0] missing 'receiver'"),
    ("routes:enforced-no-receiver",
     {"_defaults.yaml": "_routing_enforced:\n  enabled: true\n",
      **_with_receiver("")},
     None, "_routing_enforced: missing 'receiver', skipping enforced route"),
    ("routes:tenant-no-receiver", _tenant("      group_wait: 30s\n"), None,
     "missing required 'receiver', skipping"),
    # _grar_validate
    ("validate:domain-not-allowed", _with_receiver(""), ["*.allowed.test"],
     "not in allowed_domains, skipping"),
    ("validate:route-not-dict", _with_receiver("      routes: [x]\n"), None,
     "routes[0] must be a dict"),
    ("validate:route-unsupported-key",
     _with_receiver("      routes:\n        - match: {team: a}\n"
                    "          continue: true\n"),
     None, "has unsupported key(s)"),
    ("validate:route-empty-match",
     _with_receiver("      routes:\n        - match: {}\n"), None,
     "needs a non-empty 'match' mapping"),
    ("validate:route-bad-label",
     _with_receiver("      routes:\n        - match: {'bad-label': a}\n"),
     None, "is not a valid label name"),
    ("validate:route-value-not-string",
     _with_receiver("      routes:\n        - match: {team: 1}\n"), None,
     "must be a string, got int"),
    ("validate:route-value-empty",
     _with_receiver("      routes:\n        - match: {team: ''}\n"), None,
     "is empty (it would match every alert"),
    ("validate:routing-not-mapping",
     {"t1.yaml": _TENANT_HEAD + "    _routing: slack\n"}, None,
     "no route is rendered for this tenant, skipping"),
]


@pytest.fixture
def mods():
    """Fresh module objects (other tests evict and re-import these)."""
    return (importlib.import_module("_grar_validate"),
            importlib.import_module("generate_alertmanager_routes"),
            importlib.import_module("_grar_render"),
            importlib.import_module("validate_config"))


def _stream(gar, config_dir: Path, allowed_domains) -> list[str]:
    """The warning stream `--validate` hands the predicate, in-process."""
    tree = gar.load_tenant_tree(str(config_dir))
    routing, dedup, schema_warnings, enforced, _md = tree.as_tuple()
    _r, _rc, route_warnings = gar.generate_routes(
        routing, allowed_domains=allowed_domains, enforced_routing=enforced)
    _i, dedup_warnings = gar.generate_inhibit_rules(dedup)
    return schema_warnings + route_warnings + dedup_warnings


# ── 1. the regression ─────────────────────────────────────────────────────

@pytest.mark.parametrize("value", ["banana", "skipping", "WARN skipping"])
def test_value_spelled_skipping_does_not_block(tmp_path, value):
    d = _write_tree(tmp_path, _with_receiver(
        f"      repeat_interval: \"{value}\"\n"))
    # No amtool on PATH → WARN, not FAIL; the children write UTF-8.
    env = {**os.environ, "PATH": str(tmp_path), "PYTHONIOENCODING": "utf-8"}
    vc = subprocess.run(
        [sys.executable, str(OPS_DIR / "validate_config.py"),
         "--config-dir", str(d)],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
        env=env)
    gen = subprocess.run(
        [sys.executable, str(OPS_DIR / "generate_alertmanager_routes.py"),
         "--config-dir", str(d), "--validate"],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
        env=env)
    assert f"invalid repeat_interval '{value}'" in vc.stdout, vc.stdout
    assert vc.returncode == 0, vc.stdout + vc.stderr
    assert gen.returncode == 0, gen.stdout + gen.stderr


def test_clamp_warn_with_skipping_value_is_not_blocking(mods):
    gv = mods[0]
    line = ("  WARN: t1: invalid repeat_interval 'skipping', using platform "
            "default")
    assert _legacy_blocking([line]) == [line]   # what #2489 reported
    assert gv.blocking_generation_errors([line]) == []


# ── 2. equivalence + the mark reaches every caller ────────────────────────

@pytest.mark.parametrize("files,allowed,marker",
                         [c[1:] for c in CASES], ids=[c[0] for c in CASES])
def test_new_predicate_blocks_what_the_old_one_did(tmp_path, mods, files,
                                                    allowed, marker):
    gv, gar = mods[0], mods[1]
    stream = _stream(gar, _write_tree(tmp_path, files), allowed)
    hits = [w for w in stream if marker in w]
    assert hits, f"trigger did not reach its producer: {stream}"
    assert all(isinstance(w, gv.SkippedEntryWarning) for w in hits), hits
    assert gv.blocking_generation_errors(stream) == _legacy_blocking(stream)


@pytest.mark.parametrize("files,allowed,marker",
                         [c[1:] for c in CASES], ids=[c[0] for c in CASES])
def test_mark_survives_to_every_predicate_caller(tmp_path, mods, monkeypatch,
                                                 files, allowed, marker):
    """Spy on the predicate at each call site (`_validate_mode` /
    `evaluate_generated_config`, validate-config's schema and routes rows,
    `--validate` with nothing to render) and require that every line naming
    the trigger arrives still marked — a caller that re-formatted it would
    deliver a plain str here."""
    gv, gar, render, vc = mods
    seen: list[str] = []

    def spy(warnings):
        seen.extend(warnings)
        return gv.blocking_generation_errors(warnings)

    monkeypatch.setattr(gar, "blocking_generation_errors", spy)
    monkeypatch.setattr(render, "blocking_generation_errors", spy)
    monkeypatch.setenv("PATH", str(tmp_path))   # no amtool
    d = _write_tree(tmp_path, files)
    policy = None
    if allowed is not None:
        policy = tmp_path / "policy.yaml"
        policy.write_text(f"allowed_domains: {allowed!r}\n", encoding="utf-8")

    vc.check_schema(str(d))
    vc.check_routes(str(d), None if policy is None else str(policy))
    argv = ["generate_alertmanager_routes.py", "--config-dir", str(d),
            "--validate"] + ([] if policy is None else ["--policy", str(policy)])
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit):
        gar.main()

    hits = [w for w in seen if marker in w]
    assert hits, f"no caller handed the predicate the trigger line: {seen}"
    assert all(isinstance(w, gv.SkippedEntryWarning) for w in hits), hits


def test_domain_check_unparseable_host_still_blocks(mods):
    """``validate_receiver_domains``' "cannot parse host … skipping domain
    check" line. Not in CASES: no conf.d value tried reaches it — the
    receiver's schema format check refuses an unparseable URL first (its own
    dropped-entry line). It blocked before #2489 (the fixed text says
    ``skipping``) and still does: an allowlist that cannot be checked is not
    passed."""
    gv = mods[0]
    lines = gv.validate_receiver_domains(
        {"type": "webhook", "url": "https:///x"}, "t1", ["*.example.com"])
    assert len(lines) == 1 and "skipping domain check" in lines[0], lines
    assert isinstance(lines[0], gv.SkippedEntryWarning)
    assert gv.blocking_generation_errors(lines) == _legacy_blocking(lines) == lines


def test_non_dropped_lines_stay_unmarked(mods):
    """The INFO dedup line and a NOTICE carry 'skipping' as wording only."""
    gv, gar = mods[0], mods[1]
    _rules, warns = gar.generate_inhibit_rules({"t1": "disable"})
    assert any("skipping inhibit rule" in w for w in warns), warns
    assert gv.blocking_generation_errors(warns) == []


def test_mark_keeps_text_and_is_lost_on_reformat(mods):
    gv = mods[0]
    line = gv.skipped_entry_warning("  WARN: t1: x, skipping")
    assert line == "  WARN: t1: x, skipping" and isinstance(line, str)
    # The documented failure mode: any re-formatting drops the mark.
    assert gv.blocking_generation_errors([line.strip()]) == []
    assert gv.blocking_generation_errors([f"{line}"]) == []


# ── 3. the guard ──────────────────────────────────────────────────────────

# Literals that say `WARN …, skipping` but never enter a generation warning
# stream (they are printed by tools that do not call the predicate). Each
# entry must still match a literal, so a stale exemption fails too.
NOT_A_GENERATION_STREAM = {
    ("maintenance_scheduler.py", "WARN: {}: recurring entry missing "
     "cron/duration, skipping"): "printed to stderr by the scheduler",
    ("scaffold_tenant.py", "WARN: unknown type '{}', skipping routing"):
        "interactive prompt output of the scaffolder",
}


def _static_text(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "{}"
                       for v in node.values)
    return None


def unmarked_skip_literals(source: str) -> list[tuple[int, str]]:
    """``(line, text)`` of each ``WARN …, skipping`` string literal in
    *source* that is not (inside) an argument of ``skipped_entry_warning``.

    Docstrings and other bare-expression strings are not producers. The
    whole literal is judged, so an implicitly concatenated literal split
    across lines is one string, as it is at runtime.
    """
    tree = ast.parse(source)
    parent: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[id(child)] = node
    out = []
    for node in ast.walk(tree):
        text = _static_text(node)
        if text is None or isinstance(parent.get(id(node)), ast.JoinedStr):
            continue
        if "WARN" not in text or ", skipping" not in text:
            continue
        if isinstance(parent.get(id(node)), ast.Expr):
            continue
        cur, marked = node, False
        while id(cur) in parent:
            cur = parent[id(cur)]
            func = getattr(cur, "func", None)
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if isinstance(cur, ast.Call) and name == HELPER:
                marked = True
                break
        if not marked:
            out.append((node.lineno, text.strip()))
    return out


def _ops_sources() -> list[Path]:
    files = sorted(p for p in OPS_DIR.rglob("*.py")
                   if "__pycache__" not in p.parts)
    assert len(files) > 20, f"scan found too few files under {OPS_DIR}"
    return files


def test_every_dropped_entry_literal_is_built_by_the_helper():
    used = set()
    bad = []
    for path in _ops_sources():
        for lineno, text in unmarked_skip_literals(
                path.read_text(encoding="utf-8")):
            key = (path.name, text)
            if key in NOT_A_GENERATION_STREAM:
                used.add(key)
                continue
            bad.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {text}")
    assert not bad, (
        "WARN …, skipping literal not built by skipped_entry_warning() — "
        "blocking_generation_errors would treat it as non-blocking (#2489). "
        "Wrap it, or (only if it never reaches a generation warning stream) "
        "list it in NOT_A_GENERATION_STREAM:\n" + "\n".join(bad))
    stale = set(NOT_A_GENERATION_STREAM) - used
    assert not stale, f"exemptions that no longer match a literal: {stale}"


def test_guard_scan_is_not_vacuous():
    """The scan sees the real producers (counted marked + unmarked)."""
    marked = 0
    for path in _ops_sources():
        src = path.read_text(encoding="utf-8")
        marked += src.count(f"{HELPER}(")
    assert marked >= 25, marked


@pytest.mark.parametrize("source,expected", [
    ('w.append(f"  WARN: {t}: x, skipping")\n', 1),
    ('w.append(f"  WARN: {t}: a "\n         "b, skipping")\n', 1),
    ('x = None, ["  WARN: x, skipping"]\n', 1),
    ('w.append(skipped_entry_warning(t) + "  WARN: x, skipping")\n', 1),
    ('w.append(skipped_entry_warning(f"  WARN: {t}: x, skipping"))\n', 0),
    ('w.append(_gm.skipped_entry_warning("  WARN: x, skipping"))\n', 0),
    ('"""Docstring: a WARN …, skipping line."""\n', 0),
    ('w.append(f"  INFO: {t}: x, skipping inhibit rule")\n', 0),
    ('w.append(f"  WARN: {t}: x — skipping forbidden rule")\n', 0),
])
def test_guard_catches_an_unmarked_producer(source, expected):
    assert len(unmarked_skip_literals(source)) == expected


# ── 4. explain-route's "skipped by the generator" list ────────────────────

def test_explain_route_lists_only_dropped_entries(tmp_path):
    """#2489, same defect in explain-route: an override whose timing value is
    `skipping` (a clamp WARN, the override IS rendered) was listed as "Not in
    effect"; an override really dropped (no receiver) still is."""
    explain_route = importlib.import_module("explain_route")
    gar = importlib.import_module("generate_alertmanager_routes")
    d = _write_tree(tmp_path, _with_receiver(
        "      overrides:\n"
        "        - alertname: A\n"
        "          repeat_interval: skipping\n"
        "          receiver:\n"
        "            type: webhook\n"
        "            url: https://hooks.example.com/b\n"
        "        - alertname: B\n"))
    exp = explain_route.explain_tenant_routing(
        gar._parse_config_files(str(d)), "t1")
    assert [s["source"] for s in exp["sub_routes"]] == ["overrides[0]"]
    assert len(exp["skipped_sub_routes"]) == 1, exp["skipped_sub_routes"]
    assert "override[1] missing 'receiver'" in exp["skipped_sub_routes"][0]
    text = explain_route.format_explanation(exp)
    not_in_effect = text.split("Not in effect (skipped by the generator):")[1]
    assert "override[1] missing 'receiver'" in not_in_effect
    assert "repeat_interval 'skipping'" not in not_in_effect
