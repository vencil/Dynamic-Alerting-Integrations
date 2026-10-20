package handler

// #3157: GET /api/v1/tenants says, per row, who manages the tenant — the
// managed_by / git_managed_reason GET /api/v1/tenants/{id} gives the same
// file (#3064, confd.TenantFileManagement). A file whose name is not the one
// tenant it declares used to be listed as a silent empty row; it is now
// marked git-managed, id_mismatch, and still never shows the declared id.

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
)

// Fixture ids: test constants, no real tenant.
const (
	lgOwn      = "lg-own"      // its file declares itself: API-managed
	lgStem     = "lg-stem"     // its file declares lgDeclared alone
	lgDeclared = "lg-declared" // declared under lgStem's name; never listed
	lgShared   = "lg-shared"   // its file declares itself and lgCoTenant
	lgCoTenant = "lg-cotenant"
	lgShell    = "lg-shell" // an empty file created ahead of the tenant
)

// lgFiles is a conf.d holding one file of each management shape the
// fixtures above name.
func lgFiles() map[string]string {
	return map[string]string{
		lgOwn + ".yaml":  fixtureTenantYAML(lgOwn, "production", "tier1", "db", "mariadb", "alice"),
		lgStem + ".yaml": fixtureTenantYAML(lgDeclared, "staging", "tier2", "web", "redis", "bob"),
		lgShared + ".yaml": "tenants:\n  " + lgShared + ":\n    _silent_mode: \"warning\"\n" +
			"  " + lgCoTenant + ":\n    _silent_mode: \"critical\"\n",
		lgShell + ".yaml": "",
	}
}

func rowByID(t *testing.T, rows []TenantSummary, id string) TenantSummary {
	t.Helper()
	for _, r := range rows {
		if r.ID == id {
			return r
		}
	}
	t.Fatalf("no row %q in %v", id, idsOf(rows))
	return TenantSummary{}
}

func TestLoadAllTenants_ManagementMarkers(t *testing.T) {
	t.Parallel()
	dir := setupConfigDir(t, lgFiles())
	rows, err := loadAllTenants(dir)
	if err != nil {
		t.Fatalf("loadAllTenants: %v", err)
	}
	// Listed by file name: the declared id is not a row.
	if got, want := idsOf(rows), []string{lgOwn, lgShared, lgShell, lgStem}; !reflect.DeepEqual(got, want) {
		t.Fatalf("ids = %v, want %v", got, want)
	}
	for _, c := range []struct{ id, managedBy, reason string }{
		{lgOwn, "api", ""},
		{lgShell, "api", ""},
		{lgShared, "git", "tenant_count"},
		{lgStem, "git", "id_mismatch"},
	} {
		r := rowByID(t, rows, c.id)
		if r.ManagedBy != c.managedBy || r.GitManagedReason != c.reason {
			t.Errorf("%s: managed_by = %q, git_managed_reason = %q; want %q, %q", c.id, r.ManagedBy, r.GitManagedReason, c.managedBy, c.reason)
		}
	}
	// The id_mismatch row: the marker, metadata unknown, nothing else —
	// the file's metadata is lgDeclared's, not lgStem's.
	if got, want := rowByID(t, rows, lgStem), (TenantSummary{ID: lgStem, ManagedBy: "git", GitManagedReason: "id_mismatch", MetadataIncomplete: true}); !reflect.DeepEqual(got, want) {
		t.Errorf("id_mismatch row = %+v, want %+v", got, want)
	}
	// The shared file declares its own tenant: that tenant's fields are
	// read, so its metadata is known.
	if r := rowByID(t, rows, lgShared); r.SilentMode != "warning" || r.MetadataIncomplete {
		t.Errorf("tenant_count row declaring its own id = %+v; want its fields, metadata_incomplete false", r)
	}
	// An API-shaped shell declares nothing of anyone else: unchanged.
	if r := rowByID(t, rows, lgShell); r.MetadataIncomplete {
		t.Errorf("empty shell row = %+v; want metadata_incomplete false", r)
	}
}

