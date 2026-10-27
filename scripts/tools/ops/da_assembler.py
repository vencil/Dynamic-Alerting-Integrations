#!/usr/bin/env python3
"""da-assembler-controller — Lightweight ThresholdConfig CRD → YAML renderer.

Watches ThresholdConfig custom resources across namespaces and renders each
into a YAML file under --config-dir that threshold-exporter hot-reloads.

This is NOT a full Operator — it does not manage the exporter lifecycle, does
not auto-scale, and does not compete with GitOps.  It does exactly one thing:
CRD → YAML translation.

Usage:
    da_assembler.py --config-dir /etc/threshold-exporter/conf.d   # watch + reconcile
    da_assembler.py --config-dir ./build/config-dir --once         # one-shot render
    da_assembler.py --config-dir ./build/config-dir --dry-run      # preview without writing
    da_assembler.py --render-cr example.yaml --config-dir ./out    # render a single CR file

Prerequisites:
    pip install kubernetes pyyaml
    (or run inside a pod with ServiceAccount + RBAC)
    --render-cr also needs the da-crdecode binary (#2476): on $PATH, or
    named by $DA_CRDECODE_BINARY. Build it with
    `go build ./cmd/da-crdecode` in components/threshold-exporter/app
    (`make assembler-render` does).
    Every mode also needs da-guard (ADR-036 §2, #2906, #3206): on $PATH, or
    named by $DA_GUARD_BINARY. Only Go writes conf.d: the CR is sent to
    `da-guard write` as data (each value with its JSON type, the header a
    comment), which writes the file — replacing one already there whole —
    or deletes it when the CR is deleted, after reading it back in the
    tree. A CR whose name is not a Kubernetes object name, whose tenant id
    is not one (ADR-035), whose `defaults` value is a sequence, a mapping or
    an int past float64's range (a string or a bool is written as it is: the
    exporter refuses that key alone when it is no number, #3250), or that
    da-guard refuses (a path no reader reads) is not
    written — `--render-cr` exits 2 naming it, the controller logs it, sets
    the CR's status to Error and goes on. A new file is mode 0600 (da-guard
    write's), a replaced one keeps its mode.
    And an existing --config-dir is a tree this writes INTO (#3206): it
    must first pass the shared conf.d refusal — any path da-guard
    validation names. `--render-cr` then exits 2 naming every such path,
    nothing written; the controller judges the tree afresh once per
    reconcile cycle (one `--once` pass, one watch event), logs every such
    path as ERROR, skips that cycle's writes and deletes (each CR's status
    is Error) and keeps running; `--once` then exits 2. The output
    directory is judged AS the tree: a --config-dir inside a larger conf.d
    is judged as its own tree (pass the conf.d root to have it all judged).
    ⚠️ Known gaps, left to #3318 (the controller redesign): a watch event
    skipped during a refused cycle (a write, or a DELETED's removal) is not
    replayed — it waits for the CR's next event or a restart's initial
    sync, and a deleted CR's file stays until removed by hand; and a watch
    that ends with 410 Gone is not re-listed (it reconnects from the same
    resourceVersion, as before this change).
"""

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))  # Repo subdir layout
from _lib_exitcodes import EXIT_OK, EXIT_CALLER_ERROR  # noqa: E402
from _lib_io import (  # noqa: E402  (#1789)
    OutputWriteError,
    exit_on_output_write_error,
    output_write,
    safe_label,
)
from _lib_validation import is_valid_tenant_id  # noqa: E402  (ADR-035)
import _lib_confedit  # noqa: E402  (ADR-036 §2: Go writes conf.d, #3206)
import _lib_write_check  # noqa: E402  (ADR-036 §2, #2906)

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is required.  pip install pyyaml", file=sys.stderr)
    sys.exit(EXIT_CALLER_ERROR)

try:
    from kubernetes import client, config, watch
    HAS_K8S = True
except ImportError:
    HAS_K8S = False

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
logging.basicConfig(format=LOG_FORMAT, level=logging.INFO)
log = logging.getLogger("da-assembler")

CRD_GROUP = "dynamicalerting.io"
CRD_VERSION = "v1alpha1"
CRD_PLURAL = "thresholdconfigs"

# Signals. ⛔ Nothing is installed at import (#2529): `main` installs the
# handler its mode needs. Installed at import, the controller's flag handler
# also took over SIGINT / SIGTERM in every process that imports this module
# (the test suite included), and under `--render-cr` it only set a flag
# nothing read: the run went on waiting for da-crdecode.

# Controller modes (`--once` / watch): the watch loop stops at the next event.
_shutdown = False


def _signal_handler(signum, frame):
    global _shutdown
    log.info("Received signal %d, shutting down...", signum)
    _shutdown = True


class _Interrupted(BaseException):
    """SIGINT / SIGTERM under ``--render-cr``. A BaseException, so the
    ``except Exception`` handlers on the render path do not swallow it;
    raised while da-crdecode runs, ``subprocess.run`` kills the child."""

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


#: `--render-cr`: the signal that arrived while `_signals_deferred` held it.
_pending_signal: int | None = None
_deferring = False


def _render_signal_handler(signum, frame):
    global _pending_signal
    if _deferring:
        _pending_signal = signum
        return
    raise _Interrupted(signum)


class _signals_deferred:
    """Around the write: a signal that arrives inside is raised when the
    block ends, so the rendered file is never left half-written."""

    def __enter__(self):
        global _deferring, _pending_signal
        _pending_signal = None
        _deferring = True

    def __exit__(self, *exc):
        global _deferring
        _deferring = False
        if _pending_signal is not None:
            raise _Interrupted(_pending_signal)
        return False


class _handling_signals:
    """SIGINT / SIGTERM go to *handler* inside the block; the previous
    handlers come back after it (`main` is also called in-process)."""

    def __init__(self, handler) -> None:
        self.handler = handler
        self.previous: dict = {}

    def __enter__(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            self.previous[sig] = signal.signal(sig, self.handler)

    def __exit__(self, *exc):
        # Both handlers come back even when a signal arrives in between:
        # `_render_signal_handler` holds it until both are restored, then
        # it is raised (#2529 review). One that arrives before this point
        # is raised here and is the caller's to catch.
        with _signals_deferred():
            for sig, handler in self.previous.items():
                signal.signal(sig, handler)
        return False


def _ignore_signals() -> None:
    """SIGINT / SIGTERM do nothing from here on (a second Ctrl-C while the
    process is already stopping would otherwise raise again)."""
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, signal.SIG_IGN)


def _die_by_signal(signum: int) -> int:
    """End the process the way *signum* would have ended it (rc 128+N in a
    shell), as `ops/patch_config` does before its write. Returns only where
    the signal does not end the process."""
    _ignore_signals()
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)
    return EXIT_CALLER_ERROR


# ── CR → conf.d file (ADR-036 §2: Go writes it) ─────────────────────

#: The tool name on every line `_lib_confedit.write_all_or_exit` prints.
_TOOL = "da_assembler"


def render_cr_to_yaml(cr: dict) -> str:
    """The text the rendered file of *cr* holds, as `da-guard write` writes
    it (ADR-036 §2, #3206): a preview — `write --dry-run` of
    `render_entry(cr)` into an empty private directory, nothing written
    anywhere. The file `reconcile_one` writes is this text, byte for byte
    (the same entry, the same Go). Raises ValueError for a CR this tool
    does not render (`render_entry`), and `_lib_confedit`'s errors (no
    usable da-guard: `ParseTreeError`; a refusal: `EditsRefused`).
    """
    entry = render_entry(cr)
    with tempfile.TemporaryDirectory(prefix="da-assembler-") as tmp:
        [result] = _lib_confedit.write_all(tmp, [entry], dry_run=True)
    return result["content"]


