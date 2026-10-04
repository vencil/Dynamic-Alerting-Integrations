package config

// A null inside a schedule that has override windows (#2708).
//
// Owner's ruling: a schedule with no window and a null `default:` writes
// nothing — exactly plain null (nullSchedule, null_threshold.go). A null
// anywhere in a schedule that HAS windows — a window's `value: null` or
// `window: null`, a null window entry, or a null `default:` beside windows —
// is refused at validation time
// (da-guard schedule_null_value, validate-config schedule_null). No runtime
// meaning is defined for it: what /metrics and the walker do with such a
// value is left exactly as it was.
//
// "A schedule" here is a mapping with a `default:` key.

import (
	"fmt"
	"path/filepath"
	"sort"
	"strings"

	"gopkg.in/yaml.v3"
)

// ScheduleNull is one threshold, in one file the exporter reads, written as a
// schedule with override windows and a null in it (#2708).
type ScheduleNull struct {
	// File is the file, root-relative and slash-separated.
	File string
	// Section is where in the file: "defaults" (a defaults carrier's
	// defaults block), "tenants" (a tenant file's or a root platform file's
	// entry) or "profiles" (a root platform file's profile).
	Section string
	// Owner is the tenant id ("tenants") or the profile name ("profiles");
	// "" for "defaults".
	Owner string
	// Key is the threshold key as the file spells it.
	Key string
	// Problems says where the null is, in schedule order.
	Problems []string
}

// scheduleNullProblems is, for raw (a generic decode of one threshold value),
// each null inside a schedule that has override windows; nil when raw is not
// such a schedule or has no null in it. A schedule without windows is not
// judged here: with a null default it is nullSchedule (no write), otherwise
// it holds no null.
func scheduleNullProblems(raw any) []string {
	m, ok := raw.(map[string]any)
	if !ok {
		return nil
	}
	d, isSchedule := m["default"]
	if !isSchedule {
		return nil
	}
	windows, _ := m["overrides"].([]any)
	if len(windows) == 0 {
		return nil
	}
	var out []string
	if d == nil {
		out = append(out, fmt.Sprintf("`default:` is null beside %d override window(s)", len(windows)))
	}
	for i, w := range windows {
		if w == nil {
			out = append(out, fmt.Sprintf("`overrides[%d]` is null", i))
			continue
		}
		wm, ok := w.(map[string]any)
		if !ok {
			continue
		}
		if win, has := wm["window"]; has && win == nil {
			out = append(out, fmt.Sprintf("`overrides[%d]` has `window: null`", i))
		}
		if v, has := wm["value"]; has && v == nil {
			if win, ok := wm["window"].(string); ok {
				out = append(out, fmt.Sprintf("`overrides[%d]` (window %q) has `value: null`", i, win))
			} else {
				out = append(out, fmt.Sprintf("`overrides[%d]` has `value: null`", i))
			}
		}
	}
	return out
}

// thresholdScheduleNulls appends to out one ScheduleNull per threshold key of
// body (a generic decode) that scheduleNullProblems refuses. Reserved
// (`_`-prefixed) keys are not thresholds and are not judged.
func thresholdScheduleNulls(out []ScheduleNull, file, section, owner string, body map[string]any) []ScheduleNull {
	keys := make([]string, 0, len(body))
	for k := range body {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		if strings.HasPrefix(k, "_") {
			continue
		}
		if p := scheduleNullProblems(body[k]); len(p) > 0 {
			out = append(out, ScheduleNull{File: file, Section: section, Owner: owner, Key: k, Problems: p})
		}
	}
	return out
}

// sectionScheduleNulls is thresholdScheduleNulls over every entry of the
// top-level `section:` mapping (tenants or profiles) of doc, a file's first
// document. keep, when non-nil, selects the entries by id (the source text,
// as the exporter keys tenants, #2114). An entry whose body does not decode
// as a mapping is not judged: the exporter's decode already names it.
func sectionScheduleNulls(out []ScheduleNull, file, section string, doc *yaml.Node, keep func(id string) bool) []ScheduleNull {
	if doc == nil || doc.Kind != yaml.DocumentNode || len(doc.Content) == 0 {
		return out
	}
	root := doc.Content[0]
	if root.Kind != yaml.MappingNode {
		return out
	}
	for i := 0; i+1 < len(root.Content); i += 2 {
		if root.Content[i].Value != section || root.Content[i+1].Kind != yaml.MappingNode {
			continue
		}
		entries := root.Content[i+1]
		for j := 0; j+1 < len(entries.Content); j += 2 {
			id := entries.Content[j].Value
			if keep != nil && !keep(id) {
				continue
			}
			var body any
			if entries.Content[j+1].Decode(&body) != nil {
				continue
			}
			m, ok := normalizeYAMLToJSON(body).(map[string]any)
			if !ok {
				continue
			}
			out = thresholdScheduleNulls(out, file, section, id, m)
		}
	}
	return out
}

// scopeScheduleNulls is ScopedTenants.ScheduleNulls: every threshold written
// as a schedule with windows and a null in it, in the files the exporter
// reads that bear on the scope (bearsOnScope) and that its load did not drop
// (a dropped file is named once, by ParseFailed):
//
//   - a tenant file: the entries of the in-scope tenants;
//   - a defaults carrier the chain selects (any level): its defaults block,
//     as the chain merge reads it (ParseChainDefaults);
//   - a root platform file (isRootPlatformKey — the selected root carrier
//     included): the `tenants:` entries of the in-scope tenants and every
//     `profiles:` entry.
//
// A `_` file below the root that is not a selected carrier is read by no
// plane and is not judged. Sorted by File, then Section, Owner, Key.
func scopeScheduleNulls(scan *TreeScan, scopeRel string, inScope map[string]struct{}, parseFailed []string) []ScheduleNull {
	dropped := make(map[string]bool, len(parseFailed))
	for _, k := range parseFailed {
		dropped[k] = true
	}
	carriers := map[string]bool{}
	for _, abs := range scan.DefaultsCarriers().ByDir {
		if rel, err := filepath.Rel(scan.AbsRoot, abs); err == nil {
			carriers[filepath.ToSlash(rel)] = true
		}
	}
	rootCarrier := selectedRootCarrierKey(scan)
	tenantInScope := func(id string) bool { _, ok := inScope[id]; return ok }

	var out []ScheduleNull
	for _, key := range scan.Keys {
		f, ok := scan.Files[key]
		if !ok || f.Data == nil || dropped[key] || !bearsOnScope(key, scopeRel) {
			continue
		}
		platform := isPlatformKey(key)
		rootPlatform := platform && isRootPlatformKey(key, rootCarrier)
		if platform && !rootPlatform && !carriers[key] {
			continue
		}
		var doc yaml.Node
		if yaml.Unmarshal(f.Data, &doc) != nil {
			continue
		}
		if carriers[key] {
			out = thresholdScheduleNulls(out, key, "defaults", "", ParseChainDefaults(f.Data).block)
		}
		if !platform || rootPlatform {
			out = sectionScheduleNulls(out, key, "tenants", &doc, tenantInScope)
		}
		if rootPlatform {
			out = sectionScheduleNulls(out, key, "profiles", &doc, nil)
		}
	}
	sort.SliceStable(out, func(i, j int) bool {
		a, b := out[i], out[j]
		if a.File != b.File {
			return a.File < b.File
		}
		if a.Section != b.Section {
			return a.Section < b.Section
		}
		if a.Owner != b.Owner {
			return a.Owner < b.Owner
		}
		return a.Key < b.Key
	})
	return out
}
