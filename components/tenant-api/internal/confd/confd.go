// Package confd holds the single definition of "what counts as a tenant
// config file" in a conf.d directory. Every scanner that enumerates tenants
// (tenant list, federation account backfill, orphan detection) skips on the
// same predicate, and ValidateTenantID rejects any id whose {id}.yaml would
// not satisfy it. Keeping the write-accepted id namespace structurally equal
// to the scanned-file namespace is what stops a caller from addressing a
// reserved control file (_defaults.yaml, _rbac.yaml, _domain_policy.yaml, …)
// as a tenant — the class of bug that a prefix-by-prefix denylist re-opens
// every time a new reserved convention is added to the scanner alone.
package confd

import "strings"

// isReservedName reports whether a conf.d entry name is a reserved control
// file rather than a tenant config. Reserved = "_" prefix (platform control
// files, e.g. _defaults.yaml) or "." prefix (hidden / VCS files).
func isReservedName(name string) bool {
	return strings.HasPrefix(name, "_") || strings.HasPrefix(name, ".")
}

// TenantIDFromFile maps a conf.d filename to its tenant id (filename minus the
// .yaml/.yml extension). ok is false for reserved control files and non-YAML
// files — i.e. anything a scanner must skip. Directory entries are the
// caller's concern (an fs.DirEntry property, not derivable from the name).
func TenantIDFromFile(name string) (id string, ok bool) {
	if isReservedName(name) {
		return "", false
	}
	switch {
	case strings.HasSuffix(name, ".yaml"):
		return strings.TrimSuffix(name, ".yaml"), true
	case strings.HasSuffix(name, ".yml"):
		return strings.TrimSuffix(name, ".yml"), true
	default:
		return "", false
	}
}

// IsTenantConfigFile reports whether name would be picked up as a tenant
// config by the conf.d scanners (not reserved, YAML suffix).
func IsTenantConfigFile(name string) bool {
	_, ok := TenantIDFromFile(name)
	return ok
}
