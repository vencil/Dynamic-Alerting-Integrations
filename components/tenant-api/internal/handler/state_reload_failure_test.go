package handler

// #1073 item 1: a state write that the store applied, followed by a reload
// of the in-memory snapshot that failed, used to answer 200 — mutateState
// discarded the reload's error (`_ = d.Groups.Reload()`) — so the caller
// believed the change was live while reads kept serving the old snapshot.
// It now answers 503 STATE_RELOAD_FAILED with committed: true and remedy
// refresh: no Retry-After, since the client should reload and look rather
// than resend (a resend does load it — these writes replace or delete by id —
// but a resent PUT overwrites any change made in between).

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"

	"github.com/vencil/tenant-api/internal/errcode"
	"github.com/vencil/tenant-api/internal/federation/fedpolicy"
	"github.com/vencil/tenant-api/internal/groups"
	"github.com/vencil/tenant-api/internal/statestore"
	"github.com/vencil/tenant-api/internal/views"
)

// readFailingStore applies every write; while failReads is set, every read
// (Get — what a snapshot reload reads through) fails with a transient I/O
// error. Mutate is the wrapped store's own, so the write itself is unaffected.
type readFailingStore struct {
	statestore.Store
	failReads atomic.Bool
}

func (s *readFailingStore) Get(ctx context.Context, key string) ([]byte, bool, error) {
	if s.failReads.Load() {
		return nil, false, fmt.Errorf("%w: read %s: injected transient I/O error", statestore.ErrUnavailable, key)
	}
	return s.Store.Get(ctx, key)
}

// wantStateReloadFailed asserts the #1073 answer: 503, code
// STATE_RELOAD_FAILED, committed: true, and no Retry-After on the header
// or in the body (remedy refresh, not retry_after).
func wantStateReloadFailed(t *testing.T, what string, w *httptest.ResponseRecorder) {
	t.Helper()
	if w.Code != http.StatusServiceUnavailable {
		t.Fatalf("%s: status = %d, want 503; body: %s", what, w.Code, w.Body.String())
	}
	var body map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil {
		t.Fatalf("%s: body is not JSON: %v; body: %s", what, err, w.Body.String())
	}
	if body["code"] != string(errcode.StateReloadFailed) {
		t.Errorf("%s: code = %v, want %s", what, body["code"], errcode.StateReloadFailed)
	}
	if body["committed"] != true {
		t.Errorf("%s: committed = %v, want true", what, body["committed"])
	}
	if body["error"] != msgStateReloadFailed {
		t.Errorf("%s: error = %v, want the fixed text %q", what, body["error"], msgStateReloadFailed)
	}
	if got := w.Header().Get("Retry-After"); got != "" {
		t.Errorf("%s: Retry-After = %q, want none (remedy refresh: reload, do not resend)", what, got)
	}
	if v, ok := body["retry_after_s"]; ok {
		t.Errorf("%s: retry_after_s = %v, want absent (remedy refresh: reload, do not resend)", what, v)
	}
}

func TestGroupWrite_ReloadFailureIsNot200(t *testing.T) {
	t.Parallel()
	store := &readFailingStore{Store: mustDirState(t.TempDir())}
	d := &Deps{State: store, Groups: groups.NewManager(store), RBAC: permissiveRBACManager(t)}
	const id = "g-one"
	put := func() *httptest.ResponseRecorder {
		return executeWithRBAC(t, PutGroup(d), stateReq("PUT", "/api/v1/groups/"+id, id, `{"label":"G","members":["t-one"]}`))
	}
	del := func() *httptest.ResponseRecorder {
		return executeWithRBAC(t, DeleteGroup(d), stateReq("DELETE", "/api/v1/groups/"+id, id, ""))
	}
	get := func() int {
		return executeWithRBAC(t, GetGroup(d), stateReq("GET", "/api/v1/groups/"+id, id, "")).Code
	}
	stored := func() bool {
		b, ok, err := store.Store.Get(context.Background(), statestore.KeyGroups)
		if err != nil {
			t.Fatalf("store read: %v", err)
		}
		cfg, err := parseGroupsFile(b)
		if err != nil {
			t.Fatalf("parse stored groups: %v", err)
		}
		_, present := cfg.Groups[id]
		return ok && present
	}

	// PUT: saved, not served.
	store.failReads.Store(true)
	wantStateReloadFailed(t, "PUT with a failing reload", put())
	if !stored() {
		t.Fatal("PUT with a failing reload: the store does not hold the group — committed: true would be a lie")
	}
	store.failReads.Store(false)
	wantCode(t, "GET after the unreloaded PUT (snapshot still old)", get(), http.StatusNotFound, "")
	// Resending the same PUT is safe and loads it.
	wantCode(t, "resent PUT", put().Code, http.StatusOK, "")
	wantCode(t, "GET after the resent PUT", get(), http.StatusOK, "")

	// DELETE: removed from the store, still served.
	store.failReads.Store(true)
	wantStateReloadFailed(t, "DELETE with a failing reload", del())
	if stored() {
		t.Fatal("DELETE with a failing reload: the store still holds the group")
	}
	store.failReads.Store(false)
	wantCode(t, "GET after the unreloaded DELETE (snapshot still old)", get(), http.StatusOK, "")
	wantCode(t, "resent DELETE", del().Code, http.StatusOK, "")
	wantCode(t, "GET after the resent DELETE", get(), http.StatusNotFound, "")
}

