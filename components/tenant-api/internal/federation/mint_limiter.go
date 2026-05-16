package federation

import (
	"sync"
	"time"
)

// Per-tenant token-mint rate limit. This is not a fairness quota — it
// shields the backing store from a runaway client (e.g. a mis-written
// CronJob in an issuance loop): with the ConfigMap store every
// successful mint rewrites the store object into etcd (ADR-020
// Posture B; Gemini round-4 review). maxTokensPerTenant caps the burst
// size; this caps the sustained rate.
const (
	mintWindow        = time.Minute
	maxMintsPerWindow = 5
)

// mintLimiter is a per-tenant sliding-window rate limiter for token
// issuance. It is safe for concurrent use.
type mintLimiter struct {
	mu   sync.Mutex
	hits map[string][]time.Time
}

func newMintLimiter() *mintLimiter {
	return &mintLimiter{hits: make(map[string][]time.Time)}
}

// allow records a mint attempt for tenantID at now and reports whether
// it is within the rate limit. A denied attempt is not recorded, so a
// client hammering the endpoint cannot push its own window further out.
func (l *mintLimiter) allow(tenantID string, now time.Time) bool {
	l.mu.Lock()
	defer l.mu.Unlock()

	cutoff := now.Add(-mintWindow)
	kept := l.hits[tenantID][:0]
	for _, t := range l.hits[tenantID] {
		if t.After(cutoff) {
			kept = append(kept, t)
		}
	}
	if len(kept) >= maxMintsPerWindow {
		l.hits[tenantID] = kept
		return false
	}
	l.hits[tenantID] = append(kept, now)
	return true
}
