package config

// tenantFileTopLevelKeys is every top-level key a tenant file (a config file
// whose name does not start with `_`) may hold: the properties of
// docs/schemas/tenant-config.schema.json, which sets additionalProperties
// false, and the only key the exporter takes a tenant's keys from in such a
// file. TestTenantFileTopLevelKeys_MatchTheSchema holds the two equal.
//
// ⛔ THE ONE COPY (#3671). tenant-api's TenantFileManagement (a file with
// another top-level key is git-managed, #3504) and the shared key verdict (such
// a key is unread, pkg/keyverdict) both ask TenantFileTopLevelKey; neither
// restates the set.
var tenantFileTopLevelKeys = map[string]bool{"tenants": true}

// TenantFileTopLevelKey reports whether name is a top-level key a tenant file
// may hold (tenantFileTopLevelKeys).
func TenantFileTopLevelKey(name string) bool { return tenantFileTopLevelKeys[name] }
