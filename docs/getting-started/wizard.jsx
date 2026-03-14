import { useState } from "react";

// i18n helper — picks zh or en based on jsx-loader's detected language
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
  "Rule Pack": "A pre-built bundle of Prometheus recording rules and alert rules for a specific technology (e.g., MariaDB, Redis). You pick the ones you need — no PromQL required.",
  "Threshold": "A numeric limit (like \"80% CPU\") that triggers an alert. Each tenant sets their own values in simple YAML.",
  "Tenant": "A team or namespace that owns a set of services. Each tenant has isolated config and alert routing.",
  "Three-State Mode": "Every config key supports three states: custom value, default (omit the key), or explicitly disabled (set to \"disable\").",
  "Recording Rule": "A Prometheus rule that pre-computes metrics for faster queries. Rule Packs include these automatically.",
  "Severity Dedup": "When a Critical alert fires, the matching Warning is automatically suppressed to reduce noise.",
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
        className="font-semibold text-blue-600 underline decoration-dotted underline-offset-4 cursor-help"
      >
        {term}
      </button>
      {show && (
        <span className="absolute z-10 left-0 top-full mt-1 w-64 p-3 bg-gray-900 text-white text-xs rounded-lg shadow-lg leading-relaxed">
          {def}
          <button type="button" onClick={() => setShow(false)} className="block mt-1 text-blue-300 text-xs hover:underline">close</button>
        </span>
      )}
    </span>
  );
};

const ROLES = [
  {
    id: "platform",
    label: "Platform Engineer",
    icon: "⚙️",
    desc: "Deploy, scale, and operate the alerting infrastructure",
    hint: "You manage Kubernetes, Prometheus, or Helm in your org.",
  },
  {
    id: "domain",
    label: "Domain Expert (DBA)",
    icon: "🗄️",
    desc: "Define what \"unhealthy\" means for your databases",
    hint: "You know your DB internals and want to set the right thresholds.",
  },
  {
    id: "tenant",
    label: "Tenant Team",
    icon: "👥",
    desc: "Get alerts for your team's services — no PromQL needed",
    hint: "You just want to receive the right alerts in the right channel.",
  },
];

const GOALS = {
  platform: [
    { id: "setup", label: "Initial Setup", desc: "Deploy the platform from scratch" },
    { id: "migration", label: "Migration", desc: "Migrate from existing alerting systems" },
    { id: "federation", label: "Federation", desc: "Set up multi-cluster federation" },
    { id: "monitoring", label: "Monitoring & Scaling", desc: "Monitor and scale the platform" },
  ],
};

const DATABASES = {
  domain: [
    { id: "mariadb", label: "MariaDB", icon: "🗄️" },
    { id: "postgresql", label: "PostgreSQL", icon: "🗄️" },
    { id: "redis", label: "Redis", icon: "⚡" },
    { id: "mongodb", label: "MongoDB", icon: "📦" },
    { id: "other", label: "Other", icon: "❓" },
  ],
};

const NEEDS = {
  tenant: [
    { id: "onboard", label: "Onboard to Platform", desc: "Get my team started" },
    { id: "alerts", label: "Configure Alerts", desc: "Set up alert rules and thresholds" },
    { id: "routing", label: "Set Up Alert Routing", desc: "Control where alerts are sent" },
    { id: "maintenance", label: "Maintenance Mode", desc: "Manage alert suppression windows" },
  ],
};

