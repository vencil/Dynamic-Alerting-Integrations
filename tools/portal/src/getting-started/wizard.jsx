---
title: "Getting Started Wizard"
tags: [onboarding, guided, 3 min]
audience: [tenant, platform-engineer, domain-expert]
version: v2.7.0
lang: en
related: [onboarding-checklist, architecture-quiz, rule-pack-selector]
---

import { useState, useEffect, useRef } from "react";

// Style tokens (v2.7.0 Phase .a0 DEC-A Option A migration):
// - Core gray/blue/focus palette migrated to Tailwind arbitrary values
//   (bg-[color:var(--da-color-*)] / text-[color:...] / border-[color:...])
//   so theme switching via [data-theme="dark"] works automatically.
// - State-specific colors (green = completed/success, amber = priority,
//   indigo/purple = path comparison) remain as Tailwind utilities pending
//   introduction of domain-specific semantic tokens in a future audit.
// - See design-critique notes in docs/internal/design-reviews/v2.7.0/wizard.md.

// i18n helper — picks zh or en based on jsx-loader's detected language.
//
// This tool ships as a PRE-BUILT esbuild dist bundle (docs/assets/dist/
// wizard.js). The language toggle in jsx-loader.html does a FULL PAGE
// RELOAD (not an in-place React re-mount) — see jsx-loader.html
// setLanguage(). On reload the module is re-evaluated, so this module-level
// `const t` is captured with `window.__t` already set at bootstrap. That is
// why reading `window.__t` once here (rather than via a hook on every
// render) is correct: there is no in-session language flip to react to.
const t = window.__t || ((zh, en) => en);

// Base URL for doc links — GitHub renders .md files natively
const REPO_BASE = "https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main";

// Convert relative doc path to full GitHub URL
function docUrl(relativePath) {
  // relativePath is relative to docs/getting-started/
  // e.g., "for-tenants.md" → docs/getting-started/for-tenants.md
  // e.g., "../architecture-and-design.md" → docs/architecture-and-design.md
  // e.g., "../rule-packs/README.md" → rule-packs/README.md
  let resolved;
  if (relativePath.startsWith("../rule-packs/")) {
    resolved = relativePath.replace("../", "");
  } else if (relativePath.startsWith("../")) {
    resolved = "docs/" + relativePath.replace("../", "");
  } else {
    resolved = "docs/getting-started/" + relativePath;
  }
  return `${REPO_BASE}/${resolved}`;
}

// Inline concept glossary — helps newcomers understand platform terminology
const GLOSSARY = {
  "Rule Pack": t("針對特定技術（例如 MariaDB、Redis）預先打包好的 Prometheus recording rule 與 alert rule。挑你需要的即可 — 不需要 PromQL。", "A pre-built bundle of Prometheus recording rules and alert rules for a specific technology (e.g., MariaDB, Redis). You pick the ones you need — no PromQL required."),
  "Threshold": t("觸發告警的數值上限（例如「80% CPU」）。每個租戶用簡單 YAML 設定自己的值。", "A numeric limit (like \"80% CPU\") that triggers an alert. Each tenant sets their own values in simple YAML."),
  "Tenant": t("擁有一組服務的團隊或 namespace。每個租戶都有隔離的設定與告警路由。", "A team or namespace that owns a set of services. Each tenant has isolated config and alert routing."),
  "Three-State Mode": t("每個設定鍵支援三種狀態：自訂值、預設（省略該鍵）、或明確停用（設為「disable」）。", "Every config key supports three states: custom value, default (omit the key), or explicitly disabled (set to \"disable\")."),
  "Recording Rule": t("預先計算指標以加速查詢的 Prometheus 規則。Rule Pack 會自動包含這些。", "A Prometheus rule that pre-computes metrics for faster queries. Rule Packs include these automatically."),
  "Severity Dedup": t("當 Critical 告警觸發時，對應的 Warning 會自動被抑制以減少噪音。", "When a Critical alert fires, the matching Warning is automatically suppressed to reduce noise."),
};

const GlossaryTip = ({ term }) => {
  const [show, setShow] = useState(false);
  const def = GLOSSARY[term];
  if (!def) return <span className="font-semibold">{term}</span>;
  return (
    <span className="relative inline-block">
      <button
        type="button"
        onClick={() => setShow(!show)}
        aria-expanded={show}
        className="font-semibold text-[color:var(--da-color-accent)] underline decoration-dotted underline-offset-4 cursor-help"
      >
        {term}
      </button>
      {show && (
        <span className="absolute z-10 left-0 top-full mt-1 w-64 p-3 bg-[color:var(--da-color-toast-bg)] text-[color:var(--da-color-hero-fg)] text-xs rounded-lg shadow-lg leading-relaxed">
          {def}
          <button type="button" onClick={() => setShow(false)} className="block mt-1 text-[color:var(--da-color-link-on-dark)] text-xs hover:underline">{t("關閉", "close")}</button>
        </span>
      )}
    </span>
  );
};

const ROLES = [
  {
    id: "platform",
    label: t("平台工程師", "Platform Engineer"),
    icon: "⚙️",
    desc: t("部署、擴展並維運告警基礎設施", "Deploy, scale, and operate the alerting infrastructure"),
    hint: t("你在組織內管理 Kubernetes、Prometheus 或 Helm。", "You manage Kubernetes, Prometheus, or Helm in your org."),
  },
  {
    id: "domain",
    label: t("領域專家 (DBA)", "Domain Expert (DBA)"),
    icon: "🗄️",
    desc: t("定義你的資料庫怎樣算「不健康」", "Define what \"unhealthy\" means for your databases"),
    hint: t("你熟悉資料庫內部，想設定正確的閾值。", "You know your DB internals and want to set the right thresholds."),
  },
  {
    id: "tenant",
    label: t("租戶團隊", "Tenant Team"),
    icon: "👥",
    desc: t("為你團隊的服務取得告警 — 不需要 PromQL", "Get alerts for your team's services — no PromQL needed"),
    hint: t("你只想在對的頻道收到對的告警。", "You just want to receive the right alerts in the right channel."),
  },
];

const GOALS = {
  platform: [
    { id: "setup", label: t("初始部署", "Initial Setup"), desc: t("從零開始部署平台", "Deploy the platform from scratch") },
    { id: "migration", label: t("遷移", "Migration"), desc: t("從既有告警系統遷移", "Migrate from existing alerting systems") },
    { id: "federation", label: t("聯邦", "Federation"), desc: t("設定多叢集聯邦", "Set up multi-cluster federation") },
    { id: "monitoring", label: t("監控與擴展", "Monitoring & Scaling"), desc: t("監控並擴展平台", "Monitor and scale the platform") },
  ],
};

const DATABASES = {
  domain: [
    { id: "mariadb", label: "MariaDB", icon: "🗄️" },
    { id: "postgresql", label: "PostgreSQL", icon: "🗄️" },
    { id: "redis", label: "Redis", icon: "⚡" },
    { id: "mongodb", label: "MongoDB", icon: "📦" },
    { id: "other", label: t("其他", "Other"), icon: "❓" },
  ],
};

