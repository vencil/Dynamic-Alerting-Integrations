package federation

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/kubernetes/fake"
)

const (
	testCMNamespace = "monitoring"
	testCMName      = "tenant-federation-store"
)

// newFakeConfigMapStore returns a configMapStore backed by a fake
// Kubernetes client whose store ConfigMap has been pre-created (the
// Helm chart's job in production).
func newFakeConfigMapStore(t *testing.T) (RecordStore, kubernetes.Interface) {
	t.Helper()
	cm := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Name: testCMName, Namespace: testCMNamespace},
		Data:       map[string]string{},
	}
	client := fake.NewSimpleClientset(cm)
	st, err := NewConfigMapStore(client, testCMNamespace, testCMName)
	if err != nil {
		t.Fatalf("NewConfigMapStore: %v", err)
	}
	return st, client
}

func TestConfigMapStore_NotFoundFailsLoud(t *testing.T) {
	t.Parallel()
	client := fake.NewSimpleClientset() // no ConfigMap pre-created
	if _, err := NewConfigMapStore(client, testCMNamespace, "missing"); err == nil {
		t.Fatal("expected an error when the store ConfigMap does not exist")
	}
}

func TestConfigMapStore_PutGetListRevoke(t *testing.T) {
	t.Parallel()
	st, client := newFakeConfigMapStore(t)
	now := time.Now()
	r1 := Record{TokenID: "ftk_1", TenantID: "tenant-a", IssuedAt: now, ExpiresAt: now.Add(time.Hour)}
	r2 := Record{TokenID: "ftk_2", TenantID: "tenant-a", IssuedAt: now.Add(time.Second), ExpiresAt: now.Add(time.Hour)}
	r3 := Record{TokenID: "ftk_3", TenantID: "tenant-b", IssuedAt: now, ExpiresAt: now.Add(time.Hour)}
	for _, r := range []Record{r1, r2, r3} {
		if err := st.put(r); err != nil {
			t.Fatalf("put %s: %v", r.TokenID, err)
		}
	}

	got, ok, err := st.get("ftk_1")
	if err != nil || !ok || got.TenantID != "tenant-a" {
		t.Errorf("get(ftk_1) = (%+v, %v, %v)", got, ok, err)
	}
	if _, ok, _ := st.get("ftk_missing"); ok {
		t.Error("get of an unknown token reported present")
	}

	listA, err := st.list("tenant-a", now)
	if err != nil || len(listA) != 2 {
		t.Fatalf("list(tenant-a) = (%d, %v), want 2", len(listA), err)
	}
	if listA[0].TokenID != "ftk_1" || listA[1].TokenID != "ftk_2" {
		t.Errorf("list(tenant-a) not oldest-first: %v", listA)
	}

	deleted, err := st.revoke("ftk_1", r1.ExpiresAt)
	if err != nil || !deleted {
		t.Fatalf("revoke(ftk_1) = (%v, %v), want (true, nil)", deleted, err)
	}
	if _, ok, _ := st.get("ftk_1"); ok {
		t.Error("ftk_1 should be gone after revoke")
	}

	// revoked.txt is the derived, gateway-facing key.
	cm, err := client.CoreV1().ConfigMaps(testCMNamespace).Get(context.Background(), testCMName, metav1.GetOptions{})
	if err != nil {
		t.Fatalf("inspect ConfigMap: %v", err)
	}
	if !strings.Contains(cm.Data[cmKeyRevoked], "ftk_1") {
		t.Errorf("revoked.txt = %q, want it to list ftk_1", cm.Data[cmKeyRevoked])
	}
	if strings.Contains(cm.Data[cmKeyRevoked], "ftk_2") {
		t.Errorf("revoked.txt = %q, must not list a non-revoked token", cm.Data[cmKeyRevoked])
	}
}

