package handler

import (
	"encoding/json"
	"fmt"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/vencil/tenant-api/internal/rbac"
)

// SearchTenants is the v2.8.0 Phase .c C-1 server-side search /
// filter / pagination endpoint for the tenant list.
//
// Why this exists:
//   - The current `GET /api/v1/tenants` (tenant_list.go) returns the
//     entire RBAC-visible set in one shot. At 500+ tenants the
//     downstream JSX `filtered.map(...)` in the Tenant Manager UI
//     (docs/interactive/tools/tenant-manager.jsx L1260) freezes the
//     DOM — both because of the JSON payload size and because the
//     React reconciler can't keep up with a 500+ row diff.
//   - C-2 (the JSX virtualization PR) consumes this endpoint to
//     populate a windowed list. The endpoint MUST keep p99 latency
//     under ~200ms at 1000-tenant scale to make the UX coherent.
//   - Server-side filtering also lets URL-shareable tenant-manager
//     bookmarks decode to the same view across sessions, which the
//     pure-client filter doesn't support.
//
// Built-in defenses (planning §C-1):
//   1. Hard pagination cap: page_size ≤ 500. Excess returns 400.
//   2. Rate limiting: relies on the chi-level RateLimit middleware
//      already wired in cmd/server/main.go (PR #135 / Track C).
//   3. RBAC filtering: same `filterTenantsByRBAC()` used by ListTenants.
//   4. p99 latency budget: in-memory snapshot cache w/ 30s TTL (see
//      tenantSnapshotCache below). At 1000 tenants the post-cache
//      filter+sort is sub-millisecond; cache miss is a one-time
//      disk scan amortised across the next 30s of requests.
//
// Honest scope (this PR / PR-1 of the C-1+C-2+C-2a bundle):
//   - In-memory cache w/ TTL is the v1 design. File-watcher-based
//     invalidation is a future improvement; the YAML files change
//     infrequently enough that 30s staleness is acceptable for the
//     UI search use case.
//   - free-text search is a simple case-insensitive substring match
//     across id / owner / domain / db_type / tags[]. Inverted index
//     / fuzzy matching is out of scope; revisit if a customer needs
//     it (rare in practice — operators search by exact id prefix).
//   - cursor-style pagination uses a numeric offset for v1. Opaque
//     cursor tokens (resilient to ordering changes between pages)
//     are a future enhancement.

// ── pagination + sort defaults / hard caps ─────────────────────────

const (
	// defaultPageSize is what the UI gets when it doesn't pass
	// page_size — chosen to fit one virtualized list "screen" plus
	// a small lookahead.
	defaultPageSize = 50

	// maxPageSize is the hard cap. Requests beyond this get 400.
	// The cap exists to prevent any one client from extracting the
	// full tenant set in a single call — that's a DoS vector both
	// for the response size and for the cache-miss disk scan if
	// repeated.
	maxPageSize = 500

	// snapshotTTL is how long a cached tenant snapshot is reused
	// before it's rebuilt from disk. 30s balances "fresh enough for
	// the UI" against "1 disk scan per N second window".
	snapshotTTL = 30 * time.Second
)

// validSortKeys lists the columns clients are allowed to sort on.
// Anything else returns 400. We keep this allowlist narrow because
// each new key requires us to test the secondary-key tiebreaker
// (otherwise pagination becomes non-deterministic at the boundary).
var validSortKeys = map[string]struct{}{
	"id":          {},
	"environment": {},
	"tier":        {},
	"domain":      {},
}

// ── public response shape ──────────────────────────────────────────

// SearchResponse is the body returned by GET /api/v1/tenants/search.
//
// Contract guarantees (pinned by tests):
//   - `items` is page-sized, sorted by `sort` query param (default
//     "id"), with `id` always serving as the secondary tiebreaker
//     so pagination is deterministic.
//   - `total_matched` reflects the count AFTER all filters but
//     BEFORE pagination — the UI uses it to render "Showing 1–50
//     of N" affordances.
//   - `next_offset` is null when the current page reaches the end
//     of the matched set; otherwise it's the offset for the next
//     page (so client can `?offset=<value>` directly).
//   - `page_size` echoes back the effective page size (after
//     defaulting to 50 when omitted, and after clamping rejection).
type SearchResponse struct {
	Items        []TenantSummary `json:"items"`
	TotalMatched int             `json:"total_matched"`
	PageSize     int             `json:"page_size"`
	NextOffset   *int            `json:"next_offset"`
}

