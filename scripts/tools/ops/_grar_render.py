"""Output rendering + Alertmanager ConfigMap operations.

PR-3a (v2.8.0) extracted these helpers out of generate_alertmanager_routes.py
to bring the main file under the line-count cap. All symbols are re-exported
from generate_alertmanager_routes for backwards-compatible test imports.

Functions:
  Output rendering:
    render_output(...)           → fragment YAML (route + receivers + inhibit_rules)
    load_base_config(path)       → base AM YAML; defaults ONLY when the flag was
                                   omitted — a supplied-but-unusable value
                                   raises BaseConfigInputError (#1616)
    assemble_configmap(...)      → full K8s ConfigMap YAML for GitOps PR

  ConfigMap operations (--apply mode, K8s cluster deploy):
    _read_existing_configmap(...)         → kubectl get + parse
    _merge_routes_receivers_inhibits(...) → merge generated into existing
    _apply_merged_configmap(...)          → kubectl apply via stdin
    _reload_alertmanager(namespace)       → curl POST /-/reload (False on failure, #2219)
    apply_to_configmap(...)              → orchestrate read → merge → amtool gate
                                           → apply → reload

  Alertmanager's own parser (#2219):
    amtool_check(am_yml, what=, refusing=) → `amtool check-config` on the exact
                                           text, returned as an AmtoolCheck
    amtool_gate(...)                      → amtool_check, printed; None = go on,
                                           int = exit code

  The --validate verdict (#2311, shared with validate-config's routes row):
    evaluate_generated_config(...)        → GeneratedConfigVerdict; never
                                           prints, never exits
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout

from _grar_routes import (  # noqa: E402
    _build_custom_alert_routes, _build_watchdog_route, _build_synthetic_probe_route,
    _build_sentinel_sinkhole_route)
from _lib_exitcodes import EXIT_CALLER_ERROR, EXIT_VIOLATION  # noqa: E402
from _lib_io import safe_label  # noqa: E402
from _grar_validate import (  # noqa: E402
    assert_watchdog_inhibit_immunity,
    assert_platform_alerts_not_tenant_silenceable,
    assert_equal_labels_gated,
    blocking_generation_errors,
    find_tenant_silenceable_platform_inhibits,
    find_ungated_equal_label_inhibits,
    find_watchdog_suppressing_inhibits,
)


def _enforce_equal_labels_gated(inhibit_rules: list[dict] | None, strict: bool) -> None:
    """Apply the #1132 equal-label-gated invariant to a merged inhibit set.

    strict → hard-fail (raise); otherwise → emit a WARN per offending rule so a
    BYO customer's pipeline degrades to a warning rather than breaking. Platform
    CI passes strict=True (see the --strict render paths). Kept a notch softer
    than the unconditional Watchdog guard: a silent-suppression footgun is
    serious but not catastrophic like a suppressed dead-man's-switch."""
    if strict:
        assert_equal_labels_gated(inhibit_rules)
        return
    for i, _r, lbls in find_ungated_equal_label_inhibits(inhibit_rules):
        print(
            f"  WARN: inhibit_rules[{i}] equal={lbls} not presence-gated on either "
            "side — Alertmanager treats missing-on-both-sides as equal, so this rule "
            'may silently suppress unrelated alerts (#1132). Gate `<label>=~".+"` on '
            "source_matchers OR target_matchers (either side suffices; both = defence "
            "in depth), or drop it from equal:.",
            file=sys.stderr)


def _main_tenant_routes(routes: list[dict]) -> dict[str, dict]:
    """Map each tenant that owns a MAIN tenant route in *routes* to that route.

    #1092 0-pre: a route qualifies as a main tenant route only when BOTH hold —
    it carries a matcher that is exactly ``tenant="<t>"`` (literal value, no
    regex), AND its receiver is the string ``tenant-<t>``. The receiver equality
    is load-bearing: ``platform-enforced-<t>`` routes ALSO carry a tenant
    matcher, and promoting one would funnel custom alerts into the platform NOC
    (the exact leak the isolation subtree exists to prevent). Per-rule
    ``tenant-<t>-override-<idx>`` routes are children of the main tenant route
    since #2252 (only the top level of *routes* is scanned here, and those
    children carry no tenant matcher); they reach the custom subtree through
    the returned main route's ``routes`` (#2342).

    Returns ``{tenant: main_route}``; when a tenant has several, the first wins
    (the one Alertmanager would match).
    """
    tenants: dict[str, dict] = {}
    for route in routes or []:
        receiver = route.get("receiver")
        for matcher in route.get("matchers", []) or []:
            m = re.match(r'^tenant="(.+)"$', matcher)
            if m and receiver == f"tenant-{m.group(1)}":
                tenants.setdefault(m.group(1), route)
                break
    return tenants