def _render_doc(cr: dict) -> "tuple[str, Dict[str, Any]]":
    """``(head comment, document)`` of *cr*'s rendered file: the comment's
    lines without their ``# `` (Go writes it), the document as Python data."""
    spec = cr.get("spec", {})
    metadata = cr.get("metadata", {})

    doc: Dict[str, Any] = {}

    # Each block is written only when it is a non-empty MAPPING. ⛔ Not a
    # truthiness test: a text block (`defaults: "0"`) is truthy, and writing
    # it out makes the exporter skip the whole file (YamlFileError,
    # ValueError). Any non-mapping block is dropped.

    # Tenants block (required)
    tenants = spec.get("tenants")
    if isinstance(tenants, dict) and tenants:
        doc["tenants"] = tenants

    # Optional platform blocks
    defaults = spec.get("defaults")
    if isinstance(defaults, dict) and defaults:
        doc["defaults"] = defaults

    state_filters = spec.get("stateFilters")
    if isinstance(state_filters, dict) and state_filters:
        doc["state_filters"] = state_filters

    # The CR's namespace and name are outside input put into a comment
    # (`comment_field`: a character da-guard's comment refuses is named here).
    # Both sit inside the header's first line (`line=True`: a line break is
    # refused too, naming the field), as init / scaffold do.
    where = (f"{_lib_confedit.comment_field('metadata.namespace', metadata.get('namespace', '?'), line=True)}"
             f"/{_lib_confedit.comment_field('metadata.name', metadata.get('name', '?'), line=True)}")
    header = (
        f"{_HEADER_PREFIX.removeprefix('# ')}{where}\n"
        "DO NOT EDIT — changes will be overwritten on next reconcile."
    )

    return header, doc


def _render_value(doc: Dict[str, Any]) -> dict:
    """*doc* (`_render_doc`) as the typed value `da-guard write` creates the
    file from. Every value has the type the CR's JSON gave it
    (`_lib_confedit.data_value`: a string is written as a string, double
    quoted), except numbers under ``defaults`` and ``tenants``: the exporter
    decodes thresholds into ``float64``, so every number there goes through
    `number_value` (an int past 18 digits goes as the float it is to the
    exporter; a shorter one stays an int). A ``defaults`` value that is a
    string or a bool is written as it is: the exporter reads its text as a
    tenant's value is read and refuses that key alone when it is no number
    (#3250, served-values' ``root_defaults_refused``). A sequence / mapping
    there, or an int past float64's range, is ValueError naming the key, as
    is a tenant id that is not one (ADR-035)
    and a value with no edit-contract type (an int past 18 digits under
    ``stateFilters``)."""
    entries = []
    for key, block in doc.items():
        if key == "tenants":
            for tenant in block:
                if not is_valid_tenant_id(tenant):
                    raise ValueError(
                        f"spec.tenants.{tenant}: {tenant!r} is not a valid tenant id "
                        "(ADR-035: a DNS-1123 label — lowercase a-z, 0-9 and '-', "
                        "starting and ending alphanumeric, at most 63 characters)")
            entries.append((key, _numbers_as_float64(block, (key,))))
            continue
        if key == "defaults":
            values = []
            for metric, value in block.items():
                why = _default_value_error(value)
                if why:
                    raise ValueError(f"spec.defaults.{metric} {why}; the exporter skips "
                                     "the whole rendered file otherwise")
                values.append((metric, _lib_confedit.data_value(value, ("defaults", metric))
                               if value is None or isinstance(value, (str, bool))
                               else _lib_confedit.number_value(value, at=("defaults", metric))))
            entries.append((key, _lib_confedit.map_value(values)))
            continue
        entries.append((key, _lib_confedit.data_value(block, (key,))))
    return _lib_confedit.map_value(entries)


def _numbers_as_float64(obj: Any, at: "tuple[str, ...]") -> dict:
    """`data_value` of *obj*, every number in it through `number_value`
    (the exporter's float64: an int past 18 digits goes as a float, instead
    of being refused); a bool, a string and null as they are."""
    if isinstance(obj, dict):
        for k in obj:
            if not isinstance(k, str):
                return _lib_confedit.data_value(obj, at)  # its ValueError names it
        return _lib_confedit.map_value(
            (k, _numbers_as_float64(v, (*at, k))) for k, v in obj.items())
    if isinstance(obj, (list, tuple)):
        return {"seq": [_numbers_as_float64(v, (*at, f"[{i}]")) for i, v in enumerate(obj)]}
    if isinstance(obj, (int, float)) and not isinstance(obj, bool):
        return _lib_confedit.number_value(obj, at=at)
    return _lib_confedit.data_value(obj, at)


def render_entry(cr: dict) -> dict:
    """*cr*'s rendered file as one `da-guard write` entry (ADR-036 §2): a
    file generated whole from the CR, so a file already at its path is
    replaced whole (`replace`: its mode and owner are kept; a new file is
    0600, da-guard's). ValueError for a CR this tool does not render
    (`_output_filename`, `_render_value`)."""
    filename = _output_filename(cr)
    header, doc = _render_doc(cr)
    return _lib_confedit.create_entry(filename, _render_value(doc),
                                      head_comment=header, replace=True)


def _content_sha256(content: "str | bytes") -> str:
    """Compute SHA-256 hex digest of the file's content."""
    raw = content.encode("utf-8") if isinstance(content, str) else content
    return hashlib.sha256(raw).hexdigest()


def _output_filename(cr: dict) -> str:
    """The output filename, ``<metadata.name>.yaml``. ValueError when the
    name is not a Kubernetes object name (DNS-1123 subdomain, as the API
    server checks it, #2396): the name is a path under --config-dir and goes
    into the header comment. Not ADR-035's tenant-id rule: the file holds
    the CR's tenants, its name is the CR's (a subdomain may hold ``.`` and
    253 characters, which a tenant id may not)."""
    name = (cr.get("metadata") or {}).get("name")
    if not isinstance(name, str) or not _is_dns1123_subdomain(name):
        raise ValueError(
            f"metadata.name {name!r} is not a valid Kubernetes object name "
            "(DNS-1123 subdomain: lowercase a-z, 0-9, '-' and '.', starting "
            f"and ending alphanumeric, at most {_DNS1123_SUBDOMAIN_MAX} "
            "characters); not written")
    return f"{name}.yaml"


# ── File writing ─────────────────────────────────────────────────────

def _make_config_dir(config_dir: Path) -> None:
    """Create --config-dir (da-guard writes into an existing root only)."""
    # #1789: the wrapper is given the directory this mkdir creates.
    with output_write(config_dir, flag="--config-dir", action="create directory"):
        config_dir.mkdir(parents=True, exist_ok=True)