// Through the handler: the JSON carries the markers, an API-managed row no
// git_managed_reason, and no row names a declared id that is not a file name.
func TestListTenants_ManagementMarkersJSON(t *testing.T) {
	t.Parallel()
	dir := setupConfigDir(t, lgFiles())
	h := ListTenants(&Deps{ConfigDir: dir, RBAC: newRBACManager(t, "")})
	w := httptest.NewRecorder()
	h(w, httptest.NewRequest(http.MethodGet, "/api/v1/tenants", nil))
	if w.Code != http.StatusOK {
		t.Fatalf("status %d: %s", w.Code, w.Body.String())
	}
	var rows []map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &rows); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	byID := map[string]map[string]any{}
	for _, r := range rows {
		byID[r["id"].(string)] = r
	}
	// The id_mismatch row is the marker alone, metadata unknown: no
	// metadata, no state, no config_derived (the exporter serves the
	// declared id, not this one).
	if r := byID[lgStem]; !reflect.DeepEqual(r, map[string]any{"id": lgStem, "managed_by": "git", "git_managed_reason": "id_mismatch", "metadata_incomplete": true}) {
		t.Errorf("id_mismatch row JSON = %v", r)
	}
	if r := byID[lgOwn]; r["managed_by"] != "api" {
		t.Errorf("API-managed row JSON = %v", r)
	} else if _, ok := r["git_managed_reason"]; ok {
		t.Errorf("git_managed_reason on an API-managed row: %v", r)
	}
	for _, other := range []string{lgDeclared, lgCoTenant} {
		if strings.Contains(w.Body.String(), other) {
			t.Errorf("the list names %q, which no file is named for: %s", other, w.Body.String())
		}
	}
}

// The list row and GET /tenants/{id} give the same verdict for the same
// file — every shape TenantFileManagement tells apart, plus the ones the
// list reaches by another path (bytes that are not YAML, which
// confd.ReadTenantFile does not hand back; a file the typed decode rejects).
func TestListTenants_ManagementMatchesGet(t *testing.T) {
	t.Parallel()
	const id = "lg-subject"
	for _, c := range []struct{ name, file, managedBy, reason string }{
		{"own file", "tenants:\n  " + id + ":\n    _silent_mode: \"warning\"\n", "api", ""},
		{"empty shell", "", "api", ""},
		{"declares another id", "tenants:\n  " + lgDeclared + ":\n    _silent_mode: \"warning\"\n", "git", "id_mismatch"},
		{"declares several", "tenants:\n  " + id + ": {}\n  " + lgCoTenant + ": {}\n", "git", "tenant_count"},
		{"several documents", "tenants:\n  " + id + ": {}\n---\ntenants:\n  " + lgCoTenant + ": {}\n", "git", "several_documents"},
		{"merge key at the top level", "base: &b\n  x: 1\n<<: *b\ntenants:\n  " + id + ": {}\n", "git", "yaml_indirection"},
		{"not YAML", "{{not valid yaml", "git", "not_tenant_document"},
		{"tenants twice (invalid_config)", "tenants: {}\ntenants: {}\n", "git", "not_tenant_document"},
	} {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			dir := setupConfigDir(t, map[string]string{id + ".yaml": c.file})

			rows, err := loadAllTenants(dir)
			if err != nil {
				t.Fatalf("loadAllTenants: %v", err)
			}
			row := rowByID(t, rows, id)
			if row.ManagedBy != c.managedBy || row.GitManagedReason != c.reason {
				t.Errorf("list: managed_by = %q, git_managed_reason = %q; want %q, %q", row.ManagedBy, row.GitManagedReason, c.managedBy, c.reason)
			}

			w := httptest.NewRecorder()
			GetTenant(&Deps{ConfigDir: dir})(w, newRequestWithChiParam("GET", "/api/v1/tenants/"+id, "id", id, nil))
			if w.Code != http.StatusOK {
				t.Fatalf("GET status %d: %s", w.Code, w.Body.String())
			}
			var d map[string]any
			if err := json.Unmarshal(w.Body.Bytes(), &d); err != nil {
				t.Fatalf("unmarshal: %v", err)
			}
			getReason, _ := d["git_managed_reason"].(string)
			if d["managed_by"] != row.ManagedBy || getReason != row.GitManagedReason {
				t.Errorf("GET says managed_by = %v, git_managed_reason = %q; the list row says %q, %q",
					d["managed_by"], getReason, row.ManagedBy, row.GitManagedReason)
			}
		})
	}
}

