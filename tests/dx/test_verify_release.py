#!/usr/bin/env python3
"""Tests for scripts/tools/dx/verify_release.sh.

Customer-facing helper that wraps cosign keyless verification + sha256
check. We can't easily call the REAL sigstore infrastructure in CI
(needs network + actual signed artefacts), so these tests cover the
script's testable surface:

  - arg parsing (--tag / --artefact required, --download-dir optional,
    --quiet / --help)
  - error paths (missing tools, missing flags, unknown flags)
  - mock-cosign happy path (verify command actually invoked correctly)
  - mock-cosign failure path (cosign exit non-zero → script exit 1)
  - sha256 mismatch path (using a tampered local file vs SHA256SUMS)

We exercise the script end-to-end via subprocess + a mock cosign on
PATH that records its arguments and returns the desired exit code.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "tools" / "dx" / "verify_release.sh"


def run_script(args, env=None, cwd=None):
    """Invoke the script with bash, capture stdout / stderr / exit."""
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=full_env,
        cwd=str(cwd) if cwd else None,
        check=False,
    )


@pytest.fixture
def fake_cosign_dir(tmp_path):
    """Create a directory with a stub `cosign` that succeeds.

    Returns the directory path (caller prepends to PATH). The stub
    records every invocation to <dir>/cosign-args.log so tests can
    assert on the exact command line cosign saw.
    """
    cosign = tmp_path / "cosign"
    log = tmp_path / "cosign-args.log"
    cosign.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        echo "$@" >> '{log}'
        # mimic cosign's exit semantics: non-zero on `verify-blob` if
        # an env var asks us to fail (used by the failure-path test).
        if [ "${{MOCK_COSIGN_FAIL:-0}}" = "1" ]; then
            echo "stub cosign: simulated verification failure" >&2
            exit 1
        fi
        echo "Verified OK"
        exit 0
    """))
    cosign.chmod(0o755)
    return tmp_path


# ─── help / usage ───────────────────────────────────────────────────


def test_help_short_flag_returns_zero():
    r = run_script(["-h"])
    assert r.returncode == 0
    assert "verify-release" in r.stdout.lower() or "verify_release" in r.stdout
    assert "--tag" in r.stdout
    assert "--artefact" in r.stdout


def test_help_long_flag_returns_zero():
    r = run_script(["--help"])
    assert r.returncode == 0
    assert "Exit codes" in r.stdout


def test_no_args_returns_caller_error():
    r = run_script([])
    assert r.returncode == 2
    assert "required" in r.stderr.lower()


def test_missing_artefact_flag_returns_caller_error():
    r = run_script(["--tag", "tools/v2.8.0"])
    assert r.returncode == 2
    assert "required" in r.stderr.lower()


def test_missing_tag_flag_returns_caller_error():
    r = run_script(["--artefact", "foo.tar.gz"])
    assert r.returncode == 2
    assert "required" in r.stderr.lower()


def test_unknown_flag_returns_caller_error():
    r = run_script(["--frobnicate"])
    assert r.returncode == 2
    assert "unknown" in r.stderr.lower()


def test_flag_value_missing_returns_caller_error():
    r = run_script(["--tag"])  # bare --tag without value
    assert r.returncode == 2
    assert "needs a value" in r.stderr.lower()


# ─── tool resolution ────────────────────────────────────────────────


def _path_without_cosign(tmp_path):
    """Build a PATH that has bash + standard tools but NO cosign.

    We can't just use a single tmp_path because subprocess.run needs
    `bash` itself on PATH. Instead, keep system bin paths but skip any
    directory whose listing contains a cosign binary.
    """
    keep = []
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d:
            continue
        candidate = os.path.join(d, "cosign")
        if os.path.exists(candidate):
            continue  # exclude any directory that has cosign
        keep.append(d)
    return os.pathsep.join(keep)


def test_cosign_missing_returns_caller_error(tmp_path):
    """When cosign isn't on PATH, friendly install hint + exit 2."""
    pruned_path = _path_without_cosign(tmp_path)
    r = run_script(
        ["--tag", "tools/v2.8.0", "--artefact", "foo.tar.gz"],
        env={"PATH": pruned_path},
    )
    assert r.returncode == 2
    assert "cosign" in r.stderr.lower()
    assert "install" in r.stderr.lower()


# ─── happy path with mock cosign ────────────────────────────────────


def _stage_local_artefact(workdir: Path, fake_cosign: Path):
    """Pre-populate the download dir so the script doesn't try to
    fetch from GitHub (no network in test). Returns the download dir
    path the script was told to use.
    """
    dl = workdir / "verify-tmp"
    dl.mkdir()
    # Minimal artefact + sig + cert. Content doesn't matter for arg-
    # plumbing tests — the mock cosign ignores file contents.
    (dl / "foo.tar.gz").write_bytes(b"fake-tarball-content")
    (dl / "foo.tar.gz.sig").write_text("fake-signature")
    (dl / "foo.tar.gz.cert").write_text("fake-certificate")
    return dl


