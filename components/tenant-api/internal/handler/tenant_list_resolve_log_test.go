package handler

// #2467: the tenant list and search derive each tenant's silent-mode /
// maintenance state on every request; the exporter resolver's WARNs for a
// malformed _silent_mode / _state_maintenance must not reach the process log
// per request. The exporter's own reading of the same tree still writes them
// (the control below, and the config package's TestOperationalStatesLogSink).
//
// ⛔ NOT t.Parallel(): swaps the process-global log output. Top-level
// parallel tests in this package only resume after the sequential ones
// finish, so nothing else writes into the buffer; the cleanup is an
// idempotent reset to os.Stderr, never a save-then-restore.

import (
	"bytes"
	"log"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"
	"time"

	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

func TestListAndSearchWriteNoResolverLog(t *testing.T) {
	configDir := setupConfigDir(t, map[string]string{
		"_defaults.yaml": "defaults:\n  m1_x: 1\nstate_filters:\n  maintenance:\n    severity: warning\n",
		"tx.yaml":        "tenants:\n  tx:\n    _silent_mode: \"bogus\"\n    _state_maintenance:\n      expires: \"nope\"\n",
	})
	d := &Deps{ConfigDir: configDir, RBAC: newRBACManager(t, ""), SearchCache: NewTenantSnapshotCache()}
	var buf bytes.Buffer
	log.SetOutput(&buf)
	t.Cleanup(func() { log.SetOutput(os.Stderr) })

	get := func(h http.HandlerFunc, url string) {
		t.Helper()
		w := httptest.NewRecorder()
		h(w, httptest.NewRequest(http.MethodGet, url, nil))
		if w.Code != http.StatusOK {
			t.Fatalf("GET %s = %d: %s", url, w.Code, w.Body.String())
		}
		if !bytes.Contains(w.Body.Bytes(), []byte(`"config_derived"`)) {
			t.Fatalf("precondition: GET %s derived no state: %s", url, w.Body.String())
		}
	}
	// The first request loads the snapshot; what that load writes is not
	// this test's subject, the per-request derivation is.
	get(ListTenants(d), "/api/v1/tenants")
	buf.Reset()
	for i := 0; i < 2; i++ {
		get(ListTenants(d), "/api/v1/tenants")
		get(SearchTenants(d), "/api/v1/tenants/search")
	}
	if buf.Len() != 0 {
		t.Errorf("list / search wrote the resolver's lines to the process log:\n%s", buf.String())
	}

	// Control: the exporter's reading of the same tree does write them, so
	// the silence above is the requests', not a tree that trips nothing.
	c, _, err := cfg.LoadDir(configDir, nil)
	if err != nil {
		t.Fatal(err)
	}
	buf.Reset()
	c.OperationalStatesAt(time.Now())
	if got := bytes.Count(buf.Bytes(), []byte("\n")); got != 2 {
		t.Errorf("control: the exporter reading wrote %d lines, want 2 (silent mode + maintenance expires):\n%s", got, buf.String())
	}
}
