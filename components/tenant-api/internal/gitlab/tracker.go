package gitlab

import (
	"log"
	"sync"
	"time"

	"github.com/vencil/tenant-api/internal/platform"
)

// Compile-time interface assertion: *Tracker implements platform.Tracker.
var _ platform.Tracker = (*Tracker)(nil)

// Tracker maintains an in-memory cache of pending MRs, periodically syncing
// with the GitLab API. This allows the tenant-manager UI to display pending MR
// status without hitting GitLab on every request.
//
// Design (ADR-011): eventual consistency — the cache may be up to syncInterval stale.
// Implements platform.Tracker.
type Tracker struct {
	client       *Client
	mu           sync.RWMutex
	pendingMRs   []platform.PRInfo
	byTenant     map[string]platform.PRInfo // tenantID → most recent pending MR
	syncInterval time.Duration
	lastSync     time.Time
}

// NewTracker creates an MR tracker with the given sync interval.
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

// WatchLoop periodically syncs the pending MR list from GitLab.
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

// sync fetches the current open MRs from GitLab and updates the cache.
func (t *Tracker) sync() {
	mrs, err := t.client.ListOpenPRs()
	if err != nil {
		log.Printf("WARN: gitlab tracker: sync failed: %v", err)
		return
	}

	byTenant := make(map[string]platform.PRInfo, len(mrs))
	for _, mr := range mrs {
		if mr.TenantID != "" {
			// Keep the most recent MR per tenant (by IID number, higher = newer)
			if existing, ok := byTenant[mr.TenantID]; !ok || mr.Number > existing.Number {
				byTenant[mr.TenantID] = mr
			}
		}
	}

	t.mu.Lock()
	t.pendingMRs = mrs
	t.byTenant = byTenant
	t.lastSync = time.Now()
	t.mu.Unlock()

	log.Printf("gitlab tracker: synced %d pending MRs", len(mrs))
}

// PendingPRs returns all tracked pending MRs.
func (t *Tracker) PendingPRs() []platform.PRInfo {
	t.mu.RLock()
	defer t.mu.RUnlock()
	result := make([]platform.PRInfo, len(t.pendingMRs))
	copy(result, t.pendingMRs)
	return result
}

// PendingPRForTenant returns the pending MR for a specific tenant, if any.
func (t *Tracker) PendingPRForTenant(tenantID string) (platform.PRInfo, bool) {
	t.mu.RLock()
	defer t.mu.RUnlock()
	mr, ok := t.byTenant[tenantID]
	return mr, ok
}

// HasPendingPR checks if a tenant has an open MR pending review.
func (t *Tracker) HasPendingPR(tenantID string) bool {
	t.mu.RLock()
	defer t.mu.RUnlock()
	_, ok := t.byTenant[tenantID]
	return ok
}

// RegisterPR adds a newly created MR to the tracker immediately (before next sync).
// If the tenant already has a pending MR, it replaces the existing entry.
func (t *Tracker) RegisterPR(pr platform.PRInfo) {
	t.mu.Lock()
	defer t.mu.Unlock()
	// Replace existing entry for same tenant to avoid duplicates
	if pr.TenantID != "" {
		replaced := false
		for i, existing := range t.pendingMRs {
			if existing.TenantID == pr.TenantID {
				t.pendingMRs[i] = pr
				replaced = true
				break
			}
		}
		if !replaced {
			t.pendingMRs = append(t.pendingMRs, pr)
		}
		t.byTenant[pr.TenantID] = pr
	} else {
		t.pendingMRs = append(t.pendingMRs, pr)
	}
}

// LastSyncTime returns when the tracker last synced with GitLab.
func (t *Tracker) LastSyncTime() time.Time {
	t.mu.RLock()
	defer t.mu.RUnlock()
	return t.lastSync
}
