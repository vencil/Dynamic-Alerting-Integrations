package account

import (
	"strings"
	"testing"

	"gopkg.in/yaml.v3"
)

// TestParse_EmptyStartsAtFloor: a blank/missing registry yields a fresh
// registry primed at the reserved floor, so first-ever allocation = 1000.
func TestParse_EmptyStartsAtFloor(t *testing.T) {
	t.Parallel()
	for _, in := range [][]byte{nil, []byte(""), []byte("   \n\t  ")} {
		reg, err := Parse(in)
		if err != nil {
			t.Fatalf("Parse(%q): %v", in, err)
		}
		if reg.NextAccountID != FirstTenantAccountID {
			t.Errorf("NextAccountID = %d, want floor %d", reg.NextAccountID, FirstTenantAccountID)
		}
		if len(reg.Allocations) != 0 {
			t.Errorf("fresh registry has %d allocations, want 0", len(reg.Allocations))
		}
	}
}

// TestEnsure_FirstIDIsFloor: the very first allocated id is exactly the
// reserved floor (1000), proving 0 and 1..999 are never handed to a tenant.
func TestEnsure_FirstIDIsFloor(t *testing.T) {
	t.Parallel()
	reg := newRegistry()
	id, changed, err := reg.ensure("tenant-alpha")
	if err != nil {
		t.Fatalf("ensure: %v", err)
	}
	if !changed {
		t.Error("first allocation should report changed=true")
	}
	if id != FirstTenantAccountID {
		t.Errorf("first id = %d, want %d", id, FirstTenantAccountID)
	}
	if reg.NextAccountID != FirstTenantAccountID+1 {
		t.Errorf("NextAccountID = %d, want %d", reg.NextAccountID, FirstTenantAccountID+1)
	}
}

// TestEnsure_Monotonic: successive distinct tenants get strictly
// increasing consecutive ids.
func TestEnsure_Monotonic(t *testing.T) {
	t.Parallel()
	reg := newRegistry()
	want := FirstTenantAccountID
	for _, tn := range []string{"a", "b", "c", "d"} {
		id, changed, err := reg.ensure(tn)
		if err != nil || !changed {
			t.Fatalf("ensure(%s) = (%d, %v, %v)", tn, id, changed, err)
		}
		if id != want {
			t.Errorf("ensure(%s) = %d, want %d", tn, id, want)
		}
		want++
	}
}

// TestEnsure_Idempotent: a second ensure for the same tenant returns the
// same id with changed=false and does not advance the counter.
func TestEnsure_Idempotent(t *testing.T) {
	t.Parallel()
	reg := newRegistry()
	first, _, err := reg.ensure("tenant-x")
	if err != nil {
		t.Fatalf("ensure: %v", err)
	}
	nextBefore := reg.NextAccountID
	again, changed, err := reg.ensure("tenant-x")
	if err != nil {
		t.Fatalf("ensure again: %v", err)
	}
	if changed {
		t.Error("re-ensure of an existing tenant reported changed=true")
	}
	if again != first {
		t.Errorf("re-ensure id = %d, want stable %d", again, first)
	}
	if reg.NextAccountID != nextBefore {
		t.Errorf("counter advanced on idempotent ensure: %d → %d", nextBefore, reg.NextAccountID)
	}
}

// TestEnsure_NoRecycleAfterDelete: deleting a tenant's allocation must NOT
// free its id — a later new tenant takes the next number, never the gap.
func TestEnsure_NoRecycleAfterDelete(t *testing.T) {
	t.Parallel()
	reg := newRegistry()
	idA, _, _ := reg.ensure("a") // 1000
	idB, _, _ := reg.ensure("b") // 1001
	if idA != FirstTenantAccountID || idB != FirstTenantAccountID+1 {
		t.Fatalf("setup ids = %d,%d", idA, idB)
	}

	// Simulate offboarding: drop b's allocation, but the high-water mark
	// stays put (the allocator NEVER decrements NextAccountID).
	delete(reg.Allocations, "b")

	idC, _, err := reg.ensure("c")
	if err != nil {
		t.Fatalf("ensure(c): %v", err)
	}
	if idC == idB {
		t.Fatalf("recycled deleted id %d for a new tenant — cross-tenant leak risk", idC)
	}
	if idC != FirstTenantAccountID+2 {
		t.Errorf("ensure(c) = %d, want %d (next, not the freed gap)", idC, FirstTenantAccountID+2)
	}
}

// TestEnsure_EmptyTenantRejected.
func TestEnsure_EmptyTenantRejected(t *testing.T) {
	t.Parallel()
	reg := newRegistry()
	if _, _, err := reg.ensure(""); err == nil {
		t.Fatal("ensure(\"\") should error")
	}
}

// TestRoundTrip: Marshal → Parse preserves allocations and the counter.
func TestRoundTrip(t *testing.T) {
	t.Parallel()
	reg := newRegistry()
	for _, tn := range []string{"alpha", "beta", "gamma"} {
		if _, _, err := reg.ensure(tn); err != nil {
			t.Fatalf("ensure(%s): %v", tn, err)
		}
	}
	data, err := reg.Marshal()
	if err != nil {
		t.Fatalf("Marshal: %v", err)
	}
	got, err := Parse(data)
	if err != nil {
		t.Fatalf("Parse(marshaled): %v", err)
	}
	if got.NextAccountID != reg.NextAccountID {
		t.Errorf("round-trip NextAccountID = %d, want %d", got.NextAccountID, reg.NextAccountID)
	}
	for tn, id := range reg.Allocations {
		if got.Allocations[tn] != id {
			t.Errorf("round-trip %s = %d, want %d", tn, got.Allocations[tn], id)
		}
	}
	// schema_version is stamped on output.
	if !strings.Contains(string(data), "schema_version: v1") {
		t.Errorf("marshaled doc missing schema_version:\n%s", data)
	}
}