const RECOMMENDATIONS = {
  "platform-setup": {
    title: "Platform Initial Setup",
    docs: [
      { name: "Platform Engineer Quick Start", path: "for-platform-engineers.md", priority: "start-here", summary: "Deploy the platform end-to-end: Helm install, config, and verify." },
      { name: "Architecture & Design", path: "../architecture-and-design.md", summary: "Core concepts: group_left matching, three-state mode, severity dedup." },
      { name: "BYO Prometheus Integration", path: "../byo-prometheus-integration.md", summary: "Integrate with your existing Prometheus or Thanos setup." },
      { name: "BYO Alertmanager Integration", path: "../byo-alertmanager-integration.md", summary: "Connect to an existing Alertmanager with dynamic routing." },
      { name: "GitOps Deployment Guide", path: "../gitops-deployment.md", summary: "ArgoCD / Flux workflows, CI drift detection." },
      { name: "Custom Rule Governance", path: "../custom-rule-governance.md", summary: "Three-tier governance model, CI linting, naming conventions." },
    ],
  },
  "platform-migration": {
    title: "Migration from Legacy Systems",
    docs: [
      { name: "Platform Engineer Quick Start", path: "for-platform-engineers.md", priority: "start-here", summary: "Deploy the platform end-to-end: Helm install, config, and verify." },
      { name: "Migration Engine Guide", path: "../migration-engine.md", summary: "AST-based PromQL-to-YAML converter internals." },
      { name: "Migration User Guide", path: "../migration-guide.md", summary: "Step-by-step onboarding flow with scaffold and migrate tools." },
      { name: "Advanced Scenarios", path: "../scenarios/advanced-scenarios.md", summary: "Regex thresholds, scheduled values, cross-midnight configs." },
      { name: "Shadow Monitoring SOP", path: "../shadow-monitoring-sop.md", summary: "Dual-track old/new rules, auto-convergence detection." },
      { name: "Shadow Monitoring Cutover", path: "../scenarios/shadow-monitoring-cutover.md", summary: "Zero-risk cutover: readiness check, one-click switch, rollback." },
    ],
  },
  "platform-federation": {
    title: "Multi-Cluster Federation",
    docs: [
      { name: "Platform Engineer Quick Start", path: "for-platform-engineers.md", priority: "start-here", summary: "Deploy the platform end-to-end: Helm install, config, and verify." },
      { name: "Federation Integration Guide", path: "../federation-integration.md", summary: "Scenario A blueprint: central exporter + edge Prometheus." },
      { name: "Multi-Cluster Federation Scenarios", path: "../scenarios/multi-cluster-federation.md", summary: "Edge-to-central metric flow, cross-cluster alert routing." },
      { name: "Architecture & Design", path: "../architecture-and-design.md", summary: "Core concepts: group_left matching, three-state mode, severity dedup." },
      { name: "Governance & Security", path: "../governance-security.md", summary: "RBAC, webhook allowlists, cardinality guards." },
    ],
  },
  "platform-monitoring": {
    title: "Platform Monitoring & Scaling",
    docs: [
      { name: "Platform Engineer Quick Start", path: "for-platform-engineers.md", priority: "start-here", summary: "Deploy the platform end-to-end: Helm install, config, and verify." },
      { name: "Benchmarks & Performance", path: "../benchmarks.md", summary: "Idle, scaling-curve, under-load, routing, and reload benchmarks." },
      { name: "Troubleshooting Guide", path: "../troubleshooting.md", summary: "Common issues, diagnostic commands, recovery procedures." },
      { name: "Architecture & Design", path: "../architecture-and-design.md", summary: "Core concepts: group_left matching, three-state mode, severity dedup." },
      { name: "Governance & Security", path: "../governance-security.md", summary: "RBAC, webhook allowlists, cardinality guards." },
    ],
  },
  "domain-mariadb": {
    title: "MariaDB Threshold Configuration",
    docs: [
      { name: "Domain Expert Quick Start", path: "for-domain-experts.md", priority: "start-here", summary: "Learn the three-layer Rule Pack architecture and set thresholds." },
      { name: "Tenant Quick Start", path: "for-tenants.md", summary: "The 3 things every tenant needs to know in 30 seconds." },
      { name: "Alert Reference", path: "../rule-packs/ALERT-REFERENCE.md", summary: "All 99 alerts with severity, meaning, and suggested actions." },
      { name: "Rule Packs Overview", path: "../rule-packs/README.md", summary: "15 rule packs: which metrics they cover, exporter requirements." },
      { name: "Architecture & Design", path: "../architecture-and-design.md", summary: "Core concepts: group_left matching, three-state mode, severity dedup." },
      { name: "Baseline Discovery Tool", path: "../internal/testing-playbook.md", summary: "Observe real metrics and compute p50/p90/p99 threshold suggestions." },
    ],
  },
  "domain-postgresql": {
    title: "PostgreSQL Threshold Configuration",
    docs: [
      { name: "Domain Expert Quick Start", path: "for-domain-experts.md", priority: "start-here", summary: "Learn the three-layer Rule Pack architecture and set thresholds." },
      { name: "Tenant Quick Start", path: "for-tenants.md", summary: "The 3 things every tenant needs to know in 30 seconds." },
      { name: "Alert Reference", path: "../rule-packs/ALERT-REFERENCE.md", summary: "All 99 alerts with severity, meaning, and suggested actions." },
      { name: "Rule Packs Overview", path: "../rule-packs/README.md", summary: "15 rule packs: which metrics they cover, exporter requirements." },
      { name: "Architecture & Design", path: "../architecture-and-design.md", summary: "Core concepts: group_left matching, three-state mode, severity dedup." },
      { name: "Baseline Discovery Tool", path: "../internal/testing-playbook.md", summary: "Observe real metrics and compute p50/p90/p99 threshold suggestions." },
    ],
  },
  "domain-redis": {
    title: "Redis Threshold Configuration",
    docs: [
      { name: "Domain Expert Quick Start", path: "for-domain-experts.md", priority: "start-here", summary: "Learn the three-layer Rule Pack architecture and set thresholds." },
      { name: "Tenant Quick Start", path: "for-tenants.md", summary: "The 3 things every tenant needs to know in 30 seconds." },
      { name: "Alert Reference", path: "../rule-packs/ALERT-REFERENCE.md", summary: "All 99 alerts with severity, meaning, and suggested actions." },
      { name: "Rule Packs Overview", path: "../rule-packs/README.md", summary: "15 rule packs: which metrics they cover, exporter requirements." },
      { name: "Architecture & Design", path: "../architecture-and-design.md", summary: "Core concepts: group_left matching, three-state mode, severity dedup." },
      { name: "Baseline Discovery Tool", path: "../internal/testing-playbook.md", summary: "Observe real metrics and compute p50/p90/p99 threshold suggestions." },
    ],
  },
  "domain-mongodb": {
    title: "MongoDB Threshold Configuration",
    docs: [
      { name: "Domain Expert Quick Start", path: "for-domain-experts.md", priority: "start-here", summary: "Learn the three-layer Rule Pack architecture and set thresholds." },
      { name: "Tenant Quick Start", path: "for-tenants.md", summary: "The 3 things every tenant needs to know in 30 seconds." },
      { name: "Alert Reference", path: "../rule-packs/ALERT-REFERENCE.md", summary: "All 99 alerts with severity, meaning, and suggested actions." },
      { name: "Rule Packs Overview", path: "../rule-packs/README.md", summary: "15 rule packs: which metrics they cover, exporter requirements." },
      { name: "Architecture & Design", path: "../architecture-and-design.md", summary: "Core concepts: group_left matching, three-state mode, severity dedup." },
      { name: "Baseline Discovery Tool", path: "../internal/testing-playbook.md", summary: "Observe real metrics and compute p50/p90/p99 threshold suggestions." },
    ],
  },
  "domain-other": {
    title: "Custom Threshold Configuration",
    docs: [
      { name: "Domain Expert Quick Start", path: "for-domain-experts.md", priority: "start-here", summary: "Learn the three-layer Rule Pack architecture and set thresholds." },
      { name: "Tenant Quick Start", path: "for-tenants.md", summary: "The 3 things every tenant needs to know in 30 seconds." },
      { name: "Alert Reference", path: "../rule-packs/ALERT-REFERENCE.md", summary: "All 99 alerts with severity, meaning, and suggested actions." },
      { name: "Custom Rule Governance", path: "../custom-rule-governance.md", summary: "Three-tier governance model, CI linting, naming conventions." },
      { name: "Architecture & Design", path: "../architecture-and-design.md", summary: "Core concepts: group_left matching, three-state mode, severity dedup." },
      { name: "Advanced Scenarios", path: "../scenarios/advanced-scenarios.md", summary: "Regex thresholds, scheduled values, cross-midnight configs." },
    ],
  },
  "tenant-onboard": {
    title: "Getting Your Team Onboarded",
    docs: [
      { name: "Tenant Quick Start", path: "for-tenants.md", priority: "start-here", summary: "The 3 things every tenant needs to know in 30 seconds." },
      { name: "Tenant Lifecycle Scenarios", path: "../scenarios/tenant-lifecycle.md", summary: "Scaffold → validate → deploy → offboard full lifecycle." },
      { name: "Alert Reference", path: "../rule-packs/ALERT-REFERENCE.md", summary: "All 99 alerts with severity, meaning, and suggested actions." },
      { name: "Rule Packs Overview", path: "../rule-packs/README.md", summary: "15 rule packs: which metrics they cover, exporter requirements." },
      { name: "Migration Guide", path: "../migration-guide.md", summary: "Step-by-step onboarding flow with scaffold and migrate tools." },
    ],
  },
  "tenant-alerts": {
    title: "Configure Alerts for Your Services",
    docs: [
      { name: "Tenant Quick Start", path: "for-tenants.md", priority: "start-here", summary: "The 3 things every tenant needs to know in 30 seconds." },
      { name: "Alert Reference", path: "../rule-packs/ALERT-REFERENCE.md", summary: "All 99 alerts with severity, meaning, and suggested actions." },
      { name: "Rule Packs Overview", path: "../rule-packs/README.md", summary: "15 rule packs: which metrics they cover, exporter requirements." },
      { name: "Architecture & Design", path: "../architecture-and-design.md", summary: "Core concepts: group_left matching, three-state mode, severity dedup." },
      { name: "Advanced Scenarios", path: "../scenarios/advanced-scenarios.md", summary: "Regex thresholds, scheduled values, cross-midnight configs." },
      { name: "Troubleshooting Guide", path: "../troubleshooting.md", summary: "Common issues, diagnostic commands, recovery procedures." },
    ],
  },
  "tenant-routing": {
    title: "Set Up Alert Routing & Notifications",
    docs: [
      { name: "Tenant Quick Start", path: "for-tenants.md", priority: "start-here", summary: "The 3 things every tenant needs to know in 30 seconds." },
      { name: "Alert Routing Split (NOC vs Tenant)", path: "../scenarios/alert-routing-split.md", summary: "Dual-perspective notifications: NOC gets platform_summary, tenant gets summary." },
      { name: "Tenant Lifecycle Scenarios", path: "../scenarios/tenant-lifecycle.md", summary: "Scaffold → validate → deploy → offboard full lifecycle." },
      { name: "Troubleshooting Guide", path: "../troubleshooting.md", summary: "Common issues, diagnostic commands, recovery procedures." },
      { name: "Architecture & Design", path: "../architecture-and-design.md", summary: "Core concepts: group_left matching, three-state mode, severity dedup." },
    ],
  },
  "tenant-maintenance": {
    title: "Manage Maintenance Windows & Silences",
    docs: [
      { name: "Tenant Quick Start", path: "for-tenants.md", priority: "start-here", summary: "The 3 things every tenant needs to know in 30 seconds." },
      { name: "Tenant Lifecycle Scenarios", path: "../scenarios/tenant-lifecycle.md", summary: "Scaffold → validate → deploy → offboard full lifecycle." },
      { name: "Architecture & Design", path: "../architecture-and-design.md", summary: "Core concepts: group_left matching, three-state mode, severity dedup." },
      { name: "Advanced Scenarios", path: "../scenarios/advanced-scenarios.md", summary: "Regex thresholds, scheduled values, cross-midnight configs." },
      { name: "Troubleshooting Guide", path: "../troubleshooting.md", summary: "Common issues, diagnostic commands, recovery procedures." },
    ],
  },
};

