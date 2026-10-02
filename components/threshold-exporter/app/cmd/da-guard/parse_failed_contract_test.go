package main

// parse_failed_contract_test.go — exit 3 is defined by the exporter, not by
// da-guard (#2179).
//
// The contract (docs/cli-reference.md §guard): da-guard exits 3 and names
// exactly the files the exporter's own load drops — config.LoadDir's
// parseFailed — plus the files da-guard itself cannot decode, plus the files
// it reads that the route generator refuses for a repeated key (#2295),
// restricted to the files that bear on --scope. Both halves are derived here independently
// of da-guard's output (LoadDir; the untyped yaml decode its chain merge
// uses), so a guard that starts judging "broken" on its own (another decode,
// a missed path, a flag that skips a read) turns it red instead of drifting.

import (
	"encoding/json"
	"path"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/internal/testutil"
	"github.com/vencil/threshold-exporter/pkg/config"
	"github.com/vencil/threshold-exporter/pkg/pyyamlcompat"
	"github.com/vencil/threshold-exporter/pkg/routingpolicy"
	"gopkg.in/yaml.v3"
)

// contractCases is #2179 step 0's matrix: one broken file per case, at every
// level the exporter treats differently (root platform files, a nested
// `_defaults.yaml`, a tenant file) × type error / syntax error / repeated key,
// plus the healthy controls. A nested `_defaults.yaml` type error is a
// control too: the exporter skips that key only, parseFailed is empty.
func contractCases() map[string]map[string]string {
	const (
		goodDefaults   = "defaults:\n  cpu: 70\n  mem: 80\n"
		tenantA        = "tenants:\n  tenant-a:\n    cpu: \"80\"\n"
		tenantAProfile = "tenants:\n  tenant-a:\n    cpu: \"80\"\n    _profile: gold\n"
		tenantB        = "tenants:\n  tenant-b:\n    mem: \"60\"\n"
	)
	base := func(over map[string]string) map[string]string {
		tree := map[string]string{
			"_defaults.yaml":   goodDefaults,
			"tenant-a.yaml":    tenantA,
			"db/tenant-b.yaml": tenantB,
			"empty/":           "",
		}
		for k, v := range over {
			tree[k] = v
		}
		return tree
	}
	return map[string]map[string]string{
		"OK_baseline":        base(nil),
		"OK_platform":        base(map[string]string{"_platform.yaml": "tenants:\n  tenant-a:\n    mem: \"44\"\n"}),
		"OK_profiles":        base(map[string]string{"_profiles.yaml": "profiles:\n  gold:\n    mem: \"33\"\n", "tenant-a.yaml": tenantAProfile}),
		"OK_nested_defaults": base(map[string]string{"db/_defaults.yaml": "defaults:\n  mem: 90\n"}),

		"R0_issue_shape":          {"_defaults.yaml": "defaults:\n  cpu: abc\n", "tenant-a.yaml": tenantA, "db/": "", "empty/": ""},
		"R1_root_defaults_type":   base(map[string]string{"_defaults.yaml": "defaults:\n  cpu: abc\n  mem: 80\n"}),
		"R2_root_defaults_syntax": base(map[string]string{"_defaults.yaml": "defaults:\n  cpu: [70\n  mem: 80\n"}),
		"R3_root_defaults_dupkey": base(map[string]string{"_defaults.yaml": "defaults:\n  cpu: 70\n  cpu: 75\n  mem: 80\n"}),
		"R4_root_max_metrics":     base(map[string]string{"_defaults.yaml": goodDefaults + "max_metrics_per_tenant: lots\n"}),

		"P1_platform_type":   base(map[string]string{"_platform.yaml": "tenants:\n  tenant-a: 5\n"}),
		"P2_platform_syntax": base(map[string]string{"_platform.yaml": "tenants:\n  tenant-a:\n    mem: [44\n"}),
		"P3_platform_dupkey": base(map[string]string{"_platform.yaml": "tenants:\n  tenant-a:\n    mem: \"44\"\n    mem: \"45\"\n"}),

		"F1_profiles_type":   base(map[string]string{"_profiles.yaml": "profiles:\n  gold: 5\n", "tenant-a.yaml": tenantAProfile}),
		"F2_profiles_syntax": base(map[string]string{"_profiles.yaml": "profiles:\n  gold:\n    mem: [33\n", "tenant-a.yaml": tenantAProfile}),
		"F3_profiles_dupkey": base(map[string]string{"_profiles.yaml": "profiles:\n  gold:\n    mem: \"33\"\n    mem: \"34\"\n", "tenant-a.yaml": tenantAProfile}),

		"N1_nested_defaults_type":   base(map[string]string{"db/_defaults.yaml": "defaults:\n  mem: abc\n  disk: 50\n"}),
		"N2_nested_defaults_syntax": base(map[string]string{"db/_defaults.yaml": "defaults:\n  mem: [90\n"}),
		"N3_nested_defaults_dupkey": base(map[string]string{"db/_defaults.yaml": "defaults:\n  mem: 90\n  mem: 91\n"}),
		// #2439: a tag the untyped decode refuses. The nested carrier is read
		// (dropped); a nested file the exporter never reads is not, unless
		// the parser itself refuses it.
		"N4_nested_defaults_tag": base(map[string]string{"db/_defaults.yaml": "defaults:\n  mem: 90\nnote: !!null x\n"}),
		"N5_nested_policy_tag":   base(map[string]string{"db/_domain_policy.yaml": "domain_policies:\n  d1:\n    tenants: [tenant-b]\n    note: !!null x\n"}),
		"N6_nested_notes_tag":    base(map[string]string{"db/_notes.yaml": "note: !!null x\n"}),
		"N7_nested_notes_syntax": base(map[string]string{"db/_notes.yaml": "note: [1,\n"}),

		"T1_tenant_type":   base(map[string]string{"db/tenant-b.yaml": "tenants:\n  tenant-b: 5\n"}),
		"T2_tenant_syntax": base(map[string]string{"db/tenant-b.yaml": "tenants:\n  tenant-b:\n    mem: [60\n"}),
		"T3_tenant_dupkey": base(map[string]string{"db/tenant-b.yaml": "tenants:\n  tenant-b:\n    mem: \"60\"\n    mem: \"61\"\n"}),
		// Control: the exporter keeps this file (parseFailed is empty).
		"T4_tenant_value_type": base(map[string]string{"db/tenant-b.yaml": "tenants:\n  tenant-b:\n    mem:\n      a: [1]\n"}),

		// The exporter keeps these (unknown top-level field; parseFailed is
		// empty) but da-guard's untyped decode rejects the repeated key (#2179).
		"U1_tenant_dupkey_unknown_field":        base(map[string]string{"tenant-a.yaml": tenantA + "extra: {k: 1, k: 2}\n"}),
		"U2_root_defaults_dupkey_unknown_field": base(map[string]string{"_defaults.yaml": goodDefaults + "extra: {k: 1, k: 2}\n"}),

		// yaml.v3 keeps these (an alias key beside its anchor is not a repeat
		// to it); the route generator refuses each file whole (#2295).
		"G1_tenant_alias_dupkey":           base(map[string]string{"db/tenant-b.yaml": "tenants:\n  tenant-b:\n    &m mem : \"60\"\n    *m : \"61\"\n"}),
		"G2_root_defaults_alias_dupkey":    base(map[string]string{"_defaults.yaml": "defaults:\n  &c cpu : 70\n  *c : 75\n  mem: 80\n"}),
		"G3_nested_defaults_alias_dupkey":  base(map[string]string{"db/_defaults.yaml": "defaults:\n  &m mem : 90\n  *m : 91\n"}),
		"G4_platform_alias_dupkey":         base(map[string]string{"_platform.yaml": "tenants:\n  tenant-a:\n    &m mem : \"44\"\n    *m : \"45\"\n"}),
		"G5_platform_merge_key_twice":      base(map[string]string{"_platform.yaml": "a: &x {mem: \"44\"}\nb: &y {cpu: \"45\"}\ntenants:\n  tenant-a:\n    <<: *x\n    <<: *y\n"}),
		"G6_domain_policy_alias_dupkey":    base(map[string]string{"_domain_policy.yaml": "domain_policies:\n  &d d1 :\n    tenants: [tenant-a]\n  *d :\n    tenants: [tenant-a]\n"}),
		"G7_tenant_merge_override_control": base(map[string]string{"tenant-a.yaml": "base: &b\n  cpu: \"70\"\ntenants:\n  tenant-a:\n    <<: *b\n    cpu: \"80\"\n"}),
	}
}

