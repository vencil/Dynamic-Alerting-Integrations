package handler

// #1680: a tenant whose conf.d file is not usable is a DEGRADED row
// (ID + config_error) in GET /api/v1/tenants, not a silently dropped one — and
// that row is visible only to callers whose rule places no restriction on the
// metadata axes, because its environment/domain are unknown.

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/testutil"
)

// degradedCases gives each conf.d state the tenant id the fixture uses for it,
// whether it is a tenant file at all (listed), and the config_error the list
// must report for it ("" = healthy row).
var degradedCases = []struct {
	state  testutil.ConfdFileState
	id     string
	listed bool
	reason string
}{
	{testutil.ConfdGood, "t-good", true, ""},
	{testutil.ConfdMalformedYAML, "t-malformed", true, "malformed_yaml"},
	{testutil.ConfdInvalidConfig, "t-invalid", true, "invalid_config"},
	{testutil.ConfdEmptyFile, "t-empty", true, ""},
	{testutil.ConfdDanglingSymlink, "t-dangling", true, "unreadable"},
	{testutil.ConfdSymlinkToDir, "t-symdir", true, "not_regular_file"},
	{testutil.ConfdRealDir, "t-realdir", false, ""},
	{testutil.ConfdUnreadable, "t-unreadable", true, "unreadable"},
}

// buildDegradedDir writes every state into one conf.d and returns the rows
// loadAllTenants must produce, in listing (filename) order.
func buildDegradedDir(t *testing.T) (string, []TenantSummary) {
	t.Helper()
	dir := t.TempDir()
	var want []TenantSummary
	for _, c := range degradedCases {
		if !testutil.BuildConfdEntry(t, dir, c.id, c.state) {
			continue // unreadable-permissions under euid 0
		}
		if c.listed {
			want = append(want, TenantSummary{ID: c.id, ConfigError: c.reason})
		}
	}
	// ReadDir order is filename order.
	for i := 1; i < len(want); i++ {
		for j := i; j > 0 && want[j].ID < want[j-1].ID; j-- {
			want[j], want[j-1] = want[j-1], want[j]
		}
	}
	return dir, want
}

func TestLoadAllTenants_DegradedRows(t *testing.T) {
	t.Parallel()
	dir, want := buildDegradedDir(t)
	got, err := loadAllTenants(dir)
	if err != nil {
		t.Fatalf("loadAllTenants: %v", err)
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("loadAllTenants =\n  %+v\nwant\n  %+v", got, want)
	}
}

// A degraded row carries NOTHING but ID + ConfigError — in particular no
// half-read metadata that a scope filter could mistake for a label.
func TestLoadAllTenants_DegradedRowHasOnlyIDAndReason(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	testutil.BuildConfdEntry(t, dir, "acme", testutil.ConfdMalformedYAML)
	got, err := loadAllTenants(dir)
	if err != nil {
		t.Fatalf("loadAllTenants: %v", err)
	}
	b, _ := json.Marshal(got)
	if string(b) != `[{"id":"acme","config_error":"malformed_yaml"}]` {
		t.Errorf("JSON = %s", b)
	}
}

// A healthy row is unchanged, and config_error is absent from its JSON.
func TestLoadAllTenants_HealthyRowUnchanged(t *testing.T) {
	t.Parallel()
	dir := setupConfigDir(t, map[string]string{
		"acme.yaml": fixtureTenantYAML("acme", "production", "tier1", "db", "mariadb", "alice"),
	})
	got, err := loadAllTenants(dir)
	if err != nil {
		t.Fatalf("loadAllTenants: %v", err)
	}
	want := []TenantSummary{{ID: "acme", Environment: "production", Tier: "tier1", Domain: "db", DBType: "mariadb", Owner: "alice"}}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("got %+v, want %+v", got, want)
	}
	if b, _ := json.Marshal(got); strings.Contains(string(b), "config_error") {
		t.Errorf("healthy row JSON carries config_error: %s", b)
	}
}

