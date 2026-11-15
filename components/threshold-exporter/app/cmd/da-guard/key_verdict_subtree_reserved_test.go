package main

// #3881: a key a subtree `_defaults.yaml` writes that subtree defaults do not
// carry (a reserved key, or one no threshold row is made of; #2388, #3341)
// is applied by no reader there. da-guard's own check named it
// (subtree_default_reserved_key) and so did `da-guard write`, through that
// check; the shared key verdict gave no finding, so `generate-routes
// --validate` (and anything else reading the verdict) passed it. The verdict
// now reports each as an unread error on the subtree file, reason
// subtree_default_reserved_key, from the same set da-guard's check reads
// (guard.SubtreeReservedReported). Tenant ids are fixture names (CLAUDE.md #9).

import (
	"bytes"
	"encoding/json"
	"reflect"
	"sort"
	"strings"
	"testing"
)

// subtreeReservedVerdict is `da-guard key-verdict`'s findings of reason
// subtree_default_reserved_key over root, each "severity kind key file
// [tenants] blocks", sorted. A file it cannot decode is exit 3 with the
// document still printed (parse_failed).
func subtreeReservedVerdict(t *testing.T, root string, args ...string) []string {
	t.Helper()
	var out, errOut bytes.Buffer
	if code := run(append([]string{keyVerdictCmd, "--config-dir", root}, args...), &out, &errOut); code != exitOK &&
		code != exitParseFailed {
		t.Fatalf("exit %d: %s", code, errOut.String())
	}
	var doc struct {
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
		t.Fatal(err)
	}
	got := []string{}
	for _, f := range doc.Findings {
		if f.Reason == nil || *f.Reason != "subtree_default_reserved_key" {
			continue
		}
		blocks := "passes"
		if f.Blocks {
			blocks = "blocks"
		}
		got = append(got, strings.Join([]string{f.Severity, f.Kind, f.Key, *f.File,
			"[" + strings.Join(f.Tenants, ",") + "]", blocks}, " "))
	}
	sort.Strings(got)
	return got
}

// The issue's measurement, and its schedule shape: one finding per file and
// key, naming every tenant that inherits it; a tenant outside the subtree is
// not named; --tenant narrows the tenants.
func TestKeyVerdict_ASubtreeDefaultsReservedKeyIsUnread(t *testing.T) {
	t.Parallel()
	root := writeTree(t, map[string]string{
		"_defaults.yaml":      kvRoot,
		"team/_defaults.yaml": "defaults:\n  _severity_dedup: disable\n" + strings.Replace(scheduledSilent, "    ", "  ", 1),
		"team/t1.yaml":        "tenants:\n  t1: {}\n",
		"team/t2.yaml":        "tenants:\n  t2: {}\n",
		"ops/t3.yaml":         "tenants:\n  t3: {}\n",
	})
	want := []string{
		"error unread _severity_dedup team/_defaults.yaml [t1,t2] blocks",
		"error unread _silent_mode team/_defaults.yaml [t1,t2] blocks",
	}
	if got := subtreeReservedVerdict(t, root); !reflect.DeepEqual(got, want) {
		t.Fatalf("findings %q, want %q", got, want)
	}
	wantT1 := []string{
		"error unread _severity_dedup team/_defaults.yaml [t1] blocks",
		"error unread _silent_mode team/_defaults.yaml [t1] blocks",
	}
	if got := subtreeReservedVerdict(t, root, "--tenant", "t1"); !reflect.DeepEqual(got, wantT1) {
		t.Fatalf("--tenant t1: %q, want %q", got, wantT1)
	}
	if got := subtreeReservedVerdict(t, root, "--tenant", "t3"); len(got) != 0 {
		t.Fatalf("--tenant t3 (outside the subtree): %q, want none", got)
	}
}

// Each subtree file that writes the key is a finding of its own: each asks
// for a value no reader applies, and deleting one leaves the other.
func TestKeyVerdict_EachSubtreeLevelWritingTheKeyIsAFinding(t *testing.T) {
	t.Parallel()
	root := writeTree(t, map[string]string{
		"_defaults.yaml":          kvRoot,
		"team/_defaults.yaml":     "defaults:\n  _silent_mode: all\n",
		"team/sub/_defaults.yaml": "defaults:\n  _silent_mode: warning\n",
		"team/t1.yaml":            "tenants:\n  t1: {}\n",
		"team/sub/t2.yaml":        "tenants:\n  t2: {}\n",
	})
	want := []string{
		"error unread _silent_mode team/_defaults.yaml [t1,t2] blocks",
		"error unread _silent_mode team/sub/_defaults.yaml [t2] blocks",
	}
	if got := subtreeReservedVerdict(t, root); !reflect.DeepEqual(got, want) {
		t.Fatalf("findings %q, want %q", got, want)
	}
}

