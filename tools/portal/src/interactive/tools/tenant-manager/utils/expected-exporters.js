---
title: "Tenant Manager — expected exporters as a list"
purpose: |
  A tenant's `_expected_exporters` is a list of entries (#4097), each an
  exporter kind (`mariadb`) or `kind@cluster` (`mariadb@east`); the
  tenant-api search item and platform-data.json carry it as a list
  (`expected_exporters`), entries as written. expectedExportersOf reads it
  as a list of the declared entries (empty when none; a lone string is one
  entry), so the card, the filter and the filter options treat every
  source alike.

  An entry is SHOWN as written; anything that filters or groups BY KIND
  uses the part before `@` (expectedExporterKind):
  expectedExporterKindsOf is the kinds a tenant declares, each once — the
  filter's options — and declaresExporter is the filter itself, with the
  tenant-api search filter's rule (handler/tenant_search.go
  declaresExporter): a kind matches by kind, so `mariadb` finds a tenant
  declaring `mariadb@east`; a value with `@` (a saved view may carry one)
  is a whole entry and matches only that entry.

  exporterFilterOptions is what the filter's dropdown lists: the kinds,
  plus the value in effect when it is not one of them (a saved view's
  `mariadb@east`, or a kind no tenant declares any more) — a select whose
  value is not among its options shows another option as if it were the
  one applied.

  Pure functions, no React.
---

function expectedExportersOf(value) {
  if (Array.isArray(value)) return value.filter((v) => typeof v === 'string' && v !== '');
  if (typeof value === 'string' && value !== '') return [value];
  return [];
}

function expectedExporterKind(entry) {
  const at = entry.indexOf('@');
  return at < 0 ? entry : entry.slice(0, at);
}

function expectedExporterKindsOf(value) {
  return [...new Set(expectedExportersOf(value).map(expectedExporterKind))];
}

function declaresExporter(value, filter) {
  const whole = filter.includes('@');
  return expectedExportersOf(value).some(
    (entry) => entry === filter || (!whole && expectedExporterKind(entry) === filter),
  );
}

function exporterFilterOptions(kinds, current) {
  const options = [...new Set(kinds)];
  if (current && !options.includes(current)) options.push(current);
  return options;
}

export {
  expectedExportersOf, expectedExporterKind, expectedExporterKindsOf, declaresExporter,
  exporterFilterOptions,
};
