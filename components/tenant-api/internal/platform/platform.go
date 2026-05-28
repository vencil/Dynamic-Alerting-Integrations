// Package platform defines provider-agnostic interfaces for PR/MR-based write-back.
//
// Both GitHub (Pull Requests) and GitLab (Merge Requests) implement these
// interfaces. Handlers depend only on these interfaces, not on provider-specific
// types. This enables seamless switching between providers via --write-mode flag.
//
// Design: ADR-011 — PR-based write-back supports multiple Git hosting platforms.
package platform

import (
	"context"
	"time"
)

// PRInfo holds metadata about a created or existing pull/merge request.
// The fields are intentionally provider-neutral; "Number" maps to GitHub PR
// number or GitLab MR IID (project-scoped).
type PRInfo struct {
	Number    int    `json:"number"`               // GitHub PR number or GitLab MR IID
	WebURL    string `json:"web_url"`              // Browser-accessible URL
	State     string `json:"state"`                // "open"/"opened", "closed", "merged"
	Title     string `json:"title"`                // PR/MR title
	HeadRef   string `json:"head_ref"`             // Source branch name
	CreatedAt string `json:"created_at,omitempty"` // ISO 8601 timestamp
	TenantID  string `json:"tenant_id,omitempty"`  // Extracted from branch name
}

// Client abstracts Git hosting platform operations for PR/MR creation.
// Implementations: github.Client, gitlab.Client.
type Client interface {
	// ValidateToken checks if the configured token has valid permissions.
	ValidateToken() error

	// CreateBranch creates a new branch from the configured base branch HEAD.
	CreateBranch(branchName string) error

	// CreatePR creates a pull/merge request and returns its metadata.
	CreatePR(title, body, headBranch string, labels []string) (*PRInfo, error)

	// ListOpenPRs returns all open PRs/MRs created by tenant-api.
	ListOpenPRs() ([]PRInfo, error)

	// DeleteBranch deletes a feature branch (cleanup after merge/close).
	DeleteBranch(branchName string) error

	// SetBaseURL overrides the API base URL (for self-hosted instances).
	SetBaseURL(url string)

	// ProviderName returns the platform name ("GitHub" or "GitLab").
	ProviderName() string
}

// Tracker maintains an in-memory cache of pending PRs/MRs, periodically
// syncing with the platform API. Implementations: github.Tracker, gitlab.Tracker.
type Tracker interface {
	// WatchLoop periodically syncs the pending PR/MR list.
	// Call in a goroutine. Stops when stopCh is closed.
	WatchLoop(stopCh <-chan struct{})

	// PendingPRs returns all tracked pending PRs/MRs.
	PendingPRs() []PRInfo

	// PendingPRForTenant returns the pending PR/MR for a specific tenant.
	PendingPRForTenant(tenantID string) (PRInfo, bool)

	// HasPendingPR checks if a tenant has an open PR/MR pending review.
	HasPendingPR(tenantID string) bool

	// ClaimTenant atomically reserves a tenant for an in-flight PR/MR
	// creation. It returns false if the tenant already has a pending PR
	// (cached/registered) OR an in-flight claim — i.e. another request is
	// mid-creation. This is a *synchronous* check-and-set that does NOT
	// depend on the async poll cadence, closing the check-then-RegisterPR
	// race for concurrent same-tenant writes. The caller MUST ReleaseClaim
	// on any failure (RegisterPR clears the claim on success).
	ClaimTenant(tenantID string) bool

	// ReleaseClaim drops an in-flight claim taken by ClaimTenant. Safe to
	// call when no claim is held (no-op). Call it when PR/MR creation fails
	// so a retry isn't blocked by a stuck claim.
	ReleaseClaim(tenantID string)

	// RegisterPR adds a newly created PR/MR to the tracker immediately.
	RegisterPR(pr PRInfo)

	// LastSyncTime returns when the tracker last synced with the platform.
	LastSyncTime() time.Time

	// RefreshNow forces a synchronous out-of-cadence refresh of the pending PR/MR
	// cache, bounded by ctx (#644). On the 409/pending_pr_exists path the handler
	// uses this to close the polling-staleness window: after a merge, the cache
	// shows the PR as still open for up to ~30 s and would return a spurious 409
	// until the next periodic sync. The ctx bound is what stops a degraded forge
	// from extending the 409's response latency — if ctx expires, the underlying
	// refresh may continue in the background but RefreshNow returns immediately
	// and the handler falls through to the stale 409 (safe fallback).
	RefreshNow(ctx context.Context)
}
