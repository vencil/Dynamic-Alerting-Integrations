package config

import "fmt"

// DuplicateTenantError signals that the same tenant ID was discovered in two
// different files during a directory scan. This is a misconfig (e.g. forgot
// to delete the old flat copy after `git mv` to the nested layout) that the
// platform should reject hard rather than silently last-wins-merge.
//
// Returned by all three pkg/config directory walkers — ResolveEffective
// (hierarchy.go), ScopeEffective (scope.go), and ScanFromConfigSource
// (source.go) — and by the exporter's scanDirHierarchical
// (app/config_hierarchy.go), which returns the same type through the
// `type DuplicateTenantError = config.DuplicateTenantError` alias in
// config_types.go. All consumers detect it via
// `errors.As(err, &DuplicateTenantError{})` (issue #127, v2.8.x hardening).
//
// Before v2.8.x: the exporter's scanDirHierarchical returned a generic
// fmt.Errorf, and Load() swallowed it with a WARN log. Customers could deploy
// with a duplicate tenant silently merged via map last-wins iteration — easy
// to miss in production.
//
// After v2.8.x: the typed error lets Load() / fullDirLoad() reject the
// misconfig at the boundary; other scan errors (permissions, malformed file)
// keep the log-and-continue policy because hierarchical mode is opt-in and
// shouldn't tear down a flat-only deploy.
//
// Lowered into pkg/config (candidate C6-A, #127 library-side gap): the walkers
// used by library consumers (tenant-api, cmd/da-guard, simulate) previously
// returned a stringly fmt.Errorf, so those consumers could only string-match
// the message. Owning the type here lets them unpack the same typed error the
// exporter already does.
type DuplicateTenantError struct {
	TenantID string
	PathA    string // First-discovered file
	PathB    string // Second-discovered file (the one rejected)
}

func (e *DuplicateTenantError) Error() string {
	return fmt.Sprintf("duplicate tenant ID %q: defined in both %s and %s", e.TenantID, e.PathA, e.PathB)
}
