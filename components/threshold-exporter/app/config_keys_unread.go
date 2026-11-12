package main

// ============================================================
// Config keys no row of this exporter reads (ADR-038 §11, #3353)
// ============================================================
//
// What this exporter itself records as read by none of its rows, at every
// commit: one line naming every (tenant, key, reason), and the gauge
// da_config_keys_unread{reason} counting them, re-Set on every commit so a
// fixed tree reads 0 again. The reasons are config.KeysUnreadReasons (one
// table, which the gauge's HELP reads too).
//
// Why a gauge: a tree that changes outside tenant-api in direct mode (git,
// a configmap sync) runs no whole-tree key verdict — no PR, so no CI — and
// the exporter's reload is the one place every change passes (#3353, owner
// ruling A). The shipped ConfigKeysUnread rule alerts through the existing
// Alertmanager on the reasons config.KeysUnreadAlerts selects.
//
// ⛔ A FACT OF THIS READER, NOT THE VERDICT. Whether a key is an error is
// the shared key verdict's (ADR-038 decision 4). Two readings keep the two
// from disagreeing where this side can know the answer:
//   - a threshold key a Policy-as-Code rule of the root carrier names is
//     read (by policy_engine), so it is left out — through the verdict's own
//     config.NamedByPolicy and the root carrier's decode
//     (ThresholdConfig.PolicyHeads), never a second rule here;
//   - profile_not_filled is listed: ApplyProfiles' own decision, which the
//     verdict judges an error.
//
// ⚠️ NOT COVERED — the gauge reading 0 is not "the verdict is clean":
//   - a REGISTERED reserved key no reader reads (a `_state_<filter>` naming
//     a filter the platform removed, a `_routing*` key read by the route
//     generator): this exporter does not run the shared verdict;
//   - a retired `_` key (the registry lists it with a migration hint);
//   - a key of a profile no tenant binds (no tenant's map holds it);
//   - a key a subtree `_defaults.yaml` writes that no row reads: the build
//     never hands it to a tenant, and da_config_subtree_undeliverable_tenants
//     or da_config_values_not_served{reason="value_rejected"} counts it;
//   - a value that is read but not as written: that is
//     da_config_values_not_served.

