package gitlab

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/platform"
)

func newTestTracker(t *testing.T, mrs []platform.PRInfo) (*Tracker, *httptest.Server) {
	t.Helper()
	apiMRs := make([]map[string]interface{}, len(mrs))
	for i, mr := range mrs {
		apiMRs[i] = map[string]interface{}{
			"iid":           mr.Number,
			"web_url":       mr.WebURL,
			"state":         mr.State,
			"title":         mr.Title,
			"source_branch": mr.HeadRef,
			"created_at":    mr.CreatedAt,
		}
	}

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(apiMRs)
	}))

	c, _ := NewClient("token", "group/project", "main")
	c.SetBaseURL(srv.URL)
	tracker := NewTracker(c, 1*time.Hour)

	return tracker, srv
}

func TestTrackerSync(t *testing.T) {
	tracker, srv := newTestTracker(t, []platform.PRInfo{
		{Number: 1, WebURL: "https://gl/1", State: "opened", Title: "MR1", HeadRef: "tenant-api/db-a/20260406", CreatedAt: "2026-04-06T10:00:00Z"},
		{Number: 2, WebURL: "https://gl/2", State: "opened", Title: "MR2", HeadRef: "tenant-api/db-b/20260406", CreatedAt: "2026-04-06T11:00:00Z"},
	})
	defer srv.Close()

	tracker.Sync()

	prs := tracker.PendingPRs()
	if len(prs) != 2 {
		t.Fatalf("expected 2 MRs, got %d", len(prs))
	}

	if !tracker.HasPendingPR("db-a") {
		t.Error("expected db-a to have pending MR")
	}
	if !tracker.HasPendingPR("db-b") {
		t.Error("expected db-b to have pending MR")
	}
	if tracker.HasPendingPR("db-c") {
		t.Error("expected db-c to NOT have pending MR")
	}
}

func TestTrackerPendingPRForTenant(t *testing.T) {
	tracker, srv := newTestTracker(t, []platform.PRInfo{
		{Number: 5, WebURL: "https://gl/5", State: "opened", Title: "MR5", HeadRef: "tenant-api/db-a/20260406", CreatedAt: "2026-04-06T10:00:00Z"},
	})
	defer srv.Close()

	tracker.Sync()

	mr, ok := tracker.PendingPRForTenant("db-a")
	if !ok {
		t.Fatal("expected MR for db-a")
	}
	if mr.Number != 5 {
		t.Errorf("expected MR !5, got !%d", mr.Number)
	}

	_, ok = tracker.PendingPRForTenant("nonexistent")
	if ok {
		t.Error("expected no MR for nonexistent tenant")
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
		WebURL:   "https://gl/10",
		State:    "opened",
		TenantID: "test-tenant",
		HeadRef:  "tenant-api/test-tenant/20260406",
	})

	if !tracker.HasPendingPR("test-tenant") {
		t.Error("expected test-tenant to have pending MR after register")
	}

	prs := tracker.PendingPRs()
	if len(prs) != 1 {
		t.Fatalf("expected 1 MR after register, got %d", len(prs))
	}
}

func TestTrackerRegisterPR_ReplaceSameTenant(t *testing.T) {
	tracker, srv := newTestTracker(t, []platform.PRInfo{})
	defer srv.Close()

	tracker.RegisterPR(platform.PRInfo{
		Number: 1, WebURL: "https://gl/1", State: "opened", TenantID: "db-a",
	})
	tracker.RegisterPR(platform.PRInfo{
		Number: 5, WebURL: "https://gl/5", State: "opened", TenantID: "db-a",
	})

	prs := tracker.PendingPRs()
	if len(prs) != 1 {
		t.Fatalf("expected 1 MR (replaced), got %d", len(prs))
	}
	if prs[0].Number != 5 {
		t.Errorf("expected replaced MR !5, got !%d", prs[0].Number)
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

func TestTrackerMostRecentMRPerTenant(t *testing.T) {
	tracker, srv := newTestTracker(t, []platform.PRInfo{
		{Number: 1, WebURL: "https://gl/1", State: "opened", Title: "Old", HeadRef: "tenant-api/db-a/20260401", CreatedAt: "2026-04-01T10:00:00Z"},
		{Number: 5, WebURL: "https://gl/5", State: "opened", Title: "New", HeadRef: "tenant-api/db-a/20260406", CreatedAt: "2026-04-06T10:00:00Z"},
	})
	defer srv.Close()

	tracker.Sync()

	mr, ok := tracker.PendingPRForTenant("db-a")
	if !ok {
		t.Fatal("expected MR for db-a")
	}
	if mr.Number != 5 {
		t.Errorf("expected most recent MR !5, got !%d", mr.Number)
	}
}

func TestMinSyncInterval(t *testing.T) {
	c, _ := NewClient("token", "group/project", "main")
	tracker := NewTracker(c, 1*time.Second) // below minimum
	if got := tracker.SyncInterval(); got < 10*time.Second {
		t.Errorf("expected sync interval >= 10s, got %v", got)
	}
}