// guardUndecodable is the second half of the set: the files da-guard decodes
// untyped while merging the tenants under scopeRel ("" = whole tree) — each
// such tenant file and every `_defaults.yaml` on its chain (its directory and
// the ones above) — whose bytes that decode (the same yaml.Unmarshal into
// `any` as config.ParseChainDefaults / the tenant merge) rejects. No tenant
// under the scope means no merge, so nothing is decoded.
func guardUndecodable(tree map[string]string, scopeRel string) []string {
	isYAML := func(rel string) bool {
		ext := strings.ToLower(path.Ext(rel))
		return !strings.HasSuffix(rel, "/") && (ext == ".yaml" || ext == ".yml")
	}
	read := map[string]bool{}
	for rel := range tree {
		if !isYAML(rel) || strings.HasPrefix(path.Base(rel), "_") ||
			(scopeRel != "" && !strings.HasPrefix(rel, scopeRel+"/")) {
			continue
		}
		read[rel] = true
		for dir := path.Dir(rel); ; dir = path.Dir(dir) {
			read[path.Join(dir, "_defaults.yaml")] = true
			if dir == "." {
				break
			}
		}
	}
	var out []string
	for rel := range read {
		body, ok := tree[rel]
		if !ok {
			continue
		}
		var doc any
		if err := yaml.Unmarshal([]byte(body), &doc); err != nil {
			out = append(out, rel)
		}
	}
	return out
}

