---
title: "Tenant Manager — YAML Generators"
purpose: |
  Pure functions that emit copy-pasteable YAML for tenant operational-mode
  toggles (maintenance: a ConfigMap; silent: per-tenant _silent_mode
  blocks, see generateSilentModeYaml). Used by the bulk-action modal:
  the user selects N tenants → clicks "Maintenance" or "Silent Mode"
  → these helpers stringify the selection into a copy-pasteable YAML.

  Extracted from tenant-manager.jsx in PR-2d (#153). Behavior identical;
  no React, no side effects, no closures — pure string assembly.
---

function generateMaintenanceYaml(tenants) {
  const lines = [];
  lines.push('apiVersion: v1');
  lines.push('kind: ConfigMap');
  lines.push('metadata:');
  lines.push('  name: tenant-operational-modes');
  lines.push('  namespace: monitoring');
  lines.push('data:');
  tenants.forEach(name => {
    lines.push(`  ${name}_maintenance: |`);
    lines.push(`    mode: maintenance`);
    lines.push(`    reason: "Scheduled maintenance"`);
    lines.push(`    expires: "2026-04-05T00:00:00Z"`);
  });
  return lines.join('\n');
}

// Silent mode is a tenant-config key (`_silent_mode`), not a ConfigMap entry
// (#1988). Emits one block per tenant, headed by a `# <id>` comment, to be
// pasted under tenants.<id>: in that tenant's existing file. The blocks are
// fragments, not a file, so the modal offers copy only. The shape follows
// docs/schemas/tenant-config.schema.json#/definitions/silentMode. `now` is
// injectable so expires is testable.
const SILENT_MODE_TTL_MS = 24 * 60 * 60 * 1000;

// RFC3339 (no milliseconds) timestamp SILENT_MODE_TTL_MS after `now`. Shared
// with the playground templates so no example silences for a fixed far date.
function silentModeExpires(now = new Date()) {
  return new Date(now.getTime() + SILENT_MODE_TTL_MS)
    .toISOString().replace(/\.\d{3}Z$/, 'Z');
}

function generateSilentModeYaml(tenants, now = new Date()) {
  const expires = silentModeExpires(now);
  const lines = [];
  tenants.forEach(name => {
    if (lines.length) lines.push('');
    lines.push(`# ${name}`);
    lines.push('    _silent_mode:');
    lines.push('      target: "all"');
    lines.push(`      expires: "${expires}"`);
    lines.push('      reason: "Under investigation"');
  });
  return lines.join('\n');
}

export { generateMaintenanceYaml, generateSilentModeYaml, silentModeExpires };
