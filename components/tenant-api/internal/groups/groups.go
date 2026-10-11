// Package groups manages custom tenant groups stored in _groups.yaml.
//
// Schema (_groups.yaml):
//
//	# Custom tenant groups — managed via tenant-api or manual editing.
//	# Each group has a unique ID, display label, optional description,
//	# filter criteria, and a static member list.
//	#
//	# Groups are read by tenant-api for UI filtering and batch operations.
//	# The threshold-exporter loader skips _ prefixed files, so this file
//	# does not affect metric production.
//	groups:
//	  production-dba:
//	    label: "Production DBA"
//	    description: "All production database tenants managed by DBA team"
//	    filters:
//	      environment: "production"
//	      domain: "finance"
//	    members:
//	      - db-a
//	      - db-b
//	  staging-all:
//	    label: "All Staging"
//	    members:
//	      - staging-pg-01
//	      - staging-redis-01
//
// Concurrency: reads are lock-free (atomic.Value). Writes are serialized
// through gitops.Writer's sync.Mutex + HEAD conflict detection.
package groups

import (
	"fmt"
	"log/slog"
	"sort"

	"github.com/vencil/tenant-api/internal/configwatcher"
	"github.com/vencil/tenant-api/internal/statestore"
	"gopkg.in/yaml.v3"
)

// Group represents a single tenant group definition.
type Group struct {
	Label       string            `yaml:"label"       json:"label"`
	Description string            `yaml:"description" json:"description,omitempty"`
	Filters     map[string]string `yaml:"filters"     json:"filters,omitempty"`
	Members     []string          `yaml:"members"     json:"members"`
}

// GroupsConfig is the top-level _groups.yaml structure.
type GroupsConfig struct {
	Groups map[string]Group `yaml:"groups" json:"groups"`
}

// Manager handles hot-reloadable group config. The hot-reload
// machinery (atomic.Value + SHA-256 dedup + Reload) lives in the
// embedded configwatcher.Watcher; this type only adds the
// domain-specific list/get methods.
type Manager struct {
	*configwatcher.Watcher[GroupsConfig]
}

// NewManager creates a Manager that reads the groups record from the state
// store (#2856: no longer conf.d/_groups.yaml). A missing record is an empty
// config.
func NewManager(s statestore.Store) *Manager {
	desc := s.Describe() + " " + statestore.KeyGroups
	w, err := configwatcher.NewWithReader(desc, "groups", statestore.ReadFunc(s, statestore.KeyGroups), ParseConfig, emptyConfig)
	if err != nil {
		slog.Warn("groups: initial load failed", "error", err)
	}
	return &Manager{Watcher: w}
}

// NewDirManager is NewManager over a directory store at dir — tests and
// tools that hold the record as dir/_groups.yaml.
func NewDirManager(dir string) *Manager {
	s, err := statestore.NewDir(dir, nil)
	if err != nil {
		slog.Warn("groups: state directory", "error", err)
		return &Manager{Watcher: configwatcher.NewForTest("groups", emptyConfig())}
	}
	return NewManager(s)
}

// emptyConfig returns the empty fallback config used when the file
// is missing or initial load fails.
func emptyConfig() *GroupsConfig {
	return &GroupsConfig{Groups: make(map[string]Group)}
}

// ListGroups returns all groups sorted by ID.
func (m *Manager) ListGroups() []GroupWithID {
	cfg := m.Get()
	result := make([]GroupWithID, 0, len(cfg.Groups))
	for id, g := range cfg.Groups {
		result = append(result, GroupWithID{ID: id, Group: g})
	}
	sort.Slice(result, func(i, j int) bool {
		return result[i].ID < result[j].ID
	})
	return result
}

// GetGroup returns a single group by ID, or false if not found.
func (m *Manager) GetGroup(id string) (Group, bool) {
	cfg := m.Get()
	g, ok := cfg.Groups[id]
	return g, ok
}

// GroupWithID pairs a group with its ID for list responses.
type GroupWithID struct {
	ID string `json:"id"`
	Group
}

// ValidateGroupID checks that a group ID is safe.
func ValidateGroupID(id string) error {
	if id == "" {
		return fmt.Errorf("group ID must not be empty")
	}
	if len(id) > 128 {
		return fmt.Errorf("group ID must not exceed 128 characters")
	}
	for _, c := range id {
		if !isGroupIDChar(c) {
			return fmt.Errorf("group ID contains invalid character: %c (allowed: a-z, 0-9, -, _)", c)
		}
	}
	return nil
}

func isGroupIDChar(c rune) bool {
	return (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-' || c == '_'
}

// MarshalConfig serializes the groups config back to YAML.
func MarshalConfig(cfg *GroupsConfig) ([]byte, error) {
	return yaml.Marshal(cfg)
}

// ParseConfig parses a _groups.yaml document.
func ParseConfig(data []byte) (*GroupsConfig, error) {
	var cfg GroupsConfig
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return nil, err
	}
	if cfg.Groups == nil {
		cfg.Groups = make(map[string]Group)
	}
	return &cfg, nil
}
