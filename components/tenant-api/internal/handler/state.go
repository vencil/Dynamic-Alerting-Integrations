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
func (d *Deps) mutateState(ctx context.Context, key, entity, author string, transform func(current []byte) ([]byte, error), notes ...string) error {
	if d.State == nil {
		return errors.New("tenant-api state store is not configured")
	}
	_, err := d.State.Mutate(ctx, key, statestore.Meta{Entity: entity, Author: author, Notes: notes}, transform)
	return err
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
