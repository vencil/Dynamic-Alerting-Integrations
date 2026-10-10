package handler

// #2370: a tenant's metadata on GET /api/v1/tenants is the root platform
// files' `tenants.<id>._metadata` merged per key under the tenant file's own
// `_metadata` — the value /metrics resolves for the same tree
// (threshold-exporter's config_metadata_layers_test.go runs the same shapes).

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

// listedMetadata is the metadata part of one LIST row.
type listedMetadata struct {
	Environment, Domain, DBType, Owner, Tier string
	Tags                                     []string
}

func metadataOf(s TenantSummary) listedMetadata {
	return listedMetadata{Environment: s.Environment, Domain: s.Domain, DBType: s.DBType,
		Owner: s.Owner, Tier: s.Tier, Tags: s.Tags}
}

// listTenantRows calls the LIST handler over configDir and returns the
// response code and the rows by id.
func listTenantRows(t *testing.T, configDir string) (int, map[string]TenantSummary) {
	t.Helper()
	h := ListTenants(&Deps{ConfigDir: configDir, RBAC: newRBACManager(t, "")})
	w := httptest.NewRecorder()
	h(w, httptest.NewRequest("GET", "/api/v1/tenants", nil))
	if w.Code != http.StatusOK {
		return w.Code, nil
	}
	var rows []TenantSummary
	if err := json.Unmarshal(w.Body.Bytes(), &rows); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	out := make(map[string]TenantSummary, len(rows))
	for _, r := range rows {
		out[r.ID] = r
	}
	return w.Code, out
}

// listControlTenantFile is a second tenant whose metadata is written only in
// its own file; no tree gives it a platform entry, so its row must stay
// exactly listControlMetadata whatever the tree does to tx.
const listControlTenantFile = "tenants:\n  ty:\n    _metadata:\n      owner: ty-team\n      db_type: postgresql\n"

var listControlMetadata = listedMetadata{Owner: "ty-team", DBType: "postgresql"}

func platformMetadataTrees() []struct {
	name  string
	files map[string]string
	want  listedMetadata
} {
	return []struct {
		name  string
		files map[string]string
		want  listedMetadata
	}{
		{
			name: "platform layer only",
			files: map[string]string{
				"_platform.yaml": "tenants:\n  tx:\n    _metadata:\n      owner: plat-team\n      environment: production\n",
				"tx.yaml":        "tenants:\n  tx: {}\n",
			},
			want: listedMetadata{Owner: "plat-team", Environment: "production"},
		},
		{
			name: "tenant file only",
			files: map[string]string{
				"tx.yaml": "tenants:\n  tx:\n    _metadata:\n      owner: tx-team\n      tier: gold\n",
			},
			want: listedMetadata{Owner: "tx-team", Tier: "gold"},
		},
		{
			name: "different keys in both layers are merged",
			files: map[string]string{
				"_platform.yaml": "tenants:\n  tx:\n    _metadata:\n      owner: plat-team\n",
				"tx.yaml":        "tenants:\n  tx:\n    _metadata:\n      tier: gold\n",
			},
			want: listedMetadata{Owner: "plat-team", Tier: "gold"},
		},
		{
			name: "same key in both layers: the tenant file wins",
			files: map[string]string{
				"_platform.yaml": "tenants:\n  tx:\n    _metadata:\n      owner: plat-team\n      tier: silver\n",
				"tx.yaml":        "tenants:\n  tx:\n    _metadata:\n      owner: tx-team\n",
			},
			want: listedMetadata{Owner: "tx-team", Tier: "silver"},
		},
		{
			name: "tenant file writes only db_type, platform writes environment domain owner",
			files: map[string]string{
				"_platform.yaml": "tenants:\n  tx:\n    _metadata:\n      environment: production\n      domain: finance\n      owner: plat-team\n",
				"tx.yaml":        "tenants:\n  tx:\n    _metadata:\n      db_type: mariadb\n",
			},
			want: listedMetadata{Owner: "plat-team", DBType: "mariadb", Environment: "production", Domain: "finance"},
		},
		{
			name: "two platform files: the later file wins a key both write",
			files: map[string]string{
				"_a.yaml": "tenants:\n  tx:\n    _metadata:\n      owner: a-team\n      tier: silver\n",
				"_b.yaml": "tenants:\n  tx:\n    _metadata:\n      owner: b-team\n      domain: finance\n",
				"tx.yaml": "tenants:\n  tx:\n    _metadata:\n      db_type: mariadb\n",
			},
			want: listedMetadata{Owner: "b-team", Tier: "silver", Domain: "finance", DBType: "mariadb"},
		},
		{
			name: "a list value is replaced whole, not merged",
			files: map[string]string{
				"_platform.yaml": "tenants:\n  tx:\n    _metadata:\n      tags: [a, b]\n      owner: plat-team\n",
				"tx.yaml":        "tenants:\n  tx:\n    _metadata:\n      tags: [c]\n",
			},
			want: listedMetadata{Owner: "plat-team", Tags: []string{"c"}},
		},
		{
			name: "tenant file writes a null _metadata: nothing below survives",
			files: map[string]string{
				"_platform.yaml": "tenants:\n  tx:\n    _metadata:\n      owner: plat-team\n",
				"tx.yaml":        "tenants:\n  tx:\n    _metadata: null\n",
			},
			want: listedMetadata{},
		},
		{
			name: "a platform file the exporter cannot decode adds nothing",
			files: map[string]string{
				"_broken.yaml":   "tenants: [\n",
				"_platform.yaml": "tenants:\n  tx:\n    _metadata:\n      owner: plat-team\n",
				"tx.yaml":        "tenants:\n  tx:\n    _metadata:\n      db_type: mariadb\n",
			},
			want: listedMetadata{Owner: "plat-team", DBType: "mariadb"},
		},
	}
}

