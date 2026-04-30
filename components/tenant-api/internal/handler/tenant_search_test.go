package handler

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/rbac"
)

// ── fixture helpers ─────────────────────────────────────────────────

// fixtureTenantYAML returns a minimal valid tenant.yaml the
// loadAllTenants() parser accepts. We expose only the metadata
// surface SearchTenants exercises (id / environment / tier / domain
// / db_type / owner / tags) — production tenant.yaml has more
// fields but they're irrelevant to search.
func fixtureTenantYAML(id, env, tier, domain, dbType, owner string, tags ...string) string {
	tagBlock := ""
	if len(tags) > 0 {
		// 6-space indent = sibling of environment / tier / etc. under
		// `_metadata:`. NOT 8-space — that would nest tags under
		// `owner:` instead, which silently makes the search-by-tag
		// tests fail (extractMetadata returns no tags). Bug pinned by
		// TestSearchTenants_TagFilterRequiresExactMatch / FreeTextMatchesTags.
		tagBlock = "      tags:\n"
		for _, t := range tags {
			tagBlock += fmt.Sprintf("        - %s\n", t)
		}
	}
	return fmt.Sprintf(`tenants:
  %s:
    _metadata:
      environment: %s
      tier: %s
      domain: %s
      db_type: %s
      owner: %s
%s    cpu:
      default: "70"
`, id, env, tier, domain, dbType, owner, tagBlock)
}

// makeFixtureDir creates a temp configDir populated with N tenants
// generated via the supplied factory. The factory receives the
// 0-indexed tenant number; it returns (filename, content).
func makeFixtureDir(t *testing.T, n int, factory func(i int) (string, string)) string {
	t.Helper()
	files := make(map[string]string, n)
	for i := 0; i < n; i++ {
		name, content := factory(i)
		files[name] = content
	}
	return setupConfigDir(t, files)
}

// runSearch issues GET /api/v1/tenants/search?<query> against a
// freshly-built handler and returns the parsed body + status.
//
// `idpGroups` is the list of IdP groups the request claims to come
// from. Empty slice = no group claim. We wrap the handler in the
// real `rbac.Middleware` (matching the production wiring in
// cmd/server/main.go) so context-stored identity reaches the handler
// — `rbac.RequestGroups()` reads from request context, not headers
// directly. Without the middleware the context value is nil and
// every test would silently behave as "open mode" regardless of
// the configured groups.
//
// `X-Forwarded-Email` is required by the middleware (it 401s if
// missing); we set a stub email ("test@example.com") on every
// request to exercise the realistic post-oauth2-proxy path.
func runSearch(t *testing.T, configDir string, mgr *rbac.Manager, idpGroups []string, query string) (*SearchResponse, int, []byte) {
	t.Helper()
	cache := NewTenantSnapshotCache()
	inner := SearchTenants(configDir, mgr, cache)
	// The list endpoint passes nil tenantIDFn (no per-tenant
	// permission check) — same as the production route binding.
	h := mgr.Middleware(rbac.PermRead, nil)(inner)

	url := "/api/v1/tenants/search"
	if query != "" {
		url += "?" + query
	}
	req := httptest.NewRequest(http.MethodGet, url, nil)
	req.Header.Set("X-Forwarded-Email", "test@example.com")
	if len(idpGroups) > 0 {
		req.Header.Set("X-Forwarded-Groups", strings.Join(idpGroups, ","))
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)

	body := rec.Body.Bytes()
	if rec.Code != http.StatusOK {
		return nil, rec.Code, body
	}
	var resp SearchResponse
	if err := json.Unmarshal(body, &resp); err != nil {
		t.Fatalf("decode response: %v\nbody: %s", err, body)
	}
	return &resp, rec.Code, body
}

// openModeRBAC returns a manager with no group config — the RBAC
// open-mode contract grants read to everything.
func openModeRBAC(t *testing.T) *rbac.Manager {
	t.Helper()
	return newRBACManager(t, "")
}

// ── happy path ─────────────────────────────────────────────────────

