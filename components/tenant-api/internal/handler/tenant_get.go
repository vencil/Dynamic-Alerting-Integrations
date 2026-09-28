package handler

import (
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"sync"
	"time"

	cfg "github.com/vencil/threshold-exporter/pkg/config"

	"github.com/go-chi/chi/v5"

	"github.com/vencil/tenant-api/internal/boundedcall"
	"github.com/vencil/tenant-api/internal/confd"
	"github.com/vencil/tenant-api/internal/customalerts"
)

// TenantDetail is the full tenant representation returned by GET /api/v1/tenants/{id}.
type TenantDetail struct {
	ID       string                  `json:"id"`
	RawYAML  string                  `json:"raw_yaml"`
	Resolved []cfg.ResolvedThreshold `json:"resolved_thresholds"`
	// Warnings is the BLOCKING validation set (KeyValidation.Errors) — what a
	// write of this exact file would be rejected on. It judges only the keys
	// the tenant's own file writes: a problem in a root platform file's
	// `tenants:` entry for this tenant (#2208), or in the profile the tenant
	// elects (#1385), is never here. Notices is the advisory set, which
	// never blocks a write: deprecated-key alias notices (#1231 — the file
	// keeps resolving and writing, but carries a spelling the author should
	// migrate), one notice per problem in a root platform file's entry for
	// this tenant, naming that file ("platform file <name>, entry
	// tenants.<id>: …"), and one per problem in the part of the elected
	// profile that reaches this tenant, naming the file and the profile
	// ("platform file <name>, profile "<p>" …") — the platform operator
	// fixes those there. Split fields so a client never has to text-parse
	// severity out of one list.
	Warnings []string `json:"validation_warnings,omitempty"`
	Notices  []string `json:"validation_notices,omitempty"`
	// SourceHash is SHA-256[:16] of the raw tenant file. Clients echo it
	// back as `base_hash` on PUT .../custom-alerts for optimistic-
	// concurrency (ADR-024 §S6b-2): the write 409s if the file changed
	// underneath them.
	SourceHash string `json:"source_hash"`
	// CustomAlerts is the tenant's `_custom_alerts` recipes as structured
	// JSON (ADR-024 §S6b-2). The portal recipe modal reads this directly so
	// the client never parses YAML — the backend owns the round-trip on both
	// read and write. Empty slice when the tenant has none.
	CustomAlerts []map[string]any `json:"custom_alerts"`
	// ConfigError is set when the tenant's file cannot be loaded as a tenant
	// config (#2373) — the same reason GET /api/v1/tenants reports on the
	// tenant's row: malformed_yaml (not parseable as YAML) or invalid_config
	// (parses as YAML but cannot be loaded as a tenant config — wrong shape,
	// errors only a typed decode detects, or a declared tenant id that is not
	// valid UTF-8). threshold-exporter skips such a file whole, so nothing is
	// derived from it: raw_yaml and source_hash are returned (so the file can
	// be opened and fixed), and resolved_thresholds, custom_alerts,
	// validation_warnings and validation_notices are ABSENT — not empty,
	// which would read as "none". Partial writes (PUT .../custom-alerts, the
	// batch patches) refuse such a file with 409; a whole-file PUT repairs
	// it. Absent on a usable file.
	ConfigError string `json:"config_error,omitempty" enums:"malformed_yaml,invalid_config"`
}

// tenantDetailNotLoadable is TenantDetail for a file with a config_error: the
// same JSON names, minus every field derived from the file's content
// (resolved_thresholds, custom_alerts, validation_*), which are absent.
type tenantDetailNotLoadable struct {
	ID          string `json:"id"`
	RawYAML     string `json:"raw_yaml"`
	SourceHash  string `json:"source_hash"`
	ConfigError string `json:"config_error"`
}

