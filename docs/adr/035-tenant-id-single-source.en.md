---
title: "ADR-035: Single Source for the Legal Tenant-ID Character Set"
tags: [adr, config, multi-tenant, dx]
audience: [platform-engineers, sre, contributors]
version: v2.9.0
lang: en
---

# ADR-035: Single Source for the Legal Tenant-ID Character Set

> **Language / 語言：** [中文](./035-tenant-id-single-source.md) | **English (Current)**

## Status

✅ **Accepted** (drafted 2026-10-03, approved by the owner 2026-10-03).

- The decisions were settled by the owner in [#2655](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2655); two of them were settled after the external review.
- The external review was carried out adversarially by a different model, and its conclusions have been merged into this document.
- The owner has approved this document. Three revisions were made at approval time: former open question 1 (the tenant-api read and delete paths) is resolved, the fifth DNS-1123 copy in `init_project.py` is now listed, and `definitions.tenantId` gains a `description`.

## Summary

**Problem**

- The repo holds four mutually inconsistent rules for the legal tenant-id character set, spread across Python, JS, and the #2633 transitional rule.
- The exporter, the tenant-api read path, and `scaffold_tenant` barely check at all.

**Decision**

1. The final rule is **DNS-1123 label**.
2. The rule is **written once**, in the tenant-config schema. Python reads it at runtime; Go and the portal read the same JSON copy generated from the schema.
3. **Block outright, everywhere**: every generation mode exits non-zero on an illegal id and emits no config.
4. The exporter loads as usual and only prints a WARN.

This ADR only defines the rule; the implementation goes in a separate PR.

## Problem

### Current state (main `ecae7115`)

| Location | Rule |
|---|---|
| `operator_generate.py:54`, `migrate_to_operator.py:49` | DNS-1123 label (`fullmatch`) |
| portal `operator-setup-wizard/utils/generators.js:26` | DNS-1123 shape, but **no 63-character limit** |
| `_validate_tenant_name` in `init_project.py` | DNS-1123 label (`fullmatch` plus `len <= 63`; same rule as the first row, but another hand-written copy) |
| `alert_quality.py:65` | `^[a-zA-Z0-9_-]+$` |
| Generators, da-guard, tenant-api write path (#2633 transitional rule; `_lib_validation.py:98`, `routingpolicy/tenantid.go`) | Rejects the empty string and anything not matching `^[A-Za-z0-9_-]+$` |
| tenant-api read path `ValidateTenantID` (`handler/sanitize.go:14`) | Only rejects path separators, `..`, non-base names, and reserved file names |
| `scaffold_tenant.py --tenant` | No validation; the id is used directly as the key and the file name |
| exporter (`ParseTenantFile` in `config_file.go`) | Only requires valid UTF-8; otherwise the whole file is rejected |
| JSON Schema (`check_confd_schema`) | No constraint on the keys of `tenants` |

### Why we cannot stop at the transitional rule

- **Operator mode needs DNS-1123.** Operator mode puts the id into K8s object names (`da-tenant-{id}`, `da-{id}-{receiver_type}`). Object names must follow the DNS subdomain rule: lowercase alphanumerics, `-`, `.`, with no uppercase or underscores. So operator mode needs a separate DNS-1123 rule, and the same id ends up legal in config-driven mode but illegal in operator mode.
- **Multiple rules keep multiplying.** The original question in #2341 was exactly "which one is authoritative".

### Measurements

- Across git-tracked `*.yaml` / `*.yml` files in the repo, the `tenants:` keys contain 33 distinct ids, all DNS-1123 compliant, the longest being 21 characters.
- Another 3 keys come from CRD schema structure (`k8s/crd/thresholdconfig-crd.yaml`) and are not tenant ids.
- The ids customers actually use cannot be measured; this is the biggest unknown in this decision, see "Failure modes".

## Decision

### D1: The final rule is DNS-1123 label

```text
^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$
```

Lowercase alphanumerics and `-`, length 1–63, must start and end with an alphanumeric.

- **What we gain**
  - Operator mode and config-driven mode share one rule.
  - The new rule is a subset of the transitional rule and only tightens, consistent with the #2633 design of "only tighten, never loosen back".
  - Consistent with K8s namespace naming.
- **What we give up**
  - Ids containing uppercase letters, underscores, or longer than 63 characters must be renamed.
  - After a rename the metric `tenant` label value changes, so historical data is discontinuous.
- **All-numeric ids** (e.g. `123`) are legal under DNS-1123, but unquoted in YAML they parse as integer keys, which `check_confd_schema` rejects as "not a string". Such ids must therefore be quoted, and the migration docs must say so.

### D2: Write the rule once, in the tenant-config schema

**The rule itself**

- Add `definitions.tenantId` (`type: string`, `pattern`) to `docs/schemas/tenant-config.schema.json`.
- Add `propertyNames: {"$ref": "#/definitions/tenantId"}` to `properties.tenants`.
- The schema is draft-07, which supports `propertyNames`, so `check_confd_schema` starts checking without any code change. The external review tested this on a copy of the schema: `Team_A` returns rc 1; the repo's conf.d returns rc 0.
- No separate `maxLength`. The pattern already bounds the length; stating it twice would only produce two errors per violation.
- `definitions.tenantId` also carries an English `description` that explains the rule in plain words. It ships inside the generated JSON copies; the error messages of the generators, da-guard, and tenant-api cite this text instead of each describing the rule on their own. The portal UI keeps its own localized hint text.

**Python**

- `_lib_validation.is_valid_tenant_id` reads this pattern at runtime, reusing the `_find_tenant_schema()` lookup: inside the da-tools image it reads the flat layout; inside the repo it walks up to the project root.
- build.sh's `REPO_DATA_FILES` already ships this schema, and `check_build_completeness` already lists it as a required file.
- If the schema cannot be read, fail closed, following the #2180 receiver URL pattern.
- The following four tools drop their own regex and call this function instead: `operator_generate.py`, `migrate_to_operator.py`, `alert_quality.py`, `init_project.py` (`_validate_tenant_name`); `scaffold_tenant.py`, which did no validation before, also calls it.

**Go and the portal**

A Go binary cannot read the repo's `docs/` at runtime, so we use "generated copy + drift guard", the same approach as `recipe-status.json`:

- A generator script extracts `definitions.tenantId` from the schema and writes two JSON files:
  - `components/threshold-exporter/app/pkg/tenantid/tenant_id.json`
  - the corresponding data file in the portal
- Go reads it via `go:embed`, and `routingpolicy.IsValidTenantID` switches to it. tenant-api references it through the existing `replace github.com/vencil/threshold-exporter => ../threshold-exporter/app`. tenant-api's Dockerfile copies that whole module, and `.dockerignore` does not exclude `*.json`.
- The portal's `validateTenantName` switches to reading its data file and drops its own regex.
- Add a drift check to pre-commit: both copies must equal a fresh regeneration.

**Why not the receiverspec pattern**

The receiverspec pattern hand-copies constants on the Go side and then compares them against the schema in `TestSpecs_MatchSchema`; see `pkg/receiverspec/spec.go`. It saves the generator script, but has two problems:

- The rule text is still hand-copied on the Go side, which contradicts the decision of "one data file, read by both languages".
- The portal has no Go test to lean on, so a third copy would remain.

**Pattern dialect**

- The pattern may only use the subset whose semantics agree across ECMA-262, Python `re`, and Go RE2: character classes, `{m,n}`, `^`, `$`; no lookaround, backreferences, or Unicode classes.
- The external review tested this: the rule gives identical verdicts in all three for `abc`, `abc\n`, and `é`.
- Match semantics differ by path:
  - `is_valid_tenant_id`: uses `re.fullmatch`, because Python's `$` swallows a trailing `\n`.
  - Go: anchored with `^…$`; RE2's `$` does not swallow `\n`.
  - JSON Schema's `pattern` has **search** semantics, and `$` behaves differently per validator: `check_confd_schema` (Python jsonschema) lets a key with a trailing `\n` through (tested with jsonschema 4.26: `{"abc\n": 1}` returns 0 errors); the editor path, yaml-language-server, uses ECMAScript's `$` and rejects it (`new RegExp(pattern).test("abc\n")` is false).
- A key with a trailing `\n` comes from a YAML block scalar or from the `\n` escape in a double-quoted scalar (`"abc\n": 1`). Under this ADR, the generators, da-guard, and tenant-api write validation all reject such decoded keys, so we accept this difference on the schema path; tenant-api read validation stays broader, see "Resolved questions".
- The `tenant_ids` table in the parity matrix gets a case that runs jsonschema directly, to pin this difference down.

- **What we gain**: there is only one copy of the rule text, and all four readers get the rule from it: the generators (Python), da-guard and tenant-api (Go), the portal, and the schema checker.
- **What we give up**
  - One more generator script and one more drift guard.
  - Python depends on this schema file at runtime. That dependency already exists (#2180).

### D3: Block outright, everywhere — every generation mode exits non-zero

Once the new rule ships, every reader switches to the DNS-1123 verdict at the same time: generators, da-guard, the tenant-api write path, the schema, the portal, `scaffold_tenant`, and `init_project`.

In tenant-api the new rule applies to **every write path**; for the scope and the enforcement point see "Resolved questions".

When a generator hits an illegal id, it **exits non-zero and emits no config in every mode**, including plain generation, `--dry-run`, `--apply`, `--output-configmap`, `--validate`, and `--strict`.

**This is an exception to the wave-2 1A rule.** The 1A rule is "print a WARN and skip the item; render still returns rc 0, and only `--validate` returns rc 1". The reason for the exception is in the first row of "Failure modes": skipping a tenant silently makes its alerts disappear, whereas skipping a route has no such consequence.

- **What we gain**
  - We never deploy an Alertmanager config that is "missing a tenant". The deploy job turns red and the live config stays untouched.
  - Ids caught by the #2633 transitional rule (empty string, containing spaces) also switch to a non-zero exit instead of being silently skipped.
- **What we give up**
  - A single illegal id blocks config updates for all tenants until it is renamed.
  - Customers using uppercase or underscore ids will see CI and deploys turn red after upgrading the image.

### D4: The exporter loads as usual and prints a WARN

The exporter does not reject tenants with illegal ids; on every reload it prints one WARN per illegal id. No new metric and no new alert.

- **What we gain**: the exporter is a runtime component, so gatekeeping is concentrated in CI and the write side; one illegal id does not make that tenant's threshold metrics disappear.
- **What we give up**: while D3 is blocking the deploy, the exporter still emits metrics for the illegal id, while the live Alertmanager config is still the old one. Alerts in this window go through the old config, so none are lost.

## Failure modes and guardrails

| Scenario | Outcome | Guardrail |
|---|---|---|
| A customer already uses ids like `Team_A` and upgrades to the new image | The generator exits non-zero in every mode and emits no config; tenant-api refuses writes to that tenant; the exporter keeps emitting metrics and prints a WARN; the live Alertmanager config stays on the old version, so no alerts are lost | Release notes and the changelog mark this as **breaking** with rename steps; `da-tools validate-config` lists every illegal id |
| **If D3 blocked only under `--validate`** (rejected by this ADR) | The generator skips that tenant and render returns rc 0, deploying a config missing that tenant; its alerts fall to the root route's receiver, and the repo-shipped `default` is an empty receiver (`k8s/03-monitoring/configmap-alertmanager.yaml:107`, the built-in base in `_grar_render.py`), which amounts to silently dropping them | This is exactly why D3 became "every mode exits non-zero" |
| Rename | The metric `tenant` label value changes and history is discontinuous; Alertmanager silences and inhibits must be updated too; all-numeric ids must be quoted | Covered in the migration docs |
| The schema file is missing from the da-tools image | Python fails closed and `generate-routes` stops | build.sh's `REPO_DATA_FILES` already ships the file, and `check_build_completeness` guards it |
| Someone changes the schema without regenerating the copies | Go, the portal, and Python disagree in their verdicts | The pre-commit drift guard |

## Alternatives considered and rejected

| Option | Reason for rejection |
|---|---|
| Keep the transitional rule `^[A-Za-z0-9_-]+$` and add a length limit | Operator mode would still need a separate DNS-1123 rule |
| Two tiers: the transitional rule at the core, with operator mode additionally requiring DNS-1123 | The same id would be legal in one mode and illegal in another, and two tiers must be maintained |
| A permissive rule, e.g. Mimir / Loki allow `!-_.*'()` with length 150 | Still does not fit into K8s object names; `.` and `*` would appear in receiver names, file names, and URLs |
| Staged rollout: WARN first, block in the next minor version | The owner chose to block outright, so the rule is consistent across all readers from day one |
| Block outright, but fail only under `--validate` (keeping 1A) | Deploy paths without `--validate` would silently drop that tenant's alerts (see the second row of "Failure modes") |
| One copy per language, pinned only by parity; or the receiverspec pattern | More than one copy of the rule text, and the portal cannot be included |
| The exporter skips tenants with illegal ids and adds a counter and an alert | Needs a new mechanism; failing closed would make that tenant's metrics disappear |
| The exporter rejects the whole file (reusing parse failure) | Other tenants in the same file would be dropped along with it |
| Leave the portal out and only flag it as a known inconsistency | The JS rule would keep drifting, and it already lacks the 63-character limit today |

## Resolved questions

- **Whether the tenant-api read and delete paths should apply the new rule.** Only the write paths apply it; the read paths are unchanged. Confirmed at approval time:
  - tenant-api has no route that deletes or renames a tenant; renames always happen in git, so the new rule does not block a rename migration.
  - The new rule applies to every write path. The enforcement point is `gitops.guardTenantID` (`components/tenant-api/internal/gitops/writer.go`), which covers PUT, batch writes, group batch writes, custom-alerts, the federation subset, and dry-run.
  - The handlers keep `ValidateWritableTenantID` to return 400 early.
  - The rule is **not** put into `confd.IsAddressableTenantID`, because the read paths call it too.
- **Whether the 63-character limit should subtract the prefix.** It should not. The external review confirmed by grep: what operator mode writes into K8s labels is the **bare id** (`metadata.labels: {"tenant": id}`, see `operator_generate.py:461-466`, `migrate_to_operator.py:356-361`). Only object names (limit 253) and Alertmanager receiver names (no 63 limit) carry the prefix.

## Open questions

1. **Whether to WARN when the root receiver is empty** (out of scope, but related). When the generator detects that the base config's root receiver has no integration, it could print a WARN reminding that unrouted alerts will be silently dropped. This is not part of the tenant-id rule and is tracked in [#2660](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2660).

## Places to update during implementation

- The `invalid_tenant_id` row in `docs/cli-reference{,.en}.md`, which currently says "uppercase is allowed".
- Passages mentioning uppercase ids in `docs/integration/gitops-deployment*.md`, `byo-alertmanager-integration.md`, and `tenant-federation.md`; check each one.
- `_validate_tenant_name` in `scripts/tools/ops/init_project.py`: drop the hand-written regex and length check and call `_lib_validation` instead.
- tenant-api's `gitops.guardTenantID`: add the new rule; leave `confd.IsAddressableTenantID` unchanged.
- The WARN text in `_grar_parse.py` and `_grar_validate.invalid_tenant_id_text`, which currently says "letters, digits, '_' and '-' only".
- The `tenant_ids` table in `tests/shared/routing_policy_parity_matrix.json`: move cases such as `UPPER`, `Mixed_Case-1`, `_x`, `-x`, `x_`, and 64 characters to invalid, and update the description text.
- Tests such as `components/tenant-api/internal/handler/tenant_id_write_test.go`, `tests/ops/test_tenant_name_rfc1123.py`, and `pkg/routingpolicy/parity_test.go`.

## References

- [#2341](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2341), [#2655](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2655), [#2633](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/2633) (transitional rule)
- [#2180](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2180) (precedent for writing the pattern in the schema and having Python read it at runtime)
- `scripts/tools/dx/gen_recipe_status_json.py` (precedent for generating a copy consumed by both Go and the portal, with a drift guard)