def write_rendered(
    config_dir: Path,
    entry: dict,
    *,
    dry_run: bool = False,
    cli: bool = False,
) -> "tuple[bool, str]":
    """Write *entry* (`render_entry`) under *config_dir* through `da-guard
    write` (ADR-036 §2): ``(changed, sha256 of the file's content)``.
    The same bytes as the file already there are not written (da-guard's
    `unchanged`: changed False); under *dry_run* nothing is written and
    *changed* says whether it would be.

    *cli* (``--render-cr``, a one-shot run) writes through
    `_lib_confedit.write_all_or_exit`: a refusal, a write that stopped
    part-way or no usable da-guard is named on stderr and the process exits
    2 (a --config-dir it created is removed again). The controller (*cli*
    False) is a long-running process — one CR among many, in a watch loop
    that must outlive it — so it never exits here: `_lib_confedit.write_all`
    raises (EditsRefused, EditCallerError, PartialWrite, ParseTreeError),
    and `reconcile_one` logs it and sets the CR's status to Error.
    """
    filename = entry["path"]
    dest = config_dir / filename
    if dry_run:
        # A --config-dir not there yet is an empty tree: previewed in one.
        if config_dir.is_dir():
            [result] = _lib_confedit.write_all(config_dir, [entry], dry_run=True)
        else:
            with tempfile.TemporaryDirectory(prefix="da-assembler-") as tmp:
                [result] = _lib_confedit.write_all(tmp, [entry], dry_run=True)
        if result["status"] == _lib_confedit.UNCHANGED:
            with output_write(dest, flag="--config-dir", action="read"):
                return False, _content_sha256(dest.read_bytes())
        log.info("DRY-RUN: would write %s (%d bytes)", filename,
                 len(result["content"].encode("utf-8")))
        return True, _content_sha256(result["content"])
    if cli:
        [result] = _lib_confedit.write_all_or_exit(
            config_dir, [entry], tool=_TOOL,
            make_root=lambda: _make_config_dir(config_dir))
    else:
        if not config_dir.is_dir():
            _make_config_dir(config_dir)
        [result] = _lib_confedit.write_all(config_dir, [entry])
    # The bytes da-guard wrote (or left: unchanged) are the file's now.
    with output_write(dest, flag="--config-dir", action="read"):
        sha = _content_sha256(dest.read_bytes())
    return result["status"] != _lib_confedit.UNCHANGED, sha


def remove_rendered(
    config_dir: Path,
    filename: str,
    *,
    dry_run: bool = False,
) -> bool:
    """Remove a rendered file (CR deleted) through `da-guard write`'s delete
    (ADR-036 §2): True when the file was there (under *dry_run*: would be
    removed). Raises `_lib_confedit`'s errors — a delete da-guard refuses
    (the tree would be worse without the file), no usable da-guard — for
    the caller to log; never exits."""
    if not config_dir.is_dir():
        return False
    [result] = _lib_confedit.write_all(
        config_dir, [_lib_confedit.delete_entry(filename)], dry_run=dry_run)
    if result["status"] == _lib_confedit.UNCHANGED:
        return False
    if dry_run:
        log.info("DRY-RUN: would delete %s", filename)
    return True


# ── Status update ────────────────────────────────────────────────────

def update_cr_status(
    api: Any,
    cr: dict,
    phase: str,
    sha: str = "",
    message: str = "",
) -> None:
    """Patch the CR's status subresource."""
    name = cr["metadata"]["name"]
    namespace = cr["metadata"]["namespace"]
    tenant_count = len(cr.get("spec", {}).get("tenants", {}))

    status_body = {
        "status": {
            "phase": phase,
            "lastRenderedHash": sha,
            "lastRenderedTime": datetime.now(timezone.utc).isoformat(),
            "message": message,
            "tenantCount": tenant_count,
        }
    }

    try:
        api.patch_namespaced_custom_object_status(
            CRD_GROUP, CRD_VERSION, namespace, CRD_PLURAL, name,
            body=status_body,
        )
    except Exception as e:
        log.warning("Failed to update status for %s/%s: %s",
                    namespace, name, e)


# ── Reconcile loop ───────────────────────────────────────────────────

def controller_refusal(config_dir: Path) -> list[str]:
    """The controller's shared conf.d refusal (#3206): the tree under
    *config_dir*, judged by Go AFRESH once per reconcile cycle — one per
    `run_once`, one per watch event — and shared by every CR of that cycle
    (`_lib_parse_cache.refusal_now`: one tree-mode run, never the
    once-per-process memo — the tree changes under a long-running
    controller). [] when the directory does not exist yet or Go serves it;
    else every refused path is logged as ERROR and the lines are returned —
    the caller skips the cycle's writes and deletes, and the next cycle
    judges again. No usable da-guard is a refusal too (one ERROR line):
    nothing can be judged, so nothing is written. The process never exits
    on it."""
    if not config_dir.is_dir():
        return []
    import _lib_parse_cache  # noqa: PLC0415
    import _lib_parse_tree  # noqa: PLC0415
    try:
        lines = _lib_parse_cache.refusal_now(config_dir)
    except _lib_parse_tree.ParseTreeError as e:
        line = f"cannot judge the conf.d at {safe_label(str(config_dir))} " \
               f"through `da-guard parse-tree`: {e}; nothing is written this time"
        log.error("%s", line)
        return [f"ERROR: {line}"]
    for line in lines[:-1]:
        log.error("%s", line.removeprefix("ERROR: "))
    if lines:
        log.error("the conf.d at %s holds %d path(s) da-guard validation names; "
                  "nothing is written this time (fix or remove them; the next "
                  "reconcile judges the tree again)", safe_label(str(config_dir)),
                  len(lines) - 1)
    return lines


def reconcile_one(
    cr: dict,
    config_dir: Path,
    *,
    dry_run: bool = False,
    api: Any = None,
    cli: bool = False,
    refusal: "list[str] | None" = None,
) -> None:
    """Reconcile a single ThresholdConfig CR.

    *refusal* is the cycle's `controller_refusal` (the controller judges the
    tree once per cycle and hands it to every CR of it); None judges here
    (a caller that reconciles one CR on its own).

    *cli* says which of the two callers this is: ``render_cr_file``
    (``--render-cr``, one CR, the rc is the operator's answer) or the
    controller loop (``run_once`` / ``run_watch``, one item among many).
    ⛔ It is an EXPLICIT flag and not ``api is None``, because the controller
    passes ``api=None`` too whenever ``--dry-run`` is set — inferring the
    caller from it made a dry-run controller re-raise, which aborts
    ``run_once`` after the first bad CR and turns ``run_watch`` into an
    endless "Watch interrupted … Reconnecting" loop.

    ⚠️ A CR refused here (its status Error) is judged again only on its own
    next event, a restart's initial sync, or a `--once` run — not when
    another CR changes what refused it (CR A releases the tenant CR B was
    refused for declaring twice: B stays Error, unwritten). Judging refused
    CRs again periodically is the controller redesign's (#3318).

    Cost: each CR is one `da-guard write` run, the tree judged per run
    (O(N²) over N CRs in a sync). Accepted: direct-render is retired to
    local debugging (ADR-008), and one batch per CR is what lets each CR's
    refusal land on its own status.
    """
    name = cr["metadata"]["name"]
    namespace = cr["metadata"].get("namespace", "default")

    try:
        # ADR-036 §2 (#3206): Go writes the file. The CR becomes one
        # `da-guard write` entry (typed values, the header a head comment;
        # ValueError for a name, a tenant id or a value this tool does not
        # render), which da-guard writes after reading it back in the tree,
        # replacing the file there whole (it is generated whole from the CR:
        # the file it replaces is not the base of a change).
        # #3206: the tree this writes INTO passes the shared refusal first.
        # `--render-cr` (one shot): exit 2, every path named, nothing
        # written. The controller: judged afresh on every reconcile; refused
        # ⇒ every path logged, this write skipped, the loop goes on.
        if cli:
            _lib_write_check.require_output_tree(config_dir)
        elif (controller_refusal(config_dir) if refusal is None else refusal):
            if api and not dry_run:
                update_cr_status(api, cr, "Error", message=(
                    "conf.d holds paths da-guard validation names; not written")[:200])
            return
        entry = render_entry(cr)
        filename = entry["path"]
        changed, sha = write_rendered(config_dir, entry, dry_run=dry_run, cli=cli)

        if changed:
            log.info("Rendered %s/%s → %s (sha256:%s)",
                     namespace, name, filename, sha[:12])
        else:
            log.debug("No change for %s/%s", namespace, name)

        if api and not dry_run:
            update_cr_status(api, cr, "Rendered", sha=sha,
                             message=f"Written to {filename}")

    except Exception as e:
        # #1789: an unusable `--config-dir` used to be logged here and then
        # forgotten — `render_cr_file` returned EXIT_OK unconditionally, so
        # the CLI exited 0 having written nothing at all (measured). Re-raise
        # so `main`'s decorator turns it into the standard one-line rc=2.
        #
        # ⛔ ONLY on the CLI path (`cli=True`, i.e. `--render-cr`). In the
        # CONTROLLER path a reconcile is one item in a watch loop: a write
        # failure has to be reported on the CR's status and the loop has to
        # keep going, or one bad CR stops every other tenant from being
        # rendered. That path is unchanged — it still logs, still sets the
        # Error status, still returns. ⚠️ `api is None` is NOT the test: the
        # controller nulls `api` under `--dry-run`, so it would take this
        # branch too (see the docstring).
        #
        # #2395: ANY exception on the CLI path, not only OutputWriteError —
        # anything else was logged here and `render_cr_file` still returned
        # EXIT_OK with nothing written. `render_cr_file` turns it into rc 2.
        #
        # ADR-036 §2 (#3206): the same split for `da-guard write`. The CLI
        # writes through `write_all_or_exit` (rc 2, every refused file named);
        # the controller through `write_all`, whose refusal / caller error /
        # PartialWrite / no-da-guard land here as exceptions — never a
        # `sys.exit`, which `except Exception` would not catch and which
        # would end the watch loop and every other CR's reconcile with it.
        if cli:
            raise
        log.error("Failed to reconcile %s/%s: %s", namespace, name, e)
        if api and not dry_run:
            update_cr_status(api, cr, "Error", message=str(e)[:200])


