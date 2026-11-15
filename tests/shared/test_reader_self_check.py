"""#3943: da-guard, da-read and da-crdecode share one self-check contract.

Each binary's self-check runs every entry of its own dispatch table on
fixtures built into it (selfcheck.go; the Go tests hold the fixtures to the
table). The generator service reads the answer by its bytes alone
(``_lib_consumer_read.SELF_CHECK_PASSED`` / ``SELF_CHECK_FAILED``), so:

* the **real** binaries, built from this checkout, pass with exactly the
  passed document on exit 0, and refuse an argument after it (exit 2,
  nothing on stdout);
* the failed document's Go constants, in each selfcheck.go, spell exactly
  ``SELF_CHECK_FAILED`` — a binary cannot be made to fail from here;
* ``READER_SELF_CHECK_ARGS`` names a self-check for every reader binary.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

import _lib_consumer_read as cr

REPO = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.usefixtures("da_read_env", "da_guard_env")


def _binary(env: str) -> str:
    return os.environ[env]


def test_every_reader_binary_has_a_self_check() -> None:
    assert set(cr.READER_SELF_CHECK_ARGS) == set(cr.READER_BINARY_ENVS)


@pytest.mark.parametrize("env", sorted(cr.READER_BINARY_ENVS))
def test_the_real_binary_passes_with_exactly_the_passed_document(env: str, tmp_path) -> None:
    # $TMPDIR as the service gives it: da-guard writes its fixture tree there
    proc = subprocess.run([_binary(env), *cr.READER_SELF_CHECK_ARGS[env]],
                          capture_output=True, check=False, timeout=120, cwd=tmp_path,
                          env={**os.environ, "TMPDIR": str(tmp_path)})
    assert (proc.returncode, proc.stdout) == (0, cr.SELF_CHECK_PASSED), proc.stderr
    assert list(tmp_path.iterdir()) == [], "the self-check left its copy behind"


@pytest.mark.parametrize("env", sorted(cr.READER_BINARY_ENVS))
def test_an_argument_after_the_self_check_is_a_usage_error(env: str) -> None:
    proc = subprocess.run([_binary(env), *cr.READER_SELF_CHECK_ARGS[env], "extra"],
                          capture_output=True, check=False, timeout=120)
    assert (proc.returncode, proc.stdout) == (2, b""), proc.stderr


_SELFCHECK_GO = {
    cr.DA_GUARD_ENV: "components/threshold-exporter/app/cmd/da-guard/selfcheck.go",
    cr.CRDECODE_ENV: "components/threshold-exporter/app/cmd/da-crdecode/selfcheck.go",
    cr.DA_READ_ENV: "components/da-read/selfcheck.go",
}


def _go_const(text: str, name: str) -> str:
    m = re.search(rf'^\s*{name}\s*=\s*"([^"]*)"', text, re.M)
    assert m, name
    return m.group(1)


@pytest.mark.parametrize("env", sorted(_SELFCHECK_GO))
def test_the_go_documents_are_the_python_bytes(env: str) -> None:
    assert set(_SELFCHECK_GO) == set(cr.READER_BINARY_ENVS)
    text = (REPO / _SELFCHECK_GO[env]).read_text(encoding="utf-8")
    key = _go_const(text, "selfCheckKey")
    for const, want in (("selfCheckPassed", cr.SELF_CHECK_PASSED),
                        ("selfCheckFailed", cr.SELF_CHECK_FAILED)):
        doc = json.dumps({key: _go_const(text, const)}, separators=(",", ":")) + "\n"
        assert doc.encode("ascii") == want, (env, const)