const NEEDS = {
  tenant: [
    { id: "onboard", label: t("加入平台", "Onboard to Platform"), desc: t("讓我的團隊開始上手", "Get my team started") },
    { id: "alerts", label: t("設定告警", "Configure Alerts"), desc: t("設定告警規則與閾值", "Set up alert rules and thresholds") },
    { id: "routing", label: t("設定告警路由", "Set Up Alert Routing"), desc: t("控制告警送往何處", "Control where alerts are sent") },
    { id: "maintenance", label: t("維護模式", "Maintenance Mode"), desc: t("管理告警抑制視窗", "Manage alert suppression windows") },
  ],
};

// ── Per-role lifecycle axis (#811) ───────────────────────────────────────
// step-1 groups a role's options into lifecycle buckets (Provision / Operate
// / Maintain) so a single role sees a focused, staged path instead of a flat
// dump. This is a DISPLAY-ONLY grouping layered over the option ids above —
// RECOMMENDATIONS data and the deep-link `option=` schema are unchanged, so a
// bookmarked #role=tenant&option=routing still resolves (bucketing never
// gates which options exist, only how they are headed).
//
// `axis: "flat"`  → render the current flat OptionCard grid (no sub-headings).
// `axis: "lifecycle"` → render each non-empty bucket as an <h4> + its grid.
//   `buckets` is ordered; each bucket lists the optionIds it contains.
//
// domain is deliberately FLAT: its branches (mariadb / postgresql / redis /
// mongodb / other) are TYPES, not lifecycle stages — forcing a stage axis on
// them would be a false taxonomy (#811 explicit requirement).
const BUCKET_LABELS = {
  provision: () => t("建置", "Provision"),
  operate: () => t("維運", "Operate"),
  maintain: () => t("維護", "Maintain"),
};

const ROLE_AXIS = {
  platform: {
    axis: "lifecycle",
    buckets: [
      // federation is filed under Provision (not its own stage): standing up
      // multi-cluster wiring is a build-time activity, so it groups with the
      // other "get it running" goals rather than day-2 operate/maintain.
      { id: "provision", optionIds: ["setup", "migration", "federation"] },
      { id: "operate", optionIds: ["monitoring"] },
    ],
  },
  tenant: {
    axis: "lifecycle",
    buckets: [
      { id: "provision", optionIds: ["onboard"] },
      { id: "operate", optionIds: ["alerts", "routing"] },
      { id: "maintain", optionIds: ["maintenance"] },
    ],
  },
  domain: {
    // FLAT — db-type branches are types, not stages (see note above).
    axis: "flat",
  },
};

// ── Grow-ops action handoff (#811) ───────────────────────────────────────
// After the curated reading list, point each role at the interactive tool(s)
// that are its natural next action — reusing the master-onboarding handoff
// pattern (a plain anchor card with `href: '<tool>.html'`, opens the existing
// tool page). These are the SAME portal pages jsx-loader serves; we link by
// bare `<tool>.html` (sibling pages), never a portal-absolute `/foo` (TRK-104).
//
// DOC-ONLY SEAM: #811 deliberately ships links only. A future D2 "emit YAML
// skeleton" step (option 2) would slot in HERE — e.g. a button that hands the
// chosen role+option to recipe-builder pre-filled — but no emit/generate logic
// is built now (out of #811 scope). Keep this list link-only until then.
const HANDOFF_TARGETS = {
  tenant: [
    { href: "recipe-builder.html", label: () => t("配方建構器", "Recipe Builder"), desc: () => t("用引導表單組出 custom alert 配方。", "Build a custom-alert recipe with a guided form.") },
    { href: "playground.html", label: () => t("互動沙盒", "Playground"), desc: () => t("在沙盒中試玩 tenant 設定。", "Experiment with tenant config in a sandbox.") },
  ],
  platform: [
    { href: "deployment-wizard.html", label: () => t("部署精靈", "Deployment Wizard"), desc: () => t("產出 Helm values 或 Kustomize patch。", "Generate Helm values or a Kustomize patch.") },
    { href: "cli-playground.html", label: () => t("CLI 沙盒", "CLI Playground"), desc: () => t("試跑 da-tools 指令。", "Try da-tools commands interactively.") },
  ],
  domain: [
    { href: "rule-pack-selector.html", label: () => t("Rule Pack 選擇器", "Rule Pack Selector"), desc: () => t("挑出你資料庫需要的 rule pack。", "Pick the rule packs your database needs.") },
    { href: "threshold-calculator.html", label: () => t("閾值計算器", "Threshold Calculator"), desc: () => t("從觀測指標推算建議閾值。", "Derive suggested thresholds from observed metrics.") },
  ],
};

