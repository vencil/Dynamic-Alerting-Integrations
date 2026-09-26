package handler

// #1988 D1 PR-3 review P2 / P3 / P5a: the tenant snapshot cache against a real
// gitops.Writer, wired by the same WireTenantSnapshots cmd/server calls.
//
// The oracle is always "what a fresh, uncached load of conf.d says right now":
// a cached answer that differs from it after a write returned is stale state.

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/rbac"
)

const (
	snapTenant     = "t-snap"
	snapPlainYAML  = "tenants:\n  " + snapTenant + ":\n    cpu: \"70\"\n"
	snapSilentYAML = "tenants:\n  " + snapTenant + ":\n    cpu: \"70\"\n    _silent_mode: all\n"
)

func gitIn(t *testing.T, dir string, args ...string) {
	t.Helper()
	cmd := exec.Command("git", args...)
	cmd.Dir = dir
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("git %v: %v\n%s", args, err, out)
	}
}

// snapshotRepo is a bare origin on main plus a working clone holding one
// tenant; returns (work, bare).
func snapshotRepo(t *testing.T) (string, string) {
	t.Helper()
	bare := t.TempDir()
	if out, err := exec.Command("git", "init", "--bare", "-b", "main", bare).CombinedOutput(); err != nil {
		t.Skipf("git init --bare: %v\n%s", err, out)
	}
	seed := t.TempDir()
	gitIn(t, seed, "clone", bare, ".")
	gitIn(t, seed, "config", "user.email", "seed@example.com")
	gitIn(t, seed, "config", "user.name", "Seed")
	for name, body := range map[string]string{
		"_defaults.yaml":     "defaults:\n  cpu: 80\n",
		snapTenant + ".yaml": snapPlainYAML,
	} {
		if err := os.WriteFile(filepath.Join(seed, name), []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	gitIn(t, seed, "add", "-A")
	gitIn(t, seed, "commit", "-m", "seed")
	gitIn(t, seed, "push", "origin", "main")

	work := t.TempDir()
	gitIn(t, work, "clone", bare, ".")
	gitIn(t, work, "config", "user.email", "work@example.com")
	gitIn(t, work, "config", "user.name", "Work")
	return work, bare
}

// derivedNow is the oracle: an uncached load of dir, as the list serves it.
func derivedNow(t *testing.T, dir string) map[string]ConfigDerivedState {
	t.Helper()
	return derivedByID(t, getList(t, &Deps{ConfigDir: dir, RBAC: openModeRBAC(t)}))
}

// P5a: the production write → invalidate wiring. A direct write through the
// writer must show on the next request, well inside the TTL.
func TestWireTenantSnapshots_WriteInvalidates(t *testing.T) {
	t.Parallel()
	work, _ := snapshotRepo(t)
	w := gitops.NewWriter(work, work)
	d := &Deps{ConfigDir: work, RBAC: openModeRBAC(t), SearchCache: WireTenantSnapshots(w, work, false)}

	if got := derivedByID(t, getList(t, d))[snapTenant]; len(got.SilentTargets) != 0 {
		t.Fatalf("seed tenant already silenced: %+v", got)
	}
	if _, err := w.Write(context.Background(), snapTenant, "alice@example.com", snapSilentYAML); err != nil {
		t.Fatalf("Write: %v", err)
	}
	got := derivedByID(t, getList(t, d))
	if !reflect.DeepEqual(got, derivedNow(t, work)) || len(got[snapTenant].SilentTargets) == 0 {
		t.Errorf("list after a direct write = %+v, want the disk's %+v", got, derivedNow(t, work))
	}
}

// P3: a write that replaced the file and then failed to commit still changed
// the disk; the cache must not keep the old reading.
func TestWireTenantSnapshots_FailedCommitStillInvalidates(t *testing.T) {
	t.Parallel()
	work, _ := snapshotRepo(t)
	w := gitops.NewWriter(work, work)
	d := &Deps{ConfigDir: work, RBAC: openModeRBAC(t), SearchCache: WireTenantSnapshots(w, work, false)}
	_ = getList(t, d) // populate the cache

	// Empty author identity: the file is written, then `git commit` refuses.
	if _, err := w.Write(context.Background(), snapTenant, "", snapSilentYAML); err == nil {
		t.Fatal("expected the commit to fail with an empty author")
	}
	disk := derivedNow(t, work)
	if len(disk[snapTenant].SilentTargets) == 0 {
		t.Fatalf("precondition: the failed write was expected to leave the new file on disk, disk = %+v", disk)
	}
	if got := derivedByID(t, getList(t, d)); !reflect.DeepEqual(got, disk) {
		t.Errorf("list after a failed commit = %+v, want the disk's %+v", got, disk)
	}
}

// startHeldPR starts a WritePR whose push runs the given pre-receive hook
// body on the bare origin, and returns once the hook has started. The result
// arrives on the returned channel.
func startHeldPR(t *testing.T, w *gitops.Writer, bare, hookBody string) <-chan error {
	t.Helper()
	marker := filepath.Join(t.TempDir(), "push-started")
	hook := "#!/bin/sh\ntouch '" + marker + "'\n" + hookBody
	if err := os.WriteFile(filepath.Join(bare, "hooks", "pre-receive"), []byte(hook), 0o755); err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() {
		_, err := w.WritePR(context.Background(), snapTenant, "alice@example.com", snapSilentYAML)
		done <- err
	}()
	deadline := time.Now().Add(20 * time.Second)
	for {
		if _, err := os.Stat(marker); err == nil {
			return done
		}
		if time.Now().After(deadline) {
			t.Fatal("the push never reached the pre-receive hook")
		}
		time.Sleep(20 * time.Millisecond)
	}
}

func listRaw(t *testing.T, d *Deps) map[string]TenantSummary {
	t.Helper()
	out := map[string]TenantSummary{}
	for _, it := range getList(t, d) {
		out[it.ID] = it
	}
	return out
}

func openSearch(t *testing.T, d *Deps) *SearchResponse {
	t.Helper()
	resp, _ := searchAs(t, d, d.RBAC.Middleware(rbac.PermRead, nil), func(*http.Request) {})
	return resp
}

// P2: a PR-mode write checks out a feature branch in conf.d, pushes it and
// returns to base. A request that arrives during the push must not read —
// let alone cache — the unmerged proposal. The push is held open by a slow
// pre-receive hook on the bare origin.
func TestWireTenantSnapshots_NeverCachesAnUnmergedPRBranch(t *testing.T) {
	t.Parallel()
	work, bare := snapshotRepo(t)
	w := gitops.NewWriter(work, work)
	d := &Deps{ConfigDir: work, RBAC: openModeRBAC(t), SearchCache: WireTenantSnapshots(w, work, true)}

	done := startHeldPR(t, w, bare, "sleep 2\n")
	during := derivedByID(t, getList(t, d)) // issued while the push is held open
	if err := <-done; err != nil {
		t.Fatalf("WritePR: %v", err)
	}
	after := derivedByID(t, getList(t, d))
	disk := derivedNow(t, work)
	if len(disk[snapTenant].SilentTargets) != 0 {
		t.Fatalf("precondition: base must not carry the proposal, disk = %+v", disk)
	}
	if !reflect.DeepEqual(during, disk) {
		t.Errorf("request during the PR push read the feature branch: %+v, base is %+v", during, disk)
	}
	if !reflect.DeepEqual(after, disk) {
		t.Errorf("list after WritePR = %+v, want the disk's %+v", after, disk)
	}
}

// Q3 / S1: reads do not depend on writer health. While a push holds the tree
// lock (for far longer than any request should take), concurrent list and
// search requests answer promptly: from the snapshot while it is not stale,
// and UNKNOWN once it is — a stale snapshot is never served as known.
func TestWireTenantSnapshots_ReadersDoNotWaitForAPush(t *testing.T) {
	t.Parallel()
	const push = 4 * time.Second
	const bound = time.Second
	work, bare := snapshotRepo(t)
	w := gitops.NewWriter(work, work)
	d := &Deps{ConfigDir: work, RBAC: openModeRBAC(t), SearchCache: WireTenantSnapshots(w, work, true)}
	done := startHeldPR(t, w, bare, "sleep 4\n")

	type result struct {
		took    time.Duration
		state   *ConfigDerivedState
		raw     string
		loadErr string
		what    string
	}
	burst := func() []result {
		results := make(chan result, 10)
		for i := 0; i < 5; i++ {
			go func() {
				start := time.Now()
				rec := httptest.NewRecorder()
				ListTenants(d)(rec, httptest.NewRequest(http.MethodGet, "/api/v1/tenants", nil))
				var items []TenantSummary
				_ = json.Unmarshal(rec.Body.Bytes(), &items)
				r := result{took: time.Since(start), what: "list"}
				for _, it := range items {
					if it.ID == snapTenant {
						r.state, r.raw = it.ConfigDerived, it.SilentMode
					}
				}
				results <- r
			}()
			go func() {
				start := time.Now()
				rec := httptest.NewRecorder()
				SearchTenants(d)(rec, httptest.NewRequest(http.MethodGet, "/api/v1/tenants/search", nil))
				var resp SearchResponse
				_ = json.Unmarshal(rec.Body.Bytes(), &resp)
				r := result{took: time.Since(start), what: "search", loadErr: resp.ConfigDerivation.LoadError}
				for _, it := range resp.Items {
					if it.ID == snapTenant {
						r.state, r.raw = it.ConfigDerived, it.SilentMode
					}
				}
				results <- r
			}()
		}
		out := make([]result, 0, 10)
		for i := 0; i < 10; i++ {
			out = append(out, <-results)
		}
		return out
	}

	// Not stale: the snapshot predates no write, so it is served as known.
	for _, r := range burst() {
		if r.took > bound {
			t.Errorf("%s waited %v behind a %v push (bound %v)", r.what, r.took, push, bound)
		}
		if r.state == nil {
			t.Errorf("%s: %s unknown although the snapshot is not stale", r.what, snapTenant)
		} else if len(r.state.SilentTargets) != 0 {
			t.Errorf("%s served the unmerged proposal: %+v", r.what, *r.state)
		}
	}
	// Stale: must not be served as known while the write holds the tree.
	d.SearchCache.Invalidate()
	for _, r := range burst() {
		if r.took > bound {
			t.Errorf("stale %s waited %v behind a %v push (bound %v)", r.what, r.took, push, bound)
		}
		if r.state != nil || r.raw != "" {
			t.Errorf("stale %s served state as known: derived=%v raw=%q", r.what, r.state, r.raw)
		}
		if r.what == "search" && r.loadErr != writerBusyLoadError {
			t.Errorf("stale search load_error = %q, want %q", r.loadErr, writerBusyLoadError)
		}
	}
	if err := <-done; err != nil {
		t.Fatalf("WritePR: %v", err)
	}
	if got := derivedByID(t, getList(t, d)); !reflect.DeepEqual(got, derivedNow(t, work)) {
		t.Errorf("list after WritePR = %+v, want the disk's %+v", got, derivedNow(t, work))
	}
}

// Q3: with no snapshot at all, a request during a push gets the tenants with
// no derived state — promptly, and without the proposal's raw values.
func TestWireTenantSnapshots_NoSnapshotDuringAPushIsUnknown(t *testing.T) {
	t.Parallel()
	work, bare := snapshotRepo(t)
	w := gitops.NewWriter(work, work)
	d := &Deps{ConfigDir: work, RBAC: openModeRBAC(t), SearchCache: wireTenantSnapshots(w, true)} // not primed

	done := startHeldPR(t, w, bare, "sleep 3\n")
	start := time.Now()
	resp := openSearch(t, d)
	if took := time.Since(start); took > time.Second {
		t.Errorf("search waited %v behind the push", took)
	}
	for _, it := range resp.Items {
		if it.ConfigDerived != nil || it.SilentMode != "" {
			t.Errorf("unprimed search during a push derived or leaked raw state: %+v", it)
		}
	}
	if resp.ConfigDerivation.LoadError != writerBusyLoadError {
		t.Errorf("load_error = %q, want %q", resp.ConfigDerivation.LoadError, writerBusyLoadError)
	}
	if err := <-done; err != nil {
		t.Fatalf("WritePR: %v", err)
	}
}

// Q4: a PR write whose return to base fails leaves the unmerged proposal
// checked out after the lock is released. That tree must never be derived
// from or cached. The failure is injected by holding the clone's index.lock
// from the origin's pre-receive hook, so the post-push `reset --hard` fails.
func TestWireTenantSnapshots_TreeOffBaseIsNeverDerived(t *testing.T) {
	t.Parallel()
	work, bare := snapshotRepo(t)
	lock := filepath.Join(work, ".git", "index.lock")
	t.Cleanup(func() { _ = os.Remove(lock) })
	w := gitops.NewWriter(work, work)
	d := &Deps{ConfigDir: work, RBAC: openModeRBAC(t), SearchCache: WireTenantSnapshots(w, work, true)}

	done := startHeldPR(t, w, bare, "touch '"+lock+"'\n")
	if err := <-done; err != nil {
		t.Fatalf("WritePR: %v", err)
	}
	out, _ := exec.Command("git", "-C", work, "symbolic-ref", "--short", "HEAD").Output()
	if string(out) == "main\n" {
		t.Fatalf("precondition: the injected reset failure should leave the tree off base")
	}

	for i := 0; i < 2; i++ { // twice: the off-base answer must not be cached either
		got := listRaw(t, d)[snapTenant]
		if got.ConfigDerived != nil || got.SilentMode != "" {
			t.Errorf("request %d derived from or showed the off-base tree: %+v", i, got)
		}
		if msg := openSearch(t, d).ConfigDerivation.LoadError; !strings.Contains(msg, "config worktree is not on the base branch") {
			t.Errorf("unrestricted load_error = %q, want the factual not-on-base message", msg)
		}
	}
	scoped := newRBACManager(t, `groups:
  - name: readers
    tenants: ["*"]
    permissions: [read]
`)
	ds := &Deps{ConfigDir: work, RBAC: scoped, SearchCache: d.SearchCache}
	resp, _ := searchAs(t, ds, scoped.Middleware(rbac.PermRead, nil), func(r *http.Request) { r.Header.Set("X-Forwarded-Groups", "readers") })
	if resp.ConfigDerivation.LoadError != scopedLoadError {
		t.Errorf("scoped load_error = %q, want %q", resp.ConfigDerivation.LoadError, scopedLoadError)
	}
}

// holdNextLoad makes the next reload of c stop, under the tree lock and after
// it has read conf.d, until the returned release is called; loaded closes
// when it gets there. Later reloads run through.
func holdNextLoad(c *tenantSnapshotCache) (loaded <-chan struct{}, release func()) {
	reached := make(chan struct{})
	gate := make(chan struct{})
	var once sync.Once
	c.afterLoad = func() {
		first := false
		once.Do(func() { first = true })
		if first {
			close(reached)
			<-gate
		}
	}
	var released sync.Once
	return reached, func() { released.Do(func() { close(gate) }) }
}

func asyncList(d *Deps) <-chan []TenantSummary {
	out := make(chan []TenantSummary, 1)
	go func() {
		rec := httptest.NewRecorder()
		ListTenants(d)(rec, httptest.NewRequest(http.MethodGet, "/api/v1/tenants", nil))
		var items []TenantSummary
		_ = json.Unmarshal(rec.Body.Bytes(), &items)
		out <- items
	}()
	return out
}

func silentTargetsOf(items []TenantSummary, id string) ([]string, bool) {
	for _, it := range items {
		if it.ID == id && it.ConfigDerived != nil {
			return it.ConfigDerived.SilentTargets, true
		}
	}
	return nil, false
}

// R3: another request's reload is not a write. A request made right after
// Write returned, while a different request is reloading, waits for that
// reload instead of losing the tree lock to it and serving the old snapshot.
func TestTenantSnapshots_ReadAfterWriteWaitsForAnotherReadersReload(t *testing.T) {
	t.Parallel()
	work, _ := snapshotRepo(t)
	w := gitops.NewWriter(work, work)
	cache := WireTenantSnapshots(w, work, false)
	d := &Deps{ConfigDir: work, RBAC: openModeRBAC(t), SearchCache: cache}
	if _, err := w.Write(context.Background(), snapTenant, "alice@example.com", snapSilentYAML); err != nil {
		t.Fatalf("Write: %v", err)
	}
	loaded, release := holdNextLoad(cache)
	other := asyncList(d) // reloads, and holds the tree lock inside the load
	<-loaded
	mine := asyncList(d) // the writer's own read
	time.Sleep(100 * time.Millisecond)
	release()
	for name, ch := range map[string]<-chan []TenantSummary{"other reader": other, "writer's read": mine} {
		got, ok := silentTargetsOf(<-ch, snapTenant)
		if !ok || len(got) == 0 {
			t.Errorf("%s after Write returned: silent_targets=%v derived=%v, want the write", name, got, ok)
		}
	}
}

// R3: a cold cache under concurrent requests, with no write anywhere: every
// request gets derived state, and none is told a write is in progress.
func TestTenantSnapshots_ColdConcurrentReadersAllDerive(t *testing.T) {
	t.Parallel()
	work, _ := snapshotRepo(t)
	w := gitops.NewWriter(work, work)
	cache := wireTenantSnapshots(w, false) // not primed
	d := &Deps{ConfigDir: work, RBAC: openModeRBAC(t), SearchCache: cache}
	loaded, release := holdNextLoad(cache)
	first := asyncList(d)
	<-loaded
	var rest []<-chan []TenantSummary
	for i := 0; i < 3; i++ {
		rest = append(rest, asyncList(d))
	}
	searchDone := make(chan *SearchResponse, 1)
	go func() { searchDone <- openSearch(t, d) }()
	time.Sleep(100 * time.Millisecond)
	release()
	for i, ch := range append([]<-chan []TenantSummary{first}, rest...) {
		if _, ok := silentTargetsOf(<-ch, snapTenant); !ok {
			t.Errorf("reader %d on a cold cache got no derived state", i)
		}
	}
	if msg := (<-searchDone).ConfigDerivation.LoadError; msg != "" {
		t.Errorf("search on a cold cache with no write reported %q", msg)
	}
}

// R7a: a reload that read conf.d before an Invalidate and finished after it
// must leave the snapshot stale, so the next request reloads.
func TestTenantSnapshots_InvalidateDuringReloadLeavesItStale(t *testing.T) {
	t.Parallel()
	work, _ := snapshotRepo(t)
	w := gitops.NewWriter(work, work)
	cache := WireTenantSnapshots(w, work, false)
	d := &Deps{ConfigDir: work, RBAC: openModeRBAC(t), SearchCache: cache}
	cache.Invalidate() // make the next request reload
	loaded, release := holdNextLoad(cache)
	reloading := asyncList(d)
	<-loaded // it has read the plain tenant
	if err := os.WriteFile(filepath.Join(work, snapTenant+".yaml"), []byte(snapSilentYAML), 0o644); err != nil {
		t.Fatal(err)
	}
	cache.Invalidate()
	release()
	<-reloading
	if got, ok := silentTargetsOf(getList(t, d), snapTenant); !ok || len(got) == 0 {
		t.Errorf("request after an Invalidate that raced a reload served the reload's old result: %v", got)
	}
}

// R4 / R7b: in PR mode, only changes the loader would read make the tree
// untrustworthy.
func TestTenantSnapshots_PRModeTreeCheck(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name    string
		rel     string
		body    string
		derived bool
	}{
		{"hidden temp leftover", "." + snapTenant + ".yaml.tmp-1", "x", true},
		{"untracked config file", "t-new.yaml", "tenants:\n  t-new:\n    cpu: \"1\"\n", false},
		{"tracked modification", snapTenant + ".yaml", snapSilentYAML, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			work, _ := snapshotRepo(t)
			w := gitops.NewWriter(work, work)
			cache := WireTenantSnapshots(w, work, true)
			d := &Deps{ConfigDir: work, RBAC: openModeRBAC(t), SearchCache: cache}
			if err := os.WriteFile(filepath.Join(work, c.rel), []byte(c.body), 0o644); err != nil {
				t.Fatal(err)
			}
			cache.Invalidate()
			got, ok := silentTargetsOf(getList(t, d), snapTenant)
			if ok != c.derived {
				t.Fatalf("derived=%v (silent_targets=%v), want derived=%v", ok, got, c.derived)
			}
			if ok && len(got) != 0 {
				t.Errorf("derived state shows an uncommitted change: %v", got)
			}
		})
	}
}

