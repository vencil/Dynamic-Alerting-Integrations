package config

// key_refs.go — which keys of a conf.d tree belong to a platform metric key
// (#1822), for `da-guard key-refs` and through it `deprecate_rule`.
//
// A tool that retires metric `m` has to find every key that stops being
// valid once `m` leaves the platform surface: `m` itself, but also a #1231
// retired spelling of it (`mysql_cpu` for `mysql_threads_running`), its
// `_critical` tier and its dimensional keys in any spelling. Which keys those
// are is the exporter's own judgement — ValidateTenantKeys checks each
// override key against exactly one platform key — so it is answered here,
// with the function that check uses, and never re-derived by a reader.

import (
	"fmt"
	"log"
	"os"
	"sort"
	"strings"

	"gopkg.in/yaml.v3"
)

// overrideShape is how validateOverrideKeys judges an override key.
type overrideShape int

const (
	// shapeReserved: a reserved key or reserved prefix; no membership check.
	shapeReserved overrideShape = iota
	// shapeDimensional: `base{labels}`; base must be valued or declared.
	shapeDimensional
	// shapeCritical: `base_critical`; base must be valued.
	shapeCritical
	// shapeFlat: the key itself must be valued or declared.
	shapeFlat
)

// overrideKeyShape classifies a CANONICAL override key (canonicalKeyFor
// already applied) and returns the platform key — the `defaults:` /
// `optional_overrides:` entry — its membership is judged against. The one
// classifier of validateOverrideKeys and PlatformKeyFor (#1822).
func overrideKeyShape(canonKey string) (overrideShape, string) {
	if validReservedKeys[canonKey] {
		return shapeReserved, ""
	}
	for _, prefix := range validReservedPrefixes {
		if strings.HasPrefix(canonKey, prefix) {
			return shapeReserved, ""
		}
	}
	if strings.Contains(canonKey, "{") {
		base, _, _ := parseKeyWithLabels(canonKey)
		return shapeDimensional, base
	}
	if strings.HasSuffix(canonKey, criticalSuffix) {
		return shapeCritical, strings.TrimSuffix(canonKey, criticalSuffix)
	}
	return shapeFlat, canonKey
}

// PlatformKeyFor is the platform key (a `defaults:` / `optional_overrides:`
// entry, in its canonical spelling) that ValidateTenantKeys requires for a
// tenant override key, in any spelling the exporter accepts: a #1231 retired
// spelling, a `_critical` tier, a dimensional key however its labels are
// written. ok is false for a reserved key, which needs no platform key.
//
//	mysql_cpu                     → mysql_threads_running
//	mysql_cpu_critical            → mysql_threads_running
//	mysql_cpu{db='a'}             → mysql_threads_running
//	redis_queue_length{queue="x"} → redis_queue_length
func PlatformKeyFor(key string) (string, bool) {
	canon, _ := canonicalKeyFor(key)
	shape, base := overrideKeyShape(canon)
	if shape == shapeReserved {
		return "", false
	}
	return base, true
}

// KeyRef is one key a config file writes whose platform key
// (PlatformKeyFor) is a metric asked about.
type KeyRef struct {
	// File is the scan key (root-relative slash path).
	File string `json:"file"`
	// Section is the block the key is written in: "defaults",
	// "optional_overrides", "tenants" or "profiles".
	Section string `json:"section"`
	// Owner is the tenant id (tenants) or profile name (profiles); "" for
	// defaults and optional_overrides.
	Owner string `json:"owner"`
	// Key is the key as the file writes it (an optional_overrides entry: the
	// entry as written).
	Key string `json:"key"`
	// Nested: the file is a `_`-prefixed file below the root
	// (isNestedPlatformFile), which the flat load does not merge — a subtree
	// `_defaults.yaml` the hierarchy plane reads, or a file nothing reads. Its
	// references are listed so a caller can name what is left there.
	Nested bool `json:"nested"`
}

