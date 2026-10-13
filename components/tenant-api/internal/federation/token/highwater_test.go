package token

import (
	"context"
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/vencil/tenant-api/internal/federation/account"
)

func TestHighWater_AbsentReadsZero_RaiseNeverLowers(t *testing.T) {
	t.Parallel()
	_, client := newFakeConfigMapStore(t)
	hw := NewHighWater(client, testCMNamespace, testCMName)
	ctx := context.Background()

	if got, err := hw.Get(ctx); err != nil || got != (account.Mark{}) {
		t.Fatalf("Get on a fresh store = %+v, %v; want the zero Mark", got, err)
	}
	want := account.Mark{Max: 1004, Base: 1000}
	if wrote, err := hw.Raise(ctx, want); err != nil || !wrote {
		t.Fatalf("Raise(%+v) = %v, %v; want a write", want, wrote, err)
	}
	if wrote, err := hw.Raise(ctx, want); err != nil || wrote {
		t.Fatalf("Raise of the recorded mark again = %v, %v; want no write (someone else's identical mark is not ours)", wrote, err)
	}
	if wrote, err := hw.Raise(ctx, account.Mark{Max: 1001, Base: 1001}); err != nil || wrote {
		t.Fatalf("Raise(1001) after 1004 = %v, %v; want no write (never lowers)", wrote, err)
	}
	if got, _ := hw.Get(ctx); got != want {
		t.Fatalf("Get = %+v, want %+v", got, want)
	}
}

// The mark is an annotation, not a data key: federation-gateway mounts the
// ConfigMap's data whole, so a data key would land inside that pod.
func TestHighWater_IsAnAnnotationNotData(t *testing.T) {
	t.Parallel()
	_, client := newFakeConfigMapStore(t)
	if _, err := NewHighWater(client, testCMNamespace, testCMName).Raise(context.Background(), account.Mark{Max: 1000, Base: 1000}); err != nil {
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
	if _, err := hw.Raise(ctx, account.Mark{Max: 1007, Base: 1005}); err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	if err := st.put(Record{TokenID: "ftk_1", TenantID: "tenant-a", IssuedAt: now, ExpiresAt: now.Add(time.Hour)}); err != nil {
		t.Fatal(err)
	}
	if _, err := st.revoke("ftk_1", now.Add(time.Hour)); err != nil {
		t.Fatal(err)
	}
	if got, _ := hw.Get(ctx); got != (account.Mark{Max: 1007, Base: 1005}) {
		t.Fatalf("mark after token writes = %+v, want {1007 1005}", got)
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
	if _, err := hw.Raise(ctx, account.Mark{Max: 1000, Base: 1000}); err == nil {
		t.Fatal("Raise overwrote an unparsable mark instead of failing")
	}
}