def _sync(
    api: Any,
    config_dir: Path,
    *,
    dry_run: bool = False,
    namespace: str = "",
) -> "tuple[int, bool]":
    """One reconcile cycle over the listed CRs: ``(rc, refused)``. rc is
    EXIT_CALLER_ERROR when the list failed. The tree is judged once
    (`controller_refusal`) for every CR of the cycle; refused, nothing is
    written."""
    try:
        if namespace:
            result = api.list_namespaced_custom_object(
                CRD_GROUP, CRD_VERSION, namespace, CRD_PLURAL)
        else:
            result = api.list_cluster_custom_object(
                CRD_GROUP, CRD_VERSION, CRD_PLURAL)
    except Exception as e:
        log.error("Failed to list ThresholdConfig resources: %s", e)
        return EXIT_CALLER_ERROR, False

    items = result.get("items", [])
    log.info("Found %d ThresholdConfig resource(s)", len(items))

    refusal = controller_refusal(config_dir) if items else []  # once per cycle
    for cr in items:
        reconcile_one(cr, config_dir, dry_run=dry_run,
                      api=None if dry_run else api, refusal=refusal)
    return EXIT_OK, bool(refusal)


def run_once(
    api: Any,
    config_dir: Path,
    *,
    dry_run: bool = False,
    namespace: str = "",
) -> int:
    """One-shot (`--once`): list all ThresholdConfig CRs and render them. A
    refused tree is exit 2 (EXIT_CALLER_ERROR, the shared refusal's code)
    after its ERROR lines."""
    rc, refused = _sync(api, config_dir, dry_run=dry_run, namespace=namespace)
    return EXIT_CALLER_ERROR if refused else rc


def run_watch(
    api: Any,
    config_dir: Path,
    *,
    dry_run: bool = False,
    namespace: str = "",
) -> None:
    """Watch loop: react to ThresholdConfig changes in real time.

    ⚠️ Only the event's own CR is reconciled: a CR refused earlier (status
    Error) waits for its own next event, a restart or a `--once` run, even
    when the refusal no longer holds (see `reconcile_one`; periodic
    judging again is #3318's)."""
    log.info("Starting watch loop (config-dir=%s, dry-run=%s)",
             config_dir, dry_run)

    w = watch.Watch()
    resource_version = ""

    while not _shutdown:
        try:
            if namespace:
                stream = w.stream(
                    api.list_namespaced_custom_object,
                    CRD_GROUP, CRD_VERSION, namespace, CRD_PLURAL,
                    resource_version=resource_version,
                    timeout_seconds=300,
                )
            else:
                stream = w.stream(
                    api.list_cluster_custom_object,
                    CRD_GROUP, CRD_VERSION, CRD_PLURAL,
                    resource_version=resource_version,
                    timeout_seconds=300,
                )

            for event in stream:
                if _shutdown:
                    break

                event_type = event["type"]
                cr = event["object"]
                resource_version = (
                    cr.get("metadata", {}).get("resourceVersion", ""))
                name = cr.get("metadata", {}).get("name", "?")
                ns = cr.get("metadata", {}).get("namespace", "?")

                if event_type not in ("ADDED", "MODIFIED", "DELETED"):
                    continue
                refusal = controller_refusal(config_dir)  # once per event
                if event_type in ("ADDED", "MODIFIED"):
                    reconcile_one(cr, config_dir, dry_run=dry_run,
                                  api=None if dry_run else api, refusal=refusal)
                elif event_type == "DELETED":
                    if refusal:
                        continue  # #3206: no write (a delete too) this event
                    # ADR-036 §2: da-guard deletes the file. A refused or
                    # failed delete (or a name this tool never renders) is
                    # logged and the loop goes on: one CR's file never ends
                    # the watch.
                    try:
                        filename = _output_filename(cr)
                        removed = remove_rendered(config_dir, filename,
                                                  dry_run=dry_run)
                    except Exception as e:  # noqa: BLE001 — logged, loop goes on
                        log.error("Failed to delete the file of CR %s/%s: %s",
                                  ns, name, e)
                        continue
                    if removed:
                        log.info("Deleted %s (CR %s/%s removed)",
                                 filename, ns, name)

        except Exception as e:
            if _shutdown:
                break
            log.warning("Watch interrupted: %s. Reconnecting in 5s...", e)
            time.sleep(5)

    log.info("Watch loop stopped.")


# ── Offline render (no K8s required) ─────────────────────────────────

#: #2476: `--render-cr` does not read the CR with PyYAML. It asks
#: `da-crdecode` (components/threshold-exporter/app/cmd/da-crdecode), which
#: runs the YAML→JSON conversion Kubernetes clients run before a CR reaches
#: the API server (sigs.k8s.io/yaml.YAMLToJSON), and works on that JSON:
#: types, null keys, duplicate keys and merges are what that conversion
#: makes of them, not what YAML 1.1 makes of them. The API server's own
#: checks (the CRD schema) are not part of that conversion; the checks below
#: cover some of them, without a guarantee that they match.
CRDECODE_BINARY_NAME = "da-crdecode"
CRDECODE_ENV = "DA_CRDECODE_BINARY"
CRDECODE_TIMEOUT_SECONDS = 60
#: Every line of da-crdecode's stderr is passed on behind this, so none of
#: it starts at column 0.
CRDECODE_PREFIX = "  da-crdecode| "


class CrDecodeError(Exception):
    """da-crdecode could not be run, or refused the file.

    ``str()`` is one line; ``stderr_lines`` is da-crdecode's stderr, every
    non-empty line as written.
    """

    def __init__(self, message: str, stderr_lines: list[str]) -> None:
        super().__init__(message)
        self.stderr_lines = stderr_lines


def _stderr_lines(raw: bytes | None) -> list[str]:
    """Every non-empty line of *raw*, split on ``\\n`` only (not
    ``str.splitlines``, which also splits on NEL, U+2028 and others that can
    sit inside a file name). Bytes that are not UTF-8 are shown escaped."""
    if not raw:
        return []
    text = raw.decode("utf-8", errors="backslashreplace")
    return [ln.rstrip() for ln in text.split("\n") if ln.strip()]


