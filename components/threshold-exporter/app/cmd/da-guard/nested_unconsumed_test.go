package main

// nested_unconsumed_test.go — #2439, revised by ADR-036 step 6 (#3206): a
// `!!null x` (a tag yaml.v3's decode into `any` refuses) or a repeated key in
// a nested `_` file the exporter never reads. #2439 kept it out of exit 3,
// and from #2904 to step 6 it was undetermined evidence (exit 4,
// `generator_unreadable`): parse-tree refuses it, and the route generator
// reads every nested `_` file through parse-tree. Since step 6 the exporter's
// nested probe asks parse-tree too, so such a file is a parse failure
// exactly like syntax damage: exit 3, in parse_failed — for da-guard and for
// served-values, which take the same list.

import (
	"reflect"
	"testing"
)

func TestRun_NestedFileParseTreeRefusesIsExitThree(t *testing.T) {
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
		{"subtree domain policy", "team/_domain_policy.yaml", policy, exitParseFailed, []string{"team/_domain_policy.yaml"}, ""},
		{"subtree routing profiles", "team/_routing_profiles.yaml", profiles, exitParseFailed, []string{"team/_routing_profiles.yaml"}, ""},
		{"subtree threshold profiles", "team/_profiles.yaml", "profiles:\n  gold:\n" + tag, exitParseFailed, []string{"team/_profiles.yaml"}, ""},
		{"subtree notes", "team/_notes.yaml", "note: !!null x\n", exitParseFailed, []string{"team/_notes.yaml"}, ""},
		{"subtree defaults carrier", "team/_defaults.yaml", "defaults:\n  mysql_connections: 60\nnote: !!null x\n", exitParseFailed, []string{"team/_defaults.yaml"}, ""},
		{"subtree notes, syntax", "team/_notes.yaml", "note: [1,\n", exitParseFailed, []string{"team/_notes.yaml"}, ""},
		{"subtree domain policy, syntax", "team/_domain_policy.yaml", "domain_policies: [1,\n", exitParseFailed, []string{"team/_domain_policy.yaml"}, ""},
		// The root controls this file's behaviour is measured against.
		{"root domain policy", "_domain_policy.yaml", policy, exitParseFailed, []string{"_domain_policy.yaml"}, ""},
		{"root routing profiles", "_routing_profiles.yaml", profiles, exitParseFailed, []string{"_routing_profiles.yaml"}, ""},
		// What parse-tree reads is not a parse failure, nested or not.
		{"subtree notes, readable", "team/_notes.yaml", "note: 1\n", exitOK, nil, ""},
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

// TestRun_NestedFileWithARepeatedKeyIsExitThree (#2439 review F1, #2905,
// #3206): the route generator refuses a repeated key in any file of the tree,
// the nested `_` files the exporter never reads included. Until #2905
// withGeneratorDuplicates named them in exit 3, then parse-tree as
// undetermined evidence (exit 4); since ADR-036 step 6 the exporter's nested
// probe refuses them on parse-tree's duplicate_key, so each is exit 3 and in
// served-values' parse_failed, like the root control.
func TestRun_NestedFileWithARepeatedKeyIsExitThree(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name, file, body string
		code             int
		failed           []string
		servedFailed     []string
		finding          string
	}{
		{"subtree notes", "team/_notes.yaml", "note: 1\nnote: 2\n", exitParseFailed, []string{"team/_notes.yaml"}, []string{"team/_notes.yaml"}, ""},
		{"subtree notes, alias key", "team/_notes.yaml", "&q n : 1\n*q : 2\n", exitParseFailed, []string{"team/_notes.yaml"}, []string{"team/_notes.yaml"}, ""},
		{"subtree threshold profiles", "team/_profiles.yaml", "profiles:\n  gold:\n    cpu: \"1\"\n    cpu: \"2\"\n",
			exitParseFailed, []string{"team/_profiles.yaml"}, []string{"team/_profiles.yaml"}, ""},
		{"subtree routing profiles", "team/_routing_profiles.yaml",
			"routing_profiles:\n  p1:\n    receiver: {type: webhook, url: 'https://t.example/p'}\n  p1:\n    receiver: {type: webhook, url: 'https://t.example/q'}\n",
			exitParseFailed, []string{"team/_routing_profiles.yaml"}, []string{"team/_routing_profiles.yaml"}, ""},
		{"subtree domain policy", "team/_domain_policy.yaml", "domain_policies:\n  d1:\n    tenants: [ty]\n    tenants: [ty]\n",
			exitParseFailed, []string{"team/_domain_policy.yaml"}, []string{"team/_domain_policy.yaml"}, ""},
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
			if containsPrefix(r.evidence, tc.file+" unreadable_outside_run") {
				t.Errorf("evidence %v names %s unreadable_outside_run though exit 3 names it (#3206)", r.evidence, tc.file)
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
