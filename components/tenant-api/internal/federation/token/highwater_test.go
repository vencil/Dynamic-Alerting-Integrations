package token

import (
	"context"
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

func TestHighWater_AbsentReadsZero_RaiseNeverLowers(t *testing.T) {
	t.Parallel()
	_, client := newFakeConfigMapStore(t)
	hw := NewHighWater(client, testCMNamespace, testCMName)
	ctx := context.Background()

	if got, err := hw.Get(ctx); err != nil || got != 0 {
		t.Fatalf("Get on a fresh store = %d, %v; want 0, nil", got, err)
	}
	if got, err := hw.Raise(ctx, 1004); err != nil || got != 1004 {
		t.Fatalf("Raise(1004) = %d, %v", got, err)
	}
	if got, err := hw.Raise(ctx, 1001); err != nil || got != 1004 {
		t.Fatalf("Raise(1001) after 1004 = %d, %v; want 1004 (never lowers)", got, err)
	}
	if got, _ := hw.Get(ctx); got != 1004 {
		t.Fatalf("Get = %d, want 1004", got)
	}
}

// The mark is an annotation, not a data key: federation-gateway mounts the
// ConfigMap's data whole, so a data key would land inside that pod.
func TestHighWater_IsAnAnnotationNotData(t *testing.T) {
	t.Parallel()
	_, client := newFakeConfigMapStore(t)
	if _, err := NewHighWater(client, testCMNamespace, testCMName).Raise(context.Background(), 1000); err != nil {
		t.Fatal(err)
	}
	cm, err := client.CoreV1().ConfigMaps(testCMNamespace).Get(context.Background(), testCMName, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if cm.Annotations[HighWaterAnnotation] != "1000" {
		t.Fatalf("annotation = %q, want \"1000\"", cm.Annotations[HighWaterAnnotation])
	}
	for k := range cm.Data {
		t.Errorf("data key %q appeared; the mark must stay out of the mounted data", k)
	}
}

// Token writes rewrite the whole object; they must carry the mark through.
func TestHighWater_SurvivesTokenWrites(t *testing.T) {
	t.Parallel()
	st, client := newFakeConfigMapStore(t)
	hw := NewHighWater(client, testCMNamespace, testCMName)
	ctx := context.Background()
	if _, err := hw.Raise(ctx, 1007); err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	if err := st.put(Record{TokenID: "ftk_1", TenantID: "tenant-a", IssuedAt: now, ExpiresAt: now.Add(time.Hour)}); err != nil {
		t.Fatal(err)
	}
	if _, err := st.revoke("ftk_1", now.Add(time.Hour)); err != nil {
		t.Fatal(err)
	}
	if got, _ := hw.Get(ctx); got != 1007 {
		t.Fatalf("mark after token writes = %d, want 1007", got)
	}
}

// A value that is not an AccountID fails closed: nothing may be allocated
// against an unknown mark.
func TestHighWater_UnparsableFailsClosed(t *testing.T) {
	t.Parallel()
	_, client := newFakeConfigMapStore(t)
	ctx := context.Background()
	cm, err := client.CoreV1().ConfigMaps(testCMNamespace).Get(ctx, testCMName, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	cm.Annotations = map[string]string{HighWaterAnnotation: "-1"}
	if _, err := client.CoreV1().ConfigMaps(testCMNamespace).Update(ctx, cm, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
	hw := NewHighWater(client, testCMNamespace, testCMName)
	if _, err := hw.Get(ctx); err == nil {
		t.Fatal("Get accepted a mark of -1")
	}
	if _, err := hw.Raise(ctx, 1000); err == nil {
		t.Fatal("Raise overwrote an unparsable mark instead of failing")
	}
}
