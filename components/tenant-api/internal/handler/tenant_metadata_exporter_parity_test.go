package handler

// #2830: tenant-api reads a tenant's `_metadata` as threshold-exporter does
// (/metrics' tenant_metadata_info, ResolveMetadata): a profile's `_metadata`
// the tenant file does not write, the string form, and a non-string scalar
// read as its text. The four tenants are the issue's table; t4 is the
// control every reader read before. threshold-exporter's
// metadata_layers_test.go pins the same shapes against LoadDir.

import (
	"net/http"
	"reflect"
	"sort"
	"testing"
)

// exporterParityTree is the issue's tree: one tenant per `_metadata` form.
func exporterParityTree() map[string]string {
	return map[string]string{
		"_profiles.yaml": "profiles:\n  pfin:\n    _metadata:\n      environment: production\n      domain: finance\n      owner: team-p\n",
		"t1.yaml":        "tenants:\n  t1:\n    _profile: pfin\n",
		"t2.yaml":        "tenants:\n  t2:\n    _metadata: \"environment: production\\ndomain: finance\\nowner: team-s\\n\"\n",
		"t3.yaml":        "tenants:\n  t3:\n    _metadata:\n      environment: 123\n      domain: finance\n      owner: 456\n",
		"t4.yaml":        "tenants:\n  t4:\n    _metadata:\n      environment: production\n      domain: finance\n      owner: team-c\n",
	}
}

// exporterParityWant is what threshold-exporter reads for each tenant.
var exporterParityWant = map[string]listedMetadata{
	"t1": {Environment: "production", Domain: "finance", Owner: "team-p"},
	"t2": {Environment: "production", Domain: "finance", Owner: "team-s"},
	"t3": {Environment: "123", Domain: "finance", Owner: "456"},
	"t4": {Environment: "production", Domain: "finance", Owner: "team-c"},
}

func TestListTenants_MetadataReadLikeExporter(t *testing.T) {
	t.Parallel()
	code, rows := listTenantRows(t, setupConfigDir(t, exporterParityTree()))
	if code != http.StatusOK {
		t.Fatalf("LIST status = %d, want 200", code)
	}
	if len(rows) != len(exporterParityWant) {
		t.Errorf("LIST rows = %v, want t1..t4", rows)
	}
	for id, want := range exporterParityWant {
		if got := metadataOf(rows[id]); !reflect.DeepEqual(got, want) {
			t.Errorf("%s metadata on LIST = %+v, want %+v", id, got, want)
		}
		if rows[id].MetadataIncomplete {
			t.Errorf("%s: metadata_incomplete set with a readable root", id)
		}
	}
}

func TestSearchTenants_MetadataReadLikeExporter(t *testing.T) {
	t.Parallel()
	dir := setupConfigDir(t, exporterParityTree())
	mgr := newRBACManager(t, "")
	search := func(query string) map[string]TenantSummary {
		t.Helper()
		resp, code, body := runSearch(t, dir, mgr, nil, query)
		if code != http.StatusOK {
			t.Fatalf("search %q: status %d: %s", query, code, body)
		}
		out := make(map[string]TenantSummary, len(resp.Items))
		for _, it := range resp.Items {
			out[it.ID] = it
		}
		return out
	}
	ids := func(m map[string]TenantSummary) []string {
		out := make([]string, 0, len(m))
		for id := range m {
			out = append(out, id)
		}
		sort.Strings(out)
		return out
	}

	all := search("")
	for id, want := range exporterParityWant {
		if got := metadataOf(all[id]); !reflect.DeepEqual(got, want) {
			t.Errorf("%s metadata on search = %+v, want %+v", id, got, want)
		}
	}
	for _, tc := range []struct {
		query string
		want  []string
	}{
		{"domain=finance", []string{"t1", "t2", "t3", "t4"}},
		{"environment=production", []string{"t1", "t2", "t4"}},
		{"environment=123", []string{"t3"}},
	} {
		if got := ids(search(tc.query)); !reflect.DeepEqual(got, tc.want) {
			t.Errorf("search %q = %v, want %v", tc.query, got, tc.want)
		}
	}
}

// The per-tenant readers — the tenant's file on disk (WriteScopeMeta) and a
// proposed body (proposedScopeMeta) — read the same environment / domain as
// LIST. For the proposed body every tenant's file on disk holds other values
// (environment / domain "on-disk"), so only a read of the body passes.
func TestPerTenantMetadataReaders_ReadLikeExporter(t *testing.T) {
	t.Parallel()
	files := exporterParityTree()
	onDisk := WriteScopeMeta(setupConfigDir(t, files))
	for id, want := range exporterParityWant {
		if env, domain := onDisk(id); env != want.Environment || domain != want.Domain {
			t.Errorf("on-disk read for %s = (%q, %q), want (%q, %q)", id, env, domain, want.Environment, want.Domain)
		}
	}

	other := map[string]string{"_profiles.yaml": files["_profiles.yaml"]}
	for id := range exporterParityWant {
		other[id+".yaml"] = "tenants:\n  " + id + ":\n    _metadata:\n      environment: on-disk\n      domain: on-disk\n"
	}
	dir := setupConfigDir(t, other)
	for id, want := range exporterParityWant {
		if env, domain := WriteScopeMeta(dir)(id); env != "on-disk" || domain != "on-disk" {
			t.Fatalf("fixture: on-disk read for %s = (%q, %q), want (on-disk, on-disk)", id, env, domain)
		}
		if env, domain := proposedScopeMeta(dir, files[id+".yaml"])(id); env != want.Environment || domain != want.Domain {
			t.Errorf("proposed-body read for %s = (%q, %q), want (%q, %q)", id, env, domain, want.Environment, want.Domain)
		}
	}
}