func TestListTenants_MetadataMergesPlatformLayerPerKey(t *testing.T) {
	t.Parallel()
	for _, tc := range platformMetadataTrees() {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			files := map[string]string{"ty.yaml": listControlTenantFile}
			for k, v := range tc.files {
				files[k] = v
			}
			code, rows := listTenantRows(t, setupConfigDir(t, files))
			if code != http.StatusOK {
				t.Fatalf("LIST status = %d, want 200", code)
			}
			if got := metadataOf(rows["tx"]); !reflect.DeepEqual(got, tc.want) {
				t.Errorf("tx metadata on LIST = %+v, want %+v", got, tc.want)
			}
			if got := metadataOf(rows["ty"]); !reflect.DeepEqual(got, listControlMetadata) {
				t.Errorf("ty (own file only) metadata on LIST = %+v, want %+v", got, listControlMetadata)
			}
			if len(rows) != 2 {
				t.Errorf("LIST rows = %v, want exactly tx and ty", rows)
			}
		})
	}
}

// A platform entry for an id no tenant file declares adds no row.
func TestListTenants_PlatformMetadataForUndeclaredTenantAddsNoRow(t *testing.T) {
	t.Parallel()
	code, rows := listTenantRows(t, setupConfigDir(t, map[string]string{
		"_platform.yaml": "tenants:\n  tz:\n    _metadata:\n      owner: plat-team\n",
		"ty.yaml":        listControlTenantFile,
	}))
	if code != http.StatusOK {
		t.Fatalf("LIST status = %d, want 200", code)
	}
	if _, listed := rows["tz"]; listed || len(rows) != 1 {
		t.Errorf("LIST rows = %v, want ty alone", rows)
	}
}

// writeDanglingPlatformFile puts a root `_`-prefixed config file that cannot
// be read (a symlink to nothing) into dir.
func writeDanglingPlatformFile(t *testing.T, dir string) {
	t.Helper()
	if err := os.Symlink(filepath.Join(dir, "missing-target.yaml"), filepath.Join(dir, "_dangling.yaml")); err != nil {
		t.Skipf("symlink: %v", err)
	}
}

// A root platform file that cannot be read leaves the metadata unknown: the
// listing is refused rather than served without that file's keys.
func TestListTenants_UnreadablePlatformFileFailsTheListing(t *testing.T) {
	t.Parallel()
	dir := setupConfigDir(t, map[string]string{
		"_platform.yaml": "tenants:\n  tx:\n    _metadata:\n      owner: plat-team\n",
		"tx.yaml":        "tenants:\n  tx: {}\n",
	})
	writeDanglingPlatformFile(t, dir)
	if code, rows := listTenantRows(t, dir); code != http.StatusInternalServerError {
		t.Errorf("LIST status = %d (rows %v), want 500", code, rows)
	}
}

// The on-disk and proposed-body metadata readers (WriteScopeMeta,
// proposedScopeMeta) return the same merged environment / domain LIST does.
func TestMetadataReadersMergePlatformLayerPerKey(t *testing.T) {
	t.Parallel()
	const platform = "tenants:\n  tx:\n    _metadata:\n      environment: production\n      domain: finance\n      owner: plat-team\n"
	dir := setupConfigDir(t, map[string]string{
		"_platform.yaml": platform,
		"tx.yaml":        "tenants:\n  tx:\n    _metadata:\n      db_type: mariadb\n",
		"ty.yaml":        listControlTenantFile,
	})
	if env, domain, ok := WriteScopeMeta(dir)("tx"); env != "production" || domain != "finance" || !ok {
		t.Errorf("on-disk read for tx = (%q, %q, %v), want (production, finance, true)", env, domain, ok)
	}
	if env, domain, ok := WriteScopeMeta(dir)("ty"); env != "" || domain != "" || !ok {
		t.Errorf("on-disk read for ty = (%q, %q, %v), want (\"\", \"\", true)", env, domain, ok)
	}
	body := "tenants:\n  tx:\n    _metadata:\n      domain: payments\n"
	if env, domain, ok := proposedScopeMeta(dir, body)("tx"); env != "production" || domain != "payments" || !ok {
		t.Errorf("proposed-body read for tx = (%q, %q, %v), want (production, payments, true)", env, domain, ok)
	}
}

// With a root platform file that cannot be read, both readers report no
// reading (ok=false) instead of the tenant file's keys alone.
func TestMetadataReadersReportUnreadablePlatformLayer(t *testing.T) {
	t.Parallel()
	dir := setupConfigDir(t, map[string]string{
		"tx.yaml": "tenants:\n  tx:\n    _metadata:\n      environment: production\n",
	})
	writeDanglingPlatformFile(t, dir)
	if env, domain, ok := WriteScopeMeta(dir)("tx"); ok || env != "" || domain != "" {
		t.Errorf("on-disk read = (%q, %q, %v), want (\"\", \"\", false)", env, domain, ok)
	}
	if env, domain, ok := proposedScopeMeta(dir, "tenants:\n  tx: {}\n")("tx"); ok || env != "" || domain != "" {
		t.Errorf("proposed-body read = (%q, %q, %v), want (\"\", \"\", false)", env, domain, ok)
	}
}
