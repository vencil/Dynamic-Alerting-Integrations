---
title: "Before the Alert Fires: From Resource Saturation to Symptom-Based Alerting"
description: "An introduction to alert design — what to alert on: symptom vs cause, the asymmetry of the Four Golden Signals, and where saturation signals belong"
tags: [best-practices, alerting, sre, fundamentals]
audience: [sre, domain-expert, tenant]
version: v2.9.0
lang: en
---
# Before the Alert Fires: From Resource Saturation to Symptom-Based Alerting

> **Language / 語言：** [中文](./alerting-design-fundamentals.md) | **English (Current)**

> **Audience**: Teams just starting to build out their alerting — tenant teams and domain experts alike. No SRE background required.
> **What you'll take away**: one criterion for "when to wake someone up", one signal-tiering reference table, and one self-check checklist.
> **Series**: This is the first article in the alerting best-practices series (**what to alert on**); the second, [How Strict Is Strict Enough](alerting-slo-error-budget.en.md), covers how strict to be (SLOs and error budgets); the third, [Beyond Actionable](alerting-best-practices.en.md), covers the actions an alert triggers. Readers who already have a mature alerting setup can skip straight to the later two.

---

## 0. First, a Question

How many alerts in your list look like this: `CPU > 90% → critical`?

This article wants to convince you that this alert is **wrong most of the time** — not the threshold, but **what it alerts on**. A wrong alert list pulls off two bad things at once: it stays silent when something is genuinely wrong (missed alerts), and it fires constantly when nothing is (false alarms → alert fatigue → nobody responds when a real one fires). Fixing it doesn't require learning any tool first — only changing the question you ask:

> **Don't ask "what's wrong with the machine" — ask "what did the user feel" first.**

## 1. Symptom vs Cause: The Criterion for Paging

Alert signals come in two kinds:

- **Symptom**: what users can feel directly — pages getting slow, requests failing, data being stale.
- **Cause**: what engineers look at while diagnosing — CPU, memory, connection counts, queue depth.

