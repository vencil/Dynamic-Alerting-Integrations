package gitops

import (
	"log/slog"
	"reflect"
	"sort"
	"strconv"
)

// #1560 ⑤-b (owner decision: tenant-api's log keeps structured fields
// only). The key verdict and the errors that carry its lines hold what the
// generator and da-guard said about the tenants' files — a key, a file,
// tenant ids, and a reason (da-guard's own sentence since #3703, which
// quotes no value; this log still keeps none of it). Each logs itself by
// closed-vocabulary fields and counts.
// ⚠️ slog resolves a LogValuer only on the value it is handed, never on the
// elements of a slice or the fields of a struct it marshals: log the
// verdict, never kv.Findings. What keeps this text out of the log is the
// behaviour matrix; the type scan only makes a new error type decide.
//
// The status and a finding's kind are the generator's strings too, so each
// is logged through a closed vocabulary, anything else as "other".

// LogValue implements slog.LogValuer: status, reason, truncated and the
// findings by kind — never a finding's key, reason, file or tenants.
func (kv *KeyVerdict) LogValue() slog.Value {
	return slog.GroupValue(slog.String("status", logStatus(kv.Status)),
		slog.String("reason", keyVerdictLogReason(kv.Reason)),
		slog.Int("truncated", kv.Truncated), slog.Any("findings_by_kind", keyFindingKinds(kv.Findings)))
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

// logOther stands in the log for a value outside its closed vocabulary.
const logOther = "other"

// logStatus is a key verdict's status as the log keeps it.
func logStatus(status string) string {
	if status == KeyVerdictComplete || status == KeyVerdictNotCompleted {
		return status
	}
	return logOther
}

// keyFindingKinds is "kind=n" per finding kind (a verdictKinds key, else
// "other"), sorted.
func keyFindingKinds(findings []KeyFinding) []string {
	counts := map[string]int{}
	for _, f := range findings {
		k := f.Kind
		if _, ok := verdictKinds[k]; !ok {
			k = logOther
		}
		counts[k]++
	}
	out := make([]string, 0, len(counts))
	for k, n := range counts {
		out = append(out, k+"="+strconv.Itoa(n))
	}
	sort.Strings(out)
	return out
}
