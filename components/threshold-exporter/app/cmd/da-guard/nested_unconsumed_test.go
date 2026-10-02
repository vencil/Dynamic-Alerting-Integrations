package main

// nested_unconsumed_test.go — #2439: a `!!null x` (a tag yaml.v3's decode
// into `any` refuses; PyYAML and the route generator read it) in a nested
// `_` file the exporter never reads is not exit 3. A subtree domain policy
// or routing-profiles file is judged as the root one is — by the routing
// loader, as `_unusable` — and a file no reader consumes is not reported.
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
		{"subtree domain policy", "team/_domain_policy.yaml", policy, exitFindings, nil, "domain_policy_unusable team/_domain_policy.yaml"},
		{"subtree routing profiles", "team/_routing_profiles.yaml", profiles, exitOK, nil, "routing_profiles_unusable team/_routing_profiles.yaml"},
		{"subtree threshold profiles", "team/_profiles.yaml", "profiles:\n  gold:\n" + tag, exitOK, nil, ""},
		{"subtree notes", "team/_notes.yaml", "note: !!null x\n", exitOK, nil, ""},
		{"subtree defaults carrier", "team/_defaults.yaml", "defaults:\n  mysql_connections: 60\nnote: !!null x\n", exitParseFailed, []string{"team/_defaults.yaml"}, ""},
		{"subtree notes, syntax", "team/_notes.yaml", "note: [1,\n", exitParseFailed, []string{"team/_notes.yaml"}, ""},
		{"subtree domain policy, syntax", "team/_domain_policy.yaml", "domain_policies: [1,\n", exitParseFailed, []string{"team/_domain_policy.yaml"}, ""},
		// The root controls this file's behaviour is measured against.
		{"root domain policy", "_domain_policy.yaml", policy, exitFindings, nil, "domain_policy_unusable _domain_policy.yaml"},
		{"root routing profiles", "_routing_profiles.yaml", profiles, exitOK, nil, "routing_profiles_unusable _routing_profiles.yaml"},
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
			code, failed, findings := runDupTree(t, files)
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

// TestRun_NestedFileTheGeneratorRefusesForARepeatedKeyIsExitThree (#2439
// review F1): the route generator refuses a repeated key in any file of the
// tree, the nested `_` files the exporter never reads included. Since the
// exporter no longer rejects those, withGeneratorDuplicates names them:
// exit 3. served-values follows the exporter's load, which does not read
// them, so it lists none — except the root control, which the exporter does
// read and drops. A nested `_domain_policy` stays the routing loader's
// domain_policy_unusable (exit 1), as at the root.
func TestRun_NestedFileTheGeneratorRefusesForARepeatedKeyIsExitThree(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name, file, body string
		code             int
		failed           []string
		servedFailed     []string
		finding          string
	}{
		{"subtree notes", "team/_notes.yaml", "note: 1\nnote: 2\n", exitParseFailed, []string{"team/_notes.yaml"}, nil, ""},
		{"subtree notes, alias key", "team/_notes.yaml", "&q n : 1\n*q : 2\n", exitParseFailed, []string{"team/_notes.yaml"}, nil, ""},
		{"subtree threshold profiles", "team/_profiles.yaml", "profiles:\n  gold:\n    cpu: \"1\"\n    cpu: \"2\"\n", exitParseFailed, []string{"team/_profiles.yaml"}, nil, ""},
		{"subtree routing profiles", "team/_routing_profiles.yaml",
			"routing_profiles:\n  p1:\n    receiver: {type: webhook, url: 'https://t.example/p'}\n  p1:\n    receiver: {type: webhook, url: 'https://t.example/q'}\n",
			exitParseFailed, []string{"team/_routing_profiles.yaml"}, nil, ""},
		{"subtree domain policy", "team/_domain_policy.yaml", "domain_policies:\n  d1:\n    tenants: [ty]\n    tenants: [ty]\n",
			exitFindings, nil, nil, "domain_policy_unusable team/_domain_policy.yaml"},
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
			code, failed, findings := runDupTree(t, files)
			if code != tc.code || !reflect.DeepEqual(failed, tc.failed) {
				t.Errorf("exit %d, parse_failed %v; want exit %d, parse_failed %v (findings %v)",
					code, failed, tc.code, tc.failed, findings)
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
