package main

import (
	"flag"
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

// registerStateFlags registers --config-dir and the state-store flags on fs.
// The server and `tenant-api state restore` (#2884) both call it, so the
// two read the same settings from the same TA_* variables.
func registerStateFlags(fs *flag.FlagSet) *stateFlags {
	f := &stateFlags{}
	fs.StringVar(&f.ConfigDir, "config-dir", envOrDefault("TA_CONFIG_DIR", "/conf.d"),
		"Path to conf.d/ directory containing tenant YAML files")
	fs.StringVar(&f.Kind, "state-store", envOrDefault("TA_STATE_STORE", ""),
		"Where tenant-api keeps its own records: configmap (Kubernetes) or dir (single host). Required (#2856).")
	fs.StringVar(&f.ConfigMapName, "state-configmap", envOrDefault("TA_STATE_CONFIGMAP", "tenant-api-state"),
		"Name of the state ConfigMap (--state-store=configmap). The Helm chart pre-creates it.")
	fs.StringVar(&f.Namespace, "state-namespace", envOrDefault("TA_STATE_NAMESPACE", ""),
		"Namespace of the state ConfigMap. Empty uses the pod's own namespace.")
	fs.StringVar(&f.Dir, "state-dir", envOrDefault("TA_STATE_DIR", ""),
		"State directory (--state-store=dir). Must be outside --config-dir.")
	return f
}

// registerFederationStoreFlags registers the federation key and token-store
// flags on fs, shared like registerStateFlags: the restore reads live logs
// tokens from the store the server writes them to. TTL is the server's own.
func registerFederationStoreFlags(fs *flag.FlagSet) *federationFlags {
	f := &federationFlags{}
	fs.StringVar(&f.KeyPath, "federation-key", envOrDefault("TA_FEDERATION_KEY", ""),
		"Path to the RS256 private key (PEM) for signing federation tokens. Empty disables the federation endpoint.")
	fs.StringVar(&f.ConfigMapName, "federation-store", envOrDefault("TA_FEDERATION_STORE", "tenant-federation-store"),
		"Name of the ConfigMap holding the federation token record store (ADR-020 Posture B). The Helm chart pre-creates it.")
	fs.StringVar(&f.Namespace, "federation-namespace", envOrDefault("TA_FEDERATION_NAMESPACE", ""),
		"Namespace of the federation store ConfigMap. Empty uses the pod's own namespace.")
	return f
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
