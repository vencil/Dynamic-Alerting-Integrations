package platform

import (
	"errors"
	"log/slog"
	"net/http"
	"sync"
	"time"

	"github.com/sony/gobreaker/v2"
)

// ErrCircuitOpen is returned by a forge client call when the per-provider
// circuit breaker is open (or half-open and already saturated). Handlers map
// it to HTTP 503 — same bucket as a 5xx APIError — so a degraded on-prem forge
// produces a fast, explicit "service unavailable" instead of a 30s hang per
// request (#632 / #645). errors.Is reaches it through the fmt.Errorf wrapping
// the client `do()` paths apply.
var ErrCircuitOpen = errors.New("forge circuit breaker open")

// circuit-breaker tuning. Deliberately conservative — this is defense-in-depth
// (the gitExec write path already has its own timeout, #630), so the breaker
// should only trip on a genuinely sick forge, not on a transient blip.
const (
	// cbConsecutiveFailures is how many back-to-back degradation failures
	// (5xx / network / timeout) trip the breaker open. 5 absorbs a brief
	// blip while still reacting within seconds to a sustained outage.
	cbConsecutiveFailures = 5
	// cbOpenTimeout is how long the breaker stays open before allowing a
	// single half-open probe. Matches the http.Client per-request timeout
	// (30s) ×2 so a probe doesn't fire while the previous slow request that
	// tripped the breaker could still be unwinding.
	cbOpenTimeout = 60 * time.Second
)

// httpResult bundles the two non-error return values of a forge `do()` call so
// they can pass through gobreaker's single-value generic Execute.
type httpResult struct {
	body   []byte
	header http.Header
}

// CircuitBreaker wraps a forge client's HTTP chokepoint with a gobreaker
// circuit breaker. One instance per *Client (i.e. one per process, since
// tenant-api runs a single forge provider per deployment). The writer and the
// PollingTracker share the same client and therefore the same breaker, so a
// forge outage detected by either is reflected for both.
//
// Why gobreaker over a hand-rolled breaker: the half-open probe + generation-
// counter state machine is exactly the kind of concurrency-sensitive code
// that's easy to get subtly wrong; gobreaker is the de-facto-standard,
// zero-transitive-dep implementation. (The repo's sibling rate limiter is
// hand-rolled, but it's a trivial sliding window — the breaker is not.)
type CircuitBreaker struct {
	cb       *gobreaker.CircuitBreaker[httpResult]
	provider string

	// TRK-319 rate-limit gate. gobreaker's open window is a fixed cbOpenTimeout
	// (60s); a forge secondary rate limit can advise a LONGER Retry-After. Once
	// the breaker has opened on rate-limit failures, notBefore extends the
	// suppression to that Retry-After so the 60s half-open probe doesn't keep
	// punching a still-active limit ("醒來被揍一次"). Guarded by mu; the writer
	// and the PollingTracker share one breaker, so this is read/written
	// concurrently. now is a clock seam (nil → time.Now) for deterministic tests.
	mu        sync.Mutex
	notBefore time.Time
	now       func() time.Time
}

// NewCircuitBreaker builds a breaker for the named provider and registers it
// for the /metrics snapshot. `provider` is a lowercase tag ("github" /
// "gitlab") — it MUST match the string the PollingTracker uses (see
// NewPollingTracker callers) so the tenant_api_forge_circuit_state and
// tenant_api_forge_pr_conflicts metrics share an identical `provider` label
// value and can be joined/filtered together. Trips after
// cbConsecutiveFailures degradation failures; recovers via a single half-open
// probe after cbOpenTimeout.
func NewCircuitBreaker(provider string) *CircuitBreaker {
	bcb := &CircuitBreaker{provider: provider, now: time.Now}
	bcb.cb = gobreaker.NewCircuitBreaker[httpResult](gobreaker.Settings{
		Name:        provider,
		MaxRequests: 1, // half-open: a single probe decides recover-or-reopen
		Timeout:     cbOpenTimeout,
		ReadyToTrip: func(counts gobreaker.Counts) bool {
			return counts.ConsecutiveFailures >= cbConsecutiveFailures
		},
		// IsSuccessful decides whether a returned error counts as a breaker
		// FAILURE. Only genuine forge degradation (5xx / network / timeout)
		// should trip the breaker. A 403/404/409/422 is a deterministic
		// client-side outcome (bad token scope, missing resource, pending-PR
		// conflict, validation) — counting those as failures would let one
		// misconfigured tenant open the breaker for everyone, so they are
		// treated as "successful" calls from the breaker's perspective even
		// though the error still propagates to the caller unchanged.
		IsSuccessful: func(err error) bool {
			return !isForgeDegradation(err)
		},
		OnStateChange: func(name string, from, to gobreaker.State) {
			slog.Warn("forge circuit breaker state change",
				"provider", name, "from", from.String(), "to", to.String())
		},
	})
	registerBreaker(bcb)
	return bcb
}

