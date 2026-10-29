#!/usr/bin/env python3
"""Describe effective tenant config — as the exporter's resolver answers it.

Usage:
    python3 scripts/tools/dx/describe_tenant.py <tenant-id> [--conf-d PATH]
    python3 scripts/tools/dx/describe_tenant.py <tenant-id> --show-sources
    python3 scripts/tools/dx/describe_tenant.py <tenant-id> --diff <tenant-id-2>
    python3 scripts/tools/dx/describe_tenant.py <tenant-id> --what-if <draft.yaml> --replaces <conf.d/team/_defaults.yaml>
    python3 scripts/tools/dx/describe_tenant.py <tenant-id> --what-if <draft.yaml> --at <conf.d/team/_defaults.yaml>
    python3 scripts/tools/dx/describe_tenant.py --all --conf-d PATH --output effective.json

Every value comes from `da-guard effective` (ADR-036: conf.d is parsed only
by Go) — the effective config, the defaults chain, the platform and profile
overlays, source_file / source_hash and merged_hash are tenant-api's
/effective answer for the tenant, written as Go writes them. This tool reads
no conf.d file with a YAML parser of its own. `effective_config` is
da-guard effective's value as is: its `_custom_alerts` is the one list the
exporter resolves for the tenant (its own, its root platform file's
`tenants:` entry, or its profile's — never a union across directory levels).

Beside `effective_config` (not inside it), the default output, `--show-sources`
and `--all` add `custom_alerts`: every element of that list as the exporter
judges it, from `da-guard served-values` — `{index, name, recipe, status}`,
plus `reason` / `recipe_id` when the exporter gives one; status is `served`,
`disabled`, `rejected` or `duplicate` (#3230). It is omitted for a tenant
with no element; `custom_alerts_error` says why a tenant's list does not
decode. served-values runs once, only for those three modes; when it fails
(or the da-guard predates the field) a WARN line goes to stderr, the field
is omitted, and the exit code is unchanged.

`--what-if` asks Go about "these bytes at this tree path": `--replaces F`
stands the draft in for the tree file F, `--at P` adds it as a new file at P
(`da-guard effective --overlay`); the two runs' effective configs are
diffed and both merged hashes are Go's.

Output: JSON or YAML.
"""
import argparse
import copy
import os
import sys
from pathlib import Path
from typing import Any

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout, find_project_root  # noqa: E402
from _lib_confd import duplicate_declarations, tenant_declarations  # noqa: E402  (#2049)
from _lib_exitcodes import EXIT_CALLER_ERROR, EXIT_VIOLATION  # noqa: E402
from _lib_io import (  # noqa: E402  (#1789; #3276)
    YamlFileError, classify_path, exit_on_output_write_error, output_write, safe_label)
import _lib_parse_cache  # noqa: E402  (ADR-036 step 4: one parse-tree document per run)
from _lib_parse_tree import ParseTreeError  # noqa: E402
from _lib_tenant_values import (  # noqa: E402
    DA_GUARD_PREFIX,
    DaGuardError,
    DaGuardNotFoundError,
    EffectiveRun,
    ParseFailedError,
    run_effective,
    load_served_tree,
    print_load_error,
)

try:
    import yaml  # output only (--format yaml): never a conf.d read
except ImportError:
    yaml = None

# #1549: the reason a tenant of a refused tree has no merged_hash. Fixed
# words that name no file: the cause, with da-guard's own lines, is on stderr.
# (A tree whose load skips or cannot read a file is the shared conf.d refusal
# since #3206, before anything is described.)
MERGED_HASH_TREE_REFUSED = ("the exporter rejects this whole tree (a tenant is declared in more "
                            "than one file), so da-guard effective gives no merged_hash (see stderr)")


class DescribeError(Exception):
    """No tenant of the tree can be described: da-guard is missing or fails,
    or refuses the tree. `lines` is what to print on stderr (the first is the
    reason, the rest da-guard's own lines)."""

    def __init__(self, lines: "list[str]"):
        self.lines = lines
        super().__init__(lines[0] if lines else "da-guard effective failed")


