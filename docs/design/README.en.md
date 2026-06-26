---
title: "Design Deep-Dives — Architecture spoke documents"
tags: [design, architecture, navigation]
audience: [platform-engineer, devops]
version: v2.9.0
lang: en
---

# Design Deep-Dives

> **Language / 語言：** **English (Current)** | [中文](./README.md)

This directory holds the **deep-dive spoke documents** for [Architecture & Design](../architecture-and-design.en.md) (the hub). Each one focuses on a single design facet, for readers who already grasp the overall picture and want to go deeper into one area.

> **Suggested reading order:** start with [Architecture & Design](../architecture-and-design.en.md) for the full picture, then deep-dive any spoke below by interest. For the *why* behind each decision (trade-offs, alternatives), see the [ADR index](../adr/README.en.md).

## Spoke documents

| Document | Focus | When to come here |
|------|------|-----------|
| [Config-Driven Architecture](config-driven.en.md) | Tri-state config, dynamic routing, Tenant API, SHA-256 hot-reload | Understand how YAML drives the whole chain |
| [High Availability (HA) Design](high-availability.en.md) | Replicas, PodDisruptionBudget, double-counting prevention | Planning a production-grade HA deployment |
| [Rule Packs & Projected Volume](rule-packs.en.md) | Independent rule-pack delivery, zero PR conflicts, on-demand evaluation | Understand how the 16 Rule Packs are delivered in isolation |
| [Future Roadmap](roadmap-future.en.md) | K8s Operator, Design System, Auto-Discovery | Understand the mid/long-term direction |

## Next steps

- Want the decision rationale (trade-offs / alternatives)? → [ADR index](../adr/README.en.md)
- Ready to deploy? → [Integration Guides](../integration/README.en.md) · [Platform Engineer Quickstart](../getting-started/for-platform-engineers.en.md)
- Want measured numbers? → [Benchmarks](../benchmarks.en.md)
