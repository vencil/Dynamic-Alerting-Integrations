"""Every engine call outside ``_lib_engine`` is counted, and the count only falls (#3611).

``_lib_engine`` decides whether an external engine actually gave a verdict
(``Ran`` / ``NoVerdict``). It helps only where it is used, and "who still
calls an engine directly" must not be a hand-kept list: hand lists are how
#3611's surveys kept missing sites (promtool in ``_recipe_preview``, helm
``package``, ``drift_detect``'s kubectl). So the sites are computed from the
AST of the gated trees, with ``_lib_engine.ENGINES`` as the vocabulary. A
direct call site, in any module but ``_lib_engine``, is:

* a call of ``which("<engine>")`` — ``shutil.which``, ``sh.which`` after
  ``import shutil as sh``, or a bare ``which`` after ``from shutil import which``;
* a list / tuple literal whose first element is ``"<engine>"`` or a path
  ending in ``/<engine>`` (an argv: ``["promtool", "test", "rules", ...]``);
* a string ``"<engine> ..."`` passed to ``subprocess.*`` (``shell=True``).

A module-level ``NAME = "<literal>"`` is resolved where a name stands in for
the literal (``[HELM, "template"]``).

The sites are counted per (module, engine). :data:`BASELINE` is the count
when the gate landed, frozen by digest; :data:`MIGRATED` records how many of
a key's sites moved to ``_lib_engine``. The live count must equal
``BASELINE − MIGRATED`` exactly: a new site, or one more in a listed module,
is red (route it through ``_lib_engine``); a migration not recorded in
MIGRATED is red too. Some baseline sites are not calls (a directory list
headed ``"helm"``); :data:`NOT_CALLS` names them and why.

Completeness of the vocabulary: every ``which("<literal>")`` and every
literal argv head passed to ``subprocess.*`` must name an engine or an entry
of :data:`NOT_ENGINES`, so a new engine cannot be adopted unseen.

Known blind spots (by construction, not oversights): a binary name reaching
``which`` / argv through a function argument, an attribute or another module
(``which(cmd[0])``); an engine run inside ``docker run <image>`` (the image,
not the argv head, names it); ``.sh`` wrappers. Those are left to the tools'
own tests.
"""
from __future__ import annotations

import ast
import collections
import hashlib
import sys
from pathlib import Path

from _tree import REPO_ROOT, repo_files
from test_single_exit_path import GATED_TREES

sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools"))
import _lib_engine  # noqa: E402

LIB = "scripts/tools/_lib_engine.py"

#: Binaries found by ``which`` or at an argv head that are not verdict
#: engines, each with why.
NOT_ENGINES: dict[str, str] = {
    "bash": "runs repo shell scripts; their own exit status is the contract",
    "docker": "the containerised fallback runner, located by _lib_engine; "
              "dx_run uses it as a transport",
    "gh": "GitHub API transport for dx reports, not a lint verdict",
    "git": "repository plumbing; its exit status is the answer asked for",
    "go": "toolchain lookup (go env GOROOT) to find gofmt",
    "da-guard": "first-party binary with the same 0/1/2 contract (_lib_godispatch)",
    "da-crdecode": "first-party binary with the same 0/1/2 contract (_lib_consumer_read)",
    "npm": "package installer, not a verdict engine (lint_jsx_babel installed "
           "@babel/standalone with it until #3669; no caller left)",
    "openssl": "key generation (federation_keygen), not a lint verdict",
    "pre-commit": "fuse_plumbing_commit only probes `--version` to know git's hook can "
                  "start; the hooks' output is never parsed and any failure is 2 (#3634)",
    "notify-send": "desktop notification (validate_all --notify)",
    "osascript": "desktop notification (validate_all --notify)",
    "powershell": "desktop notification (validate_all --notify)",
}

Key = tuple[str, str]   # (repo-relative module, engine)

