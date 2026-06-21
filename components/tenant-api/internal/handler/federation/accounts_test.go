package federation

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"encoding/json"
	"errors"
	"net/http"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/federation/account"
	"github.com/vencil/tenant-api/internal/federation/token"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/handler"
)

// fakeRegistryWriter implements account.RegistryWriter, returning a fixed
// error from MutateConfigFile so the handler's allocator error-mapping can be
// exercised without a real git plane.
type fakeRegistryWriter struct{ err error }

func (f fakeRegistryWriter) MutateConfigFile(_ context.Context, _, _, _ string, _ func(current []byte) (next []byte, err error)) error {
	return f.err
}

// HistoricalMaxUint is a no-op here so EnsureAccountID reaches the MutateConfigFile
// error path this fake exercises (the revert-floor read returns "no history").
func (f fakeRegistryWriter) HistoricalMaxUint(_ context.Context, _, _ string) (uint32, error) {
	return 0, nil
}

// newLogsFederationDeps builds a Deps wired for logs-plane issuance: a real
// git-backed configDir (so the allocator commits the registry), a real
// token.Manager, and the account.Allocator over the same Writer.
func newLogsFederationDeps(t *testing.T, rbacYAML string, tenantFiles map[string]string) (*handler.Deps, string) {
	t.Helper()
	configDir := setupConfigDir(t, tenantFiles)
	initGitRepo(t, configDir)
	writer := newTestWriter(configDir)

	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatalf("generate key: %v", err)
	}
	fed, err := token.NewManagerForTest(key, filepath.Join(t.TempDir(), "fed-store.json"), time.Hour)
	if err != nil {
		t.Fatalf("NewManagerForTest: %v", err)
	}

	d := &handler.Deps{
		ConfigDir:  configDir,
		Writer:     writer,
		RBAC:       newRBACManager(t, rbacYAML),
		Federation: fed,
		Accounts:   account.NewAllocator(writer),
	}
	return d, configDir
}

// TestCreateFederationToken_LogsCapabilityEmbedsAccountID: capability=logs
// returns a token whose record carries an account_id (the first allocated
// id, 1000) and the logs capability.
func TestCreateFederationToken_LogsCapabilityEmbedsAccountID(t *testing.T) {
	t.Parallel()
	d, configDir := newLogsFederationDeps(t, platformAdminRBAC, nil)

	body := `{"tenant_id":"tenant-alpha","capability":"logs"}`
	w := executeWithRBAC(t, CreateFederationToken(d), fedReq(t, "POST", "/api/v1/federation/tokens", "", "", body))
	if w.Code != http.StatusCreated {
		t.Fatalf("status = %d, want 201, body: %s", w.Code, w.Body.String())
	}
	var resp CreateFederationTokenResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if resp.Record.Capability != "logs" {
		t.Errorf("record capability = %q, want logs", resp.Record.Capability)
	}
	if resp.Record.AccountID != account.FirstTenantAccountID {
		t.Errorf("record account_id = %d, want %d", resp.Record.AccountID, account.FirstTenantAccountID)
	}
	// The registry file was committed with the allocation.
	if _, ok := readRegistry(t, configDir).Lookup("tenant-alpha"); !ok {
		t.Error("registry has no allocation for tenant-alpha after logs token issuance")
	}
}

// TestCreateFederationToken_LogsAllocationIsIdempotent: two logs tokens for
// the same tenant share one account_id (the id is allocate-once).
func TestCreateFederationToken_LogsAllocationIsIdempotent(t *testing.T) {
	t.Parallel()
	d, _ := newLogsFederationDeps(t, platformAdminRBAC, nil)

	issue := func() uint32 {
		body := `{"tenant_id":"tenant-x","capability":"logs"}`
		w := executeWithRBAC(t, CreateFederationToken(d), fedReq(t, "POST", "/api/v1/federation/tokens", "", "", body))
		if w.Code != http.StatusCreated {
			t.Fatalf("status = %d, body: %s", w.Code, w.Body.String())
		}
		var resp CreateFederationTokenResponse
		if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
			t.Fatalf("unmarshal: %v", err)
		}
		return resp.Record.AccountID
	}
	first := issue()
	second := issue()
	if first != second {
		t.Errorf("account_id differed across two logs tokens: %d then %d", first, second)
	}
}

