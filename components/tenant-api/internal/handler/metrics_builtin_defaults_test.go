package handler

import (
	"net/http"
	"net/http/httptest"
	"regexp"
	"testing"

	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

// TestMetrics_BuiltinDefaultsInfo pins tenant_api_builtin_defaults_info
// (#3831): exactly one series, value 1, whose digest is the built-in table
// this binary was compiled with — the digest the platform rule pack's
// BuiltinDefaultsDigestMismatch compares with its own. The golden test
// normalizes the digest away, so this is the test that sees it.
func TestMetrics_BuiltinDefaultsInfo(t *testing.T) {
	t.Parallel()
	w := httptest.NewRecorder()
	MetricsHandler(w, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	re := regexp.MustCompile(`(?m)^tenant_api_builtin_defaults_info\{digest="([0-9a-f]+)"\} 1$`)
	got := re.FindAllStringSubmatch(w.Body.String(), -1)
	if len(got) != 1 {
		t.Fatalf("tenant_api_builtin_defaults_info: %d series, want 1\nbody:\n%s", len(got), w.Body.String())
	}
	if want := cfg.BuiltinDefaultsDigest(); got[0][1] != want || len(want) != 12 {
		t.Fatalf("digest = %q, want the compiled table's %q (12 hex digits)", got[0][1], want)
	}
}
