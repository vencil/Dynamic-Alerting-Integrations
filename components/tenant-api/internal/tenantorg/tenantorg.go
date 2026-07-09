// Package tenantorg maps tenants to the organizations they belong to
// (ADR-027 / LD-6 P4). It backs the org-scope authorization axis: an
// org-scoped RBAC rule grants a tenant to a caller only when the caller's
// verified org claim is one of the tenant's organizations.
//
// SSOT: the admin-only _tenant_orgs.yaml file under conf.d. Like the other
// _-prefixed platform state files (_rbac.yaml / _domain_policy.yaml /
// _account_registry.yaml) the leading underscore makes every tenant-file
// scanner skip it — the threshold-exporter loader, the federation orphan
// detector, and the tenant-api list scanner all skip _-prefixed names — so
// it is never mistaken for a tenant. There is NO write API: the org→tenant
// mapping is administered out-of-band (a human commits the file); tenant-api
// only reads it.
//
// Schema (_tenant_orgs.yaml):
//
//	tenant_orgs:
//	  db-a: [ORG-4821]                 # 1:N — a tenant may belong to many orgs
//	  db-b: [ORG-4821, ORG-1900]
//	  db-c: []                          # created-but-unassigned (labeled, empty)
//
// Parsing is STRICT (yaml KnownFields), like rbac.go and UNLIKE the lenient
// policy.go: this file defines an authorization boundary, so a typo'd top-level
// key (`tenant_org:` for `tenant_orgs:`) must be a load error, not a silently
// empty map that would make every org-scoped rule behave as if no tenant has
// any org. An empty or comment-only file is the one benign empty case and
// decodes to the empty config (mirroring the rbac io.EOF special-case).
//
// Concurrency: reads are lock-free (atomic.Value in the embedded
// configwatcher.Watcher); the file hot-reloads on SHA-256 change.
package tenantorg

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"path/filepath"

	"github.com/vencil/tenant-api/internal/configwatcher"
	"gopkg.in/yaml.v3"
)

// Config is the parsed _tenant_orgs.yaml structure: tenant ID → org list.
type Config struct {
	TenantOrgs map[string][]string `yaml:"tenant_orgs"`
}

// Manager holds the hot-reloadable tenant→orgs mapping. The hot-reload
// machinery (atomic.Value + SHA-256 dedup + WatchLoop) lives in the embedded
// configwatcher.Watcher; this type only adds the OrgsForTenant lookup.
type Manager struct {
	*configwatcher.Watcher[Config]
}

// NewManager creates a Manager that reads _tenant_orgs.yaml from configDir.
// An initial-load error (a malformed / typo'd file — NOT a missing or empty
// one, which decode to the empty config) is returned so main.go can treat it
// as fatal: an unparseable org-boundary file is not safe to serve, exactly as
// with _rbac.yaml. A missing file leaves the manager in the empty-config state
// (no tenant has any org), which is the correct default for a deployment that
// does not use org-scope.
func NewManager(configDir string) (*Manager, error) {
	path := filepath.Join(configDir, "_tenant_orgs.yaml")
	w, err := configwatcher.New(path, "tenantorg", parseConfig, emptyConfig)
	if err != nil {
		return &Manager{Watcher: w}, fmt.Errorf("tenantorg: initial load failed: %w", err)
	}
	return &Manager{Watcher: w}, nil
}

// NewForTest returns a Manager pre-populated with cfg and no file path.
// WatchLoop becomes a no-op; only OrgsForTenant is exercised. Intended for
// unit tests driving org-scope logic against an in-memory snapshot.
func NewForTest(cfg *Config) *Manager {
	return &Manager{Watcher: configwatcher.NewForTest("tenantorg", cfg)}
}

// OrgsForTenant returns the organizations tenant tenantID belongs to.
//
// known reports whether the tenant appears in the mapping at all:
//   - known=false: the tenant is not in _tenant_orgs.yaml (orgs is nil).
//   - known=true, len(orgs)==0: the tenant IS listed but with an empty list
//     (created-but-unassigned).
//
// For the org-scope evaluation the two "no orgs" states behave identically
// (an org-scoped rule denies both, shadow-lenient), but they are reported
// separately so P6's reverse lookup can distinguish "never onboarded" from
// "onboarded, no org yet". A nil receiver (a Deps built without wiring the
// manager, e.g. a handler test literal) reports (nil, false) rather than
// panicking, so callers do not each need a nil guard.
func (m *Manager) OrgsForTenant(tenantID string) (orgs []string, known bool) {
	if m == nil {
		return nil, false
	}
	cfg := m.Get()
	orgs, known = cfg.TenantOrgs[tenantID]
	return orgs, known
}

func emptyConfig() *Config {
	return &Config{TenantOrgs: make(map[string][]string)}
}

// parseConfig parses _tenant_orgs.yaml STRICTLY (yaml.Decoder.KnownFields):
// an unknown top-level or nested key is a load error, never a silently-ignored
// key — a typo in this authorization-boundary file must fail loud. An empty or
// comment-only file decodes to the empty config (the strict decoder surfaces
// io.EOF where a lenient Unmarshal would return a zero struct).
func parseConfig(data []byte) (*Config, error) {
	var cfg Config
	dec := yaml.NewDecoder(bytes.NewReader(data))
	dec.KnownFields(true)
	if err := dec.Decode(&cfg); err != nil {
		if errors.Is(err, io.EOF) {
			return emptyConfig(), nil
		}
		return nil, err
	}
	if cfg.TenantOrgs == nil {
		cfg.TenantOrgs = make(map[string][]string)
	}
	return &cfg, nil
}
