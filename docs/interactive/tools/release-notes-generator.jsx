---
title: "Release Notes Generator"
tags: [release, changelog, automation, communication]
audience: [maintainer]
version: v2.7.0
lang: en
related: [deployment-wizard, health-dashboard, platform-health]
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
## v2.6.0 (2026-04-06)

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

function generateAutoSummary(filtered, selectedRoles) {
  /** Generate a 2-3 sentence auto-summary for selected roles. */
  const breakingCount = filtered['Breaking Changes']?.length || 0;
  const featuresCount = filtered['Features']?.length || 0;
  const fixesCount = filtered['Fixes']?.length || 0;

  const roleLabels = selectedRoles.map(r => ROLES[r]?.label?.() || r).join(' / ');

  const impactItems = [];
  if (breakingCount > 0) {
    impactItems.push(t(
      `${breakingCount} 個重大變更`,
      `${breakingCount} breaking change${breakingCount !== 1 ? 's' : ''}`
    ));
  }
  if (featuresCount > 0) {
    impactItems.push(t(
      `${featuresCount} 個新功能`,
      `${featuresCount} new feature${featuresCount !== 1 ? 's' : ''}`
    ));
  }
  if (fixesCount > 0) {
    impactItems.push(t(
      `${fixesCount} 個修復`,
      `${fixesCount} fix${fixesCount !== 1 ? 's' : ''}`
    ));
  }

  const impactStr = impactItems.join(t('、', ', '));

  const sentences = [];
  if (breakingCount > 0) {
    sentences.push(t(
      `本版本包含 ${impactStr} 需要您的注意。`,
      `This release includes ${impactStr} that require your attention.`
    ));
  } else if (impactItems.length > 0) {
    sentences.push(t(
      `本版本為您帶來 ${impactStr}，持續改進平台。`,
      `This release brings you ${impactStr}, continuously improving the platform.`
    ));
  } else {
    sentences.push(t(
      `本版本沒有與您相關的變更。`,
      `This release contains no changes relevant to your role.`
    ));
    return sentences[0];
  }

  if (selectedRoles.length === 1) {
    const mostImpactful = breakingCount > 0 ? 'Breaking Changes' : featuresCount > 0 ? 'Features' : 'Fixes';
    if (filtered[mostImpactful]?.length > 0) {
      const firstItem = filtered[mostImpactful][0].description.substring(0, 50);
      sentences.push(t(
        `亮點包括：${firstItem}...`,
        `Highlights include: ${firstItem}...`
      ));
    }
  }

  return sentences.join(' ');
}