// ── handler ────────────────────────────────────────────────────────

// SearchTenants handles GET /api/v1/tenants/search.
//
// Query params (all optional):
//
//	q            — free-text substring (case-insensitive) matching
//	               id / owner / domain / db_type / tags[]. Empty
//	               or absent → no text constraint.
//	environment  — exact match (e.g. "prod", "staging").
//	tier         — exact match (e.g. "tier1").
//	domain       — exact match.
//	db_type      — exact match.
//	tag          — single tag the tenant must have in its
//	               _metadata.tags list (case-sensitive — tags are
//	               canonical labels, not free text).
//	page_size    — int, default 50, max 500. Beyond max → 400.
//	offset       — int ≥ 0, default 0. Pagination.
//	sort         — id (default), environment, tier, or domain.
//	               Invalid → 400.
//
// Response: SearchResponse (see above) on 200, structured JSON error
// otherwise. RBAC filtering is applied identically to ListTenants —
// callers without metadata access see fewer rows.
func (d *Deps) SearchTenants() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		idpGroups := rbac.RequestGroups(r)

		params, err := parseSearchParams(r)
		if err != nil {
			writeJSONError(w, r,http.StatusBadRequest, err.Error())
			return
		}

		all, err := d.SearchCache.snapshot(d.ConfigDir)
		if err != nil {
			writeJSONError(w, r,http.StatusInternalServerError, err.Error())
			return
		}

		// Apply RBAC FIRST so the "total matched" count reflects what
		// THIS user can see, not what exists globally. UI consumers
		// expect total_matched to equal "rows the user could ever
		// reach by paging".
		visible := filterTenantsByRBAC(all, d.RBAC, idpGroups)
		matched := applyFilters(visible, params)
		sortTenants(matched, params.sort)

		page, nextOffset := paginate(matched, params.offset, params.pageSize)

		resp := SearchResponse{
			Items:        page,
			TotalMatched: len(matched),
			PageSize:     params.pageSize,
			NextOffset:   nextOffset,
		}

		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(resp)
	}
}

// ── query-param parsing ────────────────────────────────────────────

type searchParams struct {
	q           string
	environment string
	tier        string
	domain      string
	dbType      string
	tag         string
	pageSize    int
	offset      int
	sort        string
}

func parseSearchParams(r *http.Request) (*searchParams, error) {
	q := r.URL.Query()
	p := &searchParams{
		q:           strings.TrimSpace(q.Get("q")),
		environment: strings.TrimSpace(q.Get("environment")),
		tier:        strings.TrimSpace(q.Get("tier")),
		domain:      strings.TrimSpace(q.Get("domain")),
		dbType:      strings.TrimSpace(q.Get("db_type")),
		tag:         strings.TrimSpace(q.Get("tag")),
		pageSize:    defaultPageSize,
		offset:      0,
		sort:        "id",
	}

	if v := q.Get("page_size"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n <= 0 {
			return nil, fmt.Errorf("page_size must be a positive integer (got %q)", v)
		}
		if n > maxPageSize {
			return nil, fmt.Errorf("page_size exceeds max %d (got %d)", maxPageSize, n)
		}
		p.pageSize = n
	}

	if v := q.Get("offset"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n < 0 {
			return nil, fmt.Errorf("offset must be a non-negative integer (got %q)", v)
		}
		p.offset = n
	}

	if v := q.Get("sort"); v != "" {
		if _, ok := validSortKeys[v]; !ok {
			keys := make([]string, 0, len(validSortKeys))
			for k := range validSortKeys {
				keys = append(keys, k)
			}
			sort.Strings(keys)
			return nil, fmt.Errorf("sort must be one of %s (got %q)", strings.Join(keys, "|"), v)
		}
		p.sort = v
	}

	return p, nil
}

// ── filter pipeline ────────────────────────────────────────────────

func applyFilters(in []TenantSummary, p *searchParams) []TenantSummary {
	out := make([]TenantSummary, 0, len(in))
	qLower := strings.ToLower(p.q)
	for _, t := range in {
		if p.environment != "" && t.Environment != p.environment {
			continue
		}
		if p.tier != "" && t.Tier != p.tier {
			continue
		}
		if p.domain != "" && t.Domain != p.domain {
			continue
		}
		if p.dbType != "" && t.DBType != p.dbType {
			continue
		}
		if p.tag != "" && !containsString(t.Tags, p.tag) {
			continue
		}
		if qLower != "" && !matchesFreeText(t, qLower) {
			continue
		}
		out = append(out, t)
	}
	return out
}

