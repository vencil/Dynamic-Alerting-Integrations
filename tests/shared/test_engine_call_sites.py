"""Every engine call outside ``_lib_engine`` is listed, and the list only shrinks (#3611).

``_lib_engine`` decides whether an external engine actually gave a verdict
(``Ran`` / ``NoVerdict``). It helps only where it is used, and "who still
calls an engine directly" must not be a hand-kept list: hand lists are how
#3611's surveys kept missing sites (promtool in ``_recipe_preview``, helm
``package``, ``drift_detect``'s kubectl). So the list is computed from the
AST of the gated trees, with ``_lib_engine.ENGINES`` as the vocabulary:

* a call ``shutil.which("<engine>")``, or
* a list / tuple literal whose first element is the string ``"<engine>"``
  (an argv: ``["promtool", "test", "rules", ...]``)

in any module but ``_lib_engine`` is a direct call site. The computed set
must equal :data:`PENDING` exactly: a new site turns this red (route it
through ``_lib_engine``), and so does a migrated one still listed (drop it
from ``PENDING``) — the set can only shrink.

Completeness of the vocabulary is enforced for the ``which`` shape: every
``shutil.which("<literal>")`` in the gated trees must name an engine or an
entry of :data:`NOT_ENGINES`, so a new engine cannot be adopted unseen.

Known blind spots (by construction, not oversights): a binary name reaching
``which`` / argv through a variable or another module (``which(cmd[0])``);
``.sh`` wrappers; an argv built by ``+=`` from a non-literal head. Those are
left to the tools' own tests.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

from _tree import REPO_ROOT, repo_files
from test_single_exit_path import GATED_TREES

sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools"))
import _lib_engine  # noqa: E402

LIB = "scripts/tools/_lib_engine.py"

#: ``shutil.which`` literals that are not verdict engines, each with why.
NOT_ENGINES: dict[str, str] = {
    "bash": "runs repo shell scripts; their own exit status is the contract",
    "docker": "the containerised fallback runner, located by _lib_engine; "
              "dx_run / image-ref resolution use it as a transport",
    "gh": "GitHub API transport for dx reports, not a lint verdict",
    "go": "toolchain lookup (go env GOROOT) to find gofmt",
    "da-guard": "first-party binary with the same 0/1/2 contract (_lib_godispatch)",
}

#: ⛔ Shrink-only: the direct engine call sites found when the gate landed
#: (repo-relative path → engines). A site migrated to _lib_engine is removed
#: here in the same change; a new direct site is not added — it goes
#: through _lib_engine instead.
PENDING: dict[str, frozenset[str]] = {
    "scripts/ops/bench_synth_tenants.py": frozenset({"kubectl"}),
    "scripts/ops/check_image_refs_resolve.py": frozenset({"skopeo"}),
    "scripts/ops/configmap_assemble.py": frozenset({"kubectl"}),
    "scripts/tools/_lib_kubectl.py": frozenset({"kubectl"}),
    "scripts/tools/dx/_recipe_preview.py": frozenset({"promtool"}),
    "scripts/tools/dx/measure_judgement_budget.py": frozenset({"amtool", "helm"}),
    "scripts/tools/lint/check_chart_package_contents.py": frozenset({"helm"}),
    "scripts/tools/lint/check_cli_contract.py": frozenset({"node"}),
    "scripts/tools/lint/check_codename_leak.py": frozenset({"helm"}),
    "scripts/tools/lint/check_dev_bypass_manifest.py": frozenset({"helm"}),
    "scripts/tools/lint/check_go_fmt.py": frozenset({"gofmt"}),
    "scripts/tools/lint/check_image_pin_capability.py": frozenset({"helm"}),
    "scripts/tools/lint/check_log_egress_policy.py": frozenset({"helm"}),
    "scripts/tools/lint/lint_jsx_babel.py": frozenset({"node"}),
    "scripts/tools/lint/validate_mermaid.py": frozenset({"mmdc"}),
    "scripts/tools/ops/_grar_render.py": frozenset({"amtool", "kubectl"}),
    "scripts/tools/ops/cutover_tenant.py": frozenset({"kubectl"}),
    "scripts/tools/ops/drift_detect.py": frozenset({"kubectl"}),
    "scripts/tools/ops/explain_route.py": frozenset({"amtool"}),
    "scripts/tools/ops/generator_service.py": frozenset({"amtool"}),
    "scripts/tools/ops/grafana_import.py": frozenset({"kubectl"}),
    "scripts/tools/ops/init_project.py": frozenset({"helm", "kubectl"}),
    "scripts/tools/ops/patch_config.py": frozenset({"kubectl"}),
}


def _str(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _is_which(call: ast.Call) -> bool:
    f = call.func
    return (isinstance(f, ast.Attribute) and f.attr == "which"
            and isinstance(f.value, ast.Name) and f.value.id == "shutil")


def scan(tree: ast.AST) -> tuple[set[str], set[str]]:
    """(engines called directly, which-literals that are not engines)."""
    engines = set(_lib_engine.ENGINES.values())
    found: set[str] = set()
    unknown: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_which(node) and node.args:
            name = _str(node.args[0])
            if name is None:
                continue
            if name in engines:
                found.add(name)
            elif name not in NOT_ENGINES:
                unknown.add(name)
        elif isinstance(node, (ast.List, ast.Tuple)) and node.elts:
            name = _str(node.elts[0])
            if name in engines:
                found.add(name)
    return found, unknown


def _gated_modules() -> list[Path]:
    return [p for p in repo_files(".py")
            if p.relative_to(REPO_ROOT).as_posix().startswith(GATED_TREES)]


def _survey() -> tuple[dict[str, frozenset[str]], dict[str, set[str]]]:
    sites: dict[str, frozenset[str]] = {}
    unknown: dict[str, set[str]] = {}
    for path in _gated_modules():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel == LIB:
            continue
        found, unk = scan(ast.parse(path.read_text(encoding="utf-8"), filename=rel))
        if found:
            sites[rel] = frozenset(found)
        if unk:
            unknown[rel] = unk
    return sites, unknown


def test_engine_call_sites_equal_pending():
    sites, _ = _survey()
    new = {k: sorted(v - PENDING.get(k, frozenset())) for k, v in sites.items()
           if v - PENDING.get(k, frozenset())}
    stale = {k: sorted(v - sites.get(k, frozenset())) for k, v in PENDING.items()
             if v - sites.get(k, frozenset())}
    assert not new, (
        "direct engine calls outside _lib_engine — route them through "
        f"_lib_engine.locate()/run() (#3611): {new}")
    assert not stale, (
        "PENDING lists engine calls that are gone — remove them from PENDING "
        f"(it only shrinks): {stale}")


def test_every_which_literal_is_classified():
    _, unknown = _survey()
    assert not unknown, (
        "shutil.which() of a binary that is neither in _lib_engine.ENGINES "
        "nor in NOT_ENGINES — if its output decides a verdict it is an engine "
        f"(add it to ENGINES); otherwise add it to NOT_ENGINES with why: {unknown}")


def test_not_engines_and_engines_are_disjoint():
    assert not set(NOT_ENGINES) & set(_lib_engine.ENGINES.values())


def test_every_engine_has_a_proof_or_a_pending_site():
    """An engine in the vocabulary is there because something calls it: a
    proof registered in _lib_engine, or a not-yet-migrated site. One with
    neither is a dead vocabulary entry."""
    proved = {engine for engine, _sub in _lib_engine.PROOFS}
    pending = set().union(*PENDING.values()) if PENDING else set()
    dead = set(_lib_engine.ENGINES.values()) - proved - pending
    assert not dead, dead


# ── the scanner itself: each shape it claims, red when the shape is removed ──

def _found(src: str) -> set[str]:
    return scan(ast.parse(src))[0]


def test_scan_sees_which_of_an_engine():
    assert _found('import shutil\nshutil.which("pint")') == {"pint"}


def test_scan_sees_an_argv_literal_headed_by_an_engine():
    assert _found('cmd = ["promtool", "test", "rules", f]') == {"promtool"}
    assert _found('cmd = ("amtool", "check-config")') == {"amtool"}


def test_scan_ignores_an_engine_name_not_at_argv_head():
    assert _found('x = ["docker", "run", "img", "helm"]') == set()
    assert _found('print("helm")') == set()


def test_scan_reports_an_unclassified_which_literal():
    assert scan(ast.parse('shutil.which("brand-new-linter")'))[1] == {"brand-new-linter"}
    assert scan(ast.parse('shutil.which("gh")'))[1] == set()
