---
title: "How Strict Is Strict Enough: Setting Alert Thresholds with SLOs and Error Budgets"
description: "Alerting best practices — how strict to set thresholds and when to wake someone up: the last mile from SLI / SLO / error budgets to burn-rate alerting"
tags: [best-practices, alerting, sre, slo]
audience: [sre, domain-expert, tenant]
version: v2.9.0
lang: en
---
# How Strict Is Strict Enough: Setting Alert Thresholds with SLOs and Error Budgets

> **Language / 語言：** [中文](./alerting-slo-error-budget.md) | **English (Current)**

> **Audience**: Teams that already know **what** to alert on (the first article in this series, [Before the Alert Fires](alerting-design-fundamentals.en.md)) and whose next question is "how strict should the threshold be, and when is it worth waking someone up".
> **Prerequisites**: The symptom/cause concepts from the first article; a basic sense of Prometheus queries (being able to read one without writing one is fine).
> **What you'll take away**: one formula (the error budget), one criterion (burn rate), and one translation mapping from SLOs to alert rules.

---

## 0. "What's the Difference Between 99.9 and 99.99?"

The first article settled "what to alert on"; this one tackles the next question: **how strict should it be**.

Most teams do it by gut feel — "let's alert when the error rate hits 1%". Gut-feel thresholds end one of two ways: set too loose, users suffer and you never find out; set too tight, you get woken up ten times a night and nine of them need no action — and by the tenth, the one that's real, nobody is paying attention anymore (the alert fatigue from the first article, back through another door).

To break out of this loop, first accept a counter-intuitive fact: **100% reliability is a trap**. Each additional nine of reliability costs not linearly but exponentially; and your users genuinely cannot tell 99.99% from 100% — all they care about is "does it work when I use it". So the right question is not "how do we never fail", but:

> **"How much failure do we allow ourselves?" — define that quantity, measure it, and use it to decide when to wake someone up.**