// matchesFreeText returns true when any indexed text field on the
// tenant contains qLower as a case-insensitive substring. We
// intentionally restrict the search surface to a closed list of
// fields — full-tree-scan over the YAML body would be a DoS vector
// and would surface internal metadata (passwords, etc. if anyone
// ever stored them in tenant.yaml) to clients without write access.
func matchesFreeText(t TenantSummary, qLower string) bool {
	if strings.Contains(strings.ToLower(t.ID), qLower) {
		return true
	}
	if strings.Contains(strings.ToLower(t.Owner), qLower) {
		return true
	}
	if strings.Contains(strings.ToLower(t.Domain), qLower) {
		return true
	}
	if strings.Contains(strings.ToLower(t.DBType), qLower) {
		return true
	}
	for _, tag := range t.Tags {
		if strings.Contains(strings.ToLower(tag), qLower) {
			return true
		}
	}
	return false
}

func containsString(haystack []string, needle string) bool {
	for _, s := range haystack {
		if s == needle {
			return true
		}
	}
	return false
}

// ── deterministic sort ─────────────────────────────────────────────

// sortTenants orders `in` in place according to the requested key.
// `id` is ALWAYS the secondary tiebreaker so pagination is
// deterministic — without it, two tenants with the same primary
// value could appear in different positions across requests, and a
// naïve offset-based paginator would skip or duplicate them.
func sortTenants(in []TenantSummary, key string) {
	sort.SliceStable(in, func(i, j int) bool {
		var a, b string
		switch key {
		case "environment":
			a, b = in[i].Environment, in[j].Environment
		case "tier":
			a, b = in[i].Tier, in[j].Tier
		case "domain":
			a, b = in[i].Domain, in[j].Domain
		default: // "id" path
			return in[i].ID < in[j].ID
		}
		if a != b {
			return a < b
		}
		return in[i].ID < in[j].ID // tiebreaker
	})
}

// ── pagination ─────────────────────────────────────────────────────

func paginate(in []TenantSummary, offset, pageSize int) ([]TenantSummary, *int) {
	if offset >= len(in) {
		return []TenantSummary{}, nil
	}
	end := offset + pageSize
	if end >= len(in) {
		return in[offset:], nil
	}
	next := end
	return in[offset:end], &next
}

// ── snapshot cache ─────────────────────────────────────────────────

// tenantSnapshotCache caches the disk scan + YAML parse of the full
// tenant set. The first request after expiry rebuilds; subsequent
// requests serve from memory. At 1000 tenants on a typical SSD a
// rebuild is ~50-100ms; cached responses are sub-millisecond before
// filter / sort. p99 across the 30s window is comfortably under the
// 200ms budget called out in planning §C-1.
//
// We deliberately do NOT couple to the file watcher in cmd/server.
// Forcing the watcher to invalidate the cache would entangle two
// otherwise-orthogonal subsystems; 30s staleness is acceptable for
// the UI search use case (config changes propagate to the manager
// view within at most one TTL). If a customer reports staleness as
// a real problem, the watcher hook is a small follow-up.
type tenantSnapshotCache struct {
	mu       sync.Mutex
	cached   []TenantSummary
	loadedAt time.Time
	ttl      time.Duration
}

// NewTenantSnapshotCache constructs a cache with the default TTL.
// Tests inject a shorter TTL by setting `ttl` after construction.
func NewTenantSnapshotCache() *tenantSnapshotCache {
	return &tenantSnapshotCache{ttl: snapshotTTL}
}

// snapshot returns a fresh-or-cached []TenantSummary. The slice is
// shared across callers — DO NOT mutate it. Filter / sort logic
// works on copies built downstream of this call.
func (c *tenantSnapshotCache) snapshot(configDir string) ([]TenantSummary, error) {
	c.mu.Lock()
	defer c.mu.Unlock()

	if c.cached != nil && time.Since(c.loadedAt) < c.ttl {
		return c.cached, nil
	}

	fresh, err := loadAllTenants(configDir)
	if err != nil {
		return nil, err
	}
	c.cached = fresh
	c.loadedAt = time.Now()
	return c.cached, nil
}

// invalidate forces the next snapshot() call to re-read from disk.
// Reserved for future watch-based invalidation hooks; not currently
// wired but kept as the explicit contract for that future PR.
func (c *tenantSnapshotCache) invalidate() {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.cached = nil
	c.loadedAt = time.Time{}
}

