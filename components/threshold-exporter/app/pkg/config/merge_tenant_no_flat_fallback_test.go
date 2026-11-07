package config

// #3504: a tenant's config comes only from a `tenants.<id>` block — the one
// place /metrics reads it, and the tenant-config schema's one top-level
// property. The byte entry point used to re-read a file that did not declare
// the requested tenant as a flat key-value document and file the result under
// that tenant: a second reader. A body declaring ANOTHER tenant came back
// under the requested id with a key named `tenants`, and ResolveAt served a
// row for it that /metrics never serves.

import (
	"strings"
	"testing"
	"time"
)

// Bodies that declare no `tenants.tx` block: flat key-value documents, and
// files that declare another tenant only.
var undeclaredTenantBodies = map[string]string{
	"flat thresholds":          "container_cpu: \"70\"\nmysql_threads_running: \"60\"\n",
	"flat refused + reserved":  "mysql_connections: abc\n_silent_mode: purple\n",
	"flat profile binding":     "_profile: strict\n",
	"declares another tenant":  "tenants:\n  other:\n    container_cpu: \"55\"\n",
	"another tenant, a secret": "tenants:\n  other:\n    x_note: SECRET\n",
}

func TestMergeTenantOverRootPlatform_UndeclaredTenantGetsNothing(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeMergeTree(t, dir, map[string]string{
		"_defaults.yaml": "defaults:\n  container_cpu: 80\n",
		"_profiles.yaml": "profiles:\n  strict:\n    mysql_connections: \"55\"\n",
	})
	root := LoadRootPlatform(dir)
	for name, body := range undeclaredTenantBodies {
		name, body := name, body
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			m := MergeTenantOverRootPlatform(root, "tx", []byte(body))
			if got, ok := m.Tenants["tx"]; ok {
				t.Fatalf("tx is not declared by the body, yet the merge holds it: %v", got)
			}
			if got, ok := m.own["tx"]; ok {
				t.Errorf("tx has a tenant layer the exporter never reads: %v", got)
			}
			for _, r := range m.ResolveAt(time.Date(2026, 7, 1, 12, 0, 0, 0, time.UTC)) {
				if r.Tenant == "tx" {
					t.Errorf("a row /metrics never serves: %+v", r)
				}
			}
			kv := m.ValidateTenantKeysByTenant()
			if v, ok := kv.Tenants["tx"]; ok && (len(v.Errors) > 0 || len(v.Values) > 0) {
				t.Errorf("findings for a tenant the body does not declare: %+v", v)
			}
			// Another tenant's content must not be quoted under tx.
			if strings.Contains(strings.Join(m.ValidateTenantKeys().Notices, "\n"), "SECRET") {
				t.Errorf("another tenant's value is quoted: %q", m.ValidateTenantKeys().Notices)
			}
		})
	}
}

// Both entry points answer the same: the parsed one never had the fallback.
func TestMergeTenantEntryPoints_AgreeOnAnUndeclaredTenant(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeMergeTree(t, dir, map[string]string{"_defaults.yaml": "defaults:\n  container_cpu: 80\n"})
	for name, body := range undeclaredTenantBodies {
		parsed, err := ParseConfigFile([]byte(body))
		if err != nil {
			t.Fatalf("%s: decode: %v", name, err)
		}
		_, inBytes := MergeTenantWithRootDefaults(dir, "tx", []byte(body)).Tenants["tx"]
		_, inParsed := MergeParsedTenantWithRootDefaults(dir, parsed).Tenants["tx"]
		if inBytes || inParsed {
			t.Errorf("%s: byte entry point holds tx %v, parsed %v; want neither", name, inBytes, inParsed)
		}
	}
}

// Control: the same keys under `tenants.tx` are the tenant's and are judged.
func TestMergeTenantOverRootPlatform_DeclaredTenantStillMergedAndJudged(t *testing.T) {
	t.Parallel()
	var root RootPlatform
	m := MergeTenantOverRootPlatform(root, "tx", []byte("tenants:\n  tx:\n    container_cpu: \"70\"\n    mysql_connections: abc\n"))
	if got := m.Tenants["tx"]["container_cpu"].Default; got != "70" {
		t.Fatalf("container_cpu = %q, want 70", got)
	}
	n := 0
	for _, f := range m.ValidateTenantKeysByTenant().Tenants["tx"].Values {
		if f.threshold {
			n++
		}
	}
	if n != 1 {
		t.Errorf("want the refused threshold judged once, got %d", n)
	}
}
