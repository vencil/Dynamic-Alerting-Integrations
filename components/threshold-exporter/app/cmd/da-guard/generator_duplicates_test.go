package main

import (
	"encoding/json"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/internal/testutil"
)

// runDupTree runs da-guard over files (under conf.d/) and returns the exit
// code, parse_failed, and each finding as "kind field".
func runDupTree(t *testing.T, files map[string]string) (int, []string, []string) {
	t.Helper()
	tmp := t.TempDir()
	tree := make(map[string]string, len(files)+1)
	tree["conf.d/_defaults.yaml"] = rsDefaults
	for k, v := range files {
		tree["conf.d/"+k] = v
	}
	testutil.WriteTree(t, tmp, tree)
	code, stdout, stderr := runOnce(t, "--config-dir", filepath.Join(tmp, "conf.d"), "--format", "json")
	var doc struct {
		ParseFailed []string `json:"parse_failed"`
		Report      *struct {
			Findings []jsonFinding `json:"findings"`
		} `json:"report"`
	}
	if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
		t.Fatalf("exit %d, report is not JSON: %v\nstdout=%s\nstderr=%s", code, err, stdout, stderr)
	}
	var findings []string
	if doc.Report != nil {
		for _, f := range doc.Report.Findings {
			findings = append(findings, f.Kind+" "+f.Field)
		}
	}
	return code, doc.ParseFailed, findings
}

