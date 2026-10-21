"""conf.d tree walk + `_custom_alerts` inheritance as the compiler read it
before #3042 (ADR-024, #741 S2).

⚠️ The compiler no longer reads this: since #3042 it compiles the exporter's
own reading of each tenant's list (`entries.py`, da-guard served-values). What
is left here is the tree walk `describe_tenant` still shows (a UNION across
the tenant's directory levels: a recipe in a directory's `_defaults.yaml` plus
the tenant's own) until it moves to the exporter's reading too (#3042 B2), and
`CustomAlertConfigError`, the compiler's error type. The exporter serves
neither a `_defaults.yaml` top-level `_custom_alerts` nor the union: a tenant
gets one list, its own, its root platform file's `tenants:` entry, or its
profile's.

ADR-036 step 4 (#2906): the walk reads each conf.d file through `da-guard
parse-tree` (`_lib_parse_cache.ConfdReader`), never PyYAML, and which
defaults carrier it reads is that reader's `not_read` verdict.
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from _lib_confd import (  # noqa: E402  (#1588 shared name predicates)
    is_defaults_name,
    is_hidden_name,
    is_dir_symlink,
    defaults_files_in,
    list_config_tree,
    unusable_reason,
    warn_dir_symlink_once,
)



class CustomAlertConfigError(ValueError):
    """A declaration tree is invalid (rejected at compile time)."""


#: Carriers `_dir_defaults_alerts` has named as not read in this process.
_NOT_READ_WARNED: set = set()


def _is_dir(p: Path) -> bool:
    """`Path.is_dir()` that answers False instead of raising.

    Mirrors `os.walk`'s dirs/files split, which is what the caller is asking
    about. An entry that cannot even be stat'ed is not in `dirs`, so False is
    the answer that keeps this pass and `_dir_defaults_alerts` disjoint."""
    try:
        return p.is_dir()
    except OSError:
        return False


def _file_record(origin: str, reason: str) -> dict:
    """The `file_errors` record shape, in ONE place.

    ⛔ Two callers now build these — the malformed-YAML quarantine below and
    the unusable-entry report in `collect_instances` (#1607). A second hand
    written literal is how a consumer ends up switching on a key one of them
    forgot, which is the #1911 shape reproduced inside its own fix."""
    return {"tenant": None, "origin": origin, "name": None, "reason": reason}


def _file_skip(origin: str, exc: Exception) -> dict:
    """Skip record for a conf.d FILE that could not be loaded (malformed YAML / control
    chars / bad encoding). Quarantined fail-soft (#1008 Part B): the YAML load happens
    OUTSIDE the per-recipe try, so without this one bad file (incl. a schema-check-skipped
    meta file) would crash the whole shared compile → block every tenant's PR."""
    return _file_record(origin, f"{type(exc).__name__}: {exc}")


def _dir_defaults_alerts(config_dir: Path, file_errors: List[dict],
                         reader) -> Dict[Path, List[dict]]:
    """Map each directory → its _defaults.yaml top-level `_custom_alerts` list. A
    _defaults.yaml that fails to load is quarantined into `file_errors` (#1008 Part B),
    not raised.

    *reader* (`_lib_parse_cache.ConfdReader` of *config_dir*) reads each
    carrier, and its `not_read` decides which carrier is read: one it names
    a reason for is not read (and is named on stderr), every other one is —
    a selection of this module's own could hand `read` a file parse-tree
    does not select (#2906 review)."""
    out: Dict[Path, List[dict]] = {}
    for root, dirs, files in os.walk(config_dir):
        # #2086: prune `.`-prefixed directories the way the exporter's walker
        # (and `list_config_tree`, which `collect_instances` reads through)
        # does. Without it a `_defaults.yaml` under `.snap/` or a ConfigMap
        # `..<timestamp>/` payload was loaded here — a broken one landed in
        # `file_errors` for a file no other plane reads. Same predicate as
        # the tenant pass, not a second local `startswith(".")`.
        dirs[:] = [d for d in dirs if not is_hidden_name(d)]
        # #1588: the carrier is matched by the shared predicate, not by a
        # literal name. `_DEFAULTS.YAML` used to be invisible here, so a
        # platform-level `_custom_alerts` list declared in it vanished for
        # EVERY tenant below it — silently, at rc=0, on the shipped tenant
        # self-service path.
        #
        # #1674 (B8): and ONE carrier per directory — the one the exporter's
        # chain and describe_tenant read (`select_defaults_carrier`). This
        # used to EXTEND across every spelling in a directory; a second
        # spelling is now a misconfiguration every plane names and none
        # reads, so a `_custom_alerts` list in it is ignored here too.
        #
        # ⛔ Which carrier that is, is parse-tree's `not_read` (#2906): a
        # carrier whose read fails (a dangling symlink, a permission error)
        # is not in it — its read raises below and it lands in `file_errors`,
        # and a readable `_defaults.yml` beside it is still read (blind
        # review of #1674).
        chosen_list = []
        for p in defaults_files_in(root, files):
            why = reader.not_read(p)
            if why is None:
                chosen_list.append(p)
            elif os.path.abspath(p) not in _NOT_READ_WARNED:
                # Once per file per process: describe_tenant runs this
                # loader over the tree it also reads (#1674 blind review).
                _NOT_READ_WARNED.add(os.path.abspath(p))
                print(f"WARN: {p.relative_to(config_dir).as_posix()} is ignored on every "
                      f"plane (#1674): da-guard parse-tree does not read it ({why}), "
                      f"so its _custom_alerts are not inherited", file=sys.stderr)
        # More than one only when parse-tree cannot read the others: their
        # read raises and quarantines them.
        for chosen in chosen_list:
            try:
                data = reader.read(chosen)
            except Exception as exc:  # noqa: BLE001 — malformed file quarantined, not fatal
                file_errors.append(_file_skip(chosen.relative_to(config_dir).as_posix(), exc))
                continue
            alerts = (data.get("_custom_alerts") if isinstance(data, dict) else None) or []
            if alerts:
                out.setdefault(Path(root).resolve(), []).extend(alerts)
    return out


def _inherited_for(leaf_dir: Path, config_dir: Path,
                   dir_alerts: Dict[Path, List[dict]]) -> List[dict]:
    """All ancestor _defaults.yaml _custom_alerts that apply to a leaf dir."""
    inherited: List[dict] = []
    d = leaf_dir.resolve()
    stop = config_dir.resolve()
    chain: List[Path] = []
    while True:
        chain.append(d)
        if d == stop or d.parent == d:
            break
        d = d.parent
    # platform (shallowest) first → domain → subdomain, so cap-count / ordering
    # is deterministic and platform policy reads first.
    for d in reversed(chain):
        inherited.extend(dir_alerts.get(d, []))
    return inherited


def collect_instances(config_dir: Path, reader=None,
                      ) -> Tuple[List[Tuple[str, dict, str, bool]], List[dict]]:
    """Return (triples, file_errors): (tenant, instance, origin, is_own) tuples for every
    effective decl, PLUS skip records for conf.d files that failed to load.

    origin is a human string (the file the instance was declared in) for error messages.
    is_own distinguishes a tenant's OWN declaration from an INHERITED platform/domain
    policy (the latter is vectorized + does NOT count toward the per-tenant cap). Inherited
    instances are attributed to each tenant they land on (that is what scope / effective-
    count mean). A file `da-guard parse-tree` can't read (malformed / control chars / bad
    encoding / a key written twice) is QUARANTINED into file_errors and skipped (#1008
    Part B), never raised — this load is outside the per-recipe fail-soft loop, so
    without it one bad file would crash the whole walk.

    *reader* is the `_lib_parse_cache.ConfdReader` of *config_dir* (the
    session's when None): every file is read through it (ADR-036 step 4,
    #2906). No usable da-guard raises `ParseTreeError` before any file is
    read — it is not a per-file quarantine.
    """
    import _lib_parse_cache  # lazy: the recipe-preview image imports this module for the error type only
    config_dir = Path(config_dir)
    if reader is None:
        reader = _lib_parse_cache.reader_for(config_dir)
    reader.require()
    file_errors: List[dict] = []
    dir_alerts = _dir_defaults_alerts(config_dir, file_errors, reader)
    triples: List[Tuple[str, dict, str, bool]] = []

    # ⚠️ Same third axis as `operator_generate`, and here it is louder:
    # this function's whole contract is that an unreadable file lands in
    # `file_errors` rather than vanishing (#1008 Part B). Measured on a tree
    # with a directory named `beta.yaml/` and a broken symlink `broken.yaml`,
    # `file_errors` went from TWO named records to EMPTY once `is_file()`
    # filtered them out before the `except` below could see them (#1607).
    #
    # ⚠️ That number said "four" until blind review re-ran it: four was the
    # count on a RICHER tree that also had two unusable entries under
    # `_custom_alerts/`. The description was trimmed to two shapes and the
    # number was not. Measured on the tree as described here: 774a699 → 2,
    # a821645 → 0, bccb39a → 2.
    #
    # ⛔ Reported through `unusable_reason`, NOT by dropping the filter and
    # letting `_load_yaml` raise. Both put a record in `file_errors`, but the
    # errno phrasing ("IsADirectoryError: [Errno 21] ...") is this reader's
    # own invention of an answer every other reader words differently — the
    # divergence `unusable_reason` exists to end. `entries` is listed ONCE so
    # the two passes cannot walk two different trees.
    # ⛔ `.yml` IS A TENANT CARRIER HERE NOW (#1603). Both call sites in this module
    # used to pass `suffixes=(".yaml",)`, so a tenant declared in `db-b.yml` was
    # invisible to the custom-alert compiler while the exporter was serving it —
    # `config_hierarchy.go` accepts both spellings. Measured on two trees whose
    # contents are byte-identical and differ only in the extension:
    #
    #     db-b.yaml  ->  1 shape, 6 rules, rc=0
    #     db-b.yml   ->  0 shapes, 0 rules, rc=0        <- before this change
    #     db-b.yml   ->  1 shape, 6 rules, rc=0         <- after
    #
    # rc=0 in every row: the tenant's self-service alerts (ADR-024 Capability B,
    # shipped in v2.9.0) simply did not exist for this reader, and nothing said so.
    # `list_config_tree` reads `CONFIG_SUFFIXES`, which is the exporter's set.
    #
    # ⛔ #2086: the tree is `list_config_tree`, NOT `rglob("*")`. `rglob`
    # descends `.`-prefixed directories, which the exporter's walker
    # (`pkg/config.ScanDirTree` / `confdname.IsScanned`) prunes. Measured on
    # `main` 42db84e4: a `.snap/acme.yaml` declaring a recipe compiled one
    # extra shape (2 vs 1) for a file the exporter never serves, and a
    # ConfigMap mount (`..<ts>/acme.yaml` + the `acme.yaml` symlink into it)
    # read every tenant twice → one false `duplicate custom-alert name` skip
    # per tenant. `listing.unusable` keeps the other half of the #1607
    # contract: what the walk could not read is NAMED, not dropped.
    #
    # ⛔ `sorted(listing.files)` — a `Path` sort — NOT `listing.files` as is.
    # The listing is ordered by POSIX string, where `a-b.yaml` sorts before
    # `a/x.yaml` ('-' < '/'); `Path` compares part by part and puts
    # `a/x.yaml` first, which is the order `sorted(rglob)` always produced.
    # Triple order decides the first-fit cap survivor below, so taking the
    # listing's order would flip which recipe a tenant over the cap keeps
    # and change the compiled pack (measured: `from_dir` → `from_dash`).
    listing = list_config_tree(config_dir)
    entries = sorted(listing.files)
    for bad in listing.unusable:
        # ⛔ Defaults carriers are skipped ONLY when `_dir_defaults_alerts`
        # can see them, and the dividing line is exactly `os.walk`'s: it
        # classifies by `is_dir()`, putting a directory in `dirs` and
        # everything else — a broken symlink included — in `files`.
        #
        # So a broken symlink named `_defaults.yaml` IS quarantined there and
        # must be skipped here, or one path yields two `file_errors` records
        # and the caveat line says "2" for one file — the exact double-count
        # `unusable_config_paths` was narrowed twice to avoid. A DIRECTORY
        # named `_defaults.yaml/` is NOT in `files`, so nobody else names it
        # and this pass must.
        #
        # ⚠️ The first version skipped both and left the directory case
        # silent, arguing the silence predated #1588. Blind review measured
        # it: true, and irrelevant — the commit claimed every reader names
        # what it drops, and this was the one path where that was false.
        if is_defaults_name(bad.name) and not _is_dir(bad):
            continue
        # ⛔ A DIRECTORY SYMLINK is not a quarantined recipe (#1972 blind
        # review F2). It is a subtree neither this walk nor the exporter
        # enters — kubelet's `team-a -> ..data/team-a` — so there is no
        # recipe in it to quarantine. Recording it in `file_errors` made it a
        # `tenant=None name=None` QUARANTINED row, and `--allow-empty` then
        # refused to write (rc 0 → 1) over a tree the exporter serves fine.
        # Named on stderr, in `describe_tenant`'s wording, once per process
        # (describe_tenant names it too), and not counted.
        if is_dir_symlink(bad):
            warn_dir_symlink_once(bad)
            continue
        file_errors.append(_file_record(
            bad.relative_to(config_dir).as_posix(), unusable_reason(bad)))
    # `entries` are regular, non-hidden files with either YAML spelling
    # (#1603) — `list_config_tree` already applied all three filters.
    for path in entries:
        if is_defaults_name(path.name):
            continue
        try:
            # Tenant ids are the keys' source text, as the exporter reads
            # them (#2216): `010:` is tenant "010", not 8.
            data = reader.read(path)
        except Exception as exc:  # noqa: BLE001 — malformed file quarantined, not fatal
            file_errors.append(_file_skip(path.relative_to(config_dir).as_posix(), exc))
            continue
        tenants = (data.get("tenants") if isinstance(data, dict) else None) or {}
        if not isinstance(tenants, dict):
            continue
        inherited = _inherited_for(path.parent, config_dir, dir_alerts)
        for tenant, cfg in tenants.items():
            if not isinstance(cfg, dict):
                continue
            own = cfg.get("_custom_alerts") or []
            rel = path.relative_to(config_dir).as_posix()
            for inst in inherited:
                triples.append((tenant, inst, f"{rel} (inherited _defaults.yaml)", False))
            for inst in own:
                triples.append((tenant, inst, rel, True))
    return triples, file_errors


_NOTICE_TENANT_SAMPLE = 10   # max tenant names per lifecycle notice (CI-log readability)


def _summarize_tenants(tenants) -> str:
    """`<count> tenant(s) (<=N names[, and M more])` for a lifecycle notice line.

    A deprecated/eol SHARED recipe can be declared by hundreds of tenants (sharing
    is the whole point of vectorization); joining them all makes a multi-kilobyte
    single-line CI warning that some log collectors truncate mid-line. Lead with
    the actionable COUNT, then a bounded sample of names. Full per-tenant
    enumeration belongs in a metric/dashboard, not a log line.
    """
    names = sorted(tenants)
    shown = ", ".join(names[:_NOTICE_TENANT_SAMPLE])
    if len(names) > _NOTICE_TENANT_SAMPLE:
        shown += f", and {len(names) - _NOTICE_TENANT_SAMPLE} more"
    return f"{len(names)} tenant(s) ({shown})"