#: ⛔ Frozen: the direct sites when the gate landed (#3611, 2026-11-06),
#: (module, engine) → count. Never edited — its digest is pinned in
#: test_the_baseline_is_frozen. A migrated site is recorded in MIGRATED.
BASELINE: dict[Key, int] = {
    ("scripts/ops/bench_synth_tenants.py", "kubectl"): 1,
    ("scripts/ops/check_image_refs_resolve.py", "skopeo"): 2,
    ("scripts/ops/configmap_assemble.py", "kubectl"): 1,
    ("scripts/tools/_lib_kubectl.py", "kubectl"): 2,
    ("scripts/tools/dx/_recipe_preview.py", "promtool"): 1,
    ("scripts/tools/dx/measure_judgement_budget.py", "amtool"): 1,
    ("scripts/tools/dx/measure_judgement_budget.py", "helm"): 2,
    ("scripts/tools/lint/check_chart_package_contents.py", "helm"): 1,
    ("scripts/tools/lint/check_cli_contract.py", "node"): 1,
    ("scripts/tools/lint/check_codename_leak.py", "helm"): 1,
    ("scripts/tools/lint/check_dev_bypass_manifest.py", "helm"): 1,
    ("scripts/tools/lint/check_go_fmt.py", "gofmt"): 1,
    ("scripts/tools/lint/check_image_pin_capability.py", "helm"): 1,
    ("scripts/tools/lint/check_log_egress_policy.py", "helm"): 1,
    ("scripts/tools/lint/lint_jsx_babel.py", "node"): 2,
    ("scripts/tools/lint/validate_mermaid.py", "mmdc"): 2,
    ("scripts/tools/ops/_grar_render.py", "amtool"): 1,
    ("scripts/tools/ops/_grar_render.py", "kubectl"): 3,
    ("scripts/tools/ops/cutover_tenant.py", "kubectl"): 1,
    ("scripts/tools/ops/drift_detect.py", "kubectl"): 1,
    ("scripts/tools/ops/explain_route.py", "amtool"): 1,
    ("scripts/tools/ops/generator_service.py", "amtool"): 2,
    ("scripts/tools/ops/grafana_import.py", "kubectl"): 4,
    ("scripts/tools/ops/init_project.py", "helm"): 1,
    ("scripts/tools/ops/init_project.py", "kubectl"): 1,
    ("scripts/tools/ops/patch_config.py", "kubectl"): 5,
}
_BASELINE_SHA256 = "4da9dcc811bcbef20b216905935b50ef7890886b1ac8512cba835a3aeac4d592"

#: Sites moved to _lib_engine since the baseline: key → how many.
MIGRATED: dict[Key, int] = {
    # #3611 PR 2: node / mmdc
    ("scripts/tools/lint/check_cli_contract.py", "node"): 1,
    ("scripts/tools/lint/lint_jsx_babel.py", "node"): 2,
    ("scripts/tools/lint/validate_mermaid.py", "mmdc"): 2,
    # #3611 PR 4: off the subprocess path altogether — the registry is asked
    # over its v2 API (_lib_registry), so skopeo has no caller left
    ("scripts/ops/check_image_refs_resolve.py", "skopeo"): 2,
    # #3611 PR 5: recipe-preview's promtool — check rules / test rules --junit
    ("scripts/tools/dx/_recipe_preview.py", "promtool"): 1,
    # #3793: off the subprocess path altogether — Alertmanager's gate asks
    # its loader (da-read) only, so `amtool check-config` has no caller left
    ("scripts/tools/ops/_grar_render.py", "amtool"): 1,
    ("scripts/tools/dx/measure_judgement_budget.py", "amtool"): 1,
    # #3839: the generator service's readiness asks for da-read, the binary
    # that gate runs — its two `which("amtool")` are gone
    ("scripts/tools/ops/generator_service.py", "amtool"): 2,
    # #3539: kubectl is started in one place — `_lib_kubectl.kubectl_run`,
    # which locates and runs it here (a proof per verb) — for reads and
    # writes alike. patch_config's sites are the ones left.
    ("scripts/tools/_lib_kubectl.py", "kubectl"): 2,
    ("scripts/ops/bench_synth_tenants.py", "kubectl"): 1,
    ("scripts/ops/configmap_assemble.py", "kubectl"): 1,
    ("scripts/tools/ops/_grar_render.py", "kubectl"): 3,
    ("scripts/tools/ops/cutover_tenant.py", "kubectl"): 1,
    ("scripts/tools/ops/drift_detect.py", "kubectl"): 1,
    ("scripts/tools/ops/grafana_import.py", "kubectl"): 4,
}