def _inject_custom_alert_isolation(routes: list[dict], receivers: list[dict]) -> tuple[list[dict], list[dict]]:
    """Prepend the four platform-static top-of-tree routes — the Watchdog liveness
    route (index 0, ADR-025 D1 / #838), the Custom Alerts isolation route (index 1,
    #741 S7/S8), the synthetic-probe sinkhole route (index 2, ADR-025 interop), then
    the sentinel sinkhole route (index 3, #1095) — plus their receivers, so the
    final ConfigMap always pins them AHEAD of the enforced NOC route and they
    survive the route-REPLACE that --apply performs on route.routes.

    Order is load-bearing. Watchdog MUST be index 0 (highest priority) so its
    heartbeat can never be intercepted by a broader earlier route; the custom
    isolation route follows at index 1, the synthetic-probe sink at index 2, the
    sentinel sink at index 3. The four matchers are mutually exclusive
    (alertname="Watchdog" vs component="custom" vs component="synthetic-probe" vs
    component="sentinel"), so none shadows another, but the positions are pinned
    for determinism and audit.

    #1092 0-pre: the custom isolation route is rebuilt with per-tenant child
    routes derived from the CURRENT main tenant routes (see
    _main_tenant_routes) — each child points at the existing
    ``tenant-<name>`` receiver and (#2342) carries that main route's sub-routes;
    tenants without a main tenant route get no child and fall back to the
    parent's ``custom-alerts-firehose``.

    Idempotent: any pre-existing Watchdog / component="custom" / component=
    "synthetic-probe" / component="sentinel" route is dropped and re-prepended
    canonically (for the custom route this drops the WHOLE tree, stale children
    included — it is rebuilt from this run's tenant routes, so removed tenants'
    children vanish and re-merging an already-injected config does not duplicate
    or mis-order), and the name-only placeholder receivers are added only if
    absent — a richer existing/base definition (e.g. watchdog-heartbeat's
    url_file) is preserved, never duplicated or clobbered, here.
    """
    wd_routes, wd_receivers = _build_watchdog_route()
    probe_routes, probe_receivers = _build_synthetic_probe_route()
    sent_routes, sent_receivers = _build_sentinel_sinkhole_route()

    def _is_watchdog(r: dict) -> bool:
        return 'alertname="Watchdog"' in r.get("matchers", [])

    def _is_custom(r: dict) -> bool:
        return 'component="custom"' in r.get("matchers", [])

    def _is_probe(r: dict) -> bool:
        return 'component="synthetic-probe"' in r.get("matchers", [])

    def _is_sentinel_sink(r: dict) -> bool:
        return 'component="sentinel"' in r.get("matchers", [])

    rest = [r for r in (routes or [])
            if not _is_watchdog(r) and not _is_custom(r) and not _is_probe(r)
            and not _is_sentinel_sink(r)]
    # #1092 0-pre: rebuild the custom route AFTER filtering, from the surviving
    # main tenant routes — the dropped stale custom route never contributes
    # children, so the subtree always mirrors this run's tenant set.
    mains = _main_tenant_routes(rest)
    cust_routes, cust_receivers = _build_custom_alert_routes(
        list(mains), {t: r.get("routes") or [] for t, r in mains.items()})
    # Order is load-bearing + pinned for determinism: Watchdog (0) → Custom (1) →
    # synthetic-probe (2) → sentinel sink (3), all ahead of the enforced NOC
    # match-all. The four matchers are mutually exclusive so none shadows another.
    out_routes = wd_routes + cust_routes + probe_routes + sent_routes + rest

    have = {r["name"] for r in (receivers or [])}
    add_recv = [r for r in (wd_receivers + cust_receivers + probe_receivers
                            + sent_receivers)
                if r["name"] not in have]
    out_receivers = list(receivers or []) + add_recv
    return out_routes, out_receivers


def render_output(routes: list[dict], receivers: list[dict], inhibit_rules: list[dict] | None = None) -> str:
    """Render Alertmanager route + receiver + inhibit config as YAML fragment.

    Constructs a clean YAML dictionary containing the tenant routing config
    (route tree + receivers + inhibit rules) suitable for merging into an
    existing alertmanager.yml or for --dry-run output.

    Args:
        routes: list of Alertmanager route dicts
        receivers: list of Alertmanager receiver dicts
        inhibit_rules: optional list of inhibit_rule dicts (severity dedup)

    Returns:
        YAML string fragment with keys: route (with nested routes), receivers, inhibit_rules.
        Empty sections are omitted from the output.
    """
    # Build the fragment as a clean dict
    fragment = {}

    if routes:
        fragment["route"] = {
            "routes": routes,
        }

    if receivers:
        fragment["receivers"] = receivers

    if inhibit_rules:
        fragment["inhibit_rules"] = inhibit_rules

    return yaml.dump(fragment, default_flow_style=False, allow_unicode=True, sort_keys=False)


# ── Root receiver with no integration (#2660) ──────────────────────

def receiver_integration_kinds(receiver: object) -> list[str]:
    """The Alertmanager integration kinds a receiver entry actually carries.

    One per ``<kind>_configs`` key whose value is a NON-EMPTY list —
    ``webhook_configs: []`` notifies no one, exactly like a name-only
    receiver, so it is not a kind. Shared by the generator's root-receiver
    WARN and ``explain_route``'s ``receiver_type`` (#2660), so the two cannot
    disagree about whether a receiver delivers anywhere.
    """
    if not isinstance(receiver, dict):
        return []
    return [k[:-len("_configs")] for k, v in receiver.items()
            if isinstance(k, str) and k.endswith("_configs")
            and isinstance(v, list) and v]


ROOT_RECEIVER_DOC = ("docs/integration/byo-alertmanager-integration.en.md "
                     "§11 \"Delivering Platform Self-Monitoring Alerts\"")


def root_receiver_without_integration_warning(am: object) -> str | None:
    """The WARN for an Alertmanager config whose root route's receiver has no
    integration, or None when it has one (#2660).

    Every alert no child route claims ends at the root receiver — that
    includes the platform's own self-monitoring alerts unless conf.d routes
    them (``_routing_enforced``). A root receiver with no non-empty
    ``*_configs`` (the built-in base's ``default``, and the shipped
    ``k8s/03-monitoring`` one) drops them silently. Not an error: the
    shipped posture is deliberate, so this never changes the exit code and
    ``--strict`` does not escalate it. A root receiver name that no receiver
    entry defines counts as no integration too (amtool rejects that anyway).
    """
    route = am.get("route") if isinstance(am, dict) else None
    name = route.get("receiver") if isinstance(route, dict) else None
    receivers = am.get("receivers") if isinstance(am, dict) else None
    entry = next((r for r in (receivers if isinstance(receivers, list) else [])
                  if isinstance(r, dict) and r.get("name") == name), None)
    if entry is not None and receiver_integration_kinds(entry):
        return None
    return (f"WARN: the root route's receiver {safe_label(name)!r} has no "
            "integration (no non-empty *_configs) — every alert no child "
            "route matches, including the platform's own self-monitoring "
            "alerts, ends there and notifies no one. Not an error; to deliver "
            f"platform alerts see {ROOT_RECEIVER_DOC}.")


def warn_if_root_receiver_without_integration(am: object) -> None:
    """Print :func:`root_receiver_without_integration_warning` to stderr."""
    warning = root_receiver_without_integration_warning(am)
    if warning:
        print(warning, file=sys.stderr)