// GetTenant handles GET /api/v1/tenants/{id}
//
// @Summary     Get tenant config
// @Description Returns the raw YAML and resolved thresholds for a single tenant.
// @Description When the tenant's file cannot be loaded as a tenant config, the answer is still 200 with raw_yaml and
// @Description source_hash, plus `config_error` (malformed_yaml | invalid_config, as on the list row); threshold-exporter
// @Description skips such a file, so resolved_thresholds, custom_alerts and the validation fields are absent (not empty:
// @Description the file's content is not vouched for). Partial writes refuse such a file with 409 until a whole-file PUT repairs it.
// @Tags        tenants
// @Produce     json
// @Param       id   path     string true "Tenant ID"
// @Success     200  {object} TenantDetail
// @Failure     400  {object} ErrorResponse
// @Failure     404  {object} ErrorResponse
// @Failure     409  {object} ErrorResponse
// @Failure     500  {object} ErrorResponse
// @Router      /api/v1/tenants/{id} [get]
func GetTenant(d *Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		tenantID := chi.URLParam(r, "id")
		if err := ValidateTenantID(tenantID); err != nil {
			WriteJSONError(w, r, http.StatusBadRequest, err.Error())
			return
		}

		// #1673: resolve the tenant's ACTUAL file rather than assuming the
		// `.yaml` spelling. A tenant stored as `<id>.yml` is listed by
		// GET /tenants (the enumerator accepts both) but used to 404 here.
		filePath, err := confd.ResolveTenantFile(d.ConfigDir, tenantID)
		switch {
		case errors.Is(err, confd.ErrTenantFileNotFound):
			WriteJSONError(w, r, http.StatusNotFound, "tenant not found: "+tenantID)
			return
		case errors.Is(err, confd.ErrAmbiguousTenantFile):
			WriteJSONError(w, r, http.StatusConflict, err.Error())
			return
		case err != nil:
			WriteJSONError(w, r, http.StatusInternalServerError, err.Error())
			return
		}
		data, err := os.ReadFile(filePath)
		if os.IsNotExist(err) {
			// Lost a race with a delete between resolve and read.
			WriteJSONError(w, r, http.StatusNotFound, "tenant not found: "+tenantID)
			return
		}
		if err != nil {
			WriteJSONError(w, r, http.StatusInternalServerError, err.Error())
			return
		}

		// #2373: a file threshold-exporter rejects whole (cfg.ParseTenantFile
		// — not YAML, the wrong shape, or a non-UTF-8 declared tenant id) is
		// served as its content plus config_error, the same reason the list
		// row carries. Nothing is derived from it: the exporter serves none
		// of its tenants, so thresholds resolved from it would describe
		// values no plane holds. 200, not 4xx/5xx, so an editor can still
		// load the file (and its source_hash) to fix it. The derived fields
		// are ABSENT, not [] (#2373 review F1): an empty list reads as an
		// authoritative "none", and a client editing from it would write
		// that back over the file's real content (the partial writes refuse
		// such a file for the same reason — checkPartialWriteBase). Not null
		// either: the spec types them as arrays (Swagger 2.0 has no
		// nullable) and does not require them, so absence stays valid.
		if reason := tenantConfigError(data); reason != "" {
			writeJSON(w, http.StatusOK, tenantDetailNotLoadable{
				ID:          tenantID,
				RawYAML:     string(data),
				SourceHash:  cfg.ComputeSourceHash(data),
				ConfigError: reason,
			})
			return
		}

		// Merge over the root platform surface: the defaults carrier, the
		// platform files' per-tenant `tenants:` layer (#2208) and the
		// profile the tenant elects (#1385).
		merged, err := d.loadMergedConfig(tenantID, data)
		if err != nil {
			slog.Error("tenant GET: conf.d root platform read failed", "tenant", tenantID, "error", err)
			WriteJSONErrorWithCode(w, r, http.StatusInternalServerError, CodeInternal, msgRootPlatformRead)
			return
		}

		// #1231 1b: two-channel split — validation_warnings stays Errors-only
		// (the blocking set), deprecation notices get their own field.
		kv := merged.ValidateTenantKeys()
		resolved := merged.ResolveAt(time.Now())

		// Filter to only this tenant's thresholds. Initialized non-nil so a
		// tenant with zero thresholds serializes as [] (the spec declares an
		// array; nil would serialize as null — caught by the contract fuzz,
		// same class as the /me empty-slice normalization in #245).
		tenantResolved := make([]cfg.ResolvedThreshold, 0)
		for _, rt := range resolved {
			if rt.Tenant == tenantID {
				tenantResolved = append(tenantResolved, rt)
			}
		}

		customAlerts, err := customalerts.Extract(string(data), tenantID)
		if err != nil {
			// Unreachable since #2373: Extract fails only on bytes that are not
			// YAML, which tenantConfigError answers above with config_error.
			// Kept as a guard should Extract grow another failure. Surface it rather than returning a 200 with silently-empty
			// custom_alerts. Keep the raw parser error (which can echo file
			// contents) in the server log only; return a stable, non-sensitive
			// message to clients.
			slog.Error("failed to parse tenant custom alerts", "tenant", tenantID, "err", err)
			WriteJSONError(w, r, http.StatusInternalServerError, "failed to parse tenant custom alerts")
			return
		}

		detail := TenantDetail{
			ID:           tenantID,
			RawYAML:      string(data),
			Resolved:     tenantResolved,
			Warnings:     kv.Errors,
			Notices:      kv.Notices,
			SourceHash:   cfg.ComputeSourceHash(data),
			CustomAlerts: customAlerts,
		}

		writeJSON(w, http.StatusOK, detail)
	}
}

