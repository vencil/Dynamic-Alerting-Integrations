package statestore

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strconv"
	"strings"
	"sync"
	"testing"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/client-go/kubernetes/fake"
	k8stesting "k8s.io/client-go/testing"
)

const (
	testNS   = "tenant-api"
	testName = "tenant-api-state"
)

func newCM(t *testing.T, data map[string]string) (*configMapStore, *fake.Clientset) {
	t.Helper()
	client := fake.NewSimpleClientset(&corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Name: testName, Namespace: testNS, ResourceVersion: "1"},
		Data:       data,
	})
	enforceResourceVersion(client)
	s, err := NewConfigMap(client, testNS, testName, nil)
	if err != nil {
		t.Fatal(err)
	}
	cs := s.(*configMapStore)
	cs.logger = slog.New(slog.NewTextHandler(&bytes.Buffer{}, nil))
	return cs, client
}

var cmGVR = schema.GroupVersionResource{Version: "v1", Resource: "configmaps"}

// enforceResourceVersion gives the fake clientset the API server's optimistic
// concurrency: the fake's tracker accepts an Update whatever resourceVersion
// it carries (measured), which would let a lost update pass every test here.
// An Update whose resourceVersion is not the stored one is a 409 Conflict;
// an accepted one bumps it.
func enforceResourceVersion(client *fake.Clientset) {
	var mu sync.Mutex
	client.PrependReactor("update", "configmaps", func(action k8stesting.Action) (bool, runtime.Object, error) {
		mu.Lock()
		defer mu.Unlock()
		obj := action.(k8stesting.UpdateAction).GetObject().(*corev1.ConfigMap).DeepCopy()
		stored, err := client.Tracker().Get(cmGVR, obj.Namespace, obj.Name)
		if err != nil {
			return true, nil, err
		}
		rv := stored.(*corev1.ConfigMap).ResourceVersion
		if obj.ResourceVersion != rv {
			return true, nil, apierrors.NewConflict(schema.GroupResource{Resource: "configmaps"}, obj.Name, errors.New("stale resourceVersion"))
		}
		n, _ := strconv.Atoi(rv)
		obj.ResourceVersion = strconv.Itoa(n + 1)
		if err := client.Tracker().Update(cmGVR, obj, obj.Namespace); err != nil {
			return true, nil, err
		}
		return true, obj, nil
	})
}

func newDirStore(t *testing.T) *dirStore {
	t.Helper()
	s, err := NewDir(t.TempDir(), nil)
	if err != nil {
		t.Fatal(err)
	}
	ds := s.(*dirStore)
	ds.logger = slog.New(slog.NewTextHandler(&bytes.Buffer{}, nil))
	return ds
}

// backends runs f against both backends.
func backends(t *testing.T, f func(t *testing.T, s Store)) {
	t.Run("configmap", func(t *testing.T) {
		s, _ := newCM(t, map[string]string{".chart-managed": "x"})
		f(t, s)
	})
	t.Run("dir", func(t *testing.T) { f(t, newDirStore(t)) })
}