func TestSearchTenants_DefaultsReturnAllInPageSize(t *testing.T) {
	dir := makeFixtureDir(t, 3, func(i int) (string, string) {
		id := fmt.Sprintf("tenant-%d", i)
		return id + ".yaml", fixtureTenantYAML(id, "prod", "tier1", "db", "mariadb", "alice")
	})
	resp, code, _ := runSearch(t, dir, openModeRBAC(t), nil, "")
	if code != http.StatusOK {
		t.Fatalf("status = %d, want 200", code)
	}
	if len(resp.Items) != 3 {
		t.Errorf("items = %d, want 3", len(resp.Items))
	}
	if resp.TotalMatched != 3 {
		t.Errorf("total_matched = %d, want 3", resp.TotalMatched)
	}
	if resp.PageSize != defaultPageSize {
		t.Errorf("page_size = %d, want %d", resp.PageSize, defaultPageSize)
	}
	if resp.NextOffset != nil {
		t.Errorf("next_offset = %v, want nil for fully-fitting page", *resp.NextOffset)
	}
}

func TestSearchTenants_SortedByIDAscByDefault(t *testing.T) {
	// Insert in scrambled order; expect alphabetical id order out.
	dir := setupConfigDir(t, map[string]string{
		"zeta.yaml":  fixtureTenantYAML("zeta", "prod", "tier1", "db", "mariadb", "alice"),
		"alpha.yaml": fixtureTenantYAML("alpha", "prod", "tier1", "db", "mariadb", "alice"),
		"mid.yaml":   fixtureTenantYAML("mid", "prod", "tier1", "db", "mariadb", "alice"),
	})
	resp, _, _ := runSearch(t, dir, openModeRBAC(t), nil, "")
	got := []string{resp.Items[0].ID, resp.Items[1].ID, resp.Items[2].ID}
	want := []string{"alpha", "mid", "zeta"}
	for i := range got {
		if got[i] != want[i] {
			t.Errorf("position %d: got %q, want %q", i, got[i], want[i])
		}
	}
}

// ── pagination ─────────────────────────────────────────────────────

func TestSearchTenants_PaginationProducesStableSlices(t *testing.T) {
	dir := makeFixtureDir(t, 25, func(i int) (string, string) {
		id := fmt.Sprintf("t-%02d", i)
		return id + ".yaml", fixtureTenantYAML(id, "prod", "tier1", "db", "mariadb", "alice")
	})
	// page_size=10 → expect 3 pages (10, 10, 5).
	page1, _, _ := runSearch(t, dir, openModeRBAC(t), nil, "page_size=10&offset=0")
	page2, _, _ := runSearch(t, dir, openModeRBAC(t), nil, "page_size=10&offset=10")
	page3, _, _ := runSearch(t, dir, openModeRBAC(t), nil, "page_size=10&offset=20")

	if len(page1.Items) != 10 || len(page2.Items) != 10 || len(page3.Items) != 5 {
		t.Errorf("page sizes = %d/%d/%d, want 10/10/5",
			len(page1.Items), len(page2.Items), len(page3.Items))
	}
	if page1.NextOffset == nil || *page1.NextOffset != 10 {
		t.Errorf("page1.next_offset = %v, want 10", page1.NextOffset)
	}
	if page2.NextOffset == nil || *page2.NextOffset != 20 {
		t.Errorf("page2.next_offset = %v, want 20", page2.NextOffset)
	}
	if page3.NextOffset != nil {
		t.Errorf("page3.next_offset = %d, want nil (last page)", *page3.NextOffset)
	}

	// Concatenating pages must produce a deterministic sorted set with
	// no gaps + no duplicates. This is THE invariant pagination has
	// to maintain across requests.
	all := append(append(page1.Items, page2.Items...), page3.Items...)
	if len(all) != 25 {
		t.Fatalf("concatenated = %d items, want 25", len(all))
	}
	for i := 0; i < 25; i++ {
		want := fmt.Sprintf("t-%02d", i)
		if all[i].ID != want {
			t.Errorf("position %d: got %q, want %q", i, all[i].ID, want)
		}
	}
}

func TestSearchTenants_OffsetPastEndReturnsEmpty(t *testing.T) {
	dir := makeFixtureDir(t, 3, func(i int) (string, string) {
		id := fmt.Sprintf("t-%d", i)
		return id + ".yaml", fixtureTenantYAML(id, "prod", "tier1", "db", "mariadb", "alice")
	})
	resp, code, _ := runSearch(t, dir, openModeRBAC(t), nil, "page_size=10&offset=100")
	if code != http.StatusOK {
		t.Fatalf("status = %d, want 200 (empty page is not an error)", code)
	}
	if len(resp.Items) != 0 {
		t.Errorf("items = %d, want 0", len(resp.Items))
	}
	if resp.TotalMatched != 3 {
		t.Errorf("total_matched should still reflect filter result; got %d, want 3", resp.TotalMatched)
	}
	if resp.NextOffset != nil {
		t.Errorf("next_offset should be nil past the end; got %d", *resp.NextOffset)
	}
}

