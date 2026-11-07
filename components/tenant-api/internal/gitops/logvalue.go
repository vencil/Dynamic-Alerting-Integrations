package gitops

import (
	"log/slog"
	"reflect"
	"sort"
	"strconv"
)

// #1560 ⑤-b (owner decision: tenant-api's log keeps structured fields
// only). The key verdict and the errors that carry its lines hold what the
// generator and da-guard said about the tenants' files — a key, a reason
// that quotes a value (`references unknown profile "https://u:…@…"`), a
// file, tenant ids. Each logs itself by closed-vocabulary fields and counts.
// ⚠️ slog resolves a LogValuer only on the value it is handed, never on the
// elements of a slice or the fields of a struct it marshals: log the
// verdict, never kv.Findings. What actually keeps this text out of the log
// is the behaviour matrix (internal/handler/log_canary_matrix_test.go); the
// type scan (logvalue_test.go) only makes a new error type decide.

// LogValue implements slog.LogValuer: status, reason, truncated and the
// findings by kind — never a finding's key, reason, file or tenants.
func (kv *KeyVerdict) LogValue() slog.Value {
	return slog.GroupValue(slog.String("status", kv.Status), slog.String("reason", keyVerdictLogReason(kv.Reason)),
		slog.Int("truncated", kv.Truncated), slog.Any("findings_by_kind", keyFindingKinds(kv.Findings)))
}

// LogValue implements slog.LogValuer: severity, kind and blocks only.
func (f KeyFinding) LogValue() slog.Value {
	return slog.GroupValue(slog.String("severity", f.Severity), slog.String("kind", f.Kind),
		slog.Bool("blocks", f.Blocks))
}

// LogValue implements slog.LogValuer: how many tenants and lines — never
// the lines, which quote the verdict's keys and reasons.
func (e *KeyVerdictError) LogValue() slog.Value {
	n := 0
	for _, lines := range e.ByTenant {
		n += len(lines)
	}
	return slog.GroupValue(slog.Int("tenants", len(e.ByTenant)), slog.Int("lines", n))
}

// LogValue implements slog.LogValuer: how many ops, and the type of each
// refused op's error — never its text (a generator refusal or a key
// verdict line among them).
func (e *BatchRejectedError) LogValue() slog.Value {
	var refused []string
	for _, o := range e.Outcomes {
		if o.Err != nil {
			refused = append(refused, reflect.TypeOf(o.Err).String())
		}
	}
	return slog.GroupValue(slog.Int("ops", len(e.Outcomes)), slog.Any("refused", refused))
}

// LogValue implements slog.LogValuer: the wrapped error's own LogValue when
// it has one (a generator refusal), else its type — never its text.
func (e *normalizedRefusal) LogValue() slog.Value {
	if lv, ok := e.err.(slog.LogValuer); ok {
		return lv.LogValue()
	}
	return slog.GroupValue(slog.String("type", reflect.TypeOf(e.err).String()))
}

// keyVerdictLogReasons is the closed vocabulary of a key verdict's reason
// (KeyVerdict.Reason's doc); anything else is logged as "unknown".
var keyVerdictLogReasons = map[string]bool{"": true, "no_time": true, "deadline": true, "memory": true,
	"error": true, "undecidable": true, "platform_file": true, "preempted": true, KeyVerdictNotReported: true}

func keyVerdictLogReason(reason string) string {
	if keyVerdictLogReasons[reason] {
		return reason
	}
	return "unknown"
}

// keyFindingKinds is "kind=n" per finding kind, sorted.
func keyFindingKinds(findings []KeyFinding) []string {
	counts := map[string]int{}
	for _, f := range findings {
		counts[f.Kind]++
	}
	out := make([]string, 0, len(counts))
	for k, n := range counts {
		out = append(out, k+"="+strconv.Itoa(n))
	}
	sort.Strings(out)
	return out
}