// TestRun_GeneratorRefusesTheFileWhole (#2295): a key the route generator's
// StrictLoader counts as written twice — here an alias key beside its anchor,
// which yaml.v3 does not count — makes the generator refuse the WHOLE file,
// wherever the repeat sits and whichever copy is the bad one. da-guard takes
// the path a plain repeated key takes in the same file: a tenant, platform
// or defaults file is named in parse_failed (exit 3); a `_routing_profiles`
// / `_domain_policy` file is an `_unusable` finding (exit 1). Positions:
// every place revS10 measured da-guard letting one through.
func TestRun_GeneratorRefusesTheFileWhole(t *testing.T) {
	t.Parallel()
	const (
		bad  = "{type: webhook}"
		good = "{type: webhook, url: 'https://t.example/g'}"
		b    = "    mysql_connections: \"50\"\n"
		prof = "    _routing_profile: p1\n    _routing:\n      group_wait: 30s\n"
	)
	type pos struct {
		name string
		file string                              // the file holding the repeat
		tree func(x, y string) map[string]string // x written first, y second
	}
	tenant := func(routing string) map[string]string {
		return map[string]string{"tx.yaml": rsTenant + routing}
	}
	positions := []pos{
		{"receiver", "tx.yaml", func(x, y string) map[string]string {
			return tenant("    _routing:\n      &r receiver : " + x + "\n      *r : " + y + "\n")
		}},
		{"override receiver", "tx.yaml", func(x, y string) map[string]string {
			return tenant("    _routing:\n      receiver: " + good + "\n      overrides:\n        - alertname: X\n          &r receiver : " + x + "\n          *r : " + y + "\n")
		}},
		{"override alertname", "tx.yaml", func(x, _ string) map[string]string {
			return tenant("    _routing:\n      receiver: " + good + "\n      overrides:\n        - &n alertname : X\n          *n : Y\n          receiver: " + x + "\n")
		}},
		{"route receiver", "tx.yaml", func(x, y string) map[string]string {
			return tenant("    _routing:\n      receiver: " + good + "\n      routes:\n        - match: {team: a}\n          &r receiver : " + x + "\n          *r : " + y + "\n")
		}},
		{"overrides key", "tx.yaml", func(x, y string) map[string]string {
			return tenant("    _routing:\n      receiver: " + good + "\n      &o overrides :\n        - alertname: X\n          receiver: " + x + "\n      *o :\n        - alertname: X\n          receiver: " + y + "\n")
		}},
		{"routes key", "tx.yaml", func(x, y string) map[string]string {
			return tenant("    _routing:\n      receiver: " + good + "\n      &o routes :\n        - match: {team: a}\n          receiver: " + x + "\n      *o :\n        - match: {team: a}\n          receiver: " + y + "\n")
		}},
		{"receiver url", "tx.yaml", func(x, y string) map[string]string {
			u := map[string]string{bad: "x", good: "https://t.example/a"}
			return tenant("    _routing:\n      receiver:\n        type: webhook\n        &u url : " + u[x] + "\n        *u : " + u[y] + "\n")
		}},
		{"tenants key", "tx.yaml", func(x, y string) map[string]string {
			return map[string]string{"tx.yaml": "&t tenants :\n  tx:\n" + b + "    _routing:\n      receiver: " + x + "\n*t :\n  tx:\n" + b + "    _routing:\n      receiver: " + y + "\n"}
		}},
		{"tenant id", "tx.yaml", func(x, y string) map[string]string {
			return map[string]string{"tx.yaml": "tenants:\n  &a tx :\n" + b + "    _routing:\n      receiver: " + x + "\n  *a :\n" + b + "    _routing:\n      receiver: " + y + "\n"}
		}},
		{"_routing key", "tx.yaml", func(x, y string) map[string]string {
			return tenant("    &k _routing :\n      receiver: " + x + "\n    *k :\n      receiver: " + y + "\n")
		}},
		{"merge source", "tx.yaml", func(x, y string) map[string]string {
			return map[string]string{"tx.yaml": "x: &m\n  &r receiver : " + x + "\n  *r : " + y + "\ntenants:\n  tx:\n" + b + "    _routing:\n      <<: *m\n"}
		}},
		{"unrelated body key", "tx.yaml", func(x, _ string) map[string]string {
			return map[string]string{"tx.yaml": "tenants:\n  tx:\n    &q mysql_connections : \"50\"\n    *q : \"60\"\n    _routing:\n      receiver: " + good + "\n"}
		}},
		{"profile receiver", "_routing_profiles.yaml", func(x, y string) map[string]string {
			return map[string]string{"tx.yaml": rsTenant + prof, "_routing_profiles.yaml": "routing_profiles:\n  p1:\n    &r receiver : " + x + "\n    *r : " + y + "\n"}
		}},
		{"profile name", "_routing_profiles.yaml", func(x, y string) map[string]string {
			return map[string]string{"tx.yaml": rsTenant + prof, "_routing_profiles.yaml": "routing_profiles:\n  &p p1 :\n    receiver: " + x + "\n  *p :\n    receiver: " + y + "\n"}
		}},
		{"routing_profiles key", "_routing_profiles.yaml", func(x, y string) map[string]string {
			return map[string]string{"tx.yaml": rsTenant + prof, "_routing_profiles.yaml": "&p routing_profiles :\n  p1:\n    receiver: " + x + "\n*p :\n  p1:\n    receiver: " + y + "\n"}
		}},
		{"_routing_defaults key", "_rd.yaml", func(x, y string) map[string]string {
			return map[string]string{"tx.yaml": rsTenant + "    _routing:\n      group_wait: 30s\n", "_rd.yaml": "&d _routing_defaults :\n  receiver: " + x + "\n*d :\n  receiver: " + y + "\n"}
		}},
		{"_routing_defaults receiver", "_rd.yaml", func(x, y string) map[string]string {
			return map[string]string{"tx.yaml": rsTenant + "    _routing:\n      group_wait: 30s\n", "_rd.yaml": "_routing_defaults:\n  &r receiver : " + x + "\n  *r : " + y + "\n"}
		}},
		{"platform tenants key", "_platform.yaml", func(x, y string) map[string]string {
			return map[string]string{"tx.yaml": rsTenant, "_platform.yaml": "&t tenants :\n  tx:\n    _routing:\n      receiver: " + x + "\n*t :\n  tx:\n    _routing:\n      receiver: " + y + "\n"}
		}},
		{"platform tenant id", "_platform.yaml", func(x, y string) map[string]string {
			return map[string]string{"tx.yaml": rsTenant, "_platform.yaml": "tenants:\n  &a tx :\n    _routing:\n      receiver: " + x + "\n  *a :\n    _routing:\n      receiver: " + y + "\n"}
		}},
		{"platform _routing key", "_platform.yaml", func(x, y string) map[string]string {
			return map[string]string{"tx.yaml": rsTenant, "_platform.yaml": "tenants:\n  tx:\n    &k _routing :\n      receiver: " + x + "\n    *k :\n      receiver: " + y + "\n"}
		}},
		{"platform receiver", "_platform.yaml", func(x, y string) map[string]string {
			return map[string]string{"tx.yaml": rsTenant, "_platform.yaml": "tenants:\n  tx:\n    _routing:\n      &r receiver : " + x + "\n      *r : " + y + "\n"}
		}},
		{"platform override receiver", "_platform.yaml", func(x, y string) map[string]string {
			return map[string]string{"tx.yaml": rsTenant, "_platform.yaml": "tenants:\n  tx:\n    _routing:\n      receiver: " + good + "\n      overrides:\n        - alertname: X\n          &r receiver : " + x + "\n          *r : " + y + "\n"}
		}},
		{"defaults file", "_defaults.yaml", func(x, y string) map[string]string {
			return map[string]string{"tx.yaml": rsTenant + "    _routing:\n      receiver: " + good + "\n",
				"_defaults.yaml": "defaults:\n  &m mysql_connections : 80\n  *m : 90\n"}
		}},
	}
	for _, p := range positions {
		for order, xy := range map[string][2]string{"bad first": {bad, good}, "good first": {good, bad}} {
			name := p.name + ", " + order
			code, failed, findings := runDupTree(t, p.tree(xy[0], xy[1]))
			if p.file == "_routing_profiles.yaml" {
				if code != exitFindings || !containsPrefix(findings, "routing_profiles_unusable "+p.file) || len(failed) != 0 {
					t.Errorf("%s: exit %d, parse_failed %v, findings %v; want exit 1 with routing_profiles_unusable",
						name, code, failed, findings)
				}
				continue
			}
			if code != exitParseFailed || !reflect.DeepEqual(failed, []string{p.file}) {
				t.Errorf("%s: exit %d, parse_failed %v; want exit 3 naming %s", name, code, failed, p.file)
			}
		}
	}
}

