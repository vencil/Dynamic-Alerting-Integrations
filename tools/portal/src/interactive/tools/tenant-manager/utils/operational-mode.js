---
title: "Tenant Manager — operational mode from the tenant-api summary"
purpose: |
  Maps one /api/v1/tenants/search item to the UI's operational_mode
  column (#1988 D1 PR-3). The state comes from `config_derived`
  (`silent_targets` / `maintenance_active`), which tenant-api derives
  with threshold-exporter's own load (「依設定推算」). The summary's
  `silent_mode` / `maintenance` are the RAW file values — `disable`
  is a non-empty string — so they are never read here.

  No `config_derived` (tenant-api could not load conf.d) → 'unknown',
  not a guess from the raw values.

  Pure functions, no React.
---

function operationalModeFromSummary(summary) {
  const derived = summary && summary.config_derived;
  if (!derived) return 'unknown';
  if (derived.maintenance_active) return 'maintenance';
  if (Array.isArray(derived.silent_targets) && derived.silent_targets.length > 0) return 'silent';
  return 'normal';
}

// derivationNoticeFromSearch returns what the search response's
// `config_derivation` says the derivation could not read, or null when
// there is nothing to tell the operator.
function derivationNoticeFromSearch(body) {
  const d = body && body.config_derivation;
  if (!d) return null;
  const files = Array.isArray(d.parse_failed_files) ? d.parse_failed_files : [];
  const hidden = typeof d.parse_failed_hidden === 'number' ? d.parse_failed_hidden : 0;
  const loadError = d.load_error || '';
  if (files.length === 0 && hidden === 0 && !loadError) return null;
  return { parseFailedFiles: files, parseFailedHidden: hidden, loadError };
}

export { operationalModeFromSummary, derivationNoticeFromSearch };
