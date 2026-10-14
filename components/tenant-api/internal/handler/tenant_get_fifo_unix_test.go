//go:build unix

package handler

// #2939: a FIFO named like a root platform file (`_x.yaml`), a defaults
// carrier (`_defaults.yml`) or the profiles file used to block GET's root
// read until the bound failed it, and every GET after it while the read
// stayed stuck. The exporter's walker no longer opens a special file: the
// read returns at once without the FIFO — as it does for any root file it
// cannot read — and GET serves the tenant.

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"syscall"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/boundedcall"
)

func TestGetTenant_RootFIFODoesNotBlock(t *testing.T) {
	t.Parallel()
	for _, fifoName := range []string{"_x.yaml", "_defaults.yml", "_profiles.yaml"} {
		fifoName := fifoName
		t.Run(fifoName, func(t *testing.T) {
			t.Parallel()
			configDir := setupConfigDir(t, map[string]string{
				"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
				"tx.yaml":        "tenants:\n  tx:\n    mysql_connections: \"90\"\n",
			})
			fifo := filepath.Join(configDir, fifoName)
			if err := syscall.Mkfifo(fifo, 0o644); err != nil {
				t.Skipf("mkfifo: %v", err)
			}
			t.Cleanup(func() { // a reader a failing run left on it sees EOF
				if f, err := os.OpenFile(fifo, os.O_WRONLY|syscall.O_NONBLOCK, 0); err == nil {
					_ = f.Close()
				}
			})
			guard := &boundedcall.Guard{Timeout: 5 * time.Second}
			d := &Deps{ConfigDir: configDir, RootReadGuard: guard}
			for i := 0; i < 2; i++ {
				w := httptest.NewRecorder()
				done := make(chan struct{})
				go func() {
					GetTenant(d)(w, newRequestWithChiParam("GET", "/api/v1/tenants/tx", "id", "tx", nil))
					close(done)
				}()
				select {
				case <-done:
				case <-time.After(30 * time.Second):
					t.Fatal("GET blocked on the FIFO")
				}
				if w.Code != http.StatusOK {
					t.Errorf("GET #%d = %d %s, want 200", i+1, w.Code, w.Body.String())
				}
			}
			if s := guard.Stuck(); s != 0 {
				t.Errorf("stuck root reads = %d, want 0", s)
			}
		})
	}
}
