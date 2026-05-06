---
title: "RBAC Setup Wizard — Default catalogs"
purpose: |
  Static data tables for the 5-step RBAC Setup Wizard: step
  metadata, permission hierarchy (read/write/admin levels),
  environment + domain example lists.

  Pre-PR-portal-10 these were inline at the top of rbac-setup-
  wizard.jsx. Splitting matches the operator-setup-wizard pattern
  from PR-portal-4 + the other 2 sibling wizards in this PR.

  Public API:
    window.__RBAC_STEPS                  ordered step metadata
    window.__RBAC_PERMISSION_HIERARCHY   {read, write, admin} with level + i18n
    window.__RBAC_ENVIRONMENTS           production / staging / development
    window.__RBAC_DOMAIN_EXAMPLES        finance / ecommerce / analytics / ...

  Closure deps: reads window.__t at consumer call time.
---

const t = window.__t || ((zh, en) => en);

const RBAC_STEPS = [
  { id: 'groups', label: () => t('定義群組', 'Define Groups') },
  { id: 'tenants', label: () => t('分配租戶', 'Assign Tenants') },
  { id: 'permissions', label: () => t('設定權限', 'Set Permissions') },
  { id: 'filters', label: () => t('環境/域名篩選', 'Environment/Domain Filters') },
  { id: 'review', label: () => t('檢視與匯出', 'Review & Export') },
];

const RBAC_PERMISSION_HIERARCHY = {
  read: { level: 1, label: () => t('讀取', 'Read'), desc: () => t('查看配置和告警', 'View configs and alerts') },
  write: { level: 2, label: () => t('寫入', 'Write'), desc: () => t('修改配置（read + write）', 'Modify configs (read + write)') },
  admin: { level: 3, label: () => t('管理員', 'Admin'), desc: () => t('完全控制（包含讀取和寫入）', 'Full control (includes read & write)') },
};

const RBAC_ENVIRONMENTS = ['production', 'staging', 'development'];
const RBAC_DOMAIN_EXAMPLES = ['finance', 'ecommerce', 'analytics', 'mobile', 'streaming', 'cache'];

window.__RBAC_STEPS = RBAC_STEPS;
window.__RBAC_PERMISSION_HIERARCHY = RBAC_PERMISSION_HIERARCHY;
window.__RBAC_ENVIRONMENTS = RBAC_ENVIRONMENTS;
window.__RBAC_DOMAIN_EXAMPLES = RBAC_DOMAIN_EXAMPLES;

// TD-030e: ESM exports. Removed in TD-030z.
// <!-- jsx-loader-compat: ignore -->
export { RBAC_STEPS, RBAC_PERMISSION_HIERARCHY, RBAC_ENVIRONMENTS, RBAC_DOMAIN_EXAMPLES };