func TestConfigMapStore_PrunesExpired(t *testing.T) {
	t.Parallel()
	st, _ := newFakeConfigMapStore(t)
	now := time.Now()
	live := Record{TokenID: "ftk_live", TenantID: "t", IssuedAt: now, ExpiresAt: now.Add(time.Hour)}
	expired := Record{TokenID: "ftk_exp", TenantID: "t", IssuedAt: now.Add(-2 * time.Hour), ExpiresAt: now.Add(-time.Hour)}
	if err := st.put(live); err != nil {
		t.Fatalf("put live: %v", err)
	}
	if err := st.put(expired); err != nil {
		t.Fatalf("put expired: %v", err)
	}
	// A later mutation prunes the expired record.
	if err := st.put(Record{TokenID: "ftk_live2", TenantID: "t", IssuedAt: now, ExpiresAt: now.Add(time.Hour)}); err != nil {
		t.Fatalf("put live2: %v", err)
	}
	if _, ok, _ := st.get("ftk_exp"); ok {
		t.Error("expired record should have been pruned by a later mutation")
	}
}

func TestConfigMapStore_PutEnforcesTenantCap(t *testing.T) {
	t.Parallel()
	st, _ := newFakeConfigMapStore(t)
	now := time.Now()
	// Fill one tenant up to the cap.
	for i := 0; i < maxTokensPerTenant; i++ {
		r := Record{
			TokenID:   fmt.Sprintf("ftk_cap%02d", i),
			TenantID:  "tenant-cap",
			IssuedAt:  now,
			ExpiresAt: now.Add(time.Hour),
		}
		if err := st.put(r); err != nil {
			t.Fatalf("put %d within the cap: %v", i, err)
		}
	}
	// The next new token for that tenant is rejected.
	over := Record{TokenID: "ftk_over", TenantID: "tenant-cap", IssuedAt: now, ExpiresAt: now.Add(time.Hour)}
	if err := st.put(over); !errors.Is(err, ErrTokenLimitReached) {
		t.Fatalf("put past the cap = %v, want ErrTokenLimitReached", err)
	}
	// A different tenant has its own independent cap.
	other := Record{TokenID: "ftk_other", TenantID: "tenant-other", IssuedAt: now, ExpiresAt: now.Add(time.Hour)}
	if err := st.put(other); err != nil {
		t.Fatalf("put for a different tenant: %v", err)
	}
	// Re-putting an existing token id is an idempotent replace, not a new
	// token — it must not be rejected even with the tenant at the cap.
	replace := Record{TokenID: "ftk_cap00", TenantID: "tenant-cap", IssuedBy: "u@example.com", IssuedAt: now, ExpiresAt: now.Add(2 * time.Hour)}
	if err := st.put(replace); err != nil {
		t.Fatalf("idempotent re-put at the cap: %v", err)
	}
}

func TestConfigMapStore_ConcurrentPutNeverExceedsCap(t *testing.T) {
	t.Parallel()
	st, _ := newFakeConfigMapStore(t)
	now := time.Now()
	// Far more concurrent issuers than the cap, all for one tenant — the
	// TOCTOU regression. A list()-then-put() check in the caller would
	// let many of these slip through; the cap lives inside the write
	// transaction, so the stored count can never exceed it.
	const goroutines = maxTokensPerTenant * 2
	var wg sync.WaitGroup
	for i := 0; i < goroutines; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			_ = st.put(Record{
				TokenID:   fmt.Sprintf("ftk_race%03d", i),
				TenantID:  "tenant-race",
				IssuedAt:  now,
				ExpiresAt: now.Add(time.Hour),
			})
		}(i)
	}
	wg.Wait()
	live, err := st.list("tenant-race", now)
	if err != nil {
		t.Fatalf("list: %v", err)
	}
	if len(live) > maxTokensPerTenant {
		t.Fatalf("concurrent issuance stored %d tokens, cap is %d — TOCTOU not closed",
			len(live), maxTokensPerTenant)
	}
}

func TestConfigMapStore_RefusesNewerSchema(t *testing.T) {
	t.Parallel()
	cm := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Name: testCMName, Namespace: testCMNamespace},
		Data:       map[string]string{cmKeyStore: `{"schema_version":"v99","records":[],"revoked":[]}`},
	}
	client := fake.NewSimpleClientset(cm)
	st, err := NewConfigMapStore(client, testCMNamespace, testCMName)
	if err != nil {
		t.Fatalf("NewConfigMapStore: %v", err)
	}
	now := time.Now()
	err = st.put(Record{TokenID: "ftk_x", TenantID: "t", IssuedAt: now, ExpiresAt: now.Add(time.Hour)})
	if err == nil {
		t.Fatal("expected put to refuse writing over a newer schema version")
	}
}
