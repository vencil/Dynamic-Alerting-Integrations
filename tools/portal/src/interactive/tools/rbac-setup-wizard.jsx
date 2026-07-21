---
title: "RBAC Setup Wizard"
tags: [rbac, authorization, security, setup, wizard]
audience: ["platform-engineer", "sre"]
version: v2.7.0
lang: en
related: [config-lint, tenant-manager, self-service-portal]
dependencies: [
  "rbac-setup-wizard/fixtures/wizard-defaults.js",
  "rbac-setup-wizard/utils/generators.js",
  "_common/components/ErrorBoundary.jsx",
  "_common/hooks/useCopyToClipboard.js"
]
---

import React, { useState, useMemo, useCallback } from 'react';
// TRK-230e: ESM imports; esbuild bundles them natively (TD-030z retired the jsx-loader import transform).
import { RBAC_STEPS as STEPS, RBAC_PERMISSION_HIERARCHY as PERMISSION_HIERARCHY, RBAC_ENVIRONMENTS as ENVIRONMENTS, RBAC_DOMAIN_EXAMPLES as DOMAIN_EXAMPLES } from './rbac-setup-wizard/fixtures/wizard-defaults.js';
import { rbacGenerateYaml as generateRbacYaml, rbacValidate as validateRbac } from './rbac-setup-wizard/utils/generators.js';
// PR-portal-11: per-step subtree boundary (see operator-setup-wizard).
import { ErrorBoundary } from './_common/components/ErrorBoundary.jsx';
import { useCopyToClipboard } from './_common/hooks/useCopyToClipboard.js';

const t = window.__t || ((zh, en) => en);

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
        claims: [],
        orgScope: '',
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
                placeholder={t('描述（選填，僅供填寫時參考，不會匯出至 YAML）', 'Description (optional — a local note for you, not exported to YAML)')}
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

