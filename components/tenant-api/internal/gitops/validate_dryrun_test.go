package gitops

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestValidate_CustomAlertsReadErrorCarriesNoPath: validate()'s strings reach
// the client (PUT's 400 and the read-permission dry-run), so the eol guard's
// read failure must not carry the tenant file's absolute path.
func TestValidate_CustomAlertsReadErrorCarriesNoPath(t *testing.T) {
	t.Parallel()
	const tid = "path-t"
	configDir := t.TempDir()
	// A directory where the tenant file should be: ReadFile fails with a
	// *fs.PathError that is not ENOENT, i.e. the fail-closed branch.
	target := filepath.Join(configDir, tid+".yaml")
	if err := os.Mkdir(target, 0o755); err != nil {
		t.Fatal(err)
	}
	errs, _ := validate(configDir, tid, target, "tenants:\n  "+tid+":\n    _silent_mode: \"false\"\n")
	joined := strings.Join(errs, "; ")
	if !strings.Contains(joined, "cannot read the current tenant file") {
		t.Fatalf("did not reach the read-failure branch: %q", errs)
	}
	if strings.Contains(joined, configDir) {
		t.Fatalf("validate error leaks the server path %q: %q", configDir, errs)
	}
}

// previewDryRun is direct mode's POST /validate as the handler runs it
// (#3062): PreviewPut, the production path since DryRunValidate was folded
// into it.
func previewDryRun(ctx context.Context, w *Writer, tenantID, body string) (errs, notices []string, err error) {
	res, errs, err := w.PreviewPut(ctx, tenantID, body)
	return errs, res.Notices, err
}
