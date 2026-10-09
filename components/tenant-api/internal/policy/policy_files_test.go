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
	"sync"
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
				if got := []string(snap.Get().DomainPolicies["fin"].Tenants); !reflect.DeepEqual(got, []string{"t-w"}) {
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

// loadCounter is a ReloadObserver that counts the watcher's loads, so a test
// driving the real WatchLoop can wait for a load that began after a write.
type loadCounter struct {
	mu sync.Mutex
	n  int
}

func (c *loadCounter) RecordReload(string, bool) {
	c.mu.Lock()
	c.n++
	c.mu.Unlock()
}

func (c *loadCounter) count() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.n
}

// watchSteps starts dir's watcher on its own WatchLoop and returns a step
// function: it puts the two policy files in the given state ("" = absent)
// and returns once two more loads have run — the second began after the
// write, so the watcher has seen it. Reload is not used: it clears the
// dedup hash, which is exactly what these sequences must not get for free.
func watchSteps(t *testing.T, dir string) (*Manager, func(yaml, yml string)) {
	t.Helper()
	m := NewManager(dir)
	lc := &loadCounter{}
	m.SetReloadObserver(lc)
	stop := make(chan struct{})
	t.Cleanup(func() { close(stop) })
	go m.WatchLoop(5*time.Millisecond, stop)
	put := func(name, content string) {
		if content == "" {
			if err := os.Remove(filepath.Join(dir, name)); err != nil && !os.IsNotExist(err) {
				t.Fatal(err)
			}
			return
		}
		testutil.WriteYAML(t, dir, name, content)
	}
	step := func(yaml, yml string) {
		t.Helper()
		put("_domain_policy.yaml", yaml)
		put("_domain_policy.yml", yml)
		target := lc.count() + 2
		deadline := time.Now().Add(3 * time.Second)
		for lc.count() < target {
			if time.Now().After(deadline) {
				t.Fatalf("watcher ran %d loads in 3s, want %d", lc.count(), target)
			}
			time.Sleep(time.Millisecond)
		}
	}
	return m, step
}

// #2486: lastHash may only name bytes the stored snapshot is the full parse
// of. A partial snapshot (one file broken) or the empty one (both files
// gone) left it on the last good bytes, so those bytes coming back — a git
// revert — were deduped and the watcher stayed on the partial or empty
// snapshot (fail-open). Driven through WatchLoop: Reload clears the hash.
func TestPolicyFiles_RevertAfterNonFullStoreIsReparsed(t *testing.T) {
	t.Parallel()
	const broken = "domain_policies: [unclosed\n"
	g1 := forbidding("da", "t-s", "slack")
	g1b := forbidding("db", "t-w", "webhook")
	g2 := forbidding("dc", "t-w", "email")
	cases := []struct {
		name  string
		steps [][2]string // (.yaml, .yml); the first is the startup state
		want  []string
	}{
		{"partial empty snapshot, then revert",
			[][2]string{{g1, ""}, {"", broken}, {g1, ""}}, []string{"da"}},
		{"partial snapshot, then revert",
			[][2]string{{g1, g1b}, {broken, g2}, {g1, g1b}}, []string{"da", "db"}},
		{"both removed, then revert",
			[][2]string{{g1, ""}, {"", ""}, {g1, ""}}, []string{"da"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			first := tc.steps[0]
			for name, content := range map[string]string{"_domain_policy.yaml": first[0], "_domain_policy.yml": first[1]} {
				if content != "" {
					testutil.WriteYAML(t, dir, name, content)
				}
			}
			m, step := watchSteps(t, dir)
			for _, s := range tc.steps[1:] {
				step(s[0], s[1])
			}
			if got := domainNames(m); !reflect.DeepEqual(got, tc.want) {
				t.Errorf("watcher serves %v, want %v", got, tc.want)
			}
		})
	}
}

// A removed file forgets its last good content: broken again later, it
// contributes nothing (its last good is from before the removal). Both
// forgetting paths: the other file still present (parse's delete) and
// neither file present (the reader's clear).
func TestPolicyFiles_RemovedFileForgetsLastGood(t *testing.T) {
	t.Parallel()
	const broken = "domain_policies: [unclosed\n"
	g1 := forbidding("da", "t-s", "slack")
	for _, tc := range []struct {
		name  string
		other string // the .yml throughout
		want  []string
	}{
		{"other file absent", "", nil},
		{"other file present", forbidding("db", "t-w", "webhook"), []string{"db"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			testutil.WriteYAML(t, dir, "_domain_policy.yaml", g1)
			if tc.other != "" {
				testutil.WriteYAML(t, dir, "_domain_policy.yml", tc.other)
			}
			m, step := watchSteps(t, dir)
			step("", tc.other)
			step(broken, tc.other)
			if got := domainNames(m); !reflect.DeepEqual(got, tc.want) {
				t.Errorf("watcher serves %v, want %v (the removed file's old content must not return)", got, tc.want)
			}
		})
	}
}

