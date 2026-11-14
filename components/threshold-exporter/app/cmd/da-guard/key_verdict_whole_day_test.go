package main

// #3835: the key verdict is judged over each tenant's whole UTC day; --at
// decides what has expired and nothing else. Every tree here is run at a
// minute inside its window (01:15) and one outside it (12:00) of the same
// day: the documents must be byte-for-byte the same but for `at`, and
// exit 0. Tenant ids are fixture names (CLAUDE.md #9).

import (
	"bytes"
	"encoding/json"
	"regexp"
	"strings"
	"testing"
)

const (
	kvInWindow  = "2026-11-13T01:15:00Z"
	kvOutWindow = "2026-11-13T12:00:00Z"
)

// wholeDayRoot declares cpu_pct (not a retired key) at the root.
const wholeDayRoot = "defaults:\n  cpu_pct: 80\n"

// kvAtLine is the document's `at` line, the one field --at writes as given.
var kvAtLine = regexp.MustCompile(`(?m)^  "at": "[^"]*",$`)

// keyVerdictAt is `da-guard key-verdict` over files at at: its exit code,
// its stdout with the `at` line blanked, and the document.
func keyVerdictAt(t *testing.T, files map[string]string, at string) (int, string, map[string]any) {
	t.Helper()
	root := writeTree(t, files)
	var out, errOut bytes.Buffer
	code := run([]string{keyVerdictCmd, "--config-dir", root, "--at", at}, &out, &errOut)
	var doc map[string]any
	if err := json.Unmarshal(out.Bytes(), &doc); err != nil {
		t.Fatalf("at %s: exit %d, not JSON: %v\nstderr: %s", at, code, err, errOut.String())
	}
	if n := len(kvAtLine.FindAllString(out.String(), -1)); n != 1 {
		t.Fatalf("at %s: %d `at` lines in\n%s", at, n, out.String())
	}
	return code, kvAtLine.ReplaceAllString(out.String(), `  "at": "",`), doc
}

// kvRow is tenant ta's row for key in doc.
func kvRow(t *testing.T, doc map[string]any, key string) map[string]any {
	t.Helper()
	for _, r := range doc["tenants"].(map[string]any)["ta"].([]any) {
		if row := r.(map[string]any); row["key"] == key {
			return row
		}
	}
	t.Fatalf("no row for %s in %v", key, doc["tenants"])
	return nil
}

func kvFindings(doc map[string]any) []string {
	var out []string
	for _, f := range doc["findings"].([]any) {
		m := f.(map[string]any)
		out = append(out, m["severity"].(string)+" "+m["kind"].(string)+" "+m["key"].(string))
	}
	return out
}

