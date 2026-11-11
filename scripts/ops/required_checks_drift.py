"""Compare .github/required-checks.yaml with main's live branch protection.

Input is the JSON of `GET /repos/{owner}/{repo}/branches/main` (read access is
enough; `/branches/main/protection` needs admin). The file is the one source
for the required-check list (#3696); this is the half that notices when
GitHub's settings and the file disagree.

Exit codes (scripts/tools/_lib_exitcodes.py contract):
  0  file and settings agree
  1  they disagree — every difference is printed, one per line
  2  the settings could not be read: `protection` or its
     `required_status_checks` is missing, or the JSON / YAML is malformed.
     ⛔ Never treated as "no required checks": an empty answer from a token
     that cannot see the field would otherwise read as "everything was
     removed" or, worse, as agreement with an empty file.

`pending: true` entries are declared but not yet enabled — the step between
"the job has reported once" and "the owner ticks it" (see the file header).
They may be absent from the settings; present there, they are a difference
too (the flag is now stale).

Usage:
  gh api repos/OWNER/REPO/branches/main > branch.json
  python3 scripts/ops/required_checks_drift.py branch.json [.github/required-checks.yaml]
"""
from __future__ import annotations

import json
import os
import sys

# The run() exit (_lib_exitcodes, with _lib_compat) lives in scripts/tools/.
sys.path.append(os.path.join(os.path.dirname(os.path.realpath(__file__)), os.pardir, "tools"))

import yaml  # noqa: E402

EXIT_DRIFT = 1
EXIT_UNREADABLE = 2
DEFAULT_SSOT = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                            os.pardir, os.pardir, ".github", "required-checks.yaml")


class Unreadable(Exception):
    """The live settings (or the file) cannot be read into a comparable form."""


def live_checks(branch: dict) -> tuple[dict[str, int | None], str | None]:
    """{context: app_id} and the enforcement level, from `GET /branches/main`."""
    protection = branch.get("protection") if isinstance(branch, dict) else None
    if not isinstance(protection, dict):
        raise Unreadable("the response has no `protection` object")
    rsc = protection.get("required_status_checks")
    if not isinstance(rsc, dict) or not isinstance(rsc.get("checks"), list):
        raise Unreadable("`protection.required_status_checks.checks` is missing — "
                         "the token may not be allowed to see it")
    checks: dict[str, int | None] = {}
    for item in rsc["checks"]:
        if not isinstance(item, dict) or not isinstance(item.get("context"), str):
            raise Unreadable(f"unrecognised check entry {item!r}")
        checks[item["context"]] = item.get("app_id")
    return checks, rsc.get("enforcement_level")


def declared(ssot: dict) -> tuple[dict[str, bool], int, str | None]:
    """{name: pending}, the declared app_id and enforcement level."""
    if not isinstance(ssot, dict) or not isinstance(ssot.get("required"), list):
        raise Unreadable("the file has no `required` list")
    names: dict[str, bool] = {}
    for entry in ssot["required"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise Unreadable(f"unrecognised `required` entry {entry!r}")
        if entry["name"] in names:
            raise Unreadable(f"{entry['name']!r} is listed twice")
        names[entry["name"]] = entry.get("pending") is True
    return names, ssot.get("app_id"), ssot.get("enforcement_level")


def compare(ssot: dict, branch: dict) -> list[str]:
    """Every difference between the file and the live settings, sorted."""
    live, level = live_checks(branch)
    names, app_id, want_level = declared(ssot)
    problems = []
    for name, pending in sorted(names.items()):
        if name not in live and not pending:
            problems.append(f"{name!r} is required in the file but not in branch "
                            "protection — tick it in the settings, or mark it "
                            "`pending: true` until the job has reported once")
        elif name in live and pending:
            problems.append(f"{name!r} is enabled in branch protection but still "
                            "`pending: true` in the file — drop the flag")
    for name in sorted(set(live) - set(names)):
        problems.append(f"{name!r} is required in branch protection but not in "
                        "the file — add it (tests/ops/test_required_checks_ssot.py "
                        "then checks that a job always reports it)")
    for name, got in sorted(live.items()):
        if name in names and got != app_id:
            problems.append(f"{name!r} is bound to app_id {got!r} in branch "
                            f"protection; the file says {app_id!r}")
    if level != want_level:
        problems.append(f"enforcement_level is {level!r} in branch protection; "
                        f"the file says {want_level!r}")
    return problems


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not 1 <= len(args) <= 2:
        sys.stderr.write(__doc__.split("Usage:", 1)[1])
        return EXIT_UNREADABLE
    try:
        with open(args[0], encoding="utf-8") as fh:
            branch = json.load(fh)
        with open(args[1] if len(args) == 2 else DEFAULT_SSOT, encoding="utf-8") as fh:
            ssot = yaml.safe_load(fh)
        problems = compare(ssot, branch)
    except (Unreadable, ValueError, yaml.YAMLError) as err:
        sys.stderr.write(f"required_checks_drift: cannot compare — {err}\n")
        return EXIT_UNREADABLE
    for line in problems:
        print(line)
    if problems:
        return EXIT_DRIFT
    print("required checks: .github/required-checks.yaml matches branch protection")
    return 0


if __name__ == "__main__":
    import _lib_exitcodes
    _lib_exitcodes.run(main)