// Execute runs fn through the breaker. When the breaker is open it short-
// circuits with ErrCircuitOpen without invoking fn at all (the fast-fail).
// Otherwise it runs fn, records success/failure per IsSuccessful, and returns
// fn's result and error unchanged — so 4xx errors propagate normally while not
// tripping the breaker.
func (c *CircuitBreaker) Execute(fn func() ([]byte, http.Header, error)) ([]byte, http.Header, error) {
	now := c.clock()

	// TRK-319 rate-limit gate: while a forge-advised Retry-After window is still
	// open (set when the breaker opened on rate-limit failures), suppress the call
	// WITHOUT dialing — including the breaker's half-open probe, which would
	// otherwise re-hit a still-active secondary rate limit every cbOpenTimeout.
	if c.rateLimitGateOpen(now) {
		return nil, nil, ErrCircuitOpen
	}

	res, err := c.cb.Execute(func() (httpResult, error) {
		body, header, e := fn()
		return httpResult{body: body, header: header}, e
	})
	if err != nil {
		// gobreaker's own short-circuit errors → our provider-neutral sentinel.
		if errors.Is(err, gobreaker.ErrOpenState) || errors.Is(err, gobreaker.ErrTooManyRequests) {
			return nil, nil, ErrCircuitOpen
		}
		// If THIS rate-limit failure is the one that (just) opened the breaker,
		// arm/extend the gate to the forge-advised Retry-After. Tying it to the
		// open state preserves the "trips only after cbConsecutiveFailures"
		// semantics — calls 1..N still execute and accumulate failures; the gate
		// only extends the open window beyond gobreaker's fixed 60s.
		var apiErr *APIError
		if errors.As(err, &apiErr) && apiErr.RateLimited && apiErr.RetryAfter > 0 &&
			c.cb.State() == gobreaker.StateOpen {
			c.armRateLimitGate(now.Add(apiErr.RetryAfter))
		}
		// A real fn error (4xx/5xx/network) — propagate body+header+err as-is.
		return res.body, res.header, err
	}
	return res.body, res.header, nil
}

// clock returns the breaker's time source (time.Now in prod; injectable in tests).
func (c *CircuitBreaker) clock() time.Time {
	if c.now != nil {
		return c.now()
	}
	return time.Now()
}

// rateLimitGateOpen reports whether the forge-advised Retry-After window is still
// in effect (TRK-319).
func (c *CircuitBreaker) rateLimitGateOpen(now time.Time) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return !c.notBefore.IsZero() && now.Before(c.notBefore)
}

// armRateLimitGate extends the rate-limit suppression window to `until` (never
// shortens it), logging the back-off so an SRE sees the active Retry-After.
func (c *CircuitBreaker) armRateLimitGate(until time.Time) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if until.After(c.notBefore) {
		c.notBefore = until
		slog.Warn("forge rate limit — suppressing calls until Retry-After elapses (TRK-319)",
			"provider", c.provider, "retry_after_until", until.UTC().Format(time.RFC3339))
	}
}

// State returns the breaker's current state string ("closed" / "half-open" /
// "open") for the /metrics snapshot.
func (c *CircuitBreaker) State() string {
	return c.cb.State().String()
}