func TestViewWrite_ReloadFailureIsNot200(t *testing.T) {
	t.Parallel()
	store := &readFailingStore{Store: mustDirState(t.TempDir())}
	d := &Deps{State: store, Views: views.NewManager(store)}
	const id = "v-one"
	put := func() *httptest.ResponseRecorder {
		return executeWithRBAC(t, PutView(d), stateReq("PUT", "/api/v1/views/"+id, id, `{"label":"V","filters":{"environment":"staging"}}`))
	}
	del := func() *httptest.ResponseRecorder {
		return executeWithRBAC(t, DeleteView(d), stateReq("DELETE", "/api/v1/views/"+id, id, ""))
	}
	get := func() int {
		return executeWithRBAC(t, GetView(d), stateReq("GET", "/api/v1/views/"+id, id, "")).Code
	}

	store.failReads.Store(true)
	wantStateReloadFailed(t, "PUT with a failing reload", put())
	store.failReads.Store(false)
	wantCode(t, "GET after the unreloaded PUT (snapshot still old)", get(), http.StatusNotFound, "")
	wantCode(t, "resent PUT", put().Code, http.StatusOK, "")
	wantCode(t, "GET after the resent PUT", get(), http.StatusOK, "")

	store.failReads.Store(true)
	wantStateReloadFailed(t, "DELETE with a failing reload", del())
	store.failReads.Store(false)
	wantCode(t, "GET after the unreloaded DELETE (snapshot still old)", get(), http.StatusOK, "")
	wantCode(t, "resent DELETE", del().Code, http.StatusOK, "")
	wantCode(t, "GET after the resent DELETE", get(), http.StatusNotFound, "")
}

// The federation whitelist is written by a handler in another package
// (federation.PutPolicy → MutateState → WriteStateError); this pins the
// part of that path that lives here: the error MutateState returns, and the
// answer WriteStateError gives it.
func TestFederationPolicyWrite_ReloadFailureAnsweredByWriteStateError(t *testing.T) {
	t.Parallel()
	store := &readFailingStore{Store: mustDirState(t.TempDir())}
	d := &Deps{State: store, FederationPolicy: fedpolicy.NewManager(store)}
	store.failReads.Store(true)
	err := d.MutateState(context.Background(), statestore.KeyFederationPolicy, "federation-policy", "a@example.com",
		func([]byte) ([]byte, error) { return []byte("whitelist: []\n"), nil })
	var re *stateReloadError
	if !errors.As(err, &re) {
		t.Fatalf("MutateState with a failing reload = %v, want a *stateReloadError", err)
	}
	// Not answered as the reload read's own sentinel: that would say the
	// state store is unreachable (nothing written), and this write is saved.
	if errors.Is(err, statestore.ErrUnavailable) {
		t.Errorf("the reload error unwraps to statestore.ErrUnavailable; it must not")
	}
	w := httptest.NewRecorder()
	if !WriteStateError(w, httptest.NewRequest("PUT", "/api/v1/federation/policy", nil), err) {
		t.Fatal("WriteStateError did not answer a *stateReloadError")
	}
	wantStateReloadFailed(t, "WriteStateError", w)
}

// A record with no snapshot (a federation subset) has nothing to reload, so
// a successful write stays a success whatever the reads do.
func TestMutateState_KeyWithoutSnapshotIgnoresReads(t *testing.T) {
	t.Parallel()
	store := &readFailingStore{Store: mustDirState(t.TempDir())}
	d := &Deps{State: store}
	store.failReads.Store(true)
	if err := d.mutateState(context.Background(), statestore.SubsetKey("t-one"), statestore.SubsetEntity("t-one"), "a@example.com",
		func([]byte) ([]byte, error) { return []byte("x: 1\n"), nil }); err != nil {
		t.Fatalf("mutateState of a key with no snapshot = %v, want nil", err)
	}
}
