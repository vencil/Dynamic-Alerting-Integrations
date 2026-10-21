package main

// parse_verdict_exit_test.go — ADR-036 §9, step 6 (#3206): a file
// `parse-tree` refuses is one the exporter drops (pkg/config
// parse_verdict.go), so da-guard reports it under exit 3 — named in
// parse_failed, by the default check and by served-values alike — and no
// longer as undetermined evidence (`generator_unreadable`, exit 4, is gone).
// A tree whose only generator-judged content is routing stays exit 4.

import (
	"reflect"
	"sort"
	"testing"
)

// The nine shapes ADR-036 measured (seven the exporter read before step 6,
// two it already refused) and the control; pkg/config's
// parse_verdict_test.go asserts each kind against parse-tree.
var parseVerdictExitShapes = []struct{ name, extra string }{
	{"multiple documents", "---\nx: 1\n"},
	{"!!binary", "x: !!binary aGVsbG8=\n"},
	{"!!set", "x: !!set {a: null}\n"},
	{"BOM mid-file", "# note \uFEFF here\n"},
	{"null key", "~: 1\n"},
	{"non-specific tag", "x: ! \"y\"\n"},
	{"trailing --- only", "---\n"},
	{"duplicate key", "x: 1\nx: 2\n"},
	{"non-scalar key", "? [a]\n: 1\n"},
	{"control", ""},
}

func TestParseVerdictMatrix_ExitThree(t *testing.T) {
	t.Parallel()
	const (
		tenant1  = "tenants:\n  t1:\n    mysql_connections: \"80\"\n"
		tenant2  = "tenants:\n  t2:\n    mysql_connections: \"90\"\n"
		defaults = "defaults:\n  mysql_connections: 70\n"
	)
	for _, s := range parseVerdictExitShapes {
		for _, broken := range []string{"t1.yaml", "_defaults.yaml"} {
			s, broken := s, broken
			t.Run(s.name+"/"+broken, func(t *testing.T) {
				t.Parallel()
				files := map[string]string{"_defaults.yaml": defaults, "t1.yaml": tenant1, "t2.yaml": tenant2}
				files[broken] += s.extra
				control := s.extra == ""

				r := runDupTree(t, files)
				wantCode, wantFailed := exitParseFailed, []string{broken}
				if control {
					wantCode, wantFailed = exitOK, nil
				}
				if r.code != wantCode || !reflect.DeepEqual(r.parseFailed, wantFailed) || len(r.evidence) != 0 {
					t.Errorf("guard: exit %d, parse_failed %v, undetermined %v; want exit %d, parse_failed %v, none",
						r.code, r.parseFailed, r.evidence, wantCode, wantFailed)
				}

				code, doc, _, stderr := served(t, files, "")
				var tenants []string
				for id := range doc.Tenants {
					tenants = append(tenants, id)
				}
				sort.Strings(tenants)
				wantTenants := []string{"t1", "t2"}
				if !control && broken == "t1.yaml" {
					wantTenants = []string{"t2"}
				}
				got := doc.ParseFailed
				if len(got) == 0 {
					got = nil
				}
				if code != wantCode || !reflect.DeepEqual(got, wantFailed) || !reflect.DeepEqual(tenants, wantTenants) {
					t.Errorf("served-values: exit %d, parse_failed %v, tenants %v; want exit %d, %v, %v; stderr=%q",
						code, doc.ParseFailed, tenants, wantCode, wantFailed, wantTenants, stderr)
				}
			})
		}
	}
	// What stays exit 4: routing, which only the route generator judges.
	r := runDupTree(t, map[string]string{"t1.yaml": tenant1 + "    _routing:\n      group_wait: 30s\n"})
	if r.code != exitUndetermined || len(r.parseFailed) != 0 || !reflect.DeepEqual(r.evidence, []string{"t1.yaml routing_key"}) {
		t.Errorf("routing only: exit %d, parse_failed %v, undetermined %v; want 4, none, [t1.yaml routing_key]",
			r.code, r.parseFailed, r.evidence)
	}
}
