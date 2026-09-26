package handler

// #1988 D1 PR-3: the tenant list's silent-mode / maintenance state comes from
// config.LoadDir + OperationalStatesAt (what the exporter emits), is cached,
// and names the files the load skipped as unparseable.
//
// Tenant ids here are synthetic (t-*). Each fixture tenant exercises a source
// the raw per-file fields cannot see: a profile, the root `_defaults.yaml`
// `tenants:` block, an explicit `disable`, and an expired silence.

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/testutil"
)

const derivedDefaultsYAML = "defaults:\n  cpu: 80\n" +
	"state_filters:\n  maintenance:\n    reasons: []\n    severity: info\n    default_state: disable\n" +
	"tenants:\n  t-rootblock:\n    _silent_mode: critical\n"

func derivedFixture() map[string]string {
	return map[string]string{
		"_defaults.yaml":   derivedDefaultsYAML,
		"_profiles.yaml":   "profiles:\n  quiet:\n    _silent_mode: all\n",
		"t-silent.yaml":    "tenants:\n  t-silent:\n    _silent_mode: warning\n",
		"t-maint.yaml":     "tenants:\n  t-maint:\n    _state_maintenance: enable\n",
		"t-maint-off.yaml": "tenants:\n  t-maint-off:\n    _state_maintenance: disable\n",
		"t-rootblock.yaml": "tenants:\n  t-rootblock:\n    cpu: \"70\"\n",
		"t-profile.yaml":   "tenants:\n  t-profile:\n    _profile: quiet\n",
		"t-expired.yaml": "tenants:\n  t-expired:\n" +
			"    _silent_mode:\n      target: all\n      expires: \"2001-01-01T00:00:00Z\"\n",
		"t-plain.yaml": "tenants:\n  t-plain:\n    cpu: \"60\"\n",
	}
}

// derivedWant is the exporter's reading of derivedFixture.
var derivedWant = map[string]ConfigDerivedState{
	"t-silent":    {SilentTargets: []string{"warning"}},
	"t-maint":     {SilentTargets: []string{}, MaintenanceActive: true},
	"t-maint-off": {SilentTargets: []string{}},
	"t-rootblock": {SilentTargets: []string{"critical"}},
	"t-profile":   {SilentTargets: []string{"critical", "warning"}},
	"t-expired":   {SilentTargets: []string{}},
	"t-plain":     {SilentTargets: []string{}},
}

func getList(t *testing.T, d *Deps) []TenantSummary {
	t.Helper()
	rec := httptest.NewRecorder()
	ListTenants(d)(rec, httptest.NewRequest(http.MethodGet, "/api/v1/tenants", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("ListTenants status = %d, body: %s", rec.Code, rec.Body.String())
	}
	var out []TenantSummary
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("decode: %v", err)
	}
	return out
}

func derivedByID(t *testing.T, items []TenantSummary) map[string]ConfigDerivedState {
	t.Helper()
	got := map[string]ConfigDerivedState{}
	for _, it := range items {
		if it.ConfigDerived == nil {
			t.Errorf("tenant %s has no config_derived", it.ID)
			continue
		}
		got[it.ID] = *it.ConfigDerived
	}
	return got
}

func TestTenantList_ConfigDerivedStateIsTheExportersReading(t *testing.T) {
	t.Parallel()
	dir := setupConfigDir(t, derivedFixture())

	t.Run("list", func(t *testing.T) {
		t.Parallel()
		items := getList(t, &Deps{ConfigDir: dir, RBAC: openModeRBAC(t), SearchCache: NewTenantSnapshotCache()})
		if got := derivedByID(t, items); !reflect.DeepEqual(got, derivedWant) {
			t.Errorf("config_derived\n got: %+v\nwant: %+v", got, derivedWant)
		}
		// The raw field is what the portal used to read as "in maintenance".
		for _, it := range items {
			if it.ID == "t-maint-off" && it.Maintenance != "disable" {
				t.Errorf("raw maintenance of t-maint-off = %q, want the file's value %q", it.Maintenance, "disable")
			}
		}
	})
	t.Run("search", func(t *testing.T) {
		t.Parallel()
		before := time.Now()
		resp, _, body := runSearch(t, dir, openModeRBAC(t), nil, "page_size=500")
		if resp == nil {
			t.Fatalf("search failed: %s", body)
		}
		if got := derivedByID(t, resp.Items); !reflect.DeepEqual(got, derivedWant) {
			t.Errorf("config_derived\n got: %+v\nwant: %+v", got, derivedWant)
		}
		meta := resp.ConfigDerivation
		if meta.EvaluatedAt.Before(before) || meta.ConfigLoadedAt.IsZero() || meta.LoadError != "" {
			t.Errorf("config_derived meta = %+v, want evaluated at request time and no load error", meta)
		}
		if meta.ParseFailedFiles == nil || len(meta.ParseFailedFiles) != 0 {
			t.Errorf("parse_failed_files = %#v, want [] on a clean tree", meta.ParseFailedFiles)
		}
	})
}

