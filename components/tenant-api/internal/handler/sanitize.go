package handler

import (
	"fmt"
	"path/filepath"
	"strings"

	"github.com/vencil/tenant-api/internal/confd"
)

// ValidateTenantID checks that a tenant ID is safe for use as a filename.
// Rejects path traversal sequences, slashes, and non-base names.
func ValidateTenantID(id string) error {
	if id == "" {
		return fmt.Errorf("tenant ID must not be empty")
	}
	if strings.ContainsAny(id, "/\\") {
		return fmt.Errorf("tenant ID must not contain path separators")
	}
	if strings.Contains(id, "..") {
		return fmt.Errorf("tenant ID must not contain '..'")
	}
	// After cleaning, must equal the original (catches hidden traversal)
	if filepath.Base(id) != id {
		return fmt.Errorf("tenant ID must be a simple filename")
	}
	// The id must name a file the conf.d scanners would pick up as a tenant.
	// Gating on the SAME predicate the scanners skip on (confd) keeps the
	// write-accepted namespace structurally equal to the scanned one: a
	// reserved control file (_defaults.yaml, _rbac.yaml, _domain_policy.yaml,
	// ...) can never be addressed as a tenant — a writable "_" id would let a
	// caller overwrite platform config (e.g. blank out the domain policy gate)
	// — and any future scanner skip rule propagates here for free.
	if !confd.IsTenantConfigFile(id + ".yaml") {
		return fmt.Errorf("tenant ID must not name a reserved control file")
	}
	return nil
}