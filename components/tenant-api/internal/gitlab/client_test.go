package gitlab

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestNewClient(t *testing.T) {
	tests := []struct {
		name    string
		project string
		wantErr bool
	}{
		{"valid path", "group/project", false},
		{"valid id", "12345", false},
		{"empty", "", true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			_, err := NewClient("token", tt.project, "main")
			if (err != nil) != tt.wantErr {
				t.Errorf("NewClient() error = %v, wantErr %v", err, tt.wantErr)
			}
		})
	}
}

func TestNewClientDefaultBranch(t *testing.T) {
	c, err := NewClient("tok", "group/project", "")
	if err != nil {
		t.Fatal(err)
	}
	if c.targetBranch != "main" {
		t.Errorf("expected default branch 'main', got %q", c.targetBranch)
	}
}

func TestProviderName(t *testing.T) {
	c, _ := NewClient("tok", "group/project", "main")
	if c.ProviderName() != "GitLab" {
		t.Errorf("expected 'GitLab', got %q", c.ProviderName())
	}
}

func TestValidateToken(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("PRIVATE-TOKEN") != "good-token" {
			w.WriteHeader(http.StatusUnauthorized)
			fmt.Fprint(w, `{"message":"401 Unauthorized"}`)
			return
		}
		fmt.Fprint(w, `{"id":1,"username":"testuser"}`)
	}))
	defer srv.Close()

	c, _ := NewClient("good-token", "group/project", "main")
	c.SetBaseURL(srv.URL)
	if err := c.ValidateToken(); err != nil {
		t.Errorf("expected valid token, got error: %v", err)
	}

	c2, _ := NewClient("bad-token", "group/project", "main")
	c2.SetBaseURL(srv.URL)
	if err := c2.ValidateToken(); err == nil {
		t.Error("expected error for bad token")
	}
}

func TestCreateBranch(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == "POST" && strings.Contains(r.URL.Path, "/repository/branches") {
			w.Header().Set("Content-Type", "application/json")
			fmt.Fprint(w, `{"name":"tenant-api/db-a/20260406","commit":{"id":"abc123"}}`)
			return
		}
		w.WriteHeader(http.StatusNotFound)
	}))
	defer srv.Close()

	c, _ := NewClient("token", "group/project", "main")
	c.SetBaseURL(srv.URL)

	if err := c.CreateBranch("tenant-api/db-a/20260406"); err != nil {
		t.Fatalf("CreateBranch() error: %v", err)
	}
}

func TestCreatePR(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.Contains(r.URL.Path, "/merge_requests") && r.Method == "POST" {
			resp := map[string]interface{}{
				"iid":           42,
				"web_url":       "https://gitlab.com/group/project/-/merge_requests/42",
				"state":         "opened",
				"title":         "[tenant-api] Update db-a-prod",
				"source_branch": "tenant-api/db-a-prod/20260406",
				"created_at":    "2026-04-06T14:00:00Z",
			}
			w.Header().Set("Content-Type", "application/json")
			json.NewEncoder(w).Encode(resp)
			return
		}
		w.WriteHeader(http.StatusNotFound)
	}))
	defer srv.Close()

	c, _ := NewClient("token", "group/project", "main")
	c.SetBaseURL(srv.URL)

	mr, err := c.CreatePR("title", "body", "test-branch", []string{"auto"})
	if err != nil {
		t.Fatalf("CreatePR() error: %v", err)
	}
	if mr.Number != 42 {
		t.Errorf("expected MR !42, got !%d", mr.Number)
	}
	if mr.WebURL != "https://gitlab.com/group/project/-/merge_requests/42" {
		t.Errorf("unexpected URL: %s", mr.WebURL)
	}
	if mr.State != "open" {
		t.Errorf("expected normalized state 'open', got %q", mr.State)
	}
}

