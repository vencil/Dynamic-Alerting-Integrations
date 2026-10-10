package config

// key_refs_test.go — PlatformKeyFor and KeyRefs (#1822).

import (
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/internal/testutil"
)

func TestPlatformKeyFor(t *testing.T) {
	t.Parallel()
	cases := []struct {
		key, want string
		ok        bool
	}{
		{"mysql_threads_running", "mysql_threads_running", true},
		{"mysql_cpu", "mysql_threads_running", true},
		{"mysql_cpu_critical", "mysql_threads_running", true},
		{"mysql_threads_running_critical", "mysql_threads_running", true},
		{`mysql_cpu{db="a"}`, "mysql_threads_running", true},
		{`mysql_cpu{db='a', z = "b"}`, "mysql_threads_running", true},
		{`redis_queue_length{queue=~"x.*"}`, "redis_queue_length", true},
		// Exact-match alias contract: a prefix is not an alias.
		{"mysql_cpu_util", "mysql_cpu_util", true},
		{"_profile", "", false},
		{"_state_maintenance", "", false},
		{"_routing", "", false},
	}
	for _, c := range cases {
		got, ok := PlatformKeyFor(c.key)
		if got != c.want || ok != c.ok {
			t.Errorf("PlatformKeyFor(%q) = (%q, %v), want (%q, %v)", c.key, got, ok, c.want, c.ok)
		}
	}
}

// TestPlatformKeyFor_IsValidateTenantKeysMembership pins PlatformKeyFor to
// the check it reports: an override key is accepted with its platform key
// in defaults, and refused (an error naming it) without. Both directions,
// so the classifier cannot drift from the validator it was taken out of.
func TestPlatformKeyFor_IsValidateTenantKeysMembership(t *testing.T) {
	t.Parallel()
	keys := []string{
		"mysql_threads_running", "mysql_cpu", "mysql_cpu_critical",
		"mysql_threads_running_critical", `mysql_cpu{db="a"}`, `mysql_cpu{db='a'}`,
		`redis_queue_length{queue="a", priority="high"}`, "mysql_cpu_util", "disk_usage_critical",
	}
	for _, key := range keys {
		pk, ok := PlatformKeyFor(key)
		if !ok {
			t.Fatalf("%q: not a threshold key", key)
		}
		with := &ThresholdConfig{
			Defaults: map[string]float64{pk: 1},
			Tenants:  map[string]map[string]ScheduledValue{"t1": {key: {Default: "5"}}},
		}
		if errs := with.ValidateTenantKeys().Errors; len(errs) > 0 {
			t.Errorf("%q with defaults {%q}: refused: %v", key, pk, errs)
		}
		without := &ThresholdConfig{
			Defaults: map[string]float64{"other_metric": 1},
			Tenants:  map[string]map[string]ScheduledValue{"t1": {key: {Default: "5"}}},
		}
		if errs := without.ValidateTenantKeys().Errors; len(errs) == 0 {
			t.Errorf("%q without %q in defaults: accepted, so %q is not its platform key", key, pk, pk)
		}
	}
}

func TestKeyRefs(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	testutil.WriteTree(t, dir, map[string]string{
		"_defaults.yaml": "defaults:\n  disk_usage: 80\n  mysql_cpu: 30\n" +
			"optional_overrides:\n  - mysql_threads_running_critical\n  - disk_usage_critical\n",
		"_profiles.yaml": "profiles:\n  p1:\n    mysql_threads_running: 10\n",
		"t1.yaml":        "tenants:\n  t1:\n    mysql_cpu: \"50\"\n    mysql_cpu_util: 1\n    disk_usage: 70\n",
		"t2.yaml": "tenants:\n  t2:\n    mysql_threads_running_critical: \"90\"\n" +
			"    mysql_cpu{db='a'}: \"5\"\n    _state_maintenance: enable\n",
		// The exporter drops this file (a tenant body that is a list): none
		// of its keys is listed, and it is in Load.ParseFailed.
		"t3.yaml": "tenants:\n  t3:\n    - mysql_cpu\n",
		// A nested tenant file is read too.
		"sub/t4.yaml": "tenants:\n  t4:\n    mysql_cpu: ~\n",
		// A subtree carrier's references are listed, marked nested (#1822 F4).
		"sub/_defaults.yaml": "defaults:\n  mysql_cpu: 20\n",
	})
	rep, err := KeyRefs(dir, []string{"mysql_threads_running", "nothing_here"}, nil)
	if err != nil {
		t.Fatal(err)
	}
	want := []KeyRef{
		{File: "_defaults.yaml", Section: "defaults", Key: "mysql_cpu"},
		{File: "_defaults.yaml", Section: "optional_overrides", Key: "mysql_threads_running_critical"},
		{File: "_profiles.yaml", Section: "profiles", Owner: "p1", Key: "mysql_threads_running"},
		{File: "sub/_defaults.yaml", Section: "defaults", Key: "mysql_cpu", Nested: true},
		{File: "sub/t4.yaml", Section: "tenants", Owner: "t4", Key: "mysql_cpu"},
		{File: "t1.yaml", Section: "tenants", Owner: "t1", Key: "mysql_cpu"},
		{File: "t2.yaml", Section: "tenants", Owner: "t2", Key: "mysql_cpu{db='a'}"},
		{File: "t2.yaml", Section: "tenants", Owner: "t2", Key: "mysql_threads_running_critical"},
	}
	if got := rep.Refs["mysql_threads_running"]; !reflect.DeepEqual(got, want) {
		t.Errorf("refs:\n got %+v\nwant %+v", got, want)
	}
	if got := rep.Refs["nothing_here"]; got == nil || len(got) != 0 {
		t.Errorf("a metric nothing refers to: got %#v, want an empty list", got)
	}
	if !reflect.DeepEqual(rep.Load.ParseFailed, []string{"t3.yaml"}) {
		t.Errorf("ParseFailed = %v, want [t3.yaml]", rep.Load.ParseFailed)
	}
	if len(rep.Unscanned) != 0 {
		t.Errorf("Unscanned = %v, want none", rep.Unscanned)
	}
}