def _crdecode_binary() -> str | None:
    """``$DA_CRDECODE_BINARY`` when set, else ``da-crdecode`` on ``$PATH``."""
    return os.environ.get(CRDECODE_ENV) or shutil.which(CRDECODE_BINARY_NAME)


def decode_cr(cr_path: Path) -> tuple[Any, list, list[str]]:
    """The CR in *cr_path* as Kubernetes clients decode it (each object's
    keys in the order the CR writes them), where that reading is not the
    CR's text (da-crdecode's ``differs_from_text`` for the document, #3206),
    and da-crdecode's stderr lines (empty on a normal run).

    Raises :class:`CrDecodeError` when the binary is missing, cannot be run,
    does not finish within ``CRDECODE_TIMEOUT_SECONDS``, exits non-zero (the
    file cannot be read, the conversion refuses it, it holds more than one
    document, or it cannot be read as written) or prints something that is
    not its JSON contract — a da-crdecode older than this tool prints no
    ``differs_from_text``.
    ``json.loads`` raises RecursionError for a document nested too deeply for
    Python's JSON reader; that is left to the caller.
    """
    binary = _crdecode_binary()
    if not binary:
        raise CrDecodeError(
            f"{CRDECODE_BINARY_NAME} was not found: --render-cr decodes the CR "
            "with it. Build it (cd components/threshold-exporter/app && go "
            f"build ./cmd/{CRDECODE_BINARY_NAME}) and put it on $PATH, or set "
            f"${CRDECODE_ENV} to its path", [])
    # An absolute path never starts with `-`, so it is not read as a flag.
    cmd = [binary, os.path.abspath(cr_path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, check=False,
                              timeout=CRDECODE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as e:
        raise CrDecodeError(
            f"{CRDECODE_BINARY_NAME} did not finish within "
            f"{CRDECODE_TIMEOUT_SECONDS}s", _stderr_lines(e.stderr)) from e
    except OSError as e:
        raise CrDecodeError(f"cannot run {binary}: {e}", []) from e
    lines = _stderr_lines(proc.stderr)
    if proc.returncode != 0:
        raise CrDecodeError(
            f"{CRDECODE_BINARY_NAME} exited {proc.returncode}", lines)
    try:
        out = json.loads(proc.stdout)
    except ValueError as e:  # JSONDecodeError, or stdout that is not UTF-8
        raise CrDecodeError(
            f"{CRDECODE_BINARY_NAME} printed output that is not JSON: {e}",
            lines) from e
    docs = out.get("documents") if isinstance(out, dict) else None
    if not isinstance(docs, list) or len(docs) != 1:
        raise CrDecodeError(
            f"{CRDECODE_BINARY_NAME} printed JSON without exactly one "
            "document under \"documents\"", lines)
    differs = out.get("differs_from_text")
    if (not isinstance(differs, list) or len(differs) != 1
            or not isinstance(differs[0], list)
            or not all(_is_difference(d) for d in differs[0])):
        raise CrDecodeError(
            f"{CRDECODE_BINARY_NAME} printed JSON without one list of "
            "differences under \"differs_from_text\" (a da-crdecode older than "
            "this tool prints none: rebuild it)", lines)
    return docs[0], differs[0], lines


#: The fields each kind of da-crdecode difference carries (#3206).
_DIFFERENCE_FIELDS = {
    "keys": {"kind", "path", "written_keys", "read_keys"},
    "string": {"kind", "path", "text"},
    "structure": {"kind", "path", "written", "read"},
}


def _is_difference(d: Any) -> bool:
    """Whether *d* is one entry of da-crdecode's ``differs_from_text``."""
    if not isinstance(d, dict) or not isinstance(d.get("kind"), str) \
            or set(d) != _DIFFERENCE_FIELDS.get(d["kind"]):
        return False
    path = d["path"]
    return (isinstance(path, list)
            and all(isinstance(s, str) or (isinstance(s, int)
                                           and not isinstance(s, bool))
                    for s in path))


def _log_crdecode_lines(lines: list[str], level: int) -> None:
    """Every line of da-crdecode's stderr, escaped, behind ``CRDECODE_PREFIX``."""
    for line in lines:
        log.log(level, "%s%s", CRDECODE_PREFIX, safe_label(line))


def _json_kind(value: Any) -> str:
    """How *value*, decoded from JSON, is described in an error."""
    if isinstance(value, bool):
        return f"a boolean ({json.dumps(value)})"
    if isinstance(value, (int, float)):
        return f"a number ({json.dumps(value)})"
    if isinstance(value, list):
        return "a list"
    if isinstance(value, dict):
        return "a mapping"
    return f"{value!r}"

#: #2396: the formats the API server enforces on a ThresholdConfig, so the
#: `--render-cr` path accepts the same names the controller path can ever
#: see. `metadata.name` becomes the output FILE NAME and both land in the
#: header comment: unchecked, `../x` was written outside `--config-dir` and
#: a newline split the header into a top-level key of the rendered file.
#: ⛔ Used with `re.fullmatch` only — `$` also matches before a trailing
#: newline, which is exactly the shape this has to refuse.
_DNS1123_LABEL = r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?"
_DNS1123_SUBDOMAIN_RE = re.compile(rf"{_DNS1123_LABEL}(?:\.{_DNS1123_LABEL})*")
_DNS1123_LABEL_RE = re.compile(_DNS1123_LABEL)
_DNS1123_SUBDOMAIN_MAX = 253
# The first line of every file render_cr_to_yaml writes, before
# ``<namespace>/<name>``; _stale_render_hint reads it back (#2430).
_HEADER_PREFIX = "# Auto-generated by da-assembler from ThresholdConfig "
_HEADER_READ_MAX = 4096
_DNS1123_LABEL_MAX = 63


def _is_dns1123_subdomain(text: str) -> bool:
    """RFC 1123 subdomain, as Kubernetes checks an object name."""
    return (len(text) <= _DNS1123_SUBDOMAIN_MAX
            and _DNS1123_SUBDOMAIN_RE.fullmatch(text) is not None)


def _is_dns1123_label(text: str) -> bool:
    """RFC 1123 label, as Kubernetes checks a namespace name."""
    return (len(text) <= _DNS1123_LABEL_MAX
            and _DNS1123_LABEL_RE.fullmatch(text) is not None)


def _rendered_cr_name(path: Path) -> str | None:
    """The CR name in *path*'s da-assembler header, or None.

    None when *path* is not a readable file whose first line is the header
    this tool writes (``_HEADER_PREFIX`` + ``<namespace>/<name>``): a
    hand-written file, a directory, an unreadable or undecodable one.
    Earlier versions wrote the same header, with the name as they typed it
    (``True`` for ``yes``). Only the first ``_HEADER_READ_MAX`` bytes are
    read.
    """
    try:
        if not path.is_file():
            return None
        with open(path, "rb") as fh:
            head = fh.read(_HEADER_READ_MAX)
        # A CRLF file (written on Windows, or checked out with
        # core.autocrlf) must not read its name as `8\r`.
        first = head.split(b"\n", 1)[0].removesuffix(b"\r").decode("utf-8")
    except (OSError, ValueError):
        # ValueError: an over-long / NUL path, or UnicodeDecodeError.
        return None
    if not first.startswith(_HEADER_PREFIX):
        return None
    # An earlier version's name never has `/`; a namespace might.
    _, sep, name = first[len(_HEADER_PREFIX):].rpartition("/")
    return name if sep else None


def _stale_render_hint(config_dir: Path, old_name: str, new_file: str) -> str:
    """#2430: a hint about *old_name*.yaml, or "" when there is none.

    An earlier version of this tool rendered a CR whose name is refused now
    to ``f"{name}.yaml"`` with the name as ``yaml.safe_load`` typed it
    (``010`` -> ``8.yaml``, ``~`` -> ``None.yaml``). Such a file left in a
    persistent --config-dir declares the tenant a second time once the CR
    is fixed. Only a file that exists AND carries this tool's header is
    judged; anything else (no file, a directory, a hand-written file) gets
    no hint, since nothing says it is safe to delete.

    The header's name decides. Equal to *old_name*: the earlier version may
    have rendered this CR there, but a CR whose name is the string
    *old_name* writes the same header, so the hint asks for that check.
    Different: the path resolved to another CR's file — on a
    case-insensitive file system (macOS, NTFS) ``True.yaml`` opens
    ``true.yaml``, the output of a valid CR named ``"true"`` — and the hint
    says not to delete it. The directory is not listed: only this one path
    is looked at.
    """
    target = f"{old_name}.yaml"
    shown = _rendered_cr_name(config_dir / target)
    if shown is None:
        return ""
    if shown != old_name:
        return (f". {target} in this --config-dir resolves to a file whose "
                f"header comment shows it is the output of CR {shown!r}, "
                "not an earlier version's file for this CR: do not delete "
                "it")
    return (f". {target} exists in this --config-dir: an earlier "
            "version of this tool may have rendered this CR to it. "
            "Before deleting it, confirm it is not the output of another "
            f"CR whose metadata.name is the string {old_name!r} (the "
            "header comment is the same for both, so it cannot tell them "
            f"apart). If it is this CR's, delete it, or it and the "
            f"{new_file} file both declare the tenant")


def _legacy_name_hint(cr_path: Path, config_dir: Path) -> str:
    """#2430: the stale-file hint for a refused ``metadata.name``, or ``""``.

    Versions before #2371 read the CR with PyYAML and named the output file
    after the name AS PYYAML TYPED IT (``010`` -> ``8.yaml``, ``yes`` ->
    ``True.yaml``, ``1:30`` -> ``90.yaml``, an unquoted datetime -> its
    Python spelling). Rebuilding that file name needs PyYAML's reading of the
    name, so this — and only this — still reads the file with PyYAML. It
    decides nothing about the CR: the name was already refused from
    da-crdecode's output. Best effort: no hint when PyYAML cannot read the
    file, or the name is not a scalar under ``metadata``.

    The hint is given when the earlier version's file name differs from the
    name as written, or when the name as written is not a valid name either
    (the CR has to be renamed, so the old file is stale even when spelled as
    written). A null name was rendered to ``None.yaml``; that hint is given
    by the caller.
    """
    try:
        with open(cr_path, encoding="utf-8") as fh:
            root = yaml.compose(fh, Loader=yaml.SafeLoader)
        node = _metadata_name_node(root)
        if node is None or node.tag == "tag:yaml.org,2002:null":
            return ""
        value = yaml.SafeLoader("").construct_object(node, deep=True)
    except Exception:  # noqa: BLE001 — a hint only; any failure means none
        return ""
    plain = node.style is None
    if plain and node.tag in _LEGACY_RETYPED_TAGS:
        try:
            legacy = str(value)
        except ValueError:  # an int past Python's str() digit limit
            return ""
        quotable = _is_dns1123_subdomain(node.value)
        if legacy == node.value and quotable:
            return ""  # quoting renders to the same file and overwrites it
        return _stale_render_hint(
            config_dir, legacy,
            "quoted name's" if quotable else "renamed CR's")
    if isinstance(value, datetime):
        return _stale_render_hint(config_dir, str(value), "renamed CR's")
    return ""


#: Tags of a plain scalar PyYAML retyped, so the earlier version's file name
#: was not the name as written (see :func:`_legacy_name_hint`).
_LEGACY_RETYPED_TAGS = frozenset("tag:yaml.org,2002:" + t
                                 for t in ("int", "float", "bool"))


def _metadata_name_node(root: Any) -> Any:
    """The scalar node of ``metadata.name`` in the composed *root*, or None.
    The last ``metadata`` / ``name`` key wins, as it did for the load."""
    def last(mapping: Any, key: str) -> Any:
        if not isinstance(mapping, yaml.MappingNode):
            return None
        found = None
        for k, v in mapping.value:
            if isinstance(k, yaml.ScalarNode) and k.value == key:
                found = v
        return found
    node = last(last(root, "metadata"), "name")
    return node if isinstance(node, yaml.ScalarNode) else None


def _path_text(path: "list") -> str:
    """A da-crdecode difference's path as this tool's messages spell it:
    ``spec.tenants.t1.x[0]`` (``<document>`` for the document itself)."""
    out = ""
    for seg in path:
        out += f"[{seg}]" if isinstance(seg, int) else (f".{seg}" if out else seg)
    return out or "<document>"


def _difference_message(d: dict) -> str:
    """Why the keys at a da-crdecode "keys" or "structure" difference cannot
    be rendered as the CR writes them (#3206: the comparison is
    da-crdecode's). A "string" difference is worded where it is found."""
    where = _path_text(d["path"])
    if d["kind"] == "keys":
        return (f"{where}: the keys as written ("
                + (", ".join(repr(k) for k in d["written_keys"]) or "none")
                + ") are not the keys Kubernetes reads ("
                + (", ".join(repr(k) for k in d["read_keys"]) or "none")
                + "); rendering would rename them. Quote each such key so "
                "both readings are the same text")
    return (f"{where}: Kubernetes reads {d['read']} here but the CR as "
            f"written holds {d['written']}, so the keys below cannot be "
            "compared; this tool refuses such a CR")


def _not_as_written(differs: list, path: "list", *,
                    below: bool = False) -> "dict | None":
    """The da-crdecode difference that leaves the value at *path* not read
    as written, or None (#2476, #3206).

    A "keys" or "structure" difference at *path* or above it: below one,
    da-crdecode compares nothing (which written key a read key came from
    cannot be told), so the value has no verdict and is taken as not as
    written. A "string" difference at *path* itself: the value is read as
    other text. *below*: also any "keys" or "structure" difference under
    *path* (a subtree whose every key is an id).
    """
    n = len(path)
    for d in differs:
        at = d["path"]
        if d["kind"] == "string":
            if at == path:
                return d
            continue
        if at == path[:len(at)] or (below and at[:n] == path):
            return d
    return None


def _profile_divergence(tenants: dict, differs: list) -> str:
    """#2476: why a tenant's ``_profile`` does not name the profile the CR
    writes, or ``""``.

    ``_profile`` names a profile, as a tenant id names a tenant: an unquoted
    ``_profile: 010`` is the number 8 to Kubernetes, ``yes`` is ``true``,
    and rendering that would point the tenant at a profile that does not
    exist. A null one is no profile. A string read as other text than the
    CR writes is da-crdecode's finding (#3206).
    """
    for tenant, values in tenants.items():
        if not isinstance(values, dict) or values.get("_profile") is None:
            continue
        read = values["_profile"]
        d = _not_as_written(differs, ["spec", "tenants", tenant, "_profile"])
        if not isinstance(read, str):
            written = ""
        elif d is not None:
            written = f", which is not the text written in the CR ({d['text']!r})"
        else:
            continue
        return (f"spec.tenants.{tenant}._profile is read by Kubernetes as "
                f"{read!r}{written}: the tenant would point at another "
                "profile. Quote it so both are the same")
    return ""


def render_cr_file(
    cr_path: Path,
    config_dir: Path,
    *,
    dry_run: bool = False,
) -> int:
    """Render a single CR YAML file to config-dir (offline mode).

    #2476: the CR is decoded by ``da-crdecode``, as Kubernetes clients decode
    it (see ``CRDECODE_BINARY_NAME``): values are rendered as decoded (an
    unquoted ``010`` VALUE is the number 8). The ids — ``metadata.name`` /
    ``namespace``, every key under ``spec.tenants`` and each tenant's
    ``_profile`` — must read as the CR writes them, or the CR is refused: an
    unquoted ``010:`` tenant would otherwise be renamed. Whether they do is
    da-crdecode's answer (``differs_from_text``, #3206), and so is the key
    order. What this path checks on top of the controller path is below; it
    does not reproduce the API server's validation.
    """
    try:
        cr, differs, warn_lines = decode_cr(cr_path)
    except CrDecodeError as e:
        log.error("Failed to parse %s: %s", cr_path, e)
        _log_crdecode_lines(e.stderr_lines, logging.ERROR)
        return EXIT_CALLER_ERROR
    except RecursionError:
        # Python's JSON reader has a depth limit; Kubernetes' decoder reads
        # far deeper. This tool's limit, not a verdict on the file.
        log.error("Failed to parse %s: nested too deeply for this tool to "
                  "read", cr_path)
        return EXIT_CALLER_ERROR
    _log_crdecode_lines(warn_lines, logging.WARNING)

    if not isinstance(cr, dict) or cr.get("kind") != "ThresholdConfig":
        log.error("%s is not a ThresholdConfig resource", cr_path)
        return EXIT_CALLER_ERROR

    # #2371: shape checks `reconcile_one` does not make. Its
    # `cr["metadata"]["name"]` sits outside its try (KeyError/TypeError
    # traceback, rc 1), a non-str/empty name became the filename (`.yaml`,
    # `42.yaml`), and a non-mapping `spec` failed inside the try and was
    # swallowed (rc 0, nothing written). An absent `spec` is left alone: it
    # renders as `spec: {}` did before.
    metadata = cr.get("metadata")
    name = metadata.get("name") if isinstance(metadata, dict) else None
    if isinstance(name, (bool, int, float)):
        log.error("%s: metadata.name must be a string, but Kubernetes reads "
                  "it as %s. Quoting makes it a string; it must still be a "
                  "valid Kubernetes object name (DNS-1123)%s", cr_path,
                  _json_kind(name), _legacy_name_hint(cr_path, config_dir))
        return EXIT_CALLER_ERROR
    if not isinstance(name, str) or not name:
        extra = ""
        if isinstance(metadata, dict) and "name" in metadata \
                and name is None:
            # #2430: an earlier version rendered a null name to `None.yaml`.
            extra = ("; a null name (empty, ~ or null) is not a string: if "
                     "the string null is meant, quote it (\"null\")"
                     + _stale_render_hint(config_dir, "None", "named CR's"))
        log.error("%s: metadata.name must be a non-empty string%s",
                  cr_path, extra)
        return EXIT_CALLER_ERROR
    # #2396: the name is the output file name and goes into the header
    # comment, so its format is checked here, before anything is written.
    if not _is_dns1123_subdomain(name):
        log.error("%s: metadata.name %r is not a valid Kubernetes object "
                  "name (DNS-1123 subdomain: lowercase a-z, 0-9, '-' and "
                  "'.', starting and ending alphanumeric, at most %d "
                  "characters)%s", cr_path, name, _DNS1123_SUBDOMAIN_MAX,
                  _legacy_name_hint(cr_path, config_dir))
        return EXIT_CALLER_ERROR
    # #2396: a null / empty namespace is UNSET in Kubernetes (the API
    # server fills in the request's namespace), so such a CR exists in a
    # cluster. It is dropped here and renders exactly as an absent one:
    # header `?`, log `default`.
    if metadata.get("namespace", "") in (None, ""):
        metadata.pop("namespace", None)
    if "namespace" in metadata:
        namespace = metadata["namespace"]
        if not isinstance(namespace, str):
            log.error("%s: metadata.namespace must be a string, but "
                      "Kubernetes reads it as %s. Quoting makes a scalar a "
                      "string; it must still be a valid Kubernetes namespace "
                      "name (DNS-1123 label)", cr_path, _json_kind(namespace))
            return EXIT_CALLER_ERROR
        if not _is_dns1123_label(namespace):
            log.error("%s: metadata.namespace %r is not a valid Kubernetes "
                      "namespace name (DNS-1123 label: lowercase a-z, 0-9 "
                      "and '-', starting and ending alphanumeric, at most "
                      "%d characters)", cr_path, namespace,
                      _DNS1123_LABEL_MAX)
            return EXIT_CALLER_ERROR
    # #2476, #3206: the ids must read as the CR writes them. da-crdecode
    # sets its reading against the CR's text (`differs`); this tool reads
    # no YAML for that. The order of the CR's keys is da-crdecode's too.
    for field in ("name", "namespace"):
        if field not in metadata:
            continue
        d = _not_as_written(differs, ["metadata", field])
        if d is None:
            continue
        if d["kind"] == "string":
            log.error("%s: metadata.%s is read by Kubernetes as %r, which is "
                      "not the text written in the CR (%r). Quote it so both "
                      "are the same", cr_path, field, metadata[field], d["text"])
        else:
            log.error("%s: metadata.%s cannot be compared with the text "
                      "written in the CR: %s", cr_path, field,
                      _difference_message(d))
        return EXIT_CALLER_ERROR
    if "spec" in cr and not isinstance(cr["spec"], dict):
        log.error("%s: spec must be a mapping", cr_path)
        return EXIT_CALLER_ERROR
    spec = cr.get("spec") or {}
    # #2476: the CRD declares `spec.profile`, but nothing renders it (the
    # controller path drops it too), so the rendered file would carry no
    # profile. A null one is no profile.
    if spec.get("profile") is not None:
        log.error("%s: spec.profile is set, but it is not rendered: the "
                  "rendered file would carry no profile. Set _profile on "
                  "each tenant under spec.tenants instead", cr_path)
        return EXIT_CALLER_ERROR
    why = ""
    if isinstance(spec.get("tenants"), dict):
        # Every key under spec.tenants is an id (owner ruling, #2476): a
        # tenant id, a metric name. And `_profile` names a profile.
        d = _not_as_written(differs, ["spec", "tenants"], below=True)
        why = _difference_message(d) if d else _profile_divergence(
            spec["tenants"], differs)
    if why:
        log.error("%s: %s", cr_path, why)
        return EXIT_CALLER_ERROR

    shape_error = _spec_block_shape_error(spec)
    if shape_error:
        log.error("%s: %s", cr_path, shape_error)
        return EXIT_CALLER_ERROR

    # #2395: `reconcile_one(cli=True)` re-raises whatever stopped the
    # render. OutputWriteError goes on to `main`'s decorator (#1789, rc 2
    # naming --config-dir); anything else is one line and rc 2 here. rc 0
    # only when the file was written (or, under --dry-run, would be) and
    # nothing raised. ⚠️ A failure BEFORE the write leaves no file; one after
    # it (e.g. in the log line that follows) is rc 2 with the file on disk.
    # #2529: a signal during the render / write is held until it is done
    # (see `_signals_deferred`); one before it leaves no file.
    try:
        with _signals_deferred():
            reconcile_one(cr, config_dir, dry_run=dry_run, cli=True)
    except OutputWriteError:
        raise
    except RecursionError:
        # #2476: a document that nests a little less deeply than the read
        # stage can take reads, then runs out of stack here. Same tool
        # limit as the read stage, so the same wording.
        log.error("Failed to render %s: nested too deeply for this tool to "
                  "render", cr_path)
        return EXIT_CALLER_ERROR
    except Exception as e:  # noqa: BLE001 — every cause is a failed render
        log.error("Failed to render %s: %s", cr_path, e)
        return EXIT_CALLER_ERROR
    return EXIT_OK

def _spec_block_shape_error(spec: dict) -> str:
    """Why *spec*'s blocks would not render as the CR wrote them, or ``""``.

    #2395. ``render_cr_to_yaml`` silently drops a block that is not a
    mapping (``defaults: [x]``, ``stateFilters: foo``), and writes a tenant
    whose value is not a mapping as-is — the exporter then refuses the WHOLE
    file (``cannot unmarshal !!int``), every tenant in it included. The CRD
    declares each of these ``type: object``.
    Left alone: null (a null tenant is read by the exporter as empty), and a
    BLOCK whose value YAML types as falsy (``defaults: 0``, ``tenants:
    false``, ``[]``) — dropped as an empty block, which #2331 pins
    (tests/ops/test_tenant_id_as_text.py). A TENANT value gets no such pass: it is
    written, not dropped, and the exporter refuses ``t2: 0`` too.
    A ``!!set`` is a mapping to the exporter (every value null), so it passes
    wherever a mapping is WRITTEN (a tenant, a state filter); a block that is
    a set is still refused, because ``render_cr_to_yaml`` would drop it.

    Below the blocks, only as deep as the exporter's types go
    (``pkg/config/types.go``): ``defaults`` is ``map[string]float64``
    (:func:`_default_value_error`) and ``state_filters`` is
    ``map[string]StateFilter`` (:func:`_state_filter_error`). Tenant values
    below the tenant (``ScheduledValue``) are not checked.
    Only ``--render-cr`` asks this; the controller path is unchanged.
    """
    for key in ("tenants", "defaults", "stateFilters"):
        block = spec.get(key)
        if block is None or isinstance(block, dict):
            continue
        if block:
            return f"spec.{key} must be a mapping"
    tenants = spec.get("tenants")
    for tenant, values in (tenants if isinstance(tenants, dict) else {}).items():
        if values is not None and not isinstance(values, (dict, set)):
            return (f"spec.tenants.{tenant} must be a mapping; the exporter "
                    f"skips the whole rendered file otherwise")
    defaults = spec.get("defaults")
    for metric, value in (defaults if isinstance(defaults, dict) else {}).items():
        why = _default_value_error(value)
        if why:
            return (f"spec.defaults.{metric} {why}; the exporter skips the "
                    f"whole rendered file otherwise")
    filters = spec.get("stateFilters")
    for name, sf in (filters if isinstance(filters, dict) else {}).items():
        why = _state_filter_error(sf)
        if why:
            return (f"spec.stateFilters.{name}{why}; the exporter skips the "
                    f"whole rendered file otherwise")
    return ""


def _default_value_error(value: Any) -> str:
    """Why *value* is not a ``defaults`` value the exporter decodes, as far
    as its JSON type tells, or ``""``.

    The exporter reads each root ``defaults`` value's text through
    parseThresholdNumber (#3250): a sequence / mapping makes it refuse the
    whole file, which is what this names. A scalar never does — a string or
    a bool it does not read (``foo``, ``true``) refuses that key alone and is
    the exporter's verdict to report (served-values' ``root_defaults_refused``,
    da-guard's ``root_default_refused``), not this tool's; ``"80"`` is read
    as 80. An int past float64's range has no value the writer can send.
    """
    if value is None or isinstance(value, (str, bool)):
        return ""
    if isinstance(value, int):
        # The exporter's float64: an int past 18 digits is sent as the
        # float it rounds to (`_lib_confedit.number_value`), so one past
        # float64 max (Go-measured: (2^54-1)*2^970 and up, either sign) has
        # no value there. `float()` rounds the same way and raises there;
        # it also needs no `str()`, which is capped at 4300 digits.
        try:
            float(value)
        except OverflowError:
            return "must be a number within float64 range"
        return ""
    if isinstance(value, float):
        return ""
    return "must be a number"


def _state_filter_error(sf: Any) -> str:
    """Why *sf* is not a ``StateFilter`` the exporter decodes, or ``""``.

    ``StateFilter`` is ``{reasons: []string, severity: string,
    default_state: string}``, other keys ignored. Oracle-measured: a
    non-mapping filter, a non-sequence ``reasons`` or one holding a
    sequence / mapping, and a sequence / mapping ``severity`` /
    ``default_state`` are refused. Any scalar (null included) is a string.
    The returned text follows the filter's name.
    """
    if sf is None or isinstance(sf, set):
        return ""
    if not isinstance(sf, dict):
        return " must be a mapping"
    reasons = sf.get("reasons")
    if reasons is not None:
        if not isinstance(reasons, list):
            return ".reasons must be a list"
        # `tuple`: an item of `!!omap` / `!!pairs`, a one-key mapping to
        # the exporter.
        if any(isinstance(r, (list, tuple, dict, set)) for r in reasons):
            return ".reasons must be a list of strings"
    for key in ("severity", "default_state"):
        if isinstance(sf.get(key), (list, dict, set)):
            return f".{key} must be a string"
    return ""


# ── Main ─────────────────────────────────────────────────────────────

@exit_on_output_write_error
def main() -> int:
    """CLI entry point: Lightweight ThresholdConfig CRD → YAML renderer."""
    parser = argparse.ArgumentParser(
        description="Lightweight ThresholdConfig CRD → YAML assembler.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--config-dir", required=True,
        help="Output directory for rendered YAML files",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="One-shot mode: render all CRs and exit",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview mode: show what would be written without writing",
    )
    parser.add_argument(
        "--namespace", type=str, default="",
        help="Watch only this namespace (default: all namespaces)",
    )
    parser.add_argument(
        "--render-cr", type=str, default="",
        help="Render a single CR YAML file (offline, no K8s required)",
    )
    parser.add_argument(
        "--kubeconfig", type=str, default="",
        help="Path to kubeconfig file (default: in-cluster or ~/.kube/config)",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()
    config_dir = Path(args.config_dir).resolve()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Offline render mode (no K8s client needed)
    if args.render_cr:
        # #2529: SIGINT / SIGTERM stop the render — da-crdecode included —
        # and end the process as the signal would. Before the write nothing
        # is written; during it the write completes first.
        # ⛔ The try is OUTSIDE the with: a signal that arrives as the
        # handlers are being restored is raised from `__exit__`.
        try:
            with _handling_signals(_render_signal_handler):
                return render_cr_file(
                    Path(args.render_cr), config_dir, dry_run=args.dry_run)
        except _Interrupted as e:
            _ignore_signals()  # before the log line: a second signal waits
            log.error("Received signal %d, stopped rendering %s",
                      e.signum, args.render_cr)
            return _die_by_signal(e.signum)

    with _handling_signals(_signal_handler):
        return _controller_main(args, config_dir)


def _controller_main(args: argparse.Namespace, config_dir: Path) -> int:
    """``--once`` and watch mode (both need a Kubernetes client)."""
    # K8s client required for watch/once modes
    if not HAS_K8S:
        log.error("kubernetes Python client is required.  "
                   "pip install kubernetes")
        return EXIT_CALLER_ERROR

    # Load kubeconfig
    try:
        if args.kubeconfig:
            config.load_kube_config(config_file=args.kubeconfig)
        else:
            try:
                config.load_incluster_config()
                log.info("Using in-cluster config")
            except config.ConfigException:
                config.load_kube_config()
                log.info("Using kubeconfig")
    except Exception as e:
        log.error("Failed to load Kubernetes config: %s", e)
        return EXIT_CALLER_ERROR

    api = client.CustomObjectsApi()

    if args.once:
        return run_once(api, config_dir, dry_run=args.dry_run,
                        namespace=args.namespace)

    # Default: watch mode
    # Do initial sync first. A refused tree does not stop the controller
    # (the next cycle judges again, #3206); a failed list does.
    rc, _refused = _sync(api, config_dir, dry_run=args.dry_run,
                         namespace=args.namespace)
    if rc != 0:
        return rc

    run_watch(api, config_dir, dry_run=args.dry_run,
              namespace=args.namespace)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
