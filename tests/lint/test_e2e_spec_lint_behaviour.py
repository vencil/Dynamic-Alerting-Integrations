#!/usr/bin/env python3
"""A-13, end to end: a parked spec must make the real chain exit non-zero (#1446).

WHY THIS FILE EXISTS
--------------------
``tests/lint/test_e2e_spec_lint.py`` used to guard the links BELOW the script —
``package.json``'s ``lint``, eslint's ``ignores`` / ``files``, the rule's
options — by reading each setting and asserting a property of it. Five rounds
of blind review walked through that one shape five times, each time with a
new spelling (``eslint . || exit 0``, ``eslint one.spec.ts``, an ignore on the
bracket's own line, a second config object, ``files`` narrowed, the options
object emptied), and every one of them was proven the same way: inject a
``test.fixme()``, run the real script, watch it exit 0. The owner stopped the
enumeration (#1446, 2026-09-28) and asked for that proof to BE the test.

So this file does not read the configuration. It runs it:

* a throwaway repo-shaped tree receives a byte-for-byte copy of the real
  ``tests/e2e`` (minus the specs and the artefact directories) and of
  ``scripts/tools/lint/e2e_spec_lint.sh`` at the same relative path — the
  script derives the repo root from its own location;
* ``node_modules`` is a symlink to the real install, so the eslint, plugin and
  parser are exactly the ones the config resolves in the repo;
* a spec with a RANDOM name and a parked test is injected, the real script
  runs, and the exit status plus a mention of that file are asserted.

Any link in the chain that stops the parked test from reporting — the script,
npm's ``lint``, an ignore, a files narrowing, the rule's options, a second
config file that takes precedence — turns the injected case green, without
this file having to know which link it was. The random name is what keeps a
targeted exemption (an ignore for one filename) from satisfying it.

The cases are PAIRED. A tree holding one legitimate spec must exit ZERO;
without that, ``exit 1`` as the script's whole body, or a config eslint cannot
load, would satisfy every "must fail" case here. The output-mentions-the-file
assertion is the same idea from the other side: a non-zero exit for some
unrelated reason (a missing module, a syntax error in the config) does not
count as catching the spec.

⛔ WHERE IT RUNS, AND THE GAP THAT LEAVES
----------------------------------------
It needs ``tests/e2e/node_modules``, which is gitignored. Absent, the cases
SKIP — and a skip here is not a pass, so ``VIBE_REQUIRE_E2E_ESLINT=1`` turns
the skip into a failure. The execution point that sets it is the ``E2E Spec
Lint (A-13)`` job in ``.github/workflows/playwright.yml``, right after its
``npm ci``; ``test_e2e_spec_lint.py`` (collected by Python Tests on every PR)
asserts that step exists, carries the variable, has no ``if:`` /
``continue-on-error`` and that this file is in the workflow's ``paths:``.

Known gap, deliberately not closed here: ``playwright.yml`` is path-filtered,
so a pull request that only edits ``.pre-commit-config.yaml`` or the Makefile
does not start it. Those two entry points are not something this file can see
anyway; ``test_all_three_entry_points_run_the_one_script`` guards them.
"""
from __future__ import annotations

import os
import secrets
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
# Literal paths: verify_diff.py maps sources to tests by scanning for them.
_SCRIPT_REL = "scripts/tools/lint/e2e_spec_lint.sh"
_SCRIPT = _REPO_ROOT / "scripts/tools/lint/e2e_spec_lint.sh"
_E2E = _REPO_ROOT / "tests/e2e"
_REAL_NODE_MODULES = _E2E / "node_modules"

REQUIRE_ENV = "VIBE_REQUIRE_E2E_ESLINT"

_BASH = shutil.which("bash")
_NPM = shutil.which("npm")

# Things that are not part of what `npm run lint` reads and would only make the
# copy slow or wrong: the install itself (symlinked instead), run artefacts, and
# the visual baselines (LFS payload, eslint never opens them).
_NOT_COPIED = ("node_modules", "playwright-report", "test-results",
               "blob-report", "*-snapshots")


def _require() -> bool:
    return os.environ.get(REQUIRE_ENV) == "1"


def _missing_prerequisite() -> str | None:
    if _BASH is None:
        return "bash is not on PATH"
    if _NPM is None:
        return "npm is not on PATH"
    if not (_REAL_NODE_MODULES / ".bin" / "eslint").exists():
        return ("tests/e2e/node_modules/.bin/eslint is missing — run "
                "`cd tests/e2e && npm ci`")
    return None


@pytest.fixture(scope="module", autouse=True)
def _prerequisites() -> None:
    missing = _missing_prerequisite()
    if missing is None:
        return
    msg = (f"{missing}; the A-13 chain was NOT exercised — a skip here is not "
           "a pass")
    if _require():
        pytest.fail(f"{msg} ({REQUIRE_ENV}=1)")
    pytest.skip(msg)


def _is_spec(name: str) -> bool:
    return ".spec." in name


def _tree(tmp_path: Path) -> Path:
    """A repo-shaped tree: the real script, the real tests/e2e minus its specs.

    ⛔ Copied, not rewritten. Everything under tests/e2e that is not a spec or
    an artefact comes along — package.json, eslint.config.*, tsconfig, any
    config somebody adds later — so a change to how `npm run lint` behaves
    reaches this tree without anybody having to teach this file about it.
    """
    root = tmp_path / "repo"
    (root / "scripts/tools/lint").mkdir(parents=True)
    shutil.copy2(_SCRIPT, root / _SCRIPT_REL)

    ignore_artefacts = shutil.ignore_patterns(*_NOT_COPIED)

    def _ignore(directory: str, names: list[str]) -> set[str]:
        return set(ignore_artefacts(directory, names)) | {
            n for n in names
            if _is_spec(n) and (Path(directory) / n).is_file()}

    e2e = root / "tests/e2e"
    shutil.copytree(_E2E, e2e, ignore=_ignore, symlinks=True)
    (e2e / "node_modules").symlink_to(_REAL_NODE_MODULES.resolve(),
                                      target_is_directory=True)
    return root


