---
title: "CI/CD Setup Wizard — Default catalogs"
purpose: |
  Static data tables for the 5-step CI/CD Setup Wizard: step
  metadata, Rule Pack catalog, CI platform choices, deployment-mode
  choices.

  Pre-PR-portal-10 these were inline at the top of cicd-setup-
  wizard.jsx. Splitting drops ~40 LOC from the orchestrator and
  matches the pattern established by operator-setup-wizard
  (PR-portal-4) + the other 2 sibling wizards in this same PR.

  Public API:
    window.__CICD_STEPS              ordered step metadata
    window.__CICD_RULE_PACKS         13 Rule Pack catalog
    window.__CICD_CI_OPTIONS         GitHub Actions / GitLab CI / both
    window.__CICD_DEPLOY_OPTIONS     Kustomize / Helm / ArgoCD

  Closure deps: reads window.__t at consumer call time.
---

const t = window.__t || ((zh, en) => en);

const CICD_STEPS = [
  { id: 'ci', label: () => t('CI/CD 平台', 'CI/CD Platform') },
  { id: 'deploy', label: () => t('部署方式', 'Deployment Mode') },
  { id: 'packs', label: () => t('Rule Packs', 'Rule Packs') },
  { id: 'tenants', label: () => t('Tenant 設定', 'Tenant Setup') },
  { id: 'review', label: () => t('檢視與產出', 'Review & Generate') },
];

const CICD_RULE_PACKS = [
  { id: 'mariadb', label: 'MariaDB/MySQL', category: 'database', icon: '🐬', defaultOn: true },
  { id: 'postgresql', label: 'PostgreSQL', category: 'database', icon: '🐘' },
  { id: 'redis', label: 'Redis', category: 'database', icon: '🔴' },
  { id: 'mongodb', label: 'MongoDB', category: 'database', icon: '🍃' },
  { id: 'elasticsearch', label: 'Elasticsearch', category: 'database', icon: '🔎' },
  { id: 'oracle', label: 'Oracle', category: 'database', icon: '🏛️' },
  { id: 'db2', label: 'DB2', category: 'database', icon: '🔷' },
  { id: 'clickhouse', label: 'ClickHouse', category: 'database', icon: '🖱️' },
  { id: 'kafka', label: 'Kafka', category: 'messaging', icon: '📨' },
  { id: 'rabbitmq', label: 'RabbitMQ', category: 'messaging', icon: '🐰' },
  { id: 'jvm', label: 'JVM', category: 'runtime', icon: '☕' },
  { id: 'nginx', label: 'Nginx', category: 'webserver', icon: '🌐' },
  { id: 'kubernetes', label: 'Kubernetes', category: 'infrastructure', icon: '⎈', defaultOn: true },
];

const CICD_CI_OPTIONS = [
  { id: 'github', label: 'GitHub Actions', icon: '🐙', desc: () => t('最廣泛使用的 CI/CD，直接整合 GitHub', 'Most widely used CI/CD, built into GitHub.') },
  { id: 'gitlab', label: 'GitLab CI', icon: '🦊', desc: () => t('GitLab 內建 CI/CD，支援自託管', 'GitLab built-in CI/CD, supports self-hosted.') },
  { id: 'both', label: t('兩者皆有', 'Both'), icon: '🔄', desc: () => t('同時產生 GitHub Actions 和 GitLab CI 配置', 'Generate both GitHub Actions and GitLab CI configs.') },
];

const CICD_DEPLOY_OPTIONS = [
  { id: 'kustomize', label: 'Kustomize', icon: '📦', desc: () => t('推薦入門：configMapGenerator 自動產生 ConfigMap', 'Recommended to start: configMapGenerator auto-creates ConfigMap.') },
  { id: 'helm', label: 'Helm', icon: '⛵', desc: () => t('使用 threshold-exporter Helm chart 管理', 'Managed via threshold-exporter Helm chart.') },
  { id: 'argocd', label: 'ArgoCD', icon: '🔁', desc: () => t('GitOps 自動同步：ArgoCD Application 指向你的 repo', 'GitOps auto-sync: ArgoCD Application points to your repo.') },
];

window.__CICD_STEPS = CICD_STEPS;
window.__CICD_RULE_PACKS = CICD_RULE_PACKS;
window.__CICD_CI_OPTIONS = CICD_CI_OPTIONS;
window.__CICD_DEPLOY_OPTIONS = CICD_DEPLOY_OPTIONS;

// TD-030e: ESM exports. Removed in TD-030z.
// <!-- jsx-loader-compat: ignore -->
export { CICD_STEPS, CICD_RULE_PACKS, CICD_CI_OPTIONS, CICD_DEPLOY_OPTIONS };