The classic rule is to **alert on symptoms, rarely on causes** (the sources are Rob Ewaschuk's [My Philosophy on Alerting](https://docs.google.com/document/d/199PqyG3UsyXlwieHaqbGiWVa8eMWi8zzAn0YfcApr8Q) and [Chapter 6 of the Google SRE Book](https://sre.google/sre-book/monitoring-distributed-systems/), both worth reading in full), and the reasoning is mechanical: one symptom can come from dozens of causes (a slow page might be the DB, might be GC, might be the network), and a cause doesn't necessarily produce a symptom (a machine at 100% CPU may simply be diligently getting all of its work done). Alerting on causes buys you missed alerts and false alarms at the same time.

Whether to **wake someone up (page)** comes down to three conditions holding simultaneously:

1. **Urgent** — it will get worse if it waits until morning;
2. **User-visible** — it is a symptom, not jitter in an internal metric;
3. **Requires human judgment** — anything with a clear automated fix should go to automation (see the third article in this series), not wake a person.

If any one of the three is missing, downgrade: not urgent goes to a ticket, not a symptom goes to a dashboard, automatable goes to automation.

Beyond these three sits an implicit premise: when it fires, someone must be able to **do something about it** (actionable). Take that word one step further downstream — whether that "something" is itself any good — and you have the entire subject of the third article in this series.

## 2. The Asymmetry of the Four Golden Signals: Same Table, Different Fates

Monitoring textbooks commonly present the Four Golden Signals: latency, traffic, errors, and saturation. Many teams wire all four to alerts and set them all to critical — the introductory literature does say "measure all four, alert when they go bad", but that is a **coverage baseline, not a design goal**: the four signals serve **different purposes**:

| Signal | Nature | Where it belongs |
|---|---|---|
| Latency, errors | **Symptoms** — user-visible | The alerting mainstay; page when severe |
| Traffic | **Context** — for interpreting the other signals | Dashboards, capacity planning |
| Saturation | **Leading indicator** — "about to go wrong", not "gone wrong" | warning / ticket / capacity planning |

Saturation is worth knowing about, but **most of the time it is not worth waking someone up for** — unless it has already surfaced as a symptom. That is exactly what is wrong with "CPU > 90% → critical": it treats a leading indicator as a symptom.

## 3. Should "CPU Is Maxed Out" Actually Fire?

Consider two machines, both at 100% CPU:

- **An online API server**: CPU saturates → requests queue up → latency spikes → users suffer. **But the correct thing to alert on is still latency and error rate** (the symptoms); CPU is only the cause you look at while diagnosing.
- **A worker machine running scheduled batch jobs**: periodically maxing out its CPU means **it is doing its job**. Users care about exactly one thing: **did the work finish before the deadline**. The right alert is on the "freshness of the batch output" or the "completion deadline", not on CPU.

The alert the second machine needs already has a name in the literature — **freshness** — no need to invent your own. Quantifying "what the user feels" into a monitorable metric is what the jargon calls an **SLI** (Service Level Indicator); different kinds of services call for asking the user different questions:

| Service type | What to ask the user | SLI types |
|---|---|---|
| Request-driven (APIs, websites) | Did the request succeed? Was it fast enough? | Availability, latency, correctness |
| Pipeline / batch (ETL, reports, scheduled jobs) | Is the data fresh enough? Did it finish within the deadline? | Freshness, coverage |
| Storage systems | Can I write it and read it back? | Availability, durability |

Map each of your systems into this table and the skeleton of your alert list falls out — and you will notice CPU does not appear in any cell.

## 4. Anatomy of an Anti-Pattern: Wiring a Saturation Signal to a Destructive Action

A common design pattern: when a stateful service's connection pool fills up, automatically trigger a failover and promote the standby node. The motivation sounds reasonable — "stop connections from piling up". But once live, the predictable outcome is that it does more harm than doing nothing. Three mismatches at once:

1. **Treating saturation as failure.** A full connection pool is a *load* problem, not a *broken node*. Promoting the standby moves no load away — the incoming requests simply redirect, and the new node fills up just as fast.
2. **A continuously-true condition × a one-shot destructive action.** "Pool is full" stays true until the load recedes; the action finishes, the condition is immediately true again → re-trigger → **cascading failovers**, and the whole service group starts thrashing.
3. **Destroying work that would have completed.** Many of the connections sitting in the pool were slowly finishing their own work; every switchover wipes them all out — the symptom-handling design ends up amplifying the impact.

The correct decomposition sends each signal back to where it belongs:

- **Symptoms** (request latency, error rate) → page a human to judge;
- **Saturation** (connection pool utilization) → warning + capacity planning, plus backpressure at the application layer (backpressure — when the pool is full, make upstream queue, slow down, or reject, instead of piling up without bound) — that is the targeted cure for "piling up";
- **Failover** is reserved for genuine failure (node health checks failing) — only then does the system have a clear "correct shape" (a healthy primary node exists) that automation can restore it to.

"What kind of condition can safely carry an automated action" deserves an entire article of its own — see the third article in this series, [Beyond Actionable](alerting-best-practices.en.md), which has the full criteria and guardrails.

## 5. Division of Roles: Signal Semantics Belong to Domain Experts

Every judgment above — "what counts as a symptom in this domain", "how much saturation counts as an early warning", "which metric maps to which user experience" — **is domain knowledge**. It should not be reinvented from scratch by every tenant team, nor imposed one-size-fits-all by the platform team. The healthy division of labor is:

- **Domain experts** define signal semantics: which metrics are this domain's symptoms, how severity maps, what the reasonable saturation bands are.
- **Tenant teams** only tune their own targets: how much latency my service tolerates, when my maintenance window is.

Both sides need the concepts in this article — domain experts use them to design signals, tenants use them to understand what they are tuning.

## 6. What This Platform Ships as Defaults

Everything up to this point is general guidance. The table below maps it to this platform (the legend follows the series convention): ✅ Enforced (enforced in the code path, cannot be bypassed) / ⚙️ Default (provided by default, configurable override) / 📖 Guideline (outside the platform's scope).

| Guideline | This platform | Mechanism |
|---|---|---|
| Symptom-based alert design | ⚙️ | The design orientation of the built-in rule packs (the platform's per-domain bundles of alerting rules); the platform does not review the semantics of tenant custom alerts — apply this article's self-check |
| Domain defines signal semantics, tenants only tune targets | ⚙️ | The [configuration layering](design/config-driven.en.md) of rule packs (domain semantics) + tenant YAML (thresholds) |
| No false alarms during batch windows | ⚙️ | [Scheduled thresholds](design/config-driven.en.md) — configurable time windows (e.g. nightly batch) automatically switch to looser thresholds; the targeted cure for "nightly-batch false alarms" |
| Severity tiering, deliver only the highest | ⚙️ | Severity Dedup (enabled by default; tenants can explicitly disable it) |
| Malformed / high-cardinality rules never reach production | ✅ | Schema validation + cardinality guard (blocks format and cardinality — a label-combination explosion can drag down the monitoring system itself; does not block semantically bad rules) |
| Saturation signals downgraded to warning / capacity planning | 📖 | A design choice — this article's guideline; the platform's multi-tier severity lets you configure them as warning |

**Want a worked example of the decomposition?** The platform's built-in rule packs are themselves written to this article's principles and can be read directly as templates (full catalog in the [Alert Reference](rule-packs/ALERT-REFERENCE.en.md)):

- **The base tier of saturation alerts is always warning; critical is an explicit escalation**: for connection-count alerts (e.g. `NginxHighConnections` → `NginxHighConnectionsCritical`), the critical tier only exists after you separately configure the `_critical`-suffixed threshold — "is this saturation severe *for me*" is a decision you make, not a default.
- **Escalation relies on signals corroborating each other — but be clear about where that sits in the hierarchy**: `MariaDBSystemBottleneck` requires "connection saturation **and** thread-concurrency saturation" to hold simultaneously for 60 seconds before escalating to critical. To be honest: two saturations corroborating each other is still a "composite cause", not a symptom — it is a **compromise heuristic for when symptom metrics are out of reach** (when the exporter cannot see your end-to-end latency, "both saturations true at once" is the closest stand-in for "work is jamming up"). That is why the pack also provides the closer-to-symptom `MariaDBHighSlowQueries` (slow-query rate — the direct projection of "getting slower"); the composite alert is its supplement, not its replacement. When designing your own, the order of preference is: **if you can find a symptom, alert on the symptom; only when you cannot, use multi-signal corroboration to raise the credibility of a cause alert** — don't walk away thinking "tying several hardware metrics together justifies a critical".
- **Alert copy directs attention to capacity, not actions**: the description text of saturation alerts says "check the connection pool size" and "capacity expansion" — even the annotations are part of a signal's semantic design.

When writing your own custom alerts, copy these three patterns.

## 7. Self-Check Checklist

Take your existing alert list and ask, item by item:

1. When this alert fires, **what did the user feel**? Can't answer → it is a cause alert; consider downgrading it to a dashboard.
2. Does it satisfy **urgent + user-visible + requires human judgment** all at once? Missing any one → downgrade (ticket / dashboard / automation).
3. How many of the **saturation metrics** in the list (CPU, memory, connections, disk, queue depth) carry critical? For each one, ask: has it surfaced as a symptom? No → downgrade to warning and move it to capacity planning.
4. Do the alerts for **batch / scheduled worker machines** ask about "resources", or about "finished within the deadline"? Switch to the latter.
5. Is any **automated action hanging off a saturation signal**? If so → detach it first, read the third article in this series, then decide the conditions for putting it back.
6. Are signal semantics **defined by domain experts**, or guessed independently by each team?

Next step: once you are alerting on the right things, the next question is "how severe, and when to wake someone up" — that is the subject of SLOs and error budgets — see the second article in this series, [How Strict Is Strict Enough](alerting-slo-error-budget.en.md).
