#!/usr/bin/env python3
"""patch_config.py — Patch threshold-config ConfigMap for a specific tenant.

Supports both legacy (single config.yaml key) and multi-file (per-tenant YAML keys)
ConfigMap structures: a '_defaults' key (.yaml/.yml, any casing) means
multi-file, else 'config.yaml' means legacy. The tenant's key is the one whose
`tenants:` declares it. This tool's reading of the ConfigMap is not the
exporter's; known divergences are tracked in #1950.

Usage: patch_config.py <tenant> <metric_key> <value>
       patch_config.py --diff [--json] <tenant> <metric_key> <value>

Three-state logic (for keys `_defaults.yaml` gives a value to, under `defaults:`):
  - Custom value:  patch_config.py db-a mysql_connections 50
  - Default (delete key): patch_config.py db-a mysql_connections default
  - Disable:       patch_config.py db-a mysql_connections disable

  ⚠️ For a key `_defaults.yaml` only DECLARES (its `optional_overrides:` list)
  there is no platform value to fall back to, so `default` does not restore one
  — deleting the key means no value and no series at all (#1321).

Diff preview (terraform plan analogy):
  - patch_config.py --diff db-a mysql_connections 50
  Shows the value to be written and whether apply would write anything,
  without applying. It does not show the current value: that is the
  exporter's to tell, not this tool's (#1950).

Dimensional metrics (Phase 2B):
  - patch_config.py db-a 'redis_queue_length{queue="tasks"}' 500
  - patch_config.py db-a 'redis_db_keys{db="db0"}' disable
  Note: Shell quoting is important — wrap the metric key in single quotes.

Apply is verified by the running exporter pods, then rolled back if it does
not hold (#1950 route E). The details, the exit codes and what this cannot
see are in `VERIFY_HELP` below (also printed by --help).
"""
import argparse
import contextlib
import re
import select
import signal
import subprocess
import time
import yaml
import sys
import json
import tempfile
import os

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))  # Repo subdir layout
from _lib_exitcodes import EXIT_CALLER_ERROR, EXIT_OK, EXIT_VIOLATION  # noqa: E402
from _lib_python import format_json_report  # noqa: E402
from _lib_confd import (  # noqa: E402
    has_yaml_extension,
    is_defaults_name,
    is_hidden_name,
    is_reserved_name,
)

LEGACY_KEY = "config.yaml"
_NULL = "tag:yaml.org,2002:null"
_MERGE = "tag:yaml.org,2002:merge"

# Where the ConfigMap and the exporter pods are. Taken from the chart
# (helm/threshold-exporter/templates/{service,deployment}.yaml: namespace,
# pod label, container port name); tests/ops/test_patch_config_verify.py pins
# them against it. The exporter three are the defaults of their flags.
NAMESPACE = "monitoring"
CONFIGMAP = "threshold-config"
EXPORTER_SELECTOR = "app=threshold-exporter"
EXPORTER_PORT = "http"
RELOAD_TIMEOUT_S = 120.0
POLL_INTERVAL_S = 2.0

# Exit codes of apply (sanctioned >= 3 extension, see _lib_exitcodes.py).
EXIT_VERIFY_FAILED = EXIT_VIOLATION  # the exporter shows another change; rolled back
EXIT_RELOAD_TIMEOUT = 3        # no reload seen on every pod in time; rolled back
EXIT_EXPORTER_UNREACHABLE = 4  # no pod / a pod could not be read
EXIT_ROLLBACK_FAILED = 5       # the rollback patch itself failed
EXIT_ABORTED_ROLLED_BACK = 6   # interrupt / unexpected error after the write; rolled back
EXIT_STATE_UNKNOWN = 7         # what the ConfigMap holds cannot be told

# Rollback attempts when only other keys changed under it (see `_send_rollback`).
ROLLBACK_CONFLICT_ATTEMPTS = 3

# Upper bound on a threshold key's `user_*` changes (see `verify_pod`).
THRESHOLD_KEY_MAX_CHANGES = 4

VERIFY_HELP = f"""\
apply (no --diff) is verified by every exporter pod before it succeeds:
  1. the new bytes of the one ConfigMap key equal the old ones -> no-op, exit 0,
     nothing is written or waited for.
  2. list the Running, not-terminating pods (--exporter-selector in
     --exporter-namespace) and read, via `kubectl get --raw
     .../pods/<pod>:<port>/proxy/...` (GET only), each pod's /api/v1/config
     `Last reload` and /metrics.
  3. kubectl patch, conditional on the ConfigMap's resourceVersion as read
     (changed since -> nothing is written, exit {EXIT_CALLER_ERROR}: re-run; if the call fails
     otherwise, the ConfigMap is re-read to tell whether it landed); wait
     until every pod's `Last reload` differs from the value read in 2 (pod
     clock against pod clock); --reload-timeout is the deadline that ends
     the wait, checked between polling passes.
  4. on each pod, compare every `user_*` series (name + labels -> value) with 2.
     Any change on a series whose `tenant` is not <tenant> fails. For a key
     not starting with `_`, more than {THRESHOLD_KEY_MAX_CHANGES} changed series of <tenant> fails
     (unless <tenant> had none before: a new tenant). For a `_` key,
     <tenant>'s own series are not judged, only listed (stderr / --json).
     da_config_parse_failure_total{{file_basename}} fails if it rises for a
     basename that was absent or 0 before, or for the patched key; a rise on
     another already-failing basename is only a warning. Finally the
     ConfigMap must still be at the version the write produced: if anyone
     wrote it meanwhile, the reloads may not be of these bytes -> fails.
  5. any failure in 3-4, Ctrl-C / SIGTERM or any other error from the write
     call on -> the old bytes are patched back (the key is removed if it did
     not exist) unless the ConfigMap is seen to hold the old bytes, or cannot
     be told apart after a failed write call; pods that reloaded are waited
     for once more (best effort); status and exit code follow what the
     ConfigMap then holds. The rollback is conditional on the version the
     write produced: if only other keys changed, it is retried on the fresh
     version a bounded number of times; if another writer changed the patched
     key itself, it is not rolled back (exit {EXIT_STATE_UNKNOWN}).
signals: from the write on, Ctrl-C / SIGTERM is acted on before the next
  exporter call; from the rollback on they are ignored until the process
  ends. SIGKILL cannot be handled: the ConfigMap may stay at the new bytes
  and no envelope is printed.
exit codes: 0 ok or no-op; {EXIT_VERIFY_FAILED} verification failed, rolled back;
  {EXIT_CALLER_ERROR} caller error, nothing written; {EXIT_RELOAD_TIMEOUT} no reload in time, rolled back;
  {EXIT_EXPORTER_UNREACHABLE} exporter unreachable (before the write: nothing written; after: rolled
  back); {EXIT_ROLLBACK_FAILED} rollback failed - the ConfigMap may still hold the new bytes;
  {EXIT_ABORTED_ROLLED_BACK} interrupted or an unexpected error after the write, rolled back;
  {EXIT_STATE_UNKNOWN} what the ConfigMap holds cannot be told (not readable, or neither
  version), or another writer changed the patched key after this write (not
  rolled back) - check it by hand. Ctrl-C / SIGTERM before the write ends
  as it normally would.
needs: list pods, get pods/proxy in the exporter namespace (besides get/patch
  on the ConfigMap).
not seen (known residuals): a future `expires:` in the same key rewritten so
  it no longer parses (fail-open) looks identical in /metrics before and after; a
  change of <tenant>'s other series within the bound above; under a `_` key,
  any change of <tenant>'s own series (a wrong `_profile` that drops its
  thresholds is listed, not rolled back); a write the exporter drops without
  counting a parse failure (the target just keeps its old value); a pod that
  starts after step 2; a reload of a version older than the one read (a
  write made just before apply read the ConfigMap, not yet delivered to the
  pod), or one in the same second as the previous reload; the legacy `-config` single-file
  mode reloads nothing on a broken file, so that only ever ends as a timeout.
wrongly failed: a write that FIXES a key the exporter could not parse (its
  counter keeps rising until the reload); another tenant's `expires:` passing
  while apply waits (its series change) - retry.
"""


