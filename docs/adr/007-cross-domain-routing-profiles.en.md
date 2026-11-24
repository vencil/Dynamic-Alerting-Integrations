---
title: "ADR-007: Cross-Domain Routing Profiles and Domain Policies"
tags: [adr, architecture]
audience: [platform-engineers]
version: v2.9.0
lang: en
---
# ADR-007: Cross-Domain Routing Profiles and Domain Policies

> **Language / 語言：** **English (Current)** | [中文](./007-cross-domain-routing-profiles.md)

## Status

✅ **Accepted** (v2.1.0).

Revised 2026-09-28: profiles and domain policies are scoped to the subtree they sit in, profile names are unique across the tree, and policies stack (see "Scope in a hierarchical conf.d").

Revised 2026-10-16: domain policies (including `require_critical_escalation`) and the checks on resolved routing are made by the route generator only; da-guard and tenant-api no longer keep a copy each.

Revised 2026-11-24: rewritten to describe current behavior; CLI flags that do not exist (`--audit`, `--resolve-profiles`, `--check-policies`) removed.

## Decision

**Routing settings have two layers: routing profiles for reuse, domain policies for constraints.** The three-layer contact-profile model is not adopted.

1. **Routing profile**: `_routing_profiles.yaml` defines named routing settings; a tenant references one with `_routing_profile: <name>`. The merge order is `_routing_defaults` → profile → the tenant's own `_routing`, each step a shallow merge per top-level key.
2. **Domain policy**: `_domain_policy.yaml` declares compliance constraints for a set of tenants (allowed or forbidden receiver types, `group_by`, an upper bound on `repeat_interval`, a lower bound on `group_wait`, critical alerts must escalate to PagerDuty). It injects nothing; it only **validates** the merged result.
3. **`_routing_enforced` takes no part in the merge**: it is rendered as separate routes with `continue: true`, placed before the tenant routes, so a tenant cannot override it.
4. **Only the route generator judges domain policies.** Under `--validate --strict` a violation is a blocking ERROR; without `--strict` it is a WARN.

### Example: a route that breaks a domain policy

```yaml
# _routing_profiles.yaml
routing_profiles:
  team-sre-apac:
    receiver:
      type: webhook
      url: "https://alerting.internal/sre-apac"
    group_by: [tenant, alertname, severity]
    repeat_interval: 4h
```

```yaml
# _domain_policy.yaml
domain_policies:
  finance:
    description: "Finance domain"
    tenants: [db-a]
    constraints:
      forbidden_receiver_types: [slack, webhook]
      max_repeat_interval: 1h
```

```yaml
# db-a.yaml
tenants:
  db-a:
    _routing_profile: team-sre-apac
    mysql_connection_usage_percent: "80"
```

The output of `generate_alertmanager_routes.py --config-dir conf.d --validate --strict` (exit code 1; without `--strict` both become WARNs and the exit code is 0):

```text
Config files: 3 read, 0 skipped
Found 1 tenant(s) with routing config: db-a
Found 1 tenant(s) for severity dedup: db-a
  ERROR: domain_policy 'finance', tenant 'db-a': receiver type 'webhook' is forbidden — fix: domain forbids ['slack', 'webhook']; switch the tenant's receiver.type to a compliant type or amend the domain policy
  ERROR: domain_policy 'finance', tenant 'db-a': repeat_interval '4h' exceeds max '1h' — fix: lower the tenant's repeat_interval to '1h' or less, or raise the policy's max_repeat_interval
Validation: 1 route(s), 1 receiver(s), 2 inhibit rule(s)
FAIL: 2 error(s) found (listed above)
```

Tenant `db-a` writes no `_routing` of its own; its route comes entirely from the profile, and the policy judges the merged result.

## Background

As the number of tenants grew, routing settings were duplicated widely: several tenants share an on-call team, a notification channel and a grouping strategy, yet each tenant's `_routing` had to repeat them. Renaming a team's Slack channel meant editing N files. Business domains have different compliance requirements for routing (a finance domain forbids Slack and requires PagerDuty, for example), and nothing enforced them.

## The decisions in detail

### Routing profiles

- **The same fields as a tenant's `_routing`**: `routing-profiles.schema.json` references `tenant-config.schema.json#/definitions/routing` directly instead of keeping a second copy.
- **Shallow merge**: a top-level key the tenant's `_routing` writes replaces the profile's key of the same name as a whole, `routes` and `overrides` included. To drop a profile's `routes` for one tenant, write `routes: []`. A tenant that references no profile behaves as before.
- **`routes` (sub-routes split by label)**: each `match` only compares labels for equality (all must match) — no regex, no `continue`. Each becomes a sub-route under the tenant's main route, after the `overrides`, so matching goes `overrides` → `routes` → the main route's own receiver, first match wins. A sub-route's unset `group_wait` / `group_interval` / `repeat_interval` / `group_by` follow the main route, and the ones it sets go through the same timing guardrails. Receiver names are `tenant-<id>-route-<i>`. An invalid entry (an empty `match`, an invalid label name, a non-string value, an unsupported key) is skipped with a WARN, which fails `--validate`. `_routing_defaults` does not take `routes`.

### Domain policies