def test_happy_path_invokes_cosign_with_pinned_identity(tmp_path, fake_cosign_dir):
    """Mock cosign succeeds → script exits 0 + invokes cosign with the
    expected --certificate-identity (pinned to release.yaml @ tag)."""
    dl = _stage_local_artefact(tmp_path, fake_cosign_dir)
    # Prepend mock cosign to PATH so `command -v cosign` finds it.
    env = {"PATH": f"{fake_cosign_dir}:{os.environ.get('PATH', '')}"}
    r = run_script(
        ["--tag", "tools/v2.8.0", "--artefact", "foo.tar.gz",
         "--download-dir", str(dl)],
        env=env,
    )
    assert r.returncode == 0, f"stderr: {r.stderr}\nstdout: {r.stdout}"
    # The mock recorded the cosign invocation; assert on the cmdline.
    log = (fake_cosign_dir / "cosign-args.log").read_text()
    assert "verify-blob" in log
    assert "--certificate-identity" in log
    # Identity must reference our repo + workflow + tag exactly.
    assert "vencil/Dynamic-Alerting-Integrations/.github/workflows/release.yaml" in log
    assert "refs/tags/tools/v2.8.0" in log
    assert "https://token.actions.githubusercontent.com" in log
    # The artefact file path is the last arg cosign sees.
    assert "foo.tar.gz" in log


def test_happy_path_uses_repo_overrides(tmp_path, fake_cosign_dir):
    """REPO_OWNER / REPO_NAME overrides flow into the certificate-
    identity. Forks need this to verify their own re-released
    artefacts."""
    dl = _stage_local_artefact(tmp_path, fake_cosign_dir)
    env = {
        "PATH": f"{fake_cosign_dir}:{os.environ.get('PATH', '')}",
        "REPO_OWNER": "alice",
        "REPO_NAME": "MyFork",
    }
    r = run_script(
        ["--tag", "tools/v9.9.9", "--artefact", "foo.tar.gz",
         "--download-dir", str(dl)],
        env=env,
    )
    assert r.returncode == 0
    log = (fake_cosign_dir / "cosign-args.log").read_text()
    assert "alice/MyFork" in log
    assert "refs/tags/tools/v9.9.9" in log


# ─── failure path with mock cosign ──────────────────────────────────


def test_cosign_failure_returns_one(tmp_path, fake_cosign_dir):
    """Mock cosign exits non-zero → script reports failure + exits 1."""
    dl = _stage_local_artefact(tmp_path, fake_cosign_dir)
    env = {
        "PATH": f"{fake_cosign_dir}:{os.environ.get('PATH', '')}",
        "MOCK_COSIGN_FAIL": "1",
    }
    r = run_script(
        ["--tag", "tools/v2.8.0", "--artefact", "foo.tar.gz",
         "--download-dir", str(dl)],
        env=env,
    )
    assert r.returncode == 1, f"stderr: {r.stderr}\nstdout: {r.stdout}"
    assert "cosign verification failed" in r.stderr.lower()


# ─── sha256 path ────────────────────────────────────────────────────


def test_sha256_mismatch_returns_one(tmp_path, fake_cosign_dir):
    """If SHA256SUMS lists a hash that doesn't match the local file,
    the script catches the mismatch BEFORE calling cosign and exits 1."""
    dl = _stage_local_artefact(tmp_path, fake_cosign_dir)
    # Plant a SHA256SUMS entry with a hash that intentionally doesn't
    # match foo.tar.gz's bytes.
    bogus_hash = "0" * 64
    (dl / "SHA256SUMS").write_text(f"{bogus_hash}  foo.tar.gz\n")
    env = {"PATH": f"{fake_cosign_dir}:{os.environ.get('PATH', '')}"}
    r = run_script(
        ["--tag", "tools/v2.8.0", "--artefact", "foo.tar.gz",
         "--download-dir", str(dl)],
        env=env,
    )
    assert r.returncode == 1
    assert "sha256 mismatch" in r.stderr.lower()


def test_sha256_match_passes_to_cosign(tmp_path, fake_cosign_dir):
    """Correct SHA256SUMS entry → script confirms hash + proceeds to
    cosign verify. (Tests that the hash branch doesn't accidentally
    skip cosign verification.)"""
    dl = _stage_local_artefact(tmp_path, fake_cosign_dir)
    # Compute the actual sha256 of foo.tar.gz's bytes and write
    # SHA256SUMS with the matching hash.
    actual = subprocess.check_output(
        ["sha256sum", str(dl / "foo.tar.gz")], text=True
    ).split()[0]
    (dl / "SHA256SUMS").write_text(f"{actual}  foo.tar.gz\n")
    env = {"PATH": f"{fake_cosign_dir}:{os.environ.get('PATH', '')}"}
    r = run_script(
        ["--tag", "tools/v2.8.0", "--artefact", "foo.tar.gz",
         "--download-dir", str(dl)],
        env=env,
    )
    assert r.returncode == 0, f"stderr: {r.stderr}"
    # Both hash + signature steps must have run.
    log = (fake_cosign_dir / "cosign-args.log").read_text()
    assert "verify-blob" in log


# ─── quiet mode ─────────────────────────────────────────────────────


def test_quiet_mode_suppresses_info_keeps_errors(tmp_path):
    """--quiet hides info-level output but errors still go to stderr."""
    pruned_path = _path_without_cosign(tmp_path)
    r = run_script(
        ["--tag", "tools/v2.8.0", "--artefact", "foo.tar.gz", "--quiet"],
        env={"PATH": pruned_path},
    )
    # Should still exit 2 (cosign missing) and still print the error.
    assert r.returncode == 2
    assert r.stdout.strip() == "", "quiet mode should not produce stdout output before error"
    assert "cosign" in r.stderr.lower()
