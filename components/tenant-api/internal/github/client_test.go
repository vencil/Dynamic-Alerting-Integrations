package github

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/platform"
)

func TestNewClient(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name    string
		repo    string
		wantErr bool
	}{
		{"valid", "owner/repo", false},
		{"no slash", "ownerrepo", true},
		{"empty owner", "/repo", true},
		{"empty repo", "owner/", true},
		{"empty string", "", true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			_, err := NewClient("token", tt.repo, "main")
			if (err != nil) != tt.wantErr {
				t.Errorf("NewClient() error = %v, wantErr %v", err, tt.wantErr)
			}
		})
	}
}

func TestNewClientDefaultBranch(t *testing.T) {
	t.Parallel()
	c, err := NewClient("tok", "o/r", "")
	if err != nil {
		t.Fatal(err)
	}
	if c.baseBranch != "main" {
		t.Errorf("expected default branch 'main', got %q", c.baseBranch)
	}
}

func TestProviderName(t *testing.T) {
	t.Parallel()
	c, _ := NewClient("tok", "o/r", "main")
	if c.ProviderName() != "GitHub" {
		t.Errorf("expected 'GitHub', got %q", c.ProviderName())
	}
}

func TestValidateToken(t *testing.T) {
	t.Parallel()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer good-token" {
			w.WriteHeader(http.StatusUnauthorized)
			fmt.Fprint(w, `{"message":"bad credentials"}`)
			return
		}
		fmt.Fprint(w, `{"login":"testuser"}`)
	}))
	defer srv.Close()

	c, _ := NewClient("good-token", "owner/repo", "main")
	c.SetBaseURL(srv.URL)
	if err := c.ValidateToken(); err != nil {
		t.Errorf("expected valid token, got error: %v", err)
	}

	c2, _ := NewClient("bad-token", "owner/repo", "main")
	c2.SetBaseURL(srv.URL)
	if err := c2.ValidateToken(); err == nil {
		t.Error("expected error for bad token")
	}
}

func TestCreatePR(t *testing.T) {
	t.Parallel()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/pulls") && r.Method == "POST" {
			resp := map[string]interface{}{
				"number":     42,
				"html_url":   "https://github.com/owner/repo/pull/42",
				"state":      "open",
				"title":      "[tenant-api] Update db-a-prod",
				"head":       map[string]string{"ref": "tenant-api/db-a-prod/20260406"},
				"created_at": "2026-04-06T14:00:00Z",
			}
			w.Header().Set("Content-Type", "application/json")
			json.NewEncoder(w).Encode(resp)
			return
		}
		// Label endpoint (best-effort)
		if strings.Contains(r.URL.Path, "/labels") {
			w.WriteHeader(http.StatusOK)
			fmt.Fprint(w, `[]`)
			return
		}
		w.WriteHeader(http.StatusNotFound)
	}))
	defer srv.Close()

	c, _ := NewClient("token", "owner/repo", "main")
	c.SetBaseURL(srv.URL)

	pr, err := c.CreatePR("title", "body", "test-branch", []string{"auto"})
	if err != nil {
		t.Fatalf("CreatePR() error: %v", err)
	}
	if pr.Number != 42 {
		t.Errorf("expected PR #42, got #%d", pr.Number)
	}
	if pr.WebURL != "https://github.com/owner/repo/pull/42" {
		t.Errorf("unexpected URL: %s", pr.WebURL)
	}
	if pr.State != "open" {
		t.Errorf("expected state 'open', got %q", pr.State)
	}
}

