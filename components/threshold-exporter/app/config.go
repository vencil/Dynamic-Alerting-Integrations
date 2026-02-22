package main

import (
	"fmt"
	"log"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"gopkg.in/yaml.v3"
)

// StateFilter defines a state-based monitoring filter (Scenario C).
// Each filter maps to kube_pod_container_status_waiting_reason or similar K8s state metrics.
// Per-tenant enable/disable is controlled via _state_<filter_name> in the tenants map.
type StateFilter struct {
	Reasons      []string `yaml:"reasons"`       // K8s waiting/terminated reasons to match
	Severity     string   `yaml:"severity"`      // Alert severity (default: "warning")
	DefaultState string   `yaml:"default_state"` // "enable" (default) or "disable" — 控制未設定 _state_ 時的預設行為
}

// ResolvedStateFilter is the resolved state for one tenant+filter pair.
// Exposed as user_state_filter{tenant, filter, severity} = 1.0 (flag gauge).
// Disabled filters produce no metric (same "absent = disabled" pattern as numeric thresholds).
type ResolvedStateFilter struct {
	Tenant     string
	FilterName string
	Severity   string
}

// ThresholdConfig represents the YAML config structure.
//
// Example config:
//
//	defaults:
//	  mysql_connections: 80
//	  mysql_cpu: 80
//	state_filters:
//	  container_crashloop:
//	    reasons: ["CrashLoopBackOff"]
//	    severity: "critical"
//	  container_imagepull:
//	    reasons: ["ImagePullBackOff", "InvalidImageName"]
//	    severity: "warning"
//	tenants:
//	  db-a:
//	    mysql_connections: "70"
//	    # all state filters enabled (default)
//	  db-b:
//	    mysql_connections: "disable"
//	    _state_container_crashloop: "disable"  # disable crashloop monitoring
type ThresholdConfig struct {
	Defaults     map[string]float64            `yaml:"defaults"`
	StateFilters map[string]StateFilter        `yaml:"state_filters"`
	Tenants      map[string]map[string]string  `yaml:"tenants"`
}

// ResolvedThreshold is the final resolved state for one tenant+metric pair.
type ResolvedThreshold struct {
	Tenant    string
	Metric    string
	Value     float64
	Severity  string
	Component string
}

// Resolve applies three-state logic:
//   - custom value → use it
//   - omitted      → use default
//   - "disable"    → skip (no metric exposed)
//
// Returns the list of thresholds to expose as Prometheus metrics.
func (c *ThresholdConfig) Resolve() []ResolvedThreshold {
	var result []ResolvedThreshold

	for tenant, overrides := range c.Tenants {
		for metricKey, defaultValue := range c.Defaults {
			// Skip _state_ prefixed keys — handled by ResolveStateFilters()
			if strings.HasPrefix(metricKey, "_state_") {
				continue
			}

			// Parse metric key: "mysql_connections" → component="mysql", metric="connections"
			component, metric := parseMetricKey(metricKey)
			severity := "warning" // default severity

			// Check tenant override (skip _state_ overrides)
			if override, exists := overrides[metricKey]; exists {
				lower := strings.TrimSpace(strings.ToLower(override))

				// State 3: disable
				if isDisabled(lower) {
					continue
				}

				// Check if it has severity suffix: "70:critical"
				parts := strings.SplitN(override, ":", 2)
				valueStr := strings.TrimSpace(parts[0])
				if len(parts) == 2 {
					severity = strings.TrimSpace(parts[1])
				}

				// State 1: custom value
				if v, err := strconv.ParseFloat(valueStr, 64); err == nil {
					result = append(result, ResolvedThreshold{
						Tenant:    tenant,
						Metric:    metric,
						Value:     v,
						Severity:  severity,
						Component: component,
					})
					continue
				}

				// Unknown value — log warning, use default
				log.Printf("WARN: unknown value %q for tenant=%s metric=%s, using default", override, tenant, metricKey)
			}

			// State 2: use default
			result = append(result, ResolvedThreshold{
				Tenant:    tenant,
				Metric:    metric,
				Value:     defaultValue,
				Severity:  severity,
				Component: component,
			})
		}
	}

	return result
}

