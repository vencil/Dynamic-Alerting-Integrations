package handler

// #2208: GET /tenants/{id} reads every ROOT `_`-prefixed YAML file (the
// platform files' `tenants:` layer, and the profiles it expands, #1385), so
// a root read that never returns — a hung mount — would hang the request
// forever. The read is bounded the way the writer bounds its conf.d walk:
// past the bound GET fails with a 500 and a fixed message, later GETs fail
// at once while that read is still stuck, and GET recovers once it returns.
//
// The read is held open through the per-Deps loadRoot seam. It used to be
// held by a FIFO named `_x.yaml`, which the exporter's walker read and
// blocked on; since #2939 the walker never opens one (see
// tenant_get_fifo_unix_test.go).

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	cfg "github.com/vencil/threshold-exporter/pkg/config"

	"github.com/vencil/tenant-api/internal/boundedcall"
)

// holdRootReads makes every root read of d block until release is called
// (idempotent, also registered as a cleanup), then read the files.
func holdRootReads(t *testing.T, d *Deps) (release func()) {
	t.Helper()
	gate := make(chan struct{})
	var once sync.Once
	release = func() { once.Do(func() { close(gate) }) }
	t.Cleanup(release)
	d.loadRoot = func(dir string) cfg.RootPlatform {
		<-gate
		return cfg.LoadRootPlatform(dir)
	}
	return release
}

func TestGetTenant_BlockedRootPlatformReadFailsAndRecovers(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"tx.yaml":        "tenants:\n  tx:\n    mysql_connections: \"90\"\n",
	})
	guard := &boundedcall.Guard{Timeout: 200 * time.Millisecond}
	d := &Deps{ConfigDir: configDir, RootReadGuard: guard}
	release := holdRootReads(t, d)
	get := func() *httptest.ResponseRecorder {
		req := newRequestWithChiParam("GET", "/api/v1/tenants/tx", "id", "tx", nil)
		w := httptest.NewRecorder()
		done := make(chan struct{})
		go func() { GetTenant(d)(w, req); close(done) }()
		select {
		case <-done:
		case <-time.After(5 * time.Second):
			t.Fatalf("GET still blocked after 5s with a %v read bound", guard.Timeout)
		}
		return w
	}

	start := time.Now()
	w := get()
	if w.Code != http.StatusInternalServerError || !strings.Contains(w.Body.String(), msgRootPlatformRead) {
		t.Fatalf("first GET = %d %s, want 500 with the fixed read-failure message", w.Code, w.Body.String())
	}
	if strings.Contains(w.Body.String(), configDir) {
		t.Errorf("error body leaks the server path: %s", w.Body.String())
	}
	t.Logf("first GET failed after %v", time.Since(start))

	// While that read is still blocked, a second GET does not start (and
	// leak) another one behind it.
	start = time.Now()
	if w := get(); w.Code != http.StatusInternalServerError {
		t.Fatalf("second GET = %d, want 500 while the first read is stuck", w.Code)
	}
	if took := time.Since(start); took >= guard.Timeout {
		t.Errorf("second GET took %v: it waited on a new read instead of failing at once", took)
	}

	// Release the read: it returns and GET serves the tenant again.
	release()
	deadline := time.Now().Add(5 * time.Second)
	for guard.Stuck() != 0 {
		if time.Now().After(deadline) {
			t.Fatalf("stuck read never drained (stuck=%d)", guard.Stuck())
		}
		time.Sleep(10 * time.Millisecond)
	}
	if w := get(); w.Code != http.StatusOK {
		t.Fatalf("GET after recovery = %d %s", w.Code, w.Body.String())
	}
}
