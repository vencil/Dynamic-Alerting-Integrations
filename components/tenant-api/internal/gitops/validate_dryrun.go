package gitops

import (
	"errors"
	"io/fs"
	"log/slog"
)

// DryRunValidateBodyOnly is the PR-mode dry-run: exactly what WritePR's
// pre-flight runs — guardTenantID, then validateBodyOnly — plus the
// placeholder check the PUT handler runs before WritePR (#1560), and nothing
// that reads the tree.
//
// ⛔ DELIBERATELY WEAKER THAN direct mode's dry-run (PreviewPut). The local
// tree in PR mode is only synced at pod start and may lag the base (#1718);
// refusing on it would answer
// "invalid" for a write the PR then accepts, until the pod restarts. The
// tree-derived checks (tenant file resolution, added sections, key validation
// against the base's defaults, the eol guard) run when the PR is cut, against
// the fresh base — so this cannot predict them, and returns no notices
// (validateBodyOnly produces none; the PR write collects them on the base).
func DryRunValidateBodyOnly(tenantID, yamlContent string) (errs []string, err error) {
	if err := guardTenantID(tenantID); err != nil {
		return nil, err
	}
	return dryRunPreflight(tenantID, yamlContent), nil
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