class ConfigMapShapeError(Exception):
    """The ConfigMap cannot be read unambiguously; refused instead of guessed."""


class _Unparseable(ConfigMapShapeError):
    """Locating skips a key whose read raises this."""


class KubectlError(Exception):
    """kubectl could not be run, failed, or printed something that is not JSON."""


def run_cmd(cmd):
    """Execute a command safely using list arguments (no shell=True)."""
    if isinstance(cmd, str):
        import shlex
        cmd = shlex.split(cmd)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise KubectlError(f"Error executing: {' '.join(cmd)}\n{exc}") from exc
    if result.returncode != 0:
        raise KubectlError(f"Error executing: {' '.join(cmd)}\n{result.stderr}")
    return result.stdout.strip()


def _cm_keys(cm_data):
    data = cm_data.get("data") if isinstance(cm_data, dict) else cm_data
    if not isinstance(data, (dict, type(None))):
        raise ConfigMapShapeError("threshold-config `data` is not a mapping.")
    return data or {}


def read_node(text, label, strict=True):
    """Root node of the FIRST document in `text` (None if there is none);
    later documents are not read. Refused: a syntax error in what is read,
    nesting too deep to walk, and — when `strict` — a duplicate key in any
    mapping."""
    try:
        root = next(yaml.compose_all(text or "", Loader=yaml.SafeLoader), None)
        if strict:
            _refuse_duplicate_keys(root, label, set())
    except yaml.YAMLError as exc:
        raise _Unparseable(f"{label} does not parse as YAML: {exc}") from exc
    except RecursionError as exc:
        raise _Unparseable(f"{label} is nested too deeply.") from exc
    return root


def _refuse_duplicate_keys(node, label, seen):
    if id(node) in seen:  # an alias is the same node object
        return
    seen.add(id(node))
    children = []
    if isinstance(node, yaml.MappingNode):
        names = set()
        for k, v in node.value:
            if isinstance(k, yaml.ScalarNode):
                if k.value in names:
                    raise ConfigMapShapeError(
                        f"{label} has the key {k.value!r} twice in one mapping.")
                names.add(k.value)
            children += [k, v]
    elif isinstance(node, yaml.SequenceNode):
        children = node.value
    for child in children:
        _refuse_duplicate_keys(child, label, seen)


def _entries(node, label, strict=True):
    """{key text: value node} of a mapping node, {} for any other node. A null
    key is skipped; so is a non-scalar key unless `strict`. Refused: a merge
    (`<<`) key, a duplicate key, and — when `strict` — a non-scalar key."""
    if not isinstance(node, yaml.MappingNode):
        return {}
    out, dup = {}, None
    for k, v in node.value:
        if not isinstance(k, yaml.ScalarNode):
            if strict:
                raise ConfigMapShapeError(f"{label} has a non-scalar mapping key.")
            continue
        if k.tag == _MERGE:
            raise ConfigMapShapeError(
                f"{label} has a merge key `<<` on the path being read.")
        dup = k.value if k.value in out else dup
        if k.tag != _NULL:
            out[k.value] = v
    if dup is not None:
        raise _Unparseable(f"{label} has the key {dup!r} twice in one mapping.")
    return out


def _is_null(node):
    return isinstance(node, yaml.ScalarNode) and node.tag == _NULL


def _mapping_or_null(node):
    return node is None or _is_null(node) or isinstance(node, yaml.MappingNode)


def _text(node):
    """Source text of a scalar (None for null); YAML text of a collection."""
    if isinstance(node, yaml.ScalarNode):
        return None if _is_null(node) else node.value
    return yaml.serialize(node)


def find_defaults_key(data):
    """The `_defaults` key in any casing / either spelling (`is_defaults_name`),
    or None; two such keys → refused."""
    hits = sorted(k for k in data if is_defaults_name(k))
    if len(hits) > 1:
        raise ConfigMapShapeError(
            f"threshold-config has {len(hits)} keys that are all the platform "
            f"defaults file ({', '.join(hits)}); keep exactly one.")
    return hits[0] if hits else None


def detect_mode(cm_data):
    """'multi-file' if a `_defaults` key exists, 'legacy' if only `config.yaml`
    does. Refused: neither."""
    data = _cm_keys(cm_data)
    if find_defaults_key(data) is not None:
        return "multi-file"
    if LEGACY_KEY in data:
        return "legacy"
    raise ConfigMapShapeError(
        "threshold-config has neither a _defaults.yaml/.yml key (multi-file "
        f"layout) nor a {LEGACY_KEY} key (legacy layout); refusing to guess "
        f"its schema. Keys present: {sorted(data) or '(none)'}")


def is_carrier_candidate(key):
    return (has_yaml_extension(key) and not is_hidden_name(key)
            and not is_reserved_name(key))


def tenant_declarations(cm_data, skipped=None):
    """tenant id → set of keys whose `tenants:` declares it. Only the root and
    `tenants:` mappings are checked here; a target key is read strictly. A key
    that does not parse, or repeats a key there, is refused, or — given a
    `skipped` list — declares nothing and is added to it; a `<<` there is
    refused."""
    data = _cm_keys(cm_data)
    declared = {}
    for key in sorted(k for k in data if is_carrier_candidate(k)):
        try:
            root = read_node(data[key], key, strict=False)
            tenants = _entries(root, key, strict=False).get("tenants")
            ids = _entries(tenants, key, strict=False)
        except _Unparseable as exc:
            if skipped is None:
                raise
            skipped.append(str(exc))
            continue
        for tenant in ids:
            declared.setdefault(tenant, set()).add(key)
    return declared