- **They judge the resolved routing**: the receiver types of the main receiver, `overrides` and `routes` are all judged; `forbidden_receiver_types` and `allowed_receiver_types` are judged separately and can both fire.
- **`require_critical_escalation`**: the tenant needs at least one `severity=critical` path to PagerDuty. It complies when the main receiver's type is `pagerduty`, or when a rendered `routes` entry matches `severity: critical` and sends to `pagerduty`. Non-compliance is the finding `critical_escalation_missing` (blocking under `--strict`).
- **WARN on critical leaks**: once compliant, every non-PagerDuty destination that can still receive critical alerts gets one `critical_escalation_leak` WARN, which does not affect the exit code. How: take destination N's `match` plus `severity: critical` as the alerts `C_N` it would catch; if N's `match` names another `severity` it receives no critical; if any sub-route before N has a `match` that is a subset of `C_N`, the alerts are caught first and nothing is reported. This is exact only inside the tenant's route subtree and only for equality matchers: platform routes placed before the tenant routes (Watchdog, sentinel) are not modelled and may cause over-reporting; a sub-route whose receiver is invalid and will not be rendered still counts as present and may cause over- or under-reporting. The `escalation` column of `tests/shared/routing_policy_parity_matrix.json` pins the verdicts.
- **Who judges**: the route generator only ([ADR-036](036-single-parser-effective-config.en.md) §9). da-guard does not: a `_domain_policy.yaml` in the tree is listed as undetermined (exit code 4) and left to the generator. tenant-api does not judge on its own: every write is judged by the generator over the tree after the write, non-compliance is a 422 `GENERATOR_FINDINGS` ([ADR-036](036-single-parser-effective-config.en.md) §4), and a WARN does not block the write. Since the whole tree is judged, after a profile or policy changes any write through tenant-api meets the existing tenants' non-compliance. CI blocks violations with `generate-routes --validate --strict`.

### Scope in a hierarchical conf.d

In a hierarchical conf.d ([ADR-016](016-conf-d-directory-hierarchy-mixed-mode.en.md)), the route generator and `check_routing_profiles.py` read `_routing_profiles.yaml` and `_domain_policy.yaml` (or `.yml`) on every level:

- **A profile is visible to its subtree**: resolving `_routing_profile: X`, a tenant looks at profiles defined on its own level or an ancestor's. Profile names are **unique across the whole tree**: one name defined in two files is an error (a `.yaml` and a `.yml` at the root clashing counts too), and the generator exits 2.
- **A policy applies only to its subtree**: a subtree policy whose `tenants:` names a tenant outside the subtree is an error (ERROR under `--strict`, WARN otherwise), worded apart from "this tenant is found nowhere". Policies on different levels **stack**: a tenant must satisfy every policy that applies to it, so a subtree can only tighten.
- **The merge order is unchanged**: `_routing_defaults` is supplied by the chain of directory levels and `_routing_enforced` is read only at the root; see [ADR-017 Decision 9](017-defaults-yaml-inheritance-dual-hash.en.md#9-routing-settings-are-inherited-level-by-level-along-directories). tenant-api serves only the tenant files at the root; the policy verdict on a write is the generator's, over the whole tree.

### Related tools

- `check_routing_profiles.py` (pre-commit): profile references exist and policy tenant lists are consistent.
- `explain_route.py --show-profile-expansion`: shows the routing before and after the profile is expanded, and the sub-routes actually rendered.
- `scaffold_tenant.py --routing-profile`: a new tenant references a profile directly.
- `da-tools opa-evaluate`: additional policies written in Rego (alongside domain policies).

## Consequences and known limits

- **A routing change goes from O(N) to O(1)**: N tenants share one profile, and changing the profile changes them all.
- **Compliance is machine-checked**: a platform engineer declares "the finance domain may not use Slack", and the generator and CI enforce it instead of human review.
- **A policy's `tenants` list is maintained by hand** and must follow the tenant files; at thousands of tenants it brings merge conflicts. Matching tenants by regex or prefix (`tenant_matchers`) is not implemented; see the deferred items.
- **The merge order has to be documented**: `_routing_defaults` → profile → the tenant's `_routing`, with `_routing_enforced` outside the merge.

## Alternatives considered

- **A three-layer contact-profile model** (contact → routing profile → domain policy): contact information overlaps with Alertmanager receivers, so an extra layer has to be kept in sync; a four-way merge has an unpredictable override order; no tenant needs to mix contacts within one profile. A layer can be added on top when the need appears.
- **Tenant-group inheritance**: an intuitive grouping, but implicit inheritance overrides by accident and conflicts with the existing defaults / enforced mechanisms.
- **Writing Alertmanager's native route tree directly**: no extra abstraction, but no named templates (everything is repeated by hand) and no constraint validation.

## Deferred

- **A profile extending another profile**: not implemented. Trigger: a team structure that needs layered profiles.
- **`tenant_matchers`**: regex or prefix matching instead of a policy's `tenants` array (the two can coexist, the exact list taking precedence). Not implemented. Trigger: the tenant count makes the hand-kept list a burden.

## Related

- [ADR-001](001-severity-dedup-via-inhibit.en.md) — inhibit rules for severity dedup, complementary to routing
- [ADR-003](003-sentinel-alert-pattern.en.md) — sentinel alerts
- [ADR-006](006-tenant-mapping-topologies.en.md) — tenant mapping topologies
- [ADR-016](016-conf-d-directory-hierarchy-mixed-mode.en.md), [ADR-017](017-defaults-yaml-inheritance-dual-hash.en.md) — directory hierarchy and inheritance
- [ADR-036](036-single-parser-effective-config.en.md) — who judges routing
- [Alertmanager route configuration](https://prometheus.io/docs/alerting/latest/configuration/#route)
