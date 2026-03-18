---
title: "Interactive Tools"
tags: [interactive, tools, react]
audience: [all]
version: v2.2.0
lang: en
---

# Interactive Tools

The platform provides four interactive React components that can run in any React-compatible environment (Claude Artifacts, CodeSandbox, or self-hosted pages).

## Getting Started Wizard

**File:** `docs/getting-started/wizard.jsx`

Guides users to the appropriate getting-started documentation based on their role (Platform Engineer / Domain Expert / Tenant), with dynamic display of key operational steps per role.

**Use case:** First-time users navigating the platform.

## Tenant YAML Playground

**File:** `docs/interactive/tools/playground.jsx`

Interactive Tenant YAML editor with real-time syntax validation (key names, three-state values, schedule format) and live preview of generated Prometheus metrics.

**Use case:** Quickly validate Tenant YAML configurations during authoring or debugging.

## Rule Pack Selector

**File:** `docs/interactive/tools/rule-pack-selector.jsx`

Recommends applicable Rule Packs based on technology stack (MySQL / PostgreSQL / Redis / JVM / Nginx, etc.), showing alert count and covered metrics for each pack.

**Use case:** Choosing which Rule Packs to enable during initial onboarding.

## CLI Command Builder

**File:** `docs/interactive/tools/cli-playground.jsx`

Select a da-tools subcommand, fill in parameters, and automatically generate a complete `docker run` command ready to copy.

**Use case:** Quickly building correct Docker execution commands without memorizing the format.

## ROI Calculator

**File:** `docs/interactive/tools/roi-calculator.jsx`

Adoption benefit estimator — input organization scale (tenant count, Rule Pack count, on-call staff) and current operational costs (config change time, alert storm frequency, manual onboarding time) to instantly calculate three benefit dimensions: Rule maintenance time reduction from O(N×M) to O(M), alert storm auto-suppression rate, and onboard automation speedup. Supports importing actual `alert_quality.py --json` data for more accurate projections.

**Use case:** Platform evaluation phase — presenting quantifiable TCO savings to decision-makers.

---

## How to Use

These `.jsx` files can run directly in the following environments:

1. **GitHub Pages (public access, recommended)** — Go to repo Settings → Pages → Source, select `main` / `/docs`. The landing page at `docs/interactive/index.html` lets visitors try all tools in the browser. Components are transpiled client-side via `docs/assets/jsx-loader.html` using Babel standalone — no build step required
2. **da-portal Docker Image (enterprise intranet / air-gapped, recommended)** — `docker run -p 8080:80 ghcr.io/vencil/da-portal` to host the full Interactive Tools Portal on your internal network. Supports volume-mount customisation of `platform-data.json` and `flows.json`, and nginx reverse proxy to solve Prometheus CORS issues. See [components/da-portal/](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/da-portal/README.md)
3. **Claude Artifacts** — Paste the `.jsx` content into a conversation and Claude renders it instantly
4. **React dev environment** — Import the component into a `create-react-app` project
5. **CodeSandbox / StackBlitz** — Online instant preview

Each component is a standalone React functional component with no external state management dependencies.

### Local preview

```bash
# Option A: Python http.server (quick verification)
cd docs && python3 -m http.server 8888
# Open http://localhost:8888/interactive/

# Option B: da-portal Docker (mirrors production deployment)
make portal-image && make portal-run
# Open http://localhost:8080
```