// #1673's name-based claim still refuses the WHOLE listing when a broken
// file and a healthy one claim the same id — degraded rows must not become a
// way to list the tenant twice.
func TestLoadAllTenants_DuplicateStemWithBrokenSiblingStillRefused(t *testing.T) {
	t.Parallel()
	for _, state := range []testutil.ConfdFileState{testutil.ConfdMalformedYAML, testutil.ConfdDanglingSymlink, testutil.ConfdSymlinkToDir} {
		t.Run(string(state), func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			testutil.BuildConfdEntry(t, dir, "acme", state) // acme.yaml
			testutil.WriteYAML(t, dir, "acme.yml", "tenants:\n  acme:\n    cpu: \"70\"\n")
			got, err := loadAllTenants(dir)
			if err == nil {
				t.Fatalf("loadAllTenants = %+v and no error; want the duplicate-tenant refusal", got)
			}
			if !strings.Contains(err.Error(), `"acme"`) {
				t.Errorf("error does not name the tenant: %v", err)
			}
		})
	}
}

// ── RBAC: degraded rows only for metadata-unrestricted callers ───────────────

const degradedRBACYAML = `groups:
  - name: all-tenants
    tenants: ["*"]
    permissions: [read]
  - name: prod-only
    tenants: ["*"]
    environments: ["production"]
    permissions: [read]
  - name: db-only
    tenants: ["*"]
    domains: ["db"]
    permissions: [read]
`

func listTenantsAs(t *testing.T, dir string, mgr *rbac.Manager, group string) []TenantSummary {
	t.Helper()
	h := mgr.Middleware(rbac.PermRead, nil)(ListTenants(&Deps{ConfigDir: dir, RBAC: mgr}))
	req := httptest.NewRequest(http.MethodGet, "/api/v1/tenants", nil)
	req.Header.Set("X-Forwarded-Email", "test@example.com")
	req.Header.Set("X-Forwarded-Groups", group)
	w := httptest.NewRecorder()
	h.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, body=%s", w.Code, w.Body.String())
	}
	var out []TenantSummary
	if err := json.Unmarshal(w.Body.Bytes(), &out); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	return out
}

func idsOf(rows []TenantSummary) []string {
	ids := make([]string, 0, len(rows))
	for _, r := range rows {
		ids = append(ids, r.ID)
	}
	return ids
}

func TestListTenants_DegradedRowVisibleOnlyToMetadataUnrestricted(t *testing.T) {
	t.Parallel()
	dir := setupConfigDir(t, map[string]string{
		"healthy.yaml": fixtureTenantYAML("healthy", "production", "tier1", "db", "mariadb", "alice"),
	})
	testutil.BuildConfdEntry(t, dir, "broken", testutil.ConfdMalformedYAML)

	cases := []struct {
		group string
		want  []string
	}{
		{"all-tenants", []string{"broken", "healthy"}},
		// The restricted callers still see the healthy tenant they are
		// allowed — proving they are live — but never the broken one, whose
		// real environment/domain is unknown.
		{"prod-only", []string{"healthy"}},
		{"db-only", []string{"healthy"}},
		// A tenant-pattern mismatch cannot be exercised through this route:
		// the "*" read gate 403s such a caller before the filter runs. It is
		// pinned at the decision itself in rbac's
		// TestScopeAllowedUnknownMetadata_MetadataAxis.
	}
	for _, enforce := range []bool{false, true} {
		mgr := newRBACManager(t, degradedRBACYAML)
		mode := "metadata=shadow"
		if enforce {
			mgr.EnableMetadataScopeEnforce()
			mode = "metadata=enforce"
		}
		for _, c := range cases {
			t.Run(mode+"/"+c.group, func(t *testing.T) {
				got := idsOf(listTenantsAs(t, dir, mgr, c.group))
				if !reflect.DeepEqual(got, c.want) {
					t.Errorf("ids = %v, want %v", got, c.want)
				}
			})
		}
	}

	// The unrestricted caller gets the reason, not just the id.
	rows := listTenantsAs(t, dir, newRBACManager(t, degradedRBACYAML), "all-tenants")
	if rows[0].ID != "broken" || rows[0].ConfigError != "malformed_yaml" {
		t.Errorf("degraded row = %+v, want {ID:broken ConfigError:malformed_yaml}", rows[0])
	}
}

