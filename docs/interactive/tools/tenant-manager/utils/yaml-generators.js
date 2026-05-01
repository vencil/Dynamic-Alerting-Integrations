---
title: "Tenant Manager — YAML Generators"
purpose: |
  Pure functions that emit ConfigMap YAML for tenant operational-mode
  toggles (maintenance / silent). Used by the bulk-action modal:
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

function generateSilentModeYaml(tenants) {
  const lines = [];
  lines.push('apiVersion: v1');
  lines.push('kind: ConfigMap');
  lines.push('metadata:');
  lines.push('  name: tenant-operational-modes');
  lines.push('  namespace: monitoring');
  lines.push('data:');
  tenants.forEach(name => {
    lines.push(`  ${name}_silent: |`);
    lines.push(`    mode: silent`);
    lines.push(`    reason: "Under investigation"`);
    lines.push(`    expires: "2026-04-04T12:00:00Z"`);
  });
  return lines.join('\n');
}

// Register on window for orchestrator pickup.
window.__generateMaintenanceYaml = generateMaintenanceYaml;
window.__generateSilentModeYaml = generateSilentModeYaml;