// msgRootPlatformRead is the fixed client-facing text for a GET whose read of
// the conf.d root platform files did not complete in time. The full error
// goes to the server log only.
const msgRootPlatformRead = "cannot read the conf.d root platform files in time; " +
	"the tenant's effective thresholds cannot be computed"

// defaultRootReadGuard is the process-wide bound behind Deps.RootReadGuard
// when that field is nil.
var defaultRootReadGuard = &boundedcall.Guard{}

// loadMergedConfig merges the tenant file over the conf.d root platform
// surface — the defaults carrier, the root platform files' per-tenant
// `tenants:` layer, and the profile the tenant elects from those files'
// `profiles:` (#1385; read in the same bounded read — no other file is
// opened for them). Same merge core as validate and the write gate
// (cfg.MergeTenantWithRootDefaults is exactly these two halves), so GET /
// validate / write all merge identically (the consolidation that closed the
// ADR-024 PR4 / #704 write-vs-read asymmetry).
//
// ⛔ THE ROOT READ IS BOUNDED AND SHARED (#2208, PR #2214 review). It reads
// every root `_*.yaml`, and a read that never returns (a FIFO, a hung mount)
// would hang this GET — before #2208 only a file named like the defaults
// carrier could. So:
//
//   - bounded: the read runs under RootReadGuard (boundedcall, the writer's
//     conf.d-walk mechanism, on its own guard so a stuck GET read does not
//     fail writes);
//   - shared: the read is tenant-independent, so concurrent GETs for one
//     conf.d join ONE in-flight read (rootReads) instead of each starting
//     their own. A stuck file therefore costs one stuck goroutine, not one
//     per GET that arrived inside the first timeout window; after it times
//     out, the guard fails new GETs at once until the read returns.
//
// ⚠️ WHAT A JOINER SEES. A GET that arrives while a read is in progress
// joins it and gets what the files said when THAT read started — not
// necessarily what they say when the GET arrived: a platform file rewritten
// after the read began is not seen by GETs that join it. That staleness
// window is at most the duration of the one read, and at most the guard's
// timeout. A GET that starts after the read FINISHED always reads again
// (nothing is kept; unchanged files still hit the content-hash decode
// cache). The per-tenant merge then runs on this goroutine — a panic there
// reaches chi's Recoverer, as before the bound.
func (d *Deps) loadMergedConfig(tenantID string, tenantData []byte) (cfg.TenantMerge, error) {
	guard := d.RootReadGuard
	if guard == nil {
		guard = defaultRootReadGuard
	}
	load := d.loadRoot
	if load == nil {
		load = cfg.LoadRootPlatform
	}
	// Keyed by guard as well as directory: callers with different bounds
	// (tests) must not wait on each other's read.
	key := fmt.Sprintf("%p\x00%s", guard, d.ConfigDir)
	root, err := rootReads.do(key, func() (cfg.RootPlatform, error) {
		// A panic in the read comes back as boundedcall.ErrPanicked (it runs
		// on boundedcall's goroutine, where chi's Recoverer cannot reach);
		// the caller logs it and answers with the same fixed 500.
		return boundedcall.Do(guard, func() cfg.RootPlatform { return load(d.ConfigDir) })
	})
	if err != nil {
		return cfg.TenantMerge{}, err
	}
	return cfg.MergeTenantOverRootPlatform(root, tenantID, tenantData), nil
}

