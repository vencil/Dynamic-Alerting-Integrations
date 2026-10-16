package main

// nested_unconsumed_test.go — #2439: a `!!null x` (a tag yaml.v3's decode
// into `any` refuses; PyYAML and the route generator read it) in a nested
// `_` file the exporter never reads is not exit 3. Since #2904 every such
// file is undetermined (exit 4): parse-tree refuses the tag, so the route
// generator's verdict is the one that counts (before, the routing loader
// judged a subtree domain policy or routing-profiles file as the root one,
// and a file no reader consumed was not reported).
// The subtree defaults carrier, which the exporter does read, stays exit 3;
// syntax damage stays exit 3 whatever the file. served-values takes the
// same parse_failed list.

import (
	"reflect"
	"testing"
)

func TestRun_NestedFileTheExporterDoesNotReadIsNotExitThree(t *testing.T) {
	t.Parallel()
	const tag = "    note: !!null x\n"
	policy := "domain_policies:\n  d1:\n    tenants: [tx]\n" + tag
	profiles := "routing_profiles:\n  p1:\n    receiver: {type: webhook, url: 'https://t.example/p'}\n" + tag
	cases := []struct {
		name, file, body string
		code             int
		failed           []string
		finding          string // a "kind field" prefix that must be present; "" = no finding at all
	}{
		{"subtree domain policy", "team/_domain_policy.yaml", policy, exitUndetermined, nil, ""},
		{"subtree routing profiles", "team/_routing_profiles.yaml", profiles, exitUndetermined, nil, ""},
		{"subtree threshold profiles", "team/_profiles.yaml", "profiles:\n  gold:\n" + tag, exitUndetermined, nil, ""},
		{"subtree notes", "team/_notes.yaml", "note: !!null x\n", exitUndetermined, nil, ""},
		{"subtree defaults carrier", "team/_defaults.yaml", "defaults:\n  mysql_connections: 60\nnote: !!null x\n", exitParseFailed, []string{"team/_defaults.yaml"}, ""},
		{"subtree notes, syntax", "team/_notes.yaml", "note: [1,\n", exitParseFailed, []string{"team/_notes.yaml"}, ""},
		{"subtree domain policy, syntax", "team/_domain_policy.yaml", "domain_policies: [1,\n", exitParseFailed, []string{"team/_domain_policy.yaml"}, ""},
		// The root controls this file's behaviour is measured against.
		{"root domain policy", "_domain_policy.yaml", policy, exitUndetermined, nil, ""},
		{"root routing profiles", "_routing_profiles.yaml", profiles, exitUndetermined, nil, ""},
	}
	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			files := map[string]string{
				"tx.yaml":      rsTenant,
				"team/ty.yaml": "tenants:\n  ty:\n    mysql_connections: \"60\"\n",
				tc.file:        tc.body,
			}
			r := runDupTree(t, files)
			code, failed, findings := r.code, r.parseFailed, r.findings
			if code != tc.code || !reflect.DeepEqual(failed, tc.failed) {
				t.Errorf("exit %d, parse_failed %v; want exit %d, parse_failed %v (findings %v)",
					code, failed, tc.code, tc.failed, findings)
			}
			if tc.finding == "" {
				if tc.code != exitParseFailed && len(findings) != 0 {
					t.Errorf("findings %v, want none", findings)
				}
			} else if !containsPrefix(findings, tc.finding) {
				t.Errorf("findings %v, want one starting %q", findings, tc.finding)
			}

			// served-values reads the same exporter build.
			sfiles := map[string]string{"_defaults.yaml": rsDefaults}
			for k, v := range files {
				sfiles[k] = v
			}
			scode, doc, _, stderr := served(t, sfiles, "")
			wantServed := exitOK
			if len(tc.failed) > 0 {
				wantServed = exitParseFailed
			}
			got := doc.ParseFailed
			if len(got) == 0 {
				got = nil
			}
			if scode != wantServed || !reflect.DeepEqual(got, tc.failed) {
				t.Errorf("served-values: exit %d, parse_failed %v; want exit %d, parse_failed %v; stderr=%q",
					scode, doc.ParseFailed, wantServed, tc.failed, stderr)
			}
		})
	}
}