func TestGetMutateKeys(t *testing.T) {
	backends(t, func(t *testing.T, s Store) {
		ctx := context.Background()
		if _, ok, err := s.Get(ctx, KeyGroups); err != nil || ok {
			t.Fatalf("fresh store: ok=%v err=%v, want absent", ok, err)
		}
		changed, err := s.Mutate(ctx, KeyGroups, Meta{Entity: "groups"}, func(cur []byte) ([]byte, error) {
			if cur != nil {
				t.Errorf("cur = %q, want nil for an absent record", cur)
			}
			return []byte("groups: {}\n"), nil
		})
		if err != nil || !changed {
			t.Fatalf("Mutate: changed=%v err=%v", changed, err)
		}
		got, ok, err := s.Get(ctx, KeyGroups)
		if err != nil || !ok || string(got) != "groups: {}\n" {
			t.Fatalf("Get = %q ok=%v err=%v", got, ok, err)
		}
		// nil and identical bytes are no-ops.
		for _, next := range [][]byte{nil, []byte("groups: {}\n")} {
			changed, err := s.Mutate(ctx, KeyGroups, Meta{}, func([]byte) ([]byte, error) { return next, nil })
			if err != nil || changed {
				t.Fatalf("no-op Mutate(%q): changed=%v err=%v", next, changed, err)
			}
		}
		// A transform error propagates and writes nothing.
		boom := errors.New("boom")
		if _, err := s.Mutate(ctx, KeyGroups, Meta{}, func([]byte) ([]byte, error) { return []byte("x"), boom }); !errors.Is(err, boom) {
			t.Fatalf("err = %v, want boom", err)
		}
		if got, _, _ := s.Get(ctx, KeyGroups); string(got) != "groups: {}\n" {
			t.Fatalf("after a failed transform the record is %q", got)
		}
		for _, id := range []string{"t-b", "t-a"} {
			if _, err := s.Mutate(ctx, SubsetKey(id), Meta{}, func([]byte) ([]byte, error) { return []byte("metrics: []\n"), nil }); err != nil {
				t.Fatal(err)
			}
		}
		keys, err := s.Keys(ctx, SubsetPrefix)
		if err != nil {
			t.Fatal(err)
		}
		if strings.Join(keys, ",") != "_federation.t-a.yaml,_federation.t-b.yaml" {
			t.Fatalf("Keys = %v", keys)
		}
	})
}

func TestOnWriteFiresOnlyOnChange(t *testing.T) {
	var got []string
	on := func(e string) { got = append(got, e) }
	ds, err := NewDir(t.TempDir(), on)
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	ds.Mutate(ctx, KeyViews, Meta{Entity: "views"}, func([]byte) ([]byte, error) { return []byte("a"), nil })
	ds.Mutate(ctx, KeyViews, Meta{Entity: "views"}, func([]byte) ([]byte, error) { return []byte("a"), nil })
	ds.Mutate(ctx, KeyViews, Meta{Entity: "views"}, func([]byte) ([]byte, error) { return nil, nil })
	if strings.Join(got, ",") != "views" {
		t.Fatalf("onWrite calls = %v, want exactly one", got)
	}
}

// The CAS: an Update that loses a resourceVersion race is re-applied
// against the fresher object, and the transform sees that content.
func TestConfigMapRetriesTransformOnConflict(t *testing.T) {
	s, client := newCM(t, map[string]string{KeyAccountRegistry: "1"})
	conflicts := 1
	client.PrependReactor("update", "configmaps", func(action k8stesting.Action) (bool, runtime.Object, error) {
		if conflicts == 0 {
			return false, nil, nil
		}
		conflicts--
		// Another writer got there first: bump the stored value under us.
		obj, _ := client.Tracker().Get(cmGVR, testNS, testName)
		cm := obj.(*corev1.ConfigMap).DeepCopy()
		cm.Data[KeyAccountRegistry] = "5"
		n, _ := strconv.Atoi(cm.ResourceVersion)
		cm.ResourceVersion = strconv.Itoa(n + 1)
		_ = client.Tracker().Update(cmGVR, cm, testNS)
		return true, nil, apierrors.NewConflict(schema.GroupResource{Resource: "configmaps"}, testName, errors.New("stale"))
	})
	var seen []string
	changed, err := s.Mutate(context.Background(), KeyAccountRegistry, Meta{}, func(cur []byte) ([]byte, error) {
		seen = append(seen, string(cur))
		n, _ := strconv.Atoi(string(cur))
		return []byte(strconv.Itoa(n + 1)), nil
	})
	if err != nil || !changed {
		t.Fatalf("Mutate: changed=%v err=%v", changed, err)
	}
	if strings.Join(seen, ",") != "1,5" {
		t.Fatalf("transform saw %v, want [1 5] (re-run on the fresher object)", seen)
	}
	got, _, _ := s.Get(context.Background(), KeyAccountRegistry)
	if string(got) != "6" {
		t.Fatalf("stored %q, want 6 — the concurrent write must not be lost", got)
	}
}

