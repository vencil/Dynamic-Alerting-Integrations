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
"""

import argparse
import hashlib
import json
import logging
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
from datetime import date, datetime, timezone
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
from _lib_yaml_keys import (  # noqa: E402  (#2216, #2331)
    dump_for_rewrite,
    load_for_rewrite,
)

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
        for sig, handler in self.previous.items():
            signal.signal(sig, handler)
        return False


def _die_by_signal(signum: int) -> int:
    """End the process the way *signum* would have ended it (rc 128+N in a
    shell), as `ops/patch_config` does before its write. Returns only where
    the signal does not end the process."""
    for sig in (signal.SIGINT, signal.SIGTERM):  # a second one changes nothing
        signal.signal(sig, signal.SIG_IGN)
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)
    return EXIT_CALLER_ERROR


# ── CR → YAML rendering ─────────────────────────────────────────────

def render_cr_to_yaml(cr: dict) -> str:
    """Convert a ThresholdConfig CR spec into tenant YAML content.

    The output format matches hand-written conf.d/<tenant>.yaml exactly.

    Tenant ids must arrive as ``str`` (#2216): the dump quotes a text
    key YAML would retype (``'010':``), so the exporter reads back the same
    id. Both callers hand over JSON, whose keys are strings: the API path,
    and ``render_cr_file`` (decoded as Kubernetes clients decode it, #2476).

    Values dump as ``yaml.safe_dump`` would: both callers hand over
    JSON-decoded data, so ``dump_for_rewrite``'s plain-text case
    (``RawPlain``) never applies here.
    """
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

    header = (
        f"{_HEADER_PREFIX}"
        f"{metadata.get('namespace', '?')}/{metadata.get('name', '?')}\n"
        f"# DO NOT EDIT — changes will be overwritten on next reconcile.\n"
    )

    return header + dump_for_rewrite(
        doc, default_flow_style=False, allow_unicode=True, sort_keys=False,
    )


def _content_sha256(content: str) -> str:
    """Compute SHA-256 hex digest of string content."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _output_filename(cr: dict) -> str:
    """Derive the output filename from a CR's metadata.name."""
    name = cr.get("metadata", {}).get("name", "unknown")
    return f"{name}.yaml"


# ── File writing ─────────────────────────────────────────────────────

def write_rendered(
    config_dir: Path,
    filename: str,
    content: str,
    *,
    dry_run: bool = False,
) -> bool:
    """Write rendered YAML to config-dir.  Returns True if file changed."""
    dest = config_dir / filename

    # Skip write if content is identical.
    # #1789: the idempotence read is wrapped TOO, because it reads the very
    # file this function exists to produce — when `dest` is a directory this
    # is where the run actually dies (measured), and "cannot write <dest>: Is
    # a directory" is the true statement about it. Contrast
    # `ops/config_history._load_history`, whose read is left alone: that one
    # reads a DIFFERENT path (the history state) from the one being written.
    with output_write(dest, flag="--config-dir"):
        if dest.exists():
            existing = dest.read_text(encoding="utf-8")
            if existing == content:
                return False

    if dry_run:
        log.info("DRY-RUN: would write %s (%d bytes)", filename, len(content))
        return True

    # #1789: the wrapper is given the PARENT — the directory this mkdir
    # actually creates. Handing it the output FILE makes the message a
    # sentence about a path nobody was creating (worked example in
    # `_lib_io.output_write`). The ancestor rule still converts the
    # failure, and the write below keeps naming the file.
    with output_write(config_dir, flag="--config-dir", action="create directory"):
        config_dir.mkdir(parents=True, exist_ok=True)
    # The 0644 chmod is inside the same block as the write: the rendered file
    # is read by the exporter, and a chmod that failed would leave a mode the
    # tool did not choose.
    with output_write(dest, flag="--config-dir"):
        dest.write_text(content, encoding="utf-8", newline="\n")
        os.chmod(dest, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
    return True


def remove_rendered(
    config_dir: Path,
    filename: str,
    *,
    dry_run: bool = False,
) -> bool:
    """Remove a rendered file (CR deleted).  Returns True if file existed."""
    dest = config_dir / filename
    if not dest.exists():
        return False
    if dry_run:
        log.info("DRY-RUN: would delete %s", filename)
        return True
    dest.unlink()
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

def reconcile_one(
    cr: dict,
    config_dir: Path,
    *,
    dry_run: bool = False,
    api: Any = None,
    cli: bool = False,
) -> None:
    """Reconcile a single ThresholdConfig CR.

    *cli* says which of the two callers this is: ``render_cr_file``
    (``--render-cr``, one CR, the rc is the operator's answer) or the
    controller loop (``run_once`` / ``run_watch``, one item among many).
    ⛔ It is an EXPLICIT flag and not ``api is None``, because the controller
    passes ``api=None`` too whenever ``--dry-run`` is set — inferring the
    caller from it made a dry-run controller re-raise, which aborts
    ``run_once`` after the first bad CR and turns ``run_watch`` into an
    endless "Watch interrupted … Reconnecting" loop.
    """
    name = cr["metadata"]["name"]
    namespace = cr["metadata"].get("namespace", "default")
    filename = _output_filename(cr)

    try:
        content = render_cr_to_yaml(cr)
        sha = _content_sha256(content)
        changed = write_rendered(config_dir, filename, content,
                                 dry_run=dry_run)

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
        if cli:
            raise
        log.error("Failed to reconcile %s/%s: %s", namespace, name, e)
        if api and not dry_run:
            update_cr_status(api, cr, "Error", message=str(e)[:200])


def run_once(
    api: Any,
    config_dir: Path,
    *,
    dry_run: bool = False,
    namespace: str = "",
) -> int:
    """One-shot: list all ThresholdConfig CRs and render them."""
    try:
        if namespace:
            result = api.list_namespaced_custom_object(
                CRD_GROUP, CRD_VERSION, namespace, CRD_PLURAL)
        else:
            result = api.list_cluster_custom_object(
                CRD_GROUP, CRD_VERSION, CRD_PLURAL)
    except Exception as e:
        log.error("Failed to list ThresholdConfig resources: %s", e)
        return EXIT_CALLER_ERROR

    items = result.get("items", [])
    log.info("Found %d ThresholdConfig resource(s)", len(items))

    for cr in items:
        reconcile_one(cr, config_dir, dry_run=dry_run,
                      api=None if dry_run else api)

    return EXIT_OK


def run_watch(
    api: Any,
    config_dir: Path,
    *,
    dry_run: bool = False,
    namespace: str = "",
) -> None:
    """Watch loop: react to ThresholdConfig changes in real time."""
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

                if event_type in ("ADDED", "MODIFIED"):
                    reconcile_one(cr, config_dir, dry_run=dry_run,
                                  api=None if dry_run else api)
                elif event_type == "DELETED":
                    filename = _output_filename(cr)
                    removed = remove_rendered(config_dir, filename,
                                              dry_run=dry_run)
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


def decode_cr(cr_path: Path) -> tuple[Any, list[str]]:
    """The CR in *cr_path* as Kubernetes clients decode it, and da-crdecode's
    stderr lines (empty on a normal run).

    Raises :class:`CrDecodeError` when the binary is missing, cannot be run,
    does not finish within ``CRDECODE_TIMEOUT_SECONDS``, exits non-zero (the
    file cannot be read, the conversion refuses it, or it holds more than one
    document) or prints something that is not its JSON contract.
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
    return docs[0], lines


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


def _plain_tag(text: str) -> str:
    """The tag YAML gives *text* written unquoted."""
    return yaml.resolver.Resolver().resolve(
        yaml.ScalarNode, text, (True, False))


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


def _as_text(value: Any) -> str | None:
    """*value* from the CR read as written, as the text it was written as:
    a ``str`` (a plain scalar is RawPlain, its source text), a ``date`` in
    its ISO spelling (``!!timestamp 2024-01-01``); None for anything else
    (null, a number or a ``datetime`` from an explicit tag, bytes, ...)."""
    if isinstance(value, str):
        return str(value)
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    return None


def _shape_name(value: Any) -> str:
    """How a value's structure is named in a :func:`_key_divergence` error.
    A tuple is an item of ``!!omap`` / ``!!pairs`` as written."""
    if isinstance(value, dict):
        return "a mapping"
    if isinstance(value, (set, frozenset)):
        return "a set"
    if isinstance(value, list):
        return f"a list of {len(value)}"
    if isinstance(value, tuple):
        return "a (key, value) pair"
    return "a scalar"


#: Appended to a divergence error when the CR uses `<<` merges: quoting does
#: not help when the two readings differ because of the merge itself.
_MERGE_NOTE = ("; the CR uses << merges: if the difference comes from a << "
               "written after explicit keys (on the Kubernetes side it "
               "overrides them), move it before them or do not use merges")


def _merge_note(cr_path: Path) -> str:
    """:data:`_MERGE_NOTE` when the CR has a ``<<`` merge key anywhere, else
    ``""``. Read from the composed node graph (no value is built); any
    failure to read gives ``""`` — the note is an explanation only."""
    try:
        with open(cr_path, encoding="utf-8") as fh:
            root = yaml.compose(fh, Loader=yaml.SafeLoader)
    except Exception:  # noqa: BLE001 — a note only
        return ""
    seen, stack = set(), [root]
    while stack:
        node = stack.pop()
        if node is None or id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, yaml.MappingNode):
            for key, value in node.value:
                if key.tag == "tag:yaml.org,2002:merge":
                    return _MERGE_NOTE
                stack.extend((key, value))
        elif isinstance(node, yaml.SequenceNode):
            stack.extend(node.value)
    return ""


def _key_divergence(decoded: Any, written: Any, path: str) -> str:
    """#2476: why the keys da-crdecode read under *path* differ from the
    keys as written, at any depth, or ``""``.

    An unquoted ``010:`` is the key ``8`` to Kubernetes, ``yes:`` is
    ``true``: rendering either would rename it — a tenant id, a metric
    name — and ``8:`` next to ``010:`` would become one key. Two real
    readings are compared mapping by mapping, by their key sets; nothing
    about YAML typing is decided here. Values are not compared (they are
    rendered as decoded), but the two readings must have the same
    structure: where one has a mapping or a list and the other has not, or
    two lists differ in length, the CR is refused (fail-closed) — the keys
    below cannot be paired. A ``!!set`` as written is a mapping of keys.
    A key written twice with the same text is one key in both readings.
    """
    if isinstance(written, (set, frozenset)):
        written = dict.fromkeys(written)
    if isinstance(decoded, dict) and isinstance(written, dict):
        by_text = {}
        for key, value in written.items():
            by_text[_as_text(key)] = value
        if set(by_text) != set(decoded):
            only_written = sorted(repr(k) for k in set(by_text) - set(decoded))
            only_read = sorted(repr(k) for k in set(decoded) - set(by_text))
            return (f"{path}: the keys as written ("
                    + (", ".join(only_written) or "none")
                    + ") are not the keys Kubernetes reads ("
                    + (", ".join(only_read) or "none")
                    + "); rendering would rename them. Quote each such key "
                    "so both readings are the same text")
        for key, value in decoded.items():
            why = _key_divergence(value, by_text[key], f"{path}.{key}")
            if why:
                return why
        return ""
    if isinstance(decoded, list) and isinstance(written, list) \
            and len(decoded) == len(written):
        for i, (d, w) in enumerate(zip(decoded, written)):
            why = _key_divergence(d, w, f"{path}[{i}]")
            if why:
                return why
        return ""
    if isinstance(decoded, (dict, list)) or isinstance(written, (dict, list)):
        return (f"{path}: Kubernetes reads {_shape_name(decoded)} here but "
                f"the CR as written holds {_shape_name(written)}, so the keys "
                "below cannot be compared; this tool refuses such a CR")
    return ""


def _profile_divergence(decoded: Any, written: Any) -> str:
    """#2476: why a tenant's ``_profile`` reads differently to Kubernetes
    than as written, or ``""``.

    ``_profile`` names a profile, as a tenant id names a tenant: an unquoted
    ``_profile: 010`` is the number 8 to Kubernetes, ``yes`` is ``true``,
    and rendering that would point the tenant at a profile that does not
    exist. Compared as :func:`_key_divergence` compares keys (two real
    readings); a null on both sides is no profile.
    """
    if not isinstance(decoded, dict) or not isinstance(written, dict):
        return ""
    by_text = {_as_text(k): v for k, v in written.items()}
    for tenant, values in decoded.items():
        as_written = by_text.get(tenant)
        if not isinstance(values, dict) or not isinstance(as_written, dict) \
                or "_profile" not in as_written:
            continue
        raw = as_written["_profile"]
        read = values.get("_profile")
        if raw is None and read is None:
            continue
        if not isinstance(read, str) or _as_text(raw) != read:
            return (f"spec.tenants.{tenant}._profile is read by Kubernetes "
                    f"as {read!r}, which is not the text written in the CR "
                    f"({raw!r}): the tenant would point at another profile. "
                    "Quote it so both are the same")
    return ""


def _in_written_order(decoded: Any, written: Any) -> Any:
    """*decoded* (da-crdecode's JSON, every object's keys sorted) with each
    mapping's keys in the order the CR wrote them, values untouched.

    A key is matched to the written one by its text; keys with no written
    counterpart keep their (sorted) order, after the matched ones. A list is
    walked item by item when both sides have the same length.
    """
    if isinstance(decoded, dict) and isinstance(written, dict):
        by_text = {}
        for key, value in written.items():
            text = _as_text(key)
            if text is not None and text not in by_text:
                by_text[text] = value
        order = [k for k in by_text if k in decoded]
        order += [k for k in decoded if k not in by_text]
        return {k: _in_written_order(decoded[k], by_text.get(k))
                for k in order}
    if (isinstance(decoded, list) and isinstance(written, list)
            and len(decoded) == len(written)):
        return [_in_written_order(d, w) for d, w in zip(decoded, written)]
    return decoded


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
    ``namespace`` and the tenant ids — must read the same as written
    (``_lib_yaml_keys``) and as decoded, or the CR is refused: an unquoted
    ``010:`` tenant would otherwise be renamed. The CR as written also gives
    the key order. What this path checks on top of the controller path is
    below; it does not reproduce the API server's validation.
    """
    try:
        cr, warn_lines = decode_cr(cr_path)
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
    # #2476: the CR as written (`_lib_yaml_keys`: keys and plain scalars as
    # their source text), to compare the ids with da-crdecode's reading and
    # to keep the CR's key order. Not used for any value.
    try:
        with open(cr_path, encoding="utf-8") as fh:
            as_written = load_for_rewrite(fh)
    except RecursionError:
        log.error("Failed to parse %s: nested too deeply for this tool to "
                  "read", cr_path)
        return EXIT_CALLER_ERROR
    except Exception as e:  # noqa: BLE001 — any failure: nothing to compare
        log.error("%s: Kubernetes' YAML→JSON conversion reads this CR, but "
                  "this tool cannot read it as written (%s: %s), so its ids "
                  "cannot be compared with that reading", cr_path,
                  type(e).__name__, e)
        return EXIT_CALLER_ERROR
    written_meta = (as_written.get("metadata")
                    if isinstance(as_written, dict) else None)
    if not isinstance(written_meta, dict):
        written_meta = {}
    if _as_text(written_meta.get("name")) != name:
        log.error("%s: metadata.name is read by Kubernetes as %r, which is "
                  "not the text written in the CR (%r). Quote it so both "
                  "are the same%s", cr_path, name, written_meta.get("name"),
                  _merge_note(cr_path))
        return EXIT_CALLER_ERROR
    namespace = metadata.get("namespace")
    if namespace is not None \
            and _as_text(written_meta.get("namespace")) != namespace:
        log.error("%s: metadata.namespace is read by Kubernetes as %r, "
                  "which is not the text written in the CR (%r). Quote it "
                  "so both are the same%s", cr_path, namespace,
                  written_meta.get("namespace"), _merge_note(cr_path))
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
    written_spec = (as_written.get("spec")
                    if isinstance(as_written, dict) else None)
    if not isinstance(written_spec, dict):
        written_spec = {}
    why = ""
    if isinstance(spec.get("tenants"), dict):
        why = (_key_divergence(spec["tenants"], written_spec.get("tenants"),
                               "spec.tenants")
               or _profile_divergence(spec["tenants"],
                                      written_spec.get("tenants")))
    if why:
        log.error("%s: %s%s", cr_path, why, _merge_note(cr_path))
        return EXIT_CALLER_ERROR
    if spec:
        cr["spec"] = spec = _in_written_order(spec, written_spec)
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


# ── yaml.v3's plain-scalar → number resolution (#2395) ──────────────
#
# The exporter decodes `defaults` with gopkg.in/yaml.v3 (v3.0.1): a plain
# scalar lands in a float64 only if `resolve()` (resolve.go) types it
# int / float. What follows is that function's number path, step for step,
# with the Go strconv calls it makes (internal/strconv/atoi.go, atof.go).
# `test_the_rows_match_the_exporter` re-measures it against da-guard.
# ⚠️ Not claimed for a mantissa thousands of digits long: Go's ParseFloat
# reads those differently from Python's `float()` (`1` + 5000 `0` +
# `e-1000` is 1e-201 in Go, inf here), so such a value may be refused.

_V3_NAMED_FLOATS = frozenset(
    p + w for w in (".inf", ".Inf", ".INF") for p in ("", "+", "-")
) | frozenset((".nan", ".NaN", ".NAN"))
# resolve.go `yamlStyleFloat`.
_V3_STYLE_FLOAT = re.compile(
    r"[-+]?(\.[0-9]+|[0-9]+(\.[0-9]*)?)([eE][-+]?[0-9]+)?")
# Go strconv's digit value of an ASCII byte; anything else is not a digit.
_GO_DIGIT_VALUE = {c: int(c, 36) for c in
                   "0123456789abcdefghijklmnopqrstuvwxyz"
                   "ABCDEFGHIJKLMNOPQRSTUVWXYZ"}
# What Go's ParseFloat accepts from a text starting with `.` (no sign, no
# 0x prefix, not inf/nan), once `_go_underscore_ok` has passed it.
_GO_DOT_FLOAT = re.compile(r"\.[0-9]+([eE][-+]?[0-9]+)?")


def _go_underscore_ok(s: str) -> bool:
    """Go ``strconv.underscoreOK``: each ``_`` sits between digits (or
    between a base prefix and a digit)."""
    if s[:1] in ("+", "-"):
        s = s[1:]
    saw, i, hexa = "^", 0, False
    if len(s) >= 2 and s[0] == "0" and s[1] in "bBoOxX":
        saw, i, hexa = "0", 2, s[1] in "xX"
    for c in s[i:]:
        if "0" <= c <= "9" or hexa and c in "abcdefABCDEF":
            saw = "0"
        elif c == "_":
            if saw != "0":
                return False
            saw = "_"
        elif saw == "_":
            return False
        else:
            saw = "!"
    return saw != "_"


def _go_parse_uint(s: str, base: int):
    """Go ``strconv.ParseUint(s, base, 64)`` on a text without ``_``:
    the value, or None on any error."""
    if not s:
        return None
    if base == 0:
        base = 10
        if s[0] == "0":
            prefix = {"b": 2, "B": 2, "o": 8, "O": 8,
                      "x": 16, "X": 16}.get(s[1:2])
            if len(s) >= 3 and prefix:
                base, s = prefix, s[2:]
            else:
                base, s = 8, s[1:]
    n = 0
    for c in s:
        # Go reads BYTES: only ASCII 0-9 / a-z / A-Z are digits. ⛔ No
        # `c.lower()` on the raw char — `"İ".lower()` is two chars.
        d = _GO_DIGIT_VALUE.get(c, 99)
        if d >= base:
            return None
        n = n * base + d
        # Go's cutoff: stop at the first overflow (ErrRange) instead of
        # carrying a bignum to the end of a very long text.
        if n >= 1 << 64:
            return None
    return n


def _go_parse_int(s: str, base: int):
    """Go ``strconv.ParseInt(s, base, 64)``: the value, or None."""
    neg = s[:1] == "-"
    un = _go_parse_uint(s[1:] if s[:1] in ("+", "-") else s, base)
    if un is None or un > (1 << 63 if neg else (1 << 63) - 1):
        return None
    return -un if neg else un


def _go_parse_float_ok(s: str) -> bool:
    """Go ``strconv.ParseFloat(s, 64)`` succeeds, for the two shapes
    resolve() hands it: a `.`-led text, or one `_V3_STYLE_FLOAT` matched.
    Overflow (±Inf) is an error; underflow is not. Judged by Python's
    `float()`, which matches Go except on a mantissa thousands of digits
    long (see the section comment)."""
    try:
        return not math.isinf(float(s.replace("_", "")))
    except ValueError:
        return False


def _v3_reads_as_number(text: str) -> bool:
    """yaml.v3 resolves the PLAIN scalar *text* to an int or a float."""
    if text in _V3_NAMED_FLOATS:
        return True
    head = text[:1]
    if head == ".":
        # `strconv.ParseFloat(in)` on the text as written, `_` included.
        return (_go_underscore_ok(text)
                and bool(_GO_DOT_FLOAT.fullmatch(text.replace("_", "")))
                and _go_parse_float_ok(text))
    if not head or head not in "+-0123456789":
        return False  # hint 'M' (only the named table) or none: a string
    plain = text.replace("_", "")
    if (_go_parse_int(plain, 0) is not None
            or _go_parse_uint(plain, 0) is not None):
        return True
    if _V3_STYLE_FLOAT.fullmatch(plain) and _go_parse_float_ok(plain):
        return True
    # Fallbacks after the float: an explicit base, where Go's ParseInt
    # takes a sign after the prefix (`0b-1`).
    for prefix, base in (("0b", 2), ("0o", 8)):
        if plain.startswith(prefix):
            rest = plain[2:]
            return (_go_parse_int(rest, base) is not None
                    or _go_parse_uint(rest, base) is not None)
        if plain.startswith("-" + prefix):
            return _go_parse_int("-" + plain[3:], base) is not None
    return False


def _default_value_error(value: Any) -> str:
    """Why *value* is not a ``defaults`` value the exporter decodes, or ``""``.

    The exporter reads ``defaults`` as ``map[string]float64``. Oracle
    (``da-guard served-values``, rc 3 = file refused): null and numbers are
    read; a bool, a date, a sequence / mapping, and ANY quoted scalar —
    ``"80"`` included — make it refuse the whole file.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "must be a number, not a boolean"
    if isinstance(value, int):
        # An int (`!!int "…"`) is dumped as its decimal digits, which
        # yaml.v3 reads through ParseFloat when it passes 64 bits: refused
        # once it rounds past float64 max (Go-measured: (2^54-1)*2^970 and
        # up, either sign). `float()` rounds the same way and raises there;
        # it also needs no `str()`, which is capped at 4300 digits.
        try:
            float(value)
        except OverflowError:
            return "must be a number within float64 range"
        return ""
    if isinstance(value, float):
        return ""
    if not isinstance(value, str):
        return "must be a number"
    # A str (da-crdecode's JSON, #2476) is dumped plain only where PyYAML
    # would not retype it; otherwise it is QUOTED, and yaml.v3 does not
    # decode a quoted scalar into a number.
    if _plain_tag(value) != "tag:yaml.org,2002:str":
        return "must be a number, not a quoted string"
    if not _v3_reads_as_number(value):
        return f"must be a number, not {value!r}"
    return ""


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
        with _handling_signals(_render_signal_handler):
            try:
                return render_cr_file(
                    Path(args.render_cr), config_dir, dry_run=args.dry_run)
            except _Interrupted as e:
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
    # Do initial sync first
    rc = run_once(api, config_dir, dry_run=args.dry_run,
                  namespace=args.namespace)
    if rc != 0:
        return rc

    run_watch(api, config_dir, dry_run=args.dry_run,
              namespace=args.namespace)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
