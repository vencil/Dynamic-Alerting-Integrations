package handler

import (
	"fmt"
	"log/slog"
	"path/filepath"
	"sync"

	"github.com/vencil/tenant-api/internal/boundedcall"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

// metadataRootReadGuards bounds the tenant-metadata readers' read of the
// conf.d root platform files (#2370) — the list's (loadAllTenants) and the
// one behind ScopeMetaFunc — with one boundedcall.Guard per conf.d
// directory. Not GET's guard (defaultRootReadGuard): a stuck read here must
// not fail GET /tenants/{id}, and the reverse. Per directory so that a read
// stuck under one conf.d never touches another's (tests run many in
// parallel; a test may store a guard with a short Timeout for its own
// directory before the first read). The key is filepath.Clean(configDir), so
// two spellings of one directory share a guard.
var metadataRootReadGuards sync.Map // filepath.Clean(configDir) → *boundedcall.Guard

func metadataRootReadGuard(configDir string) *boundedcall.Guard {
	g, _ := metadataRootReadGuards.LoadOrStore(filepath.Clean(configDir), &boundedcall.Guard{})
	return g.(*boundedcall.Guard)
}

// loadPlatformMetadata reads configDir's root platform layer for tenant
// metadata — every root platform file's `tenants.<id>._metadata`, read
// through cfg.LoadRootPlatformChecked — under configDir's guard.
//
// It returns an error when that layer could not be read in full: the root
// could not be walked, a root platform file could not be read, or the read
// did not return in time (a FIFO named `_*.yaml`, a hung mount). A file that
// was read but that the exporter's decode rejects is not an error: it adds
// no metadata, on /metrics as here.
//
// ⚠️ A read that timed out stays blocked until the file returns (for a FIFO:
// until something opens it for writing; removing the file does not end a
// read already in progress). Until then every call here fails at once with
// boundedcall.ErrStuck instead of starting another read; the first call
// after the blocked read returns reads the files again.
func loadPlatformMetadata(configDir string) (cfg.RootPlatform, error) {
	type result struct {
		root cfg.RootPlatform
		err  error
	}
	r, err := boundedcall.Do(metadataRootReadGuard(configDir), func() result {
		root, err := cfg.LoadRootPlatformChecked(configDir)
		return result{root, err}
	})
	if err != nil {
		return cfg.RootPlatform{}, fmt.Errorf("reading the conf.d root platform files: %w", err)
	}
	return r.root, r.err
}

// platformMetadataOrNone is loadPlatformMetadata for the metadata readers:
// when the root platform layer cannot be read it logs an ERROR naming the
// reader and returns the empty layer and known=false, instead of failing.
// What the empty layer means is the reader's call: the write plane answers
// from the tenant file's own `_metadata` alone — what tenant-api read before
// #2370 — while the list marks every healthy row's metadata unknown
// (loadAllTenants).
func platformMetadataOrNone(configDir, reader string) (root cfg.RootPlatform, known bool) {
	root, err := loadPlatformMetadata(configDir)
	if err != nil {
		slog.Error("root platform files unreadable: tenant metadata incomplete",
			"reader", reader, "config_dir", configDir, "error", err)
		return cfg.RootPlatform{}, false
	}
	return root, true
}
