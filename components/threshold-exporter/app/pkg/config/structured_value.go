package config

import "gopkg.in/yaml.v3"

// The one place a reader gets a structured reserved value's structure back
// (#3638 stage 2).
//
// ScheduledValue.Default is a string. A YAML mapping or sequence written for
// a reserved key (`_silent_mode: {target: all}`, `_custom_alerts: [...]`)
// reaches its reader as that value re-serialised (UnmarshalYAML), with
// ScheduledValue.structured set. A reader used to guess the structure from
// the text — `strings.Contains(val, "target:")` — so a scalar that merely
// read like YAML (`_silent_mode: "target: critical"`) acted as the mapping.
// The structured flag is the structure's kind; this file is the only
// decode of the text back into it. default_text_guard_test.go holds every
// other site to that.
//
// What the text still loses: the generic decode before re-serialising
// normalises a number's spelling (`0x1F` → 31). That is not judged here or
// anywhere at runtime (#3764 owner ruling): the spelling is refused at the
// entry, by validate-config's yaml_quoting row (`_lib_io`: a number in a
// string field, or in a number field written other than in plain decimal),
// which every reader shares through parse-tree's tags. ⚠️ In direct mode a
// tree changed without that row (a commit straight to git) is not judged
// until #3353's whole-tree verdict runs it.

// structureOf returns sv's structure — the node of the mapping or sequence
// its Default was written as — and structured=true; nil and false for a
// scalar, whose text is never read as YAML here. err is the decode's when
// the text does not parse (not reachable for a value UnmarshalYAML built).
func structureOf(sv ScheduledValue) (node *yaml.Node, structured bool, err error) {
	if !sv.structured {
		return nil, false, nil
	}
	var doc yaml.Node
	if err := yaml.Unmarshal([]byte(sv.Default), &doc); err != nil {
		return nil, true, err
	}
	if doc.Kind == yaml.DocumentNode && len(doc.Content) == 1 {
		return doc.Content[0], true, nil
	}
	return &doc, true, nil
}

// mappingHasKey reports whether n is a mapping with key k.
func mappingHasKey(n *yaml.Node, k string) bool {
	if n == nil || n.Kind != yaml.MappingNode {
		return false
	}
	for i := 0; i+1 < len(n.Content); i += 2 {
		if n.Content[i].Value == k {
			return true
		}
	}
	return false
}
