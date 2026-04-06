---
title: "Release Notes Generator"
tags: [release, changelog, automation, communication]
audience: [platform-engineer, sre]
version: v2.5.0
lang: en
related: [changelog-viewer, deployment-wizard, health-dashboard]
---

import React, { useState, useMemo } from 'react';

const t = window.__t || ((zh, en) => en);

const ROLES = {
  'platform-engineer': {
    label: () => t('平台工程師', 'Platform Engineer'),
    description: () => t('基礎設施、部署、配置', 'Infrastructure, deployment, configuration'),
    categories: ['Features', 'Breaking Changes', 'Fixes'],
    keywords: ['k8s', 'helm', 'docker', 'deployment', 'infrastructure', 'configuration', 'exporter', 'architecture']
  },
  'domain-expert': {
    label: () => t('領域專家', 'Domain Expert'),
    description: () => t('告警規則、指標、Rule Packs', 'Alert rules, metrics, Rule Packs'),
    categories: ['Features', 'Fixes', 'Documentation'],
    keywords: ['rule pack', 'metric', 'threshold', 'alert', 'severity', 'recording rule', 'prom']
  },
  'tenant-user': {
    label: () => t('租戶用戶', 'Tenant User'),
    description: () => t('操作、配置、UI 變更', 'Operation, configuration, UI changes'),
    categories: ['Features', 'Fixes'],
    keywords: ['tenant', 'config', 'routing', 'ui', 'portal', 'yaml', 'receiver', 'notification']
  },
  'sre': {
    label: () => t('SRE / 運維', 'SRE / Operations'),
    description: () => t('告警、監控、可靠性', 'Alerting, monitoring, reliability'),
    categories: ['Features', 'Breaking Changes', 'Fixes'],
    keywords: ['alert', 'alertmanager', 'dedup', 'suppression', 'maintenance mode', 'silent mode', 'routing', 'receiver']
  }
};

// Sample CHANGELOG data structure (or parse from markdown)
const SAMPLE_CHANGELOG = `
## v2.5.0 (2026-04-06)

### Features
- [platform-engineer] New Helm values.schema.json validation for all deployments
- [domain-expert] Rule Pack versioning support — breaking rule changes now tracked
- [tenant-user] Portal UI redesign: dark mode toggle + responsive mobile layout
- [sre] Enhanced alert correlation engine with multi-source dedup
- [platform-engineer] Prometheus cardinality guardrails: auto-truncate >10k series per tenant

### Breaking Changes
- [platform-engineer] Removed deprecated \`-config-file\` flag, use \`-config-dir\` only
- [sre] Alertmanager group_wait minimum raised from 0 to 5s (TIMING_GUARDRAILS)
- [domain-expert] Rule Pack schema: \`recordingRules\` key renamed to \`recording_rules\`

### Fixes
- [sre] Fixed alert storm during config reload with configmap-reload sidecar race condition
- [platform-engineer] K8s MCP timeout fallback now properly clears TCP connections
- [tenant-user] Portal config editor no longer drops trailing whitespace in YAML
- [domain-expert] Rule Pack metrics now correctly labeled in platform-data.json

### Documentation
- [platform-engineer] New 'Scaling to 1000+ Tenants' architecture playbook
- [sre] Alert correlation flow diagrams added to troubleshooting guide
- [domain-expert] Rule Pack contributor guide with schema examples
- [tenant-user] Quick-start portal walkthrough video link added
`;