func TestListOpenPRs(t *testing.T) {
	t.Parallel()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		prs := []map[string]interface{}{
			{
				"number":     1,
				"html_url":   "https://github.com/owner/repo/pull/1",
				"state":      "open",
				"title":      "[tenant-api] Update db-a",
				"head":       map[string]string{"ref": "tenant-api/db-a/20260406"},
				"created_at": "2026-04-06T10:00:00Z",
			},
			{
				"number":     2,
				"html_url":   "https://github.com/owner/repo/pull/2",
				"state":      "open",
				"title":      "Manual PR",
				"head":       map[string]string{"ref": "feature/manual-change"},
				"created_at": "2026-04-06T11:00:00Z",
			},
			{
				"number":     3,
				"html_url":   "https://github.com/owner/repo/pull/3",
				"state":      "open",
				"title":      "[tenant-api] Batch",
				"head":       map[string]string{"ref": "tenant-api/batch/20260406"},
				"created_at": "2026-04-06T12:00:00Z",
			},
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(prs)
	}))
	defer srv.Close()

	c, _ := NewClient("token", "owner/repo", "main")
	c.SetBaseURL(srv.URL)

	prs, err := c.ListOpenPRs()
	if err != nil {
		t.Fatalf("ListOpenPRs() error: %v", err)
	}

	// Should only include tenant-api/* PRs
	if len(prs) != 2 {
		t.Fatalf("expected 2 tenant-api PRs, got %d", len(prs))
	}

	if prs[0].TenantID != "db-a" {
		t.Errorf("expected tenant_id 'db-a', got %q", prs[0].TenantID)
	}
	if prs[1].TenantID != "batch" {
		t.Errorf("expected tenant_id 'batch', got %q", prs[1].TenantID)
	}
}

func TestListOpenPRs_APIError(t *testing.T) {
	t.Parallel()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		fmt.Fprint(w, `{"message":"internal error"}`)
	}))
	defer srv.Close()

	c, _ := NewClient("token", "owner/repo", "main")
	c.SetBaseURL(srv.URL)

	_, err := c.ListOpenPRs()
	if err == nil {
		t.Error("expected error from API failure")
	}
}

// TestListOpenPRs_Pagination asserts the Link-header loop fetches every page,
// so >100 open tenant-api PRs are all enumerated (the bug: single-page fetch
// truncated at 100, hiding a tenant's pending PR from dedup → duplicate PR).
func TestListOpenPRs_Pagination(t *testing.T) {
	t.Parallel()
	var srv *httptest.Server
	srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		page := r.URL.Query().Get("page")
		w.Header().Set("Content-Type", "application/json")
		switch page {
		case "", "1":
			// Full page of 100 + a rel="next" link to page 2. The Link URL is
			// absolute (like the real API); nextPagePath strips the host.
			w.Header().Set("Link",
				fmt.Sprintf(`<%s/repos/owner/repo/pulls?state=open&per_page=100&page=2>; rel="next", `+
					`<%s/repos/owner/repo/pulls?state=open&per_page=100&page=2>; rel="last"`, srv.URL, srv.URL))
			json.NewEncoder(w).Encode(makePRPage(1, 100))
		case "2":
			// 50 more, no Link header → last page.
			json.NewEncoder(w).Encode(makePRPage(101, 50))
		default:
			fmt.Fprint(w, `[]`)
		}
	}))
	defer srv.Close()

	c, _ := NewClient("token", "owner/repo", "main")
	c.SetBaseURL(srv.URL)

	prs, err := c.ListOpenPRs()
	if err != nil {
		t.Fatalf("ListOpenPRs() error: %v", err)
	}
	if len(prs) != 150 {
		t.Fatalf("expected 150 PRs across 2 pages, got %d", len(prs))
	}
	// Spot-check an entry from the second page proves it wasn't truncated.
	if prs[149].TenantID != "db-150" {
		t.Errorf("last PR tenant = %q, want db-150", prs[149].TenantID)
	}
}

// makePRPage builds `count` tenant-api PR JSON objects numbered start..start+count-1.
func makePRPage(start, count int) []map[string]interface{} {
	out := make([]map[string]interface{}, count)
	for i := 0; i < count; i++ {
		n := start + i
		out[i] = map[string]interface{}{
			"number":     n,
			"html_url":   fmt.Sprintf("https://github.com/owner/repo/pull/%d", n),
			"state":      "open",
			"title":      fmt.Sprintf("[tenant-api] Update db-%d", n),
			"head":       map[string]string{"ref": fmt.Sprintf("tenant-api/db-%d/20260406", n)},
			"created_at": "2026-04-06T10:00:00Z",
		}
	}
	return out
}

