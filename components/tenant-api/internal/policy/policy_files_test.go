package policy

// #2486 item 2: the domain policies come from `_domain_policy.yaml` AND
// `_domain_policy.yml`, as the route generator (_grar_parse._POLICY_FILENAMES)
// and routingpolicy.LoadRoot read them — measured on both: a `.yml` alone is
// read; two files add up; a domain both declare is the `.yml`'s, whole (name
// order, the later file wins). The watcher follows both files, and
// LoadSnapshot (PR mode's fresh-base read) reads both.

import (
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/testutil"
)

func forbidding(domain, tenant, receiverType string) string {
	return "domain_policies:\n  " + domain + ":\n    tenants: [" + tenant + "]\n" +
		"    constraints:\n      forbidden_receiver_types: [" + receiverType + "]\n"
}

// forbiddenByDomain: domain → its forbidden_receiver_types.
func forbiddenByDomain(m *Manager) map[string][]string {
	out := map[string][]string{}
	for name, dp := range m.Get().DomainPolicies {
		out[name] = dp.Constraints.ForbiddenReceiverTypes
	}
	return out
}

func domainNames(m *Manager) []string {
	var out []string
	for name := range m.Get().DomainPolicies {
		out = append(out, name)
	}
	sort.Strings(out)
	return out
}

func TestPolicyFiles_Spellings(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name  string
		files map[string]string
		want  map[string][]string
	}{
		{"yml only", map[string]string{"_domain_policy.yml": forbidding("fin", "t-s", "slack")},
			map[string][]string{"fin": {"slack"}}},
		{"both, different domains add up", map[string]string{
			"_domain_policy.yaml": forbidding("da", "t-s", "slack"),
			"_domain_policy.yml":  forbidding("db", "t-w", "webhook")},
			map[string][]string{"da": {"slack"}, "db": {"webhook"}}},
		{"both, one domain: the .yml's, whole", map[string]string{
			"_domain_policy.yaml": forbidding("fin", "t-s", "slack"),
			"_domain_policy.yml":  forbidding("fin", "t-w", "webhook")},
			map[string][]string{"fin": {"webhook"}}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			for name, content := range tc.files {
				testutil.WriteYAML(t, dir, name, content)
			}
			snap, err := LoadSnapshot(dir)
			if err != nil {
				t.Fatalf("LoadSnapshot: %v", err)
			}
			for label, m := range map[string]*Manager{"watcher": NewManager(dir), "snapshot": snap} {
				if got := forbiddenByDomain(m); !reflect.DeepEqual(got, tc.want) {
					t.Errorf("%s: policies = %v, want %v", label, got, tc.want)
				}
			}
			if tc.name == "both, one domain: the .yml's, whole" {
				// Whole: the tenants list is the .yml's too.
				if got := snap.Get().DomainPolicies["fin"].Tenants; !reflect.DeepEqual(got, []string{"t-w"}) {
					t.Errorf("fin tenants = %v, want the .yml's [t-w]", got)
				}
			}
		})
	}
}

// One file that does not parse: LoadSnapshot refuses (fail-closed, as for a
// single file); the watcher keeps its last good snapshot of BOTH.
func TestPolicyFiles_OneBroken(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	testutil.WriteYAML(t, dir, "_domain_policy.yaml", forbidding("da", "t-s", "slack"))
	testutil.WriteYAML(t, dir, "_domain_policy.yml", forbidding("db", "t-w", "webhook"))
	m := NewManager(dir)
	testutil.WriteYAML(t, dir, "_domain_policy.yml", "domain_policies: [unclosed\n")
	if _, err := LoadSnapshot(dir); err == nil {
		t.Error("LoadSnapshot accepted a broken _domain_policy.yml")
	}
	if err := m.Reload(); err == nil {
		t.Error("Reload accepted a broken _domain_policy.yml")
	}
	if got := domainNames(m); !reflect.DeepEqual(got, []string{"da", "db"}) {
		t.Errorf("after a failed reload the watcher serves %v, want the last good [da db]", got)
	}
}