const RECOMMENDATIONS = {
  "platform-setup": {
    title: t("平台初始部署", "Platform Initial Setup"),
    docs: [
      { name: t("平台工程師快速上手", "Platform Engineer Quick Start"), path: "for-platform-engineers.md", priority: "start-here", summary: t("端到端部署平台：Helm 安裝、設定與驗證。", "Deploy the platform end-to-end: Helm install, config, and verify.") },
      { name: t("架構與設計", "Architecture & Design"), path: "../architecture-and-design.md", summary: t("核心概念：group_left 配對、三態模式、severity 去重。", "Core concepts: group_left matching, three-state mode, severity dedup.") },
      { name: t("自帶 Prometheus 整合", "BYO Prometheus Integration"), path: "../byo-prometheus-integration.md", summary: t("整合你既有的 Prometheus 或 Thanos。", "Integrate with your existing Prometheus or Thanos setup.") },
      { name: t("自帶 Alertmanager 整合", "BYO Alertmanager Integration"), path: "../byo-alertmanager-integration.md", summary: t("以動態路由連接既有的 Alertmanager。", "Connect to an existing Alertmanager with dynamic routing.") },
      { name: t("GitOps 部署指南", "GitOps Deployment Guide"), path: "../gitops-deployment.md", summary: t("ArgoCD / Flux 工作流、CI 漂移偵測。", "ArgoCD / Flux workflows, CI drift detection.") },
      { name: t("自訂規則治理", "Custom Rule Governance"), path: "../custom-rule-governance.md", summary: t("三層治理模型、CI 檢查、命名慣例。", "Three-tier governance model, CI linting, naming conventions.") },
    ],
  },
  "platform-migration": {
    title: t("從舊系統遷移", "Migration from Legacy Systems"),
    docs: [
      { name: t("平台工程師快速上手", "Platform Engineer Quick Start"), path: "for-platform-engineers.md", priority: "start-here", summary: t("端到端部署平台：Helm 安裝、設定與驗證。", "Deploy the platform end-to-end: Helm install, config, and verify.") },
      { name: t("遷移引擎指南", "Migration Engine Guide"), path: "../migration-engine.md", summary: t("以 AST 為基礎的 PromQL 轉 YAML 轉換器內部機制。", "AST-based PromQL-to-YAML converter internals.") },
      { name: t("遷移使用者指南", "Migration User Guide"), path: "../migration-guide.md", summary: t("逐步導入流程，搭配 scaffold 與 migrate 工具。", "Step-by-step onboarding flow with scaffold and migrate tools.") },
      { name: t("進階情境", "Advanced Scenarios"), path: "../internal/test-coverage-matrix.md", summary: t("Regex 閾值、排程值、跨午夜設定。", "Regex thresholds, scheduled values, cross-midnight configs.") },
      { name: t("Shadow Monitoring SOP", "Shadow Monitoring SOP"), path: "../shadow-monitoring-sop.md", summary: t("新舊規則雙軌並行、自動收斂偵測。", "Dual-track old/new rules, auto-convergence detection.") },
      { name: t("Shadow Monitoring 切換", "Shadow Monitoring Cutover"), path: "../scenarios/shadow-monitoring-cutover.md", summary: t("零風險切換：就緒檢查、一鍵切換、回滾。", "Zero-risk cutover: readiness check, one-click switch, rollback.") },
    ],
  },
  "platform-federation": {
    title: t("多叢集聯邦", "Multi-Cluster Federation"),
    docs: [
      { name: t("平台工程師快速上手", "Platform Engineer Quick Start"), path: "for-platform-engineers.md", priority: "start-here", summary: t("端到端部署平台：Helm 安裝、設定與驗證。", "Deploy the platform end-to-end: Helm install, config, and verify.") },
      { name: t("聯邦整合指南", "Federation Integration Guide"), path: "../federation-integration.md", summary: t("情境 A 藍圖：中央 exporter + 邊緣 Prometheus。", "Scenario A blueprint: central exporter + edge Prometheus.") },
      { name: t("多叢集聯邦情境", "Multi-Cluster Federation Scenarios"), path: "../scenarios/multi-cluster-federation.md", summary: t("邊緣到中央的指標流、跨叢集告警路由。", "Edge-to-central metric flow, cross-cluster alert routing.") },
      { name: t("架構與設計", "Architecture & Design"), path: "../architecture-and-design.md", summary: t("核心概念：group_left 配對、三態模式、severity 去重。", "Core concepts: group_left matching, three-state mode, severity dedup.") },
      { name: t("治理與安全", "Governance & Security"), path: "../governance-security.md", summary: t("RBAC、webhook 允許清單、cardinality 防護。", "RBAC, webhook allowlists, cardinality guards.") },
    ],
  },
  "platform-monitoring": {
    title: t("平台監控與擴展", "Platform Monitoring & Scaling"),
    docs: [
      { name: t("平台工程師快速上手", "Platform Engineer Quick Start"), path: "for-platform-engineers.md", priority: "start-here", summary: t("端到端部署平台：Helm 安裝、設定與驗證。", "Deploy the platform end-to-end: Helm install, config, and verify.") },
      { name: t("基準測試與效能", "Benchmarks & Performance"), path: "../benchmarks.md", summary: t("閒置、負載中、路由、alertmanager 與 reload 基準測試。", "Idle, under-load, routing, alertmanager, and reload benchmarks.") },
      { name: t("疑難排解指南", "Troubleshooting Guide"), path: "../troubleshooting.md", summary: t("常見問題、診斷指令、復原程序。", "Common issues, diagnostic commands, recovery procedures.") },
      { name: t("架構與設計", "Architecture & Design"), path: "../architecture-and-design.md", summary: t("核心概念：group_left 配對、三態模式、severity 去重。", "Core concepts: group_left matching, three-state mode, severity dedup.") },
      { name: t("治理與安全", "Governance & Security"), path: "../governance-security.md", summary: t("RBAC、webhook 允許清單、cardinality 防護。", "RBAC, webhook allowlists, cardinality guards.") },
    ],
  },
  "domain-mariadb": {
    title: t("MariaDB 閾值設定", "MariaDB Threshold Configuration"),
    docs: [
      { name: t("領域專家快速上手", "Domain Expert Quick Start"), path: "for-domain-experts.md", priority: "start-here", summary: t("理解三層 Rule Pack 架構並設定閾值。", "Learn the three-layer Rule Pack architecture and set thresholds.") },
      { name: t("租戶快速上手", "Tenant Quick Start"), path: "for-tenants.md", summary: t("每個租戶 30 秒內該知道的 3 件事。", "The 3 things every tenant needs to know in 30 seconds.") },
      { name: t("告警參考", "Alert Reference"), path: "../rule-packs/ALERT-REFERENCE.md", summary: t("全部 99 個告警，含 severity、意義與建議動作。", "All 99 alerts with severity, meaning, and suggested actions.") },
      { name: t("Rule Pack 總覽", "Rule Packs Overview"), path: "../rule-packs/README.md", summary: t("15 個 rule pack：各自涵蓋哪些指標、exporter 需求。", "15 rule packs: which metrics they cover, exporter requirements.") },
      { name: t("架構與設計", "Architecture & Design"), path: "../architecture-and-design.md", summary: t("核心概念：group_left 配對、三態模式、severity 去重。", "Core concepts: group_left matching, three-state mode, severity dedup.") },
      { name: t("基線探勘工具", "Baseline Discovery Tool"), path: "../internal/testing-playbook.md", summary: t("觀測真實指標並計算 p50/p90/p99 閾值建議。", "Observe real metrics and compute p50/p90/p99 threshold suggestions.") },
    ],
  },
  "domain-postgresql": {
    title: t("PostgreSQL 閾值設定", "PostgreSQL Threshold Configuration"),
    docs: [
      { name: t("領域專家快速上手", "Domain Expert Quick Start"), path: "for-domain-experts.md", priority: "start-here", summary: t("理解三層 Rule Pack 架構並設定閾值。", "Learn the three-layer Rule Pack architecture and set thresholds.") },
      { name: t("租戶快速上手", "Tenant Quick Start"), path: "for-tenants.md", summary: t("每個租戶 30 秒內該知道的 3 件事。", "The 3 things every tenant needs to know in 30 seconds.") },
      { name: t("告警參考", "Alert Reference"), path: "../rule-packs/ALERT-REFERENCE.md", summary: t("全部 99 個告警，含 severity、意義與建議動作。", "All 99 alerts with severity, meaning, and suggested actions.") },
      { name: t("Rule Pack 總覽", "Rule Packs Overview"), path: "../rule-packs/README.md", summary: t("15 個 rule pack：各自涵蓋哪些指標、exporter 需求。", "15 rule packs: which metrics they cover, exporter requirements.") },
      { name: t("架構與設計", "Architecture & Design"), path: "../architecture-and-design.md", summary: t("核心概念：group_left 配對、三態模式、severity 去重。", "Core concepts: group_left matching, three-state mode, severity dedup.") },
      { name: t("基線探勘工具", "Baseline Discovery Tool"), path: "../internal/testing-playbook.md", summary: t("觀測真實指標並計算 p50/p90/p99 閾值建議。", "Observe real metrics and compute p50/p90/p99 threshold suggestions.") },
    ],
  },
  "domain-redis": {
    title: t("Redis 閾值設定", "Redis Threshold Configuration"),
    docs: [
      { name: t("領域專家快速上手", "Domain Expert Quick Start"), path: "for-domain-experts.md", priority: "start-here", summary: t("理解三層 Rule Pack 架構並設定閾值。", "Learn the three-layer Rule Pack architecture and set thresholds.") },
      { name: t("租戶快速上手", "Tenant Quick Start"), path: "for-tenants.md", summary: t("每個租戶 30 秒內該知道的 3 件事。", "The 3 things every tenant needs to know in 30 seconds.") },
      { name: t("告警參考", "Alert Reference"), path: "../rule-packs/ALERT-REFERENCE.md", summary: t("全部 99 個告警，含 severity、意義與建議動作。", "All 99 alerts with severity, meaning, and suggested actions.") },
      { name: t("Rule Pack 總覽", "Rule Packs Overview"), path: "../rule-packs/README.md", summary: t("15 個 rule pack：各自涵蓋哪些指標、exporter 需求。", "15 rule packs: which metrics they cover, exporter requirements.") },
      { name: t("架構與設計", "Architecture & Design"), path: "../architecture-and-design.md", summary: t("核心概念：group_left 配對、三態模式、severity 去重。", "Core concepts: group_left matching, three-state mode, severity dedup.") },
      { name: t("基線探勘工具", "Baseline Discovery Tool"), path: "../internal/testing-playbook.md", summary: t("觀測真實指標並計算 p50/p90/p99 閾值建議。", "Observe real metrics and compute p50/p90/p99 threshold suggestions.") },
    ],
  },
  "domain-mongodb": {
    title: t("MongoDB 閾值設定", "MongoDB Threshold Configuration"),
    docs: [
      { name: t("領域專家快速上手", "Domain Expert Quick Start"), path: "for-domain-experts.md", priority: "start-here", summary: t("理解三層 Rule Pack 架構並設定閾值。", "Learn the three-layer Rule Pack architecture and set thresholds.") },
      { name: t("租戶快速上手", "Tenant Quick Start"), path: "for-tenants.md", summary: t("每個租戶 30 秒內該知道的 3 件事。", "The 3 things every tenant needs to know in 30 seconds.") },
      { name: t("告警參考", "Alert Reference"), path: "../rule-packs/ALERT-REFERENCE.md", summary: t("全部 99 個告警，含 severity、意義與建議動作。", "All 99 alerts with severity, meaning, and suggested actions.") },
      { name: t("Rule Pack 總覽", "Rule Packs Overview"), path: "../rule-packs/README.md", summary: t("15 個 rule pack：各自涵蓋哪些指標、exporter 需求。", "15 rule packs: which metrics they cover, exporter requirements.") },
      { name: t("架構與設計", "Architecture & Design"), path: "../architecture-and-design.md", summary: t("核心概念：group_left 配對、三態模式、severity 去重。", "Core concepts: group_left matching, three-state mode, severity dedup.") },
      { name: t("基線探勘工具", "Baseline Discovery Tool"), path: "../internal/testing-playbook.md", summary: t("觀測真實指標並計算 p50/p90/p99 閾值建議。", "Observe real metrics and compute p50/p90/p99 threshold suggestions.") },
    ],
  },
  "domain-other": {
    title: t("自訂閾值設定", "Custom Threshold Configuration"),
    docs: [
      { name: t("領域專家快速上手", "Domain Expert Quick Start"), path: "for-domain-experts.md", priority: "start-here", summary: t("理解三層 Rule Pack 架構並設定閾值。", "Learn the three-layer Rule Pack architecture and set thresholds.") },
      { name: t("租戶快速上手", "Tenant Quick Start"), path: "for-tenants.md", summary: t("每個租戶 30 秒內該知道的 3 件事。", "The 3 things every tenant needs to know in 30 seconds.") },
      { name: t("告警參考", "Alert Reference"), path: "../rule-packs/ALERT-REFERENCE.md", summary: t("全部 99 個告警，含 severity、意義與建議動作。", "All 99 alerts with severity, meaning, and suggested actions.") },
      { name: t("自訂規則治理", "Custom Rule Governance"), path: "../custom-rule-governance.md", summary: t("三層治理模型、CI 檢查、命名慣例。", "Three-tier governance model, CI linting, naming conventions.") },
      { name: t("架構與設計", "Architecture & Design"), path: "../architecture-and-design.md", summary: t("核心概念：group_left 配對、三態模式、severity 去重。", "Core concepts: group_left matching, three-state mode, severity dedup.") },
      { name: t("進階情境", "Advanced Scenarios"), path: "../internal/test-coverage-matrix.md", summary: t("Regex 閾值、排程值、跨午夜設定。", "Regex thresholds, scheduled values, cross-midnight configs.") },
    ],
  },
  "tenant-onboard": {
    title: t("讓你的團隊上手", "Getting Your Team Onboarded"),
    docs: [
      { name: t("租戶快速上手", "Tenant Quick Start"), path: "for-tenants.md", priority: "start-here", summary: t("每個租戶 30 秒內該知道的 3 件事。", "The 3 things every tenant needs to know in 30 seconds.") },
      { name: t("租戶生命週期情境", "Tenant Lifecycle Scenarios"), path: "../scenarios/tenant-lifecycle.md", summary: t("Scaffold → 驗證 → 部署 → 下線的完整生命週期。", "Scaffold → validate → deploy → offboard full lifecycle.") },
      { name: t("告警參考", "Alert Reference"), path: "../rule-packs/ALERT-REFERENCE.md", summary: t("全部 99 個告警，含 severity、意義與建議動作。", "All 99 alerts with severity, meaning, and suggested actions.") },
      { name: t("Rule Pack 總覽", "Rule Packs Overview"), path: "../rule-packs/README.md", summary: t("15 個 rule pack：各自涵蓋哪些指標、exporter 需求。", "15 rule packs: which metrics they cover, exporter requirements.") },
      { name: t("遷移指南", "Migration Guide"), path: "../migration-guide.md", summary: t("逐步導入流程，搭配 scaffold 與 migrate 工具。", "Step-by-step onboarding flow with scaffold and migrate tools.") },
    ],
  },
  "tenant-alerts": {
    title: t("為你的服務設定告警", "Configure Alerts for Your Services"),
    docs: [
      { name: t("租戶快速上手", "Tenant Quick Start"), path: "for-tenants.md", priority: "start-here", summary: t("每個租戶 30 秒內該知道的 3 件事。", "The 3 things every tenant needs to know in 30 seconds.") },
      { name: t("告警參考", "Alert Reference"), path: "../rule-packs/ALERT-REFERENCE.md", summary: t("全部 99 個告警，含 severity、意義與建議動作。", "All 99 alerts with severity, meaning, and suggested actions.") },
      { name: t("Rule Pack 總覽", "Rule Packs Overview"), path: "../rule-packs/README.md", summary: t("15 個 rule pack：各自涵蓋哪些指標、exporter 需求。", "15 rule packs: which metrics they cover, exporter requirements.") },
      { name: t("架構與設計", "Architecture & Design"), path: "../architecture-and-design.md", summary: t("核心概念：group_left 配對、三態模式、severity 去重。", "Core concepts: group_left matching, three-state mode, severity dedup.") },
      { name: t("進階情境", "Advanced Scenarios"), path: "../internal/test-coverage-matrix.md", summary: t("Regex 閾值、排程值、跨午夜設定。", "Regex thresholds, scheduled values, cross-midnight configs.") },
      { name: t("疑難排解指南", "Troubleshooting Guide"), path: "../troubleshooting.md", summary: t("常見問題、診斷指令、復原程序。", "Common issues, diagnostic commands, recovery procedures.") },
    ],
  },
  "tenant-routing": {
    title: t("設定告警路由與通知", "Set Up Alert Routing & Notifications"),
    docs: [
      { name: t("租戶快速上手", "Tenant Quick Start"), path: "for-tenants.md", priority: "start-here", summary: t("每個租戶 30 秒內該知道的 3 件事。", "The 3 things every tenant needs to know in 30 seconds.") },
      { name: t("告警路由分流 (NOC vs 租戶)", "Alert Routing Split (NOC vs Tenant)"), path: "../scenarios/alert-routing-split.md", summary: t("雙視角通知：NOC 收到 platform_summary、租戶收到 summary。", "Dual-perspective notifications: NOC gets platform_summary, tenant gets summary.") },
      { name: t("租戶生命週期情境", "Tenant Lifecycle Scenarios"), path: "../scenarios/tenant-lifecycle.md", summary: t("Scaffold → 驗證 → 部署 → 下線的完整生命週期。", "Scaffold → validate → deploy → offboard full lifecycle.") },
      { name: t("疑難排解指南", "Troubleshooting Guide"), path: "../troubleshooting.md", summary: t("常見問題、診斷指令、復原程序。", "Common issues, diagnostic commands, recovery procedures.") },
      { name: t("架構與設計", "Architecture & Design"), path: "../architecture-and-design.md", summary: t("核心概念：group_left 配對、三態模式、severity 去重。", "Core concepts: group_left matching, three-state mode, severity dedup.") },
    ],
  },
  "tenant-maintenance": {
    title: t("管理維護視窗與靜音", "Manage Maintenance Windows & Silences"),
    docs: [
      { name: t("租戶快速上手", "Tenant Quick Start"), path: "for-tenants.md", priority: "start-here", summary: t("每個租戶 30 秒內該知道的 3 件事。", "The 3 things every tenant needs to know in 30 seconds.") },
      { name: t("租戶生命週期情境", "Tenant Lifecycle Scenarios"), path: "../scenarios/tenant-lifecycle.md", summary: t("Scaffold → 驗證 → 部署 → 下線的完整生命週期。", "Scaffold → validate → deploy → offboard full lifecycle.") },
      { name: t("架構與設計", "Architecture & Design"), path: "../architecture-and-design.md", summary: t("核心概念：group_left 配對、三態模式、severity 去重。", "Core concepts: group_left matching, three-state mode, severity dedup.") },
      { name: t("進階情境", "Advanced Scenarios"), path: "../internal/test-coverage-matrix.md", summary: t("Regex 閾值、排程值、跨午夜設定。", "Regex thresholds, scheduled values, cross-midnight configs.") },
      { name: t("疑難排解指南", "Troubleshooting Guide"), path: "../troubleshooting.md", summary: t("常見問題、診斷指令、復原程序。", "Common issues, diagnostic commands, recovery procedures.") },
    ],
  },
};

