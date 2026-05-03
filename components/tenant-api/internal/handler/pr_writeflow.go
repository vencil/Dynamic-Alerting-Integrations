package handler

// PR-mode write flow helper shared between PutTenant (single tenant
// per PR) and BatchTenants (multiple tenants per PR).
//
// Pre-PR-6, both handlers ran ~12 lines of duplicated post-WritePR
// glue: build PR title/body → d.PRClient.CreatePR(...) → register
// each touched tenant in d.PRTracker. The two diverged on tenant-
// registration shape: PutTenant mutated `*pr` and registered by
// value; BatchTenants constructed fresh PRInfos in a loop. The
// shared helper unifies both shapes — every per-tenant tracker
// entry is now a defensive copy of the upstream `*pr` with
// TenantID populated, so CreatedAt / Title / HeadRef are
// preserved consistently across single and batch paths.
//
// What's NOT in here:
//   - WritePR / WritePRBatch (different gitops methods)
//   - HasPendingPR check (only single-tenant uses it)
//   - PR title / body construction (caller-specific phrasing)
//   - HTTP response shape (PutTenantResponse vs BatchResponse)

import (
	"github.com/vencil/tenant-api/internal/platform"
)

// createPRAndRegister calls d.PRClient.CreatePR with the supplied
// title / body / branch / labels, then registers one tracker entry
// per tenantID. All entries share the same Number / WebURL / State /
// Title / HeadRef / CreatedAt — only TenantID varies.
//
// Returns the *platform.PRInfo (caller wraps into the HTTP response
// shape; this helper deliberately stays HTTP-agnostic so unit tests
// can drive it without httptest).
//
// Caller MUST have already pushed the branch via d.Writer.WritePR or
// d.Writer.WritePRBatch — this helper only handles the PR creation +
// tracker registration step.
//
// All tenantIDs MUST be non-empty. Empty input is a caller bug
// (would register a no-tenant PR that PendingPRForTenant cannot
// resolve, leaving the PR un-trackable).
func (d *Deps) createPRAndRegister(
	title, body, branchName string,
	labels []string,
	tenantIDs []string,
) (*platform.PRInfo, error) {
	pr, err := d.PRClient.CreatePR(title, body, branchName, labels)
	if err != nil {
		return nil, err
	}
	for _, tid := range tenantIDs {
		// Defensive copy: tracker stores by value, but make the
		// copy explicit so future readers don't wonder whether
		// concurrent register calls could alias the same struct.
		entry := *pr
		entry.TenantID = tid
		d.PRTracker.RegisterPR(entry)
	}
	return pr, nil
}
