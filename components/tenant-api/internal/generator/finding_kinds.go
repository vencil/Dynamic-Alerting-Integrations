package generator

// findingKinds is the closed vocabulary of a finding's kind as the log keeps
// it (#1560 ⑤-b): a copy of the route generator's kind catalog, held to it
// by a test. A kind missing here is logged as "other", never as the
// container's text.
var findingKinds = map[string]bool{
	"conflicting_override_matcher":        true,
	"unknown_override_metric_group":       true,
	"conflicting_receiver_field":          true,
	"credential_plaintext":                true,
	"credential_unjudged":                 true,
	"critical_escalation_leak":            true,
	"critical_escalation_missing":         true,
	"custom_alert_delivery_unchecked":     true,
	"custom_alert_undelivered":            true,
	"domain_policy_out_of_scope":          true,
	"domain_policy_unusable":              true,
	"domain_policy_violation":             true,
	"duplicate_override_matcher":          true,
	"duplicate_tenant":                    true,
	"empty_override_matcher":              true,
	"invalid_override_entry":              true,
	"invalid_receiver_field":              true,
	"invalid_route_entry":                 true,
	"invalid_tenant_id":                   true,
	"key_dead_entry":                      true,
	"key_disable_cascade":                 true,
	"key_disable_noop":                    true,
	"key_profile_not_judged":              true,
	"key_renamed":                         true,
	"key_undecidable":                     true,
	"key_unread":                          true,
	"key_value_unread":                    true,
	"key_verdict_incomplete":              true,
	"key_version_label":                   true,
	"missing_receiver_field":              true,
	"parent_receiver_not_rendered":        true,
	"receiver_domain_not_allowed":         true,
	"receiver_host_unparseable":           true,
	"redundant_override_receiver":         true,
	"refusal_summary":                     true,
	"routing_defaults_not_mapping":        true,
	"routing_defaults_null_below_root":    true,
	"routing_defaults_routes_ignored":     true,
	"routing_enforced_below_root":         true,
	"routing_enforced_enabled_invalid":    true,
	"routing_enforced_match_invalid":      true,
	"routing_group_by_invalid":            true,
	"routing_in_unread_location":          true,
	"routing_key_ignored":                 true,
	"routing_not_mapping":                 true,
	"routing_profile_duplicate":           true,
	"routing_profile_invalid":             true,
	"routing_profiles_unusable":           true,
	"routing_value_not_string":            true,
	"run_failed":                          true,
	"secret_ref_destination_unconfined":   true,
	"secret_ref_follow_redirects":         true,
	"secret_ref_no_owner":                 true,
	"secret_ref_proxy_url":                true,
	"secret_ref_shared_confined":          true,
	"secret_ref_shared_field_not_allowed": true,
	"secret_ref_shared_unknown":           true,
	"secret_ref_unsupported_field":        true,
	"severity_dedup_invalid":              true,
	"tenant_file_unreadable":              true,
	"timing_value_replaced":               true,
	"unclassified":                        true,
	"unknown_receiver_type":               true,
	"unknown_routing_profile":             true,
	"unselected_defaults_carrier":         true,
	"watchdog_undelivered":                true,
}

// logKind is kind as the log may keep it: a findingKinds key, else "other".
func logKind(kind string) string {
	if findingKinds[kind] {
		return kind
	}
	return otherValue
}

// otherValue stands in the log for a value outside its closed vocabulary.
const otherValue = "other"