// holdWrite makes the NEXT direct-mode commit in work hang in a pre-commit
// hook until release; started closes once the hook runs (the write then holds
// the tree lock).
func holdWrite(t *testing.T, work string) (started <-chan struct{}, release func()) {
	t.Helper()
	dir := t.TempDir()
	marker, gate := filepath.Join(dir, "started"), filepath.Join(dir, "gate")
	hook := "#!/bin/sh\ntouch '" + marker + "'\nwhile [ ! -e '" + gate + "' ]; do sleep 0.05; done\nrm -f \"$0\"\n"
	if err := os.WriteFile(filepath.Join(work, ".git", "hooks", "pre-commit"), []byte(hook), 0o755); err != nil {
		t.Fatal(err)
	}
	ch := make(chan struct{})
	go func() {
		for {
			if _, err := os.Stat(marker); err == nil {
				close(ch)
				return
			}
			time.Sleep(20 * time.Millisecond)
		}
	}()
	return ch, func() { _ = os.WriteFile(gate, nil, 0o644) }
}

// S1: write A returned (the snapshot is now older than A), write B holds the
// tree. The pre-A snapshot must not be served as known: A's silence is on
// disk, and "normal" would be a false statement.
func TestTenantSnapshots_StaleSnapshotIsNotKnownWhileAWriteRuns(t *testing.T) {
	t.Parallel()
	work, _ := snapshotRepo(t)
	w := gitops.NewWriter(work, work)
	d := &Deps{ConfigDir: work, RBAC: openModeRBAC(t), SearchCache: WireTenantSnapshots(w, work, false)}
	if _, err := w.Write(context.Background(), snapTenant, "alice@example.com", snapSilentYAML); err != nil {
		t.Fatalf("write A: %v", err)
	}
	started, release := holdWrite(t, work)
	writeB := make(chan error, 1)
	go func() {
		_, err := w.Write(context.Background(), "t-other", "alice@example.com", "tenants:\n  t-other:\n    cpu: \"1\"\n")
		writeB <- err
	}()
	<-started
	got := listRaw(t, d)[snapTenant]
	release()
	if err := <-writeB; err != nil {
		t.Fatalf("write B: %v", err)
	}
	if got.ConfigDerived != nil && len(got.ConfigDerived.SilentTargets) == 0 {
		t.Errorf("served the pre-A snapshot as known: %+v", got)
	}
	if got.ConfigDerived != nil {
		t.Errorf("write B holds the tree and the snapshot is stale; want unknown, got %+v", got)
	}
}

