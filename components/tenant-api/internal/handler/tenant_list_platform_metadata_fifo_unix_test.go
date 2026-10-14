//go:build unix

package handler

// #2370, split from tenant_list_platform_metadata_test.go: syscall.Mkfifo
// exists only on unix.

import (
	"os"
	"path/filepath"
	"reflect"
	"syscall"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/boundedcall"
)

// listedRowsByID runs the list's load (loadAllTenants) and returns its rows
// by id. The load, not the handler: without a snapshot cache the handler also
// derives config state through the exporter's own loader.
func listedRowsByID(t *testing.T, dir string) map[string]TenantSummary {
	t.Helper()
	rows, err := loadAllTenants(dir)
	if err != nil {
		t.Fatalf("loadAllTenants: %v", err)
	}
	out := make(map[string]TenantSummary, len(rows))
	for _, r := range rows {
		out[r.ID] = r
	}
	return out
}

// A FIFO named `_*.yaml` at the root (#2939): the platform read used to block
// on it until the bound gave up, and the list fell back while it stayed
// stuck. The exporter's walker no longer opens a special file, so the read
// returns at once — unusable, as any root file it cannot read — and the list
// answers from the tenant files without a stuck read. Once the FIFO is
// replaced by a readable platform file, the next list reads it. (A read that
// does not return — a hung mount — is bounded by boundedcall.Do, whose
// failure takes the same fallback: platformMetadataOrNone.)
func TestListTenants_PlatformFIFOFallsBackThenRecovers(t *testing.T) {
	t.Parallel()
	dir := setupConfigDir(t, map[string]string{
		"tx.yaml": "tenants:\n  tx:\n    _metadata:\n      db_type: mariadb\n",
	})
	fifo := filepath.Join(dir, "_x.yaml")
	if err := syscall.Mkfifo(fifo, 0o644); err != nil {
		t.Skipf("mkfifo: %v", err)
	}
	t.Cleanup(func() { // a reader a failing run left on it sees EOF
		if f, err := os.OpenFile(fifo, os.O_WRONLY|syscall.O_NONBLOCK, 0); err == nil {
			_ = f.Close()
		}
	})
	guard := &boundedcall.Guard{Timeout: 5 * time.Second}
	metadataRootReadGuards.Store(dir, guard)
	t.Cleanup(func() { metadataRootReadGuards.Delete(dir) })

	tenantOnly := listedMetadata{DBType: "mariadb"}
	for i := 0; i < 2; i++ {
		done := make(chan map[string]TenantSummary, 1)
		go func() { done <- listedRowsByID(t, dir) }()
		var rows map[string]TenantSummary
		select {
		case rows = <-done:
		case <-time.After(30 * time.Second):
			t.Fatal("LIST blocked on the FIFO")
		}
		if got := metadataOf(rows["tx"]); !reflect.DeepEqual(got, tenantOnly) {
			t.Errorf("LIST #%d tx metadata = %+v, want %+v", i+1, got, tenantOnly)
		}
	}
	if s := guard.Stuck(); s != 0 {
		t.Fatalf("stuck platform reads = %d, want 0", s)
	}

	if err := os.Remove(fifo); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "_platform.yaml"),
		[]byte("tenants:\n  tx:\n    _metadata:\n      owner: plat-team\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	rows := listedRowsByID(t, dir)
	if got, want := metadataOf(rows["tx"]), (listedMetadata{DBType: "mariadb", Owner: "plat-team"}); !reflect.DeepEqual(got, want) {
		t.Errorf("tx metadata after recovery = %+v, want %+v", got, want)
	}
}
