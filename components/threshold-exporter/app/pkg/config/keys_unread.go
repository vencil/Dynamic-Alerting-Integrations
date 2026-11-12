package config

// keys_unread: the closed reason set of the exporter's keys-unread audit —
// the WARN "config keys no row of this exporter reads" and the gauge
// da_config_keys_unread{reason} (#3353, ADR-038 §11). The audit itself is
// package main's (config_keys_unread.go); the table is here so the gauge's
// HELP (internal/scrape) and the audit read one list.
//
// A fact of this exporter, not a judgement: how severe an unread key is
// belongs to the shared key verdict (ADR-038 decision 4). The table's
// `alerts` column is only which reasons the shipped ConfigKeysUnread rule
// selects — the reasons the verdict judges an error wherever they are
// written; TestKeysUnreadRuleSelectsTheAlertingReasons holds the rule's
// matcher to it.

import "strings"

// KeysUnreadDisabled is the keys-unread reason of a key switched off in
// every window that no row phase reads (ValuesNotServedAndDisabled) — not a
// NotServed reason: not serving it is what `disable` asks, but the
// `disable` itself switches nothing off.
const KeysUnreadDisabled = "disabled_unread"

// keysUnreadReasonTable is every reason of da_config_keys_unread{reason}, in
// order, with its HELP text and whether ConfigKeysUnread alerts on it.
var keysUnreadReasonTable = []struct {
	reason string
	help   string
	alerts bool
}{
	{NotServedUnconsumed, "a threshold key no row reads: undeclared at the root, or a `_critical` whose base has no root `defaults:` value", true},
	{NotServedProfileNotFilled, "a key the tenant's profile writes that the root declares without a platform value (`optional_overrides:`), so the profile does not fill it in", true},
	{NotServedUnregistered, "a `_` key the reserved-key registry lists no reader for", true},
	// The verdict's disable_noop: a warning, which may be a deliberate
	// guard for a key the platform adds later (ADR-038 decision 8).
	{KeysUnreadDisabled, "a `disable` of a key no row reads, which switches nothing off", false},
}

// KeysUnreadReasons is the closed label set of da_config_keys_unread.
func KeysUnreadReasons() []string {
	out := make([]string, len(keysUnreadReasonTable))
	for i, r := range keysUnreadReasonTable {
		out[i] = r.reason
	}
	return out
}

// KeysUnreadAlerts reports whether the shipped ConfigKeysUnread rule alerts
// on reason (false for a reason outside the set).
func KeysUnreadAlerts(reason string) bool {
	for _, r := range keysUnreadReasonTable {
		if r.reason == reason {
			return r.alerts
		}
	}
	return false
}

// KeysUnreadReasonsHelp is every reason with its meaning, for the gauge's
// HELP: "unconsumed (…), …, and disabled_unread (…)".
func KeysUnreadReasonsHelp() string {
	parts := make([]string, len(keysUnreadReasonTable))
	for i, r := range keysUnreadReasonTable {
		parts[i] = r.reason + " (" + r.help + ")"
	}
	return joinWithAnd(parts)
}

// KeysUnreadReasonsLine is the reasons' meanings for the WARN line:
// "unconsumed is a threshold key no row reads: …; … ".
func KeysUnreadReasonsLine() string {
	parts := make([]string, len(keysUnreadReasonTable))
	for i, r := range keysUnreadReasonTable {
		parts[i] = r.reason + " is " + r.help
	}
	return strings.Join(parts, "; ")
}
