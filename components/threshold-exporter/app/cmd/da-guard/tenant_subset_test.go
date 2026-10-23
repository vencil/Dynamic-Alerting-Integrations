package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"

	"gopkg.in/yaml.v3"
)

// subsetTree mixes what a tenant's entry is computed from: a profile, a
// root platform file's `tenants:` entry, a subtree `_defaults.yaml`, a
// retired alias spelling, a dimensional key, a schedule window, a switched-off
// key, a time-boxed silence, a custom alert and its duplicate, and _routing.
var subsetTree = map[string]string{
	"_defaults.yaml": "defaults:\n  mysql_connections: 80\n  mysql_slave_lag: 30\n  redis_memory: 70\n" +
		"state_filters:\n  maintenance:\n    severity: warning\n" +
		"profiles:\n  gold:\n    mysql_connections: 60\n    _routing:\n      receiver: {type: webhook, url: https://hooks.example.com/g}\n" +
		"tenants:\n  t-root:\n    redis_memory: 75\n",
	"a.yaml": "tenants:\n  t-a:\n    _profile: gold\n    mysql_slave_lag: disable\n" +
		"    'mysql_connections{db=\"orders\"}': 90\n" +
		"    redis_memory:\n      default: \"70\"\n      overrides:\n        - {window: \"09:00-17:00\", value: \"85\"}\n" +
		"    _silent_mode: {target: warning, expires: \"2099-01-01T00:00:00Z\", reason: drill}\n" +
		"    _custom_alerts:\n" +
		"      - {recipe: threshold, name: q, metric: qd, op: \">\", window: 5m, threshold: \"100\"}\n" +
		"      - {recipe: threshold, name: q, metric: qd, op: \">\", window: 5m, threshold: \"200\"}\n",
	"b.yaml": "tenants:\n  t-b:\n    mysql_connections: 50\n    _state_maintenance: \"on\"\n" +
		"    _routing:\n      receiver: {type: webhook, url: https://hooks.example.com/b}\n",
	"sub/_defaults.yaml": "defaults:\n  mysql_connections: 40\n",
	"sub/c.yaml":         "tenants:\n  t-c:\n    redis_memory: 65\n",
}

func decodeDoc(t *testing.T, stdout string) map[string]any {
	t.Helper()
	var doc map[string]any
	if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
		t.Fatalf("not JSON: %v: %q", err, stdout)
	}
	return doc
}

// normalize reads every string that is a re-serialized YAML mapping back
// into its value: the load writes some of them with an unstable key order
// (a mapping with timestamp keys, #3323), which is the same value.
func normalize(v any) any {
	switch x := v.(type) {
	case map[string]any:
		out := map[string]any{}
		for k, e := range x {
			out[k] = normalize(e)
		}
		return out
	case []any:
		out := make([]any, len(x))
		for i, e := range x {
			out[i] = normalize(e)
		}
		return out
	case string:
		if strings.Contains(x, ":") && strings.Contains(x, "\n") {
			var y any
			if yaml.Unmarshal([]byte(x), &y) == nil {
				return y
			}
		}
	}
	return v
}

