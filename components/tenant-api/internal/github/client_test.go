package github

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
			_, err := NewClient("token", tt.repo, "main")
			if (err != nil) != tt.wantErr {
				t.Errorf("NewClient() error = %v, wantErr %v", err, tt.wantErr)
			}
		})
	}
}

func TestNewClientDefaultBranch(t *testing.T) {
	c, err := NewClient("tok", "o/r", "")
	if err != nil {
		t.Fatal(err)
	}
	if c.baseBranch != "main" {
		t.Errorf("expected default branch 'main', got %q", c.baseBranch)
	}
}

func TestProviderName(t *testing.T) {
	c, _ := NewClient("tok", "o/r", "main")
	if c.ProviderName() != "GitHub" {
		t.Errorf("expected 'GitHub', got %q", c.ProviderName())
	}
}

func TestValidateToken(t *testing.T) {
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
