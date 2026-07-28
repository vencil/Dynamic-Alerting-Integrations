package token

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"path/filepath"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/kubernetes/fake"
)

// The revoked.txt line contract on the WRITE side (#1235 / TRK-349).
//
// revoked.txt is read by two independently-implemented consumers whose
// agreement depends on every line being unambiguous. tenant-api is the only
// legitimate writer, so it is where a non-conforming id is cheapest to stop —
// and, as the load-side test below pins, the one place where "clean it up on
// read" would be actively dangerous.

func TestTokenIDPattern_AcceptsGeneratedIDs(t *testing.T) {
	t.Parallel()
	for i := 0; i < 50; i++ {
		id, err := newTokenID()
		if err != nil {
			t.Fatalf("newTokenID: %v", err)
		}
		if !validTokenID(id) {
			t.Fatalf("newTokenID produced %q, which violates tokenIDPattern %q", id, tokenIDPattern)
		}
	}
}

func TestValidTokenID(t *testing.T) {
	t.Parallel()
	good := []string{"ftk_1", "ftk_0a1b", "ftk_deadbeefdeadbeef"}
	for _, id := range good {
		if !validTokenID(id) {
			t.Errorf("validTokenID(%q) = false, want true", id)
		}
	}
	bad := []string{
		"",
		"ftk_",       // prefix with no body
		"ftk_G0",     // out-of-charset body
		"FTK_01",     // wrong-case prefix
		"xftk_01",    // leading junk
		"ftk_01x",    // trailing junk
		" ftk_01",    // contract-violating line
		"ftk_01 ",    // contract-violating line
		"ftk_0\n1",   // contract-violating line
		"ftk_01\n",   // contract-violating line
		"ftk_0 1", // contract-violating line
	}
	for _, id := range bad {
		if validTokenID(id) {
			t.Errorf("validTokenID(%q) = true, want false", id)
		}
	}
}

// recordStoreImpls is the roster every RecordStore conformance test runs
// against. A new backend is added HERE — that is the point of the roster.
//
// The RecordStore doc states the revoke precondition (#1235 / TRK-349), and a
// precondition stated on an interface but checked in only one implementation
// is not a precondition, it is a coincidence. The JSON-file backend writes no
// gateway-facing revoked set today, which is exactly the reasoning that would
// let it drift: "it cannot matter here" holds right up until the day the
// backend is promoted, and then nobody re-derives it. Go ships this pattern
// in its own standard library (testing/fstest.TestFS) — a conformance suite
// owned by the interface, not by one implementation.
func recordStoreImpls() map[string]func(t *testing.T) RecordStore {
	return map[string]func(t *testing.T) RecordStore{
		"configmap": func(t *testing.T) RecordStore {
			st, _ := newFakeConfigMapStore(t)
			return st
		},
		"jsonfile": func(t *testing.T) RecordStore {
			st, err := newStore(filepath.Join(t.TempDir(), "store.json"))
			if err != nil {
				t.Fatalf("newStore: %v", err)
			}
			return st
		},
	}
}

// nonConformingTokenIDs are ids that must never reach a store. Kept as
// STRUCTURAL classes (a byte before, after, or inside a well-formed id, plus
// a shape that is simply not one) rather than as a list of interesting
// characters — the breadth over the byte space lives in the conformance
// matrix in tests/ops/test_revoked_set_contract.py.
var nonConformingTokenIDs = []string{
	"",
	"ftk_",       // prefix, no body
	"ftk_G0",     // body outside the charset
	"FTK_01",     // wrong-case prefix
	"xftk_01",    // something before
	"ftk_01x",    // something after
	" ftk_01",    // a separator-ish byte before
	"ftk_01 ",    // …after
	"ftk_0 1",    // …inside
	"ftk_0\n1",   // a real separator inside: two lines pretending to be one
	"not-a-token",
}

// TestRecordStore_RevokeRejectsNonConformingTokenID pins the write-side
// guard on EVERY implementation: the legitimate revoke path must refuse an id
// that would produce a line the two readers could read differently, and must
// report ErrInvalidTokenID rather than quietly normalising or accepting it.
func TestRecordStore_RevokeRejectsNonConformingTokenID(t *testing.T) {
	t.Parallel()
	for name, mk := range recordStoreImpls() {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			st := mk(t)
			exp := time.Now().Add(time.Hour)
			for _, id := range nonConformingTokenIDs {
				found, err := st.revoke(id, exp)
				if !errors.Is(err, ErrInvalidTokenID) {
					t.Errorf("revoke(%q) err = %v, want ErrInvalidTokenID", id, err)
				}
				if found {
					t.Errorf("revoke(%q) reported a deletion despite refusing the id", id)
				}
			}
		})
	}
}

// TestRecordStore_RevokeAcceptsConformingTokenID is the other half: the guard
// must not have been tightened into something that rejects real ids. Without
// it, `return false, ErrInvalidTokenID` unconditionally would pass the test
// above.
func TestRecordStore_RevokeAcceptsConformingTokenID(t *testing.T) {
	t.Parallel()
	for name, mk := range recordStoreImpls() {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			st := mk(t)
			id, err := newTokenID()
			if err != nil {
				t.Fatalf("newTokenID: %v", err)
			}
			// Not present, so `found` is false — but the call must SUCCEED.
			if _, err := st.revoke(id, time.Now().Add(time.Hour)); err != nil {
				t.Errorf("revoke(%q) on a freshly generated id: %v", id, err)
			}
		})
	}
}