// TestCreateFederationToken_DefaultCapabilityIsMetrics: a body WITHOUT a
// capability field issues the unchanged metrics token — no account_id, and the
// serialised record carries NEITHER a `capability` nor an `account_id` key, so
// it is byte-identical to the pre-ADR-021 shape (struct docstring L34-36;
// CodeRabbit follow-up — toFederationTokenRecord must not echo
// Capability:"metrics").
func TestCreateFederationToken_DefaultCapabilityIsMetrics(t *testing.T) {
	t.Parallel()
	d, _ := newLogsFederationDeps(t, platformAdminRBAC, nil)

	body := `{"tenant_id":"tenant-alpha"}`
	w := executeWithRBAC(t, CreateFederationToken(d), fedReq(t, "POST", "/api/v1/federation/tokens", "", "", body))
	if w.Code != http.StatusCreated {
		t.Fatalf("status = %d, want 201, body: %s", w.Code, w.Body.String())
	}
	var resp CreateFederationTokenResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if resp.Record.AccountID != 0 {
		t.Errorf("default-capability record account_id = %d, want 0", resp.Record.AccountID)
	}
	if resp.Record.Capability != "" {
		t.Errorf("default-capability record capability = %q, want empty (pre-ADR shape)", resp.Record.Capability)
	}

	// Byte-level back-compat: re-decode the `record` object into a raw map and
	// assert the metrics-plane keys are ABSENT (omitempty dropped them), not
	// merely zero-valued. This is the contract toFederationTokenRecord must hold
	// — a metrics record renders exactly as a pre-ADR-021 client expects.
	var rawResp struct {
		Record map[string]json.RawMessage `json:"record"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &rawResp); err != nil {
		t.Fatalf("unmarshal raw: %v", err)
	}
	if _, present := rawResp.Record["capability"]; present {
		t.Errorf("metrics record JSON unexpectedly contains a 'capability' key (breaks pre-ADR-021 shape): %s", w.Body.String())
	}
	if _, present := rawResp.Record["account_id"]; present {
		t.Errorf("metrics record JSON unexpectedly contains an 'account_id' key (breaks pre-ADR-021 shape): %s", w.Body.String())
	}
}

// TestCreateFederationToken_RejectsUnknownCapability: capability must be one
// of metrics|logs (struct-tag oneof).
func TestCreateFederationToken_RejectsUnknownCapability(t *testing.T) {
	t.Parallel()
	d, _ := newLogsFederationDeps(t, platformAdminRBAC, nil)

	body := `{"tenant_id":"tenant-alpha","capability":"traces"}`
	w := executeWithRBAC(t, CreateFederationToken(d), fedReq(t, "POST", "/api/v1/federation/tokens", "", "", body))
	if w.Code != http.StatusBadRequest {
		t.Errorf("status = %d, want 400 for an unknown capability", w.Code)
	}
}

// TestBackfillAccounts_AllocatesFleet: backfill assigns ids to every conf.d
// tenant, monotonic in sorted order; a re-run allocates nothing.
func TestBackfillAccounts_AllocatesFleet(t *testing.T) {
	t.Parallel()
	files := map[string]string{
		"db-a.yaml":              "tenants:\n  db-a: {}\n",
		"db-b.yaml":              "tenants:\n  db-b: {}\n",
		"_defaults.yaml":         "defaults:\n  mysql_cpu: 80\n", // _-prefixed → skipped
		"_account_registry.yaml": "",                             // present-but-empty → skipped, not a tenant
	}
	d, configDir := newLogsFederationDeps(t, platformAdminRBAC, files)

	w := executeWithRBAC(t, BackfillAccounts(d), fedReq(t, "POST", "/api/v1/federation/accounts/backfill", "", "", ""))
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200, body: %s", w.Code, w.Body.String())
	}
	var resp BackfillAccountsResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if resp.AllocatedCount != 2 {
		t.Fatalf("allocated_count = %d, want 2 (db-a, db-b), body: %s", resp.AllocatedCount, w.Body.String())
	}
	// Sorted allocation order → db-a=1000, db-b=1001.
	reg := readRegistry(t, configDir)
	if id, _ := reg.Lookup("db-a"); id != account.FirstTenantAccountID {
		t.Errorf("db-a id = %d, want %d", id, account.FirstTenantAccountID)
	}
	if id, _ := reg.Lookup("db-b"); id != account.FirstTenantAccountID+1 {
		t.Errorf("db-b id = %d, want %d", id, account.FirstTenantAccountID+1)
	}
	// No _-prefixed file leaked in as a tenant.
	if _, ok := reg.Lookup("_defaults"); ok {
		t.Error("backfill allocated an id to _defaults — it must skip _-prefixed files")
	}

	// Re-run is idempotent.
	w2 := executeWithRBAC(t, BackfillAccounts(d), fedReq(t, "POST", "/api/v1/federation/accounts/backfill", "", "", ""))
	var resp2 BackfillAccountsResponse
	_ = json.Unmarshal(w2.Body.Bytes(), &resp2)
	if resp2.AllocatedCount != 0 {
		t.Errorf("re-run allocated_count = %d, want 0", resp2.AllocatedCount)
	}
	if resp2.AlreadyPresent != 2 {
		t.Errorf("re-run already_present = %d, want 2", resp2.AlreadyPresent)
	}
}

// TestCreateFederationToken_LogsAllocatorErrorMapping: when the account
// allocator fails, the logs path maps a degraded/overloaded write plane to
// 503 and a registry HEAD conflict to 409 (so the client retries) and any
// other error to 500 — it never mints a token without an allocated id.
func TestCreateFederationToken_LogsAllocatorErrorMapping(t *testing.T) {
	t.Parallel()
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatalf("generate key: %v", err)
	}
	fed, err := token.NewManagerForTest(key, filepath.Join(t.TempDir(), "fed-store.json"), time.Hour)
	if err != nil {
		t.Fatalf("NewManagerForTest: %v", err)
	}
	cases := []struct {
		name string
		err  error
		want int
	}{
		{"overloaded_503", gitops.ErrWriteOverloaded, http.StatusServiceUnavailable},
		{"degraded_503", gitops.ErrForgeDegraded, http.StatusServiceUnavailable},
		{"conflict_409", gitops.ErrConflict, http.StatusConflict},
		{"generic_500", errors.New("boom"), http.StatusInternalServerError},
	}
	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			d := &handler.Deps{
				RBAC:       newRBACManager(t, platformAdminRBAC),
				Federation: fed,
				Accounts:   account.NewAllocator(fakeRegistryWriter{err: tc.err}),
			}
			body := `{"tenant_id":"tenant-alpha","capability":"logs"}`
			w := executeWithRBAC(t, CreateFederationToken(d), fedReq(t, "POST", "/api/v1/federation/tokens", "", "", body))
			if w.Code != tc.want {
				t.Errorf("status = %d, want %d (body: %s)", w.Code, tc.want, w.Body.String())
			}
		})
	}
}

// TestBackfillAccounts_SurvivesExpiredRequestDeadline: the backfill GitOps write
// must NOT be cancelled by the request's own deadline (the global chi
// middleware.Timeout caps requests at 30s, but a fleet-wide registry write can
// legitimately exceed it). The handler detaches via context.WithoutCancel +
// d.BackfillTimeout(). We prove the detachment by handing the handler a request
// whose context is ALREADY past its deadline: a handler that threaded r.Context()
// into Backfill would commit nothing (ctx.Err() != nil aborts the write); the
// detached handler still allocates the fleet and returns 200.
func TestBackfillAccounts_SurvivesExpiredRequestDeadline(t *testing.T) {
	t.Parallel()
	files := map[string]string{
		"db-a.yaml": "tenants:\n  db-a: {}\n",
		"db-b.yaml": "tenants:\n  db-b: {}\n",
	}
	d, configDir := newLogsFederationDeps(t, platformAdminRBAC, files)

	req := fedReq(t, "POST", "/api/v1/federation/accounts/backfill", "", "", "")
	// Simulate the chi request-Timeout having already fired: an expired-deadline
	// context on the incoming request.
	expired, cancel := context.WithDeadline(req.Context(), time.Now().Add(-time.Second))
	defer cancel()
	req = req.WithContext(expired)

	w := executeWithRBAC(t, BackfillAccounts(d), req)
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200 (backfill must survive an expired request deadline), body: %s",
			w.Code, w.Body.String())
	}
	var resp BackfillAccountsResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if resp.AllocatedCount != 2 {
		t.Errorf("allocated_count = %d, want 2 (the write was not cancelled by the request deadline)", resp.AllocatedCount)
	}
	// The registry was actually committed despite the expired request context.
	if _, ok := readRegistry(t, configDir).Lookup("db-a"); !ok {
		t.Error("registry has no allocation for db-a — the detached backfill write did not commit")
	}
}

// TestBackfillAccounts_ForbiddenForNonPlatformAdmin: a tenant-scoped admin
// (not "*"-scoped) cannot backfill the whole fleet.
func TestBackfillAccounts_ForbiddenForNonPlatformAdmin(t *testing.T) {
	t.Parallel()
	d, _ := newLogsFederationDeps(t, scopedAdminRBAC, map[string]string{"db-a.yaml": "tenants:\n  db-a: {}\n"})

	w := executeWithRBAC(t, BackfillAccounts(d), fedReq(t, "POST", "/api/v1/federation/accounts/backfill", "", "", ""))
	if w.Code != http.StatusForbidden {
		t.Errorf("status = %d, want 403", w.Code)
	}
}

// readRegistry parses the committed _account_registry.yaml for assertions.
// A missing file parses as an empty registry.
func readRegistry(t *testing.T, configDir string) *account.Registry {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(configDir, account.RegistryFileName))
	if errors.Is(err, os.ErrNotExist) {
		data = nil
	} else if err != nil {
		t.Fatalf("read registry: %v", err)
	}
	reg, perr := account.Parse(data)
	if perr != nil {
		t.Fatalf("parse committed registry: %v", perr)
	}
	return reg
}