# ── §11.3 AM GitOps: --output-configmap ────────────────────────────

# Minimal inline defaults when --base-config is not provided
_DEFAULT_BASE_CONFIG = {
    "global": {"resolve_timeout": "5m"},
    "route": {
        "group_by": ["alertname", "tenant"],
        "group_wait": "10s",
        "group_interval": "10s",
        "repeat_interval": "12h",
        "receiver": "default",
    },
    "receivers": [{"name": "default"}],
    "inhibit_rules": [],
}


class BaseConfigInputError(ValueError):
    """`--base-config` was supplied but the value cannot serve as a base config.

    A dedicated subclass rather than a bare ``ValueError`` because the
    ``assert_*`` guards this module calls raise plain ``ValueError`` for a
    config-invariant violation. The two mean opposite things — a violation is
    the operator's CONFIG being wrong (exit 1), this is their INVOCATION being
    wrong (exit 2) — so a caller that caught ``ValueError`` could not tell them
    apart. Same reasoning as ``PolicyInputError`` in ``_grar_validate``.

    Pinned by ``test_the_error_type_is_distinguishable_from_a_config_violation``;
    aliasing this to ``ValueError`` must go red.
    """


def load_base_config(path: str | None) -> dict:
    """Load the base Alertmanager config that generated routes merge into.

    Two outcomes, and they must stay distinguishable (#1616):

    * the flag was OMITTED — ``path is None`` — the built-in defaults apply;
    * the flag was SUPPLIED with a value that cannot serve as a base config —
      raise, and the caller exits 2 (dev-rules #13 / ``_lib_exitcodes``).

    ⛔ "Supplied" is ``path is None``, NOT ``not path``. An empty string is
    supplied — it is what an unset shell variable expands to — and a falsy test
    routes it into the omitted branch, which is the whole defect.

    Everything past the omitted branch reduces to two re-derivable predicates —
    *the value names a file*, and *that file parses to a mapping* — so no shape
    is enumerated and an unanticipated spelling lands on an existing branch.

    ⚠️ NOT closed: the CONTENT axis, as ``load_policy``'s docstring records for
    ``--policy``. A file that IS a mapping but whose ``global:`` is empty or
    wrong still merges silently. Partial files are legitimate (absent top-level
    keys fall back per key, pinned by ``test_partial_file_fills_defaults``), so
    "is a mapping" is as far as the path axis reaches.
    """
    if path is None:
        return copy.deepcopy(_DEFAULT_BASE_CONFIG)
    if not Path(path).is_file():
        raise BaseConfigInputError(
            f"--base-config: not a file: {path!r}\n"
            "  --base-config takes a PATH to a base Alertmanager YAML "
            "(global / route / receivers / inhibit_rules).\n"
            "  ⛔ Do not drop the flag to clear this error — that substitutes "
            "the built-in placeholder `global:` for yours, which is the exact "
            "silent failure this error exists to stop.")
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except UnicodeDecodeError as exc:
        raise BaseConfigInputError(
            f"--base-config: {path!r} is not valid UTF-8: {exc}") from exc
    except yaml.YAMLError as exc:
        raise BaseConfigInputError(
            f"--base-config: {path!r} is not valid YAML: {exc}") from exc
    except OSError as exc:
        raise BaseConfigInputError(
            f"--base-config: cannot read {path!r}: {exc}") from exc
    if not isinstance(data, dict):
        # ⛔ No remedy sentence here, deliberately. This branch fires for an
        # empty file AND for one holding a top-level list or scalar, and "omit
        # --base-config" is right for the first and reproduces #1616 for the
        # second. ``load_policy``'s equivalent branch states the type and stops
        # for the same reason; ``validate_config`` carries a comment about the
        # third time a shared remedy sentence was wrong for one of its rows.
        raise BaseConfigInputError(
            f"--base-config: top level of {path!r} is {type(data).__name__}, "
            "expected a mapping with `global:` / `route:` / `receivers:` / "
            "`inhibit_rules:` (any subset; absent keys fall back).")
    # Ensure required keys exist. deepcopy so a caller mutating the result
    # cannot reach into _DEFAULT_BASE_CONFIG's nested lists and change what
    # every later call in this process returns.
    for key in ("global", "route", "receivers", "inhibit_rules"):
        if key not in data:
            data[key] = copy.deepcopy(_DEFAULT_BASE_CONFIG[key])
    return data


def _platform_placeholder_receiver_names() -> frozenset[str]:
    """The name-only receivers ``_inject_custom_alert_isolation`` adds only if
    absent — a base that defines them richer (watchdog-heartbeat's url_file)
    is the intended shape, not a shadow. Derived from the builders, so a fifth
    placeholder cannot be forgotten here."""
    names = set()
    for _routes, recvs in (_build_watchdog_route(), _build_custom_alert_routes([]),
                           _build_synthetic_probe_route(),
                           _build_sentinel_sinkhole_route()):
        names.update(r["name"] for r in recvs)
    return frozenset(names)


def assert_base_does_not_shadow_generated(base_receivers: list | None,
                                          generated: list[dict] | None) -> None:
    """Fail-closed guard (#2279): raise ValueError when a base-config receiver
    has the name of a receiver this run generates from conf.d.

    ``assemble_configmap`` keeps the base receiver and drops the generated one
    of the same name, so the conf.d definition would vanish without a word.
    ⛔ The ``--apply`` path (``_merge_routes_receivers_inhibits``) is NOT
    guarded: there the same-named receiver in the cluster is the previous
    run's output, and replacing it is the point (owner decision on #2279).
    """
    placeholders = _platform_placeholder_receiver_names()
    generated_names = {r.get("name") for r in (generated or [])
                       if isinstance(r, dict)} - placeholders
    shadowed = sorted({r.get("name") for r in (base_receivers or [])
                       if isinstance(r, dict)} & generated_names)
    if not shadowed:
        return
    raise ValueError(
        "ERROR: the base config defines receiver(s) with the same name as "
        f"receiver(s) generated from conf.d: {', '.join(map(repr, shadowed))}. "
        "The base definition would win and the conf.d one would be silently "
        "dropped, so alerts would go to the base's endpoint. Remove or rename "
        "them in the base config (receivers generated from conf.d do not "
        "belong in it).")


