package confd

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
)

// This file is the second half of the package's promise. confd.go answers
// "does this NAME count as a tenant config file"; this file answers "WHICH
// FILE in this directory is tenant X's". Splitting the two across packages
// was considered and rejected: the divergence this file exists to close is
// exactly that the enumerating plane (TenantIDFromFile, which accepts
// .yaml/.yml in any case) and the per-tenant addressing plane (a hardcoded
// `filepath.Join(dir, id+".yaml")`, repeated ~10 times) answered that
// question differently, so a tenant stored as `<id>.yml` was listed by
// GET /tenants but 404'd on GET /tenants/{id}, and a PUT to it created a
// SECOND file rather than updating the first.
//
// Consequence: unlike confd.go, this file does filesystem I/O. That is
// deliberate — "which file is this tenant's" is not answerable from a name
// alone, and answering it anywhere else would recreate the two-conventions
// split in a smaller form.

var (
	// ErrTenantFileNotFound reports that configDir holds no config file for
	// the tenant. Callers map this to 404, matching the pre-resolver
	// os.ReadFile+os.IsNotExist behavior (a missing configDir resolves here
	// too, exactly as a missing directory made the old ReadFile return
	// ENOENT).
	ErrTenantFileNotFound = errors.New("confd: no config file for tenant")

	// ErrAmbiguousTenantFile reports that MORE THAN ONE file in configDir
	// claims the same tenant id (e.g. both `<id>.yaml` and `<id>.yml`).
	//
	// This is refused rather than resolved by precedence on purpose. Picking
	// a winner would make an authorization- and write-relevant decision on a
	// coin flip: the two files can carry different `_metadata` (so different
	// environment/domain scope answers) and different threshold values, and
	// whichever one loses is silently dropped on the next whole-file PUT.
	// threshold-exporter already refuses the same shape with a typed
	// *DuplicateTenantError rather than choosing; refusing here keeps the two
	// planes agreeing instead of adding a third stance.
	ErrAmbiguousTenantFile = errors.New("confd: more than one config file for tenant")
)

// DefaultTenantFileName is the name a NEW tenant's config file gets. It is the
// only place the `.yaml` spelling is chosen; every other site resolves an
// existing file instead of assuming one.
func DefaultTenantFileName(tenantID string) string { return tenantID + ".yaml" }

// matchTenantFiles returns the sorted base names in configDir that classify as
// tenantID's config file. A missing configDir yields no matches rather than an
// error — see ErrTenantFileNotFound.
func matchTenantFiles(configDir, tenantID string) ([]string, error) {
	entries, err := os.ReadDir(configDir)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, nil
		}
		return nil, err
	}
	var names []string
	for _, e := range entries {
		if e.IsDir() {
			continue
		}
		id, ok := TenantIDFromFile(e.Name())
		if !ok || id != tenantID {
			continue
		}
		names = append(names, e.Name())
	}
	sort.Strings(names)
	return names, nil
}

// ResolveTenantFile returns the path of the single file in configDir holding
// tenantID's config.
//
// The id comparison is EXACT, not case-folded: TenantIDFromFile keeps the
// stem's original case (`Upper.YAML` → `Upper`), and the write-accepted id
// namespace has always been case-sensitive, so folding here would let
// GET /tenants/{upper} reach a tenant that no writer could ever address.
func ResolveTenantFile(configDir, tenantID string) (string, error) {
	names, err := matchTenantFiles(configDir, tenantID)
	if err != nil {
		return "", err
	}
	switch len(names) {
	case 0:
		return "", fmt.Errorf("%w: %q", ErrTenantFileNotFound, tenantID)
	case 1:
		return filepath.Join(configDir, names[0]), nil
	default:
		return "", fmt.Errorf("%w: %q is claimed by %v", ErrAmbiguousTenantFile, tenantID, names)
	}
}

// TenantFilePathForWrite returns the path a write for tenantID must land on:
// the tenant's EXISTING file when it has one, otherwise DefaultTenantFileName.
//
// This is a different question from ResolveTenantFile and must stay one: a
// writer that always joined `<id>.yaml` would, for a tenant stored as
// `<id>.yml`, read "no existing config", treat the write as a new tenant, and
// leave the original file behind as a second source of truth. An ambiguous
// tenant is refused here too — writing to either file makes the other stale.
func TenantFilePathForWrite(configDir, tenantID string) (string, error) {
	path, err := ResolveTenantFile(configDir, tenantID)
	switch {
	case err == nil:
		return path, nil
	case errors.Is(err, ErrTenantFileNotFound):
		return filepath.Join(configDir, DefaultTenantFileName(tenantID)), nil
	default:
		return "", err
	}
}
