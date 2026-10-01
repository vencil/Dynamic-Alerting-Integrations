package main

import (
	"fmt"
	"sort"
	"strconv"

	yamlv2 "go.yaml.in/yaml/v2"
)

// keyCollision returns an error when two keys of one mapping in *data* are
// different YAML values but the same JSON key (#2476): `010:` (the int 8)
// and `"8":` (the string "8"). YAMLToJSON decodes the mapping into a Go map
// and converts each key to a string while ranging over it, so which of the
// two values survives depends on Go's map iteration order — the same file
// converts differently from run to run.
//
// The document is decoded once more with go-yaml v2 (the decoder YAMLToJSON
// is built on) the way YAMLToJSON decodes it, into map[interface{}]interface{}:
// `<<` merges are applied (TestMapDecodeAppliesMerge), and keys that are the
// SAME value (`t1:` written twice, or `8:` and `010:`, both the int 8) are
// already one key, the last one — as in YAMLToJSON, every time. Two keys left
// in one map that convert to the same string are the collision.
// Only the first document is checked.
func keyCollision(data []byte) error {
	var doc interface{}
	if err := yamlv2.Unmarshal(data, &doc); err != nil {
		return err
	}
	return walkCollisions(doc, "")
}

// keyEntry is one key of a decoded map, with the string YAMLToJSON makes of it.
type keyEntry struct {
	key  interface{}
	json string
	desc string
}

func walkCollisions(v interface{}, path string) error {
	switch t := v.(type) {
	case map[interface{}]interface{}:
		entries := make([]keyEntry, 0, len(t))
		for k := range t {
			ks, ok := jsonKey(k)
			if !ok {
				continue // YAMLToJSON already refused an unsupported key
			}
			entries = append(entries, keyEntry{k, ks, describe(k)})
		}
		// Map order is random; sort so the error and the walk are the same
		// on every run.
		sort.Slice(entries, func(i, j int) bool {
			if entries[i].json != entries[j].json {
				return entries[i].json < entries[j].json
			}
			return entries[i].desc < entries[j].desc
		})
		for i := 1; i < len(entries); i++ {
			if entries[i].json == entries[i-1].json {
				where := path
				if where == "" {
					where = "the top level"
				}
				return fmt.Errorf("in %s, the keys %s and %s are different YAML values but "+
					"the same JSON key %q, and which one is kept is not fixed; quote the key "+
					"so it is written the way it is meant", where, entries[i-1].desc, entries[i].desc,
					entries[i].json)
			}
		}
		for _, e := range entries {
			if err := walkCollisions(t[e.key], joinPath(path, e.json)); err != nil {
				return err
			}
		}
	case []interface{}:
		for i, item := range t {
			if err := walkCollisions(item, fmt.Sprintf("%s[%d]", path, i)); err != nil {
				return err
			}
		}
	}
	return nil
}

func joinPath(path, key string) string {
	if path == "" {
		return key
	}
	return path + "." + key
}

// describe shows a decoded key with its type: `8 (int)`, `"8" (string)`.
func describe(k interface{}) string {
	if s, ok := k.(string); ok {
		return strconv.Quote(s) + " (string)"
	}
	return fmt.Sprintf("%v (%T)", k, k)
}

// jsonKey is the string YAMLToJSON turns a decoded map key into: the type
// switch of convertToJSONableObject in sigs.k8s.io/yaml v1.6.0 (yaml.go),
// copied. TestJSONKeyMatchesYAMLToJSON re-measures it against YAMLToJSON.
// ok is false for a key type YAMLToJSON refuses.
func jsonKey(k interface{}) (string, bool) {
	switch t := k.(type) {
	case string:
		return t, true
	case int:
		return strconv.Itoa(t), true
	case int64:
		return strconv.FormatInt(t, 10), true
	case float64:
		s := strconv.FormatFloat(t, 'g', -1, 32)
		switch s {
		case "+Inf":
			s = ".inf"
		case "-Inf":
			s = "-.inf"
		case "NaN":
			s = ".nan"
		}
		return s, true
	case bool:
		if t {
			return "true", true
		}
		return "false", true
	}
	return "", false
}
