package main

// #3671: a tenant file's top-level key other than `tenants` — a flat
// `mysql_connection_usage_percent:`, a `defaults:` or `optional_overrides:` written in a
// tenant file — is read by no reader. Before, only the tenant-config schema
// gate (check_confd_schema, CI) refused it: the shared verdict gave no
// finding, so a tree changed straight in git (direct mode) had nothing that
// judged it. Tenant ids are fixture names (CLAUDE.md #9).

import (
	"bytes"
	"encoding/json"
	"reflect"
	"strings"
	"testing"
)

// strayFindings is `da-guard key-verdict`'s findings of reason
// tenant_file_top_level over root, each "severity kind key file tenants
// blocks", and the document's blocks.
func strayFindings(t *testing.T, root string, args ...string) ([]string, bool) {
	t.Helper()
	var out, errOut bytes.Buffer
	if code := run(append([]string{keyVerdictCmd, "--config-dir", root}, args...), &out, &errOut); code != exitOK {
		t.Fatalf("exit %d: %s", code, errOut.String())
	}
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
		t.Fatal(err)
	}
	got := []string{}
	for _, f := range doc.Findings {
		if f.Reason == nil || *f.Reason != "tenant_file_top_level" {
			continue
		}
		if f.Tenants == nil {
			t.Errorf("%s: tenants null, want []", f.Key)
		}
		blocks := "passes"
		if f.Blocks {
			blocks = "blocks"
		}
		got = append(got, strings.Join([]string{f.Severity, f.Kind, f.Key, *f.File,
			"[" + strings.Join(f.Tenants, ",") + "]", blocks}, " "))
	}
	return got, doc.Blocks
}

func TestKeyVerdict_ATenantFilesStrayTopLevelKeyIsUnread(t *testing.T) {
	t.Parallel()
	// The issue's measurement: a flat file, and a tenant file with a
	// `defaults:` the exporter ignores (t1 is served the root's 80).
	root := writeTree(t, map[string]string{"_defaults.yaml": kvRoot,
		"mine.yaml": "mysql_connection_usage_percent: \"70\"\n",
		"t1.yaml":   "defaults: {mysql_connection_usage_percent: 5}\noptional_overrides: [pg_conn]\ntenants: {t1: {}}\n",
		"t2.yaml":   "tenants:\n  t2:\n    mysql_connection_usage_percent: \"60\"\n"})
	got, blocks := strayFindings(t, root)
	want := []string{
		"error unread mysql_connection_usage_percent mine.yaml [] blocks",
		"error unread defaults t1.yaml [] blocks",
		"error unread optional_overrides t1.yaml [] blocks",
	}
	if !reflect.DeepEqual(got, want) || !blocks {
		t.Fatalf("findings %q blocks %v, want %q blocks true", got, blocks, want)
	}
	// --tenant: the files declaring a requested tenant, no other.
	if got, _ := strayFindings(t, root, "--tenant", "t1"); !reflect.DeepEqual(got, want[1:]) {
		t.Fatalf("--tenant t1: %q, want %q", got, want[1:])
	}
	if got, _ := strayFindings(t, root, "--tenant", "t2"); len(got) != 0 {
		t.Fatalf("--tenant t2: %q, want none", got)
	}
	// A tenant file holding `tenants` alone is no finding.
	clean := writeTree(t, map[string]string{"_defaults.yaml": kvRoot, "t2.yaml": "tenants:\n  t2: {}\n"})
	if got, blocks := strayFindings(t, clean); len(got) != 0 || blocks {
		t.Fatalf("a clean tree: %q blocks %v", got, blocks)
	}
}

// The write gate (newlyUnread): a batch that adds such a key to a tenant
// file is refused; one that edits beside a stray key already there is not.
func TestWrite_AStrayTopLevelKeyInATenantFileWorsensTheTree(t *testing.T) {
	t.Parallel()
	setTop := `{"files":[{"path":"a.yaml","ops":[{"op":"set","path":["mysql_connection_usage_percent"],"value":{"str":"60"}}]}]}`
	root := writeTree(t, map[string]string{"_defaults.yaml": kvRoot, "a.yaml": "tenants:\n  t1: {}\n"})
	before := snapshot(t, root)
	code, res := runBatch(t, writeCmd, setTop, "--config-dir", root)
	if code != exitFindings || callReason(res) != writeReasonWorsensTree {
		t.Fatalf("exit %d reason %q worsened %v", code, callReason(res), worsenedOf(res))
	}
	if got := worsenedOf(res); !reflect.DeepEqual(got, []string{"unread a.yaml "}) ||
		res.Worsened[0].Finding != "tenant_file_top_level" {
		t.Fatalf("worsened %v %+v", got, res.Worsened)
	}
	sameTree(t, before, snapshot(t, root))

	had := writeTree(t, map[string]string{"_defaults.yaml": kvRoot,
		"a.yaml": "mysql_connection_usage_percent: \"70\"\ntenants:\n  t1: {}\n"})
	edit := `{"files":[{"path":"a.yaml","ops":[{"op":"set","path":["tenants","t1","mysql_connection_usage_percent"],` +
		`"value":{"str":"60"}}]}]}`
	if code, res := runBatch(t, writeCmd, edit, "--config-dir", had); code != exitOK {
		t.Fatalf("an existing stray key blocked an edit beside it: exit %d worsened %v (%v)", code,
			worsenedOf(res), res.Message)
	}
}

// A stray key is the file's, whatever tenant the file declares: a file set
// aside as a duplicate declarer keeps it in the baseline, so a batch that
// removes the other copy is not charged with it (review of #3671).
func TestWrite_RemovingADuplicateCopyBesideAStrayKeyPasses(t *testing.T) {
	t.Parallel()
	root := writeTree(t, map[string]string{"_defaults.yaml": kvRoot,
		"a.yaml": "defaults: {x: 1}\ntenants: {tz: {}}\n", "b.yaml": "tenants: {tz: {}}\n"})
	in := `{"files":[{"path":"b.yaml","delete":true},{"path":"a.yaml","ops":[{"op":"set",` +
		`"path":["tenants","tz","mysql_connection_usage_percent"],"value":{"str":"5"}}]}]}`
	if code, res := runBatch(t, writeCmd, in, "--config-dir", root); code != exitOK {
		t.Fatalf("exit %d worsened %v (%v)", code, worsenedOf(res), res.Message)
	}
}

// ⚠️ Pinned as it is, not as settled: a stray key's place is its file and
// key, as every finding on a file is (a profile's, a platform file's), so a
// file moved with one makes it new at the new path. A tenant's own key keeps
// its baseline across a move; whether this should too is the owner's call
// (#3671).
func TestWrite_MovingAFileWithAStrayKeyIsANewPlace(t *testing.T) {
	t.Parallel()
	root := writeTree(t, map[string]string{"_defaults.yaml": kvRoot,
		"a.yaml": "defaults: {x: 1}\ntenants: {t1: {}}\n"})
	code, res := runBatch(t, writeCmd, `{"files":[{"path":"c.yaml","from":"a.yaml"}]}`, "--config-dir", root)
	if code != exitFindings || !reflect.DeepEqual(worsenedOf(res), []string{"unread c.yaml "}) {
		t.Fatalf("exit %d worsened %v", code, worsenedOf(res))
	}
}
