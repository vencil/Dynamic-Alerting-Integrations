package handler

// #3050 round 2: the in-lock 409 of PUT .../custom-alerts. The handler's
// own base_hash check passes; the file changes before the Writer takes its
// lock; WriteIfUnchanged then answers PreconditionError with the NEW file's
// hash, which is handed out only when that new file is one GET would show.
//
// The window is held open without a sleep: a write to another tenant holds
// the single writer token, parked inside its validator (SetValidator, the
// generator seam), while the custom-alerts request queues behind it — seen
// in its goroutine's stack, waiting on this Writer — and the tenant file is
// rewritten on disk.

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/rbac"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

// parkingValidator parks the first write it judges until release closes.
type parkingValidator struct {
	once    sync.Once
	entered chan struct{}
	release chan struct{}
}

func (v *parkingValidator) ValidateWrites(context.Context, string, []gitops.ProposedFile) error {
	first := false
	v.once.Do(func() { first = true; close(v.entered) })
	if first {
		<-v.release
	}
	return nil
}

// waitForGoroutine waits until some goroutine's stack holds every frame in
// want.
func waitForGoroutine(t *testing.T, want ...string) {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	buf := make([]byte, 1<<20)
	for time.Now().Before(deadline) {
		n := runtime.Stack(buf, true)
		for _, g := range strings.Split(string(buf[:n]), "\n\n") {
			all := true
			for _, w := range want {
				all = all && strings.Contains(g, w)
			}
			if all {
				return
			}
		}
		runtime.Gosched()
	}
	t.Fatalf("no goroutine reached %v", want)
}

func TestPutTenantCustomAlerts_InLockConflictWithholdsHash(t *testing.T) {
	t.Parallel()
	base := "tenants:\n  " + orgEnfTenantIn + ":\n    _silent_mode: \"critical\"\n"
	for _, c := range []struct {
		name, rewrite string
		wantHash      bool
	}{
		// The file now also declares a tenant the caller may not read.
		{"now declares an unreadable tenant", base + wOutBlock, false},
		// Control: still only the caller's tenant — the hash, as today.
		{"still only this tenant", "tenants:\n  " + orgEnfTenantIn + ":\n    _silent_mode: \"warning\"\n", true},
	} {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			f := wFixture(t, "writer", base)
			v := &parkingValidator{entered: make(chan struct{}), release: make(chan struct{})}
			f.writer.SetValidator(v)
			released := false
			defer func() {
				if !released {
					close(v.release)
				}
			}()

			// A: holds the writer token inside its validator.
			aDone := make(chan error, 1)
			go func() {
				_, err := f.writer.Write(context.Background(), "tenant-org-park", "park@example.com",
					"tenants:\n  tenant-org-park:\n    _silent_mode: \"critical\"\n")
				aDone <- err
			}()
			<-v.entered

			// B: passes the handler's own base_hash check, then queues.
			b, _ := json.Marshal(PutCustomAlertsRequest{CustomAlerts: &[]map[string]any{}, BaseHash: cfg.ComputeSourceHash([]byte(base))})
			bDone := make(chan *httptest.ResponseRecorder, 1)
			go func() {
				bDone <- wServe(f, PutTenantCustomAlerts(f.deps()), rbac.PermWrite, "PUT", string(b),
					map[string]string{"Content-Type": "application/json"})
			}()
			// The receiver address pins it to THIS writer, not a parallel test's.
			waitForGoroutine(t, "PutTenantCustomAlerts", fmt.Sprintf("gitops.(*Writer).acquireWrite(%p", f.writer))

			if err := os.WriteFile(filepath.Join(f.configDir, orgEnfTenantIn+".yaml"), []byte(c.rewrite), 0o644); err != nil {
				t.Fatal(err)
			}
			close(v.release)
			released = true
			if err := <-aDone; err != nil {
				t.Fatalf("parking write: %v", err)
			}
			w := <-bDone

			d := decodeDetail(t, w)
			if w.Code != 409 || d["code"] != CodeConflict {
				t.Fatalf("%d %s, want the in-lock 409 %s", w.Code, w.Body.String(), CodeConflict)
			}
			got, has := d["current_source_hash"]
			switch {
			case c.wantHash && got != cfg.ComputeSourceHash([]byte(c.rewrite)):
				t.Errorf("current_source_hash = %v, want the rewritten file's hash", got)
			case !c.wantHash && has:
				t.Errorf("current_source_hash = %v handed out for a file GET withholds", got)
			}
			if !c.wantHash {
				assertNoOtherTenant(t, "in-lock 409", w.Body.Bytes(), c.rewrite)
			}
		})
	}
}
