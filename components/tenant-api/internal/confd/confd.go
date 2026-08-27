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
//
// Deliberately NOT case-folded, and that is a measurement rather than an
// opinion (#1537): sweeping every code point in the Unicode range, ZERO of
// them have a strings.ToLower or strings.ToUpper image starting with '_' or
// '.' other than '_' and '.' themselves. So HasPrefix(name, "_") and
// HasPrefix(strings.ToLower(name), "_") agree on every possible input —
// folding here would add a call that cannot change an answer. The extension
// test below is the opposite case: 'Y' does fold to 'y', so it must fold.
func isReservedName(name string) bool {
	return strings.HasPrefix(name, "_") || strings.HasPrefix(name, ".")
}

// TenantIDFromFile maps a conf.d filename to its tenant id (filename minus the
// .yaml/.yml extension). ok is false for reserved control files and non-YAML
// files — i.e. anything a scanner must skip. Directory entries are the
// caller's concern (an fs.DirEntry property, not derivable from the name).
//
// The extension test is CASE-INSENSITIVE (#1537). The exporter — the plane
// that actually serves the tenant — lowercases the name before testing the
// suffix, so it reads `upper.YAML` and merges it. This rejecting it made the
// two planes disagree about which tenants exist, and the worst consequence
// was not the missing row in GET /tenants: federation orphan detection builds
// its live-tenant set from this predicate, so a live `.YAML` tenant was absent
// from that set and its legitimate federation artifacts were reported as
// ORPHANS — i.e. proposed for deletion — while the exporter was serving it.
//
// ⛔ THE ID KEEPS THE STEM'S ORIGINAL CASE: `Upper.YAML` → `Upper`, never
// `upper`. Only the EXTENSION is matched case-insensitively; the stem is
// sliced off the original name, not read back out of the lowercased copy.
// Returning the folded stem would silently RENAME a tenant on every plane
// that keys off this — GET /tenants, federation account backfill, and both
// orphan-detector scans — and it would rename it to an id the exporter never
// uses, since the exporter takes tenant ids from the file's `tenants:` keys
// rather than from its filename. A rename that only one plane performs is a
// second, worse copy of the divergence this change exists to close.
//
// Slicing the original by the extension's own byte length is safe rather
// than merely convenient: measured over the whole Unicode range, no
// non-ASCII rune lowercases into any of '.', 'y', 'a', 'm', 'l', so a
// lowercased tail of ".yaml" can only have come from exactly 5 bytes of the
// original. (`strings.ToLower` CAN shrink a string in bytes — 'İ' is two
// bytes and lowercases to one — which is precisely why the offset is taken
// from the constant and never from len(lower).)
func TenantIDFromFile(name string) (id string, ok bool) {
	if isReservedName(name) {
		return "", false
	}
	lower := strings.ToLower(name)
	switch {
	case strings.HasSuffix(lower, ".yaml"):
		return name[:len(name)-len(".yaml")], true
	case strings.HasSuffix(lower, ".yml"):
		return name[:len(name)-len(".yml")], true
	default:
		return "", false
	}
}

// IsTenantConfigFile reports whether name would be picked up as a tenant
// config by the conf.d scanners (not reserved, YAML suffix).
//
// Two caller shapes, and only one of them saw #1537's change:
//
//   - ENUMERATORS pass a real directory entry name, so `upper.YAML` went from
//     skipped to scanned. That is the fix.
//   - VALIDATORS (handler.ValidateTenantID, gitops.Writer) pass a synthesised
//     `id + ".yaml"`, whose suffix is already lowercase — so the folded test
//     and the exact test decide identically and the write-accepted id
//     namespace did NOT widen. That is not an argument, it is measured, and
//     it is pinned by TestValidatorCallShapeDidNotWiden in this package so it
//     stays true if the extension rule is touched again.
func IsTenantConfigFile(name string) bool {
	_, ok := TenantIDFromFile(name)
	return ok
}
