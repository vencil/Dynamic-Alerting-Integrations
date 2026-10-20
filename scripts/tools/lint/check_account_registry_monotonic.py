#!/usr/bin/env python3
"""check_account_registry_monotonic.py — guard _account_registry.yaml's
next_account_id against a cross-commit DECREASE (#609 / ADR-021, Gemini #1+#3).

Why this exists
---------------
tenant-api hands out a monotonic, never-reused ``account_id`` per tenant — the
VictoriaLogs partition key — from an ever-increasing ``next_account_id``
high-water mark in ``conf.d/_account_registry.yaml`` (account/registry.go). The
"never reused" invariant is what makes the id safe to hand to a log store: a
recycled id given to a NEW tenant would let it read the OLD tenant's logs still
inside the retention window — a cross-tenant leak.

The runtime allocator only ever INCREASES the counter, so it cannot break the
invariant. The threat is a HUMAN commit:

  - ``git revert`` of an onboarding commit rolls ``next_account_id`` BACK *and*
    shrinks ``allocations`` in lockstep — so Parse()'s "id >= next" integrity
    check (which only sees the tenants that REMAIN) is satisfied and does NOT
    catch it. The retention window still holds the reverted tenant's logs, so
    re-issuing its id later leaks.
  - A hand-edit that lowers the counter is the same hazard.

Both land through a git commit, so a cross-commit "counter must not decrease"
gate catches the entire human-error class that the runtime cannot.

What it compares
----------------
The registry file's ``next_account_id`` at two git revisions, as yaml.v3 —
the parser tenant-api's ``account.Parse`` decodes it with — reads it: each
blob is read by ``da-guard parse-tree`` in file mode (ADR-036; a count is a
structured claim, never grep). PyYAML's YAML 1.1 reading differs on spellings
yaml.v3 decodes as an integer: ``0o2000`` is 1024 to tenant-api and was the
string "0o2000" here (a false fail-closed); ``yes`` is the string "yes" to
both now (no longer PyYAML's ``True``). The comparison is:

  - pre-commit (default): INCOMING = the staged blob (``git show :<path>``)
    vs BASELINE = the committed blob (``git show HEAD:<path>``). Catches the
    revert/edit at the moment it is committed.
  - CI / PR (``--base <ref>`` or ``$GITHUB_BASE_REF`` / ``$LINT_DIFF_BASE``):
    INCOMING = ``git show HEAD:<path>`` vs BASELINE = ``git show <base>:<path>``.
    Catches the CUMULATIVE PR-vs-base decrease (a revert merged anywhere in the
    branch).

Decision table (current = incoming next_account_id, prev = baseline):
  - baseline blob ABSENT (file is new in this change)      → PASS (Day-0).
  - incoming blob ABSENT (file deleted / not staged here)  → PASS (out of scope;
    a registry deletion is its own separate hazard, not this gate's concern).
  - current <  prev                                        → FAIL (the leak).
  - current >= prev                                        → PASS (monotonic;
    equal is fine — a no-counter-change commit).
  - either present blob unparseable / malformed counter    → FAIL CLOSED (a
    registry we cannot trust must not pass a safety gate; never fail-open).
    Malformed = parse-tree cannot read the blob (bad syntax, a key written
    twice, a top level that is not a mapping, …), or ``next_account_id`` is
    missing or not an integer tenant-api's ``uint32`` takes (a ``!!int`` in
    0..4294967295). Stricter than tenant-api on purpose: a null or missing
    counter (which tenant-api primes at 1000) and a float (which it truncates)
    fail closed here.

Registry path assumption
-------------------------
The live registry lives in the deployed ``conf.d`` (a PVC / mounted volume),
NOT necessarily checked into this repo — the threshold-exporter example tree
``components/threshold-exporter/config/conf.d`` is the in-repo canonical conf.d
and the default scan path. When the file is not git-tracked in the compared
revisions the gate **gracefully no-ops (PASS)** — there is nothing to regress.
Override with ``--registry-path`` for a deployment whose conf.d lives elsewhere.

Usage
-----
::

    python3 scripts/tools/lint/check_account_registry_monotonic.py            # report (pre-commit)
    python3 scripts/tools/lint/check_account_registry_monotonic.py --ci        # exit 1 on decrease
    python3 scripts/tools/lint/check_account_registry_monotonic.py --ci --base origin/main   # PR-vs-base
    python3 scripts/tools/lint/check_account_registry_monotonic.py --registry-path path/to/_account_registry.yaml

Exit codes (per scripts/tools/_lib_exitcodes.py)
------------------------------------------------
- ``0`` — monotonic (or report-only / file absent / not git-tracked).
- ``1`` — DECREASE detected under ``--ci``, OR a present-but-unparseable
  registry (fail-closed).
- ``2`` — cannot run: not a git repo, git unavailable, a resolvable but
  missing ``--base`` ref, or no usable ``da-guard`` when a blob has to be
  read (caller/environment error). A registry absent at either revision
  needs no da-guard: nothing is read.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

# Pull shared compat (UTF-8 stdout) + exit-code constants from scripts/tools/.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
try:
    from _lib_compat import try_utf8_stdout  # noqa: E402
except Exception:  # pragma: no cover - compat shim optional
    def try_utf8_stdout() -> None:  # type: ignore
        pass
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
import _lib_parse_cache  # noqa: E402
from _lib_parse_tree import FileUnreadable  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[3]

# In-repo canonical conf.d (threshold-exporter example tree). The live registry
# is runtime state under the deployed conf.d; this is the default scan path and
# the gate no-ops when the file is absent there.
DEFAULT_REGISTRY_REL = "components/threshold-exporter/config/conf.d/_account_registry.yaml"

# Sentinel distinguishing "blob is absent at this revision" from "blob is present
# but empty" — an empty/whitespace registry parses to the reserved floor, which
# is a legitimate state, not an absence.
_ABSENT = object()


def _run_git(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a git command from PROJECT_ROOT, capturing text (utf-8, replace)."""
    return subprocess.run(
        ["git", *args],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=30,
    )