import (
	"fmt"
	"sort"
	"strings"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// keysUnreadReasons is the closed label set of da_config_keys_unread{reason}.
var keysUnreadReasons = config.KeysUnreadReasons()

// keysUnreadLogPrefix opens formatKeysUnreadLog's line.
const keysUnreadLogPrefix = "config keys no row of this exporter reads"

// keysUnreadDisabled is the keys-unread reason of a key switched off in every
// window that no row phase reads (config.KeysUnreadDisabled).
const keysUnreadDisabled = config.KeysUnreadDisabled

// auditKeysUnread publishes the committed config's keys-unread set: re-Sets
// da_config_keys_unread{reason} on every call and writes the line when the
// set changed. verdicts is the commit's resolver record (auditValuesNotServed
// reads the same). Returns the set for tests.
func (m *ConfigManager) auditKeysUnread(cfg *ThresholdConfig, flatScan *flatScanState, verdicts map[string]map[string]string, context string) []valueNotServed {
	ku := collectKeysUnread(cfg, verdicts, m.valuesNotServedCache.disabledUnread(), rootPolicyHeads(cfg, flatScan))
	if m.keysUnread.recordAndDecide(ku, keysUnreadReasons, m.getMetrics().SetKeysUnread) {
		m.getLogger().Print(formatKeysUnreadLog(ku, context))
	}
	return ku
}

// rootPolicyHeads is the Policy-as-Code rule heads of the root carrier the
// committed config was built from — the one file policy_engine reads rules
// from: in directory mode its partial in flatScan.configs (kept across
// incremental reloads, so the file need not be read again), in single-file
// mode the file itself (cfg).
func rootPolicyHeads(cfg *ThresholdConfig, flatScan *flatScanState) []config.PolicyHead {
	if flatScan == nil {
		return cfg.PolicyHeads()
	}
	if flatScan.tree == nil {
		return nil
	}
	key, ok := flatScan.tree.RootCarrierKey()
	if !ok {
		return nil
	}
	partial, ok := flatScan.configs[key]
	if !ok {
		return nil
	}
	return partial.PolicyHeads()
}

// collectKeysUnread is what this exporter itself records as read by none of
// its rows (ADR-038 §11), in the written spelling: a tenant key the resolver
// records unconsumed (#3378); one switched off in every window that no row
// phase reads (disabled_unread: the `disable` switches nothing off — a
// misspelt key written as `disable`); a key the tenant's profile writes that
// ApplyProfiles does not fill in (profile_not_filled, #1189 — not in the
// tenant's map, so keyed canonically); and a `_` key the reserved-key
// registry lists no reader for (unregistered, #3045). A threshold key a
// policy rule that applies to the tenant names is read by policy_engine and
// left out (config.NamedByPolicy, the verdict's own test; a `_` key the
// registry does not list is outside policy_engine's view, as there). The
// tenant's map is the built one: a key a profile or a platform file's
// `tenants:` entry hands it is listed for each tenant it reaches. A key a
// subtree `_defaults.yaml` writes that no row reads never reaches that map —
// the build reports it undeliverable or value_rejected instead — so it is
// not listed here.
func collectKeysUnread(cfg *ThresholdConfig, verdicts map[string]map[string]string, disabled map[string][]string, heads []config.PolicyHead) []valueNotServed {
	var out []valueNotServed
	add := func(tenant, key, reason string) {
		if !strings.HasPrefix(key, "_") && config.NamedByPolicy(tenant, key, heads) {
			return
		}
		out = append(out, valueNotServed{Tenant: tenant, Key: key, Reason: reason})
	}
	for tenant, keys := range disabled {
		for _, canon := range keys {
			add(tenant, writtenSpelling(cfg.Tenants[tenant], canon), keysUnreadDisabled)
		}
	}
	for tenant, keys := range verdicts {
		for canon, reason := range keys {
			if reason == config.NotServedUnconsumed {
				add(tenant, writtenSpelling(cfg.Tenants[tenant], canon), reason)
			}
		}
	}
	for tenant, keys := range cfg.ProfileNotFilled() {
		for _, canon := range keys {
			// ApplyProfiles runs before a subtree `_defaults.yaml` hands its
			// values down, so the tenant's built map may hold the key after
			// all — a value effective shows and /metrics serves, which the
			// verdict reads as read (its profileNotFilled skips a key the
			// tenant's view holds). Asked of the built map, in either #1231
			// spelling.
			if _, held := cfg.Tenants[tenant][writtenSpelling(cfg.Tenants[tenant], canon)]; held {
				continue
			}
			// Not through add: a value no tenant is handed is in no view a
			// policy rule reads, and the verdict judges it whatever names it.
			out = append(out, valueNotServed{Tenant: tenant, Key: canon, Reason: config.NotServedProfileNotFilled})
		}
	}
	for tenant, overrides := range cfg.Tenants {
		for key := range overrides {
			if config.IsUnregisteredUnderscoreKey(key) {
				add(tenant, key, config.NotServedUnregistered)
			}
		}
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Tenant != out[j].Tenant {
			return out[i].Tenant < out[j].Tenant
		}
		return out[i].Key < out[j].Key
	})
	return out
}

// formatKeysUnreadLog is collectKeysUnread's line: no severity word, since
// how severe an unread key is depends on where it is written (ADR-038
// decision 4), which the shared key verdict judges.
func formatKeysUnreadLog(ku []valueNotServed, context string) string {
	var b strings.Builder
	fmt.Fprintf(&b,
		keysUnreadLogPrefix+" (%s): %d tenant key(s): "+config.KeysUnreadReasonsLine()+". A key may come from "+
			"the tenant's file, a profile or a platform file's `tenants:` entry. Whether one is "+
			"an error is the shared key verdict's: run `da-tools explain-keys` on the conf.d tree, or validate-config. "+
			"da_config_keys_unread counts them by reason. Keys:",
		context, len(ku))
	// Every key, not a sample: the gauge counts them and names none, and
	// this line is the one place the exporter states which keys it does not
	// read.
	for i, v := range ku {
		if i > 0 {
			b.WriteString(";")
		}
		// Quoted: a tenant id or key is the tenant's own text, and may hold
		// a newline or a "; tenant=" of its own.
		fmt.Fprintf(&b, " tenant=%q key=%q reason=%s", v.Tenant, v.Key, v.Reason)
	}
	return b.String()
}
