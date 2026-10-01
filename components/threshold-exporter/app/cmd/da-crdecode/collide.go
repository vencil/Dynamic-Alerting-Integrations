package main

import (
	"errors"
	"fmt"
	"reflect"
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
// Keys that are the SAME value (`t1:` written twice, or `8:` and `010:`,
// both the int 8) are left alone: go-yaml keeps the last one, every time.
//
// The document is decoded once more with go-yaml v2 (the decoder YAMLToJSON
// is built on) into MapSlice, which keeps every key of a mapping in order.
// ⚠️ Not covered: keys a `<<` merge brings in — v2's MapSlice decoding drops
// them, so a merged key colliding with a written one is not seen here.
// Only the first document is checked; a top-level that is not a mapping is
// left to the caller (it is no ThresholdConfig).
func keyCollision(data []byte) error {
	var doc yamlv2.MapSlice
	if err := yamlv2.Unmarshal(data, &doc); err != nil {
		var te *yamlv2.TypeError
		if errors.As(err, &te) {
			return nil
		}
		return err
	}
	return walkCollisions(doc, "")
}

func walkCollisions(v interface{}, path string) error {
	switch t := v.(type) {
	case yamlv2.MapSlice:
		seen := map[string]interface{}{}
		for _, item := range t {
			ks, ok := jsonKey(item.Key)
			if !ok {
				continue // YAMLToJSON already refused an unsupported key
			}
			if prev, dup := seen[ks]; dup && !reflect.DeepEqual(prev, item.Key) {
				where := path
				if where == "" {
					where = "the top level"
				}
				return fmt.Errorf("in %s, the keys %s and %s are different YAML values but "+
					"the same JSON key %q, and which one is kept is not fixed; quote the key "+
					"so it is written the way it is meant", where, describe(prev), describe(item.Key), ks)
			}
			seen[ks] = item.Key
			if err := walkCollisions(item.Value, joinPath(path, ks)); err != nil {
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