// isForgeDegradation reports whether err represents forge-side degradation that
// should count toward tripping the breaker. True for 5xx API errors, network
// errors, and timeouts; false for nil and for 4xx API errors (deterministic
// client-side outcomes that must NOT trip the breaker).
func isForgeDegradation(err error) bool {
	if err == nil {
		return false
	}
	var apiErr *APIError
	if errors.As(err, &apiErr) {
		// 5xx = forge degradation. A rate-limit / abuse rejection (TRK-319) is
		// ALSO degradation even though it arrives as a 4xx (GitHub secondary
		// rate limit = 403, GitLab = 429): during the rate-limit window the
		// write plane is genuinely unavailable, so the breaker must protect it
		// instead of sailing through as a "successful" 4xx. Driven by the
		// DETECTED RateLimited flag, not the bare status code, so a permission
		// 403 / validation 422 still counts as a deterministic client outcome.
		return apiErr.StatusCode >= 500 || apiErr.RateLimited
	}
	// Not an APIError → transport-layer failure (connection refused, DNS,
	// TLS, context deadline / http.Client timeout). All forge degradation.
	return true
}

// ─────────────────────────────────────────────────────────────────────
// Metrics snapshot registry. tenant-api exposes /metrics as hand-rolled
// Prometheus text (no client_golang), so the breaker registers itself here
// and the handler reads CircuitSnapshot() — mirroring orphan.OrphanCounts()
// and the rate-limiter metric plumbing (handler.activeLimiter).
//
// Deliberate single-instance design (matches handler.activeLimiter): these
// registries are package-level globals keyed by provider, written at client
// construction and read by the free-function MetricsHandler. tenant-api runs
// exactly ONE forge provider per process, so each registry holds one entry and
// there is no contention in production. Like activeLimiter, the "last writer
// per key wins" property means a test that constructs two clients/trackers for
// the SAME provider name under t.Parallel() can observe the other's snapshot —
// tests that need isolation must assert on the instance directly (the breaker's
// own .State(), the tracker's conflict count) or use a unique provider tag, NOT
// the global snapshot. We keep the global plumbing rather than threading the
// breaker/tracker through Deps into the handler precisely to stay consistent
// with the two existing sibling metrics; the handler is a free function by
// design.
// ─────────────────────────────────────────────────────────────────────

var (
	breakerRegistryMu sync.RWMutex
	breakerRegistry   = map[string]*CircuitBreaker{}
)

// registerBreaker records a breaker under its provider name for the metrics
// snapshot. Keyed by provider, so re-creating a client for the same provider
// (e.g. in tests) replaces rather than duplicates the entry.
func registerBreaker(b *CircuitBreaker) {
	breakerRegistryMu.Lock()
	defer breakerRegistryMu.Unlock()
	breakerRegistry[b.provider] = b
}

// CircuitSnapshot returns a provider→state map for the /metrics handler.
// Empty when no forge client is configured (direct write mode).
func CircuitSnapshot() map[string]string {
	breakerRegistryMu.RLock()
	defer breakerRegistryMu.RUnlock()
	out := make(map[string]string, len(breakerRegistry))
	for provider, b := range breakerRegistry {
		out[provider] = b.State()
	}
	return out
}

// ─────────────────────────────────────────────────────────────────────
// PR/MR merge-conflict count snapshot (#646). The PollingTracker updates this
// after each sync; the /metrics handler reads ConflictSnapshot(). Separate
// registry from the breaker so direct write mode (no breaker) can still report
// 0, and so a provider with no conflicts still emits its 0 line once it has
// synced at least once.
// ─────────────────────────────────────────────────────────────────────

var (
	conflictCountMu sync.RWMutex
	conflictCount   = map[string]int{}
)

// setConflictCount records the number of tracked PRs/MRs in merge conflict for
// a provider, observed at the most recent tracker sync (#646).
func setConflictCount(provider string, n int) {
	conflictCountMu.Lock()
	defer conflictCountMu.Unlock()
	conflictCount[provider] = n
}

// ConflictSnapshot returns a provider→conflict-count map for the /metrics
// handler. Empty until the tracker has synced at least once (or in direct
// write mode, where there is no tracker).
func ConflictSnapshot() map[string]int {
	conflictCountMu.RLock()
	defer conflictCountMu.RUnlock()
	out := make(map[string]int, len(conflictCount))
	for provider, n := range conflictCount {
		out[provider] = n
	}
	return out
}
