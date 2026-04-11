---
title: "ADR-007: Cross-Domain Routing Profiles and Domain Policies"
tags: [adr, architecture]
audience: [platform-engineers]
version: v2.6.0
lang: en
---

# ADR-007: Cross-Domain Routing Profiles and Domain Policies

> **Language / 語言：** **English (Current)** | [中文](007-cross-domain-routing-profiles.md)

## Status

✅ **Accepted** (v2.1.0) — Four-layer routing + domain policy validation completed, profile inheritance chain as v2.7.0+ candidate

## Background

As the number of tenants managed by the platform grows, routing configurations exhibit significant duplication. Multiple tenants share the same on-call teams, notification channels, and grouping policies, yet each tenant's `_routing` block must be configured independently.

### Problem Statement

1. **Configuration Duplication**: When 10 tenants are managed by the same team, identical `receiver`, `group_by`, `group_wait` settings are repeated 10 times
2. **Change Amplification**: Renaming a team's Slack channel requires modifying N tenant configurations
3. **Missing Domain Constraints**: Different business domains (finance, e-commerce) have different compliance requirements for routing (e.g., finance domain prohibits Slack notifications, mandates PagerDuty), but lack enforcement mechanisms
4. **Inheritance Conflicts**: Multi-layer configuration merging (`_routing_defaults` → tenant `_routing`) has unclear semantics in cross-domain scenarios

A mechanism is needed to make routing configurations reusable, constrainable, and shareable across domains.

## Decision

**Adopt a two-layer architecture: Routing Profiles (reuse) + Domain Policies (constraints), rather than a three-layer Contact Profile model.**

### Layer 1: Routing Profiles

Define named routing configurations in `_routing_profiles.yaml`; tenants reference them via `_routing_profile`:

```yaml
# _routing_profiles.yaml — in config-dir
routing_profiles:
  team-sre-apac:
    receiver: slack-sre-apac
    group_by: [tenant, alertname, severity]
    group_wait: 30s
    group_interval: 5m
    repeat_interval: 4h
    routes:
      - match: { severity: critical }
        receiver: pagerduty-sre-apac
        repeat_interval: 15m

  team-dba-global:
    receiver: slack-dba
    group_by: [tenant, alertname, db_type]
    group_wait: 1m
    group_interval: 10m
    repeat_interval: 8h
```

```yaml
# db-a.yaml — tenant configuration
db-a:
  _routing_profile: team-sre-apac   # reference profile, no duplication
  cpu_usage_percent: "80"
  memory_usage_percent: "85"
```

**Merge Semantics**: `_routing_defaults` → `routing_profiles[ref]` → tenant `_routing` → `_routing_enforced` (NOC override, immutable). Later layers override earlier ones, but `_routing_enforced` always takes final precedence.

### Layer 2: Domain Policies

Define business domain compliance constraints in `_domain_policy.yaml`. Domain Policies are **validation rules**, not an inheritance layer:

```yaml
# _domain_policy.yaml — in config-dir
domain_policies:
  finance:
    description: "Finance domain compliance requirements"
    tenants: [db-a, db-b, db-e]
    constraints:
      allowed_receiver_types: [pagerduty, email, opsgenie]
      forbidden_receiver_types: [slack, webhook]
      enforce_group_by: [tenant, alertname, severity]
      max_repeat_interval: 1h
      min_group_wait: 30s
      require_critical_escalation: true

  ecommerce:
    description: "E-commerce domain standards"
    tenants: [db-c, db-d]
    constraints:
      allowed_receiver_types: [slack, pagerduty, email]
      max_repeat_interval: 12h
```

**Validation Timing**: When `generate_alertmanager_routes.py` generates the final routes, it checks Domain Policy constraints per-entry. On constraint violation:
- `--strict` mode: Error and abort
- Default mode: Emit WARNING and flag

### Why Reject Three-Layer Contact Profile Model

The three-layer model proposed in the Gemini analysis (Contact Profile → Routing Profile → Domain Policy) carries over-engineering risk:

- **Contact Profile Overlaps with Alertmanager Receiver**: Contact information (Slack channels, PagerDuty keys) is already defined in Alertmanager `receivers`; an additional abstraction increases synchronization cost
- **Three-Layer Merge Semantics Complexity**: Four-way merging (defaults → contact → profile → tenant) has unpredictable override ordering, high debugging cost
- **YAGNI**: No current tenant requires mixing different contacts within the same profile; the system can be extended upward when the need arises

## Rationale

### Value of Routing Profiles

**Configuration Convergence**: 10 tenants sharing one profile reduces routing changes from O(N) to O(1).

