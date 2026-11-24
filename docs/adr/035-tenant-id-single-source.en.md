---
title: "ADR-035: Single Source for the Legal Tenant-ID Character Set"
tags: [adr, config, multi-tenant, dx]
audience: [platform-engineers, sre, contributors]
version: v2.9.0
lang: en
---

# ADR-035: Single Source for the Legal Tenant-ID Character Set

> **Language / 語言：** **English (Current)** | [中文](./035-tenant-id-single-source.md)

## Status

✅ **Accepted** (2026-10-03, approved by the owner).

Revised 2026-11-05: `operator_generate.py` and `migrate_to_operator.py` no longer read tenants ([ADR-008](008-operator-native-integration-path.en.md)) and are removed from the list of readers; the 63-character limit is unchanged.

Revised 2026-11-24: rewritten to describe current behavior; the decisions are numbered 1–4.

## Decision

1. **The rule is a DNS-1123 label**: lowercase letters, digits and `-`, 1–63 characters, starting and ending with a letter or digit (the pattern is in section 1).
2. **The rule is written once**, as `definitions.tenantId` in the tenant-config schema. Python reads it at run time; Go and the portal read JSON copies generated from it.
3. **The route generator exits non-zero on an invalid id in every mode, and outputs no configuration.**
4. **The exporter loads the tenant as usual and only logs a WARN.**

### Example: one invalid id

```yaml {.confd .expect-invalid}
# conf.d/Team_A.yaml
tenants:
  Team_A:
    mysql_connection_usage_percent: "70"
    _routing:
      receiver:
        type: webhook
        url: "https://hooks.example.com/a"
```

The output of `generate_alertmanager_routes.py --config-dir conf.d --output-alertmanager --dry-run` (exit code 1; `--validate` gives the same):

```text
Config files: 1 read, 0 skipped
FAIL: 1 invalid tenant id(s) — nothing was generated, written or applied; rename the tenant(s):
  Team_A.yaml: tenant id 'Team_A' is not a valid tenant id (Tenant id (ADR-035): a DNS-1123 label — 1-63 characters of lowercase letters, digits and '-', starting and ending with a letter or digit.) — no route, receiver or inhibit rule is rendered for it
```

The sentence in parentheses is the schema's `definitions.tenantId.description`; the error messages of the generator, da-guard and tenant-api all quote it instead of describing the rule themselves. Renamed to `team-a`, the tenant gets its configuration.

## Background

Before this decision the repository had four tenant-id rules that disagreed: a DNS-1123 label (hand-written once in the operator generator and again in `init_project.py`), the portal's DNS-1123 shape without the 63-character limit, `alert_quality.py`'s `^[a-zA-Z0-9_-]+$`, and the interim rule the generator, da-guard and tenant-api's write path shared (refusing only an empty string and characters outside `^[A-Za-z0-9_-]+$`). The exporter, tenant-api's read path and `scaffold_tenant.py` checked almost nothing, and the JSON schema did not constrain the keys of `tenants`.

Why DNS-1123:

- **The id ends up in Kubernetes object names and labels.** Object names accept only lowercase letters, digits, `-` and `.`; with the interim rule, an id valid in config-driven mode is invalid in a K8s object.
- **It is a subset of the interim rule**, so it only tightens, and it matches Kubernetes namespace naming.
- **Every id in the repository already complies.** At drafting time the tracked YAML held 33 distinct ids under `tenants:`, all DNS-1123 labels, the longest 21 characters. What customers actually use cannot be measured; see "Consequences and known limits".

## The decisions in detail

### 1. The rule is a DNS-1123 label

```text
^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$
```

- An id with uppercase letters, underscores or more than 63 characters must be renamed. The metric's `tenant` label value then changes and history is not continuous.
- An all-digit id (for example `123`) is valid, but unquoted in YAML it parses as an integer key, which the schema check refuses as "not a string"; quote it.
- The 63-character limit does not subtract a prefix: the id itself is used in file names, ConfigMap keys and label values.

### 2. The rule is written once, in the tenant-config schema

- **The rule**: `definitions.tenantId` in `docs/schemas/tenant-config.schema.json` (`type: string` and `pattern`); `properties.tenants` references it through `propertyNames`, so the schema check checks every tenant key. There is no separate `maxLength`: the pattern already bounds the length, and writing it twice would only give two errors for one violation. The `description` is the one-sentence rule for people, shipped with the copies.
- **Python**: `_lib_validation.is_valid_tenant_id` reads this pattern at run time; the da-tools image ships the schema. If the schema cannot be read, it fails closed. `alert_quality.py`, `init_project.py` and `scaffold_tenant.py` all call it instead of keeping their own regex.
- **Go and the portal**: a Go binary cannot read the repository's `docs/` at run time, so it reads a generated copy. `scripts/tools/dx/gen_tenant_id_json.py` extracts `definitions.tenantId` from the schema and writes `components/threshold-exporter/app/pkg/tenantid/tenant_id.json` (read with `go:embed`; `Valid` in `pkg/tenantid` is what the exporter, da-guard and tenant-api's write path call) and the portal's `_common/data/tenant-id.json`. The pre-commit hook `tenant-id-json-check` requires both copies to equal a fresh regeneration.
- **Pattern dialect**: only the subset on which ECMA-262, Python `re` and Go RE2 agree (character classes, `{m,n}`, `^`, `$`) — no lookaround, backreferences or Unicode classes. Matching is always a full match: Python uses `re.fullmatch` (its `$` accepts a trailing `\n`), Go anchors with `^…$`.

