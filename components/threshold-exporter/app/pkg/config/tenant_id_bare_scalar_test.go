package config

// #2118 — a `tenants:` key written as a bare scalar that YAML types as
// something other than a string (`010`, `0x1`, `007`, `1.0`, a date…).
// The flat plane (/metrics, LoadDir) keys the tenant by the key's TEXT, so
// it serves tenant `010`; the walker plane (/effective, tenant-api,
// da-guard) decoded the same key as an int and re-spelled it (`010` → "8"),
// so the lookup by "010" answered `tenant "010" not in file` and da-guard
// failed the whole scope. The oracle here is the flat plane: every tenant
// LoadDir serves must resolve, with the value /metrics emits.

import (
	"fmt"
	"reflect"
	"strconv"
	"testing"
	"time"

	"gopkg.in/yaml.v3"
)

// servedMySQLConnections is the flat plane's answer (LoadDir → ResolveAt,
// what /metrics emits) for tenantID's warning mysql_connections threshold.
func servedMySQLConnections(t *testing.T, dir, tenantID string) float64 {
	t.Helper()
	cfg, _, err := LoadDir(dir, nil)
	if err != nil {
		t.Fatalf("LoadDir: %v", err)
	}
	if _, ok := cfg.Tenants[tenantID]; !ok {
		var got []string
		for k := range cfg.Tenants {
			got = append(got, k)
		}
		t.Fatalf("oracle: the flat plane does not serve tenant %q (it serves %q) — the case tests nothing", tenantID, got)
	}
	for _, r := range cfg.ResolveAt(time.Now()) {
		if r.Tenant == tenantID && r.Component == "mysql" && r.Severity == "warning" {
			return r.Value
		}
	}
	t.Fatalf("oracle: no mysql warning threshold served for tenant %q", tenantID)
	return 0
}

func effectiveMySQLConnections(t *testing.T, ec *EffectiveConfig) float64 {
	t.Helper()
	v, ok := ec.EffectiveConfig["mysql_connections"]
	if !ok {
		t.Fatalf("effective config of %q has no mysql_connections: %v", ec.TenantID, ec.EffectiveConfig)
	}
	f, err := strconv.ParseFloat(fmt.Sprint(v), 64)
	if err != nil {
		t.Fatalf("effective mysql_connections of %q = %v: %v", ec.TenantID, v, err)
	}
	return f
}

// assertWalkerAgreesWithFlatPlane: ResolveEffective (/effective, tenant-api)
// and ScopeEffective (da-guard) both find tenantID and give the value the
// flat plane serves.
func assertWalkerAgreesWithFlatPlane(t *testing.T, dir, tenantID string, want float64) {
	t.Helper()
	if served := servedMySQLConnections(t, dir, tenantID); served != want {
		t.Fatalf("oracle: flat plane serves %v for %q, case expects %v", served, tenantID, want)
	}
	ec, err := ResolveEffective(dir, tenantID)
	if err != nil {
		t.Fatalf("ResolveEffective(%q): %v — the flat plane serves this tenant", tenantID, err)
	}
	if got := effectiveMySQLConnections(t, ec); got != want {
		t.Fatalf("ResolveEffective(%q) mysql_connections = %v, flat plane serves %v", tenantID, got, want)
	}
	scoped, err := ScopeEffective(dir, dir)
	if err != nil {
		t.Fatalf("ScopeEffective: %v — the flat plane serves tenant %q", err, tenantID)
	}
	for _, s := range scoped.Tenants {
		if s.TenantID == tenantID {
			if got := effectiveMySQLConnections(t, s); got != want {
				t.Fatalf("ScopeEffective %q mysql_connections = %v, flat plane serves %v", tenantID, got, want)
			}
			return
		}
	}
	t.Fatalf("ScopeEffective does not list tenant %q", tenantID)
}