function StepIdentity({ groups, onChange }) {
  const updateGroup = useCallback((idx, updates) => {
    const newGroups = [...groups];
    newGroups[idx] = { ...newGroups[idx], ...updates };
    onChange(newGroups);
  }, [onChange, groups]);

  const addClaim = useCallback((groupIdx) => {
    const claims = [...(groups[groupIdx].claims || []), { key: '', values: [] }];
    updateGroup(groupIdx, { claims });
  }, [groups, updateGroup]);

  const updateClaimKey = useCallback((groupIdx, claimIdx, key) => {
    const claims = (groups[groupIdx].claims || []).map((c, i) => (i === claimIdx ? { ...c, key } : c));
    updateGroup(groupIdx, { claims });
  }, [groups, updateGroup]);

  const removeClaim = useCallback((groupIdx, claimIdx) => {
    const claims = (groups[groupIdx].claims || []).filter((_, i) => i !== claimIdx);
    updateGroup(groupIdx, { claims });
  }, [groups, updateGroup]);

  const addClaimValue = useCallback((groupIdx, claimIdx, value) => {
    const v = value.trim();
    if (!v) return;
    const claims = (groups[groupIdx].claims || []).map((c, i) => {
      if (i !== claimIdx || c.values.includes(v)) return c;
      return { ...c, values: [...c.values, v] };
    });
    updateGroup(groupIdx, { claims });
  }, [groups, updateGroup]);

  const removeClaimValue = useCallback((groupIdx, claimIdx, value) => {
    const claims = (groups[groupIdx].claims || []).map((c, i) => (
      i === claimIdx ? { ...c, values: c.values.filter(v => v !== value) } : c
    ));
    updateGroup(groupIdx, { claims });
  }, [groups, updateGroup]);

  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-lg font-semibold mb-2">{t('第五步：身分條件（選填）', 'Step 5: Identity Conditions (optional)')}</h3>
        <p className="text-sm text-[color:var(--da-color-muted)] mb-2">
          {t(
            '選填。留空則群組僅以 IdP 群組名稱匹配（今日行為，不變）。加入條件會進一步「收窄」誰能匹配——只會更嚴、不會更寬。',
            'Optional. Leave empty to match the group purely by IdP group name (today\'s behavior, unchanged). Adding conditions only NARROWS who matches — it can never widen access.',
          )}
        </p>
        {/* This tool EMITS policy; it does not enforce it. No value here is verified. */}
        <div className="p-3 mb-2 text-sm bg-[color:var(--da-color-info-soft)] border border-[color:var(--da-color-info)]/30 rounded-lg text-[color:var(--da-color-fg)]" role="note">
          {t(
            '這些條件只有在檔案被 commit 且 tenant-api 載入後才生效；精靈無法驗證任何值是否正確。宣告鍵採「完全字串比對、區分大小寫、不支援萬用字元」。',
            'These conditions only take effect once the file is committed and loaded by tenant-api; the wizard cannot verify any value. Claim keys use exact, case-sensitive string matching with no wildcards.',
          )}
        </div>
      </div>

      <div className="space-y-5">
        {groups.map((group, groupIdx) => (
          <div key={groupIdx} className="p-4 border border-[color:var(--da-color-surface-border)] rounded-lg bg-[color:var(--da-color-surface)]">
            <h4 className="font-medium text-[color:var(--da-color-fg)] mb-3">{group.name}</h4>

            {/* Claim conditions */}
            <div className="mb-5">
              <label className="text-sm font-medium text-[color:var(--da-color-fg)] mb-2 block">{t('宣告條件（claims）', 'Claim conditions')}</label>
              <p className="text-xs text-[color:var(--da-color-muted)] mb-3">
                {t(
                  '每個宣告鍵須已在部署的 --identity-claim-headers 宣告，否則整份 _rbac.yaml 載入失敗。多個鍵之間為「且」，同鍵多值之間為「或」。',
                  'Each claim key must be declared in the deployment\'s --identity-claim-headers, or the whole _rbac.yaml fails to load. Keys are AND-ed; values within a key are OR-ed.',
                )}
              </p>

              {(group.claims || []).map((claim, claimIdx) => (
                <div key={claimIdx} className="p-3 mb-2 border border-[color:var(--da-color-surface-border)] rounded-lg bg-[color:var(--da-color-surface-hover)]">
                  <div className="flex items-center gap-2 mb-2">
                    <label htmlFor={`rbac-claim-key-${groupIdx}-${claimIdx}`} className="sr-only">{t('宣告鍵', 'Claim key')}</label>
                    <input
                      id={`rbac-claim-key-${groupIdx}-${claimIdx}`}
                      type="text"
                      value={claim.key}
                      onChange={(e) => updateClaimKey(groupIdx, claimIdx, e.target.value)}
                      placeholder={t('宣告鍵（如 org-code）', 'Claim key (e.g. org-code)')}
                      aria-label={t('宣告鍵', 'Claim key')}
                      className="flex-1 px-3 py-2 border border-[color:var(--da-color-surface-border)] rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-[color:var(--da-color-focus-ring)]"
                    />
                    <button
                      onClick={() => removeClaim(groupIdx, claimIdx)}
                      aria-label={t(`移除宣告 ${claim.key || claimIdx + 1}`, `Remove claim ${claim.key || claimIdx + 1}`)}
                      className="px-2 py-1 text-[color:var(--da-color-muted)] hover:text-[color:var(--da-color-error-text)] hover:bg-[color:var(--da-color-error-soft)] rounded text-sm"
                    >
                      ✕
                    </button>
                  </div>
                  <div className="flex flex-wrap gap-2 mb-2">
                    {claim.values.map(value => (
                      <span key={value} className="inline-flex items-center gap-1.5 px-3 py-1 bg-[color:var(--da-color-tag-bg)] text-[color:var(--da-color-fg)] rounded-full text-sm">
                        {value}
                        <button
                          onClick={() => removeClaimValue(groupIdx, claimIdx, value)}
                          className="text-[color:var(--da-color-muted)] hover:text-[color:var(--da-color-error-text)] font-bold"
                          aria-label={t(`移除值 ${value}`, `Remove value ${value}`)}
                        >
                          ×
                        </button>
                      </span>
                    ))}
                  </div>
                  <label htmlFor={`rbac-claim-val-${groupIdx}-${claimIdx}`} className="sr-only">{t('允許的值', 'Allowed value')}</label>
                  <input
                    id={`rbac-claim-val-${groupIdx}-${claimIdx}`}
                    type="text"
                    placeholder={t('輸入允許的值後按 Enter', 'Type an allowed value, press Enter')}
                    aria-label={t('允許的值', 'Allowed value')}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') {
                        addClaimValue(groupIdx, claimIdx, e.currentTarget.value);
                        e.currentTarget.value = '';
                      }
                    }}
                    className="w-full px-3 py-2 border border-[color:var(--da-color-surface-border)] rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-[color:var(--da-color-focus-ring)]"
                  />
                </div>
              ))}

              <button
                onClick={() => addClaim(groupIdx)}
                className="px-3 py-1.5 bg-[color:var(--da-color-tag-bg)] text-[color:var(--da-color-fg)] rounded-lg text-sm font-medium hover:bg-[color:var(--da-color-surface-hover)]"
              >
                + {t('新增宣告條件', 'Add claim condition')}
              </button>
            </div>

            {/* org-scope axis */}
            <div>
              <label htmlFor={`rbac-orgscope-${groupIdx}`} className="text-sm font-medium text-[color:var(--da-color-fg)] mb-2 block">{t('組織範圍（org-scope，選填）', 'Org-scope (optional)')}</label>
              <input
                id={`rbac-orgscope-${groupIdx}`}
                type="text"
                value={group.orgScope || ''}
                onChange={(e) => updateGroup(groupIdx, { orgScope: e.target.value })}
                placeholder={t('組織宣告鍵（如 org-code）', 'Org claim key (e.g. org-code)')}
                aria-label={t('組織範圍宣告鍵', 'Org-scope claim key')}
                className="w-full px-3 py-2 border border-[color:var(--da-color-surface-border)] rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-[color:var(--da-color-focus-ring)]"
              />
              {group.orgScope && (
                <div className="mt-2 p-3 text-sm bg-[color:var(--da-color-warning-soft)] border border-[color:var(--da-color-warning)]/30 rounded-lg text-[color:var(--da-color-fg)]" role="alert" aria-live="polite">
                  <p className="font-semibold mb-1">{t('開啟 org-scope 前請確認：', 'Before enabling org-scope, confirm:')}</p>
                  <ul className="list-disc list-inside space-y-1 text-xs">
                    <li>{t(`宣告鍵 "${group.orgScope}" 必須已在部署宣告，否則整份檔案載入失敗。`, `Claim key "${group.orgScope}" must be declared in the deployment, or the whole file fails to load.`)}</li>
                    <li>{t('這條規則涵蓋的每個租戶都須在 _tenant_orgs.yaml 標記，且組織值與 caller 的宣告值完全字串相等。', 'Every tenant this rule covers must be labeled in _tenant_orgs.yaml, with an org value string-equal to the caller\'s claim value.')}</li>
                    <li>{t('Shadow 模式不保護「caller 缺少或不匹配該宣告值」——這兩種情況在 shadow 與 enforce 下都會拒絕。', 'Shadow mode does NOT protect "caller missing or mismatched claim value" — both are denied under shadow AND enforce.')}</li>
                    <li>{t('上述拒絕不會計入 would-deny 觀測指標，soak 觀察期看不出來。', 'Those denials are invisible to the would-deny soak metric.')}</li>
                  </ul>
                  <p className="mt-2 text-xs">{t('下一步：commit 前先用租戶管理員的稽核（dry-run）驗證這份設定。', 'Next: verify this config with the admin audit (dry-run) before committing.')}</p>
                </div>
              )}
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
  const { copy } = useCopyToClipboard();

  const copyToClipboard = useCallback(() => {
    copy(yaml);
    alert(t('已複製到剪貼板', 'Copied to clipboard'));
  }, [yaml, copy]);

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
        <h3 className="text-lg font-semibold mb-2">{t('第六步：檢視與匯出', 'Step 6: Review & Export')}</h3>
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
          <li><span aria-hidden="true">✓</span> {t('群組數量：', 'Number of groups: ')}<span className="font-mono font-semibold">{groups.length}</span></li>
          <li><span aria-hidden="true">✓</span> {t('已設定權限：', 'Permissions set: ')}<span className="font-mono font-semibold">{groups.filter(g => g.permission).length}/{groups.length}</span></li>
          <li><span aria-hidden="true">✓</span> {t('有篩選條件：', 'With filters: ')}<span className="font-mono font-semibold">{groups.filter(g => g.environments.length > 0 || g.domains.length > 0).length}</span></li>
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
    identity: <StepIdentity groups={groups} onChange={setGroups} />,
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
                {idx < currentStep && <span aria-hidden="true">✓ </span>}{step.label()}
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

        {/* Step Content — PR-portal-11: per-step boundary, fresh
            mount per step via key={...id}. */}
        <div className="bg-[color:var(--da-color-surface)] rounded-xl shadow-md p-6 mb-6">
          <ErrorBoundary
            key={STEPS[currentStep].id}
            scope={'rbac-setup-wizard/step/' + STEPS[currentStep].id}
          >
            {stepContent[STEPS[currentStep].id]}
          </ErrorBoundary>
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
