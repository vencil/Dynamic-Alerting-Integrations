---
title: "VictoriaMetrics Integration Guide"
tags: [integration, victoriametrics, vmagent, vmalert, metricsql]
audience: [platform-engineers, sre, vm-operators]
version: v2.9.0
lang: en
---

# VictoriaMetrics Integration Guide

> This document is the **centralized entry point** — consolidating VM-related content scattered across [`cli-reference.md`](../cli-reference.en.md), [`byo-prometheus-integration.md`](byo-prometheus-integration.en.md), [`scenarios/multi-system-migration-playbook.md`](../scenarios/multi-system-migration-playbook.md) (**ZH only — [#409](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/409)**), and [`design/roadmap-future.md`](../design/roadmap-future.md) into a single clear path. **Does not duplicate existing doc content**; provides navigation + VM-specific gotchas + alignment with the anti-vendor-lock-in commitment.

---

## 1. Which type of VM customer are you? (Decision Tree)

```mermaid
flowchart TD
    Q1{Current situation?}
    Q1 -->|Already have vmagent/vmalert,<br/>just want to use our Rule Pack| A[§4 Rule Pack on vmalert]
    Q1 -->|Need to migrate existing customer<br/>PromRule into our conf.d structure| B[§5 Migration via da-parser]
    Q1 -->|Complete swap:<br/>Prom→VM + rules + AM + metric-split| C["§6 Multi-System Migration<br/>(dedicated playbook)"]
    Q1 -->|Just evaluating compatibility| D[§3 Supported Architectures]

    style A fill:#e8f4f8
    style B fill:#fff8e1
    style C fill:#dff
    style D fill:#f5f5f5
```

**Most common paths**: B (rule migration) and C (multi-system). A is "I already have a VM stack, just want your Rule Pack" — relatively rare but simple.

---

## 2. Why this document exists

VictoriaMetrics is a real customer-timeline requirement (landing in v2.8.0). But VM integration information has historically been scattered across 6+ files, forcing customers to stitch things together during onboarding. This document is the **index** — it doesn't duplicate content; every section links back to its source-of-truth doc.

---

## 3. Supported Architectures

Our official support boundaries for the VM ecosystem:

| Component | Our mapping | Support level | Detail |
|---|---|---|---|
| **vmagent** | Scrape source; customer-managed | ✅ Full | Same as vanilla Prom — the threshold-exporter `/metrics` endpoint can be read by any scraper |
| **vmsingle / vmcluster (vmstorage + vmselect + vminsert)** | Metric storage backend; threshold-exporter `remote_write` target | ✅ Full | This platform **does not replace** your VM; non-invasive integration (the design principle in [`byo-prometheus-integration.md`](byo-prometheus-integration.en.md) §1 applies equally to VM) |
| **vmalert** | Rule evaluator; can load our Rule Pack | ✅ Full | This platform's Rule Pack is pure standard PromQL; vmalert evaluates it directly (see [`byo-prometheus-integration.md`](byo-prometheus-integration.en.md)) |
| **vmauth** | Auth proxy; multi-tenant isolation front line | ⚠️ Documentation thin | The tenant-federation ADR ([issue #380](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/380)) will use vmauth for forced label injection; current federation scope is primarily platform-internal |
| **MetricsQL extensions** | Non-portable function detection | ✅ Full | `da-parser` `vm_only_functions.yaml` allowlist + freshness CI gate (details in [`cli-reference.md`](../cli-reference.en.md) §MetricsQL-as-Superset PromRule parser) |

---

## 4. Rule Pack on vmalert (simple scenario)

If the customer just wants to use our Rule Pack without replacing their existing VM stack:

```bash
# vmalert configuration example
vmalert \
  -datasource.url=http://vmstorage:8481/select/0/prometheus \
  -notifier.url=http://alertmanager:9093 \
  -remoteWrite.url=http://vminsert:8480/insert/0/prometheus \
  -rule=https://raw.githubusercontent.com/vencil/Dynamic-Alerting-Integrations/main/rule-packs/...
```

Key points:
- Our Rule Pack is pure standard PromQL → vmalert needs no compatibility layer
- The threshold metric `user_threshold{...}` is emitted by our threshold-exporter → vmagent scrapes it into VM
- **`-remoteWrite.url` is mandatory**: omit it and vmalert will still fire alerts to AM, but the `ALERTS{}` / `ALERTS_FOR_STATE{}` time-series data **will not be written back to VM Storage**. Consequences: (1) Grafana cannot render alert-state panels; (2) the future `multi-system-migration-playbook.md` Phase 0 Tier B "run `ALERTS{}` live snapshot against Prom/VM" mechanism will have no data to read and will silently fail.
- **da-parser is not needed** — da-parser is for *inbound* customer rule conversion, not outbound

Details: [`byo-prometheus-integration.md` §Advanced: Integration with Thanos / VictoriaMetrics](byo-prometheus-integration.en.md).

---

## 5. Migration via da-parser (rule migration)

Migrating a customer's **existing PromRule corpus** into our `conf.d/` structure:

### 5.1 Toolchain

```
customer PromRule YAML
    ↓ da-parser import
ParsedRule JSON (with dialect / vm_only / prom_portable annotations)
    ↓ da-tools profile build
Cluster + Profile-as-Directory-Default
    ↓ da-batchpr apply
Hierarchy-aware Batch PRs
    ↓ da-guard
Dangling Defaults Guard 4-layer check
    ↓
conf.d/ tree (GitOps merge)
```

### 5.2 MetricsQL handling and anti-vendor-lock-in

- **Dialect detection**: `da-parser import` tags each rule as `prom` / `metricsql` / `ambiguous`
- **`prom_portable: bool` flag**: identifies the subset that "also runs on vanilla Prom"
- **`vm_only_functions.yaml` allowlist**: lists MetricsQL-exclusive functions (e.g., `histogram_quantile_bucket`, `increase_prometheus`), aligned with the [VM `metricsql` package](https://docs.victoriametrics.com/MetricsQL/)
- **CI freshness gate**: `vm_only_functions_freshness_test.go` automatically detects new functions when MetricsQL upgrades, avoiding silent misses

Detailed CLI + JSON ParseResult schema: [`cli-reference.md` §MetricsQL-as-Superset PromRule parser](../cli-reference.en.md) (L2320-2397).

### 5.3 Anti-vendor-lock-in commitment

When `da-parser import --fail-on-non-portable` runs fully green against a corpus → that corpus **also** evaluates on vanilla Prometheus. The customer is not locked into VM by us.

---

## 6. Multi-System Migration (VM + rules + AM swapped together)

If the customer's situation is a "**complete swap**" — replacing storage backend (Prom→VM) **plus** the rule layer **plus** AM routing **plus** enabling `_defaults.yaml` metric-split — this is beyond the scope of this guide:

→ Follow the 5-Phase model in [`scenarios/multi-system-migration-playbook.md`](../scenarios/multi-system-migration-playbook.md) (**ZH only — [#409](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/409)**).

That playbook assumes "mature multi-system ops" and covers Phase 0 three-tier discovery, the Plan A vs Plan B Git layout trade-off, the 5 Gate invariants, and the three-tier rollback reversibility boundary. **The decision tree at the playbook's start re-routes once more** — you'll be directed to the appropriate section.

---

## 7. Known gaps / Future work

| Item | Current state | Roadmap |
|---|---|---|
| **MetricsQL → PromQL auto-conversion tool** | Does not exist | Currently only dialect detection + portability tagging; conversion is manual. Will be evaluated for v2.9 backlog if customers request it |
| **vmauth-based tenant federation** | Design phase | See [issue #380](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/380) (v2.9 epic) — ADR uses vmauth + label-enforced rewriting + 4h TTL token |
| **vmalert-specific shadow monitoring** | The `migration_status: shadow` label mechanism in [`shadow-monitoring-sop.md`](../shadow-monitoring-sop.en.md) is sufficient | vmalert also supports `migration_status` matchers → no VM-specific documentation needed |
| **VM-optimized rule pack variants** | Does not exist | Our Rule Pack is pure PromQL; in theory a new pack could be opened for MetricsQL performance optimization (e.g. `histogram_quantile_bucket`); no customer signal currently |

---

## 8. Cross-references

| Topic | Document |
|---|---|
| **Design philosophy**: non-invasive, Rule Pack pure PromQL | [`byo-prometheus-integration.md` §1](byo-prometheus-integration.en.md) |
| **vmalert load Rule Pack**: implementation details | [`byo-prometheus-integration.md` §Advanced](byo-prometheus-integration.en.md) |
| **da-parser MetricsQL handling**: CLI + JSON spec | [`cli-reference.md` §MetricsQL-as-Superset](../cli-reference.en.md) |
| **Multi-system migration** (Prom→VM + rules + AM simultaneously) | [`multi-system-migration-playbook.md`](../scenarios/multi-system-migration-playbook.md) (**ZH only — [#409](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/409)**) |
| **Federation design** (platform-internal multi-cluster) | [`federation-integration.md`](federation-integration.en.md) |
| **Tenant federation** (pulling metrics back to the customer side) | [issue #380](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/380) (v2.9 epic) |
| **MetricsQL spec** | [VictoriaMetrics official documentation](https://docs.victoriametrics.com/MetricsQL/) |

---

## 9. Quick Start checklist

After finding your path via the §1 decision tree:

<details>
<summary>📋 Path A — Rule Pack on existing vmalert (simplest)</summary>

- [ ] Verify that vmalert configuration can load a GitHub raw URL or locally-mounted Rule Pack YAML
- [ ] Deploy threshold-exporter ([helm/threshold-exporter/](https://github.com/vencil/Dynamic-Alerting-Integrations/tree/main/helm/threshold-exporter))
- [ ] Confirm vmagent scrapes the threshold-exporter `/metrics`
- [ ] Observe `user_threshold{...}` metric appearing in VM
- [ ] After vmalert starts, check that alerts trigger

</details>

<details>
<summary>📋 Path B — Migration via da-parser</summary>

- [ ] Run `da-parser import` against the customer PromRule corpus
- [ ] Check dialect distribution + non-portable ratio
- [ ] Run `da-tools profile build` to extract cluster + Profile-as-Directory-Default
- [ ] Run `da-batchpr apply` to open Base + tenant chunk PRs
- [ ] Run `da-guard` through the 4-layer schema / routing / cardinality / redundant-override check
- [ ] Details → [Migration Toolkit Installation](../migration-toolkit-installation.en.md)

</details>

<details>
<summary>📋 Path C — Multi-System Migration</summary>

→ Go directly to [`multi-system-migration-playbook.md`](../scenarios/multi-system-migration-playbook.md) (**ZH only — [#409](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/409)**); not duplicated here.

</details>
