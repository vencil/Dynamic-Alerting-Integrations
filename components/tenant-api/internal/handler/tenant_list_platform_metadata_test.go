package handler

// #2370: a tenant's metadata on GET /api/v1/tenants is the root platform
// files' `tenants.<id>._metadata` merged per key under the tenant file's own
// `_metadata` — the value /metrics resolves for the same tree
// (threshold-exporter's config_metadata_layers_test.go runs the same shapes).

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"github.com/vencil/tenant-api/internal/rbac"
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

// platform file unreadable: list falls back to tenant-file metadata.
func TestListTenants_UnreadablePlatformFileFallsBackToTenantFileMetadata(t *testing.T) {
	t.Parallel()
	dir := setupConfigDir(t, map[string]string{
		"_platform.yaml": "tenants:\n  tx:\n    _metadata:\n      owner: plat-team\n",
		"tx.yaml":        "tenants:\n  tx:\n    _metadata:\n      db_type: mariadb\n",
		"ty.yaml":        listControlTenantFile,
	})
	writeDanglingPlatformFile(t, dir)
	code, rows := listTenantRows(t, dir)
	if code != http.StatusOK {
		t.Fatalf("LIST status = %d, want 200", code)
	}
	if got, want := metadataOf(rows["tx"]), (listedMetadata{DBType: "mariadb"}); !reflect.DeepEqual(got, want) {
		t.Errorf("tx metadata on LIST = %+v, want %+v (tenant file only)", got, want)
	}
	if got := metadataOf(rows["ty"]); !reflect.DeepEqual(got, listControlMetadata) {
		t.Errorf("ty metadata on LIST = %+v, want %+v", got, listControlMetadata)
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
	if env, domain := WriteScopeMeta(dir)("tx"); env != "production" || domain != "finance" {
		t.Errorf("on-disk read for tx = (%q, %q), want (production, finance)", env, domain)
	}
	if env, domain := WriteScopeMeta(dir)("ty"); env != "" || domain != "" {
		t.Errorf("on-disk read for ty = (%q, %q), want (\"\", \"\")", env, domain)
	}
	body := "tenants:\n  tx:\n    _metadata:\n      domain: payments\n"
	if env, domain := proposedScopeMeta(dir, body)("tx"); env != "production" || domain != "payments" {
		t.Errorf("proposed-body read for tx = (%q, %q), want (production, payments)", env, domain)
	}
}

// platform file unreadable: both metadata readers return the tenant layer's
// own values (the file on disk, the proposed body).
func TestMetadataReadersFallBackToTenantLayerWhenPlatformFileUnreadable(t *testing.T) {
	t.Parallel()
	dir := setupConfigDir(t, map[string]string{
		"_platform.yaml": "tenants:\n  tx:\n    _metadata:\n      environment: staging\n      domain: finance\n",
		"tx.yaml":        "tenants:\n  tx:\n    _metadata:\n      environment: production\n",
	})
	writeDanglingPlatformFile(t, dir)
	if env, domain := WriteScopeMeta(dir)("tx"); env != "production" || domain != "" {
		t.Errorf("on-disk read = (%q, %q), want (production, \"\")", env, domain)
	}
	body := "tenants:\n  tx:\n    _metadata:\n      domain: payments\n"
	if env, domain := proposedScopeMeta(dir, body)("tx"); env != "" || domain != "payments" {
		t.Errorf("proposed-body read = (%q, %q), want (\"\", payments)", env, domain)
	}
}

// putFixtureGroups is the test server's groups file.
const putFixtureGroups = `groups:
  - name: prod-ops
    tenants: ["*"]
    permissions: [read, write]
    environments: [production]
`

// tenant write succeeds when a root platform file is unreadable: the write
// reads the tenant layer's metadata, as it did before #2370.
func TestPutTenant_SucceedsWhenRootPlatformFileUnreadable(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, map[string]string{
		"_platform.yaml": "tenants:\n  tx:\n    _metadata:\n      owner: plat-team\n",
		"tx.yaml":        "tenants:\n  tx:\n    _metadata:\n      environment: production\n",
	})
	writeDanglingPlatformFile(t, configDir)
	initGitRepo(t, configDir)
	rbacMgr := newRBACManager(t, putFixtureGroups)
	h := PutTenant(&Deps{
		Writer:    newTestWriter(configDir),
		ConfigDir: configDir,
		RBAC:      rbacMgr,
		WriteMode: WriteModeDirect,
	})
	body := "tenants:\n  tx:\n    _metadata:\n      environment: production\n      tier: gold\n"
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/tx", "id", "tx", bytes.NewBufferString(body))
	req.Header.Set("X-Forwarded-Email", "ops@example.com")
	req.Header.Set("X-Forwarded-Groups", "prod-ops")
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(h, rbacMgr, rbac.PermWrite, TenantIDFromPath).ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("PUT status = %d, body %s; want 200", w.Code, w.Body.String())
	}
	if got, err := os.ReadFile(filepath.Join(configDir, "tx.yaml")); err != nil || string(got) != body {
		t.Errorf("tx.yaml after PUT = %q, %v; want the body", got, err)
	}
}
