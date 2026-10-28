package statestore

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
)

// Snapshot is every record of a store as key → bytes, read once.
type Snapshot map[string][]byte

// ReadDirSnapshot reads every record of the directory store at dir without
// opening it as a Store: NewDir creates a missing directory, and a reader that
// only looks (`tenant-api state show`, ADR-036 §12) must not. A directory that
// does not exist, or is not a directory, cannot be read — it is not an empty
// store. Dot files are the store's own (the lock, writeAtomic's temp files),
// as for Keys.
func ReadDirSnapshot(dir string) (Snapshot, error) {
	if dir == "" {
		return nil, fmt.Errorf("statestore: state directory is empty")
	}
	fi, err := os.Stat(dir)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrUnavailable, err)
	}
	if !fi.IsDir() {
		return nil, fmt.Errorf("%w: %s is not a directory", ErrUnavailable, dir)
	}
	ents, err := os.ReadDir(dir)
	if err != nil {
		return nil, fmt.Errorf("%w: list %s: %v", ErrUnavailable, dir, err)
	}
	snap := Snapshot{}
	for _, e := range ents {
		n := e.Name()
		if !e.Type().IsRegular() || strings.HasPrefix(n, ".") {
			continue
		}
		b, err := os.ReadFile(filepath.Join(dir, n))
		if err != nil {
			return nil, fmt.Errorf("%w: read %s: %v", ErrUnavailable, filepath.Join(dir, n), err)
		}
		snap[n] = b
	}
	return snap, nil
}

// ReadConfigMapSnapshot reads every record of the ConfigMap store with one
// get, so the records come from one version of the object. A missing
// ConfigMap cannot be read — it is not an empty store.
func ReadConfigMapSnapshot(ctx context.Context, client kubernetes.Interface, namespace, name string) (Snapshot, error) {
	cctx, cancel := context.WithTimeout(ctx, k8sCallTimeout)
	defer cancel()
	cm, err := client.CoreV1().ConfigMaps(namespace).Get(cctx, name, metav1.GetOptions{})
	if err != nil {
		if apierrors.IsNotFound(err) {
			return nil, fmt.Errorf("%w: ConfigMap %s/%s not found", ErrUnavailable, namespace, name)
		}
		return nil, fmt.Errorf("%w: get ConfigMap %s/%s: %v", ErrUnavailable, namespace, name, err)
	}
	snap := Snapshot{}
	for k, v := range cm.Data {
		snap[k] = []byte(v)
	}
	return snap, nil
}