// Every tenant's entry in a --tenant document is its entry in the whole
// tree's, for served-values under every flag combination and for effective
// — on the mixed tree above and on the repository's own trees.
func TestTenantSubset_EachTenantIsItsWholeTreeEntry(t *testing.T) {
	t.Parallel()
	trees := map[string]string{"mixed": writeParityTree(t, subsetTree)}
	root, err := filepath.Abs("../../../../..")
	if err != nil {
		t.Fatal(err)
	}
	for _, rel := range []string{"components/threshold-exporter/config/conf.d", "rule-packs/recipes/examples/conf.d"} {
		if _, err := os.Stat(filepath.Join(root, rel)); err == nil {
			trees[rel] = filepath.Join(root, rel)
		}
	}
	runs := [][]string{
		{servedValuesCmd, "--at", "2026-07-01T10:00:00Z"},
		{servedValuesCmd, "--at", "2026-07-01T10:00:00Z", "--schedules"},
		{servedValuesCmd, "--at", "2026-07-01T10:00:00Z", "--disable-effects"},
		{effectiveCmd},
	}
	for name, dir := range trees {
		for _, run := range runs {
			args := append(append([]string{}, run...), "--config-dir", dir)
			code, stdout, stderr := runOnce(t, args...)
			if code != exitOK {
				t.Fatalf("%s %v: exit %d: %s", name, run, code, stderr)
			}
			full := decodeDoc(t, stdout)
			tenants := full["tenants"].(map[string]any)
			if len(tenants) == 0 {
				t.Fatalf("%s: no tenant — the comparison is vacuous", name)
			}
			ids := make([]string, 0, len(tenants))
			for id := range tenants {
				ids = append(ids, id)
			}
			sort.Strings(ids)
			for _, id := range ids {
				code, stdout, stderr := runOnce(t, append(args, "--tenant", id)...)
				if code != exitOK {
					t.Fatalf("%s %v --tenant %s: exit %d: %s", name, run, id, code, stderr)
				}
				sub := decodeDoc(t, stdout)
				got := sub["tenants"].(map[string]any)
				if len(got) != 1 || !reflect.DeepEqual(normalize(got[id]), normalize(tenants[id])) {
					t.Errorf("%s %v --tenant %s: entry differs from the whole tree's\n got: %v\nwant: %v",
						name, run, id, got[id], tenants[id])
				}
				for _, k := range []string{"parse_failed", "skipped", "unreadable", "aliases", "unread_keys"} {
					if _, ok := full[k]; ok && !reflect.DeepEqual(sub[k], full[k]) {
						t.Errorf("%s %v --tenant %s: %s differs: %v vs %v", name, run, id, k, sub[k], full[k])
					}
				}
			}
		}
	}
}

// The subset fields: requested sorted and deduplicated, absent the ones
// the tree has no tenant for — exit 0, not a caller error (a tenant a
// write deletes, or writes where the load does not read, is an answer).
func TestTenantSubset_AbsentTenantIsNamedNotRefused(t *testing.T) {
	t.Parallel()
	dir := writeParityTree(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"a.yaml":         "tenants:\n  t-a:\n    mysql_connections: 50\n",
		".hidden.yaml":   "tenants:\n  t-h:\n    mysql_connections: 50\n",
	})
	for _, cmd := range []string{servedValuesCmd, effectiveCmd} {
		code, stdout, stderr := runOnce(t, cmd, "--config-dir", dir,
			"--tenant", "t-h", "--tenant", "t-a", "--tenant", "t-a", "--tenant", "t-gone")
		if code != exitOK {
			t.Fatalf("%s: exit %d: %s", cmd, code, stderr)
		}
		doc := decodeDoc(t, stdout)
		if got := doc["tenants_requested"]; !reflect.DeepEqual(got, []any{"t-a", "t-gone", "t-h"}) {
			t.Errorf("%s: tenants_requested = %v", cmd, got)
		}
		if got := doc["tenants_absent"]; !reflect.DeepEqual(got, []any{"t-gone", "t-h"}) {
			t.Errorf("%s: tenants_absent = %v", cmd, got)
		}
		if tenants := doc["tenants"].(map[string]any); len(tenants) != 1 || tenants["t-a"] == nil {
			t.Errorf("%s: tenants = %v, want t-a only", cmd, tenants)
		}
		// Present only in subset mode; [] when every requested tenant is there.
		code, stdout, _ = runOnce(t, cmd, "--config-dir", dir)
		if full := decodeDoc(t, stdout); code != exitOK || full["tenants_requested"] != nil || full["tenants_absent"] != nil {
			t.Errorf("%s: the whole-tree document carries the subset fields (exit %d)", cmd, code)
		}
		_, stdout, _ = runOnce(t, cmd, "--config-dir", dir, "--tenant", "t-a")
		if got := decodeDoc(t, stdout)["tenants_absent"]; !reflect.DeepEqual(got, []any{}) {
			t.Errorf("%s: tenants_absent = %v, want []", cmd, got)
		}
	}
}