// generatorRefused is the third half (#2295): the files da-guard reads for
// the scope that the route generator refuses whole for a repeated mapping
// key — pyyamlcompat.FindDuplicateKeyIn, which its oracle table pins to the
// generator's StrictLoader. Read for the scope: each tenant file under it
// (every one in these trees declares a tenant), every `_defaults.yaml`
// bearing on it, and the root's other platform files except the
// `_domain_policy` / `_routing_profiles` files (routingpolicy names those as
// a Problem, never exit 3).
func generatorRefused(tree map[string]string, scopeRel string) []string {
	var out []string
	for rel, body := range tree {
		ext := strings.ToLower(path.Ext(rel))
		if strings.HasSuffix(rel, "/") || (ext != ".yaml" && ext != ".yml") {
			continue
		}
		base := path.Base(rel)
		read := false
		switch {
		case !strings.HasPrefix(base, "_"):
			read = scopeRel == "" || strings.HasPrefix(rel, scopeRel+"/")
		case base == "_defaults.yaml":
			read = bearsOnScope(rel, scopeRel)
		case path.Dir(rel) == ".":
			read = !routingpolicy.ReportsUnusable(base)
		}
		if read && pyyamlcompat.FindDuplicateKeyIn([]byte(body)) != nil {
			out = append(out, rel)
		}
	}
	return out
}

// bearsOnScope is the scope half of the contract, and only that half — what
// counts as broken is LoadDir's answer, never this function's. A dropped
// file bears on a run scoped to scopeRel ("" = whole tree) when it lies
// at-or-below the scope, or it is a `_` file in a directory above the scope
// (the root's platform files, a chain `_defaults.yaml`).
func bearsOnScope(key, scopeRel string) bool {
	if scopeRel == "" || strings.HasPrefix(key, scopeRel+"/") {
		return true
	}
	dir := path.Dir(key)
	return strings.HasPrefix(path.Base(key), "_") &&
		(dir == "." || strings.HasPrefix(scopeRel+"/", dir+"/"))
}

