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
// derives config state through the exporter's own loader, which reads the
// FIFO below with no bound of its own (the cache bounds it in production).
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

// platform read blocked (a FIFO named `_*.yaml`): the list answers from the
// tenant files while the read is blocked, and once the blocked read returns
// the next list reads the platform files again.
func TestListTenants_BlockedPlatformReadFallsBackThenRecovers(t *testing.T) {
	t.Parallel()
	dir := setupConfigDir(t, map[string]string{
		"tx.yaml": "tenants:\n  tx:\n    _metadata:\n      db_type: mariadb\n",
	})
	fifo := filepath.Join(dir, "_x.yaml")
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
	metadataRootReadGuards.Store(dir, guard)
	t.Cleanup(func() { metadataRootReadGuards.Delete(dir) })

	tenantOnly := listedMetadata{DBType: "mariadb"}
	for i := 0; i < 2; i++ { // the read that times out, then one refused at once
		rows := listedRowsByID(t, dir)
		if got := metadataOf(rows["tx"]); !reflect.DeepEqual(got, tenantOnly) {
			t.Errorf("LIST #%d tx metadata = %+v, want %+v", i+1, got, tenantOnly)
		}
	}
	if guard.Stuck() == 0 {
		t.Fatal("premise: the platform read never blocked, so this run proves nothing")
	}

	// End the blocked read, replace the FIFO with a readable platform file.
	deadline := time.Now().Add(5 * time.Second)
	for guard.Stuck() > 0 && time.Now().Before(deadline) {
		unblock()
		time.Sleep(10 * time.Millisecond)
	}
	if guard.Stuck() > 0 {
		t.Fatal("the blocked read did not return after the FIFO was opened for writing")
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
