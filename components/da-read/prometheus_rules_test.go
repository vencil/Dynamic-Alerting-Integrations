package main

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

type rulesOutput struct {
	Files []struct {
		Path      string            `json:"path"`
		Documents []json.RawMessage `json:"documents"`
		Problem   *struct {
			Message string `json:"message"`
		} `json:"problem"`
	} `json:"files"`
}

// runRulesOn writes each content to its own temp file and runs
// `da-read prometheus-rules` on them, in order.
func runRulesOn(t *testing.T, contents ...string) (int, rulesOutput, []string, string) {
	t.Helper()
	dir := t.TempDir()
	args := []string{"prometheus-rules"}
	var paths []string
	for i, c := range contents {
		p := filepath.Join(dir, "rules"+string(rune('a'+i))+".yml")
		if err := os.WriteFile(p, []byte(c), 0o600); err != nil {
			t.Fatal(err)
		}
		args = append(args, p)
		paths = append(paths, p)
	}
	var stdout, stderr bytes.Buffer
	rc := run(args, strings.NewReader(""), &stdout, &stderr)
	var out rulesOutput
	if err := json.Unmarshal(stdout.Bytes(), &out); err != nil {
		t.Fatalf("stdout %q is not the JSON contract: %v (stderr %q)", stdout.String(), err, stderr.String())
	}
	return rc, out, paths, stderr.String()
}

func ruleDocument(t *testing.T, in string) string {
	t.Helper()
	rc, out, _, errOut := runRulesOn(t, in)
	if rc != exitOK || errOut != "" || len(out.Files) != 1 || out.Files[0].Problem != nil ||
		len(out.Files[0].Documents) != 1 {
		t.Fatalf("%q: rc=%d stderr=%q out=%+v", in, rc, errOut, out)
	}
	return string(out.Files[0].Documents[0])
}

// Only the keys the text writes, each value as rulefmt reads it: `for:
// 300s` is Prometheus' 5m, a label value `010` the string "010" (the field
// is a string and yaml.v3 takes the text), an expression written as a number
// its text.
func TestRulesKeepOnlyTheWrittenKeys(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name, in, want string
	}{
		{"empty", "", `{}`},
		{"alert", "groups:\n- name: g\n  rules:\n  - alert: A\n    expr: up == 0\n    for: 300s\n" +
			"    labels: {tenant: 010, severity: page}\n    annotations: {summary: x}\n",
			`{"groups":[{"name":"g","rules":[{"alert":"A","expr":"up == 0","for":"5m",` +
				`"labels":{"severity":"page","tenant":"010"},"annotations":{"summary":"x"}}]}]}`},
		{"record-and-group-fields", "groups:\n- name: g\n  interval: 1m\n  limit: 0\n  labels: {team: a}\n" +
			"  rules:\n  - {record: 'r:x', expr: 1}\n",
			`{"groups":[{"name":"g","interval":"1m","limit":0,"labels":{"team":"a"},` +
				`"rules":[{"record":"r:x","expr":"1"}]}]}`},
		{"empty-rules", "groups:\n- name: g\n  rules: []\n", `{"groups":[{"name":"g","rules":[]}]}`},
		// Duration expressions are on in Prometheus v3.15 with no feature flag.
		{"duration-expr", "groups:\n- name: g\n  rules:\n  - {record: 'r:y', expr: 'rate(x[5m * 2])'}\n",
			`{"groups":[{"name":"g","rules":[{"record":"r:y","expr":"rate(x[5m * 2])"}]}]}`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			if got := ruleDocument(t, tc.in); got != tc.want {
				t.Fatalf("document %s\nwant     %s", got, tc.want)
			}
		})
	}
}

// rulefmt decodes with go-yaml v3, whose `<<` differs from v2's: an explicit
// key wins over a merged one wherever it is written (rule C keeps its own
// `alert` although `<<` comes after it; go-yaml v2 would take A's), and in a
// merged list the earlier mapping wins (rule D). The values Prometheus reads
// confirm which keys were written. A second `<<` in one mapping is refused
// by v3 (TestRulesReportEachFile).
func TestRuleMergedKeysAreWritten(t *testing.T) {
	t.Parallel()
	in := "groups:\n- name: g\n  rules:\n" +
		"  - &a {alert: A, expr: up == 0, for: 2m}\n" +
		"  - {<<: *a, alert: B}\n" +
		"  - {alert: C, <<: *a}\n" +
		"  - {expr: up == 1, <<: [{alert: D, expr: up == 2}, {alert: X, keep_firing_for: 1m}]}\n"
	want := `{"groups":[{"name":"g","rules":[` +
		`{"alert":"A","expr":"up == 0","for":"2m"},` +
		`{"alert":"B","expr":"up == 0","for":"2m"},` +
		`{"alert":"C","expr":"up == 0","for":"2m"},` +
		`{"expr":"up == 1","alert":"D","keep_firing_for":"1m"}]}]}`
	if got := ruleDocument(t, in); got != want {
		t.Fatalf("document %s\nwant     %s", got, want)
	}
	// Where the rules differ in what is written: the group's own one-rule
	// list wins over the merged two-rule list, and rulefmt reads one rule.
	// Composed by go-yaml v2's rules, the written list would be the merged
	// one and the walk would refuse it (2 entries written, 1 read).
	in = "groups:\n- {name: g, rules: [{record: 'r:own', expr: up}], " +
		"<<: {rules: [{record: 'r:m1', expr: up}, {record: 'r:m2', expr: up}]}}\n"
	want = `{"groups":[{"name":"g","rules":[{"record":"r:own","expr":"up"}]}]}`
	if got := ruleDocument(t, in); got != want {
		t.Fatalf("document %s\nwant     %s", got, want)
	}
}

