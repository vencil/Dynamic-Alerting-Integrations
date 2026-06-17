package handler

import (
	"fmt"
	"net/http"
)

// WriteSourceHeader is the request header a trusted in-cluster automation client
// sets to attribute a PR-mode write to a non-UI source. It lets such PRs carry
// their own label + title + body "Source:" line so reviewers and notifications
// route them on a channel independent of tenant-manager UI edits — without the
// PR body claiming "Source: tenant-manager UI", which would be a lie for an
// automated write (#656, the threshold recommender governance loop).
//
// SECURITY: the header VALUE is never interpolated into the PR title / label /
// body. It is only a lookup key into knownWriteSources; an empty or unknown
// value falls back to defaultWriteSource (the UI attribution). So a hostile or
// malformed header can neither inject PR content nor change behaviour beyond
// selecting one of the fixed, allowlisted attributions below.
const WriteSourceHeader = "X-DA-Write-Source"

// WriteSourceThresholdGovernance is the allowlisted source value emitted by the
// threshold recommender governance loop (#656). Kept as an exported const so the
// value is a single source of truth shared with tests (and any future in-process
// caller).
const WriteSourceThresholdGovernance = "threshold-governance"

// writeSource is the resolved PR attribution for one PR-mode write: the extra
// label(s) appended to the base set, the body "Source:" line, and the
// single-tenant title renderer.
//
// The batch path (BatchTenants) deliberately keeps its own inline
// "tenant-manager UI (batch)" attribution — its only caller is the portal batch
// UI, for which that text is accurate — so it is not routed through here.
type writeSource struct {
	extraLabels []string
	sourceLine  string
	titleSingle func(tenantID string) string
}

// labels returns the PR labels for this source: the shared base labels plus any
// source-specific extras. A fresh slice is returned each call so callers may
// append without aliasing the package-level attribution structs.
func (ws writeSource) labels() []string {
	return append([]string{"tenant-api", "auto-generated"}, ws.extraLabels...)
}

// defaultWriteSource is the tenant-manager UI attribution — the historical
// hardcoded title/labels/body, preserved verbatim so a PUT without the
// governance header behaves exactly as before.
var defaultWriteSource = writeSource{
	extraLabels: nil,
	sourceLine:  "tenant-manager UI",
	titleSingle: func(tenantID string) string {
		return fmt.Sprintf("[tenant-api] Update %s configuration", tenantID)
	},
}

// knownWriteSources maps each allowlisted WriteSourceHeader value to its fixed
// attribution. Onboard a new automation source by adding an entry here — never
// by interpolating the raw header value.
var knownWriteSources = map[string]writeSource{
	WriteSourceThresholdGovernance: {
		extraLabels: []string{"threshold-governance"},
		sourceLine:  "threshold recommender (governance loop, #656)",
		titleSingle: func(tenantID string) string {
			return fmt.Sprintf("[threshold-governance] Recommend threshold update for %s", tenantID)
		},
	},
}

// resolveWriteSource returns the attribution for the request's declared write
// source, falling back to the tenant-manager UI default for an empty / unknown
// header.
func resolveWriteSource(r *http.Request) writeSource {
	if ws, ok := knownWriteSources[r.Header.Get(WriteSourceHeader)]; ok {
		return ws
	}
	return defaultWriteSource
}
