package handler

// #3064 (#3050 round 2's in-lock 409, carried over): PUT .../custom-alerts
// merges outside the writer lock. The handler's own git-managed and base_hash
// checks pass; the file changes before the Writer takes its lock. If it is
// now shared (git-managed), the Writer refuses it under the lock — 409
// TENANT_GIT_MANAGED, no hash of the shared file; if it is still the tenant's
// own, WriteIfUnchanged answers PreconditionError with the NEW file's hash.
//
// The window is held open without a sleep: a write to another tenant holds
// the single writer token, parked inside its validator (SetValidator, the
// generator seam), while the custom-alerts request queues behind it — seen
// in its goroutine's stack, waiting on this Writer — and the tenant file is
// rewritten on disk.

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/errcode"
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

func (v *parkingValidator) ValidateWrites(context.Context, string, []gitops.ProposedFile) (*gitops.KeyVerdict, error) {
	first := false
	v.once.Do(func() { first = true; close(v.entered) })
	if first {
		<-v.release
	}
	return nil, nil
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

func TestPutTenantCustomAlerts_InLockGitManaged(t *testing.T) {
	t.Parallel()
	base := "tenants:\n  " + orgEnfTenantIn + ":\n    _silent_mode: \"critical\"\n"
	for _, c := range []struct {
		name, rewrite string
		wantHash      bool
	}{
		// The file now also declares another tenant: git-managed.
		{"now shared with another tenant", base + wOutBlock, false},
		// Control: still only the caller's tenant — CONFLICT and the hash.
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

			// The rewrite is a commit made outside tenant-api: an uncommitted
			// one would be TENANT_FILE_DIRTY (#1535 item 6) before either
			// verdict this test is about. The parked write's judged base is
			// then stale, so it ends in ErrConflict — it only holds the token.
			if err := os.WriteFile(filepath.Join(f.configDir, orgEnfTenantIn+".yaml"), []byte(c.rewrite), 0o644); err != nil {
				t.Fatal(err)
			}
			for _, args := range [][]string{{"add", "-A", "--", orgEnfTenantIn + ".yaml"}, {"commit", "-q", "-m", "outside rewrite"}} {
				if out, err := exec.Command("git", append([]string{"-C", f.configDir}, args...)...).CombinedOutput(); err != nil {
					t.Fatalf("git %v: %v\n%s", args, err, out)
				}
			}
			close(v.release)
			released = true
			if err := <-aDone; err != nil && !errors.Is(err, gitops.ErrConflict) {
				t.Fatalf("parking write: %v", err)
			}
			w := <-bDone

			d := decodeDetail(t, w)
			wantCode := errcode.Conflict
			if !c.wantHash {
				wantCode = errcode.TenantGitManaged
			}
			if w.Code != 409 || d["code"] != string(wantCode) {
				t.Fatalf("%d %s, want the in-lock 409 %s", w.Code, w.Body.String(), wantCode)
			}
			got, has := d["current_source_hash"]
			switch {
			case c.wantHash && got != cfg.ComputeSourceHash([]byte(c.rewrite)):
				t.Errorf("current_source_hash = %v, want the rewritten file's hash", got)
			case !c.wantHash && has:
				t.Errorf("current_source_hash = %v handed out for a git-managed file", got)
			}
			if !c.wantHash {
				assertNoOtherTenant(t, "in-lock 409", w.Body.Bytes(), c.rewrite)
				if b, _ := os.ReadFile(filepath.Join(f.configDir, orgEnfTenantIn+".yaml")); string(b) != c.rewrite {
					t.Errorf("the shared file changed: %q", b)
				}
			}
		})
	}
}
