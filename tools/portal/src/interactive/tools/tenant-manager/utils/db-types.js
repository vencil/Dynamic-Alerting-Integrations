---
title: "Tenant Manager — db_type as a list"
purpose: |
  `_metadata.db_type` is a string or a list of strings (#3771); the
  tenant-api search item carries it as a list, platform-data.json and
  the demo fixtures may still carry a string. dbTypesOf reads any of
  them as a list of the declared kinds (empty when none), so the card,
  the filter and the filter options treat the shapes alike.

  Pure functions, no React.
---

function dbTypesOf(value) {
  if (Array.isArray(value)) return value.filter((v) => typeof v === 'string' && v !== '');
  if (typeof value === 'string' && value !== '') return [value];
  return [];
}

export { dbTypesOf };
