package main

import (
	"fmt"
	"path/filepath"
	"strings"

	"github.com/vencil/tenant-api/internal/statestore"
)

// stateFlags are the CLI-flag values consumed by wireStateStore (#2856).
type stateFlags struct {
	Kind          string // --state-store: "configmap" or "dir"
	ConfigMapName string // --state-configmap
	Namespace     string // --state-namespace (empty → pod's own namespace)
	Dir           string // --state-dir
	ConfigDir     string // --config-dir: the state dir must lie outside it
}

// wireStateStore builds the store for tenant-api's own records (groups,
// views, the federation whitelist and subsets, the AccountID registry).
//
// There is deliberately no default: the old default — files in the conf.d
// git tree, committed and never pushed — is what lost these records on
// every emptyDir restart, and a silent fallback to any local directory
// would bring that back. The Helm chart passes `configmap`; try-local's
// docker compose passes `dir` with a volume outside conf.d.
func wireStateStore(f stateFlags, onWrite statestore.OnWrite) (statestore.Store, error) {
	switch f.Kind {
	case "configmap":
		client, err := buildInClusterClientset()
		if err != nil {
			return nil, fmt.Errorf("state store: %w", err)
		}
		ns := f.Namespace
		if ns == "" {
			ns, err = inClusterNamespace()
			if err != nil {
				return nil, fmt.Errorf("state store: resolve namespace (or set --state-namespace): %w", err)
			}
		}
		return statestore.NewConfigMap(client, ns, f.ConfigMapName, onWrite)
	case "dir":
		if f.Dir == "" {
			return nil, fmt.Errorf("state store: --state-store=dir needs --state-dir")
		}
		if within(f.Dir, f.ConfigDir) {
			// Inside conf.d the records would be read as tenant config and
			// swept into git commits — the arrangement #2856 removed.
			return nil, fmt.Errorf("state store: --state-dir %q must lie outside --config-dir %q", f.Dir, f.ConfigDir)
		}
		return statestore.NewDir(f.Dir, onWrite)
	case "":
		return nil, fmt.Errorf("state store: --state-store is required (configmap for Kubernetes, dir for a single host); " +
			"groups, views, federation settings and the AccountID registry no longer live in conf.d (#2856)")
	default:
		return nil, fmt.Errorf("state store: --state-store must be configmap or dir, got %q", f.Kind)
	}
}

// within reports whether path is dir or lies under it (lexically, after
// making both absolute). An empty dir contains nothing.
func within(path, dir string) bool {
	if dir == "" {
		return false
	}
	p, err1 := filepath.Abs(path)
	d, err2 := filepath.Abs(dir)
	if err1 != nil || err2 != nil {
		return false
	}
	rel, err := filepath.Rel(d, p)
	return err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator))
}