func TestTenantSnapshotCache_SecondRequestDoesNotReload(t *testing.T) {
	t.Parallel()
	dir := setupConfigDir(t, derivedFixture())
	d := &Deps{ConfigDir: dir, RBAC: openModeRBAC(t), SearchCache: NewTenantSnapshotCache()}

	first := derivedByID(t, getList(t, d))
	// Silence t-plain on disk. Within the TTL, and with no write signal, the
	// next request must be served from the cache: neither the list nor the
	// LoadDir behind config_derived is re-read.
	testutil.WriteYAML(t, dir, "t-plain.yaml", "tenants:\n  t-plain:\n    _silent_mode: all\n")
	second := derivedByID(t, getList(t, d))
	if !reflect.DeepEqual(first, second) {
		t.Errorf("second request re-loaded conf.d\n first: %+v\nsecond: %+v", first, second)
	}
}

func TestTenantSnapshotCache_ReloadsAfterAChange(t *testing.T) {
	t.Parallel()
	dir := setupConfigDir(t, derivedFixture())
	cache := NewTenantSnapshotCache()
	d := &Deps{ConfigDir: dir, RBAC: openModeRBAC(t), SearchCache: cache}
	_ = getList(t, d)

	testutil.WriteYAML(t, dir, "t-plain.yaml", "tenants:\n  t-plain:\n    _silent_mode: all\n")
	want := ConfigDerivedState{SilentTargets: []string{"critical", "warning"}}

	// The GitOps writer's post-write callback (cmd/server) calls Invalidate.
	cache.Invalidate()
	if got := derivedByID(t, getList(t, d))["t-plain"]; !reflect.DeepEqual(got, want) {
		t.Errorf("after Invalidate: t-plain = %+v, want %+v", got, want)
	}

	// A change outside this API is bounded by the TTL.
	testutil.WriteYAML(t, dir, "t-plain.yaml", "tenants:\n  t-plain:\n    cpu: \"60\"\n")
	cache.mu.Lock()
	cache.ttl = time.Millisecond
	cache.mu.Unlock()
	time.Sleep(5 * time.Millisecond)
	if got := derivedByID(t, getList(t, d))["t-plain"]; len(got.SilentTargets) != 0 {
		t.Errorf("after the TTL: t-plain = %+v, want no silence", got)
	}
}

