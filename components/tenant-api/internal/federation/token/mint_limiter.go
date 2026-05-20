package token

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
//
// Every call also prunes *all* tenants' windows and drops any tenant
// whose hits have fully aged out. Pruning only the calling tenant would
// leak a map key for every tenant that ever minted once and never came
// back; sweeping keeps l.hits bounded by the set of tenants with a mint
// in the last mintWindow.
func (l *mintLimiter) allow(tenantID string, now time.Time) bool {
	l.mu.Lock()
	defer l.mu.Unlock()

	cutoff := now.Add(-mintWindow)
	for tid, hits := range l.hits {
		kept := hits[:0]
		for _, t := range hits {
			if t.After(cutoff) {
				kept = append(kept, t)
			}
		}
		if len(kept) == 0 {
			delete(l.hits, tid)
		} else {
			l.hits[tid] = kept
		}
	}

	if len(l.hits[tenantID]) >= maxMintsPerWindow {
		return false
	}
	l.hits[tenantID] = append(l.hits[tenantID], now)
	return true
}
