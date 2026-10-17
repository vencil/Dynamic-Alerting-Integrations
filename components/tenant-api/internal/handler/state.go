package handler

import (
	"context"
	"errors"
	"log/slog"
	"net/http"

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
//     follows the 2xx sees the write.
//   - An error that proves the store was not changed: no reload. These are
//     the transform's own error (the handler's validation refusal — 404, 409,
//     a parse error: both backends return it before any write), ErrConflict
//     (the compare-and-swap kept losing; a lost CAS writes nothing) and
//     ErrTooLarge (checked before the update is sent). See notApplied.
//   - Any other error — ErrUnavailable or an unclassified one: the outcome is
//     unknown. A ConfigMap update can reach the API server and its response
//     be lost, so the caller answers 503 for a write that happened; without a
//     reload GET would miss it, and a DELETE of it answer 404, until the next
//     WatchLoop tick. The reload runs in the background so the error answer
//     does not wait for it: it reads through statestore.ReadFunc, whose
//     context is not the request's and allows a full k8sCallTimeout, and it
//     queues behind every other reload on the watcher's mutex — exactly when
//     the API server is already slow.
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
		d.reloadState(key)
	case notApplied(err, transformErr):
		// The store is as it was; the snapshot needs nothing.
	default:
		go d.reloadState(key)
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
// The error is the reload metrics' to report (configwatcher.ReloadObserver);
// on failure the snapshot keeps its last good content, as on a WatchLoop tick.
// It is safe to call from several goroutines at once: Reload holds the
// watcher's mutex for the whole read, so concurrent reloads run one after
// another and each reads the store as it is when its turn comes.
func (d *Deps) reloadState(key string) {
	switch {
	case key == statestore.KeyGroups && d.Groups != nil:
		_ = d.Groups.Reload()
	case key == statestore.KeyViews && d.Views != nil:
		_ = d.Views.Reload()
	case key == statestore.KeyFederationPolicy && d.FederationPolicy != nil:
		_ = d.FederationPolicy.Reload()
	}
}

// MutateState is mutateState for the handler subpackages (federation).
func (d *Deps) MutateState(ctx context.Context, key, entity, author string, transform func(current []byte) ([]byte, error), notes ...string) error {
	return d.mutateState(ctx, key, entity, author, transform, notes...)
}

// WriteStateError answers a state-store failure, or reports false when err
// is not one (the caller then applies its own mapping):
//   - statestore.ErrConflict    → 409 (the write kept losing a race; retry)
//   - statestore.ErrUnavailable → 503 + Retry-After (Kubernetes API or the
//     state directory unreachable)
//   - statestore.ErrTooLarge    → 507 (the ConfigMap's size budget)
func WriteStateError(w http.ResponseWriter, r *http.Request, err error) bool {
	// The bodies are fixed strings: err names the ConfigMap (namespace/name)
	// and its byte counts, which are for the operator's log, not the caller.
	switch {
	case errors.Is(err, statestore.ErrConflict):
		slog.Warn("tenant-api state write kept losing a race", "error", err)
		WriteJSONError(w, r, http.StatusConflict, "tenant-api state changed concurrently, retry the request")
	case errors.Is(err, statestore.ErrUnavailable):
		slog.Warn("tenant-api state store unavailable", "error", err)
		w.Header().Set("Retry-After", "5")
		WriteJSONError(w, r, http.StatusServiceUnavailable, "tenant-api state store unavailable, retry later")
	case errors.Is(err, statestore.ErrTooLarge):
		slog.Error("tenant-api state store is full", "error", err)
		WriteJSONErrorWithCode(w, r, http.StatusInsufficientStorage, CodeStateFull,
			"tenant-api state is full; nothing was written (an operator must shrink groups, views or federation settings)")
	default:
		return false
	}
	return true
}
