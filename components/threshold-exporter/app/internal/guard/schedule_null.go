package guard

// A null inside a schedule that has override windows (#2708).
//
// Owner's ruling: `{default: null}` with no window is plain null — no write
// at that layer (pkg/config nullSchedule). A null anywhere in a schedule
// that HAS windows — a null window entry, a window's `window:` / `value:`
// null, or a null `default:` beside windows — has no defined meaning and is refused here, as an error. Which
// values are such schedules is pkg/config's answer
// (config.ScopedTenants.ScheduleNulls, handed in as CheckInput.ScheduleNulls),
// not re-judged here.

import (
	"fmt"
	"strings"
)

// FindingScheduleNullValue (error; #2708): a threshold is written as a
// schedule with override windows and a null in it. Field is
// `<file>:<section>.[<owner>.]<key>`; TenantID is the tenant for a
// `tenants:` entry, empty for a defaults block or a profile.
const FindingScheduleNullValue FindingKind = "schedule_null_value"

// checkScheduleNullValues reports one FindingScheduleNullValue per entry of
// input.ScheduleNulls (already limited to the caller's scope).
func checkScheduleNullValues(input CheckInput) []Finding {
	var out []Finding
	for _, sn := range input.ScheduleNulls {
		where := sn.Section + "."
		if sn.Owner != "" {
			where += sn.Owner + "."
		}
		where += sn.Key
		var tenant string
		if sn.Section == "tenants" {
			tenant = sn.Owner
		}
		out = append(out, Finding{
			Severity: SeverityError,
			Kind:     FindingScheduleNullValue,
			TenantID: tenant,
			Field:    sn.File + ":" + where,
			Message: fmt.Sprintf("%s: `%s` is a schedule with override windows and a null in it: %s. A null inside "+
				"a schedule that has windows is not accepted (#2708). Give the default and every window a value. To "+
				"leave this layer without a value for `%s`, remove the key or write it as plain `null`.",
				sn.File, where, strings.Join(sn.Problems, "; "), sn.Key),
		})
	}
	return out
}
