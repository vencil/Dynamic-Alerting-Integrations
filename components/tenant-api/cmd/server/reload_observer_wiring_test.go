package main

import (
	"reflect"
	"sort"
	"sync"
	"testing"

	"github.com/vencil/tenant-api/internal/federation/fedpolicy"
	"github.com/vencil/tenant-api/internal/groups"
	"github.com/vencil/tenant-api/internal/handler"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/statestore"
	"github.com/vencil/tenant-api/internal/tenantorg"
	"github.com/vencil/tenant-api/internal/views"
)

// recordingObserver collects the component label of every RecordReload call.
// SetReloadObserver replays the initial-load outcome on install, so wiring alone
// produces exactly one call per wired manager.
type recordingObserver struct {
	mu     sync.Mutex
	labels map[string]bool
}

func (r *recordingObserver) RecordReload(component string, ok bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.labels[component] = ok
}

// newWiringManagers builds the five config managers with the same constructors
// main uses (not NewForTest: rbac's test constructor carries a different label),
// against an empty conf.d and an empty state store.
func newWiringManagers(t *testing.T) (*rbac.Manager, *tenantorg.Manager, *fedpolicy.Manager, *groups.Manager, *views.Manager) {
	t.Helper()
	store, err := statestore.NewDir(t.TempDir(), nil)
	if err != nil {
		t.Fatal(err)
	}
	rbacMgr, err := rbac.NewManager("", nil)
	if err != nil {
		t.Fatal(err)
	}
	tenantOrgMgr, err := tenantorg.NewManager(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	return rbacMgr, tenantOrgMgr, fedpolicy.NewManager(store), groups.NewManager(store), views.NewManager(store)
}

// #1073: the set of components main wires to the reload observer must equal the
// set the metrics are emitted for. A component the metrics list but main does
// not wire would keep its last-reload-successful gauge at the starting 0 for
// ever (TenantApiConfigReloadStale fires on a healthy manager); before the
// starting value was 0 it sat at 1 and the alert could never fire for it at all.
// Removing any one SetReloadObserver line from wireConfigReloadObserver makes
// this test fail.
//
// What it does NOT cover:
//   - main not calling wireConfigReloadObserver at all (this test calls the
//     function directly, not main);
//   - a new manager that is neither passed to wireConfigReloadObserver nor
//     listed in configReloadComponents: both sides stay equal, and RecordReload
//     ignores an unknown component label, so its outcomes are silently dropped.
//     handler's TestConfigReloadComponents_CoversEveryWatcherLabel guards the
//     list against the configwatcher.New call sites instead.
func TestWireConfigReloadObserver_CoversEveryComponent(t *testing.T) {
	t.Parallel()
	rbacMgr, tenantOrgMgr, fedPolicyMgr, groupMgr, viewMgr := newWiringManagers(t)
	obs := &recordingObserver{labels: map[string]bool{}}

	wireConfigReloadObserver(obs, rbacMgr, tenantOrgMgr, fedPolicyMgr, groupMgr, viewMgr)

	wired := make([]string, 0, len(obs.labels))
	for c, ok := range obs.labels {
		wired = append(wired, c)
		if !ok {
			t.Errorf("component %q replayed a failed initial load on empty fixtures", c)
		}
	}
	sort.Strings(wired)
	want := handler.ConfigReloadComponents()
	if !reflect.DeepEqual(wired, want) {
		t.Fatalf("wireConfigReloadObserver wires %v, but the reload metrics are emitted for %v — "+
			"an unwired component's last-reload-successful gauge never leaves 0", wired, want)
	}
}

// End to end with the real store: after main's wiring every gauge reads 1 on
// healthy managers. Pairs with the handler-side test that pins an unwired
// component at 0.
func TestWireConfigReloadObserver_HealthyManagersReadOne(t *testing.T) {
	// NOT t.Parallel: NewConfigReloadObserver installs the package-level store
	// /metrics renders.
	rbacMgr, tenantOrgMgr, fedPolicyMgr, groupMgr, viewMgr := newWiringManagers(t)
	obs := handler.NewConfigReloadObserver()

	wireConfigReloadObserver(obs, rbacMgr, tenantOrgMgr, fedPolicyMgr, groupMgr, viewMgr)

	snap := obs.(*handler.ConfigReloadMetrics).StateSnapshot()
	for _, c := range handler.ConfigReloadComponents() {
		if snap[c] != 1 {
			t.Errorf("last_reload_successful{component=%q} = %d after wiring, want 1", c, snap[c])
		}
	}
}
