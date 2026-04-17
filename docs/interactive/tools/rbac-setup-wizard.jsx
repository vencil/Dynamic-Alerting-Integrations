---
title: "RBAC Setup Wizard"
tags: [rbac, authorization, security, setup, wizard]
audience: ["platform-engineer", "sre"]
version: v2.6.0
lang: en
related: [config-lint, tenant-manager, self-service-portal]
dependencies: []
---

import React, { useState, useMemo, useCallback } from 'react';

const t = window.__t || ((zh, en) => en);

/* ── Step definitions ── */
const STEPS = [
  { id: 'groups', label: () => t('定義群組', 'Define Groups') },
  { id: 'tenants', label: () => t('分配租戶', 'Assign Tenants') },
  { id: 'permissions', label: () => t('設定權限', 'Set Permissions') },
  { id: 'filters', label: () => t('環境/域名篩選', 'Environment/Domain Filters') },
  { id: 'review', label: () => t('檢視與匯出', 'Review & Export') },
];

const PERMISSION_HIERARCHY = {
  read: { level: 1, label: () => t('讀取', 'Read'), desc: () => t('查看配置和告警', 'View configs and alerts') },
  write: { level: 2, label: () => t('寫入', 'Write'), desc: () => t('修改配置（read + write）', 'Modify configs (read + write)') },
  admin: { level: 3, label: () => t('管理員', 'Admin'), desc: () => t('完全控制（包含讀取和寫入）', 'Full control (includes read & write)') },
};

const ENVIRONMENTS = ['production', 'staging', 'development'];
const DOMAIN_EXAMPLES = ['finance', 'ecommerce', 'analytics', 'mobile', 'streaming', 'cache'];

/* ── Helper functions ── */

