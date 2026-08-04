"""test_federation_audit_mtail.py — federation-audit.mtail compile gate (#908)

Codifies a CI compile check for
``helm/federation-gateway/files/federation-audit.mtail`` — the mtail program that
derives ``tenant_federation_requests_total`` (ADR-020 IV-2f) and
``tenant_log_query_requests_total{account_id,project_id,status}`` /
``tenant_log_query_duration_ms`` (ADR-021 #609 PR-4) from the Envoy audit access
log.

Why this gate exists (#908 secondary line): the ``.mtail`` program is COMPILED by
the mtail sidecar at POD START. A syntax / type regression in the program
therefore surfaces only at deploy time (the sidecar CrashLoops), NOT in CI —
mtail otherwise has zero CI presence. This pins ``mtail --compile_only`` as a
gate, mirroring the ``vector validate`` codification in
``test_vector_projection_vrl.py`` and the promtool rule-pack gate.

mtail 3.0.8 — the SAME version the audit-sidecar deploys
(``helm/federation-gateway/audit-sidecar/Dockerfile`` ``MTAIL_VERSION``; the
chart's ``auditLog.image.tag`` carries that version plus a build-revision suffix,
``3.0.8-2`` since #1337) — so the gate compiles against the same mtail language
the runtime does.

⚠️ Same VERSION, not the same BINARY, since #1337: CI installs upstream's
prebuilt 3.0.8 release while the sidecar image now compiles that version's
pinned commit with a current Go toolchain (the prebuilt one is linked against Go
1.21.1 and carried 20 fixable stdlib CVEs). That is fine for what this gate
asserts — the .mtail language is fixed by the mtail version, not by the Go
toolchain that built the compiler — but do not restate it as "byte-identical to
what runs in the pod", because it is not.

Gated on ``mtail`` being on PATH (``shutil.which``); CI installs a pinned +
checksum-verified mtail (ci.yml ``python-tests`` job) so the gate is REAL there.
Without that install the behavioural tests SILENTLY SKIP — the same philosophy as
the Vector / Helm installs alongside them.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_HAS_MTAIL = shutil.which("mtail") is not None
# Set to "1" by the CI job that installs mtail (ci.yml python-tests ``env:``). When
# set, a missing mtail is a FAILURE, not a skip — see test_mtail_present_when_required.
_REQUIRE_MTAIL = os.environ.get("VIBE_REQUIRE_MTAIL") == "1"
_needs_mtail = pytest.mark.skipif(not _HAS_MTAIL, reason="mtail CLI not on PATH")

# Path (repo-relative) of the program the chart's configmap-mtail.yaml renders via
# .Files.Get — keep in lockstep if the program is ever moved/renamed.
_MTAIL_PROG = "helm/federation-gateway/files/federation-audit.mtail"


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return Path(__file__).parent.parent.parent


@pytest.fixture(scope="session")
def mtail_prog(repo_root: Path) -> Path:
    return repo_root / _MTAIL_PROG


def _compile(progdir: Path) -> subprocess.CompletedProcess:
    """Run ``mtail --compile_only`` over a directory of ``.mtail`` programs.

    ``--compile_only`` compiles and exits without loading the VM, so no
    ``--logs`` / ``--port`` is needed — it is the pre-deploy equivalent of the
    validation the sidecar implicitly does at startup. ``--logtostderr`` routes
    glog (and thus any compile diagnostic) to stderr where we capture it.
    """
    return subprocess.run(
        ["mtail", "--compile_only", "--logtostderr", "--progs", str(progdir)],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_mtail_program_exists(mtail_prog: Path) -> None:
    """Non-gated guard: the program is at the path configmap-mtail.yaml's
    ``.Files.Get`` expects, AND is non-empty. Catches a rename/move — or a
    truncate-to-zero (cf. dev-rule #11, the ``sed -i`` on a mount-path footgun) —
    even where mtail is not installed, so the gate's premise never silently rots.
    Non-emptiness matters: an empty program compiles to rc0 (see the compile
    test), so a 0-byte file would otherwise pass the whole gate."""
    assert mtail_prog.is_file(), f"mtail program missing at {_MTAIL_PROG}"
    assert mtail_prog.stat().st_size > 0, f"mtail program is empty at {_MTAIL_PROG}"


def test_mtail_present_when_required() -> None:
    """Fail-closed guard against silent disarmament. The skipif on the compile
    tests makes them skip when mtail is absent — friendly for local dev, but in
    the CI job that is SUPPOSED to install mtail (it sets ``VIBE_REQUIRE_MTAIL=1``
    via ci.yml's python-tests ``env:``) a missing mtail means the ``Install mtail``
    step was deleted or broke. Without this assertion such a regression would turn
    the gate into a green no-op: the compile tests would skip, the PR would pass,
    and a broken program would ship. Jobs that legitimately run pytest without
    mtail (validate.yaml, mirroring the vector-tests pattern) don't set the flag,
    so they keep skipping."""
    if _REQUIRE_MTAIL:
        assert _HAS_MTAIL, (
            "VIBE_REQUIRE_MTAIL=1 but `mtail` is not on PATH — the CI 'Install "
            "mtail' step (ci.yml python-tests job) is missing or broke; the compile "
            "gate would silently skip. Restore the install step."
        )


@_needs_mtail
def test_federation_audit_mtail_compiles(mtail_prog: Path, tmp_path: Path) -> None:
    """THE GATE: the shipped federation-audit.mtail compiles clean under the
    deployed mtail 3.0.8. A syntax / type regression fails CI here instead of
    CrashLooping the sidecar at deploy.

    The program is copied into an isolated dir so ``--progs`` sees ONLY it (the
    chart's files/ dir also holds envoy.yaml + .lua, which mtail would ignore,
    but an isolated dir keeps the gate hermetic).

    Single-program reality: configmap-mtail.yaml renders exactly this one program,
    and mtail compiles each ``--progs`` program independently — so compiling it in
    isolation is faithful to the sidecar's ``mtail --progs /etc/mtail`` for this
    file. If the configmap ever renders ADDITIONAL .mtail programs, extend this
    gate to compile each of them too. (Cross-program metric-name collisions are a
    RUNTIME registration concern, not a compile error: ``--compile_only`` returns 0
    even for two files declaring the same counter — verified — so no compile gate
    catches that.)"""
    progdir = tmp_path / "progs"
    progdir.mkdir()
    copied = progdir / mtail_prog.name
    copied.write_text(mtail_prog.read_text(encoding="utf-8"), encoding="utf-8")
    # Fail-open guard: `mtail --compile_only --progs <dir>` returns 0 for an EMPTY
    # progdir too, so rc==0 is necessary-but-not-sufficient — a silently-empty copy
    # would make this gate a no-op. Pin that a non-empty program is actually present
    # so rc==0 means OUR program compiled, not that mtail compiled nothing.
    assert copied.stat().st_size > 0, "copied mtail program is empty — gate would be a no-op"

    res = _compile(progdir)
    assert res.returncode == 0, (
        f"mtail --compile_only FAILED for {_MTAIL_PROG}:\n"
        f"--- stdout ---\n{res.stdout}\n--- stderr ---\n{res.stderr}"
    )


@_needs_mtail
def test_compile_gate_rejects_broken_program(tmp_path: Path) -> None:
    """Positive control: prove the gate actually catches errors (it is not a
    no-op that always passes). A program with an unterminated block MUST fail to
    compile — if this ever passes, the gate above is meaningless."""
    progdir = tmp_path / "progs"
    progdir.mkdir()
    # Valid-looking but truncated: the counter is declared, the match block is
    # opened and never closed → a guaranteed parse error in any mtail version.
    (progdir / "broken.mtail").write_text(
        "counter requests_total\n/pattern/ {\n  requests_total++\n",
        encoding="utf-8",
    )

    res = _compile(progdir)
    assert res.returncode != 0, (
        "mtail --compile_only PASSED a deliberately broken program — the compile "
        f"gate is a no-op.\n--- stdout ---\n{res.stdout}\n--- stderr ---\n{res.stderr}"
    )


# ── mtail pin parity (#1337) ────────────────────────────────────────────────
# Since #1337 the sidecar image BUILDS mtail from a pinned commit instead of
# downloading upstream's prebuilt binary. That traded a SHA-256 a reviewer could
# check against upstream's published checksums.txt for a commit id whose
# provenance lived only in a Dockerfile comment ("d4b8a71 is what v3.0.8
# dereferences to"). Content-addressing guarantees the fetched bytes match the
# id; it guarantees nothing about the id being the RIGHT one.
#
# ⭐ The oracle here is upstream's own prebuilt 3.0.8 binary — the one CI still
# installs and verifies against upstream's checksums.txt. It self-reports the
# revision it was built from, so comparing that against MTAIL_COMMIT checks the
# exact claim the comment makes, using an artifact whose integrity is already
# established. Shaped after tests/preview/test_promtool_pin_parity.py.
_DOCKERFILE = "helm/federation-gateway/audit-sidecar/Dockerfile"
_CI_YML = ".github/workflows/ci.yml"


def _one(pattern: str, text: str, what: str) -> str:
    matches = re.findall(pattern, text, re.MULTILINE)
    assert len(matches) == 1, (
        f"expected exactly one {what} match for /{pattern}/, found {len(matches)}: {matches}"
    )
    return matches[0]


def test_mtail_version_pin_parity(repo_root: Path) -> None:
    """The image's declared mtail version == the version CI installs.

    Pure file parsing, so it runs everywhere — no mtail, no Docker.

    ⚠️ SCOPE, stated precisely because an earlier docstring had it backwards:
    ``MTAIL_VERSION`` feeds only the ``-X main.Version`` / ``-X main.Branch``
    ldflags, i.e. the BANNER. The source that is actually compiled is selected by
    ``MTAIL_COMMIT`` alone. So this test catches a mislabelled image; it does NOT
    catch a language skew — changing the commit while leaving the version alone
    keeps it green. The property that matters is enforced by
    ``test_mtail_commit_pin_matches_the_release_binary`` below, which is why that
    one being skippable is guarded by ``VIBE_REQUIRE_MTAIL``.
    """
    df = (repo_root / _DOCKERFILE).read_text(encoding="utf-8")
    ci = (repo_root / _CI_YML).read_text(encoding="utf-8")
    df_version = _one(r"^ARG MTAIL_VERSION=(\S+)", df, "Dockerfile MTAIL_VERSION")
    ci_version = _one(r"^\s*MTAIL_VERSION=(\S+)", ci, "ci.yml MTAIL_VERSION")
    assert df_version == ci_version, (
        f"mtail version skew: {_DOCKERFILE} pins {df_version!r} but {_CI_YML} "
        f"installs {ci_version!r}. The compile gate would validate the .mtail "
        "program against a different mtail language than the sidecar runs."
    )


@_needs_mtail
def test_mtail_commit_pin_matches_the_release_binary(repo_root: Path) -> None:
    """MTAIL_COMMIT must be the revision upstream's own v3.0.8 binary reports.

    ⛔ This is the only mechanical check that the commit we compile is the commit
    the release tag names. Without it, the provenance of the from-source build
    rests on a prose comment (#1337 blind review).

    Gated on mtail being on PATH like its siblings — in CI that binary is the
    checksum-verified upstream release, which is precisely what makes it a valid
    oracle. Locally without mtail it skips, same as the compile gate.
    """
    df = (repo_root / _DOCKERFILE).read_text(encoding="utf-8")
    pinned_commit = _one(r"^ARG MTAIL_COMMIT=([0-9a-f]{40})", df, "Dockerfile MTAIL_COMMIT")
    pinned_version = _one(r"^ARG MTAIL_VERSION=(\S+)", df, "Dockerfile MTAIL_VERSION")

    res = subprocess.run(
        ["mtail", "--version"], capture_output=True, text=True, timeout=60,
    )
    # Report a broken binary AS a broken binary. Without this the regex below
    # fails with "found 0 matches", which points the reader at the pattern
    # instead of at the mtail install.
    assert res.returncode == 0, (
        f"`mtail --version` exited {res.returncode} — the oracle for this pin is "
        f"unusable, so the assertion below would be meaningless.\n"
        f"--- stdout ---\n{res.stdout}\n--- stderr ---\n{res.stderr}"
    )
    # mtail prints its banner on stderr in some builds and stdout in others.
    banner = f"{res.stdout}\n{res.stderr}"
    reported_version = _one(r"mtail version (\S+)", banner, "mtail --version version")
    reported_commit = _one(r"git revision ([0-9a-f]{40})", banner, "mtail --version revision")

    assert reported_version == pinned_version, (
        f"the mtail on PATH reports version {reported_version!r} but "
        f"{_DOCKERFILE} pins {pinned_version!r} — compare like with like before "
        "reading the revision assertion below."
    )
    assert reported_commit == pinned_commit, (
        f"MTAIL_COMMIT in {_DOCKERFILE} is {pinned_commit} but upstream's own "
        f"v{pinned_version} release binary was built from {reported_commit}. "
        "The sidecar would be compiled from a DIFFERENT source tree than the "
        "release it claims to be. Re-derive with:\n"
        "  gh api repos/google/mtail/git/ref/tags/v<version> --jq .object.sha\n"
        "  gh api repos/google/mtail/git/tags/<that-sha> --jq .object.sha"
    )