// Every file gets an entry, in argument order; a refused file has a problem
// and no documents, and makes the exit 2 — with the whole JSON still printed.
func TestRulesReportEachFile(t *testing.T) {
	t.Parallel()
	good := "groups:\n- name: g\n  rules:\n  - {record: 'r:x', expr: up}\n"
	cases := []struct {
		name, in, wantErr string
	}{
		{"bad-promql", "groups:\n- name: g\n  rules:\n  - {alert: A, expr: 'rate(x[5m]'}\n", "parse error"},
		{"unknown-field", "groups:\n- name: g\n  rules:\n  - {alert: A, expr: up, fore: 1m}\n",
			"field fore not found"},
		{"no-name", "groups:\n- rules: [{alert: A, expr: up}]\n", "Groupname must not be empty"},
		{"experimental-function", "groups:\n- name: g\n  rules:\n  - {record: 'r:z', expr: 'sort_by_label(up, \"a\")'}\n",
			`function "sort_by_label" is not enabled`},
		// go-yaml v3 refuses a second `<<` in one mapping (go-yaml v2,
		// Prometheus' config loader, merges both).
		{"two-merge-keys", "groups:\n- name: g\n  rules:\n  - {<<: {for: 1m}, <<: {alert: E}, expr: up}\n",
			`mapping key "<<" already defined`},
		{"second-document", good + "---\ngroups: []\n",
			"YAML document 2 of the file has content; Prometheus loads only the first"},
		{"not-yaml", "groups: [\n", "refused by Prometheus' rule file loader"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			rc, out, paths, errOut := runRulesOn(t, good, tc.in, good)
			if rc != exitCallerErr || len(out.Files) != 3 {
				t.Fatalf("rc=%d out=%+v; want rc 2 and three entries", rc, out)
			}
			for i, f := range out.Files {
				if f.Path != paths[i] {
					t.Fatalf("entry %d path %q; want %q", i, f.Path, paths[i])
				}
			}
			if out.Files[0].Problem != nil || out.Files[2].Problem != nil ||
				len(out.Files[0].Documents) != 1 || len(out.Files[2].Documents) != 1 {
				t.Fatalf("the readable files: %+v", out.Files)
			}
			bad := out.Files[1]
			if bad.Problem == nil || bad.Documents != nil || !strings.Contains(bad.Problem.Message, tc.wantErr) {
				t.Fatalf("refused file: %+v; want a problem with %q and documents null", bad, tc.wantErr)
			}
			if !strings.HasPrefix(errOut, programName+": "+paths[1]+": ") {
				t.Fatalf("stderr %q; want it to name %s", errOut, paths[1])
			}
		})
	}
}

// A later document that holds nothing is accepted.
func TestRulesAcceptEmptyLaterDocuments(t *testing.T) {
	t.Parallel()
	in := "groups:\n- name: g\n  rules: []\n---\n# nothing\n---\nnull\n"
	if got := ruleDocument(t, in); got != `{"groups":[{"name":"g","rules":[]}]}` {
		t.Fatalf("document %s", got)
	}
}

// A file that cannot be opened is a problem of its own entry, not a usage
// error; `-` is standard input, named "<stdin>", at most once.
func TestRulesFilesAndStandardInput(t *testing.T) {
	t.Parallel()
	missing := filepath.Join(t.TempDir(), "missing.yml")
	var stdout, stderr bytes.Buffer
	rc := run([]string{"prometheus-rules", "-", missing}, strings.NewReader("groups: []\n"), &stdout, &stderr)
	var out rulesOutput
	if err := json.Unmarshal(stdout.Bytes(), &out); err != nil {
		t.Fatal(err)
	}
	if rc != exitCallerErr || len(out.Files) != 2 || out.Files[0].Path != stdinName ||
		out.Files[0].Problem != nil || string(out.Files[0].Documents[0]) != `{"groups":[]}` ||
		out.Files[1].Path != missing || out.Files[1].Problem == nil {
		t.Fatalf("rc=%d out=%s stderr=%q", rc, stdout.String(), stderr.String())
	}
	stdout.Reset()
	stderr.Reset()
	if rc := run([]string{"prometheus-rules", "-", "-"}, strings.NewReader(""), &stdout, &stderr); rc != exitCallerErr ||
		stdout.Len() != 0 || !strings.Contains(stderr.String(), "can be given once") {
		t.Fatalf("rc=%d stdout=%q stderr=%q; want a usage error", rc, stdout.String(), stderr.String())
	}
}