**Clear Semantics**: A Profile is a "complete routing template", not a partial fragment. Merge order is unambiguous: defaults → profile → tenant override → enforced.

**Backward Compatible**: Tenants not referencing `_routing_profile` behave exactly as before; the profile mechanism is opt-in.

### Design Philosophy of Domain Policies

**Constraints, Not Inheritance**: Domain Policy does not "inject" configuration into tenants; it "validates" the final merged result. This avoids the diamond problem of multiple inheritance.

**Declarative Compliance**: Platform engineers can declare "finance domain tenants must not use Slack", and the toolchain enforces automatically, rather than relying on manual review.

**Auditable**: `generate_alertmanager_routes.py --audit` outputs a complete policy compliance report.

## Consequences

### Positive Impact

✅ Routing configuration duplication dramatically reduced; N tenants sharing a profile maintain only one copy
✅ Team routing changes become atomic operations (modify profile → all referencing tenants automatically updated)
✅ Domain policies provide machine-verifiable compliance constraints; CI can automatically block violations
✅ Fully backward compatible with existing tenants; both profiles and policies are opt-in
✅ No conflict with `_routing_enforced` (NOC override) mechanism

### Negative Impact

⚠️ `generate_alertmanager_routes.py` needs extension to parse `_routing_profiles.yaml` and `_domain_policy.yaml`
⚠️ Merge order (defaults → profile → tenant → enforced) must be thoroughly documented to avoid confusion
⚠️ Domain Policy `tenants` list must be kept in sync with actual tenant YAML files

### Operational Considerations

- `generate_alertmanager_routes.py` adds `--resolve-profiles` and `--check-policies` subcommands
- CI hook: `check_routing_profiles.py` validates profile references exist, policy tenant lists consistent
- Route debugging tool `explain_route.py` should show pre/post profile expansion diff
- Recommended profile naming convention: `team-{team}-{region}` or `domain-{domain}-{tier}`

### Future Extensibility

When tenant counts reach the thousands, maintaining a hardcoded `tenants` array will cause severe merge conflicts and maintenance burden. When implementing `generate_alertmanager_routes.py`, consider supporting `tenant_matchers` (regex/prefix matching) as an alternative syntax to `tenants`:

```yaml
domain_policies:
  finance:
    tenant_matchers:        # alternative to tenants
      - "^finance-db-.*"   # regex: auto-apply to all finance-db prefixed tenants
      - "payment-gateway"   # exact match still works
    constraints:
      forbidden_receiver_types: [slack, webhook]
```

This extension is backward compatible with v1's `tenants` array (both can coexist; `tenants` exact match takes priority). Implementation timing can be decided based on demand.

## Alternative Approaches Considered

### Approach A: Three-Layer Contact Profile Model (Rejected)
- Pros: More fine-grained contact management
- Cons: Overlaps with Alertmanager receiver concept, three-layer merge semantics complexity, YAGNI

### Approach B: Tenant Group Inheritance (Considered)
- Pros: Intuitive grouping concept
- Cons: Implicit inheritance prone to unexpected overrides, conflicts with existing defaults/enforced mechanism

### Approach C: Native Alertmanager Route Tree (Considered)
- Pros: Zero additional abstraction
- Cons: Alertmanager route tree does not support "named templates", requires manual duplication; no constraint validation capability

## Design Details

### Merge Pipeline

```
┌──────────────────┐
│ _routing_defaults │  ← Global defaults
└────────┬─────────┘
         ▼
┌──────────────────────┐
│ routing_profiles[ref] │  ← Team/domain shared named config
└────────┬─────────────┘
         ▼
┌──────────────────┐
│ tenant _routing   │  ← Tenant-level overrides (optional)
└────────┬─────────┘
         ▼
┌──────────────────────┐
│ domain_policies       │  ← Validation constraints (no value modification, only error/warn)
└────────┬─────────────┘
         ▼
┌──────────────────────┐
│ _routing_enforced     │  ← NOC immutable override
└──────────────────────┘
```

### Profile Reference Resolution

```python
# Extension logic for generate_alertmanager_routes.py (pseudocode)
def resolve_tenant_routing(tenant_cfg, profiles, defaults, enforced):
    base = copy(defaults)

    # If a profile is referenced, merge profile first
    if '_routing_profile' in tenant_cfg:
        profile = profiles[tenant_cfg['_routing_profile']]
        base = deep_merge(base, profile)

    # Then merge tenant-level overrides
    if '_routing' in tenant_cfg:
        base = deep_merge(base, tenant_cfg['_routing'])

    # Finally apply enforced (cannot be overridden)
    base = deep_merge(base, enforced)

    return base
```

### Policy Validation Logic