func TestListOpenPRs(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mrs := []map[string]interface{}{
			{
				"iid":           1,
				"web_url":       "https://gitlab.com/g/p/-/merge_requests/1",
				"state":         "opened",
				"title":         "[tenant-api] Update db-a",
				"source_branch": "tenant-api/db-a/20260406",
				"created_at":    "2026-04-06T10:00:00Z",
			},
			{
				"iid":           2,
				"web_url":       "https://gitlab.com/g/p/-/merge_requests/2",
				"state":         "opened",
				"title":         "Manual MR",
				"source_branch": "feature/manual-change",
				"created_at":    "2026-04-06T11:00:00Z",
			},
			{
				"iid":           3,
				"web_url":       "https://gitlab.com/g/p/-/merge_requests/3",
				"state":         "opened",
				"title":         "[tenant-api] Batch",
				"source_branch": "tenant-api/batch/20260406",
				"created_at":    "2026-04-06T12:00:00Z",
			},
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(mrs)
	}))
	defer srv.Close()

	c, _ := NewClient("token", "group/project", "main")
	c.SetBaseURL(srv.URL)

	mrs, err := c.ListOpenPRs()
	if err != nil {
		t.Fatalf("ListOpenPRs() error: %v", err)
	}

	// Should only include tenant-api/* MRs
	if len(mrs) != 2 {
		t.Fatalf("expected 2 tenant-api MRs, got %d", len(mrs))
	}

	if mrs[0].TenantID != "db-a" {
		t.Errorf("expected tenant_id 'db-a', got %q", mrs[0].TenantID)
	}
	if mrs[1].TenantID != "batch" {
		t.Errorf("expected tenant_id 'batch', got %q", mrs[1].TenantID)
	}
}

func TestListOpenPRs_APIError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		fmt.Fprint(w, `{"message":"500 Internal Server Error"}`)
	}))
	defer srv.Close()

	c, _ := NewClient("token", "group/project", "main")
	c.SetBaseURL(srv.URL)

	_, err := c.ListOpenPRs()
	if err == nil {
		t.Error("expected error from API failure")
	}
}

func TestProjectAPIEncoding(t *testing.T) {
	c, _ := NewClient("token", "group/subgroup/project", "main")
	encoded := c.projectAPI()
	// url.PathEscape encodes / as %2F
	if !strings.Contains(encoded, "%2F") {
		t.Errorf("expected URL-encoded path, got %q", encoded)
	}
}

func TestDeleteBranch(t *testing.T) {
	var deletedRawURL string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == "DELETE" && strings.Contains(r.URL.RawPath, "/repository/branches/") {
			deletedRawURL = r.URL.RawPath
			w.WriteHeader(http.StatusNoContent)
			return
		}
		// Fallback: check decoded path (some Go HTTP server versions)
		if r.Method == "DELETE" && strings.Contains(r.URL.Path, "/repository/branches/") {
			deletedRawURL = r.RequestURI
			w.WriteHeader(http.StatusNoContent)
			return
		}
		w.WriteHeader(http.StatusNotFound)
	}))
	defer srv.Close()

	c, _ := NewClient("token", "group/project", "main")
	c.SetBaseURL(srv.URL)

	if err := c.DeleteBranch("tenant-api/db-a/20260406"); err != nil {
		t.Fatalf("DeleteBranch() error: %v", err)
	}

	// Verify the branch name was URL-encoded in the request URI
	if !strings.Contains(deletedRawURL, "tenant-api%2Fdb-a%2F20260406") {
		t.Errorf("expected URL-encoded branch in request, got %q", deletedRawURL)
	}
}

func TestListOpenPRs_StateNormalization(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mrs := []map[string]interface{}{
			{
				"iid":           1,
				"web_url":       "https://gl/1",
				"state":         "opened", // GitLab-specific state
				"title":         "MR1",
				"source_branch": "tenant-api/db-a/20260406",
			},
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(mrs)
	}))
	defer srv.Close()

	c, _ := NewClient("token", "group/project", "main")
	c.SetBaseURL(srv.URL)

	mrs, err := c.ListOpenPRs()
	if err != nil {
		t.Fatalf("ListOpenPRs() error: %v", err)
	}
	if len(mrs) != 1 {
		t.Fatalf("expected 1 MR, got %d", len(mrs))
	}
	// State "opened" should be normalized to "open"
	if mrs[0].State != "open" {
		t.Errorf("expected normalized state 'open', got %q", mrs[0].State)
	}
}

func TestNormalizeState(t *testing.T) {
	tests := []struct {
		input, want string
	}{
		{"opened", "open"},
		{"open", "open"},
		{"closed", "closed"},
		{"merged", "merged"},
		{"", ""},
	}
	for _, tt := range tests {
		if got := normalizeState(tt.input); got != tt.want {
			t.Errorf("normalizeState(%q) = %q, want %q", tt.input, got, tt.want)
		}
	}
}
