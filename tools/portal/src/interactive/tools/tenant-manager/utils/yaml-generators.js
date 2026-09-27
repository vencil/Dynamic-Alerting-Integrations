---
title: "Tenant Manager — YAML Generators"
purpose: |
  Pure functions that emit copy-pasteable YAML for tenant operational-mode
  toggles: per-tenant `_state_maintenance` / `_silent_mode` blocks. Used by
  the bulk-action modal: the user selects N tenants → clicks "Maintenance"
  or "Silent Mode" → these helpers stringify the selection into fragments
  to paste under tenants.<id>: in each tenant's existing file.

  Extracted from tenant-manager.jsx in PR-2d (#153). No React, no side
  effects, no closures — pure string assembly.
---

// Both operational modes are tenant-config keys, not ConfigMap entries
// (#1988 silent, #2033 maintenance: the former `tenant-operational-modes`
// ConfigMap was read by nothing, and a file of that shape parses to zero
// tenants without an error). Each block is headed by a `# <id>` comment and
// is pasted under tenants.<id>: in that tenant's existing file. The blocks
// are fragments, not a file, so the modal offers copy only. Shapes follow
// docs/schemas/tenant-config.schema.json#/definitions/{maintenanceMode,
// silentMode}. `now` is injectable so expires is testable.
const OPERATIONAL_MODE_TTL_MS = 24 * 60 * 60 * 1000;

// RFC3339 (no milliseconds) timestamp OPERATIONAL_MODE_TTL_MS after `now`.
// Shared with the playground templates so no example expires at a fixed date.
function silentModeExpires(now = new Date()) {
  return new Date(now.getTime() + OPERATIONAL_MODE_TTL_MS)
    .toISOString().replace(/\.\d{3}Z$/, 'Z');
}

function perTenantBlocks(tenants, body) {
  const lines = [];
  tenants.forEach(name => {
    if (lines.length) lines.push('');
    lines.push(`# ${name}`);
    lines.push(...body);
  });
  return lines.join('\n');
}

function generateMaintenanceYaml(tenants, now = new Date()) {
  return perTenantBlocks(tenants, [
    '    _state_maintenance:',
    `      expires: "${silentModeExpires(now)}"`,
    '      reason: "Scheduled maintenance"',
  ]);
}

function generateSilentModeYaml(tenants, now = new Date()) {
  return perTenantBlocks(tenants, [
    '    _silent_mode:',
    '      target: "all"',
    `      expires: "${silentModeExpires(now)}"`,
    '      reason: "Under investigation"',
  ]);
}

export { generateMaintenanceYaml, generateSilentModeYaml, silentModeExpires };
