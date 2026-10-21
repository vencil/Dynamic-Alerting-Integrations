package config

// nonspecific_tag_test.go — ADR-036 step 8 (#3206). The vendored yaml.v3 no
// longer carries the patch that kept the non-specific tag `!` on a quoted or
// block scalar, so the decode alone reads `! "<<"` as the literal key "<<"
// and `objective: ! "99.9"` as the string "99.9" — upstream yaml.v3's
// reading, the one the released exporter has. Neither reading is served:
// every decode entry asks parse-tree first, and parse-tree finds `!` in the
// source text, not in Node.Tag.

import (
	"testing"

	"github.com/vencil/threshold-exporter/pkg/parsetree"
	"gopkg.in/yaml.v3"
)

var nonSpecificTagDocs = map[string]string{
	"merge key": "x-m: &m {_metadata: \"owner: x\"}\ntenants:\n  t1:\n    ! \"<<\": *m\n",
	"slo objective": "tenants:\n  t1:\n    _custom_alerts:\n" +
		"      - {recipe: slo_burn_rate, name: avail, metric: err_total, " +
		"denominator_metric: req_total, objective: ! \"99.9\"}\n",
}

// TestNonSpecificTag_RefusedBeforeEveryDecode: the files never reach
// isMergeKey (through stringMetadataEntries) or validateSloObjective.
func TestNonSpecificTag_RefusedBeforeEveryDecode(t *testing.T) {
	t.Parallel()
	for name, doc := range nonSpecificTagDocs {
		data := []byte(doc)
		_, errConfig := ParseConfigFile(data)
		_, errTenant := ParseTenantFile(data)
		for entry, err := range map[string]error{
			"ParseConfigFile": errConfig,
			"ParseTenantFile": errTenant,
			"ParseTenantDoc":  ParseTenantDoc(data).err,
		} {
			if kind, ok := UnreadableKindOf(err); !ok || kind != parsetree.KindNonSpecificTag {
				t.Errorf("%s via %s: err %v, want parse-tree's %s", name, entry, err, parsetree.KindNonSpecificTag)
			}
		}
	}
}

// TestNonSpecificTag_TheDecodeAloneReadsUpstream pins what the decode would
// make of these files without the verdict. Red in the Tag "!" walk or the
// merge-key half means the vendored yaml.v3 keeps `!` again (the patch is
// back): isMergeKey's Tag "!" arm, deleted with the patch, would then be
// needed. The objective half reads the same with or without the patch: the
// `_custom_alerts` passthrough (ScheduledValue) decodes the list into `any`
// and re-serializes it, which drops any tag before validateSloObjective runs.
func TestNonSpecificTag_TheDecodeAloneReadsUpstream(t *testing.T) {
	t.Parallel()
	var walk func(n *yaml.Node) int
	walk = func(n *yaml.Node) int {
		c := 0
		if n.Tag == "!" {
			c++
		}
		for _, k := range n.Content {
			c += walk(k)
		}
		return c
	}
	for name, doc := range nonSpecificTagDocs {
		var n yaml.Node
		if err := yaml.Unmarshal([]byte(doc), &n); err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		if c := walk(&n); c != 0 {
			t.Errorf("%s: %d node(s) carry Tag \"!\"; upstream yaml.v3 drops it", name, c)
		}
	}

	// `! "<<"`: a literal key "<<", to the typed decode and to the merge walk.
	data := []byte(nonSpecificTagDocs["merge key"])
	cfg, err := parseConfigFile(data, true)
	if err != nil {
		t.Fatalf("merge key: the decode alone: %v", err)
	}
	if _, literal := cfg.Tenants["t1"]["<<"]; !literal {
		t.Errorf("merge key: t1 = %v, want the literal key \"<<\"", cfg.Tenants["t1"])
	}
	if _, merged := cfg.Tenants["t1"]["_metadata"]; merged {
		t.Errorf("merge key: t1 = %v, nothing is merged", cfg.Tenants["t1"])
	}
	if got := cfg.StringMetadataEntries(); got != nil || TenantWritesStringMetadata(data, "t1") {
		t.Errorf("merge key: string _metadata listed (%q); the typed decode merged none", got)
	}

	// `objective: ! "99.9"`: the string "99.9", which validateSloObjective accepts.
	cfg, err = parseConfigFile([]byte(nonSpecificTagDocs["slo objective"]), true)
	if err != nil {
		t.Fatalf("slo objective: the decode alone: %v", err)
	}
	if _, objs, errs := resolveTenantCustomAlerts("t1", cfg.Tenants["t1"]); errs != 0 || len(objs) != 1 {
		t.Errorf("slo objective: errs=%d objectives=%d, want the objective read as the string \"99.9\"", errs, len(objs))
	}
}
