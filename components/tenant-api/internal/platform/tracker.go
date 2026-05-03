package platform

import (
	"log/slog"
	"sync"
	"time"
)

// Lister fetches the current open PRs/MRs from a Git hosting platform.
// Returned PRInfo entries should already be filtered to those owned by
// tenant-api (provider-side branch-prefix filtering, etc.).
type Lister func() ([]PRInfo, error)

// Compile-time interface assertion: *PollingTracker implements Tracker.
var _ Tracker = (*PollingTracker)(nil)

// PollingTracker is the shared cache+poll engine for Git-platform
// pending-PR/MR tracking. github/Tracker and gitlab/Tracker are now
// thin aliases over this type — they only differ in (a) the Lister
// they pass at construction and (b) the provider-name string used in
// log lines.
//
// Design (ADR-011): eventual consistency — the cache may be up to
// `syncInterval` stale. Concurrent reads use sync.RWMutex; sync()
// acquires a write lock only at the moment of cache replacement.
//
// The polling cadence is floor-clamped at 10s to prevent operators
// accidentally hammering the platform API with sub-second intervals
// (which usually triggers rate limits and earns the deployment a
// platform-side IP block).
type PollingTracker struct {
	lister       Lister
	provider     string // "github" / "gitlab" — used in log lines only

	mu           sync.RWMutex
	pendingPRs   []PRInfo
	byTenant     map[string]PRInfo // tenantID → most recent pending PR/MR
	syncInterval time.Duration
	lastSync     time.Time
}

// minSyncInterval is the floor for sync cadence. Constructors clamp
// any below-minimum value up to a safe default.
const minSyncInterval = 10 * time.Second

// defaultSyncInterval is what gets substituted when the requested
// interval is below the floor.
const defaultSyncInterval = 30 * time.Second

// NewPollingTracker constructs a tracker that calls `lister` on each
// poll. `provider` is just a tag for log lines ("github" / "gitlab").
// `syncInterval` below 10s is clamped to 30s — operators shouldn't
// be polling that aggressively, see PollingTracker doc.
func NewPollingTracker(lister Lister, provider string, syncInterval time.Duration) *PollingTracker {
	if syncInterval < minSyncInterval {
		syncInterval = defaultSyncInterval
	}
	return &PollingTracker{
		lister:       lister,
		provider:     provider,
		byTenant:     make(map[string]PRInfo),
		syncInterval: syncInterval,
	}
}

// WatchLoop runs an initial sync, then re-syncs every syncInterval.
// Runs until stopCh is closed. Caller is responsible for goroutine
// lifecycle.
func (t *PollingTracker) WatchLoop(stopCh <-chan struct{}) {
	t.Sync()

	ticker := time.NewTicker(t.syncInterval)
	defer ticker.Stop()

	for {
		select {
		case <-ticker.C:
			t.Sync()
		case <-stopCh:
			return
		}
	}
}

// Sync fetches the current open PRs/MRs via the lister and replaces
// the cache. Errors are logged but not propagated — the tracker keeps
// serving stale data rather than going dark on transient API failures.
//
// Exported (vs. internal sync()) so tests can drive the poll
// deterministically without waiting on the WatchLoop ticker.
func (t *PollingTracker) Sync() {
	prs, err := t.lister()
	if err != nil {
		slog.Warn("tracker sync failed", "provider", t.provider, "error", err)
		return
	}

	byTenant := make(map[string]PRInfo, len(prs))
	for _, pr := range prs {
		if pr.TenantID == "" {
			continue
		}
		// Keep the most recent PR per tenant (by Number — higher = newer
		// because both GitHub PR numbers and GitLab IIDs are monotonic).
		if existing, ok := byTenant[pr.TenantID]; !ok || pr.Number > existing.Number {
			byTenant[pr.TenantID] = pr
		}
	}

	t.mu.Lock()
	t.pendingPRs = prs
	t.byTenant = byTenant
	t.lastSync = time.Now()
	t.mu.Unlock()

	slog.Info("tracker synced", "provider", t.provider, "pending", len(prs))
}

// PendingPRs returns a defensive copy of all tracked pending PRs/MRs.
func (t *PollingTracker) PendingPRs() []PRInfo {
	t.mu.RLock()
	defer t.mu.RUnlock()
	out := make([]PRInfo, len(t.pendingPRs))
	copy(out, t.pendingPRs)
	return out
}

// PendingPRForTenant returns the most-recent pending PR/MR for the
// given tenant, if any.
func (t *PollingTracker) PendingPRForTenant(tenantID string) (PRInfo, bool) {
	t.mu.RLock()
	defer t.mu.RUnlock()
	pr, ok := t.byTenant[tenantID]
	return pr, ok
}

// HasPendingPR reports whether a tenant has at least one open PR/MR.
func (t *PollingTracker) HasPendingPR(tenantID string) bool {
	t.mu.RLock()
	defer t.mu.RUnlock()
	_, ok := t.byTenant[tenantID]
	return ok
}

// RegisterPR adds a freshly-created PR/MR to the cache immediately,
// before the next poll. If the same tenant already has an entry, the
// existing entry is replaced (avoids transient duplicates between
// "client just created" and "next sync confirms it").
func (t *PollingTracker) RegisterPR(pr PRInfo) {
	t.mu.Lock()
	defer t.mu.Unlock()

	if pr.TenantID == "" {
		t.pendingPRs = append(t.pendingPRs, pr)
		return
	}

	for i, existing := range t.pendingPRs {
		if existing.TenantID == pr.TenantID {
			t.pendingPRs[i] = pr
			t.byTenant[pr.TenantID] = pr
			return
		}
	}
	t.pendingPRs = append(t.pendingPRs, pr)
	t.byTenant[pr.TenantID] = pr
}

// LastSyncTime returns the timestamp of the most recent Sync call.
// Zero value indicates Sync has never run.
func (t *PollingTracker) LastSyncTime() time.Time {
	t.mu.RLock()
	defer t.mu.RUnlock()
	return t.lastSync
}

// SyncInterval returns the (possibly floor-clamped) poll interval.
// Exported for test inspection.
func (t *PollingTracker) SyncInterval() time.Duration {
	return t.syncInterval
}
