package config

// #3063: a scalar whose explicit tag its text does not satisfy
// (`mysql_connections: !!int zzleak42`). The typed decode never resolved the
// tag in ScheduledValue's scalar branch, nor read a top-level key
// ThresholdConfig has no field for, so ParseTenantFile accepted the file
// while the merge plane's generic decode (decodeTenantFile) refused it.
// ParseTenantFile now asks the generic decode too; these pin that its verdict
// is decodeTenantFile's on both sides of the line.

import (
	"fmt"
	"log"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

// mismatchedTagFiles are tenant files the typed decode alone accepts and the
// generic decode refuses.
var mismatchedTagFiles = map[string]string{
	"own threshold, !!int over text":        "tenants:\n  tx:\n    mysql_connections: !!int zzleak42\n",
	"sibling tenant's threshold":            "tenants:\n  tx:\n    mysql_connections: \"70\"\n  ty:\n    mysql_connections: !!int zzleak42\n",
	"profile body":                          "profiles:\n  gold:\n    mysql_connections: !!int zzleak42\ntenants:\n  tx:\n    _profile: gold\n",
	"top-level key without a field":         "tenants:\n  tx:\n    mysql_connections: \"70\"\nnotes: !!int zzleak42\n",
	"!!bool over a YAML 1.1 bool":           "tenants:\n  tx:\n    mysql_connections: !!bool yes\n",
	"!!timestamp over text":                 "tenants:\n  tx:\n    mysql_connections: !!timestamp zzleak42\n",
	"!!binary over text that is not base64": "tenants:\n  tx:\n    mysql_connections: !!binary zz*leak\n",
	"!!int over a float":                    "tenants:\n  tx:\n    mysql_connections: !!int 1.5\n",
	"reserved scalar key, !!int over text":  "tenants:\n  tx:\n    _profile: !!int zzleak42\n",
	"reserved key _silent_mode":             "tenants:\n  tx:\n    _silent_mode: !!int zzleak42\n",
}

// satisfiedTagFiles carry explicit tags their text satisfies; the scalar's
// text is what the typed decode keeps, as before.
var satisfiedTagFiles = map[string]struct{ file, want string }{
	"!!int 5":        {"tenants:\n  tx:\n    mysql_connections: !!int 5\n", "5"},
	"!!int 0x1F":     {"tenants:\n  tx:\n    mysql_connections: !!int 0x1F\n", "0x1F"},
	"!!str 5":        {"tenants:\n  tx:\n    mysql_connections: !!str 5\n", "5"},
	"!!float 1.5":    {"tenants:\n  tx:\n    mysql_connections: !!float 1.5\n", "1.5"},
	"!!float 5":      {"tenants:\n  tx:\n    mysql_connections: !!float 5\n", "5"},
	"!!bool true":    {"tenants:\n  tx:\n    _silent_mode: !!bool true\n", "true"},
	"local tag":      {"tenants:\n  tx:\n    mysql_connections: !custom zzleak42\n", "zzleak42"},
	"no tag at all":  {"tenants:\n  tx:\n    mysql_connections: zzleak42\n", "zzleak42"},
	"!!str over int": {"tenants:\n  tx:\n    mysql_connections: !!str 0x1F\n", "0x1F"},
}

func TestParseTenantFile_RejectsAnExplicitTagItsTextDoesNotSatisfy(t *testing.T) {
	t.Parallel()
	for name, file := range mismatchedTagFiles {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			// The shape this is about: the typed decode alone accepts it.
			if _, err := ParseConfigFile([]byte(file)); err != nil {
				t.Fatalf("precondition: ParseConfigFile rejects it already (%v); not the #3063 shape", err)
			}
			if _, err := decodeTenantFile([]byte(file)); err == nil {
				t.Fatalf("precondition: the generic decode accepts it")
			}
			cfg, err := ParseTenantFile([]byte(file))
			if err == nil {
				t.Fatalf("ParseTenantFile accepted a file the generic decode refuses; tenants %v", keysOfTenants(cfg))
			}
			if len(cfg.Tenants) != 0 || len(cfg.Profiles) != 0 {
				t.Errorf("a rejected file still hands back tenants %v / profiles %d", keysOfTenants(cfg), len(cfg.Profiles))
			}
		})
	}
}

func TestParseTenantFile_AcceptsAnExplicitTagItsTextSatisfies(t *testing.T) {
	t.Parallel()
	for name, c := range satisfiedTagFiles {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			if _, err := decodeTenantFile([]byte(c.file)); err != nil {
				t.Fatalf("precondition: the generic decode refuses it: %v", err)
			}
			cfg, err := ParseTenantFile([]byte(c.file))
			if err != nil {
				t.Fatalf("ParseTenantFile: %v", err)
			}
			var got []string
			for _, sv := range cfg.Tenants["tx"] {
				got = append(got, sv.Default)
			}
			if len(got) != 1 || got[0] != c.want {
				t.Errorf("tx values = %q, want [%q]", got, c.want)
			}
		})
	}
}