```python
def check_domain_policies(resolved_routing, tenant_id, policies):
    violations = []
    for policy_name, policy in policies.items():
        if tenant_id not in policy['tenants']:
            continue
        constraints = policy['constraints']

        if 'forbidden_receiver_types' in constraints:
            for recv_type in extract_receiver_types(resolved_routing):
                if recv_type in constraints['forbidden_receiver_types']:
                    violations.append(f"{policy_name}: {recv_type} forbidden")

        if 'max_repeat_interval' in constraints:
            if resolved_routing.get('repeat_interval') > parse_duration(constraints['max_repeat_interval']):
                violations.append(f"{policy_name}: repeat_interval exceeds max")

    return violations
```

## v2.1.0 Implementation Summary

- `generate_alertmanager_routes.py` — Four-layer merge (defaults → profile → tenant → enforced) + `check_domain_policies()` validation (21 tests)
- `check_routing_profiles.py` — Profile/Policy lint tool (28 tests + pre-commit hook auto-run)
- `explain_route.py` — Routing debug tool with `--show-profile-expansion` trace mode (25 tests + da-tools CLI integration)
- `scaffold_tenant.py --routing-profile` — Onboarding integration, new tenants can reference profiles directly (9 tests)
- `_parse_config_files()` → `_parse_platform_config()` + `_parse_tenant_overrides()` sub-function refactor
- Example configs `conf.d/examples/_routing_profiles.yaml`, `conf.d/examples/_domain_policy.yaml`
- JSON Schema: `routing-profiles.schema.json`, `domain-policy.schema.json`
- Go/Python dual-side `_routing_profile` reserved key sync
- Self-Service Portal: routing profile validation + example toggle UI

## Evolution Status

- **v2.1.0** (completed): Four-layer merge pipeline, `check_routing_profiles` lint, `explain_route` debug tool
- **v2.3.0** (completed): OPA integration — `da-tools opa-evaluate` supports Rego-defined domain policies (routing-compliance, threshold-bounds, naming-convention example policies)
- **v2.5.0** (completed): Domain Policy enforcement moved from CI-time validation to API-time enforcement (tenant-api 403 responses)
- **v2.6.0** (completed): `generate_alertmanager_routes.py` refactored (21 helpers extracted), `_build_receiver_config()` converted to strategy pattern

**Remaining**:
- Profile inheritance chain (profile extends another profile) — v2.7.0+ candidate
- `tenant_matchers` (regex / prefix) to replace hardcoded `tenants` arrays — v2.7.0+ candidate

## Related Decisions

- [ADR-001: Severity Dedup via Inhibit Rules](./001-severity-dedup-via-inhibit.md) — inhibit rules complement routing
- [ADR-003: Sentinel Alert Pattern](./003-sentinel-alert-pattern.md) — sentinel alerts affect routing behavior
- [ADR-006: Tenant Mapping Topologies](./006-tenant-mapping-topologies.md) — 1:N mapped tenants still use routing profiles

## References

- [`docs/architecture-and-design.en.md`](../architecture-and-design.md) §2.9 — Routing Guardrails
- [`docs/architecture-and-design.en.md`](../architecture-and-design.md) §2.11 — Dual-Perspective routing
- [`generate_alertmanager_routes.py`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/scripts/tools/ops/generate_alertmanager_routes.py) — Route generator (to be extended)
- [Alertmanager Route Configuration](https://prometheus.io/docs/alerting/latest/configuration/#route) — Official documentation

## Related Resources

| Resource | Relevance |
|----------|-----------|
| [001-severity-dedup-via-inhibit.en](001-severity-dedup-via-inhibit.en.md) | ⭐⭐ |
| [002-oci-registry-over-chartmuseum.en](002-oci-registry-over-chartmuseum.en.md) | ⭐ |
| [003-sentinel-alert-pattern.en](003-sentinel-alert-pattern.en.md) | ⭐⭐ |
| [004-federation-central-exporter-first.en](004-federation-central-exporter-first.en.md) | ⭐ |
| [005-projected-volume-for-rule-packs.en](005-projected-volume-for-rule-packs.en.md) | ⭐ |
| [006-tenant-mapping-topologies.en](006-tenant-mapping-topologies.en.md) | ⭐⭐⭐ |
| [007-cross-domain-routing-profiles.en](007-cross-domain-routing-profiles.en.md) | ⭐⭐⭐ |
| [README.en](README.en.md) | ⭐⭐⭐ |
| ["Architecture and Design"](../architecture-and-design.md) | ⭐⭐⭐ |
| ["Architecture & Design — Appendix A"](../architecture-and-design.en.md#appendix-a-role-tool-quick-reference) | ⭐⭐ |