function generateRbacYaml(groups) {
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

function validateRbac(groups) {
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

/* ── Step Components ── */

function StepGroups({ groups, onChange }) {
  const [newGroup, setNewGroup] = useState('');

  const addGroup = useCallback(() => {
    if (newGroup.trim()) {
      onChange([...groups, {
        name: newGroup.trim(),
        description: '',
        permission: '',
        tenantMode: 'all',
        specificTenants: [],
        tenantPrefix: '',
        environments: [],
        domains: [],
      }]);
      setNewGroup('');
    }
  }, [newGroup, onChange]);

  const updateGroup = useCallback((idx, updates) => {
    const newGroups = [...groups];
    newGroups[idx] = { ...newGroups[idx], ...updates };
    onChange(newGroups);
  }, [onChange]);

  const removeGroup = useCallback((idx) => {
    onChange(groups.filter((_, i) => i !== idx));
  }, [onChange, groups]);

  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-lg font-semibold mb-2">{t('第一步：定義 IdP 群組', 'Step 1: Define IdP Groups')}</h3>
        <p className="text-sm text-[color:var(--da-color-muted)] mb-4">
          {t('列出來自你的身份供應商的群組。這些將成為 RBAC 的基礎。', 'List groups from your IdP (e.g., Okta, AAD). These form the basis of RBAC.')}
        </p>
      </div>

      {groups.length > 0 && (
        <div className="space-y-3 mb-4">
          {groups.map((group, idx) => (
            <div key={idx} className="p-4 border border-[color:var(--da-color-surface-border)] rounded-lg bg-[color:var(--da-color-surface)]">
              <div className="flex items-start justify-between mb-3">
                <div className="flex-1">
                  <label htmlFor={`rbac-group-name-${idx}`} className="sr-only">{t('群組名稱', 'Group name')}</label>
                  <input
                    id={`rbac-group-name-${idx}`}
                    type="text"
                    value={group.name}
                    onChange={(e) => updateGroup(idx, { name: e.target.value })}
                    placeholder={t('群組名稱 (如: engineering-team)', 'Group name (e.g., engineering-team)')}
                    aria-label={t('群組名稱', 'Group name')}
                    className="w-full px-3 py-2 border border-[color:var(--da-color-surface-border)] rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-[color:var(--da-color-focus-ring)]"
                  />
                </div>
                <button
                  onClick={() => removeGroup(idx)}
                  aria-label={t(`移除群組 ${group.name || idx + 1}`, `Remove group ${group.name || idx + 1}`)}
                  className="ml-2 px-2 py-1 text-[color:var(--da-color-muted)] hover:text-[color:var(--da-color-error)] hover:bg-[color:var(--da-color-error-soft)] rounded text-sm"
                >
                  ✕
                </button>
              </div>
              <label htmlFor={`rbac-group-desc-${idx}`} className="sr-only">{t('描述', 'Description')}</label>
              <input
                id={`rbac-group-desc-${idx}`}
                type="text"
                value={group.description}
                onChange={(e) => updateGroup(idx, { description: e.target.value })}
                placeholder={t('描述 (選填)', 'Description (optional)')}
                aria-label={t('群組描述', 'Group description')}
                className="w-full px-3 py-2 border border-[color:var(--da-color-surface-border)] rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-[color:var(--da-color-focus-ring)]"
              />
            </div>
          ))}
        </div>
      )}

      <div className="flex gap-2">
        <label htmlFor="rbac-new-group-name" className="sr-only">{t('群組名稱', 'Group name')}</label>
        <input
          id="rbac-new-group-name"
          type="text"
          value={newGroup}
          onChange={(e) => setNewGroup(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && addGroup()}
          placeholder={t('輸入新群組名稱...', 'Enter group name...')}
          className="flex-1 px-3 py-2 border border-[color:var(--da-color-surface-border)] rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-[color:var(--da-color-focus-ring)]"
        />
        <button
          onClick={addGroup}
          className="px-4 py-2 bg-[color:var(--da-color-accent)] text-white rounded-lg text-sm font-medium hover:bg-[color:var(--da-color-accent-hover)]"
        >
          {t('加入群組', 'Add Group')}
        </button>
      </div>

      {groups.length === 0 && (
        <div className="p-4 bg-[color:var(--da-color-info-soft)] border border-[color:var(--da-color-info)]/30 rounded-lg text-sm text-[color:var(--da-color-fg)]" role="status" aria-live="polite">
          💡 {t('新增至少一個群組才能繼續', 'Add at least one group to proceed')}
        </div>
      )}
    </div>
  );
}

function StepTenants({ groups, onChange }) {
  const demoTenants = ['prod-db-01', 'prod-db-02', 'staging-db-01', 'staging-api-01', 'dev-all-01'];

  const updateGroup = useCallback((idx, updates) => {
    const newGroups = [...groups];
    newGroups[idx] = { ...newGroups[idx], ...updates };
    onChange(newGroups);
  }, [onChange, groups]);

  const toggleTenant = useCallback((groupIdx, tenant) => {
    const group = groups[groupIdx];
    const tenants = group.specificTenants.includes(tenant)
      ? group.specificTenants.filter(t => t !== tenant)
      : [...group.specificTenants, tenant];
    updateGroup(groupIdx, { specificTenants: tenants });
  }, [groups, updateGroup]);

  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-lg font-semibold mb-2">{t('第二步：分配租戶', 'Step 2: Assign Tenants')}</h3>
        <p className="text-sm text-[color:var(--da-color-muted)] mb-4">
          {t('每個群組可以訪問所有租戶、根據前綴模式、或指定租戶。', 'Each group can access all tenants, match by prefix pattern, or specific tenants.')}
        </p>
      </div>

      <div className="space-y-5">
        {groups.map((group, groupIdx) => (
          <div key={groupIdx} className="p-4 border border-[color:var(--da-color-surface-border)] rounded-lg bg-[color:var(--da-color-surface)]">
            <h4 className="font-medium text-[color:var(--da-color-fg)] mb-3">{group.name}</h4>

            <div className="space-y-3">
              <div>
                <label className="flex items-center gap-2 cursor-pointer mb-2">
                  <input
                    type="radio"
                    checked={group.tenantMode === 'all'}
                    onChange={() => updateGroup(groupIdx, { tenantMode: 'all' })}
                    className="w-4 h-4 accent-[color:var(--da-color-accent)]"
                  />
                  <span className="text-sm font-medium">{t('所有租戶 (*)', 'All tenants (*)')}</span>
                </label>
                <p className="text-xs text-[color:var(--da-color-muted)] ml-6">{t('此群組可訪問所有租戶', 'This group can access all tenants')}</p>
              </div>

              <div>
                <label className="flex items-center gap-2 cursor-pointer mb-2">
                  <input
                    type="radio"
                    checked={group.tenantMode === 'prefix'}
                    onChange={() => updateGroup(groupIdx, { tenantMode: 'prefix' })}
                    className="w-4 h-4 accent-[color:var(--da-color-accent)]"
                  />
                  <span className="text-sm font-medium">{t('前綴模式', 'Prefix pattern')}</span>
                </label>
                {group.tenantMode === 'prefix' && (
                  <input
                    type="text"
                    value={group.tenantPrefix}
                    onChange={(e) => updateGroup(groupIdx, { tenantPrefix: e.target.value })}
                    placeholder={t('例如: prod-* 或 staging-db-*', 'e.g., prod-* or staging-db-*')}
                    aria-label={t('租戶前綴', 'Tenant prefix')}
                    className="ml-6 w-full px-3 py-2 border border-[color:var(--da-color-surface-border)] rounded text-sm focus:outline-none focus:ring-2 focus:ring-[color:var(--da-color-focus-ring)]"
                  />
                )}
              </div>

              <div>
                <label className="flex items-center gap-2 cursor-pointer mb-2">
                  <input
                    type="radio"
                    checked={group.tenantMode === 'specific'}
                    onChange={() => updateGroup(groupIdx, { tenantMode: 'specific' })}
                    className="w-4 h-4 accent-[color:var(--da-color-accent)]"
                  />
                  <span className="text-sm font-medium">{t('特定租戶 ID', 'Specific tenant IDs')}</span>
                </label>
                {group.tenantMode === 'specific' && (
                  <div className="ml-6 space-y-2">
                    <div className="flex flex-wrap gap-2">
                      {demoTenants.map(tenant => (
                        <button
                          key={tenant}
                          onClick={() => toggleTenant(groupIdx, tenant)}
                          aria-pressed={group.specificTenants.includes(tenant)}
                          className={`px-3 py-1 rounded text-sm font-medium transition-colors ${
                            group.specificTenants.includes(tenant)
                              ? 'bg-[color:var(--da-color-accent)] text-white'
                              : 'bg-[color:var(--da-color-tag-bg)] text-[color:var(--da-color-fg)] hover:bg-[color:var(--da-color-surface-hover)]'
                          }`}
                        >
                          {tenant}
                        </button>
                      ))}
                    </div>
                    <p className="text-xs text-[color:var(--da-color-muted)]">{t('已選擇: ', 'Selected: ')}{group.specificTenants.length > 0 ? group.specificTenants.join(', ') : t('無', 'None')}</p>
                  </div>
                )}
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function StepPermissions({ groups, onChange }) {
  const updateGroup = useCallback((idx, updates) => {
    const newGroups = [...groups];
    newGroups[idx] = { ...newGroups[idx], ...updates };
    onChange(newGroups);
  }, [onChange, groups]);

  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-lg font-semibold mb-2">{t('第三步：設定權限', 'Step 3: Set Permissions')}</h3>
        <p className="text-sm text-[color:var(--da-color-muted)] mb-4">
          {t('定義每個群組的權限等級。權限是階層性的：admin ⊇ write ⊇ read', 'Define permission levels for each group. Permissions are hierarchical: admin ⊇ write ⊇ read')}
        </p>
      </div>

      <div className="p-4 bg-[color:var(--da-color-surface-hover)] border border-[color:var(--da-color-surface-border)] rounded-lg mb-4">
        <h4 className="text-sm font-semibold mb-3 text-[color:var(--da-color-fg)]">{t('權限階層', 'Permission Hierarchy')}</h4>
        <div className="space-y-2">
          {['read', 'write', 'admin'].map(perm => (
            <div key={perm} className="flex items-start gap-3">
              <div className={`w-6 h-6 rounded flex items-center justify-center text-white font-bold text-xs ${
                perm === 'read' ? 'bg-[color:var(--da-color-success)]' : perm === 'write' ? 'bg-[color:var(--da-color-accent)]' : 'bg-[color:var(--da-color-error)]'
              }`}>
                {perm === 'read' ? 'R' : perm === 'write' ? 'W' : 'A'}
              </div>
              <div>
                <div className="font-medium text-sm text-[color:var(--da-color-fg)]">{PERMISSION_HIERARCHY[perm].label()}</div>
                <div className="text-xs text-[color:var(--da-color-muted)]">{PERMISSION_HIERARCHY[perm].desc()}</div>
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="space-y-4">
        {groups.map((group, idx) => (
          <div key={idx} className="p-4 border border-[color:var(--da-color-surface-border)] rounded-lg bg-[color:var(--da-color-surface)]">
            <h4 className="font-medium text-[color:var(--da-color-fg)] mb-3">{group.name}</h4>
            <div className="flex gap-2" role="group" aria-label={t(`${group.name} 權限選擇`, `${group.name} permission choice`)}>
              {['read', 'write', 'admin'].map(perm => (
                <button
                  key={perm}
                  onClick={() => updateGroup(idx, { permission: perm })}
                  aria-pressed={group.permission === perm}
                  className={`flex-1 px-3 py-2 rounded-lg text-sm font-medium transition-all ${
                    group.permission === perm
                      ? perm === 'read'
                        ? 'bg-[color:var(--da-color-success)] text-white'
                        : perm === 'write'
                        ? 'bg-[color:var(--da-color-accent)] text-white'
                        : 'bg-[color:var(--da-color-error)] text-white'
                      : 'bg-[color:var(--da-color-tag-bg)] text-[color:var(--da-color-fg)] hover:bg-[color:var(--da-color-surface-hover)]'
                  }`}
                >
                  {PERMISSION_HIERARCHY[perm].label()}
                </button>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function StepFilters({ groups, onChange }) {
  const updateGroup = useCallback((idx, updates) => {
    const newGroups = [...groups];
    newGroups[idx] = { ...newGroups[idx], ...updates };
    onChange(newGroups);
  }, [onChange, groups]);

  const toggleEnv = useCallback((groupIdx, env) => {
    const group = groups[groupIdx];
    const envs = group.environments.includes(env)
      ? group.environments.filter(e => e !== env)
      : [...group.environments, env];
    updateGroup(groupIdx, { environments: envs });
  }, [groups, updateGroup]);

  const addDomain = useCallback((groupIdx, domain) => {
    if (domain.trim() && !groups[groupIdx].domains.includes(domain.trim())) {
      updateGroup(groupIdx, { domains: [...groups[groupIdx].domains, domain.trim()] });
    }
  }, [groups, updateGroup]);

  const removeDomain = useCallback((groupIdx, domain) => {
    updateGroup(groupIdx, { domains: groups[groupIdx].domains.filter(d => d !== domain) });
  }, [groups, updateGroup]);

  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-lg font-semibold mb-2">{t('第四步：環境/域名篩選 (v2.5.0)', 'Step 4: Environment/Domain Filters (v2.5.0)')}</h3>
        <p className="text-sm text-[color:var(--da-color-muted)] mb-4">
          {t('選填。進一步限制群組訪問特定的環境或業務域名。', 'Optional. Further restrict group access to specific environments or business domains.')}
        </p>
        <details className="mb-4 text-sm border border-[color:var(--da-color-surface-border)] rounded-lg">
          <summary className="px-3 py-2 cursor-pointer text-[color:var(--da-color-accent)] hover:text-[color:var(--da-color-accent-hover)] font-medium">
            {t('在哪裡找到環境和域名的值？', 'Where do I find environment and domain values?')}
          </summary>
          <div className="px-3 py-2 text-[color:var(--da-color-muted)] bg-[color:var(--da-color-surface-hover)] rounded-b-lg">
            <p className="mb-2">{t(
              '環境和域名值來自 tenant 配置中的 _metadata 區塊。',
              'Environment and domain values come from the _metadata section in tenant configurations.'
            )}</p>
            <ul className="list-disc list-inside space-y-1">
              <li><code>environment</code>: {t('如 production, staging, development', 'e.g. production, staging, development')}</li>
              <li><code>domain</code>: {t('如 finance, ecommerce, infrastructure', 'e.g. finance, ecommerce, infrastructure')}</li>
            </ul>
            <p className="mt-2 text-xs text-[color:var(--da-color-muted)]">{t(
              '詳見 docs/governance-security.md 的 RBAC 章節。',
              'See docs/governance-security.md RBAC section for details.'
            )}</p>
          </div>
        </details>
      </div>

      <div className="space-y-5">
        {groups.map((group, groupIdx) => (
          <div key={groupIdx} className="p-4 border border-[color:var(--da-color-surface-border)] rounded-lg bg-[color:var(--da-color-surface)]">
            <h4 className="font-medium text-[color:var(--da-color-fg)] mb-4">{group.name}</h4>

            {/* Environments */}
            <div className="mb-4">
              <label className="text-sm font-medium text-[color:var(--da-color-fg)] mb-2 block">{t('環境篩選', 'Environment Filter')}</label>
              <div className="flex flex-wrap gap-2" role="group" aria-label={t('環境篩選選項', 'Environment filter options')}>
                {ENVIRONMENTS.map(env => (
                  <button
                    key={env}
                    onClick={() => toggleEnv(groupIdx, env)}
                    aria-pressed={group.environments.includes(env)}
                    className={`px-3 py-1.5 rounded-lg text-sm font-medium transition-all ${
                      group.environments.includes(env)
                        ? 'bg-[color:var(--da-color-accent)] text-white ring-2 ring-[color:var(--da-color-accent)]/40'
                        : 'bg-[color:var(--da-color-tag-bg)] text-[color:var(--da-color-fg)] hover:bg-[color:var(--da-color-surface-hover)]'
                    }`}
                  >
                    {env}
                  </button>
                ))}
              </div>
              <p className="text-xs text-[color:var(--da-color-muted)] mt-1">{t('未選擇 = 無限制', 'Unchecked = no restriction')}</p>
            </div>

            {/* Domains */}
            <div>
              <label className="text-sm font-medium text-[color:var(--da-color-fg)] mb-2 block">{t('域名篩選', 'Domain Filter')}</label>
              <div className="flex flex-wrap gap-2 mb-2">
                {group.domains.map(domain => (
                  <span key={domain} className="inline-flex items-center gap-1.5 px-3 py-1 bg-[color:var(--da-color-warning-soft)] text-[color:var(--da-color-warning)] rounded-full text-sm">
                    {domain}
                    <button
                      onClick={() => removeDomain(groupIdx, domain)}
                      className="text-[color:var(--da-color-warning)] hover:opacity-80 font-bold"
                      aria-label={`Remove ${domain}`}
                    >
                      ×
                    </button>
                  </span>
                ))}
              </div>
              <div className="flex gap-2">
                <label htmlFor={`rbac-domain-select-${groupIdx}`} className="sr-only">{t('選擇域名', 'Select domain')}</label>
                <select
                  id={`rbac-domain-select-${groupIdx}`}
                  aria-label={t('選擇域名', 'Select domain')}
                  onChange={(e) => {
                    if (e.target.value) {
                      addDomain(groupIdx, e.target.value);
                      e.target.value = '';
                    }
                  }}
                  className="px-2 py-1.5 border border-[color:var(--da-color-surface-border)] rounded text-sm focus:outline-none focus:ring-2 focus:ring-[color:var(--da-color-focus-ring)] bg-[color:var(--da-color-surface)]"
                  defaultValue=""
                >
                  <option value="">{t('選擇或輸入域名...', 'Select or type domain...')}</option>
                  {DOMAIN_EXAMPLES.map(d => (
                    <option key={d} value={d}>{d}</option>
                  ))}
                </select>
                <label htmlFor={`rbac-domain-custom-${groupIdx}`} className="sr-only">{t('自訂域名', 'Custom domain')}</label>
                <input
                  id={`rbac-domain-custom-${groupIdx}`}
                  type="text"
                  placeholder={t('自訂域名', 'Custom domain')}
                  aria-label={t('自訂域名', 'Custom domain')}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                      addDomain(groupIdx, e.currentTarget.value);
                      e.currentTarget.value = '';
                    }
                  }}
                  className="flex-1 px-2 py-1.5 border border-[color:var(--da-color-surface-border)] rounded text-sm focus:outline-none focus:ring-2 focus:ring-[color:var(--da-color-focus-ring)]"
                />
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function StepReview({ groups }) {
  const yaml = useMemo(() => generateRbacYaml(groups), [groups]);
  const warnings = useMemo(() => validateRbac(groups), [groups]);

  const copyToClipboard = useCallback(() => {
    navigator.clipboard.writeText(yaml);
    alert(t('已複製到剪貼板', 'Copied to clipboard'));
  }, [yaml]);

  const downloadYaml = useCallback(() => {
    const blob = new Blob([yaml], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = '_rbac.yaml';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }, [yaml]);

  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-lg font-semibold mb-2">{t('第五步：檢視與匯出', 'Step 5: Review & Export')}</h3>
        <p className="text-sm text-[color:var(--da-color-muted)] mb-4">
          {t('檢查生成的 YAML 配置。複製或下載為 _rbac.yaml', 'Review the generated YAML. Copy or download as _rbac.yaml')}
        </p>
      </div>

      {/* Validation Warnings */}
      {warnings.length > 0 && (
        <div className="space-y-2" role="alert" aria-live="polite" aria-label={t('RBAC 驗證警告', 'RBAC validation warnings')}>
          {warnings.map((w, idx) => (
            <div
              key={idx}
              className={`p-3 rounded-lg text-sm ${
                w.level === 'error'
                  ? 'bg-[color:var(--da-color-error-soft)] border border-[color:var(--da-color-error)]/30 text-[color:var(--da-color-error)]'
                  : 'bg-[color:var(--da-color-warning-soft)] border border-[color:var(--da-color-warning)]/30 text-[color:var(--da-color-warning)]'
              }`}
            >
              {w.level === 'error' ? '⚠️ ' : '⚡ '} {w.msg()}
            </div>
          ))}
        </div>
      )}

      {/* YAML Output */}
      <div className="p-4 border border-[color:var(--da-color-surface-border)] rounded-lg bg-[color:var(--da-color-surface-hover)]">
        <pre className="font-mono text-xs overflow-x-auto text-[color:var(--da-color-fg)] whitespace-pre-wrap break-words">
          {yaml || t('（無數據）', '(No data)')}
        </pre>
      </div>

      {/* Action Buttons */}
      <div className="flex gap-2">
        <button
          onClick={copyToClipboard}
          className="px-4 py-2 bg-[color:var(--da-color-accent)] text-white rounded-lg text-sm font-medium hover:bg-[color:var(--da-color-accent-hover)]"
        >
          📋 {t('複製到剪貼板', 'Copy to Clipboard')}
        </button>
        <button
          onClick={downloadYaml}
          className="px-4 py-2 bg-[color:var(--da-color-success)] text-white rounded-lg text-sm font-medium hover:opacity-90"
        >
          ⬇️ {t('下載 _rbac.yaml', 'Download _rbac.yaml')}
        </button>
      </div>

      {/* Configuration Summary */}
      <div className="p-4 bg-[color:var(--da-color-info-soft)] border border-[color:var(--da-color-info)]/30 rounded-lg" role="status" aria-live="polite" aria-atomic="true">
        <h4 className="text-sm font-semibold text-[color:var(--da-color-fg)] mb-2">{t('配置摘要', 'Configuration Summary')}</h4>
        <ul className="text-sm text-[color:var(--da-color-fg)] space-y-1">
          <li>✓ {t('群組數量：', 'Number of groups: ')}<span className="font-mono font-semibold">{groups.length}</span></li>
          <li>✓ {t('已設定權限：', 'Permissions set: ')}<span className="font-mono font-semibold">{groups.filter(g => g.permission).length}/{groups.length}</span></li>
          <li>✓ {t('有篩選條件：', 'With filters: ')}<span className="font-mono font-semibold">{groups.filter(g => g.environments.length > 0 || g.domains.length > 0).length}</span></li>
        </ul>
      </div>
    </div>
  );
}

/* ── Main Component ── */

export default function RBACSetupWizard() {
  const [currentStep, setCurrentStep] = useState(0);
  const [groups, setGroups] = useState([]);

  const canProceed = useMemo(() => {
    const step = STEPS[currentStep];
    if (step.id === 'groups') return groups.length > 0;
    if (step.id === 'tenants') return groups.every(g => g.tenantMode);
    if (step.id === 'permissions') return groups.every(g => g.permission);
    if (step.id === 'filters') return true;
    return true;
  }, [currentStep, groups]);

  const handleReset = useCallback(() => {
    if (confirm(t('確定要重置所有設定？', 'Reset all settings?'))) {
      setGroups([]);
      setCurrentStep(0);
    }
  }, []);

  const stepContent = {
    groups: <StepGroups groups={groups} onChange={setGroups} />,
    tenants: <StepTenants groups={groups} onChange={setGroups} />,
    permissions: <StepPermissions groups={groups} onChange={setGroups} />,
    filters: <StepFilters groups={groups} onChange={setGroups} />,
    review: <StepReview groups={groups} />,
  };

  return (
    <div className="min-h-screen bg-gradient-to-br from-[color:var(--da-color-bg)] to-[color:var(--da-color-surface-hover)] p-8">
      <div className="max-w-3xl mx-auto">
        {/* Header */}
        <div className="mb-8">
          <h1 className="text-4xl font-bold text-[color:var(--da-color-fg)] mb-2">{t('RBAC 設定精靈', 'RBAC Setup Wizard')}</h1>
          <p className="text-[color:var(--da-color-muted)]">{t('逐步引導建立 _rbac.yaml 配置檔。', 'Step-by-step guide to create your _rbac.yaml configuration.')}</p>
        </div>

        {/* Progress Stepper */}
        <div className="mb-8">
          <div className="flex justify-between mb-2" role="list" aria-label={t('RBAC 設定步驟', 'RBAC configuration steps')}>
            {STEPS.map((step, idx) => (
              <button
                key={step.id}
                role="listitem"
                aria-current={idx === currentStep ? 'step' : undefined}
                onClick={() => setCurrentStep(idx)}
                className={`flex-1 mx-1 py-2 rounded-lg font-medium text-sm transition-all ${
                  idx === currentStep
                    ? 'bg-[color:var(--da-color-accent)] text-white'
                    : idx < currentStep
                    ? 'bg-[color:var(--da-color-success)]/15 text-[color:var(--da-color-success)]'
                    : 'bg-[color:var(--da-color-tag-bg)] text-[color:var(--da-color-tag-fg)]'
                }`}
              >
                {idx < currentStep && '✓ '}{step.label()}
              </button>
            ))}
          </div>
          <div className="w-full bg-[color:var(--da-color-tag-bg)] rounded-full h-1">
            <div
              className="bg-[color:var(--da-color-accent)] h-1 rounded-full transition-all duration-300"
              style={{ width: `${((currentStep + 1) / STEPS.length) * 100}%` }}
            />
          </div>
        </div>

        {/* Step Content */}
        <div className="bg-[color:var(--da-color-surface)] rounded-xl shadow-md p-6 mb-6">
          {stepContent[STEPS[currentStep].id]}
        </div>

        {/* Navigation */}
        <div className="flex gap-3 justify-between">
          <button
            onClick={handleReset}
            className="px-4 py-2 bg-[color:var(--da-color-tag-bg)] text-[color:var(--da-color-fg)] rounded-lg text-sm font-medium hover:bg-[color:var(--da-color-surface-hover)]"
          >
            🔄 {t('重置', 'Reset')}
          </button>

          <div className="flex gap-2">
            <button
              onClick={() => setCurrentStep(Math.max(0, currentStep - 1))}
              disabled={currentStep === 0}
              className={`px-4 py-2 rounded-lg text-sm font-medium ${
                currentStep === 0
                  ? 'bg-[color:var(--da-color-tag-bg)] text-[color:var(--da-color-tag-fg)] cursor-not-allowed'
                  : 'bg-[color:var(--da-color-tag-bg)] text-[color:var(--da-color-fg)] hover:bg-[color:var(--da-color-surface-hover)]'
              }`}
            >
              ← {t('上一步', 'Back')}
            </button>
            <button
              onClick={() => setCurrentStep(Math.min(STEPS.length - 1, currentStep + 1))}
              disabled={!canProceed || currentStep === STEPS.length - 1}
              className={`px-4 py-2 rounded-lg text-sm font-medium ${
                !canProceed || currentStep === STEPS.length - 1
                  ? 'bg-[color:var(--da-color-tag-bg)] text-[color:var(--da-color-tag-fg)] cursor-not-allowed'
                  : 'bg-[color:var(--da-color-accent)] text-white hover:bg-[color:var(--da-color-accent-hover)]'
              }`}
            >
              {t('下一步', 'Next')} →
            </button>
          </div>
        </div>

        {/* Help Text */}
        <div className="mt-6 p-4 bg-[color:var(--da-color-warning-soft)] border border-[color:var(--da-color-warning)]/30 rounded-lg text-sm text-[color:var(--da-color-fg)]">
          💡 {t('提示：生成的 YAML 需放在租戶配置目錄中，並在 CI/CD 流程中驗證。詳見文件。', 'Tip: The generated YAML should be placed in your tenant config directory and validated in your CI/CD pipeline. See docs for details.')}
        </div>
      </div>
    </div>
  );
}