That is the SLI / SLO / error budget trio. The concepts come from [Chapter 4 of the Google SRE Book](https://sre.google/sre-book/service-level-objectives/) and the [SRE Workbook](https://sre.google/workbook/implementing-slos/); for an introductory narrative in Chinese, we recommend these two iThome Ironman articles ([part one](https://ithelp.ithome.com.tw/articles/10392776), [part two](https://ithelp.ithome.com.tw/articles/10392778)). This document does not repeat them — we only fill in **the last mile where they stop: from the SLO table to alert rules**.

## 1. SLI: First, Turn "How Users Feel" into a Ratio

An SLI (Service Level Indicator) is a ratio with a standard shape:

> **SLI = good events ÷ total valid events**

Three design points, each of which is a decision:

1. **What counts as "good"** — the request succeeded (HTTP 2xx/3xx)? Was fast enough (< 300ms)? Data fresh enough (< 10 minutes)? This is the deepened version of the SLI-type table in §3 of the first article: each service shape picks its own definition of "good".
2. **What counts as "valid"** — the denominator should exclude events that are not your responsibility (client-side 4xx is usually not your fault).
3. **Less is more** — 2–3 SLIs per critical user journey is enough (pick one or two from availability and latency); a dozen SLIs only leads back to alert fatigue.

Which journeys deserve an SLI? Rough-rank by "business value × usage frequency": high-value high-frequency (the product's heartbeat) and high-value low-frequency (critical moments like checkout or report generation) come first; low-value peripheral features get the loosest targets — or none at all.

## 2. SLO: The Passing Grade Is Negotiated, Not Pulled Out of Thin Air

An SLO (Service Level Objective) is your public commitment on an SLI: "Over the past 30 days, 99.9% of requests must succeed." Setting one is half science, half negotiation:

- **The science: start from historical data.** Look up your SLI's actual performance over the past 28–30 days — if reality is 99.94%, setting the SLO at 99.9% is an honest starting point; declaring 99.99% outright is digging a hole for your team. Historical data is the cheapest consultant you'll ever hire — ask it first.
- **The art: cost versus commitment.** Every additional nine drives architectural complexity and engineering investment up exponentially; your brand promise ("fastest" or "most reliable"), where competitors sit, and what users actually tolerate decide whether it's worth it.
- **Two iron rules**: **never set 100%** (that means zero budget and freezes all change); **an SLO is a living thing** (after a cycle or two, revisit it based on budget consumption).

Window choice: a **rolling 28 or 30 days** is the common starting point (28 days aligns with whole weeks, avoiding the "this month had more weekends" skew). Remember this window — every number below is anchored to it.

One final honest boundary: **the more extreme the target, the less alerting can help you**. With a 99.999% SLO, a single total outage of 26 seconds burns the entire month's budget — faster than an alert can even propagate. Reliability at that level comes from deployment design that prevents total outages (canaries, progressive rollouts), not from alert-driven reaction.

## 3. Error Budget: Turning "How Much Failure Is Allowed" into a Currency

> **Error budget = (1 − SLO) × window**

SLO 99.9%, 30-day window → budget = 0.1% × 30 days ≈ **43 minutes of total unavailability** (or the equivalent in partial degradation).

The power of this number is that it turns "risk" into **a currency you can spend**: shipping a new feature spends budget, running a chaos experiment spends budget, an incident burns budget. While the budget is plentiful, the team is free to spend boldly; when the budget runs low, everyone agrees it's time to hit the brakes — "should we postpone the release" turns from an emotional debate into checking the balance.

One honest boundary: **what happens once the budget is exhausted (freeze releases? add people to stability work?) is an organizational decision** — a tool can give you the balance and the burn speed, but it cannot give you policy. The policy must be signed off in advance by product, development, and operations; a policy negotiated in the heat of the moment carries no authority.

## 4. Burn Rate: The Last Mile from SLO to Alert Rules

Most SLO tutorials cover the first three sections; most of them **stop at the dashboard**. But you are still missing one last thing: **the alert rules**. This last mile has a standard answer, and it is worth walking through in full.

### 4.1 Three Dead Ends

Try any of the naive approaches and you will find its fatal flaw:

1. **A raw error-rate threshold** ("alert when error rate > (1 − SLO)"): too **sensitive** — with a 0.1% threshold, a single network blip fires it, and alert fatigue is back.
2. **Lengthening the window**: fewer false positives, yes — but detection and recovery both go numb: the incident is long over and the alert is still hanging; the extreme version, "alert only once the SLO is already violated", means you learn about it after the budget is gone, when nothing can be done.
3. **Adding a `for:` duration condition**: it requires the error state to be **continuously** true before firing — the moment errors oscillate, the timer resets, and a real incident may never fill a contiguous window. This is not purely theoretical: probe it with fault-injection experiments and the behavior of `for:`-style alerts turns out to be highly sensitive to the failure waveform, and hard to predict.

Remember the failure mechanism of the third dead end — it and the advantage of the two-window design below are **two sides of the same coin**.

### 4.2 Burn Rate: Measure "How Fast It Burns", Not "Whether It Burned"

> **Burn rate = actual error rate ÷ budgeted error rate (1 − SLO)**

burn rate = 1 means burning the budget at exactly the planned pace — with a 30-day window (= 720 hours), even-paced burning consumes 1/720 ≈ 0.14% of the budget in 1 hour. Flip that around: if 1 hour alone burns **2%**, the burn speed is 0.02 ÷ (1/720) = **14.4 times** the even pace — a speed worth waking someone up for.

[The SRE Workbook's standard configuration](https://sre.google/workbook/alerting-on-slos/) uses **three tiers** (2%/1h and 5%/6h both page; 10%/3d files a ticket); this document **simplifies to two tiers as a starting point** for beginner teams — note that the second tier is a page in the Workbook's original recommendation; demoting it to a ticket here is a deliberate, pragmatic downgrade (6× means the budget is gone in roughly 5 days: it should be handled today, but most beginner teams do not need to wake anyone for it). As trust in the signal grows, you can restore the full three-tier spectrum:

| Tier | Meaning | Windows (why two — see below) | Action |
|---|---|---|---|
| **Fast burn** | 2% of budget burned in 1 hour (burn rate ≈ 14.4×@30d) | 1h + 5m | **page** — deal with it now |
| **Slow burn** | 5% of budget burned in 6 hours (burn rate ≈ 6×@30d) | 6h + 30m | **ticket** — handle today, no need to wake anyone |

Three details that are easy to trip on:

1. **Two windows ANDed, not one window.** With only the long window (1h), the alert keeps firing long after the incident ends (a long-window average comes down slowly); pair it with a short window (5m) ANDed in — the moment the short window goes quiet, the alert resets. This is the key to burn-rate alerts being "non-sticky". Contrast with the third dead end in §4.1: `for:` demands "**continuously** true" — so oscillation resets it and real incidents slip through; the short window demands "true **on recent average**" — robust to gaps, yet resetting the instant recovery is real. Two sides of the same coin.
2. **The multiplier is bound to the budget window.** The derivation above is anchored to a 30-day (720h) window; change the window to 28 days and the same "2% in 1 hour" multiplier becomes 13.44. When copying formulas, remember: **the multiplier is a derived value, not a magic constant**.
3. **Low traffic will lie to you.** Only 2 requests in 5 minutes with 1 failure = a 50% error rate = instant fast burn. Add an **absolute error-count floor** to the alert (e.g. "counts only if bad events in the short window ≥ N"); the value of N is itself an extension of the SLO negotiation — "how many errors do you tolerate at low traffic". For extremely low-traffic services there are a few more routes: pad the denominator with synthetic traffic (e.g. existing blackbox probes), aggregate several related small services into a single SLI, or honestly lengthen the windows / accept lower detection confidence — this is a well-known hard problem across the industry; do not pretend it does not exist.

### 4.3 The Generic Shape (Tool-Agnostic Pseudo-PromQL)

```promql
# SLI (store via recording rules first, one per window)
sli:error_ratio:5m  = sum(rate(bad_events[5m]))  / sum(rate(total_events[5m]))
sli:error_ratio:1h  = sum(rate(bad_events[1h]))  / sum(rate(total_events[1h]))

# Fast-burn page (SLO=99.9%, 30d window):
#   both windows exceed 14.4x the budgeted error rate, and enough absolute errors in the short window
  sli:error_ratio:1h > 14.4 * (1 - 0.999)
and sli:error_ratio:5m > 14.4 * (1 - 0.999)
and sum(increase(bad_events[5m])) > N_min
```

Slow-burn has the same shape (6h/30m windows, 6× multiplier, ticket-level severity). Remember to guard the denominator against division by zero (a window with no traffic should not spray alerts). The above is an **illustrative form** — the real thing is the `record:`/`expr:` pair of fields in recording rule YAML, named according to your team's recording-rule conventions.

A reminder for scale: if you run dozens of services, **do not tune parameters per service** — sort the services into a small number of tier buckets (for example five: critical / high-fast / high-slow / low / no-SLO), share one set of windows and multipliers within each bucket, and let each service set only its own objective. Parameter customization is cognitive debt; bucketing is how you pay it off.

### 4.4 How This Connects to the Previous Two Articles

- Burn-rate alerting is the completed form of the first article's "symptom-oriented" alerting: the SLI itself is the quantification of a symptom, and the page condition is anchored directly to the speed at which users are being hurt.
- Fast burn (page) versus slow burn (ticket) is by construction the three-criteria split from §1 of the first article — urgent wakes a human, non-urgent files a ticket.
- Whether the actions taken after an alert fires should be automated, and how to automate them safely — back to the idempotency spectrum in the third article, [Beyond Actionable](alerting-best-practices.en.md).

## 5. This Platform's Mapping (The Honest Version)

This document is generic guidance; the platform-side mapping reuses the series' three-value legend (✅ Enforced / ⚙️ Default / 📖 Guidance) — the full table lives in [§6 of the third article](alerting-best-practices.en.md#6-this-platforms-honest-boundary); here we list only the SLO-related increments:

| Guideline | This platform | Mechanism and honest qualifier |
|---|---|---|
| SLI ratio declaration (zero PromQL for tenants) | ⚙️ | The `ratio` recipe in custom alerts — declare the numerator/denominator counters and it compiles into a ratio alert with a division-by-zero guard; **single window only** |
| Multi-window burn-rate alerting | 📖 | The generic recipe in §4 of this document; multi-window AND and multiplier conversion currently must be assembled downstream yourself |
| Threshold science (data-driven starting points) | ⚙️ | [`da-tools threshold-recommend`](cli-reference.en.md#threshold-recommend) — recommendation engine based on historical P50/P95/P99; works on resource-class pack thresholds — the starting point for an SLO objective still comes from querying your historical SLI (§2) |
| Historical SLI performance queries (the scientific starting point for SLOs) | 📖 | Any Prometheus-compatible backend's range query will do; the platform does nothing special here |
| Error-budget policy (freeze/release) | 📖 | An organizational decision — a tool can give you the balance, not the authority |

## 6. The Takeaway Checklist

1. For each critical user journey: pick 2–3 SLIs (good events ÷ valid events), with both "good" and "valid" written as explicit definitions.
2. Set the SLO from **historical data**; never 100%; use a rolling 28/30-day window and remember which one.
3. Compute the error budget's **absolute amount** (in minutes) so everyone can see the balance.
4. Sign off the budget-exhaustion policy **in advance** with all three parties — the tool gives you numbers; the policy gives you authority.
5. Alert on burn rate, not raw error rate: fast burn (two windows ANDed) → page; slow burn → ticket.
6. The multiplier is derived from the budget window — recompute it when the window changes; add an absolute error-count floor for low traffic.
7. After it fires — for safety on the action side, see the third article, [Beyond Actionable](alerting-best-practices.en.md).
