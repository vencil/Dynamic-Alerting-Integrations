"""Render both sides of a config change and compare what they render to (ADR-037).

config-diff compares the rendered output of the base and PR conf.d trees, not
the YAML: each side is rendered by the two programs that turn conf.d into what
runs — `da-guard served-values` (what the exporter's /metrics serves per
tenant) and the route generator's `--output-configmap` (the whole Alertmanager
configuration). Nothing here reads conf.d itself, beyond asking
`_lib_confd.iter_config_files` whether a side has any config file at all.

`compare(base_dir, pr_dir, at)` returns a `RenderDiff` with one `ViewResult`
per view (`served` and `routes`), each `computed` (with its changes),
`not_computed` (the PR side, or the tool, failed: no result) or `not_compared`
(the base side's configuration is broken, so there is nothing sound to compare
against). `RenderDiff.exit_code` maps that to 0 / 1 / 2 as ADR-037 decision 4
lays down:

* a view that is `not_computed` → 2;
* otherwise a change, or a view that is `not_compared` → 1;
* otherwise 0.

A failure is the configuration's (`kind="config"`) only when it is one of:
served-values exits 3 (a file does not decode or cannot be read), da-guard
accepts the arguments but exits 2 without a Go panic (a tree the exporter's
load rejects, e.g. a tenant declared twice), or the route generator rejects the
tree (exit 1 without a traceback, or exit 2 with its routing-tree refusal).
Anything else is the tool's (`kind="tool"`) and is never downgraded. A side
with no conf.d, or no config file in it, is not rendered: as the base it is an
empty configuration (first import), as the PR side it is `not_computed`.

The route tree is compared node by node. A node's identity is the path of
matchers from the root to it, so a receiver renamed by the generator does not
read as a moved route; siblings with identical matchers are paired in order
(`#1`, `#2`, …). A node belongs to the tenant of the first `tenant="X"`
matcher on its path, else to `PLATFORM`. A receiver whose definition changed
belongs to every tenant whose route (on either side) points at it; fields that
may carry credentials are reported as changed with both values masked — in a
receiver and in served-values alike (`values._routing` carries the tenant's
receiver; `unserved` may carry a routing key as written).
Inhibit rules are compared as a multiset, each belonging to the tenant its
`tenant="X"` matcher names.

served-values is compared per tenant and per field (`values`, `severities`,
`state_filters`, `unserved`, `dropped`, `schedules`): one `ValueChange` per
key whose reading differs, with the readings of both sides.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, NamedTuple

_TOOLS = Path(__file__).resolve().parent
for _p in (_TOOLS, _TOOLS.parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import yaml  # noqa: E402

import _lib_confd  # noqa: E402
import _lib_tenant_values as tv  # noqa: E402

PLATFORM = "(platform)"
MASKED = "***"
VIEW_SERVED = "served"
VIEW_ROUTES = "routes"
# The route generator ships beside this module: in the repo both live in
# scripts/tools/ops/, in the da-tools image every tool sits in one directory.
GENERATOR = _TOOLS / "generate_alertmanager_routes.py"
DEFAULT_TIMEOUT = 600
# What the route generator renders for a tree with no tenant: it writes no
# file at all, so there is no route, receiver or inhibit rule.
EMPTY_ROUTES: dict[str, Any] = {"route": {}, "receivers": [], "inhibit_rules": []}

ROUTE_ATTRS = ("receiver", "group_by", "group_wait", "group_interval", "repeat_interval", "continue")
SERVED_FIELDS = ("values", "severities", "state_filters", "unserved", "dropped", "schedules")
# Receiver fields whose value may be a credential (webhook URLs carry tokens).
_SECRET_KEY = re.compile(r"url|token|password|secret|key|credential|auth", re.IGNORECASE)
_TENANT_MATCHER = re.compile(r'^\s*tenant\s*=\s*"?(.*?)"?\s*$')
# One Alertmanager matcher: name, operator, value (quoted or not).
_MATCHER = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(=~|!~|!=|=)\s*(?:"((?:[^"\\]|\\.)*)"|(.*?))\s*$')
# The route generator's refusal of a tree's shape (exit 2, ADR-017 Decision 9).
GENERATOR_TREE_REFUSAL = "routing-tree error(s)"


class RenderFailure(NamedTuple):
    kind: str    # "config" | "tool"
    reason: str  # one line, for the report


class RenderError(Exception):
    def __init__(self, failure: RenderFailure) -> None:
        super().__init__(failure.reason)
        self.failure = failure


class RouteChange(NamedTuple):
    tenant: str
    path: tuple[str, ...]          # matcher path, each step "m1, m2" (sorted) plus "#n" for a repeat
    kind: str                      # "added" | "removed" | "changed"
    changes: dict[str, tuple[Any, Any]]  # attribute -> (base, pr); for added / removed the node's attributes


class ReceiverChange(NamedTuple):
    name: str
    tenants: tuple[str, ...]       # sorted; PLATFORM when only platform routes point at it
    kind: str                      # "added" | "removed" | "changed"
    changes: dict[str, tuple[Any, Any]]  # dotted field path -> (base, pr), credentials MASKED


class InhibitChange(NamedTuple):
    tenant: str
    kind: str                      # "added" | "removed"
    rule: dict[str, Any]


class ValueChange(NamedTuple):
    tenant: str
    field: str                     # one of SERVED_FIELDS, or "tenant" for a tenant added / removed
    key: str
    base: Any                      # None when absent on that side
    pr: Any


class ViewResult(NamedTuple):
    status: str                    # "computed" | "not_computed" | "not_compared"
    reason: str                    # "" when computed
    changes: tuple = ()


class RenderDiff(NamedTuple):
    at: str
    served: ViewResult
    routes: ViewResult

    @property
    def exit_code(self) -> int:
        views = (self.served, self.routes)
        if any(v.status == "not_computed" for v in views):
            return 2
        if any(v.status == "not_compared" or v.changes for v in views):
            return 1
        return 0


# ── rendering ───────────────────────────────────────────────────────────


def has_config(conf_d: str | os.PathLike[str]) -> bool:
    """Whether `conf_d` is a directory holding any config file."""
    return next(iter(_lib_confd.iter_config_files(conf_d)), None) is not None


def render_served(conf_d: str | os.PathLike[str], at: str, *, binary: str | None = None,
                  timeout: float = DEFAULT_TIMEOUT) -> dict[str, tv.TenantValues]:
    """served-values for every tenant at `at`, with the whole day's schedules.
    Raises `RenderError` classified as described in the module docstring."""
    try:
        return tv.load_served_tree(conf_d, at=at, binary=binary, timeout=timeout,
                                   schedules=True).tenants
    except tv.ParseFailedError as e:
        raise RenderError(RenderFailure("config", f"served-values: {e}")) from e
    except tv.DaGuardNotFoundError as e:
        raise RenderError(RenderFailure("tool", f"served-values: {e}")) from e
    except tv.DaGuardError as e:
        # Exit 2 from a da-guard that accepts the arguments is the tree's
        # (#2725); a timeout or a signal carries no code we can trust.
        panic = "goroutine " in e.stderr and any(
            ln.startswith("panic:") for ln in e.stderr.splitlines())
        tree = not e.binary_fault and e.returncode == 2 and not panic
        raise RenderError(RenderFailure("config" if tree else "tool",
                                        f"served-values: {e.message}")) from e


def render_routes(conf_d: str | os.PathLike[str], *,
                  timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """The Alertmanager configuration the route generator writes for `conf_d`.
    Raises `RenderError` classified as described in the module docstring."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "alertmanager-configmap.yaml"
        cmd = [sys.executable, str(GENERATOR), "--config-dir", str(conf_d),
               "--output-configmap", "-o", str(out)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", check=False, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise RenderError(RenderFailure("tool", f"route generator did not finish within {timeout}s")) from e
        except OSError as e:
            raise RenderError(RenderFailure("tool", f"route generator could not be run: {e}")) from e
        if proc.returncode != 0:
            last = _last_line(proc.stderr) or _last_line(proc.stdout)
            crashed = "Traceback (most recent call last)" in proc.stderr
            refused = (proc.returncode == 1
                       or (proc.returncode == 2 and GENERATOR_TREE_REFUSAL in proc.stderr))
            kind = "config" if refused and not crashed else "tool"
            raise RenderError(RenderFailure(kind, f"route generator exited {proc.returncode}: {last}"))
        if not out.exists():
            # A tree with no tenant: the generator exits 0 and writes nothing
            # ("No tenants found in config directory.").
            return dict(EMPTY_ROUTES)
        try:
            doc = yaml.safe_load(out.read_text(encoding="utf-8"))
            cfg = yaml.safe_load(doc["data"]["alertmanager.yml"])
            if not isinstance(cfg, dict) or not isinstance(cfg.get("route"), dict):
                raise ValueError("no route tree")
        except (OSError, yaml.YAMLError, KeyError, TypeError, ValueError) as e:
            raise RenderError(RenderFailure(
                "tool", f"route generator output is not an Alertmanager ConfigMap ({e})")) from e
        return cfg


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


# ── comparing ───────────────────────────────────────────────────────────


def compare(base_dir: str | os.PathLike[str], pr_dir: str | os.PathLike[str], at: str, *,
            binary: str | None = None, timeout: float = DEFAULT_TIMEOUT) -> RenderDiff:
    """Render both sides and compare them (see the module docstring)."""
    if not has_config(pr_dir):
        reason = f"PR side: {pr_dir} has no config file"
        miss = ViewResult("not_computed", reason)
        return RenderDiff(at, miss, miss)
    first_import = not has_config(base_dir)

    def view(render, empty, diff) -> ViewResult:
        try:
            pr = render(pr_dir)
        except RenderError as e:
            return ViewResult("not_computed", f"PR side: {e.failure.reason}")
        if first_import:
            base = empty()
        else:
            try:
                base = render(base_dir)
            except RenderError as e:
                if e.failure.kind == "tool":
                    return ViewResult("not_computed", f"base side: {e.failure.reason}")
                return ViewResult("not_compared", f"base side does not render: {e.failure.reason}")
        return ViewResult("computed", "", tuple(diff(base, pr)))

    served = view(lambda d: render_served(d, at, binary=binary, timeout=timeout),
                  dict, diff_served)
    routes = view(lambda d: render_routes(d, timeout=timeout),
                  lambda: dict(EMPTY_ROUTES), diff_routes)
    return RenderDiff(at, served, routes)


def diff_served(base: dict[str, tv.TenantValues], pr: dict[str, tv.TenantValues]) -> list[ValueChange]:
    out: list[ValueChange] = []
    for tenant in sorted(set(base) | set(pr)):
        b, p = base.get(tenant), pr.get(tenant)
        if b is None or p is None:
            out.append(ValueChange(tenant, "tenant", tenant, b is not None or None, p is not None or None))
            continue
        for field in SERVED_FIELDS:
            bm, pm = getattr(b, field) or {}, getattr(p, field) or {}
            for key in sorted(set(bm) | set(pm), key=str):
                bv, pv = bm.get(key), pm.get(key)
                if _canon(bv) == _canon(pv):
                    continue
                if isinstance(bv, dict) or isinstance(pv, dict):
                    # Leaf by leaf, so a credential inside (the receiver of
                    # `_routing`) is masked like a receiver's.
                    for leaf, (lb, lp) in _field_changes(bv if isinstance(bv, dict) else {},
                                                         pv if isinstance(pv, dict) else {}).items():
                        out.append(ValueChange(tenant, field, f"{key}.{leaf}", lb, lp))
                elif _secret_key(str(key)):
                    out.append(ValueChange(tenant, field, key, _mask(bv), _mask(pv)))
                else:
                    out.append(ValueChange(tenant, field, key, bv, pv))
    return out


def diff_routes(base: dict[str, Any], pr: dict[str, Any]) -> list:
    """Route nodes, then receivers, then inhibit rules."""
    out: list = []
    bn, pn = flatten_routes(base), flatten_routes(pr)
    for path in sorted(set(bn) | set(pn)):
        b, p = bn.get(path), pn.get(path)
        tenant = tenant_of_path(path)
        if b is None:
            out.append(RouteChange(tenant, path, "added", {a: (None, v) for a, v in p.items()}))
        elif p is None:
            out.append(RouteChange(tenant, path, "removed", {a: (v, None) for a, v in b.items()}))
        else:
            changed = {a: (b.get(a), p.get(a)) for a in ROUTE_ATTRS if _canon(b.get(a)) != _canon(p.get(a))}
            if changed:
                out.append(RouteChange(tenant, path, "changed", changed))

    users: dict[str, set[str]] = {}
    for nodes in (bn, pn):
        for path, attrs in nodes.items():
            if attrs.get("receiver"):
                users.setdefault(attrs["receiver"], set()).add(tenant_of_path(path))
    br, pr_ = _receivers(base), _receivers(pr)
    for name in sorted(set(br) | set(pr_)):
        b, p = br.get(name), pr_.get(name)
        if _canon(b) == _canon(p):
            continue
        tenants = tuple(sorted(users.get(name) or {PLATFORM}))
        kind = "added" if b is None else "removed" if p is None else "changed"
        out.append(ReceiverChange(name, tenants, kind, _field_changes(b or {}, p or {})))

    bi, pi = _inhibit_counts(base), _inhibit_counts(pr)
    for key in sorted(set(bi) | set(pi)):
        rule = json.loads(key)
        n = pi.get(key, 0) - bi.get(key, 0)
        for _ in range(abs(n)):
            out.append(InhibitChange(_tenant_of_rule(rule), "added" if n > 0 else "removed", rule))
    return out


def flatten_routes(cfg: dict[str, Any]) -> dict[tuple[str, ...], dict[str, Any]]:
    """Every route node keyed by its matcher path (the root is `()`)."""
    out: dict[tuple[str, ...], dict[str, Any]] = {}

    def walk(node: dict[str, Any], path: tuple[str, ...]) -> None:
        out[path] = {a: node[a] for a in ROUTE_ATTRS if a in node}
        seen: dict[str, int] = {}
        for child in node.get("routes") or []:
            step = ", ".join(_matchers(child))
            n = seen.get(step, 0)
            seen[step] = n + 1
            walk(child, path + ((f"{step} #{n + 1}" if n else step),))

    walk(cfg.get("route") or {}, ())
    return out


def _matchers(node: dict[str, Any]) -> list[str]:
    out = [_normal_matcher(str(m)) for m in node.get("matchers") or []]
    out += [f'{k}="{v}"' for k, v in (node.get("match") or {}).items()]
    out += [f'{k}=~"{v}"' for k, v in (node.get("match_re") or {}).items()]
    return sorted(out)


def _normal_matcher(m: str) -> str:
    """`name op "value"`: the spellings Alertmanager reads as one matcher
    (spaces, an unquoted value) compare equal."""
    hit = _MATCHER.match(m)
    if not hit:
        return m.strip()
    name, op, quoted, bare = hit.groups()
    value = quoted if quoted is not None else (bare or "").replace('"', '\\"')
    return f'{name}{op}"{value}"'


def tenant_of_path(path: tuple[str, ...]) -> str:
    for step in path:
        for m in step.split(", "):
            t = _tenant_of_matcher(m.split(" #")[0])
            if t is not None:
                return t
    return PLATFORM


def _tenant_of_matcher(m: str) -> str | None:
    if "=~" in m or "!=" in m or "!~" in m:
        return None
    hit = _TENANT_MATCHER.match(m)
    return hit.group(1) if hit else None


def _tenant_of_rule(rule: dict[str, Any]) -> str:
    for side in ("source_matchers", "target_matchers"):
        for m in rule.get(side) or []:
            t = _tenant_of_matcher(str(m))
            if t is not None:
                return t
    for side in ("source_match", "target_match"):
        t = (rule.get(side) or {}).get("tenant")
        if t is not None:
            return str(t)
    return PLATFORM


def _receivers(cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(r.get("name")): r for r in cfg.get("receivers") or [] if isinstance(r, dict)}


def _inhibit_counts(cfg: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for rule in cfg.get("inhibit_rules") or []:
        key = _canon(rule)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _field_changes(base: dict[str, Any], pr: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
    """Leaf-by-leaf changes of a receiver; a credential-shaped field is masked."""
    bl, pl = _leaves(base), _leaves(pr)
    out: dict[str, tuple[Any, Any]] = {}
    for path in sorted(set(bl) | set(pl)):
        b, p = bl.get(path), pl.get(path)
        if _canon(b) == _canon(p):
            continue
        if any(_secret_key(part) for part in path.split(".") if not part.isdigit()):
            b, p = _mask(b), _mask(p)
        out[path] = (b, p)
    return out


def _secret_key(name: str) -> bool:
    """A field that may hold a credential: by its name, or a routing key as a
    whole (its value as written may be a receiver with a URL in it)."""
    return bool(_SECRET_KEY.search(name)) or name.startswith("_routing")


def _mask(v: Any) -> Any:
    return None if v is None else MASKED


def _leaves(node: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for k, v in node.items():
            out.update(_leaves(v, f"{prefix}.{k}" if prefix else str(k)))
        return out
    if isinstance(node, list) and any(isinstance(x, (dict, list)) for x in node):
        out = {}
        for i, v in enumerate(node):
            out.update(_leaves(v, f"{prefix}.{i}"))
        return out
    return {prefix: node}


def _canon(v: Any) -> str:
    return json.dumps(v, sort_keys=True, default=_jsonable)


def _jsonable(v: Any) -> Any:
    if hasattr(v, "_asdict"):
        return v._asdict()
    if isinstance(v, (tuple, set)):
        return list(v)
    return str(v)