// TestRun_DomainPolicyRepeatedKeyIsUnusable (#2295, revS10 e2eC): an alias
// key beside its anchor anywhere in `_domain_policy.yaml` makes the
// generator (--strict) refuse the file; da-guard names it
// domain_policy_unusable, as it names a plain repeated key there — never a
// policy silently read as yaml.v3 reads it (2e89719c: exit 0).
func TestRun_DomainPolicyRepeatedKeyIsUnusable(t *testing.T) {
	t.Parallel()
	tenant := map[string]string{"tx.yaml": rsTenant + "    _routing:\n      receiver: {type: webhook, url: 'https://t.example/g'}\n"}
	const c = "      forbidden_receiver_types: [webhook]\n"
	for name, pol := range map[string]string{
		"constraints key":      "domain_policies:\n  d1:\n    tenants: [tx]\n    &c constraints :\n" + c + "    *c :\n" + c,
		"forbidden key":        "domain_policies:\n  d1:\n    tenants: [tx]\n    constraints:\n      &f forbidden_receiver_types : [webhook]\n      *f : [webhook]\n",
		"tenants key":          "domain_policies:\n  d1:\n    &t tenants : [tx]\n    *t : [tx]\n    constraints:\n" + c,
		"domain_policies key":  "&d domain_policies :\n  d1:\n    tenants: [tx]\n    constraints:\n" + c + "*d :\n  d1:\n    tenants: [tx]\n    constraints:\n" + c,
		"domain name":          "domain_policies:\n  &n d1 :\n    tenants: [tx]\n    constraints:\n" + c + "  *n :\n    tenants: [tx]\n    constraints:\n" + c,
		"allowed key":          "domain_policies:\n  d1:\n    tenants: [tx]\n    constraints:\n      &f allowed_receiver_types : [email]\n      *f : [email]\n",
		"plain repeat control": "domain_policies:\n  d1:\n    tenants: [tx]\n    constraints:\n" + c + "    constraints:\n" + c,
	} {
		files := map[string]string{"_domain_policy.yaml": pol}
		for k, v := range tenant {
			files[k] = v
		}
		code, failed, findings := runDupTree(t, files)
		if code != exitFindings || len(failed) != 0 || !containsPrefix(findings, "domain_policy_unusable _domain_policy.yaml") {
			t.Errorf("%s: exit %d, parse_failed %v, findings %v; want exit 1 with domain_policy_unusable",
				name, code, failed, findings)
		}
	}
}

// TestRun_GeneratorAcceptsTheseRepeats: what the generator accepts stays
// accepted — a merge key overridden by an explicit key, one name in two
// mappings, an alias used as a value, a merge list.
func TestRun_GeneratorAcceptsTheseRepeats(t *testing.T) {
	t.Parallel()
	const g = "{type: webhook, url: 'https://t.example/g'}"
	for name, files := range map[string]map[string]string{
		"merge overridden": {"tx.yaml": "tenants:\n  ta: &b\n    mysql_connections: \"50\"\n    _routing:\n      receiver: " + g + "\n" +
			"  tx:\n    <<: *b\n    mysql_connections: \"60\"\n"},
		"same name, two mappings": {"tx.yaml": "tenants:\n  ta:\n    mysql_connections: \"50\"\n    _routing:\n      receiver: " + g +
			"\n  tx:\n    mysql_connections: \"50\"\n    _routing:\n      receiver: " + g + "\n"},
		"alias values": {"tx.yaml": rsTenant + "    _routing:\n      receiver: &r " + g + "\n      overrides:\n        - alertname: X\n          receiver: *r\n"},
		"merge list": {"tx.yaml": "tenants:\n  ta: &ba\n    mysql_connections: \"50\"\n  tb: &bb\n    mysql_connections: \"50\"\n    _routing:\n      receiver: " + g +
			"\n  tx:\n    <<: [*ba, *bb]\n"},
		"platform merge": {"tx.yaml": rsTenant, "_platform.yaml": "base: &b\n  _routing:\n    receiver: " + g + "\ntenants:\n  tx:\n    <<: *b\n"},
	} {
		if code, failed, findings := runDupTree(t, files); code != exitOK || len(failed) != 0 {
			t.Errorf("%s: exit %d, parse_failed %v, findings %v; want exit 0", name, code, failed, findings)
		}
	}
}

func containsPrefix(list []string, prefix string) bool {
	for _, s := range list {
		if strings.HasPrefix(s, prefix) {
			return true
		}
	}
	return false
}