// #2659: `domain_policies:` null, `~` or bare read as an empty policy here
// while da-guard and the generator's --strict report domain_policy_unusable;
// it is an error now (routingpolicy.DomainPoliciesShapeError — a list or a
// scalar there already was). An empty mapping, an empty document and a file
// without the key stay usable.
func TestParseConfig_DomainPoliciesNotAMapping(t *testing.T) {
	t.Parallel()
	for _, src := range []string{"domain_policies: null\n", "domain_policies: ~\n", "domain_policies:\n",
		"domain_policies: []\n", "domain_policies: x\n"} {
		if cfg, err := parseConfig([]byte(src)); err == nil {
			t.Errorf("parseConfig(%q) = %+v, nil; want the file refused", src, cfg)
		}
	}
	for _, src := range []string{"domain_policies: {}\n", "", "# nothing\n", "other: 1\n"} {
		if cfg, err := parseConfig([]byte(src)); err != nil || len(cfg.DomainPolicies) != 0 {
			t.Errorf("parseConfig(%q) = %+v, %v; want an empty policy", src, cfg, err)
		}
	}
}

// #2677: a key the route generator merges and this package's yaml.v3 decode
// reads as a plain key — `!!merge q:`, an alias naming an anchored `<<` —
// anywhere in the file, so the two can read different policies (here: none
// at all, or none for fin, while the generator forbids slack for t1). The
// file is refused, whichever way the generator reads it; so is one the
// generator drops whole (a `<<` written as a value).
func TestParseConfig_TaggedMergeKeyIsRefused(t *testing.T) {
	t.Parallel()
	const fin = "{domain_policies: {fin: {tenants: [t1], constraints: {forbidden_receiver_types: [slack]}}}}"
	for _, src := range []string{
		"!!merge q: {domain_policies: null}\n<<: " + fin + "\n", // generator: fin
		"<<: {domain_policies: null}\n!!merge q: " + fin + "\n", // generator: fin
		"<<: " + fin + "\n!!merge q: {domain_policies: null}\n", // generator: unusable
		// under domain_policies, in a domain, in its constraints, an alias value
		"domain_policies:\n  !!merge q: {fin: {tenants: [t1], constraints: {forbidden_receiver_types: [slack]}}}\n",
		"domain_policies: {fin: {!!merge q: {tenants: [t1], constraints: {forbidden_receiver_types: [slack]}}}}\n",
		"domain_policies: {fin: {tenants: [t1], constraints: {!!merge q: {forbidden_receiver_types: [slack]}}}}\n",
		"x: &b {tenants: [t1], constraints: {forbidden_receiver_types: [slack]}}\n" +
			"domain_policies: {fin: {!!merge q: *b}}\n",
		// B1c: an alias key naming an anchored `<<` — a merge to the
		// generator, a plain "<<" key to yaml.v3 (in a domain, at the top,
		// in constraints, under domain_policies)
		"x: {&m <<: {}}\ndomain_policies:\n  fin:\n    tenants: [t1]\n    *m : {constraints: {forbidden_receiver_types: [slack]}}\n",
		"x: {&m <<: {}}\n*m : {domain_policies: {fin: {tenants: [t1], constraints: {forbidden_receiver_types: [slack]}}}}\n",
		"x: {&m <<: {}}\ndomain_policies:\n  fin:\n    tenants: [t1]\n    constraints:\n      *m : {forbidden_receiver_types: [slack]}\n",
		"x: {&m !!merge <<: {}}\ndomain_policies:\n  *m : {fin: {tenants: [t1], constraints: {forbidden_receiver_types: [slack]}}}\n",
		// `<<` written as a value: the generator has no constructor for it
		// and drops the file; yaml.v3 reads the string "<<"
		"x: [<<]\ndomain_policies: {fin: {tenants: [t1], constraints: {forbidden_receiver_types: [slack]}}}\n",
		// so is a merge value it will not merge, or a key it counts twice,
		// under a key the policy struct never reads
		"x: {<<: 5}\ndomain_policies: {fin: {tenants: [t1], constraints: {forbidden_receiver_types: [slack]}}}\n",
		"x: {a: 1, a: 2}\ndomain_policies: {fin: {tenants: [t1], constraints: {forbidden_receiver_types: [slack]}}}\n",
		// require_critical_escalation anywhere, or anchored and aliased
		// (N1); in place under domain_policies.<d>.constraints too (B1,
		// hub #2486 PR-7c round 2: #2325 read only that place leniently)
		"domain_policies: {fin: {tenants: [t1], constraints: {require_critical_escalation: <<, forbidden_receiver_types: [slack]}}}\n",
		"domain_policies: {fin: {tenants: [t1], constraints: {require_critical_escalation: {<<: 5}, forbidden_receiver_types: [slack]}}}\n",
		"x: {require_critical_escalation: <<}\ndomain_policies: {fin: {tenants: [t1], constraints: {forbidden_receiver_types: [slack]}}}\n",
		"require_critical_escalation: {<<: 5}\ndomain_policies: {fin: {tenants: [t1], constraints: {forbidden_receiver_types: [slack]}}}\n",
		"domain_policies: {fin: {tenants: [t1], require_critical_escalation: <<, constraints: {forbidden_receiver_types: [slack]}}}\n",
		"domain_policies:\n  ops: {tenants: [t2], constraints: {require_critical_escalation: &e {x: [<<]}, forbidden_receiver_types: [email]}}\n" +
			"  fin: {tenants: [t1], constraints: *e}\n",
		// ... or brought in through a merge: the generator drops the file
		"_d: &d {tenants: [t1], constraints: {require_critical_escalation: <<, forbidden_receiver_types: [slack]}}\n" +
			"domain_policies:\n  fin: {<<: *d}\n",
	} {
		cfg, err := parseConfig([]byte(src))
		if err == nil || !strings.Contains(err.Error(), "unusable") {
			t.Errorf("parseConfig(%q) = %+v, %v; want the file refused as unusable", src, cfg, err)
		}
	}
	// One literal `<<` per mapping, at any depth, yaml.v3 merges as the
	// generator does.
	for _, src := range []string{
		"<<: " + fin + "\n",
		"x: &b {tenants: [t1], constraints: {forbidden_receiver_types: [slack]}}\n" +
			"domain_policies: {fin: {<<: *b, constraints: {<<: {forbidden_receiver_types: [slack]}}}}\n",
	} {
		cfg, err := parseConfig([]byte(src))
		if err != nil || len(cfg.DomainPolicies) != 1 ||
			!reflect.DeepEqual([]string(cfg.DomainPolicies["fin"].Constraints.ForbiddenReceiverTypes), []string{"slack"}) {
			t.Errorf("parseConfig(%q) = %+v, %v; want fin forbidding slack", src, cfg, err)
		}
	}
	// B1d: the generator reads a `tenants` list's scalar items as their
	// source text, a merge-tagged one included; read the same here.
	for src, want := range map[string][]string{
		"domain_policies: {fin: {tenants: [t1, <<], constraints: {forbidden_receiver_types: [slack]}}}\n":                    {"t1", "<<"},
		"domain_policies: {fin: {tenants: [t1, !!merge x], constraints: {forbidden_receiver_types: [slack]}}}\n":             {"t1", "x"},
		"x: {&v <<: {}}\ndomain_policies: {fin: {tenants: [t1, *v], constraints: {forbidden_receiver_types: [slack]}}}\n":    {"t1", "<<"},
		"domain_policies:\n  fin:\n    tenants:\n    - t1\n    - <<\n    constraints: {forbidden_receiver_types: [slack]}\n": {"t1", "<<"},
	} {
		cfg, err := parseConfig([]byte(src))
		if err != nil || !reflect.DeepEqual([]string(cfg.DomainPolicies["fin"].Tenants), want) {
			t.Errorf("parseConfig(%q) = %+v, %v; want fin for %v", src, cfg, err, want)
		}
	}
}

