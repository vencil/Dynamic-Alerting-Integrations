package handler

// Concurrent GETs share one in-flight root platform read (Deps.loadMergedConfig).
// Sharing the read must not share the ANSWER: each tenant gets its own
// platform entry, and nothing outlives the read — a GET that starts after
// every read of the old file has FINISHED sees the change. (A GET that joins
// a read already in progress sees the files as that read found them; see
// rootReadFlight.) Run with -race.

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/boundedcall"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

// TestRootReadFlightMergesOnlyWhatIsInFlight: waiters that join a running
// call share its result; a call that starts after it returned runs again.
func TestRootReadFlightMergesOnlyWhatIsInFlight(t *testing.T) {
	t.Parallel()
	var f rootReadFlight
	var runs int
	var mu sync.Mutex
	release := make(chan struct{})
	started := make(chan struct{})
	fn := func() (cfg.RootPlatform, error) {
		mu.Lock()
		runs++
		first := runs == 1
		mu.Unlock()
		if first {
			close(started)
			<-release
		}
		return cfg.RootPlatform{}, nil
	}
	var wg sync.WaitGroup
	wg.Add(1)
	go func() { defer wg.Done(); _, _ = f.do("k", fn) }()
	<-started
	for i := 0; i < 5; i++ {
		wg.Add(1)
		go func() { defer wg.Done(); _, _ = f.do("k", fn) }()
	}
	// Release the leader only once all five have joined its call.
	deadline := time.Now().Add(5 * time.Second)
	for {
		f.mu.Lock()
		joined := f.calls["k"].waiters
		f.mu.Unlock()
		if joined == 5 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("only %d of 5 callers joined the in-flight call", joined)
		}
		time.Sleep(time.Millisecond)
	}
	close(release)
	wg.Wait()
	mu.Lock()
	inFlight := runs
	mu.Unlock()
	if inFlight != 1 {
		t.Fatalf("6 overlapping calls ran fn %d times, want 1", inFlight)
	}
	_, _ = f.do("k", fn)
	mu.Lock()
	defer mu.Unlock()
	if runs != 2 {
		t.Fatalf("a call after the first returned ran fn %d times in total, want 2 (nothing may be remembered)", runs)
	}
}

// TestGetTenant_DifferentGuardsDoNotWaitOnEachOther: the in-flight key is
// guard + directory. A GET under guard B must not join (and time out with)
// a read that guard A started and that is stuck: B starts its own read and
// finishes inside its own bound.
func TestGetTenant_DifferentGuardsDoNotWaitOnEachOther(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"tx.yaml":        "tenants:\n  tx:\n    mysql_connections: \"90\"\n",
	})
	release := make(chan struct{})
	t.Cleanup(func() { close(release) })
	entered := make(chan struct{})
	gA := &boundedcall.Guard{Timeout: 3 * time.Second}
	dA := &Deps{ConfigDir: configDir, RootReadGuard: gA}
	dA.loadRoot = func(dir string) cfg.RootPlatform {
		close(entered)
		<-release
		return cfg.LoadRootPlatform(dir)
	}
	go func() {
		w := httptest.NewRecorder()
		GetTenant(dA)(w, newRequestWithChiParam("GET", "/api/v1/tenants/tx", "id", "tx", nil))
	}()
	<-entered // A's read is in flight and stuck

	gB := &boundedcall.Guard{Timeout: 500 * time.Millisecond}
	dB := &Deps{ConfigDir: configDir, RootReadGuard: gB}
	start := time.Now()
	w := httptest.NewRecorder()
	GetTenant(dB)(w, newRequestWithChiParam("GET", "/api/v1/tenants/tx", "id", "tx", nil))
	took := time.Since(start)
	if w.Code != http.StatusOK {
		t.Errorf("GET under guard B = %d %s (after %v): it joined guard A's stuck read", w.Code, w.Body.String(), took)
	}
	if took >= gB.Timeout {
		t.Errorf("GET under guard B took %v, beyond its own %v bound", took, gB.Timeout)
	}
}

