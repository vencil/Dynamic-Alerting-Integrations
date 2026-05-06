---
title: "RBAC Setup Wizard — YAML generator + validator"
purpose: |
  Two pure functions for the RBAC wizard: emit `_rbac:` YAML from a
  list of group definitions, and lint the same input for common
  configuration mistakes (missing name, broad admin scope, empty
  tenant list under "specific" mode, etc.).

  Pre-PR-portal-10 these were inline at the top of rbac-setup-
  wizard.jsx. Splitting matches the operator-setup-wizard pattern
  from PR-portal-4.

  Public API:
    window.__rbacGenerateYaml(groups)     emit _rbac: YAML block
    window.__rbacValidate(groups)         return [{level, msg}]

  Closure deps: validate uses window.__t for messages.
---

function rbacGenerateYaml(groups) {
  let yaml = '_rbac:\n';
  for (const group of groups) {
    if (!group.name) continue;
    yaml += `  ${group.name}:\n`;
    yaml += `    description: "${group.description || ''}"\n`;
    yaml += `    permission: ${group.permission}\n`;

    if (group.tenantMode === 'all') {
      yaml += `    tenants: ["*"]\n`;
    } else if (group.tenantMode === 'prefix' && group.tenantPrefix) {
      yaml += `    tenants: ["${group.tenantPrefix}"]\n`;
    } else if (group.tenantMode === 'specific' && group.specificTenants.length > 0) {
      yaml += `    tenants: [${group.specificTenants.map(t => `"${t}"`).join(', ')}]\n`;
    }

    const hasEnvFilter = group.environments && group.environments.length > 0;
    const hasDomainFilter = group.domains && group.domains.length > 0;

    if (hasEnvFilter || hasDomainFilter) {
      yaml += `    filters:\n`;
      if (hasEnvFilter) {
        yaml += `      environments: [${group.environments.map(e => `"${e}"`).join(', ')}]\n`;
      }
      if (hasDomainFilter) {
        yaml += `      domains: [${group.domains.map(d => `"${d}"`).join(', ')}]\n`;
      }
    }
  }
  return yaml;
}

function rbacValidate(groups) {
  const t = window.__t || ((zh, en) => en);
  const warnings = [];
  for (const group of groups) {
    if (!group.name) {
      warnings.push({ level: 'error', msg: () => t('群組名稱不能為空', 'Group name cannot be empty') });
    }
    if (!group.permission) {
      warnings.push({ level: 'error', msg: () => t('未設定權限', 'Permission not set') });
    }
    if (group.tenantMode === 'all' && group.permission === 'admin') {
      warnings.push({
        level: 'warning',
        msg: () => t(`群組 "${group.name}" 有管理員權限且可訪問所有租戶 - 非常寬鬆，請確認`, `Group "${group.name}" has admin on all tenants - very broad, please confirm`)
      });
    }
    if (group.tenantMode === 'specific' && group.specificTenants.length === 0) {
      warnings.push({ level: 'error', msg: () => t('特定租戶模式下未選中任何租戶', 'No specific tenants selected') });
    }
  }
  return warnings;
}

window.__rbacGenerateYaml = rbacGenerateYaml;
window.__rbacValidate = rbacValidate;

// TD-030e: ESM exports. Removed in TD-030z.
// <!-- jsx-loader-compat: ignore -->
export { rbacGenerateYaml, rbacValidate };
