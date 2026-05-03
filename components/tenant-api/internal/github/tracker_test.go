package github

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/platform"
)

func newTestTracker(t *testing.T, prs []platform.PRInfo) (*Tracker, *httptest.Server) {
	t.Helper()
	apiPRs := make([]map[string]interface{}, len(prs))
	for i, pr := range prs {
		apiPRs[i] = map[string]interface{}{
			"number":     pr.Number,
			"html_url":   pr.WebURL,
			"state":      pr.State,
			"title":      pr.Title,
			"head":       map[string]string{"ref": pr.HeadRef},
			"created_at": pr.CreatedAt,
		}
	}

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(apiPRs)
	}))

	c, _ := NewClient("token", "owner/repo", "main")
	c.SetBaseURL(srv.URL)
	tracker := NewTracker(c, 1*time.Hour)

	return tracker, srv
}

func TestTrackerSync(t *testing.T) {
	tracker, srv := newTestTracker(t, []platform.PRInfo{
		{Number: 1, WebURL: "https://gh/1", State: "open", Title: "PR1", HeadRef: "tenant-api/db-a/20260406", CreatedAt: "2026-04-06T10:00:00Z"},
		{Number: 2, WebURL: "https://gh/2", State: "open", Title: "PR2", HeadRef: "tenant-api/db-b/20260406", CreatedAt: "2026-04-06T11:00:00Z"},
	})
	defer srv.Close()

	tracker.Sync()

	prs := tracker.PendingPRs()
	if len(prs) != 2 {
		t.Fatalf("expected 2 PRs, got %d", len(prs))
	}

	if !tracker.HasPendingPR("db-a") {
		t.Error("expected db-a to have pending PR")
	}
	if !tracker.HasPendingPR("db-b") {
		t.Error("expected db-b to have pending PR")
	}
	if tracker.HasPendingPR("db-c") {
		t.Error("expected db-c to NOT have pending PR")
	}
}

func TestTrackerPendingPRForTenant(t *testing.T) {
	tracker, srv := newTestTracker(t, []platform.PRInfo{
		{Number: 5, WebURL: "https://gh/5", State: "open", Title: "PR5", HeadRef: "tenant-api/db-a/20260406", CreatedAt: "2026-04-06T10:00:00Z"},
	})
	defer srv.Close()

	tracker.Sync()

	pr, ok := tracker.PendingPRForTenant("db-a")
	if !ok {
		t.Fatal("expected PR for db-a")
	}
	if pr.Number != 5 {
		t.Errorf("expected PR #5, got #%d", pr.Number)
	}

	_, ok = tracker.PendingPRForTenant("nonexistent")
	if ok {
		t.Error("expected no PR for nonexistent tenant")
	}
}

func TestTrackerRegisterPR(t *testing.T) {
	tracker, srv := newTestTracker(t, []platform.PRInfo{})
	defer srv.Close()

	tracker.Sync()
	if len(tracker.PendingPRs()) != 0 {
		t.Fatal("expected empty tracker")
	}

	tracker.RegisterPR(platform.PRInfo{
		Number:   10,
		WebURL:   "https://gh/10",
		State:    "open",
		TenantID: "test-tenant",
		HeadRef:  "tenant-api/test-tenant/20260406",
	})

	if !tracker.HasPendingPR("test-tenant") {
		t.Error("expected test-tenant to have pending PR after register")
	}

	prs := tracker.PendingPRs()
	if len(prs) != 1 {
		t.Fatalf("expected 1 PR after register, got %d", len(prs))
	}
}

func TestTrackerLastSyncTime(t *testing.T) {
	tracker, srv := newTestTracker(t, []platform.PRInfo{})
	defer srv.Close()

	if !tracker.LastSyncTime().IsZero() {
		t.Error("expected zero time before first sync")
	}

	tracker.Sync()

	if tracker.LastSyncTime().IsZero() {
		t.Error("expected non-zero time after sync")
	}
}

func TestTrackerMostRecentPRPerTenant(t *testing.T) {
	// When same tenant has multiple PRs, tracker keeps the highest number (newest)
	tracker, srv := newTestTracker(t, []platform.PRInfo{
		{Number: 1, WebURL: "https://gh/1", State: "open", Title: "Old", HeadRef: "tenant-api/db-a/20260401", CreatedAt: "2026-04-01T10:00:00Z"},
		{Number: 5, WebURL: "https://gh/5", State: "open", Title: "New", HeadRef: "tenant-api/db-a/20260406", CreatedAt: "2026-04-06T10:00:00Z"},
	})
	defer srv.Close()

	tracker.Sync()

	pr, ok := tracker.PendingPRForTenant("db-a")
	if !ok {
		t.Fatal("expected PR for db-a")
	}
	if pr.Number != 5 {
		t.Errorf("expected most recent PR #5, got #%d", pr.Number)
	}
}

func TestMinSyncInterval(t *testing.T) {
	c, _ := NewClient("token", "owner/repo", "main")
	tracker := NewTracker(c, 1*time.Second) // below minimum
	if got := tracker.SyncInterval(); got < 10*time.Second {
		t.Errorf("expected sync interval >= 10s, got %v", got)
	}
}