func TestNextPagePath(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name string
		link string
		want string
	}{
		{"empty", "", ""},
		{"no next", `<https://api.github.com/x?page=5>; rel="last"`, ""},
		{
			name: "next present",
			link: `<https://api.github.com/repos/o/r/pulls?state=open&per_page=100&page=2>; rel="next", ` +
				`<https://api.github.com/repos/o/r/pulls?state=open&per_page=100&page=9>; rel="last"`,
			want: "/repos/o/r/pulls?state=open&per_page=100&page=2",
		},
		{
			name: "next is second segment",
			link: `<https://api.github.com/x?page=9>; rel="last", <https://api.github.com/x?page=2>; rel="next"`,
			want: "/x?page=2",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			if got := nextPagePath(tt.link); got != tt.want {
				t.Errorf("nextPagePath(%q) = %q, want %q", tt.link, got, tt.want)
			}
		})
	}
}

// TestCreatePR_Forbidden asserts a 403 from the forge (token passed
// ValidateToken's /user check but lacks pull_requests:write) surfaces as
// platform.ErrForbidden — and that the upstream response body never leaks
// into the error string.
func TestCreatePR_Forbidden(t *testing.T) {
	t.Parallel()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		fmt.Fprint(w, `{"message":"Resource not accessible by personal access token","documentation_url":"https://docs.github.com/secret"}`)
	}))
	defer srv.Close()

	c, _ := NewClient("read-only-token", "owner/repo", "main")
	c.SetBaseURL(srv.URL)

	_, err := c.CreatePR("title", "body", "tenant-api/db-a/ts", nil)
	if err == nil {
		t.Fatal("expected error for 403 response")
	}
	if !errors.Is(err, platform.ErrForbidden) {
		t.Errorf("expected errors.Is(err, ErrForbidden), got %v", err)
	}
	if strings.Contains(err.Error(), "Resource not accessible") || strings.Contains(err.Error(), "docs.github.com/secret") {
		t.Errorf("error leaked upstream body: %v", err)
	}
}

// TestClient_BreakerProviderLabelIsLowercase pins the circuit-breaker provider
// label to lowercase "github" so it matches the PollingTracker's provider tag
// (NewTracker passes "github"). If these drift, tenant_api_forge_circuit_state
// and tenant_api_forge_pr_conflicts would carry different {provider} label
// values and could not be joined/filtered together on a dashboard (#632 review
// round 3 — Gemini caught the original GitHub/github casing mismatch).
func TestClient_BreakerProviderLabelIsLowercase(t *testing.T) {
	// Not parallel: reads the package-level breaker registry.
	if _, err := NewClient("token", "owner/repo", "main"); err != nil {
		t.Fatalf("NewClient: %v", err)
	}
	snap := platform.CircuitSnapshot()
	if _, ok := snap["github"]; !ok {
		t.Errorf("breaker registered under %v, want lowercase key \"github\" (must match NewTracker's provider tag)", keysOf(snap))
	}
	if _, ok := snap["GitHub"]; ok {
		t.Error("breaker registered under TitleCase \"GitHub\" — drifts from the tracker's lowercase \"github\" provider label")
	}
}

func keysOf(m map[string]string) []string {
	ks := make([]string, 0, len(m))
	for k := range m {
		ks = append(ks, k)
	}
	return ks
}

// ── CreateBranch / DeleteBranch ─────────────────────────────────────────
//
// Branch operations were the untested half of the write path (the gitlab
// client has the mirror-image tests); same depth here: happy path with
// payload assertions, 404 semantics, 403 → ErrForbidden without leaking
// the upstream body.