const ProgressIndicator = ({ step, totalSteps }) => {
  // Step names so the state is also conveyed in the accessible name (a11y:
  // the active step is not signalled by colour/ring alone — aria-current +
  // a "(done)/(current)" suffix carry it for screen readers).
  const stepNames = [t("角色", "Role"), t("選項", "Options"), t("文件", "Docs")];
  const items = [];
  for (let i = 0; i < totalSteps; i++) {
    const stateLabel = i < step ? t("已完成", "done") : i === step ? t("目前", "current") : t("未開始", "upcoming");
    const name = stepNames[i] || (i + 1);
    items.push(
      <div
        key={`circle-${i}`}
        aria-current={i === step ? "step" : undefined}
        aria-label={`${i + 1}. ${name} (${stateLabel})`}
        className={`flex-shrink-0 flex items-center justify-center w-10 h-10 rounded-full font-bold transition-all ${
          i < step
            ? "bg-[color:var(--da-color-accent)] text-[color:var(--da-color-accent-fg)]"
            : i === step
              ? "bg-[color:var(--da-color-accent)] text-[color:var(--da-color-accent-fg)] ring-4 ring-[color:var(--da-color-focus-ring)]"
              : "bg-[color:var(--da-color-surface-border)] text-[color:var(--da-color-muted)]"
        }`}
      >
        {i < step ? <span aria-hidden="true">✓</span> : i + 1}
      </div>
    );
    if (i < totalSteps - 1) {
      items.push(
        <div
          key={`bar-${i}`}
          aria-hidden="true"
          className={`flex-1 h-1 mx-2 rounded transition-all ${
            i < step ? "bg-[color:var(--da-color-accent)]" : "bg-[color:var(--da-color-surface-border)]"
          }`}
        />
      );
    }
  }
  return (
    <nav aria-label={t("進度", "Progress")} className="flex items-center mb-8">
      {items}
    </nav>
  );
};

