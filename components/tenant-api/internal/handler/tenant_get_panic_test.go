package handler

// The bounded GET root read (Deps.loadMergedConfig) runs on boundedcall's own
// goroutine, out of reach of chi's Recoverer. A panic in it must come back as
// a 500 with the fixed message — not take the process down (before the bound,
// the read ran on the handler goroutine and Recoverer turned a panic into a
// 500).

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

func TestGetTenant_RootReadPanicIsA500(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, map[string]string{
		"tx.yaml": "tenants:\n  tx:\n    mysql_connections: \"90\"\n",
	})
	d := &Deps{ConfigDir: configDir}
	d.loadRoot = func(string) cfg.RootPlatform { panic("root read exploded: secret detail") }

	req := newRequestWithChiParam("GET", "/api/v1/tenants/tx", "id", "tx", nil)
	w := httptest.NewRecorder()
	GetTenant(d)(w, req)
	if w.Code != http.StatusInternalServerError || !strings.Contains(w.Body.String(), msgRootPlatformRead) {
		t.Fatalf("GET = %d %s, want 500 with the fixed message", w.Code, w.Body.String())
	}
	if strings.Contains(w.Body.String(), "secret detail") {
		t.Errorf("the panic value reached the client: %s", w.Body.String())
	}

	// The guard is still usable: the next GET (real merge) succeeds.
	d.loadRoot = nil
	w = httptest.NewRecorder()
	GetTenant(d)(w, newRequestWithChiParam("GET", "/api/v1/tenants/tx", "id", "tx", nil))
	if w.Code != http.StatusOK {
		t.Fatalf("GET after the panic = %d %s", w.Code, w.Body.String())
	}
}
