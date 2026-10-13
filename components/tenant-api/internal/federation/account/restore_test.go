package account

import (
	"errors"
	"strings"
	"testing"
)

func mustMarshal(t *testing.T, r *Registry) []byte {
	t.Helper()
	b, err := r.Marshal()
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func reg(next uint32, allocs map[string]uint32) *Registry {
	return &Registry{SchemaVersion: schemaVersion, NextAccountID: next, Allocations: allocs}
}

// #2884: the backup predates t-b. The merge keeps t-b and the counter above
// it, so the next allocation does not re-issue 1001.
func TestMergeForRestore_OlderBackupKeepsCounter(t *testing.T) {
	cur := mustMarshal(t, reg(1002, map[string]uint32{"t-a": 1000, "t-b": 1001}))
	bak := mustMarshal(t, reg(1001, map[string]uint32{"t-a": 1000}))
	m, rep, err := MergeForRestore(cur, bak, nil, RestoreLimits{})
	if err != nil {
		t.Fatal(err)
	}
	if m.NextAccountID != 1002 || m.Allocations["t-b"] != 1001 || rep.Changed() {
		t.Errorf("merged = %+v, report = %+v; want current kept, unchanged", m, rep)
	}
	id, _, _ := m.ensure("t-c")
	if id != 1002 {
		t.Errorf("next allocation = %d, want 1002", id)
	}
}

// The registry was lost: the backup and a live token restore what they
// know, and the counter clears the token's id.
func TestMergeForRestore_LostRegistryUsesTokens(t *testing.T) {
	bak := mustMarshal(t, reg(1001, map[string]uint32{"t-a": 1000}))
	m, rep, err := MergeForRestore(nil, bak, []TokenAllocation{{TenantID: "t-b", AccountID: 1004, TokenID: "fed_1"}}, RestoreLimits{})
	if err != nil {
		t.Fatal(err)
	}
	if m.NextAccountID != 1005 || m.Allocations["t-a"] != 1000 || m.Allocations["t-b"] != 1004 {
		t.Errorf("merged = %+v, want t-a=1000 t-b=1004 next=1005", m)
	}
	if len(rep.Added) != 2 || !strings.Contains(rep.Added[1], "token fed_1") {
		t.Errorf("added = %v, want both tenants, t-b from its token", rep.Added)
	}
	if _, err := Parse(mustMarshal(t, m)); err != nil {
		t.Errorf("merged registry does not parse: %v", err)
	}
}

func TestMergeForRestore_Conflicts(t *testing.T) {
	cur := mustMarshal(t, reg(1002, map[string]uint32{"t-a": 1000, "t-b": 1001}))
	for name, c := range map[string]struct {
		bak    []byte
		tokens []TokenAllocation
		want   string
	}{
		"tenant at two ids": {mustMarshal(t, reg(1002, map[string]uint32{"t-a": 1001})), nil, "tenant t-a"},
		"id held twice":     {mustMarshal(t, reg(1002, map[string]uint32{"t-x": 1001})), nil, "account_id 1001"},
		"token disagrees":   {nil, []TokenAllocation{{TenantID: "t-b", AccountID: 1005, TokenID: "fed_9"}}, "token fed_9: 1005"},
	} {
		_, _, err := MergeForRestore(cur, c.bak, c.tokens, RestoreLimits{})
		var ce *RestoreConflictError
		if !errors.As(err, &ce) || !strings.Contains(err.Error(), c.want) {
			t.Errorf("%s: err = %v, want a conflict naming %q", name, err, c.want)
		}
	}
}

func TestMergeForRestore_RefusesBadInput(t *testing.T) {
	if _, _, err := MergeForRestore(nil, []byte("schema_version: v9\n"), nil, RestoreLimits{}); err == nil {
		t.Error("a backup of an unknown schema was merged")
	}
	if _, _, err := MergeForRestore(nil, nil, []TokenAllocation{{TenantID: "t", AccountID: 5, TokenID: "fed_r"}}, RestoreLimits{}); err == nil {
		t.Error("a token in the reserved range was merged")
	}
}