def assemble_configmap(base: dict, routes: list[dict], receivers: list[dict], inhibit_rules: list[dict],
                       namespace: str = "monitoring", configmap_name: str = "alertmanager-config",
                       strict: bool = False) -> str:
    """Merge tenant routing fragments into base Alertmanager config and wrap as K8s ConfigMap.

    Merges generated routes, receivers, and inhibit_rules into a base Alertmanager
    configuration, then wraps the result as a Kubernetes ConfigMap YAML suitable
    for kubectl apply or GitOps workflows.

    Merge Strategy:
      - Routes: replace base route.routes with generated tenant routes
      - Receivers: append tenant receivers to base receivers (dedup by name)
      - Inhibit Rules: append tenant rules to base rules
      - Global/Other: preserve from base config

    Args:
        base: base Alertmanager config dict (from load_base_config or YAML file)
        routes: generated tenant route dicts
        receivers: generated tenant receiver dicts
        inhibit_rules: generated inhibit_rules for severity dedup
        namespace: K8s namespace for ConfigMap (default: monitoring)
        configmap_name: ConfigMap name (default: alertmanager-config)

    Returns:
        Complete Kubernetes ConfigMap YAML string (apiVersion, kind, metadata, data.alertmanager.yml).
    """
    merged = dict(base)

    # #2279: a base receiver named like a generated one would WIN the
    # de-duplicating merge below, silently dropping the conf.d definition
    # (measured: alerts went to the base's stale endpoint, rc 0, amtool
    # accepted). Checked on what the generator produced, BEFORE the platform
    # placeholders are injected — those are "add only if absent" by design.
    assert_base_does_not_shadow_generated(merged.get("receivers", []), receivers)

    # S7/S8 (#741): ensure the Custom Alerts isolation route + firehose receiver
    # are present and FIRST, regardless of what generate_routes produced.
    routes, receivers = _inject_custom_alert_isolation(routes, receivers)

    # Merge routes into base route
    merged_route = dict(merged.get("route", {}))
    merged_route["routes"] = routes
    merged["route"] = merged_route

    # Merge receivers: keep base receivers, append tenant receivers
    base_names = {r["name"] for r in merged.get("receivers", [])}
    tenant_receivers = [r for r in receivers if r["name"] not in base_names]
    merged["receivers"] = list(merged.get("receivers", [])) + tenant_receivers

    # Merge inhibit_rules: keep base rules, append tenant rules
    merged["inhibit_rules"] = list(merged.get("inhibit_rules", [])) + list(inhibit_rules or [])

    # ADR-025 D1: fail-closed if any inhibit rule (base or generated) would
    # suppress the always-firing Watchdog heartbeat — it must always egress.
    assert_watchdog_inhibit_immunity(merged["inhibit_rules"])

    # A tenant-triggered inhibit (Silent Mode) must never suppress a PLATFORM
    # self-monitoring alert — three of them carry a tenant label at fire time.
    assert_platform_alerts_not_tenant_silenceable(merged["inhibit_rules"])

    # #1132: every equal-label must be presence-gated on some side (strict → fail,
    # else → warn). Runs on the base+generated merge, so a hand-written base rule
    # (Silent Mode / Custom silence) is guarded here too.
    _enforce_equal_labels_gated(merged["inhibit_rules"], strict)

    # Render alertmanager.yml content
    am_yml = yaml.dump(merged, default_flow_style=False,
                       allow_unicode=True, sort_keys=False)

    # Wrap in ConfigMap structure
    configmap = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": configmap_name,
            "namespace": namespace,
        },
        "data": {
            "alertmanager.yml": am_yml,
        },
    }
    return yaml.dump(configmap, default_flow_style=False,
                     allow_unicode=True, sort_keys=False)


# ============================================================
# ConfigMap Operations (K8s cluster deployment)
# ============================================================

def _run_binary(argv: list[str], *, timeout: int,
                stdin_text: str | None = None,
                text: bool = True) -> subprocess.CompletedProcess:
    """Run *argv*, turning "that binary is not runnable here" into a result.

    ⛔ ``subprocess.run`` raises ``OSError`` when the binary is absent, and that
    escaped uncaught: an image without ``kubectl`` produced a traceback at rc=1,
    which in this repo means EXIT_VIOLATION — "your config is wrong" — for what
    is an environment problem. Measured with ``PATH`` reduced to the interpreter
    directory before this helper existed.

    Returns a CompletedProcess so every caller's existing ``returncode != 0``
    branch reports it, with 127 (the shell's "command not found") rather than a
    code that could be confused with the binary's own exit statuses.

    ⛔ ``timeout=`` is passed here, so the OTHER way this call fails is
    ``subprocess.TimeoutExpired`` — which is **not** an ``OSError``
    (``SubprocessError`` → ``Exception``), so the handler below does not see it.
    A cluster that accepts the connection and never answers therefore produced
    the same uncaught traceback at rc=1 that this helper exists to remove. Both
    families must be named; catching one and assuming the other is the shape of
    defect this file's own history records twice (``EOFError`` is not an
    ``OSError`` either). 124 is what ``timeout(1)`` reports, kept distinct from
    127 so the two environment failures stay tellable apart.

    ``text=False`` returns stdout / stderr as bytes (the two failure results
    too), for a caller that must not have ``\r`` translated to ``\n``.
    """
    enc = (lambda m: m) if text else (lambda m: m.encode("utf-8"))
    try:
        if not text:
            return subprocess.run(argv, input=None if stdin_text is None
                                  else stdin_text.encode("utf-8"),
                                  capture_output=True, timeout=timeout)
        return subprocess.run(argv, input=stdin_text, capture_output=True,
                              text=True, timeout=timeout, encoding="utf-8")
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            argv, returncode=124, stdout=enc(""),
            stderr=enc(f"{argv[0]!r} did not finish within {exc.timeout}s"))
    except OSError as exc:
        return subprocess.CompletedProcess(
            argv, returncode=127, stdout=enc(""),
            stderr=enc(f"cannot run {argv[0]!r}: {exc}"))


