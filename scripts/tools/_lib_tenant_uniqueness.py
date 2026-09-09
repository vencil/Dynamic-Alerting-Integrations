"""Does this ARTIFACT declare one tenant twice? One measurement, two producers.

Two tools build a flat set of carriers the exporter will read —
``configmap_assemble_precheck`` (for ``make configmap-assemble``) and
``assemble_config_dir`` (for ``make sharded-assemble``). Both have to ask
the same question before they hand that set on, because the exporter's
answer is all-or-nothing: a tenant id found in two files raises
``*DuplicateTenantError`` (``config_hierarchy.go``) and ``config.go`` rejects
the ENTIRE directory, so every tenant there loses alerting.

⛔ THE PREDICATE IS "ONE ID, TWO FILES", NOT "TWO SPELLINGS OF ONE STEM".
Measured on #1794: ``db-a.yaml`` + ``legacy-db-a.yaml`` (one tenant, two
stems) reaches the identical rejection, and ``db-a.yaml`` + ``db-a.yml``
declaring *different* tenants is accepted by the exporter. Keying on the
stem is blind to the first and a false refusal on the second.

This module owns the MEASUREMENT only: run ``validate_config``'s
``tenant_uniqueness`` (#1577 — the check that already asks this question)
against the staged artifact and classify the answer into three states. It
deliberately does NOT own the wording: the two callers refuse for different
reasons and must be able to say so.

⛔ Three states, not two. "Could not measure" (the validator hung, its output
would not parse, or it stopped reporting the check) is its own outcome —
folding it into CLEAN is how a gate reports a green it never earned.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Mapping, NamedTuple

from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR

_THIS_DIR = Path(__file__).resolve().parent
VALIDATE = _THIS_DIR / "ops" / "validate_config.py"

CHECK_NAME = "tenant_uniqueness"

# Generous: this walks a customer's whole conf.d tree. It is a hang guard,
# not a performance budget.
_VALIDATE_TIMEOUT_S = 300

CLEAN = "clean"
DUPLICATE = "duplicate"
UNMEASURED = "unmeasured"

#: What each outcome means for a producer's exit code. Shared so the two
#: callers cannot drift on which state blocks and which is a caller error.
EXIT_FOR: Mapping[str, int] = {
    CLEAN: EXIT_OK,
    DUPLICATE: EXIT_VIOLATION,
    UNMEASURED: EXIT_CALLER_ERROR,
}


class Verdict(NamedTuple):
    """outcome is always one of CLEAN / DUPLICATE / UNMEASURED."""

    outcome: str
    reason: str          # why, in one line; "" when CLEAN
    details: list[str]   # the check's own detail lines
    action: str          # the check's suggested_action, or ""


def _run_validate(config_dir: Path) -> list[dict]:
    """``validate_config --json``, parsed. Raises RuntimeError with a reason."""
    try:
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", str(VALIDATE),
             "--config-dir", str(config_dir), "--json"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            # A hang here would wedge a target customers run, with no output
            # at all. Bounded, and the timeout lands in the SAME "could not
            # measure" arm as a parse failure.
            timeout=_VALIDATE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"validate_config did not finish within {_VALIDATE_TIMEOUT_S}s"
        ) from exc
    # ⛔ The return code is deliberately IGNORED: a non-zero rc here means
    # "some check failed", and this measurement speaks for one of them.
    out = proc.stdout.strip()
    if not out:
        raise RuntimeError(
            f"validate_config produced no stdout (rc={proc.returncode}). "
            f"stderr: {proc.stderr.strip()[:400] or '(empty)'}"
        )
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"validate_config --json was not parseable: {exc}") from exc
    if not isinstance(data, list):
        raise RuntimeError(
            f"validate_config --json returned {type(data).__name__}, expected a list"
        )
    return data


def verdict_for(carriers: Mapping[str, Path]) -> Verdict:
    """Ask the question about the artifact `carriers` would become.

    `carriers` maps the path the file will have in the artifact (relative,
    POSIX, may contain `/`) to the file it is copied from — the shape
    `assemble_config_dir` already holds, and one line away from the
    pre-check's list of paths. They are
    staged into a flat throwaway directory because that, and not the source
    tree, is what the exporter will read: a second declaration living in a
    subdirectory or in a source file that loses a name collision never ships
    through these paths, and refusing over it would block a deployable
    artifact.
    """
    try:
        with tempfile.TemporaryDirectory(prefix="tenant-uniq-") as tmp:
            flat = Path(tmp)
            for name, src in carriers.items():
                dst = flat / name
                # Keys may be relative PATHS (a producer that recurses), and
                # the label the check prints is that path. Flattening them
                # would let two same-named carriers overwrite each other and
                # take a real duplicate out of the question.
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            checks = _run_validate(flat)
    except OSError as exc:
        return Verdict(UNMEASURED,
                       f"could not stage the carrier set for checking — {exc}",
                       [], "")
    except RuntimeError as exc:
        return Verdict(UNMEASURED,
                       f"could not run the duplicate-tenant check — {exc}",
                       [], "")

    entry = next((c for c in checks
                  if isinstance(c, dict) and c.get("check") == CHECK_NAME), None)
    if entry is None:
        names = sorted(str(c.get("check")) for c in checks if isinstance(c, dict))
        return Verdict(
            UNMEASURED,
            f"validate_config did not report a `{CHECK_NAME}` check; "
            f"it reported: {names}",
            [], "")

    details = [str(line) for line in (entry.get("details") or [])]
    action = str(entry.get("suggested_action") or "")
    status = entry.get("status")

    if status == "fail":
        return Verdict(DUPLICATE, f"{CHECK_NAME} failed", details, action)
    # ⛔ Derived, not enumerated: anything that is not an affirmative pass is
    # not a pass. `tenant_uniqueness` also returns `warn`, and its own details
    # say "this is a limit on what was checked, not a clean result" — a second
    # declaration can be sitting in a file it could not open.
    if status != "pass":
        return Verdict(UNMEASURED,
                       f"{CHECK_NAME} came back `{status}`, which is not a pass",
                       details, action)
    return Verdict(CLEAN, "", details, action)