def locate_tenant_key(cm_data, tenant):
    """The ONE key declaring `tenant`, or None; refused if two keys declare it,
    or if none does and a key could not be read."""
    skipped = []
    keys = sorted(tenant_declarations(cm_data, skipped).get(tenant, ()))
    if len(keys) > 1:
        raise ConfigMapShapeError(
            f"tenant '{tenant}' is declared by {len(keys)} threshold-config "
            f"keys: {', '.join(keys)}. Decide which key owns the tenant and "
            f"remove it from the others before patching.")
    if not keys and skipped:
        raise ConfigMapShapeError(
            f"no readable threshold-config key declares tenant '{tenant}', "
            f"and it cannot be ruled out that one of these declares it: "
            f"{'; '.join(skipped)}")
    return keys[0] if keys else None


def tenant_block(cm_data, tenant):
    """(key, {metric: value node}) of the key declaring `tenant`, (None, {})
    if no key does. A null block is an empty one: still declared."""
    key = locate_tenant_key(cm_data, tenant)
    if key is None:
        return None, {}
    root = read_node(_cm_keys(cm_data)[key], key)
    block = _entries(_entries(root, key)["tenants"], key)[tenant]
    if not _mapping_or_null(block):
        raise ConfigMapShapeError(
            f"tenant '{tenant}' in {key} is not a mapping of metric keys; "
            f"refusing to read or overwrite it.")
    return key, _entries(block, key)


def _is_default(value):
    return str(value).lower() == "default"


def _patch_carrier(cm_data, tenant, metric_key, value, create_key,
                   shared_key=None):
    """Patch the key declaring `tenant`. None = no-op: `default` with no value
    to remove. An emptied block is kept: it still declares the tenant. A key
    declaring more than one tenant is refused, except `shared_key`."""
    key, block = tenant_block(cm_data, tenant)
    if _is_default(value) and metric_key not in block:
        return None
    if key is None:
        key = create_key
        if not is_carrier_candidate(key):
            raise ConfigMapShapeError(
                f"patch-config does not create {key}: it only uses YAML keys "
                f"that are not dot-hidden or `_`-prefixed as tenant keys.")
    old = _cm_keys(cm_data).get(key) or ""
    declared = set(_entries(_entries(read_node(old, key), key).get("tenants"), key))
    if key != shared_key and len(declared | {tenant}) > 1:
        raise ConfigMapShapeError(
            f"{key} would declare tenants {sorted(declared | {tenant})}; "
            f"patch-config only writes a key that declares one. Edit {key} "
            f"by hand.")
    # ⚠️ The rewrite re-serialises the key's other values with YAML 1.1 types
    # and drops its comments.
    doc = yaml.safe_load(old) or {}
    tenants = doc.get("tenants") if isinstance(doc, dict) else []
    if tenants is None:
        tenants = doc["tenants"] = {}
    if not isinstance(tenants, dict):
        raise ConfigMapShapeError(
            f"{key} or its `tenants:` is not a YAML mapping; refusing to "
            f"overwrite it.")
    own = tenants[tenant] = tenants.get(tenant) or {}
    if _is_default(value):
        own.pop(metric_key, None)
    else:
        own[metric_key] = str(value)
    new = yaml.dump(doc, sort_keys=False)
    after = set(_entries(_entries(read_node(new, key), key).get("tenants"), key))
    if after != declared | {tenant}:
        raise ConfigMapShapeError(
            f"rewriting {key} would change the tenants it declares from "
            f"{sorted(declared | {tenant})} to {sorted(after)}; refusing.")
    return {"data": {key: new}}


def patch_legacy(cm_data, tenant, metric_key, value):
    """Legacy mode: a tenant no key declares is added to 'config.yaml'."""
    return _patch_carrier(cm_data, tenant, metric_key, value, LEGACY_KEY,
                          shared_key=LEGACY_KEY)


def patch_multifile(cm_data, tenant, metric_key, value):
    """Multi-file mode: a tenant no key declares gets a new `<tenant>.yaml`."""
    return _patch_carrier(cm_data, tenant, metric_key, value,
                          create_key=f"{tenant}.yaml")


def find_affected_alerts(metric_key):
    """Identify alert rules that reference this metric.

    Returns list of alert rule names (best-effort, based on naming convention).
    """
    # Strip dimensional suffix for matching
    base_metric = metric_key.split("{")[0] if "{" in metric_key else metric_key

    # Common alert naming patterns based on metric names
    alerts = []
    parts = base_metric.split("_")

    # Build likely alert name patterns
    # e.g., mysql_connections → MariaDBHighConnections
    # e.g., container_cpu → PodContainerHighCPU
    if len(parts) >= 2:
        # CamelCase conversion
        camel = "".join(p.capitalize() for p in parts)
        alerts.append(f"*{camel}*")

    return alerts


CURRENT_VALUE_NOTE = "Current value: not read by this tool (#1950); check the exporter."


def diff_preview(cm_data, mode, tenant, metric_key, value):
    """What apply would do, without doing it.

    `before` is None: the value in effect is not read here. Telling it for
    one key would mean redoing the exporter's key -> series mapping in
    Python, which #1950 ruled out; the exporter is where to look
    (CURRENT_VALUE_NOTE). `after` describes only the requested value, and
    `changed` is apply's own verdict (`build_patch`).
    """
    if _is_default(value):
        # Only what is written; what applies once the key is gone is the
        # exporter's to say, not this tool's.
        new_value, new_state = None, "key removed"
    elif str(value).lower() in ("disable", "disabled", "off", "false"):
        new_value, new_state = value, "disabled"
    else:
        new_value, new_state = value, f"custom: {value}"
    return {
        "tenant": tenant,
        "metric_key": metric_key,
        "configmap_mode": mode,
        "before": None,
        "after": {"value": new_value, "state": new_state},
        # The same patch apply would send: unchanged iff none.
        "changed": build_patch(cm_data, mode, tenant, metric_key,
                               value) is not None,
        "affected_alerts": find_affected_alerts(metric_key),
    }


def print_diff(diff):
    """Print human-readable diff preview."""
    print()
    print("=" * 55)
    print("  Config Change Preview (--diff)")
    print("=" * 55)
    print()
    print(f"  Tenant:   {diff['tenant']}")
    print(f"  Metric:   {diff['metric_key']}")
    print(f"  Mode:     {diff['configmap_mode']}")
    print()

    if diff["changed"]:
        print(f"  + After:  {diff['after']['state']}")
    else:
        print("    No change: apply would write nothing.")
    print(f"  {CURRENT_VALUE_NOTE}")

    if diff["affected_alerts"]:
        print()
        print(f"  Affected alerts (pattern): {', '.join(diff['affected_alerts'])}")

    print()
    if diff["changed"]:
        print("  To apply: remove --diff flag and re-run.")
    print()


def build_patch(cm_data, mode, tenant, metric_key, value):
    """The merge patch apply sends, or None for a no-op."""
    own = tenant_block(cm_data, tenant)[1]
    if (metric_key in own and _text(own[metric_key]) == str(value)
            and not _is_default(value)):
        return None
    patch = patch_legacy if mode == "legacy" else patch_multifile
    return patch(cm_data, tenant, metric_key, value)