const RoleCard = ({ role, isSelected, onClick }) => {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={isSelected}
      className={`p-6 rounded-lg border-2 transition-all text-left hover:shadow-lg ${
        isSelected
          ? "border-[color:var(--da-color-accent)] bg-[color:var(--da-color-accent-soft)] shadow-lg"
          : "border-[color:var(--da-color-surface-border)] bg-[color:var(--da-color-surface)] hover:border-[color:var(--da-color-accent)]"
      }`}
    >
      <div className="text-3xl mb-3" aria-hidden="true">{role.icon}</div>
      <h3 className="text-lg font-bold text-[color:var(--da-color-fg)] mb-2">{role.label}</h3>
      <p className="text-sm text-[color:var(--da-color-muted)] mb-2">{role.desc}</p>
      {role.hint && <p className="text-xs text-[color:var(--da-color-muted)] italic">{role.hint}</p>}
    </button>
  );
};

const OptionCard = ({ option, isSelected, onClick, icon = null }) => {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={isSelected}
      className={`p-4 rounded-lg border-2 transition-all text-left hover:shadow-md ${
        isSelected
          ? "border-[color:var(--da-color-accent)] bg-[color:var(--da-color-accent-soft)]"
          : "border-[color:var(--da-color-surface-border)] bg-[color:var(--da-color-surface)] hover:border-[color:var(--da-color-accent)]"
      }`}
    >
      {icon && <div className="text-2xl mb-2" aria-hidden="true">{icon}</div>}
      <h3 className="text-base font-semibold text-[color:var(--da-color-fg)] mb-1">
        {option.label}
      </h3>
      {option.desc && (
        <p className="text-sm text-[color:var(--da-color-muted)]">{option.desc}</p>
      )}
    </button>
  );
};

