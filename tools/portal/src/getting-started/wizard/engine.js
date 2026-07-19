---
title: "Getting Started Wizard — pure helpers (doc URLs, hash state, path compare)"
purpose: |
  Pure functions extracted from getting-started/wizard.jsx so doc-URL
  resolution, URL-hash (de)serialization, within-role path filtering, and the
  A/B path set-diff are unit-testable in isolation — the .jsx keeps only React
  state + render. Behaviour is preserved verbatim: functions are moved, not
  rewritten. readHash/writeHash intentionally touch window.location/history
  (jsdom-testable); pathsForRole is parametrized on the RECOMMENDATIONS map
  (which stays in the .jsx) so it carries no data-closure dep.

  Pre-this-split these lived inline in wizard.jsx, exercised only indirectly
  via the slower .tsx component test's happy paths. Splitting matches the
  recipe-builder/engine.js + cli-playground/engine.js pattern.

  Public API:
    REPO_BASE                              GitHub blob base for doc links
    docUrl(relativePath)                   full GitHub URL for a doc path
    pathsForRole(roleId, recommendations)  same-role RECOMMENDATIONS entries
    diffDocPaths(currentRec, compareRec)   {shared, onlyA, onlyB} doc lists
    readHash()                             {role, option, readDocs:Set} from hash
    writeHash(role, option, readDocs)      serialize state into the location hash
    recommendationKeyFor(role, option)     "<role>-<option>" key (null-safe)
---

// Base URL for doc links — GitHub renders .md files natively.
const REPO_BASE = "https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main";

// Convert a relative doc path (relative to docs/getting-started/) to a full
// GitHub URL. Three shapes:
//   "for-tenants.md"                → docs/getting-started/for-tenants.md
//   "../architecture-and-design.md" → docs/architecture-and-design.md
//   "../rule-packs/README.md"       → rule-packs/README.md   (not under docs/)
function docUrl(relativePath) {
  let resolved;
  if (relativePath.startsWith("../rule-packs/")) {
    resolved = relativePath.replace("../", "");
  } else if (relativePath.startsWith("../")) {
    resolved = "docs/" + relativePath.replace("../", "");
  } else {
    resolved = "docs/getting-started/" + relativePath;
  }
  return `${REPO_BASE}/${resolved}`;
}

// A/B comparison helper: build the path keys WITHIN a single role. Filters the
// RECOMMENDATIONS map (passed in) by the "<roleId>-" key prefix so a platform
// user only ever compares platform paths, a tenant user only tenant paths, etc.
// (#811 removed the old module-level ALL_PATHS that leaked cross-role entries.)
function pathsForRole(roleId, recommendations) {
  if (!roleId) return [];
  const prefix = roleId + "-";
  return Object.entries(recommendations)
    .filter(([key]) => key.startsWith(prefix))
    .map(([key, rec]) => ({ key, label: rec.title }));
}

// Pure set-diff (by doc.path) of a current vs an optional compare recommendation.
// Returns the three columns the PathCompare panel renders.
function diffDocPaths(currentRec, compareRec) {
  const currentDocs = new Set(currentRec.docs.map(d => d.path));
  const compareDocs = compareRec ? new Set(compareRec.docs.map(d => d.path)) : new Set();
  const shared = currentRec.docs.filter(d => compareDocs.has(d.path));
  const onlyA = currentRec.docs.filter(d => !compareDocs.has(d.path));
  const onlyB = compareRec ? compareRec.docs.filter(d => !currentDocs.has(d.path)) : [];
  return { shared, onlyA, onlyB };
}

// Read initial state from the URL hash (e.g. #role=tenant&option=routing&read=a,b).
// The try/catch is defensive; URLSearchParams itself does not throw on a string.
function readHash() {
  try {
    const params = new URLSearchParams(window.location.hash.slice(1));
    const read = params.get('read');
    return {
      role: params.get('role'),
      option: params.get('option'),
      readDocs: read ? new Set(read.split(',')) : new Set(),
    };
  } catch (e) { return { role: null, option: null, readDocs: new Set() }; }
}

// Serialize wizard state back into the location hash (or clear it when empty).
function writeHash(role, option, readDocs) {
  const parts = [];
  if (role) parts.push('role=' + role);
  if (option) parts.push('option=' + option);
  if (readDocs && readDocs.size > 0) parts.push('read=' + [...readDocs].join(','));
  window.history.replaceState(null, '', parts.length ? '#' + parts.join('&') : window.location.pathname + window.location.search);
}

// RECOMMENDATIONS is keyed "<role>-<option>" for all three roles. Null-safe so
// a partial deep link (role but no option) resolves to no recommendation.
function recommendationKeyFor(role, option) {
  if (!role || !option) return null;
  return `${role}-${option}`;
}

export { REPO_BASE, docUrl, pathsForRole, diffDocPaths, readHash, writeHash, recommendationKeyFor };