const ProgressIndicator = ({ step, totalSteps }) => {
  const items = [];
  for (let i = 0; i < totalSteps; i++) {
    items.push(
      <div
        key={`circle-${i}`}
        className={`flex-shrink-0 flex items-center justify-center w-10 h-10 rounded-full font-bold transition-all ${
          i < step
            ? "bg-blue-600 text-white"
            : i === step
              ? "bg-blue-500 text-white ring-4 ring-blue-200"
              : "bg-gray-200 text-gray-500"
        }`}
      >
        {i < step ? "✓" : i + 1}
      </div>
    );
    if (i < totalSteps - 1) {
      items.push(
        <div
          key={`bar-${i}`}
          className={`flex-1 h-1 mx-2 rounded transition-all ${
            i < step ? "bg-blue-600" : "bg-gray-200"
          }`}
        />
      );
    }
  }
  return <div className="flex items-center mb-8">{items}</div>;
};

const RoleCard = ({ role, isSelected, onClick }) => {
  return (
    <button
      onClick={onClick}
      className={`p-6 rounded-lg border-2 transition-all text-left hover:shadow-lg ${
        isSelected
          ? "border-blue-600 bg-blue-50 shadow-lg"
          : "border-gray-200 bg-white hover:border-blue-300"
      }`}
    >
      <div className="text-3xl mb-3">{role.icon}</div>
      <h3 className="text-lg font-bold text-gray-900 mb-2">{role.label}</h3>
      <p className="text-sm text-gray-600 mb-2">{role.desc}</p>
      {role.hint && <p className="text-xs text-gray-400 italic">{role.hint}</p>}
    </button>
  );
};