// loadMergedConfig is the unbounded, unshared merge (see Deps.loadMergedConfig).
func loadMergedConfig(configDir, tenantID string, tenantData []byte) cfg.TenantMerge {
	return cfg.MergeTenantWithRootDefaults(configDir, tenantID, tenantData)
}

// rootReads is the process-wide in-flight set behind Deps.loadMergedConfig.
var rootReads = &rootReadFlight{}

// rootReadFlight merges concurrent calls with the same key into one: the
// first caller runs fn, the others wait for its result. A single-purpose
// singleflight (the module has no direct dependency on x/sync, and this
// needs nothing beyond "wait for the one in flight").
//
// ⛔ NOTHING IS REMEMBERED. The entry is removed the moment fn returns, so a
// call that starts afterwards runs fn again. Waiters get the leader's result
// exactly, error included — when the leader's read times out, every GET that
// joined it fails with it, and none of them started a read of its own.
//
// ⚠️ A joiner gets the result of a read that began BEFORE it arrived, so it
// sees the files as they were when the leader started reading them; a
// rewrite in between is invisible to it. The window is bounded by the one
// read's duration, hence by the guard's timeout. Pinned by
// TestGetTenant_ConcurrentGETsGetTheirOwnTenantAndFreshFiles (no read is
// reused once it finished) and TestRootReadFlightMergesOnlyWhatIsInFlight.
type rootReadFlight struct {
	mu    sync.Mutex
	calls map[string]*rootReadCall
}

type rootReadCall struct {
	done    chan struct{}
	val     cfg.RootPlatform
	err     error
	waiters int // joined callers, under rootReadFlight.mu (observability for tests)
}

func (f *rootReadFlight) do(key string, fn func() (cfg.RootPlatform, error)) (cfg.RootPlatform, error) {
	f.mu.Lock()
	if c, ok := f.calls[key]; ok {
		c.waiters++
		f.mu.Unlock()
		<-c.done
		return c.val, c.err
	}
	c := &rootReadCall{done: make(chan struct{})}
	if f.calls == nil {
		f.calls = make(map[string]*rootReadCall)
	}
	f.calls[key] = c
	f.mu.Unlock()

	// fn is boundedcall.Do, which neither panics nor outlives its bound, so
	// the entry is always removed and the waiters always released. The defer
	// keeps that true should fn ever change.
	defer func() {
		f.mu.Lock()
		delete(f.calls, key)
		f.mu.Unlock()
		close(c.done)
	}()
	c.err = errors.New("root platform read did not complete")
	c.val, c.err = fn()
	return c.val, c.err
}

// tenantIDFromPath is a helper for chi URL param extraction used by middleware.
func tenantIDFromPath(r *http.Request) string {
	return chi.URLParam(r, "id")
}

// TenantIDFromPath is the exported version for use in router setup.
var TenantIDFromPath = tenantIDFromPath