// ResolveStateFilters resolves state-based monitoring filters for all tenants.
// For each state filter defined in config, each tenant gets an enabled flag
// unless explicitly disabled via _state_<filter_name>: "disable" in tenants map.
//
// Returns the list of enabled state filters to expose as Prometheus metrics.
func (c *ThresholdConfig) ResolveStateFilters() []ResolvedStateFilter {
	var result []ResolvedStateFilter

	if len(c.StateFilters) == 0 {
		return result
	}

	for filterName, filter := range c.StateFilters {
		severity := filter.Severity
		if severity == "" {
			severity = "warning"
		}

		// default_state: "disable" → 預設關閉，需明確 enable
		// default_state: "" 或 "enable" → 預設開啟 (向後相容)
		defaultEnabled := !isDisabled(strings.TrimSpace(strings.ToLower(filter.DefaultState)))

		for tenant, overrides := range c.Tenants {
			stateKey := "_state_" + filterName
			if override, exists := overrides[stateKey]; exists {
				lower := strings.TrimSpace(strings.ToLower(override))
				if isDisabled(lower) {
					continue // 明確停用
				}
				// 明確啟用 (任何非 disable 的值，如 "enable")
			} else if !defaultEnabled {
				continue // 無覆寫 + 預設關閉 = 跳過
			}

			result = append(result, ResolvedStateFilter{
				Tenant:     tenant,
				FilterName: filterName,
				Severity:   severity,
			})
		}
	}

	return result
}

// isDisabled checks if a value string means "disabled".
func isDisabled(lower string) bool {
	return lower == "disable" || lower == "disabled" || lower == "off" || lower == "false"
}

// parseMetricKey splits "mysql_connections" into ("mysql", "connections").
// If no underscore, component defaults to "default".
func parseMetricKey(key string) (component, metric string) {
	idx := strings.Index(key, "_")
	if idx < 0 {
		return "default", key
	}
	return key[:idx], key[idx+1:]
}

// ConfigManager handles loading and hot-reloading the config file.
type ConfigManager struct {
	path       string
	mu         sync.RWMutex
	config     *ThresholdConfig
	loaded     bool
	lastReload time.Time
	lastMod    time.Time
}

func NewConfigManager(path string) *ConfigManager {
	return &ConfigManager{path: path}
}

func (m *ConfigManager) Load() error {
	data, err := os.ReadFile(m.path)
	if err != nil {
		return fmt.Errorf("read config %s: %w", m.path, err)
	}

	var cfg ThresholdConfig
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return fmt.Errorf("parse config %s: %w", m.path, err)
	}

	// Validate
	if cfg.Defaults == nil {
		cfg.Defaults = make(map[string]float64)
	}
	if cfg.Tenants == nil {
		cfg.Tenants = make(map[string]map[string]string)
	}
	if cfg.StateFilters == nil {
		cfg.StateFilters = make(map[string]StateFilter)
	}

	m.mu.Lock()
	m.config = &cfg
	m.loaded = true
	m.lastReload = time.Now()
	m.mu.Unlock()

	resolved := cfg.Resolve()
	resolvedState := cfg.ResolveStateFilters()
	log.Printf("Config loaded: %d defaults, %d state_filters, %d tenants, %d resolved thresholds, %d resolved state filters",
		len(cfg.Defaults), len(cfg.StateFilters), len(cfg.Tenants), len(resolved), len(resolvedState))

	return nil
}

// WatchLoop periodically checks for config file changes and reloads.
func (m *ConfigManager) WatchLoop(interval time.Duration) {
	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	for range ticker.C {
		info, err := os.Stat(m.path)
		if err != nil {
			log.Printf("WARN: cannot stat config %s: %v", m.path, err)
			continue
		}

		m.mu.RLock()
		changed := info.ModTime().After(m.lastMod)
		m.mu.RUnlock()

		if changed {
			log.Printf("Config file changed, reloading...")
			if err := m.Load(); err != nil {
				log.Printf("ERROR: failed to reload config: %v", err)
			} else {
				m.mu.Lock()
				m.lastMod = info.ModTime()
				m.mu.Unlock()
			}
		}
	}
}

func (m *ConfigManager) GetConfig() *ThresholdConfig {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.config
}

func (m *ConfigManager) IsLoaded() bool {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.loaded
}

func (m *ConfigManager) LastReload() time.Time {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.lastReload
}
