import { useState } from "react";

const ROLES = [
  {
    id: "platform",
    label: "Platform Engineer",
    icon: "⚙️",
    desc: "Set up and manage the alerting platform",
  },
  {
    id: "domain",
    label: "Domain Expert (DBA)",
    icon: "🗄️",
    desc: "Configure thresholds for your databases",
  },
  {
    id: "tenant",
    label: "Tenant Team",
    icon: "👥",
    desc: "Manage alerts for your team's services",
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
      {
        name: "Platform Engineer Quick Start",
        path: "for-platform-engineers.md",
        priority: "start-here",
      },
      { name: "Architecture & Design", path: "../architecture-and-design.md" },
      { name: "BYO Prometheus Integration", path: "../byo-prometheus-integration.md" },
      {
        name: "BYO Alertmanager Integration",
        path: "../byo-alertmanager-integration.md",
      },
      { name: "GitOps Deployment Guide", path: "../gitops-deployment.md" },
      { name: "Custom Rule Governance", path: "../custom-rule-governance.md" },
    ],
  },
  "platform-migration": {
    title: "Migration from Legacy Systems",
    docs: [
      {
        name: "Platform Engineer Quick Start",
        path: "for-platform-engineers.md",
        priority: "start-here",
      },
      {
        name: "Migration Engine Guide",
        path: "../migration-engine.md",
      },
      {
        name: "Migration User Guide",
        path: "../migration-guide.md",
      },
      { name: "Advanced Scenarios", path: "../scenarios/advanced-scenarios.md" },
      { name: "Shadow Monitoring SOP", path: "../shadow-monitoring-sop.md" },
      {
        name: "Shadow Monitoring Cutover",
        path: "../scenarios/shadow-monitoring-cutover.md",
      },
    ],
  },
  "platform-federation": {
    title: "Multi-Cluster Federation",
    docs: [
      {
        name: "Platform Engineer Quick Start",
        path: "for-platform-engineers.md",
        priority: "start-here",
      },
      {
        name: "Federation Integration Guide",
        path: "../federation-integration.md",
      },
      {
        name: "Multi-Cluster Federation Scenarios",
        path: "../scenarios/multi-cluster-federation.md",
      },
      { name: "Architecture & Design", path: "../architecture-and-design.md" },
      { name: "Governance & Security", path: "../governance-security.md" },
    ],
  },
  "platform-monitoring": {
    title: "Platform Monitoring & Scaling",
    docs: [
      {
        name: "Platform Engineer Quick Start",
        path: "for-platform-engineers.md",
        priority: "start-here",
      },
      { name: "Benchmarks & Performance", path: "../benchmarks.md" },
      {
        name: "Troubleshooting Guide",
        path: "../troubleshooting.md",
      },
      { name: "Architecture & Design", path: "../architecture-and-design.md" },
      { name: "Governance & Security", path: "../governance-security.md" },
    ],
  },
  "domain-mariadb": {
    title: "MariaDB Threshold Configuration",
    docs: [
      {
        name: "Domain Expert Quick Start",
        path: "for-domain-experts.md",
        priority: "start-here",
      },
      { name: "Tenant Quick Start", path: "for-tenants.md" },
      { name: "Alert Reference", path: "../rule-packs/ALERT-REFERENCE.md" },
      { name: "Rule Packs Overview", path: "../rule-packs/README.md" },
      { name: "Architecture & Design", path: "../architecture-and-design.md" },
      { name: "Baseline Discovery Tool", path: "../internal/testing-playbook.md" },
    ],
  },
  "domain-postgresql": {
    title: "PostgreSQL Threshold Configuration",
    docs: [
      {
        name: "Domain Expert Quick Start",
        path: "for-domain-experts.md",
        priority: "start-here",
      },
      { name: "Tenant Quick Start", path: "for-tenants.md" },
      { name: "Alert Reference", path: "../rule-packs/ALERT-REFERENCE.md" },
      { name: "Rule Packs Overview", path: "../rule-packs/README.md" },
      { name: "Architecture & Design", path: "../architecture-and-design.md" },
      { name: "Baseline Discovery Tool", path: "../internal/testing-playbook.md" },
    ],
  },
  "domain-redis": {
    title: "Redis Threshold Configuration",
    docs: [
      {
        name: "Domain Expert Quick Start",
        path: "for-domain-experts.md",
        priority: "start-here",
      },
      { name: "Tenant Quick Start", path: "for-tenants.md" },
      { name: "Alert Reference", path: "../rule-packs/ALERT-REFERENCE.md" },
      { name: "Rule Packs Overview", path: "../rule-packs/README.md" },
      { name: "Architecture & Design", path: "../architecture-and-design.md" },
      { name: "Baseline Discovery Tool", path: "../internal/testing-playbook.md" },
    ],
  },
  "domain-mongodb": {
    title: "MongoDB Threshold Configuration",
    docs: [
      {
        name: "Domain Expert Quick Start",
        path: "for-domain-experts.md",
        priority: "start-here",
      },
      { name: "Tenant Quick Start", path: "for-tenants.md" },
      { name: "Alert Reference", path: "../rule-packs/ALERT-REFERENCE.md" },
      { name: "Rule Packs Overview", path: "../rule-packs/README.md" },
      { name: "Architecture & Design", path: "../architecture-and-design.md" },
      { name: "Baseline Discovery Tool", path: "../internal/testing-playbook.md" },
    ],
  },
  "domain-other": {
    title: "Custom Threshold Configuration",
    docs: [
      {
        name: "Domain Expert Quick Start",
        path: "for-domain-experts.md",
        priority: "start-here",
      },
      { name: "Tenant Quick Start", path: "for-tenants.md" },
      { name: "Alert Reference", path: "../rule-packs/ALERT-REFERENCE.md" },
      { name: "Custom Rule Governance", path: "../custom-rule-governance.md" },
      { name: "Architecture & Design", path: "../architecture-and-design.md" },
      { name: "Advanced Scenarios", path: "../scenarios/advanced-scenarios.md" },
    ],
  },
  "tenant-onboard": {
    title: "Getting Your Team Onboarded",
    docs: [
      {
        name: "Tenant Quick Start",
        path: "for-tenants.md",
        priority: "start-here",
      },
      { name: "Tenant Lifecycle Scenarios", path: "../scenarios/tenant-lifecycle.md" },
      { name: "Alert Reference", path: "../rule-packs/ALERT-REFERENCE.md" },
      { name: "Rule Packs Overview", path: "../rule-packs/README.md" },
      { name: "Migration Guide", path: "../migration-guide.md" },
    ],
  },
  "tenant-alerts": {
    title: "Configure Alerts for Your Services",
    docs: [
      {
        name: "Tenant Quick Start",
        path: "for-tenants.md",
        priority: "start-here",
      },
      { name: "Alert Reference", path: "../rule-packs/ALERT-REFERENCE.md" },
      { name: "Rule Packs Overview", path: "../rule-packs/README.md" },
      { name: "Architecture & Design", path: "../architecture-and-design.md" },
      { name: "Advanced Scenarios", path: "../scenarios/advanced-scenarios.md" },
      { name: "Troubleshooting Guide", path: "../troubleshooting.md" },
    ],
  },
  "tenant-routing": {
    title: "Set Up Alert Routing & Notifications",
    docs: [
      {
        name: "Tenant Quick Start",
        path: "for-tenants.md",
        priority: "start-here",
      },
      {
        name: "Alert Routing Split (NOC vs Tenant)",
        path: "../scenarios/alert-routing-split.md",
      },
      { name: "Tenant Lifecycle Scenarios", path: "../scenarios/tenant-lifecycle.md" },
      { name: "Troubleshooting Guide", path: "../troubleshooting.md" },
      { name: "Architecture & Design", path: "../architecture-and-design.md" },
    ],
  },
  "tenant-maintenance": {
    title: "Manage Maintenance Windows & Silences",
    docs: [
      {
        name: "Tenant Quick Start",
        path: "for-tenants.md",
        priority: "start-here",
      },
      { name: "Tenant Lifecycle Scenarios", path: "../scenarios/tenant-lifecycle.md" },
      { name: "Architecture & Design", path: "../architecture-and-design.md" },
      { name: "Advanced Scenarios", path: "../scenarios/advanced-scenarios.md" },
      { name: "Troubleshooting Guide", path: "../troubleshooting.md" },
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
      <p className="text-sm text-gray-600">{role.desc}</p>
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

const DocumentLink = ({ doc }) => {
  const isPriority = doc.priority === "start-here";
  return (
    <a
      href={doc.path}
      className={`block p-4 rounded-lg border transition-all hover:shadow-md ${
        isPriority
          ? "border-amber-300 bg-amber-50 hover:bg-amber-100"
          : "border-gray-200 bg-white hover:bg-gray-50"
      }`}
    >
      <div className="flex items-start justify-between">
        <div className="flex-1">
          <h4 className="font-semibold text-gray-900 text-sm">
            {doc.name}
            {isPriority && (
              <span className="ml-2 inline-block px-2 py-1 bg-amber-200 text-amber-900 text-xs font-bold rounded">
                START HERE
              </span>
            )}
          </h4>
          <p className="text-xs text-gray-500 mt-1 font-mono break-all">
            {doc.path}
          </p>
        </div>
        <div className="ml-3 text-lg">→</div>
      </div>
    </a>
  );
};

const RecommendationsSummary = ({ recommendations }) => {
  return (
    <div className="space-y-6">
      <div className="bg-blue-50 border border-blue-200 rounded-lg p-6">
        <h2 className="text-2xl font-bold text-gray-900 mb-2">
          {recommendations.title}
        </h2>
        <p className="text-gray-600">
          Click any document to read. Start with "START HERE" first.
        </p>
      </div>

      <div className="space-y-3">
        {recommendations.docs.map((doc, idx) => (
          <DocumentLink key={idx} doc={doc} />
        ))}
      </div>
    </div>
  );
};

export default function GettingStartedWizard() {
  const [step, setStep] = useState(0);
  const [selectedRole, setSelectedRole] = useState(null);
  const [selectedOption, setSelectedOption] = useState(null);
  const [recommendationKey, setRecommendationKey] = useState(null);

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
  };

  const handleOptionSelect = (optionId) => {
    setSelectedOption(optionId);
    const key = selectedRole === "platform" ? `platform-${optionId}` : selectedRole === "domain" ? `domain-${optionId}` : `tenant-${optionId}`;
    setRecommendationKey(key);
    setStep(2);
  };

  const handleStartOver = () => {
    setStep(0);
    setSelectedRole(null);
    setSelectedOption(null);
    setRecommendationKey(null);
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
        <div className="text-center mb-12">
          <h1 className="text-4xl sm:text-5xl font-bold text-gray-900 mb-3">
            Dynamic Alerting Platform
          </h1>
          <p className="text-lg text-gray-600">
            Find your personalized learning path in seconds
          </p>
        </div>

        {/* Progress Indicator */}
        <ProgressIndicator step={step} totalSteps={3} />

        {/* Step 1: Role Selection */}
        {step === 0 && (
          <div className="space-y-6">
            <div className="bg-white rounded-lg shadow-sm p-8">
              <h2 className="text-2xl font-bold text-gray-900 mb-8">
                Who are you?
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
                  {selectedRole === "platform" && "What's your goal?"}
                  {selectedRole === "domain" &&
                    "What database do you manage?"}
                  {selectedRole === "tenant" && "What do you need help with?"}
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
            <RecommendationsSummary recommendations={recommendations} />

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
            <a href="../troubleshooting.md" className="text-blue-600 hover:underline">
              Troubleshooting Guide
            </a>
            {" "}or{" "}
            <a href="../context-diagram.md" className="text-blue-600 hover:underline">
              Context Diagram
            </a>
          </p>
        </div>
      </div>
    </div>
  );
}