const DocumentLink = ({ doc, isRead, onToggleRead }) => {
  const isPriority = doc.priority === "start-here";
  const href = docUrl(doc.path);
  return (
    <div className={`flex items-center gap-3 p-4 rounded-lg border transition-all hover:shadow-md ${
      isRead ? "border-[color:var(--da-color-success)] bg-[color:var(--da-color-success-soft)]" : isPriority ? "border-[color:var(--da-color-warning)] bg-[color:var(--da-color-warning-soft)] hover:bg-[color:var(--da-color-warning-soft)]" : "border-[color:var(--da-color-surface-border)] bg-[color:var(--da-color-surface)] hover:bg-[color:var(--da-color-surface-hover)]"
    }`}>
      <button
        type="button"
        onClick={(e) => { e.preventDefault(); onToggleRead(doc.path); }}
        className={`flex-shrink-0 w-6 h-6 rounded border-2 flex items-center justify-center transition-colors ${
          isRead ? "bg-[color:var(--da-color-success)] border-[color:var(--da-color-success)] text-[color:var(--da-color-accent-fg)]" : "border-[color:var(--da-color-surface-border)] hover:border-[color:var(--da-color-accent)]"
        }`}
        title={isRead ? t("標記為未讀", "Mark as unread") : t("標記為已讀", "Mark as read")}
      >
        {isRead && <span className="text-xs font-bold"><span aria-hidden="true">✓</span></span>}
      </button>
      <a href={href} target="_blank" rel="noopener noreferrer" className="flex-1 min-w-0">
        <div className="flex items-start justify-between">
          <div className="flex-1">
            <h4 className={`font-semibold text-sm ${isRead ? "text-[color:var(--da-color-success)] line-through" : "text-[color:var(--da-color-fg)]"}`}>
              {doc.name}
              {isPriority && !isRead && (
                <span className="ml-2 inline-block px-2 py-1 bg-[color:var(--da-color-warning-soft)] text-[color:var(--da-color-warning)] text-xs font-bold rounded">
                  {t("從這開始", "START HERE")}
                </span>
              )}
            </h4>
            {doc.summary && (
              <p className="text-xs text-[color:var(--da-color-muted)] mt-1 leading-relaxed">{doc.summary}</p>
            )}
          </div>
          <div className="ml-3 text-lg">→</div>
        </div>
      </a>
    </div>
  );
};

// A/B comparison helper: build the path keys WITHIN a single role.
//
// #811: the compare dropdown must stay inside the user's chosen role — the
// old module-level ALL_PATHS flattened every role, leaking e.g. domain-redis
// into a platform user's compare list (the actual cross-role noise this
// refactor removes). `pathsForRole` filters RECOMMENDATIONS by the
// `<roleId>-` key prefix so a platform user only ever compares platform
// paths, a tenant user only tenant paths, etc.
function pathsForRole(roleId) {
  if (!roleId) return [];
  const prefix = roleId + "-";
  return Object.entries(RECOMMENDATIONS)
    .filter(([key]) => key.startsWith(prefix))
    .map(([key, rec]) => ({ key, label: rec.title }));
}

const PathCompare = ({ currentKey, role, onClose }) => {
  const [compareKey, setCompareKey] = useState(null);
  const currentRec = RECOMMENDATIONS[currentKey];
  const compareRec = compareKey ? RECOMMENDATIONS[compareKey] : null;

  const currentDocs = new Set(currentRec.docs.map(d => d.path));
  const compareDocs = compareRec ? new Set(compareRec.docs.map(d => d.path)) : new Set();
  const sharedDocs = currentRec.docs.filter(d => compareDocs.has(d.path));
  const onlyA = currentRec.docs.filter(d => !compareDocs.has(d.path));
  const onlyB = compareRec ? compareRec.docs.filter(d => !currentDocs.has(d.path)) : [];

  return (
    <div className="bg-[color:var(--da-color-surface)] rounded-lg shadow-sm border border-[color:var(--da-color-accent-border-soft)] p-6 space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="text-lg font-bold text-[color:var(--da-color-fg)]">{t('路徑比較', 'Compare Paths')}</h3>
        <button type="button" onClick={onClose} className="text-[color:var(--da-color-muted)] hover:text-[color:var(--da-color-muted)] text-sm"><span aria-hidden="true">✕</span> {t('關閉', 'Close')}</button>
      </div>
      <div>
        <label className="text-sm font-medium text-[color:var(--da-color-fg)] block mb-2">{t('選擇另一條路徑比較：', 'Compare with another path:')}</label>
        <select
          value={compareKey || ''}
          onChange={(e) => setCompareKey(e.target.value || null)}
          className="w-full px-3 py-2 border border-[color:var(--da-color-surface-border)] rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-[color:var(--da-color-focus-ring)]"
        >
          <option value="">{t('-- 選擇路徑 --', '-- Select a path --')}</option>
          {pathsForRole(role).filter(p => p.key !== currentKey).map(p => (
            <option key={p.key} value={p.key}>{p.label}</option>
          ))}
        </select>
      </div>
      {compareRec && (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4 text-sm">
          <div>
            <h4 className="font-semibold text-[color:var(--da-color-accent-hover)] mb-2">{t('僅在當前路徑', 'Only in your path')} ({onlyA.length})</h4>
            {onlyA.map(d => (
              <div key={d.path} className="py-1 text-[color:var(--da-color-fg)]">{d.name}</div>
            ))}
            {onlyA.length === 0 && <div className="text-[color:var(--da-color-muted)] italic">{t('無', 'None')}</div>}
          </div>
          <div>
            <h4 className="font-semibold text-[color:var(--da-color-success)] mb-2">{t('共同文件', 'Shared')} ({sharedDocs.length})</h4>
            {sharedDocs.map(d => (
              <div key={d.path} className="py-1 text-[color:var(--da-color-fg)]">{d.name}</div>
            ))}
          </div>
          <div>
            <h4 className="font-semibold text-[color:var(--da-color-semantic-other)] mb-2">{t('僅在比較路徑', 'Only in compared path')} ({onlyB.length})</h4>
            {onlyB.map(d => (
              <div key={d.path} className="py-1 text-[color:var(--da-color-fg)]">{d.name}</div>
            ))}
            {onlyB.length === 0 && <div className="text-[color:var(--da-color-muted)] italic">{t('無', 'None')}</div>}
          </div>
        </div>
      )}
    </div>
  );
};

const RecommendationsSummary = ({ recommendations, readDocs, onToggleRead, headingRef }) => {
  const total = recommendations.docs.length;
  const done = recommendations.docs.filter(d => readDocs.has(d.path)).length;
  const progressStyle = { width: (total > 0 ? (done / total) * 100 : 0) + '%' };
  return (
    <div className="space-y-6">
      <div className="bg-[color:var(--da-color-accent-soft)] border border-[color:var(--da-color-accent)] rounded-lg p-6">
        <h2 ref={headingRef} tabIndex={-1} className="text-2xl font-bold text-[color:var(--da-color-fg)] mb-2 focus:outline-none">
          {recommendations.title}
        </h2>
        <p className="text-[color:var(--da-color-muted)]">
          {t('點擊任一文件開始閱讀，優先從「START HERE」開始。', 'Click any document to read. Start with "START HERE" first.')}
        </p>
        <div className="mt-3 flex items-center gap-3">
          <div
            className="flex-1 h-2 bg-[color:var(--da-color-accent-soft)] rounded-full overflow-hidden"
            role="progressbar"
            aria-valuenow={done}
            aria-valuemin={0}
            aria-valuemax={total}
            aria-label={t('閱讀進度', 'Reading progress')}
          >
            <div className="h-full bg-[color:var(--da-color-success)] rounded-full transition-all" style={progressStyle}></div>
          </div>
          <span className="text-sm font-medium text-[color:var(--da-color-muted)]">{done}/{total}</span>
        </div>
      </div>

      <div className="space-y-3">
        {recommendations.docs.map((doc, idx) => (
          <DocumentLink key={idx} doc={doc} isRead={readDocs.has(doc.path)} onToggleRead={onToggleRead} />
        ))}
      </div>
    </div>
  );
};