// TestCreateBranch drives the two-step create: GET the base branch ref,
// then POST the new fully-qualified ref carrying the base SHA.
func TestCreateBranch(t *testing.T) {
	t.Parallel()
	const baseSHA = "abc123def456"
	var createdRef struct {
		Ref string `json:"ref"`
		SHA string `json:"sha"`
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == "GET" && strings.HasSuffix(r.URL.Path, "/git/ref/heads/main"):
			w.Header().Set("Content-Type", "application/json")
			fmt.Fprintf(w, `{"ref":"refs/heads/main","object":{"sha":"%s","type":"commit"}}`, baseSHA)
		case r.Method == "POST" && strings.HasSuffix(r.URL.Path, "/git/refs"):
			if err := json.NewDecoder(r.Body).Decode(&createdRef); err != nil {
				t.Errorf("decode create-ref payload: %v", err)
			}
			w.WriteHeader(http.StatusCreated)
			fmt.Fprint(w, `{"ref":"refs/heads/tenant-api/db-a/20260716"}`)
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer srv.Close()

	c, _ := NewClient("token", "owner/repo", "main")
	c.SetBaseURL(srv.URL)

	if err := c.CreateBranch("tenant-api/db-a/20260716"); err != nil {
		t.Fatalf("CreateBranch() error: %v", err)
	}
	if createdRef.Ref != "refs/heads/tenant-api/db-a/20260716" {
		t.Errorf("created ref = %q, want fully-qualified refs/heads/... form", createdRef.Ref)
	}
	if createdRef.SHA != baseSHA {
		t.Errorf("created ref sha = %q, want the base branch HEAD %q", createdRef.SHA, baseSHA)
	}
}

// A missing base branch (404 on the ref lookup) fails the FIRST step —
// the create-ref POST must never fire.
func TestCreateBranch_BaseRefMissing(t *testing.T) {
	t.Parallel()
	var refPosts int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == "POST" {
			refPosts++
		}
		w.WriteHeader(http.StatusNotFound)
		fmt.Fprint(w, `{"message":"Not Found"}`)
	}))
	defer srv.Close()

	c, _ := NewClient("token", "owner/repo", "main")
	c.SetBaseURL(srv.URL)

	err := c.CreateBranch("tenant-api/db-a/20260716")
	if err == nil || !strings.Contains(err.Error(), "get base branch") {
		t.Fatalf("err = %v, want get-base-branch failure", err)
	}
	var apiErr *platform.APIError
	if !errors.As(err, &apiErr) || apiErr.StatusCode != http.StatusNotFound {
		t.Errorf("err = %v, want a wrapped APIError with StatusCode 404", err)
	}
	if refPosts != 0 {
		t.Errorf("create-ref POST fired %d times after a failed base lookup, want 0", refPosts)
	}
}

// An unparseable base-ref response fails before the create-ref POST.
func TestCreateBranch_BaseRefUnparseable(t *testing.T) {
	t.Parallel()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `not json at all`)
	}))
	defer srv.Close()

	c, _ := NewClient("token", "owner/repo", "main")
	c.SetBaseURL(srv.URL)

	if err := c.CreateBranch("b"); err == nil || !strings.Contains(err.Error(), "parse base ref") {
		t.Fatalf("err = %v, want parse-base-ref failure", err)
	}
}

// A 403 on the ref creation (token has contents:read only) surfaces as
// platform.ErrForbidden and never leaks the upstream body — mirroring
// TestCreatePR_Forbidden.
func TestCreateBranch_CreateRefForbidden(t *testing.T) {
	t.Parallel()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == "GET" {
			fmt.Fprint(w, `{"object":{"sha":"abc123"}}`)
			return
		}
		w.WriteHeader(http.StatusForbidden)
		fmt.Fprint(w, `{"message":"Resource not accessible by personal access token"}`)
	}))
	defer srv.Close()

	c, _ := NewClient("read-only-token", "owner/repo", "main")
	c.SetBaseURL(srv.URL)

	err := c.CreateBranch("tenant-api/db-a/20260716")
	if err == nil {
		t.Fatal("expected error for 403 on ref creation")
	}
	if !errors.Is(err, platform.ErrForbidden) {
		t.Errorf("expected errors.Is(err, ErrForbidden), got %v", err)
	}
	if strings.Contains(err.Error(), "Resource not accessible") {
		t.Errorf("error leaked upstream body: %v", err)
	}
}

