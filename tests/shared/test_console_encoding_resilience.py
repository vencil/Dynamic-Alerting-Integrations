"""Console-encoding resilience gate (Wave 7, da-tools ROI round 4).

Contract under test: on ANY console encoding — specifically the legacy
Windows codecs our customer base actually uses (zh-TW cp950, zh-CN
cp936, Western cp1252) — every da-tools CLI must DEGRADE unencodable
characters instead of crashing:

  - ``--help`` exits 0 (the "--help always exits 0" contract held on
    UTF-8 consoles but was broken on cp950: help text containing '≥'
    U+2265 died with UnicodeEncodeError INSIDE argparse.parse_args(),
    i.e. before any main()-body mitigation like try_utf8_stdout()
    could run);
  - human output prints, with unencodable chars degraded to
    ``\\uXXXX`` (errors="backslashreplace"; info-preserving, and valid
    JSON escape syntax if it lands inside a --json string).

Mechanism (see scripts/tools/_lib_compat.harden_stdout_errors):
  _lib_compat reconfigures sys.stdout errors (NOT encoding) at module
  import time, and the other three shared root libs (_lib_exitcodes /
  _lib_python / _lib_godispatch) chain-import it. Every da-tools CLI
  entrypoint (ops/ + dx/ + lint/) imports at least one of those four at
  module level, which is the only spot guaranteed to execute before
  parse_args() prints --help.

Test layers:
  1. Behavioral regression: spawn the tools empirically verified to
     crash pre-fix (3 ops + 2 lint), with PYTHONIOENCODING=cp950
     explicitly overriding the session-wide utf-8 forced by
     tests/conftest.py (that fixture cures parent/child DECODE
     mismatches in tests and must stay; this gate simulates the
     customer console the fixture deliberately hides). cp950 is a
     built-in CPython codec, so this runs identically on Linux CI.
  2. Per-carrier behavior: importing each root lib alone hardens
     stdout in a fresh interpreter.
  3. AST adoption gate: every CLI entrypoint (ops/dx/lint, detected by
     argparse + __main__, not by name prefix) imports >=1 carrier at
     module level (AST, not grep — comments/strings can't false-positive).
  4. Chain integrity: carriers still import _lib_compat, and
     _lib_compat still invokes the hook at module scope.
"""
import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS_DIR = Path(__file__).parent.parent.parent / "scripts" / "tools"

# The four root libs that trigger stdout hardening at module import.
CARRIER_LIBS = ("_lib_compat", "_lib_exitcodes", "_lib_python", "_lib_godispatch")

# Ops tools empirically verified (2026-07, full 55-tool cp950 sweep) to
# crash on `--help` before the fix. If one of these later loses its
# non-cp950-encodable help characters, the backslashreplace-artifact
# assertion below fails loudly — swap in another tool from a fresh
# sweep rather than deleting the assertion (it keeps this gate from
# silently becoming vacuous).
CP950_CRASHERS = (
    "ops/threshold_recommend.py",
    "ops/threshold_govern.py",
    "ops/drift_detect.py",
    # lint CLIs whose --help live-crashed on cp950 ('⚠' U+26A0), surfaced by
    # the W7 blind review and fixed by extending carrier adoption to lint/.
    "lint/check_maintenance_symmetry.py",
    "lint/check_vmalert_coverage.py",
)


