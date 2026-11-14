package main

// #3881: a reserved key a SUBTREE `_defaults.yaml` writes is applied to no
// tenant (#3341). The main check has reported it since #2388
// (subtree_default_reserved_key); the shared key verdict gave no finding, so
// `generate-routes --validate` and validate-config's `routes` row — which
// read the verdict, not the main check — said nothing. (`da-guard write` runs
// the main check too, and refused such a write before this.)
//
// The property: over the same tree the two gates name the same (tenant, key)
// set, because both read config.SubtreeReservedReported — on every tree the
// main check judges. On a tree with a file the exporter drops the main check
// stops (exit 3) and names nothing, while the verdict still judges the files
// that load. Tenant ids are fixture names (CLAUDE.md #9).
//
// Seams: none — t.TempDir() trees through run().

import (
	"bytes"
	"encoding/json"
	"reflect"
	"sort"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/internal/guard/findingkind"
)

// gateSubtreePairs is every "tenant key" the main check reports as
// subtree_default_reserved_key over root, sorted, and its exit code (a tree
// with such a finding exits 1, one with a dropped file 3).
func gateSubtreePairs(t *testing.T, root string) ([]string, int) {
	t.Helper()
	var out, errOut bytes.Buffer
	code := run([]string{"--config-dir", root, "--format", "json"}, &out, &errOut)
	var doc struct {
		Report struct {
			Findings []struct {
				Kind     string `json:"kind"`
				TenantID string `json:"tenant_id"`
				Field    string `json:"field"`
			} `json:"findings"`
		} `json:"report"`
	}
	if err := json.Unmarshal(out.Bytes(), &doc); err != nil {
		t.Fatalf("main check: %v\n%s\n%s", err, out.String(), errOut.String())
	}
	pairs := []string{}
	for _, f := range doc.Report.Findings {
		if f.Kind == findingkind.FindingSubtreeDefaultReservedKey.String() {
			pairs = append(pairs, f.TenantID+" "+f.Field)
		}
	}
	sort.Strings(pairs)
	return pairs, code
}

// verdictSubtree is `da-guard key-verdict`'s findings of reason
// subtree_not_applied over root: each "severity kind key file [tenants]
// blocks" in the document's order, the "tenant key" pairs they name (sorted,
// deduplicated — a key two levels write is two findings), and the document's
// blocks. Any exit code, as gateSubtreePairs.
func verdictSubtree(t *testing.T, root string, args ...string) (lines, pairs []string, blocks bool) {
	t.Helper()
	var out, errOut bytes.Buffer
	run(append([]string{keyVerdictCmd, "--config-dir", root}, args...), &out, &errOut)
	var doc struct {
		Blocks   bool `json:"blocks"`
		Findings []struct {
			Severity string   `json:"severity"`
			Kind     string   `json:"kind"`
			Key      string   `json:"key"`
			File     *string  `json:"file"`
			Tenants  []string `json:"tenants"`
			Reason   *string  `json:"reason"`
			Blocks   bool     `json:"blocks"`
		} `json:"findings"`
	}
	if err := json.Unmarshal(out.Bytes(), &doc); err != nil {
		t.Fatalf("key-verdict: %v\n%s\n%s", err, out.String(), errOut.String())
	}
	lines, pairs = []string{}, []string{}
	seen := map[string]bool{}
	for _, f := range doc.Findings {
		if f.Reason == nil || *f.Reason != "subtree_not_applied" {
			continue
		}
		passes := "passes"
		if f.Blocks {
			passes = "blocks"
		}
		lines = append(lines, strings.Join([]string{f.Severity, f.Kind, f.Key, *f.File,
			"[" + strings.Join(f.Tenants, ",") + "]", passes}, " "))
		for _, tenant := range f.Tenants {
			if p := tenant + " " + f.Key; !seen[p] {
				seen[p] = true
				pairs = append(pairs, p)
			}
		}
	}
	sort.Strings(pairs)
	return lines, pairs, doc.Blocks
}

// subtreeRoot declares one state filter, so `_state_maint` is a declared
// filter's key and `_state_other` an undeclared one's.
const subtreeRoot = "defaults:\n  mysql_slave_lag: 30\n" +
	"state_filters:\n  maint:\n    reasons: []\n    severity: warning\n"

