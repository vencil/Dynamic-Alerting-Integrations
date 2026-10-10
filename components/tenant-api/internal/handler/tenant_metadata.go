package handler

import (
	"fmt"

	"github.com/vencil/tenant-api/internal/boundedcall"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

// metadataRootReadGuard bounds the tenant-metadata readers' read of the
// conf.d root platform files (#2370): the list's (loadAllTenants) and the
// one behind ScopeMetaFunc. Its own guard, not GET's (defaultRootReadGuard):
// a stuck read here must not fail GET /tenants/{id}, and the reverse.
var metadataRootReadGuard = &boundedcall.Guard{}

// loadPlatformMetadata reads configDir's root platform layer for tenant
// metadata — every root platform file's `tenants.<id>._metadata`, read
// through cfg.LoadRootPlatformChecked — under metadataRootReadGuard.
//
// It returns an error when that layer cannot be known: the root could not
// be walked, a root platform file could not be read, or the read did not
// return in time (a FIFO named `_*.yaml`, a hung mount). A file that was
// read but that the exporter's decode rejects is not an error: it adds no
// metadata, on /metrics as here.
func loadPlatformMetadata(configDir string) (cfg.RootPlatform, error) {
	type result struct {
		root cfg.RootPlatform
		err  error
	}
	r, err := boundedcall.Do(metadataRootReadGuard, func() result {
		root, err := cfg.LoadRootPlatformChecked(configDir)
		return result{root, err}
	})
	if err != nil {
		return cfg.RootPlatform{}, fmt.Errorf("reading the conf.d root platform files: %w", err)
	}
	return r.root, r.err
}