const OptionCard = ({ option, isSelected, onClick, icon = null }) => {
  return (
    <button
      onClick={onClick}
      className={`p-4 rounded-lg border-2 transition-all text-left hover:shadow-md ${
        isSelected
          ? "border-blue-600 bg-blue-50"
          : "border-gray-200 bg-white hover:border-blue-300"
      }`}
    >
      {icon && <div className="text-2xl mb-2">{icon}</div>}
      <h3 className="text-base font-semibold text-gray-900 mb-1">
        {option.label}
      </h3>
      {option.desc && (
        <p className="text-sm text-gray-600">{option.desc}</p>
      )}
    </button>
  );
};

const DocumentLink = ({ doc, isRead, onToggleRead }) => {
  const isPriority = doc.priority === "start-here";
  const href = docUrl(doc.path);
  return (
    <div className={`flex items-center gap-3 p-4 rounded-lg border transition-all hover:shadow-md ${
      isRead ? "border-green-300 bg-green-50" : isPriority ? "border-amber-300 bg-amber-50 hover:bg-amber-100" : "border-gray-200 bg-white hover:bg-gray-50"
    }`}>
      <button
        type="button"
        onClick={(e) => { e.preventDefault(); onToggleRead(doc.path); }}
        className={`flex-shrink-0 w-6 h-6 rounded border-2 flex items-center justify-center transition-colors ${
          isRead ? "bg-green-500 border-green-500 text-white" : "border-gray-300 hover:border-blue-400"
        }`}
        title={isRead ? t("標記為未讀", "Mark as unread") : t("標記為已讀", "Mark as read")}
      >
        {isRead && <span className="text-xs font-bold">✓</span>}
      </button>
      <a href={href} target="_blank" rel="noopener noreferrer" className="flex-1 min-w-0">
        <div className="flex items-start justify-between">
          <div className="flex-1">
            <h4 className={`font-semibold text-sm ${isRead ? "text-green-800 line-through" : "text-gray-900"}`}>
              {doc.name}
              {isPriority && !isRead && (
                <span className="ml-2 inline-block px-2 py-1 bg-amber-200 text-amber-900 text-xs font-bold rounded">
                  START HERE
                </span>
              )}
            </h4>
            {doc.summary && (
              <p className="text-xs text-gray-500 mt-1 leading-relaxed">{doc.summary}</p>
            )}
          </div>
          <div className="ml-3 text-lg">→</div>
        </div>
      </a>
    </div>
  );
};