func TestDeleteBranch(t *testing.T) {
	t.Parallel()
	var gotMethod, gotPath string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotMethod, gotPath = r.Method, r.URL.Path
		w.WriteHeader(http.StatusNoContent)
	}))
	defer srv.Close()

	c, _ := NewClient("token", "owner/repo", "main")
	c.SetBaseURL(srv.URL)

	if err := c.DeleteBranch("tenant-api/db-a/20260716"); err != nil {
		t.Fatalf("DeleteBranch() error: %v", err)
	}
	if gotMethod != "DELETE" {
		t.Errorf("method = %q, want DELETE", gotMethod)
	}
	if want := "/repos/owner/repo/git/refs/heads/tenant-api/db-a/20260716"; gotPath != want {
		t.Errorf("path = %q, want %q", gotPath, want)
	}
}

// Deleting a branch that is already gone is an error the caller sees
// (cleanup flows log it); the status code must survive as an APIError.
func TestDeleteBranch_NotFound(t *testing.T) {
	t.Parallel()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnprocessableEntity)
		fmt.Fprint(w, `{"message":"Reference does not exist"}`)
	}))
	defer srv.Close()

	c, _ := NewClient("token", "owner/repo", "main")
	c.SetBaseURL(srv.URL)

	err := c.DeleteBranch("tenant-api/db-a/gone")
	if err == nil || !strings.Contains(err.Error(), "delete branch") {
		t.Fatalf("err = %v, want delete-branch failure", err)
	}
	var apiErr *platform.APIError
	if !errors.As(err, &apiErr) || apiErr.StatusCode != http.StatusUnprocessableEntity {
		t.Errorf("err = %v, want a wrapped APIError with StatusCode 422", err)
	}
	if strings.Contains(err.Error(), "Reference does not exist") {
		t.Errorf("error leaked upstream body: %v", err)
	}
}

// TestClient_CircuitBreakerTripsThroughDo is the integration test for #632/#645:
// it drives the REAL client.do() path (not the platform.CircuitBreaker in
// isolation) against an httptest server that always returns 503, and asserts
// the breaker eventually fast-fails with platform.ErrCircuitOpen. Mirrors the
// PR #653 lesson — unit-testing the breaker wrapper alone wouldn't catch a
// wiring regression where do() bypassed the breaker.
func TestClient_CircuitBreakerTripsThroughDo(t *testing.T) {
	t.Parallel()
	var hits int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits++
		w.WriteHeader(http.StatusServiceUnavailable)
		fmt.Fprint(w, `{"message":"503"}`)
	}))
	defer srv.Close()

	c, _ := NewClient("token", "owner/repo", "main")
	c.SetBaseURL(srv.URL)

	// Hammer until the breaker opens. Each ListOpenPRs is one do() → one 503.
	var sawCircuitOpen bool
	for i := 0; i < 20; i++ {
		_, err := c.ListOpenPRs()
		if errors.Is(err, platform.ErrCircuitOpen) {
			sawCircuitOpen = true
			break
		}
	}
	if !sawCircuitOpen {
		t.Fatal("breaker never fast-failed with ErrCircuitOpen after 20 consecutive 503s — do() may not be wired through the breaker")
	}
	// Once open, further calls must NOT reach the server: record the hit count
	// at trip time, make another call, and confirm the server wasn't hit again.
	hitsAtTrip := hits
	_, err := c.ListOpenPRs()
	if !errors.Is(err, platform.ErrCircuitOpen) {
		t.Errorf("post-trip call err = %v, want ErrCircuitOpen", err)
	}
	if hits != hitsAtTrip {
		t.Errorf("server hit again after breaker opened (%d → %d) — fast-fail not short-circuiting the round-trip", hitsAtTrip, hits)
	}
}
