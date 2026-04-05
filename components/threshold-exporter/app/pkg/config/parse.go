package config

import (
	"fmt"
	"log"
	"regexp"
	"strconv"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

// UnmarshalYAML implements custom YAML unmarshalling for ScheduledValue.
// Supports three forms:
//  1. Scalar string (backward compatible): "80"
//  2. Structured mapping with default+overrides: {default: "80", overrides: [...]}
//  3. Arbitrary mapping (e.g., _routing): {receiver: "...", group_wait: "30s"}
//     → serialized back to YAML string and stored in Default for downstream parsing
func (sv *ScheduledValue) UnmarshalYAML(value *yaml.Node) error {
	if value.Kind == yaml.ScalarNode {
		sv.Default = value.Value
		return nil
	}
	if value.Kind == yaml.MappingNode {
		// Check if this mapping has a "default" key (structured ScheduledValue)
		hasDefault := false
		for i := 0; i < len(value.Content)-1; i += 2 {
			if value.Content[i].Value == "default" {
				hasDefault = true
				break
			}
		}
		if hasDefault {
			var structured struct {
				Default   string               `yaml:"default"`
				Overrides []TimeWindowOverride `yaml:"overrides"`
			}
			if err := value.Decode(&structured); err != nil {
				return err
			}
			sv.Default = structured.Default
			sv.Overrides = structured.Overrides
			return nil
		}
		// Arbitrary mapping (e.g., _routing): serialize back to YAML string
		var raw interface{}
		if err := value.Decode(&raw); err != nil {
			return err
		}
		out, err := yaml.Marshal(raw)
		if err != nil {
			return fmt.Errorf("ScheduledValue: failed to re-serialize mapping: %w", err)
		}
		sv.Default = string(out)
		return nil
	}
	return fmt.Errorf("ScheduledValue: unsupported YAML node kind %d", value.Kind)
}

// String returns the default value for backward-compatible string access.
func (sv ScheduledValue) String() string {
	return sv.Default
}

// ResolveValue returns the effective value at the given time.
// If a time-window override matches, its value is returned; otherwise the default.
func (sv ScheduledValue) ResolveValue(now time.Time) string {
	for _, o := range sv.Overrides {
		if matchTimeWindow(o.Window, now) {
			return o.Value
		}
	}
	return sv.Default
}

// MatchTimeWindow checks if the given time falls within a UTC "HH:MM-HH:MM" window.
// Supports cross-midnight windows (e.g., "22:00-06:00").
func MatchTimeWindow(window string, now time.Time) bool {
	return matchTimeWindow(window, now)
}

func matchTimeWindow(window string, now time.Time) bool {
	parts := strings.SplitN(window, "-", 2)
	if len(parts) != 2 {
		log.Printf("WARN: invalid time window format %q", window)
		return false
	}
	startH, startM, err1 := parseHHMM(parts[0])
	endH, endM, err2 := parseHHMM(parts[1])
	if err1 != nil || err2 != nil {
		log.Printf("WARN: invalid time window %q: start=%v end=%v", window, err1, err2)
		return false
	}

	utcNow := now.UTC()
	nowMinutes := utcNow.Hour()*60 + utcNow.Minute()
	startMinutes := startH*60 + startM
	endMinutes := endH*60 + endM

	if startMinutes <= endMinutes {
		// Same day: e.g., 01:00-09:00
		return nowMinutes >= startMinutes && nowMinutes < endMinutes
	}
	// Cross midnight: e.g., 22:00-06:00
	return nowMinutes >= startMinutes || nowMinutes < endMinutes
}

// ParseHHMM parses "HH:MM" into hour and minute (exported for testing).
func ParseHHMM(s string) (int, int, error) { return parseHHMM(s) }

// parseHHMM parses "HH:MM" into hour and minute.
func parseHHMM(s string) (int, int, error) {
	s = strings.TrimSpace(s)
	parts := strings.SplitN(s, ":", 2)
	if len(parts) != 2 {
		return 0, 0, fmt.Errorf("invalid HH:MM format: %q", s)
	}
	h, err := strconv.Atoi(strings.TrimSpace(parts[0]))
	if err != nil || h < 0 || h > 23 {
		return 0, 0, fmt.Errorf("invalid hour in %q", s)
	}
	m, err := strconv.Atoi(strings.TrimSpace(parts[1]))
	if err != nil || m < 0 || m > 59 {
		return 0, 0, fmt.Errorf("invalid minute in %q", s)
	}
	return h, m, nil
}

// ClampDuration validates a duration string against guardrails (exported for testing).
func ClampDuration(value, param, tenant string) string { return clampDuration(value, param, tenant) }

// clampDuration validates a duration string against guardrails.
// Returns the original string if within bounds, or the clamped value with a warning.
func clampDuration(value, param, tenant string) string {
	bounds, ok := routingGuardrails[param]
	if !ok {
		return value
	}

	d, err := time.ParseDuration(value)
	if err != nil {
		// Try Prometheus-style duration (e.g., "30s", "5m", "4h")
		d, err = parsePromDuration(value)
		if err != nil {
			log.Printf("WARN: invalid %s %q for tenant=%s, ignoring", param, value, tenant)
			return ""
		}
	}

	if d < bounds[0] {
		clamped := formatDuration(bounds[0])
		log.Printf("WARN: %s %q for tenant=%s below minimum %s, clamping to %s", param, value, tenant, formatDuration(bounds[0]), clamped)
		return clamped
	}
	if d > bounds[1] {
		clamped := formatDuration(bounds[1])
		log.Printf("WARN: %s %q for tenant=%s above maximum %s, clamping to %s", param, value, tenant, formatDuration(bounds[1]), clamped)
		return clamped
	}

	return value
}

// ParsePromDuration parses Prometheus-style duration strings (exported for testing).
func ParsePromDuration(s string) (time.Duration, error) { return parsePromDuration(s) }

// parsePromDuration parses Prometheus-style duration strings like "30s", "5m", "4h".
func parsePromDuration(s string) (time.Duration, error) {
	s = strings.TrimSpace(s)
	if len(s) < 2 {
		return 0, fmt.Errorf("duration too short: %q", s)
	}

	unit := s[len(s)-1]
	numStr := s[:len(s)-1]
	num, err := strconv.ParseFloat(numStr, 64)
	if err != nil {
		return 0, fmt.Errorf("invalid number in duration %q: %w", s, err)
	}

	switch unit {
	case 's':
		return time.Duration(num * float64(time.Second)), nil
	case 'm':
		return time.Duration(num * float64(time.Minute)), nil
	case 'h':
		return time.Duration(num * float64(time.Hour)), nil
	case 'd':
		return time.Duration(num * 24 * float64(time.Hour)), nil
	default:
		return 0, fmt.Errorf("unknown duration unit %q in %q", string(unit), s)
	}
}

// FormatDuration formats a duration as a Prometheus-style string (exported for testing).
func FormatDuration(d time.Duration) string { return formatDuration(d) }

// formatDuration formats a time.Duration as a human-readable Prometheus-style string.
func formatDuration(d time.Duration) string {
	// NOTE: Prometheus/Alertmanager duration format only supports s/m/h (not d/w/y).
	// Do NOT convert to days even if evenly divisible.
	if d >= time.Hour && d%time.Hour == 0 {
		return fmt.Sprintf("%dh", int(d/time.Hour))
	}
	if d >= time.Minute && d%time.Minute == 0 {
		return fmt.Sprintf("%dm", int(d/time.Minute))
	}
	return fmt.Sprintf("%ds", int(d/time.Second))
}

// IsDisabled checks if a value string means "disabled" (exported for testing).
func IsDisabled(lower string) bool { return isDisabled(lower) }

// isDisabled checks if a value string means "disabled".
func isDisabled(lower string) bool {
	return lower == "disable" || lower == "disabled" || lower == "off" || lower == "false"
}

// ParseMetricKey splits a metric key into component and metric (exported for testing).
func ParseMetricKey(key string) (string, string) { return parseMetricKey(key) }

// parseMetricKey splits "mysql_connections" into ("mysql", "connections").
// If no underscore, component defaults to "default".
func parseMetricKey(key string) (component, metric string) {
	idx := strings.Index(key, "_")
	if idx < 0 {
		return "default", key
	}
	return key[:idx], key[idx+1:]
}

// keyWithLabelsRe matches "metric_name{label1=\"val1\", label2=\"val2\"}"
var keyWithLabelsRe = regexp.MustCompile(`^([a-zA-Z0-9_]+)\{(.+)\}$`)

// ParseKeyWithLabels splits a dimensional metric key (exported for testing).
func ParseKeyWithLabels(key string) (string, map[string]string, map[string]string) {
	return parseKeyWithLabels(key)
}

// parseKeyWithLabels splits a metric key that may contain dimensional labels.
// Returns base key, exact-match labels (=), and regex-match labels (=~).
//
// Examples:
//
//	"redis_queue_length"                                         → ("redis_queue_length", nil, nil)
//	"redis_queue_length{queue=\"tasks\", priority=\"high\"}"     → ("redis_queue_length", {"queue":"tasks","priority":"high"}, nil)
//	"oracle_tablespace{tablespace=~\"SYS.*\"}"                  → ("oracle_tablespace", nil, {"tablespace":"SYS.*"})
//	"oracle_ts{env=\"prod\", tablespace=~\"SYS.*\"}"            → ("oracle_ts", {"env":"prod"}, {"tablespace":"SYS.*"})
func parseKeyWithLabels(key string) (string, map[string]string, map[string]string) {
	m := keyWithLabelsRe.FindStringSubmatch(key)
	if m == nil {
		return key, nil, nil
	}
	baseKey := m[1]
	exact, regex := parseLabelsStringWithOp(m[2])
	if len(exact) == 0 {
		exact = nil
	}
	if len(regex) == 0 {
		regex = nil
	}
	if exact == nil && regex == nil {
		return baseKey, nil, nil
	}
	return baseKey, exact, regex
}

// ParseLabelsStringWithOp parses label pairs with = and =~ operators (exported for testing).
func ParseLabelsStringWithOp(s string) (map[string]string, map[string]string) {
	return parseLabelsStringWithOp(s)
}

// parseLabelsStringWithOp parses a comma-separated label string into exact and regex maps.
// Supports both = (exact match) and =~ (regex match) operators.
//
// Input: `queue="tasks", tablespace=~"SYS.*"`
// Returns: exact={"queue":"tasks"}, regex={"tablespace":"SYS.*"}
func parseLabelsStringWithOp(s string) (exact map[string]string, regex map[string]string) {
	exact = make(map[string]string)
	regex = make(map[string]string)
	pairs := strings.Split(s, ",")
	for _, pair := range pairs {
		pair = strings.TrimSpace(pair)
		// Check for =~ first (must check before = to avoid partial match)
		if idx := strings.Index(pair, "=~"); idx >= 0 {
			k := strings.TrimSpace(pair[:idx])
			v := strings.TrimSpace(pair[idx+2:])
			v = strings.Trim(v, `"'`)
			if k != "" {
				regex[k] = v
			}
			continue
		}
		// Regular = operator
		eqIdx := strings.Index(pair, "=")
		if eqIdx < 0 {
			continue
		}
		k := strings.TrimSpace(pair[:eqIdx])
		v := strings.TrimSpace(pair[eqIdx+1:])
		// Strip surrounding quotes (single or double)
		v = strings.Trim(v, `"'`)
		if k != "" {
			exact[k] = v
		}
	}
	return
}