function parseChangelogMarkdown(text) {
  /** Parse CHANGELOG markdown into structured format. */
  const sections = {};
  let currentCategory = null;
  let currentItems = [];

  for (const line of text.split('\n')) {
    const categoryMatch = line.match(/^### (Features|Fixes|Breaking Changes|Documentation)$/);
    if (categoryMatch) {
      if (currentCategory && currentItems.length > 0) {
        sections[currentCategory] = currentItems;
      }
      currentCategory = categoryMatch[1];
      currentItems = [];
      continue;
    }

    if (currentCategory && line.match(/^- \[/)) {
      const itemMatch = line.match(/^- \[(.*?)\]\s+(.+)$/);
      if (itemMatch) {
        const [, roles, description] = itemMatch;
        const roleList = roles.split(',').map(r => r.trim());
        currentItems.push({ roles: roleList, description });
      }
    }
  }

  if (currentCategory && currentItems.length > 0) {
    sections[currentCategory] = currentItems;
  }

  return sections;
}

function filterChangesByRole(sections, selectedRoles) {
  /** Filter changelog items by selected role. */
  const filtered = {};

  for (const [category, items] of Object.entries(sections)) {
    const relevantItems = items.filter(item =>
      selectedRoles.some(role => item.roles.includes(role))
    );
    if (relevantItems.length > 0) {
      filtered[category] = relevantItems;
    }
  }

  return filtered;
}

export default function ReleaseNotesGenerator() {
  const [inputMode, setInputMode] = useState('paste'); // 'paste' or 'platform-data'
  const [changelogText, setChangelogText] = useState(SAMPLE_CHANGELOG);
  const [selectedRoles, setSelectedRoles] = useState(['platform-engineer']);
  const [lang, setLang] = useState('en');
  const [copied, setCopied] = useState(false);

  const sections = useMemo(() => parseChangelogMarkdown(changelogText), [changelogText]);
  const filtered = useMemo(() => filterChangesByRole(sections, selectedRoles), [sections, selectedRoles]);

  const toggleRole = (role) => {
    setSelectedRoles(prev =>
      prev.includes(role) ? prev.filter(r => r !== role) : [...prev, role]
    );
  };

  const generateMarkdown = () => {
    let output = [];
    const roleLabel = selectedRoles.map(r => ROLES[r]?.label?.() || r).join(' + ');
    output.push(`# ${t('發行說明', 'Release Notes')} — ${roleLabel}`);
    output.push('');

    for (const [category, items] of Object.entries(filtered)) {
      output.push(`## ${category}`);
      output.push('');
      for (const item of items) {
        output.push(`- ${item.description}`);
      }
      output.push('');
    }

    return output.join('\n');
  };

  const markdown = generateMarkdown();

  const copyToClipboard = () => {
    navigator.clipboard.writeText(markdown).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  };

  return (
    <div className="min-h-screen bg-gradient-to-br from-blue-50 to-indigo-50 p-8">
      <div className="max-w-6xl mx-auto">
        {/* Header */}
        <div className="mb-8">
          <h1 className="text-4xl font-bold text-slate-900 mb-2">
            {t('發行說明生成器', 'Release Notes Generator')}
          </h1>
          <p className="text-slate-600">
            {t('從 CHANGELOG 生成針對不同角色的發行說明', 'Generate role-specific release notes from CHANGELOG')}
          </p>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* Left: Input + Config */}
          <div className="lg:col-span-1 space-y-4">
            {/* Input Mode */}
            <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-6">
              <h3 className="text-sm font-semibold text-slate-900 mb-3">
                {t('輸入來源', 'Input Source')}
              </h3>
              <div className="space-y-2">
                <label className="flex items-center gap-2 cursor-pointer">
                  <input
                    type="radio"
                    checked={inputMode === 'paste'}
                    onChange={() => setInputMode('paste')}
                    className="w-4 h-4"
                  />
                  <span className="text-sm text-slate-700">{t('貼上 CHANGELOG', 'Paste CHANGELOG')}</span>
                </label>
                <label className="flex items-center gap-2 cursor-pointer">
                  <input
                    type="radio"
                    checked={inputMode === 'platform-data'}
                    onChange={() => setInputMode('platform-data')}
                    className="w-4 h-4"
                  />
                  <span className="text-sm text-slate-700">{t('從 platform-data.json 加載', 'Load from platform-data.json')}</span>
                </label>
              </div>
            </div>

            {/* Role Selector */}
            <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-6">
              <h3 className="text-sm font-semibold text-slate-900 mb-3">
                {t('選擇角色', 'Select Roles')}
              </h3>
              <div className="space-y-2">
                {Object.entries(ROLES).map(([roleId, role]) => (
                  <label key={roleId} className="flex items-start gap-2 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={selectedRoles.includes(roleId)}
                      onChange={() => toggleRole(roleId)}
                      className="w-4 h-4 mt-1"
                    />
                    <div className="flex-1">
                      <div className="text-sm font-medium text-slate-900">{role.label()}</div>
                      <div className="text-xs text-slate-500">{role.description()}</div>
                    </div>
                  </label>
                ))}
              </div>
            </div>

            {/* Language Toggle */}
            <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-6">
              <h3 className="text-sm font-semibold text-slate-900 mb-3">
                {t('語言', 'Language')}
              </h3>
              <div className="flex gap-2">
                <button
                  onClick={() => setLang('en')}
                  className={`flex-1 px-3 py-2 rounded-lg text-sm font-medium transition-colors ${
                    lang === 'en'
                      ? 'bg-blue-100 text-blue-800'
                      : 'bg-slate-100 text-slate-600 hover:bg-slate-200'
                  }`}
                >
                  English
                </button>
                <button
                  onClick={() => setLang('zh')}
                  className={`flex-1 px-3 py-2 rounded-lg text-sm font-medium transition-colors ${
                    lang === 'zh'
                      ? 'bg-blue-100 text-blue-800'
                      : 'bg-slate-100 text-slate-600 hover:bg-slate-200'
                  }`}
                >
                  中文
                </button>
              </div>
            </div>
          </div>

          {/* Center: CHANGELOG Input */}
          {inputMode === 'paste' && (
            <div className="lg:col-span-1 bg-white rounded-xl shadow-sm border border-slate-200 p-6">
              <h3 className="text-sm font-semibold text-slate-900 mb-3">
                {t('CHANGELOG 內容', 'CHANGELOG Content')}
              </h3>
              <textarea
                value={changelogText}
                onChange={(e) => setChangelogText(e.target.value)}
                className="w-full h-96 p-3 border border-slate-300 rounded-lg font-mono text-xs focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                placeholder={t('粘貼 CHANGELOG.md 的內容...', 'Paste CHANGELOG.md content...')}
              />
              <div className="mt-3 text-xs text-slate-500">
                {t('格式：### 分類名 + - [role] 描述', 'Format: ### Category + - [role] description')}
              </div>
            </div>
          )}

          {/* Right: Preview + Export */}
          <div className="lg:col-span-1 bg-white rounded-xl shadow-sm border border-slate-200 p-6">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-sm font-semibold text-slate-900">
                {t('預覽', 'Preview')}
              </h3>
              <button
                onClick={copyToClipboard}
                className={`px-3 py-1.5 rounded-lg text-sm font-medium transition-colors ${
                  copied
                    ? 'bg-green-100 text-green-800'
                    : 'bg-blue-100 text-blue-800 hover:bg-blue-200'
                }`}
              >
                {copied ? '✓ ' + t('已複製', 'Copied') : t('複製', 'Copy')}
              </button>
            </div>

            <div className="bg-slate-50 rounded-lg p-4 h-96 overflow-y-auto border border-slate-200 font-mono text-xs whitespace-pre-wrap break-words">
              {markdown || t('（無結果）', '(no results)')}
            </div>

            {/* Export Options */}
            <div className="mt-4 space-y-2">
              <button
                onClick={() => {
                  const element = document.createElement('a');
                  element.href = 'data:text/plain;charset=utf-8,' + encodeURIComponent(markdown);
                  element.download = `release-notes-${selectedRoles.join('-')}.md`;
                  document.body.appendChild(element);
                  element.click();
                  document.body.removeChild(element);
                }}
                className="w-full px-4 py-2 bg-slate-600 text-white text-sm font-medium rounded-lg hover:bg-slate-700 transition-colors"
              >
                {t('下載 Markdown', 'Download Markdown')}
              </button>
            </div>
          </div>
        </div>

        {/* Usage Notes */}
        <div className="mt-8 bg-blue-50 border border-blue-200 rounded-xl p-6">
          <h4 className="font-semibold text-blue-900 mb-2">💡 {t('使用提示', 'Tips')}</h4>
          <ul className="text-sm text-blue-800 space-y-1">
            <li>• {t('按 role 篩選變更項目，生成針對特定受眾的發行說明', 'Filter changes by role to generate release notes for specific audiences')}</li>
            <li>• {t('支援多角色組合 — 同時選擇多個角色查看綜合發行說明', 'Combine multiple roles to see consolidated release notes')}</li>
            <li>• {t('CHANGELOG 格式：### Category + - [role1, role2] description', 'CHANGELOG format: ### Category + - [role1, role2] description')}</li>
          </ul>
        </div>
      </div>
    </div>
  );
}
