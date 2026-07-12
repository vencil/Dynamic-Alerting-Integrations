---
title: "Staged Rule Adoption Lifecycle"
tags: [scenarios, lifecycle, custom-rules, golden-rules, rule-packs]
audience: [platform-engineers, sre, tenant-admins]
version: v2.9.0
lang: en
---

# Staged Rule Adoption Lifecycle

> **This is not "the final step of migration"** — it's a **rule curation pattern** that runs through the entire customer lifecycle: every time new custom rules are introduced, every time the platform ships a new Rule Pack, every time a new team takes over a tenant.
>
> **Three scenarios it applies to** (detailed in §7):
> 1. **Initial migration** — progressive consolidation after a large initial import of `custom_*` rules
> 2. **New tenant onboarding** — a new team comes online six months later, takes the same "custom_ first, golden later" path
> 3. **Rule Pack upgrade** — when the platform ships new golden rules, existing `custom_*` overrides are re-evaluated
>
> While curating rules, also review whether the **alert actions** they trigger (webhook automation) are safe to re-run → [Beyond Actionable](../alerting-best-practices.en.md).

---

## 1. The two states: `custom_*` vs golden

| State | Naming | Source | Trust model |
|---|---|---|---|
| **`custom_*`** | `custom_<domain>_<metric>` | Imported from the customer's existing system, or hand-written | Customer owns; platform takes no responsibility |
| **golden** | `<rule-pack-name>:<metric>` | Platform-curated Rule Pack | Platform owns; per-version SLA |

**Core motivation**: customers come from heterogeneous systems, and **rules are business logic** — they can't be force-stripped and rewritten. `custom_*` gives customers a buffer to bring their existing logic in; golden is the long-term target.

→ Full namespace rules: [`custom-rule-governance.md`](../custom-rule-governance.en.md).

---

## 2. Why staged, not big-bang

| Risk | Big-bang | Staged |
|---|---|---|
| **Missed rules** | One failed cutover blasts every tenant | One failed cutover only affects the current batch |
| **Semantic drift** | Subtle behaviour differences between golden and `custom_` all surface together | Each batch validates equivalence; differences are isolated |
| **Customer confidence** | "If something breaks after we cut everything over, we get paged" fear | First batch stabilises, then we push the next |
| **Rollback** | Reverting the whole batch via git affects a wide surface | Per-batch revert; blast radius small |
| **Knowledge transfer** | Customers have no time to internalise golden semantics | Each batch comes with its own mapping document |

**Best-practice from the SRE community**: every production cutover should have a *canary tier* + *observation period*. Staged adoption is that principle applied to the rule layer.

---

## 3. Three-tier reading speed

Each section (§4–§6) shares the same structure:

- **30-sec TL;DR** — for managers / cross-team broadcast
- **Decision principles** — for architects (the *why* of each decision)
- **Operator Checklist** (collapsed in `<details>`) — for on-call running a promotion

---

## 4. When to promote — decision criteria

### 30-sec TL;DR
- **Hard requirements**: ≥ 2 weeks in shadow phase, *Subset overlap = 100%*, no unexpected new alerts
- **Customer sign-off**: domain owner confirms golden semantics are equivalent to the existing `custom_`
- **Rollback path rehearsed**: customer ops is fluent with the `git revert` command

### Decision principles

**Hard requirements** (all must hold):

1. **Shadow-phase data evidence**: at least 2 weeks in shadow monitoring (multi-system migration playbook §5)
2. **Coverage gate (replaces "pure 100% overlap")**: pick **one** of the following and satisfy its sub-conditions:
   - **(2a) Subset overlap = 100%**: whatever `custom_` fires on, golden must fire on too (avoiding catastrophic false negatives) — applies when "golden is the equivalent or superset of `custom_`"
   - **(2b) Intentional noise reduction**: for **every** case where `custom_` fires but golden does not, all of:
     - The domain owner explicitly classifies it as an "intended noise filter" (not a bug)
     - The reviewer can articulate why golden's smarter logic (extra conditions / time window / threshold tuning) is correctly suppressing that alert
     - The corresponding "why this didn't fire this time" reasoning is written into the PR description (so future incident reviews can find it)
   - Strictly forbidden: "reduction because golden missed a catastrophic case" sneaking in under (2b). If in doubt, default to (2a).