def _is_git_repo() -> bool:
    return _run_git(["rev-parse", "--git-dir"]).returncode == 0


def _show_blob(rev: str, rel_path: str):
    """Return the bytes of ``<rev>:<rel_path>`` or ``_ABSENT``.

    ``rev`` of ``""`` selects the INDEX (staged) copy via ``git show :<path>``.
    A non-zero git exit means the path does not exist at that revision (new
    file, deleted file, or an unborn HEAD) → ``_ABSENT``. Bytes, not text:
    a blob that is not UTF-8 is parse-tree's to refuse, not ours to repair.
    """
    spec = f"{rev}:{rel_path}"
    res = subprocess.run(
        ["git", "show", spec], cwd=str(PROJECT_ROOT), capture_output=True,
        check=False, timeout=30)
    if res.returncode != 0:
        return _ABSENT
    return res.stdout


def _ref_exists(rev: str) -> bool:
    """True if ``rev`` resolves to a commit locally (for --base validation)."""
    return _run_git(["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"]).returncode == 0


class RegistryParseError(ValueError):
    """A present registry blob could not be parsed / has no usable counter."""


#: The name a blob is read under. tenant-api reads the registry by its path,
#: not through the exporter's tree walk, so which directory or name the file
#: has is no part of how its content reads: it is judged on its own, in an
#: empty directory, under the registry's own name.
_BLOB_NAME = "_account_registry.yaml"

#: tenant-api decodes the counter into a ``uint32``.
_UINT32_MAX = 2**32 - 1


def _read_registry(data: bytes) -> object:
    """*data* as yaml.v3 reads it (``da-guard parse-tree`` file mode).
    Raises FileUnreadable and ParseTreeError (no usable da-guard)."""
    import tempfile  # noqa: PLC0415
    with tempfile.TemporaryDirectory(prefix="da-account-registry-") as empty:
        return _lib_parse_cache.read_text_as(empty, _BLOB_NAME, data)


def parse_next_account_id(text: "str | bytes") -> int:
    """Extract ``next_account_id`` from registry YAML (structured parse,
    yaml.v3 through ``da-guard parse-tree``).

    A blank/whitespace document is the brand-new-file state the GitOps layer
    has not written yet: account/registry.go primes it at the reserved floor
    (1000), so we mirror that — blank → 1000 (the lowest legitimate counter),
    NOT a parse error, and no da-guard is needed for it. Any other malformed
    shape (a document parse-tree cannot read, a non-mapping top level, a
    missing / non-integer / boolean / out-of-``uint32`` ``next_account_id``)
    is fail-closed. Raises RegistryParseError, and ParseTreeError when there
    is no usable da-guard.
    """
    data = text.encode("utf-8") if isinstance(text, str) else text
    if data is None or not data.strip():
        # Mirror account.newRegistry(): blank primes at FirstTenantAccountID.
        return 1000
    try:
        doc = _read_registry(data)
    except FileUnreadable as exc:
        raise RegistryParseError(
            f"registry YAML does not parse (da-guard parse-tree: {exc.kind}: "
            f"{exc.message})") from exc
    if not isinstance(doc, dict):  # pragma: no cover - parse-tree refuses it first
        raise RegistryParseError("registry root is not a mapping")
    if "next_account_id" not in doc:
        raise RegistryParseError("registry has no next_account_id key")
    val = doc["next_account_id"]
    # bool is an int subclass in Python — reject it explicitly so `true` does
    # not silently read as 1.
    if isinstance(val, bool) or not isinstance(val, int):
        raise RegistryParseError(
            f"next_account_id is {val!r} ({type(val).__name__}), want an integer"
        )
    if not 0 <= val <= _UINT32_MAX:
        raise RegistryParseError(
            f"next_account_id is {val}, outside the uint32 tenant-api decodes it into")
    return val


