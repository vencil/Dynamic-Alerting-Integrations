---
title: "Interactive Tools"
tags: [interactive, tools, react]
audience: [all]
version: v2.9.0
lang: en
---

# Interactive Tools

> **Language / 語言：** **English (Current)** | [中文](./interactive-tools.md)

> **Audience**: all roles — the "For whom" column below marks each tool's primary users.

The platform provides **five** interactive tools to help different roles get started fast. **Want to try them now?** → [Interactive Tools Hub](https://vencil.github.io/Dynamic-Alerting-Integrations/), or run locally with `make portal-run` (see [How to use](#how-to-use)).

## Tools at a glance

| Tool | For whom | What it does | When to use |
|------|----------|--------------|-------------|
| **Getting Started Wizard** | New users, all roles | Guides you to the right getting-started doc by role (Platform Engineer / Domain Expert / Tenant) and shows the key steps per role | First contact with the platform; role-oriented onboarding |
| **Tenant YAML Playground** | Tenant / Domain Expert | Real-time Tenant YAML validation (key names, three-state values, schedule format) + live preview of generated Prometheus metrics | Authoring or debugging Tenant YAML |
| **Rule Pack Selector** | Platform / Tenant (onboarding) | Recommends applicable Rule Packs by tech stack (MySQL / PostgreSQL / Redis / JVM / Nginx, etc.), showing each pack's alert count and covered metrics | Choosing which Rule Packs to enable at initial onboarding |
| **CLI Command Builder** | DevOps / Platform | Select a da-tools subcommand → fill parameters → auto-generate a complete `docker run` command to copy | When you don't want to memorize the Docker command format |
| **ROI Calculator** | Decision-makers | Input org scale (tenants, Rule Packs, on-call staff) + current ops costs, and instantly compute three benefits: rule maintenance O(N×M)→O(M) reduction, alert-storm suppression rate, onboarding speedup (import actual `alert_quality.py --json` data to refine) | Evaluation phase — presenting quantified TCO savings to decision-makers |

> **Source**: the five components live under `tools/portal/src/` (the wizard in `getting-started/`, the rest in `interactive/tools/`); each is a standalone React functional component with no external state-management dependency.

## How to Use

These `.jsx` components can run directly in the following environments:

1. **GitHub Pages (public access, recommended)** — Go to repo Settings → Pages → Source, select `main` / `/docs`. The landing page at `docs/interactive/index.html` lets visitors try all tools in the browser. Components are transpiled client-side via `docs/assets/jsx-loader.html` using Babel standalone — no build step required
2. **da-portal Docker Image (enterprise intranet / air-gapped, recommended)** — `docker run -p 8080:80 ghcr.io/vencil/da-portal` to host the full Interactive Tools Portal on your internal network. Supports volume-mount customisation of `platform-data.json` and `flows.json`, and nginx reverse proxy to solve Prometheus CORS issues. See [components/da-portal/](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/da-portal/README.md)
3. **Claude Artifacts** — Paste the `.jsx` content into a conversation and Claude renders it instantly
4. **React dev environment** — Import the component into a `create-react-app` project
5. **CodeSandbox / StackBlitz** — Online instant preview

### Local preview

```bash
# Option A: Python http.server (quick verification)
cd docs && python3 -m http.server 8888
# Open http://localhost:8888/interactive/

# Option B: da-portal Docker (mirrors production deployment)
make portal-image && make portal-run
# Open http://localhost:8080
```