// KeyRefsReport is KeyRefs' answer.
type KeyRefsReport struct {
	// Load is the report of the exporter's own load of the tree
	// (LoadDirReport): ParseFailed and Unreadable name the files the
	// exporter drops, so no reference of theirs is listed.
	Load LoadReport
	// Refs: metric → its references, sorted by file, section, owner, key.
	// Every metric asked about has an entry (empty when nothing refers to it).
	Refs map[string][]KeyRef
	// Unscanned: files the load kept whose keys could not be listed (their
	// bytes could not be re-read, or do not decode into the sections'
	// mapping shapes), file → reason. Their references are unknown.
	Unscanned map[string]string
}

// writtenSections is the shape keys are listed from: every section whose
// keys PlatformKeyFor judges, with the values left undecoded so a value
// shape never hides a key. A superset of ThresholdConfig's decode for these
// four sections, so a file the exporter decodes always decodes here too.
type writtenSections struct {
	Defaults          map[string]yaml.Node            `yaml:"defaults"`
	OptionalOverrides []yaml.Node                     `yaml:"optional_overrides"`
	Tenants           map[string]map[string]yaml.Node `yaml:"tenants"`
	Profiles          map[string]map[string]yaml.Node `yaml:"profiles"`
}

// KeyRefs lists, for each metric, the keys of the tree at dir whose
// platform key (PlatformKeyFor) is that metric — in every file the
// exporter's load keeps (LoadDirReport's scan, minus the files it drops for
// not decoding; a nested `_` file's references are marked Nested), as written, in the
// four sections whose keys name metrics. Every key the file writes is
// listed, a null one too: the question is what the file still says once the
// metric is gone, not what the exporter serves today.
//
// The error is LoadDirReport's: a tree the exporter refuses (no config file,
// a tenant declared twice) is refused here. A tree whose every config file
// is unreadable is not refused: the report then carries no reference and
// the files in Load.Unreadable (#2627).
func KeyRefs(dir string, metrics []string, logger *log.Logger) (KeyRefsReport, error) {
	_, rep, scan, _, err := loadDirReport(dir, logger)
	out := KeyRefsReport{Load: rep, Refs: make(map[string][]KeyRef, len(metrics))}
	for _, m := range metrics {
		out.Refs[m] = []KeyRef{}
	}
	if err != nil {
		return out, err
	}
	want := make(map[string]bool, len(metrics))
	for _, m := range metrics {
		want[m] = true
	}
	dropped := make(map[string]bool, len(rep.ParseFailed))
	for _, pf := range rep.ParseFailed {
		dropped[pf] = true
	}
	nested := false
	add := func(file, section, owner, key string) {
		if pk, ok := PlatformKeyFor(key); ok && want[pk] {
			out.Refs[pk] = append(out.Refs[pk], KeyRef{File: file, Section: section, Owner: owner, Key: key,
				Nested: nested})
		}
	}
	for _, k := range scan.Keys {
		if dropped[k] {
			continue
		}
		nested = isNestedPlatformFile(k)
		f := scan.Files[k]
		data := f.Data
		if data == nil {
			if data, err = os.ReadFile(f.AbsPath); err != nil {
				out.unscanned(k, fmt.Sprintf("cannot re-read: %v", err))
				continue
			}
		}
		var w writtenSections
		if err := yaml.Unmarshal(data, &w); err != nil {
			out.unscanned(k, err.Error())
			continue
		}
		for key := range w.Defaults {
			add(k, "defaults", "", key)
		}
		for _, n := range w.OptionalOverrides {
			if n.Kind == yaml.ScalarNode {
				add(k, "optional_overrides", "", n.Value)
			}
		}
		for owner, body := range w.Tenants {
			for key := range body {
				add(k, "tenants", owner, key)
			}
		}
		for owner, body := range w.Profiles {
			for key := range body {
				add(k, "profiles", owner, key)
			}
		}
	}
	for _, refs := range out.Refs {
		sort.Slice(refs, func(i, j int) bool {
			a, b := refs[i], refs[j]
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
	}
	return out, nil
}

func (r *KeyRefsReport) unscanned(file, reason string) {
	if r.Unscanned == nil {
		r.Unscanned = map[string]string{}
	}
	r.Unscanned[file] = reason
}