func TestGetTenant_ConcurrentGETsGetTheirOwnTenantAndFreshFiles(t *testing.T) {
	t.Parallel()
	const n = 8
	files := map[string]string{"_defaults.yaml": "defaults:\n  mysql_connections: 80\n"}
	platform := func(base int) string {
		var b strings.Builder
		b.WriteString("tenants:\n")
		for i := 0; i < n; i++ {
			fmt.Fprintf(&b, "  t%d:\n    mysql_connections: \"%d\"\n", i, base+i)
		}
		return b.String()
	}
	files["_platform.yaml"] = platform(100)
	for i := 0; i < n; i++ {
		files[fmt.Sprintf("t%d.yaml", i)] = fmt.Sprintf("tenants:\n  t%d:\n    _silent_mode: warning\n", i)
	}
	configDir := setupConfigDir(t, files)
	d := &Deps{ConfigDir: configDir}

	value := func(id string) (float64, error) {
		w := httptest.NewRecorder()
		GetTenant(d)(w, newRequestWithChiParam("GET", "/api/v1/tenants/"+id, "id", id, nil))
		if w.Code != http.StatusOK {
			return 0, fmt.Errorf("GET %s = %d %s", id, w.Code, w.Body.String())
		}
		var detail TenantDetail
		if err := json.Unmarshal(w.Body.Bytes(), &detail); err != nil {
			return 0, err
		}
		for _, r := range detail.Resolved {
			if r.Tenant != id {
				return 0, fmt.Errorf("GET %s carries a row of tenant %s", id, r.Tenant)
			}
		}
		v, ok := resolvedValue(detail, "mysql", "connections", "warning")
		if !ok {
			return 0, fmt.Errorf("GET %s has no mysql_connections row", id)
		}
		return v, nil
	}

	// One synchronous GET per tenant BEFORE anything changes: it completes a
	// read of the OLD files. If a finished read were kept (its entry never
	// removed, or its result cached), every later GET would be served that
	// old read — deterministically, not only when a burst happens to straddle
	// the rewrite — and the post-rewrite check below fails every time.
	for i := 0; i < n; i++ {
		if v, err := value(fmt.Sprintf("t%d", i)); err != nil || v != float64(100+i) {
			t.Fatalf("t%d before the rewrite = (%v, %v), want %d", i, v, err, 100+i)
		}
	}

	// Rounds of concurrent GETs for every tenant, with the platform file
	// rewritten (base 100 → 200) while they run. Mid-flight a GET may see
	// either version, but always its OWN tenant's entry of it.
	var wg sync.WaitGroup
	errs := make(chan error, 1024)
	for round := 0; round < 20; round++ {
		for i := 0; i < n; i++ {
			wg.Add(1)
			go func(i int) {
				defer wg.Done()
				v, err := value(fmt.Sprintf("t%d", i))
				switch {
				case err != nil:
					errs <- err
				case v != float64(100+i) && v != float64(200+i):
					errs <- fmt.Errorf("t%d got %v, want its own entry (%d or %d)", i, v, 100+i, 200+i)
				}
			}(i)
		}
		if round == 10 {
			tmp := filepath.Join(configDir, ".platform.tmp")
			if err := os.WriteFile(tmp, []byte(platform(200)), 0o644); err != nil {
				t.Fatal(err)
			}
			// Atomic swap, as a GitOps checkout does: a reader sees the old
			// file or the new one, never a torn one.
			if err := os.Rename(tmp, filepath.Join(configDir, "_platform.yaml")); err != nil {
				t.Fatal(err)
			}
		}
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Error(err)
	}

	// After the rewrite every GET — none of them in flight with a read that
	// started before it — sees the new values.
	for i := 0; i < n; i++ {
		v, err := value(fmt.Sprintf("t%d", i))
		if err != nil {
			t.Fatal(err)
		}
		if v != float64(200+i) {
			t.Errorf("t%d = %v after the rewrite, want %d (a stale root read was reused)", i, v, 200+i)
		}
	}
}
