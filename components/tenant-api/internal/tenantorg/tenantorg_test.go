package tenantorg

import (
	"os"
	"reflect"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/testutil"
)

const sample1N = `tenant_orgs:
  db-a: [ORG-4821]
  db-b: [ORG-4821, ORG-1900]
  db-c: []
`

// #7: load 1:N + empty list + known-vs-unknown lookups.
func TestNewManager_Loads1NAndLookups(t *testing.T) {
	t.Parallel()
	dir, _ := testutil.MkTempYAML(t, "_tenant_orgs.yaml", sample1N)
	m, err := NewManager(dir)
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}

	cases := []struct {
		tenant    string
		wantOrgs  []string
		wantKnown bool
	}{
		{"db-a", []string{"ORG-4821"}, true},
		{"db-b", []string{"ORG-4821", "ORG-1900"}, true},
		{"db-c", []string{}, true}, // labeled but empty (created-but-unassigned)
		{"db-unknown", nil, false}, // absent from the map entirely
	}
	for _, c := range cases {
		orgs, known := m.OrgsForTenant(c.tenant)
		if known != c.wantKnown {
			t.Errorf("OrgsForTenant(%q) known = %v, want %v", c.tenant, known, c.wantKnown)
		}
		if !reflect.DeepEqual(orgs, c.wantOrgs) {
			t.Errorf("OrgsForTenant(%q) orgs = %v, want %v", c.tenant, orgs, c.wantOrgs)
		}
	}

	// known-but-empty and unknown are distinguishable (P6 reverse-lookup needs it)
	// yet both carry zero orgs (org-scope denies both, shadow-lenient).
	if orgs, _ := m.OrgsForTenant("db-c"); len(orgs) != 0 {
		t.Errorf("db-c must have zero orgs, got %v", orgs)
	}
}

// #7: no file → empty config (the benign default for a deployment that does not
// use org-scope); every tenant reports unknown.
func TestNewManager_NoFile(t *testing.T) {
	t.Parallel()
	m, err := NewManager(t.TempDir())
	if err != nil {
		t.Fatalf("NewManager(no file): %v", err)
	}
	if orgs, known := m.OrgsForTenant("db-a"); known || orgs != nil {
		t.Errorf("no-file manager: OrgsForTenant = (%v, %v), want (nil, false)", orgs, known)
	}
}

// #7: empty file and comment-only file both decode to the empty config (io.EOF
// special-case), NOT a load error.
func TestNewManager_EmptyAndCommentOnly(t *testing.T) {
	t.Parallel()
	for name, body := range map[string]string{
		"empty":        "",
		"comment-only": "# only a comment, no keys\n",
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			dir, _ := testutil.MkTempYAML(t, "_tenant_orgs.yaml", body)
			m, err := NewManager(dir)
			if err != nil {
				t.Fatalf("NewManager(%s): %v", name, err)
			}
			if _, known := m.OrgsForTenant("db-a"); known {
				t.Errorf("%s: expected empty config (no known tenants)", name)
			}
		})
	}
}

// #7: strict KnownFields — a typo'd top-level key or a wrong value type is a
// load error, never a silently-empty map (org boundary must fail loud).
func TestNewManager_StrictParseRejectsTypo(t *testing.T) {
	t.Parallel()
	bad := map[string]string{
		"typo top-level key":     "tenant_org:\n  db-a: [ORG-1]\n", // tenant_org vs tenant_orgs
		"unknown extra key":      "tenant_orgs:\n  db-a: [ORG-1]\nfoo: 1\n",
		"scalar instead of list": "tenant_orgs:\n  db-a: ORG-1\n", // value must be a list
	}
	for name, body := range bad {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			dir, _ := testutil.MkTempYAML(t, "_tenant_orgs.yaml", body)
			if _, err := NewManager(dir); err == nil {
				t.Errorf("NewManager(%s) = nil error, want a strict-parse load error", name)
			}
		})
	}
}

// #7: hot-reload keeps the last-good snapshot when the new file is invalid.
func TestReload_InvalidKeepsLastGood(t *testing.T) {
	t.Parallel()
	dir, path := testutil.MkTempYAML(t, "_tenant_orgs.yaml", sample1N)
	m, err := NewManager(dir)
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}
	if _, known := m.OrgsForTenant("db-a"); !known {
		t.Fatal("precondition: db-a must be known from initial load")
	}

	if err := os.WriteFile(path, []byte("tenant_org:\n  db-z: [ORG-9]\n"), 0o600); err != nil {
		t.Fatalf("write bad config: %v", err)
	}
	if err := m.Reload(); err == nil {
		t.Fatal("Reload = nil, want an error for the typo'd config")
	}
	// Last-good still served: db-a still known, the bad file's db-z not present.
	if _, known := m.OrgsForTenant("db-a"); !known {
		t.Error("after failed reload: db-a must still be known (last-good served)")
	}
	if _, known := m.OrgsForTenant("db-z"); known {
		t.Error("after failed reload: db-z from the rejected file must NOT be visible")
	}
}

// #7: hot-reload picks up a valid change.
func TestWatchLoop_PicksUpValidChange(t *testing.T) {
	t.Parallel()
	dir, _ := testutil.MkTempYAML(t, "_tenant_orgs.yaml", "tenant_orgs:\n  db-a: [ORG-1]\n")
	m, err := NewManager(dir)
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}
	stopCh := make(chan struct{})
	defer close(stopCh)
	go m.WatchLoop(50*time.Millisecond, stopCh)

	testutil.WriteYAML(t, dir, "_tenant_orgs.yaml", "tenant_orgs:\n  db-a: [ORG-1]\n  db-b: [ORG-2]\n")

	deadline := time.NewTimer(2 * time.Second)
	defer deadline.Stop()
	tick := time.NewTicker(5 * time.Millisecond)
	defer tick.Stop()
	for {
		select {
		case <-deadline.C:
			t.Fatal("WatchLoop did not pick up the org-map update within 2s")
		case <-tick.C:
			if _, known := m.OrgsForTenant("db-b"); known {
				return
			}
		}
	}
}

// A nil manager (an unwired Deps.TenantOrg) is safe to call.
func TestOrgsForTenant_NilReceiver(t *testing.T) {
	t.Parallel()
	var m *Manager
	if orgs, known := m.OrgsForTenant("db-a"); known || orgs != nil {
		t.Errorf("nil-receiver OrgsForTenant = (%v, %v), want (nil, false)", orgs, known)
	}
}

// NewForTest builds an in-memory manager without disk I/O.
func TestNewForTest(t *testing.T) {
	t.Parallel()
	m := NewForTest(&Config{TenantOrgs: map[string][]string{"db-a": {"ORG-1"}}})
	if orgs, known := m.OrgsForTenant("db-a"); !known || !reflect.DeepEqual(orgs, []string{"ORG-1"}) {
		t.Errorf("NewForTest OrgsForTenant(db-a) = (%v, %v)", orgs, known)
	}
}