3. **Extra alerts explicitly signed off**: alerts that golden fires but `custom_` doesn't are not treated as bugs — the customer confirms they're design intent

> **Design motivation** (countering the "100% overlap paradox"): hard-coding 100% overlap traps customers inside bad rules. Example: customer's old `custom_mysql_cpu` is `cpu > 80%` (firing 50× a day); the new golden is `cpu > 80% AND io_wait > 20% for 5m` (firing 5× a day, only when it should). Shadow-phase overlap will inevitably be below 100% — but this is **intended noise reduction**, not regression. Gate (2b) gives this legitimate path a home.

**Soft acceptance signals** (any of these is bonus reason to push forward):

- Domain owner (DBA / SRE / platform team) nods at golden's semantics
- Customer has an incident-response runbook that maps to golden's alert label schema
- Time exists for one ops cycle of observation (recommended: 1 week including weekly anomalies)

### Exceptions — when to deliberately keep `custom_`

- Customer has a proprietary metric source that isn't in the platform metric dictionary (e.g. a self-written business-KPI scrape exporter)
- Customer has a compliance requirement that alert text must contain specific fields (golden doesn't necessarily cover this)
- The domain is too niche to have a corresponding Rule Pack (e.g. customised IoT device monitoring)

→ Keeping `custom_` is not failure — it's a **stable equilibrium**. This guide's promotion does not force everything onto golden.

<details>
<summary>📋 Promotion gate checklist (for the executor)</summary>

- [ ] Shadow phase ≥ 2 weeks, no alert noise (`da-tools shadow-verify --window=14d`)
- [ ] **Coverage gate**, one of:
  - [ ] (2a) *Subset overlap = 100%* (`da-tools shadow-verify --check-subset-overlap`) — default strict path
  - [ ] (2b) *Intentional noise reduction* — when overlap is below 100%, each missing case in the PR description has the domain owner's "why this didn't fire" classification + the reviewer's reasoning
- [ ] List of extra alerts signed off by customer ops (PR description records the sign-off)
- [ ] Customer ops is fluent with `git revert <batch-commit>` rollback path
- [ ] Domain owner approval (Slack / email / PR review approval)
- [ ] Rollback rehearsal record retained for 30 days
</details>

---

## 5. Batch sizing — how much to push at once

### 30-sec TL;DR
- **Default = 1 domain × 1 region × canary tenant-group (5% of tenants)**
- The first wave is canary tenants only; expand to that region's full tenant set after
- For a 1000-tenant customer: ~10 batches × (canary + full) × 1 ops cycle = ~10–12 weeks
- Speed up = higher risk; if it's not life-saving, push slowly

### Decision principles

| Batch granularity | Rules × tenants | When | Risk |
|---|---|---|---|
| **Single rule × canary tenants** | 1 × 5%-tenants | High-risk / customer's first contact | Lowest risk; too slow |
| **1 domain × 1 region × canary tenants** (**default**) | 5–15 × 5%-tenants | First wave | Best balance |
| **1 domain × 1 region × full tenants** | 5–15 × 100%-tenants | After canary passes 1 ops cycle | Medium |
| **1 domain × all regions × full tenants** | 20–60 × 100% | Domain matured / 3+ incident-free batches | Higher (cross-region variables expand) |
| **Cross-domain bundle × full tenants** | 50+ × 100% | Rushing to wrap up, customer experienced | Large blast radius; not recommended |

**Why the default adds a canary-tenant dimension**:

- In a multi-tenant platform, "1 domain × 1 region" already means cutting all ~1000 tenants — blast radius is too large
- Cut 5% canary tenants first (pick high-tolerance internal / early-partner customers) → observe across 1 ops cycle → if clean, expand to full region
- Customers usually nominate some "dev / staging tenants" or "early-experience cohort" as the canary pool
- Domain isolation + Region isolation + Tenant isolation = **three-layer blast-radius fence**

### Canary tenant selection

- **Prefer**: customer's internal tenants, staging-only tenants, high-tolerance early customers
- **Avoid**: production-critical tenants, customer's highest-SLA-tier tenants, customers who just raised an alert
- **Quantity**: 5–10% is the sweet spot; below 5% the signal is too weak, above 10% the blast starts to matter

### Conditions for accelerating

- First canary batch passes 1 ops cycle cleanly → push to full-tenant tier
- 3 consecutive incident-free batches (canary + full) → can grow to 1 domain × all regions × full
- But **never** skip the canary stage. Acceleration is about scale, not about steps.

<details>
<summary>📋 Batch planning checklist</summary>

- [ ] Inventory: list all current `custom_*` rules + their promotable golden counterparts
- [ ] Group by `domain × region`
- [ ] Order: low-risk / signal-rich domains first (DB / network typically first; business-KPI later)
- [ ] **Designate the canary tenant pool**: negotiate with the customer for 5–10% high-tolerance tenants (internal / staging / early partners)
- [ ] Batch 1: smallest granularity (5–10 rules) × canary tenant pool — to build process confidence
- [ ] PR description contains the mapping table (`custom_` name → golden name) + canary tenant list
- [ ] Wait one ops cycle after canary before opening the full-tenant promotion PR
</details>

---

## 6. Observation period & rollback

### 30-sec TL;DR
- **At least 1 ops cycle per batch** (1 week recommended, covering weekly anomaly windows)
- If something goes wrong during observation: `git revert` the per-batch commit
- Monitoring state (already silenced alerts) needs manual cleanup (same as multi-system playbook §11 semi-reversible layer)

### Observation-period length

| Observation period | When | What it catches |
|---|---|---|
| **24h** | Minimum; canary-tenant tier | Smoke tests, obvious regressions |
| **1 ops cycle (typically 1 week)** | **Default** | Weekly batch jobs / weekly cron alerts |
| **1 month** | High-stakes domain (compliance / financial) | Monthly closing alerts, quarterly anomalies |

**What it can't catch**: rare events (quarter-end / big promo / Black Friday traffic) — sync the customer's calendar in advance and avoid sensitive windows.

### Rollback path

```bash
# Find the batch's commit
git log --grep="staged-adoption batch <N>" --oneline

# Revert
git revert <commit-sha>

# AM / exporter auto-reload
# Monitoring-state cleanup:
#   - Silenced alerts → manually unsilence (AM UI)
#   - Maintenance windows → manually close (tenant API)
```

**Rollback reversibility boundary** (same as [multi-system-migration-playbook §11](multi-system-migration-playbook.md#11-rollback-三層可逆界線) — **ZH only — [#409](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/409)**):

| Layer | Reversibility |
|---|---|
| Config (`custom_` rule rolled back to golden) | ✅ git revert |
| Monitoring state (already-silenced alerts) | ⚠️ manual cleanup |
| Data layer (already-ingested metrics) | ❌ accept it |

<details>
<summary>📋 Observation + rollback checklist</summary>

**Observation phase**
- [ ] Start counting 1 ops cycle from batch-promotion PR merge
- [ ] Daily check on alert volume + receiver delivery (`da-tools alert-quality --tenant=<batch-tenants>`)
- [ ] Smoke-check that the new alert label schema is compatible with the receiver
- [ ] Cover weekends / night shifts / month-end corner cases

**Rollback (if triggered)**
- [ ] Announce in Slack / on-call channel
- [ ] `git revert <batch-commit>` + push to the GitOps branch
- [ ] AM reload (auto via ArgoCD / Flux)
- [ ] Manually unsilence affected alerts
- [ ] Postmortem: which rule failed; where golden and `custom_` diverged on input
- [ ] Either fix golden or keep `custom_`. Don't pretend it wasn't a bug.
</details>

---

## 7. The three scenarios (lifecycle framing)

### 7.1 Initial migration

**Trigger**: the multi-system migration playbook has run through Phase 3 full cutover (all `custom_` are active); promotion to golden now begins.

**Key decisions** (**these are domain-owner business judgments and are not being automated** — see below):
- Which rules have golden equivalents: the domain owner manually compares `custom_` and golden rule triggers + semantics against [Rule Pack ALERT-REFERENCE](../rule-packs/ALERT-REFERENCE.en.md)
- Which rules should stay `custom_` (no Rule Pack counterpart, customer-business-specific)

> 📝 **Why we don't build a `rule-pack-mapping --suggest` tool** (tracked: [issue #405](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/405)): auto-suggesting "custom_X should promote to golden Y" requires deep PromQL semantic analysis (AST + equivalence judgment) with a low accuracy ceiling. A wrong suggestion gives the customer false confidence — worse than no suggestion. **Business-semantic judgment** stays a human decision; we won't force-automate it. `rule-pack-diff` (mechanical comparison between versions) is factual work, will be built; `--suggest` (judging which `custom_` should be promoted) is judgment work, will not.

**End condition**: all promotable rules have been upgraded, or the customer decides to stop at some point.

### 7.2 New tenant onboarding (6 months later, new team comes in)

**Trigger**: the platform is stable; a new team takes over a service or a new tenant comes in.

**Pattern**:
- The new team starts with the Rule Pack (golden) as their starter
- When they're not satisfied with a Rule Pack condition, they write a `custom_*` override first
- As the Rule Pack naturally evolves to match the new team's needs → promote `custom_*` away → delete the override

**This is the inverse of 7.1**: 7.1 is `custom_ → golden`; 7.2 is `golden → custom_ → golden` (reverse onboarding). But the **decision criteria are the same** (*Subset overlap*, observation period, rollback path).

### 7.3 Rule Pack upgrade (the platform ships a new version)

**Trigger**: the platform ships Rule Pack v2; the customer has existing `custom_*` overrides targeting Rule Pack v1.

**The problem**: Rule Pack v2 might:
- Add new alerts (the customer's `custom_*` doesn't need to override)
- Change alert semantics (the customer's `custom_*` override becomes outdated, redundant)
- Break existing labels (the customer's `custom_*` is still using old label names)

#### ⚠️ Disablement drift — the real risk of a double-fire alert storm

When a customer wrote a `custom_*` rule, it **usually means they also disabled / silenced the corresponding v1 golden rule** in the system (to prevent `custom_` and golden firing simultaneously and double-firing).

If a Rule Pack v2 upgrade **changes the alert name or label schema**, the customer's v1-targeted disable configuration (e.g. `_defaults.yaml`'s `disable: [<v1-name>]` or AM silencer's `matchers: [alertname="<v1-name>"]`) **may silently break**. Consequences:

1. The customer's `custom_*` still fires as usual
2. v2 golden (the disable no longer matches) also fires
3. → **Alert storm** (the same incident pages two alert paths simultaneously, PagerDuty goes off the rails)

**SOP**:

1. List the diff points between Rule Pack v1 and v2 (alertname / label-schema breaking changes)
   - **✅ `da-tools rule-pack-diff --from <v1.yaml> --to <v2.yaml>`** ([issue #405](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/405) Category D, shipped 2026-05-12) — outputs added / removed / breaking-label-schema categories; `--ci` mode exits 1 on breaking changes. Typical invocation:

     ```bash
     git show v1.0.0:rule-packs/rule-pack-mariadb.yaml > /tmp/v1.yaml
     git show v2.0.0:rule-packs/rule-pack-mariadb.yaml > /tmp/v2.yaml
     da-tools rule-pack-diff --from /tmp/v1.yaml --to /tmp/v2.yaml
     ```
   - **Manual fallback** (when the tool is unavailable): cross-reference the Rule Pack version's `CHANGELOG.md` ([ALERT-REFERENCE](../rule-packs/ALERT-REFERENCE.en.md) lists current stable alertnames) + `git diff rule-packs/<pack>/v1.0.0/...rule-packs/<pack>/v2.0.0/`
2. **Disablement drift check**: for every alert where the customer has a `custom_*`, verify the corresponding disable config still hits v2:
   - `_defaults.yaml`'s disable list — confirm the v2 alertname is on the list (or add it)
   - AM's silencer matchers — confirm the v2 label schema doesn't make existing matchers miss
   - **Missing either one = double-fire; both must be fixed before shipping v2**
   - Concrete diagnostic commands: [troubleshooting-checklist §1.3.2](../integration/troubleshooting-checklist.md#132-silencer-mismatchdisablement-drift-double-fire-alert-storm) (**ZH only — [#409](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/409)**)
3. For each `custom_*`: judge whether v2 absorbs it → if yes, run this guide's promotion flow
4. For semantic changes in v2: either rewrite `custom_*` to align with v2's schema or leave it as-is (**and** synchronize the disable config)
5. For v2 breaking changes: promotion is mandatory (forced upgrade)

**Audit-hook suggestion** (tracked: [issue #405](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/405)): `da-tools upgrade-check` runs at v1→v2 upgrade time, auto-detects disablement drift, lists "alerts that would double-fire", and must hit zero before merge. May be unified with `silencer-drift-check`.

### The pattern shared by all three scenarios

Regardless of scenario, promotion runs the same §4–§6 decision criteria / batch sizing / observation period — that's the core reason this is a lifecycle pattern, not a migration step.

---

## 8. Relationship to other docs

```
multi-system-migration-playbook.md  ─ Phase 3 cutover (routing flip done)
                       │
                       └─ Phase 4 starts ──────────►
                                                  │
                                                  ▼
                              This guide (Staged Adoption Lifecycle)
                                                  │
                                                  ├─► custom-rule-governance.md (custom_ namespace rules)
                                                  ├─► multi-system-migration-playbook §11 (Rollback boundaries)
                                                  └─► rule-packs/ALERT-REFERENCE.md (golden cheat sheet)
```

- **multi-system-migration-playbook**: responsible for cutover routing (one-time event during migration)
- **This guide**: responsible for the promotion lifecycle (applies every time new rules enter)
- **custom-rule-governance**: defines the `custom_*` namespace itself
- **rule-packs/ALERT-REFERENCE**: full golden alert table for mapping lookups

---

## 9. When *not* to use staged adoption

- **You only have 1–2 rules**: just promote — staged would be ceremony
- **dev / staging environments**: push everything, break things, learn fast
- **Pure noise reduction**: turning off a too-noisy alert doesn't need staged
**Core judgment**: the cost of staged is time + ops attention; only worth it when blast radius × cutover frequency is high enough.

---

## 10. Observation metrics & dashboards

Platform-side, you can see:

- `da_alert_promotion_batch_status{batch_id, status="active|reverted|done"}`
- `da_alert_quality_diff{custom_, golden, metric}` (if enabled)
- Grafana dashboard: `Staged Adoption Progress` panel (v2.9 ship; currently use [grafana-dashboards](../grafana-dashboards.en.md)' existing `shadow-rules-active` panel for visibility)

→ Watch continuously during Phase 3 + 1 ops cycle; afterwards the dashboard can be downgraded to weekly review.

---

## 11. Cross-references

- **Rule namespace governance**: [`docs/custom-rule-governance.md`](../custom-rule-governance.en.md)
- **Multi-system migration (cutover phase)**: [`docs/scenarios/multi-system-migration-playbook.md`](multi-system-migration-playbook.md) (**ZH only — [#409](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/409)**)
- **Shadow monitoring SOP**: [`docs/shadow-monitoring-sop.md`](../shadow-monitoring-sop.en.md)
- **Rule Pack cheat sheet**: [`rule-packs/ALERT-REFERENCE.md`](../rule-packs/ALERT-REFERENCE.en.md)
- **Design origin**: this document's framework crystallised from the PR #389 strategic discussion, locked as the "lifecycle pattern not migration step" perspective (replacing the initial "migration final step" design)