func TestKeyVerdict_SubtreeReservedKeysMatchTheMainCheck(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name  string
		files map[string]string
		// want: the (tenant, key) pairs BOTH gates name; nil = none.
		want []string
		// gateExit3: the tree holds a file the exporter drops, so the main
		// check exits 3 with no finding at all — only the verdict is compared
		// with want.
		gateExit3 bool
	}{
		{"the issue's tree: two reserved keys under defaults:",
			map[string]string{"_defaults.yaml": subtreeRoot,
				"team/_defaults.yaml": "defaults: {_silent_mode: all, _severity_dedup: disable}\n",
				"team/t1.yaml":        "tenants: {t1: {mysql_slave_lag: 20}}\n"},
			[]string{"t1 _severity_dedup", "t1 _silent_mode"}, false},
		{"written with a schedule (#3269: the exporter WARNs notApplied)",
			map[string]string{"_defaults.yaml": subtreeRoot,
				"team/_defaults.yaml": "defaults:\n  _silent_mode:\n    default: all\n" +
					"    overrides:\n      - {window: \"01:00-03:00\", value: warning}\n",
				"team/t1.yaml": "tenants: {t1: {}}\n"},
			[]string{"t1 _silent_mode"}, false},
		{"a schedule whose default is null writes nothing (#2708)",
			map[string]string{"_defaults.yaml": subtreeRoot,
				"team/_defaults.yaml": "defaults:\n  _silent_mode: {default: null}\n  _severity_dedup: null\n",
				"team/t1.yaml":        "tenants: {t1: {}}\n"},
			nil, false},
		{"nested levels, a tenant that sets the key itself, a sibling subtree without it",
			map[string]string{"_defaults.yaml": subtreeRoot,
				"team/_defaults.yaml":     "defaults: {_silent_mode: all}\n",
				"team/sub/_defaults.yaml": "defaults: {_silent_mode: warning, _state_maint: disable}\n",
				"team/t1.yaml":            "tenants: {t1: {_silent_mode: warning}}\n",
				"team/sub/t2.yaml":        "tenants: {t2: {}}\n",
				"ops/_defaults.yaml":      "defaults: {mysql_slave_lag: 25}\n",
				"ops/t3.yaml":             "tenants: {t3: {}}\n"},
			[]string{"t1 _silent_mode", "t2 _silent_mode", "t2 _state_maint"}, false},
		{"keys nothing reads anywhere: unregistered, an undeclared filter, a routing look-alike",
			map[string]string{"_defaults.yaml": subtreeRoot,
				"team/_defaults.yaml": "defaults: {_silent_x: 1, _state_other: disable, _routingProfile: a}\n",
				"team/t1.yaml":        "tenants: {t1: {}}\n"},
			[]string{"t1 _routingProfile", "t1 _silent_x", "t1 _state_other"}, false},
		{"_policies: read at the top level of an unwrapped file, not under defaults:",
			map[string]string{"_defaults.yaml": subtreeRoot,
				"top/_defaults.yaml":     "_policies: []\nmysql_slave_lag: 25\n",
				"top/t1.yaml":            "tenants: {t1: {}}\n",
				"wrapped/_defaults.yaml": "defaults: {_policies: []}\n",
				"wrapped/t2.yaml":        "tenants: {t2: {}}\n"},
			[]string{"t2 _policies"}, false},
		// ⚠️ What this row does NOT hold: that key-verdict hands ParseFailed to
		// the filter. The load drops such a file before the reserved set is
		// built, so the set never names it; the filter's own dropped-file
		// rule is held by pkg/config's unit test alone.
		{"a tree with a subtree file the exporter drops: only the verdict judges it",
			map[string]string{"_defaults.yaml": subtreeRoot,
				"team/_defaults.yaml": "defaults:\n  _silent_mode: all\n  x: !!binary aGk=\n",
				"team/t1.yaml":        "tenants: {t1: {}}\n",
				"ops/_defaults.yaml":  "defaults: {_silent_mode: all}\n",
				"ops/t2.yaml":         "tenants: {t2: {}}\n"},
			[]string{"t2 _silent_mode"}, true},
		// Controls: the same keys where a reader reads them, or where
		// another check owns them.
		{"control: in the tenant's own entry",
			map[string]string{"_defaults.yaml": subtreeRoot,
				"team/_defaults.yaml": "defaults: {mysql_slave_lag: 25}\n",
				"team/t1.yaml":        "tenants: {t1: {_silent_mode: all, _severity_dedup: disable, _state_maint: disable}}\n"},
			nil, false},
		{"control: in the root defaults (root_defaults_reserved_key's)",
			map[string]string{
				"_defaults.yaml": "defaults:\n  mysql_slave_lag: 30\n  _silent_mode: all\n",
				"team/t1.yaml":   "tenants: {t1: {}}\n"},
			nil, false},
		{"control: routing keys are the route generator's",
			map[string]string{"_defaults.yaml": subtreeRoot,
				"team/_defaults.yaml": "defaults: {_routing: {receiver: {type: webhook, url: \"https://example.invalid/h\"}}}\n",
				"team/t1.yaml":        "tenants: {t1: {}}\n"},
			nil, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			root := writeTree(t, c.files)
			want := c.want
			if want == nil {
				want = []string{}
			}
			gate, code := gateSubtreePairs(t, root)
			lines, verdict, blocks := verdictSubtree(t, root)
			if c.gateExit3 {
				if code != exitParseFailed || len(gate) != 0 {
					t.Errorf("main check: exit %d naming %q, want exit %d and nothing judged", code, gate, exitParseFailed)
				}
			} else if !reflect.DeepEqual(gate, want) {
				t.Errorf("main check names %q, want %q", gate, want)
			}
			if !reflect.DeepEqual(verdict, want) {
				t.Errorf("key verdict names %q, want %q (findings %q)", verdict, want, lines)
			}
			for _, l := range lines {
				if !strings.HasPrefix(l, "error unread ") || !strings.HasSuffix(l, " blocks") {
					t.Errorf("finding %q: want an unread error that blocks", l)
				}
			}
			if len(want) > 0 && !blocks {
				t.Errorf("the document's blocks is false with findings %q", lines)
			}
		})
	}
}

