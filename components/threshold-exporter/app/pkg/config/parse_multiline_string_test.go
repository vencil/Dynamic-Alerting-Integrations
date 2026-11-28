package config

// #4370: a mapping or sequence value (`_custom_alerts`, `_routing`, …) is
// decoded to interface{} and written back to a YAML string (marshalStable),
// which its readers decode again. A string written there must read back as
// the same string, or what the tenant wrote is not what is judged and served.
//
// yaml.v3 writes a string holding a line break as a literal block (`|`). When
// the string's first character is a tab, the block it writes is one its own
// reader refuses ("found a tab character where an indentation space is
// expected"), so the whole `_custom_alerts` block failed to decode and every
// entry of the tenant was refused. The issue's own case — a leading line
// break, `"\n.*"` — reads back at head (the vendored yaml.v3 writes the
// indentation indicator since #3206); it is pinned here with the rest.

import (
	"strconv"
	"strings"
	"testing"

	"gopkg.in/yaml.v3"
)

var multilineSelectorValues = []string{
	"\n.*",     // the issue's case: leading line break
	"\n\n.*",   // two leading line breaks
	"\n",       // only a line break
	".*\n",     // trailing line break
	"a\nb",     // line break inside
	"\t.*\n",   // leading tab + line break: unreadable literal block
	"\ta\nb",   // leading tab, second line plain
	"\t\n.*",   // tab-only first line
	"\t\t\n\t", // tabs on both lines
	" \n.*",    // leading space + line break
	strings.Repeat("ab", 200) + "\n" + strings.Repeat("c", 300), // long lines
}

// Each value, written as a selectors_re value in a tenant file, is the value
// the exporter decodes and resolves — not a narrower or refused one.
func TestCustomAlertSelector_MultilineValueReadsBack(t *testing.T) {
	t.Parallel()
	for _, v := range multilineSelectorValues {
		data := []byte("tenants:\n  t1:\n    _custom_alerts:\n" +
			"      - recipe: rate\n        name: x\n        metric: m\n" +
			"        selectors_re:\n          k: " + strconv.Quote(v) + "\n" +
			"        op: \">\"\n        window: 5m\n        threshold: \"1:warning\"\n")
		cfg, err := ParseTenantFile(data)
		if err != nil {
			t.Fatalf("%q: parse: %v", v, err)
		}
		specs, err := customAlertSpecsOf(cfg.Tenants["t1"])
		if err != nil {
			t.Errorf("%q: _custom_alerts does not decode: %v\nDefault:\n%s",
				v, err, cfg.Tenants["t1"]["_custom_alerts"].Default)
			continue
		}
		if len(specs) != 1 || specs[0].SelectorsRe["k"] != v {
			t.Errorf("%q: read back as %+v", v, specs)
			continue
		}
		rows, _, errs := resolveTenantCustomAlerts("t1", cfg.Tenants["t1"])
		if errs != 0 || len(rows) != 1 {
			t.Errorf("%q: resolve: errs=%d rows=%d", v, errs, len(rows))
		}
	}
}

// The arbitrary-mapping branch writes through the same marshal: a value under
// a mapping without `default` reads back too. Only values: a multi-line key is
// not covered (hasUnkeptBlockString).
func TestScheduledValue_MappingMultilineStringReadsBack(t *testing.T) {
	t.Parallel()
	for _, v := range multilineSelectorValues {
		doc := "k: " + strconv.Quote(v) + "\nj: plain\n"
		var sv ScheduledValue
		if err := yaml.Unmarshal([]byte(doc), &sv); err != nil {
			t.Fatalf("%q: unmarshal: %v", v, err)
		}
		node, _, err := structureOf(sv)
		var back map[string]string
		if err == nil {
			err = node.Decode(&back)
		}
		if err != nil || back["k"] != v || back["j"] != "plain" {
			t.Errorf("%q: read back as %q (err %v)\nDefault:\n%s", v, back, err, sv.Default)
		}
	}
}

// A reserved mapping that carries a `default` key (`_routing`, `_metadata`)
// takes another path in a tenant file (keepReservedBodies); its body must read
// back too, or the whole block is read as not written.
func TestReservedMappingWithDefault_MultilineStringReadsBack(t *testing.T) {
	t.Parallel()
	for _, v := range multilineSelectorValues {
		data := []byte("tenants:\n  t1:\n    _routing:\n      default: x\n" +
			"      note: " + strconv.Quote(v) + "\n")
		cfg, err := ParseTenantFile(data)
		if err != nil {
			t.Fatalf("%q: parse: %v", v, err)
		}
		sv := cfg.Tenants["t1"]["_routing"]
		node, _, err := structureOf(sv)
		var back map[string]string
		if err == nil {
			err = node.Decode(&back)
		}
		if err != nil || back["note"] != v || back["default"] != "x" {
			t.Errorf("%q: read back as %q (err %v)\nDefault:\n%s", v, back, err, sv.Default)
		}
	}
}

// A tree whose every multi-line string reads back from its literal block
// keeps yaml.Marshal's exact output: only a string the block cannot carry is
// written another way.
func TestMarshalStable_KeptBlockString_OutputUnchanged(t *testing.T) {
	t.Parallel()
	in := map[string]any{"note": "a\nb", "lead": "\n.*"}
	want, err := yaml.Marshal(in)
	if err != nil {
		t.Fatal(err)
	}
	got, err := marshalStable(in)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != string(want) {
		t.Errorf("output changed:\n--- got ---\n%s--- want ---\n%s", got, want)
	}
}

// A tree without a multi-line string keeps yaml.Marshal's exact output: the
// fix changes how a multi-line string is written and nothing else.
func TestMarshalStable_NoMultilineString_OutputUnchanged(t *testing.T) {
	t.Parallel()
	in := []any{map[string]any{"recipe": "rate", "selectors_re": map[string]any{"k": "\t.*", "j": " a"}}}
	want, err := yaml.Marshal(in)
	if err != nil {
		t.Fatal(err)
	}
	got, err := marshalStable(in)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != string(want) {
		t.Errorf("output changed:\n--- got ---\n%s--- want ---\n%s", got, want)
	}
}
