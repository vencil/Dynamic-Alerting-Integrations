//go:build unix

package handler

// #2208 follow-up (PR #2214 security review): a root platform read that never
// returns must cost ONE stuck goroutine, however many GETs arrive inside the
// first timeout window. The root read is tenant-independent, so concurrent
// GETs for one conf.d share a single in-flight read (and its bound) instead
// of each starting — and leaking — their own behind the same FIFO.

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"sync"
	"syscall"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/boundedcall"
)

func TestGetTenant_ConcurrentGETsShareOneStuckRootRead(t *testing.T) {
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
			unblock := func() {
				if f, err := os.OpenFile(fifo, os.O_WRONLY|syscall.O_NONBLOCK, 0); err == nil {
					_ = f.Close()
				}
			}
			t.Cleanup(unblock)

			guard := &boundedcall.Guard{Timeout: 200 * time.Millisecond}
			d := &Deps{ConfigDir: configDir, RootReadGuard: guard}
			get := func() int {
				req := newRequestWithChiParam("GET", "/api/v1/tenants/tx", "id", "tx", nil)
				w := httptest.NewRecorder()
				GetTenant(d)(w, req)
				return w.Code
			}

			before := runtime.NumGoroutine()
			const n = 20
			codes := make([]int, n)
			start := make(chan struct{})
			var wg sync.WaitGroup
			for i := 0; i < n; i++ {
				wg.Add(1)
				go func(i int) {
					defer wg.Done()
					<-start
					codes[i] = get()
				}(i)
			}
			close(start)
			done := make(chan struct{})
			go func() { wg.Wait(); close(done) }()
			select {
			case <-done:
			case <-time.After(5 * time.Second):
				t.Fatalf("GETs still blocked after 5s with a %v bound", guard.Timeout)
			}
			for i, c := range codes {
				if c != http.StatusInternalServerError {
					t.Errorf("GET %d = %d, want 500 while the root read is stuck", i, c)
				}
			}
			if s := guard.Stuck(); s > 1 {
				t.Errorf("%d concurrent GETs left %d stuck root reads, want at most 1", n, s)
			}
			// Tolerance for runtime/test goroutines; one stuck read is expected.
			if extra := runtime.NumGoroutine() - before; extra > 3 {
				t.Errorf("%d goroutines outlive the burst (want the one stuck read, give or take)", extra)
			}

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
			if c := get(); c != http.StatusOK {
				t.Fatalf("GET after recovery = %d", c)
			}
		})
	}
}