// #2486 review B1: each file is parsed on its own. A file broken at startup
// is skipped and the other applies — as the route generator, which drops
// the broken file and enforces the other (`--strict` rc 1). A whole-read
// failure left the watcher empty: the sound `.yaml` unenforced.
func TestPolicyFiles_BrokenAtStartupIsSkipped(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	testutil.WriteYAML(t, dir, "_domain_policy.yaml", forbidding("da", "t-s", "slack"))
	testutil.WriteYAML(t, dir, "_domain_policy.yml", "domain_policies: [unclosed\n")
	m := NewManager(dir)
	if got := forbiddenByDomain(m); !reflect.DeepEqual(got, map[string][]string{"da": {"slack"}}) {
		t.Errorf("watcher serves %v, want the .yaml alone", got)
	}
	if err := m.Reload(); err == nil || !strings.Contains(err.Error(), "_domain_policy.yml") {
		t.Errorf("Reload err = %v, want a failure naming _domain_policy.yml", err)
	}
	// Repaired: the two merge again.
	testutil.WriteYAML(t, dir, "_domain_policy.yml", forbidding("db", "t-w", "webhook"))
	if err := m.Reload(); err != nil {
		t.Fatalf("Reload after repair: %v", err)
	}
	if got := domainNames(m); !reflect.DeepEqual(got, []string{"da", "db"}) {
		t.Errorf("after repair the watcher serves %v, want [da db]", got)
	}
}

// On a hot reload only the broken file keeps its last good content; the
// other file's update still takes effect.
func TestPolicyFiles_BrokenOnReloadKeepsOnlyItsOwnLastGood(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	testutil.WriteYAML(t, dir, "_domain_policy.yaml", forbidding("da", "t-s", "slack"))
	testutil.WriteYAML(t, dir, "_domain_policy.yml", forbidding("db", "t-w", "webhook"))
	m := NewManager(dir)
	testutil.WriteYAML(t, dir, "_domain_policy.yaml", "domain_policies: [unclosed\n")
	testutil.WriteYAML(t, dir, "_domain_policy.yml", forbidding("dc", "t-w", "email"))
	if err := m.Reload(); err == nil || !strings.Contains(err.Error(), "_domain_policy.yaml") {
		t.Errorf("Reload err = %v, want a failure naming _domain_policy.yaml", err)
	}
	want := map[string][]string{"da": {"slack"}, "dc": {"email"}}
	if got := forbiddenByDomain(m); !reflect.DeepEqual(got, want) {
		t.Errorf("watcher serves %v, want %v (.yaml's last good + the .yml's update)", got, want)
	}
	testutil.WriteYAML(t, dir, "_domain_policy.yaml", forbidding("dz", "t-s", "slack"))
	if err := m.Reload(); err != nil {
		t.Fatalf("Reload after repair: %v", err)
	}
	if got := domainNames(m); !reflect.DeepEqual(got, []string{"dc", "dz"}) {
		t.Errorf("after repair the watcher serves %v, want [dc dz]", got)
	}
}

// Both files hot-reload through the one watcher: a `.yml` created, changed
// and removed after start is picked up, as is a `.yaml` change beside it.
func TestPolicyFiles_WatchLoopFollowsBoth(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	m := NewManager(dir)
	stop := make(chan struct{})
	defer close(stop)
	go m.WatchLoop(10*time.Millisecond, stop)

	waitFor := func(step string, want []string) {
		t.Helper()
		deadline := time.Now().Add(3 * time.Second)
		for time.Now().Before(deadline) {
			if reflect.DeepEqual(domainNames(m), want) {
				return
			}
			time.Sleep(5 * time.Millisecond)
		}
		t.Fatalf("%s: watcher serves %v, want %v", step, domainNames(m), want)
	}
	testutil.WriteYAML(t, dir, "_domain_policy.yml", forbidding("db", "t-w", "webhook"))
	waitFor(".yml created", []string{"db"})
	testutil.WriteYAML(t, dir, "_domain_policy.yaml", forbidding("da", "t-s", "slack"))
	waitFor(".yaml created beside it", []string{"da", "db"})
	testutil.WriteYAML(t, dir, "_domain_policy.yml", forbidding("dc", "t-w", "webhook"))
	waitFor(".yml changed", []string{"da", "dc"})
	if err := os.Remove(filepath.Join(dir, "_domain_policy.yml")); err != nil {
		t.Fatal(err)
	}
	waitFor(".yml removed", []string{"da"})
	if err := os.Remove(filepath.Join(dir, "_domain_policy.yaml")); err != nil {
		t.Fatal(err)
	}
	waitFor("both removed", nil)
}