func TestSearchTenants_ReportsParseFailedFiles(t *testing.T) {
	t.Parallel()
	files := derivedFixture()
	files["t-broken.yaml"] = "tenants:\n  t-broken:\n    cpu: {unclosed\n"
	files["t-shape.yaml"] = "tenants:\n  - t-shape\n"
	dir := setupConfigDir(t, files)
	for sub, body := range map[string]string{
		"team/_defaults.yaml":          "defaults: [unclosed\n",
		"org-beta-secret/t-nested.yml": "tenants:\n  t-nested:\n    cpu: {unclosed\n",
	} {
		if err := os.MkdirAll(filepath.Join(dir, filepath.Dir(sub)), 0o755); err != nil {
			t.Fatal(err)
		}
		testutil.WriteYAML(t, dir, sub, body)
	}

	t.Run("open mode names every file, relative to conf.d", func(t *testing.T) {
		t.Parallel()
		resp, _, body := runSearch(t, dir, openModeRBAC(t), nil, "page_size=500")
		if resp == nil {
			t.Fatalf("search failed: %s", body)
		}
		want := []string{"org-beta-secret/t-nested.yml", "t-broken.yaml", "t-shape.yaml", "team/_defaults.yaml"}
		if got := resp.ConfigDerivation.ParseFailedFiles; !reflect.DeepEqual(got, want) {
			t.Errorf("parse_failed_files = %v, want %v", got, want)
		}
		// The root tenant files are degraded rows (#1680) and have no
		// derived state; every healthy tenant keeps its own.
		healthy := []TenantSummary{}
		for _, it := range resp.Items {
			if it.ConfigError != "" {
				if it.ConfigDerived != nil {
					t.Errorf("degraded row %s carries config_derived", it.ID)
				}
				continue
			}
			healthy = append(healthy, it)
		}
		if got := derivedByID(t, healthy); !reflect.DeepEqual(got, derivedWant) {
			t.Errorf("config_derived with broken siblings\n got: %+v\nwant: %+v", got, derivedWant)
		}
	})

	t.Run("scoped caller gets file names only, for tenants it could see", func(t *testing.T) {
		t.Parallel()
		// Read on every tenant, but not a platform admin: not unrestricted.
		mgr := newRBACManager(t, `groups:
  - name: readers
    tenants: ["*"]
    permissions: [read]
`)
		resp, _, body := runSearch(t, dir, mgr, []string{"readers"}, "page_size=500")
		if resp == nil {
			t.Fatalf("search failed: %s", body)
		}
		meta := resp.ConfigDerivation
		// P4: the directory a file lives in is not the caller's to learn.
		if want := []string{"t-nested.yml", "t-broken.yaml", "t-shape.yaml"}; !reflect.DeepEqual(meta.ParseFailedFiles, want) || meta.ParseFailedHidden != 1 {
			t.Errorf("scoped parse failures = %v hidden=%d, want %v hidden=1", meta.ParseFailedFiles, meta.ParseFailedHidden, want)
		}
		if strings.Contains(string(body), "org-beta-secret") {
			t.Errorf("scoped response names another team's directory: %s", body)
		}
	})

	t.Run("metadata-restricted admin gets no paths", func(t *testing.T) {
		t.Parallel()
		// Q1 repro shape: admin on every tenant, but only in one environment.
		mgr := newRBACManager(t, `groups:
  - name: staging-admins
    tenants: ["*"]
    environments: ["staging"]
    permissions: [read, write, admin]
`)
		resp, _, body := runSearch(t, dir, mgr, []string{"staging-admins"}, "")
		if resp == nil {
			t.Fatalf("search failed: %s", body)
		}
		meta := resp.ConfigDerivation
		if len(meta.ParseFailedFiles) != 0 || meta.ParseFailedHidden != 4 {
			t.Errorf("env-scoped admin parse failures = %v hidden=%d, want [] hidden=4", meta.ParseFailedFiles, meta.ParseFailedHidden)
		}
		if strings.Contains(string(body), "org-beta-secret") {
			t.Errorf("env-scoped admin learns a directory name: %s", body)
		}
	})

	t.Run("metadata-restricted caller sees only a count", func(t *testing.T) {
		t.Parallel()
		// A broken file's environment is unknown, so an environment-restricted
		// caller may not learn which tenants are broken (the degraded-row rule).
		mgr := newRBACManager(t, `groups:
  - name: staging-only
    tenants: ["*"]
    environments: ["staging"]
    permissions: [read]
`)
		resp, _, body := runSearch(t, dir, mgr, []string{"staging-only"}, "")
		if resp == nil {
			t.Fatalf("search failed: %s", body)
		}
		meta := resp.ConfigDerivation
		if len(meta.ParseFailedFiles) != 0 || meta.ParseFailedHidden != 4 {
			t.Errorf("restricted parse failures = %v hidden=%d, want [] hidden=4", meta.ParseFailedFiles, meta.ParseFailedHidden)
		}
	})
}

// dupTenantTree is the reviewer's P1 repro: the org_read_enforce fixture's two
// tenants plus a file that declares ORG-BETA's tenant a second time, so
// config.LoadDir fails with a message naming both files.
func dupTenantTree(t *testing.T) string {
	t.Helper()
	return setupConfigDir(t, map[string]string{
		orgReadTenantIn + ".yaml":  "tenants:\n  " + orgReadTenantIn + ":\n    cpu: \"70\"\n",
		orgReadTenantOut + ".yaml": "tenants:\n  " + orgReadTenantOut + ":\n    cpu: \"70\"\n",
		"zz-dup.yaml":              "tenants:\n  " + orgReadTenantOut + ":\n    cpu: \"1\"\n",
	})
}

func searchAs(t *testing.T, d *Deps, mw func(http.Handler) http.Handler, decorate func(*http.Request)) (*SearchResponse, []byte) {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/tenants/search?page_size=500", nil)
	req.Header.Set("X-Forwarded-Email", "test@example.com")
	decorate(req)
	rec := httptest.NewRecorder()
	mw(SearchTenants(d)).ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("search status %d: %s", rec.Code, rec.Body.String())
	}
	var resp SearchResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode: %v", err)
	}
	return &resp, rec.Body.Bytes()
}

