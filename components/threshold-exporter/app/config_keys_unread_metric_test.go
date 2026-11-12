package main

// #3353: da_config_keys_unread{reason} — the gauge of the keys-unread audit
// (config_keys_unread.go) — and the shipped ConfigKeysUnread rule. Tenant
// ids are fixture names (CLAUDE.md #9).

import (
	"bytes"
	"log"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/testutil"
	promqlparser "github.com/prometheus/prometheus/promql/parser"
	"gopkg.in/yaml.v3"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// keysUnreadGauge is da_config_keys_unread's value per reason, read from the
// registry the manager publishes to.
func keysUnreadGauge(t *testing.T, cm *configMetrics) map[string]float64 {
	t.Helper()
	out := map[string]float64{}
	for r, g := range cm.keysUnread {
		out[r] = testutil.ToFloat64(g)
	}
	return out
}

// loadDirWithLog loads files as a conf.d tree and returns the manager, its
// metrics and its log.
func loadDirWithLog(t *testing.T, files map[string]string) (*ConfigManager, *configMetrics, *bytes.Buffer) { //nolint:unparam
	t.Helper()
	dir := t.TempDir()
	for name, body := range files {
		writeTestYAML(t, filepath.Join(dir, name), body)
	}
	fresh, _ := freshMetrics(t)
	var logBuf bytes.Buffer
	m := NewConfigManager(dir)
	m.SetMetrics(fresh)
	m.SetLogger(log.New(&logBuf, "", 0))
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	return m, fresh, &logBuf
}

// Every reason, counted per (tenant, key); a key a policy rule names is read
// by policy_engine and is not counted, as the shared verdict holds.
func TestKeysUnreadGauge_CountsEachReason(t *testing.T) {
	t.Parallel()
	_, cm, logBuf := loadDirWithLog(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\noptional_overrides: [redis_evicted_keys]\n" +
			"profiles:\n  gold:\n    redis_evicted_keys: \"11\"\n" +
			"_policies:\n  - {name: p, target: owner_tag.team, operator: required, exclude_tenants: [ty]}\n" +
			// A `_` key the registry does not list is outside policy_engine's
			// view: a rule naming it does not make it read.
			"  - {name: q, target: _myth, operator: required}\n",
		"tx.yaml": "tenants:\n  tx:\n    _profile: gold\n    mysql_conections: \"5\"\n    owner_tag: x\n" +
			"    _myth: \"1\"\n    mysql_conectionz: disable\n",
		// ty is excluded from the policy rule: its owner_tag is unread.
		"ty.yaml": "tenants:\n  ty:\n    _profile: gold\n    owner_tag: y\n",
	})
	got := keysUnreadGauge(t, cm)
	want := map[string]float64{"unconsumed": 2, "profile_not_filled": 2, "unregistered": 1, "disabled_unread": 1}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("da_config_keys_unread = %v, want %v\n%s", got, want, logBuf.String())
	}
	line := logLinesWith(logBuf.String(), keysUnreadLogPrefix)
	if len(line) != 1 {
		t.Fatalf("keys-unread lines %q, want one", line)
	}
	for _, w := range []string{`tenant="ty" key="owner_tag" reason=unconsumed`,
		`tenant="tx" key="redis_evicted_keys" reason=profile_not_filled`,
		`tenant="ty" key="redis_evicted_keys" reason=profile_not_filled`} {
		if !strings.Contains(line[0], w) {
			t.Errorf("line lacks %q: %s", w, line[0])
		}
	}
	if strings.Contains(line[0], `tenant="tx" key="owner_tag"`) {
		t.Errorf("a key a policy rule names for the tenant is listed: %s", line[0])
	}
}

// The gauge is a level: a commit that fixes the keys sets it back to 0, and
// the policy rule's heads survive a reload that did not re-read the root:
// they ride on the root's cached partial, not on the scan's bytes, which the
// commit has released.
func TestKeysUnreadGauge_ReturnsToZeroAndKeepsPolicyAcrossReload(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	root := "defaults:\n  mysql_connections: 80\n_policies:\n  - {name: p, target: owner_tag, operator: required}\n"
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), root)
	writeTestYAML(t, filepath.Join(dir, "tx.yaml"), "tenants:\n  tx:\n    owner_tag: x\n    mysql_conections: \"5\"\n")
	fresh, _ := freshMetrics(t)
	m := NewConfigManager(dir)
	m.SetMetrics(fresh)
	m.SetLogger(log.New(&bytes.Buffer{}, "", 0))
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	if got := keysUnreadGauge(t, fresh)["unconsumed"]; got != 1 {
		t.Fatalf("unconsumed = %v, want 1 (the misspelt key; owner_tag is read by the policy)", got)
	}
	// Fix the tenant file only, and reload from the prior scan: the root's
	// partial is reused, not re-parsed.
	writeTestYAML(t, filepath.Join(dir, "tx.yaml"), "tenants:\n  tx:\n    owner_tag: x\n    mysql_connections: \"5\"\n")
	m.mu.RLock()
	prior := m.flat.tree
	m.mu.RUnlock()
	scan, err := scanDirTree(m.path, prior, m.getMetrics(), m.getLogger())
	if err != nil {
		t.Fatal(err)
	}
	if f := scan.Files["_defaults.yaml"]; f == nil || f.Data != nil {
		t.Fatalf("fixture precondition: the reload must not hold the root's bytes (it reuses the prior's partial)")
	}
	m.reloadMu.Lock()
	err = m.fullDirLoadFrom(scan)
	m.reloadMu.Unlock()
	if err != nil {
		t.Fatalf("reload: %v", err)
	}
	if got := keysUnreadGauge(t, fresh); got["unconsumed"] != 0 {
		t.Errorf("after the fix da_config_keys_unread = %v, want unconsumed 0 (owner_tag still named by the root's policy)", got)
	}
}

