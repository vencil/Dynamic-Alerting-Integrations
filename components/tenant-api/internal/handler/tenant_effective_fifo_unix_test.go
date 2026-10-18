//go:build unix

package handler

import (
	"net/http"
	"os"
	"path/filepath"
	"syscall"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/gitops/gitopstest"
)

// #2939: /effective walks on the Writer's bounded walk (#1977). A FIFO named
// *.yaml used to block that walk until the bound failed the request closed;
// the exporter's walker no longer opens a special file, so the walk returns
// at once and the tenant resolves — the FIFO is an unreadable entry of the
// tree, as a file the walker cannot read always was. (A walk that does not
// return — a hung mount — still fails closed within the bound: gitops'
// TestResolveEffective_StuckWalkFailsReadsNotWrites; the 500 it maps to:
// TestGetTenantEffective_WalkFailureIsFixedText.)
func TestGetTenantEffective_FIFOInTreeDoesNotBlock(t *testing.T) {
	t.Parallel()
	dir := shortTempDir(t)
	writeFile(t, filepath.Join(dir, "t1.yaml"), "tenants:\n  t1: {}\n")
	fifo := filepath.Join(dir, "stuck.yaml")
	if err := syscall.Mkfifo(fifo, 0o644); err != nil {
		t.Skipf("mkfifo: %v", err)
	}
	t.Cleanup(func() { // a reader a failing run left on it sees EOF
		if f, err := os.OpenFile(fifo, os.O_WRONLY|syscall.O_NONBLOCK, 0); err == nil {
			_ = f.Close()
		}
	})
	h := GetTenantEffective(&Deps{ConfigDir: dir, Writer: gitopstest.NewWriter(dir, "")})
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
		if a.code != http.StatusOK {
			t.Fatalf("got %d %s, want 200", a.code, a.body)
		}
	case <-time.After(30 * time.Second):
		t.Fatal("/effective blocked on the FIFO")
	}
}