_LEGIT_SPEC = """\
import { test, expect } from '@playwright/test';

test('a spec with nothing parked', async () => {
  expect(1 + 1).toBe(2);
});
"""

# Not exhaustive on purpose: the rule's options are what decide which
# spellings report, and this file is not a second copy of that table. These are
# the spelling A-13 was written for and the two `.skip` shapes most often typed.
_PARKED = {
    "test.fixme": "test.fixme('parked', async () => {});\n",
    "test.skip": "test.skip('parked', async () => {});\n",
    "test.describe.skip": (
        "test.describe.skip('parked', () => {\n"
        "  test('inner', async () => {});\n"
        "});\n"),
}


def _random_spec_name() -> str:
    # Random so that no ignore / files entry can name it in advance.
    return f"a13-probe-{secrets.token_hex(6)}.spec.ts"


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_BASH, str(root / _SCRIPT_REL)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )


def _report(result: subprocess.CompletedProcess[str]) -> str:
    return (f"rc={result.returncode}\nstdout={result.stdout[-2000:]!r}\n"
            f"stderr={result.stderr[-2000:]!r}")


def test_a_tree_with_one_legitimate_spec_exits_zero(tmp_path: Path) -> None:
    """⛔ The negative control. Without it every case below is satisfiable by
    a chain that simply always fails — `exit 1` in the script, a config that
    no longer loads, a plugin that cannot be resolved."""
    root = _tree(tmp_path)
    (root / "tests/e2e/ok.spec.ts").write_text(
        _LEGIT_SPEC, encoding="utf-8", newline="\n")

    result = _run(root)
    assert result.returncode == 0, (
        "the real A-13 chain rejected a tree whose only spec parks nothing. "
        "Either the chain no longer runs cleanly on a correct tree, or it "
        "fails for everyone — in which case the cases that expect a failure "
        "prove nothing.\n" + _report(result))


@pytest.mark.parametrize("spelling", sorted(_PARKED))
def test_a_parked_spec_with_an_unforeseeable_name_fails_the_chain(
    tmp_path: Path, spelling: str,
) -> None:
    """Inject a parked test under a random name; the real script must fail on it.

    ⛔ Both assertions are needed. A non-zero exit alone is satisfied by a
    chain that broke for an unrelated reason; the file name in the output is
    what shows eslint reached THIS spec and reported it.
    """
    root = _tree(tmp_path)
    e2e = root / "tests/e2e"
    (e2e / "ok.spec.ts").write_text(_LEGIT_SPEC, encoding="utf-8", newline="\n")
    name = _random_spec_name()
    (e2e / name).write_text(
        "import { test } from '@playwright/test';\n\n" + _PARKED[spelling],
        encoding="utf-8", newline="\n")

    result = _run(root)
    assert result.returncode != 0, (
        f"`{spelling}(…)` in {name} went through the real A-13 chain with exit "
        "0. Some link between the script and the rule has stopped reporting "
        "it — the script's exit, package.json's `lint`, an eslint ignore or "
        "files narrowing, the rule's options, or a config file that now takes "
        "precedence. This test does not say which on purpose; run the script "
        "against a spec like this one and read what eslint saw.\n"
        + _report(result))
    assert name in result.stdout + result.stderr, (
        f"the chain failed, but not on {name}: nothing in its output names the "
        "injected spec, so the failure came from somewhere else and says "
        "nothing about whether a parked test is caught.\n" + _report(result))


def test_every_real_spec_name_is_still_linted(tmp_path: Path) -> None:
    """Each real spec, with a `test.fixme()` appended, must be reported by name.

    The random-name case above cannot see an exemption written for ONE
    existing file (an ignore entry naming `portal-home.spec.ts`). This one
    copies every real spec under its real path, appends a parked test to each,
    and requires the output to name all of them — one eslint run for the lot.
    """
    root = _tree(tmp_path)
    specs = sorted(p for p in _E2E.rglob("*.spec.*")
                   if p.is_file() and "node_modules" not in p.parts)
    assert specs, "no real specs found — this case would prove nothing"
    for spec in specs:
        rel = spec.relative_to(_E2E)
        dest = root / "tests/e2e" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        body = spec.read_text(encoding="utf-8")
        dest.write_text(
            body.rstrip("\n") + "\n\ntest.fixme('a13 probe', async () => {});\n",
            encoding="utf-8", newline="\n")

    result = _run(root)
    assert result.returncode != 0, (
        "every real spec carried an appended `test.fixme()` and the chain still "
        "exited 0.\n" + _report(result))
    # eslint prints absolute paths. Matched with the directory in front, because
    # bare names collide: `wizard.spec.ts` is a substring of
    # `deployment-wizard.spec.ts`, so a dropped `wizard.spec.ts` would hide.
    output = (result.stdout + result.stderr).replace("\\", "/")
    unreported = [rel for rel in (s.relative_to(_E2E).as_posix() for s in specs)
                  if f"tests/e2e/{rel}" not in output]
    assert not unreported, (
        f"{unreported} carried a parked test and eslint did not report them — "
        "they are exempted somewhere between the script and the rule, by name "
        "or by pattern.\n" + _report(result))