# ---------------------------------------------------------------------------
# Post-write verification by the running exporter (#1950 route E)
# ---------------------------------------------------------------------------
class VerifyFailed(Exception):
    """An exporter pod shows a change other than the one allowed."""


class ConcurrentWrite(VerifyFailed):
    """The ConfigMap changed after our write, before verification ended."""


class ReloadTimeout(Exception):
    """Not every pod's `Last reload` moved within the timeout."""


class ExporterUnreachable(Exception):
    """No exporter pod, or one could not be read through pods/proxy."""


_SAMPLE = re.compile(
    r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+(\S+)(?:\s+-?\d+)?$")
_LABEL = re.compile(r'\s*([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"\s*,?')
PARSE_FAILURE = "da_config_parse_failure_total"


def parse_metrics(text):
    """({(name, labels): value text} of every `user_*` sample,
    {file_basename: float} of da_config_parse_failure_total), from the
    Prometheus text exposition format. An absent basename is 0."""
    series, failures = {}, {}
    for line in text.splitlines():
        if not (line.startswith("user_") or line.startswith(PARSE_FAILURE)):
            continue
        m = _SAMPLE.match(line.strip())
        if not m:
            raise ExporterUnreachable(f"cannot read the /metrics line {line!r}")
        name, body, value = m.groups()
        labels, pos = [], 0
        while body and pos < len(body):
            lm = _LABEL.match(body, pos)
            if not lm:
                raise ExporterUnreachable(
                    f"cannot read the labels of /metrics line {line!r}")
            labels.append(lm.groups())
            pos = lm.end()
        if name == PARSE_FAILURE:
            failures[dict(labels).get("file_basename", "")] = float(value)
        elif name.startswith("user_"):
            series[(name, tuple(sorted(labels)))] = value
    return series, failures


class Exporter:
    """The exporter pods, read through `kubectl get --raw` on pods/proxy."""

    def __init__(self, namespace=NAMESPACE, selector=EXPORTER_SELECTOR,
                 port=EXPORTER_PORT, timeout=RELOAD_TIMEOUT_S,
                 poll=POLL_INTERVAL_S):
        self.namespace, self.selector, self.port = namespace, selector, port
        self.timeout, self.poll = timeout, poll
        self.pods = {}  # pod name -> port number (text)

    def discover(self):
        """Fill `pods` with the serving ones (phase Running, not being
        deleted); none, or one without the port → unreachable. A pod that
        starts after this is not verified."""
        try:
            out = json.loads(run_cmd([
                "kubectl", "get", "pods", "-n", self.namespace,
                "-l", self.selector, "-o", "json"]))
            items = out["items"]
        except (KubectlError, ValueError, KeyError, TypeError) as exc:
            raise ExporterUnreachable(f"cannot list exporter pods: {exc}") from exc
        for pod in items:
            meta = pod.get("metadata") or {}
            if ((pod.get("status") or {}).get("phase") != "Running"
                    or meta.get("deletionTimestamp")):
                continue  # Pending / Failed / Evicted / Terminating: not serving
            self.pods[meta["name"]] = self._port_of(pod, meta["name"])
        if not self.pods:
            raise ExporterUnreachable(
                f"no Running pod matches -l {self.selector} in namespace "
                f"{self.namespace}; nothing can verify the write.")

    def _port_of(self, pod, name):
        # pods/proxy dials <podIP>:<port> as given, so a port NAME has to be
        # resolved to its number here.
        if self.port.isdigit():
            return self.port
        for c in pod.get("spec", {}).get("containers", []):
            for p in c.get("ports", []) or []:
                if p.get("name") == self.port:
                    return str(p["containerPort"])
        raise ExporterUnreachable(
            f"pod {name} has no container port named {self.port!r}.")

    def get(self, pod, path):
        SIGNALS.checkpoint()
        try:
            return run_cmd([
                "kubectl", "get", "--raw",
                f"/api/v1/namespaces/{self.namespace}/pods/"
                f"{pod}:{self.pods[pod]}/proxy/{path}"])
        except KubectlError as exc:
            raise ExporterUnreachable(f"pod {pod}: {exc}") from exc

    def last_reload(self, pod):
        for line in self.get(pod, "api/v1/config").splitlines():
            if line.startswith("Last reload:"):
                return line.split(":", 1)[1].strip()
        raise ExporterUnreachable(
            f"pod {pod}: /api/v1/config has no `Last reload:` line.")

    def snapshot(self, pod):
        """(Last reload, user_* series, parse failures) of one pod."""
        return (self.last_reload(pod),) + parse_metrics(self.get(pod, "metrics"))

    def wait_moved(self, since, seen):
        """{pod: snapshot} once each pod of `since` shows a `Last reload`
        other than since[pod]; `seen` gets the last value read per pod."""
        start = time.monotonic()
        deadline = start + self.timeout
        done, pending = {}, set(since)
        while True:
            for pod in sorted(pending):
                seen[pod] = self.last_reload(pod)
                if seen[pod] != since[pod]:
                    done[pod] = self.snapshot(pod)
                    seen[pod] = done[pod][0]
            pending -= set(done)
            left = deadline - time.monotonic()
            if not pending:
                return done
            if left <= 0:
                raise ReloadTimeout(
                    f"no reload seen before --reload-timeout "
                    f"({time.monotonic() - start:.1f}s elapsed) on "
                    f"{', '.join(sorted(pending))} (`Last reload` still "
                    f"{', '.join(since[p] for p in sorted(pending))}).")
            time.sleep(min(self.poll, left))


def verify_pod(pod, before, after, tenant, metric_key, key):
    """(problems, warnings, target changes) of one pod: after vs before
    (`snapshot` shapes). Target changes are `tenant`'s own changed series as
    {"series", "before", "after"} (None = absent), reported whatever the key.

    Which `user_*` series a write may change is judged on the exporter's own
    output, not on a Python copy of its key → label mapping:

    * a series whose `tenant` label is not `tenant` must not change at all;
    * for a key not starting with `_`, at most THRESHOLD_KEY_MAX_CHANGES
      series of `tenant` may change (appear, disappear or change value). The
      bound is the exporter's: every threshold phase (base, `_critical`,
      dimensional, declared; pkg/config/resolve.go) emits ONE row per key
      through appendWithLegacyTwin (aliases.go), which adds at most one
      legacy twin → 2 series per key per state, and a change that moves the
      severity label (`70:critical`) replaces both → 2 gone + 2 new = 4. A
      tenant with no series before is a new tenant: all its series are new
      (every platform default row), so the bound does not apply;
    * a `_` key (`_silent_mode`, `_state_*`, `_custom_alerts`, `_profile`,
      ...) has no such bound — one `_custom_alerts` or `_profile` value can
      emit, or remove, any number of series — so `tenant`'s own series are
      not judged at all (owner decision, #1950): they are only listed.

    ⚠️ Residuals: within the bound, a change to ANOTHER key of `tenant` (e.g.
    a same-carrier `expires:` rewritten by the re-serialisation, #1950 row 8)
    passes; under a `_` key ANY change of `tenant`'s own series passes — a
    wrong `_profile` that drops its thresholds is listed, not rolled back; a
    tenant over its cardinality cap may swap which rows are truncated, which
    fails here even when the write itself was fine; so does another tenant
    whose `expires:` passes between the two reads — deliberately not modelled
    here (that would be a second copy of the exporter's expiry rules), the
    message says to retry instead.
    """
    problems, warnings = [], []
    _, old, old_pf = before
    _, new, new_pf = after
    changed = {s for s in set(old) | set(new) if old.get(s) != new.get(s)}
    ours = {s for s in changed if dict(s[1]).get("tenant") == tenant}
    for s in sorted(changed - ours):
        problems.append(f"{pod}: series of another tenant changed: "
                        f"{_show(s)} {old.get(s)} -> {new.get(s)} (if that "
                        f"tenant has an `expires:` that just passed, retry)")
    had_series = any(dict(s[1]).get("tenant") == tenant for s in old)
    target = [{"series": _show(s), "before": old.get(s), "after": new.get(s)}
              for s in sorted(ours)]
    # A `_` key's own-tenant changes are listed in `target`, not judged.
    if (not metric_key.startswith("_") and had_series
            and len(ours) > THRESHOLD_KEY_MAX_CHANGES):
        problems.append(
            f"{pod}: {len(ours)} series of {tenant} changed; one threshold "
            f"key changes at most {THRESHOLD_KEY_MAX_CHANGES}: "
            + "; ".join(_show(s) for s in sorted(ours)))
    for base in sorted(new_pf):
        was, now = old_pf.get(base, 0.0), new_pf[base]
        if now <= was:
            continue
        line = (f"{pod}: {PARSE_FAILURE}{{file_basename=\"{base}\"}} "
                f"{was:g} -> {now:g}")
        if was == 0 or base == key:
            problems.append(line)
        else:
            warnings.append(line + " (was already failing before the write)")
    return problems, warnings, target


def _show(series):
    name, labels = series
    return name + "{" + ",".join(f'{k}="{v}"' for k, v in labels) + "}"


class PatchConflict(KubectlError):
    """The apiserver refused the patch: the ConfigMap's resourceVersion is
    no longer the one the patch was based on (409 Conflict)."""


def _kubectl_patch(patch_data, resource_version):
    """Merge-patch the ConfigMap only if it is still at `resource_version`
    (metadata.resourceVersion in a merge patch is the apiserver's optimistic
    concurrency precondition). Returns the resourceVersion the patch
    produced (None if kubectl did not print it); PatchConflict on 409."""
    patch_data = dict(patch_data,
                      metadata={"resourceVersion": resource_version})
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json',
                                     encoding="utf-8", newline='\n') as temp:
        json.dump(patch_data, temp)
        temp_path = temp.name
    try:
        try:
            out = run_cmd(["kubectl", "patch", "configmap", CONFIGMAP,
                           "-n", NAMESPACE, "--type", "merge",
                           "--patch-file", temp_path, "-o", "json"])
        except KubectlError as exc:
            if "(Conflict)" in str(exc):
                raise PatchConflict(str(exc)) from exc
            raise
        try:
            return json.loads(out)["metadata"]["resourceVersion"]
        except (ValueError, KeyError, TypeError):
            return None
    finally:
        try:
            os.remove(temp_path)
        except OSError:  # a leftover temp file must not mask the patch's outcome
            pass