export default function ReleaseNotesGenerator() {
  const [inputMode, setInputMode] = useState('paste'); // 'paste' or 'platform-data'
  const [changelogText, setChangelogText] = useState(SAMPLE_CHANGELOG);
  const [selectedRoles, setSelectedRoles] = useState(['platform-engineer']);
  const [lang, setLang] = useState('en');
  const [copied, setCopied] = useState(false);

  const sections = useMemo(() => parseChangelogMarkdown(changelogText), [changelogText]);
  const filtered = useMemo(() => filterChangesByRole(sections, selectedRoles), [sections, selectedRoles]);
  const summary = useMemo(() => generateAutoSummary(filtered, selectedRoles), [filtered, selectedRoles]);

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
    <div style={{
      minHeight: '100vh',
      backgroundColor: 'var(--da-color-bg)',
      color: 'var(--da-color-fg)',
      padding: `var(--da-space-8)`
    }}>
      <div style={{
        maxWidth: '1440px',
        marginLeft: 'auto',
        marginRight: 'auto'
      }}>
        {/* Header */}
        <div style={{ marginBottom: `var(--da-space-8)` }}>
          <h1 style={{
            fontSize: 'var(--da-font-size-2xl)',
            fontWeight: 'var(--da-font-weight-bold)',
            color: 'var(--da-color-fg)',
            marginBottom: `var(--da-space-2)`
          }}>
            {t('發行說明生成器', 'Release Notes Generator')}
          </h1>
          <p style={{
            color: 'var(--da-color-muted)',
            fontSize: 'var(--da-font-size-base)'
          }}>
            {t('從 CHANGELOG 生成針對不同角色的發行說明', 'Generate role-specific release notes from CHANGELOG')}
          </p>
        </div>

        <div style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))',
          gap: `var(--da-space-6)`,
          gridAutoColumns: 'minmax(0, 1fr)'
        }}>
          {/* Left: Input + Config */}
          <div style={{ display: 'flex', flexDirection: 'column', gap: `var(--da-space-4)` }}>
            {/* Input Mode */}
            <div style={{
              backgroundColor: 'var(--da-color-surface)',
              borderRadius: 'var(--da-radius-lg)',
              boxShadow: 'var(--da-shadow-subtle)',
              border: `1px solid var(--da-color-surface-border)`,
              padding: `var(--da-space-6)`
            }}>
              <h3 style={{
                fontSize: 'var(--da-font-size-sm)',
                fontWeight: 'var(--da-font-weight-semibold)',
                color: 'var(--da-color-fg)',
                marginBottom: `var(--da-space-3)`
              }}>
                {t('輸入來源', 'Input Source')}
              </h3>
              <div style={{ display: 'flex', flexDirection: 'column', gap: `var(--da-space-2)` }}>
                <label style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: `var(--da-space-2)`,
                  cursor: 'pointer'
                }}>
                  <input
                    type="radio"
                    checked={inputMode === 'paste'}
                    onChange={() => setInputMode('paste')}
                    style={{
                      width: '16px',
                      height: '16px',
                      cursor: 'pointer'
                    }}
                  />
                  <span style={{
                    fontSize: 'var(--da-font-size-sm)',
                    color: 'var(--da-color-fg)'
                  }}>{t('貼上 CHANGELOG', 'Paste CHANGELOG')}</span>
                </label>
                <label style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: `var(--da-space-2)`,
                  cursor: 'pointer'
                }}>
                  <input
                    type="radio"
                    checked={inputMode === 'platform-data'}
                    onChange={() => setInputMode('platform-data')}
                    style={{
                      width: '16px',
                      height: '16px',
                      cursor: 'pointer'
                    }}
                  />
                  <span style={{
                    fontSize: 'var(--da-font-size-sm)',
                    color: 'var(--da-color-fg)'
                  }}>{t('從 platform-data.json 加載', 'Load from platform-data.json')}</span>
                </label>
              </div>
            </div>

            {/* Role Selector */}
            <div style={{
              backgroundColor: 'var(--da-color-surface)',
              borderRadius: 'var(--da-radius-lg)',
              boxShadow: 'var(--da-shadow-subtle)',
              border: `1px solid var(--da-color-surface-border)`,
              padding: `var(--da-space-6)`
            }}>
              <h3 style={{
                fontSize: 'var(--da-font-size-sm)',
                fontWeight: 'var(--da-font-weight-semibold)',
                color: 'var(--da-color-fg)',
                marginBottom: `var(--da-space-3)`
              }}>
                {t('選擇角色', 'Select Roles')}
              </h3>
              <div style={{ display: 'flex', flexDirection: 'column', gap: `var(--da-space-2)` }}>
                {Object.entries(ROLES).map(([roleId, role]) => (
                  <label key={roleId} style={{
                    display: 'flex',
                    alignItems: 'flex-start',
                    gap: `var(--da-space-2)`,
                    cursor: 'pointer'
                  }}>
                    <input
                      type="checkbox"
                      checked={selectedRoles.includes(roleId)}
                      onChange={() => toggleRole(roleId)}
                      style={{
                        width: '16px',
                        height: '16px',
                        marginTop: `var(--da-space-1)`,
                        cursor: 'pointer'
                      }}
                    />
                    <div style={{ flex: 1 }}>
                      <div style={{
                        fontSize: 'var(--da-font-size-sm)',
                        fontWeight: 'var(--da-font-weight-medium)',
                        color: 'var(--da-color-fg)'
                      }}>{role.label()}</div>
                      <div style={{
                        fontSize: 'var(--da-font-size-xs)',
                        color: 'var(--da-color-muted)'
                      }}>{role.description()}</div>
                    </div>
                  </label>
                ))}
              </div>
            </div>

            {/* Language Toggle */}
            <div style={{
              backgroundColor: 'var(--da-color-surface)',
              borderRadius: 'var(--da-radius-lg)',
              boxShadow: 'var(--da-shadow-subtle)',
              border: `1px solid var(--da-color-surface-border)`,
              padding: `var(--da-space-6)`
            }}>
              <h3 style={{
                fontSize: 'var(--da-font-size-sm)',
                fontWeight: 'var(--da-font-weight-semibold)',
                color: 'var(--da-color-fg)',
                marginBottom: `var(--da-space-3)`
              }}>
                {t('語言', 'Language')}
              </h3>
              <div style={{ display: 'flex', gap: `var(--da-space-2)` }}>
                <button
                  onClick={() => setLang('en')}
                  style={{
                    flex: 1,
                    padding: `var(--da-space-2) var(--da-space-3)`,
                    borderRadius: 'var(--da-radius-md)',
                    fontSize: 'var(--da-font-size-sm)',
                    fontWeight: 'var(--da-font-weight-medium)',
                    backgroundColor: lang === 'en' ? 'var(--da-color-info-soft)' : 'var(--da-color-surface-border)',
                    color: lang === 'en' ? 'var(--da-color-info)' : 'var(--da-color-muted)',
                    border: 'none',
                    cursor: 'pointer',
                    transition: `background-color var(--da-transition-base), color var(--da-transition-base)`
                  }}
                  onMouseEnter={(e) => {
                    if (lang !== 'en') e.target.style.backgroundColor = 'var(--da-color-surface-border)';
                  }}
                  onMouseLeave={(e) => {
                    if (lang !== 'en') e.target.style.backgroundColor = 'var(--da-color-surface-border)';
                  }}
                >
                  English
                </button>
                <button
                  onClick={() => setLang('zh')}
                  style={{
                    flex: 1,
                    padding: `var(--da-space-2) var(--da-space-3)`,
                    borderRadius: 'var(--da-radius-md)',
                    fontSize: 'var(--da-font-size-sm)',
                    fontWeight: 'var(--da-font-weight-medium)',
                    backgroundColor: lang === 'zh' ? 'var(--da-color-info-soft)' : 'var(--da-color-surface-border)',
                    color: lang === 'zh' ? 'var(--da-color-info)' : 'var(--da-color-muted)',
                    border: 'none',
                    cursor: 'pointer',
                    transition: `background-color var(--da-transition-base), color var(--da-transition-base)`
                  }}
                  onMouseEnter={(e) => {
                    if (lang !== 'zh') e.target.style.backgroundColor = 'var(--da-color-surface-border)';
                  }}
                  onMouseLeave={(e) => {
                    if (lang !== 'zh') e.target.style.backgroundColor = 'var(--da-color-surface-border)';
                  }}
                >
                  中文
                </button>
              </div>
            </div>
          </div>

          {/* Center: CHANGELOG Input */}
          {inputMode === 'paste' && (
            <div style={{
              backgroundColor: 'var(--da-color-surface)',
              borderRadius: 'var(--da-radius-lg)',
              boxShadow: 'var(--da-shadow-subtle)',
              border: `1px solid var(--da-color-surface-border)`,
              padding: `var(--da-space-6)`
            }}>
              <h3 style={{
                fontSize: 'var(--da-font-size-sm)',
                fontWeight: 'var(--da-font-weight-semibold)',
                color: 'var(--da-color-fg)',
                marginBottom: `var(--da-space-3)`
              }}>
                {t('CHANGELOG 內容', 'CHANGELOG Content')}
              </h3>
              <textarea
                value={changelogText}
                onChange={(e) => setChangelogText(e.target.value)}
                style={{
                  width: '100%',
                  height: '384px',
                  padding: `var(--da-space-3)`,
                  border: `1px solid var(--da-color-surface-border)`,
                  borderRadius: 'var(--da-radius-md)',
                  fontFamily: 'monospace',
                  fontSize: 'var(--da-font-size-xs)',
                  backgroundColor: 'var(--da-color-bg)',
                  color: 'var(--da-color-fg)',
                  boxSizing: 'border-box',
                  transition: `border-color var(--da-transition-base), box-shadow var(--da-transition-base)`
                }}
                onFocus={(e) => {
                  e.target.style.outline = 'none';
                  e.target.style.borderColor = 'var(--da-color-accent)';
                  e.target.style.boxShadow = '0 0 0 2px var(--da-color-accent-soft)';
                }}
                onBlur={(e) => {
                  e.target.style.borderColor = 'var(--da-color-surface-border)';
                  e.target.style.boxShadow = 'none';
                }}
                placeholder={t('粘貼 CHANGELOG.md 的內容...', 'Paste CHANGELOG.md content...')}
              />
              <div style={{
                marginTop: `var(--da-space-3)`,
                fontSize: 'var(--da-font-size-xs)',
                color: 'var(--da-color-muted)'
              }}>
                {t('格式：### 分類名 + - [role] 描述', 'Format: ### Category + - [role] description')}
              </div>
            </div>
          )}

          {/* Right: Preview + Export */}
          <div style={{
            backgroundColor: 'var(--da-color-surface)',
            borderRadius: 'var(--da-radius-lg)',
            boxShadow: 'var(--da-shadow-subtle)',
            border: `1px solid var(--da-color-surface-border)`,
            padding: `var(--da-space-6)`
          }}>
            {/* Auto-Summary Card */}
            {(filtered['Breaking Changes']?.length > 0 || filtered['Features']?.length > 0 || filtered['Fixes']?.length > 0) && (
              <div style={{
                backgroundColor: 'var(--da-color-info-soft)',
                borderLeft: `4px solid var(--da-color-info)`,
                borderRadius: 'var(--da-radius-md)',
                padding: `var(--da-space-4)`,
                marginBottom: `var(--da-space-4)`
              }}>
                <div style={{
                  fontSize: 'var(--da-font-size-sm)',
                  fontWeight: 'var(--da-font-weight-medium)',
                  color: 'var(--da-color-info)',
                  lineHeight: 1.5
                }}>
                  {t('概覽', 'What\'s New')}
                </div>
                <div style={{
                  fontSize: 'var(--da-font-size-sm)',
                  color: 'var(--da-color-info)',
                  marginTop: `var(--da-space-2)`,
                  lineHeight: 1.6
                }}>
                  {summary}
                </div>
              </div>
            )}

            {/* Preview Header */}
            <div style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              marginBottom: `var(--da-space-4)`
            }}>
              <h3 style={{
                fontSize: 'var(--da-font-size-sm)',
                fontWeight: 'var(--da-font-weight-semibold)',
                color: 'var(--da-color-fg)'
              }}>
                {t('預覽', 'Preview')}
              </h3>
              <button
                onClick={copyToClipboard}
                style={{
                  padding: `var(--da-space-1) var(--da-space-3)`,
                  borderRadius: 'var(--da-radius-md)',
                  fontSize: 'var(--da-font-size-sm)',
                  fontWeight: 'var(--da-font-weight-medium)',
                  backgroundColor: copied ? 'var(--da-color-success-soft)' : 'var(--da-color-info-soft)',
                  color: copied ? 'var(--da-color-success)' : 'var(--da-color-info)',
                  border: 'none',
                  cursor: 'pointer',
                  transition: `background-color var(--da-transition-fast), color var(--da-transition-fast)`
                }}
                onMouseEnter={(e) => {
                  if (!copied) e.target.style.backgroundColor = 'var(--da-color-info)';
                }}
                onMouseLeave={(e) => {
                  if (!copied) e.target.style.backgroundColor = 'var(--da-color-info-soft)';
                }}
              >
                {copied ? '✓ ' + t('已複製', 'Copied') : t('複製', 'Copy')}
              </button>
            </div>

            {/* Preview Content */}
            <div style={{
              backgroundColor: 'var(--da-color-bg)',
              borderRadius: 'var(--da-radius-md)',
              padding: `var(--da-space-4)`,
              height: '384px',
              overflowY: 'auto',
              border: `1px solid var(--da-color-surface-border)`,
              fontFamily: 'monospace',
              fontSize: 'var(--da-font-size-xs)',
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
              color: 'var(--da-color-fg)'
            }}>
              {markdown || t('（無結果）', '(no results)')}
            </div>

            {/* Export Options */}
            <div style={{
              marginTop: `var(--da-space-4)`,
              display: 'flex',
              flexDirection: 'column',
              gap: `var(--da-space-2)`
            }}>
              <button
                onClick={() => {
                  const element = document.createElement('a');
                  element.href = 'data:text/plain;charset=utf-8,' + encodeURIComponent(markdown);
                  element.download = `release-notes-${selectedRoles.join('-')}.md`;
                  document.body.appendChild(element);
                  element.click();
                  document.body.removeChild(element);
                }}
                style={{
                  width: '100%',
                  padding: `var(--da-space-2) var(--da-space-4)`,
                  backgroundColor: 'var(--da-color-fg)',
                  color: 'var(--da-color-bg)',
                  fontSize: 'var(--da-font-size-sm)',
                  fontWeight: 'var(--da-font-weight-medium)',
                  borderRadius: 'var(--da-radius-md)',
                  border: 'none',
                  cursor: 'pointer',
                  transition: `background-color var(--da-transition-base), opacity var(--da-transition-base)`
                }}
                onMouseEnter={(e) => {
                  e.target.style.opacity = '0.85';
                }}
                onMouseLeave={(e) => {
                  e.target.style.opacity = '1';
                }}
              >
                {t('下載 Markdown', 'Download Markdown')}
              </button>
            </div>
          </div>
        </div>

        {/* Usage Notes */}
        <div style={{
          marginTop: `var(--da-space-8)`,
          backgroundColor: 'var(--da-color-info-soft)',
          border: `1px solid var(--da-color-info)`,
          borderRadius: 'var(--da-radius-lg)',
          padding: `var(--da-space-6)`
        }}>
          <h4 style={{
            fontWeight: 'var(--da-font-weight-semibold)',
            color: 'var(--da-color-info)',
            marginBottom: `var(--da-space-2)`
          }}>💡 {t('使用提示', 'Tips')}</h4>
          <ul style={{
            fontSize: 'var(--da-font-size-sm)',
            color: 'var(--da-color-info)',
            display: 'flex',
            flexDirection: 'column',
            gap: `var(--da-space-1)`
          }}>
            <li>• {t('按 role 篩選變更項目，生成針對特定受眾的發行說明', 'Filter changes by role to generate release notes for specific audiences')}</li>
            <li>• {t('支援多角色組合 — 同時選擇多個角色查看綜合發行說明', 'Combine multiple roles to see consolidated release notes')}</li>
            <li>• {t('CHANGELOG 格式：### Category + - [role1, role2] description', 'CHANGELOG format: ### Category + - [role1, role2] description')}</li>
          </ul>
        </div>
      </div>
    </div>
  );
}