// ── 400 paths ──────────────────────────────────────────────────────

func TestSearchTenants_PageSizeOverMaxReturns400(t *testing.T) {
	dir := makeFixtureDir(t, 1, func(i int) (string, string) {
		return "x.yaml", fixtureTenantYAML("x", "prod", "tier1", "db", "mariadb", "alice")
	})
	_, code, body := runSearch(t, dir, openModeRBAC(t), nil, "page_size=501")
	if code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", code)
	}
	if !strings.Contains(string(body), "page_size exceeds max 500") {
		t.Errorf("body should explain the cap; got %s", body)
	}
}

func TestSearchTenants_NegativePageSizeReturns400(t *testing.T) {
	dir := makeFixtureDir(t, 1, func(i int) (string, string) {
		return "x.yaml", fixtureTenantYAML("x", "prod", "tier1", "db", "mariadb", "alice")
	})
	_, code, _ := runSearch(t, dir, openModeRBAC(t), nil, "page_size=-5")
	if code != http.StatusBadRequest {
		t.Errorf("status = %d, want 400", code)
	}
}

func TestSearchTenants_NegativeOffsetReturns400(t *testing.T) {
	dir := makeFixtureDir(t, 1, func(i int) (string, string) {
		return "x.yaml", fixtureTenantYAML("x", "prod", "tier1", "db", "mariadb", "alice")
	})
	_, code, _ := runSearch(t, dir, openModeRBAC(t), nil, "offset=-1")
	if code != http.StatusBadRequest {
		t.Errorf("status = %d, want 400", code)
	}
}

func TestSearchTenants_UnknownSortKeyReturns400(t *testing.T) {
	dir := makeFixtureDir(t, 1, func(i int) (string, string) {
		return "x.yaml", fixtureTenantYAML("x", "prod", "tier1", "db", "mariadb", "alice")
	})
	_, code, body := runSearch(t, dir, openModeRBAC(t), nil, "sort=created_at")
	if code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", code)
	}
	if !strings.Contains(string(body), "sort must be one of") {
		t.Errorf("body should list valid sort keys; got %s", body)
	}
}

func TestSearchTenants_NonNumericPageSizeReturns400(t *testing.T) {
	dir := makeFixtureDir(t, 1, func(i int) (string, string) {
		return "x.yaml", fixtureTenantYAML("x", "prod", "tier1", "db", "mariadb", "alice")
	})
	_, code, _ := runSearch(t, dir, openModeRBAC(t), nil, "page_size=banana")
	if code != http.StatusBadRequest {
		t.Errorf("status = %d, want 400", code)
	}
}

// ── filters ────────────────────────────────────────────────────────

func TestSearchTenants_EnvironmentFilterExactMatch(t *testing.T) {
	dir := setupConfigDir(t, map[string]string{
		"prod-1.yaml":    fixtureTenantYAML("prod-1", "prod", "tier1", "db", "mariadb", "alice"),
		"prod-2.yaml":    fixtureTenantYAML("prod-2", "prod", "tier2", "db", "mariadb", "alice"),
		"staging-1.yaml": fixtureTenantYAML("staging-1", "staging", "tier1", "db", "mariadb", "alice"),
	})
	resp, _, _ := runSearch(t, dir, openModeRBAC(t), nil, "environment=prod")
	if resp.TotalMatched != 2 {
		t.Errorf("total_matched = %d, want 2 (prod-1, prod-2)", resp.TotalMatched)
	}
	for _, t0 := range resp.Items {
		if t0.Environment != "prod" {
			t.Errorf("got tenant in wrong env: %+v", t0)
		}
	}
}

func TestSearchTenants_MultipleFiltersAreANDed(t *testing.T) {
	dir := setupConfigDir(t, map[string]string{
		"a.yaml": fixtureTenantYAML("a", "prod", "tier1", "billing", "mariadb", "alice"),
		"b.yaml": fixtureTenantYAML("b", "prod", "tier1", "ops", "mariadb", "alice"),
		"c.yaml": fixtureTenantYAML("c", "prod", "tier2", "billing", "mariadb", "alice"),
		"d.yaml": fixtureTenantYAML("d", "staging", "tier1", "billing", "mariadb", "alice"),
	})
	// Only `a` matches all three (env=prod, tier=tier1, domain=billing).
	resp, _, _ := runSearch(t, dir, openModeRBAC(t), nil,
		"environment=prod&tier=tier1&domain=billing")
	if resp.TotalMatched != 1 {
		t.Fatalf("total_matched = %d, want 1", resp.TotalMatched)
	}
	if resp.Items[0].ID != "a" {
		t.Errorf("matched item = %q, want %q", resp.Items[0].ID, "a")
	}
}

