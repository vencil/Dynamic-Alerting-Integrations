// Package rbac implements RBAC loading and permission checking for tenant-api.
//
// Design:
//   - _rbac.yaml is loaded on startup and hot-reloaded on change (SHA-256 detection).
//   - The parsed config is stored in an atomic.Value for lock-free reads.
//   - Group → Tenant mapping supports wildcards ("*") and prefix patterns ("db-a-*").
//   - Permissions: "read" | "write" | "admin".
//
// _rbac.yaml format:
//
//	groups:
//	  - name: platform-admins
//	    tenants: ["*"]
//	    permissions: [read, write, admin]
//	  - name: db-operators
//	    tenants: ["db-a-*", "db-b-*"]
//	    permissions: [read, write]
package rbac

import (
	"crypto/sha256"
	"fmt"
	"log"
	"os"
	"strings"
	"sync/atomic"
	"time"

	"gopkg.in/yaml.v3"
)

// Permission represents a single permission level.
type Permission string

const (
	PermRead  Permission = "read"
	PermWrite Permission = "write"
	PermAdmin Permission = "admin"
)

// GroupRule maps an IdP group to a set of tenants and permissions.
type GroupRule struct {
	Name        string       `yaml:"name"`
	Tenants     []string     `yaml:"tenants"`     // tenant IDs or patterns ("*", "db-a-*")
	Permissions []Permission `yaml:"permissions"` // [read, write, admin]
}

// RBACConfig is the parsed _rbac.yaml structure.
type RBACConfig struct {
	Groups []GroupRule `yaml:"groups"`
}

// Manager holds the hot-reloadable RBAC config.
type Manager struct {
	path     string
	value    atomic.Value // stores *RBACConfig
	lastHash string
}

// NewManager creates a Manager and loads the RBAC config from path.
// If path is empty or the file does not exist, the manager starts in
// open mode (all authenticated users have read access, no write).
func NewManager(path string) (*Manager, error) {
	m := &Manager{path: path}
	if path == "" {
		log.Println("RBAC: no _rbac.yaml configured, running in open-read mode")
		m.value.Store(&RBACConfig{})
		return m, nil
	}
	if err := m.load(); err != nil {
		return nil, fmt.Errorf("rbac: initial load failed: %w", err)
	}
	return m, nil
}

// load reads and parses the RBAC config file, storing the result atomically.
func (m *Manager) load() error {
	data, err := os.ReadFile(m.path)
	if err != nil {
		if os.IsNotExist(err) {
			log.Printf("RBAC: %s not found, running in open-read mode", m.path)
			m.value.Store(&RBACConfig{})
			return nil
		}
		return fmt.Errorf("read %s: %w", m.path, err)
	}

	hash := fmt.Sprintf("%x", sha256.Sum256(data))
	if hash == m.lastHash {
		return nil // unchanged
	}

	var cfg RBACConfig
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return fmt.Errorf("parse %s: %w", m.path, err)
	}

	m.value.Store(&cfg)
	m.lastHash = hash
	log.Printf("RBAC: loaded %d group rules from %s", len(cfg.Groups), m.path)
	return nil
}

// WatchLoop periodically checks for changes to the RBAC config file.
// Call in a goroutine; close stopCh to exit.
func (m *Manager) WatchLoop(interval time.Duration, stopCh <-chan struct{}) {
	if m.path == "" {
		return
	}
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-stopCh:
			return
		case <-ticker.C:
			if err := m.load(); err != nil {
				log.Printf("WARN: rbac reload failed: %v", err)
			}
		}
	}
}

// Get returns the current RBAC config snapshot (lock-free).
func (m *Manager) Get() *RBACConfig {
	v := m.value.Load()
	if v == nil {
		return &RBACConfig{}
	}
	return v.(*RBACConfig)
}

// HasPermission checks whether any of the provided IdP groups grants the
// specified permission for the given tenantID.
//
// Permission hierarchy: admin ⊇ write ⊇ read.
// An "admin" grant satisfies "write" and "read" checks.
func (m *Manager) HasPermission(idpGroups []string, tenantID string, want Permission) bool {
	cfg := m.Get()
	if len(cfg.Groups) == 0 {
		// Open mode — authenticated users have read access only
		return want == PermRead
	}

	groupSet := make(map[string]bool, len(idpGroups))
	for _, g := range idpGroups {
		groupSet[g] = true
	}

	for _, rule := range cfg.Groups {
		if !groupSet[rule.Name] {
			continue
		}
		if !tenantMatches(rule.Tenants, tenantID) {
			continue
		}
		for _, p := range rule.Permissions {
			if permCovers(p, want) {
				return true
			}
		}
	}
	return false
}

// tenantMatches reports whether tenantID matches any pattern in the list.
// Patterns: "*" (wildcard), "prefix-*" (prefix), or exact match.
func tenantMatches(patterns []string, tenantID string) bool {
	for _, pat := range patterns {
		if pat == "*" {
			return true
		}
		if strings.HasSuffix(pat, "*") {
			prefix := strings.TrimSuffix(pat, "*")
			if strings.HasPrefix(tenantID, prefix) {
				return true
			}
			continue
		}
		if pat == tenantID {
			return true
		}
	}
	return false
}

// permCovers reports whether grant satisfies want (admin covers write and read).
func permCovers(grant, want Permission) bool {
	switch want {
	case PermRead:
		return grant == PermRead || grant == PermWrite || grant == PermAdmin
	case PermWrite:
		return grant == PermWrite || grant == PermAdmin
	case PermAdmin:
		return grant == PermAdmin
	}
	return false
}