// #2659: a policy file rewritten to `domain_policies: null` (or `~`, or a
// bare key) keeps the last good policy on a hot reload (the watcher reports
// the failure); before, it replaced the policy with an empty one — every
// forbidden type allowed. LoadSnapshot (PR mode's fresh base) refuses it.
func TestPolicyFiles_NullDomainPoliciesKeepsLastGood(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	testutil.WriteYAML(t, dir, "_domain_policy.yaml", forbidding("fin", "t1", "slack"))
	m := NewManager(dir)
	for _, bad := range []string{"domain_policies: null\n", "domain_policies: ~\n", "domain_policies:\n"} {
		testutil.WriteYAML(t, dir, "_domain_policy.yaml", bad)
		if err := m.Reload(); err == nil || !strings.Contains(err.Error(), "_domain_policy.yaml") {
			t.Errorf("%q: Reload err = %v, want a failure naming _domain_policy.yaml", bad, err)
		}
		if got := forbiddenByDomain(m); !reflect.DeepEqual(got, map[string][]string{"fin": {"slack"}}) {
			t.Errorf("%q: watcher serves %v, want the last good (slack forbidden)", bad, got)
		}
		if v := m.CheckWrite("t1", map[string]string{"_routing_receiver_type": "slack"}); len(v) == 0 {
			t.Errorf("%q: a slack write for t1 is allowed, want the last good policy to refuse it", bad)
		}
		if _, err := LoadSnapshot(dir); err == nil {
			t.Errorf("%q: LoadSnapshot accepted the file, want it refused", bad)
		}
	}
	// At startup there is no last good: the file is skipped, as the
	// generator drops the block.
	if got := forbiddenByDomain(NewManager(dir)); len(got) != 0 {
		t.Errorf("startup on a null policy serves %v, want nothing", got)
	}
}