// TestConfigMapStore_RefusedRevokeWritesNothing is ConfigMap-specific: only
// that backend derives revoked.txt, so only there can a refused write leave a
// trace in the gateway-facing artifact.
func TestConfigMapStore_RefusedRevokeWritesNothing(t *testing.T) {
	t.Parallel()
	st, client := newFakeConfigMapStore(t)
	exp := time.Now().Add(time.Hour)
	for _, id := range nonConformingTokenIDs {
		if _, err := st.revoke(id, exp); !errors.Is(err, ErrInvalidTokenID) {
			t.Fatalf("revoke(%q) err = %v, want ErrInvalidTokenID", id, err)
		}
	}
	cm, err := client.CoreV1().ConfigMaps(testCMNamespace).Get(
		context.Background(), testCMName, metav1.GetOptions{})
	if err != nil {
		t.Fatalf("inspect ConfigMap: %v", err)
	}
	if got := cm.Data[cmKeyRevoked]; got != "" {
		t.Errorf("a refused revoke still wrote revoked.txt = %q", got)
	}
}

// TestConfigMapStore_LoadKeepsContractViolatingEntry is the counter-intuitive
// half of #1235, and the reason countInvalidRevokedIDs counts instead of
// filtering.
//
// revokedText() regenerates revoked.txt verbatim from the loaded document on
// EVERY write. So if the load path dropped a non-conforming revoked entry as
// "cleanup", the next unrelated write would erase that token from the revoked
// set entirely — tenant-api would finish the un-revoke on the attacker's
// behalf. The entry must survive a full load/write round-trip; it is merely
// reported.
func TestConfigMapStore_LoadKeepsContractViolatingEntry(t *testing.T) {
	t.Parallel()

	future := time.Now().Add(2 * time.Hour).UTC().Truncate(time.Second)
	planted := "ftk_0 1" // contract-violating line
	seeded, err := json.Marshal(&storeDoc{
		SchemaVersion: storeSchemaVersion,
		Revoked: []revokedEntry{
			{TokenID: "ftk_a1", ExpiresAt: future},
			{TokenID: planted, ExpiresAt: future},
		},
	})
	if err != nil {
		t.Fatalf("seed marshal: %v", err)
	}

	cm := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Name: testCMName, Namespace: testCMNamespace},
		Data:       map[string]string{cmKeyStore: string(seeded)},
	}
	var client kubernetes.Interface = fake.NewSimpleClientset(cm)
	rs, err := NewConfigMapStore(client, testCMNamespace, testCMName)
	if err != nil {
		t.Fatalf("NewConfigMapStore: %v", err)
	}
	st := rs.(*configMapStore)
	var logBuf bytes.Buffer
	st.logger = slog.New(slog.NewJSONHandler(&logBuf, nil))

	// Any ordinary write regenerates revoked.txt from the loaded document.
	now := time.Now()
	if err := st.put(Record{
		TokenID: "ftk_b2", TenantID: "tenant-x",
		IssuedAt: now, ExpiresAt: now.Add(time.Hour),
	}); err != nil {
		t.Fatalf("put: %v", err)
	}

	got, err := client.CoreV1().ConfigMaps(testCMNamespace).Get(
		context.Background(), testCMName, metav1.GetOptions{})
	if err != nil {
		t.Fatalf("inspect ConfigMap: %v", err)
	}
	revoked := got.Data[cmKeyRevoked]
	if !strings.Contains(revoked, planted) {
		t.Fatalf("the non-conforming revoked entry was dropped on load — "+
			"regenerating revoked.txt would complete the un-revoke. revoked.txt = %q", revoked)
	}
	if !strings.Contains(revoked, "ftk_a1") {
		t.Errorf("the conforming revoked entry went missing: %q", revoked)
	}

	// It is reported, by count only — the entry itself must never be logged.
	if !strings.Contains(logBuf.String(), "federation_revoked_entry_contract_violation") {
		t.Errorf("load did not report the contract-violating entry: %s", logBuf.String())
	}
	if strings.Contains(logBuf.String(), planted) {
		t.Errorf("the contract-violating entry leaked into the log: %s", logBuf.String())
	}
}

func TestCountInvalidRevokedIDs(t *testing.T) {
	t.Parallel()
	doc := &storeDoc{Revoked: []revokedEntry{
		{TokenID: "ftk_a1"},
		{TokenID: "ftk_0 1"}, // contract-violating line
		{TokenID: "nope"},
		{TokenID: "ftk_beef"},
	}}
	if got := countInvalidRevokedIDs(doc); got != 2 {
		t.Errorf("countInvalidRevokedIDs = %d, want 2", got)
	}
	// It must not mutate the document — the whole point is that the entries stay.
	if len(doc.Revoked) != 4 {
		t.Errorf("countInvalidRevokedIDs mutated doc.Revoked (len %d, want 4)", len(doc.Revoked))
	}
}