// Single-file mode reads the file's own rules.
func TestKeysUnreadGauge_SingleFilePolicy(t *testing.T) {
	t.Parallel()
	path := filepath.Join(t.TempDir(), "config.yaml")
	writeTestYAML(t, path, "defaults:\n  mysql_connections: 80\n_policies:\n  - {name: p, target: owner_tag, operator: required}\n"+
		"tenants:\n  tx:\n    owner_tag: x\n")
	fresh, _ := freshMetrics(t)
	m := NewConfigManager(path)
	m.SetMetrics(fresh)
	m.SetLogger(log.New(&bytes.Buffer{}, "", 0))
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	if got := keysUnreadGauge(t, fresh)["unconsumed"]; got != 0 {
		t.Errorf("unconsumed = %v, want 0: the file's policy rule names owner_tag", got)
	}
}

// The gauge's HELP explains every reason from the one table.
func TestKeysUnreadHelp_NamesEveryReasonAndTheGap(t *testing.T) {
	t.Parallel()
	ch := make(chan *prometheus.Desc, 1)
	newConfigMetrics().set.KeysUnread.Describe(ch)
	desc := (<-ch).String()
	for _, r := range config.KeysUnreadReasons() {
		if !strings.Contains(desc, r+" (") {
			t.Errorf("HELP does not explain reason %q: %s", r, desc)
		}
	}
	if !strings.Contains(desc, "NOT COVERED") {
		t.Errorf("HELP does not state what it cannot see: %s", desc)
	}
}

// TestKeysUnreadRuleSelectsTheAlertingReasons: in every shipped copy of the
// platform rule pack, ConfigKeysUnread's da_config_keys_unread selector
// selects exactly the reasons config.KeysUnreadAlerts names — a reason added
// to the table is alerted on, or not, by the table alone.
func TestKeysUnreadRuleSelectsTheAlertingReasons(t *testing.T) {
	root := findRepoRoot(t)
	var files []string
	for _, family := range [][]string{
		{"rule-packs/rule-pack-*.yaml", "rule-packs/rule-pack-*.yml"},
		{"k8s/03-monitoring/configmap-rules-*.yaml", "k8s/03-monitoring/configmap-rules-*.yml"},
	} {
		got := globDeclared(t, root, family...)
		if len(got) == 0 {
			t.Fatalf("glob %v: no files", family)
		}
		files = append(files, got...)
	}
	var want []string
	for _, r := range config.KeysUnreadReasons() {
		if config.KeysUnreadAlerts(r) {
			want = append(want, r)
		}
	}
	perDir := map[string]int{}
	for _, f := range files {
		raw, err := os.ReadFile(f)
		if err != nil {
			t.Fatal(err)
		}
		var doc any
		if err := yaml.Unmarshal(raw, &doc); err != nil {
			t.Fatalf("%s: %v", f, err)
		}
		rel, _ := filepath.Rel(root, f)
		for _, expr := range ruleExprs(doc) {
			parsed, err := promParser.ParseExpr(expr)
			if err != nil {
				continue // rulepack_contract_test reports invalid PromQL
			}
			promqlparser.Inspect(parsed, func(node promqlparser.Node, _ []promqlparser.Node) error {
				vs, ok := node.(*promqlparser.VectorSelector)
				if !ok || vs.Name != "da_config_keys_unread" {
					return nil
				}
				perDir[filepath.Dir(rel)]++
				var got []string
				for _, r := range config.KeysUnreadReasons() {
					all := true
					for _, m := range vs.LabelMatchers {
						if m.Name == "reason" && !m.Matches(r) {
							all = false
						}
					}
					if all {
						got = append(got, r)
					}
				}
				sort.Strings(got)
				w := append([]string(nil), want...)
				sort.Strings(w)
				if !reflect.DeepEqual(got, w) {
					t.Errorf("%s: %s selects reasons %v, config.KeysUnreadAlerts names %v", rel, vs.String(), got, w)
				}
				return nil
			})
		}
	}
	for _, dir := range []string{"rule-packs", "k8s/03-monitoring"} {
		if perDir[dir] == 0 {
			t.Errorf("%s: no da_config_keys_unread selector — ConfigKeysUnread is not shipped there", dir)
		}
	}
}

// A subtree `_defaults.yaml` valuing the key the tenant's profile could not
// fill in hands the tenant the key after ApplyProfiles ran: /metrics serves
// it and the verdict reads it as read, so it is not profile_not_filled.
func TestKeysUnreadGauge_ProfileNotFilledButSubtreeValuesIt(t *testing.T) {
	t.Parallel()
	_, cm, logBuf := loadDirWithLog(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\noptional_overrides: [redis_evicted_keys]\n" +
			"profiles:\n  gold:\n    redis_evicted_keys: \"3\"\n",
		"team/_defaults.yaml": "defaults:\n  redis_evicted_keys: 5\n",
		"team/tx.yaml":        "tenants:\n  tx:\n    _profile: gold\n",
		// the control: no subtree value, so the profile's is not filled in
		"ty.yaml": "tenants:\n  ty:\n    _profile: gold\n",
	})
	if got := keysUnreadGauge(t, cm)["profile_not_filled"]; got != 1 {
		t.Errorf("profile_not_filled = %v, want 1 (ty only; tx's subtree values the key)\n%s", got, logBuf.String())
	}
	if strings.Contains(logBuf.String(), `tenant="tx" key="redis_evicted_keys"`) {
		t.Errorf("tx's subtree-valued key is listed:\n%s", logBuf.String())
	}
}
