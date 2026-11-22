---
title: "Tenant Manager — expected exporters as a list"
purpose: |
  A tenant's `_expected_exporters` is a list of exporter kinds (#4097);
  the tenant-api search item and platform-data.json carry it as a list
  (`expected_exporters`). expectedExportersOf reads it as a list of the
  declared kinds (empty when none; a lone string is one kind), so the
  card, the filter and the filter options treat every source alike.

  Pure functions, no React.
---

function expectedExportersOf(value) {
  if (Array.isArray(value)) return value.filter((v) => typeof v === 'string' && v !== '');
  if (typeof value === 'string' && value !== '') return [value];
  return [];
}

export { expectedExportersOf };
