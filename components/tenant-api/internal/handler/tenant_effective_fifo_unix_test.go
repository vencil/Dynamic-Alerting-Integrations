//go:build unix

package handler

import (
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/gitops"
)

// #1977: /effective walks on the Writer's bounded walk. A file whose read
// never returns (a FIFO named *.yaml) fails the request closed — 500, the
// fixed text, no path — within the walk's bound, where the cold walk every
// request used to make blocked the request for as long as the FIFO had no
// writer.
func TestGetTenantEffective_BlockedWalkFailsClosed(t *testing.T) {
	t.Parallel()
	dir := shortTempDir(t)
	writeFile(t, filepath.Join(dir, "t1.yaml"), "tenants:\n  t1: {}\n")
	fifo := filepath.Join(dir, "stuck.yaml")
	if err := syscall.Mkfifo(fifo, 0o644); err != nil {
		t.Skipf("mkfifo: %v", err)
	}
	t.Cleanup(func() { // the blocked reader sees EOF
		if f, err := os.OpenFile(fifo, os.O_WRONLY|syscall.O_NONBLOCK, 0); err == nil {
			_ = f.Close()
		}
	})
	h := GetTenantEffective(&Deps{ConfigDir: dir, Writer: gitops.NewWriter(dir, "")})
	type answer struct {
		code int
		body string
	}
	done := make(chan answer, 1)
	go func() {
		code, body := effectiveGET(h, "t1")
		done <- answer{code, body}
	}()
	select {
	case a := <-done:
		if a.code != http.StatusInternalServerError || !strings.Contains(a.body, msgEffectiveUnresolved) {
			t.Fatalf("got %d %s, want 500 with the fixed text", a.code, a.body)
		}
		if strings.Contains(a.body, dir) {
			t.Errorf("body names a server path: %s", a.body)
		}
	case <-time.After(30 * time.Second):
		t.Fatal("/effective blocked on the FIFO: the walk is unbounded")
	}
}