func TestSearchTenants_TagFilterRequiresExactMatch(t *testing.T) {
	dir := setupConfigDir(t, map[string]string{
		"a.yaml": fixtureTenantYAML("a", "prod", "tier1", "db", "mariadb", "alice", "high-traffic", "audited"),
		"b.yaml": fixtureTenantYAML("b", "prod", "tier1", "db", "mariadb", "alice", "audited"),
		"c.yaml": fixtureTenantYAML("c", "prod", "tier1", "db", "mariadb", "alice"),
	})
	resp, _, _ := runSearch(t, dir, openModeRBAC(t), nil, "tag=audited")
	if resp.TotalMatched != 2 {
		t.Errorf("total_matched = %d, want 2 (a, b)", resp.TotalMatched)
	}
}

// ── free-text search ───────────────────────────────────────────────

func TestSearchTenants_FreeTextMatchesIDOwnerDomain(t *testing.T) {
	dir := setupConfigDir(t, map[string]string{
		"alpha-prod.yaml": fixtureTenantYAML("alpha-prod", "prod", "tier1", "billing", "mariadb", "alice"),
		"beta.yaml":       fixtureTenantYAML("beta", "prod", "tier1", "alpha-domain", "mariadb", "bob"),
		"gamma.yaml":      fixtureTenantYAML("gamma", "prod", "tier1", "ops", "mariadb", "alpha-team"),
		"unrelated.yaml":  fixtureTenantYAML("unrelated", "prod", "tier1", "ops", "mariadb", "carol"),
	})
	// `q=alpha` should match: alpha-prod (id), beta (domain), gamma (owner)
	resp, _, _ := runSearch(t, dir, openModeRBAC(t), nil, "q=alpha")
	if resp.TotalMatched != 3 {
		t.Errorf("total_matched = %d, want 3 (alpha-prod / beta / gamma)", resp.TotalMatched)
	}
	ids := map[string]bool{}
	for _, t0 := range resp.Items {
		ids[t0.ID] = true
	}
	for _, want := range []string{"alpha-prod", "beta", "gamma"} {
		if !ids[want] {
			t.Errorf("expected %q in results; got %v", want, ids)
		}
	}
	if ids["unrelated"] {
		t.Errorf("'unrelated' should not match q=alpha; got %v", ids)
	}
}

func TestSearchTenants_FreeTextIsCaseInsensitive(t *testing.T) {
	dir := setupConfigDir(t, map[string]string{
		"Tenant-A.yaml": fixtureTenantYAML("Tenant-A", "prod", "tier1", "billing", "mariadb", "alice"),
	})
	for _, q := range []string{"tenant-a", "TENANT-A", "TeNaNt-A"} {
		resp, _, _ := runSearch(t, dir, openModeRBAC(t), nil, "q="+q)
		if resp.TotalMatched != 1 {
			t.Errorf("q=%q: matched %d, want 1 (case insensitive)", q, resp.TotalMatched)
		}
	}
}

func TestSearchTenants_FreeTextMatchesTags(t *testing.T) {
	dir := setupConfigDir(t, map[string]string{
		"a.yaml": fixtureTenantYAML("a", "prod", "tier1", "db", "mariadb", "alice", "kubernetes"),
		"b.yaml": fixtureTenantYAML("b", "prod", "tier1", "db", "mariadb", "alice", "vmware"),
	})
	resp, _, _ := runSearch(t, dir, openModeRBAC(t), nil, "q=kuber")
	if resp.TotalMatched != 1 || resp.Items[0].ID != "a" {
		t.Errorf("expected only 'a' to match q=kuber; got %+v", resp)
	}
}

// ── RBAC interaction ───────────────────────────────────────────────