def _load_error_lines(exc: Exception) -> "list[str]":
    """`print_load_error`'s lines for `exc`, without the `ERROR: ` prefix of
    the first (each caller words its own head)."""
    import io
    buf = io.StringIO()
    print_load_error(exc, buf)
    lines = buf.getvalue().splitlines()
    if lines:
        lines[0] = lines[0].removeprefix("ERROR: ")
    return lines


def _effective_run(conf_d: Path, overlay: "tuple[str, Path] | None" = None) -> EffectiveRun:
    """`da-guard effective` over `conf_d` (DaGuardError / DaGuardNotFoundError
    pass through)."""
    return run_effective(conf_d, overlay)


class ConfDScanner:
    """One conf.d tree as `da-guard effective` resolves it.

    `tenants` maps each tenant id da-guard resolves to its
    `_lib_tenant_values.TenantEffective`; `duplicates` every tenant more
    than one file declares (`_lib_confd.tenant_declarations`, read through
    `da-guard parse-tree`), each with its sorted carrier labels; `refused` is
    set — and `tenants` empty — when the exporter rejects the whole tree
    because of such a duplicate; `declared` every tenant id some file
    declares. (A file the exporter's load skips or cannot read is a path
    da-guard validation names: the shared conf.d refusal, #3206, ended the
    run before the scan.)

    Raises DescribeError when da-guard is missing, fails, or refuses the tree
    for any other reason."""

    def __init__(self, conf_d: Path):
        self.conf_d = Path(conf_d).resolve()
        self.tenants: dict = {}
        self.duplicates: dict = {}
        self.declared: "list[str]" = []
        self.refused: "str | None" = None
        self.stderr_lines: "list[str]" = []
        # #1674: every file no plane reads (a defaults carrier its
        # directory's chain does not select), root-relative → parse-tree's
        # `not_read` reason. Named by `main`, not here: tenant_verify scans too.
        self.not_read: "dict[str, str]" = {}
        dup_error: "DaGuardError | None" = None
        with _lib_parse_cache.session():
            try:
                run = _effective_run(self.conf_d)
            except DaGuardNotFoundError as exc:
                raise DescribeError(_load_error_lines(exc)) from exc
            except DaGuardError as exc:
                if exc.binary_fault or exc.returncode != 2 or "duplicate tenant" not in exc.stderr:
                    raise DescribeError(_load_error_lines(exc)) from exc
                dup_error, run = exc, None
            try:
                declared, _unread = tenant_declarations(self.conf_d)
                pt_tree = _lib_parse_cache.reader_for(self.conf_d).tree()
                self.not_read = dict(sorted(pt_tree.not_read.items()))
            except ParseTreeError as exc:
                raise DescribeError([f"da-guard parse-tree failed: {exc}"]) from exc
            self.duplicates = duplicate_declarations(declared)
            self.declared = sorted(declared)
            if run is None:
                if not self.duplicates:  # Go names a duplicate the Python predicate does not
                    raise DescribeError(_load_error_lines(dup_error))
                self.refused = (
                    f"the exporter rejects the whole conf.d {self.conf_d}: "
                    f"{len(self.duplicates)} tenant(s) declared in more than one file "
                    f"({', '.join(sorted(self.duplicates))})")
                self.stderr_lines = dup_error.stderr_lines
                return
            self.tenants = dict(run.tenants)
            self.stderr_lines = list(run.stderr_lines)

    def tenant_ids(self) -> "list[str]":
        """Every tenant this tree names: resolved, duplicated, or — when the
        tree is refused — declared."""
        return sorted(set(self.tenants) | set(self.duplicates)
                      | (set(self.declared) if self.refused else set()))

    def duplicate_error(self, tenant_id: Any) -> "str | None":
        """Why `tenant_id` cannot be described when more than one carrier
        declares it; None when it is declared at most once (#2049).

        ⚠️ The verdict is CONDITIONAL on purpose. A carrier here is a file
        whose `tenants:` key names the tenant (the #1942 rule, see
        `_lib_confd.tenant_declarations`); a file the exporter's full decode
        rejects declares nothing there, so Go can resolve this tenant from
        the other file. Refusing is still right — it matches validate_config
        and beats picking a value."""
        files = self.duplicates.get(tenant_id)
        if not files:
            return None
        return (f"duplicate tenant ID '{tenant_id}': declared in {len(files)} "
                f"files under {self.conf_d}: {', '.join(files)}. If every one "
                f"of these files parses, the exporter rejects this tenant (a "
                f"full load rejects the whole conf.d); a file its full decode "
                f"rejects declares nothing there. Not describing any of "
                f"them — run validate-config, and keep the tenant in "
                f"exactly one file.")

    def effective_config(self, tenant_id: str) -> dict:
        """The tenant's effective config, da-guard effective's, as is (a copy)."""
        if tenant_id not in self.tenants:
            raise KeyError(f"Tenant '{tenant_id}' not found in {self.conf_d}")
        return copy.deepcopy(self.tenants[tenant_id].effective_config)

    def merged_hash(self, tenant_id: str) -> str:
        """The tenant's merged_hash — da-guard effective's (#1549)."""
        return self.tenants[tenant_id].merged_hash

    def platform_overlay(self, tenant_id: str) -> list:
        """`[{file, keys}]` — Go's `platform_overlay` (#2019)."""
        return list(self.tenants[tenant_id].platform_overlay)

    def profile_overlay(self, tenant_id: str) -> list:
        """`[{profile, file, keys}]` — Go's `profile_overlay` (#2117)."""
        return list(self.tenants[tenant_id].profile_overlay)

    def source_info(self, tenant_id: str) -> dict:
        """Source traceability for a tenant — every field da-guard effective's."""
        if tenant_id not in self.tenants:
            raise KeyError(f"Tenant '{tenant_id}' not found")
        te = self.tenants[tenant_id]
        info = {
            "tenant_id": tenant_id,
            "source_file": te.source_file,
            "source_hash": te.source_hash,
            "merged_hash": self.merged_hash(tenant_id),
        }
        info["defaults_chain"] = list(te.defaults_chain)
        # Omitted when empty, as Go's `platform_overlay,omitempty`.
        if te.platform_overlay:
            info["platform_overlay"] = self.platform_overlay(tenant_id)
        # Omitted when empty, as Go's `profile_overlay,omitempty` (#2117).
        if te.profile_overlay:
            info["profile_overlay"] = self.profile_overlay(tenant_id)
        info["effective_config"] = self.effective_config(tenant_id)
        return info

    def diff_tenants(self, id_a: str, id_b: str) -> dict:
        """Compare effective configs of two tenants."""
        eff_a = self.effective_config(id_a)
        eff_b = self.effective_config(id_b)

        only_a, only_b, different = {}, {}, {}
        for k in sorted(set(eff_a) | set(eff_b)):
            if k not in eff_b:
                only_a[k] = eff_a[k]
            elif k not in eff_a:
                only_b[k] = eff_b[k]
            elif eff_a[k] != eff_b[k]:
                different[k] = {"a": eff_a[k], "b": eff_b[k]}

        return {
            "tenant_a": id_a,
            "tenant_b": id_b,
            f"only_in_{id_a}": only_a,
            f"only_in_{id_b}": only_b,
            "different": different,
        }


