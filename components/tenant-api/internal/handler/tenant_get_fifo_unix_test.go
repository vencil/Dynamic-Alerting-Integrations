//go:build unix

package handler

// #2208: GET /tenants/{id} now reads every ROOT `_`-prefixed YAML file (the
// platform files' `tenants:` layer), so a root file whose read never returns
// — a FIFO named `_x.yaml`, a hung mount — would hang the request forever.
// (Before, only a FIFO named like the defaults carrier did.) The read is
// bounded the way the writer bounds its conf.d walk: past the bound GET
// fails with a 500 and a fixed message, later GETs fail at once while that
// read is still stuck, and GET recovers once it returns.

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/boundedcall"
)

func TestGetTenant_BlockedRootPlatformReadFailsAndRecovers(t *testing.T) {
	for _, fifoName := range []string{"_x.yaml", "_defaults.yml"} {
		t.Run(fifoName, func(t *testing.T) {
			configDir := setupConfigDir(t, map[string]string{
				"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
				"tx.yaml":        "tenants:\n  tx:\n    mysql_connections: \"90\"\n",
			})
			fifo := filepath.Join(configDir, fifoName)
			if err := syscall.Mkfifo(fifo, 0o644); err != nil {
				t.Skipf("mkfifo: %v", err)
			}
			unblock := func() { // open for write, then close: the blocked reader sees EOF
				if f, err := os.OpenFile(fifo, os.O_WRONLY|syscall.O_NONBLOCK, 0); err == nil {
					_ = f.Close()
				}
			}
			t.Cleanup(unblock)

			guard := &boundedcall.Guard{Timeout: 200 * time.Millisecond}
			d := &Deps{ConfigDir: configDir, RootReadGuard: guard}
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

			// While that read is still blocked, a second GET does not start
			// (and leak) another one behind the same FIFO.
			start = time.Now()
			if w := get(); w.Code != http.StatusInternalServerError {
				t.Fatalf("second GET = %d, want 500 while the first read is stuck", w.Code)
			}
			if took := time.Since(start); took >= guard.Timeout {
				t.Errorf("second GET took %v: it waited on a new read instead of failing at once", took)
			}

			// Release and remove the FIFO: the stuck read returns and GET
			// serves the tenant again.
			unblock()
			deadline := time.Now().Add(5 * time.Second)
			for guard.Stuck() != 0 {
				if time.Now().After(deadline) {
					t.Fatalf("stuck read never drained (stuck=%d)", guard.Stuck())
				}
				time.Sleep(10 * time.Millisecond)
			}
			if err := os.Remove(fifo); err != nil {
				t.Fatal(err)
			}
			if w := get(); w.Code != http.StatusOK {
				t.Fatalf("GET after recovery = %d %s", w.Code, w.Body.String())
			}
		})
	}
}
