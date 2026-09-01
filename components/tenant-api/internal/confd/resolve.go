package confd

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
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

	// ErrUnsafeTenantID reports a tenant id that is not a bare filename —
	// it carries a path separator, a ".." segment, or otherwise does not
	// survive filepath.Base unchanged.
	//
	// Why the check lives HERE, at the point the path is built, rather than
	// only in handler.ValidateTenantID: guardTenantID's own contract is to
	// hold "even if a future caller forgets to validate first", but it gates
	// on the reserved-NAME predicate alone, which `foo/../../etc/passwd`
	// satisfies (it neither starts with "_"/"." nor lacks a .yaml suffix) —
	// measured, not assumed. Every path this package hands back is joined
	// onto configDir, so refusing the shape at the join is the only place
	// that covers every caller. handler.ValidateTenantID stays the
	// first-line, user-facing rejection with its own messages; this is the
	// sink-side backstop, deliberately the same predicate.
	ErrUnsafeTenantID = errors.New("confd: tenant id is not a bare filename")
)

// guardBareTenantID rejects an id that must never be joined onto configDir.
func guardBareTenantID(tenantID string) error {
	switch {
	case tenantID == "":
		return fmt.Errorf("%w: empty", ErrUnsafeTenantID)
	case strings.ContainsAny(tenantID, `/\`):
		return fmt.Errorf("%w: %q contains a path separator", ErrUnsafeTenantID, tenantID)
	case strings.Contains(tenantID, ".."):
		return fmt.Errorf("%w: %q contains %q", ErrUnsafeTenantID, tenantID, "..")
	case filepath.Base(tenantID) != tenantID:
		return fmt.Errorf("%w: %q is not a simple filename", ErrUnsafeTenantID, tenantID)
	}
	return nil
}

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
	if err := guardBareTenantID(tenantID); err != nil {
		return "", err
	}
	names, err := matchTenantFiles(configDir, tenantID)
	if err != nil {
		return "", err
	}
	switch len(names) {
	case 0:
		return "", fmt.Errorf("%w: %q", ErrTenantFileNotFound, tenantID)
	case 1:
		// filepath.Base is a no-op on a directory entry name (os.ReadDir never
		// yields a name containing a separator), but applying the
		// transformation rather than relying on that invariant keeps EVERY
		// join in this file provably escape-free at the join itself.
		return filepath.Join(configDir, filepath.Base(names[0])), nil
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
	// ResolveTenantFile guards the id; the ErrTenantFileNotFound arm below is
	// reached only for an id already proven to be a bare filename, so the
	// DefaultTenantFileName join cannot escape configDir.
	path, err := ResolveTenantFile(configDir, tenantID)
	switch {
	case err == nil:
		return path, nil
	case errors.Is(err, ErrTenantFileNotFound):
		// This join is the ONLY place a caller-supplied id becomes a path.
		// guardBareTenantID has already proved the id is a bare filename, but
		// that proof lives in ANOTHER function, and a taint-tracking analyser
		// reasons about a check where it can see it. CodeQL's Go
		// path-injection query models `!strings.Contains(x, "..")` and
		// `filepath.IsLocal(x)` as barriers, and does NOT model filepath.Base
		// as a sanitizer at all (its TaintedPathCustomizations.qll lists only
		// filepath.Clean("/"+e), mux.Vars, numeric/boolean nodes and a
		// ".."-ReplaceAll) — which is why the earlier attempt to satisfy it by
		// applying Base right here could not have worked, and did not. So
		// restate the predicate INLINE, on the same value that reaches the
		// join.
		//
		// Unreachable by construction, and deliberately so: guardBareTenantID
		// rejects both shapes first, and is strictly the stronger predicate
		// (IsLocal admits `a/b`, which the guard does not). This narrows
		// nothing and replaces nothing — it is a barrier at the sink.
		if strings.Contains(tenantID, "..") || !filepath.IsLocal(tenantID) {
			return "", fmt.Errorf("%w: %q", ErrUnsafeTenantID, tenantID)
		}
		return filepath.Join(configDir, DefaultTenantFileName(tenantID)), nil
	default:
		return "", err
	}
}
