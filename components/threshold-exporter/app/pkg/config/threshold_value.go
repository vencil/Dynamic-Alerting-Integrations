package config

import (
	"fmt"
	"strconv"
	"strings"
)

// thresholdSeverities is the closed set of severities a threshold row can
// carry: the ones the rule packs' threshold recording rules select on
// user_threshold (`severity="warning"` for `tenant:alert_threshold:<key>`,
// `severity="critical"` for `<key>_critical`). A row of any other severity is
// compared by no alert, so it is never served (#3071).
//
// ⛔ The one Go source of the set. rulepack_severity_test.go holds every
// rule-pack copy's user_threshold severity selectors to it, and
// tests/shared/threshold_value_matrix.json carries it to the Python mirrors
// (backtest_threshold._COMPARED_SEVERITIES, custom_alerts/shape.py).
var thresholdSeverities = []string{"warning", "critical"}

// ThresholdSeverities is a copy of the severities a threshold row can carry.
func ThresholdSeverities() []string {
	return append([]string(nil), thresholdSeverities...)
}

// splitThresholdSeverity reads the one grammar of a threshold value's
// severity suffix, `value[:severity]` (#3071): it splits at the LAST `:`,
// trims both halves, and lower-cases an ASCII suffix. No `:` is severity
// warning. A suffix outside thresholdSeverities — `70:high`, the empty `70:`,
// or a non-ASCII spelling (`CRİTICAL`, whose Go and Python lower-casings
// differ) — is an error: the value is unreadable as a whole, exactly like a
// value whose number does not parse.
//
// Every reader of the grammar calls it: the three threshold row resolvers
// (resolveBaseRows / resolveDeclaredRows / appendDimensionalRow), the subtree
// overlay's thresholdScalar, and custom alerts (parseCustomAlertThreshold).
// The value half is returned trimmed and otherwise as written; whether it
// is a number is the caller's question. The Python mirrors are held to it by
// tests/shared/threshold_value_matrix.json (threshold_value_parity_test.go).
func splitThresholdSeverity(raw string) (value, severity string, err error) {
	raw = strings.TrimSpace(raw)
	i := strings.LastIndex(raw, ":")
	if i < 0 {
		return raw, "warning", nil
	}
	value = strings.TrimSpace(raw[:i])
	suffix := strings.TrimSpace(raw[i+1:])
	if isASCII(suffix) {
		severity = strings.ToLower(suffix)
		for _, s := range thresholdSeverities {
			if severity == s {
				return value, severity, nil
			}
		}
	}
	return "", "", fmt.Errorf("threshold severity %q must be warning or critical", strings.ToLower(suffix))
}

// parseThresholdValue reads one threshold row's value as the row resolvers
// serve it: splitThresholdSeverity, then the value half as a float64. ok is
// false for a value unreadable either way; the caller falls back to the
// platform default or drops the row (#2377, #3071).
func parseThresholdValue(raw string) (v float64, severity string, ok bool) {
	valueStr, severity, err := splitThresholdSeverity(raw)
	if err != nil {
		return 0, "", false
	}
	v, err = strconv.ParseFloat(valueStr, 64)
	if err != nil {
		return 0, "", false
	}
	return v, severity, true
}

func isASCII(s string) bool {
	for i := 0; i < len(s); i++ {
		if s[i] >= 0x80 {
			return false
		}
	}
	return true
}
