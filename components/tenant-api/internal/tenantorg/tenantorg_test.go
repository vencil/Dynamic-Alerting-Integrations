package tenantorg

import (
	"os"
	"reflect"
	"strings"
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

// parseTreeRefused are _tenant_orgs.yaml texts yaml.v3's strict decode reads
// (the first document; a `!!binary` or `!` value as a string) and parse-tree
// refuses (ADR-036 §7 step 7, #3206). The org boundary is not read from them:
// each is a load error, never a mapping that lost or changed orgs.
var parseTreeRefused = []struct{ name, yaml, kind string }{
	{"second document", "tenant_orgs:\n  t1: [ORG-1]\n---\ntenant_orgs:\n  t2: [ORG-2]\n", "multiple_documents"},
	{"trailing empty document", "tenant_orgs:\n  t1: [ORG-1]\n---\n", "multiple_documents"},
	{"!!binary org", "tenant_orgs:\n  t1: [!!binary T1JHLTE=]\n", "tag_not_allowed"},
	{"non-specific tag", "tenant_orgs:\n  t1: [! \"ORG-1\"]\n", "non_specific_tag"},
}

// The initial load fails (main.go: FATAL) and the tree read errors; neither
// is the empty mapping.
func TestNewManager_ParseTreeRefusalIsALoadError(t *testing.T) {
	t.Parallel()
	for _, c := range parseTreeRefused {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			dir, _ := testutil.MkTempYAML(t, "_tenant_orgs.yaml", c.yaml)
			if _, err := NewManager(dir); err == nil || !strings.Contains(err.Error(), "kind "+c.kind) {
				t.Fatalf("NewManager error = %v, want a load error of kind %s", err, c.kind)
			}
			if orgs, err := OrgsInTree(dir, "t1"); err == nil || !strings.Contains(err.Error(), "kind "+c.kind) {
				t.Fatalf("OrgsInTree = %v, %v; want an error of kind %s", orgs, err, c.kind)
			}
		})
	}
}

// A hot reload onto such a file keeps the last good mapping.
func TestReload_ParseTreeRefusalKeepsLastGood(t *testing.T) {
	t.Parallel()
	for _, c := range parseTreeRefused {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			dir, path := testutil.MkTempYAML(t, "_tenant_orgs.yaml", "tenant_orgs:\n  t1: [ORG-9]\n")
			m, err := NewManager(dir)
			if err != nil {
				t.Fatalf("NewManager: %v", err)
			}
			if err := os.WriteFile(path, []byte(c.yaml), 0o600); err != nil {
				t.Fatal(err)
			}
			if err := m.Reload(); err == nil || !strings.Contains(err.Error(), "kind "+c.kind) {
				t.Fatalf("Reload = %v, want an error of kind %s", err, c.kind)
			}
			if orgs, _ := m.OrgsForTenant("t1"); !reflect.DeepEqual(orgs, []string{"ORG-9"}) {
				t.Errorf("after the refused reload t1's orgs = %v, want the last good [ORG-9]", orgs)
			}
		})
	}
}
