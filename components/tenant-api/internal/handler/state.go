package handler

import (
	"context"
	"errors"
	"fmt"
	"net/http"

	"github.com/vencil/tenant-api/internal/errkind"
	"github.com/vencil/tenant-api/internal/statestore"
)

// mutateState writes one of tenant-api's own records (#2856). transform has
// MutateConfigFile's shape and contract — it reads the current bytes (nil
// when the record does not exist) and returns the next ones, or nil for "no
// change" — with one addition: a store may run it more than once, against
// fresher content, when a concurrent write wins a race.
//
// After the write it re-reads the in-memory snapshot of the record
// (reloadState) according to what the outcome says about the store (#1535):
//
//   - Success: reload synchronously, before the caller answers, so a GET that
//     follows the 2xx sees the write. When that reload fails, the write is
//     saved but reads do not show it: mutateState returns a
//     *stateReloadError (503 STATE_RELOAD_FAILED, committed: true — #1073)
//     instead of nil, so the caller does not answer 2xx for a write the
//     server is not serving. The snapshot keeps its last good content until
//     a later reload succeeds: a resend of the request, or the next
//     WatchLoop tick.
//   - An error that proves the store was not changed: no reload. These are
//     the transform's own error (the handler's validation refusal — 404, 409,
//     a parse error: both backends return it before any write), ErrConflict
//     (the compare-and-swap kept losing; a lost CAS writes nothing) and
//     ErrTooLarge (checked before the update is sent). See notApplied.
//   - Any other error — ErrUnavailable or an unclassified one: the outcome is
//     unknown. A ConfigMap update can reach the API server and its response
//     be lost, so the caller answers 503 for a write that happened; without a
//     reload GET would miss it, and a DELETE of it answer 404, until the next
//     WatchLoop tick. The reload runs in the background (reloadStateAsync) so
//     the error answer does not wait for it: it reads through
//     statestore.ReadFunc, whose context is not the request's and allows a
//     full k8sCallTimeout, and it queues behind every other reload on the
//     watcher's mutex — exactly when the API server is already slow. The
//     snapshot converges shortly after the answer, not before it; at the
//     latest on the next WatchLoop tick. Background reloads are coalesced
//     (configwatcher.ReloadAsync), so an outage's stream of failed writes
//     queues one reload per record, not one per write.
//
// A reload that finds nothing changed is a no-op (the watcher's hash dedup).
func (d *Deps) mutateState(ctx context.Context, key, entity, author string, transform func(current []byte) ([]byte, error), notes ...string) error {
	if d.State == nil {
		return errors.New("tenant-api state store is not configured")
	}
	// transformErr is the error the transform returned on its LAST run (a
	// store may run it more than once). Mutate runs the transforms in the
	// calling goroutine, so no lock is needed.
	var transformErr error
	_, err := d.State.Mutate(ctx, key, statestore.Meta{Entity: entity, Author: author, Notes: notes},
		func(current []byte) ([]byte, error) {
			next, terr := transform(current)
			transformErr = terr
			return next, terr
		})
	switch {
	case err == nil:
		if rerr := d.reloadState(key); rerr != nil {
			return &stateReloadError{key: key, err: rerr}
		}
	case notApplied(err, transformErr):
		// The store is as it was; the snapshot needs nothing.
	default:
		d.reloadStateAsync(key)
	}
	return err
}

// notApplied reports whether err, returned by Store.Mutate, proves the
// record was not written. It is deliberately a closed list; an error not on
// it counts as "unknown" and gets a reload.
//   - transformErr: both backends return the transform's error before
//     writing anything (statestore/dir.go and statestore/configmap.go return
//     it straight from t(cur)). A ConfigMap retry runs it again only after a
//     lost CAS, which wrote nothing either.
//   - ErrConflict: the ConfigMap backend returns it once RetryOnConflict gives
//     up — every attempt's Update was refused with 409 Conflict.
//   - ErrTooLarge: the ConfigMap backend returns it from the size check that
//     runs before the Update is sent.
func notApplied(err, transformErr error) bool {
	if transformErr != nil && errors.Is(err, transformErr) {
		return true
	}
	return errors.Is(err, statestore.ErrConflict) || errors.Is(err, statestore.ErrTooLarge)
}