// TestParse_RejectsNewerSchema: FAIL CLOSED on an unknown (newer) schema.
func TestParse_RejectsNewerSchema(t *testing.T) {
	t.Parallel()
	doc := []byte("schema_version: v99\nnext_account_id: 1000\nallocations: {}\n")
	if _, err := Parse(doc); err == nil {
		t.Fatal("Parse should reject a newer schema version")
	}
}

// TestParse_RejectsCounterAtOrBelowAllocatedID: a corrupt registry whose
// next_account_id would re-issue a live id must FAIL CLOSED.
func TestParse_RejectsCounterAtOrBelowAllocatedID(t *testing.T) {
	t.Parallel()
	// tenant-a holds 1005 but the counter is only 1003 → next allocation
	// would eventually collide. Refuse.
	doc := []byte("schema_version: v1\nnext_account_id: 1003\nallocations:\n  tenant-a: 1005\n")
	if _, err := Parse(doc); err == nil {
		t.Fatal("Parse should reject a counter at/below an allocated id")
	}
}

// TestParse_RejectsReservedAllocation: a tenant holding a reserved id
// (< floor) signals corruption — refuse.
func TestParse_RejectsReservedAllocation(t *testing.T) {
	t.Parallel()
	doc := []byte("schema_version: v1\nnext_account_id: 1001\nallocations:\n  tenant-a: 42\n")
	if _, err := Parse(doc); err == nil {
		t.Fatal("Parse should reject a tenant holding a reserved (<1000) id")
	}
}

// TestParse_RejectsDuplicateIDs: two tenants holding the SAME id is the
// exact cross-tenant log-merge this package exists to prevent. The monotonic
// allocator never produces it, so it is only reachable via a hand-edit of the
// committed file — Parse must still FAIL CLOSED, with a deterministic message
// regardless of map iteration order.
func TestParse_RejectsDuplicateIDs(t *testing.T) {
	t.Parallel()
	doc := []byte("schema_version: v1\nnext_account_id: 1002\nallocations:\n  tenant-a: 1000\n  tenant-b: 1000\n")
	_, err := Parse(doc)
	if err == nil {
		t.Fatal("Parse should reject two tenants sharing one account id")
	}
	// Deterministic pairing: both names present, lexical order.
	msg := err.Error()
	if !strings.Contains(msg, `"tenant-a"`) || !strings.Contains(msg, `"tenant-b"`) {
		t.Errorf("error should name both colliding tenants, got: %s", msg)
	}
}

// TestParse_RaisesUndersetCounter: a present-but-empty file with a counter
// below the floor is repaired UP to the floor (raise-only, never a reuse).
func TestParse_RaisesUndersetCounter(t *testing.T) {
	t.Parallel()
	doc := []byte("schema_version: v1\nnext_account_id: 5\nallocations: {}\n")
	reg, err := Parse(doc)
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	if reg.NextAccountID != FirstTenantAccountID {
		t.Errorf("NextAccountID = %d, want raised to floor %d", reg.NextAccountID, FirstTenantAccountID)
	}
}

// TestMarshal_LedgerOrderedByID: allocations are emitted id-ascending so
// the committed file reads as an append-only ledger regardless of insert
// order.
func TestMarshal_LedgerOrderedByID(t *testing.T) {
	t.Parallel()
	reg := newRegistry()
	// Allocate in a non-alphabetical order; ids follow allocation order.
	_, _, _ = reg.ensure("zeta")  // 1000
	_, _, _ = reg.ensure("alpha") // 1001
	_, _, _ = reg.ensure("mid")   // 1002

	data, err := reg.Marshal()
	if err != nil {
		t.Fatalf("Marshal: %v", err)
	}
	// Decode into an ordered node to read allocation key order.
	var root yaml.Node
	if err := yaml.Unmarshal(data, &root); err != nil {
		t.Fatalf("unmarshal node: %v", err)
	}
	order := allocationKeyOrder(t, &root)
	want := []string{"zeta", "alpha", "mid"} // id order = onboarding order
	if len(order) != len(want) {
		t.Fatalf("allocation keys = %v, want %v", order, want)
	}
	for i := range want {
		if order[i] != want[i] {
			t.Errorf("allocation key[%d] = %q, want %q (id order)", i, order[i], want[i])
		}
	}
}

// allocationKeyOrder extracts the key order of the `allocations` mapping
// from a parsed YAML document node.
func allocationKeyOrder(t *testing.T, root *yaml.Node) []string {
	t.Helper()
	doc := root
	if doc.Kind == yaml.DocumentNode && len(doc.Content) > 0 {
		doc = doc.Content[0]
	}
	for i := 0; i+1 < len(doc.Content); i += 2 {
		if doc.Content[i].Value == "allocations" {
			alloc := doc.Content[i+1]
			var keys []string
			for j := 0; j+1 < len(alloc.Content); j += 2 {
				keys = append(keys, alloc.Content[j].Value)
			}
			return keys
		}
	}
	return nil
}
