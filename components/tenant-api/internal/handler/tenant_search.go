package handler

import (
	"context"
	"fmt"
	"log/slog"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/vencil/tenant-api/internal/gitops"
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
//   - free-text search is a simple case-insensitive substring match
//     across id / owner / domain / db_type / tags[]. Inverted index
//     / fuzzy matching is out of scope; revisit if a customer needs
//     it (rare in practice — operators search by exact id prefix).
//   - DEGRADED rows (#1680 — a tenant whose conf.d file is not usable,
//     carrying only id + config_error) are part of the snapshot on
//     purpose, and go through the same filterTenantsByRBAC (so only
//     metadata-unrestricted callers see them). The exact-match metadata
//     filters (environment / tier / domain / db_type / tag) never match
//     one: its metadata is UNKNOWN, and matching "unknown" against a
//     requested value would be a guess. Free-text q still matches its id
//     — the only field it has — so an operator searching for a tenant by
//     name finds it broken rather than absent. Sorting treats its empty
//     metadata like any unlabeled row (sorts first, id tiebreaker).
//     Pinned by TestSearchTenants_DegradedRows.
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
//   - `config_derivation` (#1988) says when the items' config_derived
//     states were evaluated and which conf.d files the load skipped
//     as unparseable. Their tenants have no config_derived: a root
//     tenant file shows as a degraded item (config_error, #1680); a
//     subdirectory tenant or a platform `_` file is named only here.
//
// next_offset carries `x-nullable`: Swagger 2.0 has no `nullable`, and the
// schemathesis contract test rejects a null it was not told about.
type SearchResponse struct {
	Items        []TenantSummary `json:"items"`
	TotalMatched int             `json:"total_matched"`
	PageSize     int             `json:"page_size"`
	// Offset of the next page; null on the last page.
	NextOffset       *int              `json:"next_offset" extensions:"x-nullable"`
	ConfigDerivation ConfigDerivedMeta `json:"config_derivation"`
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
//
// @Summary     Search tenants
// @Description Server-side filter / sort / pagination over the tenants visible to the caller (RBAC-filtered). Each item's config_derived is derived from config at request time (「依設定推算」), not observed from Alertmanager; the response's config_derivation names the conf.d files skipped as unparseable.
// @Tags        tenants
// @Produce     json
// @Param       q           query    string false "Case-insensitive substring over id / owner / domain / db_type / tags"
// @Param       environment query    string false "Exact environment"
// @Param       tier        query    string false "Exact tier"
// @Param       domain      query    string false "Exact domain"
// @Param       db_type     query    string false "Exact db_type"
// @Param       tag         query    string false "A tag the tenant must carry (case-sensitive)"
// @Param       page_size   query    int    false "Page size (default 50, max 500)"
// @Param       offset      query    int    false "Offset (default 0)"
// @Param       sort        query    string false "Sort key" Enums(id, environment, tier, domain)
// @Success     200 {object} SearchResponse
// @Failure     400 {object} ErrorResponse
// @Failure     500 {object} ErrorResponse
// @Router      /api/v1/tenants/search [get]
func SearchTenants(d *Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		p := rbac.RequestPrincipal(r)

		params, err := parseSearchParams(r)
		if err != nil {
			WriteJSONError(w, r, http.StatusBadRequest, err.Error())
			return
		}

		snap, err := d.SearchCache.snapshot(r.Context(), d.ConfigDir)
		if err != nil {
			WriteJSONError(w, r, http.StatusInternalServerError, err.Error())
			return
		}

		// Apply RBAC FIRST so the "total matched" count reflects what
		// THIS user can see, not what exists globally. UI consumers
		// expect total_matched to equal "rows the user could ever
		// reach by paging".
		visible := filterTenantsByRBAC(snap.summaries, d.RBAC, d.TenantOrg, p)
		matched := applyFilters(visible, params)
		sortTenants(matched, params.sort)

		page, nextOffset := paginate(matched, params.offset, params.pageSize)

		now := time.Now()
		resp := SearchResponse{
			Items:            snap.withConfigDerived(page, now),
			TotalMatched:     len(matched),
			PageSize:         params.pageSize,
			NextOffset:       nextOffset,
			ConfigDerivation: snap.configDerivedMeta(d, p, now),
		}

		writeJSON(w, http.StatusOK, resp)
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

// tenantSnapshot is one load of the config dir: the per-file summaries
// (loadAllTenants) and the exporter's view of the same tree (config.LoadDir,
// #1988). Both are shared across requests — DO NOT mutate. Filter / sort
// work on copies built downstream, and withConfigDerived copies before it
// attaches the per-request reading.
type tenantSnapshot struct {
	summaries []TenantSummary
	derived   configDerivation
	loadedAt  time.Time
}

func loadTenantSnapshot(configDir string) (*tenantSnapshot, error) {
	summaries, err := loadAllTenants(configDir)
	if err != nil {
		return nil, err
	}
	return &tenantSnapshot{summaries: summaries, derived: loadConfigDerivation(configDir), loadedAt: time.Now()}, nil
}

// underivedSnapshot is what is served when there is no trustworthy tree to
// derive from: the tenants, but no config_derived for any of them, with
// loadErr saying why. (withConfigDerived drops the raw per-file state values
// whenever there is no derivation — on every unknown path alike.)
func underivedSnapshot(summaries []TenantSummary, loadedAt time.Time, loadErr string) *tenantSnapshot {
	return &tenantSnapshot{summaries: summaries, derived: configDerivation{loadErr: loadErr}, loadedAt: loadedAt}
}

// Why a request got no derived state. All content-free: configDerivedMeta
// replaces any load error with scopedLoadError for scoped callers anyway.
const (
	writerBusyLoadError = "a config write is in progress; state not derived yet"
	stillLoadingError   = "config is still loading; state not derived yet"
	loadTimeoutError    = "config load did not finish in time; state not derived"
	loadPanicError      = "config load failed; state not derived"
)

// reloadWaitBound caps how long a request waits for ANOTHER request's reload
// before it answers without it. A reload never waits for the writer and its
// load is itself bounded (loadDeadline), so this only matters for a slow load.
const reloadWaitBound = 2 * time.Second

// defaultLoadDeadline bounds how long one load may hold the writer's tree
// lock (see tenantSnapshotCache). It is therefore also the longest a write
// can be delayed by a reader.
const defaultLoadDeadline = 5 * time.Second

// tenantSnapshotCache caches the disk scan + YAML parse of the full
// tenant set, and the config.LoadDir of the same dir, for the list and
// the search endpoints. LoadDir is a cold load (every file read and
// decoded), so per-request loading is exactly what this cache exists to
// prevent.
//
// Tied to the GitOps writer by WireTenantSnapshots:
//   - a load runs only while it holds the writer's tree lock, taken
//     WITHOUT waiting (TryWithTreeLock), so a write never delays a read;
//   - ONE reload at a time (singleflight). Other requests that need a
//     reload wait for it (bounded, and never past their own context)
//     instead of racing it for the tree lock, so "writer-busy" means only
//     that a write holds the tree. A request joins only a reload that
//     started after every Invalidate it has seen;
//   - every writer lock section ends with Invalidate, success or error.
//     Invalidate marks the snapshot STALE: it is older than the latest
//     write. ⛔ A stale snapshot is never served as known. When no fresh
//     answer can be had — a write holds the tree, the wait for another
//     request's reload ran out, the load failed — the answer is UNKNOWN:
//     the tenants (from the last snapshot when there is one), no
//     config_derived, raw values dropped, load_error saying why. A snapshot
//     that is not stale is served as known even while a write runs;
//   - one load holds the tree lock for at most loadDeadline. It runs in a
//     goroutine: a read that never returns (a FIFO named *.yaml, a stuck
//     network mount — the exporter's walker blocks on those, and must stay
//     as it is) would otherwise keep the tree lock, and every write, forever.
//     Past the deadline the reload releases the lock and answers unknown;
//     the load's eventual result is discarded, and no other load starts
//     until it returns — at most one such goroutine exists at a time. A
//     panic in the load is recovered into an unknown answer as well;
//   - in PR mode the load first checks the tree is on the base branch with
//     nothing the loader would read uncommitted (gitops.TreeOnBase). A PR
//     write whose return to base failed leaves its unmerged proposal
//     checked out; that tree is never derived from.
//
// The TTL bounds how long a change made OUTSIDE this API (a git pull, a
// ConfigMap swap) can go unseen. tenant-api has no conf.d watcher.
type tenantSnapshotCache struct {
	// mu guards the fields below. ⛔ Never held across a load or a wait.
	mu       sync.Mutex
	cached   *tenantSnapshot
	stale    bool   // Invalidate was called since cached was loaded
	gen      uint64 // Invalidate count
	inflight *snapshotReload
	stuck    bool // a load passed its deadline and has not returned yet
	ttl      time.Duration

	// loadDeadline bounds one load (0 → defaultLoadDeadline).
	loadDeadline time.Duration
	// tryReadTree runs a load under the writer's tree lock, or reports
	// false without running it when a write holds the lock; nil loads
	// directly (no writer: tests, or a deployment that never writes).
	tryReadTree func(func()) bool
	// checkTree runs before a load; non-nil error means the tree must not
	// be derived from (PR mode: TreeOnBase).
	checkTree func() error
	// afterLoad is a test seam, run after a load read conf.d and before its
	// result is published.
	afterLoad func()
}

// snapshotReload is one in-flight reload; done closes when snap/err are set.
type snapshotReload struct {
	gen  uint64 // c.gen when the reload started
	done chan struct{}
	snap *tenantSnapshot
	err  error
}

// loadOutcome is what one bounded load produced.
type loadOutcome struct {
	snap     *tenantSnapshot
	err      error
	treeErr  error
	panicked bool
}

// NewTenantSnapshotCache constructs a cache with the default TTL.
// Tests inject a shorter TTL by setting `ttl` after construction.
func NewTenantSnapshotCache() *tenantSnapshotCache {
	return &tenantSnapshotCache{ttl: snapshotTTL}
}

// WireTenantSnapshots builds the cache Deps.SearchCache serves from, ties
// it to writer (see tenantSnapshotCache) and loads it once, so a request
// arriving during the first write still has a base snapshot to serve.
// prMode enables the base-branch check. cmd/server calls it; a nil writer
// yields an unwired cache.
func WireTenantSnapshots(writer *gitops.Writer, configDir string, prMode bool) *tenantSnapshotCache {
	c := wireTenantSnapshots(writer, prMode)
	_, _ = c.snapshot(context.Background(), configDir) // prime; an error is retried by the first request
	return c
}

func wireTenantSnapshots(writer *gitops.Writer, prMode bool) *tenantSnapshotCache {
	c := NewTenantSnapshotCache()
	if writer != nil {
		c.tryReadTree = writer.TryWithTreeLock
		writer.SetOnTreeRelease(c.Invalidate)
		if prMode {
			c.checkTree = writer.TreeOnBase
		}
	}
	return c
}

// snapshot returns a fresh-or-cached snapshot, or an unknown one (see
// tenantSnapshotCache). A nil cache (tests that build Deps literally) loads
// on every call. ctx ends the wait for another request's reload.
func (c *tenantSnapshotCache) snapshot(ctx context.Context, configDir string) (*tenantSnapshot, error) {
	if c == nil {
		return loadTenantSnapshot(configDir)
	}
	for {
		c.mu.Lock()
		cur, stale, gen := c.cached, c.stale, c.gen
		if cur != nil && !stale && time.Since(cur.loadedAt) < c.ttl {
			c.mu.Unlock()
			return cur, nil
		}
		r := c.inflight
		if r == nil {
			r = &snapshotReload{gen: gen, done: make(chan struct{})}
			c.inflight = r
			c.mu.Unlock()
			c.reload(configDir, r, cur)
			return r.snap, r.err
		}
		c.mu.Unlock()

		// Another request is reloading: wait for it, but not forever and
		// not past this request's own life.
		wait := time.NewTimer(reloadWaitBound)
		select {
		case <-r.done:
			wait.Stop()
		case <-ctx.Done():
			wait.Stop()
			return nil, ctx.Err()
		case <-wait.C:
			return c.answerWithout(configDir, cur, stillLoadingError)
		}
		// Its result answers this request only if it started after every
		// Invalidate this request saw; otherwise loop and reload again.
		if r.gen == gen {
			return r.snap, r.err
		}
	}
}

// answerWithout answers when no fresh load is available: cur if it is still
// the cached snapshot and not stale (known), otherwise unknown with why.
func (c *tenantSnapshotCache) answerWithout(configDir string, cur *tenantSnapshot, why string) (*tenantSnapshot, error) {
	c.mu.Lock()
	known := cur != nil && c.cached == cur && !c.stale
	c.mu.Unlock()
	if known {
		return cur, nil
	}
	return unknownSnapshot(configDir, cur, why)
}

// unknownSnapshot lists the tenants without derived state: from cur when
// there is one, otherwise from conf.d as it stands (the pre-#1988 list read;
// known limitation: that read is not under the tree lock).
func unknownSnapshot(configDir string, cur *tenantSnapshot, why string) (*tenantSnapshot, error) {
	if cur != nil {
		return underivedSnapshot(cur.summaries, cur.loadedAt, why), nil
	}
	summaries, err := loadAllTenants(configDir)
	if err != nil {
		return nil, err
	}
	return underivedSnapshot(summaries, time.Now(), why), nil
}

// reload performs r: at most one bounded load under the tree lock, published
// to the cache and to every request waiting on r. Whatever happens — a
// panic included — r is completed and the cache is free to reload again.
func (c *tenantSnapshotCache) reload(configDir string, r *snapshotReload, cur *tenantSnapshot) {
	defer func() {
		if p := recover(); p != nil {
			slog.Error("tenant snapshot reload panicked", "panic", p)
			r.snap, r.err = unknownSnapshot(configDir, cur, loadPanicError)
		}
		c.mu.Lock()
		c.inflight = nil
		c.mu.Unlock()
		close(r.done)
	}()

	c.mu.Lock()
	stuck := c.stuck
	c.mu.Unlock()
	if stuck {
		// A previous load is still blocked; do not start a second one.
		r.snap, r.err = c.answerWithout(configDir, cur, loadTimeoutError)
		return
	}

	var out loadOutcome
	var timedOut bool
	work := func() { out, timedOut = c.boundedLoad(configDir) }
	locked := true
	if c.tryReadTree != nil {
		locked = c.tryReadTree(work)
	} else {
		work()
	}

	switch {
	case !locked:
		// A WRITE holds the tree (readers never contend for it: this is the
		// only reload in flight).
		r.snap, r.err = c.answerWithout(configDir, cur, writerBusyLoadError)
	case timedOut:
		r.snap, r.err = unknownSnapshot(configDir, cur, loadTimeoutError)
	case out.panicked:
		r.snap, r.err = unknownSnapshot(configDir, cur, loadPanicError)
	case out.treeErr != nil:
		// Not cached: every reload re-checks until the tree is back on base.
		r.snap, r.err = unknownSnapshot(configDir, cur, relativeToConfDir(out.treeErr.Error(), configDir))
	case out.err != nil:
		r.err = out.err
	default:
		r.snap = out.snap
		c.mu.Lock()
		c.cached = out.snap
		// An Invalidate that landed while this load ran may describe a
		// change the load did not see; keep the result, but reload next time.
		c.stale = c.gen != r.gen
		c.mu.Unlock()
	}
}

// boundedLoad runs the tree check and the load in a goroutine and waits at
// most loadDeadline for it. Past the deadline it marks the cache stuck until
// the goroutine returns, and reports timedOut; the result is then discarded.
func (c *tenantSnapshotCache) boundedLoad(configDir string) (loadOutcome, bool) {
	done := make(chan loadOutcome, 1)
	go func() {
		var out loadOutcome
		defer func() {
			if p := recover(); p != nil {
				slog.Error("tenant snapshot load panicked", "panic", p)
				out = loadOutcome{panicked: true}
			}
			done <- out
		}()
		if c.checkTree != nil {
			if out.treeErr = c.checkTree(); out.treeErr != nil {
				return
			}
		}
		out.snap, out.err = loadTenantSnapshot(configDir)
		if c.afterLoad != nil {
			c.afterLoad()
		}
	}()
	deadline := c.loadDeadline
	if deadline <= 0 {
		deadline = defaultLoadDeadline
	}
	t := time.NewTimer(deadline)
	defer t.Stop()
	select {
	case out := <-done:
		return out, false
	case <-t.C:
		slog.Warn("tenant snapshot load passed its deadline; releasing the tree lock", "deadline", deadline)
		c.mu.Lock()
		c.stuck = true
		c.mu.Unlock()
		go func() {
			<-done // discarded
			c.mu.Lock()
			c.stuck = false
			c.mu.Unlock()
		}()
		return loadOutcome{}, true
	}
}

// Invalidate marks the snapshot stale: it is never served as known again, and
// the next request reloads when it can take the tree lock. Nil-receiver safe.
func (c *tenantSnapshotCache) Invalidate() {
	if c == nil {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	c.stale = true
	c.gen++
}
