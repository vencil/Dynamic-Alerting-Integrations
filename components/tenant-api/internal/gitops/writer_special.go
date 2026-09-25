package gitops

// Special-file write paths for non-tenant GitOps entities — _groups.yaml,
// _views.yaml, _federation_policy.yaml, and per-tenant _federation/<id>.yaml.
// Split out of writer.go (Cycle 5 refactor) so the tenant write path and these
// entity write paths read separately — no behavior change, pure intra-package
// move. All share the same writer mutex + HEAD conflict detection as tenant
// writes (via writeSpecialFile / commitFileChange in writer.go); only the
// validation step differs (basic YAML well-formedness, not full schema).

import (
	"context"
	"fmt"
	"os"
	"path/filepath"

	"github.com/vencil/tenant-api/internal/confd"
	"gopkg.in/yaml.v3"
)

// WriteGroupsFile validates, persists, and commits the _groups.yaml file.
// Reuses the same sync.Mutex and HEAD conflict detection as tenant writes.
func (w *Writer) WriteGroupsFile(ctx context.Context, authorEmail, yamlContent string) error {
	return w.writeSpecialFile(ctx, "_groups.yaml", "groups", authorEmail, yamlContent)
}

// WriteViewsFile validates, persists, and commits the _views.yaml file.
// v2.5.0 Phase C: Saved Views support.
func (w *Writer) WriteViewsFile(ctx context.Context, authorEmail, yamlContent string) error {
	return w.writeSpecialFile(ctx, "_views.yaml", "views", authorEmail, yamlContent)
}

// WriteFederationPolicyFile validates, persists, and commits the
// platform federation whitelist (_federation_policy.yaml). ADR-020 IV-2e.
//
// An optional trailer is appended to the commit message body — used to
// record an admission-validator `--force` bypass (operator + reason)
// directly in git history, the only durable audit trail in a GitOps
// system (ADR-020 IV-2e; stdout logs rotate away).
func (w *Writer) WriteFederationPolicyFile(ctx context.Context, authorEmail, yamlContent string, trailer ...string) error {
	return w.writeSpecialFile(ctx, "_federation_policy.yaml", "federation-policy", authorEmail, yamlContent, trailer...)
}

// WriteFederationSubsetFile validates, persists, and commits one
// tenant's federation metric subset under _federation/ (ADR-020 IV-2e).
// One file per tenant on purpose: a tenant's self-service subset edits
// never contend on a shared git object, so concurrent edits across
// tenants cannot conflict. The _federation/ directory is created on
// first write.
//
// The file written is the tenant's EXISTING subset file whatever its
// spelling (`<id>.yml`, `<id>.YAML`, …), and `<id>.yaml` only for a
// tenant that has none (#1698) — confd.TenantFilePathForWrite, the
// tenant plane's #1673 answer, pointed at _federation/. Joining a fixed
// `<id>.yaml` here created a SECOND file beside a `<id>.yml` subset and
// left the original with stale content and no reader. Two files already
// claiming the tenant are refused with confd.ErrAmbiguousTenantFile and
// nothing is written.
func (w *Writer) WriteFederationSubsetFile(ctx context.Context, tenantID, authorEmail, yamlContent string) error {
	// Reserved-id backstop: a brand-new subset is written as {tenantID}.yaml,
	// so a reserved id would land a _*/.* file inside _federation/
	// (defense-in-depth; see guardTenantID).
	if err := guardTenantID(tenantID); err != nil {
		return err
	}
	// Basic YAML validity check (the schema check is the caller's job).
	var raw map[string]interface{}
	if err := yaml.Unmarshal([]byte(yamlContent), &raw); err != nil {
		return fmt.Errorf("invalid YAML: %w", err)
	}

	// MkdirAll is idempotent and git-independent — done before taking
	// the write lock so a filesystem syscall never serialises behind
	// the (git-bound) write path.
	dir := confd.FederationSubsetDir(w.configDir)
	if err := os.MkdirAll(dir, 0755); err != nil {
		return fmt.Errorf("create _federation dir: %w", err)
	}

	// Load-shedding admission (TRK-320) before w.mu.
	if err := w.acquireWrite(ctx); err != nil {
		return err
	}
	defer w.releaseWrite()

	w.mu.Lock()
	defer w.mu.Unlock()

	// Resolved under w.mu so no other write through this Writer can create
	// or rename the tenant's subset file between the answer and the write.
	path, err := confd.TenantFilePathForWrite(dir, tenantID)
	if err != nil {
		return err
	}
	return w.commitFileChange(
		path,
		"federation/"+tenantID,
		authorEmail,
		[]byte(yamlContent),
	)
}

// writeSpecialFile is a shared implementation for writing _groups.yaml, _views.yaml, etc.
// These files use the same mutex and conflict detection as tenant writes — only
// the validation step differs (basic YAML well-formedness, not full schema).
func (w *Writer) writeSpecialFile(ctx context.Context, filename, entityType, authorEmail, yamlContent string, trailer ...string) error {
	// Basic YAML validity check (special files don't have a schema).
	var raw map[string]interface{}
	if err := yaml.Unmarshal([]byte(yamlContent), &raw); err != nil {
		return fmt.Errorf("invalid YAML: %w", err)
	}

	// Load-shedding admission (TRK-320) before w.mu.
	if err := w.acquireWrite(ctx); err != nil {
		return err
	}
	defer w.releaseWrite()

	w.mu.Lock()
	defer w.mu.Unlock()

	return w.commitFileChange(
		filepath.Join(w.configDir, filename),
		entityType,
		authorEmail,
		[]byte(yamlContent),
		trailer...,
	)
}
