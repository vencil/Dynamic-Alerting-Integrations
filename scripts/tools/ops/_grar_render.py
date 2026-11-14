"""Output rendering + Alertmanager ConfigMap operations.

PR-3a (v2.8.0) extracted these helpers out of generate_alertmanager_routes.py
to bring the main file under the line-count cap. All symbols are re-exported
from generate_alertmanager_routes for backwards-compatible test imports.

Functions:
  Output rendering:
    load_base_config(text, path) → base AM YAML; defaults ONLY when the flag was
                                   omitted — a supplied-but-unusable value
                                   raises BaseConfigInputError (#1616)
    merge_generated(base, ...)   → the ONE merge every output mode uses (#1211):
                                   renders output = merge(source base, conf.d)
    assemble_alertmanager_yml(...) → merged alertmanager.yml text
    assemble_configmap(...)      → the same text wrapped as a K8s ConfigMap

  ConfigMap operations (--apply mode, K8s cluster deploy):
    _read_existing_configmap(...)         → kubectl get + parse
    _apply_merged_configmap(...)          → kubectl apply via stdin
    _reload_alertmanager(namespace)       → curl POST /-/reload (False on failure, #2219)
    apply_to_configmap(...)              → orchestrate read → merge →
                                           alertmanager_check → apply → reload

  Does Alertmanager accept this text (#2219, #3689, #3793) — ONE gate:
    alertmanager_check(am_yml, what=, refusing=) → Alertmanager's loader
                                           (da-read); an AlertmanagerCheck
                                           carrying the loader's reading

  Reading Alertmanager configs (ADR-036 §12, #3206) — never PyYAML:
    emitted_alertmanager_yml(cm_yaml)     → the `alertmanager.yml` string of an
                                           assembled ConfigMap, as kubectl reads
                                           it (da-crdecode)
    alertmanager_reading(text)            → the config as Alertmanager loads it
                                           (da-read alertmanager)

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
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, NamedTuple

import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout

from _grar_routes import (  # noqa: E402
    _build_custom_alert_routes, _build_watchdog_route, _build_synthetic_probe_route,
    _build_sentinel_sinkhole_route)
from _lib_exitcodes import EXIT_CALLER_ERROR, EXIT_VIOLATION  # noqa: E402
from _lib_io import safe_label  # noqa: E402  (#3276)
from _lib_confd import WARN_LIMIT  # noqa: E402  (#1211: the alert names a line lists)
from _grar_merge import Finding  # noqa: E402  (#1211: the undelivered-custom-alert finding)
import _lib_input  # noqa: E402  (#3797: --base-config is a declared document)
import _lib_consumer_read  # noqa: E402  (ADR-036 §12: Alertmanager / Kubernetes readings)
from _lib_consumer_read import ConsumerReadError, InputRefused  # noqa: E402, F401  (re-exported)
from _grar_validate import (  # noqa: E402
    assert_watchdog_inhibit_immunity,
    assert_platform_alerts_not_tenant_silenceable,
    assert_equal_labels_gated,
    assert_tenant_inhibits_verifiable,
    blocking_generation_errors,
    find_tenant_silenceable_platform_inhibits,
    find_ungated_equal_label_inhibits,
    find_watchdog_suppressing_inhibits,
    is_generated_dedup_rule,
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
    entry defines counts as no integration too (Alertmanager's loader rejects
    that anyway).
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


# ── Page-mode custom alerts that reach no one (#1211) ──────────────

#: The custom isolation route's fallback receiver, as its builder names it.
CUSTOM_FIREHOSE_RECEIVER = _build_custom_alert_routes([])[1][0]["name"]


class UndeliveredCustomAlerts(NamedTuple):
    """A tenant whose served page-mode custom alerts get no delivery child
    under the ``component="custom"`` route (#1211)."""
    tenant: str
    alerts: tuple[str, ...]   # the served page-mode alerts' names, declaration order
    routing_disabled: bool    # the tenant's own `_routing: disable` (explicit opt-out)


def custom_firehose_has_integration(am: object) -> bool:
    """Whether *am*'s ``custom-alerts-firehose`` receiver delivers anywhere
    — by :func:`receiver_integration_kinds`, the root-receiver WARN's rule;
    a config that defines no such receiver has none (#1211)."""
    receivers = am.get("receivers") if isinstance(am, dict) else None
    entry = next((r for r in (receivers if isinstance(receivers, list) else [])
                  if isinstance(r, dict) and r.get("name") == CUSTOM_FIREHOSE_RECEIVER),
                 None)
    return bool(receiver_integration_kinds(entry))


def undelivered_custom_alert_tenants(page_alerts: dict[str, tuple[str, ...]],
                                     routes: list[dict],
                                     routing_disabled: "set[str] | frozenset[str]",
                                     ) -> list[UndeliveredCustomAlerts]:
    """The tenants of *page_alerts* (tenant -> its served page-mode custom
    alerts) that get no child under the custom route, sorted (#1211).

    A tenant gets a child exactly when it owns a main tenant route in
    *routes* — the predicate ``_inject_custom_alert_isolation`` builds the
    children from (``_main_tenant_routes``), so the two cannot disagree.
    """
    mains = _main_tenant_routes(routes)
    return [UndeliveredCustomAlerts(t, tuple(names), t in routing_disabled)
            for t, names in sorted(page_alerts.items())
            if names and t not in mains]


def custom_firehose_findings(undelivered: list[UndeliveredCustomAlerts],
                             firehose_has_integration: bool | None, *,
                             strict: bool) -> list[Finding]:
    """One finding per tenant of *undelivered* (#1211).

    *firehose_has_integration*: None when this run has no assembled config
    to read the receiver from (render / ``--validate``); then every line is a
    WARN that says so. True: the alerts are delivered there — no finding.
    False: the alerts notify no one; an implicit absence of ``_routing`` is
    ``blocks: strict`` (ERROR under ``--strict``), an explicit
    ``_routing: disable`` is a deliberate opt-out and stays a WARN.
    """
    if firehose_has_integration:
        return []
    out = []
    for u in undelivered:
        names = ", ".join(safe_label(n) for n in u.alerts[:WARN_LIMIT])
        if len(u.alerts) > WARN_LIMIT:
            names += f", … {len(u.alerts) - WARN_LIMIT} more"
        why = ("its _routing is 'disable'" if u.routing_disabled
               else "it has no effective _routing")
        head = (f"tenant {safe_label(u.tenant)!r}: {len(u.alerts)} served page-mode "
                f"custom alert(s) ({names}) get no delivery route under the "
                f'component="custom" route ({why}), so they fall to the '
                f"{CUSTOM_FIREHOSE_RECEIVER!r} receiver")
        remedy = "add a _routing for the tenant, or set `mode: silent` on the alert"
        if firehose_has_integration is None:
            out.append(Finding(
                f"WARN: {head}, which this mode cannot check (no Alertmanager "
                "config is assembled here; --output-configmap with your "
                "--base-config, or --apply, checks it). If that receiver has no "
                f"integration they notify no one: {remedy}.",
                kind="custom_alert_undelivered", blocks="never", tenant=u.tenant,
                field="_routing"))
            continue
        blocking = not u.routing_disabled
        level = "ERROR" if (strict and blocking) else "WARN"
        tail = (" An explicit `_routing: disable` is a deliberate opt-out: "
                "--strict does not escalate it." if u.routing_disabled else "")
        out.append(Finding(
            f"{level}: {head}, which has no integration (no non-empty "
            f"*_configs) — they notify no one: {remedy}.{tail}",
            kind="custom_alert_undelivered",
            blocks="strict" if blocking else "never", tenant=u.tenant,
            field="_routing"))
    return out


# ── watchdog-heartbeat with no integration (#1211 gap 2) ───────────

#: The Watchdog route's receiver, as its builder names it.
WATCHDOG_RECEIVER = _build_watchdog_route()[1][0]["name"]
WATCHDOG_DOC = "docs/integration/alerting-plane-self-liveness.en.md"


def watchdog_receiver_findings(am: object, *, strict: bool,
                               real_base: bool) -> list[Finding]:
    """The finding for an assembled config whose ``watchdog-heartbeat``
    receiver delivers nowhere (no non-empty ``*_configs``,
    ``receiver_integration_kinds``), or none (ADR-025 D1: the always-firing
    Watchdog must reach an external dead-man's switch, or a dead alerting
    pipeline goes unnoticed).

    *real_base*: the config was assembled on the operator's own base
    (``--base-config``, or the cluster's on ``--apply``). Only then is the
    finding ``blocks: strict`` (ERROR, rc 1 under ``--strict``); the built-in
    base leaves the receiver unwired by design, and refusing it would break
    every ``--strict`` run that assembles on it — so it stays a WARN, like
    ``custom_firehose_findings``' explicit opt-out.
    """
    receivers = am.get("receivers") if isinstance(am, dict) else None
    entry = next((r for r in (receivers if isinstance(receivers, list) else [])
                  if isinstance(r, dict) and r.get("name") == WATCHDOG_RECEIVER), None)
    if receiver_integration_kinds(entry):
        return []
    level = "ERROR" if (strict and real_base) else "WARN"
    where = ("the base config's" if real_base
             else "the built-in base's (no --base-config: it is unwired by "
                  "design, so --strict does not escalate this)")
    return [Finding(
        f"{level}: {where} {WATCHDOG_RECEIVER!r} receiver has no integration "
        "(no non-empty *_configs) — the always-firing Watchdog heartbeat "
        "reaches no external dead-man's switch, so a dead alerting pipeline "
        f"goes unnoticed. Wire it to one: see {WATCHDOG_DOC}.",
        kind="watchdog_undelivered",
        blocks="strict" if real_base else "never")]


# ── §11.3 AM GitOps: --output-configmap / --output-alertmanager ────────────────────────────

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


#: The ConfigMap key Alertmanager's config is mounted from.
AM_CONFIGMAP_KEY = "alertmanager.yml"
#: The key ``--apply`` keeps the SOURCE config under, beside the output it
#: renders from it (#1211 gap 2) — Prometheus Operator's input / generated
#: Secret pair, in one ConfigMap. Alertmanager ignores the extra file.
AM_BASE_CONFIGMAP_KEY = "alertmanager.base.yml"


def emitted_alertmanager_yml(cm_yaml: str) -> str:
    """The ``data["alertmanager.yml"]`` string of the ConfigMap *cm_yaml*, as
    kubectl reads it (``da-crdecode``, ADR-036 §12) — what the cluster will
    mount, not what this process meant to write.

    Raises :class:`ConsumerReadError` when Kubernetes' reading cannot be had
    (no da-crdecode, or it refuses the text) or is not one ConfigMap holding
    that key as a string: either way there is no text to hand Alertmanager."""
    docs = _lib_consumer_read.kubernetes_text_documents(cm_yaml)
    if len(docs) != 1:   # each doc is an object: the reading's guarantee (#3426)
        raise ConsumerReadError(
            f"the assembled ConfigMap is {len(docs)} document(s) in Kubernetes' reading, "
            "expected one object")
    data = docs[0].get("data")
    inner = data.get(AM_CONFIGMAP_KEY) if isinstance(data, dict) else None
    if not isinstance(inner, str):
        raise ConsumerReadError(
            f"the assembled ConfigMap has no string `data.{AM_CONFIGMAP_KEY}` in "
            "Kubernetes' reading")
    return inner


def alertmanager_reading(text: str, *, show_loader_reason: bool = False) -> dict:
    """*text* as Alertmanager loads it (``da-read alertmanager``, ADR-036
    §12): only the keys the text writes, each value Alertmanager's reading of
    it. Raises :class:`ConsumerReadError` — ``stderr_lines`` empty when there
    was no da-read to ask, else da-read's refusal: its fixed sentence, and
    the loader's own reason only with *show_loader_reason* (#3703)."""
    return _lib_consumer_read.alertmanager_config(text, show_loader_reason=show_loader_reason)


#: The generator's opt-in to Alertmanager's own reason for a refusal
#: (``--show-loader-reason``, passed on to da-read). Off by default: the
#: reason is Alertmanager's prose and quotes conf.d values (#3703).
SHOW_LOADER_REASON_FLAG = _lib_consumer_read.SHOW_LOADER_REASON_FLAG
#: What a refusal says when the reason was not asked for (da-read's own
#: sentence before it says why the reason is left out).
LOADER_REASON_HINT = f"generate-routes {SHOW_LOADER_REASON_FLAG} prints the loader's reason"


def _reason_hint(show_loader_reason: bool) -> str:
    """The tail of a refusal by Alertmanager's loader: the flag that prints
    its reason, unless it was given."""
    return "" if show_loader_reason else f" — {LOADER_REASON_HINT}"


def consumer_read_message(exc: ConsumerReadError) -> str:
    """One line for *exc*: its message and the reader's own reason."""
    return exc.detail()


#: --base-config as a declared input reads it (#3797): the file's text,
#: strict UTF-8, read once by `_lib_input.document`; `load_base_config`
#: hands it to Alertmanager's own loader.
BASE_CONFIG_DOCUMENT = _lib_input.Document("text", str)

#: The line after every refusal of the --base-config FILE itself (#3797).
BASE_CONFIG_REMEDY = (
    "--base-config takes a PATH to a base Alertmanager YAML (global / route / "
    "receivers / inhibit_rules). Do not drop the flag to clear this error: that "
    "substitutes the built-in placeholder `global:` for yours, which is the exact "
    "silent failure this error exists to stop.")


def load_base_config_file(path: str, *, show_loader_reason: bool = False) -> dict:
    """``load_base_config`` of the file at *path*, read through the same
    declaration (``BASE_CONFIG_DOCUMENT``) — a library caller's; a tool's
    ``--base-config`` is the declared document it reads. A file that cannot
    be judged or read raises BaseConfigInputError."""
    import argparse  # noqa: PLC0415
    from _lib_exitcodes import CallerError  # noqa: PLC0415
    parser = argparse.ArgumentParser(add_help=False)
    _lib_input.add_input(parser, "--base-config", decode=BASE_CONFIG_DOCUMENT,
                         remedy=BASE_CONFIG_REMEDY)
    try:  # `--base-config=<path>`: a path that starts with "-" is the value
        text = _lib_input.document(parser.parse_args([f"--base-config={path}"]),
                                   "base_config", lang="en")
    except CallerError as exc:
        raise BaseConfigInputError(str(exc)) from exc
    return load_base_config(text, path, show_loader_reason=show_loader_reason)


def load_base_config(raw_text: str | None, path: str | None = None, *,
                     show_loader_reason: bool = False) -> dict:
    """The base Alertmanager config that generated routes merge into, from
    *raw_text* — what `_lib_input.document` read from the ``--base-config``
    file at *path* (``BASE_CONFIG_DOCUMENT``), None when the flag was not
    given.

    Two outcomes, and they must stay distinguishable (#1616):

    * the flag was OMITTED — ``raw_text is None`` — the built-in defaults apply;
    * the flag was SUPPLIED with a value that cannot serve as a base config —
      the declaration refuses a path that is no readable UTF-8 file (rc 2
      naming the flag), and this raises for content Alertmanager's loader
      refuses; the caller exits 2 (dev-rules #13 / ``_lib_exitcodes``).

    ⛔ "Supplied" is ``is None``, NOT falsiness: an empty string is supplied
    — what an unset shell variable expands to — and the declaration refuses
    it; an empty FILE is content, refused by the loader below.

    ⚠️ NOT closed: the CONTENT axis (``--policy`` closed its own in #1649;
    this one stays open). A file that IS a mapping but whose ``global:`` is empty or
    wrong still merges silently — except a credential a conf.d receiver would
    inherit (#3257), which ``assert_base_global_inherits_no_credentials``
    refuses as a config violation (rc 1), not here. A file without ``global:``
    or ``inhibit_rules:`` is legitimate (they fall back per key); one without
    ``route:`` is not — Alertmanager refuses it (#3206).
    """
    if raw_text is None:
        return copy.deepcopy(_DEFAULT_BASE_CONFIG)
    raw = raw_text.encode("utf-8")
    # ADR-036 §12 (#3206): the base is an Alertmanager config, so it is read
    # the way Alertmanager loads it (da-read alertmanager), not with PyYAML.
    # ⛔ That makes the base a WHOLE config Alertmanager accepts on its own:
    # `route` (and the receivers it names) must be there — Alertmanager's
    # loader refuses a config without them, and so does this. `global` and
    # `inhibit_rules` may still be left out and fall back below. An empty
    # file, a top-level list or scalar, an unknown key: the loader's refusal
    # (its words only with --show-loader-reason, #3703).
    try:
        data = alertmanager_reading(raw, show_loader_reason=show_loader_reason)
    except ConsumerReadError as exc:
        if not isinstance(exc, InputRefused):
            raise BaseConfigInputError(
                f"--base-config: cannot read {path!r} the way Alertmanager does: {exc}") from exc
        raise BaseConfigInputError(
            f"--base-config: Alertmanager's config loader refuses {path!r}: "
            f"{consumer_read_message(exc)}{_reason_hint(show_loader_reason)}\n"
            "  --base-config takes a complete Alertmanager config (at least `route:` and "
            "the `receivers:` it names); `global:` / `inhibit_rules:` may be left out.") from exc
    # #1211 gap 2: an output fed back as the source (BaseIsAnOutputError, rc 1).
    assert_text_is_not_an_output(raw_text, f"--base-config {path!r}")
    # Ensure required keys exist. deepcopy so a caller mutating the result
    # cannot reach into _DEFAULT_BASE_CONFIG's nested lists and change what
    # every later call in this process returns.
    for key in ("global", "route", "receivers", "inhibit_rules"):
        if key not in data:
            data[key] = copy.deepcopy(_DEFAULT_BASE_CONFIG[key])
    return data


# #3257: what a base config's `global` hands to a receiver that leaves the
# field out (Alertmanager config.go, v0.33.1, the receiver loop of
# Config.UnmarshalYAML). An email receiver without auth takes every
# `smtp_auth_*` (username, password / password_file, secret / secret_file,
# identity) and sends it to ITS smarthost — which a tenant writes. A webhook,
# slack, msteams or pagerduty receiver without `http_config` takes
# `global.http_config` whole, credentials included, and sends them to its URL
# — again the tenant's (conf.d slack / teams / pagerduty receivers cannot set
# http_config at all, so they always take it). Either one would hand a
# platform credential to every tenant that names an endpoint, so the base
# config may carry none of them.
# `http_headers` counts only when some header carries `secrets` or `files`
# (a header holding only `values` is plain text the platform chose to send).
# NOT refused, on purpose: a TLS client key (`tls_config.key` / `key_file`,
# under global.http_config.tls_config or smtp_tls_config) and its cert. The
# private key never leaves the process: it only proves the certificate's
# identity in the mTLS handshake, so an inheriting receiver presents the
# platform's certificate to the tenant's endpoint but sends no secret to it.
BASE_GLOBAL_INHERITED_SECRET_PREFIX = "smtp_auth_"
BASE_GLOBAL_HTTP_CONFIG_CREDENTIAL_KEYS = (
    "basic_auth", "authorization", "bearer_token", "bearer_token_file",
    "oauth2")
BASE_GLOBAL_HEADER_SECRET_LISTS = ("secrets", "files")


def base_global_inherited_credentials(base: object) -> list[str]:
    """#3257: the ``global`` keys of *base* that would hand a platform
    credential to a conf.d receiver (see above): each ``smtp_auth_*`` key and
    each credential key of ``global.http_config`` — set when not null and
    not ``""`` / an empty mapping — and each ``global.http_config.http_headers``
    header whose ``secrets`` or ``files`` list is non-empty
    (``global.http_config.http_headers.<name>``). Paths sorted."""
    glob = base.get("global") if isinstance(base, dict) else None
    if not isinstance(glob, dict):
        return []

    def is_set(value: object) -> bool:
        return value not in (None, "", {}, [])

    out = [f"global.{k}" for k, v in glob.items()
           if isinstance(k, str) and k.startswith(BASE_GLOBAL_INHERITED_SECRET_PREFIX)
           and is_set(v)]
    http_config = glob.get("http_config")
    if isinstance(http_config, dict):
        out += [f"global.http_config.{k}"
                for k in BASE_GLOBAL_HTTP_CONFIG_CREDENTIAL_KEYS
                if is_set(http_config.get(k))]
        headers = http_config.get("http_headers")
        if isinstance(headers, dict):
            out += [f"global.http_config.http_headers.{name}"
                    for name, header in headers.items()
                    if isinstance(header, dict)
                    and any(is_set(header.get(lst)) for lst in BASE_GLOBAL_HEADER_SECRET_LISTS)]
    return sorted(out)


#: Said when a refused key is `…authorization`: the base is read as
#: Alertmanager loads it (da-read, ADR-036 §12), and its loader moves a
#: written `bearer_token` / `bearer_token_file` there, so the key named is
#: not the one the operator wrote. A fixed sentence — the written key is not
#: rebuilt here.
BEARER_TOKEN_IS_AUTHORIZATION_NOTE = (
    "(Alertmanager reads `bearer_token` / `bearer_token_file` as "
    "`authorization`, so a base that writes either is named by that key.)")


def assert_base_global_inherits_no_credentials(base: object) -> None:
    """Fail-closed guard (#3257): raise ValueError when the base config's
    ``global`` carries a credential a conf.d receiver would inherit
    (``base_global_inherited_credentials``). A config violation (rc 1), like
    the merged-set invariants ``merge_generated`` asserts."""
    keys = base_global_inherited_credentials(base)
    if not keys:
        return
    raise ValueError(
        "ERROR: the base config's global section sets "
        f"{', '.join(keys)}. Alertmanager gives these to every receiver that "
        "leaves the field out — an email receiver without auth takes the "
        "smtp_auth_* values, a receiver without http_config takes "
        "global.http_config — and a conf.d receiver names its own endpoint, "
        "so the credential would be sent there. Set the credential on the "
        "platform's own receivers in the base config instead of in global."
        + (f" {BEARER_TOKEN_IS_AUTHORIZATION_NOTE}"
           if any(k.endswith(".authorization") for k in keys) else ""))


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


# ── The one merge every output mode uses (#1211 gap 2) ─────────────
#
# `--output-configmap`, `--output-alertmanager` and `--apply` all render
# OUTPUT = merge(SOURCE base, conf.d): the base is the operator's own
# config — the `--base-config` file, the built-in base, or on `--apply` the
# `alertmanager.base.yml` key of the cluster's ConfigMap — and is never an
# earlier run's output (owner decision S on #1211 gap 2). Prometheus Operator
# works the same way: it reads the user's config from one Secret and writes
# the merged result to another (`<name>-generated`), never back. So nothing
# here has to tell the generator's part of a base from the operator's: a
# base that holds generated receivers is refused instead.
# Before #1211 gap 2 the two ConfigMap paths replaced the whole
# `route.routes` (a BYO customer's own routes vanished), and the only path
# that kept them — the routing fragment — lacked the four platform routes
# that must come first.

#: Receiver-name prefixes only the generator writes: ``tenant-<t>`` (+ its
#: ``-route-<i>`` / ``-override-<i>``) and ``platform-enforced-<t>``.
GENERATED_RECEIVER_PREFIXES = ("tenant-", "platform-enforced")
#: The single (matcher-less) enforced NOC route's receiver.
SINGLE_ENFORCED_RECEIVER = "platform-enforced"


def is_generated_receiver_name(name: object) -> bool:
    """Whether *name* is in the namespace only generated receivers use
    (``tenant-*``, ``platform-enforced*``). The four platform receivers are
    not: a base defines them on purpose (watchdog-heartbeat's ``url_file``)."""
    return isinstance(name, str) and name.startswith(GENERATED_RECEIVER_PREFIXES)


def _is_enforced_route(route: dict) -> bool:
    r = route.get("receiver")
    return isinstance(r, str) and (r == SINGLE_ENFORCED_RECEIVER
                                   or r.startswith("platform-enforced-"))


def _routes_to(route: object, names: frozenset[str]) -> bool:
    return isinstance(route, dict) and route.get("receiver") in names


#: The first line of every alertmanager.yml an output mode writes (#1211 gap 2,
#: decision S). A comment, so Alertmanager ignores it; it is how an output fed
#: back as the source is recognised when its receivers alone cannot tell —
#: a conf.d without `_routing` / `_routing_enforced` generates no `tenant-*` /
#: `platform-enforced*` receiver, only the severity dedup inhibit rule.
#: ⚠️ Narrowed, not closed: an output with this line deleted is still refused
#: when it holds a generated receiver (``assert_base_is_a_source``) or the
#: generated dedup rule (``assert_base_has_no_dedup_rule``, #3899); one with
#: neither — every tenant at `_severity_dedup: disable`, no routing — still
#: passes as a source, and then merges in nothing a second time.
GENERATED_OUTPUT_MARKER = "# GENERATED by da-tools generate-routes"
GENERATED_OUTPUT_HEADER = (
    f"{GENERATED_OUTPUT_MARKER} from a source config — an output, not a source:\n"
    "# never pass it as --base-config; render again from the source instead.\n")


class BaseIsAnOutputError(ValueError):
    """The text offered as the source carries ``GENERATED_OUTPUT_MARKER``: it
    is an output of this tool. A config verdict (rc 1), like
    ``assert_base_is_a_source``, not an invocation error."""


_PASS_THE_SOURCE = ("Pass the source the output was rendered from (keep it next "
                    "to the output, never overwrite it with it).")


def assert_text_is_not_an_output(text: str, where: str,
                                 remedy: str = _PASS_THE_SOURCE) -> None:
    """Raise ``BaseIsAnOutputError`` when *text* (the source, as read) has a
    line starting with ``GENERATED_OUTPUT_MARKER`` — a UTF-8 BOM in front of
    it included (``str.strip`` keeps U+FEFF)."""
    if any(ln.strip().lstrip("\ufeff").strip().startswith(GENERATED_OUTPUT_MARKER)
           for ln in text.splitlines()):
        raise BaseIsAnOutputError(
            f"ERROR: {where} is an output of da-tools generate-routes (its "
            f"'{GENERATED_OUTPUT_MARKER}' line), not a source: rendered from it, "
            "the generated routing and inhibit rules would be merged in a second "
            f"time. {remedy}")


def assert_base_is_a_source(base: object) -> None:
    """Fail-closed guard (#2279, #1211 gap 2): the base must be the
    operator's own config, not an earlier run's output — raise ValueError
    (rc 1) naming the receivers when it defines any in the generated
    namespace. Fed back, a previous output would merge its tenant routing in
    a second time (and an operator's own ``tenant-*`` receiver would shadow
    the generated one of that name, #2279)."""
    receivers = base.get("receivers") if isinstance(base, dict) else None
    names = sorted({r.get("name") for r in (receivers if isinstance(receivers, list) else [])
                    if isinstance(r, dict) and is_generated_receiver_name(r.get("name"))})
    if not names:
        return
    raise ValueError(
        "ERROR: the base config defines receiver(s) in the namespace only the "
        f"generator writes (tenant-*, platform-enforced*): {', '.join(map(repr, names))}. "
        "The base is your own Alertmanager config — the source the generated "
        "routing is merged into — not an earlier output of this tool: pass that "
        "source (keep it next to the output, never overwrite it with it), or "
        "rename your own receivers out of that namespace.")


_REMOVE_FROM_THE_BASE = ("Remove them from the base config — or, if it is an "
                         "earlier output of this tool, pass the source it was "
                         "rendered from instead.")


def assert_base_has_no_dedup_rule(base: object, where: str = "the base config",
                                  remedy: str = _REMOVE_FROM_THE_BASE) -> None:
    """Fail-closed guard (#3899): the base must not hold a severity dedup
    rule the route generator itself writes — raise ValueError (rc 1) naming
    each one.

    The generator renders that rule from conf.d (ONE rule, the ``disable``
    tenants in its ``tenant!~`` list), and ``merge_generated`` keeps every
    base inhibit rule as written, so a generated rule kept in the base would
    override conf.d: a tenant set to ``disable`` would still be deduped.
    Such a base is almost always an older output fed back as the source —
    v2.9.0's ``--apply`` wrote outputs with no ``GENERATED_OUTPUT_MARKER``
    line — or a hand copy of the generator's rule; this is also what still
    recognises an output with the marker deleted and no generated receiver.

    Only the generator's own shapes count
    (``_grar_validate.is_generated_dedup_rule``: today's and the per-tenant
    one before #3899). An inhibit rule the operator wrote is operator policy:
    it is not judged and is kept as written, even where it suppresses what
    ``_severity_dedup`` (``disable`` included) would let through.
    *where* / *remedy* name the source and what to do (``--apply`` names
    the ConfigMap key)."""
    rules = base.get("inhibit_rules") if isinstance(base, dict) else None
    found = [i for i, r in enumerate(rules if isinstance(rules, list) else [])
             if is_generated_dedup_rule(r)]
    if not found:
        return
    raise ValueError(
        f"ERROR: {where} holds the route generator's own severity dedup "
        f"rule(s) ({', '.join(f'inhibit_rules[{i}]' for i in found)}). That rule "
        "is generated from conf.d (`_severity_dedup`) by this tool alone; kept "
        "in the source it would override conf.d (`disable` included). This is "
        "usually an older output passed back as the source (v2.9.0's --apply "
        "wrote outputs without the GENERATED marker) or a hand copy of the "
        f"generator's rule. {remedy}")


def merge_generated(base: dict, routes: list[dict], receivers: list[dict],
                    inhibit_rules: list[dict] | None, *,
                    strict: bool = False) -> dict:
    """*base* (a SOURCE config, ``assert_base_is_a_source``) with this run's
    routing merged in (#1211 gap 2). *base* is not modified.

    Routes — ``route.routes`` becomes, in this order:

    1. the four platform routes (Watchdog, custom isolation, synthetic
       probe, sentinel sink; ``_inject_custom_alert_isolation``) and the
       enforced NOC route(s) (``continue: true``) — ahead of everything, so
       no route of the base can take their alerts first;
    2. the base's own top-level routes, in the base's order — except one to a
       platform receiver: those four routes are the platform's, emitted
       canonically in 1;
    3. the tenant routes.

    With per-tenant enforced routes (``platform-enforced-<t>``, #2519), 2 and
    3 go under ONE matcher-less route to the base's root receiver, right
    after the enforced routes. Alertmanager delivers to a route's receiver
    only when none of its children matched, and an enforced route that
    matched (``continue: true``) counts — so without it a tenant with no
    tenant route would lose its root delivery. Under it, that route plays
    the root: it delivers when nothing below matched, and NOT when the
    base's own ``continue: true`` route already took the alert (one root
    delivery fewer than a per-tenant fallthrough route would give). The
    single ``platform-enforced`` route (no ``{{tenant}}``) gets none: an
    alert it matches has never gone on to the root.

    Receivers — the base's, then this run's; a name-only platform
    placeholder defers to a richer base definition (watchdog-heartbeat's
    ``url_file``). Inhibit rules — the base's, then this run's.

    Raises ``ValueError`` for a base that is not a source, a base holding a
    severity dedup rule (#3899), a receiver name
    clash, or a merged config that breaks a platform invariant;
    ``PlatformProbeSetUnverifiable`` (#1533) when it cannot be checked.
    """
    merged = copy.deepcopy(base) if isinstance(base, dict) else {}

    assert_base_is_a_source(merged)
    # #3899: the severity dedup rule is the generator's alone.
    assert_base_has_no_dedup_rule(merged)
    # #3257: a credential in the base's global would reach every conf.d
    # receiver that leaves the field out.
    assert_base_global_inherits_no_credentials(merged)

    routes, receivers = _inject_custom_alert_isolation(routes, receivers)
    placeholders = _platform_placeholder_receiver_names()

    root = dict(merged.get("route") or {})
    head = routes[:4] + [r for r in routes[4:] if _is_enforced_route(r)]
    tail = [r for r in routes[4:] if not _is_enforced_route(r)]
    own = [r for r in (root.get("routes") or []) if not _routes_to(r, placeholders)]
    if any(str(r.get("receiver", "")).startswith("platform-enforced-") for r in head):
        # #2519: the root's place, below the enforced routes (see docstring).
        below = {"receiver": root.get("receiver")}
        if own + tail:
            below["routes"] = own + tail
        root["routes"] = head + [below]
    else:
        root["routes"] = head + own + tail
    merged["route"] = root

    base_receivers = [r for r in (merged.get("receivers") or []) if isinstance(r, dict)]
    base_names = {r.get("name") for r in base_receivers}
    # #2279: a base receiver named like a generated one (outside the four
    # platform receivers) would be emitted twice, or one definition lost.
    shadowed = sorted(r["name"] for r in receivers
                      if r["name"] in base_names and r["name"] not in placeholders)
    if shadowed:
        raise ValueError(
            "ERROR: the base config defines receiver(s) with the same name as "
            f"receiver(s) generated from conf.d: {', '.join(map(repr, shadowed))}. "
            "The base definition would clash with the conf.d one. Rename them "
            "in the base config.")
    merged["receivers"] = base_receivers + [r for r in receivers if r["name"] not in base_names]

    merged["inhibit_rules"] = (list(merged.get("inhibit_rules") or [])
                               + list(inhibit_rules or []))

    # ADR-025 D1: fail-closed if any inhibit rule (base or generated) would
    # suppress the always-firing Watchdog heartbeat — it must always egress.
    assert_watchdog_inhibit_immunity(merged["inhibit_rules"])
    # A tenant-triggered inhibit (Silent Mode) must never suppress a PLATFORM
    # self-monitoring alert — three of them carry a tenant label at fire time.
    assert_platform_alerts_not_tenant_silenceable(merged["inhibit_rules"])
    # #1132: every equal-label must be presence-gated on some side (strict →
    # fail, else → warn), the base's hand-written rules included.
    _enforce_equal_labels_gated(merged["inhibit_rules"], strict)
    # #1533: with a degraded probe set the silencing check above cannot vouch
    # for a tenant-triggered BASE rule — refuse (rc 2). Last, so any verdict
    # that does not depend on the probe set (rc 1) is reported first. This
    # run's generated rules are exempt.
    assert_tenant_inhibits_verifiable(merged["inhibit_rules"],
                                      exempt=list(inhibit_rules or []))
    return merged


def dump_alertmanager_yml(am: dict) -> str:
    """*am* as alertmanager.yml text (the source, under
    ``alertmanager.base.yml``, is written with this)."""
    return yaml.dump(am, default_flow_style=False, allow_unicode=True, sort_keys=False)


def dump_output_yml(merged: dict) -> str:
    """*merged* as the alertmanager.yml every output mode writes:
    ``GENERATED_OUTPUT_HEADER`` first."""
    return GENERATED_OUTPUT_HEADER + dump_alertmanager_yml(merged)


def assemble_alertmanager_yml(base: dict, routes: list[dict], receivers: list[dict],
                              inhibit_rules: list[dict], strict: bool = False) -> str:
    """``merge_generated`` rendered as alertmanager.yml (``--output-alertmanager``)."""
    return dump_output_yml(
        merge_generated(base, routes, receivers, inhibit_rules, strict=strict))


def assemble_configmap(base: dict, routes: list[dict], receivers: list[dict], inhibit_rules: list[dict],
                       namespace: str = "monitoring", configmap_name: str = "alertmanager-config",
                       strict: bool = False) -> str:
    """``assemble_alertmanager_yml`` wrapped as a Kubernetes ConfigMap
    (``--output-configmap``): ``data.alertmanager.yml`` is that text, and
    ``data.alertmanager.base.yml`` the source it was rendered from — the same
    pair ``--apply`` keeps, so a ConfigMap deployed through GitOps can be
    ``--apply``-ed later without losing its source (#1211 gap 2)."""
    configmap = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": configmap_name,
            "namespace": namespace,
        },
        "data": {
            AM_CONFIGMAP_KEY: assemble_alertmanager_yml(
                base, routes, receivers, inhibit_rules, strict=strict),
            AM_BASE_CONFIGMAP_KEY: dump_alertmanager_yml(base),
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


# ── #2219 / #3689 / #3793: does Alertmanager accept this text — ONE gate ─
# The JSON schema and this generator's Python checks accept values Alertmanager
# refuses at load time (measured: a webhook URL `http://[1]/`, a host carrying
# U+0085 that the YAML emitter folds into a line break, a string where an
# `http_config` bool belongs, an `oauth2` block without `client_id`). Only
# Alertmanager is the authority on that, on the SAME text that is about to be
# written or applied, never on a re-render.
#
# ⛔ Every mode (`--validate`, `--output-*`, `--apply`, validate-config's
# `routes` row) asks through `alertmanager_check`, so the verdict cannot
# drift between modes. The one reader is Alertmanager's own loader:
# `da-read alertmanager`, `config.Load` of the Alertmanager version the
# da-tools image and the cluster pin (tests/ops/test_da_tools_amtool_pin_parity.py
# holds da-read's go.mod to that version). Its reading is also what the callers
# judge (root receiver, firehose, watchdog heartbeat).
#
# ⛔ Nothing else is asked. The files a `templates:` glob names are read by
# the Alertmanager container from ITS filesystem; no tool here can read them
# from there, so parsing them on this machine would judge other files (or, for
# a relative glob, none at all).
#
# A refusal is da-read's fixed sentence, not the loader's reason: that is
# Alertmanager's prose and quotes conf.d values (#3703). Only a person who
# asks (`--show-loader-reason`, never the generator service) gets it.


# `AlertmanagerCheck.status` values. ⛔ Three: a reader that could not give a
# verdict (no da-read, it crashed) is neither "rejected" (a finding about the
# config) nor accepted — it is an environment failure and every caller
# reports it as one.
AM_ACCEPTED = "accepted"
AM_REJECTED = "rejected"
AM_UNUSABLE = "unusable"


@dataclass(frozen=True)
class AlertmanagerCheck:
    """Whether Alertmanager accepts a text, as data (#2311, #3689).

    *exit_code* is ``None`` to go on, else the code to stop with; *message* is
    the exact stderr text a caller prints for it; *reading* is the text as
    Alertmanager loads it (:func:`alertmanager_reading`) whenever the loader
    accepted it, else ``None`` — callers judge that reading instead of asking
    da-read a second time.
    """
    status: str
    exit_code: int | None
    message: str
    reading: dict | None = None


def alertmanager_check(am_yml: str, *, what: str, refusing: str,
                       show_loader_reason: bool = False) -> AlertmanagerCheck:
    """Does Alertmanager accept *am_yml*? The ONE gate every mode asks
    (``--validate``, ``--output-*``, ``--apply``; #3689) — returned, never
    printed (#2311: validate-config reports it in a row).

    Alertmanager's loader (da-read) reads the text:

    * it accepts → ``accepted``, exit None, carrying its reading;
    * it refuses → ``rejected``, EXIT_VIOLATION (the CONFIG is wrong);
    * no reading could be had (no da-read, it crashed) → ``unusable``,
      EXIT_CALLER_ERROR: no verdict on the config, so it must not read as one.

    *what* names the text for the operator; *refusing* is what the caller will
    NOT do because of a failure ("write it to …", "apply it …").
    *show_loader_reason*: a refusal carries the loader's own reason; without
    it the message names the flag that does (#3703).
    """
    try:
        reading = alertmanager_reading(am_yml, show_loader_reason=show_loader_reason)
    except ConsumerReadError as exc:
        reason = safe_label(consumer_read_message(exc))
        if isinstance(exc, InputRefused):
            return AlertmanagerCheck(
                AM_REJECTED, EXIT_VIOLATION,
                f"FAIL: Alertmanager's config loader refuses {what} — refusing "
                f"to {refusing}: {reason}{_reason_hint(show_loader_reason)}")
        return AlertmanagerCheck(
            AM_UNUSABLE, EXIT_CALLER_ERROR,
            f"ERROR: cannot read {what} the way Alertmanager does — refusing "
            f"to {refusing}: {reason}")
    return AlertmanagerCheck(
        AM_ACCEPTED, None,
        f"Alertmanager's config loader (da-read) accepted {what}", reading)


# ── #2311: the ONE verdict on a generation result ───────────────────
# `generate_alertmanager_routes --validate` judges a generation result with
# `evaluate_generated_config`, and validate-config's `routes` row is that
# `--validate` (#3788). Before it, validate-config ran only the first of
# these steps, so a tree `--validate` failed (a tripwire, an
# invariant `assemble_configmap` refuses, an Alertmanager rejection) was a
# PASS row at rc 0 — #2164's drift, one layer further down.
#
# #2260: what --validate hands Alertmanager is assembled on the BUILT-IN base
# (`--validate --base-config` stays a caller error, #1616), so a pass says
# nothing about the operator's own base — the result line names that base.
VALIDATE_WHAT = ("the generated config (assembled on the built-in "
                 "default base, never on a --base-config)")


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
    * ``alertmanager`` — ``None`` when the run stopped before Alertmanager
      was asked (errors, or the assembly was refused), else its
      :class:`AlertmanagerCheck`.

    ⛔ The steps short-circuit exactly as ``--validate`` does: Alertmanager is
    never asked about a config the Python checks already refused.
    """
    errors: list[str]
    assembly_error: str | None = None
    warnings: list[str] = field(default_factory=list)
    alertmanager: AlertmanagerCheck | None = None

    @property
    def exit_code(self) -> int | None:
        """``None`` = the run may say OK."""
        if self.errors or self.assembly_error is not None:
            return EXIT_VIOLATION
        return self.alertmanager.exit_code if self.alertmanager is not None else None


def evaluate_generated_config(routes: list[dict], receivers: list[dict],
                              inhibit_rules: list[dict], warnings: list[str],
                              *, extra_errors: "list[str] | tuple" = (),
                              show_loader_reason: bool = False
                              ) -> GeneratedConfigVerdict:
    """Every ``--validate`` verdict on a generation result, returned — never
    printed, never ``sys.exit``-ed (#2311).

    In order, stopping at the first step that blocks:

    1. ``blocking_generation_errors(warnings)`` — entries skipped as unusable,
       duplicate receiver names (#2279);
    2. *extra_errors* — caller-selected blocking lines (``--validate --strict``
       passes its ADR-007 policy errors here, so they sit where they always
       printed and still keep Alertmanager from being asked);
    3. the ADR-025 tripwires on the GENERATED inhibit rules: Watchdog
       suppression, a tenant-triggered rule silencing a platform alert;
    4. ``assemble_configmap`` on the built-in base — a ``ValueError`` is a
       verdict on the config (#2260 / #2279);
    5. :func:`alertmanager_check` on that assembly — the same gate, in the
       same order, as the write paths (#3689); *show_loader_reason* is its.
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
    try:
        am_yml = emitted_alertmanager_yml(cm_yaml)
    except ConsumerReadError as exc:
        # No text for Alertmanager: an environment failure (no da-crdecode),
        # never a verdict on the config — the "unusable" status, rc 2.
        verdict.alertmanager = AlertmanagerCheck(
            AM_UNUSABLE, EXIT_CALLER_ERROR,
            "ERROR: cannot read the assembled ConfigMap the way kubectl does, so "
            "Alertmanager was not asked — refusing to report the config as "
            f"valid: {safe_label(consumer_read_message(exc))}")
        return verdict
    verdict.alertmanager = alertmanager_check(
        am_yml, what=VALIDATE_WHAT,
        refusing="report the config as valid", show_loader_reason=show_loader_reason)
    return verdict


class AlertmanagerConfigRejected(Exception):
    """:func:`alertmanager_check` (or ``judge_merged``) stopped
    ``apply_to_configmap`` before anything reached the cluster. Carries the
    check's exit code: 1 when the CONFIG is wrong, 2 when no verdict could be
    had (no da-read, or it could not run)."""

    def __init__(self, exit_code: int):
        super().__init__(exit_code)
        self.exit_code = exit_code


class AlertmanagerConfigInvariantViolated(ValueError):
    """#2506: merging the generated fragment into the CLUSTER's config broke a
    platform invariant (ADR-025: an inhibit rule that would suppress the
    Watchdog heartbeat or let a tenant silence a platform alert; #1132 under
    ``strict``). Raised by ``apply_to_configmap`` before Alertmanager's
    loader or kubectl apply runs, so nothing reached the cluster. A verdict on the config (rc
    1), like ``assemble_configmap``'s ``ValueError`` on the other write paths
    (#2260) — a ``ValueError`` subclass for that reason, and a class of its
    own so the caller catches the merge step only, not every ``ValueError``
    the apply path could raise."""


def _read_existing_configmap(namespace: str, configmap_name: str, *,
                             show_loader_reason: bool = False
                             ) -> tuple[dict | None, list[str], str | None]:
    """Read the SOURCE config ``--apply`` merges into from the cluster's
    Alertmanager ConfigMap (#1211 gap 2, owner decision S): its
    ``alertmanager.base.yml`` key — written by the previous ``--apply`` — or,
    on the first ``--apply`` against a ConfigMap without one, its
    ``alertmanager.yml`` (adopted as the source; ``merge_generated`` refuses
    it if it already holds generated routing).

    Returns (config_dict, warnings, source_text) — config_dict is None if
    the read failed.
    """
    warnings = []
    result = _run_binary(
        ["kubectl", "get", "configmap", configmap_name, "-n", namespace, "-o", "json"],
        timeout=60)
    if result.returncode != 0:
        warnings.append(f"ERROR: Failed to read ConfigMap {configmap_name}: {result.stderr}")
        return None, warnings, None

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
        return None, warnings, None
    if not isinstance(cm, dict):
        warnings.append(
            f"ERROR: kubectl returned JSON that is not an object "
            f"({type(cm).__name__}); expected a ConfigMap resource")
        return None, warnings, None
    if "data" not in cm:
        warnings.append("ERROR: ConfigMap has no `data` block at all")
        return None, warnings, None
    data = cm["data"]
    if not isinstance(data, dict):
        warnings.append(
            f"ERROR: ConfigMap `data` is {type(data).__name__}, expected a "
            f"mapping of keys to strings")
        return None, warnings, None
    # ⛔ Membership, not truthiness — the same distinction the two guards above
    # draw for `data`. `data.get(key, "")` collapsed "the cluster has no such
    # key" into "the key is there and empty", and the message named the first:
    # an operator who emptied `alertmanager.yml` was sent looking for a missing
    # key that is present. It also swallowed every falsy non-string (`0`,
    # `false`, `[]`) before the type guard below could name its type. An empty
    # string now flows on and is diagnosed by the mapping guard further down,
    # which reads the raw TEXT and says the key holds nothing usable.
    key = AM_BASE_CONFIGMAP_KEY if AM_BASE_CONFIGMAP_KEY in data else AM_CONFIGMAP_KEY
    if key not in data:
        warnings.append("ERROR: ConfigMap has no 'alertmanager.yml' key")
        return None, warnings, None
    existing_yml = data[key]
    if not isinstance(existing_yml, str):
        # ⛔ The reader below takes text; a non-string value reaching it was
        # once an uncaught error at rc=1 with no diagnostic — the exact
        # picture the guards above were added to remove, one layer further in. The
        # Kubernetes API forces `data` values to be strings, so this is not
        # operator-reachable through a real cluster; it is guarded because it
        # is exactly as reachable as the two shapes just above it, and a
        # sentence claiming "every failure exit appends a warning" has to be
        # true for the whole function or it is not worth writing.
        warnings.append(
            f"ERROR: ConfigMap key '{key}' is "
            f"{type(existing_yml).__name__}, expected a string")
        return None, warnings, None

    # ADR-036 §12 (#3206): the cluster's config as Alertmanager loads it
    # (da-read alertmanager). A text its loader refuses — not YAML, empty,
    # comments only, no route — is refused here (in the loader's words only
    # with --show-loader-reason, #3703); the
    # cluster's Alertmanager is not running it either.
    try:
        existing = alertmanager_reading(existing_yml, show_loader_reason=show_loader_reason)
    except ConsumerReadError as exc:
        if not isinstance(exc, InputRefused):
            warnings.append(
                f"ERROR: cannot read ConfigMap key '{key}' the way "
                f"Alertmanager does: {exc}")
            return None, warnings, None
        # ⛔ The parenthetical is a GUESS, so it is gated on evidence: it reads
        # the TEXT — blank, or nothing but comments — which is the thing the
        # sentence claims (`null`, `~`, `---` hold text and are refused too).
        stripped = [ln.strip() for ln in existing_yml.splitlines()]
        looks_empty = all(not ln or ln.startswith("#") for ln in stripped)
        hint = (" — the key exists but holds nothing usable (comments or "
                "whitespace only)") if looks_empty else ""
        warnings.append(
            f"ERROR: Alertmanager's config loader refuses ConfigMap key "
            f"'{key}'{hint}: {consumer_read_message(exc)}{_reason_hint(show_loader_reason)}")
        return None, warnings, None
    return existing, warnings, existing_yml


def _apply_merged_configmap(merged_yml: str, namespace: str, configmap_name: str,
                            source_yml: str | None = None) -> bool:
    """Apply merged ConfigMap to K8s cluster: the output under
    ``alertmanager.yml`` and, when given, the source it was rendered from
    under ``alertmanager.base.yml`` (#1211 gap 2).

    Returns True if successful, False otherwise.
    """
    apply_result = _run_binary(
        ["kubectl", "create", "configmap", configmap_name,
         f"--from-literal={AM_CONFIGMAP_KEY}={merged_yml}",
         *([f"--from-literal={AM_BASE_CONFIGMAP_KEY}={source_yml}"]
           if source_yml is not None else []),
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


def apply_to_configmap(routes: list[dict], receivers: list[dict], inhibit_rules: list[dict], namespace: str, configmap_name: str, strict: bool = False,
                       judge_merged: Callable[[dict], list[str]] | None = None, *,
                       show_loader_reason: bool = False) -> bool:
    """Merge generated routing config into existing Alertmanager ConfigMap and reload.

    Applies tenant routing configuration directly to a running Alertmanager cluster.
    This is the --apply mode for immediate deployment without GitOps workflow.

    Process:
      1. kubectl get configmap → the source: ``alertmanager.base.yml``, or
         on the first --apply ``alertmanager.yml`` (``_read_existing_configmap``)
      2. ``merge_generated`` on that source (#1211: the same merge as
         --output-configmap / --output-alertmanager)
      3. ``alertmanager_check`` on the merged text: Alertmanager's loader
         (da-read; #2219, #3689)
      4. kubectl apply the ConfigMap: the output under alertmanager.yml, the
         source under alertmanager.base.yml
      5. curl POST /-/reload to trigger Alertmanager configuration reload

    Notes:
      - Keeps the source's own routes, receivers and inhibit rules
      - /-/reload needs no Alertmanager flag; it is always enabled (#1243)

    Args:
        routes: generated tenant route dicts
        receivers: generated tenant receiver dicts
        inhibit_rules: generated inhibit_rules for severity dedup
        namespace: K8s namespace where ConfigMap is located
        configmap_name: ConfigMap name (typically alertmanager-config)
        judge_merged: #1211 — called with the merged config as Alertmanager
            reads it, before anything is applied; it returns the reasons to
            refuse it (rc 1), an empty list to go on.
        show_loader_reason: #3703 — a refusal by Alertmanager's loader (of the
            cluster's config or the merged one) carries the loader's reason.

    Returns:
        True if merge, apply and reload all succeeded, False otherwise.

    Raises:
        AlertmanagerConfigRejected: Alertmanager refused the merged config (or
        no verdict could be had); nothing was sent to the cluster. #2219.
        AlertmanagerConfigInvariantViolated: the merged config breaks a
        platform invariant; nothing was sent to the cluster. #2506.
        PlatformProbeSetUnverifiable: the probe set degraded and the cluster
        config holds a tenant-triggered inhibit rule it cannot vouch for;
        nothing was sent to the cluster. #1533.
    """
    # 1. Read existing ConfigMap
    existing, read_warnings, source_yml = _read_existing_configmap(
        namespace, configmap_name, show_loader_reason=show_loader_reason)
    if existing is None:
        for w in read_warnings:
            print(w, file=sys.stderr)
        return False

    # 2. Merge fragment into existing config. #2506: its `ValueError`s are the
    # merged-set invariant asserts — the cluster's own inhibit rules are kept
    # by the merge, so this is where an existing Watchdog-suppressing rule
    # surfaces. Only this step is wrapped.
    try:
        assert_text_is_not_an_output(
            source_yml, f"the source in ConfigMap {namespace}/{configmap_name}",
            remedy=("--apply takes no --base-config: put your own config (the "
                    f"source) under the ConfigMap's '{AM_BASE_CONFIGMAP_KEY}' key, "
                    "then run --apply again."))
        # #3899: before the merge, so the remedy names the cluster's source —
        # after the generated-receiver check, the order the merge asks in.
        assert_base_is_a_source(existing)
        assert_base_has_no_dedup_rule(
            existing, f"the source in ConfigMap {namespace}/{configmap_name}",
            remedy=("--apply takes no --base-config: delete those rules from the "
                    f"source under the ConfigMap's '{AM_BASE_CONFIGMAP_KEY}' key "
                    f"(on a first --apply, with no '{AM_BASE_CONFIGMAP_KEY}' key yet, "
                    f"the '{AM_CONFIGMAP_KEY}' it adopts: put your own config without "
                    "them under that key), then run --apply again."))
        existing = merge_generated(existing, routes, receivers, inhibit_rules,
                                   strict=strict)
    except ValueError as exc:
        raise AlertmanagerConfigInvariantViolated(str(exc)) from exc
    merged_yml = dump_output_yml(existing)

    # 3. #2219 / #3689: Alertmanager gets the final say BEFORE the cluster is
    # touched, through the one gate every mode asks. `merged_yml` is the exact
    # string handed to `--from-literal=alertmanager.yml=` below, i.e. the text
    # Alertmanager loads.
    check = alertmanager_check(
        merged_yml,
        what=f"the merged alertmanager.yml for {namespace}/{configmap_name}",
        refusing="apply it to the cluster (nothing was applied)",
        show_loader_reason=show_loader_reason)
    print(check.message, file=sys.stderr)
    if check.exit_code is not None:
        raise AlertmanagerConfigRejected(check.exit_code)
    # #2660: the root route + receivers here are the CLUSTER's, kept by the
    # merge — the config about to be applied, so warn on it, as Alertmanager
    # reads it (ADR-036 §12): the gate's own reading.
    merged = check.reading
    warn_if_root_receiver_without_integration(merged)
    # #1211: on the same reading — the cluster's own firehose and
    # watchdog-heartbeat receivers decide whether those alerts reach anyone.
    reasons = judge_merged(merged) if judge_merged is not None else []
    if reasons:
        print(f"FAIL: {'; '.join(reasons)} (the ERROR line(s) above, --strict) "
              f"— refusing to apply the merged alertmanager.yml to "
              f"{namespace}/{configmap_name} (nothing was applied)", file=sys.stderr)
        raise AlertmanagerConfigRejected(EXIT_VIOLATION)

    # 4. Apply updated ConfigMap
    if not _apply_merged_configmap(merged_yml, namespace, configmap_name, source_yml):
        return False

    # 5. Reload Alertmanager
    return _reload_alertmanager(namespace)
