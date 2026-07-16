package token

// Tests for the cross-tenant listAll chain feeding the federation
// OrphanDetector (#521): store.listAll (JSON test backend),
// configMapStore.listAll (production backend), and the public
// Manager.ListAllRecords delegate. Contract shared by all three:
// every NON-EXPIRED record across ALL tenants, oldest first.

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"encoding/json"
	"path/filepath"
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// listAllFixture returns three live records spanning two tenants plus
// one expired record, deliberately in non-chronological insert order so
// the oldest-first sort is actually exercised.
func listAllFixture(now time.Time) []Record {
	return []Record{
		{TokenID: "ftk_b1", TenantID: "tenant-b", IssuedAt: now.Add(2 * time.Second), ExpiresAt: now.Add(time.Hour)},
		{TokenID: "ftk_a1", TenantID: "tenant-a", IssuedAt: now, ExpiresAt: now.Add(time.Hour)},
		{TokenID: "ftk_exp", TenantID: "tenant-a", IssuedAt: now.Add(-2 * time.Hour), ExpiresAt: now.Add(-time.Hour)},
		{TokenID: "ftk_a2", TenantID: "tenant-a", IssuedAt: now.Add(time.Second), ExpiresAt: now.Add(time.Hour)},
	}
}

// assertListAll checks the shared listAll contract on got.
func assertListAll(t *testing.T, got []Record) {
	t.Helper()
	want := []string{"ftk_a1", "ftk_a2", "ftk_b1"}
	if len(got) != len(want) {
		t.Fatalf("listAll returned %d records (%v), want %d", len(got), tokenIDs(got), len(want))
	}
	for i, id := range want {
		if got[i].TokenID != id {
			t.Errorf("listAll[%d] = %s, want %s (full: %v)", i, got[i].TokenID, id, tokenIDs(got))
		}
	}
}

func tokenIDs(recs []Record) []string {
	ids := make([]string, len(recs))
	for i, r := range recs {
		ids[i] = r.TokenID
	}
	return ids
}

func TestStore_ListAll(t *testing.T) {
	t.Parallel()
	st, err := newStore(filepath.Join(t.TempDir(), "store.json"))
	if err != nil {
		t.Fatalf("newStore: %v", err)
	}
	now := time.Now()
	for _, r := range listAllFixture(now) {
		// put() prunes expired records on every mutation, so insert the
		// expired one directly — listAll itself must still filter it.
		st.mu.Lock()
		st.recs[r.TokenID] = r
		st.mu.Unlock()
	}

	got, err := st.listAll(now)
	if err != nil {
		t.Fatalf("listAll: %v", err)
	}
	assertListAll(t, got)
}

func TestConfigMapStore_ListAll(t *testing.T) {
	t.Parallel()
	st, client := newFakeConfigMapStore(t)
	now := time.Now()
	// Seed by writing the store.json document DIRECTLY into the ConfigMap
	// rather than via put(): mutate() prunes expired records on every
	// write, so a put()-seeded fixture would never present ftk_exp to
	// listAll and its own `!r.expired(now)` filter would go untested
	// (a mutation dropping that filter survived exactly this way). An
	// expired-but-unpruned record is a real production state — pruning
	// only happens on WRITES, and the detector's listAll polling between
	// writes must still filter it.
	ctx := context.Background()
	raw, err := json.Marshal(&storeDoc{SchemaVersion: storeSchemaVersion, Records: listAllFixture(now)})
	if err != nil {
		t.Fatalf("marshal seed doc: %v", err)
	}
	cm, err := client.CoreV1().ConfigMaps(testCMNamespace).Get(ctx, testCMName, metav1.GetOptions{})
	if err != nil {
		t.Fatalf("get ConfigMap: %v", err)
	}
	cm.Data[cmKeyStore] = string(raw)
	if _, err := client.CoreV1().ConfigMaps(testCMNamespace).Update(ctx, cm, metav1.UpdateOptions{}); err != nil {
		t.Fatalf("seed ConfigMap: %v", err)
	}

	got, err := st.listAll(now)
	if err != nil {
		t.Fatalf("listAll: %v", err)
	}
	assertListAll(t, got) // ftk_exp filtered by listAll itself, not by a prior prune
}

// A corrupted store.json must surface as an ERROR from listAll — the
// OrphanDetector treats it as "skip this pass", so a read failure must
// never be masked as an empty (i.e. all-orphaned) listing.
func TestConfigMapStore_ListAll_UnparseableDocErrors(t *testing.T) {
	t.Parallel()
	st, client := newFakeConfigMapStore(t)
	ctx := context.Background()
	cm, err := client.CoreV1().ConfigMaps(testCMNamespace).Get(ctx, testCMName, metav1.GetOptions{})
	if err != nil {
		t.Fatalf("get ConfigMap: %v", err)
	}
	cm.Data[cmKeyStore] = "{not json"
	if _, err := client.CoreV1().ConfigMaps(testCMNamespace).Update(ctx, cm, metav1.UpdateOptions{}); err != nil {
		t.Fatalf("corrupt ConfigMap: %v", err)
	}

	if _, err := st.listAll(time.Now()); err == nil {
		t.Fatal("listAll over an unparseable store.json must error, not return an empty listing")
	}
}

func TestManager_ListAllRecords(t *testing.T) {
	t.Parallel()
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatalf("generate key: %v", err)
	}
	m, err := NewManagerForTest(key, filepath.Join(t.TempDir(), "store.json"), time.Hour)
	if err != nil {
		t.Fatalf("NewManagerForTest: %v", err)
	}
	now := time.Now()
	for _, r := range listAllFixture(now) {
		if r.TokenID == "ftk_exp" {
			continue // put() prunes it anyway; covered by TestStore_ListAll
		}
		if err := m.store.put(r); err != nil {
			t.Fatalf("put %s: %v", r.TokenID, err)
		}
	}

	got, err := m.ListAllRecords()
	if err != nil {
		t.Fatalf("ListAllRecords: %v", err)
	}
	assertListAll(t, got)
}