// ── search: deliberate semantics for degraded rows ──────────────────────────
//
// A degraded row is part of the snapshot and goes through the same RBAC
// filter. Metadata filters (environment/tier/domain/db_type/tag) never match
// it — its metadata is unknown, and matching "unknown" against a requested
// value would be a guess. Free-text q matches its id (the only field it has),
// so an operator searching for a tenant by name still finds it broken rather
// than absent. It sorts like any row with empty metadata.
func TestSearchTenants_DegradedRows(t *testing.T) {
	t.Parallel()
	dir := setupConfigDir(t, map[string]string{
		"healthy.yaml": fixtureTenantYAML("healthy", "production", "tier1", "db", "mariadb", "alice"),
	})
	testutil.BuildConfdEntry(t, dir, "broken", testutil.ConfdDanglingSymlink)
	mgr := newRBACManager(t, degradedRBACYAML)

	t.Run("unfiltered, unrestricted caller", func(t *testing.T) {
		resp, code, body := runSearch(t, dir, mgr, []string{"all-tenants"}, "")
		if code != http.StatusOK {
			t.Fatalf("status %d: %s", code, body)
		}
		if got := idsOf(resp.Items); !reflect.DeepEqual(got, []string{"broken", "healthy"}) {
			t.Fatalf("ids = %v", got)
		}
		if resp.Items[0].ConfigError != "unreadable" {
			t.Errorf("degraded row = %+v, want config_error=unreadable", resp.Items[0])
		}
		if resp.TotalMatched != 2 {
			t.Errorf("total_matched = %d, want 2", resp.TotalMatched)
		}
	})
	t.Run("restricted caller never sees it", func(t *testing.T) {
		resp, _, _ := runSearch(t, dir, mgr, []string{"prod-only"}, "")
		if got := idsOf(resp.Items); !reflect.DeepEqual(got, []string{"healthy"}) {
			t.Errorf("ids = %v, want [healthy]", got)
		}
	})
	t.Run("free text matches the id", func(t *testing.T) {
		resp, _, _ := runSearch(t, dir, mgr, []string{"all-tenants"}, "q=BROK")
		if got := idsOf(resp.Items); !reflect.DeepEqual(got, []string{"broken"}) {
			t.Errorf("ids = %v, want [broken]", got)
		}
	})
	t.Run("metadata filters never match it", func(t *testing.T) {
		for _, q := range []string{"environment=production", "domain=db", "tier=tier1", "db_type=mariadb"} {
			resp, _, _ := runSearch(t, dir, mgr, []string{"all-tenants"}, q)
			if got := idsOf(resp.Items); !reflect.DeepEqual(got, []string{"healthy"}) {
				t.Errorf("%s: ids = %v, want [healthy]", q, got)
			}
		}
	})
	t.Run("sort and paginate cope with empty metadata", func(t *testing.T) {
		resp, _, _ := runSearch(t, dir, mgr, []string{"all-tenants"}, "sort=environment&page_size=1")
		if got := idsOf(resp.Items); !reflect.DeepEqual(got, []string{"broken"}) {
			t.Errorf("page 1 ids = %v, want [broken] (empty environment sorts first)", got)
		}
		if resp.NextOffset == nil || *resp.NextOffset != 1 {
			t.Errorf("next_offset = %v, want 1", resp.NextOffset)
		}
	})
}