// TestKeyRefs_RootCarrierDropped: the root carrier whose `optional_overrides`
// is not a list is dropped whole by the exporter — KeyRefs names it in
// Load.ParseFailed, the one answer deprecate_rule needs, and lists no key of it.
func TestKeyRefs_RootCarrierDropped(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	testutil.WriteTree(t, dir, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_threads_running: 30\noptional_overrides: 5\n",
		"t1.yaml":        "tenants:\n  t1:\n    mysql_cpu: \"50\"\n",
	})
	rep, err := KeyRefs(dir, []string{"mysql_threads_running"}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(rep.Load.ParseFailed, []string{"_defaults.yaml"}) {
		t.Errorf("ParseFailed = %v, want [_defaults.yaml]", rep.Load.ParseFailed)
	}
	want := []KeyRef{{File: "t1.yaml", Section: "tenants", Owner: "t1", Key: "mysql_cpu"}}
	if got := rep.Refs["mysql_threads_running"]; !reflect.DeepEqual(got, want) {
		t.Errorf("refs = %+v, want %+v", got, want)
	}
}

func TestKeyRefs_RefusedTree(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	testutil.WriteTree(t, dir, map[string]string{
		"a.yaml": "tenants:\n  t1:\n    disk_usage: 1\n",
		"b.yaml": "tenants:\n  t1:\n    disk_usage: 2\n",
	})
	rep, err := KeyRefs(dir, []string{"disk_usage"}, nil)
	if err == nil || !strings.Contains(err.Error(), "duplicate") {
		t.Fatalf("err = %v, want the duplicate-tenant refusal", err)
	}
	if got := rep.Refs["disk_usage"]; got == nil || len(got) != 0 {
		t.Errorf("refs on a refused tree: %#v, want an empty list", got)
	}
	if _, err := KeyRefs(filepath.Join(dir, "missing"), []string{"disk_usage"}, nil); err == nil {
		t.Error("a missing dir: no error")
	}
}

// TestKeyRefs_RootPlatformVerdictIsPerFile pins the premise deprecate_rule's
// after-write check stands on (#1822 F1): whether the load drops a root `_`
// file depends on that file's bytes and on which root carriers exist (only
// the selected one is parsed), never on the tenant files or subdirectories.
// So judging the root `_` files alone, in an otherwise empty tree, gives the
// verdict the whole tree gives.
func TestKeyRefs_RootPlatformVerdictIsPerFile(t *testing.T) {
	t.Parallel()
	root := map[string]string{
		"_defaults.yaml": "defaults:\n  disk_usage: 80\noptional_overrides: 5\n",
		"_defaults.yml":  "defaults: [unselected, never parsed]\n",
		"_profiles.yaml": "profiles:\n  p1: [1]\n",
		"_ok.yaml":       "profiles:\n  p2:\n    disk_usage: 1\n",
	}
	full := map[string]string{
		"t1.yaml":            "tenants:\n  t1:\n    disk_usage: 1\n  t9: [1]\n",
		"sub/t2.yaml":        "tenants:\n  t2:\n    disk_usage: 1\n",
		"sub/_defaults.yaml": "defaults: [broken\n",
	}
	for k, v := range root {
		full[k] = v
	}
	whole, alone := t.TempDir(), t.TempDir()
	testutil.WriteTree(t, whole, full)
	testutil.WriteTree(t, alone, root)
	rootOnly := func(dir string) []string {
		rep, err := KeyRefs(dir, []string{"disk_usage"}, nil)
		if err != nil {
			t.Fatal(err)
		}
		var out []string
		for _, f := range rep.Load.ParseFailed {
			if !strings.Contains(f, "/") && strings.HasPrefix(f, "_") {
				out = append(out, f)
			}
		}
		return out
	}
	w, a := rootOnly(whole), rootOnly(alone)
	if !reflect.DeepEqual(w, a) || !reflect.DeepEqual(w, []string{"_defaults.yaml", "_profiles.yaml"}) {
		t.Errorf("root `_` parse_failed: whole tree %v, root files alone %v; want both [_defaults.yaml _profiles.yaml]", w, a)
	}
}