// P1: a load error names files and tenants. A caller confined to ORG-ALPHA
// must learn neither ORG-BETA's tenant id, nor the file names, nor where
// conf.d lives on the server.
func TestSearchTenants_LoadErrorIsContentFreeForScopedCallers(t *testing.T) {
	t.Parallel()
	dir := dupTenantTree(t)
	abs, _ := filepath.Abs(dir)
	resolved, _ := filepath.EvalSymlinks(abs)

	t.Run("org-scoped caller", func(t *testing.T) {
		t.Parallel()
		mgr, torg, _ := newOrgReadManager(t, orgReadRBACYAML, true)
		d := &Deps{ConfigDir: dir, RBAC: mgr, TenantOrg: torg, SearchCache: NewTenantSnapshotCache()}
		resp, body := searchAs(t, d, mgr.Middleware(rbac.PermRead, nil), func(r *http.Request) { orgReadIdentity(r, orgReadMemberOrg) })
		if resp.ConfigDerivation.LoadError != scopedLoadError {
			t.Errorf("load_error = %q, want the fixed %q", resp.ConfigDerivation.LoadError, scopedLoadError)
		}
		for _, leak := range []string{orgReadTenantOut, "zz-dup.yaml", abs, resolved} {
			if strings.Contains(string(body), leak) {
				t.Errorf("scoped response leaks %q: %s", leak, body)
			}
		}
	})

	// Q1: an admin grant restricted on environments or domains is not
	// unrestricted — PlatformAdminNonOrgScoped would have let both through.
	for name, restriction := range map[string]string{
		"environment-scoped admin": "    environments: [\"staging\"]\n",
		"domain-scoped admin":      "    domains: [\"finance\"]\n",
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			mgr := newRBACManager(t, "groups:\n  - name: scoped-admins\n    tenants: [\"*\"]\n    permissions: [read, write, admin]\n"+restriction)
			d := &Deps{ConfigDir: dir, RBAC: mgr, SearchCache: NewTenantSnapshotCache()}
			resp, body := searchAs(t, d, mgr.Middleware(rbac.PermRead, nil), func(r *http.Request) { r.Header.Set("X-Forwarded-Groups", "scoped-admins") })
			if resp.ConfigDerivation.LoadError != scopedLoadError {
				t.Errorf("load_error = %q, want the fixed %q", resp.ConfigDerivation.LoadError, scopedLoadError)
			}
			for _, leak := range []string{"zz-dup.yaml", abs, resolved} {
				if strings.Contains(string(body), leak) {
					t.Errorf("%s response leaks %q: %s", name, leak, body)
				}
			}
		})
	}

	t.Run("platform admin", func(t *testing.T) {
		t.Parallel()
		mgr := newRBACManager(t, `groups:
  - name: platform-admins
    tenants: ["*"]
    permissions: [read, write, admin]
`)
		d := &Deps{ConfigDir: dir, RBAC: mgr, SearchCache: NewTenantSnapshotCache()}
		resp, body := searchAs(t, d, mgr.Middleware(rbac.PermRead, nil), func(r *http.Request) { r.Header.Set("X-Forwarded-Groups", "platform-admins") })
		got := resp.ConfigDerivation.LoadError
		if !strings.Contains(got, "zz-dup.yaml") || !strings.Contains(got, orgReadTenantOut) {
			t.Errorf("admin load_error = %q, want the loader's message", got)
		}
		for _, leak := range []string{abs, resolved} {
			if strings.Contains(string(body), leak) {
				t.Errorf("admin response carries the server path %q: %s", leak, body)
			}
		}
		for _, it := range resp.Items {
			if it.ConfigDerived != nil {
				t.Errorf("tenant %s has config_derived without a load", it.ID)
			}
		}
		if len(resp.Items) == 0 {
			t.Errorf("list stopped serving on a load error")
		}
	})
}

// The cached config is read by every request at once; OperationalStatesAt must
// not write into it (run under -race).
func TestTenantSnapshotCache_ConcurrentReadersShareOneLoad(t *testing.T) {
	t.Parallel()
	d := &Deps{ConfigDir: setupConfigDir(t, derivedFixture()), RBAC: openModeRBAC(t), SearchCache: NewTenantSnapshotCache()}
	var wg sync.WaitGroup
	recs := make([]*httptest.ResponseRecorder, 8)
	for i := range recs {
		recs[i] = httptest.NewRecorder()
		wg.Add(1)
		go func(rec *httptest.ResponseRecorder) {
			defer wg.Done()
			ListTenants(d)(rec, httptest.NewRequest(http.MethodGet, "/api/v1/tenants", nil))
		}(recs[i])
	}
	wg.Wait()
	for i, rec := range recs {
		var items []TenantSummary
		if err := json.Unmarshal(rec.Body.Bytes(), &items); err != nil {
			t.Fatalf("reader %d: status %d, decode: %v", i, rec.Code, err)
		}
		if got := derivedByID(t, items); !reflect.DeepEqual(got, derivedWant) {
			t.Errorf("reader %d: %+v", i, got)
		}
	}
}