def _say(text, out=False):
    """Best-effort output (stderr, or stdout when `out`); never raises.
    A closed fd (`>&-`: the stream is None), a broken pipe or any other
    stream failure only loses the text — it never changes what apply does
    or the exit code it ends with."""
    stream = sys.stdout if out else sys.stderr
    try:
        stream.write(text + "\n")
        stream.flush()
    except Exception:  # noqa: BLE001 — output is best effort, by contract
        pass  # the fd is sent to /dev/null at the end of main (_seal_std_streams)


def _to_devnull(stream):
    """Point a failing stream's fd at /dev/null, so whatever is still
    buffered for it — and anything written after main (atexit hooks) — can
    no longer fail the interpreter's exit flush (exit code 120)."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, stream.fileno())
        finally:
            os.close(devnull)
    except Exception:  # noqa: BLE001 — None, no fileno, not a real fd
        pass


def _seal_std_streams():
    """At the end of main: flush stdout / stderr, and send any of them that
    fails to flush or whose reader is gone (POLLERR / POLLHUP) to
    /dev/null — including one this tool never wrote to."""
    for stream in {id(s): s for s in (sys.stdout, sys.stderr, sys.__stdout__,
                                      sys.__stderr__) if s is not None}.values():
        try:
            stream.flush()
            fd = stream.fileno()
            poller = select.poll()
            poller.register(fd, select.POLLOUT)
            broken = any(ev & (select.POLLERR | select.POLLHUP | select.POLLNVAL)
                         for _, ev in poller.poll(0))
        except Exception:  # noqa: BLE001 — a failed flush is a broken stream
            broken = True
        if broken:
            _to_devnull(stream)


def _emit(document=None):
    """The one way main answers: SIGINT / SIGTERM to SIG_IGN first, so no
    signal can end the process with an exit code other than the one the
    answer states, then the --json document (if any) on stdout."""
    _guarded(SIGNALS.ignore)
    if document is not None:
        _say(document, out=True)


def new_report():
    """Per-pod findings of apply (the --json envelope's `pods`)."""
    return {"pods": {}}


class _Write:
    """What apply has done to the ConfigMap. main() derives every apply
    outcome from this plus what the ConfigMap is observed to hold."""

    def __init__(self):
        self.key = self.old = self.new = None
        self.attempted = False   # the write call was started
        self.returned = False    # ... and returned cleanly
        self.verified = False    # every pod verified it
        self.rv_read = None      # resourceVersion the write is based on
        self.rv_written = None   # resourceVersion the write produced
        self.rollback_sent = False  # a rollback patch may have been sent


def apply_patch(cm_data, mode, tenant, metric_key, value, exporter=None,
                report=None, write=None):
    """Build and apply the ConfigMap patch, then verify it on every exporter
    pod (VERIFY_HELP). Any failure is raised; `write` says how far it got
    and `_conclude` turns that into the rollback and the answer."""
    report = new_report() if report is None else report
    write = _Write() if write is None else write
    patch_data = build_patch(cm_data, mode, tenant, metric_key, value)
    key = old = None
    if patch_data is not None:
        (key, new), = patch_data["data"].items()
        old = _cm_keys(cm_data).get(key)
    if patch_data is None or new == old:  # same bytes: the exporter would not reload
        _say(f"No-op: nothing to change for tenant '{tenant}' key "
             f"'{metric_key}'; nothing was written.")
        return "no-op"
    write.rv_read = (cm_data.get("metadata") or {}).get("resourceVersion")
    if not write.rv_read:
        raise ConfigMapShapeError(
            f"{CONFIGMAP} as read has no metadata.resourceVersion; refusing "
            f"to write without a version precondition.")

    exporter = exporter or Exporter()
    exporter.discover()
    before = {pod: exporter.snapshot(pod) for pod in sorted(exporter.pods)}
    seen = {pod: snap[0] for pod, snap in before.items()}
    write.key, write.old, write.new = key, old, new
    write.exporter, write.before, write.seen = exporter, before, seen

    _say(f"Patching ConfigMap ({mode}) for {tenant}: {metric_key} = {value}...",
         out=True)
    SIGNALS.install()
    if SIGNALS.pending is not None:  # arrived just before the write
        SIGNALS.die()
    write.attempted = True
    write.rv_written = _kubectl_patch(patch_data, write.rv_read)
    write.returned = True
    SIGNALS.armed = True
    _say(f"Waiting for {len(before)} exporter pod(s) to reload...", out=True)
    after = exporter.wait_moved({p: s[0] for p, s in before.items()}, seen)
    problems = []
    for pod in sorted(after):
        found, warned, target = verify_pod(
            pod, before[pod], after[pod], tenant, metric_key, key)
        report["pods"][pod] = {"problems": found, "warnings": warned,
                               "target_changes": target}
        problems += found
        for w in warned:
            _say(f"WARNING: {w}")
        for t in target:
            _say(f"{pod}: {tenant} series {t['series']}: "
                 f"{t['before'] or '(absent)'} -> {t['after'] or '(absent)'}")
    if problems:
        raise VerifyFailed("\n".join(problems))
    # The reloads seen above can only have loaded our bytes (or later ones)
    # if nobody wrote the ConfigMap after us.
    now = _read_cm()
    if write.rv_written is None or now is _UNREADABLE or now[1] != write.rv_written:
        raise ConcurrentWrite(
            "threshold-config changed while apply was verifying (another "
            "writer), or its version cannot be confirmed; the reloads seen "
            "may not be of these bytes.")
    write.verified = True
    _say(f"Success! Verified on {len(after)} exporter pod(s).", out=True)
    return "applied"


class Interrupted(Exception):
    """SIGINT / SIGTERM arrived after the write; raised only at a checkpoint,
    never from the signal handler."""

    def __init__(self, signum):
        super().__init__(signal.Signals(signum).name)
        self.signum = signum


class _Signals:
    """SIGINT / SIGTERM from the write on. The handler never raises: it only
    records the first signal. The code acts on it at the checkpoint before
    each exporter kubectl call, so no exception
    can land inside subprocess cleanup or an except block. From the rollback
    on (`ignore()`) both are SIG_IGN until the process ends, which the
    kubectl children started from then on inherit.

    Once apply has answered (success or rollback) both are SIG_IGN — not
    the handler, which interpreter shutdown would reset to SIG_DFL. The CLI
    never restores the caller's handlers: the process ends under SIG_IGN. An in-process caller (tests) that
    calls main() more than once calls `reset()` in between."""

    def __init__(self):
        self.pending = None
        self.armed = False
        self._saved = {}

    def _handler(self, signum, _frame):
        if self.pending is None:
            self.pending = signum

    def install(self):
        """Ours from here on; a signal recorded by an earlier in-process
        apply is forgotten. The caller's handlers are saved once, for
        `reset()`."""
        self.pending, self.armed = None, False
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                previous = signal.signal(sig, self._handler)
            except ValueError:  # not the main thread: leave signals alone
                continue
            self._saved.setdefault(sig, previous)

    def reset(self):
        """In-process seam: put the caller's handlers back and forget any
        recorded signal. The CLI path never calls this."""
        for sig, handler in self._saved.items():
            signal.signal(sig, handler)
        self.__init__()

    def ignore(self):
        """From here to the end of the process SIGINT / SIGTERM do nothing —
        also to the kubectl children started from now on, which inherit
        SIG_IGN (a Python handler is not inherited: a Ctrl-C to the group
        would kill the rollback's kubectl). Works whether or not `install()`
        ran; the caller's handlers are kept for `reset()`."""
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                previous = signal.signal(sig, signal.SIG_IGN)
            except ValueError:  # not the main thread
                continue
            self._saved.setdefault(sig, previous)

    def checkpoint(self):
        if self.armed and self.pending is not None:
            raise Interrupted(self.pending)

    def cause(self, exc):
        """A failure that came with (or from) a signal is the signal."""
        if self.pending is not None:
            return Interrupted(self.pending)
        if isinstance(exc, KeyboardInterrupt):
            return Interrupted(signal.SIGINT)
        return exc

    def die(self):
        """Nothing was written: end the way the signal would have."""
        signum = self.pending
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
        sys.exit(EXIT_CALLER_ERROR)  # not reached; still true: nothing written


SIGNALS = _Signals()


_UNREADABLE = object()


def _read_cm():
    """(data, resourceVersion) of the ConfigMap now, or _UNREADABLE."""
    try:
        cm = json.loads(run_cmd([
            "kubectl", "get", "configmap", CONFIGMAP, "-n", NAMESPACE,
            "-o", "json"]))
        return _cm_keys(cm), cm["metadata"]["resourceVersion"]
    except BaseException:  # noqa: BLE001 — any failure is "cannot tell"
        return _UNREADABLE


def _read_key(key):
    """The ConfigMap's `key` now (None if absent), or _UNREADABLE."""
    now = _read_cm()
    return _UNREADABLE if now is _UNREADABLE else now[0].get(key)


_UNKNOWN = {"status": "state-unknown", "reason": None, "written": None,
            "rolled_back": False, "changed": True,
            "message": "internal error; check the ConfigMap by hand"}


def _outcome(status, message=None, reason=None, written=False,
             rolled_back=False, changed=True):
    return {"status": status, "message": message, "reason": reason,
            "written": written, "rolled_back": rolled_back, "changed": changed}


_PREWRITE_REASONS = ((ConfigMapShapeError, "configmap_shape"),
                     (KubectlError, "kubectl_failed"))


def _conclude(exc, write):
    """The ONE place an apply's outcome is decided: from `write` (how far it
    got) and what the ConfigMap is observed to hold — never from where the
    exception came from. A write that may have landed and was not verified
    is rolled back first. Returns an `_outcome` dict, or None for "nothing
    was written and a signal ends the process". main() answers with
    `_observed_fallback` if this itself raises."""
    if exc is None or write.verified:  # success, or failing to say so
        return _outcome("applied", written=True)
    if not write.attempted:
        if isinstance(exc, ExporterUnreachable):
            return _outcome("unreachable", str(exc), changed=False)
        reason = next((r for t, r in _PREWRITE_REASONS if isinstance(exc, t)),
                      "unexpected_error")
        return _outcome("caller_error", f"{type(exc).__name__}: {exc}", reason,
                        changed=False)
    # From here the ConfigMap may hold the new bytes.
    SIGNALS.armed = False  # the rollback and its waits are not interrupted
    _guarded(SIGNALS.ignore)  # the rollback's kubectl inherits SIG_IGN
    if not write.returned and isinstance(exc, PatchConflict):
        # 409: the apiserver refused the write; nothing was written.
        if SIGNALS.pending is not None:
            return None
        return _outcome("caller_error", (
            "threshold-config changed since it was read; nothing was "
            "written — re-run patch-config."), "configmap_changed",
            changed=False)
    if not write.returned:
        now = _read_key(write.key)
        if now == write.old:  # the failed write call did not land
            if SIGNALS.pending is not None:
                return None
            reason = "kubectl_failed" if isinstance(exc, KubectlError) \
                else "unexpected_error"
            return _outcome("caller_error", f"{type(exc).__name__}: {exc}",
                            reason, changed=False)
        if now != write.new:
            return _outcome("state-unknown", (
                "the write call did not return cleanly and the key "
                + ("cannot be re-read" if now is _UNREADABLE else
                   "holds neither the old nor the new bytes")
                + "; check it by hand."), written=None)
    failed = _send_rollback(write)
    cause = SIGNALS.cause(exc)
    if isinstance(failed, Overwritten):
        return _outcome("overwritten-by-another-writer", (
            f"{type(cause).__name__}: {cause}; another writer changed key "
            f"{write.key} after this write, so it was not rolled back (that "
            f"would destroy their change); check it by hand."),
            written=failed.value == write.new)
    now = write.old if failed is None else _read_key(write.key)
    if now == write.new:
        return _outcome("rollback-failed", (
            f"{type(cause).__name__}: {cause}; the rollback failed too "
            f"({failed}), so the ConfigMap still holds the new bytes."),
            written=True)
    if now != write.old:
        return _outcome("state-unknown", (
            f"{type(cause).__name__}: {cause}; the rollback failed ({failed}) "
            f"and its outcome cannot be read back; check it by hand."),
            written=None)
    _wait_rolled_back(write)
    status = ("interrupted-rolled-back" if isinstance(cause, Interrupted) else
              "verify-failed-rolled-back" if isinstance(cause, VerifyFailed) else
              "timeout" if isinstance(cause, ReloadTimeout) else
              "unreachable" if isinstance(cause, ExporterUnreachable) else
              "error-rolled-back")
    return _outcome(status, f"{type(cause).__name__}: {cause}", rolled_back=True)


class Overwritten(Exception):
    """Another writer replaced the patched key after our write: rolling back
    would destroy their change, so it is not rolled back."""

    def __init__(self, value):
        super().__init__("another writer changed the key after this write")
        self.value = value


def _send_rollback(write):
    """Patch the old bytes back (the key removed if it did not exist), only
    over the version our write produced. On a conflict: if the key still
    holds exactly our bytes (another key changed), retry on the fresh
    version, a bounded number of times; if it holds anything else, stop —
    Overwritten. None if a rollback went through, else what stopped it."""
    rv = write.rv_written
    for _ in range(ROLLBACK_CONFLICT_ATTEMPTS):
        try:
            if rv is None:
                raise PatchConflict("the version our write produced is unknown")
            write.rollback_sent = True  # may land even if the call fails
            _kubectl_patch({"data": {write.key: write.old}}, rv)
            return None
        except PatchConflict as exc:
            now = _read_cm()
            if now is _UNREADABLE:
                return exc
            if now[0].get(write.key) != write.new:
                return Overwritten(now[0].get(write.key))
            rv = now[1]
        except BaseException as exc:  # noqa: BLE001 — judged from the ConfigMap
            return exc
    return PatchConflict("the ConfigMap kept changing under the rollback")


def _wait_rolled_back(write):
    """Best effort: pods that reloaded since `before` load the old bytes."""
    _say(f"rolling back {CONFIGMAP} key {write.key}: done; waiting for the "
         f"pods that reloaded...")
    moved = {p: write.seen[p] for p in write.before
             if write.seen[p] != write.before[p][0]}
    try:
        if moved:
            write.exporter.wait_moved(moved, dict(write.seen))
        _say("Rolled back.")
    except BaseException as exc:  # noqa: BLE001 — best effort only
        _say(f"WARNING: rolled back, but the reload of the old bytes was "
             f"not seen: {type(exc).__name__}: {exc}")


def _observed_fallback(write):
    """When _conclude itself fails: the answer is decided only by what the
    key is observed to hold (read once; after a rollback it sends, once
    more), plus what this process itself did (`write`). What a rollback
    call returned is never used to infer the state: a call can fail and
    land, and a 409 re-read cannot tell our rollback from another writer."""
    try:
        if not write.attempted:
            return _outcome("caller_error", "internal error before the write",
                            "unexpected_error", changed=False)
        if write.verified:
            return _outcome("applied", written=True)
        now = _read_key(write.key)
        if now is _UNREADABLE:
            pass  # -> state-unknown
        elif now == write.old and write.rollback_sent:
            return _outcome("error-rolled-back", "internal error; the key "
                            "holds the old bytes after the rollback",
                            rolled_back=True)
        elif now == write.old and not write.returned:
            return _outcome("caller_error", "internal error; the write call "
                            "failed and the key holds the old bytes (it did "
                            "not land)", "unexpected_error", changed=False)
        elif now == write.old:
            return _outcome("overwritten-by-another-writer", "internal error; "
                            "the key holds the old bytes, but this process "
                            "wrote the new ones and sent no rollback: another "
                            "writer changed it; check it by hand", written=False)
        elif now == write.new:
            _send_rollback(write)  # its result is not trusted; observe instead
            again = _read_key(write.key)
            if again == write.old:
                return _outcome("error-rolled-back", "internal error; the key "
                                "holds the old bytes after the rollback",
                                rolled_back=True)
            if again == write.new:
                return _outcome("rollback-failed", "internal error; the key "
                                "still holds the new bytes after the rollback",
                                written=True)
        else:
            return _outcome("overwritten-by-another-writer", "internal error; "
                            "the key holds neither the old nor the new bytes: "
                            "another writer changed it, not rolled back",
                            written=False)
    except BaseException:  # noqa: BLE001
        pass
    return _outcome("state-unknown", "internal error; check the ConfigMap "
                    "by hand", written=None)


def _guarded(fn, *args):
    try:
        return fn(*args)
    except BaseException:  # noqa: BLE001 — best effort, never in the way
        return None


def build_parser():
    """The CLI's argument parser."""
    parser = argparse.ArgumentParser(
        description="Patch threshold-config ConfigMap for a specific tenant",
        epilog=VERIFY_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("tenant", help="Tenant name (e.g., db-a)")
    parser.add_argument("metric_key", help="Metric key to patch")
    parser.add_argument("value", help="New value, 'default', or 'disable'")
    parser.add_argument(
        "--diff", action="store_true",
        help="Preview the change without applying (like terraform plan): "
             "the value to be written and whether apply would write; the "
             "current value is not shown (check the exporter)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Print the result as one JSON document (the preview under "
             "--diff; apply's outcome otherwise); all other output goes to "
             "stderr",
    )
    parser.add_argument(
        "--exporter-namespace", default=NAMESPACE, metavar="NS",
        help=f"Namespace of the exporter pods apply verifies on "
             f"(default: {NAMESPACE})")
    parser.add_argument(
        "--exporter-selector", default=EXPORTER_SELECTOR, metavar="LABELS",
        help=f"Label selector of those pods (default: {EXPORTER_SELECTOR})")
    parser.add_argument(
        "--exporter-port", default=EXPORTER_PORT, metavar="PORT",
        help=f"Container port name or number of their HTTP listener "
             f"(default: {EXPORTER_PORT})")
    parser.add_argument(
        "--reload-timeout", type=_positive_seconds, default=RELOAD_TIMEOUT_S,
        metavar="SECONDS",
        help=f"Deadline that ends apply's wait for every pod to reload, "
             f"checked between polling passes (default: {RELOAD_TIMEOUT_S:g})")
    parser.add_argument(
        "--poll-interval", type=_positive_seconds, default=POLL_INTERVAL_S,
        metavar="SECONDS",
        help=f"How often it reads `Last reload` meanwhile "
             f"(default: {POLL_INTERVAL_S:g})")
    return parser


def _positive_seconds(text):
    try:
        seconds = float(text)
    except ValueError:
        seconds = float("nan")
    if not seconds > 0 or seconds == float("inf"):
        raise argparse.ArgumentTypeError(f"not a positive number of seconds: {text!r}")
    return seconds


def _envelope(status, reason):
    """The diff keys emptied, plus `status`/`reason` (dev-rules §13)."""
    return format_json_report({
        "tenant": None, "metric_key": None, "configmap_mode": None,
        "before": None, "after": None, "changed": False,
        "affected_alerts": [], "status": status, "reason": reason,
        "written": False})


def _refuse(as_json, reason, message):
    """Caller error: message on stderr and, under --json, one envelope."""
    _say(f"ERROR: {message}")
    _emit(_envelope("caller_error", reason) if as_json else None)
    sys.exit(EXIT_CALLER_ERROR)


def _apply_envelope(args, mode, report, outcome, code):
    """apply's --json document: the --diff keys (before/after empty) plus
    `status` (see _status_exit_code), `exit_code`, `written`, `rolled_back`,
    `message` and per-pod `problems` / `warnings` / `target_changes`."""
    if outcome["status"] == "caller_error":
        return _envelope("caller_error", outcome["reason"])
    return format_json_report({
        "tenant": args.tenant, "metric_key": args.metric_key,
        "configmap_mode": mode, "before": None, "after": None,
        "changed": outcome["changed"], "affected_alerts": [],
        "status": outcome["status"], "reason": None, "exit_code": code,
        "written": outcome["written"], "rolled_back": outcome["rolled_back"],
        "message": outcome["message"], "pods": report["pods"]})


def _status_exit_code(status):
    """Exit code of an apply outcome (VERIFY_HELP)."""
    if status in ("applied", "no-op"):
        return EXIT_OK
    if status == "caller_error":
        return EXIT_CALLER_ERROR
    if status == "verify-failed-rolled-back":
        return EXIT_VIOLATION  # == EXIT_VERIFY_FAILED; spelled so the CLI-contract AST resolves it
    if status == "timeout":
        return EXIT_RELOAD_TIMEOUT
    if status == "unreachable":
        return EXIT_EXPORTER_UNREACHABLE
    if status == "rollback-failed":
        return EXIT_ROLLBACK_FAILED
    if status in ("state-unknown", "overwritten-by-another-writer"):
        return EXIT_STATE_UNKNOWN
    return EXIT_ABORTED_ROLLED_BACK  # interrupted- / error-rolled-back


def main():
    """CLI entry point: Patch threshold-config ConfigMap for a specific tenant."""
    try:
        _main()
    finally:
        _seal_std_streams()


def _main():
    as_json = any(len(a) > 2 and "--json".startswith(a.split("=", 1)[0])
                  for a in sys.argv[1:])
    try:
        with contextlib.redirect_stdout(sys.stderr if as_json else sys.stdout):
            args = build_parser().parse_args()
    except SystemExit as exc:
        if as_json and not exc.code:  # --help: its text went to stderr
            _emit(_envelope("help", None))
        elif as_json:
            _refuse(True, "bad_arguments", "arguments rejected (usage above)")
        raise

    mode, report, write = None, new_report(), _Write()
    status, exc = None, None
    try:
        try:
            cm_data = json.loads(run_cmd([
                "kubectl", "get", "configmap", CONFIGMAP,
                "-n", NAMESPACE, "-o", "json"]))
            if not isinstance(cm_data, dict):
                raise ValueError("not a JSON object")
        except ValueError as err:
            raise KubectlError(f"kubectl did not print JSON: {err}") from err
        mode = detect_mode(cm_data)

        if args.diff:
            # Preview mode: show diff without applying
            diff = diff_preview(cm_data, mode, args.tenant, args.metric_key,
                                args.value)
            if args.json:
                _emit(format_json_report(diff))
            else:
                print_diff(diff)
            return
        with contextlib.redirect_stdout(sys.stderr if args.json else sys.stdout):
            _say(f"Detected ConfigMap mode: {mode}", out=True)
            status = apply_patch(
                cm_data, mode, args.tenant, args.metric_key, args.value,
                Exporter(args.exporter_namespace, args.exporter_selector,
                         args.exporter_port, args.reload_timeout,
                         args.poll_interval), report, write)
    except BaseException as caught:  # noqa: BLE001 — every path goes to _conclude
        if not write.attempted and isinstance(caught, (KeyboardInterrupt,
                                                       SystemExit)):
            raise  # nothing written: end the way it normally would
        exc = caught
    try:
        outcome = (_outcome("no-op", changed=False) if status == "no-op"
                   else _conclude(exc, write))
    except BaseException:  # noqa: BLE001 — even _conclude failing is observed
        try:
            outcome = _observed_fallback(write)
        except BaseException:  # noqa: BLE001
            outcome = dict(_UNKNOWN)
    if outcome is None:
        SIGNALS.die()  # nothing written, a signal came: end as it would
    # The exit code is fixed once, here; saying it can fail, changing it cannot.
    try:
        code = _status_exit_code(outcome["status"])
    except BaseException:  # noqa: BLE001
        code = EXIT_STATE_UNKNOWN
    try:
        _answer(args, mode, report, outcome, code)
    except BaseException:  # noqa: BLE001 — the answer is best effort
        pass
    if code != EXIT_OK:
        sys.exit(code)


def _answer(args, mode, report, outcome, code):
    """Say the outcome (stderr, and the --json envelope)."""
    if outcome["message"] and outcome["status"] != "applied":
        _guarded(_say, f"ERROR: {outcome['message']}")
    doc = (_guarded(_apply_envelope, args, mode, report, outcome, code)
           if args.json else None)
    _guarded(_emit, doc)


if __name__ == "__main__":
    main()
