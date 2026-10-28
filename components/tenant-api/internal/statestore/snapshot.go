package statestore

import (
	"context"
	"fmt"
	"os"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
)

// Snapshot is every record of a store as key → bytes, read once.
type Snapshot map[string][]byte

// ReadDirSnapshot reads every record of the directory store at dir the way
// the server reads them, without opening it as a Store: NewDir creates a
// missing directory, and a reader that only looks (`tenant-api state show`,
// ADR-036 §12) must not. A directory that does not exist, or is not a
// directory, cannot be read — it is not an empty store.
//
// The records are read through the server's own dirStore methods, so the
// two cannot disagree: each fixed key with Get (os.ReadFile, which follows a
// symlink — a ConfigMap volume's files are symlinks into `..data/`; only
// "does not exist" is an absent record, any other read error fails the
// whole snapshot), and the per-tenant subsets as Keys lists them, each then
// read with Get.
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
	s := &dirStore{dir: dir}
	ctx := context.Background()
	subsets, err := s.Keys(ctx, SubsetPrefix)
	if err != nil {
		return nil, err
	}
	snap := Snapshot{}
	for _, k := range append([]string{KeyGroups, KeyViews, KeyFederationPolicy, KeyAccountRegistry}, subsets...) {
		b, ok, err := s.Get(ctx, k)
		if err != nil {
			return nil, err
		}
		if ok {
			snap[k] = b
		}
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