# ── #2219: Alertmanager's own parser as the last gate ───────────────
# The JSON schema and this generator's Python checks accept values that
# Alertmanager refuses at load time (measured: a webhook URL `http://[1]/`, and
# a host carrying U+0085 that the YAML emitter folds into a line break, which
# Alertmanager then reads back as a space). Only Alertmanager's parser is the
# authority on that, so when `amtool` is on PATH it gets the final say — on the
# SAME text that is about to be written or applied, never on a re-render.
# The da-tools image bundles amtool from the deployed Alertmanager image (#2294;
# parity with the manifest is tests/ops/test_da_tools_amtool_pin_parity.py).
# Outside the image, without amtool the run is unchanged EXCEPT that it says,
# every time, that nothing validated it.
AMTOOL_NOT_FOUND_NOTICE = (
    "NOTICE: amtool not found on PATH; generated Alertmanager config was NOT "
    "validated by Alertmanager")



# `AmtoolCheck.status` values. ⛔ Four, not three: amtool present but unable to
# give a verdict (could not run, timed out, crashed) is neither "rejected" (a
# finding about the config) nor "not found" (a run that says it was not
# validated) — it is an environment failure and every caller reports it as one.
AMTOOL_ACCEPTED = "accepted"
AMTOOL_REJECTED = "rejected"
AMTOOL_NOT_FOUND = "not_found"
AMTOOL_UNUSABLE = "unusable"


@dataclass(frozen=True)
class AmtoolCheck:
    """What ``amtool check-config`` said, as data (#2311).

    *exit_code* is ``None`` to go on, else the code to stop with; *message* is
    the exact stderr text :func:`amtool_gate` prints for it.
    """
    status: str
    exit_code: int | None
    message: str


def amtool_check(am_yml: str, *, what: str, refusing: str,
                 not_found_notice: str = AMTOOL_NOT_FOUND_NOTICE) -> AmtoolCheck:
    """Run ``amtool check-config`` over *am_yml* — the exact text Alertmanager
    will load — and return the verdict WITHOUT printing it (#2311: validate-
    config reports it in a row; ``amtool_gate`` prints it).

    * amtool absent → ``not_found``, exit None (the rc is unchanged, but the
      message says the run was not validated — never silent);
    * amtool accepts → ``accepted``, exit None;
    * amtool rejects (rc 1 with its ``FAILED:`` verdict) → ``rejected``,
      EXIT_VIOLATION (the CONFIG is wrong: the tool ran and found something
      the user must fix);
    * anything else non-zero — could not run, timed out, crashed (a Go panic
      exits 2), a wrapper that fails → ``unusable``, EXIT_CALLER_ERROR: no
      verdict on the config, so it must not read as one.

    *what* names the text for the operator; *refusing* is what the caller will
    NOT do because of a failure ("write -o …", "apply …"). *not_found_notice*
    lets a caller whose text is not the one it ships say so when amtool is
    absent (#2260: ``--validate`` checks an assembly on the built-in base).

    ⛔ The temp file is written with ``newline=""`` so amtool reads the same
    characters the caller holds; universal-newline translation would hand it
    a different text than the one that ships.
    """
    amtool = shutil.which("amtool")
    if amtool is None:
        return AmtoolCheck(AMTOOL_NOT_FOUND, None, not_found_notice)
    with tempfile.TemporaryDirectory(prefix="grar-amtool-") as tmp:
        path = Path(tmp) / "alertmanager.yml"
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(am_yml)
        # receiver URLs / keys live in here; the 0700 temp dir already fences
        # it, this keeps the file itself owner-only too (SAST write gate).
        os.chmod(path, 0o600)
        result = _run_binary([amtool, "check-config", str(path)], timeout=60)
    output = "\n".join(safe_label(ln) for ln in
                       (result.stdout + result.stderr).strip().splitlines())
    if result.returncode == 0:
        return AmtoolCheck(
            AMTOOL_ACCEPTED, None,
            f"amtool check-config: {what} accepted by Alertmanager's parser "
            f"({safe_label(amtool)})")
    if not (result.returncode == 1 and "FAILED:" in output):
        return AmtoolCheck(
            AMTOOL_UNUSABLE, EXIT_CALLER_ERROR,
            f"ERROR: amtool check-config could not validate {what} "
            f"(rc={result.returncode}) — refusing to {refusing}:\n{output}")
    return AmtoolCheck(
        AMTOOL_REJECTED, EXIT_VIOLATION,
        f"FAIL: amtool check-config rejected {what} (rc={result.returncode}) "
        f"— refusing to {refusing}. Alertmanager would refuse to load it:\n"
        f"{output}")


def amtool_gate(am_yml: str, *, what: str, refusing: str,
                not_found_notice: str = AMTOOL_NOT_FOUND_NOTICE) -> int | None:
    """:func:`amtool_check`, printed. Returns ``None`` to go on, or the exit
    code to stop with. Everything goes to stderr: in some modes stdout IS the
    generated config."""
    check = amtool_check(am_yml, what=what, refusing=refusing,
                         not_found_notice=not_found_notice)
    print(check.message, file=sys.stderr)
    return check.exit_code


# ── #2311: the ONE verdict on a generation result ───────────────────
# `generate_alertmanager_routes --validate` and validate-config's `routes` row
# both call `evaluate_generated_config`. Before it, validate-config ran only
# the first of these steps, so a tree `--validate` failed (a tripwire, an
# invariant `assemble_configmap` refuses, an amtool rejection) was a PASS row
# at rc 0 — #2164's drift, one layer further down.
#
# #2260: what --validate hands to amtool is assembled on the BUILT-IN base
# (`--validate --base-config` stays a caller error, #1616), so a pass says
# nothing about the operator's own base — the NOTICE / result lines say so.
VALIDATE_AMTOOL_WHAT = ("the generated config (assembled on the built-in "
                        "default base, never on a --base-config)")
VALIDATE_AMTOOL_NOT_FOUND_NOTICE = (
    "NOTICE: amtool not found on PATH; --validate did NOT check the generated "
    "config with Alertmanager's parser (that check assembles it on the "
    "built-in default base, never on a --base-config)")