func TestExitThree_NamesExactlyTheFilesTheExporterDrops(t *testing.T) {
	t.Parallel()
	for name, tree := range contractCases() {
		name, tree := name, tree
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			tmp := t.TempDir()
			files := make(map[string]string, len(tree))
			for rel, body := range tree {
				files["conf.d/"+rel] = body
			}
			testutil.WriteTree(t, tmp, files)
			root := filepath.Join(tmp, "conf.d")

			// The oracle: the exporter's own cold load of the same tree.
			_, dropped, err := config.LoadDir(root, nil)
			if err != nil {
				t.Fatalf("LoadDir: %v", err)
			}

			for _, scopeRel := range []string{"", "db", "empty"} {
				expected := map[string]bool{}
				for _, k := range dropped {
					expected[k] = true
				}
				for _, k := range guardUndecodable(tree, scopeRel) {
					expected[k] = true
				}
				for _, k := range generatorRefused(tree, scopeRel) {
					expected[k] = true
				}
				want := []string{}
				for k := range expected {
					if bearsOnScope(k, scopeRel) {
						want = append(want, k)
					}
				}
				sort.Strings(want)
				for _, withLimit := range []bool{false, true} {
					args := []string{"--config-dir", root, "--format", "json"}
					if scopeRel != "" {
						args = append(args, "--scope", filepath.Join(root, filepath.FromSlash(scopeRel)))
					}
					if withLimit {
						args = append(args, "--cardinality-limit", "500")
					}
					label := "scope=" + scopeRel + " limit=" + map[bool]string{false: "no", true: "yes"}[withLimit]
					code, stdout, stderr := runOnce(t, args...)

					if len(want) == 0 {
						if code == exitParseFailed {
							t.Errorf("[%s] exit 3 but no file bears on this run. stdout=%q stderr=%q",
								label, stdout, stderr)
						}
						continue
					}
					if code != exitParseFailed {
						t.Errorf("[%s] exit = %d, want %d (expected %v). stdout=%q stderr=%q",
							label, code, exitParseFailed, want, stdout, stderr)
						continue
					}
					var doc struct {
						ParseFailed []string `json:"parse_failed"`
					}
					if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
						t.Errorf("[%s] invalid JSON: %v\n%s", label, err, stdout)
						continue
					}
					got := append([]string(nil), doc.ParseFailed...)
					sort.Strings(got)
					if !reflect.DeepEqual(got, want) {
						t.Errorf("[%s] parse_failed = %v, want %v (LoadDir parseFailed %v)",
							label, got, want, dropped)
					}
					for _, k := range want {
						if !strings.Contains(stderr, k) {
							t.Errorf("[%s] stderr should name %s: %q", label, k, stderr)
						}
					}
				}
			}
		})
	}
}

// The md report of an exit-3 run must not carry an all-clear: before #2179 a
// root `_defaults.yaml` with a type error plus --cardinality-limit printed
// "safe to merge", and the same file under an empty scope "vacuously safe".
func TestExitThree_RootDefaultsTypeError_IsNeverSafe(t *testing.T) {
	t.Parallel()
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml": "defaults:\n  cpu: abc\n",
		"conf.d/tenant-a.yaml":  "tenants:\n  tenant-a:\n    cpu: \"80\"\n",
		"conf.d/empty/":         "",
	})
	root := filepath.Join(tmp, "conf.d")
	for _, args := range [][]string{
		{"--config-dir", root},
		{"--config-dir", root, "--cardinality-limit", "500"},
		{"--config-dir", root, "--scope", filepath.Join(root, "empty"), "--cardinality-limit", "500"},
	} {
		code, stdout, stderr := runOnce(t, args...)
		if code != exitParseFailed {
			t.Errorf("%v: exit = %d, want %d. stdout=%q stderr=%q", args, code, exitParseFailed, stdout, stderr)
		}
		if strings.Contains(stdout, "safe to merge") || strings.Contains(stdout, "vacuously safe") {
			t.Errorf("%v: an exit-3 report must not call the change safe: %q", args, stdout)
		}
		if !strings.Contains(stdout, "`_defaults.yaml`") {
			t.Errorf("%v: report should name _defaults.yaml: %q", args, stdout)
		}
	}
}

// -h / --help print the usage and exit 0 (#2179: they printed nothing).
func TestRun_HelpPrintsUsage(t *testing.T) {
	t.Parallel()
	for _, arg := range []string{"-h", "--help", "-help"} {
		code, stdout, stderr := runOnce(t, arg)
		if code != exitOK {
			t.Errorf("%s: exit = %d, want %d", arg, code, exitOK)
		}
		if !strings.Contains(stdout+stderr, "Usage: "+programName) ||
			!strings.Contains(stdout+stderr, "-config-dir") {
			t.Errorf("%s: usage not printed. stdout=%q stderr=%q", arg, stdout, stderr)
		}
	}
}
