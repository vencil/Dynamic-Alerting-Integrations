package gitops

import (
	"errors"
	"io/fs"
	"log/slog"
)

// DryRunValidate answers, without writing, the question a DIRECT-mode Write
// asks before it writes: would this body for this tenant be refused? (#2124)
//
// ⛔ IT CALLS THE WRITE PATH'S OWN CHECKS, IT DOES NOT RE-ASSEMBLE THEM. POST
// /tenants/{id}/validate used to hand-copy a subset of validate()'s checks and
// so answered `valid: true` for every body whose refusal it had not copied
// (invalid YAML among them).
//
// It runs the sequence write() judges the tree it lands on with, in the same
// order: guardTenantID → w.tenantFilePath (ambiguous tenant file, #2078
// declared-elsewhere) → validate(configDir, …). If that sequence changes, this
// must change with it.
//
// ⚠️ DIRECT MODE ONLY. A PR-mode write judges the tree-derived checks against
// the fresh base it checks out, not the local tree — use DryRunValidateBodyOnly
// there (#1718).
//
// ⛔ A *Writer METHOD, NOT A FREE FUNCTION OF configDir: tenantFilePath's walk
// is bounded by scanTree, whose stuck-walk breaker lives on the Writer. Sharing
// the production Writer lets a walk blocked on a FIFO fail every later dry-run
// AND write fast, instead of each dry-run leaking one more blocked goroutine.
//
// No side effects: it takes neither the admission token nor w.mu and only reads
// the tree, so its verdict is about the tree as it is now; a write queued
// behind others is judged again on the tree it lands on.
//
// err is a refusal that would come BEFORE validation, typed exactly as Write
// returns it (reserved id, ErrAmbiguousTenantFile, ErrTenantDeclaredElsewhere,
// ErrTenantTreeScan, or a resolver read error). errs / notices are validate()'s
// blocking and advisory sets; like validate, a structural failure is reported
// alone.
func (w *Writer) DryRunValidate(tenantID, yamlContent string) (errs, notices []string, err error) {
	if err := guardTenantID(tenantID); err != nil {
		return nil, nil, err
	}
	filePath, err := w.tenantFilePath(tenantID)
	if err != nil {
		return nil, nil, err
	}
	errs, notices = validate(w.configDir, tenantID, filePath, yamlContent)
	return errs, notices, nil
}

// DryRunValidateBodyOnly is the PR-mode dry-run: exactly what WritePR's
// pre-flight runs — guardTenantID, then validateBodyOnly — and nothing that
// reads the tree.
//
// ⛔ DELIBERATELY WEAKER THAN DryRunValidate. The local tree in PR mode is only
// synced at pod start and may lag the base (#1718); refusing on it would answer
// "invalid" for a write the PR then accepts, until the pod restarts. The
// tree-derived checks (tenant file resolution, added sections, key validation
// against the base's defaults, the eol guard) run when the PR is cut, against
// the fresh base — so this cannot predict them, and returns no notices
// (validateBodyOnly produces none; the PR write collects them on the base).
func DryRunValidateBodyOnly(tenantID, yamlContent string) (errs []string, err error) {
	if err := guardTenantID(tenantID); err != nil {
		return nil, err
	}
	return validateBodyOnly(tenantID, yamlContent), nil
}

// pathlessErrText is err's text without the file path a *fs.PathError carries:
// validate() strings reach the client (PUT's 400, and the dry-run route, which
// needs only read permission), and the tenant file's absolute path is server
// layout. The full error goes to the server log.
func pathlessErrText(err error) string {
	var pe *fs.PathError
	if errors.As(err, &pe) {
		slog.Warn("tenant validation: file read failed", "error", err)
		return pe.Err.Error()
	}
	return err.Error()
}
