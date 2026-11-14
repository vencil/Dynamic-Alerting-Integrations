package config

// The reserved keys of subtree defaults that a gate reports (#2388, #3341,
// #3881).
//
// subtreeReservedKeys (ScopedTenants.SubtreeReserved) lists every key a
// SUBTREE `_defaults.yaml` of a tenant's chain writes and does not carry.
// Two of those writes are no defect, and only a caller holding the files can
// tell: a file the exporter's load drops writes nothing, and a
// TopLevelReadElsewhere key at the TOP level of a file with no `defaults:`
// wrapper is where its reader takes it from.
//
// ⛔ ONE DEFINITION for both gates (#3881): da-guard's main check
// (subtree_default_reserved_key) and the shared key verdict (`da-guard
// key-verdict`: unread, subtree_not_applied) report the same (tenant, key)
// set because both read it here. Neither filters on its own.

import "gopkg.in/yaml.v3"

// SubtreeReservedReported is reserved (ScopedTenants.SubtreeReserved: tenant
// → key → the subtree defaults files writing it, root-first) less the writes
// that are no defect; a key with no write left, and a tenant with no key
// left, are dropped. nil when nothing is left.
//
//   - A write of a file in parseFailed (ScopedTenants.ParseFailed — since
//     ADR-036 step 6, #3206, every file parse-tree refuses): the exporter
//     drops the file, so it writes no reserved key.
//   - For a TopLevelReadElsewhere key only (#2388 r1b): a write of a file in
//     defaultsFiles (ScopedTenants.DefaultsFiles) that has no `defaults:`
//     mapping. Another reader takes such a key from the TOP level of a
//     defaults file, and the chain view cannot tell the top level from
//     `defaults:` in an unwrapped file (the merge reads the whole document),
//     so only a copy under a `defaults:` mapping is reported — nothing reads
//     it there. (`_custom_alerts` was such a key until #3042.) A file not in
//     defaultsFiles is kept (reported), not guessed.
//
// reserved is not modified.
func SubtreeReservedReported(reserved map[string]map[string][]SubtreeReservedWrite,
	defaultsFiles []DefaultsFile, parseFailed []string) map[string]map[string][]SubtreeReservedWrite {
	if len(reserved) == 0 {
		return nil
	}
	failed := make(map[string]bool, len(parseFailed))
	for _, pf := range parseFailed {
		failed[pf] = true
	}
	readElsewhere := map[string]bool{}
	for _, k := range topLevelReadElsewhere {
		readElsewhere[k] = true
	}
	unwrapped := unwrappedDefaultsFiles(defaultsFiles, failed)
	var out map[string]map[string][]SubtreeReservedWrite
	for id, byKey := range reserved {
		for k, all := range byKey {
			writes := subtreeWritesWithout(all, failed)
			if readElsewhere[k] {
				writes = subtreeWritesWithout(writes, unwrapped)
			}
			if len(writes) == 0 {
				continue
			}
			if out == nil {
				out = map[string]map[string][]SubtreeReservedWrite{}
			}
			if out[id] == nil {
				out[id] = map[string][]SubtreeReservedWrite{}
			}
			out[id][k] = writes
		}
	}
	return out
}

// unwrappedDefaultsFiles is the set of files (root-relative names) with no
// `defaults:` mapping: the defaults merge reads the whole document, so a key
// in the chain view of such a file sits at its TOP level. A file in failed
// is skipped: the exporter reads none of it. Decoded as the defaults-chain
// merge does (yaml.v3, then NormalizeYAMLToJSON); a file that does not decode
// or is not a mapping is not listed.
func unwrappedDefaultsFiles(files []DefaultsFile, failed map[string]bool) map[string]bool {
	out := map[string]bool{}
	for _, f := range files {
		if failed[f.Name] {
			continue
		}
		var raw any
		if err := yaml.Unmarshal(f.Data, &raw); err != nil {
			continue
		}
		if doc, ok := NormalizeYAMLToJSON(raw).(map[string]any); ok && doc != nil && !DefaultsBlockWrapped(doc) {
			out[f.Name] = true
		}
	}
	return out
}

// subtreeWritesWithout is writes minus the ones whose file is in drop, in
// order; writes itself when drop is empty.
func subtreeWritesWithout(writes []SubtreeReservedWrite, drop map[string]bool) []SubtreeReservedWrite {
	if len(drop) == 0 {
		return writes
	}
	var out []SubtreeReservedWrite
	for _, w := range writes {
		if !drop[w.File] {
			out = append(out, w)
		}
	}
	return out
}