@dataclass
class GeneratedConfigVerdict:
    """The outcome of :func:`evaluate_generated_config`, as data.

    * ``errors`` — blocking lines, in the text ``--validate`` prints under
      ``FAIL: N error(s) found:``;
    * ``assembly_error`` — the ``ValueError`` text a platform invariant in
      ``assemble_configmap`` refused the assembled config with;
    * ``warnings`` — non-blocking lines the checks' helpers wrote to stderr
      while running (an equal-label not presence-gated, a degraded platform-
      alert probe set), captured so the caller decides where they go;
    * ``amtool`` — ``None`` when the run stopped before amtool (errors, or the
      assembly was refused), else its :class:`AmtoolCheck`.

    ⛔ The steps short-circuit exactly as ``--validate`` does: amtool is never
    asked about a config the Python checks already refused.
    """
    errors: list[str]
    assembly_error: str | None = None
    warnings: list[str] = field(default_factory=list)
    amtool: AmtoolCheck | None = None

    @property
    def exit_code(self) -> int | None:
        """``None`` = the run may say OK (amtool may still be absent)."""
        if self.errors or self.assembly_error is not None:
            return EXIT_VIOLATION
        return self.amtool.exit_code if self.amtool is not None else None


def evaluate_generated_config(routes: list[dict], receivers: list[dict],
                              inhibit_rules: list[dict], warnings: list[str],
                              *, extra_errors: "list[str] | tuple" = ()
                              ) -> GeneratedConfigVerdict:
    """Every ``--validate`` verdict on a generation result, returned — never
    printed, never ``sys.exit``-ed (#2311).

    In order, stopping at the first step that blocks:

    1. ``blocking_generation_errors(warnings)`` — entries skipped as unusable,
       duplicate receiver names (#2279);
    2. *extra_errors* — caller-selected blocking lines (``--validate --strict``
       passes its ADR-007 policy errors here, so they sit where they always
       printed and still keep amtool from running);
    3. the ADR-025 tripwires on the GENERATED inhibit rules: Watchdog
       suppression, a tenant-triggered rule silencing a platform alert;
    4. ``assemble_configmap`` on the built-in base — a ``ValueError`` is a
       verdict on the config (#2260 / #2279);
    5. ``amtool check-config`` on that assembly (:func:`amtool_check`).
    """
    errors = blocking_generation_errors(warnings)
    errors.extend(extra_errors)
    verdict = GeneratedConfigVerdict(errors=errors)
    cm_yaml = None
    captured = io.StringIO()
    with contextlib.redirect_stderr(captured):
        # ADR-025 D1 regression tripwire: a generated inhibit rule must never
        # target the Watchdog heartbeat. (The full base+generated set is
        # enforced fail-closed at the render paths; this catches a
        # generator-side regression early.)
        for idx, _rule in find_watchdog_suppressing_inhibits(inhibit_rules):
            errors.append(f"  WARN: generated inhibit_rules[{idx}] would "
                          "suppress the Watchdog heartbeat (ADR-025) — "
                          "skipping forbidden rule")
        # Same early tripwire for the tenant-cannot-silence-platform invariant.
        for idx, _rule, lbls in find_tenant_silenceable_platform_inhibits(
                inhibit_rules):
            errors.append(f"  WARN: generated inhibit_rules[{idx}] is "
                          "tenant-triggered and would suppress platform alert "
                          f"{lbls.get('alertname')} — skipping forbidden rule")
        if not errors:
            try:
                cm_yaml = assemble_configmap(load_base_config(None), routes,
                                             receivers, inhibit_rules)
            except ValueError as exc:
                verdict.assembly_error = str(exc)
    verdict.warnings = captured.getvalue().splitlines()
    if cm_yaml is None:
        return verdict
    am_yml = yaml.safe_load(cm_yaml)["data"]["alertmanager.yml"]
    verdict.amtool = amtool_check(
        am_yml, what=VALIDATE_AMTOOL_WHAT,
        refusing="report the config as valid",
        not_found_notice=VALIDATE_AMTOOL_NOT_FOUND_NOTICE)
    return verdict


class AlertmanagerConfigRejected(Exception):
    """``amtool_gate`` stopped ``apply_to_configmap`` before anything reached
    the cluster. Carries the exit code, which is NOT the ``False`` return's 2:
    a rejected config is the operator's CONFIG being wrong (1)."""

    def __init__(self, exit_code: int):
        super().__init__(exit_code)
        self.exit_code = exit_code


class AlertmanagerConfigInvariantViolated(ValueError):
    """#2506: merging the generated fragment into the CLUSTER's config broke a
    platform invariant (ADR-025: an inhibit rule that would suppress the
    Watchdog heartbeat or let a tenant silence a platform alert; #1132 under
    ``strict``). Raised by ``apply_to_configmap`` before amtool or kubectl
    apply runs, so nothing reached the cluster. A verdict on the config (rc
    1), like ``assemble_configmap``'s ``ValueError`` on the other write paths
    (#2260) — a ``ValueError`` subclass for that reason, and a class of its
    own so the caller catches the merge step only, not every ``ValueError``
    the apply path could raise."""


