package github

import (
	"log"
	"sync"
	"time"

	"github.com/vencil/tenant-api/internal/platform"
)

// Compile-time interface assertion: *Tracker implements platform.Tracker.
var _ platform.Tracker = (*Tracker)(nil)

// Tracker maintains an in-memory cache of pending PRs, periodically syncing
// with the GitHub API. This allows the tenant-manager UI to display pending PR
// status without hitting GitHub on every request.
//
// Design (ADR-011): eventual consistency — the cache may be up to syncInterval stale.
// Implements platform.Tracker.
type Tracker struct {
	client       *Client
	mu           sync.RWMutex
	pendingPRs   []platform.PRInfo
	byTenant     map[string]platform.PRInfo // tenantID → most recent pending PR
	syncInterval time.Duration
	lastSync     time.Time
}

// NewTracker creates a PR tracker with the given sync interval.
func NewTracker(client *Client, syncInterval time.Duration) *Tracker {
	if syncInterval < 10*time.Second {
		syncInterval = 30 * time.Second
	}
	return &Tracker{
		client:       client,
		byTenant:     make(map[string]platform.PRInfo),
		syncInterval: syncInterval,
	}
}

// WatchLoop periodically syncs the pending PR list from GitHub.
// Call this in a goroutine. Stops when stopCh is closed.
func (t *Tracker) WatchLoop(stopCh <-chan struct{}) {
	// Initial sync
	t.sync()

	ticker := time.NewTicker(t.syncInterval)
	defer ticker.Stop()

	for {
		select {
		case <-ticker.C:
			t.sync()
		case <-stopCh:
			return
		}
	}
}

// sync fetches the current open PRs from GitHub and updates the cache.
func (t *Tracker) sync() {
	prs, err := t.client.ListOpenPRs()
	if err != nil {
		log.Printf("WARN: github tracker: sync failed: %v", err)
		return
	}

	byTenant := make(map[string]platform.PRInfo, len(prs))
	for _, pr := range prs {
		if pr.TenantID != "" {
			// Keep the most recent PR per tenant (by PR number, higher = newer)
			if existing, ok := byTenant[pr.TenantID]; !ok || pr.Number > existing.Number {
				byTenant[pr.TenantID] = pr
			}
		}
	}

	t.mu.Lock()
	t.pendingPRs = prs
	t.byTenant = byTenant
	t.lastSync = time.Now()
	t.mu.Unlock()

	log.Printf("github tracker: synced %d pending PRs", len(prs))
}

// PendingPRs returns all tracked pending PRs.
func (t *Tracker) PendingPRs() []platform.PRInfo {
	t.mu.RLock()
	defer t.mu.RUnlock()
	result := make([]platform.PRInfo, len(t.pendingPRs))
	copy(result, t.pendingPRs)
	return result
}

// PendingPRForTenant returns the pending PR for a specific tenant, if any.
func (t *Tracker) PendingPRForTenant(tenantID string) (platform.PRInfo, bool) {
	t.mu.RLock()
	defer t.mu.RUnlock()
	pr, ok := t.byTenant[tenantID]
	return pr, ok
}

// HasPendingPR checks if a tenant has an open PR pending review.
func (t *Tracker) HasPendingPR(tenantID string) bool {
	t.mu.RLock()
	defer t.mu.RUnlock()
	_, ok := t.byTenant[tenantID]
	return ok
}

// RegisterPR adds a newly created PR to the tracker immediately (before next sync).
// If the tenant already has a pending PR, it replaces the existing entry.
func (t *Tracker) RegisterPR(pr platform.PRInfo) {
	t.mu.Lock()
	defer t.mu.Unlock()
	// Replace existing entry for same tenant to avoid duplicates
	if pr.TenantID != "" {
		replaced := false
		for i, existing := range t.pendingPRs {
			if existing.TenantID == pr.TenantID {
				t.pendingPRs[i] = pr
				replaced = true
				break
			}
		}
		if !replaced {
			t.pendingPRs = append(t.pendingPRs, pr)
		}
		t.byTenant[pr.TenantID] = pr
	} else {
		t.pendingPRs = append(t.pendingPRs, pr)
	}
}

// LastSyncTime returns when the tracker last synced with GitHub.
func (t *Tracker) LastSyncTime() time.Time {
	t.mu.RLock()
	defer t.mu.RUnlock()
	return t.lastSync
}