def _cp950_env() -> dict:
    """Env for children: EXPLICITLY force cp950 stdio.

    Built fresh from os.environ so it overrides (not inherits) the
    session-scoped PYTHONIOENCODING=utf-8 that tests/conftest.py
    injects for every other subprocess in the suite.
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "cp950"
    return env


# ── 1. Behavioral regression: --help on a cp950 console ──────────────

@pytest.mark.parametrize("rel", CP950_CRASHERS)
def test_help_exits_zero_on_cp950_console(rel):
    proc = subprocess.run(
        [sys.executable, str(TOOLS_DIR / rel), "--help"],
        capture_output=True,
        env=_cp950_env(),
        timeout=60,
    )
    stderr = proc.stderr.decode("utf-8", "replace")
    assert proc.returncode == 0, f"{rel} --help crashed under cp950:\n{stderr}"
    assert b"UnicodeEncodeError" not in proc.stderr, stderr
    assert proc.stdout, f"{rel} --help printed nothing under cp950"
    # Degrade-not-crash also means degrade-not-DROP: the unencodable
    # char that used to kill the tool must surface as a
    # backslashreplace artifact, proving help still rendered fully.
    assert b"\\u" in proc.stdout, (
        f"{rel} --help under cp950 shows no backslashreplace artifact; "
        "either the hook regressed to 'replace'/skipped, or this tool's "
        "help no longer contains a non-cp950 char (re-sweep and swap the "
        "tool in CP950_CRASHERS)."
    )


# ── 2. Per-carrier behavior: import alone hardens stdout ─────────────

@pytest.mark.parametrize("mod", CARRIER_LIBS)
def test_carrier_import_hardens_stdout(mod):
    # chr(0x2265) = '≥', not encodable in cp950 → crashed pre-fix.
    code = (
        "import sys; "
        f"sys.path.insert(0, {str(TOOLS_DIR)!r}); "
        f"import {mod}; "
        "print(chr(0x2265))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        env=_cp950_env(),
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert proc.stdout.strip() == b"\\u2265", proc.stdout


def test_unprotected_baseline_still_crashes_on_cp950():
    """Meta-guard: prove the cp950 env in this gate really is hostile.

    If a future Python / platform change makes bare print(chr(0x2265))
    survive cp950 without our hook, the whole gate stops measuring
    anything — surface that loudly instead of green-forever.
    """
    proc = subprocess.run(
        [sys.executable, "-c", "print(chr(0x2265))"],
        capture_output=True,
        env=_cp950_env(),
        timeout=60,
    )
    assert proc.returncode != 0 and b"UnicodeEncodeError" in proc.stderr, (
        "bare cp950 stdout no longer crashes on U+2265; this gate's "
        "premise changed — re-evaluate whether the hook is still needed."
    )


# ── 3. AST adoption gate: every ops tool reaches a carrier ───────────

def _module_level_imports(path: Path) -> set:
    """Module names imported at module level (incl. top-level try/if).

    Function/class bodies are deliberately NOT walked: an import that
    only executes inside main() would not run before parse_args() and
    must not count as adoption.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()

    def visit(stmts):
        for node in stmts:
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    names.add(node.module)
            elif isinstance(node, ast.Try):
                visit(node.body)
                for handler in node.handlers:
                    visit(handler.body)
                visit(node.orelse)
                visit(node.finalbody)
            elif isinstance(node, ast.If):
                visit(node.body)
                visit(node.orelse)

    visit(tree.body)
    return names


def _is_cli_entrypoint(path: Path) -> bool:
    """True iff the file is a CLI tool: it builds an argparse parser AND has
    a ``__main__`` block.

    Detected structurally rather than by a name-prefix heuristic, so that an
    underscore-prefixed daemon CLI (e.g. ops/_federation_revocation_
    reconciler.py) IS covered while a shared ``_lib_*`` helper (argparse-free,
    no ``__main__``) is NOT. Over-inclusion is harmless (a false CLI would
    just be asked to import a carrier); under-inclusion is the real risk, and
    this errs toward inclusion.
    """
    src = path.read_text(encoding="utf-8")
    return "ArgumentParser" in src and "__main__" in src


def test_every_cli_entrypoint_imports_a_hardening_carrier():
    missing = []
    for sub in ("ops", "dx", "lint"):
        for path in sorted((TOOLS_DIR / sub).glob("*.py")):
            if not _is_cli_entrypoint(path):
                continue
            if not (_module_level_imports(path) & set(CARRIER_LIBS)):
                missing.append(f"{sub}/{path.name}")
    assert not missing, (
        "CLI entrypoints (argparse + __main__) without a module-level import "
        f"of any stdout-hardening carrier {CARRIER_LIBS}: {missing}. Their "
        "--help can crash on legacy Windows consoles (cp950). Import one of "
        "the carriers at module level (any of the four root libs works)."
    )


# ── 4. Chain integrity: carriers → _lib_compat → module-level call ───

def test_carrier_libs_chain_to_lib_compat():
    for mod in ("_lib_exitcodes", "_lib_python", "_lib_godispatch"):
        imports = _module_level_imports(TOOLS_DIR / f"{mod}.py")
        assert "_lib_compat" in imports, (
            f"{mod} no longer imports _lib_compat at module level — the "
            "import-time stdout hardening chain is broken for every tool "
            f"that relies on {mod} as its carrier."
        )


def test_lib_compat_invokes_hook_at_module_scope():
    tree = ast.parse((TOOLS_DIR / "_lib_compat.py").read_text(encoding="utf-8"))
    calls = [
        node.value.func.id
        for node in tree.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
    ]
    assert "harden_stdout_errors" in calls, (
        "_lib_compat no longer calls harden_stdout_errors() at module "
        "scope — importing a carrier lib would no longer harden stdout."
    )