func TestBareScalarTenantKeyResolvesLikeTheFlatPlane(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name     string
		key      string // the `tenants:` key exactly as written
		id       string // the tenant id the flat plane serves
		platform string // the root platform file's `tenants:` block, if any
		want     float64
	}{
		// Controls: a string-typed key resolved before #2118.
		{name: "control-quoted-010", key: `"010"`, id: "010", want: 60},
		{name: "control-name", key: "tx", id: "tx", want: 60},
		{name: "control-yes", key: "yes", id: "yes", want: 60},
		// The bug: an int-typed key.
		{name: "octal-010", key: "010", id: "010", want: 60},
		{name: "hex-0x1", key: "0x1", id: "0x1", want: 60},
		{name: "leading-zero-007", key: "007", id: "007", want: 60},
		// Both files write the bare key; the platform value fills a key the
		// tenant file does not write (#2019's layer, same id matching).
		{
			name: "octal-both", key: "010", id: "010",
			platform: "tenants:\n  010:\n    mysql_connections: \"55\"\n",
			want:     55,
		},
		// Two keys the int decode collapses onto one ("8"): each is its own
		// tenant in the flat plane.
		{name: "octal-vs-decimal-8", key: "010", id: "010", want: 60},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			tenantBody := "    mysql_connections: \"60\"\n"
			if tc.platform != "" {
				tenantBody = "    _metadata:\n      owner: x\n"
			}
			files := map[string]string{
				"_defaults.yaml": "defaults:\n  mysql_connections: 80\n" + tc.platform,
				"tx.yaml":        "tenants:\n  " + tc.key + ":\n" + tenantBody,
			}
			if tc.name == "octal-vs-decimal-8" {
				files["tx.yaml"] += "  8:\n    mysql_connections: \"8\"\n"
			}
			dir := writePlatformTree(t, files)
			assertWalkerAgreesWithFlatPlane(t, dir, tc.id, tc.want)
			if tc.name == "octal-vs-decimal-8" {
				assertWalkerAgreesWithFlatPlane(t, dir, "8", 8)
			}
		})
	}
}

// TestDecodeTenantFileIsTheGenericDecodeElsewhere: decodeTenantFile changes
// nothing but the `tenants:` keys — same result where every key is a string,
// same error (text included) where the generic decode fails, and the
// generic re-spelling kept for keys it does not re-key (inside a tenant's
// block, outside `tenants:`, a merge key).
func TestDecodeTenantFileIsTheGenericDecodeElsewhere(t *testing.T) {
	t.Parallel()
	inputs := []string{
		"",
		"---\n",
		"# only a comment\n",
		"tenants:\n  t1:\n    cpu: \"1\"\n    010: x\n  t2:\n",
		"defaults:\n  010: 5\ntenants:\n  t1: {}\n",
		"tenants: [a, b]\n",
		"tenants:\n",
		"- a\n- b\n",
		"tenants:\n  t1: {}\n  t1: {}\n",   // duplicate key
		"tenants:\n  010: {}\n  010: {}\n", // duplicate bare key
		"tenants:\n  t1: {a: 1\n",          // syntax error
		"base: &b {cpu: \"1\"}\ntenants:\n  <<: {t9: {mem: \"2\"}}\n  t1: *b\n",
		"tenants: &t {t1: {cpu: \"1\"}}\nother: *t\n",
		"tenants:\n  t1: {}\n---\ntenants:\n  010: {}\n", // second document ignored
	}
	for _, in := range inputs {
		var raw any
		wantErr := yaml.Unmarshal([]byte(in), &raw)
		got, gotErr := decodeTenantFile([]byte(in))
		if (wantErr == nil) != (gotErr == nil) || (wantErr != nil && wantErr.Error() != gotErr.Error()) {
			t.Errorf("%q: error %v, generic decode %v", in, gotErr, wantErr)
			continue
		}
		if wantErr != nil {
			continue
		}
		if want := normalizeYAMLToJSON(raw); !reflect.DeepEqual(got, want) {
			t.Errorf("%q: decoded %#v, generic decode %#v", in, got, want)
		}
	}
}

// TestTenantIDSpellingMatrix_WalkerResolvesTheExporterKey: every row of the
// shared spelling matrix (#2114) whose key the exporter keeps must resolve
// on the walker plane under that same key, with the value the flat plane
// serves.
func TestTenantIDSpellingMatrix_WalkerResolvesTheExporterKey(t *testing.T) {
	t.Parallel()
	m := loadTenantIDSpellingMatrix(t)
	ran := 0
	for _, row := range m.Spellings {
		if row.Rejected || row.ExporterKey == nil || *row.ExporterKey == "" {
			// Rejected / dropped keys have no tenant to resolve; the empty
			// id is not a tenant id any reader accepts by name.
			continue
		}
		ran++
		t.Run(row.Source, func(t *testing.T) {
			t.Parallel()
			dir := writePlatformTree(t, map[string]string{
				"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
				"tx.yaml":        "tenants:\n  " + row.Source + ":\n    mysql_connections: \"1\"\n",
			})
			assertWalkerAgreesWithFlatPlane(t, dir, *row.ExporterKey, 1)
		})
	}
	if ran == 0 {
		t.Fatal("no matrix row exercised — a vacuous table passes nothing")
	}
}