// TestRun_NestedFileTheGeneratorRefusesForARepeatedKeyIsUndetermined (#2439
// review F1, #2905): the route generator refuses a repeated key in any file
// of the tree, the nested `_` files the exporter never reads included. The
// exporter does not reject those, so they are not exit 3; until #2905
// withGeneratorDuplicates named them there, now parse-tree does: each is
// undetermined evidence (duplicate_key, exit 4). served-values follows the
// exporter's load, which does not read them, so it lists none — except the
// root control, which the exporter does read and drops (exit 3 for both).
func TestRun_NestedFileTheGeneratorRefusesForARepeatedKeyIsUndetermined(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name, file, body string
		code             int
		failed           []string
		servedFailed     []string
		finding          string
	}{
		{"subtree notes", "team/_notes.yaml", "note: 1\nnote: 2\n", exitUndetermined, nil, nil, ""},
		{"subtree notes, alias key", "team/_notes.yaml", "&q n : 1\n*q : 2\n", exitUndetermined, nil, nil, ""},
		{"subtree threshold profiles", "team/_profiles.yaml", "profiles:\n  gold:\n    cpu: \"1\"\n    cpu: \"2\"\n", exitUndetermined, nil, nil, ""},
		{"subtree routing profiles", "team/_routing_profiles.yaml",
			"routing_profiles:\n  p1:\n    receiver: {type: webhook, url: 'https://t.example/p'}\n  p1:\n    receiver: {type: webhook, url: 'https://t.example/q'}\n",
			exitUndetermined, nil, nil, ""},
		{"subtree domain policy", "team/_domain_policy.yaml", "domain_policies:\n  d1:\n    tenants: [ty]\n    tenants: [ty]\n",
			exitUndetermined, nil, nil, ""},
		{"root notes control", "_notes.yaml", "note: 1\nnote: 2\n", exitParseFailed, []string{"_notes.yaml"}, []string{"_notes.yaml"}, ""},
	}
	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			files := map[string]string{
				"tx.yaml":      rsTenant,
				"team/ty.yaml": "tenants:\n  ty:\n    mysql_connections: \"60\"\n",
				tc.file:        tc.body,
			}
			r := runDupTree(t, files)
			code, failed, findings := r.code, r.parseFailed, r.findings
			if code != tc.code || !reflect.DeepEqual(failed, tc.failed) {
				t.Errorf("exit %d, parse_failed %v; want exit %d, parse_failed %v (findings %v)",
					code, failed, tc.code, tc.failed, findings)
			}
			if want := []string{tc.file + " duplicate_key"}; tc.code == exitUndetermined && !reflect.DeepEqual(r.unreadable, want) {
				t.Errorf("generator_unreadable %v, want %v", r.unreadable, want)
			}
			if tc.finding != "" && !containsPrefix(findings, tc.finding) {
				t.Errorf("findings %v, want one starting %q", findings, tc.finding)
			}

			sfiles := map[string]string{"_defaults.yaml": rsDefaults}
			for k, v := range files {
				sfiles[k] = v
			}
			scode, doc, _, stderr := served(t, sfiles, "")
			wantServed := exitOK
			if len(tc.servedFailed) > 0 {
				wantServed = exitParseFailed
			}
			got := doc.ParseFailed
			if len(got) == 0 {
				got = nil
			}
			if scode != wantServed || !reflect.DeepEqual(got, tc.servedFailed) {
				t.Errorf("served-values: exit %d, parse_failed %v; want exit %d, parse_failed %v; stderr=%q",
					scode, doc.ParseFailed, wantServed, tc.servedFailed, stderr)
			}
		})
	}
}