### 3. The generator exits non-zero in every mode

The generator, da-guard, tenant-api's write path, the schema, the portal, `scaffold_tenant` and `init_project` all judge with DNS-1123. On an invalid id the generator exits non-zero and outputs no configuration in every mode: plain generation, `--dry-run`, `--apply`, `--output-configmap`, `--validate` and `--strict`.

The generator's usual convention for other problems is "log a WARN, skip the item, succeed, and fail only under `--validate`". This is an exception: skipping a route loses no alert, skipping a tenant does. Its alerts fall to the root route's receiver, and the root receiver the repository ships by default is empty, so they are silently dropped. The generator also logs a WARN when the root receiver has no integration.

tenant-api applies the rule on every write path: the enforcement point is `gitops.guardTenantID`, covering PUT, batch writes, group batch writes, custom-alerts, the federation subset and dry-runs; the handler's `ValidateWritableTenantID` returns an early 400. Read paths are not affected: the rule is not put into `confd.IsAddressableTenantID`, which read paths also call. tenant-api has no endpoint to delete or rename a tenant — renames happen in git — so the rule never blocks a rename.

### 4. The exporter loads the tenant and logs a WARN

The exporter does not refuse a tenant with an invalid id; on each reload it logs one WARN per invalid id, quoting the rule's `description`. No metric and no alert are added. Enforcement sits in CI and on the write side; one invalid id does not make that tenant's threshold metrics disappear.

## Consequences and known limits

- **A deployment using uppercase or underscore ids turns red in CI and deployment after upgrading the image.** The generator outputs no new configuration, the live Alertmanager configuration stays as it was and alerts follow it, so none are lost; tenant-api refuses writes to that tenant; the exporter keeps emitting metrics and logs a WARN. As long as one id is invalid, configuration updates for every tenant are blocked until it is renamed. `da-tools validate-config` lists every invalid id.
- **The cost of a rename**: the metric's `tenant` label value changes and history is not continuous; Alertmanager silences and inhibit rules must follow.
- **The schema path judges a trailing `\n` inconsistently.** JSON Schema's `pattern` has search semantics: Python jsonschema lets a key with a trailing `\n` pass, an editor (ECMAScript's `$`) refuses it. The generator, da-guard and tenant-api's write validation all refuse such a key after decoding, so the difference is accepted; tenant-api's read validation is looser. The parity matrix's `tenant_ids` table pins it with a case that runs jsonschema directly.
- **Someone changes the schema without regenerating the copies**, and Go, the portal and Python disagree: refused by `tenant-id-json-check`.
- **The schema file missing from the da-tools image** makes the Python side fail closed and stops `generate-routes`; `check_build_completeness` guards the shipped file list.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Keep the interim rule `^[A-Za-z0-9_-]+$` and add a length limit | Does not fit K8s object names; a second DNS-1123 rule would still be needed |
| Two tiers: the interim rule at the core, DNS-1123 required when going into K8s | One id valid in one mode and invalid in another; two tiers to maintain |
| A loose rule (for example Mimir/Loki's `!-_.*'()`, length 150) | Also does not fit K8s object names; `.` and `*` would appear in receiver names, file names and URLs |
| Staged rollout: WARN first, block in the next minor version | The owner chose to block at once, so the rule is the same for every reader from day one |
| Block, but fail only under `--validate` | A deployment path without `--validate` would silently drop that tenant's alerts |
| One copy per language pinned only by a parity test; or a Go constant copied by hand and compared against the schema by a test | More than one copy of the rule text; the portal has no Go test and would keep a third copy |
| The exporter skips tenants with invalid ids and adds a counter and an alert | A new mechanism; that tenant's metrics would disappear |
| The exporter refuses the whole file | The other tenants in the same file would be dropped too |
| Leave the portal out and mark it as a known inconsistency | The JS copy would keep drifting |

## Related

- [ADR-008](008-operator-native-integration-path.en.md) — withdrawal of the operator path
- [#2655](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2655) — discussion and decision
- `scripts/tools/dx/gen_recipe_contract.py` — the same precedent: a generated copy read by Go and the portal, with a drift guard
