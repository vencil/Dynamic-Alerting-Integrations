---
title: "Release Notes Generator — changelog pipeline"
purpose: |
  Pure changelog pipeline behind the release-notes generator:
  parseChangelogMarkdown turns CHANGELOG markdown into { category: items[] }
  (each item tagged with audience roles), filterChangesByRole keeps items
  relevant to the selected roles, and generateAutoSummary produces a short
  role-aware summary sentence.

  Pre-PR-portal-21 these were inline in release-notes-generator.jsx (659 LOC)
  with 0% coverage. While moving generateAutoSummary, a provably-dead
  `roleLabels` local (computed, never read) was dropped — this also removed
  the only dependency on the ROLES UI-metadata global, so the engine is now
  i18n-only.

  Public API:
    window.__parseChangelogMarkdown(text)              -> { category: [{roles, description}] }
    window.__filterChangesByRole(sections, roles)      -> filtered sections
    window.__generateAutoSummary(filtered, roles)      -> summary string

  Closure deps: window.__t for bilingual summary sentences (falls back to English).
---

// i18n fallback (moved with the cluster from release-notes-generator.jsx).
const t = window.__t || ((zh, en) => en);

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

// Legacy jsx-loader path: expose as window globals (see PR-portal-12 / TD-030z).
window.__parseChangelogMarkdown = parseChangelogMarkdown;
window.__filterChangesByRole = filterChangesByRole;
window.__generateAutoSummary = generateAutoSummary;

// TRK-230e: ESM exports (esbuild dist path). Removed with jsx-loader in TRK-230z.
// <!-- jsx-loader-compat: ignore -->
export { parseChangelogMarkdown, filterChangesByRole, generateAutoSummary };