// One finding per file and key, naming every judged tenant that inherits the
// file; `--tenant` judges those tenants alone.
func TestKeyVerdict_ASubtreeReservedKeyIsOneFindingPerFile(t *testing.T) {
	t.Parallel()
	root := writeTree(t, map[string]string{"_defaults.yaml": subtreeRoot,
		"team/_defaults.yaml":     "defaults: {_silent_mode: all}\n",
		"team/sub/_defaults.yaml": "defaults: {_silent_mode: warning}\n",
		"team/t1.yaml":            "tenants: {t1: {}}\n",
		"team/sub/t2.yaml":        "tenants: {t2: {}}\n",
		"ops/t3.yaml":             "tenants: {t3: {}}\n"})
	lines, _, _ := verdictSubtree(t, root)
	want := []string{
		"error unread _silent_mode team/_defaults.yaml [t1,t2] blocks",
		"error unread _silent_mode team/sub/_defaults.yaml [t2] blocks",
	}
	if !reflect.DeepEqual(lines, want) {
		t.Fatalf("findings %q, want %q", lines, want)
	}
	if lines, _, _ := verdictSubtree(t, root, "--tenant", "t1"); !reflect.DeepEqual(lines,
		[]string{"error unread _silent_mode team/_defaults.yaml [t1] blocks"}) {
		t.Fatalf("--tenant t1: %q", lines)
	}
	if lines, _, blocks := verdictSubtree(t, root, "--tenant", "t3"); len(lines) != 0 || blocks {
		t.Fatalf("--tenant t3 (inherits no subtree file): %q blocks %v", lines, blocks)
	}
}

// The write gate: a batch that adds a reserved key to a subtree
// `_defaults.yaml` is refused and writes nothing — by the main check's
// finding, as before #3881, and now by the verdict's (newlyUnread) too, which
// is the one tenant-api and the route generator read. One that edits beside
// a reserved key already there is charged with neither.
func TestWrite_AReservedKeyAddedToSubtreeDefaultsWorsensTheTree(t *testing.T) {
	t.Parallel()
	add := `{"files":[{"path":"team/_defaults.yaml","ops":[{"op":"set","path":["defaults","_silent_mode"],` +
		`"value":{"str":"all"}}]}]}`
	root := writeTree(t, map[string]string{"_defaults.yaml": subtreeRoot,
		"team/_defaults.yaml": "defaults: {mysql_slave_lag: 25}\n", "team/t1.yaml": "tenants: {t1: {}}\n"})
	before := snapshot(t, root)
	code, res := runBatch(t, writeCmd, add, "--config-dir", root)
	if code != exitFindings || callReason(res) != writeReasonWorsensTree {
		t.Fatalf("exit %d reason %q worsened %v (%v)", code, callReason(res), worsenedOf(res), res.Message)
	}
	var unread []string
	for _, w := range res.Worsened {
		if w.Kind == "unread" {
			unread = append(unread, w.Path+" "+w.Tenant+" "+w.Finding)
		}
	}
	if !reflect.DeepEqual(unread, []string{"team/_defaults.yaml t1 subtree_not_applied"}) {
		t.Fatalf("unread worsened %q, want the subtree file's; all: %+v", unread, res.Worsened)
	}
	sameTree(t, before, snapshot(t, root))

	had := writeTree(t, map[string]string{"_defaults.yaml": subtreeRoot,
		"team/_defaults.yaml": "defaults: {mysql_slave_lag: 25, _silent_mode: all}\n",
		"team/t1.yaml":        "tenants: {t1: {}}\n"})
	edit := `{"files":[{"path":"team/_defaults.yaml","ops":[{"op":"set","path":["defaults","mysql_slave_lag"],` +
		`"value":{"int":"20"}}]}]}`
	if code, res := runBatch(t, writeCmd, edit, "--config-dir", had); code != exitOK {
		t.Fatalf("a reserved key already there blocked an edit beside it: exit %d worsened %v (%v)", code,
			worsenedOf(res), res.Message)
	}
}