// S2: a request that times out waiting for another request's reload must not
// fall back to a stale snapshot either.
func TestTenantSnapshots_WaitTimeoutOnAStaleSnapshotIsUnknown(t *testing.T) {
	t.Parallel()
	work, _ := snapshotRepo(t)
	w := gitops.NewWriter(work, work)
	cache := WireTenantSnapshots(w, work, false)
	cache.loadDeadline = 10 * reloadWaitBound
	d := &Deps{ConfigDir: work, RBAC: openModeRBAC(t), SearchCache: cache}
	if _, err := w.Write(context.Background(), snapTenant, "alice@example.com", snapSilentYAML); err != nil {
		t.Fatalf("Write: %v", err)
	}
	loaded, release := holdNextLoad(cache)
	defer release()
	slow := asyncList(d) // the reload, held past reloadWaitBound
	<-loaded
	got := listRaw(t, d)[snapTenant] // waits reloadWaitBound, then answers
	if got.ConfigDerived != nil {
		t.Errorf("wait timeout served a stale snapshot as known: %+v", got)
	}
	release()
	<-slow
}

// S3: a panic in the load becomes an unknown answer, and the cache reloads
// normally afterwards (it used to keep `inflight` forever).
func TestTenantSnapshots_PanicInLoadIsRecovered(t *testing.T) {
	t.Parallel()
	work, _ := snapshotRepo(t)
	w := gitops.NewWriter(work, work)
	cache := wireTenantSnapshots(w, false) // not primed
	d := &Deps{ConfigDir: work, RBAC: openModeRBAC(t), SearchCache: cache}
	var once sync.Once
	cache.afterLoad = func() { once.Do(func() { panic("injected") }) }
	resp := openSearch(t, d)
	if resp.ConfigDerivation.LoadError != loadPanicError {
		t.Errorf("load_error after a panic = %q, want %q", resp.ConfigDerivation.LoadError, loadPanicError)
	}
	start := time.Now()
	if _, ok := silentTargetsOf(getList(t, d), snapTenant); !ok {
		t.Error("the cache did not reload after a panicked load")
	}
	if took := time.Since(start); took > reloadWaitBound {
		t.Errorf("the next request waited %v: the panicked reload was left in flight", took)
	}
}