// The rule is "the generic decode's verdict", not a list of tags: over every
// file above, both directions agree with decodeTenantFile.
func TestParseTenantFile_AgreesWithTheGenericDecode(t *testing.T) {
	t.Parallel()
	all := map[string]string{}
	for k, v := range mismatchedTagFiles {
		all["mismatch/"+k] = v
	}
	for k, v := range satisfiedTagFiles {
		all["satisfied/"+k] = v.file
	}
	for name, file := range all {
		_, perr := ParseTenantFile([]byte(file))
		_, gerr := decodeTenantFile([]byte(file))
		if (perr == nil) != (gerr == nil) {
			t.Errorf("%s: ParseTenantFile err = %v, generic decode err = %v", name, perr, gerr)
		}
	}
}

// The walker's verdict follows (parseTenantDecls → ParseTenantFile): the file
// declares no tenant, is marked ParseFailed, is counted once and WARNed,
// while another file's tenant is untouched.
func TestScanDirTree_MismatchedTagFileDeclaresNoTenant(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	parityWrite(t, filepath.Join(dir, "_defaults.yaml"), "defaults:\n  mysql_connections: 50\n")
	parityWrite(t, filepath.Join(dir, "a.yaml"), mismatchedTagFiles["sibling tenant's threshold"])
	parityWrite(t, filepath.Join(dir, "b.yaml"), "tenants:\n  tb:\n    mysql_connections: \"60\"\n")
	obs := &recordingObserver{}
	var logs strings.Builder
	scan, err := ScanDirTree(dir, nil, obs, log.New(&logs, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	if f := scan.Files["a.yaml"]; f == nil || !f.ParseFailed || len(f.TenantIDs) != 0 {
		t.Fatalf("a.yaml = %+v, want ParseFailed and no tenant", f)
	}
	if _, ok := scan.Partials["a.yaml"]; ok {
		t.Errorf("a.yaml handed to the flat plane")
	}
	if f := scan.Files["b.yaml"]; f == nil || f.ParseFailed || fmt.Sprint(f.TenantIDs) != "[tb]" {
		t.Errorf("b.yaml = %+v, want tenant tb", f)
	}
	if !reflect.DeepEqual(obs.parseFailures, []string{"a.yaml"}) {
		t.Errorf("parse failures counted = %v, want a.yaml once", obs.parseFailures)
	}
	if !strings.Contains(logs.String(), `WARN: skip unparseable file`) || !strings.Contains(logs.String(), `a.yaml"`) {
		t.Errorf("no skip WARN naming a.yaml:\n%s", logs.String())
	}
}

func keysOfTenants(cfg ThresholdConfig) []string {
	var out []string
	for k := range cfg.Tenants {
		out = append(out, k)
	}
	return out
}

// #3063: what ParseTenantFile's generic decode of the parsed node costs, on a
// one-tenant file and on one file declaring 1000 tenants. ParseConfigFile
// over the same bytes is the baseline without it.
func benchTenantFile(tenants int) []byte {
	var b strings.Builder
	b.WriteString("tenants:\n")
	for i := 0; i < tenants; i++ {
		fmt.Fprintf(&b, "  t%04d:\n", i)
		b.WriteString("    mysql_connections: \"70\"\n")
		b.WriteString("    mysql_cpu: \"80\"\n")
		b.WriteString("    redis_memory:\n      default: \"75\"\n      overrides:\n        - window: \"22:00-06:00\"\n          value: \"90\"\n")
		b.WriteString("    _metadata:\n      owner: team-a\n      environment: production\n")
		b.WriteString("    _routing:\n      receiver:\n        type: webhook\n        url: https://example.invalid/hook\n      group_wait: 30s\n")
		b.WriteString("    _custom_alerts:\n      - recipe: threshold\n        name: a1\n        metric: m\n        op: \">\"\n        threshold: 5\n")
	}
	return []byte(b.String())
}

func benchmarkParse(b *testing.B, tenants int, parse func([]byte) (ThresholdConfig, error)) {
	data := benchTenantFile(tenants)
	b.SetBytes(int64(len(data)))
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		if _, err := parse(data); err != nil {
			b.Fatal(err)
		}
	}
}

func BenchmarkParseTenantFile_1Tenant(b *testing.B)       { benchmarkParse(b, 1, ParseTenantFile) }
func BenchmarkParseTenantFile_1Tenant_Typed(b *testing.B) { benchmarkParse(b, 1, ParseConfigFile) }
func BenchmarkParseTenantFile_1000Tenants(b *testing.B)   { benchmarkParse(b, 1000, ParseTenantFile) }
func BenchmarkParseTenantFile_1000Tenants_Typed(b *testing.B) {
	benchmarkParse(b, 1000, ParseConfigFile)
}
