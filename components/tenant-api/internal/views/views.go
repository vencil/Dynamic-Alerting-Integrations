// Package views manages saved filter views stored in _views.yaml.
//
// v2.5.0 Phase C: Saved Views allow users to persist named filter
// combinations in the tenant-manager UI. Each view stores filter
// criteria (environment, domain, tier, tags, search text) that can
// be applied with one click.
//
// Schema (_views.yaml):
//
//	views:
//	  prod-finance:
//	    label: "Production Finance"
//	    description: "All production tenants in finance domain"
//	    created_by: "admin@example.com"
//	    filters:
//	      environment: "production"
//	      domain: "finance"
//	  critical-silent:
//	    label: "Critical + Silent"
//	    filters:
//	      tier: "tier-1"
//	      operational_mode: "silent"
//
// Concurrency: reads are lock-free (atomic.Value). Writes are serialized
// through gitops.Writer's sync.Mutex + HEAD conflict detection.
package views

import (
	"crypto/sha256"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"sort"
	"sync/atomic"

	"gopkg.in/yaml.v3"
)

// View represents a single saved filter view.
type View struct {
	Label       string            `yaml:"label"       json:"label"`
	Description string            `yaml:"description" json:"description,omitempty"`
	CreatedBy   string            `yaml:"created_by"  json:"created_by,omitempty"`
	Filters     map[string]string `yaml:"filters"     json:"filters"`
}

// ViewsConfig is the top-level _views.yaml structure.
type ViewsConfig struct {
	Views map[string]View `yaml:"views" json:"views"`
}

// Manager handles hot-reloadable views config.
type Manager struct {
	configDir string
	value     atomic.Value // stores *ViewsConfig
	lastHash  string
}

// NewManager creates a Manager that reads _views.yaml from configDir.
func NewManager(configDir string) *Manager {
	m := &Manager{configDir: configDir}
	if err := m.load(); err != nil {
		log.Printf("WARN: views: initial load: %v", err)
	}
	if m.value.Load() == nil {
		m.value.Store(&ViewsConfig{Views: make(map[string]View)})
	}
	return m
}

// Get returns the current views config snapshot (lock-free).
func (m *Manager) Get() *ViewsConfig {
	v := m.value.Load()
	if v == nil {
		return &ViewsConfig{Views: make(map[string]View)}
	}
	return v.(*ViewsConfig)
}

// Reload re-reads the _views.yaml file. Called after writes.
func (m *Manager) Reload() error {
	m.lastHash = ""
	return m.load()
}

func (m *Manager) load() error {
	path := m.filePath()
	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			m.value.Store(&ViewsConfig{Views: make(map[string]View)})
			return nil
		}
		return fmt.Errorf("read %s: %w", path, err)
	}

	hash := fmt.Sprintf("%x", sha256.Sum256(data))
	if hash == m.lastHash {
		return nil
	}

	var cfg ViewsConfig
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return fmt.Errorf("parse %s: %w", path, err)
	}
	if cfg.Views == nil {
		cfg.Views = make(map[string]View)
	}

	m.value.Store(&cfg)
	m.lastHash = hash
	log.Printf("views: loaded %d saved views from %s", len(cfg.Views), path)
	return nil
}

func (m *Manager) filePath() string {
	return filepath.Join(m.configDir, "_views.yaml")
}

// ListViews returns all views sorted by ID.
func (m *Manager) ListViews() []ViewWithID {
	cfg := m.Get()
	result := make([]ViewWithID, 0, len(cfg.Views))
	for id, v := range cfg.Views {
		result = append(result, ViewWithID{ID: id, View: v})
	}
	sort.Slice(result, func(i, j int) bool {
		return result[i].ID < result[j].ID
	})
	return result
}

// GetView returns a single view by ID, or false if not found.
func (m *Manager) GetView(id string) (View, bool) {
	cfg := m.Get()
	v, ok := cfg.Views[id]
	return v, ok
}

// ViewWithID pairs a view with its ID for list responses.
type ViewWithID struct {
	ID string `json:"id"`
	View
}

// ValidateViewID checks that a view ID is safe.
func ValidateViewID(id string) error {
	if id == "" {
		return fmt.Errorf("view ID must not be empty")
	}
	if len(id) > 128 {
		return fmt.Errorf("view ID must not exceed 128 characters")
	}
	for _, c := range id {
		if !isViewIDChar(c) {
			return fmt.Errorf("view ID contains invalid character: %c (allowed: a-z, 0-9, -, _)", c)
		}
	}
	return nil
}

func isViewIDChar(c rune) bool {
	return (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-' || c == '_'
}

// MarshalConfig serializes the views config back to YAML.
func MarshalConfig(cfg *ViewsConfig) ([]byte, error) {
	return yaml.Marshal(cfg)
}

// ParseConfig parses a _views.yaml document.
func ParseConfig(data []byte) (*ViewsConfig, error) {
	var cfg ViewsConfig
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return nil, err
	}
	if cfg.Views == nil {
		cfg.Views = make(map[string]View)
	}
	return &cfg, nil
}