func TestKeyVerdict_TheTimeOfDayChangesNothing(t *testing.T) {
	t.Parallel()
	ta := func(body string) map[string]string {
		return map[string]string{"_defaults.yaml": wholeDayRoot, "ta.yaml": "tenants:\n  ta:\n" + body}
	}
	cases := []struct {
		name  string
		files map[string]string
		// check: what the (one) document says.
		check func(t *testing.T, doc map[string]any)
	}{
		{
			// The issue's own tree: in the window cpu_pct ("70:critical")
			// and cpu_pct_critical give one series, so /metrics cannot be
			// gathered there. Both keys are read (the rows built before the
			// Gather); the collision is the main gate's. Before #3835: rc 2
			// inside the window, a disable_noop warning outside it.
			"two keys colliding inside a window",
			ta("    cpu_pct: \"70:critical\"\n    cpu_pct_critical:\n      default: disable\n" +
				"      overrides:\n        - window: \"01:00-01:59\"\n          value: \"95\"\n"),
			func(t *testing.T, doc map[string]any) {
				for _, k := range []string{"cpu_pct", "cpu_pct_critical"} {
					if r := kvRow(t, doc, k); r["verdict"] != "read" {
						t.Errorf("%s: %v", k, r)
					}
				}
				if fs := kvFindings(doc); len(fs) != 0 {
					t.Errorf("findings %v", fs)
				}
			},
		},
		{
			// A: a key the root does not declare, `disable` outside its
			// windows, a dead window. No part of the day has a row for it;
			// the window's no_row is the reason, not the noop disable, and
			// its dead entry is not judged on top of the unread error.
			// Before #3835: a dead_entry error only outside the window.
			"an undeclared key, disabled outside its windows, with a dead entry",
			ta("    nodecl_pct:\n      default: disable\n      overrides:\n" +
				"        - window: \"01:00-01:59\"\n          value: \"5\"\n" +
				"        - window: \"01:10-01:20\"\n          value: \"6\"\n"),
			func(t *testing.T, doc map[string]any) {
				r := kvRow(t, doc, "nodecl_pct")
				if r["verdict"] != "unread" || r["reason"] != "no_row" || r["severity"] != "error" ||
					len(r["dead_entries"].([]any)) != 1 {
					t.Errorf("nodecl_pct: %v", r)
				}
				if fs := kvFindings(doc); strings.Join(fs, ";") != "error unread nodecl_pct" {
					t.Errorf("findings %v", fs)
				}
			},
		},
		{
			// C2: a window value the resolver refuses ("abc") and a dead
			// window. The key is read (its value is the main gate's), and
			// its dead entry is an error all day. Before #3835: unread
			// (value_unparsed) inside the window.
			"a refused window value and a dead entry",
			ta("    cpu_pct_critical:\n      default: \"95\"\n      overrides:\n" +
				"        - window: \"01:00-01:59\"\n          value: \"abc\"\n" +
				"        - window: \"01:10-01:20\"\n          value: \"96\"\n"),
			func(t *testing.T, doc map[string]any) {
				if r := kvRow(t, doc, "cpu_pct_critical"); r["verdict"] != "read" {
					t.Errorf("cpu_pct_critical: %v", r)
				}
				if fs := kvFindings(doc); strings.Join(fs, ";") != "error dead_entry cpu_pct_critical" {
					t.Errorf("findings %v", fs)
				}
			},
		},
		{
			// E: the root switches cpu_pct off; the tenant serves 70 and
			// switches it off in a window — read, the window's `disable` a
			// noop. Before #3835: disable_noop inside the window only.
			"a noop disable in a window of a read key",
			map[string]string{"_defaults.yaml": "defaults:\n  cpu_pct: disable\n",
				"ta.yaml": "tenants:\n  ta:\n    cpu_pct:\n      default: \"70\"\n      overrides:\n" +
					"        - window: \"01:00-01:59\"\n          value: disable\n"},
			func(t *testing.T, doc map[string]any) {
				if r := kvRow(t, doc, "cpu_pct"); r["verdict"] != "read" || r["disable"] != "noop" {
					t.Errorf("cpu_pct: %v", r)
				}
				if fs := kvFindings(doc); len(fs) != 0 {
					t.Errorf("findings %v", fs)
				}
			},
		},
		{
			// A base only `dimensions:` declares (no root value): no row in
			// any part of the day it reads a value (no_row), but inside the
			// window its `disable` switches its dimensional key off — an
			// effective `disable`, so the exporter read the base. Read, with
			// whatever reason the other parts give (#3835 review): the key's
			// day reason is no_row, not disabled.
			"an effective disable in a window, no row elsewhere",
			map[string]string{"_defaults.yaml": wholeDayRoot + "dimensions:\n  nodecl_pct: [db]\n",
				"ta.yaml": "tenants:\n  ta:\n    nodecl_pct:\n      default: \"7\"\n      overrides:\n" +
					"        - window: \"01:00-01:59\"\n          value: disable\n" +
					"    nodecl_pct{db=\"x\"}: \"5\"\n"},
			func(t *testing.T, doc map[string]any) {
				r := kvRow(t, doc, "nodecl_pct")
				if r["verdict"] != "read" || r["disable"] != "effective" || r["reason"] != nil {
					t.Errorf("nodecl_pct: %v", r)
				}
				if fs := kvFindings(doc); strings.Join(fs, ";") != `warning disable_cascade nodecl_pct{db="x"}` {
					t.Errorf("findings %v", fs)
				}
			},
		},
		{
			// H: a `_critical` served only in a window. Before #3835:
			// disable_noop outside the window only.
			"a critical tier served only in a window",
			ta("    cpu_pct_critical:\n      default: disable\n      overrides:\n" +
				"        - window: \"01:00-01:59\"\n          value: \"95\"\n"),
			func(t *testing.T, doc map[string]any) {
				if r := kvRow(t, doc, "cpu_pct_critical"); r["verdict"] != "read" {
					t.Errorf("cpu_pct_critical: %v", r)
				}
				if fs := kvFindings(doc); len(fs) != 0 {
					t.Errorf("findings %v", fs)
				}
			},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			inCode, in, doc := keyVerdictAt(t, tc.files, kvInWindow)
			outCode, out, _ := keyVerdictAt(t, tc.files, kvOutWindow)
			if inCode != exitOK || outCode != exitOK {
				t.Fatalf("exit %d at %s, %d at %s", inCode, kvInWindow, outCode, kvOutWindow)
			}
			if in != out {
				t.Fatalf("the document differs between %s and %s:\n%s\n---\n%s", kvInWindow, kvOutWindow, in, out)
			}
			tc.check(t, doc)
		})
	}
}