const scheduledSilent = "    _silent_mode: {default: disable, overrides: [{window: \"03:00-04:00\", value: all}]}\n"

// ⛔ ONE SET (#3881): for every shape da-guard's check filters — a file it
// drops, a key read at the top level elsewhere, a routing key, a null — the
// verdict names exactly the (tenant, key) pairs da-guard's
// subtree_default_reserved_key does. Each case says what it expects, so two
// empty sets cannot pass for one.
func TestKeyVerdict_SubtreeReservedIsDaGuardsOwnSet(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name    string
		subtree string
		want    []string // "tenant key", sorted
	}{
		{"inside-defaults", "defaults:\n  _silent_mode: all\n  _x: 1\n", []string{"t1 _silent_mode", "t1 _x"}},
		{"schedule", "defaults:\n" + strings.Replace(scheduledSilent, "    ", "  ", 1), []string{"t1 _silent_mode"}},
		// TopLevelReadElsewhere: another reader takes `_policies` from the top
		// level of a file with no `defaults:` mapping; inside one, nothing does.
		{"read-elsewhere-top-level", "_policies: {}\n_silent_mode: all\n", []string{"t1 _silent_mode"}},
		{"read-elsewhere-inside-defaults", "defaults:\n  _policies: {}\n", []string{"t1 _policies"}},
		// A file parse-tree refuses (a `!!binary` tag) is dropped: it applies
		// nothing, so it writes no reserved key. (The filter's own drop is
		// TestSubtreeReservedKeys_SkipsAFileTheExporterDrops; here t1 is
		// judged and named by neither.)
		{"parse-failed", "_silent_mode: all\nx: !!binary aGk=\n", nil},
		// The route generator owns a routing key; a null writes nothing.
		{"routing-and-null", "defaults:\n  _routing_xyz: 1\n  _silent_mode: null\n", nil},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			root := writeTree(t, map[string]string{
				"_defaults.yaml":      kvRoot,
				"team/_defaults.yaml": c.subtree,
				"team/t1.yaml":        "tenants:\n  t1: {}\n",
			})
			guardSet := guardSubtreeReserved(t, root)
			var verdictSet []string
			for _, f := range subtreeReservedVerdict(t, root) {
				p := strings.Fields(f) // severity kind key file [tenants] blocks
				for _, tenant := range strings.Split(strings.Trim(p[4], "[]"), ",") {
					verdictSet = append(verdictSet, tenant+" "+p[2])
				}
			}
			sort.Strings(verdictSet)
			if !reflect.DeepEqual(guardSet, c.want) {
				t.Fatalf("da-guard's set %q, want %q (the case no longer measures what it names)", guardSet, c.want)
			}
			if !reflect.DeepEqual(verdictSet, guardSet) {
				t.Fatalf("key verdict's set %q, da-guard's %q", verdictSet, guardSet)
			}
		})
	}
}

// guardSubtreeReserved is da-guard's own subtree_default_reserved_key
// findings over root, each "tenant key", sorted. A tree da-guard stops on (a
// file it cannot decode: exit 3, no report) has none.
func guardSubtreeReserved(t *testing.T, root string) []string {
	t.Helper()
	code, stdout, stderr := runOnce(t, "--format", "json", "--config-dir", root)
	var doc struct {
		Report *reportedReport `json:"report"`
	}
	if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
		t.Fatalf("stdout is not JSON (exit %d): %v\nstderr=%s", code, err, stderr)
	}
	var out []string
	if doc.Report != nil {
		for _, f := range doc.Report.Findings {
			if f.Kind == "subtree_default_reserved_key" {
				out = append(out, f.TenantID+" "+f.Field)
			}
		}
	}
	sort.Strings(out)
	return out
}

// `da-guard write` already refused such a key through da-guard's own check;
// the verdict's copy of the same set is not a second line (#3881).
func TestWrite_ASubtreeDefaultsReservedKeyIsNamedOnce(t *testing.T) {
	t.Parallel()
	create := `{"files":[{"path":"team/_defaults.yaml","create":{"map":[["defaults",{"map":[` +
		`["_silent_mode",{"str":"all"}]]}]]}}]}`
	root := writeTree(t, map[string]string{"_defaults.yaml": kvRoot, "team/t1.yaml": "tenants:\n  t1: {}\n"})
	before := snapshot(t, root)
	code, res := runBatch(t, writeCmd, create, "--config-dir", root)
	if code != exitFindings || callReason(res) != writeReasonWorsensTree {
		t.Fatalf("exit %d reason %q worsened %v", code, callReason(res), worsenedOf(res))
	}
	var got []string
	for _, w := range res.Worsened {
		got = append(got, w.Kind+" "+w.Tenant+" "+w.Finding)
	}
	if want := []string{"finding t1 subtree_default_reserved_key"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("worsened %q, want %q", got, want)
	}
	sameTree(t, before, snapshot(t, root))
}
