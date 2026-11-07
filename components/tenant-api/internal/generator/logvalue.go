package generator

import (
	"log/slog"
	"sort"
	"strconv"
)

// #1560 ⑤-b (owner decision: the log keeps structured fields only). The
// generator and its container answer with text that can quote what tenants
// wrote (a credential among it), a platform path or a program's stderr: an
// error's Message, a verdict's stderr_tail, a finding's message and field.
// tenant-api's log is read across a trust boundary the 422 (disclosedFindings,
// by RBAC) is not, so every type here that carries such text logs itself as
// structured fields only (slog.LogValuer). Error() is unchanged: the
// responses build their fixed texts from it or from the fields.
//
// ⚠️ slog resolves a LogValuer only on the value it is handed: an error
// wrapped by fmt.Errorf("…: %w", e) is not one, and logs its Error() text.
// A call site logs the typed error errors.As found, never a wrapper
// (pinned by TestLogValuer_ResolvedOnTheTypedErrorOnly).
// TestLogValuer_EveryExportedErrorType requires every exported error type of
// this package to implement it.

// LogValue implements slog.LogValuer: the code and the reason code, never
// Message.
func (e *UnavailableError) LogValue() slog.Value {
	return slog.GroupValue(slog.String("code", e.Code), slog.String("reason", ClientReason(e.Reason)))
}

// LogValue implements slog.LogValuer: the reason code, never Message.
func (e *RejectedError) LogValue() slog.Value {
	return slog.GroupValue(slog.String("reason", ClientReason(e.Reason)))
}

// LogValue implements slog.LogValuer: the exit code and how many findings of
// each kind (a closed vocabulary) — never a finding's message or field.
func (e *FindingsError) LogValue() slog.Value {
	return slog.GroupValue(slog.Int("rc", e.RC), slog.Bool("compared_with_base", e.ComparedWithBase),
		slog.Any("findings_by_kind", kindCounts(e.Findings)))
}

// LogValue implements slog.LogValuer: result, reason code, exit code and the
// findings by kind — never message, stderr_tail or a finding's text.
func (v *Verdict) LogValue() slog.Value {
	attrs := []slog.Attr{slog.String("result", v.Result)}
	if v.Reason != "" {
		attrs = append(attrs, slog.String("reason", ClientReason(v.Reason)))
	}
	if v.RC != nil {
		attrs = append(attrs, slog.Int("rc", *v.RC))
	}
	if v.Findings != nil {
		attrs = append(attrs, slog.Any("findings_by_kind", kindCounts(v.Findings.Findings)))
	}
	return slog.GroupValue(attrs...)
}

// LogValue implements slog.LogValuer: status, reason code, tenants, seconds
// — never the service's Message.
func (kv *KeyVerdict) LogValue() slog.Value {
	return slog.GroupValue(slog.String("status", kv.Status), slog.String("reason", keyVerdictLogReason(kv.Reason)),
		slog.Int("tenants", kv.Tenants), slog.Float64("seconds", kv.Seconds))
}

// LogValue implements slog.LogValuer: the kind only.
func (f Finding) LogValue() slog.Value {
	return slog.GroupValue(slog.String("kind", f.Kind))
}

// kindCounts is "kind=n" per finding kind, sorted.
func kindCounts(findings []Finding) []string {
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