func TestSearchTenants_RBACFiltersBeforePagination(t *testing.T) {
	// 5 tenants — 3 in env=prod, 2 in env=staging. RBAC config:
	// caller's group sees only env=staging.
	dir := setupConfigDir(t, map[string]string{
		"prod-1.yaml":    fixtureTenantYAML("prod-1", "prod", "tier1", "db", "mariadb", "alice"),
		"prod-2.yaml":    fixtureTenantYAML("prod-2", "prod", "tier1", "db", "mariadb", "alice"),
		"prod-3.yaml":    fixtureTenantYAML("prod-3", "prod", "tier1", "db", "mariadb", "alice"),
		"staging-1.yaml": fixtureTenantYAML("staging-1", "staging", "tier1", "db", "mariadb", "alice"),
		"staging-2.yaml": fixtureTenantYAML("staging-2", "staging", "tier1", "db", "mariadb", "alice"),
	})
	rbacYAML := `groups:
  - name: staging-only
    tenants: ["*"]
    environments: ["staging"]
    permissions: [read]
`
	mgr := newRBACManager(t, rbacYAML)
	resp, _, _ := runSearch(t, dir, mgr, []string{"staging-only"}, "")
	if resp.TotalMatched != 2 {
		t.Errorf("total_matched = %d, want 2 (only staging tenants visible)", resp.TotalMatched)
	}
	for _, t0 := range resp.Items {
		if t0.Environment != "staging" {
			t.Errorf("RBAC leaked: %+v", t0)
		}
	}
}

// ── snapshot cache ─────────────────────────────────────────────────

func TestSnapshotCache_ReuseDuringTTL(t *testing.T) {
	cache := NewTenantSnapshotCache()
	cache.ttl = 5 * time.Second // explicit, easy to read
	dir := setupConfigDir(t, map[string]string{
		"a.yaml": fixtureTenantYAML("a", "prod", "tier1", "db", "mariadb", "alice"),
	})
	first, err := cache.snapshot(dir)
	if err != nil {
		t.Fatalf("first snapshot: %v", err)
	}
	if len(first) != 1 {
		t.Fatalf("first len = %d, want 1", len(first))
	}
	loadedAt1 := cache.loadedAt

	// Mutate disk — add a second tenant. Within TTL the snapshot
	// must NOT pick it up (proves cache reuse).
	if err := os.WriteFile(filepath.Join(dir, "b.yaml"),
		[]byte(fixtureTenantYAML("b", "prod", "tier1", "db", "mariadb", "alice")),
		0644); err != nil {
		t.Fatalf("write b: %v", err)
	}
	second, err := cache.snapshot(dir)
	if err != nil {
		t.Fatalf("second snapshot: %v", err)
	}
	if len(second) != 1 {
		t.Errorf("snapshot updated mid-TTL — wanted stale, got fresh (len=%d)", len(second))
	}
	if !cache.loadedAt.Equal(loadedAt1) {
		t.Errorf("loadedAt changed mid-TTL")
	}
}

func TestSnapshotCache_RebuildsAfterTTL(t *testing.T) {
	cache := NewTenantSnapshotCache()
	cache.ttl = 1 * time.Millisecond
	dir := setupConfigDir(t, map[string]string{
		"a.yaml": fixtureTenantYAML("a", "prod", "tier1", "db", "mariadb", "alice"),
	})
	if _, err := cache.snapshot(dir); err != nil {
		t.Fatalf("first snapshot: %v", err)
	}

	// Wait past TTL + add tenant; second snapshot MUST see it.
	time.Sleep(5 * time.Millisecond)
	if err := os.WriteFile(filepath.Join(dir, "b.yaml"),
		[]byte(fixtureTenantYAML("b", "prod", "tier1", "db", "mariadb", "alice")),
		0644); err != nil {
		t.Fatalf("write b: %v", err)
	}
	second, err := cache.snapshot(dir)
	if err != nil {
		t.Fatalf("second snapshot: %v", err)
	}
	if len(second) != 2 {
		t.Errorf("post-TTL snapshot len = %d, want 2 (rebuild)", len(second))
	}
}

func TestSnapshotCache_InvalidateForcesRebuild(t *testing.T) {
	cache := NewTenantSnapshotCache()
	dir := setupConfigDir(t, map[string]string{
		"a.yaml": fixtureTenantYAML("a", "prod", "tier1", "db", "mariadb", "alice"),
	})
	if _, err := cache.snapshot(dir); err != nil {
		t.Fatalf("first snapshot: %v", err)
	}
	if err := os.WriteFile(filepath.Join(dir, "b.yaml"),
		[]byte(fixtureTenantYAML("b", "prod", "tier1", "db", "mariadb", "alice")),
		0644); err != nil {
		t.Fatalf("write b: %v", err)
	}
	cache.invalidate()
	second, err := cache.snapshot(dir)
	if err != nil {
		t.Fatalf("second snapshot: %v", err)
	}
	if len(second) != 2 {
		t.Errorf("post-invalidate len = %d, want 2", len(second))
	}
}
