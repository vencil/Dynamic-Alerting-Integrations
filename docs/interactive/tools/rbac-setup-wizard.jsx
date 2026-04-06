---
title: "RBAC Setup Wizard"
tags: [rbac, authorization, security, setup, wizard]
audience: ["platform-engineer", "sre"]
version: v2.5.0
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
        msg: () => t(`群組 "${group.name}" 有管理員權限且可訪問所有租戶 - 非常寬鬆，請確認', `Group "${group.name}" has admin on all tenants - very broad, please confirm`)
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
        <p className="text-sm text-slate-600 mb-4">
          {t('列出來自你的身份供應商的群組。這些將成為 RBAC 的基礎。', 'List groups from your IdP (e.g., Okta, AAD). These form the basis of RBAC.')}
        </p>
      </div>

      {groups.length > 0 && (
        <div className="space-y-3 mb-4">
          {groups.map((group, idx) => (
            <div key={idx} className="p-4 border border-slate-200 rounded-lg bg-white">
              <div className="flex items-start justify-between mb-3">
                <div className="flex-1">
                  <input
                    type="text"
                    value={group.name}
                    onChange={(e) => updateGroup(idx, { name: e.target.value })}
                    placeholder={t('群組名稱 (如: engineering-team)', 'Group name (e.g., engineering-team)')}
                    className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-400"
                  />
                </div>
                <button
                  onClick={() => removeGroup(idx)}
                  className="ml-2 px-2 py-1 text-slate-500 hover:text-red-600 hover:bg-red-50 rounded text-sm"
                >
                  ✕
                </button>
              </div>
              <input
                type="text"
                value={group.description}
                onChange={(e) => updateGroup(idx, { description: e.target.value })}
                placeholder={t('描述 (選填)', 'Description (optional)')}
                className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-400"
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
          className="flex-1 px-3 py-2 border border-slate-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-400"
        />
        <button
          onClick={addGroup}
          className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700"
        >
          {t('加入群組', 'Add Group')}
        </button>
      </div>

      {groups.length === 0 && (
        <div className="p-4 bg-blue-50 border border-blue-200 rounded-lg text-sm text-slate-700">
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
        <p className="text-sm text-slate-600 mb-4">
          {t('每個群組可以訪問所有租戶、根據前綴模式、或指定租戶。', 'Each group can access all tenants, match by prefix pattern, or specific tenants.')}
        </p>
      </div>

      <div className="space-y-5">
        {groups.map((group, groupIdx) => (
          <div key={groupIdx} className="p-4 border border-slate-200 rounded-lg bg-white">
            <h4 className="font-medium text-slate-900 mb-3">{group.name}</h4>

            <div className="space-y-3">
              <div>
                <label className="flex items-center gap-2 cursor-pointer mb-2">
                  <input
                    type="radio"
                    checked={group.tenantMode === 'all'}
                    onChange={() => updateGroup(groupIdx, { tenantMode: 'all' })}
                    className="w-4 h-4 text-blue-600"
                  />
                  <span className="text-sm font-medium">{t('所有租戶 (*)', 'All tenants (*)')}</span>
                </label>
                <p className="text-xs text-slate-500 ml-6">{t('此群組可訪問所有租戶', 'This group can access all tenants')}</p>
              </div>

              <div>
                <label className="flex items-center gap-2 cursor-pointer mb-2">
                  <input
                    type="radio"
                    checked={group.tenantMode === 'prefix'}
                    onChange={() => updateGroup(groupIdx, { tenantMode: 'prefix' })}
                    className="w-4 h-4 text-blue-600"
                  />
                  <span className="text-sm font-medium">{t('前綴模式', 'Prefix pattern')}</span>
                </label>
                {group.tenantMode === 'prefix' && (
                  <input
                    type="text"
                    value={group.tenantPrefix}
                    onChange={(e) => updateGroup(groupIdx, { tenantPrefix: e.target.value })}
                    placeholder={t('例如: prod-* 或 staging-db-*', 'e.g., prod-* or staging-db-*')}
                    className="ml-6 w-full px-3 py-2 border border-slate-300 rounded text-sm focus:outline-none focus:ring-2 focus:ring-blue-400"
                  />
                )}
              </div>

              <div>
                <label className="flex items-center gap-2 cursor-pointer mb-2">
                  <input
                    type="radio"
                    checked={group.tenantMode === 'specific'}
                    onChange={() => updateGroup(groupIdx, { tenantMode: 'specific' })}
                    className="w-4 h-4 text-blue-600"
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
                          className={`px-3 py-1 rounded text-sm font-medium transition-colors ${
                            group.specificTenants.includes(tenant)
                              ? 'bg-blue-600 text-white'
                              : 'bg-slate-200 text-slate-700 hover:bg-slate-300'
                          }`}
                        >
                          {tenant}
                        </button>
                      ))}
                    </div>
                    <p className="text-xs text-slate-500">{t('已選擇: ', 'Selected: ')}{group.specificTenants.length > 0 ? group.specificTenants.join(', ') : t('無', 'None')}</p>
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
        <p className="text-sm text-slate-600 mb-4">
          {t('定義每個群組的權限等級。權限是階層性的：admin ⊇ write ⊇ read', 'Define permission levels for each group. Permissions are hierarchical: admin ⊇ write ⊇ read')}
        </p>
      </div>

      <div className="p-4 bg-slate-50 border border-slate-200 rounded-lg mb-4">
        <h4 className="text-sm font-semibold mb-3 text-slate-900">{t('權限階層', 'Permission Hierarchy')}</h4>
        <div className="space-y-2">
          {['read', 'write', 'admin'].map(perm => (
            <div key={perm} className="flex items-start gap-3">
              <div className={`w-6 h-6 rounded flex items-center justify-center text-white font-bold text-xs ${
                perm === 'read' ? 'bg-green-500' : perm === 'write' ? 'bg-blue-500' : 'bg-red-600'
              }`}>
                {perm === 'read' ? 'R' : perm === 'write' ? 'W' : 'A'}
              </div>
              <div>
                <div className="font-medium text-sm text-slate-900">{PERMISSION_HIERARCHY[perm].label()}</div>
                <div className="text-xs text-slate-600">{PERMISSION_HIERARCHY[perm].desc()}</div>
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="space-y-4">
        {groups.map((group, idx) => (
          <div key={idx} className="p-4 border border-slate-200 rounded-lg bg-white">
            <h4 className="font-medium text-slate-900 mb-3">{group.name}</h4>
            <div className="flex gap-2">
              {['read', 'write', 'admin'].map(perm => (
                <button
                  key={perm}
                  onClick={() => updateGroup(idx, { permission: perm })}
                  className={`flex-1 px-3 py-2 rounded-lg text-sm font-medium transition-all ${
                    group.permission === perm
                      ? perm === 'read'
                        ? 'bg-green-600 text-white'
                        : perm === 'write'
                        ? 'bg-blue-600 text-white'
                        : 'bg-red-600 text-white'
                      : 'bg-slate-200 text-slate-700 hover:bg-slate-300'
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
        <p className="text-sm text-slate-600 mb-4">
          {t('選填。進一步限制群組訪問特定的環境或業務域名。', 'Optional. Further restrict group access to specific environments or business domains.')}
        </p>
        <details className="mb-4 text-sm border border-slate-200 rounded-lg">
          <summary className="px-3 py-2 cursor-pointer text-blue-600 hover:text-blue-800 font-medium">
            {t('在哪裡找到環境和域名的值？', 'Where do I find environment and domain values?')}
          </summary>
          <div className="px-3 py-2 text-slate-600 bg-slate-50 rounded-b-lg">
            <p className="mb-2">{t(
              '環境和域名值來自 tenant 配置中的 _metadata 區塊。',
              'Environment and domain values come from the _metadata section in tenant configurations.'
            )}</p>
            <ul className="list-disc list-inside space-y-1">
              <li><code>environment</code>: {t('如 production, staging, development', 'e.g. production, staging, development')}</li>
              <li><code>domain</code>: {t('如 finance, ecommerce, infrastructure', 'e.g. finance, ecommerce, infrastructure')}</li>
            </ul>
            <p className="mt-2 text-xs text-slate-500">{t(
              '詳見 docs/governance-security.md 的 RBAC 章節。',
              'See docs/governance-security.md RBAC section for details.'
            )}</p>
          </div>
        </details>
      </div>

      <div className="space-y-5">
        {groups.map((group, groupIdx) => (
          <div key={groupIdx} className="p-4 border border-slate-200 rounded-lg bg-white">
            <h4 className="font-medium text-slate-900 mb-4">{group.name}</h4>

            {/* Environments */}
            <div className="mb-4">
              <label className="text-sm font-medium text-slate-900 mb-2 block">{t('環境篩選', 'Environment Filter')}</label>
              <div className="flex flex-wrap gap-2">
                {ENVIRONMENTS.map(env => (
                  <button
                    key={env}
                    onClick={() => toggleEnv(groupIdx, env)}
                    className={`px-3 py-1.5 rounded-lg text-sm font-medium transition-all ${
                      group.environments.includes(env)
                        ? 'bg-purple-600 text-white'
                        : 'bg-slate-200 text-slate-700 hover:bg-slate-300'
                    }`}
                  >
                    {env}
                  </button>
                ))}
              </div>
              <p className="text-xs text-slate-500 mt-1">{t('未選擇 = 無限制', 'Unchecked = no restriction')}</p>
            </div>

            {/* Domains */}
            <div>
              <label className="text-sm font-medium text-slate-900 mb-2 block">{t('域名篩選', 'Domain Filter')}</label>
              <div className="flex flex-wrap gap-2 mb-2">
                {group.domains.map(domain => (
                  <span key={domain} className="inline-flex items-center gap-1.5 px-3 py-1 bg-orange-100 text-orange-700 rounded-full text-sm">
                    {domain}
                    <button
                      onClick={() => removeDomain(groupIdx, domain)}
                      className="text-orange-600 hover:text-orange-800 font-bold"
                      aria-label={`Remove ${domain}`}
                    >
                      ×
                    </button>
                  </span>
                ))}
              </div>
              <div className="flex gap-2">
                <select
                  onChange={(e) => {
                    if (e.target.value) {
                      addDomain(groupIdx, e.target.value);
                      e.target.value = '';
                    }
                  }}
                  className="px-2 py-1.5 border border-slate-300 rounded text-sm focus:outline-none focus:ring-2 focus:ring-blue-400 bg-white"
                  defaultValue=""
                >
                  <option value="">{t('選擇或輸入域名...', 'Select or type domain...')}</option>
                  {DOMAIN_EXAMPLES.map(d => (
                    <option key={d} value={d}>{d}</option>
                  ))}
                </select>
                <input
                  type="text"
                  placeholder={t('自訂域名', 'Custom domain')}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                      addDomain(groupIdx, e.currentTarget.value);
                      e.currentTarget.value = '';
                    }
                  }}
                  className="flex-1 px-2 py-1.5 border border-slate-300 rounded text-sm focus:outline-none focus:ring-2 focus:ring-blue-400"
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
        <p className="text-sm text-slate-600 mb-4">
          {t('檢查生成的 YAML 配置。複製或下載為 _rbac.yaml', 'Review the generated YAML. Copy or download as _rbac.yaml')}
        </p>
      </div>

      {/* Validation Warnings */}
      {warnings.length > 0 && (
        <div className="space-y-2">
          {warnings.map((w, idx) => (
            <div
              key={idx}
              className={`p-3 rounded-lg text-sm ${
                w.level === 'error'
                  ? 'bg-red-50 border border-red-200 text-red-700'
                  : 'bg-yellow-50 border border-yellow-200 text-yellow-700'
              }`}
            >
              {w.level === 'error' ? '⚠️ ' : '⚡ '} {w.msg()}
            </div>
          ))}
        </div>
      )}

      {/* YAML Output */}
      <div className="p-4 border border-slate-200 rounded-lg bg-slate-50">
        <pre className="font-mono text-xs overflow-x-auto text-slate-800 whitespace-pre-wrap break-words">
          {yaml || t('（無數據）', '(No data)')}
        </pre>
      </div>

      {/* Action Buttons */}
      <div className="flex gap-2">
        <button
          onClick={copyToClipboard}
          className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700"
        >
          📋 {t('複製到剪貼板', 'Copy to Clipboard')}
        </button>
        <button
          onClick={downloadYaml}
          className="px-4 py-2 bg-green-600 text-white rounded-lg text-sm font-medium hover:bg-green-700"
        >
          ⬇️ {t('下載 _rbac.yaml', 'Download _rbac.yaml')}
        </button>
      </div>

      {/* Configuration Summary */}
      <div className="p-4 bg-blue-50 border border-blue-200 rounded-lg">
        <h4 className="text-sm font-semibold text-slate-900 mb-2">{t('配置摘要', 'Configuration Summary')}</h4>
        <ul className="text-sm text-slate-700 space-y-1">
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
    <div className="min-h-screen bg-gradient-to-br from-slate-50 to-slate-100 p-8">
      <div className="max-w-3xl mx-auto">
        {/* Header */}
        <div className="mb-8">
          <h1 className="text-4xl font-bold text-slate-900 mb-2">{t('RBAC 設定精靈', 'RBAC Setup Wizard')}</h1>
          <p className="text-slate-600">{t('逐步引導建立 _rbac.yaml 配置檔。', 'Step-by-step guide to create your _rbac.yaml configuration.')}</p>
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
                    ? 'bg-blue-600 text-white'
                    : idx < currentStep
                    ? 'bg-green-100 text-green-700'
                    : 'bg-slate-200 text-slate-600'
                }`}
              >
                {idx < currentStep && '✓ '}{step.label()}
              </button>
            ))}
          </div>
          <div className="w-full bg-slate-300 rounded-full h-1">
            <div
              className="bg-blue-600 h-1 rounded-full transition-all duration-300"
              style={{ width: `${((currentStep + 1) / STEPS.length) * 100}%` }}
            />
          </div>
        </div>

        {/* Step Content */}
        <div className="bg-white rounded-xl shadow-md p-6 mb-6">
          {stepContent[STEPS[currentStep].id]}
        </div>

        {/* Navigation */}
        <div className="flex gap-3 justify-between">
          <button
            onClick={handleReset}
            className="px-4 py-2 bg-slate-300 text-slate-700 rounded-lg text-sm font-medium hover:bg-slate-400"
          >
            🔄 {t('重置', 'Reset')}
          </button>

          <div className="flex gap-2">
            <button
              onClick={() => setCurrentStep(Math.max(0, currentStep - 1))}
              disabled={currentStep === 0}
              className={`px-4 py-2 rounded-lg text-sm font-medium ${
                currentStep === 0
                  ? 'bg-slate-200 text-slate-400 cursor-not-allowed'
                  : 'bg-slate-300 text-slate-700 hover:bg-slate-400'
              }`}
            >
              ← {t('上一步', 'Back')}
            </button>
            <button
              onClick={() => setCurrentStep(Math.min(STEPS.length - 1, currentStep + 1))}
              disabled={!canProceed || currentStep === STEPS.length - 1}
              className={`px-4 py-2 rounded-lg text-sm font-medium ${
                !canProceed || currentStep === STEPS.length - 1
                  ? 'bg-slate-200 text-slate-400 cursor-not-allowed'
                  : 'bg-blue-600 text-white hover:bg-blue-700'
              }`}
            >
              {t('下一步', 'Next')} →
            </button>
          </div>
        </div>

        {/* Help Text */}
        <div className="mt-6 p-4 bg-amber-50 border border-amber-200 rounded-lg text-sm text-slate-700">
          💡 {t('提示：生成的 YAML 需放在租戶配置目錄中，並在 CI/CD 流程中驗證。詳見文件。', 'Tip: The generated YAML should be placed in your tenant config directory and validated in your CI/CD pipeline. See docs for details.')}
        </div>
      </div>
    </div>
  );
}