def _read_existing_configmap(namespace: str, configmap_name: str) -> tuple[dict | None, list[str]]:
    """Read existing Alertmanager ConfigMap from K8s cluster.

    Returns (config_dict, warnings) — config_dict is None if read failed.
    """
    warnings = []
    result = _run_binary(
        ["kubectl", "get", "configmap", configmap_name, "-n", namespace, "-o", "json"],
        timeout=60)
    if result.returncode != 0:
        warnings.append(f"ERROR: Failed to read ConfigMap {configmap_name}: {result.stderr}")
        return None, warnings

    # ⛔ This function must not hand the caller a `None` it cannot explain:
    # every failure exit below appends a warning first, and `apply_to_configmap`
    # prints them. Measured before that rule was applied: two paths returned
    # None silently (an `alertmanager.yml` value that is comment-only, and one
    # that is whitespace-only), so the caller exited 2 with zero output — the
    # same "exit code with no diagnostic" this module refuses to ship for `-o`.
    #
    # ⛔ "Every return None appends a warning" was ALSO once written here while
    # a whole class did not reach a `return None` at all: a `cm` that parses to
    # something other than a mapping went out as an uncaught AttributeError on
    # `.get`, i.e. a traceback at rc=1. Guarding the shape of `cm` is what makes
    # the sentence true, not the sentence.
    try:
        cm = json.loads(result.stdout)
    except ValueError as exc:
        warnings.append(
            f"ERROR: kubectl returned output that is not JSON: {exc}")
        return None, warnings
    if not isinstance(cm, dict):
        warnings.append(
            f"ERROR: kubectl returned JSON that is not an object "
            f"({type(cm).__name__}); expected a ConfigMap resource")
        return None, warnings
    if "data" not in cm:
        warnings.append("ERROR: ConfigMap has no `data` block at all")
        return None, warnings
    data = cm["data"]
    if not isinstance(data, dict):
        warnings.append(
            f"ERROR: ConfigMap `data` is {type(data).__name__}, expected a "
            f"mapping of keys to strings")
        return None, warnings
    # ⛔ Membership, not truthiness — the same distinction the two guards above
    # draw for `data`. `data.get(key, "")` collapsed "the cluster has no such
    # key" into "the key is there and empty", and the message named the first:
    # an operator who emptied `alertmanager.yml` was sent looking for a missing
    # key that is present. It also swallowed every falsy non-string (`0`,
    # `false`, `[]`) before the type guard below could name its type. An empty
    # string now flows on and is diagnosed by the mapping guard further down,
    # which reads the raw TEXT and says the key holds nothing usable.
    if "alertmanager.yml" not in data:
        warnings.append("ERROR: ConfigMap has no 'alertmanager.yml' key")
        return None, warnings
    existing_yml = data["alertmanager.yml"]
    if not isinstance(existing_yml, str):
        # ⛔ `yaml.safe_load` treats a non-str argument as a STREAM and calls
        # `.read()` on it, so a non-string value left here as an uncaught
        # AttributeError at rc=1 with no diagnostic — the exact picture the
        # guards above were added to remove, one layer further in. The
        # Kubernetes API forces `data` values to be strings, so this is not
        # operator-reachable through a real cluster; it is guarded because it
        # is exactly as reachable as the two shapes just above it, and a
        # sentence claiming "every failure exit appends a warning" has to be
        # true for the whole function or it is not worth writing.
        warnings.append(
            f"ERROR: ConfigMap key 'alertmanager.yml' is "
            f"{type(existing_yml).__name__}, expected a string")
        return None, warnings

    try:
        existing = yaml.safe_load(existing_yml)
    except yaml.YAMLError as exc:
        warnings.append(
            f"ERROR: ConfigMap key 'alertmanager.yml' is not valid YAML: {exc}")
        return None, warnings
    if not isinstance(existing, dict):
        # ⛔ The parenthetical is a GUESS, so it is gated on evidence rather
        # than on the parse result. An unconditional version was measured
        # firing for list / str / int values, where the key holds something and
        # the guess is wrong. Gating on `existing is None` was still too wide:
        # `null`, `~`, `---` and `Null` also parse to None while the key does
        # hold text. The predicate below reads the TEXT — blank, or nothing but
        # comments — which is the thing the sentence actually claims.
        stripped = [ln.strip() for ln in existing_yml.splitlines()]
        looks_empty = all(not ln or ln.startswith("#") for ln in stripped)
        hint = (" — the key exists but holds nothing usable (comments or "
                "whitespace only)") if looks_empty else ""
        warnings.append(
            f"ERROR: ConfigMap key 'alertmanager.yml' parses to "
            f"{type(existing).__name__}, expected a mapping{hint}")
        return None, warnings
    return existing, warnings


def _merge_routes_receivers_inhibits(existing: dict, routes: list[dict],
                                     receivers: list[dict], inhibit_rules: list[dict],
                                     strict: bool = False) -> dict:
    """Merge generated routes, receivers, and inhibit rules into existing config.

    Returns merged config dict.
    """
    # S7/S8 (#741): keep the Custom Alerts isolation route + firehose receiver
    # present and FIRST across --apply (which REPLACES route.routes).
    routes, receivers = _inject_custom_alert_isolation(routes, receivers)

    if routes:
        if "route" not in existing:
            existing["route"] = {}
        existing["route"]["routes"] = routes

    if receivers:
        # Generated tenant receivers REPLACE the existing same-named ones (they
        # are regenerated from conf.d each run). BUT the injected platform-static
        # placeholders (custom-alerts-firehose / watchdog-heartbeat / synthetic-
        # receiver / sentinel-sinkhole are emitted NAME-ONLY) must NOT clobber a
        # richer base definition — most critically
        # watchdog-heartbeat's webhook_configs[].url_file, which lives only in the
        # base ConfigMap. For a name-only placeholder, defer to the existing
        # definition if one is present; otherwise add the placeholder so the route
        # still resolves.
        existing_by_name = {r["name"]: r for r in existing.get("receivers", [])}
        gen_names = {r["name"] for r in receivers}

        def _resolve(r: dict) -> dict:
            if set(r.keys()) == {"name"} and r["name"] in existing_by_name:
                return existing_by_name[r["name"]]
            return r

        merged_gen = [_resolve(r) for r in receivers]
        kept = [r for r in existing.get("receivers", [])
                if r["name"] not in gen_names]
        existing["receivers"] = kept + merged_gen

    if inhibit_rules:
        # Keep non-generated inhibit rules (e.g., Silent Mode sentinel rules)
        kept_rules = [r for r in existing.get("inhibit_rules", [])
                      if not any('metric_group' in m for m in r.get("source_matchers", []))]
        existing["inhibit_rules"] = kept_rules + inhibit_rules

    # ADR-025 D1: fail-closed on the FINAL inhibit set (validated even when no
    # inhibit rules were generated this run) — the Watchdog heartbeat must never
    # be inhibited before it reaches the external dead-man's-switch.
    assert_watchdog_inhibit_immunity(existing.get("inhibit_rules", []))
    # Same tenant-cannot-silence-platform invariant on the --apply merge path.
    assert_platform_alerts_not_tenant_silenceable(existing.get("inhibit_rules", []))
    # #1132: same equal-label-gated invariant on the --apply merge path.
    _enforce_equal_labels_gated(existing.get("inhibit_rules", []), strict)

    return existing