#: Baseline sites the scanner counts that are not engine calls. They stay in
#: the count (the scanner cannot tell them apart); naming them here keeps a
#: reader from "migrating" them, and from reading the key as all-calls.
NOT_CALLS: dict[Key, str] = {
    ("scripts/tools/lint/check_codename_leak.py", "helm"):
        "a list of directory names to scan, headed by helm/",
    ("scripts/tools/lint/check_dev_bypass_manifest.py", "helm"):
        "a tuple of directory names to scan, headed by helm/",
    ("scripts/tools/ops/init_project.py", "helm"):
        "a data tuple (prerequisite name, env var, description), not argv",
    ("scripts/tools/ops/init_project.py", "kubectl"):
        "a data tuple (prerequisite name, env var, description), not argv",
}


def _consts(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "literal"`` assignments."""
    out: dict[str, str] = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            out[node.targets[0].id] = node.value.value
    return out


def _str(node: ast.AST, consts: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    return None


def _binary(text: str | None) -> str | None:
    """The binary a literal names: its basename (``/usr/bin/helm`` → helm)."""
    if not text or "/" in text.rstrip("/") and not text.startswith(("/", "./")):
        return text
    return text.rstrip("/").rsplit("/", 1)[-1]


def _is_which(call: ast.Call) -> bool:
    f = call.func
    return ((isinstance(f, ast.Attribute) and f.attr == "which")
            or (isinstance(f, ast.Name) and f.id == "which"))


_SUBPROCESS = {"run", "Popen", "check_output", "check_call", "call"}


def _is_subprocess(call: ast.Call) -> bool:
    f = call.func
    return (isinstance(f, ast.Attribute) and f.attr in _SUBPROCESS
            and isinstance(f.value, ast.Name) and f.value.id == "subprocess")


def scan(tree: ast.Module) -> tuple[collections.Counter, set[str]]:
    """(engine → direct sites, binaries named at a which / subprocess argv
    head that are neither engines nor NOT_ENGINES)."""
    engines = set(_lib_engine.ENGINES.values())
    consts = _consts(tree)
    found: collections.Counter = collections.Counter()
    unknown: set[str] = set()

    def classify(name: str | None) -> None:
        if name and name not in engines and name not in NOT_ENGINES:
            unknown.add(name)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_which(node) and node.args:
            name = _binary(_str(node.args[0], consts))
            if name in engines:
                found[name] += 1
            classify(name)
        elif isinstance(node, (ast.List, ast.Tuple)) and node.elts:
            name = _binary(_str(node.elts[0], consts))
            if name in engines:
                found[name] += 1
        if isinstance(node, ast.Call) and _is_subprocess(node) and node.args:
            head = node.args[0]
            if isinstance(head, (ast.List, ast.Tuple)) and head.elts:
                classify(_binary(_str(head.elts[0], consts)))
            else:
                text = _str(head, consts)
                if text and text.split():
                    name = _binary(text.split()[0])
                    if name in engines:
                        found[name] += 1
                    classify(name)
    return found, unknown


def _gated_modules() -> list[Path]:
    return [p for p in repo_files(".py")
            if p.relative_to(REPO_ROOT).as_posix().startswith(GATED_TREES)]


def _survey() -> tuple[dict[Key, int], dict[str, set[str]]]:
    sites: dict[Key, int] = {}
    unknown: dict[str, set[str]] = {}
    for path in _gated_modules():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel == LIB:
            continue
        found, unk = scan(ast.parse(path.read_text(encoding="utf-8"), filename=rel))
        for engine, n in found.items():
            sites[(rel, engine)] = n
        if unk:
            unknown[rel] = unk
    return sites, unknown


def _expected() -> dict[Key, int]:
    out = {k: n - MIGRATED.get(k, 0) for k, n in BASELINE.items()}
    return {k: n for k, n in out.items() if n}


def test_engine_call_sites_equal_baseline_minus_migrated():
    sites, _ = _survey()
    expected = _expected()
    more = {f"{m} [{e}]": f"{n} > {expected.get((m, e), 0)}"
            for (m, e), n in sites.items() if n > expected.get((m, e), 0)}
    fewer = {f"{m} [{e}]": f"{sites.get((m, e), 0)} < {n}"
             for (m, e), n in expected.items() if sites.get((m, e), 0) < n}
    assert not more, (
        "direct engine calls outside _lib_engine — route them through "
        f"_lib_engine.locate()/run() (#3611): {more}")
    assert not fewer, (
        "engine sites migrated but not recorded — add them to MIGRATED "
        f"(BASELINE is frozen): {fewer}")


def _digest(baseline: dict[Key, int]) -> str:
    lines = sorted(f"{m}\t{e}\t{n}" for (m, e), n in baseline.items())
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def test_the_baseline_is_frozen():
    """BASELINE is the count as it stood when the gate landed; it is never
    edited. A changed digest means a site was added — put it back, and route
    the new call through _lib_engine instead."""
    assert _digest(BASELINE) == _BASELINE_SHA256


def test_migrated_and_not_calls_name_baseline_keys():
    assert set(MIGRATED) <= set(BASELINE), sorted(set(MIGRATED) - set(BASELINE))
    assert all(0 < n <= BASELINE[k] for k, n in MIGRATED.items())
    assert set(NOT_CALLS) <= set(BASELINE), sorted(set(NOT_CALLS) - set(BASELINE))
    assert not set(NOT_CALLS) & set(MIGRATED)


def test_every_named_binary_is_classified():
    _, unknown = _survey()
    assert not unknown, (
        "which() or a subprocess argv head names a binary that is neither in "
        "_lib_engine.ENGINES nor in NOT_ENGINES — if its output decides a "
        "verdict it is an engine (add it to ENGINES); otherwise add it to "
        f"NOT_ENGINES with why: {unknown}")


def test_not_engines_and_engines_are_disjoint():
    assert not set(NOT_ENGINES) & set(_lib_engine.ENGINES.values())


def test_every_engine_has_a_proof_or_a_pending_site():
    """An engine in the vocabulary is there because something calls it: a
    proof registered in _lib_engine, or a not-yet-migrated site. One with
    neither is a dead vocabulary entry."""
    proved = {engine for engine, _sub in _lib_engine.PROOFS}
    pending = {e for (_m, e), n in _expected().items()
               if (_m, e) not in NOT_CALLS}
    dead = set(_lib_engine.ENGINES.values()) - proved - pending
    assert not dead, dead


# ── the scanner itself: each shape it claims, red when the shape is removed ──

def _found(src: str) -> dict[str, int]:
    return dict(scan(ast.parse(src))[0])


def test_scan_sees_which_in_every_import_shape():
    assert _found('import shutil\nshutil.which("pint")') == {"pint": 1}
    assert _found('import shutil as sh\nsh.which("pint")') == {"pint": 1}
    assert _found('from shutil import which\nwhich("pint")') == {"pint": 1}


def test_scan_sees_an_argv_literal_headed_by_an_engine():
    assert _found('cmd = ["promtool", "test", "rules", f]') == {"promtool": 1}
    assert _found('cmd = ("amtool", "check-config")') == {"amtool": 1}
    assert _found('cmd = ["/usr/local/bin/helm", "template"]') == {"helm": 1}
    assert _found('HELM = "helm"\ncmd = [HELM, "template"]') == {"helm": 1}


def test_scan_sees_a_shell_string():
    src = 'import subprocess\nsubprocess.run("helm template x", shell=True)'
    assert _found(src) == {"helm": 1}


def test_scan_counts_each_site():
    assert _found('a = ["helm", "x"]\nb = ["helm", "y"]') == {"helm": 2}


def test_scan_ignores_an_engine_name_not_at_argv_head():
    assert _found('x = ["docker", "run", "img", "helm"]') == {}
    assert _found('print("helm")') == {}
    assert _found('x = ["helm/charts", "y"]') == {}


def test_scan_reports_an_unclassified_binary():
    assert scan(ast.parse('shutil.which("brand-new-linter")'))[1] == {"brand-new-linter"}
    assert scan(ast.parse('import subprocess\nsubprocess.run(["trivy", "fs"])'))[1] == {"trivy"}
    assert scan(ast.parse('shutil.which("gh")'))[1] == set()
    assert scan(ast.parse('gh.run(["api", "x"])'))[1] == set()   # not subprocess
