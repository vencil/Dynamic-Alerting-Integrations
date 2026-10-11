package statestore

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"log/slog"
	"sort"
	"strings"
	"sync"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/util/retry"
)

// k8sCallTimeout bounds one ConfigMap API call.
const k8sCallTimeout = 10 * time.Second

// sizeBudget is the most data the store lets the ConfigMap carry. The API
// server refuses an object over 1 MiB including metadata; the margin keeps a
// write from failing there with a less useful error.
const sizeBudget = 900 * 1024

// conflictRetry is how often a write is re-applied against fresher content
// after losing a resourceVersion race before it gives up with ErrConflict.
var conflictRetry = retry.DefaultRetry

// configMapStore keeps every record as one data key of a single ConfigMap
// the Helm chart pre-creates. Writes are compare-and-swap on the object's
// resourceVersion (client-go's RetryOnConflict), the same mechanism as the
// federation token store (internal/federation/token/configmap_store.go).
type configMapStore struct {
	client    kubernetes.Interface
	namespace string
	name      string
	onWrite   OnWrite
	logger    *slog.Logger
	// mu serialises this process's writes so they do not burn conflict
	// retries on each other; the CAS is what makes writes safe.
	mu sync.Mutex
}

// NewConfigMap returns a Store backed by the named ConfigMap. The ConfigMap
// must exist (the chart pre-creates it, so tenant-api needs only get and
// update on that one object); a missing one is an error.
func NewConfigMap(client kubernetes.Interface, namespace, name string, onWrite OnWrite) (Store, error) {
	ctx, cancel := context.WithTimeout(context.Background(), k8sCallTimeout)
	defer cancel()
	if _, err := client.CoreV1().ConfigMaps(namespace).Get(ctx, name, metav1.GetOptions{}); err != nil {
		if apierrors.IsNotFound(err) {
			return nil, fmt.Errorf("statestore: ConfigMap %s/%s not found — the Helm chart must pre-create it", namespace, name)
		}
		return nil, fmt.Errorf("statestore: open ConfigMap %s/%s: %w", namespace, name, err)
	}
	return &configMapStore{client: client, namespace: namespace, name: name, onWrite: onWrite}, nil
}

func (s *configMapStore) Describe() string { return "configmap " + s.namespace + "/" + s.name }

func (s *configMapStore) log() *slog.Logger {
	if s.logger != nil {
		return s.logger
	}
	return slog.Default()
}

func (s *configMapStore) Get(ctx context.Context, key string) ([]byte, bool, error) {
	cctx, cancel := context.WithTimeout(ctx, k8sCallTimeout)
	defer cancel()
	cm, err := s.client.CoreV1().ConfigMaps(s.namespace).Get(cctx, s.name, metav1.GetOptions{})
	if err != nil {
		return nil, false, fmt.Errorf("%w: get %s: %v", ErrUnavailable, s.Describe(), err)
	}
	v, ok := cm.Data[key]
	if !ok {
		return nil, false, nil
	}
	return []byte(v), true, nil
}

func (s *configMapStore) Keys(ctx context.Context, prefix string) ([]string, error) {
	cctx, cancel := context.WithTimeout(ctx, k8sCallTimeout)
	defer cancel()
	cm, err := s.client.CoreV1().ConfigMaps(s.namespace).Get(cctx, s.name, metav1.GetOptions{})
	if err != nil {
		return nil, fmt.Errorf("%w: get %s: %v", ErrUnavailable, s.Describe(), err)
	}
	var keys []string
	for k := range cm.Data {
		if strings.HasPrefix(k, prefix) {
			keys = append(keys, k)
		}
	}
	sort.Strings(keys)
	return keys, nil
}

func (s *configMapStore) Mutate(ctx context.Context, key string, m Meta, t Transform) (bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	changed := false
	err := retry.RetryOnConflict(conflictRetry, func() error {
		changed = false // re-derived on every attempt
		cctx, cancel := context.WithTimeout(ctx, k8sCallTimeout)
		defer cancel()
		cm, err := s.client.CoreV1().ConfigMaps(s.namespace).Get(cctx, s.name, metav1.GetOptions{})
		if err != nil {
			return fmt.Errorf("%w: get %s: %v", ErrUnavailable, s.Describe(), err)
		}
		var cur []byte
		if v, ok := cm.Data[key]; ok {
			cur = []byte(v)
		}
		next, err := t(cur)
		if err != nil {
			return err
		}
		if next == nil || (cur != nil && bytes.Equal(next, cur)) {
			return nil
		}
		if cm.Data == nil {
			cm.Data = map[string]string{}
		}
		cm.Data[key] = string(next)
		if n := dataSize(cm.Data, cm.BinaryData); n > sizeBudget {
			return fmt.Errorf("%w: %s would hold %d bytes (budget %d)", ErrTooLarge, s.Describe(), n, sizeBudget)
		}
		if _, err := s.client.CoreV1().ConfigMaps(s.namespace).Update(cctx, cm, metav1.UpdateOptions{}); err != nil {
			if apierrors.IsConflict(err) {
				return err // retried by RetryOnConflict
			}
			return fmt.Errorf("%w: update %s: %v", ErrUnavailable, s.Describe(), err)
		}
		changed = true
		return nil
	})
	if err != nil {
		if apierrors.IsConflict(err) {
			return false, fmt.Errorf("%w (%s)", ErrConflict, s.Describe())
		}
		return false, err
	}
	if changed {
		audit(s.log(), s.Describe(), key, m)
		if s.onWrite != nil {
			s.onWrite(m.Entity)
		}
	}
	return changed, nil
}

func dataSize(data map[string]string, bin map[string][]byte) int {
	n := 0
	for k, v := range data {
		n += len(k) + len(v)
	}
	for k, v := range bin {
		n += len(k) + len(v)
	}
	return n
}

// audit records one state write. It replaces the git commit (author,
// message, trailers) these records used to get (#2856).
func audit(l *slog.Logger, store, key string, m Meta) {
	attrs := []any{"event", "state_write", "store", store, "key", key, "entity", m.Entity, "author", m.Author}
	if len(m.Notes) > 0 {
		attrs = append(attrs, "notes", m.Notes)
	}
	l.Info("tenant-api state written", attrs...)
}

// IsUnavailable reports whether err means the store could not be reached.
func IsUnavailable(err error) bool { return errors.Is(err, ErrUnavailable) }