def _apply_merged_configmap(merged_yml: str, namespace: str, configmap_name: str) -> bool:
    """Apply merged ConfigMap to K8s cluster.

    Returns True if successful, False otherwise.
    """
    apply_result = _run_binary(
        ["kubectl", "create", "configmap", configmap_name,
         f"--from-literal=alertmanager.yml={merged_yml}",
         "-n", namespace, "--dry-run=client", "-o", "yaml"],
        timeout=60)
    if apply_result.returncode != 0:
        print(f"ERROR: Failed to generate ConfigMap: {apply_result.stderr}", file=sys.stderr)
        return False

    pipe_result = _run_binary(
        ["kubectl", "apply", "-f", "-"],
        timeout=120, stdin_text=apply_result.stdout)
    if pipe_result.returncode != 0:
        print(f"ERROR: kubectl apply failed: {pipe_result.stderr}", file=sys.stderr)
        return False

    print(f"ConfigMap {namespace}/{configmap_name} updated")
    return True


def _reload_alertmanager(namespace: str) -> bool:
    """Reload Alertmanager configuration via HTTP POST.

    Returns True on success, False when the reload failed (#2219). It used to
    return True on both branches (#1243 made the failure warning-level), so an
    --apply whose new config Alertmanager REJECTED exited 0 — the running
    Alertmanager was still on the old config and the tool said "done".
    """
    svc_url = f"http://alertmanager.{namespace}.svc.cluster.local:9093"
    reload_result = _run_binary(
        ["curl", "-sf", "-X", "POST", f"{svc_url}/-/reload"],
        timeout=60)
    if reload_result.returncode != 0:
        # NOT a missing flag: Alertmanager's /-/reload is unconditional (it has no
        # --web.enable-lifecycle — that is Prometheus'). A failure here is network
        # reachability, a NetworkPolicy, or a rejected config. (#1243)
        print(f"ERROR: Alertmanager reload failed (rc={reload_result.returncode}; "
              "service unreachable, blocked by NetworkPolicy, or the new config "
              "was rejected)",
              file=sys.stderr)
        # ⚠️ Only true for the reachability failures: if the reload was refused
        # because the NEW CONFIG IS INVALID, a restart will not save you either —
        # Alertmanager will fail to start on it. Verify before relying on a restart.
        print("ConfigMap was updated. If Alertmanager was merely unreachable it will "
              "load the new config on next restart; if the config was REJECTED, fix or "
              "roll it back first — a restart would fail too.",
              file=sys.stderr)
        return False

    print("Alertmanager reloaded")
    return True


def apply_to_configmap(routes: list[dict], receivers: list[dict], inhibit_rules: list[dict], namespace: str, configmap_name: str, strict: bool = False) -> bool:
    """Merge generated routing config into existing Alertmanager ConfigMap and reload.

    Applies tenant routing configuration directly to a running Alertmanager cluster.
    This is the --apply mode for immediate deployment without GitOps workflow.

    Process:
      1. kubectl get configmap → extract alertmanager.yml
      2. Merge generated routes, receivers, inhibit_rules into existing config
      3. amtool check-config on the merged text, when amtool is on PATH (#2219)
      4. kubectl apply ConfigMap with merged config
      5. curl POST /-/reload to trigger Alertmanager configuration reload

    Notes:
      - Keeps existing base routes/receivers, appends tenant-generated ones
      - Preserves non-generated inhibit rules (e.g., Silent Mode sentinel rules)
      - /-/reload needs no Alertmanager flag; it is always enabled (#1243)

    Args:
        routes: generated tenant route dicts
        receivers: generated tenant receiver dicts
        inhibit_rules: generated inhibit_rules for severity dedup
        namespace: K8s namespace where ConfigMap is located
        configmap_name: ConfigMap name (typically alertmanager-config)

    Returns:
        True if merge, apply and reload all succeeded, False otherwise.

    Raises:
        AlertmanagerConfigRejected: amtool refused the merged config (or could
        not run); nothing was sent to the cluster. #2219.
        AlertmanagerConfigInvariantViolated: the merged config breaks a
        platform invariant; nothing was sent to the cluster. #2506.
    """
    # 1. Read existing ConfigMap
    existing, read_warnings = _read_existing_configmap(namespace, configmap_name)
    if existing is None:
        for w in read_warnings:
            print(w, file=sys.stderr)
        return False

    # 2. Merge fragment into existing config. #2506: its `ValueError`s are the
    # merged-set invariant asserts — the cluster's own inhibit rules are kept
    # by the merge, so this is where an existing Watchdog-suppressing rule
    # surfaces. Only this step is wrapped.
    try:
        existing = _merge_routes_receivers_inhibits(existing, routes, receivers,
                                                    inhibit_rules, strict=strict)
    except ValueError as exc:
        raise AlertmanagerConfigInvariantViolated(str(exc)) from exc
    merged_yml = yaml.dump(existing, default_flow_style=False,
                           allow_unicode=True, sort_keys=False)

    # 3. #2219: Alertmanager's parser gets the final say BEFORE the cluster is
    # touched. `merged_yml` is the exact string handed to
    # `--from-literal=alertmanager.yml=` below, i.e. the text Alertmanager loads.
    rc = amtool_gate(merged_yml,
                     what=f"the merged alertmanager.yml for {namespace}/{configmap_name}",
                     refusing="apply it to the cluster (nothing was applied)")
    if rc is not None:
        raise AlertmanagerConfigRejected(rc)
    # #2660: the root route + receivers here are the CLUSTER's, kept by the
    # merge — the config about to be applied, so warn on it.
    warn_if_root_receiver_without_integration(yaml.safe_load(merged_yml))

    # 4. Apply updated ConfigMap
    if not _apply_merged_configmap(merged_yml, namespace, configmap_name):
        return False

    # 5. Reload Alertmanager
    return _reload_alertmanager(namespace)
