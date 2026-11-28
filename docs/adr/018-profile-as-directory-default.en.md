---
title: "ADR-018: Profile-as-Directory-Default"
tags: [adr, profile-builder, conf-d, v2.8.0]
audience: [platform-engineers, sre, contributors]
version: v2.9.0
lang: en
---

# ADR-018: Profile-as-Directory-Default

> **Language / 語言：** **English (Current)** | [中文](./018-profile-as-directory-default.md)

## Status

🟢 **Accepted** (v2.8.0, 2026-04-27). Paired with [ADR-016](016-conf-d-directory-hierarchy-mixed-mode.en.md) (directory hierarchy) and [ADR-017](017-defaults-yaml-inheritance-dual-hash.en.md) (inheritance semantics of `_defaults.yaml`).

Revised 2026-11-22: the Profile Builder that first implemented this rule (a Go library that clustered similar alert rules and wrote a `_defaults.yaml` and tenant override files for each cluster) was deleted from the code; it was never wired to any CLI ([#4228](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4228)). The rule itself still holds.

Revised 2026-11-24: rewritten to describe current behavior; the decision is unchanged.

Revised 2026-11-28: a redundant override of a threshold key is judged as a counterfactual — redundant only when deleting the line leaves what the exporter serves unchanged — instead of comparing two values ([#4330](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4330)).

## Decision

**A threshold shared by a group of tenants goes in the `_defaults.yaml` of the directory above them; a tenant file holds only the keys whose value differs from what the tenant inherits.**

1. **Shared values go under `defaults:` in `_defaults.yaml`.** The value is inherited down the directory chain to every tenant (inheritance semantics: ADR-017).
2. **Same as inherited means not written.** When a tenant's value equals the value it inherits, the tenant file does not carry that key; only a genuinely different value is written. Every line in a tenant file is then a deliberate deviation, and a reviewer does not have to guess whether it pins the value on purpose or is a stale line someone forgot to delete.
3. **Every tenant still has its own tenant file.** A tenant is declared by its tenant file: no file, no tenant. A tenant with nothing to override is written as an empty mapping (`db-c` below).
4. **The default is the group's median, not its mean.** One outlying tenant does not drag the default off. This is guidance for whoever arranges `conf.d/`; no tool computes it for you and no check enforces it.

### Example

Three tenants have thresholds 80, 95 and 80; the median, 80, goes in `_defaults.yaml`. `db-b` writes only the value where it differs and `db-c` overrides nothing; `db-a` carries a line equal to the default, which this rule says to delete:

```yaml
# conf.d/_defaults.yaml
defaults:
  mysql_connection_usage_percent: 80

# conf.d/db-a.yaml
tenants:
  db-a:
    mysql_connection_usage_percent: "80"

# conf.d/db-b.yaml
tenants:
  db-b:
    mysql_connection_usage_percent: "95"

# conf.d/db-c.yaml
tenants:
  db-c: {}
```

`da-guard served-values --config-dir conf.d` gives `mysql_connection_usage_percent` 80 for `db-a`, 95 for `db-b` and 80 for `db-c`: `db-c` inherits 80 from `_defaults.yaml`, and `db-a` gets 80 whether its line is deleted or not. Thresholds in a tenant file are written as strings per the schema; the exporter reads them as numbers.

### What enforces the rule

da-guard's `redundant_override` check: when a key in a tenant file has the same value the tenant would get with the key deleted, it reports a warning. The comparison is against this tenant's own inheritance: every `_defaults.yaml` along its directory chain, and the `_profile` the tenant binds (where the profile sets a value, the profile's value is what counts).

For a threshold key the verdict is a counterfactual: the line is redundant when, with it deleted from the tenant's own file and the tenant rebuilt, the threshold rows the exporter serves (value, severity, labels) are exactly the same from now on: at every cut point of the day's schedules, and after every `expires:` of the tenant's or the inherited value. It compares the exporter's own build and resolve, not two values, so YAML types and spellings (`80`, `"80"`, `"80.0"`, `"80:warning"`), schedules, and inheritance from subdirectories and profiles are all decided as the exporter reads them; `"80:critical"` serves another severity and is not redundant. A line another finding owns is not reported either (one cause, one finding): a value the exporter refuses (`value_not_served`), and any line a finding of the shared key verdict names (`da-guard key-verdict`; tenant-api's writes use the same verdict) — for example a key nothing reads (a dimensional label not declared under `dimensions:`), a retired key, a `disable` that switches nothing off, or a value the tenant's own base `disable` switches off. A threshold the tenant writes under both its new and its legacy spelling is not judged either: the line whose deletion changes nothing is the new spelling, and deleting it would leave only the retired one. On the example, `da-guard --config-dir conf.d` names `db-a` (exit code 0; summary and scanned-file list omitted):

```text
### Warnings (informational)

| Tenant | Field | Kind | Message |
|--------|-------|------|---------|
| db-a | mysql_connection_usage_percent | redundant_override | tenant "db-a" overrides "mysql_connection_usage_percent" with the same value as the new defaults; remove the override and rely on inheritance |
```

- A warning does not affect the exit code (0); with `--warn-as-error` the exit code is 1. Held by `TestRun_RedundantOverride_SurfacesAsWarning` and `TestRun_RedundantOverride_WithWarnAsError_ExitsOne` in `cmd/da-guard/main_test.go`.
- Tenants in different subdirectories are each compared against their own chain: `TestRun_RedundantOverride_HonorsCascadingDefaults`.
- Whether it reports is checked, case by case, against whether deleting the line leaves `da-guard served-values`' values and severities unchanged: `TestGuard_RedundantOverrideFiresExactlyWhenServedIsUnchanged` in `cmd/da-guard/redundant_override_read_test.go`; the shapes easy to misjudge (a root carrier without the `defaults:` wrapper, a `_critical` whose base is switched off, an undeclared dimensional label, a regex dimensional key containing `.`, schedules) are in `TestGuard_RedundantOverrideIsTheCounterfactual` in `cmd/da-guard/redundant_counterfactual_test.go`.
- A tenant bound to a `_profile` is compared against the profile's value: `TestGuard_RedundantOverrideFollowsTheProfile` in `cmd/da-guard/profile_guard_test.go`.

## Context

When a group of structurally similar tenants lands in `conf.d/`, there are three ways to write it:

1. **One complete tenant file per tenant**: N tenants repeat the same keys N times. Changing a shared value means editing N files, and nothing shows which values differ on purpose.
2. **Everything in `_defaults.yaml`, no thresholds in tenant files**: per-tenant tuning is lost.
3. **Shared values in `_defaults.yaml`, overrides only for tenants that differ**: ADR-017's merge rules already support this sparse override (a tenant file lists only some keys; the rest are filled in from above). What was missing is the rule: which value becomes the default, and which tenants count as "genuinely different". Without an explicit rule, every person handling the tree interprets it their own way.

This ADR picks option 3 and supplies the rule. It is a cross-component convention: tools that produce `conf.d/`, people reviewing PRs, and da-guard's redundant-override check all draw the same boundary; it also determines the shape of `conf.d/` that customers see.

## Consequences and known limitations

- **A redundant override is a warning, not a merge block.** It is harmless at runtime; it only makes the tenant file longer and the intent less clear. To block on it, add `--warn-as-error` in CI.
- **Reserved `_` keys and the values under them are still compared by YAML value and type.** They are not served as threshold rows but read by their own readers (the route generator, metadata, …), and the counterfactual does not cover them yet; da-guard reports one only when its value and type are both the same ([#4331](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4331)).
- **Only single values of reserved keys are compared.** A reserved key's mapping is flattened and each leaf compared; lists are not compared, and identical contents are not reported (`TestCheckRedundantOverrides_SkipsStructuredValues` in `internal/guard/guard_test.go`).
- **Keys a subdirectory `_defaults.yaml` cannot set are not judged.** The exporter does not apply `_`-prefixed keys (for example `_silent_mode`) from a subdirectory `_defaults.yaml` (ADR-017 decision 4), so the tenant's own line is the value in effect; such an override is not redundant and da-guard does not report it (`TestGuard_RedundantOverrideSkipsRefusedSubtreeKey` in `cmd/da-guard/subtree_reserved_test.go`).
- **A tenant can only override a declared threshold.** When a tenant file sets a threshold key that no `_defaults.yaml` declares, `/metrics` does not serve it; da-guard's main report says nothing, while `da-guard key-verdict` and tenant-api's write verdict read it as an unread error. What counts as declared (root directory, built-in layer, `optional_overrides:`) is in ADR-017 decisions 3, 4 and 11.
- **To switch off an inherited threshold, write `"disable"`, not `null`.** `null` in a tenant file is the same as not writing the key at that level; the tenant still gets the value from above (ADR-017 decision 4), and `check_confd_schema.py` refuses a threshold written as `null` in a tenant file.
- **A shared value placed in a subdirectory does not reach production under the standard deployment.** The flat assembly customers deploy with carries only the top-level files of conf.d, so a subdirectory `_defaults.yaml` never reaches the live exporter (ADR-016, "Consequences and known limits"). Until that is resolved, a shared value must be in the root `_defaults.yaml` to take effect in production.
- **No tool generates this shape from a rule set.** Customer rules are imported per tenant with `da-tools migrate`; bulk import has not shipped and is tracked in [#4226](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4226).

## Alternatives considered

- **A complete tenant file per tenant**: option 1 under "Context". Repetitive, and deliberate deviations are buried among repeated values.
- **Only `_defaults.yaml`**: option 2 under "Context". No per-tenant tuning.
- **Mean as the default**: a single outlier drags the default off, forcing tenants that agree with the majority to write overrides too.

## Related

- [ADR-016: conf.d/ Directory Hierarchy + Mixed Mode + Migration Strategy](016-conf-d-directory-hierarchy-mixed-mode.en.md)
- [ADR-017: Multi-Level `_defaults.yaml` Inheritance and Dual-Hash Hot Reload](017-defaults-yaml-inheritance-dual-hash.en.md)
- [#4211](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4211) — discussion of retiring the Profile Builder
- Implementation: `components/threshold-exporter/app/internal/guard/redundant.go` (the `redundant_override` check)
