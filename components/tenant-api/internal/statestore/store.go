// Package statestore holds the state tenant-api itself maintains — groups,
// saved views, the federation whitelist, per-tenant federation subsets and
// the AccountID registry — outside the conf.d git tree (#2856).
//
// These used to be files in conf.d committed to the pod's local base branch.
// Nothing ever pushed those commits (PR mode pushes tenant PR branches only),
// so with the chart's default emptyDir they vanished on every restart, and
// with a PVC the local base diverged from origin. They are tenant-api's own
// records, not customer-authored config, so they now live in one Kubernetes
// ConfigMap (Kubernetes deployments) or one plain directory (try-local).
//
// Each record keeps the YAML text it had as a file, so the existing parse and
// marshal code is reused unchanged; only where the bytes live moves.
//
// Accepted cost (owner decision on #2856): these records lose git history and
// PR review. Every write logs a structured `state_write` event instead.
package statestore

import (
	"context"
	"errors"
	"strings"
)

// Record keys. They are the old conf.d file names, so an operator who knew
// the files recognises them, and the AccountID registry keeps the key the
// Vector projection gate already mounts (`_account_registry.yaml`).
const (
	KeyGroups           = "_groups.yaml"
	KeyViews            = "_views.yaml"
	KeyFederationPolicy = "_federation_policy.yaml"
	KeyAccountRegistry  = "_account_registry.yaml"

	// subsetPrefix + tenant id + subsetSuffix is a per-tenant federation
	// subset. A ConfigMap key cannot hold the old `_federation/` directory's
	// slash; tenant ids are DNS-1123 labels, which every key charset allows.
	subsetPrefix = "_federation."
	subsetSuffix = ".yaml"
)

// SubsetKey is the record key of tenantID's federation subset.
func SubsetKey(tenantID string) string { return subsetPrefix + tenantID + subsetSuffix }

// SubsetTenant is SubsetKey's inverse: the tenant id of a subset key, and
// false for any other key.
func SubsetTenant(key string) (string, bool) {
	if !strings.HasPrefix(key, subsetPrefix) || !strings.HasSuffix(key, subsetSuffix) {
		return "", false
	}
	id := strings.TrimSuffix(strings.TrimPrefix(key, subsetPrefix), subsetSuffix)
	if id == "" {
		return "", false
	}
	return id, true
}

// SubsetPrefix is the Keys prefix that lists every federation subset.
const SubsetPrefix = subsetPrefix

// Meta describes one write for the audit event and the change notification.
type Meta struct {
	// Entity names what changed in the change notification ("groups",
	// "views", "federation-policy", "federation/<id>", "account-registry").
	Entity string
	// Author is the caller's identity (email); empty for system writes.
	Author string
	// Notes are extra audit lines, e.g. the federation `--force` reason that
	// used to ride in a commit trailer.
	Notes []string
}

// Transform computes a record's next content from its current one. cur is
// nil when the record does not exist. Returning nil next means "no change"
// and nothing is written. A transform may run more than once (a backend
// retries it against fresher content on a concurrent write), so it must not
// keep state across calls other than what it re-derives from cur.
type Transform func(cur []byte) (next []byte, err error)

// Store is where tenant-api's own state lives.
type Store interface {
	// Get returns a record's content; exists is false when it was never
	// written.
	Get(ctx context.Context, key string) (data []byte, exists bool, err error)
	// Mutate applies t to the record atomically with respect to every other
	// Mutate on the same store. changed reports whether anything was written
	// (false when t returned nil or the same bytes).
	Mutate(ctx context.Context, key string, m Meta, t Transform) (changed bool, err error)
	// Keys lists the record keys starting with prefix, sorted.
	Keys(ctx context.Context, prefix string) ([]string, error)
	// Describe names the backing object for logs, e.g. "configmap ns/name".
	Describe() string
}

// ErrConflict means a write kept losing to concurrent writes and gave up.
// The caller may retry the request.
var ErrConflict = errors.New("statestore: concurrent update, retry the request")

// ErrTooLarge means the write would push the backing object past its size
// budget (a ConfigMap holds at most 1 MiB in total).
var ErrTooLarge = errors.New("statestore: state would exceed the store's size limit")

// ErrUnavailable wraps a failure to reach the backing store (the Kubernetes
// API, the state directory). The request can be retried later.
var ErrUnavailable = errors.New("statestore: state store unavailable")

// OnWrite is called after every write that changed a record, with the
// record's Meta.Entity. tenant-api wires its change-event broadcaster here.
type OnWrite func(entity string)

// ReadFunc adapts one record of s to configwatcher.ReadFunc's shape, so a
// hot-reloading manager can read it the way it read its file.
func ReadFunc(s Store, key string) func() ([]byte, bool, error) {
	return func() ([]byte, bool, error) {
		ctx, cancel := context.WithTimeout(context.Background(), k8sCallTimeout)
		defer cancel()
		return s.Get(ctx, key)
	}
}