// reloadState re-reads the in-memory snapshot that serves key, when one does:
// groups, views and the federation whitelist. A federation subset and the
// AccountID registry are read from the store on every request and have none.
// It returns the reload's error (nil for a key with no snapshot); the reload
// metrics record it too (configwatcher.ReloadObserver). On failure the
// snapshot keeps its last good content, as on a WatchLoop tick.
// It is safe to call from several goroutines at once, and beside a
// background reload: Reload holds the watcher's mutex for the whole read, so
// concurrent reloads run one after another and each reads the store as it is
// when its turn comes.
func (d *Deps) reloadState(key string) error {
	switch {
	case key == statestore.KeyGroups && d.Groups != nil:
		return d.Groups.Reload()
	case key == statestore.KeyViews && d.Views != nil:
		return d.Views.Reload()
	case key == statestore.KeyFederationPolicy && d.FederationPolicy != nil:
		return d.FederationPolicy.Reload()
	}
	return nil
}

// stateReloadError is mutateState's answer to a write that the store applied
// and whose snapshot reload then failed (#1073): errkind.StateReloadFailed.
// It deliberately does not unwrap to the reload's error: that error may carry
// a state-store sentinel (statestore.ErrUnavailable from the reload's read),
// and the write must not be answered as one of those — they say nothing was
// written, or that the outcome is unknown, and this write IS saved. The
// reload's error is in Error() for the server log only; the client text is
// msgStateReloadFailed.
type stateReloadError struct {
	key string
	err error
}

func (e *stateReloadError) Error() string {
	return fmt.Sprintf("state record %q written, but reloading the snapshot that serves it failed: %v", e.key, e.err)
}

// ErrorKind is errkind.StateReloadFailed.
func (e *stateReloadError) ErrorKind() errkind.Kind { return errkind.StateReloadFailed }

// msgStateReloadFailed is the fixed client text of STATE_RELOAD_FAILED.
const msgStateReloadFailed = "the change was saved, but this server could not load it yet, so reads may not show it until the next reload; " +
	"it needs no resend (a resend loads it at once, but a resent DELETE answers 404 once a reload has loaded it)"

// reloadStateAsync is reloadState in the background, coalesced per record
// (configwatcher.ReloadAsync: a call while a background reload of the same
// record is still waiting to read is covered by it and dropped).
func (d *Deps) reloadStateAsync(key string) {
	switch {
	case key == statestore.KeyGroups && d.Groups != nil:
		d.Groups.ReloadAsync()
	case key == statestore.KeyViews && d.Views != nil:
		d.Views.ReloadAsync()
	case key == statestore.KeyFederationPolicy && d.FederationPolicy != nil:
		d.FederationPolicy.ReloadAsync()
	}
}

// MutateState is mutateState for the handler subpackages (federation).
func (d *Deps) MutateState(ctx context.Context, key, entity, author string, transform func(current []byte) ([]byte, error), notes ...string) error {
	return d.mutateState(ctx, key, entity, author, transform, notes...)
}

// WriteStateError answers a state-store failure, or reports false when err
// is not one (the caller then applies its own mapping) — kindsState:
//   - statestore.ErrConflict    → 409 (the write kept losing a race; retry)
//   - statestore.ErrUnavailable → 503 + Retry-After / retry_after_s
//     (Kubernetes API or the state directory unreachable)
//   - statestore.ErrTooLarge    → 507 (the ConfigMap's size budget)
//   - *stateReloadError         → 503 STATE_RELOAD_FAILED, committed: true,
//     no Retry-After (saved, not yet served; remedy refresh — #1073)
//
// The bodies are fixed strings: err names the ConfigMap (namespace/name)
// and its byte counts, which are for the operator's log, not the caller.
func WriteStateError(w http.ResponseWriter, r *http.Request, err error) bool {
	handled, _ := writeErr(w, r, nil, err, errCtx{accept: kindsState})
	return handled
}