// The subset narrows what is computed, never what is refused: a tenant
// declared twice refuses the tree though it is not requested; a file the
// load skips is still exit 3, named, beside the requested tenant.
func TestTenantSubset_TheTreesRefusalsStand(t *testing.T) {
	t.Parallel()
	dup := writeParityTree(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"a.yaml":         "tenants:\n  t-a:\n    mysql_connections: 50\n",
		"b.yaml":         "tenants:\n  t-b:\n    mysql_connections: 50\n",
		"c.yaml":         "tenants:\n  t-b:\n    mysql_connections: 60\n",
	})
	broken := writeParityTree(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"a.yaml":         "tenants:\n  t-a:\n    mysql_connections: 50\n",
		"bad.yaml":       "tenants: [\n",
	})
	for _, cmd := range []string{servedValuesCmd, effectiveCmd} {
		if code, _, stderr := runOnce(t, cmd, "--config-dir", dup, "--tenant", "t-a"); code != exitCallerErr ||
			!strings.Contains(stderr, "t-b") {
			t.Errorf("%s: a duplicate outside the subset: exit %d, stderr %q; want %d naming t-b", cmd, code, stderr, exitCallerErr)
		}
		code, stdout, stderr := runOnce(t, cmd, "--config-dir", broken, "--tenant", "t-a")
		if code != exitParseFailed {
			t.Fatalf("%s: exit %d, want %d: %s", cmd, code, exitParseFailed, stderr)
		}
		if doc := decodeDoc(t, stdout); !reflect.DeepEqual(doc["parse_failed"], []any{"bad.yaml"}) {
			t.Errorf("%s: parse_failed = %v", cmd, doc["parse_failed"])
		}
	}
}

// A duplicate is refused as without --tenant, naming the same tenant: the
// whole tree names the first duplicated tenant in sorted order (t-a), not
// the walk's first duplicate (t-z) — whichever tenants the subset names.
func TestTenantSubset_ADuplicateIsRefusedAsTheWholeTreeRefusesIt(t *testing.T) {
	t.Parallel()
	dir := writeParityTree(t, map[string]string{
		"a.yaml": "tenants:\n  t-z:\n    mysql_connections: 50\n",
		"b.yaml": "tenants:\n  t-z:\n    mysql_connections: 50\n  t-a:\n    mysql_connections: 50\n",
		"c.yaml": "tenants:\n  t-a:\n    mysql_connections: 60\n",
		"d.yaml": "tenants:\n  t-ok:\n    mysql_connections: 60\n",
	})
	for _, cmd := range []string{servedValuesCmd, effectiveCmd} {
		wantCode, _, want := runOnce(t, cmd, "--config-dir", dir)
		if wantCode != exitCallerErr {
			t.Fatalf("%s whole tree: exit %d, want %d: %s", cmd, wantCode, exitCallerErr, want)
		}
		for _, tenant := range []string{"t-a", "t-z", "t-ok"} {
			code, _, stderr := runOnce(t, cmd, "--config-dir", dir, "--tenant", tenant)
			if code != wantCode || stderr != want {
				t.Errorf("%s --tenant %s: exit %d, stderr %q; whole tree: exit %d, stderr %q",
					cmd, tenant, code, stderr, wantCode, want)
			}
		}
	}
}

func TestTenantSubset_AnUnusableTenantIsACallerError(t *testing.T) {
	t.Parallel()
	dir := writeParityTree(t, map[string]string{"a.yaml": "tenants:\n  t-a:\n    mysql_connections: 50\n"})
	for _, cmd := range []string{servedValuesCmd, effectiveCmd} {
		for _, bad := range []string{"", "t-\xff"} {
			// Not valid UTF-8: JSON cannot carry it back (checkOutputUTF8
			// walks the embedded tenantSubset).
			if code, _, _ := runOnce(t, cmd, "--config-dir", dir, "--tenant", bad); code != exitCallerErr {
				t.Errorf("%s --tenant %q: exit %d, want %d", cmd, bad, code, exitCallerErr)
			}
		}
	}
}