// Grow-ops handoff card — the "Ready to act?" seam at the end of a role path.
// Renders the role's HANDOFF_TARGETS as anchor cards opening existing tools.
const GrowOpsHandoff = ({ role }) => {
  const targets = HANDOFF_TARGETS[role] || [];
  if (targets.length === 0) return null;
  return (
    <div className="bg-[color:var(--da-color-surface)] rounded-lg shadow-sm border border-[color:var(--da-color-accent-border-soft)] p-6">
      <h3 className="text-lg font-bold text-[color:var(--da-color-fg)] mb-1">
        {t("準備動手了嗎？", "Ready to act?")}
      </h3>
      <p className="text-sm text-[color:var(--da-color-muted)] mb-4">
        {t("讀完後，用這些互動工具把所學付諸實作：", "When you've read enough, put it into practice with these interactive tools:")}
      </p>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        {targets.map((target) => (
          <a
            key={target.href}
            href={target.href}
            className="block p-4 rounded-lg border border-[color:var(--da-color-surface-border)] bg-[color:var(--da-color-surface)] hover:border-[color:var(--da-color-accent)] hover:shadow-md transition-all focus:outline-none focus:ring-2 focus:ring-[color:var(--da-color-focus-ring)]"
          >
            <div className="flex items-center justify-between">
              <span className="text-sm font-semibold text-[color:var(--da-color-accent)]">{target.label()}</span>
              <span className="text-[color:var(--da-color-accent)]" aria-hidden="true">→</span>
            </div>
            <p className="text-xs text-[color:var(--da-color-muted)] mt-1 leading-relaxed">{target.desc()}</p>
          </a>
        ))}
      </div>
    </div>
  );
};

// Read initial state from URL hash (e.g., #role=tenant&option=routing)
function readHash() {
  try {
    const params = new URLSearchParams(window.location.hash.slice(1));
    const read = params.get('read');
    return {
      role: params.get('role'),
      option: params.get('option'),
      readDocs: read ? new Set(read.split(',')) : new Set(),
    };
  } catch (e) { return { role: null, option: null, readDocs: new Set() }; }
}

function writeHash(role, option, readDocs) {
  const parts = [];
  if (role) parts.push('role=' + role);
  if (option) parts.push('option=' + option);
  if (readDocs && readDocs.size > 0) parts.push('read=' + [...readDocs].join(','));
  window.history.replaceState(null, '', parts.length ? '#' + parts.join('&') : window.location.pathname + window.location.search);
}

