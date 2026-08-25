---
title: "ADR-017: _defaults.yaml Inheritance Semantics + Dual-Hash Hot-Reload"
tags: [adr, defaults, inheritance, hot-reload, dual-hash, v2.7.0]
audience: [platform-engineers, sre, contributors]
version: v2.9.0
lang: en
---

# ADR-017: _defaults.yaml Inheritance Semantics + Dual-Hash Hot-Reload

> **Language / 語言：** **English (Current)** | [中文](./017-defaults-yaml-inheritance-dual-hash.md)

> Second building block of v2.7.0 Scale Foundation. Paired with [ADR-016](016-conf-d-directory-hierarchy-mixed-mode.en.md) (Directory Hierarchy).

## Status

✅ **Accepted** (v2.7.0, 2026-04-19) — Multi-level `_defaults.yaml` inheritance, dual-hash hot-reload, and 300ms debounce shipped with v2.7.0; the noop-semantic split (`shadowed` / `cosmetic`) was added as a v2.8.0 amendment.

## Context

The v2.6.x `_defaults.yaml` only exists as a single global defaults file in the flat `conf.d/` root.
With ADR-016's hierarchical directories, we need to define multi-layer `_defaults.yaml` inheritance semantics:

- Which directory levels can contain `_defaults.yaml`?
- How do parent-child defaults merge?
- When `_defaults.yaml` changes, which tenants need reload? How do we prevent reload storms?

v2.5.0 already has SHA-256 hot-reload (`source_hash` comparison), but it only tracks tenant YAML itself.
Now a tenant's **effective config** depends on both its own YAML and inherited defaults,
requiring a second hash to determine "did the effective config actually change?"

## Decision

### Inheritance Levels

`_defaults.yaml` can appear at any of the following levels (all optional):

```
conf.d/
├── _defaults.yaml              ← L0: global defaults
├── {domain}/
│   ├── _defaults.yaml          ← L1: domain-level defaults
│   └── {region}/
│       ├── _defaults.yaml      ← L2: region-level defaults (uncommon)
│       └── {env}/
│           ├── _defaults.yaml  ← L3: env-level defaults
│           └── tenant-001.yaml
```

Inheritance order: **L0 → L1 → L2 → L3 → tenant YAML** (later overrides earlier).

### Merge Semantics: Deep Merge with Override