def warn_not_read(scanner: "ConfDScanner") -> None:
    """One WARN line per file no plane reads (#1674): a defaults carrier its
    directory's chain does not select. describe_tenant and tenant_verify
    both say it, in these words (the custom-alert loader's walk did until
    #3230 removed it)."""
    for rel, why in scanner.not_read.items():
        print(f"WARN: {safe_label(rel)} is ignored on every plane (#1674): da-guard "
              f"parse-tree does not read it ({safe_label(why)})", file=sys.stderr)


# ---------------------------------------------------------------------------
# custom_alerts: the exporter's judgement of each `_custom_alerts` element
# ---------------------------------------------------------------------------

def custom_alerts_fields(conf_d: Path,
                         declared: "dict[str, Any] | None" = None) -> "dict[str, dict] | None":
    """`{tenant_id: fields}` — what the default output, `--show-sources` and
    `--all` add beside `effective_config` (#3230), from one `da-guard
    served-values` run over `conf_d` (`partial`: a file the load skips leaves
    the rest of the tree): `custom_alerts`, one `{index, name, recipe,
    status[, reason][, recipe_id]}` per element of the tenant's
    `_custom_alerts` as the exporter judges it, and `custom_alerts_error`
    when the list does not decode. A tenant with neither has no entry —
    unless `declared` (tenant → its effective config) gives it a
    `_custom_alerts` the exporter takes nothing from: then `custom_alerts`
    is `[]`, so "declared, none served" reads apart from "not declared".

    None, with one WARN line on stderr, when served-values fails; a tenant
    whose da-guard is older than the field has no entry, with one WARN line.
    The caller omits what is missing; the exit code does not change.

    ⛔ Not a `ConfDScanner` method on purpose: `tenant_verify` calls
    `source_info` per tenant, and must not pay a second da-guard run for a
    field it does not read."""
    try:
        tree = load_served_tree(conf_d, partial=True)
    except (DaGuardError, DaGuardNotFoundError, YamlFileError) as exc:
        # YamlFileError: `partial` makes a file the load skips no error, but
        # whatever served-values raises only costs this field (#1654 lint).
        lines = (_load_error_lines(exc)
                 if isinstance(exc, (DaGuardError, DaGuardNotFoundError, ParseFailedError))
                 else [str(exc)])
        print(f"WARN: custom_alerts not shown — da-guard served-values failed: "
              f"{safe_label(lines[0] if lines else str(exc))}", file=sys.stderr)
        # #1822: a caller that goes on after the failure passes da-guard's
        # own lines on whole.
        for line in lines[1:]:
            print(line if line.startswith(DA_GUARD_PREFIX) else f"{DA_GUARD_PREFIX}{safe_label(line)}",
                  file=sys.stderr)
        return None
    out: "dict[str, dict]" = {}
    stale = False
    for tid, tv in tree.tenants.items():
        fields: dict = {}
        if tv.custom_alerts is None:
            stale = True
        elif not tv.custom_alerts:
            if (declared or {}).get(tid, {}).get("_custom_alerts") and tv.custom_alerts_error is None:
                fields["custom_alerts"] = []
        else:
            rows = []
            for e in tv.custom_alerts:
                row = {"index": e.index, "name": e.spec.get("name"),
                       "recipe": e.spec.get("recipe"), "status": e.status}
                if e.reason is not None:
                    row["reason"] = e.reason
                if e.recipe_id is not None:
                    row["recipe_id"] = e.recipe_id
                rows.append(row)
            fields["custom_alerts"] = rows
        if tv.custom_alerts_error is not None:
            fields["custom_alerts_error"] = tv.custom_alerts_error
        if fields:
            out[tid] = fields
    if stale:
        print("WARN: custom_alerts not shown — this da-guard's served-values gives no "
              "custom_alerts (it is older than this tool): upgrade or rebuild it",
              file=sys.stderr)
    return out