// Q2: only whole path components of a conf.d root are stripped, longest
// first, so a root that is a prefix of another spelling cannot leave a
// fragment of it behind.
func TestRelativeToConfDir_PrefixAndSymlinkRoots(t *testing.T) {
	t.Parallel()
	base := t.TempDir()
	realDir := filepath.Join(base, "conf-real")
	if err := os.Mkdir(realDir, 0o755); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(base, "conf")
	if err := os.Symlink(realDir, link); err != nil {
		t.Skipf("symlink: %v", err)
	}
	resolvedReal, _ := filepath.EvalSymlinks(realDir)
	sep := string(filepath.Separator)
	msg := "defined in both " + resolvedReal + sep + "a.yaml and " + link + sep + "b.yaml; root " + resolvedReal + ", also " + link
	got := relativeToConfDir(msg, link)
	want := "defined in both a.yaml and b.yaml; root ., also ."
	if got != want {
		t.Errorf("relativeToConfDir =\n %q\nwant\n %q", got, want)
	}
	for _, frag := range []string{base, resolvedReal, "-real", "conf"} {
		if strings.Contains(got, frag) {
			t.Errorf("result still carries %q: %q", frag, got)
		}
	}
	// A sibling that merely starts with the root's name is not the root.
	sibling := link + "2" + sep + "x.yaml"
	if got := relativeToConfDir(sibling, link); got != sibling {
		t.Errorf("a sibling path was rewritten: %q → %q", sibling, got)
	}
}

// Q2: the relative spelling of conf.d is a substring of its absolute one;
// stripping it first would leave the absolute prefix behind.
func TestStripRoots_LongestFirst(t *testing.T) {
	t.Parallel()
	sep := string(filepath.Separator)
	abs := sep + filepath.Join("srv", "conf")
	msg := "duplicate in " + abs + sep + "a.yaml and " + abs
	got := stripRoots(msg, []string{"conf", abs})
	if want := "duplicate in a.yaml and ."; got != want {
		t.Errorf("stripRoots = %q, want %q", got, want)
	}
}

// R6: a root is rewritten only where it starts a path token.
func TestStripRoots_OnlyAtTheStartOfAPath(t *testing.T) {
	t.Parallel()
	cases := []struct {
		msg   string
		roots []string
		want  string
	}{
		{"read /conf.d/team/conf.d/a.yaml", []string{"/conf.d"}, "read team/conf.d/a.yaml"},
		{"read /conf.d/team/conf.d/a.yaml", []string{"conf.d"}, "read /conf.d/team/conf.d/a.yaml"},
		{"open /mnt/srv/conf/a.yaml: denied", []string{"/srv/conf"}, "open /mnt/srv/conf/a.yaml: denied"},
		{"open /srv/conf/a.yaml: denied", []string{"/srv/conf"}, "open a.yaml: denied"},
		{`both "/srv/conf/a.yaml" and (/srv/conf/b.yaml)`, []string{"/srv/conf"}, `both "a.yaml" and (b.yaml)`},
		{"path:/srv/conf/x.yaml", []string{"/srv/conf"}, "path:x.yaml"},
		{"/srv/conf/a.yaml at start", []string{"/srv/conf"}, "a.yaml at start"},
		{"no files in /srv/conf", []string{"/srv/conf"}, "no files in ."},
	}
	for _, c := range cases {
		if got := stripRoots(c.msg, c.roots); got != c.want {
			t.Errorf("stripRoots(%q, %v) = %q, want %q", c.msg, c.roots, got, c.want)
		}
	}
}

// R2: every unknown path drops the raw per-file state values; a load error
// used to keep them.
func TestListTenants_LoadErrorDropsRawState(t *testing.T) {
	t.Parallel()
	files := derivedFixture()
	files["t-dup.yaml"] = "tenants:\n  t-silent:\n    cpu: \"1\"\n" // the exporter rejects the tree
	dir := setupConfigDir(t, files)
	for _, it := range getList(t, &Deps{ConfigDir: dir, RBAC: openModeRBAC(t), SearchCache: NewTenantSnapshotCache()}) {
		if it.ConfigDerived != nil || it.SilentMode != "" || it.Maintenance != "" {
			t.Errorf("load-error row carries state: %+v", it)
		}
	}
}