export default function GettingStartedWizard() {
  const initial = readHash();
  const hasInitialOption = initial.role && initial.option && RECOMMENDATIONS[initial.role + '-' + initial.option];
  const [step, setStep] = useState(hasInitialOption ? 2 : initial.role ? 1 : 0);
  const [selectedRole, setSelectedRole] = useState(initial.role);
  const [selectedOption, setSelectedOption] = useState(initial.option);
  const [recommendationKey, setRecommendationKey] = useState(
    hasInitialOption ? initial.role + '-' + initial.option : null
  );
  const [readDocs, setReadDocs] = useState(initial.readDocs || new Set());
  const [showCompare, setShowCompare] = useState(false);

  // A11y: move keyboard focus to the new step's heading on every step change
  // so screen-reader / keyboard users are not stranded at the top of the page
  // after the visible content swaps. `headingRef` is attached to each step's
  // <h2>; the heading carries tabIndex={-1} so it is programmatically
  // focusable without entering the tab order.
  const headingRef = useRef(null);
  const didMountRef = useRef(false);
  useEffect(() => {
    // Skip the very first paint (don't steal focus on initial load / deep
    // link); only move focus on subsequent step transitions.
    if (!didMountRef.current) { didMountRef.current = true; return; }
    if (headingRef.current) headingRef.current.focus();
  }, [step]);

  const toggleReadDoc = (docPath) => {
    setReadDocs(prev => {
      const next = new Set(prev);
      if (next.has(docPath)) next.delete(docPath); else next.add(docPath);
      writeHash(selectedRole, selectedOption, next);
      return next;
    });
  };

  const getOptionsList = () => {
    if (selectedRole === "platform") return GOALS.platform;
    if (selectedRole === "domain") return DATABASES.domain;
    if (selectedRole === "tenant") return NEEDS.tenant;
    return [];
  };

  const getRecommendationKey = () => {
    if (selectedRole === "platform") return `platform-${selectedOption}`;
    if (selectedRole === "domain") return `domain-${selectedOption}`;
    if (selectedRole === "tenant") return `tenant-${selectedOption}`;
    return null;
  };

  const handleRoleSelect = (roleId) => {
    setSelectedRole(roleId);
    setSelectedOption(null);
    setRecommendationKey(null);
    setStep(1);
    writeHash(roleId, null, readDocs);
    // Persist role to flow state for cross-step data passing
    if (window.__flowSave) window.__flowSave({ role: roleId });
  };

  const handleOptionSelect = (optionId) => {
    setSelectedOption(optionId);
    const key = selectedRole === "platform" ? `platform-${optionId}` : selectedRole === "domain" ? `domain-${optionId}` : `tenant-${optionId}`;
    setRecommendationKey(key);
    setStep(2);
    writeHash(selectedRole, optionId, readDocs);
  };

  const handleStartOver = () => {
    setStep(0);
    setSelectedRole(null);
    setSelectedOption(null);
    setRecommendationKey(null);
    setReadDocs(new Set());
    writeHash(null, null, null);
  };

  const selectedRoleObj = ROLES.find((r) => r.id === selectedRole);
  const optionsList = getOptionsList();
  // Per-role lifecycle axis for step-1 grouping (defaults to flat for an
  // unknown role so a stale deep link can never crash the render).
  const axisForRole = ROLE_AXIS[selectedRole] || { axis: "flat" };
  const selectedOptionObj = optionsList.find((o) => o.id === selectedOption);
  const recommendations = recommendationKey
    ? RECOMMENDATIONS[recommendationKey]
    : null;

  return (
    <div className="min-h-screen bg-[image:var(--da-color-hero-gradient)]">
      <div className="max-w-4xl mx-auto px-4 py-8 sm:py-12">
        {/* Header */}
        <div className="text-center mb-8">
          <h1 className="text-4xl sm:text-5xl font-bold text-[color:var(--da-color-fg)] mb-3">
            {t("動態警報平台", "Dynamic Alerting Platform")}
          </h1>
          <p className="text-lg text-[color:var(--da-color-muted)] mb-4">
            {t("幾秒內找到你的專屬學習路徑", "Find your personalized learning path in seconds")}
          </p>
          {step === 0 && (
            <div className="inline-flex flex-wrap justify-center gap-2 text-xs text-[color:var(--da-color-muted)]">
              <span>{t("第一次接觸？點擊術語了解更多：", "New to the platform? Tap any term to learn more:")}</span>
              {Object.keys(GLOSSARY).map(term => (
                <GlossaryTip key={term} term={term} />
              ))}
            </div>
          )}
        </div>

        {/* Progress Indicator */}
        <ProgressIndicator step={step} totalSteps={3} />

        {/* Step 1: Role Selection */}
        {step === 0 && (
          <div className="space-y-6">
            <div className="bg-[color:var(--da-color-surface)] rounded-lg shadow-sm p-8">
              <h2 ref={headingRef} tabIndex={-1} className="text-2xl font-bold text-[color:var(--da-color-fg)] mb-8 focus:outline-none">
                {t("你的角色是？", "Who are you?")}
              </h2>
              <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                {ROLES.map((role) => (
                  <RoleCard
                    key={role.id}
                    role={role}
                    isSelected={selectedRole === role.id}
                    onClick={() => handleRoleSelect(role.id)}
                  />
                ))}
              </div>
            </div>
          </div>
        )}

        {/* Step 2: Role-Specific Questions */}
        {step === 1 && selectedRoleObj && (
          <div className="space-y-6">
            <div className="bg-[color:var(--da-color-surface)] rounded-lg shadow-sm p-8">
              <h2 ref={headingRef} tabIndex={-1} className="text-2xl font-bold text-[color:var(--da-color-fg)] mb-2 focus:outline-none">
                {selectedRoleObj.label}
              </h2>
              <p className="text-[color:var(--da-color-muted)] mb-8">
                {selectedRoleObj.desc}
              </p>

              <div className="mb-6">
                <h3 className="text-lg font-semibold text-[color:var(--da-color-fg)] mb-4">
                  {selectedRole === "platform" && t("你的目標是？", "What's your goal?")}
                  {selectedRole === "domain" && t("你管理哪種資料庫？", "What database do you manage?")}
                  {selectedRole === "tenant" && t("你需要什麼幫助？", "What do you need help with?")}
                </h3>
                {axisForRole.axis === "lifecycle" ? (
                  // Lifecycle axis: render each NON-EMPTY bucket as an <h4>
                  // sub-heading + a grid of that bucket's OptionCards (the
                  // role's option list filtered to the bucket's optionIds).
                  // This is the ONLY place the lifecycle axis appears —
                  // RECOMMENDATIONS data + the option= deep link are untouched.
                  <div className="space-y-6">
                    {axisForRole.buckets.map((bucket) => {
                      const bucketOptions = optionsList.filter((o) => bucket.optionIds.includes(o.id));
                      if (bucketOptions.length === 0) return null; // skip empty buckets
                      return (
                        <div key={bucket.id}>
                          <h4 className="text-sm font-semibold uppercase tracking-wide text-[color:var(--da-color-muted)] mb-3">
                            {(BUCKET_LABELS[bucket.id] || (() => bucket.id))()}
                          </h4>
                          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                            {bucketOptions.map((option) => (
                              <OptionCard
                                key={option.id}
                                option={option}
                                icon={option.icon}
                                isSelected={selectedOption === option.id}
                                onClick={() => handleOptionSelect(option.id)}
                              />
                            ))}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                ) : (
                  // Flat axis (domain): keep the original flat OptionCard grid —
                  // db-type branches are types, not lifecycle stages.
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                    {optionsList.map((option) => (
                      <OptionCard
                        key={option.id}
                        option={option}
                        icon={option.icon}
                        isSelected={selectedOption === option.id}
                        onClick={() => handleOptionSelect(option.id)}
                      />
                    ))}
                  </div>
                )}
              </div>

              <button
                type="button"
                onClick={handleStartOver}
                className="w-full px-4 py-2 text-sm font-medium text-[color:var(--da-color-fg)] bg-[color:var(--da-color-surface-hover)] rounded-lg hover:bg-[color:var(--da-color-surface-border)] transition-colors"
              >
                {t("返回", "Back")}
              </button>
            </div>
          </div>
        )}

        {/* Step 3: Recommendations */}
        {step === 2 && recommendations && (
          <div className="space-y-6">
            <RecommendationsSummary recommendations={recommendations} readDocs={readDocs} onToggleRead={toggleReadDoc} headingRef={headingRef} />

            {/* Grow-ops handoff: the "Ready to act?" seam to the role's tools */}
            <GrowOpsHandoff role={selectedRole} />

            {showCompare && recommendationKey && (
              <PathCompare currentKey={recommendationKey} role={selectedRole} onClose={() => setShowCompare(false)} />
            )}

            <div className="flex flex-col sm:flex-row gap-3">
              <button
                type="button"
                onClick={() => setStep(1)}
                className="flex-1 px-4 py-3 text-sm font-medium text-[color:var(--da-color-fg)] bg-[color:var(--da-color-surface-hover)] rounded-lg hover:bg-[color:var(--da-color-surface-border)] transition-colors"
              >
                {t("返回", "Back")}
              </button>
              <button
                type="button"
                onClick={() => setShowCompare(!showCompare)}
                aria-pressed={showCompare}
                className={`flex-1 px-4 py-3 text-sm font-medium rounded-lg transition-colors ${
                  showCompare
                    ? 'bg-[color:var(--da-color-accent)] text-[color:var(--da-color-accent-fg)]'
                    : 'bg-[color:var(--da-color-accent-soft)] text-[color:var(--da-color-accent)] hover:bg-[color:var(--da-color-accent-soft)]'
                }`}
              >
                {showCompare ? t('隱藏比較', 'Hide Compare') : t('比較路徑', 'Compare Paths')}
              </button>
              <button
                type="button"
                onClick={handleStartOver}
                className="flex-1 px-4 py-3 text-sm font-medium text-[color:var(--da-color-accent-fg)] bg-[color:var(--da-color-accent)] rounded-lg hover:bg-[color:var(--da-color-accent-hover)] transition-colors"
              >
                {t("重新開始", "Start Over")}
              </button>
            </div>
          </div>
        )}

        {/* Footer */}
        <div className="mt-12 pt-8 border-t border-[color:var(--da-color-surface-border)]">
          <p className="text-center text-sm text-[color:var(--da-color-muted)]">
            {t("有問題嗎？查看", "Questions? Check the")}{" "}
            <a href={docUrl("../troubleshooting.md")} target="_blank" rel="noopener noreferrer" className="text-[color:var(--da-color-accent)] hover:underline">
              {t("疑難排解指南", "Troubleshooting Guide")}
            </a>
            {" "}{t("或", "or")}{" "}
            <a href={docUrl("../context-diagram.md")} target="_blank" rel="noopener noreferrer" className="text-[color:var(--da-color-accent)] hover:underline">
              {t("情境關係圖", "Context Diagram")}
            </a>
          </p>
        </div>
      </div>
    </div>
  );
}
