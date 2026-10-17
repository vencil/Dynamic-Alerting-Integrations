package handler

// #1535 item 3: a state-store write can be applied although the caller hears
// an error — the ConfigMap update reached the API server and its response was
// lost. Before the fix only the success path re-read the in-memory snapshot,
// so for up to one reload period after such a 503 a GET did not show what was
// written and a DELETE of it answered 404. Every exit of a state write now
// reloads the snapshot.

import (
	"bytes"
	"context"
	"fmt"
	"net/http"
	"sync/atomic"
	"testing"

	"github.com/vencil/tenant-api/internal/groups"
	"github.com/vencil/tenant-api/internal/statestore"
	"github.com/vencil/tenant-api/internal/views"
)

// appliedThenFailedStore applies every Mutate to the store it wraps and then,
// while fail is set, reports it as failed — the shape of a ConfigMap update
// whose response never came back.
type appliedThenFailedStore struct {
	statestore.Store
	fail atomic.Bool
}

func (s *appliedThenFailedStore) Mutate(ctx context.Context, key string, m statestore.Meta, t statestore.Transform) (bool, error) {
	changed, err := s.Store.Mutate(ctx, key, m, t)
	if err == nil && s.fail.Load() {
		return changed, fmt.Errorf("%w: update %s: response lost after the server applied it", statestore.ErrUnavailable, key)
	}
	return changed, err
}

func stateReq(method, path, id, body string) *http.Request {
	req := newRequestWithChiParam(method, path, "id", id, bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	setRequestIdentity(req, "test@example.com")
	return req
}

func wantCode(t *testing.T, what string, got, want int, body string) {
	t.Helper()
	if got != want {
		t.Fatalf("%s: status = %d, want %d; body: %s", what, got, want, body)
	}
}

func TestGroupWrite_ErrorAfterApplyStillReloads(t *testing.T) {
	t.Parallel()
	store := &appliedThenFailedStore{Store: mustDirState(t.TempDir())}
	d := &Deps{State: store, Groups: groups.NewManager(store), RBAC: permissiveRBACManager(t)}
	const id = "g-one"
	put := func() int {
		w := executeWithRBAC(t, PutGroup(d), stateReq("PUT", "/api/v1/groups/"+id, id, `{"label":"G","members":["t-one"]}`))
		return w.Code
	}
	get := func() int {
		return executeWithRBAC(t, GetGroup(d), stateReq("GET", "/api/v1/groups/"+id, id, "")).Code
	}
	del := func() int {
		return executeWithRBAC(t, DeleteGroup(d), stateReq("DELETE", "/api/v1/groups/"+id, id, "")).Code
	}

	// PUT applied, answered 503: the next GET sees it, and DELETE finds it.
	store.fail.Store(true)
	wantCode(t, "PUT (applied, then failed)", put(), http.StatusServiceUnavailable, "")
	wantCode(t, "GET right after the failed-but-applied PUT", get(), http.StatusOK, "")
	store.fail.Store(false)
	wantCode(t, "DELETE of the group that PUT wrote", del(), http.StatusOK, "")
	wantCode(t, "GET after the DELETE", get(), http.StatusNotFound, "")

	// The success path still reloads.
	wantCode(t, "PUT", put(), http.StatusOK, "")
	wantCode(t, "GET after a successful PUT", get(), http.StatusOK, "")

	// DELETE applied, answered 503: the next GET no longer shows the group.
	store.fail.Store(true)
	wantCode(t, "DELETE (applied, then failed)", del(), http.StatusServiceUnavailable, "")
	wantCode(t, "GET right after the failed-but-applied DELETE", get(), http.StatusNotFound, "")
}

func TestViewWrite_ErrorAfterApplyStillReloads(t *testing.T) {
	t.Parallel()
	store := &appliedThenFailedStore{Store: mustDirState(t.TempDir())}
	d := &Deps{State: store, Views: views.NewManager(store)}
	const id = "v-one"
	put := func() int {
		return executeWithRBAC(t, PutView(d), stateReq("PUT", "/api/v1/views/"+id, id, `{"label":"V","filters":{"environment":"staging"}}`)).Code
	}
	get := func() int {
		return executeWithRBAC(t, GetView(d), stateReq("GET", "/api/v1/views/"+id, id, "")).Code
	}
	del := func() int {
		return executeWithRBAC(t, DeleteView(d), stateReq("DELETE", "/api/v1/views/"+id, id, "")).Code
	}

	store.fail.Store(true)
	wantCode(t, "PUT (applied, then failed)", put(), http.StatusServiceUnavailable, "")
	wantCode(t, "GET right after the failed-but-applied PUT", get(), http.StatusOK, "")
	store.fail.Store(false)
	wantCode(t, "DELETE of the view that PUT wrote", del(), http.StatusOK, "")
	wantCode(t, "GET after the DELETE", get(), http.StatusNotFound, "")

	wantCode(t, "PUT", put(), http.StatusOK, "")
	wantCode(t, "GET after a successful PUT", get(), http.StatusOK, "")

	store.fail.Store(true)
	wantCode(t, "DELETE (applied, then failed)", del(), http.StatusServiceUnavailable, "")
	wantCode(t, "GET right after the failed-but-applied DELETE", get(), http.StatusNotFound, "")
}