// RBAC (#3157, owner decision A): an id_mismatch row's file holds another
// id's metadata (lgDeclared's, labeled staging / web), so the row's own is
// UNKNOWN, not unlabeled. The row is metadata_incomplete and the list filter
// decides it as a degraded row (rbac.ScopeAllowedUnknownMetadata): a caller
// restricted on environments or domains does not see it, in shadow mode as in
// enforce mode, although shadow mode lets an unlabeled tenant through. A
// caller whose rule restricts neither axis still sees it. Breaking against
// the behaviour before, where shadow mode showed it to restricted callers.
func TestListTenants_IDMismatchRowVisibility(t *testing.T) {
	t.Parallel()
	dir := setupConfigDir(t, map[string]string{
		lgOwn + ".yaml":  fixtureTenantYAML(lgOwn, "production", "tier1", "db", "mariadb", "alice"),
		lgStem + ".yaml": fixtureTenantYAML(lgDeclared, "staging", "tier2", "web", "redis", "bob"),
	})
	for _, c := range []struct {
		enforce bool
		group   string
		want    []string
	}{
		{false, "all-tenants", []string{lgOwn, lgStem}},
		{false, "prod-only", []string{lgOwn}}, // no unlabeled leniency: unknown
		{false, "db-only", []string{lgOwn}},
		{true, "all-tenants", []string{lgOwn, lgStem}},
		{true, "prod-only", []string{lgOwn}},
		{true, "db-only", []string{lgOwn}},
	} {
		mode := "metadata=shadow"
		if c.enforce {
			mode = "metadata=enforce"
		}
		t.Run(mode+"/"+c.group, func(t *testing.T) {
			t.Parallel()
			mgr := newRBACManager(t, degradedRBACYAML)
			if c.enforce {
				mgr.EnableMetadataScopeEnforce()
			}
			rows := listTenantsAs(t, dir, mgr, c.group)
			if got := idsOf(rows); !reflect.DeepEqual(got, c.want) {
				t.Errorf("ids = %v, want %v", got, c.want)
			}
			for _, r := range rows {
				if r.ID == lgStem && (!r.MetadataIncomplete || r.Environment != "" || r.Domain != "") {
					t.Errorf("id_mismatch row = %+v; want empty metadata, metadata_incomplete true", r)
				}
			}
		})
	}
}

// Which git-managed rows are metadata_incomplete: those whose file does not
// declare the row's id, whatever the reason — the metadata in it is other
// ids'. One that does declare it reads its own metadata, as /metrics does.
func TestLoadAllTenants_GitManagedMetadataIncomplete(t *testing.T) {
	t.Parallel()
	const id = "lg-subject"
	for _, c := range []struct {
		name, file, reason string
		incomplete         bool
	}{
		{"id_mismatch", "tenants:\n  " + lgDeclared + ":\n    _silent_mode: \"warning\"\n", "id_mismatch", true},
		{"tenant_count, own id declared", "tenants:\n  " + id + ": {}\n  " + lgCoTenant + ": {}\n", "tenant_count", false},
		{"tenant_count, own id not declared", "tenants:\n  " + lgDeclared + ": {}\n  " + lgCoTenant + ": {}\n", "tenant_count", true},
		{"several_documents, own id in the first", "tenants:\n  " + id + ": {}\n---\ntenants:\n  " + lgCoTenant + ": {}\n", "several_documents", false},
		{"several_documents, own id only in the second", "tenants:\n  " + lgCoTenant + ": {}\n---\ntenants:\n  " + id + ": {}\n", "several_documents", true},
		{"yaml_indirection, own id declared", "base: &b\n  x: 1\n<<: *b\ntenants:\n  " + id + ": {}\n", "yaml_indirection", false},
	} {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			dir := setupConfigDir(t, map[string]string{id + ".yaml": c.file})
			rows, err := loadAllTenants(dir)
			if err != nil {
				t.Fatalf("loadAllTenants: %v", err)
			}
			r := rowByID(t, rows, id)
			if r.GitManagedReason != c.reason || r.MetadataIncomplete != c.incomplete {
				t.Errorf("row = %+v; want git_managed_reason %q, metadata_incomplete %v", r, c.reason, c.incomplete)
			}
		})
	}
}