func TestConfigMapGivesUpWithErrConflict(t *testing.T) {
	s, client := newCM(t, map[string]string{})
	client.PrependReactor("update", "configmaps", func(k8stesting.Action) (bool, runtime.Object, error) {
		return true, nil, apierrors.NewConflict(schema.GroupResource{Resource: "configmaps"}, testName, errors.New("stale"))
	})
	_, err := s.Mutate(context.Background(), KeyViews, Meta{}, func([]byte) ([]byte, error) { return []byte("v"), nil })
	if !errors.Is(err, ErrConflict) {
		t.Fatalf("err = %v, want ErrConflict", err)
	}
}

func TestConfigMapUnavailable(t *testing.T) {
	s, client := newCM(t, map[string]string{})
	client.PrependReactor("get", "configmaps", func(k8stesting.Action) (bool, runtime.Object, error) {
		return true, nil, apierrors.NewServiceUnavailable("down")
	})
	if _, _, err := s.Get(context.Background(), KeyViews); !IsUnavailable(err) {
		t.Fatalf("Get err = %v, want ErrUnavailable", err)
	}
	if _, err := s.Mutate(context.Background(), KeyViews, Meta{}, func([]byte) ([]byte, error) { return []byte("v"), nil }); !IsUnavailable(err) {
		t.Fatalf("Mutate err = %v, want ErrUnavailable", err)
	}
}

func TestConfigMapSizeBudget(t *testing.T) {
	s, _ := newCM(t, map[string]string{})
	big := bytes.Repeat([]byte("x"), sizeBudget+1)
	_, err := s.Mutate(context.Background(), KeyViews, Meta{}, func([]byte) ([]byte, error) { return big, nil })
	if !errors.Is(err, ErrTooLarge) {
		t.Fatalf("err = %v, want ErrTooLarge", err)
	}
	if _, ok, _ := s.Get(context.Background(), KeyViews); ok {
		t.Fatal("an over-budget record was written")
	}
}

func TestConfigMapMustExist(t *testing.T) {
	_, err := NewConfigMap(fake.NewSimpleClientset(), testNS, testName, nil)
	if err == nil || !strings.Contains(err.Error(), "pre-create") {
		t.Fatalf("err = %v, want a not-found error naming the chart", err)
	}
}

// Concurrent increments through two store instances on one ConfigMap (two
// processes) never lose an update: the CAS, not the in-process mutex, is
// what makes the AccountID counter safe.
func TestConfigMapConcurrentWritersLoseNothing(t *testing.T) {
	a, client := newCM(t, map[string]string{KeyAccountRegistry: "0"})
	bs, err := NewConfigMap(client, testNS, testName, nil)
	if err != nil {
		t.Fatal(err)
	}
	b := bs.(*configMapStore)
	b.logger = a.logger
	const n = 20
	var wg sync.WaitGroup
	for i := 0; i < n; i++ {
		s := a
		if i%2 == 1 {
			s = b
		}
		wg.Add(1)
		go func(s *configMapStore) {
			defer wg.Done()
			_, err := s.Mutate(context.Background(), KeyAccountRegistry, Meta{}, func(cur []byte) ([]byte, error) {
				v, _ := strconv.Atoi(string(cur))
				return []byte(strconv.Itoa(v + 1)), nil
			})
			if err != nil {
				t.Error(err)
			}
		}(s)
	}
	wg.Wait()
	got, _, _ := a.Get(context.Background(), KeyAccountRegistry)
	if string(got) != fmt.Sprint(n) {
		t.Fatalf("counter = %s after %d increments", got, n)
	}
}

func TestDirRejectsPathKeys(t *testing.T) {
	s := newDirStore(t)
	for _, k := range []string{"", "../x", "a/b", `a\b`, ".", ".."} {
		if _, err := s.Mutate(context.Background(), k, Meta{}, func([]byte) ([]byte, error) { return []byte("x"), nil }); err == nil {
			t.Errorf("key %q accepted", k)
		}
	}
}

func TestSubsetKeyRoundTrip(t *testing.T) {
	id, ok := SubsetTenant(SubsetKey("db-a"))
	if !ok || id != "db-a" {
		t.Fatalf("SubsetTenant(SubsetKey(db-a)) = %q, %v", id, ok)
	}
	for _, k := range []string{KeyGroups, "_federation..yaml", "_federation.x.yml"} {
		if _, ok := SubsetTenant(k); ok {
			t.Errorf("SubsetTenant(%q) matched", k)
		}
	}
}