const RecommendationsSummary = ({ recommendations, readDocs, onToggleRead }) => {
  const total = recommendations.docs.length;
  const done = recommendations.docs.filter(d => readDocs.has(d.path)).length;
  return (
    <div className="space-y-6">
      <div className="bg-blue-50 border border-blue-200 rounded-lg p-6">
        <h2 className="text-2xl font-bold text-gray-900 mb-2">
          {recommendations.title}
        </h2>
        <p className="text-gray-600">
          {t('點擊任一文件開始閱讀，優先從「START HERE」開始。', 'Click any document to read. Start with "START HERE" first.')}
        </p>
        <div className="mt-3 flex items-center gap-3">
          <div className="flex-1 h-2 bg-blue-100 rounded-full overflow-hidden">
            <div className="h-full bg-green-500 rounded-full transition-all" style={{ width: `${total > 0 ? (done / total) * 100 : 0}%` }} />
          </div>
          <span className="text-sm font-medium text-gray-600">{done}/{total}</span>
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
  const selectedOptionObj = optionsList.find((o) => o.id === selectedOption);
  const recommendations = recommendationKey
    ? RECOMMENDATIONS[recommendationKey]
    : null;

  return (
    <div className="min-h-screen bg-gradient-to-br from-blue-50 via-white to-indigo-50">
      <div className="max-w-4xl mx-auto px-4 py-8 sm:py-12">
        {/* Header */}
        <div className="text-center mb-8">
          <h1 className="text-4xl sm:text-5xl font-bold text-gray-900 mb-3">
            {t("動態警報平台", "Dynamic Alerting Platform")}
          </h1>
          <p className="text-lg text-gray-600 mb-4">
            {t("幾秒內找到你的專屬學習路徑", "Find your personalized learning path in seconds")}
          </p>
          {step === 0 && (
            <div className="inline-flex flex-wrap justify-center gap-2 text-xs text-gray-500">
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
            <div className="bg-white rounded-lg shadow-sm p-8">
              <h2 className="text-2xl font-bold text-gray-900 mb-8">
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
            <div className="bg-white rounded-lg shadow-sm p-8">
              <h2 className="text-2xl font-bold text-gray-900 mb-2">
                {selectedRoleObj.label}
              </h2>
              <p className="text-gray-600 mb-8">
                {selectedRoleObj.desc}
              </p>

              <div className="mb-6">
                <h3 className="text-lg font-semibold text-gray-900 mb-4">
                  {selectedRole === "platform" && t("你的目標是？", "What's your goal?")}
                  {selectedRole === "domain" && t("你管理哪種資料庫？", "What database do you manage?")}
                  {selectedRole === "tenant" && t("你需要什麼幫助？", "What do you need help with?")}
                </h3>
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
              </div>

              <button
                onClick={handleStartOver}
                className="w-full px-4 py-2 text-sm font-medium text-gray-700 bg-gray-100 rounded-lg hover:bg-gray-200 transition-colors"
              >
                Back
              </button>
            </div>
          </div>
        )}

        {/* Step 3: Recommendations */}
        {step === 2 && recommendations && (
          <div className="space-y-6">
            <RecommendationsSummary recommendations={recommendations} readDocs={readDocs} onToggleRead={toggleReadDoc} />

            <div className="flex flex-col sm:flex-row gap-3">
              <button
                onClick={() => setStep(1)}
                className="flex-1 px-4 py-3 text-sm font-medium text-gray-700 bg-gray-100 rounded-lg hover:bg-gray-200 transition-colors"
              >
                Back
              </button>
              <button
                onClick={handleStartOver}
                className="flex-1 px-4 py-3 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 transition-colors"
              >
                Start Over
              </button>
            </div>
          </div>
        )}

        {/* Footer */}
        <div className="mt-12 pt-8 border-t border-gray-200">
          <p className="text-center text-sm text-gray-600">
            Questions? Check the{" "}
            <a href={docUrl("../troubleshooting.md")} target="_blank" rel="noopener noreferrer" className="text-blue-600 hover:underline">
              Troubleshooting Guide
            </a>
            {" "}or{" "}
            <a href={docUrl("../context-diagram.md")} target="_blank" rel="noopener noreferrer" className="text-blue-600 hover:underline">
              Context Diagram
            </a>
          </p>
        </div>
      </div>
    </div>
  );
}