- **Dict/Map fields**: deep merge (child layer's new keys preserved, same keys overridden by child)
- **Array/List fields**: **replace, not concat** (avoids ambiguity — "I overrode group_by, why are old values there?")
  - ⚠️ **Known exception: `_custom_alerts` uses UNION** (ADR-024 / #772; the test is "is it
    overwritten AFTER deep_merge", not a list of key names) — a tenant's own list
    **adds to** the inherited platform/domain policy recipes rather than replacing them
    (`describe_tenant.py` overwrites that key after deep_merge). ⛔ **This is Python-only**:
    Go has no such path and still does REPLACE, so the two implementations' `effective` are
    different sets (see Known reachable exceptions, 2, and #1549).
- **Scalar fields**: child overrides parent
- **Null values — per-field, not a blanket rule** (#1339 split what used to be one line):
  - **The four routing fields** (`group_by` / `group_wait` / `group_interval` /
    `repeat_interval` under `_routing`): an explicit `null` **opts out of
    inheritance** and the generated route omits the field. These fields have no
    `"disable"` sentinel, so removing the value is the only way to say it.
    ⚠️ But `null` is **not the only spelling**: all four are falsy checks (`if val:` in
    `_grar_merge.py` for the three timing fields, `if group_by and isinstance(group_by,
    list)` in `_grar_routes.py` for `group_by`), so `""` / `0` / `[]` omit the field just
    the same. (Also note `group_by` is not a timing field.)
    `_routing.receiver` and `_routing.overrides` are **excluded**: the former
    makes the tenant's entire route disappear (alerts fall through to the
    catch-all), the latter has nothing above it to opt out of.
  - **Threshold keys**: an explicit `null` does **not** opt out — use
    `"disable"`. The emitting path (`collector.go` → `ResolveAtWithStats`)
    already ignores the null and falls back to the platform default; the
    diagnostic path (`/effective`, `describe_tenant`, simulate) was aligned to
    it in #1339.
  - **Every other `_`-prefixed reserved key**: an explicit `null` **does opt out** —
    `deepMerge` in `pkg/config/hierarchy.go` runs `delete(result, k)` for an explicit
    null on any `_`-prefixed key, while a non-`_` key (i.e. a threshold) only
    `continue`s. That code names THIS ADR as the authority for the rule, so the rule
    is stated here: **the test is "is it `_`-prefixed", not "is it a routing field".**
    ⛔ **But this only works on the `_defaults.yaml` side.** `tenant-config.schema.json`
    declares non-null types for `_silent_mode` / `_profile` / `_severity_dedup` /
    `_namespaces` / `_custom_alerts`, so writing `null` in a **tenant** file is rejected by
    `check_confd_schema.py`; `platform-defaults.schema.json` leaves those keys loose, so only
    a `_defaults.yaml` — at its top level or inside `defaults:` — is admitted (the `defaults`
    sub-schema says verbatim that its interior values are "left loose"). The reachable path is
    defaults-file to defaults-file.
    ⛔ **And the inherited value and the `null` must sit in the SAME position**: a `_`-prefixed
    key written as a **sibling** of `defaults:` never enters `effective` in the wrapped shape
    (see "three things", item 1), so there is nothing to delete — writing it is a silent no-op.
    The combinations that actually work are "both inside `defaults:`" or "both at the top level
    of a wrapper-less file".
- **⚠️ A blank value and an explicit `null` are the same thing** — listing them
  as "Null / empty values" was itself the misleading part: `mysql_connections: ~`
  and `mysql_connections:` are different syntax that YAML parses to the **same
  null**. Honouring null on a threshold key would therefore make "I stopped
  typing halfway" mean "silently switch this alert off". That is why it is
  unsupported there: an accident must fail **loud**, never **silent**.
- **`_metadata` fields do not inherit**: each tenant's `_metadata` comes only from its own YAML + path inference (ADR-016)

```yaml
# L0 _defaults.yaml
defaults:
  pg_stat_activity_count: 500
  pg_replication_lag_seconds: 30

# ↓ top-level key, a sibling of `defaults:` — NOT nested under it.
#   `defaults:` is map[string]float64; any nested mapping inside it fails the
#   unmarshal for the WHOLE file, so EVERY default is dropped — not just the
#   offending key. Both consumers log an ERROR (the exporter via
#   parsePartialConfig, which also bumps parse_failure; tenant-api via
#   merge_tenant.go), and neither applies any defaults.
_routing_defaults:
  group_wait: "60s"
  group_interval: "5m"

# L1 finance/_defaults.yaml
defaults:
  pg_stat_activity_count: 200     # override: finance domain is stricter
  pg_locks_count: 100             # addition: domain-specific

# tenant YAML
tenants:
  fin-db-001:
    pg_stat_activity_count: "150" # override: single tenant is strictest
                                  # ⚠️ quoted here, unquoted under `defaults:` above —
                                  # tenant values are ScheduledValue (string | object),
                                  # platform defaults are map[string]float64
    # pg_replication_lag_seconds: inherited from L0 = 30
    # pg_locks_count: inherited from L1 = 100
    # _routing_defaults.group_wait: inherited by the four-layer routing engine = 60s
    #   ⛔ but NOT part of the effective config below — see the scope note that follows
```

**Effective config computation**:

```
effective = deep_merge( defaults_block(L0), …, defaults_block(Ln), tenant_body )

  where defaults_block(f) = f["defaults"]
        ⤷ falls back to f itself (the WHOLE document) when that key is absent,
          or present with a null value (see exceptions 1 and 3 below)
```

The two unwrap implementations: `ddata.get("defaults", ddata)` in `describe_tenant.py`, and
`pkg/config.ExtractDefaultsBlock` in Go (implemented by the unexported `extractDefaultsBlock`
in the same package;
`config_inheritance.go` holds a same-named thin wrapper that just forwards to it — not a second
implementation).

### If you are editing a `_defaults.yaml`, these four things

1. **Only keys inside the `defaults:` block enter `effective`.** The top-level keys that sit
   **alongside** it (`state_filters` / `optional_overrides` / `profiles` /
   `max_metrics_per_tenant` / `_routing*` and so on) **still take effect** — they just travel
   their own pipelines: `state_filters` / `optional_overrides` are merged into `ThresholdConfig`
   by `pkg/config` and expanded per tenant at resolve time, while `_routing_defaults` /
   `_routing_enforced` are consumed by the four-layer merge behind
   `generate_alertmanager_routes.py` (implemented in the `_grar_*` modules it imports). They
   never pass through `effective` / `merged_hash`.
   ⛔ **Which keys may appear is defined by
   [`platform-defaults.schema.json`](../schemas/platform-defaults.schema.json) — but that is a
   list of "which top-level keys this file allows", NOT a list of "put the key here and it will
   take effect".** `_silent_mode` / `_profile` / `_severity_dedup` are all listed at that
   schema's top level, yet the place they are actually consumed is the **`tenants:` block**
   inside `_defaults.yaml`; writing them at the top level, alongside `defaults:`, is a **silent
   no-op** (measured: zero WARN from the exporter, `merged_hash` unchanged, schema lint returns
   `OK`).
   ⛔ Treat that schema as the source of truth for permitted keys and **do not start a second
   enumeration in this ADR** — the names in parentheses above are examples, not a list.

2. ⛔ **Do not indent sibling keys INTO `defaults:` to "make them visible". Three planes fail
   differently, and one of them stops a customer receiving alerts**:
   - **exporter**: the field is typed `map[string]float64` (`types.go`). A **non-scalar**
     sibling (mapping / list) fails to unmarshal ⇒ `parsePartialConfig` returns `ok=false`,
     **the entire file is dropped**, and it logs `ERROR: ... entire block dropped`, taking every
     other sibling key in that file with it.
     ⛔ **The line is not "scalar vs non-scalar" — it is "does it parse as `float64`"**:
     measured, `100`, `1.5` and **`null` (which becomes 0)** all give `ok=true` and an **empty
     log**, while `_profile: standard`, a quoted `"100"` and `true` — all scalars too — take the
     loud failure path above.
     ⛔ The real damage on the silent side is that **the sibling key itself stops working**:
     once `max_metrics_per_tenant` leaves the top level, `ThresholdConfig.MaxMetricsPerTenant`
     is 0 (measured) and the runtime falls back to the built-in
     `DefaultMaxMetricsPerTenant = 500` (`resolve.go`, on `== 0`) ⇒ **the per-tenant
     cardinality cap you wrote is silently replaced by 500**. ⚠️ The direction depends on what
     you wrote: 100 widens it 5×; **anyone who wrote more than 500 is silently tightened, and
     the excess metrics are truncated**.
     ⚠️ The side effect is one extra
     `user_threshold{component="max", metric="metrics_per_tenant"}` series per tenant. **Any
     query that does not name `metric=` will select it** — ready examples in this repo are
     Grafana's `count(user_threshold)`, `count by(component) (user_threshold)` and
     `label_values(user_threshold, metric)`. ⛔ **This document deliberately does not enumerate
     who consumes it**: that list spans rule packs, dashboards, recording rules and operator
     manifests, and any enumeration here would drift. To find out for **your** environment, run
     `count by(component) (user_threshold)` against the live Prometheus — an extra
     `component="max"` bucket is it.
   - **routing**: ⛔ once `_routing_defaults` leaves the top level,
     `generate_alertmanager_routes.py` cannot find it — **a tenant with no `_routing` of its own
     loses its entire route AND receiver** (measured: `Found 2 tenant(s) with routing config:
     db-a, db-b` → `Found 1 tenant(s) with routing config: db-b`, the `tenant-db-a` receiver
     gone, with **RC=0, zero
     errors, zero warnings**). `check_confd_schema.py` does **not** block it either (measured:
     `RC=0` both ways; the `defaults` sub-schema says verbatim that its values are left loose).
   - **`effective`**: **silently accepted** as a nested key — and blast-radius therefore goes
     from "no changes" to a Tier B report. ⚠️ **In other words: the diagnostic surface rewards
     this action while it takes a tenant's alerting away.**

3. **After changing a sibling key, do not use `merged_hash` / `/effective` / `blast_radius` to
   confirm it took effect** (those three cannot see it, and the runtime labels it
   `effect="cosmetic"` — see "The diagnostic cost" below). **Ask the real consumer instead**:
   - `state_filters` → `user_state_filter{tenant,filter,severity}` on the exporter's `/metrics`
   - `_silent_mode` (the one under the **`tenants:` block** of `_defaults.yaml`; written at the
     top level alongside `defaults:` it is the silent no-op from item 1) →
     `user_silent_mode{tenant,target_severity}`
   - `_routing_defaults` / `_routing_enforced` → `generate_alertmanager_routes.py --config-dir
     conf.d/ --dry-run`, and **diff the full before/after output**. ⛔ The `Found N tenant(s)
     with routing config` line and the receiver set are **not enough, and their blind spots
     differ**:
     - The `Found N` line counts **how many tenants parsed a routing config**, not how many
       routes came out. Measured: dropping `_routing_defaults.receiver.type` makes db-a's route
       AND receiver **both disappear** (`2 route(s), 2 receiver(s)` → `1, 1`) while that line
       stays byte-identical. ⚠️ This one does emit
       `WARN: db-a: missing required 'receiver.type', skipping` — unlike the genuinely
       signal-free indenting in item 2; do not conflate the two.
     - The receiver set only reacts to a receiver appearing or disappearing; it is blind to
       **values**. Measured with `_routing_defaults.group_wait` 30s→35s, and again with
       `receiver.to` changed to **a recipient on a different domain** — both times the full
       output differed by that one line while the `Found N` line and the receiver set did not
       move. ⚠️ So the blind spot covers **where the notification goes**, not just timing
       parameters; and `group_wait` is the very key used in the inheritance example above.

4. **To opt out of an inherited `_`-prefixed key with an explicit `null`, the test is "after the
   unwrap, do the two land on the SAME key path".** Which keys qualify is decided by the prefix
   (`deepMerge` in `pkg/config/hierarchy.go` runs `delete(result, k)` for `_`-prefixed keys,
   while a non-`_` key only `continue`s). **Position** does not require the two files to have the
   same shape, because `defaults_block(f)` is applied **per file**. Measured, four arms with a
   control:

   | Parent | Where the child's `null` sits | Result |
   |:--|:--|:--|
   | wrapped | (no child file — control) | kept |
   | wrapped | **sibling** of the child's `defaults:` | ⛔ **kept = silent no-op** |
   | wrapped | **inside** the child's `defaults:` | deleted ✅ |
   | wrapped | **top level of a wrapper-less child** | deleted ✅ |

   ⛔ The only combination that does nothing is "sibling of `defaults:` within the same file,
   **while `defaults:` is a real mapping**" — there is nothing there to delete. ⚠️ When
   `defaults:` is **absent** both implementations fall back to the whole document and that
   position becomes effective; when `defaults:` is **null** only Go falls back (measured: the
   sibling-position `null` does delete), while Python's `ddata.get("defaults", ddata)` returns
   `None` ⇒ `describe_tenant` crashes outright (see Known reachable exceptions, 1 and 3).
   ⛔ Writing it in a **tenant file whose name does not start with `_`** is always rejected by
   `check_confd_schema.py` (`definitions/tenantConfig` in `tenant-config.schema.json`
   declares non-null types for the named keys, and the rest of `_*` are caught by the
   `additionalProperties` `oneOf` catch-all — both paths measured at `RC=1`);
   writing it under the **`tenants:` block of a `_defaults.yaml` is NOT rejected** (measured
   `RC=0`) — and item 1 points the reader at exactly that position.

### Known reachable exceptions (NOT exhaustive — this list is not a guarantee)

1. **A file with no `defaults:` key** merges the **entire document** into `effective`, siblings
   included. ⚠️ The schema only admits this when **every** top-level key is on the whitelist
   (`additionalProperties: false` + 15 fixed properties + `^_state_` / `^_routing`
   patternProperties), so "drop the `defaults:` wrapper and write bare threshold keys" is in
   fact rejected by `check_confd_schema.py`. The shape exists in this repo
   (`rule-packs/recipes/examples/conf.d/finance/_defaults.yaml`, whose only top-level key is
   `_custom_alerts`). ⛔ **This document does not state how many such files there are** — that
   number drifts. To inventory your own tree, load every `_defaults*.y*ml` with `yaml.safe_load`
   and keep the ones whose result **is a dict and has no `defaults` key** (⚠️ a comments-only
   file parses to `None` — exclude it, it merges nothing, and counting it overstates the set). ⇒ This shape being invisible to the reachability gate is the subject of
   [#1552](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1552).

2. **`_custom_alerts` is injected by ADR-024's UNION resolver AFTER the unwrap**
   (`describe_tenant.py`, #772), so it **enters `effective` even when the `defaults:` wrapper is
   present**; Go has no such injection path. Measured on identical input: Python yields
   `{cpu_usage, _custom_alerts, _custom_alerts_resolution}`, Go yields only `{cpu_usage}` ⇒
   **the two implementations' `effective` are different sets, so `merged_hash` differs** ⇒
   [#1549](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1549).

3. **`defaults:` present but explicitly `null`** (the schema's `type: ["object","null"]` permits
   it; `check_confd_schema.py` was measured to admit it): Go's type assertion
   `m["defaults"].(map[string]any)` fails ⇒ it falls through and merges the entire document;
   Python's `ddata.get("defaults", ddata)` returns `None` for a key that exists with a null
   value (**not** the fallback) ⇒ `deep_merge` raises `AttributeError` and `describe_tenant`
   crashes outright. ⇒ Not yet ticketed.

⛔ Every `_defaults.yaml` under `tests/golden/fixtures` currently has the "wrapped, zero
siblings" shape, so the **golden parity suite structurally cannot detect any of these
exceptions** — do not read its green as an endorsement that the two implementations are
equivalent.

### The diagnostic cost (deliberately accepted)

Platform-level changes are structurally invisible to consumers that take `effective_config` /
`merged_hash` as input. ⛔ **What follows is the currently-known set, not an exhaustive one** —
the first version of this list missed `tenant-verify` (below), and the set spans Go, Python and
the Portal. To inventory it in **your** tree, search the non-test hits of
`merged_hash|MergedHash|ResolveEffective|EffectiveConfig`. Known today: `GET /effective`,
`describe_tenant`, `blast_radius`, the what-if preview (`handler_simulate.go`, Portal
`simulate-preview.jsx`), and `da-guard`. Two of those are worse than merely blind:

⛔ **At runtime the change is MISLABELLED, not just missed.** `parseDefaultsBytes` in
`config_defaults_diff.go` goes through the same unwrap, so `classifyDefaultsNoOpEffect` never
sees sibling keys — a real "changed platform severity AND changed routing" edit produces a
**byte-identical** `effect="cosmetic"` to "added one comment line". An SRE seeing
`blast_radius{effect="cosmetic"}` will read it as "just a comment change".

⛔ **`da-tools tenant-verify --expect-merged-hash` is a gate, not a diagnostic** (the blocking
signal in the rollback checklist, exit 2 = mismatch): after a platform-plane rollback it returns
exit 0, and **that means "this plane was not covered", not "the rollback is verified"**.

⇒ This is the subject of
[#1516](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1516); the remedy is a
**separate platform-plane comparison**. For why the domain here is not simply widened, see
**§Alternatives Considered, D** in this document.

### Dual-Hash Mechanism

Each tenant maintains two hashes:

| Hash | Definition | Purpose |
|:-----|:-----------|:--------|
| `source_hash` | SHA-256 of tenant YAML file bytes, **truncated to the first 16 hex chars** | Detect tenant source file changes |
| `merged_hash` | SHA-256 of effective config (canonical JSON after merge), **truncated to the first 16 hex chars** | Detect actual effective config changes |

⛔ **Both are 16 chars, not the full 64-char digest.** The scanner's internal
`m.hierarchy.hashes` stores the **untruncated 64-char** SHA-256 — the *same* digest; the first
16 chars of the tenant-file entry are exactly the `source_hash` above. ⛔ But `merged_hash`
hashes the canonical JSON, **not** file bytes, so running `sha256sum` on any file will never
match `--expect-merged-hash`.

**Reload decision logic**:

```
if source_hash changed:
    recompute effective config → update merged_hash
    record as applied (reason=source; reason=new if the tenant is newly seen)
    ⚠️ this branch does NOT compare merged_hash — see the note below
elif any ancestor _defaults.yaml changed:
    recompute effective config → update merged_hash
    if merged_hash changed:
        record as applied (reason=defaults)
    else:
        record as shadowed / cosmetic (see §Amendment 2026-04-25)
```

⚠️ **This pseudocode describes how a change is CLASSIFIED, not whether a rebuild
happens.** In the implementation, the hierarchical path's `diffAndReload` calls
`installNewHierarchyState` **unconditionally** after `classifyAndCount`, and the first thing
that function does is run `fullDirLoad` unconditionally. `merged_hash` decides whether the
change is recorded as `applied` (`IncReloadTrigger`) or as `shadowed` / `cosmetic` — it does
**not decide the rebuild**.

⛔ **The `source_hash` branch does not compare `merged_hash`**: the `if sourceChanged` path in
`config_debounce.go` records `applied` directly, and the `prev == mh` comparison appears only
under `else if defaultsChanged`. ⚠️ The consequence: **a comment-only edit to a tenant YAML is
also recorded as `applied`**, polluting the blast-radius high-impact signal. That is a known
gap between the implementation and this ADR's intent, not a deliberate design.

### Inheritance Graph Data Structure

The Scanner maintains an **inheritance graph**:

```go
type InheritanceGraph struct {
    // _defaults.yaml path → affected tenant ID list
    DefaultsToTenants map[string][]string
    // tenant ID → its inheritance chain _defaults.yaml paths (ordered, L0→L3)
    TenantDefaults    map[string][]string
}
```

⚠️ **`DefaultsToTenants` / `TenantsAffectedBy` currently have no production consumer** (the
only readers are the accessor in `inheritance_graph.go` itself, plus tests). The actual reload path, `classifyAndCount`, iterates over every scanned tenant, uses
the **reverse** map `TenantDefaults[tid]`, and decides whether that tenant needs recomputing by
comparing **per-file SHA-256 values** (`scan.hashes` vs `prior.hashes`, covering the tenant file
and its whole defaults chain); when nothing moved it reuses the previous round's cached
`merged_hash`. ⛔ The `merged_hash` comparison itself (`prev == mh`) happens **after** the
recompute — its right operand IS the recompute's output — so it **cannot save any recompute**;
it only decides whether the change is classified `applied` or `shadowed` / `cosmetic`.
"Avoiding full recalculation" is achieved by that **file-hash** comparison, not by this forward
map. The map is retained as existing structure; changing it does not change
behaviour.

### Watch Mechanism: Maintain Periodic Scan

- **Do not adopt inotify/fsnotify**: container mount event loss + kernel watch limits
- Maintain existing periodic scan (configurable interval, default 30s)
- ⚠️ **"Only recalculate hashes for files whose `stat()` changed" is NOT implemented**: the
  `priorMtimes` parameter of `scanDirHierarchical` is currently ignored (`config_hierarchy.go`
  says verbatim `_ = priorMtimes // reserved for Phase 3`), and every file walked is hashed
  unconditionally with `sha256.Sum256`. The benchmark numbers are the cost of hashing everything.

### Debounce

- When `git pull` lands 50 files, each `stat()` change does not immediately trigger reload
- Debounce window: **300ms** (configurable via `--scan-debounce` flag)
- Window accumulates all changes → batch recompute → single reload pass
- Prevents reload storms (50 tenant reloads → becomes 1 batch reload)

### Cardinality Guard

- `_defaults.yaml` **does not produce Prometheus metric series**
- Inherited fields still follow existing Cardinality Guard rules (v2.5.0 ADR-005)
- `merged_hash` label is not exposed in metrics (prevents label explosion)

### New Prometheus Metrics

| Metric | Type | Labels | Description |
|:-------|:-----|:-------|:------------|
| `da_config_scan_duration_seconds` | histogram | — | Single periodic scan duration |
| `da_config_reload_trigger_total` | counter | `reason` | Reload reason. **Only four values are actually emitted**: source / defaults / new / delete (all from `classifyAndCount`, hierarchical mode only). ⚠️ `config_metrics.go` lists `forced` in the declared domain, but **no production path ever uses it as a label**: `ReloadReasonForced` is returned by `detectChange()` in hierarchical mode (**not** the manual / SIGHUP trigger its constant comment describes) and only flows into the debouncer's `pendingReasons`, whose length alone feeds `da_config_debounce_batch`. The effective domain of `blast_radius`'s `reason` below is the **same** |
| `da_config_defaults_change_noop_total` | counter | — | **Classification** count: a defaults change whose merged_hash did not move. ⚠️ **Not "rebuilds saved"** — the rebuild runs unconditionally (see the note under §Reload decision logic) — **v2.8.0 narrows the semantics to cosmetic-only** (see Amendment 2026-04-25) |
| `da_config_defaults_shadowed_total` | counter | — | **v2.8.0 (Issue #61)** — Defaults change blocked by tenant override (split out from `da_config_defaults_change_noop_total`) |
| `da_config_blast_radius_tenants_affected` | histogram | `reason / scope / effect` | **v2.8.0 (Issue #61)** — Per-tick distribution of affected tenants |

### Amendment 2026-04-25 (Issue #61): noop semantic split

The original §Reload logic conflated "comment-only edit" with "override-shadowed edit" under `da_config_defaults_change_noop_total`, leaving ops unable to distinguish "truly no impact" from "inheritance system blocked the change". v2.8.0 splits this by `effect`:

```
elif any ancestor _defaults.yaml changed:
    recompute effective config → update merged_hash
    if merged_hash changed:
        record as applied (IncReloadTrigger(reason=defaults))
        emit blast_radius{effect="applied"}
    else:
        # Further classification (Issue #61)
        compute changedKeys = diff(prior_parsed_defaults, new_parsed_defaults)
        if len(changedKeys) == 0:
            # Pure cosmetic: comment-only / reordering / whitespace
            increment da_config_defaults_change_noop_total
            emit blast_radius{effect="cosmetic"}
        elif tenantOverridesAll(tenant_src, changedKeys):
            # Shadowed: tenant overrides every changed key
            increment da_config_defaults_shadowed_total
            emit blast_radius{effect="shadowed"}
        else:
            # Logically unreachable (merged_hash should have moved)
            # — defensive fallback to cosmetic
            increment da_config_defaults_change_noop_total
```

Implementation notes:
- `m.hierarchy.parsedDefaults` and `m.hierarchy.hashes` (folded into the `hierarchyState` sub-struct as of v2.8.0), atomic-swapped together, caching the normalized parsed dict (`map[string]any`) of every `_defaults.yaml`. ~1 MB at 1000 tenants.
- `populateHierarchyState` eager-parses every defaults file at cold start; `diffAndReload` only re-parses files whose hash actually moved, reusing the prior parse otherwise.
- See `components/threshold-exporter/app/config_defaults_diff.go` and Issue #61 RFC.

## Alternatives Considered

### A: Single-Hash (source_hash only)

❌ Cannot determine which tenants are actually affected when `_defaults.yaml` changes,
forcing full reload. Reload storms are unacceptable in 1000+ tenant environments.

### B: fsnotify / inotify

❌ Event loss in container mounts (NFS/FUSE/projected volume) is a known issue.
Kernel watch limits (default 8192) are exhausted in thousand-tenant environments.
Measured cost of the periodic scan is in
[`benchmarks.en.md` §1](../benchmarks.en.md#1-scale-how-many-tenants): at **1000 tenants**, a cold full
load takes **112 ms** and a steady-state reload **1.3 ms**. ⚠️ The "< 200ms for 2000 tenants"
figure in this ADR's earlier text has no locatable source in the repo and has been replaced
with the numbers that can actually be checked.

### C: Array Concat (Instead of Replace)

❌ `group_by: [severity]` (L0) + `group_by: [alertname]` (L1)
→ concat result `[severity, alertname]` has unclear semantics.
Users expect "I overrode group_by" not "I appended to it."
Replace semantics are more intuitive and consistent with Helm values merge behavior.

### D: Widen the domain of `effective` (merge the siblings in) — REJECTED

❌ This is the intuitive fix proposed in
[#1516](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1516): since
platform-level changes are invisible on the diagnostic surface, merge the sibling keys
(`state_filters` / `_routing_defaults` and friends) into `effective` too. **Three reasons,
equally load-bearing, all measured**:

1. **It would manufacture reload-attribution noise.** `merged_hash` is the input to reload
   **attribution** and to the blast-radius signal: `classifyTenant` in `config_debounce.go`
   uses it to decide whether
   a defaults change is recorded as `applied` (`IncReloadTrigger`) or as `shadowed` /
   `cosmetic`. Merging the siblings in would flip every tenant from `cosmetic` to `applied` on
   every platform routing edit, and increment
   `da_config_reload_trigger_total{reason="defaults"}` each time — while the reason this ADR
   exists is the question above, how to avoid a reload storm. **It shares its root with why
   alternative A was rejected (both live on the reload-attribution line), but the problem
   differs**: A's rejection reads verbatim "cannot tell which tenants are actually affected,
   so it can only reload everything. At 1000+ tenants a reload storm is unacceptable." The
   problem here is purely **attribution** — mislabelling **the tick that already happens** from
   `cosmetic` to `applied`. Both `defaultsChanged` branches of `classifyTenant` have already
   run `recomputeMergedHash`, and `installNewHierarchyState` runs unconditionally, so merging
   the siblings in changes the labels and counters, not the amount of loading.
   ⚠️ **Which surfaces a tension this ADR never states**: hierarchical mode **already runs
   `fullDirLoad` on every tick**. What dual-hash buys is **attribution**, not saved loading —
   §A's "can only reload everything" is stale as a description of today's implementation. ⚠️ Precisely: tenants are
   **already** fed into the `da_config_blast_radius_tenants_affected` histogram on every tick;
   what would change is the `effect` label and whether the counter increments, not whether they
   are recorded at all.

2. **It would invalidate every existing snapshot at once.** `merged_hash` is the comparison
   value for `da-tools tenant-verify --expect-merged-hash`; widening the domain makes all
   existing snapshots mismatch.

3. **deep_merge cannot express the semantics.** `_routing_defaults` is overridden by `_routing`
   — a **different key name** — as a top-level shallow overwrite (`merge_routing_with_defaults`
   in `_grar_merge.py`), not a same-key deep merge. Measured: one edit to the platform
   `_routing_defaults.group_wait` records all 5 tenants as affected, yet the one tenant carrying
   its own `_routing` sees **no change at all** to its actual route — that attribution is
   provably false.

**Two findings back this decision — the first is a measurement with a control that DID move**:

- Applying a sibling key does not depend on `merged_hash`. In the wrapped shape, changing only
  `state_filters.<filter>.severity` takes effect while `merged_hash` stays byte-identical; the
  control (changing a key under `defaults:` that the tenant does not override) does move the
  hash. ⚠️ In the wrapper-less shape the same edit moves **every** tenant's hash (see Known
  reachable exceptions, 1).
- Neither load path is gated on `merged_hash`: in flat mode any change to a `_`-prefixed file
  takes the full-rebuild path (`isTenantOnlyChange` in `config.go`), and in hierarchical mode
  `installNewHierarchyState` runs `fullDirLoad` every time. ⚠️ The flat path does have a
  composite-hash (one per directory) no-op fast path that returns early
  (`compositeHash == prevHash` in `config.go`) — that is a **different hash**.

⚠️ **The `_routing` family's status toward the exporter is asymmetric and worth recording**:
`_routing_defaults` has no corresponding `ThresholdConfig` field and is dropped at decode time,
whereas tenant-level `_routing` **is** loaded into `ThresholdConfig.Tenants` (`resolveBaseRows`
has to skip the `_routing*` prefix explicitly precisely because it sits in that map) — but **no
production caller consumes it**: `ResolveRouting()` is called only from tests, and `types.go`
states verbatim that it "is currently not called by the exporter", retained as a guardrail
reference implementation. It does have two real consumers in the repo: the four-layer merge in
`generate_alertmanager_routes.py`, and `cmd/da-guard`, which reads `EffectiveConfig["_routing"]`
to build `RoutingByTenant`.

## Consequences

- **Directory Scanner Go code**: New inheritance graph + dual-hash + debounce logic
- **CLI**: New `describe-tenant` command expands effective config + shows inheritance sources
- **Tenant API**: New `GET /api/v1/tenants/{id}/effective` endpoint
- **Schema**: new `platform-defaults.schema.json` for `_defaults*.yaml`. ⚠️ **Not** an upgrade to `tenant-config.schema.json` — that file's root has only `tenants` with `additionalProperties: false` and structurally cannot express platform defaults; the routing lives in `check_confd_schema.py`
- **Benchmark**: Thousand-tenant + multi-layer inheritance scan performance compared against the v2.7.0 planning baseline (validated)

## Related

- [ADR-016: conf.d/ Directory Hierarchy + Mixed Mode](016-conf-d-directory-hierarchy-mixed-mode.en.md)
- [Benchmark Report §1 Scale](../benchmarks.en.md#1-scale-how-many-tenants) — dual-hash 1000-tenant measurements + SLO interpretation
- [architecture-and-design.md §Design Concepts](../architecture-and-design.md#設計概念總覽)