def evaluate(registry_rel: str, base: Optional[str]) -> dict:
    """Compare next_account_id across two git revisions for one registry file.

    Returns a result dict:
      {status, current, previous, incoming_rev, baseline_rev, detail}
      status ∈ {"ok", "absent", "decrease", "parse_error"}

    Raises RegistryParseError on a present-but-malformed blob (fail-closed; the
    caller turns it into exit 1).
    """
    if base:
        incoming_rev, baseline_rev = "HEAD", base
    else:
        incoming_rev, baseline_rev = "", "HEAD"  # "" = staged/index

    incoming = _show_blob(incoming_rev, registry_rel)
    baseline = _show_blob(baseline_rev, registry_rel)

    incoming_label = "staged" if incoming_rev == "" else incoming_rev
    baseline_label = baseline_rev

    # Baseline absent → the file is new in this change (Day-0). Nothing to
    # regress against → pass. Incoming absent → the file is not part of this
    # change / was deleted → out of scope for a monotonic gate → pass.
    if baseline is _ABSENT or incoming is _ABSENT:
        why = []
        if baseline is _ABSENT:
            why.append(f"no baseline at {baseline_label}")
        if incoming is _ABSENT:
            why.append(f"no incoming at {incoming_label}")
        return {
            "status": "absent",
            "current": None,
            "previous": None,
            "incoming_rev": incoming_label,
            "baseline_rev": baseline_label,
            "detail": "; ".join(why),
        }

    # Both present → parse both (fail-closed on either).
    prev = parse_next_account_id(baseline)
    current = parse_next_account_id(incoming)

    if current < prev:
        status = "decrease"
        detail = (
            f"next_account_id decreased {prev} → {current} "
            f"({baseline_label} → {incoming_label}). An account_id is monotonic "
            f"and NEVER reused; lowering the high-water mark re-issues an id "
            f"whose tenant's logs may still be in the retention window "
            f"(cross-tenant leak). A git revert/hand-edit of an onboarding "
            f"commit is the usual cause — undo it (the allocator only ever "
            f"increases the counter)."
        )
    else:
        status = "ok"
        detail = f"next_account_id {prev} → {current} (monotonic)"

    return {
        "status": status,
        "current": current,
        "previous": prev,
        "incoming_rev": incoming_label,
        "baseline_rev": baseline_label,
        "detail": detail,
    }


@_lib_parse_cache.exit_on_parse_tree_error
def main() -> int:
    """CLI entry point: cross-commit monotonic guard for _account_registry.yaml."""
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Assert conf.d/_account_registry.yaml's next_account_id never "
        "DECREASES across a commit (revert/edit guard; #609 / ADR-021 cross-tenant "
        "leak prevention)."
    )
    parser.add_argument(
        "--registry-path",
        default=DEFAULT_REGISTRY_REL,
        help="Registry path (repo-relative) to guard. Default: the in-repo "
        "canonical conf.d. Override for a deployment whose conf.d lives elsewhere.",
    )
    parser.add_argument(
        "--base",
        default=None,
        help="Diff base ref for CI/PR mode (compare <base>:registry vs "
        "HEAD:registry). Default (no --base): pre-commit mode (HEAD vs staged). "
        "Falls back to $GITHUB_BASE_REF / $LINT_DIFF_BASE when unset.",
    )
    parser.add_argument("--ci", action="store_true", help="Exit 1 on a decrease.")
    args = parser.parse_args()

    # Environment / invocation sanity (EXIT_CALLER_ERROR).
    if not _is_git_repo():
        print("⚠ not a git repo — skipping account-registry monotonic check")
        return EXIT_CALLER_ERROR

    # Resolve the diff base: explicit --base wins, else CI envs, else stay in
    # pre-commit (staged-vs-HEAD) mode. A base that is SET but does not resolve
    # is a caller/environment error (shallow clone — see lint-policy fetch-depth).
    base = args.base
    if not base:
        env_base = os.environ.get("LINT_DIFF_BASE")
        gh_base = os.environ.get("GITHUB_BASE_REF")
        if env_base:
            base = env_base
        elif gh_base:
            base = f"origin/{gh_base}"
    if base and not _ref_exists(base):
        print(
            f"ERROR: diff base ref '{base}' does not resolve. In CI ensure "
            f"actions/checkout uses fetch-depth: 0 (or fetch the base ref first). "
            f"See docs/internal/lint-policy.md.",
            file=sys.stderr,
        )
        return EXIT_CALLER_ERROR

    registry_rel = args.registry_path.replace("\\", "/")

    try:
        result = evaluate(registry_rel, base)
    except RegistryParseError as exc:
        # FAIL CLOSED: a registry we cannot trust must never pass a safety gate.
        print(
            f"✗ {registry_rel}: cannot validate next_account_id — {exc}\n"
            f"  A malformed registry blocks the monotonic guard (fail-closed): a "
            f"wrong/unreadable counter risks re-issuing a live account_id.",
            file=sys.stderr,
        )
        return EXIT_VIOLATION  # fail-closed even without --ci: a parse error is system-actionable

    if result["status"] == "absent":
        print(
            f"✓ {registry_rel}: monotonic check skipped — {result['detail']} "
            f"(nothing to regress)."
        )
        return EXIT_OK

    if result["status"] == "decrease":
        print(f"✗ {registry_rel}: {result['detail']}", file=sys.stderr)
        return EXIT_VIOLATION if args.ci else EXIT_OK

    print(f"✓ {registry_rel}: {result['detail']}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