// What a write makes new is not the same unit in the two gates: the main
// check's is (tenant, key), the verdict's is (file, key) — as every finding
// on a file is (a profile's, a platform file's, #3671's stray key).
//
// ⚠️ Pinned as it is, not as settled: the second and third cases are a
// write the main check passes and the verdict refuses. A second copy of a
// key a tenant already inherits is a new dead write, which the verdict is
// right to name; a file moved deeper carries its key to a new place while
// reaching fewer tenants. Whether a move should keep its baseline is the
// same open call as TestWrite_MovingAFileWithAStrayKeyIsANewPlace.
func TestWrite_SubtreeReservedKeysAndWhatEachGateCallsNew(t *testing.T) {
	t.Parallel()
	tree := func() map[string]string {
		return map[string]string{"_defaults.yaml": subtreeRoot,
			"team/_defaults.yaml": "defaults: {mysql_slave_lag: 25, _silent_mode: all}\n",
			"team/t1.yaml":        "tenants: {t1: {}}\n",
			"team/sub/t2.yaml":    "tenants: {t2: {}}\n",
			"ops/t3.yaml":         "tenants: {t3: {}}\n"}
	}
	cases := []struct {
		name, in string
		code     int
		// want: every worsened item, "kind path tenant finding", in order.
		want []string
	}{
		{"a tenant added under a file that already has the key: the main check's alone",
			`{"files":[{"path":"team/t4.yaml","create":{"map":[["tenants",{"map":[["t4",{"map":[]}]]}]]}}]}`,
			exitFindings, []string{"finding  t4 subtree_default_reserved_key"}},
		{"the same key written again one level down: the verdict's alone",
			`{"files":[{"path":"team/sub/_defaults.yaml","create":{"map":[["defaults",{"map":[["_silent_mode",{"str":"warning"}]]}]]}}]}`,
			exitFindings, []string{"unread team/sub/_defaults.yaml t2 subtree_not_applied"}},
		{"the file moved one level down, reaching fewer tenants: the verdict's alone",
			`{"files":[{"path":"team/sub/_defaults.yaml","from":"team/_defaults.yaml"}]}`,
			exitFindings, []string{"unread team/sub/_defaults.yaml t2 subtree_not_applied"}},
		{"a second key added to the file: both, once per inheriting tenant",
			`{"files":[{"path":"team/_defaults.yaml","ops":[{"op":"set","path":["defaults","_severity_dedup"],"value":{"str":"disable"}}]}]}`,
			exitFindings, []string{
				"finding  t1 subtree_default_reserved_key", "finding  t2 subtree_default_reserved_key",
				"unread team/_defaults.yaml t1 subtree_not_applied", "unread team/_defaults.yaml t2 subtree_not_applied"}},
		{"the key's value changed in place: neither",
			`{"files":[{"path":"team/_defaults.yaml","ops":[{"op":"set","path":["defaults","_silent_mode"],"value":{"str":"warning"}}]}]}`,
			exitOK, []string{}},
		{"the key removed: neither",
			`{"files":[{"path":"team/_defaults.yaml","ops":[{"op":"unset","path":["defaults","_silent_mode"]}]}]}`,
			exitOK, []string{}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			root := writeTree(t, tree())
			code, res := runBatch(t, writeCmd, c.in, "--config-dir", root, "--dry-run")
			got := []string{}
			for _, w := range res.Worsened {
				got = append(got, w.Kind+" "+w.Path+" "+w.Tenant+" "+w.Finding)
			}
			sort.Strings(got)
			if code != c.code || !reflect.DeepEqual(got, c.want) {
				t.Fatalf("exit %d worsened %q, want exit %d %q (%v)", code, got, c.code, c.want, res.Message)
			}
		})
	}
}
