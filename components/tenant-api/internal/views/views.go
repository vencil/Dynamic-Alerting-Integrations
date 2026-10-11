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
	"fmt"
	"log/slog"
	"sort"

	"github.com/vencil/tenant-api/internal/configwatcher"
	"github.com/vencil/tenant-api/internal/statestore"
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

// Manager handles hot-reloadable views config. The hot-reload
// machinery lives in the embedded configwatcher.Watcher;
// Get / Reload / WatchLoop promote through. This type adds only
// the list/get methods.
type Manager struct {
	*configwatcher.Watcher[ViewsConfig]
}

// NewManager creates a Manager that reads the views record from the state
// store (#2856: no longer conf.d/_views.yaml).
func NewManager(s statestore.Store) *Manager {
	desc := s.Describe() + " " + statestore.KeyViews
	w, err := configwatcher.NewWithReader(desc, "views", statestore.ReadFunc(s, statestore.KeyViews), ParseConfig, emptyConfig)
	if err != nil {
		slog.Warn("views: initial load failed", "error", err)
	}
	return &Manager{Watcher: w}
}

// NewDirManager is NewManager over a directory store at dir — tests and
// tools that hold the record as dir/_views.yaml.
func NewDirManager(dir string) *Manager {
	s, err := statestore.NewDir(dir, nil)
	if err != nil {
		slog.Warn("views: state directory", "error", err)
		return &Manager{Watcher: configwatcher.NewForTest("views", emptyConfig())}
	}
	return NewManager(s)
}

// emptyConfig returns the empty fallback config used when the file
// is missing or initial load fails.
func emptyConfig() *ViewsConfig {
	return &ViewsConfig{Views: make(map[string]View)}
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