// `expires:` is the one input --at still decides: a base switched off until
// an instant switches its `_critical` off with it before that instant
// (disable_cascade) and not after it.
func TestKeyVerdict_ExpiresStillDependsOnAt(t *testing.T) {
	t.Parallel()
	files := map[string]string{"_defaults.yaml": wholeDayRoot,
		"ta.yaml": "tenants:\n  ta:\n    cpu_pct:\n      default: disable\n      expires: \"2026-11-13T06:00:00Z\"\n" +
			"      reason: fixture\n    cpu_pct_critical: \"95\"\n"}
	_, before, bdoc := keyVerdictAt(t, files, kvInWindow)
	_, after, adoc := keyVerdictAt(t, files, kvOutWindow)
	if before == after {
		t.Fatal("the same document before and after the `expires:` instant")
	}
	if r := kvRow(t, bdoc, "cpu_pct"); r["disable"] != "effective" {
		t.Errorf("before: cpu_pct %v", r)
	}
	if fs := kvFindings(bdoc); strings.Join(fs, ";") != "warning disable_cascade cpu_pct_critical" {
		t.Errorf("before: findings %v", fs)
	}
	if r := kvRow(t, adoc, "cpu_pct"); r["disable"] != "noop" {
		t.Errorf("after: cpu_pct %v", r)
	}
	if fs := kvFindings(adoc); len(fs) != 0 {
		t.Errorf("after: findings %v", fs)
	}
}

// No row of the document carries what /metrics served at --at (#3835: v3
// has no `served`).
func TestKeyVerdict_NoFieldOfOneInstant(t *testing.T) {
	t.Parallel()
	_, _, doc := keyVerdictAt(t, map[string]string{"_defaults.yaml": wholeDayRoot,
		"ta.yaml": "tenants:\n  ta:\n    cpu_pct: \"70\"\n"}, kvOutWindow)
	if doc["schema"] != keyVerdictSchema || keyVerdictSchema != "da-guard.key-verdict/v3" {
		t.Errorf("schema %v", doc["schema"])
	}
	if r := kvRow(t, doc, "cpu_pct"); r["verdict"] != "read" {
		t.Errorf("row %v", r)
	}
	if _, has := kvRow(t, doc, "cpu_pct")["served"]; has {
		t.Error("a row carries `served`")
	}
}