// S3: the same when the panic happens outside the load goroutine (here: in
// the tree-lock call itself).
func TestTenantSnapshots_PanicInReloadIsRecovered(t *testing.T) {
	t.Parallel()
	work, _ := snapshotRepo(t)
	w := gitops.NewWriter(work, work)
	cache := wireTenantSnapshots(w, false)
	d := &Deps{ConfigDir: work, RBAC: openModeRBAC(t), SearchCache: cache}
	cache.tryReadTree = func(func()) bool { panic("injected") }
	if resp := openSearch(t, d); resp.ConfigDerivation.LoadError != loadPanicError {
		t.Errorf("load_error = %q, want %q", resp.ConfigDerivation.LoadError, loadPanicError)
	}
	cache.tryReadTree = w.TryWithTreeLock
	if _, ok := silentTargetsOf(getList(t, d), snapTenant); !ok {
		t.Error("the cache did not reload after a panicked reload")
	}
}

// S6: a request waiting for another request's reload gives up when its own
// context ends.
func TestTenantSnapshots_WaitHonoursTheRequestContext(t *testing.T) {
	t.Parallel()
	work, _ := snapshotRepo(t)
	w := gitops.NewWriter(work, work)
	cache := wireTenantSnapshots(w, false) // cold: no snapshot to fall back on
	cache.loadDeadline = time.Minute
	loaded, release := holdNextLoad(cache)
	defer release()
	first := make(chan struct{})
	go func() {
		_, _ = cache.snapshot(context.Background(), work)
		close(first)
	}()
	<-loaded
	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()
	start := time.Now()
	_, err := cache.snapshot(ctx, work)
	if took := time.Since(start); took > time.Second {
		t.Errorf("waiter ignored its context for %v", took)
	}
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Errorf("err = %v, want the context's", err)
	}
	release()
	<-first
}

// S10: a request that saw an Invalidate does not take the result of a reload
// that started before it.
func TestTenantSnapshots_JoinerSkipsAReloadOlderThanItsInvalidate(t *testing.T) {
	t.Parallel()
	work, _ := snapshotRepo(t)
	w := gitops.NewWriter(work, work)
	cache := WireTenantSnapshots(w, work, false)
	d := &Deps{ConfigDir: work, RBAC: openModeRBAC(t), SearchCache: cache}
	cache.Invalidate()
	loaded, release := holdNextLoad(cache)
	older := asyncList(d)
	<-loaded // the older reload has read the plain tenant
	if err := os.WriteFile(filepath.Join(work, snapTenant+".yaml"), []byte(snapSilentYAML), 0o644); err != nil {
		t.Fatal(err)
	}
	cache.Invalidate()
	joiner := asyncList(d) // arrives while the older reload is in flight
	time.Sleep(100 * time.Millisecond)
	release()
	<-older
	if got, ok := silentTargetsOf(<-joiner, snapTenant); !ok || len(got) == 0 {
		t.Errorf("joiner took the older reload's result: silent_targets=%v derived=%v", got, ok)
	}
}
