package handler

import (
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/gitops"
)

// A walk that fails (here: the config dir is gone; a walk past its bound
// fails the same way, see gitops' TestResolveEffective_StuckWalkFailsReadsNotWrites)
// answers 500 with the fixed text: the walker's error names server paths
// (`stat "/…/conf.d": …`), so it goes to the log only.
func TestGetTenantEffective_WalkFailureIsFixedText(t *testing.T) {
	t.Parallel()
	dir := filepath.Join(t.TempDir(), "conf.d")
	if err := os.Mkdir(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	h := GetTenantEffective(&Deps{ConfigDir: dir, Writer: gitops.NewWriter(dir, "")})
	if err := os.Remove(dir); err != nil {
		t.Fatal(err)
	}
	code, body := effectiveGET(h, "t1")
	if code != http.StatusInternalServerError || !strings.Contains(body, msgEffectiveUnresolved) {
		t.Fatalf("got %d %s, want 500 with the fixed text", code, body)
	}
	if strings.Contains(body, dir) {
		t.Errorf("body names a server path: %s", body)
	}
}