# ---------------------------------------------------------------------------
# --what-if: these bytes at this tree path, judged by Go
# ---------------------------------------------------------------------------

class WhatIfError(Exception):
    """A `--what-if` the simulation cannot answer (a caller error)."""


def _same_file(a: Path, b: Path) -> bool:
    """One file on disk — a hard link is one too (#2097 blind review 2, D)."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.abspath(a) == os.path.abspath(b)


def tree_rel(conf_d: Path, given: "str | os.PathLike[str]") -> str:
    """`given` as a path relative to `conf_d` (already resolved), '/'-joined:
    its directory resolved, its own name never followed — a link in the tree
    is the entry it is, as the exporter's walk sees it (#1967). Raises
    WhatIfError for a path outside the tree."""
    p = Path(given).absolute()
    holder = Path(os.path.realpath(p.parent))
    try:
        rel = (holder / p.name).relative_to(conf_d)
    except ValueError:
        raise WhatIfError(f"{given} is not inside the scanned tree {conf_d}") from None
    if not rel.parts:
        raise WhatIfError(f"{given} is the scanned tree {conf_d} itself, not a file in it")
    return rel.as_posix()


def what_if_result(scanner: ConfDScanner, tid: str, draft: Path,
                   replaces: "str | None" = None, at: "str | None" = None) -> dict:
    """`--what-if`: `tid`'s effective config with `draft`'s bytes at a tree
    path, diffed against the tree `scanner` read. Exactly one of
    `replaces` / `at` (each a path as the user gave it):

      - "substitute" (`replaces`): the draft stands in for that file of the
        scanned tree (#2097) — it must be a file the scan lists, and not the
        draft itself (or a hard link to it): that would compare the file
        with itself and never report a reload (fail-open, #2097 F1).
      - "create" (`at`): the draft is a new file at that path, which must not
        exist; its directory must be one the exporter's walk descends.

    Both sides are `da-guard effective` runs — the scanned tree, and the tree
    with `--overlay <path>=<draft>` — so every rule the exporter applies to
    those bytes at that path (the chain, the platform and profile overlays,
    a file it refuses) is Go's. Both merged hashes are Go's.

    Raises WhatIfError when Go refuses the overlay, when the draft makes the
    exporter skip a file it read before (it does not decode there), or when
    the tree with the draft no longer resolves `tid`."""
    if (replaces is None) == (at is None):
        raise WhatIfError("give exactly one of --replaces / --at")
    conf_d = scanner.conf_d
    if replaces is not None:
        rel = tree_rel(conf_d, replaces)
        target = conf_d / rel
        # #3276: the shared judgement of a caller-given path, in this tool's
        # words (a WhatIfError, the document the --what-if mode answers with).
        problem = classify_path(target, "file", flag="--replaces")
        if problem is not None:
            raise WhatIfError(
                f"--replaces {replaces} is not a file of the scanned tree {conf_d} "
                f"(it must name an existing config file the scan lists): {problem.message}")
        if _same_file(target, draft):
            raise WhatIfError(
                f"--what-if {draft} and --replaces {replaces} are the same file: "
                f"that compares the file with itself. Save the modified content as a "
                f"separate copy and pass the copy to --what-if")
        substitution_type, flag = "substitute", "--replaces"
    else:
        rel = tree_rel(conf_d, at)
        target = conf_d / rel
        if os.path.lexists(target):
            raise WhatIfError(
                f"--at {at} already exists in the scanned tree: to stand the draft in "
                f"for it, pass --replaces {at}")
        substitution_type, flag = "create", "--at"

    try:
        run = _effective_run(conf_d, (rel, draft))
    except (DaGuardNotFoundError, DaGuardError) as exc:
        lines = _load_error_lines(exc)
        raise WhatIfError("\n".join(
            [f"da-guard effective refuses the tree with the --what-if file {draft} at "
             f"{flag} {rel}: {lines[0] if lines else exc}"] + lines[1:])) from exc
    # The tree on disk passed the shared refusal (#3206): every file the
    # overlaid run drops is the draft's doing.
    new_parse_failed = sorted(run.parse_failed)
    new_unreadable = sorted(u.file for u in run.unreadable)
    if new_parse_failed or new_unreadable:
        what = ", ".join(new_parse_failed + new_unreadable)
        raise WhatIfError("\n".join(
            [f"with the --what-if file {draft} at {flag} {rel}, the exporter's load skips "
             f"file(s) it reads today: {what} (the draft does not decode there)"]
            + [f"{DA_GUARD_PREFIX}{safe_label(line)}" for line in run.stderr_lines]))
    if tid not in run.tenants:
        raise WhatIfError(
            f"with the --what-if file {draft} at {flag} {rel}, da-guard effective resolves "
            f"no tenant '{tid}' (the draft no longer declares it, or declares it "
            f"in a file the exporter refuses)")

    baseline = scanner.tenants[tid]
    simulated = run.tenants[tid]
    base_eff, sim_eff = baseline.effective_config, simulated.effective_config
    only_baseline: dict = {}
    only_what_if: dict = {}
    changed: dict = {}
    for k in sorted(set(base_eff) | set(sim_eff)):
        if k not in sim_eff:
            only_baseline[k] = base_eff[k]
        elif k not in base_eff:
            only_what_if[k] = sim_eff[k]
        elif base_eff[k] != sim_eff[k]:
            changed[k] = {"baseline": base_eff[k], "what_if": sim_eff[k]}

    hash_changed = baseline.merged_hash != simulated.merged_hash
    return {
        "tenant_id": tid,
        "what_if_file": str(draft),
        "substitution_type": substitution_type,
        # The scanned file the draft stood in for (substitute), or the path
        # it was added at (create); the other is None.
        "replaces": str(target) if substitution_type == "substitute" else None,
        "at": str(target) if substitution_type == "create" else None,
        "baseline_merged_hash": baseline.merged_hash,
        "what_if_merged_hash": simulated.merged_hash,
        "merged_hash_changed": hash_changed,
        "would_trigger_reload": hash_changed,  # per ADR-017 dual-hash logic
        "removed_keys": only_baseline,
        "added_keys": only_what_if,
        "changed_keys": changed,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_lines(lines: "list[str]", head: str) -> None:
    """`head` + the first line, then the rest behind da-guard's prefix."""
    if lines:
        print(f"{head}{lines[0]}", file=sys.stderr)
        for line in lines[1:]:
            print(line if line.startswith(DA_GUARD_PREFIX) else f"{DA_GUARD_PREFIX}{line}",
                  file=sys.stderr)


@exit_on_output_write_error
def main() -> None:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Describe effective tenant config as da-guard effective resolves it (ADR-017, ADR-036).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "tenant_id", nargs="?",
        help="Tenant ID to describe (omit with --all)",
    )
    parser.add_argument(
        "--conf-d", "-c", type=str, default=None,
        help="Path to conf.d/ directory (default: auto-detect from repo root)",
    )
    parser.add_argument(
        "--show-sources", "-s", action="store_true",
        help="Show inheritance chain sources for each field. This output, the default "
             "one and --all also carry custom_alerts beside effective_config: each "
             "_custom_alerts element as the exporter judges it (da-guard served-values)",
    )
    parser.add_argument(
        "--diff", "-d", type=str, default=None, metavar="TENANT_ID_2",
        help="Diff effective config against another tenant",
    )
    parser.add_argument(
        "--what-if", "-w", type=str, default=None, metavar="PATH",
        help="Simulate a draft file: the tenant's effective config and merged_hash with "
             "PATH's bytes at a tree path, diffed against the tree as it is. Needs "
             "--replaces or --at (rc 2 without either)",
    )
    parser.add_argument(
        "--replaces", type=str, default=None, metavar="PATH",
        help="With --what-if: the conf.d file the draft stands in for (substitute). "
             "PATH must be a file the scan lists, and must not be the --what-if file "
             "itself (rc 2)",
    )
    parser.add_argument(
        "--at", type=str, default=None, metavar="PATH",
        help="With --what-if: the conf.d path the draft is added at as a new file "
             "(create), e.g. <tenant dir>/_defaults.yaml. PATH must not exist, and "
             "its directory must be one the exporter's walk reads (rc 2)",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Dump all tenants' effective configs",
    )
    parser.add_argument(
        "--output", "-o", type=str, default=None,
        help="Output file (default: stdout). Used with --all.",
    )
    parser.add_argument(
        "--format", "-f", choices=["json", "yaml"], default="json",
        help="Output format (default: json)",
    )
    args = parser.parse_args()
    # #2097 blind review 2 (E): before any mode branch — `--all` / `--diff`
    # returned rc 0 with `--replaces` silently ignored. `is not None`, not
    # truthiness: `--replaces ''` was taken as "not given" (#2509 blind review 3),
    # and so was `--what-if ''` (blind review 4).
    if args.what_if == "":
        parser.error("--what-if needs a path (got an empty string)")
    for flag, value in (("--replaces", args.replaces), ("--at", args.at)):
        if value is not None and args.what_if is None:
            parser.error(f"{flag} requires --what-if")
        if value == "":
            parser.error(f"{flag} needs a path (got an empty string)")
    if args.replaces is not None and args.at is not None:
        parser.error("--replaces and --at cannot be used together: the draft either stands "
                     "in for a file of the tree (--replaces) or is added as a new one (--at)")
    # #2739: the same shape as (E) above, one flag over — the `--all` /
    # `--diff` branches run before the what-if one.
    if args.what_if is not None and (args.all or args.diff is not None):
        extra = "".join(f" / {f}" for f, v in (("--replaces", args.replaces), ("--at", args.at))
                        if v is not None)
        mode = "--all" if args.all else "--diff"
        parser.error(f"--what-if{extra} cannot be used together with {mode}: the simulation "
                     f"describes one tenant; drop {mode}, or run the two separately")

    # Resolve conf.d path
    if args.conf_d:
        conf_d = Path(args.conf_d)
    else:
        # ⛔ Was `.parent` four times (#1494). This file ships flattened into
        # the image (`/opt/da-tools/describe_tenant.py`, three ancestors), and
        # a `.parent` chain does not raise past the top — it SATURATES at the
        # filesystem root. Search for the directory instead of counting levels.
        # ⛔ BOUNDED to THIS tree's root (`find_project_root`, #2845): an
        # unbounded walk bound to a `conf.d` belonging to somebody else.
        here = Path(__file__).resolve().parent
        repo_root = find_project_root(here)
        conf_d = None
        if repo_root is not None:
            if (repo_root / "conf.d").is_dir():
                conf_d = repo_root / "conf.d"
            else:
                fixtures = repo_root / "tests" / "fixtures"
                for fixture_dir in sorted(fixtures.glob("synthetic-*")):
                    if (fixture_dir / "conf.d").is_dir():
                        conf_d = fixture_dir / "conf.d"
                        break
        if conf_d is None:
            # Non-existent on purpose: the check below reports it with the
            # `--conf-d` remedy.
            conf_d = (repo_root or here) / "conf.d"

    # #3276: the shared judgement of a caller-given path (or the one found
    # for the caller, named without the flag it did not give).
    problem = classify_path(conf_d, "dir", flag="--conf-d" if args.conf_d else None)
    if problem is not None:
        word = "not found" if problem.missing else "not usable"
        print(f"❌ conf.d/ {word}: {problem.message}", file=sys.stderr)
        print(f"   Use --conf-d to specify the path.", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    if args.what_if is not None and args.replaces is None and args.at is None:
        # #2906: the implicit forms (a `.`-prefixed draft inserted at its
        # directory's level, a draft outside conf.d appended as the highest
        # level) are gone — the draft's place is said, and Go judges it.
        hint = ""
        given = Path(args.what_if)
        # #3276: a hint only — `is_file()` raised on a path under a parent
        # the caller cannot enter (3.13), ending in a traceback.
        if classify_path(given, "file") is None:
            try:
                rel = tree_rel(conf_d.resolve(), given)
                hint = (f". {args.what_if} is in the scanned tree: to try an edit of "
                        f"{conf_d.resolve() / rel}, save the modified content as a copy and "
                        f"run --what-if <copy> --replaces {conf_d.resolve() / rel}")
            except WhatIfError:
                pass
        parser.error("--what-if needs --replaces <file> (the tree file the draft stands in "
                     "for) or --at <path> (where the draft is added as a new file, e.g. "
                     f"--at <tenant dir>/_defaults.yaml){hint}")

    draft = None
    if args.what_if is not None:
        draft = Path(args.what_if).resolve()
        problem = classify_path(draft, "file", flag="--what-if")  # #3276
        if problem is not None:
            word = "not found" if problem.missing else "not usable"
            print(f"❌ --what-if file {word}: {problem.message}", file=sys.stderr)
            sys.exit(EXIT_CALLER_ERROR)

    try:
        scanner = ConfDScanner(conf_d)
    except DescribeError as exc:
        _print_lines(exc.lines, "❌ ")
        sys.exit(EXIT_CALLER_ERROR)
    print(f"📂 Scanned {conf_d}: {len(scanner.tenants)} tenants (da-guard effective)",
          file=sys.stderr)
    warn_not_read(scanner)

    def _output(data: Any) -> str:
        import json
        if args.format == "yaml":
            if yaml:
                return yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)
            print("⚠️  PyYAML not installed — falling back to JSON output "
                  "(pip install pyyaml)", file=sys.stderr)
        return json.dumps(data, indent=2, ensure_ascii=False)

    # #2049: THE checkpoint for "this tenant is declared by more than one
    # carrier". Every mode that names a tenant (default, --show-sources,
    # --diff for BOTH sides, --what-if) passes through it before anything
    # is computed; --all asks `duplicate_error` per tenant. Exit code is
    # EXIT_VIOLATION, not the not-found EXIT_CALLER_ERROR: the invocation
    # and the tree are readable, and the finding is one the user must fix
    # in conf.d — the code validate_config's `tenant_uniqueness` gives for
    # the same state. It also keeps "ambiguous" apart from "absent".
    def _refuse_duplicate(t: Any, tree: bool = True) -> None:
        msg = scanner.duplicate_error(t)
        if msg is not None:
            print(f"❌ {msg}", file=sys.stderr)
            sys.exit(EXIT_VIOLATION)
        if tree and scanner.refused is not None:
            # The exporter serves no tenant of a tree with a duplicate.
            print(f"❌ tenant '{t}' is not described: {scanner.refused}.", file=sys.stderr)
            _print_lines(scanner.stderr_lines, DA_GUARD_PREFIX)
            sys.exit(EXIT_VIOLATION)

    def _require(t: str) -> None:
        _refuse_duplicate(t)
        if t not in scanner.tenants:
            avail = ", ".join(sorted(scanner.tenants)[:10])
            print(f"❌ Tenant '{t}' not found. Available: {avail}...", file=sys.stderr)
            sys.exit(EXIT_CALLER_ERROR)

    # --all mode
    if args.all:
        result = {}
        duplicated = 0
        for tid in scanner.tenant_ids():
            msg = scanner.duplicate_error(tid)
            if msg is not None:
                # Reported, not described; the other tenants still are.
                print(f"❌ {msg}", file=sys.stderr)
                duplicated += 1
                continue
            if tid in scanner.tenants:
                result[tid] = scanner.source_info(tid)
        if result:
            # Beside effective_config, never inside it (#3230).
            declared = {t: scanner.tenants[t].effective_config for t in result}
            for tid, fields in (custom_alerts_fields(scanner.conf_d, declared) or {}).items():
                if tid in result:
                    result[tid].update(fields)
        if scanner.refused is not None:
            print(f"❌ no other tenant is described: {scanner.refused}.", file=sys.stderr)
        out = _output(result)
        if args.output:
            with output_write(args.output, flag="-o/--output"):
                Path(args.output).write_text(out, encoding="utf-8", newline="\n")
            print(f"✅ Written {len(result)} tenants to {args.output}", file=sys.stderr)
        else:
            print(out)
        if duplicated:
            print(f"❌ {duplicated} tenant(s) not described: declared in more "
                  f"than one file (see above).", file=sys.stderr)
            sys.exit(EXIT_VIOLATION)
        return

    if not args.tenant_id:
        parser.error("tenant_id is required (or use --all)")

    tid = args.tenant_id
    if args.diff:
        # Both sides' duplicate first: the refused tree is said once.
        _refuse_duplicate(tid, tree=False)
        _refuse_duplicate(args.diff, tree=False)
    _require(tid)

    # --diff mode
    if args.diff:
        _require(args.diff)
        print(_output(scanner.diff_tenants(tid, args.diff)))
        return

    # --what-if mode: these bytes at this tree path
    if draft is not None:
        try:
            result = what_if_result(scanner, tid, draft, replaces=args.replaces, at=args.at)
        except WhatIfError as e:
            print(f"❌ {e}", file=sys.stderr)
            sys.exit(EXIT_CALLER_ERROR)
        print(_output(result))
        return

    # Default: show effective config
    if args.show_sources:
        result = scanner.source_info(tid)
    else:
        result = {"tenant_id": tid}
        overlay = scanner.platform_overlay(tid)
        if overlay:
            result["platform_overlay"] = overlay
        profile = scanner.profile_overlay(tid)
        if profile:
            result["profile_overlay"] = profile
        result["effective_config"] = scanner.effective_config(tid)
    # Beside effective_config, never inside it: that one is Go's value as is (#3230).
    declared = {tid: scanner.tenants[tid].effective_config}
    result.update((custom_alerts_fields(scanner.conf_d, declared) or {}).get(tid, {}))
    print(_output(result))


if __name__ == "__main__":
    main()
